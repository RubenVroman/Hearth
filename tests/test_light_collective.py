"""Collective light queries control every light.* entity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hearth.agent.loop import _pretty_tool, route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.house import HouseCommand, _format_failure, parse_house_command
from hearth.telegram.store import TelegramStore
from hearth.tools.ha import ha

CHAT_ID = -1004242
USER_ID = 73

TRADFRI_IDS = [
    "light.tradfri_bulb",
    "light.tradfri_bulb_2",
    "light.tradfri_bulb_3",
    "light.tradfri_bulb_4",
    "light.tradfri_bulb_5",
    "light.tradfri_bulb_6",
]


def _message(text: str, *, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "text": text,
    }


def _tradfri_house(*, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The live VAULT set: six TRADFRI bulbs, none of them named 'lights'."""
    rows: list[dict[str, Any]] = []
    for index, entity_id in enumerate(TRADFRI_IDS, start=1):
        name = "TRADFRI bulb" if index == 1 else f"TRADFRI bulb {index}"
        rows.append(
            {
                "entity_id": entity_id,
                "state": "on",
                "attributes": {
                    "friendly_name": name,
                    "brightness": 200,
                    "brightness_pct": 78,
                },
            }
        )
    rows.append(
        {
            "entity_id": "switch.tuya_desk_plug",
            "state": "on",
            "attributes": {"friendly_name": "Tuya desk plug"},
        }
    )
    if extra:
        rows.extend(extra)
    return rows


async def _state(entity_id: str) -> str:
    result = await ha.get_state(entity_id)
    return str((result.get("state") or {}).get("state") or "")


@pytest.mark.parametrize(
    "device",
    ["lights", "all lights", "every light", "the lights", "all the lights", "each light"],
)
async def test_collective_light_query_controls_every_light(device: str) -> None:
    ha.set_mock_states(_tradfri_house())
    result = await registry.call(
        "ha_device_control",
        {"device": device, "domain": "light", "action": "turn_off"},
    )
    assert result.ok
    assert result.data["collective"] is True
    assert result.data["count"] == 6
    assert result.data["entity_ids"] == TRADFRI_IDS
    assert "all 6 lights are off" in result.data["speak"].lower()
    for entity_id in TRADFRI_IDS:
        assert await _state(entity_id) == "off"
    assert await _state("switch.tuya_desk_plug") == "on"


async def test_collective_brightness_and_toggle_hit_every_light() -> None:
    ha.set_mock_states(_tradfri_house())
    dimmed = await registry.call(
        "ha_device_control",
        {"device": "all lights", "action": "brightness", "value": 40},
    )
    assert dimmed.ok
    assert "40%" in dimmed.data["speak"]
    for entity_id in TRADFRI_IDS:
        state = (await ha.get_state(entity_id))["state"]
        assert state["state"] == "on"
        assert state["attributes"]["brightness_pct"] == 40

    toggled = await registry.call(
        "ha_device_control",
        {"device": "lights", "domain": "light", "action": "toggle"},
    )
    assert toggled.ok
    assert toggled.data["speak"] == "Toggled 6 lights."
    for entity_id in TRADFRI_IDS:
        assert await _state(entity_id) == "off"
    assert await _state("switch.tuya_desk_plug") == "on"


async def test_voice_turn_off_the_lights_controls_every_light() -> None:
    ha.set_mock_states(_tradfri_house())
    plan = route_intent("turn off the lights")
    assert plan == {
        "tool": "ha_device_control",
        "args": {"device": "lights", "action": "turn_off"},
    }
    result = await registry.call(plan["tool"], plan["args"])
    assert result.ok
    assert result.data["entity_ids"] == TRADFRI_IDS
    spoken = _pretty_tool("ha_device_control", result.data)
    assert spoken is not None
    assert "all 6 lights are off" in spoken.lower()
    for entity_id in TRADFRI_IDS:
        assert await _state(entity_id) == "off"


