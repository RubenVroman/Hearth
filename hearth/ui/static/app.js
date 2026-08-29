const $ = (id) => document.getElementById(id);

const state = {
  ws: null,
  mode: "fallback",
  recording: false,
  pending: null,
};

async function api(path, opts) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!response.ok) {
    throw new Error(`${path} ${response.status}`);
  }
  return response.json();
}

function fmtMs(ms) {
  if (!ms && ms !== 0) return "";
  const s = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${String(r).padStart(2, "0")}s`;
}

function renderNowPlaying(payload) {
  const root = $("now-playing");
  const session = (payload.sessions || [])[0];
  if (!session) {
    root.innerHTML = `<p class="muted">Nothing on the wire.</p>`;
    return;
  }
  const pct = session.duration_ms
    ? Math.min(100, Math.round((session.progress_ms / session.duration_ms) * 100))
    : 0;
  const show = session.show ? `${session.show} · ` : "";
  root.innerHTML = `
    <p class="kicker" style="margin:0 0 8px">${payload.mode || "plex"}</p>
    <h2>${show}${session.title || "Untitled"}</h2>
    <p class="meta">${session.player || "player"} · ${session.state || "idle"} · ${fmtMs(session.remaining_ms)} left</p>
    <div class="progress"><span style="width:${pct}%"></span></div>
  `;
}

function renderRooms(payload) {
  const lights = $("lights");
  lights.innerHTML = "";
  for (const light of payload.lights || []) {
    const on = light.state === "on";
    const btn = document.createElement("button");
    btn.className = `tile ${on ? "on" : ""}`;
    btn.type = "button";
    btn.innerHTML = `<span>${light.attributes?.friendly_name || light.entity_id}</span><span class="dot"></span>`;
    btn.addEventListener("click", () =>
      invoke("ha_call_service", {
        domain: "light",
        service: on ? "turn_off" : "turn_on",
        entity_id: light.entity_id,
        confirm: true,
      }).then(refresh)
    );
    lights.appendChild(btn);
  }

  const scenes = $("scenes");
  scenes.innerHTML = "";
  for (const scene of payload.scenes || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = scene.attributes?.friendly_name || scene.entity_id;
    btn.addEventListener("click", () =>
      invoke("ha_call_service", {
        domain: "scene",
        service: "turn_on",
        entity_id: scene.entity_id,
        confirm: true,
      }).then(refresh)
    );
    scenes.appendChild(btn);
  }

  const media = $("media-stack");
  media.innerHTML = "";
  for (const player of payload.media || []) {
    const el = document.createElement("button");
    el.type = "button";
    const name = player.attributes?.friendly_name || player.entity_id;
    const extra = player.attributes?.source || player.state;
    el.textContent = `${name} · ${extra}`;
    el.addEventListener("click", () =>
      invoke("ha_call_service", {
        domain: "media_player",
        service: player.state === "off" ? "turn_on" : "turn_off",
        entity_id: player.entity_id,
        confirm: true,
      }).then(refresh)
    );
    media.appendChild(el);
  }
}

function renderStatus(status) {
  $("house").textContent = status.house || "VAULT";
  $("agent-pill").textContent = status.agent || "idle";
  const voice = status.voice || {};
  $("voice-pill").textContent = `voice ${voice.mode || "off"}`;
  $("voice-pill").classList.toggle("live", voice.mode === "live");
  $("mode-pill").textContent = status.openai ? "openai" : "local";
  state.pending = status.pending;
  $("confirm-btn").classList.toggle("hidden", !status.pending);
  if (status.pending) {
    $("confirm-btn").textContent = `Confirm ${status.pending.tool}`;
  }
}

function appendLog(role, text) {
  if (!text) return;
  const log = $("log");
  const li = document.createElement("li");
  li.innerHTML = `<span class="who">${role}</span>${text}`;
  log.appendChild(li);
  log.scrollTop = log.scrollHeight;
}

async function invoke(tool, args) {
  return api("/api/invoke", {
    method: "POST",
    body: JSON.stringify({ tool, args }),
  });
}

async function talk(message, confirm = false) {
  const out = await api("/api/chat", {
    method: "POST",
    body: JSON.stringify({ message, confirm }),
  });
  appendLog("hearth", out.reply);
  return out;
}

async function refresh() {
  const [status, playing, rooms, transcript] = await Promise.all([
    api("/api/status"),
    api("/api/now-playing"),
    api("/api/rooms"),
    api("/api/transcript"),
  ]);
  renderStatus(status);
  renderNowPlaying(playing);
  renderRooms(rooms);
  if ($("log").childElementCount === 0) {
    for (const line of transcript.lines || []) {
      appendLog(line.role, line.text);
    }
  }
}

function connectVoice() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/voice`);
  state.ws = ws;
  ws.addEventListener("message", (ev) => {
    const event = JSON.parse(ev.data);
    if (event.type === "session.ready") {
      state.mode = event.mode;
      $("hint").textContent =
        event.mode === "live"
          ? "Live Realtime. Hold the hearth to speak."
          : event.reason || "Fallback protocol. Type, or hold to send audio once a key is set.";
      $("orb-label").textContent = event.mode === "live" ? "Hold to speak" : "Hold / type";
    }
    if (event.type === "transcript.user") appendLog("you", event.text);
    if (event.type === "transcript.assistant" && event.final) appendLog("hearth", event.text);
    if (event.type === "audio.delta" && event.audio) playPcm(event.audio, event.sample_rate || 24000);
    if (event.type === "status" && event.agent) $("agent-pill").textContent = event.agent;
    if (event.type === "error") appendLog("system", event.message);
    if (event.type === "tool.result") refresh();
  });
  ws.addEventListener("close", () => {
    state.ws = null;
    setTimeout(connectVoice, 1500);
  });
}

