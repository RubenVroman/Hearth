"""Jev-routed tool calling: allow, deny, which tool, and fail-open.

Every TypeSafe call is mocked — no network. The cases mirror the four things the
gate is responsible for:

* it lets a legitimate house action through (allow),
* it blocks a state-changing tool when Jev is clearly against it (deny),
* it picks between tools when several could plausibly run (multi-tool choice),
* it gets out of the way when it cannot reach a verdict (fail open).
"""

from __future__ import annotations

from typing import Any

import pytest

from hearth.agent.loop import AgentLoop, route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.jev import reset_client, set_client
from hearth.jev.schema import (
    ChoiceAnswer,
    JevAnswers,
    NoulAnswer,
    ScoreAnswer,
    parse_answers,
    tool_gate_questions,
)
from hearth.jev.tools import (
    TOOL_LANES,
    ToolDecision,
    authorize_tool,
    is_write_tool,
    lane_for_tool,
    status_snapshot,
    suggest_tool_action,
    tool_turn,
)


class FakeSystemOne:
    def __init__(self, payload: dict[str, Any] | None = None, *, error: Exception | None = None):
        self.payload = payload or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def system_one(
        self,
        *,
        state: Any,
        questions: dict[str, Any] | None = None,
        model: str | None = None,
    ):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.error is not None:
            raise self.error
        return parse_answers(self.payload)


def _payload(
    *,
    tool_lane: str | None = None,
    lane_conf: float = 0.93,
    tool_allow: float = 0.9,
    domain: str = "chat",
    domain_conf: float = 0.9,
    is_cancel: float = 0.02,
    needs_llm: float = 0.05,
    risk: float = 0.0,
    risk_conf: float = 0.85,
) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "domain": {
            "type": "choice",
            "choice": domain,
            "confidence": domain_conf,
            "probabilities": {domain: domain_conf},
        },
        "tool_allow": {"type": "noul", "noul": tool_allow},
        "needs_llm": {"type": "noul", "noul": needs_llm},
        "is_cancel": {"type": "noul", "noul": is_cancel},
        "is_confirm": {"type": "noul", "noul": 0.05},
        "wants_queue": {"type": "noul", "noul": 0.1},
        "risk": {
            "type": "score",
            "score": risk,
            "confidence": risk_conf,
            "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"},
        },
    }
    if tool_lane is not None:
        answers["tool_lane"] = {
            "type": "choice",
            "choice": tool_lane,
            "confidence": lane_conf,
            "probabilities": {tool_lane: lane_conf},
        }
    return {"model": "jev-1.13.0", "answers": answers}


def _enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "jev_tool_gate", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")


def _shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", True)
    monkeypatch.setattr(settings, "jev_tool_gate", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")


@pytest.fixture(autouse=True)
def _reset_jev():
    reset_client()
    yield
    reset_client()


# --- lane map -----------------------------------------------------------------


def test_every_registered_tool_resolves_to_a_lane() -> None:
    """A tool nobody classified would be gated with no lane to compare against."""
    unclassified = [name for name in registry.names() if not lane_for_tool(name)]
    assert unclassified == []


def test_workspace_skills_land_in_the_files_lane() -> None:
    # vault_echo is loaded from workspace/skills/ by the test fixture.
    assert "vault_echo" in registry.names()
    assert lane_for_tool("vault_echo") == "files"
    # Sandboxed code runs, so a skill is gated like a write.
    assert is_write_tool("vault_echo") is True


def test_lane_map_has_no_duplicate_tools() -> None:
    flat = [tool for tools in TOOL_LANES.values() for tool in tools]
    assert len(flat) == len(set(flat))


def test_lane_map_has_no_stale_entries() -> None:
    classified = {tool for tools in TOOL_LANES.values() for tool in tools}
    assert classified - set(registry.names()) == set()


def test_write_classification_splits_reads_from_side_effects() -> None:
    assert is_write_tool("overseerr_request") is True
    assert is_write_tool("memory_purge") is True
    assert is_write_tool("plex_play") is True
    assert is_write_tool("plex_search") is False
    assert is_write_tool("radarr_queue") is False
    assert is_write_tool("get_weather") is False
    # An unclassified tool is gated rather than silently ungoverned.
    assert is_write_tool("some_future_tool") is True


def test_lane_for_tool() -> None:
    assert lane_for_tool("overseerr_request") == "media_queue"
    assert lane_for_tool("radarr_queue") == "media_status"
    assert lane_for_tool("chief_of_staff") == "escalate_cos"
    assert lane_for_tool("nope") == ""


# --- policy (pure, no network) -------------------------------------------------


def _answers(**kwargs: Any) -> JevAnswers:
    return parse_answers(_payload(**kwargs))


def test_policy_allows_a_clear_house_instruction() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="media_queue", tool_allow=0.94),
        tool="overseerr_request",
    )
    assert (action, reason) == ("allow", "pass")


