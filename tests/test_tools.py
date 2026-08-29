from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.tools import files as workspace_files


async def test_plex_now_playing_is_mocked():
    result = await registry.call("plex_now_playing", {})
    assert result.ok
    sessions = result.data["sessions"]
    assert sessions[0]["title"] == "Dune: Part Two"
    assert result.data["mode"] == "mock"


async def test_ha_list_includes_denon_and_lg():
    result = await registry.call("ha_list_entities", {"domain": "media_player"})
    ids = {row["entity_id"] for row in result.data["states"]}
    assert "media_player.denon_avr_x3700h" in ids
    assert "media_player.lg_webos_tv" in ids


async def test_destructive_ha_defaults_to_dry_run():
    result = await registry.call(
        "ha_call_service",
        {"domain": "light", "service": "turn_off", "entity_id": "light.living_room"},
    )
    assert result.needs_confirm
    assert result.dry_run
    state = await registry.call("ha_get_state", {"entity_id": "light.living_room"})
    assert state.data["state"]["state"] == "on"


async def test_destructive_ha_runs_with_confirm():
    result = await registry.call(
        "ha_call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.kitchen",
            "confirm": True,
        },
    )
    assert result.ok
    assert not result.needs_confirm
    state = await registry.call("ha_get_state", {"entity_id": "light.kitchen"})
    assert state.data["state"]["state"] == "on"


async def test_workspace_blocks_path_escape(isolated_workspace):
    try:
        workspace_files.safe_path("../etc/passwd")
        assert False, "should have rejected"
    except ValueError:
        pass
    listed = await registry.call("workspace_list", {})
    names = {row["name"] for row in listed.data["entries"]}
    assert "skills" in names


async def test_workspace_skill_echo_loaded():
    assert registry.get("vault_echo") is not None
    result = await registry.call("vault_echo", {"text": "from the hearth"})
    assert result.data["echo"] == "from the hearth"


async def test_intent_router_now_playing():
    assert route_intent("what's playing on plex")["tool"] == "plex_now_playing"
    assert route_intent("what is playing")["tool"] == "plex_now_playing"


async def test_intent_repo_work_goes_to_chief_of_staff():
    plan = route_intent("add a weather skill to the repo")
    assert plan["tool"] == "chief_of_staff"
    assert plan["args"]["said"] == "add a weather skill to the repo"
    assert plan["args"]["repo"] == "RubenVroman/Hearth"


async def test_intent_discord_and_gridways_escalate():
    assert route_intent("connect to Discord")["tool"] == "chief_of_staff"
    assert route_intent("open tasks on project Atlas")["tool"] == "chief_of_staff"
    assert route_intent("what's on my calendar tomorrow")["tool"] == "chief_of_staff"


async def test_intent_grab_movie_uses_radarr_not_plex():
    plan = route_intent("download the movie Dune")
    assert plan["tool"] == "radarr_add"
    assert "Dune" in plan["args"]["query"]


def test_intent_remember_does_not_hit_github():
    plan = route_intent("remember that I like the living room dim")
    assert plan["tool"] == "memory_remember"
    assert "dim" in plan["args"]["value"]


async def test_intent_grab_show_uses_sonarr():
    plan = route_intent("grab the show Severance")
    assert plan["tool"] == "sonarr_add"


async def test_intent_request_uses_overseerr():
    plan = route_intent("request Dune")
    assert plan["tool"] == "overseerr_request"


async def test_workspace_list_skills_does_not_hit_github():
    plan = route_intent("list skills")
    assert plan["tool"] == "workspace_list"


async def test_docker_stop_needs_confirm():
    result = await registry.call("docker_stop", {"container": "plex"})
    assert result.needs_confirm
    live = await registry.call("docker_stop", {"container": "plex", "confirm": True})
    assert live.ok
    assert not live.needs_confirm


async def test_chief_of_staff_not_configured_is_not_fake_success():
    result = await registry.call(
        "chief_of_staff",
        {"task": "add a weather skill", "said": "add a weather skill to the repo", "confirm": True},
    )
    assert not result.ok
    assert result.data.get("configured") is False
    assert "not configured" in result.data["error"].lower()


