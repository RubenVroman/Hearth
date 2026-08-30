from __future__ import annotations

from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import MockHouse

_mock = MockHouse()

# Domains the agent cares about for house control / media.
_MEDIA_DOMAINS = ("media_player", "remote", "switch")
_KEEP_ATTRS = (
    "friendly_name",
    "brightness",
    "color_temp",
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
)


class HomeAssistant:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def live(self) -> bool:
        return settings.ha_configured

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.ha_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {settings.ha_token}",
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> dict[str, Any]:
        if not self.live:
            return {"ok": True, "mode": "mock"}
        client = await self._http()
        try:
            response = await client.get("/api/")
            response.raise_for_status()
            return {"ok": True, "mode": "live", "ha": response.json()}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {"ok": True, "mode": "mock", "error": str(exc)}
            return {"ok": False, "mode": "live", "error": str(exc)}

    async def list_states(self, domain: str | None = None) -> dict[str, Any]:
        if not self.live:
            states = _mock.list_states(domain)
            return {"mode": "mock", "states": _summarize(states)}
        client = await self._http()
        try:
            response = await client.get("/api/states")
            response.raise_for_status()
            states = response.json()
            if domain:
                prefix = domain.rstrip(".") + "."
                states = [s for s in states if str(s.get("entity_id", "")).startswith(prefix)]
            return {"mode": "live", "states": _summarize(states)}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {
                    "mode": "mock",
                    "error": str(exc),
                    "states": _summarize(_mock.list_states(domain)),
                }
            raise

    async def list_media_entities(self) -> dict[str, Any]:
        """media_player / remote / switch — what Hearth uses for TV & AVR."""
        collected: list[dict[str, Any]] = []
        mode = "mock"
        errors: list[str] = []
        for domain in _MEDIA_DOMAINS:
            result = await self.list_states(domain)
            mode = result.get("mode") or mode
            if result.get("error"):
                errors.append(str(result["error"]))
            collected.extend(result.get("states") or [])
        out: dict[str, Any] = {"mode": mode, "states": collected}
        if errors:
            out["error"] = "; ".join(errors)
        return out

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        if not self.live:
            state = _mock.get_state(entity_id)
            return {"mode": "mock", "state": state, "ok": state is not None}
        client = await self._http()
        try:
            response = await client.get(f"/api/states/{entity_id}")
            if response.status_code == 404:
                return {"mode": "live", "ok": False, "error": "not found"}
            response.raise_for_status()
            return {"mode": "live", "ok": True, "state": _summarize_one(response.json())}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                state = _mock.get_state(entity_id)
                return {"mode": "mock", "ok": state is not None, "state": state, "error": str(exc)}
            raise

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
            return {"mode": "mock", **result}
        client = await self._http()
        try:
            response = await client.post(f"/api/services/{domain}/{service}", json=payload)
            response.raise_for_status()
            return {"mode": "live", "ok": True, "changed": response.json()}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                result = _mock.call_service(domain, service, entity_id, data)
                return {"mode": "mock", "error": str(exc), **result}
            raise

    def entity_for(self, device: str) -> str:
        """Map tv|avr|apple_tv (and aliases) to configured HA entity_id."""
        key = (device or "").strip().lower().replace("-", "_").replace(" ", "_")
        if key in {"tv", "lg", "webos", "lg_tv", "lg_webos_tv", "television"}:
            return settings.ha_tv_entity.strip() or "media_player.lg_webos_tv"
        if key in {"avr", "denon", "receiver", "amp", "denon_avr", "denon_avr_x3700h"}:
            return settings.ha_avr_entity.strip() or "media_player.denon_avr_x3700h"
        if key in {
            "apple_tv",
            "appletv",
            "atv",
            "infuse",
            "living_room_apple_tv",
        }:
            return settings.ha_apple_tv_entity.strip() or "media_player.apple_tv"
        raise ValueError(f"unknown media device {device!r}; use tv, avr, or apple_tv")

    async def resolve_device_state(self, device: str) -> dict[str, Any]:
        entity_id = self.entity_for(device)
        result = await self.get_state(entity_id)
        state = result.get("state")
        if result.get("ok") is False or state is None:
            # Fallback: fuzzy match by friendly name when env entity_id is stale.
            fuzzy = await self._find_by_hint(device)
            if fuzzy:
                return {
                    "mode": result.get("mode") or "live",
                    "ok": True,
                    "device": device,
                    "entity_id": fuzzy.get("entity_id"),
                    "state": fuzzy,
                    "resolved": "fuzzy",
                }
            hint = (
                "set HA_APPLE_TV_ENTITY after pairing the Apple TV integration"
                if self._device_role(device) == "apple_tv"
                else "set HA_TV_ENTITY / HA_AVR_ENTITY after pairing"
            )
            return {
                "mode": result.get("mode") or "live",
                "ok": False,
                "device": device,
                "entity_id": entity_id,
                "error": result.get("error") or f"{entity_id} not found — {hint}",
            }
        return {
            "mode": result.get("mode"),
            "ok": True,
            "device": device,
            "entity_id": entity_id,
            "state": state,
            "resolved": "config",
        }

    def _device_role(self, device: str) -> str:
        key = (device or "").strip().lower().replace("-", "_").replace(" ", "_")
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
        if role == "apple_tv":
            hints = ("apple tv", "apple_tv", "appletv", "atv")
        elif role == "tv":
            hints = ("lg", "webos", "tv")
        else:
            hints = ("denon", "avr", "receiver", "heos")
        for row in states:
            eid = str(row.get("entity_id") or "").lower()
            name = str((row.get("attributes") or {}).get("friendly_name") or "").lower()
            blob = f"{eid} {name}"
            # Prefer Apple TV over generic "tv" substring when resolving apple_tv.
            if role == "apple_tv":
                if any(h in blob for h in hints):
                    return row
                continue
            if role == "tv" and ("apple" in blob or "atv" in eid):
                continue
            if any(h in blob for h in hints):
                return row
        return None

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
        """Control LG TV or Denon AVR via media_player.* HA services."""
        action = (action or "").strip().lower()
        resolved = await self.resolve_device_state(device)
        entity_id = str(resolved.get("entity_id") or self.entity_for(device))
        if not resolved.get("ok") and action not in {"turn_on", "turn_off"}:
            # Still allow turn_on/off against configured entity even if state fetch failed
            # (TV off often means entity is unavailable until powered).
            if resolved.get("error") and "not found" in str(resolved.get("error")):
                return {**resolved, "action": action}

        domain = entity_id.split(".", 1)[0] if "." in entity_id else "media_player"
        data: dict[str, Any] = {}
        service: str

        if action in {"turn_on", "on", "power_on"}:
            service = "turn_on"
        elif action in {"turn_off", "off", "power_off"}:
            service = "turn_off"
        elif action in {"toggle"}:
            service = "toggle"
        elif action in {"volume_set", "set_volume", "volume"}:
            if volume_level is None:
                return {"ok": False, "error": "volume_level required (0.0–1.0 or 0–100)"}
            service = "volume_set"
            data["volume_level"] = _normalize_volume(volume_level)
        elif action in {"volume_mute", "mute"}:
            service = "volume_mute"
            data["is_volume_muted"] = True if is_volume_muted is None else bool(is_volume_muted)
        elif action in {"volume_unmute", "unmute"}:
            service = "volume_mute"
            data["is_volume_muted"] = False
        elif action in {"volume_up"}:
            service = "volume_up"
        elif action in {"volume_down"}:
            service = "volume_down"
        elif action in {"select_source", "source"}:
            if not source:
                return {"ok": False, "error": "source required"}
            service = "select_source"
            data["source"] = source
        elif action in {"play_media"}:
            if not media_content_id or not media_content_type:
                return {
                    "ok": False,
                    "error": "media_content_id and media_content_type required for play_media",
                }
            service = "play_media"
            data["media_content_id"] = media_content_id
            data["media_content_type"] = media_content_type
        elif action in {"media_play", "play"}:
            service = "media_play"
        elif action in {"media_pause", "pause"}:
            service = "media_pause"
        elif action in {"media_stop", "stop"}:
            service = "media_stop"
        elif action in {"media_next_track", "next", "skip"}:
            service = "media_next_track"
        elif action in {"media_previous_track", "previous", "back"}:
            service = "media_previous_track"
        else:
            return {
                "ok": False,
                "error": (
                    f"unknown action {action!r}; use turn_on, turn_off, volume_set, "
                    "volume_mute, unmute, volume_up, volume_down, select_source, play_media, "
                    "media_play, media_pause, media_stop, media_next_track, media_previous_track"
                ),
            }

        result = await self.call_service(domain, service, entity_id, data or None)
        # Re-read state when possible so the agent can speak the outcome.
        after = await self.get_state(entity_id)
        return {
            "ok": result.get("ok", True) is not False,
            "device": self._device_role(device),
            "action": action,
            "entity_id": entity_id,
            "service": f"{domain}.{service}",
            "data": data or None,
            "mode": result.get("mode"),
            "result": result,
            "state": after.get("state"),
            "error": result.get("error"),
        }


def _normalize_volume(level: float) -> float:
    value = float(level)
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _summarize(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_summarize_one(s) for s in states]


def _summarize_one(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes") or {}
    keep = {k: attrs[k] for k in _KEEP_ATTRS if k in attrs}
    return {
        "entity_id": state.get("entity_id"),
        "state": state.get("state"),
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
        bits.append(f"volume {int(round(vol * 100))}%")
    if attrs.get("is_volume_muted"):
        bits.append("muted")
    return ", ".join(bits) + "."


ha = HomeAssistant()
