#!/usr/bin/env bash
# Run the service locally over HTTPS on https://localhost:8088.
#
#   scripts/run-dev.sh              API, webhooks and console
#   scripts/run-dev.sh --all        also the outbox worker and the evidence jobs
#   PORT=8090 scripts/run-dev.sh    another port
#
# Reads .env (never printed). Creates a dev certificate in var/certs the first
# time (mkcert if installed, otherwise self-signed), and creates the schema
# when the database is empty. Ctrl-C stops everything it started.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8088}"
CERT=var/certs/localhost.pem
KEY=var/certs/localhost-key.pem

if [[ ! -f .env ]]; then
  echo "No .env: copy .env.example to .env and fill it in." >&2
  exit 1
fi
set -a; source .env; set +a

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  exit 1
fi

if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  mkdir -p var/certs
  if command -v mkcert >/dev/null; then
    mkcert -cert-file "$CERT" -key-file "$KEY" localhost 127.0.0.1
  else
    echo "mkcert not found: creating a self-signed certificate (the browser will warn)."
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj /CN=localhost \
      -addext subjectAltName=DNS:localhost,IP:127.0.0.1 \
      -keyout "$KEY" -out "$CERT" 2>/dev/null
  fi
fi

# The migrations are not re-runnable, so apply them only to an empty database.
python - <<'PY'
from sqlalchemy import inspect
from app.db import apply_migrations, engine
if not inspect(engine()).has_table("consent"):
    apply_migrations()
    print("Schema created.")
PY

trap 'kill $(jobs -p) 2>/dev/null' EXIT
if [[ "${1:-}" == "--all" ]]; then
  python -m app.worker &
  python -m app.jobs &
fi

echo "Serving https://localhost:$PORT  (console: /console, OAuth: /sprinklr/oauth/login)"
uvicorn app.main:app --port "$PORT" --ssl-certfile "$CERT" --ssl-keyfile "$KEY"
