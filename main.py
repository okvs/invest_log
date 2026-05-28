import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from bot.futures_alerts import (
    build_alert_message,
    collect_expiry_alerts,
    schedule_daily_expiry_check,
)
from bot.handlers.broker import broker_conversation
from bot.handlers.buy import buy_conversation
from bot.handlers.cash import cash_conversation
from bot.handlers.asset_graph import asset_graph_handler
from bot.handlers.backtest import backtest_handler
from bot.handlers.cash_event import (
    delete_cash_event,
    deposit_conversation,
    list_cash_events,
)
from bot.handlers.dashboard import dashboard_handler
from bot.handlers.edit import edit_conversation
from bot.handlers.futures_buy import futures_entry_conversation
from bot.handlers.futures_quote import futures_quote_conversation
from bot.handlers.futures_retro import futures_retro_conversation
from bot.handlers.futures_roll import futures_roll_conversation
from bot.handlers.futures_sell import futures_close_conversation
from bot.handlers.help import help_handler
from bot.handlers.nickname import nickname_handler
from bot.handlers.retro import retro_conversation
from bot.handlers.sell import sell_conversation
from storage.json_store import load_futures_positions, save_chat_id

load_dotenv()

# 로그 설정: 파일(logs/bot.log) + 콘솔 동시 출력
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

_log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_formatter = logging.Formatter(_log_format)

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_formatter)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_file_handler, _console_handler],
)
logger = logging.getLogger(__name__)


async def _cache_chat_id(update: Update, context) -> None:
    """모든 update에서 chat_id를 자동 캐싱 (만기 알림 등 푸시 발송용).

    group=-1로 등록되어 다른 핸들러보다 먼저 실행되지만,
    ApplicationHandlerStop를 던지지 않아 후속 핸들러도 정상 동작.
    """
    chat = update.effective_chat if update else None
    if chat is None:
        return
    try:
        save_chat_id(chat.id)
    except Exception:
        logger.warning("chat_id 자동 캐싱 실패", exc_info=True)


async def start(update: Update, context) -> None:
    await update.message.reply_text(
        "안녕하세요! 투자 로그 봇입니다.\n"
        "사용 가능한 명령어:\n"
        "매수 - 매수 기록\n"
        "매도 - 매도 기록\n"
        "회고 - 매도 회고 작성\n"
        "수정 - 보유 종목 수정\n"
        "현황 - 투자 현황 (잔고와 동일)\n"
        "잔고 - 투자 현황 (현황과 동일)\n"
        "예수금 - 초기자본/예수금 설정\n"
        "닉네임 - 종목 닉네임 관리\n"
        "선물진입 - 개별주식선물 진입\n"
        "선물청산 - 선물 포지션 청산\n"
        "선물롤오버 - 차월물로 롤오버\n"
        "선물회고 - 선물 청산 회고\n"
        "선물시세 - 정확한 선물가 수동 입력 (6시간 유효)\n"
        "만기점검 - 만기 임박 선물 포지션 즉시 점검\n"
        "자산그래프 - 기록 첫날부터 일별 NAV 추이 그래프\n"
        "백테스트 - 과거 거래일을 동결했으면 오늘 NAV 가 어땠을지 비교\n"
        "입금 - 입금 이벤트 등록 (날짜+금액+메모)\n"
        "출금 - 출금 이벤트 등록\n"
        "입출금목록 - 등록된 입출금 이벤트 목록\n"
        "도움말 - 사용법"
    )


def _korean_command(keyword: str) -> filters.BaseFilter:
    """한국어 키워드로 시작하는 메시지를 필터링."""
    return filters.Regex(rf"^{keyword}$")


async def expiry_check_handler(update: Update, context) -> None:
    """만기점검 즉시 명령. JobQueue 알림과 같은 메시지를 호출 즉시 표시."""
    alerts = collect_expiry_alerts(load_futures_positions())
    if not alerts:
        await update.message.reply_text("만기 임박 선물 포지션이 없습니다.")
        return
    await update.message.reply_text(build_alert_message(alerts))


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN 환경변수를 설정해주세요. (.env 파일 참고)")

    app = Application.builder().token(token).build()

    # 모든 메시지에서 chat_id 자동 캐싱 (다른 핸들러 동작은 그대로)
    app.add_handler(TypeHandler(Update, _cache_chat_id), group=-1)

    # 기본 명령어
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("dashboard", dashboard_handler))
    app.add_handler(CommandHandler("nickname", nickname_handler))

    # 한국어 키워드 핸들러
    app.add_handler(MessageHandler(_korean_command("도움말"), help_handler))
    app.add_handler(MessageHandler(_korean_command("현황"), dashboard_handler))
    app.add_handler(MessageHandler(_korean_command("잔고"), dashboard_handler))
    app.add_handler(MessageHandler(_korean_command("자산그래프"), asset_graph_handler))
    app.add_handler(MessageHandler(_korean_command("백테스트"), backtest_handler))
    app.add_handler(MessageHandler(_korean_command("입출금목록"), list_cash_events))
    app.add_handler(MessageHandler(filters.Regex(r"^입출금삭제"), delete_cash_event))
    app.add_handler(MessageHandler(_korean_command("만기점검"), expiry_check_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^닉네임"), nickname_handler))

    # ConversationHandler — 증권사 메시지가 먼저 매칭되도록 순서 중요
    app.add_handler(broker_conversation())
    app.add_handler(cash_conversation())
    app.add_handler(deposit_conversation())
    app.add_handler(buy_conversation())
    app.add_handler(sell_conversation())
    app.add_handler(retro_conversation())
    app.add_handler(edit_conversation())
    app.add_handler(futures_entry_conversation())
    app.add_handler(futures_close_conversation())
    app.add_handler(futures_roll_conversation())
    app.add_handler(futures_retro_conversation())
    app.add_handler(futures_quote_conversation())

    schedule_daily_expiry_check(app)

    logger.info("봇 시작!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
