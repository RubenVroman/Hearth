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


def _summarize_movie(item: dict[str, Any]) -> dict[str, Any]:
    tmdb = item.get("tmdbId")
    if tmdb is None and item.get("id") is not None and not _library_id(item):
        tmdb = item.get("id")
    out = {
        "title": item.get("title"),
        "year": item.get("year"),
        "tmdbId": tmdb,
        "libraryId": _library_id(item),
        "inLibrary": _in_library(item) or _library_id(item) is not None,
        "hasFile": bool(item.get("hasFile")),
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
        "posterPath": _poster_path(item),
    }
    if item.get("matched"):
        out["matched"] = item.get("matched")
    return out


def _summarize_series(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdbId": item.get("tvdbId"),
        "libraryId": _library_id(item),
        "inLibrary": _in_library(item) or _library_id(item) is not None,
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
        "posterPath": _poster_path(item),
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
        "inLibrary": _in_library(item),
        "posterPath": _poster_path(item),
    }
    if item.get("matched"):
        out["matched"] = item.get("matched")
    return out



def queue_percent(size: Any, sizeleft: Any) -> float | None:
    """Percent complete from Radarr/Sonarr size + sizeleft (0–100, one decimal)."""
    try:
        if size is None or sizeleft is None:
            return None
        total = float(size)
        left = float(sizeleft)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return 100.0 if left <= 0 else None
    done = ((total - left) / total) * 100.0
    return round(max(0.0, min(100.0, done)), 1)


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


def summarize_queue_item(item: dict[str, Any], *, service: str) -> dict[str, Any]:
    size = item.get("size")
    sizeleft = item.get("sizeleft")
    percent = queue_percent(size, sizeleft)
    title = _download_title(item)
    status = normalize_download_status(item)
    return {
        "title": title or None,
        "status": status,
        "percent": percent,
        "size": size,
        "sizeleft": sizeleft,
        "timeleft": item.get("timeleft"),
        "indexer": item.get("indexer"),
        "quality": _quality_name(item),
        "downloadClient": item.get("downloadClient"),
        "errorMessage": item.get("errorMessage"),
        "service": service,
    }


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
        if not self.live:
            raw = (
                pipeline.list_radarr_downloads(title)
                if self.kind == "radarr"
                else pipeline.list_sonarr_downloads(title)
            )
            downloads = [summarize_queue_item(row, service=self.kind) for row in raw]
            return {
                "mode": "mock",
                "service": self.kind,
                "query": title or None,
                "downloads": downloads,
                "found": bool(downloads) if title else None,
                "speak": speak_queue(downloads, title=title, service=self.kind, mock=True),
            }

        client = await self._http()
        params: dict[str, Any] = {"page": 1, "pageSize": 100}
        if self.kind == "radarr":
            params["includeUnknownMovieItems"] = "true"
            params["includeMovie"] = "true"
        else:
            params["includeUnknownSeriesItems"] = "true"
            params["includeSeries"] = "true"
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
            downloads = [summarize_queue_item(row, service=self.kind) for row in rows]
            return {
                "mode": "live",
                "service": self.kind,
                "query": title or None,
                "downloads": downloads,
                "found": bool(downloads) if title else None,
                "speak": speak_queue(downloads, title=title, service=self.kind, mock=False),
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                raw = (
                    pipeline.list_radarr_downloads(title)
                    if self.kind == "radarr"
                    else pipeline.list_sonarr_downloads(title)
                )
                downloads = [summarize_queue_item(row, service=self.kind) for row in raw]
                return {
                    "mode": "mock",
                    "service": self.kind,
                    "query": title or None,
                    "error": str(exc),
                    "downloads": downloads,
                    "found": bool(downloads) if title else None,
                    "speak": speak_queue(downloads, title=title, service=self.kind, mock=True),
                }
            raise


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


radarr = StarrClient("radarr")
sonarr = StarrClient("sonarr")
overseerr = Overseerr()
