"""Live voice session policy and the hide-transcription preference."""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "hearth" / "ui" / "static"
NODE = Path("/exec-daemon/node")


def test_live_transcription_defaults_hidden_in_look_settings(client):
    """Same localStorage Look store as the other knobs. Default is hidden."""
    page = client.get("/")
    assert page.status_code == 200
    assert 'src="/static/voice-session.js"' in page.text
    settings = (UI / "settings.js").read_text(encoding="utf-8")
    assert 'id: "captions"' in settings
    assert 'default: "hidden"' in settings
    assert 'value: "shown"' in settings
    assert 'label: "Live transcription"' in settings
    assert "hearth.look.v1" in settings
    assert "subscribe" in settings
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert 'html[data-captions="hidden"] .spoken-answer' in css
    app_js = (UI / "app.js").read_text(encoding="utf-8")
    assert "liveCaptionsOn" in app_js
    assert "applyCaptionPreference" in app_js
    assert "HearthSettings.subscribe" in app_js
    assert "captionsVisible" in app_js
    # Captions off must not block the spoken utterance used for tools.
    assert "HearthVoiceSession.utteranceText(state.userUtterance)" in app_js
    assert "sendBeacon" in app_js
    assert "replaceTrack" in app_js
    assert "recoverConversation" in app_js
    sw = (UI / "sw.js").read_text(encoding="utf-8")
    assert "hearth-shell-v23" in sw
    assert "/static/voice-session.js" in sw


def test_voice_session_policy_node():
    """Reconnect budget, ICE wait, caption flag, and utterance merge."""
    node_bin = shutil.which("node") or str(NODE if NODE.is_file() else Path("/usr/bin/node"))
    script = r"""
const vs = require('./hearth/ui/static/voice-session.js');
const assert = (cond, msg) => { if (!cond) { console.error(msg); process.exit(1); } };

assert(vs.captionsVisible('shown') === true, 'shown');
assert(vs.captionsVisible('hidden') === false, 'hidden');
assert(vs.captionsVisible(undefined) === false, 'missing defaults hidden');
assert(vs.captionsVisible('Shown') === false, 'case sensitive');

const ice = vs.connectionAction({ connectionState: 'disconnected', iceConnectionState: 'disconnected' });
assert(ice.action === 'wait' && ice.reason === 'ice_disconnected', 'ice blip waits');

const back = vs.connectionAction({ connectionState: 'connected', iceConnectionState: 'connected', dataChannelState: 'open' });
assert(back.action === 'keep' && back.reason === 'connected', 'recovered ice stays');

const failed = vs.connectionAction({ connectionState: 'failed', reconnectsUsed: 0 });
assert(failed.action === 'reconnect' && failed.reason === 'peer_failed', 'first failure reconnects');
assert(vs.connectionAction({connectionState:'connected',iceConnectionState:'failed'}).action === 'reconnect', 'terminal ICE takes precedence over stale connected peer state');
assert(vs.connectionAction({connectionState:'connected',iceConnectionState:'disconnected'}).action === 'wait', 'ICE disconnect waits while peer still reports connected');

const dc = vs.connectionAction({ connectionState: 'connected', dataChannelState: 'closed', reconnectsUsed: 0 });
assert(dc.action === 'reconnect' && dc.reason === 'datachannel_closed', 'dead data channel reconnects');

const spent = vs.connectionAction({ connectionState: 'failed', reconnectsUsed: 1 });
assert(spent.action === 'end' && spent.reason === 'reconnect_exhausted', 'second failure ends');

const user = vs.connectionAction({ connectionState: 'failed', userEnded: true, reconnectsUsed: 0 });
assert(user.action === 'end' && user.reason === 'user', 'user hangup does not reconnect');

assert(vs.shouldRecoverFromServer({
  hasCall: true, voiceMode: 'disconnected', sidebandOk: true, phase: 'live', userEnded: false,
}) === true, 'dead sideband recovers');
assert(vs.shouldRecoverFromServer({
  hasCall: true, voiceMode: 'disconnected', sidebandOk: false, phase: 'live',
}) === false, 'client-relay path is not a sideband recovery');
assert(vs.shouldRecoverFromServer({
  hasCall: true, voiceMode: 'disconnected', sidebandOk: true, phase: 'recovering',
}) === false, 'already recovering');
assert(vs.shouldRecoverFromServer({
  hasCall: true, voiceMode: 'live', sidebandOk: true, phase: 'live',
}) === false, 'healthy sideband stays');

const life = new vs.VoiceLifecycle();
const start = life.beginUserStart();
assert(life.phase === 'connecting', 'start connects');
assert(life.markLive(start) === true, 'mark live');
assert(life.onTransport({ connectionState: 'disconnected' }).action === 'wait', 'lifecycle waits on ice');
const again = life.beginReconnect();
assert(again !== null && life.phase === 'recovering', 'one reconnect');
assert(life.beginReconnect() === null, 'no overlapping reconnect');
assert(life.markLive(again) === true, 'reconnect goes live');
assert(life.beginReconnect() === null, 'budget spent');
assert(life.phase === 'live', 'spent budget leaves the call live for the caller to end');
life.beginUserStop();
assert(life.isCurrent(again) === false, 'stop invalidates epoch');
assert(life.onTransport({ connectionState: 'closed' }).reason === 'user', 'stop is a user end');
life.markIdle();
assert(life.phase === 'idle', 'idle after the ending generation');

const raced = new vs.VoiceLifecycle();
raced.beginUserStart();
raced.beginUserStop();
const newer = raced.beginUserStart();
raced.markIdle();
assert(raced.phase === 'connecting', 'older stop cannot idle a newer start');
assert(raced.isCurrent(newer) === true, 'newer start still current');

let said = vs.mergeUserUtterance(null, { delta: 'dim ' }, true);
said = vs.mergeUserUtterance(said, { delta: 'the kitchen' }, true);
assert(vs.utteranceText(said) === 'dim the kitchen', 'partials concatenate');
said = vs.mergeUserUtterance(said, { transcript: 'dim the kitchen lights' }, false);
assert(vs.utteranceText(said) === 'dim the kitchen lights', 'final wins');
assert(said.partial === '', 'final clears partial');

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
