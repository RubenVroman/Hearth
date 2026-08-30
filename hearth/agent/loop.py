from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from hearth.agent.prompts import SYSTEM_PROMPT, compose_system_prompt_async
from hearth.agent.registry import ToolRegistry, registry
from hearth.config import settings
from hearth.memory import store as memory_store
from hearth.memory.summarize import maybe_summarize
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
                out = {
                    "reply": reply,
                    "mode": "confirm",
                    "tools": [result.as_dict()],
                }
                await _after_turn(text or pending.tool, out, channel="chat")
                runtime.agent_status = "idle"
                widget_bus.finish_turn(ok=True, detail="Confirmed.")
                out["widgets"] = runtime.list_widgets()
                return out

            if settings.openai_configured:
                try:
                    out = await self._run_openai(text)
                    await _after_turn(text, out, channel="chat")
                    runtime.agent_status = "idle"
                    widget_bus.finish_turn(ok=True, detail="Done.")
                    out["widgets"] = runtime.list_widgets()
                    return out
                except Exception as exc:  # noqa: BLE001
                    runtime.note("system", f"OpenAI path failed, using local router: {exc}", kind="status")

            out = await self._run_local(text)
            await _after_turn(text, out, channel="chat")
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
        system = await compose_system_prompt_async(
            user_text,
            include_recent_turns=not bool(self.history),
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
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
                "I can drive the house — lights, Denon, LG TV, play titles in Infuse on the "
                "Apple TV (or Plex on LG), grab movies in Radarr or shows in Sonarr, check "
                "download progress, request "
                "via Overseerr, order food on Thuisbezorgd, live web search, Plex now-playing, "
                "workspace, docker inspect. Repo, Gridways, Discord, calendar, and anything I "
                "can't do yet go to Chief of Staff."
            )
            runtime.note("assistant", reply)
            return {"reply": reply, "mode": "local", "tools": used}

        runtime.agent_status = "tool"
        result = await self.tools.call(plan["tool"], plan.get("args") or {})
        used.append(result.as_dict())
        reply = _format_tool_reply(used)
        runtime.note("assistant", reply)
        return {"reply": reply, "mode": "local", "tools": used}


