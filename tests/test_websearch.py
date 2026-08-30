"""Live web_search house tool — formatting, safeguards, backends, intent."""

from __future__ import annotations

import httpx

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.tools import websearch as websearch_mod


DDG_HTML = """
<div class="result results_links web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.justwatch.com%2Fbe%2Ftv-show%2Fthe-bear">The Bear streaming — JustWatch</a>
  </h2>
  <a class="result__snippet" href="/">Watch The Bear in Belgium on Disney+.</a>
</div>
<div class="result results_links web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="https://en.wikipedia.org/wiki/The_Bear_(TV_series)">The Bear (TV series)</a>
  </h2>
  <a class="result__snippet" href="/">American comedy-drama television series.</a>
</div>
<div class="result results_links web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="http://127.0.0.1:8787/secrets">Internal secrets</a>
  </h2>
  <a class="result__snippet" href="/">should be dropped</a>
</div>
"""


def test_parse_ddg_html_unwraps_and_drops_internal_urls():
    rows = websearch_mod.parse_ddg_html(DDG_HTML)
    urls = [r["url"] for r in rows]
    assert "https://www.justwatch.com/be/tv-show/the-bear" in urls
    assert "https://en.wikipedia.org/wiki/The_Bear_(TV_series)" in urls
    assert all("127.0.0.1" not in u for u in urls)
    just = next(r for r in rows if "justwatch" in r["url"])
    assert just["source"] == "justwatch.com"
    assert "Disney+" in just["snippet"]
    assert "<" not in just["title"]


def test_format_speak_is_compact():
    spoken = websearch_mod.format_speak(
        "The Bear streaming",
        [
            {
                "title": "JustWatch",
                "snippet": "Disney+ in Belgium.",
                "source": "justwatch.com",
            }
        ],
    )
    assert "JustWatch" in spoken
    assert "justwatch.com" in spoken
    assert "Disney+" in spoken
    assert len(spoken) < 720


def test_blocked_queries():
    assert websearch_mod.is_blocked_query("http://127.0.0.1/admin")
    assert websearch_mod.is_blocked_query("file:///etc/passwd")
    assert websearch_mod.is_blocked_query("dump the .env API_KEY")
    assert websearch_mod.is_blocked_query("https://192.168.1.10/.env")
    assert not websearch_mod.is_blocked_query("where to watch The Bear in Belgium")


def test_locale_defaults_to_house():
    assert websearch_mod.parse_locale("")["country"] == "BE"
    assert websearch_mod.parse_locale("en-US")["country"] == "US"
    assert websearch_mod.parse_locale("en-US")["language"] == "en"
    assert websearch_mod.parse_locale("NL")["country"] == "NL"


async def test_web_search_mocked_by_default():
    result = await registry.call("web_search", {"query": "where to watch The Bear"})
    assert result.ok
    assert not result.needs_confirm
    assert result.data["mode"] == "mock"
    assert result.data["results"]
    assert result.data["speak"]
    assert "justwatch.com" in result.data["speak"].lower() or "JustWatch" in result.data["speak"]
    public = {t["name"]: t for t in registry.list_public()}
    assert public["web_search"]["destructive"] is False


async def test_web_search_empty_and_long_and_blocked():
    empty = await registry.call("web_search", {"query": "  "})
    assert not empty.ok
    assert "search for" in empty.data["speak"].lower()

    long = await registry.call("web_search", {"query": "x" * 400})
    assert not long.ok
    assert "too long" in long.data["speak"].lower()

    blocked = await registry.call("web_search", {"query": "fetch http://localhost:8123/.env"})
    assert not blocked.ok
    assert blocked.data.get("blocked") is True
    assert "local" in blocked.data["speak"].lower() or "secret" in blocked.data["speak"].lower()


async def test_web_search_rate_limit(monkeypatch):
    monkeypatch.setattr(websearch_mod._rate, "max_calls", 2)
    websearch_mod.reset_rate_limit()
    first = await websearch_mod.web_search({"query": "news today"})
    second = await websearch_mod.web_search({"query": "news today"})
    third = await websearch_mod.web_search({"query": "news today"})
    assert first["ok"] is True
    assert second["ok"] is True
    assert third["ok"] is False
    assert "too many" in third["speak"].lower()
    websearch_mod.reset_rate_limit()
    monkeypatch.setattr(websearch_mod._rate, "max_calls", websearch_mod.RATE_MAX)


