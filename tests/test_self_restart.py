"""bot.self_restart — 소스 변경 감지 자기 재실행."""
from __future__ import annotations

import os
import sys

from bot import self_restart


def _tmp_tree(tmp_path):
    (tmp_path / "bot").mkdir()
    f = tmp_path / "bot" / "a.py"
    f.write_text("x = 1\n")
    return f


def test_no_change_no_exec(tmp_path):
    _tmp_tree(tmp_path)
    self_restart.arm(root=tmp_path)
    calls = []
    out = self_restart.reexec_if_source_changed(
        lambda m: None, root=tmp_path, _exec=lambda *a: calls.append(a))
    assert out is False and calls == []


def test_source_changed_flag(tmp_path):
    """봇/웹의 자기 종료 판단용 source_changed() — arm 전 False, 수정 후 True."""
    f = _tmp_tree(tmp_path)
    self_restart._baseline = None
    assert self_restart.source_changed(root=tmp_path) is False  # arm 전
    self_restart.arm(root=tmp_path)
    assert self_restart.source_changed(root=tmp_path) is False
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    assert self_restart.source_changed(root=tmp_path) is True


def test_modified_file_triggers_exec(tmp_path):
    f = _tmp_tree(tmp_path)
    self_restart.arm(root=tmp_path)
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))  # 코드 수정 시뮬레이션
    calls = []
    out = self_restart.reexec_if_source_changed(
        lambda m: None, root=tmp_path, _exec=lambda exe, argv: calls.append((exe, argv)))
    assert out is True
    assert calls and calls[0][0] == sys.executable
    assert calls[0][1][0] == sys.executable  # execv argv = [python, script, ...]


def test_new_file_triggers_exec(tmp_path):
    f = _tmp_tree(tmp_path)
    self_restart.arm(root=tmp_path)
    g = tmp_path / "bot" / "b.py"
    g.write_text("y = 2\n")
    os.utime(g, (g.stat().st_atime, f.stat().st_mtime + 10))
    calls = []
    assert self_restart.reexec_if_source_changed(
        lambda m: None, root=tmp_path, _exec=lambda *a: calls.append(a)) is True
    assert len(calls) == 1


def test_pycache_ignored(tmp_path):
    f = _tmp_tree(tmp_path)
    self_restart.arm(root=tmp_path)
    pc = tmp_path / "bot" / "__pycache__"
    pc.mkdir()
    c = pc / "a.cpython-312.pyc.py"
    c.write_text("cache")
    os.utime(c, (c.stat().st_atime, f.stat().st_mtime + 100))
    assert self_restart.reexec_if_source_changed(
        lambda m: None, root=tmp_path, _exec=lambda *a: (_ for _ in ()).throw(AssertionError)) is False
