"""투자 현황 HTML 리포트 생성."""
from __future__ import annotations

import io
import math
from collections import defaultdict
from datetime import datetime

from bot.formatters import fetch_current_quotes, format_number, _resolve_tickers
from bot.futures_report import build_futures_section


SIZE_TABLE = {1: 40, 2: 120, 3: 280, 4: 280, 5: 280}
MARKER_TABLE = {1: "tri", 2: "sq", 3: "o", 4: "h", 5: "*"}
# T1 < T2 < (T3 ≈ T4 ≈ T5). 작은 마커일수록 가벼운 비중, T2 보다 T3-5 가 크지만
# 이전(원/원/원/육각/별 시절)보단 살짝 축소.
SVG_RADIUS = {1: 7, 2: 8, 3: 11, 4: 11, 5: 12}


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


def _triangle_points(cx: float, cy: float, r: float) -> str:
    """위 꼭짓점이 위로 향하는 정삼각형 (외접원 반지름 r)."""
    pts = []
    for i in range(3):
        angle = -math.pi / 2 + 2 * math.pi / 3 * i
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        pts.append(f"{x:.2f},{y:.2f}")
    return " ".join(pts)


def _square_points(cx: float, cy: float, r: float) -> str:
    """축에 평행한 정사각형 (한 변의 절반 = r)."""
    return (
        f"{cx - r:.2f},{cy - r:.2f} "
        f"{cx + r:.2f},{cy - r:.2f} "
        f"{cx + r:.2f},{cy + r:.2f} "
        f"{cx - r:.2f},{cy + r:.2f}"
    )


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
    """사분면 plot 바깥 모서리에 붙는 이름표 배지.

    anchor_x: badge 의 좌/우 외곽선이 닿을 X 좌표 (tl/bl → 왼쪽 끝, tr/br → 오른쪽 끝).
    anchor_y: plot 외곽선 쪽 badge 면이 닿을 Y 좌표
      (tl/tr → badge 아래 면 = plot 위쪽 바깥, bl/br → badge 위쪽 면 = plot 아래쪽 바깥).
    """
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
    # plot 바깥 배치: top 사분면은 위로, bottom 사분면은 아래로
    if side in ("tl", "tr"):
        ry = anchor_y - h  # badge 가 anchor 위로 그려져 plot 위 바깥에 위치
    else:
        ry = anchor_y      # badge 가 anchor 아래로 그려져 plot 아래 바깥에 위치
    ty = ry + h / 2 + 4  # text baseline 보정
    return (
        f'<rect x="{rx:.2f}" y="{ry:.2f}" width="{w:.2f}" height="{h}" '
        f'rx="6" ry="6" fill="{bg}" fill-opacity="0.92"/>'
        f'<text x="{tx:.2f}" y="{ty:.2f}" font-size="{font_px}" fill="{fg}" '
        f'text-anchor="{text_anchor}" font-weight="700">{text}</text>'
    )


def _isoprofit_paths(
    sx_fn, sy_fn, y_max: float, total_eval: float, x_abs: float = 50.0
) -> list[str]:
    """수익금 등고선 (포지션 KRW 임팩트 = const).

    포지션 KRW profit ≈ total_eval × w × r / 10000 이므로,
    동일 KRW 임팩트 곡선은 w[%] × r[%] = 10000 · KRW / total_eval.
    예) total_eval=1억5천 + 500만 임팩트 → k = 333.
    """
    if total_eval <= 0:
        return []

    # 색·강도 모두 1천만 톤(#22c55e/#ef4444 · opacity 0.70)으로 통일
    krw_targets = [
        (5_000_000,   "500만",   "#22c55e", "#ef4444", 0.70),
        (10_000_000,  "1천만",   "#22c55e", "#ef4444", 0.70),
        (30_000_000,  "3천만",   "#22c55e", "#ef4444", 0.70),
    ]
    levels: list[tuple[float, str, float, str]] = []
    for krw, label, c_pos, c_neg, opacity in krw_targets:
        k = 10000.0 * krw / total_eval
        levels.append(( k, c_pos, opacity, f"+{label}"))
        levels.append((-k, c_neg, opacity, f"-{label}"))

    paths: list[str] = []
    for k, color, opacity, label in levels:
        # r 샘플링 — 부호 따라 r>0 또는 r<0
        step = 0.5
        rs = []
        if k > 0:
            r = step
            while r <= x_abs:
                rs.append(r); r += step
        else:
            r = -step
            while r >= -x_abs:
                rs.append(r); r -= step
        pts = []
        for r in rs:
            w = k / r  # w × r = k
            if 0 < w <= y_max:
                pts.append((sx_fn(r), sy_fn(w)))
        if not pts:
            continue
        d = "M " + " L ".join(f"{p[0]:.2f},{p[1]:.2f}" for p in pts)
        paths.append(
            f'<path d="{d}" stroke="{color}" stroke-width="1.4" fill="none" '
            f'stroke-dasharray="4,3" opacity="{opacity}"/>'
        )
        # 라벨은 곡선 중간 지점에 작게 — 데이터 점과 부딪힐 수 있어 일단 생략
        # 곡선 끝점(가장 윗쪽) 옆에 작은 텍스트
        top_pt = max(pts, key=lambda p: -p[1])  # y 가 가장 작은 (위쪽) 점
        tx, ty = top_pt
        # 라벨 좌측·우측은 부호로 결정
        anchor = "start" if k > 0 else "end"
        dx_off = 4 if k > 0 else -4
        paths.append(
            f'<text x="{tx + dx_off:.2f}" y="{ty - 4:.2f}" font-size="9" '
            f'fill="{color}" text-anchor="{anchor}" font-weight="600" '
            f'opacity="{opacity}">{label}</text>'
        )
    return paths


