def test_health_and_chat_calls_plex_tool(client):
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


def test_chat_repo_work_calls_chief_of_staff_not_github(client):
    chat = client.post("/api/chat", json={"message": "add a weather skill to the repo"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"]
    assert body["tools"][0]["name"] == "chief_of_staff"
    assert body["tools"][0]["name"] != "workspace_write"
    assert "not configured" in body["reply"].lower() or body["tools"][0].get("data", {}).get("configured") is False


def test_chat_download_movie_uses_radarr(client):
    chat = client.post("/api/chat", json={"message": "download the movie Dune"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "radarr_add"
    assert body["tools"][0]["needs_confirm"] is False
    assert "Radarr" in body["reply"]


def test_chat_download_progress_uses_radarr_queue(client):
    chat = client.post("/api/chat", json={"message": "How far along is Annihilation?"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "radarr_queue"
    assert body["tools"][0]["needs_confirm"] is False
    reply = body["reply"].lower()
    assert "annihilation" in reply
    assert "75" in reply or "downloading" in reply

    listed = client.post("/api/chat", json={"message": "What's downloading right now?"})
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["tools"][0]["name"] == "radarr_queue"
    assert "annihilation" in listed_body["reply"].lower() or "dune" in listed_body["reply"].lower()


def test_media_inventory_endpoint(client):
    response = client.get("/api/media")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["tv"]["entity_id"] == "media_player.lg_webos_tv"
    assert body["avr"]["ok"] is True
    assert body["apple_tv"]["entity_id"] == "media_player.apple_tv"
    assert body["apple_tv"]["ok"] is True
    assert body["plex"]["sessions"]
    assert "speak" in body
    status = client.get("/api/status")
    assert status.status_code == 200
    assert "radarr" in status.json()
    assert "web_search" in status.json()
    assert status.json()["web_search"]["backend"] == "mock"
    assert status.json()["ha"]["tv_entity"] == "media_player.lg_webos_tv"
    assert status.json()["ha"]["apple_tv_entity"] == "media_player.apple_tv"
    assert status.json()["ha"]["apple_tv_player"] == "infuse"

    network = client.get("/api/network")
    assert network.status_code == 200
    assert network.json()["key_media"]["avr"]["reachable"] is True
    assert network.json()["key_media"]["tv"]["reachable"] is True
    assert network.json()["key_media"]["apple_tv"]["reachable"] is True


def test_plex_genre_library_endpoints(client):
    genres = client.get("/api/plex/genres")
    assert genres.status_code == 200
    body = genres.json()
    assert body["ok"] is True
    names = {g["title"] for g in body["genres"]}
    assert "Animation" in names
    assert "Science Fiction" in names
    assert "animation" in body["speak"].lower()

    library = client.get("/api/plex/library", params={"genre": "Animation"})
    assert library.status_code == 200
    lib = library.json()
    assert lib["ok"] is True
    assert lib["genre"] == "Animation"
    assert lib["total"] == 3
    titles = {r["title"] for r in lib["results"]}
    assert "Spirited Away" in titles
    assert "3" in lib["speak"] and "Animation" in lib["speak"]
    for row in lib["results"]:
        assert "Animation" in (row.get("genres") or [])

    sci_fi = client.get("/api/plex/library", params={"genre": "Science Fiction"})
    assert sci_fi.status_code == 200
    sci = sci_fi.json()
    assert sci["ok"] is True
    assert sci["genre"] == "Science Fiction"
    sci_titles = {r["title"] for r in sci["results"]}
    assert "Dune: Part Two" in sci_titles or "The Endless" in sci_titles

    chat = client.post("/api/chat", json={"message": "what animation movies do we have"})
    assert chat.status_code == 200
    chat_body = chat.json()
    assert chat_body["tools"][0]["name"] == "plex_browse_genre"
    assert "Spirited Away" in chat_body["reply"] or "animation" in chat_body["reply"].lower()


def test_chat_turn_on_tv_uses_media_control(client):
    chat = client.post("/api/chat", json={"message": "turn on the TV"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["tools"][0]["name"] == "ha_media_control"
    assert body["tools"][0]["needs_confirm"] is False
    assert body["tools"][0]["ok"] is True


def test_command_center_served(client):
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
    assert "X-Auth-Token" in js.text
    assert "info-overlay" in page.text
    assert "openInfoOverlay" in js.text
    assert "/api/memory" in js.text
    assert 'id="memory-block"' in page.text
    css = client.get("/static/styles.css")
    assert css.status_code == 200


def test_voice_fallback_text_roundtrip(client):
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
        assert (
            "docker_ps" in assistant["text"]
            or "plex" in assistant["text"].lower()
            or "containers" in assistant["text"].lower()
            or "Names" in assistant["text"]
            or "gluetun" in assistant["text"].lower()
        )
