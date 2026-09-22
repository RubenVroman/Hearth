"""Telegram house bot with Overseerr-first media handling.

**Jev-first media router:** every media-ish turn hits TypeSafe System One
(Choice/Noul/Score) to pick a lane — exact title, known franchise, series-all,
edition, person filmography, mood/vibe, "something like X", a multi-title batch,
or an in-thread follow-up. OpenAI (gpt-4o) runs only when Jev says a descriptive
riddle or needs_llm (or fail-open). Get / yes confirm remains the only queue
boundary — never invent a grab from chat alone, and confirming queues by
mediaId, never by re-searching the title.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hearth.config import settings
from hearth.jev import (
    adopt_verdict,
    authorize_tool,
    evaluate_message,
    evaluate_telegram_media,
    log_shadow_outcome,
    noul_high,
    tool_turn,
)

if TYPE_CHECKING:  # pragma: no cover
    from hearth.jev.tools import ToolDecision
from hearth.telegram.callbacks import (
    ACTION_DISMISS,
    ACTION_MORE,
    ACTION_PLAY,
    ACTION_SERIES,
    ACTION_SIMILAR,
    ACTION_STATUS,
    ACTION_TITLE,
    CallbackCodec,
    CallbackError,
    is_action_callback,
)
from hearth.telegram.heuristics import (
    looks_like_concrete_title,
    looks_like_confirm_no,
    looks_like_confirm_yes,
)
from hearth.telegram.house import TelegramHouseCommands
from hearth.telegram.media import (
    MAX_RESULTS,
    SERIES_MAX_RESULTS,
    AskPart,
    CardRenderer,
    CatalogSearch,
    CatalogUnavailable,
    ChatContext,
    MediaIntent,
    MediaMemory,
    answer_catalog_question,
    apply_exclusions,
    best_franchise_seed,
    blocked_status_line,
    classify_media_ask,
    detect_mood,
    guess_catalog_titles,
    house_pick_spec,
    in_release_order,
    plausible_match,
    rank_hits,
    voice,
    without_ids,
)
from hearth.telegram.media.memory import speaker_scope, storage_key
from hearth.telegram.media.phrases import is_known_franchise
from hearth.telegram.media.play import looks_like_play_command, play_lane_enabled, play_on_tv
from hearth.telegram.media.watch_next import WatchNext, pick_next_in_order

from hearth.telegram.models import BotReply, MediaHit, MediaQuery, MessageView
from hearth.telegram.house import house_control_reply, looks_like_house_control
from hearth.telegram.parse import parse_message
from hearth.telegram.progress import (
    ProgressTracker,
    format_guess_confirm,
    format_reject_download,
    matching_request_row,
)
from hearth.telegram.safeguards import RateLimiter, authorized
from hearth.telegram.store import TelegramStore
from hearth.butler.decision import decide_butler_tool
from hearth.butler.nudge import queue_shelf_aside
from hearth.butler.phrases import classify_house_phrase
from hearth.butler.scenes import activate_preset
from hearth.butler.shelf import shelf_snapshot
from hearth.tools.arr import OverseerrError, overseerr, overseerr_request_error_text

log = logging.getLogger("hearth.telegram")

HELP_TEXT = (
    "House: /house, /lights, /lights <name> on|off|toggle|0-100, /scenes, "
    "/scene <name>, /covers, /cover <name> open|close|stop|0-100. "
    "Natural commands like “turn off kitchen lights”, “activate movie night”, "
    "and “close the living room blind” work too. "
    "If a name is unclear, list that device type first and use the exact name. "
    "Media: send a title and I’ll find it. I also do franchises (“all Harry Potters”), "
    "editions (“LOTR extended”), people (“anything with Florence Pugh”), vibes "
    "(“scary under 2 hours”), lookalikes (“something like Arrival”), and several "
    "at once (“grab Inception and Interstellar”). Follow-ups work too: “the "
    "sequel”, “all of them”, “more like that”. Ask “what’s on tonight” for "
    "what’s already on Plex, or “quiet hours” for the lights. "
    "Tap Get to request — I never queue from chat alone. House controls, when "
    "Home Assistant has them: house sleep, good morning, movie night mode, "
    "climate, feeder, purifier. "
    "Commands: /search <title>, /status, /help."
)
_PENDING_GUESS_PREFIX = "guess:"


def _catalog_movie_night(text: str) -> bool:
    """Bare movie/film/cinema night is a media vibe, not the scene preset."""
    raw = " ".join((text or "").strip().split()).strip(" .!?").casefold()
    return raw in {"movie night", "film night", "cinema night"}


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_title(title: str, year: int | None = None) -> str:
    return voice.display_title(title, year)


def _with_speaker(fn):  # type: ignore[no-untyped-def]
    """Keep group threads per person without rewriting every return path."""

    async def wrapper(self: Any, payload: dict[str, Any]) -> BotReply | None:
        chat_id, user_id = _actor(payload)
        if chat_id is None:
            return await fn(self, payload)
        with speaker_scope(chat_id, user_id):
            return await fn(self, payload)

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


def _actor(payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    if "data" in payload and isinstance(payload.get("message"), Mapping):
        message = payload["message"]
        chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
        sender = payload.get("from") if isinstance(payload.get("from"), Mapping) else {}
        try:
            return int(chat["id"]), _integer(sender.get("id"))
        except (KeyError, TypeError, ValueError):
            return None, None
    view = MessageView.from_telegram(payload)
    if view is None:
        return None, None
    return view.chat_id, view.user_id


class TelegramMediaBot:
    """Transport-independent Telegram message and callback handlers."""

    def __init__(
        self,
        store: TelegramStore,
        *,
        overseerr_client: Any | None = None,
        progress: ProgressTracker | None = None,
        house_commands: TelegramHouseCommands | None = None,
    ) -> None:
        self.store = store
        self.overseerr = overseerr_client or overseerr
        self.progress = progress or ProgressTracker(overseerr_client=self.overseerr)
        self.catalog = CatalogSearch(self.overseerr)
        self.memory = MediaMemory(store)
        self.house = house_commands or TelegramHouseCommands()
        self.rate = RateLimiter()
        self.bot_user_id: int | None = None
        self._codec: CallbackCodec | None = None
        self._codec_signature: tuple[str, int] | None = None

    def reset(self) -> None:
        self.rate.reset()
        self.progress.reset()
        self.bot_user_id = None

    def _cards(self) -> CardRenderer:
        return CardRenderer(
            self._callback_codec(),
            self.store,
            ttl_s=max(60, int(settings.telegram_callback_ttl_seconds)),
        )

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
        return storage_key(_PENDING_GUESS_PREFIX, chat_id)

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

    @_with_speaker
    async def handle_message(self, message: dict[str, Any]) -> BotReply | None:
        view = MessageView.from_telegram(message)
        if view is None or not self._authorized(view.chat_id, view.user_id):
            return None
        # One Jev scope per Telegram turn: the media router's verdict is reused
        # by the tool gate, so routing and authorization share a single call.
        with tool_turn(view.text, channel="telegram"):
            return await self._handle_message(view, message)

    async def _handle_message(
        self,
        view: MessageView,
        message: dict[str, Any],
    ) -> BotReply | None:
        house_reply = await self.house.handle(view.text)
        if house_reply is not None:
            # A new explicit house command supersedes any stale media yes/no offer.
            self._clear_pending_guess(view.chat_id)
            return house_reply

        pending = self._get_pending_guess(view.chat_id)
        regex_yes = looks_like_confirm_yes(view.text)
        regex_no = looks_like_confirm_no(view.text)

        # Pending-guess confirm/cancel: Jev may sharpen yes/nah in enforce mode.
        # Never invent a queue without a pending guess (Overseerr confirm rule).
        if pending is not None and settings.jev_active:
            jev_verdict = await evaluate_telegram_media(view.text)
            adopt_verdict(jev_verdict)
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
            return await self._offer_alternative(view)
        if pending is None and (regex_yes or regex_no):
            # Bare yes/nah/no without an armed offer must never invent a queue.
            # With a live card on screen it is still a real answer, so reply.
            context = self.memory.load(view.chat_id)
            if context is None or not context.hits:
                return None
            if regex_no:
                self.memory.forget(view.chat_id)
                return BotReply(voice.cancelled())
            if len(context.hits) == 1:
                return await self._confirm_context_pick(view, context, index=1)
            return BotReply(voice.which_one())

        if looks_like_play_command(view.text):
            return await self._play_from_context(view)

        aside = await self._house_aside(view)
        if aside is not None:
            return aside

        _, query = parse_message(
            message,
            max_length=max(20, int(settings.telegram_max_title_length)),
            bot_user_id=self.bot_user_id,
        )
        # Durable update ids in TelegramStore own transport deduplication. Do
        # not mark a message seen before its reply has actually been delivered:
        # a transient send failure must be able to replay the search.
        # Explicit house control (rituals, climate, feeder, purifier). This
        # sits above the chatter ignore list so "good morning" can run, and
        # above the media router so it never becomes a title search. Bare
        # "movie night" is still a catalog vibe and is not matched here.
        if looks_like_house_control(view.text):
            self._clear_pending_guess(view.chat_id)
            return await house_control_reply(view.text)
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
            # Echo the ask back: a dropped turn the user has to retype from
            # memory is the part that actually stings.
            return BotReply(
                voice.rate_limited(
                    wait_s=wait,
                    ask=query.display_label() if query.title else "",
                )
            )

        # New search/guess replaces any sticky yes/no offer.
        self._clear_pending_guess(view.chat_id)

        # Typed TMDB ids stay on the exact Overseerr detail path.
        if query.tmdb_id is not None:
            return await self._search_reply(view, query)

        context = self.memory.load(view.chat_id)
        # Jev-first media router (fail-open to local heuristics).
        intent = await classify_media_ask(
            query.raw_text or query.title or view.text,
            parsed=query,
            recent=self._recent_context(context),
        )
        try:
            return await self._route_media_intent(view, query, intent, context)
        except CatalogUnavailable as exc:
            return BotReply(exc.message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # A routed media turn is never silent. Retrying an unexpected lane
            # failure three times just delays the same outcome behind silence,
            # so answer honestly instead of letting the update dead-letter.
            log.exception("telegram media lane failed for intent %s", intent.kind)
            return BotReply(voice.lane_failed())

    async def _play_from_context(self, view: MessageView) -> BotReply:
        """Run the explicit Telegram Play follow-up without entering classify/search."""
        if not play_lane_enabled():
            return BotReply(
                "Play-from-Telegram is turned off "
                "(HEARTH_TELEGRAM_PLAY_LANE=false)."
            )
        context = self.memory.load(view.chat_id)
        if context is None or not context.hits:
            return BotReply(
                "I don't have a title in this thread to put on the TV. "
                "Send or pick one first."
            )
        if len(context.hits) > 1:
            names = ", ".join(hit.label for hit in context.hits[:4])
            return BotReply(f"Which one should I put on the TV? {names}.")
        hit = context.hits[0]
        if hit.media_status != 5:
            # Only Plex can play it and it is not there yet. Saying so beats
            # handing Infuse a title it will never find and reporting its error.
            return BotReply(voice.play_not_on_plex(_display_title(hit.title, hit.year)))
        outcome = await play_on_tv(
            title=hit.title,
            tmdb_id=hit.tmdb_id,
            media_type=hit.media_type,
            year=hit.year,
            season=hit.season,
        )
        return BotReply(outcome.message)

    @staticmethod
    def _recent_context(context: ChatContext | None) -> list[str] | None:
        """A few words of thread history so Jev can read follow-ups."""
        if context is None:
            return None
        recent = [context.ask_text] if context.ask_text else []
        recent.extend(hit.label for hit in context.hits[:3])
        return recent or None

    async def _route_media_intent(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
        context: ChatContext | None = None,
    ) -> BotReply:
        if intent.note == "list_ask":
            return BotReply(voice.list_ask())

        if intent.kind == "other":
            # not_media / chatter. Search anything the parser already salvaged,
            # otherwise say what I can do — a routed media turn is never silent.
            if query.title and looks_like_concrete_title(query.title):
                return await self._search_reply(view, query)
            return BotReply(voice.nudge())

        if intent.kind == "chat_about":
            return await self._chat_about_reply(view, query, intent)

        if intent.kind == "follow_up":
            return await self._follow_up_reply(view, query, intent, context)

        if intent.kind == "batch" and settings.telegram_batch_lane:
            return await self._batch_reply(view, intent)

        if intent.kind == "person" and settings.telegram_person_lane:
            return await self._person_reply(view, intent)

        if intent.kind == "similar" and settings.telegram_similar_lane:
            return await self._similar_reply(view, intent, context)

        if intent.kind in {"mood", "house_pick"} and settings.telegram_mood_lane:
            return await self._mood_reply(view, intent)

        if intent.kind == "describe":
            return await self._guess_reply(view, query, intent=intent)

        # Jev can flag needs_llm on a turn whose lane is still fully
        # deterministic ("all Harry Potters", "LOTR extended"). Spend the gpt
        # hop only when there is genuinely no seed to search with, otherwise a
        # low-confidence verdict throws away a seed we already hold.
        if intent.needs_llm and not (intent.search_title or query.title):
            return await self._guess_reply(view, query, intent=intent)

        if intent.kind == "series_all":
            return await self._series_all_reply(view, query, intent)

        if intent.kind == "edition":
            return await self._edition_reply(view, query, intent)

        if intent.kind == "known_franchise":
            return await self._franchise_reply(view, query, intent)

        # A disabled lane still has to answer something useful.
        if intent.kind in {"batch", "person", "similar", "mood", "house_pick"}:
            search_query = self._intent_search_query(query, intent)
            if search_query.title:
                return await self._search_reply(view, search_query)
            return BotReply(voice.nudge())

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

    # --- presentation ------------------------------------------------------

    def _present(
        self,
        chat_id: int,
        hits: list[MediaHit],
        *,
        header: str,
        ask_kind: str,
        ask_text: str = "",
        season: int | None = None,
        edition_key: str = "",
        edition_label: str = "",
        search_title: str = "",
        franchise_seed: str = "",
        person_name: str = "",
        person_role: str = "",
        media_type: str = "",
        anchor: MediaHit | None = None,
        remember_single_guess: bool = False,
        offer_similar: bool = False,
        offer_series: bool = False,
        offer_more: bool = False,
        offer_dismiss: bool = False,
        title_chip: str = "",
        page: int = 1,
        accumulate_shown: bool = True,
    ) -> BotReply:
        """Render cards, remember the thread context, and arm yes/nah."""
        cards = self._cards()
        top = hits[0] if hits else None
        rendered = cards.render(
            chat_id,
            hits,
            header=header,
            season=season,
            edition_key=edition_key,
            edition_label=edition_label,
            similar_anchor=top if (offer_similar and top is not None) else None,
            series_anchor=top if (offer_series and top is not None) else None,
            offer_more=offer_more,
            offer_dismiss=offer_dismiss,
            title_chip=title_chip,
        )
        self.memory.remember(
            chat_id,
            hits=hits,
            ask_kind=ask_kind,
            ask_text=ask_text,
            search_title=search_title,
            franchise_seed=franchise_seed,
            person_name=person_name,
            person_role=person_role,
            media_type=media_type,
            anchor_id=anchor.tmdb_id if anchor is not None else None,
            anchor_type=anchor.media_type if anchor is not None else "",
            season=season,
            page=page,
            accumulate_shown=accumulate_shown,
        )
        single = rendered.single_offer
        if remember_single_guess and single is not None:
            hit, hit_season = single
            self._set_pending_guess(chat_id, hit, season=hit_season)
        # Nothing to request and only one candidate: lead with the one clear
        # status line instead of a numbered list of one. Any Play / status /
        # refine button still belongs on it — "On Plex" is exactly when Play is
        # the useful action.
        if not rendered.requestable and len(hits) == 1:
            return BotReply(blocked_status_line(hits[0]), rendered.reply.reply_markup)
        return rendered.reply

    def _miss(self, label: str) -> BotReply:
        return BotReply(voice.no_match(label))

    # --- lanes -------------------------------------------------------------

    async def _guess_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        *,
        intent: MediaIntent | None = None,
    ) -> BotReply:
        """LLM catalog resolve for descriptive riddles — never auto-queue."""
        if not self.backend_configured:
            return BotReply(voice.backend_not_configured())
        if not settings.openai_configured:
            return BotReply(voice.needs_openai())

        guesses = await guess_catalog_titles(query.raw_text or query.title)
        if not guesses:
            return BotReply("I’m not sure which title you mean. Send the movie or series name.")

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
            hits = await self.catalog.hits(guessed)
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
        return self._present(
            view.chat_id,
            hits,
            header=header,
            ask_kind="describe",
            ask_text=query.raw_text or view.text,
            search_title=guessed.title,
            media_type=guessed.media_type or "",
            remember_single_guess=True,
            offer_similar=len(hits) == 1,
            offer_dismiss=len(hits) == 1,
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
                hits = await self.catalog.hits(probe)
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
            return BotReply(voice.backend_not_configured())

        seed = search_query.title
        hits = await self._franchise_hits(search_query, seed)
        if not hits:
            return self._miss(search_query.display_label())

        ordered = in_release_order(hits)
        kept = apply_exclusions(
            ordered,
            drop_last=intent.drop_last,
            drop_first=intent.drop_first,
        )
        if not kept:
            return BotReply(voice.exclusion_left_nothing(seed, found=len(ordered)))
        # Say what was skipped by position, not by title: naming a film that has
        # no button invites "did you queue it?".
        dropped = ""
        skipped = len(ordered) - len(kept)
        if skipped > 0:
            if intent.drop_last:
                dropped = "the last" if intent.drop_last == 1 else f"the last {intent.drop_last}"
            elif intent.drop_first:
                dropped = (
                    "the first" if intent.drop_first == 1 else f"the first {intent.drop_first}"
                )
        return self._present(
            view.chat_id,
            kept[:SERIES_MAX_RESULTS],
            header=voice.series_header(seed, dropped=dropped),
            ask_kind="series_all",
            ask_text=query.raw_text or view.text,
            search_title=seed,
            franchise_seed=seed,
            media_type=search_query.media_type or "",
            edition_key=intent.edition_key,
            edition_label=intent.edition_label,
            offer_similar=False,
        )

    async def _franchise_hits(self, search_query: MediaQuery, seed: str) -> list[MediaHit]:
        """Franchise entries, preferring the exact TMDB collection when there is one."""
        hits = await self.catalog.hits(
            search_query,
            franchise_seed=seed,
            limit=SERIES_MAX_RESULTS,
        )
        if not hits:
            return []
        anchor = in_release_order(hits)[0]
        if anchor.media_type != "movie":
            return hits
        try:
            _, parts = await self.catalog.collection_hits(
                anchor.media_type,
                anchor.tmdb_id,
                limit=SERIES_MAX_RESULTS + 4,
            )
        except CatalogUnavailable:
            return hits
        # A real collection beats fuzzy title matching, but only when it is at
        # least as complete as what search already found.
        return parts if len(parts) >= len(hits) else hits

    async def _franchise_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
    ) -> BotReply:
        """Known franchise seed (e.g. Harry Potter) → franchise-aware Get cards."""
        search_query = self._intent_search_query(query, intent)
        if not self.backend_configured:
            return BotReply(voice.backend_not_configured())

        seed = search_query.title
        hits = await self.catalog.hits(
            search_query,
            franchise_seed=seed,
            limit=SERIES_MAX_RESULTS,
        )
        if not hits:
            return self._miss(search_query.display_label())
        ordered = in_release_order(hits) if len(hits) > 1 else hits
        header = (
            voice.franchise_header(seed)
            if len(ordered) > 1
            else voice.exact_header(ordered[0].title, single=True)
        )
        return self._present(
            view.chat_id,
            ordered,
            header=header,
            ask_kind="known_franchise",
            ask_text=query.raw_text or view.text,
            search_title=seed,
            franchise_seed=seed,
            media_type=search_query.media_type or "",
            remember_single_guess=len(ordered) == 1,
            offer_similar=len(ordered) == 1,
            offer_dismiss=len(ordered) == 1,
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
            return BotReply(voice.backend_not_configured())

        seed = search_query.title
        multiword = len(seed.split()) >= 2
        hits = await self.catalog.hits(
            search_query,
            franchise_seed=seed if multiword else None,
            limit=SERIES_MAX_RESULTS if multiword else MAX_RESULTS,
        )
        if not hits:
            return self._miss(search_query.display_label())
        label = intent.edition_label or "preferred edition"
        return self._present(
            view.chat_id,
            in_release_order(hits) if len(hits) > 1 else hits,
            header=voice.edition_header(seed, label),
            ask_kind="edition",
            ask_text=query.raw_text or view.text,
            search_title=seed,
            franchise_seed=seed if multiword else "",
            media_type=search_query.media_type or "",
            edition_key=intent.edition_key,
            edition_label=intent.edition_label,
            remember_single_guess=len(hits) == 1,
            offer_dismiss=len(hits) == 1,
        )

    async def _person_reply(self, view: MessageView, intent: MediaIntent) -> BotReply:
        """Actor / director filmography — never a literal search of the sentence."""
        if not self.backend_configured:
            return BotReply(voice.backend_not_configured())
        name = intent.person_name.strip()
        if not name:
            return BotReply(voice.nudge())
        resolved, hits = await self.catalog.person_hits(
            name,
            role=intent.person_role or "cast",
            limit=SERIES_MAX_RESULTS,
        )
        if not resolved:
            return BotReply(voice.unknown_person(name))
        if not hits:
            return BotReply(
                f"I found {resolved}, but nothing of theirs is in the catalog right now."
            )
        return self._present(
            view.chat_id,
            hits,
            header=voice.person_header(resolved, role=intent.person_role or "cast"),
            ask_kind="person",
            ask_text=intent.raw_text or view.text,
            person_name=resolved,
            person_role=intent.person_role or "cast",
            offer_more=True,
        )

    async def _mood_reply(
        self,
        view: MessageView,
        intent: MediaIntent,
        *,
        page: int = 1,
        exclude_ids: frozenset[int] = frozenset(),
    ) -> BotReply:
        """Mood / vibe / house pick via real TMDB discover coordinates."""
        if not self.backend_configured:
            return BotReply(voice.backend_not_configured())
        spec = intent.mood or house_pick_spec(
            media_type=intent.media_type or "movie",
        )
        hits = await self.catalog.mood_hits(
            spec,
            limit=MAX_RESULTS,
            page=page,
            exclude_ids=exclude_ids,
        )
        if not hits:
            if exclude_ids:
                return BotReply(voice.no_more_options(spec.label))
            return BotReply(
                f"Nothing in the catalog fits {spec.label} right now. "
                "Give me a title or a different vibe?"
            )
        header = (
            voice.house_pick_header()
            if intent.kind == "house_pick"
            else voice.mood_header(spec.label)
        )
        return self._present(
            view.chat_id,
            hits,
            header=header,
            ask_kind=intent.kind,
            ask_text=intent.raw_text or view.text,
            media_type=spec.media_type,
            offer_more=True,
            # "Date Night" is both a vibe and a film. Answer as the vibe, but
            # leave the correction one tap away instead of guessing silently.
            title_chip=spec.ambiguous_title,
            page=page,
        )

    async def _similar_reply(
        self,
        view: MessageView,
        intent: MediaIntent,
        context: ChatContext | None,
    ) -> BotReply:
        """"Something like X" — resolve the anchor once, then ask TMDB for neighbours."""
        if not self.backend_configured:
            return BotReply(voice.backend_not_configured())

        anchor_type = ""
        anchor_id: int | None = None
        anchor_label = intent.search_title.strip()
        if anchor_label:
            probe = MediaQuery(
                action="search",
                title=anchor_label,
                year=intent.year,
                media_type=intent.media_type if intent.media_type in {"movie", "tv"} else None,
                reason="similar",
                raw_text=intent.raw_text,
            )
            found = await self.catalog.hits(probe, limit=1)
            if not found:
                return self._miss(anchor_label)
            anchor_type = found[0].media_type
            anchor_id = found[0].tmdb_id
            anchor_label = _display_title(found[0].title, found[0].year)
        elif context is not None and context.top is not None:
            top = context.top
            anchor_type = top.media_type
            anchor_id = top.tmdb_id
            anchor_label = top.label
        else:
            return BotReply(voice.need_a_subject())

        return await self._neighbour_reply(
            view.chat_id,
            anchor_type,
            int(anchor_id),
            anchor_label,
            ask_text=intent.raw_text or view.text,
            exclude_ids=frozenset(),
        )

    async def _neighbour_reply(
        self,
        chat_id: int,
        anchor_type: str,
        anchor_id: int,
        anchor_label: str,
        *,
        ask_text: str,
        exclude_ids: frozenset[int] = frozenset(),
        edit_message_id: int | None = None,
    ) -> BotReply:
        hits = await self.catalog.neighbour_hits(
            anchor_type,
            anchor_id,
            limit=MAX_RESULTS,
            exclude_ids=exclude_ids,
        )
        if not hits:
            return BotReply(
                voice.no_more_options(anchor_label),
                edit_message_id=edit_message_id,
            )
        anchor = MediaHit(media_type=anchor_type, tmdb_id=anchor_id, title=anchor_label)
        reply = self._present(
            chat_id,
            hits,
            header=voice.similar_header(anchor_label),
            ask_kind="similar",
            ask_text=ask_text,
            search_title=anchor_label,
            media_type=anchor_type,
            anchor=anchor,
            offer_more=True,
        )
        if edit_message_id is None:
            return reply
        return BotReply(reply.text, reply.reply_markup, edit_message_id=edit_message_id)

    async def _batch_reply(self, view: MessageView, intent: MediaIntent) -> BotReply:
        """A compound ask becomes one plan with per-item progress."""
        if not self.backend_configured:
            return BotReply(voice.backend_not_configured())
        parts = intent.parts[: max(2, int(settings.telegram_batch_max_items))]
        if not parts:
            return BotReply(voice.nudge())

        groups: list[tuple[str, list[MediaHit]]] = []
        misses: list[str] = []
        shown: list[MediaHit] = []
        for part in parts:
            hits = await self._part_hits(part)
            if not hits:
                misses.append(part.label())
                continue
            groups.append((part.label(), hits))
            shown.extend(hits)

        if not groups:
            return BotReply(
                voice.no_match(", ".join(part.label() for part in parts)),
            )

        rendered = self._cards().render_plan(
            view.chat_id,
            groups,
            header=voice.batch_header([part.label() for part in parts]),
            misses=misses,
        )
        self.memory.remember(
            view.chat_id,
            hits=shown,
            ask_kind="batch",
            ask_text=intent.raw_text or view.text,
            search_title=parts[0].title,
        )
        return rendered.reply

    async def _part_hits(self, part: AskPart) -> list[MediaHit]:
        """Resolve one plan item with the same intelligence as a solo ask."""
        query = MediaQuery(
            action="search",
            media_type=part.media_type if part.media_type in {"movie", "tv"} else None,
            title=part.title,
            year=part.year,
            reason="batch",
            raw_text=part.raw,
        )
        known_seed = is_known_franchise(part.title)
        try:
            if part.series_all:
                hits = await self._franchise_hits(query, part.title)
                return apply_exclusions(
                    in_release_order(hits),
                    drop_last=part.drop_last,
                    drop_first=part.drop_first,
                )[:SERIES_MAX_RESULTS]
            hits = await self.catalog.hits(
                query,
                franchise_seed=part.title if known_seed else None,
                limit=3,
            )
        except CatalogUnavailable:
            # One unavailable item must not sink the whole plan.
            return []
        # Silently swapping in a loosely related film would be worse than
        # reporting the item as a miss.
        kept = [hit for hit in hits if plausible_match(part.title, hit)]
        if not kept and known_seed:
            # An alias like "LOTR" can never fuzzy-match "The Lord of the Rings:
            # The Fellowship of the Ring", so the guard that protects unknown
            # titles would turn a franchise everyone knows into a plan miss.
            kept = hits
        return kept[:1]

    # --- follow-ups --------------------------------------------------------

    async def _follow_up_reply(
        self,
        view: MessageView,
        query: MediaQuery,
        intent: MediaIntent,
        context: ChatContext | None,
    ) -> BotReply:
        """Resolve "the sequel" / "all of them" / "more" against recent context."""
        if context is None or not context.present:
            # Relational follow-ups ("the sequel", "all of them") are not titles.
            # In a group they must not fall through into a search that looks like
            # someone else's thread, and they must not borrow that thread either.
            relational = intent.follow_up in {
                "sequel",
                "prequel",
                "all_of_them",
                "more_like_that",
                "that_one",
                "other_one",
                "ordinal",
                "continue_pack",
            }
            if int(view.chat_id) < 0 and relational:
                return BotReply(
                    "That follow-up isn’t on your thread. I keep each person in "
                    "this chat separate — send the title, or ask what’s on tonight."
                )
            # A real title that merely looks like a follow-up ("Next", "More").
            if query.title and looks_like_concrete_title(query.title):
                return await self._search_reply(view, query)
            return BotReply(voice.lost_context())

        kind = intent.follow_up
        if kind == "ordinal" and intent.ordinal is not None:
            return await self._confirm_context_pick(view, context, index=intent.ordinal)
        if kind == "that_one":
            return await self._confirm_context_pick(view, context, index=1)
        if kind == "other_one":
            return await self._offer_alternative(view, context=context)
        if kind == "all_of_them":
            return await self._expand_series_from_context(view, context)
        if kind == "more_like_that":
            top = context.top
            if top is None:
                return BotReply(voice.lost_context())
            return await self._neighbour_reply(
                view.chat_id,
                top.media_type,
                top.tmdb_id,
                top.label,
                ask_text=context.ask_text or view.text,
            )
        if kind == "continue_pack":
            offered = await self._offer_watch_next(view, context)
            if offered is not None:
                return offered
            # Fall through to sequel when no stored watch-next exists.
            return await self._adjacent_entry_reply(view, context, direction="sequel")
        if kind in {"sequel", "prequel"}:
            if kind == "sequel":
                offered = await self._offer_watch_next(view, context)
                if offered is not None:
                    return offered
            return await self._adjacent_entry_reply(view, context, direction=kind)
        if kind == "more":
            # "next" lands here too, and a watch-next only exists in the moments
            # after a queue — when the nudge has just named the next film in the
            # pack. Honour that before paging the old search again.
            offered = await self._offer_watch_next(view, context)
            if offered is not None:
                return offered
            return await self._more_of_the_same(view, context)
        return BotReply(voice.lost_context())

    async def _confirm_context_pick(
        self,
        view: MessageView,
        context: ChatContext,
        *,
        index: int,
    ) -> BotReply:
        """Arm yes/Get for the nth card that was on screen."""
        picked = context.nth(index)
        if picked is None:
            return BotReply(voice.lost_context())
        hit = MediaHit(
            media_type=picked.media_type,  # type: ignore[arg-type]
            tmdb_id=picked.tmdb_id,
            title=picked.title,
            year=picked.year,
            media_status=picked.media_status,
        )
        if hit.media_status == 5 or hit.media_status in {2, 3}:
            return BotReply(blocked_status_line(hit))

        rendered = self._cards().render(
            view.chat_id,
            [hit],
            header=format_guess_confirm(hit.title, hit.year),
            season=picked.season,
            similar_anchor=hit,
            offer_dismiss=True,
        )
        single = rendered.single_offer
        if single is not None:
            self._set_pending_guess(view.chat_id, single[0], season=single[1])
        # The rest of the list stays addressable, so "the third one" still works
        # after the user has narrowed down to one card.
        self.memory.remember(
            view.chat_id,
            hits=[
                MediaHit(
                    media_type=remembered.media_type,  # type: ignore[arg-type]
                    tmdb_id=remembered.tmdb_id,
                    title=remembered.title,
                    year=remembered.year,
                    media_status=remembered.media_status,
                )
                for remembered in context.hits
            ],
            ask_kind=context.ask_kind or "exact_title",
            ask_text=context.ask_text,
            search_title=context.search_title or hit.title,
            franchise_seed=context.franchise_seed,
            person_name=context.person_name,
            person_role=context.person_role,
            media_type=context.media_type,
            anchor_id=context.anchor_id,
            anchor_type=context.anchor_type,
            page=context.page,
            accumulate_shown=False,
        )
        return rendered.reply

    async def _offer_alternative(
        self,
        view: MessageView,
        *,
        context: ChatContext | None = None,
    ) -> BotReply:
        """"Nah, the other one" — offer the runner-up instead of going quiet."""
        self._clear_pending_guess(view.chat_id)
        ctx = context if context is not None else self.memory.load(view.chat_id)
        if ctx is None or len(ctx.hits) < 2:
            return BotReply(voice.cancelled())
        return await self._confirm_context_pick(view, ctx, index=2)

    async def _expand_series_from_context(
        self,
        view: MessageView,
        context: ChatContext,
    ) -> BotReply:
        """"All of them" → the franchise behind whatever was last on screen."""
        seed = context.franchise_seed or best_franchise_seed(context.subject())
        if not seed:
            return BotReply(voice.lost_context())
        intent = MediaIntent(
            kind="series_all",
            search_title=seed,
            media_type=context.media_type,
            raw_text=context.ask_text or view.text,
            note="follow_up:all_of_them",
        )
        query = MediaQuery(action="search", title=seed, reason="follow_up")
        return await self._series_all_reply(view, query, intent)

    async def _adjacent_entry_reply(
        self,
        view: MessageView,
        context: ChatContext,
        *,
        direction: str,
    ) -> BotReply:
        """"The sequel" / "the prequel" resolved inside the franchise, by year."""
        top = context.top
        if top is None:
            return BotReply(voice.lost_context())
        seed = context.franchise_seed or best_franchise_seed(top.title)
        # The TMDB collection is authoritative about what "the sequel" is; a
        # seeded title search is only the fallback.
        _, entries = await self.catalog.collection_hits(
            top.media_type,
            top.tmdb_id,
            limit=SERIES_MAX_RESULTS + 4,
        )
        if not entries:
            query = MediaQuery(
                action="search",
                title=seed,
                media_type=top.media_type if top.media_type in {"movie", "tv"} else None,
                reason="follow_up",
            )
            entries = await self.catalog.hits(
                query,
                franchise_seed=seed,
                limit=SERIES_MAX_RESULTS,
            )
        hits = in_release_order(entries)
        if not hits:
            return self._miss(seed)

        anchor_year = top.year
        if anchor_year is None:
            anchor = next((hit for hit in hits if hit.tmdb_id == top.tmdb_id), None)
            anchor_year = anchor.year if anchor is not None else None
        candidates = [hit for hit in hits if hit.tmdb_id != top.tmdb_id]
        if anchor_year is not None:
            if direction == "sequel":
                candidates = [
                    hit for hit in candidates if hit.year is not None and hit.year > anchor_year
                ]
            else:
                candidates = [
                    hit for hit in candidates if hit.year is not None and hit.year < anchor_year
                ][::-1]
        if not candidates:
            word = "sequel" if direction == "sequel" else "prequel"
            return BotReply(f"{top.label} has no {word} in the catalog — that's the end of it.")
        return self._present(
            view.chat_id,
            candidates[:1],
            header=voice.follow_up_header(top.label, what=direction),
            ask_kind="exact_title",
            ask_text=context.ask_text,
            search_title=candidates[0].title,
            franchise_seed=seed,
            remember_single_guess=True,
            offer_series=True,
            offer_dismiss=True,
        )

    async def _more_of_the_same(
        self,
        view: MessageView,
        context: ChatContext,
        *,
        edit_message_id: int | None = None,
    ) -> BotReply:
        """"More" / "More options" — same lane, fresh titles."""
        exclude = frozenset(context.shown_ids)
        if context.ask_kind in {"mood", "house_pick"}:
            spec = detect_mood(context.ask_text) or house_pick_spec(
                media_type=context.media_type or "movie"
            )
            intent = MediaIntent(
                kind=context.ask_kind,  # type: ignore[arg-type]
                mood=spec,
                media_type=spec.media_type,
                raw_text=context.ask_text,
            )
            reply = await self._mood_reply(
                view,
                intent,
                page=context.page + 1,
                exclude_ids=exclude,
            )
        elif context.ask_kind == "person" and context.person_name:
            resolved, hits = await self.catalog.person_hits(
                context.person_name,
                role=context.person_role or "cast",
                limit=SERIES_MAX_RESULTS,
                exclude_ids=exclude,
            )
            if not hits:
                reply = BotReply(voice.no_more_options(resolved or context.person_name))
            else:
                reply = self._present(
                    view.chat_id,
                    hits,
                    header=voice.person_header(
                        resolved or context.person_name,
                        role=context.person_role or "cast",
                    ),
                    ask_kind="person",
                    ask_text=context.ask_text,
                    person_name=resolved or context.person_name,
                    person_role=context.person_role or "cast",
                    offer_more=True,
                )
        elif context.ask_kind == "similar" and context.anchor_id:
            reply = await self._neighbour_reply(
                view.chat_id,
                context.anchor_type or "movie",
                int(context.anchor_id),
                context.search_title or context.subject(),
                ask_text=context.ask_text,
                exclude_ids=exclude,
            )
        else:
            subject = context.subject()
            if not subject:
                return BotReply(voice.lost_context())
            query = MediaQuery(action="search", title=subject, reason="follow_up")
            hits = without_ids(
                await self.catalog.hits(
                    query,
                    franchise_seed=context.franchise_seed or None,
                    limit=SERIES_MAX_RESULTS,
                ),
                set(exclude),
            )
            if not hits:
                reply = BotReply(voice.no_more_options(subject))
            else:
                reply = self._present(
                    view.chat_id,
                    hits,
                    header=voice.exact_header(subject, single=False),
                    ask_kind=context.ask_kind or "exact_title",
                    ask_text=context.ask_text,
                    search_title=subject,
                    franchise_seed=context.franchise_seed,
                    offer_more=True,
                )
        if edit_message_id is None:
            return reply
        return BotReply(reply.text, reply.reply_markup, edit_message_id=edit_message_id)

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

        # The typed yes is the confirm, so only Jev's hard stops apply here.
        decision = await self._authorize_queue(
            said=view.text,
            tmdb_id=tmdb_id,
            media_type=media_type,
        )
        if decision is not None and decision.denied:
            self._clear_pending_guess(view.chat_id)
            return BotReply(decision.message)

        try:
            result = await self.overseerr.request(
                query=title,
                media_id=tmdb_id,
                media_type=media_type,
                seasons=seasons,
            )
        except OverseerrError:
            return BotReply(self._uncertain_request_text(label, answered=False))
        except Exception:  # noqa: BLE001
            log.exception("telegram pending-guess request failed")
            return BotReply(self._uncertain_request_text(label, answered=True))

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
        text = await self._with_queue_asides(
            text,
            view.chat_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
        )
        return BotReply(text)

    async def _search_reply(self, view: MessageView, query: MediaQuery) -> BotReply:
        """Exact-title lane: one Overseerr search, ranked, no LLM."""
        if not self.backend_configured:
            return BotReply(voice.backend_not_configured())
        hits = await self.catalog.hits(query)
        if not hits:
            broadened = self._broaden(query)
            if broadened is not None:
                hits = await self.catalog.hits(broadened)
            if not hits:
                return self._miss(query.display_label())
            query = broadened or query
        single = len(hits) == 1
        return self._present(
            view.chat_id,
            hits,
            header=voice.exact_header(
                _display_title(hits[0].title, hits[0].year) if single else query.display_label(),
                single=single,
            ),
            ask_kind="exact_title",
            ask_text=query.raw_text or view.text,
            search_title=query.title,
            media_type=query.media_type or "",
            season=query.season,
            remember_single_guess=single,
            offer_similar=single,
            offer_series=single and hits[0].media_type == "movie",
            offer_dismiss=single,
        )

    @staticmethod
    def _broaden(query: MediaQuery) -> MediaQuery | None:
        """One honest retry before calling it a miss (subtitle / year / article).

        A human would not give up on "Dune: Part Two (2024)" just because the
        catalog spells it differently, so neither should the bot.
        """
        title = (query.title or "").strip()
        if query.tmdb_id is not None or len(title) < 3:
            return None
        candidate = title
        for separator in (":", " - ", " – ", " — "):
            if separator in candidate:
                head = candidate.split(separator, 1)[0].strip()
                if len(head) >= 3:
                    candidate = head
                    break
        else:
            if query.year is None:
                lowered = candidate.casefold()
                for article in ("the ", "a ", "an ", "de ", "het "):
                    if lowered.startswith(article):
                        candidate = candidate[len(article) :].strip()
                        break
                else:
                    return None
        if not candidate or candidate.casefold() == title.casefold():
            return None
        return MediaQuery(
            action="search",
            media_type=query.media_type,
            title=candidate,
            year=None,
            season=query.season,
            reason="broadened",
            raw_text=query.raw_text,
        )

    async def _search_rows(self, query: MediaQuery) -> list[dict[str, Any]]:
        """Compatibility shim over :class:`CatalogSearch`."""
        return await self.catalog.rows(query)

    @staticmethod
    def _rank_hits(
        rows: list[dict[str, Any]],
        query: MediaQuery,
        *,
        franchise_seed: str | None = None,
        limit: int = MAX_RESULTS,
    ) -> list[MediaHit]:
        return rank_hits(rows, query, franchise_seed=franchise_seed, limit=limit)

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
        """Render result cards for an already-ranked hit list."""
        rendered = self._cards().render(
            chat_id,
            hits,
            header=header or voice.exact_header(query.display_label(), single=False),
            season=query.season,
            edition_key=edition_key,
            edition_label=edition_label,
        )
        single = rendered.single_offer
        if remember_single_guess and single is not None:
            hit, season = single
            self._set_pending_guess(chat_id, hit, season=season)
        return rendered.reply

    async def _handle_action_callback(
        self,
        chat_id: int,
        message_id: int,
        data: str,
        *,
        user_id: int | None,
    ) -> BotReply:
        """Refine buttons: change the conversation, never queue anything."""
        try:
            action = self._callback_codec().decode_action(data, chat_id)
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

        if action.action == ACTION_DISMISS:
            self.memory.forget(chat_id)
            return BotReply(voice.cancelled(), edit_message_id=message_id)

        view = MessageView(chat_id=chat_id, message_id=message_id, user_id=user_id, text="")
        context = self.memory.load(chat_id)
        # Card rendering stored the title/year behind this exact button, which
        # outlives the chat context. Without it a Play tap on an older card
        # reaches Infuse as "TMDB 603" and cannot possibly succeed.
        stored = self.store.get_callback_media(data) or {}
        try:
            if action.action == ACTION_TITLE:
                return await self._title_correction_reply(view, stored)
            if action.action == ACTION_SIMILAR and action.tmdb_id:
                anchor_label = ""
                if context is not None:
                    match = next(
                        (hit for hit in context.hits if hit.tmdb_id == action.tmdb_id),
                        None,
                    )
                    anchor_label = match.label if match is not None else ""
                return await self._neighbour_reply(
                    chat_id,
                    action.media_type or "movie",
                    int(action.tmdb_id),
                    anchor_label or "that one",
                    ask_text=context.ask_text if context is not None else "",
                    edit_message_id=message_id,
                )
            if action.action == ACTION_SERIES and action.tmdb_id:
                return await self._expand_series_from_button(
                    view,
                    action.media_type or "movie",
                    int(action.tmdb_id),
                    context,
                )
            if action.action == ACTION_MORE:
                if context is None or not context.present:
                    return BotReply(voice.lost_context(), edit_message_id=message_id)
                return await self._more_of_the_same(
                    view,
                    context,
                    edit_message_id=message_id,
                )
            if action.action == ACTION_PLAY and action.tmdb_id:
                return await self._play_callback(
                    chat_id,
                    message_id,
                    media_type=action.media_type or "movie",
                    tmdb_id=int(action.tmdb_id),
                    context=context,
                    stored=stored,
                )
            if action.action == ACTION_STATUS and action.tmdb_id:
                return self._status_ack_callback(
                    message_id,
                    media_type=action.media_type or "movie",
                    tmdb_id=int(action.tmdb_id),
                    context=context,
                    stored=stored,
                )
        except CatalogUnavailable as exc:
            return BotReply(str(getattr(exc, "message", exc)), edit_message_id=message_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # A tap is unambiguously addressed to me. Retrying an unexpected
            # refine failure three times only delays the same outcome behind a
            # card that never changes.
            log.exception("telegram refine action %s failed", action.action)
            return BotReply(voice.lane_failed(), edit_message_id=message_id)
        return BotReply(voice.lost_context(), edit_message_id=message_id)

    async def _title_correction_reply(
        self,
        view: MessageView,
        stored: Mapping[str, Any],
    ) -> BotReply:
        """"I meant the title" — re-run the ambiguous vibe ask as an exact search."""
        title = str(stored.get("title") or "").strip()
        if not title:
            return BotReply(voice.lost_context(), edit_message_id=view.message_id)
        query = MediaQuery(
            action="search",
            title=title,
            reason="title_correction",
            raw_text=title,
        )
        reply = await self._search_reply(view, query)
        return BotReply(reply.text, reply.reply_markup, edit_message_id=view.message_id)

    async def _offer_watch_next(
        self,
        view: MessageView,
        context: ChatContext | None,
    ) -> BotReply | None:
        """Surface a stored watch-next card when the lane is enabled."""
        if not bool(getattr(settings, "telegram_watch_next", True)):
            return None
        if context is None:
            return None
        payload = context.watch_next()
        if not payload:
            return None
        watch = WatchNext.from_dict(payload)
        if watch is None or not watch.present:
            return None
        hits: list[MediaHit] = []
        try:
            details = await self.catalog.details(watch.tmdb_id, watch.media_type)
            media = details.get("media") if isinstance(details, dict) else None
            if isinstance(media, dict):
                hits = [
                    MediaHit(
                        media_type=watch.media_type,  # type: ignore[arg-type]
                        tmdb_id=watch.tmdb_id,
                        title=str(media.get("title") or watch.title),
                        year=watch.year,
                        media_status=(
                            int(media["mediaStatus"])
                            if media.get("mediaStatus") is not None
                            else None
                        ),
                    )
                ]
        except Exception:
            hits = []
        if not hits:
            hits = [watch.as_hit()]
        header = voice.watch_next_offer(
            watch.label,
            after=watch.from_title or "the last one",
        )
        self.memory.clear_watch_next(view.chat_id)
        return self._present(
            view.chat_id,
            hits,
            header=header,
            ask_kind="follow_up",
            ask_text=view.text or "what's next",
            media_type=watch.media_type,
            remember_single_guess=True,
            offer_dismiss=True,
        )

    @staticmethod
    def _card_subject(
        tmdb_id: int,
        *,
        context: ChatContext | None,
        stored: Mapping[str, Any] | None,
    ) -> tuple[str, int | None, int | None]:
        """Title, year and last-known status for the card a button belongs to.

        The chat context is freshest, but it expires long before the signed
        button does; the per-button payload is the durable fallback.
        """
        if context is not None:
            match = next((hit for hit in context.hits if hit.tmdb_id == tmdb_id), None)
            if match is not None:
                return match.title, match.year, match.media_status
        if stored and _integer(stored.get("tmdb_id")) == tmdb_id:
            title = str(stored.get("title") or "").strip()
            if title:
                return title, _integer(stored.get("year")), None
        return "", None, None

    def _status_ack_callback(
        self,
        message_id: int,
        *,
        media_type: str,
        tmdb_id: int,
        context: ChatContext | None,
        stored: Mapping[str, Any] | None = None,
    ) -> BotReply:
        del media_type  # the label carries the kind already
        title, year, status = self._card_subject(tmdb_id, context=context, stored=stored)
        label = _display_title(title, year) if title else "that title"
        state = "downloading" if status == 3 else "pending" if status == 2 else "in flight"
        return BotReply(voice.status_ack(label, state=state), edit_message_id=message_id)

    async def _play_callback(
        self,
        chat_id: int,
        message_id: int,
        *,
        media_type: str,
        tmdb_id: int,
        context: ChatContext | None,
        stored: Mapping[str, Any] | None = None,
    ) -> BotReply:
        del chat_id  # playback targets the house TV, not the chat
        title, year, _status = self._card_subject(tmdb_id, context=context, stored=stored)
        if not title:
            # Honest fail: guessing a title here would send Infuse chasing
            # "TMDB 603" and report a failure the user cannot act on.
            return BotReply(
                voice.play_needs_title(),
                edit_message_id=message_id,
            )
        try:
            decision = await authorize_tool(
                "plex_play",
                {"query": title, "media_type": media_type},
                said=(context.ask_text if context is not None else "") or f"play {title}",
                channel="telegram_play",
                explicit_confirm=True,
            )
        except Exception:  # noqa: BLE001 — never block a tapped Play on the gate
            log.warning("jev play gate failed open", exc_info=True)
            decision = None
        if decision is not None and decision.denied:
            return BotReply(decision.message, edit_message_id=message_id)
        outcome = await play_on_tv(
            title=title,
            tmdb_id=tmdb_id,
            media_type=media_type,
            year=year,
        )
        return BotReply(outcome.message, edit_message_id=message_id)

    async def _house_aside(self, view: MessageView) -> BotReply | None:
        """Shelf and scene asks. Jev chooses the tool; the phrase only fail-opens.

        Bare “movie night” stays a catalog vibe. House sleep / filmavond / good
        night stay on the ritual commands.
        """
        phrase = classify_house_phrase(view.text)
        if phrase is None or looks_like_house_control(view.text):
            return None
        if _catalog_movie_night(view.text):
            return None
        verdict = await evaluate_message(view.text)
        decision = decide_butler_tool(view.text, verdict)
        log_shadow_outcome(
            verdict,
            channel="telegram_butler",
            tools=[decision.tool] if decision.run else [],
            outcome=decision.source,
        )
        if decision.blocked_by_jev:
            if decision.source == "jev_cancel":
                return BotReply("Okay — I won't run that.")
            return BotReply(
                "That doesn't sound like the shelf or a house scene, so I left it alone."
            )
        if not decision.run:
            return None
        if decision.tool == "house_shelf":
            try:
                snap = await shelf_snapshot()
            except Exception:  # noqa: BLE001
                log.exception("telegram shelf snapshot failed")
                return BotReply(
                    "Plex didn’t answer. Say “what’s on tonight” again in a moment — "
                    "I won’t guess a title."
                )
            return BotReply(str(snap.get("speak") or "The shelf is quiet."))
        preset = decision.as_args().get("preset") or phrase.preset
        try:
            result = await activate_preset(preset)
        except Exception:  # noqa: BLE001
            log.exception("telegram scene preset failed")
            return BotReply(
                "Home Assistant didn’t run that scene. I left the lights alone — "
                "say it again in a moment."
            )
        return BotReply(str(result.get("speak") or "I couldn’t run that scene."))

    async def _with_queue_asides(
        self,
        text: str,
        chat_id: int,
        *,
        media_type: str,
        tmdb_id: int,
        title: str,
        year: int | None,
    ) -> str:
        nudge = await self._remember_watch_next_after_queue(
            chat_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
        )
        try:
            shelf_line = await queue_shelf_aside(title)
        except Exception:  # noqa: BLE001
            log.exception("telegram queue shelf aside failed")
            shelf_line = None
        extra = "\n".join(part for part in (nudge, shelf_line) if part)
        if not extra:
            return text
        return f"{text}\n{extra}"

    async def _remember_watch_next_after_queue(
        self,
        chat_id: int,
        *,
        media_type: str,
        tmdb_id: int,
        title: str,
        year: int | None,
    ) -> str | None:
        """After a successful Get, remember the next franchise entry when possible."""
        if not bool(getattr(settings, "telegram_watch_next", True)):
            return None
        try:
            _name, entries = await self.catalog.collection_hits(
                media_type,
                tmdb_id,
                limit=SERIES_MAX_RESULTS + 4,
            )
        except Exception:
            return None
        if not entries:
            return None
        nxt = pick_next_in_order(
            entries,
            after_tmdb_id=tmdb_id,
            after_year=year,
        )
        if nxt is None or nxt.tmdb_id == tmdb_id:
            return None
        self.memory.remember_watch_next(
            chat_id,
            media_type=nxt.media_type,
            tmdb_id=nxt.tmdb_id,
            title=nxt.title,
            year=nxt.year,
            from_title=title,
            from_tmdb_id=tmdb_id,
        )
        return voice.watch_next_nudge(
            f"{nxt.title} ({nxt.year})" if nxt.year else nxt.title,
            after=title,
        )

    async def _expand_series_from_button(
        self,
        view: MessageView,
        media_type: str,
        tmdb_id: int,
        context: ChatContext | None,
    ) -> BotReply:
        """"All of them" tapped on a card — expand that title's franchise."""
        label = ""
        if context is not None:
            match = next((hit for hit in context.hits if hit.tmdb_id == tmdb_id), None)
            label = match.title if match is not None else ""
        seed = best_franchise_seed(label) if label else ""
        if not seed:
            details = await self.catalog.details(tmdb_id, media_type)
            media = details.get("media") if isinstance(details.get("media"), dict) else {}
            seed = best_franchise_seed(str(media.get("title") or ""))
        if not seed:
            return BotReply(voice.lost_context(), edit_message_id=view.message_id)
        intent = MediaIntent(
            kind="series_all",
            search_title=seed,
            media_type=media_type if media_type in {"movie", "tv"} else "",
            raw_text=context.ask_text if context is not None else "",
            note="button:all_of_them",
        )
        query = MediaQuery(action="search", title=seed, reason="follow_up")
        reply = await self._series_all_reply(view, query, intent)
        return BotReply(reply.text, reply.reply_markup, edit_message_id=view.message_id)

    @_with_speaker
    async def handle_callback(self, callback: dict[str, Any]) -> BotReply | None:
        # One Jev scope per tap so the gate's decisions land in one log line.
        with tool_turn("", channel="telegram_callback"):
            return await self._handle_callback(callback)

    async def _handle_callback(self, callback: dict[str, Any]) -> BotReply | None:
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
        if is_action_callback(data):
            return await self._handle_action_callback(
                chat_id,
                message_id,
                data,
                user_id=user_id,
            )
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

        # Gate before claiming. A tap is a confirm, so only Jev's hard stops can
        # block it — and leaving the action unclaimed means a refused button is
        # still there rather than permanently spent.
        decision = await self._authorize_queue(
            said=str(metadata.get("title") or f"TMDB {request.tmdb_id}"),
            tmdb_id=request.tmdb_id,
            media_type=request.media_type,
        )
        if decision is not None and decision.denied:
            return BotReply(decision.message, edit_message_id=message_id)

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
                self._uncertain_request_text(label, answered=False),
                edit_message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("telegram Overseerr request failed")
            self.store.finish_callback(digest, state="uncertain", error=type(exc).__name__)
            return BotReply(
                self._uncertain_request_text(label, answered=True),
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
        text = await self._with_queue_asides(
            text,
            chat_id,
            media_type=request.media_type,
            tmdb_id=request.tmdb_id,
            title=title,
            year=year,
        )
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

    async def _authorize_queue(
        self,
        *,
        said: str,
        tmdb_id: int,
        media_type: str,
    ) -> ToolDecision | None:
        """Jev gate for the one Telegram action that spends the house's bandwidth.

        Get taps and typed yeses are already explicit confirms, so only Jev's
        hard stops (refuse / do-not-auto-run) can block them. ``None`` means the
        gate had no opinion and the request proceeds.

        For a tap, ``said`` is the title rather than the original sentence: the
        only question left is whether *this title* is something the house should
        decline to fetch, and the sentence that found it was answered already.
        """
        try:
            return await authorize_tool(
                "overseerr_request",
                {"media_id": tmdb_id, "media_type": media_type},
                said=said or None,
                channel="telegram_queue",
                explicit_confirm=True,
            )
        except Exception:  # noqa: BLE001 — the gate must never block a confirmed Get
            log.warning("jev queue gate failed open", exc_info=True)
            return None

    @staticmethod
    def _uncertain_request_text(label: str, *, answered: bool) -> str:
        """One sentence for a request whose provider outcome Hearth cannot confirm."""
        because = "" if answered else " because Overseerr did not answer"
        return (
            f"The request outcome for {label} is uncertain{because}. "
            "Check Overseerr before trying again."
        )

    @staticmethod
    def _request_error_text(label: str, result: Mapping[str, Any]) -> str:
        """Phrase an Overseerr rejection using Telegram's richer label.

        The taxonomy lives once, in ``hearth.tools.arr``; only the subject
        differs here because Telegram knows the year and season.
        """
        return overseerr_request_error_text(
            label,
            reason=str(result.get("reason") or ""),
            already=bool(result.get("already")),
        )

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
