from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from hearth.config import settings

AgentStatus = Literal["idle", "listening", "thinking", "speaking", "tool"]
VoiceMode = Literal["disconnected", "fallback", "live"]
WidgetKind = Literal["weather", "media", "downloads"]
WidgetStatus = Literal["pending", "running", "done", "error", "info"]


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
    # "confirm" = normal dry-run; "awaiting_client" = plex_play waiting for Plex to open.
    reason: str = "confirm"


@dataclass
class Widget:
    """Centered glass-panel overlay for rich visual content (weather, media, downloads)."""

    id: str
    kind: WidgetKind | str
    title: str
    status: WidgetStatus | str = "info"
    body: str = ""
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    dismissible: bool = True
    sticky: bool = False
    ts: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "body": self.body,
            "detail": self.detail,
            "data": self.data,
            "dismissible": self.dismissible,
            "sticky": self.sticky,
            "ts": self.ts,
            "updated_at": self.updated_at,
        }


class Runtime:
    def __init__(self) -> None:
        self.started_at = _now()
        self.agent_status: AgentStatus = "idle"
        self.voice_mode: VoiceMode = "disconnected"
        self.voice_reason: str = ""
        self.voice_path: str = "disconnected"
        self.transcript: deque[TranscriptLine] = deque(maxlen=80)
        self.last_tools: deque[dict[str, Any]] = deque(maxlen=12)
        self.widgets: dict[str, Widget] = {}
        self.pending: PendingConfirm | None = None
        self.openai_live: bool = False

    def note(self, role: str, text: str, kind: str = "message") -> TranscriptLine:
        line = TranscriptLine(role=role, text=text, kind=kind)
        self.transcript.append(line)
        return line

    def get_widget(self, widget_id: str) -> Widget | None:
        return self.widgets.get(widget_id)

    def upsert_widget(self, widget: Widget) -> Widget:
        existing = self.widgets.get(widget.id)
        if existing is not None:
            widget.ts = existing.ts
            # Keep updated_at stable when payload is unchanged so the UI does not
            # re-render / flicker on status polls.
            if (
                existing.kind == widget.kind
                and existing.title == widget.title
                and existing.status == widget.status
                and existing.body == widget.body
                and existing.detail == widget.detail
                and existing.data == widget.data
            ):
                widget.updated_at = existing.updated_at
                self.widgets[widget.id] = widget
                return widget
        widget.updated_at = _now()
        # Re-insert so the latest visual floats to the end.
        self.widgets.pop(widget.id, None)
        self.widgets[widget.id] = widget
        # Cap overlays (weather + media + a little headroom).
        while len(self.widgets) > 4:
            oldest = next(iter(self.widgets))
            del self.widgets[oldest]
        return widget

    def dismiss_widget(self, widget_id: str) -> bool:
        if widget_id not in self.widgets:
            return False
        del self.widgets[widget_id]
        return True

    def clear_widgets(self, *, dismissible_only: bool = True) -> int:
        if not dismissible_only:
            n = len(self.widgets)
            self.widgets.clear()
            return n
        remove = [wid for wid, w in self.widgets.items() if w.dismissible and not w.sticky]
        for wid in remove:
            del self.widgets[wid]
        return len(remove)

    def list_widgets(self) -> list[dict[str, Any]]:
        return [w.as_dict() for w in self.widgets.values()]

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
                "reason": self.pending.reason,
            },
            "last_tools": list(self.last_tools),
            "widgets": self.list_widgets(),
        }

    def latest_user(self) -> str:
        for line in reversed(self.transcript):
            if line.role == "user" and line.text:
                return line.text
        return ""


runtime = Runtime()
