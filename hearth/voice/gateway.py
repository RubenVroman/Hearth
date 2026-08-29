"""Voice front door.

Live path: WebSocket proxy to OpenAI Realtime, with local tool execution.
Fallback path: same client protocol, text (and optional Whisper/TTS) via the agent loop.

Client events:
  session.start
  input_text {text, confirm?}
  input_audio.append {audio}   # base64 pcm16le 24kHz
  input_audio.commit
  response.cancel
  confirm                      # execute pending destructive tool

Server events:
  session.ready {mode: live|fallback, reason?}
  transcript.user {text}
  transcript.assistant {text, final?}
  audio.delta {audio, format}
  tool.call / tool.result
  status {agent}
  error {message}
  openai {event}               # passthrough of Realtime server events (live only)
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

from hearth.agent.loop import AgentLoop
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.runtime import runtime
from hearth.voice.protocol import dumps, pcm16_to_wav, session_update_payload

REALTIME_URL = "wss://api.openai.com/v1/realtime"


class VoiceSession:
    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.agent = AgentLoop()
        self.audio_buf = bytearray()
        self.mode = "fallback"
        self._openai = None
        self._pump: asyncio.Task[None] | None = None

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send_text(dumps(event))

    async def run(self) -> None:
        await self.ws.accept()
        live, reason = await self._maybe_connect_live()
        self.mode = "live" if live else "fallback"
        runtime.voice_mode = self.mode  # type: ignore[assignment]
        runtime.voice_reason = reason
        runtime.openai_live = live
        runtime.agent_status = "listening"
        await self.send(
            {
                "type": "session.ready",
                "mode": self.mode,
                "reason": reason,
                "sample_rate": 24000,
                "audio_format": "pcm16",
                "tools": registry.names(),
            }
        )
        try:
            while True:
                message = await self.ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    await self.send({"type": "error", "message": "invalid json"})
                    continue
                await self._on_client(event)
        except WebSocketDisconnect:
            pass
        finally:
            await self._close_live()
            runtime.voice_mode = "disconnected"
            runtime.openai_live = False
            runtime.agent_status = "idle"

    async def _maybe_connect_live(self) -> tuple[bool, str]:
        if not settings.openai_configured:
            return False, "OPENAI_API_KEY unset — text fallback. Same protocol; plug in a key for live voice."
        url = f"{REALTIME_URL}?model={settings.openai_realtime_model}"
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        try:
            self._openai = await ws_connect(url, additional_headers=headers, open_timeout=8)
        except Exception as exc:  # noqa: BLE001
            return False, f"Realtime connect failed ({exc}). Fallback active."
        await self._openai.send(dumps(session_update_payload(registry.openai_realtime_tools())))
        self._pump = asyncio.create_task(self._pump_openai())
        return True, "OpenAI Realtime connected. Tools execute on Hearth."

    async def _close_live(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self._openai is not None:
            try:
                await self._openai.close()
            except Exception:  # noqa: BLE001
                pass
            self._openai = None

    async def _on_client(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype in {"session.start", "ping"}:
            await self.send({"type": "pong", "mode": self.mode})
            return
        if etype == "confirm":
            await self._fallback_turn("", confirm=True)
            return
        if etype == "input_text":
            text = str(event.get("text") or "")
            confirm = bool(event.get("confirm"))
            if self.mode == "live" and self._openai is not None and not confirm:
                await self._openai.send(
                    dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": text}],
                            },
                        }
                    )
                )
                await self._openai.send(dumps({"type": "response.create"}))
                runtime.note("user", text)
                await self.send({"type": "transcript.user", "text": text})
                return
            await self._fallback_turn(text, confirm=confirm)
            return
        if etype == "input_audio.append":
            chunk = event.get("audio") or ""
            try:
                self.audio_buf.extend(base64.b64decode(chunk))
            except Exception:  # noqa: BLE001
                await self.send({"type": "error", "message": "bad audio chunk"})
                return
            if self.mode == "live" and self._openai is not None:
                await self._openai.send(dumps({"type": "input_audio_buffer.append", "audio": chunk}))
            return
        if etype == "input_audio.commit":
            if self.mode == "live" and self._openai is not None:
                await self._openai.send(dumps({"type": "input_audio_buffer.commit"}))
                await self._openai.send(dumps({"type": "response.create"}))
                self.audio_buf.clear()
                return
            pcm = bytes(self.audio_buf)
            self.audio_buf.clear()
            text = await self._transcribe(pcm)
            if not text:
                await self.send(
                    {
                        "type": "error",
                        "message": "No transcript. In fallback, send input_text or set OPENAI_API_KEY for Whisper.",
                    }
                )
                return
            await self._fallback_turn(text)
            return
        if etype == "response.cancel":
            if self._openai is not None:
                await self._openai.send(dumps({"type": "response.cancel"}))
            return
        # Unknown events: in live mode, forward to OpenAI so the protocol stays complete.
        if self.mode == "live" and self._openai is not None and etype:
            await self._openai.send(dumps(event))

    async def _fallback_turn(self, text: str, *, confirm: bool = False) -> None:
        runtime.agent_status = "thinking"
        await self.send({"type": "status", "agent": "thinking"})
        if text:
            await self.send({"type": "transcript.user", "text": text})
        result = await self.agent.run(text or "(confirm)", confirm=confirm)
        for tool in result.get("tools") or []:
            await self.send({"type": "tool.result", "name": tool.get("name"), "result": tool})
        reply = result.get("reply") or ""
        await self.send({"type": "transcript.assistant", "text": reply, "final": True})
        audio = await self._speak(reply)
        if audio:
            await self.send(
                {
                    "type": "audio.delta",
                    "audio": base64.b64encode(audio).decode("ascii"),
                    "format": "pcm16",
                    "sample_rate": 24000,
                    "final": True,
                }
            )
        runtime.agent_status = "idle"
        await self.send({"type": "status", "agent": "idle"})

    async def _transcribe(self, pcm: bytes) -> str:
        if not pcm or not settings.openai_configured:
            return ""
        try:
            from openai import AsyncOpenAI

            wav = pcm16_to_wav(pcm)
            client = AsyncOpenAI(api_key=settings.openai_api_key)
            file = ("speech.wav", wav, "audio/wav")
            out = await client.audio.transcriptions.create(
                model=settings.openai_transcribe_model,
                file=file,
            )
            return (getattr(out, "text", None) or str(out)).strip()
        except Exception:  # noqa: BLE001
            return ""

    async def _speak(self, text: str) -> bytes:
        if not text or not settings.openai_configured:
            return b""
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=settings.openai_api_key)
            speech = await client.audio.speech.create(
                model=settings.openai_tts_model,
                voice=settings.openai_tts_voice,
                input=text,
                response_format="pcm",
            )
            return speech.content if hasattr(speech, "content") else b""
        except Exception:  # noqa: BLE001
            return b""

    async def _pump_openai(self) -> None:
        assert self._openai is not None
        try:
            async for raw in self._openai:
                payload = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                await self._on_openai(event)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            await self.send({"type": "error", "message": f"Realtime stream ended: {exc}"})
            self.mode = "fallback"
            runtime.voice_mode = "fallback"
            runtime.voice_reason = str(exc)

    async def _on_openai(self, event: dict[str, Any]) -> None:
        await self.send({"type": "openai", "event": event})
        etype = event.get("type")
        if etype == "input_audio_buffer.speech_started":
            runtime.agent_status = "listening"
            await self.send({"type": "status", "agent": "listening"})
        elif etype == "response.created":
            runtime.agent_status = "thinking"
            await self.send({"type": "status", "agent": "thinking"})
        elif etype in {"response.output_audio.delta", "response.audio.delta"}:
            delta = event.get("delta") or ""
            if delta:
                runtime.agent_status = "speaking"
                await self.send(
                    {
                        "type": "audio.delta",
                        "audio": delta,
                        "format": "pcm16",
                        "sample_rate": 24000,
                    }
                )
        elif etype in {
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            await self.send(
                {
                    "type": "transcript.assistant",
                    "text": event.get("delta") or "",
                    "final": False,
                }
            )
        elif etype in {
            "conversation.item.input_audio_transcription.completed",
        }:
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                runtime.note("user", transcript)
                await self.send({"type": "transcript.user", "text": transcript})
        elif etype == "response.done":
            await self._handle_function_calls(event)
            runtime.agent_status = "listening"
            await self.send({"type": "status", "agent": "idle"})
        elif etype == "error":
            err = event.get("error") or event
            await self.send({"type": "error", "message": str(err)})

    async def _handle_function_calls(self, event: dict[str, Any]) -> None:
        response = event.get("response") or {}
        output = response.get("output") or []
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls or self._openai is None:
            # Capture final transcript if present.
            for item in output:
                if item.get("type") == "message":
                    for content in item.get("content") or []:
                        text = content.get("transcript") or content.get("text")
                        if text:
                            runtime.note("assistant", text)
                            await self.send(
                                {"type": "transcript.assistant", "text": text, "final": True}
                            )
            return
        runtime.agent_status = "tool"
        for item in calls:
            name = item.get("name") or ""
            call_id = item.get("call_id") or ""
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            await self.send({"type": "tool.call", "name": name, "args": args})
            result = await registry.call(name, args if isinstance(args, dict) else {})
            await self.send({"type": "tool.result", "name": name, "result": result.as_dict()})
            await self._openai.send(
                dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result.as_dict(), default=str),
                        },
                    }
                )
            )
        await self._openai.send(dumps({"type": "response.create"}))


async def voice_socket(websocket: WebSocket) -> None:
    await VoiceSession(websocket).run()
