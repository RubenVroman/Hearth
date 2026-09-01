"""Telegram movie-request intent — conversation-first.

Instant path (no model) only when there is no reasonable doubt:
catalog id/URL, explicit ``Title (YYYY)``, or a live numbered pick
(``1``/``2``/``3``, ``all of them``, ``de eerste``, or short exact ``yes``
while a 1-item guess is on screen).

Everything else always calls gpt-4o with live pending options and/or Overseerr
catalog hits for this turn as ``candidates``, plus the last ~8 turns of this
chat. No keyword gates, no franchise/actor maps, no expanding confirm-phrase
lists. Plot/vibe asks → one best ``search_title`` guess; the inbox asks the
user to confirm before queueing. Unsure with 2+ hits → clarify with a real
1–N list; never invent a grab or a list-less ``reply 1–1``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from hearth.config import settings
from hearth.memory.redact import redact
from hearth.telegram.catalog import catalog_search_title

log = logging.getLogger("hearth.telegram")

IntentAction = Literal[
    "passthrough",
    "ignore",
    "clarify",
    "pick",
    "pick_many",
    "search",
    "retry",
]

MAX_CANDIDATES = 12
MAX_BATCH = 10
MIN_RESOLVE_CONFIDENCE = 0.55

# Telegram conversation hop: prefer gpt-4o. Honor OPENAI_MODEL when it is
# already set to a named model other than mini (house UI keeps its own default).
TELEGRAM_INTENT_MODEL = "gpt-4o"

_FOLLOWUP_ALL = re.compile(
    r"\b("
    r"all(?:\s+of\s+(?:them|em|'em|it))?"
    r"|all\s+(?:the\s+)?(?:movies?|films?|ones?)"
    r"|every(?:one|thing)?"
    r"|allemaal|alles"
    r")\b",
    re.I,
)
_FOLLOWUP_FIRST = re.compile(
    r"^\s*(?:"
    r"(?:just\s+)?(?:the\s+)?first(?:\s+one)?|"
    r"(?:de\s+|het\s+)?eerste(?:\s+(?:een|één|one|film|movie))?"
    r")\s*[.!?]*\s*$",
    re.I,
)
_YEAR_PAREN = re.compile(r"\(\s*((?:19|20)\d{2})\s*\)")
_CATALOG_ID = re.compile(r"\btt\d{7,}|\btmdb:\d+|\btvdb:\d+", re.I)
_URLISH = re.compile(r"https?://", re.I)
_ORDINAL_PICK = re.compile(r"^\s*#?\s*([1-9])\s*$")
# Elongated enthusiasm counts: yesss, yeahhh, jaaa, yeess, …
_CONFIRM_YES = re.compile(
    r"^\s*(?:"
    r"y+e+s+|y+e+a+h+|y+e+p+|y+u+p+|su+re+|correct|right|"
    r"that(?:'s| is|s)?\s+(?:it|the\s+one|correct|right)|"
    r"j+a+|jawel|klopt|juist|"
    r"dat\s+is\s+(?:hem|die|het|correct)|"
    r"doe\s+(?:maar|die)|"
    r"go\s+ahead"
    r")\s*[.!?]*\s*$",
    re.I,
)
_CONFIRM_THUMBS = re.compile(
    r"^\s*👍[\U0001F3FB-\U0001F3FF]?\uFE0F?\s*[.!?]*\s*$"
)
# Bare reject of an on-screen Did-you-mean / numbered offer (NL+EN).
# Keep short — longer "niet X, ik zoek Y" clues still go through the model.
_CONFIRM_NO = re.compile(
    r"^\s*(?:"
    r"n+a+h+|"
    r"n+o+p+e+|"
    r"n+o+(?:\s+thanks?)?|"
    r"n+e+e+(?:\s+hoor)?|"
    r"niet(?:\s+die)?|"
    r"not\s+that|"
    r"no\s+not\s+(?:that|it|this)|"
    r"anders|"
    r"wrong"
    r")\s*[.!?]*\s*$",
    re.I,
)
# "name a few more" / "give me options" — want a short list, not one Did-you-mean.
# Do NOT match "name a few that look like Land" (seed look-alike; reuse that title).
_LIST_ASK = re.compile(
    r"(?:"
    r"\bname a few more\b|"
    r"\ba few more\b|"
    r"\bi was asking for a few\b|"
    r"\basking for a (?:few|list|options)\b|"
    r"\ba few\b.+\b(?:movies?|films?|shows?|series|titles?|options|sci-?fi)\b|"
    r"\bgive me (?:a few |some )?(?:cool |good )?.{0,40}\b(?:movies?|films?|shows?|options)\b|"
    r"\bgive me (?:a few |some )?options\b|"
    r"\b(?:show|give|list)\s+(?:me\s+)?(?:some\s+)?options\b|"
    r"\bmore options\b|"
    r"\been paar meer\b|"
    r"\bnog een paar\b|"
    r"^\s*name a few\s*[.!?]*\s*$|"
    r"^\s*(?:show|give|list)\s+(?:me\s+)?(?:a\s+)?few\s*[.!?]*\s*$|"
    r"^\s*opties\s*[.!?]*\s*$"
    r")",
    re.I,
)
# "another one" / "find one" / "surprise me" — bot must guess, not demand a title.
# Do NOT match bare "Another Earth"-style title asks.
_RECOMMEND_ASK = re.compile(
    r"(?:"
    r"\bdo you know another\b|"
    r"\banother\s+one\b|"
    r"^\s*another\s*[.!?]*\s*$|"
    r"\banother\s+.+\b(?:horror|space|sci-?fi|spaceship|vibe|like|old|classic)\b|"
    r"\bfind(?:\s+me)?\s+(?:one|another|something)\b|"
    r"\bi don'?t know,?\s*find\b|"
    r"\bsurprise me\b|"
    r"\bmore like that\b|"
    r"\b(?:same|that)\s+vibe\b|"
    r"\brecommend\b|"
    r"\bsuggest(?:\s+(?:one|something|another))?\b|"
    r"\bpick(?:\s+me)?\s+(?:one|another)\b|"
    r"\bnog\s+(?:een(?:tje)?|iets)\b|"
    r"\bken je nog\b|"
    r"\bzoek\s+maar\b"
    r")",
    re.I,
)
_STOP_TOKENS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "met",
        "featuring",
        "starring",
        "feat",
        "ft",
        "movie",
        "movies",
        "film",
        "films",
        "series",
        "show",
        "tv",
        "all",
        "them",
        "one",
        "de",
        "het",
        "een",
        "die",
        "dat",
    }
)
_CLARIFY_LIST_HINT = re.compile(
    r"(?:reply|pick|kies|antwoord).{0,24}\b1\s*[-–—]\s*\d+"
    r"|\b1\s*[-–—]\s*\d+\b.{0,16}(?:or|of|,'|\")",
    re.I,
)

_SYSTEM = (
    "You interpret short Telegram messages for a house movie/TV download bot. "
    "Return JSON only. Never invent a grab. "
    "DEFAULT for chit-chat, emoji, reactions, meta talk about the bot, thanks, "
    "and off-topic chatter (NL+EN) is action=ignore (empty — no reply). "
    "Examples of ignore: 🙈, 'ga ik fixen', 'jaartallen kloppen niet', lol, "
    "thanks, ok, cool, short jokes about the bot. Do NOT ask which movie. "
    "You are in a multi-turn conversation: use recent_history (NL+EN) to resolve "
    "plot descriptions, pronouns, corrections, actor/artist clues, and misspellings. "
    "subject_title is the last resolved title — drop it when the user starts a "
    "NEW title ask that does not match it, or when they reject it. "
    "rejected_titles must NEVER be re-offered as search_title or implied picks. "
    "When the user rejects the current/last suggestion (nah/no/nope/nee/niet die/"
    "not that/anders/no not X / we already have that) and adds new clues or asks "
    "for another, return action=search with a NEW search_title from the original "
    "plot + new clues; do not ask them to pick 1–2 for a rejected film. NEVER "
    "action=pick after a reject — that queues the rejected title. Bare "
    "nah/no/nope/nee/anders with no new info → action=search a DIFFERENT catalog "
    "title (preferred) or action=clarify asking what they want instead (not a "
    "1–2 re-list of the rejected film). "
    "candidates are the live on-screen offer and/or Overseerr/TMDB hits for THIS "
    "turn. When candidates_are_live_pending is true, those rows are the sticky "
    "Did-you-mean / 1–N list still on screen — confirm → action=pick; reject "
    "with new clues → action=search a NEW title not in those candidates; bare "
    "nah/no/nope/nee → NEVER pick — search another or clarify. "
    "last_bot_reply may be 'Did you mean Title (year)?' — that row is "
    "still pending. Confirmations that accept the on-screen offer (yes/yep/yeah/"
    "ja/duh/1, or any short accept/bring/get/download/queue of 'it'/'that'/the "
    "offered title) → action=pick with indices=[1] (or the matching candidate). "
    "Reject phrases never confirm. Do NOT ask which movie when a single "
    "candidate is already on screen and they confirmed. "
    "When candidates are listed: pick/pick_many with 1-based indices, OR "
    "action=search with search_title that MATCHES a candidate title (or the "
    "user's own words). NEVER invent a title that is not a candidate and not "
    "grounded in the user message (e.g. do not turn Christophers+McKellen into "
    "Christopher Guest). If the user adds actor/plot/year clues that point to "
    "a DIFFERENT title than the listed candidates, action=search that resolved "
    "catalog title (+ year + people) — do not re-pick wrong substring hits. "
    "If several candidates remain and still fit, action=clarify — the bot will "
    "list them as 1–N with real Title (year) rows. NEVER say 'reply 1–N' unless "
    "candidates has at least 2 rows. "
    "When candidates are empty: for plot/appearance/vibe/description asks "
    "(guy with…, weird…, about a…), return action=search with your ONE best "
    "catalog guess as search_title (+ year + media_kind + people when known). "
    "The bot will ASK the user to confirm that guess before queueing — do not "
    "invent a numbered 1–N menu, and do NOT answer with 'send the title if you "
    "know it' or 'want another in that vibe' when they already gave a "
    "plot/appearance clue. Do not echo the plot as search_title. Do not reuse "
    "subject_title when the user clearly named or described a different title. "
    "Follow-ups that ask YOU to pick/find another title in the recent vibe "
    "('another one', 'do you know another', 'another horror in space', "
    "'I don't know, find one', 'surprise me', 'more like that', "
    "'we already have that, find another') are media asks: "
    "always action=search with a NEW catalog title that is NOT in "
    "rejected_titles and not the last queued/subject/candidate title. Never "
    "answer with 'send the title if you know it' — the user is asking you to "
    "guess. "
    "When the user names a title (even prefixed with download/queue/get/bring/"
    "add), action=search that catalog title, or pick if it matches a candidate. "
    "When the user asks you to find/match/confirm a previously stated title "
    "('find a title that matches that', 'zoek die', 'that one', 'deze') and "
    "recent_history / subject_title / pending_query already name one, "
    "action=search THAT prior catalog title (+ year when known) — never "
    "action=clarify with 'any year, actor, or other clue' when the title was "
    "already given. "
    "Bare short exact titles (even one word) → action=search that catalog "
    "title (+ year when known), or pick the matching candidate. Never reply "
    "with 'any year, actor, or other clue' or 'send an IMDb/TMDB link' for a "
    "title the user already named — catalog/TMDB run after you. "
    "Asks to list/looks-like/name a few titles that resemble a named seed "
    "('name a few that look like Land') → action=search that seed title "
    "(or clarify listing exact/prefix catalog matches for it). Never the "
    "canned clue template — they already gave the seed. "
    "Follow-ups that ask for a SHORT LIST of more titles after a genre or a "
    "queued/suggested title ('name a few more', 'a few', 'give me options', "
    "'een paar meer') → action=search with search_titles as a JSON array of "
    "2–4 DIFFERENT catalog titles in that vibe (also set search_title to the "
    "first). Never a single Did-you-mean for a list ask. "
    "Never clarify with 'send the title if you know it' when candidates, "
    "last_bot_reply, subject_title, pending_query, or recent_history already "
    "name a guessed/queued title, or when the user asked you to find/confirm/"
    "download something. "
    "Actor/artist names and misspellings are clues for you — never refuse because "
    "of spelling. If unsure which title and you have no good guess, action=clarify "
    "once with a useful question (never 'reply 1–1' / list-less 1–N). "
    "NEVER action=ignore for plot/vibe/description asks, recommend/find-one "
    "follow-ups, confirmations of an on-screen guess, titled download asks, or "
    "title typos "
    "(e.g. 'coolest sci-fi you can fins', 'old horror movie on a spaceship') — "
    "always action=search or pick so the bot can queue or ask yes/no. Ignore is "
    "only for pure chatter/emoji/meta with no media ask. "
    "When the user says the current/recent download did not work, stalled, failed, "
    "or asks to try another source / get a new one / retry that title "
    "(NL+EN: 'this download didn't work', 'try another source', 'get a new one', "
    "'probeer een andere bron', 'download werkt niet'), action=retry. "
    "Set search_title to that SAME title when known (subject_title / recent_history / "
    "queued title); leave search_title empty to retry the active tracked download. "
    "Retry is NOT a new movie search and NOT Overseerr re-request — it blocklists the "
    "bad release and grabs an alternate indexer for the same title. "
    "Actions: passthrough, ignore, clarify, pick, pick_many, search, retry. "
    "search sets search_title (and year when known; select_all=true for whole "
    "series/trilogy). Catalog year wins later — still include your best year. "
    "When the user names people/actors, also set people to a JSON array of "
    "those names (clues for you — still strip them from search_title). "
    "search_title must be the catalog title only — strip trailing 'with Actor' / "
    "'featuring' / 'starring' / 'met …' clauses (those are clues for you, not "
    "part of the Overseerr query). media_kind is movie or tv when known; omit "
    "when unsure (Overseerr returns both). "
    "If the user clearly names a concrete catalog title with no ambiguity, "
    "action=search with that title (or pick the matching candidate)."
)

SOFT_CONTEXT_CLARIFY = (
    "Want another in that vibe? Any year, actor, or other clue?"
)
# Pending/history already in play, but the user did not ask for "another".
CONTEXT_CLUE_CLARIFY = "Any year, actor, or other clue?"
EMPTY_TITLE_CLARIFY = (
    "Which movie or series did you mean? Send the title if you know it."
)


@dataclass
class IntentDecision:
    action: IntentAction = "passthrough"
    indices: list[int] = field(default_factory=list)
    search_title: str = ""
    search_titles: list[str] = field(default_factory=list)
    year: int | None = None
    select_all: bool = False
    media_kind: str = ""
    people: list[str] = field(default_factory=list)
    clarify_question: str = ""
    confidence: float = 0.0
    source: str = "heuristic"


_EMOJI_ONLY = re.compile(
    r"^[\s"
    r"\U0001F300-\U0001FAFF"
    r"\U00002700-\U000027BF"
    r"\U0001F000-\U0001F02F"
    r"\U00002600-\U000026FF"
    r"\U0000FE00-\U0000FE0F"
    r"\U0000200D"
    r"\U00002122"
    r"\U0000231A-\U0000231B"
    r"\U000023E9-\U000023F3"
    r"\U000025AA-\U000025FE"
    r"\U00002B50"
    r"\U0001F1E0-\U0001F1FF"
    r"!?.…,~*\-_/\\'\"()]+$"
)


def looks_like_chatter(text: str) -> bool:
    """True for emoji/reactions/meta chatter that must not ask 'which movie'."""
    raw = (text or "").strip()
    if not raw:
        return True
    if len(raw) <= 2 and not re.search(r"[A-Za-zÀ-ÿ0-9]", raw):
        return True
    if _EMOJI_ONLY.match(raw) and not re.search(r"[A-Za-zÀ-ÿ0-9]", raw):
        return True
    lowered = re.sub(r"\s+", " ", raw).strip().lower()
    # Pure acknowledgements / thanks — not catalog asks.
    # Note: yes/nee/no stay out — they need conversation context (lists / rejects).
    chatter = {
        "ok",
        "okay",
        "k",
        "kk",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "lol",
        "haha",
        "cool",
        "nice",
        "great",
        "bot too dumb",
        "te dom",
    }
    if lowered in chatter:
        return True
    # Meta / self-talk without a title ask (keep short — model still sees longer plots).
    if len(lowered) <= 80 and not _YEAR_PAREN.search(raw) and not _CATALOG_ID.search(raw):
        meta_bits = (
            "ga ik fixen",
            "ik ga fixen",
            "jaartallen kloppen",
            "bot te",
            "too dumb",
            "niet slim",
            "kan geen gesprek",
            "hold a conversation",
        )
        if any(bit in lowered for bit in meta_bits):
            return True
    return False


def looks_like_concrete_title(text: str) -> bool:
    """True for short near-exact title asks (not plot sentences)."""
    raw = (text or "").strip()
    if not raw or looks_like_chatter(raw):
        return False
    # Confirmations / bare rejects are not catalog titles.
    if looks_like_confirm_yes(raw):
        return False
    if looks_like_confirm_no(raw):
        return False
    if re.fullmatch(
        r"(?:n+e+e+|n+o+|nope|nah|niet|no\s+thanks)\s*[.!?]*",
        raw,
        flags=re.I,
    ):
        return False
    # "another one" / "find one" are recommend asks — never treat as a title.
    if looks_like_recommend_ask(raw):
        return False
    # Anaphoric find/match follow-ups refer to a prior title — conversation hop.
    if re.search(
        r"\b(?:find|match|zoek|resolve|confirm)\b.{0,48}\b(?:that|it|this|die|dat|deze)\b",
        raw,
        re.I,
    ):
        return False
    if _CATALOG_ID.search(raw) or _URLISH.search(raw):
        return False
    if is_explicit_title_year(raw):
        return True
    # Strip quality tokens for length checks.
    cleaned = re.sub(
        r"\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|dv|dolby\s*vision)\b",
        " ",
        raw,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    # Cast clauses are clues, not part of the title length budget.
    cleaned = re.sub(
        r"\b(?:with|featuring|starring|feat\.?|ft\.?|met)\s+.+$",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    # "That movie with …" / "the film …" shells are plot asks, not titles.
    if re.fullmatch(
        r"(?:(?:that|this|the|a|an|die|deze|dat|een)\s+)*"
        r"(?:movie|film|films|series|show|one|ones)?",
        cleaned,
        flags=re.I,
    ):
        return False
    words = cleaned.split()
    if len(words) > 8 or len(cleaned) > 80:
        return False
    # Plot / descriptive / vibe cues → conversation hop, not catalog-first.
    descriptive = re.compile(
        r"\b("
        r"about|waar|waarin|film\s+met|movie\s+about|series\s+about|"
        r"die\s+film|zoek|looking\s+for|someone\s+who|iemand\s+die|"
        r"puzzel|spiegel|tovenaar|wizard|harige|voeten|"
        r"coolest|oldest|newest|classic\s+\w+\s+movie|"
        r"old\s+\w+\s+movie|horror\s+movie|sci-?fi|"
        r"spaceship|space\s+ship|you\s+can\s+f(?:i)?n[ds]|"
        r"movie\s+on\s+a|film\s+on\s+a|on\s+a\s+spaceship|"
        r"bring\s+it|download\s+it|queue\s+it|get\s+it|"
        r"name\s+a\s+few|look(?:s)?\s+like|similar\s+to|"
        r"a\s+few\s+that|lijkt\s+op|een\s+paar"
        r")\b",
        re.I,
    )
    if descriptive.search(cleaned) or descriptive.search(raw):
        return False
    # Imperative confirm/download of "it/that" — not a title name.
    if re.search(
        r"\b(?:sure|ok|okay|ja|yes)\b.+\b(?:bring|download|queue|get|doe)\b",
        raw,
        re.I,
    ):
        return False
    # "Yes… duh" / "yes please" — yes + punctuation/commentary, not a title.
    if re.match(r"^\s*y+e+s+[.…!?,:]+", raw, re.I):
        return False
    if re.match(
        r"^\s*y+e+s+\s+(?:duh|please|pls|thanks|thx)\b",
        raw,
        re.I,
    ):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]", cleaned))


def telegram_intent_model() -> str:
    """Model for the Telegram conversation hop (smart + fast, no new env key)."""
    configured = (settings.openai_model or "").strip()
    if configured and "mini" not in configured.lower():
        return configured
    return TELEGRAM_INTENT_MODEL


def is_explicit_title_year(text: str) -> bool:
    """True for unambiguous ``Title (YYYY)`` (optional trailing quality)."""
    raw = (text or "").strip()
    if not raw or len(raw) > 200:
        return False
    if _CATALOG_ID.search(raw) or _URLISH.search(raw):
        return False
    match = _YEAR_PAREN.search(raw)
    if not match:
        return False
    # Year must be the disambiguator — title text before the paren.
    before = raw[: match.start()].strip(" -–—|,.")
    after = raw[match.end() :].strip()
    if not before or not re.search(r"[A-Za-zÀ-ÿ]", before):
        return False
    # Allow optional quality tokens after the year; reject extra clauses.
    if after and not re.fullmatch(
        r"(?:2160p|1080p|720p|480p|4k|uhd|hdr|dv|dolby\s*vision\s*)+",
        after,
        flags=re.I,
    ):
        return False
    return True


def looks_like_named_title_year(text: str) -> bool:
    """True for a specific titled ask with a year — download/Get path.

    Covers ``Title (YYYY)``, ``Title 2026 film``, and ``the 2026 film Title``.
    Genre browse / plot sentences stay False.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return False
    if is_explicit_title_year(raw):
        return True
    if looks_like_list_ask(raw) or looks_like_recommend_ask(raw):
        return False
    if looks_like_chatter(raw) or looks_like_confirm_yes(raw) or looks_like_confirm_no(raw):
        return False
    from hearth.telegram.parse import strip_title_year_media

    stripped, year = strip_title_year_media(raw)
    if year is None or not stripped:
        return False
    # Need a real title left after stripping year/media words.
    if not re.search(r"[A-Za-zÀ-ÿ]", stripped):
        return False
    if len(stripped.split()) > 8:
        return False
    return looks_like_concrete_title(stripped) or looks_like_concrete_title(raw)


