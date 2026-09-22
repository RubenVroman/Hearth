from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.jev import set_client
from hearth.jev.schema import JevAnswers, NoulAnswer
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.house import parse_house_command
from hearth.telegram.store import TelegramStore
from hearth.tools.ha import HomeAssistant

CHAT_ID = -1004242
USER_ID = 73


def _message(text: str, *, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "text": text,
    }


@pytest.mark.asyncio
async def test_house_status_uses_one_snapshot_and_only_reports_existing_feeder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HomeAssistant()
    calls = 0
    states = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "reachable": True,
            "attributes": {"friendly_name": "Kitchen", "brightness": 128},
        },
        {
            "entity_id": "light.office",
            "state": "off",
            "reachable": True,
            "attributes": {"friendly_name": "Office", "brightness": 0},
        },
        {
            "entity_id": "climate.downstairs",
            "state": "heat",
            "reachable": True,
            "attributes": {
                "friendly_name": "Downstairs",
                "current_temperature": 20.5,
                "temperature": 21,
                "temperature_unit": "°C",
                "hvac_action": "heating",
            },
        },
        {
            "entity_id": "cover.patio_blind",
            "state": "open",
            "reachable": True,
            "attributes": {"friendly_name": "Patio blind", "current_position": 60},
        },
        {
            "entity_id": "sensor.pet_feeder_last_feeding",
            "state": "2026-09-22T07:42:00+00:00",
            "reachable": True,
            "last_changed": "2026-09-22T07:42:01+00:00",
            "attributes": {"friendly_name": "Cat feeder last feeding"},
        },
        {
            "entity_id": "media_player.denon",
            "state": "playing",
            "reachable": True,
            "attributes": {"friendly_name": "Denon"},
        },
        {
            "entity_id": "switch.unreachable",
            "state": "unavailable",
            "reachable": False,
            "attributes": {"friendly_name": "Old switch"},
        },
    ]

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True, "mode": "live", "states": states}

    monkeypatch.setattr(client, "list_states", _states)
    result = await client.house_status()

    assert calls == 1
    assert result["ok"] is True
    assert result["health"] == "degraded"
    assert result["summary"]["lights_on"] == 1
    assert result["what_is_on"]["lights"][0]["name"] == "Kitchen"
    assert result["what_is_on"]["media_players"][0]["name"] == "Denon"
    assert result["climate"][0]["current_temperature"] == 20.5
    assert result["covers"][0]["position"] == 60
    assert result["feeder"]["last_fed"] == "2026-09-22T07:42:00+00:00"
    assert "last fed" in result["speak"].lower()


@pytest.mark.asyncio
async def test_house_status_has_clear_offline_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HomeAssistant()

    async def _offline(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "mode": "live",
            "error": "receiver network unavailable",
            "states": [],
        }

    monkeypatch.setattr(client, "list_states", _offline)
    result = await client.house_status()

    assert result["ok"] is False
    assert result["health"] == "offline"
    assert result["feeder"] is None
    assert "try again" in result["speak"].lower()


def test_house_status_endpoint_and_rooms_share_coherent_shapes(client) -> None:
    status = client.get("/api/house/status")
    assert status.status_code == 200
    body = status.json()
    assert body["ok"] is True
    assert body["summary"]["lights_total"] >= 3
    assert body["climate"][0]["name"] == "Living room climate"
    assert body["covers"][0]["name"] == "Living room blind"
    assert body["feeder"] is None

    rooms = client.get("/api/rooms")
    assert rooms.status_code == 200
    assert rooms.json()["ok"] is True
    assert rooms.json()["covers"][0]["entity_id"] == "cover.living_room_blind"
    assert rooms.json()["climate"][0]["entity_id"] == "climate.living_room"


