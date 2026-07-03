"""회계 불변식 감사 — 장부가 조용히 오염되는 것을 조기 발견한다.

과거 사고들(증거금률≠현금부담률 6,107만 과소, 신한 예수금 버킷 박제 등)은
전부 "잔고류 값이 조용히 틀어진 채 쌓이다" 수동 대조에서야 드러났다.
여기서는 코드로 강제 가능한 불변식을 매 재발행 주기에 검사하고,
새 위반이 생기면 웹 푸시로 알린다(같은 위반 반복 알림은 fingerprint 로 억제).

불변식 (2026-07-03 실데이터 캘리브레이션):
  · 보유: 수량>0, 평단·융자·투자원금 ≥0, |투자원금 − 평단×수량| ≤ max(1000, 1%)
  · by_account 기록이 있으면: Σ수량 == 보유수량, Σcredit == credit_loan(±1)
    (USD 종목은 by_account 미사용 — 기록 없으면 검사 생략)
  · 계좌: cash·futures_cash·usd_cash ≥ 0,
    |cash − Σcash_by_account| ≤ CASH_DRIFT_LIMIT (수동/PWA 거래는 버킷을
    안 건드리므로 소량 드리프트는 정상 — 임계 초과만 경고, 정합은 실측 reconcile)
  · 선물 포지션: 계약수>0, 진입가>0, 증거금 ≥0
  · 카톡 자동반영 데드레터(_failed)가 남아 있으면 경고(미반영 체결 방치)
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from storage import json_store

logger = logging.getLogger(__name__)

CASH_DRIFT_LIMIT = 1_000_000  # 원 — cash ↔ Σcash_by_account 허용 드리프트
_AUDIT_STATE_FILE = "audit_state.json"


@dataclass
class Violation:
    code: str        # 기계용 식별자 (예: credit-mismatch)
    severity: str    # "error"(회계 깨짐) | "warn"(임계 초과·방치 항목)
    message: str     # 사람용 설명


def _inv_tolerance(h: dict) -> float:
    inv = float(h.get("total_invested", 0) or 0)
    base = 10.0 if h.get("currency") == "USD" else 1000.0
    return max(base, abs(inv) * 0.01)


def run_audit() -> list[Violation]:
    """모든 불변식을 검사해 위반 목록 반환(비어 있으면 건강)."""
    v: list[Violation] = []

    # ── 보유 ────────────────────────────────────────────────────────────
    for h in json_store.load_holdings():
        name = h.get("name", "?")
        qty = int(h.get("quantity", 0) or 0)
        avg = float(h.get("avg_price", 0) or 0)
        inv = float(h.get("total_invested", 0) or 0)
        credit = float(h.get("credit_loan", 0) or 0)

        if qty <= 0:
            v.append(Violation("qty-nonpositive", "error", f"{name}: 보유 수량 {qty} ≤ 0"))
        for field, val in (("평단", avg), ("투자원금", inv), ("융자", credit)):
            if val < 0:
                v.append(Violation("negative-value", "error", f"{name}: {field} {val:,.0f} < 0"))
        if qty > 0 and abs(inv - avg * qty) > _inv_tolerance(h):
            v.append(Violation(
                "invested-mismatch", "warn",
                f"{name}: 투자원금 {inv:,.0f} vs 평단×수량 {avg * qty:,.0f} 오차 초과",
            ))

        ba = h.get("by_account") or []
        if ba:
            ba_qty = sum(int(e.get("quantity", 0) or 0) for e in ba)
            if ba_qty != qty:
                v.append(Violation(
                    "byaccount-qty-mismatch", "error",
                    f"{name}: by_account 수량합 {ba_qty} ≠ 보유 {qty}",
                ))
            ba_credit = sum(float(e.get("credit", 0) or 0) for e in ba)
            if abs(ba_credit - credit) > 1.0:
                v.append(Violation(
                    "credit-mismatch", "error",
                    f"{name}: by_account 융자합 {ba_credit:,.0f} ≠ credit_loan {credit:,.0f}",
                ))

    # ── 계좌 ────────────────────────────────────────────────────────────
    acc = json_store.load_account()
    for field in ("cash", "futures_cash", "usd_cash"):
        val = acc.get(field)
        if val is not None and float(val) < 0:
            v.append(Violation("negative-cash", "error", f"{field} {float(val):,.0f} < 0"))
    cba = acc.get("cash_by_account")
    if isinstance(cba, dict) and cba and acc.get("cash") is not None:
        drift = float(acc["cash"]) - sum(float(x or 0) for x in cba.values())
        if abs(drift) > CASH_DRIFT_LIMIT:
            v.append(Violation(
                "cash-drift", "warn",
                f"cash {float(acc['cash']):,.0f} vs 버킷합 {sum(float(x or 0) for x in cba.values()):,.0f} "
                f"드리프트 {drift:+,.0f} (임계 {CASH_DRIFT_LIMIT:,}) — 잔고 실측 reconcile 권장",
            ))

    # ── 선물 포지션 ─────────────────────────────────────────────────────
    for p in json_store.load_futures_positions():
        pname = p.get("name", "?")
        if int(p.get("contracts", 0) or 0) <= 0:
            v.append(Violation("fut-contracts", "error", f"{pname}: 계약수 ≤ 0"))
        if float(p.get("avg_entry_price", 0) or 0) <= 0:
            v.append(Violation("fut-entry-price", "error", f"{pname}: 진입가 ≤ 0"))
        if float(p.get("initial_margin", 0) or 0) < 0:
            v.append(Violation("fut-margin", "error", f"{pname}: 증거금 < 0"))

    # ── 카톡 자동반영 데드레터 ───────────────────────────────────────────
    failed = json_store.load("kakao_apply_state.json").get("_failed") or {}
    n_failed = sum(len(ids) for ids in failed.values() if ids)
    if n_failed:
        v.append(Violation(
            "kakao-dead-letter", "warn",
            f"카톡 자동반영 실패 {n_failed}건 방치(_failed) — 수동 반영 후 항목 제거 필요",
        ))

    return v


def _fingerprint(violations: list[Violation]) -> str:
    key = "\n".join(sorted(f"{x.code}|{x.message}" for x in violations))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def audit_and_notify() -> list[Violation]:
    """감사 실행 + 새 위반 조합이면 웹 푸시 1회(같은 상태 반복 알림 억제).

    재발행 주기(dash-refresh 15분·kakao 반영 직후)마다 호출해도 시끄럽지 않다.
    위반이 해소되면 fingerprint 를 비워 다음 위반 때 다시 알린다.
    """
    violations = run_audit()
    fp = _fingerprint(violations) if violations else ""
    state_path = json_store.DATA_DIR / _AUDIT_STATE_FILE
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8")).get("fingerprint", "")
    except (OSError, json.JSONDecodeError):
        prev = None  # 파일 없음 — 건강해도 최초 1회 기록해 '감사 가동 중' 마커를 남긴다

    if fp != prev:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"fingerprint": fp}), encoding="utf-8")
        except OSError:
            logger.warning("audit state 저장 실패", exc_info=True)
        if violations:
            n_err = sum(1 for x in violations if x.severity == "error")
            head = f"장부 불변식 위반 {len(violations)}건" + (f" (error {n_err})" if n_err else "")
            body = "\n".join(f"[{x.severity}] {x.message}" for x in violations[:5])
            logger.warning("%s\n%s", head, body)
            try:
                from bot.push_service import send_push
                send_push(f"⚠️ {head}", body)
            except Exception:  # noqa: BLE001
                logger.warning("감사 푸시 발송 실패", exc_info=True)
    return violations
