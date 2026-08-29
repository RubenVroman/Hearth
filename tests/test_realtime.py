from unittest.mock import patch

import json
import pytest

from hearth.config import settings
from hearth.voice import webrtc as realtime_rtc


def test_status_advertises_ga_webrtc_path(client):
    status = client.get("/api/status").json()
    assert status["realtime"]["path"] == "webrtc-ga"
    assert status["realtime"]["beta"] is False
    assert status["realtime"]["model"] == settings.openai_realtime_model
    assert settings.openai_realtime_model == "gpt-realtime-2.1"
    assert status["realtime"]["calls"] == "/api/realtime/calls"
    assert status["realtime"]["client_secrets"] == "/api/realtime/client_secrets"
    assert status["voice"]["beta"] is False


def test_ga_headers_never_send_beta_shape(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-hearth")
    headers = realtime_rtc.openai_auth_headers()
    assert "OpenAI-Beta" not in headers
    assert "realtime=v1" not in " ".join(headers.values())
    assert headers["Authorization"].startswith("Bearer ")
    json_headers = realtime_rtc.openai_auth_headers(json_body=True)
    assert "OpenAI-Beta" not in json_headers


def test_client_secrets_without_key_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    response = client.post("/api/realtime/client_secrets")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["path"] == "webrtc-ga"
    assert body.get("value") in {None, ""}
    assert "sk-" not in response.text


def test_client_secrets_mints_ephemeral_ek_token(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-hearth")
    captured: dict = {}

    class FakeResp:
        is_success = True
        status_code = 200

        def json(self):
            return {"value": "ek_live_test", "expires_at": 1_700_000_000}

    class FakeClient:
        def __init__(self, timeout=None, **_kwargs):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, headers=None, json=None, files=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["json"] = json
            return FakeResp()

    with patch("hearth.voice.webrtc.httpx.AsyncClient", FakeClient):
        response = client.post("/api/realtime/client_secrets")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["path"] == "webrtc-ga"
    assert body["beta"] is False
    assert body["value"] == "ek_live_test"
    assert body["model"] == "gpt-realtime-2.1"
    assert "sk-test-hearth" not in response.text
    assert captured["url"] == realtime_rtc.SECRETS_URL
    assert "OpenAI-Beta" not in captured["headers"]
    assert captured["json"]["session"]["type"] == "realtime"
    assert captured["json"]["session"]["model"] == "gpt-realtime-2.1"


def test_create_call_posts_ga_calls_without_beta_header(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-hearth")
    captured: dict = {}
    answer = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

    class FakeResp:
        is_success = True
        status_code = 201
        text = answer
        headers = {"Location": "/v1/realtime/calls/rtc_test_call"}

        def json(self):
            return {}

    class DummyWS:
        async def send(self, _msg):
            return None

        async def close(self):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeClient:
        def __init__(self, timeout=None, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, headers=None, json=None, files=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["files"] = files
            return FakeResp()

    async def fake_ws_connect(url, additional_headers=None, open_timeout=None):
        captured["ws_url"] = url
        captured["ws_headers"] = dict(additional_headers or {})
        return DummyWS()

    with (
        patch("hearth.voice.webrtc.httpx.AsyncClient", FakeClient),
        patch("hearth.voice.webrtc.ws_connect", fake_ws_connect),
    ):
        response = client.post(
            "/api/realtime/calls",
            content="v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n",
            headers={"Content-Type": "application/sdp"},
        )
        hangup = client.post("/api/realtime/calls/rtc_test_call/hangup")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.headers["X-Hearth-Realtime-Path"] == "webrtc-ga"
    assert response.headers["X-Hearth-Realtime-Beta"] == "false"
    assert response.headers["X-Hearth-Call-Id"] == "rtc_test_call"
    assert response.headers["X-Hearth-Realtime-Model"] == "gpt-realtime-2.1"
    assert response.text == answer
    assert captured["url"] == realtime_rtc.CALLS_URL
    assert "OpenAI-Beta" not in captured["headers"]
    assert "sdp" in captured["files"]
    assert "session" in captured["files"]
    session = captured["files"]["session"][1]
    assert '"type": "realtime"' in session or '"type":"realtime"' in session
    assert "gpt-realtime-2.1" in session
    assert captured["ws_url"] == f"{realtime_rtc.SIDEBAND_URL}?call_id=rtc_test_call"
    assert "OpenAI-Beta" not in captured["ws_headers"]
    assert hangup.status_code == 200
    assert hangup.json()["path"] == "webrtc-ga"


def test_create_call_empty_sdp_is_400(client):
    response = client.post(
        "/api/realtime/calls",
        content="   ",
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 400
    assert response.json()["path"] == "webrtc-ga"


def test_create_call_without_key_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    response = client.post(
        "/api/realtime/calls",
        content="v=0",
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 503
    assert response.json()["path"] == "webrtc-ga"


def test_realtime_tools_run_on_hearth(client):
    response = client.post(
        "/api/realtime/tools",
        json={"name": "plex_now_playing", "arguments": {}, "call_id": "rtc_x"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "webrtc-ga"
    assert body["output"]["name"] == "plex_now_playing"
    assert "Dune" in str(body["output"])


def test_secret_value_reads_ga_and_nested_shapes():
    assert realtime_rtc.secret_value({"value": "ek_abc"}) == "ek_abc"
    assert realtime_rtc.secret_value({"client_secret": {"value": "ek_nested"}}) == "ek_nested"
    assert realtime_rtc.secret_value({}) is None


@pytest.mark.asyncio
async def test_end_call_tool_marks_close_of_call():
    from hearth.agent.registry import registry

    result = await registry.call("end_call", {"reason": "goodbye"})
    assert result.ok
    assert result.data["ended"] is True
    assert result.data["reason"] == "goodbye"
    tools = {t["name"] for t in registry.openai_realtime_tools()}
    assert "end_call" in tools


@pytest.mark.asyncio
async def test_sideband_end_call_closes_after_response_done(monkeypatch):
    """Close-of-call: end_call tool → response.done → sideband hangup (no extra response.create)."""
    sent: list[dict] = []
    closed = {"n": 0}

    class DummyWS:
        async def send(self, msg):
            sent.append(json.loads(msg) if isinstance(msg, str) else msg)

        async def close(self):
            closed["n"] += 1

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    band = realtime_rtc.Sideband("rtc_close_test")
    band._ws = DummyWS()
    realtime_rtc._sidebands["rtc_close_test"] = band

    await band._on_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "end_call",
            "arguments": '{"reason":"goodbye"}',
            "call_id": "call_end_1",
        }
    )
    assert band._pending_hangup is True
    assert any(m.get("type") == "conversation.item.create" for m in sent)
    assert not any(m.get("type") == "response.create" for m in sent)

    await band._on_event({"type": "response.done", "response": {"output": []}})
    # Hangup is scheduled as a task; let it run.
    if band._hangup_task is not None:
        await band._hangup_task

    assert "rtc_close_test" not in realtime_rtc._sidebands
    assert closed["n"] >= 1
    assert band._ws is None


@pytest.mark.asyncio
async def test_sideband_house_tool_still_requests_follow_up_response():
    sent: list[dict] = []

    class DummyWS:
        async def send(self, msg):
            sent.append(json.loads(msg) if isinstance(msg, str) else msg)

        async def close(self):
            return None

    band = realtime_rtc.Sideband("rtc_tool_test")
    band._ws = DummyWS()
    await band._run_function_call("plex_now_playing", "{}", "call_plex_1")
    assert any(m.get("type") == "response.create" for m in sent)
    assert band._pending_hangup is False
