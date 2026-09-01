from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from hearth.config import settings
from hearth.tools.arr import Overseerr, OverseerrError


def _live_gateway(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[Overseerr, httpx.AsyncClient]:
    monkeypatch.setattr(settings, "overseerr_url", "http://overseerr.test/api/v1/")
    monkeypatch.setattr(settings, "overseerr_api_key", "configured-live-key")
    monkeypatch.setattr(settings, "mock_if_unconfigured", True)
    http = httpx.AsyncClient(
        base_url="http://overseerr.test",
        headers={"X-Api-Key": "configured-live-key", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    gateway = Overseerr()
    gateway._client = http
    gateway._client_signature = ("http://overseerr.test", "configured-live-key")
    return gateway, http


@pytest.mark.asyncio
async def test_search_normalizes_official_payload_and_sends_page_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "page": 1,
                "totalPages": 2,
                "totalResults": 20,
                "results": [
                    {
                        "id": 438631,
                        "mediaType": "movie",
                        "title": "Dune",
                        "releaseDate": "2021-09-15",
                        "overview": "A noble family becomes embroiled in a war.",
                        "mediaInfo": {
                            "id": 7001,
                            "tmdbId": 438631,
                            "status": 5,
                            "requests": [{"id": 91, "status": 2}],
                        },
                    },
                    {
                        "id": 42,
                        "mediaType": "person",
                        "name": "Someone",
                        "knownFor": [],
                    },
                ],
            },
        )

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.search("Dune")
    finally:
        await gateway.aclose()

    assert result["ok"] is True
    assert result["mode"] == "live"
    assert result["page"] == 1
    assert result["totalPages"] == 2
    assert result["totalResults"] == 20
    assert len(result["results"]) == 2
    dune = result["results"][0]
    assert dune["title"] == "Dune"
    assert dune["year"] == "2021"
    assert dune["tmdbId"] == 438631
    assert dune["mediaStatus"] == 5
    assert dune["requestStatus"] == 2
    assert dune["requestId"] == 91
    assert dune["inLibrary"] is True
    assert result["results"][1]["mediaType"] == "person"
    assert seen[0].url.path == "/api/v1/search"
    assert dict(seen[0].url.params) == {"query": "Dune", "page": "1"}
    assert seen[0].headers["X-Api-Key"] == "configured-live-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 500])
async def test_configured_search_failure_never_falls_back_to_mock(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "backend failed"})

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        with pytest.raises(OverseerrError) as raised:
            await gateway.search("Fight Club")
    finally:
        await gateway.aclose()

    assert raised.value.operation == "search"
    assert raised.value.status_code == status_code


@pytest.mark.asyncio
async def test_configured_transport_failure_never_falls_back_or_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("overseerr unavailable", request=request)

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        with pytest.raises(OverseerrError):
            await gateway.request("Dune", media_id=438631, media_type="movie")
    finally:
        await gateway.aclose()

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_type", "seasons", "expected_seasons"),
    [("movie", None, None), ("tv", None, "all"), ("tv", [2, 4, 2], [2, 4])],
)
async def test_request_uses_official_json_and_normalizes_201_response(
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
    seasons: list[int] | None,
    expected_seasons: list[int] | str | None,
) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            201,
            json={
                "id": 91,
                "status": 1,
                "media": {
                    "id": 7001,
                    "tmdbId": 438631,
                    "status": 2,
                    "mediaType": media_type,
                },
            },
        )

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.request(
            "Dune",
            media_id=438631,
            media_type=media_type,
            seasons=seasons,
        )
    finally:
        await gateway.aclose()

    expected = {"mediaId": 438631, "mediaType": media_type, "is4k": False}
    if media_type == "tv":
        expected["seasons"] = expected_seasons
    assert bodies == [expected]
    assert result["ok"] is True
    assert result["status_code"] == 201
    assert result["requestId"] == 91
    assert result["requestStatus"] == 1
    assert result["mediaStatus"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "reason", "already"),
    [
        (202, "no_seasons", False),
        (403, "forbidden", False),
        (409, "already_requested", True),
    ],
)
async def test_request_handles_documented_non_201_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    reason: str,
    already: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "not accepted"})

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.request(
            "Dune: Prophecy",
            media_id=11733,
            media_type="tv",
            seasons="all",
        )
    finally:
        await gateway.aclose()

    assert result["ok"] is False
    assert result["mode"] == "live"
    assert result["status_code"] == status_code
    assert result["reason"] == reason
    assert bool(result.get("already")) is already


@pytest.mark.asyncio
async def test_empty_search_probes_provider_instead_of_claiming_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={"page": 1, "totalPages": 1, "totalResults": 0, "results": []},
            )
        assert request.url.path == "/api/v1/movie/550"
        return httpx.Response(503, json={"message": "TMDB unavailable"})

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.search("A title that should exist")
    finally:
        await gateway.aclose()

    assert paths == ["/api/v1/search", "/api/v1/movie/550"]
    assert result["ok"] is False
    assert result["reason"] == "provider_unavailable"
    assert result["providerOk"] is False
    assert result["provider"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_media_details_normalizes_status_and_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": 550,
                "title": "Fight Club",
                "releaseDate": "1999-10-15",
                "mediaInfo": {
                    "id": 22,
                    "tmdbId": 550,
                    "status": 5,
                    "requests": [
                        {"id": 80, "status": 1},
                        {"id": 91, "status": 2},
                    ],
                },
            },
        )

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.media_details(550, "movie")
    finally:
        await gateway.aclose()

    assert seen == ["/api/v1/movie/550"]
    assert result["ok"] is True
    assert result["mediaId"] == 550
    assert result["mediaType"] == "movie"
    assert result["mediaStatus"] == 5
    assert result["requestStatus"] == 2
    assert [row["id"] for row in result["requests"]] == [80, 91]
    assert result["media"]["inLibrary"] is True


@pytest.mark.asyncio
async def test_person_search_keeps_late_person_rows_and_uses_combined_credits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    movie_rows = [
        {
            "id": 1000 + index,
            "mediaType": "movie",
            "title": f"Tom Hanks Documentary {index}",
            "releaseDate": "2010-01-01",
        }
        for index in range(8)
    ]
    person = {
        "id": 31,
        # Some live multi-search payloads omit mediaType; knownFor identifies
        # the row as a person without inventing a title result.
        "name": "Tom Hanks",
        "popularity": 82.4,
        "knownFor": [{"id": 13, "mediaType": "movie", "title": "Forrest Gump"}],
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "totalPages": 1,
                    "results": [*movie_rows, person],
                },
            )
        if request.url.path == "/api/v1/person/31/combined_credits":
            return httpx.Response(
                200,
                json={
                    "id": 31,
                    "cast": [
                        {
                            "id": 13,
                            "mediaType": "movie",
                            "title": "Forrest Gump",
                            "releaseDate": "1994-07-06",
                        }
                    ],
                    "crew": [],
                },
            )
        return httpx.Response(404)

    gateway, http = _live_gateway(monkeypatch, handler)
    try:
        found = await gateway.search_person("tom hanks")
        credits = await gateway.person_combined_credits(31)
    finally:
        await gateway.aclose()

    assert found["results"][0]["id"] == 31
    assert found["results"][0]["mediaType"] == "person"
    assert found["results"][0]["knownFor"]
    assert credits["cast"][0]["title"] == "Forrest Gump"
    search = next(request for request in seen if request.url.path == "/api/v1/search")
    assert dict(search.url.params) == {"query": "tom hanks", "page": "1"}
