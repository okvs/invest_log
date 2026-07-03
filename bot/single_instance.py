"""봇 단일 인스턴스 강제 — flock 기반.

중복 인스턴스가 뜨면 텔레그램 getUpdates 가 409 Conflict 를 반복해 거래가
기록되지 않는 사고가 반복됐다(메모리/lesson 다수). pidfile+pid 검사 방식과
달리 flock 은 프로세스가 어떻게 죽든(킬/정전) OS 가 잠금을 자동 해제하므로
stale lock 청소가 필요 없다.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


def acquire(path: str | Path) -> IO[str] | None:
    """배타 잠금 획득. 성공 시 열린 파일 핸들(프로세스 종료까지 유지할 것),
    이미 다른 인스턴스가 잡고 있으면 None."""
    fp = Path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    f = open(fp, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f
