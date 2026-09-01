"""Unit tests for hearth.telegram.offer — Overseerr resolve_offer contract."""

from __future__ import annotations

import pytest

from hearth.telegram.offer import (
    FUZZY_THRESHOLD,
    is_short_seed,
    movie_tv_hits,
    multiword_title_matches,
    offer_row_matches_seed,
    pick_offer_for_title,
    resolve_offer,
    short_seed_matches,
)


def test_is_short_seed_land_vs_multiword():
    assert is_short_seed("Land")
    assert is_short_seed("Wild")
    assert not is_short_seed("Late Night with the Devil")
    assert not is_short_seed("Rescued by Ruby")
    assert not is_short_seed("The Man from Earth")


def test_short_seed_land_not_la_la_land():
    assert short_seed_matches("Land", "Land")
    assert not short_seed_matches("Land", "La La Land")
    assert not offer_row_matches_seed("Land", "La La Land")


def test_multiword_distinctive_containment():
    assert multiword_title_matches(
        "Late Night with the Devil", "Late Night with the Devil"
    )
    assert multiword_title_matches("Rescued by ruby", "Rescued by Ruby")
    assert multiword_title_matches("The Man from Earth", "The Man from Earth")
    assert offer_row_matches_seed(
        "Late Night with the Devil", "Late Night with the Devil"
    )


def test_movie_tv_hits_drops_person_and_fallback():
    rows = movie_tv_hits(
        [
            {
                "id": 1,
                "mediaType": "person",
                "name": "Someone",
                "knownFor": [],
            },
            {
                "id": 2,
                "mediaType": "movie",
                "title": "Dune",
                "year": 2021,
                "matched": "fallback",
            },
            {
                "id": 1020006,
                "mediaType": "movie",
                "title": "Late Night with the Devil",
                "year": 2023,
            },
        ]
    )
    assert len(rows) == 1
    assert rows[0]["tmdbId"] == 1020006
    assert rows[0]["mediaType"] == "movie"


def test_pick_offer_fuzzy_when_pending_id_lost():
    offers = [
        {
            "id": 99,
            "tmdbId": 99,
            "mediaId": 99,
            "mediaType": "movie",
            "title": "Some Other Film",
            "year": 1999,
        },
        {
            "id": 1020006,
            "tmdbId": 1020006,
            "mediaId": 1020006,
            "mediaType": "movie",
            "title": "Late Night with the Devil",
            "year": 2023,
        },
    ]
    pick = pick_offer_for_title(offers, "Late Night with the Devil", year=2023)
    assert pick is not None
    assert pick["tmdbId"] == 1020006
    assert FUZZY_THRESHOLD >= 80


def test_pick_offer_returns_top_when_search_had_hits():
    """Never return None when offers is non-empty — avoid format_not_found."""
    offers = [
        {
            "id": 1,
            "tmdbId": 1,
            "mediaId": 1,
            "mediaType": "movie",
            "title": "Unrelated",
            "year": 2000,
        }
    ]
    pick = pick_offer_for_title(offers, "Totally Different Title")
    assert pick is not None
    assert pick["tmdbId"] == 1


@pytest.mark.asyncio
async def test_resolve_offer_multiword_keeps_overseerr_hit(monkeypatch):
    from hearth.telegram import offer as offer_mod

    late = {
        "id": 1020006,
        "mediaType": "movie",
        "title": "Late Night with the Devil",
        "year": 2023,
    }

    async def _search(query: str):
        return {"results": [late]}

    monkeypatch.setattr(offer_mod.overseerr, "search", _search)
    rows = await resolve_offer("Late Night with the Devil")
    assert rows
    assert rows[0]["tmdbId"] == 1020006


@pytest.mark.asyncio
async def test_resolve_offer_short_seed_excludes_substring(monkeypatch):
    from hearth.telegram import offer as offer_mod

    async def _search(query: str):
        return {
            "results": [
                {
                    "id": 313369,
                    "mediaType": "movie",
                    "title": "La La Land",
                    "year": 2016,
                },
                {
                    "id": 688271,
                    "mediaType": "movie",
                    "title": "Land",
                    "year": 2021,
                },
            ]
        }

    monkeypatch.setattr(offer_mod.overseerr, "search", _search)
    rows = await resolve_offer("Land")
    assert rows
    assert all(r["title"] == "Land" for r in rows)
    assert all(r["tmdbId"] == 688271 for r in rows)
