"""Glass info overlays — weather / media only (no flickering update guards)."""

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.runtime import runtime


def test_command_center_includes_info_overlay(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="info-overlay"' in page.text
    assert 'id="widget-stack"' not in page.text
    js = client.get("/static/app.js")
    assert "openInfoOverlay" in js.text
    assert "renderWidgets" in js.text
    assert "/api/widgets/" in js.text
    assert "upsertLocalWidget" not in js.text
    assert 'kind: "action"' not in js.text
    css = client.get("/static/styles.css")
    assert ".info-overlay" in css.text
    assert ".info-glass" in css.text
    assert ".widget-stack" not in css.text
    assert "widget-in" not in css.text
    sw = client.get("/sw.js")
    assert "hearth-shell-v8" in sw.text


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

    thumb = client.get("/api/plex/thumb/1001")
    assert thumb.status_code == 200
    assert "image/" in thumb.headers.get("content-type", "")


def test_now_playing_surfaces_media_overlay(client):
    chat = client.post("/api/chat", json={"message": "what's playing"})
    assert chat.status_code == 200
    body = chat.json()
    media = next(w for w in body["widgets"] if w["kind"] == "media")
    assert "Dune" in media["title"]


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


async def test_get_weather_tool_mocked():
    result = await registry.call("get_weather", {})
    assert result.ok
    assert result.data["mode"] == "mock"
    assert result.data["temperature"] == 14
    weather = runtime.get_widget("weather")
    assert weather is not None
    assert weather.kind == "weather"
