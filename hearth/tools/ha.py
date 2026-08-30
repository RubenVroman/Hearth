from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import MockHouse

_mock = MockHouse()

_MEDIA_DOMAINS = ("media_player", "remote", "switch")
_CONTROL_DOMAINS = frozenset(
    {
        "button",
        "climate",
        "cover",
        "fan",
        "input_boolean",
        "light",
        "media_player",
        "remote",
        "scene",
        "script",
        "switch",
        "vacuum",
    }
)
_UNREACHABLE_STATES = frozenset({"unavailable", "unknown"})
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_KEEP_ATTRS = (
    "friendly_name",
    "brightness",
    "brightness_pct",
    "color_temp",
    "color_temp_kelvin",
    "rgb_color",
    "source",
    "source_list",
    "volume_level",
    "is_volume_muted",
    "media_title",
    "media_artist",
    "media_content_type",
    "media_content_id",
    "app_name",
    "device_class",
    "supported_features",
    "temperature",
    "current_temperature",
    "hvac_action",
    "percentage",
    "current_position",
    "battery_level",
)


class HomeAssistant:
    """Reliable Home Assistant REST client and house-device resolver.

    Fixtures are used only when HA is genuinely unconfigured. Once a token is
    present, failures stay failures; a real command must never be reported as a
    successful mutation of a mock house.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_signature: tuple[str, str] | None = None
        self._client_lock = asyncio.Lock()
        self._entity_cache: dict[str, tuple[float, str]] = {}
        self._last_success_at: float | None = None
        self._last_error: str | None = None

    @property
    def live(self) -> bool:
        return settings.ha_configured

    async def _http(self) -> httpx.AsyncClient:
        signature = (settings.ha_url.rstrip("/"), settings.ha_token)
        async with self._client_lock:
            if self._client is not None and self._client_signature != signature:
                await self._client.aclose()
                self._client = None
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=signature[0],
                    headers={
                        "Authorization": f"Bearer {signature[1]}",
                        "Content-Type": "application/json",
                    },
                    timeout=httpx.Timeout(10.0, connect=4.0),
                )
                self._client_signature = signature
        return self._client

    async def _drop_client(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                await self._client.aclose()
            self._client = None
            self._client_signature = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> tuple[httpx.Response, int]:
        attempts = max(1, int(settings.ha_request_retries))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                client = await self._http()
                if method == "GET":
                    response = await client.get(path)
                else:
                    response = await client.post(path, json=json)
                if response.status_code in _TRANSIENT_STATUS and attempt < attempts:
                    await self._retry_pause(attempt)
                    continue
                response.raise_for_status()
                self._last_success_at = time.time()
                self._last_error = None
                return response, attempt
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status not in _TRANSIENT_STATUS or attempt >= attempts:
                    break
                await self._retry_pause(attempt)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                await self._drop_client()
                if attempt >= attempts:
                    break
                await self._retry_pause(attempt)
        assert last_error is not None
        self._last_error = _error_text(last_error)
        raise last_error

    async def _retry_pause(self, attempt: int) -> None:
        base = max(0.0, float(settings.ha_retry_base_seconds))
        if base:
            await asyncio.sleep(min(2.0, base * (2 ** (attempt - 1))))

    async def aclose(self) -> None:
        await self._drop_client()
        self._entity_cache.clear()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "configured": self.live,
            "url": settings.ha_url,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "receiver_centric": settings.receiver_centric,
        }

    async def ping(self) -> dict[str, Any]:
        if not self.live:
            return {"ok": True, "mode": "mock", "configured": False}
        try:
            response, attempts = await self._request("GET", "/api/")
            return {
                "ok": True,
                "mode": "live",
                "configured": True,
                "attempts": attempts,
                "ha": response.json(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "mode": "live",
                "configured": True,
                "error": _error_text(exc),
            }

    async def list_states(self, domain: str | None = None) -> dict[str, Any]:
        if not self.live:
            states = _mock.list_states(domain)
            return {"ok": True, "mode": "mock", "states": _summarize(states)}
        try:
            response, attempts = await self._request("GET", "/api/states")
            states = response.json()
            if domain:
                prefix = domain.rstrip(".") + "."
                states = [s for s in states if str(s.get("entity_id", "")).startswith(prefix)]
            return {
                "ok": True,
                "mode": "live",
                "attempts": attempts,
                "states": _summarize(states),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "mode": "live",
                "error": _error_text(exc),
                "states": [],
            }

    async def list_media_entities(self) -> dict[str, Any]:
        """Return media_player/remote/switch entities with one HA snapshot."""
        result = await self.list_states()
        states = [
            row
            for row in result.get("states") or []
            if _domain(str(row.get("entity_id") or "")) in _MEDIA_DOMAINS
        ]
        return {**result, "states": states}

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        if not self.live:
            state = _mock.get_state(entity_id)
            return {"mode": "mock", "state": state, "ok": state is not None}
        try:
            response, attempts = await self._request("GET", f"/api/states/{entity_id}")
            return {
                "mode": "live",
                "ok": True,
                "attempts": attempts,
                "state": _summarize_one(response.json()),
            }
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {"mode": "live", "ok": False, "error": "not found"}
            return {"mode": "live", "ok": False, "error": _error_text(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"mode": "live", "ok": False, "error": _error_text(exc)}

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {"entity_id": entity_id, **(data or {})}
        if not self.live:
            result = _mock.call_service(domain, service, entity_id, data)
            return {"mode": "mock", "attempts": 1, **result}
        try:
            response, attempts = await self._request(
                "POST", f"/api/services/{domain}/{service}", json=payload
            )
            return {
                "mode": "live",
                "ok": True,
                "accepted": True,
                "attempts": attempts,
                "changed": response.json(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "mode": "live",
                "ok": False,
                "accepted": False,
                "error": _error_text(exc),
            }

    def entity_for(self, device: str) -> str:
        role = self._device_role(device)
        if role == "tv":
            return settings.ha_tv_entity.strip() or "media_player.lg_webos_tv"
        if role == "avr":
            return settings.ha_avr_entity.strip() or "media_player.denon_avr_x3700h"
        if role == "apple_tv":
            return settings.ha_apple_tv_entity.strip() or "media_player.apple_tv"
        raise ValueError(f"unknown media device {device!r}; use tv, avr, or apple_tv")

    async def resolve_device_state(self, device: str) -> dict[str, Any]:
        role = self._device_role(device)
        configured = self.entity_for(role)
        cached = self._entity_cache.get(role)
        candidates = [configured]
        if (
            cached
            and time.monotonic() - cached[0] <= max(0, settings.ha_entity_cache_seconds)
            and cached[1] not in candidates
        ):
            candidates.insert(0, cached[1])

        last: dict[str, Any] = {}
        for entity_id in candidates:
            result = await self.get_state(entity_id)
            last = result
            state = result.get("state")
            if result.get("ok") and state is not None:
                self._entity_cache[role] = (time.monotonic(), entity_id)
                return {
                    "mode": result.get("mode"),
                    "ok": True,
                    "reachable": _state_reachable(state),
                    "device": role,
                    "entity_id": entity_id,
                    "state": state,
                    "resolved": "config" if entity_id == configured else "cache",
                }

        fuzzy = await self._find_by_hint(role)
        if fuzzy:
            entity_id = str(fuzzy.get("entity_id") or configured)
            self._entity_cache[role] = (time.monotonic(), entity_id)
            return {
                "mode": last.get("mode") or "live",
                "ok": True,
                "reachable": _state_reachable(fuzzy),
                "device": role,
                "entity_id": entity_id,
                "state": fuzzy,
                "resolved": "discovery",
            }

        hint = (
            "pair the Apple TV integration; Hearth auto-discovers its current entity id"
            if role == "apple_tv"
            else "pair the device in Home Assistant; Hearth will auto-discover its entity id"
        )
        return {
            "mode": last.get("mode") or ("live" if self.live else "mock"),
            "ok": False,
            "reachable": False,
            "device": role,
            "entity_id": configured,
            "error": last.get("error") or f"{configured} not found — {hint}",
        }

    def _device_role(self, device: str) -> str:
        key = _slug(device)
        if key in {"tv", "lg", "webos", "lg_tv", "lg_webos_tv", "television"}:
            return "tv"
        if key in {"avr", "denon", "receiver", "amp", "denon_avr", "denon_avr_x3700h"}:
            return "avr"
        if key in {"apple_tv", "appletv", "atv", "infuse", "living_room_apple_tv"}:
            return "apple_tv"
        return key or "unknown"

    async def _find_by_hint(self, device: str) -> dict[str, Any] | None:
        media = await self.list_media_entities()
        states = media.get("states") or []
        role = self._device_role(device)
        scored = sorted(
            ((self._media_match_score(role, row), row) for row in states),
            key=lambda item: item[0],
            reverse=True,
        )
        return scored[0][1] if scored and scored[0][0] > 0 else None

    @staticmethod
    def _media_match_score(role: str, row: dict[str, Any]) -> int:
        eid = str(row.get("entity_id") or "").lower()
        name = str((row.get("attributes") or {}).get("friendly_name") or "").lower()
        blob = f"{eid} {name}"
        if role == "apple_tv":
            return 100 if "apple" in blob and "tv" in blob else (70 if "atv" in blob else 0)
        if role == "tv":
            if "apple" in blob or "atv" in eid:
                return 0
            return 100 if "webos" in blob or "lg" in blob else (35 if " tv" in blob else 0)
        if role == "avr":
            if "denon" in blob:
                return 100
            if "avr" in blob or "receiver" in blob or "heos" in blob:
                return 70
        return 0

    async def resolve_entity(
        self,
        hint: str,
        *,
        domains: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve an entity id or friendly name without inventing an id."""
        query = (hint or "").strip()
        if not query:
            return {"ok": False, "error": "device or entity name required"}
        result = await self.list_states()
        if not result.get("ok"):
            return {**result, "ok": False}
        allowed = {d.rstrip(".") for d in domains or [] if d}
        rows = [
            row
            for row in result.get("states") or []
            if not allowed or _domain(str(row.get("entity_id") or "")) in allowed
        ]
        scored = sorted(
            ((_entity_match_score(query, row), row) for row in rows),
            key=lambda item: (item[0], _state_reachable(item[1])),
            reverse=True,
        )
        scored = [item for item in scored if item[0] > 0]
        if not scored:
            return {
                "ok": False,
                "mode": result.get("mode"),
                "error": f"No Home Assistant entity matches {query!r}",
            }
        top_score, top = scored[0]
        tied = [row for score, row in scored if score == top_score]
        if len(tied) > 1 and top_score < 100:
            return {
                "ok": False,
                "mode": result.get("mode"),
                "ambiguous": True,
                "error": f"More than one Home Assistant entity matches {query!r}",
                "matches": tied[:8],
            }
        return {
            "ok": True,
            "mode": result.get("mode"),
            "entity_id": top.get("entity_id"),
            "state": top,
            "reachable": _state_reachable(top),
            "resolved": "exact" if top_score >= 100 else "friendly_name",
        }

    async def media_control(
        self,
        device: str,
        action: str,
        *,
        volume_level: float | None = None,
        source: str | None = None,
        media_content_id: str | None = None,
        media_content_type: str | None = None,
        is_volume_muted: bool | None = None,
    ) -> dict[str, Any]:
        """Control one media device, retry the write, then verify real state."""
        action = (action or "").strip().lower()
        role = self._device_role(device)
        resolved = await self.resolve_device_state(device)
        entity_id = str(resolved.get("entity_id") or self.entity_for(device))
        if not resolved.get("ok") and action not in {"turn_on", "on", "power_on"}:
            return {**resolved, "action": action}

        domain = _domain(entity_id) or "media_player"
        service, data, error = _media_service(
            action,
            volume_level=volume_level,
            source=source,
            media_content_id=media_content_id,
            media_content_type=media_content_type,
            is_volume_muted=is_volume_muted,
        )
        if error:
            return {"ok": False, "error": error}

        result = await self.call_service(domain, service, entity_id, data or None)
        if not result.get("ok"):
            return {
                "ok": False,
                "device": self._device_role(device),
                "action": action,
                "entity_id": entity_id,
                "service": f"{domain}.{service}",
                "data": data or None,
                "mode": result.get("mode"),
                "result": result,
                "verified": False,
                "error": result.get("error") or "Home Assistant rejected the service call",
            }

        after, verified = await self._verify_media_state(entity_id, service, data)
        fallback: dict[str, Any] | None = None
        if role == "apple_tv" and verified is False and service in {"turn_on", "turn_off"}:
            fallback = await self._apple_tv_remote_power(entity_id, service)
            if fallback.get("ok"):
                after, verified = await self._verify_media_state(entity_id, service, data)

        accepted = bool(result.get("accepted", result.get("ok"))) or bool(
            fallback and fallback.get("accepted")
        )
        verified_ok = verified is not False
        out: dict[str, Any] = {
            "ok": verified_ok,
            "accepted": accepted,
            "device": role,
            "action": action,
            "entity_id": entity_id,
            "service": f"{domain}.{service}",
            "data": data or None,
            "mode": result.get("mode"),
            "result": result,
            "state": after,
            "verified": verified,
            "attempts": result.get("attempts", 1),
        }
        if fallback is not None:
            out["fallback"] = fallback
        if verified is False:
            message = "Home Assistant accepted the command, but the requested state was not observed"
            out["warning"] = message
            out["error"] = message
        return out

    async def _apple_tv_remote_power(
        self,
        media_entity_id: str,
        service: str,
    ) -> dict[str, Any]:
        """Use Apple TV's HA remote entity when media-player power does not take."""
        object_id = media_entity_id.split(".", 1)[-1]
        preferred = f"remote.{object_id}"
        direct = await self.get_state(preferred)
        remote_entity_id: str | None = preferred if direct.get("ok") else None
        resolved_by = "matching_entity_id"
        if remote_entity_id is None:
            found = await self.resolve_entity(object_id, domains=["remote"])
            if found.get("ok"):
                remote_entity_id = str(found.get("entity_id") or "") or None
                resolved_by = str(found.get("resolved") or "friendly_name")
        if remote_entity_id is None:
            return {
                "ok": False,
                "accepted": False,
                "error": "No matching Home Assistant remote entity was found for Apple TV",
            }

        command = "wakeup" if service == "turn_on" else "suspend"
        result = await self.call_service(
            "remote",
            "send_command",
            remote_entity_id,
            {"command": command},
        )
        return {
            "ok": bool(result.get("ok")),
            "accepted": bool(result.get("accepted", result.get("ok"))),
            "entity_id": remote_entity_id,
            "resolved": resolved_by,
            "service": "remote.send_command",
            "command": command,
            "result": result,
            "error": result.get("error"),
        }

    async def _verify_media_state(
        self,
        entity_id: str,
        service: str,
        data: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool | None]:
        verify_services = {
            "turn_on",
            "turn_off",
            "volume_set",
            "volume_mute",
            "select_source",
            "media_play",
            "media_pause",
            "media_stop",
            "play_media",
        }
        if service not in verify_services:
            state = await self.get_state(entity_id)
            return state.get("state"), None
        timeout = 0.0 if not self.live else max(0.0, float(settings.ha_verify_timeout_seconds))
        interval = max(0.05, float(settings.ha_verify_poll_interval))
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] | None = None
        while True:
            state_result = await self.get_state(entity_id)
            latest = state_result.get("state")
            if latest is not None and _matches_service_state(latest, service, data):
                return latest, True
            if time.monotonic() >= deadline:
                return latest, False
            await asyncio.sleep(interval)

    async def activate_media_path(self, target: str = "apple_tv") -> dict[str, Any]:
        """Wake and route the receiver-centric living-room media chain."""
        role = self._device_role(target)
        if role not in {"apple_tv", "tv"}:
            return {"ok": False, "error": "activity target must be apple_tv or tv"}
        if not settings.receiver_centric:
            direct = await self.media_control(role, "turn_on")
            return {
                "ok": bool(direct.get("ok")),
                "activity": role,
                "receiver_centric": False,
                "steps": [direct],
                "speak": f"Turned on the {_role_label(role)}.",
            }

        steps: list[dict[str, Any]] = []
        steps.append(await self.media_control("avr", "turn_on"))
        steps.append(await self.media_control("tv", "turn_on"))
        requested_source = (
            settings.ha_avr_apple_tv_source if role == "apple_tv" else settings.ha_avr_tv_source
        ).strip()
        if requested_source:
            source = await self._resolve_avr_source(requested_source)
            if source:
                steps.append(await self.media_control("avr", "select_source", source=source))
            else:
                steps.append(
                    {
                        "ok": True,
                        "skipped": True,
                        "action": "select_source",
                        "warning": f"Receiver source {requested_source!r} is not in its source list",
                    }
                )
        if role == "apple_tv":
            steps.append(await self.media_control("apple_tv", "turn_on"))
        if self.live and settings.ha_media_settle_seconds > 0:
            await asyncio.sleep(min(3.0, settings.ha_media_settle_seconds))

        failed = [step for step in steps if step.get("ok") is False]
        return {
            "ok": not failed,
            "activity": role,
            "receiver_centric": True,
            "source": requested_source or None,
            "steps": steps,
            "failed_steps": len(failed),
            "speak": (
                f"The Denon, LG TV, and {_role_label(role)} path are ready."
                if not failed
                else f"The {_role_label(role)} path is only partly ready; {len(failed)} step failed."
            ),
        }

    async def power_off_media_path(self) -> dict[str, Any]:
        """Stop the living-room chain in source-to-screen-to-receiver order."""
        steps: list[dict[str, Any]] = []
        for role in ("apple_tv", "tv", "avr"):
            steps.append(await self.media_control(role, "turn_off"))
        failed = [step for step in steps if step.get("ok") is False]
        return {
            "ok": not failed,
            "activity": "off",
            "receiver_centric": settings.receiver_centric,
            "steps": steps,
            "failed_steps": len(failed),
            "speak": (
                "The Apple TV, LG TV, and Denon are off."
                if not failed
                else f"The media chain is only partly off; {len(failed)} step failed."
            ),
        }

    async def _resolve_avr_source(self, requested: str) -> str | None:
        avr = await self.resolve_device_state("avr")
        attrs = ((avr.get("state") or {}).get("attributes") or {})
        sources = [str(source) for source in attrs.get("source_list") or []]
        if not sources:
            return requested
        needle = _slug(requested)
        for source in sources:
            if _slug(source) == needle:
                return source
        for source in sources:
            source_slug = _slug(source)
            if needle in source_slug or source_slug in needle:
                return source
        aliases = {
            "media_player": ("apple_tv", "appletv", "player"),
            "tv_audio": ("tv", "arc", "earc"),
        }
        for alias in aliases.get(needle, ()):
            for source in sources:
                if alias in _slug(source):
                    return source
        return None

    async def network_inventory(self, *, limit: int = 250) -> dict[str, Any]:
        """Inventory everything represented in HA, including unavailable entities."""
        result = await self.list_states()
        if not result.get("ok"):
            return {
                "ok": False,
                "mode": result.get("mode"),
                "health": "offline",
                "error": result.get("error"),
                "devices": [],
                "speak": f"Home Assistant is unreachable: {result.get('error')}",
            }
        rows = result.get("states") or []
        domains = Counter(_domain(str(row.get("entity_id") or "")) for row in rows)
        reachable = [row for row in rows if _state_reachable(row)]
        unavailable = [row for row in rows if not _state_reachable(row)]
        controllable = [row for row in rows if _domain(str(row.get("entity_id") or "")) in _CONTROL_DOMAINS]
        key_media: dict[str, Any] = {}
        media_rows = [
            row
            for row in rows
            if _domain(str(row.get("entity_id") or "")) in _MEDIA_DOMAINS
        ]
        for role in ("avr", "tv", "apple_tv"):
            configured = self.entity_for(role)
            matched = next(
                (
                    row
                    for row in media_rows
                    if str(row.get("entity_id") or "").lower() == configured.lower()
                ),
                None,
            )
            exact = matched is not None
            if matched is None:
                matched = max(
                    media_rows,
                    key=lambda row: self._media_match_score(role, row),
                    default=None,
                )
            score = self._media_match_score(role, matched) if matched else 0
            found = bool(matched and (exact or score > 0))
            key_media[role] = {
                "configured_entity_id": configured,
                "found": found,
                "entity": matched if found else None,
                "reachable": bool(found and matched and _state_reachable(matched)),
            }
        health = "healthy" if not unavailable else "degraded"
        shown = rows[: max(1, min(int(limit), 1000))]
        speak = (
            f"Home Assistant sees {len(rows)} entities across {len(domains)} domains; "
            f"{len(reachable)} reachable, {len(unavailable)} unavailable. "
            f"Denon {'reachable' if key_media['avr']['reachable'] else 'not reachable'}, "
            f"LG TV {'reachable' if key_media['tv']['reachable'] else 'not reachable'}, and "
            f"Apple TV {'reachable' if key_media['apple_tv']['reachable'] else 'not reachable'}."
        )
        return {
            "ok": True,
            "mode": result.get("mode"),
            "health": health,
            "total": len(rows),
            "reachable": len(reachable),
            "unavailable": len(unavailable),
            "controllable": len(controllable),
            "domains": dict(sorted(domains.items())),
            "key_media": key_media,
            "unavailable_entities": unavailable,
            "devices": shown,
            "truncated": len(shown) < len(rows),
            "speak": speak,
        }

    async def control_entity(
        self,
        device: str,
        action: str,
        *,
        domain: str | None = None,
        value: float | str | bool | None = None,
    ) -> dict[str, Any]:
        """Control any routine HA entity by friendly name, safely and dynamically."""
        resolved = await self.resolve_entity(device, domains=[domain] if domain else _CONTROL_DOMAINS)
        if not resolved.get("ok"):
            return resolved
        entity_id = str(resolved["entity_id"])
        entity_domain = _domain(entity_id)
        service, data, error = _generic_service(entity_domain, action, value)
        if error:
            return {"ok": False, "entity_id": entity_id, "error": error}
        result = await self.call_service(entity_domain, service, entity_id, data or None)
        after = await self.get_state(entity_id)
        return {
            "ok": bool(result.get("ok")),
            "mode": result.get("mode"),
            "device": device,
            "entity_id": entity_id,
            "service": f"{entity_domain}.{service}",
            "data": data or None,
            "result": result,
            "state": after.get("state"),
            "error": result.get("error"),
        }


