"""Deterministically parse Telegram messages into Overseerr searches.

This module deliberately performs no network calls and never asks a language
model to interpret a request. The only accepted catalog links are TMDB links,
whose type and id can be extracted locally.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from hearth.telegram.models import MediaQuery, MediaType, MessageView

_MAX_TITLE_LENGTH = 200
_URL = re.compile(r"https?://[^\s<>\[\]\"']+", re.IGNORECASE)
_MAGNET = re.compile(r"(?:^|\s)magnet:\?\S+", re.IGNORECASE)
_TORRENT = re.compile(
    r"(?:^|[/\\\s])[^\s/\\]+\.torrent(?=$|[\s.,;:!?()\[\]{}])",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_COMMAND = re.compile(
    r"^/(?P<command>[a-z]+)(?:@[a-z0-9_]+)?(?:\s+(?P<argument>.*))?$",
    re.IGNORECASE,
)
_REQUEST_PREFIX = re.compile(
    r"^(?:(?P<polite>please|pls|alstublieft|aub)\s+)?"
    r"(?P<verb>request|download|get|grab|find|search|zoek|haal|vraag)"
    r"(?:\s+(?P<filler>me|mij|for(?:\s+me)?|naar|voor(?:\s+mij)?))?"
    r"\s+(?P<title>.+)$",
    re.IGNORECASE,
)
_MODAL_REQUEST_PREFIX = re.compile(
    r"^(?:can|could|would|kan|kun)\s+(?:you|je|jij)\s+"
    r"(?:(?:please|even)\s+)?"
    r"(?:request|download|get|grab|find|search|zoek|haal|vraag)"
    r"(?:\s+(?:me|mij|for(?:\s+me)?|naar|voor(?:\s+mij)?))?"
    r"\s+(?P<title>.+)$",
    re.IGNORECASE,
)
_TYPE_PREFIX = re.compile(
    r"^(?P<type>movie|film|tv|show|series|serie)\s*[:\-–—]\s*(?P<title>.+)$",
    re.IGNORECASE,
)
_TYPE_SUFFIX = re.compile(
    r"^(?P<title>.+?)\s+\((?P<type>movie|film|tv|show|series|serie)\)$",
    re.IGNORECASE,
)
_SEASON = re.compile(
    r"^(?P<title>.+?)(?:\s+|[._-])(?:"
    r"[sS](?P<s_short>\d{1,3})(?:[._-]?[eE](?P<episode>\d{1,3}))?"
    r"|(?:season|seizoen)\s*(?P<s_word>\d{1,3})"
    r")\s*$",
    re.IGNORECASE,
)
_SEASON_ONLY = re.compile(
    r"^(?:"
    r"[sS](?P<s_short>\d{1,3})(?:[._-]?[eE](?P<episode>\d{1,3}))?"
    r"|(?:season|seizoen)\s*(?P<s_word>\d{1,3})"
    r")$",
    re.IGNORECASE,
)
_EPISODE_TOKEN = re.compile(
    r"[sS]\d{1,3}(?:[._-]?[eE])\d{1,3}(?!\d)"
)
_MOVIE_HINT_PREFIX = re.compile(r"^(?:movie|film)(?:\s*[:\-–—]\s*|\s+)", re.IGNORECASE)
_MOVIE_HINT_SUFFIX = re.compile(r"\s+(?:movie|film)\s*$", re.IGNORECASE)
_MOVIE_SEASON_CONTRADICTION = re.compile(
    r"(?:"
    r"^(?:movie|film)(?:\s*[:\-–—]\s*|\s+).+\s+"
    r"(?:[sS]\d{1,3}|(?:season|seizoen)\s*\d{1,3})\s*$"
    r"|(?:^|\s)(?:[sS]\d{1,3}|(?:season|seizoen)\s*\d{1,3})"
    r"\s+(?:movie|film)\s*$"
    r")",
    re.IGNORECASE,
)
_TITLE_YEAR = re.compile(
    r"^(?P<title>.+?)\s*\(\s*(?P<year>(?:18|19|20|21)\d{2})\s*\)\s*$"
)
_TMDB_TYPED_ID = re.compile(
    r"\btmdb\s*:\s*(?P<type>movie|film|tv|show|series|serie)\s*:\s*(?P<id>\d{1,10})\b",
    re.IGNORECASE,
)
_TYPE_TMDB_ID = re.compile(
    r"\b(?P<type>movie|film|tv|show|series|serie)\s+tmdb\s*:\s*(?P<id>\d{1,10})\b",
    re.IGNORECASE,
)
_TMDB_ID_TYPE = re.compile(
    r"\btmdb\s*:\s*(?P<id>\d{1,10})\s+(?P<type>movie|film|tv|show|series|serie)\b",
    re.IGNORECASE,
)
_UNTYPED_TMDB = re.compile(r"\btmdb\s*:\s*\d{1,10}\b", re.IGNORECASE)
_TMDB_MARKER = re.compile(r"\btmdb\s*:", re.IGNORECASE)
_TMDB_PATH = re.compile(
    r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?P<type>movie|tv)/(?P<id>\d{1,10})(?!\d)"
    r"(?:[-/](?P<slug>[^/?#]+))?",
    re.IGNORECASE,
)

_GREETINGS_AND_CHATTER = frozenset(
    {
        "hi",
        "hey",
        "hello",
        "hoi",
        "hallo",
        "yo",
        "ok",
        "okay",
        "thanks",
        "thank you",
        "dank je",
        "dankjewel",
        "thx",
        "ty",
        "k",
        "kk",
        "nice",
        "great",
        "top",
        "cool",
        "yes",
        "yeah",
        "yep",
        "no",
        "nope",
        "lol",
        "haha",
        "sup",
        "how are you",
        "how are you?",
        "what's up",
        "whats up",
        "good morning",
        "good night",
        "goedemorgen",
        "goedenavond",
        "welterusten",
        "status",
        "👍",
        "🙏",
        "❤️",
        "+1",
        "+",
        "++",
    }
)


def _normalize(value: str) -> str:
    return _SPACE.sub(" ", value.strip())


def _media_type(value: str) -> MediaType:
    return "movie" if value.casefold() in {"movie", "film"} else "tv"


def _clean_title(value: str) -> str:
    return _normalize(value).strip(" \"'`.,;:!?-–—|")


def _title_from_slug(value: str) -> str:
    slug = unquote(value or "").replace("_", " ").replace("-", " ")
    return _normalize(slug).title()


def _parse_tmdb_url(value: str) -> tuple[MediaType, int, str, str] | None:
    """Extract a typed TMDB id locally. This function never fetches a URL."""
    cleaned = value.rstrip('.,;!?)]}"')
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in {"themoviedb.org", "www.themoviedb.org"}:
        return None
    match = _TMDB_PATH.search(unquote(parsed.path or ""))
    if not match:
        return None
    tmdb_id = int(match.group("id"))
    if tmdb_id <= 0:
        return None
    return (
        _media_type(match.group("type")),
        tmdb_id,
        _title_from_slug(match.group("slug") or ""),
        host,
    )


def _extract_title_parts(
    text: str,
    *,
    media_type: MediaType | None = None,
) -> tuple[str, int | None, int | None, int | None, MediaType | None]:
    title = _clean_title(text)
    season: int | None = None
    episode: int | None = None
    season_match = _SEASON.match(title)
    if season_match:
        title = _clean_title(season_match.group("title"))
        season = int(season_match.group("s_short") or season_match.group("s_word"))
        episode_text = season_match.group("episode")
        episode = int(episode_text) if episode_text is not None else None
        media_type = "tv"

    type_match = _TYPE_PREFIX.match(title)
    if type_match:
        media_type = _media_type(type_match.group("type"))
        title = _clean_title(type_match.group("title"))
    else:
        type_match = _TYPE_SUFFIX.match(title)
        if type_match:
            media_type = _media_type(type_match.group("type"))
            title = _clean_title(type_match.group("title"))

    year: int | None = None
    year_match = _TITLE_YEAR.match(title)
    if year_match:
        title = _clean_title(year_match.group("title"))
        year = int(year_match.group("year"))

    return title, year, season, episode, media_type


def _parse_season_residual(
    text: str,
) -> tuple[int | None, int | None, bool]:
    """Parse an optional season token and require full residual consumption."""
    residual = _clean_title(text)
    if not residual:
        return None, None, True
    match = _SEASON_ONLY.fullmatch(residual)
    if not match:
        return None, None, False
    season = int(match.group("s_short") or match.group("s_word"))
    episode_text = match.group("episode")
    episode = int(episode_text) if episode_text is not None else None
    return season, episode, True


def parse_message_text(
    text: str,
    *,
    max_length: int = _MAX_TITLE_LENGTH,
    is_bot: bool = False,
    has_media: bool = False,
    media_kind: str = "",
    # Transitional keyword names used by the former inbox implementation.
    has_media_file: bool | None = None,
    media_kind_hint: str | None = None,
) -> MediaQuery:
    """Classify a Telegram message without executing or fetching anything."""
    if has_media_file is not None:
        has_media = has_media_file
    if media_kind_hint is not None:
        media_kind = media_kind_hint
    raw = (text or "").strip()
    clipped = raw[: max(0, max_length * 2)]

    if is_bot:
        return MediaQuery(action="ignore", reason="bot_sender", raw_text=clipped)
    if has_media:
        return MediaQuery(
            action="reject",
            reason=f"media_attachment:{media_kind or 'unknown'}",
            raw_text=clipped,
        )
    if _MAGNET.search(raw) or _TORRENT.search(raw):
        return MediaQuery(action="reject", reason="torrent_download", raw_text=clipped)
    if not raw:
        return MediaQuery(action="ignore", reason="empty")
    if _CONTROL.search(raw) or len(raw) > max(max_length * 4, 800):
        return MediaQuery(action="reject", reason="invalid_text")

    normalized = _normalize(raw)
    lowered = normalized.casefold()
    if lowered in _GREETINGS_AND_CHATTER or len(normalized) == 1:
        return MediaQuery(action="ignore", reason="chatter", raw_text=clipped)

    explicit_command = False
    command = _COMMAND.match(normalized)
    if command:
        name = command.group("command").casefold()
        argument = _normalize(command.group("argument") or "")
        if name in {"start", "help"}:
            return MediaQuery(action="help", reason=f"command:{name}", raw_text=clipped)
        if name == "status":
            return MediaQuery(action="status", reason="command:status", raw_text=clipped)
        if name not in {"search", "request"}:
            return MediaQuery(action="ignore", reason="unknown_command", raw_text=clipped)
        if not argument:
            return MediaQuery(action="help", reason="missing_query", raw_text=clipped)
        normalized = argument
        explicit_command = True

    prefix = None if explicit_command else _MODAL_REQUEST_PREFIX.match(normalized)
    if prefix is None and not explicit_command:
        candidate = _REQUEST_PREFIX.match(normalized)
        if candidate is not None:
            # Keep title-cased ambiguous titles such as "Get Out" and
            # "Search Party". Lower-case verbs and any polite/filler form are
            # explicit request language.
            verb = candidate.group("verb")
            ambiguous = verb.casefold() in {"get", "find", "search"}
            explicit = bool(candidate.group("polite") or candidate.group("filler"))
            if not ambiguous or verb.islower() or explicit:
                prefix = candidate
    if prefix is not None:
        normalized = _normalize(prefix.group("title"))
    elif not explicit_command and normalized.casefold() in {
        "request",
        "download",
        "get",
        "grab",
        "find",
        "search",
        "zoek",
        "haal",
        "vraag",
    }:
        return MediaQuery(action="help", reason="missing_query", raw_text=clipped)

    if _EPISODE_TOKEN.search(normalized):
        return MediaQuery(
            action="reject",
            reason="episode_not_supported",
            raw_text=clipped,
        )
    if _MOVIE_SEASON_CONTRADICTION.search(normalized):
        return MediaQuery(action="reject", reason="movie_has_season", raw_text=clipped)

    urls = _URL.findall(normalized)
    if len(urls) > 1:
        return MediaQuery(action="reject", reason="ambiguous_catalog_id", raw_text=clipped)
    for url in urls:
        parsed_url = _parse_tmdb_url(url)
        if parsed_url is None:
            continue
        media_type, tmdb_id, title, host = parsed_url
        season, episode, residual_ok = _parse_season_residual(
            _URL.sub(" ", normalized)
        )
        if not residual_ok:
            return MediaQuery(action="reject", reason="invalid_season", raw_text=clipped)
        if episode is not None:
            return MediaQuery(
                action="reject",
                reason="episode_not_supported",
                raw_text=clipped,
            )
        if season is not None and media_type == "movie":
            return MediaQuery(action="reject", reason="movie_has_season", raw_text=clipped)
        return MediaQuery(
            action="search",
            media_type=media_type,
            title=title[:max_length],
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
            reason="tmdb_url",
            raw_text=clipped,
            catalog_host=host,
        )
    if urls:
        # Never turn arbitrary URLs into title searches or fetch them.
        return MediaQuery(action="ignore", reason="unsupported_url", raw_text=clipped)

    typed_id = (
        _TMDB_TYPED_ID.search(normalized)
        or _TYPE_TMDB_ID.search(normalized)
        or _TMDB_ID_TYPE.search(normalized)
    )
    if typed_id:
        tmdb_id = int(typed_id.group("id"))
        if tmdb_id <= 0:
            return MediaQuery(action="reject", reason="invalid_tmdb_id", raw_text=clipped)
        media_type = _media_type(typed_id.group("type"))
        # A season may be written on either side of the exact id. Inspect the
        # full residual text so ``S02 tmdb:tv:95396`` cannot widen to ``all``.
        residual = _normalize(
            f"{normalized[: typed_id.start()]} {normalized[typed_id.end() :]}"
        )
        season, episode, residual_ok = _parse_season_residual(residual)
        if not residual_ok:
            return MediaQuery(action="reject", reason="invalid_season", raw_text=clipped)
        if episode is not None:
            return MediaQuery(
                action="reject",
                reason="episode_not_supported",
                raw_text=clipped,
            )
        if season is not None and media_type != "tv":
            return MediaQuery(action="reject", reason="movie_has_season", raw_text=clipped)
        return MediaQuery(
            action="search",
            media_type=media_type,
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
            reason="tmdb_id",
            raw_text=clipped,
        )
    if _UNTYPED_TMDB.search(normalized):
        return MediaQuery(action="reject", reason="tmdb_type_required", raw_text=clipped)
    if _TMDB_MARKER.search(normalized):
        return MediaQuery(action="reject", reason="invalid_tmdb_id", raw_text=clipped)

    title, year, season, episode, media_type = _extract_title_parts(normalized)
    if episode is not None:
        return MediaQuery(
            action="reject",
            reason="episode_not_supported",
            raw_text=clipped,
        )
    movie_contradiction = bool(
        season is not None
        and (
            media_type == "movie"
            or _MOVIE_HINT_PREFIX.search(normalized)
            or _MOVIE_HINT_SUFFIX.search(normalized)
        )
    )
    if movie_contradiction:
        return MediaQuery(action="reject", reason="movie_has_season", raw_text=clipped)
    if len(title) > max_length:
        return MediaQuery(action="reject", reason="title_too_long", raw_text=title[:80])
    if len(title) < 2 or not any(character.isalnum() for character in title):
        return MediaQuery(action="ignore", reason="not_a_title", raw_text=clipped)

    return MediaQuery(
        action="search",
        media_type=media_type,
        title=title,
        year=year,
        season=season,
        episode=episode,
        reason="title",
        raw_text=clipped,
    )


def parse_message(
    message: dict[str, Any] | MessageView,
    *,
    max_length: int = _MAX_TITLE_LENGTH,
    bot_user_id: int | None = None,
) -> tuple[MessageView | None, MediaQuery]:
    view = message if isinstance(message, MessageView) else MessageView.from_telegram(message)
    if view is None:
        return None, MediaQuery(action="ignore", reason="invalid_message")
    if view.is_bot or (bot_user_id is not None and view.user_id == bot_user_id):
        return view, MediaQuery(action="ignore", reason="bot_sender")
    return view, parse_message_text(
        view.text,
        max_length=max_length,
        has_media=view.has_media,
        media_kind=view.media_kind,
    )


# One release-cycle import alias for small downstream extensions. New code
# should use MediaQuery directly.
ParsedRequest = MediaQuery