def test_policy_denies_a_write_when_jev_is_clearly_against_it() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="media_queue", tool_allow=0.05),
        tool="overseerr_request",
    )
    assert (action, reason) == ("deny", "tool_not_allowed")


def test_policy_never_denies_a_read() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="no_tool", tool_allow=0.01, is_cancel=0.99),
        tool="plex_search",
    )
    assert (action, reason) == ("allow", "read_only")


def test_policy_denies_on_lane_mismatch_and_allows_the_matching_tool() -> None:
    answers = _answers(tool_lane="media_status", tool_allow=0.9)
    denied, reason = suggest_tool_action(answers, tool="overseerr_request")
    assert (denied, reason) == ("deny", "lane_mismatch")
    # radarr_queue is the media_status tool, and it is a read either way.
    allowed, _ = suggest_tool_action(answers, tool="radarr_queue")
    assert allowed == "allow"


def test_policy_denies_a_write_when_jev_says_no_tool() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="no_tool", tool_allow=0.9),
        tool="memory_purge",
    )
    assert (action, reason) == ("deny", "no_tool_lane")


def test_policy_denies_a_cancel_before_looking_at_the_lane() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="media_queue", tool_allow=0.9, is_cancel=0.95),
        tool="overseerr_request",
    )
    assert (action, reason) == ("deny", "cancelled")


def test_policy_hard_stops_apply_to_reads_and_to_explicit_confirms() -> None:
    refuse = _answers(domain="refuse", domain_conf=0.95)
    assert suggest_tool_action(refuse, tool="plex_search") == ("deny", "refused")
    assert suggest_tool_action(refuse, tool="plex_search", explicit_confirm=True) == (
        "deny",
        "refused",
    )
    risky = _answers(risk=2.0, risk_conf=0.9)
    assert suggest_tool_action(risky, tool="plex_search") == ("deny", "high_risk")
    assert suggest_tool_action(risky, tool="overseerr_request", explicit_confirm=True) == (
        "deny",
        "high_risk",
    )


def test_policy_low_risk_confidence_does_not_bite() -> None:
    action, _ = suggest_tool_action(
        _answers(tool_lane="media_queue", tool_allow=0.9, risk=2.0, risk_conf=0.2),
        tool="overseerr_request",
    )
    assert action == "allow"


def test_policy_escalates_a_needs_confirm_risk() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="media_queue", tool_allow=0.9, risk=1.0, risk_conf=0.9),
        tool="overseerr_request",
    )
    assert (action, reason) == ("confirm", "needs_confirm")


def test_policy_allows_an_explicit_confirm_it_would_otherwise_refuse() -> None:
    answers = _answers(tool_lane="no_tool", tool_allow=0.01, is_cancel=0.9)
    assert suggest_tool_action(answers, tool="overseerr_request") == (
        "deny",
        "cancelled",
    )
    assert suggest_tool_action(
        answers,
        tool="overseerr_request",
        explicit_confirm=True,
    ) == ("allow", "explicit_confirm")


def test_policy_without_answers_allows() -> None:
    assert suggest_tool_action(None, tool="memory_purge") == ("allow", "no_answers")


