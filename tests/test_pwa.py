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
    # Phone transcript shares the column with house rails instead of overlapping.
    assert ":has(#transcript:not(.is-empty))" in css
    assert "isolation: isolate" in css
    assert 'id="logout-btn"' in index_html
    assert 'id="agent-pill"' in index_html
    assert 'id="settings-btn"' in index_html
    assert 'id="settings-sheet"' in index_html
    assert "/static/settings.js" in index_html
    assert "setEmpty" in (UI / "app.js").read_text(encoding="utf-8")
    app_js = (UI / "app.js").read_text(encoding="utf-8")
    assert "displayRole" in app_js
    assert "conversation.item.input_audio_transcription.completed" in app_js
    settings_js = (UI / "settings.js").read_text(encoding="utf-8")
    assert "hearth.look.v1" in settings_js
    assert "HearthSettings" in settings_js
    assert "localStorage" in settings_js
    assert ".settings-sheet" in css
    assert 'html[data-theme="ash"]' in css
    assert (UI / "icons" / "apple-touch-icon.png").stat().st_size > 200
    assert (UI / "icons" / "icon-192.png").stat().st_size > 200
    assert (UI / "icons" / "icon-512.png").stat().st_size > 200
    assert "/static/settings.js" in (UI / "sw.js").read_text(encoding="utf-8")
    assert "/static/vad.js" in (UI / "sw.js").read_text(encoding="utf-8")


def test_phone_transcript_shows_user_and_assistant_roles():
    app_js = (UI / "app.js").read_text(encoding="utf-8")
    assert 'appendLog("you"' in app_js or "appendLog(\"you\"" in app_js
    assert 'appendLog("hearth"' in app_js or "appendLog(\"hearth\"" in app_js
    assert "input_audio_transcription.completed" in app_js
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert 'li[data-role="you"]' in css
    assert "max-height: min(30vh, 200px)" in css
    assert ".rail-media" in css and "z-index: 0" in css


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
