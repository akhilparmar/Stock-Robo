"""Standalone FastAPI app for the OI-spurts stock option paper tester.

Run alone:

    python -m oi_spurts
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from oi_spurts.engine import engine
from oi_spurts.neo import NeoError, neo
from oi_spurts.runtime import shutdown, startup

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    yield
    shutdown()


app = FastAPI(title="OI Spurts Paper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="oi_spurts_static")


class ConnectBody(BaseModel):
    totp: str = Field(min_length=4, max_length=10)
    mpin: str = Field(min_length=4, max_length=10)


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    return engine.status()


@app.post("/api/refresh")
def refresh() -> dict[str, Any]:
    return engine.refresh()


@app.post("/api/connect")
def connect(body: ConnectBody) -> dict[str, Any]:
    try:
        result = neo.connect(body.totp.strip(), body.mpin.strip())
    except NeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "neo": neo.status()}


@app.post("/api/disconnect")
def disconnect() -> dict[str, Any]:
    neo.disconnect()
    return {"ok": True, "neo": neo.status()}


@app.post("/api/start")
def start() -> dict[str, Any]:
    return engine.start()


@app.post("/api/stop")
def stop() -> dict[str, Any]:
    return engine.stop()


@app.post("/api/paper-today")
def paper_today() -> dict[str, Any]:
    """Flatten today's paper trades if any, then rebuy with the live 09:20 strategy."""
    return engine.paper_today()
