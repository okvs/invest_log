# Lessons Learned

## 2026-05-31
### 매매 복기 차트 — 가격우주 정합 선검증 + 적대적 리뷰가 잡은 함정들
- **문제**: 보유 종목별 일봉 차트에 내 매수/매도를 찍는 '복기' 기능을 만드는데, 기록된 거래 단가(삼성전자 286,000·SK하이닉스 1,649,885)가 실제 yfinance 시세와 안 맞을까 봐(스케일 우주) 마커가 캔들과 어긋날 위험이 핵심 리스크였다.
- **원인/검증**: 착수 전 yfinance 라이브를 실측하니 000660.KS=2,333,000, 005930.KS=317,000 등 **기록 단가와 동일한 스케일 우주**였다(yfinance 일봉 history 도 같은 우주). 즉 candles+markers 가 같은 축에 그대로 정렬 → ÷10 보정 불필요. "스케일 버그 의심"을 코드 짜기 전에 데이터로 먼저 죽인 게 시간을 아꼈다.
- **해결**: 23-에이전트 적대적 리뷰 워크플로우(4렌즈→검증)로 19발견 중 10확정 수정. 특히 (a) **yfinance NaN 행 → highs.max()=NaN → set_ylim(NaN) ValueError**, 그런데 호출부 _send_chart 에 try/except 가 없어 차트도 캡션도 못 보내고 복기 전체가 죽음 → dropna + math.isfinite 가드 + 폴백 try/except. (b) **transaction_ids 는 현재 보유분만** 담겨(삼성전자 6 ids vs 실제 15거래) 전체 매매여정 매칭에 못 씀 — rename/alias 대응은 '같은 티커를 공유하는 모든 종목명' 매칭으로 풀어 full journey 보존. (c) 같은 날·같은 방향 분할체결은 VWAP 1마커 합산(안 그러면 코나아이 5/19 ×6 이 겹침).
- **교훈**:
  1. **외부 시세 소스로 그릴 땐 "기록값과 같은 축인지" 먼저 실측**하고 착수. 스케일 가정은 코드가 아니라 데이터로 확인.
  2. **matplotlib 렌더는 NaN 한 칸이면 set_ylim 에서 죽는다.** 외부 데이터(yfinance)는 NaN 행을 흔히 준다 → fetch 직후 dropna + 한계값 유한성 가드 + 렌더 호출부 try/except 폴백을 기본 장착.
  3. **조인키를 "있어 보이는 것"으로 고르지 말 것**: portfolio.transaction_ids 는 현재 lot 만이라 거래이력 전체 조인엔 부적합. 이름/티커 매칭이 오히려 full history 를 살린다.
  4. 새 명령 추가 시 **모든 ConversationHandler 의 abort 필터 동기화**를 빼먹기 쉽다(cash.py 가 stale 이어서 복기·10억 등 누락 + 10억은 capital=10 오염 위험까지 잠재). 키워드 목록이 13곳에 중복 — 공용 상수화 후보.