async def test_chief_of_staff_posts_when_configured(monkeypatch):
    import httpx
    from hearth.tools import cos

    captured: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            response = httpx.Response(202, text="ok")
            return response

    monkeypatch.setattr(cos.settings, "cos_webhook", "http://127.0.0.1:9/escalate")
    monkeypatch.setattr(cos.settings, "cos_webhook_key", "secret-token")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await registry.call(
        "chief_of_staff",
        {
            "task": "add a weather skill to Hearth",
            "said": "add a weather skill to the repo",
            "confirm": True,
        },
    )
    assert result.ok
    assert result.data["escalated"] is True
    assert captured["url"] == "http://127.0.0.1:9/escalate"
    assert captured["json"]["source"] == "hearth"
    assert captured["json"]["repo"] == "RubenVroman/Hearth"
    assert captured["json"]["confirm"] is True
    assert captured["json"]["said"] == "add a weather skill to the repo"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"


async def test_radarr_add_defaults_to_dry_run():
    result = await registry.call("radarr_add", {"query": "Dune"})
    assert result.needs_confirm
    assert result.dry_run


async def test_radarr_add_with_confirm_queues_mock():
    result = await registry.call("radarr_add", {"query": "Dune", "confirm": True})
    assert result.ok
    assert not result.needs_confirm
    assert result.data["added"]["title"]


async def test_house_media_inventory_speaks_tv_avr_plex():
    result = await registry.call("house_media", {})
    assert result.ok
    assert result.data["tv"]["entity_id"] == "media_player.lg_webos_tv"
    assert result.data["avr"]["entity_id"] == "media_player.denon_avr_x3700h"
    assert result.data["plex"]["sessions"][0]["title"] == "Dune: Part Two"
    speak = result.data["speak"].lower()
    assert "lg" in speak or "tv" in speak
    assert "denon" in speak or "avr" in speak
    assert "plex" in speak or "dune" in speak


async def test_ha_media_control_tv_defaults_to_dry_run():
    result = await registry.call("ha_media_control", {"device": "tv", "action": "turn_off"})
    assert result.needs_confirm
    assert result.dry_run


async def test_ha_media_control_turns_tv_on_with_confirm():
    off = await registry.call(
        "ha_media_control",
        {"device": "tv", "action": "turn_off", "confirm": True},
    )
    assert off.ok
    on = await registry.call(
        "ha_media_control",
        {"device": "tv", "action": "turn_on", "confirm": True},
    )
    assert on.ok
    assert on.data["entity_id"] == "media_player.lg_webos_tv"
    assert on.data["state"]["state"] == "on"


async def test_ha_media_control_sets_avr_volume():
    result = await registry.call(
        "ha_media_control",
        {"device": "avr", "action": "volume_set", "volume_level": 40, "confirm": True},
    )
    assert result.ok
    assert result.data["state"]["attributes"]["volume_level"] == 0.4


async def test_intent_turn_on_the_tv_uses_ha_media_control():
    plan = route_intent("turn on the TV")
    assert plan["tool"] == "ha_media_control"
    assert plan["args"]["device"] == "tv"
    assert plan["args"]["action"] == "turn_on"


async def test_intent_avr_volume_and_house_media():
    vol = route_intent("set denon volume to 25")
    assert vol["tool"] == "ha_media_control"
    assert vol["args"]["device"] == "avr"
    assert vol["args"]["action"] == "volume_set"
    assert vol["args"]["volume_level"] == 25
    assert route_intent("media status")["tool"] == "house_media"
    assert route_intent("is the TV on")["tool"] == "house_media"


async def test_intent_mute_avr():
    plan = route_intent("mute the denon")
    assert plan["tool"] == "ha_media_control"
    assert plan["args"]["action"] == "volume_mute"
    assert plan["args"]["device"] == "avr"


async def test_plex_search_returns_rating_key():
    result = await registry.call("plex_search", {"query": "Endless"})
    assert result.ok
    assert result.data["mode"] == "mock"
    hit = result.data["results"][0]
    assert hit["title"] == "The Endless"
    assert hit["ratingKey"] == "2042"
    assert hit["key"] == "/library/metadata/2042"
    assert hit.get("guid")


