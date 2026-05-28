import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from config_loader import load_config


CACHE_FILE = Path(__file__).resolve().parent / "sentinel_cache.json"
CACHE_TTL_SECONDS = 600
DEFAULT_SENTINEL_TOKEN_TIMEOUT_SECONDS = 90
_CACHE_LOCK = threading.Lock()


def get_cached(force_fresh=False):
    if force_fresh:
        return None
    with _CACHE_LOCK:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
        if time.time() - float(data.get("ts") or 0) > CACHE_TTL_SECONDS:
            return None
        return data if data.get("sentinel_token") else None


def save_cache(data):
    payload = dict(data)
    payload["ts"] = time.time()
    tmp = CACHE_FILE.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    with _CACHE_LOCK:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)


def clear_cache():
    with _CACHE_LOCK:
        try:
            CACHE_FILE.unlink()
        except FileNotFoundError:
            pass


def _token_timeout_seconds(cfg):
    raw = cfg.get("timeouts", {}).get("sentinel_token") or DEFAULT_SENTINEL_TOKEN_TIMEOUT_SECONDS
    try:
        return max(10, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_SENTINEL_TOKEN_TIMEOUT_SECONDS


def _evaluate_with_timeout(page, source, *, arg=None, timeout_ms=90000, label="page.evaluate"):
    wrapper = """({ source, arg, timeoutMs, label }) => {
        const fn = eval(`(${source})`);
        let timer;
        const timeout = new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
        });
        return Promise.race([
            Promise.resolve().then(() => fn(arg)),
            timeout
        ]).finally(() => clearTimeout(timer));
    }"""
    return page.evaluate(
        wrapper,
        {
            "source": source,
            "arg": arg,
            "timeoutMs": int(timeout_ms),
            "label": label,
        },
    )


def extract_sentinel(force_fresh=True, cache_enabled=False):
    """Extract OpenAI Sentinel cookies/tokens with local Playwright Chromium only."""
    if cache_enabled and not force_fresh:
        cached = get_cached()
        if cached:
            return cached

    cfg = load_config()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("需要先安装 Playwright：pip install -r requirements.txt && playwright install chromium") from exc

    auth_base = cfg["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    chat_client_id = cfg["chatgpt"].get("chat_web_client_id") or "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"
    timeout_seconds = _token_timeout_seconds(cfg)
    timeout_ms = timeout_seconds * 1000

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=cfg["http"]["user_agent_chrome"],
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()

        device_id = str(uuid.uuid4())
        state = secrets.token_urlsafe(32)
        scope = "openid email profile offline_access model.request model.read organization.read organization.write"
        auth_url = (
            f"{auth_base}/api/accounts/authorize"
            f"?client_id={chat_client_id}"
            f"&scope={quote(scope)}"
            f"&response_type=code"
            f"&redirect_uri={quote('https://chatgpt.com/api/auth/callback/openai')}"
            f"&audience={quote('https://api.openai.com/v1')}"
            f"&device_id={device_id}"
            f"&prompt=login"
            f"&screen_hint=signup"
            f"&state={state}"
        )

        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=120000)
        except Exception:
            page.goto(auth_url, wait_until="commit", timeout=120000)

        for i in range(30):
            time.sleep(2)
            if page.evaluate("() => typeof window.SentinelSDK !== 'undefined'"):
                print(f"  SentinelSDK loaded after {i * 2}s")
                break
        else:
            browser.close()
            raise RuntimeError("SentinelSDK not loaded after 60s")

        print(f"  Sentinel token start (timeout {timeout_seconds}s)")
        try:
            _evaluate_with_timeout(
                page,
                "() => SentinelSDK.init()",
                timeout_ms=timeout_ms,
                label="SentinelSDK.init",
            )
            time.sleep(2)
            did = _evaluate_with_timeout(
                page,
                "() => document.cookie.match(/oai-did=([^;]+)/)?.[1] || ''",
                timeout_ms=timeout_ms,
                label="Sentinel device id",
            )
            sentinel_token = _evaluate_with_timeout(
                page,
                """(did) => SentinelSDK.token().then(raw => {
                    const parsed = JSON.parse(raw);
                    parsed.id = did;
                    parsed.flow = 'username_password_create';
                    return JSON.stringify(parsed);
                })""",
                arg=did,
                timeout_ms=timeout_ms,
                label="SentinelSDK.token username_password_create",
            )
            sentinel_so = _evaluate_with_timeout(
                page,
                """(did) => SentinelSDK.token().then(raw => {
                    const parsed = JSON.parse(raw);
                    return JSON.stringify({ so: raw, c: parsed.c, id: did, flow: 'oauth_create_account' });
                })""",
                arg=did,
                timeout_ms=timeout_ms,
                label="SentinelSDK.token oauth_create_account",
            )
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies())
        finally:
            browser.close()

    result = {
        "sentinel_token": sentinel_token,
        "sentinel_so_token": sentinel_so,
        "cookie_str": cookie_str,
        "oai_did": did,
    }
    if cache_enabled:
        save_cache(result)
    return result
