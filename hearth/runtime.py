from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from hearth.config import settings

AgentStatus = Literal["idle", "listening", "thinking", "speaking", "tool"]
ActivityPhase = Literal[
    "idle",
    "listening",
    "thinking",
    "speaking",
    "working",
    "ha",
    "web_search",
    "cos",
    "error",
]
VoiceMode = Literal["disconnected", "fallback", "live"]
WidgetKind = Literal["weather", "media", "downloads"]
WidgetStatus = Literal["pending", "running", "done", "error", "info"]

# Brief hold so a failed backend call is readable without sticky alarms.
ERROR_HOLD_SECONDS = 4.0

# Tool families → UI activity (labels only — never secrets).
_HA_TOOLS = frozenset(
    {
        "ha_list_entities",
        "ha_get_state",
        "ha_call_service",
        "ha_device_control",
        "ha_media_control",
        "media_activity",
        "house_media",
        "house_network",
    }
)
_WEB_SEARCH_TOOLS = frozenset({"web_search"})
_COS_TOOLS = frozenset({"chief_of_staff"})

_PHASE_LABELS: dict[ActivityPhase, str] = {
    "idle": "",
    "listening": "",
    "thinking": "Working…",
    "speaking": "",
    "working": "Working…",
    "ha": "Fetching from Home Assistant…",
    "web_search": "Searching…",
    "cos": "Escalating to Chief of Staff…",
    "error": "Something went wrong",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def activity_for_tool(tool_name: str) -> tuple[ActivityPhase, str]:
    """Map a house tool name to a UI phase + short label."""
    name = (tool_name or "").strip()
    if name in _WEB_SEARCH_TOOLS:
        return "web_search", _PHASE_LABELS["web_search"]
    if name in _COS_TOOLS:
        return "cos", _PHASE_LABELS["cos"]
    if name in _HA_TOOLS or name.startswith("ha_"):
        return "ha", _PHASE_LABELS["ha"]
    return "working", _PHASE_LABELS["working"]


def brief_error_label(message: str = "", *, tool: str = "") -> str:
    """Human-readable, non-scary error line for the status indicator."""
    raw = (message or "").strip()
    # Never echo keys / tokens into the browser chrome.
    raw = re.sub(
        r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[=:]\s*\S+",
        r"\1=…",
        raw,
    )
    raw = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}\b", "sk-…", raw)
    lower = raw.lower()
    if "not configured" in lower:
        return "Not configured"
    if "timeout" in lower or "timed out" in lower:
        return "Timed out"
    if "network" in lower or "connection" in lower:
        return "Connection failed"
    if tool == "web_search" or "search" in lower:
        return "Search failed"
    if tool == "chief_of_staff" or "chief of staff" in lower:
        return "Escalation failed"
    if tool.startswith("ha_") or tool in _HA_TOOLS or "home assistant" in lower:
        return "Home Assistant failed"
    if raw:
        # Keep it short — no stack traces or secrets in the chrome.
        clip = raw.split("\n", 1)[0].strip()
        if len(clip) > 48:
            clip = clip[:45].rstrip() + "…"
        return clip
    return _PHASE_LABELS["error"]


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


@dataclass
class Activity:
    """Shared UI status — labels only, safe for the browser."""

    phase: ActivityPhase = "idle"
    label: str = ""
    tool: str = ""
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "label": self.label,
            "tool": self.tool,
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
        self._activity = Activity()
        self._error_until: float = 0.0

    def note(self, role: str, text: str, kind: str = "message") -> TranscriptLine:
        line = TranscriptLine(role=role, text=text, kind=kind)
        self.transcript.append(line)
        return line

    def set_status(self, status: AgentStatus, *, tool: str = "") -> None:
        """Update coarse agent status and the UI activity label together."""
        self.agent_status = status
        if status == "tool":
            self.begin_tool(tool or "tool")
            return
        if status == "thinking":
            self._set_activity("thinking", _PHASE_LABELS["thinking"])
            return
        if status == "listening":
            # Keep a brief error visible until it expires.
            if self._error_active():
                return
            self._set_activity("listening", "")
            return
        if status == "speaking":
            if self._error_active():
                return
            self._set_activity("speaking", "")
            return
        # idle
        if self._error_active():
            return
        self._set_activity("idle", "")

    def begin_tool(self, tool_name: str) -> None:
        phase, label = activity_for_tool(tool_name)
        self.agent_status = "tool"
        self._error_until = 0.0
        self._set_activity(phase, label, tool=tool_name)

    def end_tool(self, *, ok: bool = True, error: str = "", tool: str = "") -> None:
        """After a tool returns — sticky brief error, or leave status for the caller."""
        if ok:
            return
        self.flash_error(error, tool=tool or self._activity.tool)

    def flash_error(self, message: str = "", *, tool: str = "", hold: float = ERROR_HOLD_SECONDS) -> None:
        label = brief_error_label(message, tool=tool)
        self._error_until = time.monotonic() + max(0.5, hold)
        self._set_activity("error", label, tool=tool)

    def activity_snapshot(self) -> dict[str, Any]:
        """Resolve current activity, clearing expired errors."""
        if self._activity.phase == "error" and not self._error_active():
            self._error_until = 0.0
            # Fall back to coarse agent status once the flash expires.
            if self.agent_status == "tool":
                self._set_activity("working", _PHASE_LABELS["working"], tool=self._activity.tool)
            elif self.agent_status == "thinking":
                self._set_activity("thinking", _PHASE_LABELS["thinking"])
            elif self.agent_status == "listening":
                self._set_activity("listening", "")
            elif self.agent_status == "speaking":
                self._set_activity("speaking", "")
            else:
                self._set_activity("idle", "")
        return self._activity.as_dict()

    def _error_active(self) -> bool:
        return self._activity.phase == "error" and time.monotonic() < self._error_until

    def _set_activity(self, phase: ActivityPhase, label: str, *, tool: str = "") -> None:
        self._activity = Activity(phase=phase, label=label, tool=tool or "", updated_at=_now())

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
        from hearth.overlay_context import enrich_widget_dict

        return [enrich_widget_dict(w) for w in self.widgets.values()]

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "agent": self.agent_status,
            "activity": self.activity_snapshot(),
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
