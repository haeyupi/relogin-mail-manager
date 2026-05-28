import email
import imaplib
import re
import time
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from curl_cffi import requests as curl_requests

from mail_store import normalize_email


GRAPH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
IMAP_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def extract_code(*parts):
    text = " ".join(str(part or "") for part in parts)
    match = CODE_RE.search(text)
    return match.group(1) if match else ""


def decode_mime_header(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def account_email(account):
    return normalize_email((account or {}).get("email"))


class LocalMailReader:
    def __init__(self, timeout=30):
        self.timeout = int(timeout or 30)

    def poll_code(self, account, context=None, timeout=120, interval=3, limit=20):
        context = context or {}
        deadline = time.time() + max(1, int(timeout or 120))
        excluded = {str(code).strip() for code in context.get("exclude_codes") or [] if str(code).strip()}
        after_ts = int(context.get("after_timestamp") or 0)
        last_error = ""
        while time.time() < deadline:
            try:
                messages = self.messages(account, limit=limit)
            except Exception as exc:
                last_error = str(exc)
                messages = []
            for msg in messages:
                code = msg.get("verification_code") or extract_code(
                    msg.get("subject"), msg.get("text"), msg.get("content")
                )
                if not code or code in excluded:
                    continue
                msg_ts = int(msg.get("timestamp_ms") or 0)
                if after_ts and msg_ts and msg_ts < after_ts * 1000 - 5000:
                    continue
                return code
            time.sleep(max(1, int(interval or 3)))
        if last_error:
            raise RuntimeError(f"Email OTP timeout; last mail reader error: {last_error}")
        raise RuntimeError("Email OTP timeout")

    def messages(self, account, limit=20):
        errors = []
        try:
            return self.graph_messages(account, limit=limit)
        except Exception as exc:
            errors.append(f"graph: {exc}")
        for host, method in [
            ("outlook.live.com", "imap_new"),
            ("outlook.office365.com", "imap_old"),
        ]:
            try:
                return self.imap_messages(account, host, method, limit=limit)
            except Exception as exc:
                errors.append(f"{method}: {exc}")
        raise RuntimeError("; ".join(errors))

    def access_token(self, account, endpoint, scope):
        client_id = str((account or {}).get("client_id") or "").strip()
        refresh_token = str((account or {}).get("mail_refresh_token") or (account or {}).get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("mail client_id and refresh_token are required")
        response = curl_requests.post(
            endpoint,
            data=urlencode(
                {
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": scope,
                }
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            impersonate="chrome",
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"token endpoint failed: HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("token endpoint did not return access_token")
        return token

    def graph_messages(self, account, limit=20):
        token = self.access_token(account, GRAPH_TOKEN_URL, "https://graph.microsoft.com/.default")
        params = urlencode(
            {
                "$top": max(1, min(50, int(limit or 20))),
                "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body",
                "$orderby": "receivedDateTime desc",
            }
        )
        response = curl_requests.get(
            f"{GRAPH_MESSAGES_URL}?{params}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Prefer": "outlook.body-content-type='text'",
            },
            impersonate="chrome",
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"graph failed: HTTP {response.status_code}: {response.text[:300]}")
        out = []
        for item in response.json().get("value") or []:
            body = item.get("body") or {}
            text = item.get("bodyPreview") or body.get("content") or ""
            ts = parse_timestamp(item.get("receivedDateTime"))
            from_addr = (((item.get("from") or {}).get("emailAddress") or {}).get("address") or "")
            subject = item.get("subject") or ""
            out.append(
                {
                    "id": item.get("id") or "",
                    "email_address": account_email(account),
                    "from_address": from_addr,
                    "subject": subject,
                    "content": text,
                    "text": text,
                    "timestamp_ms": ts,
                    "method": "graph",
                    "verification_code": extract_code(subject, text),
                }
            )
        return out

    def imap_messages(self, account, host, method, limit=20):
        token = self.access_token(account, IMAP_TOKEN_URL, "https://outlook.office.com/IMAP.AccessAsUser.All offline_access")
        with imaplib.IMAP4_SSL(host, 993, timeout=self.timeout) as conn:
            auth = f"user={account_email(account)}\x01auth=Bearer {token}\x01\x01"
            conn.authenticate("XOAUTH2", lambda _: auth.encode("utf-8"))
            status, _ = conn.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("imap select INBOX failed")
            status, data = conn.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("imap search failed")
            ids = (data[0] or b"").split()
            ids = ids[-max(1, min(50, int(limit or 20))) :]
            out = []
            for msg_id in reversed(ids):
                status, msg_data = conn.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = next((part[1] for part in msg_data if isinstance(part, tuple) and len(part) > 1), b"")
                if not raw:
                    continue
                out.append(self.parse_imap_message(raw, msg_id.decode("ascii", "ignore"), account, method))
            return out

    def parse_imap_message(self, raw, msg_id, account, method):
        parsed = email.message_from_bytes(raw)
        subject = decode_mime_header(parsed.get("Subject", ""))
        from_addr = decode_mime_header(parsed.get("From", ""))
        text = message_text(parsed)
        ts = parse_timestamp(parsed.get("Date"))
        return {
            "id": msg_id,
            "email_address": account_email(account),
            "from_address": from_addr,
            "subject": subject,
            "content": text,
            "text": text,
            "timestamp_ms": ts,
            "method": method,
            "verification_code": extract_code(subject, text),
        }


def parse_timestamp(value):
    if not value:
        return 0
    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000)
    except Exception:
        pass
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return int(datetime.fromisoformat(raw).timestamp() * 1000)
    except Exception:
        pass
    try:
        return int(time.mktime(time.strptime(str(value).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")) * 1000)
    except Exception:
        return 0


def message_text(message):
    if message.is_multipart():
        parts = []
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if content_type in {"text/plain", "text/html"}:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts)
    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")
