import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse, urlunparse

from curl_cffi import requests as curl_requests

from cpa_provider import request_cpa_management_json, request_cpa_oauth_url, submit_cpa_oauth_callback
from mail_store import normalize_email


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def compact_dict(data):
    return {k: v for k, v in (data or {}).items() if v not in (None, "")}


def normalize_base_url(raw_url, default_scheme="https"):
    raw = str(raw_url or "").strip()
    if not raw:
        raise RuntimeError("尚未配置目标服务地址。")
    if raw.lower().startswith("http:") and not raw.lower().startswith("http://"):
        raw = "http://" + raw[5:].lstrip("/")
    if raw.lower().startswith("https:") and not raw.lower().startswith("https://"):
        raw = "https://" + raw[6:].lstrip("/")
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"{default_scheme}://{raw}"
    parsed = urlparse(raw)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")


def response_json(response, label):
    text = str(getattr(response, "text", "") or "")
    try:
        data = response.json() if text else {}
    except Exception:
        data = {"raw": text}
    if response.status_code >= 400:
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("error") or data.get("raw") or "").strip()
        raise RuntimeError(f"{label} 返回 HTTP {response.status_code}: {message or response.reason}")
    return data


def response_data(data):
    if isinstance(data, dict) and "data" in data:
        return data.get("data")
    return data


def extract_list(data, *keys):
    data = response_data(data)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys or ("items", "accounts", "files"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def target_config(cfg):
    target = dict((cfg.get("target") or {}))
    provider = str(target.get("provider") or "").strip().lower()
    if provider in {"none", "disabled", "off"}:
        provider = ""

    # Backward compatibility for existing config.json files that only have cpa.enabled.
    legacy_cpa = cfg.get("cpa") or {}
    if not provider and legacy_cpa.get("enabled"):
        provider = "cpa"
        if not target.get("base_url"):
            target["base_url"] = legacy_cpa.get("base_url") or ""
        if not target.get("management_key"):
            target["management_key"] = legacy_cpa.get("management_key") or ""

    if provider not in {"", "cpa", "sub2api"}:
        raise RuntimeError(f"不支持的目标 provider: {provider}")

    base_url = str(target.get("base_url") or target.get("url") or "").strip()
    management_key = str(
        target.get("management_key")
        or target.get("key")
        or target.get("api_key")
        or target.get("password")
        or ""
    ).strip()
    return {
        **target,
        "provider": provider,
        "enabled": bool(provider and base_url and management_key),
        "base_url": base_url,
        "management_key": management_key,
        "request_timeout": int(target.get("request_timeout") or 45),
        "poll_timeout": int(target.get("poll_timeout") or 120),
        "poll_interval": max(0.25, float(target.get("poll_interval") or 1)),
        "sub2api_concurrency": int(target.get("sub2api_concurrency") or 3),
        "sub2api_priority": int(target.get("sub2api_priority") or 50),
    }


@dataclass
class RemoteAccount:
    provider: str
    email: str = ""
    remote_id: str = ""
    name: str = ""
    raw: dict | None = None

    def store_fields(self):
        return {
            "remote_provider": self.provider,
            "remote_id": self.remote_id,
            "remote_name": self.name,
        }


def possible_account_names(account):
    email = normalize_email((account or {}).get("email"))
    phone = str((account or {}).get("phone_number") or "").strip().replace("+", "")
    names = {email}
    for key in ("remote_name", "remote_id"):
        value = str((account or {}).get(key) or "").strip()
        if value:
            names.add(value.lower())
    if phone:
        names.update({f"auth_{phone}.json", f"codex-{phone}.json", f"cpa-{phone}.json", phone})
    return {name for name in names if name}


def account_email_from_payload(payload):
    if not isinstance(payload, dict):
        return ""
    creds = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    id_token = payload.get("id_token") if isinstance(payload.get("id_token"), dict) else {}
    return normalize_email(
        payload.get("email")
        or payload.get("_email")
        or creds.get("email")
        or id_token.get("email")
        or payload.get("account")
        or ""
    )


class BaseProvider:
    provider = ""

    def __init__(self, cfg, config):
        self.cfg = cfg
        self.config = config

    def require_enabled(self):
        if not self.config.get("enabled"):
            raise RuntimeError("请先配置 target.provider、target.base_url 和 target.management_key。")

    def list_accounts(self):
        return []

    def find_match(self, account, remotes=None):
        remotes = remotes if remotes is not None else self.list_accounts()
        email = normalize_email((account or {}).get("email"))
        remote_id = str((account or {}).get("remote_id") or "").strip()
        names = possible_account_names(account)
        for remote in remotes:
            if remote_id and remote.remote_id and str(remote.remote_id) == remote_id:
                return remote
            if email and remote.email == email:
                return remote
            if remote.name and remote.name.lower() in names:
                return remote
        return None

    def request_oauth_url(self):
        raise RuntimeError(f"{self.provider} 不支持远端 OAuth URL。")

    def submit_oauth_callback(self, redirect_url, state=None):
        raise RuntimeError(f"{self.provider} 不支持 OAuth callback 上传。")

    def upload_token_result(self, account, token_result):
        raise RuntimeError(f"{self.provider} 不支持本地 token 上传。")


class NullProvider(BaseProvider):
    provider = ""

    def require_enabled(self):
        return None

    def upload_token_result(self, account, token_result):
        return {"provider": "", "uploaded": False, "message": "未配置远端 provider，仅保存本地 token"}


class CpaProvider(BaseProvider):
    provider = "cpa"

    def _cfg(self):
        cpa_cfg = {
            "enabled": True,
            "base_url": self.config["base_url"],
            "management_key": self.config["management_key"],
            "poll_timeout": self.config["poll_timeout"],
            "poll_interval": self.config["poll_interval"],
            "request_timeout": self.config["request_timeout"],
        }
        return {**self.cfg, "cpa": cpa_cfg}

    def list_accounts(self):
        self.require_enabled()
        data = request_cpa_management_json(self._cfg(), "/auth-files")
        remotes = []
        for item in extract_list(data, "files"):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "").strip()
            email = account_email_from_payload(item)
            remotes.append(
                RemoteAccount(
                    provider=self.provider,
                    email=email,
                    remote_id=str(item.get("id") or name or "").strip(),
                    name=name,
                    raw=item,
                )
            )
        return remotes

    def request_oauth_url(self):
        self.require_enabled()
        return request_cpa_oauth_url(self._cfg())

    def submit_oauth_callback(self, redirect_url, state=None):
        self.require_enabled()
        result = submit_cpa_oauth_callback(self._cfg(), redirect_url, state)
        return {
            **result,
            "provider": self.provider,
            "remote_id": result.get("state") or "",
            "remote_name": "",
        }


