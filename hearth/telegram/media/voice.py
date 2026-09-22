"""House-butler phrasing for Telegram media replies.

Short, confident, warm — and deterministic. Variant choice is derived from the
subject text, so the same ask always reads the same way and tests can rely on
the stable core of every line (title, year, status words) being present.

``HEARTH_TELEGRAM_BUTLER_VOICE=false`` drops the flavour and keeps the plain
operational sentence.
"""

from __future__ import annotations

import hashlib

from hearth.config import settings


def _enabled() -> bool:
    return bool(getattr(settings, "telegram_butler_voice", True))


def _pick(bank: tuple[str, ...], seed: str) -> str:
    if not bank:
        return ""
    if not _enabled():
        return bank[0]
    digest = hashlib.sha256((seed or "").encode("utf-8", "ignore")).digest()
    return bank[digest[0] % len(bank)]


def display_title(title: str, year: int | None = None) -> str:
    clean = (title or "").strip() or "that title"
    return f"{clean} ({year})" if year else clean


def kind_word(media_type: str) -> str:
    return "movie" if media_type == "movie" else "series"


# --- headers ------------------------------------------------------------------


def exact_header(label: str, *, single: bool, kind: str = "exact_title", confidence: float = 1.0) -> str:
    if single:
        return terse_pick(
            (
                f"Found it — {label}.",
                f"Got it: {label}.",
                f"{label} — that's the one.",
            ),
            label,
            kind=kind,
            confidence=confidence,
        )
    return _pick(
        (
            f"Closest matches for “{label}”:",
            f"Here's what the catalog has for “{label}”:",
            f"A few candidates for “{label}”:",
        ),
        label,
    )


def franchise_header(seed: str) -> str:
    return _pick(
        (
            f"The {seed} shelf — pick your poison:",
            f"{seed}, in order:",
            f"Here's the {seed} run:",
        ),
        seed,
    )


def series_header(seed: str, *, dropped: str = "") -> str:
    tail = f" (skipping {dropped})" if dropped else ""
    return _pick(
        (
            f"Whole {seed} series{tail} — tap Get on each one you want:",
            f"All of {seed}{tail}, in release order — tap Get per title:",
            f"{seed} complete{tail}. Tap Get on the ones you want:",
        ),
        seed,
    )


def edition_header(title: str, edition_label: str) -> str:
    return _pick(
        (
            f"{title} — noted, you want the {edition_label}:",
            f"{edition_label} of {title}, coming up:",
            f"Right, {title} in {edition_label}:",
        ),
        f"{title}{edition_label}",
    )


def person_header(name: str, *, role: str = "cast") -> str:
    verb = "directed" if role == "directing" else "starred in"
    return _pick(
        (
            f"Best of what {name} {verb}:",
            f"{name} — the highlights:",
            f"Pick of the {name} catalog:",
        ),
        f"{name}{verb}",
    )


def mood_header(mood_label: str) -> str:
    return _pick(
        (
            f"{mood_label.capitalize()} — house shortlist:",
            f"For {mood_label}, I'd put these on the table:",
            f"{mood_label.capitalize()}. Try one of these:",
        ),
        mood_label,
    )


def similar_header(anchor: str) -> str:
    return _pick(
        (
            f"More in the vein of {anchor}:",
            f"If you liked {anchor}, these are next:",
            f"Same energy as {anchor}:",
        ),
        anchor,
    )


def house_pick_header() -> str:
    return _pick(
        (
            "No brief, so here are the house picks:",
            "Dealer's choice — a few solid ones:",
            "Nothing specific? These never miss:",
        ),
        "house",
    )


def batch_header(labels: list[str]) -> str:
    joined = ", ".join(labels)
    return _pick(
        (
            f"Two jobs on the list: {joined}." if len(labels) == 2 else f"On the list: {joined}.",
            f"Plan: {joined}.",
            f"Lining up {joined}.",
        ),
        joined,
    )


def follow_up_header(subject: str, *, what: str) -> str:
    if what == "prequel":
        return _pick(
            (
                f"What came before {subject}:",
                f"The prequel to {subject}:",
            ),
            subject,
        )
    return _pick(
        (
            f"The sequel to {subject}:",
            f"What comes after {subject}:",
        ),
        subject,
    )


