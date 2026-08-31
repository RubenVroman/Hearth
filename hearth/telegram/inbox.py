"""Grab orchestration for Telegram inbox requests — reuses *arr / Overseerr."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from hearth.config import settings
from hearth.memory.redact import redact
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
    looks_like_confirm_yes,
    looks_like_media_ask,
    looks_like_recommend_ask,
    search_title_grounded,
    subject_matches_user_title,
    titles_match,
)
from hearth.telegram.memory import ChatMemory
from hearth.telegram.parse import (
    MessageView,
    ParsedRequest,
    normalize_title,
    parse_message,
)
from hearth.telegram.catalog import (
    CatalogHit,
    catalog_search_title,
    catalog_seed_matches_title,
    hit_to_parsed,
    resolve_parsed,
    resolve_title,
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
from hearth.telegram.safeguards import Deduper, RateLimiter, chat_allowed, user_allowed
from hearth.tools.arr import overseerr, radarr, sonarr

log = logging.getLogger("hearth.telegram")

# Short follow-up window so "all of them" / "the new one" stay grounded.
PENDING_TTL_S = 15 * 60


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

    def _is_loop(self, view: MessageView) -> bool:
        if self.bot_user_id is not None and view.user_id == self.bot_user_id:
            return True
        if (view.chat_id, view.message_id) in self.outbound_message_ids:
            return True
        return False

    def _pending_for(self, chat_id: int) -> PendingDisambiguation | None:
        pending = self.pending.get(chat_id)
        if pending is None:
            return None
        if time.monotonic() - pending.created_at > PENDING_TTL_S:
            del self.pending[chat_id]
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
        self.pending[view.chat_id] = PendingDisambiguation(
            chat_id=view.chat_id,
            options=options[:MAX_CANDIDATES],
            media_kind=media_kind,
            query=query,
            created_message_id=view.message_id,
            last_bot_reply=reply[:400],
        )
        self.memory.set_subject(
            view.chat_id,
            query,
            media_kind=media_kind if media_kind in {"movie", "tv"} else "",
            offered=options,
        )

    def _finish(
        self,
        view: MessageView,
        result: InboxResult,
        *,
        search_title: str = "",
        media_kind: str = "",
        offered: list[dict[str, Any]] | None = None,
        record_user: bool = False,
        user_text: str = "",
    ) -> InboxResult:
        if record_user and user_text:
            self.memory.record_user(view.chat_id, user_text)
        # Unique grab must not leave a 1-item offered menu for the next ask.
        if result.grabbed:
            self.pending.pop(view.chat_id, None)
            self.memory.clear_offered(view.chat_id)
            offered = []
        if result.reply:
            pending = self.pending.get(view.chat_id)
            self.memory.record_bot(
                view.chat_id,
                result.reply,
                search_title=search_title
                or (pending.query if pending else "")
                or result.title
                or "",
                media_kind=media_kind
                or (pending.media_kind if pending and pending.media_kind in {"movie", "tv"} else ""),
                offered=offered if offered is not None else (pending.options if pending else None),
            )
        # Queued titles join rejected memory so "another one" / find-one guesses
        # do not re-offer Alien / Event Horizon after they were just queued.
        if result.grabbed:
            queued: list[str] = []
            if result.title:
                queued.append(result.title)
            for title in result.titles or []:
                text = str(title or "").strip()
                if text:
                    queued.append(text)
            if queued:
                self.memory.remember_rejected(view.chat_id, queued)
        return result

    def _titles_from_options(self, options: list[dict[str, Any]] | None) -> list[str]:
        titles: list[str] = []
        seen: set[str] = set()
        for row in options or []:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            titles.append(title)
        return titles

    def _index_in_pending(
        self, pending: PendingDisambiguation, search_title: str
    ) -> int | None:
        """1-based index of a pending option matching ``search_title``, if any."""
        needle = (search_title or "").strip()
        if not needle:
            return None
        for idx, row in enumerate(pending.options, start=1):
            if titles_match(str(row.get("title") or ""), needle):
                return idx
        return None

    def _reconcile_pending_after_intent(
        self,
        chat_id: int,
        pending: PendingDisambiguation,
        intent: IntentDecision,
    ) -> tuple[IntentDecision, PendingDisambiguation | None]:
        """Keep live pending until the model picks, matches, or starts a new ask.

        Confirm / download of the on-screen title → pick that row's tmdbId.
        New search_title that does not match pending → reject + clear, then
        let the search path run. Clarify that re-lists the pending options
        keeps them; bare reject clarify (nee/no) drops them.
        """
        if intent.action in {"pick", "pick_many"} and intent.indices:
            return intent, pending

        if intent.action == "retry":
            # Retry the download already in play — drop sticky pick menus.
            self.pending.pop(chat_id, None)
            return intent, None

        if intent.action == "search" and intent.search_title.strip():
            match_idx = self._index_in_pending(pending, intent.search_title)
            if match_idx is not None:
                return (
                    IntentDecision(
                        action="pick",
                        indices=[match_idx],
                        confidence=intent.confidence,
                        source=intent.source,
                        media_kind=intent.media_kind,
                        people=list(intent.people),
                        year=intent.year,
                        search_title=intent.search_title,
                    ),
                    pending,
                )
            # Drop sticky wrong options. Do NOT reject pending.query when the
            # model is refining the same title with actor/year clues
            # ("Land with robin wright" after a bad Land menu).
            rejected_now = self._titles_from_options(pending.options)
            same_title = titles_match(pending.query, intent.search_title)
            if pending.query and not same_title:
                rejected_now.append(pending.query)
            self.memory.remember_rejected(
                chat_id,
                rejected_now,
                clear_offered=True,
                clear_subject=not same_title,
            )
            self.pending.pop(chat_id, None)
            return intent, None

        if intent.action == "clarify":
            # Bare nee/no / soft reject without naming a replacement → drop list.
            # Numbered-list clarify about the same options → keep pending.
            if not clarify_wants_numbered_list(intent.clarify_question or ""):
                rejected_now = self._titles_from_options(pending.options)
                if pending.query:
                    rejected_now.append(pending.query)
                self.memory.remember_rejected(
                    chat_id,
                    rejected_now,
                    clear_offered=True,
                    clear_subject=False,
                )
                self.pending.pop(chat_id, None)
                return intent, None
            return intent, pending

        # ignore / passthrough — keep the on-screen guess alive.
        return intent, pending

    def _is_instant_catalog(self, parsed: ParsedRequest, view: MessageView) -> bool:
        """No-doubt grabs: catalog id/URL or explicit ``Title (YYYY)`` only.

        Bare titles, season asks, and plot clues always go through gpt-4o with
        catalog candidates in-loop. Live numbered picks are handled separately.
        """
        if parsed.kind != "request":
            return False
        if parsed.imdb_id or parsed.tmdb_id or parsed.tvdb_id:
            return True
        if parsed.year and parsed.title and is_explicit_title_year(view.text):
            return True
        return False

    async def _catalog_candidates_for_message(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        rejected_titles: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Overseerr hits for this turn — only for concrete title-like asks.

        Actor clauses are stripped from the query; the model still sees the
        full user text (actor clues). Plot/reject sentences skip pre-search
        so Dutch plots never become Overseerr queries.

        Only exact / franchise-prefix rows become candidates — never substring
        hits like La La Land for ``Land`` or The Wild Robot for ``Wild``.
        """
        raw = (view.text or "").strip()
        if not looks_like_concrete_title(raw):
            return []
        seed = ""
        if parsed.kind == "request" and parsed.title:
            seed = catalog_search_title(parsed.title) or parsed.title
        if not seed:
            seed = catalog_search_title(raw) or raw
        if not seed:
            return []
        # Always search the cast-stripped seed (never the raw "with Actor" blob).
        # Exact/prefix filtering below drops La La Land for Land and Wild Robot
        # for a bare Wild seed; title+person blobs with no "with" still go to
        # gpt-4o when Overseerr has no exact seed match.
        try:
            hits = await resolve_title(
                seed,
                year=parsed.year if parsed.kind == "request" else None,
                media_kind=(
                    parsed.media_kind
                    if parsed.kind == "request" and parsed.media_kind in {"movie", "tv"}
                    else ""
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.info("catalog-in-loop search failed: %s", redact(str(exc)))
            return []
        rows = self._dedupe_choices([h.as_dict() for h in hits])
        grounded = [
            row
            for row in rows
            if catalog_seed_matches_title(seed, str(row.get("title") or ""))
        ]
        rows = grounded[:MAX_CANDIDATES]
        rejected_norm = {
            normalize_title(t) for t in (rejected_titles or []) if str(t).strip()
        }
        if rejected_norm:
            kept: list[dict[str, Any]] = []
            for row in rows:
                title_n = normalize_title(str(row.get("title") or ""))
                rejected_hit = title_n in rejected_norm or any(
                    (title_n in r or r in title_n) for r in rejected_norm if r
                )
                if rejected_hit and not (
                    titles_match(str(row.get("title") or ""), raw)
                    or catalog_seed_matches_title(seed, str(row.get("title") or ""))
                ):
                    continue
                kept.append(row)
            rows = kept
        return rows
    def _dedupe_choices(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse indistinguishable options (same title+year+kind+id).

        Never offer two identical ``Title (2025)`` rows. Prefer a row with a
        TMDB/TVDB id when collapsing duplicates.
        """
        best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str, str]] = []
        for row in rows:
            title = normalize_title(str(row.get("title") or ""))
            year = str(row.get("year") or "")
            kind = str(
                row.get("mediaType")
                or row.get("media_kind")
                or ("tv" if row.get("tvdbId") else "movie")
            )
            tmdb = row.get("tmdbId") or row.get("mediaId")
            tvdb = row.get("tvdbId")
            imdb = str(row.get("imdbId") or "").lower()
            # Identity key: prefer catalog id when present, else title+year+kind.
            if tmdb not in (None, ""):
                id_key = f"tmdb:{tmdb}"
            elif tvdb not in (None, ""):
                id_key = f"tvdb:{tvdb}"
            elif imdb:
                id_key = f"imdb:{imdb}"
            else:
                id_key = ""
            # Indistinguishable to the user: same title + year + kind.
            display_key = (title, year, kind, "")
            id_full = (title, year, kind, id_key)
            # Collapse by display first — two rows that look identical merge.
            key = display_key if display_key[0] else id_full
            if key not in best:
                best[key] = row
                order.append(key)
                continue
            prev = best[key]
            prev_id = prev.get("tmdbId") or prev.get("mediaId") or prev.get("tvdbId")
            new_id = tmdb or tvdb
            # Prefer the row that carries a catalog id / higher popularity.
            prev_pop = prev.get("popularity") or prev.get("voteCount") or 0
            new_pop = row.get("popularity") or row.get("voteCount") or 0
            if (not prev_id and new_id) or (new_pop and new_pop > (prev_pop or 0)):
                best[key] = row
        return [best[k] for k in order]

    def _choices_are_indistinguishable(self, choices: list[dict[str, Any]]) -> bool:
        if len(choices) <= 1:
            return True
        labels = {
            (
                normalize_title(str(c.get("title") or "")),
                str(c.get("year") or ""),
                str(c.get("mediaType") or ""),
            )
            for c in choices
        }
        return len(labels) == 1

    async def _resolve_and_grab(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        select_all: bool = False,
        exact: bool = False,
    ) -> InboxResult:
        """TMDB/catalog resolve then queue by id — never search raw ``tt…``."""
        needs_resolve = parsed.needs_catalog_resolve()
        if not needs_resolve:
            return await self._grab(
                view, parsed, exact=exact, select_all=select_all
            )

        hits, err_label = await resolve_parsed(parsed)
        if not hits:
            return InboxResult(
                handled=True,
                reply=format_not_found(err_label or parsed.display_label()),
            )

        # Multiple hits: only disambiguate when title/year/type actually differ.
        if len(hits) > 1 and not select_all:
            rows = self._dedupe_choices([h.as_dict() for h in hits])
            if len(rows) == 1 or self._choices_are_indistinguishable(rows):
                hits = [
                    CatalogHit(
                        title=str(rows[0].get("title") or hits[0].title),
                        year=int(rows[0]["year"])
                        if rows[0].get("year") not in (None, "")
                        else hits[0].year,
                        media_kind=(
                            "tv"
                            if str(rows[0].get("mediaType") or "") == "tv"
                            else "movie"
                        ),
                        tmdb_id=int(rows[0]["tmdbId"])
                        if rows[0].get("tmdbId") not in (None, "")
                        else hits[0].tmdb_id,
                        tvdb_id=int(rows[0]["tvdbId"])
                        if rows[0].get("tvdbId") not in (None, "")
                        else hits[0].tvdb_id,
                        imdb_id=hits[0].imdb_id,
                        source="deduped",
                    )
                ]
            else:
                reply = format_ambiguous(
                    parsed.title or err_label or "that",
                    rows,
                )
                kind = rows[0].get("mediaType") or parsed.media_kind or "movie"
                self._remember_pending(
                    view,
                    options=rows,
                    media_kind=str(kind) if str(kind) in {"movie", "tv"} else "movie",
                    query=parsed.title or err_label,
                    reply=reply,
                )
                return InboxResult(handled=True, reply=reply)

        if select_all and len(hits) > 1:
            rows = self._dedupe_choices([h.as_dict() for h in hits])
            return await self._queue_many(
                view,
                parsed.title or err_label,
                str(rows[0].get("mediaType") or "movie"),
                rows,
            )

        resolved = hit_to_parsed(hits[0], base=parsed, reason="catalog_resolve")
        return await self._grab(
            view, resolved, exact=True, select_all=False
        )

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

        # Instant: bare 1/2/3 while a live list is pending.
        if parsed.kind == "disambiguation_pick":
            result = await self._handle_pick(view, parsed)
            return self._finish(view, result, record_user=True, user_text=view.text)

        pending = self._pending_for(view.chat_id)
        history = self.memory.history_blob(view.chat_id)
        subject_title, subject_media_kind = self.memory.subject(view.chat_id)
        memory_offered = self.memory.offered(view.chat_id)
        rejected = list(self.memory.rejected(view.chat_id))

        # Instant: 1/2/3, all of them, de eerste, or yes/that's it — only while a
        # live PendingDisambiguation is on screen. Never soft-revive leftover
        # memory_offered from a prior grab as a fake menu.
        live_options = pending.options if pending else None
        instant = instant_pick_decision(view.text, live_options)
        if instant is not None and instant.action in {"pick", "pick_many"}:
            handled = await self._apply_intent(
                view, parsed, instant, pending=pending
            )
            if handled is not None:
                return self._finish(
                    view,
                    handled,
                    search_title=subject_title,
                    media_kind=subject_media_kind,
                    record_user=True,
                    user_text=view.text,
                )

        # Instant: catalog id/URL or Title (YYYY) — resolve via TMDB, no model.
        if self._is_instant_catalog(parsed, view):
            if not self.rate.allow():
                return InboxResult(handled=True, reply=format_rate_limited())
            if self.deduper.seen_title(view.chat_id, parsed.dedup_key()):
                return InboxResult(handled=True, reply="")
            self.pending.pop(view.chat_id, None)
            result = await self._resolve_and_grab(view, parsed)
            self.memory.set_subject(
                view.chat_id,
                result.title or parsed.title or "",
                media_kind=(
                    parsed.media_kind
                    if parsed.media_kind in {"movie", "tv"}
                    else ("tv" if result.service == "sonarr" else "movie")
                    if result.grabbed
                    else ""
                ),
                clear_rejected=True,
            )
            return self._finish(
                view,
                result,
                search_title=result.title or parsed.title or "",
                media_kind=parsed.media_kind if parsed.media_kind in {"movie", "tv"} else "",
                record_user=True,
                user_text=view.text,
            )

        if parsed.kind == "reject_download":
            return self._finish(
                view,
                InboxResult(handled=True, reply=format_reject_download()),
                record_user=True,
                user_text=view.text,
            )

        # Chatter / emoji / meta — never ask "which movie".
        if looks_like_chatter(view.text) and not pending:
            return self._finish(
                view,
                InboxResult(handled=True, reply=""),
                record_user=True,
                user_text=view.text,
            )

        # Magnets/greetings/empty already classified — ignore without a model hop
        # only when there is truly nothing conversational to resolve.
        if parsed.kind == "ignore" and not history and not pending:
            return InboxResult(handled=True, reply="")

        # Everything else: gpt-4o with live pending options and/or Overseerr
        # candidates on the SAME turn. Do NOT pivot-clear a live 1-item guess
        # before the model hop — the model must see that offer (e.g. a multi-word
        # confirm of "Did you mean Title (year)?"). Only drop pending after the
        # model decides pick / new search / reject.
        catalog_hits: list[dict[str, Any]] = []
        pending_query = (pending.query if pending else subject_title) or ""
        last_bot = (
            pending.last_bot_reply
            if pending
            else (history[-1]["text"] if history else "")
        )

        # New plot/title ask that does not match sticky subject → clear leftover
        # subject + offered so Harry Potter never leaks into a spaceship-horror turn.
        # Skip this wipe while a live PendingDisambiguation is on screen — the
        # model needs that offer + subject to interpret confirms/rejects.
        if (
            pending is None
            and subject_title
            and not subject_matches_user_title(subject_title, view.text)
            and not looks_like_confirm_yes(view.text)
        ):
            self.memory.remember_rejected(
                view.chat_id,
                [],
                clear_offered=True,
                clear_subject=True,
            )
            subject_title, subject_media_kind = "", ""
            pending_query = ""
            memory_offered = []
        elif memory_offered and pending is None:
            # Leftover offered rows from a prior grab are not candidates unless
            # a live PendingDisambiguation is on screen (handled above).
            self.memory.clear_offered(view.chat_id)
            memory_offered = []

        # Catalog-in-the-loop: search Overseerr (actor clause stripped) and pass
        # real hits to the model as candidates this turn.
        if not looks_like_chatter(view.text):
            catalog_hits = await self._catalog_candidates_for_message(
                view, parsed, rejected_titles=rejected
            )

        # Live pending options are the pick targets. Prefer them over a fresh
        # catalog search so confirm/reject of the on-screen guess stays grounded.
        # Actor/year refinements still see the sticky list; the model returns a
        # new search_title and reconcile drops non-matching options.
        candidates_are_pending = bool(pending is not None and pending.options)
        if candidates_are_pending:
            candidates_for_model: list[dict[str, Any]] | None = list(pending.options)
        else:
            candidates_for_model = catalog_hits if catalog_hits else None

        intent = await interpret_intent(
            view.text,
            candidates=candidates_for_model,
            pending_query=pending_query,
            last_bot_reply=last_bot,
            force=True,
            history=history,
            subject_title=subject_title,
            subject_media_kind=subject_media_kind,
            rejected_titles=rejected,
            candidates_are_pending=candidates_are_pending,
        )

        # Reconcile live pending with the model decision — only then clear.
        if pending is not None:
            intent, pending = self._reconcile_pending_after_intent(
                view.chat_id, pending, intent
            )
            if pending is None:
                rejected = list(self.memory.rejected(view.chat_id))
                subject_title, subject_media_kind = self.memory.subject(view.chat_id)
                pending_query = subject_title or ""
                memory_offered = []

        # Soft-revive this turn's catalog hits only when the model picks indices.
        active_pending = pending
        pick_rows = candidates_for_model
        if (
            active_pending is None
            and intent.action in {"pick", "pick_many"}
            and intent.indices
            and pick_rows
        ):
            active_pending = PendingDisambiguation(
                chat_id=view.chat_id,
                options=pick_rows[:MAX_CANDIDATES],
                media_kind=subject_media_kind
                or str(pick_rows[0].get("mediaType") or "movie"),
                query=subject_title
                or (catalog_search_title(view.text) or view.text)[:120],
                created_message_id=view.message_id,
                last_bot_reply=last_bot,
            )
            self.pending[view.chat_id] = active_pending

        handled = await self._apply_intent(
            view,
            parsed,
            intent,
            pending=active_pending,
            catalog_hits=catalog_hits,
        )
        if handled is not None:
            # Never re-stick an unrelated leftover subject on clarify/ignore.
            finish_title = intent.search_title if intent.action == "search" else ""
            if intent.action in {"pick", "pick_many"} and active_pending:
                finish_title = active_pending.query or subject_title
            finish_offered: list[dict[str, Any]] | None
            if handled.grabbed:
                finish_offered = []
            elif self.pending.get(view.chat_id):
                finish_offered = self.pending[view.chat_id].options
            else:
                finish_offered = []
            return self._finish(
                view,
                handled,
                search_title=finish_title,
                media_kind=intent.media_kind or subject_media_kind,
                offered=finish_offered,
                record_user=True,
                user_text=view.text,
            )

        if parsed.kind == "ignore":
            return InboxResult(handled=True, reply="")

        if parsed.kind != "request":
            return InboxResult(handled=True, reply="")

        # Model said passthrough on a non-instant title — still grab, but this
        # path is rare (AI should return search/clarify).
        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())
        if self.deduper.seen_title(view.chat_id, parsed.dedup_key()):
            return InboxResult(handled=True, reply="")
        self.pending.pop(view.chat_id, None)
        result = await self._resolve_and_grab(view, parsed)
        return self._finish(
            view,
            result,
            search_title=parsed.title or result.title or "",
            media_kind=parsed.media_kind if parsed.media_kind in {"movie", "tv"} else "",
            record_user=True,
            user_text=view.text,
        )

    async def _apply_intent(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        intent: IntentDecision,
        *,
        pending: PendingDisambiguation | None,
        catalog_hits: list[dict[str, Any]] | None = None,
    ) -> InboxResult | None:
        hits = list(catalog_hits or [])
        if intent.action == "retry":
            return await self._handle_retry(view, intent)
        if intent.action == "ignore":
            # Belt-and-suspenders: media asks must never go silent.
            if looks_like_chatter(view.text) or not looks_like_media_ask(view.text):
                return InboxResult(handled=True, reply="")
            if intent.search_title.strip():
                intent = IntentDecision(
                    action="search",
                    search_title=intent.search_title,
                    year=intent.year,
                    media_kind=intent.media_kind,
                    confidence=intent.confidence,
                    source=intent.source,
                )
            elif pending is not None and len(pending.options) == 1:
                # On-screen guess still live — re-ask, never empty-title fallback.
                return self._ask_guess_confirm(
                    view, pending.options[0], query=pending.query
                )
            else:
                if (
                    not looks_like_recommend_ask(view.text)
                    and self._last_bot_was_title_miss_or_guess(view.chat_id)
                ):
                    prior, prior_kind, prior_year = self._prior_titled_ask(view.chat_id)
                    if prior:
                        new_concrete = looks_like_concrete_title(
                            view.text
                        ) and not titles_match(prior, view.text)
                        if not new_concrete:
                            reused = await self._reuse_prior_title(
                                view,
                                prior,
                                media_kind=prior_kind,
                                year=prior_year,
                            )
                            if reused is not None:
                                return reused
                return InboxResult(
                    handled=True,
                    reply=(
                        SOFT_CONTEXT_CLARIFY
                        if looks_like_recommend_ask(view.text)
                        else (
                            CONTEXT_CLUE_CLARIFY
                            if (
                                pending is not None
                                or self.memory.has_history(view.chat_id)
                            )
                            else (
                                "Which movie or series did you mean? "
                                "Send the title if you know it."
                            )
                        )
                    ),
                )
        if intent.action == "clarify":
            # Unique close catalog hit → request that tmdb id (never list-less 1–N).
            if hits and (
                len(hits) == 1 or self._choices_are_indistinguishable(hits)
            ):
                if (
                    clarify_wants_numbered_list(intent.clarify_question)
                    or looks_like_concrete_title(view.text)
                ):
                    return await self._grab_catalog_row(view, hits[0], query=view.text)
                # Plot-ish clarify with one grounded hit → name it and ask.
                return self._ask_guess_confirm(view, hits[0], query=view.text)
            # Multiple hits (or list-wording clarify with hits) → always name them.
            if hits and (
                len(hits) > 1
                or clarify_wants_numbered_list(intent.clarify_question)
            ):
                query = (
                    catalog_search_title(parsed.title or view.text)
                    or parsed.title
                    or view.text
                    or "that"
                )
                reply = format_ambiguous(query, hits)
                kind = str(hits[0].get("mediaType") or "movie")
                self._remember_pending(
                    view,
                    options=hits,
                    media_kind=kind if kind in {"movie", "tv"} else "movie",
                    query=str(query)[:200],
                    reply=reply,
                )
                return InboxResult(handled=True, reply=reply)
            # Model named a guess in search_title alongside clarify → ask about it.
            if intent.search_title.strip():
                row = {
                    "title": intent.search_title.strip(),
                    "year": intent.year,
                    "mediaType": intent.media_kind or "movie",
                }
                return self._ask_guess_confirm(view, row, query=intent.search_title)
            # Live pending 1-item guess → re-ask that row, never empty-title.
            if pending is not None and len(pending.options) == 1:
                return self._ask_guess_confirm(
                    view, pending.options[0], query=pending.query
                )
            # Live 1–N list + numbered clarify → re-show those rows.
            if pending is not None and len(pending.options) >= 2:
                reply = format_ambiguous(pending.query or "that", pending.options)
                self._remember_pending(
                    view,
                    options=pending.options,
                    media_kind=pending.media_kind,
                    query=pending.query,
                    reply=reply,
                )
                return InboxResult(handled=True, reply=reply)
            # After a catalog miss / guess-confirm, follow-ups that do not name a
            # NEW concrete title must reuse the prior titled ask — never clue-fish.
            if (
                pending is None
                and not hits
                and not looks_like_recommend_ask(view.text)
                and self._last_bot_was_title_miss_or_guess(view.chat_id)
            ):
                prior, prior_kind, prior_year = self._prior_titled_ask(view.chat_id)
                if prior:
                    new_concrete = looks_like_concrete_title(
                        view.text
                    ) and not titles_match(prior, view.text)
                    if not new_concrete:
                        reused = await self._reuse_prior_title(
                            view,
                            prior,
                            media_kind=prior_kind,
                            year=prior_year,
                        )
                        if reused is not None:
                            return reused
            question = intent.clarify_question or (
                SOFT_CONTEXT_CLARIFY
                if looks_like_recommend_ask(view.text)
                else (
                    CONTEXT_CLUE_CLARIFY
                    if self.memory.has_history(view.chat_id)
                    else "Which movie or series did you mean? Send the title if you know it."
                )
            )
            # Never emit list-less "reply 1–N" / "1–1".
            if clarify_wants_numbered_list(question):
                question = (
                    SOFT_CONTEXT_CLARIFY
                    if looks_like_recommend_ask(view.text)
                    else (
                        CONTEXT_CLUE_CLARIFY
                        if self.memory.has_history(view.chat_id)
                        else "Which movie or series did you mean? Send the title if you know it."
                    )
                )
            # Never demand a title when history already has one / plot clue given.
            if "send the title if you know it" in question.lower() and (
                looks_like_recommend_ask(view.text)
                or self.memory.has_history(view.chat_id)
            ):
                question = (
                    SOFT_CONTEXT_CLARIFY
                    if looks_like_recommend_ask(view.text)
                    else CONTEXT_CLUE_CLARIFY
                )
            # Never re-stick a rejected 1–N list after a clarify pivot.
            if pending is None:
                self.pending.pop(view.chat_id, None)
            return InboxResult(handled=True, reply=question)
        if intent.action == "pick" and pending and intent.indices:
            return await self._handle_indices(view, pending, intent.indices[:1])
        if intent.action == "pick_many" and pending and intent.indices:
            return await self._handle_indices(view, pending, intent.indices)
        if intent.action == "search" and intent.search_title:
            # Extra guard: invented titles must not become the Overseerr query.
            if hits and not search_title_grounded(
                intent.search_title,
                user_message=view.text,
                candidates=hits,
            ):
                if len(hits) == 1 or self._choices_are_indistinguishable(hits):
                    if looks_like_concrete_title(view.text):
                        return await self._grab_catalog_row(
                            view, hits[0], query=view.text
                        )
                    return self._ask_guess_confirm(view, hits[0], query=view.text)
                query = catalog_search_title(view.text) or view.text
                reply = format_ambiguous(query, hits)
                kind = str(hits[0].get("mediaType") or "movie")
                self._remember_pending(
                    view,
                    options=hits,
                    media_kind=kind if kind in {"movie", "tv"} else "movie",
                    query=str(query)[:200],
                    reply=reply,
                )
                return InboxResult(handled=True, reply=reply)
            # Plot/vibe/description: one best guess → ASK, do not queue yet.
            if not looks_like_concrete_title(view.text):
                return await self._confirm_plot_guess(
                    view, parsed, intent, catalog_hits=hits
                )
            if not self.rate.allow():
                return InboxResult(handled=True, reply=format_rate_limited())
            media_kind = (
                intent.media_kind
                if intent.media_kind in {"movie", "tv"}
                else (parsed.media_kind if parsed.media_kind in {"movie", "tv"} else "unknown")
            )
            # Prefer an exact candidate match (tmdb id) when the model names one
            # and the hit is unique — multiple years/types still need a list.
            matched = next(
                (
                    row
                    for row in hits
                    if titles_match(str(row.get("title") or ""), intent.search_title)
                ),
                None,
            )
            if (
                matched is not None
                and not intent.select_all
                and (len(hits) == 1 or self._choices_are_indistinguishable(hits))
            ):
                return await self._grab_catalog_row(
                    view,
                    matched,
                    query=intent.search_title,
                    media_kind_hint=media_kind,
                )
            # Resolve the MODEL's title (exact/prefix only). Never list substring
            # hits (Wild Robot for Wild + Reese Witherspoon). Catalog miss after
            # the model named a title → confirm Title (year), never "send a link".
            search_title = catalog_search_title(intent.search_title) or intent.search_title
            self.memory.set_subject(
                view.chat_id,
                search_title,
                media_kind=media_kind if media_kind in {"movie", "tv"} else "",
                clear_rejected=True,
                clear_offered=True,
            )
            if intent.select_all:
                synthetic = ParsedRequest(
                    kind="request",
                    media_kind=media_kind,  # type: ignore[arg-type]
                    title=search_title,
                    year=intent.year,
                    reason="intent_search",
                )
                self.pending.pop(view.chat_id, None)
                return await self._resolve_and_grab(
                    view,
                    synthetic,
                    select_all=True,
                )
            try:
                resolved = await resolve_title(
                    search_title,
                    year=intent.year,
                    media_kind=media_kind if media_kind in {"movie", "tv"} else "",
                    strict=True,
                )
            except Exception as exc:  # noqa: BLE001
                log.info("model-title catalog resolve failed: %s", redact(str(exc)))
                resolved = []
            exact_rows = self._dedupe_choices([h.as_dict() for h in resolved])
            exact_rows = [
                row
                for row in exact_rows
                if catalog_seed_matches_title(
                    search_title, str(row.get("title") or "")
                )
            ]
            if len(exact_rows) == 1 or (
                exact_rows and self._choices_are_indistinguishable(exact_rows)
            ):
                return await self._grab_catalog_row(
                    view,
                    exact_rows[0],
                    query=search_title,
                    media_kind_hint=media_kind,
                )
            if len(exact_rows) > 1:
                reply = format_ambiguous(search_title, exact_rows)
                kind = str(exact_rows[0].get("mediaType") or media_kind or "movie")
                self._remember_pending(
                    view,
                    options=exact_rows,
                    media_kind=kind if kind in {"movie", "tv"} else "movie",
                    query=search_title,
                    reply=reply,
                )
                return InboxResult(handled=True, reply=reply)
            return self._ask_guess_confirm(
                view,
                {
                    "title": search_title,
                    "year": intent.year,
                    "mediaType": media_kind if media_kind in {"movie", "tv"} else "movie",
                },
                query=search_title,
            )
        return None

    def _prior_titled_ask(self, chat_id: int) -> tuple[str, str, int | None]:
        """Last model/user titled ask still in play (subject or history).

        Used when a follow-up says 'find/match that' and the model clarifies
        instead of reusing the already-stated title.
        """
        subject, kind = self.memory.subject(chat_id)
        year: int | None = None
        title = (subject or "").strip()
        if title:
            return title, kind if kind in {"movie", "tv"} else "", year
        for turn in reversed(self.memory.history_blob(chat_id)):
            st = str(turn.get("search_title") or "").strip()
            if not st:
                continue
            mk = str(turn.get("media_kind") or "")
            return st, mk if mk in {"movie", "tv"} else "", year
        return "", "", None

    def _last_bot_was_title_miss_or_guess(self, chat_id: int) -> bool:
        """True when the last bot turn was a catalog miss or guess-confirm."""
        history = self.memory.history_blob(chat_id)
        for turn in reversed(history):
            if turn.get("role") != "bot":
                continue
            text = str(turn.get("text") or "").lower()
            if "couldn't find a match" in text:
                return True
            if "did you mean" in text:
                return True
            return False
        return False

    async def _reuse_prior_title(
        self,
        view: MessageView,
        title: str,
        *,
        media_kind: str = "",
        year: int | None = None,
    ) -> InboxResult | None:
        """Resolve a previously stated title after an anaphoric follow-up."""
        search_title = catalog_search_title(title) or title
        if not search_title:
            return None
        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())
        kind = media_kind if media_kind in {"movie", "tv"} else ""
        try:
            resolved = await resolve_title(
                search_title,
                year=year,
                media_kind=kind,
                strict=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("prior-title reuse resolve failed: %s", redact(str(exc)))
            resolved = []
        rows = self._dedupe_choices([h.as_dict() for h in resolved])
        rows = [
            row
            for row in rows
            if catalog_seed_matches_title(search_title, str(row.get("title") or ""))
        ]
        if len(rows) == 1 or (rows and self._choices_are_indistinguishable(rows)):
            return await self._grab_catalog_row(
                view,
                rows[0],
                query=search_title,
                media_kind_hint=kind,
                skip_rate=True,
            )
        if len(rows) > 1:
            reply = format_ambiguous(search_title, rows)
            row_kind = str(rows[0].get("mediaType") or kind or "movie")
            self._remember_pending(
                view,
                options=rows,
                media_kind=row_kind if row_kind in {"movie", "tv"} else "movie",
                query=search_title,
                reply=reply,
            )
            return InboxResult(handled=True, reply=reply)
        return self._ask_guess_confirm(
            view,
            {
                "title": search_title,
                "year": year,
                "mediaType": kind or "movie",
            },
            query=search_title,
        )

    def _ask_guess_confirm(
        self,
        view: MessageView,
        row: dict[str, Any],
        *,
        query: str = "",
    ) -> InboxResult:
        """Ask about one guessed title — never queue, never list-less 1–N."""
        title = str(row.get("title") or query or "Untitled")
        year = row.get("year")
        try:
            year_i = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year_i = None
        kind = str(row.get("mediaType") or row.get("media_kind") or "movie")
        if kind not in {"movie", "tv"}:
            kind = "movie"
        reply = format_guess_confirm(title, year_i)
        option = {
            "title": title,
            "year": year_i,
            "mediaType": kind,
            "tmdbId": row.get("tmdbId") or row.get("mediaId"),
            "tvdbId": row.get("tvdbId"),
            "mediaId": row.get("mediaId") or row.get("tmdbId"),
        }
        self._remember_pending(
            view,
            options=[option],
            media_kind=kind,
            query=title,
            reply=reply,
        )
        self.memory.set_subject(
            view.chat_id,
            title,
            media_kind=kind,
            offered=[option],
            clear_rejected=False,
        )
        return InboxResult(handled=True, reply=reply)

    async def _confirm_plot_guess(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        intent: IntentDecision,
        *,
        catalog_hits: list[dict[str, Any]],
    ) -> InboxResult:
        """Resolve a plot/vibe guess to catalog rows, then ask (never auto-queue)."""
        search_title = catalog_search_title(intent.search_title) or intent.search_title
        media_kind = (
            intent.media_kind
            if intent.media_kind in {"movie", "tv"}
            else (parsed.media_kind if parsed.media_kind in {"movie", "tv"} else "")
        )
        rows = list(catalog_hits)
        if not rows:
            try:
                hits = await resolve_title(
                    search_title,
                    year=intent.year,
                    media_kind=media_kind,
                )
            except Exception as exc:  # noqa: BLE001
                log.info("plot-guess catalog resolve failed: %s", redact(str(exc)))
                hits = []
            rows = self._dedupe_choices([h.as_dict() for h in hits])
            # Prefer title matches for the guessed name.
            grounded = [
                row
                for row in rows
                if titles_match(str(row.get("title") or ""), search_title)
            ]
            rows = grounded or rows

        if len(rows) > 1 and not self._choices_are_indistinguishable(rows):
            reply = format_ambiguous(search_title, rows)
            kind = str(rows[0].get("mediaType") or media_kind or "movie")
            self._remember_pending(
                view,
                options=rows,
                media_kind=kind if kind in {"movie", "tv"} else "movie",
                query=search_title,
                reply=reply,
            )
            return InboxResult(handled=True, reply=reply)

        if rows:
            pick = rows[0]
            # Prefer model year when catalog row has none.
            if pick.get("year") in (None, "") and intent.year:
                pick = {**pick, "year": intent.year}
            return self._ask_guess_confirm(view, pick, query=search_title)

        # No catalog hit — still ask about the model's best guess.
        return self._ask_guess_confirm(
            view,
            {
                "title": search_title,
                "year": intent.year,
                "mediaType": media_kind or "movie",
            },
            query=search_title,
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
        """Queue a known Overseerr/TMDB row by id — no fuzzy title re-search."""
        if not skip_rate and not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())
        kind = str(
            media_kind_hint
            or row.get("mediaType")
            or ("tv" if row.get("tvdbId") else "movie")
        )
        if kind not in {"movie", "tv"}:
            kind = "movie"
        title = str(row.get("title") or query or "Untitled")
        year = int(row["year"]) if row.get("year") not in (None, "") else None
        tmdb = row.get("tmdbId") or row.get("mediaId")
        tvdb = row.get("tvdbId")
        resolved = ParsedRequest(
            kind="request",
            media_kind=kind,  # type: ignore[arg-type]
            title=title,
            year=year,
            tmdb_id=int(tmdb) if tmdb not in (None, "") else None,
            tvdb_id=int(tvdb) if tvdb not in (None, "") else None,
            reason="catalog_in_loop",
        )
        self.pending.pop(view.chat_id, None)
        self.memory.set_subject(
            view.chat_id,
            title,
            media_kind=kind,
            clear_rejected=True,
            clear_offered=True,
        )
        return await self._grab(view, resolved, exact=True)

    async def _handle_retry(
        self, view: MessageView, intent: IntentDecision
    ) -> InboxResult:
        """User asked to retry a stalled/failed grab from another *arr source."""
        title = (intent.search_title or "").strip()
        subject, subject_kind = self.memory.subject(view.chat_id)
        if not title:
            title = self.progress.active_title_for(view.chat_id) or subject
        if not title:
            return InboxResult(
                handled=True,
                reply=(
                    "Which download should I retry? Name the title "
                    "(or wait until one is queued)."
                ),
            )
        media_kind = intent.media_kind or subject_kind
        service = self.progress.active_service_for(view.chat_id, title)
        if not service:
            if media_kind == "tv":
                service = "sonarr"
            elif media_kind == "movie":
                service = "radarr"
            else:
                # Prefer radarr; fall through to sonarr if that queue misses.
                service = "radarr"
        client = radarr if service == "radarr" else sonarr
        try:
            result = await client.retry_download(
                title, force=True, reason="user:telegram"
            )
        except Exception as exc:  # noqa: BLE001
            log.info("telegram retry failed: %s", redact(str(exc)))
            return InboxResult(
                handled=True,
                reply=f"Couldn't retry {title}.",
            )
        # If movie miss and we guessed radarr, try sonarr once.
        if (
            not result.get("ok")
            and result.get("reason") == "not_found"
            and service == "radarr"
            and media_kind != "movie"
        ):
            try:
                result = await sonarr.retry_download(
                    title, force=True, reason="user:telegram"
                )
                service = "sonarr"
            except Exception as exc:  # noqa: BLE001
                log.info("telegram sonarr retry failed: %s", redact(str(exc)))
        spoken = str(result.get("speak") or "").strip()
        if not spoken:
            spoken = f"Couldn't retry {title}."
        if result.get("ok"):
            year = intent.year
            self.progress.track(view.chat_id, title, service, year)
            self.memory.set_subject(
                view.chat_id,
                title,
                media_kind="tv" if service == "sonarr" else "movie",
            )
        return InboxResult(
            handled=True,
            reply=spoken,
            grabbed=bool(result.get("ok")),
            title=str(result.get("title") or title),
            service=service,
            year=intent.year,
        )

    async def _handle_pick(self, view: MessageView, parsed: ParsedRequest) -> InboxResult:
        pending = self._pending_for(view.chat_id)
        if not pending or parsed.pick_index is None:
            return InboxResult(handled=True, reply="")
        return await self._handle_indices(view, pending, [parsed.pick_index])

    async def _handle_indices(
        self,
        view: MessageView,
        pending: PendingDisambiguation,
        indices: list[int],
    ) -> InboxResult:
        picks: list[dict[str, Any]] = []
        for pick_index in indices[:MAX_BATCH]:
            idx = pick_index - 1
            if idx < 0 or idx >= len(pending.options):
                return InboxResult(
                    handled=True,
                    reply=f"Pick 1–{min(3, len(pending.options))} from the list "
                    "(or say 'all of them').",
                )
            picks.append(pending.options[idx])
        if not picks:
            return InboxResult(handled=True, reply="")
        if not self.rate.allow():
            return InboxResult(handled=True, reply=format_rate_limited())

        # Consume pending before queueing so retries don't double-apply.
        del self.pending[view.chat_id]

        if len(picks) == 1:
            # Always grab THIS pending row's title/year/tmdbId — never a fresh
            # fuzzy search that could land on a different catalog hit.
            return await self._grab_catalog_row(
                view,
                picks[0],
                query=str(picks[0].get("title") or pending.query or ""),
                media_kind_hint=pending.media_kind,
                skip_rate=True,
            )

        queued_titles: list[str] = []
        via = "Overseerr"
        for pick in picks:
            synthetic = self._synthetic_from_pick(pending, pick)
            result = await self._grab(view, synthetic, exact=True, skip_rate=True)
            if result.grabbed and result.title:
                queued_titles.append(
                    f"{result.title} ({result.year})" if result.year else result.title
                )
            elif result.reply.startswith("Queued "):
                queued_titles.append(result.title or pick.get("title") or "")
        if queued_titles:
            return InboxResult(
                handled=True,
                reply=format_queued_many(queued_titles, via),
                grabbed=True,
                titles=queued_titles,
                service="overseerr",
            )
        # Nothing new queued — surface the last useful reply or a short summary.
        return InboxResult(
            handled=True,
            reply=f"Nothing new to queue for '{pending.query}'.",
        )

    def _synthetic_from_pick(
        self,
        pending: PendingDisambiguation,
        pick: dict[str, Any],
    ) -> ParsedRequest:
        return ParsedRequest(
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

    async def _grab(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        *,
        exact: bool = False,
        select_all: bool = False,
        skip_rate: bool = False,
    ) -> InboxResult:
        del skip_rate  # reserved; batch path rate-limits once up-front
        media_kind = parsed.media_kind
        exact_id = bool(parsed.imdb_id or parsed.tmdb_id or parsed.tvdb_id or exact)

        # Telegram Movies inbox: Overseerr only for search + request (movie and TV).
        # Radarr/Sonarr stay for download progress / queue tools, not title lookup.
        if media_kind == "unknown" and parsed.season is not None:
            media_kind = "tv"
        elif media_kind == "unknown" and parsed.tvdb_id:
            media_kind = "tv"

        try:
            return await self._grab_overseerr(
                view, parsed, media_kind, exact_id=exact_id, select_all=select_all
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram grab failed: %s", redact(str(exc)))
            query = parsed.display_label() or parsed.title or "that"
            return InboxResult(
                handled=True,
                reply=f"Couldn't queue '{query}' — Overseerr look-up failed.",
            )

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
            needle = normalize_title(parsed.title)
            exact_title = [
                row
                for row in hits
                if normalize_title(str(row.get("title") or "")) == needle
            ]
            if len(exact_title) == 1:
                return exact_title
            if len(exact_title) > 1 and not parsed.year:
                return exact_title[:MAX_CANDIDATES]
            # Substring / franchise hits — keep a bounded set for "all of them".
            related = [
                row
                for row in hits
                if needle and needle in normalize_title(str(row.get("title") or ""))
            ]
            if related:
                if len(related) == 1:
                    return related
                return related[:MAX_CANDIDATES]
            if not exact_title:
                if len(hits) == 1:
                    return hits
                return hits[:MAX_CANDIDATES]

        # Single hit is fine; multiple fuzzy hits need disambiguation.
        if len(hits) == 1:
            return hits
        return self._dedupe_choices(hits[:MAX_CANDIDATES])

    async def _already_queued(self, title: str, service: str) -> bool:
        client = radarr if service == "radarr" else sonarr
        try:
            payload = await client.queue(title)
        except Exception:  # noqa: BLE001
            return False
        return bool(payload.get("downloads"))

    async def _queue_many(
        self,
        view: MessageView,
        pending_query: str,
        media_kind: str,
        choices: list[dict[str, Any]],
    ) -> InboxResult:
        """Queue every choice through the existing single-title grab path."""
        synthetic_pending = PendingDisambiguation(
            chat_id=view.chat_id,
            options=choices,
            media_kind=media_kind,
            query=pending_query,
            created_message_id=view.message_id,
        )
        indices = list(range(1, min(len(choices), MAX_BATCH) + 1))
        # Stash then consume via the shared multi-pick path.
        self.pending[view.chat_id] = synthetic_pending
        return await self._handle_indices(view, synthetic_pending, indices)

    async def _grab_overseerr(
        self,
        view: MessageView,
        parsed: ParsedRequest,
        media_kind: str,
        *,
        exact_id: bool,
        select_all: bool = False,
    ) -> InboxResult:
        # Overseerr search term: title only (no actor clause, no raw tt…).
        query = catalog_search_title(parsed.title or "") or parsed.title or parsed.search_query()
        if parsed.tmdb_id and media_kind in {"movie", "tv"}:
            query = query or str(parsed.tmdb_id)
        elif parsed.imdb_id and not query:
            query = parsed.imdb_id

        found = await overseerr.search(query) if query else {"results": []}
        hits = [row for row in (found.get("results") or []) if row.get("matched") != "fallback"]

        # Prefer implied kind when present; if empty, keep both movie and TV.
        if media_kind in {"movie", "tv"}:
            want = "movie" if media_kind == "movie" else "tv"
            typed = [row for row in hits if (row.get("mediaType") or "") == want]
            if typed:
                hits = typed
            # else: implied kind missed — keep other type (TV-only title under movie ask)

        choices = self._dedupe_choices(self._filter_hits(hits, parsed, exact_id=exact_id))
        if parsed.tmdb_id:
            id_hits = [
                row
                for row in hits
                if row.get("tmdbId") == parsed.tmdb_id or row.get("mediaId") == parsed.tmdb_id
            ]
            if id_hits:
                choices = self._dedupe_choices(id_hits)[:1]
            elif exact_id:
                # Direct request by resolved TMDB id — skip fuzzy search miss.
                choices = [
                    {
                        "title": parsed.title or f"TMDB {parsed.tmdb_id}",
                        "year": parsed.year,
                        "mediaType": media_kind if media_kind in {"movie", "tv"} else "movie",
                        "mediaId": parsed.tmdb_id,
                        "tmdbId": parsed.tmdb_id,
                    }
                ]
        if not choices:
            return InboxResult(
                handled=True,
                reply=format_not_found(parsed.display_label() or query or "that"),
            )
        if select_all and len(choices) > 1 and not exact_id:
            return await self._queue_many(
                view,
                parsed.title or query or parsed.display_label(),
                media_kind if media_kind in {"movie", "tv"} else "movie",
                choices,
            )
        if (
            len(choices) > 1
            and not exact_id
            and not self._choices_are_indistinguishable(choices)
        ):
            reply = format_ambiguous(parsed.title or query or parsed.display_label(), choices)
            self._remember_pending(
                view,
                options=choices,
                media_kind=media_kind,
                query=parsed.title or query or parsed.display_label(),
                reply=reply,
            )
            return InboxResult(handled=True, reply=reply)
        pick = choices[0]
        title = str(pick.get("title") or parsed.title or "Untitled")
        year = pick.get("year") if pick.get("year") is not None else parsed.year
        year_i = int(year) if year not in (None, "") else None
        if pick.get("inLibrary"):
            return InboxResult(handled=True, reply=format_already(title, library=True))
        media_type = str(pick.get("mediaType") or media_kind or "movie")
        if media_type not in {"movie", "tv"}:
            media_type = "movie"
        # Progress tools still read *arr queues — skip if already downloading there.
        progress_service = "radarr" if media_type == "movie" else "sonarr"
        if await self._already_queued(title, progress_service):
            return InboxResult(handled=True, reply=format_already(title, queued=True))
        from hearth.fixtures import pipeline

        if any(
            normalize_title(str(row.get("title") or row.get("name") or ""))
            == normalize_title(title)
            for row in pipeline.overseerr_queue
        ):
            return InboxResult(handled=True, reply=format_already(title, queued=True))

        media_id = pick.get("mediaId") or pick.get("tmdbId") or parsed.tmdb_id
        result = await overseerr.request(
            title,
            media_id=int(media_id) if media_id else None,
            media_type=media_type,
        )
        if result.get("ok") is False:
            return InboxResult(
                handled=True,
                reply=format_not_found(parsed.display_label() or title),
            )
        self.progress.track(view.chat_id, title, progress_service, year_i)
        return InboxResult(
            handled=True,
            reply=format_queued(title, year_i, "Overseerr"),
            grabbed=True,
            title=title,
            service=progress_service,
            year=year_i,
        )
