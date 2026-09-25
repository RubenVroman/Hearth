"""The conversation presents truthful, complete findings without UI interaction."""

import asyncio

import pytest

from hearth.overlay_context import evaluate_widget, topics_for_widget
from hearth.runtime import runtime
from hearth.tools import suggest
from hearth.widgets import is_visual, publish_tool


def media_result(name="suggest_titles", **data):
    return {"name": name, "ok": True, "data": {"ok": True, **data}}


def test_full_recommendation_list_replaces_previous_topic():
    publish_tool(media_result("plex_search", results=[{"title": "Old title", "ratingKey": "old"}]))
    rows = [{"title": f"Pick {n}", "tmdbId": n, "year": 2000 + n} for n in range(1, 13)]
    widget = publish_tool(media_result(results=rows, query="Twelve horror films"))
    assert len(widget.data["items"]) == 12
    assert [row["title"] for row in widget.data["items"]] == [row["title"] for row in rows]
    assert widget.data["heading"] == "Twelve horror films"
    assert widget.data["presentation"] == "board"
    assert "Old title" not in str(widget.data)


@pytest.mark.asyncio
async def test_large_explicit_list_reports_its_bound_instead_of_silently_losing_titles(monkeypatch):
    async def resolve(title, *, media_type):
        return {"title": title, "type": "movie", "skeleton": False}

    monkeypatch.setattr(suggest, "resolve_title", resolve)
    result = await suggest.suggest_titles({"titles": [f"Film {n}" for n in range(15)]})
    widget = publish_tool({"name": "suggest_titles", "ok": True, "data": result})
    assert len(widget.data["items"]) == 12
    assert widget.data["total"] == 15
    assert widget.data["truncated"] is True


@pytest.mark.parametrize("ok", [True, False])
def test_empty_or_failed_lookup_clears_stale_titles(ok):
    publish_tool(media_result(results=[{"title": "Old title", "tmdbId": 1}]))
    widget = publish_tool({"name": "overseerr_search", "ok": ok,
                           "data": {"ok": ok, "query": "Unknown", "results": []}})
    assert widget.data["items"] == []
    assert widget.data["empty"] is True
    assert widget.status == ("info" if ok else "error")
    assert widget.body
    assert "Old title" not in str(widget.data)


def test_shelf_presents_distinct_titles_and_context():
    widget = publish_tool(media_result("house_shelf", continue_watching=[
        {"title": "Severance", "ratingKey": "1", "type": "show", "progress_pct": 40},
    ], recently_added=[
        {"title": "Severance", "ratingKey": "1", "type": "show"},
        {"title": "Arrival", "ratingKey": "2", "type": "movie"},
    ]))
    assert len(widget.data["items"]) == 2
    first = widget.data["items"][0]
    assert first["reason"] == "Continue watching"
    assert first["availability"] == "On Plex"
    assert first["progress_pct"] == 40


def test_suggestion_endpoint_preserves_the_whole_explicit_list(client, monkeypatch):
    async def resolve(title, *, media_type):
        return {"title": title, "type": "movie", "skeleton": True}

    monkeypatch.setattr(suggest, "resolve_title", resolve)
    titles = [f"Film {n}" for n in range(8)]
    response = client.post("/api/media/suggest", json={"titles": titles})
    assert response.status_code == 200
    assert [row["title"] for row in response.json()["results"]] == titles
    widget = next(row for row in response.json()["widgets"] if row["kind"] == "media")
    assert len(widget["data"]["items"]) == 8
    assert client.post("/api/media/suggest", json={"titles": titles, "limit": 13}).status_code == 422


def test_realtime_endpoint_forwards_execution_identity(client, monkeypatch):
    from hearth.voice import webrtc

    received = []

    async def run(name, args, **kwargs):
        received.append((name, args, kwargs))
        return {"ok": True, "data": {"ok": True}}

    monkeypatch.setattr(webrtc, "run_house_tool", run)
    response = client.post("/api/realtime/tools", json={
        "name": "get_weather", "arguments": {}, "call_id": "tool-call",
        "session_id": "voice-session", "said": "Weather please",
    })
    assert response.status_code == 200
    assert received == [("get_weather", {}, {
        "said": "Weather please", "execution_id": "tool-call", "session_id": "voice-session",
    })]


def test_web_results_are_visual_and_preserve_sources_without_unsafe_urls():
    runtime.note("user", "Search for meteor showers")
    widget = publish_tool({"name": "web_search", "ok": True, "data": {
        "query": "Meteor showers", "summary": "Three upcoming events.", "mode": "live",
        "results": [
            {"title": "Meteor guide", "url": "https://science.nasa.gov/meteors/", "snippet": "Watch after dark."},
            {"title": "Bad link", "url": "javascript:alert(1)", "snippet": "Untrusted text"},
            {"title": "Credentials", "url": "https://secret:password@example.com/"},
        ],
    }})
    assert is_visual(widget.kind)
    assert widget.kind == "information"
    assert widget.body == "Three upcoming events."
    assert widget.data["items"][0]["body"] == "Watch after dark."
    assert len(widget.data["sources"]) == 1
    assert widget.data["items"][1]["url"] is None
    assert widget.data["items"][2]["url"] is None
    assert "meteors" in topics_for_widget(widget) or "meteor" in topics_for_widget(widget)
    assert evaluate_widget(widget).relevant


def test_web_failure_replaces_previous_sources():
    publish_tool({"name": "web_search", "ok": True, "data": {
        "results": [{"title": "Old", "url": "https://example.com/"}],
    }})
    widget = publish_tool({"name": "web_search", "ok": False, "data": {
        "speak": "Search is temporarily unavailable.", "results": [{"title": "Stale"}],
        "summary": "Stale answer",
    }})
    assert widget.status == "error"
    assert widget.data["items"] == []
    assert widget.data["sources"] == []
    assert widget.data["summary"] == ""
    assert "unavailable" in widget.body


@pytest.mark.asyncio
async def test_recommendations_resolve_concurrently_but_keep_order(monkeypatch):
    running = peak = 0
    ready = asyncio.Event()

    async def resolve(title, *, media_type):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == suggest.LOOKUP_CONCURRENCY:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=1)
        await asyncio.sleep(0)
        running -= 1
        return {"title": title, "skeleton": False}

    monkeypatch.setattr(suggest, "resolve_title", resolve)
    result = await suggest.suggest_titles({"titles": [f"Film {n}" for n in range(8)]})
    assert peak == suggest.LOOKUP_CONCURRENCY
    assert [row["title"] for row in result["results"]] == [f"Film {n}" for n in range(8)]


@pytest.mark.asyncio
async def test_slow_title_does_not_drop_other_recommendations(monkeypatch):
    async def resolve(title, *, media_type):
        if title == "Slow":
            await asyncio.sleep(10)
        return {"title": title, "skeleton": False}

    monkeypatch.setattr(suggest, "resolve_title", resolve)
    monkeypatch.setattr(suggest, "LOOKUP_TIMEOUT_SECONDS", 0.01)
    result = await suggest.suggest_titles({"titles": ["Fast", "Slow", "Fast"], "limit": 3})
    assert [row["title"] for row in result["results"]] == ["Fast", "Slow"]
    assert result["results"][0]["skeleton"] is False
    assert result["results"][1]["skeleton"] is True
    assert result["results"][1]["reason"] == "Metadata unavailable"