def _media_service(
    action: str,
    *,
    volume_level: float | None,
    source: str | None,
    media_content_id: str | None,
    media_content_type: str | None,
    is_volume_muted: bool | None,
) -> tuple[str, dict[str, Any], str | None]:
    data: dict[str, Any] = {}
    if action in {"turn_on", "on", "power_on"}:
        return "turn_on", data, None
    if action in {"turn_off", "off", "power_off"}:
        return "turn_off", data, None
    if action == "toggle":
        return "toggle", data, None
    if action in {"volume_set", "set_volume", "volume"}:
        if volume_level is None:
            return "", data, "volume_level required (0.0–1.0 or 0–100)"
        data["volume_level"] = _normalize_volume(volume_level)
        return "volume_set", data, None
    if action in {"volume_mute", "mute"}:
        data["is_volume_muted"] = True if is_volume_muted is None else bool(is_volume_muted)
        return "volume_mute", data, None
    if action in {"volume_unmute", "unmute"}:
        data["is_volume_muted"] = False
        return "volume_mute", data, None
    if action in {"volume_up", "volume_down"}:
        return action, data, None
    if action in {"select_source", "source"}:
        if not source:
            return "", data, "source required"
        data["source"] = source
        return "select_source", data, None
    if action == "play_media":
        if not media_content_id or not media_content_type:
            return "", data, "media_content_id and media_content_type required for play_media"
        data.update(
            {"media_content_id": media_content_id, "media_content_type": media_content_type}
        )
        return "play_media", data, None
    aliases = {
        "play": "media_play",
        "media_play": "media_play",
        "pause": "media_pause",
        "media_pause": "media_pause",
        "stop": "media_stop",
        "media_stop": "media_stop",
        "next": "media_next_track",
        "skip": "media_next_track",
        "media_next_track": "media_next_track",
        "previous": "media_previous_track",
        "back": "media_previous_track",
        "media_previous_track": "media_previous_track",
    }
    service = aliases.get(action)
    if service:
        return service, data, None
    return "", data, f"unknown media action {action!r}"


