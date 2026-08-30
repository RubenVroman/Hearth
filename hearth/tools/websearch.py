"""Live web search for the house agent — compact, speakable results.

Backends (first match):
1. ``BRAVE_SEARCH_API_KEY`` — Brave Search API (structured title/snippet/url).
2. ``OPENAI_API_KEY`` — Responses API hosted ``web_search`` (reuses the house key).
3. Fixtures when ``HEARTH_MOCK_IF_UNCONFIGURED`` / ``HEARTH_WEB_SEARCH_MOCK``.
4. DuckDuckGo HTML lite as a no-key last resort.

Never fetches result pages, never executes result content, never sends keys to the browser.
"""

from __future__ import annotations

import html
import ipaddress
import re
import time
from collections import deque
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from hearth.config import settings
from hearth.fixtures import MOCK_WEB_SEARCH_RESULTS

MAX_QUERY_LEN = 240
MIN_QUERY_LEN = 2
DEFAULT_LIMIT = 4
MAX_LIMIT = 5
SNIPPET_LEN = 180
SPEAK_LEN = 720
HTTP_TIMEOUT = 10.0
RATE_MAX = 8
RATE_WINDOW_S = 60.0

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Hearth/0.1 (house agent; +https://github.com/RubenVroman/Hearth)"

NOT_CONFIGURED = (
    "Web search is not configured. Set OPENAI_API_KEY (uses OpenAI web search) "
    "or BRAVE_SEARCH_API_KEY in the host .env. I cannot search the live web until then."
)
BLOCKED_SPEAK = "I can't search for local addresses or secrets."
EMPTY_SPEAK = "I need something to search for."
LONG_SPEAK = "That search is too long. Ask me in a shorter phrase."
RATE_SPEAK = "Too many searches just now. Try again in a moment."
TIMEOUT_SPEAK = "The web search timed out. Try again in a moment."
EMPTY_RESULTS_SPEAK = "I didn't find live web results for that."
FAILED_SPEAK = "Web search failed. Try again in a moment."

_SECRET_HINT = re.compile(
    r"("
    r"\b(api[_-]?key|secret[_-]?key|password|passwd|private[_-]?key|"
    r"authorized_keys|id_rsa|\.env|htpasswd)\b|"
    r"file://|"
    r"javascript:|"
    r"https?://("
    r"localhost\b|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1\b|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"169\.254\.\d{1,3}\.\d{1,3}"
    r")"
    r")",
    re.I,
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class _RateLimit:
    def __init__(self, max_calls: int = RATE_MAX, window_s: float = RATE_WINDOW_S) -> None:
        self.max_calls = max_calls
        self.window_s = window_s
        self._hits: deque[float] = deque()

    def reset(self) -> None:
        self._hits.clear()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > self.window_s:
            self._hits.popleft()
        if len(self._hits) >= self.max_calls:
            return False
        self._hits.append(now)
        return True


_rate = _RateLimit()


def reset_rate_limit() -> None:
    _rate.reset()


def selected_backend() -> str:
    if settings.web_search_force_mock:
        return "mock"
    if settings.brave_search_configured:
        return "brave"
    if settings.openai_configured:
        return "openai"
    if settings.mock_if_unconfigured:
        return "mock"
    return "duckduckgo"


def not_configured_message() -> str:
    return NOT_CONFIGURED


def sanitize_query(raw: Any) -> str:
    text = _WS.sub(" ", str(raw or "")).strip()
    return text


def is_blocked_query(query: str) -> bool:
    if not query:
        return False
    if _SECRET_HINT.search(query):
        return True
    # Bare internal hosts without a scheme.
    lowered = query.lower()
    if lowered.startswith(("localhost", "127.0.0.1", "0.0.0.0", "::1")):
        return True
    return False


def _strip_html(text: str) -> str:
    cleaned = html.unescape(_TAG.sub(" ", text or ""))
    return _WS.sub(" ", cleaned).strip()


def _clip(text: str, limit: int) -> str:
    text = _WS.sub(" ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def unwrap_ddg_url(url: str) -> str:
    raw = (url or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if "duckduckgo.com" in host and "/l/" in (parsed.path or ""):
        qs = parse_qs(parsed.query)
        target = (qs.get("uddg") or [""])[0]
        if target:
            return unquote(target)
    return raw


def is_blocked_url(url: str) -> bool:
    parsed = urlparse(url or "")
    scheme = (parsed.scheme or "").lower()
    if scheme in {"file", "javascript", "data", "vbscript"}:
        return True
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "ip6-localhost"}:
        return True
    if host.endswith((".local", ".internal", ".lan", ".home")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)


def _pack_result(title: str, url: str, snippet: str) -> dict[str, str] | None:
    url = unwrap_ddg_url(url)
    if not url or is_blocked_url(url):
        return None
    title = _clip(_strip_html(title), 120)
    snippet = _clip(_strip_html(snippet), SNIPPET_LEN)
    source = domain_of(url) or "web"
    if not title:
        title = source
    return {"title": title, "url": url, "snippet": snippet, "source": source}


def format_speak(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        q = _clip(query, 80) if query else "that"
        return f"I didn't find live web results for {q}."
    bits: list[str] = []
    for row in results[:MAX_LIMIT]:
        title = str(row.get("title") or "Untitled")
        source = str(row.get("source") or "")
        snippet = str(row.get("snippet") or "")
        if snippet and source:
            bits.append(f"{title} ({source}): {snippet}")
        elif snippet:
            bits.append(f"{title}: {snippet}")
        elif source:
            bits.append(f"{title} ({source})")
        else:
            bits.append(title)
    if len(bits) == 1:
        spoken = bits[0]
    else:
        numbered = " ".join(f"{i}. {bit}" for i, bit in enumerate(bits, 1))
        q = _clip(query, 60)
        spoken = f"Top results for {q}: {numbered}" if q else numbered
    return _clip(spoken, SPEAK_LEN)


def parse_locale(raw: Any) -> dict[str, str]:
    """Map a short locale (nl-BE, en-US, BE) to country / language / city."""
    text = str(raw or "").strip().replace("_", "-")
    country = "BE"
    language = "nl"
    city = "Ghent"
    if not text:
        return {"country": country, "language": language, "city": city}
    parts = [p for p in text.split("-") if p]
    if len(parts) >= 2:
        language = parts[0].lower()[:8] or language
        country = parts[1].upper()[:2] or country
    elif len(parts[0]) == 2 and parts[0].isalpha():
        token = parts[0]
        if token.isupper() or token.lower() in {"be", "nl", "us", "gb", "de", "fr"}:
            country = token.upper()
            language = {"BE": "nl", "NL": "nl", "US": "en", "GB": "en", "DE": "de", "FR": "fr"}.get(
                country, token.lower()
            )
        else:
            language = token.lower()
            country = {"nl": "BE", "en": "US", "de": "DE", "fr": "FR"}.get(language, country)
    if country != "BE":
        city = ""
    return {"country": country, "language": language, "city": city}


def parse_ddg_html(body: str) -> list[dict[str, str]]:
    """Pull title/url/snippet from DuckDuckGo HTML or lite markup. No page fetch."""
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(title: str, url: str, snippet: str) -> None:
        packed = _pack_result(title, url, snippet)
        if packed is None:
            return
        key = packed["url"]
        if key in seen:
            return
        seen.add(key)
        results.append(packed)

    html_pat = re.compile(
        r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'(?P<rest>.*?)(?=class="result__a"|$)',
        re.I | re.S,
    )
    snippet_pat = re.compile(
        r'class="result__snippet"[^>]*>(?P<snippet>.*?)</(?:a|td|div|span)',
        re.I | re.S,
    )
    for match in html_pat.finditer(body or ""):
        rest = match.group("rest") or ""
        snip_m = snippet_pat.search(rest)
        _add(match.group("title"), match.group("href"), snip_m.group("snippet") if snip_m else "")
        if len(results) >= MAX_LIMIT:
            return results

    lite_pat = re.compile(
        r"class=['\"]result-link['\"][^>]*href=['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<title>.*?)</a>"
        r".*?class=['\"]result-snippet['\"][^>]*>(?P<snippet>.*?)</",
        re.I | re.S,
    )
    for match in lite_pat.finditer(body or ""):
        _add(match.group("title"), match.group("href"), match.group("snippet"))
        if len(results) >= MAX_LIMIT:
            break
    return results


def _limit_from_args(args: dict[str, Any]) -> int:
    raw = args.get("limit") if args.get("limit") is not None else args.get("count")
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _fail(speak: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": speak, "speak": speak}
    payload.update(extra)
    return payload


def _ok(query: str, results: list[dict[str, Any]], *, mode: str, **extra: Any) -> dict[str, Any]:
    clipped = results[:MAX_LIMIT]
    payload: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "query": query,
        "results": clipped,
        "speak": format_speak(query, clipped),
    }
    payload.update(extra)
    return payload


def _mock_payload(query: str, limit: int) -> dict[str, Any]:
    results = [dict(row) for row in MOCK_WEB_SEARCH_RESULTS[:limit]]
    return _ok(query, results, mode="mock")


async def web_search(args: dict[str, Any]) -> dict[str, Any]:
    """Run a live (or mocked) web search. Always returns a speakable payload."""
    query = sanitize_query(args.get("query") or args.get("q") or "")
    if not query:
        return _fail(EMPTY_SPEAK)
    if len(query) > MAX_QUERY_LEN:
        return _fail(LONG_SPEAK)
    if is_blocked_query(query):
        return _fail(BLOCKED_SPEAK, blocked=True)
    if not _rate.allow():
        return _fail(RATE_SPEAK)

    limit = _limit_from_args(args)
    locale = parse_locale(args.get("locale"))
    backend = selected_backend()

    if backend == "mock":
        return _mock_payload(query, limit)

    try:
        if backend == "brave":
            return await _search_brave(query, limit=limit, locale=locale)
        if backend == "openai":
            return await _search_openai(query, limit=limit, locale=locale)
        return await _search_duckduckgo(query, limit=limit)
    except httpx.TimeoutException:
        if settings.mock_if_unconfigured:
            payload = _mock_payload(query, limit)
            payload["fallback_error"] = "timeout"
            return payload
        return _fail(TIMEOUT_SPEAK, query=query)
    except Exception as exc:  # noqa: BLE001 — speakable error, never crash the app
        if settings.mock_if_unconfigured:
            payload = _mock_payload(query, limit)
            payload["fallback_error"] = str(exc)[:200]
            return payload
        return _fail(f"{FAILED_SPEAK} {str(exc)[:120]}".strip(), query=query)


async def _search_brave(query: str, *, limit: int, locale: dict[str, str]) -> dict[str, Any]:
    key = settings.brave_search_api_key.strip()
    if not key:
        return _fail(NOT_CONFIGURED)
    params: dict[str, Any] = {
        "q": query,
        "count": limit,
        "country": locale["country"],
        "search_lang": locale["language"],
        "text_decorations": 0,
    }
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": key,
        "User-Agent": USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(BRAVE_URL, params=params, headers=headers)
        response.raise_for_status()
        raw = response.json()
    results: list[dict[str, str]] = []
    web = raw.get("web") if isinstance(raw, dict) else None
    rows = (web or {}).get("results") if isinstance(web, dict) else None
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        packed = _pack_result(
            str(item.get("title") or ""),
            str(item.get("url") or ""),
            str(item.get("description") or item.get("snippet") or ""),
        )
        if packed is None:
            continue
        results.append(packed)
        if len(results) >= limit:
            break
    if not results:
        return _fail(EMPTY_RESULTS_SPEAK, query=query, mode="brave")
    return _ok(query, results, mode="brave")


async def _search_openai(query: str, *, limit: int, locale: dict[str, str]) -> dict[str, Any]:
    from openai import AsyncOpenAI

    tool: dict[str, Any] = {
        "type": "web_search",
        "search_context_size": "low",
    }
    location: dict[str, Any] = {"type": "approximate", "country": locale["country"]}
    if locale.get("city"):
        location["city"] = locale["city"]
    tool["user_location"] = location

    prompt = (
        f"Search the live web for: {query}\n"
        f"Return at most {limit} short bullets. Each bullet: title — one sentence "
        "snippet — source domain. No HTML, no long quotes. Prefer current, reliable sources. "
        "For where-to-watch / streaming, name the services and region if known."
    )
    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=HTTP_TIMEOUT)
    try:
        try:
            response = await client.responses.create(
                model=settings.openai_model,
                tools=[tool],
                tool_choice="required",
                include=["web_search_call.action.sources"],
                input=prompt,
            )
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower():
                raise
            # Older SDK / model may reject include or tool_choice shape — retry simply.
            response = await client.responses.create(
                model=settings.openai_model,
                tools=[{"type": "web_search", "search_context_size": "low"}],
                input=prompt,
            )
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower():
            raise
        # Hosted search unavailable — last-resort HTML search, not a crash.
        return await _search_duckduckgo(query, limit=limit)

    results = _openai_results(response, limit=limit)
    summary = _clip(str(getattr(response, "output_text", "") or ""), SPEAK_LEN)
    if not results and not summary:
        # Hosted search unavailable on this model — last-resort HTML search.
        return await _search_duckduckgo(query, limit=limit)
    if not results and summary:
        results = [{"title": "Web", "url": "", "snippet": summary, "source": "openai"}]
    payload = _ok(query, results, mode="openai")
    if summary and summary not in payload["speak"]:
        payload["speak"] = _clip(summary, SPEAK_LEN)
        payload["summary"] = summary
    return payload


def _openai_results(response: Any, *, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(title: str, url: str, snippet: str) -> None:
        packed = _pack_result(title, url, snippet) if url else None
        if packed is None and url:
            return
        if packed is None:
            return
        if packed["url"] in seen:
            return
        seen.add(packed["url"])
        results.append(packed)

    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    for item in output or []:
        data = item if isinstance(item, dict) else _maybe_dump(item)
        if not isinstance(data, dict):
            continue
        action = data.get("action") if isinstance(data.get("action"), dict) else {}
        for source in action.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "")
            _add(str(source.get("title") or domain_of(url) or "Source"), url, "")
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                block = _maybe_dump(block) or {}
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or "")
            for ann in block.get("annotations") or []:
                if not isinstance(ann, dict):
                    ann = _maybe_dump(ann) or {}
                if not isinstance(ann, dict):
                    continue
                if str(ann.get("type") or "") not in {"url_citation", "citation"}:
                    continue
                url = str(ann.get("url") or "")
                title = str(ann.get("title") or domain_of(url) or "Source")
                start = ann.get("start_index")
                end = ann.get("end_index")
                snippet = ""
                if isinstance(start, int) and isinstance(end, int) and text:
                    snippet = text[start:end]
                _add(title, url, snippet or text[:SNIPPET_LEN])
        if len(results) >= limit:
            break
    return results[:limit]


def _maybe_dump(obj: Any) -> dict[str, Any] | None:
    if hasattr(obj, "model_dump"):
        try:
            dumped = obj.model_dump()
            return dumped if isinstance(dumped, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


async def _search_duckduckgo(query: str, *, limit: int) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(DDG_HTML_URL, params={"q": query}, headers=headers)
        response.raise_for_status()
        body = response.text or ""
    # Cap HTML so a huge dump never reaches the model / voice path.
    results = parse_ddg_html(body[:80_000])[:limit]
    if not results:
        return _fail(EMPTY_RESULTS_SPEAK, query=query, mode="duckduckgo")
    return _ok(query, results, mode="duckduckgo")
