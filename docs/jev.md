# Jev (TypeSafe System One) — decision / governance sandbox

Jev is **not** an LLM and does not generate text. It returns typed **Choice / Noul / Score** answers with probabilities. Hearth uses it as:

1. A cheap gate **before** the OpenAI agent tool loop (cancel / CoS / refuse).
2. The **first-class Telegram media intent router** — every media-ish Telegram turn hits Jev first; OpenAI is only called when Jev says `descriptive_riddle` / `needs_llm` (or confidence is too low).

## Defaults (safe)

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEARTH_JEV_ENABLED` | `false` | Master switch. Off = fail-open to local Telegram heuristics / today's agent path. |
| `HEARTH_JEV_SHADOW` | `true` | When enabled: cancel/confirm/CoS enforcement stays off (log only). **Media `media_ask` routing is still first-class when Jev is enabled and confident.** |
| `TYPESAFE_API_KEY` | empty | Bearer key for `POST https://api.typesafe.ai/v1/systemone`. **Host VAULT `.env` only — never commit, never log.** |
| `HEARTH_JEV_MODEL` | `jev-latest` | Alias (moves with releases). Pin e.g. `jev-1.13.0` once thresholds are tuned. |
| `HEARTH_JEV_DOMAIN_CONFIDENCE` | `0.72` | Min Choice confidence to prefer CoS / refuse in enforce mode. |
| `HEARTH_JEV_MEDIA_ASK_CONFIDENCE` | `0.72` | Min Choice confidence to route Telegram on `media_ask`. |
| `HEARTH_JEV_NEEDS_LLM_THRESHOLD` | `0.55` | Min Noul `needs_llm` to force a gpt hop even for non-riddle asks. |
| `HEARTH_JEV_CANCEL_THRESHOLD` | `0.78` | Min Noul `is_cancel` to block queue paths in enforce mode. |
| `HEARTH_JEV_CONFIRM_THRESHOLD` | `0.78` | Min Noul `is_confirm` for Telegram pending-guess confirm in enforce mode. |

## Telegram media router (`media_ask`)

One parallel System One call (`evaluate_telegram_media`) classifies:

| Choice | Path |
| --- | --- |
| `exact_title` | Overseerr/TMDB search — no LLM |
| `known_franchise` | Franchise seed search → multi Get cards — no LLM |
| `series_all` | Whole-series expansion → multi Get (never silent bulk queue) — no LLM |
| `edition_aware` | Strip cut/quality tokens → search clean title + note preference — no LLM |
| `descriptive_riddle` | gpt-4o catalog guess → search → Get/yes confirm |
| `chat_about_title` | gpt-4o short answer (info only) — **no** Get / queue |
| `not_media` | Ignore / leave to other handlers |

`needs_llm` Noul reinforces when a generative hop is required. Missing key, disabled Jev, API errors, or low confidence → **fail open** to local heuristics. Overseerr still queues **only** after Get / explicit yes on a pending guess.

## Shadow vs enforce

- **Shadow** (`HEARTH_JEV_ENABLED=true`, `HEARTH_JEV_SHADOW=true`): cancel/confirm/CoS stay advisory (logged). Telegram **media_ask routing still applies** when confidence clears the media threshold — that is the product differentiator.
- **Enforce** (`HEARTH_JEV_SHADOW=false`): high-confidence cancel → do not run queue tools; high-confidence `escalate_cos` → Chief of Staff; API errors / low confidence → fail open.
- Telegram: never invents a queue without a pending guess or Get tap. Enforce may treat high-confidence Jev confirm/cancel like yes/nah.

## Ops

1. Put `TYPESAFE_API_KEY=…` in the VAULT host `.env` (same place as other secrets).
2. Restart Hearth / recreate the container so settings reload.
3. Flip `HEARTH_JEV_ENABLED=true` (leave shadow on at first). Watch logs for `jev.gate` / `jev.shadow_outcome` with `channel=telegram_media_router`.
4. When comfortable with cancel/confirm, set `HEARTH_JEV_SHADOW=false` to enforce those gates.

Client prefers the official `typesafe-sdk` (`AsyncTypeSafeClient`); if the package is missing, Hearth falls back to a thin httpx POST to System One.
