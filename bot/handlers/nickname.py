"""닉네임 등록/삭제/목록 핸들러.

사용법:
  /nickname 삼전 : 삼성전자   → 닉네임 등록
  /nickname 삼전 : 삭제       → 닉네임 삭제
  /nickname                  → 등록된 닉네임 목록
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from storage.json_store import load_nickname_map, save_nickname_map


async def nickname_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """닉네임 등록/삭제/목록 처리."""
    text = (update.message.text or "").strip()

    # 명령어 부분 제거 (/nickname, 닉네임)
    for prefix in ("/nickname", "닉네임"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    nmap = load_nickname_map()

    # 인자 없으면 목록 표시
    if not text:
        if not nmap:
            await update.message.reply_text("등록된 닉네임이 없습니다.")
            return
        lines = [f"  {nick} → {real}" for nick, real in nmap.items()]
        await update.message.reply_text("등록된 닉네임:\n" + "\n".join(lines))
        return

    # "닉네임 : 종목명" 파싱
    if ":" not in text:
        await update.message.reply_text(
            "형식: 닉네임 : 종목명\n"
            "예시: 삼전 : 삼성전자\n"
            "삭제: 삼전 : 삭제"
        )
        return

    nick, real = text.split(":", 1)
    nick = nick.strip()
    real = real.strip()

    if not nick or not real:
        await update.message.reply_text("닉네임과 종목명을 모두 입력해주세요.")
        return

    # 삭제
    if real == "삭제":
        # 대소문자 무시하고 삭제
        nick_lower = nick.lower()
        found_key = None
        for k in nmap:
            if k.lower() == nick_lower:
                found_key = k
                break
        if found_key:
            del nmap[found_key]
            save_nickname_map(nmap)
            await update.message.reply_text(f"닉네임 '{found_key}' 삭제 완료.")
        else:
            await update.message.reply_text(f"'{nick}' 닉네임이 존재하지 않습니다.")
        return

    # 등록/수정
    nmap[nick] = real
    save_nickname_map(nmap)
    await update.message.reply_text(f"닉네임 등록: {nick} → {real}")
