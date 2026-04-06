"""테스트용 공통 fixture — 임시 data 디렉토리 사용."""
from __future__ import annotations

import pytest

import storage.json_store as store


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    """모든 테스트가 임시 디렉토리를 data 저장소로 사용."""
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    return tmp_path
