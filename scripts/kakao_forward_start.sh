#!/usr/bin/env bash
# 카톡 → 텔레그램 포워더를 tmux 세션(kakao-fwd)으로 시작/재시작한다.
#
# 반드시 '전체 디스크 접근' 권한이 있는 터미널(iTerm 등) 안에서 실행해야
# 맥 카톡 DB를 읽을 수 있다. tmux 세션은 터미널 창을 닫아도 백그라운드에
# 남고, 재부팅 때만 꺼진다 → 재부팅 후 이 스크립트를 한 번 더 실행하면 됨.
set -euo pipefail

SESSION=kakao-fwd
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"   # sqlcipher3(카톡 DB 직접 읽기)·requests 등 deps는 .venv에 있음

# 이미 떠 있으면 교체
if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
  echo "기존 '$SESSION' 세션 종료"
fi

# --loop 는 내부적으로 60초마다 폴링하며 예외를 흡수한다.
# 바깥 while 은 프로세스가 통째로 죽는 드문 경우의 자동 재시작용.
tmux new-session -d -s "$SESSION" \
  "cd '$DIR' && while true; do '$PY' scripts/kakao_to_telegram.py --loop 60; echo '[restart] forwarder exited, 5s 후 재시작'; sleep 5; done"

echo "tmux 세션 '$SESSION' 시작됨 (60초 간격 폴링)"
echo "  로그:   tail -f '$DIR/logs/kakao_forward.log'"
echo "  보기:   tmux attach -t $SESSION   (빠져나오기: Ctrl-b d)"
echo "  끄기:   tmux kill-session -t $SESSION"
