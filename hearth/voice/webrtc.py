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


def session_config(*, query: str | None = None, instructions: str | None = None) -> dict[str, Any]:
    """GA session shape. ChatGPT-app voice: gpt-realtime-2.1 + speech-aware VAD.

    ``noise_reduction`` runs before server VAD so TV/HVAC energy is less likely
    to fire ``speech_started``. Client barge-in gate (``/static/vad.js``) adds a
    second speech-band check while the assistant is talking.

    Input transcription supplies Jev's decision context and memory; it is an
    internal input, not the visual presentation. The UI shows grounded results.
    """
    text = instructions or compose_system_prompt(
        query if query is not None else runtime.latest_user(),
        include_recent_turns=True,
    )
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

    async def start(self) -> None:
        url = f"{SIDEBAND_URL}?call_id={self.call_id}"
        self._ws = await ws_connect(url, additional_headers=openai_auth_headers(), open_timeout=8)
        await self._ws.send(dumps({"type": "session.update", "session": session_config()}))
        self._pump = asyncio.create_task(self._listen())
        runtime.voice_mode = "live"
        runtime.voice_path = PATH_ID
        runtime.voice_reason = f"GA WebRTC + sideband {self.call_id[:12]}"
        runtime.openai_live = True
        runtime.set_status("listening")

    async def close(self) -> None:
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
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if runtime.voice_path == PATH_ID and not any(
            band is not self and band._ws is not None for band in _sidebands.values()
        ):
            runtime.voice_mode = "disconnected"
            runtime.openai_live = False
            runtime.set_status("idle")

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
        assert self._ws is not None
        try:
            async for raw in self._ws:
                payload = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    await self._on_event(event)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Voice sideband disconnected: %s", type(exc).__name__)
            runtime.voice_reason = "Voice connection interrupted. Reconnect to continue."
        finally:
            if _sidebands.get(self.call_id) is self:
                _sidebands.pop(self.call_id, None)
            await self.close()

    async def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "input_audio_buffer.speech_started":
            self._generation += 1
            self._utterance = ""
            self._verdict = None
            self._input_item_id = str(event.get("item_id") or "")
            self._writes = {}
            self._tool_count = 0
            self._transcript_ready.clear()
            runtime.set_status("listening")
        elif etype == "response.created":
            response = event.get("response") or {}
            if isinstance(response, dict) and response.get("id"):
                self._response_generations[str(response["id"])] = self._generation
            runtime.set_status("thinking")
        elif etype in {"response.output_audio.delta", "response.audio.delta"}:
            runtime.set_status("speaking")
        elif etype in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            runtime.set_status("speaking")
            text = (event.get("transcript") or "").strip()
            if text:
                self._note_transcript("assistant", text, str(event.get("item_id") or event.get("response_id") or ""))
        elif etype in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.audio_transcription.completed",
        }:
            text = (event.get("transcript") or "").strip()
            if text:
                item_id = str(event.get("item_id") or "")
                if item_id and self._input_item_id and item_id != self._input_item_id:
                    self._note_transcript("user", text, item_id)
                    return
                self._utterance = text
                self._verdict = None
                self._transcript_ready.set()
                self._note_transcript("user", text, str(event.get("item_id") or ""))
                if self._memory_task is not None:
                    self._memory_task.cancel()
                self._memory_task = asyncio.create_task(self._refresh_memory(text))
        elif etype == "response.function_call_arguments.done":
            # Arguments may complete on a response that is subsequently
            # cancelled. Execute only the final completed response's output.
            return
        elif etype == "response.done":
            response = event.get("response") or {}
            if not isinstance(response, dict):
                return
            response_id = str(response.get("id") or "")
            if response_id and response_id in self._done_responses:
                return
            if response_id:
                self._done_responses.add(response_id)
            if response.get("status", "completed") != "completed":
                runtime.set_status("listening")
                return
            generation = self._response_generations.pop(response_id, self._generation)
            job = asyncio.create_task(self._finish_response(event, generation, self._utterance))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)
        elif etype == "error":
            err = event.get("error") or event
            runtime.voice_reason = str(err)[:300]

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
            utterance = (said or self._utterance).strip()
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
        if self._ws is None:
            return
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
        if name == "end_call" and result.get("ok"):
            # Close after this response finishes so farewell audio can play.
            self._pending_hangup = True
            runtime.voice_reason = f"close_of_call:{result.get('reason', 'close_of_call')}"
            return
        if resume:
            await self._ws.send(dumps({"type": "response.create"}))

    async def _refresh_memory(self, query: str) -> None:
        """Re-inject a retrieved memory slice after each spoken turn (Realtime hook)."""
        if self._ws is None:
            return
        try:
            async with asyncio.timeout(5.0):
                instructions = await compose_system_prompt_async(query, include_recent_turns=True)
            if self._ws is None or query != self._utterance:
                return
            await self._ws.send(
                dumps({"type": "session.update", "session": session_config(instructions=instructions)})
            )
        except Exception:  # noqa: BLE001
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
