from telegram import Update
from telegram.ext import ContextTypes

HELP_TEXT = """<b>📊 명령어 안내</b>

<b>📈 조회 · 리포트</b>
  <code>현황</code> · <code>잔고</code> — 보유 종목 + 선물 + 현금 HTML 대시보드
  <code>자산그래프</code> — 기록 첫날부터 일별 NAV 추이 PNG
  <code>백테스트</code> — 과거 거래일 동결 시 오늘 순자산 비교 (PNG + HTML)
  <code>10억</code> — 순자산 10억 진척률·필요수익률(2·5년)·생존선 트래커
  <code>복기</code> — 보유 현물별 일봉 + 내 ▲매수/▼매도 마커 차트로 한 종목씩 되짚기
  <code>피라미딩</code> — 오늘 강한 돌파(전일대비 ≥4% + 신고가/갭상승)·수익 중 종목 = 1천만원 추가 검토
  <code>만기점검</code> — 만기 임박 선물 포지션 점검

<b>💰 현물 거래</b>
  <code>매수</code> — 종목·섹터·수량·단가·근거 입력 (닉네임 가능)
  <code>매도</code> — 종목 선택 후 수량·단가·사유
  <code>회고</code> — 미회고 매도 카드 선택해 복기
  <code>수정</code> — 보유 종목 평단·수량·섹터·사유 편집

<b>📊 선물 거래</b>
  <code>선물진입</code> — 종목·방향(롱/숏)·계약월·계약수·단가·증거금률·섹터·사유
  <code>선물청산</code> — 보유 포지션 선택 후 청산가·사유
  <code>선물롤오버</code> — 차월물로 자동 청산+재진입
  <code>선물회고</code> — 미회고 선물 청산 복기
  <code>선물시세</code> — 정확한 선물가 수동 입력 (6시간 유효)

<b>💵 현금 이벤트</b>
  <code>입금</code> — 날짜·금액·메모 입력 (예: <code>2026-04-15 5천만 월급</code>)
  <code>출금</code> — 동일 포맷
  <code>입출금목록</code> — 등록된 이벤트 리스트
  <code>입출금삭제 N</code> — N번 이벤트 삭제
  <code>예수금</code> — 초기자본/예수금 설정

<b>🏷 설정</b>
  <code>닉네임</code> — 종목 닉네임 관리 (<code>닉네임 삼전 : 삼성전자</code> 등록)
  <code>도움말</code> · <code>help</code> — 이 안내

<b>🤖 자동 기능</b>
  · KB증권 체결 메시지 자동 파싱 (현물·선물 양쪽)
  · 평일 15:30 KST 자동손절(-10%) 임박 알림 + 추세 이평선 깨짐 알림
  · 매일 08:00 KST 만기 임박(D-3 이하) 선물 포지션 푸시
  · 평일 08:00~20:00 KST 10분마다 현선물 괴리(선물%−현물%) ≥3%p 자동알림

<b>💡 입력 예시 — 매수</b>
  <code>삼성전자</code>
  <code>반도체</code>
  <code>10주</code>
  <code>72000원</code>
  <code>AI 수요 증가 전망</code>

<b>💡 입력 예시 — 매도 (종목 선택 후)</b>
  <code>5주</code>
  <code>85000원</code>
  <code>목표가 도달</code>
"""


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")
