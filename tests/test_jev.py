"""Unit tests for the TypeSafe Jev (System One) decision gate.

All TypeSafe calls are mocked — no network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hearth.agent.loop import AgentLoop
from hearth.config import settings
from hearth.jev import evaluate_message, reset_client, set_client
from hearth.jev.client import HttpSystemOneClient
from hearth.jev.gate import suggest_action
from hearth.jev.schema import (
    ChoiceAnswer,
    JevAnswers,
    NoulAnswer,
    ScoreAnswer,
    parse_answers,
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
    domain: str = "chat",
    domain_conf: float = 0.9,
    wants_queue: float = 0.1,
    is_confirm: float = 0.05,
    is_cancel: float = 0.05,
    risk: float = 0.2,
) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "domain": {
                "type": "choice",
                "choice": domain,
                "confidence": domain_conf,
                "probabilities": {domain: domain_conf, "chat": max(0.0, 1.0 - domain_conf)},
            },
            "wants_queue": {"type": "noul", "noul": wants_queue},
            "is_confirm": {"type": "noul", "noul": is_confirm},
            "is_cancel": {"type": "noul", "noul": is_cancel},
            "risk": {
                "type": "score",
                "score": risk,
                "confidence": 0.8,
                "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"},
                "probabilities": {"0": 0.7, "1": 0.2, "2": 0.1},
            },
        },
    }


@pytest.fixture(autouse=True)
def _reset_jev():
    reset_client()
    yield
    reset_client()


@pytest.mark.asyncio
async def test_jev_disabled_skips_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    fake = FakeSystemOne(_payload())
    set_client(fake)
    verdict = await evaluate_message("turn on the kitchen lights")
    assert verdict.enabled is False
    assert verdict.action == "continue"
    assert verdict.reason == "disabled"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_jev_shadow_logs_but_does_not_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    fake = FakeSystemOne(_payload(domain="escalate_cos", domain_conf=0.95))
    set_client(fake)
    verdict = await evaluate_message("open a PR for the hearth repo")
    assert verdict.ok is True
    assert verdict.shadow is True
    assert verdict.suggested == "escalate_cos"
    assert verdict.action == "continue"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_jev_enforce_cancel_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    monkeypatch.setattr(settings, "jev_cancel_threshold", 0.78)
    fake = FakeSystemOne(_payload(is_cancel=0.91, wants_queue=0.8))
    set_client(fake)
    verdict = await evaluate_message("no, don't download that")
    assert verdict.ok is True
    assert verdict.action == "block_cancel"
    assert verdict.suggested == "block_cancel"


@pytest.mark.asyncio
async def test_jev_fail_open_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    fake = FakeSystemOne(error=RuntimeError("boom"))
    set_client(fake)
    verdict = await evaluate_message("download dune")
    assert verdict.ok is False
    assert verdict.action == "continue"
    assert verdict.reason == "api_error"


@pytest.mark.asyncio
async def test_agent_loop_enforce_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    monkeypatch.setattr(settings, "openai_api_key", "")
    fake = FakeSystemOne(_payload(is_cancel=0.95))
    set_client(fake)
    out = await AgentLoop().run("nah cancel that download")
    assert out["mode"] == "jev_cancel"
    assert out["tools"] == []
    assert "won't" in out["reply"].lower()


@pytest.mark.asyncio
async def test_agent_loop_shadow_still_runs_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key-not-real")
    monkeypatch.setattr(settings, "openai_api_key", "")
    fake = FakeSystemOne(_payload(is_cancel=0.99, domain="refuse", domain_conf=0.99))
    set_client(fake)
    out = await AgentLoop().run("what's the weather")
    assert out["mode"] == "local"
    assert out.get("jev", {}).get("suggested") == "block_cancel"
    assert out.get("jev", {}).get("action") == "continue"
    tools = out.get("tools") or []
    assert tools and tools[0]["name"] == "get_weather"


def test_suggest_action_escalate_cos() -> None:
    answers = JevAnswers(
        domain=ChoiceAnswer(choice="escalate_cos", confidence=0.9, probabilities={}),
        wants_queue=NoulAnswer(0.1),
        is_confirm=NoulAnswer(0.05),
        is_cancel=NoulAnswer(0.05),
        risk=ScoreAnswer(score=0.2, confidence=0.7),
    )
    action, reason = suggest_action(answers, domain_confidence=0.72, cancel_threshold=0.78)
    assert action == "escalate_cos"
    assert reason == "high_confidence_escalate_cos"


def test_parse_answers_from_http_shape() -> None:
    answers = parse_answers(_payload(domain="media", wants_queue=0.2))
    assert answers.domain is not None
    assert answers.domain.choice == "media"
    assert answers.wants_queue is not None
    assert answers.wants_queue.noul == pytest.approx(0.2)
    assert answers.risk is not None
    assert answers.risk.level in {"harmless", "needs_confirm", "do_not_auto_run"}


@pytest.mark.asyncio
async def test_http_client_posts_bearer_without_logging_key() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("Authorization")
        body = json.loads(request.content.decode())
        seen["body"] = body
        return httpx.Response(200, json=_payload(domain="lights"))

    client = HttpSystemOneClient(
        api_key="ts-secret-should-not-leak",
        model="jev-latest",
        transport=httpx.MockTransport(handler),
    )
    answers = await client.system_one(state={"user_message": "kitchen lights on"})
    assert seen["url"].endswith("/v1/systemone")
    assert seen["authorization"] == "Bearer ts-secret-should-not-leak"
    assert seen["body"]["model"] == "jev-latest"
    assert "domain" in seen["body"]["questions"]
    assert answers.domain is not None
    assert answers.domain.choice == "lights"
