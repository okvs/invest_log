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


# --- 회고 대상 매도 선택 ---
RETRO_SELECT_PREFIX = "retro_select:"


def retro_select_keyboard(transactions: list[dict]) -> InlineKeyboardMarkup:
    """회고할 매도 거래를 선택할 수 있는 인라인 키보드 생성.

    각 카드: 종목명 | 수량주 | 손익(±%) | 날짜
    callback_data: retro_select:<transaction_id>
    """
    buttons = []
    for tx in transactions:
        name = tx.get("name", "")
        qty = tx.get("quantity", 0)
        pnl = tx.get("profit_loss", 0.0)
        pnl_pct = tx.get("profit_loss_pct", 0.0)
        sign = "+" if pnl >= 0 else ""
        date = tx.get("date", "")[:10]  # YYYY-MM-DD만
        label = (
            f"{name} | {qty}주 | {sign}{int(pnl):,}원({sign}{pnl_pct:.1f}%) | {date}"
        )
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"{RETRO_SELECT_PREFIX}{tx['id']}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


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


# --- 추가 매수: 기존 섹터+사유 유지/이어쓰기/대체 ---
KEEP_EXISTING = "keep_existing"
EDIT_SECTOR = "edit_sector"
EDIT_THESIS = "edit_thesis"          # 기존거 대체
APPEND_THESIS = "append_thesis"      # 기존거에서 추가(이어쓰기)
# 하위 호환용
KEEP_THESIS = KEEP_EXISTING


def existing_info_keyboard() -> InlineKeyboardMarkup:
    """기존 보유 종목의 섹터/사유를 유지하거나 이어쓰기/대체 선택."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("그대로 유지", callback_data=KEEP_EXISTING),
        ],
        [
            InlineKeyboardButton("섹터 수정", callback_data=EDIT_SECTOR),
        ],
        [
            InlineKeyboardButton("매수사유 이어쓰기", callback_data=APPEND_THESIS),
            InlineKeyboardButton("매수사유 새로쓰기", callback_data=EDIT_THESIS),
        ],
    ])


# --- 최근 사유 빠른 선택 ---
REASON_PICK_PREFIX = "reason_pick:"

# 매도 사유 입력 시 항상 노출되는 고정 사유
AUTO_STOPLOSS_REASON = "자동손절"
SELL_PINNED_REASONS = [AUTO_STOPLOSS_REASON]


def reason_select_keyboard(reasons: list[str]) -> InlineKeyboardMarkup:
    """최근 사유를 클릭으로 고를 수 있는 인라인 키보드.

    callback_data: reason_pick:<idx> (실제 사유는 user_data에 따로 저장).
    버튼 라벨은 줄바꿈을 ' / '로 치환하고 40자에서 잘라낸다.
    """
    buttons = []
    for idx, reason in enumerate(reasons):
        label = reason.replace("\n", " / ")
        if len(label) > 40:
            label = label[:37] + "..."
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"{REASON_PICK_PREFIX}{idx}")
        ])
    return InlineKeyboardMarkup(buttons)


# 하위 호환
thesis_reuse_keyboard = existing_info_keyboard


# --- 증거금비율 선택 ---
MARGIN_PREFIX = "margin:"
MARGIN_CASH = "margin:100"
MARGIN_60 = "margin:60"
MARGIN_50 = "margin:50"
MARGIN_45 = "margin:45"
MARGIN_40 = "margin:40"


def margin_ratio_keyboard() -> InlineKeyboardMarkup:
    """매수 시 증거금비율 선택 키보드."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("현금 100%", callback_data=MARGIN_CASH),
        ],
        [
            InlineKeyboardButton("신용 60%", callback_data=MARGIN_60),
            InlineKeyboardButton("신용 50%", callback_data=MARGIN_50),
        ],
        [
            InlineKeyboardButton("신용 45%", callback_data=MARGIN_45),
            InlineKeyboardButton("신용 40%", callback_data=MARGIN_40),
        ],
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


# --- 매수 종목 검색 결과 선택 ---
BUY_STOCK_PREFIX = "buy_stock:"


def stock_search_keyboard(candidates: list) -> InlineKeyboardMarkup:
    """종목 검색 결과 선택 키보드. candidates: list of StockCandidate."""
    buttons = []
    for c in candidates:
        suffix = ".KQ" if c.market == "KOSDAQ" else ".KS"
        label = f"{c.name}  ({c.code}{suffix})  [{c.market}]"
        # callback_data: "buy_stock:종목명|코드.KS"
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"{BUY_STOCK_PREFIX}{c.name}|{c.code}{suffix}",
            )
        ])
    buttons.append([
        InlineKeyboardButton("종목코드 없이 진행", callback_data=f"{BUY_STOCK_PREFIX}|")
    ])
    return InlineKeyboardMarkup(buttons)


# --- 수정 종목 선택 ---
EDIT_SELECT_PREFIX = "edit_select:"


