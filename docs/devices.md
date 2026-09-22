# House devices — PetZero feeders, Tuya airco, KPT air purifier

Home Assistant is the device layer for these, exactly as it is for the Denon and
the LG TV. Hearth talks to HA over REST; it does not speak Tuya, Smart Life, or
PetZero protocol itself. Nothing works here until the device exists in HA.

## Where VAULT actually stands (audited 2026-09-22)

| Thing | State |
| --- | --- |
| `hearth-ha` | Up |
| HACS 2.0.5 | Installed |
| `tuya_local` 2026.9.1 | Files present under `custom_components/` |
| Devices adopted by `tuya_local` | **None** |
| HA entities | ~78 — lights, media players, sensors, switches |
| `climate` / `fan` / `humidifier` entities | **Zero** |
| Entities matching feeder / pet / purifier / tuya / airco / KPT | **Zero** |

Installing `tuya_local` through HACS only copies files onto disk. Home Assistant
does not load a custom integration until it has a config entry, and it creates
no entities until a device is added. That is the whole gap: **the integration is
installed, no device has been added.** Everything below is that one step.

Ask Hearth to confirm before and after — "tuya devices" in chat, `/devices` on
Telegram, the `ha_discover_entities` tool, or `GET /api/devices/discover`. It
reads HA's loaded-integration list, so it can tell "`tuya_local` has adopted
nothing" apart from "this device is not paired", which look identical from
entities alone and need different fixes.

## Adding a device (`tuya_local`, cloud-assisted)

Do this once per device. The cloud step is only used to fetch the device id and
local key; control afterwards is LAN-only.

**Before you start:** have the Smart Life (or Tuya Smart) app on your phone with
all three devices already working in it, and have Home Assistant open on a
second screen so you can scan a QR code.

1. Get your **User Code** in the phone app: **Me** → gear icon (top right) →
   **Account and Security** → **User Code** (at the bottom). Copy it.
2. In Home Assistant: **Settings → Devices & Services → Add Integration** →
   search **Tuya Local**.
3. Choose **SmartLife cloud-assisted device setup**.
4. Enter the User Code from step 1.
5. Home Assistant shows a **QR code**. In the phone app tap **+** (top right) →
   **Scan**, scan it, and approve the login.
6. Pick the device from the list it returns. If the device sits behind a Zigbee
   or Bluetooth hub, pick the hub as well.
7. Let local discovery run — it scans the subnet for about 18 seconds to find
   the device's IP. **Close the Smart Life app while this runs**; an open app
   holds the device's single local connection slot and discovery will miss it.
8. **Stage one** (connection): device id and local key are pre-filled, and the
   IP too if discovery found it. Leave **protocol version** on *Auto* unless you
   know otherwise. If the IP is blank, fill it from your router's lease list.
9. **Stage two** (device type): choose the closest match from the offered list —
   it is already filtered to types that match what the device reported. Name it.
10. Repeat from step 2 for the next device. Cloud login is cached, so devices
    two and three are quicker.

### Per device

**Tuya airco** — pick the air-conditioner / heat-pump device type. You want a
`climate.*` entity. Check under **Developer Tools → States** that it reports
`hvac_modes`, and `fan_modes` if the unit has fan speeds: Hearth validates any
requested mode against that list rather than guessing.

**KPT Air Purifier** — pick the air-purifier device type. You want a `fan.*`
entity with `preset_modes`. Some OEM builds land as `humidifier.*` or a plain
`switch.*`; that still works for on/off, but speeds and presets need the `fan`
entity, so re-pair with a purifier device type if you get a bare switch.

**PetZero feeders** — pick the pet-feeder device type. You want a `button.*`
(sometimes `switch.*`) for manual feed. These usually publish companions too: a
`number.*` for portion size, a `switch.*` for the built-in schedule, and
sensors. Hearth ignores the companions when looking for the feed control, and
uses the portion number when you ask for more than one portion. Add each feeder
separately.

### Manual setup instead

