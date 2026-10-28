"""Living-room /tv shell: public page, gated board, buttons on the fixture house."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from hearth.app import app
from hearth.config import settings
from hearth.tools.ha import _mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "webos" / "com.hearth.tv"
UI = ROOT / "hearth" / "ui" / "static" / "tv"
PNG = b"\x89PNG"


def _tv_source() -> str:
    state = _mock.get_state("media_player.lg_webos_tv")
    assert state is not None
    return str(state["attributes"].get("source") or "")


def _avr_volume() -> float:
    state = _mock.get_state("media_player.denon_avr_x3700h")
    assert state is not None
    return float(state["attributes"]["volume_level"])


def test_tv_page_is_public_and_remote_friendly():
    with TestClient(app) as client:
        page = client.get("/tv")
        assert page.status_code == 200
        assert "no-cache" in page.headers.get("cache-control", "")
        html = page.text
        assert "data-tv-focus" in html
        assert "Sign in" in html
        assert 'id="dashboard"' in html
        denied = client.get("/api/tv/board")
        assert denied.status_code == 401
        assert denied.json() == {"error": "unauthorized"}

    css = (UI / "tv.css").read_text(encoding="utf-8")
    script = (UI / "tv.js").read_text(encoding="utf-8")
    assert "min-height: 88px" in css
    assert "outline: 4px solid" in css
    assert "backdrop-filter:" in css
    assert "-webkit-backdrop-filter:" in css
    assert "keyCode === 461" in script
    assert "X-Hearth-Token" in script
    assert "X-Auth-Token" in script
    assert "console.log" not in script
    assert "?token=" not in script
    worker = (ROOT / "hearth" / "ui" / "static" / "sw.js").read_text(encoding="utf-8")
    assert 'pathname === "/tv"' in worker
    assert 'pathname.startsWith("/static/tv/")' in worker


def test_board_is_the_fixture_living_room(client):
    body = client.get("/api/tv/board").json()
    assert body["house"] == "VAULT"
    assert body["target_tv"] == "OLED65G1RLA"
    assert body["now"]["title"] == "Dune: Part Two"
    assert body["now"]["player"] == "Apple TV"
    assert body["players"]["avr"]["volume_pct"] == 32
    assert body["players"]["tv"]["source"] == "HDMI 1"
    labels = {row["label"] for row in body["continue_watching"]}
    assert any("The Bear" in label for label in labels)
    assert "Arrival" in labels
    launches = {row["id"]: row for row in body["launches"]}
    assert launches["netflix"]["available"] is True
    assert launches["videoland"]["available"] is True
    assert launches["infuse"]["available"] is True
    assert launches["prime"]["available"] is False
    assert launches["prime"]["status"] == "not_in_source_list"
    assert body["overseerr"]["stub"] is True
    assert body["overseerr"]["requests"] == []
    assert "8787" not in json.dumps(body["reach"])
    text = json.dumps(body)
    assert "HA_TOKEN" not in text
    assert settings.ha_url not in text


def test_board_accepts_machine_token_and_does_not_echo_it(monkeypatch):
    secret = "tv-machine-token-should-not-leak"
    monkeypatch.setattr(settings, "token", secret)
    with TestClient(app) as client:
        denied = client.get("/api/tv/board")
        assert denied.status_code == 401
        ok = client.get("/api/tv/board", headers={"X-Hearth-Token": secret})
        assert ok.status_code == 200
        assert secret not in ok.text
        assert "Bearer" not in ok.text


def test_volume_and_scenes_hit_the_fixture_house(client):
    before = _avr_volume()
    louder = client.post("/api/tv/action", json={"kind": "volume", "id": "up"})
    assert louder.status_code == 200
    assert louder.json()["ok"] is True
    assert _avr_volume() == round(before + 0.05, 2)

    muted = client.post("/api/tv/action", json={"kind": "volume", "id": "mute"})
    assert muted.json()["ok"] is True
    denon = _mock.get_state("media_player.denon_avr_x3700h")
    assert denon is not None
    assert denon["attributes"]["is_volume_muted"] is True

    night = client.post("/api/tv/action", json={"kind": "activity", "id": "movie_night"})
    assert night.status_code == 200
    assert night.json()["ok"] is True
    scene = _mock.get_state("scene.movie_night")
    assert scene is not None
    assert scene["state"] == "on"

    missing = client.post("/api/tv/action", json={"kind": "scene", "id": "quiet_hours"})
    assert missing.status_code == 200
    assert missing.json()["ok"] is False
    assert "Quiet hours" in missing.json()["speak"] or "quiet" in missing.json()["speak"].lower()

    unknown = client.post("/api/tv/action", json={"kind": "launch", "id": "morphe"})
    assert unknown.status_code == 400


def test_source_launches_are_honest(client):
    prime = client.post("/api/tv/action", json={"kind": "launch", "id": "prime"})
    assert prime.status_code == 200
    body = prime.json()
    assert body["ok"] is False
    assert "source list" in body["speak"].lower()
    assert _tv_source() == "HDMI 1"
    assert body["tools"] == []

    netflix = client.post("/api/tv/action", json={"kind": "launch", "id": "netflix"})
    assert netflix.json()["ok"] is True
    assert _tv_source() == "Netflix"
    assert "title" in netflix.json()["speak"].lower()

    videoland = client.post("/api/tv/action", json={"kind": "launch", "id": "videoland"})
    assert videoland.json()["ok"] is True
    assert _tv_source() == "Videoland"


def test_infuse_tile_and_continue_watching(client):
    opened = client.post("/api/tv/action", json={"kind": "launch", "id": "infuse"})
    assert opened.status_code == 200
    assert opened.json()["ok"] is True
    apple = _mock.get_state("media_player.apple_tv")
    assert apple is not None
    assert apple["attributes"]["media_content_id"] == "com.firecore.infuse"
    assert apple["attributes"]["media_content_type"] == "app"

    continued = client.post(
        "/api/tv/action",
        json={"kind": "continue", "id": "9002", "title": "Arrival"},
    )
    assert continued.status_code == 200
    assert continued.json()["ok"] is True
    assert "Infuse" in continued.json()["speak"]
    assert str(apple["attributes"].get("media_content_id", "")).startswith("infuse://") or (
        _mock.get_state("media_player.apple_tv")["attributes"]["media_content_id"].startswith("infuse://")
    )


def test_webos_package_is_ipk_ready():
    info = json.loads((PACKAGE / "appinfo.json").read_text(encoding="utf-8"))
    assert info["id"] == "com.hearth.tv"
    assert info["type"] == "web"
    assert info["main"] == "index.html"
    assert info["version"] == "1.0.0"
    assert info["disableBackHistoryAPI"] is True
    assert info["icon"] == "icon.png"
    assert info["largeIcon"] == "largeIcon.png"
    for name in ("icon.png", "largeIcon.png", "index.html", "shell.js", "shell.css"):
        assert (PACKAGE / name).is_file()
    assert (PACKAGE / "icon.png").read_bytes().startswith(PNG)
    assert (PACKAGE / "largeIcon.png").read_bytes().startswith(PNG)
    en = json.loads((PACKAGE / "resources" / "en" / "US" / "appinfo.json").read_text(encoding="utf-8"))
    nl = json.loads((PACKAGE / "resources" / "nl" / "NL" / "appinfo.json").read_text(encoding="utf-8"))
    assert en["title"] == "Hearth"
    assert nl["title"] == "Hearth"
    shell = (PACKAGE / "shell.js").read_text(encoding="utf-8")
    html = (PACKAGE / "index.html").read_text(encoding="utf-8")
    assert "192.168.2.26" in html
    assert "8787" in html
    assert "keyCode === 461" in shell
    assert "token=" not in shell
    assert "token=" not in html
    assert "/tv" in shell
    docs = (ROOT / "docs" / "webos.md").read_text(encoding="utf-8")
    assert "ares-package" in docs
    assert "Developer Mode" in docs
    assert "Homebrew" in docs
    assert "1920" in docs
    assert "do not root" in docs.lower() or "does not root" in docs.lower()
