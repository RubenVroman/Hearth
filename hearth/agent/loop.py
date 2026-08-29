from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from hearth.agent.prompts import SYSTEM_PROMPT
from hearth.agent.registry import ToolRegistry, registry
from hearth.config import settings
from hearth.runtime import runtime

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
        text = user_text.strip()
        if confirm and runtime.pending is not None:
            pending = runtime.pending
            args = dict(pending.args)
            args["confirm"] = True
            args["dry_run"] = False
            result = await self.tools.call(pending.tool, args)
            reply = _format_tool_reply([result.as_dict()])
            runtime.note("assistant", reply)
            runtime.agent_status = "idle"
            return {"reply": reply, "mode": "confirm", "tools": [result.as_dict()]}

        if settings.openai_configured:
            try:
                out = await self._run_openai(text)
                runtime.agent_status = "idle"
                return out
            except Exception as exc:  # noqa: BLE001
                runtime.note("system", f"OpenAI path failed, using local router: {exc}", kind="status")

        out = await self._run_local(text)
        runtime.agent_status = "idle"
        return out

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
                "I can drive the house from here — lights, Denon, LG TV via Home Assistant, "
                "Plex now-playing, workspace files, and docker inspect. "
                "Say what's playing, list lights, or list containers. "
                "Plug in OPENAI_API_KEY for a live language model; the tool registry is already on."
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
            parts.append(f"{name} is destructive and waiting for confirm. Preview: {preview}")
            continue
        if not tool.get("ok"):
            parts.append(f"{name} failed: {tool.get('data')}")
            continue
        data = tool.get("data") or {}
        parts.append(f"{name}: {json.dumps(data, default=str)[:1200]}")
    return "\n".join(parts)


_PLAYING = re.compile(r"\b(now playing|what'?s (on|playing)|plex|now-playing)\b", re.I)
_DOCKER = re.compile(r"\b(docker|containers?)\b", re.I)
_LIGHTS = re.compile(r"\b(lights?|scenes?|rooms?|home assistant|denon|webos|tv)\b", re.I)
_WORKSPACE = re.compile(r"\b(workspace|skills?)\b", re.I)
_TURN_ON = re.compile(r"\bturn on\s+(.+)$", re.I)
_TURN_OFF = re.compile(r"\bturn off\s+(.+)$", re.I)
_INSPECT = re.compile(r"\binspect\s+(\S+)", re.I)


def route_intent(text: str) -> dict[str, Any] | None:
    """Tiny local router so the runtime is useful before an API key is set."""
    raw = text.strip()
    if not raw:
        return None

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
    if _PLAYING.search(raw):
        return {"tool": "plex_now_playing", "args": {}}
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


# Re-export for tests / app
__all__ = ["AgentLoop", "SYSTEM_PROMPT", "route_intent"]
