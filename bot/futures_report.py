"""선물 포지션 HTML 섹션 — 대시보드 리포트에서 사용.

현재가가 비어 있으면 미실현 손익은 0으로 표시하고
"시세 미연동"으로 안내한다. Phase 4에서 자동 시세 조회로 채워진다.
"""
from __future__ import annotations

from datetime import date

from bot.formatters import format_number


def _format_man(n: float) -> str:
    man = int(n // 10000)
    return f"{man:,}만"


def _days_to_expiry(expiry_iso: str) -> int | None:
    try:
        y, m, d = map(int, expiry_iso.split("-"))
        return (date(y, m, d) - date.today()).days
    except Exception:
        return None


def build_futures_section(
    positions: list[dict],
    current_prices: dict[str, float] | None = None,
) -> str:
    """선물 포지션을 HTML 조각으로 반환. 빈 리스트면 빈 문자열.

    Args:
      positions: futures_positions.json 의 dict 리스트
      current_prices: 기초자산 종목코드(예: "005930") → 현재가(또는 선물가)
                      매핑. 비어 있으면 미실현 0으로 표시.
    """
    active = [p for p in positions if p.get("contracts", 0) > 0]
    if not active:
        return ""

    current_prices = current_prices or {}
    rows_html = ""
    total_unrealized = 0.0
    total_margin = 0.0

    for p in active:
        name = p.get("name", "")
        direction = p.get("direction", "long")
        direction_kr = "롱" if direction == "long" else "숏"
        contracts = p.get("contracts", 0)
        avg = float(p.get("avg_entry_price", 0))
        mult = int(p.get("multiplier", 10))
        margin = float(p.get("initial_margin", 0))
        cm = p.get("contract_month", "")
        cm_label = f"{cm[2:4]}-{cm[4:6]}" if len(cm) == 6 else cm

        cur = current_prices.get(p.get("symbol", ""))
        if cur is None:
            cur_display = "-"
            unrealized = 0.0
            unrealized_pct = 0.0
        else:
            cur_display = f"{format_number(int(cur))}원"
            sign = 1 if direction == "long" else -1
            unrealized = (cur - avg) * contracts * mult * sign
            notional = avg * contracts * mult
            unrealized_pct = (unrealized / notional * 100) if notional else 0.0

        total_unrealized += unrealized
        total_margin += margin

        days = _days_to_expiry(p.get("expiry_date", ""))
        if days is None:
            d_display = "-"
            d_class = ""
        else:
            d_display = f"D-{days}" if days >= 0 else f"만기경과 {-days}일"
            d_class = "loss" if days <= 3 else ""

        pnl_class = "profit" if unrealized >= 0 else "loss"
        pnl_sign = "+" if unrealized >= 0 else ""
        rows_html += f"""
        <tr>
          <td>{name}</td>
          <td>{direction_kr}</td>
          <td class="num">{contracts}계약</td>
          <td>{cm_label}</td>
          <td class="num {d_class}">{d_display}</td>
          <td class="num">{format_number(int(avg))}원</td>
          <td class="num">{cur_display}</td>
          <td class="num {pnl_class}">{pnl_sign}{format_number(int(unrealized))}원<br><small>{pnl_sign}{unrealized_pct:.1f}%</small></td>
          <td class="num">{format_number(int(margin))}원</td>
          <td class="thesis">{p.get("thesis","")}</td>
        </tr>"""

    # 증거금 사용률: |미실현 손실| / 위탁증거금 합 (롱숏 무관 손실분만)
    if total_margin > 0:
        worst_loss = min(0.0, total_unrealized)  # 손실이면 음수, 익절이면 0
        margin_burn = (-worst_loss / total_margin * 100) if worst_loss < 0 else 0.0
    else:
        margin_burn = 0.0

    burn_class = "loss" if margin_burn > 50 else ("profit" if margin_burn == 0 else "")
    total_class = "profit" if total_unrealized >= 0 else "loss"
    total_sign = "+" if total_unrealized >= 0 else ""

    quote_note = (
        '<div style="font-size:11px;color:#888;margin-top:4px">'
        '시세는 기초자산 현재가 기준 추정치. 정확한 선물가는 텔레그램에서 \'선물시세\' 입력.'
        '</div>'
    )

    return f"""
  <div class="section-title" style="margin-top:40px">선물 포지션</div>
  {quote_note}
  <div class="cards" style="grid-template-columns:repeat(3,1fr);max-width:600px">
    <div class="card">
      <div class="label">포지션 수</div>
      <div class="value">{len(active)}개</div>
    </div>
    <div class="card">
      <div class="label">미실현 손익</div>
      <div class="value {total_class}">{total_sign}{format_number(int(total_unrealized))}원</div>
    </div>
    <div class="card">
      <div class="label">증거금 잠식률</div>
      <div class="value {burn_class}">{margin_burn:.1f}%</div>
      <div class="sub">총 증거금 {format_number(int(total_margin))}원</div>
    </div>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>종목</th><th>방향</th><th>계약수</th><th>결제월</th><th>만기</th>
          <th>평균진입</th><th>현재가</th><th>미실현</th><th>증거금</th><th>사유</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
"""
