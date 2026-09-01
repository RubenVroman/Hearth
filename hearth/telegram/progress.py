"""Verified progress notifications for Telegram media requests.

Overseerr/Seerr is the source of truth for completion. A title leaving an
*arr queue is ambiguous (it may have imported, failed, or simply disappeared),
so queue absence never becomes a success notification here.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from hearth.tools.arr import download_is_unhealthy, overseerr, radarr, sonarr

log = logging.getLogger("hearth.telegram")

START_THRESHOLD = 5.0
START_PERCENT_MAX = 95.0
_RAW_TMDB_LABEL = re.compile(r"^tmdb:\d+$", re.I)
_FAILED_MEDIA_TEXT = frozenset({"blocked", "blocklisted", "deleted", "failed"})
_FAILED_REQUEST_TEXT = frozenset({"declined", "denied", "failed", "rejected"})


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_title(title: str, year: int | None = None) -> str:
    raw = (title or "").strip()
    if not raw or _RAW_TMDB_LABEL.fullmatch(raw):
        return "that title"
    return f"{raw} ({year})" if year else raw


@dataclass
class TrackedGrab:
    """Primitive-only state for one accepted Overseerr request."""

    chat_id: int
    title: str
    service: str  # radarr | sonarr
    year: int | None = None
    season: int | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    arr_media_id: int | None = None
    media_type: str = ""  # movie | tv
    request_id: int | None = None
    request_key: str = ""
    request_status: int | str | None = None
    started_at: float = field(default_factory=time.time)
    last_status: str = ""
    last_percent: float | None = None
    last_progress_at: float = field(default_factory=time.time)
    announce_started: bool = False
    announce_retrying: bool = False
    retry_attempts: int = 0
    retried_queue_keys: list[str] = field(default_factory=list)
    resolved_retry_keys: list[str] = field(default_factory=list)
    pending_terminal_text: str = ""
    pending_terminal_state: str = ""
    terminal_state: str = ""
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def notification_title(self) -> str:
        return (
            f"{self.title} season {self.season}"
            if self.season is not None
            else self.title
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TrackedGrab:
        allowed = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in allowed})


def format_requested(
    title: str,
    year: int | None,
    via: str = "Overseerr",
    *,
    pending_approval: bool = False,
) -> str:
    """Acknowledge an API request without claiming it entered a queue."""
    label = _display_title(title, year)
    if pending_approval:
        return f"Requested {label} via {via}; waiting for approval."
    return f"Requested {label} via {via}."


def format_queued(title: str, year: int | None, via: str) -> str:
    """Describe a verified queue insertion, not an accepted pending request."""
    return f"Queued {_display_title(title, year)} via {via}."


def format_downloading(title: str, percent: float | None) -> str:
    if percent is None or percent < 0 or percent >= START_PERCENT_MAX:
        return f"{title} is downloading."
    return f"{title} is downloading, ~{percent:g}%."


def format_done(title: str) -> str:
    return f"{title} is done — in Plex."


def format_failed(title: str, detail: str = "") -> str:
    return f"{title} failed{f' ({detail})' if detail else ''}."


def format_expired(title: str) -> str:
    return f"{title} is still unresolved after seven days; I stopped tracking it."


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
        return f"{title} failed — ran out of alternate sources after {max_attempts} tries."
    return f"{title} failed — ran out of alternate sources."


def format_retry_uncertain(title: str) -> str:
    return (
        f"{title} needs attention — an automatic retry had an uncertain outcome. "
        "Check the media queue before retrying it manually."
    )


def format_not_found(query: str) -> str:
    return f"Couldn't find a match for '{query}'."


def format_guess_confirm(title: str, year: int | None = None) -> str:
    return f"Did you mean {_display_title(title, year)}?"


def format_ambiguous(query: str, options: list[dict[str, Any]]) -> str:
    if len(options) == 1:
        row = options[0]
        return format_guess_confirm(
            str(row.get("title") or query or "Untitled"), _integer(row.get("year"))
        )
    rows = options[:4]
    kinds = {str(row.get("mediaType") or "").lower() for row in rows}
    lines = [f"Which one for '{query}'? Tap Get, or None of these:"]
    for index, row in enumerate(rows, start=1):
        label = _display_title(
            str(row.get("title") or "Untitled"), _integer(row.get("year"))
        )
        kind = str(row.get("mediaType") or "").lower()
        if len(kinds) > 1 and kind in {"movie", "tv"}:
            label += f" [{'TV' if kind == 'tv' else 'movie'}]"
        lines.append(f"{index}. {label}")
    return "\n".join(lines)


def format_queued_many(titles: list[str], via: str) -> str:
    if not titles:
        return f"Nothing new to queue via {via}."
    return format_queued(titles[0], None, via)


def format_already(title: str, *, queued: bool = False, library: bool = False) -> str:
    if library:
        return f"{title} is already in the library (in Plex)."
    if queued:
        return f"{title} is already queued."
    return f"{title} is already on the list."


def format_reject_download() -> str:
    return (
        "This bot requests movies and TV through Overseerr — it does not accept "
        "torrent files, magnets, or arbitrary downloads."
    )


def format_rate_limited() -> str:
    return "Too many requests in this group — try again in a minute."


def _as_percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 else None


def _details_body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    body = payload.get("details")
    return body if isinstance(body, dict) else payload


def _media_status(payload: Any) -> int | str | None:
    body = _details_body(payload)
    media = body.get("mediaInfo")
    if not isinstance(media, dict):
        media = body.get("media") if isinstance(body.get("media"), dict) else {}
    status = media.get("status")
    return body.get("mediaStatus") if status is None else status


def _request_rows(payload: Any) -> list[dict[str, Any]]:
    body = _details_body(payload)
    media = body.get("mediaInfo")
    rows: Any = body.get("requests")
    if not isinstance(rows, list) and isinstance(media, dict):
        rows = media.get("requests")
    if not isinstance(rows, list):
        one = body.get("request")
        rows = [one] if isinstance(one, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def _request_row(payload: Any, request_id: int | None) -> dict[str, Any] | None:
    rows = _request_rows(payload)
    if request_id is None:
        # Aggregate/latest request state can belong to another TV season.
        return None
    for row in rows:
        if _integer(row.get("id")) == request_id:
            return row
    return None


def matching_request_row(
    payload: Any,
    request_id: int | None,
    *,
    media_type: str,
    season: int | None,
) -> dict[str, Any] | None:
    """Resolve one exact Overseerr request without using latest/aggregate state.

    A known request id is authoritative.  Crash recovery may not have saved the
    id yet, so it can infer one only when the media/season coordinates leave a
    single candidate.  Ambiguity deliberately remains pending.
    """
    rows = _request_rows(payload)
    if request_id is not None:
        for row in rows:
            if _integer(row.get("id")) == request_id:
                return row
        return None

    candidates = rows
    if media_type == "tv" and season is not None:
        candidates = []
        for row in rows:
            raw_seasons = row.get("seasons")
            if not isinstance(raw_seasons, list):
                continue
            numbers: set[int] = set()
            for value in raw_seasons:
                if isinstance(value, dict):
                    number = _integer(value.get("seasonNumber"))
                else:
                    number = _integer(value)
                if number is not None:
                    numbers.add(number)
            if season in numbers:
                candidates.append(row)
    return candidates[0] if len(candidates) == 1 else None


def _status_text(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ")


def _media_failure(status: Any) -> str:
    number = _integer(status)
    if number in {6, 7}:
        return "blocked or deleted in Overseerr"
    text = _status_text(status)
    return text if text in _FAILED_MEDIA_TEXT else ""


def _request_failure(status: Any) -> str:
    if _integer(status) == 3:
        return "request declined in Overseerr"
    if _integer(status) == 4:
        return "request failed in Overseerr"
    text = _status_text(status)
    return f"request {text} in Overseerr" if text in _FAILED_REQUEST_TEXT else ""


def _queue_key(row: dict[str, Any]) -> str:
    for key in ("queueId", "downloadId", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    return "|".join(
        str(row.get(key) or "").strip().lower()
        for key in ("title", "indexer", "status", "quality")
    )


async def _send_succeeded(
    send: Callable[[int, str], Awaitable[Any]],
    chat_id: int,
    text: str,
) -> bool:
    result = await send(chat_id, text)
    if isinstance(result, dict):
        # A lost Telegram response is at-most-once: retrying could duplicate a
        # message that the API already accepted.
        return result.get("ok") is True or result.get("outcome_unknown") is True
    return True


def _normalized_title(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _verified_live_payload(client: Any, payload: Any) -> bool:
    """Reject fixture/error payloads from real *arr clients.

    Minimal injected test doubles without a ``live`` contract remain usable;
    production Starr clients always expose ``live`` and return an explicit mode.
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return False
    if hasattr(client, "live") and not bool(getattr(client, "live")):
        return False
    mode = str(payload.get("mode") or "").strip().lower()
    if mode == "live":
        return True
    if mode:
        return False
    return not hasattr(client, "live")


