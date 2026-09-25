"""GA OpenAI Realtime over WebRTC (ChatGPT-app voice).

Never use ``OpenAI-Beta: realtime=v1`` — that shape is disabled
(``beta_api_shape_disabled`` / close 4000).

Live path:
  Browser mic → RTCPeerConnection (AEC, barge-in)
  POST SDP to Hearth ``/api/realtime/calls``
  Hearth POSTs multipart to ``https://api.openai.com/v1/realtime/calls``
  Sideband ``wss://api.openai.com/v1/realtime?call_id=…`` (no beta header)
  runs house tools on Hearth.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import nullcontext
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect

from hearth.agent.prompts import compose_system_prompt, compose_system_prompt_async
from hearth.agent.registry import registry
from hearth.butler.decision import JEV_GATED_TOOL_NAMES, decide_butler_tool, hide_from_llm
from hearth.config import settings
from hearth.jev import adopt_verdict, current_turn, evaluate_message, is_write_tool, log_shadow_outcome, tool_turn
from hearth.memory import store as memory_store
from hearth.runtime import runtime
from hearth.voice.protocol import dumps
from hearth.voice.vad import audio_input_config

CALLS_URL = "https://api.openai.com/v1/realtime/calls"
SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
SIDEBAND_URL = "wss://api.openai.com/v1/realtime"
PATH_ID = "webrtc-ga"
log = logging.getLogger("hearth.voice")
TOOL_TIMEOUT_SECONDS = 60.0
_executions: dict[tuple[str, str], tuple[str, asyncio.Task[dict[str, Any]], float]] = {}
# One transparent sideband reopen per call. A second drop ends the server session
# so the client can place a fresh call instead of looping on a dead socket.
MAX_SIDEBAND_RECONNECTS = 1
# Chat injects 4 turns because the agent loop already carries history.
# Voice sessions are recreated on reconnect, so the instruction slice is the memory.
VOICE_RECENT_TURNS = 8
VOICE_TURN_ADDENDUM = """## Live voice
You are on a spoken call. One or two short sentences, then stop so the other person can talk.
Do not monologue or read lists aloud unless asked. Resolve "it", "that", and "the other one" from this call and the recent turns below.
When a house tool returns a speak line, say that outcome — do not invent a different one.
If they interrupt, drop the rest of the sentence.
"""
_FATAL_ERROR_TOKENS = (
    "session_expired",
    "call_id_not_found",
    "call_ended",
    "invalid_call_id",
)
_USER_TRANSCRIPT_DONE = {
    "conversation.item.input_audio_transcription.completed",
    "conversation.item.audio_transcription.completed",
}
_USER_TRANSCRIPT_DELTA = {
    "conversation.item.input_audio_transcription.delta",
    "conversation.item.audio_transcription.delta",
}
_ASSISTANT_TRANSCRIPT_DONE = {
    "response.output_audio_transcript.done",
    "response.audio_transcript.done",
}


def safety_identifier() -> str:
    raw = f"hearth:{settings.owner}:{settings.house_name}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def openai_auth_headers(*, json_body: bool = False) -> dict[str, str]:
    """Standard GA auth. Do not add OpenAI-Beta — that API shape is disabled."""
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "OpenAI-Safety-Identifier": safety_identifier(),
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def is_fatal_realtime_error(err: Any) -> bool:
    """True when the Realtime call itself is gone and retrying the socket will not help."""
    try:
        blob = err if isinstance(err, str) else json.dumps(err, default=str)
    except Exception:  # noqa: BLE001
        blob = str(err)
    lowered = blob.lower()
    return any(token in lowered for token in _FATAL_ERROR_TOKENS)


def voice_instructions(query: str | None = None, *, instructions: str | None = None) -> str:
    """System prompt plus a short spoken-call addendum and a wider recent-turn slice."""
    text = instructions
    if text is None:
        text = compose_system_prompt(
            query if query is not None else runtime.latest_user(),
            include_recent_turns=True,
            turn_limit=VOICE_RECENT_TURNS,
        )
    addendum = VOICE_TURN_ADDENDUM.strip()
    if addendum not in text:
        text = f"{text}\n\n{addendum}"
    return text


async def voice_instructions_async(query: str) -> str:
    text = await compose_system_prompt_async(
        query,
        include_recent_turns=True,
        turn_limit=VOICE_RECENT_TURNS,
    )
    return voice_instructions(instructions=text)


def instructions_update(instructions: str, *, include_tools: bool = False) -> dict[str, Any]:
    """Mid-call session.update that does not resend VAD / noise-reduction.

    Resending ``audio.input`` between turns restarts server turn detection and
    drops the utterance in flight. Tools are included only when the sideband
    socket itself was replaced.
    """
    session: dict[str, Any] = {"type": "realtime", "instructions": instructions}
    if include_tools:
        session["tools"] = hide_from_llm(registry.openai_realtime_tools())
        session["tool_choice"] = "auto"
    return {"type": "session.update", "session": session}


class SidebandFatal(Exception):
    """Realtime error that means this call_id is finished."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def session_config(*, query: str | None = None, instructions: str | None = None) -> dict[str, Any]:
    """GA session shape. ChatGPT-app voice: gpt-realtime-2.1 + speech-aware VAD.

    ``noise_reduction`` runs before server VAD so TV/HVAC energy is less likely
    to fire ``speech_started``. Client barge-in gate (``/static/vad.js``) adds a
    second speech-band check while the assistant is talking.

    Input transcription supplies Jev's decision context and memory; it is an
    internal input, not the visual presentation. The UI shows grounded results.
    """
    if instructions is not None:
        text = voice_instructions(instructions=instructions)
    else:
        text = voice_instructions(query if query is not None else runtime.latest_user())
    return {
        "type": "realtime",
        "model": settings.openai_realtime_model,
        "instructions": text,
        "output_modalities": ["audio"],
        "audio": {
            "input": audio_input_config(),
            "output": {
                "voice": settings.openai_tts_voice,
            },
        },
        "tools": hide_from_llm(registry.openai_realtime_tools()),
        "tool_choice": "auto",
    }