def looks_like_confirm_yes(text: str) -> bool:
    """True for short yes / that's it confirmations of a single guess.

    Accepts elongated enthusiasm (yesss, yeahhh, jaaa), ``Yes... duh``,
    and 👍 when a 1-item guess-confirm is pending. Multi-word accepts like
    ``Sure. Bring it`` stay False so they go through the model tool loop.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return False
    if looks_like_confirm_no(raw):
        return False
    if _CONFIRM_THUMBS.match(raw):
        return True
    if _CONFIRM_YES.match(raw):
        return True
    # "Yes... duh" / "yes please" — yes + light commentary, still a confirm.
    if re.match(
        r"^\s*y+e+s+\s*[.…!?,:]*\s*(?:duh|please|pls|thanks|thx)?\s*[.!?]*\s*$",
        raw,
        re.I,
    ):
        return True
    if re.match(r"^\s*duh\s*[.!?]*\s*$", raw, re.I):
        return True
    return False

def looks_like_confirm_no(text: str) -> bool:
    """True for short reject of a single on-screen Did-you-mean / list offer.

    Nah / no / nope / nee / not that / anders clear pending and must NEVER queue.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return False
    return bool(_CONFIRM_NO.match(raw))


def looks_like_list_ask(text: str) -> bool:
    """True when the user wants a short list of options, not one Did-you-mean."""
    raw = (text or "").strip()
    if not raw or len(raw) > 160:
        return False
    if looks_like_chatter(raw) or looks_like_confirm_yes(raw) or looks_like_confirm_no(raw):
        return False
    return bool(_LIST_ASK.search(raw))


