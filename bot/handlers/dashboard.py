import asyncio
import base64
import io
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

from telegram import Update
from telegram.ext import ContextTypes

from bot.formatters import format_dashboard, fetch_current_prices, format_number, _resolve_tickers
from bot.html_report import build_html_report
from bot import firebase_publish
from parsers.input_parser import search_stocks
from storage.json_store import (
    load_account,
    load_futures_positions,
    load_futures_transactions,
    load_holdings,
    load_ticker_map,
    load_transactions,
    save_holdings,
    save_ticker_map,
)

logger = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096

# --- PWA (홈 화면에 설치하면 독립 창의 웹앱처럼 동작) ---
_PWA_DIR = Path(__file__).resolve().parent.parent / "pwa"
_PWA_HEAD = (
    '<meta http-equiv="refresh" content="300">\n'  # 5분마다 자동 새로고침(재발행 반영)
    '<link rel="manifest" href="manifest.webmanifest">\n'
    '<meta name="theme-color" content="#e9efe9" media="(prefers-color-scheme: light)">\n'
    '<meta name="theme-color" content="#0d1411" media="(prefers-color-scheme: dark)">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
    '<meta name="apple-mobile-web-app-title" content="투자">\n'
    '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
)
_PWA_MANIFEST = json.dumps(
    {
        "name": "내 투자 현황",
        "short_name": "투자",
        "id": "./",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f0f14",
        "theme_color": "#0f0f14",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    },
    ensure_ascii=False,
).encode("utf-8")


def _inject_pwa(html: bytes) -> bytes:
    """HTML <head>에 PWA 메타/매니페스트 링크를 주입(중복 방지)."""
    text = html.decode("utf-8")
    if "manifest.webmanifest" in text or "</head>" not in text:
        return html
    return text.replace("</head>", _PWA_HEAD + "</head>", 1).encode("utf-8")


# --- 하단 탭바(앱 스타일) ---
_TAB_CSS = """
<style>
  body { padding-bottom: calc(98px + env(safe-area-inset-bottom)) !important; }
  .tab-panel { display:none; }
  .tab-panel.active { display:block; }

  /* 하단 플로팅 탭바 (앱 스타일, 반투명+블러).
     래퍼를 bottom:0 에 고정 — iOS 동적 툴바(스크롤 시 주소창 접힘)에서도
     기준점이 흔들리지 않아 현황 탭에서 바가 내려가지 않는다. 가시 바는
     래퍼 내부 패딩으로 홈 인디케이터 위에 띄운다. */
  .tabbar-wrap { position:fixed; left:0; right:0; bottom:0; z-index:1000;
            padding:0 12px calc(12px + env(safe-area-inset-bottom));
            pointer-events:none; }
  .tabbar { pointer-events:auto; display:flex; gap:4px; padding:8px;
            background:var(--tabbar-bg);
            -webkit-backdrop-filter:saturate(160%) blur(14px);
            backdrop-filter:saturate(160%) blur(14px);
            border:1px solid var(--border);
            border-radius:24px; box-shadow:var(--shadow);
            max-width:520px; margin:0 auto; }
  .tabbar button { flex:1; background:none; border:none; cursor:pointer;
            color:var(--text-dim); font-size:11px; font-weight:600;
            display:flex; flex-direction:column; align-items:center; gap:5px;
            padding:9px 4px; border-radius:16px; font-family:inherit;
            -webkit-tap-highlight-color:transparent;
            transition:background .15s, color .15s; }
  .tabbar button svg { width:22px; height:22px; fill:none; stroke:currentColor;
            stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
  .tabbar button.active { color:var(--accent); background:var(--accent-soft); }

  /* 차트 PNG는 다크 프레임 카드로(이미지 자체가 다크 렌더라 라이트모드에서도 일관) */
  .imgcard { background:#0e1512; border-radius:14px; padding:12px;
             max-width:960px; margin:0 auto; box-shadow:var(--shadow); }
  .imgcard img { width:100%; height:auto; border-radius:8px; display:block; }

  .hist-list { max-width:760px; margin:0 auto; }
  .hist-row { background:var(--card); border:1px solid var(--border);
              border-radius:12px; padding:11px 14px; margin-bottom:8px;
              box-shadow:var(--shadow); }
  .hist-row .r1 { display:flex; align-items:center; gap:8px; }
  .hist-row .nm { font-weight:600; color:var(--text-strong); font-size:14px; flex:1;
                  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .hist-row .pnl { font-size:13px; font-weight:700; white-space:nowrap; }
  .hist-row .r2 { font-size:12px; color:var(--text-dim); margin-top:5px; }
  .hbadge { font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; white-space:nowrap; }
  .b-buy { background:rgba(216,60,60,.16); color:#d83c3c; }
  .b-sell { background:rgba(31,122,82,.16); color:var(--accent); }
  .b-open { background:rgba(139,92,246,.16); color:#8b5cf6; }
  .b-close { background:rgba(21,146,74,.16); color:var(--profit); }
  .hist-date { color:var(--text-dim); font-size:12px; font-weight:700;
               max-width:760px; margin:18px auto 8px; }
</style>
"""

