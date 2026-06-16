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
echo "발급된 주소(API): $URL"
echo "Firebase 허용 도메인 등록 중..."
"$DIR/.venv/bin/python" "$DIR/scripts/firebase_authz_domain.py" "$HOST" || \
  echo "(허용 도메인 자동 등록 실패 — 수동: scripts/firebase_authz_domain.py $HOST)"

# web.app 발행본(app.html)에 새 터널 API 주소를 반영하도록 재발행
echo "PWA(app.html) 재발행 중..."
FIREBASE_PUBLISH=1 "$DIR/.venv/bin/python" "$DIR/scripts/dashboard_refresh.py" --once --force >/dev/null 2>&1 \
  && echo "재발행 완료" || echo "(재발행 실패 — dash-refresh 다음 주기에 반영됨)"

APP_URL="$(FIREBASE_PUBLISH=1 "$DIR/.venv/bin/python" -c "from bot import firebase_publish as fp; print(fp.dashboard_url().rstrip('/')+'/app.html')" 2>/dev/null || true)"
echo ""
echo "📱 폰에서 열 주소(PWA, 로그인+입력):"
echo "    ${APP_URL:-"(빌드 후 web.app/<token>/app.html)"}"
echo "  (API 백엔드는 $URL — 직접 열 필요 없음)"
echo "  로그: tail -f '$LOG'   끄기: tmux kill-session -t $SESSION"
