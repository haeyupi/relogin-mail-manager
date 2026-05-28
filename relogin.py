import argparse
import base64
import contextlib
import contextvars
import hashlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from curl_cffi import requests as curl_requests

from config_loader import load_config, mail_db_path
from mail_reader import LocalMailReader
from mail_store import MailStore, normalize_email
from providers import create_provider_client
from sentinel import extract_sentinel


ROOT = Path(__file__).resolve().parent
CFG = load_config()
UA = CFG["http"]["user_agent_chrome"]
AUTH_BASE = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com").rstrip("/")
_LOG_CALLBACK = contextvars.ContextVar("relogin_log_callback", default=None)


def configure(config=None):
    global CFG, UA, AUTH_BASE
    CFG = config or load_config()
    UA = CFG["http"]["user_agent_chrome"]
    AUTH_BASE = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com").rstrip("/")
    return CFG


def emit_log(message):
    callback = _LOG_CALLBACK.get()
    if not callback:
        return
    try:
        callback(str(message))
    except Exception:
        pass


@contextlib.contextmanager
def log_to(callback):
    if not callback:
        yield
        return
    token = _LOG_CALLBACK.set(callback)
    try:
        yield
    finally:
        _LOG_CALLBACK.reset(token)


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.@+-]+", "_", str(value or "unknown")).strip("_") or "unknown"


def save_json(data, subdir, name):
    out_root = Path(CFG.get("output", {}).get("directory") or ".")
    if not out_root.is_absolute():
        out_root = ROOT / out_root
    path = out_root / subdir
    path.mkdir(parents=True, exist_ok=True)
    fname = path / f"{safe_name(name)}_{int(time.time() * 1000)}.json"
    fname.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved: {fname}")
    return fname


def save_exact_json(data, subdir, filename):
    out_root = Path(CFG.get("output", {}).get("directory") or ".")
    if not out_root.is_absolute():
        out_root = ROOT / out_root
    path = out_root / subdir
    path.mkdir(parents=True, exist_ok=True)
    fname = path / filename
    fname.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved: {fname}")
    return fname


def open_mail_store():
    return MailStore(mail_db_path(CFG))


def validate_local_relogin_account(account):
    if not isinstance(account, dict):
        raise RuntimeError("邮箱账号资料为空，请先导入原项目导出的 ZIP。")
    email = normalize_email(account.get("email"))
    if not email:
        raise RuntimeError("邮箱账号缺少 email。")
    if not account.get("client_id") or not account.get("mail_refresh_token"):
        raise RuntimeError(f"{email} 缺少邮箱 client_id 或 refresh_token，无法本地读取验证码。")
    return {
        "email": email,
        "phone": str(account.get("phone_number") or "").strip(),
        "password": str(account.get("gpt_password") or account.get("password") or "").strip(),
        "status": str(account.get("status") or "").strip(),
    }


def token_file_suffix(phone, email):
    phone = str(phone or "").strip().replace("+", "")
    if phone:
        return phone
    return safe_name(email)


def tick(timings, name):
    timings.append([name, time.time()])
    print(f"  [{name}]", end=" ", flush=True)
    emit_log(f"开始 {name}")


def tock(timings):
    name = timings[-1][0]
    timings[-1][1] = time.time() - timings[-1][1]
    emit_log(f"完成 {name} ({timings[-1][1]:.1f}s)")


def print_timings(timings):
    total = sum(item[1] for item in timings)
    print(f"\n  {'=' * 50}")
    print(f"  {'Step':<40} {'Time (s)':>8}")
    print(f"  {'-' * 50}")
    for name, elapsed in timings:
        print(f"  {name:<40} {elapsed:>8.2f}")
    print(f"  {'-' * 50}")
    print(f"  {'TOTAL':<40} {total:>8.2f}")
    print(f"  {'=' * 50}")


def api_headers(sentinel_token):
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": CFG["http"].get("accept_language", "en-US,en;q=0.9"),
        "Content-Type": "application/json",
        "Origin": AUTH_BASE,
        "openai-sentinel-token": sentinel_token,
    }


def json_or_empty(response):
    try:
        return response.json()
    except Exception:
        return {}


