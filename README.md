# invest_log

텔레그램 기반 한국 주식 투자 기록 봇. 매수/매도 기록, 포트폴리오 현황, 매도 회고까지 텔레그램 채팅으로 관리합니다.

## 주요 기능

| 명령어 | 설명 |
|--------|------|
| **매수** | 종목명 입력 → 네이버 금융에서 종목코드 자동 검색 → 후보 선택 → 저장 |
| **매도** | 보유 종목 선택 → 매도 정보 입력 → 수익률 계산 → 매도 회고 |
| **수정** | 보유 종목 선택 → 현재 정보 표시 → 수정 내용 입력 → 저장 |
| **현황** | 섹터별 대시보드 + 현재가 기반 수익률 + HTML 리포트 |
| **닉네임** | 종목 닉네임 등록/삭제 (삼전 → 삼성전자) |
| **도움말** | 사용법 안내 |

### 매수 플로우

```
매수 →

삼성전자
반도체
10주
72000원
AI 수요 증가 전망

→ 종목 검색 중...
→ [삼성전자 (005930.KS)] [삼성전자우 (005935.KS)] ← 선택
→ 매수 기록 완료! 삼성전자 [005930.KS] (반도체) 10주 x 72,000원
```

- 부분 검색 지원: "삼성" → 삼성전자, 삼성전기, 삼성SDI 중 선택
- 닉네임 지원: "삼전" → 삼성전자로 자동 변환
- 추가 매수 시 평균단가 자동 재계산

### 매도 회고

매도 후 5단계 회고를 통해 투자 판단을 기록합니다:

1. 투자 판단이 맞았는가? (맞았다 / 틀렸다 / 부분적으로)
2. 잘한 점
3. 아쉬운 점
4. 피할 수 있었는가?
5. 교훈

### 현황 대시보드

- 섹터별 보유 종목 + 평가금액
- yfinance 기반 실시간 현재가 조회
- 종목코드 없는 종목은 자동으로 네이버에서 검색하여 보정
- HTML 리포트 파일 첨부 (브라우저에서 상세 확인)

## 기술 스택

- **Python 3.10+**
- **python-telegram-bot** 21.0+ (async)
- **Playwright** - 네이버 금융 종목코드 자동 검색
- **yfinance** - 실시간 현재가 조회
- **matplotlib** - 섹터별 비중 차트
- **filelock** - JSON 파일 동시 접근 보호

## 설치 및 실행

```bash
# 1. 의존성 설치
pip install -r requirements.txt
playwright install chromium

# 2. 환경변수 설정
cp .env.example .env
# .env 파일에 텔레그램 봇 토큰 입력
# BOT_TOKEN=your-telegram-bot-token-here

# 3. 실행
python main.py
```

## 프로젝트 구조

```
invest_log/
├── main.py                  # 봇 진입점
├── bot/
│   ├── handlers/
│   │   ├── buy.py           # 매수 (종목 검색 + 선택)
│   │   ├── sell.py          # 매도 + 회고
│   │   ├── edit.py          # 보유 종목 수정
│   │   ├── dashboard.py     # 현황 대시보드
│   │   ├── nickname.py      # 닉네임 관리
│   │   └── help.py          # 도움말
│   ├── keyboards.py         # 인라인 키보드 정의
│   ├── formatters.py        # 텍스트 포맷 + 현재가 조회
│   └── html_report.py       # HTML 리포트 생성
├── models/
│   ├── portfolio.py         # Holding (보유 종목)
│   ├── transaction.py       # Transaction (매수/매도 거래)
│   └── retrospective.py     # Retrospective (매도 회고)
├── parsers/
│   └── input_parser.py      # 한국어 입력 파싱 + 네이버 종목 검색
├── storage/
│   └── json_store.py        # JSON 파일 저장 (filelock)
├── tests/                   # pytest 유닛 테스트
└── data/                    # 데이터 파일 (gitignore)
    ├── portfolio.json       # 보유 종목
    ├── transactions.json    # 거래 내역
    ├── ticker_map.json      # 종목명 → 종목코드 캐시
    └── retrospectives.json  # 회고 기록
```

## 데이터 저장

모든 데이터는 `data/` 디렉토리에 JSON 파일로 저장됩니다. 별도 DB 없이 파일 기반으로 동작하며, filelock으로 동시 접근을 보호합니다.

## 테스트

```bash
python -m pytest tests/ -v
```
