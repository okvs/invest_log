"""매수 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers.buy import (
    APPEND_INPUT,
    EXISTING_CONFIRM,
    SECTOR_INPUT,
    THESIS_INPUT,
    _append_input,
    _existing_confirm,
    _receive_input,
    _sector_input,
    _start,
    _thesis_input,
    _thesis_pick,
)
from bot.keyboards import REASON_PICK_PREFIX
from parsers.input_parser import StockCandidate
from storage.json_store import (
    get_recent_reasons,
    load_holdings,
    load_ticker_map,
    load_transactions,
    save_nickname_map,
    save_ticker_map,
    save_transactions,
)


def _make_update_and_context(text: str = ""):
    """가짜 Update, Context 생성."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    return update, context


def _make_callback_update(data: str):
    """가짜 콜백 Update 생성."""
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.message = None
    return update


# ── /buy 시작 ──


@pytest.mark.asyncio
async def test_start_returns_input_state():
    update, context = _make_update_and_context()
    result = await _start(update, context)
    assert result == 0  # INPUT state
    update.message.reply_text.assert_called_once()


# ── 매수 입력 파싱 실패 ──


@pytest.mark.asyncio
async def test_receive_input_invalid_returns_input():
    update, context = _make_update_and_context("잘못된 입력")
    result = await _receive_input(update, context)
    assert result == 0  # INPUT (다시 입력 요청)
    reply = update.message.reply_text.call_args[0][0]
    assert "입력 오류" in reply


# ── 신규 매수: 종목명/수량/매수가 → 섹터 → 근거 ──


@pytest.mark.asyncio
async def test_receive_input_new_buy():
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼성전자\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == SECTOR_INPUT  # 신규 → 섹터 입력 요청

    # 섹터 입력
    sector_update, _ = _make_update_and_context("반도체")
    result = await _sector_input(sector_update, context)
    assert result == THESIS_INPUT

    # 근거 입력
    thesis_update, _ = _make_update_and_context("AI 수요 증가 전망")
    result = await _thesis_input(thesis_update, context)
    assert result == -1  # ConversationHandler.END

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"
    assert holdings[0]["quantity"] == 10
    assert holdings[0]["avg_price"] == 72000
    assert holdings[0]["ticker"] == "005930.KS"
    assert holdings[0]["sector"] == "반도체"
    assert holdings[0]["buy_thesis"] == "AI 수요 증가 전망"


# ── 신규 매수 — 검색으로 정확히 1개 매칭 ──


@pytest.mark.asyncio
@patch(
    "bot.handlers.buy.search_stocks",
    return_value=[StockCandidate("삼성전자", "005930", "KOSPI")],
)
async def test_receive_input_search_exact_match(mock_search):
    text = "삼성전자\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == SECTOR_INPUT  # 신규 → 섹터 입력

    sector_update, _ = _make_update_and_context("반도체")
    result = await _sector_input(sector_update, context)
    assert result == THESIS_INPUT

    thesis_update, _ = _make_update_and_context("테스트")
    result = await _thesis_input(thesis_update, context)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["ticker"] == "005930.KS"


# ── 검색 결과 여러 개 → PICK_STOCK 상태 ──


@pytest.mark.asyncio
@patch(
    "bot.handlers.buy.search_stocks",
    return_value=[
        StockCandidate("삼성전자", "005930", "KOSPI"),
        StockCandidate("삼성전기", "009150", "KOSPI"),
        StockCandidate("삼성바이오로직스", "207940", "KOSPI"),
    ],
)
async def test_receive_input_multiple_candidates(mock_search):
    text = "삼성\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == 1  # PICK_STOCK

    assert "buy_input" in context.user_data
    reply_call = update.message.reply_text.call_args
    assert reply_call.kwargs.get("reply_markup") is not None


# ── 추가 매수 — 기존 섹터+근거 유지 ──


