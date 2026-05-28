"""CPA management API helpers.

Matches the extension CPA import flow:
1. GET /v0/management/codex-auth-url
2. POST /v0/management/oauth-callback with the localhost redirect URL
3. Poll GET /v0/management/get-auth-status?state=...
"""
import json
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from curl_cffi import requests as curl_requests


CPA_API_REQUEST_TIMEOUT = 45
CPA_AUTH_STATUS_TIMEOUT = 120
CPA_AUTH_STATUS_INTERVAL = 1


def cpa_config(cfg):
    data = cfg.get("cpa") or {}
    base_url = str(data.get("base_url") or data.get("url") or data.get("vps_url") or data.get("vpsUrl") or "").strip()
    management_key = str(
        data.get("management_key")
        or data.get("key")
        or data.get("api_key")
        or data.get("vps_password")
        or data.get("vpsPassword")
        or data.get("password")
        or ""
    ).strip()
    enabled = bool(data.get("enabled")) and bool(base_url) and bool(management_key)
    return {
        **data,
        "enabled": enabled,
        "base_url": base_url,
        "management_key": management_key,
        "poll_timeout": int(data.get("poll_timeout") or CPA_AUTH_STATUS_TIMEOUT),
        "poll_interval": max(0.25, float(data.get("poll_interval") or CPA_AUTH_STATUS_INTERVAL)),
        "request_timeout": int(data.get("request_timeout") or CPA_API_REQUEST_TIMEOUT),
    }


def cpa_enabled(cfg):
    return cpa_config(cfg)["enabled"]


def parse_url_safely(raw_url):
    try:
        return urlparse(str(raw_url or "").strip())
    except Exception:
        return None


def normalize_cpa_management_api_base_url(raw_url):
    raw = str(raw_url or "").strip()
    if not raw:
        raise RuntimeError("尚未配置 CPA 地址。")

    if raw.lower().startswith("http:") and not raw.lower().startswith("http://"):
        raw = "http://" + raw[5:].lstrip("/")
    if raw.lower().startswith("https:") and not raw.lower().startswith("https://"):
        raw = "https://" + raw[6:].lstrip("/")

    hostish = raw.split("/", 1)[0]
    localhost_like = (
        hostish.startswith("localhost")
        or hostish.startswith("127.")
        or hostish.startswith("[::1]")
    )
    if not raw.lower().startswith(("http://", "https://")):
        raw = ("http://" if localhost_like else "https://") + raw

    parsed = urlparse(raw)
    return urlunparse((parsed.scheme, parsed.netloc, "/v0/management", "", "", "")).rstrip("/")


def build_cpa_management_api_url(raw_url, endpoint, query=None):
    base_url = normalize_cpa_management_api_base_url(raw_url)
    path = str(endpoint or "").lstrip("/")
    url = f"{base_url}/{path}"
    query = {k: v for k, v in (query or {}).items() if v is not None and str(v) != ""}
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def extract_oauth_state_from_url(raw_url):
    parsed = parse_url_safely(raw_url)
    if not parsed:
        return ""
    return (parse_qs(parsed.query).get("state") or [""])[0].strip()


def is_localhost_oauth_callback_url(raw_url):
    parsed = parse_url_safely(raw_url)
    if not parsed:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return False
    if parsed.path not in {"/auth/callback", "/codex/callback"}:
        return False
    query = parse_qs(parsed.query)
    return bool((query.get("code") or [""])[0] and (query.get("state") or [""])[0])


def cpa_headers(config, include_json=False):
    key = config["management_key"]
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
    }
    if include_json:
        headers["Content-Type"] = "application/json"
    return headers


def parse_json_response_text(text):
    text = str(text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


def request_cpa_management_json(cfg, endpoint, method="GET", body=None, query=None, timeout=None):
    config = cpa_config(cfg)
    if not config["base_url"]:
        raise RuntimeError("尚未配置 CPA 地址。")
    if not config["management_key"]:
        raise RuntimeError("CPA API 需要管理密钥。")

    method = str(method or "GET").upper()
    url = build_cpa_management_api_url(config["base_url"], endpoint, query=query)
    response = curl_requests.request(
        method,
        url,
        headers=cpa_headers(config, include_json=body is not None),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        impersonate="chrome",
        timeout=int(timeout or config["request_timeout"] or CPA_API_REQUEST_TIMEOUT),
    )
    data = parse_json_response_text(response.text)
    if response.status_code >= 400:
        message = ""
        if isinstance(data, dict):
            message = str(data.get("error") or data.get("message") or data.get("raw") or "").strip()
        error = RuntimeError(f"CPA API {method} {endpoint} 返回 {response.status_code}：{message or response.reason}")
        error.status_code = response.status_code
        error.data = data
        raise error
    return data


def request_cpa_oauth_url(cfg):
    data = request_cpa_management_json(cfg, "/codex-auth-url", timeout=CPA_API_REQUEST_TIMEOUT)
    oauth_url = str(data.get("url") or data.get("oauthUrl") or data.get("auth_url") or "").strip()
    state = str(data.get("state") or extract_oauth_state_from_url(oauth_url)).strip()
    if not oauth_url.lower().startswith(("http://", "https://")):
        raise RuntimeError("CPA API 未返回可用的 Codex OAuth 链接。")
    return {"oauth_url": oauth_url, "state": state}


def wait_for_cpa_oauth_completion(cfg, oauth_state):
    config = cpa_config(cfg)
    if not oauth_state:
        return "CPA 已接收回调"

    deadline = time.time() + max(1, int(config["poll_timeout"]))
    last_status = ""
    while time.time() < deadline:
        data = request_cpa_management_json(
            cfg,
            "/get-auth-status",
            query={"state": oauth_state},
            timeout=min(15, int(config["request_timeout"] or CPA_API_REQUEST_TIMEOUT)),
        )
        status = str(data.get("status") or "").strip().lower()
        last_status = status or last_status
        if status == "ok":
            return "CPA 已导入 Codex OAuth 账号"
        if status == "error":
            raise RuntimeError(data.get("error") or "CPA OAuth 导入失败。")
        time.sleep(config["poll_interval"])

    raise RuntimeError(f"CPA OAuth 导入状态轮询超时，最后状态：{last_status or 'unknown'}。")


def submit_cpa_oauth_callback(cfg, redirect_url, oauth_state=None):
    if not is_localhost_oauth_callback_url(redirect_url):
        raise RuntimeError("CPA 回调地址无效，缺少 localhost code/state。")

    oauth_state = oauth_state or extract_oauth_state_from_url(redirect_url)
    try:
        request_cpa_management_json(
            cfg,
            "/oauth-callback",
            method="POST",
            body={
                "provider": "codex",
                "redirect_url": redirect_url,
            },
            timeout=CPA_API_REQUEST_TIMEOUT,
        )
    except RuntimeError as exc:
        text = str(getattr(exc, "data", {}) or exc).lower()
        if not (getattr(exc, "status_code", None) == 409 and oauth_state and "pending" in text):
            raise

    return {
        "uploaded": True,
        "state": oauth_state,
        "redirect_url": redirect_url,
        "verified_status": wait_for_cpa_oauth_completion(cfg, oauth_state),
    }
