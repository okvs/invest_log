"""data/ 일일 스냅샷 백업 — 원장 유실(실수 삭제·파일 파손·repo 오염) 대비.

data/ 는 gitignore 라 버전 관리가 전혀 없고, 수동 .bak-* 사본도 같은 폴더에만
있었다. 여기서 하루 1회 tar.gz 스냅샷을 프로젝트 밖으로 남긴다.

  · 대상: json_store.DATA_DIR 의 최상위 파일 전부
  · 제외: *.lock / *.tmp / *.mst(재다운 가능 캐시) / *.bak*(이미 사본) /
          하위 디렉토리(balance_shots 사진 등)
  · 위치: iCloud Drive 가 있으면 iCloud/invest_log_backup/ (기기 밖 사본),
          없으면 ~/Backups/invest_log/
  · 보관: data-YYYYMMDD.tar.gz 최근 30개, 초과분 자동 삭제
  · 트리거: dash-refresh 데몬 루프가 매 주기 maybe_backup() 호출 —
    오늘자 아카이브가 이미 있으면 no-op 이라 하루 1회만 생성된다.
"""
from __future__ import annotations

import logging
import tarfile
from datetime import datetime
from pathlib import Path

from storage import json_store

logger = logging.getLogger(__name__)

KEEP_ARCHIVES = 30
_EXCLUDE_SUFFIXES = (".lock", ".tmp", ".mst")


def backup_root() -> Path:
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.is_dir():
        return icloud / "invest_log_backup"
    return Path.home() / "Backups" / "invest_log"


def _wanted(fp: Path) -> bool:
    if not fp.is_file():
        return False
    if fp.suffix in _EXCLUDE_SUFFIXES or ".bak" in fp.name:
        return False
    return True


def make_backup(*, root: Path | None = None, now: datetime | None = None) -> Path | None:
    """오늘자 스냅샷 생성(+오래된 것 프루닝). 이미 있으면 None."""
    root = root or backup_root()
    now = now or datetime.now()
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"data-{now.strftime('%Y%m%d')}.tar.gz"
    if out.exists():
        return None

    files = sorted(fp for fp in json_store.DATA_DIR.iterdir() if _wanted(fp))
    if not files:
        return None
    tmp = out.with_suffix(".tar.gz.tmp")
    try:
        # 스냅샷 일관성: 장부 파일들이 쓰이는 중이 아닐 때 담기도록 락 아래에서 수행
        with json_store.transaction(json_store.PORTFOLIO_FILE, json_store.ACCOUNT_FILE,
                                    json_store.TRANSACTIONS_FILE):
            with tarfile.open(tmp, "w:gz") as tar:
                for fp in files:
                    tar.add(fp, arcname=fp.name)
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    # 프루닝 — 최근 KEEP_ARCHIVES 개만 유지
    archives = sorted(root.glob("data-*.tar.gz"))
    for old in archives[:-KEEP_ARCHIVES]:
        try:
            old.unlink()
        except OSError:
            pass
    logger.info("data 백업 완료: %s (%d files)", out, len(files))
    return out


def maybe_backup() -> Path | None:
    """하루 1회 백업 — 오늘자가 이미 있으면 no-op. 실패해도 예외를 삼키고 로그만."""
    try:
        return make_backup()
    except Exception:  # noqa: BLE001
        logger.warning("data 백업 실패(다음 주기 재시도)", exc_info=True)
        return None