@pytest.mark.asyncio
async def test_additional_buy_keep_existing():
    save_ticker_map({"삼성전자": "005930.KS"})

    # 1차 매수 (신규)
    text1 = "삼성전자\n10주\n70000원"
    update1, context1 = _make_update_and_context(text1)
    result = await _receive_input(update1, context1)
    assert result == SECTOR_INPUT

    sector_update, _ = _make_update_and_context("반도체")
    await _sector_input(sector_update, context1)
    thesis_update, _ = _make_update_and_context("1차 매수")
    await _thesis_input(thesis_update, context1)

    # 2차 매수 (추가) → 기존 정보 확인
    text2 = "삼성전자\n10주\n80000원"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == EXISTING_CONFIRM

    # "그대로 유지" 선택
    cb_update = _make_callback_update("keep_existing")
    result = await _existing_confirm(cb_update, context2)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 20
    assert holdings[0]["avg_price"] == 75000
    assert holdings[0]["sector"] == "반도체"
    assert holdings[0]["buy_thesis"] == "1차 매수"


# ── 닉네임으로 매수 ──


@pytest.mark.asyncio
async def test_receive_input_with_nickname():
    save_nickname_map({"삼전": "삼성전자"})
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼전\n5주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == SECTOR_INPUT

    sector_update, _ = _make_update_and_context("반도체")
    await _sector_input(sector_update, context)
    thesis_update, _ = _make_update_and_context("테스트")
    await _thesis_input(thesis_update, context)

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"


# ── 영어 대소문자 무시 추가매수 ──


@pytest.mark.asyncio
async def test_receive_input_case_insensitive():
    save_ticker_map({"NVIDIA": "NVDA"})

    # 1차 매수
    text1 = "NVIDIA\n5주\n800원"
    update1, context1 = _make_update_and_context(text1)
    result = await _receive_input(update1, context1)
    assert result == SECTOR_INPUT
    sector1, _ = _make_update_and_context("AI")
    await _sector_input(sector1, context1)
    thesis1, _ = _make_update_and_context("1차")
    await _thesis_input(thesis1, context1)

    # 2차 매수 — 소문자
    text2 = "nvidia\n5주\n900원"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == EXISTING_CONFIRM

    cb_update = _make_callback_update("keep_existing")
    result = await _existing_confirm(cb_update, context2)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 10


# ── 종목명 공백 제거 ──


@pytest.mark.asyncio
async def test_stock_name_no_spaces():
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼성 전자\n5주\n72000원"
    update, context = _make_update_and_context(text)
    result = await _receive_input(update, context)

    # 공백이 제거되어 "삼성전자"로 매칭
    assert result == SECTOR_INPUT
    assert context.user_data["buy_input"].name == "삼성전자"


# ── 매수 사유 입력: 최근 사유 버튼 노출 + 직접 입력 + 버튼 선택 ──


def _seed_buy_transactions(name: str, theses: list[str]) -> None:
    """주어진 종목의 매수 거래 히스토리를 시드."""
    txs = []
    for i, thesis in enumerate(theses):
        txs.append({
            "id": f"tx-{i}",
            "type": "buy",
            "name": name,
            "sector": "테스트",
            "date": f"2026-04-0{i+1}T10:00:00",
            "price": 1000.0,
            "quantity": 1,
            "total_amount": 1000.0,
            "thesis": thesis,
            "research_notes": "",
        })
    save_transactions(txs)


def test_get_recent_reasons_buy_dedup():
    _seed_buy_transactions("삼성전자", ["사유A", "사유B", "사유A", "사유C"])
    reasons = get_recent_reasons("buy")
    # 최신순 + 중복 제거 (가장 최근 등장 순)
    assert reasons == ["사유C", "사유A", "사유B"]


def test_get_recent_reasons_is_global():
    """종목을 가리지 않고 전체 거래에서 최근 사유를 합산한다."""
    _seed_buy_transactions("삼성전자", ["사유A"])
    txs = load_transactions()
    txs.append({
        "id": "other",
        "type": "buy",
        "name": "다른종목",
        "sector": "",
        "date": "2026-05-01T10:00:00",
        "price": 1.0,
        "quantity": 1,
        "total_amount": 1.0,
        "thesis": "다른사유",
        "research_notes": "",
    })
    save_transactions(txs)
    # "다른사유"가 더 최신 → 맨 앞
    assert get_recent_reasons("buy") == ["다른사유", "사유A"]


