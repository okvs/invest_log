"""현선물 괴리(베이시스) 장중 알림.

평일 08:00~20:00 KST JobQueue로 10분마다 호출되어, 보유 선물 포지션의
**당일 변동률 괴리**(선물 등락% − 현물 등락%)가 임계(기본 3%p) 이상이면 푸시한다.

선물 시세가 KIS 실시간(source=='kis')일 때만 유효하다. yfinance 폴백
(source=='underlying')이면 선물가=현물가라 괴리가 항상 0이므로 스킵한다.

도배 방지(종목별): (1) 임계 신규 돌파 시 1회, (2) 직전 알림보다 REARM_WIDEN_PP
이상 더 벌어지면, (3) 쿨다운(COOLDOWN_MIN분) 경과 후 여전히 임계 이상이면 재알림.
임계 미만으로 좁혀지면 상태에서 제거해 다음 재돌파 때 새로 알린다. 변동률은 매일
0부터 시작하므로 날짜가 바뀌면 상태를 리셋한다.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.futures_quote import fetch_futures_quotes
from storage.json_store import (
    load as _load_json,
    save as _save_json,
    load_chat_id,
    load_futures_positions,
)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
DIVERGENCE_THRESHOLD_PP = 3.0   # |선물% − 현물%| 알림 임계 (%p)
REARM_WIDEN_PP = 1.5            # 직전 알림보다 이만큼 더 벌어지면 재알림 (%p)
COOLDOWN_MIN = 120             # 재알림 쿨다운 (분)
CHECK_INTERVAL_SEC = 600       # 점검 간격 (10분)
MONITOR_OPEN = dtime(8, 0)     # 감시 시작 (오전 8시)
MONITOR_CLOSE = dtime(20, 0)   # 감시 종료 (오후 8시)
_STATE_FILE = "basis_alert_state.json"


@dataclass
class BasisAlert:
    name: str
    symbol: str
    contract_month: str
    fut_price: float
    fut_change_pct: float
    spot_price: float
    spot_change_pct: float
    direction: str

    @property
    def divergence_pp(self) -> float:
        """당일 변동률 괴리 = 선물% − 현물%. 음수 = 선물이 더 약세."""
        return self.fut_change_pct - self.spot_change_pct

    @property
    def basis_pct(self) -> float:
        """베이시스 = (선물−현물)/현물 × 100."""
        return (self.fut_price - self.spot_price) / self.spot_price * 100 if self.spot_price else 0.0

    def render(self) -> str:
        cm = self.contract_month
        cm_label = f"{cm[2:4]}년{cm[4:6]}월물" if len(cm) == 6 else cm
        d = self.divergence_pp
        note = "선물이 더 약세(디스카운트↑)" if d < 0 else "선물이 더 강세(프리미엄↑)"
        nm = html.escape(self.name)
        return (
            f"<b>{nm}</b> ({cm_label})\n"
            f"  선물 {self.fut_price:,.0f} ({self.fut_change_pct:+.1f}%) vs "
            f"현물 {self.spot_price:,.0f} ({self.spot_change_pct:+.1f}%)\n"
            f"  당일 괴리 <b>{d:+.1f}%p</b> · 베이시스 {self.basis_pct:+.2f}% — {note}"
        )


def _entry_for(quotes: dict, p: dict) -> dict | None:
    sym = p.get("symbol", "")
    cm = p.get("contract_month", "")
    e = quotes.get(f"{sym}|{cm}")
    if e is None:
        e = quotes.get(sym)
    return e if isinstance(e, dict) else None


def find_divergence_alerts(
    positions: list[dict],
    quotes: dict,
    *,
    threshold_pp: float = DIVERGENCE_THRESHOLD_PP,
) -> list[BasisAlert]:
    """임계 이상 괴리 포지션 → BasisAlert 리스트 (도배 필터 전 순수 계산).

    source!='kis' 이거나 선물/현물 등락률이 비어 있으면 측정 불가로 제외한다.
    괴리 절대값 내림차순 정렬.
    """
    out: list[BasisAlert] = []
    for p in positions:
        if p.get("contracts", 0) <= 0:
            continue
        e = _entry_for(quotes, p)
        if e is None or e.get("source") != "kis":
            continue
        fc = e.get("change_pct")
        uc = e.get("underlying_change_pct")
        fp = e.get("price")
        up = e.get("underlying_price")
        if fc is None or uc is None or fp is None or up is None:
            continue
        if abs(float(fc) - float(uc)) < threshold_pp:
            continue
        out.append(BasisAlert(
            name=p.get("name", ""), symbol=p.get("symbol", ""),
            contract_month=p.get("contract_month", ""),
            fut_price=float(fp), fut_change_pct=float(fc),
            spot_price=float(up), spot_change_pct=float(uc),
            direction=p.get("direction", "long"),
        ))
    out.sort(key=lambda a: abs(a.divergence_pp), reverse=True)
    return out


def filter_new_alerts(
    alerts: list[BasisAlert],
    state: dict,
    now: datetime,
    *,
    rearm_pp: float = REARM_WIDEN_PP,
    cooldown_min: float = COOLDOWN_MIN,
) -> tuple[list[BasisAlert], dict]:
    """도배 방지 — 실제로 보낼 알림만 추리고 갱신된 state 를 반환.

    state = {"date": "YYYY-MM-DD", "symbols": {sym: {"div": float, "ts": iso}}}
    날짜가 바뀌면 리셋. 이번에 임계 미만인(=alerts 에 없는) 종목은 state 에서 제거해
    재돌파 시 다시 알리도록 한다.
    """
    today = now.date().isoformat()
    if state.get("date") != today:
        state = {"date": today, "symbols": {}}
    syms: dict = state.setdefault("symbols", {})

    active_syms = {a.symbol for a in alerts}
    for s in list(syms):
        if s not in active_syms:
            del syms[s]  # 임계 아래로 복귀 → 상태 해제

    send: list[BasisAlert] = []
    for a in alerts:
        prev = syms.get(a.symbol)
        cur = a.divergence_pp
        if prev is None:
            fire = True  # 신규 돌파
        else:
            widened = abs(cur) - abs(float(prev["div"])) >= rearm_pp
            try:
                cooled = (now - datetime.fromisoformat(prev["ts"])).total_seconds() >= cooldown_min * 60
            except (ValueError, TypeError, KeyError):
                cooled = True
            fire = widened or cooled
        if fire:
            send.append(a)
            syms[a.symbol] = {"div": cur, "ts": now.isoformat()}
    return send, state


def build_basis_alert_message(alerts: list[BasisAlert]) -> str:
    if not alerts:
        return ""
    header = "⚠️ 현선물 괴리 확대\n\n"
    body = "\n\n".join(a.render() for a in alerts)
    return header + body + "\n\n→ 롤오버·차익·마진 점검"


def load_basis_alert_state() -> dict:
    return _load_json(_STATE_FILE)


def save_basis_alert_state(state: dict) -> None:
    _save_json(_STATE_FILE, state)


def in_monitor_window(now: datetime) -> bool:
    """감시 시간대(평일 08:00~20:00) 여부."""
    if now.weekday() >= 5:  # 토(5)·일(6) — 휴장
        return False
    return MONITOR_OPEN <= now.time() <= MONITOR_CLOSE


async def run_basis_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue 콜백 — 평일 08:00~20:00, 10분마다 현선물 괴리 점검·푸시."""
    chat_id = load_chat_id()
    if chat_id is None:
        return
    now = datetime.now(KST)
    if not in_monitor_window(now):
        return
    positions = [p for p in load_futures_positions() if p.get("contracts", 0) > 0]
    if not positions:
        return
    try:
        quotes = await fetch_futures_quotes(positions)
    except Exception:
        logger.exception("현선물 괴리 알림: 시세 조회 실패")
        return

    alerts = find_divergence_alerts(positions, quotes)
    state = load_basis_alert_state()
    send, new_state = filter_new_alerts(alerts, state, now)
    save_basis_alert_state(new_state)
    if not send:
        return
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=build_basis_alert_message(send),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("현선물 괴리 알림 발송 실패")


def schedule_basis_check(application) -> None:
    """앱 시작 시 JobQueue 에 장중 괴리 점검 작업 등록."""
    if application.job_queue is None:
        logger.warning("JobQueue가 없어 현선물 괴리 알림을 등록할 수 없습니다.")
        return
    application.job_queue.run_repeating(
        run_basis_check, interval=CHECK_INTERVAL_SEC, first=30,
        name="basis_divergence_check",
    )
    logger.info("현선물 괴리 알림 등록: %d초 간격 (평일 08:00~20:00)", CHECK_INTERVAL_SEC)
