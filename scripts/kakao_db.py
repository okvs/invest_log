#!/usr/bin/env python3
"""맥 카카오톡 로컬 SQLCipher DB를 **in-process로 직접** 읽는다 (kakaocli 불필요).

기존엔 `kakaocli query` 바이너리에 SQL을 위임했지만, 같은 키·같은 cipher
파라미터로 `sqlcipher3`(libsqlcipher 링크)를 우리 파이썬 프로세스에서 직접 연다.
NomaDamas/katok 가 Rust에서 SQLCipher DB를 직접 복호화하는 것과 같은 접근이다.

cipher 파라미터 (kakaocli 소스 DatabaseReader.swift 와 동일):
    PRAGMA cipher_default_compatibility = 3   # SQLCipher 3 호환 (PBKDF2-HMAC-SHA1)
    PRAGMA key = "<256-hex 문자열>"            # passphrase 모드 (raw x'..' 아님)
(2026-06-17 실측 확인: compat=3 + 256hex passphrase 만 sqlite_master 가 열림)

키 확보 순서 (어디서도 kakaocli 미사용):
  1) ~/.cache/k-skill/kakaotalk-mac-auth.json 캐시(db_path+key) → sqlcipher3로 검증
  2) 캐시가 없거나 안 열리면 kakaotalk-mac 스킬의 **순수 파이썬 파생 함수**만
     재사용(secure_key/database_name/uuid·user_id 탐지)해 in-process로 재파생·검증·캐시

전제: macOS + 카카오톡 Mac 앱 + 터미널 전체 디스크 접근(FDA). 데몬이면 python 바이너리에 FDA.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import sqlcipher3

CACHE_PATH = Path.home() / ".cache" / "k-skill" / "kakaotalk-mac-auth.json"
_SKILL_PY = Path.home() / ".claude" / "skills" / "kakaotalk-mac" / "scripts" / "kakaotalk_mac.py"
_CIPHER_COMPAT = 3  # KakaoTalk Mac DB는 SQLCipher 3 호환 모드

_auth: tuple[str, str] | None = None  # (db_path, key) 캐시 — 재해결 비용 절감


# ---------------------------------------------------------------------------
# SQLCipher 연결
# ---------------------------------------------------------------------------
def _open(db: str, key: str) -> "sqlcipher3.Connection":
    """read-only 로 DB를 열고 cipher 파라미터를 적용한다. 검증은 호출측 책임."""
    con = sqlcipher3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    con.execute(f"PRAGMA cipher_default_compatibility = {_CIPHER_COMPAT}")
    con.execute(f'PRAGMA key = "{key}"')      # compat 설정 후 key (순서 중요)
    con.execute("PRAGMA query_only = ON")     # 우리는 절대 쓰지 않는다
    con.execute("PRAGMA busy_timeout = 5000")  # 카톡 앱이 쓰는 중이면 잠깐 대기
    return con


def _verify(db: str, key: str) -> bool:
    """(db, key) 로 실제 복호화가 되는지 sqlite_master 조회로 확인."""
    try:
        con = _open(db, key)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        con.close()
        return True
    except Exception:  # noqa: BLE001 — 잘못된 키면 'file is not a database'
        return False


# ---------------------------------------------------------------------------
# 키 확보 (kakaocli 미사용)
# ---------------------------------------------------------------------------
def _read_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _persist(user_id: int, uuid: str, db: str, dbname: str, key: str) -> None:
    payload = {
        "user_id": user_id, "uuid": uuid, "database_path": db,
        "database_name": dbname, "key": key, "source": "in-process-sqlcipher3",
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(CACHE_PATH, 0o600)
    except OSError:
        pass


def _load_skill():
    """kakaotalk-mac 스킬 모듈을 파일경로로 로드(순수 파생 함수만 재사용)."""
    if not _SKILL_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location("_kt_skill", _SKILL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _try_uid(mod, uid: int, state) -> tuple[str, str] | None:
    """user_id 후보로 키·DB파일을 파생해 검증되면 (db, key) 반환 + 캐시."""
    key = mod.secure_key(uid, state.uuid)
    dbname = mod.database_name(uid, state.uuid)
    for dbp in mod.prioritized_database_paths(state.database_files, dbname):
        if _verify(str(dbp), key):
            _persist(uid, state.uuid, str(dbp), dbname, key)
            return str(dbp), key
    return None


def _derive_in_process() -> tuple[str, str] | None:
    """스킬의 순수 파이썬 파생만 써서 in-process로 키를 재파생·검증한다."""
    mod = _load_skill()
    if mod is None:
        return None
    cached = _read_cache()
    state = mod.collect_detection_state(cached.get("uuid"))
    # 캐시의 user_id 우선, 그다음 plist 후보들
    candidates: list[int] = []
    if cached.get("user_id"):
        candidates.append(int(cached["user_id"]))
    candidates += list(state.candidate_user_ids)
    for uid in dict.fromkeys(candidates):
        hit = _try_uid(mod, uid, state)
        if hit:
            return hit
    # 최후 수단: 활성 계정 해시(sha512)에서 user_id 역추적 (느릴 수 있음)
    if state.active_account_hash:
        uid = mod.recover_user_id_from_sha512(state.active_account_hash)
        if uid is not None:
            hit = _try_uid(mod, uid, state)
            if hit:
                return hit
    return None


def resolve_auth() -> tuple[str, str]:
    """(database_path, key) 반환. 캐시 우선, 실패 시 in-process 재파생."""
    global _auth
    if _auth is not None:
        return _auth
    cached = _read_cache()
    db, key = cached.get("database_path"), cached.get("key")
    if db and key and os.path.exists(db) and _verify(db, key):
        _auth = (db, key)
        return _auth
    derived = _derive_in_process()
    if derived is None:
        raise SystemExit(
            "error: 카카오톡 DB 키를 확보하지 못했습니다.\n"
            "  - macOS + 카카오톡 Mac 앱 + 터미널/파이썬 '전체 디스크 접근(FDA)' 권한 확인\n"
            f"  - 캐시 경로: {CACHE_PATH}\n"
            "  - 스킬 파생 경로: ~/.claude/skills/kakaotalk-mac/scripts/kakaotalk_mac.py"
        )
    _auth = derived
    return _auth


# ---------------------------------------------------------------------------
# 질의 (kakaocli kc_query 대체)
# ---------------------------------------------------------------------------
def query(sql: str, params: tuple = ()) -> list[list]:
    """SQL 실행 후 행을 list[list] 로 반환 (kakaocli JSON 출력과 동일 형태).

    매 호출마다 새 연결을 열고 닫는다(장수 read-only WAL 리더의 스냅샷/락
    리스크 회피). 키 파생(KDF)은 캐시된 auth 로 1회만, 연결당 PRAGMA key 비용은
    kakaocli 서브프로세스 기동보다 훨씬 싸다.
    """
    db, key = resolve_auth()
    con = _open(db, key)
    try:
        cur = con.execute(sql, params)
        return [list(r) for r in cur.fetchall()]
    finally:
        con.close()


__all__ = ["resolve_auth", "query", "CACHE_PATH"]
