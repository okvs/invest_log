"""YAML 시나리오 파일을 실제 Application으로 실행하는 테스트.

tests/scenarios/*.yaml 을 모두 읽어서 각 스텝을 봇에 주입하고 응답을 검증.
Bot 객체는 MagicMock으로 대체되어 실제 네트워크 호출 없이 돌아감.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import Application, ExtBot

from bot.handlers.broker import broker_conversation
from bot.handlers.buy import buy_conversation
from bot.handlers.edit import edit_conversation
from bot.handlers.sell import sell_conversation
from storage.json_store import save_holdings, save_ticker_map


SCENARIOS_DIR = Path(__file__).parent / "scenarios"


class ScenarioRunner:
    """YAML 시나리오를 실제 Application 에 주입하여 검증."""

    def __init__(self):
        self.responses: list[str] = []
        self.chat_id = 123456
        self.user_id = 789
        self._msg_id = 1000

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    # ─────────────────────────────────────────────
    # Bot 생성 (네트워크 호출 mock)
    # ─────────────────────────────────────────────

    def _make_bot(self) -> ExtBot:
        runner = self

        bot = MagicMock(spec=ExtBot)
        bot.token = "0:test"
        bot.base_url = "https://api.telegram.org/bot"
        bot.base_file_url = "https://api.telegram.org/file/bot"
        bot.defaults = None
        bot.rate_limiter = None
        bot.callback_data_cache = None
        bot.arbitrary_callback_data = False
        bot.local_mode = False
        bot.private_key = None
        bot.id = 1
        bot.first_name = "TestBot"
        bot.username = "testbot"
        bot.last_name = None
        bot._initialized = True

        async def send_message(chat_id, text, reply_markup=None, **kwargs):
            runner.responses.append(str(text))
            msg = MagicMock(spec=Message)
            msg.message_id = runner._next_id()
            msg.chat_id = chat_id
            msg.text = text
            return msg

        async def edit_message_text(text=None, chat_id=None, message_id=None,
                                    reply_markup=None, **kwargs):
            runner.responses.append(str(text))
            return True

        async def answer_callback_query(callback_query_id, **kwargs):
            return True

        async def initialize():
            pass

        async def shutdown():
            pass

        async def get_me():
            return User(id=1, first_name="TestBot", is_bot=True, username="testbot")

        bot.send_message = AsyncMock(side_effect=send_message)
        bot.edit_message_text = AsyncMock(side_effect=edit_message_text)
        bot.answer_callback_query = AsyncMock(side_effect=answer_callback_query)
        bot.initialize = AsyncMock(side_effect=initialize)
        bot.shutdown = AsyncMock(side_effect=shutdown)
        bot.get_me = AsyncMock(side_effect=get_me)
        bot.bot = User(id=1, first_name="TestBot", is_bot=True, username="testbot")
        return bot

    # ─────────────────────────────────────────────
    # Update 생성
    # ─────────────────────────────────────────────

    def _user(self) -> User:
        return User(id=self.user_id, first_name="Tester", is_bot=False)

    def _chat(self) -> Chat:
        return Chat(id=self.chat_id, type=Chat.PRIVATE)

    def _make_message_update(self, text: str, bot) -> Update:
        msg = Message(
            message_id=self._next_id(),
            date=datetime.now(),
            chat=self._chat(),
            from_user=self._user(),
            text=text,
        )
        msg.set_bot(bot)
        upd = Update(update_id=self._next_id(), message=msg)
        return upd

    def _make_callback_update(self, data: str, bot) -> Update:
        parent = Message(
            message_id=self._next_id(),
            date=datetime.now(),
            chat=self._chat(),
            from_user=User(id=1, first_name="TestBot", is_bot=True),
            text="(previous message)",
        )
        parent.set_bot(bot)
        cbq = CallbackQuery(
            id=f"cb{self._next_id()}",
            from_user=self._user(),
            chat_instance="test_instance",
            data=data,
            message=parent,
        )
        cbq.set_bot(bot)
        upd = Update(update_id=self._next_id(), callback_query=cbq)
        return upd

    # ─────────────────────────────────────────────
    # 시나리오 실행
    # ─────────────────────────────────────────────

    def _apply_setup(self, setup: dict) -> None:
        """setup: holdings, ticker_map 등 초기 데이터 세팅."""
        if "holdings" in setup:
            holdings = []
            for i, h in enumerate(setup["holdings"]):
                qty = h["quantity"]
                price = h["avg_price"]
                holdings.append({
                    "id": f"h_{i}",
                    "name": h["name"],
                    "ticker": h.get("ticker", ""),
                    "sector": h.get("sector", ""),
                    "buy_date": h.get("buy_date", "2026-01-01"),
                    "avg_price": price,
                    "quantity": qty,
                    "total_invested": qty * price,
                    "buy_thesis": h.get("buy_thesis", ""),
                    "research_notes": h.get("research_notes", ""),
                    "transaction_ids": [],
                })
            save_holdings(holdings)

        if "ticker_map" in setup:
            save_ticker_map(setup["ticker_map"])

    def _check_step(self, i: int, step: dict) -> None:
        joined = "\n---\n".join(self.responses)
        expect = step.get("expect")
        not_expect = step.get("not_expect")

        if expect is not None:
            items = expect if isinstance(expect, list) else [expect]
            if not any(item in joined for item in items):
                raise AssertionError(
                    f"\n[Step {i}] expect 실패\n"
                    f"기대(아래 중 하나라도 포함):\n  - "
                    + "\n  - ".join(items)
                    + f"\n실제 응답:\n{joined or '(응답 없음)'}"
                )

        if not_expect is not None:
            items = not_expect if isinstance(not_expect, list) else [not_expect]
            for item in items:
                if item in joined:
                    raise AssertionError(
                        f"\n[Step {i}] not_expect 실패\n"
                        f"포함되면 안 됨: {item}\n"
                        f"실제 응답:\n{joined}"
                    )

    async def run(self, scenario_path: Path) -> None:
        with open(scenario_path, encoding="utf-8") as f:
            scenario = yaml.safe_load(f)

        setup = scenario.get("setup") or {}
        self._apply_setup(setup)

        bot = self._make_bot()
        app = Application.builder().bot(bot).updater(None).build()
        app.add_handler(broker_conversation())
        app.add_handler(buy_conversation())
        app.add_handler(sell_conversation())
        app.add_handler(edit_conversation())

        await app.initialize()
        try:
            for i, step in enumerate(scenario.get("steps", []), 1):
                self.responses.clear()

                if "send" in step:
                    update = self._make_message_update(step["send"], bot)
                elif "click" in step:
                    update = self._make_callback_update(step["click"], bot)
                else:
                    raise ValueError(f"Step {i}: send 또는 click 필수")

                await app.process_update(update)
                self._check_step(i, step)
        finally:
            await app.shutdown()


# ──────────────────────────────────────────────────
# pytest 진입점 — scenarios/*.yaml 을 모두 발견
# ──────────────────────────────────────────────────

def _discover_scenarios() -> list[Path]:
    if not SCENARIOS_DIR.exists():
        return []
    return sorted(SCENARIOS_DIR.glob("*.yaml"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_path",
    _discover_scenarios(),
    ids=lambda p: p.stem,
)
async def test_scenario(scenario_path: Path) -> None:
    runner = ScenarioRunner()
    await runner.run(scenario_path)
