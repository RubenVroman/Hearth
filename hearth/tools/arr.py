"""Radarr / Sonarr / Overseerr — the VAULT *arr request pipeline, not Plex playback."""

from __future__ import annotations

from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import pipeline


def _summarize_movie(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "year": item.get("year"),
        "tmdbId": item.get("tmdbId") or item.get("id"),
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
    }


def _bytes_label(value: Any) -> str | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return None


def _queue_percent(size: Any, sizeleft: Any) -> float | None:
    try:
        total = float(size)
        left = float(sizeleft)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    done = max(0.0, min(100.0, ((total - left) / total) * 100.0))
    return round(done, 1)


def _queue_title(item: dict[str, Any]) -> str:
    movie = item.get("movie") if isinstance(item.get("movie"), dict) else {}
    return str(
        movie.get("title")
        or item.get("title")
        or item.get("sourceTitle")
        or "Unknown"
    )


def _summarize_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Radarr /api/v3/queue record for voice/status replies."""
    movie = item.get("movie") if isinstance(item.get("movie"), dict) else {}
    size = item.get("size")
    sizeleft = item.get("sizeleft") if "sizeleft" in item else item.get("sizeLeft")
    percent = _queue_percent(size, sizeleft)
    title = _queue_title(item)
    year = movie.get("year") or item.get("year")
    status = item.get("status") or item.get("trackedDownloadState") or "unknown"
    timeleft = item.get("timeleft") or item.get("timeLeft")
    return {
        "id": item.get("id"),
        "title": title,
        "year": year,
        "release": item.get("title"),
        "percent": percent,
        "size": _bytes_label(size),
        "size_bytes": size,
        "sizeleft": _bytes_label(sizeleft),
        "sizeleft_bytes": sizeleft,
        "status": status,
        "trackedDownloadStatus": item.get("trackedDownloadStatus"),
        "trackedDownloadState": item.get("trackedDownloadState"),
        "timeleft": timeleft,
        "downloadClient": item.get("downloadClient") or item.get("downloadClientName"),
        "indexer": item.get("indexer"),
        "protocol": item.get("protocol"),
        "tmdbId": movie.get("tmdbId"),
    }


def _fuzzy_queue_match(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = (query or "").strip().lower()
    if not needle:
        return list(items)
    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for row in items:
        title = str(row.get("title") or "").lower()
        release = str(row.get("release") or "").lower()
        hay = f"{title} {release}"
        if needle == title:
            exact.append(row)
        elif needle in hay or any(tok and tok in hay for tok in needle.split()):
            partial.append(row)
    return exact or partial


def _speak_queue(items: list[dict[str, Any]], *, query: str = "", empty_reason: str = "") -> str:
    if not items:
        if query:
            return (
                empty_reason
                or f"{query} is not in the Radarr download queue right now."
            )
        return empty_reason or "The Radarr download queue is empty."
    bits: list[str] = []
    for row in items[:5]:
        title = row.get("title") or "a title"
        year = row.get("year")
        label = f"{title} ({year})" if year else str(title)
        percent = row.get("percent")
        status = row.get("status") or "downloading"
        timeleft = row.get("timeleft")
        if percent is not None:
            piece = f"{label} is {percent}% complete ({status})"
        else:
            piece = f"{label} is {status}"
        if timeleft:
            piece += f", about {timeleft} left"
        client = row.get("downloadClient")
        if client:
            piece += f" via {client}"
        bits.append(piece)
    if len(items) == 1:
        return bits[0] + "."
    return "Radarr queue: " + "; ".join(bits) + "."


def _summarize_series(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdbId": item.get("tvdbId"),
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
    }


def _summarize_overseerr(item: dict[str, Any]) -> dict[str, Any]:
    year = item.get("year")
    if not year:
        date = item.get("releaseDate") or item.get("firstAirDate") or ""
        year = str(date)[:4] or None
    return {
        "title": item.get("title") or item.get("name"),
        "year": year,
        "mediaType": item.get("mediaType"),
        "mediaId": item.get("id"),
    }


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

    async def queue(self, query: str = "") -> dict[str, Any]:
        """List active download-queue items; optional fuzzy title filter (movies).

        Response shape (tool `radarr_queue`):
          mode, service, query, count, items[{title,year,percent,size,sizeleft,
          status,timeleft,downloadClient,indexer,…}], matched, empty, speak
        Empty queue / no title match returns empty=True and a clear speak line
        so Hearth can say that cleanly without escalating.
        """
        query = (query or "").strip()
        if self.kind != "radarr":
            return {
                "ok": False,
                "service": self.kind,
                "error": "download queue progress is only wired for Radarr right now",
            }

        if not self.live:
            return self._queue_payload(
                pipeline.list_radarr_downloads(),
                query=query,
                mode="mock",
            )

        client = await self._http()
        try:
            response = await client.get(
                "/api/v3/queue",
                params={"page": 1, "pageSize": 50, "includeMovie": "true"},
            )
            response.raise_for_status()
            payload = response.json() or {}
            if isinstance(payload, list):
                rows = payload
            else:
                rows = payload.get("records") or payload.get("Records") or []
            return self._queue_payload(rows, query=query, mode="live")
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return self._queue_payload(
                    pipeline.list_radarr_downloads(),
                    query=query,
                    mode="mock",
                    error=str(exc),
                )
            raise

    def _queue_payload(
        self,
        rows: list[dict[str, Any]],
        *,
        query: str = "",
        mode: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        summarized = [_summarize_queue_item(row) for row in rows if isinstance(row, dict)]
        matched = _fuzzy_queue_match(summarized, query) if query else list(summarized)
        empty = len(matched) == 0
        if empty and query and summarized:
            empty_reason = f"{query} is not in the Radarr download queue right now."
        elif empty:
            empty_reason = "The Radarr download queue is empty."
        else:
            empty_reason = ""
        out: dict[str, Any] = {
            "ok": True,
            "mode": mode,
            "service": "radarr",
            "query": query or None,
            "count": len(matched),
            "items": matched,
            "matched": bool(query) and not empty,
            "empty": empty,
            "speak": _speak_queue(matched, query=query, empty_reason=empty_reason),
        }
        if error:
            out["error"] = error
        return out

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
