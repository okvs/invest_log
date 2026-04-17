"""투자 현황 HTML 리포트 생성."""
from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime

from bot.formatters import fetch_current_prices, format_number, _resolve_tickers


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

    # 현재가 조회
    name_to_ticker, missing = _resolve_tickers(active)
    tickers = list(set(name_to_ticker.values()))
    prices = fetch_current_prices(tickers) if tickers else {}

    # 종목별 데이터 계산
    rows = []
    for h in active:
        name = h["name"]
        qty = h["quantity"]
        avg = h["avg_price"]
        invested = h["total_invested"]
        ticker = name_to_ticker.get(name, "")
        cur_price = prices.get(ticker)

        if cur_price is not None:
            eval_amt = cur_price * qty
            pnl = eval_amt - invested
            pnl_pct = (pnl / invested * 100) if invested else 0
        else:
            cur_price = None
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

    # 섹터별 집계
    sector_data: dict[str, float] = defaultdict(float)
    for r in rows:
        sector_data[r["sector"]] += r["eval"]
    sector_sorted = sorted(sector_data.items(), key=lambda x: x[1], reverse=True)
    sector_total = sum(v for _, v in sector_sorted)

    # 섹터별 색상
    colors = ["#4A90D9", "#E74C3C", "#2ECC71", "#F39C12", "#9B59B6",
              "#1ABC9C", "#E67E22", "#3498DB", "#E91E63", "#00BCD4"]
    sector_colors = {s: colors[i % len(colors)] for i, (s, _) in enumerate(sector_sorted)}

    now = datetime.now().strftime("%Y.%m.%d %H:%M")

    # 종목 행 HTML
    stock_rows_html = ""
    for r in rows:
        pnl_class = "profit" if r["pnl"] >= 0 else "loss"
        pnl_sign = "+" if r["pnl"] >= 0 else ""
        dot_color = sector_colors.get(r["sector"], "#999")

        cur_display = f'{format_number(int(r["cur_price"]))}원' if r["cur_price"] is not None else "-"

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

    # 섹터 바 HTML — 가장 큰 섹터 기준 상대 비율
    max_pct = (sector_sorted[0][1] / sector_total * 100) if sector_sorted and sector_total else 100
    sector_bars_html = ""
    for sector, val in sector_sorted:
        pct = (val / sector_total * 100) if sector_total else 0
        bar_width = (pct / max_pct * 100) if max_pct else 0
        color = sector_colors[sector]
        sector_bars_html += f"""
        <div class="sector-row">
          <div class="sector-label">{sector}</div>
          <div class="sector-bar-wrap">
            <div class="sector-bar" style="width:{bar_width}%;background:{color}"></div>
          </div>
          <div class="sector-val">{pct:.1f}% <span class="sector-amt">{format_number(int(val))}원</span></div>
        </div>"""

    # 스택바 (섹터 비중 한 줄)
    stack_segments = ""
    for sector, val in sector_sorted:
        pct = (val / sector_total * 100) if sector_total else 0
        color = sector_colors[sector]
        if pct >= 3:
            stack_segments += f'<div class="stack-seg" style="width:{pct}%;background:{color}" title="{sector} {pct:.1f}%"><span>{sector}<br>{pct:.0f}%</span></div>'
        else:
            stack_segments += f'<div class="stack-seg" style="width:{pct}%;background:{color}" title="{sector} {pct:.1f}%"></div>'

    pnl_class = "profit" if total_pnl >= 0 else "loss"
    pnl_sign = "+" if total_pnl >= 0 else ""

    # 현금 카드 계산 (show_cash 여부와 무관하게 변수 정의)
    if cash_override is not None:
        cash_remaining = cash_override
    else:
        cash_remaining = (initial_capital - total_invested) if initial_capital is not None else 0
    total_asset = cash_remaining + total_eval if initial_capital is not None else total_eval
    total_return = (total_asset - initial_capital) if initial_capital is not None else total_pnl
    total_return_pct = (total_return / initial_capital * 100) if initial_capital else total_pnl_pct
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
  .sector-bar {{ height:100%; border-radius:4px; }}
  .sector-val {{ font-size:13px; font-weight:600; white-space:nowrap; }}
  .sector-amt {{ color:#888; font-weight:400; margin-left:6px; }}
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
