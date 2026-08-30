"""Text fallback voice socket. Live speech uses GA WebRTC, not this.

This path never opens ``wss://api.openai.com/v1/realtime?model=`` and never
sends ``OpenAI-Beta: realtime=v1`` (that shape is disabled).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from hearth.agent.loop import AgentLoop
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.runtime import runtime
from hearth.voice.protocol import dumps, pcm16_to_wav


class VoiceSession:
    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.agent = AgentLoop()
        self.audio_buf = bytearray()

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send_text(dumps(event))

    async def run(self) -> None:
        await self.ws.accept()
        runtime.voice_mode = "fallback"
        runtime.voice_path = "text-fallback"
        runtime.voice_reason = (
            "Text fallback. Live voice is GA WebRTC at POST /api/realtime/calls — "
            "not the disabled beta websocket."
        )
        runtime.openai_live = False
        runtime.set_status("idle")
        await self.send(
            {
                "type": "session.ready",
                "mode": "fallback",
                "path": "text-fallback",
                "reason": runtime.voice_reason,
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
            if runtime.voice_path == "text-fallback":
                runtime.voice_mode = "disconnected"
                runtime.set_status("idle")

    async def _on_client(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype in {"session.start", "ping"}:
            await self.send({"type": "pong", "mode": "fallback", "path": "text-fallback"})
            return
        if etype == "confirm":
            await self._fallback_turn("", confirm=True)
            return
        if etype == "input_text":
            await self._fallback_turn(str(event.get("text") or ""), confirm=bool(event.get("confirm")))
            return
        if etype == "input_audio.append":
            chunk = event.get("audio") or ""
            try:
                self.audio_buf.extend(base64.b64decode(chunk))
            except Exception:  # noqa: BLE001
                await self.send({"type": "error", "message": "bad audio chunk"})
            return
        if etype == "input_audio.commit":
            pcm = bytes(self.audio_buf)
            self.audio_buf.clear()
            text = await self._transcribe(pcm)
            if not text:
                await self.send(
                    {
                        "type": "error",
                        "message": "Live voice is WebRTC. Type in the composer, or tap the hearth on HTTPS.",
                    }
                )
                return
            await self._fallback_turn(text)

    async def _fallback_turn(self, text: str, *, confirm: bool = False) -> None:
        runtime.set_status("thinking")
        await self.send({"type": "status", "agent": "thinking"})
        if text:
            await self.send({"type": "transcript.user", "text": text})
        result = await self.agent.run(text or "(confirm)", confirm=confirm)
        for tool in result.get("tools") or []:
            await self.send({"type": "tool.result", "name": tool.get("name"), "result": tool})
        reply = result.get("reply") or ""
        await self.send({"type": "transcript.assistant", "text": reply, "final": True})
        runtime.set_status("idle")
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


async def voice_socket(websocket: WebSocket) -> None:
    await VoiceSession(websocket).run()