def response_snippet(response, limit=240):
    status = getattr(response, "status_code", "?")
    url = str(getattr(response, "url", "") or "").strip()
    body = re.sub(r"\s+", " ", str(getattr(response, "text", "") or "").strip())[:limit]
    parts = [f"HTTP {status}"]
    if url:
        parts.append(f"url={url[:160]}")
    if body:
        parts.append(f"body={body}")
    return " ".join(parts)


def json_or_raise(response, stage):
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{stage} returned non-JSON response: {response_snippet(response)}") from exc
    if response.status_code >= 400:
        detail = data.get("detail") or data.get("message") or data.get("error") or data
        error = RuntimeError(f"{stage} failed: HTTP {response.status_code}: {detail}")
        error.status_code = response.status_code
        error.data = data
        raise error
    return data


def normalize_redirect_url(raw_url, *, label, referer=AUTH_BASE):
    url = str(raw_url or "").strip()
    if not url:
        raise RuntimeError(f"{label} missing redirect url")
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"{label} invalid scheme: {url[:200]}")
        return url
    if url.startswith("/"):
        return urljoin(referer, url)
    raise RuntimeError(f"{label} invalid redirect url: {url[:200]}")


def next_location_or_raise(response, label):
    raw_loc = response.headers.get("Location", "")
    try:
        return normalize_redirect_url(raw_loc, label=label, referer=str(response.url))
    except RuntimeError as exc:
        raise RuntimeError(f"{exc}; {response_snippet(response)}") from exc


def follow_consent_redirects(session, start_url, browser_h):
    current_url = normalize_redirect_url(start_url, label="consent continue_url")
    final_loc = ""
    for hop in range(1, 4):
        response = session.get(
            current_url,
            headers=browser_h,
            impersonate="chrome",
            allow_redirects=False,
            timeout=30,
        )
        final_loc = next_location_or_raise(response, f"consent redirect hop {hop}")
        if hop < 3:
            current_url = final_loc
    return final_loc


def auth_session_email(session, api_h):
    response = session.get(
        f"{AUTH_BASE}/api/accounts/client_auth_session_dump",
        headers=api_h,
        impersonate="chrome",
        timeout=30,
    )
    return json_or_empty(response).get("client_auth_session", {}).get("email", "")


def wrong_email_otp_response(response):
    data = json_or_empty(response)
    text = str(data).lower()
    return response.status_code == 401 and (
        data.get("code") == "wrong_email_otp_code" or "wrong code" in text
    )


def send_add_email(session, api_h, email):
    response = session.post(
        f"{AUTH_BASE}/api/accounts/add-email/send",
        json={"email": email},
        headers=api_h,
        impersonate="chrome",
        timeout=30,
    )
    data = json_or_raise(response, "add-email/send")
    return data.get("continue_url", ""), data.get("page", {}).get("type", "")


def invalid_username_or_password(exc):
    data = getattr(exc, "data", {}) or {}
    text = str(data or exc).lower()
    return (
        getattr(exc, "status_code", None) == 401
        and (
            data.get("code") == "invalid_username_or_password"
            or "invalid_username_or_password" in text
            or "login failed" in text
        )
    )


def send_login_email_otp(session, api_h, email):
    attempts = [
        ("/api/accounts/passwordless/send-otp", None),
        ("/api/accounts/email-otp/resend", {}),
    ]
    errors = []
    for path, payload in attempts:
        kwargs = {
            "headers": api_h,
            "impersonate": "chrome",
            "timeout": 30,
        }
        if payload is not None:
            kwargs["json"] = payload
        response = session.post(f"{AUTH_BASE}{path}", **kwargs)
        data = json_or_empty(response)
        if response.status_code < 400:
            print(f"→ sent via {path}")
            emit_log(f"已请求邮箱验证码: {path}")
            return data.get("continue_url", ""), data.get("page", {}).get("type", "")
        errors.append(f"{path}: HTTP {response.status_code} {data or response.text[:120]}")
        if response.status_code not in {400, 404, 405}:
            break
    raise RuntimeError("Email one-time code send failed: " + " | ".join(errors))


