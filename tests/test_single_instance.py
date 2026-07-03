"""bot.single_instance — flock 단일 인스턴스 잠금."""
from __future__ import annotations

import os
import subprocess
import sys

from bot.single_instance import acquire

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_acquire_writes_pid(tmp_path):
    lock = acquire(tmp_path / "t.lock")
    assert lock is not None
    assert (tmp_path / "t.lock").read_text() == str(os.getpid())
    lock.close()


def test_second_process_cannot_acquire(tmp_path):
    """다른 프로세스가 잡고 있으면 acquire 가 None — 중복 인스턴스 차단."""
    lock = acquire(tmp_path / "t.lock")
    assert lock is not None
    code = subprocess.run([
        sys.executable, "-c",
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from bot.single_instance import acquire\n"
        "sys.exit(0 if acquire(sys.argv[2]) is None else 1)",
        PROJECT_ROOT, str(tmp_path / "t.lock"),
    ]).returncode
    assert code == 0  # 서브프로세스는 획득 실패해야 정상
    lock.close()


def test_released_after_holder_exits(tmp_path):
    """선점 프로세스가 죽으면 OS 가 자동 해제 — 다음 인스턴스가 획득."""
    subprocess.run([
        sys.executable, "-c",
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from bot.single_instance import acquire\n"
        "assert acquire(sys.argv[2]) is not None",
        PROJECT_ROOT, str(tmp_path / "t.lock"),
    ], check=True)
    lock = acquire(tmp_path / "t.lock")  # 홀더 종료 후 → 획득 성공
    assert lock is not None
    lock.close()
