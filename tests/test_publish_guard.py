"""firebase_publish._deploy_guarded — 발행 역행 방지(M6)."""
from __future__ import annotations

import pytest

import bot.firebase_publish as fp
from storage import json_store


@pytest.fixture()
def fake_deploy(monkeypatch):
    calls = []
    monkeypatch.setattr(fp, "deploy", lambda files: calls.append(files) or "https://x/")
    return calls


def test_fresh_build_deploys_and_records(fake_deploy):
    url = fp._deploy_guarded({"/index.html": b"x"}, stamp=100.0)
    assert url == "https://x/" and len(fake_deploy) == 1
    assert json_store.load(fp._STAMP_FILE)["data_stamp"] == 100.0


def test_stale_build_is_skipped(fake_deploy):
    json_store.save(fp._STAMP_FILE, {"data_stamp": 200.0})
    url = fp._deploy_guarded({"/index.html": b"x"}, stamp=100.0)  # 더 오래된 데이터
    assert url is None and fake_deploy == []
    assert json_store.load(fp._STAMP_FILE)["data_stamp"] == 200.0  # 기록 보존


def test_equal_stamp_allows_price_refresh(fake_deploy):
    """데이터 무변경 재발행(시세만 갱신)은 스탬프가 같아도 허용."""
    json_store.save(fp._STAMP_FILE, {"data_stamp": 100.0})
    url = fp._deploy_guarded({"/index.html": b"x"}, stamp=100.0)
    assert url == "https://x/" and len(fake_deploy) == 1
