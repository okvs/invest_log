"""invest_log PWA 백엔드 (FastAPI).

PWA-only 전환 1단계: 텔레그램 없이 PWA → 이 API 로 매수/매도/회고 '쓰기' +
상태 조회. 맥에서 실행하고 Cloudflare 터널로 노출한다.

  uvicorn server.app:app --host 127.0.0.1 --port 8787

인증: 기본 '공개 모드' — 토큰 없이 /api/* 쓰기 가능(server.auth.require_user 가
환경변수 WEBAPP_AUTH 가 켜졌을 때만 서명 토큰을 검증). 비밀번호 단계에서
WEBAPP_AUTH=1 로 켜면 /api/login 흐름이 그대로 살아난다.
프론트(server/static)는 항상 인증 없이 서빙한다.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import auth, service
from server.auth import require_user

app = FastAPI(title="invest_log PWA API")


@app.on_event("startup")
async def _watch_source_changes() -> None:
    """1분마다 소스 변경 확인 — 바뀌었으면 SIGTERM 으로 곱게 종료.

    invest-web 은 tmux while-loop 래퍼 안에서 돌므로, 종료하면 래퍼가 새 코드로
    재기동한다(dash-refresh/kakao-apply 의 자가재기동과 같은 목적). uvicorn 은
    SIGTERM 을 받으면 진행 중 요청을 마친 뒤 내려간다."""
    import asyncio
    import logging
    import signal

    from bot.self_restart import arm, source_changed

    arm()

    async def _loop() -> None:
        while True:
            await asyncio.sleep(60)
            if source_changed():
                logging.getLogger("uvicorn.error").warning(
                    "소스 변경 감지 — 서버 종료(래퍼 while-loop 가 새 코드로 재기동)")
                signal.raise_signal(signal.SIGTERM)
                return

    app.state.source_watch = asyncio.get_running_loop().create_task(_loop())

# 프론트는 Firebase Hosting(web.app)에서 서빙되고 API는 맥 터널을 호출하므로
# 교차출처(CORS) 허용이 필요하다. 출처는 안정적인 Firebase 도메인 + 로컬 개발만 허용.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://invest-log-caf3d.web.app",
        "https://invest-log-caf3d.firebaseapp.com",
        "http://localhost:8787",
        "http://127.0.0.1:8787",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

_STATIC = Path(__file__).resolve().parent / "static"


class BuyReq(BaseModel):
    name: str
    quantity: int
    price: float
    sector: str = ""
    thesis: str = ""
    ticker: str = ""
    margin_ratio: int = 100
    research_notes: str = ""


class SellReq(BaseModel):
    name: str
    quantity: int
    price: float
    reason: str = ""


class RetroReq(BaseModel):
    transaction_id: str
    thesis_correct: bool | None = None
    what_went_well: str = ""
    regrets: str = ""
    avoidable: str = ""
    lessons: str = ""


class SectorReq(BaseModel):
    sector: str
    ticker: str = ""
    name: str = ""


class PensionReq(BaseModel):
    transaction_id: str


class PushSubReq(BaseModel):
    subscription: dict


class LoginReq(BaseModel):
    password: str


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "auth_required": auth.auth_enabled(),
        "password_set": auth.is_password_set(),
    }


@app.post("/api/login")
async def login(req: LoginReq) -> dict:
    try:
        token = auth.login(req.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"ok": True, "token": token}


@app.get("/api/state")
async def state(uid: str = Depends(require_user)) -> dict:
    return service.get_state()


@app.post("/api/buy")
async def buy(req: BuyReq, uid: str = Depends(require_user)) -> dict:
    try:
        tx = service.record_buy(
            req.name, req.quantity, req.price,
            sector=req.sector, thesis=req.thesis, ticker=req.ticker,
            margin_ratio=req.margin_ratio, research_notes=req.research_notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "transaction": tx}


@app.post("/api/sell")
async def sell(req: SellReq, uid: str = Depends(require_user)) -> dict:
    try:
        tx = service.record_sell(req.name, req.quantity, req.price, reason=req.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "transaction": tx}


@app.post("/api/retro")
async def retro(req: RetroReq, uid: str = Depends(require_user)) -> dict:
    try:
        r = service.record_retro(
            req.transaction_id, thesis_correct=req.thesis_correct,
            what_went_well=req.what_went_well, regrets=req.regrets,
            avoidable=req.avoidable, lessons=req.lessons,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "retrospective": r}


@app.post("/api/sector")
async def sector_set(req: SectorReq, uid: str = Depends(require_user)) -> dict:
    try:
        h = service.record_sector(req.sector, ticker=req.ticker, name=req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "holding": h}


@app.post("/api/pension")
async def pension_toggle(req: PensionReq, uid: str = Depends(require_user)) -> dict:
    try:
        tx = service.toggle_pension(req.transaction_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # holding_remains: 토글 후에도 그 종목이 보유에 남아 있는지 — 프론트가 이 값으로
    # '확인 필요' 탭의 섹터 입력 카드를 즉시 숨길지 판단한다(연금 처리로 보유가 사라진 경우).
    return {
        "ok": True, "transaction": tx,
        "holding_remains": service.holding_active(tx.get("name", "")),
    }


# --- 웹 푸시 알림 ---
@app.get("/api/push/key")
async def push_key(uid: str = Depends(require_user)) -> dict:
    from bot import push_service
    return {"key": push_service.public_key()}


@app.post("/api/push/subscribe")
async def push_subscribe(req: PushSubReq, uid: str = Depends(require_user)) -> dict:
    from bot import push_service
    try:
        push_service.add_subscription(req.subscription)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(uid: str = Depends(require_user)) -> dict:
    from bot import push_service
    n = push_service.send_push("테스트 알림", "푸시 알림이 정상 동작합니다 ✅")
    return {"ok": True, "sent": n}


# --- 프론트(정적 PWA) 서빙: /static 아래에 빌드 산출물을 둔다 ---
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC / "index.html"))
