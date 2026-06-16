#!/usr/bin/env bash
# PWA 백엔드(FastAPI)를 tmux 세션(invest-web)으로 시작/재시작한다.
#   - 맥에서 실행, http://localhost:8787 (LAN/터널용으로 0.0.0.0 바인드)
#   - /api/* 는 Firebase ID 토큰 필요. 허용 계정 = ALLOWED_EMAILS.
#   - 폰에서 접속하려면 Cloudflare 터널로 이 포트를 노출(scripts/tunnel_start.sh, 예정).
set -euo pipefail

SESSION=invest-web
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"
PORT="${INVEST_WEB_PORT:-8787}"
ALLOW="${ALLOWED_EMAILS:-tmdals5992@gmail.com}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
  echo "기존 '$SESSION' 세션 종료"
fi

tmux new-session -d -s "$SESSION" \
  "cd '$DIR' && export ALLOWED_EMAILS='$ALLOW' && while true; do '$PY' -m uvicorn server.app:app --host 0.0.0.0 --port $PORT; echo '[restart] web exited, 5s 후 재시작'; sleep 5; done"

echo "tmux 세션 '$SESSION' 시작됨"
echo "  맥 브라우저:  http://localhost:$PORT"
echo "  허용 계정:    $ALLOW"
echo "  로그:         tmux attach -t $SESSION   (나오기: Ctrl-b d)"
echo "  끄기:         tmux kill-session -t $SESSION"
