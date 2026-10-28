# Hearth on the LG webOS TV

Living-room shell for the **OLED65G1RLA** (webOS 6, LAN `192.168.2.19`, hostname `lgwebostv.home`).

The TV runs a packaged web app (`webos/com.hearth.tv`). That package is not a second house brain. It opens Hearth’s `/tv` route fullscreen. Buttons call the existing Hearth tools, which call Home Assistant, Plex, and Infuse. The app does not speak the webOS SSAP protocol itself, and it does not root the TV.

## What you get

| Control | What it calls |
| --- | --- |
| Now playing | Plex sessions, plus LG / Denon / Apple TV state from HA |
| Quieter / louder / mute | `ha_media_control` on the Denon (`HA_AVR_ENTITY`). Receiver-centric, same as voice |
| Movie night | `media_activity` — Movie night scene, then the Apple TV path |
| Quiet hours, Good night | `house_scene` presets, only if HA has that scene |
| Apple TV / TV Audio / Media off | `media_activity` (`apple_tv`, `tv`, `off`) |
| Play / pause / stop | `infuse_transport` via the HA Apple TV entity |
| Infuse | Apple TV activity, then `play_media` with `INFUSE_APP_ID` (`com.firecore.infuse`, type `app`) |
| Videoland | `videoland_play` (`select_source`). Titles and profiles stay on the remote — the tool says so |
| Netflix, Prime Video | `select_source` using the friendly name on the TV `source_list` |
| Continue watching | Plex On Deck, played with `infuse_play` |
| Requests | Stub. Overseerr search/request stays on chat and Telegram until a queue read exists |

Netflix, Prime, and Videoland tiles **open the app**. They do not start a title, strip ads, or remux a stream.

## Reach

Use the LAN. VAULT is `192.168.2.26`. The webOS setup screen defaults to `http://192.168.2.26:8787`.

- Do **not** port-forward `8787` or `8123`.
- Do **not** put `HEARTH_TOKEN` in the URL. Hearth ignores `?token=`.
- Tailscale (`https://vault.taileff393.ts.net`) is the right address for a laptop. The TV can use it only if something on the LAN routes to the tailnet (a subnet router). The TV app does not run Tailscale.
- `HEARTH_BIND` must accept LAN connections. The compose file publishes `0.0.0.0`, which covers LAN and Tailscale. Binding only to the Tailscale IP hides Hearth from the TV.
- For plain HTTP, set `COOKIE_SECURE=false` so the refresh cookie sticks. Or skip cookies and paste `HEARTH_TOKEN` on the TV setup screen (`X-Hearth-Token`). The token stays in the TV webview’s storage for the Hearth origin. It is not written to Hearth logs.

Home Assistant still has to know the set. After pairing, `HA_TV_ENTITY` may differ from the default `media_player.lg_webos_tv` (for example `media_player.lg_webos_tv_oled65g1rla`). Apple TV launch needs `HA_APPLE_TV_ENTITY`. Denon activities use `HA_AVR_APPLE_TV_SOURCE` (default `Media Player`) and `HA_AVR_TV_SOURCE` (default `TV Audio`).

### Pairing the tiles

| Tile | What has to be true in HA |
| --- | --- |
| Infuse / continue | Apple TV integration paired. Infuse installed, library already signed in. |
| TV Audio / movie night / Videoland / Netflix / Prime | LG webOS integration paired. The app’s friendly name is on the TV `source_list`. |
| Prime Video | Often `Amazon Prime Video`, `Prime Video`, or `Amazon`. If none of those are on the list, the tile says so and does not guess. |
| Videoland | `VIDEOLAND_SOURCE` (default `Videoland`). `VIDEOLAND_APP_ID` is optional and still cannot start a title. |
| Lights | HA scenes named Movie night, Quiet hours, Good night — or `HA_MOVIE_NIGHT_SCENE` for the activity. |

## Magic Remote

The page is a 10-foot layout: large type, 88px controls, an ember focus ring on `:focus`. The screen is dark tvOS-style glass — frosted panels, soft blur, and rounded tiles on a graphite field.

- **Arrows** move focus. Left/right inside a text field move the caret.
- **Enter / OK** activates the focused button. Pointer clicks work too; every control is a `<button>`.
- **Back** (webOS key code 461) closes Setup, or returns to the address screen. The package sets `disableBackHistoryAPI` so the page receives Back. From the address screen, Back leaves the app (`PalmSystem.platformBack`).
- The icon opens the address screen with **Open Hearth** focused. One OK press goes to `/tv`. Back from the dashboard returns here (the package does not auto-redirect, so changing the address does not loop). Clicking the icon again while the app is already open (`webOSRelaunch`) jumps straight back to `/tv`.

webOS 6 on the G1 does not run the phone command center well (small type, orb, composer). Use `/tv`.

## Package

```
webos/com.hearth.tv/
  appinfo.json
  index.html
  shell.css
  shell.js
  icon.png
  largeIcon.png
  resources/en/US/appinfo.json
  resources/nl/NL/appinfo.json
```

App id: `com.hearth.tv`. Icons are placeholders (ember mark on the house black).

Build with the webOS TV CLI (`@webos-tools/cli`, which provides `ares-package` / `ares-install` / `ares-launch`). The older TV SDK from the LG developer site ships the same commands.

```bash
npm install -g @webos-tools/cli
ares-package webos/com.hearth.tv -o dist
```

That writes `dist/com.hearth.tv_1.0.0_all.ipk`. `dist/` is gitignored.

### Developer Mode (no root)

1. On the TV, install **Developer Mode** from the LG Content Store and sign in with the LG account.
2. Turn Dev Mode on and reboot when it asks. Leave the app open; it shows the passphrase and key server.
3. From a machine on the same LAN:

   ```bash
   ares-setup-device --add lgwebostv \
     -i "host=192.168.2.19" \
     -i "port=9922" \
     -i "username=prisoner"
   ares-setup-device --modify lgwebostv -i "passphrase=<from the TV app>"
   ares-novacom --device lgwebostv --getkey
   ares-install --device lgwebostv dist/com.hearth.tv_1.0.0_all.ipk
   ares-launch --device lgwebostv com.hearth.tv
   ```

   `lgwebostv.home` works in place of `192.168.2.19` when mDNS resolves. Username `prisoner` is the Developer Mode default.

4. The icon appears as **Hearth**. Arrow to **Open Hearth** (or let a saved address jump straight in) and sign in.

Developer Mode sessions expire (the TV app shows the remaining time). Extend them in that app. This path does not root the set.

### Homebrew Channel

If the TV is already on Homebrew / webOS Homebrew Channel, you can sideload the same IPK from a LAN URL or with `ares-install` over the Homebrew SSH user. This repo does not install Homebrew and does not root the TV. Developer Mode above is enough.

## Check it without the TV

CI does not boot a webOS device. With Hearth running on the LAN (or locally):

1. Open `http://127.0.0.1:8787/tv` (or the VAULT LAN URL).
2. Set the browser viewport to **1920×1080**.
3. Sign in, or send `X-Hearth-Token` from the setup screen.
4. Tab or the arrow keys should move the ember ring. Enter runs a button. Back (or the browser Back key, key code 461 is TV-only) closes setup.
5. Fixture mode (no HA/Plex tokens) still shows Dune, the Denon, Videoland/Netflix as launchable, and Prime Video as not on the source list.

`pytest tests/test_tv.py` covers the board, the button wiring against the fixture house, the public `/tv` page, and the IPK manifest.
