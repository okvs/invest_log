"""데몬 소스 변경 감지 → 자기 재실행(os.execv).

파이썬은 import 한 모듈을 프로세스에 캐시하므로, 장수 데몬(dash-refresh,
kakao-apply)은 코드를 고쳐도 구버전으로 계속 렌더/발행해 새 발행본을
덮어쓴다 — "코드 고쳤는데 라이브 안 바뀜" 사고가 lesson 에 3회 이상 기록됨.
지금까지는 '수정 후 데몬 전부 재시작'이라는 수동 절차로 방어했는데,
여기서 그 절차를 코드로 내린다: 데몬 루프의 안전지점(폴링 사이)마다
감시 대상 소스의 최대 mtime 을 확인하고, 프로세스 시작 시점보다 새 파일이
있으면 os.execv 로 같은 인자 그대로 자기를 재실행한다(PID 유지, tmux
래퍼 영향 없음). 재실행 후 baseline 이 새 mtime 으로 잡혀 무한 재실행은
없다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCH_DIRS = ("bot", "scripts", "parsers", "models", "storage", "server")

_baseline: float | None = None


def source_mtime(root: Path | None = None) -> float:
    """감시 대상(.py) 중 가장 최근 수정 시각."""
    root = root or PROJECT_ROOT
    latest = 0.0
    for d in WATCH_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for f in filenames:
                if f.endswith(".py"):
                    try:
                        latest = max(latest, os.stat(os.path.join(dirpath, f)).st_mtime)
                    except OSError:
                        pass
    for f in root.glob("*.py"):  # main.py 등 루트 스크립트
        try:
            latest = max(latest, f.stat().st_mtime)
        except OSError:
            pass
    return latest


def arm(root: Path | None = None) -> None:
    """현재 소스 상태를 baseline 으로 기록 — 데몬 시작 직후 1회 호출."""
    global _baseline
    _baseline = source_mtime(root)


def reexec_if_source_changed(log=print, *, root: Path | None = None, _exec=None) -> bool:
    """루프 안전지점에서 호출. 소스가 baseline 이후 바뀌었으면 자기 재실행.

    반환값은 테스트용(_exec 주입 시): 재실행이 트리거됐으면 True.
    실전에서는 os.execv 가 돌아오지 않는다.
    """
    global _baseline
    if _baseline is None:
        arm(root)
        return False
    cur = source_mtime(root)
    if cur <= _baseline + 1e-6:
        return False
    log("소스 변경 감지 — 새 코드로 자기 재실행(os.execv)")
    sys.stdout.flush()
    sys.stderr.flush()
    (_exec or os.execv)(sys.executable, [sys.executable] + sys.argv)
    return True  # _exec 주입(테스트) 시에만 도달
