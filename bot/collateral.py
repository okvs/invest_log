"""신용 담보비율 시뮬레이션 — `담보` 명령.

담보비율 = (현물 평가금 + 예수금) ÷ 신용융자금 × 100.
현재 보유/시세로 채운 인터랙티브 HTML(슬라이더로 하락률·금액 직접 시뮬)을 텔레그램
document 로 전송하고, 캡션에 현재 담보비율·반대매매 도달 하락률을 요약한다.

선물 증거금은 별개(신용=현물 기준)이므로 여기 포함하지 않는다.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot.formatters import _resolve_tickers, fetch_current_quotes, format_number
from storage.json_store import load_account, load_holdings

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
MAINT_PCT = 140   # 반대매매선 기본값
WARN_PCT = 170    # 주의선 기본값


def compute_collateral(
    holdings: list[dict], quotes: dict, name_to_ticker: dict, cash: float,
) -> dict:
    """현물 평가금/신용융자금/종목별 내역 계산 (순수). 평가금 내림차순."""
    total_eval = 0.0
    total_loan = 0.0
    stocks: list[dict] = []
    for h in holdings:
        if h.get("quantity", 0) <= 0:
            continue
        name = h["name"]
        ticker = h.get("ticker") or name_to_ticker.get(name, "")
        qty = int(h.get("quantity", 0) or 0)
        cur = (quotes.get(ticker) or {}).get("price")
        ev = cur * qty if cur else float(h.get("total_invested", 0) or 0)
        loan = float(h.get("credit_loan", 0) or 0)
        total_eval += ev
        total_loan += loan
        stocks.append({"n": name, "ev": round(ev), "loan": round(loan)})
    stocks.sort(key=lambda s: s["ev"], reverse=True)
    return {
        "eval": round(total_eval),
        "cash": round(cash),
        "loan": round(total_loan),
        "stocks": stocks,
    }


def collateral_ratio(data: dict) -> float:
    """(평가금+예수금)/융자금 × 100. 융자 0이면 inf."""
    return (data["eval"] + data["cash"]) / data["loan"] * 100 if data["loan"] > 0 else float("inf")


def liquidation_drop_pct(data: dict, maint: float = MAINT_PCT) -> float | None:
    """반대매매(maint%) 도달까지 필요한 현물 일괄 하락률(%) — 보통 음수. 불가 시 None."""
    if data["loan"] <= 0 or data["eval"] <= 0:
        return None
    return ((maint / 100 * data["loan"] - data["cash"]) / data["eval"] - 1) * 100


def build_collateral_html(data: dict, asof: str = "") -> io.BytesIO:
    """현재 수치를 주입한 시뮬레이션 HTML 을 BytesIO 로 반환."""
    html = (
        _TEMPLATE
        .replace("__DEF_JSON__", json.dumps({k: data[k] for k in ("eval", "cash", "loan")}))
        .replace("__STOCKS_JSON__", json.dumps(data["stocks"], ensure_ascii=False))
        .replace("__MAINT__", str(MAINT_PCT))
        .replace("__WARN__", str(WARN_PCT))
        .replace("__ASOF__", asof)
    )
    buf = io.BytesIO(html.encode("utf-8"))
    buf.name = "collateral_sim.html"
    return buf


def _gather() -> dict:
    """보유 현물 + 시세 + 예수금 조회 후 담보 데이터 계산 (블로킹)."""
    active = [h for h in load_holdings() if h.get("quantity", 0) > 0]
    name_to_ticker, _ = _resolve_tickers(active)
    tickers = list({t for t in name_to_ticker.values() if t})
    quotes = fetch_current_quotes(tickers) if tickers else {}
    cash = float(load_account().get("cash", 0) or 0)
    return compute_collateral(active, quotes, name_to_ticker, cash)


def _caption(data: dict) -> str:
    ev, cash, loan = data["eval"], data["cash"], data["loan"]
    if loan <= 0:
        return ("📊 담보비율 — 신용융자 없음 (해당사항 없음).\n"
                "첨부 HTML에서 금액을 직접 입력해 가정 시뮬레이션할 수 있어요.")
    ratio = collateral_ratio(data)
    d = liquidation_drop_pct(data)
    lines = [
        f"📊 담보비율 {ratio:.1f}%",
        f"(평가금 {format_number(ev)} + 예수금 {format_number(cash)}) / 융자 {format_number(loan)}",
    ]
    if d is not None:
        lines.append(
            f"반대매매({MAINT_PCT}%) 도달 = 현물 {d:.1f}% 하락 시"
            if d > -100 else
            f"현물 전액 사라져도 {MAINT_PCT}% 위 — 사실상 반대매매 불가"
        )
    lines.append("첨부 HTML: 슬라이더로 하락률 시뮬 · 금액 직접 입력 가능")
    return "\n".join(lines)


async def collateral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`담보` — 신용 담보비율 시뮬레이션 HTML 전송."""
    active = [h for h in load_holdings() if h.get("quantity", 0) > 0]
    if not active:
        await update.message.reply_text("보유 현물이 없습니다.")
        return
    try:
        data = await asyncio.to_thread(_gather)
    except Exception:
        logger.warning("담보비율 계산 실패", exc_info=True)
        await update.message.reply_text("시세 조회에 실패했어요. 잠시 후 다시 시도해주세요.")
        return

    asof = datetime.now().strftime("%Y-%m-%d %H:%M")
    buf = build_collateral_html(data, asof=asof)
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        fp = REPORTS_DIR / f"collateral_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        fp.write_bytes(buf.getvalue())
        out = io.BytesIO(buf.getvalue())
        out.name = fp.name
        buf = out
    except Exception:
        logger.warning("담보 HTML 로컬 저장 실패", exc_info=True)

    await update.message.reply_document(document=buf, caption=_caption(data))