def looks_like_recommend_ask(text: str) -> bool:
    """True when the user asks the bot to find/pick another title (not name one)."""
    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return False
    if looks_like_chatter(raw) or looks_like_confirm_yes(raw) or looks_like_confirm_no(raw):
        return False
    if looks_like_list_ask(raw):
        return True
    return bool(_RECOMMEND_ASK.search(raw))


def looks_like_media_ask(text: str) -> bool:
    """True for title/plot/vibe asks that must never be silently ignored."""
    raw = (text or "").strip()
    if not raw or looks_like_chatter(raw):
        return False
    if looks_like_recommend_ask(raw):
        return True
    if _CATALOG_ID.search(raw) or _URLISH.search(raw) or is_explicit_title_year(raw):
        return True
    if looks_like_concrete_title(raw):
        return True
    # Plot / vibe / descriptive (incl. typos) — anything that is not chatter
    # and has enough substance to be a media request.
    if len(raw) >= 10 and re.search(r"[A-Za-zÀ-ÿ]", raw):
        return True
    return False


def history_has_named_title(history: list[dict[str, Any]] | None) -> bool:
    """True when recent turns already named or offered a title."""
    for turn in history or []:
        if str(turn.get("search_title") or "").strip():
            return True
        if turn.get("offered"):
            return True
        text = str(turn.get("text") or "")
        if re.search(r"did you mean\b", text, re.I):
            return True
        if re.search(r"queued\b", text, re.I):
            return True
    return False


