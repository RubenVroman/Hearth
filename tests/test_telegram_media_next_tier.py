"""Next-tier Telegram media magic: status truth, house nights, watch-next, voice, play."""

from __future__ import annotations

from typing import Any

import pytest

from hearth.config import settings
from hearth.telegram.callbacks import (
    ACTION_CODES,
    ACTION_PLAY,
    ACTION_STATUS,
    CallbackCodec,
)
from hearth.telegram.media.cards import STATUS_MARKS, CardRenderer, button_label
from hearth.telegram.media.followups import detect_follow_up
from hearth.telegram.media.memory import MediaMemory
from hearth.telegram.media.moods import detect_house_night, detect_mood
from hearth.telegram.media.play import looks_like_play_command, play_lane_enabled
from hearth.telegram.media.status import availability_of, status_mark
from hearth.telegram.media.voice import exact_header, terse_pick, verbosity_for
from hearth.telegram.media.watch_next import (
    WatchNext,
    looks_like_continue_pack,
    pick_next_in_order,
    soft_prompt,
)
from hearth.telegram.models import MediaHit


class _MemStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def put_callback_media(
        self, key: str, metadata: dict[str, Any], *, ttl_s: float = 0
    ) -> None:
        self.data[key] = dict(metadata)

    def get_callback_media(self, key: str) -> dict[str, Any] | None:
        payload = self.data.get(key)
        return dict(payload) if payload else None

    def clear_callback_media(self, key: str) -> None:
        self.data.pop(key, None)


def _hit(
    title: str,
    *,
    tmdb_id: int = 1,
    year: int | None = 2020,
    status: int | None = 1,
    media_type: str = "movie",
) -> MediaHit:
    return MediaHit(
        media_type=media_type,  # type: ignore[arg-type]
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        media_status=status,
    )


# --- 1) Plex-aware status truth -------------------------------------------------


def test_status_marks_name_plex_and_downloading() -> None:
    assert "Plex" in STATUS_MARKS[5]
    assert "Download" in STATUS_MARKS[3] or "…" in STATUS_MARKS[3]


def test_availability_of_available_is_playable_not_requestable() -> None:
    avail = availability_of(_hit("Dune", status=5))
    assert avail.kind == "available"
    assert avail.playable is True
    assert avail.requestable is False
    assert "Plex" in avail.mark


def test_availability_of_processing_is_in_flight() -> None:
    avail = availability_of(_hit("Dune", status=3))
    assert avail.kind == "downloading"
    assert avail.in_flight is True
    assert avail.requestable is False
    assert "Download" in avail.button or "…" in avail.button


def test_availability_of_missing_is_gettable() -> None:
    avail = availability_of(_hit("Dune", status=1))
    assert avail.requestable is True
    assert avail.button == "Get"


def test_cards_use_status_aware_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_status_truth", True)
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    store = _MemStore()
    codec = CallbackCodec(secret="test-secret-key-for-cards")
    cards = CardRenderer(codec, store, ttl_s=600)

    available = _hit("Dune", tmdb_id=10, status=5)
    downloading = _hit("Arrival", tmdb_id=11, status=3)
    missing = _hit("Heat", tmdb_id=12, status=1)

    rendered = cards.render(
        42,
        [available, downloading, missing],
        header="Picks:",
        offer_dismiss=True,
    )
    texts: list[str] = []
    markup = rendered.reply.reply_markup or {}
    for row in markup.get("inline_keyboard") or []:
        for button in row:
            texts.append(str(button.get("text") or ""))

    assert any(t.startswith("▶") or "Play" in t or "On Plex" in t for t in texts)
    assert any("Download" in t or "Pending" in t or "…" in t for t in texts)
    assert any(t.startswith("Get") for t in texts)
    assert len(rendered.requestable) == 1
    assert rendered.requestable[0][0].tmdb_id == 12


def test_action_codes_include_play_and_status() -> None:
    assert ACTION_PLAY in ACTION_CODES
    assert ACTION_STATUS in ACTION_CODES


# --- 2) House night moods -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Friday night for us", "date_night"),
        ("date night for us", "date_night"),
        ("kids movie for Parel", "parel_kids"),
        ("something for Parel", "parel_kids"),
        ("something short while cooking", "cooking_short"),
        ("while cooking", "cooking_short"),
        ("sofa Sunday", "sofa_sunday"),
        ("something on in the background", "background_noise"),
    ],
)
def test_house_night_phrases_route_deterministically(text: str, key: str) -> None:
    spec = detect_house_night(text)
    assert spec is not None
    assert spec.key == key


