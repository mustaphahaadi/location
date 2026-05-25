#!/usr/bin/env bash
# Location consent server + HTTPS tunnel (Cloudflare by default — no ngrok warning page)
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

export USE_TUNNEL="${USE_TUNNEL:-${USE_NGROK:-1}}"
export TUNNEL="${TUNNEL:-cloudflare}"
export PORT="${PORT:-8000}"

if [ "${USE_TUNNEL}" = "1" ] && [ "${TUNNEL}" = "cloudflare" ]; then
  if ! command -v cloudflared >/dev/null 2>&1 && [ ! -x "./bin/cloudflared" ]; then
    echo "Downloading cloudflared (free HTTPS tunnel, no warning page)…"
    mkdir -p bin
    ARCH="$(uname -m)"
    case "${ARCH}" in
      x86_64)  CF_ARCH="amd64" ;;
      aarch64|arm64) CF_ARCH="arm64" ;;
      *) echo "Unsupported arch: ${ARCH}. Install cloudflared manually."; exit 1 ;;
    esac
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -o bin/cloudflared.tmp
    mv bin/cloudflared.tmp bin/cloudflared
    chmod +x bin/cloudflared
  fi
  if command -v cloudflared >/dev/null 2>&1; then
    export CLOUDFLARED_BIN="$(command -v cloudflared)"
  else
    export CLOUDFLARED_BIN="$(pwd)/bin/cloudflared"
  fi
fi

if [ "${USE_TUNNEL}" = "1" ] && [ "${TUNNEL}" = "ngrok" ]; then
  if [ -z "${NGROK_AUTHTOKEN:-}" ] || [ "${NGROK_AUTHTOKEN}" = "your_token_here" ]; then
    echo ""
    echo "ERROR: TUNNEL=ngrok requires NGROK_AUTHTOKEN in .env"
    echo "  Tip: use TUNNEL=cloudflare instead — free, no warning page, no token needed."
    echo ""
    exit 1
  fi
  if command -v ngrok >/dev/null 2>&1; then
    ngrok config add-authtoken "${NGROK_AUTHTOKEN}" >/dev/null 2>&1 || true
  fi
fi

echo ""
echo "Starting server on http://127.0.0.1:${PORT}  (tunnel: ${TUNNEL})"
echo "Operator dashboard: http://127.0.0.1:${PORT}/dashboard"
echo ""

exec .venv/bin/python location_server.py
