"""Parse Telegram group messages into movie/series/TV grab requests.

Treat as a request:
- Catalog links (IMDb, TMDB, TVDB, Trakt, JustWatch) — extract id/title/year.
- Plain IMDb tt IDs and TMDB/TVDB ids.
- Plain titles: "Movie Title", "Movie Title (2024)", "Show S02E03", "Show season 2".
- Optional quality hints (1080p, 4K) are recorded but not required to grab.

Do NOT treat as a request:
- Greetings, reactions, chatter ("ok", "thanks").
- Hearth's own status messages / bot outbound.
- Non-catalog http(s) links, shorteners, magnets, torrents, raw media files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

MediaKind = Literal["movie", "tv", "unknown"]
ParseKind = Literal[
    "request",
    "ignore",
    "reject_download",
    "disambiguation_pick",
]


# Hosts we trust for title/id extraction. Never fetch arbitrary URLs (SSRF).
CATALOG_HOSTS = frozenset(
    {
        "imdb.com",
        "www.imdb.com",
        "m.imdb.com",
        "themoviedb.org",
        "www.themoviedb.org",
        "thetvdb.com",
        "www.thetvdb.com",
        "trakt.tv",
        "www.trakt.tv",
        "justwatch.com",
        "www.justwatch.com",
    }
)

_GREETINGS = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "hi",
        "hey",
        "hello",
        "yo",
        "sup",
        "cool",
        "nice",
        "great",
        "yes",
        "yep",
        "yeah",
        "no",
        "nope",
        "lol",
        "haha",
        "👍",
        "🙏",
        "+",
        "++",
    }
)

_STATUS_PREFIXES = (
    "queued ",
    "couldn't find",
    "could not find",
    "already in ",
    "already queued",
    "failed to ",
    "which one",
    "pick one",
    "this inbox only queues",
    "too many requests",
    "rate limited",
    "ignored: ",
)

_STATUS_CONTAINS = (
    " is downloading",
    " is done —",
    " is done -",
    " failed.",
    " failed (",
    " via radarr.",
    " via sonarr.",
    " via overseerr.",
    " is already in the library",
    " is already queued",
)

_IMDB_TT = re.compile(r"\b(tt\d{5,10})\b", re.I)
_TMDB_ID = re.compile(r"\b(?:tmdb[:\s#]*)(\d{1,10})\b", re.I)
_TVDB_ID = re.compile(r"\b(?:tvdb[:\s#]*)(\d{1,10})\b", re.I)
_YEAR = re.compile(r"\((19|20)\d{2}\)")
_SEASON_EP = re.compile(
    r"^(?P<title>.+?)\s+[Ss](?P<season>\d{1,2})(?:[Ee](?P<episode>\d{1,3}))?\s*$"
)
_SEASON_WORD = re.compile(
    r"^(?P<title>.+?)\s+(?:season|seizoen)\s+(?P<season>\d{1,2})\s*$",
    re.I,
)
_QUALITY = re.compile(
    r"\b(?P<q>2160p|1080p|720p|480p|4k|uhd|hdr|dv|dolby\s*vision)\b",
    re.I,
)
_URL = re.compile(r"https?://[^\s<>\[\]()\"']+", re.I)
_MAGNET = re.compile(r"magnet:\?[^\s]+", re.I)
_BINARYISH = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PICK = re.compile(r"^\s*(?:#?\s*)?([1-3])\s*$")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedRequest:
    kind: ParseKind
    media_kind: MediaKind = "unknown"
    title: str = ""
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    season: int | None = None
    episode: int | None = None
    quality: str | None = None
    pick_index: int | None = None
    reason: str = ""
    catalog_host: str | None = None
    raw_text: str = ""

    def search_query(self) -> str:
        """Human/search term for *arr — never a raw ``tt…`` id string."""
        if self.title:
            bits = [self.title.strip()]
            if self.year:
                bits.append(f"({self.year})")
            return " ".join(bit for bit in bits if bit).strip()
        if self.tmdb_id and self.media_kind != "tv":
            return f"tmdb:{self.tmdb_id}"
        if self.tvdb_id:
            return f"tvdb:{self.tvdb_id}"
        # Unresolved IMDb id: empty — callers must resolve via catalog first.
        return ""

    def display_label(self) -> str:
        """Label for user-facing not-found / status — never a raw ``tt…`` id."""
        if self.title and self.year:
            return f"{self.title} ({self.year})"
        if self.title:
            return self.title
        return "that title"

    def dedup_key(self) -> str:
        if self.imdb_id:
            return f"imdb:{self.imdb_id.lower()}"
        if self.tmdb_id:
            return f"tmdb:{self.tmdb_id}:{self.media_kind}"
        if self.tvdb_id:
            return f"tvdb:{self.tvdb_id}"
        title = normalize_title(self.title)
        year = self.year or ""
        return f"title:{title}:{year}:{self.media_kind}"

    def needs_catalog_resolve(self) -> bool:
        """True when an external id or title/year must be verified on TMDB."""
        if self.kind != "request":
            return False
        if self.imdb_id or self.tmdb_id or self.tvdb_id:
            return True
        return bool(self.title)


@dataclass
class MessageView:
    """Minimal redacted view of an inbound Telegram message (no full payload kept)."""

    chat_id: int
    message_id: int
    user_id: int | None
    text: str
    is_bot: bool = False
    has_media_file: bool = False
    media_kind_hint: str = ""
    reply_to_message_id: int | None = None
    reply_to_text: str = ""

    @classmethod
    def from_telegram(cls, message: dict[str, Any]) -> MessageView | None:
        if not isinstance(message, dict):
            return None
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        try:
            chat_id = int(chat.get("id"))
            message_id = int(message.get("message_id"))
        except (TypeError, ValueError):
            return None
        user_id = None
        if sender.get("id") is not None:
            try:
                user_id = int(sender["id"])
            except (TypeError, ValueError):
                user_id = None
        text = (
            message.get("text")
            or message.get("caption")
            or ""
        )
        if not isinstance(text, str):
            text = str(text)
        reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
        reply_id = None
        if reply.get("message_id") is not None:
            try:
                reply_id = int(reply["message_id"])
            except (TypeError, ValueError):
                reply_id = None
        reply_text = ""
        if isinstance(reply.get("text"), str):
            reply_text = reply["text"]
        elif isinstance(reply.get("caption"), str):
            reply_text = reply["caption"]
        has_file = any(
            message.get(key)
            for key in (
                "document",
                "video",
                "audio",
                "voice",
                "video_note",
                "animation",
                "sticker",
                "photo",
            )
        )
        # Stickers / voice with no caption are chatter, not requests.
        media_hint = ""
        for key in ("document", "video", "audio", "voice", "sticker", "photo"):
            if message.get(key):
                media_hint = key
                break
        return cls(
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            text=text.strip(),
            is_bot=bool(sender.get("is_bot")),
            has_media_file=has_file,
            media_kind_hint=media_hint,
            reply_to_message_id=reply_id,
            reply_to_text=reply_text[:400],
        )


def normalize_title(value: str) -> str:
    return _WHITESPACE.sub(" ", (value or "").strip().lower())


def is_status_echo(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if any(lowered.startswith(prefix) for prefix in _STATUS_PREFIXES):
        return True
    return any(bit in lowered for bit in _STATUS_CONTAINS)


def _host_allowed(hostname: str) -> bool:
    host = (hostname or "").lower().rstrip(".")
    if host in CATALOG_HOSTS:
        return True
    # Allow one subdomain level under known catalog apexes (e.g. www).
    parts = host.split(".")
    if len(parts) >= 2:
        apex = ".".join(parts[-2:])
        return apex in {h for h in CATALOG_HOSTS if h.count(".") == 1} or host in CATALOG_HOSTS
    return False


def _strip_quality(text: str) -> tuple[str, str | None]:
    match = _QUALITY.search(text)
    quality = match.group("q").upper().replace(" ", "") if match else None
    cleaned = _QUALITY.sub(" ", text)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" -–—|,")
    return cleaned, quality


def _parse_catalog_url(url: str) -> ParsedRequest | None:
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.hostname or "").lower()
    if not host or not _host_allowed(host):
        return None
    path = unquote(parsed.path or "")
    query = parsed.query or ""

    imdb = _IMDB_TT.search(path) or _IMDB_TT.search(query)
    if "imdb.com" in host and imdb:
        return ParsedRequest(
            kind="request",
            media_kind="unknown",
            imdb_id=imdb.group(1).lower(),
            catalog_host=host,
            reason="imdb_url",
        )

    tmdb_movie = re.search(r"/movie/(\d+)", path, re.I)
    tmdb_tv = re.search(r"/(?:tv|tv-show|show)/(\d+)", path, re.I)
    if "themoviedb.org" in host:
        if tmdb_movie:
            slug_title = _slug_title(path, after=tmdb_movie.end())
            return ParsedRequest(
                kind="request",
                media_kind="movie",
                tmdb_id=int(tmdb_movie.group(1)),
                title=slug_title,
                catalog_host=host,
                reason="tmdb_movie_url",
            )
        if tmdb_tv:
            slug_title = _slug_title(path, after=tmdb_tv.end())
            return ParsedRequest(
                kind="request",
                media_kind="tv",
                tmdb_id=int(tmdb_tv.group(1)),
                title=slug_title,
                catalog_host=host,
                reason="tmdb_tv_url",
            )

    tvdb = re.search(r"/(?:series|movie|dereferrer/(?:series|movie))/(\d+)", path, re.I)
    if "thetvdb.com" in host and tvdb:
        return ParsedRequest(
            kind="request",
            media_kind="tv" if "series" in path.lower() else "unknown",
            tvdb_id=int(tvdb.group(1)),
            catalog_host=host,
            reason="tvdb_url",
        )

    trakt_movie = re.search(r"/movies/([^/?#]+)", path, re.I)
    trakt_show = re.search(r"/shows/([^/?#]+)", path, re.I)
    if "trakt.tv" in host:
        if trakt_movie:
            title, year = _title_year_from_slug(trakt_movie.group(1))
            return ParsedRequest(
                kind="request",
                media_kind="movie",
                title=title,
                year=year,
                catalog_host=host,
                reason="trakt_movie_url",
            )
        if trakt_show:
            title, year = _title_year_from_slug(trakt_show.group(1))
            return ParsedRequest(
                kind="request",
                media_kind="tv",
                title=title,
                year=year,
                catalog_host=host,
                reason="trakt_show_url",
            )

    justwatch = re.search(r"/(?:movie|tv-show|show)/([^/?#]+)", path, re.I)
    if "justwatch.com" in host and justwatch:
        media = "tv" if "/tv-show" in path.lower() or "/show/" in path.lower() else "movie"
        title, year = _title_year_from_slug(justwatch.group(1))
        return ParsedRequest(
            kind="request",
            media_kind=media,  # type: ignore[arg-type]
            title=title,
            year=year,
            catalog_host=host,
            reason="justwatch_url",
        )
    return None


def _slug_title(path: str, *, after: int) -> str:
    rest = path[after:].lstrip("/-")
    if not rest:
        return ""
    slug = rest.split("/")[0]
    return _title_year_from_slug(slug)[0]


def _title_year_from_slug(slug: str) -> tuple[str, int | None]:
    raw = unquote(slug or "").replace("_", " ").replace("-", " ")
    raw = _WHITESPACE.sub(" ", raw).strip()
    year = None
    m = re.search(r"(19|20)\d{2}$", raw)
    if m:
        year = int(m.group(0))
        raw = raw[: m.start()].strip()
    title = raw.title() if raw else ""
    return title, year


def _parse_plain_title(text: str) -> ParsedRequest | None:
    cleaned, quality = _strip_quality(text)
    if not cleaned:
        return None

    season_ep = _SEASON_EP.match(cleaned)
    if season_ep:
        title = season_ep.group("title").strip(" -–—|")
        year = None
        y = _YEAR.search(title)
        if y:
            year = int(y.group(0)[1:-1])
            title = _YEAR.sub("", title).strip()
        return ParsedRequest(
            kind="request",
            media_kind="tv",
            title=title,
            year=year,
            season=int(season_ep.group("season")),
            episode=int(season_ep.group("episode")) if season_ep.group("episode") else None,
            quality=quality,
            reason="season_episode",
        )

    season_word = _SEASON_WORD.match(cleaned)
    if season_word:
        title = season_word.group("title").strip(" -–—|")
        year = None
        y = _YEAR.search(title)
        if y:
            year = int(y.group(0)[1:-1])
            title = _YEAR.sub("", title).strip()
        return ParsedRequest(
            kind="request",
            media_kind="tv",
            title=title,
            year=year,
            season=int(season_word.group("season")),
            quality=quality,
            reason="season_word",
        )

    year = None
    y = _YEAR.search(cleaned)
    title = cleaned
    if y:
        year = int(y.group(0)[1:-1])
        title = _YEAR.sub("", cleaned).strip(" -–—|")
    title = title.strip(" \"'`")
    if len(title) < 2:
        return None
    # Reject pure numbers / punctuation.
    if not re.search(r"[A-Za-zÀ-ÿ]", title):
        return None
    return ParsedRequest(
        kind="request",
        media_kind="unknown",
        title=title,
        year=year,
        quality=quality,
        reason="plain_title",
    )


def parse_message_text(
    text: str,
    *,
    max_length: int = 200,
    is_bot: bool = False,
    has_media_file: bool = False,
    media_kind_hint: str = "",
) -> ParsedRequest:
    """Classify a message body. Never executes text as code or a shell command."""
    raw = (text or "").strip()
    if is_bot or is_status_echo(raw):
        return ParsedRequest(kind="ignore", reason="bot_or_status", raw_text=raw[:max_length])

    if _BINARYISH.search(raw) or len(raw) > max(max_length * 4, 800):
        return ParsedRequest(kind="ignore", reason="oversized_or_binary", raw_text="")

    if _MAGNET.search(raw) or re.search(r"\.(torrent)\b", raw, re.I):
        return ParsedRequest(
            kind="reject_download",
            reason="magnet_or_torrent",
            raw_text=raw[:max_length],
        )

    # Raw media attachments without a catalog caption are not a general downloader.
    if has_media_file and media_kind_hint in {"document", "video", "audio", "voice", "sticker"} and not raw:
        if media_kind_hint == "sticker":
            return ParsedRequest(kind="ignore", reason="sticker", raw_text="")
        if media_kind_hint in {"voice", "audio"}:
            return ParsedRequest(kind="ignore", reason="voice", raw_text="")
        return ParsedRequest(
            kind="reject_download",
            reason="media_attachment",
            raw_text="",
        )

    if not raw:
        return ParsedRequest(kind="ignore", reason="empty", raw_text="")

    pick = _PICK.match(raw)
    if pick:
        return ParsedRequest(
            kind="disambiguation_pick",
            pick_index=int(pick.group(1)),
            reason="pick_index",
            raw_text=raw,
        )

    lowered = normalize_title(raw)
    if lowered in _GREETINGS or len(lowered) <= 1:
        return ParsedRequest(kind="ignore", reason="greeting", raw_text=raw)

    # Catalog / id extraction first.
    urls = _URL.findall(raw)
    non_catalog = False
    for url in urls:
        catalog = _parse_catalog_url(url)
        if catalog:
            return ParsedRequest(
                kind=catalog.kind,
                media_kind=catalog.media_kind,
                title=catalog.title[:max_length],
                year=catalog.year,
                imdb_id=catalog.imdb_id,
                tmdb_id=catalog.tmdb_id,
                tvdb_id=catalog.tvdb_id,
                quality=catalog.quality,
                catalog_host=catalog.catalog_host,
                reason=catalog.reason,
                raw_text=raw[: max_length * 2],
            )
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:  # noqa: BLE001
            host = ""
        if host and not _host_allowed(host):
            non_catalog = True

    if non_catalog and not (_IMDB_TT.search(raw) or _TMDB_ID.search(raw) or _TVDB_ID.search(raw)):
        # Only URLs, none catalog → ignore (not a grab).
        remainder = _URL.sub(" ", raw).strip()
        if not remainder or normalize_title(remainder) in _GREETINGS:
            return ParsedRequest(kind="ignore", reason="non_catalog_url", raw_text=raw[:max_length])

    imdb = _IMDB_TT.search(raw)
    if imdb:
        return ParsedRequest(
            kind="request",
            media_kind="unknown",
            imdb_id=imdb.group(1).lower(),
            reason="imdb_id",
            raw_text=raw[:max_length],
        )
    tmdb = _TMDB_ID.search(raw)
    if tmdb:
        return ParsedRequest(
            kind="request",
            media_kind="unknown",
            tmdb_id=int(tmdb.group(1)),
            reason="tmdb_id",
            raw_text=raw[:max_length],
        )
    tvdb = _TVDB_ID.search(raw)
    if tvdb:
        return ParsedRequest(
            kind="request",
            media_kind="tv",
            tvdb_id=int(tvdb.group(1)),
            reason="tvdb_id",
            raw_text=raw[:max_length],
        )

    # Strip URLs before plain-title parse.
    without_urls = _URL.sub(" ", raw)
    without_urls = _WHITESPACE.sub(" ", without_urls).strip()
    if len(without_urls) > max_length:
        return ParsedRequest(kind="ignore", reason="title_too_long", raw_text=without_urls[:80])

    plain = _parse_plain_title(without_urls)
    if plain:
        return ParsedRequest(
            kind=plain.kind,
            media_kind=plain.media_kind,
            title=plain.title[:max_length],
            year=plain.year,
            season=plain.season,
            episode=plain.episode,
            quality=plain.quality,
            reason=plain.reason,
            raw_text=raw[: max_length * 2],
        )
    return ParsedRequest(kind="ignore", reason="unrecognized", raw_text=raw[:max_length])


def parse_message(
    message: dict[str, Any] | MessageView,
    *,
    max_length: int = 200,
    bot_user_id: int | None = None,
) -> tuple[MessageView | None, ParsedRequest]:
    view = message if isinstance(message, MessageView) else MessageView.from_telegram(message)
    if view is None:
        return None, ParsedRequest(kind="ignore", reason="invalid_message")
    if bot_user_id is not None and view.user_id == bot_user_id:
        return view, ParsedRequest(kind="ignore", reason="own_bot", raw_text="")
    if view.is_bot:
        return view, ParsedRequest(kind="ignore", reason="bot_sender", raw_text=view.text[:max_length])
    parsed = parse_message_text(
        view.text,
        max_length=max_length,
        is_bot=False,
        has_media_file=view.has_media_file,
        media_kind_hint=view.media_kind_hint,
    )
    return view, parsed
