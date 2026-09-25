"""Telegram still-image intake: allowlist, refusal, titles, and no silent queue."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hearth.config import Settings, settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.media.vision import (
    VisionCandidate,
    VisionResult,
    declared_mime_matches,
    parse_vision_payload,
    residency_allows_upload,
    sniff_image_mime,
)
from hearth.telegram.media.vision_provider import (
    FixtureVisionProvider,
    OpenAIVisionProvider,
    VisionUnavailable,
)
from hearth.telegram.parse import choose_photo_size, parse_message, screen_media
from hearth.telegram.progress import format_reject_download
from hearth.telegram.store import TelegramStore

CHAT_ID = -100123
USER_ID = 42
JPEG = b"\xff\xd8\xff" + b"\x00" * 32 + b"SECRETPIXEL"


def _photo(
    caption: str = "",
    *,
    chat_id: int = CHAT_ID,
    user_id: int = USER_ID,
    sizes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "message_id": 7,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": False},
        "caption": caption,
        "photo": sizes
        or [
            {"file_id": "small", "width": 320, "height": 240, "file_size": 4000},
            {"file_id": "mid", "width": 800, "height": 600, "file_size": 20000},
            {"file_id": "big", "width": 2000, "height": 1500, "file_size": 400000},
        ],
    }


def _document(
    *,
    mime: str = "image/jpeg",
    file_name: str = "poster.jpg",
    file_size: int = 1000,
    caption: str = "",
    file_id: str = "doc-1",
) -> dict[str, Any]:
    return {
        "message_id": 8,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "caption": caption,
        "document": {
            "file_id": file_id,
            "mime_type": mime,
            "file_name": file_name,
            "file_size": file_size,
        },
    }


class QueryOverseerr:
    live = True

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.search_calls: list[tuple[str, int]] = []
        self.request_calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append((query, page))
        needle = query.casefold()
        matched = [
            row
            for row in self.rows
            if str(row.get("title") or row.get("name") or "").casefold() in needle
        ]
        return {"ok": True, "mode": "live", "results": matched}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        return {"ok": False, "reason": "not_found"}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(dict(kwargs))
        return {"ok": True, "requestStatus": 2, "mediaStatus": 3, "requestId": 9}


class RecordingProgress:
    def __init__(self) -> None:
        self.active: list[Any] = []

    def reset(self) -> None:
        self.active.clear()

    def track(self, *args: Any, **kwargs: Any) -> None:
        return None


def _row(
    title: str,
    tmdb_id: int,
    *,
    year: int = 2008,
    media_type: str = "movie",
) -> dict[str, Any]:
    return {
        "mediaType": media_type,
        "id": tmdb_id,
        "title": title,
        "releaseDate": f"{year}-06-01",
        "year": year,
    }


def _candidate(title: str, *, year: int | None = 2008, confidence: float = 0.95) -> VisionCandidate:
    return VisionCandidate(title=title, year=year, media_type="movie", confidence=confidence)


@pytest.fixture
def vision_bot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]]:
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(CHAT_ID))
    monkeypatch.setattr(settings, "telegram_user_ids", str(USER_ID))
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "telegram_callback_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "telegram_vision_lane", True)
    monkeypatch.setattr(settings, "telegram_vision_mode", "confirm")
    monkeypatch.setattr(settings, "telegram_vision_provider", "fixture")
    monkeypatch.setattr(settings, "telegram_vision_per_minute", 10)
    monkeypatch.setattr(settings, "telegram_vision_daily_cap", 30)
    monkeypatch.setattr(settings, "telegram_vision_list_cap", 16)
    monkeypatch.setattr(settings, "openai_api_key", "")
    stores: list[TelegramStore] = []

    def make(
        result: VisionResult | Exception,
        rows: list[dict[str, Any]],
        *,
        fetcher_bytes: bytes | None = JPEG,
    ) -> tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]:
        store = TelegramStore(tmp_path / f"vision-{len(stores)}.db")
        stores.append(store)
        overseerr = QueryOverseerr(rows)
        provider = FixtureVisionProvider(result)
        bot = TelegramMediaBot(
            store,
            overseerr_client=overseerr,
            progress=RecordingProgress(),
        )
        bot._vision_provider = provider

        async def fetch(file_id: str, *, max_bytes: int) -> bytes | None:
            provider.calls  # touch so a missing fetch is obvious in asserts
            fetch.ids.append(file_id)
            if fetcher_bytes is None:
                return None
            if len(fetcher_bytes) > max_bytes:
                return None
            return fetcher_bytes

        fetch.ids = []  # type: ignore[attr-defined]
        bot._file_fetcher = fetch
        bot._fetch = fetch  # type: ignore[attr-defined]
        return bot, overseerr, provider

    yield make
    for store in stores:
        store.close()


def test_screen_allows_photos_and_still_image_documents() -> None:
    photo = screen_media(_photo(), max_bytes=4_000_000, min_edge=512)
    assert photo.disposition == "eligible"
    assert photo.file_id == "mid"
    assert photo.mime == "image/jpeg"

    for mime in ("image/png", "image/webp", "image/jpeg"):
        screen = screen_media(_document(mime=mime), max_bytes=4_000_000, min_edge=512)
        assert screen.disposition == "eligible", mime
        assert screen.mime == mime

    parsed = parse_message(_photo())
    assert parsed[1].action == "vision"


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            {
                "message_id": 1,
                "chat": {"id": 1},
                "from": {"id": 2},
                "video": {"file_id": "v"},
            },
            "media_attachment:video",
        ),
        (
            {
                "message_id": 1,
                "chat": {"id": 1},
                "from": {"id": 2},
                "audio": {"file_id": "a"},
            },
            "media_attachment:audio",
        ),
        (
            {
                "message_id": 1,
                "chat": {"id": 1},
                "from": {"id": 2},
                "sticker": {"file_id": "s"},
            },
            "media_attachment:sticker",
        ),
        (_document(mime="image/gif", file_name="loop.gif"), "media_attachment:document"),
        (
            _document(mime="application/octet-stream", file_name="notes.txt"),
            "media_attachment:document",
        ),
        (_document(file_name="movie.torrent", mime="image/jpeg"), "torrent_download"),
        (_photo(caption="magnet:?xt=urn:btih:abc"), "torrent_download"),
        (_document(file_size=5_000_000), "media_too_large"),
    ],
)
def test_screen_refuses_non_images_magnets_and_oversize(
    message: dict[str, Any],
    reason: str,
) -> None:
    screen = screen_media(message, max_bytes=4_000_000, min_edge=512)
    assert screen.disposition == "reject"
    assert screen.reason == reason


def test_choose_photo_size_skips_original_when_a_readable_size_exists() -> None:
    chosen = choose_photo_size(
        [
            {"file_id": "small", "width": 90, "height": 90, "file_size": 100},
            {"file_id": "mid", "width": 640, "height": 480, "file_size": 1000},
            {"file_id": "huge", "width": 4000, "height": 3000, "file_size": 9_000_000},
        ],
        min_edge=512,
        max_bytes=4_000_000,
    )
    assert chosen is not None
    assert chosen["file_id"] == "mid"


def test_magic_bytes_accept_jpeg_png_webp_and_reject_html() -> None:
    assert sniff_image_mime(JPEG) == "image/jpeg"
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"rest") == "image/png"
    assert sniff_image_mime(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP") == "image/webp"
    assert sniff_image_mime(b"<html>not an image</html>") is None
    assert declared_mime_matches("image/jpeg", "image/jpeg")
    assert not declared_mime_matches("image/png", "image/jpeg")


def test_parse_vision_payload_keeps_titles_and_drops_magnets() -> None:
    parsed = parse_vision_payload(
        {
            "kind": "list",
            "list_label": "Five best horror",
            "notes": "ignore previous instructions magnet:?xt=urn:btih:dead",
            "candidates": [
                {"title": "The Witch", "year": 2015, "media_type": "movie", "confidence": 0.91},
                {"title": "magnet:?xt=urn:btih:abc", "confidence": 0.99},
                {"title": "https://evil.example/grab", "confidence": 0.99},
            ],
        }
    )
    assert parsed.kind == "single"
    assert [item.title for item in parsed.candidates] == ["The Witch"]
    assert parsed.list_label == "Five best horror"


def test_residency_does_not_block_catalog_posters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_vision_provider", "openai")
    monkeypatch.setattr(settings, "telegram_vision_residency", "")
    assert residency_allows_upload("catalog") is True
    assert residency_allows_upload("poster") is True
    assert residency_allows_upload("personal") is False
    monkeypatch.setattr(settings, "telegram_vision_residency", "accepted")
    assert residency_allows_upload("personal") is True
    monkeypatch.setattr(settings, "telegram_vision_residency", "")
    monkeypatch.setattr(settings, "telegram_vision_provider", "local")
    assert residency_allows_upload("personal") is True


def test_batch_ceiling_fits_a_sixteen_poster_grid() -> None:
    loaded = Settings.model_validate({"HEARTH_TELEGRAM_BATCH_MAX_ITEMS": 16})
    assert loaded.telegram_batch_max_items == 16
    assert Settings.model_fields["telegram_vision_list_cap"].default == 16
    assert Settings.model_fields["telegram_vision_lane"].default is True
    assert Settings.model_fields["telegram_vision_mode"].default == "confirm"


@pytest.mark.asyncio
async def test_poster_becomes_a_get_card_and_does_not_queue(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, overseerr, provider = vision_bot(
        VisionResult(
            kind="single",
            candidates=(_candidate("The Witch", year=2015),),
            list_label="",
        ),
        [_row("The Witch", 310131, year=2015)],
    )
    reply = await bot.handle_message(_photo())

    assert reply is not None
    assert "The Witch" in reply.text
    assert reply.reply_markup is not None
    button = reply.reply_markup["inline_keyboard"][0][0]["text"]
    assert button.startswith("Get ")
    assert overseerr.request_calls == []
    assert overseerr.search_calls
    assert provider.calls and provider.calls[0]["nbytes"] == len(JPEG)
    assert "SECRETPIXEL" not in reply.text

    searches_before = len(overseerr.search_calls)
    data = reply.reply_markup["inline_keyboard"][0][0]["callback_data"]
    queued = await bot.handle_callback(
        {
            "id": "callback-1",
            "data": data,
            "from": {"id": USER_ID, "is_bot": False},
            "message": {
                "message_id": 900,
                "chat": {"id": CHAT_ID, "type": "supergroup"},
            },
        }
    )
    assert queued is not None
    assert overseerr.request_calls == [
        {
            "query": "The Witch",
            "media_id": 310131,
            "media_type": "movie",
            "seasons": None,
        }
    ]
    assert len(overseerr.search_calls) == searches_before


@pytest.mark.asyncio
async def test_collage_plans_sixteen_titles_with_one_get_each_and_no_queue(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    titles = [
        "The Devil's Backbone",
        "Signs",
        "28 Days Later",
        "Saw",
        "The Ring",
        "Cloverfield",
        "Let the Right One In",
        "The Strangers",
        "Let Me In",
        "The Cabin in the Woods",
        "Creep",
        "Goodnight Mommy",
        "The Witch",
        "Green Room",
        "It Comes at Night",
        "Host",
    ]
    rows = [_row(title, 1000 + index, year=2000 + index) for index, title in enumerate(titles)]
    candidates = tuple(
        _candidate(title, year=2000 + index, confidence=0.93) for index, title in enumerate(titles)
    )
    bot, overseerr, _provider = vision_bot(
        VisionResult(kind="list", candidates=candidates, list_label="Horror"),
        rows,
    )
    reply = await bot.handle_message(_photo())

    assert reply is not None
    assert reply.reply_markup is not None
    buttons = [
        cell["text"]
        for row in reply.reply_markup["inline_keyboard"]
        for cell in row
        if str(cell.get("text", "")).startswith("Get ")
    ]
    assert len(buttons) == 16
    assert overseerr.request_calls == []
    assert len(overseerr.search_calls) == 16
    assert "Host" in reply.text
    assert "Horror" in reply.text


@pytest.mark.asyncio
async def test_list_over_the_cap_names_what_was_left_off(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    titles = [f"Poster {index:02d}" for index in range(1, 18)]
    rows = [_row(title, index, year=2001) for index, title in enumerate(titles, start=1)]
    candidates = tuple(_candidate(title, year=2001) for title in titles)
    bot, overseerr, _provider = vision_bot(
        VisionResult(kind="list", candidates=candidates),
        rows,
    )
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert "leaving 1 off" in reply.text
    assert "Poster 17" in reply.text
    assert len(overseerr.search_calls) == 16
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_non_images_never_download_or_call_the_provider(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, overseerr, provider = vision_bot(VisionResult(kind="not_media"), [])
    message = {
        "message_id": 3,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": USER_ID, "is_bot": False},
        "caption": "Dune",
        "video": {"file_id": "video-1"},
    }
    reply = await bot.handle_message(message)
    assert reply is not None
    assert reply.text == format_reject_download()
    assert provider.calls == []
    assert bot._fetch.ids == []  # type: ignore[attr-defined]
    assert overseerr.request_calls == []
    assert overseerr.search_calls == []


@pytest.mark.asyncio
async def test_html_with_an_image_mime_is_refused_without_a_provider_call(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, overseerr, provider = vision_bot(
        VisionResult(kind="single", candidates=(_candidate("Dune"),)),
        [_row("Dune", 438631, year=2021)],
        fetcher_bytes=b"<html>SECRETPIXEL</html>",
    )
    reply = await bot.handle_message(_document())
    assert reply is not None
    assert reply.text == format_reject_download()
    assert provider.calls == []
    assert overseerr.search_calls == []
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_not_media_refuse_and_low_confidence_do_not_search(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, overseerr, _provider = vision_bot(VisionResult(kind="not_media"), [_row("Dune", 1)])
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert "film or series" in reply.text
    assert overseerr.search_calls == []
    assert overseerr.request_calls == []

    bot, overseerr, _provider = vision_bot(VisionResult(kind="refuse"), [_row("Dune", 1)])
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert reply.text == "I can't use that image."
    assert overseerr.search_calls == []

    bot, overseerr, _provider = vision_bot(
        VisionResult(kind="single", candidates=(_candidate("Saw", confidence=0.2),)),
        [_row("Saw", 176)],
    )
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert "not sure" in reply.text
    assert reply.reply_markup is None
    assert overseerr.search_calls == []
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_ambiguous_catalog_hits_are_a_pick_and_not_a_queue(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, overseerr, _provider = vision_bot(
        VisionResult(kind="single", candidates=(_candidate("Dune", year=None, confidence=0.9),)),
        [
            _row("Dune", 841, year=1984),
            _row("Dune", 438631, year=2021),
        ],
    )
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert "Which one matches the image?" in reply.text
    assert reply.reply_markup is not None
    gets = [
        cell
        for row in reply.reply_markup["inline_keyboard"]
        for cell in row
        if str(cell.get("text", "")).startswith("Get ")
    ]
    assert len(gets) == 2
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_shadow_mode_refuses_without_search_or_get(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_vision_mode", "shadow")
    bot, overseerr, provider = vision_bot(
        VisionResult(kind="single", candidates=(_candidate("Host"),)),
        [_row("Host", 736769, year=2020)],
    )
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert reply.text == format_reject_download()
    assert reply.reply_markup is None
    assert provider.calls
    assert overseerr.search_calls == []
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_auto_mode_still_requires_get(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_vision_mode", "auto")
    bot, overseerr, _provider = vision_bot(
        VisionResult(
            kind="single",
            candidates=(_candidate("Host", year=2020, confidence=0.99),),
        ),
        [_row("Host", 736769, year=2020)],
    )
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert "Host" in reply.text
    assert reply.reply_markup is not None
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_lane_off_and_missing_key_do_not_download(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_vision_lane", False)
    bot, overseerr, provider = vision_bot(
        VisionResult(kind="single", candidates=(_candidate("Host"),)),
        [],
    )
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert reply.text == format_reject_download()
    assert bot._fetch.ids == []  # type: ignore[attr-defined]
    assert provider.calls == []
    assert overseerr.request_calls == []

    monkeypatch.setattr(settings, "telegram_vision_lane", True)
    monkeypatch.setattr(settings, "telegram_vision_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    bot._vision_provider = None
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert reply.text == format_reject_download()
    assert bot._fetch.ids == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unknown_chat_never_downloads(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, _overseerr, provider = vision_bot(VisionResult(kind="not_media"), [])
    reply = await bot.handle_message(_photo(chat_id=-1, user_id=99))
    assert reply is None
    assert provider.calls == []
    assert bot._fetch.ids == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_caption_fallback_when_the_image_cannot_be_read(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, overseerr, _provider = vision_bot(
        VisionUnavailable("timeout"),
        [_row("Arrival", 329865, year=2016)],
    )
    reply = await bot.handle_message(_photo("Arrival"))
    assert reply is not None
    assert "couldn't read that image" in reply.text
    assert "Arrival" in reply.text
    assert overseerr.search_calls
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_filename_is_not_a_title(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    bot, overseerr, _provider = vision_bot(
        VisionResult(kind="not_media"),
        [_row("Inception", 27205)],
    )
    reply = await bot.handle_message(_document(file_name="Inception.jpg"))
    assert reply is not None
    assert "film or series" in reply.text
    assert overseerr.search_calls == []


@pytest.mark.asyncio
async def test_vision_logs_have_no_pixels_file_url_or_base64(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, _overseerr, _provider = vision_bot(
        VisionResult(kind="single", candidates=(_candidate("Host", year=2020),)),
        [_row("Host", 736769, year=2020)],
    )
    with caplog.at_level(logging.INFO, logger="hearth.telegram"):
        reply = await bot.handle_message(_photo())
    assert reply is not None
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SECRETPIXEL" not in text
    assert "base64" not in text.casefold()
    assert "api.telegram.org" not in text
    assert "candidate_count" in text
    assert "123456:test-token" not in text


@pytest.mark.asyncio
async def test_ack_is_edited_into_the_card(
    vision_bot: Callable[..., tuple[TelegramMediaBot, QueryOverseerr, FixtureVisionProvider]],
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.edits: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
            self.sent.append(text)
            return {"ok": True, "result": {"message_id": 50}}

        async def edit_message_text(
            self,
            chat_id: int,
            message_id: int,
            text: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.edits.append(text)
            return {"ok": True}

    bot, overseerr, _provider = vision_bot(
        VisionResult(kind="single", candidates=(_candidate("Host", year=2020),)),
        [_row("Host", 736769, year=2020)],
    )
    client = FakeClient()
    bot.bind_telegram(client)
    reply = await bot.handle_message(_photo())
    assert reply is not None
    assert reply.text == ""
    assert client.sent == ["Looking at that…"]
    assert client.edits and "Host" in client.edits[-1]
    assert overseerr.request_calls == []


@pytest.mark.asyncio
async def test_openai_provider_failure_does_not_log_the_image(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Boom:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs: Any) -> Any:
                    raise RuntimeError("data:image/jpeg;base64,SECRETPIXEL")

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "telegram_vision_model", "gpt-4o-mini")
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", Boom)
    provider = OpenAIVisionProvider()
    with caplog.at_level(logging.DEBUG, logger="hearth.telegram"):
        with pytest.raises(VisionUnavailable):
            await provider.identify(JPEG, "image/jpeg", None)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SECRETPIXEL" not in text


def test_openai_schema_is_json_not_free_text() -> None:
    with pytest.raises(ValueError):
        parse_vision_payload("The Witch")
    parsed = parse_vision_payload(json.loads('{"kind": "not_media", "candidates": []}'))
    assert parsed.kind == "not_media"
    assert parsed.candidates == ()
