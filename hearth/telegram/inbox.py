"""Conversation-first Telegram inbox for movies and series.

The inbox owns Telegram I/O, safety checks, short-lived pending choices, and
tool execution.  ``interpret_intent`` owns the conversation and names titles;
the catalog and Overseerr tools ground and execute those decisions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.catalog import (
    catalog_search_title,
    catalog_seed_matches_title,
)
from hearth.telegram.intent import (
    MAX_BATCH,
    MAX_CANDIDATES,
    CONTEXT_CLUE_CLARIFY,
    SOFT_CONTEXT_CLARIFY,
    IntentDecision,
    clarify_wants_numbered_list,
    instant_pick_decision,
    interpret_intent,
    is_explicit_title_year,
    looks_like_chatter,
    looks_like_concrete_title,
    looks_like_confirm_no,
    looks_like_confirm_yes,
    looks_like_list_ask,
    looks_like_media_ask,
    looks_like_recommend_ask,
    search_title_grounded,
    subject_matches_user_title,
    titles_match,
)
from hearth.telegram.memory import ChatMemory
from hearth.telegram.parse import MessageView, ParsedRequest, normalize_title, parse_message
from hearth.telegram.progress import (
    ProgressTracker,
    format_already,
    format_ambiguous,
    format_guess_confirm,
    format_not_found,
    format_queued,
    format_queued_many,
    format_rate_limited,
    format_reject_download,
)
from hearth.telegram.runner import (
    choices_are_indistinguishable,
    dedupe_choice_rows,
    filter_seed_rows,
    reply_is_banned,
    row_to_parsed,
    tool_lookup_parsed,
    tool_lookup_title,
)
from hearth.telegram.safeguards import Deduper, RateLimiter, chat_allowed, user_allowed

# These module-level imports are intentionally retained. Tests and deployment
# wiring patch these exact clients, and progress/retry still use *arr.
from hearth.tools.arr import overseerr, radarr, sonarr

log = logging.getLogger("hearth.telegram")

PENDING_TTL_S = 15 * 60
SAFE_CLARIFY = "Which movie or series did you mean?"


# --- Public state -----------------------------------------------------------


@dataclass
class PendingDisambiguation:
    chat_id: int
    options: list[dict[str, Any]]
    media_kind: str
    query: str
    created_message_id: int
    created_at: float = field(default_factory=time.monotonic)
    last_bot_reply: str = ""


@dataclass
class InboxResult:
    handled: bool
    reply: str = ""
    grabbed: bool = False
    title: str = ""
    service: str = ""
    year: int | None = None
    titles: list[str] = field(default_factory=list)


@dataclass
class TelegramInbox:
    deduper: Deduper = field(default_factory=Deduper)
    rate: RateLimiter = field(default_factory=RateLimiter)
    progress: ProgressTracker = field(default_factory=ProgressTracker)
    pending: dict[int, PendingDisambiguation] = field(default_factory=dict)
    outbound_message_ids: set[tuple[int, int]] = field(default_factory=set)
    bot_user_id: int | None = None
    memory: ChatMemory = field(default_factory=ChatMemory)

    def reset(self) -> None:
        self.deduper.reset()
        self.rate.reset()
        self.progress.reset()
        self.pending.clear()
        self.outbound_message_ids.clear()
        self.memory.reset()

    def remember_outbound(self, chat_id: int, message_id: int) -> None:
        self.outbound_message_ids.add((int(chat_id), int(message_id)))

    # --- Message lifecycle --------------------------------------------------

    async def handle_message(self, message: dict[str, Any]) -> InboxResult:
        """Parse one Telegram message, interpret it, and run grounded tools."""
        view, parsed = parse_message(
            message,
            max_length=settings.telegram_max_title_length,
            bot_user_id=self.bot_user_id,
        )
        if view is None:
            return InboxResult(handled=False)
        if not chat_allowed(view.chat_id, settings.telegram_chat_id_list):
            return InboxResult(handled=False)
        if self._is_loop(view):
            return InboxResult(handled=True)
        if not user_allowed(
            view.user_id,
            settings.telegram_user_id_list,
            bot_user_id=self.bot_user_id,
        ):
            return InboxResult(handled=True)
        if self.deduper.seen_message(view.chat_id, view.message_id):
            return InboxResult(handled=True)

        # 1) A bare numeric choice is always local while a live menu exists.
        if parsed.kind == "disambiguation_pick":
            result = await self._handle_pick(view, parsed)
            return await self._finish(view, result, user_text=view.text)

        pending = self._pending_for(view.chat_id)

        # 2) Tiny, deterministic pending shortcuts: yes, all, de eerste.
        instant = instant_pick_decision(
            view.text,
            pending.options if pending is not None else None,
        )
        if instant is not None and instant.action in {"pick", "pick_many"}:
            result = await self._apply_intent(view, parsed, instant, pending=pending)
            return await self._finish(
                view,
                result or InboxResult(handled=True),
                search_title=pending.query if pending else "",
                media_kind=pending.media_kind if pending else "",
                user_text=view.text,
            )

        # 3) IDs, catalog URLs, and explicit Title (YYYY) are unambiguous tools.
        if self._is_instant_catalog(parsed, view):
            if not self.rate.allow():
                return await self._finish(
                    view,
                    InboxResult(handled=True, reply=format_rate_limited()),
                    user_text=view.text,
                )
            if self.deduper.seen_title(view.chat_id, parsed.dedup_key()):
                return await self._finish(
                    view, InboxResult(handled=True), user_text=view.text
                )
            self.pending.pop(view.chat_id, None)
            result = await self._resolve_and_grab(view, parsed)
            return await self._finish(
                view,
                result,
                search_title=result.title or parsed.title,
                media_kind=(
                    parsed.media_kind
                    if parsed.media_kind in {"movie", "tv"}
                    else ("tv" if result.service == "sonarr" else "movie")
                    if result.grabbed
                    else ""
                ),
                user_text=view.text,
            )

        # 4) Unsupported download payloads receive the existing house reply.
        if parsed.kind == "reject_download":
            return await self._finish(
                view,
                InboxResult(handled=True, reply=format_reject_download()),
                user_text=view.text,
            )

        # 5) Chatter is silent unless a pending offer makes it conversational.
        if looks_like_chatter(view.text) and pending is None:
            return await self._finish(
                view, InboxResult(handled=True), user_text=view.text
            )

        # 5b) Bare reject of a live Did-you-mean / list: clear pending, never
        # queue that row. Model still sees the thread (+ rejected_titles) and
        # should suggest something else or ask what they want.
        if pending is not None and looks_like_confirm_no(view.text):
            rejected_rows = self._titles_from_options(pending.options)
            if pending.query:
                rejected_rows.append(pending.query)
            self.memory.remember_rejected(
                view.chat_id,
                rejected_rows,
                clear_offered=True,
                clear_subject=False,
            )
            self.pending.pop(view.chat_id, None)
            pending = None

        history = self.memory.history_blob(view.chat_id)
        subject_title, subject_kind = self.memory.subject(view.chat_id)
        rejected = list(self.memory.rejected(view.chat_id))

        # A new ask must not inherit stale candidates from a prior subject.
        if (
            pending is None
            and subject_title
            and not subject_matches_user_title(subject_title, view.text)
            and not looks_like_confirm_yes(view.text)
            and not looks_like_confirm_no(view.text)
        ):
            self.memory.remember_rejected(
                view.chat_id,
                [],
                clear_offered=True,
                clear_subject=True,
            )
            subject_title, subject_kind = "", ""
        elif pending is None and self.memory.offered(view.chat_id):
            self.memory.clear_offered(view.chat_id)

        # Concrete title text gets exact/prefix catalog candidates before the
        # model. Descriptions, Dutch plots, and vibe asks never become queries.
        catalog_rows = await self._catalog_candidates_for_message(
            view,
            parsed,
            rejected_titles=rejected,
        )
        candidates_are_pending = bool(pending and pending.options)
        candidates = (
            list(pending.options)
            if candidates_are_pending and pending is not None
            else list(catalog_rows)
        )
        last_bot = (
            pending.last_bot_reply
            if pending is not None
            else str(history[-1].get("text") or "")
            if history
            else ""
        )

        intent = await interpret_intent(
            view.text,
            candidates=candidates or None,
            pending_query=(pending.query if pending else subject_title) or "",
            last_bot_reply=last_bot,
            force=True,
            history=history,
            subject_title=subject_title,
            subject_media_kind=subject_kind,
            rejected_titles=rejected,
            candidates_are_pending=candidates_are_pending,
        )

        # A one-row offer is a hard binding. A confirm cannot be redirected to
        # an invented model title (Land -> La La Land). Rejects never bind.
        if pending is not None and len(pending.options) == 1:
            if looks_like_confirm_no(view.text):
                # Should already be cleared above; belt-and-suspenders.
                pass
            else:
                model_pick = intent.action in {"pick", "pick_many"}
                ungrounded_search = (
                    intent.action == "search"
                    and bool(intent.search_title.strip())
                    and not search_title_grounded(
                        intent.search_title,
                        user_message=view.text,
                        candidates=pending.options,
                    )
                )
                if (
                    looks_like_confirm_yes(view.text)
                    or model_pick
                    or ungrounded_search
                ):
                    result = await self._grab_catalog_row(
                        view,
                        pending.options[0],
                        query=pending.query,
                        media_kind_hint=pending.media_kind,
                    )
                    return await self._finish(
                        view,
                        result,
                        search_title=pending.query,
                        media_kind=pending.media_kind,
                        user_text=view.text,
                    )

        # Reconcile the model's decision with the still-live on-screen rows.
        if pending is not None:
            intent, pending = self._reconcile_pending_after_intent(
                view.chat_id, pending, intent
            )
            rejected = list(self.memory.rejected(view.chat_id))
            subject_title, subject_kind = self.memory.subject(view.chat_id)

        # A model pick of this turn's catalog candidates gets a temporary,
        # concrete pending object so the shared row-grab path can execute it.
        active_pending = pending
        if (
            active_pending is None
            and intent.action in {"pick", "pick_many"}
            and intent.indices
            and candidates
            and not candidates_are_pending
        ):
            active_pending = PendingDisambiguation(
                chat_id=view.chat_id,
                options=candidates[:MAX_CANDIDATES],
                media_kind=subject_kind
                or str(candidates[0].get("mediaType") or "movie"),
                query=subject_title
                or (catalog_search_title(view.text) or view.text)[:200],
                created_message_id=view.message_id,
                last_bot_reply=last_bot,
            )
            self.pending[view.chat_id] = active_pending

        result = await self._apply_intent(
            view,
            parsed,
            intent,
            pending=active_pending,
            catalog_hits=catalog_rows,
        )
        if result is None:
            result = await self._passthrough_fallback(view, parsed, intent)

        finish_title = intent.search_title if intent.action == "search" else ""
        if intent.action in {"pick", "pick_many"} and active_pending is not None:
            finish_title = active_pending.query
        return await self._finish(
            view,
            result,
            search_title=finish_title,
            media_kind=intent.media_kind or subject_kind,
            offered=(
                self.pending[view.chat_id].options
                if self.pending.get(view.chat_id)
                else []
            ),
            user_text=view.text,
        )

    async def _finish(
        self,
        view: MessageView,
        result: InboxResult,
        *,
        search_title: str = "",
        media_kind: str = "",
        offered: list[dict[str, Any]] | None = None,
        user_text: str = "",
    ) -> InboxResult:
        """Apply the final reply invariant, then record the completed turn."""
        result = await self._gate_reply(view, result)
        if user_text:
            self.memory.record_user(view.chat_id, user_text)

        if result.grabbed:
            self.pending.pop(view.chat_id, None)
            self.memory.clear_offered(view.chat_id)
            offered = []

        if result.reply:
            live = self.pending.get(view.chat_id)
            self.memory.record_bot(
                view.chat_id,
                result.reply,
                search_title=search_title
                or (live.query if live else "")
                or result.title,
                media_kind=media_kind
                or (
                    live.media_kind
                    if live and live.media_kind in {"movie", "tv"}
                    else ""
                ),
                offered=offered if offered is not None else (
                    live.options if live else None
                ),
            )

        if result.grabbed:
            queued = [result.title] if result.title else []
            queued.extend(str(t).strip() for t in result.titles if str(t).strip())
            if queued:
                self.memory.remember_rejected(view.chat_id, queued)
        return result

    async def _gate_reply(
        self, view: MessageView, result: InboxResult
    ) -> InboxResult:
        """Never let a banned canned reply leave the inbox."""
        if not reply_is_banned(result.reply):
            return result

        pending = self._pending_for(view.chat_id)
        if pending is not None:
            if len(pending.options) == 1:
                row = pending.options[0]
                year = self._row_year(row)
                result.reply = format_guess_confirm(
                    str(row.get("title") or pending.query or "that"),
                    year,
                )
                return result
            if pending.options:
                result.reply = format_ambiguous(pending.query or "that", pending.options)
                return result

        prior, prior_kind, prior_year = self._prior_titled_ask(view.chat_id)
        if (
            prior
            and self._followup_should_reuse_prior(view.chat_id, view.text, prior)
        ):
            reused = await self._reuse_prior_title(
                view, prior, media_kind=prior_kind, year=prior_year
            )
            if reused is not None and not reply_is_banned(reused.reply):
                return reused

        concrete = self._concrete_title_from_message(view.text)
        if concrete:
            reused = await self._reuse_prior_title(view, concrete)
            if reused is not None and not reply_is_banned(reused.reply):
                return reused

        result.reply = SAFE_CLARIFY if looks_like_media_ask(view.text) else ""
        return result

    # --- Pending state ------------------------------------------------------

    def _is_loop(self, view: MessageView) -> bool:
        return bool(
            (self.bot_user_id is not None and view.user_id == self.bot_user_id)
            or (view.chat_id, view.message_id) in self.outbound_message_ids
        )

    def _pending_for(self, chat_id: int) -> PendingDisambiguation | None:
        pending = self.pending.get(chat_id)
        if pending is None:
            return None
        if time.monotonic() - pending.created_at > PENDING_TTL_S:
            self.pending.pop(chat_id, None)
            return None
        return pending

    def _remember_pending(
        self,
        view: MessageView,
        *,
        options: list[dict[str, Any]],
        media_kind: str,
        query: str,
        reply: str,
    ) -> None:
        rows = dedupe_choice_rows(options)[:MAX_CANDIDATES]
        kind = media_kind if media_kind in {"movie", "tv"} else "movie"
        self.pending[view.chat_id] = PendingDisambiguation(
            chat_id=view.chat_id,
            options=rows,
            media_kind=kind,
            query=query[:200],
            created_message_id=view.message_id,
            last_bot_reply=reply[:400],
        )
        self.memory.set_subject(
            view.chat_id,
            query,
            media_kind=kind,
            offered=rows,
        )

    def _index_in_pending(
        self, pending: PendingDisambiguation, search_title: str
    ) -> int | None:
        for index, row in enumerate(pending.options, start=1):
            if titles_match(str(row.get("title") or ""), search_title):
                return index
        return None

    def _reconcile_pending_after_intent(
        self,
        chat_id: int,
        pending: PendingDisambiguation,
        intent: IntentDecision,
    ) -> tuple[IntentDecision, PendingDisambiguation | None]:
        """Pick matching rows, clear pivots, and drop bare rejected offers."""
        if intent.action in {"pick", "pick_many"} and intent.indices:
            return intent, pending
        if intent.action == "retry":
            self.pending.pop(chat_id, None)
            return intent, None

        if intent.action == "search" and intent.search_title.strip():
            match = self._index_in_pending(pending, intent.search_title)
            if match is not None:
                return (
                    IntentDecision(
                        action="pick",
                        indices=[match],
                        search_title=intent.search_title,
                        year=intent.year,
                        media_kind=intent.media_kind,
                        people=list(intent.people),
                        confidence=intent.confidence,
                        source=intent.source,
                    ),
                    pending,
                )

            rejected = self._titles_from_options(pending.options)
            same_query = titles_match(pending.query, intent.search_title)
            if pending.query and not same_query:
                rejected.append(pending.query)
            self.memory.remember_rejected(
                chat_id,
                rejected,
                clear_offered=True,
                clear_subject=not same_query,
            )
            self.pending.pop(chat_id, None)
            return intent, None

        if intent.action == "clarify":
            if clarify_wants_numbered_list(intent.clarify_question):
                return intent, pending
            rejected = self._titles_from_options(pending.options)
            if pending.query:
                rejected.append(pending.query)
            self.memory.remember_rejected(
                chat_id,
                rejected,
                clear_offered=True,
                clear_subject=False,
            )
            self.pending.pop(chat_id, None)
            return intent, None

        return intent, pending

    # --- Catalog tools ------------------------------------------------------

    def _is_instant_catalog(self, parsed: ParsedRequest, view: MessageView) -> bool:
        if parsed.kind != "request":
            return False
        if parsed.imdb_id or parsed.tmdb_id or parsed.tvdb_id:
            return True
        return bool(
            parsed.title
            and parsed.year
            and is_explicit_title_year(view.text)
        )

    async def _catalog_candidates_for_message(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        rejected_titles: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not looks_like_concrete_title(view.text):
            return []
        seed = ""
        if parsed.kind == "request" and parsed.title:
            seed = catalog_search_title(parsed.title) or parsed.title
        if not seed:
            seed = catalog_search_title(view.text) or view.text
        rows = await tool_lookup_title(
            seed,
            year=parsed.year if parsed.kind == "request" else None,
            media_kind=(
                parsed.media_kind
                if parsed.kind == "request"
                and parsed.media_kind in {"movie", "tv"}
                else ""
            ),
        )
        return filter_seed_rows(
            rows,
            seed,
            rejected_titles=rejected_titles,
        )[:MAX_CANDIDATES]

    async def _resolve_and_grab(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        select_all: bool = False,
    ) -> InboxResult:
        hits, label = await tool_lookup_parsed(parsed)
        if not hits:
            return InboxResult(
                handled=True,
                reply=format_not_found(label or parsed.display_label()),
            )
        rows = dedupe_choice_rows([hit.as_dict() for hit in hits])
        if select_all and len(rows) > 1:
            return await self._queue_many(
                view,
                parsed.title or label,
                parsed.media_kind,
                rows,
                skip_rate=True,
            )
        if len(rows) > 1 and not choices_are_indistinguishable(rows):
            return self._offer_rows(
                view,
                parsed.title or label or "that",
                rows,
                media_kind=parsed.media_kind,
            )
        return await self._grab_catalog_row(
            view,
            rows[0],
            query=parsed.title or label,
            media_kind_hint=parsed.media_kind,
            skip_rate=True,
        )

    async def _grab_catalog_row(
        self,
        view: MessageView,
        row: dict[str, Any],
        *,
        query: str = "",
        media_kind_hint: str = "",
        skip_rate: bool = False,
    ) -> InboxResult:
        """Queue exactly this row; never turn a title into a substring hit."""
        if not skip_rate and not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())

        current = dict(row)
        title = str(current.get("title") or query or "Untitled")
        year = self._row_year(current)
        kind = self._row_kind(current, media_kind_hint)
        tmdb_id = current.get("tmdbId") or current.get("mediaId")
        tvdb_id = current.get("tvdbId")

        if current.get("inLibrary"):
            return InboxResult(
                handled=True,
                reply=format_already(title, library=True),
                title=title,
                year=year,
            )

        # Guess-confirm rows may not yet have an id. Resolve exact/prefix title
        # plus year so Land (2021) binds to TMDB 688271, never La La Land.
        if tmdb_id in (None, "") and tvdb_id in (None, ""):
            rows = await tool_lookup_title(
                title,
                year=year,
                media_kind=kind,
            )
            if year is not None:
                by_year = [
                    candidate
                    for candidate in rows
                    if self._row_year(candidate) == year
                ]
                if by_year:
                    rows = by_year
            rows = [
                candidate
                for candidate in rows
                if catalog_seed_matches_title(
                    title, str(candidate.get("title") or "")
                )
            ]
            if len(rows) > 1 and not choices_are_indistinguishable(rows):
                return self._offer_rows(
                    view, title, rows, media_kind=kind
                )
            if rows:
                current = dict(rows[0])
                title = str(current.get("title") or title)
                year = self._row_year(current) or year
                kind = self._row_kind(current, kind)

        parsed = row_to_parsed(
            current,
            query=title,
            media_kind_hint=kind,
        )
        if parsed.year is None and year is not None:
            parsed = ParsedRequest(
                kind="request",
                media_kind=parsed.media_kind,
                title=parsed.title,
                year=year,
                tmdb_id=parsed.tmdb_id,
                tvdb_id=parsed.tvdb_id,
                reason="confirmed_tool_row",
            )

        self.pending.pop(view.chat_id, None)
        self.memory.set_subject(
            view.chat_id,
            title,
            media_kind=kind,
            clear_rejected=True,
            clear_offered=True,
        )
        return await self._grab(view, parsed, exact=True)

    async def _grab(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        exact: bool = False,
    ) -> InboxResult:
        kind = parsed.media_kind
        if kind == "unknown" and (parsed.season is not None or parsed.tvdb_id):
            kind = "tv"
        try:
            return await self._grab_overseerr(
                view,
                parsed,
                kind,
                exact=exact,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram Overseerr request failed: %s", redact(str(exc)))
            return InboxResult(
                handled=True,
                reply=f"Couldn't queue '{parsed.display_label()}' through Overseerr.",
            )

    async def _grab_overseerr(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        media_kind: str,
        *,
        exact: bool,
    ) -> InboxResult:
        """Ground one parsed row and request it through the patched client."""
        kind = media_kind if media_kind in {"movie", "tv"} else "movie"
        title = parsed.title or parsed.display_label() or "Untitled"
        year = parsed.year
        media_id = parsed.tmdb_id

        # A confirmed row without an id still requests that exact title. The
        # Overseerr adapter performs its own lookup; Python never substitutes a
        # neighboring substring result.
        if media_id is None and parsed.tvdb_id is None and not exact:
            query = catalog_search_title(title) or title
            found = await overseerr.search(query)
            rows = [
                row
                for row in (found.get("results") or [])
                if row.get("matched") != "fallback"
            ]
            typed = [
                row for row in rows if str(row.get("mediaType") or "") == kind
            ]
            if typed:
                rows = typed
            rows = filter_seed_rows(rows, query)
            if year is not None:
                by_year = [
                    row for row in rows if self._row_year(row) == year
                ]
                if by_year:
                    rows = by_year
            if not rows:
                return InboxResult(
                    handled=True,
                    reply=format_not_found(parsed.display_label()),
                )
            if len(rows) > 1 and not choices_are_indistinguishable(rows):
                return self._offer_rows(view, query, rows, media_kind=kind)
            pick = rows[0]
            title = str(pick.get("title") or title)
            year = self._row_year(pick) or year
            kind = self._row_kind(pick, kind)
            media_id = pick.get("mediaId") or pick.get("tmdbId")
            if pick.get("inLibrary"):
                return InboxResult(
                    handled=True,
                    reply=format_already(title, library=True),
                    title=title,
                    year=year,
                )

        service = "radarr" if kind == "movie" else "sonarr"
        if await self._already_queued(title, service):
            return InboxResult(
                handled=True,
                reply=format_already(title, queued=True),
                title=title,
                service=service,
                year=year,
            )

        from hearth.fixtures import pipeline

        if any(
            normalize_title(str(row.get("title") or row.get("name") or ""))
            == normalize_title(title)
            for row in pipeline.overseerr_queue
        ):
            return InboxResult(
                handled=True,
                reply=format_already(title, queued=True),
                title=title,
                service=service,
                year=year,
            )

        response = await overseerr.request(
            title,
            media_id=int(media_id) if media_id not in (None, "") else None,
            media_type=kind,
        )
        if response.get("ok") is False:
            return InboxResult(
                handled=True,
                reply=f"Couldn't queue '{title}' through Overseerr.",
                title=title,
                service=service,
                year=year,
            )

        self.progress.track(view.chat_id, title, service, year)
        return InboxResult(
            handled=True,
            reply=format_queued(title, year, "Overseerr"),
            grabbed=True,
            title=title,
            service=service,
            year=year,
        )

    async def _already_queued(self, title: str, service: str) -> bool:
        client = radarr if service == "radarr" else sonarr
        try:
            payload = await client.queue(title)
        except Exception:  # noqa: BLE001
            return False
        return bool(payload.get("downloads"))

    # --- Intent execution ---------------------------------------------------

    async def _apply_intent(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        intent: IntentDecision,
        *,
        pending: PendingDisambiguation | None,
        catalog_hits: list[dict[str, Any]] | None = None,
    ) -> InboxResult | None:
        hits = dedupe_choice_rows(list(catalog_hits or []))

        if intent.action == "retry":
            return await self._handle_retry(view, intent)
        if intent.action == "pick" and pending and intent.indices:
            return await self._handle_indices(view, pending, intent.indices[:1])
        if intent.action == "pick_many" and pending and intent.indices:
            return await self._handle_indices(view, pending, intent.indices)

        if intent.action == "ignore":
            if looks_like_chatter(view.text) or not looks_like_media_ask(view.text):
                return InboxResult(handled=True)
            if intent.search_title.strip():
                intent = IntentDecision(
                    action="search",
                    search_title=intent.search_title,
                    year=intent.year,
                    media_kind=intent.media_kind,
                    people=list(intent.people),
                    confidence=intent.confidence,
                    source=intent.source,
                )
            elif pending is not None and len(pending.options) == 1:
                return self._ask_guess_confirm(
                    view, pending.options[0], query=pending.query
                )
            else:
                return InboxResult(handled=True, reply=SAFE_CLARIFY)

        if intent.action == "clarify":
            if hits:
                if len(hits) == 1 or choices_are_indistinguishable(hits):
                    if (
                        looks_like_concrete_title(view.text)
                        or clarify_wants_numbered_list(intent.clarify_question)
                    ):
                        return await self._grab_catalog_row(
                            view, hits[0], query=view.text
                        )
                    return self._ask_guess_confirm(view, hits[0], query=view.text)
                return self._offer_rows(
                    view,
                    catalog_search_title(parsed.title or view.text)
                    or parsed.title
                    or view.text,
                    hits,
                    media_kind=intent.media_kind or parsed.media_kind,
                )

            if intent.search_title.strip():
                return await self._confirm_plot_guess(view, parsed, intent)

            if pending is not None:
                if len(pending.options) == 1:
                    return self._ask_guess_confirm(
                        view, pending.options[0], query=pending.query
                    )
                if pending.options:
                    return self._offer_rows(
                        view,
                        pending.query,
                        pending.options,
                        media_kind=pending.media_kind,
                    )

            concrete = self._concrete_title_from_message(view.text)
            if concrete:
                return await self._reuse_prior_title(
                    view,
                    concrete,
                    media_kind=(
                        intent.media_kind
                        if intent.media_kind in {"movie", "tv"}
                        else ""
                    ),
                    year=intent.year,
                )

            prior, prior_kind, prior_year = self._prior_titled_ask(view.chat_id)
            if (
                prior
                and self._followup_should_reuse_prior(
                    view.chat_id, view.text, prior
                )
            ):
                reused = await self._reuse_prior_title(
                    view,
                    prior,
                    media_kind=prior_kind,
                    year=prior_year,
                )
                if reused is not None:
                    return reused

            question = (intent.clarify_question or "").strip()
            if (
                not question
                or reply_is_banned(question)
                or clarify_wants_numbered_list(question)
                or question in {CONTEXT_CLUE_CLARIFY, SOFT_CONTEXT_CLARIFY}
            ):
                question = SAFE_CLARIFY
            return InboxResult(handled=True, reply=question)

        if intent.action == "search" and intent.search_title.strip():
            # List asks want 2–4 titled options, never a single Did-you-mean.
            if looks_like_list_ask(view.text):
                list_result = await self._offer_list_guesses(
                    view,
                    intent,
                    media_kind_hint=(
                        intent.media_kind
                        if intent.media_kind in {"movie", "tv"}
                        else parsed.media_kind
                    ),
                )
                if list_result is not None:
                    return list_result

            # If the model invents a title while exact user-title rows exist,
            # those grounded rows win.
            if hits and not search_title_grounded(
                intent.search_title,
                user_message=view.text,
                candidates=hits,
            ):
                if len(hits) == 1 or choices_are_indistinguishable(hits):
                    if looks_like_concrete_title(view.text):
                        return await self._grab_catalog_row(
                            view, hits[0], query=view.text
                        )
                    return self._ask_guess_confirm(view, hits[0], query=view.text)
                return self._offer_rows(
                    view,
                    catalog_search_title(parsed.title or view.text)
                    or parsed.title
                    or view.text,
                    hits,
                    media_kind=parsed.media_kind,
                )

            # Plot/vibe/actorless recommendation guesses always need yes/no.
            if not looks_like_concrete_title(view.text):
                return await self._confirm_plot_guess(
                    view, parsed, intent, catalog_hits=hits
                )

            search_title = (
                catalog_search_title(intent.search_title) or intent.search_title
            )
            media_kind = (
                intent.media_kind
                if intent.media_kind in {"movie", "tv"}
                else parsed.media_kind
                if parsed.media_kind in {"movie", "tv"}
                else ""
            )
            self.memory.set_subject(
                view.chat_id,
                search_title,
                media_kind=media_kind,
                clear_rejected=True,
                clear_offered=True,
            )

            rows = filter_seed_rows(hits, search_title) if hits else []
            if not rows:
                rows = await tool_lookup_title(
                    search_title,
                    year=intent.year,
                    media_kind=media_kind,
                )

            if intent.select_all:
                if rows:
                    return await self._queue_many(
                        view,
                        search_title,
                        media_kind,
                        rows,
                    )
                return self._ask_guess_confirm(
                    view,
                    {
                        "title": search_title,
                        "year": intent.year,
                        "mediaType": media_kind or "movie",
                    },
                    query=search_title,
                )

            if len(rows) == 1 or (
                rows and choices_are_indistinguishable(rows)
            ):
                return await self._grab_catalog_row(
                    view,
                    rows[0],
                    query=search_title,
                    media_kind_hint=media_kind,
                )
            if len(rows) > 1:
                return self._offer_rows(
                    view, search_title, rows, media_kind=media_kind
                )

            # A model-named catalog miss is a guess-confirm, never a 404.
            return self._ask_guess_confirm(
                view,
                {
                    "title": search_title,
                    "year": intent.year,
                    "mediaType": media_kind or "movie",
                },
                query=search_title,
            )

        return None

    async def _passthrough_fallback(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        intent: IntentDecision,
    ) -> InboxResult:
        if parsed.kind != "request":
            return InboxResult(handled=True)
        if looks_like_concrete_title(view.text) and parsed.title:
            reused = await self._reuse_prior_title(
                view,
                parsed.title,
                media_kind=(
                    intent.media_kind
                    if intent.media_kind in {"movie", "tv"}
                    else parsed.media_kind
                ),
                year=intent.year or parsed.year,
            )
            if reused is not None:
                return reused
        return InboxResult(
            handled=True,
            reply=SAFE_CLARIFY if looks_like_media_ask(view.text) else "",
        )

    async def _confirm_plot_guess(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        intent: IntentDecision,
        *,
        catalog_hits: list[dict[str, Any]] | None = None,
    ) -> InboxResult:
        title = catalog_search_title(intent.search_title) or intent.search_title
        kind = (
            intent.media_kind
            if intent.media_kind in {"movie", "tv"}
            else parsed.media_kind
            if parsed.media_kind in {"movie", "tv"}
            else ""
        )
        rows = filter_seed_rows(list(catalog_hits or []), title)
        if not rows:
            rows = await tool_lookup_title(
                title,
                year=intent.year,
                media_kind=kind,
            )
        if len(rows) > 1 and not choices_are_indistinguishable(rows):
            return self._offer_rows(view, title, rows, media_kind=kind)
        if rows:
            row = dict(rows[0])
            if row.get("year") in (None, "") and intent.year is not None:
                row["year"] = intent.year
            return self._ask_guess_confirm(view, row, query=title)
        return self._ask_guess_confirm(
            view,
            {
                "title": title,
                "year": intent.year,
                "mediaType": kind or "movie",
            },
            query=title,
        )

    def _ask_guess_confirm(
        self,
        view: MessageView,
        row: dict[str, Any],
        *,
        query: str = "",
    ) -> InboxResult:
        title = str(row.get("title") or query or "Untitled")
        year = self._row_year(row)
        kind = self._row_kind(row)
        option = {
            "title": title,
            "year": year,
            "mediaType": kind,
            "tmdbId": row.get("tmdbId") or row.get("mediaId"),
            "mediaId": row.get("mediaId") or row.get("tmdbId"),
            "tvdbId": row.get("tvdbId"),
            "inLibrary": bool(row.get("inLibrary")),
        }
        reply = format_guess_confirm(title, year)
        self._remember_pending(
            view,
            options=[option],
            media_kind=kind,
            query=title,
            reply=reply,
        )
        return InboxResult(handled=True, reply=reply)

    async def _offer_list_guesses(
        self,
        view: MessageView,
        intent: IntentDecision,
        *,
        media_kind_hint: str = "",
    ) -> InboxResult | None:
        """Build a 2–4 option numbered list for 'name a few more' / options asks."""
        titles: list[str] = []
        for title in list(intent.search_titles or []):
            cleaned = catalog_search_title(title) or str(title).strip()
            if cleaned and cleaned not in titles:
                titles.append(cleaned)
            if len(titles) >= 4:
                break
        primary = catalog_search_title(intent.search_title) or intent.search_title.strip()
        if primary and primary not in titles:
            titles.insert(0, primary)
        titles = titles[:4]
        if len(titles) < 2:
            return None

        kind = media_kind_hint if media_kind_hint in {"movie", "tv"} else ""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for title in titles:
            found = await tool_lookup_title(title, media_kind=kind)
            row: dict[str, Any]
            if found:
                row = dict(found[0])
            else:
                row = {
                    "title": title,
                    "year": intent.year if title == primary else None,
                    "mediaType": kind or "movie",
                }
            label = str(row.get("title") or title).strip()
            key = normalize_title(label)
            if not key or key in seen:
                continue
            # Skip titles the user already rejected / queued this chat.
            if self._title_is_rejected(view.chat_id, label):
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= 4:
                break

        if len(rows) < 2:
            return None
        return self._offer_rows(
            view,
            primary or "a few more",
            rows,
            media_kind=kind or self._row_kind(rows[0]),
        )

    def _offer_rows(
        self,
        view: MessageView,
        query: str,
        rows: list[dict[str, Any]],
        *,
        media_kind: str = "",
    ) -> InboxResult:
        choices = dedupe_choice_rows(rows)[:MAX_CANDIDATES]
        if len(choices) == 1:
            return self._ask_guess_confirm(view, choices[0], query=query)
        reply = format_ambiguous(query or "that", choices)
        kind = self._row_kind(
            choices[0],
            media_kind if media_kind in {"movie", "tv"} else "",
        )
        self._remember_pending(
            view,
            options=choices,
            media_kind=kind,
            query=query or "that",
            reply=reply,
        )
        return InboxResult(handled=True, reply=reply)

    # --- Picks and batch execution -----------------------------------------

    async def _handle_pick(
        self, view: MessageView, parsed: ParsedRequest
    ) -> InboxResult:
        pending = self._pending_for(view.chat_id)
        if pending is None or parsed.pick_index is None:
            return InboxResult(handled=True)
        return await self._handle_indices(view, pending, [parsed.pick_index])

    async def _handle_indices(
        self,
        view: MessageView,
        pending: PendingDisambiguation,
        indices: list[int],
    ) -> InboxResult:
        picks: list[dict[str, Any]] = []
        for number in indices[:MAX_BATCH]:
            index = number - 1
            if index < 0 or index >= len(pending.options):
                return InboxResult(
                    handled=True,
                    reply=(
                        f"Pick 1–{min(3, len(pending.options))} from the list "
                        "(or say 'all of them')."
                    ),
                )
            picks.append(pending.options[index])
        if not picks:
            return InboxResult(handled=True)
        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())

        self.pending.pop(view.chat_id, None)
        if len(picks) == 1:
            return await self._grab_catalog_row(
                view,
                picks[0],
                query=str(picks[0].get("title") or pending.query),
                media_kind_hint=pending.media_kind,
                skip_rate=True,
            )

        queued: list[str] = []
        for row in picks:
            result = await self._grab_catalog_row(
                view,
                row,
                query=str(row.get("title") or pending.query),
                media_kind_hint=pending.media_kind,
                skip_rate=True,
            )
            if result.grabbed and result.title:
                queued.append(
                    f"{result.title} ({result.year})"
                    if result.year
                    else result.title
                )
        if queued:
            return InboxResult(
                handled=True,
                reply=format_queued_many(queued, "Overseerr"),
                grabbed=True,
                titles=queued,
                service="overseerr",
            )
        return InboxResult(
            handled=True,
            reply=f"Nothing new to queue for '{pending.query}'.",
        )

    async def _queue_many(
        self,
        view: MessageView,
        query: str,
        media_kind: str,
        rows: list[dict[str, Any]],
        *,
        skip_rate: bool = False,
    ) -> InboxResult:
        pending = PendingDisambiguation(
            chat_id=view.chat_id,
            options=dedupe_choice_rows(rows)[:MAX_BATCH],
            media_kind=media_kind if media_kind in {"movie", "tv"} else "movie",
            query=query,
            created_message_id=view.message_id,
        )
        self.pending[view.chat_id] = pending
        if skip_rate:
            # Instant caller already consumed the rate token.
            picks = pending.options
            self.pending.pop(view.chat_id, None)
            queued: list[str] = []
            for row in picks:
                result = await self._grab_catalog_row(
                    view,
                    row,
                    query=str(row.get("title") or query),
                    media_kind_hint=pending.media_kind,
                    skip_rate=True,
                )
                if result.grabbed and result.title:
                    queued.append(
                        f"{result.title} ({result.year})"
                        if result.year
                        else result.title
                    )
            if queued:
                return InboxResult(
                    handled=True,
                    reply=format_queued_many(queued, "Overseerr"),
                    grabbed=True,
                    titles=queued,
                    service="overseerr",
                )
            return InboxResult(
                handled=True,
                reply=f"Nothing new to queue for '{query}'.",
            )
        return await self._handle_indices(
            view,
            pending,
            list(range(1, len(pending.options) + 1)),
        )

    # --- Retry and conversation memory -------------------------------------

    async def _handle_retry(
        self, view: MessageView, intent: IntentDecision
    ) -> InboxResult:
        title = intent.search_title.strip()
        subject, subject_kind = self.memory.subject(view.chat_id)
        if not title:
            title = self.progress.active_title_for(view.chat_id) or subject
        if not title:
            return InboxResult(
                handled=True,
                reply="Which download should I retry?",
            )

        media_kind = intent.media_kind or subject_kind
        service = self.progress.active_service_for(view.chat_id, title)
        if not service:
            service = "sonarr" if media_kind == "tv" else "radarr"
        client = radarr if service == "radarr" else sonarr
        try:
            response = await client.retry_download(
                title,
                force=True,
                reason="user:telegram",
            )
        except Exception as exc:  # noqa: BLE001
            log.info("telegram retry failed: %s", redact(str(exc)))
            return InboxResult(handled=True, reply=f"Couldn't retry {title}.")

        if (
            not response.get("ok")
            and response.get("reason") == "not_found"
            and service == "radarr"
            and media_kind != "movie"
        ):
            try:
                response = await sonarr.retry_download(
                    title,
                    force=True,
                    reason="user:telegram",
                )
                service = "sonarr"
            except Exception as exc:  # noqa: BLE001
                log.info("telegram Sonarr retry failed: %s", redact(str(exc)))

        reply = str(response.get("speak") or "").strip()
        if not reply:
            reply = f"Couldn't retry {title}."
        grabbed = bool(response.get("ok"))
        if grabbed:
            self.progress.track(view.chat_id, title, service, intent.year)
            self.memory.set_subject(
                view.chat_id,
                title,
                media_kind="tv" if service == "sonarr" else "movie",
            )
        return InboxResult(
            handled=True,
            reply=reply,
            grabbed=grabbed,
            title=str(response.get("title") or title),
            service=service,
            year=intent.year,
        )

    async def _reuse_prior_title(
        self,
        view: MessageView,
        title: str,
        *,
        media_kind: str = "",
        year: int | None = None,
    ) -> InboxResult | None:
        search_title = catalog_search_title(title) or title.strip()
        if not search_title:
            return None
        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())
        kind = media_kind if media_kind in {"movie", "tv"} else ""
        rows = await tool_lookup_title(
            search_title,
            year=year,
            media_kind=kind,
        )
        if len(rows) == 1 or (
            rows and choices_are_indistinguishable(rows)
        ):
            return await self._grab_catalog_row(
                view,
                rows[0],
                query=search_title,
                media_kind_hint=kind,
                skip_rate=True,
            )
        if len(rows) > 1:
            return self._offer_rows(
                view, search_title, rows, media_kind=kind
            )
        return self._ask_guess_confirm(
            view,
            {
                "title": search_title,
                "year": year,
                "mediaType": kind or "movie",
            },
            query=search_title,
        )

    def _prior_titled_ask(self, chat_id: int) -> tuple[str, str, int | None]:
        subject, kind = self.memory.subject(chat_id)
        if subject.strip():
            return subject.strip(), kind if kind in {"movie", "tv"} else "", None
        history = self.memory.history_blob(chat_id)
        for turn in reversed(history):
            title = str(turn.get("search_title") or "").strip()
            if title:
                media_kind = str(turn.get("media_kind") or "")
                return (
                    title,
                    media_kind if media_kind in {"movie", "tv"} else "",
                    None,
                )
        for turn in reversed(history):
            if turn.get("role") != "user":
                continue
            text = str(turn.get("text") or "").strip()
            if looks_like_concrete_title(text):
                return catalog_search_title(text) or text, "", None
        return "", "", None

    def _followup_should_reuse_prior(
        self, chat_id: int, user_text: str, prior: str
    ) -> bool:
        from hearth.telegram.intent import _significant_tokens

        if not user_text.strip() or not prior.strip():
            return False
        if looks_like_recommend_ask(user_text) or looks_like_list_ask(user_text):
            return False
        if looks_like_confirm_no(user_text) or looks_like_confirm_yes(user_text):
            return False
        if _significant_tokens(prior) & _significant_tokens(user_text):
            return True
        if titles_match(prior, user_text):
            return True
        if self._title_is_rejected(chat_id, prior):
            return False
        lowered = user_text.lower()
        anaphoric = any(
            verb in lowered
            for verb in ("find", "match", "zoek", "resolve", "confirm")
        ) and any(
            pronoun in lowered.split()
            for pronoun in ("that", "it", "this", "die", "dat", "deze")
        )
        if anaphoric:
            return True
        if (
            looks_like_media_ask(user_text)
            and not looks_like_concrete_title(user_text)
            and len(user_text) >= 24
        ):
            return False
        return self._last_bot_was_title_miss_or_guess(chat_id)

    def _last_bot_was_title_miss_or_guess(self, chat_id: int) -> bool:
        for turn in reversed(self.memory.history_blob(chat_id)):
            if turn.get("role") != "bot":
                continue
            text = str(turn.get("text") or "").lower()
            return "couldn't find a match" in text or "did you mean" in text
        return False

    def _title_is_rejected(self, chat_id: int, title: str) -> bool:
        return any(
            titles_match(title, rejected)
            for rejected in self.memory.rejected(chat_id)
        )

    # --- Small row helpers --------------------------------------------------

    def _dedupe_choices(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper retained for tests and older callers."""
        return dedupe_choice_rows(rows)

    def _choices_are_indistinguishable(
        self, rows: list[dict[str, Any]]
    ) -> bool:
        return choices_are_indistinguishable(rows)

    @staticmethod
    def _row_year(row: dict[str, Any]) -> int | None:
        value = row.get("year")
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_kind(row: dict[str, Any], hint: str = "") -> str:
        catalog_kind = str(row.get("mediaType") or row.get("media_kind") or "")
        if catalog_kind in {"movie", "tv"}:
            return catalog_kind
        if row.get("tvdbId"):
            return "tv"
        kind = str(
            hint if hint in {"movie", "tv"} else "movie"
        )
        return kind if kind in {"movie", "tv"} else "movie"

    @staticmethod
    def _titles_from_options(
        options: list[dict[str, Any]] | None,
    ) -> list[str]:
        titles: list[str] = []
        seen: set[str] = set()
        for row in options or []:
            title = str(row.get("title") or "").strip()
            key = title.lower()
            if title and key not in seen:
                seen.add(key)
                titles.append(title)
        return titles

    @staticmethod
    def _concrete_title_from_message(text: str) -> str:
        if not looks_like_concrete_title(text):
            return ""
        return catalog_search_title(text) or text.strip()


__all__ = [
    "PENDING_TTL_S",
    "InboxResult",
    "PendingDisambiguation",
    "TelegramInbox",
]