function send(event) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(event));
    return true;
  }
  return false;
}

$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("line");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendLog("you", text);
  if (!send({ type: "input_text", text })) {
    await talk(text);
    refresh();
  }
});

$("confirm-btn").addEventListener("click", async () => {
  if (send({ type: "confirm" })) return;
  await talk("confirm", true);
  refresh();
});

let media = {
  ctx: null,
  proc: null,
  stream: null,
};

async function startTalk() {
  if (state.recording) return;
  state.recording = true;
  $("orb").classList.add("hot");
  $("orb-label").textContent = "Listening";
  try {
    media.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    media.ctx = new AudioContext();
    const source = media.ctx.createMediaStreamSource(media.stream);
    const proc = media.ctx.createScriptProcessor(4096, 1, 1);
    media.proc = proc;
    proc.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      const pcm = downsample(input, media.ctx.sampleRate, 24000);
      const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
      let bin = "";
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      send({ type: "input_audio.append", audio: btoa(bin) });
    };
    source.connect(proc);
    proc.connect(media.ctx.destination);
  } catch (err) {
    appendLog("system", `Mic unavailable: ${err.message}`);
    state.recording = false;
    $("orb").classList.remove("hot");
  }
}

function stopTalk() {
  if (!state.recording) return;
  state.recording = false;
  $("orb").classList.remove("hot");
  $("orb-label").textContent = "Hold to speak";
  try {
    media.proc && media.proc.disconnect();
    media.stream && media.stream.getTracks().forEach((t) => t.stop());
    media.ctx && media.ctx.close();
  } catch (_) {
    /* ignore */
  }
  send({ type: "input_audio.commit" });
}

function downsample(float32, fromRate, toRate) {
  if (fromRate === toRate) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }
  const ratio = fromRate / toRate;
  const n = Math.round(float32.length / ratio);
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, float32[Math.floor(i * ratio)] || 0));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

let outCtx = null;
async function playPcm(b64, rate) {
  const raw = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const samples = new Int16Array(raw.buffer, raw.byteOffset, Math.floor(raw.byteLength / 2));
  if (!outCtx) outCtx = new AudioContext({ sampleRate: rate });
  const buffer = outCtx.createBuffer(1, samples.length, rate);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) data[i] = samples[i] / 0x8000;
  const src = outCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(outCtx.destination);
  src.start();
}

const orb = $("orb");
orb.addEventListener("mousedown", startTalk);
orb.addEventListener("mouseup", stopTalk);
orb.addEventListener("mouseleave", () => state.recording && stopTalk());
orb.addEventListener("touchstart", (e) => {
  e.preventDefault();
  startTalk();
});
orb.addEventListener("touchend", (e) => {
  e.preventDefault();
  stopTalk();
});

connectVoice();
refresh();
setInterval(refresh, 8000);
