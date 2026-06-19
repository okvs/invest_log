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


# --- 프론트(정적 PWA) 서빙: /static 아래에 빌드 산출물을 둔다 ---
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC / "index.html"))
