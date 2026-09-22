"""Confirm-gate races: a paid tool must run once, and only while it is fresh.

``runtime.pending`` is a single armed confirm shared by the browser, voice, and
chat paths. Two of those arriving together, or a "yes" typed long after the
preview scrolled away, are the two ways one confirm becomes two actions — or the
wrong action.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hearth.agent.loop import AgentLoop
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.fixtures import mock_thuisbezorgd
from hearth.runtime import PendingConfirm, runtime


async def _arm_food_order() -> None:
    """Put a real, confirmable food order in the pending slot."""
    await registry.call(
        "thuisbezorgd_cart",
        {"action": "add", "restaurant_id": "resto-napoli", "item_id": "napoli-margherita"},
    )
    preview = await registry.call("thuisbezorgd_order", {})
    assert preview.needs_confirm is True
    assert runtime.pending is not None


@pytest.fixture(autouse=True)
def _live_food(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    monkeypatch.setattr(settings, "hearth_delivery_street", "Testlaan 1")
    monkeypatch.setattr(settings, "hearth_delivery_postcode", "1234 AB")
    monkeypatch.setattr(settings, "hearth_delivery_city", "Ghent")
    yield


def test_claim_pending_is_atomic() -> None:
    runtime.pending = PendingConfirm(tool="docker_stop", args={"container": "plex"}, preview="x")

    first = runtime.claim_pending()
    second = runtime.claim_pending()

    assert first is not None
    assert first.tool == "docker_stop"
    assert second is None
    assert runtime.pending is None


def test_claim_pending_drops_a_stale_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confirm_ttl_seconds", 60.0)
    stale = PendingConfirm(tool="thuisbezorgd_order", args={}, preview="x")
    object.__setattr__(stale, "armed_at", stale.armed_at - 600.0)
    runtime.pending = stale

    assert runtime.claim_pending() is None
    # The slot is cleared either way, so the stale confirm cannot be replayed.
    assert runtime.pending is None


def test_claim_pending_keeps_a_fresh_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confirm_ttl_seconds", 60.0)
    runtime.pending = PendingConfirm(tool="thuisbezorgd_order", args={}, preview="x")

    assert runtime.claim_pending() is not None


def test_zero_ttl_disables_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confirm_ttl_seconds", 0.0)
    old = PendingConfirm(tool="thuisbezorgd_order", args={}, preview="x")
    object.__setattr__(old, "armed_at", old.armed_at - 100_000.0)
    runtime.pending = old

    assert runtime.claim_pending() is not None


@pytest.mark.asyncio
async def test_two_concurrent_confirms_place_one_order() -> None:
    """A double-tap must not spend the house's money twice."""
    await _arm_food_order()
    orders_before = len(mock_thuisbezorgd.orders)

    loop = AgentLoop()
    first, second = await asyncio.gather(
        loop.run("yes", confirm=True),
        loop.run("yes", confirm=True),
    )

    placed = len(mock_thuisbezorgd.orders) - orders_before
    assert placed == 1
    modes = {first["mode"], second["mode"]}
    # One turn confirmed; the other found nothing armed and was read as a message.
    assert "confirm" in modes
    assert modes != {"confirm"}


@pytest.mark.asyncio
async def test_a_stale_yes_does_not_place_the_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confirm_ttl_seconds", 60.0)
    await _arm_food_order()
    pending = runtime.pending
    assert pending is not None
    object.__setattr__(pending, "armed_at", pending.armed_at - 600.0)
    orders_before = len(mock_thuisbezorgd.orders)

    out = await AgentLoop().run("yes", confirm=True)

    assert len(mock_thuisbezorgd.orders) == orders_before
    assert out["mode"] != "confirm"


@pytest.mark.asyncio
async def test_a_fresh_yes_still_places_the_order() -> None:
    await _arm_food_order()
    orders_before = len(mock_thuisbezorgd.orders)

    out = await AgentLoop().run("yes", confirm=True)

    assert out["mode"] == "confirm"
    assert len(mock_thuisbezorgd.orders) == orders_before + 1
    assert runtime.pending is None


@pytest.mark.asyncio
async def test_a_retryable_play_rearms_its_own_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claiming must not break the Plex "Try again" loop, which re-arms itself."""
    monkeypatch.setattr(settings, "plex_token", "plex-test-token")
    monkeypatch.setattr(settings, "plex_client_wait_seconds", 0.0)

    async def no_clients(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "needs_client": True,
            "retryable": True,
            "error": "no Plex clients online",
        }

    spec = registry.get("plex_play")
    assert spec is not None
    monkeypatch.setattr(spec, "handler", no_clients)
    monkeypatch.setattr(spec, "preview_handler", None)

    first = await registry.call("plex_play", {"query": "Dune", "confirm": True})
    assert first.needs_confirm is True
    assert runtime.pending is not None
    assert runtime.pending.reason == "awaiting_client"

    # The retry claims that pending and re-arms it, so Try again keeps working.
    out = await AgentLoop().run("try again", confirm=True)
    assert out["mode"] == "confirm"
    assert runtime.pending is not None
    assert runtime.pending.tool == "plex_play"