def test_policy_skips_the_lane_check_for_an_unclassified_tool() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="lights", tool_allow=0.9),
        tool="some_future_tool",
    )
    assert (action, reason) == ("allow", "pass")


def test_policy_allows_when_lane_confidence_is_too_low() -> None:
    action, reason = suggest_tool_action(
        _answers(tool_lane="media_status", lane_conf=0.4, tool_allow=0.9),
        tool="overseerr_request",
    )
    assert (action, reason) == ("allow", "pass")


def test_policy_allows_when_jev_returned_no_tool_answers() -> None:
    # A Telegram media verdict has no tool_lane; only cancel/risk can decide.
    answers = JevAnswers(
        media_ask=ChoiceAnswer(choice="exact_title", confidence=0.9),
        is_cancel=NoulAnswer(0.02),
        risk=ScoreAnswer(score=0.0, confidence=0.9),
    )
    assert suggest_tool_action(answers, tool="overseerr_request") == ("allow", "pass")


# --- authorize_tool ------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_allows_and_asks_the_tool_gate_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    fake = FakeSystemOne(_payload(tool_lane="media_queue", tool_allow=0.95))
    set_client(fake)

    decision = await authorize_tool("overseerr_request", {"query": "Dune"}, said="grab Dune")

    assert decision.allowed is True
    assert decision.reason == "pass"
    assert decision.chosen_lane == "media_queue"
    assert decision.ok is True
    questions = fake.calls[0]["questions"] or {}
    assert "tool_lane" in questions
    assert "tool_allow" in questions
    assert "needs_llm" in questions
    # Short typed state, not a transcript dump.
    assert set(fake.calls[0]["state"]) <= {"user_message", "recent_context"}


@pytest.mark.asyncio
async def test_authorize_denies_a_write_in_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    _enforce(monkeypatch)
    set_client(FakeSystemOne(_payload(tool_lane="media_queue", tool_allow=0.05)))

    decision = await authorize_tool("overseerr_request", said="hmm what about dune though")

    assert decision.denied is True
    assert decision.suggested == "deny"
    assert decision.reason == "tool_not_allowed"
    assert decision.message


@pytest.mark.asyncio
async def test_authorize_shadow_computes_the_deny_without_taking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shadow(monkeypatch)
    set_client(FakeSystemOne(_payload(tool_lane="no_tool", tool_allow=0.01)))

    decision = await authorize_tool("overseerr_request", said="wonder if dune is any good")

    assert decision.suggested == "deny"
    assert decision.action == "allow"
    assert decision.allowed is True
    assert decision.enforced is False


@pytest.mark.asyncio
async def test_authorize_fails_open_without_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    fake = FakeSystemOne(_payload(tool_lane="no_tool", tool_allow=0.0))
    set_client(fake)

    decision = await authorize_tool("overseerr_request", said="grab Dune")

    assert decision.allowed is True
    assert decision.ok is False
    assert decision.reason == "missing_api_key"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_authorize_fails_open_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _enforce(monkeypatch)
    set_client(FakeSystemOne(error=RuntimeError("system one is down")))

    decision = await authorize_tool("memory_purge", said="purge the house memory")

    assert decision.allowed is True
    assert decision.ok is False
    assert decision.reason == "api_error"


@pytest.mark.asyncio
async def test_authorize_fails_open_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "jev_timeout_seconds", 0.5)

    class Hanging:
        async def system_one(self, **_kwargs: Any):
            await asyncio.sleep(5)
            raise AssertionError("should have timed out")

    set_client(Hanging())

    decision = await authorize_tool("overseerr_request", said="grab Dune")

    assert decision.allowed is True
    assert decision.ok is False
    assert decision.reason == "timeout"


