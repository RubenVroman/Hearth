"""House-shaped fixtures used when HA / Plex / Docker are unconfigured."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MOCK_HA_STATES: list[dict[str, Any]] = [
    {
        "entity_id": "light.living_room",
        "state": "on",
        "attributes": {
            "friendly_name": "Living room",
            "brightness": 180,
            "color_temp": 370,
        },
    },
    {
        "entity_id": "light.kitchen",
        "state": "off",
        "attributes": {"friendly_name": "Kitchen", "brightness": 0},
    },
    {
        "entity_id": "light.office",
        "state": "on",
        "attributes": {"friendly_name": "Office", "brightness": 120},
    },
    {
        "entity_id": "scene.movie_night",
        "state": "off",
        "attributes": {"friendly_name": "Movie night"},
    },
    {
        "entity_id": "scene.good_night",
        "state": "off",
        "attributes": {"friendly_name": "Good night"},
    },
    {
        "entity_id": "media_player.denon_avr_x3700h",
        "state": "playing",
        "attributes": {
            "friendly_name": "Denon AVR-X3700H",
            "source": "Media Player",
            "volume_level": 0.32,
            "is_volume_muted": False,
            "media_title": "Dune: Part Two",
        },
    },
    {
        "entity_id": "media_player.lg_webos_tv",
        "state": "on",
        "attributes": {
            "friendly_name": "LG webOS TV",
            "source": "HDMI 1",
            "volume_level": 0.0,
        },
    },
    {
        "entity_id": "media_player.apple_tv",
        "state": "idle",
        "attributes": {
            "friendly_name": "Living Room Apple TV",
            "app_name": None,
            "source_list": ["Infuse", "Plex", "TV"],
            "volume_level": 0.4,
        },
    },
]

MOCK_PLEX_SESSIONS: dict[str, Any] = {
    "MediaContainer": {
        "size": 1,
        "Metadata": [
            {
                "title": "Dune: Part Two",
                "type": "movie",
                "year": 2024,
                "ratingKey": "1001",
                "key": "/library/metadata/1001",
                "guid": "plex://movie/dune-part-two",
                "summary": "Paul Atreides unites with Chani and the Fremen while seeking revenge against the conspirators who destroyed his family.",
                "duration": 16600000,
                "viewOffset": 4920000,
                "tmdbId": 693134,
                "posterPath": "/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
                "Player": {
                    "title": "Apple TV",
                    "state": "playing",
                    "local": True,
                },
                "User": {"title": "Ruben"},
                "grandparentTitle": None,
            }
        ],
    }
}

# Library titles for mock search / play (not only now-playing sessions).
MOCK_PLEX_LIBRARY: list[dict[str, Any]] = [
    {
        "title": "Dune: Part Two",
        "type": "movie",
        "year": 2024,
        "ratingKey": "1001",
        "key": "/library/metadata/1001",
        "guid": "plex://movie/dune-part-two",
        "summary": "Paul Atreides unites with Chani and the Fremen while seeking revenge against the conspirators who destroyed his family.",
        "contentRating": "PG-13",
        "audienceRating": 8.8,
        "Guid": [{"id": "tmdb://693134"}, {"id": "imdb://tt15239678"}],
        "tmdbId": 693134,
        "posterPath": "/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
    },
    {
        "title": "The Endless",
        "type": "movie",
        "year": 2017,
        "ratingKey": "2042",
        "key": "/library/metadata/2042",
        "guid": "plex://movie/the-endless",
        "summary": "Two brothers return to the UFO death cult they fled from years earlier to discover the group’s secret.",
        "contentRating": "NR",
        "audienceRating": 6.5,
        "Guid": [{"id": "tmdb://430231"}, {"id": "imdb://tt3986820"}],
        "tmdbId": 430231,
        "posterPath": "/uVHPBTLb6Sj1Eso9HzyBAOMRheM.jpg",
    },
    {
        "title": "The Brutalist",
        "type": "movie",
        "year": 2024,
        "ratingKey": "1002",
        "key": "/library/metadata/1002",
        "guid": "plex://movie/the-brutalist",
        "summary": "A Hungarian-born Jewish architect starts over in America.",
        "contentRating": "R",
        "audienceRating": 7.9,
        "Guid": [{"id": "tmdb://974950"}],
        "tmdbId": 974950,
        "posterPath": "/7seqaCaaXDNUHOx4DqwpoOH8pPa.jpg",
    },
    # Two exact-title editions so ambiguous library matches can be tested.
    {
        "title": "Heat",
        "type": "movie",
        "year": 1995,
        "ratingKey": "3001",
        "key": "/library/metadata/3001",
        "guid": "plex://movie/heat-1995",
        "summary": "A group of professional bank robbers start to feel the heat from police when they unknowingly leave a clue at their latest heist.",
        "contentRating": "R",
        "audienceRating": 8.3,
        "Guid": [{"id": "tmdb://949"}],
        "tmdbId": 949,
        "posterPath": "/gKaePbkEkaqvMtw74EyhhkfCKKh.jpg",
    },
    {
        "title": "Heat",
        "type": "movie",
        "year": 1986,
        "ratingKey": "3002",
        "key": "/library/metadata/3002",
        "guid": "plex://movie/heat-1986",
        "summary": "An ex-mercenary living in Las Vegas is hired to protect an old friend.",
        "contentRating": "R",
        "audienceRating": 5.7,
        "Guid": [{"id": "tmdb://10784"}],
        "tmdbId": 10784,
        "posterPath": "/fMhOeJ2TvuY46iYGmsowhgRXfnr.jpg",
    },
    {
        "title": "Hide and Seek",
        "type": "episode",
        "year": 2022,
        "ratingKey": "4001",
        "key": "/library/metadata/4001",
        "guid": "plex://episode/severance-s1e1",
        "grandparentTitle": "Severance",
        "parentIndex": 1,
        "index": 1,
        "summary": "Mark discovers a mysterious self-help book that leads him to Lumon Industries.",
        "contentRating": "TV-MA",
        "audienceRating": 8.7,
        "Guid": [{"id": "tmdb://95396"}],
        "tmdbId": 95396,
        "season": 1,
        "episode": 1,
        "posterPath": "/pPHpeI2X1qEd1CS1SeyrdhZ4qnT.jpg",
    },
]

MOCK_PLEX_CLIENTS: list[dict[str, Any]] = [
    {
        "name": "Apple TV",
        "host": "192.168.1.40",
        "machineIdentifier": "mock-apple-tv",
        "product": "Plex for Apple TV",
        "deviceClass": "stb",
        "version": "8.0",
        "protocolCapabilities": ["timeline", "playback", "navigation", "playqueues"],
        "controllable": True,
    },
    {
        "name": "LG webOS TV",
        "host": "192.168.1.41",
        "machineIdentifier": "mock-lg-webos",
        "product": "Plex for LG",
        "deviceClass": "tv",
        "version": "5.0",
        "protocolCapabilities": ["timeline", "playback", "navigation"],
        "controllable": True,
    },
]
MOCK_WEATHER: dict[str, Any] = {
    "ok": True,
    "mode": "mock",
    "place": "Home",
    "latitude": 51.05,
    "longitude": 3.72,
    "temperature": 14,
    "temperature_unit": "°C",
    "humidity": 68,
    "wind_speed": 12,
    "wind_unit": "km/h",
    "weather_code": 2,
    "condition": "Partly cloudy",
    "observed_at": "2026-08-29T12:00",
}

# Compact live-web fixtures (used when no OpenAI/Brave key and mock-if-unconfigured).
MOCK_WEB_SEARCH_RESULTS: list[dict[str, Any]] = [
    {
        "title": "JustWatch — streaming search",
        "url": "https://www.justwatch.com/",
        "snippet": "Where to watch movies and shows across Netflix, Disney+, Prime, and more.",
        "source": "justwatch.com",
    },
    {
        "title": "VRT NWS",
        "url": "https://www.vrt.be/vrtnws/nl/",
        "snippet": "Belgian news headlines and current events.",
        "source": "vrt.be",
    },
    {
        "title": "Wikipedia",
        "url": "https://en.wikipedia.org/wiki/Main_Page",
        "snippet": "Background articles. Not a live news wire.",
        "source": "wikipedia.org",
    },
]


MOCK_DOCKER_CONTAINERS: list[dict[str, Any]] = [
    {"Id": "plex01", "Names": ["/plex"], "Image": "lscr.io/linuxserver/plex", "State": "running", "Status": "Up 3 days"},
    {"Id": "sonarr01", "Names": ["/sonarr"], "Image": "lscr.io/linuxserver/sonarr", "State": "running", "Status": "Up 3 days"},
    {"Id": "radarr01", "Names": ["/radarr"], "Image": "lscr.io/linuxserver/radarr", "State": "running", "Status": "Up 3 days"},
    {"Id": "prowlarr01", "Names": ["/prowlarr"], "Image": "lscr.io/linuxserver/prowlarr", "State": "running", "Status": "Up 3 days"},
    {"Id": "overseerr01", "Names": ["/overseerr"], "Image": "lscr.io/linuxserver/overseerr", "State": "running", "Status": "Up 3 days"},
    {"Id": "gluetun01", "Names": ["/gluetun"], "Image": "qmcgaw/gluetun", "State": "running", "Status": "Up 3 days"},
    {"Id": "hearth01", "Names": ["/hearth"], "Image": "hearth:local", "State": "running", "Status": "Up 12 minutes"},
    {"Id": "ha01", "Names": ["/hearth-ha"], "Image": "home-assistant", "State": "running", "Status": "Up 12 minutes"},
]


MOCK_RADARR_LOOKUP: list[dict[str, Any]] = [
    {
        "title": "Dune: Part Two",
        "year": 2024,
        "tmdbId": 693134,
        "overview": "Paul Atreides unites with Chani and the Fremen.",
        "status": "released",
        "posterPath": "/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
    },
    {
        "title": "The Brutalist",
        "year": 2024,
        "tmdbId": 974950,
        "overview": "A Hungarian-born Jewish architect starts over in America.",
        "status": "released",
        "posterPath": "/7seqaCaaXDNUHOx4DqwpoOH8pPa.jpg",
    },
    {
        "title": "The Endless",
        "year": 2017,
        "tmdbId": 430231,
        "overview": "Two brothers return to a UFO death cult.",
        "status": "released",
        "posterPath": "/uVHPBTLb6Sj1Eso9HzyBAOMRheM.jpg",
    },
    {
        "title": "Annihilation",
        "year": 2018,
        "tmdbId": 300668,
        "overview": "A biologist enters the Shimmer after her husband's return.",
        "status": "released",
    },
]

MOCK_SONARR_LOOKUP: list[dict[str, Any]] = [
    {
        "title": "Severance",
        "year": 2022,
        "tvdbId": 371980,
        "overview": "Mark Scout leads a team whose memories are split.",
        "status": "continuing",
        "posterPath": "/pPHpeI2X1qEd1CS1SeyrdhZ4qnT.jpg",
    },
    {
        "title": "Slow Horses",
        "year": 2022,
        "tvdbId": 397382,
        "overview": "Misfit spies at MI5's Slough House.",
        "status": "continuing",
    },
]

# Active download-client queue fixtures (Radarr/Sonarr /api/v3/queue shape).
MOCK_RADARR_DOWNLOADS: list[dict[str, Any]] = [
    {
        "id": 1,
        "title": "Annihilation",
        "status": "downloading",
        "trackedDownloadState": "downloading",
        "trackedDownloadStatus": "ok",
        "size": 8_000_000_000,
        "sizeleft": 2_000_000_000,
        "timeleft": "00:25:00",
        "indexer": "MockIndexer",
        "quality": {"quality": {"name": "Bluray-1080p"}},
        "downloadClient": "qBittorrent",
        "movie": {"title": "Annihilation", "year": 2018, "tmdbId": 300668},
    },
    {
        "id": 2,
        "title": "Dune: Part Two",
        "status": "queued",
        "trackedDownloadState": "downloading",
        "trackedDownloadStatus": "ok",
        "size": 12_000_000_000,
        "sizeleft": 12_000_000_000,
        "timeleft": "00:00:00",
        "indexer": "MockIndexer",
        "quality": {"quality": {"name": "Bluray-2160p"}},
        "downloadClient": "qBittorrent",
        "movie": {"title": "Dune: Part Two", "year": 2024, "tmdbId": 693134},
    },
]

MOCK_SONARR_DOWNLOADS: list[dict[str, Any]] = [
    {
        "id": 11,
        "title": "Severance - S02E03",
        "status": "downloading",
        "trackedDownloadState": "downloading",
        "trackedDownloadStatus": "ok",
        "size": 2_500_000_000,
        "sizeleft": 1_000_000_000,
        "timeleft": "00:12:00",
        "indexer": "MockIndexer",
        "quality": {"quality": {"name": "WEBDL-1080p"}},
        "downloadClient": "qBittorrent",
        "series": {"title": "Severance", "year": 2022, "tvdbId": 371980},
    },
]

MOCK_OVERSEERR_RESULTS: list[dict[str, Any]] = [
    {
        "id": 693134,
        "mediaType": "movie",
        "title": "Dune: Part Two",
        "year": 2024,
        "posterPath": "/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
    },
    {
        "id": 95396,
        "mediaType": "tv",
        "title": "Severance",
        "year": 2022,
        "posterPath": "/pPHpeI2X1qEd1CS1SeyrdhZ4qnT.jpg",
    },
]


class MockPipeline:
    """In-memory Radarr / Sonarr / Overseerr so grab/request works without keys."""

    def __init__(self) -> None:
        self.radarr_queue: list[dict[str, Any]] = []
        self.sonarr_queue: list[dict[str, Any]] = []
        self.overseerr_queue: list[dict[str, Any]] = []
        self.radarr_downloads: list[dict[str, Any]] | None = None
        self.sonarr_downloads: list[dict[str, Any]] | None = None

    def search_radarr(self, query: str) -> list[dict[str, Any]]:
        return _filter_title(MOCK_RADARR_LOOKUP, query)

    def search_sonarr(self, query: str) -> list[dict[str, Any]]:
        return _filter_title(MOCK_SONARR_LOOKUP, query)

    def search_overseerr(self, query: str) -> list[dict[str, Any]]:
        return _filter_title(MOCK_OVERSEERR_RESULTS, query)

    def add_radarr(self, item: dict[str, Any]) -> dict[str, Any]:
        queued = {**item, "queued": True}
        self.radarr_queue.append(queued)
        return queued

    def add_sonarr(self, item: dict[str, Any]) -> dict[str, Any]:
        queued = {**item, "queued": True}
        self.sonarr_queue.append(queued)
        return queued

    def request_overseerr(self, item: dict[str, Any]) -> dict[str, Any]:
        queued = {**item, "requested": True}
        self.overseerr_queue.append(queued)
        return queued

    def list_radarr_downloads(self, title: str = "") -> list[dict[str, Any]]:
        source = self.radarr_downloads if self.radarr_downloads is not None else MOCK_RADARR_DOWNLOADS
        return _filter_download_title(source, title)

    def list_sonarr_downloads(self, title: str = "") -> list[dict[str, Any]]:
        source = self.sonarr_downloads if self.sonarr_downloads is not None else MOCK_SONARR_DOWNLOADS
        return _filter_download_title(source, title)


def _filter_title(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = (query or "").strip().lower()
    if not needle:
        return deepcopy(items[:5])
    hits = [deepcopy(item) for item in items if needle in str(item.get("title", "")).lower()]
    return hits or [deepcopy(items[0]) | {"title": items[0]["title"], "matched": "fallback"}]


def _download_title_for_filter(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "")
    movie = item.get("movie") if isinstance(item.get("movie"), dict) else {}
    series = item.get("series") if isinstance(item.get("series"), dict) else {}
    return " ".join(
        bit for bit in (title, str(movie.get("title") or ""), str(series.get("title") or "")) if bit
    )


def _filter_download_title(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Filter active downloads by title. No fallback — missing title means not downloading."""
    needle = (query or "").strip().lower()
    if not needle:
        return deepcopy(items)
    return [
        deepcopy(item)
        for item in items
        if needle in _download_title_for_filter(item).lower()
    ]


