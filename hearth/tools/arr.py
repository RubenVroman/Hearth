"""Radarr / Sonarr / Overseerr — the VAULT *arr request pipeline, not Plex playback."""

from __future__ import annotations

import re
from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import pipeline

# Spoken / tool statuses for active *arr downloads.
DOWNLOAD_STATUSES = (
    "queued",
    "downloading",
    "paused",
    "importing",
    "stalled",
    "completed",
    "failed",
    "unknown",
)

_TITLE_YEAR_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")
_ARTICLES = frozenset({"the", "a", "an", "de", "het", "een"})


def _normalize_title_tokens(value: str) -> list[str]:
    text = re.sub(r"[^a-z0-9à-ÿ]+", " ", (value or "").lower()).strip()
    tokens = [t for t in text.split() if t]
    while tokens and tokens[0] in _ARTICLES:
        tokens = tokens[1:]
    return tokens


def title_seed_matches(seed: str, title: str) -> bool:
    """Exact / multi-word franchise-prefix match — never Land→La La Land.

    Shared gate for Overseerr auto-request: mismatched search/fallback hits
    must not be queued.
    """
    seed_tokens = _normalize_title_tokens(seed)
    title_tokens = _normalize_title_tokens(title)
    if not seed_tokens or not title_tokens:
        return False
    if seed_tokens == title_tokens:
        return True
    if len(seed_tokens) >= 2 and title_tokens[: len(seed_tokens)] == seed_tokens:
        return True
    return False


def _split_query_year(query: str) -> tuple[str, int | None]:
    raw = (query or "").strip()
    match = _TITLE_YEAR_RE.match(raw)
    if not match:
        return raw, None
    try:
        return match.group("title").strip(), int(match.group("year"))
    except (TypeError, ValueError):
        return match.group("title").strip(), None


def _row_title(row: dict[str, Any]) -> str:
    return str(row.get("title") or row.get("name") or "").strip()


def _confident_overseerr_hits(
    results: list[dict[str, Any]],
    *,
    query: str,
    media_type: str | None = None,
) -> list[dict[str, Any]]:
    """Keep only non-fallback hits whose title clearly matches the ask."""
    asked, year = _split_query_year(query)
    seed = asked or query
    out: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        if row.get("matched") == "fallback":
            continue
        mt = str(row.get("mediaType") or "").lower()
        if mt == "person" or _is_person_result(row):
            continue
        if media_type in {"movie", "tv"} and mt and mt != media_type:
            continue
        title = _row_title(row)
        if not title_seed_matches(seed, title):
            continue
        out.append(row)
    if year is not None:
        year_hits = []
        for row in out:
            try:
                row_year = int(row.get("year")) if row.get("year") not in (None, "") else None
            except (TypeError, ValueError):
                row_year = None
            if row_year == year:
                year_hits.append(row)
        if year_hits:
            return year_hits
    return out


def _indistinguishable_overseerr_hits(hits: list[dict[str, Any]]) -> bool:
    if len(hits) <= 1:
        return True
    labels = {
        (
            " ".join(_normalize_title_tokens(_row_title(h))),
            str(h.get("year") or ""),
            str(h.get("mediaType") or ""),
        )
        for h in hits
    }
    return len(labels) == 1


def _poster_path(item: dict[str, Any]) -> str | None:
    from hearth.tools.media_art import enrich_media_hit, sanitize_poster_path

    path = sanitize_poster_path(item.get("posterPath") or item.get("poster_path"))
    if path:
        return path
    remote = item.get("remotePoster")
    if isinstance(remote, str):
        path = sanitize_poster_path(remote)
        if path:
            return path
    for img in item.get("images") or []:
        if not isinstance(img, dict):
            continue
        cover = str(img.get("coverType") or "").lower()
        if cover and cover not in {"poster", "primary"}:
            continue
        candidate = img.get("remoteUrl") or img.get("url")
        path = sanitize_poster_path(candidate) if candidate else None
        if path:
            return path
    tmdb = item.get("tmdbId") or item.get("id")
    enriched = enrich_media_hit({"tmdbId": tmdb, "posterPath": None})
    return enriched.get("posterPath")


def _library_id(item: dict[str, Any]) -> int | None:
    """Radarr/Sonarr assign a numeric id only after the title is in the library."""
    raw = item.get("id")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    # Lookup payloads sometimes echo tmdb/tvdb into id; real library ids are present
    # alongside tmdbId/tvdbId as a separate field. Prefer explicit hasFile / path.
    if item.get("hasFile") or item.get("path") or item.get("movieFile") or item.get("statistics"):
        return value
    if item.get("tmdbId") is not None and value == item.get("tmdbId"):
        return None
    if item.get("tvdbId") is not None and value == item.get("tvdbId"):
        return None
    # If both catalog id and a distinct library id exist, keep the library id.
    if item.get("tmdbId") is not None or item.get("tvdbId") is not None:
        return value
    return None


def _in_library(item: dict[str, Any]) -> bool:
    if item.get("hasFile") is True:
        return True
    if item.get("path") and _library_id(item) is not None:
        return True
    if item.get("queued") is True or item.get("requested") is True:
        return True
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    # Overseerr mediaInfo status: 3=available, 4=partial, 5=processing, …
    status = media.get("status")
    try:
        if status is not None and int(status) >= 3:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _summarize_series(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdbId": item.get("tvdbId"),
        "tmdbId": item.get("tmdbId"),
        "imdbId": item.get("imdbId"),
        "libraryId": _library_id(item),
        "inLibrary": _in_library(item) or _library_id(item) is not None,
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
        "posterPath": _poster_path(item),
        "mediaType": "tv",
    }
    if item.get("matched"):
        out["matched"] = item.get("matched")
    return out


def _summarize_movie(item: dict[str, Any]) -> dict[str, Any]:
    tmdb = item.get("tmdbId")
    if tmdb is None and item.get("id") is not None and not _library_id(item):
        tmdb = item.get("id")
    out = {
        "title": item.get("title"),
        "year": item.get("year"),
        "tmdbId": tmdb,
        "imdbId": item.get("imdbId"),
        "libraryId": _library_id(item),
        "inLibrary": _in_library(item) or _library_id(item) is not None,
        "hasFile": bool(item.get("hasFile")),
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
        "posterPath": _poster_path(item),
        "mediaType": "movie",
    }
    if item.get("matched"):
        out["matched"] = item.get("matched")
    return out


def _row_media_type(item: dict[str, Any]) -> str:
    """Normalize Overseerr/TMDB media type (camelCase or snake_case)."""
    raw = item.get("mediaType") if item.get("mediaType") not in (None, "") else item.get(
        "media_type"
    )
    return str(raw or "").strip().lower()


def _is_person_result(item: dict[str, Any]) -> bool:
    """True for Overseerr multi-search person hits.

    Overseerr maps TMDB multi-search people with ``mediaType: person`` and
    ``knownFor``. Some payloads omit mediaType — treat name + knownFor as person
    so title-only filters cannot silently drop them.
    """
    if not isinstance(item, dict):
        return False
    if _row_media_type(item) == "person":
        return True
    known = item.get("knownFor")
    if known is None:
        known = item.get("known_for")
    name = str(item.get("name") or "").strip()
    # Person rows have a name and knownFor; movie/tv use title/name + release dates.
    if name and isinstance(known, list):
        # Avoid mistaking a titled movie that somehow carries an empty knownFor.
        if item.get("title") and _row_media_type(item) in {"movie", "tv"}:
            return False
        return True
    return False


def _summarize_overseerr(item: dict[str, Any]) -> dict[str, Any]:
    year = item.get("year")
    if not year:
        date = item.get("releaseDate") or item.get("firstAirDate") or ""
        year = str(date)[:4] or None
    media_id = item.get("id") or item.get("mediaId")
    media_type = _row_media_type(item)
    if not media_type and _is_person_result(item):
        media_type = "person"
    name = item.get("name")
    title = item.get("title") or name
    out = {
        "title": title,
        "name": name or (title if media_type == "person" else None),
        "year": year,
        "mediaType": media_type or item.get("mediaType"),
        "mediaId": media_id,
        "tmdbId": media_id,
        "imdbId": item.get("imdbId"),
        "inLibrary": _in_library(item),
        "overview": (item.get("overview") or item.get("summary") or "")[:180],
        "posterPath": _poster_path(item),
        "popularity": item.get("popularity"),
    }
    known = item.get("knownFor")
    if known is None:
        known = item.get("known_for")
    if isinstance(known, list):
        out["knownFor"] = known
    if item.get("profilePath") or item.get("profile_path"):
        out["profilePath"] = item.get("profilePath") or item.get("profile_path")
    if item.get("matched"):
        out["matched"] = item.get("matched")
    return out



# Statuses where a grab is still in flight — never report 100% yet.
_IN_FLIGHT_STATUSES = frozenset(
    {"queued", "downloading", "paused", "warning", "stalled", "importing", "unknown"}
)


def queue_percent(size: Any, sizeleft: Any) -> float | None:
    """Percent complete from real *arr bytes only (0–100, one decimal).

    Uses ``(size - sizeleft) / size * 100`` when ``size > 0`` and ``sizeleft``
    is numeric. Missing/invalid size or sizeleft → ``None`` (never invent 100
    from size=0 / sizeleft=0).
    """
    try:
        if size is None or sizeleft is None:
            return None
        total = float(size)
        left = float(sizeleft)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    done = ((total - left) / total) * 100.0
    return round(max(0.0, min(100.0, done)), 1)


