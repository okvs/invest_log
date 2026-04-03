import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    PicklePersistence,
)

from bot.handlers.buy import buy_conversation
from bot.handlers.sell import sell_conversation
from bot.handlers.dashboard import dashboard_handler
from bot.handlers.help import help_handler

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context) -> None:
    await update.message.reply_text(
        "안녕하세요! 투자 로그 봇입니다.\n"
        "사용 가능한 명령어:\n"
        "/매수 - 매수 기록\n"
        "/매도 - 매도 기록 + 회고\n"
        "/현황 - 투자 현황\n"
        "/도움말 - 사용법"
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN 환경변수를 설정해주세요. (.env 파일 참고)")

    persistence = PicklePersistence(filepath="data/bot_persistence.pickle")
    app = Application.builder().token(token).persistence(persistence).build()

    # 기본 명령어
    app.add_handler(CommandHandler(["start"], start))
    app.add_handler(CommandHandler(["help", "도움말"], help_handler))
    app.add_handler(CommandHandler(["dashboard", "현황"], dashboard_handler))

    # ConversationHandler (매수/매도)
    app.add_handler(buy_conversation())
    app.add_handler(sell_conversation())

    logger.info("봇 시작!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
