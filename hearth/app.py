from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
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
from hearth.memory.prune import prune_loop
from hearth.memory.retrieve import search as memory_search
from hearth.memory.retrieve import status_snapshot as memory_status_snapshot
from hearth.memory.store import export_snapshot, init_memory_db, remember_preference
from hearth.memory.store import forget as memory_forget_row
from hearth.memory.tools import register_memory_tools
from hearth.openai_usage import spend_monitor
from hearth.runtime import runtime
from hearth.telegram import telegram_inbox
from hearth.tools.arr import overseerr, radarr, sonarr
from hearth.tools.builtin import register_builtin_tools
from hearth.tools.docker import docker
from hearth.tools.ha import ha
from hearth.tools.media import house_media_inventory
from hearth.tools.plex import plex
from hearth.tools.thuisbezorgd import thuisbezorgd
from hearth.tools.websearch import selected_backend as web_search_backend
from hearth.voice import webrtc as realtime_rtc
from hearth.voice.gateway import voice_socket

UI_DIR = Path(__file__).parent / "ui" / "static"
_agent = AgentLoop()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import asyncio

    settings.workspace_path.mkdir(parents=True, exist_ok=True)
    init_db()
    bootstrap_admin()
    init_memory_db()
    register_builtin_tools()
    register_memory_tools()
    stop_prune = asyncio.Event()
    prune_task = asyncio.create_task(prune_loop(stop_prune))
    await telegram_inbox.start()
    yield
    stop_prune.set()
    prune_task.cancel()
    try:
        await prune_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await telegram_inbox.stop()
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
        "openai_admin": settings.openai_admin_configured,
        "realtime": {
            "path": "webrtc-ga",
            "model": settings.openai_realtime_model,
            "beta": False,
            "calls": "/api/realtime/calls",
            "client_secrets": "/api/realtime/client_secrets",
        },
        "ha": {
            **ha.diagnostics(),
            **ha_ping,
            "tv_entity": settings.ha_tv_entity,
            "avr_entity": settings.ha_avr_entity,
            "apple_tv_entity": settings.ha_apple_tv_entity,
            "apple_tv_player": settings.apple_tv_player,
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
        "web_search": {
            "configured": settings.web_search_live,
            "backend": web_search_backend(),
        },
        "telegram": telegram_inbox.status_snapshot(),
        "docker": {"socket": docker.live},
        "tools": registry.names(),
        "workspace": str(settings.workspace_path.resolve()),
        "memory": memory_status_snapshot(),
    }


@app.get("/api/openai/spend")
async def openai_spend(days: int = Query(default=30, ge=1, le=180)) -> dict[str, Any]:
    """OpenAI org costs/usage (admin key) + local measured-token ledger + list pricing.

    Never invents billed amounts. Keys stay on the server.
    """
    return await spend_monitor(days=days)


@app.get("/api/openai/costs")
async def openai_costs(days: int = Query(default=30, ge=1, le=180)) -> dict[str, Any]:
    from hearth.openai_usage import fetch_organization_costs

    return await fetch_organization_costs(days=days)


@app.get("/api/openai/usage")
async def openai_usage(days: int = Query(default=30, ge=1, le=180)) -> dict[str, Any]:
    from hearth.openai_usage import fetch_organization_completions_usage

    return await fetch_organization_completions_usage(days=days)


@app.get("/api/openai/pricing")
async def openai_pricing() -> dict[str, Any]:
    from hearth.openai_usage import official_list_pricing

    return official_list_pricing()


@app.get("/api/now-playing")
async def now_playing() -> dict[str, Any]:
    return await plex.now_playing()


@app.get("/api/plex/genres")
async def plex_genres(type: str = Query(default="movie")) -> dict[str, Any]:
    """List genres for the Plex movie or show library. Token stays server-side."""
    return await plex.genres(type)


