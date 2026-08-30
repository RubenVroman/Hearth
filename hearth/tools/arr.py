"""Radarr / Sonarr / Overseerr — the VAULT *arr request pipeline, not Plex playback."""

from __future__ import annotations

from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import pipeline


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


def _summarize_movie(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "year": item.get("year"),
        "tmdbId": item.get("tmdbId") or item.get("id"),
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
        "posterPath": _poster_path(item),
    }


def _summarize_series(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdbId": item.get("tvdbId"),
        "status": item.get("status"),
        "overview": (item.get("overview") or "")[:180],
        "posterPath": _poster_path(item),
    }


def _summarize_overseerr(item: dict[str, Any]) -> dict[str, Any]:
    year = item.get("year")
    if not year:
        date = item.get("releaseDate") or item.get("firstAirDate") or ""
        year = str(date)[:4] or None
    media_id = item.get("id") or item.get("mediaId")
    return {
        "title": item.get("title") or item.get("name"),
        "year": year,
        "mediaType": item.get("mediaType"),
        "mediaId": media_id,
        "tmdbId": media_id,
        "posterPath": _poster_path(item),
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
