"""Paid Realtime responses are never repeated for a replayed delivery."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hearth.voice import webrtc
from hearth.config import settings
from hearth.memory import store


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_attaching_sideband_keeps_already_configured_session(monkeypatch):
    band = webrtc.Sideband("rtc-existing", initial_memory="Existing call context")
    socket = Socket()

    async def connect():
        band._ws = socket

    async def listen():
        await asyncio.Event().wait()

    def forbidden_config():
        pytest.fail("Attaching to an existing call must not rebuild its prompt")

    monkeypatch.setattr(band, "_connect_socket", connect)
    monkeypatch.setattr(band, "_listen", listen)
    monkeypatch.setattr(webrtc, "session_config", forbidden_config)
    await band.start()
    assert socket.sent == []
    assert band._socket_ready.is_set()
    await band.close()


@pytest.mark.asyncio
async def test_replayed_tool_batch_does_not_create_an_empty_paid_continuation(monkeypatch):
    executed = []

    async def run(name, args, **kwargs):
        executed.append(name)
        return {"ok": True, "name": name}

    monkeypatch.setattr(webrtc, "run_house_tool", run)
    band = webrtc.Sideband("rtc-batch-replay")
    band._ws = socket = Socket()
    event = {"response": {"output": [
        {"type": "function_call", "name": "plex_search", "call_id": "tool-a", "arguments": "{}"},
        {"type": "function_call", "name": "radarr_queue", "call_id": "tool-b", "arguments": "{}"},
    ]}}
    await band._handle_function_calls(event)
    await band._handle_function_calls(event)
    assert executed == ["plex_search", "radarr_queue"]
    assert [event["type"] for event in socket.sent] == [
        "conversation.item.create", "conversation.item.create", "response.create",
    ]
    await band.close()


@pytest.mark.asyncio
async def test_duplicate_final_transcript_does_not_repeat_memory_retrieval(monkeypatch):
    retrieved = []

    async def instructions(query, **_context):
        retrieved.append(query)
        return "Same relevant memory"

    monkeypatch.setattr(webrtc, "voice_memory_async", instructions)
    band = webrtc.Sideband("rtc-transcript-replay")
    band._ws = socket = Socket()
    await band._remember_user_final("What is playing?", item_id="speech-one")
    await band._memory_task
    await band._remember_user_final("What is playing?", item_id="speech-one")
    await band._memory_task
    assert retrieved == ["What is playing?"]
    assert len(socket.sent) == 1
    # Repeating the same words in a genuinely new utterance still refreshes.
    await band._on_event({"type": "input_audio_buffer.speech_started", "item_id": "speech-two"})
    await band._remember_user_final("What is playing?", item_id="speech-two")
    await band._memory_task
    assert len(retrieved) == 2
    assert len(socket.sent) == 1  # Identical instructions retain cached context.
    await band.close()


@pytest.mark.asyncio
async def test_unchanged_bootstrap_instructions_do_not_send_session_update(monkeypatch):
    async def instructions(query, **_context):
        return "Initial memory"

    monkeypatch.setattr(webrtc, "voice_memory_async", instructions)
    band = webrtc.Sideband("rtc-stable-context", initial_memory="Initial memory")
    band._ws = socket = Socket()
    band._pending_query = "What is playing?"
    await band._flush_memory()
    assert socket.sent == []
    assert band._pending_query is None
    await band.close()


@pytest.mark.asyncio
async def test_voice_memory_keeps_bootstrap_and_preferences_without_repeating_call_history():
    store.persist_turn("user", "Before this call we chose Arrival", channel="voice")
    assert "Before this call we chose Arrival" in webrtc.session_config()["instructions"]
    band = webrtc.Sideband("rtc-frozen-memory")
    band._ws = socket = Socket()
    band._note_transcript("user", "I am asking about cerulean penguins now", "current-user")
    band._note_transcript("assistant", "The cerulean penguins are on screen", "current-assistant")
    store.remember_preference("captions", "Always prefer original language")
    band._pending_query = "cerulean penguins"
    await band._flush_memory()
    prompt = socket.sent[0]["item"]["content"][0]["text"]
    assert "Before this call we chose Arrival" not in prompt  # Already in initial instructions.
    assert "Always prefer original language" in prompt
    assert "I am asking about cerulean penguins now" not in prompt
    assert "The cerulean penguins are on screen" not in prompt
    assert len(band._persisted_turn_ids) == 2
    await band.close()


@pytest.mark.asyncio
async def test_changed_topics_append_only_new_memory_without_restarting_inference():
    store.remember_preference("format", "subtitles only")
    store.persist_turn("user", "Horror favourite is Alien", channel="voice")
    store.persist_turn("user", "Comedy favourite is Groundhog Day", channel="voice")
    band = webrtc.Sideband("rtc-topic-memory")
    band._ws = socket = Socket()
    for query in ("horror", "horror", "comedy", "comedy"):
        band._note_transcript("user", f"{query} current call question", query)
        band._pending_query = query
        await band._flush_memory()
    assert len(socket.sent) == 2
    contexts = [event["item"]["content"][0]["text"] for event in socket.sent]
    assert "Horror favourite is Alien" in contexts[0]
    assert "Comedy favourite is Groundhog Day" in contexts[1]
    assert all("current call question" not in text for text in contexts)
    assert all("subtitles only" in text for text in contexts)
    assert all("You are Hearth" not in text for text in contexts)
    store.forget(key="format")
    band._pending_query = "cerulean penguins"
    await band._flush_memory()
    assert len(socket.sent) == 3
    cleared = socket.sent[-1]["item"]["content"][0]["text"]
    assert "subtitles only" not in cleared
    assert "No stored preferences" in cleared
    assert "supersedes earlier stored-preference snapshots" in cleared
    assert all(event["type"] == "conversation.item.create" for event in socket.sent)
    assert all(event["item"]["role"] == "system" for event in socket.sent)
    await band.close()


@pytest.mark.asyncio
async def test_disconnected_browser_setup_releases_upstream_call(monkeypatch):
    from hearth.app import realtime_calls

    released = []

    class Request:
        async def body(self):
            return b"sdp"

        async def is_disconnected(self):
            return True

    async def created(sdp):
        return {"ok": True, "call_id": "rtc-lost-response", "sdp": "answer"}

    async def hangup(call_id):
        released.append(call_id)

    monkeypatch.setattr(webrtc, "create_call", created)
    monkeypatch.setattr(webrtc, "hangup", hangup)
    response = await realtime_calls(Request())
    assert response.status_code == 499
    assert released == ["rtc-lost-response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404, 410, 503])
async def test_explicit_hangup_ends_upstream_call_once_without_retry(monkeypatch, status):
    calls = []
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 5.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(is_success=status == 200, status_code=status)

    monkeypatch.setattr(webrtc.httpx, "AsyncClient", Client)
    await webrtc.hangup("rtc-closed")
    assert len(calls) == 1
    assert calls[0][0] == f"{webrtc.CALLS_URL}/rtc-closed/hangup"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_canceled_setup_releases_call_the_browser_never_received(monkeypatch):
    started = asyncio.Event()
    hung_up = []
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def post(self, url, **kwargs):
            return SimpleNamespace(is_success=True, headers={"Location": "/v1/realtime/calls/rtc-abandoned"})

    async def start(self):
        started.set()
        await asyncio.Event().wait()

    async def hangup(call_id):
        hung_up.append(call_id)

    monkeypatch.setattr(webrtc.httpx, "AsyncClient", Client)
    monkeypatch.setattr(webrtc.Sideband, "start", start)
    monkeypatch.setattr(webrtc, "_hangup_upstream", hangup)
    setup = asyncio.create_task(webrtc.create_call("sdp"))
    await started.wait()
    setup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await setup
    assert hung_up == ["rtc-abandoned"]
    assert "rtc-abandoned" not in webrtc._sidebands


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
async def test_voice_usage_is_forwarded_for_every_provider_done_status(monkeypatch, status):
    measured = []
    monkeypatch.setattr(webrtc, "record_realtime_usage", lambda response, **kwargs: measured.append(response))
    band = webrtc.Sideband("rtc-usage")
    band._ws = Socket()
    response = {"id": "resp-measured", "status": status, "usage": {"total_tokens": 70}}
    await band._on_event({"type": "response.done", "response": response})
    await asyncio.gather(*list(band._jobs))
    assert measured == [response]
    await band.close()


@pytest.mark.asyncio
async def test_input_transcription_usage_is_forwarded_with_its_separate_model(monkeypatch):
    measured = []
    monkeypatch.setattr(webrtc, "record_transcription_usage", lambda event, **kwargs: measured.append((event, kwargs)))
    band = webrtc.Sideband("rtc-transcription-usage")
    band._ws = Socket()
    band._active_responses = 1  # The voice answer is still in progress.
    event = {"type": "conversation.item.input_audio_transcription.completed", "item_id": "speech-metered",
             "transcript": "What is playing?", "usage": {"type": "tokens", "total_tokens": 70}}
    await band._on_event(event)
    assert measured == [(event, {"model": "gpt-4o-mini-transcribe"})]
    assert band._utterance == "What is playing?"
    await band.close()


@pytest.mark.asyncio
async def test_response_finishes_when_usage_ledger_raises(monkeypatch):
    monkeypatch.setattr(webrtc, "record_realtime_usage", Mock(side_effect=RuntimeError("ledger failed")))
    band = webrtc.Sideband("rtc-ledger-error")
    finished = AsyncMock()
    monkeypatch.setattr(band, "_finish_response", finished)
    await band._on_event({"type": "response.created", "response": {"id": "resp-still-finishes"}})
    event = {"type": "response.done", "response": {
        "id": "resp-still-finishes", "status": "completed", "usage": {"total_tokens": 70},
    }}
    await band._on_event(event)
    await asyncio.gather(*list(band._jobs))
    assert band._active_responses == 0
    assert "resp-still-finishes" in band._done_responses
    finished.assert_awaited_once_with(event, band._generation, "")
    await band.close()


@pytest.mark.asyncio
async def test_user_transcript_advances_when_usage_ledger_raises(monkeypatch):
    monkeypatch.setattr(webrtc, "record_transcription_usage", Mock(side_effect=RuntimeError("ledger failed")))
    band = webrtc.Sideband("rtc-transcription-ledger-error")
    band._active_responses = 1
    await band._on_event({"type": "conversation.item.input_audio_transcription.completed",
                          "item_id": "speech-survives", "transcript": "Tell me about Alien"})
    assert band._utterance == "Tell me about Alien"
    assert band._transcript_ready.is_set()
    await band.close()
