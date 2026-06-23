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


# 서비스워커 — 웹 푸시 수신/표시. {token}/sw.js 로 발행되어 {token}/ 스코프를 제어한다.
_SERVICE_WORKER_JS = b"""
self.addEventListener('install', function(e){ self.skipWaiting(); });
self.addEventListener('activate', function(e){ e.waitUntil(self.clients.claim()); });
self.addEventListener('push', function(event){
  var d = {};
  try { d = event.data ? event.data.json() : {}; }
  catch(e){ d = { title: '\\uD22C\\uC790 \\uC54C\\uB9BC', body: (event.data && event.data.text()) || '' }; }
  var opts = {
    body: d.body || '',
    icon: 'icon-192.png',
    badge: 'icon-192.png',
    data: { url: d.url || './app.html' }
  };
  event.waitUntil(self.registration.showNotification(d.title || '\\uD22C\\uC790 \\uC54C\\uB9BC', opts));
});
self.addEventListener('notificationclick', function(event){
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || './app.html';
  event.waitUntil(
    self.clients.matchAll({ type:'window', includeUncontrolled:true }).then(function(list){
      for (var i=0;i<list.length;i++){
        if (list[i].url.indexOf('app.html') >= 0 && 'focus' in list[i]) return list[i].focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
"""


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
  /* backdrop-filter 는 스크롤 시 iOS에서 바가 흔들려 보이는 원인이라 제외.
     반투명(rgba)만으로 투명도 유지. */
  .tabbar { pointer-events:auto; display:flex; gap:4px; padding:8px;
            background:var(--tabbar-bg);
            border:1px solid var(--border);
            border-radius:24px; box-shadow:var(--shadow);
            max-width:520px; margin:0 auto; }
  .tabbar button { position:relative; flex:1; background:none; border:none; cursor:pointer;
            color:var(--text-dim); font-size:11px; font-weight:600;
            display:flex; flex-direction:column; align-items:center; gap:5px;
            padding:9px 4px; border-radius:16px; font-family:inherit;
            -webkit-tap-highlight-color:transparent;
            transition:background .15s, color .15s; }
  .tabbar button svg { width:22px; height:22px; fill:none; stroke:currentColor;
            stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
  .tabbar button.active { color:var(--accent); background:var(--accent-soft); }
  /* 그래프 탭 새 알림 배지 (빨간 동그라미 숫자) */
  .tab-badge { position:absolute; top:4px; left:calc(50% + 6px);
            min-width:16px; height:16px; padding:0 4px; box-sizing:border-box;
            border-radius:9px; background:#e5484d; color:#fff;
            font-size:10px; font-weight:700; line-height:16px; text-align:center;
            box-shadow:0 0 0 2px var(--card); }

  /* 그래프 탭 부가 카드 (회고/피라미딩 봉차트) */
  .extra-card { max-width:960px; margin:0 auto 14px; }
  .extra-head { display:flex; align-items:center; gap:8px; margin:0 2px 2px; }
  .extra-name { font-weight:700; color:var(--text-strong); font-size:14px; }
  .extra-tag { font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px;
            background:var(--accent-soft); color:var(--accent); }
  .extra-cap { font-size:12px; color:var(--text-dim); margin:7px 2px 0; line-height:1.6; }
  a.extra-card { display:block; text-decoration:none; color:inherit; }
  .extra-act { margin:8px 2px 0; font-size:12px; font-weight:700; color:var(--accent); }
  .dismiss-btn { margin:10px 2px 0; width:100%; padding:9px 0; border-radius:10px;
            border:1px solid var(--border); background:var(--card); color:var(--text-dim);
            font-size:13px; font-weight:700; font-family:inherit; cursor:pointer; }
  .dismiss-btn:hover { color:var(--accent); border-color:var(--accent); }

  /* 확인 필요 탭 — 섹터 입력 카드 */
  .check-intro { max-width:960px; margin:0 auto 12px; font-size:13px; line-height:1.6;
            color:var(--text-dim); padding:0 2px; }
  .sector-need-card { border:1px solid var(--border); background:var(--card);
            border-radius:14px; padding:13px 15px; box-shadow:var(--shadow); }
  .sector-need-card .extra-head { margin:0 0 4px; }
  .sector-need-tkr { font-size:11px; font-weight:700; color:var(--text-dim);
            background:var(--accent-soft); padding:2px 7px; border-radius:6px; margin-left:6px; }
  .sector-need-msg { font-size:12.5px; color:#e5704d; font-weight:600; margin:2px 0 10px; }
  .sector-form { display:flex; gap:8px; }
  .sector-input { flex:1; padding:11px 12px; border:1px solid var(--border);
            border-radius:10px; background:var(--bg); color:var(--text);
            font-size:15px; font-family:inherit; box-sizing:border-box; min-width:0; }
  .sector-save { flex:0 0 auto; padding:11px 18px; border:none; border-radius:10px;
            background:var(--accent); color:#fff; font-size:14px; font-weight:700;
            font-family:inherit; cursor:pointer; -webkit-tap-highlight-color:transparent; }
  .sector-save:disabled { opacity:.6; }

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
  /* 연금 거래 — 기록 탭에만 보이고 모든 계산서 제외. 연금으로 설정된 행만 '연금' 라벨이
     붙고, 토글 버튼은 평소 숨김 → 행을 꾹 누르면(long-press) 나타난다. */
  .hist-row.is-pension { opacity:.62; }
  .hist-row .r1 { -webkit-user-select:none; user-select:none; }  /* 롱프레스 시 텍스트선택 방지 */
  .pen-label { font-size:10px; font-weight:700; padding:2px 8px; border-radius:999px;
               background:var(--accent); color:#fff; white-space:nowrap; flex:none; }
  .pen-toggle { display:none; font-size:10px; font-weight:700; padding:2px 8px; border-radius:999px;
                border:1px solid var(--accent); background:transparent; color:var(--accent);
                cursor:pointer; white-space:nowrap; font-family:inherit; line-height:1.4; flex:none;
                -webkit-tap-highlight-color:transparent; }
  .hist-row.pen-armed { opacity:1; background:var(--accent-soft,rgba(31,122,82,.14)); }
  .hist-row.pen-armed .pen-toggle { display:inline-flex; align-items:center; }
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
_IC_CHECK = (
    '<svg viewBox="0 0 24 24"><rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/>'
    '<polyline points="8 12.5 11 15.5 16.5 9"/></svg>'
)
_TABBAR_HTML = (
    '<div class="tabbar-wrap"><nav class="tabbar">'
    f'<button data-tab="tab-status" class="active">{_IC_STATUS}현황</button>'
    f'<button data-tab="tab-check">{_IC_CHECK}확인 필요'
    '<span class="tab-badge" id="graph-badge" style="display:none"></span></button>'
    f'<button data-tab="tab-history">{_IC_HISTORY}기록</button>'
    f'<button data-tab="tab-backtest">{_IC_STRATEGY}전략</button>'
    '</nav></div>'
)


def _norm(s: str) -> str:
    """종목명 비교용 정규화 — 공백 제거 + casefold."""
    return (s or "").replace(" ", "").casefold()


_BOT_USERNAME_CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "bot_username.txt"


def _bot_username() -> str:
    """봇 username (회고 카드 → 텔레그램 딥링크용). 봇이 기동 시
    data/bot_username.txt 에 기록한다(main._save_bot_username). 빌드 시 네트워크
    의존을 피하려 파일만 읽고, 없으면 '' (회고 카드는 링크 없이 렌더)."""
    try:
        if _BOT_USERNAME_CACHE.exists():
            return _BOT_USERNAME_CACHE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


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
        is_usd = t.get("currency") == "USD"
        is_orphan = bool(t.get("orphan"))
        qty = int(t.get("quantity", 0) or 0)
        price = float(t.get("price", 0) or 0)
        total = float(t.get("total_amount", 0) or 0) or (price * qty)  # 총 거래금액
        if is_usd:
            detail = f"{qty:,}주 × ${price:,.2f} = ${total:,.2f}"
        else:
            detail = f"{qty:,}주 × {_fmt_won(price)}원 = {_fmt_won(total)}원"
        items.append({
            "date": str(t.get("date", "")),
            "cls": "b-buy" if is_buy else "b-sell",
            "label": ("🇺🇸" if is_usd else "") + ("매수" if is_buy else "매도"),
            "name": t.get("name", ""),
            "detail": detail,
            # 매수/orphan 매도는 손익 표시 없음
            "pnl": None if (is_buy or is_orphan) else t.get("profit_loss"),
            "pnl_pct": None if (is_buy or is_orphan) else t.get("profit_loss_pct"),
            "cur": "USD" if is_usd else "KRW",
            "txid": t.get("id", ""),
            "pension": bool(t.get("is_pension")),
            "pensionable": True,  # 현물 거래는 연금 토글 대상
        })

    fut_label = {"open": "선물진입", "close": "선물청산",
                 "roll_open": "롤(진입)", "roll_close": "롤(청산)"}
    for t in load_futures_transactions():
        typ = t.get("type", "")
        is_open = typ in ("open", "roll_open")
        f_contracts = int(t.get("contracts", 0) or 0)
        f_price = float(t.get("price", 0) or 0)
        f_mult = int(t.get("multiplier", 10) or 10)
        f_notional = f_price * f_contracts * f_mult  # 명목 거래금액(단가×계약×승수)
        items.append({
            "date": str(t.get("date", "")),
            "cls": "b-open" if is_open else "b-close",
            "label": fut_label.get(typ, typ),
            "name": t.get("name", ""),
            "detail": f"{f_contracts:,}계약 × {_fmt_won(f_price)}원 = {_fmt_won(f_notional)}원",
            "pnl": None if is_open else t.get("pnl"),
            "pnl_pct": None if is_open else t.get("pnl_pct"),
            "pensionable": False,  # 선물은 연금 토글 대상 아님
            "pension": False,
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
            if it.get("cur") == "USD":
                amt = f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}"
            else:
                amt = f"{sign}{_fmt_won(pnl)}원"
            pnl_html = f'<span class="pnl {cls}">{amt}{pct_s}</span>'
        tm = it["date"][11:16]
        is_pension = it.get("pension")
        pen_html = ""
        if it.get("pensionable") and it.get("txid"):
            # 연금 설정된 행만 '연금' 라벨(정적). 토글 버튼은 숨김(롱프레스로 노출).
            label = '<span class="pen-label">연금</span>' if is_pension else ""
            btn_txt = "연금 해제" if is_pension else "연금"
            pen_html = (
                label
                + f'<button type="button" class="pen-toggle" '
                  f'data-pension-tx="{it["txid"]}">{btn_txt}</button>'
            )
        row_cls = "hist-row is-pension" if is_pension else "hist-row"
        out.append(
            f'<div class="{row_cls}">'
            '<div class="r1">'
            f'<span class="hbadge {it["cls"]}">{it["label"]}</span>'
            f'<span class="nm">{it["name"]}</span>'
            f'{pen_html}'
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


# --- 그래프 탭 부가: 회고 대기(매도 복기) · 피라미딩 후보 봉차트 ---
_GRAPH_REVIEW_TTL = 3 * 3600   # 매도 종목 봉차트(과거 데이터) — 길게 캐시
_GRAPH_PYRAMID_TTL = 3600      # 피라미딩(시장 신호) — 1시간
_MAX_REVIEW = 6
_MAX_PYRAMID = 6


def _avg_buy_price(stxs: list[dict]) -> float:
    amt = qty = 0.0
    for t in stxs:
        if t.get("type") == "buy":
            q = int(t.get("quantity", 0) or 0)
            p = float(t.get("price", 0) or 0)
            amt += p * q
            qty += q
    return (amt / qty) if qty else 0.0


def _lookup_ticker_cf(tmap: dict, name: str) -> str:
    nk = _norm(name)
    for k, v in tmap.items():
        if _norm(k) == nk:
            return v
    return ""


def _extra_card(
    name: str, tag: str, caption: str, chart_buf,
    *, kind: str = "", item_id: str = "", link: str = "", data_retro: str = "",
) -> str:
    """봉차트 카드. kind='retro'면 카드 전체가 텔레그램 회고 딥링크(읽기 대시보드),
    kind='pyramid'면 하단에 '확인(숨기기)' 버튼(클라이언트 dismiss).

    retro 카드에는 미회고 매도 거래정보(data_retro, HTML 이스케이프된 JSON)를
    함께 심는다 — 읽기 대시보드에선 무시되고, app.html(쓰기) 의 JS 가 이 값으로
    탭하면 인라인 회고 폼을 열어 /api/retro 로 기록한다."""
    if chart_buf is None:
        body = '<div style="color:var(--text-dim);padding:24px;text-align:center;">차트를 불러오지 못했습니다(시세 조회 실패).</div>'
    else:
        b64 = base64.b64encode(chart_buf.getvalue()).decode("ascii")
        body = (
            '<div class="imgcard" style="margin:8px 0 0;">'
            f'<img src="data:image/png;base64,{b64}" alt="{name}"></div>'
        )
    inner = (
        f'<div class="extra-head"><span class="extra-name">{name}</span>'
        f'<span class="extra-tag">{tag}</span></div>'
        f'{body}'
        f'<div class="extra-cap">{caption}</div>'
    )
    if kind == "retro":
        href = f' href="{link}" target="_blank" rel="noopener"' if link else ""
        dr = f' data-retro="{data_retro}"' if data_retro else ""
        return (
            f'<a class="extra-card retro-card"{href}{dr}>'
            f'{inner}<div class="extra-act">탭하면 텔레그램에서 회고 ›</div></a>'
        )
    if kind == "pyramid":
        return (
            f'<div class="extra-card" data-item="{item_id}">{inner}'
            f'<button class="dismiss-btn" data-item="{item_id}">확인 (숨기기)</button></div>'
        )
    return f'<div class="extra-card">{inner}</div>'


def _render_review_section() -> tuple[str, list[str]]:
    """미회고 매도(type==sell·retrospective_id 없음) 종목별 봉차트 카드."""
    from bot.trade_review import build_trade_chart

    txs = load_transactions()
    holdings = load_holdings()
    tmap = load_ticker_map()
    unrev = [
        t for t in txs
        if t.get("type") == "sell" and not t.get("retrospective_id")
        and not t.get("is_pension")  # 연금 매도는 회고 대상 아님(기록 탭에만 노출)
    ]
    if not unrev:
        return "", []

    uname = _bot_username()
    bot_url = f"https://t.me/{uname}" if uname else ""

    by_stock: dict[str, list[dict]] = {}
    for t in unrev:
        by_stock.setdefault(_norm(t.get("name", "")), []).append(t)
    ordered = sorted(
        by_stock.values(),
        key=lambda lst: max((x.get("date", "") for x in lst), default=""),
        reverse=True,
    )

    ids: list[str] = []
    cards: list[str] = []
    for sells in ordered[:_MAX_REVIEW]:
        name = sells[0].get("name", "")
        payload: list[dict] = []
        for s in sells:
            ids.append("retro:" + str(s.get("id", "")))
            payload.append({
                "id": str(s.get("id", "")),
                "name": name,
                "date": (s.get("date", "") or "")[:10],
                "pnl": int(float(s.get("profit_loss", 0) or 0)),
            })
        data_retro = json.dumps(payload, ensure_ascii=False).replace('"', "&quot;")
        ticker = next(
            (h.get("ticker") for h in holdings
             if _norm(h.get("name", "")) == _norm(name) and h.get("ticker")), ""
        ) or _lookup_ticker_cf(tmap, name)
        stxs = [t for t in txs if _norm(t.get("name", "")) == _norm(name)]
        avg = _avg_buy_price(stxs)
        chart = build_trade_chart(name, ticker, stxs, avg, None)
        last = max(sells, key=lambda x: x.get("date", ""))
        pnl = sum(float(x.get("profit_loss", 0) or 0) for x in sells)
        cap = (
            f'{last.get("date", "")[:10]} 매도 · {len(sells)}건 미회고 · '
            f'<span class="{"profit" if pnl >= 0 else "loss"}">손익 {int(pnl):+,}원</span>'
        )
        cards.append(_extra_card(
            name, "회고 대기", cap, chart, kind="retro", link=bot_url, data_retro=data_retro,
        ))

    html = (
        '<div class="section-title" style="margin-top:24px;">📝 회고 대기 '
        '<span style="font-weight:400;font-size:12px;color:var(--text-dim);">(매도 복기)</span></div>'
        + "".join(cards)
    )
    return html, ids


def _render_pyramid_section() -> tuple[str, list[str]]:
    """오늘 피라미딩 후보(강한 돌파·수익 중) 종목별 봉차트 카드."""
    from bot.trade_review import build_trade_chart
    from bot.pyramiding import detect_opportunities

    holdings = load_holdings()
    txs = load_transactions()
    cands = detect_opportunities(holdings)
    if not cands:
        return "", []

    today = datetime.now().strftime("%Y-%m-%d")
    ids: list[str] = []
    cards: list[str] = []
    for c in cands[:_MAX_PYRAMID]:
        item_id = f"pyr:{c['name']}:{today}"
        ids.append(item_id)
        stxs = [t for t in txs if _norm(t.get("name", "")) == _norm(c["name"])]
        avg = float(c.get("avg") or 0) or _avg_buy_price(stxs)
        chart = build_trade_chart(c["name"], c.get("ticker", ""), stxs, avg, c.get("cur"))
        cap = (
            f'{c["kind"]} · 전일대비 {c["chg"]:+.1f}% · '
            f'<span class="profit">수익 {c["pnl_pct"]:+.1f}%</span> · '
            f'제안 {c["suggested_shares"]:,}주(≈1천만)'
        )
        cards.append(_extra_card(c["name"], "피라미딩각", cap, chart, kind="pyramid", item_id=item_id))

    html = (
        '<div class="section-title" style="margin-top:24px;">🔺 피라미딩 후보 '
        '<span style="font-weight:400;font-size:12px;color:var(--text-dim);">(강한 돌파·수익 중)</span></div>'
        + "".join(cards)
    )
    return html, ids


async def _cached_extra(fname: str, key: str, ttl: int, render_fn) -> tuple[str, list[str]]:
    """무거운 봉차트 섹션을 reports/<fname> 에 캐시. key 불일치 또는 ttl 경과 시 재생성."""
    cache = REPORTS_DIR / fname
    try:
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            fresh = (datetime.now().timestamp() - cache.stat().st_mtime) < ttl
            if data.get("key") == key and fresh:
                return data.get("html", ""), data.get("ids", [])
    except (OSError, json.JSONDecodeError):
        pass
    try:
        html, ids = await asyncio.to_thread(render_fn)
    except Exception:
        logger.warning("그래프 부가섹션 렌더 실패: %s", fname, exc_info=True)
        try:
            if cache.exists():
                d = json.loads(cache.read_text(encoding="utf-8"))
                return d.get("html", ""), d.get("ids", [])
        except (OSError, json.JSONDecodeError):
            pass
        return "", []
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "html": html, "ids": ids}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return html, ids


async def _build_graph_extras() -> tuple[str, list[str]]:
    """확인 필요 탭의 회고/피라미딩 부가 섹션 + 배지용 item id 목록.

    항목이 없으면 빈 문자열을 반환 — placeholder 표시는 섹터 카드와 합쳐
    _wrap_with_tabs 에서 한 번만 결정한다.
    """
    txs = load_transactions()
    # 'v2|' 접두 = data-retro 마크업 도입 시 캐시 무효화(코드 변경은 key 에 안 잡힘).
    review_key = "v3|" + "|".join(sorted(
        str(t.get("id", "")) for t in txs
        if t.get("type") == "sell" and not t.get("retrospective_id")
        and not t.get("is_pension")
    ))
    today = datetime.now().strftime("%Y-%m-%d")

    rh, rids = await _cached_extra(".graph_review.json", review_key, _GRAPH_REVIEW_TTL, _render_review_section)
    ph, pids = await _cached_extra(".graph_pyramid.json", today, _GRAPH_PYRAMID_TTL, _render_pyramid_section)

    # 회고 대기(rids)는 '확인 필요' 배지에서 제외 — 회고는 모든 매도를 하는 게 아니라
    # 골라서 하는 선택적 복기라(사용자 결정 2026-06-23) 카드만 두고 배지로 재촉하지 않는다.
    return rh + ph, pids


def _build_sector_needed_html() -> tuple[str, int]:
    """섹터가 비어 있거나(기타) 기본값(미국주식)인 보유 종목 = '확인 필요' 카드.

    종목별로 섹터 입력 폼을 렌더한다. 저장 동작(POST /api/sector)은 app.html
    쓰기 레이어(_WRITE_LAYER)가 담당하고, 읽기 대시보드에선 안내만 표시된다.
    반환: (HTML, 카드 수).
    """
    rows = []
    for h in load_holdings():
        if h.get("quantity", 0) <= 0:
            continue
        s = (h.get("sector") or "").strip()
        if s not in ("", "기타", "미국주식"):
            continue
        rows.append((
            h.get("name", ""),
            h.get("ticker", "") or "",
            h.get("currency") == "USD",
        ))

    if not rows:
        return "", 0

    cards = []
    for name, ticker, is_us in rows:
        nm_attr = name.replace('"', "&quot;")
        tk_attr = ticker.replace('"', "&quot;")
        tkr_tag = f'<span class="sector-need-tkr">{ticker}</span>' if (is_us and ticker) else ""
        placeholder = "예: 빅테크, 반도체, AI" if is_us else "예: 반도체, 2차전지, 바이오"
        cards.append(
            f'<div class="extra-card sector-need-card" data-name="{nm_attr}" data-ticker="{tk_attr}">'
            f'<div class="extra-head"><span class="extra-name">{name}</span>{tkr_tag}</div>'
            '<div class="sector-need-msg">섹터 입력이 필요합니다.</div>'
            '<div class="sector-form">'
            f'<input class="sector-input" type="text" placeholder="{placeholder}" '
            'autocomplete="off" autocapitalize="off">'
            '<button class="sector-save" type="button">저장</button>'
            '</div></div>'
        )
    intro = (
        '<div class="check-intro">아래 종목은 섹터가 비어 있어요. '
        '섹터를 입력해 저장하면 현황 탭의 <b>섹터 비중 차트</b>에 반영됩니다.</div>'
    )
    return intro + "".join(cards), len(rows)


# 그림 확대(라이트박스) — 자산그래프·4사분면·백테스트(.imgcard / .qd-chart-wrap) 탭하면
# 화면 꽉 차게 확대. 가로로 긴 그림은 세로 화면에서 90° 회전해 더 크게. 읽기 대시보드에도
# 들어가도록 tail(_wrap_with_tabs)에서 모든 대시보드에 주입한다.
_ZOOM_LAYER = """
<style>
  .zoomable { cursor:zoom-in; }
  .zoom-modal { position:fixed; inset:0; z-index:4000; display:none;
       background:rgba(0,0,0,.97); -webkit-tap-highlight-color:transparent; }
  .zoom-modal.show { display:block; }
  .zoom-stage { position:absolute; left:50%; top:50%; transform-origin:center center; will-change:transform; }
  .zoom-stage > * { display:block; }
  .zoom-close { position:fixed; top:calc(8px + env(safe-area-inset-top)); right:14px; z-index:4001;
       width:42px; height:42px; border-radius:50%; border:none; background:rgba(255,255,255,.16);
       color:#fff; font-size:24px; line-height:42px; text-align:center; cursor:pointer; padding:0; }
  .zoom-hint { position:fixed; left:50%; transform:translateX(-50%); z-index:4001; pointer-events:none;
       bottom:calc(18px + env(safe-area-inset-bottom)); color:rgba(255,255,255,.55); font-size:12px; }
</style>
<div class="zoom-modal" id="zoom-modal">
  <button class="zoom-close" id="zoom-close" aria-label="닫기">&times;</button>
  <div class="zoom-stage" id="zoom-stage"></div>
  <div class="zoom-hint">탭하면 닫기</div>
</div>
<script>(function(){
  var modal=document.getElementById('zoom-modal'), stage=document.getElementById('zoom-stage');
  if(!modal||!stage) return;
  function close(){ modal.classList.remove('show'); stage.innerHTML=''; document.body.style.overflow=''; }
  function fit(w,h){
    var vw=window.innerWidth, vh=window.innerHeight, rot=(w>h && vh>vw);
    var s=rot ? Math.min(vh/w, vw/h) : Math.min(vw/w, vh/h); if(!(s>0)) s=1;
    stage.style.transform='translate(-50%,-50%)'+(rot?' rotate(90deg)':'')+' scale('+s+')';
    stage.dataset.w=w; stage.dataset.h=h;
  }
  function open(srcEl){
    var media=srcEl.querySelector('img, svg'); if(!media) return;
    var w,h;
    if(media.tagName.toLowerCase()==='img'){ w=media.naturalWidth||media.clientWidth; h=media.naturalHeight||media.clientHeight; }
    else { var vb=media.viewBox&&media.viewBox.baseVal; if(vb&&vb.width){w=vb.width;h=vb.height;}
           else {var r=media.getBoundingClientRect(); w=r.width; h=r.height;} }
    if(!(w>0&&h>0)) return;
    var clone=media.cloneNode(true); clone.removeAttribute('style');
    clone.style.width=w+'px'; clone.style.height=h+'px';
    if(media.tagName.toLowerCase()==='svg'){  // 클론 시 잃은 배경 복원(투명 SVG 뒤로 페이지 비침 방지)
      var bg=getComputedStyle(media).backgroundColor;
      clone.style.background=(bg&&bg!=='rgba(0, 0, 0, 0)'&&bg!=='transparent')?bg:'#0e1512';
    }
    stage.innerHTML=''; stage.appendChild(clone);
    modal.classList.add('show'); document.body.style.overflow='hidden';
    fit(w,h);
  }
  document.querySelectorAll('.imgcard, .qd-chart-wrap').forEach(function(el){
    el.classList.add('zoomable');
    el.addEventListener('click', function(e){
      if(e.target.closest('[data-tip], .qd-toggle, a, button')) return;  // 포인트/토글 탭은 확대 제외
      open(el);
    });
  });
  document.getElementById('zoom-close').addEventListener('click', function(e){ e.stopPropagation(); close(); });
  modal.addEventListener('click', close);
  window.addEventListener('resize', function(){ if(modal.classList.contains('show')&&stage.dataset.w) fit(+stage.dataset.w, +stage.dataset.h); });
  document.addEventListener('keydown', function(e){ if(e.key==='Escape') close(); });
})();</script>
"""


async def _wrap_with_tabs(html: bytes) -> bytes:
    """대시보드 HTML 을 하단 탭바(현황/확인 필요/기록/전략) 구조로 감싼다.

    기존 본문은 그대로 1번 탭(현황)으로 두고 자산 그래프를 그 맨 아래에 붙인다.
    2번 탭(확인 필요)에는 섹터 입력이 필요한 종목 + 회고/피라미딩 부가 카드를 모은다.
    구조가 안 맞으면 원본 그대로 반환.
    """
    import re

    text = html.decode("utf-8")
    if "</head>" not in text or "</body>" not in text or "<body" not in text:
        return html

    graph_html = await _graph_img_html()
    extras_html, graph_ids = await _build_graph_extras()
    sector_html, sector_n = _build_sector_needed_html()
    history_html = _build_history_html()
    backtest_html = await _backtest_tab_html()

    check_body = sector_html + extras_html
    if not check_body:
        check_body = ('<div style="color:var(--text-dim);text-align:center;padding:32px 24px;">'
                      '확인할 항목이 없습니다.</div>')

    # 1) <head> 에 탭 CSS 주입
    text = text.replace("</head>", _TAB_CSS + "</head>", 1)

    # 2) <body ...> 직후에 현황 탭 패널 시작
    text = re.sub(
        r"(<body[^>]*>)",
        r'\1<div id="tab-status" class="tab-panel active">',
        text, count=1,
    )

    # 3) </body> 직전: 현황(자산 그래프 추가)·확인 필요·기록·전략 탭 + 탭바 + 스크립트
    panels = (
        # 현황 탭 맨 아래에 자산 그래프
        '<div class="section-title" style="margin-top:28px;">📈 자산 그래프</div>'
        f"{graph_html}"
        "</div>"  # close tab-status
        # 확인 필요 탭 = 섹터 입력 필요 종목 + 회고/피라미딩 부가 카드
        '<div id="tab-check" class="tab-panel">'
        '<div class="section-title" style="margin-top:8px;">✅ 확인 필요</div>'
        f"{check_body}</div>"
        '<div id="tab-history" class="tab-panel">'
        '<div class="section-title" style="margin-top:8px;">🧾 매수·매도 히스토리</div>'
        f"{history_html}</div>"
        '<div id="tab-backtest" class="tab-panel">'
        '<div class="section-title" style="margin-top:8px;">🧪 백테스트</div>'
        f"{backtest_html}</div>"
    )
    # 확인 필요 탭 배지 = 백엔드 기준 '섹터 입력 필요' 종목 수 — 모든 기기 동일(기기별
    # localStorage 'seen' 의존 제거, 사용자 결정 2026-06-23). 섹터를 실제 입력하면(.resolved)
    # 줄고, 단순히 탭을 봤다고 사라지지 않는다. 회고·피라미딩은 배지에서 빼고 탭에서 골라봄
    # (피라미딩 '확인(숨기기)'는 이 기기 한정 카드 숨김일 뿐 배지/데이터엔 영향 없음).
    badge_script = (
        "<script>(function(){"
        "var b=document.getElementById('graph-badge');"
        "function gs(k){var o={};try{o=JSON.parse(localStorage.getItem(k)||'{}');}catch(e){}return o;}"
        "function ss(k,o){try{localStorage.setItem(k,JSON.stringify(o));}catch(e){}}"
        "function sectorN(){return document.querySelectorAll('.sector-need-card:not(.resolved)').length;}"
        "function refresh(){var n=sectorN();"
        "if(b){if(n){b.textContent=n;b.style.display='';}else{b.style.display='none';}}}"
        "window.__refreshCheckBadge=refresh;"
        "var dis=gs('graph_dismissed');"
        "document.querySelectorAll('.extra-card[data-item]').forEach(function(c){"
        "if(dis[c.getAttribute('data-item')])c.style.display='none';});"
        "document.querySelectorAll('.dismiss-btn').forEach(function(btn){"
        "btn.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();"
        "var id=btn.getAttribute('data-item');dis[id]=1;ss('graph_dismissed',dis);"
        "var card=btn.closest('.extra-card');if(card)card.style.display='none';});});"
        "refresh();})();</script>"
    )
    _ = graph_ids  # 배지에서 더는 쓰지 않음(회고·피라미딩 제외) — 섹션 렌더는 extras_html 에 포함
    tail = panels + _TABBAR_HTML + _TAB_SCRIPT + badge_script + _ZOOM_LAYER
    text = text.replace("</body>", tail + "</body>", 1)
    return text.encode("utf-8")


_TUNNEL_URL_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "tunnel_url.txt"

# 쓰기 레이어 — app.html(쓰기 PWA)에만 주입. 읽기 대시보드와 HTML 은 동일하고,
# '확인 필요' 탭의 ① '회고 대기' 카드(.retro-card[data-retro]) 탭 → 인라인 회고 폼
# (/api/retro), ② 섹터 입력 카드(.sector-need-card) 저장 → /api/sector 로 섹터 보정.
# 폼 스타일은 대시보드 CSS 변수를 그대로 사용한다.
_WRITE_LAYER = """
<style>
  .retro-card{cursor:pointer}
  .retro-modal{position:fixed;inset:0;z-index:3000;display:none;align-items:flex-end;
       justify-content:center;background:rgba(0,0,0,.45)}
  .retro-modal.show{display:flex}
  .retro-sheet{background:var(--card);color:var(--text);width:100%;max-width:560px;
       max-height:88vh;overflow:auto;border-radius:18px 18px 0 0;
       padding:18px 16px calc(22px + env(safe-area-inset-bottom));
       box-shadow:0 -4px 24px rgba(0,0,0,.3)}
  .retro-hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
  .retro-hd b{font-size:16px}
  .retro-x{background:none;border:none;color:var(--text-dim);font-size:20px;line-height:1;cursor:pointer}
  .retro-sheet label{display:block;font-size:12px;color:var(--text-dim);font-weight:600;margin:12px 0 4px}
  .retro-sheet input,.retro-sheet select,.retro-sheet textarea{width:100%;padding:11px 12px;
       border:1px solid var(--border);border-radius:10px;background:var(--bg);color:var(--text);
       font-size:15px;font-family:inherit;box-sizing:border-box}
  .retro-sheet textarea{min-height:60px;resize:vertical}
  .retro-btns{display:flex;gap:8px}
  .retro-opt{flex:1;padding:11px 8px;border:1px solid var(--border);border-radius:10px;
       background:var(--bg);color:var(--text);font-size:14px;font-weight:600;font-family:inherit;
       cursor:pointer;-webkit-tap-highlight-color:transparent;transition:background .12s,color .12s}
  .retro-opt.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .retro-submit{width:100%;padding:13px;border:none;border-radius:12px;background:var(--accent);
       color:#fff;font-size:15px;font-weight:700;margin-top:16px;cursor:pointer}
  .retro-submit:disabled{opacity:.6}
  .retro-toast{position:fixed;left:50%;bottom:calc(112px + env(safe-area-inset-bottom));
       transform:translateX(-50%);background:var(--accent);color:#fff;padding:11px 18px;
       border-radius:10px;font-size:14px;font-weight:600;opacity:0;transition:opacity .2s;
       pointer-events:none;z-index:3100;max-width:90vw;text-align:center}
  .retro-toast.show{opacity:1}
  .retro-skip{display:block;width:100%;margin-top:10px;padding:9px;
       border:1px solid var(--border);border-radius:10px;background:transparent;
       color:var(--text-dim);font-size:13px;font-weight:600;font-family:inherit;cursor:pointer;
       -webkit-tap-highlight-color:transparent}
  .retro-skip:active{opacity:.7}
</style>
<div class="retro-modal" id="retro-modal"><div class="retro-sheet">
  <div class="retro-hd"><b id="retro-title">회고</b><button class="retro-x" id="retro-close">&times;</button></div>
  <label>매수 근거가 맞았나?</label>
  <div class="retro-btns" id="retro-correct">
    <button type="button" class="retro-opt" data-val="true">맞았다</button>
    <button type="button" class="retro-opt" data-val="false">틀렸다</button>
    <button type="button" class="retro-opt active" data-val="">부분적·모름</button>
  </div>
  <label>잘한 점</label><textarea id="retro-well"></textarea>
  <label>아쉬운 점</label><textarea id="retro-regret"></textarea>
  <label>피할 수 있었나</label><input id="retro-avoid" placeholder="피할 수 있었다 / 통제 불가 / 모르겠다">
  <label>교훈</label><textarea id="retro-lesson"></textarea>
  <button class="retro-submit" id="retro-submit">회고 저장</button>
</div></div>
<div class="retro-toast" id="retro-toast"></div>
<script>(function(){
  var CFG=window.__APPCFG__||{}, API=CFG.api||'';
  var modal=document.getElementById('retro-modal'); if(!modal) return;
  var cur=null, $=function(id){return document.getElementById(id);};
  function toast(m,bad){var t=$('retro-toast');t.textContent=m;
    t.style.background=bad?'#d83c3c':'var(--accent)';t.classList.add('show');
    setTimeout(function(){t.classList.remove('show');},2600);}
  function correctBtns(){return $('retro-correct').querySelectorAll('.retro-opt');}
  function setCorrect(v){correctBtns().forEach(function(b){b.classList.toggle('active',b.getAttribute('data-val')===v);});}
  function getCorrect(){var a=$('retro-correct').querySelector('.retro-opt.active');return a?a.getAttribute('data-val'):'';}
  correctBtns().forEach(function(b){b.addEventListener('click',function(){setCorrect(b.getAttribute('data-val'));});});
  function open(card){var raw=card.getAttribute('data-retro');if(!raw)return;
    try{cur={card:card,items:JSON.parse(raw)};}catch(e){return;}
    var nm=(cur.items[0]&&cur.items[0].name)||'';
    $('retro-title').textContent=nm+' 회고'+(cur.items.length>1?(' · '+cur.items.length+'건'):'');
    setCorrect('');['retro-well','retro-regret','retro-avoid','retro-lesson'].forEach(function(i){$(i).value='';});
    modal.classList.add('show');}
  function close(){modal.classList.remove('show');cur=null;}
  function val(id){return ($(id).value||'').trim();}
  function getDis(){try{return JSON.parse(localStorage.getItem('graph_dismissed')||'{}');}catch(e){return {};}}
  function markDismissed(items){var d=getDis();items.forEach(function(it){d['retro:'+it.id]=1;});
    try{localStorage.setItem('graph_dismissed',JSON.stringify(d));}catch(e){}}
  function allDismissed(items){var d=getDis();return items.length>0&&items.every(function(it){return d['retro:'+it.id];});}
  function decBadge(n){var b=document.getElementById('graph-badge');if(!b)return;
    var v=(parseInt(b.textContent,10)||0)-n;if(v>0){b.textContent=v;}else{b.style.display='none';}}
  async function submit(){if(!cur)return;var btn=$('retro-submit');btn.disabled=true;
    var tc=getCorrect();
    var base={thesis_correct:tc===''?null:(tc==='true'),what_went_well:val('retro-well'),
      regrets:val('retro-regret'),avoidable:val('retro-avoid'),lessons:val('retro-lesson')};
    try{
      for(var i=0;i<cur.items.length;i++){
        var body=Object.assign({transaction_id:cur.items[i].id},base);
        var r=await fetch(API+'/api/retro',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(!r.ok){var j=await r.json().catch(function(){return{};});throw new Error(j.detail||('오류 '+r.status));}
      }
      markDismissed(cur.items);
      cur.card.style.display='none';toast('회고 저장 완료');close();
    }catch(e){toast(e.message||'저장 실패',true);}finally{btn.disabled=false;}}
  document.querySelectorAll('.retro-card').forEach(function(card){
    var items=[];try{items=JSON.parse(card.getAttribute('data-retro')||'[]');}catch(e){}
    if(allDismissed(items)){card.style.display='none';return;}  // 이전에 스킵/회고한 카드는 숨김
    var act=card.querySelector('.extra-act');if(act){act.textContent='탭해서 회고 기록 ›';}
    card.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();open(card);});
    var sk=document.createElement('button');sk.type='button';sk.className='retro-skip';
    sk.textContent='건너뛰기 (이 종목 회고 안 함)';
    sk.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();
      markDismissed(items);card.style.display='none';toast('건너뛰었어요');});
    card.appendChild(sk);});  // 카드 안에 넣어야 카드 숨길 때 버튼도 함께 사라짐
  $('retro-close').addEventListener('click',close);
  modal.addEventListener('click',function(e){if(e.target===modal)close();});
  $('retro-submit').addEventListener('click',submit);
})();</script>
<script>(function(){
  // 확인 필요 탭의 섹터 입력 카드 → POST /api/sector. 저장 성공 시 카드를 숨기고
  // (.resolved) 배지를 갱신한다. 읽기 대시보드(API 없음)에선 동작하지 않는다.
  var CFG=window.__APPCFG__||{}, API=CFG.api||'';
  if(!API) return;
  function toast(m,bad){var t=document.getElementById('retro-toast');if(!t)return;
    t.textContent=m;t.style.background=bad?'#d83c3c':'var(--accent)';t.classList.add('show');
    setTimeout(function(){t.classList.remove('show');},2600);}
  document.querySelectorAll('.sector-need-card').forEach(function(card){
    var inp=card.querySelector('.sector-input'), btn=card.querySelector('.sector-save');
    if(!inp||!btn) return;
    async function save(){
      var sec=(inp.value||'').trim();
      if(!sec){inp.focus();return;}
      btn.disabled=true;
      var body={sector:sec,ticker:card.getAttribute('data-ticker')||'',name:card.getAttribute('data-name')||''};
      try{
        var r=await fetch(API+'/api/sector',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(!r.ok){var j=await r.json().catch(function(){return{};});throw new Error(j.detail||('오류 '+r.status));}
        card.classList.add('resolved');card.style.display='none';
        if(window.__refreshCheckBadge)window.__refreshCheckBadge();
        toast('섹터 저장: '+sec);
      }catch(e){toast(e.message||'저장 실패',true);btn.disabled=false;}
    }
    btn.addEventListener('click',save);
    inp.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();save();}});
  });
})();</script>
<script>(function(){
  // 기록 탭: 거래 행을 '꾹 누르면(long-press)' 연금 토글 버튼이 나타나고, 탭하면
  // POST /api/pension. 연금 설정된 행만 '연금' 라벨이 정적으로 붙는다. 토글하면 그 거래가
  // 보유/평가/총자산/예수금/섹터/그래프/실현손익 등 모든 계산에서 빠지거나(켜기) 다시
  // 포함되며(끄기), 기록 탭엔 계속 보인다(다른 탭은 다음 새로고침 때 반영).
  var CFG=window.__APPCFG__||{}, API=CFG.api||'';
  if(!API) return;
  function toast(m,bad){var t=document.getElementById('retro-toast');if(!t)return;
    t.textContent=m;t.style.background=bad?'#d83c3c':'var(--accent)';t.classList.add('show');
    setTimeout(function(){t.classList.remove('show');},2600);}
  var LONG=480, timer=null, armed=null;
  function disarm(){ if(armed){armed.classList.remove('pen-armed');armed=null;} }
  function arm(row){ if(armed===row)return; disarm(); row.classList.add('pen-armed'); armed=row; }
  document.querySelectorAll('.hist-row .pen-toggle[data-pension-tx]').forEach(function(btn){
    var row=btn.closest('.hist-row'); if(!row) return;
    function start(){ cancel(); timer=setTimeout(function(){ arm(row); }, LONG); }
    function cancel(){ if(timer){clearTimeout(timer);timer=null;} }
    row.addEventListener('touchstart',start,{passive:true});
    row.addEventListener('touchend',cancel);
    row.addEventListener('touchmove',cancel);
    row.addEventListener('mousedown',start);
    row.addEventListener('mouseup',cancel);
    row.addEventListener('mouseleave',cancel);
    row.addEventListener('contextmenu',function(e){e.preventDefault();});  // 롱프레스 콜아웃 차단
    btn.addEventListener('click',async function(e){
      e.preventDefault();e.stopPropagation();
      var id=btn.getAttribute('data-pension-tx');
      btn.disabled=true;
      try{
        var r=await fetch(API+'/api/pension',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({transaction_id:id})});
        if(!r.ok){var j=await r.json().catch(function(){return{};});throw new Error(j.detail||('오류 '+r.status));}
        var jj=await r.json();var on=!!(jj.transaction&&jj.transaction.is_pension);
        row.classList.toggle('is-pension',on);
        var lab=row.querySelector('.pen-label');
        if(on){ if(!lab){lab=document.createElement('span');lab.className='pen-label';
                 lab.textContent='연금';btn.parentNode.insertBefore(lab,btn);} btn.textContent='연금 해제'; }
        else { if(lab)lab.remove(); btn.textContent='연금'; }
        toast(on?'연금으로 표시 — 계산에서 제외':'연금 해제 — 다시 포함');
        disarm();
      }catch(e2){toast(e2.message||'실패',true);}finally{btn.disabled=false;}
    });
  });
  // 무장된 행 바깥을 탭하면 닫기(버튼 탭은 위에서 처리되어 유지)
  document.addEventListener('click',function(e){ if(!e.target.closest('.pen-armed')) disarm(); },true);
})();</script>
<style>
  .push-card{background:var(--card);border:1px solid var(--border);border-radius:12px;
       padding:12px 14px;margin:0 0 12px;box-shadow:var(--shadow)}
  .push-row{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .push-ttl{font-weight:700;color:var(--text-strong);font-size:14px}
  .push-actions{display:flex;gap:6px;flex:none}
  .push-btn{font-size:12px;font-weight:700;padding:7px 12px;border-radius:9px;border:none;
       background:var(--accent);color:#fff;cursor:pointer;font-family:inherit;-webkit-tap-highlight-color:transparent}
  .push-btn.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
  .push-btn:disabled{opacity:.6}
  .push-sub{font-size:12px;color:var(--text-dim);margin-top:6px;line-height:1.4}
</style>
<script>(function(){
  // PWA 웹 푸시 — 확인 필요 탭 상단에 '알림 켜기' 카드. 권한 요청 → 구독 → /api/push/subscribe.
  // iOS 는 홈 화면에 추가(설치)된 PWA + iOS16.4+ 에서만 푸시 가능.
  var CFG=window.__APPCFG__||{}, API=CFG.api||'';
  if(!API) return;
  function toast(m,bad){var t=document.getElementById('retro-toast');if(!t)return;
    t.textContent=m;t.style.background=bad?'#d83c3c':'var(--accent)';t.classList.add('show');
    setTimeout(function(){t.classList.remove('show');},2600);}
  function u8(b64){ var pad='='.repeat((4-b64.length%4)%4); var s=(b64+pad).replace(/-/g,'+').replace(/_/g,'/');
    var raw=atob(s),arr=new Uint8Array(raw.length); for(var i=0;i<raw.length;i++)arr[i]=raw.charCodeAt(i); return arr; }
  var panel=document.getElementById('tab-check'); if(!panel) return;
  var card=document.createElement('div'); card.className='push-card';
  card.innerHTML='<div class="push-row"><span class="push-ttl">\\uD83D\\uDD14 \\uD478\\uC2DC \\uC54C\\uB9BC</span>'
    +'<span class="push-actions"><button class="push-btn" id="push-enable">\\uC54C\\uB9BC \\uCF1C\\uAE30</button>'
    +'<button class="push-btn ghost" id="push-test" style="display:none">\\uD14C\\uC2A4\\uD2B8</button></span></div>'
    +'<div class="push-sub" id="push-sub">\\uCCB4\\uACB0 \\uBC18\\uC601\\u00B7\\uD655\\uC778 \\uD544\\uC694 \\uC2DC \\uD3F0\\uC73C\\uB85C \\uC54C\\uB9BC\\uC744 \\uBC1B\\uC2B5\\uB2C8\\uB2E4.</div>';
  panel.insertBefore(card, panel.firstChild);
  var enableBtn=card.querySelector('#push-enable'), testBtn=card.querySelector('#push-test'), sub=card.querySelector('#push-sub');
  var ok=('serviceWorker' in navigator)&&('PushManager' in window)&&('Notification' in window);
  function setOn(on){
    if(on){ enableBtn.textContent='\\uC54C\\uB9BC \\uCF1C\\uC9D0 \\u2705'; enableBtn.disabled=true; testBtn.style.display='';
            sub.textContent='\\uC774 \\uAE30\\uAE30\\uB85C \\uC54C\\uB9BC\\uC744 \\uBC1B\\uC2B5\\uB2C8\\uB2E4.'; }
    else { enableBtn.textContent='\\uC54C\\uB9BC \\uCF1C\\uAE30'; enableBtn.disabled=false; testBtn.style.display='none'; }
  }
  if(!ok){ enableBtn.disabled=true; enableBtn.textContent='\\uBBF8\\uC9C0\\uC6D0';
    sub.textContent='iOS\\uB294 \\uD648 \\uD654\\uBA74\\uC5D0 \\uCD94\\uAC00(\\uC124\\uCE58) \\uD6C4 \\uC0AC\\uC6A9\\uD558\\uC138\\uC694.'; return; }
  async function reg(){ return await navigator.serviceWorker.register('sw.js'); }
  (async function(){ try{ await reg(); var r=await navigator.serviceWorker.ready;
    var s=await r.pushManager.getSubscription(); setOn(!!s && Notification.permission==='granted'); }catch(e){} })();
  enableBtn.addEventListener('click', async function(){
    enableBtn.disabled=true;
    try{
      var perm=await Notification.requestPermission();
      if(perm!=='granted'){ toast('\\uC54C\\uB9BC \\uAD8C\\uD55C \\uAC70\\uBD80\\uB428',true); setOn(false); return; }
      await reg(); var r=await navigator.serviceWorker.ready;
      var kj=await (await fetch(API+'/api/push/key')).json();
      var s=await r.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:u8(kj.key)});
      var pr=await fetch(API+'/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({subscription:s.toJSON()})});
      if(!pr.ok) throw new Error('\\uAD6C\\uB3C5 \\uB4F1\\uB85D \\uC2E4\\uD328');
      setOn(true); toast('\\uC54C\\uB9BC \\uCF1C\\uC9D0');
    }catch(e){ toast(e.message||'\\uC2E4\\uD328',true); setOn(false); }
  });
  testBtn.addEventListener('click', async function(){
    testBtn.disabled=true;
    try{ var j=await (await fetch(API+'/api/push/test',{method:'POST'})).json();
      toast(j.sent>0?'\\uD14C\\uC2A4\\uD2B8 \\uC54C\\uB9BC \\uBC1C\\uC1A1':'\\uAD6C\\uB3C5\\uC774 \\uC5C6\\uC2B5\\uB2C8\\uB2E4', j.sent===0); }
    catch(e){ toast('\\uC2E4\\uD328',true); } finally{ testBtn.disabled=false; }
  });
})();</script>
"""


def _inject_write_layer(dashboard_html: bytes | None) -> bytes | None:
    """대시보드 HTML 에 쓰기 레이어(__APPCFG__ + _WRITE_LAYER)를 주입해 반환.

    '확인 필요' 탭에서 '회고 대기' 카드를 탭하면 인라인 회고 폼(/api/retro),
    섹터 입력 카드를 저장하면 섹터 보정(/api/sector)이 맥 터널 API 로 기록된다
    (__APPCFG__ 로 터널 주소 주입). 터널 주소(data/tunnel_url.txt)가 없으면 None
    (읽기 전용 유지).
    """
    if not dashboard_html:
        return None
    try:
        tunnel = _TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        tunnel = ""
    if not tunnel:
        return None
    text = dashboard_html.decode("utf-8")
    cfg = json.dumps({"api": tunnel}, ensure_ascii=False)
    layer = f"<script>window.__APPCFG__={cfg};</script>" + _WRITE_LAYER
    text = text.replace("</body>", layer + "</body>", 1)
    return text.encode("utf-8")


def _pwa_assets() -> dict[str, bytes]:
    """manifest + 아이콘을 {경로: 바이트}로 반환(비밀 경로 아래로 함께 발행)."""
    assets: dict[str, bytes] = {
        "/manifest.webmanifest": _PWA_MANIFEST,
        "/sw.js": _SERVICE_WORKER_JS,
    }
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
            cash_by_account=account.get("cash_by_account"),
            usd_cash=account.get("usd_cash"),
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
            cash_by_account=account.get("cash_by_account"),
            usd_cash=account.get("usd_cash"),
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
    # 루트 대시보드(index.html)와 app.html 모두에 쓰기 레이어 주입 — 어느 주소로
    # 열어도 그 자리에서 회고 폼·섹터 저장이 동작한다(터널 있을 때). 터널이 없으면
    # index.html 은 읽기 전용 그대로 두고 app.html 은 미발행.
    writable = _inject_write_layer(files.get("/index.html"))
    if writable is not None:
        files["/index.html"] = writable
        files["/app.html"] = writable

    return files
