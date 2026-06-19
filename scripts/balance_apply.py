#!/usr/bin/env python3
"""잔고 스샷 → 종목별 융자(신용대출) 반영 헬퍼.

`balance-apply` 스킬이 호출하는 CLI. 스킬이 잔고 스크린샷을 비전으로 읽어
종목별 융자금액을 뽑으면, 이 스크립트가 봇 수동 `융자` 명령과 동일한 경로
(credit_sync.apply_credit_sync)로 credit_loan 을 실측값에 맞춘다.

서브커맨드:
  state                       # 현재 보유 종목(name/qty/avg/credit_loan) JSON 출력 — 스킬이 diff 용으로 읽음
  apply  <req_id> <json>      # json = {"종목명": 융자원, ...}. credit_loan set → 저장 → 대시보드 재발행.
                              #   적용 결과(changes/unmatched/total)를 JSON 으로 출력.
  reply  <req_id> [--text T]  # 요청파일의 chat_id 로 텔레그램 회신(T 없으면 stdin). 사용자에게 결과 통보.

요청파일: data/balance_shots/<req_id>.json = {req_id, image_path, chat_id, message_id, ...}
  (봇의 사진 핸들러가 저장. 스킬은 image_path 를 Read 로 읽어 파싱한다.)

⚠️ credit_loan 은 NAV(− 신용)에 직접 들어가므로 정확값이 중요하다. 이 반영은
   봇 `융자` 명령과 동일하게 **종목 합산 credit_loan** 만 건드린다(by_account credit 미변경).
실행 파이썬: .venv (telegram/google-auth/yfinance 등 필요).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bot.handlers.credit_sync import apply_credit_sync  # noqa: E402
from storage.json_store import load_holdings, save_holdings  # noqa: E402

SHOTS_DIR = PROJECT_ROOT / "data" / "balance_shots"


def _req_path(req_id: str) -> Path:
    return SHOTS_DIR / f"{req_id}.json"


def _load_req(req_id: str) -> dict:
    try:
        return json.loads(_req_path(req_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: 요청파일을 읽지 못했습니다({req_id}): {e}")


# ---------------------------------------------------------------------------
# state — 현재 보유 종목 스냅샷(스킬이 스샷과 diff)
# ---------------------------------------------------------------------------
def cmd_state() -> int:
    rows = [
        {
            "name": h.get("name", ""),
            "quantity": int(h.get("quantity", 0) or 0),
            "avg_price": round(float(h.get("avg_price", 0) or 0)),
            "credit_loan": round(float(h.get("credit_loan", 0) or 0)),
        }
        for h in load_holdings()
        if h.get("quantity", 0) > 0
    ]
    rows.sort(key=lambda r: r["name"])
    print(json.dumps({"holdings": rows}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# apply — 종목별 credit_loan 을 스샷 실측값으로 set
# ---------------------------------------------------------------------------
def cmd_apply(req_id: str, loan_json: str) -> int:
    try:
        parsed_raw = json.loads(loan_json)
    except json.JSONDecodeError as e:
        sys.exit(f"error: 융자 JSON 파싱 실패: {e}")
    if not isinstance(parsed_raw, dict) or not parsed_raw:
        sys.exit("error: 융자 JSON 은 {\"종목명\": 금액원, ...} 형태여야 합니다.")

    # 금액 정규화(문자/콤마 허용)
    parsed: dict[str, float] = {}
    for name, amt in parsed_raw.items():
        try:
            val = float(str(amt).replace(",", "").strip())
        except (TypeError, ValueError):
            sys.exit(f"error: '{name}' 금액 인식 실패: {amt!r}")
        if val < 0:
            sys.exit(f"error: '{name}' 음수 금액 불가: {amt!r}")
        parsed[name] = val

    holdings = load_holdings()
    changes, unmatched = apply_credit_sync(holdings, parsed)
    save_holdings(holdings)

    total = sum(
        float(h.get("credit_loan") or 0)
        for h in holdings if h.get("quantity", 0) > 0
    )
    published = _republish()

    out = {
        "req_id": req_id,
        "changes": [{"name": n, "old": round(o), "new": round(v)} for n, o, v in changes],
        "unmatched": unmatched,
        "total_loan": round(total),
        "dashboard_published": published,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _republish() -> bool:
    """적용 직후 대시보드를 강제 재발행(장 시간 무관)해 융자/NAV 를 즉시 반영."""
    try:
        from dashboard_refresh import refresh_once
        return bool(refresh_once(force=True))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 대시보드 재발행 실패(다음 dash-refresh 주기가 처리): {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# reply — 원래 보낸 사람(chat_id)에게 텔레그램으로 결과 회신
# ---------------------------------------------------------------------------
def cmd_reply(req_id: str, text: str | None) -> int:
    req = _load_req(req_id)
    chat_id = req.get("chat_id")
    if not chat_id:
        sys.exit(f"error: 요청파일에 chat_id 가 없습니다({req_id}).")

    if text is None:
        text = sys.stdin.read()
    text = (text or "").strip()
    if not text:
        sys.exit("error: 회신할 텍스트가 비었습니다.")

    from kakao_to_telegram import tg_send

    token = _bot_token()
    ok = tg_send(token, int(chat_id), text)
    if not ok:
        sys.exit("error: 텔레그램 전송 실패")
    print(json.dumps({"ok": True, "req_id": req_id, "chat_id": chat_id}, ensure_ascii=False))
    return 0


def _bot_token() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv(str(PROJECT_ROOT / ".env"))
    except ImportError:
        pass
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("error: BOT_TOKEN 이 .env 에 없습니다.")
    return token


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="잔고 스샷 → 종목별 융자 반영 헬퍼")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state", help="현재 보유 종목(name/qty/avg/credit_loan) JSON 출력")

    ap_apply = sub.add_parser("apply", help="종목별 credit_loan set + 대시보드 재발행")
    ap_apply.add_argument("req_id")
    ap_apply.add_argument("loan_json", help='{"종목명": 융자원, ...}')

    ap_reply = sub.add_parser("reply", help="요청 chat_id 로 텔레그램 회신")
    ap_reply.add_argument("req_id")
    ap_reply.add_argument("--text", default=None, help="회신 텍스트(생략 시 stdin)")

    args = ap.parse_args(argv)
    if args.cmd == "state":
        return cmd_state()
    if args.cmd == "apply":
        return cmd_apply(args.req_id, args.loan_json)
    if args.cmd == "reply":
        return cmd_reply(args.req_id, args.text)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
