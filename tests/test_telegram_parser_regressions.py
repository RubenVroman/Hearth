"""Regression coverage for deterministic Telegram request parsing."""

from __future__ import annotations

import pytest

from hearth.telegram.parse import parse_message_text


@pytest.mark.parametrize(
    "text",
    [
        "Severance S02E03",
        "tmdb:tv:95396 S02E03",
        "https://www.themoviedb.org/tv/95396-severance S02E03",
    ],
)
def test_episode_syntax_is_rejected_instead_of_widened_to_a_season(text: str) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "reject"
    assert parsed.reason == "episode_not_supported"


@pytest.mark.parametrize(
    "text",
    [
        "movie: Dune S02",
        "movie Dune S02",
        "Dune S02 movie",
        "https://www.themoviedb.org/movie/438631-dune S02",
    ],
)
def test_movie_season_contradictions_are_rejected(text: str) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "reject"
    assert parsed.reason == "movie_has_season"


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("Movie 43", "Movie 43"),
        ("The Movie", "The Movie"),
        ("Series 7", "Series 7"),
        ("Dune movie", "Dune movie"),
    ],
)
def test_media_words_in_real_titles_are_not_treated_as_type_hints(
    text: str,
    title: str,
) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "search"
    assert parsed.title == title
    assert parsed.media_type is None


def test_only_unambiguous_type_syntax_sets_a_media_type() -> None:
    movie = parse_message_text("movie: Dune (2021)")
    series = parse_message_text("tv: Severance S02")
    suffix = parse_message_text("Dune (movie)")

    assert (movie.title, movie.year, movie.media_type) == ("Dune", 2021, "movie")
    assert (series.title, series.season, series.media_type) == ("Severance", 2, "tv")
    assert (suffix.title, suffix.media_type) == ("Dune", "movie")


@pytest.mark.parametrize(
    "title",
    [
        "Searching for Bobby Fischer",
        "Searching for Sugar Man",
        "Dune is downloading",
    ],
)
def test_human_titles_are_not_suppressed_as_bot_status_echoes(title: str) -> None:
    parsed = parse_message_text(title)

    assert parsed.action == "search"
    assert parsed.title == title


def test_actual_bot_messages_are_still_ignored() -> None:
    parsed = parse_message_text("Searching for Dune", is_bot=True)

    assert parsed.action == "ignore"
    assert parsed.reason == "bot_sender"


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("get Dune", "Dune"),
        ("get me Arrival", "Arrival"),
        ("get mij Arrival", "Arrival"),
        ("search Dune", "Dune"),
        ("search for The Matrix", "The Matrix"),
        ("zoek naar Dark", "Dark"),
        ("download voor mij Blade Runner", "Blade Runner"),
        ("can you please get Dune", "Dune"),
    ],
)
def test_request_prefixes_and_fillers_are_removed(text: str, title: str) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "search"
    assert parsed.title == title


@pytest.mark.parametrize("title", ["Get Out", "Search Party"])
def test_title_cased_ambiguous_request_words_remain_part_of_titles(title: str) -> None:
    parsed = parse_message_text(title)

    assert parsed.action == "search"
    assert parsed.title == title


def test_search_command_treats_its_argument_as_a_literal_title() -> None:
    parsed = parse_message_text("/search get Dune")

    assert parsed.action == "search"
    assert parsed.title == "get Dune"


@pytest.mark.parametrize(
    ("text", "title", "year", "season"),
    [
        ("Severance - S02", "Severance", None, 2),
        ("Dune - (2021)", "Dune", 2021, None),
        ("Dark | seizoen 3", "Dark", None, 3),
    ],
)
def test_separator_noise_is_removed(
    text: str,
    title: str,
    year: int | None,
    season: int | None,
) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "search"
    assert parsed.title == title
    assert parsed.year == year
    assert parsed.season == season


@pytest.mark.parametrize(
    "text",
    [
        "file.torrent!",
        "please get release.torrent,",
        "show.torrent.zip",
    ],
)
def test_torrent_filenames_with_punctuation_are_rejected(text: str) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "reject"
    assert parsed.reason == "torrent_download"


@pytest.mark.parametrize(
    "text",
    [
        "tmdb:movie:12345678901",
        "movie tmdb:12345678901",
        "tmdb:not-a-number",
    ],
)
def test_invalid_or_overlong_tmdb_ids_are_rejected(text: str) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "reject"
    assert parsed.reason == "invalid_tmdb_id"


@pytest.mark.parametrize(
    ("text", "title", "media_type"),
    [
        ("Talk to me, the movie", "Talk to me", "movie"),
        ("Late night with the devil", "Late night with the devil", None),
        ("Back to the future", "Back to the future", None),
    ],
)
def test_vault_horror_and_classic_titles_parse_cleanly(
    text: str,
    title: str,
    media_type: str | None,
) -> None:
    parsed = parse_message_text(text)

    assert parsed.action == "search"
    assert parsed.title == title
    assert parsed.media_type == media_type
