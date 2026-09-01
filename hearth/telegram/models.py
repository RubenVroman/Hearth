"""Small, transport-independent models for the Telegram media bot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

MediaType = Literal["movie", "tv"]
QueryAction = Literal["search", "help", "status", "ignore", "reject"]


@dataclass(frozen=True, slots=True)
class MediaQuery:
    """A deterministic interpretation of one Telegram message."""

    action: QueryAction
    media_type: MediaType | None = None
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    tmdb_id: int | None = None
    reason: str = ""
    raw_text: str = ""
    catalog_host: str | None = None

    @property
    def is_search(self) -> bool:
        return self.action == "search"

    def search_query(self) -> str:
        """Return the query to pass to Overseerr/Seerr search."""
        if self.tmdb_id is not None:
            return f"tmdb:{self.tmdb_id}"
        if not self.title:
            return ""
        return f"{self.title} ({self.year})" if self.year is not None else self.title

    @property
    def query(self) -> str:
        return self.search_query()

    def display_label(self) -> str:
        if self.title and self.year is not None:
            return f"{self.title} ({self.year})"
        if self.title:
            return self.title
        if self.tmdb_id is not None:
            kind = "movie" if self.media_type == "movie" else "series"
            return f"TMDB {kind} {self.tmdb_id}"
        return "that title"

    def dedup_key(self) -> str:
        season = "all" if self.season is None else str(self.season)
        if self.tmdb_id is not None:
            return f"tmdb:{self.media_type}:{self.tmdb_id}:{season}"
        normalized = " ".join(self.title.casefold().split())
        return (
            f"title:{self.media_type or 'any'}:{normalized}:"
            f"{self.year or ''}:{season}"
        )


@dataclass(frozen=True, slots=True)
class MediaHit:
    """Normalized movie/series row returned by Overseerr/Seerr."""

    media_type: MediaType
    tmdb_id: int
    title: str
    original_title: str = ""
    year: int | None = None
    media_status: int | None = None
    overview: str = ""
    poster_path: str | None = None
    vote_average: float | None = None

    @classmethod
    def from_overseerr(cls, row: Mapping[str, Any]) -> MediaHit:
        """Normalize one official search-result shape.

        Person results and malformed rows are rejected instead of being guessed.
        """
        media_type = str(row.get("mediaType") or "").strip().lower()
        if media_type not in {"movie", "tv"}:
            raise ValueError("not a movie or TV search result")
        # The gateway's normalized rows use tmdbId/mediaId; the official raw
        # API uses id. mediaInfo.id is Overseerr's internal DB id and must never
        # be sent back as mediaId in a request.
        external_id: Any = None
        for key in ("tmdbId", "mediaId", "id"):
            if key in row and row.get(key) is not None:
                external_id = row.get(key)
                break
        try:
            tmdb_id = int(external_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("search result has no valid TMDB id") from exc
        if tmdb_id <= 0:
            raise ValueError("search result has no valid TMDB id")

        title = str(row.get("title") or row.get("name") or "").strip()
        if not title:
            raise ValueError("search result has no title")
        date = str(row.get("releaseDate") or row.get("firstAirDate") or "")
        year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
        if row.get("year") is not None:
            try:
                year = int(row["year"])
            except (TypeError, ValueError):
                pass

        media_info = row.get("mediaInfo")
        status: int | None = None
        normalized_status = row.get("mediaStatus")
        if normalized_status is not None:
            try:
                status = int(normalized_status)
            except (TypeError, ValueError):
                status = None
        elif isinstance(media_info, Mapping) and media_info.get("status") is not None:
            try:
                status = int(media_info["status"])
            except (TypeError, ValueError):
                status = None
        elif row.get("inLibrary") is True:
            status = 5

        vote: float | None = None
        if row.get("voteAverage") is not None:
            try:
                vote = float(row["voteAverage"])
            except (TypeError, ValueError):
                vote = None

        poster = row.get("posterPath")
        return cls(
            media_type=media_type,  # type: ignore[arg-type]
            tmdb_id=tmdb_id,
            title=title,
            original_title=str(
                row.get("originalTitle") or row.get("originalName") or ""
            ).strip(),
            year=year,
            media_status=status,
            overview=str(row.get("overview") or "").strip(),
            poster_path=str(poster).strip() if poster else None,
            vote_average=vote,
        )

    @property
    def available(self) -> bool:
        # Status 5 is "available" in both Overseerr and Seerr.
        return self.media_status == 5

    @property
    def in_library(self) -> bool:
        return self.available

    @property
    def already_requested(self) -> bool:
        return self.media_status in {2, 3}

    @property
    def status_label(self) -> str:
        return {
            1: "unknown",
            2: "pending",
            3: "processing",
            4: "partly available",
            5: "available",
            6: "blocklisted or deleted",
            7: "removed",
        }.get(self.media_status, "not requested")

    def display_label(self) -> str:
        kind = "Movie" if self.media_type == "movie" else "Series"
        year = f" ({self.year})" if self.year is not None else ""
        return f"{self.title}{year} · {kind}"


@dataclass(frozen=True, slots=True)
class BotReply:
    """A reply the Telegram transport can send or use to edit a message."""

    text: str
    reply_markup: dict[str, Any] | None = None
    edit_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class MessageView:
    """The small subset of an inbound Telegram message the bot retains."""

    chat_id: int
    message_id: int
    user_id: int | None
    text: str
    is_bot: bool = False
    has_media: bool = False
    media_kind: str = ""

    @classmethod
    def from_telegram(cls, message: Mapping[str, Any]) -> MessageView | None:
        if not isinstance(message, Mapping):
            return None
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, Mapping):
            return None
        if not isinstance(sender, Mapping):
            sender = {}
        try:
            chat_id = int(chat["id"])
            message_id = int(message["message_id"])
        except (KeyError, TypeError, ValueError):
            return None

        user_id: int | None = None
        if sender.get("id") is not None:
            try:
                user_id = int(sender["id"])
            except (TypeError, ValueError):
                pass

        text = message.get("text") or message.get("caption") or ""
        if not isinstance(text, str):
            text = str(text)

        media_kind = ""
        for key in (
            "document",
            "video",
            "audio",
            "voice",
            "video_note",
            "animation",
            "sticker",
            "photo",
        ):
            if message.get(key):
                media_kind = key
                break

        return cls(
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            text=text.strip(),
            is_bot=bool(sender.get("is_bot")),
            has_media=bool(media_kind),
            media_kind=media_kind,
        )
