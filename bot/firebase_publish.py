"""Firebase Hosting에 대시보드 HTML을 배포한다.

Storage 버킷은 Blaze(결제수단) 요금제가 필요하지만, Hosting은 무료(Spark)로
서비스 계정만으로 배포할 수 있다. 따라서 '잔고' 대시보드 HTML을 Hosting REST API로
올려 고정 URL에서 항상 최신본을 볼 수 있게 한다.

재무 데이터이므로 공개 루트가 아닌 추측 불가능한 비밀 경로 `/{token}/` 아래에 둔다.
token은 data/firebase_dashboard_token.txt에 한 번 생성·고정되어 URL이 안 바뀐다.

배포(release)는 사이트 전체 스냅샷이다 — 매 배포에 서빙할 모든 파일을 함께 올린다.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CRED_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS", str(PROJECT_ROOT / "firebase-credentials.json")
)
SITE_ID = os.environ.get("FIREBASE_HOSTING_SITE", "invest-log-caf3d")
_TOKEN_FILE = PROJECT_ROOT / "data" / "firebase_dashboard_token.txt"

_HOSTING_API = "https://firebasehosting.googleapis.com/v1beta1"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def is_enabled() -> bool:
    """자격증명이 있으면 발행 활성화."""
    return Path(CRED_PATH).exists()


def secret_token() -> str:
    """비밀 경로 토큰을 반환(없으면 생성·고정)."""
    if _TOKEN_FILE.exists():
        tok = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(16)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def dashboard_url() -> str:
    """사용자가 북마크할 대시보드 URL."""
    return f"https://{SITE_ID}.web.app/{secret_token()}/"


def _access_token() -> str:
    import google.auth.transport.requests as gtr
    from google.oauth2 import service_account

    cred = service_account.Credentials.from_service_account_file(
        CRED_PATH, scopes=[_SCOPE]
    )
    cred.refresh(gtr.Request())
    return cred.token


def _api(method: str, url: str, token: str, *, body=None, raw: bytes | None = None,
         content_type: str = "application/json") -> dict:
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = r.read()
    return json.loads(payload.decode("utf-8")) if payload else {}


def deploy(files: dict[str, bytes]) -> str:
    """files({'/index.html': b'...'})를 Hosting에 배포하고 release URL을 반환.

    Hosting REST 배포 5단계: 버전 생성 → populateFiles → gzip 업로드 →
    FINALIZE → release.
    """
    token = _access_token()

    # gzip + sha256(of gzipped)
    gzipped: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    path_by_hash: dict[str, str] = {}
    for path, content in files.items():
        gz = gzip.compress(content)
        h = hashlib.sha256(gz).hexdigest()
        gzipped[path] = gz
        hashes[path] = h
        path_by_hash[h] = path

    # 1) 버전 생성
    ver = _api("POST", f"{_HOSTING_API}/sites/{SITE_ID}/versions", token, body={})
    version_name = ver["name"]  # sites/<site>/versions/<id>

    # 2) populateFiles
    pop = _api(
        "POST", f"{_HOSTING_API}/{version_name}:populateFiles", token,
        body={"files": hashes},
    )
    upload_url = pop.get("uploadUrl", "")
    required = pop.get("uploadRequiredHashes", []) or []

    # 3) 필요한 해시만 업로드 (gzip 바이트)
    for h in required:
        path = path_by_hash.get(h)
        if path is None:
            continue
        _api(
            "POST", f"{upload_url}/{h}", token,
            raw=gzipped[path], content_type="application/octet-stream",
        )

    # 4) FINALIZE
    _api(
        "PATCH", f"{_HOSTING_API}/{version_name}?update_mask=status", token,
        body={"status": "FINALIZED"},
    )

    # 5) release
    _api(
        "POST",
        f"{_HOSTING_API}/sites/{SITE_ID}/releases?version_name={version_name}",
        token, body={},
    )
    logger.info("Firebase Hosting 배포 완료: %s (%d files)", version_name, len(files))
    return dashboard_url()


# --- 비차단 발행 트리거 (json_store.save에서 자동 호출) ---
_publish_tasks: set = set()
_publishing = False   # 워커 실행 중
_dirty = False        # 발행 후 들어온 추가 변경 표시(디바운스)
_suppress = False     # 빌드 중 내부 저장으로 인한 재귀 트리거 차단


def auto_enabled() -> bool:
    """자동 발행 활성 여부 — 자격증명 + FIREBASE_PUBLISH opt-in."""
    return is_enabled() and os.getenv("FIREBASE_PUBLISH", "").lower() in (
        "1", "true", "yes", "on"
    )


async def _build_and_deploy() -> str | None:
    import asyncio

    # 무거운 모듈/순환참조 회피용 지연 import
    from bot.handlers.dashboard import build_all_dashboard_html

    global _suppress
    _suppress = True  # 빌드 중 발생하는 save_holdings(병합/보정)가 재귀 트리거하지 않도록
    try:
        files = await build_all_dashboard_html()
    finally:
        _suppress = False

    if not files:
        return None
    tok = secret_token()
    prefixed = {f"/{tok}{path}": content for path, content in files.items()}
    return await asyncio.to_thread(deploy, prefixed)


async def _publish_worker() -> None:
    global _publishing, _dirty
    if _publishing:
        return
    _publishing = True
    try:
        while _dirty:
            _dirty = False
            try:
                url = await _build_and_deploy()
                if url:
                    logger.info("대시보드 Firebase 발행 완료: %s", url)
            except Exception:
                logger.warning("대시보드 Firebase 발행 실패", exc_info=True)
    finally:
        _publishing = False


def trigger_publish() -> None:
    """대시보드 발행을 백그라운드로 시작한다(핸들러 응답을 막지 않음).

    - FIREBASE_PUBLISH opt-in + 자격증명 존재 시에만 동작(테스트/스크립트 오발행 방지).
    - 빌드 중 내부 저장(_suppress)으로 인한 재귀 트리거는 무시.
    - 발행 중 들어온 추가 변경은 _dirty로 합쳐 한 번 더 배포(연속 저장 디바운스).
    - 실행 중 이벤트 루프가 없으면(스크립트/테스트) 조용히 무시.
    """
    global _dirty
    if _suppress or not auto_enabled():
        return
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _dirty = True
    if not _publishing:
        task = loop.create_task(_publish_worker())
        _publish_tasks.add(task)
        task.add_done_callback(_publish_tasks.discard)