def _generic_service(
    domain: str,
    action: str,
    value: float | str | bool | None,
) -> tuple[str, dict[str, Any], str | None]:
    action = _slug(action)
    data: dict[str, Any] = {}
    if domain not in _CONTROL_DOMAINS:
        return "", data, f"{domain} entities are read-only through smart control"
    if domain in {"scene", "script", "button"} and action in {"on", "turn_on", "activate", "run", "press"}:
        return ("press" if domain == "button" else "turn_on"), data, None
    if domain == "cover" and action in {"open", "close", "stop"}:
        return f"{action}_cover", data, None
    if domain == "climate" and action in {"temperature", "set_temperature", "heat_to"}:
        if value is None:
            return "", data, "temperature value required"
        data["temperature"] = float(value)
        return "set_temperature", data, None
    if action in {"brightness", "dim", "set_brightness"} and domain == "light":
        if value is None:
            return "", data, "brightness value required"
        data["brightness_pct"] = max(0.0, min(100.0, float(value)))
        return "turn_on", data, None
    if action in {"percentage", "set_percentage"} and domain == "fan":
        if value is None:
            return "", data, "percentage value required"
        data["percentage"] = max(0.0, min(100.0, float(value)))
        return "set_percentage", data, None
    if domain == "vacuum" and action in {"start", "stop", "pause", "return_to_base"}:
        return action, data, None
    aliases = {
        "on": "turn_on",
        "turn_on": "turn_on",
        "off": "turn_off",
        "turn_off": "turn_off",
        "toggle": "toggle",
    }
    if action in aliases and domain not in {"button", "climate", "cover", "scene"}:
        return aliases[action], data, None
    return "", data, f"action {action!r} is not supported for {domain}"