## 2026-05-30
### 잔고 카드 — 선물 '차입금'은 현재 명목금이 아니라 진입 명목금−증거금
- **문제**: 총평가금 카드에서 신용은 빼고(−) 선물은 더하던(+) 비대칭이 헷갈렸다. 사용자가 "총평가금에서 대출 다 빼면 자산(전부청산)과 같지?"라며 gross→net 분해를 요청. 직관적으로 "선물 평가금(현재 명목금) − 증거금"을 차입금으로 빼려 했다.
- **원인**: (1) **선물 차입금을 '현재 명목금 − 증거금'으로 빼면 선물 미실현손익이 자산에서 같이 사라진다.** 현재 명목금 = 진입 명목금 + 미실현이므로, 빼야 할 고정 차입(빌린 노출)은 `진입 명목금 − 증거금`이다. 차입금은 진입 시점에 고정되며 가격으로 변하지 않는다. (2) "평가금"에는 예수금이 안 들어가므로 `총평가금 − 대출`은 '포지션 순자산'이지 '전부청산 자산'이 아니다 — 예수금(현+선)을 더해야 자산과 같아진다.
- **해결**: 항등식을 `gross(현물평가+선물 현재명목금) − 신용차입 − 선물차입(진입명목−증거금) = 포지션순자산`, `+예수금 = 전부청산 자산(= compute_balance_nav)`으로 재구성. 두 카드를 양방향으로 통일: 총자산은 순 구성요소의 **합(전부 +)**, 총평가금은 gross에서 **차입금을 뺌(둘 다 −, 대칭)**. 신용·선물 둘 다 '빚'이라 비대칭이 사라짐. html_report에 `total_futures_notional`/`total_futures_entry_notional` 누적 추가. end-to-end 테스트로 렌더된 헤드라인 == compute_balance_nav 검증(6 케이스).
- **교훈**:
  1. **레버리지 차감은 '현재 가치'가 아니라 '빌린 원금'으로**: 선물 차입 = 진입 명목금 − 증거금(고정). 현재 명목금으로 빼면 미실현이 증발한다. 신용대출이 가격과 무관히 고정인 것과 같은 논리.
  2. **'평가금'과 '자산'의 경계 = 예수금**: 평가금(보유 가치)에서 빚을 다 빼도 예수금만큼 자산에 못 미친다. "전부청산 = 자산"을 주장할 땐 +예수금 단계를 명시.
  3. 부호 비대칭(한쪽 +, 한쪽 −)이 헷갈리면 **기준선을 바꿔 대칭으로**: gross에서 모든 빚을 빼거나(전부 −), 순 조각을 모두 더하거나(전부 +). 같은 NAV를 두 방향에서 보여주면 reconcile도 눈으로 확인된다.

### 선물 매매가 예수금을 안 건드려 cash가 영구 stale — 회계 전수 감사로 발견
- **문제**: 사용자가 "선물 1계약 더 샀는데 가용현금이 안 줄었다". 저장 cash 47.18M/futures_cash 23.62M가 실측 16.49M/7.66M보다 크게 부풀려져 있었고, NAV 두 방식이 31M 어긋남.
- **원인**: 현물 buy/sell(buy.py·sell.py)은 `account["cash"]`를 갱신하는데 **선물 진입/청산/롤·broker 자동파싱(futures_buy/sell/roll, broker)은 account.futures_cash를 전혀 갱신하지 않았다.** 증거금이 한 번도 예수금에서 차감된 적이 없어 futures_cash가 거래와 무관하게 고정 → 증거금 합(56M)이 저장 futures_cash(23.6M)를 초과하는 물리적으로 불가능한 상태. 또 cash.py가 `save_account({initial_capital,cash})`로 account 전체를 덮어써 futures_cash/chat_id를 삭제할 위험까지.
- **해결**: (1) `update_account(**kwargs)` merge 헬퍼 + `adjust_futures_cash(delta)`(없으면 no-op) 신설. (2) 선물 4핸들러+broker에 진입 `-=margin` / 청산 `+=margin_release+pnl` / 롤 `+=pnl+release−new_margin` 추가. (3) cash.py를 merge로 교체. (4) html_report 현금모델을 '별도 버킷'으로 통일(cash=현물, futures_cash=선물 가용, 둘 다 증거금 차감 안 함 — 예전엔 spot=cash−futures_cash로 이중차감). (5) account.json 실측 정합. → 예수금 카드가 실측 24,152,305와 정확히 일치, NAV 갭 31M→8M.
- **교훈**:
  1. **상태를 바꾸는 모든 경로가 같은 장부를 갱신해야 한다.** 현물만 cash를 갱신하고 선물은 빠뜨리면, 한쪽 자산군 거래가 늘수록 잔액이 조용히 틀어진다. 자산군 추가 시 "이 거래가 어떤 잔액을 어떻게 바꾸나"를 체크리스트로.
  2. **물리적 불변식으로 데이터 의미를 역추론하라**: 증거금(56M) > futures_cash(7.66M)이면 futures_cash는 '총예수금'일 수 없고 '증거금 차감 후 가용분'이다. 매일 헷갈리던 의미를 추측 대신 부등식으로 확정.
  3. **dict 통째 저장(save_account)은 키 손실 위험** — 부분 갱신은 load-mutate-save(merge)로. 한 명령(예수금)이 다른 기능(선물/푸시)의 설정을 지우면 안 됨.
  4. 같은 수량을 두 화면이 다른 식으로 계산하면 갈라진다([[2026-05-30 10억 트래커 교훈]]) — 잔액기반 NAV와 P&L기반 NAV는 정의가 다르므로 reconcile 항목(stale cash, 입금 누락, 유령포지션)을 명시적으로 추적.

