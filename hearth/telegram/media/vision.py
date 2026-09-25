"""Still-image intake for the Telegram media bot.

Vision stops at titles. Bytes stay in memory for the turn, logs carry counts
and outcomes, and Overseerr is reached only through the same search the typed
title lane already uses. Nothing here queues a request.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from hearth.config import settings
from hearth.telegram.media.ranking import plausible_match
from hearth.telegram.media.search import CatalogUnavailable
from hearth.telegram.models import MediaHit, MediaQuery
from hearth.tools.arr import title_seed_matches

log = logging.getLogger("hearth.telegram")

VisionKind = Literal["single", "list", "not_media", "refuse"]

_INJECTION = re.compile(
    r"magnet:\?|https?://|\.torrent\b|\btorrent\b|"
    r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions|"
    r"download\s+this",
    re.IGNORECASE,
)
_CAPTION_SEASON = re.compile(
    r"\b(?:s(?P<s_short>\d{1,2})(?:[._-]?e(?P<episode>\d{1,3}))?|"
    r"(?:season|seizoen)\s*(?P<s_word>\d{1,2}))\b",
    re.IGNORECASE,
)
_LOG_FIELDS = (
    "chat_id",
    "message_id",
    "mime",
    "byte_length",
    "provider",
    "kind",
    "candidate_count",
    "confidence_bucket",
    "outcome",
)


@dataclass(frozen=True, slots=True)
class VisionCandidate:
    """One depicted catalog title. Confidence never queues by itself."""

    title: str
    year: int | None = None
    media_type: str | None = None
    confidence: float = 0.0
    season: int | None = None


@dataclass(frozen=True, slots=True)
class VisionResult:
    """Structured provider output. Notes are dropped before they can be shown."""

    kind: VisionKind
    candidates: tuple[VisionCandidate, ...] = ()
    list_label: str = ""
    more_visible: bool = False


@dataclass(frozen=True, slots=True)
class VisionPlanItem:
    label: str
    hits: tuple[MediaHit, ...] = ()
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class VisionPlan:
    items: tuple[VisionPlanItem, ...] = ()
    omitted: tuple[str, ...] = ()
    list_label: str = ""
    catalog_message: str = ""

    @property
    def single(self) -> bool:
        return len(self.items) == 1 and not self.omitted


def vision_mode() -> str:
    """Preserve explicit preview/shadow modes; auto uses the guarded queue."""
    mode = (settings.telegram_vision_mode or "").strip().lower()
    if mode == "shadow":
        return "shadow"
    if mode in {"confirm", "auto"}:
        return mode
    return ""


def vision_provider_name() -> str:
    return (settings.telegram_vision_provider or "").strip().lower()


def vision_lane_active() -> bool:
    """True when an allowlisted image should be identified instead of refused.

    The lane defaults on. It still stays dark without a configured provider,
    and the OpenAI adapter stays dark without ``OPENAI_API_KEY``, so a house
    with no key keeps today's refusal.
    """
    if not settings.telegram_vision_lane or not settings.telegram_vision_enabled:
        return False
    if vision_mode() not in {"shadow", "confirm", "auto"}:
        return False
    name = vision_provider_name()
    if name == "openai":
        return settings.openai_configured
    return name in {"local", "fixture"}


def residency_allows_upload(purpose: str) -> bool:
    """Whether image bytes may leave the house for this purpose.

    Catalog art — posters, title cards, list graphics — is not house CCTV and
    is not blocked on an EU residency note. Household photos and any other
    personal image stay off a cloud provider until
    ``HEARTH_TELEGRAM_VISION_RESIDENCY`` is ``accepted`` or ``eu``, or the
    provider is ``local``.
    """
    kind = (purpose or "").strip().lower()
    if kind in {"catalog", "poster", "list"}:
        return True
    if vision_provider_name() == "local":
        return True
    note = (settings.telegram_vision_residency or "").strip().lower()
    return note in {"accepted", "eu"}


def sniff_image_mime(data: bytes) -> str | None:
    """Return jpeg/png/webp when the magic matches, else None."""
    if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def declared_mime_matches(declared: str, sniffed: str | None) -> bool:
    if sniffed is None:
        return False
    declared_norm = (declared or "").split(";", 1)[0].strip().casefold()
    if declared_norm == "image/jpg":
        declared_norm = "image/jpeg"
    return declared_norm == sniffed


def sanitize_title(value: object) -> str | None:
    """Keep a catalog title. Drop magnets, URLs, and instruction text."""
    title = " ".join(str(value or "").split()).strip().strip("\"'`")
    if not title or len(title) > 200:
        return None
    if _INJECTION.search(title):
        return None
    if not any(character.isalnum() for character in title):
        return None
    return title


def sanitize_label(value: object) -> str:
    label = sanitize_title(value) or ""
    return label[:80]


def _year(value: object) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    if 1888 <= year <= 2100:
        return year
    return None


def _confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def _media_type(value: object) -> str | None:
    kind = str(value or "").strip().lower()
    if kind in {"movie", "film"}:
        return "movie"
    if kind in {"tv", "show", "series", "serie"}:
        return "tv"
    return None


def parse_vision_payload(data: object) -> VisionResult:
    """Validate a provider JSON object. Free text never becomes a title."""
    if not isinstance(data, dict):
        raise ValueError("vision payload is not an object")
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in {"single", "list", "not_media", "refuse"}:
        kind = "not_media"
    if kind in {"not_media", "refuse"}:
        return VisionResult(kind=kind)  # type: ignore[arg-type]
    raw_candidates = data.get("candidates")
    candidates: list[VisionCandidate] = []
    seen: set[tuple[str, int | None]] = set()
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            title = sanitize_title(item.get("title") or item.get("search_title"))
            if title is None:
                continue
            year = _year(item.get("year"))
            key = (title.casefold(), year)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                VisionCandidate(
                    title=title,
                    year=year,
                    media_type=_media_type(item.get("media_type") or item.get("media_kind")),
                    confidence=_confidence(item.get("confidence")),
                )
            )
    if not candidates:
        return VisionResult(kind="not_media")
    resolved: VisionKind = "single" if len(candidates) == 1 else "list"
    return VisionResult(
        kind=resolved,
        candidates=tuple(candidates),
        list_label=sanitize_label(data.get("list_label")),
    )


def confidence_bucket(candidates: tuple[VisionCandidate, ...] | list[VisionCandidate]) -> str:
    if not candidates:
        return "none"
    top = max(item.confidence for item in candidates)
    show = float(settings.telegram_vision_show_confidence)
    if top >= show:
        return "show"
    if top >= show - float(settings.telegram_vision_ambiguous_margin):
        return "ambiguous"
    return "low"


def log_vision_outcome(**fields: object) -> None:
    """Log an image turn without pixels, file URLs, or provider payloads."""
    payload = {key: fields[key] for key in _LOG_FIELDS if key in fields}
    log.info("telegram vision %s", payload)


def caption_season(caption: str) -> tuple[int | None, bool]:
    """Season stated in a caption, plus whether an episode number was ignored."""
    match = _CAPTION_SEASON.search(caption or "")
    if match is None:
        return None, False
    season_text = match.group("s_short") or match.group("s_word")
    try:
        season = int(season_text)
    except (TypeError, ValueError):
        season = None
    if season is not None and not 1 <= season <= 80:
        season = None
    episode = bool(match.group("episode"))
    return season, episode


def select_candidates(
    candidates: tuple[VisionCandidate, ...] | list[VisionCandidate],
    *,
    show: float,
    margin: float,
    cap: int,
) -> tuple[list[VisionCandidate], list[VisionCandidate], list[str]]:
    """Split depicted titles into search, uncertain, and over-cap names.

    A two-title ambiguity within ``margin`` is kept even when the runner-up
    sits just under the show band. A long list does not pull weak guesses in.
    """
    ordered = list(candidates)
    if len(ordered) == 2:
        top = max(item.confidence for item in ordered)
        if top >= show:
            chosen = [
                item
                for item in ordered
                if top - item.confidence <= margin and item.confidence >= show - margin
            ]
        else:
            chosen = []
    else:
        chosen = [item for item in ordered if item.confidence >= show]
    uncertain = [item for item in ordered if item not in chosen]
    shown = chosen[: max(0, cap)]
    omitted = [item.title for item in chosen[max(0, cap) :]]
    return shown, uncertain, omitted


def _prefer_hits(candidate: VisionCandidate, hits: list[MediaHit]) -> list[MediaHit]:
    """Title agreement first, then year. A year conflict is a pick, not a swap."""
    titled = [
        hit
        for hit in hits
        if title_seed_matches(candidate.title, hit.title)
        or title_seed_matches(candidate.title, hit.original_title)
        or plausible_match(candidate.title, hit)
    ]
    pool = titled or []
    if not pool:
        return []
    if candidate.year is not None:
        agreed = [hit for hit in pool if hit.year == candidate.year]
        if len(agreed) == 1:
            return agreed
        if len(agreed) > 1:
            return agreed[:2]
        return pool[:2]
    years = {hit.year for hit in pool[:2]}
    if len(pool) > 1 and len(years) > 1:
        return pool[:2]
    return pool[:1]


async def resolve_candidates(
    candidates: list[VisionCandidate],
    *,
    search_hits: Callable[[MediaQuery], Awaitable[list[MediaHit]]],
    list_label: str = "",
    season: int | None = None,
    show: float | None = None,
    margin: float | None = None,
    cap: int | None = None,
) -> VisionPlan:
    """Search each depicted title once. Misses stay names. Nothing is queued."""
    show_at = float(settings.telegram_vision_show_confidence if show is None else show)
    margin_at = float(
        settings.telegram_vision_ambiguous_margin if margin is None else margin
    )
    limit = int(settings.telegram_vision_list_cap if cap is None else cap)
    chosen, uncertain, omitted = select_candidates(
        candidates,
        show=show_at,
        margin=margin_at,
        cap=limit,
    )
    items: list[VisionPlanItem] = []
    failures = 0
    catalog_message = ""
    for candidate in chosen:
        # A caption season applies to one series, not to every tile in a collage.
        item_season = None
        if len(chosen) == 1 and candidate.media_type != "movie":
            item_season = season
        query = MediaQuery(
            action="search",
            media_type=candidate.media_type if candidate.media_type in {"movie", "tv"} else None,
            title=candidate.title,
            year=candidate.year,
            season=item_season,
            reason="vision",
            raw_text=candidate.title,
        )
        try:
            hits = await search_hits(query)
        except CatalogUnavailable as exc:
            failures += 1
            catalog_message = exc.message
            items.append(VisionPlanItem(label=candidate.title))
            continue
        kept = _prefer_hits(candidate, list(hits))
        items.append(VisionPlanItem(label=candidate.title, hits=tuple(kept)))
    for candidate in uncertain:
        items.append(VisionPlanItem(label=candidate.title, uncertain=True))
    if chosen and failures == len(chosen) and catalog_message:
        return VisionPlan(
            items=tuple(items),
            omitted=tuple(omitted),
            list_label=list_label,
            catalog_message=catalog_message,
        )
    return VisionPlan(
        items=tuple(items),
        omitted=tuple(omitted),
        list_label=list_label,
    )
