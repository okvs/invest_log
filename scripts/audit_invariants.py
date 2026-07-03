"""회계 불변식 감사 CLI.

  .venv/bin/python scripts/audit_invariants.py            # 검사 + 리포트, error 있으면 exit 1
  .venv/bin/python scripts/audit_invariants.py --notify   # 새 위반이면 웹 푸시도 발송
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.audit import audit_and_notify, run_audit  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="장부 불변식 감사")
    ap.add_argument("--notify", action="store_true", help="새 위반 조합이면 웹 푸시 발송")
    args = ap.parse_args(argv)

    violations = audit_and_notify() if args.notify else run_audit()
    if not violations:
        print("✅ 불변식 전부 통과 — 장부 건강")
        return 0
    for x in violations:
        print(f"[{x.severity}] {x.code}: {x.message}")
    return 1 if any(x.severity == "error" for x in violations) else 0


if __name__ == "__main__":
    raise SystemExit(main())