async def test_plex_clients_lists_apple_tv_and_lg():
    result = await registry.call("plex_clients", {})
    assert result.ok
    names = {c["name"] for c in result.data["clients"]}
    assert "Apple TV" in names
    assert "LG webOS TV" in names
    assert all(c.get("machineIdentifier") for c in result.data["clients"])


async def test_plex_play_defaults_to_dry_run():
    result = await registry.call("plex_play", {"query": "The Endless", "player": "Apple TV"})
    assert result.needs_confirm
    assert result.dry_run
    assert result.data.get("speak")
    assert "The Endless" in result.data["speak"]
    assert "Apple TV" in result.data["speak"]
    # Fixture session is playing on Apple TV — dry-run should mention switching.
    assert "Dune" in result.data["speak"]
    assert result.data.get("plan", {}).get("already_playing", {}).get("title") == "Dune: Part Two"


async def test_plex_play_starts_on_apple_tv_with_confirm():
    result = await registry.call(
        "plex_play",
        {"query": "The Endless", "player": "Apple TV", "confirm": True},
    )
    assert result.ok
    assert not result.needs_confirm
    assert result.data["played"] is True
    assert result.data["item"]["title"] == "The Endless"
    assert result.data["client"]["name"] == "Apple TV"
    assert "Playing The Endless" in result.data["speak"]


async def test_plex_play_missing_title_is_clear():
    result = await registry.call(
        "plex_play",
        {"query": "Definitely Not In Library XYZ", "player": "Apple TV", "confirm": True},
    )
    assert not result.ok
    assert result.data.get("in_library") is False
    assert "not in the Plex library" in result.data["speak"]


async def test_plex_play_ambiguous_titles_ask_which():
    result = await registry.call(
        "plex_play",
        {"query": "Heat", "player": "Apple TV", "confirm": True},
    )
    assert not result.ok
    assert result.data.get("ambiguous_titles") is True
    assert "Which title" in result.data["speak"]
    keys = {str(c.get("ratingKey")) for c in result.data.get("candidates") or []}
    assert "3001" in keys and "3002" in keys


async def test_plex_play_rating_key_disambiguates_title():
    result = await registry.call(
        "plex_play",
        {"query": "Heat", "ratingKey": "3001", "player": "Apple TV", "confirm": True},
    )
    assert result.ok
    assert result.data["item"]["ratingKey"] == "3001"
    assert result.data["item"]["year"] == 1995


async def test_plex_play_tv_prefers_active_client():
    """Vague 'tv' + an active session on Apple TV → use that client, not ask."""
    result = await registry.call(
        "plex_play",
        {"query": "The Endless", "player": "tv", "confirm": True},
    )
    assert result.ok
    assert result.data["client"]["name"] == "Apple TV"
    assert result.data.get("resolved") == "active"


async def test_plex_play_ambiguous_tv_without_sessions(monkeypatch):
    from hearth.tools.plex import plex as plex_client

    async def _no_sessions():
        return {"mode": "mock", "sessions": []}

    monkeypatch.setattr(plex_client, "now_playing", _no_sessions)
    result = await registry.call(
        "plex_play",
        {"query": "The Endless", "player": "tv", "confirm": True},
    )
    assert not result.ok
    assert result.data.get("ambiguous") is True
    assert "Which player" in result.data["speak"]


async def test_intent_play_on_apple_tv():
    plan = route_intent("play The Endless on the Apple TV")
    assert plan["tool"] == "plex_play"
    assert plan["args"]["query"] == "The Endless"
    assert "Apple" in plan["args"]["player"]


async def test_intent_play_on_the_tv():
    plan = route_intent("play The Endless on the TV")
    assert plan["tool"] == "plex_play"
    assert plan["args"]["query"] == "The Endless"
    assert plan["args"]["player"] == "tv"


async def test_intent_plex_clients():
    assert route_intent("which plex clients are available")["tool"] == "plex_clients"


async def test_plex_play_prefers_active_without_player():
    result = await registry.call(
        "plex_play",
        {"query": "The Endless", "confirm": True},
    )
    assert result.ok
    assert result.data["client"]["name"] == "Apple TV"
    assert result.data.get("resolved") == "active"


