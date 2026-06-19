"""봇 → Claude Code 세션(터미널) 명령 주입.

invest_log 봇(main.py)이 잔고 스샷을 받으면, invest_log **cmux 워크스페이스**의
Claude 세션에 `/balance-apply <req_id>` 를 주입해 잔고반영 스킬을 트리거한다.
remote_by_tlgm 의 리모트컨트롤 패턴(cmux send → 타깃 워크스페이스 Claude)과 동일.

cmux 소켓 인증은 ~/.config/cmux/cmux.json 의 저장된 비밀번호로 처리되므로,
봇이 cmux 자식 프로세스가 아니어도(예: invest-bot tmux 세션) CLI 가 동작한다.
워크스페이스 ref(workspace:N) 는 재배치될 수 있어 **이름으로 매번 재해석**한다.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)

_WS_LINE = re.compile(r"(workspace:\d+)\s+(\S+)")
_DEFAULT_CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"


def _cmux_bin() -> str | None:
    cand = (
        os.environ.get("CMUX_BUNDLED_CLI_PATH")
        or shutil.which("cmux")
        or _DEFAULT_CMUX
    )
    return cand if cand and os.path.exists(cand) else None


def _resolve_ref(cmux: str, workspace_name: str) -> str | None:
    """`cmux list-workspaces` 출력에서 이름 → ref(workspace:N) 해석."""
    try:
        r = subprocess.run(
            [cmux, "list-workspaces"],
            capture_output=True, text=True, timeout=6,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("cmux list-workspaces 실패: %s", e)
        return None
    if r.returncode != 0:
        logger.warning("cmux list-workspaces rc=%s: %s", r.returncode, r.stderr[:200])
        return None
    for line in r.stdout.splitlines():
        m = _WS_LINE.search(line)
        if m and m.group(2) == workspace_name:
            return m.group(1)
    return None


def inject_command(text: str, *, workspace: str = "invest_log") -> tuple[bool, str]:
    """타깃 cmux 워크스페이스의 Claude 세션에 한 줄 명령을 주입(+Enter).

    반환: (성공여부, 정보문자열[ref 또는 실패사유]).
    """
    cmux = _cmux_bin()
    if not cmux:
        return False, "cmux CLI 를 찾지 못함"

    ref = _resolve_ref(cmux, workspace)
    if not ref:
        return False, f"cmux 워크스페이스 '{workspace}' 미발견"

    try:
        # 1) 텍스트 입력(슬래시 자동완성 메뉴가 인자 공백으로 닫히도록 한 줄로 전송)
        send = subprocess.run(
            [cmux, "send", "--workspace", ref, "--", text],
            capture_output=True, text=True, timeout=6,
        )
        if send.returncode != 0:
            return False, f"cmux send 실패: {send.stderr[:160]}"
        # 2) 입력이 자리잡도록 잠깐 대기 후 Enter 로 제출
        time.sleep(0.4)
        enter = subprocess.run(
            [cmux, "send-key", "--workspace", ref, "enter"],
            capture_output=True, text=True, timeout=6,
        )
        if enter.returncode != 0:
            return False, f"cmux send-key enter 실패: {enter.stderr[:160]}"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"cmux 호출 예외: {e}"

    return True, ref