_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>담보비율 시뮬레이션</title>
<style>
  :root{
    --bg:#0f0f14; --card:#181820; --line:#2a2a35; --txt:#e6e6ea; --sub:#8b8b98;
    --green:#22c55e; --amber:#fbbf24; --red:#ef4444; --blue:#4A90D9;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font-family:'Apple SD Gothic Neo',-apple-system,BlinkMacSystemFont,sans-serif;
    padding:22px 16px 48px;max-width:760px;margin:0 auto;}
  h1{font-size:20px;margin:0 0 4px}
  .formula{background:#13131a;border:1px solid var(--line);border-radius:10px;
    padding:12px 14px;font-size:14px;color:var(--sub);margin:14px 0 20px}
  .formula b{color:var(--txt)}
  .asof{display:inline-block;background:#1c2533;color:var(--blue);
    border:1px solid #28384f;border-radius:6px;padding:2px 8px;font-size:11px;margin-left:6px;vertical-align:middle}
  .inputs{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px}
  .ifield{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .ifield label{font-size:11px;color:var(--sub);letter-spacing:.4px}
  .inrow{display:flex;align-items:baseline;gap:5px;margin-top:6px}
  .inrow input{flex:1;min-width:0;background:#0e0e14;border:1px solid var(--line);color:var(--txt);
    border-radius:7px;padding:7px 8px;font-size:16px;font-weight:700;text-align:right;
    font-variant-numeric:tabular-nums}
  .inrow input:focus{outline:none;border-color:var(--blue)}
  .inrow span{font-size:12px;color:var(--sub)}
  #reset{margin:2px 0 20px;background:#1f1f2a;color:var(--sub);border:1px solid var(--line);
    border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer}
  #reset:hover{color:var(--txt);border-color:#444}
  .ratio-box{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:22px;text-align:center;margin-bottom:18px;transition:border-color .2s}
  .ratio-box .cap{font-size:12px;color:var(--sub)}
  .ratio-num{font-size:52px;font-weight:800;line-height:1.1;margin:4px 0;font-variant-numeric:tabular-nums}
  .ratio-status{font-size:14px;font-weight:700}
  .ratio-eval{font-size:12px;color:var(--sub);margin-top:8px}
  .bar-wrap{margin:18px 0 8px}
  .bar{position:relative;height:26px;border-radius:7px;overflow:hidden;background:#222}
  .bar .marker{position:absolute;top:-6px;bottom:-6px;width:3px;background:#fff;
    box-shadow:0 0 6px rgba(0,0,0,.6);transition:left .15s}
  .bar-ticks{position:relative;height:32px;margin-top:5px;font-size:10px;color:var(--sub)}
  .bar-ticks .tk{position:absolute;transform:translateX(-50%);text-align:center;white-space:nowrap;line-height:1.35}
  .bar-ticks .tk.l{left:0;transform:none;text-align:left}
  .bar-ticks .tk.r{right:0;left:auto;transform:none;text-align:right}
  .bar-ticks .tk b{font-size:11px}
  .control{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:20px}
  .control label{font-size:13px;color:var(--sub)}
  .control .drop-val{font-size:22px;font-weight:800;color:var(--txt);float:right}
  input[type=range]{width:100%;margin-top:14px;accent-color:var(--blue)}
  .row2{display:flex;gap:14px;align-items:center;margin-top:14px;flex-wrap:wrap}
  .row2 .mini{font-size:12px;color:var(--sub)}
  .row2 input[type=number]{width:64px;background:#0f0f15;border:1px solid var(--line);
    color:var(--txt);border-radius:6px;padding:4px 6px;font-size:13px}
  .crit{font-size:13px;margin-top:12px;color:var(--amber)}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
  th,td{padding:9px 10px;text-align:right;border-bottom:1px solid #1c1c25;font-variant-numeric:tabular-nums}
  th{color:var(--sub);font-size:11px;text-transform:uppercase;letter-spacing:.5px;text-align:right}
  th:first-child,td:first-child{text-align:left}
  .pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:700}
  .sec-title{font-size:14px;font-weight:700;margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
  .foot{font-size:11px;color:var(--sub);margin-top:22px;line-height:1.7}
  .g{color:var(--green)}.a{color:var(--amber)}.r{color:var(--red)}
</style>
</head>
<body>
  <h1>담보비율 시뮬레이션<span class="asof">기준 __ASOF__</span></h1>
  <div class="formula">
    <b>담보비율 = (현물 평가금 + 예수금) ÷ 신용융자금 × 100</b><br>
    아래 값을 직접 입력하고, 슬라이더로 하락률을 바꿔 담보비율을 시뮬레이션하세요.
    140% 밑이면 반대매매(강제청산) 위험.
  </div>

  <div class="inputs">
    <div class="ifield"><label>현물 평가금</label>
      <div class="inrow"><input id="in-eval" type="text" inputmode="numeric"><span>원</span></div></div>
    <div class="ifield"><label>예수금</label>
      <div class="inrow"><input id="in-cash" type="text" inputmode="numeric"><span>원</span></div></div>
    <div class="ifield"><label>신용융자금</label>
      <div class="inrow"><input id="in-loan" type="text" inputmode="numeric"><span>원</span></div></div>
  </div>
  <button id="reset" type="button">↺ 실제값 불러오기</button>

  <div class="ratio-box" id="ratioBox">
    <div class="cap">시뮬레이션 담보비율</div>
    <div class="ratio-num" id="ratioNum">—</div>
    <div class="ratio-status" id="ratioStatus">—</div>
    <div class="ratio-eval" id="ratioEval"></div>
    <div class="bar-wrap">
      <div class="bar" id="bar"><div class="marker" id="marker"></div></div>
      <div class="bar-ticks" id="barTicks"></div>
    </div>
  </div>

  <div class="control">
    <label>전체 현물 일괄 하락률 <span class="drop-val" id="dropVal">0%</span></label>
    <input type="range" id="slider" min="-40" max="0" step="5" value="0">
    <div class="row2">
      <span class="mini">반대매매선</span>
      <input type="number" id="maint" value="__MAINT__" min="100" max="200" step="5"><span class="mini">%</span>
      <span class="mini">· 주의선</span>
      <input type="number" id="warn" value="__WARN__" min="100" max="300" step="5"><span class="mini">%</span>
    </div>
    <div class="crit" id="crit"></div>
  </div>

  <div class="sec-title">하락률별 담보비율</div>
  <table id="scenario">
    <thead><tr><th>현물 하락률</th><th>평가금</th><th>담보비율</th><th>상태</th></tr></thead>
    <tbody></tbody>
  </table>

  <div class="sec-title">신용융자 보유 종목 (현재 실제 · 참고)</div>
  <table id="byStock">
    <thead><tr><th>종목</th><th>평가금</th><th>융자금</th></tr></thead>
    <tbody></tbody>
  </table>

  <div class="foot">
    · 현재 보유·시세 기준으로 자동 생성. 금액을 직접 입력해 가정 시뮬레이션 가능("실제값 불러오기"로 복원).<br>
    · 가정: 전 종목이 동일 비율로 하락(개별 하락률은 추후 옵션). 선물 증거금은 별개(여기 미포함).
  </div>

<script>
  const DEF = __DEF_JSON__;
  const STOCKS = __STOCKS_JSON__;

  const won = v => Math.round(v).toLocaleString('ko-KR') + '원';
  const $ = id => document.getElementById(id);
  const getN = id => { const v = parseFloat(String($(id).value).replace(/[^\d.]/g,'')); return isFinite(v) ? v : 0; };

  function attachComma(el){
    el.addEventListener('input', ()=>{
      const caret = el.selectionStart;
      const before = el.value.slice(0, caret).replace(/[^\d]/g,'').length;
      const digits = el.value.replace(/[^\d]/g,'');
      const formatted = digits ? Number(digits).toLocaleString('ko-KR') : '';
      el.value = formatted;
      let pos = 0, seen = 0;
      while (pos < formatted.length && seen < before){ if (/\d/.test(formatted[pos])) seen++; pos++; }
      el.setSelectionRange(pos, pos);
      render();
    });
  }

  function ratioAt(dropPct, ev, cash, loan){
    if (loan <= 0) return Infinity;
    return (ev*(1+dropPct/100) + cash) / loan * 100;
  }
  function cls(r, maint, warn){ return r <= maint ? 'r' : (r <= warn ? 'a' : 'g'); }
  function statusTxt(r, maint, warn){
    if (!isFinite(r)) return '🟢 융자 없음 (담보비율 ∞)';
    if (r <= maint) return '🔴 반대매매 위험';
    if (r <= warn)  return '🟡 주의 (추가담보 구간)';
    return '🟢 안전';
  }
  const COLOR = {r:'var(--red)', a:'var(--amber)', g:'var(--green)'};

  function render(){
    const d = +$('slider').value;
    const maint = +$('maint').value, warn = +$('warn').value;
    const ev = getN('in-eval'), cash = getN('in-cash'), loan = getN('in-loan');
    const r = ratioAt(d, ev, cash, loan);
    const c = isFinite(r) ? cls(r, maint, warn) : 'g';

    $('dropVal').textContent = d + '%';
    $('ratioNum').textContent = isFinite(r) ? r.toFixed(1) + '%' : '∞';
    $('ratioNum').style.color = COLOR[c];
    $('ratioStatus').textContent = statusTxt(r, maint, warn);
    $('ratioStatus').style.color = COLOR[c];
    $('ratioBox').style.borderColor = COLOR[c];
    $('ratioEval').textContent =
      '평가금 ' + won(ev*(1+d/100)) + ' (' + d + '%) · 예수금 ' + won(cash) + ' · 융자 ' + won(loan);

    const posOf = v => Math.max(0, Math.min(100, (v-100)/(250-100)*100));
    const mp = posOf(maint), wp = posOf(warn);
    $('bar').style.background =
      `linear-gradient(90deg,var(--red) 0%,var(--red) ${mp}%,var(--amber) ${mp}%,var(--amber) ${wp}%,var(--green) ${wp}%,var(--green) 100%)`;
    $('marker').style.left = posOf(isFinite(r) ? r : 250) + '%';
    $('barTicks').innerHTML =
      `<div class="tk l">100%</div>` +
      `<div class="tk r"><b class="g">250%+</b><br>안전</div>` +
      `<div class="tk" style="left:${mp}%"><b class="r">${maint}%</b><br>반대매매</div>` +
      `<div class="tk" style="left:${wp}%"><b class="a">${warn}%</b><br>주의</div>`;

    if (loan <= 0 || ev <= 0){
      $('crit').textContent = '※ 융자/평가금 입력 시 반대매매 도달 하락률 계산';
    } else {
      const dCrit = ((maint/100*loan - cash)/ev - 1) * 100;
      $('crit').textContent = dCrit <= -100
        ? `※ 현물이 전액(-100%) 사라져도 담보비율 ${maint}% 위 — 사실상 반대매매 불가`
        : `※ 담보비율 ${maint}%(반대매매) 도달 = 전체 현물 ${dCrit.toFixed(1)}% 하락 시`;
    }

    const tb = $('scenario').querySelector('tbody'); tb.innerHTML='';
    [0,-5,-10,-15,-20,-25,-30,-35,-40].forEach(dd=>{
      const rr = ratioAt(dd, ev, cash, loan), cc = isFinite(rr) ? cls(rr, maint, warn) : 'g';
      tb.insertAdjacentHTML('beforeend',
        `<tr><td>${dd}%</td><td>${won(ev*(1+dd/100))}</td>`+
        `<td class="${cc}">${isFinite(rr)?rr.toFixed(1)+'%':'∞'}</td>`+
        `<td><span class="pill" style="background:${COLOR[cc]}22;color:${COLOR[cc]}">`+
        `${!isFinite(rr)?'안전':cc==='r'?'반대매매':cc==='a'?'주의':'안전'}</span></td></tr>`);
    });
  }

  function setDefaults(){
    $('in-eval').value = DEF.eval.toLocaleString('ko-KR');
    $('in-cash').value = DEF.cash.toLocaleString('ko-KR');
    $('in-loan').value = DEF.loan.toLocaleString('ko-KR');
    render();
  }

  const sb = $('byStock').querySelector('tbody');
  STOCKS.forEach(s=> sb.insertAdjacentHTML('beforeend',
    `<tr><td>${s.n}</td><td>${won(s.ev)}</td><td>${s.loan?won(s.loan):'—'}</td></tr>`));

  ['in-eval','in-cash','in-loan'].forEach(id=> attachComma($(id)));
  ['slider','maint','warn'].forEach(id=> $(id).addEventListener('input', render));
  $('reset').addEventListener('click', setDefaults);
  setDefaults();
</script>
</body>
</html>
"""