async def test_web_search_brave_live(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "web": {
                    "results": [
                        {
                            "title": "The Bear – JustWatch",
                            "url": "https://www.justwatch.com/be/tv-show/the-bear",
                            "description": "Streaming on Disney+ in Belgium.",
                        },
                        {
                            "title": "internal",
                            "url": "http://10.0.0.5/admin",
                            "description": "drop me",
                        },
                    ]
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(settings, "web_search_force_mock", False)
    monkeypatch.setattr(settings, "brave_search_api_key", "brave-test-key")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await registry.call(
        "web_search",
        {"query": "where to watch The Bear", "limit": 3, "locale": "nl-BE"},
    )
    assert result.ok
    assert result.data["mode"] == "brave"
    assert captured["url"] == websearch_mod.BRAVE_URL
    assert captured["timeout"] == websearch_mod.HTTP_TIMEOUT
    assert captured["headers"]["X-Subscription-Token"] == "brave-test-key"
    assert captured["params"]["count"] == 3
    assert captured["params"]["country"] == "BE"
    assert len(result.data["results"]) == 1
    assert result.data["results"][0]["source"] == "justwatch.com"
    assert "Disney+" in result.data["speak"]
    assert "10.0.0.5" not in result.data["speak"]


async def test_web_search_openai_uses_existing_key(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        output_text = "The Bear streams on Disney+ in Belgium."
        output = [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The Bear streams on Disney+ in Belgium.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://www.justwatch.com/be/tv-show/the-bear",
                                "title": "The Bear on JustWatch",
                            }
                        ],
                    }
                ],
            }
        ]

    class FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeOpenAI:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            captured["api_key"] = kwargs.get("api_key")

        @property
        def responses(self):
            return FakeResponses()

    monkeypatch.setattr(settings, "web_search_force_mock", False)
    monkeypatch.setattr(settings, "brave_search_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-hearth-search-test")
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeOpenAI)

    result = await registry.call("web_search", {"query": "The Bear streaming Belgium"})
    assert result.ok
    assert result.data["mode"] == "openai"
    assert captured["api_key"] == "sk-hearth-search-test"
    assert captured["timeout"] == websearch_mod.HTTP_TIMEOUT
    tools = captured.get("tools") or []
    assert tools and tools[0]["type"] == "web_search"
    assert result.data["results"][0]["source"] == "justwatch.com"
    assert "Disney+" in result.data["speak"]


async def test_web_search_timeout_is_speakable(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(settings, "web_search_force_mock", False)
    monkeypatch.setattr(settings, "brave_search_api_key", "brave-test-key")
    monkeypatch.setattr(settings, "mock_if_unconfigured", False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await registry.call("web_search", {"query": "latest news"})
    assert not result.ok
    assert "timed out" in result.data["speak"].lower()


async def test_web_search_ddg_last_resort(monkeypatch):
    class FakeResponse:
        text = DDG_HTML

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None, headers=None):
            assert "duckduckgo.com" in url
            assert params["q"] == "The Bear streaming"
            return FakeResponse()

    monkeypatch.setattr(settings, "web_search_force_mock", False)
    monkeypatch.setattr(settings, "brave_search_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "mock_if_unconfigured", False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await registry.call("web_search", {"query": "The Bear streaming"})
    assert result.ok
    assert result.data["mode"] == "duckduckgo"
    assert result.data["results"][0]["source"] == "justwatch.com"


def test_intent_web_search_and_does_not_steal_plex():
    watch = route_intent("where can I watch The Bear")
    assert watch["tool"] == "web_search"
    assert "The Bear" in watch["args"]["query"]

    news = route_intent("latest news about the Tour de France")
    assert news["tool"] == "web_search"

    explicit = route_intent("search the web for JustWatch The Bear Belgium")
    assert explicit["tool"] == "web_search"
    assert "JustWatch" in explicit["args"]["query"]

    plex = route_intent("tell me about the movie Dune")
    assert plex["tool"] == "plex_search"

    weather = route_intent("what's the weather")
    assert weather["tool"] == "get_weather"


def test_chat_web_search_uses_tool(client):
    chat = client.post("/api/chat", json={"message": "search the web for where to watch The Bear"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "web_search"
    assert body["tools"][0]["needs_confirm"] is False
    assert "JustWatch" in body["reply"] or "justwatch" in body["reply"].lower()