def has_guess_context(
    *,
    candidate_count: int = 0,
    pending_query: str = "",
    last_bot_reply: str = "",
    subject_title: str = "",
    history: list[dict[str, Any]] | None = None,
    rejected_titles: list[str] | None = None,
) -> bool:
    """True when a guessed/named title is already in play — never demand one."""
    if candidate_count > 0:
        return True
    if (pending_query or "").strip() or (subject_title or "").strip():
        return True
    if (last_bot_reply or "").strip():
        return True
    if rejected_titles:
        return True
    return history_has_named_title(history)


def _default_clarify_question(
    candidate_count: int,
    *,
    has_context: bool = False,
    recommend: bool = False,
) -> str:
    """Never emit list-less 1–N wording when fewer than 2 candidates exist.

    Recommend asks keep the soft vibe template. Other context (pending guess,
    history, plot clue) must NOT become "want another in that vibe" — that
    fires only when they actually asked for another.
    """
    if candidate_count >= 2:
        return (
            f"Which one — reply 1–{min(3, candidate_count)}, "
            "'all of them', or a clearer title?"
        )
    if recommend:
        return SOFT_CONTEXT_CLARIFY
    if has_context:
        return CONTEXT_CLUE_CLARIFY
    return EMPTY_TITLE_CLARIFY


