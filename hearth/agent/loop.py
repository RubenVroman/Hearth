from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from hearth.agent.prompts import SYSTEM_PROMPT, compose_system_prompt_async
from hearth.agent.registry import ToolRegistry, ToolResult, registry
from hearth.config import settings
from hearth.butler.decision import decide_butler_tool, hide_from_llm, is_butler_phrase
from hearth.jev import (
    adopt_verdict,
    current_turn,
    evaluate_message,
    log_shadow_outcome,
    tool_turn,
    turn_lane,
)
from hearth.memory import store as memory_store
from hearth.memory.summarize import maybe_summarize
from hearth.runtime import runtime
from hearth.tools.house import voice_plan
from hearth import widgets as widget_bus

MAX_TURNS = 8


@dataclass
class _TurnScope:
    """Per-call options. Defaults keep the glass Ask-the-House path unchanged."""

    channel: str = "chat"
    announce: bool = True
    blocked_tools: frozenset[str] = field(default_factory=frozenset)
    context_note: str = ""
    idle_reply: str = ""
    external_history: list[dict[str, Any]] | None = None


class AgentLoop:
    def __init__(self, tools: ToolRegistry | None = None) -> None:
        self.tools = tools or registry
        self.history: list[dict[str, Any]] = []
        self._turn = _TurnScope()

    def reset(self) -> None:
        self.history = []

    def _seal(self, out: dict[str, Any], *, detail: str) -> dict[str, Any]:
        """Finish a turn. Glass channels record it; Telegram leaves the overlay alone."""
        if not self._turn.announce:
            out["widgets"] = []
            return out
        reply = str(out.get("reply") or "")
        if reply:
            runtime.note("assistant", reply)
        runtime.set_status("idle")
        widget_bus.finish_turn(ok=True, detail=detail)
        out["widgets"] = runtime.list_widgets()
        return out

    async def run(
        self,
        user_text: str,
        *,
        confirm: bool = False,
        channel: str = "chat",
        recent: list[str] | None = None,
        inherit_turn: bool = False,
        blocked_tools: frozenset[str] | None = None,
        context_note: str = "",
        history: list[dict[str, Any]] | None = None,
        announce: bool | None = None,
        idle_reply: str = "",
    ) -> dict[str, Any]:
        """Run one house turn.

        ``inherit_turn`` reuses a Jev scope the caller already opened (Telegram
        does this) so routing and tool authorization stay one typed decision.
        ``blocked_tools`` are refused inside the loop instead of executed —
        Telegram uses that to keep download queues on the Get button.
        ``announce`` updates the glass transcript; other channels leave it alone.
        """
        self._turn = _TurnScope(
            channel=channel or "chat",
            announce=channel == "chat" if announce is None else bool(announce),
            blocked_tools=frozenset(blocked_tools or ()),
            context_note=(context_note or "").strip(),
            idle_reply=(idle_reply or "").strip(),
            external_history=history,
        )
        if self._turn.announce:
            runtime.set_status("thinking")
            runtime.note("user", user_text)
            widget_bus.start_turn(user_text)
        text = user_text.strip()
        recent_turns = list(recent) if recent is not None else _recent_turns()
        try:
            # One Jev scope per turn. Every tool call underneath is decided from
            # the same typed answer set, so an eight-tool OpenAI turn still costs
            # exactly one System One call. A caller that already opened the scope
            # (Telegram) inherits it instead of paying for a second one.
            if inherit_turn and current_turn() is not None:
                return await self._run_turn(text, recent=recent_turns, confirm=confirm)
            with tool_turn(text, channel=self._turn.channel, recent=recent_turns):
                return await self._run_turn(text, recent=recent_turns, confirm=confirm)
        except Exception:
            if self._turn.announce:
                widget_bus.finish_turn(ok=False, detail="Failed.")
            raise
        finally:
            self._turn = _TurnScope()

    async def _run_turn(
        self,
        text: str,
        *,
        recent: list[str],
        confirm: bool,
    ) -> dict[str, Any]:
        # Claiming clears the pending atomically, so a double confirm cannot run
        # the same destructive tool twice. An expired one falls through and the
        # message is read fresh.
        pending = runtime.claim_pending() if confirm else None
        if pending is not None:
            args = dict(pending.args)
            args["confirm"] = True
            args["dry_run"] = False
            # The user already confirmed this exact call; only Jev's hard
            # stops (refuse / do-not-auto-run) may still block it.
            result = await self.tools.call(
                pending.tool,
                args,
                said=text or pending.tool,
                explicit_confirm=True,
            )
            reply = _format_tool_reply([result.as_dict()])
            out = {
                "reply": reply,
                "mode": "confirm",
                "tools": [result.as_dict()],
            }
            await _after_turn(text or pending.tool, out, channel=self._turn.channel)
            return self._seal(out, detail="Confirmed.")

        # Cheap typed gate before OpenAI / local tool routing (shadow by
        # default). The tool gate reuses this verdict for the whole turn.
        # A caller that already paid for System One this turn (the Telegram
        # media router) keeps that answer set — one decision, not two.
        scope = current_turn()
        if scope is not None and scope.verdict is not None:
            jev_verdict = scope.verdict
        else:
            jev_verdict = await evaluate_message(text, recent=recent)
            adopt_verdict(jev_verdict)
        if jev_verdict.action == "block_cancel":
            reply = (
                "Okay — I won't queue or run that. Say what you'd like instead, "
                "or confirm explicitly if you meant to proceed."
            )
            out = {
                "reply": reply,
                "mode": "jev_cancel",
                "tools": [],
                "jev": jev_verdict.as_log_dict(),
            }
            await _after_turn(text, out, channel=self._turn.channel)
            log_shadow_outcome(
                jev_verdict,
                channel=self._turn.channel,
                tools=[],
                outcome="blocked_cancel",
            )
            return self._seal(out, detail="Cancelled (Jev).")
        if jev_verdict.action == "escalate_cos":
            # Jev already chose this tool this turn; don't ask it again.
            result = await self.tools.call(
                "chief_of_staff",
                {"task": text, "said": text, "repo": settings.cos_repo},
                said=text,
                gate=False,
            )
            used = [result.as_dict()]
            reply = _format_tool_reply(used)
            out = {
                "reply": reply,
                "mode": "jev_cos",
                "tools": used,
                "jev": jev_verdict.as_log_dict(),
            }
            await _after_turn(text, out, channel=self._turn.channel)
            log_shadow_outcome(
                jev_verdict,
                channel=self._turn.channel,
                tools=["chief_of_staff"],
                outcome="escalated_cos",
            )
            return self._seal(out, detail="Escalated (Jev).")

        # Jev chooses shelf and scene-preset tools. Phrases the playback
        # and device routers already own (movie night, lights down, covers)
        # stay on those routes. The OpenAI tool loop never sees shelf/scene.
        routed = route_intent(text)
        butler_turn = routed is None or str(routed.get("tool") or "") in {
            "house_shelf",
            "house_scene",
        }
        decision = decide_butler_tool(text, jev_verdict)
        if butler_turn and decision.run:
            if self._turn.announce:
                runtime.set_status("tool")
            result = await self.tools.call(
                decision.tool,
                decision.as_args(),
                said=text,
            )
            used = [result.as_dict()]
            reply = _format_tool_reply(used)
            mode = "jev_butler" if decision.source == "jev" else "local"
            out = {
                "reply": reply,
                "mode": mode,
                "tools": used,
                "jev": jev_verdict.as_log_dict(),
            }
            await _after_turn(text, out, channel=self._turn.channel)
            log_shadow_outcome(
                jev_verdict,
                channel=self._turn.channel,
                tools=[decision.tool],
                outcome=mode,
            )
            return self._seal(out, detail="Butler tool.")
        if butler_turn and decision.blocked_by_jev and is_butler_phrase(text):
            reply = (
                "Okay — I won't run that."
                if decision.source == "jev_cancel"
                else "That doesn't sound like the shelf or a house scene, so I left it alone."
            )
            out = {
                "reply": reply,
                "mode": "jev_butler",
                "tools": [],
                "jev": jev_verdict.as_log_dict(),
            }
            await _after_turn(text, out, channel=self._turn.channel)
            log_shadow_outcome(
                jev_verdict,
                channel=self._turn.channel,
                tools=[],
                outcome=decision.source,
            )
            return self._seal(out, detail="Butler tool held.")

        if settings.openai_configured:
            try:
                out = await self._run_openai(text)
                if jev_verdict is not None:
                    out["jev"] = jev_verdict.as_log_dict()
                    log_shadow_outcome(
                        jev_verdict,
                        channel=self._turn.channel,
                        tools=[
                            str(t.get("name") or "")
                            for t in (out.get("tools") or [])
                            if isinstance(t, dict)
                        ],
                        outcome=str(out.get("mode") or "openai"),
                    )
                await _after_turn(text, out, channel=self._turn.channel)
                return self._seal(out, detail="Done.")
            except Exception as exc:  # noqa: BLE001
                if self._turn.announce:
                    runtime.note(
                        "system",
                        f"OpenAI path failed, using local router: {exc}",
                        kind="status",
                    )
                    runtime.flash_error("Model call failed")

        out = await self._run_local(text)
        if out.get("mode") == "held":
            return out
        if jev_verdict is not None:
            out["jev"] = jev_verdict.as_log_dict()
            log_shadow_outcome(
                jev_verdict,
                channel=self._turn.channel,
                tools=[
                    str(t.get("name") or "")
                    for t in (out.get("tools") or [])
                    if isinstance(t, dict)
                ],
                outcome=str(out.get("mode") or "local"),
            )
        await _after_turn(text, out, channel=self._turn.channel)
        return self._seal(out, detail="Done.")

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
        history = (
            self._turn.external_history
            if self._turn.external_history is not None
            else self.history
        )
        system = await compose_system_prompt_async(
            user_text,
            include_recent_turns=not bool(history),
        )
        spoken = user_text
        if self._turn.context_note:
            spoken = f"{user_text}\n\n{self._turn.context_note}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": spoken},
        ]
        used: list[dict[str, Any]] = []
        tools = hide_from_llm(self.tools.openai_chat_tools())

        for _ in range(MAX_TURNS):
            kwargs: dict[str, Any] = {
                "model": settings.openai_model,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            response = await client.chat.completions.create(**kwargs)
            try:
                from hearth.openai_usage import record_chat_usage

                record_chat_usage(response, model=settings.openai_model, kind="chat")
            except Exception:  # noqa: BLE001 — never break the house loop for metering
                pass
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
                if self._turn.announce:
                    runtime.set_status("tool")
                for tc in msg.tool_calls:
                    args = _parse_args(tc.function.arguments)
                    if tc.function.name == "chief_of_staff":
                        args.setdefault("said", user_text)
                        args.setdefault("task", user_text)
                    # Queue-shaped tools stay on the caller's confirm button
                    # (Telegram Get). The model hears the refusal and can answer.
                    if tc.function.name in self._turn.blocked_tools:
                        result = ToolResult(
                            name=tc.function.name,
                            ok=False,
                            data={
                                "denied": True,
                                "speak": (
                                    "Tap Get on the card to queue that. "
                                    "I won't grab it from chat."
                                ),
                            },
                        )
                    else:
                        # Every model-chosen tool still passes the Jev gate; a deny
                        # comes back as a tool result the model can react to.
                        result = await self.tools.call(
                            tc.function.name,
                            args,
                            said=user_text,
                        )
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
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": reply})
            trimmed = history[-24:]
            if self._turn.external_history is not None:
                self._turn.external_history[:] = trimmed
            else:
                self.history = trimmed
            return {"reply": reply, "mode": "openai", "tools": used}

        reply = "Stopped after too many tool turns."
        return {"reply": reply, "mode": "openai", "tools": used}

    async def _run_local(self, user_text: str) -> dict[str, Any]:
        plan = route_intent(user_text, jev_lane=_jev_lane())
        used: list[dict[str, Any]] = []
        if plan is not None and str(plan.get("tool") or "") in self._turn.blocked_tools:
            # Caller owns this tool (Telegram queues only from a Get tap).
            return {
                "reply": "",
                "mode": "held",
                "tools": [],
                "held_tool": str(plan.get("tool") or ""),
            }
        if plan is None:
            reply = self._turn.idle_reply or (
                "I can drive the house — lights, scenes, covers, house status, house sleep, "
                "good morning, movie night, climate, the feeder, the purifier, Denon, LG TV, "
                "play titles in Infuse on the "
                "Apple TV (or Plex on LG), grab movies in Radarr or shows in Sonarr, check "
                "download progress, request "
                "via Overseerr, suggest movie cards on the glass UI, order food on Thuisbezorgd, "
                "live web search, Plex now-playing, "
                "workspace, docker inspect. Repo, Gridways, Discord, calendar, and anything I "
                "can't do yet go to Chief of Staff."
            )
            return {"reply": reply, "mode": "local", "tools": used}

        if self._turn.announce:
            runtime.set_status("tool")
        result = await self.tools.call(
            plan["tool"],
            plan.get("args") or {},
            said=user_text,
        )
        used.append(result.as_dict())
        reply = _format_tool_reply(used)
        return {"reply": reply, "mode": "local", "tools": used}


def _recent_turns(limit: int = 4) -> list[str]:
    """A few words of recent chat so Jev can read follow-ups (typed short state)."""
    return [
        line.text
        for line in list(runtime.transcript)[-limit:]
        if getattr(line, "role", "") in {"user", "assistant"} and line.text
    ]


def _jev_lane() -> str:
    """Jev's tool lane for the open turn, or ``""`` when it should not steer."""
    if not settings.jev_route_local_tools:
        return ""
    picked = turn_lane()
    return picked[0] if picked else ""


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
            if data.get("denied") and data.get("speak"):
                # A governance refusal says why in the house voice, not in a code.
                parts.append(str(data["speak"]))
                continue
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
    if name in {
        "house_ritual",
        "house_climate",
        "house_feeder",
        "house_purifier",
        "house_comfort",
    }:
        spoken = str(data.get("speak") or "").strip()
        if spoken:
            return spoken if mock == "" else f"{spoken.rstrip('.')}{mock}."
        return None
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
        state = data.get("entity") or data.get("state") or {}
        state = state if isinstance(state, dict) else {}
        entity = state.get("entity_id") or data.get("entity_id") or "the device"
        return f"Done{mock}: {entity} is {state.get('state', 'updated')}."
    if name in {"house_shelf", "house_scene"}:
        spoken = str(data.get("speak") or "")
        if not spoken:
            return f"{name}{mock}."
        if mock and "(mock)" not in spoken:
            return spoken.rstrip(".") + f"{mock}."
        return spoken
    if name == "house_media":
        return str(data.get("speak") or f"House media{mock}.")
    if name == "house_status":
        return str(data.get("speak") or f"House status{mock}: {data.get('health', 'unknown')}.")
    if name == "house_network":
        return str(data.get("speak") or f"House network{mock}: {data.get('health', 'unknown')}.")
    if name == "media_activity":
        return str(data.get("speak") or f"Media activity{mock}: {data.get('activity', 'done')}.")
    if name == "ha_device_control":
        state = data.get("state") or {}
        label = (state.get("attributes") or {}).get("friendly_name") or data.get("entity_id")
        return f"Done{mock}: {label or data.get('device')} is {state.get('state', 'updated')}."
    if name == "ha_media_control":
        spoken = data.get("speak")
        if spoken:
            return f"Done{mock}: {spoken}"
        return f"Done{mock}: {data.get('device')} {data.get('action')} on {data.get('entity_id')}."
    if name == "tuya_lan_probe":
        return str(data.get("speak") or f"Tuya LAN check{mock}.")
    if name == "ha_discover_entities":
        return str(data.get("speak") or f"No house device entities found{mock}.")
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
    if name in {"radarr_retry", "sonarr_retry"}:
        spoken = data.get("speak")
        if spoken:
            return str(spoken)
        title = data.get("title") or "that download"
        if data.get("needs_pick") or data.get("reason") in {
            "needs_pick",
            "needs_pick_large",
            "needs_pick_keep",
        }:
            return str(spoken or f"{title} needs a release pick.")
        if data.get("ok"):
            return f"Retrying {title} from another source{mock}."
        return f"Couldn't retry {title}{mock}."
    if name == "radarr_list_releases":
        spoken = data.get("speak")
        if spoken:
            return str(spoken)
        return f"No alternate releases{mock}."
    if name == "radarr_grab_release":
        spoken = data.get("speak")
        if spoken:
            return str(spoken)
        title = data.get("title") or "that title"
        if data.get("ok"):
            if data.get("reason") == "kept_both":
                return (
                    f"Downloading an extra release of {title} — "
                    f"keeping the current file{mock}."
                )
            return f"Grabbing a different release of {title}{mock}."
        return f"Couldn't grab that release of {title}{mock}."
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
        if data.get("ok") is False:
            spoken = data.get("speak")
            if spoken:
                return str(spoken)
            if data.get("ambiguous"):
                choices = data.get("choices") or []
                bits = "; ".join(
                    f"{c.get('title')} ({c.get('year')})" if c.get("year") else str(c.get("title"))
                    for c in choices[:4]
                    if c.get("title")
                )
                return f"Which title should I request{mock}? {bits or 'say the year or send a link'}."
            q = data.get("query") or "that"
            return f"I couldn't find a confident Overseerr match for {q}{mock}."
        item = data.get("requested") or {}
        return f"I'll request {item.get('title') or 'that'} in Overseerr{mock}."
    if name == "suggest_titles":
        spoken = data.get("speak")
        if spoken:
            return spoken if mock == "" else f"{spoken.rstrip('.')}" + mock + "."
        results = data.get("results") or []
        if not results:
            return f"No suggestion cards{mock}."
        titles = ", ".join(
            f"{r.get('title')} ({r.get('year')})" if r.get("year") else str(r.get("title"))
            for r in results[:4]
            if r.get("title")
        )
        return f"On screen{mock}: {titles}."
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
_PLAY_REFERENCE = re.compile(
    r"^\s*(?:put|play|throw|send)\s+(?:it|that|this)\s+on\s+(?:the\s+)?("
    r"infuse|firecore|apple\s*tv|atv|lg(?:\s*webos)?(?:\s*tv)?|webos|"
    r"living\s*room(?:\s*tv)?|shield|plex|tv|television"
    r")\s*[.!?]*\s*$",
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
    r".*\b(?:apple\s*tv|infuse|atv|tv|television)\b"
    r"|\b(?:apple\s*tv|infuse|atv|tv|television)\b.*"
    r"\b(pause|stop|skip|next|resume|unpause|go\s+back|previous)\b",
    re.I,
)
_BARE_TRANSPORT = re.compile(
    r"^\s*(pause|resume|unpause)(?:\s+(?:it|that|this))?\s*[.!?]*\s*$",
    re.I,
)
_MEDIA_STATUS = re.compile(
    r"\b(house media|media status|media inventory|what'?s on the (tv|avr|denon|apple\s*tv)|"
    r"is the (tv|avr|denon|apple\s*tv) on|avr status|tv status)\b",
    re.I,
)
_NETWORK_STATUS = re.compile(
    r"\b(network (?:status|inventory|devices?|connections?|health)|"
    r"what(?:'s| is) (?:connected|on (?:the|my|our) network)|"
    r"connected devices?|all (?:home assistant|house|network) (?:devices?|entities)|"
    r"check (?:all )?(?:the )?(?:devices?|connections?))\b",
    re.I,
)
_HOUSE_STATUS = re.compile(
    r"\b(?:"
    r"(?:house|home)\s+(?:status|snapshot|check)|"
    r"status\s+of\s+(?:the\s+)?(?:house|home)|"
    r"how(?:'s| is)\s+(?:the\s+)?(?:house|home)|"
    r"what(?:'s| is)\s+on\s+(?:in|around|at)\s+(?:the\s+)?(?:house|home)"
    r")\b",
    re.I,
)
_MEDIA_ACTIVITY = re.compile(
    r"\b(?:watch|use|start|prepare|switch to)\s+(?:the\s+)?(apple\s*tv|atv|tv|television)\b"
    r"|\b(?:turn|switch|power)\s+(?:the\s+)?(?:whole\s+)?(?:media|tv)\s+(chain\s+)?off\b",
    re.I,
)
_MOVIE_NIGHT = re.compile(
    r"^\s*(?:(?:set|start|prepare|it'?s)\s+)?"
    r"(?:movie|film|cinema)\s+night(?:\s+mode)?\s*[.!?]*\s*$",
    re.I,
)
_LIGHTS_DOWN = re.compile(
    r"^\s*(?:turn|bring|put|dim)?\s*(?:the\s+)?lights?\s+down\s*[.!?]*\s*$"
    r"|^\s*dim\s+(?:the\s+)?lights?\s*[.!?]*\s*$",
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
_SUGGEST_TITLES = re.compile(
    r"\b("
    r"(?:suggest|recommend)(?:\s+[\w'’-]+){0,6}\s+(?:movies?|films?|shows?|series|titles?)|"
    r"movie recommendations?|film recommendations?|"
    r"what should (?:we|i) watch|"
    r"any (?:good )?(?:movie|film|show) (?:ideas?|recs?|recommendations?)|"
    r"give me (?:some )?(?:movie|film|show) (?:ideas?|recs?|suggestions?)"
    r")\b",
    re.I,
)
_SHOW_SUGGESTIONS_UI = re.compile(
    r"\b("
    r"show (?:them|those|these|it|the(?:se|m)?(?: titles?| movies?| films?| shows?)?)"
    r"\s+(?:on (?:the )?(?:ui|screen|overlay|display|glass)|here)|"
    r"put (?:them|those|these)\s+on\s+(?:the )?(?:ui|screen|overlay|display)|"
    r"display (?:them|those|these|the titles?)(?:\s+on (?:the )?(?:ui|screen|overlay))?|"
    r"can you show (?:them|those|these)(?:\s+on (?:the )?(?:ui|screen))?"
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
_VOLUME_STEP = re.compile(
    r"\b(?:(?:(tv|lg|avr|denon|receiver)\s+)?volume\s+(up|down)"
    r"|turn\s+(?:(?:the\s+)?(tv|lg|avr|denon|receiver|it)\s+)?(up|down))\b",
    re.I,
)
_MUTE = re.compile(r"\b(un)?mute\s+(?:the\s+)?(tv|lg|avr|denon|receiver)\b", re.I)
_SOURCE = re.compile(
    r"\b(?:set|switch)\s+(?:the\s+)?(tv|lg|avr|denon|receiver)\s+(?:to\s+|source\s+|input\s+)(.+)$",
    re.I,
)
_LIGHT_BRIGHTNESS = re.compile(
    r"\b(?:dim|set)\s+(?:the\s+)?(.+?\blights?)\s+"
    r"(?:to|at)\s+(\d{1,3})%?\s*[.?!]*$",
    re.I,
)
_SCENE_ACTIVATE = re.compile(
    r"\b(?:activate|run|start|turn\s+on)\s+(?:the\s+)?(?:"
    r"scene\s+(.+?)|(.+?)\s+scene|"
    r"(movie\s+night|good\s+night)"
    r")\s*[.?!]*$",
    re.I,
)
_COVER_ACTION = re.compile(
    r"\b(open|close|stop)\s+(?:the\s+)?"
    r"(.+?(?:cover|blind|blinds|shade|shades|curtain|curtains|shutter|shutters))"
    r"\s*[.?!]*$",
    re.I,
)
_COVER_POSITION = re.compile(
    r"\b(?:set|move)\s+(?:the\s+)?"
    r"(.+?(?:cover|blind|blinds|shade|shades|curtain|curtains|shutter|shutters))"
    r"\s+(?:to\s+)?(\d{1,3})%?\s*[.?!]*$",
    re.I,
)
# Videoland on LG — before generic play/plex so Dutch "zet … aan op Videoland" stays house-local.
_VIDEOLAND_PLAY = re.compile(
    r"(?:"
    r"(?:play|start|put on|watch)\s+(.+?)\s+on\s+(?:the\s+)?videoland\b"
    r"|(?:zet|speel|start|doe)\s+(.+?)\s+(?:aan\s+)?(?:op|in)\s+videoland\b"
    r"|(?:op|in)\s+videoland\s+(?:zet|speel|start)\s+(.+?)$"
    r")",
    re.I,
)
_VIDEOLAND_OPEN = re.compile(
    r"\b(?:open|launch|start|start\s+op)\s+(?:de\s+|the\s+)?videoland\b"
    r"|\bvideoland\s+(?:openen|starten)\b"
    r"|\bopen\s+videoland\b",
    re.I,
)
_VIDEOLAND_PROFILE = re.compile(
    r"(?:"
    r"(?:open|switch(?:\s+to)?|select|kies|open\s+het)\s+"
    r"(?:the\s+|het\s+)?(?:videoland\s+)?profiel(?:e)?\s+[\"“”']?(.+?)[\"“”']?"
    r"(?:\s+(?:in|on|op)\s+(?:the\s+)?videoland)?\b"
    r"|(?:switch|zet)\s+videoland\s+(?:to|naar)\s+[\"“”']?(.+?)[\"“”']?\b"
    r"|(?:videoland\s+)?profile\s+[\"“”']?(.+?)[\"“”']?"
    r")",
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
_DOWNLOAD_RETRY = re.compile(
    r"\b("
    r"try\s+(?:it\s+)?(?:again|another\s+source)|"
    r"another\s+source|"
    r"(?:get|grab|download)\s+a\s+new\s+one|"
    r"(?:get|grab|download)\s+(?:a\s+)?new\s+version|"
    r"(?:get|grab|download)\s+another\s+(?:one|version|release|copy|download)|"
    r"(?:a\s+)?new\s+version|"
    r"(?:smaller|other)\s+(?:version|release|one|copy)|"
    r"find\s+another(?:\s+download)?"
    r"|already\s+(?:there|in\s+(?:the\s+)?library)"
    r"|don'?t\s+delete"
    r"|do\s+not\s+delete"
    r"|keep\s+(?:the\s+)?(?:old|current|existing|both)"
    r"|another\s+copy"
    r"|extra\s+(?:download|copy|release)|"
    r"too\s+big|"
    r"won'?t\s+play|"
    r"doesn'?t\s+play|"
    r"no\s+(?:usable\s+)?file|"
    r"missing\s+(?:the\s+)?file|"
    r"(?:this\s+)?download\s+(?:didn'?t|doesn'?t|won'?t|isn'?t)\s+work|"
    r"download\s+(?:failed|stalled|stuck)|"
    r"(?:is\s+)?stalled|"
    r"retry|"
    r"blocklist|"
    r"andere\s+bron|"
    r"andere\s+versie|"
    r"te\s+groot|"
    r"speelt\s+niet|"
    r"kleinere?\s+(?:versie|release)|"
    r"download\s+werkt\s+niet|"
    r"probeer\s+(?:opnieuw|een\s+andere)"
    r")\b",
    re.I,
)
_DOWNLOAD_RETRY_TITLE = re.compile(
    r"(?:"
    r"retry(?:\s+the)?(?:\s+download)?(?:\s+of|\s+for)?|"
    r"try\s+another\s+source\s+(?:for|on)|"
    r"(?:get|grab)\s+a\s+new\s+(?:one\s+)?(?:for|of)|"
    r"(?:get|grab|download)\s+(?:a\s+)?new\s+version\s+(?:for|of)|"
    r"(?:get|grab|download)\s+another\s+(?:version|release|one|copy)\s+(?:for|of)|"
    r"(?:too\s+big|won'?t\s+play|doesn'?t\s+play).{0,40}\b(?:for|of|on)\b|"
    r"download\s+(?:of|for)"
    r")\s+(.+?)(?:\s+didn'?t\s+work)?[.?!]*$",
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


def _playback_scene_route(raw: str) -> dict[str, Any] | None:
    """Playback chain and explicit device routes that outrank butler presets."""
    # Explicit activate/start/turn on is the scene command. Bare movie night
    # stays the receiver-centric activity, checked just below.
    scene = _SCENE_ACTIVATE.search(raw)
    if scene:
        target = next((group for group in scene.groups() if group), "")
        return {
            "tool": "ha_device_control",
            "args": {
                "device": target.strip(" ."),
                "domain": "scene",
                "action": "activate",
            },
        }
    if _MOVIE_NIGHT.search(raw):
        return {"tool": "media_activity", "args": {"activity": "movie_night"}}
    if _LIGHTS_DOWN.search(raw):
        return {
            "tool": "ha_device_control",
            "args": {
                "device": settings.ha_movie_night_scene.strip() or "Movie night",
                "domain": "scene",
                "action": "activate",
            },
        }
    light_brightness = _LIGHT_BRIGHTNESS.search(raw)
    if light_brightness:
        return {
            "tool": "ha_device_control",
            "args": {
                "device": light_brightness.group(1).strip(" ."),
                "domain": "light",
                "action": "brightness",
                "value": int(light_brightness.group(2)),
            },
        }
    cover_position = _COVER_POSITION.search(raw)
    if cover_position:
        return {
            "tool": "ha_device_control",
            "args": {
                "device": cover_position.group(1).strip(" ."),
                "domain": "cover",
                "action": "set_position",
                "value": int(cover_position.group(2)),
            },
        }
    cover_action = _COVER_ACTION.search(raw)
    if cover_action:
        return {
            "tool": "ha_device_control",
            "args": {
                "device": cover_action.group(2).strip(" ."),
                "domain": "cover",
                "action": cover_action.group(1).lower(),
            },
        }
    return None


def route_intent(text: str, *, jev_lane: str = "") -> dict[str, Any] | None:
    """Tiny local router so the runtime is useful before an API key is set.

    ``jev_lane`` is the tool family Jev picked for this turn. When it is set and
    the text can substantiate it, Jev's choice wins over regex precedence; when
    it cannot, routing falls through to the chain below unchanged.
    """
    raw = text.strip()
    if not raw:
        return None
    # Movie night, lights down, and covers stay on the playback/device routes.
    owned = _playback_scene_route(raw)
    if owned is not None:
        return owned

    if jev_lane:
        planned = _plan_for_lane(raw, jev_lane)
        if planned is not None:
            return planned

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
    # Rituals, climate, feeder, purifier. voice_plan already falls through to
    # the wider device phrases, so "zet de airco op 21" lands here too.
    house = voice_plan(raw)
    if house:
        return house
    # Shelf and unclaimed scene presets belong to the Jev gate, so they do
    # not become now-playing or a recommendation search.
    if is_butler_phrase(raw):
        return None
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
    retry = _download_retry_plan(raw)
    if retry:
        return retry
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
    if _HOUSE_STATUS.search(raw):
        return {"tool": "house_status", "args": {}}
    if _NETWORK_STATUS.search(raw):
        return {"tool": "house_network", "args": {}}
    videoland_plan = _videoland_plan(raw)
    if videoland_plan is not None:
        return videoland_plan
    play_reference = _PLAY_REFERENCE.search(raw)
    if play_reference:
        reference_plan = _play_reference_plan(play_reference.group(1))
        if reference_plan is not None:
            return reference_plan
    playback = _playback_plan(raw)
    if playback is not None:
        return playback
    if _PLEX_CLIENTS.search(raw):
        return {"tool": "plex_clients", "args": {}}
    genre_plan = _plex_genre_plan(raw)
    if genre_plan is not None:
        return genre_plan
    suggest_plan = _suggest_titles_plan(raw)
    if suggest_plan is not None:
        return suggest_plan
    if _MEDIA_STATUS.search(raw):
        return {"tool": "house_media", "args": {}}
    device = _device_plan(raw)
    if device is not None:
        return device
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


def _playback_plan(raw: str) -> dict[str, Any] | None:
    """Media-chain activity, Apple TV transport, and play-a-title routing."""
    activity = _MEDIA_ACTIVITY.search(raw)
    if activity:
        lower_activity = raw.lower()
        if " off" in lower_activity:
            target = "off"
        elif "apple" in lower_activity or "atv" in lower_activity:
            target = "apple_tv"
        else:
            target = "tv"
        return {"tool": "media_activity", "args": {"activity": target}}
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
        if title.lower() in {"it", "that", "this", "something"}:
            return None
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
    return None


def _device_plan(raw: str) -> dict[str, Any] | None:
    """Mute / volume / source on the AVR or TV, and named turn on/off."""
    mute = _MUTE.search(raw)
    if mute:
        device = _media_device(mute.group(2))
        action = "unmute" if mute.group(1) else "volume_mute"
        return {"tool": "ha_media_control", "args": {"device": device, "action": action}}
    volume_step = _VOLUME_STEP.search(raw)
    if volume_step:
        device_name = volume_step.group(1) or volume_step.group(3) or "avr"
        direction = volume_step.group(2) or volume_step.group(4)
        device = _media_device("avr" if device_name == "it" else device_name)
        return {
            "tool": "ha_media_control",
            "args": {"device": device, "action": f"volume_{direction.lower()}"},
        }
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
    return None


def _media_queue_plan(raw: str) -> dict[str, Any] | None:
    """Grab / request routing for the media_queue lane."""
    retry = _download_retry_plan(raw)
    if retry is not None:
        return retry
    query = _media_query(raw)
    if len(query) < 2:
        return None
    # A queue needs something title-shaped. Without a grab verb or a media noun,
    # a long sentence is prose, not a title, and searching it would only miss.
    if len(query.split()) > 4 and not (
        _GRAB.search(raw) or _MOVIE.search(raw) or _SERIES.search(raw) or _OVERSEERR.search(raw)
    ):
        return None
    if _OVERSEERR.search(raw):
        return {"tool": "overseerr_request", "args": {"query": query}}
    if _SERIES.search(raw) and not _MOVIE.search(raw):
        return {"tool": "sonarr_add", "args": {"query": query}}
    if _MOVIE.search(raw):
        return {"tool": "radarr_add", "args": {"query": query}}
    return {"tool": "overseerr_request", "args": {"query": query}}


def _media_library_plan(raw: str) -> dict[str, Any] | None:
    """Read-only library / suggestion routing for the media_library lane."""
    if _PLEX_CLIENTS.search(raw):
        return {"tool": "plex_clients", "args": {}}
    genre = _plex_genre_plan(raw)
    if genre is not None:
        return genre
    suggest = _suggest_titles_plan(raw)
    if suggest is not None:
        return suggest
    if _MEDIA_STATUS.search(raw):
        return {"tool": "house_media", "args": {}}
    about = _about_media_plan(raw)
    if about is not None:
        return about
    if _PLAYING.search(raw) or _PLEX_ONLY.search(raw):
        return {"tool": "plex_now_playing", "args": {}}
    return None


def _files_plan(raw: str) -> dict[str, Any] | None:
    """Docker / workspace routing for the files lane."""
    inspect = _INSPECT.search(raw)
    if inspect:
        return {"tool": "docker_inspect", "args": {"container": inspect.group(1)}}
    if _DOCKER.search(raw):
        return {"tool": "docker_ps", "args": {}}
    if _WORKSPACE.search(raw):
        return {"tool": "workspace_list", "args": {}}
    return None


def _memory_write_plan(raw: str) -> dict[str, Any] | None:
    forget = _FORGET_FACT.search(raw)
    if forget:
        rest = forget.group(1).strip(" .?!")
        return {"tool": "memory_forget", "args": {"key": rest, "text": rest}}
    remember = _REMEMBER_FACT.search(raw)
    if remember:
        rest = remember.group(1).strip(" .?!")
        return {"tool": "memory_remember", "args": {"text": rest, "value": rest, "key": rest}}
    return None


def _plan_for_lane(raw: str, lane: str) -> dict[str, Any] | None:
    """Build a plan inside the tool lane Jev picked for this turn.

    Jev chooses the lane; the arguments are still derived deterministically from
    the text, never from prose. Returning ``None`` means the message cannot
    substantiate the lane, so :func:`route_intent` falls through to its own
    precedence chain — the fail-open path.
    """
    if lane in {"", "no_tool"}:
        return None
    if lane == "escalate_cos":
        return {
            "tool": "chief_of_staff",
            "args": {"task": raw, "said": raw, "repo": settings.cos_repo},
        }
    if lane == "weather":
        return {"tool": "get_weather", "args": {}}
    if lane == "network":
        return {"tool": "house_network", "args": {}}
    if lane == "web":
        return {"tool": "web_search", "args": {"query": _web_search_query(raw)}}
    if lane == "food":
        if _FOOD.search(raw) or _FOOD_CART.search(raw) or _FOOD_ORDER.search(raw):
            return _food_plan(raw)
        return None
    if lane == "memory_read":
        if _MEMORY_LIST.search(raw):
            return {"tool": "memory_list", "args": {"kind": "preferences"}}
        return {"tool": "memory_search", "args": {"query": raw}}
    if lane == "memory_write":
        return _memory_write_plan(raw)
    if lane == "media_queue":
        return _media_queue_plan(raw)
    if lane == "media_status":
        return _download_progress_plan(raw) or {"tool": "radarr_queue", "args": {}}
    if lane == "media_playback":
        return _videoland_plan(raw) or _playback_plan(raw) or _device_plan(raw)
    if lane == "media_library":
        return _media_library_plan(raw)
    if lane == "lights":
        return _device_plan(raw) or (
            {"tool": "ha_list_entities", "args": {}} if _LIGHTS.search(raw) else None
        )
    if lane == "files":
        return _files_plan(raw)
    return None


def _videoland_plan(raw: str) -> dict[str, Any] | None:
    """Route Videoland title / open / profile asks to videoland_play (honest HA path)."""
    play = _VIDEOLAND_PLAY.search(raw)
    if play:
        title = next((g for g in play.groups() if g), "") or ""
        title = _play_title_clean(title)
        if title and title.lower() not in {"it", "that", "this", "iets", "het"}:
            return {"tool": "videoland_play", "args": {"query": title}}

    profile = _VIDEOLAND_PROFILE.search(raw)
    if profile and (
        "videoland" in raw.lower()
        or "profiel" in raw.lower()
        or "profile" in raw.lower()
    ):
        name = next((g for g in profile.groups() if g), "") or ""
        name = _play_title_clean(name)
        name = re.sub(
            r"\s+(?:in|on|op)\s+(?:the\s+|de\s+)?videoland\b",
            "",
            name,
            flags=re.I,
        )
        name = name.strip(" .?!'\"")
        if name and name.lower() not in {"videoland", "the", "het", "de"}:
            return {"tool": "videoland_play", "args": {"profile": name}}

    if _VIDEOLAND_OPEN.search(raw):
        return {"tool": "videoland_play", "args": {}}

    return None


def _plex_genre_plan(raw: str) -> dict[str, Any] | None:
    """Route genre browse / list-genres asks to plex_browse_genre."""
    if _PLEX_GENRES_LIST.search(raw):
        media_type = "show" if _SERIES.search(raw) and not _MOVIE.search(raw) else "movie"
        return {"tool": "plex_browse_genre", "args": {"genre": "", "type": media_type}}

    match = _PLEX_GENRE_BROWSE.search(raw)
    if not match:
        return None
    # Don't steal play / grab / download-progress / recommendation phrasing.
    if (
        _GRAB.search(raw)
        or _PLAY_ON_TV.search(raw)
        or _PLAY_TITLE.search(raw)
        or _DOWNLOAD_PROGRESS.search(raw)
        or _SUGGEST_TITLES.search(raw)
        or _SHOW_SUGGESTIONS_UI.search(raw)
    ):
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


def _titles_from_transcript() -> list[str]:
    """Pull recent title-like phrases from assistant turns (for 'show them on the UI')."""
    from hearth.tools.suggest import parse_title_list

    lines = list(runtime.transcript)
    for line in reversed(lines):
        if line.role != "assistant":
            continue
        text = (line.text or "").strip()
        if not text:
            continue
        parsed = parse_title_list(text)
        # Keep only items that look like real titles (not prose leftovers).
        cleaned = [
            t
            for t in parsed
            if t
            and not re.match(r"^(top picks?|on screen|here)\b", t, re.I)
            and len(t) <= 80
        ]
        if len(cleaned) >= 2:
            return cleaned[:6]
        years = re.findall(
            r"([A-Z][^(\n]{1,60}?)\s*\((\d{4})\)",
            text,
        )
        if len(years) >= 2:
            out = []
            for title, year in years[:6]:
                bit = re.sub(r"^\d+[\).\]]\s*", "", title).strip(" .\"'-•·:")
                bit = re.sub(r"^(?:top picks?|recommendations?)\s*:?\s*", "", bit, flags=re.I)
                if bit:
                    out.append(f"{bit.strip()} ({year})")
            if len(out) >= 2:
                return out
    return []


def _suggest_titles_plan(raw: str) -> dict[str, Any] | None:
    """Route recommendation / show-on-UI asks to suggest_titles (glass overlay)."""
    if _GRAB.search(raw) or _PLAY_ON_TV.search(raw) or _PLAY_TITLE.search(raw):
        return None
    if _DOWNLOAD_PROGRESS.search(raw) or _PLEX_GENRES_LIST.search(raw):
        return None

    media_type = "movie"
    if re.search(r"\b(tv shows?|series|tv series|sonarr)\b", raw, re.I) and not _MOVIE.search(raw):
        media_type = "show"
    elif _SERIES.search(raw) and not _MOVIE.search(raw):
        # Avoid matching the verb in "show them on the UI".
        if not re.search(r"\bshow\s+(?:them|those|these|it|me)\b", raw, re.I):
            media_type = "show"
    if _SHOW_SUGGESTIONS_UI.search(raw):
        titles = _titles_from_transcript()
        if titles:
            return {"tool": "suggest_titles", "args": {"titles": titles, "type": media_type}}
        # No prior list — still open a short default suggestion pack.
        return {
            "tool": "suggest_titles",
            "args": {"query": "movie recommendations", "type": media_type},
        }

    if not _SUGGEST_TITLES.search(raw):
        return None

    # "suggest sci-fi movies" — keep the mood in query for pack selection.
    query = re.sub(
        r"^\s*(?:please\s+)?(?:can you\s+|could you\s+)?",
        "",
        raw,
        flags=re.I,
    ).strip(" .?!")
    return {"tool": "suggest_titles", "args": {"query": query, "type": media_type}}


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


def _download_retry_plan(raw: str) -> dict[str, Any] | None:
    """Route stalled/failed / try-another-source / too-big asks to radarr_retry."""
    if not _DOWNLOAD_RETRY.search(raw):
        return None
    # Don't steal a fresh grab (“download the movie Dune”) unless it also
    # clearly asks for another source / says the download failed / too big.
    if re.search(
        r"\b(download|grab|snatch|get me)\b.+\b(movie|film|show|series|season)\b",
        raw,
        re.I,
    ) and not re.search(
        r"\b("
        r"another\s+source|new\s+one|new\s+version|another\s+(?:version|release|copy|download)|"
        r"didn'?t\s+work|failed|stalled|retry|stuck|"
        r"too\s+big|won'?t\s+play|smaller|"
        r"already\s+(?:there|in)|don'?t\s+delete|find\s+another|keep\s+(?:the\s+)?old"
        r")\b",
        raw,
        re.I,
    ):
        return None

    title = ""
    match = _DOWNLOAD_RETRY_TITLE.search(raw)
    if match:
        title = _play_title_clean(match.group(1))
    if not title:
        # Prefer an explicit "for/on Title" clause over "this download didn't…".
        named = re.search(
            r"\b(?:for|on)\s+([A-Za-zÀ-ÿ0-9][\w'’.:\- ]{1,60})[.?!]*$",
            raw,
            re.I,
        ) or re.search(
            r"\bretry(?:\s+the)?(?:\s+download)?(?:\s+(?:of|for))?\s+(.+?)[.?!]*$",
            raw,
            re.I,
        ) or re.search(
            r"^(.+?)\s+(?:didn'?t|doesn'?t)\s+work\b",
            raw,
            re.I,
        ) or re.search(
            r"^(.+?)\s+is\s+too\s+big\b",
            raw,
            re.I,
        ) or re.search(
            r"^(.+?)\s+won'?t\s+play\b",
            raw,
            re.I,
        ) or re.search(
            r"^(.+?)\s+has\s+no\s+(?:usable\s+)?file\b",
            raw,
            re.I,
        ) or re.search(
            r"^(.+?)\s+(?:is\s+)?missing\s+(?:the\s+)?file\b",
            raw,
            re.I,
        ) or re.search(
            r"(?:download|grab|get)\s+(.+?)\s*[?.!]?\s*"
            r"(?:it'?s\s+)?already\s+(?:there|in)",
            raw,
            re.I,
        ) or re.search(
            r"(?:download|grab|get)\s+(.+?)(?:\s*[?.!].*)?$",
            raw,
            re.I,
        )
        if named:
            title = _play_title_clean(named.group(1))
    title = re.sub(
        r"\b(the )?(movie|film|show|series|episode|download|source|one|it|that|this|"
        r"version|release|copy|file)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip(" .?!'\"")
    # Drop a leftover leading article ("the Event Horizon" → "Event Horizon").
    title = re.sub(r"^(?:the|a|an)\s+", "", title, flags=re.I).strip()
    if title.lower() in {
        "",
        "it",
        "that",
        "this",
        "something",
        "everything",
        "anything",
        "download",
        "one",
        "can you",
        "could you",
    }:
        title = ""
    if not title:
        return None

    tool = "sonarr_retry" if _SERIES.search(raw) and not _MOVIE.search(raw) else "radarr_retry"
    from hearth.tools.arr import want_keep_existing

    args: dict[str, Any] = {"query": title}
    if want_keep_existing(raw):
        args["keep_existing"] = True
    return {"tool": tool, "args": args}


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
    if "apple" in name or name in {"atv", "appletv"}:
        return "Apple TV"
    if "lg" in name or "webos" in name:
        return "LG"
    if "living" in name:
        return "living room"
    if "shield" in name:
        return "Shield"
    return phrase.strip() if phrase else "tv"


def _play_reference_plan(target: str) -> dict[str, Any] | None:
    """Resolve “put it on the TV” from the active server-side media card."""
    widget = runtime.get_widget("media")
    if widget is None:
        return None
    data = widget.data if isinstance(widget.data, dict) else {}
    active = data.get("item") if isinstance(data.get("item"), dict) else None
    items = [row for row in data.get("items") or [] if isinstance(row, dict)]
    active_id = str(data.get("active_id") or "")
    if active_id:
        active = next(
            (row for row in items if str(row.get("id") or "") == active_id),
            active,
        )
    if not active:
        return None
    title = str(active.get("title") or "").strip()
    if not title:
        return None
    player = _plex_player_hint(target)
    args: dict[str, Any] = {"query": title}
    rating_key = active.get("ratingKey")
    tmdb_id = active.get("tmdbId")
    if rating_key is not None and str(rating_key).strip():
        args["ratingKey"] = str(rating_key)
    if tmdb_id is not None:
        args["tmdbId"] = tmdb_id

    from hearth.tools.infuse import prefer_infuse_for_apple_tv

    if prefer_infuse_for_apple_tv(player) or player.lower() in {"infuse", "firecore"}:
        return {"tool": "infuse_play", "args": args}
    args.pop("tmdbId", None)
    args["player"] = player
    return {"tool": "plex_play", "args": args}


def _infuse_transport_plan(raw: str) -> dict[str, Any] | None:
    match = _INFUSE_TRANSPORT.search(raw)
    if match:
        # Action may be in group 1 or 2 depending on word order.
        action_raw = (match.group(1) or match.group(2) or "").strip().lower()
    else:
        bare = _BARE_TRANSPORT.match(raw)
        if not bare:
            return None
        action_raw = bare.group(1).strip().lower()
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
    if (
        action == "play"
        and _PLAY_TITLE.search(raw)
        and "apple" not in raw.lower()
        and "infuse" not in raw.lower()
    ):
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
    apple_tv_names = {"apple tv", "apple_tv", "appletv", "atv", "living room apple tv"}
    media_tokens = (
        "tv",
        "lg",
        "webos",
        "television",
        "avr",
        "denon",
        "receiver",
        "amp",
        "atv",
        "appletv",
    )
    if any(token in cleaned.split() or cleaned == token for token in media_tokens) or cleaned in {
        "lg tv", "webos tv", "lg webos tv", "denon avr", *apple_tv_names,
    }:
        if cleaned in apple_tv_names or "apple tv" in cleaned:
            device = "apple_tv"
        elif any(t in cleaned for t in ("tv", "lg", "webos", "television")):
            device = "tv"
        else:
            device = "avr"
        return {
            "tool": "ha_media_control",
            "args": {"device": device, "action": "turn_on" if on else "turn_off"},
        }
    if re.search(
        r"\b(cover|blind|blinds|shade|shades|curtain|curtains|shutter|shutters)\b",
        cleaned,
    ):
        return {
            "tool": "ha_device_control",
            "args": {
                "device": cleaned,
                "domain": "cover",
                "action": "open" if on else "close",
            },
        }
    return {
        "tool": "ha_device_control",
        "args": {"device": cleaned, "action": "turn_on" if on else "turn_off"},
    }


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