_TAB_SCRIPT = """
<script>
(function(){
  var KEY='invest_active_tab';
  function show(id){
    document.querySelectorAll('.tab-panel').forEach(function(p){p.classList.toggle('active',p.id===id);});
    document.querySelectorAll('.tabbar button').forEach(function(b){b.classList.toggle('active',b.dataset.tab===id);});
    try{localStorage.setItem(KEY,id);}catch(e){}
  }
  document.querySelectorAll('.tabbar button[data-tab]').forEach(function(b){
    b.addEventListener('click',function(){show(b.dataset.tab);window.scrollTo(0,0);});
  });
  var s=null; try{s=localStorage.getItem(KEY);}catch(e){}
  if(s&&document.getElementById(s)){show(s);}
})();
</script>
"""

_IC_STATUS = (
    '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7.5" height="7.5" rx="1.6"/>'
    '<rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6"/>'
    '<rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6"/>'
    '<rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6"/></svg>'
)
_IC_GRAPH = (
    '<svg viewBox="0 0 24 24"><polyline points="3 16 9 10 13 14 21 6"/>'
    '<polyline points="15 6 21 6 21 12"/></svg>'
)
_IC_HISTORY = (
    '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 2.6-6.4"/>'
    '<polyline points="3 4 3 9 8 9"/><polyline points="12 8 12 12 15 13.5"/></svg>'
)
_IC_STRATEGY = (
    '<svg viewBox="0 0 24 24"><path d="M9.5 18h5"/><path d="M10 21h4"/>'
    '<path d="M12 3a6 6 0 0 0-3.8 10.7c.7.6 1.3 1.4 1.3 2.3h5c0-.9.6-1.7 1.3-2.3'
    'A6 6 0 0 0 12 3z"/></svg>'
)
_TABBAR_HTML = (
    '<div class="tabbar-wrap"><nav class="tabbar">'
    f'<button data-tab="tab-status" class="active">{_IC_STATUS}현황</button>'
    f'<button data-tab="tab-graph">{_IC_GRAPH}그래프</button>'
    f'<button data-tab="tab-history">{_IC_HISTORY}기록</button>'
    f'<button data-tab="tab-backtest">{_IC_STRATEGY}전략</button>'
    '</nav></div>'
)


def _fmt_won(v) -> str:
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return "-"


