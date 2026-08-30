"""Grab orchestration for Telegram inbox requests — reuses *arr / Overseerr."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.parse import (
    MessageView,
    ParsedRequest,
    normalize_title,
    parse_message,
)
from hearth.telegram.progress import (
    ProgressTracker,
    format_already,
    format_ambiguous,
    format_not_found,
    format_queued,
    format_rate_limited,
    format_reject_download,
)
from hearth.telegram.safeguards import Deduper, RateLimiter, chat_allowed, user_allowed
from hearth.tools.arr import overseerr, radarr, sonarr

log = logging.getLogger("hearth.telegram")


@dataclass
class PendingDisambiguation:
    chat_id: int
    options: list[dict[str, Any]]
    media_kind: str
    query: str
    created_message_id: int


@dataclass
class InboxResult:
    handled: bool
    reply: str = ""
    grabbed: bool = False
    title: str = ""
    service: str = ""
    year: int | None = None


@dataclass
class TelegramInbox:
    deduper: Deduper = field(default_factory=Deduper)
    rate: RateLimiter = field(default_factory=RateLimiter)
    progress: ProgressTracker = field(default_factory=ProgressTracker)
    pending: dict[int, PendingDisambiguation] = field(default_factory=dict)
    outbound_message_ids: set[tuple[int, int]] = field(default_factory=set)
    bot_user_id: int | None = None

    def reset(self) -> None:
        self.deduper.reset()
        self.rate.reset()
        self.progress.reset()
        self.pending.clear()
        self.outbound_message_ids.clear()

    def remember_outbound(self, chat_id: int, message_id: int) -> None:
        self.outbound_message_ids.add((int(chat_id), int(message_id)))

    def _is_loop(self, view: MessageView) -> bool:
        if self.bot_user_id is not None and view.user_id == self.bot_user_id:
            return True
        if (view.chat_id, view.message_id) in self.outbound_message_ids:
            return True
        return False

    async def handle_message(self, message: dict[str, Any]) -> InboxResult:
        view, parsed = parse_message(
            message,
            max_length=settings.telegram_max_title_length,
            bot_user_id=self.bot_user_id,
        )
        if view is None:
            return InboxResult(handled=False)

        if not chat_allowed(view.chat_id, settings.telegram_chat_id_list):
            return InboxResult(handled=False, reply="")

        if self._is_loop(view):
            return InboxResult(handled=True, reply="")

        if not user_allowed(
            view.user_id,
            settings.telegram_user_id_list,
            bot_user_id=self.bot_user_id,
        ):
            return InboxResult(handled=True, reply="")

        if self.deduper.seen_message(view.chat_id, view.message_id):
            return InboxResult(handled=True, reply="")

        # Disambiguation pick (1/2/3) for a pending prompt in this chat.
        if parsed.kind == "disambiguation_pick":
            return await self._handle_pick(view, parsed)

        if parsed.kind == "ignore":
            return InboxResult(handled=True, reply="")

        if parsed.kind == "reject_download":
            return InboxResult(handled=True, reply=format_reject_download())

        if parsed.kind != "request":
            return InboxResult(handled=True, reply="")

        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())

        if self.deduper.seen_title(view.chat_id, parsed.dedup_key()):
            return InboxResult(handled=True, reply="")

        return await self._grab(view, parsed)

    async def _handle_pick(self, view: MessageView, parsed: ParsedRequest) -> InboxResult:
        pending = self.pending.get(view.chat_id)
        if not pending or parsed.pick_index is None:
            return InboxResult(handled=True, reply="")
        idx = parsed.pick_index - 1
        if idx < 0 or idx >= len(pending.options):
            return InboxResult(handled=True, reply="Pick 1, 2, or 3 from the list.")
        pick = pending.options[idx]
        del self.pending[view.chat_id]
        synthetic = ParsedRequest(
            kind="request",
            media_kind=pending.media_kind if pending.media_kind in {"movie", "tv"} else (
                "tv" if pick.get("mediaType") == "tv" or pick.get("tvdbId") else "movie"
            ),
            title=str(pick.get("title") or pending.query),
            year=int(pick["year"]) if pick.get("year") not in (None, "") else None,
            tmdb_id=int(pick["tmdbId"]) if pick.get("tmdbId") else (
                int(pick["mediaId"]) if pick.get("mediaId") else None
            ),
            tvdb_id=int(pick["tvdbId"]) if pick.get("tvdbId") else None,
            reason="disambiguation_choice",
        )
        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())
        return await self._grab(view, synthetic, exact=True)

    async def _grab(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        exact: bool = False,
    ) -> InboxResult:
        media_kind = parsed.media_kind
        exact_id = bool(parsed.imdb_id or parsed.tmdb_id or parsed.tvdb_id or exact)

        # Prefer Overseerr when configured (feeds *arr). Fall back to Radarr/Sonarr.
        use_overseerr = settings.telegram_prefer_overseerr and settings.overseerr_configured

        if media_kind == "unknown" and not parsed.tvdb_id:
            # Heuristic: season markers already set tv; otherwise try movie first.
            media_kind = "movie" if parsed.season is None else "tv"

        try:
            if use_overseerr and not parsed.tvdb_id:
                return await self._grab_overseerr(view, parsed, media_kind, exact_id=exact_id)
            if media_kind == "tv" or parsed.tvdb_id:
                return await self._grab_sonarr(view, parsed, exact_id=exact_id)
            return await self._grab_radarr(view, parsed, exact_id=exact_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram grab failed: %s", redact(str(exc)))
            query = parsed.search_query() or parsed.title or "that"
            return InboxResult(
                handled=True,
                reply=f"Couldn't queue '{query}' — *arr look-up failed.",
            )

    async def _search_hits(
        self,
        parsed: ParsedRequest,
        media_kind: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        if media_kind == "tv" or parsed.tvdb_id:
            if parsed.tvdb_id:
                found = await sonarr.search(str(parsed.tvdb_id))
                rows = [r for r in (found.get("results") or []) if r.get("tvdbId") == parsed.tvdb_id]
                if rows:
                    return "sonarr", rows
                # Fixture / broad scan by id.
                broad = await sonarr.search("")
                rows = [r for r in (broad.get("results") or []) if r.get("tvdbId") == parsed.tvdb_id]
                return "sonarr", rows
            query = parsed.title or parsed.search_query()
            if parsed.year and parsed.title:
                # Keep year out of the *arr term — filter by year after lookup.
                query = parsed.title
            found = await sonarr.search(query)
            return "sonarr", list(found.get("results") or [])

        if parsed.tmdb_id:
            found = await radarr.search(f"tmdb:{parsed.tmdb_id}")
            rows = [r for r in (found.get("results") or []) if r.get("tmdbId") == parsed.tmdb_id]
            if rows:
                return "radarr", rows
            broad = await radarr.search("")
            rows = [r for r in (broad.get("results") or []) if r.get("tmdbId") == parsed.tmdb_id]
            return "radarr", rows

        query = parsed.title or parsed.search_query()
        if parsed.imdb_id and not parsed.title:
            query = parsed.imdb_id
        found = await radarr.search(query)
        return "radarr", list(found.get("results") or [])

    def _drop_fixture_fallbacks(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Mock pipeline may attach matched=fallback when nothing fits — treat as miss."""
        return [row for row in hits if row.get("matched") != "fallback"]

    def _filter_hits(
        self,
        hits: list[dict[str, Any]],
        parsed: ParsedRequest,
        *,
        exact_id: bool,
    ) -> list[dict[str, Any]]:
        if exact_id and (parsed.tmdb_id or parsed.tvdb_id or parsed.imdb_id):
            narrowed: list[dict[str, Any]] = []
            for row in hits:
                if parsed.tmdb_id and row.get("tmdbId") == parsed.tmdb_id:
                    narrowed.append(row)
                elif parsed.tvdb_id and row.get("tvdbId") == parsed.tvdb_id:
                    narrowed.append(row)
                elif parsed.imdb_id and row.get("matched") != "fallback":
                    narrowed.append(row)
            if narrowed:
                return narrowed[:1]
            # No id match (ignore fixture fallback for unknown ids).
            return []

        hits = self._drop_fixture_fallbacks(hits)
        if not hits:
            return []

        if parsed.title and parsed.year:
            exact = [
                row
                for row in hits
                if normalize_title(str(row.get("title") or "")) == normalize_title(parsed.title)
                and str(row.get("year") or "") == str(parsed.year)
            ]
            if exact:
                return exact[:1]

        if parsed.title:
            exact_title = [
                row
                for row in hits
                if normalize_title(str(row.get("title") or "")) == normalize_title(parsed.title)
            ]
            if len(exact_title) == 1:
                return exact_title
            if len(exact_title) > 1 and not parsed.year:
                return exact_title[:3]
            # Substring hits from *arr search — single clear hit ok; many → disambiguate.
            if not exact_title:
                if len(hits) == 1:
                    return hits
                return hits[:3]

        # Single hit is fine; multiple fuzzy hits need disambiguation.
        if len(hits) == 1:
            return hits
        return hits[:3]

    async def _already_queued(self, title: str, service: str) -> bool:
        client = radarr if service == "radarr" else sonarr
        try:
            payload = await client.queue(title)
        except Exception:  # noqa: BLE001
            return False
        return bool(payload.get("downloads"))

    async def _grab_radarr(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        exact_id: bool,
    ) -> InboxResult:
        _service, hits = await self._search_hits(parsed, "movie")
        choices = self._filter_hits(hits, parsed, exact_id=exact_id)
        if not choices:
            return InboxResult(
                handled=True,
                reply=format_not_found(parsed.search_query() or parsed.title or "that"),
            )
        if len(choices) > 1 and not exact_id:
            self.pending[view.chat_id] = PendingDisambiguation(
                chat_id=view.chat_id,
                options=choices,
                media_kind="movie",
                query=parsed.title or parsed.search_query(),
                created_message_id=view.message_id,
            )
            return InboxResult(
                handled=True,
                reply=format_ambiguous(parsed.title or parsed.search_query(), choices),
            )
        pick = choices[0]
        title = str(pick.get("title") or parsed.title or "Untitled")
        year = pick.get("year") if pick.get("year") is not None else parsed.year
        year_i = int(year) if year not in (None, "") else None
        if pick.get("inLibrary") or pick.get("hasFile"):
            return InboxResult(handled=True, reply=format_already(title, library=True))
        if await self._already_queued(title, "radarr"):
            return InboxResult(handled=True, reply=format_already(title, queued=True))
        # Also skip if mock pipeline already queued the same title.
        from hearth.fixtures import pipeline

        if any(
            normalize_title(str(row.get("title") or "")) == normalize_title(title)
            for row in pipeline.radarr_queue
        ):
            return InboxResult(handled=True, reply=format_already(title, queued=True))

        tmdb = pick.get("tmdbId") or parsed.tmdb_id
        result = await radarr.add(title, tmdb_id=int(tmdb) if tmdb else None)
        if result.get("ok") is False:
            return InboxResult(
                handled=True,
                reply=format_not_found(parsed.search_query() or title),
            )
        self.progress.track(view.chat_id, title, "radarr", year_i)
        return InboxResult(
            handled=True,
            reply=format_queued(title, year_i, "Radarr"),
            grabbed=True,
            title=title,
            service="radarr",
            year=year_i,
        )

    async def _grab_sonarr(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        exact_id: bool,
    ) -> InboxResult:
        _service, hits = await self._search_hits(parsed, "tv")
        choices = self._filter_hits(hits, parsed, exact_id=exact_id)
        if not choices:
            return InboxResult(
                handled=True,
                reply=format_not_found(parsed.search_query() or parsed.title or "that"),
            )
        if len(choices) > 1 and not exact_id:
            self.pending[view.chat_id] = PendingDisambiguation(
                chat_id=view.chat_id,
                options=choices,
                media_kind="tv",
                query=parsed.title or parsed.search_query(),
                created_message_id=view.message_id,
            )
            return InboxResult(
                handled=True,
                reply=format_ambiguous(parsed.title or parsed.search_query(), choices),
            )
        pick = choices[0]
        title = str(pick.get("title") or parsed.title or "Untitled")
        year = pick.get("year") if pick.get("year") is not None else parsed.year
        year_i = int(year) if year not in (None, "") else None
        if pick.get("inLibrary"):
            return InboxResult(handled=True, reply=format_already(title, library=True))
        if await self._already_queued(title, "sonarr"):
            return InboxResult(handled=True, reply=format_already(title, queued=True))
        from hearth.fixtures import pipeline

        if any(
            normalize_title(str(row.get("title") or "")) == normalize_title(title)
            for row in pipeline.sonarr_queue
        ):
            return InboxResult(handled=True, reply=format_already(title, queued=True))

        tvdb = pick.get("tvdbId") or parsed.tvdb_id
        result = await sonarr.add(title, tvdb_id=int(tvdb) if tvdb else None)
        if result.get("ok") is False:
            return InboxResult(
                handled=True,
                reply=format_not_found(parsed.search_query() or title),
            )
        self.progress.track(view.chat_id, title, "sonarr", year_i)
        return InboxResult(
            handled=True,
            reply=format_queued(title, year_i, "Sonarr"),
            grabbed=True,
            title=title,
            service="sonarr",
            year=year_i,
        )

    async def _grab_overseerr(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        media_kind: str,
        *,
        exact_id: bool,
    ) -> InboxResult:
        query = parsed.search_query() or parsed.title
        if parsed.tmdb_id and not query:
            query = str(parsed.tmdb_id)
        found = await overseerr.search(query)
        hits = list(found.get("results") or [])
        if media_kind in {"movie", "tv"}:
            want = "movie" if media_kind == "movie" else "tv"
            typed = [row for row in hits if (row.get("mediaType") or "") == want]
            if typed:
                hits = typed
        choices = self._filter_hits(hits, parsed, exact_id=exact_id)
        if parsed.tmdb_id:
            id_hits = [
                row
                for row in hits
                if row.get("tmdbId") == parsed.tmdb_id or row.get("mediaId") == parsed.tmdb_id
            ]
            if id_hits:
                choices = id_hits[:1]
        if not choices:
            # Fall back to direct *arr if Overseerr has nothing.
            if media_kind == "tv":
                return await self._grab_sonarr(view, parsed, exact_id=exact_id)
            return await self._grab_radarr(view, parsed, exact_id=exact_id)
        if len(choices) > 1 and not exact_id:
            self.pending[view.chat_id] = PendingDisambiguation(
                chat_id=view.chat_id,
                options=choices,
                media_kind=media_kind,
                query=parsed.title or query,
                created_message_id=view.message_id,
            )
            return InboxResult(
                handled=True,
                reply=format_ambiguous(parsed.title or query, choices),
            )
        pick = choices[0]
        title = str(pick.get("title") or parsed.title or "Untitled")
        year = pick.get("year") if pick.get("year") is not None else parsed.year
        year_i = int(year) if year not in (None, "") else None
        if pick.get("inLibrary"):
            return InboxResult(handled=True, reply=format_already(title, library=True))
        media_type = str(pick.get("mediaType") or media_kind or "movie")
        if media_type not in {"movie", "tv"}:
            media_type = "movie"
        media_id = pick.get("mediaId") or pick.get("tmdbId") or parsed.tmdb_id
        result = await overseerr.request(
            title,
            media_id=int(media_id) if media_id else None,
            media_type=media_type,
        )
        if result.get("ok") is False:
            return InboxResult(handled=True, reply=format_not_found(query or title))
        service = "radarr" if media_type == "movie" else "sonarr"
        self.progress.track(view.chat_id, title, service, year_i)
        return InboxResult(
            handled=True,
            reply=format_queued(title, year_i, "Overseerr"),
            grabbed=True,
            title=title,
            service=service,
            year=year_i,
        )
