# Jev (TypeSafe System One) — decision / governance sandbox

Jev is **not** an LLM and does not generate text. It returns typed **Choice / Noul / Score** answers with probabilities. Hearth uses it as:

1. A cheap gate **before** the OpenAI agent tool loop (cancel / CoS / refuse).
2. The **butler tool gate** (`butler_ask`) for shelf and scene-preset tools. The language model does not choose those tools. Bare “movie night” and “lights down” stay on the playback routes.
3. The **first-class Telegram media intent router** — every media-ish Telegram turn hits Jev first, which picks one of eleven lanes; OpenAI is only called when Jev says `descriptive_riddle` / `needs_llm` (or confidence is too low).
4. A gate before Telegram house commands (slash commands and strict natural
   light/scene/cover/status forms) call the shared Home Assistant tools.

## Defaults (safe)

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEARTH_JEV_ENABLED` | `false` | Master switch. Off = fail-open to local Telegram heuristics / today's agent path. |
| `HEARTH_JEV_SHADOW` | `true` | When enabled: cancel/confirm/CoS enforcement stays off (log only). **Media `media_ask` routing and butler `butler_ask` tool choice are still first-class when Jev is enabled and confident.** |
| `TYPESAFE_API_KEY` | empty | Bearer key for `POST https://api.typesafe.ai/v1/systemone`. **Host VAULT `.env` only — never commit, never log.** |
| `HEARTH_JEV_MODEL` | `jev-latest` | Alias (moves with releases). Pin e.g. `jev-1.13.0` once thresholds are tuned. |
| `HEARTH_JEV_DOMAIN_CONFIDENCE` | `0.72` | Min Choice confidence to prefer CoS / refuse in enforce mode, and to act on `butler_ask`. |
| `HEARTH_JEV_MEDIA_ASK_CONFIDENCE` | `0.72` | Min Choice confidence to route Telegram on `media_ask`. |
| `HEARTH_JEV_NEEDS_LLM_THRESHOLD` | `0.55` | Min Noul `needs_llm` to force a gpt hop even for non-riddle asks. |
| `HEARTH_JEV_MULTI_ITEM_THRESHOLD` | `0.65` | Min Noul `multi_item` for an ask that covers more than one title. |
| `HEARTH_JEV_CANCEL_THRESHOLD` | `0.78` | Min Noul `is_cancel` to block queue paths in enforce mode. |
| `HEARTH_JEV_CONFIRM_THRESHOLD` | `0.78` | Min Noul `is_confirm` for Telegram pending-guess confirm in enforce mode. |

## Telegram media router (`media_ask`)

One parallel System One call (`evaluate_telegram_media`) classifies:

| Choice | Path | LLM |
| --- | --- | --- |
| `exact_title` | Overseerr/TMDB search | no |
| `known_franchise` | Franchise seed search → multi Get cards in release order | no |
| `series_all` | Whole-series expansion, TMDB collection preferred, honours "except the last" (never a silent bulk queue) | no |
| `edition_aware` | Strip cut/quality tokens → search clean title + note preference | no |
| `person_filmography` | `search_person` → `person_combined_credits` → ranked credits | no |
| `mood_vibe` | Vibe language → `discover` genre / runtime / era / rating filters | no |
| `similar_to` | Resolve the anchor once → `/{movie,tv}/{id}/similar` + `/recommendations` | no |
| `batch_multi` | Split into ≤ `HEARTH_TELEGRAM_BATCH_MAX_ITEMS` items → one plan, one Get per item | no |
| `follow_up` | Resolve against the recent chat context (sequel, all of them, nth, other, more) | no |
| `descriptive_riddle` | gpt-4o catalog guess → search → Get/yes confirm | yes |
| `chat_about_title` | gpt-4o short answer (info only) — **no** Get / queue | yes |
| `not_media` | Search a salvaged title, else say what the bot can do | no |

`needs_llm` reinforces when a generative hop is required; `multi_item` reinforces batch/series asks.

**Jev picks the lane; the lane payload is always derived locally.** Franchise seeds, edition tokens, person names, discover coordinates and plan items come from the deterministic extractors in `hearth/telegram/media/`, never from prose. If Jev picks a lane the text cannot substantiate — `mood_vibe` for a plot riddle, say — the ask degrades to a lane that *can* answer (here, the gpt guess lane) instead of running a search that is bound to miss.

Missing key, disabled Jev, API errors, or low confidence → **fail open** to local heuristics, which route the same lanes. Overseerr still queues **only** after Get / explicit yes on a pending guess, and that queue always goes out by `mediaId` — confirming never re-searches the title.

The Telegram media question map does **not** include `butler_ask` or `domain`. Shelf and scene asks on Telegram call `evaluate_message` (the shared hearth map) before Plex or Home Assistant.

## Butler tools (`butler_ask`)

`house_shelf` and `house_scene` are chosen on the shared hearth System One call (`evaluate_message`), not by the language model. The Choice is `shelf`, `movie_night`, `quiet_hours`, `good_night`, or `other`. Confidence uses `HEARTH_JEV_DOMAIN_CONFIDENCE`.

- A confident choice runs that tool even in shadow (answers are populated; same idea as `media_ask`).
- A confident `other` does not run the shelf or preset tool. Bare “movie night” and “lights down” still follow the playback routes.
- Disabled Jev, a missing key, an API error, or low confidence **fails open** to the local phrase router.
- OpenAI chat and Realtime tool lists omit `house_shelf` and `house_scene`. Voice `run_house_tool` re-checks the gate and refuses a call Jev did not clear.
- The scene button sends a canonical phrase (`movie night`, `quiet hours`, `good night`) through the same gate. An unknown preset still gets the local “I know movie night…” reply when Jev is off.
- The after-queue shelf line calls `evaluate_message` and skips the Plex read only on enforced `block_cancel`. It does not require `butler_ask=shelf`.

## Shadow vs enforce

- **Shadow** (`HEARTH_JEV_ENABLED=true`, `HEARTH_JEV_SHADOW=true`): cancel/confirm/CoS stay advisory (logged). Telegram **media_ask routing still applies** when confidence clears the media threshold — that is the product differentiator. Confident **butler_ask** choices still run `house_shelf` / `house_scene`.
- **Enforce** (`HEARTH_JEV_SHADOW=false`): high-confidence cancel → do not run queue tools; high-confidence `escalate_cos` → Chief of Staff; API errors / low confidence → fail open.
- Telegram media: never invents a queue without a pending guess or Get tap. Enforce may treat high-confidence Jev confirm/cancel like yes/nah.
- Telegram house: shadow logs the intended HA tool; enforce blocks the tool on a high-confidence cancel/refuse verdict. API errors still fail open.

## Ops

1. Put `TYPESAFE_API_KEY=…` in the VAULT host `.env` (same place as other secrets).
2. Restart Hearth / recreate the container so settings reload.
3. Flip `HEARTH_JEV_ENABLED=true` (leave shadow on at first). Watch logs for `jev.gate` / `jev.shadow_outcome` with `channel=telegram_media_router`.
4. When comfortable with cancel/confirm, set `HEARTH_JEV_SHADOW=false` to enforce those gates.

Client prefers the official `typesafe-sdk` (`AsyncTypeSafeClient`); if the package is missing, Hearth falls back to a thin httpx POST to System One.
