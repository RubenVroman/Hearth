"""Whole-home rituals, comfort chips, and the Telegram quick-reply lane."""

from __future__ import annotations

from pathlib import Path

import pytest

from hearth.agent.loop import AgentLoop, route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.jev import reset_client, set_client
from hearth.jev.schema import parse_answers
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.house import looks_like_house_control, telegram_plan
from hearth.telegram.store import TelegramStore
from hearth.tools.ha import ha


class _CancelJev:
    def __init__(self) -> None:
        self.calls = 0

    async def system_one(self, *, state, questions=None, model=None):
        self.calls += 1
        return parse_answers(
            {
                "model": "jev-test",
                "answers": {
                    "domain": {
                        "type": "choice",
                        "choice": "lights",
                        "confidence": 0.2,
                        "probabilities": {"lights": 0.2, "chat": 0.8},
                    },
                    "wants_queue": {"type": "noul", "noul": 0.05},
                    "is_confirm": {"type": "noul", "noul": 0.05},
                    "is_cancel": {"type": "noul", "noul": 0.95},
                    "risk": {
                        "type": "score",
                        "score": 0.1,
                        "confidence": 0.8,
                        "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"},
                        "probabilities": {"0": 0.8, "1": 0.1, "2": 0.1},
                    },
                },
            }
        )


async def _entity(entity_id: str) -> dict:
    result = await registry.call("ha_get_state", {"entity_id": entity_id})
    assert result.ok
    return result.data["state"]


@pytest.fixture(autouse=True)
def _fresh_house():
    ha.reset_mock()
    yield
    ha.reset_mock()
    reset_client()


def test_voice_routes_rituals_without_stealing_playback():
    assert route_intent("good morning")["tool"] == "house_ritual"
    assert route_intent("good morning")["args"]["ritual"] == "morning"
    assert route_intent("house sleep")["args"]["ritual"] == "sleep"
    assert route_intent("filmavond")["args"]["ritual"] == "movie"
    assert route_intent("cinema mode")["args"]["ritual"] == "movie"
    assert route_intent("movie night") == {
        "tool": "media_activity",
        "args": {"activity": "movie_night"},
    }
    assert route_intent("play Dune")["tool"] == "infuse_play"
    assert route_intent("set the heat to 21") == {
        "tool": "house_climate",
        "args": {"action": "set", "temperature": 21.0},
    }
    assert route_intent("feed the cats")["tool"] == "house_feeder"
    assert route_intent("turn the purifier on")["args"]["action"] == "on"
    assert route_intent("how's the air")["tool"] == "house_comfort"
    assert route_intent("what's the temperature")["tool"] == "get_weather"


def test_telegram_leaves_bare_movie_night_to_the_media_bot():
    assert looks_like_house_control("movie night") is False
    assert looks_like_house_control("scary movie night") is False
    assert looks_like_house_control("house of the dragon") is False
    assert telegram_plan("movie night mode")["args"]["ritual"] == "movie"
    assert telegram_plan("good morning")["args"]["ritual"] == "morning"
    assert telegram_plan("warmer")["tool"] == "house_climate"
    assert telegram_plan("Feed")["tool"] == "house_feeder"


async def test_rituals_compose_scene_lights_and_media_path():
    sleep = await registry.call("house_ritual", {"ritual": "sleep"})
    assert sleep.ok
    assert "scene" in sleep.data["speak"].lower() or "lights" in sleep.data["speak"].lower()
    assert (await _entity("light.living_room"))["state"] == "off"
    assert (await _entity("light.kitchen"))["state"] == "off"
    assert (await _entity("media_player.denon_avr_x3700h"))["state"] == "off"
    assert (await _entity("media_player.lg_webos_tv"))["state"] == "off"

    ha.reset_mock()
    await registry.call(
        "ha_call_service",
        {"domain": "media_player", "service": "turn_off", "entity_id": "media_player.lg_webos_tv"},
    )
    morning = await registry.call("house_ritual", {"ritual": "morning"})
    assert morning.ok
    assert "cinema stays dark" in morning.data["speak"].lower()
    kitchen = await _entity("light.kitchen")
    assert kitchen["state"] == "on"
    assert kitchen["attributes"]["brightness"] > 100
    assert (await _entity("media_player.lg_webos_tv"))["state"] == "off"

    ha.reset_mock()
    movie = await registry.call("house_ritual", {"ritual": "movie"})
    assert movie.ok
    assert "movie night" in movie.data["speak"].lower()
    living = await _entity("light.living_room")
    assert living["state"] == "on"
    assert living["attributes"]["brightness"] == 40
    assert (await _entity("media_player.denon_avr_x3700h"))["state"] != "off"
    assert (await _entity("media_player.lg_webos_tv"))["state"] != "off"


