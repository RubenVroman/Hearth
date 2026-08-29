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
    assert "getBoundingClientRect" in (UI / "pwa.js").read_text(encoding="utf-8")
    assert ".pills .pill" in css
    assert ".is-empty" in css
    assert "min-height: 28vh" not in css
    assert 'id="logout-btn"' in index_html
    assert 'id="agent-pill"' in index_html
    assert "setEmpty" in (UI / "app.js").read_text(encoding="utf-8")
    assert (UI / "icons" / "apple-touch-icon.png").stat().st_size > 200
    assert (UI / "icons" / "icon-192.png").stat().st_size > 200
    assert (UI / "icons" / "icon-512.png").stat().st_size > 200
