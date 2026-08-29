from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hearth import __version__
from hearth.agent.loop import AgentLoop
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.runtime import runtime
from hearth.tools.builtin import register_builtin_tools
from hearth.tools.arr import overseerr, radarr, sonarr
from hearth.tools.docker import docker
from hearth.tools.ha import ha
from hearth.tools.plex import plex
from hearth.voice.gateway import voice_socket

UI_DIR = Path(__file__).parent / "ui" / "static"
_agent = AgentLoop()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.workspace_path.mkdir(parents=True, exist_ok=True)
    register_builtin_tools()
    yield
    await ha.aclose()
    await plex.aclose()
    await docker.aclose()
    await radarr.aclose()
    await sonarr.aclose()
    await overseerr.aclose()


app = FastAPI(
    title="Hearth",
    version=__version__,
    description="House agent runtime for VAULT",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def hearth_token_gate(request: Request, call_next):
    if not settings.token:
        return await call_next(request)
    if request.url.path in {"/health"}:
        return await call_next(request)
    header = request.headers.get("x-hearth-token", "")
    query = request.query_params.get("token", "")
    cookie = request.cookies.get("hearth_token", "")
    if settings.token not in {header, query, cookie}:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class ChatBody(BaseModel):
    message: str = Field(min_length=1)
    confirm: bool = False


class InvokeBody(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "name": "hearth", "version": __version__, "house": settings.house_name}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    ha_ping = await ha.ping()
    return {
        **runtime.snapshot(),
        "house": settings.house_name,
        "owner": settings.owner,
        "version": __version__,
        "openai": settings.openai_configured,
        "ha": ha_ping,
        "plex": {"configured": settings.plex_configured},
        "docker": {"socket": docker.live},
        "tools": registry.names(),
        "workspace": str(settings.workspace_path.resolve()),
    }


@app.get("/api/now-playing")
async def now_playing() -> dict[str, Any]:
    return await plex.now_playing()


@app.get("/api/rooms")
async def rooms() -> dict[str, Any]:
    lights = await ha.list_states("light")
    scenes = await ha.list_states("scene")
    media = await ha.list_states("media_player")
    return {
        "lights": lights.get("states") or [],
        "scenes": scenes.get("states") or [],
        "media": media.get("states") or [],
        "mode": lights.get("mode"),
    }


@app.get("/api/transcript")
async def transcript() -> dict[str, Any]:
    return {
        "lines": [
            {"role": line.role, "text": line.text, "ts": line.ts, "kind": line.kind}
            for line in runtime.transcript
        ]
    }


@app.get("/api/tools")
async def tools() -> dict[str, Any]:
    return {"tools": registry.list_public()}


@app.post("/api/chat")
async def chat(body: ChatBody) -> dict[str, Any]:
    runtime.agent_status = "thinking"
    try:
        return await _agent.run(body.message, confirm=body.confirm)
    finally:
        runtime.agent_status = "idle"


@app.post("/api/invoke")
async def invoke(body: InvokeBody) -> dict[str, Any]:
    if registry.get(body.tool) is None:
        raise HTTPException(status_code=404, detail="unknown tool")
    result = await registry.call(body.tool, body.args)
    return result.as_dict()


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    if settings.token:
        header = websocket.headers.get("x-hearth-token", "")
        query = websocket.query_params.get("token", "")
        if settings.token not in {header, query}:
            await websocket.close(code=4401)
            return
    await voice_socket(websocket)


if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")
