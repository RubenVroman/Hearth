"""UI activity status — shared model + tool labels for the hearth indicator."""

from __future__ import annotations

import time

from hearth.agent.registry import registry
from hearth.runtime import (
    activity_for_tool,
    brief_error_label,
    runtime,
)


def test_activity_for_tool_maps_families():
    assert activity_for_tool("web_search") == ("web_search", "Searching…")
    assert activity_for_tool("chief_of_staff") == ("cos", "Escalating to Chief of Staff…")
    assert activity_for_tool("ha_get_state")[0] == "ha"
    assert "Home Assistant" in activity_for_tool("ha_call_service")[1]
    assert activity_for_tool("plex_now_playing") == ("working", "Working…")


def test_status_includes_idle_activity(client):
    status = client.get("/api/status").json()
    assert "activity" in status
    assert status["activity"]["phase"] == "idle"
    assert status["activity"]["label"] == ""
    assert status["agent"] == "idle"


def test_set_status_thinking_exposes_working_label():
    runtime.set_status("thinking")
    snap = runtime.activity_snapshot()
    assert snap["phase"] == "thinking"
    assert snap["label"] == "Working…"
    runtime.set_status("idle")
    assert runtime.activity_snapshot()["phase"] == "idle"


def test_begin_tool_sets_ha_and_search_labels():
    runtime.begin_tool("ha_list_entities")
    assert runtime.agent_status == "tool"
    assert runtime.activity_snapshot()["phase"] == "ha"
    runtime.begin_tool("web_search")
    assert runtime.activity_snapshot()["label"] == "Searching…"
    runtime.begin_tool("chief_of_staff")
    assert "Chief of Staff" in runtime.activity_snapshot()["label"]
    runtime.set_status("idle")


def test_flash_error_holds_then_expires():
    runtime.set_status("listening")
    runtime.flash_error("connection refused", tool="ha_get_state", hold=0.05)
    assert runtime.activity_snapshot()["phase"] == "error"
    assert "Home Assistant" in runtime.activity_snapshot()["label"] or runtime.activity_snapshot()["label"]
    # Force expiry
    runtime._error_until = time.monotonic() - 1
    snap = runtime.activity_snapshot()
    assert snap["phase"] == "listening"
    assert snap["label"] == ""


def test_brief_error_label_is_short_and_safe():
    assert brief_error_label("not configured", tool="web_search") == "Not configured"
    assert "sk-" not in brief_error_label("OPENAI_API_KEY=sk-secret-value-here")
    long = "x" * 80
    assert len(brief_error_label(long)) <= 48


def test_failed_tool_flashes_error_activity(client, monkeypatch):
    async def boom(_args):
        return {"ok": False, "error": "Home Assistant unreachable"}

    spec = registry.get("ha_get_state")
    assert spec is not None
    monkeypatch.setattr(spec, "handler", boom)
    # Clear configured gate so the handler runs.
    monkeypatch.setattr(spec, "configured", None)

    response = client.post(
        "/api/invoke",
        json={"tool": "ha_get_state", "args": {"entity_id": "light.living_room"}},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    status = client.get("/api/status").json()
    assert status["activity"]["phase"] == "error"
    assert status["activity"]["label"]
    assert "token" not in status["activity"]["label"].lower()
    assert "sk-" not in status["activity"]["label"].lower()
