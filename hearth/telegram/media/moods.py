"""Vibe requests → real TMDB discover coordinates (no LLM, no guessing).

"scary under 2 hours", "Friday night comfort", "kids movie" are not catalog
titles, so a literal Overseerr title search is guaranteed to miss. This module
maps house language onto the genre / runtime / era filters that
``Overseerr.discover`` actually supports.
"""

from __future__ import annotations

import re

from hearth.telegram.media.types import MoodSpec

# TMDB movie genres.
ACTION = 28
ADVENTURE = 12
ANIMATION = 16
COMEDY = 35
CRIME = 80
DOCUMENTARY = 99
DRAMA = 18
FAMILY = 10751
FANTASY = 14
HISTORY = 36
HORROR = 27
MUSIC = 10402
MYSTERY = 9648
ROMANCE = 10749
SCIFI = 878
THRILLER = 53
WAR = 10752
WESTERN = 37

# TMDB TV genres (separate id space for the overlapping buckets).
TV_ACTION_ADVENTURE = 10759
TV_KIDS = 10762
TV_SCIFI_FANTASY = 10765

_MOOD_RULES: tuple[tuple[str, str, re.Pattern[str], tuple[int, ...], tuple[int, ...]], ...] = (
    (
        "kids",
        "kid-friendly",
        re.compile(
            r"\b(?:kids?|children'?s?|kinder(?:film|s)?|family(?:\s+friendly)?|"
            r"familie(?:film)?|for\s+the\s+(?:kids|little\s+ones)|toddler|age[- ]appropriate)\b",
            re.I,
        ),
        (FAMILY, ANIMATION),
        (HORROR, WAR),
    ),
    (
        "scary",
        "scary",
        re.compile(
            r"\b(?:scary|scare\s+me|horror|spooky|creepy|terrifying|frightening|"
            r"griezel(?:ig|film)?|spannend\s+en\s+eng|eng(?:e\s+film)?|slasher|haunted)\b",
            re.I,
        ),
        (HORROR,),
        (),
    ),
    (
        "comfort",
        "Friday-night comfort",
        re.compile(
            r"\b(?:comfort(?:\s+(?:movie|film|watch))?|cozy|cosy|feel[-\s]?good|"
            r"friday\s+night|saturday\s+night|easy\s+watch(?:ing)?|light(?:\s+and\s+fun)?|"
            r"niets?\s+te\s+zwaar|gezellig|wholesome|heart\s?warming)\b",
            re.I,
        ),
        (COMEDY, FAMILY, ROMANCE),
        (HORROR, WAR, DOCUMENTARY),
    ),
    (
        "funny",
        "funny",
        re.compile(
            r"\b(?:funny|comedy|hilarious|make\s+me\s+laugh|laugh|grappig|komedie|lach)\b",
            re.I,
        ),
        (COMEDY,),
        (HORROR,),
    ),
    (
        "mindbender",
        "mind-bending",
        re.compile(
            r"\b(?:mind[-\s]?(?:bend(?:er|ing)|f\*{0,3}ck)|twisty|twist\s+ending|"
            r"cerebral|makes?\s+you\s+think|clever\s+sci[-\s]?fi|puzzle\s+box|"
            r"hoofd\s?brekers?|slim(?:me)?\s+film)\b",
            re.I,
        ),
        (SCIFI, MYSTERY, THRILLER),
        (),
    ),
    (
        "action",
        "high-octane",
        re.compile(
            r"\b(?:action(?:\s+(?:movie|film|packed))?|explosions?|shoot\s?em\s?up|"
            r"popcorn\s+(?:movie|flick)|adrenaline|actie(?:film)?|knal(?:film)?|"
            r"blockbuster)\b",
            re.I,
        ),
        (ACTION, ADVENTURE),
        (DOCUMENTARY,),
    ),
    (
        "thriller",
        "tense",
        re.compile(
            r"\b(?:thriller|tense|edge\s+of\s+(?:my|your|the)\s+seat|nail[-\s]?biting|"
            r"suspense(?:ful)?|spannend)\b",
            re.I,
        ),
        (THRILLER, MYSTERY),
        (),
    ),
    (
        "romance",
        "romantic",
        re.compile(
            r"\b(?:romance|romantic|rom[-\s]?com|date\s+night|love\s+story|"
            r"romantisch|liefdes(?:film)?)\b",
            re.I,
        ),
        (ROMANCE, COMEDY),
        (HORROR,),
    ),
    (
        "scifi",
        "science fiction",
        re.compile(
            r"\b(?:sci[-\s]?fi|science\s+fiction|space\s+(?:opera|movie|film)|"
            r"aliens?|dystopian|cyberpunk|ruimte(?:film)?)\b",
            re.I,
        ),
        (SCIFI,),
        (),
    ),
    (
        "fantasy",
        "fantasy",
        re.compile(
            r"\b(?:fantasy|magical?|wizards?|dragons?|sword\s+and\s+sorcery|"
            r"fantasie|tovenaars?|sprookje)\b",
            re.I,
        ),
        (FANTASY, ADVENTURE),
        (),
    ),
    (
        "tearjerker",
        "a good cry",
        re.compile(
            r"\b(?:tear[-\s]?jerker|sad(?:\s+(?:movie|film))?|cry|emotional|"
            r"heart\s?break(?:ing)?|verdrietig|huil(?:film)?)\b",
            re.I,
        ),
        (DRAMA, ROMANCE),
        (COMEDY, HORROR),
    ),
    (
        "true_story",
        "true story",
        re.compile(
            r"\b(?:true\s+story|based\s+on\s+(?:a\s+)?(?:true|real)|documentary|docu|"
            r"waargebeurd|documentaire|real\s+events)\b",
            re.I,
        ),
        (DOCUMENTARY, HISTORY),
        (),
    ),
    (
        "crime",
        "crime",
        re.compile(
            r"\b(?:crime|heist|gangster|mob(?:ster)?|detective|whodunn?it|"
            r"misdaad|maffia)\b",
            re.I,
        ),
        (CRIME, THRILLER),
        (),
    ),
    (
        "animation",
        "animated",
        re.compile(r"\b(?:animated|animation|anime|cartoon|pixar|tekenfilm)\b", re.I),
        (ANIMATION,),
        (),
    ),
    (
        "western",
        "western",
        re.compile(r"\b(?:western|cowboys?|wild\s+west)\b", re.I),
        (WESTERN,),
        (),
    ),
    (
        "war",
        "war",
        re.compile(r"\b(?:war\s+(?:movie|film)|oorlogs(?:film)?|wwii|world\s+war)\b", re.I),
        (WAR, HISTORY),
        (),
    ),
)

