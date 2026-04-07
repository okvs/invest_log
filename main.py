import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

from bot.handlers.buy import buy_conversation
from bot.handlers.dashboard import dashboard_handler
from bot.handlers.edit import edit_conversation
from bot.handlers.help import help_handler
from bot.handlers.nickname import nickname_handler
from bot.handlers.sell import sell_conversation

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context) -> None:
    await update.message.reply_text(
        "안녕하세요! 투자 로그 봇입니다.\n"
        "사용 가능한 명령어:\n"
        "매수 - 매수 기록\n"
        "매도 - 매도 기록 + 회고\n"
        "수정 - 보유 종목 수정\n"
        "현황 - 투자 현황\n"
        "닉네임 - 종목 닉네임 관리\n"
        "도움말 - 사용법"
    )


def _korean_command(keyword: str) -> filters.BaseFilter:
    """한국어 키워드로 시작하는 메시지를 필터링."""
    return filters.Regex(rf"^{keyword}$")


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN 환경변수를 설정해주세요. (.env 파일 참고)")

    persistence = PicklePersistence(filepath="data/bot_persistence.pickle")
    app = Application.builder().token(token).persistence(persistence).build()

    # 기본 명령어
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("dashboard", dashboard_handler))
    app.add_handler(CommandHandler("nickname", nickname_handler))

    # 한국어 키워드 핸들러
    app.add_handler(MessageHandler(_korean_command("도움말"), help_handler))
    app.add_handler(MessageHandler(_korean_command("현황"), dashboard_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^닉네임"), nickname_handler))

    # ConversationHandler (매수/매도/수정)
    app.add_handler(buy_conversation())
    app.add_handler(sell_conversation())
    app.add_handler(edit_conversation())

    logger.info("봇 시작!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