### 10억 트래커 — NAV는 새로 계산하지 말고 자산그래프 식을 재사용
- **문제**: "순자산 10억" 트래커를 만들 때 순자산을 어떻게 정의하느냐가 함정. 직관적으로 `자산 − 신용대출`로 또 빼고 싶지만, 그러면 신용을 이중 차감하게 됨.
- **원인**: `compute_profit_trend`의 `asset = 초기자본 + 실현 + 미실현`은 각 보유의 평단이 **전체 매입가(신용 포함분까지)** 기준이라, 미실현이 신용으로 산 주식의 손익까지 잡는다. 자기자본(초기자본)에 앵커돼 있어 이 식은 **이미 신용 차감된 순자산과 같다**(매입 시점 항등식으로 검증). 여기서 신용을 또 빼면 음수 쪽으로 틀어진다 — 2026-05-28에 이미 "신용 차감 제거"로 통일해 둔 이유.
- **해결**: 트래커는 자산그래프와 동일한 `asset` 시계열을 그대로 가져다 씀(제3의 NAV 정의를 만들지 않음). 신용대출은 차감 대신 레버리지/마진콜 같은 **위험 지표**로만 노출.
- **교훈**:
  1. 같은 수량을 두 군데서 계산하면 반드시 갈라진다. 이미 reconcile된 단일 출처(`compute_profit_trend`)를 재사용하고, 새 화면은 그 위에 얹는다.
  2. 회계 항등식은 prose로 추론하지 말고 매입/평가 시나리오로 손계산해 검증한다("초기자본+미실현 = 보유평가−신용?"을 한 케이스로 확인).
  3. 짧은 표본(57일) 수익률을 연환산하면 +7448% 같은 무의미한 값이 나온다 — 표본<6개월이면 연환산·도달예상에 반드시 caveat를 붙이거나 생략한다.

## 2026-05-27
### KIS 선물 시세 다건 조회 시 초당 거래건수 초과(EGW00201)
- **문제**: `잔고` 명령에서 선물 4건을 연속 조회하니 2번째 호출부터 `rt_cd=1, msg="초당 거래건수를 초과하였습니다", msg_cd=EGW00201` 로 실패 → 기초자산 yfinance로 폴백돼 정확도 저하.
- **원인**: `fetch_kis_futures_quote`가 포지션마다 딜레이 없이 back-to-back로 KIS 시세 endpoint를 때렸고, 매 호출마다 `KisClient`를 새로 만들어 토큰 관련 부가 요청까지 더해 한도를 빨리 소진.
- **해결**: `bot/kis_futures.py`에 (1) 호출 간 최소 간격 스로틀 `_throttle()`(기본 0.35s, `KIS_QUOTE_MIN_INTERVAL`로 조정, threading.Lock으로 스레드 안전), (2) EGW00201 감지 시 0.6s→1.2s→2.4s 백오프 재시도(최대 3회), (3) `KisClient` 프로세스당 1개 메모이즈. 적용 후 4건 모두 `src=kis`로 해결.
- **교훈**:
  1. 외부 시세 API를 루프에서 다건 호출할 땐 **호출 간 최소 간격(스로틀) + 레이트리밋 코드 감지 후 백오프 재시도**를 기본으로 둔다. `asyncio.to_thread`로 감싸도 동시성이 아니라 순차라서 간격이 없으면 그대로 초당 한도에 걸린다.
  2. 클라이언트(토큰 포함) 객체는 호출마다 새로 만들지 말고 재사용 — 인증 부가 요청이 한도를 갉아먹는다.

