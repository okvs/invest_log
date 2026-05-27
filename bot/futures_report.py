"""선물 포지션 HTML 섹션 — 대시보드 리포트에서 사용.

현재가가 비어 있으면 미실현 손익은 0으로 표시하고
"시세 미연동"으로 안내한다. Phase 4에서 자동 시세 조회로 채워진다.
"""
from __future__ import annotations

from datetime import date

from bot.formatters import format_number

# 유지위탁증거금률 ÷ 위탁증거금률 — KRX 주식선물은 보통 위탁의 2/3 수준.
# 포지션에 maintenance_margin 이 직접 세팅돼 있으면 그 값을 우선 쓴다.
MAINTENANCE_RATIO = 2.0 / 3.0


def _format_man(n: float) -> str:
    man = int(n // 10000)
    return f"{man:,}만"


def _margin_call_banner(
    equity_now: float, maint_total: float, total_margin: float,
    free_cash: float, total_unrealized: float, signed_notional: float,
    maint_note: str = "위탁 × 2/3 가정",
) -> str:
    """보유 선물 현재가가 일괄로 몇 % 움직이면 마진콜(추가증거금)이 걸리는지 배너.

    순자산(예수금 + 위탁증거금 + 미실현) = E, 일괄 변동률 x 에서 longs/shorts
    부호를 반영한 명목금 합 signed_notional 만큼 손익이 바뀐다:
        E(x) = equity_now - x · signed_notional.
    E(x) 가 유지증거금(maint_total) 밑으로 내려가는 x* 를 구한다.
        x* = (equity_now - maint_total) / signed_notional.
    signed_notional > 0(순롱)이면 x*>0 은 '하락', <0(순숏)이면 '상승' 트리거.
    """
    if total_margin <= 0 or signed_notional == 0:
        return ""

    x_call = (equity_now - maint_total) / signed_notional
    x_cash = (free_cash + total_unrealized) / signed_notional  # 순자산이 위탁증거금까지 내려오는 변동
    net_long = signed_notional > 0
    sign_txt = "-" if net_long else "+"
    move = "하락" if net_long else "상승"

    if x_call <= 0:
        head = "보유 선물이 현재 이미 추가증거금(마진콜) 필요 수준입니다"
    elif x_call >= 1:
        head = f"현재가가 일괄 100% {move}해도 마진콜 없음 (증거금 여유 충분)"
    else:
        head = (
            f"보유 선물 현재가가 일괄 "
            f"<b style='color:#f87171'>{sign_txt}{abs(x_call) * 100:.1f}%</b> "
            f"{move}하면 추가증거금(마진콜) 발생 예상"
        )

    sub_parts: list[str] = []
    if 0 < x_cash < x_call:  # 순롱 기준 가용현금이 먼저 소진되는 지점
        sub_parts.append(f"가용예수금 소진 {sign_txt}{abs(x_cash) * 100:.1f}%")
    sub_parts.append(
        f"현 순자산 {_format_man(equity_now)} · "
        f"유지증거금 {_format_man(maint_total)}({maint_note})"
    )
    sub_txt = " · ".join(sub_parts)

    return (
        "<div style='margin-top:12px;padding:10px 14px;border-radius:8px;"
        "background:#161620;border:1px solid #2a2a3a;font-size:13px;color:#e5e7eb'>"
        f"{head}"
        f"<div style='font-size:11px;color:#888;margin-top:3px'>{sub_txt}</div>"
        "</div>"
    )


def _days_to_expiry(expiry_iso: str) -> int | None:
    try:
        y, m, d = map(int, expiry_iso.split("-"))
        return (date(y, m, d) - date.today()).days
    except Exception:
        return None


def build_futures_section(
    positions: list[dict],
    current_prices: dict | None = None,
    total_equity: float | None = None,
    futures_cash: float | None = None,
    maintenance_ratio: float | None = None,
) -> str:
    """선물 포지션을 HTML 조각으로 반환. 빈 리스트면 빈 문자열.

    Args:
      positions: futures_positions.json 의 dict 리스트
      current_prices: 두 가지 포맷 지원 (호환):
        - {"005930|202606": {"price": ..., "change_pct": ..., "source": ...}, ...}
        - {"005930": <price>, ...}  (구버전 — change_pct 없음)
      maintenance_ratio: 유지증거금 ÷ 위탁증거금 비율(실측). 주어지면 마진콜
        기준선으로 `위탁증거금 합 × 이 비율`을 쓰고, 없으면 위탁 × 2/3 추정치.
    """
    active = [p for p in positions if p.get("contracts", 0) > 0]
    if not active:
        return ""

    current_prices = current_prices or {}
    rows_html = ""
    total_unrealized = 0.0
    total_margin = 0.0
    total_maint = 0.0  # 유지증거금 합 (마진콜 기준선)
    total_notional = 0.0  # 현재가 × 계약수 × 승수 합 (= 선물 평가액)
    total_notional_signed = 0.0  # 롱 +, 숏 - (마진콜 하락률 계산용)
    sources_seen: set[str] = set()

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

        sym = p.get("symbol", "")
        key = f"{sym}|{cm}"
        entry = current_prices.get(key)
        if entry is None:
            entry = current_prices.get(sym)
        if isinstance(entry, dict):
            cur = entry.get("price")
            change_pct = entry.get("change_pct")
            source = entry.get("source")
            u_price = entry.get("underlying_price")
            u_change = entry.get("underlying_change_pct")
            if source:
                sources_seen.add(source)
        else:
            cur = entry
            change_pct = None
            source = None
            u_price = None
            u_change = None

        if cur is None:
            cur_display = "-"
            unrealized = 0.0
            unrealized_pct = 0.0
        else:
            cur_display = f"{format_number(int(cur))}원"
            if change_pct is not None:
                cp_class = "profit" if change_pct >= 0 else "loss"
                cp_sign = "+" if change_pct >= 0 else ""
                cur_display += (
                    f'<br><small class="{cp_class}">'
                    f'({cp_sign}{change_pct:.2f}%)</small>'
                )
            sign = 1 if direction == "long" else -1
            unrealized = (cur - avg) * contracts * mult * sign
            notional = avg * contracts * mult
            unrealized_pct = (unrealized / notional * 100) if notional else 0.0
            total_notional += float(cur) * contracts * mult
            total_notional_signed += float(cur) * contracts * mult * sign

        # 기초자산 셀
        if u_price is None:
            underlying_display = "-"
        else:
            underlying_display = f"{format_number(int(u_price))}원"
            if u_change is not None:
                uc_class = "profit" if u_change >= 0 else "loss"
                uc_sign = "+" if u_change >= 0 else ""
                underlying_display += (
                    f'<br><small class="{uc_class}">'
                    f'({uc_sign}{u_change:.2f}%)</small>'
                )

        total_unrealized += unrealized
        total_margin += margin
        maint = float(p.get("maintenance_margin", 0) or 0)
        if maint <= 0:
            maint = margin * MAINTENANCE_RATIO
        total_maint += maint

        days = _days_to_expiry(p.get("expiry_date", ""))
        if days is None:
            d_display = "-"
            d_class = ""
        else:
            d_display = f"D-{days}" if days >= 0 else f"만기경과 {-days}일"
            d_class = "loss" if days <= 3 else ""

        pnl_class = "profit" if unrealized >= 0 else "loss"
        pnl_sign = "+" if unrealized >= 0 else ""
        sector = p.get("sector", "") or "기타"
        rows_html += f"""
        <tr>
          <td>{sector}</td>
          <td>{name}</td>
          <td>{direction_kr}</td>
          <td class="num">{contracts}계약</td>
          <td>{cm_label}</td>
          <td class="num {d_class}">{d_display}</td>
          <td class="num">{format_number(int(avg))}원</td>
          <td class="num">{cur_display}</td>
          <td class="num">{underlying_display}</td>
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

    # 평가액 대비 증거금(레버리지 노출률) — 내가 굴리는 자산 중 선물 증거금으로 묶여 있는 비율.
    if total_equity and total_equity > 0:
        equity_ratio = total_margin / total_equity * 100
    else:
        equity_ratio = 0.0
    # 30% 넘으면 위험 신호, 10% 이하면 여유, 그 사이는 보통.
    if equity_ratio >= 30:
        equity_class = "loss"
    elif equity_ratio > 0 and equity_ratio < 10:
        equity_class = "profit"
    else:
        equity_class = ""

    # 시세 출처 안내 — KIS 실시간이 잡혔으면 추정치 문구 생략
    if sources_seen == {"kis"} or sources_seen == {"kis", "manual"} or sources_seen == {"manual"}:
        quote_note = ""
    elif "kis" in sources_seen and "underlying" in sources_seen:
        quote_note = (
            '<div style="font-size:11px;color:#888;margin-top:4px">'
            '일부 종목은 기초자산 현재가 기준 추정치 (KIS 시세 조회 실패).'
            '</div>'
        )
    else:
        quote_note = (
            '<div style="font-size:11px;color:#888;margin-top:4px">'
            '시세는 기초자산 현재가 기준 추정치. 정확한 선물가는 텔레그램에서 \'선물시세\' 입력.'
            '</div>'
        )

    # 선물 예수금 (futures_cash 인자가 들어오면 카드로 표기)
    fc_val = float(futures_cash) if futures_cash else 0.0
    deposit_total = fc_val + total_margin  # 선물 예탁금 = 가용 + 위탁증거금

    # 마진콜(추가증거금) 예상 — 보유 선물 현재가가 일괄로 몇 % 움직이면 순자산이
    # 유지증거금 밑으로 내려가는지. 순자산 = 예탁금 + 현재 미실현.
    # 유지증거금은 실측 비율(maintenance_ratio)이 있으면 `위탁 × 비율`, 없으면 위탁×2/3.
    if maintenance_ratio and maintenance_ratio > 0:
        maint_for_call = total_margin * float(maintenance_ratio)
        maint_note = f"위탁 × {float(maintenance_ratio) * 100:.1f}%"
    else:
        maint_for_call = total_maint
        maint_note = "위탁 × 2/3 가정"
    equity_now = deposit_total + total_unrealized
    margin_call_banner = _margin_call_banner(
        equity_now, maint_for_call, total_margin, fc_val,
        total_unrealized, total_notional_signed, maint_note,
    )

    futures_cash_card = (
        "<div class='card'>"
        "<div class='label'>선물 예수금</div>"
        f"<div class='value'>{format_number(int(fc_val))}원</div>"
        f"<div class='sub' style='color:#9ca3af'>예탁금 총 {format_number(int(deposit_total))}원</div>"
        "</div>"
    ) if fc_val > 0 else ""

    return f"""
  <div class="section-title" style="margin-top:40px">선물 포지션</div>
  {quote_note}
  <div class="cards" style="grid-template-columns:repeat(3,1fr)">
    <div class="card">
      <div class="label">포지션 수</div>
      <div class="value">{len(active)}개</div>
    </div>
    <div class="card">
      <div class="label">선물 평가액 <span style="font-size:10px;color:#888">(notional)</span></div>
      <div class="value">{format_number(int(total_notional))}원</div>
    </div>
    <div class="card">
      <div class="label">미실현 손익</div>
      <div class="value {total_class}">{total_sign}{format_number(int(total_unrealized))}원</div>
    </div>
    {futures_cash_card}
    <div class="card">
      <div class="label">위탁증거금</div>
      <div class="value">{format_number(int(total_margin))}원</div>
      <div class="sub" style="color:#9ca3af">잠식 <span class="{burn_class}">{margin_burn:.1f}%</span></div>
    </div>
    <div class="card">
      <div class="label">평가액 대비 증거금</div>
      <div class="value {equity_class}">{equity_ratio:.1f}%</div>
      <div class="sub" style="color:#9ca3af">전체 자산 중 증거금 비중</div>
    </div>
  </div>
  {margin_call_banner}
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>섹터</th><th>종목</th><th>방향</th><th>계약수</th><th>결제월</th><th>만기</th>
          <th>평균진입</th><th>현재가</th><th>기초자산</th><th>미실현</th><th>증거금</th><th>사유</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
"""
