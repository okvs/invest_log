"""백테스트 결과를 단일 HTML 파일로 렌더링.

다크 테마, PNG 임베드(base64), 전체 거래일 표(★ 초과 강조), 요약 카드.
텔레그램 reply_document 로 발송 가능.
"""
from __future__ import annotations

import base64
import io
from datetime import date
from html import escape


def _fmt_krw(x: float) -> str:
    if abs(x) >= 1e8:
        return f"{x/1e8:.2f}억"
    if abs(x) >= 1e7:
        return f"{x/1e7:.1f}천만"
    if abs(x) >= 1e4:
        return f"{x/1e4:.0f}만"
    return f"{int(round(x)):,}원"


def build_backtest_html(res: dict) -> io.BytesIO:
    """run_backtest() 결과 dict 를 HTML BytesIO 로 변환."""
    rows = res["rows"]
    higher = res["higher"]
    nav_now = res["nav_actual_today"]
    gross = res.get("nav_actual_today_gross", nav_now)
    credit = res.get("cur_credit", 0)
    initial = res["initial"]
    return_pct = (nav_now / initial - 1) * 100 if initial else 0.0
    title = (
        f"백테스트 — {rows[0]['date']:%Y-%m-%d} ~ {rows[-1]['date']:%Y-%m-%d}"
    )

    # PNG → base64
    png_buf = res["png_buf"]
    if hasattr(png_buf, "getvalue"):
        png_b64 = base64.b64encode(png_buf.getvalue()).decode("ascii")
    else:
        png_b64 = base64.b64encode(png_buf).decode("ascii")

    # 표 행
    tr_rows = []
    for r in rows:
        diff = r["nav_frozen_today"] - nav_now
        cls = "beat" if diff > 0 else ("worse" if diff < 0 else "tie")
        sign = "+" if diff > 0 else ""
        marker = "★" if diff > 0 else ""
        tr_rows.append(
            f"<tr class='{cls}'>"
            f"<td>{r['date']:%Y-%m-%d}</td>"
            f"<td class='num'>{_fmt_krw(r['nav_frozen_today'])}</td>"
            f"<td class='num diff'>{sign}{_fmt_krw(diff)}</td>"
            f"<td class='num'>{_fmt_krw(r['nav_actual_d'])}</td>"
            f"<td class='num small'>{_fmt_krw(r.get('credit_d', 0))}</td>"
            f"<td class='marker'>{marker}</td>"
            f"</tr>"
        )
    table_body = "\n".join(tr_rows)

    # 정점 (가장 큰 win) 강조
    top_beats = sorted(
        higher, key=lambda r: r["nav_frozen_today"], reverse=True
    )[:5]
    top_html = ""
    if top_beats:
        items = "".join(
            f"<li><b>{r['date']:%m-%d}</b> · "
            f"{_fmt_krw(r['nav_frozen_today'])} "
            f"<span class='pos'>(+{_fmt_krw(r['nav_frozen_today']-nav_now)})</span></li>"
            for r in top_beats
        )
        top_html = (
            f"<div class='card'><div class='card-label'>"
            f"★ 현재 순자산 초과 거래일 TOP 5</div>"
            f"<ul class='top-list'>{items}</ul></div>"
        )

    html = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>
  :root {{
    --bg: #0f0f14;
    --card: #16161e;
    --border: #2a2a36;
    --text: #e5e7eb;
    --muted: #9ca3af;
    --pos: #22c55e;
    --neg: #ef4444;
    --tie: #f59e0b;
    --accent: #3b82f6;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); margin: 0; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo",
                 "Segoe UI", sans-serif;
    font-size: 14px;
  }}
  h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 4px 0; }}
  .subtitle {{ color: var(--muted); margin-bottom: 20px; font-size: 13px; }}
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
              margin-bottom: 20px; }}
  .card {{ background: var(--card); border: 1px solid var(--border);
           border-radius: 8px; padding: 14px 16px; }}
  .card-label {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
  .card-value {{ font-size: 22px; font-weight: 600; }}
  .card-sub {{ color: var(--muted); font-size: 11px; margin-top: 4px; }}
  .pos {{ color: var(--pos); }} .neg {{ color: var(--neg); }}
  .accent {{ color: var(--accent); }} .tie {{ color: var(--tie); }}
  .chart-wrap {{ background: var(--card); border: 1px solid var(--border);
                 border-radius: 8px; padding: 14px; margin-bottom: 20px;
                 text-align: center; }}
  .chart-wrap img {{ max-width: 100%; height: auto; }}
  .top-list {{ list-style: none; padding: 0; margin: 0; }}
  .top-list li {{ padding: 4px 0; border-bottom: 1px solid var(--border);
                  font-size: 13px; }}
  .top-list li:last-child {{ border-bottom: none; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card);
           border-radius: 8px; overflow: hidden; }}
  th, td {{ padding: 8px 12px; text-align: left; font-size: 13px;
            border-bottom: 1px solid var(--border); }}
  th {{ background: #1c1c26; color: var(--muted); font-weight: 500;
        font-size: 12px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.small {{ color: var(--muted); font-size: 12px; }}
  td.marker {{ color: var(--pos); font-size: 14px; text-align: center;
               width: 36px; }}
  tr.beat td.diff {{ color: var(--pos); font-weight: 500; }}
  tr.beat {{ background: rgba(34, 197, 94, 0.04); }}
  tr.worse td.diff {{ color: var(--neg); }}
  .note {{ color: var(--muted); font-size: 11px; margin-top: 16px;
           line-height: 1.5; }}
</style></head><body>
<h1>{escape(title)}</h1>
<div class="subtitle">
  포트폴리오 동결 백테스트 — 현물+선물 (다음 만기 롤오버 가정) · 신용 제외 순자산 기준
</div>

<div class="summary">
  <div class="card">
    <div class="card-label">현재 순자산 (신용 제외)</div>
    <div class="card-value accent">{_fmt_krw(nav_now)}</div>
    <div class="card-sub">초기자본 {_fmt_krw(initial)} 대비 {return_pct:+.1f}%</div>
  </div>
  <div class="card">
    <div class="card-label">총자산 (신용 포함)</div>
    <div class="card-value">{_fmt_krw(gross)}</div>
    <div class="card-sub">현물 {_fmt_krw(res['cur_holdings_value'])} ·
      선물 {_fmt_krw(res['cur_futures_value'])} ·
      현금 {_fmt_krw(res['today_total_cash'])}</div>
  </div>
  <div class="card">
    <div class="card-label">신용대출</div>
    <div class="card-value neg">−{_fmt_krw(credit)}</div>
    <div class="card-sub">현재 종목별 합산</div>
  </div>
  <div class="card">
    <div class="card-label">현재 초과 거래일</div>
    <div class="card-value {'pos' if higher else 'tie'}">
      {len(higher)}/{len(rows)}건
    </div>
    <div class="card-sub">동결-오늘 &gt; 현재 순자산</div>
  </div>
</div>

{top_html}

<div class="chart-wrap">
  <img src="data:image/png;base64,{png_b64}" alt="backtest chart"/>
</div>

<table>
  <thead><tr>
    <th>날짜 (D)</th>
    <th class="num">동결-오늘 순자산</th>
    <th class="num">vs 현재</th>
    <th class="num">그날 실제 순자산</th>
    <th class="num">그날 신용</th>
    <th></th>
  </tr></thead>
  <tbody>{table_body}</tbody>
</table>

<div class="note">
  · <b>동결-오늘 순자산</b>: D 시점의 현물 보유·선물 포지션·신용을 그대로 동결하고 오늘 종가로 평가.<br>
  · <b>그날 실제 순자산</b>: D 시점 보유를 D 시점 종가로 평가 (참고용).<br>
  · 선물은 만기 도달 시 같은 종목·방향으로 다음 만기 롤오버 가정, 기초자산 가격 사용.<br>
  · 신용대출 잔액은 매수 시 (100−margin_ratio)/100 비율 누적, 매도 시 비례 차감으로 재구성.<br>
  · 가격은 pykrx 일별 종가 + 누락 종목은 마지막 거래가 폴백.
</div>
</body></html>
"""
    buf = io.BytesIO(html.encode("utf-8"))
    return buf