pipeline = MockPipeline()


MOCK_THUISBEZORGD_RESTAURANTS: list[dict[str, Any]] = [
    {
        "id": "resto-napoli",
        "name": "Pizzeria Napoli",
        "cuisine": ["Pizza", "Italian"],
        "rating": 4.6,
        "delivery_fee_cents": 249,
        "eta_minutes": 35,
    },
    {
        "id": "resto-saigon",
        "name": "Saigon Kitchen",
        "cuisine": ["Vietnamese", "Asian"],
        "rating": 4.4,
        "delivery_fee_cents": 199,
        "eta_minutes": 40,
    },
    {
        "id": "resto-burgerbar",
        "name": "Burger Bar VAULT",
        "cuisine": ["Burgers", "American"],
        "rating": 4.2,
        "delivery_fee_cents": 299,
        "eta_minutes": 30,
    },
]

MOCK_THUISBEZORGD_MENUS: dict[str, dict[str, Any]] = {
    "resto-napoli": {
        "restaurant_id": "resto-napoli",
        "restaurant": "Pizzeria Napoli",
        "items": [
            {
                "id": "napoli-margherita",
                "name": "Margherita",
                "description": "Tomato, mozzarella, basil",
                "price_cents": 950,
                "price": "€9.50",
                "category": "Pizza",
            },
            {
                "id": "napoli-diavola",
                "name": "Diavola",
                "description": "Spicy salami, mozzarella",
                "price_cents": 1250,
                "price": "€12.50",
                "category": "Pizza",
            },
            {
                "id": "napoli-garlic",
                "name": "Garlic bread",
                "description": "Side",
                "price_cents": 450,
                "price": "€4.50",
                "category": "Sides",
            },
        ],
    },
    "resto-saigon": {
        "restaurant_id": "resto-saigon",
        "restaurant": "Saigon Kitchen",
        "items": [
            {
                "id": "saigon-pho",
                "name": "Phở bò",
                "description": "Beef noodle soup",
                "price_cents": 1350,
                "price": "€13.50",
                "category": "Noodles",
            },
            {
                "id": "saigon-banhmi",
                "name": "Bánh mì",
                "description": "Crispy baguette",
                "price_cents": 850,
                "price": "€8.50",
                "category": "Sandwiches",
            },
        ],
    },
    "resto-burgerbar": {
        "restaurant_id": "resto-burgerbar",
        "restaurant": "Burger Bar VAULT",
        "items": [
            {
                "id": "bb-classic",
                "name": "Classic burger",
                "description": "Beef, cheddar, pickles",
                "price_cents": 1150,
                "price": "€11.50",
                "category": "Burgers",
            },
            {
                "id": "bb-fries",
                "name": "Fries",
                "description": "Salted",
                "price_cents": 350,
                "price": "€3.50",
                "category": "Sides",
            },
        ],
    },
}


