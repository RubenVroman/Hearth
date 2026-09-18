# Jev (TypeSafe System One) — decision / governance sandbox

Jev is **not** an LLM and does not generate text. It returns typed **Choice / Noul / Score** answers with probabilities. Hearth uses it as a cheap gate **before** the OpenAI agent tool loop (and optionally to sharpen Telegram yes/nah detection when enforce is on). Generation and tools still go through gpt / OpenAI.

## Defaults (safe)

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEARTH_JEV_ENABLED` | `false` | Master switch. Off = today's behavior, no TypeSafe calls. |
| `HEARTH_JEV_SHADOW` | `true` | When enabled: log Jev answers vs what Hearth did; **do not enforce**. |
| `TYPESAFE_API_KEY` | empty | Bearer key for `POST https://api.typesafe.ai/v1/systemone`. **Host VAULT `.env` only — never commit, never log.** |
| `HEARTH_JEV_MODEL` | `jev-latest` | Alias (moves with releases). Pin e.g. `jev-1.13.0` once thresholds are tuned. |
| `HEARTH_JEV_DOMAIN_CONFIDENCE` | `0.72` | Min Choice confidence to prefer CoS / refuse in enforce mode. |
| `HEARTH_JEV_CANCEL_THRESHOLD` | `0.78` | Min Noul `is_cancel` to block queue paths in enforce mode. |
| `HEARTH_JEV_CONFIRM_THRESHOLD` | `0.78` | Min Noul `is_confirm` for Telegram pending-guess confirm in enforce mode. |

## Shadow vs enforce

- **Shadow** (`HEARTH_JEV_ENABLED=true`, `HEARTH_JEV_SHADOW=true`): one System One call per chat turn; structured logs (`jev.gate`, `jev.shadow_outcome`); existing routing and tools unchanged.
- **Enforce** (`HEARTH_JEV_SHADOW=false`): high-confidence cancel → do not run queue tools (agent returns a short decline); high-confidence `escalate_cos` → call `chief_of_staff` without the gpt tool loop; API errors / low confidence → **fail open** to today's agent path.
- Telegram: Overseerr still queues **only** after Get / explicit confirm of a pending guess. Enforce may treat high-confidence Jev confirm/cancel like yes/nah; it never invents a queue without a pending guess.

## Ops

1. Put `TYPESAFE_API_KEY=…` in the VAULT host `.env` (same place as other secrets).
2. Restart Hearth / recreate the container so settings reload.
3. Flip `HEARTH_JEV_ENABLED=true` and leave `HEARTH_JEV_SHADOW=true`. Watch logs for `jev.gate` / `jev.shadow_outcome`.
4. When comfortable, set `HEARTH_JEV_SHADOW=false` to enforce.

Client prefers the official `typesafe-sdk` (`AsyncTypeSafeClient`); if the package is missing, Hearth falls back to a thin httpx POST to System One.
