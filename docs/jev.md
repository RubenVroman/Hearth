# Jev (TypeSafe System One) — the tool-calling gate

Jev is **not** an LLM and does not generate text. It returns typed **Choice / Noul / Score** answers with probabilities. In Hearth it is not a side-channel that logs opinions — it is the thing that decides **every tool call**:

1. **Which tool** — a `tool_lane` Choice narrows a turn to one family of house tools. The local router follows Jev instead of regex precedence alone.
2. **Allow or deny** — a `tool_allow` Noul, the `is_cancel` Noul, and the `risk` Score decide whether a state-changing tool may run at all.
3. **Whether an LLM is needed** — the `needs_llm` Noul rides along so a lane can skip or force a gpt hop.
4. **Which media lane** — on Telegram, a `media_ask` Choice picks one of eleven catalog lanes.
5. **Butler tools** (`butler_ask`) — shelf and scene-preset tools. The language model does not choose those. Bare “movie night” and “lights down” stay on the playback routes.
6. A gate before Telegram house commands (slash commands and strict natural light/scene/cover/status forms) call the shared Home Assistant tools.

## Three invariants

**One call per turn.** A turn opens a scope and the first gated tool call fetches — or inherits — a single System One answer set. Every later tool call in that turn is decided locally from it, so an eight-tool OpenAI turn costs one typed call, not eight. The agent loop and the Telegram media router hand the gate the verdict they already paid for. Concurrent tools in one turn share it behind a lock.

**Fail open.** Jev off, no `TYPESAFE_API_KEY`, no user text to reason about, an API error, a timeout, or an unparseable answer set all return `allow` with `ok=false` and a reason in the log. A house that cannot reach its gate behaves exactly like a house without one. **Reads are never denied** except by a hard stop, so a misread gate can stop Hearth *acting* but never stop it *answering*.

**Shadow never changes behavior.** In shadow mode the gate still computes and logs the decision it *would* have taken. `suggested` is the enforce decision; `action` is what actually happened. Review `jev.tool_gate` log lines before switching enforcement on.

## Defaults (magic on, safely)

| Variable | Default | Meaning |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | empty | Bearer key for `POST https://api.typesafe.ai/v1/systemone`. **Host VAULT `.env` only — never commit, never log.** |
| `HEARTH_JEV_ENABLED` | `true` | Master switch. Without a key this is a no-op: nothing is called and every path fails open. |
| `HEARTH_JEV_SHADOW` | `true` | Log the decision without taking it. Enforcement is a deliberate flip. |
| `HEARTH_JEV_TOOL_GATE` | `true` | Gate every `ToolRegistry.call()` plus the Telegram queue/play chokepoints. |
| `HEARTH_JEV_ROUTE_LOCAL_TOOLS` | `true` | Let a confident `tool_lane` pick the tool in the local (no-OpenAI) router. |
| `HEARTH_JEV_TIMEOUT_SECONDS` | `8.0` | Hard ceiling on one System One call, transport included. |
| `HEARTH_JEV_MODEL` | `jev-latest` | Alias (moves with releases). Pin e.g. `jev-1.13.0` once thresholds are tuned. |