# A vibe ask needs at least one of these to exist — otherwise it is a title.
_VIBE_FRAME = re.compile(
    r"\b(?:"
    r"something|somethin|anything|iets|iemand\s+raden|"
    r"in\s+the\s+mood|mood\s+for|zin\s+in|"
    r"what\s+should\s+(?:i|we)\s+watch|wat\s+moeten?\s+we\s+kijken|"
    r"recommend|recommendation|suggest|suggestion|pick|picks|"
    r"movie\s+night|film\s?avond|"
    r"surprise\s+me|verras\s+me|"
    r"i(?:'m| am)\s+bored|geen\s+idee"
    r")\b",
    re.I,
)

_TV_HINT = re.compile(r"\b(?:series|show|shows|serie|seriesss|tv|binge|season)\b", re.I)
_MOVIE_HINT = re.compile(r"\b(?:movie|movies|film|films|flick)\b", re.I)

# Riddle language describes *one* specific title the user half-remembers. A
# genre word inside such a sentence ("became a wizard") is a clue, not a mood,
# so discover must not hijack it from the LLM guess lane.
_RIDDLE_FRAME = re.compile(
    r"(?:"
    r"\b(?:that|this)\s+(?:movie|film|one|show|series)\b|"
    r"\b(?:die|deze)\s+(?:film|serie)\b|\bhet\s+filmpje\b|"
    r"\bthe\s+one\s+(?:where|with|about|who)\b|"
    r"\blooking\s+for\b|\bcan'?t\s+remember\b|\bforgot\s+the\s+name\b|"
    r"\b(?:guy|girl|man|woman|boy|kid|someone|iemand)\s+(?:with|who|die|met)\b|"
    r"\bbecame?\s+an?\b|\bturns?\s+into\b|\bwhere\s+(?:a|the|they|he|she)\b"
    r")",
    re.I,
)
# Deliberately *not* a riddle signal: "with the <noun>" appears in plenty of real
# titles ("Gone with the Wind", "Late Night with the Devil").


def looks_like_riddle(text: str) -> bool:
    """True when the message describes one half-remembered title.

    Such a message is for the LLM guess lane: it is neither a catalog title nor
    a mood, and searching it verbatim is a guaranteed miss.
    """
    return bool(_RIDDLE_FRAME.search((text or "").strip()))

