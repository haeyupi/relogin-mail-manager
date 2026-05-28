#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
IMAGE_NAME="${IMAGE_NAME:-relogin:local}"
CONTAINER_NAME="${CONTAINER_NAME:-relogin}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker first, then run this script again." >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  cp "$ROOT_DIR/.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example."
  echo "Edit WEB_PASSWORD and provider settings, then run this script again." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_PORT="${WEB_PORT:-8787}"
WEB_PASSWORD="${WEB_PASSWORD:-}"
BIND_IP="${BIND_IP:-127.0.0.1}"
HOST_PORT="${HOST_PORT:-8787}"

if [ "$WEB_HOST" != "127.0.0.1" ] && [ "$WEB_HOST" != "localhost" ] && [ -z "$WEB_PASSWORD" ]; then
  echo "WEB_PASSWORD is required when WEB_HOST is not localhost." >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/tokens" "$ROOT_DIR/success" "$ROOT_DIR/fail"
cd "$ROOT_DIR"

python3 - <<'PY'
import json
import os
from pathlib import Path

config = {
    "mail_db": {"path": "data/mail_accounts.sqlite3"},
    "target": {
        "provider": os.getenv("TARGET_PROVIDER", "").strip(),
        "base_url": os.getenv("TARGET_BASE_URL", "").strip(),
        "management_key": os.getenv("TARGET_MANAGEMENT_KEY", "").strip(),
        "request_timeout": int(os.getenv("TARGET_REQUEST_TIMEOUT", "45")),
        "poll_timeout": int(os.getenv("TARGET_POLL_TIMEOUT", "120")),
        "poll_interval": int(os.getenv("TARGET_POLL_INTERVAL", "1")),
        "sub2api_concurrency": int(os.getenv("SUB2API_CONCURRENCY", "3")),
        "sub2api_priority": int(os.getenv("SUB2API_PRIORITY", "50")),
    },
    "web": {
        "host": os.getenv("WEB_HOST", "0.0.0.0"),
        "port": int(os.getenv("WEB_PORT", "8787")),
        "password": os.getenv("WEB_PASSWORD", ""),
    },
    "chatgpt": {
        "auth_base_url": os.getenv("AUTH_BASE_URL", "https://auth.openai.com"),
        "codex_client_id": os.getenv("CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"),
        "prefer_one_time_code": os.getenv("PREFER_ONE_TIME_CODE", "true").lower() in {"1", "true", "yes", "on"},
    },
    "timeouts": {
        "email_poll": int(os.getenv("EMAIL_POLL", "120")),
        "poll_interval": int(os.getenv("POLL_INTERVAL", "3")),
        "sentinel_token": int(os.getenv("SENTINEL_TOKEN", "90")),
        "mail_reader_request": int(os.getenv("MAIL_READER_REQUEST", "30")),
    },
    "output": {"directory": "."},
    "http": {
        "user_agent_chrome": os.getenv(
            "USER_AGENT_CHROME",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
        ),
        "accept_language": os.getenv("ACCEPT_LANGUAGE", "en-US,en;q=0.9"),
    },
}
Path("config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
PY

docker build -t "$IMAGE_NAME" "$ROOT_DIR"

if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  docker rm -f "$CONTAINER_NAME" >/dev/null
fi

docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${BIND_IP}:${HOST_PORT}:${WEB_PORT}" \
  -v "$ROOT_DIR/config.json:/app/config.json" \
  -v "$ROOT_DIR/data:/app/data" \
  -v "$ROOT_DIR/tokens:/app/tokens" \
  -v "$ROOT_DIR/success:/app/success" \
  -v "$ROOT_DIR/fail:/app/fail" \
  "$IMAGE_NAME" \
  python app.py serve --host "$WEB_HOST" --port "$WEB_PORT"

echo "Container started: $CONTAINER_NAME"
echo "Open: http://${BIND_IP}:${HOST_PORT}/"