def _wave_glyph(cx: float, cy: float, color: str = "#fbbf24") -> str:
    """≋ 스타일 — 수평 두 줄, 각 줄 2 humps (덜 구부러짐). 폭 18px, 진폭 ~2px."""
    top = (
        f"M {cx - 9:.2f},{cy - 3:.2f} c 3,-2 6,-2 9,0 s 6,2 9,0"
    )
    bot = (
        f"M {cx - 9:.2f},{cy + 3:.2f} c 3,-2 6,-2 9,0 s 6,2 9,0"
    )
    return (
        f'<path d="{top} {bot}" stroke="{color}" stroke-width="1.6" '
        f'fill="none" stroke-linecap="round"/>'
    )


def _wave_glyph_v(cx: float, cy: float, color: str = "#fbbf24") -> str:
    """≋ 스타일 — 수직 두 줄, 각 줄 2 humps (덜 구부러짐). 높이 18px, 진폭 ~2px."""
    left = (
        f"M {cx - 3:.2f},{cy - 9:.2f} c -2,3 -2,6 0,9 s 2,6 0,9"
    )
    right = (
        f"M {cx + 3:.2f},{cy - 9:.2f} c -2,3 -2,6 0,9 s 2,6 0,9"
    )
    return (
        f'<path d="{left} {right}" stroke="{color}" stroke-width="1.6" '
        f'fill="none" stroke-linecap="round"/>'
    )


def _tier_marker_svg(
    cx: float, cy: float, tier: int, color: str, *, is_futures: bool = False,
) -> str:
    """Tier 마커 SVG. 선물이면 같은 도형을 한 번 더 빗금 패턴으로 오버레이."""
    marker = MARKER_TABLE[tier]
    r = SVG_RADIUS[tier]
    common = f'fill="{color}" stroke="#0f0f14" stroke-width="1" fill-opacity="0.88"'
    if marker == "o":
        shape = f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" {{fill}}/>'
    elif marker == "h":
        shape = f'<polygon points="{_hexagon_points(cx, cy, r)}" {{fill}}/>'
    elif marker == "*":
        shape = f'<polygon points="{_star_points(cx, cy, r, r * 0.45)}" {{fill}}/>'
    elif marker == "tri":
        shape = f'<polygon points="{_triangle_points(cx, cy, r)}" {{fill}}/>'
    elif marker == "sq":
        shape = f'<polygon points="{_square_points(cx, cy, r)}" {{fill}}/>'
    else:
        shape = f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" {{fill}}/>'

    base = shape.format(fill=common)
    if is_futures:
        overlay = shape.format(fill='fill="url(#fut-hatch)" stroke="none"')
        return base + overlay
    return base


