import json
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "mail_db": {
        "path": "data/mail_accounts.sqlite3",
    },
    "target": {
        "provider": "",
        "base_url": "",
        "management_key": "",
        "request_timeout": 45,
        "poll_timeout": 120,
        "poll_interval": 1,
        "sub2api_concurrency": 3,
        "sub2api_priority": 50,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8787,
        "password": "",
    },
    "chatgpt": {
        "auth_base_url": "https://auth.openai.com",
        "codex_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "prefer_one_time_code": True,
    },
    "cpa": {
        "enabled": False,
        "base_url": "",
        "management_key": "",
        "poll_timeout": 120,
        "poll_interval": 1,
        "request_timeout": 45,
    },
    "timeouts": {
        "email_poll": 120,
        "poll_interval": 3,
        "sentinel_token": 90,
        "mail_reader_request": 30,
    },
    "output": {
        "directory": ".",
    },
    "http": {
        "user_agent_chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
        "accept_language": "en-US,en;q=0.9",
    },
}


def deep_merge(base, override):
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path=None):
    path = Path(path or ROOT / "config.json")
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
    else:
        user_config = {}
    return deep_merge(DEFAULT_CONFIG, user_config)


def save_config_patch(patch, path=None):
    path = Path(path or ROOT / "config.json")
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    else:
        existing = {}
    updated = deep_merge(existing, patch or {})
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_config(path)


def resolve_project_path(value):
    path = Path(value or ".")
    if not path.is_absolute():
        path = ROOT / path
    return path


def mail_db_path(cfg):
    data = cfg.get("mail_db") or {}
    return resolve_project_path(data.get("path") or DEFAULT_CONFIG["mail_db"]["path"])