_RUNTIME_HOURS = re.compile(
    r"\b(?:under|below|less\s+than|shorter\s+than|max(?:imum)?|within|no\s+more\s+than|"
    r"onder|minder\s+dan|korter\s+dan)\s+"
    r"(?P<count>\d{1,2}(?:[.,]5)?)\s*(?:h|hr|hrs|hour|hours|uur)\b",
    re.I,
)
_RUNTIME_MINUTES = re.compile(
    r"\b(?:under|below|less\s+than|shorter\s+than|max(?:imum)?|within|no\s+more\s+than|"
    r"onder|minder\s+dan|korter\s+dan)\s+"
    r"(?P<count>\d{2,3})\s*(?:m|min|mins|minutes|minuten)\b",
    re.I,
)
_RUNTIME_SHORT = re.compile(
    r"\b(?:short(?:\s+(?:movie|film|one))?|quick(?:\s+(?:watch|one))?|"
    r"nothing\s+long|not\s+too\s+long|kort(?:e\s+film)?|snel\s+klaar)\b",
    re.I,
)
_RUNTIME_LONG = re.compile(
    r"\b(?:epic|long(?:\s+(?:movie|film|one))?|three\s+hours?|lange\s+film)\b",
    re.I,
)

# Two-word asks that are unambiguously a vibe rather than a catalog title.
_BARE_MOOD_ASKS = frozenset(
    {
        "kids movie",
        "kids film",
        "kids movies",
        "family movie",
        "horror movie",
        # "Scary Movie" is a real franchise, so it stays on the title path.
        "comfort movie",
        "action movie",
        "kinderfilm",
        "feel good",
        "feel-good",
        "date night",
        "movie night",
        "friday night",
    }
)

_DECADE = re.compile(r"\b(?:(?:19|20)?(?P<decade>\d0)(?:'?s|er(?:jaren)?)|(?P<full>19\d0s))\b")
_RECENT = re.compile(
    r"\b(?:recent|new(?:est)?|latest|this\s+year|fresh|nieuw(?:e|ste)?|net\s+uit|"
    r"just\s+(?:out|released))\b",
    re.I,
)
_CLASSIC = re.compile(
    r"\b(?:classic|classics|old(?:er|\s+school)?|vintage|klassiek(?:er)?)\b",
    re.I,
)
_ACCLAIMED = re.compile(
    r"\b(?:best|top[-\s]?(?:rated|notch)|highly\s+rated|acclaimed|award[-\s]?winning|"
    r"masterpiece|beste|hoog\s+gewaardeerd)\b",
    re.I,
)

_TV_GENRE_MAP: dict[int, int] = {
    ACTION: TV_ACTION_ADVENTURE,
    ADVENTURE: TV_ACTION_ADVENTURE,
    SCIFI: TV_SCIFI_FANTASY,
    FANTASY: TV_SCIFI_FANTASY,
    FAMILY: TV_KIDS,
    HORROR: MYSTERY,
    WAR: 10768,
}


def _runtime_cap(text: str) -> int | None:
    hours = _RUNTIME_HOURS.search(text)
    if hours:
        raw = hours.group("count").replace(",", ".")
        try:
            return max(40, min(400, int(round(float(raw) * 60))))
        except ValueError:
            return None
    minutes = _RUNTIME_MINUTES.search(text)
    if minutes:
        try:
            return max(40, min(400, int(minutes.group("count"))))
        except ValueError:
            return None
    if _RUNTIME_SHORT.search(text):
        return 105
    return None


def _decade_floor(text: str) -> str:
    decade = _DECADE.search(text)
    if not decade:
        return ""
    raw = decade.group("full") or decade.group("decade") or ""
    digits = re.sub(r"\D", "", raw)[:4]
    if len(digits) == 4:
        return f"{digits[:3]}0-01-01"
    if len(digits) == 2:
        century = "19" if int(digits[0]) >= 3 else "20"
        return f"{century}{digits[0]}0-01-01"
    return ""


def _era_bounds(text: str) -> tuple[str, str]:
    """Return ``(floor, ceiling)`` release dates for era language."""
    floor = _decade_floor(text)
    if floor:
        # A named decade is a window, not an open-ended floor.
        year = int(floor[:4])
        return floor, f"{year + 9}-12-31"
    if _RECENT.search(text):
        return "2021-01-01", ""
    if _CLASSIC.search(text):
        return "", "1995-12-31"
    return "", ""


