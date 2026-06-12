"""현선물 괴리 알림 — 순수 로직 테스트 (네트워크/텔레그램 없음)."""
from datetime import datetime, timedelta

from bot.basis_alert import (
    BasisAlert,
    DIVERGENCE_THRESHOLD_PP,
    build_basis_alert_message,
    filter_new_alerts,
    find_divergence_alerts,
    in_monitor_window,
)


def _pos(name, sym, cm="202606", contracts=1):
    return {"name": name, "symbol": sym, "contract_month": cm,
            "contracts": contracts, "direction": "long"}


def _q(price, change, u_price, u_change, source="kis"):
    return {"price": price, "change_pct": change, "source": source,
            "underlying_price": u_price, "underlying_change_pct": u_change}


# ── find_divergence_alerts ──────────────────────────────────────────────
def test_over_threshold_included_under_excluded():
    positions = [_pos("하닉", "000660"), _pos("삼전", "005930", cm="202607")]
    quotes = {
        "000660|202606": _q(1980000, -13.4, 2070000, -9.9),   # 괴리 -3.5%p → 포함
        "005930|202607": _q(328000, -7.1, 329000, -6.4),       # 괴리 -0.7%p → 제외
    }
    alerts = find_divergence_alerts(positions, quotes)
    assert [a.name for a in alerts] == ["하닉"]
    assert round(alerts[0].divergence_pp, 1) == -3.5
    assert round(alerts[0].basis_pct, 2) == round((1980000 - 2070000) / 2070000 * 100, 2)


def test_source_not_kis_excluded():
    """yfinance 폴백(source=underlying)은 선물=현물이라 괴리 측정 불가 → 제외."""
    positions = [_pos("하닉", "000660")]
    quotes = {"000660|202606": _q(2070000, -9.9, 2070000, -9.9, source="underlying")}
    assert find_divergence_alerts(positions, quotes) == []


def test_missing_change_excluded():
    positions = [_pos("하닉", "000660")]
    quotes = {"000660|202606": _q(1980000, None, 2070000, -9.9)}  # 선물 등락 없음(manual 등)
    assert find_divergence_alerts(positions, quotes) == []


def test_zero_contracts_and_unpriced_excluded():
    positions = [_pos("청산됨", "111111", contracts=0), _pos("시세없음", "222222")]
    assert find_divergence_alerts(positions, {}) == []


def test_sorted_by_abs_divergence_desc():
    positions = [_pos("A", "111"), _pos("B", "222")]
    quotes = {
        "111|202606": _q(100, -3.5, 100, 0.0),    # 3.5
        "222|202606": _q(100, +6.0, 100, 0.0),    # 6.0
    }
    alerts = find_divergence_alerts(positions, quotes)
    assert [a.name for a in alerts] == ["B", "A"]


# ── filter_new_alerts (도배 방지) ────────────────────────────────────────
def _alert(sym="000660", div_pp=-3.5):
    # spot_change 0 → divergence == fut_change
    return BasisAlert(name="하닉", symbol=sym, contract_month="202606",
                      fut_price=1980000, fut_change_pct=div_pp,
                      spot_price=2070000, spot_change_pct=0.0, direction="long")


def test_new_crossing_fires_and_records_state():
    now = datetime(2026, 6, 6, 10, 0)
    send, state = filter_new_alerts([_alert()], {}, now)
    assert len(send) == 1
    assert state["date"] == "2026-06-06"
    assert state["symbols"]["000660"]["div"] == -3.5


def test_repeat_within_cooldown_not_widened_suppressed():
    now = datetime(2026, 6, 6, 10, 0)
    _, state = filter_new_alerts([_alert(div_pp=-3.5)], {}, now)
    later = now + timedelta(minutes=30)  # 쿨다운(120분) 이내
    send, _ = filter_new_alerts([_alert(div_pp=-3.6)], state, later)  # 거의 그대로
    assert send == []