# --- status lines -------------------------------------------------------------


def already_available(label: str) -> str:
    return _pick(
        (
            f"{label} is already on Plex — nothing to fetch.",
            f"Already in the library: {label}. Go press play.",
            f"{label} is sitting on Plex already.",
        ),
        label,
    )


def already_requested(label: str) -> str:
    return _pick(
        (
            f"{label} is already requested — it's in the pipeline.",
            f"Already queued: {label}. I'll leave it be.",
            f"{label} is already on its way.",
        ),
        label,
    )


def queued(label: str, *, detail: str) -> str:
    return _pick(
        (
            f"{label} — {detail}",
            f"Done. {label} — {detail}",
            f"On it: {label} — {detail}",
        ),
        label,
    )


def tap_get_hint(*, single: bool) -> str:
    if single:
        return _pick(
            (
                "Tap Get to request it, or reply yes / nah.",
                "Say the word — tap Get, or just reply yes.",
            ),
            "single",
        )
    return _pick(
        (
            "Tap Get on the one you want.",
            "Tap Get on whichever you fancy.",
        ),
        "multi",
    )


def nothing_requestable() -> str:
    return _pick(
        (
            "Everything above is already handled — on Plex or already requested.",
            "All of that is already sorted: available or queued.",
        ),
        "handled",
    )


def cancelled() -> str:
    # Every variant keeps "not queueing": the user must never be left guessing
    # whether a request slipped through.
    return _pick(
        (
            "Okay — not queueing that. Send another title or description.",
            "Right, not queueing it. What else can I dig up?",
        ),
        "cancel",
    )


def which_one() -> str:
    return _pick(
        (
            "Which one? Tap Get on it, or say “the second one”.",
            "Happy to — which of those? Tap Get, or say “number 2”.",
        ),
        "which",
    )


def pending_expired() -> str:
    return "That offer expired. Send the title again and I'll line it up."


def no_match(label: str) -> str:
    return _pick(
        (
            f"Nothing in the catalog matches “{label}”. Give me the exact title, "
            "a TMDB link, or describe it and I'll guess.",
            f"No catalog hit for “{label}”. Try the exact name, a TMDB link, "
            "or tell me the plot.",
        ),
        label,
    )


def exclusion_left_nothing(seed: str, *, found: int) -> str:
    entries = "1 entry" if found == 1 else f"{found} entries"
    return (
        f"That leaves nothing — the catalog only has {entries} for {seed}, and you "
        "asked me to skip at least that many. Narrow the exclusion?"
    )


def no_more_options(subject: str) -> str:
    return _pick(
        (
            f"That's the end of the good ones for {subject}. Want a different angle?",
            f"I'm out of fresh picks for {subject}. Give me another vibe?",
        ),
        subject,
    )


def unknown_person(name: str) -> str:
    return f"I can't find anyone called “{name}” in the catalog. Check the spelling?"


def need_a_subject() -> str:
    return _pick(
        (
            "Like what, exactly? Name a title and I'll find its neighbours.",
            "Give me an anchor title and I'll pull similar ones.",
        ),
        "anchor",
    )


def lost_context() -> str:
    return _pick(
        (
            "I've lost the thread — which title did you mean?",
            "Nothing recent to go on. Name the title again?",
        ),
        "context",
    )


def nudge() -> str:
    return (
        "Give me a title, a franchise, an actor, or a vibe — “scary under 2 hours”, "
        "“all Harry Potters”, “something like Arrival”. I'll find it; you tap Get."
    )


def list_ask() -> str:
    return (
        "I don't queue from a list ask. Name a title, franchise, edition, or vibe — "
        "then tap Get."
    )


# --- failures -----------------------------------------------------------------


def backend_not_configured() -> str:
    return "Overseerr isn't configured here, so I can't run a real catalog search."


def backend_auth_failed() -> str:
    return (
        "Overseerr rejected its configured API key. Fix the key or its request "
        "permissions before searching again."
    )


def backend_unavailable() -> str:
    return (
        "Overseerr search is unavailable right now. That's a backend error, not a "
        "catalog miss."
    )


def backend_unexpected() -> str:
    return "Overseerr search failed unexpectedly. Try again shortly."