def _sanitize_clarify_question(
    question: str,
    *,
    candidate_count: int,
    has_context: bool,
    recommend: bool,
) -> str:
    """Replace the empty-title / vibe templates when they do not fit."""
    raw = (question or "").strip()
    lowered = raw.lower()
    if "send the title if you know it" in lowered and (
        recommend or has_context or candidate_count > 0
    ):
        return _default_clarify_question(
            candidate_count, has_context=True, recommend=recommend
        )
    # Model (or fallback) echoed the recommend vibe template without a recommend ask.
    if "want another in that vibe" in lowered and not recommend:
        return _default_clarify_question(
            candidate_count, has_context=has_context or True, recommend=False
        )
    return raw


def _year_from_candidate(row: dict[str, Any]) -> int | None:
    year = row.get("year")
    try:
        year_i = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if year_i is not None and 1900 <= year_i <= 2100:
        return year_i
    return None


def instant_pick_decision(
    text: str,
    candidates: list[dict[str, Any]] | None,
) -> IntentDecision | None:
    """Live numbered-list / single-guess confirm shortcuts while options are on screen."""
    raw = (text or "").strip()
    if not raw or not candidates:
        return None
    n = len(candidates)
    if n == 0:
        return None

    digit = _ORDINAL_PICK.match(raw)
    if digit:
        idx = int(digit.group(1))
        if 1 <= idx <= n:
            return IntentDecision(
                action="pick",
                indices=[idx],
                confidence=1.0,
                source="instant",
            )
        return IntentDecision(
            action="clarify",
            clarify_question=(
                f"Pick a number from 1–{min(3, n)} (or say 'all of them')."
                if n >= 2
                else "Did you mean that title? Reply yes or send another clue."
            ),
            confidence=0.9,
            source="instant",
        )

    # Single on-screen guess: yes / that's it → queue it.
    # Rejects are never confirm — leave them for the inbox reject path / model.
    if n == 1 and looks_like_confirm_no(raw):
        return None
    if n == 1 and looks_like_confirm_yes(raw):
        return IntentDecision(
            action="pick",
            indices=[1],
            confidence=1.0,
            source="instant",
        )

    if _FOLLOWUP_ALL.search(raw) and len(raw) <= 40:
        return IntentDecision(
            action="pick_many",
            indices=list(range(1, min(n, MAX_BATCH) + 1)),
            select_all=True,
            confidence=1.0,
            source="instant",
        )

    if _FOLLOWUP_FIRST.match(raw):
        oldest = min(
            range(n),
            key=lambda i: _year_sort_key(candidates[i]) or 9999,
        )
        return IntentDecision(
            action="pick",
            indices=[oldest + 1],
            confidence=1.0,
            source="instant",
        )

    return None


def _year_sort_key(row: dict[str, Any]) -> int:
    year = row.get("year")
    try:
        return int(year)
    except (TypeError, ValueError):
        return 0


