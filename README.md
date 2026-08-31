# Hearth

House agent runtime for **Ruben Vroman** on **VAULT** (Synology DS1817+, DSM 7.3, x86_64).

Voice is the front door. Home Assistant is the device layer (lights, Denon AVR-X3700H, LG webOS TV). A thin command-center UI is optional and secondary — not a SaaS chatbot.

Hearth is meant to sit in Docker **next to** the existing stack (Plex, Sonarr, Radarr, Prowlarr, Overseerr, Gluetun). It does not replace that stack.

## What you get

| Surface | Role |
| --- | --- |
| Agent loop + tool registry | Whole-house HA inventory/control, receiver-centric Denon/LG/Apple TV activities, Infuse play on ATV, *arr/Overseerr grab/request, Telegram drop-group inbox, Plex now-playing + play-on-client, live `web_search`, Thuisbezorgd food order, workspace, docker inspect, Chief of Staff escalate |
| `GET /` command center | Now playing, lights/scenes, transcript, agent status. Requires login. |
| `GET /login` | Email + password. House FastAPI auth (X-Auth-Token + HttpOnly refresh cookie). |
| `POST /api/realtime/calls` | GA OpenAI Realtime over WebRTC (ChatGPT-app voice). Browser mic, barge-in, house tools on a sideband. |
| `WS /ws/voice` | Text fallback only. Never the disabled beta Realtime websocket. |
| `workspace/` | Sandboxed “build whatever I ask” directory. Not the whole NAS. |
| House memory | SQLite on `./data` — conversations, preferences, optional house events. Injected as a small retrieved slice, not the whole store. |
| `homeassistant` compose service | Official HA image, unconfigured, so the device layer exists on day one |

## Quick start on VAULT (Synology Container Manager)

1. Clone this repo onto the NAS, e.g. `/volume1/docker/hearth`.
2. Copy env template and edit it:

   ```bash
   cp .env.example .env
   ```

3. In Container Manager → Project → Create, point at this folder’s `docker-compose.yml`, **or** from SSH:

   ```bash
   cd /volume1/docker/hearth
   docker compose up -d --build
   ```

4. Set `APP_SECRET_KEY` (long random string) and create the first user (see Login below). Open `https://vault.taileff393.ts.net/login` on Tailscale, never a WAN port-forward.

5. Home Assistant onboarding: `http://<vault-lan-or-tailscale>:8123`  
   Create a long-lived token (Profile → Security), put it in `.env` as `HA_TOKEN`, then `docker compose up -d`.

`docker compose up` is enough to get a runnable runtime + UI. Without tokens, tools return house-shaped **fixtures** (living room lights, Denon, LG TV, Dune on Plex). Text chat still calls real tools against those mocks.

### Bind to Tailscale / LAN, not the internet

- Do **not** port-forward `8787` or `8123` on the router.
- Do **not** expose SMB for the agent. Workspace is a local bind mount only.
- Set `HEARTH_BIND` / `HA_BIND` in `.env` to the Tailscale IP (`100.x.y.z`) or a LAN IP if you want the ports off the rest of the host’s interfaces.
- Optional: `HEARTH_TOKEN` is a **machine/agent** bypass (`X-Hearth-Token` header only). Browser users log in. A shared URL with `?token=` does nothing.

The compose file publishes `0.0.0.0` by default so Tailscale and LAN both work. That is **not** the public internet unless you forward the port.

## Login

Hearth copies Ruben’s house FastAPI auth: bcrypt user, short-lived JWT in `X-Auth-Token` (not Bearer), HttpOnly refresh cookie. There is no Google, signup, or Next.js.

**First user** — either:

1. Set `APP_SECRET_KEY`, `HEARTH_ADMIN_EMAIL`, and `HEARTH_ADMIN_PASSWORD` in `.env`. On boot, if the users table is empty, Hearth creates that superuser, then you can remove the password from `.env` if you want.
2. Or run, once the container is up:

   ```bash
   docker compose exec hearth python -m hearth.auth.create_superuser
   ```

Users live in SQLite at `HEARTH_AUTH_DB` (compose bind-mounts `./data`). House memory uses a sibling file `HEARTH_MEMORY_DB` on the same volume. No Postgres.

`COOKIE_SECURE=true` is correct behind Tailscale HTTPS. Set `COOKIE_SECURE=false` only if you are hitting plain HTTP on the LAN.

Public without a session: `/login`, `/auth/token`, `/auth/session/refresh`, `/auth/session/logout`, `/health`, static files needed to render login. Everything else (including `/` and `/api/status`) requires a session or `X-Hearth-Token`.

## Environment

