#!/usr/bin/env python3
"""증권사 카카오톡 알림 → 텔레그램 포워딩 (테스트용, 읽기 전용).

KB증권 / 신한투자증권 채널에 새로 도착한 카톡 알림(체결·입출금·안내 등 전부)을
텔레그램으로 그대로 전달한다. invest_log 데이터에는 쓰지 않는다.

동작:
  - 맥 카톡 로컬 DB에서 두 채널의 새 메시지(logId > watermark)만 읽어 전송
  - watermark(채널별 마지막 logId)는 data/kakao_forward_state.json 에 저장 → 중복 차단
  - 첫 실행은 baseline 만 잡고 과거 메시지는 보내지 않음("앞으로 받는 것"만)

자격증명: 기존 invest_log 봇 재사용
  - BOT_TOKEN  : .env (python-dotenv)
  - chat_id    : data/account.json (load_chat_id 가 저장한 값)
  (sendMessage 는 봇의 getUpdates 폴링과 충돌하지 않음)

사용 예:
  python3 scripts/kakao_to_telegram.py --init          # baseline 만 설정(전송 X)
  python3 scripts/kakao_to_telegram.py --catchup 2      # 채널별 최근 2건 즉시 전송(연결 테스트)
  python3 scripts/kakao_to_telegram.py                  # 1회 폴링(새 메시지 전송)
  python3 scripts/kakao_to_telegram.py --loop 60        # 60초마다 폴링(데몬)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kakao_trade_preview import find_kakaocli, load_auth, kc_query, detail_text  # noqa: E402

import requests  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(PROJECT_ROOT, "data", "kakao_forward_state.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "kakao_forward.log")

# 전달 대상 채널 (이름 → 알려진 chat_id 폴백). 이름으로 재확인 후 폴백 사용.
TARGETS = {
    "KB증권": 4803250456343651,
    "신한투자증권": 4697684299181193,
}
TG_LIMIT = 4000  # 텔레그램 메시지 길이 한도(4096) 여유분
HEALTH_KEY = "__health__"   # state 안 헬스 상태 키(숫자 chat_id와 충돌 안 함)
ALERT_REPEAT_HOURS = 6      # 계속 실패 시 재알림 간격(도배 방지)


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 자격증명
# ---------------------------------------------------------------------------
def load_credentials() -> tuple[str, int]:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    except ImportError:
        _load_env_manual(os.path.join(PROJECT_ROOT, ".env"))
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("error: BOT_TOKEN 이 .env 에 없습니다.")
    try:
        with open(os.path.join(PROJECT_ROOT, "data", "account.json"), encoding="utf-8") as f:
            chat_id = json.load(f).get("chat_id")
    except (OSError, json.JSONDecodeError):
        chat_id = None
    if not chat_id:
        sys.exit("error: chat_id 를 data/account.json 에서 찾지 못했습니다 (봇에 한 번 말 걸어 캐싱 필요).")
    return token, int(chat_id)


def _load_env_manual(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 상태(watermark)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 텔레그램
# ---------------------------------------------------------------------------
def tg_send(token: str, chat_id: int, text: str) -> bool:
    if len(text) > TG_LIMIT:
        text = text[:TG_LIMIT] + "\n…(생략)"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            log(f"  텔레그램 전송 실패 {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"  텔레그램 전송 예외: {e}")
        return False


def format_msg(broker: str, ts_kst: str, detail: str) -> str:
    head = f"🔔 {broker}" + (f" · {ts_kst} KST" if ts_kst else "")
    return f"{head}\n{'─' * 20}\n{detail.strip()}"


def notify_health(token: str, chat_id: int, ok: bool, detail: str = "") -> None:
    """카톡 읽기 정상/실패 전이 시에만 텔레그램으로 알림(상태는 state에 보존).

    - 실패→정상: '복구됨' 1회.
    - 정상→실패: '멈춤 + 복구법' 1회. 계속 실패하면 ALERT_REPEAT_HOURS 마다만 재알림.
    - 매 폴링 정상일 땐 아무것도 안 보냄(도배 방지). launchd가 매번 새 프로세스라
      상태를 state 파일에 저장해야 전이를 판단할 수 있다.
    """
    state = load_state()
    h = state.get(HEALTH_KEY, {"failing": False, "last_alert": ""})
    changed = False

    if ok:
        if h.get("failing"):
            tg_send(token, chat_id, "✅ 카톡 알림 포워딩이 복구됐어요. 다시 정상 전송됩니다.")
            log("health: 복구 알림 전송")
            h = {"failing": False, "last_alert": ""}
            changed = True
    else:
        need = not h.get("failing")
        if not need and h.get("last_alert"):
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(h["last_alert"])).total_seconds()
                need = elapsed > ALERT_REPEAT_HOURS * 3600
            except ValueError:
                need = True
        if need:
            tg_send(token, chat_id, (
                "⚠️ 카톡 알림 포워딩이 멈췄어요.\n"
                "맥 카톡 DB를 읽지 못합니다 — 카톡 재로그인 / 카톡 대형 업데이트 / "
                "디스크 접근 권한 문제일 수 있어요.\n\n"
                "복구 방법(맥 터미널):\n"
                "python3 ~/.claude/skills/kakaotalk-mac/scripts/kakaotalk_mac.py auth --refresh\n\n"
                f"사유: {detail[:300]}"
            ))
            log("health: 실패 알림 전송")
            h = {"failing": True, "last_alert": datetime.now().isoformat(timespec="seconds")}
            changed = True

    if changed:
        state[HEALTH_KEY] = h
        save_state(state)


# ---------------------------------------------------------------------------
# 채널 해석 / 메시지 조회
# ---------------------------------------------------------------------------
def resolve_targets(cli: str, db: str, key: str) -> dict[str, int]:
    """이름으로 chat_id 재확인, 실패 시 폴백 id 사용."""
    resolved = dict(TARGETS)
    try:
        names = "','".join(TARGETS.keys())
        rows = kc_query(
            cli, db, key,
            "SELECT r.chatId, COALESCE(NULLIF(r.chatName,''), u.displayName, '') "
            "FROM NTChatRoom r LEFT JOIN NTUser u ON u.directChatId = r.chatId "
            f"WHERE r.chatId != 0 AND COALESCE(NULLIF(r.chatName,''), u.displayName, '') IN ('{names}') "
            "GROUP BY r.chatId",
        )
        for cid, name in rows:
            if name in resolved:
                resolved[name] = int(cid)
    except SystemExit:
        pass  # 폴백 id 유지
    return resolved


def fetch_new(cli, db, key, chat_id, after_log_id, limit) -> list[tuple[int, str, str]]:
    """(logId, detail, ts_kst) 리스트, logId 오름차순. after_log_id 보다 큰 것만."""
    rows = kc_query(
        cli, db, key,
        f"SELECT logId, message, attachment FROM NTChatMessage "
        f"WHERE chatId={chat_id} AND logId > {after_log_id} "
        f"ORDER BY logId ASC LIMIT {limit}",
    )
    out = []
    for log_id, message, attachment in rows:
        detail, sent_kst = detail_text(message, attachment)
        if detail and detail.strip():
            out.append((int(log_id), detail, sent_kst or ""))
    return out


def current_max(cli, db, key, chat_id) -> int:
    rows = kc_query(cli, db, key, f"SELECT MAX(logId) FROM NTChatMessage WHERE chatId={chat_id}")
    return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0


# ---------------------------------------------------------------------------
# 1회 폴링
# ---------------------------------------------------------------------------
def poll_once(token: str, chat_id: int, catchup: int = 0, init_only: bool = False) -> int:
    cli = find_kakaocli()
    db, key = load_auth()
    targets = resolve_targets(cli, db, key)
    state = load_state()
    sent_total = 0

    for broker, cid in targets.items():
        key_str = str(cid)
        last = int(state.get(key_str, -1))

        if init_only or (last < 0 and catchup == 0):
            # baseline: 현재 최신까지 워터마크만 올리고 전송 안 함
            mx = current_max(cli, db, key, cid)
            state[key_str] = mx
            log(f"[{broker}] baseline 설정 logId={mx} (전송 생략)")
            continue

        if catchup > 0:
            # 최근 catchup 건을 강제로 보냄(테스트). 이후 워터마크는 최신으로.
            rows = kc_query(
                cli, db, key,
                f"SELECT logId, message, attachment FROM NTChatMessage "
                f"WHERE chatId={cid} ORDER BY logId DESC LIMIT {catchup}",
            )
            items = []
            for log_id, message, attachment in reversed(rows):
                detail, sent_kst = detail_text(message, attachment)
                if detail and detail.strip():
                    items.append((int(log_id), detail, sent_kst or ""))
        else:
            items = fetch_new(cli, db, key, cid, last, 50)

        if not items:
            log(f"[{broker}] 새 메시지 없음 (watermark={last})")
            continue

        for log_id, detail, ts_kst in items:
            ok = tg_send(token, chat_id, format_msg(broker, ts_kst, detail))
            if ok:
                sent_total += 1
                log(f"[{broker}] 전송 logId={log_id} ({ts_kst})")
            time.sleep(0.4)  # 텔레그램 rate 여유
            state[key_str] = max(int(state.get(key_str, 0)), log_id)

        # catchup 의 경우 워터마크를 현재 최신까지 끌어올려 중복 방지
        if catchup > 0:
            state[key_str] = max(int(state.get(key_str, 0)), current_max(cli, db, key, cid))

    save_state(state)
    return sent_total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="증권사 카톡 알림 → 텔레그램 포워딩(테스트용)")
    ap.add_argument("--init", action="store_true", help="baseline 만 설정(과거 전송 안 함)")
    ap.add_argument("--catchup", type=int, default=0, help="채널별 최근 N건 즉시 전송(연결 테스트)")
    ap.add_argument("--loop", type=int, default=0, help="N초 간격 데몬 폴링(0=1회만)")
    args = ap.parse_args(argv)

    token, chat_id = load_credentials()

    if args.loop > 0:
        log(f"=== 포워더 데몬 시작: {args.loop}초 간격 ===")
        while True:
            try:
                n = poll_once(token, chat_id)
                notify_health(token, chat_id, ok=True)
                if n:
                    log(f"이번 주기 {n}건 전송")
            except SystemExit as e:
                notify_health(token, chat_id, ok=False, detail=str(e))
                log(f"치명적 오류: {e}")  # auth 캐시 만료 등 → 다음 주기 재시도
            except Exception as e:  # noqa: BLE001
                notify_health(token, chat_id, ok=False, detail=str(e))
                log(f"폴링 예외: {e}")
            time.sleep(args.loop)

    try:
        n = poll_once(token, chat_id, catchup=args.catchup, init_only=args.init)
        notify_health(token, chat_id, ok=True)
        log(f"완료: {n}건 전송")
        return 0
    except SystemExit as e:
        notify_health(token, chat_id, ok=False, detail=str(e))
        log(f"오류: {e}")
        log("  → kakaocli가 DB를 못 읽으면 '전체 디스크 접근(FDA)' 권한을 확인하세요 "
            "(kakaocli, python3.12 바이너리).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
