"""Conversation-context relevance for glass info overlays.

Panels stay in runtime memory; the UI soft-hides when talk moves away and
reappears when context matches again. Hard dismiss (X / Escape) still deletes.

Relevance is driven by *entity* topics (title, place, active media card), not
generic kind words like "movie" / "weather" alone — those kept panels stuck on
screen after talk had moved on.
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
            "plex_browse_genre",
            "radarr_search",
            "radarr_add",
            "sonarr_search",
            "sonarr_add",
            "overseerr_search",
            "overseerr_request",
            "infuse_play",
            "infuse_transport",
            "house_media",
            "media_activity",
        }
    ),
    "downloads": frozenset({"radarr_queue", "sonarr_queue"}),
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
            "genre",
            "genres",
            "animation",
            "anime",
        }
    ),
    "downloads": frozenset(
        {
            "download",
            "downloads",
            "downloading",
            "queue",
            "progress",
            "percent",
            "radarr",
            "sonarr",
            "grab",
            "torrent",
            "usenet",
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

_ACK_PATTERN = re.compile(
    r"^\s*(ok|okay|k|thanks|thank you|thx|got it|cool|nice|great|sure|yep|yeah|"
    r"yup|alright|perfect|sweet|cheers|awesome|good|fine|noted|understood|"
    r"sounds good|all good|no problem|np)[.!?]*\s*$",
    re.I,
)

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
        "episode",
        "season",
        "year",
        "type",
        "watching",
        "playing",
        "watch",
        "poster",
        "trailer",
        "playback",
        "stream",
        "weather",
        "forecast",
        "temperature",
        "download",
        "downloads",
        "downloading",
        "queue",
        "progress",
        "percent",
    }
)

_TOKEN = re.compile(r"[a-z0-9']{3,}", re.I)


@dataclass(frozen=True)
class OverlayRelevance:
    relevant: bool
    reason: str
    topics: list[str]
    active_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "relevant": self.relevant,
            "reason": self.reason,
            "topics": list(self.topics),
        }
        if self.active_id:
            out["active_id"] = self.active_id
        return out


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


def is_ack(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 48:
        return False
    return bool(_ACK_PATTERN.match(raw))


def media_items(widget: Widget) -> list[dict[str, Any]]:
    data = widget.data or {}
    items = data.get("items")
    if isinstance(items, list) and items:
        return [row for row in items if isinstance(row, dict)]
    item = data.get("item")
    if isinstance(item, dict) and (item.get("title") or item.get("id")):
        return [item]
    return []


def active_media_item(widget: Widget) -> dict[str, Any] | None:
    items = media_items(widget)
    if not items:
        return None
    data = widget.data or {}
    active_id = str(data.get("active_id") or "")
    if active_id:
        for row in items:
            if str(row.get("id") or "") == active_id:
                return row
    return items[0]


def _entity_tokens_from_chunks(*chunks: Any) -> set[str]:
    topics: set[str] = set()
    for chunk in chunks:
        for token in _tokenize(str(chunk or "")):
            if token not in _STOP:
                topics.add(token)
    return topics


def entity_topics_for_widget(widget: Widget) -> list[str]:
    """Title / place tokens for the *active* on-screen thing (not kind lexicon)."""
    kind = str(widget.kind or "")
    topics: set[str] = set()
    data = widget.data or {}
    if kind == "weather":
        place = data.get("place") or data.get("location") or widget.title
        topics |= _entity_tokens_from_chunks(place, data.get("condition"))
    elif kind == "media":
        item = active_media_item(widget) or {}
        topics |= _entity_tokens_from_chunks(
            item.get("title"),
            item.get("show"),
            widget.title,
        )
    elif kind == "downloads":
        topics |= _entity_tokens_from_chunks(widget.title, data.get("query"))
        for row in list(data.get("downloads") or [])[:8]:
            if isinstance(row, dict):
                topics |= _entity_tokens_from_chunks(row.get("title"))
    else:
        topics |= _entity_tokens_from_chunks(widget.title, widget.body)
    ordered = sorted(topics)
    return ordered[:48]


def stack_topics_for_widget(widget: Widget) -> list[str]:
    """All media-stack title tokens — used to focus a card already on screen."""
    topics: set[str] = set()
    for item in media_items(widget):
        topics |= _entity_tokens_from_chunks(item.get("title"), item.get("show"))
    return sorted(topics)[:64]


def topics_for_widget(widget: Widget) -> list[str]:
    """Topics exposed to the client: entity tokens + kind + light lexicon."""
    kind = str(widget.kind or "")
    topics: set[str] = set(entity_topics_for_widget(widget))
    topics.add(kind)
    # Keep a thin kind lexicon so the client can detect same-domain talk, but
    # server relevance no longer treats these alone as "still about the panel".
    topics.update(_KIND_LEXICON.get(kind, ()))
    if kind == "media":
        topics.update(stack_topics_for_widget(widget))
    ordered = sorted(topics, key=lambda t: (0 if t == kind else 1, t))
    return ordered[:64]


def text_matches_topics(text: str, topics: Iterable[str]) -> bool:
    if not text or not topics:
        return False
    tokens = _tokenize(text)
    topic_set = {t.lower() for t in topics}
    return bool(tokens & topic_set)


def title_mentioned(text: str, title: str) -> bool:
    """True when a media title (or distinctive multi-token slice) appears in text."""
    raw = (text or "").strip().lower()
    title_l = (title or "").strip().lower()
    if not raw or not title_l or len(title_l) < 3:
        return False
    if title_l in raw:
        return True
    # Multi-word titles: require at least two significant token hits in order.
    parts = [t for t in _tokenize(title_l) if t not in _STOP]
    if len(parts) >= 2:
        return all(p in raw for p in parts[:3])
    if len(parts) == 1:
        # Single distinctive token (e.g. "Dune", "Annihilation") — whole word.
        return bool(re.search(rf"\b{re.escape(parts[0])}\b", raw))
    return False


def focus_media_from_text(widget: Widget, text: str) -> str | None:
    """If `text` names a stacked title, return that item id (does not mutate)."""
    if str(widget.kind or "") != "media" or not text:
        return None
    items = media_items(widget)
    if len(items) < 1:
        return None
    # Prefer longer / more specific titles when several could match.
    ranked = sorted(
        items,
        key=lambda row: len(str(row.get("title") or "")),
        reverse=True,
    )
    for row in ranked:
        title = str(row.get("title") or "")
        if title_mentioned(text, title):
            return str(row.get("id") or "") or None
        show = str(row.get("show") or "")
        if show and title_mentioned(text, show):
            return str(row.get("id") or "") or None
    return None


def apply_media_focus(widget: Widget, active_id: str) -> bool:
    """Point the media widget at `active_id`. Returns True if something changed."""
    if str(widget.kind or "") != "media" or not active_id:
        return False
    items = media_items(widget)
    match = next((row for row in items if str(row.get("id") or "") == active_id), None)
    if match is None:
        return False
    data = dict(widget.data or {})
    prev = str(data.get("active_id") or "")
    if prev == active_id and data.get("item") == match:
        return False
    data["active_id"] = active_id
    data["item"] = match
    data["items"] = items
    widget.data = data
    title = str(match.get("title") or widget.title or "Untitled")
    year = match.get("year")
    media_type = str(match.get("type") or "movie")
    bits = [media_type]
    if year:
        bits.append(str(year))
    if match.get("show"):
        bits.append(str(match["show"]))
    widget.title = title
    widget.body = " · ".join(bits)
    detail_parts: list[str] = []
    summary = (match.get("summary") or "").strip()
    if summary:
        detail_parts.append(summary[:220] + ("…" if len(summary) > 220 else ""))
    if match.get("player") and match.get("state"):
        detail_parts.append(f"{match['player']} · {match['state']}")
    elif match.get("player"):
        detail_parts.append(str(match["player"]))
    if match.get("skeleton"):
        detail_parts.append("looking up")
    n = len(items)
    if n > 1:
        detail_parts.append(f"{n} on screen")
    widget.detail = " · ".join(detail_parts)
    return True


def sync_media_focus_from_transcript(
    widget: Widget,
    *,
    transcript: Iterable[Any] | None = None,
) -> str | None:
    """Bring a stacked card forward when recent talk names it."""
    if str(widget.kind or "") != "media":
        return None
    lines = list(transcript if transcript is not None else runtime.transcript)
    recent = [line for line in lines[-8:] if getattr(line, "kind", "message") == "message"]
    for line in reversed(recent):
        text = str(getattr(line, "text", "") or "")
        hit = focus_media_from_text(widget, text)
        if hit:
            apply_media_focus(widget, hit)
            return hit
    return str((widget.data or {}).get("active_id") or "") or None


def tool_matches_kind(tool_name: str, kind: str) -> bool:
    return tool_name in _KIND_TOOLS.get(kind, frozenset())


def unrelated_domain(text: str, kind: str) -> str | None:
    """Return a foreign intent domain if the utterance is clearly not about `kind`."""
    raw = (text or "").strip()
    if not raw or is_ack(raw):
        return None
    # Same-kind lexicon → not unrelated (entity match handled elsewhere).
    if text_matches_topics(raw, _KIND_LEXICON.get(kind, ())):
        return None
    for domain, pattern in _UNRELATED_PATTERNS:
        if domain == kind:
            continue
        if pattern.search(raw):
            return domain
    # Cross-kind: weather ask while media/downloads is up (and vice versa).
    others = {
        "weather": ("media", "downloads"),
        "media": ("weather", "downloads"),
        "downloads": ("weather", "media"),
    }.get(kind, ())
    for other in others:
        if text_matches_topics(raw, _KIND_LEXICON.get(other, ())):
            return other
    return None


def _latest_user_text(recent: list[Any]) -> str:
    for line in reversed(recent):
        if getattr(line, "role", "") == "user" and getattr(line, "text", ""):
            return str(line.text)
    return ""


def _tool_ts(row: dict[str, Any]) -> datetime | None:
    return _parse_ts(str(row.get("ts") or row.get("at") or "") or None)


def evaluate_widget(
    widget: Widget,
    *,
    transcript: Iterable[Any] | None = None,
    last_tools: Iterable[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> OverlayRelevance:
    """Decide whether the glass panel should stay visible for this conversation."""
    kind = str(widget.kind or "")
    active_id: str | None = None
    if kind == "media":
        active_id = sync_media_focus_from_transcript(widget, transcript=transcript)

    topics = topics_for_widget(widget)
    entity = entity_topics_for_widget(widget)
    clock = now or _now()
    fresh_s = max(1, int(settings.overlay_fresh_seconds))
    idle_s = max(fresh_s + 1, int(settings.overlay_idle_seconds))

    updated = _parse_ts(widget.updated_at) or _parse_ts(widget.ts) or clock
    age = max(0.0, (clock - updated).total_seconds())

    lines = list(transcript if transcript is not None else runtime.transcript)
    recent = [line for line in lines[-8:] if getattr(line, "kind", "message") == "message"]
    latest_user = _latest_user_text(recent)

    # Sync already focused the newest named stack card. Latest user can hide on a
    # clear domain switch, or confirm relevance — but must not steal focus back
    # from a newer assistant utterance that named another stacked title.
    if latest_user and not is_ack(latest_user):
        user_stack = focus_media_from_text(widget, latest_user) if kind == "media" else None
        user_entity = text_matches_topics(latest_user, entity)
        foreign = unrelated_domain(latest_user, kind)
        if foreign and not user_stack and not user_entity:
            return OverlayRelevance(False, f"unrelated:{foreign}", topics, active_id=active_id)
        if user_stack or user_entity:
            return OverlayRelevance(True, "topic_match", topics, active_id=active_id)

    if age <= fresh_s:
        return OverlayRelevance(True, "fresh", topics, active_id=active_id)

    tools = list(last_tools if last_tools is not None else runtime.last_tools)
    for row in tools[-6:]:
        name = str(row.get("name") or "")
        if not tool_matches_kind(name, kind):
            continue
        # Older house-tool rows must not keep a panel up after the user has
        # clearly moved on (and tools historically lacked timestamps).
        if latest_user and not is_ack(latest_user):
            same_entity = text_matches_topics(latest_user, entity)
            same_kind = text_matches_topics(latest_user, _KIND_LEXICON.get(kind, ()))
            if not same_entity and not same_kind:
                continue
            tool_time = _tool_ts(row)
            latest_user_ts = None
            for line in reversed(recent):
                if getattr(line, "role", "") == "user":
                    latest_user_ts = _parse_ts(getattr(line, "ts", None))
                    break
            if latest_user_ts and tool_time and tool_time < latest_user_ts and not same_entity:
                continue
        return OverlayRelevance(True, "tool_match", topics, active_id=active_id)

    # Walk newest → older. A non-matching user turn means talk left the panel;
    # older entity hits must not keep it stuck.
    for line in reversed(recent[-6:]):
        text = str(getattr(line, "text", "") or "")
        role = str(getattr(line, "role", "") or "")
        if is_ack(text):
            continue
        if kind == "media":
            hit = focus_media_from_text(widget, text)
            if hit:
                apply_media_focus(widget, hit)
                return OverlayRelevance(True, "topic_match", topics, active_id=hit)
        if text_matches_topics(text, entity):
            return OverlayRelevance(True, "topic_match", topics, active_id=active_id)
        if role == "user":
            break

    if age >= idle_s:
        return OverlayRelevance(False, "idle", topics, active_id=active_id)

    # Past the fresh window with no entity evidence → hide (was "active" and stuck).
    return OverlayRelevance(False, "stale", topics, active_id=active_id)


def enrich_widget_dict(widget: Widget) -> dict[str, Any]:
    rel = evaluate_widget(widget)
    payload = widget.as_dict()
    payload["context"] = rel.as_dict()
    return payload