def _tv_genres(ids: tuple[int, ...]) -> tuple[int, ...]:
    mapped: list[int] = []
    for gid in ids:
        swapped = _TV_GENRE_MAP.get(gid, gid)
        if swapped not in mapped:
            mapped.append(swapped)
    return tuple(mapped)



# --- House night-mode (Ruben's house phrases) ---------------------------------
# Deterministic lifestyle moods beyond generic TMDB genres. Gated by
# HEARTH_TELEGRAM_HOUSE_NIGHTS (default on).

_HOUSE_NIGHT_RULES: tuple[tuple[str, str, re.Pattern[str], tuple[int, ...], tuple[int, ...], int | None], ...] = (
    (
        "date_night",
        "Friday night for us",
        re.compile(
            r"\b(?:"
            r"friday\s+night\s+for\s+us|"
            r"date[- ]?night(?:\s+for\s+us)?|"
            r"night\s+for\s+(?:us|two)|"
            r"something\s+(?:cosy|cozy)\s+for\s+(?:us|two)|"
            r"romantisch\s+avondje|"
            r"avondje\s+(?:voor\s+ons|samen)"
            r")\b",
            re.I,
        ),
        (COMEDY, FAMILY, ROMANCE),
        (HORROR, WAR, DOCUMENTARY),
        None,
    ),
    (
        "parel_kids",
        "kids movie for Parel",
        re.compile(
            r"\b(?:"
            r"kids?\s+movie\s+for\s+parel|"
            r"(?:for|voor)\s+parel|"
            r"parel(?:'s)?\s+(?:movie|film|kids?\s+movie)|"
            r"something\s+for\s+parel|"
            r"family[- ]safe(?:\s+for\s+parel)?"
            r")\b",
            re.I,
        ),
        (FAMILY, ANIMATION),
        (HORROR, WAR, THRILLER, CRIME),
        None,
    ),
    (
        "cooking_short",
        "something short while cooking",
        re.compile(
            r"\b(?:"
            r"something\s+short\s+while\s+cooking|"
            r"(?:short|quick)\s+(?:one|watch|movie|film)?\s+while\s+cooking|"
            r"while\s+(?:i(?:'m|\s+am)?\s+)?cooking|"
            r"during\s+cooking|"
            r"tijdens\s+het\s+koken|"
            r"iets\s+korts?\s+(?:tijdens|bij)\s+(?:het\s+)?koken"
            r")\b",
            re.I,
        ),
        (COMEDY, ANIMATION, FAMILY),
        (HORROR, WAR, DOCUMENTARY),
        90,
    ),
    (
        "sofa_sunday",
        "sofa Sunday",
        re.compile(
            r"\b(?:"
            r"sofa\s+sunday|sunday\s+sofa|"
            r"rainy\s+(?:sunday|afternoon)\s+(?:movie|film|watch)?|"
            r"lazy\s+sunday"
            r")\b",
            re.I,
        ),
        (COMEDY, FAMILY, ROMANCE, DRAMA),
        (HORROR, WAR),
        None,
    ),
    (
        "background_noise",
        "something on in the background",
        re.compile(
            r"\b(?:"
            r"background(?:\s+(?:noise|watch|movie|film))?|"
            r"something\s+on\s+in\s+the\s+background|"
            r"half[- ]watching|"
            r"op\s+de\s+achtergrond"
            r")\b",
            re.I,
        ),
        (COMEDY, ANIMATION),
        (HORROR, THRILLER, WAR),
        100,
    ),
)


def detect_house_night(text: str) -> MoodSpec | None:
    """House lifestyle moods for Ruben's phrases — no LLM."""
    from hearth.config import settings as _settings

    if not bool(getattr(_settings, "telegram_house_nights", True)):
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    for key, label, pattern, include, exclude, runtime_lte in _HOUSE_NIGHT_RULES:
        if not pattern.search(raw):
            continue
        media_type = "tv" if _TV_HINT.search(raw) and not _MOVIE_HINT.search(raw) else "movie"
        include_ids = tuple(include)
        exclude_ids = tuple(g for g in exclude if g not in include_ids)
        if media_type == "tv":
            include_ids = _tv_genres(include_ids)
            exclude_ids = tuple(g for g in _tv_genres(exclude_ids) if g not in include_ids)
        # Cooking / background phrases always prefer a short runtime.
        cap = runtime_lte
        if cap is None:
            cap = _runtime_cap(raw)
        out_label = label
        if cap is not None:
            hours, minutes = divmod(cap, 60)
            span = f"{hours}h{minutes:02d}" if minutes else f"{hours}h"
            if "under" not in out_label:
                out_label = f"{out_label} under {span}"
        return MoodSpec(
            key=key,
            label=out_label,
            media_type=media_type,
            genre_ids=include_ids,
            exclude_genre_ids=exclude_ids,
            runtime_lte=cap,
            vote_count_gte=150 if key == "parel_kids" else 200,
            sort_by="popularity.desc",
        )
    return None


