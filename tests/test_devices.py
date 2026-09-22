"""Finding and driving the Tuya / PetZero hardware through Home Assistant.

Two HA mocks are used: the fixture house that ships with Hearth (whenever
HA_TOKEN is empty) and, for the live path, an ``httpx.MockTransport`` so the
REST calls and the read-back verification are asserted rather than assumed.

``_ruben_ha`` reproduces the real VAULT install on 2026-09-22 — tuya_local
present on disk via HACS but no device adopted, so HA has lights, media
players, sensors and switches and nothing else. Every "device missing" path is
tested against that state rather than an imagined one.
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
from hearth.telegram.house import telegram_plan
from hearth.tools.device_intent import match_device_phrase
from hearth.tools.ha import HomeAssistant, ha

# --------------------------------------------------------------------------
# Ruben's live Home Assistant, 2026-09-22
# --------------------------------------------------------------------------


def _ruben_rows() -> list[dict[str, Any]]:
    """~78 entities: lights, media players, sensors, switches. No climate/fan."""
    rows: list[dict[str, Any]] = []
    for index in range(20):
        rows.append(
            {
                "entity_id": f"light.room_{index}",
                "state": "off",
                "reachable": True,
                "attributes": {"friendly_name": f"Room {index}"},
            }
        )
    for index in range(8):
        rows.append(
            {
                "entity_id": f"media_player.player_{index}",
                "state": "idle",
                "reachable": True,
                "attributes": {"friendly_name": f"Player {index}"},
            }
        )
    for index in range(40):
        rows.append(
            {
                "entity_id": f"sensor.sensor_{index}",
                "state": "1",
                "reachable": True,
                "attributes": {"friendly_name": f"Sensor {index}"},
            }
        )
    for index in range(10):
        rows.append(
            {
                "entity_id": f"switch.switch_{index}",
                "state": "off",
                "reachable": True,
                "attributes": {"friendly_name": f"Switch {index}"},
            }
        )
    return rows


@pytest.fixture
def ruben_ha(monkeypatch: pytest.MonkeyPatch):
    """HA as it really is right now: integration on disk, nothing adopted."""
    rows = _ruben_rows()

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {"ok": True, "mode": "live", "states": rows}

    async def _integrations() -> dict[str, Any]:
        # HACS is loaded; tuya_local is not, because it has no config entry yet.
        return {
            "ok": True,
            "mode": "live",
            "known": True,
            "version": "2026.9.1",
            "components": ["hacs", "light", "media_player", "sensor", "switch"],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    monkeypatch.setattr(ha, "loaded_integrations", _integrations)
    monkeypatch.setattr(settings, "ha_climate_entity", "")
    monkeypatch.setattr(settings, "ha_feeder_entity", "")
    monkeypatch.setattr(settings, "ha_purifier_entity", "")
    return rows


# --------------------------------------------------------------------------
# Discovery and diagnosis
# --------------------------------------------------------------------------


async def test_discovery_resolves_each_role_on_the_fixture_house() -> None:
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    roles = result.data["roles"]
    assert roles["pet_feeder"]["resolved_entity_id"] == "button.pet_feeder"
    assert roles["airco"]["resolved_entity_id"] == "climate.living_room"
    assert roles["air_purifier"]["resolved_entity_id"] == "fan.air_purifier"
    assert all(info["status"] == "ready" for info in roles.values())
    # Once a role resolves, suggest the single-id pin rather than a candidate list.
    assert result.data["env_suggestions"] == {
        "HA_FEEDER_ENTITY": "button.pet_feeder",
        "HA_CLIMATE_ENTITY": "climate.living_room",
        "HA_PURIFIER_ENTITY": "fan.air_purifier",
    }


async def test_discovery_excludes_feeder_companions_from_the_feed_control() -> None:
    """A schedule switch or a portion number is not the thing that dispenses."""
    result = await registry.call("ha_discover_entities", {"kind": "feeder"})
    assert result.ok
    candidates = {row["entity_id"] for row in result.data["roles"]["pet_feeder"]["candidates"]}
    assert "button.pet_feeder" in candidates
    assert "switch.pet_feeder_schedule" not in candidates
    assert "number.pet_feeder_portion" not in candidates


async def test_discovery_counts_entities_by_domain() -> None:
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    domains = result.data["domains"]
    assert domains["climate"] >= 1
    assert domains["fan"] >= 1
    assert sum(domains.values()) == result.data["total_entities"]


async def test_discovery_filters_by_domain() -> None:
    result = await registry.call("ha_discover_entities", {"domain": "fan"})
    assert result.ok
    assert set(result.data["domains"]) == {"fan"}
    # A climate entity cannot be found when only fans were asked for.
    assert result.data["roles"]["airco"]["resolved_entity_id"] is None


async def test_discovery_filters_by_keyword_across_every_domain() -> None:
    result = await registry.call("ha_discover_entities", {"keywords": ["portion"]})
    assert result.ok
    found = {row["entity_id"] for row in result.data["keyword_matches"]}
    assert found == {"number.pet_feeder_portion"}


async def test_discovery_reports_tuya_hardware() -> None:
    result = await registry.call("ha_discover_entities", {"kind": "tuya"})
    assert result.ok
    assert "switch.tuya_desk_plug" in {row["entity_id"] for row in result.data["tuya"]}


async def test_discovery_explains_an_integration_that_adopted_nothing(ruben_ha) -> None:
    """The decisive case: files installed via HACS, but no device added."""
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    assert result.data["total_entities"] == 78
    assert result.data["integration"]["known"] is True
    assert result.data["integration"]["adopted"] is False
    assert "Add Integration" in result.data["integration"]["speak"]
    assert "HACS" in result.data["integration"]["speak"]
    # It must not read as "the integration is missing" — that is a different fix.
    assert "Not on Home Assistant yet" in result.data["speak"]


async def test_discovery_separates_no_such_domain_from_nothing_matching(ruben_ha) -> None:
    result = await registry.call("ha_discover_entities", {})
    roles = result.data["roles"]
    # Zero climate entities exist at all.
    assert roles["airco"]["status"] == "no_entities_in_domain"
    assert roles["airco"]["entities_in_domains"] == 0
    assert "no climate entity at all" in roles["airco"]["next_step"]
    # Switches exist, but none of them is a feeder.
    assert roles["pet_feeder"]["status"] == "not_paired"
    assert roles["pet_feeder"]["entities_in_domains"] == 10
    assert "HA_FEEDER_ENTITY" in roles["pet_feeder"]["next_step"]


async def test_discovery_invents_nothing_when_the_house_is_empty(ruben_ha) -> None:
    result = await registry.call("ha_discover_entities", {})
    for info in result.data["roles"].values():
        assert info["resolved_entity_id"] is None
        assert info["candidates"] == []
    assert result.data["env_suggestions"] == {}


async def test_discovery_survives_home_assistant_being_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {"ok": False, "mode": "live", "error": "Connection refused", "states": []}

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call("ha_discover_entities", {})
    assert not result.ok
    assert "unreachable" in result.data["speak"]


async def test_discovery_is_honest_when_it_cannot_read_the_integration_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _integrations() -> dict[str, Any]:
        return {"ok": False, "known": False, "components": [], "error": "HTTP 403"}

    monkeypatch.setattr(ha, "loaded_integrations", _integrations)
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    assert result.data["integration"]["known"] is False
    assert "cannot read" in result.data["integration"]["speak"]


async def test_ambiguous_match_lists_candidates_instead_of_picking_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_air_purifier_entities", "")
    monkeypatch.setattr(settings, "ha_purifier_entity", "")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
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
    result = await registry.call("ha_discover_entities", {"kind": "purifier"})
    assert result.ok
    role = result.data["roles"]["air_purifier"]
    assert role["status"] == "ambiguous"
    assert "HA_PURIFIER_ENTITY" in role["next_step"]


async def test_a_single_climate_entity_is_the_airco_whatever_it_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_airco_entities", "climate.not_paired_yet")
    monkeypatch.setattr(settings, "ha_climate_entity", "")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
            "states": [
                {
                    "entity_id": "climate.woonkamer",
                    "state": "off",
                    "attributes": {"friendly_name": "Woonkamer", "hvac_modes": ["off", "cool"]},
                }
            ],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call("ha_discover_entities", {"kind": "airco"})
    assert result.data["roles"]["airco"]["resolved_entity_id"] == "climate.woonkamer"


def test_discovery_endpoint_is_reachable(client) -> None:
    response = client.get("/api/devices/discover?kind=tuya")
    assert response.status_code == 200
    assert response.json()["ok"] is True


# --------------------------------------------------------------------------
# Tuya hardware on the LAN
# --------------------------------------------------------------------------


async def test_lan_probe_reports_nothing_configured() -> None:
    result = await registry.call("tuya_lan_probe", {})
    assert result.ok
    assert result.data["configured"] is False
    assert "TUYA_LAN_HOSTS" in result.data["speak"]


async def test_lan_probe_refuses_anything_off_the_house_network() -> None:
    """Globally routable addresses and loopback are never dialled."""
    result = await registry.call(
        "tuya_lan_probe", {"hosts": ["8.8.8.8", "1.1.1.1", "127.0.0.1"]}
    )
    assert result.ok
    assert result.data["open_count"] == 0
    for row in result.data["hosts"]:
        assert row["open"] is False
        assert "private" in row["error"], f"{row['host']} should have been refused outright"


async def test_lan_probe_reports_silence_without_claiming_a_device_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tuya_lan_hosts", "192.168.2.5,192.168.2.8")
    result = await registry.call("tuya_lan_probe", {})
    assert result.ok
    assert result.data["open_count"] == 0
    assert "powered" in result.data["speak"]
    # An address check can never name a device.
    assert result.data["identifies_devices"] is False


async def test_lan_probe_sees_a_real_open_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the socket path, not just the refusals."""
    import asyncio

    from hearth.tools import tuya_lan

    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    # Loopback is normally refused; allow it so the connect path is real.
    monkeypatch.setattr(tuya_lan, "_private", lambda _host: True)
    try:
        result = await tuya_lan.probe_tuya_lan(["127.0.0.1"], port=port, timeout=1.0)
    finally:
        server.close()
        await server.wait_closed()
    assert result["open_count"] == 1
    assert result["hosts"][0]["open"] is True
    assert "answer on port" in result["speak"]


