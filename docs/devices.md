# House devices — PetZero feeders, Tuya airco, KPT air purifier

Home Assistant is the device layer for these, exactly as it is for the Denon and
the LG TV. Hearth talks to HA over REST; it does not speak Tuya, Smart Life, or
PetZero protocol itself.

## Pair them in Home Assistant first

Prefer **Tuya Local** over the Tuya cloud integration. Local keeps control on the
LAN, survives a WAN outage, and has no cloud polling limits — which matters when
"feed the cats" is expected to work instantly.

1. In HA: Settings → Devices & Services → Add Integration.
2. **Tuya Local** for the air conditioner and the KPT Air Purifier. Each device
   needs its local key; pull it from the Tuya IoT / Smart Life developer console
   once, then the device runs offline.
   (The cloud **Tuya** integration also works and produces the same entity
   domains — Hearth does not care which one you used.)
3. The **PetZero** feeder is Tuya OEM hardware too; it adopts the same way.
4. Confirm each device has the entity you expect:

| Device | Expected HA domain | Typical entity |
| --- | --- | --- |
| Pet feeder, manual feed | `button` (sometimes `switch`) | `button.<name>_feed` |
| Pet feeder, portion count | `number` | `number.<name>_portion` |
| Pet feeder, schedule on/off | `switch` | `switch.<name>_schedule` |
| Air conditioning | `climate` | `climate.<name>` |
| Air purifier | `fan` (sometimes `humidifier` or `switch`) | `fan.<name>` |

## Then let Hearth find the entity ids

Nothing is hardcoded. Ask Hearth instead of guessing:

- Chat or voice: “which entities are there for the feeder”, “tuya devices”
- Telegram: `/devices`
- Tool: `ha_discover_entities` (`kind=feeder|airco|purifier|tuya|all`)

It returns the candidate entities per role, what `.env` currently points at, and
ready-to-paste `.env` lines under `env_suggestions`. Copy those into the host
`.env` and restart:

```bash
HA_PET_FEEDER_ENTITIES=button.pet_feeder_feed
HA_PET_FEEDER_PORTION_ENTITIES=number.pet_feeder_portion
HA_PET_FEEDER_SCHEDULE_ENTITIES=switch.pet_feeder_schedule
HA_AIRCO_ENTITIES=climate.airco
HA_AIR_PURIFIER_ENTITIES=fan.air_purifier
```

Each variable is an **ordered candidate list**, not a single id. Hearth takes the
first candidate HA actually has, then falls back to keyword discovery. If several
entities fit equally well it returns the matches and asks — switching the wrong
Tuya relay is worse than asking.

## Tools

| Tool | What it does |
| --- | --- |
| `pet_feeder_feed` | Dispense now. `portions`, `feeder`, `force`. |
| `pet_feeder_schedule` | `status` / `on` / `off` for the feeder's own timetable. |
| `airco_control` | `status` / `on` / `off` / `set_temperature` / `set_mode` / `set_fan_mode`. |
| `air_purifier_control` | `status` / `on` / `off` / `toggle` / `set_speed` / `set_mode`. |
| `house_devices` | One speakable snapshot of all three. |
| `ha_discover_entities` | Read-only wiring aid (above). |

Writes are **observed**, not assumed: after every service call Hearth re-reads
the entity and reports whether the requested state actually landed. An accepted
HTTP 200 with no state change comes back as a failure, not a success.

## Phrases

English and Dutch, on chat, voice, and Telegram:

| Say | Result |
| --- | --- |
| “feed the cats”, “geef de katten eten”, “voer de katten” | one portion now |
| “feed the cats twice”, “feed the cats 3 portions” | multiple portions |
| “feed them anyway”, “voer de katten toch” | overrides the cooldown |
| “airco 21”, “zet de airco op 21” | set 21 °C (starts the unit if it is off) |
| “airco on”, “zet de airco uit” | power |
| “airco op koelen”, “airco fan high” | mode / fan speed |
| “is the airco on” | status, not a power command |
| “purifier on”, “luchtreiniger uit” | power |
| “purifier 40%”, “luchtreiniger op auto” | speed / preset |
| “turn off automatic feeding”, “voerschema uit” | feeder schedule |

Telegram also accepts `/feed`, `/feed 2`, `/airco 21`, `/purifier sleep`,
`/devices`.

Matching is deliberately narrow. On Telegram anything unrecognised falls through
to the Overseerr media search, so a bare **“Cats”** stays a film and **“Feed”**
stays a title.

## Feeding safety

Dispensed food cannot be recalled, and voice plus Telegram make an accidental
second meal easy. So:

- A repeat feed inside `HA_PET_FEEDER_COOLDOWN_SECONDS` (default 600) is refused
  with the remaining wait. “Feed them anyway” overrides it.
- Portions are capped by `HA_PET_FEEDER_MAX_PORTIONS` (default 6).
- When a `number` portion entity exists, N portions is one trigger with the count
  set. Without one, Hearth triggers N times and reports `presses`.
- An `unavailable` feeder is reported as such. Hearth never says it fed.

## Jev gates every device call

`pet_feeder_feed`, `pet_feeder_schedule`, `airco_control`, and
`air_purifier_control` are registered with `jev_gated=True`. The gate lives in
the **tool registry**, so chat, voice, Telegram, and `POST /api/invoke` all pass
through the same decision instead of each surface inventing its own guard.

The gate calls `house_device_system_one_questions()` — the `device_ask` Choice
plus confirm / cancel / risk. It deliberately omits the media router: a feeder
turn is never an Overseerr ask, and the extra questions would only cost money.

Behaviour follows the rest of Jev (see [jev.md](jev.md)):

| State | Effect |
| --- | --- |
| `HEARTH_JEV_ENABLED=false` (default) | Gate is a no-op. |
| Enabled + `HEARTH_JEV_SHADOW=true` (default) | Consulted and logged, never blocks. |
| Enabled + shadow off | High-confidence `is_cancel` (≥ `HEARTH_JEV_CANCEL_THRESHOLD`) or a `do_not_auto_run` risk level blocks the call. |
| Missing key, API error, empty message | Fails open. A flaky System One call must not leave the pets unfed. |

A blocked call returns `blocked_by: "jev"` and never reaches Home Assistant.

## Troubleshooting

**“No Home Assistant entity looks like the …”** — the device is not paired, or it
paired under a name Hearth's keywords miss. Run `ha_discover_entities` and set
the matching `HA_*_ENTITIES` variable.

**“More than one Home Assistant entity could be the …”** — you have two
candidates (two purifiers, or a second Tuya relay with a similar name). Set the
env var to the one you mean.

**“… only does on and off”** — the purifier adopted as a `switch`, so HA exposes
no speed. Re-pair it with Tuya Local to get the `fan` entity.

**“This feeder does not expose its schedule”** — normal. Most Tuya feeders keep
meal times on the device. Set them in the feeder's own integration; Hearth will
not pretend it changed a timetable it cannot see.

**“accepted the command, but the requested state was not observed”** — HA took
the call and the device did not move. Usually a Tuya cloud entity that went
stale, or a unit that rejects the mode. Check the device in the HA UI.
