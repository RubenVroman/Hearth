"""Conversation-first Telegram inbox for movies and series.

The inbox owns Telegram I/O, safety checks, short-lived pending choices, and
tool execution. Conversation is an OpenAI Chat Completions loop with native
function tools (``hearth.telegram.agent``).

Queueing is HITL only: inline-keyboard ``q:movie:<tmdbId>`` / ``q:tv:<tmdbId>``
or an explicit yes bound to that pending tmdb_id. Free-text "all" / "3" /
"those" / "de eerste" never queues. ``intent.py`` is not the router.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.agent import (
    SessionMode,
    looks_like_correction,
    looks_like_explain,
    run_telegram_agent,
    should_refuse_queue,
)
from hearth.telegram.buttons import (
    GENRE_FANTASY,
    GENRE_SCI_FI,
    genre_hint_from_text,
    is_none_of_these_callback,
    offer_inline_keyboard,
    parse_queue_callback,
    single_get_keyboard,
)
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
    mode: SessionMode = "offer"
    reply_markup: dict[str, Any] | None = None


@dataclass
class InboxResult:
    handled: bool
    reply: str = ""
    grabbed: bool = False
    title: str = ""
    service: str = ""
    year: int | None = None
    titles: list[str] = field(default_factory=list)
    reply_markup: dict[str, Any] | None = None
    mode: SessionMode = "idle"


@dataclass
class TelegramInbox:
    deduper: Deduper = field(default_factory=Deduper)
    rate: RateLimiter = field(default_factory=RateLimiter)
    progress: ProgressTracker = field(default_factory=ProgressTracker)
    pending: dict[int, PendingDisambiguation] = field(default_factory=dict)
    outbound_message_ids: set[tuple[int, int]] = field(default_factory=set)
    bot_user_id: int | None = None
    memory: ChatMemory = field(default_factory=ChatMemory)
    modes: dict[int, SessionMode] = field(default_factory=dict)

    def reset(self) -> None:
        self.deduper.reset()
        self.rate.reset()
        self.progress.reset()
        self.pending.clear()
        self.outbound_message_ids.clear()
        self.memory.reset()
        self.modes.clear()

    def remember_outbound(self, chat_id: int, message_id: int) -> None:
        self.outbound_message_ids.add((int(chat_id), int(message_id)))

    def _set_mode(self, chat_id: int, mode: SessionMode) -> None:
        self.modes[int(chat_id)] = mode

    def _mode(self, chat_id: int) -> SessionMode:
        return self.modes.get(int(chat_id), "idle")

    # --- Message lifecycle --------------------------------------------------

    async def handle_callback(self, callback: dict[str, Any]) -> InboxResult:
        """HITL Get / None-of-these — the only free path that queues by tmdb id."""
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        from_user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        try:
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return InboxResult(handled=False)
        try:
            user_id = int(from_user.get("id") or 0)
        except (TypeError, ValueError):
            user_id = 0
        if not chat_allowed(chat_id, settings.telegram_chat_id_list):
            return InboxResult(handled=False)
        if not user_allowed(
            user_id,
            settings.telegram_user_id_list,
            bot_user_id=self.bot_user_id,
        ):
            return InboxResult(handled=True)

        data = str(callback.get("data") or "")
        if is_none_of_these_callback(data):
            pending = self._pending_for(chat_id)
            if pending is not None:
                rejected = self._titles_from_options(pending.options)
                self.memory.remember_rejected(
                    chat_id, rejected, clear_offered=True, clear_subject=False
                )
                self.pending.pop(chat_id, None)
            self._set_mode(chat_id, "browse")
            return InboxResult(
                handled=True,
                reply="Ok — none of those. What should I look for?",
                mode="browse",
            )

        parsed = parse_queue_callback(data)
        if parsed is None:
            return InboxResult(handled=True)
        media_type, tmdb_id = parsed

        # Prefer the live offer row so title/year stay accurate.
        pending = self._pending_for(chat_id)
        row: dict[str, Any] | None = None
        if pending is not None:
            for option in pending.options:
                oid = option.get("tmdbId") or option.get("mediaId")
                try:
                    if oid is not None and int(oid) == tmdb_id:
                        row = dict(option)
                        break
                except (TypeError, ValueError):
                    continue
        if row is None:
            row = {
                "title": f"tmdb:{tmdb_id}",
                "tmdbId": tmdb_id,
                "mediaId": tmdb_id,
                "mediaType": media_type,
            }

        # Synthetic view for grab helpers.
        view = MessageView(
            chat_id=chat_id,
            message_id=int(message.get("message_id") or 0),
            user_id=user_id,
            text=f"[Get {media_type}:{tmdb_id}]",
            is_bot=False,
        )
        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited(), mode="offer")

        result = await self._grab_catalog_row(
            view,
            row,
            query=str(row.get("title") or ""),
            media_kind_hint=media_type,
            skip_rate=True,
        )
        result.mode = "queued" if result.grabbed else self._mode(chat_id)
        if result.grabbed:
            self._set_mode(chat_id, "queued")
            self.pending.pop(chat_id, None)
        if result.reply:
            self.memory.record_user(chat_id, f"[button Get {tmdb_id}]")
            self.memory.record_bot(
                chat_id,
                result.reply,
                search_title=result.title,
                media_kind=media_type,
            )
            if result.grabbed and result.title:
                self.memory.remember_rejected(chat_id, [result.title])
        return result

    async def handle_message(self, message: dict[str, Any]) -> InboxResult:
        """Parse one Telegram message and run the tool-calling conversation."""
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

        pending = self._pending_for(view.chat_id)

        # Bare 1/2/3 / all-of-them / de eerste MUST NOT queue (second brain killed).
        # Buttons carry the tmdb id; free-text ordinals are ignored or re-prompted.
        if parsed.kind == "disambiguation_pick":
            if pending is not None and pending.options:
                return await self._finish(
                    view,
                    InboxResult(
                        handled=True,
                        reply=(
                            "Tap a Get button for the title you want — "
                            "numbers in chat don't queue."
                        ),
                        reply_markup=pending.reply_markup
                        or offer_inline_keyboard(pending.options),
                        mode="offer",
                    ),
                    user_text=view.text,
                )
            return await self._finish(
                view, InboxResult(handled=True), user_text=view.text
            )

        # Exact Title (YYYY) / catalog URL → search then offer Get (never silent grab).
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
            result = await self._resolve_and_offer(view, parsed)
            return await self._finish(
                view,
                result,
                search_title=result.title or parsed.title,
                media_kind=(
                    parsed.media_kind
                    if parsed.media_kind in {"movie", "tv"}
                    else ""
                ),
                user_text=view.text,
            )

        if parsed.kind == "reject_download":
            return await self._finish(
                view,
                InboxResult(handled=True, reply=format_reject_download()),
                user_text=view.text,
            )

        if (
            looks_like_chatter(view.text)
            and pending is None
            and not looks_like_media_ask(view.text)
            and not looks_like_explain(view.text)
            and not looks_like_correction(view.text)
        ):
            return await self._finish(
                view, InboxResult(handled=True), user_text=view.text
            )

        # Explain mode: apologize, do not re-search unless asked.
        if looks_like_explain(view.text):
            self._set_mode(view.chat_id, "explain")
            apology = (
                "Sorry — that was a bug. A leftover shortcut treated a free-text "
                "reply like a confirm and queued titles without a Get tap. "
                "I won't re-offer that wrong set; say what you want next."
            )
            return await self._finish(
                view,
                InboxResult(handled=True, reply=apology, mode="explain"),
                user_text=view.text,
            )

        rejected_this_turn = False
        if pending is not None and looks_like_confirm_no(view.text):
            rejected_rows = self._titles_from_options(pending.options)
            if pending.query:
                rejected_rows.append(pending.query)
            self.memory.remember_rejected(
                view.chat_id,
                rejected_rows,
                clear_offered=False,
                clear_subject=False,
            )
            rejected_this_turn = True
            self._set_mode(view.chat_id, "browse")

        # Genre correction stays in browse — never queue.
        if looks_like_correction(view.text):
            self._set_mode(view.chat_id, "browse")
            if pending is not None:
                rejected_rows = self._titles_from_options(pending.options)
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

        if (
            pending is None
            and subject_title
            and not subject_matches_user_title(subject_title, view.text)
            and not looks_like_confirm_yes(view.text)
            and not looks_like_confirm_no(view.text)
            and not should_refuse_queue(view.text)
        ):
            self.memory.remember_rejected(
                view.chat_id,
                [],
                clear_offered=True,
                clear_subject=True,
            )
            subject_title, subject_kind = "", ""

        pending_blob = None
        offered_rows: list[dict[str, Any]] = []
        if pending is not None:
            offered_rows = list(pending.options)
            pending_blob = {
                "query": pending.query,
                "media_kind": pending.media_kind,
                "last_bot_reply": pending.last_bot_reply[:400],
                "options": [
                    {
                        "title": str(r.get("title") or ""),
                        "year": r.get("year"),
                        "tmdbId": r.get("tmdbId") or r.get("mediaId"),
                        "mediaType": r.get("mediaType") or pending.media_kind,
                    }
                    for r in pending.options[:8]
                ],
            }
        elif self.memory.offered(view.chat_id):
            offered_rows = list(self.memory.offered(view.chat_id))

        if not self.rate.allow():
            return await self._finish(
                view,
                InboxResult(handled=True, reply=format_rate_limited()),
                user_text=view.text,
            )

        # Explicit yes / download-of-pending bound to a single pending tmdb_id.
        queue_approved = False
        if pending is not None and len(pending.options) == 1:
            pending_title = str(pending.options[0].get("title") or "")
            has_id = bool(
                pending.options[0].get("tmdbId") or pending.options[0].get("mediaId")
            )
            names_pending = bool(
                pending_title
                and normalize_title(pending_title)
                and normalize_title(pending_title) in normalize_title(view.text)
            )
            download_verb = bool(
                re.search(
                    r"\b(?:download|queue|get|bring|add|grab|doe)\b",
                    view.text,
                    re.I,
                )
            )
            queue_approved = has_id and (
                looks_like_confirm_yes(view.text)
                or (names_pending and download_verb)
            )

        session_mode = self._mode(view.chat_id)
        if pending is not None:
            session_mode = "confirm" if len(pending.options) == 1 else "offer"
        elif looks_like_correction(view.text):
            session_mode = "browse"

        handlers = self._tool_handlers(view)
        agent = await run_telegram_agent(
            view.text,
            handlers=handlers,
            history=history,
            pending=pending_blob,
            subject_title=subject_title,
            subject_media_kind=subject_kind,
            rejected_titles=rejected,
            offered=offered_rows,
            mode=session_mode,
            queue_approved=queue_approved,
        )

        result = InboxResult(
            handled=True,
            reply=agent.reply or "",
            grabbed=agent.grabbed,
            title=agent.title,
            year=agent.year,
            titles=list(agent.titles),
            service="overseerr" if agent.grabbed else "",
            reply_markup=agent.reply_markup,
            mode=agent.mode,
        )

        if rejected_this_turn and not result.grabbed:
            self.pending.pop(view.chat_id, None)
            self.memory.clear_offered(view.chat_id)

        live = self.pending.get(view.chat_id)
        if live is not None:
            if not result.reply_markup:
                result.reply_markup = live.reply_markup
            if not result.reply and live.last_bot_reply:
                result.reply = live.last_bot_reply
            self._set_mode(
                view.chat_id,
                "confirm" if len(live.options) == 1 else "offer",
            )
        elif result.mode:
            self._set_mode(view.chat_id, result.mode)

        return await self._finish(
            view,
            result,
            search_title=agent.search_title
            or (self.pending[view.chat_id].query if self.pending.get(view.chat_id) else "")
            or agent.title,
            media_kind=agent.media_kind
            or (
                self.pending[view.chat_id].media_kind
                if self.pending.get(view.chat_id)
                else subject_kind
            ),
            offered=(
                self.pending[view.chat_id].options
                if self.pending.get(view.chat_id)
                else offered_rows
            ),
            user_text=view.text,
        )

    def _tool_handlers(self, view: MessageView) -> dict[str, Any]:
        """Bind per-message tool handlers for the Chat Completions loop."""

        async def search_title(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_search_catalog(view, args)

        async def discover_by_genre(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_discover_by_genre(view, args)

        async def suggest_titles(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_suggest_titles(view, args)

        async def queue_request(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_queue_request(view, args)

        async def library_status(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_already_queued(view, args)

        async def download_progress(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_download_progress(view, args)

        async def retry_download(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_retry_download(view, args)

        return {
            "search_title": search_title,
            "search_catalog": search_title,  # back-compat alias
            "discover_by_genre": discover_by_genre,
            "suggest_titles": suggest_titles,
            "queue_request": queue_request,
            "library_status": library_status,
            "already_queued": library_status,
            "download_progress": download_progress,
            "retry_download": retry_download,
        }

    async def _tool_search_catalog(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        title = catalog_search_title(str(args.get("title") or "")) or str(
            args.get("title") or ""
        ).strip()
        if not title:
            return {"ok": False, "error": "title required"}
        year = args.get("year")
        try:
            year_i = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year_i = None
        media_type = str(args.get("media_type") or args.get("mediaType") or "").strip()
        kind = media_type if media_type in {"movie", "tv"} else ""

        # Prefer the user's concrete seed when the model invents a different
        # title (Christophers + McKellen ≠ Christopher Guest; Land ≠ La La Land).
        user_seed = ""
        if looks_like_concrete_title(view.text):
            user_seed = (
                catalog_search_title(view.text)
                or self._concrete_title_from_message(view.text)
            )
        if (
            user_seed
            and not titles_match(user_seed, title)
            and not catalog_seed_matches_title(user_seed, title)
        ):
            user_rows = await tool_lookup_title(
                user_seed, year=year_i, media_kind=kind
            )
            user_rows = filter_seed_rows(user_rows, user_seed) if user_rows else []
            if user_rows:
                title = user_seed
                rows = user_rows
            else:
                rows = await tool_lookup_title(title, year=year_i, media_kind=kind)
                rows = filter_seed_rows(rows, title) if rows else []
        else:
            rows = await tool_lookup_title(title, year=year_i, media_kind=kind)
            rows = [
                r
                for r in rows
                if catalog_seed_matches_title(title, str(r.get("title") or ""))
                or titles_match(title, str(r.get("title") or ""))
            ]
            rows = filter_seed_rows(rows, title) if rows else []

        # Drop rejected titles.
        rows = [
            r
            for r in rows
            if not self._title_is_rejected(view.chat_id, str(r.get("title") or ""))
        ]

        # Pivoting to a new search clears the prior on-screen offer.
        live = self._pending_for(view.chat_id)
        if live is not None and not titles_match(live.query, title):
            rejected_rows = self._titles_from_options(live.options)
            if live.query:
                rejected_rows.append(live.query)
            self.memory.remember_rejected(
                view.chat_id,
                rejected_rows,
                clear_offered=True,
                clear_subject=False,
            )
            self.pending.pop(view.chat_id, None)

        compact = [
            {
                "title": str(r.get("title") or ""),
                "year": self._row_year(r),
                "tmdbId": r.get("tmdbId") or r.get("mediaId"),
                "mediaType": self._row_kind(r, kind),
                "inLibrary": bool(r.get("inLibrary")),
            }
            for r in rows[:MAX_CANDIDATES]
        ]

        if len(compact) == 1:
            offered = self._ask_guess_confirm(
                view,
                {
                    "title": compact[0]["title"],
                    "year": compact[0]["year"],
                    "tmdbId": compact[0]["tmdbId"],
                    "mediaId": compact[0]["tmdbId"],
                    "mediaType": compact[0]["mediaType"],
                },
                query=title,
            )
            return {
                "ok": True,
                "query": title,
                "media_type": compact[0]["mediaType"],
                "results": compact,
                "count": 1,
                "reply": offered.reply,
                "reply_markup": offered.reply_markup,
                "hint": "Ask the user to confirm or tap Get before queue_request.",
            }

        if len(compact) > 1:
            offered = self._offer_rows(
                view, title, rows[:MAX_CANDIDATES], media_kind=kind
            )
            return {
                "ok": True,
                "query": title,
                "media_type": kind or self._row_kind(rows[0]),
                "results": compact,
                "count": len(compact),
                "reply": offered.reply,
                "reply_markup": offered.reply_markup,
                "hint": "Present the numbered list with Get buttons; wait for a tap or yes.",
            }

        # No catalog hit — still offer a confirm on the model-named title so
        # Eat Pray Love / plot guesses can proceed without a 404/link nag.
        offered = self._ask_guess_confirm(
            view,
            {
                "title": title,
                "year": year_i,
                "mediaType": kind or "movie",
            },
            query=title,
        )
        return {
            "ok": True,
            "query": title,
            "media_type": kind or "movie",
            "results": [],
            "count": 0,
            "reply": offered.reply,
            "reply_markup": offered.reply_markup,
            "hint": "No exact catalog hit; confirm the guessed title with the user.",
        }

    async def _tool_discover_by_genre(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        raw_ids = args.get("genre_ids") or args.get("genreIds") or []
        raw_excl = args.get("exclude_genre_ids") or args.get("excludeGenreIds") or []
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids]
        if not isinstance(raw_excl, list):
            raw_excl = [raw_excl]
        genre_ids: list[int] = []
        for item in raw_ids:
            try:
                genre_ids.append(int(item))
            except (TypeError, ValueError):
                continue
        exclude_ids: list[int] = []
        for item in raw_excl:
            try:
                exclude_ids.append(int(item))
            except (TypeError, ValueError):
                continue

        # Fantasy asks must exclude Sci-Fi even if the model forgets.
        if GENRE_FANTASY in genre_ids and GENRE_SCI_FI not in exclude_ids:
            exclude_ids.append(GENRE_SCI_FI)

        # Also honor genre hints from the user/correction text.
        hint_inc, hint_exc = genre_hint_from_text(view.text)
        for gid in hint_inc:
            if gid not in genre_ids:
                genre_ids.append(gid)
        for gid in hint_exc:
            if gid not in exclude_ids:
                exclude_ids.append(gid)

        if not genre_ids:
            return {"ok": False, "error": "genre_ids required"}

        media_type = str(args.get("media_type") or args.get("mediaType") or "movie").strip()
        kind = media_type if media_type in {"movie", "tv"} else "movie"
        try:
            limit = int(args.get("limit") or 4)
        except (TypeError, ValueError):
            limit = 4
        limit = max(2, min(4, limit))
        query = str(args.get("query") or view.text or "discover").strip()[:120]

        discovered = await overseerr.discover(
            genre_ids=genre_ids,
            exclude_genre_ids=exclude_ids,
            media_type=kind,
            limit=limit + 2,
        )
        rows = list(discovered.get("results") or [])
        rows = [
            r
            for r in rows
            if not self._title_is_rejected(view.chat_id, str(r.get("title") or ""))
        ][:limit]

        if len(rows) < 2:
            return {
                "ok": False,
                "error": "discover_by_genre returned fewer than 2 titles",
                "genre_ids": genre_ids,
                "exclude_genre_ids": exclude_ids,
                "partial": [
                    {"title": str(r.get("title") or ""), "year": self._row_year(r)}
                    for r in rows
                ],
            }

        # Correction copy when rediscovering after a wrong genre set.
        prefix = ""
        if looks_like_correction(view.text):
            prefix = "You're right — those were sci-fi. Fantasy instead:\n"

        offered = self._offer_rows(
            view,
            query or "discover",
            rows,
            media_kind=kind,
            reply_prefix=prefix,
        )
        compact = [
            {
                "title": str(r.get("title") or ""),
                "year": self._row_year(r),
                "tmdbId": r.get("tmdbId") or r.get("mediaId"),
                "mediaType": self._row_kind(r, kind),
            }
            for r in rows
        ]
        self._set_mode(view.chat_id, "offer")
        return {
            "ok": True,
            "query": query,
            "media_type": kind,
            "genre_ids": genre_ids,
            "exclude_genre_ids": exclude_ids,
            "results": compact,
            "count": len(compact),
            "reply": offered.reply,
            "reply_markup": offered.reply_markup,
            "hint": "Present this list with Get buttons. Do not call queue_request.",
        }

    async def _tool_suggest_titles(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        # Genre/vibe asks must go through TMDB discover — never chat-memory sci-fi packs.
        hint_inc, hint_exc = genre_hint_from_text(query or view.text)
        if hint_inc and not (isinstance(args.get("titles"), list) and len(args.get("titles") or []) >= 2):
            return await self._tool_discover_by_genre(
                view,
                {
                    "genre_ids": hint_inc,
                    "exclude_genre_ids": hint_exc,
                    "media_type": args.get("media_type") or "movie",
                    "limit": args.get("limit") or 4,
                    "query": query or view.text,
                },
            )

        raw_titles = args.get("titles")
        titles: list[str] = []
        if isinstance(raw_titles, list):
            for item in raw_titles:
                if isinstance(item, dict):
                    label = str(item.get("title") or item.get("name") or "").strip()
                    year = item.get("year")
                    if label and year not in (None, "") and "(" not in label:
                        label = f"{label} ({year})"
                else:
                    label = str(item or "").strip()
                cleaned = catalog_search_title(label) or label
                if cleaned and cleaned not in titles:
                    titles.append(cleaned)
                if len(titles) >= 4:
                    break

        media_type = str(args.get("media_type") or args.get("type") or "").strip()
        kind = media_type if media_type in {"movie", "tv"} else ""
        try:
            limit = int(args.get("limit") or 4)
        except (TypeError, ValueError):
            limit = 4
        limit = max(2, min(4, limit))

        if len(titles) < 2:
            return {
                "ok": False,
                "error": (
                    "suggest_titles needs 2–4 explicit titles; "
                    "for genre/vibe asks use discover_by_genre"
                ),
            }

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in titles[:limit]:
            seed = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip() or name
            year_m = re.search(r"\((\d{4})\)\s*$", name)
            year_i = int(year_m.group(1)) if year_m else None
            found = await tool_lookup_title(seed, year=year_i, media_kind=kind)
            if found:
                row = dict(found[0])
            else:
                row = {
                    "title": seed,
                    "year": year_i,
                    "mediaType": kind or "movie",
                }
            label = str(row.get("title") or seed).strip()
            key = normalize_title(label)
            if not key or key in seen:
                continue
            if self._title_is_rejected(view.chat_id, label):
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= limit:
                break

        if len(rows) < 2:
            return {
                "ok": False,
                "error": "Could not resolve 2+ distinct titles for suggest_titles",
                "partial": [
                    {"title": str(r.get("title") or ""), "year": self._row_year(r)}
                    for r in rows
                ],
            }

        offered = self._offer_rows(
            view,
            query or "a few more",
            rows,
            media_kind=kind or self._row_kind(rows[0]),
        )
        compact = [
            {
                "title": str(r.get("title") or ""),
                "year": self._row_year(r),
                "tmdbId": r.get("tmdbId") or r.get("mediaId"),
                "mediaType": self._row_kind(r, kind),
            }
            for r in rows
        ]
        return {
            "ok": True,
            "query": query or "a few more",
            "media_type": kind or self._row_kind(rows[0]),
            "results": compact,
            "count": len(compact),
            "reply": offered.reply,
            "reply_markup": offered.reply_markup,
            "hint": "Present this numbered list with Get buttons. Do not call queue_request.",
        }

    async def _tool_queue_request(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        # Belt-and-suspenders: refuse free-text all/3/correction/reject/list.
        if should_refuse_queue(view.text):
            return {
                "ok": False,
                "refused": True,
                "error": (
                    "User rejected, corrected, listed, or used a free-text pick; "
                    "queue_request refused. Use Get buttons or yes for one pending id."
                ),
            }

        year = args.get("year")
        try:
            year_i = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year_i = None
        tmdb_raw = args.get("tmdb_id") or args.get("tmdbId") or args.get("mediaId")
        try:
            tmdb_id = int(tmdb_raw) if tmdb_raw not in (None, "") else None
        except (TypeError, ValueError):
            tmdb_id = None
        media_type = str(args.get("media_type") or args.get("mediaType") or "").strip()
        kind = media_type if media_type in {"movie", "tv"} else "movie"
        title = str(args.get("title") or "").strip()

        pending = self._pending_for(view.chat_id)
        # HITL: queue only when the live pending tmdb_id is targeted and the
        # user is not a refuse/list/correction/bare-pick. Buttons use
        # handle_callback instead. Multi-row free-text never approves.
        approved = False
        row: dict[str, Any] | None = None
        if pending is not None and not should_refuse_queue(view.text):
            if len(pending.options) == 1:
                option = pending.options[0]
                oid = option.get("tmdbId") or option.get("mediaId")
                try:
                    oid_i = int(oid) if oid not in (None, "") else None
                except (TypeError, ValueError):
                    oid_i = None
                pending_title = str(option.get("title") or "")
                names_pending = bool(
                    pending_title
                    and normalize_title(pending_title)
                    and normalize_title(pending_title) in normalize_title(view.text)
                )
                download_verb = bool(
                    re.search(
                        r"\b(?:download|queue|get|bring|add|grab|doe)\b",
                        view.text,
                        re.I,
                    )
                )
                if looks_like_confirm_yes(view.text) or (
                    names_pending and download_verb
                ):
                    row = dict(option)
                    approved = True
                elif tmdb_id is not None and oid_i == tmdb_id and (
                    looks_like_confirm_yes(view.text)
                    or names_pending
                    or download_verb
                ):
                    # Model proposed the on-screen pending id after yes / bring-it.
                    row = dict(option)
                    approved = True
            elif tmdb_id is not None and looks_like_confirm_yes(view.text):
                for option in pending.options:
                    oid = option.get("tmdbId") or option.get("mediaId")
                    try:
                        if oid is not None and int(oid) == tmdb_id:
                            row = dict(option)
                            approved = True
                            break
                    except (TypeError, ValueError):
                        continue

        if not approved or row is None:
            return {
                "ok": False,
                "refused": True,
                "error": (
                    "queue_request requires a Get button callback or an explicit "
                    "yes bound to a pending tmdb_id. Do not queue from free text."
                ),
            }

        if year_i is not None and row.get("year") in (None, ""):
            row["year"] = year_i
        if tmdb_id is not None and not (row.get("tmdbId") or row.get("mediaId")):
            row["tmdbId"] = tmdb_id
            row["mediaId"] = tmdb_id

        result = await self._grab_catalog_row(
            view,
            row,
            query=str(row.get("title") or title),
            media_kind_hint=str(row.get("mediaType") or kind),
            skip_rate=True,
        )
        if result.grabbed:
            self._set_mode(view.chat_id, "queued")
        return {
            "ok": bool(result.grabbed) or "Queued" in (result.reply or ""),
            "grabbed": bool(result.grabbed),
            "title": result.title or str(row.get("title") or title),
            "year": result.year if result.year is not None else self._row_year(row),
            "reply": result.reply,
            "media_type": str(row.get("mediaType") or kind),
        }

    async def _tool_already_queued(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        title = str(args.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title required"}
        media_type = str(args.get("media_type") or "").strip()
        service = "sonarr" if media_type == "tv" else "radarr"
        queued = await self._already_queued(title, service)
        return {
            "ok": True,
            "title": title,
            "queued": queued,
            "service": service,
        }

    async def _tool_download_progress(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        title = str(args.get("title") or "").strip()
        if not title:
            title = self.progress.active_title_for(view.chat_id) or ""
        if not title:
            return {"ok": False, "error": "no active download title"}
        media_type = str(args.get("media_type") or "").strip()
        service = self.progress.active_service_for(view.chat_id, title)
        if not service:
            service = "sonarr" if media_type == "tv" else "radarr"
        client = radarr if service == "radarr" else sonarr
        try:
            payload = await client.queue(title)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "title": title,
            "service": service,
            "downloads": payload.get("downloads") or [],
            "speak": payload.get("speak") or "",
            "reply": str(payload.get("speak") or ""),
        }

    async def _tool_retry_download(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        title = str(args.get("title") or "").strip()
        media_kind = str(args.get("media_type") or args.get("media_kind") or "").strip()
        intent = IntentDecision(
            action="retry",
            search_title=title,
            media_kind=media_kind if media_kind in {"movie", "tv"} else "",
            confidence=0.9,
            source="tool",
        )
        result = await self._handle_retry(view, intent)
        return {
            "ok": True,
            "title": title or result.title,
            "reply": result.reply,
            "grabbed": False,
        }

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
        # Intentional silence (dedup / chatter) stays empty.
        if not (result.reply or "").strip():
            return result

        weak = (
            reply_is_banned(result.reply)
            or result.reply.strip() in {SAFE_CLARIFY, CONTEXT_CLUE_CLARIFY, SOFT_CONTEXT_CLARIFY}
            or "any year, actor" in (result.reply or "").lower()
            or (
                # List-less "reply 1–N" with no actual numbered Title rows.
                bool(re.search(r"reply\s*1\s*[-–—]\s*\d+", result.reply or "", re.I))
                and not re.search(r"^\s*1\.\s+\S", result.reply or "", re.M)
            )
        )
        if not weak:
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
        reply_markup: dict[str, Any] | None = None,
        mode: SessionMode = "offer",
    ) -> None:
        rows = dedupe_choice_rows(options)[:MAX_CANDIDATES]
        kind = media_kind if media_kind in {"movie", "tv"} else "movie"
        if reply_markup is None:
            if len(rows) == 1:
                reply_markup = single_get_keyboard(rows[0])
            elif rows:
                reply_markup = offer_inline_keyboard(rows)
        self.pending[view.chat_id] = PendingDisambiguation(
            chat_id=view.chat_id,
            options=rows,
            media_kind=kind,
            query=query[:200],
            created_message_id=view.message_id,
            last_bot_reply=reply[:400],
            mode=mode if len(rows) != 1 else "confirm",
            reply_markup=reply_markup,
        )
        self._set_mode(view.chat_id, "confirm" if len(rows) == 1 else "offer")
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

    async def _resolve_and_offer(
        self,
        view: MessageView,
        parsed: ParsedRequest,
    ) -> InboxResult:
        """Exact Title (YYYY) / URL → offer Get, never silently grab."""
        hits, label = await tool_lookup_parsed(parsed)
        if not hits:
            return InboxResult(
                handled=True,
                reply=format_not_found(label or parsed.display_label()),
            )
        rows = dedupe_choice_rows([hit.as_dict() for hit in hits])
        if len(rows) > 1 and not choices_are_indistinguishable(rows):
            return self._offer_rows(
                view,
                parsed.title or label or "that",
                rows,
                media_kind=parsed.media_kind,
            )
        return self._ask_guess_confirm(
            view,
            rows[0],
            query=parsed.title or label,
        )

    async def _resolve_and_grab(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        select_all: bool = False,
    ) -> InboxResult:
        # Legacy name retained for older callers/tests — HITL: offer, don't grab.
        del select_all
        return await self._resolve_and_offer(view, parsed)

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
        markup = single_get_keyboard(option)
        self._remember_pending(
            view,
            options=[option],
            media_kind=kind,
            query=title,
            reply=reply,
            reply_markup=markup,
            mode="confirm",
        )
        return InboxResult(
            handled=True,
            reply=reply,
            reply_markup=markup,
            mode="confirm",
            title=title,
            year=year,
        )

    def _offer_rows(
        self,
        view: MessageView,
        query: str,
        rows: list[dict[str, Any]],
        *,
        media_kind: str = "",
        reply_prefix: str = "",
    ) -> InboxResult:
        choices = dedupe_choice_rows(rows)[:MAX_CANDIDATES]
        if len(choices) == 1:
            return self._ask_guess_confirm(view, choices[0], query=query)
        reply = format_ambiguous(query or "that", choices)
        if reply_prefix:
            reply = f"{reply_prefix}{reply}"
        kind = self._row_kind(
            choices[0],
            media_kind if media_kind in {"movie", "tv"} else "",
        )
        markup = offer_inline_keyboard(choices)
        self._remember_pending(
            view,
            options=choices,
            media_kind=kind,
            query=query or "that",
            reply=reply,
            reply_markup=markup,
            mode="offer",
        )
        return InboxResult(
            handled=True,
            reply=reply,
            reply_markup=markup,
            mode="offer",
        )

    async def _offer_list_guesses(
        self,
        view: MessageView,
        intent: IntentDecision,
        *,
        media_kind_hint: str = "",
    ) -> InboxResult | None:
        """Legacy helper — prefer discover_by_genre / suggest_titles tools."""
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
            row = dict(found[0]) if found else {
                "title": title,
                "year": intent.year if title == primary else None,
                "mediaType": kind or "movie",
            }
            label = str(row.get("title") or title).strip()
            key = normalize_title(label)
            if not key or key in seen or self._title_is_rejected(view.chat_id, label):
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

    # --- Picks and batch execution -----------------------------------------

    async def _handle_pick(
        self, view: MessageView, parsed: ParsedRequest
    ) -> InboxResult:
        """Bare numeric picks no longer queue — buttons carry tmdb ids."""
        del parsed
        pending = self._pending_for(view.chat_id)
        if pending is None:
            return InboxResult(handled=True)
        return InboxResult(
            handled=True,
            reply=(
                "Tap a Get button for the title you want — "
                "numbers in chat don't queue."
            ),
            reply_markup=pending.reply_markup
            or offer_inline_keyboard(pending.options),
            mode="offer",
        )

    async def _handle_indices(
        self,
        view: MessageView,
        pending: PendingDisambiguation,
        indices: list[int],
    ) -> InboxResult:
        """Deprecated free-text index grab — never queue from list indices."""
        del indices
        return InboxResult(
            handled=True,
            reply=(
                "Tap a Get button for the title you want — "
                "numbers in chat don't queue."
            ),
            reply_markup=pending.reply_markup
            or offer_inline_keyboard(pending.options),
            mode="offer",
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
        """Never bulk-queue — offer Get buttons instead."""
        del skip_rate
        return self._offer_rows(
            view,
            query,
            rows,
            media_kind=media_kind,
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
