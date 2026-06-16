#!/usr/bin/env python3
"""Firebase Authentication 의 authorizedDomains 에 도메인을 추가한다.

Cloudflare 터널 주소(*.trycloudflare.com 등)에서 구글 로그인이 동작하려면 그
호스트가 Firebase Auth 허용 도메인에 있어야 한다. 서비스 계정(cloud-platform)
으로 Identity Toolkit Admin API 를 호출해 콘솔 없이 추가한다.

사용:  python3 scripts/firebase_authz_domain.py <host>   (host 예: abc-def.trycloudflare.com)
       python3 scripts/firebase_authz_domain.py --list
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import firebase_publish as fp  # noqa: E402


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: firebase_authz_domain.py <host> | --list")
        return 2
    project = fp.SITE_ID
    token = fp._access_token()
    base = f"https://identitytoolkit.googleapis.com/admin/v2/projects/{project}/config"
    cfg = fp._api("GET", base, token)
    doms = list(cfg.get("authorizedDomains", []))

    if argv[0] == "--list":
        print("\n".join(doms))
        return 0

    host = argv[0].removeprefix("https://").removeprefix("http://").rstrip("/")
    if host in doms:
        print(f"이미 등록됨: {host}")
        return 0
    doms.append(host)
    fp._api("PATCH", base + "?updateMask=authorizedDomains", token,
            body={"authorizedDomains": doms})
    print(f"추가 완료: {host}")
    print("현재 허용 도메인:", ", ".join(doms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
