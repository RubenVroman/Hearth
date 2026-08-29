# Hearth

House agent runtime for **Ruben Vroman** on **VAULT** (Synology DS1817+, DSM 7.3, x86_64).

Voice is the front door. Home Assistant is the device layer (lights, Denon AVR-X3700H, LG webOS TV). A thin command-center UI is optional and secondary — not a SaaS chatbot.

Hearth is meant to sit in Docker **next to** the existing stack (Plex, Sonarr, Radarr, Prowlarr, Overseerr, Gluetun). It does not replace that stack.

## What you get

| Surface | Role |
| --- | --- |
| Agent loop + tool registry | Lights/AVR/TV via HA, *arr/Overseerr grab/request, Plex now-playing + play-on-client, Thuisbezorgd food order, workspace, docker inspect, Chief of Staff escalate |
| `GET /` command center | Now playing, lights/scenes, transcript, agent status. Requires login. |
| `GET /login` | Email + password. House FastAPI auth (X-Auth-Token + HttpOnly refresh cookie). |
| `POST /api/realtime/calls` | GA OpenAI Realtime over WebRTC (ChatGPT-app voice). Browser mic, barge-in, house tools on a sideband. |
| `WS /ws/voice` | Text fallback only. Never the disabled beta Realtime websocket. |
| `workspace/` | Sandboxed “build whatever I ask” directory. Not the whole NAS. |
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

4. Set `APP_SECRET_KEY` (long random string) and create the first user (see Login below). Open `https://vault.taileff393.ts.net:8443/login` on Tailscale, never a WAN port-forward.

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

Users live in SQLite at `HEARTH_AUTH_DB` (compose bind-mounts `./data`). No Postgres.

`COOKIE_SECURE=true` is correct behind Tailscale HTTPS. Set `COOKIE_SECURE=false` only if you are hitting plain HTTP on the LAN.

Public without a session: `/login`, `/auth/token`, `/auth/session/refresh`, `/auth/session/logout`, `/health`, static files needed to render login. Everything else (including `/` and `/api/status`) requires a session or `X-Hearth-Token`.

## Environment

| Variable | Live vs stub |
| --- | --- |
| `OPENAI_API_KEY` | **Live** text agent (chat completions + tools) and GA Realtime WebRTC voice. Empty → local intent router + text fallback on `/ws/voice`. |
| `OPENAI_MODEL` | Chat model. Default `gpt-4o-mini`. |
| `OPENAI_REALTIME_MODEL` | Conversational Realtime model. Default `gpt-realtime-2.1` (ChatGPT-app equivalent). |
| `HA_URL` | Default `http://homeassistant:8123` (compose DNS). |
| `HA_TOKEN` | **Live** HA REST. Empty → mocked lights/scenes/Denon/LG. |
| `HA_TV_ENTITY` | LG webOS `media_player` entity_id. Default `media_player.lg_webos_tv`. Set after HA pairing if different. |
| `HA_AVR_ENTITY` | Denon AVR entity_id. Default `media_player.denon_avr_x3700h`. |
| `PLEX_URL` | Existing Plex on the host. Default `http://host.docker.internal:32400`. |
| `PLEX_TOKEN` | **Live** Plex sessions/search/clients/play. Empty → mocked now-playing + library/play fixtures. |
| `PLEX_DEFAULT_PLAYER` | Optional default client name substring (e.g. `Apple TV`) when “the TV” is ambiguous. |
| `RADARR_URL` / `RADARR_API_KEY` | **Live** movie search/add. Default URL `http://host.docker.internal:7878`. Empty key → fixtures. |
| `SONARR_URL` / `SONARR_API_KEY` | **Live** series search/add. Default `http://host.docker.internal:8989`. |
| `OVERSEERR_URL` / `OVERSEERR_API_KEY` | **Live** request front door. Default `http://host.docker.internal:5055`. |
| `HEARTH_DELIVERY_STREET` / `POSTCODE` / `CITY` | House delivery address for Thuisbezorgd. Empty → browse/order refuse until set. Never invent an address in code. |
| `THUISBEZORGD_API_KEY` | **Live** partner JE-API-KEY. Empty → fixtures only. Just Eat Takeaway has no public self-serve consumer ordering API. |
| `THUISBEZORGD_SESSION_TOKEN` / `EMAIL` / `PASSWORD` | Server-side consumer auth for live submit (with API key). Never sent to the browser; never logged. |
| `THUISBEZORGD_API_BASE` / `TENANT` | Default `https://nl.api.just-eat.io` / `nl`. |
| `HEARTH_COS_WEBHOOK` | **Live** Chief of Staff POST. Empty → tool returns “not configured” (not fake success). |
| `HEARTH_COS_WEBHOOK_KEY` | Optional. Sent as `Authorization: Bearer <key>`. |
| `HEARTH_COS_REPO` | Default `RubenVroman/Hearth`. |
| `DOCKER_SOCKET` | Read-only socket is mounted. If missing → mocked container list (plex/sonarr/…/gluetun). |
| `WORKSPACE_PATH` | Inside the container, `/app/workspace`. |
| `HEARTH_MOCK_IF_UNCONFIGURED` | Default `true`. If a live call fails, fall back to fixtures instead of dying. |
| `APP_SECRET_KEY` | Required to sign JWTs. Empty → nobody can log in. |
| `HEARTH_ADMIN_EMAIL` / `HEARTH_ADMIN_PASSWORD` | Bootstrap first superuser if the users table is empty. Leave empty after that. |
| `HEARTH_TOKEN` | Optional **machine** bypass (`X-Hearth-Token` header). Not a browser login. |