class Sub2ApiProvider(BaseProvider):
    provider = "sub2api"

    def __init__(self, cfg, config):
        super().__init__(cfg, config)
        self.base_url = normalize_base_url(config.get("base_url") or "")

    def headers(self, include_json=False):
        key = self.config["management_key"]
        headers = {
            "Accept": "application/json",
            "x-api-key": key,
            "Authorization": f"Bearer {key}",
        }
        if include_json:
            headers["Content-Type"] = "application/json"
        return headers

    def request_json(self, method, endpoint, body=None, query=None):
        self.require_enabled()
        endpoint = "/" + str(endpoint or "").lstrip("/")
        url = f"{self.base_url}{endpoint}"
        query = {k: v for k, v in (query or {}).items() if v not in (None, "")}
        if query:
            url = f"{url}?{urlencode(query)}"
        response = curl_requests.request(
            str(method or "GET").upper(),
            url,
            headers=self.headers(include_json=body is not None),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            impersonate="chrome",
            timeout=int(self.config.get("request_timeout") or 45),
        )
        return response_json(response, f"Sub2API {method} {endpoint}")

    def list_accounts(self):
        page = 1
        page_size = 1000
        remotes = []
        while True:
            data = self.request_json(
                "GET",
                "/api/v1/admin/accounts",
                query={
                    "page": page,
                    "page_size": page_size,
                    "platform": "openai",
                    "type": "oauth",
                    "sort_by": "name",
                    "sort_order": "asc",
                },
            )
            payload = response_data(data) or {}
            items = extract_list(payload, "items", "accounts")
            for item in items:
                if not isinstance(item, dict):
                    continue
                remotes.append(
                    RemoteAccount(
                        provider=self.provider,
                        email=account_email_from_payload(item),
                        remote_id=str(item.get("id") or "").strip(),
                        name=str(item.get("name") or "").strip(),
                        raw=item,
                    )
                )
            total = int((payload or {}).get("total") or len(remotes) or 0) if isinstance(payload, dict) else len(remotes)
            pages = int((payload or {}).get("pages") or 0) if isinstance(payload, dict) else 0
            if not items or (pages and page >= pages) or (total and len(remotes) >= total):
                break
            page += 1
            if page > 100:
                raise RuntimeError("Sub2API 账号分页超过 100 页，已停止以避免无限分页。")
        return remotes

    def token_credentials(self, account, token_result):
        tokens = (token_result or {}).get("tokens") or {}
        credentials = compact_dict(
            {
                "access_token": tokens.get("access_token"),
                "refresh_token": tokens.get("refresh_token"),
                "id_token": tokens.get("id_token"),
                "expires_at": tokens.get("expires_at"),
                "client_id": (self.cfg.get("chatgpt") or {}).get("codex_client_id"),
                "email": normalize_email((account or {}).get("email") or token_result.get("_email")),
                "chatgpt_account_id": tokens.get("account_id"),
            }
        )
        if not credentials.get("access_token"):
            raise RuntimeError("Sub2API 上传需要 access_token。")
        return credentials

    def upload_token_result(self, account, token_result):
        self.require_enabled()
        credentials = self.token_credentials(account, token_result)
        email = normalize_email((account or {}).get("email") or credentials.get("email"))
        existing = self.find_match(account)
        base_name = str((existing.name if existing else "") or (account or {}).get("remote_name") or email).strip()
        base_name = base_name or f"openai-{int(time.time())}"
        extra = {
            "import_source": "relogin",
            "imported_at": utc_now(),
        }
        if existing and isinstance(existing.raw, dict) and isinstance(existing.raw.get("extra"), dict):
            extra = {**existing.raw["extra"], **extra}

        if existing:
            payload = {
                "name": base_name,
                "type": "oauth",
                "credentials": credentials,
                "extra": extra,
                "status": "active",
                "concurrency": int((existing.raw or {}).get("concurrency") or self.config["sub2api_concurrency"]),
                "priority": int((existing.raw or {}).get("priority") or self.config["sub2api_priority"]),
                "confirm_mixed_channel_risk": True,
            }
            data = self.request_json("PUT", f"/api/v1/admin/accounts/{existing.remote_id}", body=payload)
            remote = self.remote_from_response(data, fallback_name=base_name, fallback_email=email)
            return {"provider": self.provider, "uploaded": True, "action": "updated", **remote.store_fields(), "raw": data}

        payload = {
            "name": base_name,
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
            "extra": extra,
            "concurrency": self.config["sub2api_concurrency"],
            "priority": self.config["sub2api_priority"],
            "confirm_mixed_channel_risk": True,
        }
        data = self.request_json("POST", "/api/v1/admin/accounts", body=payload)
        remote = self.remote_from_response(data, fallback_name=base_name, fallback_email=email)
        return {"provider": self.provider, "uploaded": True, "action": "created", **remote.store_fields(), "raw": data}

    def remote_from_response(self, data, fallback_name="", fallback_email=""):
        payload = response_data(data)
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            payload = {}
        return RemoteAccount(
            provider=self.provider,
            email=account_email_from_payload(payload) or normalize_email(fallback_email),
            remote_id=str(payload.get("id") or "").strip(),
            name=str(payload.get("name") or fallback_name or "").strip(),
            raw=payload,
        )


def create_provider_client(cfg, require_config=False):
    config = target_config(cfg)
    provider = config["provider"]
    if provider == "cpa":
        client = CpaProvider(cfg, config)
    elif provider == "sub2api":
        client = Sub2ApiProvider(cfg, config)
    else:
        client = NullProvider(cfg, config)
    if require_config:
        client.require_enabled()
    return client


def mask_secret(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * max(4, len(value) - 6)}{value[-3:]}"


def looks_like_email(value):
    return bool(re.search(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(value or "").strip()))
