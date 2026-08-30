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
    IntentDecision,
    clarify_wants_numbered_list,
    instant_pick_decision,
    interpret_intent,
    is_explicit_title_year,
    looks_like_chatter,
    looks_like_concrete_title,
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
    hit_to_parsed,
    resolve_parsed,
    resolve_title,
)
from hearth.telegram.progress import (
    ProgressTracker,
    format_already,
    format_ambiguous,
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
        seed_norm = normalize_title(seed)
        grounded = [
            row
            for row in rows
            if titles_match(str(row.get("title") or ""), seed)
            or seed_norm in normalize_title(str(row.get("title") or ""))
            or normalize_title(str(row.get("title") or "")) in seed_norm
        ]
        rows = (grounded or rows)[:MAX_CANDIDATES]
        rejected_norm = {
            normalize_title(t) for t in (rejected_titles or []) if str(t).strip()
        }
        if rejected_norm:
            rows = [
                row
                for row in rows
                if normalize_title(str(row.get("title") or "")) not in rejected_norm
                and not any(
                    normalize_title(str(row.get("title") or "")) in r
                    or r in normalize_title(str(row.get("title") or ""))
                    for r in rejected_norm
                    if r
                )
            ]
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

        # Instant: all of them / de eerste only while a numbered list is on screen.
        live_options = pending.options if pending else (memory_offered or None)
        instant = instant_pick_decision(view.text, live_options)
        if instant is not None and instant.action in {"pick", "pick_many"}:
            active_pending = pending
            if (
                active_pending is None
                and instant.indices
                and memory_offered
            ):
                active_pending = PendingDisambiguation(
                    chat_id=view.chat_id,
                    options=memory_offered[:MAX_CANDIDATES],
                    media_kind=subject_media_kind or "movie",
                    query=subject_title or "",
                    created_message_id=view.message_id,
                    last_bot_reply=history[-1]["text"] if history else "",
                )
                self.pending[view.chat_id] = active_pending
            handled = await self._apply_intent(
                view, parsed, instant, pending=active_pending
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

        # Everything else: gpt-4o with Overseerr candidates on the SAME turn.
        # Non-instant reply while a 1–N list is pending → pivot: remember those
        # titles as rejected, clear the sticky list, then refill candidates from
        # a fresh catalog search for the new user text.
        catalog_hits: list[dict[str, Any]] = []
        pending_query = (pending.query if pending else subject_title) or ""
        last_bot = (
            pending.last_bot_reply
            if pending
            else (history[-1]["text"] if history else "")
        )
        pivoted_off_list = False
        if pending is not None:
            rejected_now = self._titles_from_options(pending.options)
            if pending.query:
                rejected_now.append(pending.query)
            self.memory.remember_rejected(
                view.chat_id,
                rejected_now,
                clear_offered=True,
                clear_subject=True,
            )
            rejected = list(self.memory.rejected(view.chat_id))
            self.pending.pop(view.chat_id, None)
            pending = None
            pending_query = ""
            subject_title, subject_media_kind = "", ""
            memory_offered = []
            pivoted_off_list = True

        # New/repeated title ask that does not match sticky subject → clear it
        # so Da Vinci never leaks into a Christophers turn.
        if (
            subject_title
            and looks_like_concrete_title(view.text)
            and not subject_matches_user_title(subject_title, view.text)
        ):
            self.memory.remember_rejected(
                view.chat_id,
                [],
                clear_offered=False,
                clear_subject=True,
            )
            subject_title, subject_media_kind = "", ""
            pending_query = ""

        # Catalog-in-the-loop: search Overseerr (actor clause stripped) and pass
        # real hits to the model as candidates this turn.
        if not looks_like_chatter(view.text):
            catalog_hits = await self._catalog_candidates_for_message(
                view, parsed, rejected_titles=rejected
            )

        # Prefer fresh catalog hits; fall back to last offered list only when
        # we did not just reject/pivot and catalog returned nothing.
        if catalog_hits:
            candidates_for_model: list[dict[str, Any]] | None = catalog_hits
        elif not pivoted_off_list and memory_offered:
            candidates_for_model = memory_offered
        else:
            candidates_for_model = None

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
        )

        # Soft-revive offered rows only when the model explicitly picks indices
        # and we did not just reject the on-screen list — prefer this turn's
        # catalog hits so picks map to real Overseerr rows.
        active_pending = pending
        pick_rows = candidates_for_model or memory_offered
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
            return self._finish(
                view,
                handled,
                search_title=finish_title,
                media_kind=intent.media_kind or subject_media_kind,
                offered=catalog_hits or None,
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
        if intent.action == "ignore":
            return InboxResult(handled=True, reply="")
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
            question = intent.clarify_question or (
                "Which movie or series did you mean? Send the title if you know it."
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
                    return await self._grab_catalog_row(
                        view, hits[0], query=view.text
                    )
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
            # Actor/cast clauses are clues for the model — strip from Overseerr query.
            search_title = catalog_search_title(intent.search_title) or intent.search_title
            self.memory.set_subject(
                view.chat_id,
                search_title,
                media_kind=media_kind if media_kind in {"movie", "tv"} else "",
                clear_rejected=True,
            )
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
                select_all=intent.select_all,
            )
        return None

    async def _grab_catalog_row(
        self,
        view: MessageView,
        row: dict[str, Any],
        *,
        query: str = "",
        media_kind_hint: str = "",
    ) -> InboxResult:
        """Queue a known Overseerr/TMDB row by id — no fuzzy title re-search."""
        if not self.rate.allow():
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
        )
        return await self._grab(view, resolved, exact=True)
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
            return await self._grab(view, self._synthetic_from_pick(pending, picks[0]), exact=True)

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
