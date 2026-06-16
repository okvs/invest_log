#!/usr/bin/env bash
# Cloudflare quick tunnel 로 맥 PWA 백엔드(localhost:PORT)를 인터넷에 노출한다.
#   - cloudflared(brew) 필요. Cloudflare 로그인/도메인 불필요(quick tunnel).
#   - 발급된 *.trycloudflare.com 주소를 Firebase Auth 허용 도메인에 자동 추가.
#   - tmux 세션(invest-tunnel)으로 상시 가동. ⚠️ quick tunnel 은 재시작 시 URL 변동.
set -euo pipefail

SESSION=invest-tunnel
DIR="$(cd "$(dirname "$0")/.." && pwd)"
CF="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"
PORT="${INVEST_WEB_PORT:-8787}"
LOG="$DIR/logs/tunnel.log"
mkdir -p "$DIR/logs"

if [ ! -x "$CF" ] && ! command -v cloudflared >/dev/null 2>&1; then
  echo "error: cloudflared 가 없습니다. 'brew install cloudflared' 후 다시 실행."
  exit 1
fi

tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"
: > "$LOG"
tmux new-session -d -s "$SESSION" \
  "$CF tunnel --url http://localhost:$PORT --no-autoupdate >> '$LOG' 2>&1"

echo "터널 시작 — URL 발급 대기..."
URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "URL 캡처 실패 — 로그 확인:"; tail -8 "$LOG"; exit 1
fi

HOST="${URL#https://}"
echo "$URL" > "$DIR/data/tunnel_url.txt"
echo "발급된 주소: $URL"
echo "Firebase 허용 도메인 등록 중..."
"$DIR/.venv/bin/python" "$DIR/scripts/firebase_authz_domain.py" "$HOST" || \
  echo "(허용 도메인 자동 등록 실패 — 수동: scripts/firebase_authz_domain.py $HOST)"

echo ""
echo "📱 폰/외부 접속:  $URL"
echo "  로그: tail -f '$LOG'   끄기: tmux kill-session -t $SESSION"
