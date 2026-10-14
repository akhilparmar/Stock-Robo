"""Standalone FastAPI app for the Nifty index option tester.

Mounted at /nifty by the stock app, or run alone:

    python -m index_tester
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from index_tester.engine import engine
from index_tester.neo import NeoError, neo
from index_tester.runtime import shutdown_index_tester, startup_index_tester

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup_index_tester()
    yield
    shutdown_index_tester()


app = FastAPI(title="Nifty Index Option Tester", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="nifty_static")


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
