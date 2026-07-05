#!/usr/bin/env bash
# 맥 재부팅 후 invest_log 전 서비스 자동 기동 — launchd(LaunchAgent)가 로그인 시 실행.
#
# run_cmd 와 달리 **멱등**: 이미 떠 있는 tmux 세션은 절대 건드리지 않고,
# 없는 세션만 기동한다. (그래서 launchd 등록 시점에 즉시 1회 실행돼도 무해하고,
# 재부팅 후엔 전부 없으므로 전부 기동된다. 터널 URL 도 살아있으면 안 바뀜.)
#
# ⚠️ 재부팅 후 카톡 자동반영(kakao-apply)은 카톡 DB 읽기에 FDA(전체 디스크 접근)가
#    필요하다. python(.venv) 바이너리에 FDA 가 부여돼 있으면 정상, 아니면
#    kakao_apply.log 에 오류가 남으니 시스템 설정 → 개인정보 보호 → 전체 디스크
#    접근에서 python 을 허용할 것 (lesson.md 2026-06-15 참조).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$DIR/logs/boot_start.log"
mkdir -p "$DIR/logs"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z')  $*" >> "$LOG"; }

has() { tmux has-session -t "$1" 2>/dev/null; }

log "=== boot_start 실행 ==="

# 카톡 동기화 주체인 카카오톡 앱도 재부팅 후 꺼져 있으면 체결 알림이 안 들어온다.
if ! pgrep -xq KakaoTalk; then
  open -gja KakaoTalk 2>/dev/null && log "KakaoTalk 실행" || log "KakaoTalk 실행 실패(수동 확인)"
fi

if has invest-bot; then log "invest-bot: 이미 실행 중 — skip"; else
  tmux new-session -d -s invest-bot \
    "cd '$DIR' && while true; do uv run python main.py; echo '[restart] bot exited, 5s 후 재시작'; sleep 5; done"
  log "invest-bot: 기동"
fi

if has kakao-apply; then log "kakao-apply: 이미 실행 중 — skip"; else
  bash "$DIR/scripts/kakao_apply_start.sh" >> "$LOG" 2>&1 && log "kakao-apply: 기동"
fi

if has dash-refresh; then log "dash-refresh: 이미 실행 중 — skip"; else
  bash "$DIR/scripts/dashboard_refresh_start.sh" >> "$LOG" 2>&1 && log "dash-refresh: 기동"
fi

if has invest-web; then log "invest-web: 이미 실행 중 — skip"; else
  bash "$DIR/scripts/webapp_start.sh" >> "$LOG" 2>&1 && log "invest-web: 기동"
fi

if has invest-tunnel; then log "invest-tunnel: 이미 실행 중 — skip"; else
  # 터널은 URL 재발급 + Firebase 도메인 등록 + app.html 재발행까지 스스로 처리
  bash "$DIR/scripts/tunnel_start.sh" >> "$LOG" 2>&1 && log "invest-tunnel: 기동(새 URL 자동 등록/재발행)"
fi

log "=== boot_start 완료: $(tmux ls 2>/dev/null | grep -cE 'invest|kakao|dash')개 세션 가동 ==="
