const $ = (id) => document.getElementById(id);

const state = {
  pending: null,
  call: null,
  openai: false,
  realtime: { path: "webrtc-ga", model: "gpt-realtime-2.1", beta: false },
  accessToken: "",
};

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.accessToken) headers["X-Auth-Token"] = state.accessToken;
  return headers;
}

function bounceToLogin() {
  state.accessToken = "";
  window.location.replace("/login");
}

async function refreshAccessToken() {
  const response = await fetch("/auth/session/refresh", { method: "POST" });
  if (!response.ok) return false;
  const body = await response.json();
  state.accessToken = body.access_token || "";
  return Boolean(state.accessToken);
}

async function request(path, opts = {}, retried = false) {
  const headers = authHeaders(opts.headers || {});
  const response = await fetch(path, { ...opts, headers });
  if (response.status !== 401) return response;
  if (retried) {
    bounceToLogin();
    throw new Error("unauthorized");
  }
  const ok = await refreshAccessToken();
  if (!ok) {
    bounceToLogin();
    throw new Error("unauthorized");
  }
  return request(path, opts, true);
}

async function api(path, opts = {}) {
  const response = await request(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
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

function setEmpty(id, empty) {
  const el = $(id);
  if (el) el.classList.toggle("is-empty", empty);
}

function renderNowPlaying(payload) {
  const root = $("now-playing");
  const session = (payload.sessions || [])[0];
  if (!session) {
    root.innerHTML = `<p class="muted">Nothing on the wire.</p>`;
    setEmpty("now-playing-block", true);
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
  setEmpty("now-playing-block", false);
  setEmpty("rail-media", false);
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
  setEmpty("lights-block", lights.childElementCount === 0);
  setEmpty("scenes-block", scenes.childElementCount === 0);
  setEmpty("rail-rooms", lights.childElementCount === 0 && scenes.childElementCount === 0 && ($("memory-list")?.childElementCount || 0) === 0);
  setEmpty("media-block", media.childElementCount === 0);
  setEmpty("rail-media", $("now-playing-block")?.classList.contains("is-empty") && media.childElementCount === 0);
}

function renderMemory(payload) {
  const list = $("memory-list");
  if (!list) return;
  list.innerHTML = "";
  for (const pref of payload.preferences || []) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `${pref.key || ""}: ${pref.value || ""}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "memory-forget";
    btn.textContent = "Forget";
    btn.addEventListener("click", () =>
      api("/api/memory/forget", {
        method: "POST",
        body: JSON.stringify({ key: pref.key || "", confirm: true }),
      }).then(refresh)
    );
    li.appendChild(label);
    li.appendChild(btn);
    list.appendChild(li);
  }
  setEmpty("memory-block", list.childElementCount === 0);
  const roomsEmpty =
    ($("lights")?.childElementCount || 0) === 0 &&
    ($("scenes")?.childElementCount || 0) === 0 &&
    list.childElementCount === 0;
  setEmpty("rail-rooms", roomsEmpty);
}

function phoneUi() {
  return window.matchMedia("(max-width: 960px)").matches;
}

function idleHint() {
  if (!state.openai) {
    return phoneUi() ? "Text still works." : "Text works now. Live voice needs OPENAI_API_KEY on the NAS — then tap the hearth.";
  }
  if (phoneUi()) return "Tap to talk.";
  const rt = state.realtime || {};
  return `Tap the hearth for a live conversation (${rt.model || "gpt-realtime-2.1"} · ${rt.path || "webrtc-ga"}). Interrupt anytime.`;
}

function renderStatus(status) {
  $("house").textContent = status.house || "VAULT";
  $("agent-pill").textContent = status.agent || "idle";
  const voice = status.voice || {};
  const rt = status.realtime || {};
  state.openai = Boolean(status.openai);
  state.realtime = rt;
  const live = Boolean(state.call) || voice.mode === "live";
  $("voice-pill").textContent = live
    ? `voice ${rt.path || voice.path || "webrtc-ga"}`
    : `voice ${voice.mode || "off"}`;
  $("voice-pill").classList.toggle("live", live);
  $("mode-pill").textContent = rt.beta ? "beta" : status.openai ? "openai" : "local";
  state.pending = status.pending;
  $("confirm-btn").classList.toggle("hidden", !status.pending);
  if (status.pending) {
    $("confirm-btn").textContent = `Confirm ${status.pending.tool}`;
  }
  if (!state.call) {
    $("hint").textContent = idleHint();
    $("orb-label").textContent = "Tap to talk";
  }
}

function appendLog(role, text) {
  if (!text) return;
  const log = $("log");
  const li = document.createElement("li");
  li.innerHTML = `<span class="who">${role}</span>${text}`;
  log.appendChild(li);
  log.scrollTop = log.scrollHeight;
  setEmpty("transcript", false);
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
  const [status, playing, rooms, transcript, memory] = await Promise.all([
    api("/api/status"),
    api("/api/now-playing"),
    api("/api/rooms"),
    api("/api/transcript"),
    api("/api/memory"),
  ]);
  renderStatus(status);
  renderNowPlaying(playing);
  renderRooms(rooms);
  renderMemory(memory);
  if ($("log").childElementCount === 0) {
    for (const line of transcript.lines || []) {
      if (line.kind === "delta") continue;
      appendLog(line.role, line.text);
    }
  }
  setEmpty("transcript", $("log").childElementCount === 0);
}

function sendRealtime(event) {
  const dc = state.call && state.call.dc;
  if (dc && dc.readyState === "open") {
    dc.send(JSON.stringify(event));
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
  if (
    sendRealtime({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text }],
      },
    })
  ) {
    sendRealtime({ type: "response.create" });
    return;
  }
  await talk(text);
  refresh();
});

$("confirm-btn").addEventListener("click", async () => {
  await talk("confirm", true);
  refresh();
});

function onRealtimeEvent(event) {
  const type = event.type;
  if (type === "response.output_audio_transcript.done" || type === "response.audio_transcript.done") {
    appendLog("hearth", event.transcript);
  }
  if (type === "conversation.item.input_audio_transcription.completed") {
    appendLog("you", event.transcript);
  }
  if (type === "error") {
    const message = event.error?.message || event.message || "realtime error";
    appendLog("system", message);
  }
  if (state.call?.sidebandOk) return;
  if (type !== "response.function_call_arguments.done") return;
  relayTool(event);
}

async function relayTool(event) {
  let args = {};
  try {
    args = JSON.parse(event.arguments || "{}");
  } catch (_) {
    args = {};
  }
  try {
    const out = await api("/api/realtime/tools", {
      method: "POST",
      body: JSON.stringify({
        name: event.name,
        arguments: args,
        call_id: event.call_id || "",
      }),
    });
    sendRealtime({
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: event.call_id,
        output: JSON.stringify(out.output || out),
      },
    });
    sendRealtime({ type: "response.create" });
    refresh();
  } catch (err) {
    appendLog("system", `Tool failed: ${err.message}`);
  }
}

async function startConversation() {
  const remote = $("remote-audio");
  const pc = new RTCPeerConnection();
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });
  for (const track of stream.getAudioTracks()) {
    pc.addTrack(track, stream);
  }
  pc.ontrack = (ev) => {
    remote.srcObject = ev.streams[0];
    remote.play().catch(() => {});
  };
  const dc = pc.createDataChannel("oai-events");
  dc.addEventListener("message", (ev) => {
    try {
      onRealtimeEvent(JSON.parse(ev.data));
    } catch (_) {
      /* ignore non-json */
    }
  });
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const sdpResponse = await request("/api/realtime/calls", {
    method: "POST",
    body: offer.sdp,
    headers: { "Content-Type": "application/sdp" },
  });
  const path = sdpResponse.headers.get("X-Hearth-Realtime-Path") || "";
  const beta = sdpResponse.headers.get("X-Hearth-Realtime-Beta") || "";
  if (!sdpResponse.ok) {
    let err = { error: `calls ${sdpResponse.status}` };
    try {
      err = await sdpResponse.json();
    } catch (_) {
      /* ignore */
    }
    stream.getTracks().forEach((t) => t.stop());
    pc.close();
    throw new Error(err.error || err.message || `realtime/calls ${sdpResponse.status}`);
  }
  if (path && path !== "webrtc-ga") {
    stream.getTracks().forEach((t) => t.stop());
    pc.close();
    throw new Error(`unexpected realtime path ${path}`);
  }
  if (beta === "true") {
    stream.getTracks().forEach((t) => t.stop());
    pc.close();
    throw new Error("beta realtime path is disabled");
  }
  const answer = await sdpResponse.text();
  await pc.setRemoteDescription({ type: "answer", sdp: answer });
  const callId = sdpResponse.headers.get("X-Hearth-Call-Id") || "";
  const sideband = sdpResponse.headers.get("X-Hearth-Sideband") || "";
  state.call = {
    pc,
    dc,
    stream,
    callId,
    sidebandOk: sideband === "ok" || sideband === "starting",
  };
  $("orb").classList.add("live", "hot");
  $("orb").setAttribute("aria-label", "End conversation");
  $("orb-label").textContent = "Listening";
  $("hint").textContent = phoneUi() ? "Listening. Tap to hang up." : "Live WebRTC conversation. Talk over it — barge-in is on. Tap to hang up.";
  $("voice-pill").textContent = "voice webrtc-ga";
  $("voice-pill").classList.add("live");
}

async function stopConversation() {
  const call = state.call;
  state.call = null;
  $("orb").classList.remove("live", "hot");
  $("orb").setAttribute("aria-label", "Tap to talk");
  $("orb-label").textContent = "Tap to talk";
  $("hint").textContent = idleHint();
  $("voice-pill").classList.remove("live");
  if (!call) return;
  try {
    call.stream && call.stream.getTracks().forEach((t) => t.stop());
    call.pc && call.pc.close();
  } catch (_) {
    /* ignore */
  }
  $("remote-audio").srcObject = null;
  if (call.callId) {
    try {
      await request(`/api/realtime/calls/${encodeURIComponent(call.callId)}/hangup`, {
        method: "POST",
      });
    } catch (_) {
      /* ignore */
    }
  }
  refresh();
}

$("orb").addEventListener("click", async () => {
  if (state.call) {
    await stopConversation();
    return;
  }
  $("orb").classList.add("hot");
  $("orb-label").textContent = "Connecting";
  $("hint").textContent = phoneUi() ? "Connecting…" : "Opening GA WebRTC session…";
  try {
    await startConversation();
  } catch (err) {
    $("orb").classList.remove("hot", "live");
    $("orb-label").textContent = "Tap to talk";
    $("hint").textContent = idleHint();
    appendLog("system", err.message || "Voice failed");
  }
});

$("logout-btn").addEventListener("click", async () => {
  try {
    await fetch("/auth/session/logout", { method: "POST" });
  } catch (_) {
    /* still leave */
  }
  bounceToLogin();
});

async function boot() {
  const ok = await refreshAccessToken();
  if (!ok) {
    bounceToLogin();
    return;
  }
  refresh();
  setInterval(refresh, 8000);
}

boot();
