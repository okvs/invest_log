"""자체 비밀번호 로그인 + 서명 토큰 인증 (Firebase Auth 미사용).

이유: 앱은 web.app 에서 서빙되는데 구글 로그인 핸들러는 firebaseapp.com 이라
모바일(iOS ITP)에서 교차도메인 세션이 안 잡혀 탭/새로고침마다 로그아웃됐다.
자체 토큰은 web.app 의 1st-party localStorage 에 저장돼 안정적이다.

저장: data/webapp_auth.json {salt, pw_hash, secret}. 비밀번호는 첫 로그인 시 설정.
토큰: HMAC 서명(JWT 유사), 기본 30일 만료. 추가 의존성 없음(stdlib).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Header, HTTPException

from storage import json_store

_PBKDF2_ROUNDS = 200_000
_TOKEN_DAYS = 30


def _auth_file():
    # json_store.DATA_DIR 기준 — 테스트(임시 data dir 패치)와 운영 모두 정상.
    return json_store.DATA_DIR / "webapp_auth.json"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _load() -> dict:
    try:
        return json.loads(_auth_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(d: dict) -> None:
    fp = _auth_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(d), encoding="utf-8")
    try:
        os.chmod(fp, 0o600)
    except OSError:
        pass


def _hash_pw(pw: str, salt: bytes) -> str:
    return _b64e(hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ROUNDS))


def is_password_set() -> bool:
    return bool(_load().get("pw_hash"))


def auth_enabled() -> bool:
    """인증 게이트 활성 여부. 기본 OFF(공개) — 비밀번호 단계에서 WEBAPP_AUTH=1 로 켠다.

    공개 모드에서는 토큰 없이 모든 /api/* 를 쓸 수 있다(프론트도 로그인 화면을
    건너뛴다). 비밀번호를 다시 도입할 땐 서버 환경변수 WEBAPP_AUTH=1 만 켜면
    아래 login/_issue_token/_verify_token 경로가 그대로 살아난다.
    """
    return os.getenv("WEBAPP_AUTH", "").strip().lower() in ("1", "true", "yes", "on")


def login(password: str) -> str:
    """비밀번호 검증(또는 첫 설정) 후 서명 토큰 반환. 실패 시 ValueError."""
    password = (password or "").strip()
    if len(password) < 4:
        raise ValueError("비밀번호는 4자 이상이어야 합니다.")
    d = _load()
    if not d.get("pw_hash"):
        # 첫 로그인 → 비밀번호 설정 + 토큰 시크릿 생성
        salt = os.urandom(16)
        d = {
            "salt": salt.hex(),
            "pw_hash": _hash_pw(password, salt),
            "secret": _b64e(os.urandom(32)),
        }
        _save(d)
    else:
        salt = bytes.fromhex(d["salt"])
        if not hmac.compare_digest(_hash_pw(password, salt), d["pw_hash"]):
            raise ValueError("비밀번호가 틀렸습니다.")
    return _issue_token(d["secret"])


def _issue_token(secret: str, days: int = _TOKEN_DAYS) -> str:
    payload = {"sub": "owner", "exp": int(time.time()) + days * 86400}
    p = _b64e(json.dumps(payload).encode("utf-8"))
    sig = _b64e(hmac.new(secret.encode(), p.encode(), hashlib.sha256).digest())
    return f"{p}.{sig}"


def _verify_token(token: str) -> bool:
    d = _load()
    secret = d.get("secret")
    if not secret:
        return False
    try:
        p, sig = token.split(".", 1)
        exp_sig = _b64e(hmac.new(secret.encode(), p.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, exp_sig):
            return False
        payload = json.loads(_b64d(p))
        return int(payload.get("exp", 0)) > time.time()
    except Exception:  # noqa: BLE001
        return False


async def require_user(authorization: str = Header(None)) -> str:
    if not auth_enabled():
        return "owner"  # 공개 모드 — 토큰 불필요
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization[len("Bearer "):].strip()
    if not _verify_token(token):
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return "owner"
