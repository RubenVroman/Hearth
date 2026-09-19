"""Edition / cut preference extraction for Telegram media asks."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Strip quality/cut tokens so we never search a literal "… extended edition"
# catalog title that does not exist on TMDB.
_EDITION_TOKEN = re.compile(
    r"\b(?P<label>"
    r"extended(?:\s+(?:edition|cut|version))?|"
    r"director'?s?\s+cut|"
    r"theatrical(?:\s+(?:cut|edition|version))?|"
    r"unrated(?:\s+cut)?|"
    r"ultimate(?:\s+edition)?|"
    r"special(?:\s+edition)?|"
    r"collector'?s?\s+edition|"
    r"anniversary(?:\s+edition)?|"
    r"remaster(?:ed)?|"
    r"criterion|"
    r"imax|"
    r"(?:4k|uhd|2160p|1080p|720p)|"
    r"hdr(?:10)?|"
    r"dolby\s*vision|"
    r"dv\b"
    r")\b",
    re.I,
)

_EDITION_KEY_MAP: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"extended", re.I), "extended", "extended edition"),
    (re.compile(r"director", re.I), "directors_cut", "director's cut"),
    (re.compile(r"theatrical", re.I), "theatrical", "theatrical cut"),
    (re.compile(r"unrated", re.I), "unrated", "unrated cut"),
    (re.compile(r"ultimate", re.I), "ultimate", "ultimate edition"),
    (re.compile(r"special", re.I), "special", "special edition"),
    (re.compile(r"collector", re.I), "collectors", "collector's edition"),
    (re.compile(r"anniversary", re.I), "anniversary", "anniversary edition"),
    (re.compile(r"remaster", re.I), "remastered", "remastered"),
    (re.compile(r"criterion", re.I), "criterion", "Criterion"),
    (re.compile(r"imax", re.I), "imax", "IMAX"),
    (re.compile(r"4k|uhd|2160p", re.I), "uhd", "4K/UHD"),
    (re.compile(r"1080p|720p", re.I), "hd", "HD"),
    (re.compile(r"hdr|dolby\s*vision|\bdv\b", re.I), "hdr", "HDR"),
]


@dataclass(frozen=True, slots=True)
class EditionPreference:
    """Cut / quality preference stripped from a user ask."""

    key: str
    label: str
    clean_title: str
    raw_label: str = ""

    @property
    def present(self) -> bool:
        return bool(self.key)


def extract_edition(text: str) -> EditionPreference | None:
    """Return edition preference and a clean search title, or None."""
    raw = (text or "").strip()
    if not raw:
        return None
    matches = list(_EDITION_TOKEN.finditer(raw))
    if not matches:
        return None

    key = ""
    label = ""
    raw_label = matches[0].group("label").strip()
    for pattern, mapped_key, mapped_label in _EDITION_KEY_MAP:
        if any(pattern.search(m.group("label")) for m in matches):
            key = mapped_key
            label = mapped_label
            break
    if not key:
        key = "edition"
        label = raw_label or "preferred edition"

    cleaned = _EDITION_TOKEN.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    # Drop trailing "edition" / "cut" leftovers after token removal.
    cleaned = re.sub(r"\b(?:edition|cut|version)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.")
    if len(cleaned) < 2:
        return None
    return EditionPreference(
        key=key,
        label=label,
        clean_title=cleaned,
        raw_label=raw_label,
    )


__all__ = ["EditionPreference", "extract_edition"]
