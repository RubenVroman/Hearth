"""Glass info overlays — weather / media stack / downloads (no flickering update guards)."""

from datetime import datetime, timedelta, timezone

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.fixtures import pipeline
from hearth.overlay_context import (
    apply_media_focus,
    evaluate_widget,
    focus_media_from_text,
    text_matches_topics,
    topics_for_widget,
)
from hearth.runtime import Widget, runtime
from hearth.widgets import publish_tool


def test_command_center_includes_info_overlay(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="info-overlay"' in page.text
    assert 'id="widget-stack"' not in page.text
    js = client.get("/static/app.js")
    assert "openInfoOverlay" in js.text
    assert "softHideInfoOverlay" in js.text
    assert "noteOverlayConversation" in js.text
    assert "focusMediaFromText" in js.text
    assert "info-media-stack" in js.text
    assert "is-soft-hidden" in js.text
    assert "renderWidgets" in js.text
    assert "/api/media/art" in js.text
    assert "downloadsMarkup" in js.text
    assert "info-download-bar" in js.text
    assert "fadeMediaOverlayOnProgress" not in js.text
    assert "response.output_audio_transcript.delta" in js.text

    assert "/api/widgets/" in js.text
    assert "upsertLocalWidget" not in js.text
    assert 'kind: "action"' not in js.text
    css = client.get("/static/styles.css")
    assert ".info-overlay" in css.text
    assert ".info-glass" in css.text
    assert ".info-overlay.is-soft-hidden" in css.text
    assert ".info-media-stack" in css.text
    assert ".info-media-card" in css.text
    assert ".info-download-list" in css.text
    assert ".widget-stack" not in css.text
    assert "widget-in" not in css.text
    sw = client.get("/sw.js")
    assert "hearth-shell-v14" in sw.text


def test_weather_ask_surfaces_weather_overlay(client):
    chat = client.post("/api/chat", json={"message": "what's the weather"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "get_weather"
    assert "14" in body["reply"] or "Partly cloudy" in body["reply"]
    widgets = body["widgets"]
    kinds = {w["kind"] for w in widgets}
    assert "weather" in kinds
    assert "action" not in kinds
    weather = next(w for w in widgets if w["kind"] == "weather")
    assert weather["status"] == "done"
    assert "14" in weather["body"]
    assert "Partly cloudy" in weather["body"]
    assert weather["data"]["temperature"] == 14
    assert weather["context"]["relevant"] is True
    assert weather["context"]["reason"] in {"fresh", "tool_match", "topic_match", "active"}
    assert "weather" in weather["context"]["topics"]

    status = client.get("/api/status")
    assert status.status_code == 200
    assert any(w["kind"] == "weather" for w in status.json()["widgets"])


def test_no_action_update_guards_for_radarr(client):
    chat = client.post("/api/chat", json={"message": "download the movie Dune"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["needs_confirm"] is False
    kinds = {w["kind"] for w in body["widgets"]}
    assert "action" not in kinds
    assert "generic" not in kinds


def test_movie_ask_surfaces_media_overlay(client):
    chat = client.post("/api/chat", json={"message": "tell me about the movie Dune"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "plex_search"
    media = next(w for w in body["widgets"] if w["kind"] == "media")
    assert media["status"] == "done"
    assert "Dune" in media["title"]
    item = media["data"]["item"]
    assert item.get("summary")
    assert item.get("ratingKey") == "1001"
    assert media["context"]["relevant"] is True
    assert item.get("tmdbId") == 693134
    assert item.get("posterPath")
    assert media.get("sticky") is False
    items = media["data"]["items"]
    assert isinstance(items, list) and items
    assert media["data"]["active_id"] == items[0]["id"]
    assert items[0]["title"] == item["title"]

    thumb = client.get("/api/plex/thumb/1001")
    assert thumb.status_code == 200
    assert "image/" in thumb.headers.get("content-type", "")

    art = client.get("/api/media/art", params={"ratingKey": "1001", "tmdbId": 693134})
    assert art.status_code == 200
    assert "image/" in art.headers.get("content-type", "")


def test_media_search_stacks_multiple_hits(client):
    chat = client.post("/api/chat", json={"message": "tell me about the movie Heat"})
    assert chat.status_code == 200
    media = next(w for w in chat.json()["widgets"] if w["kind"] == "media")
    items = media["data"]["items"]
    assert len(items) >= 2
    titles = {str(row.get("title") or "") for row in items}
    assert "Heat" in titles
    assert media["data"]["active_id"]
    assert media["data"]["item"]["id"] == media["data"]["active_id"]


def test_media_stack_accumulates_across_searches(client):
    first = client.post("/api/chat", json={"message": "tell me about the movie Dune"})
    assert first.status_code == 200
    second = client.post("/api/chat", json={"message": "tell me about the movie The Endless"})
    assert second.status_code == 200
    media = next(w for w in second.json()["widgets"] if w["kind"] == "media")
    items = media["data"]["items"]
    titles = [str(row.get("title") or "") for row in items]
    assert any("Endless" in t for t in titles)
    assert any("Dune" in t for t in titles)
    assert "Endless" in media["title"]
    # Active card is the latest spoken title.
    assert "Endless" in str(media["data"]["item"].get("title") or "")


def test_media_active_card_follows_transcript(client):
    client.post("/api/chat", json={"message": "tell me about the movie Dune"})
    client.post("/api/chat", json={"message": "tell me about the movie The Endless"})
    media = runtime.get_widget("media")
    assert media is not None
    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    media.updated_at = past
    media.ts = past
    runtime.note("assistant", "Dune: Part Two is the one with the Fremen sandwalk.")
    rel = evaluate_widget(media)
    assert rel.relevant is True
    assert rel.reason == "topic_match"
    assert "Dune" in media.title
    hit = focus_media_from_text(media, "what about The Endless again")
    assert hit
    apply_media_focus(media, hit)
    assert "Endless" in media.title


def test_media_overlay_soft_hides_on_next_turn(client):
    """#27 policy: unrelated/ack turns keep media for reappear; do not hard-delete."""
    first = client.post("/api/chat", json={"message": "tell me about the movie Dune"})
    assert first.status_code == 200
    assert any(w["kind"] == "media" for w in first.json()["widgets"])

    second = client.post("/api/chat", json={"message": "thanks"})
    assert second.status_code == 200
    # Soft-hide memory: widget remains; relevance may stay active for a brief ack.
    assert runtime.get_widget("media") is not None
    kinds = {w["kind"] for w in second.json()["widgets"]}
    assert "media" in kinds


def test_media_art_endpoint_accepts_tmdb_and_falls_back(client):
    ok = client.get(
        "/api/media/art",
        params={"tmdbId": 693134, "mediaType": "movie", "title": "Dune: Part Two"},
    )
    assert ok.status_code == 200
    ctype = ok.headers.get("content-type", "")
    assert ctype.startswith("image/")
    # Real TMDB JPEG when CDN reachable; SVG placeholder otherwise.
    assert ctype in {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/svg+xml"} or ctype.startswith(
        "image/"
    )


def test_now_playing_surfaces_media_overlay(client):
    chat = client.post("/api/chat", json={"message": "what's playing"})
    assert chat.status_code == 200
    body = chat.json()
    media = next(w for w in body["widgets"] if w["kind"] == "media")
    assert "Dune" in media["title"]


def test_animation_genre_browse_surfaces_media_stack(client):
    chat = client.post("/api/chat", json={"message": "list animation movies"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "plex_browse_genre"
    media = next(w for w in body["widgets"] if w["kind"] == "media")
    items = media["data"].get("items") or []
    titles = {row.get("title") for row in items}
    assert "Spirited Away" in titles or "Spirited Away" in media["title"]
    assert "animation" in body["reply"].lower() or "Spirited" in body["reply"]


def test_download_progress_surfaces_downloads_overlay(client):
    chat = client.post("/api/chat", json={"message": "How far along is Annihilation?"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "radarr_queue"
    downloads = next(w for w in body["widgets"] if w["kind"] == "downloads")
    assert downloads["status"] == "done"
    assert "Annihilation" in downloads["title"]
    rows = downloads["data"]["downloads"]
    assert len(rows) == 1
    assert rows[0]["percent"] == 75.0
    assert rows[0]["status"] == "downloading"
    assert rows[0].get("sizeleft_label")
    assert downloads["data"].get("empty") is None

    listed = client.post("/api/chat", json={"message": "What's downloading right now?"})
    assert listed.status_code == 200
    panel = next(w for w in listed.json()["widgets"] if w["kind"] == "downloads")
    assert len(panel["data"]["downloads"]) >= 1
    assert panel["data"]["service"] == "radarr"


def test_download_progress_empty_and_missing_are_calm(client, monkeypatch):
    miss = client.post("/api/chat", json={"message": "Download progress for NotARealMovieXYZ"})
    assert miss.status_code == 200
    missing = next(w for w in miss.json()["widgets"] if w["kind"] == "downloads")
    assert missing["data"]["empty"] == "missing"
    assert missing["data"]["downloads"] == []
    assert "not" in missing["body"].lower() or "not" in missing["detail"].lower()

    monkeypatch.setattr(pipeline, "radarr_downloads", [])
    idle = client.post("/api/chat", json={"message": "What's downloading right now?"})
    assert idle.status_code == 200
    quiet = next(w for w in idle.json()["widgets"] if w["kind"] == "downloads")
    assert quiet["data"]["empty"] == "idle"
    assert quiet["data"]["downloads"] == []
    assert "nothing" in quiet["body"].lower()


def test_dismiss_overlay(client):
    client.post("/api/chat", json={"message": "what's the weather"})
    listed = client.get("/api/widgets")
    assert listed.status_code == 200
    widgets = listed.json()["widgets"]
    assert widgets
    wid = next(w["id"] for w in widgets if w["kind"] == "weather")
    gone = client.delete(f"/api/widgets/{wid}")
    assert gone.status_code == 200
    assert all(w["id"] != wid for w in gone.json()["widgets"])
    missing = client.delete(f"/api/widgets/{wid}")
    assert missing.status_code == 404


def test_intent_weather_movie_and_repo_weather_skill():
    assert route_intent("what's the weather outside")["tool"] == "get_weather"
    assert route_intent("add a weather skill to the repo")["tool"] == "chief_of_staff"
    about = route_intent("tell me about the movie The Endless")
    assert about is not None
    assert about["tool"] == "plex_search"
    assert "Endless" in about["args"]["query"]


def test_overlay_updated_at_stable_across_identical_upserts(client):
    client.post("/api/chat", json={"message": "what's the weather"})
    first = runtime.get_widget("weather")
    assert first is not None
    stamp = first.updated_at
    # Re-publish the same weather payload via status poll path (tool again).
    client.post("/api/chat", json={"message": "what's the weather"})
    second = runtime.get_widget("weather")
    assert second is not None
    assert second.updated_at == stamp


def test_overlay_stays_through_unrelated_turn_but_marks_irrelevant(client):
    """Smart hide keeps the widget for reappear; context.relevant flips off."""
    client.post("/api/chat", json={"message": "what's the weather"})
    assert runtime.get_widget("weather") is not None
    follow = client.post("/api/chat", json={"message": "turn on the living room lights"})
    assert follow.status_code == 200
    weather = runtime.get_widget("weather")
    assert weather is not None  # soft-hide, not hard delete
    listed = {w["id"]: w for w in follow.json()["widgets"]}
    assert "weather" in listed
    assert listed["weather"]["context"]["relevant"] is False
    assert listed["weather"]["context"]["reason"].startswith("unrelated:")


def test_overlay_hides_when_talk_leaves_without_domain_keyword(client):
    """Past the fresh window, generic chat must not keep a title panel stuck."""
    client.post("/api/chat", json={"message": "tell me about the movie Dune"})
    media = runtime.get_widget("media")
    assert media is not None
    past = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    media.updated_at = past
    media.ts = past
    runtime.note("user", "tell me a joke about chickens")
    rel = evaluate_widget(media)
    assert rel.relevant is False
    assert rel.reason in {"stale", "idle"}


def test_overlay_reappears_relevant_when_talk_returns(client):
    client.post("/api/chat", json={"message": "tell me about the movie Dune"})
    client.post("/api/chat", json={"message": "turn on the kitchen lights"})
    media = runtime.get_widget("media")
    assert media is not None
    # Age the update so we're past the fresh window; topic match should still win.
    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    media.updated_at = past
    media.ts = past
    runtime.note("user", "what was that Dune movie about again?")
    rel = evaluate_widget(media)
    assert rel.relevant is True
    assert rel.reason == "topic_match"


def test_overlay_idle_marks_irrelevant(monkeypatch):
    monkeypatch.setattr(
        "hearth.overlay_context.settings.overlay_fresh_seconds",
        5,
    )
    monkeypatch.setattr(
        "hearth.overlay_context.settings.overlay_idle_seconds",
        20,
    )
    widget = Widget(
        id="weather",
        kind="weather",
        title="Home",
        status="done",
        body="14°C · Partly cloudy",
        data={"place": "Home", "condition": "Partly cloudy", "temperature": 14},
        sticky=True,
    )
    past = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    widget.updated_at = past
    widget.ts = past
    runtime.widgets["weather"] = widget
    rel = evaluate_widget(widget, transcript=[], last_tools=[])
    assert rel.relevant is False
    assert rel.reason == "idle"


def test_topics_include_media_title():
    widget = Widget(
        id="media",
        kind="media",
        title="Dune: Part Two",
        status="done",
        body="movie · 2024",
        data={
            "item": {
                "id": "plex:1001",
                "title": "Dune: Part Two",
                "type": "movie",
                "year": 2024,
            },
            "items": [
                {
                    "id": "plex:1001",
                    "title": "Dune: Part Two",
                    "type": "movie",
                    "year": 2024,
                }
            ],
            "active_id": "plex:1001",
        },
    )
    topics = topics_for_widget(widget)
    assert "media" in topics
    assert "dune" in topics
    assert text_matches_topics("tell me more about dune", topics)


def test_publish_tool_builds_media_stack_from_results():
    runtime.widgets.clear()
    publish_tool(
        {
            "name": "plex_search",
            "ok": True,
            "data": {
                "ok": True,
                "mode": "mock",
                "results": [
                    {
                        "title": "Dune: Part Two",
                        "type": "movie",
                        "year": 2024,
                        "ratingKey": "1001",
                        "summary": "Fremen.",
                        "tmdbId": 693134,
                        "posterPath": "/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
                    },
                    {
                        "title": "The Endless",
                        "type": "movie",
                        "year": 2017,
                        "ratingKey": "2042",
                        "summary": "Cult.",
                        "tmdbId": 430231,
                        "posterPath": "/uVHPBTLb6Sj1Eso9HzyBAOMRheM.jpg",
                    },
                ],
            },
        }
    )
    media = runtime.get_widget("media")
    assert media is not None
    items = media.data["items"]
    assert len(items) == 2
    assert media.data["active_id"] == items[0]["id"]
    assert items[0]["title"] == "Dune: Part Two"


async def test_get_weather_tool_mocked():
    result = await registry.call("get_weather", {})
    assert result.ok
    assert result.data["mode"] == "mock"
    assert result.data["temperature"] == 14
    weather = runtime.get_widget("weather")
    assert weather is not None
    assert weather.kind == "weather"
