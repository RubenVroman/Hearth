"""Spoken-answer read-along — live assistant transcript while Hearth is speaking."""

from __future__ import annotations

import subprocess
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "hearth" / "ui" / "static"
SPOKEN_JS = UI / "spoken-answer.js"
NODE = Path("/exec-daemon/node")


def test_spoken_answer_shell_is_wired(client):
    """Panel markup, module, styles, and SW cache are present — no parallel bus."""
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="spoken-answer"' in page.text
    assert 'id="spoken-answer-text"' in page.text
    assert 'src="/static/spoken-answer.js"' in page.text

    js = client.get("/static/spoken-answer.js")
    assert js.status_code == 200
    body = js.text
    assert "HearthSpokenAnswer" in body
    assert "classifyEvent" in body
    assert "response.output_audio_transcript.delta" in body
    assert "response.output_audio_transcript.done" in body
    assert "input_audio_buffer.speech_started" in body
    assert "response.cancelled" in body
    assert "output_audio_buffer.stopped" in body
    assert "HOLD_MS" in body
    assert "onCallEnded" in body
    assert "never break the voice path" in body.lower()

    app_js = client.get("/static/app.js")
    assert "noteSpokenAnswer" in app_js.text
    assert "dismissSpokenAnswer" in app_js.text
    assert "ensureSpokenAnswer" in app_js.text
    # Wired into the existing DC handler — not a second websocket.
    assert "noteSpokenAnswer(type, event)" in app_js.text
    assert "dismissSpokenAnswer({ callEnded: true })" in app_js.text
    # User transcript must not feed the spoken-answer panel.
    assert "User mic transcript is NOT shown on the spoken-answer panel" in app_js.text

    css = client.get("/static/styles.css")
    assert ".spoken-answer" in css.text
    assert ".spoken-answer-glass" in css.text
    assert ".spoken-answer-live" in css.text
    assert ".spoken-answer.is-open" in css.text
    assert ".spoken-answer.is-closing" in css.text
    # Zero flow height — no orb/composer layout jump.
    assert "height: 0" in css.text

    sw = client.get("/sw.js")
    assert "hearth-shell-v20" in sw.text
    assert "/static/spoken-answer.js" in sw.text


def test_spoken_answer_reuses_overlay_dismiss_event_set():
    """Interrupt/dismiss events stay aligned with VAD barge-in stop-speaking set."""
    vad = (UI / "vad.js").read_text(encoding="utf-8")
    spoken = SPOKEN_JS.read_text(encoding="utf-8")
    for event_name in (
        "input_audio_buffer.speech_started",
        "response.cancelled",
        "output_audio_buffer.stopped",
    ):
        assert event_name in vad
        assert event_name in spoken


def test_spoken_answer_lifetime_policy_node():
    """Pure classifier + panel lifetime: reveal → finalize/hold → interrupt/call-end."""
    node_bin = str(NODE if NODE.is_file() else Path("/usr/bin/node"))
    script = r"""
const sa = require('./hearth/ui/static/spoken-answer.js');
const assert = (cond, msg) => { if (!cond) { console.error(msg); process.exit(1); } };

assert(sa.classifyEvent('response.output_audio_transcript.delta') === 'reveal', 'delta→reveal');
assert(sa.classifyEvent('response.audio_transcript.delta') === 'reveal', 'alias delta→reveal');
assert(sa.classifyEvent('response.output_audio_transcript.done') === 'finalize', 'done→finalize');
assert(sa.classifyEvent('response.audio_transcript.done') === 'finalize', 'alias done→finalize');
assert(sa.classifyEvent('input_audio_buffer.speech_started') === 'interrupt', 'barge-in→interrupt');
assert(sa.classifyEvent('response.cancelled') === 'interrupt', 'cancel→interrupt');
assert(sa.classifyEvent('output_audio_buffer.stopped') === 'soft_stop', 'audio stop→soft_stop');
assert(sa.classifyEvent('response.created') === 'reset', 'new turn→reset');
assert(sa.classifyEvent('conversation.item.input_audio_transcription.completed') === 'ignore', 'user transcript ignored');
assert(sa.classifyEvent('response.output_audio.delta') === 'ignore', 'audio delta ignored');

const markup = sa.renderMarkup('Hello ', 'world');
assert(markup.includes('spoken-answer-settled'), 'settled class');
assert(markup.includes('spoken-answer-live'), 'live class');
assert(markup.includes('Hello '), 'settled text');
assert(markup.includes('world'), 'live text');
assert(sa.renderMarkup('<b>', '').includes('&lt;b&gt;'), 'html escaped');

function el(id) {
  return {
    id,
    hidden: true,
    classList: {
      _s: new Set(),
      add(...xs) { xs.forEach(x => this._s.add(x)); },
      remove(...xs) { xs.forEach(x => this._s.delete(x)); },
      contains(x) { return this._s.has(x); },
    },
    offsetWidth: 1,
    innerHTML: '',
    setAttribute() {},
  };
}
const root = el('spoken-answer');
const text = el('spoken-answer-text');
const panel = new sa.SpokenAnswerPanel({ root, text });

panel.onRealtimeEvent('response.output_audio_transcript.delta', { delta: 'Hi ' });
assert(panel._visible === true, 'appears on first delta');
assert(root.hidden === false, 'root shown');
assert(root.classList.contains('is-open'), 'is-open');
assert(panel._full === 'Hi ', 'buffer matches spoken delta');

panel.onRealtimeEvent('response.output_audio_transcript.delta', { delta: 'there' });
assert(panel._full === 'Hi there', 'accumulates spoken text only');

panel.onRealtimeEvent('response.output_audio_transcript.done', { transcript: 'Hi there' });
assert(panel._liveChunk === '', 'live chunk cleared on finalize');
assert(panel._holdTimer != null, 'hold scheduled after done');

panel.onRealtimeEvent('response.output_audio_transcript.delta', { delta: 'More' });
panel.onRealtimeEvent('input_audio_buffer.speech_started', {});
assert(
  panel._holdTimer != null || panel._closing || !panel._visible || root.classList.contains('is-closing'),
  'barge-in starts dismiss'
);

panel.onCallEnded();
assert(panel._visible === false, 'call end hard-dismisses');
assert(root.hidden === true, 'root hidden after call end');
assert(panel._full === '', 'buffer cleared on call end');

// Overlay throw must not escape
panel.root = null;
panel.textEl = null;
panel.onRealtimeEvent('response.output_audio_transcript.delta', { delta: 'x' });
panel.onCallEnded();

console.log('ok');
"""
    result = subprocess.run(
        [node_bin, "-e", script],
        cwd=str(UI.parents[2]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "ok" in result.stdout


def test_spoken_answer_failsoft_in_app_handler():
    """app.js wraps spoken-answer updates so overlay errors cannot break audio."""
    app_js = (UI / "app.js").read_text(encoding="utf-8")
    assert "noteSpokenAnswer" in app_js
    assert "overlay must never break the voice path" in app_js
    # Still drives media overlay from the same transcript (one stream).
    assert "noteOverlayConversation(state.liveAssistantTranscript)" in app_js
