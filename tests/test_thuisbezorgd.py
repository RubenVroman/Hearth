"""Thuisbezorgd browse / cart / confirm / dry-run — mock backend only (never hits the real site)."""

from __future__ import annotations

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.fixtures import mock_thuisbezorgd
from hearth.tools.thuisbezorgd import thuisbezorgd


def _set_delivery(monkeypatch) -> None:
    monkeypatch.setattr(settings, "hearth_delivery_street", "Voorbeeldstraat 1")
    monkeypatch.setattr(settings, "hearth_delivery_postcode", "1234AB")
    monkeypatch.setattr(settings, "hearth_delivery_city", "Amsterdam")
    monkeypatch.setattr(settings, "hearth_delivery_country", "NL")


def _reset_kitchen() -> None:
    mock_thuisbezorgd.clear_cart()
    mock_thuisbezorgd.orders.clear()
    mock_thuisbezorgd._order_seq = 0


async def test_restaurants_require_delivery_address(monkeypatch):
    _reset_kitchen()
    monkeypatch.setattr(settings, "hearth_delivery_street", "")
    monkeypatch.setattr(settings, "hearth_delivery_postcode", "")
    monkeypatch.setattr(settings, "hearth_delivery_city", "")
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    result = await registry.call("thuisbezorgd_restaurants", {})
    assert result.data.get("ok") is False
    assert "HEARTH_DELIVERY" in (result.data.get("error") or "")
    assert result.data.get("restaurants") == []


async def test_browse_restaurants_and_menu_mock(monkeypatch):
    _reset_kitchen()
    _set_delivery(monkeypatch)
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    listed = await registry.call("thuisbezorgd_restaurants", {"cuisine": "pizza"})
    assert listed.ok
    assert listed.data["mode"] == "mock"
    assert listed.data["widget"] == "restaurant_list"
    names = {r["name"] for r in listed.data["restaurants"]}
    assert "Pizzeria Napoli" in names

    menu = await registry.call("thuisbezorgd_menu", {"restaurant_id": "resto-napoli"})
    assert menu.ok
    assert menu.data["widget"] == "restaurant_menu"
    item_ids = {i["id"] for i in menu.data["items"]}
    assert "napoli-margherita" in item_ids


async def test_cart_add_view_clear(monkeypatch):
    _reset_kitchen()
    _set_delivery(monkeypatch)
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    added = await registry.call(
        "thuisbezorgd_cart",
        {
            "action": "add",
            "restaurant_id": "resto-napoli",
            "item_id": "napoli-margherita",
            "quantity": 2,
        },
    )
    assert added.ok
    assert added.data["ok"] is True
    cart = added.data["cart"]
    assert cart["items"][0]["quantity"] == 2
    assert cart["total_cents"] > 0
    assert added.data["widget"] == "food_cart"

    viewed = await registry.call("thuisbezorgd_cart", {"action": "view"})
    assert viewed.data["cart"]["items"]
    cleared = await registry.call("thuisbezorgd_cart", {"action": "clear"})
    assert cleared.data["cart"]["items"] == []


async def test_order_defaults_to_dry_run_no_submit(monkeypatch):
    _reset_kitchen()
    _set_delivery(monkeypatch)
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    await registry.call(
        "thuisbezorgd_cart",
        {"action": "add", "restaurant_id": "resto-napoli", "item_id": "napoli-diavola"},
    )
    result = await registry.call("thuisbezorgd_order", {})
    assert result.needs_confirm
    assert result.dry_run
    assert result.data.get("restaurant", {}).get("name") == "Pizzeria Napoli"
    assert result.data.get("items")
    assert result.data.get("total")
    assert "Voorbeeldstraat" in (result.data.get("delivery_address") or "")
    assert mock_thuisbezorgd.orders == []
    # Cart must still be intact — dry-run must not place.
    assert mock_thuisbezorgd.cart_snapshot()["items"]