def secret_value(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("value")
    if isinstance(value, str) and value.startswith("ek_"):
        return value
    nested = data.get("client_secret")
    if isinstance(nested, dict):
        inner = nested.get("value")
        if isinstance(inner, str) and inner:
            return inner
    if isinstance(value, str) and value:
        return value
    return None


async def run_house_tool(
    name: str,
    args: dict[str, Any],
    *,
    said: str = "",
    execution_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Deduplicate retried voice delivery without replaying a house action."""
    scope = current_turn()
    utterance = said or (scope.said if scope is not None else "")
    if execution_id and is_write_tool(name) and not utterance.strip():
        return {"ok": False, "name": name, "data": {
            "error": "missing_current_utterance",
            "speak": "I couldn't verify what you asked, so I haven't run that action. Please say it again.",
        }}
    if not execution_id:
        return await _execute_house_tool(name, args, said=said)
    now = time.monotonic()
    for key, (_, task, started) in list(_executions.items()):
        if task.done() and (now - started > 600 or len(_executions) >= 512):
            _executions.pop(key, None)
    key = (session_id, execution_id)
    fingerprint = name + ":" + json.dumps(args, sort_keys=True, default=str)
    previous = _executions.get(key)
    if previous is not None:
        if previous[0] != fingerprint:
            return {"ok": False, "name": name, "data": {"error": "execution_id_conflict"}}
        return await asyncio.shield(previous[1])
    if len(_executions) >= 512:
        return {"ok": False, "name": name, "data": {"error": "voice_tools_busy"}}
    task = asyncio.create_task(_execute_house_tool(name, args, said=said))
    _executions[key] = (fingerprint, task, now)
    # Client disconnects may interrupt the wait, not an action already in flight.
    return await asyncio.shield(task)


async def _execute_house_tool(name: str, args: dict[str, Any], *, said: str) -> dict[str, Any]:
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            return await _execute_house_tool_gated(name, args, said=said)
    except TimeoutError:
        return {"ok": False, "name": name, "data": {"error": "tool_timeout", "uncertain": True,
            "speak": "That took too long. I couldn't verify its outcome; check its status before retrying."}}
    except Exception as exc:  # noqa: BLE001
        log.warning("Voice tool %s failed: %s", name, type(exc).__name__)
        return {"ok": False, "name": name, "data": {"error": "tool_failed", "uncertain": True,
            "speak": "I couldn't verify that action's outcome. Check its status before retrying."}}


async def _execute_house_tool_gated(name: str, args: dict[str, Any], *, said: str) -> dict[str, Any]:
    payload = dict(args or {})
    if name == "chief_of_staff":
        payload.setdefault("said", said or json.dumps(payload))
    # Shelf/scene stay Jev-chosen even if a client names them. The registry gate
    # still wraps the call for allow/deny; butler_ask picks which tool may run.
    butler_verdict = None
    if name in JEV_GATED_TOOL_NAMES:
        uttered = (said or "").strip()
        scope = current_turn()
        butler_verdict = scope.verdict if scope is not None else None
        if butler_verdict is None:
            butler_verdict = await evaluate_message(uttered or name)
        decision = decide_butler_tool(uttered, butler_verdict)
        log_shadow_outcome(
            butler_verdict,
            channel="voice",
            tools=[decision.tool] if decision.run else [],
            outcome=decision.source,
        )
        if not decision.run or decision.tool != name:
            return {
                "ok": False,
                "name": name,
                "speak": "Jev didn't clear that, so I left it alone.",
                "jev": butler_verdict.as_log_dict(),
            }
        payload = decision.as_args()
    # Voice tool calls pass the same Jev gate as chat and Telegram. Without a
    # transcript there is no state to gate on, and the gate fails open.
    with (nullcontext() if current_turn() is not None else tool_turn(said, channel="voice")):
        if butler_verdict is not None:
            adopt_verdict(butler_verdict)
        result = await registry.call(name, payload, said=said)
    return result.as_dict()


def _persist_voice_turn(role: str, text: str) -> None:
    try:
        memory_store.persist_turn(role, text, channel="voice")
    except Exception:  # noqa: BLE001
        return


def client_secret_body() -> dict[str, Any]:
    return {"session": session_config()}


class Sideband:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self._ws = None
        self._pump: asyncio.Task[None] | None = None
        self._done_calls: set[str] = set()
        self._pending_hangup = False
        self._hangup_task: asyncio.Task[None] | None = None
        self._jobs: set[asyncio.Task[None]] = set()
        self._tool_lock = asyncio.Lock()
        self._memory_task: asyncio.Task[None] | None = None
        self._done_responses: set[str] = set()
        self._transcripts: set[tuple[str, str, str]] = set()
        self._utterance = ""
        self._generation = 0
        self._verdict = None
        self._response_generations: dict[str, int] = {}
        self._input_item_id = ""
        self._writes: dict[str, dict[str, Any]] = {}
        self._tool_count = 0
        self._transcript_ready = asyncio.Event()
        self._transcript_ready.set()
        self._closing = False
        self._abandoned = False
        self._sideband_reconnects = 0
        self._fail_reason = ""
        self._active_responses = 0
        self._pending_query: str | None = None
        self._latest_user = ""
        self._partial_user = ""
        self._active_response_ids: set[str] = set()
        self._ended_response_ids: set[str] = set()
        self._continuation_pending = False
        self._instruction_lock = asyncio.Lock()
        self._socket_ready = asyncio.Event()

    async def start(self) -> None:
        last_exc: Exception | None = None
        for _attempt in range(2):
            try:
                await self._connect_socket()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                await self._discard_socket()
        if last_exc is not None:
            raise last_exc
        assert self._ws is not None
        await self._ws.send(dumps({"type": "session.update", "session": session_config()}))
        self._socket_ready.set()
        self._pump = asyncio.create_task(self._listen())
        runtime.voice_mode = "live"
        runtime.voice_path = PATH_ID
        runtime.voice_reason = f"GA WebRTC + sideband {self.call_id[:12]}"
        runtime.openai_live = True
        runtime.set_status("listening")

    async def close(self) -> None:
        self._closing = True
        self._pending_hangup = False
        current = asyncio.current_task()
        if (
            self._hangup_task is not None
            and not self._hangup_task.done()
            and self._hangup_task is not current
        ):
            self._hangup_task.cancel()
            self._hangup_task = None
        if self._memory_task is not None:
            self._memory_task.cancel()
            self._memory_task = None
        for task in list(self._jobs):
            if task is not current:
                task.cancel()
        if self._pump is not None and self._pump is not current:
            self._pump.cancel()
            self._pump = None
        await self._discard_socket()
        self._release_runtime()

    def _release_runtime(self) -> None:
        """Clear the live flag only when no other call still owns the path."""
        if runtime.voice_path != PATH_ID:
            return
        others = [band for band in _sidebands.values() if band is not self and not band._closing]
        if others:
            return
        runtime.voice_mode = "disconnected"
        runtime.openai_live = False
        if self._fail_reason:
            runtime.voice_reason = self._fail_reason[:300]
        runtime.set_status("idle")

    async def _connect_socket(self) -> None:
        url = f"{SIDEBAND_URL}?call_id={self.call_id}"
        self._ws = await ws_connect(url, additional_headers=openai_auth_headers(), open_timeout=8)

    async def _discard_socket(self) -> None:
        ws = self._ws
        self._ws = None
        self._socket_ready.clear()
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            return

    async def _send_tool_output(self, event: dict[str, Any]) -> bool:
        """Keep a completed action's result through the one permitted reopen."""
        if self._closing:
            return False
        if self._ws is None:
            async with asyncio.timeout(10.0):
                await self._socket_ready.wait()
        ws = self._ws
        if ws is None or self._closing:
            return False
        try:
            await ws.send(dumps(event))
        except Exception:
            if self._closing:
                return False
            if ws is self._ws:
                self._socket_ready.clear()
            # The event pump owns reconnecting; only resend this output on the
            # replacement socket. Never execute the associated tool again.
            async with asyncio.timeout(10.0):
                await self._socket_ready.wait()
            if self._ws is None or self._ws is ws or self._closing:
                raise ConnectionError("sideband could not deliver tool result") from None
            await self._ws.send(dumps(event))
        return True

    def _schedule_hangup(self) -> None:
        """Close this call once the current Realtime response has finished."""
        if self._hangup_task is not None and not self._hangup_task.done():
            return
        self._hangup_task = asyncio.create_task(self._hangup_self())

    async def _hangup_self(self) -> None:
        band = _sidebands.pop(self.call_id, None)
        self._hangup_task = None
        if band is self or band is None:
            await self.close()
        elif band is not None:
            await band.close()

    async def _listen(self) -> None:
        abandon = False
        try:
            while not self._closing:
                try:
                    await self._pump_socket()
                    if self._closing:
                        return
                    # A clean close frame means this call_id is finished.
                    # Network resets raise and take the reconnect path below.
                    self._fail_reason = self._fail_reason or "sideband closed"
                    abandon = True
                    return
                except asyncio.CancelledError:
                    return
                except SidebandFatal as exc:
                    self._fail_reason = (exc.reason or "realtime session ended")[:300]
                    abandon = True
                    return
                except Exception as exc:  # noqa: BLE001
                    if self._closing:
                        return
                    if self._sideband_reconnects >= MAX_SIDEBAND_RECONNECTS:
                        self._fail_reason = f"sideband dropped: {type(exc).__name__}"
                        abandon = True
                        return
                    self._sideband_reconnects += 1
                    runtime.voice_reason = "sideband reconnecting"
                    if not await self._reopen_socket():
                        self._fail_reason = "sideband reconnect failed"
                        abandon = True
                        return
        finally:
            if abandon and not self._closing:
                await self._abandon()

    async def _pump_socket(self) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("sideband missing")
        async for raw in ws:
            if self._closing:
                return
            payload = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                await self._on_event(event)

    async def _reopen_socket(self) -> bool:
        await self._discard_socket()
        try:
            await self._connect_socket()
            instructions = voice_instructions(self._latest_user or self._utterance)
            assert self._ws is not None
            await self._ws.send(dumps(instructions_update(instructions, include_tools=True)))
            # Completion events may have been lost with the old socket. Do not
            # leave memory refreshes waiting forever for an unseen response.
            self._active_responses = 0
            self._active_response_ids.clear()
            self._continuation_pending = False
            self._socket_ready.set()
            runtime.voice_mode = "live"
            runtime.voice_path = PATH_ID
            runtime.openai_live = True
            runtime.voice_reason = f"sideband reconnected {self.call_id[:12]}"
            return True
        except Exception:  # noqa: BLE001
            await self._discard_socket()
            return False

    async def _abandon(self) -> None:
        if self._abandoned:
            return
        self._abandoned = True
        current = _sidebands.get(self.call_id)
        if current is self:
            _sidebands.pop(self.call_id, None)
        await self.close()

    def _said(self) -> str:
        """Current-call context; partial ASR is suitable only for read tools."""
        return (self._utterance or self._latest_user or self._partial_user).strip()

    async def _remember_user_final(self, text: str, *, item_id: str = "") -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if item_id and self._input_item_id and item_id != self._input_item_id:
            self._note_transcript("user", cleaned, item_id)
            return
        self._partial_user = ""
        if cleaned != self._utterance:
            self._verdict = None
        self._utterance = self._latest_user = cleaned[:800]
        self._transcript_ready.set()
        self._note_transcript("user", self._latest_user, item_id)
        self._pending_query = self._latest_user
        if self._active_responses == 0 and not self._pending_hangup:
            self._schedule_instruction_refresh()

    def _remember_user_delta(self, delta: str) -> None:
        if not delta:
            return
        self._partial_user = f"{self._partial_user}{delta}"[-800:]

    def _begin_response(self, response_id: str = "") -> None:
        if response_id:
            if response_id in self._active_response_ids or response_id in self._ended_response_ids:
                return
            self._active_response_ids.add(response_id)
        self._continuation_pending = False
        self._active_responses += 1

    def _end_response(self, response_id: str = "") -> bool:
        """Return True when no Realtime response is still in flight."""
        if response_id:
            if response_id in self._ended_response_ids:
                return self._active_responses == 0
            self._ended_response_ids.add(response_id)
            if response_id not in self._active_response_ids and self._active_response_ids:
                return self._active_responses == 0
            self._active_response_ids.discard(response_id)
        if self._active_responses <= 0:
            self._active_responses = 0
            return True
        self._active_responses -= 1
        return self._active_responses == 0

    async def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "input_audio_buffer.speech_started":
            self._generation += 1
            self._utterance = ""
            self._latest_user = ""
            self._partial_user = ""
            self._pending_query = None
            self._continuation_pending = False
            self._verdict = None
            self._input_item_id = str(event.get("item_id") or "")
            self._writes = {}
            self._tool_count = 0
            self._transcript_ready.clear()
            runtime.set_status("listening")
        elif etype == "response.created":
            response = event.get("response") or {}
            if isinstance(response, dict) and response.get("id") and str(response["id"]) not in self._ended_response_ids:
                self._response_generations[str(response["id"])] = self._generation
            self._begin_response(str(response.get("id") or "") if isinstance(response, dict) else "")
            runtime.set_status("thinking")
        elif etype == "response.cancelled":
            response = event.get("response") or {}
            if not isinstance(response, dict):
                return
            response_id = str(response.get("id") or event.get("response_id") or "")
            if not response_id and len(self._active_response_ids) == 1:
                response_id = next(iter(self._active_response_ids))
            if response_id:
                self._done_responses.add(response_id)
                self._response_generations.pop(response_id, None)
            if self._end_response(response_id) and not self._pending_hangup:
                self._schedule_instruction_refresh()
        elif etype in {"response.output_audio.delta", "response.audio.delta"}:
            runtime.set_status("speaking")
        elif etype in _ASSISTANT_TRANSCRIPT_DONE:
            runtime.set_status("speaking")
            text = (event.get("transcript") or "").strip()
            if text:
                self._note_transcript("assistant", text, str(event.get("item_id") or event.get("response_id") or ""))
        elif etype in _USER_TRANSCRIPT_DELTA:
            item_id = str(event.get("item_id") or "")
            if not (item_id and self._input_item_id and item_id != self._input_item_id):
                self._remember_user_delta(str(event.get("delta") or ""))
        elif etype in _USER_TRANSCRIPT_DONE:
            await self._remember_user_final(
                str(event.get("transcript") or ""), item_id=str(event.get("item_id") or "")
            )
        elif etype == "response.function_call_arguments.done":
            # Arguments may complete on a response that is subsequently
            # cancelled. Execute only the final completed response's output.
            return
        elif etype == "response.done":
            response = event.get("response") or {}
            if not isinstance(response, dict):
                return
            response_id = str(response.get("id") or "")
            if not response_id and len(self._active_response_ids) == 1:
                response_id = next(iter(self._active_response_ids))
            if response_id and response_id in self._done_responses:
                return
            if response_id:
                self._done_responses.add(response_id)
            self._end_response(response_id)
            generation = self._response_generations.pop(response_id, self._generation)
            if response.get("status", "completed") != "completed":
                runtime.set_status("listening")
                self._schedule_instruction_refresh()
                return
            job = asyncio.create_task(self._finish_response(event, generation, self._utterance))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)
        elif etype == "error":
            err = event.get("error") or event
            runtime.voice_reason = str(err)[:300]
            if is_fatal_realtime_error(err):
                raise SidebandFatal(runtime.voice_reason)

    def _note_transcript(self, role: str, text: str, item_id: str) -> None:
        key = (role, item_id or str(self._generation), text)
        if key in self._transcripts:
            return
        self._transcripts.add(key)
        runtime.note(role, text)
        _persist_voice_turn(role, text)

    async def _finish_response(self, event: dict[str, Any], generation: int, said: str) -> None:
        try:
            async with self._tool_lock:
                if generation != self._generation or self._ws is None:
                    return
                if not said and not self._transcript_ready.is_set():
                    # Transcription arrives independently from audio reasoning.
                    # Keep reading events while giving Jev a small chance to
                    # receive this utterance instead of gating an empty string.
                    try:
                        async with asyncio.timeout(1.0):
                            await self._transcript_ready.wait()
                    except TimeoutError:
                        pass
                    if generation != self._generation:
                        return
                    said = self._utterance
                with tool_turn(said, channel="voice") as scope:
                    if self._verdict is not None:
                        adopt_verdict(self._verdict)
                    await self._handle_function_calls(event, generation=generation, said=said)
                    if generation == self._generation:
                        self._verdict = scope.verdict
                if generation != self._generation:
                    return
                if self._pending_hangup:
                    self._schedule_hangup()
                else:
                    await self._flush_instructions()
                    runtime.set_status("listening")
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Voice response failed: %s", type(exc).__name__)
            runtime.voice_reason = "Voice response interrupted. Please try again."
            runtime.set_status("listening")

    async def _handle_function_calls(self, event: dict[str, Any], *, generation: int | None = None, said: str = "") -> None:
        response = event.get("response") or {}
        output = response.get("output") or []
        output = [item for item in output if isinstance(item, dict)]
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            for item in output:
                if item.get("type") == "message":
                    for content in item.get("content") or []:
                        if not isinstance(content, dict):
                            continue
                        text = content.get("transcript") or content.get("text")
                        if text:
                            self._note_transcript("assistant", str(text), str(item.get("id") or response.get("id") or ""))
            return
        for item in calls:
            if generation is not None and generation != self._generation:
                return
            await self._run_function_call(
                item.get("name") or "",
                item.get("arguments") or "{}",
                item.get("call_id") or "",
                said=said,
                resume=False,
            )
            if self._pending_hangup:
                break
        if self._ws is not None and not self._pending_hangup and (generation is None or generation == self._generation):
            followup: dict[str, Any] = {"type": "response.create"}
            if self._tool_count >= 32:
                followup["response"] = {"tool_choice": "none"}
            self._continuation_pending = True
            await self._ws.send(dumps(followup))

    async def _run_function_call(self, name: str, arguments: str, call_id: str, *, said: str = "", resume: bool = True) -> None:
        if self._ws is None or not name or not call_id:
            return
        if call_id and call_id in self._done_calls:
            return
        if call_id:
            self._done_calls.add(call_id)
        self._tool_count += 1
        runtime.begin_tool(name)
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("non-object arguments")
        except (ValueError, TypeError):
            result = {"ok": False, "name": name, "data": {"error": "invalid_arguments", "speak": "The tool arguments were invalid; nothing ran."}}
        else:
            fingerprint = name + ":" + json.dumps(args, sort_keys=True)
            writes = self._writes
            utterance = (said or self._utterance or (self._said() if not is_write_tool(name) else "")).strip()
            if is_write_tool(name) and not utterance:
                result = {"ok": False, "name": name, "data": {
                    "error": "missing_current_utterance",
                    "speak": "I couldn't verify what you asked, so I haven't run that action. Please say it again.",
                }}
            elif is_write_tool(name) and fingerprint in writes:
                result = writes[fingerprint]
            elif self._tool_count > 32:
                result = {"ok": False, "name": name, "data": {"error": "tool_limit", "speak": "I stopped the tool loop. Summarize the results already returned."}}
            else:
                result = await run_house_tool(name, args, said=utterance, execution_id=call_id, session_id=self.call_id)
                if is_write_tool(name):
                    writes[fingerprint] = result
        delivered = await self._send_tool_output({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, default=str),
            },
        })
        if not delivered:
            return
        if name == "end_call" and result.get("ok"):
            # Close after this response finishes so farewell audio can play.
            self._pending_hangup = True
            runtime.voice_reason = f"close_of_call:{result.get('reason', 'close_of_call')}"
            return
        if resume:
            self._continuation_pending = True
            await self._ws.send(dumps({"type": "response.create"}))

    def _schedule_instruction_refresh(self) -> None:
        if self._memory_task is None or self._memory_task.done():
            self._memory_task = asyncio.create_task(self._flush_instructions())

    async def _flush_instructions(self, *, include_tools: bool = False) -> None:
        """Push a memory refresh between turns, never over an in-flight response."""
        async with self._instruction_lock:
            await self._flush_instructions_locked(include_tools=include_tools)

    async def _flush_instructions_locked(self, *, include_tools: bool = False) -> None:
        query = self._pending_query
        if not query or self._ws is None or self._active_responses > 0 or self._pending_hangup or self._continuation_pending:
            return
        self._pending_query = None
        generation = self._generation
        try:
            async with asyncio.timeout(5.0):
                instructions = await voice_instructions_async(query)
            # A response may have started while memory was loading. Hold the
            # slice for the next idle boundary instead of resetting the turn.
            if (
                self._ws is None
                or self._active_responses > 0
                or self._pending_hangup
                or self._closing
                or self._continuation_pending
                or generation != self._generation
                or (self._pending_query is not None and self._pending_query != query)
            ):
                if self._pending_query is None and generation == self._generation:
                    self._pending_query = query
                return
            await self._ws.send(dumps(instructions_update(instructions, include_tools=include_tools)))
        except Exception:  # noqa: BLE001
            if self._pending_query is None and generation == self._generation:
                self._pending_query = query
            return