def _build_history_html() -> str:
    """현물(매수/매도)+선물(진입/청산) 거래를 최신순으로 카드 리스트 렌더."""
    items: list[dict] = []

    for t in load_transactions():
        is_buy = t.get("type") == "buy"
        items.append({
            "date": str(t.get("date", "")),
            "cls": "b-buy" if is_buy else "b-sell",
            "label": "매수" if is_buy else "매도",
            "name": t.get("name", ""),
            "detail": f"{int(t.get('quantity', 0)):,}주 × {_fmt_won(t.get('price'))}원",
            "pnl": None if is_buy else t.get("profit_loss"),
            "pnl_pct": None if is_buy else t.get("profit_loss_pct"),
        })

    fut_label = {"open": "선물진입", "close": "선물청산",
                 "roll_open": "롤(진입)", "roll_close": "롤(청산)"}
    for t in load_futures_transactions():
        typ = t.get("type", "")
        is_open = typ in ("open", "roll_open")
        items.append({
            "date": str(t.get("date", "")),
            "cls": "b-open" if is_open else "b-close",
            "label": fut_label.get(typ, typ),
            "name": t.get("name", ""),
            "detail": f"{int(t.get('contracts', 0)):,}계약 × {_fmt_won(t.get('price'))}원",
            "pnl": None if is_open else t.get("pnl"),
            "pnl_pct": None if is_open else t.get("pnl_pct"),
        })

    items = [it for it in items if it["date"]]
    items.sort(key=lambda x: x["date"], reverse=True)
    items = items[:200]

    if not items:
        return '<div style="color:var(--text-dim);text-align:center;padding:48px 24px;">거래 내역이 없습니다.</div>'

    out: list[str] = ['<div class="hist-list">']
    cur_day = None
    for it in items:
        day = it["date"][:10]
        if day != cur_day:
            cur_day = day
            out.append(f'<div class="hist-date">{day}</div>')
        pnl_html = ""
        if it["pnl"] is not None:
            pnl = float(it["pnl"])
            sign = "+" if pnl >= 0 else ""
            cls = "profit" if pnl >= 0 else "loss"
            pct = it.get("pnl_pct")
            pct_s = f" ({sign}{float(pct):.1f}%)" if pct is not None else ""
            pnl_html = f'<span class="pnl {cls}">{sign}{_fmt_won(pnl)}원{pct_s}</span>'
        tm = it["date"][11:16]
        out.append(
            '<div class="hist-row">'
            '<div class="r1">'
            f'<span class="hbadge {it["cls"]}">{it["label"]}</span>'
            f'<span class="nm">{it["name"]}</span>'
            f'{pnl_html}'
            '</div>'
            f'<div class="r2">{tm} · {it["detail"]}</div>'
            '</div>'
        )
    out.append("</div>")
    return "".join(out)


async def _graph_img_html() -> str:
    """자산그래프(전체 기간) PNG 를 base64 <img> 로. 실패 시 안내 문구."""
    try:
        from bot.asset_history import render_asset_graph
        from bot.handlers.asset_graph import _balance_nav

        target_nav = await _balance_nav()
        buf = await asyncio.to_thread(render_asset_graph, target_nav, None)
        if buf is not None:
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return (
                '<div class="imgcard">'
                f'<img src="data:image/png;base64,{b64}" alt="자산 그래프"></div>'
            )
    except Exception:
        logger.warning("자산그래프 렌더 실패", exc_info=True)
    return '<div style="color:var(--text-dim);text-align:center;padding:48px 24px;">자산 기록이 없습니다.</div>'


_BACKTEST_TAB_TTL = 3 * 3600  # 백테스트 탭 캐시 유효시간(초) — 과거시세 다운로드가 무거워 재계산 제한


def _bt_short(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f}억"
    if abs(x) >= 1e7:
        return f"{x / 1e7:.1f}천만"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.0f}만"
    return f"{x:,.0f}원"


