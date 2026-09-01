from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from hearth.config import settings
from hearth.tools.arr import Overseerr, OverseerrError, _summarize_overseerr


def _live_gateway(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> Overseerr:
    monkeypatch.setattr(settings, "overseerr_url", "http://overseerr.test")
    monkeypatch.setattr(settings, "overseerr_api_key", "live-key")
    gateway = Overseerr()
    gateway._client = httpx.AsyncClient(
        base_url="http://overseerr.test",
        transport=httpx.MockTransport(handler),
    )
    gateway._client_signature = ("http://overseerr.test", "live-key")
    return gateway


@pytest.mark.asyncio
async def test_media_details_for_fresh_tmdb_title_forces_type_and_external_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Official details omit mediaType/mediaInfo when Seerr has never seen the title."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/movie/550"
        return httpx.Response(
            200,
            json={
                "id": 550,
                "title": "Fight Club",
                "releaseDate": "1999-10-15",
            },
        )

    gateway = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.media_details(550, "movie")
    finally:
        await gateway.aclose()

    assert result["ok"] is True
    assert result["media"]["mediaType"] == "movie"
    assert result["media"]["mediaId"] == 550
    assert result["media"]["tmdbId"] == 550
    assert result["media"]["title"] == "Fight Club"


@pytest.mark.asyncio
async def test_title_request_preserves_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = Overseerr()

    async def unavailable(query: str, *, page: int = 1) -> dict[str, Any]:
        return {
            "ok": False,
            "mode": "live",
            "service": "overseerr",
            "query": query,
            "page": page,
            "reason": "provider_unavailable",
            "providerOk": False,
            "results": [],
        }

    monkeypatch.setattr(gateway, "search", unavailable)
    result = await gateway.request("Fight Club")

    assert result["ok"] is False
    assert result["reason"] == "provider_unavailable"
    assert result.get("not_found") is not True
    assert "provider" in result["speak"].lower()


@pytest.mark.asyncio
async def test_explicit_year_never_auto_requests_only_wrong_year_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = Overseerr()

    async def wrong_year(query: str, *, page: int = 1) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "mock",
            "service": "overseerr",
            "query": query,
            "page": page,
            "results": [
                {
                    "title": "The Thing",
                    "year": 1982,
                    "mediaType": "movie",
                    "mediaId": 1091,
                    "tmdbId": 1091,
                }
            ],
        }

    monkeypatch.setattr(gateway, "search", wrong_year)
    result = await gateway.request("The Thing (2011)", media_type="movie")

    assert result["ok"] is False
    assert result["not_found"] is True
    assert result["results"][0]["year"] == 1982


@pytest.mark.asyncio
async def test_missing_type_for_tmdb_id_fails_closed_without_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = Overseerr()
    calls = 0

    async def should_not_search(query: str, *, page: int = 1) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True, "query": query, "page": page, "results": []}

    monkeypatch.setattr(gateway, "search", should_not_search)
    result = await gateway.request("", media_id=603)

    assert result["ok"] is False
    assert result["reason"] == "media_type_required"
    assert calls == 0


@pytest.mark.asyncio
async def test_search_preserves_up_to_twenty_official_page_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": 1000 + index,
            "mediaType": "movie",
            "title": f"Result {index}",
            "releaseDate": "2020-01-01",
            "posterPath": "/poster.jpg",
        }
        for index in range(12)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "page": 1,
                "totalPages": 1,
                "totalResults": len(rows),
                "results": rows,
            },
        )

    gateway = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.search("Result")
    finally:
        await gateway.aclose()

    assert result["ok"] is True
    assert len(result["results"]) == 12
    assert result["results"][-1]["tmdbId"] == 1011


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, text="not-json"),
    ],
)
async def test_malformed_successful_search_is_backend_error(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    gateway = _live_gateway(monkeypatch, handler)
    try:
        with pytest.raises(OverseerrError) as raised:
            await gateway.search("Fight Club")
    finally:
        await gateway.aclose()

    assert raised.value.operation == "search"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, json=[]),
        httpx.Response(201, json={}),
        httpx.Response(201, json={"id": 8, "status": 2}),
        httpx.Response(200, json={"id": 8, "status": 2, "media": {}}),
    ],
)
async def test_malformed_or_unexpected_successful_post_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    gateway = _live_gateway(monkeypatch, handler)
    try:
        with pytest.raises(OverseerrError) as raised:
            await gateway.request(
                "Fight Club",
                media_id=550,
                media_type="movie",
            )
    finally:
        await gateway.aclose()

    assert calls == 1
    assert raised.value.operation == "request"


@pytest.mark.asyncio
async def test_successful_post_for_different_media_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": 8,
                "status": 2,
                "media": {
                    "id": 80,
                    "tmdbId": 999,
                    "mediaType": "tv",
                    "status": 3,
                },
            },
        )

    gateway = _live_gateway(monkeypatch, handler)
    try:
        with pytest.raises(OverseerrError) as raised:
            await gateway.request("Fight Club", media_id=550, media_type="movie")
    finally:
        await gateway.aclose()

    assert raised.value.operation == "request"


def test_status_six_is_named_safely_across_overseerr_and_seerr() -> None:
    result = _summarize_overseerr(
        {
            "id": 550,
            "mediaType": "movie",
            "title": "Fight Club",
            "mediaInfo": {"status": 6},
        }
    )

    assert result["mediaStatus"] == 6
    assert result["mediaStatusLabel"] == "blocklisted_or_deleted"
    assert result["inLibrary"] is False


def test_normalized_search_preserves_original_language_title() -> None:
    result = _summarize_overseerr(
        {
            "id": 71446,
            "mediaType": "tv",
            "name": "La casa de papel",
            "originalName": "Money Heist",
        }
    )

    assert result["title"] == "La casa de papel"
    assert result["originalTitle"] == "Money Heist"


@pytest.mark.asyncio
async def test_empty_search_distinguishes_api_key_rejection_from_tmdb_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={"page": 1, "totalPages": 1, "totalResults": 0, "results": []},
            )
        assert request.url.path == "/api/v1/movie/550"
        return httpx.Response(401, json={"message": "invalid api key"})

    gateway = _live_gateway(monkeypatch, handler)
    try:
        result = await gateway.search("Definitely absent")
    finally:
        await gateway.aclose()

    assert result["ok"] is False
    assert result["reason"] == "authentication_failed"
    assert result["provider"]["status"] == "authentication_failed"
