"""Progress status posts for titles queued by the Telegram inbox.

Quiet policy: one early "started and healthy" ping once a download reaches
a trustworthy mid-start percent, then silence until done / failed (or a manual
status ask via radarr_queue / sonarr_queue elsewhere).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from hearth.tools.arr import radarr, sonarr

log = logging.getLogger("hearth.telegram")

# Early healthy-start signal. Small threshold so we know the grab is moving
# without spamming later percent ticks (10%, 25%, …). Skip near-complete
# first sightings so we never announce "downloading, ~100%".
START_THRESHOLD = 5.0
START_PERCENT_MAX = 95.0


@dataclass
class TrackedGrab:
    chat_id: int
    title: str
    service: str  # radarr | sonarr
    year: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    last_status: str = ""
    announce_started: bool = False
    done: bool = False


def format_queued(title: str, year: int | None, via: str) -> str:
    label = f"{title} ({year})" if year else title
    return f"Queued {label} via {via}."


def format_downloading(title: str, percent: float | None) -> str:
    """Announce an in-flight grab. Never formats a fake near-100% figure."""
    if percent is None or percent < 0:
        return f"{title} is downloading."
    # Guard: never say "~100%" (or ~95%+) while still "downloading".
    if percent >= START_PERCENT_MAX:
        return f"{title} is downloading."
    return f"{title} is downloading, ~{percent:g}%."


def format_done(title: str) -> str:
    return f"{title} is done — in Plex."


def format_failed(title: str, detail: str = "") -> str:
    bit = f" ({detail})" if detail else ""
    return f"{title} failed{bit}."


def format_not_found(query: str) -> str:
    return f"Couldn't find a match for '{query}'. Send an IMDb/TMDB link?"


def format_ambiguous(query: str, options: list[dict[str, Any]]) -> str:
    show = min(3, len(options))
    lines = [f"Which one for '{query}'? Reply 1–{show}:"]
    for idx, row in enumerate(options[:show], start=1):
        title = row.get("title") or "Untitled"
        year = row.get("year")
        label = f"{title} ({year})" if year else str(title)
        lines.append(f"{idx}. {label}")
    if len(options) > 1:
        extra = f" ({len(options)} matches)" if len(options) > show else ""
        lines.append(
            f"Or say 'all of them'{extra}, 'the first one', or 'the new one'."
        )
    return "\n".join(lines)


def format_queued_many(titles: list[str], via: str) -> str:
    if not titles:
        return f"Nothing new to queue via {via}."
    if len(titles) == 1:
        return format_queued(titles[0], None, via)
    preview = ", ".join(titles[:5])
    more = f" (+{len(titles) - 5} more)" if len(titles) > 5 else ""
    return f"Queued {len(titles)} via {via}: {preview}{more}."


def format_already(title: str, *, queued: bool = False, library: bool = False) -> str:
    if library:
        return f"{title} is already in the library."
    if queued:
        return f"{title} is already queued."
    return f"{title} is already on the list."


def format_reject_download() -> str:
    return (
        "This inbox only queues movies/series/TV through *arr/Overseerr — "
        "not a general downloader. Drop an IMDb/TMDB link or a title."
    )


def format_rate_limited() -> str:
    return "Too many requests in this group — try again in a minute."


def _as_percent(value: Any) -> float | None:
    """Coerce a queue percent already on 0–100 (from summarize_queue_item)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    n = float(value)
    if n < 0:
        return None
    # summarize_queue_item already scales 0–1 API fractions; do not ×100 again.
    return n


def _trustworthy_start_percent(percent: float | None, *, threshold: float) -> bool:
    """True when percent is a real early-progress signal (not missing / not ~done)."""
    if percent is None:
        return False
    return threshold <= percent < START_PERCENT_MAX


class ProgressTracker:
    def __init__(self) -> None:
        self._items: list[TrackedGrab] = []

    def reset(self) -> None:
        self._items.clear()

    def track(self, chat_id: int, title: str, service: str, year: int | None = None) -> None:
        title = (title or "").strip()
        if not title:
            return
        for item in self._items:
            if item.chat_id == chat_id and item.title.lower() == title.lower() and not item.done:
                return
        self._items.append(
            TrackedGrab(chat_id=chat_id, title=title, service=service, year=year)
        )

    @property
    def active(self) -> list[TrackedGrab]:
        return [item for item in self._items if not item.done]

    async def poll_once(
        self,
        send: Callable[[int, str], Awaitable[Any]],
        *,
        max_age_s: float = 6 * 3600,
        start_threshold: float = START_THRESHOLD,
    ) -> None:
        now = time.monotonic()
        for item in list(self.active):
            if now - item.started_at > max_age_s:
                item.done = True
                continue
            client = radarr if item.service == "radarr" else sonarr
            try:
                payload = await client.queue(item.title)
            except Exception:  # noqa: BLE001
                log.exception("telegram progress poll failed for %s", item.title)
                continue
            downloads = payload.get("downloads") or []
            if not downloads:
                # Not in queue anymore — treat as imported / done after we had progress.
                if item.announce_started or item.last_status:
                    await send(item.chat_id, format_done(item.title))
                    item.done = True
                continue
            row = downloads[0]
            status = str(row.get("status") or "unknown").strip().lower()
            percent = _as_percent(row.get("percent"))
            item.last_status = status
            if status == "failed":
                await send(item.chat_id, format_failed(item.title))
                item.done = True
                continue
            if status == "completed":
                # Imported (or *arr says completed) — announce done, never
                # "downloading ~100%" even on first sighting.
                await send(item.chat_id, format_done(item.title))
                item.done = True
                continue
            if status == "importing":
                # Still processing — stay quiet; do not claim Plex or ~100%.
                continue
            # One early healthy-start ping at a trustworthy mid-start percent;
            # no later percent spam. Skip bogus near-100 / missing-byte first polls.
            if not item.announce_started and _trustworthy_start_percent(
                percent, threshold=start_threshold
            ):
                await send(item.chat_id, format_downloading(item.title, percent))
                item.announce_started = True
