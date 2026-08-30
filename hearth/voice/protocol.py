from __future__ import annotations

import io
import json
import wave
from typing import Any

from hearth.config import settings


def pcm16_to_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def session_update_payload(tools: list[dict[str, Any]]) -> dict[str, Any]:
    from hearth.agent.prompts import compose_system_prompt
    from hearth.voice.vad import audio_input_config

    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": settings.openai_realtime_model,
            "instructions": compose_system_prompt(),
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input_config(),                "output": {
                    "voice": settings.openai_tts_voice,
                },
            },
            "tools": tools,
            "tool_choice": "auto",
        },
    }


def dumps(event: dict[str, Any]) -> str:
    return json.dumps(event, default=str)
