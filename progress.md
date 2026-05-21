# Progress

## 완료
- [x] 선물 Phase 4: 시세 자동조회 + 수동 보정 (2026-05-21) — `bot/futures_quote.py`(수동 시세 우선 → yfinance 기초자산 폴백, 6시간 TTL), `bot/handlers/futures_quote.py` `선물시세` 명령. HTML 리포트에 "기초자산 시세 기준 추정치" 안내. pykrx 개별주식선물은 KRX 로그인 필요라 1차에선 yfinance 근사. 94 passed.
- [x] 선물 Phase 3: 회고 + 대시보드 선물 섹션 (2026-05-21) — `models/retrospective.py`에 `is_futures` 플래그, `bot/handlers/futures_retro.py`(close/roll_close 미회고 카드), `bot/futures_report.py`(미실현·증거금 잠식률·D-만기 표), `bot/html_report.py`에 선물 섹션 통합. 시세는 `_fetch_futures_prices` stub만 두고 Phase 4에서 연결. 85 passed.
- [x] 선물 Phase 2: 핸들러 — 선물진입/청산/롤오버 (2026-05-21) — `bot/handlers/futures_buy.py|sell|roll`, 키보드(`futures_direction_keyboard`, `futures_month_keyboard`, `futures_positions_keyboard`), 파서(`parsers/futures_input.py`), `main.py`에 라우터 등록. 기존 핸들러의 `other_command_filter`도 선물 명령 인지하도록 업데이트. 핸들러 14개 + 모델 15개 = 77 passed.
- [x] 선물 Phase 1: 모델/저장소/만기 계산 (2026-05-21) — `FuturesPosition`, `FuturesTransaction` 모델, `futures_positions.json`/`futures_transactions.json` 편의함수, `parsers/expiry.py`(분기물·두 번째 목요일), `tests/test_futures_models.py` 15 테스트 통과.
- [x] 사유 빠른 선택 — 전역화 + 자동손절 고정 (2026-05-20) — 최근 사유는 종목별이 아닌 전체 거래 기준 최신순으로 노출. 매도 사유에는 항상 "자동손절"을 맨 앞에 고정 노출.
- [x] 매수/매도 사유 입력 UX 개선 (2026-05-19) — 최근 사유를 클릭 버튼으로 노출 + 직접 입력 가능. 추가 매수 시 "그대로 유지 / 매수사유 이어쓰기 / 매수사유 새로쓰기" 3-옵션. 매도는 수량·매도가 입력 후 사유 단계 분리.
- [x] 매도/회고 분리 (2026-04-27) — 매도 시 회고 흐름 제거, `회고` 명령으로 미회고 매도 카드 노출 후 선택. Transaction에 `buy_thesis` 스냅샷 필드 추가
- [x] 현황 명령에 Claude KIS 모의투자 포트폴리오 HTML 추가 (2026-04-24) — stocks_battle/data_kis/ 로드, 3개 HTML(내/Claude/Claude-KIS) 전송
- [x] 현황 리포트 상단 총수익과 테이블 손익합 일치 (2026-04-21) — 신용대출 도입 후 깨진 항등식을 `total_pnl` 기반으로 통일
- [x] 현황 조회 현재가 yfinance로 정상 동작 (2026-04-20) — `fast_info.last_price`로 교체

## 진행중
- [ ] 선물 Phase 5: 만기 임박 D-3 알림 (JobQueue)

## 다음 할 것
- (없음)

## 블로커
- (없음)