@app.get("/api/plex/library")
async def plex_library_by_genre(
    genre: str = Query(default=""),
    type: str = Query(default="movie"),
    limit: int = Query(default=24, ge=1, le=50),
) -> dict[str, Any]:
    """Browse Plex library by genre (speakable). Empty genre lists available genres."""
    return await plex.browse_genre(genre, media_type=type, limit=limit)


@app.get("/api/media")
async def media_inventory() -> dict[str, Any]:
    """TV + AVR + Apple TV (HA) + Plex — speakable house media snapshot."""
    return await house_media_inventory()


@app.get("/api/network")
async def network_inventory(limit: int = Query(default=250, ge=1, le=1000)) -> dict[str, Any]:
    """All HA-represented network entities plus reachability and key media links."""
    return await ha.network_inventory(limit=limit)


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
        "entities": {
            "tv": settings.ha_tv_entity,
            "avr": settings.ha_avr_entity,
            "apple_tv": settings.ha_apple_tv_entity,
        },
    }


@app.get("/api/transcript")
async def transcript() -> dict[str, Any]:
    return {
        "lines": [
            {"role": line.role, "text": line.text, "ts": line.ts, "kind": line.kind}
            for line in runtime.transcript
        ]
    }


@app.get("/api/widgets")
async def widgets() -> dict[str, Any]:
    return {"widgets": runtime.list_widgets()}


def _poster_placeholder_svg(title: str) -> Response:
    initials = "".join(part[0] for part in title.split()[:2] if part).upper() or "H"
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600" viewBox="0 0 400 600">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#3a1608"/>
      <stop offset="55%" stop-color="#c45a12"/>
      <stop offset="100%" stop-color="#ffd7a1"/>
    </linearGradient>
  </defs>
  <rect width="400" height="600" fill="url(#g)"/>
  <text x="200" y="280" text-anchor="middle" font-family="Georgia, serif" font-size="72" fill="#f4ead8" opacity="0.92">{initials}</text>
  <text x="200" y="520" text-anchor="middle" font-family="Georgia, serif" font-size="22" fill="#f4ead8" opacity="0.75">{safe_title[:28]}</text>
