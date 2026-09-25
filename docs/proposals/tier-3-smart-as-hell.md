# Make Hearth “as smart as hell” (tier-3 house intelligence)

**Issue:** [#93](https://github.com/RubenVroman/Hearth/issues/93)

**Status:** proposal only. Do not treat this file as an implementation. Later PRs chip away at the [build slices](#suggested-build-slices) below. This document is the intent those PRs should not have to re-derive.

## Source

Voice ask via the Hearth → Chief of Staff webhook on **2026-09-25**.

**Spoken (STT):** “Bots—to make it as smart as hard as you'd, so actually add a feature, so create this as a big feature request. Can you hear me?”

## Phrase clarification

The STT string **“as smart as hard as you'd”** is ambiguous. Product intent, in order:

1. **Primary:** *“as smart as hell as you'd [like]”* (`hell` → `hard` is a common STT swap). Push Hearth’s intelligence hard. This is not a tiny tweak.
2. **Secondary:** make the *smart* side (LLM / intent / ranking / routines) as strong as the *hard* side (integrations and tools) already is.
3. **Not assumed:** a new product named “Hard”, or “as smart as Annie”. Annie / Studio Zorg is out of scope for this Hearth ask.

Treat the ask as a **major tier-3 intelligence enhancement**. Hearth should feel proactive, context-aware, and decisive across house and media, not only reactive tool calls.

## Goals

1. **Proactive house brain** — anticipate routines (sleep / morning / movie night) from time, presence, and recent context without needing perfect phrasing.
2. **Psychic media** — exact titles go fast (Get); fuzzy / sequel / franchise / “what’s next” asks resolve with ranking memory; play-on-TV when that is the natural end state.
3. **Unified governance** — Jev gates *every* tool decision (HA, Plex, Overseerr, play, memory, macros); fail-open only when the key is missing; never silent unsafe tool spam.
4. **Speed-matched butler voice / chat** — short confirmations for clear intents; richer dialogue only when ambiguity is real.
5. **Whole-home macros** — one utterance runs coherent scenes (lights + media + climate) with undo and status.

## In scope (tier-3)

- Plex-aware Get + watch-next / franchise memory
- Night / morning / movie-night (and similar) modes via HA + media tools
- Play-on-TV end-to-end from Telegram / voice / UI
- Hard Jev gate on all tool paths (shared gate module)
- Surprise butler behaviors that are reversible and logged (for example “wind down” when late)
- Clear status / “what did you just do?” replies

## Out of scope (for this RFC)

- Editing non-Hearth repos
- Discord / Gridways / outbound email
- Annie / Studio Zorg product features
- Asking Ruben for DSM passwords or editing the `hearth-recreate` task definition

## Non-goals for v1 of the build

Perfect autonomy with no confirmations on destructive actions; purchasing; contacting people outside the house stack.

## Current baseline

Slices extend what is already on `main`. They do not invent a second house stack.

| Area | Already in tree | Still missing for this RFC |
| --- | --- | --- |
| Jev gate | `hearth/jev/tools.py` `authorize_tool()` decides `ToolRegistry.call()`. Telegram queue and play call the same function. One System One answer set per turn. Missing key, errors, and timeouts fail open (`ok=false`, reason logged). Shadow is the default. Reads are denied only by hard stops. See [docs/jev.md](../jev.md). | Prove **every** invocation path (voice sideband, `/api/invoke`, local router, Telegram house commands, butler tools, memory writes, macros) shares that module. Tests that lock missing-key fail-open. No parallel ungated tool path. |
| Whole-home rituals | `house_ritual` (`sleep` / `morning` / `movie`) in `hearth/tools/house.py` composes an HA scene or lights plus Denon / LG / Apple TV power. Spoken “movie night” stays on `media_activity`. Climate is a separate tool. | One utterance that is a coherent scene (lights + media + climate), with undo and a status the user can ask for. Imperfect phrasing and time/presence context should still land on the right ritual. |
| Watch-next | Telegram thread memory (`hearth/telegram/media/memory.py`) stores one watch-next after a Get/queue and offers it on `next`. Smoke script: [docs/telegram-media-smoke.md](../telegram-media-smoke.md). | Plex-aware ranking: fuzzy title, sequel, franchise, and “what’s next” resolve to a ranked pick using **house memory** (`hearth/memory/`), not an ad-hoc file on VAULT and not only the Telegram thread TTL. |
| Play-on-TV | Telegram `play_on_tv` and `plex_play` / Infuse. A missing title says it is not on Plex yet and points at Get. | Same end state from voice and the command-center UI, not only Telegram, when play is the natural end of a resolved ask. |
| Butler | `butler_ask` chooses `house_shelf` / `house_scene`. Confident choices run even in shadow. Bare “movie night” and “lights down” stay on playback routes. | Surprise behaviors (late wind-down and similar) that are reversible, logged, and summarizable. “What did you do?” names the last macro or media action. |

## Suggested implementation notes

1. Keep a single **intent → Jev → tools** loop. No parallel ungated tool paths. New tools register a lane in `hearth/jev/tools.py` and go through `authorize_tool()` / `ToolRegistry.call()`. An unclassified tool stays a **write**.
2. Prefer **typed decisions** (Choice / Score / Noul) for classify / allow / escalate before any LLM tool loop. Lane arguments stay deterministic extracts from the text, never prose from the model. `needs_llm` is the only reason to hop to gpt.
3. Store watch-next and franchise preference in the existing memory skills (`memory_remember` / `memory_search`, SQLite on `./data`). Do not add ad-hoc files on VAULT. Telegram thread memory may cache a turn; the durable preference lives in house memory.
4. Macros compose existing HA + Plex / Overseerr tools (`house_ritual`, `media_activity`, `house_climate`, `plex_play`, `infuse_play`). Do not add a new device protocol unless Tuya Local / Home Assistant already exposes the entity. Missing devices are skipped and said so, the way `run_ritual` already skips an unpaired cinema.
5. Ship risky behavior behind the existing Jev shadow switch or a dedicated flag. Expand only after smoke tests in [docs/telegram-media-smoke.md](../telegram-media-smoke.md) and the ops steps in [docs/jev.md](../jev.md). Shadow logs `suggested` vs `action`; it does not change allow/deny until `HEARTH_JEV_SHADOW=false`.
6. Deploy path stays: merge to `main` → overlay SHA on VAULT → run the existing `hearth-recreate` task (Enabled unchecked). Do not edit that task definition and do not ask for DSM passwords.
7. Voice and chat stay short when the intent is clear (“House is down.”, “Playing Dune on the LG.”). Longer dialogue is for real ambiguity (two titles, a confirm on a write, a missing device). Do not waffle into a generic LLM answer when a ranked media pick or a ritual exists.
8. Destructive or hard-to-undo writes still confirm. Explicit confirms (Get tap, typed yes, authenticated `/api/invoke`) stay pre-authorized: the gate runs and logs, and only hard stops (`refuse`, `do_not_auto_run`) can block them.
9. Fail-open means: Jev off, no `TYPESAFE_API_KEY`, no user text, an API error, a timeout, or an unparseable answer set returns `allow` with `ok=false` and a reason in the log. It does **not** mean a path that never calls the gate.

## Suggested build slices

Ordered PRs. Each slice should be mergeable on its own and should update this file’s checklist when it lands. Do not start slice *n+1* by re-litigating slice *n*’s intent.

### 1. Harden the shared Jev gate on all tools

- Audit every tool entry: agent loop, Realtime sideband, `/api/invoke`, local (no-OpenAI) router, Telegram house commands, Telegram queue/play, butler shelf/scene, memory writes, `house_ritual`.
- Anything that changes house or media state calls `authorize_tool()` (directly or via `ToolRegistry.call()`). Delete or fold leftover regex-only write paths that skip the gate.
- Tests: missing `TYPESAFE_API_KEY` fail-open (`allow`, `ok=false`, reason `missing_api_key`); shadow logs `suggested` without changing behavior; a confident deny/cancel does not enter the handler once enforce is on; reads still pass unless `refuse` / `do_not_auto_run`.
- Docs: if the chokepoint list in [docs/jev.md](../jev.md) is wrong after the audit, fix that page in the same PR.

### 2. Whole-home macros: sleep, morning, movie night

- One utterance (voice, Telegram, UI) runs a coherent scene: lights + media + climate, composing `house_ritual` / `media_activity` / `house_climate`. Do not fork a second ritual implementation.
- Spoken “movie night” / “lights down” keep today’s routing (playback / scene) unless the utterance is clearly the whole-home mode (“movie night mode”, “house sleep”, “good morning”).
- Each macro returns a step list and a short speak line. Persist enough of that result for undo and for slice 5’s status reply.
- Undo restores the previous light / media / climate snapshot when HA still has it, and says what it could not put back.
- Imperfect phrasing and obvious context (late night → sleep, “we’re watching” → movie) may *suggest* the macro. A state-changing run still goes through Jev; high `needs_confirm` / `do_not_auto_run` escalates instead of firing.

### 3. Plex-aware Get and watch-next memory

- Exact titles stay on the fast Get path (Overseerr / TMDB, no gpt).
- Fuzzy, sequel, franchise, and “what’s next” resolve to a **ranked** pick. Ranking uses Plex availability (on-library titles beat a queue) plus franchise order and the stored preference.
- Durable watch-next and franchise preference go through house memory, not a new file on VAULT. Telegram may still nudge from thread memory; “next” on voice and chat must see the same preference.
- A miss stays honest (`isn’t on Plex yet`, offer Get). It does not fall through to a generic chat answer.

### 4. Play-on-TV

- After a resolved on-Plex pick, play is the natural end state from Telegram, voice, and the command center: `plex_play` or Infuse, matching today’s Apple TV preference (`HEARTH_APPLE_TV_PLAYER`).
- Same honesty rules as the Telegram smoke script: name the client, surface a real Infuse/Plex error, never claim success PMS did not confirm, never play a title that is not on Plex (point at Get instead).
- The play call is a write and goes through the shared Jev gate. A Get tap or typed yes stays an explicit confirm.

### 5. Surprise butler and status summaries

- A small set of reversible, logged surprises. First one: **wind down** when it is late and the house is still “up” (lights + cinema still on) — run the sleep macro or a quieter subset, and say what changed.
- Log the action the way other notable house writes are logged (house-event / memory), so it can be undone and explained.
- “What did you do?” / “status” summarizes the last macro or media action in one or two sentences (which ritual or title, which steps failed or were skipped). It does not dump tool JSON.
- Surprises never spend money, never queue a download, and never message anyone outside the house stack. If Jev says `needs_confirm` or `do_not_auto_run`, ask; do not surprise.

## Acceptance criteria

- [x] Written proposal landed in-repo under `docs/proposals/` (this file).
- [ ] Voice / Telegram / UI can trigger at least one whole-home macro end-to-end on VAULT.
- [ ] A fuzzy media ask resolves to a ranked pick, with optional play-on-TV, and does not fall through to a generic LLM waffle.
- [ ] Every tool invocation path goes through the shared Jev gate (tests cover missing-key fail-open).
- [ ] “What did you do?” / status can summarize the last macro or media action.
- [x] Docs updated (`docs/jev.md` points here) so CoS / cloud agents can continue without re-deriving intent.

The open boxes are the build. Checking one of them belongs in the PR that actually lands that slice.

## Tracking

Opened from Hearth webhook `confirm:true`. Standing Hearth consent applies for follow-up PRs that implement slices of this RFC. Issue [#93](https://github.com/RubenVroman/Hearth/issues/93) stays open until the build criteria above are met; this proposal does not close it.
