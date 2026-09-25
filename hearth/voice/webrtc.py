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
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect

from hearth.agent.prompts import compose_system_prompt, compose_system_prompt_async
from hearth.agent.registry import registry
from hearth.butler.decision import JEV_GATED_TOOL_NAMES, decide_butler_tool, hide_from_llm
from hearth.config import settings
from hearth.jev import adopt_verdict, evaluate_message, log_shadow_outcome, tool_turn
from hearth.memory import store as memory_store
from hearth.runtime import runtime
from hearth.voice.protocol import dumps
from hearth.voice.vad import audio_input_config

CALLS_URL = "https://api.openai.com/v1/realtime/calls"
SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
SIDEBAND_URL = "wss://api.openai.com/v1/realtime"
PATH_ID = "webrtc-ga"
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

    Input ``transcription`` stays on even when the phone hides live captions.
    The text feeds house memory, the Jev ``said`` gate, and the next-turn
    instruction slice — it is not only a UI caption.
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


async def run_house_tool(name: str, args: dict[str, Any], *, said: str = "") -> dict[str, Any]:
    payload = dict(args or {})
    if name == "chief_of_staff":
        payload.setdefault("said", said or json.dumps(payload))
    # Shelf/scene stay Jev-chosen even if a client names them. The registry gate
    # still wraps the call for allow/deny; butler_ask picks which tool may run.
    butler_verdict = None
    if name in JEV_GATED_TOOL_NAMES:
        uttered = (said or runtime.latest_user() or "").strip()
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
    with tool_turn(said, channel="voice"):
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
        self._closing = False
        self._abandoned = False
        self._sideband_reconnects = 0
        self._fail_reason = ""
        self._active_responses = 0
        self._pending_query: str | None = None
        self._latest_user = ""
        self._partial_user = ""
        self._last_assistant = ""

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
        if self._pump is not None and self._pump is not current:
            self._pump.cancel()
            self._pump = None
        await self._discard_socket()
        self._release_runtime()

    def _release_runtime(self) -> None:
        """Clear the live flag only when no other call still owns the path."""
        if runtime.voice_path != PATH_ID:
            return
        others = [cid for cid in _sidebands if cid != self.call_id]
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
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            return

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
                        self._fail_reason = f"sideband dropped: {exc}"[:300]
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
            await self._on_event(event)

    async def _reopen_socket(self) -> bool:
        await self._discard_socket()
        try:
            await self._connect_socket()
            instructions = voice_instructions(self._latest_user or runtime.latest_user())
            assert self._ws is not None
            await self._ws.send(dumps(instructions_update(instructions, include_tools=True)))
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
        self._closing = True
        await self._discard_socket()
        current = _sidebands.get(self.call_id)
        if current is self:
            _sidebands.pop(self.call_id, None)
        self._release_runtime()

    def _said(self) -> str:
        return (self._latest_user or self._partial_user or runtime.latest_user() or "").strip()

    def _note_assistant(self, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned or cleaned == self._last_assistant:
            return
        self._last_assistant = cleaned
        runtime.note("assistant", cleaned)
        _persist_voice_turn("assistant", cleaned)

    async def _remember_user_final(self, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self._partial_user = ""
        if cleaned == self._latest_user:
            return
        self._latest_user = cleaned[:800]
        runtime.note("user", self._latest_user)
        _persist_voice_turn("user", self._latest_user)
        self._pending_query = self._latest_user
        if self._active_responses == 0 and not self._pending_hangup:
            await self._flush_instructions()

    def _remember_user_delta(self, delta: str) -> None:
        if not delta:
            return
        self._partial_user = f"{self._partial_user}{delta}"[-800:]

    def _begin_response(self) -> None:
        self._active_responses += 1

    def _end_response(self) -> bool:
        """Return True when no Realtime response is still in flight."""
        if self._active_responses <= 0:
            self._active_responses = 0
            return True
        self._active_responses -= 1
        return self._active_responses == 0

    async def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "input_audio_buffer.speech_started":
            runtime.set_status("listening")
        elif etype == "response.created":
            self._begin_response()
            runtime.set_status("thinking")
        elif etype == "response.cancelled":
            if self._end_response() and not self._pending_hangup:
                await self._flush_instructions()
        elif etype in {"response.output_audio.delta", "response.audio.delta"}:
            runtime.set_status("speaking")
        elif etype in _ASSISTANT_TRANSCRIPT_DONE:
            runtime.set_status("speaking")
            self._note_assistant(event.get("transcript") or "")
        elif etype in _USER_TRANSCRIPT_DELTA:
            self._remember_user_delta(str(event.get("delta") or ""))
        elif etype in _USER_TRANSCRIPT_DONE:
            await self._remember_user_final(str(event.get("transcript") or ""))
        elif etype == "response.function_call_arguments.done":
            await self._run_function_call(
                event.get("name") or "",
                event.get("arguments") or "{}",
                event.get("call_id") or "",
            )
        elif etype == "response.done":
            await self._handle_function_calls(event)
            idle = self._end_response()
            runtime.set_status("listening")
            if idle and not self._pending_hangup:
                await self._flush_instructions()
            if self._pending_hangup:
                self._schedule_hangup()
        elif etype == "error":
            err = event.get("error") or event
            runtime.voice_reason = str(err)[:300]
            if is_fatal_realtime_error(err):
                raise SidebandFatal(runtime.voice_reason)

    async def _handle_function_calls(self, event: dict[str, Any]) -> None:
        response = event.get("response") or {}
        output = response.get("output") or []
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            for item in output:
                if item.get("type") == "message":
                    for content in item.get("content") or []:
                        text = content.get("transcript") or content.get("text")
                        if text:
                            self._note_assistant(text)
            return
        for item in calls:
            await self._run_function_call(
                item.get("name") or "",
                item.get("arguments") or "{}",
                item.get("call_id") or "",
            )

    async def _run_function_call(self, name: str, arguments: str, call_id: str) -> None:
        if self._ws is None or not name:
            return
        if call_id and call_id in self._done_calls:
            return
        if call_id:
            self._done_calls.add(call_id)
        runtime.begin_tool(name)
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        result = await run_house_tool(
            name,
            args if isinstance(args, dict) else {},
            said=self._said(),
        )
        await self._ws.send(
            dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, default=str),
                    },
                }
            )
        )
        if name == "end_call":
            # Close after this response finishes so farewell audio can play.
            self._pending_hangup = True
            runtime.voice_reason = f"close_of_call:{result.get('reason', 'close_of_call')}"
            return
        await self._ws.send(dumps({"type": "response.create"}))

    async def _flush_instructions(self, *, include_tools: bool = False) -> None:
        """Push a memory refresh between turns, never over an in-flight response."""
        query = self._pending_query
        if not query or self._ws is None or self._active_responses > 0 or self._pending_hangup:
            return
        self._pending_query = None
        try:
            instructions = await voice_instructions_async(query)
            # A response may have started while memory was loading. Hold the
            # slice for the next idle boundary instead of resetting the turn.
            if (
                self._ws is None
                or self._active_responses > 0
                or self._pending_hangup
                or self._closing
            ):
                if self._pending_query is None:
                    self._pending_query = query
                return
            await self._ws.send(dumps(instructions_update(instructions, include_tools=include_tools)))
        except Exception:  # noqa: BLE001
            if self._pending_query is None:
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
            sideband = f"failed:{exc}"
            await band.close()
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