def _verified_live_result(client: Any, payload: Any) -> bool:
    """Verify live mutation provenance without requiring operation success."""
    if not isinstance(payload, dict):
        return False
    if hasattr(client, "live") and not bool(getattr(client, "live")):
        return False
    mode = str(payload.get("mode") or "").strip().lower()
    if mode:
        return mode == "live"
    return not hasattr(client, "live")


def _verified_overseerr_details(client: Any, payload: Any) -> bool:
    """Fixture data must never become Plex/request terminal truth."""
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return False
    mode = str(payload.get("mode") or "").strip().lower()
    if hasattr(client, "live") and not bool(getattr(client, "live")):
        return False
    if client is overseerr:
        return payload.get("ok") is True and mode == "live"
    return not mode or mode == "live"


def _matching_queue_row(item: TrackedGrab, rows: Any) -> dict[str, Any] | None:
    """Select one unambiguous queue row; never trust substring ordering."""
    candidates = [row for row in (rows or []) if isinstance(row, dict)]
    stable_identity = False
    arr_key = "movieId" if item.media_type == "movie" else "seriesId"
    if item.arr_media_id is not None:
        identified = [
            row for row in candidates if _integer(row.get(arr_key)) is not None
        ]
        candidates = [
            row
            for row in identified
            if _integer(row.get(arr_key)) == item.arr_media_id
        ]
        if not candidates:
            return None
        stable_identity = True
    if item.media_type == "movie" and item.tmdb_id is not None:
        identified = [row for row in candidates if _integer(row.get("tmdbId")) is not None]
        stable = [row for row in identified if _integer(row.get("tmdbId")) == item.tmdb_id]
        if identified:
            if not stable:
                return None
            candidates = stable
            stable_identity = True

    if item.media_type == "tv":
        identified = [
            row for row in candidates if _integer(row.get("tvdbId")) is not None
        ]
        if item.tvdb_id is not None:
            candidates = [
                row
                for row in identified
                if _integer(row.get("tvdbId")) == item.tvdb_id
            ]
            if not candidates:
                return None
            stable_identity = True
        elif identified:
            # The queue exposes a stable namespace, but this request has no
            # corresponding identifier. A title match is not safe enough.
            return None

    if item.media_type == "tv" and item.season is not None:
        identified = [
            row for row in candidates if _integer(row.get("seasonNumber")) is not None
        ]
        if not identified:
            return None
        candidates = [
            row for row in identified if _integer(row.get("seasonNumber")) == item.season
        ]
        if not candidates:
            return None

    if stable_identity:
        exact = candidates
    else:
        title = _normalized_title(item.title)
        if not title:
            return None
        exact = [
            row
            for row in candidates
            if title
            in {
                _normalized_title(row.get("mediaTitle")),
                _normalized_title(row.get("title")),
            }
        ]
        # A title is a compatibility fallback for old *arr payloads, never a
        # tie-breaker. Multiple exact-title rows could be different releases.
        if len(exact) != 1:
            return None
    # A TV season normally has several episode queue rows. Once stable
    # series/season identity is established, selecting one deterministic row
    # is safe; prefer an unhandled unhealthy row so each failed episode can be
    # retried without ever falling back to a different title.
    unhealthy = [
        row
        for row in exact
        if download_is_unhealthy(row)
        and _queue_key(row) not in item.retried_queue_keys
    ]
    if unhealthy:
        return min(unhealthy, key=_queue_key)
    active = [
        row
        for row in exact
        if _status_text(row.get("status")) in {"downloading", "queued", "paused"}
    ]
    if active:
        return min(
            active,
            key=lambda row: (
                _as_percent(row.get("percent")) is None,
                _as_percent(row.get("percent")) or 0.0,
                _queue_key(row),
            ),
        )
    return min(exact, key=_queue_key)


