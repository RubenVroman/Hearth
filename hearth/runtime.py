from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from hearth.config import settings

AgentStatus = Literal["idle", "listening", "thinking", "speaking", "tool"]
VoiceMode = Literal["disconnected", "fallback", "live"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TranscriptLine:
    role: str
    text: str
    ts: str = field(default_factory=_now)
    kind: str = "message"


@dataclass
class PendingConfirm:
    tool: str
    args: dict[str, Any]
    preview: str
    ts: str = field(default_factory=_now)


class Runtime:
    def __init__(self) -> None:
        self.started_at = _now()
        self.agent_status: AgentStatus = "idle"
        self.voice_mode: VoiceMode = "disconnected"
        self.voice_reason: str = ""
        self.voice_path: str = "disconnected"
        self.transcript: deque[TranscriptLine] = deque(maxlen=80)
        self.last_tools: deque[dict[str, Any]] = deque(maxlen=12)
        self.pending: PendingConfirm | None = None
        self.openai_live: bool = False

    def note(self, role: str, text: str, kind: str = "message") -> TranscriptLine:
        line = TranscriptLine(role=role, text=text, kind=kind)
        self.transcript.append(line)
        return line

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "agent": self.agent_status,
            "voice": {
                "mode": self.voice_mode,
                "path": self.voice_path,
                "reason": self.voice_reason,
                "openai_live": self.openai_live,
                "model": settings.openai_realtime_model if self.openai_live else "",
                "beta": False,
            },
            "pending": None
            if self.pending is None
            else {
                "tool": self.pending.tool,
                "args": self.pending.args,
                "preview": self.pending.preview,
                "ts": self.pending.ts,
            },
            "last_tools": list(self.last_tools),
        }

    def latest_user(self) -> str:
        for line in reversed(self.transcript):
            if line.role == "user" and line.text:
                return line.text
        return ""


runtime = Runtime()