If cloud login fails, **Add Integration → Tuya Local → Manual** and enter the
device id, local key, and IP yourself. Getting a local key without the cloud is
the fiddly part — see `DEVICE_DETAILS.md` in the
[make-all/tuya-local](https://github.com/make-all/tuya-local) repo.

A known QR-login failure was fixed in Home Assistant Core 2026.3; VAULT is well
past that, so if the QR step fails it is worth retrying with a fresh **Cloud**
login before falling back to manual.

## Telling Hearth which entity is which

Ask `ha_discover_entities` and paste its `env_suggestions` into the host `.env`.
Nothing in Hearth hardcodes an entity id, because Tuya object ids depend on how
a device was paired.

```bash
# One pinned id per role. Set these once discovery shows the real ones.
HA_CLIMATE_ENTITY=climate.<your_airco>
HA_PURIFIER_ENTITY=fan.<your_purifier>
HA_FEEDER_ENTITY=button.<your_feeder>

# Optional feeder companions.
HA_PET_FEEDER_PORTION_ENTITIES=number.<your_feeder>_portion
HA_PET_FEEDER_SCHEDULE_ENTITIES=switch.<your_feeder>_schedule
```

The `HA_*_ENTITIES` candidate lists are the search order used when nothing is
pinned. When several entities fit equally, Hearth returns the candidates and
asks — picking a Tuya relay at random is worse than a question.

## Tools

| Tool | Purpose |
| --- | --- |
| `ha_discover_entities` | Filter HA entities by domain and keyword, per-role readiness, integration diagnosis, `.env` suggestions. Read-only. |
| `house_climate` | `status` / `on` / `off` / `warmer` / `cooler` / `set` / `fan_mode` / a named hvac mode |
| `house_feeder` | `feed` / `status` / `schedule_status` / `schedule_on` / `schedule_off`, with `portions` and `force` |
| `house_purifier` | `status` / `on` / `off` / `toggle` / `set_speed` / `set_mode` |
| `house_comfort` | Climate, indoor air, purifier, and feeder in one snapshot |

Modes and fan speeds are matched against what the entity actually publishes. An
unsupported request comes back with the real list rather than being sent and
silently ignored.

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
| “warmer”, “cooler” | nudge the setpoint half a degree |
| “purifier on”, “luchtreiniger uit” | power |
| “purifier 40%”, “luchtreiniger op auto” | speed / preset |
| “voerschema uit” | feeder schedule off |

Telegram also accepts `/feed`, `/feed 2`, `/airco 21`, `/purifier sleep`,
`/devices`, alongside the existing `/house`, `/lights`, `/scenes`, `/covers`.

Matching is deliberately narrow, because unrecognised Telegram text falls
through to the Overseerr search: a bare **“Cats”** stays a film, **“Feed”**
stays a title, and **“airco 2024”** is not 20 degrees.

## Feeding safety

Dispensed food cannot be recalled, and voice plus Telegram make an accidental
second meal easy. So a repeat feed inside `HA_PET_FEEDER_COOLDOWN_SECONDS`
(default 600) is refused with the remaining wait until you say “feed them
anyway”, portions are capped by `HA_PET_FEEDER_MAX_PORTIONS` (default 6), and
an unavailable feeder is reported rather than quietly treated as fed.

## Jev gates every device call

`house_feeder`, `house_climate`, and `house_purifier` are registered with
`jev_gated=True`. The gate lives in the **tool registry**, so chat, voice,
Telegram, and `POST /api/invoke` pass through one decision instead of each
surface inventing its own guard. It is a no-op while Jev is disabled (the
default), observes-only in shadow mode, and fails open on any API error — a
flaky System One call must not leave the pets unfed. See [jev.md](jev.md).

## Troubleshooting

**Hearth says no Tuya integration is loaded.** Expected until step 9 above
completes for at least one device. HACS put the files on disk; Home Assistant
loads the integration when it has a config entry.

**Discovery cannot find the device's IP (step 7).** Close the Smart Life app and
retry — Tuya devices accept one local connection at a time. Confirm the device
answers on the Tuya LAN port: `nc -vz <device-ip> 6668`. If HA runs in a
container without host networking it cannot see the broadcast; enter the IP by
hand in stage one.

**Entities appear but Hearth still says not paired.** The friendly name probably
misses the keywords. Run `ha_discover_entities` to see what HA has and pin the id
with `HA_CLIMATE_ENTITY` / `HA_PURIFIER_ENTITY` / `HA_FEEDER_ENTITY`.

**“More than one … matches”.** Two candidates — a second purifier, or another
Tuya relay with a similar name. Pin the one you mean.

**“… only does on and off”.** The purifier adopted as a `switch`. Re-pair it
with a purifier device type to get the `fan` entity and its speeds.

**“This feeder does not expose its schedule”.** Normal. Most Tuya feeders keep
meal times on the device. Set them in the app, or point
`HA_PET_FEEDER_SCHEDULE_ENTITIES` at the schedule switch if one exists.

**“has no 'x' mode. Available: …”.** The unit really does not offer that mode.
The list in the message is what it published to Home Assistant.