def test_rearm_when_widened():
    now = datetime(2026, 6, 6, 10, 0)
    _, state = filter_new_alerts([_alert(div_pp=-3.5)], {}, now)
    later = now + timedelta(minutes=20)
    send, _ = filter_new_alerts([_alert(div_pp=-5.1)], state, later)  # 1.6%p 더 벌어짐
    assert len(send) == 1


def test_refires_after_cooldown():
    now = datetime(2026, 6, 6, 10, 0)
    _, state = filter_new_alerts([_alert(div_pp=-3.5)], {}, now)
    later = now + timedelta(minutes=121)  # 쿨다운 경과
    send, _ = filter_new_alerts([_alert(div_pp=-3.5)], state, later)
    assert len(send) == 1


def test_resolved_then_recross_fires_fresh():
    now = datetime(2026, 6, 6, 10, 0)
    _, state = filter_new_alerts([_alert(div_pp=-3.5)], {}, now)
    # 임계 아래로 복귀(알림 없음) → 상태에서 제거돼야 함
    mid = now + timedelta(minutes=10)
    send, state = filter_new_alerts([], state, mid)
    assert send == [] and "000660" not in state["symbols"]
    # 재돌파 → 쿨다운 무관하게 즉시 알림
    send, _ = filter_new_alerts([_alert(div_pp=-3.5)], state, now + timedelta(minutes=20))
    assert len(send) == 1


def test_date_change_resets_state():
    d1 = datetime(2026, 6, 6, 14, 0)
    _, state = filter_new_alerts([_alert()], {}, d1)
    d2 = datetime(2026, 6, 7, 10, 0)  # 다음 날
    send, state = filter_new_alerts([_alert()], state, d2)
    assert len(send) == 1 and state["date"] == "2026-06-07"


# ── 메시지 / 장중 판정 ───────────────────────────────────────────────────
def test_message_contains_key_fields():
    msg = build_basis_alert_message([_alert()])
    assert "현선물 괴리" in msg and "하닉" in msg and "%p" in msg


def test_in_monitor_window():
    assert in_monitor_window(datetime(2026, 6, 5, 9, 5))       # 금 09:05 (경계 포함)
    assert in_monitor_window(datetime(2026, 6, 5, 13, 0))      # 금 낮
    assert in_monitor_window(datetime(2026, 6, 5, 20, 0))      # 금 20:00 (경계 포함)
    assert not in_monitor_window(datetime(2026, 6, 5, 9, 4))   # 개장 직후 동시호가 반영 전
    assert not in_monitor_window(datetime(2026, 6, 5, 8, 30))  # 개장 전 (현물 미거래)
    assert not in_monitor_window(datetime(2026, 6, 5, 20, 1))  # 20시 이후
    assert not in_monitor_window(datetime(2026, 6, 6, 11, 0))  # 토요일


def test_stale_yfinance_underlying_excluded():
    """현물이 yfinance 폴백이면 전일 데이터 staleness 위험 → 측정 제외."""
    positions = [_pos("삼전", "005930")]
    q = _q(333000, +11.0, 299000, -1.2)
    q["underlying_source"] = "yfinance"
    assert find_divergence_alerts(positions, {"005930|202606": q}) == []


def test_kis_underlying_included():
    positions = [_pos("삼전", "005930")]
    q = _q(333000, +11.0, 331500, +10.9)
    q["underlying_source"] = "kis"
    # 괴리 0.1%p → 임계 미만 제외지만, 소스 가드는 통과해야 함
    assert find_divergence_alerts(positions, {"005930|202606": q}) == []
    q2 = _q(333000, +11.0, 320000, +7.0)
    q2["underlying_source"] = "kis"
    alerts = find_divergence_alerts(positions, {"005930|202606": q2})
    assert len(alerts) == 1 and round(alerts[0].divergence_pp, 1) == 4.0
