"""종목코드 조회 테스트 — 봇 서버에서 직접 실행하여 확인.

Usage: python test_ticker_lookup.py
"""
import logging

logging.basicConfig(level=logging.DEBUG)

from parsers.input_parser import lookup_ticker, _lookup_naver, _lookup_krx

test_names = ["삼성전자", "카카오", "NAVER", "셀트리온"]

for name in test_names:
    print(f"\n{'='*40}")
    print(f"종목명: {name}")
    print(f"  네이버: {_lookup_naver(name)!r}")
    print(f"  KRX:   {_lookup_krx(name)!r}")
    print(f"  통합:  {lookup_ticker(name)!r}")