def _matches_service_state(state: dict[str, Any], service: str, data: dict[str, Any]) -> bool:
    status = str(state.get("state") or "").lower()
    attrs = state.get("attributes") or {}
    if service == "turn_on":
        return status not in {"off", "unavailable", "unknown", ""}
    if service == "turn_off":
        return status == "off"
    if service == "volume_set":
        actual = attrs.get("volume_level")
        return actual is not None and abs(float(actual) - float(data["volume_level"])) <= 0.025
    if service == "volume_mute":
        return bool(attrs.get("is_volume_muted")) is bool(data.get("is_volume_muted"))
    if service == "select_source":
        return _slug(str(attrs.get("source") or "")) == _slug(str(data.get("source") or ""))
    if service == "media_play":
        return status == "playing"
    if service == "media_pause":
        return status == "paused"
    if service == "media_stop":
        return status in {"idle", "off", "standby"}
    if service == "play_media":
        content = str(data.get("media_content_id") or "")
        return status in {"playing", "buffering", "on", "idle"} or (
            content and str(attrs.get("media_content_id") or "") == content
        )
    return True


def _entity_match_score(query: str, row: dict[str, Any]) -> int:
    entity_id = str(row.get("entity_id") or "")
    friendly = str((row.get("attributes") or {}).get("friendly_name") or "")
    q = _slug(query)
    eid = _slug(entity_id)
    object_id = _slug(entity_id.split(".", 1)[-1])
    name = _slug(friendly)
    if query.lower() == entity_id.lower():
        return 110
    if q in {eid, object_id, name}:
        return 100
    q_tokens = set(q.split("_"))
    name_tokens = set(name.split("_")) | set(object_id.split("_"))
    if q_tokens and q_tokens <= name_tokens:
        return 80 + len(q_tokens)
    if q and (q in name or q in object_id):
        return 60
    overlap = len(q_tokens & name_tokens)
    return overlap * 10 if overlap else 0


