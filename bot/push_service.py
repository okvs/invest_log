"""PWA 웹 푸시(Web Push) 서비스 — VAPID 키 관리 · 구독 저장 · 발송.

텔레그램 알림을 대체하기 위한 1단계. 봇/카톡자동반영/스케줄러 등 어느 프로세스에서나
import 해서 `send_push()` 로 폰(설치된 PWA)에 알림을 보낸다. 데이터는 json_store 와
같은 data/ 디렉토리를 공유한다(VAPID 키·구독목록).

- VAPID 키: data/vapid.json (private PEM + 브라우저용 public raw b64url). 최초 1회 생성.
- 구독: data/push_subscriptions.json (endpoint+keys). 폰에서 구독 시 추가, 만료(404/410) 시 제거.
- 발송: pywebpush. 페이로드 {title, body, url} 를 서비스워커가 받아 알림 표시.

iOS 는 '홈 화면에 추가'로 설치된 PWA + iOS16.4+ 에서만 푸시 허용.
"""
from __future__ import annotations

import base64
import json
import logging

from storage import json_store as store

logger = logging.getLogger(__name__)

VAPID_SUBJECT = "mailto:tmdals5992@gmail.com"


def _vapid_file():
    return store.DATA_DIR / "vapid.json"


def _subs_file():
    return store.DATA_DIR / "push_subscriptions.json"


def _ensure_vapid() -> dict:
    """VAPID 키쌍을 로드(없으면 생성). {private_pem, public_b64} 반환."""
    fp = _vapid_file()
    if fp.exists():
        return json.loads(fp.read_text(encoding="utf-8"))

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    private_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )  # 65바이트 비압축 포인트 = 브라우저 applicationServerKey
    public_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("ascii")

    data = {"private_pem": private_pem, "public_b64": public_b64}
    fp.write_text(json.dumps(data), encoding="utf-8")
    return data


def public_key() -> str:
    """브라우저 pushManager.subscribe 에 넘길 VAPID 공개키(b64url, 패딩 없음)."""
    return _ensure_vapid()["public_b64"]


# ---------------------------------------------------------------------------
# 구독 관리
# ---------------------------------------------------------------------------
def load_subscriptions() -> list[dict]:
    fp = _subs_file()
    if not fp.exists():
        return []
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []


def save_subscriptions(subs: list[dict]) -> None:
    _subs_file().write_text(json.dumps(subs, ensure_ascii=False), encoding="utf-8")


def add_subscription(sub: dict) -> None:
    """구독 추가(endpoint 기준 중복 제거)."""
    endpoint = (sub or {}).get("endpoint")
    if not endpoint:
        raise ValueError("구독에 endpoint 가 없습니다.")
    subs = [s for s in load_subscriptions() if s.get("endpoint") != endpoint]
    subs.append(sub)
    save_subscriptions(subs)


# ---------------------------------------------------------------------------
# 발송
# ---------------------------------------------------------------------------
def send_push(title: str, body: str, url: str = "") -> int:
    """저장된 모든 구독에 푸시 발송. 성공 건수 반환. 만료(404/410)는 목록에서 제거."""
    subs = load_subscriptions()
    if not subs:
        return 0
    from pywebpush import WebPushException, webpush

    vapid = _ensure_vapid()
    payload = json.dumps({"title": title, "body": body, "url": url or ""}, ensure_ascii=False)
    alive: list[dict] = []
    ok = 0
    for s in subs:
        try:
            webpush(
                subscription_info=s,
                data=payload,
                vapid_private_key=vapid["private_pem"],
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=600,
            )
            ok += 1
            alive.append(s)
        except WebPushException as e:  # noqa: PERF203
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                logger.info("만료된 푸시 구독 제거: %s", code)
                continue  # 구독 만료 → 드롭
            logger.warning("푸시 발송 실패(유지): %s", e)
            alive.append(s)  # 일시 오류는 유지
        except Exception:  # noqa: BLE001
            logger.warning("푸시 발송 예외(유지)", exc_info=True)
            alive.append(s)
    if len(alive) != len(subs):
        save_subscriptions(alive)
    return ok


# ---------------------------------------------------------------------------
# '확인 필요'(입력 필요) 알림 — 섹터 미입력 종목 + 회고 대기 매도
# ---------------------------------------------------------------------------
# 대시보드 '확인 필요' 탭과 동일 기준(섹터 비었거나 기본값) — dashboard import 없이 직접 계산.
_SECTOR_NEEDS = {"", "기타", "미국주식"}


def _push_state_file():
    return store.DATA_DIR / "push_state.json"


def pending_input_counts() -> tuple[int, int]:
    """(섹터 입력 필요 종목 수, 회고 대기 매도 수). 연금 매도는 회고 대상 아님."""
    holdings = store.load_holdings()
    sector_n = sum(
        1 for h in holdings
        if h.get("quantity", 0) > 0 and (h.get("sector") or "") in _SECTOR_NEEDS
    )
    txs = store.load_transactions()
    retro_n = sum(
        1 for t in txs
        if t.get("type") == "sell" and not t.get("retrospective_id") and not t.get("is_pension")
    )
    return sector_n, retro_n


def notify_pending_inputs_if_new() -> bool:
    """'확인 필요' 건수가 직전보다 늘었으면 푸시(늘면 True). 최초 호출은 baseline 만 잡고 미발송."""
    sector_n, retro_n = pending_input_counts()
    fp = _push_state_file()
    first = not fp.exists()
    prev = {}
    if not first:
        try:
            prev = json.loads(fp.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    fp.write_text(json.dumps({"sector_n": sector_n, "retro_n": retro_n}), encoding="utf-8")
    if first:
        return False
    grew = sector_n > prev.get("sector_n", 0) or retro_n > prev.get("retro_n", 0)
    if not grew:
        return False
    parts = []
    if sector_n:
        parts.append(f"섹터입력 {sector_n}")
    if retro_n:
        parts.append(f"회고 {retro_n}")
    if not parts:
        return False
    send_push("📝 확인 필요", " · ".join(parts) + "건이 있어요 — 앱에서 확인하세요")
    return True