Plex token: Plex Web → settings URL, or XML at `http://<plex>:32400/library/sections` while signed in — `X-Plex-Token` in the query. Do not commit it.

Reach Plex on the existing stack:

- `PLEX_URL=http://host.docker.internal:32400` (compose sets `host-gateway`)
- or the host LAN IP, e.g. `http://192.168.1.10:32400`

## Voice

Live voice is the **GA OpenAI Realtime API over WebRTC** — the same family as ChatGPT Advanced Voice / GPT Realtime. It is **not** hold-to-talk, and it is **not** the old beta websocket.

**Live** (key present): the browser opens `RTCPeerConnection`, POSTs SDP to Hearth `POST /api/realtime/calls`, and Hearth forwards a multipart `sdp` + `session` to `https://api.openai.com/v1/realtime/calls` with the NAS `OPENAI_API_KEY`. No `OpenAI-Beta` header. Mic uses browser AEC (`echoCancellation`) plus a client speech-band barge-in gate (`/static/vad.js`): while Hearth is talking, the outbound track stays muted until consecutive speech-like frames are seen, so TV/HVAC/clatter should not cut playback. The GA session also enables `audio.input.noise_reduction` (`near_field`) before semantic VAD. Remote audio plays through a hidden `<audio>` element. House tools (`ha_*`, `plex_*`, `radarr_*`, `sonarr_*`, `overseerr_*`, `workspace_*`, `docker_*`, `chief_of_staff`, `end_call`, Thuisbezorgd, weather) run on Hearth over a sideband `wss://api.openai.com/v1/realtime?call_id=…`.

**Close of call:** when the conversation is finished (goodbye / done / nothing left), the model calls `end_call`. After that response completes, Hearth closes the sideband and the UI hangs up the WebRTC peer connection so the session does not stay open idle.

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
- LG TV / Denon AVR power, volume, source → `ha_media_control` (prefer over raw `ha_call_service`)
- House media snapshot (TV + AVR + Plex) → `house_media` or `GET /api/media`
- What's playing → `plex_now_playing`
- Play a specific library title on Apple TV / LG / living-room Plex → `plex_play` (optional `plex_search` / `plex_clients`). Starts playback on the client via the Plex Media Server remote API — not HA `play_media` on the webOS entity.
- Download / grab a **movie** → Radarr (`radarr_search` / `radarr_add`)
- Download / grab a **show** → Sonarr (`sonarr_search` / `sonarr_add`)
- “Request X” → Overseerr (`overseerr_search` / `overseerr_request`), the request front door that feeds *arr
- Food / Thuisbezorgd → `thuisbezorgd_restaurants` → `thuisbezorgd_menu` → `thuisbezorgd_cart` → `thuisbezorgd_order` (confirm to place)

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

Auth: `Authorization: Bearer <HEARTH_COS_WEBHOOK_KEY>` when the key is set. Writes still default to dry-run until `confirm=true` (voice or UI confirm is enough). If `HEARTH_COS_WEBHOOK` is empty, the tool says it is not configured.

## Tools