## 2026-05-21
### KIS Open Trading API 개별주식선물 시세 — 시장구분코드는 JF (F 아님)
- **문제**: `FHMIF10000000` 으로 개별주식선물 단축코드(`A11606` 등) 시세를 조회했더니 `rt_cd=0, msg="정상처리"` 인데 `output1`이 빈 dict로 옴.
- **원인**: 공식 KIS 예제(`examples_llm/domestic_futureoption/inquire_price`)가 `FID_COND_MRKT_DIV_CODE="F"`로 적혀 있어서 그대로 따라했는데, `F`는 **지수선물(KOSPI200 등) 전용**. 같은 endpoint·같은 TR_ID 라도 개별주식선물은 `JF`로 호출해야 데이터가 채워짐.
- **해결**: `bot/kis_futures.py`에서 `mrkt_div="JF"`로 호출. 두 값 모두 200 OK가 떨어져서 사용자가 success로 오인하기 쉬움.
- **교훈**:
  1. KIS 공식 샘플은 "지수선물" 가정으로 쓰여 있을 때가 많다. 개별주식선물(`1XX###`)은 `FID_COND_MRKT_DIV_CODE`를 `JF`로 바꿔야 함. 옵션도 마찬가지로 지수옵션 `O` ↔ 주식옵션 `JO`로 추정.
  2. KIS 응답은 잘못된 종목/시장 조합이라도 `rt_cd=0`을 떨굴 수 있다 — `output1`의 핵심 필드(`futs_prpr`) 존재 여부까지 확인하지 않으면 silent failure.

### KIS 선물 단축코드는 마스터 파일로 lookup이 정답
- **문제**: 우리 데이터는 (기초자산 종목코드 + YYYYMM)인데 KIS는 자체 단축코드(`A11606`)를 요구. 인코딩 규칙(`A` + 종목 약식 + 연 + 월)을 추측으로 합성하려 했음.
- **원인**: 종목별 약식 코드(`A116`=삼성전자, `A206`=현대모비스)는 KIS가 별도 발급하는 값이고, 신규 상장·종목 변경 시 마음대로 바뀐다. 규칙 합성으로는 안전하지 않음.
- **해결**: KIS 공식 마스터 파일 `https://new.real.download.dws.co.kr/common/master/fo_stk_code_mts.mst.zip` 을 받아서 `(info_type∈{1,3}, 기초자산 단축코드, 한글종목명에 "F YYYYMM" 패턴)` 로 필터링해 단축코드를 추출. `data/kis_fo_stk_code.mst`에 캐싱(TTL 24h).
- **교훈**:
  1. 외부 API 종목 식별자는 **마스터 파일/검색 API로 lookup**이 원칙. 규칙 추측은 만기/연도 롤오버 시 깨짐.
  2. KIS 마스터의 `월물구분코드`(0연결~4차차차근월물)는 "오늘 기준 상대 위치"라서 시간이 지나면 의미가 바뀜. 절대 시점이 필요하면 `한글종목명` 안의 YYYYMM 텍스트가 안정적.

## 2026-04-21
### 신용거래 도입 후 현황 리포트 상단 총수익과 테이블 손익합 불일치
- **문제**: `/현황` HTML 리포트에서 상단 "총 수익" 카드 값과 보유종목 테이블의 수익 컬럼 합계가 맞지 않음. 신용거래 + 예수금 설정 기능을 도입한 이후 발생.
- **원인**: `bot/html_report.py`의 `show_cash` 분기에서 `total_return = (cash_remaining + total_eval) - initial_capital`로 계산. 그런데 신용거래 시 `cash_override`로 넘어오는 `cash_remaining`은 **신용대출을 차감한 실제 예수금**이고, `total_invested`는 여전히 **신용대출 포함 전체 포지션 가치**. 두 변수의 기준이 달라서 `cash_remaining + total_eval`이 `sum(credit_loan)` 만큼 실제 순자산보다 크게 잡힘 → 상단 수익이 대출금만큼 과대계상.
- **해결**: 상단 카드도 테이블과 동일하게 `total_pnl = total_eval - total_invested`를 사용하도록 통일. 이 값은 신용대출 유무와 무관하게 항상 정확(각 행의 `pnl = eval - invested` 합과 수학적으로 동일).
- **교훈**:
  1. 동일한 "수익"이라는 수치가 UI 상에서 두 군데(요약 카드/상세 테이블) 이상 표시될 때는 **같은 원천 값에서 파생**시켜야 한다. 서로 다른 공식을 쓰면 한 쪽이 바뀔 때 반드시 드리프트한다.
  2. 신용/레버리지를 다루는 계산에서 `invested`/`cash`/`asset`이 각각 "대출 포함"인지 "대출 차감 후"인지 변수별 기준을 명확히 문서화하거나 네이밍에 반영해야 함. 여기선 `total_invested`(대출 포함) vs `cash_remaining`(대출 차감) 혼합이 원인.
  3. 기능을 **확장**(신용거래 도입)할 때 기존 수식이 "무대출 가정"으로 수렴해 우연히 맞던 경우가 있다 — 확장 후 항등식을 재검증해야 한다. (기존: `cash_remaining = initial_capital - total_invested` → `total_asset - initial_capital = total_pnl`, 신용 도입 후에는 이 항등식이 깨짐)

