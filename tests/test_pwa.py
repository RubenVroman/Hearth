from pathlib import Path

from fastapi.testclient import TestClient

from hearth.app import app

PNG = b"\x89PNG"
UI = Path(__file__).resolve().parents[1] / "hearth" / "ui" / "static"


def test_manifest_is_public_standalone_hearth():
    with TestClient(app) as client:
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert "application/manifest+json" in response.headers["content-type"]
        body = response.json()
        assert body["name"] == "Hearth"
        assert body["short_name"] == "Hearth"
        assert body["display"] == "standalone"
        assert body["start_url"] in {"/", "/login"}
        assert body["theme_color"] == "#070604"
        assert body["background_color"] == "#070604"
        srcs = {icon["src"] for icon in body["icons"]}
        assert "/static/icons/icon-192.png" in srcs
        assert "/static/icons/icon-512.png" in srcs


def test_pwa_icons_and_service_worker_are_public():
    with TestClient(app) as client:
        apple = client.get("/apple-touch-icon.png")
        assert apple.status_code == 200
        assert apple.content.startswith(PNG)
        icon192 = client.get("/static/icons/icon-192.png")
        assert icon192.status_code == 200
        assert icon192.content.startswith(PNG)
        icon512 = client.get("/static/icons/icon-512.png")
        assert icon512.status_code == 200
        assert len(icon512.content) > 200
        worker = client.get("/sw.js")
        assert worker.status_code == 200
        assert "hearth-shell" in worker.text
        assert "/api/" in worker.text


def test_login_and_home_are_installable_and_phone_ready():
    with TestClient(app) as client:
        login = client.get("/login")
        assert login.status_code == 200
        html = login.text
        assert 'rel="manifest"' in html
        assert 'href="/manifest.webmanifest"' in html
        assert 'apple-mobile-web-app-capable' in html
        assert 'name="apple-mobile-web-app-title" content="Hearth"' in html
        assert 'rel="apple-touch-icon"' in html
        assert "viewport-fit=cover" in html
        assert "theme-color" in html
        assert "Sign in" in html
        assert 'id="login-form"' in html

    login_html = (UI / "login.html").read_text(encoding="utf-8")
    index_html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")
    for page in (login_html, index_html):
        assert 'apple-mobile-web-app-capable" content="yes"' in page
        assert "/apple-touch-icon.png" in page
        assert "viewport-fit=cover" in page
    assert "composer-dock" in index_html
    assert "safe-area-inset" in css
    assert "--keyboard-inset" in css
    assert "100dvh" in css
    assert "--dock-space: 108px" not in css
    assert "calc(12px + var(--safe-bottom))" not in css
    assert "translate(-50%, -50%)" not in css
    assert "justify-content: center" in css
    assert "isTyping" in (UI / "pwa.js").read_text(encoding="utf-8")
    assert ".pills .pill" in css
    assert ".is-empty" in css
    assert "min-height: 28vh" not in css
    # Phone: first viewport is the orb under fixed Look chrome.
    assert ".hearth-hero" in css
    assert "--phone-fold: 100dvh" in css
    assert "--phone-fold: 100svh" in css
    assert "min-height: var(--phone-fold)" in css
    assert "phone-rest-anchor" in css
    assert 'class="hearth-hero"' in index_html
    assert 'class="phone-rest-anchor"' in index_html
    # Composer/widgets are not pinned over the orb on phone.
    assert ".composer-dock:focus-within" in css
    assert "isolation: isolate" in css
    # Phone: confirm stacks above Ask the House; hidden confirm must not reserve space.
    assert ".composer-dock .confirm" in css
    assert "order: 0" in css
    assert "margin-bottom: 8px" in css
    assert "has-confirm" in (UI / "app.js").read_text(encoding="utf-8")
    assert "hearth-shell-v18" in (UI / "sw.js").read_text(encoding="utf-8")
    assert 'id="logout-btn"' in index_html
    assert 'id="agent-pill"' in index_html
    assert 'id="settings-btn"' in index_html
    assert 'id="settings-sheet"' in index_html
    assert 'class="pill-actions"' in index_html
    assert 'class="look-chrome"' in index_html
    assert "/static/settings.js" in index_html
    assert "setEmpty" in (UI / "app.js").read_text(encoding="utf-8")
    app_js = (UI / "app.js").read_text(encoding="utf-8")
    assert "displayRole" in app_js
    assert "conversation.item.input_audio_transcription.completed" in app_js
    assert "pendingHangup" in app_js
    assert 'event.name === "end_call"' in app_js
    settings_js = (UI / "settings.js").read_text(encoding="utf-8")
    assert "hearth.look.v1" in settings_js
    assert "HearthSettings" in settings_js
    assert "localStorage" in settings_js
    assert 'id: "look"' in settings_js
    assert 'value: "jarvis"' in settings_js
    assert 'value: "forge"' in settings_js
    assert "document.documentElement" in settings_js
    assert "data-${knob.id}" in settings_js or 'data-${knob.id}' in settings_js
    assert ".settings-sheet" in css
    assert 'html[data-theme="ash"]' in css
    assert 'html[data-look="jarvis"]' in css
    assert 'html[data-look="forge"]' in css
    assert ".pill-actions" in css
    assert "gap: 12px" in css
    assert "hearth-shell-v18" in (UI / "sw.js").read_text(encoding="utf-8")
    assert (UI / "icons" / "apple-touch-icon.png").stat().st_size > 200
    assert (UI / "icons" / "icon-192.png").stat().st_size > 200
    assert (UI / "icons" / "icon-512.png").stat().st_size > 200
    assert "/static/settings.js" in (UI / "sw.js").read_text(encoding="utf-8")
    assert "/static/vad.js" in (UI / "sw.js").read_text(encoding="utf-8")


