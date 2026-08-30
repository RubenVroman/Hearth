"""Server-side movie/TV poster fetch — tokens never leave the host.

Resolution order:
1. Plex library thumb (ratingKey) when PMS is live
2. Explicit TMDB posterPath → public image.tmdb.org CDN (no API key)
3. Overseerr / Radarr metadata for a tmdbId (house keys stay server-side)
4. Fixture poster paths for known mock library ids
5. Caller synthesizes an SVG placeholder
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from hearth.config import settings
from hearth.fixtures import MOCK_OVERSEERR_RESULTS, MOCK_PLEX_LIBRARY, MOCK_RADARR_LOOKUP

_TMDB_PATH_RE = re.compile(r"^/[A-Za-z0-9_]+(?:\.[A-Za-z0-9]+)?$")
_TMDB_IMAGE_HOSTS = frozenset({"image.tmdb.org", "www.themoviedb.org"})
# Allow Radarr remotePoster hosts that are commonly TMDB mirrors / house CDNs.
_ALLOWED_POSTER_HOSTS = _TMDB_IMAGE_HOSTS | frozenset(
    {
        "artworks.thetvdb.com",
        "thetvdb.com",
    }
)

# Known poster paths for offline / mock fixtures (public TMDB CDN paths).
_FIXTURE_POSTERS: dict[int, str] = {
    693134: "/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",  # Dune: Part Two
    430231: "/uVHPBTLb6Sj1Eso9HzyBAOMRheM.jpg",  # The Endless
    974950: "/7seqaCaaXDNUHOx4DqwpoOH8pPa.jpg",  # The Brutalist
    949: "/gKaePbkEkaqvMtw74EyhhkfCKKh.jpg",  # Heat (1995)
    10784: "/fMhOeJ2TvuY46iYGmsowhgRXfnr.jpg",  # Heat (1986)
    95396: "/pPHpeI2X1qEd1CS1SeyrdhZ4qnT.jpg",  # Severance
}


def sanitize_poster_path(raw: str | None) -> str | None:
    """Accept only a TMDB-style relative path like ``/abc.jpg``."""
    if not raw:
        return None
    path = str(raw).strip()
    if path.startswith("http://") or path.startswith("https://"):
        parsed = urlparse(path)
        if parsed.hostname not in _TMDB_IMAGE_HOSTS:
            return None
        # Pull the file segment after /t/p/{size}
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "t" and parts[1] == "p":
            path = "/" + parts[-1]
        else:
            return None
    if not path.startswith("/"):
        path = "/" + path
    if not _TMDB_PATH_RE.match(path):
        return None
    return path


def _allowed_remote_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    if host in _ALLOWED_POSTER_HOSTS:
        return True
    # Radarr often returns image.tmdb.org already; also allow house Overseerr proxy hosts.
    if settings.overseerr_url:
        ov = urlparse(settings.overseerr_url).hostname
        if ov and host == ov.lower():
            return True
    if settings.radarr_url:
        rd = urlparse(settings.radarr_url).hostname
        if rd and host == rd.lower():
            return True
    return False


def poster_path_for_tmdb(tmdb_id: int | None) -> str | None:
    if tmdb_id is None:
        return None
    try:
        tid = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    return _FIXTURE_POSTERS.get(tid)


def tmdb_id_for_rating_key(rating_key: str | None) -> int | None:
    key = str(rating_key or "").strip()
    if not key:
        return None
    for row in MOCK_PLEX_LIBRARY:
        if str(row.get("ratingKey")) == key and row.get("tmdbId") is not None:
            try:
                return int(row["tmdbId"])
            except (TypeError, ValueError):
                return None
    return None


def _extract_poster_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Pull posterPath / remote poster URL from *arr / Overseerr payloads."""
    path = sanitize_poster_path(item.get("posterPath") or item.get("poster_path"))
    remote = item.get("remotePoster") or item.get("posterUrl") or item.get("poster")
    if not remote:
        for img in item.get("images") or []:
            if not isinstance(img, dict):
                continue
            cover = str(img.get("coverType") or img.get("cover_type") or "").lower()
            if cover and cover not in {"poster", "primary"}:
                continue
            candidate = img.get("remoteUrl") or img.get("url") or img.get("remote_url")
            if candidate:
                remote = candidate
                break
    if isinstance(remote, str):
        remote = remote.strip() or None
        if remote and not path:
            path = sanitize_poster_path(remote)
    else:
        remote = None
    return {"posterPath": path, "remotePoster": remote if remote and _looks_like_url(remote) else None}


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


async def _fetch_url(url: str) -> tuple[bytes, str] | None:
    if not _allowed_remote_url(url):
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url)
            if response.status_code >= 400:
                return None
            content_type = response.headers.get("content-type") or "image/jpeg"
            if not content_type.startswith("image/"):
                return None
            return response.content, content_type.split(";")[0].strip()
    except Exception:  # noqa: BLE001
        return None


async def _fetch_tmdb_path(path: str) -> tuple[bytes, str] | None:
    clean = sanitize_poster_path(path)
    if not clean:
        return None
    return await _fetch_url(f"https://image.tmdb.org/t/p/w500{clean}")