async def test_exact_entity_and_friendly_name_stay_single_targets() -> None:
    ha.set_mock_states(
        _tradfri_house(
            extra=[
                {
                    "entity_id": "light.group_lights",
                    "state": "on",
                    "attributes": {"friendly_name": "Lights", "brightness": 180},
                }
            ]
        )
    )
    named = await registry.call(
        "ha_device_control",
        {"device": "TRADFRI bulb 3", "domain": "light", "action": "turn_off"},
    )
    assert named.ok
    assert named.data["entity_id"] == "light.tradfri_bulb_3"
    assert "collective" not in named.data
    assert await _state("light.tradfri_bulb") == "on"
    assert await _state("light.tradfri_bulb_3") == "off"

    exact = await registry.call(
        "ha_device_control",
        {"device": "light.tradfri_bulb_2", "action": "turn_off"},
    )
    assert exact.ok
    assert exact.data["entity_id"] == "light.tradfri_bulb_2"
    assert await _state("light.tradfri_bulb_4") == "on"

    # An entity whose friendly name is exactly "Lights" is that one light.
    group = await registry.call(
        "ha_device_control",
        {"device": "lights", "domain": "light", "action": "turn_off"},
    )
    assert group.ok
    assert group.data["entity_id"] == "light.group_lights"
    assert await _state("light.group_lights") == "off"
    assert await _state("light.tradfri_bulb_5") == "on"
    assert await _state("light.tradfri_bulb_6") == "on"


async def test_room_name_and_ambiguous_partial_are_unchanged() -> None:
    ha.reset_mock()
    kitchen = await registry.call(
        "ha_device_control",
        {"device": "kitchen lights", "domain": "light", "action": "turn_off"},
    )
    assert kitchen.ok
    assert kitchen.data["entity_id"] == "light.kitchen"
    assert await _state("light.office") == "on"
    assert await _state("light.living_room") == "on"

    ha.set_mock_states(_tradfri_house())
    ambiguous = await registry.call(
        "ha_device_control",
        {"device": "bulb", "domain": "light", "action": "turn_off"},
    )
    assert ambiguous.ok is False
    assert ambiguous.data["ambiguous"] is True
    assert "collective" not in ambiguous.data
    for entity_id in TRADFRI_IDS:
        assert await _state(entity_id) == "on"


async def test_collective_with_no_lights_says_so() -> None:
    ha.set_mock_states(
        [
            {
                "entity_id": "switch.tuya_desk_plug",
                "state": "on",
                "attributes": {"friendly_name": "Tuya desk plug"},
            }
        ]
    )
    result = await registry.call(
        "ha_device_control",
        {"device": "every light", "domain": "light", "action": "turn_on"},
    )
    assert result.ok is False
    assert result.data["count"] == 0
    assert "no light entities" in result.data["speak"].lower()
    assert await _state("switch.tuya_desk_plug") == "on"


async def test_cover_query_named_lights_does_not_touch_lights() -> None:
    ha.set_mock_states(_tradfri_house())
    result = await registry.call(
        "ha_device_control",
        {"device": "lights", "domain": "cover", "action": "open"},
    )
    assert result.ok is False
    assert "matches" in str(result.data.get("error") or "").lower()
    for entity_id in TRADFRI_IDS:
        assert await _state(entity_id) == "on"


async def test_telegram_collective_and_match_miss_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "123:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    ha.set_mock_states(_tradfri_house())
    parsed = parse_house_command("turn off the lights")
    assert parsed is not None
    assert parsed.args == {"device": "lights", "domain": "light", "action": "turn_off"}
    assert parse_house_command("turn off every light") is not None
    assert parse_house_command("/lights all lights off") is not None
    assert parse_house_command("/lights all lights off").args["device"] == "all lights"

    store = TelegramStore(tmp_path / "collective-lights.db")
    bot = TelegramMediaBot(store)
    try:
        reply = await bot.handle_message(_message("turn off the lights"))
        assert reply is not None
        assert "all 6 lights are off" in reply.text.lower()
        assert "isn't reachable" not in reply.text.lower()
        for entity_id in TRADFRI_IDS:
            assert await _state(entity_id) == "off"

        miss = await bot.handle_message(_message("/lights attic off", message_id=2))
        assert miss is not None
        assert "isn't reachable" not in miss.text.lower()
        assert "matches" in miss.text.lower()
        assert "/lights" in miss.text
    finally:
        store.close()


def test_live_match_miss_is_not_worded_as_unreachable() -> None:
    command = HouseCommand(
        "control_light",
        "ha_device_control",
        {"device": "attic", "domain": "light", "action": "turn_off"},
    )
    miss = _format_failure(
        command,
        {
            "ok": False,
            "mode": "live",
            "error": "No Home Assistant entity matches 'attic'",
        },
    )
    assert "isn't reachable" not in miss.lower()
    assert "responded" in miss.lower()
    assert "matches" in miss.lower()

    offline = _format_failure(
        command,
        {"ok": False, "mode": "live", "error": "ConnectError: timed out"},
    )
    assert "isn't reachable" in offline.lower()
