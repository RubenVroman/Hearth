from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import MOCK_DOCKER_CONTAINERS


class DockerHost:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def socket_path(self) -> Path:
        return Path(settings.docker_socket)

    @property
    def live(self) -> bool:
        return self.socket_path.exists()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
            self._client = httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost",
                timeout=10.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ps(self) -> dict[str, Any]:
        if not self.live:
            return {"mode": "mock", "containers": _summarize(MOCK_DOCKER_CONTAINERS)}
        client = await self._http()
        try:
            response = await client.get("/v1.41/containers/json", params={"all": "true"})
            response.raise_for_status()
            return {"mode": "live", "containers": _summarize(response.json())}
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {
                    "mode": "mock",
                    "error": str(exc),
                    "containers": _summarize(MOCK_DOCKER_CONTAINERS),
                }
            raise

    async def inspect(self, container: str) -> dict[str, Any]:
        if not self.live:
            match = next(
                (
                    c
                    for c in MOCK_DOCKER_CONTAINERS
                    if container in c["Id"] or container in "".join(c["Names"])
                ),
                None,
            )
            if match is None:
                return {"mode": "mock", "ok": False, "error": f"no mock container matching {container}"}
            return {"mode": "mock", "ok": True, "container": _summarize_one(match)}
        client = await self._http()
        try:
            response = await client.get(f"/v1.41/containers/{container}/json")
            if response.status_code == 404:
                return {"mode": "live", "ok": False, "error": "not found"}
            response.raise_for_status()
            body = response.json()
            return {
                "mode": "live",
                "ok": True,
                "container": {
                    "id": (body.get("Id") or "")[:12],
                    "name": body.get("Name"),
                    "image": (body.get("Config") or {}).get("Image"),
                    "state": (body.get("State") or {}).get("Status"),
                    "health": ((body.get("State") or {}).get("Health") or {}).get("Status"),
                    "ports": (body.get("NetworkSettings") or {}).get("Ports"),
                },
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                return {"mode": "mock", "ok": False, "error": str(exc)}
            raise

    async def stop(self, container: str) -> dict[str, Any]:
        if not self.live:
            return {"mode": "mock", "ok": True, "stopped": container, "note": "mock — nothing stopped"}
        client = await self._http()
        response = await client.post(f"/v1.41/containers/{container}/stop")
        if response.status_code not in {204, 304}:
            response.raise_for_status()
        return {"mode": "live", "ok": True, "stopped": container, "status_code": response.status_code}


def _summarize(containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_summarize_one(c) for c in containers]


def _summarize_one(container: dict[str, Any]) -> dict[str, Any]:
    names = container.get("Names") or []
    return {
        "id": str(container.get("Id", ""))[:12],
        "name": names[0] if names else container.get("name"),
        "image": container.get("Image"),
        "state": container.get("State"),
        "status": container.get("Status"),
    }


docker = DockerHost()
