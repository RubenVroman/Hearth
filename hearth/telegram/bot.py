"""Deterministic Overseerr-first Telegram media bot.

Messages only search. A signed inline button is the explicit mutation boundary,
and every request uses the exact TMDB id and media type shown to the user.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from typing import Any

from rapidfuzz import fuzz

from hearth.config import settings
from hearth.telegram.callbacks import CallbackCodec, CallbackError
from hearth.telegram.models import BotReply, MediaHit, MediaQuery, MessageView
from hearth.telegram.parse import parse_message
from hearth.telegram.progress import (
    ProgressTracker,
    format_reject_download,
    matching_request_row,
)
from hearth.telegram.safeguards import RateLimiter, authorized
from hearth.telegram.store import TelegramStore
from hearth.tools.arr import OverseerrError, overseerr

log = logging.getLogger("hearth.telegram")

MAX_RESULTS = 5
HELP_TEXT = (
    "Send a movie or series title and I’ll search Overseerr. Add a year or season "
    "to narrow it, for example Dune (2021) or Severance S02. Tap Get on the exact "
    "match to request it. Commands: /search <title>, /status, /help."
)

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

    async def handle_message(self, message: dict[str, Any]) -> BotReply | None:
        view, query = parse_message(
            message,
            max_length=max(20, int(settings.telegram_max_title_length)),
            bot_user_id=self.bot_user_id,
        )
        if view is None or not self._authorized(view.chat_id, view.user_id):
            return None
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
        return await self._search_reply(view, query)

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
    def _rank_hits(rows: list[dict[str, Any]], query: MediaQuery) -> list[MediaHit]:
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

        def score(hit: MediaHit) -> float:
            title = _normalized(hit.title)
            original = _normalized(hit.original_title)
            candidates = [candidate for candidate in (title, original) if candidate]
            relevance = (
                max(float(fuzz.WRatio(asked, candidate)) for candidate in candidates)
                if asked and candidates
                else 100.0
            )
            if asked and asked in candidates:
                relevance += 1000
            elif asked and any(candidate.startswith(asked) for candidate in candidates):
                relevance += 300
            if query.year is not None and hit.year == query.year:
                relevance += 500
            return relevance

        hits.sort(key=score, reverse=True)
        return hits[:MAX_RESULTS]

    def _results_reply(
        self,
        chat_id: int,
        query: MediaQuery,
        hits: list[MediaHit],
    ) -> BotReply:
        lines = [f"Overseerr results for “{query.display_label()}”:"]
        buttons: list[list[dict[str, str]]] = []
        codec = self._callback_codec()
        ttl = max(60, int(settings.telegram_callback_ttl_seconds))
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
            self.store.put_callback_media(
                callback_data,
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
            season_bit = f" S{season:02d}" if season is not None else ""
            label = f"Get {index} · {hit.title}{season_bit}"
            buttons.append(
                [{"text": label[:60], "callback_data": callback_data}]
            )
        if buttons:
            lines.append("Tap the exact title to request it.")
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