async def _after_turn(user_text: str, out: dict[str, Any], *, channel: str) -> None:
    """Persist the turn and maybe roll a session summary. Never raise into the house loop."""
    try:
        session_id = memory_store.ensure_session(channel)
        if user_text.strip():
            memory_store.persist_turn("user", user_text, session_id=session_id, channel=channel)
        reply = str(out.get("reply") or "")
        if reply:
            memory_store.persist_turn("assistant", reply, session_id=session_id, channel=channel)
        if session_id:
            await maybe_summarize(session_id)
    except Exception:  # noqa: BLE001
        return


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
            data = tool.get("data") or {}
            if data.get("speak"):
                parts.append(str(data["speak"]))
                continue
            preview = data.get("would_call_with", {})
            spoken = _confirm_line(
                name,
                preview,
                data=data,
                plan=data.get("plan") if isinstance(data.get("plan"), dict) else None,
            )
            parts.append(spoken)
            continue
        if not tool.get("ok"):
            data = tool.get("data") or {}
            if name == "chief_of_staff" and data.get("error"):
                parts.append(str(data["error"]))
                continue
            if data.get("speak"):
                parts.append(str(data["speak"]))
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
    if name == "plex_search":
        results = data.get("results") or []
        if not results:
            return f"Nothing in the Plex library{mock}."
        titles = ", ".join(
            f"{r.get('title')} ({r.get('year') or r.get('type')})"
            for r in results[:4]
            if r.get("title")
        )
        return f"Plex{mock} found: {titles or 'nothing'}."
    if name == "plex_clients":
        clients = data.get("clients") or []
        if not clients:
            return f"No Plex clients online{mock}."
        names = ", ".join(str(c.get("name") or "unknown") for c in clients[:6])
        return f"Plex clients{mock}: {names}."
    if name == "plex_browse_genre":
        spoken = data.get("speak")
        if spoken:
            return spoken if mock == "" else f"{spoken.rstrip('.')}" + mock + "."
        if data.get("listed_genres"):
            genres = data.get("genres") or []
            names = ", ".join(str(g.get("title") or "") for g in genres[:8] if g.get("title"))
            return f"Plex genres{mock}: {names or 'none'}."
        results = data.get("results") or []
        genre = data.get("genre") or "that genre"
        if not results:
            return f"No {genre} titles in the Plex library{mock}."
        titles = ", ".join(
            f"{r.get('title')} ({r.get('year')})" if r.get("year") else str(r.get("title"))
            for r in results[:4]
            if r.get("title")
        )
        total = data.get("total") or len(results)
        return f"{total} {genre} in Plex{mock}: {titles}."
    if name == "plex_play":
        spoken = data.get("speak")
        if spoken:
            return spoken if mock == "" else f"{spoken.rstrip('.')}" + mock + "."
        if data.get("in_library") is False:
            return str(data.get("error") or "That title is not in the Plex library.")
        item = data.get("item") or {}
        client = data.get("client") or {}
        return (
            f"Playing {item.get('title') or 'that'} on "
            f"{client.get('name') or 'the TV'}{mock}."
        )
    if name == "infuse_play":
        spoken = data.get("speak")
        if spoken:
            return spoken if mock == "" else f"{spoken.rstrip('.')}" + mock + "."
        if data.get("needs_setup"):
            return str(data.get("speak") or data.get("error") or "Apple TV / Infuse needs setup.")
        item = data.get("item") or {}
        return f"Opening {item.get('title') or 'that'} in Infuse on the Apple TV{mock}."
    if name == "infuse_transport":
        spoken = data.get("speak")
        if spoken:
            return spoken if mock == "" else f"{spoken.rstrip('.')}" + mock + "."
        return f"Apple TV transport{mock}: {data.get('action') or 'done'}."
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
    if name == "house_media":
        return str(data.get("speak") or f"House media{mock}.")
    if name == "ha_media_control":
        spoken = data.get("speak")
        if spoken:
            return f"Done{mock}: {spoken}"
        return f"Done{mock}: {data.get('device')} {data.get('action')} on {data.get('entity_id')}."
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
    if name in {"radarr_queue", "sonarr_queue"}:
        spoken = data.get("speak")
        if spoken:
            return str(spoken)
        label = "Radarr" if name == "radarr_queue" else "Sonarr"
        return f"No {label} queue update{mock}."
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
    if name == "thuisbezorgd_restaurants":
        return str(data.get("speak") or f"Thuisbezorgd restaurants{mock}.")
    if name == "thuisbezorgd_menu":
        return str(data.get("speak") or f"Menu{mock}.")
    if name == "thuisbezorgd_cart":
        return str(data.get("speak") or f"Cart{mock}.")
    if name == "thuisbezorgd_auth_status":
        addr = (data.get("delivery_address") or {}).get("line") or "address not set"
        mode = data.get("mode") or "mock"
        return f"Thuisbezorgd is {mode}; delivery {addr}."
    if name == "thuisbezorgd_order":
        return str(data.get("speak") or f"Order placed{mock}.")
    if name == "get_weather":
        place = data.get("place") or "Home"
        temp = data.get("temperature")
        unit = data.get("temperature_unit") or "°C"
        condition = data.get("condition") or "unknown"
        if temp is None:
            return f"Weather{mock} at {place}: {condition}."
        return f"{place}{mock}: {temp}{unit}, {condition}."
    if name == "web_search":
        spoken = data.get("speak")
        if spoken:
            return spoken if mock == "" else f"{spoken.rstrip('.')}" + mock + "."
        results = data.get("results") or []
        if not results:
            return f"No live web results{mock}."
        titles = ", ".join(str(r.get("title") or r.get("source") or "result") for r in results[:4])
        return f"Web{mock} found: {titles}."
    if name == "memory_remember":
        if data.get("ok"):
            return f"I'll remember {data.get('key')}: {data.get('value')}"
        return f"Couldn't remember that: {data.get('error')}"
    if name == "memory_forget":
        forgotten = data.get("forgotten") or {}
        if data.get("ok"):
            return f"Forgotten {forgotten.get('key') or 'that'}."
        return f"Nothing to forget: {data.get('error')}"
    if name == "memory_list":
        items = data.get("items") or []
        if not items:
            return "I don't have anything stored for that yet."
        if data.get("kind") == "house_events":
            titles = "; ".join(str(item.get("title") or "") for item in items[:8])
            return f"House history: {titles}"
        bits = "; ".join(f"{item.get('key')}: {item.get('value')}" for item in items[:8])
        return f"I remember: {bits}"
    if name == "memory_search":
        hits = data.get("hits") or []
        if not hits:
            return "Nothing in memory matched that."
        return "From memory: " + "; ".join(str(hit.get("text") or "") for hit in hits[:5])
    if name == "memory_export":
        return f"Exported memory to {data.get('path')} ({data.get('counts')})."
    if name == "memory_purge":
        return f"Purged house memory: {data.get('purged') or data}."
    return None


