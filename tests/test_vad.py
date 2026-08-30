"""Speech-band barge-in gate and Realtime VAD session shape."""

from __future__ import annotations

import json
import math

from hearth.voice import protocol, webrtc
from hearth.voice.vad import (
    CONSECUTIVE_SPEECH_FRAMES,
    MIN_RMS,
    MIN_SPEECH_BAND_RATIO,
    BargeInGate,
    audio_input_config,
    is_speech_frame,
    speech_band_ratio,
    turn_detection_config,
)


def _magnitudes_for_band(
    *,
    sample_rate: float = 16_000.0,
    n_fft: int = 512,
    peak_hz: float,
    peak: float = 1.0,
    floor: float = 0.05,
) -> list[float]:
    """Synthetic linear spectrum peaked around ``peak_hz``."""
    bins = n_fft // 2 + 1
    hz_per_bin = sample_rate / n_fft
    out = [floor] * bins
    center = int(round(peak_hz / hz_per_bin))
    for i in range(bins):
        dist = abs(i - center)
        out[i] = peak * math.exp(-(dist * dist) / 8.0) + floor
    return out


def _flat_spectrum(*, n_fft: int = 512, level: float = 0.4) -> list[float]:
    return [level] * (n_fft // 2 + 1)


def test_speech_band_ratio_prefers_voice_shaped_energy():
    sample_rate = 16_000.0
    n_fft = 512
    speech = _magnitudes_for_band(sample_rate=sample_rate, n_fft=n_fft, peak_hz=1000.0)
    hvac = _magnitudes_for_band(sample_rate=sample_rate, n_fft=n_fft, peak_hz=80.0)
    tv = _flat_spectrum(n_fft=n_fft)

    speech_ratio = speech_band_ratio(speech, sample_rate=sample_rate, n_fft=n_fft)
    hvac_ratio = speech_band_ratio(hvac, sample_rate=sample_rate, n_fft=n_fft)
    tv_ratio = speech_band_ratio(tv, sample_rate=sample_rate, n_fft=n_fft)

    assert speech_ratio >= MIN_SPEECH_BAND_RATIO
    assert hvac_ratio < MIN_SPEECH_BAND_RATIO
    assert tv_ratio < speech_ratio
    assert tv_ratio < MIN_SPEECH_BAND_RATIO or abs(tv_ratio - 0.5) < 0.25


def test_is_speech_frame_rejects_loud_noise_and_quiet_voice_band():
    assert is_speech_frame(0.05, 0.7) is True
    assert is_speech_frame(0.05, 0.1) is False  # loud HVAC / rumble
    assert is_speech_frame(0.005, 0.8) is False  # too quiet
    assert is_speech_frame(MIN_RMS, MIN_SPEECH_BAND_RATIO) is True


def test_barge_in_gate_ignores_noise_bursts_while_assistant_speaks():
    gate = BargeInGate()
    gate.set_assistant_speaking(True)
    assert gate.mic_open is False

    # Short broadband clatter / TV blip — not enough consecutive speech frames.
    for _ in range(CONSECUTIVE_SPEECH_FRAMES - 1):
        assert gate.observe(True) is False
    assert gate.observe(False) is False
    assert gate.mic_open is False

    # Sustained speech-like frames open the mic for barge-in.
    opened = False
    for _ in range(CONSECUTIVE_SPEECH_FRAMES):
        opened = gate.observe(True)
    assert opened is True
    assert gate.mic_open is True


def test_barge_in_gate_stays_open_when_assistant_idle():
    gate = BargeInGate()
    assert gate.observe(False) is True
    gate.set_assistant_speaking(True)
    assert gate.mic_open is False
    gate.set_assistant_speaking(False)
    assert gate.observe(False) is True


def test_noise_like_input_does_not_open_gate_speech_like_does():
    sample_rate = 16_000.0
    n_fft = 512
    gate = BargeInGate()
    gate.set_assistant_speaking(True)

    noise_mags = _magnitudes_for_band(sample_rate=sample_rate, n_fft=n_fft, peak_hz=60.0, peak=1.2)
    noise_ratio = speech_band_ratio(noise_mags, sample_rate=sample_rate, n_fft=n_fft)
    for _ in range(12):
        assert gate.observe(is_speech_frame(0.06, noise_ratio)) is False

    speech_mags = _magnitudes_for_band(sample_rate=sample_rate, n_fft=n_fft, peak_hz=1200.0, peak=1.0)
    speech_ratio = speech_band_ratio(speech_mags, sample_rate=sample_rate, n_fft=n_fft)
    open_mic = False
    for _ in range(CONSECUTIVE_SPEECH_FRAMES):
        open_mic = gate.observe(is_speech_frame(0.04, speech_ratio))
    assert open_mic is True


def test_session_config_enables_noise_reduction_and_semantic_vad():
    cfg = webrtc.session_config()
    audio_in = cfg["audio"]["input"]
    assert audio_in["noise_reduction"] == {"type": "near_field"}
    td = audio_in["turn_detection"]
    assert td["type"] == "semantic_vad"
    assert td["eagerness"] == "low"
    assert td["interrupt_response"] is True
    assert td["create_response"] is True


def test_session_update_payload_matches_audio_input_config():
    payload = protocol.session_update_payload([])
    assert payload["session"]["audio"]["input"] == audio_input_config()
    assert turn_detection_config()["eagerness"] == "low"


def test_create_call_session_json_includes_noise_reduction(client, monkeypatch):
    from unittest.mock import patch

    from hearth.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-test-hearth")
    captured: dict = {}
    answer = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

    class FakeResp:
        is_success = True
        status_code = 201
        text = answer
        headers = {"Location": "/v1/realtime/calls/rtc_vad_call"}

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
            captured["files"] = files
            return FakeResp()

    async def fake_ws_connect(url, additional_headers=None, open_timeout=None):
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

    assert response.status_code == 200
    session = json.loads(captured["files"]["session"][1])
    assert session["audio"]["input"]["noise_reduction"]["type"] == "near_field"
    assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert session["audio"]["input"]["turn_detection"]["eagerness"] == "low"


def test_client_vad_js_mirrors_python_thresholds():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "hearth/ui/static/vad.js").read_text(encoding="utf-8")
    assert "MIN_SPEECH_BAND_RATIO = 0.42" in js
    assert "MIN_RMS = 0.018" in js
    assert "CONSECUTIVE_SPEECH_FRAMES = 4" in js
    assert "SpeechBargeIn" in js
    index = (Path(__file__).resolve().parents[1] / "hearth/ui/static/index.html").read_text(encoding="utf-8")
    assert "/static/vad.js" in index
