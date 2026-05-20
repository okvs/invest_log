"""증권사 체결 알림 메시지 처리 ConversationHandler.

KB증권 ([KB증권] ...) 또는 신한증권 (계좌명 : ...) 메시지를
붙여넣으면 매수/매도를 자동 파싱하고 부족한 정보만 추가 질문.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.formatters import format_buy_result, format_sell_result
from bot.handlers.sell import _process_sell
from bot.handlers.buy import (
    _process_and_save,
    _find_existing_holding,
    _strip_name,
)
from bot.keyboards import (
    APPEND_THESIS,
    EDIT_SECTOR,
    EDIT_THESIS,
    KEEP_EXISTING,
    MARGIN_PREFIX,
    REASON_PICK_PREFIX,
    SELL_PINNED_REASONS,
    existing_info_keyboard,
    margin_ratio_keyboard,
    reason_select_keyboard,
)
from parsers.input_parser import BuyInput, parse_broker_message, resolve_name
from storage.json_store import (
    get_recent_reasons,
    load_account,
    load_nickname_map,
)

# ConversationHandler states (10~부터 시작하여 buy/sell 상태값과 충돌 방지)
SELL_REASON = 10
BUY_SECTOR = 11
BUY_THESIS = 12
BROKER_EXISTING_CONFIRM = 13
BROKER_MARGIN_SELECT = 14
BUY_THESIS_APPEND = 15


def _end_other_conversations(
    context: ContextTypes.DEFAULT_TYPE, update: Update, keep: str = "broker"
) -> None:
    """다른 ConversationHandler에 남아있는 orphan 상태를 정리.

    사용자가 '매수'/'매도'/'수정' 명령으로 buy/sell/edit 대화를 시작한 뒤
    broker 메시지를 붙여넣으면, broker가 update를 가로채기 때문에 기존 대화는
    해당 update를 보지 못하고 state가 남은 채로 방치됨.
    이후 '매도' 같은 다른 명령을 입력하면 고아 상태에 걸려서
    '매수 기록이 취소되었습니다' 메시지가 뜨는 문제가 생김.
    broker 대화가 시작될 때 이 함수를 호출해 다른 ConversationHandler의
    state를 명시적으로 비워준다.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    key = (chat.id, user.id)
    app = context.application
    for handlers in app.handlers.values():
        for handler in handlers:
            if (
                isinstance(handler, ConversationHandler)
                and handler.name != keep
                and key in handler._conversations
            ):
                handler._conversations.pop(key, None)


async def _receive_broker_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증권사 메시지 파싱 → 매수/매도 분기."""
    _end_other_conversations(context, update, keep="broker")
    text = update.message.text

    try:
        msg = parse_broker_message(text)
    except ValueError as e:
        await update.message.reply_text(f"메시지 인식 실패: {e}")
        return ConversationHandler.END

    # 닉네임 변환 + 공백 제거
    nmap = load_nickname_map()
    msg.name = resolve_name(msg.name, nickname_map=nmap)

    if msg.trade_type == "sell":
        context.user_data["broker_sell"] = msg
        reasons = get_recent_reasons("sell", pinned=SELL_PINNED_REASONS)
        context.user_data["recent_reasons"] = reasons
        prompt = (
            f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매도 체결 확인.\n\n"
            "매도사유를 입력해주세요.\n\n"
            "최근 사유를 선택하거나 직접 입력하세요."
        )
        await update.message.reply_text(
            prompt,
            reply_markup=reason_select_keyboard(reasons),
        )
        return SELL_REASON
    else:
        # 기존 보유 종목 확인
        existing = _find_existing_holding("", msg.name)
        if existing:
            sector = existing.get("sector", "") or "(없음)"
            thesis = existing.get("buy_thesis", "") or "(없음)"

            buy_input = BuyInput(
                name=msg.name,
                ticker=existing.get("ticker", ""),
                sector=existing.get("sector", ""),
                quantity=msg.quantity,
                price=msg.price,
                thesis=existing.get("buy_thesis", ""),
            )
            context.user_data["buy_input"] = buy_input

            await update.message.reply_text(
                f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매수 체결 확인.\n\n"
                f"기존 보유 종목입니다.\n\n"
                f"섹터: {sector}\n"
                f"매수사유: {thesis}\n\n"
                f"그대로 유지하거나 수정할 항목을 선택해주세요.",
                reply_markup=existing_info_keyboard(),
            )
            return BROKER_EXISTING_CONFIRM
        else:
            context.user_data["broker_buy"] = msg
            await update.message.reply_text(
                f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매수 체결 확인.\n\n"
                "섹터를 입력해주세요. (예: 반도체, IT, 바이오)"
            )
            return BUY_SECTOR


async def _sell_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도사유 직접 입력 → 매도 처리."""
    reason = update.message.text.strip()
    msg = context.user_data.pop("broker_sell")
    context.user_data.pop("recent_reasons", None)

    return await _process_sell(
        update, context,
        msg.name, msg.quantity, msg.price, reason,
        error_state=ConversationHandler.END,
    )


