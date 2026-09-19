"""Telegram result cards and inline keyboards.

Every Get button carries signed request coordinates (media type + TMDB id +
season) so confirming never re-searches by title. Refine buttons are signed too
but carry no queue authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from hearth.telegram.callbacks import (
    ACTION_DISMISS,
    ACTION_MORE,
    ACTION_SERIES,
    ACTION_SIMILAR,
    CallbackCodec,
)
from hearth.telegram.media import voice
from hearth.telegram.models import BotReply, MediaHit

STATUS_MARKS: dict[int, str] = {
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


class MediaKeyStore(Protocol):
    """The slice of ``TelegramStore`` card rendering needs."""

    def put_callback_media(
        self,
        callback_key: str,
        metadata: Mapping[str, Any],
        *,
        ttl_s: float = ...,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RenderedCards:
    """A rendered result message plus what actually got a Get button."""

    reply: BotReply
    requestable: tuple[tuple[MediaHit, int | None], ...]
    shown: tuple[MediaHit, ...]

    @property
    def single_offer(self) -> tuple[MediaHit, int | None] | None:
        return self.requestable[0] if len(self.requestable) == 1 else None


def button_label(index: int, hit: MediaHit, *, season: int | None = None) -> str:
    year_bit = f" ({hit.year})" if hit.year is not None else ""
    season_bit = f" S{season:02d}" if season is not None else ""
    kind = "movie" if hit.media_type == "movie" else "TV"
    # Telegram shows ~64 visible chars; keep index + title + year + kind.
    return f"Get {index} · {hit.title}{year_bit} {kind}{season_bit}"


def status_of(hit: MediaHit) -> str:
    return STATUS_MARKS.get(hit.media_status, "○ Not requested")


def blocked_status_line(hit: MediaHit) -> str:
    """A clear, single-line answer when there is nothing to request."""
    label = voice.display_title(hit.title, hit.year)
    if hit.media_status == 5:
        return voice.already_available(label)
    if hit.media_status in {2, 3}:
        return voice.already_requested(label)
    if hit.media_status == 4:
        return f"{label} is partly available in Plex — the rest is still coming."
    return f"{label} can't be requested right now ({status_of(hit).lstrip('○◷◐✓◇ ')})."


class CardRenderer:
    """Builds result messages, Get buttons, and refine buttons."""

    def __init__(self, codec: CallbackCodec, store: MediaKeyStore, *, ttl_s: int) -> None:
        self.codec = codec
        self.store = store
        self.ttl_s = max(60, int(ttl_s))

    def _emit(
        self,
        chat_id: int,
        hits: list[MediaHit],
        *,
        start_index: int,
        lines: list[str],
        rows: list[list[dict[str, str]]],
        requestable: list[tuple[MediaHit, int | None]],
        season: int | None,
        edition_key: str,
        edition_label: str,
    ) -> int:
        """Append one block of numbered cards; returns the next free index."""
        index = start_index
        for hit in hits:
            lines.append(f"{index}. {hit.display_label()} — {status_of(hit)}")
            explicitly_requesting_tv_season = hit.media_type == "tv" and season is not None
            non_requestable = hit.media_status == 5 or (
                hit.media_status in {2, 3} and not explicitly_requesting_tv_season
            )
            position = index
            index += 1
            if non_requestable:
                continue
            hit_season = season if hit.media_type == "tv" else None
            callback_data = self.codec.encode(
                hit.media_type,
                hit.tmdb_id,
                chat_id,
                season=hit_season,
            )
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "media_type": hit.media_type,
                "tmdb_id": hit.tmdb_id,
                "title": hit.title,
                "year": hit.year,
                "season": hit_season,
            }
            if edition_key:
                payload["edition_key"] = edition_key
                payload["edition_label"] = edition_label
            self.store.put_callback_media(callback_data, payload, ttl_s=self.ttl_s)
            label = button_label(position, hit, season=hit_season)
            rows.append([{"text": label[:64], "callback_data": callback_data}])
            requestable.append((hit, hit_season))
        return index

    def render(
        self,
        chat_id: int,
        hits: list[MediaHit],
        *,
        header: str,
        season: int | None = None,
        edition_key: str = "",
        edition_label: str = "",
        tap_hint: bool = True,
        similar_anchor: MediaHit | None = None,
        offer_more: bool = False,
        offer_dismiss: bool = False,
        series_anchor: MediaHit | None = None,
    ) -> RenderedCards:
        lines = [header]
        rows: list[list[dict[str, str]]] = []
        requestable: list[tuple[MediaHit, int | None]] = []
        self._emit(
            chat_id,
            hits,
            start_index=1,
            lines=lines,
            rows=rows,
            requestable=requestable,
            season=season,
            edition_key=edition_key,
            edition_label=edition_label,
        )

        if tap_hint and requestable:
            lines.append(voice.tap_get_hint(single=len(requestable) == 1))
        elif not requestable and len(hits) > 1:
            lines.append(voice.nothing_requestable())

        refine = self._refine_row(
            chat_id,
            similar_anchor=similar_anchor,
            series_anchor=series_anchor,
            offer_more=offer_more,
            offer_dismiss=offer_dismiss,
        )
        if refine:
            rows.append(refine)

        return RenderedCards(
            reply=BotReply(
                "\n".join(lines),
                reply_markup={"inline_keyboard": rows} if rows else None,
            ),
            requestable=tuple(requestable),
            shown=tuple(hits),
        )

    def render_plan(
        self,
        chat_id: int,
        groups: list[tuple[str, list[MediaHit]]],
        *,
        header: str,
        misses: list[str] | None = None,
        offer_dismiss: bool = False,
    ) -> RenderedCards:
        """One message for a compound ask, grouped per requested item."""
        lines = [header]
        rows: list[list[dict[str, str]]] = []
        requestable: list[tuple[MediaHit, int | None]] = []
        shown: list[MediaHit] = []
        index = 1
        for label, hits in groups:
            if not hits:
                continue
            lines.append(f"▸ {label}")
            index = self._emit(
                chat_id,
                hits,
                start_index=index,
                lines=lines,
                rows=rows,
                requestable=requestable,
                season=None,
                edition_key="",
                edition_label="",
            )
            shown.extend(hits)
        for miss in misses or []:
            lines.append(f"▸ {miss} — no catalog match")
        if requestable:
            lines.append(voice.tap_get_hint(single=len(requestable) == 1))
        elif shown:
            lines.append(voice.nothing_requestable())

        refine = self._refine_row(
            chat_id,
            similar_anchor=None,
            series_anchor=None,
            offer_more=False,
            offer_dismiss=offer_dismiss,
        )
        if refine:
            rows.append(refine)
        return RenderedCards(
            reply=BotReply(
                "\n".join(lines),
                reply_markup={"inline_keyboard": rows} if rows else None,
            ),
            requestable=tuple(requestable),
            shown=tuple(shown),
        )

    def _refine_row(
        self,
        chat_id: int,
        *,
        similar_anchor: MediaHit | None,
        series_anchor: MediaHit | None,
        offer_more: bool,
        offer_dismiss: bool,
    ) -> list[dict[str, str]]:
        row: list[dict[str, str]] = []
        if similar_anchor is not None:
            row.append(
                {
                    "text": "✨ More like this",
                    "callback_data": self.codec.encode_action(
                        ACTION_SIMILAR,
                        chat_id,
                        media_type=similar_anchor.media_type,
                        tmdb_id=similar_anchor.tmdb_id,
                    ),
                }
            )
        if series_anchor is not None:
            row.append(
                {
                    "text": "📚 All of them",
                    "callback_data": self.codec.encode_action(
                        ACTION_SERIES,
                        chat_id,
                        media_type=series_anchor.media_type,
                        tmdb_id=series_anchor.tmdb_id,
                    ),
                }
            )
        if offer_more:
            row.append(
                {
                    "text": "🔁 More options",
                    "callback_data": self.codec.encode_action(ACTION_MORE, chat_id),
                }
            )
        if offer_dismiss:
            row.append(
                {
                    "text": "✋ Nah",
                    "callback_data": self.codec.encode_action(ACTION_DISMISS, chat_id),
                }
            )
        return row[:3]


__all__ = [
    "STATUS_MARKS",
    "CardRenderer",
    "MediaKeyStore",
    "RenderedCards",
    "blocked_status_line",
    "button_label",
    "status_of",
]
