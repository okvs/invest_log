# 투자 로그 텔레그램 봇 — Agent 지침서

이 문서는 이 프로젝트에 참여하는 AI Agent가 코드를 읽고, 수정하고, 확장할 때 참고하는 전체 프로젝트 가이드입니다.

---

## 1. 프로젝트 개요

- **목적**: 주식 매수/매도를 텔레그램 봇으로 기록하고, 섹터별 대시보드와 투자 복기(회고) 시스템을 제공
- **런타임**: 맥미니 로컬 상시 가동 (polling 방식)
- **언어/프레임워크**: Python 3.10+, `python-telegram-bot>=21.0` (async API)
- **데이터 저장**: JSON 파일 (`data/` 디렉토리, filelock으로 동시 접근 보호)
- **대화 상태**: `PicklePersistence` — 봇 재시작 시 진행 중인 ConversationHandler 상태 보존

---

## 2. 디렉토리 구조

```
invest_log/
├── .env                  # BOT_TOKEN (gitignore 대상)
├── .env.example          # 환경변수 템플릿
├── .gitignore
├── requirements.txt      # python-telegram-bot, python-dotenv, filelock
├── main.py               # 엔트리포인트: Application 빌드, 핸들러 등록, polling
├── bot/
│   ├── __init__.py
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── buy.py        # /buy (/매수) ConversationHandler
│   │   ├── sell.py       # /sell (/매도) + 회고 ConversationHandler
│   │   ├── dashboard.py  # /dashboard (/현황)
│   │   └── help.py       # /help (/도움말)
│   ├── keyboards.py      # InlineKeyboard 빌더 + callback_data 상수
│   └── formatters.py     # 대시보드/매수/매도 텍스트 포맷팅
├── models/
│   ├── __init__.py
│   ├── portfolio.py      # Holding 데이터클래스
│   ├── transaction.py    # Transaction 데이터클래스
│   └── retrospective.py  # Retrospective 데이터클래스
├── storage/
│   ├── __init__.py
│   └── json_store.py     # JSON CRUD + filelock
├── parsers/
│   ├── __init__.py
│   └── input_parser.py   # 한국어 입력 파싱 (BuyInput, SellInput)
└── data/                 # 런타임 생성, gitignore 대상
    ├── portfolio.json
    ├── transactions.json
    └── retrospectives.json
```

---

## 3. 데이터 모델

### Holding (models/portfolio.py)
보유 종목 1개를 나타냄. `portfolio.json`의 `holdings` 배열에 저장.

| 필드 | 타입 | 설명 |
|------|------|------|
| id | str (UUID) | 고유 식별자 |
| name | str | 종목명 (예: "삼성전자") |
| sector | str | 섹터 (예: "반도체") |
| buy_date | str | 최초 매수일 (YYYY-MM-DD) |
| avg_price | float | 평균 매수 단가 |
| quantity | int | 보유 수량 |
| total_invested | float | 총 투자금 |
| buy_thesis | str | 매수 근거 |
| research_notes | str | 참고 자료 |
| transaction_ids | list[str] | 연결된 거래 ID 목록 |

**주요 메서드:**
- `add_buy(price, quantity, transaction_id)` — 추가 매수, 평균단가 재계산
- `remove_sell(quantity)` — 매도 시 수량 차감 (초과 시 ValueError)
- `to_dict()` / `from_dict(data)` — 직렬화

### Transaction (models/transaction.py)
매수/매도 거래 1건. `transactions.json`의 `transactions` 배열에 저장.

| 필드 | 타입 | 적용 | 설명 |
|------|------|------|------|
| id | str (UUID) | 공통 | 고유 식별자 |
| type | str | 공통 | "buy" 또는 "sell" |
| name | str | 공통 | 종목명 |
| sector | str | 공통 | 섹터 |
| date | str (ISO) | 공통 | 거래 일시 |
| price | float | 공통 | 거래 단가 |
| quantity | int | 공통 | 거래 수량 |
| total_amount | float | 공통 | 총 거래금액 |
| thesis | str | buy | 매수 근거 |
| research_notes | str | buy | 참고 자료 |
| profit_loss | float | sell | 수익/손실 금액 |
| profit_loss_pct | float | sell | 수익률 (%) |
| sell_reason | str | sell | 매도 사유 |
| holding_id | str | sell | 연결된 Holding ID |
| retrospective_id | str | sell | 연결된 회고 ID |

### Retrospective (models/retrospective.py)
매도 후 투자 복기. `retrospectives.json`의 `retrospectives` 배열에 저장.

| 필드 | 타입 | 설명 |
|------|------|------|
| id | str (UUID) | 고유 식별자 |
| transaction_id | str | 연결된 매도 거래 ID |
| stock_name | str | 종목명 |
| sell_date | str | 매도일 |
| original_thesis | str | 원래 매수 근거 |
| thesis_correct | bool/None | 판단 평가 (True/False/None=부분적) |
| what_went_well | str | 잘한 점 |
| regrets | str | 아쉬운 점 |
| avoidable | str | 회피 가능 여부 |
| lessons | str | 교훈 |
| created_at | str (ISO) | 생성 일시 |

---

## 4. storage/json_store.py 사용법

```python
from storage import json_store

# 저수준 API
data = json_store.load("portfolio.json")   # dict 반환, 없으면 {}
json_store.save("portfolio.json", data)

# 편의 함수
holdings = json_store.load_holdings()       # list[dict]
json_store.save_holdings(holdings)

transactions = json_store.load_transactions()
json_store.save_transactions(transactions)

retrospectives = json_store.load_retrospectives()
json_store.save_retrospectives(retrospectives)
```