def test_cooking_short_caps_runtime() -> None:
    spec = detect_house_night("something short while cooking")
    assert spec is not None
    assert spec.runtime_lte is not None
    assert spec.runtime_lte <= 90


def test_parel_kids_excludes_horror() -> None:
    spec = detect_house_night("kids movie for Parel")
    assert spec is not None
    assert 27 in spec.exclude_genre_ids  # HORROR


def test_detect_mood_prefers_house_night_over_generic_comfort() -> None:
    spec = detect_mood("Friday night for us")
    assert spec is not None
    assert spec.key == "date_night"


def test_house_nights_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_house_nights", False)
    assert detect_house_night("Friday night for us") is None


# --- 3) Watch-next memory -------------------------------------------------------


def test_pick_next_in_order_by_year() -> None:
    hits = [
        _hit("One", tmdb_id=1, year=2001),
        _hit("Two", tmdb_id=2, year=2003),
        _hit("Three", tmdb_id=3, year=2005),
    ]
    nxt = pick_next_in_order(hits, after_tmdb_id=1)
    assert nxt is not None
    assert nxt.tmdb_id == 2


def test_looks_like_continue_pack() -> None:
    assert looks_like_continue_pack("what's next")
    assert looks_like_continue_pack("continue the pack")
    assert looks_like_continue_pack("the rest of the pack")
    assert not looks_like_continue_pack("Dune: Part Two")


def test_follow_up_detects_continue_pack() -> None:
    ask = detect_follow_up("what's next")
    assert ask is not None
    assert ask.kind == "continue_pack"


def test_memory_persists_watch_next() -> None:
    memory = MediaMemory(_MemStore())
    memory.remember_watch_next(
        7,
        media_type="movie",
        tmdb_id=693134,
        title="Dune: Part Two",
        year=2024,
        from_title="Dune",
        from_tmdb_id=438631,
    )
    ctx = memory.load(7)
    assert ctx is not None
    payload = ctx.watch_next()
    assert payload is not None
    assert payload["tmdb_id"] == 693134
    assert payload["from_title"] == "Dune"
    watch = WatchNext.from_dict(payload)
    assert watch is not None
    assert "Part Two" in soft_prompt(watch)


def test_clear_watch_next_keeps_hits() -> None:
    memory = MediaMemory(_MemStore())
    memory.remember(
        9,
        hits=[_hit("Dune", tmdb_id=438631, status=1)],
        ask_kind="exact_title",
        ask_text="Dune",
        search_title="Dune",
    )
    memory.remember_watch_next(
        9,
        media_type="movie",
        tmdb_id=693134,
        title="Dune: Part Two",
        year=2024,
        from_title="Dune",
        from_tmdb_id=438631,
    )
    memory.clear_watch_next(9)
    ctx = memory.load(9)
    assert ctx is not None
    assert ctx.watch_next() is None
    assert ctx.hits and ctx.hits[0].tmdb_id == 438631


# --- 4) Voice verbosity ---------------------------------------------------------


def test_verbosity_exact_title_is_terse() -> None:
    assert verbosity_for("exact_title", confidence=0.95) == "terse"


def test_verbosity_mood_is_warm() -> None:
    assert verbosity_for("mood", confidence=0.7) == "warm"


def test_terse_pick_uses_first_variant_on_fast_path() -> None:
    bank = ("short line", "a much longer warmer line about the title")
    assert terse_pick(bank, "Dune", kind="exact_title", confidence=0.99) == "short line"


def test_exact_header_stays_short_when_confident(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_voice_verbosity", True)
    monkeypatch.setattr(settings, "telegram_butler_voice", True)
    text = exact_header("Dune (2021)", single=True, kind="exact_title", confidence=0.99)
    assert "Dune (2021)" in text
    assert len(text) < 40


def test_verbosity_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_voice_verbosity", False)
    assert verbosity_for("mood", confidence=0.4) == "terse"


# --- 5) Play command routing ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "put it on the TV",
        "play it on the TV",
        "play it",
        "throw it on the TV",
        "zet hem op de tv",
    ],
)
def test_looks_like_play_command(text: str) -> None:
    assert looks_like_play_command(text)


def test_play_command_ignores_titles() -> None:
    assert not looks_like_play_command("Playtime")
    assert not looks_like_play_command("Dune")


def test_play_lane_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_play_lane", False)
    assert play_lane_enabled() is False
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    assert play_lane_enabled() is True


def test_button_label_still_says_get_for_missing() -> None:
    label = button_label(1, _hit("Heat", status=1))
    assert label.startswith("Get 1")


def test_status_mark_uses_on_plex_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_status_truth", True)
    assert "Plex" in status_mark(_hit("Dune", status=5))
