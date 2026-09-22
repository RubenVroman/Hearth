# Telegram media — smoke script

A hand-run pass over the media surface. Every line is a message to send (or a
button to tap) plus the answer that proves the lane is honest. It takes a few
minutes end to end and is meant to be run against the real house bot after a
deploy, or against `pytest` for the parts that do not need Overseerr.

Nothing here queues anything by itself: **Get / yes is the only queue
boundary**, so you can run the whole script and only tap Get where noted.

## 0. Automated first

```bash
pytest -q                                   # whole suite
pytest -q tests/test_telegram_media_psychic.py    # this surface's regressions
pytest -q tests/test_telegram_media_magic.py \
         tests/test_telegram_media_next_tier.py \
         tests/test_telegram_media_intelligence.py
```

A deterministic lane check with no Overseerr, no OpenAI and no TypeSafe:

```bash
python3 - <<'PY'
from hearth.telegram.media.classify import classify_media_ask_sync as c
from hearth.telegram.parse import parse_message_text as p
for text in [
    "Dune", "Cowboys & Aliens", "Bonnie and Clyde", "Scary Movie (2000)",
    "American Horror Story", "Something Wild", "Date Night",
    "something scary under 2 hours", "anything with Florence Pugh",
    "something like Arrival", "grab Inception and Interstellar",
    "all Harry Potters except the last", "LOTR extended", "the sequel",
]:
    i = c(text, parsed=p(text))
    print(f"{text:<34} {i.kind:<16} {i.search_title or (i.mood.key if i.mood else '')}")
PY
```

Expected: the first seven land on `exact_title` / `known_franchise`, the rest on
`mood`, `person`, `similar`, `batch`, `series_all`, `edition`, `follow_up`.

## 1. Status truth — never a wasted Get

| Send | Expect |
| --- | --- |
| a title that is **on Plex** | `… is already on Plex` **and** a `▶ Play` button. No Get. |
| a title that is **downloading** | `… is already requested` and a `Downloading…` button. No Get. |
| a title that is **missing** | `Get 1 · <title> (year) movie` |
| a **partly** available series | `Get rest 1 · …` |

Tap the Get on the missing one. Expect `Requested …; Overseerr sent it to the
media stack.` Send the same title again: the card now shows the in-flight state,
not a second Get.

## 2. House nights, mood, person, similar

| Send | Expect |
| --- | --- |
| `something scary under 2 hours` | horror shortlist, header mentions `under 2h` |
| `Friday night for us` | date-night shortlist, no horror in it |
| `kids movie for Parel` | family/animation only, no horror or war |
| `something short while cooking` | shortlist capped around 90 minutes |
| `anything with Florence Pugh` | her credits, `🔁 More options` present |
| `movies with Robert de Niro` | his credits — the surname particle must survive |
| `something like Arrival` | neighbours of Arrival, header names Arrival |

## 3. Ambiguous vibe vs title

Send `Date Night`. Expect the **vibe** shortlist plus a `🎬 I meant "Date Night"`
chip. Tap the chip: the message is edited into the exact-title card for the 2010
film. Same for `Feel Good`.

Send `Date Night (2010)` instead: no vibe at all, straight to the title. A year,
a sequel number (`Scary Movie 3`) or quotes always win over a vibe reading.

## 4. Plans (batch)

| Send | Expect |
| --- | --- |
| `grab Inception and Interstellar` | one plan message, one Get per item |
| `grab Harry Potter and Dune` | two items, not one bad search |
| `grab LOTR and Hobbit` | two items, **no** `no catalog match` |
| `Cowboys & Aliens` | **one** card for the 2011 film |
| `Bonnie and Clyde` | **one** card |
| `Crouching Tiger, Hidden Dragon` | **one** card |

## 5. Franchise and exclusions

| Send | Expect |
| --- | --- |
| `all Harry Potters` | release-order cards, one Get each |
| `all Harry Potters except the last` | same minus the newest, header says `skipping the last` |
| `all Harry Potters except the last 4` | the `4` is read, not searched |
| an exclusion bigger than the franchise | `That leaves nothing — the catalog only has N entries…` |

## 6. Follow-ups and watch-next

Search a franchise entry, then in the same thread:

| Send | Expect |
| --- | --- |
| `the second one` | confirm card for card #2 |
| `nah, the other one` | the runner-up |
| `all of them` | the whole franchise |
| `more like that` | neighbours |
| `more` | fresh titles, none repeated |

Now tap Get on part one. The accept line ends with a watch-next nudge naming the
next film. Send `next` — expect **that** film, not another page of the old
search. Send `next` again: it pages normally, because the offer was consumed.

## 7. Play on the TV

| Do | Expect |
| --- | --- |
| tap `▶ Play` on an on-Plex card | `Playing <title> on …`, or a real Infuse/Plex error |
| `play it on the TV` after an on-Plex card | same |
| `play it on the TV` after a **missing** card | `… isn't on Plex yet … Tap Get` |
| `play it on the TV` with nothing on screen | `Nothing on screen to play.` |
| tap a Play button from yesterday's card | still names the real title, never `TMDB 603` |

With `HEARTH_TELEGRAM_PLAY_LANE=false`, Play says it is turned off. It never
claims success it did not get.

## 8. Never silent, and the confirm boundary

| Do | Expect |
| --- | --- |
| send anything media-ish | **always** a reply — a card, a miss line, or an honest failure |
| exceed `HEARTH_TELEGRAM_RATE_LIMIT_PER_MINUTE` | `Give me about Ns …` **and** the ask echoed back |
| send a title, then `yes` | queues by mediaId — check Overseerr shows one request |
| send a title, then `nah` | `not queueing that`, and Overseerr has nothing new |
| tap an expired button | `That button is invalid or expired.` |
| tap the same Get twice | `already being handled` / `already handled`, never two requests |

The mediaId path is the one to watch: confirming must **never** re-search by
title. After a `yes`, the Overseerr request should carry the same TMDB id that
was on the card.

## 9. Latency feel

With `HEARTH_JEV_ENABLED=true`, watch the logs for `jev.gate`. Exact titles,
franchises, editions, people, moods, similar, batch and follow-ups must all
answer **without** a gpt call. Only a descriptive riddle (`the one where the guy
loses his memory`) or a plot question should reach OpenAI.

```bash
docker compose logs -f hearth | grep -E 'jev.gate|openai'
```

An ask that Jev is unsure about but that still has a seed — `all Harry Potters`,
`LOTR extended` — must stay on the deterministic lane rather than falling back
to a guess.
