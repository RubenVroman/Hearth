from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from hearth.config import settings
from hearth.fixtures import MOCK_PLEX_CLIENTS, MOCK_PLEX_LIBRARY, MOCK_PLEX_SESSIONS

# Prefer living-room TVs when no explicit player is named.
_PREFERRED_CLIENT_HINTS = (
    "apple tv",
    "appletv",
    "lg",
    "webos",
    "living room",
    "livingroom",
    "shield",
)


class Plex:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._identity_cache: dict[str, Any] | None = None

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
                    "X-Plex-Device-Name": "Hearth",
                },
                timeout=10.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._identity_cache = None

    async def thumb_bytes(self, rating_key: str) -> tuple[bytes, str] | None:
        """Fetch poster art for a library item. Token stays server-side."""
        key = str(rating_key or "").strip()
        if not key or not key.isdigit():
            return None
        # Mock / offline: no remote art — caller may synthesize a placeholder.
        if not self.live:
            return None
        client = await self._http()
        try:
            response = await client.get(
                f"/library/metadata/{key}/thumb",
                params={"width": 400, "height": 600, "minSize": 1},
            )
            if response.status_code >= 400:
                response = await client.get(f"/library/metadata/{key}/thumb")
            response.raise_for_status()
            content_type = response.headers.get("content-type") or "image/jpeg"
            return response.content, content_type.split(";")[0].strip()
        except Exception:  # noqa: BLE001
            return None

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
            return {"mode": "mock", "results": _mock_search(query, limit)}
        client = await self._http()
        try:
            response = await client.get("/search", params={"query": query, "limit": limit})
            response.raise_for_status()
            payload = response.json()
            metadata = (payload.get("MediaContainer") or {}).get("Metadata") or []
            results = [_item_summary(m) for m in metadata[:limit]]
            return {"mode": "live", "results": results}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {
                    "mode": "mock",
                    "error": str(exc),
                    "results": _mock_search(query, limit),
                }
            raise

    async def clients(self) -> dict[str, Any]:
        if not self.live:
            return {"mode": "mock", "clients": list(MOCK_PLEX_CLIENTS)}
        client = await self._http()
        try:
            response = await client.get("/clients")
            response.raise_for_status()
            return {"mode": "live", "clients": _clients(response.json())}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {
                    "mode": "mock",
                    "error": str(exc),
                    "clients": list(MOCK_PLEX_CLIENTS),
                }
            raise

    async def identity(self) -> dict[str, Any]:
        if self._identity_cache is not None:
            return self._identity_cache
        parsed = urlparse(settings.plex_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 32400)
        protocol = parsed.scheme or "http"
        if not self.live:
            info = {
                "mode": "mock",
                "machineIdentifier": "hearth-mock-plex-server",
                "address": host,
                "port": port,
                "protocol": protocol,
            }
            self._identity_cache = info
            return info
        client = await self._http()
        try:
            response = await client.get("/identity")
            response.raise_for_status()
            payload = response.json()
            container = payload.get("MediaContainer") or payload
            info = {
                "mode": "live",
                "machineIdentifier": container.get("machineIdentifier"),
                "address": host,
                "port": int(container.get("port") or port),
                "protocol": protocol,
            }
            self._identity_cache = info
            return info
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                info = {
                    "mode": "mock",
                    "error": str(exc),
                    "machineIdentifier": "hearth-mock-plex-server",
                    "address": host,
                    "port": port,
                    "protocol": protocol,
                }
                return info
            raise

    async def resolve_play(
        self,
        query: str,
        *,
        player: str | None = None,
        rating_key: str | int | None = None,
        offset_ms: int = 0,
    ) -> dict[str, Any]:
        """Resolve library item + target client without starting playback."""
        title_query = (query or "").strip()
        if not title_query and rating_key is None:
            return {
                "ok": False,
                "error": "query or rating_key required",
                "speak": "Tell me which title to play.",
            }

        item_result = await self._resolve_item(title_query, rating_key=rating_key)
        if not item_result.get("ok"):
            return item_result
        item = item_result["item"]

        playing = await self.now_playing()
        sessions = playing.get("sessions") or []

        clients_result = await self.clients()
        clients = clients_result.get("clients") or []
        resolved = _resolve_client(clients, player, sessions=sessions)
        if not resolved.get("ok"):
            return {
                **resolved,
                "mode": clients_result.get("mode") or item_result.get("mode") or playing.get("mode"),
                "item": item,
                "clients": clients,
                "sessions": sessions,
            }

        client_row = resolved["client"]
        already = _session_on_client(sessions, client_row)
        offset = max(int(offset_ms or 0), 0)
        speak = _plan_speak(item, client_row, already)

        return {
            "ok": True,
            "mode": clients_result.get("mode") or item_result.get("mode") or playing.get("mode"),
            "item": item,
            "client": client_row,
            "resolved": resolved.get("resolved"),
            "offset_ms": offset,
            "sessions": sessions,
            "already_playing": already,
            "candidates": item_result.get("candidates"),
            "speak": speak,
        }

    async def play(
        self,
        query: str,
        *,
        player: str | None = None,
        rating_key: str | int | None = None,
        offset_ms: int = 0,
    ) -> dict[str, Any]:
        """Search (or use ratingKey), resolve a client, start playback via PMS-proxied playMedia."""
        plan = await self.resolve_play(
            query,
            player=player,
            rating_key=rating_key,
            offset_ms=offset_ms,
        )
        if not plan.get("ok"):
            return plan

        item = plan["item"]
        client_row = plan["client"]
        offset = plan.get("offset_ms") or 0

        identity = await self.identity()
        machine_id = identity.get("machineIdentifier")
        if not machine_id:
            return {
                "ok": False,
                "error": "Plex server machineIdentifier unavailable",
                "speak": "Plex server identity is missing; cannot start playback.",
                "item": item,
                "client": client_row,
            }

        key = str(item.get("key") or f"/library/metadata/{item.get('ratingKey')}")
        media_type = _playback_type(item.get("type"))

        if not self.live:
            speak = f"Playing {item.get('title')} on {client_row.get('name')} (mock)."
            return {
                "ok": True,
                "mode": "mock",
                "played": True,
                "item": item,
                "client": client_row,
                "resolved": plan.get("resolved"),
                "offset_ms": offset,
                "already_playing": plan.get("already_playing"),
                "speak": speak,
            }

        client = await self._http()
        play_queue_id: int | None = None
        try:
            play_queue_id = await self._create_play_queue(item, identity)
        except Exception:  # noqa: BLE001 — playQueue is preferred but not required
            play_queue_id = None

        params: dict[str, Any] = {
            "key": key,
            "offset": offset,
            "machineIdentifier": machine_id,
            "address": identity.get("address"),
            "port": identity.get("port"),
            "protocol": identity.get("protocol") or "http",
            "path": (
                f"{identity.get('protocol') or 'http'}://"
                f"{identity.get('address')}:{identity.get('port')}{key}"
            ),
            "providerIdentifier": "com.plexapp.plugins.library",
            "type": media_type,
            "token": settings.plex_token,
            "commandID": 1,
        }
        if play_queue_id is not None:
            params["containerKey"] = f"/playQueues/{play_queue_id}?window=100&own=1"

        headers = {
            "X-Plex-Target-Client-Identifier": str(client_row.get("machineIdentifier") or ""),
        }
        try:
            # Proxy through PMS (same path python-plexapi uses) so Hearth need not
            # reach the client's LAN IP from the Docker bridge.
            response = await client.get("/player/playback/playMedia", params=params, headers=headers)
            # Some clients return empty / "OK" bodies with odd status; accept 2xx and empty OK.
            if response.status_code >= 400:
                response.raise_for_status()
            speak = f"Playing {item.get('title')} on {client_row.get('name')}."
            return {
                "ok": True,
                "mode": "live",
                "played": True,
                "item": item,
                "client": client_row,
                "resolved": plan.get("resolved"),
                "offset_ms": offset,
                "playQueueID": play_queue_id,
                "already_playing": plan.get("already_playing"),
                "speak": speak,
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                speak = (
                    f"Could not reach the Plex client ({exc}); "
                    f"fixture says playing {item.get('title')} on {client_row.get('name')}."
                )
                return {
                    "ok": True,
                    "mode": "mock",
                    "played": True,
                    "error": str(exc),
                    "item": item,
                    "client": client_row,
                    "speak": speak,
                }
            return {
                "ok": False,
                "mode": "live",
                "error": str(exc),
                "item": item,
                "client": client_row,
                "speak": (
                    f"Couldn't start {item.get('title')} on {client_row.get('name')}: {exc}."
                ),
            }

    async def _resolve_item(
        self,
        title_query: str,
        *,
        rating_key: str | int | None = None,
    ) -> dict[str, Any]:
        if rating_key is not None:
            item: dict[str, Any] = {
                "title": title_query or f"item {rating_key}",
                "type": "movie",
                "ratingKey": str(rating_key),
                "key": f"/library/metadata/{rating_key}",
            }
            if title_query:
                searched = await self.search(title_query, limit=8)
                for hit in searched.get("results") or []:
                    if str(hit.get("ratingKey")) == str(rating_key):
                        item = hit
                        break
                return {"ok": True, "mode": searched.get("mode"), "item": item, "candidates": [item]}
            return {"ok": True, "item": item, "candidates": [item]}

        searched = await self.search(title_query, limit=8)
        hits = [
            h
            for h in (searched.get("results") or [])
            if h.get("type") in {"movie", "episode", "show", "season", "clip", "track"}
            or h.get("ratingKey")
        ]
        needle = title_query.lower()
        exact = [h for h in hits if str(h.get("title") or "").lower() == needle]
        soft = [
            h
            for h in hits
            if needle in str(h.get("title") or "").lower()
            or needle in str(h.get("grandparentTitle") or "").lower()
        ]
        ordered = exact or soft or hits
        library_hits = [h for h in ordered if h.get("ratingKey") and h.get("type") != "hint"]
        if not library_hits:
            return {
                "ok": False,
                "mode": searched.get("mode"),
                "in_library": False,
                "query": title_query,
                "results": searched.get("results") or [],
                "error": f"{title_query!r} is not in the Plex library",
                "speak": (
                    f"{title_query} is not in the Plex library. "
                    "Say grab or download if you want Radarr or Overseerr to get it."
                ),
            }

        # Prefer exact title matches; ask when several editions share the same title.
        if len(exact) > 1:
            return _ambiguous_titles(exact, title_query, mode=searched.get("mode"))
        if not exact and len(soft) > 1:
            # Multiple soft matches with no exact hit — ask rather than guessing.
            return _ambiguous_titles(soft, title_query, mode=searched.get("mode"))

        return {
            "ok": True,
            "mode": searched.get("mode"),
            "item": library_hits[0],
            "candidates": library_hits,
        }

    async def _create_play_queue(
        self,
        item: dict[str, Any],
        identity: dict[str, Any],
    ) -> int | None:
        """Create a playQueue for modern clients (Apple TV / webOS Plex)."""
        machine_id = identity.get("machineIdentifier")
        rating_key = item.get("ratingKey")
        if not machine_id or not rating_key:
            return None
        media_type = _playback_type(item.get("type"))
        uri = (
            f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{rating_key}"
        )
        client = await self._http()
        response = await client.post(
            "/playQueues",
            params={
                "type": media_type,
                "uri": uri,
                "shuffle": 0,
                "repeat": 0,
                "includeChapters": 1,
                "includeRelated": 0,
                "continuous": 0,
            },
        )
        response.raise_for_status()
        payload = response.json()
        container = payload.get("MediaContainer") or payload
        pq = container.get("playQueueID")
        return int(pq) if pq is not None else None


def _mock_search(query: str, limit: int) -> list[dict[str, Any]]:
    needle = query.lower()
    hits = [
        _item_summary(item)
        for item in MOCK_PLEX_LIBRARY
        if needle in str(item.get("title", "")).lower()
        or needle in str(item.get("grandparentTitle") or "").lower()
    ]
    if not hits:
        # Also allow matching the session fixture title for continuity.
        for item in MOCK_PLEX_SESSIONS["MediaContainer"]["Metadata"]:
            if needle in str(item.get("title", "")).lower():
                hits.append(_item_summary(item))
    if not hits:
        return [
            {
                "title": query,
                "type": "hint",
                "note": "Plex token not set; search is mocked — title not in fixture library",
            }
        ]
    return hits[:limit]


def _item_summary(m: dict[str, Any]) -> dict[str, Any]:
    rating_key = m.get("ratingKey")
    key = m.get("key") or (f"/library/metadata/{rating_key}" if rating_key is not None else None)
    summary = (m.get("summary") or m.get("tagline") or "").strip()
    rating = m.get("audienceRating") or m.get("rating")
    return {
        "title": m.get("title"),
        "type": m.get("type"),
        "year": m.get("year"),
        "grandparentTitle": m.get("grandparentTitle"),
        "ratingKey": str(rating_key) if rating_key is not None else None,
        "key": key,
        "guid": m.get("guid"),
        "summary": summary[:400] if summary else "",
        "contentRating": m.get("contentRating"),
        "rating": rating,
        "thumb": bool(rating_key),
    }


def _sessions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = (payload.get("MediaContainer") or {}).get("Metadata") or []
    out: list[dict[str, Any]] = []
    for item in metadata:
        duration = item.get("duration") or 0
        offset = item.get("viewOffset") or 0
        remaining_ms = max(duration - offset, 0)
        player = item.get("Player") or {}
        user = item.get("User") or {}
        rating_key = item.get("ratingKey")
        summary = (item.get("summary") or item.get("tagline") or "").strip()
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
                "ratingKey": str(rating_key) if rating_key is not None else None,
                "summary": summary[:400] if summary else "",
                "thumb": bool(rating_key),
            }
        )
    return out