class MockThuisbezorgd:
    """In-memory Thuisbezorgd kitchen so browse/cart/order work without partner keys."""

    def __init__(self) -> None:
        self.restaurants: list[dict[str, Any]] = deepcopy(MOCK_THUISBEZORGD_RESTAURANTS)
        self.menus: dict[str, dict[str, Any]] = deepcopy(MOCK_THUISBEZORGD_MENUS)
        self.cart: dict[str, Any] = _empty_cart()
        self.orders: list[dict[str, Any]] = []
        self._order_seq = 0

    def list_restaurants(self, *, cuisine: str = "", query: str = "") -> list[dict[str, Any]]:
        rows = deepcopy(self.restaurants)
        cuisine_needle = (cuisine or "").strip().lower()
        query_needle = (query or "").strip().lower()
        if cuisine_needle:
            rows = [
                r
                for r in rows
                if any(cuisine_needle in str(c).lower() for c in (r.get("cuisine") or []))
            ]
        if query_needle:
            rows = [r for r in rows if query_needle in str(r.get("name") or "").lower()]
        return rows

    def get_menu(self, restaurant_id: str) -> dict[str, Any]:
        menu = self.menus.get(restaurant_id)
        if menu is None:
            return {"ok": False, "error": f"unknown restaurant {restaurant_id}"}
        return {"ok": True, **deepcopy(menu)}

    def cart_snapshot(self) -> dict[str, Any]:
        return deepcopy(self.cart)

    def add_to_cart(
        self,
        *,
        restaurant_id: str,
        item_id: str,
        quantity: int = 1,
        notes: str = "",
    ) -> dict[str, Any]:
        restaurant_id = (restaurant_id or "").strip()
        item_id = (item_id or "").strip()
        qty = max(1, int(quantity or 1))
        menu = self.menus.get(restaurant_id)
        if menu is None:
            return {"ok": False, "error": f"unknown restaurant {restaurant_id}"}
        product = next((p for p in menu["items"] if p["id"] == item_id), None)
        if product is None:
            return {"ok": False, "error": f"unknown item {item_id}"}

        current_rid = self.cart.get("restaurant_id")
        if current_rid and current_rid != restaurant_id:
            return {
                "ok": False,
                "error": (
                    f"cart already has items from {self.cart.get('restaurant', {}).get('name')}; "
                    "clear the cart before ordering from another restaurant"
                ),
                "cart": self.cart_snapshot(),
            }

        resto = next((r for r in self.restaurants if r["id"] == restaurant_id), None)
        self.cart["restaurant_id"] = restaurant_id
        self.cart["restaurant"] = {
            "id": restaurant_id,
            "name": (resto or {}).get("name") or menu.get("restaurant"),
        }
        existing = next(
            (i for i in self.cart["items"] if i["item_id"] == item_id),
            None,
        )
        if existing:
            existing["quantity"] += qty
            if notes:
                existing["notes"] = notes
        else:
            self.cart["items"].append(
                {
                    "item_id": item_id,
                    "name": product["name"],
                    "quantity": qty,
                    "unit_price_cents": product["price_cents"],
                    "price_cents": product["price_cents"] * qty,
                    "price": _eur(product["price_cents"] * qty),
                    "notes": notes or "",
                }
            )
        self._reprice_cart()
        return {"ok": True, "added": {"item_id": item_id, "quantity": qty}, "cart": self.cart_snapshot()}

    def remove_from_cart(self, item_id: str) -> dict[str, Any]:
        item_id = (item_id or "").strip()
        before = len(self.cart["items"])
        self.cart["items"] = [i for i in self.cart["items"] if i.get("item_id") != item_id]
        if not self.cart["items"]:
            self.cart = _empty_cart()
        else:
            self._reprice_cart()
        return {
            "ok": True,
            "removed": item_id if before != len(self.cart["items"]) else None,
            "cart": self.cart_snapshot(),
        }

    def clear_cart(self) -> dict[str, Any]:
        self.cart = _empty_cart()
        return {"ok": True, "cart": self.cart_snapshot()}

    def place_order(self, *, delivery_line: str) -> dict[str, Any]:
        self._order_seq += 1
        order = {
            "id": f"mock-order-{self._order_seq:04d}",
            "status": "accepted",
            "restaurant": deepcopy(self.cart.get("restaurant")),
            "items": deepcopy(self.cart.get("items") or []),
            "total_cents": self.cart.get("total_cents") or 0,
            "total": self.cart.get("total") or "€0.00",
            "currency": "EUR",
            "delivery_address": delivery_line,
            "live": False,
        }
        self.orders.append(deepcopy(order))
        self.cart = _empty_cart()
        return order

    def _reprice_cart(self) -> None:
        total = 0
        for item in self.cart["items"]:
            item["price_cents"] = int(item["unit_price_cents"]) * int(item["quantity"])
            item["price"] = _eur(item["price_cents"])
            total += item["price_cents"]
        fee = 0
        rid = self.cart.get("restaurant_id")
        if rid:
            resto = next((r for r in self.restaurants if r["id"] == rid), None)
            fee = int((resto or {}).get("delivery_fee_cents") or 0)
        self.cart["delivery_fee_cents"] = fee
        self.cart["delivery_fee"] = _eur(fee)
        self.cart["subtotal_cents"] = total
        self.cart["subtotal"] = _eur(total)
        self.cart["total_cents"] = total + fee
        self.cart["total"] = _eur(total + fee)
        self.cart["currency"] = "EUR"