## 2026-04-20
### 현황 조회 시 yfinance가 한국 종목 현재가를 반환하지 않음
- **문제**: `/현황` 실행 시 한국 종목(`.KS`/`.KQ`)의 현재가가 비어서 평가금/수익률이 표시되지 않음. 로그에 `possibly delisted; no price data found (period=1d)` 에러.
- **원인**: `bot/formatters.py`의 `fetch_current_prices`가 `yf.download(tickers, period="1d")`를 사용. 이 API는 "최근 1거래일 **일봉**"을 요청하므로, 당일 일봉이 yfinance에 아직 반영되지 않은 시점(한국 장중/장 직후)에는 한국 종목에 대해 빈 DataFrame을 반환. 미국 종목만 금요일 종가가 잡혀 정합성이 깨짐.
- **해결**: 종목별 `yf.Ticker(sym).fast_info.last_price`로 교체. 이 속성은 장중이면 체결가, 마감 후면 당일 종가를 즉시 반환. 단, `fast_info['last_price']`처럼 dict 스타일이 아닌 **속성 접근**(`.last_price`)을 써야 정상값이 나옴 — dict 스타일은 None을 반환함.
- **교훈**:
  1. yfinance에서 "현재가"는 `download(period='1d')`가 아니라 `Ticker.fast_info.last_price`로 조회해야 함. `download`는 일봉(historical bar) 전용.
  2. `fast_info`는 dict-like지만 `.last_price`/`.previous_close` 등 **속성 접근이 공식 인터페이스**. `.get('last_price')`나 `['last_price']`는 일부 키에서 None을 반환할 수 있음.
  3. 한국 주식은 당일 일봉 반영이 미국보다 느릴 수 있으니, 다중 마켓 포트폴리오에서 period='1d'는 특히 위험.