def coerce_api_percent(value: Any) -> float | None:
    """Normalize a raw API percent-like value to 0–100.

    Values in ``[0, 1]`` are treated as fractions and scaled ×100. Values
    above 1 are treated as already on a 0–100 scale.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    n = float(value)
    if n < 0:
        return None
    if n <= 1.0:
        n *= 100.0
    return round(min(100.0, n), 1)


def clamp_download_percent(percent: float | None, status: str | None) -> float | None:
    """Clamp in-flight downloads to 0–99; allow 100 only when completed."""
    if percent is None:
        return None
    try:
        n = float(percent)
    except (TypeError, ValueError):
        return None
    n = max(0.0, min(100.0, n))
    state = (status or "").strip().lower()
    if state in _IN_FLIGHT_STATUSES:
        return round(min(99.0, n), 1)
    return round(n, 1)


def resolve_queue_percent(
    item: dict[str, Any],
    *,
    status: str | None = None,
) -> float | None:
    """Best-effort queue percent: bytes first, then scaled API field, then clamp."""
    size = item.get("size")
    sizeleft = item.get("sizeleft") if "sizeleft" in item else item.get("sizeLeft")
    percent = queue_percent(size, sizeleft)
    if percent is None:
        raw = item.get("percent")
        if raw is None:
            raw = item.get("progress")
        percent = coerce_api_percent(raw)
    return clamp_download_percent(percent, status)


def _status_messages_text(item: dict[str, Any]) -> str:
    messages = item.get("statusMessages") or []
    bits: list[str] = []
    for row in messages:
        if isinstance(row, dict):
            bits.append(str(row.get("title") or ""))
            for msg in row.get("messages") or []:
                bits.append(str(msg))
        else:
            bits.append(str(row))
    return " ".join(bits).lower()


def normalize_download_status(item: dict[str, Any]) -> str:
    """Map *arr queue fields → queued/downloading/paused/importing/stalled/completed/failed/unknown."""
    status = str(item.get("status") or "").strip().lower()
    state = str(item.get("trackedDownloadState") or "").strip().lower()
    tracked = str(item.get("trackedDownloadStatus") or "").strip().lower()
    messages = _status_messages_text(item)

    if state in {"failed", "failedpending"} or status == "failed":
        return "failed"
    if status == "paused" or state == "paused":
        return "paused"
    if state in {"importpending", "importing"}:
        return "importing"
    if state == "imported" or (status == "completed" and state in {"", "downloaded"}):
        return "completed"
    if "stall" in messages or (tracked == "warning" and "stall" in (status + state + messages)):
        return "stalled"
    if status in {"queued", "delay"} or state in {"queued"}:
        return "queued"
    if status == "downloading" or state == "downloading":
        if "stall" in messages:
            return "stalled"
        return "downloading"
    if status == "completed":
        return "completed"
    if status in DOWNLOAD_STATUSES:
        return status
    return "unknown"


def _download_title(item: dict[str, Any]) -> str:
    title = item.get("title")
    if title:
        return str(title)
    movie = item.get("movie") if isinstance(item.get("movie"), dict) else {}
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    episode = item.get("episode") if isinstance(item.get("episode"), dict) else {}
    for candidate in (
        movie.get("title"),
        series.get("title"),
        episode.get("title"),
    ):
        if candidate:
            return str(candidate)
    return ""


def _quality_name(item: dict[str, Any]) -> str | None:
    quality = item.get("quality")
    if not isinstance(quality, dict):
        return str(quality) if quality else None
    inner = quality.get("quality")
    if isinstance(inner, dict) and inner.get("name"):
        return str(inner["name"])
    if quality.get("name"):
        return str(quality["name"])
    return None


def _queue_media_ids(item: dict[str, Any], *, service: str) -> dict[str, Any]:
    """Stable *arr ids needed for release search / grab (never sent to the browser as secrets)."""
    movie = item.get("movie") if isinstance(item.get("movie"), dict) else {}
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    episode = item.get("episode") if isinstance(item.get("episode"), dict) else {}
    out: dict[str, Any] = {}
    if service == "radarr":
        movie_id = item.get("movieId") or movie.get("id")
        if movie_id is not None:
            try:
                out["movieId"] = int(movie_id)
            except (TypeError, ValueError):
                pass
        tmdb = movie.get("tmdbId") or item.get("tmdbId")
        if tmdb is not None:
            try:
                out["tmdbId"] = int(tmdb)
            except (TypeError, ValueError):
                pass
    else:
        series_id = item.get("seriesId") or series.get("id")
        episode_id = item.get("episodeId") or episode.get("id")
        if series_id is not None:
            try:
                out["seriesId"] = int(series_id)
            except (TypeError, ValueError):
                pass
        if episode_id is not None:
            try:
                out["episodeId"] = int(episode_id)
            except (TypeError, ValueError):
                pass
    return out


def summarize_queue_item(item: dict[str, Any], *, service: str) -> dict[str, Any]:
    size = item.get("size")
    sizeleft = item.get("sizeleft") if "sizeleft" in item else item.get("sizeLeft")
    title = _download_title(item)
    status = normalize_download_status(item)
    percent = resolve_queue_percent(item, status=status)
    queue_id = item.get("id")
    try:
        queue_id_i = int(queue_id) if queue_id is not None else None
    except (TypeError, ValueError):
        queue_id_i = None
    out = {
        "title": title or None,
        "status": status,
        "percent": percent,
        "size": size,
        "sizeleft": sizeleft,
        "timeleft": item.get("timeleft") or item.get("timeLeft"),
        "indexer": item.get("indexer"),
        "quality": _quality_name(item),
        "downloadClient": item.get("downloadClient"),
        "errorMessage": item.get("errorMessage"),
        "downloadId": item.get("downloadId") or item.get("downloadClientId"),
        "queueId": queue_id_i,
        "service": service,
        "unhealthy": download_is_unhealthy(item),
    }
    out.update(_queue_media_ids(item, service=service))
    return out


# Statuses that warrant an alternate-source retry (real *arr fields only).
_UNHEALTHY_STATUSES = frozenset({"failed", "stalled"})


def download_status_of(item: dict[str, Any]) -> str:
    """Status from a raw *arr queue row or a summarized row."""
    if any(
        key in item
        for key in ("trackedDownloadState", "trackedDownloadStatus", "statusMessages")
    ):
        return normalize_download_status(item)
    return str(item.get("status") or "unknown").strip().lower() or "unknown"


def download_is_unhealthy(item: dict[str, Any]) -> bool:
    """True when *arr reports failed/stalled (or warning+stall messages)."""
    return download_status_of(item) in _UNHEALTHY_STATUSES


def retry_media_key(item: dict[str, Any], *, service: str) -> str:
    """Stable key for retry caps — same title/episode across releases."""
    ids = _queue_media_ids(item, service=service)
    if service == "radarr":
        if ids.get("movieId") is not None:
            return f"radarr:movie:{ids['movieId']}"
        if ids.get("tmdbId") is not None:
            return f"radarr:tmdb:{ids['tmdbId']}"
    else:
        if ids.get("episodeId") is not None:
            return f"sonarr:episode:{ids['episodeId']}"
        if ids.get("seriesId") is not None:
            return f"sonarr:series:{ids['seriesId']}"
    title = _download_title(item).strip().lower() or "unknown"
    return f"{service}:title:{title}"


def release_identity(release: dict[str, Any]) -> str:
    """Compare releases so we do not re-grab the same dead torrent."""
    for key in ("guid", "downloadUrl", "magnetUrl", "infoUrl"):
        value = release.get(key)
        if value:
            return str(value).strip().lower()
    title = str(release.get("title") or release.get("releaseTitle") or "").strip().lower()
    indexer = str(release.get("indexer") or release.get("indexerId") or "").strip().lower()
    return f"{indexer}|{title}"


# Radarr interactive search marks other torrents as rejected when the movie
# already has a file / meets cutoff. Those are still grab-able for keep-both /
# switch paths — only hard quality / safety rejections should hide a row.
_SOFT_LIBRARY_REJECTION = re.compile(
    r"already\s+(?:downloaded|have|in(?:\s+the)?\s+library)|"
    r"existing\s+file|"
    r"(?:quality\s+)?cutoff\s+already\s+met|"
    r"cutoff\s+met|"
    r"not\s+wanted\s+because\s+already|"
    r"equal\s+or\s+higher\s+(?:preference|quality)|"
    r"already\s+being\s+downloaded|"
    r"meets?\s+cut[\s-]?off",
    re.I,
)
_HARD_RELEASE_REJECTION = re.compile(
    r"password|"
    r"encrypt|"
    r"\bsample\b|"
    r"\bmissing\b|"
    r"quality\s+rejected|"
    r"unknown\s+movie|"
    r"does\s+not\s+meet|"
    r"must\s+(?:contain|not)|"
    r"custom\s+format|"
    r"\bbanned\b|"
    r"unavailable|"
    r"not\s+a\s+movie|"
    r"invalid|"
    r"failed\s+to\s+parse",
    re.I,
)


def _rejection_messages(release: dict[str, Any]) -> list[str]:
    raw = release.get("rejections") or []
    if not isinstance(raw, list):
        return [str(raw)] if raw else []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(
                item.get("reason") or item.get("message") or item.get("rejection") or item
            ).strip()
        else:
            text = str(item).strip()
        if text:
            out.append(text)
    return out


def _is_soft_library_rejection(message: str) -> bool:
    return bool(message and _SOFT_LIBRARY_REJECTION.search(message))


def release_is_hard_rejected(release: dict[str, Any]) -> bool:
    """True when a release should be hidden from alternate-offer menus.

    Soft library-state rejections (already downloaded / cutoff met / existing
    file) are NOT hard — Radarr still returns grab-able guids for them.
    """
    msgs = _rejection_messages(release)
    if not msgs:
        # Bare rejected=true with no detail — treat as unusable.
        return bool(release.get("rejected") is True)
    for msg in msgs:
        if _HARD_RELEASE_REJECTION.search(msg):
            return True
        if not _is_soft_library_rejection(msg):
            # Unknown non-soft rejection — keep conservative and skip.
            return True
    return False


def _normalize_release_blob(text: str) -> str:
    raw = (text or "").strip().lower()
    raw = re.sub(r"[\\/]+", " ", raw)
    raw = re.sub(r"\.(mkv|mp4|avi|m4v|ts|m2ts|iso)$", "", raw)
    raw = re.sub(r"[.\-_]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def release_matches_blocked(release: dict[str, Any], blocked: str) -> bool:
    """True when this row is the current / blocked library release."""
    blocked_norm = (blocked or "").strip().lower()
    if not blocked_norm:
        return False
    ident = release_identity(release)
    if ident and (
        ident == blocked_norm
        or blocked_norm in ident
        or ident in blocked_norm
    ):
        return True
    blocked_blob = _normalize_release_blob(blocked_norm)
    title_blob = _normalize_release_blob(
        str(release.get("title") or release.get("releaseTitle") or "")
    )
    if blocked_blob and title_blob and (
        blocked_blob == title_blob
        or blocked_blob in title_blob
        or title_blob in blocked_blob
    ):
        return True
    return False


def library_file_block_token(movie_raw: dict[str, Any] | None) -> str:
    """Token used to exclude the currently imported release from offer menus."""
    if not isinstance(movie_raw, dict):
        return ""
    mf = movie_raw.get("movieFile") if isinstance(movie_raw.get("movieFile"), dict) else {}
    for key in ("relativePath", "path", "sceneName", "originalFilePath"):
        value = mf.get(key)
        if value:
            return str(value)
    return ""


def speak_retry(
    *,
    title: str,
    ok: bool,
    reason: str = "",
    indexer: str = "",
    attempt: int = 0,
    max_attempts: int = 0,
    mock: bool = False,
) -> str:
    """User-facing copy for Telegram + house tools (no fake percents)."""
    label = (title or "That download").strip() or "That download"
    mock_bit = " (mock)" if mock else ""
    if ok and reason in {"retried", "grabbed", "switched", "kept_both"}:
        source = f" via {indexer}" if indexer else ""
        cap = f" (attempt {attempt}/{max_attempts})" if attempt and max_attempts else ""
        if reason == "kept_both":
            return (
                f"Downloading an extra release of {label}{source}{mock_bit} — "
                f"keeping your current library file."
            )
        if reason == "switched":
            return f"Grabbing a different release of {label}{source}{mock_bit}."
        return (
            f"{label} stalled — trying another source{source}{cap}{mock_bit}."
        )
    if reason == "needs_pick":
        return (
            f"{label} is in the library without a usable file — "
            f"pick a smaller release to grab{mock_bit}."
        )
    if reason == "needs_pick_large":
        return (
            f"{label} has a huge / hard-to-play file — "
            f"pick a smaller release to replace it{mock_bit}."
        )
    if reason == "needs_pick_keep":
        return (
            f"{label} is already in the library — "
            f"pick another release to download without deleting the current file{mock_bit}."
        )
    if reason == "exhausted":
        return (
            f"{label} failed — ran out of alternate sources "
            f"after {max_attempts or attempt or 'several'} tries{mock_bit}."
        )
    if reason == "no_alternate":
        return (
            f"{label} — no other grab-able release found on the indexers{mock_bit}."
        )
    if reason == "not_found":
        return f"{label} is not in the download queue{mock_bit}."
    if reason == "not_in_library":
        return f"{label} is not in the Radarr library{mock_bit}."
    if reason == "healthy":
        return (
            f"{label} is still downloading — say you want another source "
            f"if this one is stuck{mock_bit}."
        )
    if reason == "confirm_required":
        return (
            f"Confirm to grab that release of {label}"
            f"{(' via ' + indexer) if indexer else ''}{mock_bit}."
        )
    if reason == "error":
        return f"Couldn't retry {label}{mock_bit}."
    if ok:
        return f"Retrying {label} from another source{mock_bit}."
    return f"Couldn't retry {label}{mock_bit}."


def release_token(release: dict[str, Any]) -> str:
    """Short stable token for Telegram callback_data (not a secret)."""
    import hashlib

    ident = release_identity(release) or str(release.get("title") or "x")
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()
    return digest[:10]


def _release_size_bytes(release: dict[str, Any]) -> int | None:
    for key in ("size", "sizebytes", "sizeBytes"):
        raw = release.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _release_quality_label(release: dict[str, Any]) -> str:
    quality = release.get("quality")
    if isinstance(quality, dict):
        inner = quality.get("quality")
        if isinstance(inner, dict) and inner.get("name"):
            return str(inner["name"])
        if quality.get("name"):
            return str(quality["name"])
    title = str(release.get("title") or release.get("releaseTitle") or "")
    return title


def _format_size_gb(size: int | None) -> str | None:
    if size is None or size <= 0:
        return None
    gb = size / 1_000_000_000
    if gb >= 10:
        return f"{gb:.0f} GB"
    return f"{gb:.1f} GB"


def summarize_release(
    release: dict[str, Any],
    *,
    movie: dict[str, Any] | None = None,
    service: str = "radarr",
) -> dict[str, Any]:
    """User-facing release row — ids stay server-side; token is for HITL."""
    movie = movie or {}
    size = _release_size_bytes(release)
    quality = _release_quality_label(release)
    title = str(release.get("title") or release.get("releaseTitle") or "Release")
    indexer = str(release.get("indexer") or release.get("indexerId") or "")
    movie_id = release.get("movieId") or movie.get("id") or movie.get("libraryId")
    tmdb = movie.get("tmdbId") or release.get("tmdbId")
    try:
        movie_id_i = int(movie_id) if movie_id is not None else None
    except (TypeError, ValueError):
        movie_id_i = None
    try:
        tmdb_i = int(tmdb) if tmdb is not None else None
    except (TypeError, ValueError):
        tmdb_i = None
    token = release_token(release)
    size_label = _format_size_gb(size)
    label_bits = [title]
    if size_label:
        label_bits.append(size_label)
    out: dict[str, Any] = {
        "title": title,
        "label": " · ".join(label_bits),
        "indexer": indexer or None,
        "quality": quality or None,
        "size": size,
        "sizeLabel": size_label,
        "releaseToken": token,
        "guid": release.get("guid"),
        "indexerId": release.get("indexerId"),
        "movieId": movie_id_i,
        "tmdbId": tmdb_i,
        "mediaType": "movie" if service == "radarr" else "tv",
        "approved": bool(release.get("approved")),
        # Movie title for subject memory (not the release torrent name).
        "movieTitle": str(movie.get("title") or "") or None,
        "year": movie.get("year"),
    }
    return out


def _playability_score(release: dict[str, Any], *, prefer_smaller: bool) -> int:
    """Rank grab-able releases; prefer smaller / 1080p when size is the complaint."""
    score = 0
    if release.get("approved") is True:
        score += 40
    try:
        score += int(release.get("score") or 0)
    except (TypeError, ValueError):
        pass
    blob = (
        f"{release.get('title') or ''} {_release_quality_label(release)}"
    ).lower()
    size = _release_size_bytes(release)
    if prefer_smaller:
        if any(tag in blob for tag in ("2160", "4k", "uhd")):
            score -= 90
        if "remux" in blob:
            score -= 70
        if "1080" in blob:
            score += 45
        if "720" in blob:
            score += 35
        if size is not None:
            if size <= 4_000_000_000:
                score += 35
            elif size <= 10_000_000_000:
                score += 25
            elif size <= 20_000_000_000:
                score += 5
            else:
                score -= 50
    else:
        if size is not None:
            # Default *arr preference: higher score already applied; slight size nudge.
            score += max(0, 20 - size // 5_000_000_000)
    return score


def format_release_offer(
    movie_title: str,
    releases: list[dict[str, Any]],
    *,
    reason: str = "needs_pick",
) -> str:
    """Numbered alternate-release menu for Telegram / voice."""
    label = (movie_title or "That title").strip() or "That title"
    if reason == "needs_pick_large":
        header = (
            f"{label} is too big / won't play well. "
            f"Pick a smaller release (this replaces the current file):"
        )
    elif reason == "needs_pick_keep":
        header = (
            f"{label} is already in the library. "
            f"Pick another release to download — the current file stays:"
        )
    else:
        header = (
            f"{label} is in the library but has no usable file. "
            f"Pick a release to grab:"
        )
    lines = [header]
    for idx, row in enumerate(releases[:4], start=1):
        bit = str(row.get("label") or row.get("title") or f"Release {idx}")
        indexer = row.get("indexer")
        if indexer:
            bit = f"{bit} ({indexer})"
        lines.append(f"{idx}. {bit}")
    lines.append("Tap Get for the one you want — I won't grab until you confirm.")
    return "\n".join(lines)


def want_keep_existing(text: str) -> bool:
    """True when the user asks for an extra download without replacing the library file."""
    raw = (text or "").strip()
    if not raw:
        return False
    # Explicit keep always wins over too-big / replace wording.
    if re.search(
        r"\b(?:don'?t|do\s+not)\s+delete\b"
        r"|\bkeep\s+(?:the\s+)?(?:old|current|existing|both)\b"
        r"|\bwithout\s+(?:deleting|replacing)\b",
        raw,
        re.I,
    ):
        return True
    # Too-big / won't-play / replace → switch/delete path (not keep-both),
    # unless they also said keep (handled above).
    if re.search(
        r"\btoo\s+big\b|\bwon'?t\s+play\b|\bdoesn'?t\s+play\b|\breplace\b|"
        r"\bte\s+groot\b|\bspeelt\s+niet\b",
        raw,
        re.I,
    ):
        return False
    if re.search(
        r"\balready\s+(?:there|in\s+(?:the\s+)?library)\b"
        r"|\b(?:find|get|grab|download)\s+another\s+(?:download|copy|one|release|version)\b"
        r"|\b(?:get|grab|download)\s+(?:a\s+)?new\s+version\b"
        r"|\b(?:a\s+)?new\s+version\b"
        r"|\banother\s+(?:copy|version|release|download)\b"
        r"|\bextra\s+(?:download|copy|release)\b",
        raw,
        re.I,
    ):
        return True
    return False


def title_matches_download(item: dict[str, Any], title: str) -> bool:
    needle = (title or "").strip().lower()
    if not needle:
        return True
    haystacks = [_download_title(item).lower()]
    movie = item.get("movie") if isinstance(item.get("movie"), dict) else {}
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    for extra in (movie.get("title"), series.get("title"), item.get("title")):
        if extra:
            haystacks.append(str(extra).lower())
    return any(needle in hay for hay in haystacks if hay)


def speak_queue(
    downloads: list[dict[str, Any]],
    *,
    title: str = "",
    service: str = "radarr",
    mock: bool = False,
) -> str:
    label = "Radarr" if service == "radarr" else "Sonarr"
    mock_bit = " (mock)" if mock else ""
    needle = (title or "").strip()
    if needle and not downloads:
        return f"{needle} is not downloading in {label}{mock_bit}."
    if not downloads:
        return f"Nothing downloading in {label}{mock_bit}."
    parts: list[str] = []
    for row in downloads[:6]:
        name = row.get("title") or "Untitled"
        status = row.get("status") or "unknown"
        percent = row.get("percent")
        bit = f"{name} is {status}"
        if percent is not None:
            bit += f", {percent:g}% complete"
        timeleft = row.get("timeleft")
        if timeleft and status == "downloading":
            bit += f", about {timeleft} left"
        parts.append(bit)
    joined = "; ".join(parts)
    if needle and len(downloads) == 1:
        return f"{joined}{mock_bit}."
    return f"{label}{mock_bit}: {joined}."


class StarrClient:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._client: httpx.AsyncClient | None = None
        # Cap alternate-source retries per movie/episode (in-process).
        self._retry_counts: dict[str, int] = {}

    def reset_retry_counts(self) -> None:
        self._retry_counts.clear()

    def retry_count(self, key: str) -> int:
        return int(self._retry_counts.get(key, 0))

    def _bump_retry(self, key: str) -> int:
        nxt = self.retry_count(key) + 1
        self._retry_counts[key] = nxt
        return nxt

    @property
    def max_retries(self) -> int:
        try:
            return max(0, int(settings.download_max_retries))
        except (TypeError, ValueError):
            return 3

    @property
    def live(self) -> bool:
        if self.kind == "radarr":
            return settings.radarr_configured
        return settings.sonarr_configured

    @property
    def base_url(self) -> str:
        if self.kind == "radarr":
            return settings.radarr_url.rstrip("/")
        return settings.sonarr_url.rstrip("/")

    @property
    def api_key(self) -> str:
        if self.kind == "radarr":
            return settings.radarr_api_key
        return settings.sonarr_api_key

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
                timeout=12.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str) -> dict[str, Any]:
        query = (query or "").strip()
        if not self.live:
            hits = (
                pipeline.search_radarr(query)
                if self.kind == "radarr"
                else pipeline.search_sonarr(query)
            )
            summarized = [
                _summarize_movie(h) if self.kind == "radarr" else _summarize_series(h) for h in hits
            ]
            return {"mode": "mock", "service": self.kind, "query": query, "results": summarized}
        path = "/api/v3/movie/lookup" if self.kind == "radarr" else "/api/v3/series/lookup"
        client = await self._http()
        try:
            response = await client.get(path, params={"term": query})
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else []
            summarized = [
                _summarize_movie(h) if self.kind == "radarr" else _summarize_series(h)
                for h in rows[:8]
            ]
            return {"mode": "live", "service": self.kind, "query": query, "results": summarized}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                hits = (
                    pipeline.search_radarr(query)
                    if self.kind == "radarr"
                    else pipeline.search_sonarr(query)
                )
                summarized = [
                    _summarize_movie(h) if self.kind == "radarr" else _summarize_series(h)
                    for h in hits
                ]
                return {
                    "mode": "mock",
                    "service": self.kind,
                    "query": query,
                    "error": str(exc),
                    "results": summarized,
                }
            raise

    async def add(self, query: str = "", tmdb_id: int | None = None, tvdb_id: int | None = None) -> dict[str, Any]:
        query = (query or "").strip()
        if not self.live:
            if self.kind == "radarr":
                hits = pipeline.search_radarr(query or "dune")
                if tmdb_id:
                    hits = [h for h in hits if h.get("tmdbId") == tmdb_id] or hits
                item = pipeline.add_radarr(hits[0] if hits else {"title": query or "unknown"})
                return {"mode": "mock", "service": "radarr", "added": _summarize_movie(item)}
            hits = pipeline.search_sonarr(query or "severance")
            if tvdb_id:
                hits = [h for h in hits if h.get("tvdbId") == tvdb_id] or hits
            item = pipeline.add_sonarr(hits[0] if hits else {"title": query or "unknown"})
            return {"mode": "mock", "service": "sonarr", "added": _summarize_series(item)}

        lookup = await self.search(query or str(tmdb_id or tvdb_id or ""))
        results = lookup.get("results") or []
        if not results:
            return {"ok": False, "service": self.kind, "error": f"no {self.kind} match for {query}"}
        pick = results[0]
        client = await self._http()
        try:
            root = await self._root_folder(client)
            quality = await self._quality_profile_id(client)
            if self.kind == "radarr":
                body = {
                    "title": pick.get("title"),
                    "tmdbId": pick.get("tmdbId") or tmdb_id,
                    "qualityProfileId": quality,
                    "rootFolderPath": root,
                    "monitored": True,
                    "addOptions": {"searchForMovie": True},
                }
                response = await client.post("/api/v3/movie", json=body)
            else:
                body = {
                    "title": pick.get("title"),
                    "tvdbId": pick.get("tvdbId") or tvdb_id,
                    "qualityProfileId": quality,
                    "rootFolderPath": root,
                    "monitored": True,
                    "seasonFolder": True,
                    "addOptions": {"searchForMissingEpisodes": True},
                }
                response = await client.post("/api/v3/series", json=body)
            response.raise_for_status()
            return {
                "mode": "live",
                "service": self.kind,
                "added": pick,
                "status_code": response.status_code,
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                if self.kind == "radarr":
                    item = pipeline.add_radarr(pick)
                    return {
                        "mode": "mock",
                        "service": "radarr",
                        "error": str(exc),
                        "added": _summarize_movie(item),
                    }
                item = pipeline.add_sonarr(pick)
                return {
                    "mode": "mock",
                    "service": "sonarr",
                    "error": str(exc),
                    "added": _summarize_series(item),
                }
            raise

    async def _root_folder(self, client: httpx.AsyncClient) -> str:
        response = await client.get("/api/v3/rootFolder")
        response.raise_for_status()
        rows = response.json() or []
        if not rows:
            raise RuntimeError(f"{self.kind} has no root folder")
        return rows[0].get("path") or rows[0].get("Path")

    async def _quality_profile_id(self, client: httpx.AsyncClient) -> int:
        response = await client.get("/api/v3/qualityProfile")
        response.raise_for_status()
        rows = response.json() or []
        if not rows:
            raise RuntimeError(f"{self.kind} has no quality profile")
        return int(rows[0]["id"])

    async def queue(self, title: str = "") -> dict[str, Any]:
        """Active download queue with status + percent complete (optional title filter)."""
        title = (title or "").strip()
        raw_rows = await self.queue_raw(title)
        mode = str(raw_rows.get("mode") or ("live" if self.live else "mock"))
        downloads = [
            summarize_queue_item(row, service=self.kind)
            for row in raw_rows.get("records") or []
        ]
        out: dict[str, Any] = {
            "mode": mode,
            "service": self.kind,
            "query": title or None,
            "downloads": downloads,
            "found": bool(downloads) if title else None,
            "speak": speak_queue(
                downloads, title=title, service=self.kind, mock=mode == "mock"
            ),
        }
        if raw_rows.get("error"):
            out["error"] = raw_rows["error"]
        return out

    async def queue_raw(self, title: str = "") -> dict[str, Any]:
        """Raw *arr queue records (ids intact) for retry / blocklist."""
        title = (title or "").strip()
        if not self.live:
            raw = (
                pipeline.list_radarr_downloads(title)
                if self.kind == "radarr"
                else pipeline.list_sonarr_downloads(title)
            )
            return {"mode": "mock", "service": self.kind, "records": raw}

        client = await self._http()
        params: dict[str, Any] = {"page": 1, "pageSize": 100}
        if self.kind == "radarr":
            params["includeUnknownMovieItems"] = "true"
            params["includeMovie"] = "true"
        else:
            params["includeUnknownSeriesItems"] = "true"
            params["includeSeries"] = "true"
            params["includeEpisode"] = "true"
        try:
            response = await client.get("/api/v3/queue", params=params)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                rows = payload.get("records") or []
            elif isinstance(payload, list):
                rows = payload
            else:
                rows = []
            if title:
                rows = [row for row in rows if title_matches_download(row, title)]
            return {"mode": "live", "service": self.kind, "records": rows}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                raw = (
                    pipeline.list_radarr_downloads(title)
                    if self.kind == "radarr"
                    else pipeline.list_sonarr_downloads(title)
                )
                return {
                    "mode": "mock",
                    "service": self.kind,
                    "error": str(exc),
                    "records": raw,
                }
            raise

    def _blocked_identity(self, item: dict[str, Any]) -> str:
        """Identity of the bad queue release so we skip it on re-grab."""
        for key in ("downloadId", "downloadClientId"):
            value = item.get(key)
            if value:
                return str(value).strip().lower()
        # Fall back to release title + indexer (queue rows often lack guid).
        title = str(item.get("title") or "").strip().lower()
        indexer = str(item.get("indexer") or "").strip().lower()
        return f"{indexer}|{title}"

    async def _blocklist_remove(self, queue_id: int) -> None:
        """Remove queue item, drop from client, blocklist — do not auto-redownload."""
        if not self.live:
            pipeline.blocklist_queue_item(self.kind, queue_id)
            return
        client = await self._http()
        response = await client.delete(
            f"/api/v3/queue/{int(queue_id)}",
            params={
                "removeFromClient": "true",
                "blocklist": "true",
                "skipRedownload": "true",
            },
        )
        response.raise_for_status()

    async def _detach_queue_keep_client(self, queue_id: int) -> None:
        """Stop Radarr from importing/replacing; leave the torrent in the download client.

        Radarr only tracks one movie file. Grabbing via POST /release would normally
        import and replace the library file. Removing the queue row with
        removeFromClient=false keeps the download as an extra copy on disk without
        deleting the existing library entry.
        """
        if not self.live:
            pipeline.detach_queue_item(self.kind, queue_id)
            return
        client = await self._http()
        response = await client.delete(
            f"/api/v3/queue/{int(queue_id)}",
            params={
                "removeFromClient": "false",
                "blocklist": "false",
                "skipRedownload": "true",
            },
        )
        response.raise_for_status()

    async def _queue_id_for_release(
        self,
        *,
        guid: str = "",
        movie_id: Any = None,
        indexer: str = "",
        release_title: str = "",
    ) -> int | None:
        """Best-effort match of a just-grabbed release in the *arr queue."""
        raw = await self.queue_raw("")
        records = list(raw.get("records") or [])
        guid_norm = (guid or "").strip().lower()
        indexer_norm = (indexer or "").strip().lower()
        title_norm = (release_title or "").strip().lower()
        try:
            movie_id_i = int(movie_id) if movie_id is not None else None
        except (TypeError, ValueError):
            movie_id_i = None

        def _score(row: dict[str, Any]) -> int:
            score = 0
            download_id = str(row.get("downloadId") or "").strip().lower()
            if guid_norm and download_id and (
                download_id == guid_norm or guid_norm in download_id or download_id in guid_norm
            ):
                score += 100
            ids = _queue_media_ids(row, service=self.kind)
            if movie_id_i is not None and ids.get("movieId") == movie_id_i:
                score += 40
            if indexer_norm and str(row.get("indexer") or "").strip().lower() == indexer_norm:
                score += 20
            row_title = str(row.get("title") or "").strip().lower()
            if title_norm and title_norm in row_title:
                score += 15
            return score

        ranked = sorted(
            (( _score(row), row) for row in records if isinstance(row, dict)),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] <= 0:
            return None
        try:
            return int(ranked[0][1].get("id"))
        except (TypeError, ValueError):
            return None

    async def _search_releases(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        ids = _queue_media_ids(item, service=self.kind)
        if not self.live:
            return pipeline.list_releases(self.kind, item)
        client = await self._http()
        params: dict[str, Any] = {}
        if self.kind == "radarr":
            movie_id = ids.get("movieId")
            if movie_id is None:
                return []
            params["movieId"] = movie_id
        else:
            episode_id = ids.get("episodeId")
            if episode_id is not None:
                params["episodeId"] = episode_id
            else:
                series_id = ids.get("seriesId")
                if series_id is None:
                    return []
                params["seriesId"] = series_id
        response = await client.get("/api/v3/release", params=params)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    async def _grab_release(self, release: dict[str, Any]) -> dict[str, Any]:
        if not self.live:
            return pipeline.grab_release(self.kind, release)
        client = await self._http()
        body = {
            "guid": release.get("guid"),
            "indexerId": release.get("indexerId"),
        }
        response = await client.post("/api/v3/release", json=body)
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {}
        return payload if isinstance(payload, dict) else {"ok": True}

    def _pick_alternate_release(
        self,
        releases: list[dict[str, Any]],
        *,
        blocked: str,
        prefer_smaller: bool = False,
    ) -> dict[str, Any] | None:
        """Best acceptable release that is not the blocklisted one."""
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in releases:
            if not isinstance(row, dict):
                continue
            # Soft library-state rejections stay offerable; hard ones skip.
            if release_is_hard_rejected(row):
                continue
            if release_matches_blocked(row, blocked):
                continue
            score = _playability_score(row, prefer_smaller=prefer_smaller)
            ranked.append((score, row))
        if not ranked:
            return None
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return ranked[0][1]

    def _rank_releases(
        self,
        releases: list[dict[str, Any]],
        *,
        blocked: str = "",
        prefer_smaller: bool = True,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        """Return top grab-able releases (raw *arr rows), ranked.

        Soft Radarr library-state rejections (already downloaded / cutoff met /
        existing file) are included — interactive search still returns grab-able
        guids for them. Hard rejections (passworded, encrypted, etc.) are skipped.
        """
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in releases:
            if not isinstance(row, dict):
                continue
            if release_is_hard_rejected(row):
                continue
            guid = row.get("guid")
            indexer_id = row.get("indexerId")
            if not guid or indexer_id in (None, ""):
                continue
            if release_matches_blocked(row, blocked):
                continue
            ranked.append((_playability_score(row, prefer_smaller=prefer_smaller), row))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in ranked[: max(1, limit)]]

    def _select_retry_target(
        self,
        records: list[dict[str, Any]],
        *,
        title: str,
        force: bool,
    ) -> dict[str, Any] | None:
        """Prefer unhealthy queue rows; with force, allow a healthy in-flight grab."""
        if not records:
            return None
        unhealthy = [row for row in records if download_is_unhealthy(row)]
        if unhealthy:
            return unhealthy[0]
        if force:
            # User asked for another source — retry the matching in-flight item.
            for row in records:
                status = download_status_of(row)
                if status in {"completed", "importing"}:
                    continue
                return row
        if title:
            # Title lookup with only healthy rows — do not auto-retry healthy.
            return None
        # Auto path with no title: first unhealthy only (already handled).
        return None

    async def retry_download(
        self,
        title: str = "",
        *,
        force: bool = False,
        reason: str = "",
        keep_existing: bool | None = None,
    ) -> dict[str, Any]:
        """Blocklist the bad release and grab an alternate for the SAME title.

        Does not delete the movie/series from the library. Does not re-POST
        Overseerr. Caps attempts per movie/episode via ``download_max_retries``.
        """
        title = (title or "").strip()
        max_attempts = self.max_retries
        mock = not self.live
        raw = await self.queue_raw(title)
        records = list(raw.get("records") or [])
        target = self._select_retry_target(records, title=title, force=force)
        display = (
            _download_title(target)
            if target
            else (title or ("Radarr" if self.kind == "radarr" else "Sonarr"))
        )

        if target is None:
            if title and records and not force:
                # Title is downloading fine — user must force for another source.
                return {
                    "ok": False,
                    "mode": "mock" if mock else "live",
                    "service": self.kind,
                    "query": title or None,
                    "reason": "healthy",
                    "title": display,
                    "downloads": [
                        summarize_queue_item(row, service=self.kind) for row in records
                    ],
                    "speak": speak_retry(
                        title=display, ok=False, reason="healthy", mock=mock
                    ),
                }
            # Not in the active queue — offer library alternate releases when the
            # title is monitored (missing file, too big, or keep-both extra copy).
            if title and self.kind == "radarr":
                switch = await self.list_alternate_releases(
                    title,
                    prefer_smaller=keep_existing is not True,
                    keep_existing=keep_existing,
                )
                if switch.get("ok") and switch.get("releases"):
                    return switch
                if switch.get("reason") in {"no_alternate", "not_in_library"}:
                    # Fall through to classic not_found when nothing to offer.
                    if switch.get("reason") == "no_alternate":
                        return switch
            return {
                "ok": False,
                "mode": "mock" if mock else "live",
                "service": self.kind,
                "query": title or None,
                "reason": "not_found",
                "title": display,
                "downloads": [],
                "speak": speak_retry(
                    title=display or title or "That download",
                    ok=False,
                    reason="not_found",
                    mock=mock,
                ),
            }

        key = retry_media_key(target, service=self.kind)
        used = self.retry_count(key)
        if used >= max_attempts:
            return {
                "ok": False,
                "mode": "mock" if mock else "live",
                "service": self.kind,
                "query": title or None,
                "reason": "exhausted",
                "title": display,
                "attempt": used,
                "max_attempts": max_attempts,
                "mediaKey": key,
                "downloads": [
                    summarize_queue_item(target, service=self.kind)
                ],
                "speak": speak_retry(
                    title=display,
                    ok=False,
                    reason="exhausted",
                    attempt=used,
                    max_attempts=max_attempts,
                    mock=mock,
                ),
            }

        queue_id = target.get("id")
        try:
            queue_id_i = int(queue_id) if queue_id is not None else None
        except (TypeError, ValueError):
            queue_id_i = None
        if queue_id_i is None:
            return {
                "ok": False,
                "mode": "mock" if mock else "live",
                "service": self.kind,
                "query": title or None,
                "reason": "error",
                "title": display,
                "error": "queue item missing id",
                "speak": speak_retry(title=display, ok=False, reason="error", mock=mock),
            }

        blocked = self._blocked_identity(target)
        old_indexer = str(target.get("indexer") or "")
        try:
            await self._blocklist_remove(queue_id_i)
            releases = await self._search_releases(target)
            pick = self._pick_alternate_release(releases, blocked=blocked)
            if pick is None:
                # Still count the attempt — we burned a try removing the bad grab.
                attempt = self._bump_retry(key)
                return {
                    "ok": False,
                    "mode": "mock" if mock else "live",
                    "service": self.kind,
                    "query": title or None,
                    "reason": "no_alternate",
                    "title": display,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "mediaKey": key,
                    "blockedIndexer": old_indexer or None,
                    "blocklisted": True,
                    "downloads": [],
                    "speak": speak_retry(
                        title=display,
                        ok=False,
                        reason="no_alternate",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        mock=mock,
                    ),
                }
            await self._grab_release(pick)
            attempt = self._bump_retry(key)
            new_indexer = str(pick.get("indexer") or pick.get("indexerId") or "")
            # Refresh queue summary after grab (mock updates in-place).
            refreshed = await self.queue(display if not title else title)
            return {
                "ok": True,
                "mode": "mock" if mock else "live",
                "service": self.kind,
                "query": title or None,
                "reason": "retried",
                "title": display,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "mediaKey": key,
                "blockedIndexer": old_indexer or None,
                "indexer": new_indexer or None,
                "blocklisted": True,
                "trigger": reason or ("user" if force else "auto"),
                "downloads": refreshed.get("downloads") or [],
                "speak": speak_retry(
                    title=display,
                    ok=True,
                    reason="retried",
                    indexer=new_indexer,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    mock=mock,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured and self.live:
                # Fall through to fixture path when live *arr is unreachable.
                try:
                    pipeline.blocklist_queue_item(self.kind, queue_id_i)
                    releases = pipeline.list_releases(self.kind, target)
                    pick = self._pick_alternate_release(releases, blocked=blocked)
                    if pick is None:
                        attempt = self._bump_retry(key)
                        return {
                            "ok": False,
                            "mode": "mock",
                            "service": self.kind,
                            "query": title or None,
                            "reason": "no_alternate",
                            "title": display,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "error": str(exc),
                            "speak": speak_retry(
                                title=display,
                                ok=False,
                                reason="no_alternate",
                                mock=True,
                            ),
                        }
                    pipeline.grab_release(self.kind, pick)
                    attempt = self._bump_retry(key)
                    new_indexer = str(pick.get("indexer") or "")
                    refreshed = await self.queue(display if not title else title)
                    return {
                        "ok": True,
                        "mode": "mock",
                        "service": self.kind,
                        "query": title or None,
                        "reason": "retried",
                        "title": display,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "indexer": new_indexer or None,
                        "error": str(exc),
                        "downloads": refreshed.get("downloads") or [],
                        "speak": speak_retry(
                            title=display,
                            ok=True,
                            reason="retried",
                            indexer=new_indexer,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            mock=True,
                        ),
                    }
                except Exception as mock_exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "mode": "mock",
                        "service": self.kind,
                        "query": title or None,
                        "reason": "error",
                        "title": display,
                        "error": str(mock_exc),
                        "speak": speak_retry(
                            title=display, ok=False, reason="error", mock=True
                        ),
                    }
            return {
                "ok": False,
                "mode": "live",
                "service": self.kind,
                "query": title or None,
                "reason": "error",
                "title": display,
                "error": str(exc),
                "speak": speak_retry(title=display, ok=False, reason="error", mock=False),
            }

    async def library_lookup(self, query: str) -> dict[str, Any]:
        """Find a monitored library title (hasFile may be false)."""
        query = (query or "").strip()
        mock = not self.live
        if self.kind != "radarr":
            return {
                "ok": False,
                "mode": "mock" if mock else "live",
                "service": self.kind,
                "query": query or None,
                "reason": "unsupported",
                "movie": None,
                "speak": "Library switch is only wired for Radarr movies.",
            }
        if not query:
            return {
                "ok": False,
                "mode": "mock" if mock else "live",
                "service": self.kind,
                "query": None,
                "reason": "not_in_library",
                "movie": None,
                "speak": speak_retry(title="That title", ok=False, reason="not_in_library", mock=mock),
            }
        if not self.live:
            rows = pipeline.list_radarr_library(query)
            movie = rows[0] if rows else None
            return {
                "ok": bool(movie),
                "mode": "mock",
                "service": self.kind,
                "query": query,
                "movie": _summarize_movie(movie) if movie else None,
                "raw": movie,
                "reason": None if movie else "not_in_library",
                "speak": (
                    f"Found {movie.get('title')} in the library."
                    if movie
                    else speak_retry(
                        title=query, ok=False, reason="not_in_library", mock=True
                    )
                ),
            }
        client = await self._http()
        try:
            response = await client.get("/api/v3/movie")
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else []
            needle = query.lower()
            matches = [
                row
                for row in rows
                if isinstance(row, dict)
                and needle in str(row.get("title") or "").lower()
            ]
            # Prefer exact / tight title matches.
            exact = [
                row
                for row in matches
                if " ".join(_normalize_title_tokens(str(row.get("title") or "")))
                == " ".join(_normalize_title_tokens(query))
            ]
            movie = (exact or matches)[0] if (exact or matches) else None
            return {
                "ok": bool(movie),
                "mode": "live",
                "service": self.kind,
                "query": query,
                "movie": _summarize_movie(movie) if movie else None,
                "raw": movie,
                "reason": None if movie else "not_in_library",
                "speak": (
                    f"Found {movie.get('title')} in the library."
                    if movie
                    else speak_retry(
                        title=query, ok=False, reason="not_in_library", mock=False
                    )
                ),
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                rows = pipeline.list_radarr_library(query)
                movie = rows[0] if rows else None
                return {
                    "ok": bool(movie),
                    "mode": "mock",
                    "service": self.kind,
                    "query": query,
                    "error": str(exc),
                    "movie": _summarize_movie(movie) if movie else None,
                    "raw": movie,
                    "reason": None if movie else "not_in_library",
                    "speak": (
                        f"Found {movie.get('title')} in the library (mock)."
                        if movie
                        else speak_retry(
                            title=query, ok=False, reason="not_in_library", mock=True
                        )
                    ),
                }
            raise

    def _library_file_too_large(self, movie: dict[str, Any]) -> bool:
        """Heuristic: huge remux / 4K on disk that won't play well."""
        size = movie.get("sizeOnDisk")
        try:
            size_i = int(size) if size not in (None, "") else 0
        except (TypeError, ValueError):
            size_i = 0
        mf = movie.get("movieFile") if isinstance(movie.get("movieFile"), dict) else {}
        if not size_i and mf.get("size") not in (None, ""):
            try:
                size_i = int(mf.get("size") or 0)
            except (TypeError, ValueError):
                size_i = 0
        quality = ""
        q = mf.get("quality")
        if isinstance(q, dict):
            inner = q.get("quality") if isinstance(q.get("quality"), dict) else q
            quality = str((inner or {}).get("name") or "")
        path = str(mf.get("relativePath") or mf.get("path") or "")
        blob = f"{quality} {path}".lower()
        if size_i >= 20_000_000_000:
            return True
        if any(tag in blob for tag in ("remux", "2160", "uhd", "4k")) and size_i >= 12_000_000_000:
            return True
        return False

    async def _delete_movie_file(self, movie_file_id: int) -> None:
        if not self.live:
            pipeline.delete_movie_file(int(movie_file_id))
            return
        client = await self._http()
        response = await client.delete(f"/api/v3/moviefile/{int(movie_file_id)}")
        response.raise_for_status()

    async def list_alternate_releases(
        self,
        query: str,
        *,
        prefer_smaller: bool = True,
        limit: int = 4,
        keep_existing: bool | None = None,
    ) -> dict[str, Any]:
        """List grab-able alternate releases for a library movie (no auto-grab)."""
        query = (query or "").strip()
        mock = not self.live
        if self.kind != "radarr":
            return {
                "ok": False,
                "mode": "mock" if mock else "live",
                "service": self.kind,
                "query": query or None,
                "reason": "unsupported",
                "releases": [],
                "speak": "Alternate releases are only wired for Radarr movies.",
            }
        found = await self.library_lookup(query)
        movie_raw = found.get("raw") if isinstance(found.get("raw"), dict) else None
        movie_sum = found.get("movie") if isinstance(found.get("movie"), dict) else None
        if not movie_raw or not movie_sum:
            return {
                "ok": False,
                "mode": found.get("mode") or ("mock" if mock else "live"),
                "service": self.kind,
                "query": query or None,
                "reason": "not_in_library",
                "title": query,
                "releases": [],
                "speak": speak_retry(
                    title=query or "That title",
                    ok=False,
                    reason="not_in_library",
                    mock=mock,
                ),
            }

        display = str(movie_sum.get("title") or query)
        has_file = bool(movie_raw.get("hasFile"))
        too_large = has_file and self._library_file_too_large(movie_raw)
        # Default: keep-both when a usable (non-huge) file is already present;
        # too-big without an explicit keep → switch/replace path.
        if keep_existing is None:
            keep_existing = bool(has_file) and not too_large

        search_item = {
            "movieId": movie_raw.get("id") or movie_sum.get("libraryId"),
            "movie": movie_raw,
            "title": display,
        }
        try:
            raw_releases = await self._search_releases(search_item)
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                raw_releases = pipeline.list_releases(self.kind, search_item)
            else:
                return {
                    "ok": False,
                    "mode": "live",
                    "service": self.kind,
                    "query": query,
                    "reason": "error",
                    "title": display,
                    "error": str(exc),
                    "releases": [],
                    "speak": speak_retry(title=display, ok=False, reason="error", mock=False),
                }

        # Exclude the currently imported release identity from the offer menu.
        # Keep-both and switch both need this so we do not re-offer the same file.
        blocked = library_file_block_token(movie_raw) if has_file else ""

        # Prefer-smaller ranking is for no-file / too-big switch paths.
        # Keep-both should offer other qualities/sizes, not empty the menu.
        if keep_existing:
            use_prefer_smaller = False
        else:
            use_prefer_smaller = bool(prefer_smaller or too_large or not has_file)

        ranked = self._rank_releases(
            raw_releases,
            blocked=blocked,
            prefer_smaller=use_prefer_smaller,
            limit=limit,
        )
        if use_prefer_smaller:
            # Drop remaining giant remuxes from the offer when size is the ask.
            filtered: list[dict[str, Any]] = []
            for row in ranked:
                blob = str(row.get("title") or "").lower()
                size = _release_size_bytes(row) or 0
                if ("remux" in blob or "2160" in blob or "uhd" in blob) and size >= 20_000_000_000:
                    continue
                filtered.append(row)
            if filtered:
                ranked = filtered

        summarized = [
            summarize_release(row, movie=movie_sum, service=self.kind) for row in ranked
        ]
        if not summarized:
            return {
                "ok": False,
                "mode": "mock" if mock else str(found.get("mode") or "live"),
                "service": self.kind,
                "query": query,
                "reason": "no_alternate",
                "title": display,
                "hasFile": has_file,
                "tooLarge": too_large,
                "keepExisting": bool(keep_existing),
                "movie": movie_sum,
                "releases": [],
                "speak": speak_retry(
                    title=display, ok=False, reason="no_alternate", mock=mock
                ),
            }

        if too_large and not keep_existing:
            reason = "needs_pick_large"
        elif has_file and keep_existing:
            reason = "needs_pick_keep"
        else:
            reason = "needs_pick"
        speak = format_release_offer(display, summarized, reason=reason)
        return {
            "ok": True,
            "mode": "mock" if mock else str(found.get("mode") or "live"),
            "service": self.kind,
            "query": query,
            "reason": reason,
            "needs_pick": True,
            "title": display,
            "hasFile": has_file,
            "tooLarge": too_large,
            "keepExisting": bool(keep_existing),
            "movie": movie_sum,
            "releases": summarized,
            "preferred": summarized[0],
            "speak": speak,
        }

    async def grab_alternate_release(
        self,
        query: str = "",
        *,
        guid: str = "",
        release_token_value: str = "",
        confirm: bool = False,
        prefer_smaller: bool = True,
        reason: str = "",
        keep_existing: bool | None = None,
    ) -> dict[str, Any]:
        """Grab one alternate release for a library movie (confirm-gated).

        Never auto-grabs from a vague yes — caller must pass confirm=True and a
        concrete guid/token (or confirm the single preferred pick explicitly).

        keep_existing=True downloads an extra copy without deleting/replacing the
        current library file (Radarr queue detach, download client keeps the torrent).
        keep_existing=False is the switch path (may delete oversized files so the
        replacement can import).
        """
        query = (query or "").strip()
        guid = (guid or "").strip()
        token = (release_token_value or "").strip()
        mock = not self.live
        listed = await self.list_alternate_releases(
            query,
            prefer_smaller=prefer_smaller,
            keep_existing=keep_existing,
        )
        if not listed.get("ok"):
            return listed

        if keep_existing is None:
            keep_existing = bool(listed.get("keepExisting"))

        display = str(listed.get("title") or query)
        releases = list(listed.get("releases") or [])
        pick_sum: dict[str, Any] | None = None
        if guid:
            for row in releases:
                if str(row.get("guid") or "") == guid:
                    pick_sum = row
                    break
        elif token:
            for row in releases:
                if str(row.get("releaseToken") or "") == token:
                    pick_sum = row
                    break
        elif confirm and len(releases) == 1:
            pick_sum = releases[0]
        elif confirm and listed.get("preferred"):
            # Explicit confirm on preferred after the menu was shown.
            pick_sum = listed["preferred"] if isinstance(listed["preferred"], dict) else None

        if pick_sum is None:
            return {
                **listed,
                "ok": True,
                "needs_pick": True,
                "reason": listed.get("reason") or "needs_pick",
                "keepExisting": bool(keep_existing),
                "speak": listed.get("speak")
                or format_release_offer(
                    display, releases, reason=str(listed.get("reason") or "needs_pick")
                ),
            }

        if not confirm:
            indexer = str(pick_sum.get("indexer") or "")
            return {
                "ok": False,
                "mode": listed.get("mode") or ("mock" if mock else "live"),
                "service": self.kind,
                "query": query or None,
                "reason": "confirm_required",
                "needs_confirm": True,
                "title": display,
                "release": pick_sum,
                "releases": releases,
                "movie": listed.get("movie"),
                "keepExisting": bool(keep_existing),
                "speak": speak_retry(
                    title=display,
                    ok=False,
                    reason="confirm_required",
                    indexer=indexer,
                    mock=mock,
                ),
            }

        # Rebuild a raw release body for POST /release.
        raw_body = {
            "guid": pick_sum.get("guid"),
            "indexerId": pick_sum.get("indexerId"),
            "title": pick_sum.get("title"),
            "indexer": pick_sum.get("indexer"),
            "movieId": pick_sum.get("movieId"),
            "size": pick_sum.get("size"),
        }
        movie_raw = None
        found = await self.library_lookup(query or display)
        if isinstance(found.get("raw"), dict):
            movie_raw = found["raw"]

        has_file = bool(movie_raw and movie_raw.get("hasFile"))
        # Never pretend keep-both if we are about to delete the library file.
        will_delete = (
            (not keep_existing)
            and has_file
            and bool(movie_raw and self._library_file_too_large(movie_raw))
        )
        if keep_existing and will_delete:
            return {
                "ok": False,
                "mode": listed.get("mode") or ("mock" if mock else "live"),
                "service": self.kind,
                "query": query or None,
                "reason": "would_replace",
                "title": display,
                "keepExisting": True,
                "hasFile": has_file,
                "speak": (
                    f"I won't grab that for {display} — it would replace the "
                    f"current library file, and you asked to keep it."
                ),
            }

        try:
            # Switch path: replace oversized / unplayable file so the new grab
            # can import. Keep-both never deletes.
            if will_delete:
                mf = movie_raw.get("movieFile") if movie_raw else None
                if isinstance(mf, dict) and mf.get("id") is not None:
                    await self._delete_movie_file(int(mf["id"]))

            await self._grab_release(raw_body)

            # Keep-both: detach from Radarr import so the existing file stays.
            if keep_existing and has_file:
                queue_id = await self._queue_id_for_release(
                    guid=str(pick_sum.get("guid") or ""),
                    movie_id=pick_sum.get("movieId")
                    or (movie_raw or {}).get("id"),
                    indexer=str(pick_sum.get("indexer") or ""),
                    release_title=str(pick_sum.get("title") or ""),
                )
                if queue_id is not None:
                    await self._detach_queue_keep_client(queue_id)
                elif not mock:
                    # Could not detach — refuse rather than risk a replace on import.
                    return {
                        "ok": False,
                        "mode": "live",
                        "service": self.kind,
                        "query": query or None,
                        "reason": "would_replace",
                        "title": display,
                        "keepExisting": True,
                        "speak": (
                            f"Started a grab for {display} but couldn't detach it "
                            f"from Radarr import — not safe to keep your current file. "
                            f"Check the Radarr queue."
                        ),
                    }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured and self.live:
                try:
                    if will_delete and movie_raw:
                        mf = movie_raw.get("movieFile")
                        if isinstance(mf, dict) and mf.get("id") is not None:
                            pipeline.delete_movie_file(int(mf["id"]))
                    grabbed = pipeline.grab_release(self.kind, raw_body)
                    if keep_existing and has_file:
                        queued = grabbed.get("queued") if isinstance(grabbed, dict) else None
                        qid = queued.get("id") if isinstance(queued, dict) else None
                        if qid is not None:
                            pipeline.detach_queue_item(self.kind, int(qid))
                except Exception as mock_exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "mode": "mock",
                        "service": self.kind,
                        "query": query or None,
                        "reason": "error",
                        "title": display,
                        "error": str(mock_exc),
                        "speak": speak_retry(
                            title=display, ok=False, reason="error", mock=True
                        ),
                    }
            else:
                return {
                    "ok": False,
                    "mode": "live" if self.live else "mock",
                    "service": self.kind,
                    "query": query or None,
                    "reason": "error",
                    "title": display,
                    "error": str(exc),
                    "speak": speak_retry(
                        title=display, ok=False, reason="error", mock=mock
                    ),
                }

        indexer = str(pick_sum.get("indexer") or "")
        outcome = "kept_both" if (keep_existing and has_file) else "switched"
        refreshed = await self.queue(display)
        # Confirm library file still present after keep-both.
        still_has_file = has_file
        if keep_existing and has_file:
            check = await self.library_lookup(query or display)
            raw_check = check.get("raw") if isinstance(check.get("raw"), dict) else {}
            still_has_file = bool(raw_check.get("hasFile"))
            if not still_has_file:
                return {
                    "ok": False,
                    "mode": listed.get("mode") or ("mock" if mock else "live"),
                    "service": self.kind,
                    "query": query or None,
                    "reason": "would_replace",
                    "title": display,
                    "keepExisting": True,
                    "speak": (
                        f"Something cleared the library file for {display} — "
                        f"I won't claim keep-both succeeded."
                    ),
                }
        return {
            "ok": True,
            "mode": listed.get("mode") or ("mock" if mock else "live"),
            "service": self.kind,
            "query": query or None,
            "reason": outcome,
            "title": display,
            "indexer": indexer or None,
            "release": pick_sum,
            "trigger": reason or "user",
            "keepExisting": bool(keep_existing),
            "hasFile": still_has_file,
            "downloads": refreshed.get("downloads") or [],
            "clientDownloads": (
                pipeline.list_client_downloads(self.kind, display)
                if outcome == "kept_both" and mock
                else []
            ),
            "speak": speak_retry(
                title=display,
                ok=True,
                reason=outcome,
                indexer=indexer,
                mock=mock,
            ),
        }


