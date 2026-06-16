"""invest_log PWA 백엔드 (FastAPI).

PWA-only 전환 1단계: 텔레그램 없이 PWA → 이 API 로 매수/매도/회고 '쓰기' +
상태 조회. 맥에서 실행하고 Cloudflare 터널로 노출한다.

  uvicorn server.app:app --host 127.0.0.1 --port 8787

인증: 모든 /api/* 는 Firebase ID 토큰 필요(server.auth.require_user).
프론트(server/static)는 인증 없이 서빙(로그인 화면 포함).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import service
from server.auth import require_user

app = FastAPI(title="invest_log PWA API")

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


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


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


# --- 프론트(정적 PWA) 서빙: /static 아래에 빌드 산출물을 둔다 ---
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC / "index.html"))
