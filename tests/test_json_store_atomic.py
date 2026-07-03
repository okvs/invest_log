"""json_store 원자적 쓰기 — 쓰다 실패해도 원본 파일이 깨지지 않는다."""
from __future__ import annotations

import json

import pytest

import storage.json_store as store


def test_save_failure_keeps_original_intact(monkeypatch):
    """쓰기 도중 실패(디스크 풀/킬 시뮬레이션) → 원본 보존 + tmp 잔재 없음."""
    store.save("portfolio.json", {"holdings": ["원본"]})

    real_dump = json.dump

    def boom(obj, f, **kw):
        f.write('{"holdings": [')  # 일부만 쓰다가
        raise RuntimeError("disk full")

    monkeypatch.setattr(store.json, "dump", boom)
    with pytest.raises(RuntimeError):
        store.save("portfolio.json", {"holdings": ["새값"]})
    monkeypatch.setattr(store.json, "dump", real_dump)

    assert store.load("portfolio.json") == {"holdings": ["원본"]}
    assert not (store.DATA_DIR / "portfolio.json.tmp").exists()


def test_save_success_no_tmp_residue():
    store.save("account.json", {"cash": 1})
    assert store.load("account.json") == {"cash": 1}
    assert not (store.DATA_DIR / "account.json.tmp").exists()


def test_ticker_map_atomic(monkeypatch):
    store.save_ticker_map({"삼성전자": "005930"})

    real_dump = json.dump

    def boom(obj, f, **kw):
        raise RuntimeError("boom")

    # 주의: monkeypatch.undo() 는 conftest 의 tmp DATA_DIR 패치까지 되돌려
    # 실데이터를 건드리게 되므로, dump 만 원복한다.
    monkeypatch.setattr(store.json, "dump", boom)
    with pytest.raises(RuntimeError):
        store.save_ticker_map({"x": "y"})
    monkeypatch.setattr(store.json, "dump", real_dump)

    assert store.load_ticker_map() == {"삼성전자": "005930"}
