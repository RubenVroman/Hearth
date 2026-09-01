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
    claims_overseerr_catalog,
    extract_person_name,
    looks_like_asking_for_others,
    looks_like_correction,
    looks_like_encyclopedia_dump,
    looks_like_exhausted_offer_reply,
    looks_like_explain,
    looks_like_person_ask,
    looks_like_person_followup,
    resolve_person_query,
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
    parse_release_callback,
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
    looks_like_named_title_year,
    looks_like_recommend_ask,
    search_title_grounded,
    subject_matches_user_title,
    titles_match,
)
from hearth.telegram.memory import ChatMemory
from hearth.telegram.offer import (
    is_short_seed,
    movie_tv_hits,
    normalize_offer_row,
    pick_offer_for_title,
    resolve_offer,
)
from hearth.telegram.parse import (
    MessageView,
    ParsedRequest,
    normalize_title,
    parse_message,
    strip_title_year_media,
)
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
DISCOVER_VOTE_COUNT_GTE = 200
_TITLE_YEAR = re.compile(
    r"([A-Z][\w'&.:\-]*(?:\s+[A-Z][\w'&.:\-]*){0,6})\s*\((\d{4})\)"
)


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
    # "title" = Overseerr Get offer; "release" = Radarr alternate-release switch/keep-both.
    offer_kind: str = "title"
    # When offer_kind=release: True = keep library file (extra download).
    keep_existing: bool = False


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
    # Person-name typo confirm (no Get yet) — Yeah continues search_person credits.
    pending_person: dict[int, dict[str, Any]] = field(default_factory=dict)
    outbound_message_ids: set[tuple[int, int]] = field(default_factory=set)
    bot_user_id: int | None = None
    memory: ChatMemory = field(default_factory=ChatMemory)
    modes: dict[int, SessionMode] = field(default_factory=dict)

    def reset(self) -> None:
        self.deduper.reset()
        self.rate.reset()
        self.progress.reset()
        self.pending.clear()
        self.pending_person.clear()
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
            genre_hint = ""
            was_release = bool(pending and pending.offer_kind == "release")
            if pending is not None:
                rejected = self._titles_from_options(pending.options)
                self.memory.remember_shown(chat_id, pending.options)
                self.memory.remember_rejected(
                    chat_id, rejected, clear_offered=True, clear_subject=False
                )
                genre_hint = str(pending.query or "")
                self.pending.pop(chat_id, None)
            self._set_mode(chat_id, "browse")
            # Release-switch dismiss — do not auto-fetch a genre pack.
            if was_release:
                return InboxResult(
                    handled=True,
                    reply="Ok — leaving that release alone. What should I look for?",
                    mode="browse",
                )
            # Auto-fetch the next pack when we still know the genre cursor.
            cursor = self.memory.discover_cursor(chat_id)
            if cursor.get("genre_ids"):
                view = MessageView(
                    chat_id=chat_id,
                    message_id=int(message.get("message_id") or 0),
                    user_id=user_id,
                    text=genre_hint or "others",
                    is_bot=False,
                )
                nxt = await self._tool_discover_by_genre(
                    view,
                    {
                        "genre_ids": cursor["genre_ids"],
                        "exclude_genre_ids": cursor.get("exclude_genre_ids") or [],
                        "media_type": cursor.get("media_type") or "movie",
                        "page": int(cursor.get("page") or 1) + 1,
                        "query": genre_hint or "others",
                        "limit": 4,
                    },
                )
                if nxt.get("ok") and nxt.get("reply"):
                    reply = str(nxt["reply"])
                    if not reply.lower().startswith("ok"):
                        reply = f"Ok — none of those. Here are some others:\n{reply}"
                    self.memory.record_user(chat_id, "[button None of these]")
                    self.memory.record_bot(
                        chat_id,
                        reply,
                        search_title=str(nxt.get("query") or genre_hint or ""),
                        media_kind=str(nxt.get("media_type") or "movie"),
                        offered=list(nxt.get("results") or []),
                    )
                    return InboxResult(
                        handled=True,
                        reply=reply,
                        reply_markup=nxt.get("reply_markup")
                        if isinstance(nxt.get("reply_markup"), dict)
                        else None,
                        mode="offer",
                    )
                # Exhausted — no Get buttons for a list we couldn't refresh.
                fail = (
                    "Ok — none of those. I'm out of fresh options in that genre "
                    "right now. Want a different genre or a specific title?"
                )
                self.memory.record_user(chat_id, "[button None of these]")
                self.memory.record_bot(chat_id, fail)
                return InboxResult(handled=True, reply=fail, mode="browse")
            return InboxResult(
                handled=True,
                reply="Ok — none of those. What should I look for?",
                mode="browse",
            )

        release_parsed = parse_release_callback(data)
        if release_parsed is not None:
            media_type, token = release_parsed
            pending = self._pending_for(chat_id)
            if pending is None or pending.offer_kind != "release":
                return InboxResult(
                    handled=True,
                    reply="That release offer expired — ask again if you still want another version.",
                    mode="idle",
                )
            row: dict[str, Any] | None = None
            for option in pending.options:
                if str(option.get("releaseToken") or "") == token:
                    row = dict(option)
                    break
            if row is None:
                return InboxResult(
                    handled=True,
                    reply=(
                        "That Get button is from an older list — "
                        "tap one on the current offer."
                    ),
                    reply_markup=pending.reply_markup
                    or offer_inline_keyboard(pending.options),
                    mode="offer",
                )
            view = MessageView(
                chat_id=chat_id,
                message_id=int(message.get("message_id") or 0),
                user_id=user_id,
                text=f"[Get release:{token}]",
                is_bot=False,
            )
            if not self.rate.allow():
                return InboxResult(handled=True, reply=format_rate_limited(), mode="offer")
            return await self._grab_pending_release(view, row, pending=pending)

        parsed = parse_queue_callback(data)
        if parsed is None:
            return InboxResult(handled=True)
        media_type, tmdb_id = parsed

        # Prefer the live offer row so title/year stay accurate. A Get tap for
        # an id that is NOT on the current offer is a stale button (e.g. old
        # Spider-Man Get while a 90s sci-fi list is live) — never queue it.
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
                return InboxResult(
                    handled=True,
                    reply=(
                        "That Get button is from an older list — "
                        "tap one on the current offer."
                    ),
                    reply_markup=pending.reply_markup
                    or offer_inline_keyboard(pending.options),
                    mode="offer",
                )
        else:
            # No live offer: ignore redeliveries / expired buttons. Never invent
            # a queue from a raw tmdb id outside the current Get row.
            if self.deduper.seen_tmdb(chat_id, tmdb_id):
                return InboxResult(handled=True, reply="", mode=self._mode(chat_id))
            return InboxResult(
                handled=True,
                reply="That offer expired — send the title again if you still want it.",
                mode="idle",
            )

        # Duplicate Telegram deliveries / double-taps of the same Get.
        if self.deduper.seen_tmdb(chat_id, tmdb_id):
            return InboxResult(
                handled=True,
                reply="",
                mode=self._mode(chat_id),
            )

        row = await self._ensure_human_title(row, tmdb_id, media_type)

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
        # Belt-and-suspenders: never post "Queued tmdb:123".
        if result.reply and "tmdb:" in result.reply.lower():
            human = str(row.get("title") or result.title or "").strip()
            if human and not re.match(r"^tmdb:\d+$", human, re.I):
                result.reply = format_queued(
                    human, result.year or self._row_year(row), "Overseerr"
                )
            else:
                result.reply = format_queued("that title", result.year, "Overseerr")
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
            and view.chat_id not in self.pending_person
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

        # Person-name typo confirm: Yeah → credits; Nah → clear (never catalog miss).
        person_pending = self.pending_person.get(view.chat_id)
        if person_pending is None and looks_like_confirm_yes(view.text):
            person_pending = self._person_confirm_from_history(view.chat_id)
            if person_pending is not None:
                self.pending_person[view.chat_id] = person_pending
        if person_pending is not None and looks_like_confirm_no(view.text):
            self.pending_person.pop(view.chat_id, None)
            self._set_mode(view.chat_id, "browse")
            return await self._finish(
                view,
                InboxResult(
                    handled=True,
                    reply="Ok — who should I look up instead?",
                    mode="browse",
                ),
                user_text=view.text,
            )
        if person_pending is not None and looks_like_confirm_yes(view.text):
            # Title Did-you-mean / single Get always wins over person recovery —
            # even when mediaId is not yet bound (Yes resolves via Overseerr).
            title_pending = self._pending_for(view.chat_id)
            if title_pending is not None and len(title_pending.options) == 1:
                person_pending = None
                self.pending_person.pop(view.chat_id, None)
            if person_pending is not None:
                if not self.rate.allow():
                    return await self._finish(
                        view,
                        InboxResult(handled=True, reply=format_rate_limited()),
                        user_text=view.text,
                    )
                forced = await self._tool_search_person(
                    view,
                    {
                        "name": str(person_pending.get("name") or ""),
                        "person_id": person_pending.get("personId"),
                        "confirmed": True,
                        "media_type": person_pending.get("media_type") or "movie",
                        "limit": int(person_pending.get("limit") or 4),
                    },
                )
                result = InboxResult(
                    handled=True,
                    reply=str(forced.get("reply") or ""),
                    reply_markup=forced.get("reply_markup")
                    if isinstance(forced.get("reply_markup"), dict)
                    else None,
                    mode="offer" if forced.get("count", 0) > 1 else "confirm",
                )
                return await self._finish(
                    view,
                    result,
                    search_title=str(
                        forced.get("query") or person_pending.get("name") or ""
                    ),
                    media_kind=str(forced.get("media_type") or "movie"),
                    offered=(
                        self.pending[view.chat_id].options
                        if self.pending.get(view.chat_id)
                        else None
                    ),
                    user_text=view.text,
                )

        # Release-switch HITL: yes on a single pending release grabs that guid;
        # multi-offer yes never auto-grabs ("all of them" / casual yes).
        if pending is not None and pending.offer_kind == "release":
            if looks_like_confirm_no(view.text):
                self.pending.pop(view.chat_id, None)
                self._set_mode(view.chat_id, "browse")
                return await self._finish(
                    view,
                    InboxResult(
                        handled=True,
                        reply="Ok — leaving that release alone. What should I look for?",
                        mode="browse",
                    ),
                    user_text=view.text,
                )
            if looks_like_confirm_yes(view.text) or re.search(
                r"\b(?:download|queue|get|bring|add|grab|doe)\b",
                view.text,
                re.I,
            ):
                if len(pending.options) == 1:
                    if not self.rate.allow():
                        return await self._finish(
                            view,
                            InboxResult(handled=True, reply=format_rate_limited()),
                            user_text=view.text,
                        )
                    return await self._finish(
                        view,
                        await self._grab_pending_release(
                            view, pending.options[0], pending=pending
                        ),
                        user_text=view.text,
                    )
                return await self._finish(
                    view,
                    InboxResult(
                        handled=True,
                        reply=(
                            "Tap a Get button for the release you want — "
                            "I won't grab from 'yes' / 'all of them' on a list."
                        ),
                        reply_markup=pending.reply_markup
                        or offer_inline_keyboard(pending.options),
                        mode="offer",
                    ),
                    user_text=view.text,
                )

        # Title Did-you-mean / single Get: Yes MUST queue THAT pending row via
        # Overseerr (resolve tmdb_id if needed). Never re-run a model search that
        # invents "not in catalog" when Overseerr has the title.
        if pending is None and looks_like_confirm_yes(view.text):
            recovered = self._title_confirm_from_history(view.chat_id)
            if recovered is not None:
                self.pending[view.chat_id] = recovered
                pending = recovered
        if (
            pending is not None
            and pending.offer_kind != "release"
            and len(pending.options) == 1
            and not should_refuse_queue(view.text)
        ):
            option = pending.options[0]
            pending_title = str(option.get("title") or pending.query or "")
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
                if not self.rate.allow():
                    return await self._finish(
                        view,
                        InboxResult(handled=True, reply=format_rate_limited()),
                        user_text=view.text,
                    )
                result = await self._grab_catalog_row(
                    view,
                    dict(option),
                    query=pending_title or pending.query,
                    media_kind_hint=str(
                        option.get("mediaType") or pending.media_kind or "movie"
                    ),
                    skip_rate=True,
                )
                return await self._finish(
                    view,
                    result,
                    search_title=result.title or pending_title,
                    media_kind=str(
                        option.get("mediaType") or pending.media_kind or ""
                    ),
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

        # "others" / "you've just mentioned these" — keep shown ids, stay browse.
        if looks_like_asking_for_others(view.text) and pending is not None:
            self.memory.remember_shown(view.chat_id, pending.options)
            rejected_rows = self._titles_from_options(pending.options)
            self.memory.remember_rejected(
                view.chat_id,
                rejected_rows,
                clear_offered=False,
                clear_subject=False,
            )
            self._set_mode(view.chat_id, "browse")

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

        # Actor / "movies with X" / person follow-ups after a miss: always use
        # Overseerr multi-search person rows + combined_credits — never title search.
        person_query = resolve_person_query(view.text, history)
        if (
            person_query
            and (
                looks_like_person_ask(view.text)
                or looks_like_person_followup(view.text, history)
            )
            and not looks_like_confirm_yes(view.text)
            and not looks_like_confirm_no(view.text)
        ):
            forced = await self._tool_search_person(
                view,
                {"name": person_query, "media_type": "movie", "limit": 4},
            )
            result = InboxResult(
                handled=True,
                reply=str(forced.get("reply") or ""),
                reply_markup=(
                    forced["reply_markup"]
                    if isinstance(forced.get("reply_markup"), dict)
                    else None
                ),
                mode=(
                    "confirm"
                    if forced.get("confirm_person")
                    or (forced.get("count") or 0) <= 1
                    else "offer"
                ),
            )
            return await self._finish(
                view,
                result,
                search_title=str(forced.get("query") or person_query),
                media_kind=str(forced.get("media_type") or "movie"),
                offered=(
                    self.pending[view.chat_id].options
                    if self.pending.get(view.chat_id)
                    else None
                ),
                user_text=view.text,
            )

        # Explicit yes / download-of-pending bound to a single pending title.
        # tmdb_id may still be missing (Did-you-mean before resolve) — Yes still
        # queues after Overseerr search+mediaId request.
        queue_approved = False
        if pending is not None and len(pending.options) == 1:
            pending_title = str(pending.options[0].get("title") or "")
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
            queue_approved = looks_like_confirm_yes(view.text) or (
                names_pending and download_verb
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
            shown_tmdb_ids=self.memory.shown_tmdb_ids(view.chat_id),
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

        # Named title+year must never leave as a Wikipedia/RT encyclopedia dump.
        if (
            not result.grabbed
            and looks_like_encyclopedia_dump(result.reply)
            and (
                looks_like_named_title_year(view.text)
                or looks_like_concrete_title(view.text)
            )
            and not looks_like_person_ask(view.text)
        ):
            forced = await self._tool_search_catalog(
                view,
                {
                    "title": catalog_search_title(view.text) or view.text,
                    "year": (
                        parsed.year
                        if parsed.kind == "request" and parsed.year
                        else strip_title_year_media(view.text)[1]
                    ),
                    "media_type": (
                        parsed.media_kind
                        if parsed.kind == "request"
                        and parsed.media_kind in {"movie", "tv"}
                        else "movie"
                    ),
                },
            )
            if forced.get("ok") and forced.get("reply"):
                result.reply = str(forced["reply"])
                if isinstance(forced.get("reply_markup"), dict):
                    result.reply_markup = forced["reply_markup"]
                result.mode = "confirm" if forced.get("count") == 1 else "offer"
                agent.search_title = str(forced.get("query") or agent.search_title)

        # Actor / "movies with X" must use Overseerr person credits — never
        # title-only search, never "couldn't find in the catalog".
        if not result.grabbed and (
            looks_like_person_ask(view.text)
            or looks_like_person_followup(view.text, history)
            or claims_overseerr_catalog(result.reply)
        ):
            used_person = any(
                str(t.get("name") or "") == "search_person" for t in agent.tools_used
            )
            person_ok = used_person and (
                bool(result.reply_markup)
                or self.pending_person.get(view.chat_id) is not None
            )
            missish = bool(
                re.search(
                    r"(?i)couldn'?t find|could not find|not in the catalog|"
                    r"no (?:specific )?(?:movies|films).{0,40}(?:catalog|found)|"
                    r"check (?:the )?spelling|try another spelling",
                    result.reply or "",
                )
            )
            want_person = looks_like_person_ask(view.text) or looks_like_person_followup(
                view.text, history
            )
            if want_person and (
                not person_ok or missish or claims_overseerr_catalog(result.reply)
            ):
                name = resolve_person_query(view.text, history) or extract_person_name(
                    " ".join(
                        str(t.get("args", {}).get("name") or "")
                        for t in agent.tools_used
                        if t.get("name") == "search_person"
                    )
                )
                if name:
                    forced = await self._tool_search_person(
                        view,
                        {"name": name, "media_type": "movie", "limit": 4},
                    )
                    # Always replace catalog-miss wording for person asks —
                    # even when the person lookup itself returns ok=False.
                    if forced.get("reply"):
                        result.reply = str(forced["reply"])
                        result.reply_markup = (
                            forced["reply_markup"]
                            if isinstance(forced.get("reply_markup"), dict)
                            else None
                        )
                        if forced.get("confirm_person"):
                            result.mode = "confirm"
                        else:
                            result.mode = (
                                "offer" if (forced.get("count") or 0) > 1 else "confirm"
                            )
                        agent.search_title = str(
                            forced.get("query") or name or agent.search_title
                        )
            elif claims_overseerr_catalog(result.reply):
                # Non-person ask that leaked Overseerr-as-catalog wording.
                result.reply = re.sub(
                    r"(?i)\b(?:the\s+)?overseerr\s+catalog\b",
                    "the movie catalog",
                    result.reply,
                )
                result.reply = re.sub(
                    r"(?i)I(?:'m| am) using (?:the )?Overseerr\b[^.?!]*[.?!]?",
                    "",
                    result.reply,
                ).strip()

        if rejected_this_turn and not result.grabbed:
            self.pending.pop(view.chat_id, None)
            self.memory.clear_offered(view.chat_id)

        live = self.pending.get(view.chat_id)
        if live is not None:
            if looks_like_exhausted_offer_reply(result.reply):
                # Don't show Get buttons for a list we just said we couldn't refresh.
                result.reply_markup = None
                self.pending.pop(view.chat_id, None)
                self.memory.clear_offered(view.chat_id)
                self._set_mode(view.chat_id, "browse")
            elif not result.reply_markup:
                if looks_like_asking_for_others(view.text):
                    # Asking for a new pack without a fresh offer — drop stale Get ids.
                    result.reply_markup = None
                    self.pending.pop(view.chat_id, None)
                    self.memory.clear_offered(view.chat_id)
                    self._set_mode(view.chat_id, "browse")
                else:
                    result.reply_markup = live.reply_markup
                    if not result.reply and live.last_bot_reply:
                        result.reply = live.last_bot_reply
                    self._set_mode(
                        view.chat_id,
                        "confirm" if len(live.options) == 1 else "offer",
                    )
            elif self.pending.get(view.chat_id) is live:
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

        async def search_person(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_search_person(view, args)

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

        async def web_search(args: dict[str, Any]) -> dict[str, Any]:
            return await self._tool_web_search(view, args)

        return {
            "search_title": search_title,
            "search_catalog": search_title,  # back-compat alias
            "discover_by_genre": discover_by_genre,
            "search_person": search_person,
            "suggest_titles": suggest_titles,
            "queue_request": queue_request,
            "library_status": library_status,
            "already_queued": library_status,
            "download_progress": download_progress,
            "retry_download": retry_download,
            "web_search": web_search,
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
        # Pull year from the user message when the model omits it
        # ("Miss you love you 2026 film").
        if year_i is None:
            _seed, year_from_msg = strip_title_year_media(view.text)
            if year_from_msg is not None:
                year_i = year_from_msg
            else:
                _seed2, year_from_title = strip_title_year_media(
                    str(args.get("title") or "")
                )
                if year_from_title is not None:
                    year_i = year_from_title
        media_type = str(args.get("media_type") or args.get("mediaType") or "").strip()
        kind = media_type if media_type in {"movie", "tv"} else ""
        if not kind and re.search(r"(?i)\b(?:films?|movies?|flicks?)\b", view.text):
            kind = "movie"

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
                rows = await self._overseerr_title_hits(
                    title, year=year_i, media_kind=kind
                )
        else:
            rows = await self._overseerr_title_hits(
                title, year=year_i, media_kind=kind
            )

        # Drop rejected titles.
        rows = [
            r
            for r in rows
            if not self._title_is_rejected(view.chat_id, str(r.get("title") or ""))
        ]

        # Exact-ish catalog miss → house web_search then search_title on the
        # resolved name (Land → Land (2021) Robin Wright). Never invent La La Land.
        from_web = False
        if not rows and (
            looks_like_concrete_title(title)
            or looks_like_concrete_title(view.text)
            or len(title.split()) <= 6
        ):
            web_rows = await self._title_web_search_resolve(
                view,
                title,
                year=year_i,
                media_kind=kind,
            )
            if web_rows:
                rows = web_rows
                from_web = True

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
                    "inLibrary": compact[0]["inLibrary"],
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
                "source": "web_search" if from_web else "catalog",
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

        # No grounded offer yet — for multi-word titles only, if raw Overseerr
        # search still returned movie/tv, pick the top hit and offer Get.
        # Short seeds stay exact (Land must never become La La Land via raw[0]).
        if not compact and not is_short_seed(title):
            try:
                found = await overseerr.search(
                    catalog_search_title(title) or title
                )
            except Exception:  # noqa: BLE001
                found = {"results": []}
            raw_hits = movie_tv_hits(
                found.get("results") if isinstance(found, dict) else None
            )
            kind_hint = kind if kind in {"movie", "tv"} else ""
            if kind_hint:
                typed = [
                    r
                    for r in raw_hits
                    if str(r.get("mediaType") or "") == kind_hint
                ]
                if typed:
                    raw_hits = typed
            if raw_hits:
                pick = (
                    pick_offer_for_title(
                        raw_hits, title, year=year_i, media_kind=kind
                    )
                    or raw_hits[0]
                )
                offered = self._ask_guess_confirm(view, pick, query=title)
                return {
                    "ok": True,
                    "query": title,
                    "media_type": str(pick.get("mediaType") or kind or "movie"),
                    "results": [pick],
                    "count": 1,
                    "reply": offered.reply,
                    "reply_markup": offered.reply_markup,
                    "hint": (
                        "Overseerr search returned movie/tv; offered top match. "
                        "On yes, queue_request uses mediaId — never format_not_found."
                    ),
                    "source": "catalog",
                }

        # Truly zero movie/tv from Overseerr — confirm the model guess so Yes
        # can re-search. Never claim "not in catalog" here.
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
            "hint": (
                "No Overseerr movie/tv rows yet; confirm the guessed title. "
                "On yes, resolve_offer searches again and requests by mediaId."
            ),
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

        # "others" with empty genre_ids → reuse last discover cursor.
        cursor = self.memory.discover_cursor(view.chat_id)
        if not genre_ids and cursor.get("genre_ids"):
            genre_ids = list(cursor["genre_ids"])
            for gid in cursor.get("exclude_genre_ids") or []:
                if gid not in exclude_ids:
                    exclude_ids.append(int(gid))

        if not genre_ids:
            return {"ok": False, "error": "genre_ids required"}

        media_type = str(args.get("media_type") or args.get("mediaType") or "").strip()
        if media_type not in {"movie", "tv"}:
            media_type = str(cursor.get("media_type") or "movie")
        kind = media_type if media_type in {"movie", "tv"} else "movie"
        try:
            limit = int(args.get("limit") or 4)
        except (TypeError, ValueError):
            limit = 4
        limit = max(2, min(4, limit))
        try:
            page = int(args.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        if page < 1:
            # Bump page when the user asks for others / none-of-these.
            if looks_like_asking_for_others(view.text) and cursor.get("genre_ids"):
                page = int(cursor.get("page") or 1) + 1
            else:
                page = 1
        query = str(args.get("query") or view.text or "discover").strip()[:120]
        today = self._amsterdam_today()
        ban_ids = self._discover_exclude_ids(view.chat_id)

        rows: list[dict[str, Any]] = []
        used_page = page
        for attempt in range(page, page + 3):
            discovered = await overseerr.discover(
                genre_ids=genre_ids,
                exclude_genre_ids=exclude_ids,
                media_type=kind,
                limit=limit + 4,
                page=attempt,
                primary_release_date_lte=today,
                vote_count_gte=DISCOVER_VOTE_COUNT_GTE,
                exclude_tmdb_ids=ban_ids,
            )
            batch = list(discovered.get("results") or [])
            batch = [
                r
                for r in batch
                if not self._row_is_excluded(view.chat_id, r, ban_ids)
            ]
            if batch:
                rows = batch[:limit]
                used_page = attempt
                break

        source = "discover"
        if len(rows) < 2:
            fallback = await self._discover_web_search_fallback(
                view,
                genre_ids=genre_ids,
                media_kind=kind,
                limit=limit,
                ban_ids=ban_ids,
                query=query,
            )
            if fallback is not None:
                rows = fallback
                source = "web_search"

        self.memory.set_discover_cursor(
            view.chat_id,
            genre_ids=genre_ids,
            exclude_genre_ids=exclude_ids,
            page=used_page,
            media_type=kind,
        )

        if len(rows) < 2:
            # Clear stale pending so we never re-attach the same Get buttons.
            self.pending.pop(view.chat_id, None)
            self.memory.clear_offered(view.chat_id)
            self._set_mode(view.chat_id, "browse")
            return {
                "ok": False,
                "error": (
                    "No fresh released titles left in this genre. "
                    "Ask for a different genre or a specific title — "
                    "do not re-attach the previous Get buttons."
                ),
                "genre_ids": genre_ids,
                "exclude_genre_ids": exclude_ids,
                "page": used_page,
                "reply": (
                    "I'm out of fresh released options in that genre right now. "
                    "Want a different genre or a specific title?"
                ),
                "reply_markup": None,
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
        self.memory.remember_shown(view.chat_id, compact)
        self._set_mode(view.chat_id, "offer")
        return {
            "ok": True,
            "query": query,
            "media_type": kind,
            "genre_ids": genre_ids,
            "exclude_genre_ids": exclude_ids,
            "page": used_page,
            "source": source,
            "primary_release_date_lte": today,
            "vote_count_gte": DISCOVER_VOTE_COUNT_GTE,
            "results": compact,
            "count": len(compact),
            "reply": offered.reply,
            "reply_markup": offered.reply_markup,
            "hint": "Present this list with Get buttons. Do not call queue_request.",
        }

    async def _tool_search_person(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Overseerr multi-search person + released movie credits with Get buttons.

        Uses the same ``GET /api/v1/search?query=`` the UI uses (movies/TV/people),
        keeps person rows, then ``/api/v1/person/{id}/combined_credits``. Never
        title-search an actor name. Never claim the browse catalog is Overseerr.
        """
        name = str(args.get("name") or args.get("query") or "").strip()
        if not name:
            name = extract_person_name(view.text)
        if not name:
            return {
                "ok": False,
                "error": "person name required",
                "reply": "Which actor or person should I look up?",
            }
        media_type = str(args.get("media_type") or args.get("mediaType") or "").strip()
        if media_type not in {"movie", "tv"}:
            # Default movie for "movies with …"; Dutch "films met" too.
            if re.search(r"(?i)\b(?:shows?|series|tv)\b", view.text) and not re.search(
                r"(?i)\b(?:movies?|films?|flicks?)\b", view.text
            ):
                media_type = "tv"
            else:
                media_type = "movie"
        try:
            limit = int(args.get("limit") or 4)
        except (TypeError, ValueError):
            limit = 4
        limit = max(2, min(4, limit))
        confirmed = bool(args.get("confirmed"))
        person_id = args.get("person_id") or args.get("personId")
        try:
            person_id_i = int(person_id) if person_id not in (None, "") else None
        except (TypeError, ValueError):
            person_id_i = None

        person_name = name
        if person_id_i is None:
            found = await overseerr.search_person(name)
            people = list(found.get("results") or [])
            if not people:
                self.pending_person.pop(view.chat_id, None)
                return {
                    "ok": False,
                    "query": name,
                    "error": f"No person match for {name!r}",
                    "reply": (
                        f"I couldn't match anyone named {name}. "
                        "Want to try a different name?"
                    ),
                    "reply_markup": None,
                }
            pick = people[0]
            try:
                person_id_i = int(pick.get("id"))
            except (TypeError, ValueError):
                person_id_i = None
            person_name = str(pick.get("name") or name).strip() or name
            if person_id_i is None:
                return {
                    "ok": False,
                    "query": name,
                    "error": "person id missing",
                    "reply": f"I found {person_name} but couldn't load credits.",
                }

            # Typo / spelling correction — wait for Yeah before credits.
            if (
                not confirmed
                and normalize_title(person_name) != normalize_title(name)
            ):
                self.pending.pop(view.chat_id, None)
                self.pending_person[view.chat_id] = {
                    "name": person_name,
                    "personId": person_id_i,
                    "media_type": media_type,
                    "limit": limit,
                    "query": name,
                }
                self._set_mode(view.chat_id, "confirm")
                reply = f"Did you mean {person_name}?"
                return {
                    "ok": True,
                    "query": person_name,
                    "person_id": person_id_i,
                    "person_name": person_name,
                    "confirm_person": True,
                    "count": 0,
                    "results": [],
                    "reply": reply,
                    "reply_markup": None,
                    "hint": (
                        "Wait for Yeah/Yes before listing credits. "
                        "Do not call search_title or claim a catalog miss."
                    ),
                    "source": "tmdb_person",
                }
        else:
            # Confirmed path may still carry the corrected name.
            pending = self.pending_person.get(view.chat_id) or {}
            if pending.get("name"):
                person_name = str(pending["name"])

        self.pending_person.pop(view.chat_id, None)
        credits = await overseerr.person_combined_credits(person_id_i)
        rows = self._person_credit_rows(
            credits,
            media_type=media_type,
            limit=limit,
            ban_ids=self._discover_exclude_ids(view.chat_id),
        )
        if len(rows) < 1:
            return {
                "ok": False,
                "query": person_name,
                "person_id": person_id_i,
                "error": "no released credits",
                "reply": (
                    f"I matched {person_name} but don't have released "
                    f"{'movies' if media_type == 'movie' else 'shows'} to offer yet."
                ),
                "reply_markup": None,
                "source": "tmdb_person",
            }

        label = f"movies with {person_name}" if media_type == "movie" else (
            f"shows with {person_name}"
        )
        offered = self._offer_rows(
            view,
            label,
            rows,
            media_kind=media_type,
            reply_prefix=f"A few with {person_name}:\n",
        )
        compact = [
            {
                "title": str(r.get("title") or ""),
                "year": self._row_year(r),
                "tmdbId": r.get("tmdbId") or r.get("mediaId"),
                "mediaType": self._row_kind(r, media_type),
            }
            for r in rows
        ]
        self.memory.remember_shown(view.chat_id, compact)
        self._set_mode(view.chat_id, "offer" if len(compact) > 1 else "confirm")
        return {
            "ok": True,
            "query": person_name,
            "person_id": person_id_i,
            "person_name": person_name,
            "media_type": media_type,
            "results": compact,
            "count": len(compact),
            "reply": offered.reply,
            "reply_markup": offered.reply_markup,
            "hint": "Present this list with Get buttons. Do not call queue_request.",
            "source": "tmdb_person",
        }

    def _person_credit_rows(
        self,
        credits: dict[str, Any],
        *,
        media_type: str,
        limit: int,
        ban_ids: list[int] | set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Pick popular released cast credits (skip unreleased filler)."""
        today = self._amsterdam_today()
        ban = set(ban_ids or [])
        kind = media_type if media_type in {"movie", "tv"} else "movie"
        cast = list(credits.get("cast") or [])
        scored: list[tuple[float, dict[str, Any]]] = []
        seen: set[int] = set()
        for row in cast:
            if not isinstance(row, dict):
                continue
            mt = str(row.get("mediaType") or row.get("media_type") or "").strip()
            if mt and mt != kind:
                continue
            if not mt:
                # Combined credits sometimes omit mediaType for movies.
                if kind == "tv" and not (row.get("name") or row.get("firstAirDate")):
                    continue
                if kind == "movie" and not (row.get("title") or row.get("releaseDate")):
                    continue
                mt = kind
            try:
                tid = int(row.get("id") or row.get("tmdbId") or row.get("mediaId") or 0)
            except (TypeError, ValueError):
                tid = 0
            if not tid or tid in ban or tid in seen:
                continue
            release = str(
                row.get("releaseDate")
                or row.get("firstAirDate")
                or row.get("release_date")
                or row.get("first_air_date")
                or ""
            )[:10]
            year = self._year_from_credit(row)
            if release and release > today:
                continue
            if year is not None and year > int(today[:4]):
                continue
            # Prefer known/popular titles; skip zero-signal vapor.
            try:
                popularity = float(row.get("popularity") or 0)
            except (TypeError, ValueError):
                popularity = 0.0
            try:
                votes = int(row.get("voteCount") or row.get("vote_count") or 0)
            except (TypeError, ValueError):
                votes = 0
            title = str(
                row.get("title") or row.get("name") or ""
            ).strip()
            if not title:
                continue
            seen.add(tid)
            scored.append(
                (
                    popularity * 10 + votes,
                    {
                        "title": title,
                        "year": year,
                        "tmdbId": tid,
                        "mediaId": tid,
                        "mediaType": kind,
                        "releaseDate": release or None,
                        "popularity": popularity,
                        "voteCount": votes,
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _score, row in scored[:limit]]

    @staticmethod
    def _year_from_credit(row: dict[str, Any]) -> int | None:
        year = row.get("year")
        try:
            if year not in (None, ""):
                y = int(year)
                if 1900 <= y <= 2100:
                    return y
        except (TypeError, ValueError):
            pass
        for key in (
            "releaseDate",
            "firstAirDate",
            "release_date",
            "first_air_date",
        ):
            raw = str(row.get(key) or "")
            if len(raw) >= 4 and raw[:4].isdigit():
                y = int(raw[:4])
                if 1900 <= y <= 2100:
                    return y
        return None

    def _discover_exclude_ids(self, chat_id: int) -> list[int]:
        ban: set[int] = set(self.memory.shown_tmdb_ids(chat_id))
        for row in self.memory.offered(chat_id):
            raw = row.get("tmdbId") or row.get("mediaId")
            try:
                ban.add(int(raw))
            except (TypeError, ValueError):
                continue
        pending = self.pending.get(int(chat_id))
        if pending is not None:
            for row in pending.options:
                raw = row.get("tmdbId") or row.get("mediaId")
                try:
                    ban.add(int(raw))
                except (TypeError, ValueError):
                    continue
        return sorted(ban)

    def _row_is_excluded(
        self,
        chat_id: int,
        row: dict[str, Any],
        ban_ids: list[int] | set[int],
    ) -> bool:
        title = str(row.get("title") or "")
        if self._title_is_rejected(chat_id, title):
            return True
        raw = row.get("tmdbId") or row.get("mediaId")
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            return False
        return tid in set(ban_ids)

    @staticmethod
    def _amsterdam_today() -> str:
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime

            return datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
        except Exception:  # noqa: BLE001
            from datetime import date

            return date.today().isoformat()

    async def _discover_web_search_fallback(
        self,
        view: MessageView,
        *,
        genre_ids: list[int],
        media_kind: str,
        limit: int,
        ban_ids: list[int],
        query: str,
    ) -> list[dict[str, Any]] | None:
        """When discover is exhausted, use the house web_search tool + search_title."""
        from hearth.tools.websearch import web_search

        genre_label = "fantasy" if GENRE_FANTASY in genre_ids else "movie"
        if GENRE_SCI_FI in genre_ids:
            genre_label = "sci-fi"
        search_q = (
            f"well-known released classic {genre_label} movies films "
            f"(not upcoming {self._amsterdam_today()[:4]})"
        )
        try:
            payload = await web_search({"query": search_q, "limit": 5})
        except Exception as exc:  # noqa: BLE001
            log.info("discover web_search fallback failed: %s", redact(str(exc)))
            return None
        if not payload.get("ok"):
            return None

        names = self._titles_from_web_search(payload)
        if not names:
            return None

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        ban = set(ban_ids)
        for name in names:
            seed = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip() or name
            year_m = re.search(r"\((\d{4})\)\s*$", name)
            year_i = int(year_m.group(1)) if year_m else None
            found = await tool_lookup_title(seed, year=year_i, media_kind=media_kind)
            if not found:
                continue
            row = dict(found[0])
            label = str(row.get("title") or seed).strip()
            key = normalize_title(label)
            if not key or key in seen:
                continue
            if self._row_is_excluded(view.chat_id, row, ban):
                continue
            # Skip unreleased / future years relative to Amsterdam today.
            year = self._row_year(row)
            try:
                today_year = int(self._amsterdam_today()[:4])
            except ValueError:
                today_year = 2026
            if year is not None and year > today_year:
                continue
            seen.add(key)
            rows.append(row)
            raw = row.get("tmdbId") or row.get("mediaId")
            try:
                ban.add(int(raw))
            except (TypeError, ValueError):
                pass
            if len(rows) >= limit:
                break
        return rows if len(rows) >= 2 else None

    @classmethod
    def _titles_from_web_search(cls, payload: dict[str, Any]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        blobs: list[str] = []
        for row in payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            blobs.append(str(row.get("title") or ""))
            blobs.append(str(row.get("snippet") or ""))
        blobs.append(str(payload.get("speak") or ""))
        for blob in blobs:
            for match in _TITLE_YEAR.finditer(blob):
                title = f"{match.group(1).strip()} ({match.group(2)})"
                key = title.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append(title)
        return names[:8]

    async def _resolve_tmdb_row(
        self, tmdb_id: int, media_type: str
    ) -> dict[str, Any] | None:
        """Look up a human title for a TMDB id — never display ``tmdb:N``."""
        for query in (f"tmdb:{int(tmdb_id)}", str(int(tmdb_id))):
            try:
                found = await overseerr.search(query)
            except Exception as exc:  # noqa: BLE001
                log.info("tmdb resolve failed: %s", redact(str(exc)))
                continue
            for hit in found.get("results") or []:
                if not isinstance(hit, dict):
                    continue
                raw = hit.get("tmdbId") or hit.get("mediaId") or hit.get("id")
                try:
                    if raw is not None and int(raw) == int(tmdb_id):
                        row = dict(hit)
                        row["tmdbId"] = int(tmdb_id)
                        row["mediaId"] = int(tmdb_id)
                        kind = str(
                            row.get("mediaType") or media_type or "movie"
                        ).strip()
                        if kind not in {"movie", "tv"}:
                            kind = media_type if media_type in {"movie", "tv"} else "movie"
                        row["mediaType"] = kind
                        title = str(row.get("title") or "").strip()
                        if title and not re.match(r"^tmdb:\d+$", title, re.I):
                            return row
                except (TypeError, ValueError):
                    continue
        return None

    async def _ensure_human_title(
        self,
        row: dict[str, Any],
        tmdb_id: int,
        media_type: str,
    ) -> dict[str, Any]:
        title = str(row.get("title") or "").strip()
        if title and not re.match(r"^tmdb:\d+$", title, re.I):
            out = dict(row)
            out.setdefault("tmdbId", tmdb_id)
            out.setdefault("mediaId", tmdb_id)
            out.setdefault("mediaType", media_type)
            return out
        resolved = await self._resolve_tmdb_row(tmdb_id, media_type)
        if resolved is not None:
            return resolved
        out = dict(row)
        out["title"] = "that title"
        out["tmdbId"] = tmdb_id
        out["mediaId"] = tmdb_id
        out["mediaType"] = media_type if media_type in {"movie", "tv"} else "movie"
        return out

    async def _title_web_search_resolve(
        self,
        view: MessageView,
        title: str,
        *,
        year: int | None = None,
        media_kind: str = "",
    ) -> list[dict[str, Any]]:
        """Catalog miss → web_search → search_title on the resolved film name."""
        from hearth.tools.websearch import web_search

        seed = (title or "").strip()
        if not seed:
            return []
        year_bit = f" {year}" if year else ""
        search_q = f"{seed}{year_bit} movie film"
        try:
            payload = await web_search({"query": search_q, "limit": 5})
        except Exception as exc:  # noqa: BLE001
            log.info("title web_search resolve failed: %s", redact(str(exc)))
            return []
        if not payload.get("ok"):
            return []

        names = self._titles_from_web_search(payload)
        # Also accept bare seed from speak/snippets when year is known.
        if year and seed:
            names.insert(0, f"{seed} ({year})")
        if seed and seed not in names:
            names.append(seed)

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            bare = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip() or name
            year_m = re.search(r"\((\d{4})\)\s*$", name)
            year_i = int(year_m.group(1)) if year_m else year
            # Exact-ish seed: Land ≠ La La Land / Cop Land.
            if not (
                catalog_seed_matches_title(seed, bare)
                or titles_match(seed, bare)
            ):
                continue
            found = await tool_lookup_title(
                bare, year=year_i, media_kind=media_kind
            )
            found = filter_seed_rows(found, seed) if found else []
            found = [
                r
                for r in found
                if catalog_seed_matches_title(seed, str(r.get("title") or ""))
                or titles_match(seed, str(r.get("title") or ""))
            ]
            if not found:
                continue
            row = dict(found[0])
            label = str(row.get("title") or bare).strip()
            key = normalize_title(label)
            if not key or key in seen:
                continue
            if self._title_is_rejected(view.chat_id, label):
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= MAX_CANDIDATES:
                break
        return rows

    async def _tool_web_search(
        self, view: MessageView, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Explicit 'do a websearch' / 'zoek op het web' — never refuse web search."""
        from hearth.tools.websearch import web_search

        query = str(args.get("query") or args.get("q") or "").strip()
        if not query:
            try:
                subject, _kind = self.memory.subject(view.chat_id)
            except Exception:  # noqa: BLE001
                subject = ""
            query = subject or view.text or ""
        query = query.strip()
        if not query:
            return {
                "ok": False,
                "error": "query required",
                "hint": "Pass a film/TV title or question to search the web.",
            }

        try:
            payload = await web_search({"query": query, "limit": 5})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        seed = catalog_search_title(query) or query
        seed = re.sub(
            r"(?i)\b(?:do\s+a\s+)?web\s*search\b|\bzoek\s+op\s+(?:het\s+)?web\b|"
            r"\bsearch\s+(?:the\s+)?web\b|\bgoogle\b",
            " ",
            seed,
        ).strip(" -–—|,.")
        if not seed or len(seed) < 2:
            try:
                subject, _kind = self.memory.subject(view.chat_id)
            except Exception:  # noqa: BLE001
                subject = ""
            seed = (subject or "").strip() or query

        media_kind = str(args.get("media_type") or args.get("mediaType") or "").strip()
        kind = media_kind if media_kind in {"movie", "tv"} else ""
        year = args.get("year")
        try:
            year_i = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year_i = None
        if year_i is None:
            _bare, year_from_q = strip_title_year_media(query)
            if year_from_q is not None:
                year_i = year_from_q
            else:
                _bare2, year_from_msg = strip_title_year_media(view.text)
                if year_from_msg is not None:
                    year_i = year_from_msg
        if not kind and re.search(r"(?i)\b(?:films?|movies?|flicks?)\b", view.text or query):
            kind = "movie"
        # Prefer a clean catalog seed (strip "2026 film").
        bare_seed, _ = strip_title_year_media(seed)
        if bare_seed:
            seed = bare_seed

        names = self._titles_from_web_search(payload if isinstance(payload, dict) else {})
        if year_i and seed:
            names.insert(0, f"{seed} ({year_i})")
        if seed and seed not in names:
            names.append(seed)

        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            bare = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip() or name
            year_m = re.search(r"\((\d{4})\)\s*$", name)
            y = int(year_m.group(1)) if year_m else year_i
            concrete_seed = looks_like_concrete_title(seed)
            if concrete_seed and not (
                catalog_seed_matches_title(seed, bare) or titles_match(seed, bare)
            ):
                continue
            found = await tool_lookup_title(bare, year=y, media_kind=kind)
            if concrete_seed:
                found = filter_seed_rows(found, seed) if found else []
                found = [
                    r
                    for r in found
                    if catalog_seed_matches_title(seed, str(r.get("title") or ""))
                    or titles_match(seed, str(r.get("title") or ""))
                ]
            if not found:
                continue
            row = dict(found[0])
            label = str(row.get("title") or bare).strip()
            key = normalize_title(label)
            if not key or key in seen:
                continue
            if self._title_is_rejected(view.chat_id, label):
                continue
            seen.add(key)
            resolved.append(row)
            if len(resolved) >= 1:
                break

        if resolved:
            row = resolved[0]
            offered = self._ask_guess_confirm(view, row, query=seed)
            compact = [
                {
                    "title": str(row.get("title") or ""),
                    "year": self._row_year(row),
                    "tmdbId": row.get("tmdbId") or row.get("mediaId"),
                    "mediaType": self._row_kind(row, kind),
                }
            ]
            return {
                "ok": True,
                "query": query,
                "resolved_title": compact[0]["title"],
                "results": compact,
                "count": 1,
                "reply": offered.reply,
                "reply_markup": offered.reply_markup,
                "web": {
                    "identity_hints": [
                        {
                            "title": str(r.get("title") or "")[:80],
                            "source": str(r.get("source") or "")[:40],
                        }
                        for r in (payload.get("results") or [])[:3]
                        if isinstance(r, dict)
                    ],
                },
                "hint": (
                    "Offer Get for the resolved title. Do not auto-queue. "
                    "Do not paste Wikipedia/RT/plot text. "
                    "Never say you cannot search the web."
                ),
                "source": "web_search",
            }

        # Named title asks must still Get/request — never dump speak/synopses.
        named = looks_like_named_title_year(view.text) or looks_like_concrete_title(
            seed
        ) or looks_like_named_title_year(query)
        if named:
            bare, year_guess = strip_title_year_media(seed)
            if year_i is None:
                year_i = year_guess
            offer_title = bare or catalog_search_title(seed) or seed
            note = ""
            try:
                today_year = int(self._amsterdam_today()[:4])
            except ValueError:
                today_year = 2026
            if year_i is not None and year_i > today_year:
                note = "Not out yet — I can still request it. "
            offered = self._ask_guess_confirm(
                view,
                {
                    "title": offer_title,
                    "year": year_i,
                    "mediaType": kind or "movie",
                },
                query=offer_title,
            )
            reply = offered.reply or ""
            if note:
                reply = f"{note}{reply}"
            return {
                "ok": True,
                "query": query,
                "resolved_title": offer_title,
                "results": [],
                "count": 0,
                "reply": reply,
                "reply_markup": offered.reply_markup,
                "hint": (
                    "Offer Get for the named title. Do not summarize web pages. "
                    "Never paste wikipedia.org, rottentomatoes.com, or "
                    "utm_source=openai links."
                ),
                "source": "web_search",
            }

        return {
            "ok": bool(payload.get("ok")),
            "query": query,
            "results": [
                {
                    "title": str(r.get("title") or "")[:80],
                    "source": str(r.get("source") or "")[:40],
                }
                for r in (payload.get("results") or [])[:3]
                if isinstance(r, dict)
            ],
            "reply": (
                "I searched the web but couldn't pin a catalog title. "
                "Any other clue?"
            ),
            "hint": (
                "Do not paste Wikipedia/RT essays or utm_source=openai links. "
                "Ask a short clarifying question or call search_title."
            ),
            "source": "web_search",
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

        # Dedup (chat_id, tmdb_id) — model + callback must not triple-queue.
        oid = row.get("tmdbId") or row.get("mediaId") or tmdb_id
        try:
            oid_i = int(oid) if oid not in (None, "") else None
        except (TypeError, ValueError):
            oid_i = None
        if oid_i is not None and self.deduper.seen_tmdb(view.chat_id, oid_i):
            return {
                "ok": True,
                "grabbed": False,
                "already": True,
                "title": str(row.get("title") or title),
                "reply": format_already(str(row.get("title") or title), queued=True),
                "media_type": str(row.get("mediaType") or kind),
            }

        if year_i is not None and row.get("year") in (None, ""):
            row["year"] = year_i
        if tmdb_id is not None and not (row.get("tmdbId") or row.get("mediaId")):
            row["tmdbId"] = tmdb_id
            row["mediaId"] = tmdb_id
        if oid_i is not None:
            row = await self._ensure_human_title(
                row, oid_i, str(row.get("mediaType") or kind)
            )

        result = await self._grab_catalog_row(
            view,
            row,
            query=str(row.get("title") or title),
            media_kind_hint=str(row.get("mediaType") or kind),
            skip_rate=True,
        )
        if result.reply and "tmdb:" in result.reply.lower():
            human = str(row.get("title") or result.title or "").strip()
            if human and not re.match(r"^tmdb:\d+$", human, re.I):
                result.reply = format_queued(
                    human, result.year or self._row_year(row), "Overseerr"
                )
            else:
                result.reply = format_queued("that title", result.year, "Overseerr")
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
        out: dict[str, Any] = {
            "ok": True,
            "title": title or result.title,
            "reply": result.reply,
            "grabbed": bool(result.grabbed),
            "media_type": media_kind or "movie",
        }
        if result.reply_markup:
            out["reply_markup"] = result.reply_markup
            out["needs_pick"] = True
            pending = self._pending_for(view.chat_id)
            if pending and pending.offer_kind == "release":
                out["releases"] = pending.options
                out["hint"] = (
                    "Present Get buttons for these releases. "
                    "Do not grab until the user taps Get or says yes on a single option."
                )
        return out

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

    def _person_confirm_from_history(self, chat_id: int) -> dict[str, Any] | None:
        """Recover a person typo-confirm from recent turns when state was lost.

        Live path: bot said ``Did you mean Leonardo DiCaprio?`` (no year / Get)
        after a ``movies with …`` ask; Yeah must continue credits, not title search.
        """
        history = self.memory.history_blob(chat_id)
        if not history:
            return None
        last_bot = ""
        prior_user = ""
        for turn in reversed(history):
            role = turn.get("role")
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            if role == "bot" and not last_bot:
                last_bot = text
                continue
            if role == "user" and last_bot and not prior_user:
                prior_user = text
                break
        if not last_bot or not prior_user:
            return None
        if not looks_like_person_ask(prior_user):
            return None
        match = re.match(
            r"(?i)^\s*did you mean\s+(.+?)\s*\??\s*$",
            last_bot.strip(),
        )
        if not match:
            return None
        guessed = match.group(1).strip().strip("\"'")
        # Title confirms include (YYYY); person confirms usually do not.
        if re.search(r"\(\s*(?:19|20)\d{2}\s*\)", guessed):
            return None
        if not guessed or len(guessed.split()) > 5:
            return None
        media_type = "movie"
        if re.search(r"(?i)\b(?:shows?|series|tv)\b", prior_user) and not re.search(
            r"(?i)\b(?:movies?|films?|flicks?)\b", prior_user
        ):
            media_type = "tv"
        return {
            "name": guessed[:80],
            "personId": None,
            "media_type": media_type,
            "limit": 4,
            "query": extract_person_name(prior_user) or guessed,
        }

    def _title_confirm_from_history(self, chat_id: int) -> PendingDisambiguation | None:
        """Recover a title Did-you-mean when pending state was lost.

        Live path: bot said ``Did you mean The Man from Earth?`` (or with year)
        without a sticky pending row; Yes must still queue THAT title via
        Overseerr search + mediaId — never invent a catalog miss.
        """
        history = self.memory.history_blob(chat_id)
        if not history:
            return None
        last_bot = ""
        offered: list[dict[str, Any]] = []
        for turn in reversed(history):
            role = turn.get("role")
            text = str(turn.get("text") or "").strip()
            if role == "bot" and not last_bot:
                last_bot = text
                raw_offered = turn.get("offered")
                if isinstance(raw_offered, list):
                    offered = [r for r in raw_offered if isinstance(r, dict)]
                break
        if not last_bot:
            return None
        match = re.match(
            r"(?i)^\s*did you mean\s+(.+?)\s*\??\s*$",
            last_bot.strip(),
        )
        if not match:
            return None
        guessed = match.group(1).strip().strip("\"'")
        year = None
        year_m = re.search(r"\((\s*(?:19|20)\d{2}\s*)\)\s*$", guessed)
        if year_m:
            try:
                year = int(year_m.group(1).strip())
            except (TypeError, ValueError):
                year = None
            guessed = guessed[: year_m.start()].strip().strip("\"'")
        # Person confirms: no year and prior ask was filmography — leave alone.
        if year is None and not offered:
            prior_user = ""
            for turn in reversed(history):
                if turn.get("role") == "user":
                    prior_user = str(turn.get("text") or "").strip()
                    if prior_user:
                        break
            if prior_user and looks_like_person_ask(prior_user):
                return None
        if not guessed or len(guessed) > 120:
            return None
        if offered and len(offered) == 1:
            option = dict(offered[0])
            if not option.get("title"):
                option["title"] = guessed
            if year is not None and option.get("year") in (None, ""):
                option["year"] = year
            # ChatMemory must keep mediaType for Yes → POST by mediaId.
            tid = option.get("tmdbId") or option.get("mediaId") or option.get("id")
            if tid not in (None, ""):
                option["tmdbId"] = tid
                option["mediaId"] = tid
        else:
            option = {
                "title": guessed,
                "year": year,
                "mediaType": "movie",
            }
        kind = str(option.get("mediaType") or "movie")
        if kind not in {"movie", "tv"}:
            kind = "movie"
        markup = single_get_keyboard(option)
        return PendingDisambiguation(
            chat_id=int(chat_id),
            options=[option],
            media_kind=kind,
            query=str(option.get("title") or guessed)[:200],
            created_message_id=0,
            last_bot_reply=last_bot[:400],
            mode="confirm",
            reply_markup=markup,
            offer_kind="title",
        )

    async def _overseerr_title_hits(
        self,
        title: str,
        *,
        year: int | None = None,
        media_kind: str = "",
    ) -> list[dict[str, Any]]:
        """Overseerr multi-search → offerable movie/tv rows (``resolve_offer``).

        One ``GET /api/v1/search?query=`` call. Short seeds stay exact
        (Land ≠ La La Land). Multi-word titles like ``Late Night with the Devil``
        / ``Rescued by Ruby`` / ``The Man from Earth`` keep Overseerr hits —
        never drop them with a Land-style ``title_seed_matches`` gate.
        """
        seed = catalog_search_title(title) or (title or "").strip()
        if not seed:
            return []
        return await resolve_offer(
            seed,
            year=year,
            media_kind=media_kind if media_kind in {"movie", "tv"} else "",
        )

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
        # Title Get offer replaces any person-name typo confirm.
        self.pending_person.pop(view.chat_id, None)
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
        # Title (YYYY) or "Title 2026 film" — download/Get path, not explain.
        return bool(
            parsed.title
            and parsed.year
            and (
                is_explicit_title_year(view.text)
                or looks_like_named_title_year(view.text)
            )
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
        """Exact Title (YYYY) / named title+year → offer Get, never silently grab."""
        hits, label = await tool_lookup_parsed(parsed)
        rows = dedupe_choice_rows([hit.as_dict() for hit in hits]) if hits else []
        if not rows:
            # Catalog miss → web identity resolve, then still offer Get/request
            # (including upcoming/unreleased — Overseerr can hold them).
            seed = catalog_search_title(parsed.title) or parsed.title or label
            kind = (
                parsed.media_kind
                if parsed.media_kind in {"movie", "tv"}
                else "movie"
            )
            web_rows = await self._title_web_search_resolve(
                view,
                seed,
                year=parsed.year,
                media_kind=kind,
            )
            if web_rows:
                rows = dedupe_choice_rows(web_rows)
            else:
                note = ""
                try:
                    today_year = int(self._amsterdam_today()[:4])
                except ValueError:
                    today_year = 2026
                if parsed.year is not None and parsed.year > today_year:
                    note = "Not out yet — I can still request it. "
                offered = self._ask_guess_confirm(
                    view,
                    {
                        "title": seed,
                        "year": parsed.year,
                        "mediaType": kind,
                    },
                    query=seed,
                )
                if note and offered.reply:
                    offered.reply = f"{note}{offered.reply}"
                return offered
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
        """Queue exactly this pending offer via Overseerr mediaId.

        Never re-run ``title_seed_matches`` as a gate that can drop the offered
        row. Missing mediaId → ``resolve_offer`` + fuzzy pick, then POST.
        ``format_not_found`` only when Overseerr search returned zero movie/tv.
        """
        if not skip_rate and not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())

        current = dict(row)
        title = str(current.get("title") or query or "Untitled")
        year = self._row_year(current)
        kind = self._row_kind(current, media_kind_hint)
        tmdb_id = current.get("tmdbId") or current.get("mediaId") or current.get("id")
        tvdb_id = current.get("tvdbId")

        if current.get("inLibrary"):
            return InboxResult(
                handled=True,
                reply=format_already(title, library=True),
                title=title,
                year=year,
            )

        # Pending may lack mediaId (history recovery / id-less confirm). Resolve
        # via Overseerr search once, fuzzy-pick the offered title, then request
        # by mediaId — never claim not-found when search returned movie/tv.
        if tmdb_id in (None, "") and tvdb_id in (None, ""):
            rows = await resolve_offer(
                title,
                year=year,
                media_kind=kind if kind in {"movie", "tv"} else "",
            )
            if not rows:
                # Last resort: only when Overseerr returned zero movie/tv rows.
                return InboxResult(
                    handled=True,
                    reply=format_not_found(title),
                    title=title,
                    year=year,
                )
            pick = pick_offer_for_title(
                rows,
                title,
                year=year,
                media_kind=kind if kind in {"movie", "tv"} else "",
            )
            if pick is None:
                pick = rows[0]
            if len(rows) > 1 and not choices_are_indistinguishable(rows):
                # Ambiguous years/types — offer list instead of guessing wrong id.
                # But if fuzzy pick clearly matches the offered string, use it.
                picked_title = str(pick.get("title") or "")
                if not (
                    titles_match(title, picked_title)
                    or normalize_title(title) in normalize_title(picked_title)
                    or normalize_title(picked_title) in normalize_title(title)
                ):
                    return self._offer_rows(view, title, rows, media_kind=kind)
            current = dict(pick)
            title = str(current.get("title") or title)
            year = self._row_year(current) or year
            kind = self._row_kind(current, kind)
            tmdb_id = current.get("tmdbId") or current.get("mediaId")
            tvdb_id = current.get("tvdbId")
            if current.get("inLibrary"):
                return InboxResult(
                    handled=True,
                    reply=format_already(title, library=True),
                    title=title,
                    year=year,
                )

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
        # Still no id after resolve_offer had hits — should not happen; use pick.
        if parsed.tmdb_id is None and parsed.tvdb_id is None:
            return InboxResult(
                handled=True,
                reply=format_not_found(title),
                title=title,
                year=year,
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
        """Ground one parsed row and request it through Overseerr by mediaId."""
        del exact  # Confirmed and non-confirmed both resolve mediaId via search.
        kind = media_kind if media_kind in {"movie", "tv"} else "movie"
        title = parsed.title or parsed.display_label() or "Untitled"
        year = parsed.year
        media_id = parsed.tmdb_id

        # Resolve TMDB id via resolve_offer when missing. Never invent a catalog
        # miss when Overseerr returned movie/tv hits for the confirmed title.
        if media_id is None and parsed.tvdb_id is None:
            query = catalog_search_title(title) or title
            rows = await resolve_offer(
                query,
                year=year,
                media_kind=kind,
            )
            if not rows:
                return InboxResult(
                    handled=True,
                    reply=format_not_found(parsed.display_label()),
                )
            pick = pick_offer_for_title(
                rows, query, year=year, media_kind=kind
            ) or rows[0]
            if (
                len(rows) > 1
                and not choices_are_indistinguishable(rows)
                and not (
                    titles_match(query, str(pick.get("title") or ""))
                    or normalize_title(query)
                    in normalize_title(str(pick.get("title") or ""))
                )
            ):
                return self._offer_rows(view, query, rows, media_kind=kind)
            title = str(pick.get("title") or title)
            year = self._row_year(pick) or year
            kind = self._row_kind(pick, kind)
            media_id = pick.get("mediaId") or pick.get("tmdbId") or pick.get("id")
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
            # Prefer honest library / already-queued copy over "not in catalog".
            if response.get("already") or (
                isinstance(response.get("requested"), dict)
                and response["requested"].get("inLibrary")
            ):
                return InboxResult(
                    handled=True,
                    reply=format_already(title, library=True),
                    title=title,
                    service=service,
                    year=year,
                )
            speak = str(response.get("speak") or "").strip()
            if response.get("ambiguous") and speak:
                return InboxResult(
                    handled=True,
                    reply=speak,
                    title=title,
                    service=service,
                    year=year,
                )
            # Search returned a hit but request failed — never claim Overseerr
            # "doesn't have" the title when we already resolved a mediaId.
            if media_id not in (None, ""):
                return InboxResult(
                    handled=True,
                    reply=f"Couldn't queue '{title}' through Overseerr.",
                    title=title,
                    service=service,
                    year=year,
                )
            return InboxResult(
                handled=True,
                reply=speak or format_not_found(title),
                title=title,
                service=service,
                year=year,
            )

        self.progress.track(view.chat_id, title, service, year)
        display = title
        if not display or re.match(r"^tmdb:\d+$", display.strip(), re.I):
            display = "that title"
        return InboxResult(
            handled=True,
            reply=format_queued(display, year, "Overseerr"),
            grabbed=True,
            title=display if display != "that title" else title,
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
        """Persist {tmdbId, mediaType, title, year} on pending + ChatMemory."""
        normalized = normalize_offer_row(row) if isinstance(row, dict) else None
        source = normalized or dict(row)
        title = str(source.get("title") or query or "Untitled")
        year = self._row_year(source)
        kind = self._row_kind(source)
        tid = source.get("tmdbId") or source.get("mediaId") or source.get("id")
        option = {
            "title": title,
            "year": year,
            "mediaType": kind if kind in {"movie", "tv"} else "movie",
            "tmdbId": tid,
            "mediaId": tid,
            "tvdbId": source.get("tvdbId"),
            "inLibrary": bool(source.get("inLibrary")),
        }
        if option["mediaType"] not in {"movie", "tv"}:
            option["mediaType"] = "movie"
            kind = "movie"
        else:
            kind = option["mediaType"]
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

        from hearth.tools.arr import want_keep_existing

        keep_existing = want_keep_existing(view.text) or None

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
                keep_existing=keep_existing if service == "radarr" else None,
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

        # Library / no-file / too-large / keep-both → confirmable alternate-release menu.
        if response.get("needs_pick") and response.get("releases"):
            return self._offer_release_rows(
                view,
                str(response.get("title") or title),
                list(response.get("releases") or []),
                speak=str(response.get("speak") or ""),
                reason=str(response.get("reason") or "needs_pick"),
                keep_existing=bool(response.get("keepExisting")),
            )

        reply = str(response.get("speak") or "").strip()
        if not reply:
            reply = f"Couldn't retry {title}."
        grabbed = bool(response.get("ok")) and str(response.get("reason") or "") in {
            "retried",
            "grabbed",
            "switched",
            "kept_both",
        }
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

    def _offer_release_rows(
        self,
        view: MessageView,
        movie_title: str,
        releases: list[dict[str, Any]],
        *,
        speak: str = "",
        reason: str = "needs_pick",
        keep_existing: bool = False,
    ) -> InboxResult:
        rows = [dict(r) for r in releases[:4] if isinstance(r, dict) and r.get("releaseToken")]
        if not rows:
            return InboxResult(
                handled=True,
                reply=speak or f"No alternate releases found for {movie_title}.",
            )
        from hearth.tools.arr import format_release_offer

        reply = speak or format_release_offer(movie_title, rows, reason=reason)
        markup = (
            single_get_keyboard(rows[0])
            if len(rows) == 1
            else offer_inline_keyboard(rows)
        )
        self.pending[view.chat_id] = PendingDisambiguation(
            chat_id=view.chat_id,
            options=rows,
            media_kind="movie",
            query=movie_title,
            created_message_id=view.message_id,
            last_bot_reply=reply,
            mode="confirm" if len(rows) == 1 else "offer",
            reply_markup=markup,
            offer_kind="release",
            keep_existing=bool(keep_existing) or reason == "needs_pick_keep",
        )
        self.memory.set_subject(view.chat_id, movie_title, media_kind="movie")
        self.memory.clear_offered(view.chat_id)
        self._set_mode(view.chat_id, "confirm" if len(rows) == 1 else "offer")
        return InboxResult(
            handled=True,
            reply=reply,
            title=movie_title,
            service="radarr",
            reply_markup=markup,
            mode="confirm" if len(rows) == 1 else "offer",
        )

    async def _grab_pending_release(
        self,
        view: MessageView,
        row: dict[str, Any],
        *,
        pending: PendingDisambiguation | None = None,
    ) -> InboxResult:
        """Confirm-gated Radarr release grab for a pending switch offer."""
        token = str(row.get("releaseToken") or "").strip()
        guid = str(row.get("guid") or "").strip()
        movie_title = str(
            (pending.query if pending else "")
            or row.get("movieTitle")
            or row.get("title")
            or ""
        ).strip()
        if not token and not guid:
            return InboxResult(
                handled=True,
                reply="That release offer is missing an id — ask again.",
            )
        try:
            response = await radarr.grab_alternate_release(
                movie_title,
                guid=guid,
                release_token_value=token,
                confirm=True,
                prefer_smaller=True,
                reason="user:telegram",
                keep_existing=bool(pending.keep_existing) if pending else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("telegram release grab failed: %s", redact(str(exc)))
            return InboxResult(
                handled=True,
                reply=f"Couldn't grab that release of {movie_title or 'that title'}.",
            )
        if response.get("ok") and response.get("reason") in {"switched", "kept_both"}:
            title = str(response.get("title") or movie_title)
            self.pending.pop(view.chat_id, None)
            self.progress.track(view.chat_id, title, "radarr", None)
            self.memory.set_subject(view.chat_id, title, media_kind="movie")
            self._set_mode(view.chat_id, "queued")
            return InboxResult(
                handled=True,
                reply=str(
                    response.get("speak")
                    or (
                        f"Downloading an extra release of {title} — keeping the current file."
                        if response.get("reason") == "kept_both"
                        else f"Grabbing a different release of {title}."
                    )
                ),
                grabbed=True,
                title=title,
                service="radarr",
                mode="queued",
            )
        reply = str(response.get("speak") or "").strip() or (
            f"Couldn't grab that release of {movie_title or 'that title'}."
        )
        return InboxResult(handled=True, reply=reply, title=movie_title, service="radarr")

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
