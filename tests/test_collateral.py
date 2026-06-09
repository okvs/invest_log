"""담보비율 시뮬레이션 — 순수 로직/HTML 생성 테스트."""
import json

from bot.collateral import (
    build_collateral_html,
    collateral_ratio,
    compute_collateral,
    liquidation_drop_pct,
)


def _holdings():
    return [
        {"name": "SK하이닉스", "ticker": "000660.KS", "quantity": 30, "credit_loan": 31871265, "total_invested": 0},
        {"name": "삼성전자", "ticker": "005930.KS", "quantity": 196, "credit_loan": 20317275, "total_invested": 0},
        {"name": "현금형ETF", "ticker": "X.KS", "quantity": 10, "credit_loan": 0, "total_invested": 1000000},  # 시세없음 → total_invested
    ]


def _quotes():
    return {"000660.KS": {"price": 2070000}, "005930.KS": {"price": 329000}}


def _n2t():
    return {"SK하이닉스": "000660.KS", "삼성전자": "005930.KS", "현금형ETF": "X.KS"}


def test_compute_eval_loan_and_sort():
    d = compute_collateral(_holdings(), _quotes(), _n2t(), cash=70112487)
    assert d["eval"] == 2070000*30 + 329000*196 + 1000000   # 시세없는 건 total_invested
    assert d["loan"] == 31871265 + 20317275
    assert d["cash"] == 70112487
    # 평가금 내림차순
    evs = [s["ev"] for s in d["stocks"]]
    assert evs == sorted(evs, reverse=True)
    assert d["stocks"][0]["n"] == "삼성전자"  # 329,000×196=64.48M > 하닉 62.1M


def test_ratio_matches_formula():
    d = {"eval": 180000000, "cash": 70000000, "loan": 50000000, "stocks": []}
    assert collateral_ratio(d) == (180000000 + 70000000) / 50000000 * 100  # 500%


def test_ratio_infinite_when_no_loan():
    d = {"eval": 100, "cash": 0, "loan": 0, "stocks": []}
    assert collateral_ratio(d) == float("inf")


def test_liquidation_drop_pct():
    # (eval*(1-x)+cash)/loan = 1.4 풀이와 일치
    d = {"eval": 180611385, "cash": 70112487, "loan": 56246639, "stocks": []}
    drop = liquidation_drop_pct(d, maint=140)
    # 검산: 그 하락률 적용 시 담보비율 == 140
    ev2 = d["eval"] * (1 + drop/100)
    assert abs((ev2 + d["cash"]) / d["loan"] * 100 - 140) < 1e-6
    assert drop < 0  # 하락이어야 도달


def test_liquidation_none_without_loan():
    assert liquidation_drop_pct({"eval": 100, "cash": 0, "loan": 0, "stocks": []}) is None


def test_html_injects_values_and_is_valid():
    d = compute_collateral(_holdings(), _quotes(), _n2t(), cash=70112487)
    html = build_collateral_html(d, asof="2026-06-09 10:00").getvalue().decode()
    # 플레이스홀더가 모두 치환됐는지
    assert "__DEF_JSON__" not in html and "__STOCKS_JSON__" not in html
    assert "__MAINT__" not in html and "__ASOF__" not in html
    assert "2026-06-09 10:00" in html
    # 주입된 DEF JSON 파싱 가능 + 값 일치
    assert f'const DEF = {json.dumps({"eval": d["eval"], "cash": d["cash"], "loan": d["loan"]})};' in html
    assert "SK하이닉스" in html
    assert 'value="140"' in html and 'value="170"' in html
