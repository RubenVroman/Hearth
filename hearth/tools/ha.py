from __future__ import annotations

from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import MockHouse

_mock = MockHouse()


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


def _summarize(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_summarize_one(s) for s in states]


def _summarize_one(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes") or {}
    keep = {
        k: attrs[k]
        for k in (
            "friendly_name",
            "brightness",
            "color_temp",
            "rgb_color",
            "source",
            "volume_level",
            "is_volume_muted",
            "media_title",
            "media_artist",
        )
        if k in attrs
    }
    return {
        "entity_id": state.get("entity_id"),
        "state": state.get("state"),
        "attributes": keep,
    }


ha = HomeAssistant()
