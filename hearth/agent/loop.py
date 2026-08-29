from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from hearth.agent.prompts import SYSTEM_PROMPT
from hearth.agent.registry import ToolRegistry, registry
from hearth.config import settings
from hearth.runtime import runtime
from hearth import widgets as widget_bus

MAX_TURNS = 8


class AgentLoop:
    def __init__(self, tools: ToolRegistry | None = None) -> None:
        self.tools = tools or registry
        self.history: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.history = []

    async def run(self, user_text: str, *, confirm: bool = False) -> dict[str, Any]:
        runtime.agent_status = "thinking"
        runtime.note("user", user_text)
        widget_bus.start_turn(user_text)
        text = user_text.strip()
        try:
            if confirm and runtime.pending is not None:
                pending = runtime.pending
                args = dict(pending.args)
                args["confirm"] = True
                args["dry_run"] = False
                result = await self.tools.call(pending.tool, args)
                reply = _format_tool_reply([result.as_dict()])
                runtime.note("assistant", reply)
                runtime.agent_status = "idle"
                widget_bus.finish_turn(ok=True, detail="Confirmed.")
                return {
                    "reply": reply,
                    "mode": "confirm",
                    "tools": [result.as_dict()],
                    "widgets": runtime.list_widgets(),
                }

            if settings.openai_configured:
                try:
                    out = await self._run_openai(text)
                    runtime.agent_status = "idle"
                    widget_bus.finish_turn(ok=True, detail="Done.")
                    out["widgets"] = runtime.list_widgets()
                    return out
                except Exception as exc:  # noqa: BLE001
                    runtime.note("system", f"OpenAI path failed, using local router: {exc}", kind="status")

            out = await self._run_local(text)
            runtime.agent_status = "idle"
            widget_bus.finish_turn(ok=True, detail="Done.")
            out["widgets"] = runtime.list_widgets()
            return out
        except Exception:
            widget_bus.finish_turn(ok=False, detail="Failed.")
            raise

    async def iter_events(self, user_text: str, *, confirm: bool = False) -> AsyncIterator[dict[str, Any]]:
        """Yield protocol events while running a turn (used by the voice fallback)."""
        yield {"type": "status", "agent": "thinking"}
        result = await self.run(user_text, confirm=confirm)
        for tool in result.get("tools") or []:
            yield {"type": "tool.result", "name": tool.get("name"), "result": tool}
        yield {
            "type": "transcript.assistant",
            "text": result["reply"],
            "final": True,
        }
        yield {"type": "status", "agent": "idle"}

    async def _run_openai(self, user_text: str) -> dict[str, Any]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.history,
            {"role": "user", "content": user_text},
        ]
        used: list[dict[str, Any]] = []
        tools = self.tools.openai_chat_tools()

        for _ in range(MAX_TURNS):
            kwargs: dict[str, Any] = {
                "model": settings.openai_model,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            response = await client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            msg = choice.message
            if msg.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
                runtime.agent_status = "tool"
                for tc in msg.tool_calls:
                    args = _parse_args(tc.function.arguments)
                    if tc.function.name == "chief_of_staff":
                        args.setdefault("said", user_text)
                        args.setdefault("task", user_text)
                    result = await self.tools.call(tc.function.name, args)
                    used.append(result.as_dict())
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result.as_dict(), default=str),
                        }
                    )
                continue

            reply = (msg.content or "").strip() or "Done."
            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": reply})
            self.history = self.history[-24:]
            runtime.note("assistant", reply)
            return {"reply": reply, "mode": "openai", "tools": used}

        reply = "Stopped after too many tool turns."
        runtime.note("assistant", reply)
        return {"reply": reply, "mode": "openai", "tools": used}

    async def _run_local(self, user_text: str) -> dict[str, Any]:
        plan = route_intent(user_text)
        used: list[dict[str, Any]] = []
        if plan is None:
            reply = (
                "I can drive the house — lights, Denon, LG TV, grab movies in Radarr or shows in "
                "Sonarr, request via Overseerr, Plex now-playing, workspace, docker inspect. "
                "Repo, Gridways, Discord, calendar, and anything I can't do yet go to Chief of Staff."
            )
            runtime.note("assistant", reply)
            return {"reply": reply, "mode": "local", "tools": used}

        runtime.agent_status = "tool"
        result = await self.tools.call(plan["tool"], plan.get("args") or {})
        used.append(result.as_dict())
        reply = _format_tool_reply(used)
        runtime.note("assistant", reply)
        return {"reply": reply, "mode": "local", "tools": used}


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _format_tool_reply(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return "No tools ran."
    parts: list[str] = []
    for tool in tools:
        name = tool.get("name", "tool")
        if tool.get("needs_confirm"):
            preview = tool.get("data", {}).get("would_call_with", {})
            spoken = _confirm_line(name, preview)
            parts.append(spoken)
            continue
        if not tool.get("ok"):
            data = tool.get("data") or {}
            if name == "chief_of_staff" and data.get("error"):
                parts.append(str(data["error"]))
                continue
            parts.append(f"{name} failed: {data}")
            continue
        data = tool.get("data") or {}
        pretty = _pretty_tool(name, data)
        parts.append(pretty if pretty else f"{name}: {json.dumps(data, default=str)[:1200]}")
    return "\n".join(parts)


def _pretty_tool(name: str, data: dict[str, Any]) -> str | None:
    mock = " (mock)" if data.get("mode") == "mock" else ""
    if name == "plex_now_playing":
        sessions = data.get("sessions") or []
        if not sessions:
            return f"Nothing playing on Plex{mock}."
        lines = []
        for session in sessions:
            title = session.get("title") or "Untitled"
            player = session.get("player") or "a player"
            state = session.get("state") or "idle"
            lines.append(f"{title} on {player} — {state}{mock}.")
        return " ".join(lines)
    if name == "ha_list_entities":
        states = data.get("states") or []
        if not states:
            return f"No matching HA entities{mock}."
        bits = []
        for row in states[:12]:
            label = (row.get("attributes") or {}).get("friendly_name") or row.get("entity_id")
            bits.append(f"{label}: {row.get('state')}")
        return f"House{mock}: " + "; ".join(bits)
    if name == "docker_ps":
        containers = data.get("containers") or []
        names = [c.get("name") or c.get("id") for c in containers]
        return f"Containers{mock}: " + ", ".join(str(n) for n in names)
    if name == "ha_call_service":
        entity = (data.get("entity") or {}).get("entity_id") or data.get("entity")
        return f"Done{mock}: {entity} is {(data.get('entity') or {}).get('state', 'updated')}."
    if name == "chief_of_staff":
        if data.get("configured") is False:
            return str(data.get("error") or "Chief of Staff is not configured.")
        repo = data.get("repo") or (data.get("payload") or {}).get("repo")
        if data.get("escalated"):
            return f"Escalated to Chief of Staff for {repo}."
        if data.get("would_send") or data.get("payload"):
            target = repo or "the repo"
            return f"I'll ask Chief of Staff to handle that for {target}."
        return None
    if name == "radarr_search":
        titles = ", ".join(
            f"{r.get('title')} ({r.get('year')})" for r in (data.get("results") or [])[:4] if r.get("title")
        )
        return f"Radarr{mock} found: {titles or 'nothing'}."
    if name == "radarr_add":
        added = data.get("added") or {}
        return f"I'll grab {added.get('title') or 'that'} in Radarr{mock}."
    if name == "sonarr_search":
        titles = ", ".join(
            f"{r.get('title')} ({r.get('year')})" for r in (data.get("results") or [])[:4] if r.get("title")
        )
        return f"Sonarr{mock} found: {titles or 'nothing'}."
    if name == "sonarr_add":
        added = data.get("added") or {}
        return f"I'll grab {added.get('title') or 'that'} in Sonarr{mock}."
    if name == "overseerr_search":
        titles = ", ".join(
            f"{r.get('title')} ({r.get('year')})" for r in (data.get("results") or [])[:4] if r.get("title")
        )
        return f"Overseerr{mock} found: {titles or 'nothing'}."
    if name == "overseerr_request":
        item = data.get("requested") or {}
        return f"I'll request {item.get('title') or 'that'} in Overseerr{mock}."
    if name == "get_weather":
        place = data.get("place") or "Home"
        temp = data.get("temperature")
        unit = data.get("temperature_unit") or "°C"
        condition = data.get("condition") or "unknown"
        if temp is None:
            return f"Weather{mock} at {place}: {condition}."
        return f"{place}{mock}: {temp}{unit}, {condition}."
    return None


def _confirm_line(name: str, preview: dict[str, Any]) -> str:
    if name == "chief_of_staff":
        task = preview.get("task") or preview.get("said") or "that"
        return f"I'll ask Chief of Staff to handle that: {task}. Confirm to send."
    if name == "radarr_add":
        return f"I'll grab {preview.get('query') or 'that'} in Radarr. Confirm to add."
    if name == "sonarr_add":
        return f"I'll grab {preview.get('query') or 'that'} in Sonarr. Confirm to add."
    if name == "overseerr_request":
        return f"I'll request {preview.get('query') or 'that'} in Overseerr. Confirm to send."
    return f"{name} is waiting for confirm. Preview: {preview}"


_PLAYING = re.compile(r"\b(now playing|what'?s (on|playing)|what is playing|now-playing)\b", re.I)
_PLEX_ONLY = re.compile(r"\bplex\b", re.I)
_DOCKER = re.compile(r"\b(docker|containers?)\b", re.I)
_LIGHTS = re.compile(r"\b(lights?|scenes?|rooms?|home assistant|denon|webos|tv)\b", re.I)
_WORKSPACE = re.compile(r"\b(workspace|skills?)\b", re.I)
_WEATHER = re.compile(
    r"\b(weather|forecast|temperature|how hot|how cold|is it raining|is it snowing)\b",
    re.I,
)
_TURN_ON = re.compile(r"\bturn on\s+(.+)$", re.I)
_TURN_OFF = re.compile(r"\bturn off\s+(.+)$", re.I)
_INSPECT = re.compile(r"\binspect\s+(\S+)", re.I)
_MOVIE = re.compile(r"\b(movie|film|radarr)\b", re.I)
_SERIES = re.compile(r"\b(show|series|season|episode|sonarr)\b", re.I)
_OVERSEERR = re.compile(r"\b(overseerr|request)\b", re.I)
_GRAB = re.compile(
    r"\b(download|grab|snatch|request|get me)\b"
    r"|\badd .{0,80}\b(to|in) (radarr|sonarr|overseerr|the library|the queue)\b",
    re.I,
)
_CONNECT = re.compile(r"\bconnect(?: me)? to\s+(.+)", re.I)
_HOUSE_CONNECT = re.compile(r"\b(denon|avr|tv|lg|plex|home assistant|\bha\b|light)\b", re.I)
_COS = re.compile(
    r"("
    r"\bgithub\b|\bgitlab\b|"
    r"\bpull requests?\b|\bmerge requests?\b|"
    r"\bopen a pr\b|\bcreate a pr\b|\bmake a pr\b|\bopen a pull request\b|"
    r"\bthe repo\b|\bthis repo\b|\bhearth repo\b|"
    r"\bto the repo\b|\bin the repo\b|"
    r"\bchange the repo\b|\bupdate the repo\b|\bedit the repo\b|"
    r"\bfix (this|it) in hearth\b|"
    r"\badd a feature\b|"
    r"\bdeploy to git\b|\bpush to git\b|\bgit push\b|"
    r"\bcommit (this|that|the|to)\b|"
    r"\bdiscord\b|\bslack\b|\btelegram\b|"
    r"\bgridways\b|\bkanban\b|\bproject board\b|\bopen tasks\b|\btasks on project\b|"
    r"\bcalendar\b|\bschedule a meeting\b|"
    r"\bteammate agents?\b|\bother agents?\b"
    r")",
    re.I,
)


def route_intent(text: str) -> dict[str, Any] | None:
    """Tiny local router so the runtime is useful before an API key is set."""
    raw = text.strip()
    if not raw:
        return None

    if _COS.search(raw):
        return {
            "tool": "chief_of_staff",
            "args": {"task": raw, "said": raw, "repo": "RubenVroman/Hearth"},
        }
    connect = _CONNECT.search(raw)
    if connect and not _HOUSE_CONNECT.search(connect.group(1)):
        return {
            "tool": "chief_of_staff",
            "args": {"task": raw, "said": raw, "repo": "RubenVroman/Hearth"},
        }
    if _GRAB.search(raw):
        query = _media_query(raw)
        if _OVERSEERR.search(raw):
            return {"tool": "overseerr_request", "args": {"query": query or raw}}
        if _SERIES.search(raw) and not _MOVIE.search(raw):
            return {"tool": "sonarr_add", "args": {"query": query or raw}}
        if _MOVIE.search(raw):
            return {"tool": "radarr_add", "args": {"query": query or raw}}
        return {"tool": "overseerr_request", "args": {"query": query or raw}}
    m = _TURN_ON.search(raw)
    if m:
        entity = _guess_entity(m.group(1), on=True)
        return {"tool": "ha_call_service", "args": entity}
    m = _TURN_OFF.search(raw)
    if m:
        entity = _guess_entity(m.group(1), on=False)
        return {"tool": "ha_call_service", "args": entity}
    m = _INSPECT.search(raw)
    if m:
        return {"tool": "docker_inspect", "args": {"container": m.group(1)}}
    if _PLAYING.search(raw) or (_PLEX_ONLY.search(raw) and not _GRAB.search(raw)):
        return {"tool": "plex_now_playing", "args": {}}
    if _WEATHER.search(raw):
        return {"tool": "get_weather", "args": {}}
    if _DOCKER.search(raw):
        return {"tool": "docker_ps", "args": {}}
    if _WORKSPACE.search(raw):
        return {"tool": "workspace_list", "args": {}}
    if _LIGHTS.search(raw):
        return {"tool": "ha_list_entities", "args": {}}
    return None


def _guess_entity(phrase: str, *, on: bool) -> dict[str, Any]:
    name = phrase.strip().lower().rstrip(".")
    mapping = {
        "living room": "light.living_room",
        "living room lights": "light.living_room",
        "kitchen": "light.kitchen",
        "kitchen lights": "light.kitchen",
        "office": "light.office",
        "movie night": "scene.movie_night",
        "good night": "scene.good_night",
        "tv": "media_player.lg_webos_tv",
        "lg": "media_player.lg_webos_tv",
        "denon": "media_player.denon_avr_x3700h",
        "avr": "media_player.denon_avr_x3700h",
    }
    entity = mapping.get(name, f"light.{name.replace(' ', '_')}")
    domain = entity.split(".", 1)[0]
    if domain == "scene":
        service = "turn_on"
    elif domain == "media_player":
        service = "turn_on" if on else "turn_off"
    else:
        service = "turn_on" if on else "turn_off"
    return {"domain": domain, "service": service, "entity_id": entity}


_MEDIA_NOISE = re.compile(
    r"\b(please|can you|could you|download|grab|snatch|request|get me|add|"
    r"the movie|the film|the show|the series|a movie|a show|"
    r"to radarr|in radarr|to sonarr|in sonarr|on overseerr|via overseerr|"
    r"to the library|to the queue|for me)\b",
    re.I,
)


def _media_query(text: str) -> str:
    cleaned = _MEDIA_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip(" .?!")


# Re-export for tests / app
__all__ = ["AgentLoop", "SYSTEM_PROMPT", "route_intent"]