### Thresholds

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEARTH_JEV_TOOL_ALLOW_THRESHOLD` | `0.35` | Deny a write only when `tool_allow` is **below** this — clearly against, not a coin flip. |
| `HEARTH_JEV_TOOL_LANE_CONFIDENCE` | `0.75` | Min `tool_lane` Choice confidence to pick or veto a tool family. |
| `HEARTH_JEV_RISK_CONFIDENCE` | `0.60` | Min `risk` Score confidence before `needs_confirm` / `do_not_auto_run` bites. |
| `HEARTH_JEV_CANCEL_THRESHOLD` | `0.78` | Min `is_cancel` Noul to block a write (and queue paths in the message gate). |
| `HEARTH_JEV_CONFIRM_THRESHOLD` | `0.78` | Min `is_confirm` Noul for the Telegram pending-guess confirm in enforce mode. |
| `HEARTH_JEV_DOMAIN_CONFIDENCE` | `0.72` | Min `domain` Choice confidence to prefer CoS / refuse, and to act on `butler_ask`. |
| `HEARTH_JEV_MEDIA_ASK_CONFIDENCE` | `0.72` | Min Choice confidence to route Telegram on `media_ask`. |
| `HEARTH_JEV_NEEDS_LLM_THRESHOLD` | `0.55` | Min `needs_llm` Noul to force a gpt hop even for a non-riddle ask. |
| `HEARTH_JEV_MULTI_ITEM_THRESHOLD` | `0.65` | Min `multi_item` Noul for an ask covering more than one title. |

## Tool lanes

Lanes are coarse on purpose: a 14-label Choice is cheap and stable, a 50-label one is neither. **Jev picks the lane; the lane's arguments are always derived locally** from the text, never from prose.

| Lane | Tools | Read / write |
| --- | --- | --- |
| `lights` | HA devices, `house_scene`, `house_ritual`, climate/feeder/purifier/comfort/status | mixed |
| `media_playback` | `plex_play`, `infuse_play`, `infuse_transport`, `videoland_play`, `ha_media_control`, `media_activity` | write |
| `media_library` | `plex_search`, `plex_now_playing`, `plex_clients`, `plex_browse_genre`, `house_media`, `house_shelf`, `suggest_titles` | read |
| `media_queue` | `overseerr_request`, `radarr_add`, `sonarr_add`, `radarr_retry`, `sonarr_retry`, `radarr_grab_release` | write |
| `media_status` | `radarr_queue`, `sonarr_queue`, `radarr_search`, `sonarr_search`, `overseerr_search`, `radarr_list_releases` | read |
| `food` | `thuisbezorgd_*` | mixed |
| `weather` | `get_weather` | read |
| `web` | `web_search` | read |
| `files` | `workspace_*`, `docker_*`, plus any workspace skill | mixed |
| `memory_read` | `memory_list`, `memory_search` | read |
| `memory_write` | `memory_remember`, `memory_forget`, `memory_export`, `memory_purge` | write |
| `network` | `house_network` | read |
| `escalate_cos` | `chief_of_staff` | write |
| `no_tool` | `end_call` | read |

A tool Hearth cannot classify is treated as a **write**, so a newly registered tool is gated by default rather than silently ungoverned. Workspace skills from `workspace/skills/` resolve to `files` through the registry.

## Enforce decisions

`suggest_tool_action()` is pure and synchronous, so the policy is unit-testable without a network. It is evaluated in this order:

| Order | Condition | Decision | Applies to |
| --- | --- | --- | --- |
| 1 | `domain` = `refuse` above `DOMAIN_CONFIDENCE` | `deny` / `refused` | **every** tool, including reads and taps |
| 2 | `risk` = `do_not_auto_run` above `RISK_CONFIDENCE` | `deny` / `high_risk` | **every** tool, including reads and taps |
| 3 | Caller marked the call an explicit confirm | `allow` / `explicit_confirm` | tapped buttons, typed yes, `/api/invoke` |
| 4 | Tool is a read | `allow` / `read_only` | reads |
| 5 | `is_cancel` above `CANCEL_THRESHOLD` | `deny` / `cancelled` | writes |
| 6 | `tool_allow` below `TOOL_ALLOW_THRESHOLD` | `deny` / `tool_not_allowed` | writes |
| 7 | Confident `tool_lane` = `no_tool` | `deny` / `no_tool_lane` | writes |
| 8 | Confident `tool_lane` ≠ the tool's lane | `deny` / `lane_mismatch` | writes |
| 9 | `risk` = `needs_confirm` above `RISK_CONFIDENCE` | `confirm` / `needs_confirm` | writes |
| — | otherwise | `allow` / `pass` | |

A `confirm` decision escalates a normally auto-run tool into the existing dry-run/confirm path, so the user gets the same "Confirm to run" affordance as a destructive tool. A `deny` returns a typed refusal with a house-voice sentence, and the handler is never entered.

## Where the gate sits

`ToolRegistry.call()` is the chokepoint — the agent loop, `/api/invoke`, and the realtime voice sideband all funnel through it. The Telegram bot reaches Overseerr and the TV directly rather than through the registry, so its **queue** and **play** chokepoints call `authorize_tool()` themselves.

**Explicit confirms are pre-authorized.** A Get tap, a typed yes on a pending guess, and an authenticated `/api/invoke` still run the gate and still log, but only the hard stops can block them. Denying a button the user just pressed would be the wrong product.

## Telegram media router (`media_ask`)

One parallel System One call (`evaluate_telegram_media`) classifies every media-ish turn:

| Choice | Path | LLM |
| --- | --- | --- |
| `exact_title` | Overseerr/TMDB search | no |
| `known_franchise` | Franchise seed search → multi Get cards in release order | no |
| `series_all` | Whole-series expansion, TMDB collection preferred, honours "except the last" (never a silent bulk queue) | no |
| `edition_aware` | Strip cut/quality tokens → search clean title + note preference | no |
| `person_filmography` | `search_person` → `person_combined_credits` → ranked credits | no |
| `mood_vibe` | Vibe language → `discover` genre / runtime / era / rating filters | no |
| `similar_to` | Resolve the anchor once → `/{movie,tv}/{id}/similar` + `/recommendations` | no |
| `batch_multi` | Split into ≤ `HEARTH_TELEGRAM_BATCH_MAX_ITEMS` items (default 4, ceiling 16) → one plan, one Get per item | no |
| `follow_up` | Resolve against the recent chat context (sequel, all of them, nth, other, more) | no |
| `descriptive_riddle` | gpt-4o catalog guess → search → Get/yes confirm | yes |
| `chat_about_title` | gpt-4o short answer (info only) — **no** Get / queue | yes |
| `not_media` | Search a salvaged title, else say what the bot can do | no |

Franchise seeds, edition tokens, person names, discover coordinates and plan items come from the deterministic extractors in `hearth/telegram/media/`, never from prose. If Jev picks a lane the text cannot substantiate — `mood_vibe` for a plot riddle, say — the ask degrades to a lane that *can* answer instead of running a search that is bound to miss.

The Telegram question map has no `tool_lane`: on Telegram `media_ask` **is** the lane choice, and `tool_allow` is what gates the queue and play tools.

Text media searches queue after Get or an explicit yes on a pending guess.
The [image lane](ambient-ai-and-images.md) also requests confidently resolved
pictured titles automatically when enabled and permitted by the caption. Both
paths use the same Jev gate and durable queue, always by resolved `mediaId`.

A poster or list graphic first becomes structured titles and verified catalog coordinates. The image itself is not sent to Jev. Automatic requests and signed Get callbacks share the same `tool_allow` gate and durable queue; automatic requests do not claim the explicit confirmation that a Get tap supplies. See [current image behavior](ambient-ai-and-images.md).

The Telegram media question map does **not** include `butler_ask` or `domain`. Shelf and scene asks on Telegram call `evaluate_message` (the shared hearth map) before Plex or Home Assistant.

## Butler tools (`butler_ask`)

`house_shelf` and `house_scene` are chosen on the shared hearth System One call (`evaluate_message`), not by the language model. The Choice is `shelf`, `movie_night`, `quiet_hours`, `good_night`, or `other`. Confidence uses `HEARTH_JEV_DOMAIN_CONFIDENCE`.

- A confident choice runs that tool even in shadow (answers are populated; same idea as `media_ask`).
- A confident `other` does not run the shelf or preset tool. Bare “movie night” and “lights down” still follow the playback routes.
- Disabled Jev, a missing key, an API error, or low confidence **fails open** to the local phrase router.
- OpenAI chat and Realtime tool lists omit `house_shelf` and `house_scene`. Voice tool calls still pass `ToolRegistry.call()` (fail-open).
- The scene button sends a canonical phrase (`movie night`, `quiet hours`, `good night`) through the same gate. An unknown preset still gets the local “I know movie night…” reply when Jev is off.
- The after-queue shelf line calls `evaluate_message` and skips the Plex read only on enforced `block_cancel`. It does not require `butler_ask=shelf`.

## Shadow vs enforce

- **Shadow** (`HEARTH_JEV_ENABLED=true`, `HEARTH_JEV_SHADOW=true`): cancel/confirm/CoS stay advisory (logged). Telegram **media_ask routing still applies** when confidence clears the media threshold — that is the product differentiator. Confident **butler_ask** choices still run `house_shelf` / `house_scene`.
- **Enforce** (`HEARTH_JEV_SHADOW=false`): high-confidence cancel → do not run queue tools; high-confidence `escalate_cos` → Chief of Staff; API errors / low confidence → fail open.
- Telegram text media: Get or a pending-guess confirmation authorizes the queue. Image auto-requests use the configured picture-request policy and trusted caption, with one Jev scope for the resolved batch. Enforce-mode cancel/refuse/risk decisions remain authoritative.
- Telegram house: shadow logs the intended HA tool; enforce blocks the tool on a high-confidence cancel/refuse verdict. API errors still fail open.
## State sent to Jev

Typed and short, never a transcript dump:

```json
{"user_message": "grab Dune", "recent_context": ["what's on plex", "Dune (2021)"]}
```

The message is redacted (`hearth.memory.redact`) and clipped to 500 characters; up to four recent lines are clipped to 160 each.

## Logs

| Line | When | Contains |
| --- | --- | --- |
| `jev.gate` | One per System One call | verdict, suggested vs action, all typed answers with probabilities |
| `jev.tool_gate` | One per gated tool call | tool, lane, write flag, suggested vs action, reason, chosen lane, risk level, channel, **argument key names only** |
| `jev.tool_turn` | One per turn that gated anything | channel, mode, every decision in the turn |
| `jev.shadow_outcome` | Message gate / media router | suggestion vs what Hearth actually did |

No line ever contains the API key, a bot token, or tool argument *values*.

## Ops

1. Put `TYPESAFE_API_KEY=…` in the VAULT host `.env` (same place as other secrets).
2. Restart Hearth / recreate the container so settings reload.
3. Check `GET /readyz`. `checks.jev.active` should be `true` and `degraded` should no longer contain `jev:no_api_key`.
4. Watch `jev.tool_gate` lines for a few days. Every one carries `suggested` — the decision enforce *would* have taken — alongside `action`. Look for suggested denies you disagree with.
5. Tune `HEARTH_JEV_TOOL_ALLOW_THRESHOLD` and `HEARTH_JEV_TOOL_LANE_CONFIDENCE` if the suggestions are too eager.
6. Set `HEARTH_JEV_SHADOW=false` to enforce. To keep the message gate advisory while still letting Jev route tools, leave shadow on — `media_ask` routing and `tool_lane` steering are first-class in shadow; only allow/deny/confirm waits for enforce.
7. To turn the gate off without turning Jev off, set `HEARTH_JEV_TOOL_GATE=false`. To stop Jev steering the local router, set `HEARTH_JEV_ROUTE_LOCAL_TOOLS=false`.

The client prefers the official `typesafe-sdk` (`AsyncTypeSafeClient`); if the package is missing, Hearth falls back to a thin httpx POST to System One. Both honour `HEARTH_JEV_TIMEOUT_SECONDS`, and `evaluate_message` wraps the call in a hard `asyncio.timeout` so an injected or SDK client without its own budget still cannot stall a turn.

## See also

Tier-3 house intelligence (proactive macros, Plex-aware watch-next, play-on-TV, surprise butler) is specified in [docs/proposals/tier-3-smart-as-hell.md](proposals/tier-3-smart-as-hell.md) and tracked in [#93](https://github.com/RubenVroman/Hearth/issues/93). That proposal does not change the gate; it requires every new tool path to keep using this one.