_sidebands: dict[str, Sideband] = {}


async def mint_client_secret() -> dict[str, Any]:
    """POST /v1/realtime/client_secrets — ephemeral ek_ token for browser WebRTC."""
    if not settings.openai_configured:
        return {
            "ok": False,
            "configured": False,
            "path": PATH_ID,
            "error": "OPENAI_API_KEY unset",
        }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            SECRETS_URL,
            headers=openai_auth_headers(json_body=True),
            json=client_secret_body(),
        )
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        data = {}
    if not response.is_success:
        message = data.get("error", {}).get("message") if isinstance(data, dict) else None
        return {
            "ok": False,
            "configured": True,
            "path": PATH_ID,
            "status_code": response.status_code,
            "error": message or f"client_secrets {response.status_code}",
        }
    value = secret_value(data)
    if not value:
        return {
            "ok": False,
            "configured": True,
            "path": PATH_ID,
            "error": "client_secrets response missing ephemeral value",
        }
    expires_at = data.get("expires_at") if isinstance(data, dict) else None
    if isinstance(data, dict) and isinstance(data.get("client_secret"), dict):
        expires_at = data["client_secret"].get("expires_at", expires_at)
    return {
        "ok": True,
        "configured": True,
        "path": PATH_ID,
        "model": settings.openai_realtime_model,
        "value": value,
        "expires_at": expires_at,
        "beta": False,
    }