class ProgressTracker:
    def __init__(
        self,
        *,
        overseerr_client: Any | None = None,
        radarr_client: Any | None = None,
        sonarr_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._items: list[TrackedGrab] = []
        self._overseerr = overseerr_client or overseerr
        self._radarr = radarr_client or radarr
        self._sonarr = sonarr_client or sonarr
        self._clock = clock

    def reset(self) -> None:
        self._items.clear()

    def track(
        self,
        chat_id: int,
        title: str,
        service: str,
        year: int | None = None,
        *,
        season: int | None = None,
        tmdb_id: int | None = None,
        media_type: str = "",
        request_id: int | None = None,
        request_key: str = "",
        request_status: int | str | None = None,
    ) -> TrackedGrab | None:
        title = (title or "").strip()
        if not title:
            return None
        kind = media_type.strip().lower()
        if kind not in {"movie", "tv"}:
            kind = "movie" if service == "radarr" else "tv" if service == "sonarr" else ""
        arr_service = service.strip().lower()
        if arr_service not in {"radarr", "sonarr"}:
            arr_service = "radarr" if kind == "movie" else "sonarr" if kind == "tv" else ""
        media_id = _integer(tmdb_id)
        external_request_id = _integer(request_id)
        incoming_key = str(request_key or "")
        for item in self.active:
            if incoming_key:
                same_request = item.request_key == incoming_key
            elif external_request_id is not None:
                same_request = item.request_id == external_request_id
            elif media_id is not None:
                same_request = (
                    item.request_id is None
                    and item.tmdb_id == media_id
                    and item.media_type == kind
                    and item.season == season
                )
            else:
                same_request = (
                    item.request_id is None
                    and item.tmdb_id is None
                    and item.title.casefold() == title.casefold()
                    and item.media_type == kind
                    and item.service == arr_service
                    and item.season == season
                )
            if item.chat_id == chat_id and same_request:
                item.tmdb_id = media_id or item.tmdb_id
                item.media_type = kind or item.media_type
                item.service = arr_service or item.service
                item.season = season if season is not None else item.season
                item.request_id = external_request_id or item.request_id
                item.request_key = incoming_key or item.request_key
                item.request_status = (
                    request_status if request_status is not None else item.request_status
                )
                return item
        now = self._clock()
        item = TrackedGrab(
            chat_id=chat_id,
            title=title,
            service=arr_service,
            year=year,
            season=season,
            tmdb_id=media_id,
            media_type=kind,
            request_id=external_request_id,
            request_key=str(request_key or ""),
            request_status=request_status,
            started_at=now,
            last_progress_at=now,
        )
        self._items.append(item)
        return item

    def restore(self, rows: list[dict[str, Any]]) -> None:
        self._items = [
            TrackedGrab.from_dict(row) for row in rows if isinstance(row, dict)
        ]

    def dump(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.active]

    def active_title_for(self, chat_id: int) -> str:
        for item in reversed(self._items):
            if item.chat_id == chat_id and not item.done:
                return item.title
        return ""

    def active_service_for(self, chat_id: int, title: str = "") -> str:
        needle = (title or "").strip().casefold()
        for item in reversed(self._items):
            if item.chat_id != chat_id or item.done:
                continue
            if not needle or item.title.casefold() == needle or needle in item.title.casefold():
                return item.service
        return ""

    @property
    def active(self) -> list[TrackedGrab]:
        return [item for item in self._items if not item.done]

    @property
    def completed(self) -> list[TrackedGrab]:
        return [item for item in self._items if item.done]

    def prune_completed(self) -> list[TrackedGrab]:
        completed = self.completed
        self._items[:] = [item for item in self._items if not item.done]
        return completed

    async def _media_details(self, item: TrackedGrab) -> dict[str, Any] | None:
        if item.tmdb_id is None or item.media_type not in {"movie", "tv"}:
            return None
        method = getattr(self._overseerr, "media_details", None)
        if method is None:
            log.warning("Overseerr client has no media_details method")
            return None
        try:
            payload = await method(item.tmdb_id, item.media_type)
        except Exception:  # noqa: BLE001 - polling must not stop the bot
            log.exception("telegram Overseerr progress poll failed for %s", item.title)
            return None
        if not _verified_overseerr_details(self._overseerr, payload):
            log.warning("ignored unverified Overseerr progress result for %s", item.title)
            return None
        return payload

    def _apply_request_state(self, item: TrackedGrab, payload: dict[str, Any]) -> str:
        body = _details_body(payload)
        media = body.get("mediaInfo")
        if isinstance(media, dict):
            item.arr_media_id = (
                _integer(media.get("externalServiceId")) or item.arr_media_id
            )
            if item.media_type == "tv":
                item.tvdb_id = _integer(media.get("tvdbId")) or item.tvdb_id
        row = _request_row(payload, item.request_id)
        if row:
            item.request_id = _integer(row.get("id")) or item.request_id
            if row.get("status") is not None:
                item.request_status = row["status"]
        failure = _media_failure(_media_status(payload))
        return failure or _request_failure(item.request_status)

    @staticmethod
    async def _finish_after_send(
        item: TrackedGrab,
        send: Callable[[int, str], Awaitable[Any]],
        *,
        state: str,
        text: str,
        checkpoint: Callable[[TrackedGrab], Awaitable[Any]] | None = None,
    ) -> bool:
        item.pending_terminal_text = text
        item.pending_terminal_state = state
        if checkpoint is not None:
            await checkpoint(item)
        if not await _send_succeeded(send, item.chat_id, text):
            return False
        item.pending_terminal_text = ""
        item.pending_terminal_state = ""
        item.terminal_state = state
        item.done = True
        if checkpoint is not None:
            await checkpoint(item)
        return True

    async def _auto_retry(
        self,
        item: TrackedGrab,
        row: dict[str, Any],
        send: Callable[[int, str], Awaitable[Any]],
        checkpoint: Callable[[TrackedGrab], Awaitable[Any]] | None = None,
    ) -> None:
        failure_key = _queue_key(row)
        if failure_key in item.retried_queue_keys:
            if failure_key in item.resolved_retry_keys:
                return
            if item.pending_terminal_text:
                await self._finish_after_send(
                    item,
                    send,
                    state=item.pending_terminal_state or "uncertain",
                    text=item.pending_terminal_text,
                    checkpoint=checkpoint,
                )
            else:
                await self._finish_after_send(
                    item,
                    send,
                    state="uncertain",
                    text=format_retry_uncertain(item.notification_title),
                    checkpoint=checkpoint,
                )
            return
        client = self._radarr if item.service == "radarr" else self._sonarr
        try:
            max_attempts = max(0, int(getattr(client, "max_retries", 3)))
        except (TypeError, ValueError):
            max_attempts = 3
        used_attempts = max(item.retry_attempts, len(item.retried_queue_keys))
        if used_attempts >= max_attempts:
            await self._finish_after_send(
                item,
                send,
                state="failed",
                text=format_retry_exhausted(
                    item.notification_title,
                    max_attempts=max_attempts,
                ),
                checkpoint=checkpoint,
            )
            return
        item.retried_queue_keys.append(failure_key)
        # Persist the one-shot guard before mutating *arr. If the process dies
        # after the grab, restart recovery must not submit the retry again.
        if checkpoint is not None:
            try:
                await checkpoint(item)
            except BaseException:
                item.retried_queue_keys.remove(failure_key)
                raise
        status = _status_text(row.get("status")) or "failed"
        try:
            result = await client.retry_download(
                item.title,
                force=False,
                reason=f"auto:{status}",
                queue_id=_integer(
                    row.get("queueId")
                    if row.get("queueId") is not None
                    else row.get("id")
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception("telegram alternate-source retry failed for %s", item.title)
            await self._finish_after_send(
                item,
                send,
                state="uncertain",
                text=format_retry_uncertain(item.notification_title),
                checkpoint=checkpoint,
            )
            return

        if not _verified_live_result(client, result):
            log.warning("ignored unverified *arr retry result for %s", item.title)
            return
        reason = _status_text(result.get("reason"))
        if result.get("ok") and reason == "retried":
            item.retry_attempts = (
                _integer(result.get("attempt")) or item.retry_attempts + 1
            )
            item.announce_retrying = True
            item.announce_started = False
            item.last_percent = None
            if failure_key not in item.resolved_retry_keys:
                item.resolved_retry_keys.append(failure_key)
            if checkpoint is not None:
                await checkpoint(item)
            await send(
                item.chat_id,
                str(result.get("speak") or "").strip()
                or format_retrying(
                    item.notification_title,
                    indexer=str(result.get("indexer") or ""),
                    attempt=_integer(result.get("attempt")) or 0,
                    max_attempts=_integer(result.get("max_attempts")) or 0,
                ),
            )
            return

        if reason in {"healthy", "not found", "not_found"}:
            # The queue changed between observation and retry lookup. Its final
            # outcome is ambiguous; keep watching Overseerr instead of lying.
            if failure_key not in item.resolved_retry_keys:
                item.resolved_retry_keys.append(failure_key)
            if checkpoint is not None:
                await checkpoint(item)
            return
        if reason in {"error", "unavailable"}:
            await self._finish_after_send(
                item,
                send,
                state="uncertain",
                text=format_retry_uncertain(item.notification_title),
                checkpoint=checkpoint,
            )
            return
        if reason == "exhausted":
            text = format_retry_exhausted(
                item.notification_title,
                max_attempts=_integer(result.get("max_attempts")) or 0,
            )
        else:
            text = format_failed(item.notification_title, reason or status)
        await self._finish_after_send(
            item,
            send,
            state="failed",
            text=str(result.get("speak") or "").strip() or text,
            checkpoint=checkpoint,
        )

    async def poll_once(
        self,
        send: Callable[[int, str], Awaitable[Any]],
        *,
        max_age_s: float = 7 * 24 * 3600,
        start_threshold: float = START_THRESHOLD,
        stall_idle_s: float | None = None,
        checkpoint: Callable[[TrackedGrab], Awaitable[Any]] | None = None,
    ) -> None:
        """Poll verified state once; queue absence is deliberately a no-op."""
        del stall_idle_s  # compatibility: flat progress is not proof of failure
        now = self._clock()
        for item in list(self.active):
            if item.pending_terminal_text:
                await self._finish_after_send(
                    item,
                    send,
                    state=item.pending_terminal_state or "uncertain",
                    text=item.pending_terminal_text,
                    checkpoint=checkpoint,
                )
                continue
            if now - item.started_at > max_age_s:
                await self._finish_after_send(
                    item,
                    send,
                    state="expired",
                    text=format_expired(item.notification_title),
                    checkpoint=checkpoint,
                )
                continue

            details = await self._media_details(item)
            if details is not None:
                if _integer(_media_status(details)) == 5:
                    await self._finish_after_send(
                        item,
                        send,
                        state="available",
                        text=format_done(item.notification_title),
                        checkpoint=checkpoint,
                    )
                    continue
                failure = self._apply_request_state(item, details)
                if failure:
                    await self._finish_after_send(
                        item,
                        send,
                        state="failed",
                        text=format_failed(item.notification_title, failure),
                        checkpoint=checkpoint,
                    )
                    continue
                if _integer(item.request_status) == 5:
                    await self._finish_after_send(
                        item,
                        send,
                        state="available",
                        text=format_done(item.notification_title),
                        checkpoint=checkpoint,
                    )
                    continue

            if item.service not in {"radarr", "sonarr"}:
                continue
            client = self._radarr if item.service == "radarr" else self._sonarr
            try:
                # Fetch the whole bounded queue, then apply stable ids locally.
                # Server-side substring filtering can discard localized or
                # alternate-title rows before exact TMDB/TVDB matching.
                payload = await client.queue("")
            except Exception:  # noqa: BLE001
                log.exception("telegram *arr progress poll failed for %s", item.title)
                continue
            if not _verified_live_payload(client, payload):
                log.warning("ignored unverified *arr queue result for %s", item.title)
                continue
            row = _matching_queue_row(item, payload.get("downloads"))
            if row is None:
                # Missing from *arr is not evidence of import or availability.
                continue

            status = _status_text(row.get("status")) or "unknown"
            percent = _as_percent(row.get("percent"))
            item.last_status = status
            if percent is not None:
                if item.last_percent is None or abs(item.last_percent - percent) >= 0.5:
                    item.last_progress_at = now
                item.last_percent = percent

            if download_is_unhealthy(row) or status in {"failed", "stalled"}:
                await self._auto_retry(item, row, send, checkpoint)
                continue

            # Completed/importing still require Overseerr status 5 later.
            if status != "downloading" or item.announce_started:
                continue
            trustworthy = (
                percent is None or start_threshold <= percent < START_PERCENT_MAX
            )
            if trustworthy:
                shown = (
                    percent
                    if percent is not None and percent >= start_threshold
                    else None
                )
                if await _send_succeeded(
                    send,
                    item.chat_id,
                    format_downloading(item.notification_title, shown),
                ):
                    item.announce_started = True
                    item.announce_retrying = False