async def _sell_reason_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """최근 매도 사유 버튼 클릭 → 그 사유로 매도 처리."""
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    msg = context.user_data.pop("broker_sell", None)
    context.user_data.pop("recent_reasons", None)

    if msg is None or idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    reason = reasons[idx]
    await query.edit_message_text(f"매도 사유: {reason}")
    return await _process_sell(
        query, context,
        msg.name, msg.quantity, msg.price, reason,
        error_state=ConversationHandler.END,
    )


async def _broker_ask_margin(update, context, buy_input, *, is_callback=False) -> int:
    """증거금비율 질문. 계좌 미설정 시 바로 저장."""
    account = load_account()
    if not account.get("initial_capital"):
        result_text = _process_and_save(buy_input)
        if is_callback:
            query = update.callback_query if hasattr(update, 'callback_query') and update.callback_query else update
            await query.edit_message_text(result_text)
        else:
            await update.message.reply_text(result_text)
        return ConversationHandler.END

    context.user_data["buy_input"] = buy_input
    msg = "증거금비율을 선택해주세요."
    if is_callback:
        query = update.callback_query if hasattr(update, 'callback_query') and update.callback_query else update
        await query.edit_message_text(msg, reply_markup=margin_ratio_keyboard())
    else:
        await update.message.reply_text(msg, reply_markup=margin_ratio_keyboard())
    return BROKER_MARGIN_SELECT


async def _broker_margin_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증거금비율 선택 후 매수 저장."""
    query = update.callback_query
    await query.answer()

    margin_ratio = int(query.data.removeprefix(MARGIN_PREFIX))
    buy_input = context.user_data.pop("buy_input", None)
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    result_text = _process_and_save(buy_input, margin_ratio=margin_ratio)
    await query.edit_message_text(result_text)
    return ConversationHandler.END


async def _broker_existing_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증권사 매수: 기존 보유 종목 섹터/근거 유지·이어쓰기·대체."""
    query = update.callback_query
    await query.answer()

    buy_input = context.user_data.get("buy_input")
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    if query.data == KEEP_EXISTING:
        return await _broker_ask_margin(update, context, buy_input, is_callback=True)
    elif query.data == EDIT_SECTOR:
        await query.edit_message_text("새로운 섹터를 입력해주세요.")
        context.user_data["_broker_edit"] = "sector"
        return BUY_SECTOR
    elif query.data == APPEND_THESIS:
        existing_thesis = buy_input.thesis or ""
        context.user_data["append_base"] = existing_thesis
        preview = existing_thesis if existing_thesis else "(기존 사유 없음)"
        await query.edit_message_text(
            f"기존 매수사유:\n{preview}\n\n이어붙일 내용을 입력해주세요."
        )
        return BUY_THESIS_APPEND
    else:  # EDIT_THESIS — 대체
        context.user_data["_broker_edit"] = "thesis"
        return await _broker_ask_thesis(
            query, context, is_callback=True,
            prompt="새로운 매수 근거를 입력해주세요.",
        )


async def _buy_sector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """섹터 입력."""
    sector = update.message.text.strip()

    # 기존 보유 종목에서 섹터 수정 중인 경우 → 바로 저장
    edit_mode = context.user_data.pop("_broker_edit", None)
    if edit_mode == "sector":
        buy_input = context.user_data.pop("buy_input", None)
        if buy_input:
            buy_input.sector = sector
            return await _broker_ask_margin(update, context, buy_input)

    # 신규 종목 → 매수 근거 입력 (최근 사유 버튼)
    context.user_data["broker_sector"] = sector
    return await _broker_ask_thesis(update, context, is_callback=False)


