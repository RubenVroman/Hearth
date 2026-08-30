"""Progress status posts for titles queued by the Telegram inbox."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from hearth.tools.arr import radarr, sonarr

log = logging.getLogger("hearth.telegram")

MILESTONES = (0, 25, 50, 75, 100)


@dataclass
class TrackedGrab:
    chat_id: int
    title: str
    service: str  # radarr | sonarr
    year: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    last_percent_bucket: int = -1
    last_status: str = ""
    announce_started: bool = False
    done: bool = False


def format_queued(title: str, year: int | None, via: str) -> str:
    label = f"{title} ({year})" if year else title
    return f"Queued {label} via {via}."


def format_downloading(title: str, percent: float | None) -> str:
    if percent is None:
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
    lines = [f"Which one for '{query}'? Reply 1–{min(3, len(options))}:"]
    for idx, row in enumerate(options[:3], start=1):
        title = row.get("title") or "Untitled"
        year = row.get("year")
        label = f"{title} ({year})" if year else str(title)
        lines.append(f"{idx}. {label}")
    return "\n".join(lines)


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


def percent_bucket(percent: float | None) -> int:
    if percent is None:
        return 0
    value = float(percent)
    chosen = 0
    for mark in MILESTONES:
        if value >= mark:
            chosen = mark
    return chosen


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
                if item.announce_started or item.last_percent_bucket >= 25:
                    await send(item.chat_id, format_done(item.title))
                    item.done = True
                continue
            row = downloads[0]
            status = str(row.get("status") or "unknown")
            percent = row.get("percent")
            if status == "failed":
                await send(item.chat_id, format_failed(item.title))
                item.done = True
                continue
            if status == "completed":
                await send(item.chat_id, format_done(item.title))
                item.done = True
                continue
            bucket = percent_bucket(percent if isinstance(percent, (int, float)) else None)
            if not item.announce_started:
                await send(
                    item.chat_id,
                    format_downloading(
                        item.title,
                        float(percent) if isinstance(percent, (int, float)) else None,
                    ),
                )
                item.announce_started = True
                item.last_percent_bucket = bucket
                item.last_status = status
                continue
            if bucket > item.last_percent_bucket and bucket in {25, 50, 75}:
                await send(
                    item.chat_id,
                    format_downloading(item.title, float(bucket)),
                )
                item.last_percent_bucket = bucket
            item.last_status = status