def needs_openai() -> str:
    return (
        "That reads like a description rather than a title. Send the name, or "
        "configure OpenAI so I can guess."
    )


def lane_failed() -> str:
    # Silence after a real ask is the worst possible answer, so an unexpected
    # lane failure still says what happened and that nothing was queued.
    return (
        "That search broke on my side — nothing was queued. Try again, or send "
        "the exact title and I'll go straight at it."
    )


def rate_limited(*, wait_s: int, ask: str = "") -> str:
    heard = f" I heard “{ask}”, so send it again then." if ask else ""
    return f"Give me about {wait_s}s — too many searches in a row.{heard}"




# --- verbosity ---------------------------------------------------------------


def verbosity_for(kind: str, *, confidence: float = 1.0) -> str:
    """Return ``terse`` / ``warm`` based on lane + confidence.

    Exact title / sure Get stays short. Fuzzy mood / person / similar gets a
    slightly richer line. Disabled when ``HEARTH_TELEGRAM_VOICE_VERBOSITY`` is off.
    """
    from hearth.config import settings as _settings

    if not bool(getattr(_settings, "telegram_voice_verbosity", True)):
        return "terse"
    kind = (kind or "").strip().lower()
    conf = float(confidence or 0.0)
    if kind in {"exact_title", "edition", "known_franchise"} and conf >= 0.8:
        return "terse"
    if kind in {"mood", "person", "similar", "house_pick", "batch", "follow_up"}:
        return "warm"
    if conf < 0.65:
        return "warm"
    return "terse"


def terse_pick(bank: tuple[str, ...], seed: str, *, kind: str = "", confidence: float = 1.0) -> str:
    """Like ``_pick``, but forces the first (shortest) line on the fast path."""
    if not bank:
        return ""
    if verbosity_for(kind, confidence=confidence) == "terse":
        return bank[0]
    return _pick(bank, seed)


def play_started(label: str, *, where: str = "the TV") -> str:
    return _pick(
        (
            f"Playing {label} on {where}.",
            f"On the screen: {label}.",
            f"{label} — going out to {where}.",
        ),
        label,
    )


def play_failed(label: str, *, reason: str) -> str:
    clean = (reason or "playback isn't wired for that right now").strip()
    return f"Couldn't play {label} — {clean}"


def play_needs_title() -> str:
    return (
        "I've lost which title that button was for, so I won't guess at the TV. "
        "Search it again and tap Play on the fresh card."
    )


def play_not_on_plex(label: str) -> str:
    return f"{label} isn't on Plex yet, so there's nothing to play. Tap Get and I'll fetch it."


def status_ack(label: str, *, state: str) -> str:
    if state == "pending":
        return f"{label} is still waiting for approval — nothing to Get again."
    if state == "downloading":
        return f"{label} is already downloading — I'll leave the queue alone."
    return f"{label} is already handled ({state})."


def watch_next_nudge(label: str, *, after: str) -> str:
    return (
        f"Queued. Next in the pack after {after}: {label} — "
        "say “what's next” or “the sequel” when you want it."
    )


def watch_next_offer(label: str, *, after: str) -> str:
    return f"Continuing the pack after {after} — {label}:"

__all__ = [
    "verbosity_for",
    "terse_pick",
    "play_started",
    "play_failed",
    "play_needs_title",
    "play_not_on_plex",
    "status_ack",
    "watch_next_nudge",
    "watch_next_offer",
    "already_available",
    "already_requested",
    "backend_auth_failed",
    "backend_not_configured",
    "backend_unavailable",
    "backend_unexpected",
    "batch_header",
    "cancelled",
    "display_title",
    "edition_header",
    "exact_header",
    "exclusion_left_nothing",
    "follow_up_header",
    "franchise_header",
    "house_pick_header",
    "kind_word",
    "lane_failed",
    "list_ask",
    "lost_context",
    "mood_header",
    "need_a_subject",
    "needs_openai",
    "no_match",
    "no_more_options",
    "nothing_requestable",
    "nudge",
    "pending_expired",
    "person_header",
    "queued",
    "rate_limited",
    "series_header",
    "similar_header",
    "tap_get_hint",
    "unknown_person",
    "which_one",
]