def _candidate_blob(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(candidates[:MAX_CANDIDATES], start=1):
        rows.append(
            {
                "i": idx,
                "title": str(row.get("title") or "")[:120],
                "year": row.get("year"),
                "tmdbId": row.get("tmdbId") or row.get("mediaId"),
                "tvdbId": row.get("tvdbId"),
                "mediaType": row.get("mediaType") or row.get("media_kind") or "",
            }
        )
    return rows


def _norm_title(value: str) -> str:
    text = re.sub(r"[^a-z0-9à-ÿ]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _significant_tokens(value: str) -> set[str]:
    return {
        tok
        for tok in _norm_title(value).split()
        if len(tok) >= 3 and tok not in _STOP_TOKENS and not tok.isdigit()
    }


def titles_match(left: str, right: str) -> bool:
    """Loose title equality for candidate / subject checks."""
    a = _norm_title(left)
    b = _norm_title(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Drop leading articles once more after year stripping.
    a2 = re.sub(r"^(?:the|a|an|de|het|een)\s+", "", a)
    b2 = re.sub(r"^(?:the|a|an|de|het|een)\s+", "", b)
    return bool(a2 and b2 and a2 == b2)


def search_title_grounded(
    search_title: str,
    *,
    user_message: str,
    candidates: list[dict[str, Any]] | None,
) -> bool:
    """True when search_title matches a candidate or shares tokens with the user.

    Blocks invented titles like ``The Christopher Guest Movies`` when the user
    said Christophers + McKellen and candidates contain The Christophers.
    Plot asks with empty candidates may invent a catalog title. Concrete title
    asks must still share tokens with the user message (Da Vinci must not
    survive a Christophers ask).

    Recommend / find-another asks may invent a NEW catalog title that
    intentionally avoids the on-screen candidates (those are the offer being
    rejected) — still grounded for the inbox guess-and-ask path.
    """
    title = (search_title or "").strip()
    if not title:
        return False
    rows = list(candidates or [])
    if any(titles_match(title, str(row.get("title") or "")) for row in rows):
        return True
    user_tokens = _significant_tokens(user_message)
    title_tokens = _significant_tokens(title)
    shared = bool(user_tokens and title_tokens and (user_tokens & title_tokens))
    if shared:
        return True
    if not rows:
        # Plot / character resolve may invent when the user did not name a title.
        if looks_like_concrete_title(user_message):
            return False
        return True
    # Live pending options are often the rejected offer on a find-another turn.
    if looks_like_recommend_ask(user_message):
        return True
    return False


def clarify_wants_numbered_list(question: str) -> bool:
    """True when a clarify string asks for 1–N without listing the rows."""
    raw = (question or "").strip()
    if not raw:
        return False
    if _CLARIFY_LIST_HINT.search(raw):
        return True
    return bool(re.search(r"\breply\s+1\s*[-–—]\s*\d+\b", raw, re.I))


def subject_matches_user_title(subject_title: str, user_message: str) -> bool:
    """True when the sticky subject still refers to the user's current title ask."""
    subject = (subject_title or "").strip()
    if not subject:
        return True
    if titles_match(subject, user_message):
        return True
    subj_tokens = _significant_tokens(subject)
    user_tokens = _significant_tokens(user_message)
    if not subj_tokens or not user_tokens:
        return False
    return bool(subj_tokens & user_tokens)


def _offline_fallback(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None,
    rejected_titles: list[str] | None,
) -> IntentDecision:
    """No API key: never invent titles from plots — ignore chatter, else clarify."""
    raw = (text or "").strip()
    if not raw or looks_like_chatter(raw):
        return IntentDecision(action="ignore", confidence=1.0, source="offline")
    instant = instant_pick_decision(raw, candidates)
    if instant is not None:
        return instant
    if candidates:
        return IntentDecision(
            action="clarify",
            clarify_question=(
                "Which movie did you mean? Send the title, or reply with a number "
                f"from the list (1–{min(3, len(candidates))})."
            ),
            confidence=0.4,
            source="offline",
        )
    rejected = ", ".join((rejected_titles or [])[:4])
    extra = f" (not {rejected})" if rejected else ""
    return IntentDecision(
        action="clarify",
        clarify_question=(
            f"Which movie or series did you mean{extra}? "
            "Send the title if you know it, or a bit more detail."
        ),
        confidence=0.4,
        source="offline",
    )


async def interpret_intent(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    pending_query: str = "",
    last_bot_reply: str = "",
    force: bool = False,
    history: list[dict[str, Any]] | None = None,
    subject_title: str = "",
    subject_media_kind: str = "",
    rejected_titles: list[str] | None = None,
    candidates_are_pending: bool = False,
) -> IntentDecision:
    """Interpret a user message. Always uses the model when configured.

    ``force`` is retained for callers; non-empty text with history/candidates/
    conversational content always hits the model. Instant catalog/year/pick
    shortcuts are decided by the inbox *before* calling this.
    """
    del force  # inbox decides instant vs AI; this hop always models when keyed
    raw = (text or "").strip()
    if not raw:
        return IntentDecision(action="ignore", confidence=1.0)

    # Clear chatter never needs a model hop — empty reply, no "which movie".
    # Skip when candidates are on screen (yes/1/all still need that context).
    if looks_like_chatter(raw) and not candidates:
        return IntentDecision(action="ignore", confidence=1.0, source="chatter")

    # Live-list shortcuts stay local even when the model is available — they are
    # the instant path (1/2/3, all of them, de eerste).
    instant = instant_pick_decision(raw, candidates)
    if instant is not None and instant.action in {"pick", "pick_many"}:
        return instant

    if not settings.openai_configured:
        return _offline_fallback(
            raw, candidates=candidates, rejected_titles=rejected_titles
        )

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        rejected = [
            redact(str(t))[:120]
            for t in (rejected_titles or [])
            if str(t).strip()
        ][:12]
        payload = {
            "user_message": redact(raw)[:240],
            "pending_query": redact(pending_query)[:120] if pending_query else "",
            "last_bot_reply": redact(last_bot_reply)[:400] if last_bot_reply else "",
            "candidates": _candidate_blob(candidates or []),
            "candidates_are_live_pending": bool(candidates_are_pending),
            "rejected_titles": rejected,
            "recent_history": history or [],
            "subject_title": redact(subject_title)[:120] if subject_title else "",
            "subject_media_kind": (
                subject_media_kind if subject_media_kind in {"movie", "tv"} else ""
            ),
        }
        model = telegram_intent_model()
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
            # Candidates + history need headroom; gpt-4o is cheap for this hop.
            max_tokens=450,
            temperature=0,
        )
        drafted = (response.choices[0].message.content or "").strip()
        ctx = has_guess_context(
            candidate_count=len(candidates or []),
            pending_query=pending_query,
            last_bot_reply=last_bot_reply,
            subject_title=subject_title,
            history=history,
            rejected_titles=rejected_titles,
        )
        parsed = _parse_model_json(
            drafted,
            candidate_count=len(candidates or []),
            rejected_titles=rejected,
            user_message=raw,
            candidates=candidates,
            has_context=ctx,
            candidates_are_pending=candidates_are_pending,
        )
        if parsed is None:
            return IntentDecision(
                action="clarify",
                clarify_question=_default_clarify_question(
                    len(candidates or []),
                    has_context=ctx,
                    recommend=looks_like_recommend_ask(raw),
                ),
                confidence=0.4,
                source="openai_fallback",
            )
        parsed.source = "openai"
        return parsed
    except Exception as exc:  # noqa: BLE001
        log.info("telegram intent openai failed: %s", redact(str(exc)))
        return _offline_fallback(
            raw, candidates=candidates, rejected_titles=rejected_titles
        )


def _parse_model_json(
    raw: str,
    *,
    candidate_count: int,
    rejected_titles: list[str] | None = None,
    user_message: str = "",
    candidates: list[dict[str, Any]] | None = None,
    has_context: bool = False,
    candidates_are_pending: bool = False,
) -> IntentDecision | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "passthrough").strip().lower()
    if action not in {
        "passthrough",
        "ignore",
        "clarify",
        "pick",
        "pick_many",
        "search",
        "retry",
    }:
        return None
    indices_raw = data.get("indices") or data.get("picks") or []
    indices: list[int] = []
    if isinstance(indices_raw, list):
        for item in indices_raw:
            try:
                num = int(item)
            except (TypeError, ValueError):
                continue
            if candidate_count and 1 <= num <= candidate_count:
                indices.append(num)
            elif not candidate_count and 1 <= num <= MAX_BATCH:
                indices.append(num)
    seen: set[int] = set()
    uniq: list[int] = []
    for num in indices:
        if num not in seen:
            seen.add(num)
            uniq.append(num)
    indices = uniq[:MAX_BATCH]

    conf = data.get("confidence")
    try:
        confidence = float(conf) if conf is not None else 0.6
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))

    search_title = str(data.get("search_title") or data.get("title") or "").strip()[:200]
    search_titles: list[str] = []
    titles_raw = data.get("search_titles") or data.get("alt_titles") or []
    if isinstance(titles_raw, str):
        titles_raw = [titles_raw]
    if isinstance(titles_raw, list):
        for item in titles_raw:
            title = str(item or "").strip()[:200]
            if title and title not in search_titles:
                search_titles.append(title)
            if len(search_titles) >= 4:
                break
    if search_title and search_title not in search_titles:
        search_titles.insert(0, search_title)
    select_all = bool(data.get("select_all"))
    clarify = str(data.get("clarify_question") or data.get("question") or "").strip()[:400]
    media_kind = str(data.get("media_kind") or data.get("kind") or "").strip().lower()
    if media_kind not in {"movie", "tv"}:
        media_kind = ""
    year_raw = data.get("year")
    year: int | None = None
    if year_raw not in (None, ""):
        try:
            year_i = int(year_raw)
            if 1900 <= year_i <= 2100:
                year = year_i
        except (TypeError, ValueError):
            year = None
    people: list[str] = []
    people_raw = data.get("people") or data.get("actors") or data.get("cast") or []
    if isinstance(people_raw, str):
        people_raw = [people_raw]
    if isinstance(people_raw, list):
        for item in people_raw:
            name = str(item or "").strip()
            if name and name not in people:
                people.append(name[:80])
            if len(people) >= 6:
                break

    rejected_norm = {
        re.sub(r"\s+", " ", t).strip().lower()
        for t in (rejected_titles or [])
        if str(t).strip()
    }

    # Bare reject of an on-screen offer must never become pick/queue.
    user_rejects = looks_like_confirm_no(user_message)
    if user_rejects and action in {"pick", "pick_many"}:
        action = "search" if search_title else "clarify"
        indices = []
        if action == "clarify" and not clarify:
            clarify = "Ok — what should I grab instead?"

    if action in {"pick", "pick_many"} and not indices and select_all and candidate_count:
        indices = list(range(1, min(candidate_count, MAX_BATCH) + 1))
        action = "pick_many"
    if action == "pick" and len(indices) > 1:
        action = "pick_many"
    if action == "pick" and not indices:
        # Unique candidate on screen → treat as that pick (never list-less 1–1).
        # Rejects never auto-pick the rejected row.
        if candidate_count == 1 and not user_rejects:
            indices = [1]
        else:
            return IntentDecision(
                action="clarify",
                clarify_question=clarify
                or _default_clarify_question(
                    candidate_count,
                    has_context=has_context,
                    recommend=looks_like_recommend_ask(user_message),
                ),
                confidence=confidence,
                people=people,
                search_titles=search_titles,
            )
    if action == "pick_many" and not indices:
        return IntentDecision(
            action="clarify",
            clarify_question=clarify or "Which titles should I queue?",
            confidence=confidence,
            people=people,
            search_titles=search_titles,
        )
    recommend = looks_like_recommend_ask(user_message) or looks_like_list_ask(
        user_message
    )
    # Pending/history already named a title → never demand one from the user.
    ctx = bool(has_context or candidate_count > 0 or recommend)
    fallback_clarify = _default_clarify_question(
        candidate_count, has_context=ctx, recommend=recommend
    )

    if action == "search":
        if not search_title and search_titles:
            search_title = search_titles[0]
        if not search_title:
            return IntentDecision(
                action="clarify",
                clarify_question=clarify or fallback_clarify,
                confidence=confidence,
                media_kind=media_kind,
                people=people,
                search_titles=search_titles,
            )
        if search_title.strip().lower() in rejected_norm:
            # Queued titles are remembered as rejected so "another one" won't
            # re-offer them — but a concrete same-title re-ask / actor-year
            # refinement ("Land with robin wright") must still proceed.
            reask = titles_match(search_title, user_message) or bool(
                _significant_tokens(search_title) & _significant_tokens(user_message)
            )
            if not reask:
                return IntentDecision(
                    action="clarify",
                    clarify_question=clarify or fallback_clarify,
                    confidence=confidence,
                    media_kind=media_kind,
                    people=people,
                    search_titles=search_titles,
                )
        if not search_title_grounded(
            search_title,
            user_message=user_message,
            candidates=candidates,
        ):
            # Invented title while real THIS-TURN catalog hits exist → clarify.
            # Live pending options are the on-screen offer: a different
            # search_title is a reject/new-ask pivot (recommend or clue update).
            # Actor/plot refinements that name a title absent from fuzzy hits
            # also pivot — do not force La La Land when the model said Land.
            # Single Did-you-mean pending: ungrounded different title on a
            # confirm turn must NOT invent La La Land — pick the on-screen row.
            # Only pivot when the USER message itself grounds the new title
            # (shared tokens), a recommend ask, or an actor/year they typed.
            # Bare reject is NOT a confirm — never force-pick the rejected row.
            user_grounds_new = bool(
                _significant_tokens(user_message)
                & _significant_tokens(search_title)
            )
            if (
                candidates_are_pending
                and candidate_count == 1
                and not recommend
                and not looks_like_recommend_ask(user_message)
                and not looks_like_list_ask(user_message)
                and not user_rejects
                and not user_grounds_new
            ):
                return IntentDecision(
                    action="pick",
                    indices=[1],
                    confidence=confidence,
                    media_kind=media_kind
                    or str(
                        (candidates or [{}])[0].get("mediaType")
                        or (candidates or [{}])[0].get("media_kind")
                        or ""
                    ),
                    people=people,
                    search_title=str((candidates or [{}])[0].get("title") or "")[
                        :200
                    ],
                    year=_year_from_candidate((candidates or [{}])[0]),
                    search_titles=search_titles,
                )
            allow_pending_pivot = (
                candidates_are_pending
                and search_title.strip().lower() not in rejected_norm
                and not any(
                    titles_match(search_title, str(row.get("title") or ""))
                    for row in (candidates or [])
                )
                and (
                    recommend
                    or looks_like_recommend_ask(user_message)
                    or user_rejects
                    or user_grounds_new
                    or (
                        bool(people)
                        and any(
                            p.lower() in user_message.lower()
                            for p in people
                            if p
                        )
                    )
                    or (year is not None and str(year) in user_message)
                )
            )
            allow_clue_pivot = (
                (bool(people) or bool(year) or not looks_like_concrete_title(user_message))
                and search_title.strip().lower() not in rejected_norm
                and not any(
                    titles_match(search_title, str(row.get("title") or ""))
                    for row in (candidates or [])
                )
            )
            if not (
                allow_pending_pivot
                or allow_clue_pivot
                or (
                    recommend
                    and search_title.strip().lower() not in rejected_norm
                )
            ):
                return IntentDecision(
                    action="clarify",
                    clarify_question=clarify or fallback_clarify,
                    confidence=confidence,
                    media_kind=media_kind,
                    people=people,
                    search_titles=search_titles,
                )
        if confidence < MIN_RESOLVE_CONFIDENCE:
            # Recommend/find-one: keep the guess so inbox can ask "Did you mean …?".
            if not (recommend and search_title):
                return IntentDecision(
                    action="clarify",
                    clarify_question=clarify or fallback_clarify,
                    confidence=confidence,
                    media_kind=media_kind,
                    people=people,
                    search_titles=search_titles,
                )

    # Plot/vibe/title asks must never be silently ignored (live: "coolest sci-fi
    # you can fins" stored no bot reply). Promote to search/clarify instead.
    if action == "ignore" and looks_like_media_ask(user_message):
        if search_title and search_title.strip().lower() not in rejected_norm:
            action = "search"
        else:
            action = "clarify"
            clarify = clarify or _default_clarify_question(
                candidate_count, has_context=ctx, recommend=recommend
            )

    # Ban list-less "reply 1–N" / "1–1" forever when fewer than 2 real candidates.
    # Live VAULT: offered was empty but the default template still fired with
    # candidate_count=1 (or the model echoed "reply 1–1"). Prefer guess→ask.
    if action == "clarify":
        # Bare concrete title ask + canned/empty clarify → search that title.
        # Never leave "Any year, actor…" as the first reply for "Land".
        # Do NOT promote when a live pending list/guess is on screen (pick path),
        # or when the user is confirming/rejecting rather than naming a title.
        canned_clarify = (not clarify) or any(
            bit in (clarify or "").lower()
            for bit in (
                "any year, actor, or other clue",
                "want another in that vibe",
                "send the title if you know it",
            )
        ) or clarify_wants_numbered_list(clarify or "")
        if (
            canned_clarify
            and not candidates_are_pending
            and looks_like_concrete_title(user_message)
            and not (search_title or "").strip()
        ):
            seeded = catalog_search_title(user_message) or user_message.strip()
            if seeded:
                search_title = seeded[:200]
                action = "search"
                clarify = ""
        # Model echoed the canned clue template while a candidate/guess exists
        # — promote to search that row, never leave the template.
        if action == "clarify" and clarify and not candidates_are_pending:
            lowered_q = clarify.lower()
            if (
                "any year, actor, or other clue" in lowered_q
                or "want another in that vibe" in lowered_q
            ):
                if looks_like_concrete_title(user_message):
                    seeded = (
                        catalog_search_title(user_message) or user_message.strip()
                    )
                    if seeded:
                        search_title = seeded[:200]
                        action = "search"
                        clarify = ""
                elif (
                    search_title
                    and search_title.strip().lower() not in rejected_norm
                ):
                    action = "search"
                    clarify = ""
                elif candidate_count == 1 and candidates:
                    phantom_title = str(
                        (candidates or [{}])[0].get("title") or ""
                    ).strip()
                    if (
                        phantom_title
                        and phantom_title.lower() not in rejected_norm
                    ):
                        search_title = phantom_title[:200]
                        if year is None:
                            year = _year_from_candidate((candidates or [{}])[0])
                        action = "search"
                        clarify = ""
        listless = (not clarify) or clarify_wants_numbered_list(clarify)
        if action == "clarify" and candidate_count < 2 and listless:
            phantom = None
            rows = list(candidates or [])
            if len(rows) == 1:
                phantom = rows[0]
            usable_guess = (
                search_title
                and search_title.strip().lower() not in rejected_norm
            )
            if usable_guess:
                # Have a guess — inbox will ask "Did you mean …?" before queueing.
                action = "search"
                clarify = ""
            elif phantom and str(phantom.get("title") or "").strip():
                phantom_title = str(phantom.get("title") or "").strip()[:200]
                if phantom_title.strip().lower() not in rejected_norm:
                    search_title = phantom_title
                    if year is None:
                        year = _year_from_candidate(phantom)
                    kind = str(
                        phantom.get("mediaType") or phantom.get("media_kind") or ""
                    ).strip().lower()
                    if kind in {"movie", "tv"}:
                        media_kind = kind
                    action = "search"
                    clarify = ""
                else:
                    clarify = _default_clarify_question(
                        0, has_context=ctx, recommend=recommend
                    )
            else:
                # Recommend/find-one / in-context must never demand the user
                # already knows the title (live empty-title fallback).
                clarify = _default_clarify_question(
                    0, has_context=ctx, recommend=recommend
                )
        elif not clarify:
            clarify = _default_clarify_question(
                candidate_count, has_context=ctx, recommend=recommend
            )
        elif clarify_wants_numbered_list(clarify) and candidate_count < 2:
            clarify = _default_clarify_question(
                0, has_context=ctx, recommend=recommend
            )

    # Absolute last line of defense: never return list-less 1–N wording.
    if clarify and clarify_wants_numbered_list(clarify) and candidate_count < 2:
        if search_title and search_title.strip().lower() not in rejected_norm:
            action = "search"
            clarify = ""
        else:
            clarify = _default_clarify_question(
                0, has_context=ctx, recommend=recommend
            )

    # Never leave the empty-title "if you know it" template when context exists.
    if action == "clarify":
        clarify = _sanitize_clarify_question(
            clarify,
            candidate_count=candidate_count,
            has_context=ctx,
            recommend=recommend,
        )
        if (
            "send the title if you know it" in (clarify or "").lower()
            and search_title
            and search_title.strip().lower() not in rejected_norm
        ):
            action = "search"
            clarify = ""

    return IntentDecision(
        action=action,  # type: ignore[arg-type]
        indices=indices,
        search_title=search_title,
        search_titles=search_titles,
        year=year,
        select_all=select_all,
        media_kind=media_kind,
        people=people,
        clarify_question=clarify,
        confidence=confidence,
    )