## 2026-04-09
### 수정 rename 충돌로 인한 중복 holding 생성
- **문제**: 같은 종목이 서로 다른 이름으로 두 개 등록된 상태(`반도체레버리지` 640주, `KODEX반도체레버리지` 116주)에서 `수정`으로 한 쪽을 다른 쪽과 같은 이름으로 rename하자 포트폴리오에 **같은 name/ticker를 가진 두 행**이 남음. 이후 매수가 첫 매칭 행만 갱신하고 매도/현황 조회는 두 행을 합쳐 보여주니 수량이 실제보다 많게 나오고 정합성이 깨짐.
- **원인**: `bot/handlers/edit.py`의 `_receive_edit`가 이름을 바꿀 때 **다른 holding과 name/ticker가 겹치는지 검사하지 않음**. 반면 `bot/handlers/buy.py`의 `_process_and_save`는 같은 ticker면 기존 행에 자동 병합하고, `broker.py`도 기존 보유를 감지하는데, edit만 이 정합성 로직이 빠져 있었음.
- **해결**: `_receive_edit`에서 새 name/ticker를 확정한 뒤, 수정 대상이 아닌 holding 중 name 또는 ticker가 일치하는 행을 찾아 **수량/total_invested/transaction_ids를 합치고 avg_price를 가중평균(=total_invested/qty)으로 재계산**. 병합된 행은 리스트에서 제거하고 성공 메시지에 "중복 N건을 병합했습니다"를 덧붙여 사용자에게 알림. 회귀 테스트(`tests/scenarios/edit_rename_collision_merge.yaml`)로 재현/검증.
- **교훈**:
  1. 한 리소스(holding)를 수정하는 경로가 여러 개(buy, broker, edit)일 때, "기존 엔티티와의 충돌 처리" 책임을 한 경로에만 두면 나머지 경로에서 반드시 구멍이 생김. 충돌/병합 로직은 모든 쓰기 경로에서 동일하게 적용되어야 함.
  2. list-of-dict 데이터 모델에서 `(name, ticker)`가 사실상 natural key인데 스키마 수준의 unique 제약이 없음 — write 시점에 duplicate detection을 항상 해줘야 안전.
  3. "기존 이름 → 재명명으로 합쳐지는" 케이스는 단위 테스트로 놓치기 쉬움. 시나리오 러너로 "setup에 두 행 → rename → 매수 → 매도"를 전 구간 검증해야 재발 방지됨.
  4. 데이터 복구 스크립트도 같은 함정에 빠질 수 있음 — 이 버그 수습 중에 내가 재구성 스크립트로 복구한 portfolio.json에 같은 병합 모호성으로 **holding이 두 번 저장돼 수량이 2배**가 되는 2차 사고가 났음. 복구 후엔 `len({(name, ticker) for h in holdings}) == len(holdings)` 같은 최종 검증을 꼭 거쳐야 함.

### 여러 ConversationHandler 간 orphan state 문제
- **문제**: `매수` 타이핑 후 KB증권 체결 메시지를 붙여넣고 "그대로 유지" 버튼으로 완료한 뒤, `매도`를 입력하면 "매수 기록이 취소되었습니다" 메시지가 뜸. 매도 대화는 시작되지 않음.
- **원인**: `main.py`에서 `broker_conversation`이 `buy_conversation`보다 먼저 등록돼 있고, PTB는 하나의 ConversationHandler가 update를 처리하면 같은 group의 다른 handler에는 전달하지 않음. 사용자 흐름:
  1. `매수` → `buy_conversation`이 state 0 (INPUT) 진입
  2. `[KB증권]` 메시지 붙여넣기 → `broker_conversation`이 entry_point로 가로채 처리 (buy는 이 메시지를 아예 못 봄)
  3. broker 완료 → broker state는 END, 그런데 buy는 state 0에 **고아 상태로 남음** (각 ConversationHandler는 독립된 `_conversations` dict로 state 관리)
  4. `매도` 입력 → buy state 0의 `other_cmd` 필터가 매칭 → `_abort` 발동 → "매수 기록이 취소되었습니다"
- **해결**: `broker._receive_broker_msg` entry_point 맨 앞에서 `_end_other_conversations()` 헬퍼를 호출해, 다른 ConversationHandler(buy/sell/edit)의 `_conversations` dict에서 현재 (chat_id, user_id) 키를 제거. broker가 메시지를 가로채는 순간 기존 대화를 명시적으로 끝내는 것.
- **교훈**:
  1. 여러 ConversationHandler가 병렬로 존재할 때, 한 쪽이 entry_point로 update를 가로채면 **다른 쪽은 자신의 state가 만료됐다는 사실을 알 수 없음**. state는 완전히 독립적.
  2. 한 사용자가 동시에 여러 대화에 참여하는 것을 허용하지 않으려면, 진입점에서 **명시적으로 다른 대화의 state를 정리**해줘야 함.
  3. `conversation_timeout` 기반 자동 정리는 APScheduler 의존성이 필요하므로, 의존성 없이 해결하려면 위처럼 수동 정리가 최선.
  4. 이런 버그는 단위 테스트로 재현이 어려움 — YAML 시나리오 러너(`tests/test_scenarios.py`)로 `send` / `click` 시퀀스를 실제 Application에 주입해 검증하는 것이 효과적. 수정 전/후로 실패/통과가 확실히 갈리는지도 체크.
  5. **로그가 진실**. 사용자의 설명("그대로 유지 누르고 매도했어")은 간단한 흐름을 가정하게 했지만, 실제 로그는 "매수 타이핑 → 증권사 메시지 붙여넣기"라는 다른 흐름이었음. 파일 로깅(`logs/bot.log`)을 먼저 확보해서 진짜 이벤트 시퀀스를 확인해야 추측이 아닌 원인 분석이 가능.

