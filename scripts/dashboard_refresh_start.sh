#!/usr/bin/env bash
# 장중(KRX 09:00~15:30) 20분마다 대시보드를 재발행해 현재가를 갱신한다.
# tmux 세션(dash-refresh)으로 돌리며, 터미널 창을 닫아도 유지된다(재부팅 때만 꺼짐).
# 카톡 DB를 읽지 않으므로 디스크 접근 권한과 무관(yfinance/KIS/firebase만 사용).
set -euo pipefail

SESSION=dash-refresh
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"   # 발행 deps(yfinance/matplotlib/google-auth)는 .venv에 있음

if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
  echo "기존 '$SESSION' 세션 종료"
fi

# --loop 1200 = 20분. 데몬이 매 주기 장중 여부를 판단해 장중에만 발행한다.
# 바깥 while 은 프로세스가 통째로 죽는 드문 경우의 자동 재시작용.
tmux new-session -d -s "$SESSION" \
  "cd '$DIR' && while true; do '$PY' scripts/dashboard_refresh.py --loop 1200; echo '[restart] refresh exited, 5s 후 재시작'; sleep 5; done"

echo "tmux 세션 '$SESSION' 시작됨 (장중 20분마다 재발행)"
echo "  로그: tail -f '$DIR/logs/dashboard_refresh.log'"
echo "  끄기: tmux kill-session -t $SESSION"