@pytest.mark.asyncio
async def test_authorize_fails_open_with_no_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _enforce(monkeypatch)
    fake = FakeSystemOne(_payload(tool_lane="no_tool", tool_allow=0.0))
    set_client(fake)

    decision = await authorize_tool("overseerr_request", said="")

    assert decision.allowed is True
    assert decision.reason == "no_state"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_authorize_is_off_when_the_gate_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "jev_tool_gate", False)
    fake = FakeSystemOne(_payload(tool_lane="no_tool", tool_allow=0.0))
    set_client(fake)

    decision = await authorize_tool("overseerr_request", said="grab Dune")

    assert decision.allowed is True
    assert decision.reason == "gate_off"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_one_system_one_call_decides_every_tool_in_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    fake = FakeSystemOne(_payload(tool_lane="media_queue", tool_allow=0.9))
    set_client(fake)

    with tool_turn("grab Dune and Arrival", channel="chat") as scope:
        first = await authorize_tool("overseerr_request", said="grab Dune and Arrival")
        second = await authorize_tool("radarr_add", said="grab Dune and Arrival")
        third = await authorize_tool("plex_search", said="grab Dune and Arrival")

    assert len(fake.calls) == 1
    assert [first.allowed, second.allowed, third.allowed] == [True, True, True]
    assert len(scope.decisions) == 3


@pytest.mark.asyncio
async def test_concurrent_tools_in_one_turn_share_a_single_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    _enforce(monkeypatch)

    class Slow(FakeSystemOne):
        async def system_one(self, **kwargs: Any):
            await asyncio.sleep(0.05)
            return await super().system_one(**kwargs)

    fake = Slow(_payload(tool_lane="media_queue", tool_allow=0.9))
    set_client(fake)

    with tool_turn("grab Dune", channel="chat"):
        decisions = await asyncio.gather(
            authorize_tool("overseerr_request", said="grab Dune"),
            authorize_tool("radarr_add", said="grab Dune"),
            authorize_tool("sonarr_add", said="grab Dune"),
        )

    assert len(fake.calls) == 1
    assert all(d.allowed for d in decisions)


# --- registry chokepoint -------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_deny_does_not_run_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "overseerr_api_key", "over-test-key")
    set_client(FakeSystemOne(_payload(tool_lane="no_tool", tool_allow=0.02)))
    called: list[dict[str, Any]] = []
    spec = registry.get("overseerr_request")
    assert spec is not None
    original = spec.handler

    async def spy(args: dict[str, Any]) -> dict[str, Any]:
        called.append(args)
        return await original(args)

    monkeypatch.setattr(spec, "handler", spy)

    with tool_turn("was dune any good", channel="chat"):
        result = await registry.call("overseerr_request", {"query": "Dune"})

    assert result.ok is False
    assert result.data["denied"] is True
    assert result.data["jev"]["reason"] == "tool_not_allowed"
    assert result.data["speak"]
    assert called == []


@pytest.mark.asyncio
async def test_registry_allows_the_tool_when_jev_agrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    set_client(FakeSystemOne(_payload(tool_lane="weather", tool_allow=0.9)))

    with tool_turn("what's the weather", channel="chat"):
        result = await registry.call("get_weather", {})

    assert result.ok is True
    assert "denied" not in result.data


@pytest.mark.asyncio
async def test_registry_needs_confirm_risk_dry_runs_an_auto_run_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    set_client(
        FakeSystemOne(
            _payload(tool_lane="memory_write", tool_allow=0.9, risk=1.0, risk_conf=0.95)
        )
    )

    with tool_turn("remember the kids go to bed at seven", channel="chat"):
        result = await registry.call(
            "memory_remember",
            {"text": "kids bedtime 19:00", "value": "19:00", "key": "kids bedtime"},
        )

    assert result.needs_confirm is True
    assert result.dry_run is True
    assert result.data["jev"]["reason"] == "needs_confirm"

    # Confirming runs it: the user has answered the question Jev raised.
    with tool_turn("yes", channel="chat"):
        confirmed = await registry.call(
            "memory_remember",
            {
                "text": "kids bedtime 19:00",
                "value": "19:00",
                "key": "kids bedtime",
                "confirm": True,
            },
            explicit_confirm=True,
        )
    assert confirmed.ok is True
    assert confirmed.needs_confirm is False


