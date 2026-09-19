"""One place where Telegram media lanes talk to Overseerr.

Each method returns ranked :class:`MediaHit` rows or raises
:class:`CatalogUnavailable` carrying the sentence the user should read. Keeping
the transport quirks here is what lets the router read like a decision tree.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from hearth.telegram.media import voice
from hearth.telegram.media.people import pick_person, rank_credits
from hearth.telegram.media.ranking import (
    MAX_RESULTS,
    rank_hits,
    to_hits,
    without_ids,
)
from hearth.telegram.media.types import MoodSpec
from hearth.telegram.models import MediaHit, MediaQuery
from hearth.tools.arr import OverseerrError

log = logging.getLogger("hearth.telegram")


class CatalogUnavailable(RuntimeError):
    """A backend failure the user must not mistake for a catalog miss."""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason or "unavailable"


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class CatalogSearch:
    """Overseerr-backed lookups for every Telegram media lane."""

    def __init__(self, client: Any) -> None:
        self.client = client

    # --- plumbing ---------------------------------------------------------

    def _fail(self, exc: BaseException, *, operation: str) -> CatalogUnavailable:
        if isinstance(exc, OverseerrError):
            if exc.operation == "authentication" or exc.status_code in {401, 403}:
                return CatalogUnavailable(voice.backend_auth_failed(), reason="auth")
            return CatalogUnavailable(voice.backend_unavailable(), reason="backend")
        log.exception("telegram %s failed", operation)
        return CatalogUnavailable(voice.backend_unexpected(), reason="unexpected")

    async def rows(self, query: MediaQuery) -> list[dict[str, Any]]:
        """Raw Overseerr rows for one query (exact id path or title search)."""
        try:
            if query.tmdb_id is not None:
                if query.media_type not in {"movie", "tv"}:
                    return []
                payload = await self.client.media_details(query.tmdb_id, query.media_type)
                if not payload.get("ok"):
                    return []
                media = payload.get("media")
                if not isinstance(media, dict):
                    media = {
                        "mediaType": query.media_type,
                        "mediaId": query.tmdb_id,
                        "title": query.title or f"TMDB {query.tmdb_id}",
                        "mediaStatus": payload.get("mediaStatus"),
                    }
                else:
                    # Movie/TV detail routes already encode the kind in their URL,
                    # so official payloads do not consistently repeat mediaType.
                    # Preserve the exact typed id from the parsed Telegram input.
                    media = dict(media)
                    media["mediaType"] = query.media_type
                    media["tmdbId"] = query.tmdb_id
                return [media]

            title = (query.title or "").strip()
            if len(title) < 2 or not any(character.isalnum() for character in title):
                return []

            payload = await self.client.search(title, page=1)
            if not payload.get("ok"):
                reason = payload.get("reason")
                if reason == "authentication_failed":
                    raise CatalogUnavailable(voice.backend_auth_failed(), reason="auth")
                raise CatalogUnavailable(voice.backend_unavailable(), reason="provider")
            return [row for row in (payload.get("results") or []) if isinstance(row, dict)]
        except CatalogUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, operation="search") from exc

    async def hits(
        self,
        query: MediaQuery,
        *,
        franchise_seed: str | None = None,
        limit: int = MAX_RESULTS,
    ) -> list[MediaHit]:
        """Ranked hits for a title query."""
        return rank_hits(
            await self.rows(query),
            query,
            franchise_seed=franchise_seed,
            limit=limit,
        )

    async def details(self, tmdb_id: int, media_type: str) -> dict[str, Any]:
        try:
            payload = await self.client.media_details(tmdb_id, media_type)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, operation="media details") from exc
        return payload if isinstance(payload, dict) else {}

    # --- lanes ------------------------------------------------------------

    async def person_hits(
        self,
        name: str,
        *,
        role: str = "cast",
        limit: int = 8,
        exclude_ids: set[int] | frozenset[int] = frozenset(),
    ) -> tuple[str, list[MediaHit]]:
        """Resolve a person then return their most recognisable credits."""
        asked = (name or "").strip()
        if not asked:
            return "", []
        try:
            found = await self.client.search_person(asked)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, operation="person search") from exc
        person = pick_person(list(found.get("results") or []), asked)
        if person is None:
            return "", []
        person_id = _integer(person.get("id"))
        resolved = str(person.get("name") or asked).strip() or asked
        if person_id is None or person_id <= 0:
            return resolved, []
        try:
            credits = await self.client.person_combined_credits(person_id)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, operation="person credits") from exc
        if not isinstance(credits, dict):
            return resolved, []
        ranked = rank_credits(credits, role=role, limit=limit + len(exclude_ids))
        hits = without_ids(to_hits(ranked), set(exclude_ids))
        return resolved, hits[: max(1, int(limit))]

    async def mood_hits(
        self,
        spec: MoodSpec,
        *,
        limit: int = 5,
        page: int = 1,
        exclude_ids: set[int] | frozenset[int] = frozenset(),
    ) -> list[MediaHit]:
        """Discover titles that match a vibe, newest vaporware excluded."""
        try:
            payload = await self.client.discover(
                genre_ids=list(spec.genre_ids),
                exclude_genre_ids=list(spec.exclude_genre_ids),
                media_type=spec.media_type,
                limit=max(1, int(limit)),
                page=max(1, int(page)),
                # An era ceiling is stricter than "released already", which is
                # the default guard against upcoming vaporware.
                primary_release_date_lte=min(spec.release_date_lte or _today(), _today()),
                primary_release_date_gte=spec.release_date_gte or None,
                vote_count_gte=spec.vote_count_gte,
                vote_average_gte=spec.vote_average_gte,
                with_runtime_lte=spec.runtime_lte,
                with_runtime_gte=spec.runtime_gte,
                sort_by=spec.sort_by,
                exclude_tmdb_ids=sorted(exclude_ids),
            )
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, operation="discover") from exc
        if not payload.get("ok"):
            return []
        rows = [row for row in (payload.get("results") or []) if isinstance(row, dict)]
        hits = to_hits(rows, media_type=spec.media_type)
        return without_ids(hits, set(exclude_ids))[: max(1, int(limit))]

    async def neighbour_hits(
        self,
        media_type: str,
        tmdb_id: int,
        *,
        limit: int = 5,
        exclude_ids: set[int] | frozenset[int] = frozenset(),
    ) -> list[MediaHit]:
        """"Something like X" via the documented similar/recommendations routes."""
        try:
            payload = await self.client.neighbours(
                tmdb_id,
                media_type,
                limit=max(1, int(limit)) + len(exclude_ids) + 4,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, operation="neighbours") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            return []
        rows = [row for row in (payload.get("results") or []) if isinstance(row, dict)]
        hits = to_hits(rows)
        banned = set(exclude_ids) | {int(tmdb_id)}
        return without_ids(hits, banned)[: max(1, int(limit))]

    async def collection_hits(
        self,
        media_type: str,
        tmdb_id: int,
        *,
        limit: int = 12,
    ) -> tuple[str, list[MediaHit]]:
        """Exact franchise pack via the title's TMDB collection, when it has one."""
        if media_type != "movie":
            return "", []
        payload = await self.details(tmdb_id, media_type)
        collection_id = _integer(payload.get("collectionId"))
        if collection_id is None or collection_id <= 0:
            return "", []
        try:
            collection = await self.client.collection(collection_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, operation="collection") from exc
        if not isinstance(collection, dict) or not collection.get("ok"):
            return "", []
        rows = [row for row in (collection.get("results") or []) if isinstance(row, dict)]
        name = str(collection.get("name") or payload.get("collectionName") or "").strip()
        return name, to_hits(rows, media_type="movie")


__all__ = ["CatalogSearch", "CatalogUnavailable"]