def _empty_cart() -> dict[str, Any]:
    return {
        "restaurant_id": None,
        "restaurant": None,
        "items": [],
        "subtotal_cents": 0,
        "subtotal": "€0.00",
        "delivery_fee_cents": 0,
        "delivery_fee": "€0.00",
        "total_cents": 0,
        "total": "€0.00",
        "currency": "EUR",
    }


def _eur(cents: int) -> str:
    return f"€{int(cents) / 100:.2f}"


mock_thuisbezorgd = MockThuisbezorgd()


class MockHouse:
    """Mutable in-memory house so mocked lights actually toggle in the UI."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = deepcopy(MOCK_HA_STATES)

    def list_states(self, domain: str | None = None) -> list[dict[str, Any]]:
        if not domain:
            return deepcopy(self.states)
        prefix = domain.rstrip(".") + "."
        return deepcopy([s for s in self.states if s["entity_id"].startswith(prefix)])

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        for state in self.states:
            if state["entity_id"] == entity_id:
                return deepcopy(state)
        return None

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = data or {}
        state = next((s for s in self.states if s["entity_id"] == entity_id), None)
        if state is None:
            return {"ok": False, "error": f"unknown entity {entity_id}"}
        if domain == "light":
            if service == "turn_on":
                state["state"] = "on"
                if "brightness" in data:
                    state["attributes"]["brightness"] = data["brightness"]
                elif not state["attributes"].get("brightness"):
                    state["attributes"]["brightness"] = 180
            elif service == "turn_off":
                state["state"] = "off"
                state["attributes"]["brightness"] = 0
            elif service == "toggle":
                state["state"] = "off" if state["state"] == "on" else "on"
        elif domain == "scene" and service == "turn_on":
            state["state"] = "on"
            if entity_id == "scene.movie_night":
                self._set_light("light.living_room", "on", 40)
                self._set_light("light.kitchen", "off", 0)
                self._set_light("light.office", "off", 0)
            elif entity_id == "scene.good_night":
                for light in ("light.living_room", "light.kitchen", "light.office"):
                    self._set_light(light, "off", 0)
        elif domain == "media_player":
            if service == "volume_set" and "volume_level" in data:
                state["attributes"]["volume_level"] = float(data["volume_level"])
            elif service == "volume_mute":
                state["attributes"]["is_volume_muted"] = bool(data.get("is_volume_muted", True))
            elif service == "volume_up":
                current = float(state["attributes"].get("volume_level") or 0)
                state["attributes"]["volume_level"] = min(1.0, round(current + 0.05, 2))
            elif service == "volume_down":
                current = float(state["attributes"].get("volume_level") or 0)
                state["attributes"]["volume_level"] = max(0.0, round(current - 0.05, 2))
            elif service == "toggle":
                state["state"] = "off" if state["state"] not in {"off", "unavailable"} else "on"
            elif service in {"turn_on", "media_play"}:
                if service == "turn_on":
                    state["state"] = "on"
                else:
                    state["state"] = "playing"
            elif service in {"turn_off", "media_stop"}:
                state["state"] = "off" if service == "turn_off" else "idle"
            elif service == "media_pause":
                state["state"] = "paused"
            elif service in {"media_next_track", "media_previous_track"}:
                state["state"] = "playing"
            elif service == "select_source" and "source" in data:
                state["attributes"]["source"] = data["source"]
            elif service == "play_media":
                state["state"] = "playing"
                if "media_content_id" in data:
                    state["attributes"]["media_content_id"] = data["media_content_id"]
                if "media_content_type" in data:
                    state["attributes"]["media_content_type"] = data["media_content_type"]
                # Infuse / app deep links often surface as app_name on Apple TV.
                content = str(data.get("media_content_id") or "")
                if content.startswith("infuse://"):
                    state["attributes"]["app_name"] = "Infuse"
                    state["attributes"]["media_title"] = content.split("?")[0]
        return {"ok": True, "entity": deepcopy(state)}

    def _set_light(self, entity_id: str, on_off: str, brightness: int) -> None:
        state = next((s for s in self.states if s["entity_id"] == entity_id), None)
        if state is None:
            return
        state["state"] = on_off
        state["attributes"]["brightness"] = brightness
