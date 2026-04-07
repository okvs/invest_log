import asyncio
import io
import logging
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

from telegram import Update
from telegram.ext import ContextTypes

from bot.formatters import format_dashboard, fetch_current_prices, format_number, _resolve_tickers
from bot.html_report import build_html_report
from parsers.input_parser import search_stocks
from storage.json_store import load_holdings, save_holdings, load_ticker_map, save_ticker_map

logger = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096

# 한글 폰트 설정
_KOREAN_FONT = None
for fname in fm.findSystemFonts():
    if any(k in fname for k in ["AppleGothic", "NanumGothic", "Malgun", "NotoSansCJK"]):
        _KOREAN_FONT = fname
        break

if _KOREAN_FONT:
    plt.rcParams["font.family"] = fm.FontProperties(fname=_KOREAN_FONT).get_name()
plt.rcParams["axes.unicode_minus"] = False


def _build_sector_chart(holdings: list[dict]):
    """섹터별 비중 가로 막대 차트를 생성하여 BytesIO로 반환."""
    active = [h for h in holdings if h.get("quantity", 0) > 0]
    if not active:
        return None

    # 현재가 기준 평가금 계산
    name_to_ticker, _ = _resolve_tickers(active)
    tickers = list(set(name_to_ticker.values()))
    current_prices = fetch_current_prices(tickers) if tickers else {}

    sector_eval: dict[str, float] = defaultdict(float)
    for h in active:
        name = h["name"]
        ticker = name_to_ticker.get(name, "")
        if ticker in current_prices:
            val = current_prices[ticker] * h["quantity"]
        else:
            val = h.get("total_invested", 0)
        sector_eval[h.get("sector", "기타")] += val

    total = sum(sector_eval.values())
    if total == 0:
        return None

    # 비중 내림차순 정렬
    sorted_sectors = sorted(sector_eval.items(), key=lambda x: x[1], reverse=True)
    sectors = [s for s, _ in sorted_sectors]
    values = [v for _, v in sorted_sectors]
    pcts = [v / total * 100 for v in values]

    # 차트 생성
    colors = ["#4A90D9", "#E74C3C", "#2ECC71", "#F39C12", "#9B59B6",
              "#1ABC9C", "#E67E22", "#3498DB", "#E91E63", "#00BCD4"]

    fig, ax = plt.subplots(figsize=(8, max(3, len(sectors) * 0.7)))
    bars = ax.barh(range(len(sectors)), pcts, color=[colors[i % len(colors)] for i in range(len(sectors))])

    ax.set_yticks(range(len(sectors)))
    ax.set_yticklabels(sectors, fontsize=12)
    ax.invert_yaxis()
    ax.set_xlabel("비중 (%)", fontsize=11)
    ax.set_title("섹터별 비중", fontsize=14, fontweight="bold", pad=12)

    # 막대 위에 비중 + 금액 표시
    for i, (bar, pct, val) in enumerate(zip(bars, pcts, values)):
        label = f" {pct:.1f}%  ({format_number(val)}원)"
        ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                label, va="center", fontsize=10)

    ax.set_xlim(0, max(pcts) * 1.5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


async def _backfill_missing_tickers(holdings_data: list[dict]) -> list[str]:
    """ticker가 없는 보유 종목을 검색하여 자동 보정. 보정된 종목명 리스트 반환."""
    missing = [
        h for h in holdings_data
        if h.get("quantity", 0) > 0 and not h.get("ticker", "")
    ]
    if not missing:
        return []

    tmap = load_ticker_map()
    filled: list[str] = []

    for h in missing:
        name = h["name"]
        # ticker_map 캐시 먼저 확인
        cached = tmap.get(name, "")
        if not cached:
            for k, v in tmap.items():
                if k.lower() == name.lower():
                    cached = v
                    break
        if cached:
            h["ticker"] = cached
            filled.append(name)
            continue

        # Playwright로 검색
        try:
            candidates = await asyncio.to_thread(search_stocks, name)
            exact = [c for c in candidates if c.name == name]
            if exact:
                suffix = ".KQ" if exact[0].market == "KOSDAQ" else ".KS"
                ticker = exact[0].code + suffix
                h["ticker"] = ticker
                tmap[name] = ticker
                filled.append(name)
        except Exception:
            logger.warning("ticker 보정 실패: %s", name, exc_info=True)

    if filled:
        save_holdings(holdings_data)
        save_ticker_map(tmap)

    return filled


def _merge_duplicate_holdings(holdings: list[dict]) -> tuple[list[dict], bool]:
    """같은 종목명의 보유 종목을 합쳐서 반환. (merged_holdings, changed)"""
    from collections import OrderedDict

    grouped: dict[str, list[int]] = {}
    for i, h in enumerate(holdings):
        name = h.get("name", "")
        if name not in grouped:
            grouped[name] = []
        grouped[name].append(i)

    changed = False
    for name, indices in grouped.items():
        if len(indices) <= 1:
            continue
        # 활성(quantity>0) 항목끼리만 합침
        active_indices = [i for i in indices if holdings[i].get("quantity", 0) > 0]
        if len(active_indices) <= 1:
            continue

        changed = True
        base = holdings[active_indices[0]]
        for idx in active_indices[1:]:
            dup = holdings[idx]
            base_qty = base.get("quantity", 0)
            dup_qty = dup.get("quantity", 0)
            base_invested = base.get("total_invested", 0)
            dup_invested = dup.get("total_invested", 0)

            new_qty = base_qty + dup_qty
            new_invested = base_invested + dup_invested
            new_avg = round(new_invested / new_qty) if new_qty > 0 else 0

            base["quantity"] = new_qty
            base["total_invested"] = new_invested
            base["avg_price"] = new_avg

            # ticker, sector 등 빈 값이면 채워줌
            if not base.get("ticker") and dup.get("ticker"):
                base["ticker"] = dup["ticker"]
            if not base.get("sector") and dup.get("sector"):
                base["sector"] = dup["sector"]
            if not base.get("buy_thesis") and dup.get("buy_thesis"):
                base["buy_thesis"] = dup["buy_thesis"]

            # transaction_ids 합침
            base_tids = base.get("transaction_ids", [])
            dup_tids = dup.get("transaction_ids", [])
            base["transaction_ids"] = base_tids + dup_tids

            # 중복 항목 수량 0으로 (사실상 삭제)
            dup["quantity"] = 0
            dup["total_invested"] = 0

    return holdings, changed


async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """보유 종목 현황 대시보드를 전송한다."""
    holdings = load_holdings()

    # 같은 종목명 중복 합치기
    holdings, merged = _merge_duplicate_holdings(holdings)
    if merged:
        save_holdings(holdings)

    active = [h for h in holdings if h.get("quantity", 0) > 0]

    if not active:
        await update.message.reply_text("보유 종목이 없습니다.")
        return

    # ticker 없는 종목 자동 보정
    filled = await _backfill_missing_tickers(holdings)
    if filled:
        await update.message.reply_text(
            "종목코드 자동 보정:\n" + "\n".join(f"  {n} → {next(h['ticker'] for h in holdings if h['name'] == n)}" for n in filled)
        )
        # 보정된 데이터로 다시 로드
        holdings = load_holdings()

    # HTML 리포트 전송
    html_file = build_html_report(holdings)
    await update.message.reply_document(document=html_file, caption="상세 리포트 (브라우저에서 열기)")
