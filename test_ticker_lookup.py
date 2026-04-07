"""종목코드 조회 테스트 — 봇 서버에서 직접 실행하여 확인.

Usage: python test_ticker_lookup.py
"""
import logging

logging.basicConfig(level=logging.DEBUG)

from parsers.input_parser import lookup_ticker, search_stocks

test_names = ["삼성전자", "카카오", "NAVER", "셀트리온", "삼성"]

for name in test_names:
    print(f"\n{'='*40}")
    print(f"검색어: {name}")
    candidates = search_stocks(name)
    if candidates:
        for c in candidates:
            suffix = ".KQ" if c.market == "KOSDAQ" else ".KS"
            print(f"  {c.name} ({c.code}{suffix}) [{c.market}]")
    else:
        print("  검색 결과 없음")
    print(f"  lookup_ticker: {lookup_ticker(name)!r}")