class Overseerr:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def live(self) -> bool:
        return settings.overseerr_configured

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.overseerr_url.rstrip("/"),
                headers={"X-Api-Key": settings.overseerr_api_key, "Accept": "application/json"},
                timeout=12.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str) -> dict[str, Any]:
        query = (query or "").strip()
        if not self.live:
            return {
                "mode": "mock",
                "service": "overseerr",
                "query": query,
                "results": [_summarize_overseerr(h) for h in pipeline.search_overseerr(query)],
            }
        client = await self._http()
        try:
            response = await client.get("/api/v1/search", params={"query": query})
            response.raise_for_status()
            payload = response.json() or {}
            rows = payload.get("results") or []
            return {
                "mode": "live",
                "service": "overseerr",
                "query": query,
                "results": [_summarize_overseerr(h) for h in rows[:8]],
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {
                    "mode": "mock",
                    "service": "overseerr",
                    "query": query,
                    "error": str(exc),
                    "results": [_summarize_overseerr(h) for h in pipeline.search_overseerr(query)],
                }
            raise

    async def request(
        self,
        query: str = "",
        media_id: int | None = None,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        mt = media_type if media_type in {"movie", "tv"} else None

        # Explicit TMDB id — never fuzzy-pick a mismatched search/fallback hit.
        if media_id and not mt:
            # Resolve media type from an id search when the caller omitted it.
            id_found = await self.search(str(int(media_id)))
            for row in id_found.get("results") or []:
                rid = row.get("mediaId") or row.get("tmdbId") or row.get("id")
                try:
                    if int(rid) != int(media_id):
                        continue
                except (TypeError, ValueError):
                    continue
                if row.get("matched") == "fallback":
                    continue
                row_mt = str(row.get("mediaType") or "")
                if row_mt in {"movie", "tv"}:
                    mt = row_mt
                    if not query:
                        query = _row_title(row)
                    break
            if not mt:
                return {
                    "ok": False,
                    "service": "overseerr",
                    "error": f"mediaType required for Overseerr id {media_id}",
                    "speak": f"I need to know if TMDB {media_id} is a movie or a show before requesting it.",
                }

        if media_id and mt:
            pick: dict[str, Any] = {
                "mediaId": media_id,
                "id": media_id,
                "mediaType": mt,
                "title": query or f"TMDB {media_id}",
            }
            if not self.live:
                enriched = [
                    h
                    for h in pipeline.search_overseerr(query or str(media_id))
                    if (h.get("id") == media_id or h.get("mediaId") == media_id)
                    and h.get("matched") != "fallback"
                ]
                if enriched:
                    pick = {**enriched[0], "mediaType": mt}
                item = pipeline.request_overseerr(pick)
                return {
                    "mode": "mock",
                    "service": "overseerr",
                    "requested": _summarize_overseerr(item),
                }
            return await self._post_request(pick, media_id=int(media_id), media_type=mt, query=query)

        # Title-only path: require a confident title match (never results[0] fallback).
        found = await self.search(query) if query else {"results": []}
        results = list(found.get("results") or [])
        confident = _confident_overseerr_hits(results, query=query, media_type=mt)
        if not confident:
            label = query or "that title"
            return {
                "ok": False,
                "service": "overseerr",
                "not_found": True,
                "mismatch": True,
                "query": query,
                "error": f"no confident Overseerr match for {label}",
                "speak": (
                    f"I couldn't find a confident Overseerr match for {label}. "
                    "Want to pick from search results, or send a TMDB/IMDb link?"
                ),
                "results": [_summarize_overseerr(r) for r in results[:6] if isinstance(r, dict)],
            }
        if len(confident) > 1 and not _indistinguishable_overseerr_hits(confident):
            choices = [_summarize_overseerr(r) for r in confident[:6]]
            bits = "; ".join(
                f"{c.get('title')} ({c.get('year')})" if c.get("year") else str(c.get("title"))
                for c in choices
                if c.get("title")
            )
            return {
                "ok": False,
                "service": "overseerr",
                "ambiguous": True,
                "query": query,
                "choices": choices,
                "error": f"ambiguous Overseerr match for {query}",
                "speak": f"Which one for {query}? {bits}",
            }

        pick = confident[0]
        pick_id = pick.get("mediaId") or pick.get("tmdbId") or pick.get("id")
        pick_type = str(pick.get("mediaType") or mt or "movie")
        if pick_type not in {"movie", "tv"}:
            pick_type = mt or "movie"
        try:
            pick_id_i = int(pick_id)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "service": "overseerr",
                "not_found": True,
                "query": query,
                "error": f"Overseerr hit for {query} has no TMDB id",
                "speak": f"I found {pick.get('title') or query}, but it has no TMDB id to request.",
            }

        if not self.live:
            item = pipeline.request_overseerr({**pick, "mediaType": pick_type, "id": pick_id_i})
            return {
                "mode": "mock",
                "service": "overseerr",
                "requested": _summarize_overseerr(item),
            }
        return await self._post_request(
            pick,
            media_id=pick_id_i,
            media_type=pick_type,
            query=query,
        )

    async def _post_request(
        self,
        pick: dict[str, Any],
        *,
        media_id: int,
        media_type: str,
        query: str = "",
    ) -> dict[str, Any]:
        client = await self._http()
        body: dict[str, Any] = {"mediaId": media_id, "mediaType": media_type}
        if media_type == "tv":
            try:
                detail = await client.get(f"/api/v1/tv/{media_id}")
                detail.raise_for_status()
                seasons = [
                    int(s["seasonNumber"])
                    for s in (detail.json().get("seasons") or [])
                    if int(s.get("seasonNumber") or 0) > 0
                ]
                body["seasons"] = seasons or [1]
            except Exception:  # noqa: BLE001
                body["seasons"] = [1]
        body["is4k"] = False
        try:
            response = await client.post("/api/v1/request", json=body)
            response.raise_for_status()
            return {
                "mode": "live",
                "service": "overseerr",
                "requested": pick,
                "status_code": response.status_code,
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                item = pipeline.request_overseerr(pick if isinstance(pick, dict) else {"title": query})
                return {
                    "mode": "mock",
                    "service": "overseerr",
                    "error": str(exc),
                    "requested": _summarize_overseerr(item),
                }
            raise

    async def discover(
        self,
        *,
        genre_ids: list[int] | None = None,
        exclude_genre_ids: list[int] | None = None,
        media_type: str = "movie",
        limit: int = 4,
        page: int = 1,
        primary_release_date_lte: str | None = None,
        vote_count_gte: int | None = None,
        exclude_tmdb_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """TMDB discover via Overseerr (genre include / exclude).

        Fantasy=14, Sci-Fi=878, Horror=27. Results with an excluded genre id
        are filtered out client-side when the API cannot exclude them.

        Released-only defaults: pass ``primary_release_date_lte`` (YYYY-MM-DD)
        and ``vote_count_gte`` so upcoming vaporware does not dominate.
        ``exclude_tmdb_ids`` drops titles already shown this chat session.
        """
        kind = media_type if media_type in {"movie", "tv"} else "movie"
        include = [int(g) for g in (genre_ids or []) if str(g).isdigit() or isinstance(g, int)]
        exclude = [
            int(g) for g in (exclude_genre_ids or []) if str(g).isdigit() or isinstance(g, int)
        ]
        try:
            page_i = max(1, int(page or 1))
        except (TypeError, ValueError):
            page_i = 1
        try:
            vote_floor = int(vote_count_gte) if vote_count_gte is not None else None
        except (TypeError, ValueError):
            vote_floor = None
        date_lte = str(primary_release_date_lte or "").strip() or None
        ban_ids = {
            int(x)
            for x in (exclude_tmdb_ids or [])
            if str(x).isdigit() or isinstance(x, int)
        }
        # Fetch a wider page so client-side exclude/vote filters still fill limit.
        cap = max(1, min(int(limit or 4), 8))
        fetch_cap = max(cap + len(ban_ids), cap * 3)

        if not self.live:
            rows = pipeline.discover_overseerr(
                genre_ids=include,
                exclude_genre_ids=exclude,
                media_type=kind,
                limit=fetch_cap,
                page=page_i,
                primary_release_date_lte=date_lte,
                vote_count_gte=vote_floor,
                exclude_tmdb_ids=sorted(ban_ids),
            )
            return {
                "mode": "mock",
                "service": "overseerr",
                "media_type": kind,
                "genre_ids": include,
                "exclude_genre_ids": exclude,
                "page": page_i,
                "primary_release_date_lte": date_lte,
                "vote_count_gte": vote_floor,
                "results": [_summarize_overseerr(h) for h in rows[:cap]],
            }

        client = await self._http()
        path = "/api/v1/discover/movies" if kind == "movie" else "/api/v1/discover/tv"
        params: dict[str, Any] = {"page": page_i}
        if include:
            # Overseerr accepts comma-separated genre ids (with_genres).
            params["genre"] = ",".join(str(g) for g in include)
        if date_lte:
            if kind == "movie":
                params["primaryReleaseDateLte"] = date_lte
            else:
                params["firstAirDateLte"] = date_lte
        if vote_floor is not None and vote_floor > 0:
            params["voteCountGte"] = str(vote_floor)
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            payload = response.json() or {}
            raw_rows = list(payload.get("results") or [])
            filtered: list[dict[str, Any]] = []
            for row in raw_rows:
                genres = row.get("genreIds") or row.get("genre_ids") or []
                try:
                    gset = {int(g) for g in genres}
                except (TypeError, ValueError):
                    gset = set()
                if exclude and gset and gset.intersection(exclude):
                    continue
                try:
                    tid = int(row.get("id") or row.get("tmdbId") or 0)
                except (TypeError, ValueError):
                    tid = 0
                if tid and tid in ban_ids:
                    continue
                if vote_floor is not None and vote_floor > 0:
                    try:
                        votes = int(row.get("voteCount") or row.get("vote_count") or 0)
                    except (TypeError, ValueError):
                        votes = 0
                    if votes < vote_floor:
                        continue
                if date_lte:
                    release = str(
                        row.get("releaseDate")
                        or row.get("firstAirDate")
                        or row.get("release_date")
                        or ""
                    )[:10]
                    if release and release > date_lte:
                        continue
                filtered.append(row)
                if len(filtered) >= cap:
                    break
            return {
                "mode": "live",
                "service": "overseerr",
                "media_type": kind,
                "genre_ids": include,
                "exclude_genre_ids": exclude,
                "page": page_i,
                "primary_release_date_lte": date_lte,
                "vote_count_gte": vote_floor,
                "results": [_summarize_overseerr(h) for h in filtered[:cap]],
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                rows = pipeline.discover_overseerr(
                    genre_ids=include,
                    exclude_genre_ids=exclude,
                    media_type=kind,
                    limit=fetch_cap,
                    page=page_i,
                    primary_release_date_lte=date_lte,
                    vote_count_gte=vote_floor,
                    exclude_tmdb_ids=sorted(ban_ids),
                )
                return {
                    "mode": "mock",
                    "service": "overseerr",
                    "media_type": kind,
                    "genre_ids": include,
                    "exclude_genre_ids": exclude,
                    "page": page_i,
                    "primary_release_date_lte": date_lte,
                    "vote_count_gte": vote_floor,
                    "error": str(exc),
                    "results": [_summarize_overseerr(h) for h in rows[:cap]],
                }
            raise

    def _people_from_search_rows(self, rows: list[Any]) -> list[dict[str, Any]]:
        """Keep Overseerr multi-search person hits (never title-only movie/tv filter)."""
        people: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict) or not _is_person_result(row):
                continue
            try:
                pid = int(row.get("id") or row.get("mediaId") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            known = row.get("knownFor")
            if known is None:
                known = row.get("known_for")
            people.append(
                {
                    "id": pid or row.get("id"),
                    "mediaType": "person",
                    "name": row.get("name") or row.get("title"),
                    "popularity": row.get("popularity"),
                    "profilePath": row.get("profilePath")
                    or row.get("profile_path")
                    or row.get("posterPath"),
                    "knownFor": known if isinstance(known, list) else [],
                }
            )
        people.sort(key=lambda r: float(r.get("popularity") or 0), reverse=True)
        return people

    async def search_person(self, query: str) -> dict[str, Any]:
        """Person search via the same Overseerr multi-search the UI uses.

        ``GET /api/v1/search?query=…`` is TMDB multi-search (movies, TV, AND
        people). Do **not** pass a ``mediaType=person`` query param — that route
        has no such filter and it can break or be ignored. Keep person rows
        (``mediaType``/``knownFor``), then call ``person_combined_credits``.
        """
        query = (query or "").strip()
        if not query:
            return {
                "mode": "mock" if not self.live else "live",
                "service": "overseerr",
                "query": query,
                "results": [],
            }
        if not self.live:
            return {
                "mode": "mock",
                "service": "overseerr",
                "query": query,
                "results": pipeline.search_person(query),
            }
        client = await self._http()
        try:
            # Same endpoint as Overseerr.search / the UI — query only.
            # Scan the full first page (typically 20) before truncating so a
            # person ranked after several title hits is not dropped.
            response = await client.get(
                "/api/v1/search",
                params={"query": query, "page": 1},
            )
            response.raise_for_status()
            payload = response.json() or {}
            raw_rows = list(payload.get("results") or [])
            # Never forward mediaType as a search filter param.
            people = self._people_from_search_rows(raw_rows)
            # If page 1 had no person but more pages exist, check page 2 once.
            try:
                total_pages = int(payload.get("totalPages") or payload.get("total_pages") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if not people and total_pages > 1:
                response2 = await client.get(
                    "/api/v1/search",
                    params={"query": query, "page": 2},
                )
                response2.raise_for_status()
                payload2 = response2.json() or {}
                people = self._people_from_search_rows(
                    list(payload2.get("results") or [])
                )
            return {
                "mode": "live",
                "service": "overseerr",
                "query": query,
                "results": people[:8],
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {
                    "mode": "mock",
                    "service": "overseerr",
                    "query": query,
                    "error": str(exc),
                    "results": pipeline.search_person(query),
                }
            raise

    async def person_combined_credits(self, person_id: int) -> dict[str, Any]:
        """Combined credits via Overseerr ``GET /api/v1/person/{id}/combined_credits``."""
        try:
            pid = int(person_id)
        except (TypeError, ValueError):
            return {"mode": "error", "service": "overseerr", "cast": [], "crew": [], "id": person_id}
        if not self.live:
            payload = pipeline.person_combined_credits(pid)
            return {
                "mode": "mock",
                "service": "overseerr",
                "id": pid,
                "cast": list(payload.get("cast") or []),
                "crew": list(payload.get("crew") or []),
            }
        client = await self._http()
        try:
            response = await client.get(f"/api/v1/person/{pid}/combined_credits")
            response.raise_for_status()
            payload = response.json() or {}
            return {
                "mode": "live",
                "service": "overseerr",
                "id": pid,
                "cast": list(payload.get("cast") or []),
                "crew": list(payload.get("crew") or []),
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                payload = pipeline.person_combined_credits(pid)
                return {
                    "mode": "mock",
                    "service": "overseerr",
                    "id": pid,
                    "error": str(exc),
                    "cast": list(payload.get("cast") or []),
                    "crew": list(payload.get("crew") or []),
                }
            raise


radarr = StarrClient("radarr")
sonarr = StarrClient("sonarr")
overseerr = Overseerr()