def _render_backtest_tab(res: dict) -> str:
    """run_backtest 결과 → 탭 본문 HTML(요약 + PNG base64)."""
    rows = res["rows"]
    higher = res.get("higher") or []
    nav_now = res["nav_actual_today"]
    gross = res.get("nav_actual_today_gross", nav_now)
    cur_credit = res.get("cur_credit", 0)
    initial = res["initial"]
    pct = (nav_now / initial - 1) * 100 if initial else 0.0

    def d(x):
        return x.strftime("%m-%d") if hasattr(x, "strftime") else str(x)

    head = (
        '<div class="card" style="max-width:760px;margin:0 auto 12px;text-align:left;padding:16px 18px;">'
        '<div style="font-weight:700;color:var(--text-strong);margin-bottom:8px;">포트폴리오 동결 백테스트 '
        '<span style="color:var(--text-dim);font-weight:400;font-size:12px;">(현물+선물 · 신용 제외)</span></div>'
        f'<div style="font-size:13px;color:var(--text);line-height:1.9;">'
        f'기간 {d(rows[0]["date"])} ~ {d(rows[-1]["date"])} · 거래일 {len(rows)}일<br>'
        f'현재 순자산 <b style="color:var(--text-strong);">{_bt_short(nav_now)}</b> '
        f'(총자산 {_bt_short(gross)} − 신용 {_bt_short(cur_credit)})<br>'
        f'초기자본 {_bt_short(initial)} · '
        f'<span class="{"profit" if pct >= 0 else "loss"}">{pct:+.1f}%</span>'
        '</div>'
    )
    if higher:
        tops = "".join(
            f'<div style="font-size:12px;color:var(--text-dim);margin-top:3px;">'
            f'{d(r["date"])} · {_bt_short(r["nav_frozen_today"])} '
            f'(+{_bt_short(r["nav_frozen_today"] - nav_now)})</div>'
            for r in higher[:5]
        )
        more = f'<div style="font-size:12px;color:var(--text-dim);margin-top:3px;">… +{len(higher) - 5}건</div>' if len(higher) > 5 else ""
        head += (
            f'<div style="margin-top:10px;color:#f0b03a;font-weight:700;font-size:13px;">'
            f'★ 현재 순자산 초과 거래일 {len(higher)}/{len(rows)}건</div>{tops}{more}'
        )
    else:
        head += (
            '<div style="margin-top:10px;color:var(--profit);font-weight:700;font-size:13px;">'
            '→ 현재 순자산이 ALL-TIME HIGH (모든 동결 시나리오보다 높음)</div>'
        )
    head += "</div>"

    img = ""
    buf = res.get("png_buf")
    if buf is not None:
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        img = (
            '<div class="imgcard">'
            f'<img src="data:image/png;base64,{b64}" alt="백테스트"></div>'
        )
    ts = datetime.now().strftime("%m-%d %H:%M")
    foot = f'<div style="text-align:center;color:var(--text-dim);font-size:11px;margin-top:10px;">계산 시각 {ts} · 최대 3시간마다 갱신</div>'
    return head + img + foot


async def _backtest_tab_html() -> str:
    """백테스트 탭 본문. reports/.backtest_tab.html 에 캐시(3h) — 매 발행 재계산 방지."""
    cache = REPORTS_DIR / ".backtest_tab.html"
    try:
        if cache.exists() and (datetime.now().timestamp() - cache.stat().st_mtime) < _BACKTEST_TAB_TTL:
            return cache.read_text(encoding="utf-8")
    except OSError:
        pass

    try:
        from scripts.backtest_frozen_portfolio import run_backtest
        res = await asyncio.to_thread(run_backtest)
        if res is None:
            return '<div style="color:var(--text-dim);text-align:center;padding:48px 24px;">거래 내역이 없습니다.</div>'
        html = _render_backtest_tab(res)
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(html, encoding="utf-8")
        except OSError:
            pass
        return html
    except Exception:
        logger.warning("백테스트 탭 렌더 실패", exc_info=True)
        try:
            if cache.exists():
                return cache.read_text(encoding="utf-8")
        except OSError:
            pass
        return '<div style="color:var(--text-dim);text-align:center;padding:48px 24px;">백테스트 계산 실패 — 잠시 후 다시 시도됩니다.</div>'


