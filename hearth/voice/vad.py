"""Speech-aware barge-in helpers.

OpenAI Realtime still owns turn detection, but Hearth raises the bar before
ambient energy (TV, HVAC, clatter) can interrupt playback:

1. Server session: ``noise_reduction`` filters the mic buffer *before* VAD.
2. Client (``/static/vad.js``): speech-band gate mutes the outbound WebRTC
   track while the assistant is speaking until consecutive speech-like frames
   are seen. Thresholds here are mirrored in that script — keep them in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

# Mirrored in hearth/ui/static/vad.js
SPEECH_HZ_LOW = 300.0
SPEECH_HZ_HIGH = 3400.0
MIN_RMS = 0.018
MIN_SPEECH_BAND_RATIO = 0.42
CONSECUTIVE_SPEECH_FRAMES = 4
HANGOVER_FRAMES = 10
NOISE_REDUCTION_TYPE = "near_field"
SEMANTIC_EAGERNESS = "low"


def noise_reduction_config() -> dict[str, str]:
    """GA Realtime ``audio.input.noise_reduction`` — filters before VAD."""
    return {"type": NOISE_REDUCTION_TYPE}


def turn_detection_config() -> dict[str, Any]:
    """Semantic VAD tuned for less eager cut-ins; interrupt still on real speech."""
    return {
        "type": "semantic_vad",
        "eagerness": SEMANTIC_EAGERNESS,
        "create_response": True,
        "interrupt_response": True,
    }


def transcription_config() -> dict[str, str]:
    """GA Realtime input transcription for live user turns in the phone UI."""
    return {"model": "gpt-4o-mini-transcribe"}


def audio_input_config() -> dict[str, Any]:
    return {
        "noise_reduction": noise_reduction_config(),
        "transcription": transcription_config(),
        "turn_detection": turn_detection_config(),
    }


def speech_band_ratio(
    magnitudes: Sequence[float],
    *,
    sample_rate: float,
    n_fft: int,
) -> float:
    """Fraction of spectral energy in the speech band (300–3400 Hz).

    ``magnitudes`` are non-negative linear magnitudes for bins ``0 .. n_fft/2``.
    Broadband / low-frequency noise scores low; voice-shaped spectra score high.
    """
    if not magnitudes or sample_rate <= 0 or n_fft <= 0:
        return 0.0
    hz_per_bin = sample_rate / float(n_fft)
    speech = 0.0
    total = 0.0
    limit = min(len(magnitudes), n_fft // 2 + 1)
    for i in range(limit):
        mag = float(magnitudes[i])
        if mag <= 0:
            continue
        energy = mag * mag
        total += energy
        hz = i * hz_per_bin
        if SPEECH_HZ_LOW <= hz <= SPEECH_HZ_HIGH:
            speech += energy
    if total <= 0:
        return 0.0
    return speech / total


def is_speech_frame(
    rms: float,
    band_ratio: float,
    *,
    min_rms: float = MIN_RMS,
    min_ratio: float = MIN_SPEECH_BAND_RATIO,
) -> bool:
    """True when a single analysis frame looks like close speech, not ambient noise."""
    return rms >= min_rms and band_ratio >= min_ratio


@dataclass
class BargeInGate:
    """Mic open/closed while the assistant is speaking.

    Outside assistant playback the gate stays open (normal turn-taking).
    During playback the outbound track stays muted until consecutive speech-like
    frames arrive, then stays open through a short hangover.
    """

    consecutive_needed: int = CONSECUTIVE_SPEECH_FRAMES
    hangover_frames: int = HANGOVER_FRAMES
    assistant_speaking: bool = False
    _speech_streak: int = 0
    _hangover: int = 0
    _open: bool = True

    def set_assistant_speaking(self, speaking: bool) -> None:
        if speaking == self.assistant_speaking:
            return
        self.assistant_speaking = speaking
        self._speech_streak = 0
        if speaking:
            self._open = False
            self._hangover = 0
        else:
            self._open = True
            self._hangover = 0

    def observe(self, speech_like: bool) -> bool:
        """Feed one frame; returns whether the outbound mic should be enabled."""
        if not self.assistant_speaking:
            self._open = True
            self._speech_streak = 0
            self._hangover = 0
            return True

        if speech_like:
            self._speech_streak += 1
            if self._speech_streak >= self.consecutive_needed:
                self._open = True
                self._hangover = self.hangover_frames
        else:
            self._speech_streak = 0
            if self._open and self._hangover > 0:
                self._hangover -= 1
            elif self._open and self._hangover <= 0:
                self._open = False

        return self._open

    @property
    def mic_open(self) -> bool:
        return self._open
