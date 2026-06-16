#!/usr/bin/env bash
# 카톡 체결 알림 → 잔고 자동반영 데몬을 tmux 세션(kakao-apply)으로 시작/재시작한다.
#
# 텔레그램 포워더(kakao-fwd)와 별개 watermark(data/kakao_apply_state.json)로 돌며,
# 새 체결 알림을 읽어 포트폴리오/예수금/선물 포지션에 그대로 반영하고 반영 결과를
# 텔레그램으로 1건씩 확인 메시지로 보낸다(원문 포워딩은 kakao-fwd가 계속 담당).
#
# 반드시 '전체 디스크 접근' 권한이 있는 터미널(iTerm 등) 안에서 실행해야 카톡 DB를
# 읽을 수 있다. tmux 세션은 터미널을 닫아도 유지되고 재부팅 때만 꺼진다.
#
# ⚠️ 이 데몬을 켜는 동안에는 같은 거래를 봇에 수동으로도 기록하지 말 것(이중계상).
set -euo pipefail

SESSION=kakao-apply
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"   # telegram/filelock/firebase 등 deps는 .venv에 있음

if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
  echo "기존 '$SESSION' 세션 종료"
fi

# 첫 기동은 --from-today 로 '오늘 0시(KST) 이후' 미반영분을 따라잡고, 이후 60초 폴링.
# 바깥 while 은 프로세스가 통째로 죽는 드문 경우의 자동 재시작용.
tmux new-session -d -s "$SESSION" \
  "cd '$DIR' && while true; do '$PY' scripts/kakao_apply.py --from-today --loop 60; echo '[restart] apply exited, 5s 후 재시작'; sleep 5; done"

echo "tmux 세션 '$SESSION' 시작됨 (오늘분 따라잡기 + 60초 폴링)"
echo "  로그: tail -f '$DIR/logs/kakao_apply.log'"
echo "  보기: tmux attach -t $SESSION   (빠져나오기: Ctrl-b d)"
echo "  끄기: tmux kill-session -t $SESSION"
