# Progress

## 완료
- [x] 4사분면 축 범위 재조정 + 50% 초과 수익률 물결 표시 + invset_mind 배경색 적용 (2026-05-22) — Y축(비중)은 `(max_w+10)/10` ceil×10 동적, X축(수익률)은 ±50% 고정. 50% 초과 종목은 `_wave_glyph()` SVG 사인 글리프로 클립 표시 + 실제 % 라벨 함께. 사분면 배경색은 invset_mind의 연빨강(Q1)/연파랑(Q2)/연초록(Q3)/연노랑(Q4)에 맞춰 다크 테마용 wash로, 배지도 같은 계열로 매칭. 122 passed.
- [x] 4사분면 산점도 라벨을 배지 형태로 + 크기 720×720으로 확대 (2026-05-22) — 색 이모지(🔴🚨🟢🟡)가 데이터 점 모양과 혼동되어, 라벨을 배경색 칠한 이름표(rounded rect)로 교체. `_quadrant_badge()` 추가, 텍스트 폭은 CJK/ASCII 글자수 추정. 사분면 외곽 모서리에 inset 4px로 부착. 122 passed.
- [x] 4사분면 산점도 축 재배치: X=수익률, Y=비중(미러), 정사각 plot로 4분면 동일 크기 (2026-05-22) — 원점(0,0)이 plot 정중앙. Y축(비중)은 위·아래 둘 다 양수 라벨(미러), X축(수익률)은 부호 있는 표준 표기. 라벨도 사분면별 의미 풀어서: 🔴 잘하는 것(高비중·수익) / 🚨 큰 위험(高비중·손실) / 🟢 다행(低비중·손실) / 🟡 비중 부족(低비중·수익). 122 passed.
- [x] 잔고 HTML에 비중×수익률 4사분면 산점도 추가 (2026-05-22) — `bot/html_report.py`에 `_build_quadrants_svg()` 신설. 인라인 SVG로 원점(0,0) 기준 4사분면 격자, 10% 단위 tier 마커(T1=작은원/T2=중간원/T3=큰원/T4=육각형/T5=별, SIZE_TABLE/MARKER_TABLE invset_mind 룰 그대로), 종목명 우측 라벨, 사분면 4개 라벨(🔴 잘하는 것/🚨 큰 위험/🟡 비중 부족/🟢 다행), T1~T5 항상 표시되는 범례, "점 크기 = 비중 tier (10% 단위)" 캡션. 데이터는 이미 계산된 rows(eval, pnl_pct) 그대로 재사용. 122 passed.
- [x] KIS Open Trading API 개별주식선물 실시간 시세 연동 (2026-05-21) — `stocks_battle/broker/kis.py`에 `futures_current_price` 추가 (TR_ID `FHMIF10000000`, FID_COND_MRKT_DIV_CODE `JF`). `invest_log/bot/kis_futures.py` 신설: KIS 선물 마스터(`fo_stk_code_mts.mst`)를 받아 (기초자산 종목코드 + YYYYMM) → KIS 단축코드 매핑 + 5분 가격 캐시. `bot/futures_quote.py` 우선순위 재배치: 수동 시세 → KIS → 기초자산 yfinance 폴백. 키는 stocks_battle/.env 절대경로로 로드(default 실전). HTML 선물 표 현재가 셀에 오늘 등락률(%) 표시, KIS로 다 채워지면 "추정치" 안내 제거. 실전 키 라이브 확인: 삼성전자 6월물 30만/+7.53%, 현대모비스 6월물 67.2만/+23.99%. 122 passed.
- [x] '잔고' = '현황' alias + 현재가 옆 오늘 등락률 표시 (2026-05-21) — `main.py` 한국어 키워드 `잔고` 바인딩, 모든 ConversationHandler abort 정규식(11개)에 잔고 추가. `bot/formatters.py` `fetch_current_quotes()`로 yfinance previous_close 기반 일간 변동률 함께 반환. 121 passed.
- [x] 선물 현황 리포트에 섹터 컬럼 추가 + 진입 플로우에 섹터 단계 (2026-05-21) — `bot/futures_report.py` 표 맨 앞에 `섹터` 컬럼 추가. `futures_buy.py`/`broker.py` 양쪽 플로우에 증거금 다음 단계로 섹터 입력을 끼움(같은 종목의 기존 선물/현물 섹터를 자동 제안, `.` 입력 시 그대로 사용). 시드 데이터 갱신: 삼성전자 6/7월물 → 반도체 / "에이전트 토큰 롱", 현대모비스 6월물 → 로봇 / "피지컬ai". 121 passed.
- [x] 잔고 백로딩 + 증거금률 카드 + 근월물 허용 (2026-05-21) — `data/futures_positions.json`에 보유 3건 시드(삼성전자 6/7월물, 현대모비스 6월물). `parsers/expiry.py`에 `upcoming_months`(분기물 외 근월물 포함) 추가, `parse_contract_month` 분기물 제한 해제(1~12월 허용). `keyboards.futures_margin_rate_keyboard`(18/30/32.85/36/40/50% + 원화 직접입력 + 종목별 최근 rate 학습). `broker.py`가 진입 단계에서 카드 노출 → 클릭 시 단가×계약×승수×rate 자동 계산. `storage/json_store.py`에 `save_futures_margin_rate`. 121 passed.
- [x] KB증권 선물옵션 체결 메시지 자동 파싱 + chat_id 자동 캐싱 (2026-05-21) — `parsers/input_parser.py`에 `FuturesBrokerMessage`/`_parse_kb_futures_message`(종목명 `<이름> F YYYYMM ( 승수 )` 패턴). `broker.py`가 isinstance로 선물 분기 → **매수체결/매도체결과 기존 포지션 방향으로 진입(신규/추가) vs 청산을 자동 추론**. 진입은 증거금+사유, 청산은 사유만 묻고 처리. 만기일은 `second_thursday`로 자동 계산. `main.py`에 `TypeHandler` group=-1로 모든 update에서 chat_id 자동 캐싱 — `/start` 없이도 푸시 알림 동작. 단가 가설은 "총 체결대금 / (계약수×승수)" = A안. 117 passed.
- [x] 선물 Phase 5: 만기 임박 알림 + 만기점검 명령 (2026-05-21) — `bot/futures_alerts.py`(D-3 이하 포지션 collect→render→push, 같은 날 중복 발송 방지), 매일 08:00 KST `JobQueue.run_daily`로 자동 실행. `만기점검` 즉시 명령. `/start` 시 chat_id 자동 캐싱. requirements에 `python-telegram-bot[job-queue]`로 APScheduler 의존성 명시. 105 passed.
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
- (없음)

## 다음 할 것
- (없음)

## 블로커
- (없음)