def test_phone_orb_owns_first_viewport_alone():
    css = (UI / "styles.css").read_text(encoding="utf-8")
    index_html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'class="hearth-hero"' in index_html
    assert 'id="orb"' in index_html
    # Hero is a full phone screen; Look chrome is fixed on the first screen.
    assert ".hearth-hero" in css and "min-height: var(--phone-fold)" in css
    assert "--phone-fold: 100svh" in css
    phone = _phone_media_block(css)
    assert "position: fixed" in phone
    assert "top: var(--phone-fold)" not in phone
    assert "#settings-btn" in index_html or 'id="settings-btn"' in index_html
    # Fixed dock/widgets over the sphere are gone on phone.
    assert "bottom: calc(var(--dock-space) + var(--keyboard-inset) + 10px)" not in css
    assert ".composer-dock:focus-within" in css


def test_phone_fold_resyncs_on_orientation_change():
    """Ask the House must settle immediately after rotate — not after a second resize."""
    pwa = (UI / "pwa.js").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert "--phone-fold" in pwa
    assert "syncPhoneFold" in pwa
    assert "afterOrientation" in pwa
    assert 'addEventListener("orientationchange"' in pwa
    assert "screen.orientation" in pwa
    assert "requestAnimationFrame" in pwa
    assert 'addEventListener("pageshow"' in pwa
    assert "min-height: var(--phone-fold)" in css
    # Do not freeze the fold to a stale height while the keyboard is open.
    assert "isTyping" in pwa
    assert "hearth-shell-v18" in (UI / "sw.js").read_text(encoding="utf-8")