async def _wrap_with_tabs(html: bytes) -> bytes:
    """대시보드 HTML 을 하단 탭바(자산현황/그래프/히스토리/추후) 구조로 감싼다.

    기존 본문은 그대로 1번 탭(자산현황)으로 두고, 그래프/히스토리/빈 탭과
    고정 하단 탭바·전환 스크립트를 추가한다. 구조가 안 맞으면 원본 그대로 반환.
    """
    import re

    text = html.decode("utf-8")
    if "</head>" not in text or "</body>" not in text or "<body" not in text:
        return html

    graph_html = await _graph_img_html()
    history_html = _build_history_html()
    backtest_html = await _backtest_tab_html()

    # 1) <head> 에 탭 CSS 주입
    text = text.replace("</head>", _TAB_CSS + "</head>", 1)

    # 2) <body ...> 직후에 자산현황 탭 패널 시작
    text = re.sub(
        r"(<body[^>]*>)",
        r'\1<div id="tab-status" class="tab-panel active">',
        text, count=1,
    )

    # 3) </body> 직전: 자산현황 닫기 + 그래프/히스토리/추후 탭 + 탭바 + 스크립트
    panels = (
        "</div>"  # close tab-status
        '<div id="tab-graph" class="tab-panel">'
        '<div class="section-title" style="margin-top:8px;">📈 자산 그래프</div>'
        f"{graph_html}</div>"
        '<div id="tab-history" class="tab-panel">'
        '<div class="section-title" style="margin-top:8px;">🧾 매수·매도 히스토리</div>'
        f"{history_html}</div>"
        '<div id="tab-backtest" class="tab-panel">'
        '<div class="section-title" style="margin-top:8px;">🧪 백테스트</div>'
        f"{backtest_html}</div>"
    )
    tail = panels + _TABBAR_HTML + _TAB_SCRIPT
    text = text.replace("</body>", tail + "</body>", 1)
    return text.encode("utf-8")


def _pwa_assets() -> dict[str, bytes]:
    """manifest + 아이콘을 {경로: 바이트}로 반환(비밀 경로 아래로 함께 발행)."""
    assets: dict[str, bytes] = {"/manifest.webmanifest": _PWA_MANIFEST}
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        fp = _PWA_DIR / name
        if fp.exists():
            assets[f"/{name}"] = fp.read_bytes()
    return assets

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


# Claude 포트폴리오 경로
_STOCKS_BATTLE_ROOT = Path(os.environ.get(
    "STOCKS_BATTLE_DIR",
    str(Path(__file__).resolve().parent.parent.parent.parent / "stocks_battle")
))
CLAUDE_DATA_DIR = _STOCKS_BATTLE_ROOT / "data"
CLAUDE_KIS_DATA_DIR = _STOCKS_BATTLE_ROOT / "data_kis"

# 리포트 저장 경로
REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"


