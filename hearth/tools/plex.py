from __future__ import annotations

from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import MOCK_PLEX_SESSIONS


class Plex:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def live(self) -> bool:
        return settings.plex_configured

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.plex_url.rstrip("/"),
                headers={
                    "X-Plex-Token": settings.plex_token,
                    "Accept": "application/json",
                    "X-Plex-Client-Identifier": "hearth-vault",
                    "X-Plex-Product": "Hearth",
                },
                timeout=10.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def now_playing(self) -> dict[str, Any]:
        if not self.live:
            return {"mode": "mock", "sessions": _sessions(MOCK_PLEX_SESSIONS)}
        client = await self._http()
        try:
            response = await client.get("/status/sessions")
            response.raise_for_status()
            return {"mode": "live", "sessions": _sessions(response.json())}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {
                    "mode": "mock",
                    "error": str(exc),
                    "sessions": _sessions(MOCK_PLEX_SESSIONS),
                }
            raise

    async def search(self, query: str, limit: int = 8) -> dict[str, Any]:
        if not query.strip():
            return {"mode": "live" if self.live else "mock", "results": []}
        if not self.live:
            needle = query.lower()
            hits = []
            for item in MOCK_PLEX_SESSIONS["MediaContainer"]["Metadata"]:
                if needle in str(item.get("title", "")).lower():
                    hits.append({"title": item["title"], "type": item.get("type"), "year": item.get("year")})
            if not hits:
                hits = [{"title": query, "type": "hint", "note": "Plex token not set; search is mocked"}]
            return {"mode": "mock", "results": hits[:limit]}
        client = await self._http()
        try:
            response = await client.get("/search", params={"query": query, "limit": limit})
            response.raise_for_status()
            payload = response.json()
            metadata = (payload.get("MediaContainer") or {}).get("Metadata") or []
            results = [
                {
                    "title": m.get("title"),
                    "type": m.get("type"),
                    "year": m.get("year"),
                    "grandparentTitle": m.get("grandparentTitle"),
                }
                for m in metadata[:limit]
            ]
            return {"mode": "live", "results": results}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {"mode": "mock", "error": str(exc), "results": []}
            raise


def _sessions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = (payload.get("MediaContainer") or {}).get("Metadata") or []
    out: list[dict[str, Any]] = []
    for item in metadata:
        duration = item.get("duration") or 0
        offset = item.get("viewOffset") or 0
        remaining_ms = max(duration - offset, 0)
        player = item.get("Player") or {}
        user = item.get("User") or {}
        out.append(
            {
                "title": item.get("title"),
                "type": item.get("type"),
                "year": item.get("year"),
                "show": item.get("grandparentTitle"),
                "state": player.get("state"),
                "player": player.get("title"),
                "user": user.get("title"),
                "progress_ms": offset,
                "duration_ms": duration,
                "remaining_ms": remaining_ms,
            }
        )
    return out


plex = Plex()
