#!/usr/bin/env bash
# 本机资料研究台；服务仅监听回环地址。
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
PYTHON_BIN="${WORKBENCH_PYTHON:-${SEARCHOS_PYTHON:-$REPO/.venv/bin/python}}"
export PYTHONPATH="$REPO:$REPO/web${PYTHONPATH:+:$PYTHONPATH}"
export NEXT_TELEMETRY_DISABLED=1
export CRAWL4_AI_BASE_DIRECTORY="$REPO/data/crawler-runtime"
# 浏览器只访问页面同源地址；Next 在服务端转发 HTTP 与 WebSocket 到后端。
export NEXT_PUBLIC_API_URL=""
export NEXT_PUBLIC_WS_URL=""
export WORKBENCH_API_ORIGIN="http://127.0.0.1:8000"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

start_api() {
  exec "$PYTHON_BIN" -m uvicorn api.main:app --app-dir "$REPO/web" --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 10
}

start_web() {
  cd "$REPO/web/frontend"
  if [ ! -d node_modules ]; then
    echo "界面依赖缺失，请先在 web/frontend 运行 npm ci。" >&2
    return 1
  fi
  if [ "${WORKBENCH_DEV:-0}" = "1" ]; then
    exec node node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 3000
  else
    if [ ! -f .next/BUILD_ID ]; then npm run build -- --webpack; fi
    exec node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3000
  fi
}

case "${1:-all}" in
  api) start_api ;;
  web|frontend) start_web ;;
  all)
    "$PYTHON_BIN" - <<'CHECK'
import socket
for port in (8000, 3000):
    with socket.socket() as sock:
        # Match the servers' socket behavior: a recently stopped connection can
        # remain in TIME_WAIT even after the listener has released the port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'本机端口 {port} 已在使用。若研究台已经打开，请直接访问 http://127.0.0.1:3000 。')
CHECK
    echo "资料研究台：http://127.0.0.1:3000"
    echo "关闭本窗口或按 Ctrl+C 即可停止。"
    start_api & API_PID=$!
    start_web & WEB_PID=$!
    cleanup() {
      trap - EXIT INT TERM
      kill "$API_PID" "$WEB_PID" 2>/dev/null || true
      wait "$API_PID" "$WEB_PID" 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    while kill -0 "$API_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; do sleep 1; done
    echo "一个服务已退出，请查看上方提示。" >&2
    exit 1
    ;;
  *) echo "用法：web/start.sh [all|api|web]"; exit 1 ;;
esac
