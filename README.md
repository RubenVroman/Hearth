# Hearth

House agent runtime for **Ruben Vroman** on **VAULT** (Synology DS1817+, DSM 7.3, x86_64).

Voice is the front door. Home Assistant is the device layer (lights, Denon AVR-X3700H, LG webOS TV). A thin command-center UI is optional and secondary — not a SaaS chatbot.

Hearth is meant to sit in Docker **next to** the existing stack (Plex, Sonarr, Radarr, Prowlarr, Overseerr, Gluetun). It does not replace that stack.

## What you get

| Surface | Role |
| --- | --- |
| Agent loop + tool registry | Lights/scenes/AVR/TV via HA, Plex now-playing/search, workspace files/skills, docker inspect |
| `GET /` command center | Now playing, lights/scenes, transcript, agent status. Dark, cinematic. |
| `WS /ws/voice` | OpenAI Realtime live voice, or the same protocol with a working text fallback |
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

4. Open the command center on the **LAN or Tailscale** address, never a WAN port-forward:

   `http://<vault-lan-or-tailscale>:8787`

5. Home Assistant onboarding: `http://<vault-lan-or-tailscale>:8123`  
   Create a long-lived token (Profile → Security), put it in `.env` as `HA_TOKEN`, then `docker compose up -d`.

`docker compose up` is enough to get a runnable runtime + UI. Without tokens, tools return house-shaped **fixtures** (living room lights, Denon, LG TV, Dune on Plex). Text chat still calls real tools against those mocks.

### Bind to Tailscale / LAN, not the internet

- Do **not** port-forward `8787` or `8123` on the router.
- Do **not** expose SMB for the agent. Workspace is a local bind mount only.
- Set `HEARTH_BIND` / `HA_BIND` in `.env` to the Tailscale IP (`100.x.y.z`) or a LAN IP if you want the ports off the rest of the host’s interfaces.
- Optional: set `HEARTH_TOKEN` so the UI/API require `X-Hearth-Token`.

The compose file publishes `0.0.0.0` by default so Tailscale and LAN both work. That is **not** the public internet unless you forward the port.

## Environment

| Variable | Live vs stub |
| --- | --- |
| `OPENAI_API_KEY` | **Live** text agent (chat completions + tools) and Realtime voice. Empty → local intent router + text fallback on `/ws/voice`. |
| `OPENAI_MODEL` | Chat model. Default `gpt-4o-mini`. |
| `OPENAI_REALTIME_MODEL` | Realtime model. Default `gpt-realtime`. |
| `HA_URL` | Default `http://homeassistant:8123` (compose DNS). |
| `HA_TOKEN` | **Live** HA REST. Empty → mocked lights/scenes/Denon/LG. |
| `PLEX_URL` | Existing Plex on the host. Default `http://host.docker.internal:32400`. |
| `PLEX_TOKEN` | **Live** Plex sessions/search. Empty → mocked now-playing. |
| `DOCKER_SOCKET` | Read-only socket is mounted. If missing → mocked container list (plex/sonarr/…/gluetun). |
| `WORKSPACE_PATH` | Inside the container, `/app/workspace`. |
| `HEARTH_MOCK_IF_UNCONFIGURED` | Default `true`. If a live call fails, fall back to fixtures instead of dying. |

Plex token: Plex Web → settings URL, or XML at `http://<plex>:32400/library/sections` while signed in — `X-Plex-Token` in the query. Do not commit it.

Reach Plex on the existing stack:

- `PLEX_URL=http://host.docker.internal:32400` (compose sets `host-gateway`)
- or the host LAN IP, e.g. `http://192.168.1.10:32400`

## Voice

`WS /ws/voice` is first-class.

**Live** (key present, Realtime reachable): Hearth opens `wss://api.openai.com/v1/realtime`, sends `session.update` with house tools, streams PCM16 24 kHz, and executes `function_call` items locally (`ha_*`, `plex_*`, `workspace_*`, `docker_*`) before returning `function_call_output`.

**Fallback** (no key, or Realtime connect failed): same client events still work.

| Client event | Meaning |
| --- | --- |
| `session.start` | Hello |
| `input_text` | `{text, confirm?}` — runs the agent loop |
| `input_audio.append` | `{audio}` base64 PCM16LE 24 kHz |
| `input_audio.commit` | Live: commit to Realtime. Fallback: Whisper if a key exists, else ask for text |
| `confirm` | Execute the pending destructive tool |
| `response.cancel` | Interrupt |

The UI hold-to-speak control uses this protocol. Text in the composer uses it too. Plug in `OPENAI_API_KEY` later — you do not rewrite the front door.

## Tools

Destructive tools **default to dry-run** unless `confirm=true`:

- `ha_call_service` — lights, scenes, `media_player` (Denon, LG)
- `workspace_write` / `workspace_delete`
- `docker_stop`

Read-only / inspect:

- `ha_list_entities`, `ha_get_state`
- `plex_now_playing`, `plex_search`
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

Then `POST /api/chat` with `{"message":"what's playing"}` — you should see the Plex tool fire.

## Home Assistant devices

Hearth will not talk webOS or Denon protocol itself. After HA is on:

1. Add **LG webOS TV**.
2. Add **Denon AVR** / HEOS for the AVR-X3700H.
3. Add lights (Hue, ZHA, Matter, …).
4. Paste a long-lived token into `HA_TOKEN`.

For LAN discovery (Cast, some TVs), you may want host networking on the HA service — see comments in `docker-compose.yml`. Hearth itself stays on the `hearth` bridge.

## What is stubbed vs live in v0.1

| Piece | Status |
| --- | --- |
| FastAPI runtime + UI + compose (incl. HA) | Live |
| Tool registry + agent loop | Live |
| Local intent router (no API key) | Live — “what’s playing”, lights, docker, workspace |
| OpenAI chat tools | Live when `OPENAI_API_KEY` is set |
| Realtime voice WebSocket + tool execution | Live protocol; connects when the key works |
| Whisper/TTS on fallback | Live when a key is set but Realtime is down |
| HA / Plex / Docker backends | Live with tokens/socket; otherwise fixtures |
| HA onboarding, TV/AVR pairing | Yours — service is included unconfigured |
| Auth | Optional `HEARTH_TOKEN` only; trust the LAN/Tailscale |
| SMB / public internet | Not exposed. Don’t add it. |