@pytest.mark.asyncio
async def test_registry_gate_false_skips_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _enforce(monkeypatch)
    fake = FakeSystemOne(_payload(tool_lane="no_tool", tool_allow=0.0))
    set_client(fake)

    with tool_turn("never mind", channel="chat"):
        result = await registry.call("get_weather", {}, gate=False)

    assert result.ok is True
    assert fake.calls == []


# --- which tool: multi-tool choice in the local router -------------------------


@pytest.mark.parametrize(
    ("lane", "text", "expected"),
    [
        ("escalate_cos", "sort out the kitchen wiring diagram", "chief_of_staff"),
        ("media_queue", "the movie Arrival", "radarr_add"),
        ("media_queue", "Severance season two", "sonarr_add"),
        ("media_queue", "Arrival", "overseerr_request"),
        ("media_status", "Arrival", "radarr_queue"),
        ("media_library", "what's on plex", "plex_search"),
        ("media_library", "which plex clients are up", "plex_clients"),
        ("media_playback", "put on Arrival", "infuse_play"),
        ("weather", "should I bring a coat", "get_weather"),
        ("web", "who won the game", "web_search"),
        ("network", "anything odd going on", "house_network"),
        ("memory_read", "what do you know about me", "memory_search"),
        ("memory_write", "remember that Parel likes penguins", "memory_remember"),
        ("food", "order a pizza", "thuisbezorgd_restaurants"),
        ("lights", "turn on the kitchen lights", "ha_device_control"),
        ("files", "inspect hearth", "docker_inspect"),
    ],
)
def test_jev_lane_picks_the_tool_in_the_local_router(
    lane: str, text: str, expected: str
) -> None:
    plan = route_intent(text, jev_lane=lane)
    assert plan is not None
    assert plan["tool"] == expected


def test_jev_lane_beats_regex_precedence() -> None:
    """Grab phrasing normally wins; Jev can read the same words as a status check."""
    text = "download the movie Arrival"
    assert route_intent(text)["tool"] == "radarr_add"
    assert route_intent(text, jev_lane="media_status")["tool"] == "radarr_queue"


def test_jev_lane_routes_a_bare_title_the_regex_router_cannot_place() -> None:
    assert route_intent("Arrival") is None
    assert route_intent("Arrival", jev_lane="media_queue")["tool"] == "overseerr_request"


def test_jev_lane_picks_between_two_plausible_tools() -> None:
    """The same words are a library read or a playback write depending on the lane."""
    assert route_intent("put on Arrival", jev_lane="media_playback")["tool"] == "infuse_play"
    assert route_intent("put on Arrival", jev_lane="media_queue")["tool"] == "overseerr_request"
    assert route_intent("what's on plex", jev_lane="media_library")["tool"] == "plex_search"


def test_media_queue_lane_refuses_to_search_prose() -> None:
    """A lane Jev picked still needs a title; prose falls through instead."""
    assert route_intent("I was wondering about all of this really", jev_lane="media_queue") is None


def test_jev_lane_falls_back_when_the_text_cannot_substantiate_it() -> None:
    """Jev says food, the text is plainly about lights — routing falls through."""
    plan = route_intent("turn on the kitchen lights", jev_lane="food")
    assert plan is not None
    assert plan["tool"] == "ha_device_control"


def test_jev_no_tool_lane_does_not_hijack_the_router() -> None:
    plan = route_intent("what's the weather", jev_lane="no_tool")
    assert plan is not None
    assert plan["tool"] == "get_weather"


def test_route_intent_is_unchanged_without_a_lane() -> None:
    """The extracted playback/device planners kept the old precedence exactly."""
    assert route_intent("turn on the kitchen lights")["tool"] == "ha_device_control"
    assert route_intent("play Arrival on the LG")["tool"] == "plex_play"
    assert route_intent("play Arrival on the Apple TV")["tool"] == "infuse_play"
    assert route_intent("put Arrival on the infuse")["tool"] == "infuse_play"
    assert route_intent("pause the apple tv")["tool"] == "infuse_transport"
    assert route_intent("watch the apple tv")["tool"] == "media_activity"
    assert route_intent("set the avr volume to 40")["tool"] == "ha_media_control"
    assert route_intent("mute the tv")["tool"] == "ha_media_control"
    assert route_intent("switch the avr to Media Player")["tool"] == "ha_media_control"
    assert route_intent("turn off the tv")["tool"] == "ha_media_control"
    assert route_intent("inspect hearth")["tool"] == "docker_inspect"
    assert route_intent("what's playing")["tool"] == "plex_now_playing"
    assert route_intent("which plex clients are up")["tool"] == "plex_clients"
    assert route_intent("house media status")["tool"] == "house_media"