def resend_current_email_otp(session, api_h):
    path = "/api/accounts/email-otp/resend"
    response = session.post(
        f"{AUTH_BASE}{path}",
        json={},
        headers=api_h,
        impersonate="chrome",
        timeout=30,
    )
    data = json_or_empty(response)
    if response.status_code >= 400:
        raise RuntimeError(f"Email OTP resend failed: HTTP {response.status_code} {data or response.text[:120]}")
    print(f"→ resent via {path}")
    emit_log("已请求邮箱验证码 resend")
    return data.get("continue_url", ""), data.get("page", {}).get("type", "")


def reset_email_code_context(context):
    context["after_timestamp"] = int(time.time())
    context["exclude_codes"] = []
    return context


def prefer_one_time_code_login():
    return bool((CFG.get("chatgpt") or {}).get("prefer_one_time_code", True))


def complete_about_you(session, api_h, sentinel_data):
    first = ["James", "John", "Robert", "Michael", "David", "William", "Mary", "Linda"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
    import random

    headers = {**api_h, "Referer": f"{AUTH_BASE}/about-you"}
    if sentinel_data.get("sentinel_so_token"):
        headers["openai-sentinel-so-token"] = sentinel_data["sentinel_so_token"]
    response = session.post(
        f"{AUTH_BASE}/api/accounts/create_account",
        json={
            "name": f"{random.choice(first)} {random.choice(last)}",
            "birthdate": f"{random.randint(1985, 2004)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
        },
        headers=headers,
        impersonate="chrome",
        timeout=30,
    )
    data = json_or_raise(response, "about_you")
    return data.get("continue_url", ""), data.get("page", {}).get("type", "")


def poll_account_email_code(mail, account, context):
    timeout = CFG.get("timeouts", {}).get("email_poll", 120)
    interval = CFG.get("timeouts", {}).get("poll_interval", 3)
    emit_log(f"等待邮箱验证码，最长 {timeout}s")
    code = mail.poll_code(
        account,
        context,
        timeout=timeout,
        interval=interval,
        limit=20,
    )
    if code:
        emit_log("已读取邮箱验证码")
        return code
    raise RuntimeError("Email OTP timeout")


def decode_account_id(id_token):
    try:
        parts = str(id_token or "").split(".")
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        auth = decoded.get("https://api.openai.com/auth", {})
        return auth.get("chatgpt_account_id") or auth.get("user_id")
    except Exception:
        return None


def build_oauth_start(provider=None):
    provider = provider or create_provider_client(CFG)
    redirect_uri = "http://localhost:1455/auth/callback"
    if provider.provider == "cpa":
        cpa_oauth = provider.request_oauth_url()
        return {
            "provider": "cpa",
            "remote_oauth": True,
            "oauth_url": cpa_oauth["oauth_url"],
            "cpa_state": cpa_oauth.get("state") or "",
            "code_verifier": "",
            "redirect_uri": redirect_uri,
        }

    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    scope = "openid profile email offline_access api.connectors.read api.connectors.invoke"
    client_id = CFG["chatgpt"]["codex_client_id"]
    oauth_url = (
        f"{AUTH_BASE}/oauth/authorize"
        f"?client_id={client_id}&scope={quote(scope)}&response_type=code"
        f"&redirect_uri={quote(redirect_uri)}&prompt=login&state={state}"
        f"&code_challenge={code_challenge}&code_challenge_method=S256"
        f"&codex_cli_simplified_flow=true&id_token_add_organizations=true"
        f"&originator=codex_cli_rs"
    )
    return {
        "provider": provider.provider,
        "remote_oauth": False,
        "oauth_url": oauth_url,
        "cpa_state": "",
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }


def login_with_email_account(account, login_with_phone=False, store=None, provider=None):
    mail = LocalMailReader(timeout=CFG.get("timeouts", {}).get("mail_reader_request", 30))
    provider = provider or create_provider_client(CFG)
    details = validate_local_relogin_account(account)
    email = details["email"]
    phone = details["phone"]
    password = details["password"]
    timings = []

    print(f"  Email: {email}")
    print(f"  Phone: {phone or '-'}")
    login_identifier = phone if login_with_phone else email
    if login_with_phone and not phone:
        raise RuntimeError(f"{email} 没有手机号，不能使用 --phone-login。")
    login_kind = "phone_number" if login_with_phone else "email"
    login_label = "phone" if login_with_phone else "email"
    print(f"  Login: {login_label}")
    print("  Network: local traffic, no proxy")

    tick(timings, "1-Sentinel")
    sentinel_data = extract_sentinel(force_fresh=True, cache_enabled=False)
    tock(timings)

    auth_prefixes = (
        "oai-login-csrf",
        "oai-did",
        "oai-client-auth",
        "auth-session",
        "auth_provider",
        "login_session",
        "unified_session",
        "rg_context",
        "iss_context",
    )
    session = curl_requests.Session()
    code_context = {"after_timestamp": int(time.time()), "exclude_codes": []}
    for pair in sentinel_data["cookie_str"].split("; "):
        if "=" in pair:
            key, value = pair.split("=", 1)
            if not any(key.startswith(prefix) for prefix in auth_prefixes):
                session.cookies.set(key, value, domain=".openai.com")

    api_h = api_headers(sentinel_data["sentinel_token"])
    browser_h = {**api_h, "Accept": "text/html,application/xhtml+xml"}
    oauth = build_oauth_start(provider)

    tick(timings, "2-OAuth")
    session.get(oauth["oauth_url"], headers=browser_h, impersonate="chrome", allow_redirects=True, timeout=30)
    session.get(f"{AUTH_BASE}/log-in", headers=browser_h, impersonate="chrome", allow_redirects=True, timeout=30)
    tock(timings)

    tick(timings, "3-Identifier")
    response = session.post(
        f"{AUTH_BASE}/api/accounts/authorize/continue",
        json={"username": {"kind": login_kind, "value": login_identifier}},
        headers=api_h,
        impersonate="chrome",
        timeout=30,
    )
    data = json_or_raise(response, "authorize/continue")
    next_url = data.get("continue_url", "")
    page_type = data.get("page", {}).get("type", "")
    print(f"→ {page_type or '?'}")
    session.get(f"{AUTH_BASE}/api/accounts/client_auth_session_dump", headers=api_h, impersonate="chrome", timeout=30)
    tock(timings)

    used_one_time_code = page_type == "email_otp_verification"
    if used_one_time_code:
        tick(timings, "4-Password")
        print("→ skipped; email one-time code")
        tock(timings)
        tick(timings, "4b-Resend Login OTP")
        reset_email_code_context(code_context)
        resend_next_url, resend_page_type = resend_current_email_otp(session, api_h)
        next_url = resend_next_url or next_url
        page_type = resend_page_type or page_type
        tock(timings)
    else:
        tick(timings, "4-Password")
        if not password:
            print("→ no saved password; fallback to email one-time code")
            tock(timings)
            tick(timings, "4b-Send Login OTP")
            reset_email_code_context(code_context)
            next_url, page_type = send_login_email_otp(session, api_h, email)
            used_one_time_code = True
            tock(timings)
        elif not login_with_phone and page_type == "login_password" and prefer_one_time_code_login():
            print("→ prefer email one-time code")
            tock(timings)
            tick(timings, "4b-Send Login OTP")
            reset_email_code_context(code_context)
            send_error = None
            try:
                next_url, page_type = send_login_email_otp(session, api_h, email)
                used_one_time_code = True
            except RuntimeError as exc:
                send_error = exc
            tock(timings)
            if send_error:
                exc = send_error
                print(f"→ one-time code failed; fallback to password: {exc}")
                emit_log(f"一次性验证码登录发送失败，回退密码: {exc}")
                tick(timings, "4c-Password Fallback")
                response = session.post(
                    f"{AUTH_BASE}/api/accounts/password/verify",
                    json={"password": password},
                    headers=api_h,
                    impersonate="chrome",
                    timeout=30,
                )
                data = json_or_raise(response, "password/verify")
                next_url = data.get("continue_url", "")
                page_type = data.get("page", {}).get("type", "")
                print(f"→ {page_type or '?'}")
                used_one_time_code = page_type == "email_otp_verification"
                tock(timings)
                if used_one_time_code:
                    tick(timings, "4d-Resend Login OTP")
                    reset_email_code_context(code_context)
                    resend_next_url, resend_page_type = resend_current_email_otp(session, api_h)
                    next_url = resend_next_url or next_url
                    page_type = resend_page_type or page_type
                    tock(timings)
        else:
            response = session.post(
                f"{AUTH_BASE}/api/accounts/password/verify",
                json={"password": password},
                headers=api_h,
                impersonate="chrome",
                timeout=30,
            )
            try:
                data = json_or_raise(response, "password/verify")
                next_url = data.get("continue_url", "")
                page_type = data.get("page", {}).get("type", "")
                print(f"→ {page_type or '?'}")
            except RuntimeError as exc:
                if not invalid_username_or_password(exc):
                    raise
                print("→ invalid password; fallback to email one-time code")
                tock(timings)
                tick(timings, "4b-Send Login OTP")
                reset_email_code_context(code_context)
                next_url, page_type = send_login_email_otp(session, api_h, email)
                used_one_time_code = True
                tock(timings)
            else:
                used_one_time_code = page_type == "email_otp_verification"
                tock(timings)
                if used_one_time_code:
                    tick(timings, "4b-Resend Login OTP")
                    reset_email_code_context(code_context)
                    resend_next_url, resend_page_type = resend_current_email_otp(session, api_h)
                    next_url = resend_next_url or next_url
                    page_type = resend_page_type or page_type
                    tock(timings)

    tick(timings, "5-Email")
    about_you_after_email = False
    if used_one_time_code:
        poll_email = email
    elif page_type == "about_you":
        try:
            next_url, page_type = complete_about_you(session, api_h, sentinel_data)
            print(f"→ about_you → {page_type or 'next'}", end=" ")
        except RuntimeError as exc:
            if "missing_email" not in str(exc).lower() and "provide email" not in str(exc).lower():
                raise
            page_type = "add_email"
            about_you_after_email = True

    if used_one_time_code:
        pass
    elif page_type == "add_email":
        next_url, page_type = send_add_email(session, api_h, email)
        poll_email = email
    else:
        poll_email = auth_session_email(session, api_h)
        if not poll_email:
            time.sleep(1)
            poll_email = auth_session_email(session, api_h)
        if not poll_email:
            next_url, page_type = send_add_email(session, api_h, email)
            poll_email = email

    if poll_email.lower() != email.lower():
        raise RuntimeError(f"OpenAI 要求验证的邮箱是 {poll_email}，不是你指定的 {email}")
    print(f"→ {poll_email}")
    tock(timings)

    tick(timings, "6-Email OTP")
    ecode = poll_account_email_code(mail, account, code_context)
    print(f" {ecode}")
    tock(timings)

    for attempt in range(1, 3):
        response = session.post(
            f"{AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": ecode},
            headers=api_h,
            impersonate="chrome",
            timeout=30,
        )
        if response.status_code < 400:
            data = json_or_raise(response, "email-otp/validate")
            break
        if attempt < 2 and wrong_email_otp_response(response):
            print("  Email OTP wrong; repoll once")
            code_context.setdefault("exclude_codes", []).append(ecode)
            ecode = poll_account_email_code(mail, account, code_context)
            continue
        data = json_or_raise(response, "email-otp/validate")
        break

    next_url = data.get("continue_url", "") or next_url
    page_type = data.get("page", {}).get("type", page_type)
    if about_you_after_email or page_type == "about_you":
        tick(timings, "6b-About You")
        next_url, page_type = complete_about_you(session, api_h, sentinel_data)
        print(f"→ {page_type or 'next'}")
        tock(timings)

    tick(timings, "7-Consent")
    response = session.get(f"{AUTH_BASE}/api/accounts/client_auth_session_dump", headers=api_h, impersonate="chrome", timeout=30)
    workspaces = json_or_empty(response).get("client_auth_session", {}).get("workspaces", [])
    if workspaces:
        response = session.post(
            f"{AUTH_BASE}/api/accounts/workspace/select",
            json={"workspace_id": workspaces[0]["id"]},
            headers=api_h,
            impersonate="chrome",
            timeout=30,
        )
        data = json_or_raise(response, "workspace/select")
        next_url = data.get("continue_url", "") or next_url
    redirect_url = follow_consent_redirects(session, next_url, browser_h)
    tock(timings)

    upload_result = {"provider": provider.provider, "uploaded": False}
    if oauth["remote_oauth"] and provider.provider == "cpa":
        tick(timings, "8-CPA Upload")
        cpa_result = provider.submit_oauth_callback(redirect_url, oauth["cpa_state"])
        tock(timings)
        upload_result = cpa_result
        token_result = {
            "auth_mode": "cpa",
            "OPENAI_API_KEY": None,
            "tokens": {},
            "last_refresh": utc_now(),
            "_email": email,
            "cpa": cpa_result,
        }
        save_exact_json(token_result, "tokens", f"cpa-{token_file_suffix(phone, email)}.json")
    else:
        tick(timings, "8-Token")
        parsed = urlparse(redirect_url)
        auth_code = (parse_qs(parsed.query).get("code") or [""])[0]
        if not auth_code:
            raise RuntimeError(f"No code in redirect: {redirect_url[:150]}")
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": oauth["redirect_uri"],
                "client_id": CFG["chatgpt"]["codex_client_id"],
                "code_verifier": oauth["code_verifier"],
            }
        )
        response = session.post(
            f"{AUTH_BASE}/oauth/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            impersonate="chrome",
            timeout=30,
        )
        token_data = json_or_raise(response, "oauth/token")
        tock(timings)
        expires_at = ""
        try:
            expires_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() + int(token_data.get("expires_in") or 0)),
            )
        except Exception:
            expires_at = ""
        token_result = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": token_data.get("id_token", ""),
                "access_token": token_data.get("access_token", ""),
                "refresh_token": token_data.get("refresh_token", ""),
                "account_id": decode_account_id(token_data.get("id_token", "")),
                "expires_at": expires_at,
            },
            "last_refresh": utc_now(),
            "_email": email,
        }
        safe_suffix = token_file_suffix(phone, email)
        save_exact_json(token_result, "tokens", f"auth_{safe_suffix}.json")
        save_exact_json(token_result, "tokens", f"codex-{safe_suffix}.json")
        if provider.provider:
            tick(timings, "9-Provider Upload")
            upload_result = provider.upload_token_result(account, token_result)
            print(f"→ {upload_result.get('provider')} {upload_result.get('action') or 'uploaded'}")
            tock(timings)

    tick(timings, "10-Mark Local")
    if store is not None:
        store.mark_relogin_success(
            email,
            remote_provider=upload_result.get("remote_provider") or upload_result.get("provider") or "",
            remote_id=upload_result.get("remote_id") or "",
            remote_name=upload_result.get("remote_name") or "",
        )
        print("→ normal")
    else:
        print("→ skipped")
    tock(timings)

    result = {
        "success": True,
        "relogin": True,
        "login_method": "email_otp" if used_one_time_code else "password",
        "email": email,
        "phone": phone,
        "password": password,
        "token": {k: v for k, v in token_result.items() if k != "_email"},
        "local_mail": {
            "email": email,
            "phone": phone,
            "status": "normal",
        },
        "provider": upload_result,
        "timestamp": utc_now(),
    }
    save_json(result, "success", email)
    print_timings(timings)
    return result


