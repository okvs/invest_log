#!/usr/bin/env python3
"""장중(KRX) 주기적 대시보드 재발행 — 현재가 스냅샷 갱신.

대시보드는 발행 시점의 yfinance/KIS 시세를 HTML에 박는 구조라(클라이언트 시세
fetch 없음), 거래가 없으면 PWA 현재가가 멈춘다. 이 스크립트가 장중 일정 주기로
재발행해 현재가를 갱신한다. firebase_publish._build_and_deploy()를 그대로 호출
하므로 PWA(manifest/아이콘) 포함 전체 대시보드가 최신 시세로 다시 올라간다.

장 시간: KRX 정규장 평일 09:00~15:30 KST (공휴일은 별도 처리 안 함 — 그날은
재발행이 한 번 더 돌 뿐 무해).

자격증명: firebase-credentials.json (BOT_TOKEN 불필요 — 텔레그램 전송 안 함).
실행 파이썬: .venv (yfinance/matplotlib/google-auth 필요).

사용 예:
  .venv/bin/python scripts/dashboard_refresh.py --once          # 장중이면 1회 재발행
  .venv/bin/python scripts/dashboard_refresh.py --once --force  # 장 시간 무시하고 강제 재발행(테스트)
  .venv/bin/python scripts/dashboard_refresh.py --loop 900      # 15분마다(장중에만) 재발행
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "dashboard_refresh.log")
KST = timezone(timedelta(hours=9))
# KRX 정규장 09:00~15:30 + NXT(넥스트레이드) 프리/애프터마켓 포함 08:00~20:00.
MARKET_OPEN = (8, 0)     # 08:00 (NXT 프리마켓)
MARKET_CLOSE = (20, 0)   # 20:00 (NXT 애프터마켓 마감)


def log(msg: str) -> None:
    line = f"{datetime.now(KST).isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    if now.weekday() >= 5:  # 토(5)/일(6)
        return False
    hm = (now.hour, now.minute)
    return MARKET_OPEN <= hm <= MARKET_CLOSE


def refresh_once(force: bool = False) -> bool:
    """장중이면(또는 force) 대시보드를 재발행. 발행했으면 True."""
    if not force and not market_open():
        log("장 시간 아님 — skip")
        return False

    from bot import firebase_publish as fp

    if not fp.is_enabled():
        log("firebase 자격증명 없음 — skip")
        return False

    try:
        url = asyncio.run(fp._build_and_deploy())
        log(f"재발행 완료: {url}" if url else "재발행: 생성된 파일 없음(보유/포지션 없음?)")
        _audit_ledger()
        return bool(url)
    except Exception as e:  # noqa: BLE001
        log(f"재발행 실패: {e}")
        return False


def _audit_ledger() -> None:
    """재발행 주기마다 장부 불변식 감사 — 새 위반 조합이면 웹 푸시(중복 억제)."""
    try:
        from bot.audit import audit_and_notify
        violations = audit_and_notify()
        if violations:
            log("⚠️ 장부 불변식 위반 " + str(len(violations)) + "건: "
                + "; ".join(f"[{x.severity}]{x.code}" for x in violations))
    except Exception as e:  # noqa: BLE001
        log(f"불변식 감사 실패(발행엔 영향 없음): {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="장중 대시보드 재발행(현재가 갱신)")
    ap.add_argument("--loop", type=int, default=0, help="N초 간격 데몬(장중에만 발행). 0=1회만")
    ap.add_argument("--once", action="store_true", help="1회 실행")
    ap.add_argument("--force", action="store_true", help="장 시간 무시하고 강제 발행")
    args = ap.parse_args(argv)

    if args.loop > 0:
        from bot.self_restart import arm, reexec_if_source_changed
        arm()
        log(f"=== 대시보드 재발행 데몬 시작: {args.loop}초 간격(장중 08:00~20:00 KST, NXT 포함) ===")
        while True:
            try:
                refresh_once(force=args.force)
            except Exception as e:  # noqa: BLE001
                log(f"루프 예외: {e}")
            # 하루 1회 data/ 스냅샷 백업(장 시간 무관 — 오늘자 있으면 no-op)
            try:
                from bot.backup import maybe_backup
                made = maybe_backup()
                if made:
                    log(f"data 백업 생성: {made}")
            except Exception as e:  # noqa: BLE001
                log(f"백업 체크 실패: {e}")
            time.sleep(args.loop)
            # 발행 사이 안전지점 — 소스가 바뀌었으면 새 코드로 자기 재실행
            reexec_if_source_changed(log)

    refresh_once(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
