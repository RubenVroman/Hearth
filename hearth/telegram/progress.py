"""Progress status posts for titles queued by the Telegram inbox.

Quiet policy: one early "started and healthy" ping once a download reaches
a trustworthy mid-start percent, then silence until done / failed (or a manual
status ask via radarr_queue / sonarr_queue elsewhere).

When a watched grab stalls or fails, automatically blocklist the bad release
and grab an alternate *arr source (capped) — with clear user feedback.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from hearth.config import settings
from hearth.tools.arr import download_is_unhealthy, radarr, sonarr

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
    last_percent: float | None = None
    last_progress_at: float = field(default_factory=time.monotonic)
    announce_started: bool = False
    announce_retrying: bool = False
    retry_attempts: int = 0
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


def format_retrying(
    title: str,
    *,
    indexer: str = "",
    attempt: int = 0,
    max_attempts: int = 0,
) -> str:
    source = f" via {indexer}" if indexer else ""
    cap = f" (attempt {attempt}/{max_attempts})" if attempt and max_attempts else ""
    return f"{title} stalled — trying another source{source}{cap}."


def format_retry_exhausted(title: str, *, max_attempts: int = 0) -> str:
    if max_attempts:
        return (
            f"{title} failed — ran out of alternate sources "
            f"after {max_attempts} tries."
        )
    return f"{title} failed — ran out of alternate sources."


def format_not_found(query: str) -> str:
    """Catalog miss for an instant Title (YYYY) / id path.

    Never ask for an IMDb/TMDB link — the conversation hop confirms
    model-named titles instead of calling this.
    """
    return f"Couldn't find a match for '{query}'."


def format_guess_confirm(title: str, year: int | None = None) -> str:
    """Single best-guess confirmation — never a list-less 1–N range."""
    label = f"{title} ({year})" if year not in (None, "") else str(title or "that")
    return f"Did you mean {label}?"


def format_ambiguous(query: str, options: list[dict[str, Any]]) -> str:
    # n==1 must name/confirm the title — never "Reply 1–1" without a real menu.
    if len(options) == 1:
        row = options[0]
        title = str(row.get("title") or query or "Untitled")
        year = row.get("year")
        try:
            year_i = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year_i = None
        return format_guess_confirm(title, year_i)
    show = min(3, len(options))
    lines = [f"Which one for '{query}'? Reply 1–{show}:"]
    for idx, row in enumerate(options[:show], start=1):
        title = row.get("title") or "Untitled"
        year = row.get("year")
        kind = str(row.get("mediaType") or "").strip().lower()
        label = f"{title} ({year})" if year else str(title)
        if kind in {"movie", "tv"}:
            type_bit = "TV" if kind == "tv" else "movie"
            # Only annotate type when it helps tell options apart.
            kinds = {
                str(o.get("mediaType") or "").strip().lower()
                for o in options[:show]
            }
            if len(kinds) > 1:
                label = f"{label} [{type_bit}]"
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


def _stall_idle_seconds() -> float:
    try:
        return max(30.0, float(settings.download_stall_idle_seconds))
    except (TypeError, ValueError):
        return 20 * 60


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

    def active_title_for(self, chat_id: int) -> str:
        """Most recent unfinished tracked title for this chat (retry subject)."""
        for item in reversed(self._items):
            if item.chat_id == chat_id and not item.done:
                return item.title
        return ""

    def active_service_for(self, chat_id: int, title: str = "") -> str:
        needle = (title or "").strip().lower()
        for item in reversed(self._items):
            if item.chat_id != chat_id or item.done:
                continue
            if not needle or item.title.lower() == needle or needle in item.title.lower():
                return item.service
        return ""

    @property
    def active(self) -> list[TrackedGrab]:
        return [item for item in self._items if not item.done]

    def _note_progress(self, item: TrackedGrab, percent: float | None, now: float) -> None:
        prev = item.last_percent
        if percent is None:
            return
        if prev is None or abs(percent - prev) >= 0.5:
            item.last_percent = percent
            item.last_progress_at = now
        else:
            item.last_percent = percent

    def _idle_stalled(self, item: TrackedGrab, *, now: float, stall_idle_s: float) -> bool:
        """Zero/flat progress for long enough while still 'downloading'."""
        if item.last_status not in {"downloading", "queued", "paused", "warning", "unknown"}:
            return False
        # Need at least one observation before declaring idle stall.
        if not item.last_status:
            return False
        return (now - item.last_progress_at) >= stall_idle_s

    async def _auto_retry(
        self,
        item: TrackedGrab,
        send: Callable[[int, str], Awaitable[Any]],
        *,
        why: str,
    ) -> bool:
        """Try alternate source. Returns True when the grab is finished (exhausted/failed)."""
        client = radarr if item.service == "radarr" else sonarr
        try:
            result = await client.retry_download(
                item.title, force=False, reason=f"auto:{why}"
            )
        except Exception:  # noqa: BLE001
            log.exception("telegram auto-retry failed for %s", item.title)
            await send(item.chat_id, format_failed(item.title, why))
            item.done = True
            return True

        reason = str(result.get("reason") or "")
        if result.get("ok") and reason == "retried":
            item.retry_attempts = int(result.get("attempt") or item.retry_attempts + 1)
            item.announce_retrying = True
            item.announce_started = False
            item.last_status = "downloading"
            item.last_percent = None
            item.last_progress_at = time.monotonic()
            spoken = str(result.get("speak") or "").strip()
            if spoken:
                await send(item.chat_id, spoken)
            else:
                await send(
                    item.chat_id,
                    format_retrying(
                        item.title,
                        indexer=str(result.get("indexer") or ""),
                        attempt=int(result.get("attempt") or 0),
                        max_attempts=int(result.get("max_attempts") or 0),
                    ),
                )
            return False

        if reason == "exhausted":
            await send(
                item.chat_id,
                str(result.get("speak") or "").strip()
                or format_retry_exhausted(
                    item.title, max_attempts=int(result.get("max_attempts") or 0)
                ),
            )
            item.done = True
            return True

        if reason in {"no_alternate", "not_found", "error"}:
            await send(
                item.chat_id,
                str(result.get("speak") or "").strip()
                or format_failed(item.title, reason.replace("_", " ")),
            )
            item.done = True
            return True

        # Healthy / unexpected — fall back to a plain failure notice for failed/stalled.
        await send(item.chat_id, format_failed(item.title, why))
        item.done = True
        return True

    async def poll_once(
        self,
        send: Callable[[int, str], Awaitable[Any]],
        *,
        max_age_s: float = 6 * 3600,
        start_threshold: float = START_THRESHOLD,
        stall_idle_s: float | None = None,
    ) -> None:
        now = time.monotonic()
        idle_limit = float(stall_idle_s) if stall_idle_s is not None else _stall_idle_seconds()
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
                if item.announce_started or item.last_status or item.announce_retrying:
                    await send(item.chat_id, format_done(item.title))
                    item.done = True
                continue
            row = downloads[0]
            status = str(row.get("status") or "unknown").strip().lower()
            percent = _as_percent(row.get("percent"))
            prev_status = item.last_status
            item.last_status = status
            self._note_progress(item, percent, now)

            unhealthy = download_is_unhealthy(row) or status in {"failed", "stalled"}
            idle_stalled = (not unhealthy) and self._idle_stalled(
                item, now=now, stall_idle_s=idle_limit
            )
            if unhealthy or idle_stalled:
                why = status if unhealthy else "stalled"
                await self._auto_retry(item, send, why=why)
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
            # After an auto-retry, allow one fresh start ping for the new source.
            if not item.announce_started and _trustworthy_start_percent(
                percent, threshold=start_threshold
            ):
                await send(item.chat_id, format_downloading(item.title, percent))
                item.announce_started = True
            elif (
                item.announce_retrying
                and prev_status in {"failed", "stalled", ""}
                and status == "downloading"
                and not item.announce_started
            ):
                # Retry just landed; stay quiet until real mid-start percent.
                pass
