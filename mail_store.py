import json
import io
import sqlite3
import time
import zipfile
from pathlib import Path


VALID_STATUSES = {"unknown", "normal", "dropped", "unavailable"}


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_email(value):
    return str(value or "").strip().lower()


def strip_gpt_prefix(value):
    value = str(value or "").strip()
    return value[4:] if value.startswith("gpt_") else value


def account_line_parts(line):
    parts = str(line or "").strip().split("----")
    if len(parts) < 4:
        raise ValueError("expected email----password----client_id----refresh_token")
    email = normalize_email(parts[0])
    if not email or "@" not in email:
        raise ValueError(f"invalid email {parts[0]!r}")
    return {
        "email": email,
        "mailbox_password": parts[1].strip(),
        "client_id": parts[2].strip(),
        "mail_refresh_token": "----".join(parts[3:]).strip(),
    }


def parse_mapping_lines(raw, *, strip_prefix=False):
    out = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) < 2:
            continue
        email = normalize_email(parts[0])
        value = "----".join(parts[1:]).strip()
        if strip_prefix:
            value = strip_gpt_prefix(value)
        if email and value:
            out[email] = value
    return out


def read_zip_text(entries, name):
    data = entries.get(name)
    if data is None:
        suffix = "/" + name.lower()
        for entry_name, entry_data in entries.items():
            lower = entry_name.lower()
            if lower == name.lower() or lower.endswith(suffix):
                data = entry_data
                break
    if data is None:
        return ""
    return data.decode("utf-8-sig", errors="replace")


