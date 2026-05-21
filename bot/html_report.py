"""투자 현황 HTML 리포트 생성."""
from __future__ import annotations

import io
import math
from collections import defaultdict
from datetime import datetime

from bot.formatters import fetch_current_quotes, format_number, _resolve_tickers
from bot.futures_report import build_futures_section


SIZE_TABLE = {1: 40, 2: 120, 3: 280, 4: 280, 5: 280}
MARKER_TABLE = {1: "o", 2: "o", 3: "o", 4: "h", 5: "*"}
SVG_RADIUS = {1: 5, 2: 9, 3: 13, 4: 13, 5: 14}


def _tier_for_weight(weight_pct: float) -> int:
    """비중(%)로부터 tier(1~5)를 계산. T6+ 는 T5로 묶는다."""
    return min(int(weight_pct // 10) + 1, 5)


def _hexagon_points(cx: float, cy: float, r: float) -> str:
    pts = []
    for i in range(6):
        angle = math.pi / 3 * i + math.pi / 6
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        pts.append(f"{x:.2f},{y:.2f}")
    return " ".join(pts)


def _star_points(cx: float, cy: float, r_out: float, r_in: float) -> str:
    pts = []
    for i in range(10):
        angle = -math.pi / 2 + math.pi / 5 * i
        r = r_out if i % 2 == 0 else r_in
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        pts.append(f"{x:.2f},{y:.2f}")
    return " ".join(pts)


def _text_width_est(text: str, font_px: int = 13) -> float:
    """간이 SVG 텍스트 폭 추정. CJK 1글자 ≈ font_px·1.05, ASCII ≈ font_px·0.6."""
    w = 0.0
    for ch in text:
        if ch == " ":
            w += font_px * 0.35
        elif ord(ch) > 127 and ch not in "·":
            w += font_px * 1.05
        else:
            w += font_px * 0.6
    return w


def _quadrant_badge(
    anchor_x: float,
    anchor_y: float,
    text: str,
    bg: str,
    fg: str,
    side: str,  # "tl" / "tr" / "bl" / "br"
) -> str:
    """사분면 모서리에 붙는 이름표(배지) SVG. anchor_x/y 는 사분면 외곽 모서리 좌표."""
    pad_x = 10
    h = 26
    font_px = 13
    w = _text_width_est(text, font_px) + pad_x * 2
    if side in ("tr", "br"):
        rx = anchor_x - w
        text_anchor = "end"
        tx = anchor_x - pad_x
    else:
        rx = anchor_x
        text_anchor = "start"
        tx = anchor_x + pad_x
    if side in ("tl", "tr"):
        ry = anchor_y
    else:
        ry = anchor_y - h
    ty = ry + h / 2 + 4  # text baseline 보정
    return (
        f'<rect x="{rx:.2f}" y="{ry:.2f}" width="{w:.2f}" height="{h}" '
        f'rx="6" ry="6" fill="{bg}" fill-opacity="0.92"/>'
        f'<text x="{tx:.2f}" y="{ty:.2f}" font-size="{font_px}" fill="{fg}" '
        f'text-anchor="{text_anchor}" font-weight="700">{text}</text>'
    )


def _wave_glyph(cx: float, cy: float, color: str = "#fbbf24") -> str:
    """수평 사인 물결(∿) 글리프 — cubic Bezier 4 반파, 폭 18px, 진폭 ~4.5px, 좌우대칭."""
    return (
        f'<path d="M {cx - 9:.2f},{cy:.2f} '
        f'c 1.5,-6 3,-6 4.5,0 s 3,6 4.5,0 s 3,-6 4.5,0 s 3,6 4.5,0" '
        f'stroke="{color}" stroke-width="1.8" fill="none" stroke-linecap="round"/>'
    )


def _wave_glyph_v(cx: float, cy: float, color: str = "#fbbf24") -> str:
    """수직 사인 물결 글리프 — 위→아래 cubic Bezier 4 반파, 높이 18px, 진폭 ~4.5px."""
    return (
        f'<path d="M {cx:.2f},{cy - 9:.2f} '
        f'c -6,1.5 -6,3 0,4.5 s 6,3 0,4.5 s -6,3 0,4.5 s 6,3 0,4.5" '
        f'stroke="{color}" stroke-width="1.8" fill="none" stroke-linecap="round"/>'
    )


def _tier_marker_svg(cx: float, cy: float, tier: int, color: str) -> str:
    marker = MARKER_TABLE[tier]
    r = SVG_RADIUS[tier]
    common = f'fill="{color}" stroke="#0f0f14" stroke-width="1" fill-opacity="0.88"'
    if marker == "o":
        return f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" {common}/>'
    if marker == "h":
        return f'<polygon points="{_hexagon_points(cx, cy, r)}" {common}/>'
    if marker == "*":
        return f'<polygon points="{_star_points(cx, cy, r, r * 0.45)}" {common}/>'
    return f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" {common}/>'


def _build_quadrants_svg(
    rows: list[dict], sector_colors: dict[str, str], total_eval: float
) -> str:
    """수익률(X) × 비중(Y) 4사분면 산점도를 inline SVG 로 그린다.

    분리선: 수익률 X=0, 비중 Y=10% (비중부족 기준). X축은 ±50% 대칭, Y축은
    0~max 비대칭 (바닥이 0%). 50% 초과 종목은 plot 좌우 끝에 마커를 찍고
    세로 물결로 클립 신호 + (±NN%) 실측 라벨. 점 크기/모양은 SIZE_TABLE/
    MARKER_TABLE (10% tier). 범례는 T1~T5 전부 표시.
    """
    if not rows or total_eval <= 0:
        return ""

    points = []
    for r in rows:
        weight = r["eval"] / total_eval * 100
        ret = r["pnl_pct"]
        tier = _tier_for_weight(weight)
        points.append({
            "name": r["name"],
            "weight": weight,
            "return": ret,
            "tier": tier,
            "color": sector_colors.get(r["sector"], "#9ca3af"),
        })

    # SVG 영역 — 정사각형 plot
    W, H = 720, 720
    pad_l, pad_r, pad_t, pad_b = 70, 36, 40, 60
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    # X축: 수익률(%). ±50% 고정, 그 너머는 클립 + 세로 물결 표시.
    x_abs = 50.0

    # Y축: 비중(%). 0 ~ ceil((max_w+10)/10)*10 (최소 20). 비대칭.
    Y_THRESH = 10.0  # 비중부족/과체중 분리선
    max_w = max(p["weight"] for p in points)
    y_max = max(math.ceil((max_w + 10.0) / 10.0) * 10.0, 20.0)
    y_min = 0.0

    x_min, x_max = -x_abs, x_abs

    def sx(x: float) -> float:
        return pad_l + (x - x_min) / (x_max - x_min) * plot_w

    def sy(y: float) -> float:
        return pad_t + (y_max - y) / (y_max - y_min) * plot_h

    ox = sx(0)  # X축 0 (수익률 분리선)
    oty = sy(Y_THRESH)  # Y=10% (비중 분리선)
    x_left, x_right = pad_l, pad_l + plot_w
    y_top, y_bottom = pad_t, pad_t + plot_h

    parts: list[str] = []
    parts.append(
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="background:#0f0f14;width:100%;max-width:{W}px;height:auto;display:block;margin:0 auto;">'
    )

    # 사분면 배경 — invset_mind 색감
    parts.append(
        f'<rect x="{ox}" y="{y_top}" width="{x_right - ox}" height="{oty - y_top}" '
        f'fill="#ef4444" fill-opacity="0.11"/>'
    )  # Q1 top-right · 잘하는 것 · 연빨강
    parts.append(
        f'<rect x="{x_left}" y="{y_top}" width="{ox - x_left}" height="{oty - y_top}" '
        f'fill="#6366f1" fill-opacity="0.13"/>'
    )  # Q2 top-left · 큰 위험 · 연파랑/라벤더
    parts.append(
        f'<rect x="{x_left}" y="{oty}" width="{ox - x_left}" height="{y_bottom - oty}" '
        f'fill="#22c55e" fill-opacity="0.10"/>'
    )  # Q3 bottom-left · 다행 · 연초록
    parts.append(
        f'<rect x="{ox}" y="{oty}" width="{x_right - ox}" height="{y_bottom - oty}" '
        f'fill="#fbbf24" fill-opacity="0.11"/>'
    )  # Q4 bottom-right · 비중 부족 · 연노랑

    # 격자 (10% 단위)
    ticks_x = list(range(-int(x_abs), int(x_abs) + 1, 10))
    ticks_y = list(range(0, int(y_max) + 1, 10))
    for tx in ticks_x:
        px = sx(tx)
        parts.append(
            f'<line x1="{px:.2f}" y1="{y_top}" x2="{px:.2f}" y2="{y_bottom}" '
            f'stroke="#1a1a24" stroke-width="1"/>'
        )
    for ty in ticks_y:
        py = sy(ty)
        parts.append(
            f'<line x1="{x_left}" y1="{py:.2f}" x2="{x_right}" y2="{py:.2f}" '
            f'stroke="#1a1a24" stroke-width="1"/>'
        )

    # 사분면 구분선: X=0 (수익률), Y=10% (비중)
    parts.append(
        f'<line x1="{ox:.2f}" y1="{y_top}" x2="{ox:.2f}" y2="{y_bottom}" '
        f'stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5,4"/>'
    )
    parts.append(
        f'<line x1="{x_left}" y1="{oty:.2f}" x2="{x_right}" y2="{oty:.2f}" '
        f'stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5,4"/>'
    )

    # 외곽 프레임
    parts.append(
        f'<rect x="{x_left}" y="{y_top}" width="{plot_w}" height="{plot_h}" '
        f'fill="none" stroke="#2a2a3a" stroke-width="1"/>'
    )

    # X 눈금 텍스트 — 수익률은 부호 있는 표준 표기
    for tx in ticks_x:
        if tx == 0:
            label = "0%"
        elif tx > 0:
            label = f"+{tx}%"
        else:
            label = f"{tx}%"
        parts.append(
            f'<text x="{sx(tx):.2f}" y="{y_bottom + 14}" font-size="10" '
            f'fill="#888" text-anchor="middle">{label}</text>'
        )
    # Y 눈금 텍스트 — 0% 바닥, 위로만 양수
    for ty in ticks_y:
        parts.append(
            f'<text x="{x_left - 6}" y="{sy(ty) + 3:.2f}" font-size="10" '
            f'fill="#888" text-anchor="end">{ty}%</text>'
        )

    # 축 제목
    parts.append(
        f'<text x="{x_left + plot_w / 2:.2f}" y="{H - 8}" font-size="11" '
        f'fill="#aaa" text-anchor="middle">수익률 (%)</text>'
    )
    mid_y = pad_t + plot_h / 2
    parts.append(
        f'<text x="16" y="{mid_y:.2f}" font-size="11" fill="#aaa" text-anchor="middle" '
        f'transform="rotate(-90 16 {mid_y:.2f})">비중 (%)</text>'
    )

    # 사분면 이름표(배지)
    badge_inset = 4
    parts.append(_quadrant_badge(
        anchor_x=x_right - badge_inset, anchor_y=y_top + badge_inset,
        text="잘하는 것 (비중↑·수익)",
        bg="#ef4444", fg="#ffffff", side="tr",
    ))
    parts.append(_quadrant_badge(
        anchor_x=x_left + badge_inset, anchor_y=y_top + badge_inset,
        text="큰 위험 (비중↑·손실)",
        bg="#6366f1", fg="#ffffff", side="tl",
    ))
    parts.append(_quadrant_badge(
        anchor_x=x_left + badge_inset, anchor_y=y_bottom - badge_inset,
        text="다행 (비중↓·손실)",
        bg="#22c55e", fg="#0f0f14", side="bl",
    ))
    parts.append(_quadrant_badge(
        anchor_x=x_right - badge_inset, anchor_y=y_bottom - badge_inset,
        text="비중 부족 (비중↓·수익)",
        bg="#fbbf24", fg="#0f0f14", side="br",
    ))

    # 데이터 점 — 큰 마커가 먼저 그려져서 작은 마커가 위에 오도록 tier desc 정렬
    WAVE_COLOR = "#fbbf24"
    for p in sorted(points, key=lambda d: (-d["tier"], -d["weight"])):
        r = SVG_RADIUS[p["tier"]]
        clipped = abs(p["return"]) > 50.0
        cy = sy(p["weight"])
        if clipped:
            direction = 1 if p["return"] > 0 else -1
            # 마커를 plot 좌/우 끝에 바짝 붙임 (프레임과 r+2 간격).
            cx = (x_right - r - 2) if direction > 0 else (x_left + r + 2)
        else:
            cx = sx(p["return"])
        parts.append(_tier_marker_svg(cx, cy, p["tier"], p["color"]))

        if clipped:
            direction = 1 if p["return"] > 0 else -1
            # 세로 물결 — 마커 안쪽에 세로 방향으로
            parts.append(
                _wave_glyph_v(cx - direction * (r + 8), cy, WAVE_COLOR)
            )
            # 라벨 — 물결 너머 안쪽. 실제 수익률 % 함께.
            label_x = cx - direction * (r + 18)
            anchor = "end" if direction > 0 else "start"
            label_html = (
                f'{p["name"]} <tspan fill="{WAVE_COLOR}" font-weight="700" '
                f'font-size="10">({p["return"]:+.0f}%)</tspan>'
            )
        else:
            label_x = cx + r + 4
            anchor = "start"
            label_html = p["name"]

        parts.append(
            f'<text x="{label_x:.2f}" y="{cy + 3:.2f}" font-size="11" '
            f'fill="#e0e0e0" text-anchor="{anchor}">{label_html}</text>'
        )

    parts.append("</svg>")

    # 범례 (T1~T5 항상 표시)
    legend_specs = [
        (1, "T1 (<10%)"),
        (2, "T2 (10–20%)"),
        (3, "T3 (20–30%)"),
        (4, "T4 (30–40%)"),
        (5, "T5 (≥40%)"),
    ]
    legend_items = []
    for tier, label in legend_specs:
        size = 36
        chip = (
            f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
            f'style="vertical-align:middle">'
            + _tier_marker_svg(size / 2, size / 2, tier, "#888")
            + "</svg>"
        )
        legend_items.append(
            f'<div class="qd-legend-item">{chip}<span>{label}</span></div>'
        )
    legend_html = (
        '<div class="qd-legend">' + "".join(legend_items) + "</div>"
    )

    caption = (
        '<div class="qd-caption">점 크기 = 비중 tier (10% 단위) · '
        "원/원/원/육각형/별 (T1·T2·T3·T4·T5)</div>"
    )

    return (
        '<div class="section-title" style="margin-top:32px">비중 × 수익률 4사분면</div>'
        '<div class="qd-wrap">'
        + "".join(parts)
        + legend_html
        + caption
        + "</div>"
    )


def _format_man(n: float) -> str:
    """만 단위로 표시. 만 미만 절삭."""
    man = int(n // 10000)
    return f"{man:,}만"


def build_html_report(
    holdings: list[dict],
    title: str = "투자 현황",
    initial_capital: float | None = None,
    show_cash: bool = False,
    cash_override: float | None = None,
    futures_positions: list[dict] | None = None,
    futures_prices: dict[str, float] | None = None,
) -> io.BytesIO:
    """보유 종목 현황을 HTML 파일로 생성.

    Args:
        holdings: 보유 종목 리스트
        title: HTML 헤더 제목
        initial_capital: 초기자본 (show_cash=True일 때 사용)
        show_cash: True면 잔여현금 카드를 추가로 표시
        cash_override: 직접 관리하는 예수금 값 (None이면 initial_capital - total_invested로 계산)
    """
    active = [h for h in holdings if h.get("quantity", 0) > 0]

    # 현재가 조회 (오늘 등락률 포함)
    name_to_ticker, missing = _resolve_tickers(active)
    tickers = list(set(name_to_ticker.values()))
    quotes = fetch_current_quotes(tickers) if tickers else {}

    # 종목별 데이터 계산
    rows = []
    for h in active:
        name = h["name"]
        qty = h["quantity"]
        avg = h["avg_price"]
        invested = h["total_invested"]
        ticker = name_to_ticker.get(name, "")
        q = quotes.get(ticker) or {}
        cur_price = q.get("price")
        change_pct = q.get("change_pct")

        if cur_price is not None:
            eval_amt = cur_price * qty
            pnl = eval_amt - invested
            pnl_pct = (pnl / invested * 100) if invested else 0
        else:
            eval_amt = invested
            pnl = 0
            pnl_pct = 0

        rows.append({
            "name": name,
            "sector": h.get("sector", "기타"),
            "qty": qty,
            "avg": avg,
            "invested": invested,
            "cur_price": cur_price,
            "change_pct": change_pct,
            "eval": eval_amt,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "thesis": h.get("buy_thesis", ""),
            "date": h.get("buy_date", ""),
        })

    # 평가금 내림차순 기본 정렬
    rows.sort(key=lambda r: r["eval"], reverse=True)

    total_invested = sum(r["invested"] for r in rows)
    total_eval = sum(r["eval"] for r in rows)
    total_pnl = total_eval - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0

    # 현금 카드 계산 (섹터 비중에 포함시키기 위해 미리 계산)
    if cash_override is not None:
        cash_remaining = cash_override
    else:
        cash_remaining = (initial_capital - total_invested) if initial_capital is not None else 0

    # 섹터별 집계 — 현물 평가금 + 선물 명목금(현재가×계약×승수) + 현금
    sector_data: dict[str, float] = defaultdict(float)
    sector_futures: dict[str, float] = defaultdict(float)  # 선물 부분만 별도 추적
    for r in rows:
        sector_data[r["sector"]] += r["eval"]

    # 선물 포지션을 섹터에 합산
    for fp in futures_positions or []:
        if fp.get("contracts", 0) <= 0:
            continue
        sector = fp.get("sector", "") or "기타"
        sym = fp.get("symbol", "")
        cm = fp.get("contract_month", "")
        key = f"{sym}|{cm}"
        q = (futures_prices or {}).get(key) or (futures_prices or {}).get(sym) or {}
        price = q.get("price") if isinstance(q, dict) else q
        if price is None:
            price = float(fp.get("avg_entry_price", 0))
        notional = float(price) * int(fp.get("contracts", 0)) * int(fp.get("multiplier", 10))
        sector_data[sector] += notional
        sector_futures[sector] += notional

    if show_cash and cash_remaining > 0:
        sector_data["현금"] += cash_remaining
    sector_sorted = sorted(sector_data.items(), key=lambda x: x[1], reverse=True)
    sector_total = sum(v for _, v in sector_sorted)

    # 섹터별 색상 — 현금은 회색 고정
    colors = ["#4A90D9", "#E74C3C", "#2ECC71", "#F39C12", "#9B59B6",
              "#1ABC9C", "#E67E22", "#3498DB", "#E91E63", "#00BCD4"]
    sector_colors: dict[str, str] = {}
    color_idx = 0
    for s, _ in sector_sorted:
        if s == "현금":
            sector_colors[s] = "#6b7280"
        else:
            sector_colors[s] = colors[color_idx % len(colors)]
            color_idx += 1

    now = datetime.now().strftime("%Y.%m.%d %H:%M")

    # 종목 행 HTML
    stock_rows_html = ""
    for r in rows:
        pnl_class = "profit" if r["pnl"] >= 0 else "loss"
        pnl_sign = "+" if r["pnl"] >= 0 else ""
        dot_color = sector_colors.get(r["sector"], "#999")

        if r["cur_price"] is not None:
            cur_display = f'{format_number(int(r["cur_price"]))}원'
            cp = r.get("change_pct")
            if cp is not None:
                chg_class = "profit" if cp >= 0 else "loss"
                chg_sign = "+" if cp >= 0 else ""
                cur_display += f'<br><small class="{chg_class}">({chg_sign}{cp:.2f}%)</small>'
        else:
            cur_display = "-"

        cur_raw = r["cur_price"] if r["cur_price"] is not None else 0
        stock_rows_html += f"""
        <tr data-name="{r["name"]}" data-sector="{r["sector"]}" data-qty="{r["qty"]}"
            data-avg="{r["avg"]}" data-cur="{cur_raw}" data-invested="{r["invested"]}"
            data-eval="{r["eval"]}" data-pnl="{r["pnl"]}" data-pnlpct="{r["pnl_pct"]:.2f}">
          <td><span class="dot" style="background:{dot_color}"></span>{r["sector"]}</td>
          <td>{r["name"]}</td>
          <td class="num">{_format_man(r["eval"])}</td>
          <td class="thesis">{r["thesis"]}</td>
          <td class="num {pnl_class}">{pnl_sign}{format_number(int(r["pnl"]))}원<br><small>{pnl_sign}{int(r["pnl_pct"])}%</small></td>
          <td class="num">{cur_display}</td>
          <td class="num">{format_number(int(r["avg"]))}원</td>
          <td class="num">{r["qty"]}주</td>
          <td class="num">{format_number(int(r["invested"]))}원</td>
        </tr>"""

    # 섹터별 한 줄짜리 막대를 만들 row 리스트 구성.
    # 정렬 규칙: 섹터 단위 합계 내림차순으로 가되, 같은 섹터에 현물·선물이 둘 다 있으면
    # 현물 바로 아래에 선물(빗금) 행을 붙임. sector_sorted 가 이미 합계 내림차순.
    detail_rows: list[tuple[str, float, str, bool]] = []
    # (label, value, color, is_futures)
    for sector, val in sector_sorted:
        fut_val = sector_futures.get(sector, 0)
        spot_val = val - fut_val
        color = sector_colors[sector]
        if spot_val > 0:
            detail_rows.append((sector, spot_val, color, False))
        if fut_val > 0:
            detail_rows.append((f"{sector}(선물)", fut_val, color, True))

    max_row_val = max((r[1] for r in detail_rows), default=0)
    sector_bars_html = ""
    for label, val, color, is_futures in detail_rows:
        pct = (val / sector_total * 100) if sector_total else 0
        bar_width = (val / max_row_val * 100) if max_row_val else 0
        bar_class = "sector-bar futures-stripe" if is_futures else "sector-bar"
        # background-color 명시 — `background:` 단축형은 background-image(빗금)를 초기화해 버림
        sector_bars_html += f"""
        <div class="sector-row">
          <div class="sector-label">{label}</div>
          <div class="sector-bar-wrap">
            <div class="{bar_class}" style="width:{bar_width}%;background-color:{color}"></div>
          </div>
          <div class="sector-val">{pct:.1f}% <span class="sector-amt">{format_number(int(val))}원</span></div>
        </div>"""

    # 스택바 (섹터 비중 한 줄) — 같은 섹터 안에서 현물/선물을 인접 두 조각으로 분리
    stack_segments = ""
    for sector, val in sector_sorted:
        pct = (val / sector_total * 100) if sector_total else 0
        color = sector_colors[sector]
        fut_val = sector_futures.get(sector, 0)
        spot_val = val - fut_val
        spot_pct = (spot_val / sector_total * 100) if sector_total else 0
        fut_pct = (fut_val / sector_total * 100) if sector_total else 0

        # 레이블은 면적이 큰 쪽에 표시 (보통 둘을 합쳐 한 섹터로 인식하게)
        label_seg = "spot" if spot_pct >= fut_pct else "futures"
        label_html = (
            f'<span>{sector}<br>{pct:.0f}%</span>' if pct >= 3 else ''
        )

        if spot_pct > 0:
            seg_label = label_html if label_seg == "spot" else ""
            stack_segments += (
                f'<div class="stack-seg" style="width:{spot_pct}%;background:{color}" '
                f'title="{sector} 현물 {spot_pct:.1f}%">{seg_label}</div>'
            )
        if fut_pct > 0:
            seg_label = label_html if label_seg == "futures" else ""
            stack_segments += (
                f'<div class="stack-seg futures-stripe" '
                f'style="width:{fut_pct}%;background-color:{color}" '
                f'title="{sector} 선물 {fut_pct:.1f}%">{seg_label}</div>'
            )

    pnl_class = "profit" if total_pnl >= 0 else "loss"
    pnl_sign = "+" if total_pnl >= 0 else ""

    # 총 수익은 테이블 손익 합(total_pnl) 기준 — 신용대출을 total_invested에 포함시켜도 일치함
    total_return = total_pnl
    total_return_pct = (total_pnl / initial_capital * 100) if initial_capital else total_pnl_pct
    return_class = "profit" if total_return >= 0 else "loss"
    return_sign = "+" if total_return >= 0 else ""

    # 배지 HTML (Claude 리포트 구분용)
    badge_html = ""
    if show_cash:
        badge_html = '<div class="badge" style="display:inline-block;background:#4A90D9;color:#fff;font-size:11px;padding:3px 10px;border-radius:12px;margin-top:6px;letter-spacing:1px;">AI vs Human Battle</div>'

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — {now}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background:#0f0f14; color:#e0e0e0; padding:24px; }}

  .header {{ text-align:center; margin-bottom:32px; }}
  .header h1 {{ font-size:22px; font-weight:700; color:#fff; }}
  .header .date {{ font-size:13px; color:#888; margin-top:4px; }}

  /* 요약 카드 */
  .cards {{ display:grid; grid-template-columns:repeat({'4' if show_cash else '3'},1fr); gap:12px; margin-bottom:32px; max-width:{'800' if show_cash else '600'}px; margin-left:auto; margin-right:auto; }}
  .card {{ background:#1a1a24; border-radius:12px; padding:16px 12px; text-align:center; }}
  .card .label {{ font-size:11px; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; }}
  .card .value {{ font-size:18px; font-weight:700; }}
  .card .sub {{ font-size:12px; margin-top:4px; }}
  @media (max-width: 600px) {{
    .cards {{ grid-template-columns:{'repeat(2,1fr)' if show_cash else '1fr'}; max-width:100%; }}
    .card .value {{ font-size:{'16' if show_cash else '20'}px; }}
  }}
  .profit {{ color:#22c55e; }}
  .loss {{ color:#ef4444; }}

  /* 스택바 */
  .stack {{ display:flex; height:36px; border-radius:8px; overflow:hidden; margin-bottom:32px; }}
  .stack-seg {{ display:flex; align-items:center; justify-content:center; color:#fff; font-size:10px;
                font-weight:600; text-align:center; line-height:1.2; min-width:0; overflow:hidden; }}
  .stack-seg span {{ white-space:nowrap; }}

  /* 섹터 상세 */
  .section-title {{ font-size:15px; font-weight:700; color:#fff; margin-bottom:16px;
                    padding-bottom:8px; border-bottom:1px solid #2a2a3a; }}
  .sector-row {{ display:flex; align-items:center; margin-bottom:10px; }}
  .sector-label {{ width:120px; font-size:13px; font-weight:600; flex-shrink:0; }}
  .sector-bar-wrap {{ width:200px; height:20px; background:#1a1a24; border-radius:4px; overflow:hidden; margin:0 12px; flex-shrink:0; }}
  .sector-bar {{ height:100%; }}
  .sector-val {{ font-size:13px; font-weight:600; white-space:nowrap; }}
  .sector-amt {{ color:#888; font-weight:400; margin-left:6px; }}
  .futures-tag {{ color:#aaa; font-size:11px; }}
  .futures-tag-strong {{ color:#ddd; font-weight:600; }}
  .sector-breakdown {{ font-size:11px; color:#888; margin-top:2px; font-weight:400; }}
  /* 선물 영역 빗금 오버레이 — 섹터 색은 그대로 유지하고 그 위에 줄무늬 */
  .futures-stripe {{
    background-image: repeating-linear-gradient(
      135deg,
      rgba(255,255,255,0.30) 0,
      rgba(255,255,255,0.30) 3px,
      transparent 3px,
      transparent 8px
    );
  }}
  @media (max-width: 480px) {{
    .sector-bar-wrap {{ width:120px; }}
  }}

  /* 종목 테이블 */
  .table-wrap {{ margin-top:32px; overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ background:#1a1a24; color:#888; font-size:11px; text-transform:uppercase; letter-spacing:1px;
        padding:10px 12px; text-align:left; white-space:nowrap; position:sticky; top:0; cursor:pointer; user-select:none; }}
  th:hover {{ color:#fff; }}
  th .arrow {{ font-size:10px; margin-left:4px; color:#555; }}
  th.sorted .arrow {{ color:#4A90D9; }}
  td {{ padding:10px 12px; border-bottom:1px solid #1a1a24; vertical-align:middle; }}
  tr:hover td {{ background:#1a1a24; }}
  .num {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
  .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px; }}
  .thesis {{ color:#888; font-size:12px; min-width:210px; max-width:340px; }}

  /* 4사분면 산점도 */
  .qd-wrap {{ margin-top:8px; margin-bottom:32px; }}
  .qd-legend {{ display:flex; flex-wrap:wrap; justify-content:center; gap:18px; margin-top:12px; }}
  .qd-legend-item {{ display:flex; align-items:center; gap:6px; font-size:12px; color:#bbb; }}
  .qd-caption {{ text-align:center; font-size:11px; color:#888; margin-top:8px; }}

  /* 미등록 알림 */
  .warning {{ margin-top:24px; background:#2a2215; border:1px solid #665520; border-radius:8px; padding:16px; font-size:13px; color:#fbbf24; }}
</style>
</head>
<body>
  <div class="header">
    <h1>{title}</h1>
    {badge_html}
    <div class="date">{now} 기준</div>
  </div>

  <div class="cards">
    {"<div class='card'><div class='label'>초기자본</div><div class='value'>" + format_number(int(initial_capital)) + "원</div></div>" if show_cash and initial_capital else ""}
    {"<div class='card'><div class='label'>잔여 현금</div><div class='value'>" + format_number(int(cash_remaining)) + "원</div></div>" if show_cash and initial_capital else "<div class='card'><div class='label'>총 투자금</div><div class='value'>" + format_number(int(total_invested)) + "원</div></div>"}
    <div class="card">
      <div class="label">총 평가금</div>
      <div class="value">{format_number(int(total_eval))}원</div>
    </div>
    <div class="card">
      <div class="label">총 수익</div>
      <div class="value {return_class if show_cash else pnl_class}">{(return_sign + format_number(int(total_return)) + '원') if show_cash else (pnl_sign + format_number(int(total_pnl)) + '원')}</div>
      <div class="sub {return_class if show_cash else pnl_class}">{(return_sign + f'{total_return_pct:.1f}%') if show_cash else (pnl_sign + f'{int(total_pnl_pct)}%')}</div>
    </div>
  </div>

  <div class="stack">{stack_segments}</div>
  {('<div style="font-size:11px;color:#888;margin:-24px 0 32px;display:flex;align-items:center;gap:6px"><span class="stripe-chip" style="display:inline-block;width:14px;height:14px;border-radius:3px;background-color:#888;background-image:repeating-linear-gradient(135deg,rgba(255,255,255,0.30) 0,rgba(255,255,255,0.30) 3px,transparent 3px,transparent 8px)"></span>빗금 = 선물 명목 노출 (계약수 × 현재가 × 승수)</div>') if any(sector_futures.values()) else ''}

  {_build_quadrants_svg(rows, sector_colors, total_eval)}

  <div class="section-title">섹터별 비중</div>
  {sector_bars_html}

  <div class="table-wrap">
    <div class="section-title" style="margin-top:32px">보유 종목</div>
    <table>
      <thead>
        <tr>
          <th data-key="sector" data-type="str">섹터<span class="arrow">▲▼</span></th>
          <th data-key="name" data-type="str">종목<span class="arrow">▲▼</span></th>
          <th data-key="eval" data-type="num">평가금<span class="arrow">▲▼</span></th>
          <th>매수근거</th>
          <th data-key="pnl" data-type="num">수익<span class="arrow">▲▼</span></th>
          <th data-key="cur" data-type="num">현재가<span class="arrow">▲▼</span></th>
          <th data-key="avg" data-type="num">평균단가<span class="arrow">▲▼</span></th>
          <th data-key="qty" data-type="num">수량<span class="arrow">▲▼</span></th>
          <th data-key="invested" data-type="num">투자금<span class="arrow">▲▼</span></th>
        </tr>
      </thead>
      <tbody>{stock_rows_html}</tbody>
    </table>
  </div>

  {"<div class='warning'>⚠ 종목코드 미등록: " + ", ".join(missing) + "</div>" if missing else ""}

  {build_futures_section(futures_positions or [], futures_prices or {})}

<script>
document.querySelectorAll('th[data-key]').forEach(th => {{
  th.addEventListener('click', () => {{
    const key = th.dataset.key;
    const type = th.dataset.type;
    const tbody = th.closest('table').querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const asc = th.classList.toggle('asc');

    // 다른 헤더 초기화
    th.closest('tr').querySelectorAll('th').forEach(h => {{
      if (h !== th) {{ h.classList.remove('sorted','asc'); }}
    }});
    th.classList.add('sorted');

    rows.sort((a, b) => {{
      let va = a.dataset[key];
      let vb = b.dataset[key];
      if (type === 'num') {{ va = parseFloat(va) || 0; vb = parseFloat(vb) || 0; }}
      if (va < vb) return asc ? -1 : 1;
      if (va > vb) return asc ? 1 : -1;
      return 0;
    }});

    rows.forEach(r => tbody.appendChild(r));

    // 화살표 업데이트
    th.querySelector('.arrow').textContent = asc ? '▲' : '▼';
    th.closest('tr').querySelectorAll('th[data-key]').forEach(h => {{
      if (h !== th) h.querySelector('.arrow').textContent = '▲▼';
    }});
  }});
}});
</script>
</body>
</html>"""

    buf = io.BytesIO(html.encode("utf-8"))
    prefix = "claude_portfolio" if show_cash else "portfolio"
    buf.name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    buf.seek(0)
    return buf