</svg>"""
    return Response(
        content=svg.encode("utf-8"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/api/media/art")
async def media_art(
    ratingKey: str | None = Query(default=None, alias="ratingKey"),
    tmdbId: int | None = Query(default=None, alias="tmdbId"),
    mediaType: str = Query(default="movie", alias="mediaType"),
    posterPath: str | None = Query(default=None, alias="posterPath"),
    title: str | None = Query(default=None),
) -> Response:
    """Poster/backdrop proxy — Plex / *arr / Overseerr / TMDB CDN; keys stay server-side."""
    from hearth.tools.media_art import fetch_poster_bytes, fixture_title_for_art

    fetched = await fetch_poster_bytes(
        rating_key=ratingKey,
        tmdb_id=tmdbId,
        media_type=mediaType or "movie",
        poster_path=posterPath,
    )
    if fetched is not None:
        body, content_type = fetched
        return Response(
            content=body,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )
    label = (title or "").strip() or fixture_title_for_art(
        rating_key=ratingKey,
        tmdb_id=tmdbId,
    )
    return _poster_placeholder_svg(label)


class SuggestBody(BaseModel):
    titles: list[str] | None = None
    query: str | None = None
    type: str = "any"
    limit: int = Field(default=4, ge=1, le=6)


@app.post("/api/media/suggest")
async def media_suggest(body: SuggestBody) -> dict[str, Any]:
    """Resolve recommended titles into overlay-ready metadata (keys stay server-side)."""
    from hearth.tools.suggest import suggest_titles
    from hearth import widgets as widget_bus

    payload = await suggest_titles(
        {
            "titles": body.titles,
            "query": body.query,
            "type": body.type,
            "limit": body.limit,
        }
    )
    # Publish the same media glass overlay chat/tools use.
    widget_bus.publish_tool(
        {
            "name": "suggest_titles",
            "ok": bool(payload.get("ok")),
            "needs_confirm": False,
            "dry_run": False,
            "data": payload,
        }
    )
    return {**payload, "widgets": runtime.list_widgets()}


@app.get("/api/plex/thumb/{rating_key}")
async def plex_thumb(rating_key: str) -> Response:
    """Poster proxy — Plex token never leaves the server. Prefers real art via /api/media/art."""
    key = str(rating_key or "").strip()
    if not key or not key.isdigit():
        raise HTTPException(status_code=400, detail="invalid ratingKey")
    return await media_art(ratingKey=key, tmdbId=None, mediaType="movie", posterPath=None, title=None)


@app.delete("/api/widgets/{widget_id}")
async def dismiss_widget(widget_id: str) -> dict[str, Any]:
    ok = runtime.dismiss_widget(widget_id)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown widget")
    return {"ok": True, "id": widget_id, "widgets": runtime.list_widgets()}


@app.delete("/api/widgets")
async def clear_widgets() -> dict[str, Any]:
    removed = runtime.clear_widgets(dismissible_only=True)
    return {"ok": True, "removed": removed, "widgets": runtime.list_widgets()}


@app.get("/api/tools")
async def tools() -> dict[str, Any]:
    return {"tools": registry.list_public()}


@app.post("/api/chat")
async def chat(body: ChatBody) -> dict[str, Any]:
    runtime.set_status("thinking")
    try:
        out = await _agent.run(body.message, confirm=body.confirm)
        out.setdefault("widgets", runtime.list_widgets())
        return out
    except Exception:  # noqa: BLE001
        runtime.flash_error("Request failed")
        raise
    finally:
        runtime.set_status("idle")


@app.post("/api/invoke")
async def invoke(body: InvokeBody) -> dict[str, Any]:
    if registry.get(body.tool) is None:
        raise HTTPException(status_code=404, detail="unknown tool")
    result = await registry.call(body.tool, body.args)
    return {**result.as_dict(), "widgets": runtime.list_widgets()}


class MemoryRememberBody(BaseModel):
    key: str = ""
    value: str = ""
    text: str = ""
    category: str = "general"


class MemoryForgetBody(BaseModel):
    key: str = ""
    id: str = ""
    confirm: bool = False


class MemoryPurgeBody(BaseModel):
    kind: str = "all"
    confirm: bool = False


@app.get("/api/memory")
async def memory_get() -> dict[str, Any]:
    snap = memory_status_snapshot()
    from hearth.memory.store import list_preferences, recent_house_events

    snap["preferences"] = list_preferences(limit=40)
    snap["house_events"] = recent_house_events(limit=12)
    return snap


@app.get("/api/memory/search")
async def memory_search_api(q: str = Query(default="", min_length=0)) -> dict[str, Any]:
    hits = await memory_search(q) if q.strip() else []
    return {"query": q, "hits": hits}


@app.post("/api/memory/remember")
async def memory_remember_api(body: MemoryRememberBody) -> dict[str, Any]:
    value = (body.value or body.text).strip()
    if not value:
        raise HTTPException(status_code=400, detail="value required")
    row = remember_preference(body.key or value, value, category=body.category or "general")
    return row


@app.post("/api/memory/forget")
async def memory_forget_api(body: MemoryForgetBody) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true required to forget. Memory deletes default to dry-run.",
        )
    result = memory_forget_row(pref_id=body.id, key=body.key)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "not found")
    return result


@app.post("/api/memory/export")
async def memory_export_api(confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true required to export. Memory export is gated like other high-risk tools.",
        )
    return export_snapshot()


@app.post("/api/memory/purge")
async def memory_purge_api(body: MemoryPurgeBody) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true required to purge.",
        )
    from hearth.memory.store import purge

    kind = (body.kind or "all").lower()
    return purge(
        conversations=kind in {"all", "conversations", "session", "sessions"},
        house_events=kind in {"all", "events", "house", "house_events"},
        preferences=kind in {"all", "preferences", "prefs"},
    )


@app.post("/api/realtime/client_secrets")


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
        "widgets": runtime.list_widgets(),
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