def _confirm_line(
    name: str,
    preview: dict[str, Any],
    data: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    data = data or {}
    if plan is None and isinstance(data.get("plan"), dict):
        plan = data.get("plan")
    if name == "thuisbezorgd_order":
        summary = data.get("summary")
        if summary:
            return f"Order ready: {summary}. Confirm to place — this spends money."
        restaurant = (data.get("restaurant") or {}).get("name") or "the restaurant"
        total = data.get("total") or "?"
        address = data.get("delivery_address") or "the house address"
        return f"I'll order from {restaurant} for {total} to {address}. Confirm to place."
    if name == "memory_forget":
        target = preview.get("key") or preview.get("id") or "that"
        return f"I'll forget {target}. Confirm to delete it."
    if name == "memory_export":
        return "I'll export a redacted memory snapshot into the workspace. Confirm to write it."
    if name == "memory_purge":
        return f"I'll purge house memory ({preview.get('kind') or 'all'}). Confirm to delete it."
    if name == "workspace_delete":
        path = preview.get("path") or "that file"
        return f"I'll delete {path} from the workspace. Confirm to remove it."
    if name == "docker_stop":
        container = preview.get("container") or "that container"
        return f"I'll stop Docker container {container}. Confirm to stop it."
    if name == "plex_play":
        if plan and plan.get("speak"):
            return str(plan["speak"])
        query = preview.get("query") or "that"
        player = preview.get("player") or "the TV"
        return f"I'll play {query} on {player}. Confirm to start."
    if name == "infuse_play":
        if plan and plan.get("speak"):
            return str(plan["speak"])
        query = preview.get("query") or "that"
        return f"I'll open {query} in Infuse on the Apple TV. Confirm to launch."
    if name == "infuse_transport":
        action = preview.get("action") or "control"
        return f"I'll {action} on the Apple TV. Confirm to run."
    return f"{name} is waiting for confirm. Preview: {preview}"


_PLAYING = re.compile(r"\b(now playing|what'?s (on|playing)|what is playing|now-playing)\b", re.I)
_PLAY_ON_TV = re.compile(
    r"\bplay\s+(.+?)\s+on\s+(?:the\s+)?("
    r"infuse|firecore|"
    r"apple\s*tv|lg(?:\s*webos)?(?:\s*tv)?|webos|"
    r"living\s*room(?:\s*tv)?|shield|plex|"
    r"tv|television"
    r")\b",
    re.I,
)
_PLAY_TITLE = re.compile(
    r"\b(?:play|put on)\s+(.+?)(?:\s+please)?$",
    re.I,
)
_PUT_ON_INFUSE = re.compile(
    r"\b(?:put|play)\s+(.+?)\s+(?:on|in)\s+(?:the\s+)?infuse\b",
    re.I,
)
_INFUSE_TRANSPORT = re.compile(
    r"\b(pause|stop|skip(?:\s+(?:ahead|forward))?|next(?:\s+track)?|"
    r"resume|unpause|play|go\s+back|previous(?:\s+track)?)\b"
    r".*\b(?:apple\s*tv|infuse|atv)\b"
    r"|\b(?:apple\s*tv|infuse|atv)\b.*"
    r"\b(pause|stop|skip|next|resume|unpause|go\s+back|previous)\b",
    re.I,
)
_MEDIA_STATUS = re.compile(
    r"\b(house media|media status|media inventory|what'?s on the (tv|avr|denon|apple\s*tv)|"
    r"is the (tv|avr|denon|apple\s*tv) on|avr status|tv status)\b",
    re.I,
)
_PLEX_CLIENTS = re.compile(
    r"\b(plex clients|which (plex )?(players?|clients?)|list (plex )?(players?|clients?))\b",
    re.I,
)
_PLEX_GENRES_LIST = re.compile(
    r"\b(?:list|show|what(?:'s| are)|which)\s+(?:the\s+|our\s+|plex\s+)?(?:movie\s+|film\s+|tv\s+|show\s+)?genres?\b"
    r"|\bgenres?\s+(?:in|on)\s+(?:the\s+)?(?:plex\s+)?(?:library|movies?)\b",
    re.I,
)
_PLEX_GENRE_BROWSE = re.compile(
    r"\b(?:"
    r"(?:list|show(?:\s+me)?|browse|what(?:'s| are)|which|any|have we got|do we have|"
    r"got any|are there)\s+"
    r"(?:all\s+)?(?:the\s+|our\s+|my\s+)?"
    r"(?P<genre>[a-z][\w &'\-]{0,40}?)\s+"
    r"(?P<kind>movies?|films?|shows?|series)\b"
    r"|"
    r"(?P<genre2>animation|anime|comedy|horror|action|drama|thriller|romance|"
    r"documentary|sci[\-\s]?fi|scifi|science fiction|fantasy|family|kids?|crime|adventure|"
    r"mystery|western|war|music|musical|sport|sports|history|biography|biopic)\s+"
    r"(?P<kind2>movies?|films?|shows?|series)\b"
    r"|"
    r"(?:movies?|films?|shows?|series)\s+(?:in|by|from)\s+(?:the\s+)?"
    r"(?P<genre3>[a-z][\w &'\-]{0,40}?)\s+genre\b"
    r")",
    re.I,
)
_PLEX_ONLY = re.compile(r"\bplex\b", re.I)
_DOCKER = re.compile(r"\b(docker|containers?)\b", re.I)
_LIGHTS = re.compile(r"\b(lights?|scenes?|rooms?|home assistant)\b", re.I)
_WORKSPACE = re.compile(r"\b(workspace|skills?)\b", re.I)
_WEATHER = re.compile(
    r"\b(weather|forecast|temperature|how hot|how cold|is it raining|is it snowing)\b",
    re.I,
)
_WEB_SEARCH = re.compile(
    r"\b("
    r"search the web|web search|look(?: it)? up online|search online|"
    r"google(?:\s+for)?|"
    r"where (?:can i|to) watch|where(?:'s| is) .+ streaming|"
    r"what(?:'s| is) streaming|just\s*watch|"
    r"latest news|current events|news about|what(?:'s| is) in the news|"
    r"who won|score of"
    r")\b",
    re.I,
)
_ABOUT_MEDIA = re.compile(
    r"\b(?:"
    r"tell me about|what(?:'s| is| about)|"
    r"info(?:rmation)? (?:on|about)|"
    r"look up|search (?:plex |the library )?(?:for )?"
    r")\b",
    re.I,
)
_ABOUT_MEDIA_TITLE = re.compile(
    r"\b(?:"
    r"tell me about|what(?:'s| is)(?: the)?(?: movie| film| show)?|"
    r"info(?:rmation)? (?:on|about)|"
    r"look up|search (?:plex |the library )?(?:for )?"
    r")\s+(.+)$",
    re.I,
)
_TURN_ON = re.compile(r"\bturn on\s+(.+)$", re.I)
_TURN_OFF = re.compile(r"\bturn off\s+(.+)$", re.I)
_VOLUME = re.compile(
    r"\b(?:set\s+)?(?:the\s+)?(tv|lg|avr|denon|receiver)?\s*volume\s*(?:to\s*)?(\d{1,3})%?",
    re.I,
)
_MUTE = re.compile(r"\b(un)?mute\s+(?:the\s+)?(tv|lg|avr|denon|receiver)\b", re.I)
_SOURCE = re.compile(
    r"\b(?:set|switch)\s+(?:the\s+)?(tv|lg|avr|denon|receiver)\s+(?:to\s+|source\s+|input\s+)(.+)$",
    re.I,
)
_INSPECT = re.compile(r"\binspect\s+(\S+)", re.I)
_MOVIE = re.compile(r"\b(movie|film|radarr)\b", re.I)
_SERIES = re.compile(r"\b(show|series|season|episode|sonarr)\b", re.I)
_OVERSEERR = re.compile(r"\b(overseerr|request)\b", re.I)
_DOWNLOAD_PROGRESS = re.compile(
    r"\b("
    r"how far along|"
    r"download progress|download status|queue (?:status|progress)|"
    r"what(?:'s| is) downloading|anything downloading|"
    r"(?:is|how(?:'s| is)) .{0,60}\bdownloading\b|"
    r"(?:check|show|get) (?:the )?(?:download|radarr|sonarr)(?: progress|status|queue)?"
    r")\b",
    re.I,
)
_DOWNLOAD_PROGRESS_TITLE = re.compile(
    r"(?:"
    r"how far along (?:is |with )?(?:the )?(?:download (?:of |for )?)?|"
    r"download progress (?:for |on |of )|"
    r"(?:check|show|get) (?:the )?(?:download|queue)(?: progress|status)? (?:for |on |of )|"
    r"is (?:the )?(?:download (?:of |for )?)?|"
    r"how(?:'s| is) (?:the )?(?:download (?:of |for )?)?"
    r")(.+?)(?:\s+downloading)?[.?!]*$",
    re.I,
)
_GRAB = re.compile(
    r"\b(download|grab|snatch|request|get me)\b"
    r"|\badd .{0,80}\b(to|in) (radarr|sonarr|overseerr|the library|the queue)\b",
    re.I,
)
_FOOD = re.compile(
    r"\b("
    r"thuisbezorgd|just\s*eat|takeaway|"
    r"order food|order (a |some )?(pizza|burger|sushi|pho|noodles)|"
    r"i'?m hungry|food delivery|nearby restaurants|restaurants nearby|"
    r"what('s| is) (for dinner|to eat)|"
    r"browse restaurants|food cart|my (food )?cart"
    r")\b",
    re.I,
)
_FOOD_CART = re.compile(r"\b(cart|basket)\b", re.I)
_FOOD_ORDER = re.compile(r"\b(place|submit|checkout|confirm)\b.*\b(order|cart|basket)\b", re.I)
_CONNECT = re.compile(r"\bconnect(?: me)? to\s+(.+)", re.I)
_HOUSE_CONNECT = re.compile(r"\b(denon|avr|tv|lg|plex|home assistant|\bha\b|light)\b", re.I)
_REMEMBER_FACT = re.compile(r"\bremember (?:that |this |i |my |we )(.+)$", re.I)
_FORGET_FACT = re.compile(r"\bforget (?:that |this |my |the )(.+)$", re.I)
_MEMORY_LIST = re.compile(
    r"\b(what do you remember|what you remember|list (?:my )?preferences|show (?:my )?(?:house )?memory)\b",
    re.I,
)
_MEMORY_SEARCH = re.compile(r"\b(?:do you remember|search memory|recall)\b", re.I)
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
    if _FOOD.search(raw) or (_FOOD_CART.search(raw) and _FOOD_ORDER.search(raw)):
        return _food_plan(raw)
    if _MEMORY_LIST.search(raw):
        return {"tool": "memory_list", "args": {"kind": "preferences"}}
    if _MEMORY_SEARCH.search(raw):
        return {"tool": "memory_search", "args": {"query": raw}}
    forget = _FORGET_FACT.search(raw)
    if forget:
        rest = forget.group(1).strip(" .?!")
        return {"tool": "memory_forget", "args": {"key": rest, "text": rest}}
    remember = _REMEMBER_FACT.search(raw)
    if remember:
        rest = remember.group(1).strip(" .?!")
        return {"tool": "memory_remember", "args": {"text": rest, "value": rest, "key": rest}}
    progress = _download_progress_plan(raw)
    if progress:
        return progress
    if _GRAB.search(raw):
        query = _media_query(raw)
        if _OVERSEERR.search(raw):
            return {"tool": "overseerr_request", "args": {"query": query or raw}}
        if _SERIES.search(raw) and not _MOVIE.search(raw):
            return {"tool": "sonarr_add", "args": {"query": query or raw}}
        if _MOVIE.search(raw):
            return {"tool": "radarr_add", "args": {"query": query or raw}}
        return {"tool": "overseerr_request", "args": {"query": query or raw}}
    transport = _infuse_transport_plan(raw)
    if transport:
        return transport
    put_infuse = _PUT_ON_INFUSE.search(raw)
    if put_infuse:
        title = _play_title_clean(put_infuse.group(1))
        return {"tool": "infuse_play", "args": {"query": title}}
    play_on = _PLAY_ON_TV.search(raw)
    if play_on:
        title = _play_title_clean(play_on.group(1))
        player = _plex_player_hint(play_on.group(2))
        from hearth.tools.infuse import prefer_infuse_for_apple_tv

        if prefer_infuse_for_apple_tv(player) or "infuse" in player.lower() or "firecore" in player.lower():
            return {"tool": "infuse_play", "args": {"query": title}}
        return {"tool": "plex_play", "args": {"query": title, "player": player}}
    play_title = _PLAY_TITLE.search(raw)
    if play_title and not _PLAYING.search(raw):
        title = _play_title_clean(play_title.group(1))
        # Avoid treating "play" as resume on HA media_player without a title.
        if title and title.lower() not in {"it", "that", "this", "something"}:
            from hearth.tools.infuse import prefer_infuse_for_apple_tv

            if prefer_infuse_for_apple_tv(None):
                return {"tool": "infuse_play", "args": {"query": title}}
            return {"tool": "plex_play", "args": {"query": title}}
    if _PLEX_CLIENTS.search(raw):
        return {"tool": "plex_clients", "args": {}}
    genre_plan = _plex_genre_plan(raw)
    if genre_plan is not None:
        return genre_plan
    if _MEDIA_STATUS.search(raw):
        return {"tool": "house_media", "args": {}}
    mute = _MUTE.search(raw)
    if mute:
        device = _media_device(mute.group(2))
        action = "unmute" if mute.group(1) else "volume_mute"
        return {"tool": "ha_media_control", "args": {"device": device, "action": action}}
    vol = _VOLUME.search(raw)
    if vol:
        device = _media_device(vol.group(1) or "avr")
        return {
            "tool": "ha_media_control",
            "args": {
                "device": device,
                "action": "volume_set",
                "volume_level": int(vol.group(2)),
            },
        }
    source = _SOURCE.search(raw)
    if source:
        device = _media_device(source.group(1))
        return {
            "tool": "ha_media_control",
            "args": {
                "device": device,
                "action": "select_source",
                "source": source.group(2).strip(" ."),
            },
        }
    m = _TURN_ON.search(raw)
    if m:
        return _turn_plan(m.group(1), on=True)
    m = _TURN_OFF.search(raw)
    if m:
        return _turn_plan(m.group(1), on=False)
    m = _INSPECT.search(raw)
    if m:
        return {"tool": "docker_inspect", "args": {"container": m.group(1)}}
    if _PLAYING.search(raw) or (_PLEX_ONLY.search(raw) and not _GRAB.search(raw)):
        return {"tool": "plex_now_playing", "args": {}}
    if _WEB_SEARCH.search(raw) and not _WEATHER.search(raw):
        return {"tool": "web_search", "args": {"query": _web_search_query(raw)}}
    if _WEATHER.search(raw):
        return {"tool": "get_weather", "args": {}}
    about = _about_media_plan(raw)
    if about is not None:
        return about
    if _DOCKER.search(raw):
        return {"tool": "docker_ps", "args": {}}
    if _WORKSPACE.search(raw):
        return {"tool": "workspace_list", "args": {}}
    if _LIGHTS.search(raw):
        return {"tool": "ha_list_entities", "args": {}}
    return None


def _plex_genre_plan(raw: str) -> dict[str, Any] | None:
    """Route genre browse / list-genres asks to plex_browse_genre."""
    if _PLEX_GENRES_LIST.search(raw):
        media_type = "show" if _SERIES.search(raw) and not _MOVIE.search(raw) else "movie"
        return {"tool": "plex_browse_genre", "args": {"genre": "", "type": media_type}}

    match = _PLEX_GENRE_BROWSE.search(raw)
    if not match:
        return None
    # Don't steal play / grab / download-progress phrasing.
    if _GRAB.search(raw) or _PLAY_ON_TV.search(raw) or _PLAY_TITLE.search(raw) or _DOWNLOAD_PROGRESS.search(raw):
        return None

    genre = (
        match.groupdict().get("genre")
        or match.groupdict().get("genre2")
        or match.groupdict().get("genre3")
        or ""
    ).strip(" .?!'\"")
    kind = (
        match.groupdict().get("kind")
        or match.groupdict().get("kind2")
        or ""
    ).strip().lower()

    genre = re.sub(r"\s+", " ", genre).strip()
    # Drop leading filler the regex may have left ("all the Animation").
    genre = re.sub(r"^(all|the|our|my|some|any)\s+", "", genre, flags=re.I).strip()
    if not genre or genre.lower() in {
        "a",
        "an",
        "the",
        "some",
        "any",
        "good",
        "new",
        "old",
        "best",
        "recent",
        "plex",
        "library",
        "movie",
        "movies",
        "film",
        "films",
        "show",
        "shows",
        "series",
    }:
        return None

    media_type = "show" if kind in {"show", "shows", "series"} or (
        _SERIES.search(raw) and not _MOVIE.search(raw) and not kind
    ) else "movie"
    # "sci fi" / "scifi" / "science fiction" normalize for Plex tag matching.
    if re.fullmatch(r"sci[\-\s]?fi|scifi|science\s+fiction", genre, flags=re.I):
        genre = "Science Fiction"
    return {"tool": "plex_browse_genre", "args": {"genre": genre, "type": media_type}}


def _about_media_plan(raw: str) -> dict[str, Any] | None:
    """Route “tell me about / what’s … movie” asks to plex_search for the glass overlay."""
    if not _ABOUT_MEDIA.search(raw):
        return None
    # Avoid stealing pure weather / food / light questions that also matched softly.
    if _WEATHER.search(raw) or _FOOD.search(raw) or _LIGHTS.search(raw) or _DOCKER.search(raw):
        return None
    if _GRAB.search(raw) or _PLAY_ON_TV.search(raw) or _DOWNLOAD_PROGRESS.search(raw):
        return None
    match = _ABOUT_MEDIA_TITLE.search(raw)
    if not match:
        return None
    title = _play_title_clean(match.group(1))
    title = re.sub(
        r"\b(the )?(movie|film|show|series|in (plex|the library))\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip(" .?!'\"")
    if not title or title.lower() in {"it", "that", "this", "something", "a movie", "a film"}:
        return None
    # Prefer plex library detail for visualization; grab/download still uses _GRAB above.
    if _MOVIE.search(raw) or _SERIES.search(raw) or _PLEX_ONLY.search(raw) or len(title.split()) <= 6:
        return {"tool": "plex_search", "args": {"query": title}}
    return None


def _download_progress_plan(raw: str) -> dict[str, Any] | None:
    """Route download progress / “how far along” asks to radarr_queue / sonarr_queue."""
    if not _DOWNLOAD_PROGRESS.search(raw):
        return None
    # Don't steal an explicit grab/add (“download the movie Dune”).
    if re.search(r"\b(grab|snatch|get me|add )\b", raw, re.I) and not re.search(
        r"\b(how far|progress|status|what(?:'s| is) downloading)\b",
        raw,
        re.I,
    ):
        return None
    if re.search(
        r"\b(download|grab|snatch|get me)\b.+\b(movie|film|show|series|season)\b",
        raw,
        re.I,
    ) and not re.search(r"\b(how far|progress|status|what(?:'s| is) downloading)\b", raw, re.I):
        return None

    title = ""
    match = _DOWNLOAD_PROGRESS_TITLE.search(raw)
    if match:
        title = _play_title_clean(match.group(1))
        title = re.sub(
            r"\b(the )?(movie|film|show|series|download|progress|status|right now|atm|currently)\b",
            " ",
            title,
            flags=re.I,
        )
        title = re.sub(r"\s+downloading$", "", title, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip(" .?!'\"")
        if title.lower() in {
            "",
            "it",
            "that",
            "this",
            "something",
            "everything",
            "anything",
            "now",
            "right now",
        }:
            title = ""

    tool = "sonarr_queue" if _SERIES.search(raw) and not _MOVIE.search(raw) else "radarr_queue"
    args: dict[str, Any] = {}
    if title:
        args["query"] = title
    return {"tool": tool, "args": args}


def _food_plan(text: str) -> dict[str, Any]:
    raw = text.strip()
    lower = raw.lower()
    if _FOOD_ORDER.search(raw) or "place order" in lower or "checkout" in lower:
        return {"tool": "thuisbezorgd_order", "args": {}}
    if _FOOD_CART.search(raw) and not re.search(r"\b(restaurant|menu|pizza|burger)\b", lower):
        return {"tool": "thuisbezorgd_cart", "args": {"action": "view"}}
    cuisine = ""
    for token in ("pizza", "vietnamese", "burger", "sushi", "asian", "italian"):
        if token in lower:
            cuisine = token
            break
    return {
        "tool": "thuisbezorgd_restaurants",
        "args": {"cuisine": cuisine} if cuisine else {},
    }


def _media_device(phrase: str | None) -> str:
    name = (phrase or "").strip().lower()
    if name in {"tv", "lg", "webos", "television"}:
        return "tv"
    return "avr"


def _plex_player_hint(phrase: str | None) -> str:
    name = re.sub(r"\s+", " ", (phrase or "").strip().lower())
    if name in {"tv", "television", "plex"}:
        return "tv"
    if "infuse" in name or "firecore" in name:
        return "Infuse"
    if "apple" in name:
        return "Apple TV"
    if "lg" in name or "webos" in name:
        return "LG"
    if "living" in name:
        return "living room"
    if "shield" in name:
        return "Shield"
    return phrase.strip() if phrase else "tv"


def _infuse_transport_plan(raw: str) -> dict[str, Any] | None:
    match = _INFUSE_TRANSPORT.search(raw)
    if not match:
        return None
    # Action may be in group 1 or 2 depending on word order.
    action_raw = (match.group(1) or match.group(2) or "").strip().lower()
    action_raw = re.sub(r"\s+", " ", action_raw)
    mapping = {
        "pause": "pause",
        "stop": "stop",
        "skip": "skip",
        "skip ahead": "skip",
        "skip forward": "skip",
        "next": "skip",
        "next track": "skip",
        "resume": "play",
        "unpause": "play",
        "play": "play",
        "go back": "previous",
        "previous": "previous",
        "previous track": "previous",
    }
    action = mapping.get(action_raw)
    if not action:
        return None
    # Bare "play …" with a title is handled elsewhere; only transport when ATV/Infuse is named.
    if action == "play" and _PLAY_ON_TV.search(raw):
        return None
    if action == "play" and _PLAY_TITLE.search(raw) and "apple" not in raw.lower() and "infuse" not in raw.lower():
        return None
    return {"tool": "infuse_transport", "args": {"action": action}}


_PLAY_NOISE = re.compile(
    r"\b(please|can you|could you|for me|the movie|the film|the show|a movie|a show)\b",
    re.I,
)


def _play_title_clean(text: str) -> str:
    cleaned = _PLAY_NOISE.sub(" ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip(" .?!'\"")


def _turn_plan(phrase: str, *, on: bool) -> dict[str, Any]:
    cleaned = re.sub(r"^(the|my|our)\s+", "", phrase.strip(), flags=re.I).lower().rstrip(".")
    media_tokens = ("tv", "lg", "webos", "television", "avr", "denon", "receiver", "amp")
    if any(token in cleaned.split() or cleaned == token for token in media_tokens) or cleaned in {
        "lg tv", "webos tv", "lg webos tv", "denon avr",
    }:
        device = "tv" if any(t in cleaned for t in ("tv", "lg", "webos", "television")) else "avr"
        return {
            "tool": "ha_media_control",
            "args": {"device": device, "action": "turn_on" if on else "turn_off"},
        }
    entity = _guess_entity(cleaned, on=on)
    return {"tool": "ha_call_service", "args": entity}


def _guess_entity(phrase: str, *, on: bool) -> dict[str, Any]:
    name = re.sub(r"^(the|my|our)\s+", "", phrase.strip(), flags=re.I).lower().rstrip(".")
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


_WEB_QUERY_PREFIX = re.compile(
    r"^(please |can you |could you )?(search the web( for)?|web search( for)?|"
    r"look( it)? up online( for)?|search online( for)?|google( for)?)\s+",
    re.I,
)


def _web_search_query(text: str) -> str:
    cleaned = _WEB_QUERY_PREFIX.sub("", text.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .?!")
    return cleaned or text.strip()


# Re-export for tests / app
__all__ = ["AgentLoop", "SYSTEM_PROMPT", "route_intent"]
