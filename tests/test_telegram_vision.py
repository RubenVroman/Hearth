"""Image intake contract. All Telegram, vision, Jev and Overseerr calls are fake."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from hearth.config import settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.client import TelegramBotClient, TelegramFileError
from hearth.telegram.media.memory import speaker_scope
from hearth.telegram.media.image_requests import (
    OpenAIVisionProvider, VisionCandidate, VisionError, VisionResult,
    caption_mode, image_attachment, prepare_image,
)
from hearth.telegram.store import TelegramStore
from hearth.telegram.service import TelegramBotService


def pixels(kind: str = "PNG", *, size: tuple[int, int] = (30, 30)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color="blue").save(output, format=kind)
    return output.getvalue()


def candidate(title: str, *, year: int | None = None, kind: str | None = "movie",
              confidence: float = 0.99, season: int | None = None) -> VisionCandidate:
    return VisionCandidate(title=title, year=year, media_type=kind, confidence=confidence, season=season)


def movie(title: str, number: int, year: int, *, kind: str = "movie", status: int = 1) -> dict[str, Any]:
    return {"id": number, "title": title, "mediaType": kind, "year": year, "mediaStatus": status}


def photo(caption: str = "", *, message_id: int = 1, user: int = 42, chat: int = -1001) -> dict[str, Any]:
    return {"message_id": message_id, "chat": {"id": chat}, "from": {"id": user},
            "caption": caption, "photo": [{"file_id": "small", "width": 100, "height": 100},
                                           {"file_id": "readable", "width": 1280, "height": 900}]}


class FakeVision:
    def __init__(self, titles: list[VisionCandidate], *, kind: str = "titles", more: bool = False) -> None:
        self.result = VisionResult(kind=kind, candidates=titles, more_visible=more)
        self.calls: list[Any] = []

    async def identify(self, *args: Any, **kwargs: Any) -> VisionResult:
        self.calls.append((args, kwargs))
        return self.result


class FakeFiles:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.typing: list[int] = []
        self.data = pixels()

    async def download_image(self, file_id: str, **kwargs: Any) -> bytes:
        self.calls.append(file_id)
        return self.data

    async def send_chat_action(self, chat_id: int) -> dict[str, Any]:
        self.typing.append(chat_id)
        return {"ok": True}


class FakeCatalog:
    live = True

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.searches: list[str] = []
        self.requests: list[Any] = []
        self.result: Any = {"ok": True, "requestStatus": 2, "mediaStatus": 3, "requestId": 77}

    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.searches.append(query)
        await asyncio.sleep(0)
        return {"ok": True, "results": self.rows}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def setup_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_ids", "-1001")
    monkeypatch.setattr(settings, "telegram_user_ids", "42")
    monkeypatch.setattr(settings, "telegram_vision_enabled", True)
    monkeypatch.setattr(settings, "telegram_vision_auto_request", True)
    monkeypatch.setattr(settings, "telegram_vision_rate_per_minute", 20)
    monkeypatch.setattr(settings, "telegram_vision_max_items", 8)
    stores = []

    def make(titles, rows, **kwargs):
        store = TelegramStore(tmp_path / f"images-{len(stores)}.db")
        stores.append(store)
        files = FakeFiles()
        vision = FakeVision(titles, **kwargs)
        catalog = FakeCatalog(rows)
        tracker = SimpleNamespace(reset=lambda: None, track=lambda *args, **kwargs: None)
        bot = TelegramMediaBot(store, overseerr_client=catalog, image_client=files,
                               vision_provider=vision, progress=tracker)
        return bot, files, vision, catalog

    yield make
    for store in stores:
        store.close()


@pytest.mark.asyncio
async def test_image_automatically_requests_exact_missing_titles_once(setup_bot):
    bot, files, vision, catalog = setup_bot(
        [candidate("Alien", year=1979), candidate("Arrival", year=2016), candidate("Alien", year=1979)],
        [movie("Alien", 348, 1979), movie("Arrival", 329865, 2016)],
    )
    first = await bot.handle_message(photo())
    repeated = await bot.handle_message(photo())
    assert first == repeated
    assert len(vision.calls) == 1
    assert files.calls == ["readable"]
    assert files.typing == [-1001]
    assert [item["media_id"] for item in catalog.requests] == [348, 329865]
    assert first is not None and "Alien" in first.text and "Arrival" in first.text
    assert "Overseerr sent it" in first.text
    assert first.edit_message_id is None and first.reply_markup is None
    assert len(bot.store.list_active_requests()) == 2


@pytest.mark.asyncio
async def test_image_status_truth_and_confidence_leave_unresolved_items_unqueued(setup_bot):
    bot, _, _, catalog = setup_bot(
        [candidate("Alien"), candidate("Dune"), candidate("Arrival", year=2016),
         candidate("No idea", confidence=0.4), candidate("Ghost"), candidate("Unknown")],
        [movie("Alien", 1, 1979, status=5), movie("Dune", 2, 1984), movie("Dune", 3, 2021),
         movie("Arrival", 4, 2016, status=3), movie("Ghost", 5, 1990, status=6)],
    )
    reply = await bot.handle_message(photo())
    assert catalog.requests == []
    assert reply and "already on Plex" in reply.text and "Ambiguous" in reply.text
    assert "already requested" in reply.text and "unclear" in reply.text
    assert "No exact catalog match" in reply.text
    assert "No idea" not in catalog.searches


@pytest.mark.parametrize("caption", ["preview", "don't download these", "do not download", "What movies are these?",
                                    "download only Alien", "download all except Alien", "niet downloaden",
                                    "never download this", "stop downloading", "download the first two",
                                    "download these excluding Dune", "download 2 movies", "get the top 3",
                                    "download these 2 movies"])
@pytest.mark.asyncio
async def test_image_preview_and_negative_captions_never_request(setup_bot, caption):
    bot, _, _, catalog = setup_bot([candidate("Alien", year=1979)], [movie("Alien", 348, 1979)])
    reply = await bot.handle_message(photo(caption))
    assert not catalog.requests
    assert reply and "nothing requested" in reply.text and reply.reply_markup
    data = reply.reply_markup["inline_keyboard"][0][0]["callback_data"]
    decoded = bot._callback_codec().decode(data, -1001)
    assert decoded.tmdb_id == 348


@pytest.mark.asyncio
async def test_cancel_stops_before_download_and_clears_stale_yes_context(setup_bot):
    bot, files, vision, catalog = setup_bot([candidate("Alien")], [])
    await bot.handle_message(photo("cancel"))
    assert not files.calls and not files.typing and not vision.calls and not catalog.requests


@pytest.mark.asyncio
async def test_named_subset_caption_does_not_authorize_entire_picture(setup_bot):
    bot, _, _, catalog = setup_bot([candidate("Alien"), candidate("Arrival")],
                                   [movie("Alien", 1, 1979), movie("Arrival", 2, 2016)])
    reply = await bot.handle_message(photo("download Alien"))
    assert catalog.requests == []
    assert reply and "Image preview" in reply.text
    single, _, _, single_catalog = setup_bot([candidate("Alien")], [movie("Alien", 1, 1979)])
    await single.handle_message(photo("download Alien"))
    assert len(single_catalog.requests) == 1


@pytest.mark.parametrize("change", ["chat", "user", "bot", "torrent", "magnet", "large"])
@pytest.mark.asyncio
async def test_rejected_intake_does_not_download_or_upload(setup_bot, change):
    bot, files, vision, catalog = setup_bot([candidate("Alien")], [])
    message = photo()
    if change == "chat":
        message["chat"]["id"] = 55
    elif change == "user":
        message["from"]["id"] = 55
    elif change == "bot":
        message["from"]["is_bot"] = True
    elif change == "magnet":
        message["caption"] = "magnet:?xt=foo"
    else:
        message.pop("photo")
        message["document"] = {"file_id": "bad", "mime_type": "image/png", "file_name": "movies.torrent",
                               "file_size": 100}
        if change == "large":
            message["document"].update(file_name="movies.png", file_size=100_000_000)
    await bot.handle_message(message)
    assert not files.calls and not files.typing and not vision.calls and not catalog.requests


@pytest.mark.asyncio
async def test_invalid_pixels_do_not_reach_provider(setup_bot):
    bot, files, vision, catalog = setup_bot([candidate("Alien")], [])
    files.data = b"<html>not an image</html>"
    reply = await bot.handle_message(photo())
    assert reply and "couldn't read" in reply.text
    assert not vision.calls and not catalog.requests


@pytest.mark.asyncio
async def test_series_season_preserved_in_request_and_speaker_memory(setup_bot):
    bot, _, _, catalog = setup_bot([candidate("Severance", kind="tv", season=2)],
                                   [movie("Severance", 95396, 2022, kind="tv")])
    await bot.handle_message(photo())
    assert catalog.requests[0]["seasons"] == [2]
    with speaker_scope(-1001, 42):
        remembered = bot.memory.load(-1001)
    assert remembered and remembered.hits[0].season == 2


@pytest.mark.asyncio
async def test_uncertain_write_is_not_automatically_retried(setup_bot):
    bot, _, _, catalog = setup_bot([candidate("Alien")], [movie("Alien", 348, 1979)])
    catalog.result = RuntimeError("connection lost after POST")
    reply = await bot.handle_message(photo())
    # Simulate losing final reply while retaining the extracted evidence.
    key = "image:-1001:42:1"
    saved = bot.store.get_callback_media(key)
    saved.pop("reply")
    bot.store.put_callback_media(key, saved, ttl_s=3600)
    replay = await bot.handle_message(photo())
    assert len(catalog.requests) == 1
    assert reply and replay and "uncertain" in replay.text


@pytest.mark.asyncio
async def test_jev_hard_stop_blocks_entire_batch_with_one_decision(setup_bot, monkeypatch):
    from hearth.jev import reset_client, set_client
    from hearth.jev.schema import parse_answers

    calls = []
    async def system_one(**kwargs):
        calls.append(kwargs)
        return parse_answers({"model": "jev-test", "answers": {
            "domain": {"type": "choice", "choice": "refuse", "confidence": 0.99,
                       "probabilities": {"refuse": 0.99}},
            "risk": {"type": "score", "score": 2, "confidence": 0.99,
                     "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"}},
        }})
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    set_client(SimpleNamespace(system_one=system_one))
    try:
        bot, _, _, catalog = setup_bot([candidate("Alien"), candidate("Arrival")],
                                       [movie("Alien", 1, 1979), movie("Arrival", 2, 2016)])
        reply = await bot.handle_message(photo())
        assert len(calls) == 1
        assert not catalog.requests
        assert reply is not None
    finally:
        reset_client()


@pytest.mark.parametrize("gate", ["cancel", "deny", "confirm", "lane"])
@pytest.mark.asyncio
async def test_automatic_images_honor_jev_write_decision(setup_bot, monkeypatch, gate):
    from hearth.jev import reset_client, set_client
    from hearth.jev.schema import parse_answers

    calls = []
    answers = {
        "tool_allow": {"type": "noul", "noul": 0.1 if gate == "deny" else 0.9},
        "is_cancel": {"type": "noul", "noul": 0.99 if gate == "cancel" else 0.01},
        "risk": {"type": "score", "score": 1 if gate == "confirm" else 0, "confidence": 0.99,
                 "legend": {"0": "harmless", "1": "needs_confirm", "2": "do_not_auto_run"}},
        "tool_lane": {"type": "choice", "choice": "no_tool" if gate == "lane" else "media_queue",
                      "confidence": 0.99,
                      "probabilities": {"no_tool" if gate == "lane" else "media_queue": 0.99}},
    }
    async def system_one(**kwargs):
        calls.append(kwargs)
        return parse_answers({"model": "jev-test", "answers": answers})
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_shadow", False)
    set_client(SimpleNamespace(system_one=system_one))
    try:
        bot, _, _, catalog = setup_bot([candidate("Alien"), candidate("Arrival")],
                                       [movie("Alien", 1, 1979), movie("Arrival", 2, 2016)])
        reply = await bot.handle_message(photo())
        assert len(calls) == 1
        assert catalog.requests == []
        assert reply is not None
        if gate == "confirm":
            assert "confirmation" in reply.text and reply.reply_markup
            data = reply.reply_markup["inline_keyboard"][0][0]["callback_data"]
            # A real Get is an explicit confirm and retains existing behavior.
            await bot.handle_callback({"id": "cb", "data": data, "from": {"id": 42},
                                       "message": {"message_id": 99, "chat": {"id": -1001}}})
            assert len(catalog.requests) == 1
    finally:
        reset_client()


def test_prepare_image_validates_contents_and_strips_exif():
    original = Image.new("RGB", (30, 30))
    exif = Image.Exif()
    exif[270] = "private photo metadata"
    source = io.BytesIO()
    original.save(source, format="JPEG", exif=exif)
    data, mime = prepare_image(source.getvalue(), mime="image/jpeg", max_bytes=100000)
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as cleaned:
        assert not cleaned.getexif()
    with pytest.raises(VisionError):
        prepare_image(pixels(), mime="image/jpeg", max_bytes=100000)
    with pytest.raises(VisionError):
        prepare_image(pixels("GIF"), mime=None, max_bytes=100000)


@pytest.mark.parametrize("value", ["https://bad.test/file", "magnet:?xt=123", "title\nrequest all", "movie.torrent"])
def test_provider_title_validation_drops_file_and_instruction_payloads(value):
    with pytest.raises(ValidationError):
        candidate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["path", "redirect", "size", "stream", "token_error"])
async def test_telegram_file_download_boundary(failure):
    requests = []
    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {
                "file_path": "../secret" if failure == "path" else "photos/file.png",
                "file_size": 10000 if failure == "size" else 100,
            }})
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://untrusted.test/file"})
        if failure == "token_error":
            raise httpx.ConnectError("URL has token 123:SECRET", request=request)
        return httpx.Response(200, content=b"x" * 1025)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient("123:SECRET", client=http)
        with pytest.raises(TelegramFileError) as error:
            await client.download_image("file", max_bytes=1024)
    assert "SECRET" not in str(error.value)
    assert all(request.url.host == "api.telegram.org" for request in requests)
    if failure in {"path", "size"}:
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_structured_vision_payload_has_no_remote_file_url_and_no_retries(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    provider = OpenAIVisionProvider()
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        result = VisionResult(kind="titles", candidates=[candidate("Alien", year=1979)], more_visible=False)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            refusal=None, content=result.model_dump_json()))])
    provider._key = "test-key"
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    identified = await provider.identify(pixels(), "image/png", "best horror movies", limit=8)
    assert identified.candidates[0].title == "Alien"
    request = calls[0]
    assert request["store"] is False
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_list_cap_visible_and_all_results_fit_message(setup_bot, monkeypatch):
    monkeypatch.setattr(settings, "telegram_vision_max_items", 2)
    bot, _, _, catalog = setup_bot([candidate("Alien"), candidate("Arrival"), candidate("Dune")],
                                   [movie("Alien", 1, 1979), movie("Arrival", 2, 2016), movie("Dune", 3, 2021)])
    reply = await bot.handle_message(photo())
    assert len(catalog.requests) == 2
    assert reply and "remaining" in reply.text and len(reply.text) < 4096


def test_caption_mode_and_thumbnail_choice():
    assert caption_mode("download these movies") == "request"
    assert caption_mode("download this list of horror films") == "request"
    assert caption_mode("get these shows") == "request"
    assert caption_mode("show me what is in this image") == "preview"
    assert caption_mode("don’t download this list") == "preview"
    assert caption_mode("") == "request"
    assert caption_mode("stop") == "cancel"
    assert image_attachment(photo(), max_bytes=1024).file_id == "readable"


@pytest.mark.asyncio
async def test_vision_close_failure_still_releases_poller_lock(setup_bot, monkeypatch):
    bot, _, vision, _ = setup_bot([], [])
    released = []
    async def close():
        raise RuntimeError("vision client close failed")
    vision.aclose = close
    monkeypatch.setattr(bot.store, "release_poller_lock", lambda: released.append(True))
    service = TelegramBotService(bot=bot)
    with pytest.raises(RuntimeError, match="close failed"):
        await service.stop()
    assert released == [True]


@pytest.mark.asyncio
async def test_stream_limit_applies_without_content_length_and_successful_files_return_bytes():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 600
            yield b"x" * 600
    async def handler(request):
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/file.png"}})
        return httpx.Response(200, stream=Stream())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient("123:SECRET", client=http)
        with pytest.raises(TelegramFileError, match="too large"):
            await client.download_image("file", max_bytes=1024)
        assert await client.download_image("file", max_bytes=2048) == b"x" * 1200


@pytest.mark.asyncio
async def test_typing_task_is_cancelled_when_image_fails(setup_bot):
    bot, files, _, _ = setup_bot([], [])
    stopped = asyncio.Event()
    async def action(chat_id):
        files.typing.append(chat_id)
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()
    files.send_chat_action = action
    files.data = b"not image bytes"
    reply = await bot.handle_message(photo())
    assert reply and "couldn't read" in reply.text
    assert files.typing == [-1001] and stopped.is_set()
