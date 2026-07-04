"""json_store.transaction — RMW 임계구역(재진입 + 프로세스 간 배타)."""
from __future__ import annotations

import subprocess
import sys

import storage.json_store as store


def test_reentrant_load_save_inside_transaction():
    """트랜잭션 안에서 load/save(같은 락 재획득)와 중첩 트랜잭션이 데드락 없이 동작."""
    with store.transaction(store.PORTFOLIO_FILE, store.ACCOUNT_FILE):
        store.save_holdings([{"name": "X", "quantity": 1}])
        assert store.load_holdings() == [{"name": "X", "quantity": 1}]
        with store.transaction(store.PORTFOLIO_FILE):
            store.save_account({"cash": 1})
        assert store.load_account() == {"cash": 1}


def test_transaction_excludes_other_process():
    """트랜잭션 보유 중엔 다른 프로세스가 같은 장부 락을 못 잡는다(lost update 차단)."""
    with store.transaction(store.PORTFOLIO_FILE):
        lock_path = store._lock_path(store.PORTFOLIO_FILE)
        code = subprocess.run([
            sys.executable, "-c",
            "import sys\n"
            "from filelock import FileLock, Timeout\n"
            "try:\n"
            "    FileLock(sys.argv[1], timeout=0.5).acquire()\n"
            "except Timeout:\n"
            "    sys.exit(0)\n"
            "sys.exit(1)",
            lock_path,
        ]).returncode
        assert code == 0  # 자식은 Timeout 이어야 정상(배타 확인)

    # 트랜잭션 종료 후엔 즉시 획득 가능
    code = subprocess.run([
        sys.executable, "-c",
        "import sys\n"
        "from filelock import FileLock\n"
        "FileLock(sys.argv[1], timeout=2).acquire()\n"
        "sys.exit(0)",
        store._lock_path(store.PORTFOLIO_FILE),
    ]).returncode
    assert code == 0