## 2026-05-30
### 누락 거래 소급 입력 — 날짜만 고치면 안 되는 경우 (코스트베이시스 순서 의존성)
- **문제**: 사용자가 5/26 제주반도체 매수(118,800×50)를 "까먹고 못 넣었다"고 했으나, 데이터를 보니 단가·수량·금액이 원 단위까지 동일한 거래가 이미 `2026-05-28T11:51:56`로 들어가 있었음(= 누락이 아니라 **날짜 오기**). 단순히 그 거래의 날짜만 5/28→5/26으로 바꾸자, 그 사이에 끼어 있던 5/27 매도의 실현손익과 보유 평단이 어긋남(37,500원).
- **원인**: 이 시스템은 평단을 **거래 순서대로** 계산한다 — `Holding.add_buy`는 매수 시 가중평균을 재계산하고, `remove_sell`은 매도 시 `total_invested = avg_price × 잔량`으로 **그 시점 평단 기준**으로 차감하며, `sell.py`는 매도 PL을 `(price − 그 시점 avg_price) × qty`로 계산해 **transactions.json에 박제(stored)**한다. 따라서 매수를 매도보다 *앞으로* 옮기면 매도 시점 평단이 바뀌고(120,300→119,550), 실현손익(−645,000→−607,500)·잔여 평단(107,933→108,183)·투자원금(16,190,000→16,227,500)이 전부 달라진다. 차이 = 50주 × (120,300−119,550) = 37,500원.
- **추가 함정**: 자산그래프(`asset_history.py`)는 **미실현은 거래 replay로 평단을 재구성**하지만 **실현손익은 stored `profit_loss`를 그대로 합산**한다(line 247). 즉 날짜만 고치면 미실현은 새 순서(108,183), 실현은 옛 순서(−645,000)를 섞어 써서 그래프가 37,500원 자기모순에 빠진다.
- **해결**: 날짜 변경(transactions.json) + 끼어 있던 5/27 매도의 `profit_loss`/`profit_loss_pct` 재계산 + live `portfolio.json`의 `avg_price`/`total_invested`까지 **세 곳을 함께** 보정. 검증은 실제 `Holding` 모델로 해당 종목 거래를 날짜순 replay해 portfolio.json과 일치(qty/avg/invested)하고 매도 PL이 stored값과 일치하는지 assert. `data/`는 `.gitignore` 대상이라 커밋 없음.
- **교훈**:
  1. 거래 시계열은 `date`로 정렬·replay되므로 파일 내 **순서가 아니라 `date` 필드만 정확하면** 백테스트·자산그래프는 자동으로 올바른 위치에 끼워 넣는다. 단, 시각이 아닌 **날짜(day) 단위**로 버킷팅하므로 같은 날 안의 시각 순서는 일자별 보유엔 영향 없음.
  2. 그러나 평단(코스트베이시스)은 **순서 의존적**이다. 소급 입력/날짜수정으로 매수를 매도보다 앞으로 옮기면 매도 시점 평단이 바뀐다 → 단순 append나 날짜수정만으로는 부족하고, **사이에 매도가 있으면** 그 매도의 stored PL과 holding 평단/원금을 함께 재계산해야 정합이 맞는다.
  3. "거래 추가했나/안 했나"는 사용자 기억보다 **데이터가 진실**. 추가 전 동일 종목 거래를 단가·수량·금액으로 대조해 중복/오기 여부를 먼저 확인할 것. 여기선 그 대조 덕분에 중복 매수를 막았다.
  4. live 상태(`portfolio.json`, `account.json`)는 증분 갱신이라 **거래로부터 재계산하는 로직이 없다**. 거래만 손대면 이 둘은 안 맞으니, 정합이 필요하면 같은 거래를 모델 산식대로 replay해 맞춰주고 최종에 assert로 검증해야 한다.