async def _poster_from_overseerr(tmdb_id: int, media_type: str) -> str | None:
    if not settings.overseerr_configured:
        return None
    kind = "movie" if media_type in {"movie", "film"} else "tv"
    try:
        async with httpx.AsyncClient(
            base_url=settings.overseerr_url.rstrip("/"),
            headers={"X-Api-Key": settings.overseerr_api_key, "Accept": "application/json"},
            timeout=10.0,
        ) as client:
            response = await client.get(f"/api/v1/{kind}/{int(tmdb_id)}")
            if response.status_code >= 400:
                return None
            data = response.json() or {}
            return sanitize_poster_path(data.get("posterPath"))
    except Exception:  # noqa: BLE001
        return None


async def _poster_from_radarr(tmdb_id: int) -> dict[str, Any] | None:
    if not settings.radarr_configured:
        return None
    try:
        async with httpx.AsyncClient(
            base_url=settings.radarr_url.rstrip("/"),
            headers={"X-Api-Key": settings.radarr_api_key, "Accept": "application/json"},
            timeout=10.0,
        ) as client:
            response = await client.get(
                "/api/v3/movie/lookup/tmdb",
                params={"tmdbId": int(tmdb_id)},
            )
            if response.status_code >= 400:
                # Fallback: term lookup
                response = await client.get(
                    "/api/v3/movie/lookup",
                    params={"term": f"tmdb:{int(tmdb_id)}"},
                )
            if response.status_code >= 400:
                return None
            payload = response.json()
            row = payload[0] if isinstance(payload, list) and payload else payload
            if not isinstance(row, dict):
                return None
            return _extract_poster_fields(row)
    except Exception:  # noqa: BLE001
        return None


async def fetch_poster_bytes(
    *,
    rating_key: str | None = None,
    tmdb_id: int | str | None = None,
    media_type: str = "movie",
    poster_path: str | None = None,
    remote_poster: str | None = None,
) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` or None when nothing could be fetched."""
    from hearth.tools.plex import plex

    key = str(rating_key or "").strip()
    if key and key.isdigit():
        got = await plex.thumb_bytes(key)
        if got is not None:
            return got

    path = sanitize_poster_path(poster_path)
    if path:
        got = await _fetch_tmdb_path(path)
        if got is not None:
            return got

    if remote_poster and _looks_like_url(str(remote_poster)):
        got = await _fetch_url(str(remote_poster).strip())
        if got is not None:
            return got

    tid: int | None = None
    if tmdb_id is not None and str(tmdb_id).strip():
        try:
            tid = int(tmdb_id)
        except (TypeError, ValueError):
            tid = None
    if tid is None and key:
        tid = tmdb_id_for_rating_key(key)

    if tid is not None:
        ov_path = await _poster_from_overseerr(tid, media_type)
        if ov_path:
            got = await _fetch_tmdb_path(ov_path)
            if got is not None:
                return got
        kind = str(media_type or "movie").lower()
        if kind in {"movie", "film", ""}:
            radarr_fields = await _poster_from_radarr(tid)
            if radarr_fields:
                if radarr_fields.get("posterPath"):
                    got = await _fetch_tmdb_path(str(radarr_fields["posterPath"]))
                    if got is not None:
                        return got
                remote = radarr_fields.get("remotePoster")
                if remote:
                    got = await _fetch_url(str(remote))
                    if got is not None:
                        return got
        fixture_path = poster_path_for_tmdb(tid)
        if fixture_path:
            got = await _fetch_tmdb_path(fixture_path)
            if got is not None:
                return got

    return None


def enrich_media_hit(item: dict[str, Any]) -> dict[str, Any]:
    """Attach posterPath (and keep tmdbId) for UI art URL construction."""
    out = dict(item)
    fields = _extract_poster_fields(out)
    if fields.get("posterPath"):
        out["posterPath"] = fields["posterPath"]
    elif out.get("tmdbId") is not None:
        fixture = poster_path_for_tmdb(out.get("tmdbId"))
        if fixture:
            out["posterPath"] = fixture
    # Never leak raw house-proxied secrets; remotePoster is only used server-side
    # when the art endpoint is called with an explicit allowlisted URL.
    out.pop("remotePoster", None)
    out["thumb"] = bool(out.get("ratingKey") or out.get("posterPath") or out.get("tmdbId"))
    return out


def fixture_title_for_art(
    *,
    rating_key: str | None = None,
    tmdb_id: int | None = None,
) -> str:
    key = str(rating_key or "").strip()
    if key:
        for row in MOCK_PLEX_LIBRARY:
            if str(row.get("ratingKey")) == key:
                return str(row.get("title") or "Hearth")
    if tmdb_id is not None:
        for row in MOCK_RADARR_LOOKUP:
            if row.get("tmdbId") == tmdb_id:
                return str(row.get("title") or "Hearth")
        for row in MOCK_OVERSEERR_RESULTS:
            if row.get("id") == tmdb_id:
                return str(row.get("title") or "Hearth")
    return "Hearth"