def _clients(payload: dict[str, Any]) -> list[dict[str, Any]]:
    container = payload.get("MediaContainer") or {}
    # PMS returns Server array for /clients (historical naming).
    rows = container.get("Server") or container.get("Player") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        caps = row.get("protocolCapabilities") or ""
        if isinstance(caps, list):
            caps_list = [str(c).lower() for c in caps]
        else:
            caps_list = [c.strip().lower() for c in str(caps).split(",") if c.strip()]
        out.append(
            {
                "name": row.get("name") or row.get("title") or "Plex client",
                "host": row.get("host") or row.get("address"),
                "machineIdentifier": row.get("machineIdentifier"),
                "product": row.get("product"),
                "deviceClass": row.get("deviceClass"),
                "version": row.get("version"),
                "protocolCapabilities": caps_list,
                "controllable": "playback" in caps_list,
            }
        )
    return out


def _playback_type(item_type: Any) -> str:
    kind = str(item_type or "movie").lower()
    if kind in {"track", "album", "artist"}:
        return "music"
    if kind in {"photo", "photoalbum"}:
        return "photo"
    return "video"


def _resolve_client(
    clients: list[dict[str, Any]],
    player: str | None,
    *,
    sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    controllable = [c for c in clients if c.get("controllable") and c.get("machineIdentifier")]
    pool = controllable or [c for c in clients if c.get("machineIdentifier")]

    if not pool:
        return {
            "ok": False,
            "error": "no Plex clients available",
            "speak": (
                "No Plex clients are online. Open Plex on the Apple TV or LG TV, then try again."
            ),
            "ambiguous": False,
        }

    sessions = sessions or []
    active = _active_clients(pool, sessions)

    hint = (player or "").strip().lower()
    explicit = bool(player and player.strip())
    if not hint:
        hint = (settings.plex_default_player or "").strip().lower()

    if hint:
        matched = [c for c in pool if _client_matches(c, hint)]
        # Vague "tv" / default: narrow with the active/recent session when possible.
        if len(matched) > 1 and active:
            narrowed = [c for c in matched if c in active]
            if len(narrowed) == 1:
                return {"ok": True, "client": narrowed[0], "resolved": "active"}
            if len(narrowed) > 1:
                return _ambiguous(narrowed, hint)
        if len(matched) == 1:
            return {"ok": True, "client": matched[0], "resolved": "hint"}
        if len(matched) > 1:
            return _ambiguous(matched, hint)
        # Hint given but nothing matched — fall through with a clear miss if it was explicit.
        if explicit:
            names = ", ".join(str(c.get("name")) for c in pool)
            return {
                "ok": False,
                "error": f"no Plex client matching {player!r}",
                "speak": (
                    f"I couldn't find a Plex client matching {player}. "
                    f"Available: {names or 'none'}."
                ),
                "ambiguous": False,
                "clients": pool,
            }

    # No usable hint: prefer currently playing / recently used client.
    if len(active) == 1:
        return {"ok": True, "client": active[0], "resolved": "active"}
    if len(active) > 1:
        return _ambiguous(active, "active player")

    preferred = [c for c in pool if any(_client_matches(c, h) for h in _PREFERRED_CLIENT_HINTS)]
    if len(preferred) == 1:
        return {"ok": True, "client": preferred[0], "resolved": "preferred"}
    if len(preferred) > 1:
        return _ambiguous(preferred, "living-room TV")

    if len(pool) == 1:
        return {"ok": True, "client": pool[0], "resolved": "only"}

    return _ambiguous(pool, player or "TV")


def _active_clients(
    clients: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Clients that appear in now-playing sessions (playing/paused preferred)."""
    if not sessions:
        return []
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered_sessions = sorted(
        sessions,
        key=lambda s: 0 if str(s.get("state") or "").lower() in {"playing", "paused"} else 1,
    )
    for session in ordered_sessions:
        state = str(session.get("state") or "").lower()
        if state and state not in {"playing", "paused", "buffering"}:
            continue
        player_name = str(session.get("player") or "").strip()
        if not player_name:
            continue
        for client in clients:
            mid = str(client.get("machineIdentifier") or "")
            if mid and mid in seen:
                continue
            if _client_matches(client, player_name.lower()):
                if mid:
                    seen.add(mid)
                ranked.append(client)
                break
    return ranked


def _session_on_client(
    sessions: list[dict[str, Any]],
    client: dict[str, Any],
) -> dict[str, Any] | None:
    for session in sessions:
        state = str(session.get("state") or "").lower()
        if state and state not in {"playing", "paused", "buffering"}:
            continue
        player_name = str(session.get("player") or "").strip()
        if player_name and _client_matches(client, player_name.lower()):
            return {
                "title": session.get("title"),
                "show": session.get("show"),
                "state": session.get("state"),
                "player": session.get("player"),
            }
    return None


def _plan_speak(
    item: dict[str, Any],
    client: dict[str, Any],
    already: dict[str, Any] | None,
) -> str:
    title = item.get("title") or "that"
    year = item.get("year")
    label = f"{title} ({year})" if year else str(title)
    player = client.get("name") or "the TV"
    base = f"I'll play {label} on {player}."
    if already and already.get("title"):
        current = already.get("title")
        show = already.get("show")
        current_label = f"{show} — {current}" if show else current
        return (
            f"{base} {current_label} is currently "
            f"{already.get('state') or 'playing'} there — confirm to switch."
        )
    return f"{base} Confirm to start."


def _ambiguous_titles(
    candidates: list[dict[str, Any]],
    query: str,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    bits = []
    for hit in candidates[:6]:
        title = hit.get("title") or "Untitled"
        year = hit.get("year")
        kind = hit.get("type")
        rk = hit.get("ratingKey")
        label = f"{title} ({year})" if year else str(title)
        if kind:
            label = f"{label} [{kind}]"
        if rk:
            label = f"{label} #{rk}"
        bits.append(label)
    listed = "; ".join(bits)
    return {
        "ok": False,
        "ambiguous": True,
        "ambiguous_titles": True,
        "mode": mode,
        "query": query,
        "candidates": candidates,
        "error": f"multiple Plex titles match {query!r}",
        "speak": f"Which title? I found {listed}.",
    }


def _client_matches(client: dict[str, Any], hint: str) -> bool:
    blob = " ".join(
        str(client.get(k) or "")
        for k in ("name", "product", "deviceClass", "host")
    ).lower()
    tokens = [t for t in hint.replace("-", " ").split() if t and t not in {"the", "on", "plex"}]
    if not tokens:
        return False
    # All significant tokens should appear, or the full hint as a substring.
    if hint in blob:
        return True
    return all(token in blob for token in tokens)


def _ambiguous(clients: list[dict[str, Any]], hint: str) -> dict[str, Any]:
    names = [str(c.get("name") or "unknown") for c in clients]
    listed = ", ".join(names)
    return {
        "ok": False,
        "ambiguous": True,
        "error": f"multiple Plex clients match {hint!r}",
        "clients": clients,
        "speak": f"Which player? I see {listed}.",
    }


plex = Plex()
