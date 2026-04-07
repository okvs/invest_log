from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# --- 매수 확인 ---
CONFIRM_BUY = "confirm_buy"
EDIT_BUY = "edit_buy"
CANCEL_BUY = "cancel_buy"


def buy_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("확인", callback_data=CONFIRM_BUY),
            InlineKeyboardButton("수정", callback_data=EDIT_BUY),
            InlineKeyboardButton("취소", callback_data=CANCEL_BUY),
        ]
    ])


# --- 회고 시작 여부 ---
START_RETRO = "start_retro"
SKIP_RETRO = "skip_retro"


def retro_ask_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("회고 시작", callback_data=START_RETRO),
            InlineKeyboardButton("나중에", callback_data=SKIP_RETRO),
        ]
    ])


# --- 투자 판단 평가 ---
THESIS_CORRECT = "thesis_correct"
THESIS_WRONG = "thesis_wrong"
THESIS_PARTIAL = "thesis_partial"


def thesis_eval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("맞았다", callback_data=THESIS_CORRECT),
            InlineKeyboardButton("틀렸다", callback_data=THESIS_WRONG),
            InlineKeyboardButton("부분적으로", callback_data=THESIS_PARTIAL),
        ]
    ])


# --- 아쉬움 회피 가능 여부 ---
AVOIDABLE_YES = "avoidable_yes"
AVOIDABLE_NO = "avoidable_no"
AVOIDABLE_UNKNOWN = "avoidable_unknown"


def avoidable_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("피할 수 있었다", callback_data=AVOIDABLE_YES),
            InlineKeyboardButton("통제 불가", callback_data=AVOIDABLE_NO),
            InlineKeyboardButton("모르겠다", callback_data=AVOIDABLE_UNKNOWN),
        ]
    ])


# --- 매도 종목 선택 ---
SELL_SELECT_PREFIX = "sell_select:"


def holdings_select_keyboard(holdings: list[dict]) -> InlineKeyboardMarkup:
    """보유 종목을 선택할 수 있는 인라인 키보드 생성."""
    buttons = []
    for h in holdings:
        name = h["name"]
        qty = h["quantity"]
        buttons.append([
            InlineKeyboardButton(
                f"{name}  |  {qty}주",
                callback_data=f"{SELL_SELECT_PREFIX}{name}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


# --- 매도 확인 ---
CONFIRM_SELL = "confirm_sell"
CANCEL_SELL = "cancel_sell"


def sell_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("확인", callback_data=CONFIRM_SELL),
            InlineKeyboardButton("취소", callback_data=CANCEL_SELL),
        ]
    ])