# --- agent loop ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loop_routes_the_turn_to_the_jev_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "")
    fake = FakeSystemOne(_payload(tool_lane="weather", tool_allow=0.9, domain="weather"))
    set_client(fake)

    out = await AgentLoop().run("is it going to be miserable out there")

    assert out["mode"] == "local"
    assert [t["name"] for t in out["tools"]] == ["get_weather"]
    # Routing and gating shared one call.
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_agent_loop_deny_surfaces_a_house_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "radarr_api_key", "radarr-test-key")
    # Jev reads this as musing rather than an instruction to queue anything.
    set_client(
        FakeSystemOne(_payload(tool_lane="media_queue", tool_allow=0.04, domain="media"))
    )

    out = await AgentLoop().run("wonder if we should download the movie Arrival")

    assert out["mode"] == "local"
    tools = out["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "radarr_add"
    assert tools[0]["ok"] is False
    assert tools[0]["data"]["jev"]["reason"] == "tool_not_allowed"
    assert "rather answer that" in out["reply"]


@pytest.mark.asyncio
async def test_agent_loop_read_still_runs_when_a_write_would_be_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misread gate can stop Hearth acting, never stop it answering."""
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "")
    set_client(FakeSystemOne(_payload(tool_lane="no_tool", tool_allow=0.01)))

    out = await AgentLoop().run("what's playing")

    tools = out["tools"]
    assert [t["name"] for t in tools] == ["plex_now_playing"]
    assert tools[0]["ok"] is True


@pytest.mark.asyncio
async def test_agent_loop_local_routing_flag_disables_lane_steering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "jev_route_local_tools", False)
    set_client(FakeSystemOne(_payload(tool_lane="weather", tool_allow=0.9)))

    out = await AgentLoop().run("what's playing")

    assert [t["name"] for t in out["tools"]] == ["plex_now_playing"]


@pytest.mark.asyncio
async def test_agent_loop_without_jev_behaves_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", False)
    monkeypatch.setattr(settings, "openai_api_key", "")
    fake = FakeSystemOne(_payload(tool_lane="food", tool_allow=0.0))
    set_client(fake)

    out = await AgentLoop().run("what's the weather")

    assert [t["name"] for t in out["tools"]] == ["get_weather"]
    assert fake.calls == []


# --- observability -------------------------------------------------------------


def test_status_snapshot_never_leaks_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-secret-should-not-leak")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    snap = status_snapshot()

    assert snap["mode"] == "enforce"
    assert snap["key_configured"] is True
    assert "ts-secret-should-not-leak" not in repr(snap)
    assert "media_queue" in snap["lanes"]


def test_decision_log_dict_is_flat_and_safe() -> None:
    decision = ToolDecision(
        tool="overseerr_request",
        lane="media_queue",
        write=True,
        enabled=True,
        shadow=False,
        ok=True,
        suggested="deny",
        action="deny",
        reason="lane_mismatch",
        chosen_lane="media_status",
        lane_confidence=0.91,
    )
    logged = decision.as_log_dict()

    assert logged["suggested"] == "deny"
    assert logged["chosen_lane"] == "media_status"
    assert all(not isinstance(v, dict) for v in logged.values())


def test_tool_gate_questions_can_be_narrowed_to_a_few_lanes() -> None:
    questions = tool_gate_questions(("lights", "media_queue"))
    criteria = questions["tool_lane"]["criteria"]

    assert set(criteria) == {"lights", "media_queue", "no_tool"}
    assert questions["tool_lane"]["type"] == "choice"
    assert questions["tool_allow"]["type"] == "noul"
