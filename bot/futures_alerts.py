"""선물 만기 임박 알림.

매일 1회 (오전 8시) JobQueue로 호출되어,
보유 선물 포지션 중 만기까지 D-N 이하인 것을 사용자에게 푸시한다.

같은 날짜에 두 번 보내지 않도록 account.json의 last_expiry_alert에
어제 알림 발송 일자를 저장한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from telegram.ext import ContextTypes

from storage.json_store import (
    load_account,
    load_chat_id,
    load_futures_positions,
    save_account,
)

logger = logging.getLogger(__name__)

# D-3 이하 만기에 알림
EXPIRY_ALERT_THRESHOLD_DAYS = 3
KST = ZoneInfo("Asia/Seoul")
ALERT_TIME = dtime(hour=8, minute=0, tzinfo=KST)


@dataclass
class ExpiryAlert:
    name: str
    direction: str   # "long" | "short"
    contracts: int
    contract_month: str
    days_to_expiry: int  # 0이면 만기 당일, 음수면 경과

    def render(self) -> str:
        direction_kr = "롱" if self.direction == "long" else "숏"
        cm = self.contract_month
        cm_label = f"{cm[2:4]}년{cm[4:6]}월물" if len(cm) == 6 else cm
        if self.days_to_expiry < 0:
            head = f"만기 경과 {-self.days_to_expiry}일"
        elif self.days_to_expiry == 0:
            head = "오늘이 만기일"
        else:
            head = f"D-{self.days_to_expiry}"
        return (
            f"[{self.name} {direction_kr} {self.contracts}계약] "
            f"{cm_label} — {head}"
        )


def _parse_iso(d: str) -> date | None:
    try:
        return date.fromisoformat(d)
    except (ValueError, TypeError):
        return None


def collect_expiry_alerts(
    positions: list[dict],
    today: date | None = None,
    threshold_days: int = EXPIRY_ALERT_THRESHOLD_DAYS,
) -> list[ExpiryAlert]:
    """알림 대상 포지션 → ExpiryAlert 리스트. 만기 임박순 정렬."""
    today = today or date.today()
    out: list[ExpiryAlert] = []
    for p in positions:
        if p.get("contracts", 0) <= 0:
            continue
        exp = _parse_iso(p.get("expiry_date", ""))
        if exp is None:
            continue
        diff = (exp - today).days
        if diff > threshold_days:
            continue
        out.append(ExpiryAlert(
            name=p.get("name", ""),
            direction=p.get("direction", "long"),
            contracts=p.get("contracts", 0),
            contract_month=p.get("contract_month", ""),
            days_to_expiry=diff,
        ))
    out.sort(key=lambda a: a.days_to_expiry)
    return out


def build_alert_message(alerts: list[ExpiryAlert]) -> str:
    if not alerts:
        return ""
    header = "⚠️ 만기 임박 선물 포지션\n"
    lines = [a.render() for a in alerts]
    return header + "\n".join(f"• {ln}" for ln in lines) + (
        "\n\n'선물롤오버' 또는 '선물청산'을 검토하세요."
    )


async def run_daily_expiry_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue 콜백: 매일 1회 호출되어 만기 임박 푸시."""
    chat_id = load_chat_id()
    if chat_id is None:
        logger.debug("만기 알림: chat_id 미설정 — 사용자가 /start 한 번 입력 필요")
        return

    positions = load_futures_positions()
    alerts = collect_expiry_alerts(positions)
    if not alerts:
        return

    today_iso = date.today().isoformat()
    account = load_account()
    if account.get("last_expiry_alert") == today_iso:
        logger.debug("만기 알림: %s 에 이미 발송됨, 스킵", today_iso)
        return

    msg = build_alert_message(alerts)
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg)
        account["last_expiry_alert"] = today_iso
        save_account(account)
    except Exception:
        logger.exception("만기 알림 발송 실패")


def schedule_daily_expiry_check(application) -> None:
    """앱 시작 시 JobQueue에 매일 알림 작업 등록."""
    if application.job_queue is None:
        logger.warning("JobQueue가 없어 만기 알림을 등록할 수 없습니다.")
        return
    application.job_queue.run_daily(
        run_daily_expiry_check,
        time=ALERT_TIME,
        name="daily_expiry_check",
    )
    logger.info("만기 임박 알림 등록: 매일 %s KST", ALERT_TIME.strftime("%H:%M"))
