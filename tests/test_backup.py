"""bot.backup — data/ 일일 스냅샷."""
from __future__ import annotations

import tarfile
from datetime import datetime

from bot.backup import KEEP_ARCHIVES, make_backup
from storage.json_store import save_account, save_holdings


def _seed(tmp_data_dir):
    save_holdings([{"name": "X", "quantity": 1}])
    save_account({"cash": 1})
    (tmp_data_dir / "junk.lock").write_text("x")
    (tmp_data_dir / "cache.mst").write_text("x")
    (tmp_data_dir / "portfolio.json.bak-old").write_text("x")
    (tmp_data_dir / "balance_shots").mkdir()
    (tmp_data_dir / "balance_shots" / "a.jpg").write_bytes(b"img")


def test_backup_contents_and_exclusions(tmp_data_dir, tmp_path):
    _seed(tmp_data_dir)
    root = tmp_path / "bk"
    out = make_backup(root=root, now=datetime(2026, 7, 4))
    assert out is not None and out.name == "data-20260704.tar.gz"
    names = set(tarfile.open(out).getnames())
    assert {"portfolio.json", "account.json"} <= names
    assert not any(n.endswith((".lock", ".mst")) or ".bak" in n or n.endswith(".jpg")
                   for n in names)


def test_backup_once_per_day(tmp_data_dir, tmp_path):
    _seed(tmp_data_dir)
    root = tmp_path / "bk"
    assert make_backup(root=root, now=datetime(2026, 7, 4)) is not None
    assert make_backup(root=root, now=datetime(2026, 7, 4)) is None  # 같은 날 no-op
    assert make_backup(root=root, now=datetime(2026, 7, 5)) is not None


def test_backup_prunes_old_archives(tmp_data_dir, tmp_path):
    _seed(tmp_data_dir)
    root = tmp_path / "bk"
    root.mkdir()
    for i in range(KEEP_ARCHIVES + 5):
        (root / f"data-2026{i:04d}.tar.gz").write_bytes(b"old")
    make_backup(root=root, now=datetime(2026, 7, 4))
    assert len(list(root.glob("data-*.tar.gz"))) == KEEP_ARCHIVES