async def create_call(sdp: str) -> dict[str, Any]:
    """Unified interface: server-side POST /v1/realtime/calls with SDP + session."""
    if not settings.openai_configured:
        return {
            "ok": False,
            "configured": False,
            "path": PATH_ID,
            "error": "OPENAI_API_KEY unset",
        }
    session = json.dumps(session_config())
    files = {
        "sdp": (None, sdp),
        "session": (None, session),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            CALLS_URL,
            headers=openai_auth_headers(),
            files=files,
        )
    if not response.is_success:
        try:
            err = response.json()
        except Exception:  # noqa: BLE001
            err = {"error": response.text[:400]}
        message = ""
        if isinstance(err, dict):
            nested = err.get("error")
            if isinstance(nested, dict):
                message = str(nested.get("message") or nested)
            else:
                message = str(nested or err)
        return {
            "ok": False,
            "configured": True,
            "path": PATH_ID,
            "status_code": response.status_code,
            "error": message or f"realtime/calls {response.status_code}",
        }

    location = response.headers.get("Location") or response.headers.get("location") or ""
    call_id = location.rstrip("/").split("/")[-1] if location else ""
    if call_id:
        band = Sideband(call_id)
        try:
            await band.start()
            _sidebands[call_id] = band
            sideband = "ok"
        except Exception as exc:  # noqa: BLE001
            await band.close()
            sideband = f"failed:{type(exc).__name__}"
    else:
        sideband = "no-call-id"
        runtime.voice_mode = "live"
        runtime.voice_path = PATH_ID
        runtime.openai_live = True
        runtime.set_status("listening")

    return {
        "ok": True,
        "path": PATH_ID,
        "model": settings.openai_realtime_model,
        "call_id": call_id,
        "sdp": response.text,
        "sideband": sideband,
        "beta": False,
    }


async def hangup(call_id: str) -> None:
    band = _sidebands.pop(call_id, None)
    if band is not None:
        await band.close()
