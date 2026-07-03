"""kakao_apply poll_once 멱등성 — crash 후 재기동해도 같은 체결이 이중 반영되지 않는다.

poll_once 의 외부 의존(kakaocli/DB/텔레그램/푸시/재발행)은 전부 모킹하고,
멱등 장부(_processed)·메시지별 state 저장·데드레터(_failed)만 검증한다.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import kakao_apply as ka  # noqa: E402
import kakao_to_telegram as kt  # noqa: E402
import kakao_trade_preview as kp  # noqa: E402

import bot.push_service as push_service  # noqa: E402

CID = 123
KS = str(CID)


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """poll_once 의 외부 의존 모킹 + tmp state 파일. rows/applied/sent 를 조작·관찰."""
    env = {"rows": [], "applied": [], "sent": []}

    monkeypatch.setattr(ka, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(kp, "find_kakaocli", lambda: "kakaocli")
    monkeypatch.setattr(kp, "load_auth", lambda: ("db", "key"))
    monkeypatch.setattr(kp, "kc_query", lambda *a, **k: env["rows"])
    monkeypatch.setattr(kp, "detail_text", lambda m, a: (m, "2026-07-03 10:00:00"))
    monkeypatch.setattr(kt, "resolve_targets", lambda *a, **k: {"KB증권": CID})
    monkeypatch.setattr(kt, "current_max", lambda *a, **k: 0)
    monkeypatch.setattr(kt, "tg_send", lambda tok, cid, text: env["sent"].append(text))
    monkeypatch.setattr(ka, "_republish_dashboard", lambda: None)
    monkeypatch.setattr(push_service, "send_push", lambda *a, **k: 0)
    monkeypatch.setattr(push_service, "notify_pending_inputs_if_new", lambda: None)

    def fake_apply(detail, **kw):
        if "BOOM" in detail:
            raise RuntimeError("boom")
        env["applied"].append(detail)
        return ka.ApplyResult(True, "주식매수", f"테스트 {detail}", "")

    monkeypatch.setattr(ka, "apply_message", fake_apply)
    monkeypatch.setattr(ka.time, "sleep", lambda s: None)
    return env


def _state() -> dict:
    with open(ka.STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def test_processed_ledger_blocks_reapply(harness):
    """watermark 가 유실돼 같은 배치를 다시 읽어도(crash 시나리오) 장부가 재반영을 막는다."""
    ka.save_state({KS: 0})
    harness["rows"] = [(10, 0, "체결A", None)]
    ka.poll_once(None, None)
    assert harness["applied"] == ["체결A"]
    assert _state()["_processed"][KS] == [10]
    assert _state()[KS] == 10

    # crash 시뮬레이션: watermark 만 반영 전으로 되돌리고(장부는 유지) 같은 배치 재폴링
    st = _state()
    st[KS] = 0
    ka.save_state(st)
    ka.poll_once(None, None)
    assert harness["applied"] == ["체결A"]  # 두 번째 반영 없음 — 멱등


def test_state_saved_per_message_and_dead_letter(harness):
    """메시지별 즉시 저장 + 예외 건은 데드레터로 격리하고 뒷 건은 계속 처리."""
    ka.save_state({KS: 0})
    harness["rows"] = [(10, 0, "체결A", None), (11, 0, "BOOM", None), (12, 0, "체결B", None)]
    ka.poll_once("tok", 1)

    assert harness["applied"] == ["체결A", "체결B"]  # BOOM 이 배치를 못 막음
    st = _state()
    assert st["_processed"][KS] == [10, 12]
    assert st["_failed"][KS] == [11]
    assert st[KS] == 12
    assert any("자동반영 실패" in m and "logId=11" in m for m in harness["sent"])


def test_crash_mid_batch_then_rerun_skips_applied(harness):
    """2번째 메시지에서 프로세스가 죽어도(예외 전파) 1번째는 장부에 남아 재실행 시 스킵."""
    ka.save_state({KS: 0})

    def crash_detail(m, a):
        if m == "CRASH":
            raise KeyboardInterrupt  # Exception 가드 밖 — 진짜 크래시처럼 전파
        return (m, "2026-07-03 10:00:00")

    import kakao_trade_preview as kp2
    kp2.detail_text = crash_detail  # harness 의 monkeypatch 위에 덮어씀
    harness["rows"] = [(10, 0, "체결A", None), (11, 0, "CRASH", None)]
    with pytest.raises(KeyboardInterrupt):
        ka.poll_once(None, None)

    assert harness["applied"] == ["체결A"]
    assert _state()["_processed"][KS] == [10]  # 죽기 전에 이미 저장됨

    # 재기동: 같은 배치 재폴링 — 체결A 스킵, CRASH(이제 정상) 반영
    kp2.detail_text = lambda m, a: (m, "2026-07-03 10:00:00")
    ka.poll_once(None, None)
    assert harness["applied"] == ["체결A", "CRASH"]
    assert sorted(_state()["_processed"][KS]) == [10, 11]