# --- Compatibility shims (not used as gates; kept so older imports/tests soft-fail) ---


def looks_like_followup(text: str) -> bool:
    """Deprecated as a gate — instant picks use ``instant_pick_decision``."""
    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return False
    if _FOLLOWUP_ALL.search(raw) or _FOLLOWUP_FIRST.match(raw):
        return True
    return raw.lower() in {
        "all",
        "both",
        "yes",
        "yep",
        "yeah",
        "sure",
        "ja",
        "jawel",
        "nee",
        "no",
        "nope",
        "allemaal",
        "alles",
    }


def looks_like_contextual_followup(text: str) -> bool:
    """Deprecated as a gate."""
    raw = (text or "").strip()
    return bool(raw) and len(raw) <= 80


def looks_like_collection_request(text: str) -> bool:
    """Deprecated as a gate."""
    raw = (text or "").strip()
    return bool(raw) and bool(_FOLLOWUP_ALL.search(raw))


def looks_like_descriptive_ask(text: str) -> bool:
    """Deprecated as a gate — plots always go to the model now."""
    raw = (text or "").strip()
    return bool(raw) and len(raw) >= 12 and not is_explicit_title_year(raw)


def heuristic_intent(
    text: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    pending_query: str = "",
) -> IntentDecision:
    """Deprecated offline helper — prefer ``interpret_intent`` / instant picks."""
    del pending_query
    instant = instant_pick_decision(text, candidates)
    if instant is not None:
        return instant
    return _offline_fallback(text, candidates=candidates, rejected_titles=None)


__all__ = [
    "IntentDecision",
    "MAX_BATCH",
    "MAX_CANDIDATES",
    "MIN_RESOLVE_CONFIDENCE",
    "SOFT_CONTEXT_CLARIFY",
    "CONTEXT_CLUE_CLARIFY",
    "EMPTY_TITLE_CLARIFY",
    "TELEGRAM_INTENT_MODEL",
    "clarify_wants_numbered_list",
    "has_guess_context",
    "heuristic_intent",
    "instant_pick_decision",
    "interpret_intent",
    "is_explicit_title_year",
    "looks_like_chatter",
    "looks_like_concrete_title",
    "looks_like_confirm_no",
    "looks_like_confirm_yes",
    "looks_like_list_ask",
    "looks_like_media_ask",
    "looks_like_named_title_year",
    "looks_like_recommend_ask",
    "looks_like_collection_request",
    "looks_like_contextual_followup",
    "looks_like_descriptive_ask",
    "looks_like_followup",
    "search_title_grounded",
    "subject_matches_user_title",
    "telegram_intent_model",
    "titles_match",
]
