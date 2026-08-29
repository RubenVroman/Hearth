/**
 * Client speech-band barge-in gate.
 * Thresholds must stay in sync with hearth/voice/vad.py.
 *
 * While the assistant is speaking, the outbound WebRTC mic track stays muted
 * until consecutive frames look like close speech (speech-band energy), not
 * broadband TV / HVAC / clatter. Outside playback the mic stays open.
 */
(function (global) {
  const SPEECH_HZ_LOW = 300;
  const SPEECH_HZ_HIGH = 3400;
  const MIN_RMS = 0.018;
  const MIN_SPEECH_BAND_RATIO = 0.42;
  const CONSECUTIVE_SPEECH_FRAMES = 4;
  const HANGOVER_FRAMES = 10;
  const POLL_MS = 40;

  function speechBandRatio(byteFreq, sampleRate, fftSize) {
    if (!byteFreq || !byteFreq.length || sampleRate <= 0 || fftSize <= 0) return 0;
    const hzPerBin = sampleRate / fftSize;
    let speech = 0;
    let total = 0;
    const limit = Math.min(byteFreq.length, Math.floor(fftSize / 2) + 1);
    for (let i = 0; i < limit; i += 1) {
      const mag = byteFreq[i] / 255;
      if (mag <= 0) continue;
      const energy = mag * mag;
      total += energy;
      const hz = i * hzPerBin;
      if (hz >= SPEECH_HZ_LOW && hz <= SPEECH_HZ_HIGH) speech += energy;
    }
    return total > 0 ? speech / total : 0;
  }

  function frameRms(timeData) {
    if (!timeData || !timeData.length) return 0;
    let sum = 0;
    for (let i = 0; i < timeData.length; i += 1) {
      const v = (timeData[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / timeData.length);
  }

  function isSpeechFrame(rms, bandRatio) {
    return rms >= MIN_RMS && bandRatio >= MIN_SPEECH_BAND_RATIO;
  }

  class BargeInGate {
    constructor(opts = {}) {
      this.consecutiveNeeded = opts.consecutiveNeeded || CONSECUTIVE_SPEECH_FRAMES;
      this.hangoverFrames = opts.hangoverFrames || HANGOVER_FRAMES;
      this.assistantSpeaking = false;
      this._speechStreak = 0;
      this._hangover = 0;
      this._open = true;
    }

    setAssistantSpeaking(speaking) {
      if (speaking === this.assistantSpeaking) return;
      this.assistantSpeaking = speaking;
      this._speechStreak = 0;
      if (speaking) {
        this._open = false;
        this._hangover = 0;
      } else {
        this._open = true;
        this._hangover = 0;
      }
    }

    observe(speechLike) {
      if (!this.assistantSpeaking) {
        this._open = true;
        this._speechStreak = 0;
        this._hangover = 0;
        return true;
      }
      if (speechLike) {
        this._speechStreak += 1;
        if (this._speechStreak >= this.consecutiveNeeded) {
          this._open = true;
          this._hangover = this.hangoverFrames;
        }
      } else {
        this._speechStreak = 0;
        if (this._open && this._hangover > 0) {
          this._hangover -= 1;
        } else if (this._open && this._hangover <= 0) {
          this._open = false;
        }
      }
      return this._open;
    }

    get micOpen() {
      return this._open;
    }
  }

  class SpeechBargeIn {
    /**
     * @param {MediaStreamTrack} track outbound mic track on the peer connection
     * @param {MediaStream} stream local mic stream (analysed; never played)
     */
    constructor(track, stream) {
      this.track = track;
      this.stream = stream;
      this.gate = new BargeInGate();
      this._ctx = null;
      this._analyser = null;
      this._freq = null;
      this._time = null;
      this._timer = null;
      this._running = false;
    }

    async start() {
      if (this._running) return;
      const Ctx = global.AudioContext || global.webkitAudioContext;
      if (!Ctx || !this.track || !this.stream) return;
      this._ctx = new Ctx();
      if (this._ctx.state === "suspended") {
        try {
          await this._ctx.resume();
        } catch (_) {
          /* ignore */
        }
      }
      const source = this._ctx.createMediaStreamSource(this.stream);
      this._analyser = this._ctx.createAnalyser();
      this._analyser.fftSize = 1024;
      this._analyser.smoothingTimeConstant = 0.35;
      source.connect(this._analyser);
      this._freq = new Uint8Array(this._analyser.frequencyBinCount);
      this._time = new Uint8Array(this._analyser.fftSize);
      this._running = true;
      this._applyMic(this.gate.micOpen);
      this._timer = global.setInterval(() => this._tick(), POLL_MS);
    }

    stop() {
      this._running = false;
      if (this._timer != null) {
        global.clearInterval(this._timer);
        this._timer = null;
      }
      if (this._ctx) {
        try {
          this._ctx.close();
        } catch (_) {
          /* ignore */
        }
        this._ctx = null;
      }
      this._analyser = null;
      this._applyMic(true);
    }

    setAssistantSpeaking(speaking) {
      this.gate.setAssistantSpeaking(Boolean(speaking));
      this._applyMic(this.gate.micOpen);
    }

    noteRealtimeEvent(type) {
      if (!type) return;
      if (
        type === "response.output_audio.delta" ||
        type === "response.audio.delta" ||
        type === "output_audio_buffer.started"
      ) {
        this.setAssistantSpeaking(true);
      }
      if (
        type === "response.done" ||
        type === "output_audio_buffer.stopped" ||
        type === "response.cancelled" ||
        type === "input_audio_buffer.speech_started"
      ) {
        // speech_started means barge-in already won — keep mic open for the turn.
        this.setAssistantSpeaking(false);
      }
    }

    _tick() {
      if (!this._running || !this._analyser) return;
      this._analyser.getByteFrequencyData(this._freq);
      this._analyser.getByteTimeDomainData(this._time);
      const rms = frameRms(this._time);
      const ratio = speechBandRatio(this._freq, this._ctx.sampleRate, this._analyser.fftSize);
      const speechLike = isSpeechFrame(rms, ratio);
      const open = this.gate.observe(speechLike);
      this._applyMic(open);
    }

    _applyMic(open) {
      if (this.track && this.track.enabled !== open) {
        this.track.enabled = open;
      }
    }
  }

  global.HearthVad = {
    SPEECH_HZ_LOW,
    SPEECH_HZ_HIGH,
    MIN_RMS,
    MIN_SPEECH_BAND_RATIO,
    CONSECUTIVE_SPEECH_FRAMES,
    HANGOVER_FRAMES,
    speechBandRatio,
    frameRms,
    isSpeechFrame,
    BargeInGate,
    SpeechBargeIn,
  };
})(typeof window !== "undefined" ? window : globalThis);