async def test_order_refuses_without_confirm_even_if_handler_args_look_ready(monkeypatch):
    _reset_kitchen()
    _set_delivery(monkeypatch)
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    await registry.call(
        "thuisbezorgd_cart",
        {"action": "add", "restaurant_id": "resto-burgerbar", "item_id": "bb-classic"},
    )
    # Explicit dry_run / missing confirm — registry must block before place_order.
    blocked = await registry.call("thuisbezorgd_order", {"dry_run": True})
    assert blocked.needs_confirm
    assert blocked.dry_run
    assert mock_thuisbezorgd.orders == []


async def test_order_with_confirm_places_mock_only(monkeypatch):
    _reset_kitchen()
    _set_delivery(monkeypatch)
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    await registry.call(
        "thuisbezorgd_cart",
        {"action": "add", "restaurant_id": "resto-saigon", "item_id": "saigon-pho"},
    )
    result = await registry.call("thuisbezorgd_order", {"confirm": True})
    assert result.ok
    assert not result.needs_confirm
    assert result.data["submitted"] is True
    assert result.data["live"] is False
    assert result.data["mode"] == "mock"
    assert result.data["order"]["id"].startswith("mock-order-")
    assert "Saigon" in (result.data["order"]["restaurant"] or {}).get("name", "")
    assert "Voorbeeldstraat" in result.data["order"]["delivery_address"]
    assert mock_thuisbezorgd.cart_snapshot()["items"] == []
    assert len(mock_thuisbezorgd.orders) == 1


async def test_empty_cart_confirm_does_not_submit(monkeypatch):
    _reset_kitchen()
    _set_delivery(monkeypatch)
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    result = await registry.call("thuisbezorgd_order", {"confirm": True})
    assert not result.ok or result.data.get("ok") is False
    assert result.data.get("submitted") is False
    assert mock_thuisbezorgd.orders == []


async def test_auth_status_never_leaks_secrets(monkeypatch):
    _reset_kitchen()
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "secret-partner-key")
    monkeypatch.setattr(settings, "thuisbezorgd_email", "ruben@example.com")
    monkeypatch.setattr(settings, "thuisbezorgd_password", "super-secret-password")
    monkeypatch.setattr(settings, "thuisbezorgd_session_token", "jwt-should-not-leak")
    _set_delivery(monkeypatch)
    result = await registry.call("thuisbezorgd_auth_status", {})
    blob = str(result.data)
    assert "super-secret-password" not in blob
    assert "jwt-should-not-leak" not in blob
    assert "secret-partner-key" not in blob
    assert "ruben@example.com" not in blob
    assert result.data["partner_api_key"] is True
    assert result.data["consumer_credentials"] is True
    assert result.data["session"] is True


async def test_live_submit_ready_requires_partner_and_session(monkeypatch):
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    monkeypatch.setattr(settings, "thuisbezorgd_session_token", "")
    monkeypatch.setattr(settings, "thuisbezorgd_email", "")
    monkeypatch.setattr(settings, "thuisbezorgd_password", "")
    thuisbezorgd._session_token = ""
    assert thuisbezorgd.live_submit_ready is False

    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "partner")
    monkeypatch.setattr(settings, "thuisbezorgd_session_token", "tok")
    assert thuisbezorgd.live_submit_ready is True


async def test_intent_food_routes_to_thuisbezorgd():
    plan = route_intent("I'm hungry, show nearby restaurants")
    assert plan["tool"] == "thuisbezorgd_restaurants"
    pizza = route_intent("order pizza from thuisbezorgd")
    assert pizza["tool"] == "thuisbezorgd_restaurants"
    assert pizza["args"].get("cuisine") == "pizza"
    checkout = route_intent("place the order from my cart")
    assert checkout["tool"] == "thuisbezorgd_order"


async def test_no_auto_reorder(monkeypatch):
    _reset_kitchen()
    _set_delivery(monkeypatch)
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    await registry.call(
        "thuisbezorgd_cart",
        {"action": "add", "restaurant_id": "resto-napoli", "item_id": "napoli-garlic"},
    )
    first = await registry.call("thuisbezorgd_order", {"confirm": True})
    assert first.data["submitted"] is True
    # Second confirm with empty cart must not invent a reorder.
    second = await registry.call("thuisbezorgd_order", {"confirm": True})
    assert second.data.get("submitted") is False
    assert len(mock_thuisbezorgd.orders) == 1
