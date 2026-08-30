from fastapi.testclient import TestClient

from hearth.app import app
from hearth.config import settings
from tests.conftest import TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD


def test_wrong_password_is_401():
    with TestClient(app) as client:
        response = client.post(
            "/auth/token",
            json={"email": TEST_ADMIN_EMAIL, "password": "definitely-not-the-password"},
        )
        assert response.status_code == 401
        assert "access_token" not in response.json()
        assert TEST_ADMIN_PASSWORD not in response.text


def test_login_sets_refresh_cookie_and_returns_jwt():
    with TestClient(app) as client:
        response = client.post(
            "/auth/token",
            json={"email": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "X-Auth-Token"
        assert body["access_token"]
        assert TEST_ADMIN_PASSWORD not in response.text
        assert client.cookies.get("refresh_token")
        assert client.cookies.get("session_hint") == "1"


def test_refresh_mints_new_access_jwt():
    with TestClient(app) as client:
        login = client.post(
            "/auth/token",
            json={"email": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        )
        first = login.json()["access_token"]
        refreshed = client.post("/auth/session/refresh")
        assert refreshed.status_code == 200
        second = refreshed.json()["access_token"]
        assert second
        assert second != first
        me = client.get("/auth/me", headers={"X-Auth-Token": second})
        assert me.status_code == 200
        assert me.json()["email"] == TEST_ADMIN_EMAIL


def test_logout_clears_cookie_and_refresh_fails():
    with TestClient(app) as client:
        client.post("/auth/token", json={"email": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD})
        logout = client.post("/auth/session/logout")
        assert logout.status_code == 200
        assert not client.cookies.get("refresh_token")
        denied = client.post("/auth/session/refresh")
        assert denied.status_code == 401


def test_gated_api_is_401_without_auth():
    with TestClient(app) as client:
        status = client.get("/api/status")
        assert status.status_code == 401
        assert status.json() == {"error": "unauthorized"}
        home = client.get("/", follow_redirects=False)
        assert home.status_code == 302
        assert home.headers["location"] == "/login"


def test_gated_api_is_200_with_jwt(client):
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["house"] == "VAULT"
    home = client.get("/")
    assert home.status_code == 200
    assert "Tap to talk" in home.text


def test_hearth_token_machine_bypass_without_login(monkeypatch):
    monkeypatch.setattr(settings, "token", "machine-test-token")
    with TestClient(app) as client:
        denied = client.get("/api/status")
        assert denied.status_code == 401
        query = client.get("/api/status?token=machine-test-token")
        assert query.status_code == 401
        ok = client.get("/api/status", headers={"X-Hearth-Token": "machine-test-token"})
        assert ok.status_code == 200
        assert ok.json()["house"] == "VAULT"


def test_login_page_and_static_are_public():
    with TestClient(app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert "House login" in page.text
        css = client.get("/static/styles.css")
        assert css.status_code == 200
        js = client.get("/static/login.js")
        assert js.status_code == 200
        health = client.get("/health")
        assert health.status_code == 200


def test_media_art_allows_refresh_cookie_like_img_tag():
    """Overlay posters use <img src>/api/media/art> — no X-Auth-Token header.

    The refresh cookie alone must authorize those GETs, otherwise the browser
    shows a broken-image question mark for every title.
    """
    with TestClient(app) as client:
        login = client.post(
            "/auth/token",
            json={"email": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        assert client.cookies.get("refresh_token")

        # Cookie session only — same as a browser <img> request.
        art = client.get(
            "/api/media/art",
            params={
                "tmdbId": 693134,
                "posterPath": "/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
                "title": "Dune: Part Two",
            },
            headers={},
        )
        assert art.status_code == 200
        assert art.headers.get("content-type", "").startswith("image/")
        # Real JPEG when CDN reachable; SVG initials placeholder otherwise.
        assert art.content[:1] in {b"\xff", b"<"} or len(art.content) > 100

        thumb = client.get("/api/plex/thumb/1001", headers={})
        assert thumb.status_code == 200
        assert thumb.headers.get("content-type", "").startswith("image/")

        # Other API routes still require the access JWT (not refresh alone).
        status = client.get("/api/status", headers={})
        assert status.status_code == 401


def test_media_art_still_401_without_any_session():
    with TestClient(app) as client:
        denied = client.get(
            "/api/media/art",
            params={"tmdbId": 693134, "title": "Dune"},
        )
        assert denied.status_code == 401
        assert denied.json() == {"error": "unauthorized"}
