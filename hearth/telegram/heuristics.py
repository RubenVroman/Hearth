"""Shared title / confirm heuristics for the Telegram media bot."""

from __future__ import annotations

import re

_YEAR_PAREN = re.compile(r"\(\s*((?:19|20)\d{2})\s*\)")
_CATALOG_ID = re.compile(r"\btmdb\s*:\s*(?:movie|tv|film|show|series|serie\s*:)?\s*\d+", re.I)
_URLISH = re.compile(r"https?://", re.I)
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
_CONFIRM_THUMBS = re.compile(r"^\s*👍[\U0001F3FB-\U0001F3FF]?\uFE0F?\s*[.!?]*\s*$")
_CONFIRM_NO = re.compile(
    r"^\s*(?:"
    r"n+o+|n+o+p+e+|n+a+h+|nee+|neen|"
    r"niet(?:\s+die)?|not\s+that|wrong|"
    r"geen\s+van\s+(?:deze|die)|"
    r"none(?:\s+of\s+(?:these|them|em|'em))?"
    r")\s*[.!?]*\s*$",
    re.I,
)
_CAST_CLAUSE = re.compile(
    r"^(?P<title>.+?)\s+(?:with|featuring|starring|feat\.?|ft\.?|met)\s+(?P<who>.+)$",
    re.I,
)
_ARTICLE_WHO = re.compile(r"^(?:the|a|an|de|het|een)\b", re.I)
_DESCRIPTIVE = re.compile(
    r"\b("
    r"about|waar|waarin|film\s+met|movie\s+about|series\s+about|"
    r"die\s+film|deze\s+film|that\s+movie|this\s+movie|looking\s+for|"
    r"someone\s+who|iemand\s+die|guy\s+with|girl\s+with|man\s+with|"
    r"woman\s+with|boy\s+with|kid\s+with|scar|litteken|wizard|tovenaar|"
    r"puzzel|spiegel|coolest|oldest|newest|classic\s+\w+\s+movie|"
    r"old\s+\w+\s+movie|horror\s+movie|sci-?fi|spaceship|space\s+ship|"
    r"you\s+can\s+f(?:i)?n[ds]|movie\s+on\s+a|film\s+on\s+a|"
    r"on\s+a\s+spaceship|vibe|like\s+that|something\s+like|"
    r"het\s+filmpje|die\s+serie|glasses|bril"
    r")\b",
    re.I,
)
_PLOT_SHELL = re.compile(
    r"(?:(?:that|this|the|a|an|die|deze|dat|een)\s+)*"
    r"(?:movie|film|films|series|show|one|ones)?",
    re.I,
)


def looks_like_confirm_yes(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return False
    if _CONFIRM_THUMBS.match(raw):
        return True
    return bool(_CONFIRM_YES.match(raw))


def looks_like_confirm_no(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return False
    return bool(_CONFIRM_NO.match(raw))


def _looks_like_actor_clue(text: str) -> bool:
    """True for short ``Title with Person`` asks — not ``Title with the Noun`` films."""
    match = _CAST_CLAUSE.match((text or "").strip())
    if not match:
        return False
    title = match.group("title").strip(" -–—|,.")
    who = match.group("who").strip(" -–—|,.")
    if not title or not who or _ARTICLE_WHO.match(who):
        return False
    who_words = who.split()
    if not 1 <= len(who_words) <= 4:
        return False
    if _DESCRIPTIVE.search(who) or _DESCRIPTIVE.search(title):
        return False
    return len(title.split()) <= 4 and bool(re.search(r"[A-Za-zÀ-ÿ]", who))


def looks_like_concrete_title(text: str) -> bool:
    """True for short near-exact title asks (not plot/actor sentences)."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _CATALOG_ID.search(raw) or _URLISH.search(raw):
        return False
    if looks_like_confirm_yes(raw) or looks_like_confirm_no(raw):
        return False
    if _YEAR_PAREN.search(raw):
        year_match = _YEAR_PAREN.search(raw)
        assert year_match is not None
        before = raw[: year_match.start()].strip(" -–—|,.")
        if before and re.search(r"[A-Za-zÀ-ÿ]", before) and len(before.split()) <= 8:
            return True
    cleaned = re.sub(
        r"\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|dv|dolby\s*vision)\b",
        " ",
        raw,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    if _looks_like_actor_clue(cleaned):
        return False
    if _PLOT_SHELL.fullmatch(cleaned):
        return False
    words = cleaned.split()
    if len(words) > 8 or len(cleaned) > 80:
        return False
    if _DESCRIPTIVE.search(cleaned) or _DESCRIPTIVE.search(raw):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]", cleaned))


__all__ = [
    "looks_like_concrete_title",
    "looks_like_confirm_no",
    "looks_like_confirm_yes",
]