def _save_html_locally(html_buf: io.BytesIO, prefix: str) -> io.BytesIO:
    """HTML BytesIO를 reports/에 저장하고, 텔레그램 전송용 새 BytesIO를 반환."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    data = html_buf.getvalue()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = REPORTS_DIR / f"{prefix}_{ts}.html"
    fp.write_bytes(data)
    logger.info("리포트 저장: %s", fp)

    new_buf = io.BytesIO(data)
    new_buf.name = fp.name
    return new_buf


def _load_claude_holdings(data_dir: Path = CLAUDE_DATA_DIR) -> list[dict]:
    """stocks_battle/<data_dir>/portfolio.json에서 Claude 보유종목 로드."""
    fp = data_dir / "portfolio.json"
    if not fp.exists():
        return []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f).get("holdings", [])
    except Exception:
        logger.warning("Claude 포트폴리오 로드 실패 (%s)", data_dir, exc_info=True)
        return []


def _load_claude_account(data_dir: Path = CLAUDE_DATA_DIR) -> tuple[float, float] | None:
    """Claude 계좌 정보 로드. (initial_capital, cash) 반환."""
    account_fp = data_dir / "account.json"
    txn_fp = data_dir / "transactions.json"
    if not account_fp.exists():
        return None
    try:
        with open(account_fp, "r", encoding="utf-8") as f:
            account = json.load(f)
        initial = account.get("initial_capital", 200_000_000)

        transactions = []
        if txn_fp.exists():
            with open(txn_fp, "r", encoding="utf-8") as f:
                transactions = json.load(f).get("transactions", [])

        cash = initial
        for txn in transactions:
            amount = txn.get("total_amount", 0)
            if txn.get("type") == "buy":
                cash -= amount
            elif txn.get("type") == "sell":
                cash += amount

        return initial, cash
    except Exception:
        logger.warning("Claude 계좌 로드 실패 (%s)", data_dir, exc_info=True)
        return None


async def _fetch_futures_prices(positions: list[dict]) -> dict[str, dict]:
    """선물 포지션 시세 매핑.

    반환: {"<symbol>|<contract_month>": {"price": ..., "change_pct": ..., "source": ...}}
    우선순위: 수동 시세 → KIS 실시간 선물가 → 기초자산 yfinance 폴백.
    """
    try:
        from bot.futures_quote import fetch_futures_quotes
        return await fetch_futures_quotes(positions)
    except ImportError:
        return {}
    except Exception as e:
        logger.warning("선물 시세 조회 실패: %s", e, exc_info=True)
        return {}


async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """보유 종목 현황 대시보드를 전송한다."""
    holdings = load_holdings()

    # 같은 종목명 중복 합치기
    holdings, merged = _merge_duplicate_holdings(holdings)
    if merged:
        save_holdings(holdings)

    active = [h for h in holdings if h.get("quantity", 0) > 0]
    futures_positions = [
        p for p in load_futures_positions() if p.get("contracts", 0) > 0
    ]
    futures_prices = await _fetch_futures_prices(futures_positions)

    if not active and not futures_positions:
        await update.message.reply_text("보유 종목이 없습니다.")
    elif not active:
        # 현물은 없지만 선물 포지션은 있는 경우 — 선물만 표시
        html_file = build_html_report(
            holdings=[],
            futures_positions=futures_positions,
            futures_prices=futures_prices,
        )
        html_file = _save_html_locally(html_file, "my_portfolio")
        await update.message.reply_document(document=html_file, caption="내 포트폴리오 (선물)")
    else:
        # ticker 없는 종목 자동 보정
        filled = await _backfill_missing_tickers(holdings)
        if filled:
            await update.message.reply_text(
                "종목코드 자동 보정:\n" + "\n".join(f"  {n} → {next(h['ticker'] for h in holdings if h['name'] == n)}" for n in filled)
            )
            holdings = load_holdings()

        # 내 HTML 리포트 전송 (예수금 설정되어 있으면 표시)
        account = load_account()
        user_capital = account.get("initial_capital")
        user_cash = account.get("cash")
        user_futures_cash = account.get("futures_cash")
        user_futures_maint_ratio = account.get("futures_maintenance_ratio")
        html_file = build_html_report(
            holdings,
            initial_capital=user_capital,
            show_cash=bool(user_capital),
            cash_override=user_cash,
            futures_positions=futures_positions,
            futures_prices=futures_prices,
            futures_cash=user_futures_cash,
            futures_maintenance_ratio=user_futures_maint_ratio,
        )
        html_file = _save_html_locally(html_file, "my_portfolio")
        await update.message.reply_document(document=html_file, caption="내 포트폴리오")

    # Claude 포트폴리오 전송
    claude_account = _load_claude_account()
    if claude_account is not None:
        initial_capital, cash = claude_account
        claude_holdings = _load_claude_holdings()
        claude_html = build_html_report(
            claude_holdings,
            title="Claude 투자 현황",
            initial_capital=initial_capital,
            show_cash=True,
        )
        claude_html = _save_html_locally(claude_html, "claude_portfolio")
        await update.message.reply_document(document=claude_html, caption="Claude 포트폴리오")

    # Claude KIS 모의투자 포트폴리오 전송
    claude_kis_account = _load_claude_account(CLAUDE_KIS_DATA_DIR)
    if claude_kis_account is not None:
        kis_initial, kis_cash = claude_kis_account
        claude_kis_holdings = _load_claude_holdings(CLAUDE_KIS_DATA_DIR)
        claude_kis_html = build_html_report(
            claude_kis_holdings,
            title="Claude KIS 모의투자 현황",
            initial_capital=kis_initial,
            show_cash=True,
        )
        claude_kis_html = _save_html_locally(claude_kis_html, "claude_kis_portfolio")
        await update.message.reply_document(
            document=claude_kis_html,
            caption="Claude KIS 모의투자 포트폴리오",
        )

    # Firebase Hosting 최신본 갱신 (비차단)
    firebase_publish.trigger_publish()


async def build_all_dashboard_html() -> dict[str, bytes]:
    """현재 상태로 모든 대시보드 HTML을 생성해 {경로: 바이트}로 반환한다.

    Hosting 배포는 사이트 전체 스냅샷이므로, 서빙할 모든 파일을 매번 포함한다.
    `/index.html`은 내 포트폴리오와 동일(비밀 경로 루트에서 바로 보이게).
    텔레그램 전송 없이 dashboard_handler와 동일한 HTML을 만든다.
    """
    files: dict[str, bytes] = {}

    # --- 내 포트폴리오 ---
    holdings = load_holdings()
    holdings, merged = _merge_duplicate_holdings(holdings)
    if merged:
        save_holdings(holdings)

    active = [h for h in holdings if h.get("quantity", 0) > 0]
    futures_positions = [
        p for p in load_futures_positions() if p.get("contracts", 0) > 0
    ]
    futures_prices = await _fetch_futures_prices(futures_positions)

    my_html: bytes | None = None
    if active:
        await _backfill_missing_tickers(holdings)
        holdings = load_holdings()
        account = load_account()
        my_buf = build_html_report(
            holdings,
            initial_capital=account.get("initial_capital"),
            show_cash=bool(account.get("initial_capital")),
            cash_override=account.get("cash"),
            futures_positions=futures_positions,
            futures_prices=futures_prices,
            futures_cash=account.get("futures_cash"),
            futures_maintenance_ratio=account.get("futures_maintenance_ratio"),
        )
        my_html = my_buf.getvalue()
    elif futures_positions:
        my_buf = build_html_report(
            holdings=[],
            futures_positions=futures_positions,
            futures_prices=futures_prices,
        )
        my_html = my_buf.getvalue()

    if my_html is not None:
        my_html = await _wrap_with_tabs(my_html)  # 하단 탭바(자산현황/그래프/히스토리/추후)
        files["/my_portfolio.html"] = my_html
        files["/index.html"] = my_html  # 비밀 경로 루트 = 내 포트폴리오

    # --- Claude 포트폴리오 (있을 때만) ---
    claude_account = _load_claude_account()
    if claude_account is not None:
        initial_capital, _cash = claude_account
        claude_buf = build_html_report(
            _load_claude_holdings(),
            title="Claude 투자 현황",
            initial_capital=initial_capital,
            show_cash=True,
        )
        files["/claude.html"] = claude_buf.getvalue()

    claude_kis_account = _load_claude_account(CLAUDE_KIS_DATA_DIR)
    if claude_kis_account is not None:
        kis_initial, _ = claude_kis_account
        claude_kis_buf = build_html_report(
            _load_claude_holdings(CLAUDE_KIS_DATA_DIR),
            title="Claude KIS 모의투자 현황",
            initial_capital=kis_initial,
            show_cash=True,
        )
        files["/claude_kis.html"] = claude_kis_buf.getvalue()

    # PWA: HTML <head>에 매니페스트/메타 주입 + manifest·아이콘 동봉
    # (홈 화면에 설치하면 새 탭 대신 독립 창의 웹앱처럼 동작)
    files = {
        path: (_inject_pwa(content) if path.endswith(".html") else content)
        for path, content in files.items()
    }
    files.update(_pwa_assets())

    return files
