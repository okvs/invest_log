"""쓰기 레이어의 매수/매도 입력 시트(PWA 4단계 1차) — 주입 마크업 검증."""
from __future__ import annotations

import pytest

import bot.handlers.dashboard as dash
from storage.json_store import save_holdings


@pytest.fixture()
def tunnel(tmp_path, monkeypatch):
    fp = tmp_path / "tunnel_url.txt"
    fp.write_text("https://test.trycloudflare.com")
    monkeypatch.setattr(dash, "_TUNNEL_URL_FILE", fp)


def _inject() -> str:
    out = dash._inject_write_layer(b"<html><body>x</body></html>")
    assert out is not None
    return out.decode("utf-8")


def test_trade_sheet_injected_with_holdings(tunnel):
    save_holdings([
        {"name": "삼성전자", "quantity": 10, "avg_price": 70000},
        {"name": "마이크론2배", "quantity": 5, "avg_price": 26, "currency": "USD"},
        {"name": "전량매도됨", "quantity": 0, "avg_price": 100},
    ])
    html = _inject()
    assert "trade-fab" in html and "trade-modal" in html
    assert '"name": "삼성전자"' in html
    # USD·수량 0 종목은 매수/매도 API 대상이 아님 — 임베드 제외
    assert "마이크론2배" not in html.split("__HOLDINGS__=")[1].split("</script>")[0]
    assert "전량매도됨" not in html.split("__HOLDINGS__=")[1].split("</script>")[0]
    # 이중기록 경고 상시 표시
    assert "이중기록" in html


def test_no_tunnel_no_write_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "_TUNNEL_URL_FILE", tmp_path / "none.txt")
    assert dash._inject_write_layer(b"<html><body>x</body></html>") is None
