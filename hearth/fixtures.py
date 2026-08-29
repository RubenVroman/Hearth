"""House-shaped fixtures used when HA / Plex / Docker are unconfigured."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MOCK_HA_STATES: list[dict[str, Any]] = [
    {
        "entity_id": "light.living_room",
        "state": "on",
        "attributes": {
            "friendly_name": "Living room",
            "brightness": 180,
            "color_temp": 370,
        },
    },
    {
        "entity_id": "light.kitchen",
        "state": "off",
        "attributes": {"friendly_name": "Kitchen", "brightness": 0},
    },
    {
        "entity_id": "light.office",
        "state": "on",
        "attributes": {"friendly_name": "Office", "brightness": 120},
    },
    {
        "entity_id": "scene.movie_night",
        "state": "off",
        "attributes": {"friendly_name": "Movie night"},
    },
    {
        "entity_id": "scene.good_night",
        "state": "off",
        "attributes": {"friendly_name": "Good night"},
    },
    {
        "entity_id": "media_player.denon_avr_x3700h",
        "state": "playing",
        "attributes": {
            "friendly_name": "Denon AVR-X3700H",
            "source": "Media Player",
            "volume_level": 0.32,
            "is_volume_muted": False,
            "media_title": "Dune: Part Two",
        },
    },
    {
        "entity_id": "media_player.lg_webos_tv",
        "state": "on",
        "attributes": {
            "friendly_name": "LG webOS TV",
            "source": "HDMI 1",
            "volume_level": 0.0,
        },
    },
]

MOCK_PLEX_SESSIONS: dict[str, Any] = {
    "MediaContainer": {
        "size": 1,
        "Metadata": [
            {
                "title": "Dune: Part Two",
                "type": "movie",
                "year": 2024,
                "duration": 16600000,
                "viewOffset": 4920000,
                "Player": {
                    "title": "Living Room Shield",
                    "state": "playing",
                    "local": True,
                },
                "User": {"title": "Ruben"},
                "grandparentTitle": None,
            }
        ],
    }
}

MOCK_DOCKER_CONTAINERS: list[dict[str, Any]] = [
    {"Id": "plex01", "Names": ["/plex"], "Image": "lscr.io/linuxserver/plex", "State": "running", "Status": "Up 3 days"},
    {"Id": "sonarr01", "Names": ["/sonarr"], "Image": "lscr.io/linuxserver/sonarr", "State": "running", "Status": "Up 3 days"},
    {"Id": "radarr01", "Names": ["/radarr"], "Image": "lscr.io/linuxserver/radarr", "State": "running", "Status": "Up 3 days"},
    {"Id": "prowlarr01", "Names": ["/prowlarr"], "Image": "lscr.io/linuxserver/prowlarr", "State": "running", "Status": "Up 3 days"},
    {"Id": "overseerr01", "Names": ["/overseerr"], "Image": "lscr.io/linuxserver/overseerr", "State": "running", "Status": "Up 3 days"},
    {"Id": "gluetun01", "Names": ["/gluetun"], "Image": "qmcgaw/gluetun", "State": "running", "Status": "Up 3 days"},
    {"Id": "hearth01", "Names": ["/hearth"], "Image": "hearth:local", "State": "running", "Status": "Up 12 minutes"},
    {"Id": "ha01", "Names": ["/hearth-ha"], "Image": "home-assistant", "State": "running", "Status": "Up 12 minutes"},
]


class MockHouse:
    """Mutable in-memory house so mocked lights actually toggle in the UI."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = deepcopy(MOCK_HA_STATES)

    def list_states(self, domain: str | None = None) -> list[dict[str, Any]]:
        if not domain:
            return deepcopy(self.states)
        prefix = domain.rstrip(".") + "."
        return deepcopy([s for s in self.states if s["entity_id"].startswith(prefix)])

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        for state in self.states:
            if state["entity_id"] == entity_id:
                return deepcopy(state)
        return None

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = data or {}
        state = next((s for s in self.states if s["entity_id"] == entity_id), None)
        if state is None:
            return {"ok": False, "error": f"unknown entity {entity_id}"}
        if domain == "light":
            if service == "turn_on":
                state["state"] = "on"
                if "brightness" in data:
                    state["attributes"]["brightness"] = data["brightness"]
                elif not state["attributes"].get("brightness"):
                    state["attributes"]["brightness"] = 180
            elif service == "turn_off":
                state["state"] = "off"
                state["attributes"]["brightness"] = 0
            elif service == "toggle":
                state["state"] = "off" if state["state"] == "on" else "on"
        elif domain == "scene" and service == "turn_on":
            state["state"] = "on"
            if entity_id == "scene.movie_night":
                self._set_light("light.living_room", "on", 40)
                self._set_light("light.kitchen", "off", 0)
                self._set_light("light.office", "off", 0)
            elif entity_id == "scene.good_night":
                for light in ("light.living_room", "light.kitchen", "light.office"):
                    self._set_light(light, "off", 0)
        elif domain == "media_player":
            if service == "volume_set" and "volume_level" in data:
                state["attributes"]["volume_level"] = data["volume_level"]
            elif service == "volume_mute":
                state["attributes"]["is_volume_muted"] = bool(data.get("is_volume_muted", True))
            elif service in {"turn_on", "media_play"}:
                state["state"] = "playing" if domain else "on"
                if service == "turn_on":
                    state["state"] = "on"
            elif service in {"turn_off", "media_stop"}:
                state["state"] = "off" if service == "turn_off" else "idle"
            elif service == "select_source" and "source" in data:
                state["attributes"]["source"] = data["source"]
        return {"ok": True, "entity": deepcopy(state)}

    def _set_light(self, entity_id: str, on_off: str, brightness: int) -> None:
        state = next((s for s in self.states if s["entity_id"] == entity_id), None)
        if state is None:
            return
        state["state"] = on_off
        state["attributes"]["brightness"] = brightness
