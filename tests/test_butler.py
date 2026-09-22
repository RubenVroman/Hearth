"""Shelf, scene presets, queue asides, and per-person Telegram threads."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hearth.butler.nudge import aside_for, queue_shelf_aside
from hearth.butler.phrases import classify_house_phrase, house_route
from hearth.butler.scenes import activate_preset
from hearth.butler.shelf import shelf_snapshot
from hearth.config import settings
from hearth.telegram.bot import TelegramMediaBot
from hearth.telegram.media.memory import MediaMemory, speaker_scope
from hearth.telegram.models import MediaHit
from hearth.telegram.store import TelegramStore
from hearth.tools.ha import _mock
from hearth.tools.plex import plex


def test_house_phrases_do_not_steal_playback_or_titles() -> None:
    assert classify_house_phrase("what's playing") is None
    assert classify_house_phrase("what's on") is None
    assert classify_house_phrase("what's on the tv") is None
    assert classify_house_phrase("Get Out") is None
    assert classify_house_phrase("play Dune") is None
    assert classify_house_phrase("something like Arrival") is None

    tonight = classify_house_phrase("what's on tonight?")
    assert tonight is not None and tonight.kind == "shelf"
    assert classify_house_phrase("wat kunnen we kijken").kind == "shelf"
    assert classify_house_phrase("already on plex").kind == "shelf"

    movie = classify_house_phrase("please start movie night")
    assert movie is not None and movie.preset == "movie_night"
    assert classify_house_phrase("quiet hours").preset == "quiet_hours"
    assert classify_house_phrase("slaap lekker").preset == "good_night"
    assert house_route("turn on movie night") == {
        "tool": "house_scene",
        "args": {"preset": "movie_night"},
    }
    assert house_route("what's playing") is None


def test_on_deck_fixture_has_progress() -> None:
    deck = asyncio.run(plex.on_deck())
    assert deck["mode"] == "mock"
    bear = deck["items"][0]
    assert bear["show"] == "The Bear"
    assert bear["progress_pct"] == 40
    assert "S02E06" in bear["label"]


def test_shelf_snapshot_speaks_only_real_fixture_titles() -> None:
    snap = asyncio.run(shelf_snapshot())
    assert snap["ok"] is True
    spoken = snap["speak"]
    assert "The Bear" in spoken
    assert "Poor Things" in spoken
    assert "Dune: Part Two" in spoken
    assert "Blade Runner" not in spoken or "On right now" in spoken
    assert snap["last_played"]["title"] == "Blade Runner 2049"


def test_queue_aside_matches_continue_watching_exactly() -> None:
    deck = [
        {"title": "Arrival", "label": "Arrival", "progress_pct": 40},
        {"title": "Fishes", "show": "The Bear", "label": "The Bear S02E06", "progress_pct": 40},
    ]
    now = [{"title": "Dune: Part Two", "state": "playing", "player": "Apple TV"}]
    assert aside_for("Dune", on_deck=deck, now=now) is None
    live = aside_for("Dune: Part Two", on_deck=deck, now=now)
    assert live is not None and "playing" in live and "Apple TV" in live
    parked = aside_for("Arrival", on_deck=deck, now=[])
    assert parked is not None and "40%" in parked
    assert aside_for("The Bear", on_deck=deck, now=[]) is not None

    spoken = asyncio.run(queue_shelf_aside("Arrival"))
    assert spoken is not None and "40%" in spoken
    assert asyncio.run(queue_shelf_aside("Severance")) is None


def test_movie_night_runs_and_quiet_hours_refuses_without_a_scene() -> None:
    try:
        movie = asyncio.run(activate_preset("movie_night"))
        assert movie["ok"] is True
        assert movie["entity_id"] == "scene.movie_night"
        assert "Movie night is on" in movie["speak"]
        living = _mock.get_state("light.living_room")
        assert living is not None
        assert living["attributes"]["brightness"] == 40

        quiet = asyncio.run(activate_preset("quiet_hours"))
        assert quiet["ok"] is False
        assert "don’t see a Quiet hours scene" in quiet["speak"]
        assert "Movie night" in quiet["speak"]
        assert "Good night" in quiet["speak"]
        # Lights stay where movie night left them.
        assert _mock.get_state("light.living_room")["attributes"]["brightness"] == 40
    finally:
        _mock.reset()


def test_shell_offers_shelf_presets_and_a_retry(client) -> None:
    html = client.get("/")
    assert html.status_code == 200
    page = html.text
    assert 'id="house-chips"' in page
    assert 'id="house-fault-retry"' in page
    assert 'id="shelf-list"' in page
    assert 'data-preset="movie_night"' in page
    assert 'data-preset="quiet_hours"' in page
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "renderHousePulse" in script.text
    assert "runPreset" in script.text


def test_chat_tonight_and_scenes(client) -> None:
    tonight = client.post("/api/chat", json={"message": "what's on tonight"})
    assert tonight.status_code == 200
    body = tonight.json()
    assert body["tools"][0]["name"] == "house_shelf"
    assert "The Bear" in body["reply"]
    assert "Poor Things" in body["reply"]

    playing = client.post("/api/chat", json={"message": "what's playing"})
    assert playing.json()["tools"][0]["name"] == "plex_now_playing"

    pulse = client.get("/api/house/pulse")
    assert pulse.status_code == 200
    pulse_body = pulse.json()
    assert pulse_body["continue_watching"]
    assert pulse_body["last_played"]["title"] == "Blade Runner 2049"
    presets = {row["preset"]: row for row in pulse_body["presets"]}
    assert presets["movie_night"]["available"] is True
    assert presets["quiet_hours"]["available"] is False

    scene = client.post("/api/house/scene", json={"preset": "movie_night"})
    assert scene.status_code == 200
    assert scene.json()["ok"] is True
    rooms = client.get("/api/rooms")
    living = next(row for row in rooms.json()["lights"] if row["entity_id"] == "light.living_room")
    assert living["attributes"]["brightness"] == 40

    missing = client.post("/api/house/scene", json={"preset": "quiet_hours"})
    assert missing.json()["ok"] is False
    assert "Quiet hours" in missing.json()["speak"]

    unknown = client.post("/api/house/scene", json={"preset": "disco"})
    assert unknown.json()["ok"] is False


def test_group_memory_is_per_speaker(tmp_path) -> None:
    store = TelegramStore(tmp_path / "threads.db")
    memory = MediaMemory(store)
    hit = MediaHit(media_type="movie", tmdb_id=438631, title="Dune", year=2021)
    try:
        with speaker_scope(-100, 1):
            memory.remember(-100, hits=[hit], ask_text="dune", search_title="Dune")
            assert memory.load(-100) is not None
        with speaker_scope(-100, 2):
            assert memory.load(-100) is None
        with speaker_scope(-100, 1):
            loaded = memory.load(-100)
            assert loaded is not None
            assert loaded.search_title == "Dune"
        with speaker_scope(8, 1):
            memory.remember(8, hits=[hit], ask_text="dm")
        with speaker_scope(8, 2):
            loaded_dm = memory.load(8)
            assert loaded_dm is not None
            assert loaded_dm.ask_text == "dm"
    finally:
        store.close()


class _SearchOnly:
    live = True

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results
        self.search_calls: list[tuple[str, int]] = []
        self.request_calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, page: int = 1) -> dict[str, Any]:
        self.search_calls.append((query, page))
        return {"ok": True, "mode": "live", "results": list(self.results)}

    async def media_details(self, media_id: int, media_type: str) -> dict[str, Any]:
        return {"ok": False, "reason": "not_found"}

    async def request(self, **kwargs: Any) -> dict[str, Any]:
        self.request_calls.append(dict(kwargs))
        return {"ok": True, "requestStatus": 2, "mediaStatus": 3, "requestId": 1}


def _message(text: str, *, user_id: int, message_id: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": -100123, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": False},
        "text": text,
    }


@pytest.fixture
def shelf_bot(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TelegramMediaBot:
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:butler")
    monkeypatch.setattr(settings, "telegram_chat_ids", "-100123")
    monkeypatch.setattr(settings, "telegram_user_ids", "")
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 30)
    monkeypatch.setattr(settings, "jev_enabled", False)
    store = TelegramStore(tmp_path / "butler-telegram.db")
    bot = TelegramMediaBot(store, overseerr_client=_SearchOnly([]))
    yield bot
    store.close()


@pytest.mark.asyncio
async def test_telegram_shelf_and_scene_skip_overseerr(shelf_bot: TelegramMediaBot) -> None:
    fake = shelf_bot.overseerr
    tonight = await shelf_bot.handle_message(_message("what's on tonight", user_id=7))
    assert tonight is not None
    assert "The Bear" in tonight.text
    assert "Poor Things" in tonight.text
    assert fake.search_calls == []

    movie = await shelf_bot.handle_message(_message("movie night", user_id=7, message_id=2))
    assert movie is not None
    assert "Movie night is on" in movie.text
    assert fake.search_calls == []

    quiet = await shelf_bot.handle_message(_message("quiet hours", user_id=7, message_id=3))
    assert quiet is not None
    assert quiet.text
    assert "Quiet hours" in quiet.text
    assert "is on" not in quiet.text
    _mock.reset()


@pytest.mark.asyncio
async def test_other_person_cannot_confirm_or_follow_a_thread(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:butler")
    monkeypatch.setattr(settings, "telegram_chat_ids", "-100123")
    monkeypatch.setattr(settings, "telegram_user_ids", "")
    monkeypatch.setattr(settings, "telegram_rate_limit_per_minute", 30)
    monkeypatch.setattr(settings, "jev_enabled", False)
    store = TelegramStore(tmp_path / "split.db")
    fake = _SearchOnly(
        [
            {
                "mediaType": "movie",
                "id": 438631,
                "title": "Dune",
                "releaseDate": "2021-09-15",
                "mediaInfo": {"status": 1},
            }
        ]
    )
    bot = TelegramMediaBot(store, overseerr_client=fake)
    try:
        first = await bot.handle_message(_message("Dune", user_id=1))
        assert first is not None
        assert "Dune" in first.text
        assert fake.request_calls == []

        stolen = await bot.handle_message(_message("yes", user_id=2, message_id=2))
        assert stolen is None
        assert fake.request_calls == []

        follow = await bot.handle_message(_message("the sequel", user_id=2, message_id=3))
        assert follow is not None
        assert "isn’t on your thread" in follow.text
        assert fake.request_calls == []

        confirmed = await bot.handle_message(_message("yes", user_id=1, message_id=4))
        assert confirmed is not None
        assert fake.request_calls
        assert fake.request_calls[0]["media_id"] == 438631
    finally:
        store.close()
        _mock.reset()
