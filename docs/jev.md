# Jev (TypeSafe System One) — decision / governance sandbox

Jev is **not** an LLM and does not generate text. It returns typed **Choice / Noul / Score** answers with probabilities. Hearth uses it as:

1. A cheap gate **before** the OpenAI agent tool loop (cancel / CoS / refuse).
2. The **first-class Telegram media intent router** — every media-ish Telegram turn hits Jev first, which picks one of eleven lanes; OpenAI is only called when Jev says `descriptive_riddle` / `needs_llm` (or confidence is too low).
3. The shared **tool execution gate** — every registry invocation asks `allow_tool` and `which_tool`; Telegram Play uses the same gate to choose Infuse or Plex before either backend is called.

## Defaults (safe)

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEARTH_JEV_ENABLED` | `false` | Master switch. Off = fail-open to local Telegram heuristics / today's agent path. |
| `HEARTH_JEV_SHADOW` | `true` | Cancel/confirm/CoS enforcement stays off (log only). **Media `media_ask` and tool `allow_tool` / `which_tool` routing are still first-class when Jev is enabled and confident.** |
| `TYPESAFE_API_KEY` | empty | Bearer key for `POST https://api.typesafe.ai/v1/systemone`. **Host VAULT `.env` only — never commit, never log.** |
| `HEARTH_JEV_MODEL` | `jev-latest` | Alias (moves with releases). Pin e.g. `jev-1.13.0` once thresholds are tuned. |
| `HEARTH_JEV_DOMAIN_CONFIDENCE` | `0.72` | Min Choice confidence to prefer CoS / refuse in enforce mode. |
| `HEARTH_JEV_TOOL_CONFIDENCE` | `0.72` | Min `which_tool` Choice confidence. Lower confidence fails open to the proposed tool. |
| `HEARTH_JEV_TOOL_ALLOW_THRESHOLD` | `0.5` | Min `allow_tool` Noul to execute when Jev returned a valid tool-gate answer. |
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

## Shared tool gate (`allow_tool` / `which_tool`)

`ToolRegistry.call` uses `evaluate_tool_call` before executing a handler. The System One state contains the redacted user message, proposed tool, redacted arguments, and channel. Its typed questions are:

- `allow_tool` (Noul): whether any tool should execute now.
- `which_tool` (Choice): the single registered tool that owns the turn, or `no_tool`.

A confident different choice reroutes to that registered tool; the raw LLM/local proposal is not executed. A confident deny runs nothing. Disabled Jev, missing credentials, API errors, missing tool-gate answers, unknown choices, or low `which_tool` confidence fail open to the proposed tool. This fail-open contract is shared by chat, Realtime voice, authenticated web invokes, future Telegram house commands that use the registry, and Telegram Play’s direct Infuse/Plex dispatcher.

Telegram Play deliberately gives Jev only `infuse_play` and `plex_play` as choices. Title/TMDB/season arguments remain deterministic and server-side; Jev chooses the backend but does not generate playback arguments.

## Shadow vs enforce

- **Shadow** (`HEARTH_JEV_ENABLED=true`, `HEARTH_JEV_SHADOW=true`): cancel/confirm/CoS stay advisory (logged). Telegram `media_ask` and shared tool `allow_tool` / `which_tool` routing still apply when their thresholds clear.
- **Enforce** (`HEARTH_JEV_SHADOW=false`): high-confidence cancel → do not run queue tools; high-confidence `escalate_cos` → Chief of Staff; API errors / low confidence → fail open.
- Telegram: never invents a queue without a pending guess or Get tap. Enforce may treat high-confidence Jev confirm/cancel like yes/nah.

## Ops

1. Put `TYPESAFE_API_KEY=…` in the VAULT host `.env` (same place as other secrets).
2. Restart Hearth / recreate the container so settings reload.
3. Flip `HEARTH_JEV_ENABLED=true` (leave shadow on at first). Watch logs for `jev.gate`, `jev.tool_gate`, and `jev.shadow_outcome`.
4. When comfortable with cancel/confirm, set `HEARTH_JEV_SHADOW=false` to enforce those gates.

Client prefers the official `typesafe-sdk` (`AsyncTypeSafeClient`); if the package is missing, Hearth falls back to a thin httpx POST to System One.