Destructive tools **default to dry-run** unless `confirm=true`:

- `ha_call_service` — lights, scenes, raw `media_player` (Denon, LG)
- `ha_media_control` — LG TV / Denon AVR turn_on/off, volume, source, play_media
- `plex_play` — start a Plex library title on a Plex client (Apple TV / LG / …)
- `radarr_add` / `sonarr_add` / `overseerr_request`
- `thuisbezorgd_order` — places the food cart (spends money)
- `workspace_write` / `workspace_delete`
- `docker_stop`
- `chief_of_staff`

Read-only / inspect:

- `house_media` — speakable TV + AVR + Plex inventory (`GET /api/media`)
- `ha_list_entities`, `ha_get_state`
- `plex_now_playing`, `plex_search`, `plex_clients`
- `radarr_search`, `sonarr_search`, `overseerr_search`
- `thuisbezorgd_restaurants`, `thuisbezorgd_menu`, `thuisbezorgd_cart`, `thuisbezorgd_auth_status`
- `workspace_list`, `workspace_read`
- `docker_ps`, `docker_inspect`

The command-center light tiles send `confirm=true` because a click is the confirmation.

### Workspace skills

`workspace/skills/*.py` are loaded at boot and after a confirmed `workspace_write`. Example: `vault_echo`. Skills cannot see outside `workspace/`. Do not bind-mount `/volume1` here.

## Layout

```
hearth/          FastAPI runtime, agent loop, tools, voice gateway
hearth/ui/       Static command center (no Node build)
workspace/       Sandboxed files + skills
ha/              Home Assistant config (onboarding still required)
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

Then `POST /api/chat` with `{"message":"what's playing"}` — you should see the Plex tool fire. `{"message":"play The Endless on the Apple TV"}` dry-runs `plex_play` until confirm. `{"message":"add a weather skill to the repo"}` should call `chief_of_staff`, not GitHub. `{"message":"download the movie Dune"}` should dry-run `radarr_add`.

## Home Assistant devices

Hearth will not talk webOS or Denon protocol itself. After HA is on:

1. Add **LG webOS TV** (accept the pairing PIN on the TV).
2. Add **Denon AVR** / HEOS for the AVR-X3700H (same LAN as HA).
3. Add lights (Hue, ZHA, Matter, …).
4. Paste a long-lived token into `HA_TOKEN`.
5. Check Developer Tools → States for the real `media_player.*` entity_ids. If they are not
   `media_player.lg_webos_tv` / `media_player.denon_avr_x3700h`, set `HA_TV_ENTITY` and
   `HA_AVR_ENTITY` in `.env` and recreate the hearth container.

For LAN discovery (Cast, some TVs), you may want host networking on the HA service — see comments in `docker-compose.yml`. Hearth itself stays on the `hearth` bridge.

Live URL for Hearth is **https://vault.taileff393.ts.net/** (Tailscale Serve → the app). Do not document or use `:8443` / `:8787` in the UI.

## What is stubbed vs live in v0.1

| Piece | Status |
| --- | --- |
| FastAPI runtime + UI + compose (incl. HA) | Live |
| Tool registry + agent loop | Live |
| Local intent router (no API key) | Live — playing, play-on-TV, lights, *arr/Overseerr grab, docker, workspace, CoS escalate |
| OpenAI chat tools | Live when `OPENAI_API_KEY` is set |
| Realtime voice (GA WebRTC + sideband tools) | Live when `OPENAI_API_KEY` is set; UI is tap-to-talk duplex |
| `/ws/voice` text fallback | Live protocol; not the disabled beta websocket |
| Whisper/TTS on fallback | Live when a key is set but Realtime is down |
| HA / Plex / *arr / Docker backends | Live with tokens/socket; otherwise fixtures |
| Thuisbezorgd / Just Eat Takeaway NL | Fixtures + confirm/dry-run always. Live paid submit needs partner `THUISBEZORGD_API_KEY` + session (no public consumer OAuth; no scrape). |
| Chief of Staff webhook | Live when `HEARTH_COS_WEBHOOK` is set; otherwise explicit not-configured |
| HA onboarding, TV/AVR pairing | Yours — service is included unconfigured |
| Auth | Login (bcrypt + X-Auth-Token + HttpOnly refresh). Optional `HEARTH_TOKEN` for machines |
| SMB / public internet | Not exposed. Don’t add it. |
