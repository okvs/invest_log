from telegram import Update
from telegram.ext import ContextTypes

from invest_log.bot.formatters import format_dashboard
from invest_log.storage.json_store import load_holdings

TELEGRAM_MSG_LIMIT = 4096


async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """보유 종목 현황 대시보드를 전송한다."""
    holdings = load_holdings()
    text = format_dashboard(holdings)

    # 텔레그램 메시지 길이 제한 초과 시 분할 전송
    if len(text) <= TELEGRAM_MSG_LIMIT:
        await update.message.reply_text(text)
        return

    # 줄 단위로 분할하여 제한 내에서 최대한 묶어 전송
    lines = text.split("\n")
    chunk: list[str] = []
    chunk_len = 0

    for line in lines:
        # +1 은 줄바꿈 문자
        added_len = len(line) + (1 if chunk else 0)
        if chunk_len + added_len > TELEGRAM_MSG_LIMIT:
            await update.message.reply_text("\n".join(chunk))
            chunk = [line]
            chunk_len = len(line)
        else:
            chunk.append(line)
            chunk_len += added_len

    if chunk:
        await update.message.reply_text("\n".join(chunk))