- 모든 읽기/쓰기는 `filelock`으로 보호됨
- `ensure_ascii=False` — 한국어 그대로 저장
- `data/` 디렉토리는 자동 생성됨

---

## 5. parsers/input_parser.py 사용법

```python
from parsers.input_parser import parse_buy_input, parse_sell_input

# 매수 파싱 (최소 5줄)
buy = parse_buy_input("삼성전자\n반도체\n10주\n72000원\nAI 수요 증가 전망")
# buy.name, buy.sector, buy.quantity, buy.price, buy.thesis, buy.research_notes

# 매도 파싱 (최소 4줄)
sell = parse_sell_input("삼성전자\n5주\n85000원\n목표가 도달")
# sell.name, sell.quantity, sell.price, sell.sell_reason
```

- 숫자에서 "주", "원", 콤마 자동 제거
- 줄 수 부족하면 ValueError (사용자 친화적 에러 메시지 포함)

---

## 6. bot/keyboards.py 상수 & 함수

### 콜백 데이터 상수
```python
# 매수 확인
CONFIRM_BUY, EDIT_BUY, CANCEL_BUY

# 매도 확인
CONFIRM_SELL, CANCEL_SELL

# 회고 시작 여부
START_RETRO, SKIP_RETRO

# 투자 판단 평가
THESIS_CORRECT, THESIS_WRONG, THESIS_PARTIAL

# 아쉬움 회피 가능 여부
AVOIDABLE_YES, AVOIDABLE_NO, AVOIDABLE_UNKNOWN
```

### 키보드 빌더
```python
buy_confirm_keyboard()   → [확인 / 수정 / 취소]
sell_confirm_keyboard()  → [확인 / 취소]
retro_ask_keyboard()     → [회고 시작 / 나중에]
thesis_eval_keyboard()   → [맞았다 / 틀렸다 / 부분적으로]
avoidable_keyboard()     → [피할 수 있었다 / 통제 불가 / 모르겠다]
```

---

## 7. bot/formatters.py 함수

```python
format_number(n)          # 천 단위 콤마: 720000 → "720,000"
format_dashboard(holdings)  # 섹터별 대시보드 텍스트
format_buy_preview(...)   # 매수 확인 전 미리보기
format_buy_result(...)    # 매수 완료 메시지
format_sell_result(...)   # 매도 완료 + 수익률 메시지
```

---

## 8. 핸들러 패턴

### ConversationHandler 패턴 (buy.py, sell.py)
```python
from telegram.ext import ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, filters

def buy_conversation() -> ConversationHandler:
    # 상태 상수 정의
    WAITING_INPUT, WAITING_CONFIRM = range(2)

    return ConversationHandler(
        entry_points=[CommandHandler(["buy", "매수"], start_buy)],
        states={
            WAITING_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_input)],
            WAITING_CONFIRM: [CallbackQueryHandler(handle_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
```

### 일반 핸들러 패턴 (dashboard.py, help.py)
```python
async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # main.py에서 CommandHandler(["dashboard", "현황"], dashboard_handler)로 등록됨
```

---

## 9. main.py 핸들러 등록 순서

```python
# 1. 일반 CommandHandler
app.add_handler(CommandHandler(["start"], start))
app.add_handler(CommandHandler(["help", "도움말"], help_handler))
app.add_handler(CommandHandler(["dashboard", "현황"], dashboard_handler))

# 2. ConversationHandler (매수/매도)
app.add_handler(buy_conversation())
app.add_handler(sell_conversation())
```

---

## 10. 코딩 컨벤션

- **async/await**: 모든 핸들러 함수는 async
- **타입 힌트**: `from __future__ import annotations` 사용
- **한국어 메시지**: 사용자에게 보내는 모든 텍스트는 한국어
- **에러 처리**: 파싱 에러 시 사용자 친화적 메시지 + 재입력 요청
- **context.user_data**: ConversationHandler 내에서 임시 데이터 저장에 사용
- **직렬화**: 모든 모델은 `to_dict()` / `from_dict()` 패턴 사용

---

## 11. 수익률 계산 공식

```python
profit_loss = (sell_price - avg_price) * quantity
profit_loss_pct = (profit_loss / (avg_price * quantity)) * 100
```

---

## 12. 엣지 케이스

| 상황 | 처리 방법 |
|------|-----------|
| 추가 매수 | Holding.add_buy()로 평균단가 재계산 |
| 보유량 초과 매도 | Holding.remove_sell()에서 ValueError |
| 보유량 0 | 매도 후 quantity == 0이면 holdings에서 제거 |
| 미보유 종목 매도 | "보유하지 않은 종목입니다" 에러 |
| 텔레그램 4096자 초과 | 대시보드에서 자동 분할 전송 |
| 파싱 실패 | 에러 메시지 + 입력 예시 안내 |

---

## 13. 실행 방법

```bash
cd invest_log
pip install -r requirements.txt
cp .env.example .env
# .env에 BOT_TOKEN 설정
python main.py
```

---

## 14. 확장 시 참고

새로운 핸들러를 추가할 때:
1. `bot/handlers/` 에 파일 생성
2. `main.py`에 import + `app.add_handler()` 등록
3. 필요 시 `bot/keyboards.py`에 키보드 추가
4. 필요 시 `bot/formatters.py`에 포맷 함수 추가
5. 새 데이터 모델이 필요하면 `models/`에 추가하고 `storage/json_store.py`에 편의 함수 추가
