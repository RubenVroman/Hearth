"""Radarr / Sonarr / Overseerr — the VAULT *arr request pipeline, not Plex playback."""

from __future__ import annotations

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


def _summarize_overseerr(item: dict[str, Any]) -> dict[str, Any]:
    year = item.get("year")
    if not year:
        date = item.get("releaseDate") or item.get("firstAirDate") or ""
        year = str(date)[:4] or None
    media_id = item.get("id") or item.get("mediaId")
    out = {
        "title": item.get("title") or item.get("name"),
        "year": year,
        "mediaType": item.get("mediaType"),
        "mediaId": media_id,
        "tmdbId": media_id,
        "imdbId": item.get("imdbId"),
        "inLibrary": _in_library(item),
        "overview": (item.get("overview") or item.get("summary") or "")[:180],
        "posterPath": _poster_path(item),
        "popularity": item.get("popularity"),
    }
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
    if ok and reason in {"retried", "grabbed"}:
        source = f" via {indexer}" if indexer else ""
        cap = f" (attempt {attempt}/{max_attempts})" if attempt and max_attempts else ""
        return (
            f"{label} stalled — trying another source{source}{cap}{mock_bit}."
        )
    if reason == "exhausted":
        return (
            f"{label} failed — ran out of alternate sources "
            f"after {max_attempts or attempt or 'several'} tries{mock_bit}."
        )
    if reason == "no_alternate":
        return f"{label} failed — no other release found{mock_bit}."
    if reason == "not_found":
        return f"{label} is not in the download queue{mock_bit}."
    if reason == "healthy":
        return (
            f"{label} is still downloading — say you want another source "
            f"if this one is stuck{mock_bit}."
        )
    if reason == "error":
        return f"Couldn't retry {label}{mock_bit}."
    if ok:
        return f"Retrying {label} from another source{mock_bit}."
    return f"Couldn't retry {label}{mock_bit}."


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
    ) -> dict[str, Any] | None:
        """First acceptable release that is not the blocklisted one."""
        blocked_norm = (blocked or "").strip().lower()
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in releases:
            if not isinstance(row, dict):
                continue
            # Skip rejected / already-grabbed flags when present.
            if row.get("rejected") is True:
                continue
            rejections = row.get("rejections") or []
            if rejections:
                continue
            ident = release_identity(row)
            if blocked_norm and (
                ident == blocked_norm
                or blocked_norm in ident
                or (ident and ident in blocked_norm)
            ):
                continue
            # Prefer approved / higher score when available.
            score = 0
            if row.get("approved") is True:
                score += 100
            try:
                score += int(row.get("score") or 0)
            except (TypeError, ValueError):
                pass
            ranked.append((score, row))
        if not ranked:
            return None
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return ranked[0][1]

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
        if not self.live:
            hits = pipeline.search_overseerr(query or "dune")
            if media_id:
                hits = [h for h in hits if h.get("id") == media_id] or hits
            pick = hits[0] if hits else {"title": query or "unknown", "id": media_id, "mediaType": media_type or "movie"}
            if media_type:
                pick = {**pick, "mediaType": media_type}
            item = pipeline.request_overseerr(pick)
            return {"mode": "mock", "service": "overseerr", "requested": _summarize_overseerr(item)}

        if not media_id or not media_type:
            found = await self.search(query)
            results = found.get("results") or []
            if not results:
                return {"ok": False, "service": "overseerr", "error": f"no Overseerr match for {query}"}
            media_id = results[0].get("mediaId")
            media_type = results[0].get("mediaType") or "movie"
            pick = results[0]
        else:
            pick = {"mediaId": media_id, "mediaType": media_type, "title": query}

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
    ) -> dict[str, Any]:
        """TMDB discover via Overseerr (genre include / exclude).

        Fantasy=14, Sci-Fi=878, Horror=27. Results with an excluded genre id
        are filtered out client-side when the API cannot exclude them.
        """
        kind = media_type if media_type in {"movie", "tv"} else "movie"
        include = [int(g) for g in (genre_ids or []) if str(g).isdigit() or isinstance(g, int)]
        exclude = [
            int(g) for g in (exclude_genre_ids or []) if str(g).isdigit() or isinstance(g, int)
        ]
        cap = max(1, min(int(limit or 4), 8))

        if not self.live:
            rows = pipeline.discover_overseerr(
                genre_ids=include,
                exclude_genre_ids=exclude,
                media_type=kind,
                limit=cap,
            )
            return {
                "mode": "mock",
                "service": "overseerr",
                "media_type": kind,
                "genre_ids": include,
                "exclude_genre_ids": exclude,
                "results": [_summarize_overseerr(h) for h in rows],
            }

        client = await self._http()
        path = "/api/v1/discover/movies" if kind == "movie" else "/api/v1/discover/tv"
        params: dict[str, Any] = {}
        if include:
            # Overseerr accepts comma-separated genre ids.
            params["genre"] = ",".join(str(g) for g in include)
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
                filtered.append(row)
                if len(filtered) >= cap:
                    break
            return {
                "mode": "live",
                "service": "overseerr",
                "media_type": kind,
                "genre_ids": include,
                "exclude_genre_ids": exclude,
                "results": [_summarize_overseerr(h) for h in filtered[:cap]],
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                rows = pipeline.discover_overseerr(
                    genre_ids=include,
                    exclude_genre_ids=exclude,
                    media_type=kind,
                    limit=cap,
                )
                return {
                    "mode": "mock",
                    "service": "overseerr",
                    "media_type": kind,
                    "genre_ids": include,
                    "exclude_genre_ids": exclude,
                    "error": str(exc),
                    "results": [_summarize_overseerr(h) for h in rows],
                }
            raise


radarr = StarrClient("radarr")
sonarr = StarrClient("sonarr")
overseerr = Overseerr()