def relogin_by_email(email, login_with_phone=False, store=None, provider=None, mark_failure=True):
    close_store = False
    if store is None:
        store = open_mail_store()
        close_store = True
    try:
        account = store.get(email)
        details = validate_local_relogin_account(account)
        print(f"  Found local mailbox: status={details['status'] or '?'}")
        emit_log(f"读取本地邮箱: status={details['status'] or '?'}")
        return login_with_email_account(account, login_with_phone=login_with_phone, store=store, provider=provider)
    except Exception as exc:
        if mark_failure:
            try:
                store.set_status(email, "unavailable", str(exc))
            except Exception:
                pass
        raise
    finally:
        if close_store:
            store.close()


def main():
    parser = argparse.ArgumentParser(description="Use a locally imported Outlook/Hotmail mailbox to relogin and upload/save RT.")
    parser.add_argument("--email", required=True, help="需要重新登录的 Outlook/Hotmail 邮箱")
    parser.add_argument("--phone-login", action="store_true", help="使用手机号作为 OpenAI 登录标识；默认使用邮箱登录")
    args = parser.parse_args()

    try:
        relogin_by_email(args.email, login_with_phone=args.phone_login)
    except Exception as exc:
        fail = {
            "success": False,
            "relogin": True,
            "email": args.email,
            "error": str(exc),
            "timestamp": utc_now(),
        }
        save_json(fail, "fail", args.email)
        print(f"  Failed: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