| Variable | Live vs stub |
| --- | --- |
| `OPENAI_API_KEY` | **Live** text agent (chat completions + tools) and GA Realtime WebRTC voice. Also the default backend for the `web_search` house tool (Responses API hosted web search). Empty → local intent router + text fallback on `/ws/voice`. |
| `OPENAI_ADMIN_KEY` | **Optional.** Admin API key for organization **Costs** + **Usage** monitors in Look → OpenAI spend. Project/inference keys cannot call `/v1/organization/costs` or `/usage/*`. Create at [Admin keys](https://platform.openai.com/settings/organization/admin-keys). Host `.env` only — never paste into the UI. |
| `OPENAI_MODEL` | Chat model. Default `gpt-4o-mini`. Also used for OpenAI-backed `web_search`. |
| `OPENAI_REALTIME_MODEL` | Conversational Realtime model. Default `gpt-realtime-2.1` (ChatGPT-app equivalent). |
| `HA_URL` | Default `http://host.docker.internal:8123`; HA uses host networking for LAN mDNS/SSDP discovery. |
| `HA_TOKEN` | **Live** HA REST. Empty → mocked lights/scenes/Denon/LG. |
| `HA_TV_ENTITY` | LG webOS `media_player` entity_id. Default `media_player.lg_webos_tv`. Set after HA pairing if different. |
| `HA_AVR_ENTITY` | Denon AVR entity_id. Default `media_player.denon_avr_x3700h`. |
| `HA_APPLE_TV_ENTITY` | Apple TV `media_player` (HA apple_tv / pyatv). Default `media_player.apple_tv`. Required for Infuse. |
| `HA_REQUEST_RETRIES` / `HA_RETRY_BASE_SECONDS` | Transient HA retry policy. Defaults `3` / `0.25`. Transport failures force a fresh connection. |
| `HA_VERIFY_TIMEOUT_SECONDS` / `HA_VERIFY_POLL_INTERVAL` | Observe device state after writes instead of trusting HTTP acceptance alone. Defaults `6` / `0.4`. |
| `HEARTH_RECEIVER_CENTRIC` | Default `true`. Media activities route through the Denon; TV/Apple-TV volume requests control the receiver. |
| `HA_AVR_APPLE_TV_SOURCE` / `HA_AVR_TV_SOURCE` | Denon source names for the Apple TV and TV Audio activities. Defaults `Media Player` / `TV Audio`. |
| `HEARTH_APPLE_TV_PLAYER` | `infuse` (default) or `plex`. Prefer Infuse over the Plex tvOS app for Apple TV. |
| `INFUSE_APP_ID` | Optional Infuse bundle id. Default `com.firecore.infuse`. |
| `PLEX_URL` | Existing Plex on the host. Default `http://host.docker.internal:32400`. |
| `PLEX_TOKEN` | **Live** Plex sessions/search/clients/play. Empty → mocked now-playing + library/play fixtures. |
| `PLEX_DEFAULT_PLAYER` | Optional default client name substring (e.g. `Apple TV`) when using the Plex-client path. |
| `PLEX_CLIENT_WAIT_SECONDS` | On confirm/play with no online clients, re-poll `/clients` this long (default `12`). |
| `PLEX_CLIENT_POLL_INTERVAL` | Seconds between client re-polls while waiting (default `1.5`). |
| `RADARR_URL` / `RADARR_API_KEY` | **Live** movie search/add. Default URL `http://host.docker.internal:7878`. Empty key → fixtures. |
| `SONARR_URL` / `SONARR_API_KEY` | **Live** series search/add. Default `http://host.docker.internal:8989`. |
| `OVERSEERR_URL` / `OVERSEERR_API_KEY` | **Live** request front door. Default `http://host.docker.internal:5055`. |
| `HEARTH_DELIVERY_STREET` / `POSTCODE` / `CITY` | House delivery address for Thuisbezorgd. Empty → browse/order refuse until set. Never invent an address in code. |
| `THUISBEZORGD_API_KEY` | **Live** partner JE-API-KEY. Empty → fixtures only. Just Eat Takeaway has no public self-serve consumer ordering API. |
| `THUISBEZORGD_SESSION_TOKEN` / `EMAIL` / `PASSWORD` | Server-side consumer auth for live submit (with API key). Never sent to the browser; never logged. |
| `THUISBEZORGD_API_BASE` / `TENANT` | Default `https://nl.api.just-eat.io` / `nl`. |
| `HEARTH_WEATHER_LAT` / `LON` / `PLACE` | Open-Meteo house weather. Defaults near Ghent. |
| `BRAVE_SEARCH_API_KEY` | Optional. If set, `web_search` uses Brave Search (structured title/snippet/url, 10s timeout) instead of OpenAI. Server-side only — never sent to the browser. |
| `HEARTH_WEB_SEARCH_MOCK` | Force fixture results for `web_search`. Default `false`. |
| `HEARTH_COS_WEBHOOK` | **Live** Chief of Staff POST. Empty → tool returns “not configured” (not fake success). |
| `HEARTH_COS_WEBHOOK_KEY` | Optional. Sent as `Authorization: Bearer <key>`. |
| `HEARTH_COS_REPO` | Default `RubenVroman/Hearth`. |
| `DOCKER_SOCKET` | Read-only socket is mounted. If missing → mocked container list (plex/sonarr/…/gluetun). |
| `WORKSPACE_PATH` | Inside the container, `/app/workspace`. |
| `HEARTH_MOCK_IF_UNCONFIGURED` | Default `true`. Fixtures are used only when a backend is unconfigured. A configured live HA failure is never turned into fake success. |
| `APP_SECRET_KEY` | Required to sign JWTs. Empty → nobody can log in. |
| `HEARTH_ADMIN_EMAIL` / `HEARTH_ADMIN_PASSWORD` | Bootstrap first superuser if the users table is empty. Leave empty after that. |
| `HEARTH_TOKEN` | Optional **machine** bypass (`X-Hearth-Token` header). Not a browser login. |
| `HEARTH_MEMORY_ENABLED` | Master switch. Default `true`. |
| `HEARTH_MEMORY_STORE_CONVERSATIONS` | Persist sessions/turns. Default `true`. |
| `HEARTH_MEMORY_STORE_HOUSE_EVENTS` | Log notable confirmed house writes (lights, grabs). Default `false` (opt-in). |
| `HEARTH_MEMORY_HOUSE_EVENT_SAMPLE` | When house events are on, fraction to keep (`1` = all notable writes). |
| `HEARTH_MEMORY_EMBEDDINGS` | Optional OpenAI `text-embedding-3-small`. Off or no key → FTS5 keyword search still works. **Redacted text leaves the NAS** when embeddings are on. |
| `HEARTH_MEMORY_INJECT` | Attach a small retrieved slice to chat + Realtime prompts. Default `true`. |
| `HEARTH_MEMORY_RETENTION_DAYS` | Conversation prune. Default `90`. Preferences are kept until forgotten (`HEARTH_MEMORY_PREFERENCE_RETENTION_DAYS=0`). |
| `HEARTH_MEMORY_DB` | SQLite path. Compose: `/app/data/hearth-memory.db` on the `./data` volume. |

Plex token: Plex Web → settings URL, or XML at `http://<plex>:32400/library/sections` while signed in — `X-Plex-Token` in the query. Do not commit it.

Reach Plex on the existing stack:

- `PLEX_URL=http://host.docker.internal:32400` (compose sets `host-gateway`)
- or the host LAN IP, e.g. `http://192.168.1.10:32400`

## OpenAI spend monitor

Look → **OpenAI spend** shows real usage/cost for the house app. The browser never sees API keys; Hearth proxies OpenAI server-side.

| Source | What you get | Requirement |
| --- | --- | --- |
| `GET /v1/organization/costs` | Billed USD amounts (org Costs API) | `OPENAI_ADMIN_KEY` (Admin API key) |
| `GET /v1/organization/usage/completions` | Token counts by model | `OPENAI_ADMIN_KEY` |
| Hearth local ledger | Measured `usage` fields from Hearth’s own chat/embed/search calls | `OPENAI_API_KEY` (already used for inference) |
| Official list pricing | Public per-model rates, labeled **not your invoice** | None (shipped reference) |

**Security**

- Secrets stay in the host `.env` on VAULT. Do not paste keys into the UI.
- Admin keys are for organization management only — they cannot run chat/Realtime. Keep `OPENAI_API_KEY` for inference and `OPENAI_ADMIN_KEY` for spend reads.
- If the admin key is missing or OpenAI rejects the call, the UI shows an explicit unavailable/error state. It never invents spend figures.
- Local list-price math is labeled as a **local estimate** and only uses token counts OpenAI returned on Hearth’s own responses.

**Setup:** add `OPENAI_ADMIN_KEY=…` to `.env`, recreate the container (`docker compose up -d`), open Look → OpenAI spend. Create the key at [platform.openai.com → Admin keys](https://platform.openai.com/settings/organization/admin-keys).

API surface (auth required): `GET /api/openai/spend`, `/api/openai/costs`, `/api/openai/usage`, `/api/openai/pricing`.

## Voice

Live voice is the **GA OpenAI Realtime API over WebRTC** — the same family as ChatGPT Advanced Voice / GPT Realtime. It is **not** hold-to-talk, and it is **not** the old beta websocket.

**Live** (key present): the browser opens `RTCPeerConnection`, POSTs SDP to Hearth `POST /api/realtime/calls`, and Hearth forwards a multipart `sdp` + `session` to `https://api.openai.com/v1/realtime/calls` with the NAS `OPENAI_API_KEY`. No `OpenAI-Beta` header. Mic uses browser AEC (`echoCancellation`) plus a client speech-band barge-in gate (`/static/vad.js`): while Hearth is talking, the outbound track stays muted until consecutive speech-like frames are seen, so TV/HVAC/clatter should not cut playback. The GA session also enables `audio.input.noise_reduction` (`near_field`) before semantic VAD. Remote audio plays through a hidden `<audio>` element. House tools (`ha_*`, `plex_*`, `radarr_*`, `sonarr_*`, `overseerr_*`, `workspace_*`, `docker_*`, `chief_of_staff`, `end_call`, `memory_*`, Thuisbezorgd, weather, `web_search`) run on Hearth over a sideband `wss://api.openai.com/v1/realtime?call_id=…`.

**Close of call:** when the conversation is finished (goodbye / done / nothing left), the model calls `end_call`. After that response completes, Hearth closes the sideband and the UI hangs up the WebRTC peer connection so the session does not stay open idle.

Realtime session instructions include the same retrieved memory slice as text chat; after each spoken transcript the sideband refreshes that slice.

**Fallback** (`WS /ws/voice`, or no key): text only. Composer always uses `POST /api/chat`. Do not send PCM over that socket expecting live voice.

### How to tell it is the new path

- `GET /api/status` → `realtime.path === "webrtc-ga"`, `realtime.beta === false`, `realtime.model === "gpt-realtime-2.1"`
- After a call: `voice.path === "webrtc-ga"`, `voice.mode === "live"`, `voice.beta === false`
- `POST /api/realtime/calls` response headers: `X-Hearth-Realtime-Path: webrtc-ga`, `X-Hearth-Realtime-Beta: false`
- Browser talks to Hearth, not `wss://api.openai.com/v1/realtime?model=…` with `OpenAI-Beta: realtime=v1` (that shape is disabled: `beta_api_shape_disabled` / close 4000)
- UI: tap the hearth to start/stop a conversation. No “Hold to speak”. Playback is `<audio id="remote-audio">`, not ScriptProcessor PCM

`POST /api/realtime/client_secrets` mints an ephemeral `ek_…` token (never the long-lived key). The live UI uses the unified server SDP path so the long-lived key stays on the NAS.

## Routing

Hearth does the house itself. Everything else goes to Chief of Staff.

**Do it yourself**

- Lights, scenes → Home Assistant tools
- Everything HA represents on the house network → `house_network` / `GET /api/network`; reports reachability, unavailable entities, domains, and explicit Denon/LG/Apple TV links
- Any routine HA entity by friendly name → `ha_device_control` (lights, switches, fans, covers, climate, scenes, scripts, buttons, vacuums); ambiguous matches are returned instead of guessed
- LG TV / Denon AVR / Apple TV power, volume, source, transport → `ha_media_control` (prefer over raw `ha_call_service`)
- Receiver-centric “watch Apple TV”, “watch TV”, and “media chain off” → `media_activity`; orders Denon → LG → Denon source → Apple TV and reports every failed step
- House media snapshot (TV + AVR + Apple TV + Plex) → `house_media` or `GET /api/media`
- What's playing on Plex → `plex_now_playing` (Infuse has **no** now-playing API)
- Browse Plex library **by genre** (Animation, Science Fiction, …) → `plex_browse_genre` / `GET /api/plex/library?genre=Science%20Fiction`. Speakable count + short title list; glass overlay shows tappable genre category chips from real Plex metadata. `GET /api/plex/genres` / “list plex genres” opens the category picker.
- Recommend / suggest movies or shows (or “show them on the UI”) → `suggest_titles` / `POST /api/media/suggest` (same glass media overlay; metadata resolved server-side)
- Play on **Apple TV / Infuse** → `infuse_play` (default). Title → TMDB (Plex Guids / Radarr / Overseerr) → `infuse://movie/{tmdb}?play` via HA Apple TV `play_media` type `url`. See [Infuse on Apple TV](#infuse-on-apple-tv) below. Runs immediately (lenient).
- Pause / stop / skip while Infuse is up → `infuse_transport` (HA Apple TV remote — not Infuse REST)
- Play on **LG / Shield / an explicit Plex client** → `plex_play` (optional `plex_search` / `plex_clients`). PMS-proxied `playMedia`. Starts immediately; if no clients are online, Hearth keeps the play ready and **Try again** / confirm re-polls until the client appears.
- Download / grab a **movie** → Radarr (`radarr_search` / `radarr_add`)
- Download / grab a **show** → Sonarr (`sonarr_search` / `sonarr_add`)
- “Request X” → Overseerr (`overseerr_search` / `overseerr_request`), the request front door that feeds *arr
- **Telegram drop-group** → same *arr/Overseerr grab path; status posts back into the group (see below)
- Food / Thuisbezorgd → `thuisbezorgd_restaurants` → `thuisbezorgd_menu` → `thuisbezorgd_cart` → `thuisbezorgd_order` (confirm to place)
- Weather outside → `get_weather` (Open-Meteo; no API key)
- Live web (news, current events, where-to-watch / streaming) → `web_search` (OpenAI hosted web search by default; optional Brave; DuckDuckGo HTML lite last resort). Search results only — Hearth does not fetch arbitrary pages. Follow with `suggest_titles` when movie/TV ideas should appear as overlay cards.
- “Tell me about / what’s the movie …” → `plex_search` (glass overlay with title, year, summary, poster)

**Call Chief of Staff** (`chief_of_staff`)

- Repo / code / PR / git / “add a weather skill to the repo” — Hearth never edits GitHub
- Anything it cannot do yet (e.g. “connect to Discord”) — it must not pretend it connected
- Gridways / kanban / boards / “open tasks on project X” — Hearth has no Gridways; CoS + the Gridways agent do
- Calendar, GitHub/GitLab org work, teammate agents

Webhook payload:

```json
{
  "source": "hearth",
  "task": "…",
  "repo": "RubenVroman/Hearth",
  "confirm": true,
  "said": "original user text"
}
```

Auth: `Authorization: Bearer <HEARTH_COS_WEBHOOK_KEY>` when the key is set. Escalation runs
immediately when asked (no Hearth confirm step). If `HEARTH_COS_WEBHOOK` is empty, the tool
says it is not configured.

## Glass info overlay

The old “update guard” widget stack (Thinking / Action / Update cards beside the orb) is gone — it remounted on every status poll and flickered.

In its place, a **centered glass panel** opens when there is rich visual content. Media titles stack as cards when several come up in conversation.

| Kind | Source tools | Shows |
| --- | --- | --- |
| `weather` | `get_weather` | Place, temperature, condition, humidity / wind (single panel) |
| `media` | `plex_search`, `plex_browse_genre`, `plex_now_playing`, `plex_play`, `radarr_search`, `sonarr_search`, `overseerr_search`, `suggest_titles` | Stacked title cards (poster, year, summary); front card = what is being talked about now |
| `downloads` | `radarr_queue`, `sonarr_queue` | Queue progress list |

### Behavior

- **Hide when talk leaves.** Soft-hide (fade) when the conversation is no longer about the on-screen weather/title. Acknowledgments (“ok”, “thanks”) do not force-hide. Clear topic switches (lights, food, docker, weather↔media) hide promptly. The widget stays in runtime memory so the same topic can reappear without a new tool fetch. Hard dismiss (× / backdrop / Esc) still deletes.
- **Stacked media cards.** Search hits and successive title lookups accumulate into one `media` widget (`data.items` + `data.active_id`, with `data.item` = the active card). Naming a stacked title (chat or live Realtime transcript, including assistant audio deltas) brings that card forward. Single title → one card (not an empty stack).
- **Relevance** is evaluated against the *active* card’s entity tokens (title/place), not generic words like “movie”. Past the fresh window with no entity evidence → `context.relevant: false` (`stale` / `idle` / `unrelated:*`).

Payload path:

1. Tool runs on the server → `hearth/widgets.publish_tool` upserts a `Widget` on `runtime` (`kind` = `weather` \| `media` \| `downloads`).
2. Chat / invoke / realtime / `GET /api/status` return `widgets: [...]` with `context: { relevant, reason, topics, active_id? }` from `hearth/overlay_context.py`.
3. The UI (`#info-overlay`) renders the glass panel / card stack, reacts to live transcripts (not only the 8s status poll), and **skips DOM work when the overlay signature is unchanged** so polls do not flicker.
4. Dismiss: ×, backdrop, or Esc → `DELETE /api/widgets/{id}`.
5. Poster art: `GET /api/media/art` (and `/api/plex/thumb/{ratingKey}`) proxy art; API keys stay on the server. Missing art → initials fallback, never a broken image.

Ordinary confirms still use the bottom **Confirm** button — not the glass panel.

## House memory

Durable memory lives in SQLite next to auth on the compose `./data` volume (`HEARTH_MEMORY_DB=/app/data/hearth-memory.db`). WAL + indexes + FTS5. Optional embeddings in a BLOB table — **no local embedding model** (the Hearth container is 512m) and **no Postgres/Qdrant/Redis**.

What is stored by default:

- **Conversations** — sessions/turns (not only the last 24 in RAM). Long sessions get a rolling summary.
- **Preferences** — stable facts Ruben asks Hearth to remember (`memory_remember`).
- **House events** — notable confirmed writes (lights, grabs, CoS). **Off** until `HEARTH_MEMORY_STORE_HOUSE_EVENTS=true`.

Retrieval: preferences + latest session summary + a few FTS/semantic hits. The model never receives the whole store. The same `compose_system_prompt` path feeds chat completions and GA Realtime.

Privacy: secrets (API keys, JWTs, `.env` assignments, live tokens from settings) are redacted on write. They are not embedded, not exported, and not shown in the UI. Embeddings, when enabled, send **redacted** text to OpenAI.

Retention: conversations 90 days (and a max-turn cap); house events 30 days when enabled; preferences until `memory_forget`. A prune job runs at boot and every `HEARTH_MEMORY_PRUNE_INTERVAL_MINUTES` (default 60).

Tools (voice can speak these): `memory_remember`, `memory_search`, `memory_list`, `memory_forget`. `memory_export` / `memory_purge` exist too. Forget/export/purge default to dry-run until `confirm=true`. A click on **Forget** in the command center is the confirmation.

Gated APIs: `GET /api/memory`, `GET /api/memory/search`, `POST /api/memory/remember`, `POST /api/memory/forget`, `POST /api/memory/export`, `POST /api/memory/purge`. Same login / `X-Hearth-Token` gate as the rest of `/api/*`.

### First deploy / later schema bumps

Empty `./data` → boot runs `init_memory_db()` and creates schema v1. Re-running is idempotent. Later bumps are numbered SQL in `hearth/memory/store.py` (`SCHEMA_VERSION` / `_MIGRATIONS`). No manual migration step on first deploy.

## Infuse on Apple TV

Before every Infuse launch, Hearth prepares the receiver-centric media path. It retries transient
Home Assistant failures, reconnects stale HTTP sockets, and verifies device state after accepted
writes. If HA is configured but offline, Hearth reports the real failure; it never mutates fixtures
and claims the living room changed.

Ruben plays movies on the living-room **Apple TV in Infuse** (Firecore), not the Plex tvOS app. Plex `/clients` is often empty because Infuse is the player — so Hearth’s default Apple TV path is Infuse.

### What must already be true

1. **Infuse** installed on the Apple TV (8.4.7+ for the URL API), with your **Plex library and/or VAULT SMB shares** already connected inside Infuse.
2. **Home Assistant Apple TV** integration paired (pyatv). Entity shows under Developer Tools → States.
3. Hearth `.env` on VAULT:
   - `HA_TOKEN` — long-lived HA token
   - `HA_APPLE_TV_ENTITY=media_player.<your_apple_tv>` (default fixture id: `media_player.apple_tv`)
   - `HEARTH_APPLE_TV_PLAYER=infuse` (default; set `plex` only if you want the old Plex-app path)
   - `PLEX_TOKEN` (and optionally Radarr/Overseerr) so Hearth can resolve title → TMDB id
4. Recreate/restart the `hearth` container after `.env` changes. **Do not deploy from this PR** until you choose to.

### How it works

1. Resolve the title in the Plex library (same search as `plex_play`).
2. Read TMDB id from Plex `Guid` (`tmdb://…`). If missing, fall back to Radarr / Overseerr lookup.
3. Build a Firecore deep link, e.g. `infuse://movie/430231?play` or `infuse://series/{id}-{season}-{episode}?play`.
4. Call HA `media_player.play_media` on the Apple TV entity with `media_content_type: url` and that deep link (pyatv `apps.launch_app`).
5. Pause / play / stop / skip use the same HA Apple TV `media_player` services. **Infuse exposes no playback-state API or webhooks** — Hearth will not invent now-playing inside Infuse.

Direct `infuse://x-callback-url/play?url=…` file URLs are a documented fallback only; they do **not** sync Plex watch state.

### Spoken workflow (voice / chat)

| You say | Hearth does |
| --- | --- |
| “Play The Endless on the Apple TV” / “put it on Infuse” | Dry-run `infuse_play` → confirm → open Infuse deep link |
| “Play Heat on Infuse” | Same; asks which edition if ambiguous |
| “Pause the Apple TV” / “skip on Infuse” | `infuse_transport` via HA remote |
| “Play X on the LG” | Still `plex_play` (Plex client on webOS) |

If Apple TV isn’t paired in HA, Hearth fails clearly with the setup steps above — it does not silently no-op or tell you to open the Plex app.

### Limits

- No Infuse now-playing / progress / webhook.
- Deep link needs a TMDB id; titles missing from Plex Guids and *arr will fail with a clear speak line.
- tvOS may prompt once to open the Infuse URL the first time.

## Tools

### Confirmation policy (lenient)

Routine house actions **run immediately** — no second “confirm” step:

- `ha_call_service` — lights, scenes, raw `media_player` (Denon, LG, Apple TV)
- `ha_media_control` — LG TV / Denon AVR / Apple TV turn_on/off, volume, source, play_media, transport
- `infuse_play` — open a library title in Infuse on the Apple TV (HA deep link)
- `infuse_transport` — pause / play / stop / skip via HA Apple TV remote
- `plex_play` — start a Plex library title on a Plex client (LG / Shield / …)
- `radarr_add` / `sonarr_add` / `overseerr_request` — grab / request titles
- `chief_of_staff` — escalate repo/feature/Gridways/calendar work
- `workspace_write` — sandboxed VAULT writes (not git)
- Searches, status, remember/list memory, food browse/cart, `web_search`

High-risk / irreversible / paid actions **default to dry-run** until `confirm=true`
(voice or UI Confirm chip):

- `thuisbezorgd_order` — places the food cart (spends money)
- `workspace_delete` — irreversible sandbox delete
- `docker_stop` — stops a house container
- `memory_forget` / `memory_export` / `memory_purge`

Read-only / inspect:

- `house_media` — speakable TV + AVR + Apple TV + Plex inventory (`GET /api/media`)
- `ha_list_entities`, `ha_get_state`
- `plex_now_playing`, `plex_search`, `plex_clients`, `plex_browse_genre`
- `radarr_search`, `sonarr_search`, `overseerr_search`
- `suggest_titles` — resolve recommended movie/TV titles into overlay cards (Overseerr/*arr metadata, public TMDB links)
- `web_search` — live public web (title, short snippet, source). Caps query length; refuses local/internal URLs and secret-fishing; does not fetch result pages.
- `thuisbezorgd_restaurants`, `thuisbezorgd_menu`, `thuisbezorgd_cart`, `thuisbezorgd_auth_status`
- `workspace_list`, `workspace_read`
- `docker_ps`, `docker_inspect`
- `memory_remember` (write, not gated), `memory_search`, `memory_list`

Command-center light tiles and Forget buttons may still send `confirm=true` — a click is the
confirmation for those UI paths. Auth / house-token gating is unchanged.

### Workspace skills

`workspace/skills/*.py` are loaded at boot and after `workspace_write` to a skill file. Example: `vault_echo`. Skills cannot see outside `workspace/`. Do not bind-mount `/volume1` here.

## Layout

```
hearth/          FastAPI runtime, agent loop, tools, voice gateway, house memory
hearth/ui/       Static command center (no Node build)
workspace/       Sandboxed files + skills
ha/              Home Assistant config (onboarding still required)
data/            Auth + memory SQLite (compose bind-mount; gitignores *.db)
docker-compose.yml
Dockerfile
.env.example
```

## Local checks (not on the NAS)

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Text chat without Docker:

```bash
cp .env.example .env
python -m hearth
```

Then `POST /api/chat` with `{"message":"what's playing"}` — you should see the Plex tool fire and a `media` glass overlay. `{"message":"what animation movies do we have"}` → `plex_browse_genre` (speakable Animation list). `GET /api/plex/library?genre=Animation` does the same without chat. `{"message":"suggest some sci-fi movies"}` / `{"message":"Can you show them on the UI?"}` → `suggest_titles` + suggestion cards on the same overlay (`POST /api/media/suggest` also works). `{"message":"what's the weather"}` → `weather` overlay. `{"message":"search the web for where to watch The Bear"}` → `web_search`. `{"message":"tell me about the movie Dune"}` → `plex_search` + media overlay. `{"message":"play The Endless on the Apple TV"}` should start `infuse_play` immediately. `{"message":"play The Endless on the LG"}` should start `plex_play` immediately. `{"message":"add a weather skill to the repo"}` should call `chief_of_staff`, not GitHub. `{"message":"download the movie Dune"}` should queue `radarr_add` immediately (no action/update guard cards). `{"message":"forget that I like dim lights"}` / food checkout still dry-run until confirm.

## Home Assistant devices

Hearth will not talk webOS, Denon, or Infuse protocol itself. After HA is on:

1. Add **LG webOS TV** (accept the pairing PIN on the TV).
2. Add **Denon AVR** / HEOS for the AVR-X3700H (same LAN as HA).
3. Add **Apple TV** (HA Apple TV integration / pyatv) — required for Infuse deep links.
4. Add lights (Hue, ZHA, Matter, …).
5. Paste a long-lived token into `HA_TOKEN`.
6. Check Developer Tools → States for the real `media_player.*` entity_ids. If they differ from
   the defaults, set `HA_TV_ENTITY`, `HA_AVR_ENTITY`, and `HA_APPLE_TV_ENTITY` in `.env` and
   recreate the hearth container.

For LAN discovery (Cast, some TVs), you may want host networking on the HA service — see comments in `docker-compose.yml`. Hearth itself stays on the `hearth` bridge.

Live URL for Hearth is **https://vault.taileff393.ts.net/** (Tailscale Serve → the app). Do not document or use `:8443` / `:8787` in the UI. Do **not** enable Tailscale Funnel. Hearth stays Tailscale-only; bind the app to LAN/Tailscale (or localhost behind Serve), never a WAN port-forward.

## Telegram drop-group inbox

A dedicated house Telegram group can act as a movie/series/TV **request inbox**. Titles and catalog links are searched and requested through **Overseerr** (movie and TV via `mediaType`). Radarr/Sonarr are used only for download progress / queue status on titles this inbox queued. Status (queued, progress, done, failed) is posted back into the **same** group. There is no WhatsApp / WAHA / Baileys path — Telegram Bot API only.

### Setup (Ruben)

1. Talk to [@BotFather](https://t.me/BotFather): `/newbot`, copy the token.
2. Add the bot to the house group. Prefer making it an admin (or at least able to read messages).
3. Disable privacy mode so the bot sees all group messages: BotFather → `/setprivacy` → **Disable**.
4. Get the group chat id (negative number for groups/supergroups). Easiest: temporarily add a “get ids” bot, or inspect `getUpdates` once after posting in the group.
5. On VAULT, put secrets in host `.env` only (never commit):

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC…
   TELEGRAM_CHAT_IDS=-1001234567890
   # optional house-member allowlist:
   # TELEGRAM_USER_IDS=111,222
   ```

6. Recreate the hearth container. Feature stays **off** until both token and chat id are set.
7. Long-polling (`getUpdates`) runs inside Hearth — no public webhook, no Funnel, no extra NAS docker sidecar. An optional localhost-only webhook (`TELEGRAM_WEBHOOK_LOCAL=true`, loopback clients only) exists for advanced setups; it is **not** the default.

### Behavior

- Only allowlisted `TELEGRAM_CHAT_IDS` are inboxes. Messages from other chats are ignored.
- Catalog links (IMDb / TMDB / TVDB / Trakt / JustWatch), plain `tt…` / `tmdb:` / `tvdb:` ids, and titles like `Annihilation (2018)` or `Severance S02E03` are requests. The group itself is the confirmation — no extra Hearth UI confirm.
- Magnets, `.torrent` files, and raw media attachments get a short in-group refusal (this is not a general downloader).
- Ambiguous titles get a top-3 disambiguation; reply `1` / `2` / `3`, or `all of them` / `de eerste` while that list is on screen (instant, no model). Bare titles without a year, season asks, plot descriptions in any language, corrections (“nee niet die…”, actor clues, misspellings), and short follow-ups always go through a conversation hop with gpt-4o: Overseerr catalog hits for that message are passed as `candidates` on the same turn, plus the last ~8 turns of that chat (or `OPENAI_MODEL` when it is already set to a named model other than mini). House chat keeps the existing `OPENAI_MODEL` default. The hop must not invent a search title that is not in the user text or the candidate list, and a new title/plot ask clears leftover `subject_title` / `offered` candidates from a prior grab. Rejected titles are remembered so a wrong first guess cannot trap the group in a 1–2 loop. Plot/vibe asks get one best gpt-4o guess and a confirm (“Did you mean Alien (1979)?”) before Overseerr queues — never a list-less “reply 1–1”. Unsure with 2+ real catalog hits → clarify with a numbered `1. Title (year)` list; never invent a grab. Exact catalog ids and `Title (YYYY)` grab immediately after TMDB/Overseerr resolve (IMDb `tt…` is never searched as a title string). Duplicate catalog rows are collapsed. Chit-chat / emoji / meta talk is ignored (no “which movie”). Magnets / torrents are rejected.
- Dedup (message id + title/year window), per-group rate limit, max title length, bot loop-prevention, and log redaction for `TELEGRAM_BOT_TOKEN` ship by default.
- Progress polls Radarr/Sonarr queue tools only for titles this inbox queued. One early “started and healthy” ping around ~5%, then silence until done / failed (manual status asks still use `radarr_queue` / `sonarr_queue`).

## What is stubbed vs live in v0.1

| Piece | Status |
| --- | --- |
| FastAPI runtime + UI + compose (incl. HA) | Live |
| Tool registry + agent loop | Live |
| Local intent router (no API key) | Live — playing, play-on-TV, lights, *arr/Overseerr grab, docker, workspace, CoS escalate, web search |
| OpenAI chat tools | Live when `OPENAI_API_KEY` is set |
| Realtime voice (GA WebRTC + sideband tools) | Live when `OPENAI_API_KEY` is set; UI is tap-to-talk duplex |
| `web_search` | Live via OpenAI hosted web search when `OPENAI_API_KEY` is set; optional `BRAVE_SEARCH_API_KEY` for structured results; otherwise fixtures (or DuckDuckGo HTML lite if mock is off). Never fetches result pages. |
| `/ws/voice` text fallback | Live protocol; not the disabled beta websocket |
| Whisper/TTS on fallback | Live when a key is set but Realtime is down |
| HA / Plex / *arr / Docker backends | Live with tokens/socket; otherwise fixtures |
| Telegram drop-group inbox | Live when `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_IDS` are set; long-poll inside Hearth. Otherwise off. |
| Thuisbezorgd / Just Eat Takeaway NL | Fixtures + confirm/dry-run always. Live paid submit needs partner `THUISBEZORGD_API_KEY` + session (no public consumer OAuth; no scrape). |
| Chief of Staff webhook | Live when `HEARTH_COS_WEBHOOK` is set; otherwise explicit not-configured |
| HA onboarding, TV/AVR pairing | Yours — service is included unconfigured |
| Auth | Login (bcrypt + X-Auth-Token + HttpOnly refresh). Optional `HEARTH_TOKEN` for machines |
| House memory | Live (SQLite + FTS5; optional OpenAI embeddings). Not deployed until this lands on VAULT |
| SMB / public internet | Not exposed. Don’t add it. |
