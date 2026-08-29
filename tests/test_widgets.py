"""Widget panel system — weather, actions, dismiss."""

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.runtime import runtime


def test_command_center_includes_widget_stack(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="widget-stack"' in page.text
    js = client.get("/static/app.js")
    assert "renderWidgets" in js.text
    assert "/api/widgets/" in js.text
    css = client.get("/static/styles.css")
    assert ".widget-stack" in css.text
    assert "widget-in" in css.text


def test_weather_ask_surfaces_weather_widget(client):
    chat = client.post("/api/chat", json={"message": "what's the weather"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "get_weather"
    assert "14" in body["reply"] or "Partly cloudy" in body["reply"]
    widgets = body["widgets"]
    kinds = {w["kind"] for w in widgets}
    assert "weather" in kinds
    weather = next(w for w in widgets if w["kind"] == "weather")
    assert weather["status"] == "done"
    assert "14" in weather["body"]
    assert "Partly cloudy" in weather["body"]

    status = client.get("/api/status")
    assert status.status_code == 200
    assert any(w["kind"] == "weather" for w in status.json()["widgets"])


def test_in_progress_action_widget_for_radarr(client):
    chat = client.post("/api/chat", json={"message": "download the movie Dune"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["needs_confirm"] is True
    action = next(w for w in body["widgets"] if w["kind"] == "action" and "Grabbing" in w["title"])
    assert action["status"] == "pending"
    assert "Waiting for confirm" in action["detail"]


def test_dismiss_widget(client):
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


def test_intent_weather_and_repo_weather_skill():
    assert route_intent("what's the weather outside")["tool"] == "get_weather"
    assert route_intent("add a weather skill to the repo")["tool"] == "chief_of_staff"


async def test_get_weather_tool_mocked():
    result = await registry.call("get_weather", {})
    assert result.ok
    assert result.data["mode"] == "mock"
    assert result.data["temperature"] == 14
    weather = runtime.get_widget("weather")
    assert weather is not None
    assert weather.kind == "weather"
