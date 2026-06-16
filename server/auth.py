"""Firebase ID 토큰 검증 인증 시브.

PWA(프론트)가 Firebase Auth 로그인으로 받은 ID 토큰을 Authorization: Bearer
로 보내면, 백엔드가 firebase-admin 으로 검증한다(이미 의존성에 포함).
허용 사용자: 환경변수 ALLOWED_UIDS(쉼표구분) 또는 ALLOWED_EMAILS. 비어 있으면
'경고: 인증된 누구나 허용'(초기 셋업 편의) — 운영에선 반드시 설정할 것.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_CRED_PATH = Path(__file__).resolve().parent.parent / "firebase-credentials.json"
_initialized = False


def _ensure_init() -> None:
    global _initialized
    if _initialized:
        return
    import firebase_admin
    from firebase_admin import credentials

    # 다른 모듈이 default app 을 이미 만들었을 수 있으니 named app 사용
    try:
        firebase_admin.get_app("authapp")
    except ValueError:
        firebase_admin.initialize_app(
            credentials.Certificate(str(_CRED_PATH)), name="authapp"
        )
    _initialized = True


def _allowed() -> tuple[set[str], set[str]]:
    uids = {x.strip() for x in os.getenv("ALLOWED_UIDS", "").split(",") if x.strip()}
    emails = {x.strip().lower() for x in os.getenv("ALLOWED_EMAILS", "").split(",") if x.strip()}
    return uids, emails


async def require_user(authorization: str = Header(None)) -> str:
    """검증 성공 시 uid 반환, 실패 시 401/403."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer "):].strip()

    import firebase_admin
    from firebase_admin import auth as fb_auth

    _ensure_init()
    app = firebase_admin.get_app("authapp")
    try:
        decoded = fb_auth.verify_id_token(token, app=app)
    except Exception as e:  # noqa: BLE001
        logger.info("토큰 검증 실패: %s", e)
        raise HTTPException(status_code=401, detail="invalid token")

    uid = decoded.get("uid", "")
    email = (decoded.get("email", "") or "").lower()
    allow_uids, allow_emails = _allowed()
    if allow_uids or allow_emails:
        if uid not in allow_uids and email not in allow_emails:
            raise HTTPException(status_code=403, detail="not allowed")
    else:
        logger.warning("ALLOWED_UIDS/EMAILS 미설정 — 인증된 모든 사용자 허용(운영 전 설정 필요)")
    return uid
