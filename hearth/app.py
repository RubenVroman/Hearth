from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hearth import __version__
from hearth.agent.loop import AgentLoop
from hearth.agent.registry import registry
from hearth.auth.bootstrap import bootstrap_admin
from hearth.auth.db import init_db
from hearth.auth.gate import http_authorized, is_public_path, ws_authorized
from hearth.auth.routers import auth_router
from hearth.config import settings
from hearth.runtime import runtime
from hearth.tools.builtin import register_builtin_tools
from hearth.tools.arr import overseerr, radarr, sonarr
from hearth.tools.docker import docker
from hearth.tools.ha import ha
from hearth.tools.media import house_media_inventory
from hearth.tools.plex import plex
from hearth.tools.thuisbezorgd import thuisbezorgd
from hearth.voice.gateway import voice_socket
from hearth.voice import webrtc as realtime_rtc

UI_DIR = Path(__file__).parent / "ui" / "static"
_agent = AgentLoop()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.workspace_path.mkdir(parents=True, exist_ok=True)
    init_db()
    bootstrap_admin()
    register_builtin_tools()
    yield
    await ha.aclose()
    await plex.aclose()
    await docker.aclose()
    await radarr.aclose()
    await sonarr.aclose()
    await overseerr.aclose()
    await thuisbezorgd.aclose()


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

app.include_router(auth_router)


@app.middleware("http")
async def house_auth_gate(request: Request, call_next):
    if is_public_path(request.url.path):
        return await call_next(request)
    if http_authorized(request):
        return await call_next(request)
    if request.url.path == "/" and request.method in {"GET", "HEAD"}:
        return RedirectResponse("/login", status_code=302)
    return JSONResponse({"error": "unauthorized"}, status_code=401)


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
        "realtime": {
            "path": "webrtc-ga",
            "model": settings.openai_realtime_model,
            "beta": False,
            "calls": "/api/realtime/calls",
            "client_secrets": "/api/realtime/client_secrets",
        },
        "ha": {
            **ha_ping,
            "tv_entity": settings.ha_tv_entity,
            "avr_entity": settings.ha_avr_entity,
        },
        "plex": {"configured": settings.plex_configured},
        "radarr": {"configured": settings.radarr_configured},
        "sonarr": {"configured": settings.sonarr_configured},
        "overseerr": {"configured": settings.overseerr_configured},
        "thuisbezorgd": {
            "configured": settings.thuisbezorgd_configured,
            "live_submit_ready": thuisbezorgd.live_submit_ready,
            "delivery_address": settings.delivery_address_configured,
        },
        "docker": {"socket": docker.live},
        "tools": registry.names(),
        "workspace": str(settings.workspace_path.resolve()),
    }


@app.get("/api/now-playing")
async def now_playing() -> dict[str, Any]:
    return await plex.now_playing()


@app.get("/api/media")
async def media_inventory() -> dict[str, Any]:
    """TV + AVR (HA) + Plex — speakable house media snapshot for the agent/UI."""
    return await house_media_inventory()


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
        "entities": {"tv": settings.ha_tv_entity, "avr": settings.ha_avr_entity},
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


@app.post("/api/realtime/client_secrets")
async def realtime_client_secrets() -> JSONResponse:
    """Mint an ephemeral ek_ token (GA). Never returns the long-lived API key."""
    result = await realtime_rtc.mint_client_secret()
    status = 200 if result.get("ok") else (503 if not result.get("configured") else 502)
    return JSONResponse(result, status_code=status)


@app.post("/api/realtime/calls")
async def realtime_calls(request: Request) -> Response:
    """Unified GA WebRTC: browser SDP in, OpenAI SDP out. Tools run on a sideband."""
    sdp = (await request.body()).decode("utf-8", errors="replace")
    if not sdp.strip():
        return JSONResponse({"error": "empty sdp", "path": "webrtc-ga"}, status_code=400)
    result = await realtime_rtc.create_call(sdp)
    if not result.get("ok"):
        status = 503 if not result.get("configured") else 502
        return JSONResponse(result, status_code=status)
    return Response(
        content=result["sdp"],
        media_type="application/sdp",
        headers={
            "X-Hearth-Realtime-Path": "webrtc-ga",
            "X-Hearth-Realtime-Model": settings.openai_realtime_model,
            "X-Hearth-Call-Id": str(result.get("call_id") or ""),
            "X-Hearth-Sideband": str(result.get("sideband") or ""),
            "X-Hearth-Realtime-Beta": "false",
        },
    )


@app.post("/api/realtime/calls/{call_id}/hangup")
async def realtime_hangup(call_id: str) -> dict[str, Any]:
    await realtime_rtc.hangup(call_id)
    return {"ok": True, "path": "webrtc-ga", "call_id": call_id}


class RealtimeToolBody(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
    said: str = ""


@app.post("/api/realtime/tools")
async def realtime_tools(body: RealtimeToolBody) -> dict[str, Any]:
    """House tools stay on Hearth. Browser only relays function_call events."""
    args = dict(body.arguments)
    result = await realtime_rtc.run_house_tool(body.name, args, said=body.said)
    return {
        "ok": result.get("ok", False),
        "path": "webrtc-ga",
        "call_id": body.call_id,
        "output": result,
    }


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    if not ws_authorized(websocket):
        await websocket.close(code=4401)
        return
    await voice_socket(websocket)


if UI_DIR.exists():
    @app.get("/manifest.webmanifest")
    async def web_manifest() -> FileResponse:
        return FileResponse(
            UI_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(
            UI_DIR / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/apple-touch-icon.png")
    async def apple_touch_icon() -> FileResponse:
        return FileResponse(UI_DIR / "icons" / "apple-touch-icon.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(UI_DIR / "login.html")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")