def collect_zip_entries(zip_path):
    entries = {}
    with zipfile.ZipFile(zip_path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            normalized = info.filename.replace("\\", "/").lstrip("/")
            entries[normalized] = archive.read(info)
            if normalized.lower().endswith(".zip"):
                prefix = normalized.rsplit("/", 1)[0]
                prefix = prefix + "/" if prefix else ""
                try:
                    with zipfile.ZipFile(io.BytesIO(entries[normalized]), "r") as nested:
                        for nested_info in nested.infolist():
                            if nested_info.is_dir():
                                continue
                            nested_name = prefix + nested_info.filename.replace("\\", "/").lstrip("/")
                            entries.setdefault(nested_name, nested.read(nested_info))
                except zipfile.BadZipFile:
                    pass
    return entries


def remote_hints_from_zip(entries):
    hints = {}
    for name, raw in entries.items():
        lower = name.lower()
        if (lower.startswith("cpa/") or "/cpa/" in lower) and lower.endswith(".json"):
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except Exception:
                continue
            email = normalize_email(data.get("email") or data.get("_email"))
            if not email:
                continue
            hints.setdefault(email, {})["cpa_name"] = Path(name).name
        if lower.endswith("sub2api/accounts.json") or (
            (lower.startswith("sub2api/") or "/sub2api/" in lower) and lower.endswith(".json")
        ):
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except Exception:
                continue
            accounts = data.get("accounts") if isinstance(data, dict) else data
            if not isinstance(accounts, list):
                continue
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                creds = account.get("credentials") or {}
                email = normalize_email(creds.get("email") or account.get("email") or account.get("name"))
                if not email:
                    continue
                hint = hints.setdefault(email, {})
                hint["sub2api_id"] = account.get("id") or hint.get("sub2api_id")
                hint["sub2api_name"] = account.get("name") or hint.get("sub2api_name")
    return hints


class MailStore:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.init()

    def close(self):
        self.db.close()

    def init(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_accounts (
                email TEXT PRIMARY KEY,
                mailbox_password TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL DEFAULT '',
                mail_refresh_token TEXT NOT NULL DEFAULT '',
                gpt_password TEXT NOT NULL DEFAULT '',
                phone_number TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unknown',
                failure_reason TEXT NOT NULL DEFAULT '',
                remote_provider TEXT NOT NULL DEFAULT '',
                remote_id TEXT NOT NULL DEFAULT '',
                remote_name TEXT NOT NULL DEFAULT '',
                last_synced_at TEXT NOT NULL DEFAULT '',
                last_relogin_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_mail_accounts_status ON mail_accounts(status)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_mail_accounts_remote_provider ON mail_accounts(remote_provider)")
        self.db.commit()

    def import_zip(self, zip_path):
        entries = collect_zip_entries(zip_path)
        mail_text = read_zip_text(entries, "mail/mail_accounts.txt")
        if not mail_text:
            raise RuntimeError("ZIP missing mail/mail_accounts.txt")
        gpt_by_email = parse_mapping_lines(read_zip_text(entries, "mail/gpt_passwords.txt"), strip_prefix=True)
        phone_by_email = parse_mapping_lines(read_zip_text(entries, "mail/phone_numbers.txt"))
        hints = remote_hints_from_zip(entries)
        imported = updated = failed = 0
        errors = []
        for index, line in enumerate(mail_text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                account = account_line_parts(line)
            except ValueError as exc:
                failed += 1
                errors.append(f"line {index}: {exc}")
                continue
            email = account["email"]
            account["gpt_password"] = gpt_by_email.get(email, "")
            account["phone_number"] = phone_by_email.get(email, "")
            hint = hints.get(email, {})
            if hint.get("cpa_name"):
                account["remote_provider"] = "cpa"
                account["remote_name"] = str(hint["cpa_name"])
            elif hint.get("sub2api_id") or hint.get("sub2api_name"):
                account["remote_provider"] = "sub2api"
                account["remote_id"] = str(hint.get("sub2api_id") or "")
                account["remote_name"] = str(hint.get("sub2api_name") or "")
            changed = self.upsert_imported(account)
            if changed == "inserted":
                imported += 1
            else:
                updated += 1
        return {"imported": imported, "updated": updated, "failed": failed, "errors": errors}

    def upsert_imported(self, account):
        now = utc_now()
        existing = self.get(account["email"], missing_ok=True)
        params = {
            "email": account["email"],
            "mailbox_password": account.get("mailbox_password", ""),
            "client_id": account.get("client_id", ""),
            "mail_refresh_token": account.get("mail_refresh_token", ""),
            "gpt_password": account.get("gpt_password", ""),
            "phone_number": account.get("phone_number", ""),
            "remote_provider": account.get("remote_provider", existing.get("remote_provider", "") if existing else ""),
            "remote_id": account.get("remote_id", existing.get("remote_id", "") if existing else ""),
            "remote_name": account.get("remote_name", existing.get("remote_name", "") if existing else ""),
            "now": now,
        }
        if existing:
            self.db.execute(
                """
                UPDATE mail_accounts
                SET mailbox_password=:mailbox_password, client_id=:client_id,
                    mail_refresh_token=:mail_refresh_token, gpt_password=:gpt_password,
                    phone_number=:phone_number, status='unknown', failure_reason='',
                    remote_provider=:remote_provider, remote_id=:remote_id,
                    remote_name=:remote_name, updated_at=:now
                WHERE email=:email
                """,
                params,
            )
            self.db.commit()
            return "updated"
        self.db.execute(
            """
            INSERT INTO mail_accounts (
                email, mailbox_password, client_id, mail_refresh_token, gpt_password,
                phone_number, status, remote_provider, remote_id, remote_name,
                created_at, updated_at
            )
            VALUES (
                :email, :mailbox_password, :client_id, :mail_refresh_token, :gpt_password,
                :phone_number, 'unknown', :remote_provider, :remote_id, :remote_name,
                :now, :now
            )
            """,
            params,
        )
        self.db.commit()
        return "inserted"

    def list(self, status="", search=""):
        query = "SELECT * FROM mail_accounts WHERE 1=1"
        args = []
        if status:
            query += " AND status=?"
            args.append(status)
        if search:
            query += " AND (email LIKE ? OR phone_number LIKE ? OR remote_name LIKE ? OR failure_reason LIKE ?)"
            like = f"%{search}%"
            args.extend([like, like, like, like])
        query += (
            " ORDER BY CASE status WHEN 'dropped' THEN 0 WHEN 'unavailable' THEN 1 "
            "WHEN 'unknown' THEN 2 WHEN 'normal' THEN 3 ELSE 4 END, email ASC"
        )
        return [dict(row) for row in self.db.execute(query, args).fetchall()]

    def summary(self):
        out = {"total": 0, "unknown": 0, "normal": 0, "dropped": 0, "unavailable": 0}
        for row in self.db.execute("SELECT status, COUNT(*) count FROM mail_accounts GROUP BY status"):
            out[row["status"]] = row["count"]
            out["total"] += row["count"]
        return out

    def get(self, email, missing_ok=False):
        row = self.db.execute("SELECT * FROM mail_accounts WHERE email=?", (normalize_email(email),)).fetchone()
        if not row:
            if missing_ok:
                return None
            raise RuntimeError(f"mail account not found: {email}")
        return dict(row)

    def set_status(self, email, status, failure_reason="", *, remote_provider=None, remote_id=None, remote_name=None):
        if status not in VALID_STATUSES:
            raise RuntimeError(f"invalid mail status: {status}")
        fields = ["status=?", "failure_reason=?", "updated_at=?"]
        args = [status, str(failure_reason or ""), utc_now()]
        if status in {"normal", "dropped"}:
            fields.append("last_synced_at=?")
            args.append(utc_now())
        if remote_provider is not None:
            fields.append("remote_provider=?")
            args.append(str(remote_provider or ""))
        if remote_id is not None:
            fields.append("remote_id=?")
            args.append(str(remote_id or ""))
        if remote_name is not None:
            fields.append("remote_name=?")
            args.append(str(remote_name or ""))
        args.append(normalize_email(email))
        self.db.execute(f"UPDATE mail_accounts SET {', '.join(fields)} WHERE email=?", args)
        self.db.commit()

    def mark_relogin_success(self, email, *, remote_provider="", remote_id="", remote_name=""):
        now = utc_now()
        self.db.execute(
            """
            UPDATE mail_accounts
            SET status='normal', failure_reason='', remote_provider=?, remote_id=?, remote_name=?,
                last_relogin_at=?, last_synced_at=?, updated_at=?
            WHERE email=?
            """,
            (remote_provider, str(remote_id or ""), str(remote_name or ""), now, now, now, normalize_email(email)),
        )
        self.db.commit()

    def update_gpt_password(self, email, password):
        self.db.execute(
            "UPDATE mail_accounts SET gpt_password=?, updated_at=? WHERE email=?",
            (strip_gpt_prefix(password), utc_now(), normalize_email(email)),
        )
        self.db.commit()