def test_rooms_endpoint_reads_home_assistant_once(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.app import ha

    calls = 0

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "ok": True,
            "mode": "live",
            "states": [
                {
                    "entity_id": "light.one",
                    "state": "on",
                    "attributes": {"friendly_name": "One"},
                },
                {
                    "entity_id": "scene.night",
                    "state": "off",
                    "attributes": {"friendly_name": "Night"},
                },
            ],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    response = client.get("/api/rooms")
    assert response.status_code == 200
    assert calls == 1
    assert response.json()["lights"][0]["entity_id"] == "light.one"
    assert response.json()["scenes"][0]["entity_id"] == "scene.night"


@pytest.mark.asyncio
async def test_cover_position_and_light_brightness_use_safe_device_tool() -> None:
    cover = await registry.call(
        "ha_device_control",
        {
            "device": "Living room blind",
            "domain": "cover",
            "action": "set_position",
            "value": 35,
        },
    )
    assert cover.ok
    assert cover.data["service"] == "cover.set_cover_position"
    assert cover.data["state"]["attributes"]["current_position"] == 35

    light = await registry.call(
        "ha_device_control",
        {
            "device": "Kitchen",
            "domain": "light",
            "action": "brightness",
            "value": 42,
        },
    )
    assert light.ok
    assert light.data["state"]["attributes"]["brightness_pct"] == 42


def test_local_router_handles_house_status_and_covers() -> None:
    assert route_intent("house status") == {"tool": "house_status", "args": {}}
    assert route_intent("what is on in the house") == {"tool": "house_status", "args": {}}
    assert route_intent("open the living room blind") == {
        "tool": "ha_device_control",
        "args": {
            "device": "living room blind",
            "domain": "cover",
            "action": "open",
        },
    }
    assert route_intent("set the living room blind to 30%") == {
        "tool": "ha_device_control",
        "args": {
            "device": "living room blind",
            "domain": "cover",
            "action": "set_position",
            "value": 30,
        },
    }
    assert route_intent("turn off the living room blind") == {
        "tool": "ha_device_control",
        "args": {
            "device": "living room blind",
            "domain": "cover",
            "action": "close",
        },
    }


@pytest.mark.parametrize(
    ("text", "kind", "tool", "args"),
    [
        ("/house", "house_status", "house_status", {}),
        (
            "/lights kitchen on",
            "control_light",
            "ha_device_control",
            {"device": "kitchen", "domain": "light", "action": "turn_on"},
        ),
        (
            "/lights kitchen 40%",
            "control_light",
            "ha_device_control",
            {"device": "kitchen", "domain": "light", "action": "brightness", "value": 40},
        ),
        (
            "/scene movie night",
            "activate_scene",
            "ha_device_control",
            {"device": "movie night", "domain": "scene", "action": "activate"},
        ),
        (
            "/cover close living room blind",
            "control_cover",
            "ha_device_control",
            {"device": "living room blind", "domain": "cover", "action": "close"},
        ),
        (
            "/cover living room blind 55",
            "control_cover",
            "ha_device_control",
            {
                "device": "living room blind",
                "domain": "cover",
                "action": "set_position",
                "value": 55,
            },
        ),
    ],
)
def test_house_command_parser(
    text: str,
    kind: str,
    tool: str,
    args: dict[str, Any],
) -> None:
    parsed = parse_house_command(text)
    assert parsed is not None
    assert parsed.kind == kind
    assert parsed.tool == tool
    assert parsed.args == args
    assert not parsed.error


@pytest.mark.asyncio
async def test_telegram_house_commands_control_and_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "123:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    store = TelegramStore(tmp_path / "house-commands.db")
    bot = TelegramMediaBot(store)
    try:
        lights = await bot.handle_message(_message("/lights"))
        assert lights is not None
        assert "Living room" in lights.text
        assert "/lights <name>" in lights.text

        dimmed = await bot.handle_message(_message("/lights kitchen 35", message_id=2))
        assert dimmed is not None
        assert "35%" in dimmed.text

        scene = await bot.handle_message(_message("/scene movie night", message_id=3))
        assert scene is not None
        assert "activated" in scene.text

        cover = await bot.handle_message(
            _message("/cover living room blind close", message_id=4)
        )
        assert cover is not None
        assert "closed" in cover.text

        house = await bot.handle_message(_message("/house", message_id=5))
        assert house is not None
        assert "House status" in house.text
        assert "Climate:" in house.text

        bad = await bot.handle_message(_message("/cover attic", message_id=6))
        assert bad is not None
        assert "Use /covers" in bad.text
    finally:
        store.close()


class _CancelJev:
    async def system_one(self, **_: Any) -> JevAnswers:
        return JevAnswers(is_cancel=NoulAnswer(0.99), model="jev-test")


@pytest.mark.asyncio
async def test_telegram_house_write_is_jev_gated_in_enforce_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "123:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    set_client(_CancelJev())

    await registry.call(
        "ha_device_control",
        {"device": "Kitchen", "domain": "light", "action": "turn_on"},
    )
    store = TelegramStore(tmp_path / "jev-house-command.db")
    bot = TelegramMediaBot(store)
    try:
        reply = await bot.handle_message(_message("/lights kitchen off"))
        assert reply is not None
        assert "didn't change" in reply.text
        state = await registry.call("ha_get_state", {"entity_id": "light.kitchen"})
        assert state.data["state"]["state"] == "on"
    finally:
        store.close()