def test_get_recent_reasons_pinned_always_first():
    """pinned 인자가 결과 맨 앞에 위치하고 중복은 제거된다."""
    save_transactions([
        {
            "id": "1",
            "type": "sell",
            "name": "삼성전자",
            "sector": "",
            "date": "2026-05-01T10:00:00",
            "price": 1.0,
            "quantity": 1,
            "total_amount": 1.0,
            "sell_reason": "익절",
        },
        {
            "id": "2",
            "type": "sell",
            "name": "삼성전자",
            "sector": "",
            "date": "2026-05-02T10:00:00",
            "price": 1.0,
            "quantity": 1,
            "total_amount": 1.0,
            "sell_reason": "자동손절",
        },
    ])
    reasons = get_recent_reasons("sell", pinned=["자동손절"])
    # pinned이 맨 앞, 그 다음 최근 사유에서 자동손절은 중복 제거됨
    assert reasons[0] == "자동손절"
    assert "익절" in reasons
    assert reasons.count("자동손절") == 1


@pytest.mark.asyncio
async def test_thesis_prompt_shows_recent_reasons_buttons():
    """신규 종목 매수 시 같은 이름의 과거 매수 사유가 버튼으로 노출."""
    save_ticker_map({"삼성전자": "005930.KS"})
    _seed_buy_transactions("삼성전자", ["사유A", "사유B"])

    text = "삼성전자\n10주\n72000원"
    update, context = _make_update_and_context(text)
    await _receive_input(update, context)

    sector_update, _ = _make_update_and_context("반도체")
    result = await _sector_input(sector_update, context)
    assert result == THESIS_INPUT

    # 마지막 reply_text에 reply_markup 있고 recent_reasons가 user_data에 저장.
    # 전역 최근 사유이므로 같은 종목의 과거 사유가 포함됨.
    last_call = sector_update.message.reply_text.call_args
    assert last_call.kwargs.get("reply_markup") is not None
    assert context.user_data["recent_reasons"] == ["사유B", "사유A"]


@pytest.mark.asyncio
async def test_thesis_pick_uses_clicked_reason():
    """매수 사유 버튼 클릭 시 해당 사유로 저장."""
    save_ticker_map({"삼성전자": "005930.KS"})
    _seed_buy_transactions("삼성전자", ["기존사유"])

    text = "삼성전자\n10주\n72000원"
    update, context = _make_update_and_context(text)
    await _receive_input(update, context)

    sector_update, _ = _make_update_and_context("반도체")
    await _sector_input(sector_update, context)

    # 인덱스 0번 버튼 클릭
    cb_update = _make_callback_update(f"{REASON_PICK_PREFIX}0")
    result = await _thesis_pick(cb_update, context)
    assert result == -1  # END

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["buy_thesis"] == "기존사유"


# ── 추가 매수: 기존 사유에 이어쓰기 (APPEND_THESIS) ──


@pytest.mark.asyncio
async def test_additional_buy_append_thesis():
    save_ticker_map({"삼성전자": "005930.KS"})

    # 1차 매수
    text1 = "삼성전자\n10주\n70000원"
    update1, context1 = _make_update_and_context(text1)
    await _receive_input(update1, context1)
    await _sector_input(_make_update_and_context("반도체")[0], context1)
    await _thesis_input(_make_update_and_context("AI 수요")[0], context1)

    # 2차 매수 → 기존 정보 확인
    text2 = "삼성전자\n10주\n80000원"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == EXISTING_CONFIRM

    # "매수사유 이어쓰기" 선택
    cb_update = _make_callback_update("append_thesis")
    result = await _existing_confirm(cb_update, context2)
    assert result == APPEND_INPUT
    assert context2.user_data["append_base"] == "AI 수요"

    # 이어쓸 텍스트 입력
    append_update, _ = _make_update_and_context("HBM 가속")
    result = await _append_input(append_update, context2)
    assert result == -1  # END

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["buy_thesis"] == "AI 수요\nHBM 가속"
