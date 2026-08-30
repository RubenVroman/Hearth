"""Conversation-context relevance for glass info overlays.

Panels stay in runtime memory; the UI soft-hides when talk moves away and
reappears when context matches again. Hard dismiss (X / Escape) still deletes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from hearth.config import settings
from hearth.runtime import Widget, runtime

# Tools that mean the on-screen overlay is about the current turn.
_KIND_TOOLS: dict[str, frozenset[str]] = {
    "weather": frozenset({"get_weather"}),
    "media": frozenset(
        {
            "plex_search",
            "plex_now_playing",
            "plex_play",
            "plex_clients",
            "radarr_search",
            "radarr_add",
            "sonarr_search",
            "sonarr_add",
            "overseerr_search",
            "overseerr_request",
            "infuse_play",
            "infuse_transport",
            "house_media",
        }
    ),
}

_KIND_LEXICON: dict[str, frozenset[str]] = {
    "weather": frozenset(
        {
            "weather",
            "forecast",
            "temperature",
            "temp",
            "raining",
            "rain",
            "snowing",
            "snow",
            "humidity",
            "wind",
            "outside",
            "celsius",
            "fahrenheit",
            "degrees",
            "cloudy",
            "sunny",
            "hot",
            "cold",
        }
    ),
    "media": frozenset(
        {
            "movie",
            "film",
            "show",
            "series",
            "episode",
            "season",
            "plex",
            "radarr",
            "sonarr",
            "overseerr",
            "infuse",
            "playing",
            "watch",
            "watching",
            "poster",
            "trailer",
            "library",
            "playback",
            "stream",
        }
    ),
}

# Intent domains that are clearly not about a given overlay kind.
_UNRELATED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "lights",
        re.compile(
            r"\b(lights?|scenes?|turn (on|off)|dim |brightness|home assistant)\b",
            re.I,
        ),
    ),
    (
        "food",
        re.compile(
            r"\b(thuisbezorgd|just\s*eat|takeaway|order food|i'?m hungry|"
            r"restaurants?|pizza|burger|sushi|food cart|delivery)\b",
            re.I,
        ),
    ),
    (
        "docker",
        re.compile(r"\b(docker|containers?)\b", re.I),
    ),
    (
        "memory",
        re.compile(
            r"\b(remember that|forget that|what do you remember|search memory|"
            r"list (?:my )?preferences)\b",
            re.I,
        ),
    ),
    (
        "workspace",
        re.compile(r"\b(workspace|chief of staff|open a pr|pull request)\b", re.I),
    ),
]

_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
        "is",
        "it",
        "at",
        "by",
        "with",
        "from",
        "that",
        "this",
        "about",
        "tell",
        "me",
        "what",
        "whats",
        "how",
        "please",
        "part",
        "untitled",
        "library",
        "outside",
        "home",
        "movie",
        "film",
        "show",
        "series",
    }
)

_TOKEN = re.compile(r"[a-z0-9']{3,}", re.I)


@dataclass(frozen=True)
class OverlayRelevance:
    relevant: bool
    reason: str
    topics: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "relevant": self.relevant,
            "reason": self.reason,
            "topics": list(self.topics),
        }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokenize(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN.finditer(text or "")}


def topics_for_widget(widget: Widget) -> list[str]:
    """Stable topic tokens the UI can match against new utterances."""
    kind = str(widget.kind or "")
    topics: set[str] = set(_KIND_LEXICON.get(kind, ()))
    topics.add(kind)
    for chunk in (widget.title, widget.body, widget.detail):
        for token in _tokenize(str(chunk or "")):
            if token not in _STOP:
                topics.add(token)
    data = widget.data or {}
    if kind == "weather":
        place = data.get("place") or data.get("location") or widget.title
        for token in _tokenize(str(place or "")):
            if token not in _STOP:
                topics.add(token)
        condition = data.get("condition")
        for token in _tokenize(str(condition or "")):
            if token not in _STOP:
                topics.add(token)
    if kind == "media":
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        for key in ("title", "show", "type", "player"):
            for token in _tokenize(str(item.get(key) or "")):
                if token not in _STOP:
                    topics.add(token)
    # Prefer short, readable order: kind first, then alpha.
    ordered = sorted(topics, key=lambda t: (0 if t == kind else 1, t))
    return ordered[:48]


def text_matches_topics(text: str, topics: Iterable[str]) -> bool:
    if not text or not topics:
        return False
    tokens = _tokenize(text)
    topic_set = {t.lower() for t in topics}
    # Prefer meaningful overlaps (skip ultra-generic single hits already filtered).
    return bool(tokens & topic_set)


def tool_matches_kind(tool_name: str, kind: str) -> bool:
    return tool_name in _KIND_TOOLS.get(kind, frozenset())


def unrelated_domain(text: str, kind: str) -> str | None:
    """Return a foreign intent domain if the utterance is clearly not about `kind`."""
    raw = (text or "").strip()
    if not raw:
        return None
    # Same-kind lexicon → not unrelated.
    if text_matches_topics(raw, _KIND_LEXICON.get(kind, ())):
        return None
    for domain, pattern in _UNRELATED_PATTERNS:
        if domain == kind:
            continue
        if pattern.search(raw):
            return domain
    # Cross-kind: weather ask while media is up (and vice versa).
    other = "media" if kind == "weather" else "weather" if kind == "media" else ""
    if other and text_matches_topics(raw, _KIND_LEXICON.get(other, ())):
        # Only treat as switch if it doesn't also hit this widget's entity topics.
        return other
    return None


def evaluate_widget(
    widget: Widget,
    *,
    transcript: Iterable[Any] | None = None,
    last_tools: Iterable[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> OverlayRelevance:
    """Decide whether the glass panel should stay visible for this conversation."""
    topics = topics_for_widget(widget)
    kind = str(widget.kind or "")
    clock = now or _now()
    fresh_s = max(1, int(settings.overlay_fresh_seconds))
    idle_s = max(fresh_s + 1, int(settings.overlay_idle_seconds))

    updated = _parse_ts(widget.updated_at) or _parse_ts(widget.ts) or clock
    age = max(0.0, (clock - updated).total_seconds())

    lines = list(transcript if transcript is not None else runtime.transcript)
    recent = [line for line in lines[-8:] if getattr(line, "kind", "message") == "message"]
    latest_user = ""
    for line in reversed(recent):
        if getattr(line, "role", "") == "user" and getattr(line, "text", ""):
            latest_user = str(line.text)
            break

    # Newest user turn wins domain switches (even inside the fresh window).
    if latest_user:
        if text_matches_topics(latest_user, topics):
            return OverlayRelevance(True, "topic_match", topics)
        foreign = unrelated_domain(latest_user, kind)
        if foreign:
            return OverlayRelevance(False, f"unrelated:{foreign}", topics)

    if age <= fresh_s:
        return OverlayRelevance(True, "fresh", topics)

    tools = list(last_tools if last_tools is not None else runtime.last_tools)
    for row in tools[-6:]:
        name = str(row.get("name") or "")
        if tool_matches_kind(name, kind):
            return OverlayRelevance(True, "tool_match", topics)

    for line in reversed(recent[-6:]):
        text = str(getattr(line, "text", "") or "")
        if text_matches_topics(text, topics):
            return OverlayRelevance(True, "topic_match", topics)

    if age >= idle_s:
        return OverlayRelevance(False, "idle", topics)

    return OverlayRelevance(True, "active", topics)


def enrich_widget_dict(widget: Widget) -> dict[str, Any]:
    payload = widget.as_dict()
    payload["context"] = evaluate_widget(widget).as_dict()
    return payload
