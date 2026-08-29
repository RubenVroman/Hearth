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
                "ratingKey": "1001",
                "key": "/library/metadata/1001",
                "guid": "plex://movie/dune-part-two",
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

# Library titles for mock search / play (not only now-playing sessions).
MOCK_PLEX_LIBRARY: list[dict[str, Any]] = [
    {
        "title": "Dune: Part Two",
        "type": "movie",
        "year": 2024,
        "ratingKey": "1001",
        "key": "/library/metadata/1001",
        "guid": "plex://movie/dune-part-two",
    },
    {
        "title": "The Endless",
        "type": "movie",
        "year": 2017,
        "ratingKey": "2042",
        "key": "/library/metadata/2042",
        "guid": "plex://movie/the-endless",
    },
    {
        "title": "The Brutalist",
        "type": "movie",
        "year": 2024,
        "ratingKey": "1002",
        "key": "/library/metadata/1002",
        "guid": "plex://movie/the-brutalist",
    },
]

MOCK_PLEX_CLIENTS: list[dict[str, Any]] = [
    {
        "name": "Apple TV",
        "host": "192.168.1.40",
        "machineIdentifier": "mock-apple-tv",
        "product": "Plex for Apple TV",
        "deviceClass": "stb",
        "version": "8.0",
        "protocolCapabilities": ["timeline", "playback", "navigation", "playqueues"],
        "controllable": True,
    },
    {
        "name": "LG webOS TV",
        "host": "192.168.1.41",
        "machineIdentifier": "mock-lg-webos",
        "product": "Plex for LG",
        "deviceClass": "tv",
        "version": "5.0",
        "protocolCapabilities": ["timeline", "playback", "navigation"],
        "controllable": True,
    },
]

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


MOCK_RADARR_LOOKUP: list[dict[str, Any]] = [
    {
        "title": "Dune: Part Two",
        "year": 2024,
        "tmdbId": 693134,
        "overview": "Paul Atreides unites with Chani and the Fremen.",
        "status": "released",
    },
    {
        "title": "The Brutalist",
        "year": 2024,
        "tmdbId": 974950,
        "overview": "A Hungarian-born Jewish architect starts over in America.",
        "status": "released",
    },
]

MOCK_SONARR_LOOKUP: list[dict[str, Any]] = [
    {
        "title": "Severance",
        "year": 2022,
        "tvdbId": 371980,
        "overview": "Mark Scout leads a team whose memories are split.",
        "status": "continuing",
    },
    {
        "title": "Slow Horses",
        "year": 2022,
        "tvdbId": 397382,
        "overview": "Misfit spies at MI5's Slough House.",
        "status": "continuing",
    },
]

MOCK_OVERSEERR_RESULTS: list[dict[str, Any]] = [
    {"id": 693134, "mediaType": "movie", "title": "Dune: Part Two", "year": 2024},
    {"id": 95396, "mediaType": "tv", "title": "Severance", "year": 2022},
]


class MockPipeline:
    """In-memory Radarr / Sonarr / Overseerr so grab/request works without keys."""

    def __init__(self) -> None:
        self.radarr_queue: list[dict[str, Any]] = []
        self.sonarr_queue: list[dict[str, Any]] = []
        self.overseerr_queue: list[dict[str, Any]] = []

    def search_radarr(self, query: str) -> list[dict[str, Any]]:
        return _filter_title(MOCK_RADARR_LOOKUP, query)

    def search_sonarr(self, query: str) -> list[dict[str, Any]]:
        return _filter_title(MOCK_SONARR_LOOKUP, query)

    def search_overseerr(self, query: str) -> list[dict[str, Any]]:
        return _filter_title(MOCK_OVERSEERR_RESULTS, query)

    def add_radarr(self, item: dict[str, Any]) -> dict[str, Any]:
        queued = {**item, "queued": True}
        self.radarr_queue.append(queued)
        return queued

    def add_sonarr(self, item: dict[str, Any]) -> dict[str, Any]:
        queued = {**item, "queued": True}
        self.sonarr_queue.append(queued)
        return queued

    def request_overseerr(self, item: dict[str, Any]) -> dict[str, Any]:
        queued = {**item, "requested": True}
        self.overseerr_queue.append(queued)
        return queued


def _filter_title(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = (query or "").strip().lower()
    if not needle:
        return deepcopy(items[:5])
    hits = [deepcopy(item) for item in items if needle in str(item.get("title", "")).lower()]
    return hits or [deepcopy(items[0]) | {"title": items[0]["title"], "matched": "fallback"}]


pipeline = MockPipeline()


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
                state["attributes"]["volume_level"] = float(data["volume_level"])
            elif service == "volume_mute":
                state["attributes"]["is_volume_muted"] = bool(data.get("is_volume_muted", True))
            elif service == "volume_up":
                current = float(state["attributes"].get("volume_level") or 0)
                state["attributes"]["volume_level"] = min(1.0, round(current + 0.05, 2))
            elif service == "volume_down":
                current = float(state["attributes"].get("volume_level") or 0)
                state["attributes"]["volume_level"] = max(0.0, round(current - 0.05, 2))
            elif service == "toggle":
                state["state"] = "off" if state["state"] not in {"off", "unavailable"} else "on"
            elif service in {"turn_on", "media_play"}:
                if service == "turn_on":
                    state["state"] = "on"
                else:
                    state["state"] = "playing"
            elif service in {"turn_off", "media_stop"}:
                state["state"] = "off" if service == "turn_off" else "idle"
            elif service == "media_pause":
                state["state"] = "paused"
            elif service == "select_source" and "source" in data:
                state["attributes"]["source"] = data["source"]
            elif service == "play_media":
                state["state"] = "playing"
                if "media_content_id" in data:
                    state["attributes"]["media_content_id"] = data["media_content_id"]
                if "media_content_type" in data:
                    state["attributes"]["media_content_type"] = data["media_content_type"]
        return {"ok": True, "entity": deepcopy(state)}

    def _set_light(self, entity_id: str, on_off: str, brightness: int) -> None:
        state = next((s for s in self.states if s["entity_id"] == entity_id), None)
        if state is None:
            return
        state["state"] = on_off
        state["attributes"]["brightness"] = brightness
