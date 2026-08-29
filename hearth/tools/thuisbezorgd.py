"""Thuisbezorgd (Just Eat Takeaway NL) — browse restaurants, cart, and place food orders.

Official consumer ordering is not self-serve for third-party house agents. Just Eat
Takeaway exposes partner/POS APIs (JE-API-KEY) and internal consumer JWTs; neither is
a public OAuth app for Hearth. This module:

- Always supports mock browse / cart / confirm-gated place (fixtures).
- Talks live only when ``THUISBEZORGD_API_KEY`` (partner) is set — never scrapes
  thuisbezorgd.nl.
- Keeps credentials and session tokens on the server (env + ``data/`` file).
- Never logs email/password/token values.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import MockThuisbezorgd, mock_thuisbezorgd

log = logging.getLogger("hearth.thuisbezorgd")

# Public widget keys a separate UI branch may render later. Do not block on widgets.
WIDGET_RESTAURANTS = "restaurant_list"
WIDGET_MENU = "restaurant_menu"
WIDGET_CART = "food_cart"
WIDGET_ORDER = "order_status"


def _session_path() -> Path:
    return Path(settings.auth_db_path).resolve().parent / "thuisbezorgd-session.json"


class Thuisbezorgd:
    """Sibling house service: food delivery via Thuisbezorgd / Just Eat Takeaway NL."""

    def __init__(self, kitchen: MockThuisbezorgd | None = None) -> None:
        self._client: httpx.AsyncClient | None = None
        self._kitchen = kitchen or mock_thuisbezorgd
        self._session_token: str = ""
        self._load_session()

    # --- configuration / address -------------------------------------------------

    @property
    def partner_configured(self) -> bool:
        return bool(settings.thuisbezorgd_api_key.strip())

    @property
    def consumer_credentials_configured(self) -> bool:
        email = settings.thuisbezorgd_email.strip()
        password = settings.thuisbezorgd_password.strip()
        return bool(email and password)

    @property
    def session_configured(self) -> bool:
        return bool(self._session_token.strip() or settings.thuisbezorgd_session_token.strip())

    @property
    def live(self) -> bool:
        """True when a legitimate partner API key is present (not consumer-site scrape)."""
        return self.partner_configured

    @property
    def live_submit_ready(self) -> bool:
        """Live paid submit needs partner key + (session or consumer credentials)."""
        return self.partner_configured and (self.session_configured or self.consumer_credentials_configured)

    def delivery_address(self) -> dict[str, Any]:
        street = settings.hearth_delivery_street.strip()
        postcode = settings.hearth_delivery_postcode.strip()
        city = settings.hearth_delivery_city.strip()
        country = (settings.hearth_delivery_country or "NL").strip() or "NL"
        configured = bool(street and postcode and city)
        return {
            "configured": configured,
            "street": street,
            "postcode": postcode,
            "city": city,
            "country": country,
            "line": ", ".join(p for p in (street, postcode, city, country) if p) if configured else "",
            "hint": (
                None
                if configured
                else "Set HEARTH_DELIVERY_STREET, HEARTH_DELIVERY_POSTCODE, and HEARTH_DELIVERY_CITY in host .env"
            ),
        }

    def auth_status(self) -> dict[str, Any]:
        """Safe status for the agent/UI — never returns secrets."""
        address = self.delivery_address()
        return {
            "ok": True,
            "service": "thuisbezorgd",
            "tenant": settings.thuisbezorgd_tenant,
            "mode": "live" if self.live else "mock",
            "partner_api_key": self.partner_configured,
            "consumer_credentials": self.consumer_credentials_configured,
            "session": self.session_configured,
            "live_submit_ready": self.live_submit_ready,
            "delivery_address": {
                "configured": address["configured"],
                "line": address["line"],
                "hint": address.get("hint"),
            },
            "note": (
                "Just Eat Takeaway has no public self-serve consumer ordering API for "
                "house agents. Live submit needs a partner JE-API-KEY (and consumer "
                "session). Without that, browse/cart/confirm run against fixtures only."
            ),
        }

    # --- session store (server-side only) ----------------------------------------

    def _effective_token(self) -> str:
        return (self._session_token or settings.thuisbezorgd_session_token or "").strip()

    def _load_session(self) -> None:
        path = _session_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("thuisbezorgd session file unreadable; ignoring")
            return
        token = str(raw.get("access_token") or raw.get("token") or "").strip()
        if token:
            self._session_token = token

    def _save_session(self, token: str) -> None:
        token = (token or "").strip()
        self._session_token = token
        path = _session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not token:
            if path.is_file():
                path.unlink()
            return
        # Never write email/password. Token only.
        path.write_text(
            json.dumps({"access_token": token, "tenant": settings.thuisbezorgd_tenant}, indent=2),
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass

    async def ensure_session(self) -> dict[str, Any]:
        """Use env session token or partner-authenticated consumer login when configured.

        Does not scrape the consumer site. Without partner credentials, returns mock auth.
        """
        if self._effective_token():
            if settings.thuisbezorgd_session_token.strip() and not self._session_token:
                self._save_session(settings.thuisbezorgd_session_token.strip())
            return {
                "ok": True,
                "mode": "live" if self.live else "mock",
                "authenticated": True,
                "source": "session",
            }
        if not self.live:
            # Mock path: treat as signed-in house account without storing secrets.
            return {
                "ok": True,
                "mode": "mock",
                "authenticated": True,
                "source": "fixture",
                "note": "No partner API key — using mock account.",
            }
        if not self.consumer_credentials_configured:
            return {
                "ok": False,
                "mode": "live",
                "authenticated": False,
                "error": (
                    "Thuisbezorgd live auth needs THUISBEZORGD_SESSION_TOKEN or "
                    "THUISBEZORGD_EMAIL + THUISBEZORGD_PASSWORD in host .env "
                    "(plus THUISBEZORGD_API_KEY)."
                ),
            }
        # Partner-gated consumer token exchange — documented env shape only.
        # Real endpoint varies by JET market; without a contracted partner path we refuse.
        return {
            "ok": False,
            "mode": "live",
            "authenticated": False,
            "error": (
                "Partner API key is set but consumer token exchange is not available "
                "without a Just Eat Takeaway partner agreement. Set "
                "THUISBEZORGD_SESSION_TOKEN from an approved flow, or clear the API key "
                "to stay on fixtures."
            ),
        }

    # --- HTTP --------------------------------------------------------------------

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Accept": "application/json",
                "Accept-Tenant": settings.thuisbezorgd_tenant,
            }
            key = settings.thuisbezorgd_api_key.strip()
            if key:
                headers["Authorization"] = f"JE-API-KEY {key}"
            token = self._effective_token()
            if token and "Authorization" not in headers:
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.AsyncClient(
                base_url=settings.thuisbezorgd_api_base.rstrip("/"),
                headers=headers,
                timeout=15.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- browse / cart / order ---------------------------------------------------

    async def restaurants(self, *, cuisine: str = "", query: str = "") -> dict[str, Any]:
        address = self.delivery_address()
        if not address["configured"]:
            return {
                "ok": False,
                "service": "thuisbezorgd",
                "error": address.get("hint") or "delivery address not configured",
                "delivery_address": address,
                "widget": WIDGET_RESTAURANTS,
                "restaurants": [],
            }

        if not self.live:
            rows = self._kitchen.list_restaurants(cuisine=cuisine, query=query)
            return {
                "ok": True,
                "mode": "mock",
                "service": "thuisbezorgd",
                "delivery_address": {"line": address["line"], "configured": True},
                "query": query or None,
                "cuisine": cuisine or None,
                "restaurants": rows,
                "widget": WIDGET_RESTAURANTS,
                "speak": _speak_restaurants(rows, mock=True),
            }

        client = await self._http()
        try:
            # Partner discovery shape (postcode). Exact path depends on contracted API.
            response = await client.get(
                f"/restaurants/{settings.thuisbezorgd_tenant}",
                params={
                    "postcode": address["postcode"],
                    "cuisine": cuisine or None,
                    "restaurantName": query or None,
                },
            )
            response.raise_for_status()
            payload = response.json() or {}
            rows = _normalize_restaurants(payload)
            return {
                "ok": True,
                "mode": "live",
                "service": "thuisbezorgd",
                "delivery_address": {"line": address["line"], "configured": True},
                "restaurants": rows,
                "widget": WIDGET_RESTAURANTS,
                "speak": _speak_restaurants(rows, mock=False),
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                rows = self._kitchen.list_restaurants(cuisine=cuisine, query=query)
                return {
                    "ok": True,
                    "mode": "mock",
                    "service": "thuisbezorgd",
                    "error": str(exc),
                    "delivery_address": {"line": address["line"], "configured": True},
                    "restaurants": rows,
                    "widget": WIDGET_RESTAURANTS,
                    "speak": _speak_restaurants(rows, mock=True),
                }
            return {"ok": False, "mode": "live", "service": "thuisbezorgd", "error": str(exc)}

    async def menu(self, restaurant_id: str) -> dict[str, Any]:
        restaurant_id = (restaurant_id or "").strip()
        if not restaurant_id:
            return {"ok": False, "error": "restaurant_id required", "widget": WIDGET_MENU}

        if not self.live:
            result = self._kitchen.get_menu(restaurant_id)
            if not result.get("ok"):
                return {**result, "mode": "mock", "widget": WIDGET_MENU}
            return {
                **result,
                "mode": "mock",
                "service": "thuisbezorgd",
                "widget": WIDGET_MENU,
                "speak": _speak_menu(result),
            }

        client = await self._http()
        try:
            response = await client.get(
                f"/restaurants/{settings.thuisbezorgd_tenant}/{restaurant_id}/menu"
            )
            response.raise_for_status()
            payload = response.json() or {}
            normalized = _normalize_menu(restaurant_id, payload)
            return {
                **normalized,
                "mode": "live",
                "service": "thuisbezorgd",
                "widget": WIDGET_MENU,
                "speak": _speak_menu(normalized),
            }
        except Exception as exc:  # noqa: BLE001
            if settings.mock_if_unconfigured:
                result = self._kitchen.get_menu(restaurant_id)
                return {
                    **result,
                    "mode": "mock",
                    "error": str(exc),
                    "widget": WIDGET_MENU,
                    "speak": _speak_menu(result) if result.get("ok") else None,
                }
            return {"ok": False, "mode": "live", "error": str(exc)}

    def cart_view(self) -> dict[str, Any]:
        cart = self._kitchen.cart_snapshot()
        return {
            "ok": True,
            "mode": "mock" if not self.live else "live",
            "service": "thuisbezorgd",
            "cart": cart,
            "widget": WIDGET_CART,
            "speak": _speak_cart(cart),
        }

    def cart_add(
        self,
        *,
        restaurant_id: str,
        item_id: str,
        quantity: int = 1,
        notes: str = "",
    ) -> dict[str, Any]:
        result = self._kitchen.add_to_cart(
            restaurant_id=restaurant_id,
            item_id=item_id,
            quantity=quantity,
            notes=notes,
        )
        cart = result.get("cart") or self._kitchen.cart_snapshot()
        return {
            **result,
            "mode": "mock" if not self.live else "session",
            "service": "thuisbezorgd",
            "widget": WIDGET_CART,
            "speak": _speak_cart(cart),
        }

    def cart_remove(self, item_id: str) -> dict[str, Any]:
        result = self._kitchen.remove_from_cart(item_id)
        cart = result.get("cart") or self._kitchen.cart_snapshot()
        return {
            **result,
            "mode": "mock" if not self.live else "session",
            "service": "thuisbezorgd",
            "widget": WIDGET_CART,
            "speak": _speak_cart(cart),
        }

    def cart_clear(self) -> dict[str, Any]:
        result = self._kitchen.clear_cart()
        return {
            **result,
            "mode": "mock" if not self.live else "session",
            "service": "thuisbezorgd",
            "widget": WIDGET_CART,
            "speak": "Cart cleared.",
        }

    async def place_order(self) -> dict[str, Any]:
        """Place the current cart. Caller must have passed confirm via the registry.

        Never auto-reorders. Without live_submit_ready, places a fixture order only.
        """
        address = self.delivery_address()
        if not address["configured"]:
            return {
                "ok": False,
                "service": "thuisbezorgd",
                "submitted": False,
                "error": address.get("hint") or "delivery address not configured",
            }

        cart = self._kitchen.cart_snapshot()
        if not cart.get("items"):
            return {
                "ok": False,
                "service": "thuisbezorgd",
                "submitted": False,
                "error": "cart is empty — add items before ordering",
                "widget": WIDGET_CART,
                "cart": cart,
            }

        preview = {
            "restaurant": cart.get("restaurant"),
            "items": cart.get("items"),
            "total_cents": cart.get("total_cents"),
            "total": cart.get("total"),
            "currency": cart.get("currency") or "EUR",
            "delivery_address": address["line"],
        }

        if not self.live_submit_ready:
            order = self._kitchen.place_order(delivery_line=address["line"])
            return {
                "ok": True,
                "mode": "mock",
                "service": "thuisbezorgd",
                "submitted": True,
                "live": False,
                "order": order,
                "preview": preview,
                "widget": WIDGET_ORDER,
                "speak": _speak_order(order, mock=True),
                "note": (
                    "Fixture order only. Live paid submit needs THUISBEZORGD_API_KEY "
                    "plus an approved consumer session (THUISBEZORGD_SESSION_TOKEN)."
                ),
            }

        auth = await self.ensure_session()
        if not auth.get("authenticated"):
            return {
                "ok": False,
                "mode": "live",
                "service": "thuisbezorgd",
                "submitted": False,
                "error": auth.get("error") or "not authenticated",
                "preview": preview,
            }

        client = await self._http()
        body = {
            "restaurantId": cart.get("restaurant_id"),
            "items": [
                {
                    "productId": item.get("item_id"),
                    "quantity": item.get("quantity"),
                    "notes": item.get("notes") or "",
                }
                for item in cart.get("items") or []
            ],
            "deliveryAddress": {
                "street": address["street"],
                "postcode": address["postcode"],
                "city": address["city"],
                "country": address["country"],
            },
        }
        try:
            response = await client.post(
                f"/orders/{settings.thuisbezorgd_tenant}",
                json=body,
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
            self._kitchen.clear_cart()
            order = {
                "id": str(payload.get("id") or payload.get("orderId") or "live"),
                "status": payload.get("status") or "submitted",
                "restaurant": cart.get("restaurant"),
                "items": deepcopy(cart.get("items") or []),
                "total_cents": cart.get("total_cents"),
                "total": cart.get("total"),
                "currency": "EUR",
                "delivery_address": address["line"],
                "live": True,
            }
            return {
                "ok": True,
                "mode": "live",
                "service": "thuisbezorgd",
                "submitted": True,
                "live": True,
                "order": order,
                "preview": preview,
                "widget": WIDGET_ORDER,
                "speak": _speak_order(order, mock=False),
            }
        except Exception as exc:  # noqa: BLE001
            # Never silently charge via a fallback scrape. Surface the failure.
            return {
                "ok": False,
                "mode": "live",
                "service": "thuisbezorgd",
                "submitted": False,
                "error": str(exc),
                "preview": preview,
                "note": (
                    "Live submit failed. Cart was not cleared. No consumer-site scrape fallback."
                ),
            }


def _normalize_restaurants(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("Restaurants") or payload.get("restaurants") or payload.get("results") or []
    if isinstance(payload, list):
        rows = payload
    out: list[dict[str, Any]] = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "id": str(row.get("Id") or row.get("id") or row.get("UniqueName") or ""),
                "name": row.get("Name") or row.get("name") or "Restaurant",
                "cuisine": row.get("Cuisine") or row.get("cuisine") or row.get("CuisineTypes") or [],
                "rating": row.get("RatingStars") or row.get("rating"),
                "delivery_fee_cents": row.get("delivery_fee_cents"),
                "eta_minutes": row.get("eta_minutes") or row.get("DeliveryEtaMinutes"),
            }
        )
    return out


def _normalize_menu(restaurant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    categories = payload.get("categories") or payload.get("Categories") or []
    items: list[dict[str, Any]] = []
    for cat in categories:
        cat_name = cat.get("name") or cat.get("Name") or "Menu"
        for product in cat.get("products") or cat.get("Products") or []:
            items.append(
                {
                    "id": str(product.get("id") or product.get("Id") or ""),
                    "name": product.get("name") or product.get("Name"),
                    "description": product.get("description") or product.get("Description") or "",
                    "price_cents": int(
                        product.get("price_cents")
                        or round(float(product.get("Price") or product.get("price") or 0) * 100)
                    ),
                    "price": _euros(
                        int(
                            product.get("price_cents")
                            or round(float(product.get("Price") or product.get("price") or 0) * 100)
                        )
                    ),
                    "category": cat_name,
                }
            )
    return {
        "ok": True,
        "restaurant_id": restaurant_id,
        "restaurant": payload.get("name") or payload.get("Name") or restaurant_id,
        "items": items,
    }


def _euros(cents: int) -> str:
    return f"€{cents / 100:.2f}"


def _speak_restaurants(rows: list[dict[str, Any]], *, mock: bool) -> str:
    suffix = " (mock)" if mock else ""
    if not rows:
        return f"No restaurants found for that address{suffix}."
    bits = []
    for row in rows[:5]:
        name = row.get("name") or "Restaurant"
        eta = row.get("eta_minutes")
        bits.append(f"{name}" + (f" (~{eta} min)" if eta else ""))
    return f"Nearby{suffix}: " + "; ".join(bits) + "."


def _speak_menu(data: dict[str, Any]) -> str:
    name = data.get("restaurant") or "the restaurant"
    items = data.get("items") or []
    if not items:
        return f"No menu items for {name}."
    bits = [f"{i.get('name')} {i.get('price')}" for i in items[:6] if i.get("name")]
    return f"{name} menu: " + "; ".join(bits) + "."


def _speak_cart(cart: dict[str, Any]) -> str:
    items = cart.get("items") or []
    if not items:
        return "Your Thuisbezorgd cart is empty."
    restaurant = (cart.get("restaurant") or {}).get("name") or "restaurant"
    bits = [f"{i.get('quantity')}× {i.get('name')}" for i in items]
    return f"Cart at {restaurant}: " + ", ".join(bits) + f" — {cart.get('total')}."


def _speak_order(order: dict[str, Any], *, mock: bool) -> str:
    suffix = " (mock — not charged)" if mock else ""
    restaurant = order.get("restaurant") or {}
    name = restaurant.get("name") if isinstance(restaurant, dict) else str(restaurant)
    return (
        f"Order {order.get('id')} placed at {name or 'restaurant'} for "
        f"{order.get('total')} to {order.get('delivery_address')}{suffix}."
    )


thuisbezorgd = Thuisbezorgd()
