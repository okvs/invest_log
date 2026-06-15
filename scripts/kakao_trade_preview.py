#!/usr/bin/env python3
"""증권사 카카오톡(알림톡) 체결 알림 → 거래 미리보기 (읽기 전용, DRY-RUN).

맥 카카오톡 로컬 DB(SQLCipher)에서 증권사 채널의 체결 알림톡을 읽어
종목/구분/수량/단가/금액을 구조화해 **화면에 미리보기만** 한다.
invest_log 데이터(거래 로그)에는 절대 쓰지 않는다.

전제:
  - kakaocli 바이너리가 PATH 또는 /opt/homebrew/bin 에 있어야 함
  - ~/.cache/k-skill/kakaotalk-mac-auth.json 에 db/key 캐시가 있어야 함
    (없으면: python3 ~/.claude/skills/kakaotalk-mac/scripts/kakaotalk_mac.py auth --refresh)

핵심 발견(2026-06-15):
  증권사 카톡은 전부 카카오 알림톡이라, `message` 컬럼엔 요약만 들어가고
  실제 체결 상세(종목코드·체결수량·체결단가 등)는 `NTChatMessage.attachment`
  JSON 의 C.TI.TD.T 필드에 있다. 그래서 attachment 우선, message 폴백으로 파싱한다.

사용 예:
  python3 scripts/kakao_trade_preview.py                 # 증권사 전체, 최근 30일
  python3 scripts/kakao_trade_preview.py --since 90d
  python3 scripts/kakao_trade_preview.py --broker 신한
  python3 scripts/kakao_trade_preview.py --json
  python3 scripts/kakao_trade_preview.py --list-channels  # 감지된 증권 채널만
  python3 scripts/kakao_trade_preview.py --raw            # 원문 상세도 같이
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

CACHE_PATH = os.path.expanduser("~/.cache/k-skill/kakaotalk-mac-auth.json")
KST = timezone(timedelta(hours=9))

# 체결 구분 판정용
_SIDE = {"매수체결": "buy", "매도체결": "sell"}


# ---------------------------------------------------------------------------
# kakaocli / auth 자원
# ---------------------------------------------------------------------------
def find_kakaocli() -> str:
    path = shutil.which("kakaocli") or "/opt/homebrew/bin/kakaocli"
    if not os.path.exists(path) and not shutil.which("kakaocli"):
        sys.exit("error: kakaocli 를 찾을 수 없습니다 (PATH 또는 /opt/homebrew/bin).")
    return path


def load_auth() -> tuple[str, str]:
    if not os.path.exists(CACHE_PATH):
        sys.exit(
            "error: auth 캐시가 없습니다.\n"
            "  먼저 실행: python3 ~/.claude/skills/kakaotalk-mac/scripts/kakaotalk_mac.py auth --refresh"
        )
    with open(CACHE_PATH, encoding="utf-8") as f:
        d = json.load(f)
    db, key = d.get("database_path"), d.get("key")
    if not db or not key or not os.path.exists(db):
        sys.exit("error: auth 캐시가 불완전합니다. auth --refresh 로 다시 만드세요.")
    return db, key


def kc_query(cli: str, db: str, key: str, sql: str) -> list:
    """kakaocli query → JSON(배열의 배열) 반환."""
    res = subprocess.run(
        [cli, "query", sql, "--db", db, "--key", key],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.exit(f"error: kakaocli query 실패\n{res.stderr.strip() or res.stdout.strip()}")
    out = res.stdout.strip()
    # kakaocli 출력 앞에 잡음 줄이 섞일 수 있어 JSON 시작점부터 파싱
    start = out.find("[")
    return json.loads(out[start:]) if start >= 0 else []


# ---------------------------------------------------------------------------
# 알림톡 파싱
# ---------------------------------------------------------------------------
def detail_text(message: str, attachment: str) -> tuple[str, str | None]:
    """(상세텍스트, KST 발신시각) 반환. attachment(C.TI.TD.T) 우선, message 폴백."""
    detail, sent = message or "", None
    if attachment:
        try:
            a = json.loads(attachment)
            td = a.get("C", {}).get("TI", {}).get("TD", {}).get("T")
            if td:
                detail = td
            vendor = a.get("P", {}).get("VENDOR")
            if isinstance(vendor, str):
                vendor = json.loads(vendor)
            if isinstance(vendor, dict):
                sent = vendor.get("sendDatetime")  # "YYYY-MM-DD HH:MM:SS" (KST)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return detail, sent


def _field(text: str, label: str) -> str | None:
    """'■ 종목명: 값' / '종목명 : 값' 모두에서 값 추출 (한 줄)."""
    m = re.search(rf"(?:■\s*)?{re.escape(label)}\s*[:：]\s*(.+)", text)
    return m.group(1).strip() if m else None


def _num(s: str | None) -> float | None:
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s.replace(",", ""))
    return float(cleaned) if cleaned else None


def _qty_unit(s: str | None) -> tuple[float | None, str]:
    """'4계약' -> (4, '계약'), '10주' -> (10, '주')."""
    if not s:
        return None, ""
    unit = "계약" if "계약" in s else ("주" if "주" in s else "")
    return _num(s), unit


@dataclass
class ParsedTrade:
    broker: str
    log_id: int            # KakaoTalk 고유 로그ID (dedup 키)
    ts_kst: str            # 체결/발신 시각 (KST)
    asset_type: str        # stock | futures
    side: str              # buy | sell
    name: str              # 종목명(정리)
    code: str = ""         # 종목코드 (신한 등에서 제공)
    quantity: float | None = None
    unit: str = "주"       # 주 | 계약
    unit_price: float | None = None   # 1주/1계약 체결단가
    amount: float | None = None       # 체결금액(총액)
    contract_month: str = ""          # 선물 YYYYMM
    exec_no: str = ""                 # 증권사 체결번호
    maps_to: str = ""                 # invest_log 매핑 힌트
    note: str = ""                    # 파싱 주의사항
    raw: str = ""                     # 원문 상세 (--raw 시)


def parse_trade(broker: str, log_id: int, detail: str, sent_kst: str | None) -> ParsedTrade | None:
    """체결 알림톡 1건 파싱. 체결이 아니면 None."""
    side_key = next((k for k in _SIDE if k in detail), None)
    if not side_key:
        return None  # 입출금/승인/예탁금/안내 등 비체결 메시지 제외
    side = _SIDE[side_key]

    name_raw = _field(detail, "종목명") or ""
    name_raw = re.sub(r"\s+", " ", name_raw).strip()
    code = _field(detail, "종목코드") or ""

    exec_qty, u1 = _qty_unit(_field(detail, "체결수량"))
    order_qty, u2 = _qty_unit(_field(detail, "주문수량"))
    quantity = exec_qty if exec_qty is not None else order_qty
    unit = u1 or u2 or "주"

    price_dangga = _num(_field(detail, "체결단가"))    # 신한: 명시적 주당 체결단가
    price_geumaek = _num(_field(detail, "체결금액"))   # KB: 라벨은 '체결금액'이나 실제 주당 단가

    exec_no = ""
    m = re.search(r"(?:매수|매도)체결\((\d+)\)", detail)
    if m:
        exec_no = m.group(1)

    # 선물 여부 / 만기월
    cmonth = ""
    mf = re.search(r"\bF\s*([0-9]{6})\b", name_raw)
    is_futures = bool(mf) or unit == "계약" or "선물" in detail
    if mf:
        cmonth = mf.group(1)

    # 기초자산명 정리 (선물이면 'F 202607 (10)' 앞부분)
    name = re.split(r"\s*F\s*[0-9]{6}", name_raw)[0].strip() if is_futures and mf else name_raw

    note = ""
    if is_futures:
        asset_type = "futures"
        # 선물은 금액/단가 해석이 증권사마다 달라 강제 환산하지 않고 원문값만 보존
        unit_price = None
        amount = price_geumaek if price_geumaek is not None else price_dangga
        maps_to = "FuturesTransaction (open/close·direction 은 포지션 대조 필요)"
        note = "선물: 방향(신규/청산/롤) 불명 + 금액/단가 원문값(해석주의) → 포지션 대조 필요"
    else:
        asset_type = "stock"
        # 신한=체결단가, KB=체결금액(실측상 주당 단가). 총 체결금액 = 단가 × 수량 으로 산출.
        unit_price = price_dangga if price_dangga is not None else price_geumaek
        amount = round(unit_price * quantity, 2) if (unit_price is not None and quantity is not None) else None
        if price_dangga is None and price_geumaek is not None:
            note = "KB '체결금액'은 주당 체결단가(수량 다른 체결에 동일값 확인). 총액=단가×수량"
        elif price_dangga is not None:
            note = "신한 체결단가 사용. 총액=단가×수량"
        maps_to = f"Transaction(type={side})"

    return ParsedTrade(
        broker=broker, log_id=log_id, ts_kst=sent_kst or "",
        asset_type=asset_type, side=side, name=name, code=code,
        quantity=quantity, unit=unit, unit_price=unit_price, amount=amount,
        contract_month=cmonth, exec_no=exec_no, maps_to=maps_to, note=note,
        raw=detail,
    )


# ---------------------------------------------------------------------------
# 채널 수집
# ---------------------------------------------------------------------------
def securities_chats(cli: str, db: str, key: str, broker_filter: str | None) -> list[tuple[int, str]]:
    """display_name 에 '증권'이 들어가는 채팅방 (chat_id, name) 목록.
    NTChatRoom.chatName 이 비어 있는 경우가 있어 NTUser.displayName 으로 보강."""
    rows = kc_query(
        cli, db, key,
        "SELECT r.chatId, COALESCE(NULLIF(r.chatName,''), u.displayName, '(unknown)') "
        "FROM NTChatRoom r "
        "LEFT JOIN NTUser u ON u.directChatId = r.chatId "
        "WHERE r.chatId != 0 "
        "AND COALESCE(NULLIF(r.chatName,''), u.displayName, '') LIKE '%증권%' "
        "GROUP BY r.chatId ORDER BY r.lastUpdatedAt DESC",
    )
    out = []
    for cid, name in rows:
        if broker_filter and broker_filter not in (name or ""):
            continue
        out.append((int(cid), name))
    return out


def fetch_trades(cli, db, key, chat_id, name, since_dt, limit) -> list[ParsedTrade]:
    rows = kc_query(
        cli, db, key,
        f"SELECT logId, sentAt, message, attachment FROM NTChatMessage "
        f"WHERE chatId={chat_id} ORDER BY logId DESC LIMIT {limit}",
    )
    trades = []
    for log_id, sent_at, message, attachment in rows:
        detail, sent_kst = detail_text(message, attachment)
        t = parse_trade(name, int(log_id), detail, sent_kst)
        if not t:
            continue
        if since_dt and t.ts_kst:
            try:
                if datetime.strptime(t.ts_kst, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST) < since_dt:
                    continue
            except ValueError:
                pass
        trades.append(t)
    return trades


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([dhw])", s.strip())
    if not m:
        sys.exit("error: --since 형식은 30d / 12h / 4w 형태")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    return datetime.now(KST) - delta


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return str(v)


def print_table(trades: list[ParsedTrade], show_raw: bool) -> None:
    side_ko = {"buy": "매수", "sell": "매도"}
    for t in trades:
        tag = "📈선물" if t.asset_type == "futures" else "주식"
        head = f"{t.ts_kst or '(시각?)'}  [{t.broker}] {tag}  {side_ko.get(t.side, t.side)}"
        print(f"\n{head}")
        line = f"  {t.name}"
        if t.code:
            line += f" ({t.code})"
        if t.contract_month:
            line += f" {t.contract_month}월물"
        print(line)
        print(
            f"  수량 {fmt(t.quantity)}{t.unit}"
            f" | 단가 {fmt(t.unit_price)}"
            f" | 체결금액 {fmt(t.amount)}"
            + (f" | 체결번호 {t.exec_no}" if t.exec_no else "")
        )
        print(f"  → {t.maps_to}   (logId={t.log_id})")
        if t.note:
            print(f"  ⚠ {t.note}")
        if show_raw:
            print("  ┄ 원문 ┄")
            for ln in t.raw.split("\n"):
                print(f"    {ln}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="증권사 카톡 체결 알림 → 거래 미리보기 (읽기 전용)")
    ap.add_argument("--since", default="30d", help="조회 기간 (기본 30d; 예 90d, 12h, 4w)")
    ap.add_argument("--limit", type=int, default=200, help="채널별 최대 스캔 메시지 수 (기본 200)")
    ap.add_argument("--broker", help="채널명 부분일치 필터 (예: 신한, KB)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--raw", action="store_true", help="원문 상세도 함께 출력")
    ap.add_argument("--list-channels", action="store_true", help="감지된 증권 채널만 출력")
    args = ap.parse_args(argv)

    cli = find_kakaocli()
    db, key = load_auth()
    since_dt = parse_since(args.since)

    chats = securities_chats(cli, db, key, args.broker)
    if not chats:
        print("감지된 증권 채널이 없습니다 (채널명에 '증권' 포함 기준).")
        return 0

    if args.list_channels:
        print("=== 감지된 증권 채널 ===")
        for cid, name in chats:
            print(f"  {name}  (chat_id={cid})")
        return 0

    all_trades: list[ParsedTrade] = []
    for cid, name in chats:
        all_trades.extend(fetch_trades(cli, db, key, cid, name, since_dt, args.limit))
    # 최신순 정렬 (시각 문자열 사전식이면 충분, 없으면 logId)
    all_trades.sort(key=lambda t: (t.ts_kst or "", t.log_id), reverse=True)

    if args.json:
        payload = [asdict(t) for t in all_trades]
        if not args.raw:
            for p in payload:
                p.pop("raw", None)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("=" * 64)
    print("  증권사 카톡 체결 미리보기 — DRY-RUN (invest_log 에 저장하지 않음)")
    print(f"  기간: 최근 {args.since}   채널: {len(chats)}개   체결 건수: {len(all_trades)}건")
    print("=" * 64)
    print_table(all_trades, args.raw)

    # 요약
    n_stock = sum(1 for t in all_trades if t.asset_type == "stock")
    n_fut = sum(1 for t in all_trades if t.asset_type == "futures")
    n_buy = sum(1 for t in all_trades if t.side == "buy")
    n_sell = sum(1 for t in all_trades if t.side == "sell")
    print("\n" + "-" * 64)
    print(f"요약: 총 {len(all_trades)}건 (주식 {n_stock} / 선물 {n_fut}), 매수 {n_buy} / 매도 {n_sell}")
    print("dedup 키 = logId (적재 시 watermark + 처리완료 집합으로 중복 차단)")
    print("※ 미리보기 전용입니다. 실제 적재는 별도 단계에서 진행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
