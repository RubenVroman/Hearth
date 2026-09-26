"""Regression coverage for retries, interrupted voice turns, and tool outcomes."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from hearth.agent.loop import AgentLoop
from hearth.agent.registry import ToolRegistry, ToolSpec
from hearth.config import settings
from hearth.runtime import runtime
from hearth.voice import webrtc


def _response(*calls, content=""):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, tool_calls=list(calls),
    ))])


def _call(name="ha_device_control", arguments='{"device":"kitchen","action":"turn_off"}', call_id="tool1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _client(monkeypatch, responses):
    client = SimpleNamespace(closed=False, options={})
    pending = iter(responses)

    async def create(**kwargs):
        value = next(pending)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return await value()
        return value

    class FakeOpenAI:
        def __init__(self, **kwargs):
            client.options = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            client.closed = True

    monkeypatch.setattr("openai.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(settings, "openai_api_key", "fake-no-live-provider")
    monkeypatch.setattr(settings, "memory_enabled", False)
    return client


def _tools(name="ha_device_control"):
    tools = ToolRegistry()
    calls = []

    async def handler(args):
        calls.append(args)
        return {"ok": True, "speak": "Kitchen light is off."}

    tools.register(ToolSpec(name, "test", {"type": "object"}, handler))
    return tools, calls


@pytest.mark.asyncio
async def test_model_failure_after_write_returns_result_without_replaying(monkeypatch):
    client = _client(monkeypatch, [_response(_call()), RuntimeError("provider unavailable")])
    tools, calls = _tools()
    agent = AgentLoop(tools)
    result = await agent.run("turn off kitchen")
    assert len(calls) == 1
    assert result["mode"] == "openai_degraded"
    assert result["tools"][0]["ok"] is True
    assert agent.history[-1]["content"] == result["reply"]
    assert client.closed
    assert client.options["max_retries"] == 0
    assert 0 < client.options["timeout"] <= 30


@pytest.mark.asyncio
async def test_hanging_model_times_out_and_falls_back_before_any_write(monkeypatch):
    async def hanging():
        await asyncio.Event().wait()

    client = _client(monkeypatch, [hanging])
    monkeypatch.setattr("hearth.agent.loop.MODEL_TIMEOUT_SECONDS", 0.01)
    tools, calls = _tools()
    result = await asyncio.wait_for(AgentLoop(tools).run("turn off kitchen"), timeout=1)
    assert result["mode"] == "local"
    assert len(calls) == 1
    assert client.closed


@pytest.mark.asyncio
async def test_cancelled_chat_restores_idle_and_releases_session_lock(monkeypatch):
    agent = AgentLoop()
    entered = asyncio.Event()

    async def turn(text, **_kwargs):
        if text == "wait":
            entered.set()
            await asyncio.Event().wait()
        return {"reply": "recovered"}

    monkeypatch.setattr(agent, "_run_turn", turn)
    task = asyncio.create_task(agent.run("wait"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.agent_status == "idle"
    assert (await agent.run("continue", announce=False))["reply"] == "recovered"


@pytest.mark.parametrize("arguments", ["{broken", "[]", "null", '"off"'])
@pytest.mark.asyncio
async def test_invalid_model_arguments_cannot_execute_default_action(monkeypatch, arguments):
    _client(monkeypatch, [_response(_call(arguments=arguments)), _response(content="Please clarify.")])
    tools, calls = _tools()
    result = await AgentLoop(tools).run("turn off kitchen")
    assert calls == []
    assert result["tools"][0]["data"]["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_model_repeated_write_reuses_outcome_but_read_can_refresh(monkeypatch):
    _client(monkeypatch, [
        _response(_call(call_id="one")),
        _response(_call(call_id="two")),
        _response(content="The light is off."),
    ])
    tools, calls = _tools()
    result = await AgentLoop(tools).run("turn off kitchen")
    assert len(calls) == 1
    assert len(result["tools"]) == 2

    _client(monkeypatch, [
        _response(_call("plex_now_playing", "{}", "read1")),
        _response(_call("plex_now_playing", "{}", "read2")),
        _response(content="Nothing is playing."),
    ])
    tools, calls = _tools("plex_now_playing")
    await AgentLoop(tools).run("what is playing")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_overlapping_requests_keep_their_channel_and_tool_restrictions(monkeypatch):
    agent = AgentLoop()
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def turn(text, **_kwargs):
        entered.set()
        if text == "first":
            await release.wait()
        seen.append((text, agent._turn.channel, agent._turn.blocked_tools))
        return {"reply": text}

    monkeypatch.setattr(agent, "_run_turn", turn)
    first = asyncio.create_task(agent.run("first", channel="telegram", blocked_tools=frozenset({"radarr_add"})))
    await entered.wait()
    second = asyncio.create_task(agent.run("second", channel="voice"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert seen == [("first", "telegram", frozenset({"radarr_add"})), ("second", "voice", frozenset())]


@pytest.mark.asyncio
async def test_jev_confident_local_request_skips_generative_model(monkeypatch):
    from hearth.jev.schema import ChoiceAnswer, JevAnswers, JevVerdict, NoulAnswer

    async def decision(*_args, **_kwargs):
        return JevVerdict(enabled=True, shadow=False, ok=True, answers=JevAnswers(
            tool_lane=ChoiceAnswer(choice="weather", confidence=0.99),
            needs_llm=NoulAnswer(0.05),
        ))

    def forbidden(*_args, **_kwargs):
        pytest.fail("Jev's ready-to-run local decision should not need another model")

    monkeypatch.setattr("hearth.agent.loop.evaluate_message", decision)
    monkeypatch.setattr("openai.AsyncOpenAI", forbidden)
    monkeypatch.setattr(settings, "openai_api_key", "fake-no-live-provider")
    monkeypatch.setattr(settings, "memory_enabled", False)
    result = await AgentLoop().run("what's the weather")
    assert result["mode"] == "jev_local"
    assert result["tools"][0]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_widget_failure_cannot_turn_a_completed_action_into_failure(monkeypatch):
    def broken(_result):
        raise ValueError("malformed card")

    monkeypatch.setattr("hearth.widgets.publish_tool", broken)
    tools, calls = _tools()
    result = await tools.call("ha_device_control", {"action": "turn_off"})
    assert result.ok
    assert len(calls) == 1


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self):
        self.closed = True


def _event(*names, response_id="resp1", status="completed"):
    return {"type": "response.done", "response": {
        "id": response_id, "status": status,
        "output": [{"type": "function_call", "name": name, "arguments": "{}", "call_id": f"{response_id}-{i}"}
                   for i, name in enumerate(names)],
    }}


async def _drain(band):
    await asyncio.gather(*list(band._jobs))


@pytest.mark.asyncio
async def test_completed_voice_batch_executes_once_and_continues_once(monkeypatch):
    calls = []

    async def run(name, args, **kwargs):
        calls.append((name, kwargs["said"]))
        return {"ok": True, "name": name}

    monkeypatch.setattr(webrtc, "run_house_tool", run)
    band = webrtc.Sideband("rtc_batch")
    band._ws = socket = Socket()
    band._utterance = "show my movies and their download progress"
    await band._on_event({"type": "response.function_call_arguments.done", "name": "plex_search", "call_id": "early"})
    assert calls == []
    event = _event("plex_search", "radarr_queue")
    await band._on_event(event)
    await _drain(band)
    await band._on_event(event)
    await _drain(band)
    assert calls == [("plex_search", band._utterance), ("radarr_queue", band._utterance)]
    assert [e["type"] for e in socket.sent] == ["conversation.item.create", "conversation.item.create", "response.create"]
    await band.close()


@pytest.mark.asyncio
async def test_cancelled_voice_response_does_not_run_actions(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("cancelled response ran a tool")

    monkeypatch.setattr(webrtc, "run_house_tool", forbidden)
    band = webrtc.Sideband("rtc_cancel")
    band._ws = Socket()
    await band._on_event(_event("radarr_add", status="cancelled"))
    await _drain(band)
    assert band._ws.sent == []
    await band.close()


@pytest.mark.asyncio
async def test_stale_response_after_new_speech_cannot_run_actions(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("stale response ran a tool")

    monkeypatch.setattr(webrtc, "run_house_tool", forbidden)
    band = webrtc.Sideband("rtc_stale")
    band._ws = Socket()
    await band._on_event({"type": "response.created", "response": {"id": "resp1"}})
    await band._on_event({"type": "input_audio_buffer.speech_started"})
    await band._on_event(_event("radarr_add"))
    await _drain(band)
    assert band._ws.sent == []
    await band.close()


@pytest.mark.asyncio
async def test_barge_in_during_tool_skips_remaining_actions_and_stale_speech(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(name, *_args, **_kwargs):
        calls.append(name)
        started.set()
        await release.wait()
        return {"ok": True, "name": name}

    monkeypatch.setattr(webrtc, "run_house_tool", run)
    band = webrtc.Sideband("rtc_barge")
    band._ws = Socket()
    band._utterance = "download these movies and series"
    await band._on_event(_event("radarr_add", "sonarr_add"))
    await started.wait()
    await band._on_event({"type": "input_audio_buffer.speech_started"})
    release.set()
    await _drain(band)
    assert calls == ["radarr_add"]
    assert not any(e["type"] == "response.create" for e in band._ws.sent)
    await band.close()


@pytest.mark.asyncio
async def test_voice_invalid_args_return_an_error_without_running(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("invalid arguments executed a tool")

    monkeypatch.setattr(webrtc, "run_house_tool", forbidden)
    band = webrtc.Sideband("rtc_args")
    band._ws = Socket()
    await band._run_function_call("ha_device_control", "[]", "invalid1")
    result = json.loads(band._ws.sent[0]["item"]["output"])
    assert result["data"]["error"] == "invalid_arguments"
    await band.close()


@pytest.mark.asyncio
async def test_voice_write_without_current_transcript_does_not_fail_open(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("a write without current speech cannot be decided by Jev")

    monkeypatch.setattr(webrtc, "run_house_tool", forbidden)
    runtime.note("user", "download an old movie")
    band = webrtc.Sideband("rtc_missing_current")
    band._ws = Socket()
    await band._on_event(_event("radarr_add"))
    await _drain(band)
    result = json.loads(band._ws.sent[0]["item"]["output"])
    assert result["data"]["error"] == "missing_current_utterance"
    await band.close()


@pytest.mark.asyncio
async def test_voice_jev_receives_late_transcript_and_is_reused_for_followup(monkeypatch):
    from hearth.jev.schema import JevAnswers, JevVerdict
    from hearth.jev import current_turn

    seen = []
    verdict = JevVerdict(enabled=True, shadow=False, ok=True, answers=JevAnswers())

    async def run(name, args, **kwargs):
        scope = current_turn()
        seen.append((kwargs["said"], scope.verdict))
        scope.verdict = verdict
        return {"ok": True, "name": name}

    monkeypatch.setattr(webrtc, "run_house_tool", run)
    band = webrtc.Sideband("rtc_context")
    band._ws = Socket()
    await band._on_event({"type": "input_audio_buffer.speech_started", "item_id": "user1"})
    await band._on_event(_event("plex_search"))
    await asyncio.sleep(0)
    await band._on_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": "user1", "transcript": "show horror movies"})
    await _drain(band)
    await band._on_event(_event("radarr_queue", response_id="resp2"))
    await _drain(band)
    assert seen == [("show horror movies", None), ("show horror movies", verdict)]
    await band.close()


@pytest.mark.asyncio
async def test_voice_transcript_is_persisted_once_for_stream_and_final_output():
    band = webrtc.Sideband("rtc_transcript")
    band._ws = Socket()
    await band._on_event({"type": "response.output_audio_transcript.done", "item_id": "msg1", "transcript": "Here are your movies."})
    await band._on_event({"type": "response.done", "response": {"id": "resp1", "output": [{
        "type": "message", "id": "msg1", "content": [{"transcript": "Here are your movies."}],
    }]}})
    await _drain(band)
    assert [t.text for t in runtime.transcript] == ["Here are your movies."]
    await band.close()


@pytest.mark.asyncio
async def test_duplicate_voice_delivery_shares_one_action_even_after_waiter_cancellation(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(name, args, **_kwargs):
        calls.append(name)
        started.set()
        await release.wait()
        return {"ok": True, "name": name}

    monkeypatch.setattr(webrtc, "_execute_house_tool", run)
    options = {"execution_id": "same-delivery", "session_id": "unique-test-session", "said": "download Dune"}
    first = asyncio.create_task(webrtc.run_house_tool("radarr_add", {"query": "Dune"}, **options))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    duplicate = asyncio.create_task(webrtc.run_house_tool("radarr_add", {"query": "Dune"}, **options))
    release.set()
    assert (await duplicate)["ok"]
    assert calls == ["radarr_add"]
    conflict = await webrtc.run_house_tool("sonarr_add", {"query": "Dune"}, **options)
    assert conflict["data"]["error"] == "execution_id_conflict"
    webrtc._executions.pop((options["session_id"], options["execution_id"]), None)


@pytest.mark.asyncio
async def test_voice_tool_timeout_reports_uncertainty_and_does_not_retry(monkeypatch):
    calls = []

    async def slow(*_args, **_kwargs):
        calls.append("attempt")
        await asyncio.Event().wait()

    monkeypatch.setattr(webrtc, "_execute_house_tool_gated", slow)
    monkeypatch.setattr(webrtc, "TOOL_TIMEOUT_SECONDS", 0.01)
    options = {"execution_id": "timeout-delivery", "session_id": "timeout-test-session", "said": "download Dune"}
    result = await webrtc.run_house_tool("radarr_add", {}, **options)
    replay = await webrtc.run_house_tool("radarr_add", {}, **options)
    assert result == replay
    assert result["data"]["uncertain"] is True
    assert calls == ["attempt"]
    webrtc._executions.pop((options["session_id"], options["execution_id"]), None)


@pytest.mark.asyncio
async def test_browser_voice_relay_without_transcript_holds_writes_but_allows_reads(monkeypatch):
    calls = []

    async def execute(name, args, **_kwargs):
        calls.append(name)
        return {"ok": True, "name": name}

    monkeypatch.setattr(webrtc, "_execute_house_tool", execute)
    options = {"execution_id": "relay-missing", "session_id": "relay-test"}
    result = await webrtc.run_house_tool("radarr_add", {}, **options)
    assert result["data"]["error"] == "missing_current_utterance"
    assert calls == []
    result = await webrtc.run_house_tool("plex_search", {}, **options)
    assert result["ok"]
    assert calls == ["plex_search"]
    # Ordinary explicitly invoked tools are not Realtime auto-executions.
    assert (await webrtc.run_house_tool("radarr_add", {}))["ok"]
    webrtc._executions.pop((options["session_id"], options["execution_id"]), None)


@pytest.mark.asyncio
async def test_sideband_eof_cleans_live_state_and_ignores_non_object_json():
    class EofSocket(Socket):
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not hasattr(self, "yielded"):
                self.yielded = True
                return "[]"
            raise StopAsyncIteration

    band = webrtc.Sideband("rtc_eof")
    band._ws = socket = EofSocket()
    runtime.voice_path = webrtc.PATH_ID
    runtime.voice_mode = "live"
    runtime.openai_live = True
    webrtc._sidebands[band.call_id] = band
    await band._listen()
    assert socket.closed
    assert band.call_id not in webrtc._sidebands
    assert runtime.voice_mode == "disconnected"
    assert runtime.openai_live is False


@pytest.mark.asyncio
async def test_voice_memory_refresh_waits_through_tool_continuation(monkeypatch):
    async def run(name, _args, **_kwargs):
        return {"ok": True, "name": name}

    async def instructions(query, **_context):
        return f"remember {query}"

    monkeypatch.setattr(webrtc, "run_house_tool", run)
    monkeypatch.setattr(webrtc, "voice_memory_async", instructions)
    band = webrtc.Sideband("rtc_memory_continuation")
    band._ws = socket = Socket()
    await band._on_event({"type": "response.created", "response": {"id": "resp1"}})
    await band._on_event({"type": "conversation.item.input_audio_transcription.completed", "transcript": "show horror"})
    await band._on_event(_event("plex_search"))
    await _drain(band)
    assert not any(event.get("item", {}).get("role") == "system" for event in socket.sent)
    await band._on_event({"type": "response.created", "response": {"id": "resp2"}})
    await band._on_event(_event(response_id="resp2"))
    await _drain(band)
    updates = [event for event in socket.sent if event.get("item", {}).get("role") == "system"]
    assert len(updates) == 1
    assert updates[0]["item"]["content"][0]["text"].endswith("remember show horror")
    assert not any(event["type"] == "session.update" for event in socket.sent)
    await band.close()


@pytest.mark.asyncio
async def test_cancel_and_done_do_not_finish_a_different_active_response():
    band = webrtc.Sideband("rtc_response_accounting")
    band._ws = Socket()
    for response_id in ("r1", "r1", "r2"):
        await band._on_event({"type": "response.created", "response": {"id": response_id}})
    assert band._active_responses == 2
    await band._on_event({"type": "response.cancelled", "response": {"id": "r1"}})
    await band._on_event(_event(response_id="r1", status="cancelled"))
    assert band._active_responses == 1
    await band._on_event(_event(response_id="r2"))
    await _drain(band)
    assert band._active_responses == 0
    await band.close()


@pytest.mark.asyncio
async def test_partial_voice_transcript_cannot_authorize_a_write(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("partial speech must not authorize a write")

    monkeypatch.setattr(webrtc, "run_house_tool", forbidden)
    band = webrtc.Sideband("rtc_partial_write")
    band._ws = Socket()
    band._partial_user = "download Dune unless"
    await band._run_function_call("radarr_add", "{}", "partial_write1")
    result = json.loads(band._ws.sent[0]["item"]["output"])
    assert result["data"]["error"] == "missing_current_utterance"
    await band.close()


@pytest.mark.asyncio
async def test_completed_tool_result_survives_socket_reopen_without_reexecution(monkeypatch):
    started, finish_action, reconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def run(name, _args, **_kwargs):
        calls.append(name)
        started.set()
        await finish_action.wait()
        return {"ok": True, "name": name}

    monkeypatch.setattr(webrtc, "run_house_tool", run)
    band = webrtc.Sideband("rtc_mid_action_reopen")
    band._ws = Socket()
    band._utterance = "download Dune"
    band._active_responses = 1
    replacement = Socket()

    async def connect():
        await reconnect.wait()
        band._ws = replacement

    monkeypatch.setattr(band, "_connect_socket", connect)
    action = asyncio.create_task(band._run_function_call("radarr_add", "{}", "reopen-write"))
    await started.wait()
    reopening = asyncio.create_task(band._reopen_socket())
    await asyncio.sleep(0)
    assert band._ws is None
    finish_action.set()
    await asyncio.sleep(0)
    reconnect.set()
    assert await reopening
    await action
    await band._run_function_call("radarr_add", "{}", "reopen-write")
    assert calls == ["radarr_add"]
    assert [event["type"] for event in replacement.sent] == [
        "conversation.item.create", "response.create",
    ]
    assert band._active_responses == 0
    await band.close()