def _normalize_volume(level: float) -> float:
    value = float(level)
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (value or "").lower())).strip("_")


def _state_reachable(state: dict[str, Any]) -> bool:
    return str(state.get("state") or "").lower() not in _UNREACHABLE_STATES


def _error_text(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return f"Home Assistant HTTP {exc.response.status_code}"
    return str(exc) or exc.__class__.__name__


def _role_label(role: str) -> str:
    return "Apple TV" if role == "apple_tv" else "LG TV"


def _summarize(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_summarize_one(s) for s in states]


def _summarize_one(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes") or {}
    keep = {k: attrs[k] for k in _KEEP_ATTRS if k in attrs}
    return {
        "entity_id": state.get("entity_id"),
        "state": state.get("state"),
        "reachable": str(state.get("state") or "").lower() not in _UNREACHABLE_STATES,
        "attributes": keep,
    }


def speak_player(state: dict[str, Any] | None, *, label: str) -> str:
    """One short sentence the voice agent can say about a media_player."""
    if not state:
        return f"{label} is not available."
    name = (state.get("attributes") or {}).get("friendly_name") or state.get("entity_id") or label
    status = state.get("state") or "unknown"
    attrs = state.get("attributes") or {}
    bits = [f"{name} is {status}"]
    if attrs.get("source"):
        bits.append(f"source {attrs['source']}")
    if attrs.get("media_title"):
        title = attrs["media_title"]
        artist = attrs.get("media_artist")
        bits.append(f"playing {title}" + (f" by {artist}" if artist else ""))
    if attrs.get("volume_level") is not None and status not in {"off", "unavailable", "unknown"}:
        vol = float(attrs["volume_level"])
        bits.append(f"volume {round(vol * 100)}%")
    if attrs.get("is_volume_muted"):
        bits.append("muted")
    return ", ".join(bits) + "."


ha = HomeAssistant()
