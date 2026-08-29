from fastapi.testclient import TestClient

from hearth.app import app


def test_health_and_chat_calls_plex_tool():
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        chat = client.post("/api/chat", json={"message": "what's playing"})
        assert chat.status_code == 200
        body = chat.json()
        assert body["mode"] == "local"
        assert body["tools"]
        assert body["tools"][0]["name"] == "plex_now_playing"
        assert "Dune" in body["reply"]

        rooms = client.get("/api/rooms")
        assert rooms.status_code == 200
        lights = {row["entity_id"] for row in rooms.json()["lights"]}
        assert "light.living_room" in lights

        playing = client.get("/api/now-playing")
        assert playing.json()["sessions"][0]["title"] == "Dune: Part Two"


def test_chat_repo_work_calls_chief_of_staff_not_github():
    with TestClient(app) as client:
        chat = client.post("/api/chat", json={"message": "add a weather skill to the repo"})
        assert chat.status_code == 200
        body = chat.json()
        assert body["tools"]
        assert body["tools"][0]["name"] == "chief_of_staff"
        assert body["tools"][0]["name"] != "workspace_write"
        assert "not configured" in body["reply"].lower() or body["tools"][0].get("data", {}).get("configured") is False


def test_chat_download_movie_uses_radarr():
    with TestClient(app) as client:
        chat = client.post("/api/chat", json={"message": "download the movie Dune"})
        assert chat.status_code == 200
        body = chat.json()
        assert body["tools"][0]["name"] == "radarr_add"
        assert body["tools"][0]["needs_confirm"] is True
        assert "Radarr" in body["reply"]


def test_command_center_served():
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Hearth" in page.text
        assert 'id="remote-audio"' in page.text
        assert "Tap to talk" in page.text
        assert "Hold to speak" not in page.text
        js = client.get("/static/app.js")
        assert js.status_code == 200
        assert "RTCPeerConnection" in js.text
        assert "ScriptProcessor" not in js.text
        assert "playPcm" not in js.text
        assert "/api/chat" in js.text
        assert "/api/realtime/calls" in js.text
        assert "OpenAI-Beta" not in js.text
        css = client.get("/static/styles.css")
        assert css.status_code == 200


def test_voice_fallback_text_roundtrip():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            assert ready["mode"] == "fallback"
            assert ready["path"] == "text-fallback"
            ws.send_json({"type": "input_text", "text": "list docker containers"})
            events = []
            for _ in range(12):
                event = ws.receive_json()
                events.append(event)
                if event.get("type") == "transcript.assistant" and event.get("final"):
                    break
            types = [e["type"] for e in events]
            assert "tool.result" in types
            assistant = next(e for e in events if e["type"] == "transcript.assistant")
            assert "docker_ps" in assistant["text"] or "plex" in assistant["text"].lower() or "containers" in assistant["text"].lower() or "Names" in assistant["text"] or "gluetun" in assistant["text"].lower()