def edit_select_keyboard(holdings: list[dict]) -> InlineKeyboardMarkup:
    """수정할 종목을 선택할 수 있는 인라인 키보드 생성."""
    buttons = []
    for h in holdings:
        name = h["name"]
        qty = h["quantity"]
        sector = h.get("sector", "")
        avg = h.get("avg_price", 0)
        buttons.append([
            InlineKeyboardButton(
                f"{name}  |  {sector}  |  {qty}주  |  평균 {avg:,.0f}원",
                callback_data=f"{EDIT_SELECT_PREFIX}{name}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


# --- 선물: 방향 (롱/숏) ---
FUTURES_DIR_PREFIX = "fut_dir:"
FUTURES_LONG = "fut_dir:long"
FUTURES_SHORT = "fut_dir:short"


def futures_direction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("롱 (매수)", callback_data=FUTURES_LONG),
            InlineKeyboardButton("숏 (매도)", callback_data=FUTURES_SHORT),
        ]
    ])


# --- 선물: 결제월 선택 ---
FUTURES_MONTH_PREFIX = "fut_month:"


def futures_month_keyboard(months) -> InlineKeyboardMarkup:
    """결제월 선택 키보드. months: list of parsers.expiry.FuturesMonth."""
    buttons = []
    for m in months:
        buttons.append([
            InlineKeyboardButton(
                m.label(),
                callback_data=f"{FUTURES_MONTH_PREFIX}{m.contract_month}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


# --- 선물: 포지션 선택 (청산/롤오버 공용) ---
FUTURES_POS_PREFIX = "fut_pos:"


def futures_positions_keyboard(positions: list[dict]) -> InlineKeyboardMarkup:
    """선물 포지션 카드 키보드. callback_data: fut_pos:<position_id>."""
    buttons = []
    for p in positions:
        name = p.get("name", "")
        direction = "롱" if p.get("direction") == "long" else "숏"
        contracts = p.get("contracts", 0)
        month = p.get("contract_month", "")
        month_label = f"{month[2:4]}년{month[4:6]}월물" if len(month) == 6 else month
        label = f"{name} {direction} {contracts}계약 ({month_label})"
        buttons.append([
            InlineKeyboardButton(
                label, callback_data=f"{FUTURES_POS_PREFIX}{p['id']}"
            )
        ])
    return InlineKeyboardMarkup(buttons)


# --- 선물 사유 고정값 ---
FUTURES_PINNED_CLOSE_REASONS = ["자동손절", "롤오버"]


# --- 선물 증거금률 카드 (단가 × 계약수 × 승수 × rate = 증거금) ---
FUT_MARGIN_RATE_PREFIX = "fut_margin_rate:"
FUT_MARGIN_CUSTOM = "fut_margin_rate:custom"

# 자주 쓰이는 위탁증거금률 기본값 (KRX 종목별로 다름, 매번 갱신될 수 있음)
DEFAULT_FUT_MARGIN_RATES = [0.18, 0.30, 0.3285, 0.36, 0.40, 0.50]


def _format_rate_label(rate: float) -> str:
    pct = rate * 100
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct))}%"
    return f"{pct:.2f}".rstrip("0").rstrip(".") + "%"


def futures_margin_rate_keyboard(
    recent: list[float] | None = None,
) -> InlineKeyboardMarkup:
    """위탁증거금률 선택 카드 + '원화 직접 입력'.

    recent: 같은 종목에서 최근 사용한 rate 리스트. 있으면 카드 맨 앞에 노출.
    """
    seen: set[float] = set()
    ordered: list[float] = []
    for r in (recent or []) + DEFAULT_FUT_MARGIN_RATES:
        key = round(r, 4)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(r)

    buttons = []
    row = []
    for r in ordered:
        row.append(
            InlineKeyboardButton(
                _format_rate_label(r),
                callback_data=f"{FUT_MARGIN_RATE_PREFIX}{r}",
            )
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("원화 직접 입력", callback_data=FUT_MARGIN_CUSTOM)
    ])
    return InlineKeyboardMarkup(buttons)


# --- 선물 회고: 청산 거래 카드 ---
FUTURES_RETRO_PREFIX = "fut_retro:"


def futures_retro_select_keyboard(transactions: list[dict]) -> InlineKeyboardMarkup:
    """회고할 선물 청산 거래 카드.

    각 카드: 종목명 | 방향 | 계약수 | 손익(±%) | 결제월 | 날짜
    callback_data: fut_retro:<transaction_id>
    """
    buttons = []
    for tx in transactions:
        name = tx.get("name", "")
        contracts = tx.get("contracts", 0)
        direction = "롱" if tx.get("direction") == "long" else "숏"
        pnl = tx.get("pnl", 0.0)
        pnl_pct = tx.get("pnl_pct", 0.0)
        cm = tx.get("contract_month", "")
        cm_label = f"{cm[4:6]}월물" if len(cm) == 6 else cm
        sign = "+" if pnl >= 0 else ""
        date = tx.get("date", "")[:10]
        kind = "롤" if tx.get("type") == "roll_close" else "청산"
        label = (
            f"{name} {direction} {contracts}계약 | "
            f"{sign}{int(pnl):,}원({sign}{pnl_pct:.1f}%) | "
            f"{cm_label} {kind} | {date}"
        )
        buttons.append([
            InlineKeyboardButton(
                label, callback_data=f"{FUTURES_RETRO_PREFIX}{tx['id']}"
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