async def test_ritual_without_a_scene_still_sets_lights():
    states = await ha.list_states()
    ha.set_mock_states(
        [row for row in states["states"] if not str(row["entity_id"]).startswith("scene.")]
    )
    sleep = await registry.call("house_ritual", {"ritual": "sleep"})
    assert sleep.ok
    assert (await _entity("light.office"))["state"] == "off"
    assert any(step["kind"] == "light.turn_off" for step in sleep.data["steps"])


async def test_climate_feeder_and_purifier_call_ha_services():
    warmer = await registry.call("house_climate", {"action": "warmer"})
    assert warmer.ok
    climate = await _entity("climate.living_room")
    assert climate["attributes"]["temperature"] == 21.5
    assert "21.5" in warmer.data["speak"]

    fed = await registry.call("house_feeder", {"action": "feed"})
    assert fed.ok
    assert "pet feeder" in fed.data["speak"].lower()
    assert (await _entity("button.pet_feeder"))["state"] != "2026-09-22T07:00:00+00:00"

    purifier = await registry.call("house_purifier", {"action": "on"})
    assert purifier.ok
    assert (await _entity("fan.air_purifier"))["state"] == "on"

    off = await registry.call("house_purifier", {"action": "off"})
    assert off.ok
    assert (await _entity("fan.air_purifier"))["state"] == "off"


async def test_missing_purifier_is_an_honest_miss():
    states = await ha.list_states()
    ha.set_mock_states(
        [row for row in states["states"] if row["entity_id"] != "fan.air_purifier"]
    )
    result = await registry.call("house_purifier", {"action": "on"})
    assert result.ok is False
    assert "home assistant" in result.data["speak"].lower()


async def test_comfort_endpoint_exposes_climate_and_air(client):
    response = client.get("/api/comfort")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["climate"][0]["entity_id"] == "climate.living_room"
    air = {row["device_class"]: row for row in body["air"]}
    assert air["pm25"]["state"] == "8"
    assert air["pm25"]["tone"] == "good"
    assert air["carbon_dioxide"]["tone"] == "good"
    assert {chip["id"] for chip in body["rituals"]} == {"sleep", "morning", "movie"}
    assert body["purifiers"][0]["entity_id"] == "fan.air_purifier"
    assert body["feeders"][0]["entity_id"] == "button.pet_feeder"


async def test_chat_good_morning_runs_the_ritual(client):
    chat = client.post("/api/chat", json={"message": "good morning"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "house_ritual"
    assert "morning" in body["reply"].lower()


async def test_jev_cancel_blocks_the_ritual_before_tools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    fake = _CancelJev()
    set_client(fake)
    out = await AgentLoop().run("good morning")
    assert out["mode"] == "jev_cancel"
    assert out["tools"] == []
    assert fake.calls == 1
    assert (await _entity("light.kitchen"))["state"] == "off"


async def test_telegram_quick_replies_and_jev_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", "-100123")
    monkeypatch.setattr(settings, "telegram_user_ids", "42")
    store = TelegramStore(tmp_path / "house.db")
    bot = TelegramMediaBot(store)
    try:
        reply = await bot.handle_message(
            {
                "message_id": 1,
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": 42, "is_bot": False},
                "text": "good morning",
            }
        )
        assert reply is not None
        assert "morning" in reply.text.lower()
        labels = [label for row in reply.reply_markup["keyboard"] for label in row]
        assert "House sleep" in labels
        assert "Movie night mode" in labels
        assert "Warmer" in labels
        assert "Feed" in labels
        assert "Purifier on" in labels
        assert reply.reply_markup["one_time_keyboard"] is True

        vibe = await bot.handle_message(
            {
                "message_id": 2,
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": 42, "is_bot": False},
                "text": "movie night",
            }
        )
        assert vibe is not None
        assert "denon" not in vibe.text.lower()
        assert (await _entity("light.living_room"))["attributes"]["brightness"] != 40

        monkeypatch.setattr(settings, "jev_enabled", True)
        monkeypatch.setattr(settings, "jev_shadow", False)
        monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
        set_client(_CancelJev())
        kitchen_before = (await _entity("light.kitchen"))["state"]
        blocked = await bot.handle_message(
            {
                "message_id": 3,
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": 42, "is_bot": False},
                "text": "house sleep",
            }
        )
        assert blocked is not None
        # The shared tool gate owns the deny copy now; what matters is that it
        # says nothing ran and that nothing did.
        assert "won't run that" in blocked.text.lower()
        assert "house is unchanged" in blocked.text.lower()
        assert (await _entity("light.kitchen"))["state"] == kitchen_before
    finally:
        store.close()
