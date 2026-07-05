#!/usr/bin/env python3
"""Firebase Authentication 의 authorizedDomains 에 도메인을 추가한다.

Cloudflare 터널 주소(*.trycloudflare.com 등)에서 구글 로그인이 동작하려면 그
호스트가 Firebase Auth 허용 도메인에 있어야 한다. 서비스 계정(cloud-platform)
으로 Identity Toolkit Admin API 를 호출해 콘솔 없이 추가한다.

quick tunnel 은 재시작마다 주소가 바뀌므로, 새 주소를 추가할 때 이전
*.trycloudflare.com 주소들은 함께 제거한다(죽은 도메인 누적 방지).

사용:  python3 scripts/firebase_authz_domain.py <host>   (host 예: abc-def.trycloudflare.com)
       python3 scripts/firebase_authz_domain.py --list
       python3 scripts/firebase_authz_domain.py --prune   (현재 터널 외 trycloudflare 제거)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import firebase_publish as fp  # noqa: E402

_TUNNEL_URL_FILE = Path(__file__).resolve().parent.parent / "data" / "tunnel_url.txt"


def _current_tunnel_host() -> str:
    try:
        url = _TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return url.removeprefix("https://").removeprefix("http://").rstrip("/")


def _is_tunnel(dom: str) -> bool:
    return dom.endswith(".trycloudflare.com")


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: firebase_authz_domain.py <host> | --list | --prune")
        return 2
    project = fp.SITE_ID
    token = fp._access_token()
    base = f"https://identitytoolkit.googleapis.com/admin/v2/projects/{project}/config"
    cfg = fp._api("GET", base, token)
    doms = list(cfg.get("authorizedDomains", []))

    if argv[0] == "--list":
        print("\n".join(doms))
        return 0

    if argv[0] == "--prune":
        keep = _current_tunnel_host()
        new_doms = [d for d in doms if not _is_tunnel(d) or d == keep]
        removed = [d for d in doms if d not in new_doms]
        if not removed:
            print("제거할 죽은 터널 도메인 없음")
            return 0
        fp._api("PATCH", base + "?updateMask=authorizedDomains", token,
                body={"authorizedDomains": new_doms})
        print("제거:", ", ".join(removed))
        print("현재 허용 도메인:", ", ".join(new_doms))
        return 0

    host = argv[0].removeprefix("https://").removeprefix("http://").rstrip("/")
    # 새 터널 주소를 넣으면서 이전 터널 주소들은 청소(재시작마다 누적되는 것 방지)
    new_doms = [d for d in doms if not _is_tunnel(d) or d == host]
    if host not in new_doms:
        new_doms.append(host)
    if new_doms == doms:
        print(f"이미 등록됨: {host}")
        return 0
    fp._api("PATCH", base + "?updateMask=authorizedDomains", token,
            body={"authorizedDomains": new_doms})
    dropped = [d for d in doms if d not in new_doms]
    print(f"추가 완료: {host}" + (f" (이전 터널 제거: {', '.join(dropped)})" if dropped else ""))
    print("현재 허용 도메인:", ", ".join(new_doms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
