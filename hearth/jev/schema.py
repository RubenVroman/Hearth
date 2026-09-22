"""Hearth-tuned System One question schema and typed verdicts.

Jev returns Choice / Noul / Score answers with probabilities — it does not
generate text. Labels follow Hearth tools (HA lights, media/Infuse/Plex,
Thuisbezorgd, weather, workspace/docker, Overseerr/*arr queue, CoS).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Choice options for house routing (cheap triage before gpt/tools).
DOMAIN_CRITERIA: dict[str, str] = {
    "lights": (
        "Home Assistant lights, scenes, covers/blinds, or routine room device "
        "control/status (not media)."
    ),
    "media": (
        "Play/pause, TV/AVR/Apple TV/Infuse/Plex/Videoland, house media status, "
        "or browsing the library — not a new download request."
    ),
    "food": "Thuisbezorgd / Just Eat food browse, cart, or order.",
    "weather": "Local weather or forecast.",
    "files": "Workspace files, skills, or Docker container inspect/stop.",
    "web": "Live web search or current events outside the house stack.",
    "escalate_cos": (
        "Repo/PR/code, GitHub/GitLab, Gridways, Discord, calendar, or anything "
        "Hearth cannot do — escalate to Chief of Staff."
    ),
    "chat": "Ordinary conversation or clarification with no house tool needed.",
    "refuse": "Unsafe, off-limits, or something Hearth should decline.",
}

# Telegram media router (first-class). Parallel Choice on every media-ish turn.
MEDIA_ASK_CRITERIA: dict[str, str] = {
    "exact_title": (
        "User named one clear movie/show title to find or download "
        "(e.g. 'Dune', 'Talk to Me', 'Severance S02') — not a plot riddle."
    ),
    "known_franchise": (
        "User named a well-known franchise seed without asking for every entry "
        "(e.g. bare 'Harry Potter' or 'Lord of the Rings') — show franchise titles."
    ),
    "series_all": (
        "User wants the whole series/franchise/collection "
        "(e.g. 'Harry Potter, all movies', 'all Harry Potters', 'whole LOTR trilogy')."
    ),
    "edition_aware": (
        "User asked for a specific cut or quality of a known title "
        "(extended edition, director's cut, theatrical, 4K/UHD, remastered, etc.)."
    ),
    "person_filmography": (
        "User wants what an actor or director made, not one title "
        "(e.g. 'anything with Florence Pugh', 'Nolan films', 'Tom Hanks movies')."
    ),
    "mood_vibe": (
        "User described a mood, genre, occasion, runtime, or era instead of a "
        "title (e.g. 'scary under 2 hours', 'Friday night comfort', 'kids movie', "
        "'what should we watch?')."
    ),
    "similar_to": (
        "User wants titles adjacent to a named one "
        "(e.g. 'something like Arrival', 'more in that vein')."
    ),
    "batch_multi": (
        "One message asking for two or more distinct titles "
        "(e.g. 'grab Inception and Interstellar', 'LOTR extended + Hobbit theatrical')."
    ),
    "follow_up": (
        "Short message that only resolves against the last few media replies "
        "(e.g. 'the sequel', 'all of them', 'nah the other one', 'more like that', "
        "'the second one')."
    ),
    "descriptive_riddle": (
        "Plot, appearance, Dutch/English description, or riddle about one "
        "specific title — needs an LLM to guess the catalog name before search."
    ),
    "chat_about_title": (
        "Question about a title (plot, year, cast, 'what's that about?') "
        "without clear download/get intent."
    ),
    "not_media": (
        "Not a movie/TV catalog ask — lights, food, chatter, list/status, "
        "or unrelated house talk."
    ),
}

MEDIA_ASK_KINDS = tuple(MEDIA_ASK_CRITERIA.keys())

RISK_LEVELS = (
    "harmless",  # routine read / chat
    "needs_confirm",  # paid, destructive, or queue-ish — confirm gate
    "do_not_auto_run",  # cancel / refuse / high harm — do not auto-execute tools
)

Domain = Literal[
    "lights",
    "media",
    "food",
    "weather",
    "files",
    "web",
    "escalate_cos",
    "chat",
    "refuse",
]

EnforceAction = Literal["continue", "block_cancel", "escalate_cos"]

# Tools that queue / grab media — blocked on high-confidence cancel in enforce.
QUEUE_TOOLS = frozenset(
    {
        "radarr_add",
        "sonarr_add",
        "overseerr_request",
        "radarr_grab_release",
    }
)


def _confirm_cancel_risk_questions() -> dict[str, dict[str, Any]]:
    return {
        "wants_queue": {
            "type": "noul",
            "instructions": (
                "Is the user asking to download, grab, snatch, or request a movie/show "
                "via Overseerr, Radarr, or Sonarr (queue a new title)?"
            ),
            "criteria": {
                "true": "Clear download/request/grab intent for media.",
                "false": "Play, status, search-only, or unrelated.",
            },
        },
        "is_confirm": {
            "type": "noul",
            "instructions": (
                "Is this a short affirmative confirm (yes/ja/sure/👍) to proceed with a "
                "pending house action such as queueing a title?"
            ),
            "criteria": {
                "true": "Bare confirm to proceed.",
                "false": "New request, cancel, or unrelated text.",
            },
        },
        "is_cancel": {
            "type": "noul",
            "instructions": (
                "Is this a cancel / reject (no/nah/nee/stop/don't) of a pending queue "
                "or confirmable action?"
            ),
            "criteria": {
                "true": "User cancels or refuses the pending action.",
                "false": "Not a cancel.",
            },
        },
        "risk": {
            "type": "score",
            "instructions": (
                "How risky is auto-running house tools for this message without an "
                "extra confirm step?"
            ),
            "criteria": list(RISK_LEVELS),
        },
    }


def _media_router_questions() -> dict[str, dict[str, Any]]:
    """First-class Telegram media intent router (Choice + needs_llm Noul)."""
    return {
        "media_ask": {
            "type": "choice",
            "instructions": (
                "Classify this Telegram house message for the Overseerr movie/TV bot. "
                "Prefer exact_title or known_franchise when the user named a real title. "
                "Use series_all only when they want every entry. "
                "Use edition_aware when a cut/quality preference is attached to a title. "
                "Use person_filmography for actor/director asks, mood_vibe for "
                "mood/genre/runtime/occasion asks, similar_to for 'like X' asks, "
                "batch_multi when two or more distinct titles are requested at once, "
                "and follow_up for short messages that only make sense against the "
                "previous reply. "
                "Use descriptive_riddle for plots/riddles that need an LLM. "
                "Use chat_about_title for info questions without download intent. "
                "Use not_media for lights, food, chatter, or non-catalog asks."
            ),
            "criteria": dict(MEDIA_ASK_CRITERIA),
        },
        "needs_llm": {
            "type": "noul",
            "instructions": (
                "Does resolving this message into catalog title(s) require a generative "
                "LLM (gpt) hop, rather than a direct TMDB/Overseerr title search?"
            ),
            "criteria": {
                "true": (
                    "Plot/riddle/description guess, or the media_ask is "
                    "descriptive_riddle / chat_about_title with unclear title."
                ),
                "false": (
                    "Exact title, franchise seed, series-all, edition-aware, person, "
                    "mood, similar-to, batch, or follow-up ask that can be answered "
                    "from Overseerr/TMDB routes directly."
                ),
            },
        },
        "multi_item": {
            "type": "noul",
            "instructions": (
                "Does this message ask for more than one title (a batch, a whole "
                "franchise, or a shortlist to choose from)?"
            ),
            "criteria": {
                "true": "Two or more titles, a whole series, or a 'give me options' ask.",
                "false": "One title, one question, or no catalog ask at all.",
            },
        },
    }


def hearth_system_one_questions() -> dict[str, dict[str, Any]]:
    """Raw question map for POST /v1/systemone (also used to build SDK objects)."""
    return {
        "domain": {
            "type": "choice",
            "instructions": (
                "Which Hearth house domain best matches the latest user message? "
                "Prefer escalate_cos for repo/PR/agent work Hearth cannot do."
            ),
            "criteria": dict(DOMAIN_CRITERIA),
        },
        **_media_router_questions(),
        **_confirm_cancel_risk_questions(),
    }


def telegram_media_system_one_questions() -> dict[str, dict[str, Any]]:
    """Telegram-first System One map: media router + confirm/cancel (no house domain).

    Used on every media-ish Telegram turn so Jev classifies intent in one parallel
    call before any OpenAI prose or Overseerr search strategy is chosen.
    """
    return {
        **_media_router_questions(),
        **_confirm_cancel_risk_questions(),
    }


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    noul: float

    @property
    def yes(self) -> bool:
        return self.noul >= 0.5


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    confidence: float
    legend: dict[str, str] = field(default_factory=dict)
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def level_index(self) -> int:
        return int(round(max(0.0, min(float(len(RISK_LEVELS) - 1), self.score))))

    @property
    def level(self) -> str:
        idx = self.level_index
        if 0 <= idx < len(RISK_LEVELS):
            return RISK_LEVELS[idx]
        return RISK_LEVELS[0]


@dataclass(frozen=True, slots=True)
class JevAnswers:
    domain: ChoiceAnswer | None = None
    media_ask: ChoiceAnswer | None = None
    needs_llm: NoulAnswer | None = None
    multi_item: NoulAnswer | None = None
    wants_queue: NoulAnswer | None = None
    is_confirm: NoulAnswer | None = None
    is_cancel: NoulAnswer | None = None
    risk: ScoreAnswer | None = None
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def as_log_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"model": self.model}
        if self.domain is not None:
            out["domain"] = {
                "choice": self.domain.choice,
                "confidence": round(self.domain.confidence, 4),
                "probabilities": {
                    k: round(v, 4) for k, v in self.domain.probabilities.items()
                },
            }
        if self.media_ask is not None:
            out["media_ask"] = {
                "choice": self.media_ask.choice,
                "confidence": round(self.media_ask.confidence, 4),
                "probabilities": {
                    k: round(v, 4) for k, v in self.media_ask.probabilities.items()
                },
            }
        if self.needs_llm is not None:
            out["needs_llm"] = round(self.needs_llm.noul, 4)
        if self.multi_item is not None:
            out["multi_item"] = round(self.multi_item.noul, 4)
        if self.wants_queue is not None:
            out["wants_queue"] = round(self.wants_queue.noul, 4)
        if self.is_confirm is not None:
            out["is_confirm"] = round(self.is_confirm.noul, 4)
        if self.is_cancel is not None:
            out["is_cancel"] = round(self.is_cancel.noul, 4)
        if self.risk is not None:
            out["risk"] = {
                "score": round(self.risk.score, 4),
                "level": self.risk.level,
                "confidence": round(self.risk.confidence, 4),
            }
        return out


@dataclass(frozen=True, slots=True)
class JevVerdict:
    """Decision gate result. Shadow mode never enforces; enforce may redirect."""

    enabled: bool
    shadow: bool
    ok: bool
    answers: JevAnswers | None = None
    # What enforce would do (also logged in shadow).
    suggested: EnforceAction = "continue"
    # What the caller should actually do this turn.
    action: EnforceAction = "continue"
    error: str = ""
    reason: str = ""

    @property
    def enforcing(self) -> bool:
        return self.enabled and not self.shadow

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "shadow": self.shadow,
            "ok": self.ok,
            "suggested": self.suggested,
            "action": self.action,
            "reason": self.reason,
            "error": self.error or None,
            "answers": self.answers.as_log_dict() if self.answers else None,
        }


def parse_answers(payload: dict[str, Any]) -> JevAnswers:
    """Normalize SDK or HTTP response bodies into JevAnswers."""
    model = str(payload.get("model") or "")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        # SDK may expose typed maps instead of a unified answers dict.
        answers = {}
        for key, bucket in (
            ("choices", "choice"),
            ("nouls", "noul"),
            ("scores", "score"),
        ):
            block = payload.get(key)
            if isinstance(block, dict):
                for name, value in block.items():
                    answers[name] = _answer_from_object(value, default_type=bucket)

    domain = _parse_choice(answers.get("domain"))
    media_ask = _parse_choice(answers.get("media_ask"))
    needs_llm = _parse_noul(answers.get("needs_llm"))
    multi_item = _parse_noul(answers.get("multi_item"))
    wants_queue = _parse_noul(answers.get("wants_queue"))
    is_confirm = _parse_noul(answers.get("is_confirm"))
    is_cancel = _parse_noul(answers.get("is_cancel"))
    risk = _parse_score(answers.get("risk"))
    return JevAnswers(
        domain=domain,
        media_ask=media_ask,
        needs_llm=needs_llm,
        multi_item=multi_item,
        wants_queue=wants_queue,
        is_confirm=is_confirm,
        is_cancel=is_cancel,
        risk=risk,
        model=model,
        raw=dict(payload),
    )


def _answer_from_object(value: Any, *, default_type: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    data: dict[str, Any] = {"type": default_type}
    for attr in ("choice", "noul", "score", "confidence", "probabilities", "legend"):
        if hasattr(value, attr):
            data[attr] = getattr(value, attr)
    return data


def _parse_choice(raw: Any) -> ChoiceAnswer | None:
    if raw is None:
        return None
    data = _answer_from_object(raw, default_type="choice")
    choice = str(data.get("choice") or "").strip()
    if not choice:
        return None
    try:
        confidence = float(data.get("confidence") if data.get("confidence") is not None else 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    probs_raw = data.get("probabilities") or {}
    probs: dict[str, float] = {}
    if isinstance(probs_raw, dict):
        for key, value in probs_raw.items():
            try:
                probs[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return ChoiceAnswer(
        choice=choice,
        confidence=max(0.0, min(1.0, confidence)),
        probabilities=probs,
    )


def _parse_noul(raw: Any) -> NoulAnswer | None:
    if raw is None:
        return None
    data = _answer_from_object(raw, default_type="noul")
    try:
        value = float(data.get("noul"))
    except (TypeError, ValueError):
        return None
    return NoulAnswer(noul=max(0.0, min(1.0, value)))


def _parse_score(raw: Any) -> ScoreAnswer | None:
    if raw is None:
        return None
    data = _answer_from_object(raw, default_type="score")
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError):
        return None
    try:
        confidence = float(data.get("confidence") if data.get("confidence") is not None else 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    legend_raw = data.get("legend") or {}
    legend = {str(k): str(v) for k, v in legend_raw.items()} if isinstance(legend_raw, dict) else {}
    probs_raw = data.get("probabilities") or {}
    probs: dict[str, float] = {}
    if isinstance(probs_raw, dict):
        for key, value in probs_raw.items():
            try:
                probs[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return ScoreAnswer(
        score=score,
        confidence=max(0.0, min(1.0, confidence)),
        legend=legend,
        probabilities=probs,
    )


__all__ = [
    "DOMAIN_CRITERIA",
    "MEDIA_ASK_CRITERIA",
    "MEDIA_ASK_KINDS",
    "Domain",
    "EnforceAction",
    "JevAnswers",
    "JevVerdict",
    "QUEUE_TOOLS",
    "RISK_LEVELS",
    "ChoiceAnswer",
    "NoulAnswer",
    "ScoreAnswer",
    "hearth_system_one_questions",
    "parse_answers",
    "telegram_media_system_one_questions",
]