async def test_lan_probe_caps_the_host_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long list would be a subnet scan, not a health check."""
    from hearth.tools.tuya_lan import MAX_HOSTS

    many = [f"192.168.2.{n}" for n in range(2, 2 + MAX_HOSTS + 10)]
    result = await registry.call("tuya_lan_probe", {"hosts": many})
    assert len(result.data["hosts"]) == MAX_HOSTS


async def test_discovery_says_the_hardware_is_there_but_unpaired(
    ruben_ha, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: unpaired reads differently to unplugged."""
    from hearth.tools import devices as devices_module

    async def _lan(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "port": 6668,
            "configured": True,
            "open_count": 4,
            "hosts": [{"host": h, "open": True} for h in ("192.168.2.5", "192.168.2.8")],
            "speak": "All 4 Tuya addresses answer on port 6668.",
        }

    monkeypatch.setattr(devices_module, "probe_tuya_lan", _lan)
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    assert result.data["lan"]["open_count"] == 4
    speak = result.data["speak"]
    assert "do answer on the LAN" in speak
    assert "pairing step, not a broken device" in speak


async def test_discovery_skips_the_lan_probe_once_everything_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to diagnose on a paired house, so do not touch the network."""
    from hearth.tools import devices as devices_module

    called = False

    async def _lan(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ok": True, "open_count": 0, "configured": False, "speak": "", "port": 6668}

    monkeypatch.setattr(devices_module, "probe_tuya_lan", _lan)
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    assert result.data["lan"] is None
    assert called is False


# --------------------------------------------------------------------------
# Graceful degradation on the control path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "env_var"),
    [
        ("house_climate", {"action": "set", "temperature": 21}, "HA_CLIMATE_ENTITY"),
        ("house_feeder", {"action": "feed"}, "HA_FEEDER_ENTITY"),
        ("house_purifier", {"action": "on"}, "HA_PURIFIER_ENTITY"),
    ],
)
async def test_control_says_how_to_pair_instead_of_just_refusing(
    ruben_ha, tool: str, args: dict[str, Any], env_var: str
) -> None:
    result = await registry.call(tool, args)
    assert not result.ok
    speak = result.data["speak"]
    assert "Add Integration" in speak and "Tuya Local" in speak
    assert env_var in speak
    assert result.data["discover_with"] == "ha_discover_entities"


async def test_control_never_names_an_entity_it_did_not_see(ruben_ha) -> None:
    """No invented ids: nothing that looks like an unseen entity_id is spoken."""
    real = {str(row["entity_id"]) for row in ruben_ha}
    for tool, args in (
        ("house_climate", {"action": "set", "temperature": 21}),
        ("house_feeder", {"action": "feed"}),
        ("house_purifier", {"action": "on"}),
    ):
        result = await registry.call(tool, args)
        spoken = result.data["speak"]
        for token in spoken.replace(",", " ").replace("(", " ").replace(")", " ").split():
            cleaned = token.strip(".;:'\"")
            if cleaned.count(".") == 1 and cleaned.split(".")[0] in {
                "climate",
                "fan",
                "button",
                "switch",
                "humidifier",
                "number",
            }:
                assert cleaned in real, f"{tool} invented {cleaned!r}"


# --------------------------------------------------------------------------
# Control depth carried over onto the merged house_* tools
# --------------------------------------------------------------------------


async def test_feeding_presses_the_feeder() -> None:
    before = (await ha.get_state("button.pet_feeder"))["state"]["state"]
    result = await registry.call("house_feeder", {"action": "feed"})
    assert result.ok
    assert "Fed the pets" in result.data["speak"]
    assert (await ha.get_state("button.pet_feeder"))["state"]["state"] != before


async def test_second_feed_inside_the_cooldown_is_refused_until_forced() -> None:
    assert (await registry.call("house_feeder", {"action": "feed"})).ok

    blocked = await registry.call("house_feeder", {"action": "feed"})
    assert not blocked.ok
    assert blocked.data["cooldown_active"] is True
    assert blocked.data["cooldown_remaining_s"] > 0
    assert "feed them anyway" in blocked.data["speak"]

    forced = await registry.call("house_feeder", {"action": "feed", "force": True})
    assert forced.ok
    assert forced.data["forced"] is True


async def test_cooldown_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ha_pet_feeder_cooldown_seconds", 0.0)
    assert (await registry.call("house_feeder", {"action": "feed"})).ok
    assert (await registry.call("house_feeder", {"action": "feed"})).ok


async def test_multiple_portions_use_the_number_entity_not_repeat_presses() -> None:
    result = await registry.call("house_feeder", {"action": "feed", "portions": 4})
    assert result.ok
    assert result.data["portion_entity_id"] == "number.pet_feeder_portion"
    assert result.data["presses"] == 1
    assert (await ha.get_state("number.pet_feeder_portion"))["state"]["state"] == "4"


async def test_portions_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ha_pet_feeder_max_portions", 3)
    result = await registry.call("house_feeder", {"action": "feed", "portions": 40})
    assert result.ok
    assert result.data["portions"] == 3
    assert "Capped at 3" in result.data["speak"]


async def test_feeder_schedule_reads_and_switches() -> None:
    status = await registry.call("house_feeder", {"action": "schedule_status"})
    assert status.ok
    assert status.data["enabled"] is True

    off = await registry.call("house_feeder", {"action": "schedule_off"})
    assert off.ok
    assert (await ha.get_state("switch.pet_feeder_schedule"))["state"]["state"] == "off"


async def test_feeder_without_a_schedule_entity_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_pet_feeder_schedule_entities", "switch.nope")
    result = await registry.call("house_feeder", {"action": "schedule_on"})
    assert not result.ok
    assert result.data["supported"] is False
    assert "does not expose its schedule" in result.data["speak"]


async def test_airco_on_picks_a_real_hvac_mode() -> None:
    await registry.call("house_climate", {"action": "off"})
    result = await registry.call("house_climate", {"action": "on"})
    assert result.ok
    # The fixture offers off/heat/cool/auto, so "on" must become one of them.
    assert (await ha.get_state("climate.living_room"))["state"]["state"] == "cool"


async def test_airco_refuses_a_mode_the_unit_does_not_have() -> None:
    result = await registry.call("house_climate", {"action": "dry"})
    assert not result.ok
    assert "Available: off, heat, cool, auto" in result.data["speak"]


async def test_airco_fan_speed_is_matched_against_the_units_own_list() -> None:
    ok = await registry.call("house_climate", {"action": "fan_mode", "fan_mode": "hoog"})
    assert ok.ok
    assert (await ha.get_state("climate.living_room"))["state"]["attributes"]["fan_mode"] == "high"

    bad = await registry.call("house_climate", {"action": "fan_mode", "fan_mode": "hurricane"})
    assert not bad.ok
    assert "Available: auto, low, medium, high" in bad.data["speak"]


async def test_temperature_is_clamped_to_the_units_published_range() -> None:
    result = await registry.call("house_climate", {"action": "set", "temperature": 45})
    assert result.ok
    # The fixture's own max_temp is 30.
    assert (await ha.get_state("climate.living_room"))["state"]["attributes"]["temperature"] == 30


async def test_purifier_speed_and_preset() -> None:
    speed = await registry.call("house_purifier", {"action": "set_speed", "percentage": 40})
    assert speed.ok
    assert (await ha.get_state("fan.air_purifier"))["state"]["attributes"]["percentage"] == 40

    preset = await registry.call("house_purifier", {"action": "set_mode", "preset_mode": "slaap"})
    assert preset.ok
    assert (await ha.get_state("fan.air_purifier"))["state"]["attributes"]["preset_mode"] == "sleep"


async def test_purifier_refuses_an_unknown_preset_with_the_real_list() -> None:
    result = await registry.call(
        "house_purifier", {"action": "set_mode", "preset_mode": "hyperdrive"}
    )
    assert not result.ok
    assert "Available: auto, sleep, manual, turbo" in result.data["speak"]


async def test_a_purifier_paired_as_a_switch_admits_it_has_no_speeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_purifier_entity", "switch.air_purifier")

    async def _states(_domain: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
            "states": [
                {
                    "entity_id": "switch.air_purifier",
                    "state": "off",
                    "attributes": {"friendly_name": "Air purifier"},
                }
            ],
        }

    monkeypatch.setattr(ha, "list_states", _states)
    result = await registry.call("house_purifier", {"action": "set_speed", "percentage": 50})
    assert not result.ok
    assert "only does on and off" in result.data["speak"]


# --------------------------------------------------------------------------
# Phrases — one matcher, every surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("feed the cats", "house_feeder", {"action": "feed"}),
        ("voer de katten", "house_feeder", {"action": "feed"}),
        ("geef de katten eten", "house_feeder", {"action": "feed"}),
        ("feed the cats 2 portions", "house_feeder", {"action": "feed", "portions": 2}),
        ("feed them anyway", "house_feeder", {"action": "feed", "force": True}),
        ("voer de katten toch", "house_feeder", {"action": "feed", "force": True}),
        ("airco 21", "house_climate", {"action": "set", "temperature": 21}),
        ("zet de airco op 22", "house_climate", {"action": "set", "temperature": 22}),
        ("airco on", "house_climate", {"action": "on"}),
        ("zet de airco uit", "house_climate", {"action": "off"}),
        ("airco op koelen", "house_climate", {"action": "cool"}),
        ("airco fan high", "house_climate", {"action": "fan_mode", "fan_mode": "high"}),
        ("is the airco on", "house_climate", {"action": "status"}),
        ("purifier on", "house_purifier", {"action": "on"}),
        ("luchtreiniger uit", "house_purifier", {"action": "off"}),
        ("purifier 40%", "house_purifier", {"action": "set_speed", "percentage": 40}),
        ("luchtreiniger op auto", "house_purifier", {"action": "set_mode", "preset_mode": "auto"}),
        ("voerschema uit", "house_feeder", {"action": "schedule_off"}),
        ("tuya devices", "ha_discover_entities", {}),
        ("/feed 3", "house_feeder", {"action": "feed", "portions": 3}),
        ("/airco 20", "house_climate", {"action": "set", "temperature": 20}),
        ("/purifier sleep", "house_purifier", {"action": "set_mode", "preset_mode": "sleep"}),
    ],
)
def test_device_phrases_map_to_the_house_tools(
    text: str, tool: str, args: dict[str, Any]
) -> None:
    plan = match_device_phrase(text)
    assert plan is not None, f"{text!r} should be a house-device command"
    assert plan.tool == tool
    assert plan.args == args


@pytest.mark.parametrize(
    "text",
    [
        "Cats",
        "Feed",
        "The Purifier",
        "Air",
        "airco 2024",
        "air purifier 2019",
        "grab Interstellar",
        "play Dune on the Apple TV",
        "turn on the TV",
        "order pizza",
    ],
)
def test_film_titles_are_not_device_commands(text: str) -> None:
    assert match_device_phrase(text) is None


def test_the_same_phrase_works_spoken_and_in_telegram() -> None:
    """One matcher behind both routers, so surfaces cannot drift apart."""
    for text, tool in (
        ("zet de airco op 21", "house_climate"),
        ("geef de katten eten", "house_feeder"),
        ("luchtreiniger op auto", "house_purifier"),
    ):
        assert route_intent(text)["tool"] == tool
        assert telegram_plan(text)["tool"] == tool


def test_the_exact_phrase_table_still_wins() -> None:
    """Sibling-owned phrases keep their own routing, not the wider net."""
    assert route_intent("warmer") == {"tool": "house_climate", "args": {"action": "warmer"}}
    assert telegram_plan("good night")["args"] == {"ritual": "sleep"}


# --------------------------------------------------------------------------
# Live Home Assistant (mocked HTTP)
# --------------------------------------------------------------------------


def _live_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> HomeAssistant:
    client = HomeAssistant()
    http = httpx.AsyncClient(
        base_url="http://homeassistant:8123",
        transport=httpx.MockTransport(handler),
    )

    async def _http() -> httpx.AsyncClient:
        return http

    monkeypatch.setattr(settings, "ha_token", "live-house-token")
    monkeypatch.setattr(settings, "ha_verify_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "ha_verify_poll_interval", 0.01)
    monkeypatch.setattr(client, "_http", _http)
    return client


async def test_live_integration_probe_reads_loaded_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/config"
        return httpx.Response(
            200,
            json={"version": "2026.9.1", "components": ["light", "tuya_local", "sensor"]},
        )

    client = _live_client(monkeypatch, handler)
    result = await client.loaded_integrations()
    assert result["ok"] is True
    assert "tuya_local" in result["components"]
    assert result["version"] == "2026.9.1"


async def test_live_integration_probe_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    client = _live_client(monkeypatch, handler)
    monkeypatch.setattr(settings, "ha_request_retries", 1)
    result = await client.loaded_integrations()
    assert result["ok"] is False
    assert result["known"] is False
    assert result["components"] == []


async def test_live_write_is_verified_against_real_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = {"entity_id": "climate.airco", "state": "off", "attributes": {"temperature": 22}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/services/climate/set_temperature":
            states["attributes"] = {"temperature": 21}
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=states)

    client = _live_client(monkeypatch, handler)
    result = await client.call_and_verify(
        "climate",
        "set_temperature",
        "climate.airco",
        {"temperature": 21},
        expect=lambda state: (state.get("attributes") or {}).get("temperature") == 21,
    )
    assert result["ok"] is True
    assert result["verified"] is True


async def test_live_write_accepted_but_not_observed_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/services/"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200, json={"entity_id": "fan.air_purifier", "state": "off", "attributes": {}}
        )

    client = _live_client(monkeypatch, handler)
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
    is_cancel: float = 0.05,
    risk: float = 0.2,
    risk_confidence: float = 0.9,
) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "device_ask": {"type": "choice", "choice": device_ask, "confidence": 0.95},
            "is_cancel": {"type": "noul", "noul": is_cancel},
            "risk": {"type": "score", "score": risk, "confidence": risk_confidence},
        },
    }


@pytest.fixture
def jev_enforcing(monkeypatch: pytest.MonkeyPatch):
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
    gate = await guard_tool_call("house_feeder", {"said": "feed the cats"})
    assert gate.allowed is True
    assert gate.reason == "disabled"


async def test_gate_asks_the_device_question_set(jev_enforcing) -> None:
    fake = jev_enforcing(_device_payload())
    gate = await guard_tool_call("house_feeder", {"said": "feed the cats"})
    assert gate.allowed is True
    asked = fake.calls[0]["questions"]
    assert "device_ask" in asked
    # The media router costs money and says nothing useful about a feeder.
    assert "media_ask" not in asked


async def test_gate_blocks_a_high_confidence_cancel(jev_enforcing) -> None:
    jev_enforcing(_device_payload(is_cancel=0.96))
    gate = await guard_tool_call("house_feeder", {"said": "no, don't feed them"})
    assert gate.allowed is False
    assert gate.reason == "high_confidence_cancel"


async def test_gate_blocks_a_do_not_auto_run_risk(jev_enforcing) -> None:
    jev_enforcing(_device_payload(risk=2.0, risk_confidence=0.95))
    gate = await guard_tool_call("house_climate", {"said": "airco 21"})
    assert gate.allowed is False
    assert gate.reason == "high_risk"


async def test_shadow_mode_observes_but_never_blocks(
    monkeypatch: pytest.MonkeyPatch, jev_enforcing
) -> None:
    fake = jev_enforcing(_device_payload(is_cancel=0.99))
    monkeypatch.setattr(settings, "jev_shadow", True)
    gate = await guard_tool_call("house_feeder", {"said": "no, don't feed them"})
    assert gate.allowed is True
    assert fake.calls, "shadow mode should still consult Jev"


async def test_gate_fails_open_when_system_one_errors(jev_enforcing) -> None:
    jev_enforcing(_device_payload(), error=RuntimeError("system one down"))
    gate = await guard_tool_call("house_feeder", {"said": "feed the cats"})
    assert gate.allowed is True
    assert gate.reason == "api_error"


async def test_a_blocked_tool_never_touches_the_hardware(jev_enforcing) -> None:
    jev_enforcing(_device_payload(is_cancel=0.97))
    before = (await ha.get_state("button.pet_feeder"))["state"]["state"]
    result = await registry.call("house_feeder", {"said": "nee, niet voeren"})
    assert not result.ok
    assert result.data["blocked_by"] == "jev"
    assert (await ha.get_state("button.pet_feeder"))["state"]["state"] == before


async def test_the_gate_reads_the_utterance_from_the_turn(jev_enforcing) -> None:
    """Tool args alone can be too thin to judge; the turn's sentence is not."""
    jev_enforcing(_device_payload(is_cancel=0.97))
    set_utterance("actually no, leave the cats alone")
    try:
        result = await registry.call("house_feeder", {})
        assert not result.ok
        assert result.data["blocked_by"] == "jev"
    finally:
        set_utterance("")


async def test_read_only_tools_skip_the_gate_entirely(jev_enforcing) -> None:
    fake = jev_enforcing(_device_payload(is_cancel=0.99))
    result = await registry.call("ha_discover_entities", {})
    assert result.ok
    assert fake.calls == []