def detect_mood(text: str) -> MoodSpec | None:
    """Return discover coordinates for a vibe ask, or None when it is a title.

    A bare genre word ("horror") is only a mood when framed as a request
    ("something scary", "any horror movies"), so exact titles such as *Horror
    Express* still take the fast title path.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    house = detect_house_night(raw)
    if house is not None:
        return house

    matched: list[tuple[str, str, tuple[int, ...], tuple[int, ...]]] = []
    for key, label, pattern, include, exclude in _MOOD_RULES:
        if pattern.search(raw):
            matched.append((key, label, include, exclude))

    runtime_lte = _runtime_cap(raw)
    era_floor, era_ceiling = _era_bounds(raw)
    acclaimed = bool(_ACCLAIMED.search(raw))
    framed = bool(_VIBE_FRAME.search(raw))
    constrained = bool(runtime_lte or era_floor or era_ceiling or acclaimed)

    if not matched and not (framed and constrained):
        return None

    # A half-remembered plot belongs to the guess lane, not to discover.
    if _RIDDLE_FRAME.search(raw) and not constrained:
        return None

    words = len(raw.split())
    if matched and not framed and not constrained:
        # A bare genre noun is only a mood when the ask is short enough to be
        # nothing else; longer sentences are titles or descriptions.
        lowered = raw.casefold().strip(" .!?")
        if lowered in _BARE_MOOD_ASKS:
            pass
        elif words > 4 or words <= 2:
            return None

    media_type = "tv" if _TV_HINT.search(raw) and not _MOVIE_HINT.search(raw) else "movie"

    include: list[int] = []
    exclude: list[int] = []
    labels: list[str] = []
    keys: list[str] = []
    for key, label, inc, exc in matched:
        keys.append(key)
        labels.append(label)
        for gid in inc:
            if gid not in include:
                include.append(gid)
        for gid in exc:
            if gid not in exclude:
                exclude.append(gid)
    # Never exclude a genre the user explicitly asked for.
    exclude = [gid for gid in exclude if gid not in include]

    include_ids = tuple(include)
    exclude_ids = tuple(exclude)
    if media_type == "tv":
        include_ids = _tv_genres(include_ids)
        exclude_ids = tuple(g for g in _tv_genres(exclude_ids) if g not in include_ids)

    if not labels:
        labels.append("house pick")
        keys.append("house")

    label = labels[0] if len(labels) == 1 else " + ".join(labels[:2])
    if runtime_lte is not None:
        hours, minutes = divmod(runtime_lte, 60)
        span = f"{hours}h{minutes:02d}" if minutes else f"{hours}h"
        label = f"{label} under {span}"

    return MoodSpec(
        key="-".join(keys[:2]),
        label=label,
        media_type=media_type,
        genre_ids=include_ids,
        exclude_genre_ids=exclude_ids,
        runtime_lte=runtime_lte,
        runtime_gte=120 if _RUNTIME_LONG.search(raw) and runtime_lte is None else None,
        vote_average_gte=7.2 if acclaimed else None,
        vote_count_gte=500 if acclaimed else 200,
        release_date_gte=era_floor,
        release_date_lte=era_ceiling,
        sort_by="vote_average.desc" if acclaimed else "popularity.desc",
    )


def house_pick_spec(*, media_type: str = "movie") -> MoodSpec:
    """A safe, broadly-good default when the ask is vague but actionable."""
    return MoodSpec(
        key="house",
        label="house pick",
        media_type="tv" if media_type == "tv" else "movie",
        genre_ids=(),
        exclude_genre_ids=(DOCUMENTARY,),
        vote_average_gte=7.0,
        vote_count_gte=800,
        sort_by="popularity.desc",
    )


def looks_like_vague_ask(text: str) -> bool:
    """True for "what should we watch?" style asks with no vibe attached."""
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_VIBE_FRAME.search(raw)) and detect_mood(raw) is None


__all__ = [
    "detect_house_night",
    "detect_mood",
    "house_pick_spec",
    "looks_like_riddle",
    "looks_like_vague_ask",
]