async def test_plex_play_uses_default_player(monkeypatch):
    from hearth.config import settings
    from hearth.tools.plex import plex as plex_client

    async def _no_sessions():
        return {"mode": "mock", "sessions": []}

    monkeypatch.setattr(settings, "plex_default_player", "Apple TV")
    monkeypatch.setattr(plex_client, "now_playing", _no_sessions)
    result = await registry.call(
        "plex_play",
        {"query": "The Endless", "confirm": True},
    )
    assert result.ok
    assert result.data["client"]["name"] == "Apple TV"


async def test_plex_play_dry_run_missing_title_skips_confirm():
    result = await registry.call(
        "plex_play",
        {"query": "Definitely Not In Library XYZ", "player": "Apple TV"},
    )
    assert not result.ok
    assert not result.needs_confirm
    assert result.data.get("in_library") is False


async def test_plex_play_live_proxies_play_media(monkeypatch):
    """Live path: identity + clients + playQueue + PMS-proxied playMedia."""
    import httpx
    from hearth.config import settings
    from hearth.tools.plex import plex as plex_client

    calls: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None, text: str = "OK"):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("GET", "http://plex.test"),
                    response=httpx.Response(self.status_code),
                )

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers") or {}

        async def get(self, path, params=None, headers=None):
            calls.append({"method": "GET", "path": path, "params": params, "headers": headers})
            if path == "/identity":
                return FakeResponse(
                    200,
                    {"MediaContainer": {"machineIdentifier": "server-abc", "port": 32400}},
                )
            if path == "/status/sessions":
                return FakeResponse(200, {"MediaContainer": {"Metadata": []}})
            if path == "/clients":
                return FakeResponse(
                    200,
                    {
                        "MediaContainer": {
                            "Server": [
                                {
                                    "name": "Apple TV",
                                    "host": "192.168.1.40",
                                    "machineIdentifier": "client-apple",
                                    "product": "Plex for Apple TV",
                                    "deviceClass": "stb",
                                    "protocolCapabilities": "timeline,playback,navigation,playqueues",
                                }
                            ]
                        }
                    },
                )
            if path == "/search":
                return FakeResponse(
                    200,
                    {
                        "MediaContainer": {
                            "Metadata": [
                                {
                                    "title": "The Endless",
                                    "type": "movie",
                                    "year": 2017,
                                    "ratingKey": "2042",
                                    "key": "/library/metadata/2042",
                                    "guid": "plex://movie/the-endless",
                                }
                            ]
                        }
                    },
                )
            if path == "/player/playback/playMedia":
                return FakeResponse(200, text="OK")
            return FakeResponse(404, {})

        async def post(self, path, params=None, headers=None):
            calls.append({"method": "POST", "path": path, "params": params, "headers": headers})
            if path == "/playQueues":
                return FakeResponse(200, {"MediaContainer": {"playQueueID": 77}})
            return FakeResponse(404, {})

        async def aclose(self):
            return None

    monkeypatch.setattr(settings, "plex_token", "test-token")
    monkeypatch.setattr(settings, "plex_url", "http://plex.test:32400")
    monkeypatch.setattr(settings, "mock_if_unconfigured", False)
    await plex_client.aclose()
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = await registry.call(
        "plex_play",
        {"query": "The Endless", "player": "Apple TV", "confirm": True},
    )
    assert result.ok
    assert result.data["mode"] == "live"
    assert result.data["playQueueID"] == 77
    play = next(c for c in calls if c["path"] == "/player/playback/playMedia")
    assert play["headers"]["X-Plex-Target-Client-Identifier"] == "client-apple"
    assert play["params"]["key"] == "/library/metadata/2042"
    assert play["params"]["machineIdentifier"] == "server-abc"
    assert play["params"]["containerKey"].startswith("/playQueues/77")
    await plex_client.aclose()
    monkeypatch.setattr(settings, "plex_token", "")
    monkeypatch.setattr(settings, "mock_if_unconfigured", True)


async def test_end_call_is_registered_and_not_destructive():
    result = await registry.call("end_call", {"reason": "natural_end"})
    assert result.ok
    assert not result.needs_confirm
    assert result.data["ended"] is True
    assert result.data["reason"] == "natural_end"
    public = {t["name"]: t for t in registry.list_public()}
    assert "end_call" in public
    assert public["end_call"]["destructive"] is False
