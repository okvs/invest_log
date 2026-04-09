# Lessons Learned

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