def _css_brace_depth(css: str) -> int:
    """Return final brace depth after stripping comments and strings (0 = balanced)."""
    depth = 0
    in_comment = False
    i = 0
    n = len(css)
    while i < n:
        if in_comment:
            if css.startswith("*/", i):
                in_comment = False
                i += 2
                continue
            i += 1
            continue
        if css.startswith("/*", i):
            in_comment = True
            i += 2
            continue
        ch = css[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n:
                if css[i] == "\\":
                    i += 2
                    continue
                if css[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth


def _phone_media_block(css: str) -> str:
    marker = "@media (max-width: 960px)"
    start = css.find(marker)
    assert start != -1
    i = css.find("{", start)
    depth = 0
    for j in range(i, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[start : j + 1]
    raise AssertionError("unclosed phone media query")


def test_styles_css_braces_balanced_so_look_rules_apply():
    """Regression: an unclosed #memory-block.is-empty nested look + phone rules."""
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert _css_brace_depth(css) == 0
    assert css.count("{") == css.count("}")
    # Look selectors must be top-level html[data-look=...], not nested under another rule.
    jarvis = css.index('html[data-look="jarvis"]')
    forge = css.index('html[data-look="forge"]')
    memory = css.index("#memory-block.is-empty")
    memory_close = css.find("}", memory)
    assert memory_close != -1
    assert memory_close < jarvis
    assert 'display: none;\n}' in css[memory : memory_close + 1] or "display: none;\n}" in css[memory : memory + 80]
    # Closed memory rule before look styles.
    assert _css_brace_depth(css[:jarvis]) == 0
    assert _css_brace_depth(css[:forge]) == 0


def test_settings_apply_writes_html_data_look_attribute():
    """Style knob must set html[data-look], matching CSS (not body / data-style)."""
    settings_js = (UI / "settings.js").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert 'id: "look"' in settings_js
    assert 'id: "style"' not in settings_js
    assert "document.documentElement" in settings_js
    assert "root.dataset[knob.id] = value" in settings_js
    assert "data-${knob.id}" in settings_js
    assert 'html[data-look="jarvis"]' in css
    assert 'html[data-look="forge"]' in css
    assert "body[data-look" not in css
    assert 'html[data-style=' not in css


def test_phone_layout_keeps_look_reachable_and_orb_centered():
    css = (UI / "styles.css").read_text(encoding="utf-8")
    phone = _phone_media_block(css)
    assert ".top" in phone
    assert "position: fixed" in phone
    assert "top: var(--phone-fold)" not in phone
    assert ".hearth-hero" in phone
    assert "min-height: var(--phone-fold)" in phone
    assert "justify-content: center" in phone
    assert ".composer-dock" in phone
    assert "position: relative" in phone
    # Look button exists in chrome; phone chrome is first-screen fixed.
    index_html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'id="settings-btn"' in index_html
    assert ">Look<" in index_html or "Look\n" in index_html


def test_orb_focus_outline_is_circular():
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert ".orb:focus" in css
    assert ".orb:focus-visible" in css
    assert "border-radius: 50%" in css
    # Square browser default outline must not win on the orb button.
    assert "outline: none" in css


def test_phone_transcript_shows_user_and_assistant_roles():
    app_js = (UI / "app.js").read_text(encoding="utf-8")
    assert 'appendLog("you"' in app_js or "appendLog(\"you\"" in app_js
    assert 'appendLog("hearth"' in app_js or "appendLog(\"hearth\"" in app_js
    assert "input_audio_transcription.completed" in app_js
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert 'li[data-role="you"]' in css
    assert ".transcript-details[open]" in css
    assert ".rail-media" in css and "z-index: 0" in css

def test_conversation_is_collapsed_until_opened():
    """Transcript stays out of the way; expand only via the Conversation control."""
    index_html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")
    app_js = (UI / "app.js").read_text(encoding="utf-8")

    assert 'id="transcript-details"' in index_html
    assert 'class="transcript-toggle"' in index_html
    assert "<details" in index_html
    assert 'id="log"' in index_html
    # Must start collapsed (no open attribute on details).
    assert "transcript-details" in index_html
    assert 'open="' not in index_html.split('id="transcript-details"')[1].split(">")[0]
    assert ".transcript.is-empty" in css
    assert ".transcript-details[open]" in css
    assert "displayRole" in app_js
    assert "input_audio_transcription.completed" in app_js
    assert 'appendLog("you"' in app_js
    assert 'appendLog("hearth"' in app_js

def test_realtime_session_enables_user_input_transcription():
    from hearth.voice.webrtc import session_config

    cfg = session_config()
    assert cfg["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
    assert cfg["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"


def test_mic_permission_ux_avoids_boot_probe_and_keeps_warm_stream():
    app_js = (UI / "app.js").read_text(encoding="utf-8")
    index_html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")

    assert "queryMicPermission" in app_js
    assert "acquireMicStream" in app_js
    assert "permissions.query" in app_js
    assert 'name: "microphone"' in app_js
    assert "shouldShowMicGate" in app_js
    assert "MIC_GATE_COOLDOWN_MS" in app_js
    assert "hearth.mic.granted" in app_js
    assert "releaseMicStream" in app_js
    assert "track.enabled = false" in app_js
    assert "do not track.stop()" in app_js
    assert "never probe with getUserMedia on boot" in app_js

    # getUserMedia only via acquireMicStream — not on boot or as a permission probe.
    assert app_js.count("getUserMedia(") == 1
    assert "acquireMicStream" in app_js

    assert 'id="mic-gate"' in index_html
    assert 'id="mic-gate-continue"' in index_html
    assert 'id="mic-denied"' in index_html
    assert 'id="mic-denied-retry"' in index_html
    assert "Settings → Hearth → Microphone" in app_js
    assert ".mic-panel" in css
    assert "SpeechBargeIn" in app_js
    assert "HearthVad" in app_js