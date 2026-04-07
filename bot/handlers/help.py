from telegram import Update
from telegram.ext import ContextTypes

HELP_TEXT = """사용법 안내

/매수 — 매수 기록
  종목명, 섹터, 수량, 매수가, 매수 근거를 입력합니다.
  종목코드는 자동으로 조회됩니다.
  추가 매수 시 평균단가가 자동 계산됩니다.
  닉네임으로도 입력 가능합니다. (예: 삼전 → 삼성전자)
  영어 종목명은 대소문자를 구분하지 않습니다.

/매도 — 매도 기록 + 회고
  보유 종목 목록에서 종목을 선택한 후
  수량, 매도가, 매도 사유를 입력합니다.
  매도 후 회고를 통해 투자 판단을 복기할 수 있습니다.

/현황 — 투자 현황 대시보드
  섹터별 보유 종목, 투자금, 매수 근거를 한눈에 봅니다.

/닉네임 — 종목 닉네임 관리
  닉네임 삼전 : 삼성전자  → 등록
  닉네임 삼전 : 삭제      → 삭제
  닉네임                  → 목록 보기

/도움말 — 이 도움말 표시

입력 예시 (매수):
  삼성전자
  반도체
  10주
  72000원
  AI 수요 증가 전망

입력 예시 (매도 — 종목 선택 후):
  5주
  85000원
  목표가 도달
"""


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)