def _build_quadrants_svg(
    rows: list[dict], sector_colors: dict[str, str], total_eval: float,
    futures_positions: list[dict] | None = None,
    futures_prices: dict | None = None,
) -> str:
    """수익률(X) × 비중(Y) 4사분면 산점도를 inline SVG 로 그린다.

    분리선: 수익률 X=0, 비중 Y=10% (비중부족 기준). X축은 ±50% 대칭, Y축은
    0~max 비대칭 (바닥이 0%). 50% 초과 종목은 plot 좌우 끝에 마커를 찍고
    세로 물결로 클립 신호 + (±NN%) 실측 라벨. 점 크기/모양은 SIZE_TABLE/
    MARKER_TABLE (10% tier). 범례는 T1~T5 전부 표시.

    선물 포지션은 같은 마커 위에 빗금 패턴 오버레이로 표시.
    비중 분모는 (현물 평가금 + 선물 notional) 합산.
    """
    if not rows or total_eval <= 0:
        return ""

    # 선물 노출(notional) — 같은 (종목, 방향)이면 월물 다른 것도 합산.
    # 합산 손익률 = sum(미실현 PnL) / sum(cost_basis) × 100  (가중평균)
    futures_prices = futures_prices or {}
    fut_groups: dict[tuple[str, str], dict] = {}
    for fp in futures_positions or []:
        if fp.get("contracts", 0) <= 0:
            continue
        sym = fp.get("symbol", "")
        cm = fp.get("contract_month", "")
        q = futures_prices.get(f"{sym}|{cm}") or futures_prices.get(sym) or {}
        cur = q.get("price") if isinstance(q, dict) else q
        avg = float(fp.get("avg_entry_price", 0) or 0)
        contracts = int(fp.get("contracts", 0))
        mult = int(fp.get("multiplier", 10))
        if cur is None or avg <= 0:
            continue
        name = fp.get("name", "")
        direction = fp.get("direction", "long")
        sign = 1 if direction == "long" else -1
        notional = float(cur) * contracts * mult
        cost_basis = avg * contracts * mult
        pnl = (float(cur) - avg) * contracts * mult * sign
        key = (name, direction)
        g = fut_groups.setdefault(key, {
            "name": name, "direction": direction,
            "sector": fp.get("sector", "") or "기타",
            "notional": 0.0, "cost_basis": 0.0, "pnl": 0.0,
            "months": [],
        })
        g["notional"] += notional
        g["cost_basis"] += cost_basis
        g["pnl"] += pnl
        if len(cm) == 6:
            g["months"].append(cm[4:6])
        # 섹터는 첫 등장값 유지 (대부분 같음)

    fut_rows: list[dict] = []
    fut_total = 0.0
    for g in fut_groups.values():
        fut_total += g["notional"]
        pnl_pct = (g["pnl"] / g["cost_basis"] * 100) if g["cost_basis"] else 0.0
        months = sorted(set(g["months"]))
        months_label = "+".join(months) if months else ""
        fut_rows.append({
            "name": g["name"],
            "sector": g["sector"],
            "eval": g["notional"],
            "pnl_pct": pnl_pct,
            "direction": g["direction"],
            "months_label": months_label,
        })

    denom = total_eval + fut_total

    def _fmt_krw_short(amt: float) -> str:
        a = abs(amt)
        if a >= 1e8:
            return f"{amt / 1e8:.2f}억"
        if a >= 1e7:
            return f"{amt / 1e7:.1f}천만"
        if a >= 1e6:
            return f"{amt / 1e6:.0f}백만"
        return f"{int(amt):,}원"

    points = []
    for r in rows:
        weight = r["eval"] / denom * 100
        tier = _tier_for_weight(weight)
        points.append({
            "name": r["name"],
            "weight": weight,
            "return": r["pnl_pct"],
            "tier": tier,
            "color": sector_colors.get(r["sector"], "#9ca3af"),
            "is_futures": False,
            "amount_label": _fmt_krw_short(r["eval"]),
            "weight_label": f"{weight:.1f}%",
        })
    for r in fut_rows:
        weight = r["eval"] / denom * 100
        tier = _tier_for_weight(weight)
        dir_mark = "↑" if r["direction"] == "long" else "↓"
        months_label = r["months_label"] or "?"
        points.append({
            "name": f"{r['name']}F{dir_mark}{months_label}",
            "weight": weight,
            "return": r["pnl_pct"],
            "tier": tier,
            "color": sector_colors.get(r["sector"], "#9ca3af"),
            "is_futures": True,
            "amount_label": _fmt_krw_short(r["eval"]),
            "weight_label": f"{weight:.1f}%",
        })

    # SVG 영역 — 정사각형 plot. 배지는 plot 바깥에 배치하므로 top/bottom pad 확보.
    W, H = 720, 760
    pad_l, pad_r, pad_t, pad_b = 70, 36, 52, 84
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    # X축: 수익률(%). ±50% 고정, 그 너머는 클립 + 세로 물결 표시.
    x_abs = 50.0

    # Y축: 비중(%). 35% 고정 max, 그 너머는 클립 + 가로 물결.
    Y_THRESH = 10.0  # 비중부족/과체중 분리선
    y_max = 30.0
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

    # 선물 마커용 빗금 패턴 (대각선 white stripes, 반투명)
    parts.append(
        '<defs>'
        '<pattern id="fut-hatch" patternUnits="userSpaceOnUse" '
        'width="6" height="6" patternTransform="rotate(45)">'
        '<rect width="6" height="6" fill="none"/>'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,0.55)" stroke-width="2"/>'
        '</pattern>'
        '</defs>'
    )

    # 사분면 배경 — 단색 (SVG 컨테이너 background:#0f0f14 그대로),
    # 사분면 구분은 그리드선과 모서리 배지로만 표시 (시인성 우선)

    # 격자 — X 10% 단위, Y 5% 단위 (5/15/25 도 확인 가능하게)
    ticks_x = list(range(-int(x_abs), int(x_abs) + 1, 10))
    ticks_y = list(range(0, int(y_max) + 1, 5))
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

    # 수익금 iso-profit 등고선 — 데이터 점 아래, 분리선 위에 깔림
    parts.extend(_isoprofit_paths(sx, sy, y_max, total_eval, x_abs))

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

    # X 눈금 텍스트 — 하단 배지 바깥쪽에 배치
    x_tick_y = y_bottom + 6 + 26 + 14  # badge 아래에서 14px
    for tx in ticks_x:
        if tx == 0:
            label = "0%"
        elif tx > 0:
            label = f"+{tx}%"
        else:
            label = f"{tx}%"
        parts.append(
            f'<text x="{sx(tx):.2f}" y="{x_tick_y:.2f}" font-size="10" '
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

    # 사분면 이름표(배지) — plot 바깥(위/아래)에 배치
    badge_gap = 6
    parts.append(_quadrant_badge(
        anchor_x=x_right, anchor_y=y_top - badge_gap,
        text="잘하는 것 (비중↑·수익)",
        bg="#ef4444", fg="#ffffff", side="tr",
    ))
    parts.append(_quadrant_badge(
        anchor_x=x_left, anchor_y=y_top - badge_gap,
        text="큰 위험 (비중↑·손실)",
        bg="#6366f1", fg="#ffffff", side="tl",
    ))
    parts.append(_quadrant_badge(
        anchor_x=x_left, anchor_y=y_bottom + badge_gap,
        text="다행 (비중↓·손실)",
        bg="#22c55e", fg="#0f0f14", side="bl",
    ))
    parts.append(_quadrant_badge(
        anchor_x=x_right, anchor_y=y_bottom + badge_gap,
        text="비중 부족 (비중↓·수익)",
        bg="#fbbf24", fg="#0f0f14", side="br",
    ))

    # 데이터 점 — 큰 마커가 먼저 그려져서 작은 마커가 위에 오도록 tier desc 정렬
    WAVE_COLOR = "#fbbf24"
    for p in sorted(points, key=lambda d: (-d["tier"], -d["weight"])):
        r = SVG_RADIUS[p["tier"]]
        clipped_x = abs(p["return"]) > 50.0
        clipped_y = p["weight"] > 35.0  # 위쪽으로 잘림 (비중은 항상 양수)

        # 마커 좌표 — 클립된 축은 plot 가장자리로 밀어붙임
        if clipped_x:
            dir_x = 1 if p["return"] > 0 else -1
            cx = (x_right - r - 2) if dir_x > 0 else (x_left + r + 2)
        else:
            cx = sx(p["return"])
        if clipped_y:
            cy = y_top + r + 2
        else:
            cy = sy(p["weight"])
        parts.append(_tier_marker_svg(
            cx, cy, p["tier"], p["color"], is_futures=p.get("is_futures", False),
        ))

        # 클립 표시 물결 — X 잘림은 세로 물결, Y 잘림은 가로 물결
        if clipped_x:
            dir_x = 1 if p["return"] > 0 else -1
            parts.append(_wave_glyph_v(cx - dir_x * (r + 8), cy, WAVE_COLOR))
        if clipped_y:
            parts.append(_wave_glyph(cx, cy + r + 9, WAVE_COLOR))

        # 라벨 — 클립된 축에 따라 위치 / 실측 값 부착
        extras = []
        if clipped_x:
            extras.append(f"{p['return']:+.0f}%")
        if clipped_y:
            extras.append(f"비중 {p['weight']:.0f}%")
        suffix = (
            f' <tspan fill="{WAVE_COLOR}" font-weight="700" font-size="10">'
            f'({" · ".join(extras)})</tspan>'
        ) if extras else ""

        if clipped_x:
            dir_x = 1 if p["return"] > 0 else -1
            label_x = cx - dir_x * (r + 20)
            ly = cy + 3
            anchor = "end" if dir_x > 0 else "start"
        elif clipped_y:
            # 위에 붙은 마커 → 라벨은 아래(물결 너머)에 중앙 정렬
            label_x = cx
            ly = cy + r + 22
            anchor = "middle"
        else:
            label_x = cx + r + 4
            ly = cy + 3
            anchor = "start"

        # 금액/비중 토글용 tspan — body.show-amount / body.show-weight 클래스로 표시
        amount_span = (
            f'<tspan class="qd-label-amount" style="display:none" '
            f'fill="#9ca3af"> ({p["amount_label"]})</tspan>'
        )
        weight_span = (
            f'<tspan class="qd-label-weight" style="display:none" '
            f'fill="#9ca3af"> {p["weight_label"]}</tspan>'
        )
        parts.append(
            f'<text x="{label_x:.2f}" y="{ly:.2f}" font-size="11" '
            f'fill="#e0e0e0" text-anchor="{anchor}">'
            f'{p["name"]}{amount_span}{weight_span}{suffix}</text>'
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
    # 선물 빗금 범례
    if fut_rows:
        size = 36
        fut_chip = (
            f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
            f'style="vertical-align:middle">'
            '<defs><pattern id="fut-hatch-legend" patternUnits="userSpaceOnUse" '
            'width="6" height="6" patternTransform="rotate(45)">'
            '<rect width="6" height="6" fill="none"/>'
            '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,0.55)" stroke-width="2"/>'
            '</pattern></defs>'
            f'<circle cx="{size/2}" cy="{size/2}" r="11" fill="#888" '
            'fill-opacity="0.88" stroke="#0f0f14" stroke-width="1"/>'
            f'<circle cx="{size/2}" cy="{size/2}" r="11" fill="url(#fut-hatch-legend)" stroke="none"/>'
            '</svg>'
        )
        legend_items.append(
            f'<div class="qd-legend-item">{fut_chip}<span>선물 (빗금)</span></div>'
        )
    legend_html = (
        '<div class="qd-legend">' + "".join(legend_items) + "</div>"
    )

    caption = (
        '<div class="qd-caption">'
        '점 크기 = 비중 tier (10% 단위) · 세모/네모/원/육각형/별 (T1·T2·T3·T4·T5) · '
        '<span style="color:#22c55e">초록</span>/<span style="color:#ef4444">빨강</span> 점선 = '
        '동일 수익금 등고선 (500만 / 1천만 / 3천만 KRW — 현재 포트폴리오 평가금 대비 동적 계산)'
        "</div>"
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
    futures_cash: float | None = None,
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

    # 선물 포지션을 섹터에 합산 + 미실현 손익 누적
    total_margin = 0.0
    total_futures_unrealized = 0.0
    for fp in futures_positions or []:
        if fp.get("contracts", 0) <= 0:
            continue
        sector = fp.get("sector", "") or "기타"
        sym = fp.get("symbol", "")
        cm = fp.get("contract_month", "")
        key = f"{sym}|{cm}"
        q = (futures_prices or {}).get(key) or (futures_prices or {}).get(sym) or {}
        price = q.get("price") if isinstance(q, dict) else q
        avg = float(fp.get("avg_entry_price", 0))
        contracts = int(fp.get("contracts", 0))
        mult = int(fp.get("multiplier", 10))
        if price is None:
            price = avg
        else:
            sign = 1 if fp.get("direction", "long") == "long" else -1
            total_futures_unrealized += (float(price) - avg) * contracts * mult * sign
        notional = float(price) * contracts * mult
        sector_data[sector] += notional
        sector_futures[sector] += notional
        total_margin += float(fp.get("initial_margin", 0))

    # 현금 분할: futures_cash 가 지정되면 현물/선물 cash 를 분리.
    # - 현물 cash = total cash - futures_cash (≥0 clamp)
    # - 선물 free cash = futures_cash - total_margin (≥0 clamp, 마진 차감 후 가용분)
    futures_cash_val = float(futures_cash) if futures_cash else 0.0
    if show_cash and cash_remaining > 0:
        if futures_cash_val > 0:
            spot_part = max(cash_remaining - futures_cash_val, 0.0)
            fut_free_part = max(futures_cash_val - total_margin, 0.0)
            if spot_part > 0:
                sector_data["현금"] += spot_part
            if fut_free_part > 0:
                sector_data["현금"] += fut_free_part
                sector_futures["현금"] += fut_free_part
        else:
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

    # 스택바 (섹터 비중 한 줄) — 같은 섹터 안에서 현물/선물을 인접 두 조각으로 분리.
    # 라벨은 두 조각을 묶은 group 의 정중앙에 위치 (현물 segment 기준 X).
    stack_segments = ""
    for sector, val in sector_sorted:
        pct = (val / sector_total * 100) if sector_total else 0
        color = sector_colors[sector]
        fut_val = sector_futures.get(sector, 0)
        spot_val = val - fut_val
        spot_pct = (spot_val / sector_total * 100) if sector_total else 0
        fut_pct = (fut_val / sector_total * 100) if sector_total else 0

        if pct < 0.001:
            continue

        # 라벨 — 둘 다 있으면 "총%(현%+선%)" 분해, 아니면 합계만.
        if spot_pct > 0 and fut_pct > 0:
            label_body = (
                f"{sector}<br>{pct:.0f}%({spot_pct:.0f}+{fut_pct:.0f})"
            )
            min_pct_for_breakdown = 8
        else:
            label_body = f"{sector}<br>{pct:.0f}%"
            min_pct_for_breakdown = 3
        label_html = (
            f'<span class="seg-label">{label_body}</span>'
            if pct >= min_pct_for_breakdown else ""
        )

        # group 내부 spot/fut 비중 (group width 100% 기준)
        group_inner = ""
        if spot_pct > 0:
            spot_in = spot_pct / pct * 100
            group_inner += (
                f'<div class="stack-seg" style="width:{spot_in}%;background:{color}" '
                f'title="{sector} 현물 {spot_pct:.1f}%"></div>'
            )
        if fut_pct > 0:
            fut_in = fut_pct / pct * 100
            group_inner += (
                f'<div class="stack-seg futures-stripe" '
                f'style="width:{fut_in}%;background-color:{color}" '
                f'title="{sector} 선물 {fut_pct:.1f}%"></div>'
            )
        stack_segments += (
            f'<div class="stack-group" style="width:{pct}%">'
            + group_inner + label_html + '</div>'
        )

    pnl_class = "profit" if total_pnl >= 0 else "loss"
    pnl_sign = "+" if total_pnl >= 0 else ""

    # 총 수익은 테이블 손익 합(total_pnl) 기준 — 신용대출을 total_invested에 포함시켜도 일치함
    total_return = total_pnl
    total_return_pct = (total_pnl / initial_capital * 100) if initial_capital else total_pnl_pct
    return_class = "profit" if total_return >= 0 else "loss"
    return_sign = "+" if total_return >= 0 else ""

    # 총 자산(NAV) = 현물 평가금 + 잔여 현금 + 선물 미실현손익
    total_nav = total_eval + (cash_remaining if show_cash else 0) + total_futures_unrealized
    nav_return = total_nav - initial_capital if initial_capital else 0
    nav_return_pct = (nav_return / initial_capital * 100) if initial_capital else 0
    nav_class = "profit" if nav_return >= 0 else "loss"
    nav_sign = "+" if nav_return >= 0 else ""

    # 신용대출 제외 순자산 — 메인 표기는 신용 제외(net_nav), 신용 포함은 sub
    total_credit = sum(float(h.get("credit_loan", 0) or 0) for h in active)
    net_nav = total_nav - total_credit
    net_return = net_nav - initial_capital if initial_capital else 0
    net_return_pct = (net_return / initial_capital * 100) if initial_capital else 0
    net_class = "profit" if net_return >= 0 else "loss"
    net_sign = "+" if net_return >= 0 else ""

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
  .stack-group {{ position:relative; display:flex; height:100%; min-width:0; }}
  .stack-seg {{ height:100%; min-width:0; }}
  .seg-label {{ position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
               color:#fff; font-size:10px; font-weight:600; text-align:center; line-height:1.2;
               white-space:nowrap; pointer-events:none;
               text-shadow:0 0 3px rgba(0,0,0,0.55); }}

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

  /* 4사분면 라벨 토글 */
  .qd-toolbar {{ display:flex; justify-content:flex-end; gap:8px; margin:8px 0 4px; }}
  .qd-toggle {{ background:#1a1a24; color:#888; border:1px solid #333;
                padding:5px 12px; border-radius:6px; cursor:pointer;
                font-size:12px; font-family:inherit; }}
  .qd-toggle:hover {{ border-color:#555; color:#bbb; }}
  .qd-toggle.on {{ background:#fbbf24; color:#0f0f14; border-color:#fbbf24; font-weight:600; }}
  body.show-amount .qd-label-amount {{ display:inline !important; }}
  body.show-weight .qd-label-weight {{ display:inline !important; }}

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
    {("<div class='card'><div class='label'>총 자산 (신용 제외)</div><div class='value'>" + format_number(int(net_nav)) + "원</div><div class='sub " + net_class + "'>" + net_sign + f"{net_return_pct:.1f}% vs 초기자본</div>" + ("<div class='sub' style='color:#9ca3af'>신용 포함: " + format_number(int(total_nav)) + "원</div>" if total_credit > 0 else "") + "</div>") if show_cash and initial_capital else ""}
    {("<div class='card'><div class='label'>예수금</div><div class='value'>" + format_number(int(cash_remaining)) + "원</div>" + ("<div class='sub' style='color:#9ca3af'>현물 " + format_number(int(max(cash_remaining - min(futures_cash_val, cash_remaining), 0))) + " / 선물 " + format_number(int(min(futures_cash_val, cash_remaining))) + "</div>" if futures_cash_val > 0 else "") + "</div>") if show_cash and initial_capital else "<div class='card'><div class='label'>총 투자금</div><div class='value'>" + format_number(int(total_invested)) + "원</div></div>"}
    <div class="card">
      <div class="label">총 평가금{" (신용 제외)" if total_credit > 0 else ""}</div>
      <div class="value">{format_number(int(total_eval - total_credit))}원</div>
      {("<div class='sub' style='color:#9ca3af'>신용 포함: " + format_number(int(total_eval)) + "원</div>") if total_credit > 0 else ""}
    </div>
    <div class="card">
      <div class="label">총 수익</div>
      <div class="value {return_class if show_cash else pnl_class}">{(return_sign + format_number(int(total_return)) + '원') if show_cash else (pnl_sign + format_number(int(total_pnl)) + '원')}</div>
      <div class="sub {return_class if show_cash else pnl_class}">{(return_sign + f'{total_return_pct:.1f}%') if show_cash else (pnl_sign + f'{int(total_pnl_pct)}%')}</div>
    </div>
  </div>

  <div class="stack">{stack_segments}</div>
  {('<div style="font-size:11px;color:#888;margin:-24px 0 32px;display:flex;align-items:center;gap:6px"><span class="stripe-chip" style="display:inline-block;width:14px;height:14px;border-radius:3px;background-color:#888;background-image:repeating-linear-gradient(135deg,rgba(255,255,255,0.30) 0,rgba(255,255,255,0.30) 3px,transparent 3px,transparent 8px)"></span>빗금 = 선물 명목 노출 (계약수 × 현재가 × 승수)</div>') if any(sector_futures.values()) else ''}

  <div class="qd-toolbar">
    <button type="button" class="qd-toggle" data-target="amount">금액 표시</button>
    <button type="button" class="qd-toggle" data-target="weight">비중 표시</button>
  </div>
  {_build_quadrants_svg(rows, sector_colors, total_eval, futures_positions, futures_prices)}

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

  {build_futures_section(futures_positions or [], futures_prices or {}, total_equity=total_eval + cash_remaining)}

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

// 4사분면 라벨 토글 (금액/비중)
document.querySelectorAll('.qd-toggle').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const target = btn.dataset.target;
    document.body.classList.toggle(`show-${{target}}`);
    btn.classList.toggle('on');
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
