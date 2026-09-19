"""Overseerr-first Telegram media bot.

**Jev-first media router:** every media-ish turn hits TypeSafe System One
(Choice/Noul/Score) to classify exact title / franchise / series-all / edition /
descriptive riddle / chat-about. OpenAI (gpt-4o) runs only when Jev says a
descriptive riddle or needs_llm (or fail-open). Get / yes confirm remains the
only queue boundary — never invent a grab from chat alone.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from typing import Any

from rapidfuzz import fuzz

from hearth.config import settings
from hearth.jev import evaluate_telegram_media, log_shadow_outcome, noul_high
from hearth.telegram.callbacks import CallbackCodec, CallbackError
from hearth.telegram.heuristics import (
    looks_like_concrete_title,
    looks_like_confirm_no,
    looks_like_confirm_yes,
)
from hearth.telegram.media import (
    MediaIntent,
    answer_catalog_question,
    classify_media_ask,
    guess_catalog_titles,
)
from hearth.telegram.models import BotReply, MediaHit, MediaQuery, MessageView
from hearth.telegram.parse import parse_message
from hearth.telegram.progress import (
    ProgressTracker,
    format_guess_confirm,
    format_reject_download,
    matching_request_row,
)
from hearth.telegram.safeguards import RateLimiter, authorized
from hearth.telegram.store import TelegramStore
from hearth.tools.arr import OverseerrError, overseerr, title_seed_matches

log = logging.getLogger("hearth.telegram")

MAX_RESULTS = 5
SERIES_MAX_RESULTS = 8
HELP_TEXT = (
    "Send a movie or series title and I’ll search Overseerr. Describe a plot or "
    "vibe and I’ll guess, then ask before requesting. Ask for a whole franchise "
    "(e.g. Harry Potter, all movies) or an edition (extended, director’s cut). "
    "Tap Get on the exact match to request it — I never queue from chat alone. "
    "Commands: /search <title>, /status, /help."
)
_PENDING_GUESS_PREFIX = "guess:"

_STATUS_MARKS = {
    1: "○ Not requested",
    2: "◷ Pending approval",
    3: "◷ Requested",
    4: "◐ Partly available",
    5: "✓ In Plex",
    # Archived Overseerr used 6 for deleted; current Seerr uses it for
    # blocklisted. Keep the label honest across both servers and let the
    # backend decide whether a fresh request is allowed.
    6: "◇ Blocklisted or deleted",
    7: "○ Removed",
}


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalized(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _display_title(title: str, year: int | None = None) -> str:
    clean = (title or "").strip() or "that title"
    return f"{clean} ({year})" if year else clean


def _kind_label(media_type: str) -> str:
    return "movie" if media_type == "movie" else "TV"


def _button_label(
    index: int,
    hit: MediaHit,
    *,
    season: int | None = None,
) -> str:
    year_bit = f" ({hit.year})" if hit.year is not None else ""
    season_bit = f" S{season:02d}" if season is not None else ""
    # Telegram shows ~64 visible chars; keep index + title + year + kind.
    return f"Get {index} · {hit.title}{year_bit} {_kind_label(hit.media_type)}{season_bit}"


class TelegramMediaBot:
    """Transport-independent Telegram message and callback handlers."""

    def __init__(
        self,
        store: TelegramStore,
        *,
        overseerr_client: Any | None = None,
        progress: ProgressTracker | None = None,
    ) -> None:
        self.store = store
        self.overseerr = overseerr_client or overseerr
        self.progress = progress or ProgressTracker(overseerr_client=self.overseerr)
        self.rate = RateLimiter()
        self.bot_user_id: int | None = None
        self._codec: CallbackCodec | None = None
        self._codec_signature: tuple[str, int] | None = None

    def reset(self) -> None:
        self.rate.reset()
        self.progress.reset()
        self.bot_user_id = None

    @property
    def backend_configured(self) -> bool:
        if self.overseerr is overseerr:
            return settings.overseerr_configured
        return bool(getattr(self.overseerr, "live", True))

    def _callback_codec(self) -> CallbackCodec:
        # APP_SECRET_KEY is preferred; a Telegram bot token is already a strong
        # server-side secret and is a safe fallback for a domain-separated key.
        secret = settings.app_secret_key.strip() or settings.telegram_bot_token.strip()
        ttl = max(60, int(settings.telegram_callback_ttl_seconds))
        signature = (secret, ttl)
        if not secret:
            raise RuntimeError("Telegram callback signing secret is not configured")
        if self._codec is None or self._codec_signature != signature:
            self._codec = CallbackCodec(secret, ttl_seconds=ttl)
            self._codec_signature = signature
        return self._codec

    def _authorized(self, chat_id: int, user_id: int | None) -> bool:
        return authorized(
            chat_id=chat_id,
            user_id=user_id,
            chat_allowlist=settings.telegram_chat_id_list,
            user_allowlist=settings.telegram_user_id_list,
            bot_user_id=self.bot_user_id,
        )

    @staticmethod
    def _pending_key(chat_id: int) -> str:
        return f"{_PENDING_GUESS_PREFIX}{int(chat_id)}"

    def _get_pending_guess(self, chat_id: int) -> dict[str, Any] | None:
        payload = self.store.get_callback_media(self._pending_key(chat_id))
        if not payload:
            return None
        media_type = str(payload.get("media_type") or "").lower()
        tmdb_id = _integer(payload.get("tmdb_id"))
        title = str(payload.get("title") or "").strip()
        if media_type not in {"movie", "tv"} or tmdb_id is None or not title:
            self._clear_pending_guess(chat_id)
            return None
        return payload

    def _set_pending_guess(self, chat_id: int, hit: MediaHit, *, season: int | None) -> None:
        ttl = max(60, int(settings.telegram_callback_ttl_seconds))
        self.store.put_callback_media(
            self._pending_key(chat_id),
            {
                "chat_id": chat_id,
                "media_type": hit.media_type,
                "tmdb_id": hit.tmdb_id,
                "title": hit.title,
                "year": hit.year,
                "season": season,
            },
            ttl_s=ttl,
        )

    def _clear_pending_guess(self, chat_id: int) -> None:
        self.store.clear_callback_media(self._pending_key(chat_id))

    async def handle_message(self, message: dict[str, Any]) -> BotReply | None:
        view = MessageView.from_telegram(message)
        if view is None or not self._authorized(view.chat_id, view.user_id):
            return None

        pending = self._get_pending_guess(view.chat_id)
        regex_yes = looks_like_confirm_yes(view.text)
        regex_no = looks_like_confirm_no(view.text)

        # Pending-guess confirm/cancel: Jev may sharpen yes/nah in enforce mode.
        # Never invent a queue without a pending guess (Overseerr confirm rule).
        if pending is not None and settings.jev_enabled:
            jev_verdict = await evaluate_telegram_media(view.text)
            log_shadow_outcome(
                jev_verdict,
                channel="telegram_pending_guess",
                tools=[],
                outcome=(
                    "regex_yes"
                    if regex_yes
                    else "regex_no"
                    if regex_no
                    else "pending_guess"
                ),
            )
            if jev_verdict.enforcing and jev_verdict.ok:
                if noul_high(
                    jev_verdict.answers,
                    "is_cancel",
                    settings.jev_cancel_threshold,
                ):
                    self._clear_pending_guess(view.chat_id)
                    return BotReply(
                        "Okay — not queueing that. Send another title or description."
                    )
                if noul_high(
                    jev_verdict.answers,
                    "is_confirm",
                    settings.jev_confirm_threshold,
                ):
                    return await self._queue_pending_guess(view, pending)

        if pending is not None and regex_yes:
            return await self._queue_pending_guess(view, pending)
        if pending is not None and regex_no:
            self._clear_pending_guess(view.chat_id)
            return BotReply("Okay — not queueing that. Send another title or description.")
        if pending is None and (regex_yes or regex_no):
            # Bare yes/nah/no without an on-screen guess must never invent a queue.
            return None

        _, query = parse_message(
            message,
            max_length=max(20, int(settings.telegram_max_title_length)),
            bot_user_id=self.bot_user_id,
        )
        # Durable update ids in TelegramStore own transport deduplication. Do
        # not mark a message seen before its reply has actually been delivered:
        # a transient send failure must be able to replay the search.
        if query.action == "ignore":
            return None
        if query.action == "help":
            return BotReply(HELP_TEXT)
        if query.action == "status":
            return await self._status_reply()
        if query.action == "reject":
            return BotReply(self._rejection_text(query))

        self.rate.max_calls = max(1, int(settings.telegram_rate_limit_per_minute))
        self.rate.window_s = 60.0
        rate_key = (view.chat_id, view.user_id)
        if not self.rate.allow(rate_key):
            wait = max(1, math.ceil(self.rate.retry_after(rate_key)))
            return BotReply(f"Too many searches. Try again in about {wait} seconds.")

        # New search/guess replaces any sticky yes/no offer.
        self._clear_pending_guess(view.chat_id)

        # Typed TMDB ids stay on the exact Overseerr detail path.
        if query.tmdb_id is not None:
            return await self._search_reply(view, query)

        # Jev-first media router (fail-open to local heuristics).
        intent = await classify_media_ask(
            query.raw_text or query.title or view.text,
            parsed=query,
        )
        return await self._route_media_intent(view, query, intent)

    async def _route_media_intent(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
    ) -> BotReply:
        if intent.note == "list_ask" or intent.kind == "other":
            # List / chatter / not_media — never invent a queue from a vague ask.
            if intent.note == "list_ask":
                return BotReply(
                    "I don’t queue from a list ask. Send a title, franchise, "
                    "edition, or short description — then tap Get."
                )
            # Fall through: treat leftover "other" as a normal title search when
            # the parser already extracted something searchable.
            if query.title and looks_like_concrete_title(query.title):
                return await self._search_reply(view, query)
            return None

        if intent.kind == "chat_about":
            return await self._chat_about_reply(view, query, intent)

        if intent.kind == "describe" or intent.needs_llm:
            return await self._guess_reply(view, query, intent=intent)

        if intent.kind == "series_all":
            return await self._series_all_reply(view, query, intent)

        if intent.kind == "edition":
            return await self._edition_reply(view, query, intent)

        if intent.kind == "known_franchise":
            return await self._franchise_reply(view, query, intent)

        # exact_title — instant Overseerr path, no LLM.
        search_query = self._intent_search_query(query, intent)
        return await self._search_reply(view, search_query)

    @staticmethod
    def _intent_search_query(query: MediaQuery, intent: MediaIntent) -> MediaQuery:
        title = (intent.search_title or query.title or "").strip()
        year = intent.year if intent.year is not None else query.year
        media_type = intent.media_type if intent.media_type in {"movie", "tv"} else query.media_type
        return MediaQuery(
            action="search",
            media_type=media_type if media_type in {"movie", "tv"} else None,
            title=title,
            year=year,
            season=query.season,
            episode=query.episode,
            tmdb_id=query.tmdb_id,
            reason=query.reason or "title",
            raw_text=query.raw_text,
            catalog_host=query.catalog_host,
        )

    async def _status_reply(self) -> BotReply:
        if not self.backend_configured:
            return BotReply("Telegram is ready, but Overseerr is not configured.")
        probe_method = getattr(self.overseerr, "provider_probe", None)
        if probe_method is None:
            return BotReply(
                f"Telegram and Overseerr are configured. "
                f"Tracking {len(self.progress.active)} approved request(s)."
            )
        try:
            probe = await probe_method()
        except OverseerrError:
            return BotReply("Telegram is ready, but Overseerr is unreachable right now.")
        except Exception:  # noqa: BLE001
            log.exception("telegram Overseerr status probe failed")
            return BotReply("Telegram is ready, but the Overseerr health check failed.")
        if not probe.get("ok"):
            if probe.get("status") == "authentication_failed":
                return BotReply(
                    "Telegram is ready, but Overseerr rejected its configured API key."
                )
            return BotReply("Overseerr is reachable, but its TMDB provider is unavailable.")
        return BotReply(
            f"Telegram, Overseerr, and TMDB are ready. "
            f"Tracking {len(self.progress.active)} approved request(s)."
        )

    @staticmethod
    def _rejection_text(query: MediaQuery) -> str:
        if query.reason == "tmdb_type_required":
            return "Say whether it is a movie or series, for example tmdb:movie:603."
        if query.reason == "title_too_long":
            return "That title is too long. Send only the movie or series name."
        if query.reason == "movie_has_season":
            return "A movie cannot have a season. Send a series title or TV TMDB id."
        if query.reason == "episode_not_supported":
            return (
                "Overseerr requests whole seasons, not individual episodes. "
                "Send the series and season, for example Severance S02."
            )
        if query.reason in {"invalid_season", "ambiguous_catalog_id"}:
            return (
                "That catalog request is ambiguous. Send one TMDB movie/TV id "
                "with an optional numeric season such as S02."
            )
        return format_reject_download()

    async def _guess_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        *,
        intent: MediaIntent | None = None,
    ) -> BotReply:
        """LLM catalog resolve for descriptive riddles — never auto-queue."""
        if not self.backend_configured:
            return BotReply(
                "Overseerr is not configured, so I cannot run a real catalog search."
            )
        if not settings.openai_configured:
            return BotReply(
                "That sounds like a description. Send the movie or series title "
                "(or configure OpenAI so I can guess)."
            )

        guesses = await guess_catalog_titles(query.raw_text or query.title)
        if not guesses:
            return BotReply(
                "I’m not sure which title you mean. Send the movie or series name."
            )

        # Search the primary guess; if empty, try the next candidate once.
        hits: list[MediaHit] = []
        guessed = MediaQuery(action="search", reason="guess", raw_text=query.raw_text)
        for guess in guesses:
            guessed = MediaQuery(
                action="search",
                media_type=guess.media_kind if guess.media_kind in {"movie", "tv"} else None,
                title=guess.search_title,
                year=guess.year,
                reason="guess",
                raw_text=query.raw_text,
            )
            try:
                rows = await self._search_rows(guessed)
            except OverseerrError as exc:
                if exc.operation == "authentication" or exc.status_code in {401, 403}:
                    return BotReply(
                        "Overseerr rejected its configured API key. Fix the key or its "
                        "request permissions before searching again."
                    )
                return BotReply(
                    "Overseerr search is unavailable right now. This is a backend error, "
                    "not a catalog miss."
                )
            except Exception:  # noqa: BLE001
                log.exception("telegram guess search failed")
                return BotReply("Overseerr search failed unexpectedly. Try again shortly.")
            hits = self._rank_hits(rows, guessed)
            if hits:
                break

        if not hits:
            label = _display_title(guesses[0].search_title, guesses[0].year)
            return BotReply(
                f"Did you mean {label}? I couldn’t find it in Overseerr yet. "
                "Send the exact title or a TMDB link."
            )
        header = (
            format_guess_confirm(hits[0].title, hits[0].year)
            if len(hits) == 1
            else f"Which one for “{guessed.title}”?"
        )
        if intent and intent.source == "jev":
            header = f"{header}"
        return self._results_reply(
            view.chat_id,
            guessed,
            hits,
            header=header,
            remember_single_guess=True,
        )

    async def _chat_about_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
    ) -> BotReply:
        """Answer plot/year/cast questions — no Get buttons, never queue."""
        del view  # chat_id unused; info-only replies have no callbacks
        catalog_context = ""
        search_title = (intent.search_title or query.title or "").strip()
        if self.backend_configured and search_title and looks_like_concrete_title(search_title):
            try:
                probe = MediaQuery(
                    action="search",
                    title=search_title,
                    year=intent.year or query.year,
                    media_type=(
                        intent.media_type
                        if intent.media_type in {"movie", "tv"}
                        else query.media_type
                    ),
                    reason="chat_about",
                    raw_text=query.raw_text,
                )
                rows = await self._search_rows(probe)
                hits = self._rank_hits(rows, probe)
                if hits:
                    top = hits[0]
                    bits = [top.display_label()]
                    if top.overview:
                        bits.append(top.overview[:220])
                    catalog_context = " — ".join(bits)
            except Exception:  # noqa: BLE001 — Q&A can proceed without catalog
                log.exception("telegram chat_about catalog lookup failed")

        if not settings.openai_configured:
            if catalog_context:
                return BotReply(catalog_context)
            return BotReply(
                "Ask with a clear title, or configure OpenAI so I can answer plot questions."
            )

        answered = await answer_catalog_question(
            query.raw_text or query.title,
            catalog_context=catalog_context,
        )
        if answered and answered.get("answer"):
            return BotReply(str(answered["answer"]))
        if catalog_context:
            return BotReply(catalog_context)
        return BotReply("I’m not sure. Send the title more clearly, or ask to get it.")

    async def _series_all_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
    ) -> BotReply:
        """Whole franchise/series — multi Get cards, never silent bulk queue."""
        search_query = self._intent_search_query(query, intent)
        if not search_query.title:
            return BotReply("Which franchise should I expand? Send the series name.")
        if not self.backend_configured:
            return BotReply(
                "Overseerr is not configured, so I cannot run a real catalog search."
            )
        try:
            rows = await self._search_rows(search_query)
        except OverseerrError as exc:
            if exc.operation == "authentication" or exc.status_code in {401, 403}:
                return BotReply(
                    "Overseerr rejected its configured API key. Fix the key or its "
                    "request permissions before searching again."
                )
            return BotReply(
                "Overseerr search is unavailable right now. This is a backend error, "
                "not a catalog miss."
            )
        except Exception:  # noqa: BLE001
            log.exception("telegram series_all search failed")
            return BotReply("Overseerr search failed unexpectedly. Try again shortly.")

        hits = self._rank_hits(
            rows,
            search_query,
            franchise_seed=search_query.title,
            limit=SERIES_MAX_RESULTS,
        )
        if not hits:
            return BotReply(f"No Overseerr matches for “{search_query.display_label()}”.")
        return self._results_reply(
            view.chat_id,
            search_query,
            hits,
            header=(
                f"Whole series for “{search_query.title}” — tap Get on each title "
                "you want (I won’t queue them all at once):"
            ),
            remember_single_guess=False,
        )

    async def _franchise_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
    ) -> BotReply:
        """Known franchise seed (e.g. Harry Potter) → franchise-aware Get cards."""
        search_query = self._intent_search_query(query, intent)
        if not self.backend_configured:
            return BotReply(
                "Overseerr is not configured, so I cannot run a real catalog search."
            )
        try:
            rows = await self._search_rows(search_query)
        except OverseerrError as exc:
            if exc.operation == "authentication" or exc.status_code in {401, 403}:
                return BotReply(
                    "Overseerr rejected its configured API key. Fix the key or its "
                    "request permissions before searching again."
                )
            return BotReply(
                "Overseerr search is unavailable right now. This is a backend error, "
                "not a catalog miss."
            )
        except Exception:  # noqa: BLE001
            log.exception("telegram franchise search failed")
            return BotReply("Overseerr search failed unexpectedly. Try again shortly.")

        hits = self._rank_hits(
            rows,
            search_query,
            franchise_seed=search_query.title,
            limit=SERIES_MAX_RESULTS,
        )
        if not hits:
            return BotReply(f"No Overseerr matches for “{search_query.display_label()}”.")
        header = (
            f"“{search_query.title}” franchise — pick a title:"
            if len(hits) > 1
            else None
        )
        return self._results_reply(
            view.chat_id,
            search_query,
            hits,
            header=header,
            remember_single_guess=len(hits) == 1,
        )

    async def _edition_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
    ) -> BotReply:
        """Edition/cut preference — search clean title, never literal edition string."""
        search_query = self._intent_search_query(query, intent)
        if not search_query.title:
            return BotReply("Which title should I look up with that edition preference?")
        if not self.backend_configured:
            return BotReply(
                "Overseerr is not configured, so I cannot run a real catalog search."
            )
        try:
            rows = await self._search_rows(search_query)
        except OverseerrError as exc:
            if exc.operation == "authentication" or exc.status_code in {401, 403}:
                return BotReply(
                    "Overseerr rejected its configured API key. Fix the key or its "
                    "request permissions before searching again."
                )
            return BotReply(
                "Overseerr search is unavailable right now. This is a backend error, "
                "not a catalog miss."
            )
        except Exception:  # noqa: BLE001
            log.exception("telegram edition search failed")
            return BotReply("Overseerr search failed unexpectedly. Try again shortly.")

        seed = search_query.title
        hits = self._rank_hits(
            rows,
            search_query,
            franchise_seed=seed if len(seed.split()) >= 2 else None,
            limit=SERIES_MAX_RESULTS if len(seed.split()) >= 2 else MAX_RESULTS,
        )
        if not hits:
            return BotReply(f"No Overseerr matches for “{search_query.display_label()}”.")
        label = intent.edition_label or "preferred edition"
        header = (
            f"Resolved “{search_query.title}” for {label} — tap Get on the film(s). "
            f"I’ll note {label} when grabbing."
        )
        return self._results_reply(
            view.chat_id,
            search_query,
            hits,
            header=header,
            remember_single_guess=len(hits) == 1,
            edition_key=intent.edition_key,
            edition_label=intent.edition_label,
        )

    async def _queue_pending_guess(
        self,
        view: MessageView,
        pending: Mapping[str, Any],
    ) -> BotReply:
        """Queue the single on-screen guess after an explicit yes — never on nah/no."""
        media_type = str(pending.get("media_type") or "").lower()
        tmdb_id = _integer(pending.get("tmdb_id"))
        title = str(pending.get("title") or "").strip()
        year = _integer(pending.get("year"))
        season = _integer(pending.get("season"))
        if media_type not in {"movie", "tv"} or tmdb_id is None or not title:
            self._clear_pending_guess(view.chat_id)
            return BotReply("That guess expired. Search again.")

        label = _display_title(title, year)
        if season is not None:
            label = f"{label} · Season {season}"
        seasons: list[int] | str | None = None
        if media_type == "tv":
            seasons = [season] if season is not None else "all"

        try:
            result = await self.overseerr.request(
                query=title,
                media_id=tmdb_id,
                media_type=media_type,
                seasons=seasons,
            )
        except OverseerrError:
            return BotReply(
                f"The request outcome for {label} is uncertain because Overseerr did "
                "not answer. Check Overseerr before trying again."
            )
        except Exception:  # noqa: BLE001
            log.exception("telegram pending-guess request failed")
            return BotReply(
                f"The request outcome for {label} is uncertain. Check Overseerr before "
                "trying again."
            )

        self._clear_pending_guess(view.chat_id)
        if not result.get("ok"):
            return BotReply(self._request_error_text(label, result))

        request_status = _integer(result.get("requestStatus"))
        media_status = _integer(result.get("mediaStatus"))
        request_id = _integer(result.get("requestId"))
        state, text = self._accepted_text(
            label,
            request_status=request_status,
            media_status=media_status,
        )
        season_key = "all" if season is None else str(season)
        media_key = f"{media_type}:{tmdb_id}:{season_key}"
        request_key = f"{view.chat_id}:{media_key}"
        durable_state = (
            "pending" if request_status == 2 and media_status != 5 else state
        )
        try:
            self.store.upsert_request(
                request_key,
                media_type=media_type,
                tmdb_id=tmdb_id,
                title=title,
                season=season,
                external_request_id=request_id,
                state=durable_state,
                metadata={
                    "chat_id": view.chat_id,
                    "year": year,
                    "request_status": request_status,
                    "media_status": media_status,
                    "tracked": None,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to journal yes-confirmed Overseerr request")
            return BotReply(
                f"Overseerr accepted {label}, but Hearth could not save its local "
                "tracking state. Check Overseerr before trying again."
            )

        if durable_state == "pending" and request_status == 2:
            try:
                self.progress.track(
                    view.chat_id,
                    title,
                    "radarr" if media_type == "movie" else "sonarr",
                    year,
                    season=season,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    request_id=request_id,
                    request_key=request_key,
                    request_status=request_status,
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to attach tracker after yes-confirm")
        return BotReply(text)

    async def _search_reply(self, view: MessageView, query: MediaQuery) -> BotReply:
        if not self.backend_configured:
            return BotReply(
                "Overseerr is not configured, so I cannot run a real catalog search."
            )
        try:
            rows = await self._search_rows(query)
        except OverseerrError as exc:
            if exc.operation == "authentication" or exc.status_code in {401, 403}:
                return BotReply(
                    "Overseerr rejected its configured API key. Fix the key or its "
                    "request permissions before searching again."
                )
            return BotReply(
                "Overseerr search is unavailable right now. This is a backend error, "
                "not a catalog miss."
            )
        except Exception:  # noqa: BLE001
            log.exception("telegram search failed")
            return BotReply("Overseerr search failed unexpectedly. Try again shortly.")

        hits = self._rank_hits(rows, query)
        if not hits:
            return BotReply(f"No Overseerr matches for “{query.display_label()}”.")
        return self._results_reply(view.chat_id, query, hits)

    async def _search_rows(self, query: MediaQuery) -> list[dict[str, Any]]:
        if query.tmdb_id is not None:
            if query.media_type not in {"movie", "tv"}:
                return []
            payload = await self.overseerr.media_details(query.tmdb_id, query.media_type)
            if not payload.get("ok"):
                return []
            media = payload.get("media")
            if not isinstance(media, dict):
                media = {
                    "mediaType": query.media_type,
                    "mediaId": query.tmdb_id,
                    "title": query.title or f"TMDB {query.tmdb_id}",
                    "mediaStatus": payload.get("mediaStatus"),
                }
            else:
                # Movie/TV detail routes already encode the kind in their URL,
                # so official payloads do not consistently repeat mediaType.
                # Preserve the exact typed id from the parsed Telegram input.
                media = dict(media)
                media["mediaType"] = query.media_type
                media["tmdbId"] = query.tmdb_id
            return [media]

        title = (query.title or "").strip()
        if len(title) < 2 or not any(character.isalnum() for character in title):
            return []

        payload = await self.overseerr.search(title, page=1)
        if not payload.get("ok"):
            if payload.get("reason") == "authentication_failed":
                raise OverseerrError(
                    "Overseerr authentication failed",
                    operation="authentication",
                    status_code=_integer(payload.get("status_code")),
                )
            if payload.get("reason") == "provider_unavailable":
                raise OverseerrError(
                    "Overseerr TMDB provider is unavailable",
                    operation="search",
                )
            raise OverseerrError("Overseerr search failed", operation="search")
        return [row for row in (payload.get("results") or []) if isinstance(row, dict)]

    @staticmethod
    def _rank_hits(
        rows: list[dict[str, Any]],
        query: MediaQuery,
        *,
        franchise_seed: str | None = None,
        limit: int = MAX_RESULTS,
    ) -> list[MediaHit]:
        hits: list[MediaHit] = []
        seen: set[tuple[str, int]] = set()
        for row in rows:
            try:
                hit = MediaHit.from_overseerr(row)
            except ValueError:
                continue
            key = (hit.media_type, hit.tmdb_id)
            if key in seen:
                continue
            if query.media_type and hit.media_type != query.media_type:
                continue
            seen.add(key)
            hits.append(hit)

        asked = _normalized(query.title)
        seed = _normalized(franchise_seed or "")
        # Franchise / series-all: keep prefix matches for the seed.
        if seed:
            seeded = [
                hit
                for hit in hits
                if title_seed_matches(franchise_seed or "", hit.title)
                or title_seed_matches(franchise_seed or "", hit.original_title)
            ]
            if seeded:
                hits = seeded
        # Short exact titles must not become substring menus (Land→La La Land).
        elif asked and looks_like_concrete_title(query.title):
            seeded = [
                hit
                for hit in hits
                if title_seed_matches(query.title, hit.title)
                or title_seed_matches(query.title, hit.original_title)
            ]
            if seeded:
                hits = seeded

        def score(hit: MediaHit) -> float:
            title = _normalized(hit.title)
            original = _normalized(hit.original_title)
            candidates = [candidate for candidate in (title, original) if candidate]
            relevance = (
                max(float(fuzz.WRatio(asked or seed, candidate)) for candidate in candidates)
                if (asked or seed) and candidates
                else 100.0
            )
            if asked and asked in candidates:
                relevance += 1000
            elif asked and any(candidate.startswith(asked) for candidate in candidates):
                relevance += 300
            if query.year is not None and hit.year == query.year:
                relevance += 500
            # Prefer earlier release years for franchise lists (stable order).
            if seed and hit.year is not None:
                relevance += max(0, 2100 - hit.year) / 100.0
            return relevance

        hits.sort(key=score, reverse=True)
        return hits[: max(1, int(limit))]

    def _results_reply(
        self,
        chat_id: int,
        query: MediaQuery,
        hits: list[MediaHit],
        *,
        header: str | None = None,
        remember_single_guess: bool = False,
        edition_key: str = "",
        edition_label: str = "",
    ) -> BotReply:
        lines = [header or f"Overseerr results for “{query.display_label()}”:"]
        buttons: list[list[dict[str, str]]] = []
        codec = self._callback_codec()
        ttl = max(60, int(settings.telegram_callback_ttl_seconds))
        requestable: list[tuple[MediaHit, int | None]] = []
        for index, hit in enumerate(hits, start=1):
            status = _STATUS_MARKS.get(hit.media_status, "○ Not requested")
            lines.append(f"{index}. {hit.display_label()} — {status}")
            explicitly_requesting_tv_season = (
                hit.media_type == "tv" and query.season is not None
            )
            non_requestable = hit.media_status == 5 or (
                hit.media_status in {2, 3} and not explicitly_requesting_tv_season
            )
            if non_requestable:
                continue
            season = query.season if hit.media_type == "tv" else None
            callback_data = codec.encode(
                hit.media_type,
                hit.tmdb_id,
                chat_id,
                season=season,
            )
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "media_type": hit.media_type,
                "tmdb_id": hit.tmdb_id,
                "title": hit.title,
                "year": hit.year,
                "season": season,
            }
            if edition_key:
                payload["edition_key"] = edition_key
                payload["edition_label"] = edition_label
            self.store.put_callback_media(
                callback_data,
                payload,
                ttl_s=ttl,
            )
            label = _button_label(index, hit, season=season)
            buttons.append(
                [{"text": label[:64], "callback_data": callback_data}]
            )
            requestable.append((hit, season))
        if remember_single_guess and len(requestable) == 1:
            hit, season = requestable[0]
            self._set_pending_guess(chat_id, hit, season=season)
            lines.append("Tap Get to request it, or reply yes / nah.")
        elif buttons:
            lines.append("Tap Get on the exact title to request it.")
        else:
            lines.append("Everything shown is already handled or unavailable.")
        return BotReply(
            "\n".join(lines),
            reply_markup={"inline_keyboard": buttons} if buttons else None,
        )

    async def handle_callback(self, callback: dict[str, Any]) -> BotReply | None:
        message = callback.get("message")
        message = message if isinstance(message, Mapping) else {}
        chat = message.get("chat")
        chat = chat if isinstance(chat, Mapping) else {}
        sender = callback.get("from")
        sender = sender if isinstance(sender, Mapping) else {}
        try:
            chat_id = int(chat["id"])
            message_id = int(message["message_id"])
        except (KeyError, TypeError, ValueError):
            return None
        user_id = _integer(sender.get("id"))
        if not self._authorized(chat_id, user_id):
            return None

        # A Get tap is the confirm — drop any sticky yes/nah offer for this chat.
        self._clear_pending_guess(chat_id)

        data = str(callback.get("data") or "")
        try:
            request = self._callback_codec().decode(data, chat_id)
        except CallbackError:
            return BotReply(
                "That button is invalid or expired. Search again for fresh results.",
                edit_message_id=message_id,
            )
        except RuntimeError:
            return BotReply(
                "Callback signing is not configured on Hearth.",
                edit_message_id=message_id,
            )

        metadata = self.store.get_callback_media(data) or {}
        if metadata and (
            _integer(metadata.get("chat_id")) != chat_id
            or metadata.get("media_type") != request.media_type
            or _integer(metadata.get("tmdb_id")) != request.tmdb_id
        ):
            return BotReply(
                "That result is stale. Search again for a fresh button.",
                edit_message_id=message_id,
            )

        callback_id = str(callback.get("id") or "")
        digest = hashlib.sha256(f"{chat_id}:{message_id}:{data}".encode()).hexdigest()[:32]
        season_key = "all" if request.season is None else str(request.season)
        media_key = f"{request.media_type}:{request.tmdb_id}:{season_key}"
        claimed = self.store.claim_callback(
            digest,
            callback_query_id=callback_id,
            chat_id=chat_id,
            user_id=user_id,
            media_key=media_key,
        )
        reclaimed_uncertain = False
        if not claimed:
            previous = self.store.callback_state(digest) or {}
            state = str(previous.get("state") or "done")
            if state == "uncertain":
                # A previous process may have stopped on either side of the
                # provider POST. Overseerr rejects duplicate media/season
                # requests, so reclaiming lets a pre-POST crash finish without
                # creating a second request after a post-POST crash.
                claimed = self.store.claim_callback(
                    digest,
                    callback_query_id=callback_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    media_key=media_key,
                    reclaim_uncertain=True,
                )
                reclaimed_uncertain = claimed
            if claimed:
                state = "processing"
            else:
                text = (
                    "This request is already being handled."
                    if state == "processing"
                    else "This button was already handled. Search again to refresh its status."
                )
                return BotReply(text, edit_message_id=message_id)

        title = str(metadata.get("title") or f"TMDB {request.tmdb_id}")
        year = _integer(metadata.get("year"))
        label = _display_title(title, year)
        if request.season is not None:
            label = f"{label} · Season {request.season}"
        seasons: list[int] | str | None = None
        if request.media_type == "tv":
            seasons = [request.season] if request.season is not None else "all"

        try:
            result = await self.overseerr.request(
                query=title,
                media_id=request.tmdb_id,
                media_type=request.media_type,
                seasons=seasons,
            )
        except OverseerrError as exc:
            self.store.finish_callback(digest, state="uncertain", error=str(exc))
            return BotReply(
                f"The request outcome for {label} is uncertain because Overseerr did "
                "not answer. Check Overseerr before trying again.",
                edit_message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("telegram Overseerr request failed")
            self.store.finish_callback(digest, state="uncertain", error=type(exc).__name__)
            return BotReply(
                f"The request outcome for {label} is uncertain. Check Overseerr before "
                "trying again.",
                edit_message_id=message_id,
            )

        recovered_duplicate = False
        duplicate_outcome = result.get("already") or result.get("reason") in {
            "already_requested",
            "no_seasons",
        }
        if not result.get("ok") and reclaimed_uncertain and duplicate_outcome:
            # The first process may have stopped after Overseerr committed the
            # POST but before Hearth journaled it.  Resolve an exact request id
            # when possible, then run the normal atomic acceptance path.  Even
            # if details are temporarily unavailable/ambiguous, the exact
            # media coordinates are durably retained for later reconciliation.
            recovered = await self._recover_uncertain_request(request, result)
            if recovered is not None:
                result = recovered
                recovered_duplicate = True

        if not result.get("ok"):
            callback_state = (
                "done"
                if result.get("already")
                or result.get("reason") in {"already_requested", "no_seasons"}
                else "failed"
            )
            self.store.finish_callback(
                digest,
                state=callback_state,
                error=str(result.get("reason") or result.get("status_code") or "rejected"),
            )
            return BotReply(
                self._request_error_text(label, result),
                edit_message_id=message_id,
            )

        request_status = _integer(result.get("requestStatus"))
        media_status = _integer(result.get("mediaStatus"))
        request_id = _integer(result.get("requestId"))
        state, text = self._accepted_text(
            label,
            request_status=request_status,
            media_status=media_status,
        )
        if recovered_duplicate:
            text = (
                f"{label} was already requested; Hearth recovered its tracking state."
            )
        request_key = f"{chat_id}:{media_key}"
        base_metadata = {
            "chat_id": chat_id,
            "year": year,
            "request_status": request_status,
            "media_status": media_status,
            "tracked": None,
        }
        # Approved work is first journaled as pending. If Hearth stops before
        # its in-memory tracker is attached, the pending reconciler can rebuild
        # it from the exact Overseerr request id.
        durable_state = (
            "pending" if request_status == 2 and media_status != 5 else state
        )
        try:
            self.store.record_request_and_finish_callback(
                digest,
                request_key,
                media_type=request.media_type,
                tmdb_id=request.tmdb_id,
                title=title,
                season=request.season,
                external_request_id=request_id,
                state=durable_state,
                metadata=base_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to journal accepted Overseerr request")
            try:
                self.store.finish_callback(
                    digest,
                    state="uncertain",
                    error=type(exc).__name__,
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to mark callback outcome uncertain")
            return BotReply(
                f"Overseerr accepted {label}, but Hearth could not save its local "
                "tracking state. Check Overseerr before trying again.",
                edit_message_id=message_id,
            )

        if durable_state == "pending" and request_status == 2:
            try:
                tracked = self.progress.track(
                    chat_id,
                    title,
                    "radarr" if request.media_type == "movie" else "sonarr",
                    year,
                    season=request.season,
                    tmdb_id=request.tmdb_id,
                    media_type=request.media_type,
                    request_id=request_id,
                    request_key=request_key,
                    request_status=request_status,
                )
            except Exception:  # noqa: BLE001
                # The durable pending row is the recovery path; never lose the
                # acknowledgement after the provider result was journaled.
                log.exception("failed to attach in-memory request tracker")
                tracked = None
            if tracked is not None:
                tracked_metadata = dict(base_metadata)
                tracked_metadata["tracked"] = tracked.to_dict()
                if not self.store.update_request(
                    request_key,
                    state="processing",
                    metadata=tracked_metadata,
                ):
                    log.warning("accepted request remains pending for reconciliation")
        return BotReply(text, edit_message_id=message_id)

    async def _recover_uncertain_request(
        self,
        request: Any,
        result: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Turn a replayed provider duplicate into a durable accepted outcome."""
        request_id = _integer(result.get("requestId"))
        request_status = _integer(result.get("requestStatus"))
        media_status = _integer(result.get("mediaStatus"))
        details: dict[str, Any] | None = None
        try:
            candidate = await self.overseerr.media_details(
                request.tmdb_id,
                request.media_type,
            )
            if (
                isinstance(candidate, dict)
                and candidate.get("ok") is True
                and candidate.get("mode") == "live"
            ):
                returned_id = _integer(candidate.get("mediaId"))
                returned_type = str(candidate.get("mediaType") or "").lower()
                if returned_id not in {None, request.tmdb_id}:
                    return None
                if returned_type and returned_type != request.media_type:
                    return None
                details = candidate
        except Exception:  # noqa: BLE001 - durable pending row is the fallback
            log.warning("could not resolve duplicate Overseerr request", exc_info=True)

        if details is not None:
            row = matching_request_row(
                details,
                request_id,
                media_type=request.media_type,
                season=request.season,
            )
            if row is not None:
                request_id = _integer(row.get("id")) or request_id
                request_status = _integer(row.get("status")) or request_status
            media_status = _integer(details.get("mediaStatus")) or media_status

        return {
            "ok": True,
            "mode": "live",
            "recovered": True,
            "requestId": request_id,
            "requestStatus": request_status,
            "mediaStatus": media_status,
        }

    @staticmethod
    def _request_error_text(label: str, result: Mapping[str, Any]) -> str:
        reason = str(result.get("reason") or "")
        if result.get("already") or reason == "already_requested":
            return f"{label} is already requested in Overseerr."
        if reason == "no_seasons":
            return f"Overseerr has no requestable seasons for {label}."
        if reason == "forbidden":
            return (
                f"Overseerr rejected {label}. Check the API key, request permission, "
                "quota, and blocklist."
            )
        if reason == "invalid_request":
            return f"Overseerr could not accept the request for {label}."
        return f"Overseerr did not accept the request for {label}."

    @staticmethod
    def _accepted_text(
        label: str,
        *,
        request_status: int | None,
        media_status: int | None,
    ) -> tuple[str, str]:
        if media_status == 5 or request_status == 5:
            return "available", f"{label} is already available in Plex."
        if request_status == 1:
            return "pending", f"Requested {label}; waiting for Overseerr approval."
        if request_status == 2:
            return "processing", f"Requested {label}; Overseerr sent it to the media stack."
        if request_status == 3:
            return "declined", f"Overseerr declined the request for {label}."
        if request_status == 4:
            return "failed", f"Overseerr marked the request for {label} as failed."
        return "pending", (
            f"Overseerr accepted the request for {label}; checking its approval status."
        )
