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

# FIREBASE_PUBLISH=1: PWA(app.html/대시보드)에서 매수/매도/회고/섹터를 저장하면
# json_store.save → trigger_publish 로 Firebase Hosting 을 즉시 재발행한다. 이게
# 꺼져 있으면 디스크엔 저장돼도 라이브 HTML 이 그대로라, 새로고침 시 옛 화면이 보인다.
tmux new-session -d -s "$SESSION" \
  "cd '$DIR' && export ALLOWED_EMAILS='$ALLOW' FIREBASE_PUBLISH=1 && while true; do '$PY' -m uvicorn server.app:app --host 0.0.0.0 --port $PORT; echo '[restart] web exited, 5s 후 재시작'; sleep 5; done"

echo "tmux 세션 '$SESSION' 시작됨"
echo "  맥 브라우저:  http://localhost:$PORT"
echo "  허용 계정:    $ALLOW"
echo "  로그:         tmux attach -t $SESSION   (나오기: Ctrl-b d)"
echo "  끄기:         tmux kill-session -t $SESSION"
