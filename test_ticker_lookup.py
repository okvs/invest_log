"""종목코드 조회 테스트 — 봇 서버에서 직접 실행하여 확인.

Usage: python test_ticker_lookup.py
"""
import logging
import sys

sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO)

from parsers.input_parser import search_stocks

test_queries = ["삼성전자", "삼성", "카카오", "한화솔"]

for query in test_queries:
    print(f"\n{'='*40}")
    print(f"검색어: {query}")
    candidates = search_stocks(query)
    if candidates:
        for c in candidates:
            suffix = ".KQ" if c.market == "KOSDAQ" else ".KS"
            print(f"  {c.name} ({c.code}{suffix}) [{c.market}]")
    else:
        print("  검색 결과 없음")
