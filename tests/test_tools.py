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
