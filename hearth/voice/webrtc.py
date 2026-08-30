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
from hearth.config import settings
from hearth.memory import store as memory_store
from hearth.runtime import runtime
from hearth.voice.protocol import dumps
from hearth.voice.vad import audio_input_config

CALLS_URL = "https://api.openai.com/v1/realtime/calls"
SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
SIDEBAND_URL = "wss://api.openai.com/v1/realtime"
PATH_ID = "webrtc-ga"


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

    Input ``transcription`` is required for
    ``conversation.item.input_audio_transcription.completed`` so the phone UI
    can show what the user said when Conversation is expanded. Spoken turns also
    refresh the memory slice injected into Realtime ``instructions``.
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
            "input": audio_input_config(),            "output": {
                "voice": settings.openai_tts_voice,
            },
        },
        "tools": registry.openai_realtime_tools(),
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
    result = await registry.call(name, payload)
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
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if runtime.voice_path == PATH_ID:
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
                await self._on_event(event)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return

    async def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "input_audio_buffer.speech_started":
            runtime.set_status("listening")
        elif etype == "response.created":
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
                runtime.note("assistant", text)
                _persist_voice_turn("assistant", text)
        elif etype in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.audio_transcription.completed",
        }:
            text = (event.get("transcript") or "").strip()
            if text:
                runtime.note("user", text)
                _persist_voice_turn("user", text)
                await self._refresh_memory(text)
        elif etype == "response.function_call_arguments.done":
            await self._run_function_call(
                event.get("name") or "",
                event.get("arguments") or "{}",
                event.get("call_id") or "",
            )
        elif etype == "response.done":
            await self._handle_function_calls(event)
            runtime.set_status("listening")
            if self._pending_hangup:
                self._schedule_hangup()
        elif etype == "error":
            err = event.get("error") or event
            runtime.voice_reason = str(err)[:300]

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
                            runtime.note("assistant", text)
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
        result = await run_house_tool(name, args if isinstance(args, dict) else {})
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

    async def _refresh_memory(self, query: str) -> None:
        """Re-inject a retrieved memory slice after each spoken turn (Realtime hook)."""
        if self._ws is None:
            return
        try:
            instructions = await compose_system_prompt_async(query, include_recent_turns=True)
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
            sideband = f"failed:{exc}"
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
