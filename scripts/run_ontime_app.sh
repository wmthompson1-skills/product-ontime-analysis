#!/usr/bin/env bash
set -euo pipefail

# Starts the Product On-Time Analysis stack locally:
#   - main.py (Flask)                 -> port from FLASK_PORT in .env
#   - hf-space-inventory-sqlgen/app.py -> port 5000 (fixed; main.py's GRADIO_BACKEND
#     proxy expects it here, and it must NOT be left to inherit the generic PORT=
#     value from the shared .env, which is used for something else)
#
# Usage:
#   ./scripts/run_ontime_app.sh start
#   ./scripts/run_ontime_app.sh stop
#   ./scripts/run_ontime_app.sh status

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV=".venv"
GRADIO_APP_DIR="hf-space-inventory-sqlgen"
GRADIO_PORT=5000
GRADIO_PID_FILE="hf_app.pid"
GRADIO_LOG_FILE="hf_app.out"
FLASK_PID_FILE="app.pid"
FLASK_LOG_FILE="app.out"

FLASK_APP_DEPS=(flask flask-sqlalchemy sqlalchemy python-dotenv requests pandas numpy openpyxl xlrd)
GRADIO_APP_DEPS=(fastapi "uvicorn[standard]" gradio pydantic python-multipart sqlalchemy sqlglot)

port_in_use() {
  # Prints the PID bound to $1, if any.
  ss -ltnp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {print $0}' | grep -oP 'pid=\K[0-9]+' | head -1 || true
}

read_flask_port() {
  local p
  p=$(grep -E '^FLASK_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 || true)
  echo "${p:-3000}"
}

ensure_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    echo "No venv at $VENV — creating it."
    if ! python3 -m venv "$VENV" 2>/tmp/venv_err; then
      cat /tmp/venv_err >&2
      echo "Could not create a venv. On Debian/Ubuntu you likely need:" >&2
      echo "  sudo apt install python3-venv python3-pip" >&2
      exit 1
    fi
  fi
  echo "Ensuring dependencies are installed in $VENV (idempotent, quick if already satisfied)..."
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q "${FLASK_APP_DEPS[@]}" "${GRADIO_APP_DEPS[@]}"
}

start() {
  ensure_venv
  local flask_port
  flask_port="$(read_flask_port)"

  local existing
  existing="$(port_in_use "$GRADIO_PORT")"
  if [ -n "$existing" ]; then
    echo "Port $GRADIO_PORT is already in use by PID $existing — not starting the gradio app." >&2
    echo "  Inspect it:  ps -p $existing -o pid,cmd" >&2
    echo "  Stop it:     kill $existing" >&2
    exit 1
  fi
  existing="$(port_in_use "$flask_port")"
  if [ -n "$existing" ]; then
    echo "Port $flask_port (FLASK_PORT) is already in use by PID $existing — not starting main.py." >&2
    echo "  Inspect it:  ps -p $existing -o pid,cmd" >&2
    echo "  Stop it:     kill $existing" >&2
    exit 1
  fi

  echo "Starting gradio/fastapi backend on port $GRADIO_PORT ($GRADIO_APP_DIR/app.py)..."
  # `exec` after `cd` replaces the subshell process image with python itself,
  # so `$!` below is the actual server PID (not a wrapper that `stop` can't reach).
  ( cd "$GRADIO_APP_DIR" && exec env PORT="$GRADIO_PORT" "$REPO_ROOT/$VENV/bin/python" app.py ) \
      > "$GRADIO_LOG_FILE" 2>&1 &
  echo $! > "$GRADIO_PID_FILE"

  echo -n "Waiting for it to come up"
  for _ in $(seq 1 30); do
    if curl -sSf "http://127.0.0.1:$GRADIO_PORT/gradio/" >/dev/null 2>&1; then
      echo " — up."
      break
    fi
    echo -n "."
    sleep 1
  done

  echo "Starting main.py (Flask) on port $flask_port..."
  "$VENV/bin/python" main.py > "$FLASK_LOG_FILE" 2>&1 &
  echo $! > "$FLASK_PID_FILE"

  echo -n "Waiting for it to come up"
  for _ in $(seq 1 30); do
    if curl -sSf "http://127.0.0.1:$flask_port/health" >/dev/null 2>&1; then
      echo " — up."
      break
    fi
    echo -n "."
    sleep 1
  done

  echo
  echo "App:    http://127.0.0.1:$flask_port/ontime-analysis  (logs: $FLASK_LOG_FILE, pid file: $FLASK_PID_FILE)"
  echo "Gradio: http://127.0.0.1:$GRADIO_PORT/gradio/          (logs: $GRADIO_LOG_FILE, pid file: $GRADIO_PID_FILE)"
  echo "Stop both with: $0 stop"
}

stop() {
  for pf in "$FLASK_PID_FILE" "$GRADIO_PID_FILE"; do
    if [ -f "$pf" ]; then
      local pid
      pid="$(cat "$pf")"
      if kill "$pid" 2>/dev/null; then
        echo "Stopped $pf (pid $pid)."
      else
        echo "$pf (pid $pid) was not running."
      fi
      rm -f "$pf"
    fi
  done
}

status() {
  local flask_port
  flask_port="$(read_flask_port)"
  for label_port in "main.py:$flask_port" "gradio app:$GRADIO_PORT"; do
    local label="${label_port%%:*}" port="${label_port##*:}"
    local pid
    pid="$(port_in_use "$port")"
    if [ -n "$pid" ]; then
      echo "$label — port $port in use by pid $pid ($(ps -p "$pid" -o cmd= 2>/dev/null))"
    else
      echo "$label — port $port free (not running)"
    fi
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 1 ;;
esac