async def _broker_ask_thesis(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    is_callback: bool,
    prompt: str = "매수 근거를 입력해주세요.",
) -> int:
    """증권사 매수의 매수근거 입력 프롬프트 (최근 사유 버튼 포함)."""
    reasons = get_recent_reasons("buy")
    context.user_data["recent_reasons"] = reasons

    msg = prompt
    if reasons:
        msg += "\n\n최근 사유를 선택하거나 직접 입력하세요."
    keyboard = reason_select_keyboard(reasons) if reasons else None

    if is_callback:
        await update_or_query.edit_message_text(msg, reply_markup=keyboard)
    else:
        await update_or_query.message.reply_text(msg, reply_markup=keyboard)
    return BUY_THESIS


async def _buy_thesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매수근거 직접 입력 → 매수 저장."""
    thesis = update.message.text.strip()
    context.user_data.pop("recent_reasons", None)

    # 기존 보유 종목에서 근거 수정 중인 경우 → 바로 저장
    edit_mode = context.user_data.pop("_broker_edit", None)
    if edit_mode == "thesis":
        buy_input = context.user_data.pop("buy_input", None)
        if buy_input:
            buy_input.thesis = thesis
            return await _broker_ask_margin(update, context, buy_input)

    # 신규 종목
    msg = context.user_data.pop("broker_buy")
    sector = context.user_data.pop("broker_sector")

    buy_input = BuyInput(
        name=msg.name,
        ticker="",
        sector=sector,
        quantity=msg.quantity,
        price=msg.price,
        thesis=thesis,
    )

    return await _broker_ask_margin(update, context, buy_input)


async def _buy_thesis_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """최근 매수 사유 버튼 클릭 → 그 사유로 저장."""
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    context.user_data.pop("recent_reasons", None)

    if idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    thesis = reasons[idx]

    edit_mode = context.user_data.pop("_broker_edit", None)
    if edit_mode == "thesis":
        buy_input = context.user_data.pop("buy_input", None)
        if buy_input:
            buy_input.thesis = thesis
            return await _broker_ask_margin(query, context, buy_input, is_callback=True)

    # 신규 종목
    msg = context.user_data.pop("broker_buy", None)
    sector = context.user_data.pop("broker_sector", "")
    if msg is None:
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    buy_input = BuyInput(
        name=msg.name,
        ticker="",
        sector=sector,
        quantity=msg.quantity,
        price=msg.price,
        thesis=thesis,
    )
    return await _broker_ask_margin(query, context, buy_input, is_callback=True)


async def _buy_thesis_append(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """기존 매수사유에 이어쓰기 → 결합 후 저장."""
    extra = update.message.text.strip()
    base = context.user_data.pop("append_base", "")
    buy_input = context.user_data.pop("buy_input", None)
    context.user_data.pop("_broker_edit", None)

    if buy_input is None:
        await update.message.reply_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    buy_input.thesis = f"{base}\n{extra}" if base else extra
    return await _broker_ask_margin(update, context, buy_input)


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 취소."""
    for key in (
        "broker_sell", "broker_buy", "broker_sector",
        "buy_input", "_broker_edit", "recent_reasons", "append_base",
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text("취소되었습니다.")
    return ConversationHandler.END


def _other_command_filter() -> filters.BaseFilter:
    """다른 명령어 필터 — 대화 중 다른 명령 입력 시 대화 종료용."""
    return filters.Regex(r"^(매도|매수|현황|도움말|수정|회고)$") | filters.COMMAND


def broker_conversation() -> ConversationHandler:
    """증권사 체결 메시지 ConversationHandler."""
    other_cmd = _other_command_filter()

    return ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r"(?s)^\[KB증권\]"), _receive_broker_msg
            ),
            MessageHandler(
                filters.Regex(r"(?s)^계좌명\s*:"), _receive_broker_msg
            ),
        ],
        states={
            SELL_REASON: [
                CallbackQueryHandler(_sell_reason_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _sell_reason),
            ],
            BUY_SECTOR: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_sector),
            ],
            BUY_THESIS: [
                CallbackQueryHandler(_buy_thesis_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_thesis),
            ],
            BUY_THESIS_APPEND: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_thesis_append),
            ],
            BROKER_EXISTING_CONFIRM: [
                CallbackQueryHandler(
                    _broker_existing_confirm,
                    pattern=f"^({KEEP_EXISTING}|{EDIT_SECTOR}|{EDIT_THESIS}|{APPEND_THESIS})$",
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            BROKER_MARGIN_SELECT: [
                CallbackQueryHandler(_broker_margin_selected, pattern=f"^{MARGIN_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _cancel),
            CommandHandler("cancel", _cancel),
        ],
        name="broker",
        allow_reentry=True,
    )
