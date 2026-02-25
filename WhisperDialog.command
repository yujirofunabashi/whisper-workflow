#!/bin/zsh
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
unset SYSTEM_VERSION_COMPAT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CACHE_DIR="$HOME/Library/Caches/WhisperGUI"
mkdir -p "$CACHE_DIR"

SERVER_SCRIPT="$SCRIPT_DIR/whisper_gui_web.py"
URL_FILE="$CACHE_DIR/whisper_gui_server.url"
PID_FILE="$CACHE_DIR/whisper_gui_server.pid"
SERVER_LOG="$CACHE_DIR/whisper_gui_server.log"

if [[ ! -f "$SERVER_SCRIPT" ]]; then
  echo "whisper_gui_web.py が見つかりません: $SERVER_SCRIPT" >&2
  exit 1
fi

pick_python() {
  if [[ -n "${WHISPER_PYTHON_BIN:-}" && -x "${WHISPER_PYTHON_BIN}" ]]; then
    echo "$WHISPER_PYTHON_BIN"
    return 0
  fi

  for c in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11; do
    [[ -x "$c" ]] || continue
    if "$c" - <<'PY' >/dev/null 2>&1
import http.server
import subprocess
PY
    then
      echo "$c"
      return 0
    fi
  done
  return 1
}

PY_BIN="$(pick_python || true)"
if [[ -z "$PY_BIN" ]]; then
  echo "python3 が見つかりません。" >&2
  exit 1
fi

open_url_with_fallback() {
  local url="$1"
  open "$url" >/dev/null 2>&1 && return 0
  open -a "Safari" "$url" >/dev/null 2>&1 && return 0
  open -a "Google Chrome" "$url" >/dev/null 2>&1 && return 0
  return 1
}

server_is_healthy() {
  local url=""
  [[ -s "$URL_FILE" ]] && url="$(cat "$URL_FILE" 2>/dev/null || true)"
  [[ -n "$url" ]] || return 1
  curl -fsS --max-time 1 "$url/api/state" >/dev/null 2>&1 || return 1
  echo "$url"
  return 0
}

restart_existing_server() {
  local old_pid=""
  [[ -s "$PID_FILE" ]] && old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]]; then
    kill "$old_pid" >/dev/null 2>&1 || true
    sleep 0.2
  fi
}

if URL="$(server_is_healthy)"; then
  if [[ "${WHISPER_GUI_FORCE_RESTART:-0}" == "1" || "$SERVER_SCRIPT" -nt "$PID_FILE" ]]; then
    restart_existing_server
  else
    open_url_with_fallback "$URL" || true
    exit 0
  fi
fi

# If URL health check failed but stale PID remains, stop it before relaunch.
restart_existing_server

# Cleanup stale files.
rm -f "$URL_FILE" "$PID_FILE"

# Launch server in background so app process can exit quickly.
nohup "$PY_BIN" "$SERVER_SCRIPT" \
  --host 127.0.0.1 \
  --port 8876 \
  --url-file "$URL_FILE" \
  --open-browser >> "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Wait briefly for URL file and health endpoint.
for _ in {1..40}; do
  if URL="$(server_is_healthy)"; then
    open_url_with_fallback "$URL" || true
    exit 0
  fi
  sleep 0.1
done

# Fallback: try opening expected URL even if health check is delayed.
open_url_with_fallback "http://127.0.0.1:8876/" || true
exit 0
