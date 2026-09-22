"""House devices: PetZero feeder, Tuya airco, KPT air purifier.

Home Assistant is mocked two ways: the fixture house that ships with Hearth
(``MockHouse``, used whenever HA_TOKEN is empty) and, for the live path, an
``httpx.MockTransport`` so the exact REST calls and the read-back verification
are asserted rather than assumed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.jev import guard_tool_call, reset_client, set_client, set_utterance
from hearth.jev.schema import parse_answers
from hearth.tools.device_intent import match_device_phrase
from hearth.tools.ha import HomeAssistant, ha

# --------------------------------------------------------------------------
# Phrase matching
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("feed the cats", "pet_feeder_feed", {}),
        ("feed the cat", "pet_feeder_feed", {}),
        ("feed them", "pet_feeder_feed", {}),
        ("give the cats food", "pet_feeder_feed", {}),
        ("voer de katten", "pet_feeder_feed", {}),
        ("geef de katten eten", "pet_feeder_feed", {}),
        ("katten voeren", "pet_feeder_feed", {}),
        ("feed the cats 2 portions", "pet_feeder_feed", {"portions": 2}),
        ("feed the cats twice", "pet_feeder_feed", {"portions": 2}),
        ("feed them anyway", "pet_feeder_feed", {"force": True}),
        ("voer de katten toch", "pet_feeder_feed", {"force": True}),
        ("airco 21", "airco_control", {"action": "set_temperature", "temperature": 21}),
        ("set the airco to 19", "airco_control", {"action": "set_temperature", "temperature": 19}),
        ("zet de airco op 22", "airco_control", {"action": "set_temperature", "temperature": 22}),
        ("airco on", "airco_control", {"action": "on"}),
        ("zet de airco uit", "airco_control", {"action": "off"}),
        ("turn off the air conditioning", "airco_control", {"action": "off"}),
        ("airco op koelen", "airco_control", {"action": "set_mode", "mode": "cool"}),
        ("airco fan high", "airco_control", {"action": "set_fan_mode", "fan_mode": "high"}),
        ("is the airco on", "airco_control", {"action": "status"}),
        ("purifier on", "air_purifier_control", {"action": "on"}),
        ("luchtreiniger uit", "air_purifier_control", {"action": "off"}),
        ("purifier 40%", "air_purifier_control", {"action": "set_speed", "percentage": 40}),
        ("purifier auto", "air_purifier_control", {"action": "set_mode", "preset_mode": "auto"}),
        ("is the purifier running", "air_purifier_control", {"action": "status"}),
        ("turn off automatic feeding", "pet_feeder_schedule", {"action": "off"}),
        ("voerschema uit", "pet_feeder_schedule", {"action": "off"}),
        ("tuya devices", "ha_discover_entities", {}),
        ("/feed", "pet_feeder_feed", {}),
        ("/feed 3", "pet_feeder_feed", {"portions": 3}),
        ("/airco 20", "airco_control", {"action": "set_temperature", "temperature": 20}),
        ("/airco", "airco_control", {"action": "status"}),
        ("/purifier sleep", "air_purifier_control", {"action": "set_mode", "preset_mode": "sleep"}),
        ("/devices", "house_devices", {}),
    ],
)
def test_device_phrases_map_to_tool_plans(text: str, tool: str, args: dict[str, Any]) -> None:
    plan = match_device_phrase(text)
    assert plan is not None, f"{text!r} should be a house-device command"
    assert plan.tool == tool
    assert plan.args == args


@pytest.mark.parametrize(
    "text",
    [
        # Film titles that share words with the device vocabulary.
        "Cats",
        "Feed",
        "The Purifier",
        "Air",
        "grab Interstellar",
        "play Dune on the Apple TV",
        # Other house domains must keep their existing routing.
        "turn on the TV",
        "turn off the kitchen lights",
        "order pizza",
        "what's playing",
    ],
)
def test_non_device_messages_are_left_alone(text: str) -> None:
    assert match_device_phrase(text) is None


def test_device_plan_carries_the_utterance_for_jev() -> None:
    plan = match_device_phrase("feed the cats")
    assert plan is not None
    assert plan.as_plan("feed the cats")["args"]["said"] == "feed the cats"
    # No utterance, no said key — the gate then falls back to the contextvar.
    assert "said" not in plan.as_plan("")["args"]


def test_local_router_prefers_devices_over_generic_turn_on() -> None:
    assert route_intent("turn off the airco") == {
        "tool": "airco_control",
        "args": {"action": "off", "said": "turn off the airco"},
    }
    # The media chain keeps its own routing.
    assert route_intent("turn on the TV")["tool"] == "ha_media_control"


# --------------------------------------------------------------------------
# Entity discovery
# --------------------------------------------------------------------------


async def test_discovery_resolves_each_role_and_suggests_env_lines() -> None:
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    roles = result.data["roles"]
    assert roles["pet_feeder"]["resolved_entity_id"] == "button.pet_feeder_feed"
    assert roles["airco"]["resolved_entity_id"] == "climate.airco"
    assert roles["air_purifier"]["resolved_entity_id"] == "fan.air_purifier"
    assert result.data["env_suggestions"]["HA_AIRCO_ENTITIES"] == "climate.airco"
    # The schedule switch shares the feeder's name but is not the feed control.
    feeder_ids = {row["entity_id"] for row in roles["pet_feeder"]["candidates"]}
    assert "switch.pet_feeder_schedule" not in feeder_ids


async def test_discovery_reports_tuya_hardware() -> None:
    result = await registry.call("ha_discover_entities", {"kind": "tuya"})
    assert result.ok
    found = {row["entity_id"] for row in result.data["tuya"]}
    assert "switch.tuya_desk_plug" in found
    assert "fan.air_purifier" in found  # friendly name carries the KPT marker


async def test_discovery_keyword_search_finds_companion_entities() -> None:
    """Keyword search spans every domain, including read-only sensors."""
    result = await registry.call("ha_discover_entities", {"keywords": ["portion"]})
    assert result.ok
    found = {row["entity_id"] for row in result.data["keyword_matches"]}
    assert found == {"number.pet_feeder_portion", "sensor.pet_feeder_portions_today"}


async def test_unpaired_device_returns_pairing_help_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_air_purifier_entities", "fan.nothing_here")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {"ok": True, "mode": "mock", "states": [
            {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
        ]}

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call("air_purifier_control", {"action": "on"})
    assert not result.ok
    assert "Tuya Local" in result.data["hint"]
    assert "HA_AIR_PURIFIER_ENTITIES" in result.data["error"]


async def test_ambiguous_match_lists_candidates_instead_of_picking_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_air_purifier_entities", "")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "mock",
            "states": [
                {
                    "entity_id": "fan.purifier_upstairs",
                    "state": "off",
                    "attributes": {"friendly_name": "Purifier"},
                },
                {
                    "entity_id": "fan.purifier_downstairs",
                    "state": "off",
                    "attributes": {"friendly_name": "Purifier"},
                },
            ],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call("air_purifier_control", {"action": "on"})
    assert not result.ok
    assert result.data["ambiguous"] is True
    assert {row["entity_id"] for row in result.data["matches"]} == {
        "fan.purifier_upstairs",
        "fan.purifier_downstairs",
    }


async def test_a_single_climate_entity_is_the_airco_whatever_it_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_airco_entities", "climate.not_paired_yet")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "mock",
            "states": [
                {
                    "entity_id": "climate.woonkamer",
                    "state": "off",
                    "attributes": {"friendly_name": "Woonkamer", "hvac_modes": ["off", "cool"]},
                }
            ],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call("airco_control", {"action": "status"})
    assert result.ok
    assert result.data["entity_id"] == "climate.woonkamer"


# --------------------------------------------------------------------------
# Pet feeder
# --------------------------------------------------------------------------


async def test_feeding_presses_the_feeder_and_says_so() -> None:
    before = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
    result = await registry.call("pet_feeder_feed", {})
    assert result.ok
    assert result.data["entity_id"] == "button.pet_feeder_feed"
    assert result.data["portions"] == 1
    assert "Fed the pets" in result.data["speak"]
    after = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
    assert after != before


async def test_second_feed_inside_the_cooldown_is_refused_until_forced() -> None:
    assert (await registry.call("pet_feeder_feed", {})).ok

    blocked = await registry.call("pet_feeder_feed", {})
    assert not blocked.ok
    assert blocked.data["cooldown_active"] is True
    assert blocked.data["cooldown_remaining_s"] > 0
    assert "feed them anyway" in blocked.data["speak"]

    forced = await registry.call("pet_feeder_feed", {"force": True})
    assert forced.ok
    assert forced.data["forced"] is True


async def test_cooldown_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ha_pet_feeder_cooldown_seconds", 0.0)
    assert (await registry.call("pet_feeder_feed", {})).ok
    assert (await registry.call("pet_feeder_feed", {})).ok


async def test_multiple_portions_use_the_number_entity_rather_than_repeat_presses() -> None:
    result = await registry.call("pet_feeder_feed", {"portions": 4})
    assert result.ok
    assert result.data["portion_entity_id"] == "number.pet_feeder_portion"
    assert result.data["presses"] == 1
    assert (await ha.get_state("number.pet_feeder_portion"))["state"]["state"] == "4"


async def test_portions_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ha_pet_feeder_max_portions", 3)
    result = await registry.call("pet_feeder_feed", {"portions": 40})
    assert result.ok
    assert result.data["portions"] == 3
    assert result.data["portions_capped"] is True
    assert "Capped at 3" in result.data["speak"]


async def test_an_unavailable_feeder_never_claims_it_fed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
            "states": [
                {
                    "entity_id": "button.pet_feeder_feed",
                    "state": "unavailable",
                    "attributes": {"friendly_name": "Pet feeder"},
                }
            ],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call("pet_feeder_feed", {})
    assert not result.ok
    assert "nothing was dispensed" in result.data["speak"]


async def test_feeder_schedule_reads_and_switches() -> None:
    status = await registry.call("pet_feeder_schedule", {"action": "status"})
    assert status.ok
    assert status.data["enabled"] is True

    off = await registry.call("pet_feeder_schedule", {"action": "off"})
    assert off.ok
    assert off.data["speak"] == "Scheduled feeding is off."
    assert (await ha.get_state("switch.pet_feeder_schedule"))["state"]["state"] == "off"


async def test_feeder_without_a_schedule_entity_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_pet_feeder_schedule_entities", "switch.nope")
    result = await registry.call("pet_feeder_schedule", {"action": "on"})
    assert not result.ok
    assert result.data["supported"] is False
    assert "does not expose its schedule" in result.data["speak"]


# --------------------------------------------------------------------------
# Airco
# --------------------------------------------------------------------------


async def test_setting_a_temperature_also_starts_an_airco_that_is_off() -> None:
    assert (await ha.get_state("climate.airco"))["state"]["state"] == "off"
    result = await registry.call(
        "airco_control", {"action": "set_temperature", "temperature": 21}
    )
    assert result.ok
    assert result.data["temperature"] == 21
    state = (await ha.get_state("climate.airco"))["state"]
    assert state["state"] == "cool"
    assert state["attributes"]["temperature"] == 21


async def test_airco_temperature_is_clamped_to_the_units_range() -> None:
    result = await registry.call(
        "airco_control", {"action": "set_temperature", "temperature": 45}
    )
    assert result.ok
    # HA_AIRCO_MAX_TEMPERATURE (30) is tighter than the fixture's max_temp (31).
    assert result.data["temperature"] == 30
    assert "outside its range" in result.data["speak"]


async def test_airco_off_sets_the_hvac_mode_off() -> None:
    await registry.call("airco_control", {"action": "on"})
    result = await registry.call("airco_control", {"action": "off"})
    assert result.ok
    assert result.data["speak"] == "Airco is off."
    assert (await ha.get_state("climate.airco"))["state"]["state"] == "off"


async def test_airco_mode_accepts_dutch_and_reports_the_real_list() -> None:
    dutch = await registry.call("airco_control", {"action": "set_mode", "mode": "verwarmen"})
    assert dutch.ok
    assert (await ha.get_state("climate.airco"))["state"]["state"] == "heat"

    missing = await registry.call("airco_control", {"action": "set_mode", "mode": "cryo"})
    assert not missing.ok
    assert "Available: off, cool, heat, dry, fan_only" in missing.data["speak"]


async def test_a_mode_given_alongside_a_temperature_is_not_dropped() -> None:
    """"airco 19 op verwarmen" has to change both, whether or not it was off."""
    await registry.call("airco_control", {"action": "on"})
    result = await registry.call(
        "airco_control",
        {"action": "set_temperature", "temperature": 19, "mode": "heat"},
    )
    assert result.ok
    state = (await ha.get_state("climate.airco"))["state"]
    assert state["state"] == "heat"
    assert state["attributes"]["temperature"] == 19


async def test_powering_on_with_a_mode_does_not_set_it_twice() -> None:
    result = await registry.call("airco_control", {"action": "on", "mode": "dry"})
    assert result.ok
    hvac_calls = [
        step
        for step in result.data["steps"]
        if step.get("service") == "climate.set_hvac_mode"
    ]
    assert len(hvac_calls) == 1
    assert (await ha.get_state("climate.airco"))["state"]["state"] == "dry"


async def test_airco_fan_speed_is_verified_in_state() -> None:
    result = await registry.call("airco_control", {"action": "set_fan_mode", "fan_mode": "high"})
    assert result.ok
    assert result.data["verified"] is True
    assert (await ha.get_state("climate.airco"))["state"]["attributes"]["fan_mode"] == "high"


async def test_airco_status_is_a_read_not_a_write() -> None:
    result = await registry.call("airco_control", {"action": "status"})
    assert result.ok
    assert result.data["speak"] == "Airco is off."
    assert (await ha.get_state("climate.airco"))["state"]["state"] == "off"


# --------------------------------------------------------------------------
# Air purifier
# --------------------------------------------------------------------------


async def test_purifier_power_speaks_air_quality() -> None:
    result = await registry.call("air_purifier_control", {"action": "on"})
    assert result.ok
    assert "PM2.5 12" in result.data["speak"]
    assert "filter 78%" in result.data["speak"]
    assert (await ha.get_state("fan.air_purifier"))["state"]["state"] == "on"


async def test_purifier_speed_and_preset() -> None:
    speed = await registry.call(
        "air_purifier_control", {"action": "set_speed", "percentage": 40}
    )
    assert speed.ok
    assert (await ha.get_state("fan.air_purifier"))["state"]["attributes"]["percentage"] == 40

    preset = await registry.call(
        "air_purifier_control", {"action": "set_mode", "preset_mode": "sleep"}
    )
    assert preset.ok
    assert (await ha.get_state("fan.air_purifier"))["state"]["attributes"]["preset_mode"] == "sleep"


async def test_purifier_rejects_an_unknown_preset_with_the_real_list() -> None:
    result = await registry.call(
        "air_purifier_control", {"action": "set_mode", "preset_mode": "hyperdrive"}
    )
    assert not result.ok
    assert "Available: auto, sleep, manual, turbo" in result.data["speak"]


async def test_a_purifier_paired_as_a_switch_admits_it_has_no_speeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_air_purifier_entities", "switch.air_purifier")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "mock",
            "states": [
                {
                    "entity_id": "switch.air_purifier",
                    "state": "off",
                    "attributes": {"friendly_name": "Air purifier"},
                }
            ],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call(
        "air_purifier_control", {"action": "set_speed", "percentage": 50}
    )
    assert not result.ok
    assert "only does on and off" in result.data["speak"]


async def test_house_devices_snapshot_covers_all_three() -> None:
    result = await registry.call("house_devices", {})
    assert result.ok
    devices = result.data["devices"]
    assert devices["pet_feeder"]["entity_id"] == "button.pet_feeder_feed"
    assert devices["airco"]["entity_id"] == "climate.airco"
    assert devices["air_purifier"]["entity_id"] == "fan.air_purifier"
    assert "Airco is off." in result.data["speak"]


# --------------------------------------------------------------------------
# Live Home Assistant (mocked HTTP)
# --------------------------------------------------------------------------


def _live_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> tuple[HomeAssistant, list[httpx.Request]]:
    """A HomeAssistant bound to a mock transport, as if HA_TOKEN were set."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = HomeAssistant()
    http = httpx.AsyncClient(
        base_url="http://homeassistant:8123",
        transport=httpx.MockTransport(_record),
    )

    async def _http() -> httpx.AsyncClient:
        return http

    monkeypatch.setattr(settings, "ha_token", "live-house-token")
    monkeypatch.setattr(settings, "ha_verify_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "ha_verify_poll_interval", 0.01)
    monkeypatch.setattr(client, "_http", _http)
    return client, seen


async def test_live_write_is_verified_against_real_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = {"entity_id": "climate.airco", "state": "off", "attributes": {"temperature": 22}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/services/climate/set_temperature":
            states["attributes"] = {"temperature": 21}
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=states)

    client, seen = _live_client(monkeypatch, handler)
    result = await client.call_and_verify(
        "climate",
        "set_temperature",
        "climate.airco",
        {"temperature": 21},
        expect=lambda state: (state.get("attributes") or {}).get("temperature") == 21,
    )
    assert result["ok"] is True
    assert result["verified"] is True
    assert seen[0].url.path == "/api/services/climate/set_temperature"


async def test_live_write_accepted_but_not_observed_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/services/"):
            return httpx.Response(200, json=[])
        # The device never actually turns on.
        return httpx.Response(
            200, json={"entity_id": "fan.air_purifier", "state": "off", "attributes": {}}
        )

    client, _ = _live_client(monkeypatch, handler)
    result = await client.call_and_verify(
        "fan",
        "turn_on",
        "fan.air_purifier",
        expect=lambda state: state.get("state") == "on",
    )
    assert result["accepted"] is True
    assert result["verified"] is False
    assert result["ok"] is False
    assert "not observed" in result["error"]


async def test_live_service_failure_is_reported_not_mocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    client, _ = _live_client(monkeypatch, handler)
    monkeypatch.setattr(settings, "ha_request_retries", 1)
    result = await client.call_and_verify("button", "press", "button.pet_feeder_feed")
    assert result["ok"] is False
    assert result["accepted"] is False
    assert "401" in result["error"]


# --------------------------------------------------------------------------
# Jev gating
# --------------------------------------------------------------------------


class FakeSystemOne:
    """Stand-in for TypeSafe System One — no network."""

    def __init__(self, payload: dict[str, Any], *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def system_one(
        self,
        *,
        state: Any,
        questions: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Any:
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.error is not None:
            raise self.error
        return parse_answers(self.payload)


def _device_payload(
    *,
    device_ask: str = "feed_pets",
    confidence: float = 0.95,
    is_cancel: float = 0.05,
    risk: float = 0.2,
    risk_confidence: float = 0.9,
) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "device_ask": {
                "type": "choice",
                "choice": device_ask,
                "confidence": confidence,
                "probabilities": {device_ask: confidence},
            },
            "wants_queue": {"type": "noul", "noul": 0.02},
            "is_confirm": {"type": "noul", "noul": 0.05},
            "is_cancel": {"type": "noul", "noul": is_cancel},
            "risk": {"type": "score", "score": risk, "confidence": risk_confidence},
        },
    }


@pytest.fixture
def jev_enforcing(monkeypatch: pytest.MonkeyPatch):
    """Jev on and enforcing, with an injected System One client."""

    def _install(payload: dict[str, Any], *, error: Exception | None = None) -> FakeSystemOne:
        monkeypatch.setattr(settings, "jev_enabled", True)
        monkeypatch.setattr(settings, "jev_shadow", False)
        monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
        fake = FakeSystemOne(payload, error=error)
        set_client(fake)
        return fake

    yield _install
    reset_client()


async def test_gate_is_a_no_op_while_jev_is_disabled() -> None:
    gate = await guard_tool_call("pet_feeder_feed", {"said": "feed the cats"})
    assert gate.allowed is True
    assert gate.reason == "disabled"


async def test_gate_asks_the_device_question_set(jev_enforcing) -> None:
    fake = jev_enforcing(_device_payload())
    gate = await guard_tool_call("pet_feeder_feed", {"said": "feed the cats"})
    assert gate.allowed is True
    asked = fake.calls[0]["questions"]
    assert "device_ask" in asked
    # The media router costs money and says nothing useful about a feeder.
    assert "media_ask" not in asked
    assert fake.calls[0]["state"]["user_message"] == "feed the cats"


async def test_gate_blocks_a_high_confidence_cancel(jev_enforcing) -> None:
    jev_enforcing(_device_payload(device_ask="feed_pets", is_cancel=0.96))
    gate = await guard_tool_call("pet_feeder_feed", {"said": "no, don't feed them"})
    assert gate.allowed is False
    assert gate.reason == "high_confidence_cancel"


async def test_gate_blocks_a_do_not_auto_run_risk(jev_enforcing) -> None:
    jev_enforcing(_device_payload(risk=2.0, risk_confidence=0.95))
    gate = await guard_tool_call("airco_control", {"said": "airco 21"})
    assert gate.allowed is False
    assert gate.reason == "high_risk"


async def test_shadow_mode_observes_but_never_blocks(
    monkeypatch: pytest.MonkeyPatch, jev_enforcing
) -> None:
    fake = jev_enforcing(_device_payload(is_cancel=0.99))
    monkeypatch.setattr(settings, "jev_shadow", True)
    gate = await guard_tool_call("pet_feeder_feed", {"said": "no, don't feed them"})
    assert gate.allowed is True
    assert fake.calls, "shadow mode should still consult Jev"


async def test_gate_fails_open_when_system_one_errors(jev_enforcing) -> None:
    jev_enforcing(_device_payload(), error=RuntimeError("system one down"))
    gate = await guard_tool_call("pet_feeder_feed", {"said": "feed the cats"})
    assert gate.allowed is True
    assert gate.reason == "api_error"


async def test_a_blocked_tool_never_touches_the_hardware(jev_enforcing) -> None:
    jev_enforcing(_device_payload(is_cancel=0.97))
    before = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
    result = await registry.call("pet_feeder_feed", {"said": "nee, niet voeren"})
    assert not result.ok
    assert result.data["blocked_by"] == "jev"
    after = (await ha.get_state("button.pet_feeder_feed"))["state"]["state"]
    assert after == before


async def test_the_gate_reads_the_utterance_from_the_turn(jev_enforcing) -> None:
    """Tool args alone can be too thin to judge; the turn's sentence is not."""
    jev_enforcing(_device_payload(is_cancel=0.97))
    set_utterance("actually no, leave the cats alone")
    try:
        result = await registry.call("pet_feeder_feed", {})
        assert not result.ok
        assert result.data["blocked_by"] == "jev"
    finally:
        set_utterance("")


async def test_ungated_tools_skip_the_gate_entirely(jev_enforcing) -> None:
    fake = jev_enforcing(_device_payload(is_cancel=0.99))
    result = await registry.call("house_devices", {})
    assert result.ok
    assert fake.calls == []


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_devices_endpoint_reports_the_resolved_entities(client) -> None:
    response = client.get("/api/devices")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["devices"]["airco"]["entity_id"] == "climate.airco"
    assert body["devices"]["air_purifier"]["entity_id"] == "fan.air_purifier"


def test_devices_endpoint_needs_a_session(client) -> None:
    response = client.get("/api/devices", headers={"X-Auth-Token": ""})
    assert response.status_code in {401, 403}


async def test_invoke_endpoint_runs_a_device_tool_through_the_registry(client) -> None:
    response = client.post(
        "/api/invoke",
        json={"tool": "airco_control", "args": {"action": "set_temperature", "temperature": 19}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["temperature"] == 19
