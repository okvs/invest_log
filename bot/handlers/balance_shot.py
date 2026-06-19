"""잔고 스크린샷 → 융자 자동반영 트리거 (사진 메시지 핸들러).

증권사 앱 '잔고' 화면을 텔레그램으로 보내면:
  1) 사진을 data/balance_shots/<req_id>.jpg 로 저장
  2) 요청 메타(req_id, image_path, chat_id, ...)를 같은 폴더 <req_id>.json 으로 기록
  3) invest_log Claude 세션에 `/balance-apply <req_id>` 주입(cmux)
  4) 사용자에게 접수 ack

이후는 invest_log 워크스페이스의 Claude 가 `balance-apply` 스킬로 처리한다:
이미지를 비전으로 읽어 종목별 융자금액을 파싱 → scripts/balance_apply.py 로
credit_loan 반영 → 결과를 텔레그램으로 회신. (카톡 체결엔 증거금이 안 나와서,
스샷으로 종목별 융자를 실측 반영하는 용도.)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot.claude_inject import inject_command

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SHOTS_DIR = PROJECT_ROOT / "data" / "balance_shots"
SKILL_COMMAND = "/balance-apply"


async def balance_shot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or not msg.photo:
        return

    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    req_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    img_path = SHOTS_DIR / f"{req_id}.jpg"

    try:
        tg_file = await msg.photo[-1].get_file()  # 최대 해상도
        await tg_file.download_to_drive(str(img_path))
    except Exception:  # noqa: BLE001
        logger.warning("잔고 스샷 다운로드 실패", exc_info=True)
        await msg.reply_text("⚠️ 사진을 받지 못했어요. 다시 보내주세요.")
        return

    payload = {
        "req_id": req_id,
        "image_path": str(img_path),
        "chat_id": msg.chat_id,
        "message_id": msg.message_id,
        "caption": (msg.caption or "").strip(),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    (SHOTS_DIR / f"{req_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ok, info = inject_command(f"{SKILL_COMMAND} {req_id}")
    logger.info("balance shot req=%s inject ok=%s info=%s", req_id, ok, info)

    if ok:
        await msg.reply_text(
            "📸 잔고 스샷 받았어요. 종목별 융자금액을 반영하는 중이에요…\n"
            "잠시 후 결과를 알려드릴게요."
        )
    else:
        await msg.reply_text(
            "📸 스샷은 저장했는데 자동반영 트리거에 실패했어요.\n"
            f"({info})\n"
            f"req_id={req_id} — invest_log Claude 세션이 떠 있는지 확인해주세요."
        )
