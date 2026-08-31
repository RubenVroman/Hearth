const $ = (id) => document.getElementById(id);

const MIC_CONSTRAINTS = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    channelCount: 1,
  },
};

const MIC_STORAGE = {
  granted: "hearth.mic.granted",
  denied: "hearth.mic.denied",
  gateAt: "hearth.mic.gateAt",
};

/** Re-show the in-app mic explainer at most this often when the OS won't remember. */
const MIC_GATE_COOLDOWN_MS = 14 * 24 * 60 * 60 * 1000;

const state = {
  pending: null,
  call: null,
  /** Kept alive across hangups so iOS Safari / Home Screen PWAs avoid re-prompting. */
  mic: null,
  micPermission: "unknown",
  openai: false,
  realtime: { path: "webrtc-ga", model: "gpt-realtime-2.1", beta: false },
  accessToken: "",
  widgets: [],
  infoSignature: "",
  infoCloseTimer: null,
  /** Soft-hidden by context/idle — widget stays; can reappear without refetch. */
  infoSoftHidden: false,
  /** User focused the glass — keep visible until idle or hard dismiss. */
  infoPinned: false,
  infoHideTimer: null,
  infoIdleTimer: null,
  /** Live assistant transcript buffer for mid-utterance card focus. */
  liveAssistantTranscript: "",
  /** Status poll timer — faster while a voice call is live so overlays appear promptly. */
  refreshTimer: null,
  /** Client-only provisional media cards (title skeletons) pending tool fill-in. */
  localMediaExtras: [],
  /** Last activity payload from /api/status. */
  serverActivity: { phase: "idle", label: "", tool: "" },
  /** Optimistic / client-flash activity (chat submit, fetch errors). */
  localActivity: null,
  localActivityTimer: null,
  /**
   * Live spoken-answer read-along panel (Realtime output transcript).
   * Isolated from the voice path — failures never break audio.
   */
  spokenAnswer: null,
};

/** Client grace before fading when talk is clearly unrelated (ms). */
const OVERLAY_IRRELEVANT_GRACE_MS = 650;
/** Client idle soft-hide while still "relevant" but conversation is quiet (ms). */
const OVERLAY_IDLE_HIDE_MS = 28000;
/** Max stacked media cards in the glass overlay (genre browse + search). */
const MEDIA_STACK_CAP = 12;
/** Horizontal swipe distance (px) to flick to the next/prev card. */
const MEDIA_FLICK_PX = 56;

function micStorageGet(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch (_) {
    return "";
  }
}

function micStorageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (_) {
    /* private mode / quota */
  }
}

function micStorageClear(key) {
  try {
    localStorage.removeItem(key);
  } catch (_) {
    /* ignore */
  }
}

function isStandalonePwa() {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true
  );
}

function isAppleTouchDevice() {
  return (
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
  );
}

/**
 * Query mic permission without calling getUserMedia.
 * Returns "granted" | "denied" | "prompt" | "unknown".
 * iOS Safari / Home Screen PWAs often lack Permissions API support for microphone.
 */
async function queryMicPermission() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return "denied";
  }
  const permissions = navigator.permissions;
  if (!permissions || typeof permissions.query !== "function") {
    return "unknown";
  }
  try {
    const status = await permissions.query({ name: "microphone" });
    if (status) {
      status.onchange = () => {
        state.micPermission = status.state || "unknown";
        if (status.state === "granted") {
          micStorageSet(MIC_STORAGE.granted, "1");
          micStorageClear(MIC_STORAGE.denied);
          hideMicPanels();
        }
        if (status.state === "denied") {
          micStorageSet(MIC_STORAGE.denied, "1");
        }
        if (!state.call) $("hint").textContent = idleHint();
      };
    }
    return (status && status.state) || "unknown";
  } catch (_) {
    return "unknown";
  }
}

function rememberMicGranted() {
  micStorageSet(MIC_STORAGE.granted, "1");
  micStorageClear(MIC_STORAGE.denied);
  state.micPermission = "granted";
}

function rememberMicDenied() {
  micStorageSet(MIC_STORAGE.denied, "1");
  state.micPermission = "denied";
}

function micTracksLive(stream) {
  if (!stream) return false;
  return stream.getAudioTracks().some((t) => t.readyState === "live");
}

function releaseMicStream({ hard = false } = {}) {
  const stream = state.mic;
  if (!stream) return;
  for (const track of stream.getAudioTracks()) {
    try {
      if (hard) track.stop();
      else track.enabled = false;
    } catch (_) {
      /* ignore */
    }
  }
  if (hard) state.mic = null;
}

async function acquireMicStream() {
  if (micTracksLive(state.mic)) {
    for (const track of state.mic.getAudioTracks()) track.enabled = true;
    rememberMicGranted();
    return state.mic;
  }
  if (state.mic) releaseMicStream({ hard: true });
  const stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
  state.mic = stream;
  for (const track of stream.getAudioTracks()) {
    track.addEventListener("ended", () => {
      if (state.mic === stream) state.mic = null;
    });
  }
  rememberMicGranted();
  return stream;
}

function shouldShowMicGate(permission) {
  if (permission === "granted") return false;
  if (permission === "denied") return false;
  if (micStorageGet(MIC_STORAGE.denied) === "1" && permission !== "prompt") {
    /* Sticky denial from a prior NotAllowedError — show settings, not the gate. */
    return false;
  }
  if (micStorageGet(MIC_STORAGE.granted) === "1" && permission === "unknown") {
    /* Previously succeeded; OS may still re-prompt on cold start — skip nag copy. */
    return false;
  }
  const last = Number(micStorageGet(MIC_STORAGE.gateAt) || 0);
  if (last && Date.now() - last < MIC_GATE_COOLDOWN_MS) return false;
  return true;
}

function micSettingsCopy() {
  if (isAppleTouchDevice()) {
    return isStandalonePwa()
      ? "On iPhone: Settings → Hearth → Microphone. Turn it on, then return here and tap the hearth."
      : "On iPhone: Settings → Safari → [site settings] or the aA menu → Website Settings → Microphone. Then tap the hearth again.";
  }
  return "Allow the microphone for this site in your browser settings, then tap the hearth again.";
}

function hideMicPanels() {
  $("mic-gate")?.classList.add("hidden");
  $("mic-denied")?.classList.add("hidden");
}

function showMicGate() {
  hideMicPanels();
  const gate = $("mic-gate");
  if (!gate) return;
  micStorageSet(MIC_STORAGE.gateAt, String(Date.now()));
  const detail = $("mic-gate-detail");
  if (detail) {
    detail.textContent = isAppleTouchDevice()
      ? "iPhone may ask again after you leave the app. Hearth only opens the mic when you tap to talk — never in the background."
      : "Your browser should remember this after you allow it once. Hearth only opens the mic when you tap to talk.";
  }
  gate.classList.remove("hidden");
}

function showMicDenied(message) {
  hideMicPanels();
  const panel = $("mic-denied");
  if (!panel) return;
  const detail = $("mic-denied-detail");
  if (detail) detail.textContent = message || micSettingsCopy();
  panel.classList.remove("hidden");
  $("hint").textContent = "Microphone blocked.";
  $("orb-label").textContent = "Mic blocked";
}

function classifyMicError(err) {
  const name = err && err.name ? err.name : "";
  const message = (err && err.message) || "Voice failed";
  if (name === "NotAllowedError" || name === "PermissionDeniedError" || /permission|not allowed|denied/i.test(message)) {
    rememberMicDenied();
    return { kind: "denied", message: micSettingsCopy() };
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return { kind: "missing", message: "No microphone found on this device." };
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return { kind: "busy", message: "Microphone is in use by another app. Close it and try again." };
  }
  return { kind: "other", message };
}

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

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function isVisualOverlay(widget) {
  const kind = widget && widget.kind;
  return kind === "weather" || kind === "media" || kind === "downloads";
}

function pickVisualOverlay(widgets) {
  const list = (Array.isArray(widgets) ? widgets : []).filter(isVisualOverlay);
  if (!list.length) return null;
  // Prefer a still-relevant panel; otherwise the most recently updated visual.
  const relevant = [...list].reverse().find((w) => {
    const ctx = w && w.context;
    return ctx && ctx.relevant === true;
  });
  if (relevant) return relevant;
  return list[list.length - 1];
}

function mediaItemsOf(widget) {
  if (!widget || widget.kind !== "media") return [];
  const data = widget.data || {};
  const fromServer = Array.isArray(data.items) && data.items.length
    ? data.items.filter((row) => row && typeof row === "object")
    : data.item
      ? [data.item]
      : [];
  const byId = new Map();
  for (const row of fromServer) {
    const id = String(row.id || mediaItemKey(row));
    byId.set(id, { ...row, id });
  }
  for (const row of state.localMediaExtras || []) {
    const id = String(row.id || mediaItemKey(row));
    if (!byId.has(id)) byId.set(id, { ...row, id, skeleton: true });
  }
  // Keep server order. Active title is indicated via active_id + carousel slots —
  // reordering remounts the deck and causes pop-in/out flicker.
  return [...byId.values()].slice(0, MEDIA_STACK_CAP);
}

function mediaItemKey(item) {
  if (!item) return "title:untitled";
  if (item.id) return String(item.id);
  if (item.ratingKey) return `plex:${item.ratingKey}`;
  if (item.tmdbId != null && item.tmdbId !== "") {
    return `tmdb:${item.type || "movie"}:${item.tmdbId}`;
  }
  const slug = String(item.title || "untitled")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return item.year != null ? `title:${slug}:${item.year}` : `title:${slug}`;
}

function overlaySignature(widget) {
  if (!widget) return "";
  const downloads = widget.data && Array.isArray(widget.data.downloads) ? widget.data.downloads : [];
  const progressKey = downloads
    .map((row) => `${row.title || ""}:${row.status || ""}:${row.percent ?? ""}`)
    .join(",");
  const media = mediaItemsOf(widget);
  const mediaKey = media
    .map((row) => `${row.id || ""}:${row.title || ""}:${row.skeleton ? 1 : 0}`)
    .join(",");
  const activeId =
    (widget.context && widget.context.active_id) ||
    (widget.data && widget.data.active_id) ||
    (media[0] && media[0].id) ||
    "";
  return [
    widget.id,
    widget.kind,
    widget.updated_at || "",
    widget.status || "",
    widget.title || "",
    widget.body || "",
    widget.detail || "",
    progressKey,
    mediaKey,
    activeId,
    widget.context && widget.context.relevant === false ? "0" : "1",
  ].join("|");
}

function clearInfoCloseTimer() {
  if (state.infoCloseTimer) {
    clearTimeout(state.infoCloseTimer);
    state.infoCloseTimer = null;
  }
}

function clearOverlayPolicyTimers() {
  if (state.infoHideTimer) {
    clearTimeout(state.infoHideTimer);
    state.infoHideTimer = null;
  }
  if (state.infoIdleTimer) {
    clearTimeout(state.infoIdleTimer);
    state.infoIdleTimer = null;
  }
}

function overlayEntityTopics(widget) {
  const topics = new Set();
  if (!widget) return topics;
  const stop = new Set([
    "the",
    "and",
    "for",
    "movie",
    "film",
    "show",
    "series",
    "weather",
    "download",
    "downloads",
    "part",
    "untitled",
  ]);
  const addChunk = (chunk) => {
    String(chunk || "")
      .toLowerCase()
      .match(/[a-z0-9']{3,}/g)
      ?.forEach((t) => {
        if (!stop.has(t)) topics.add(t);
      });
  };
  if (widget.kind === "media") {
    for (const item of mediaItemsOf(widget)) {
      addChunk(item.title);
      addChunk(item.show);
      for (const tag of item.genres || []) addChunk(tag);
    }
    addChunk(widget.data && widget.data.genre);
    for (const row of (widget.data && widget.data.genres) || []) {
      addChunk(row && row.title ? row.title : row);
    }
  } else {
    addChunk(widget.title);
    if (widget.kind === "weather") {
      addChunk(widget.data && widget.data.place);
      addChunk(widget.data && widget.data.condition);
    }
    if (widget.kind === "downloads") {
      for (const row of (widget.data && widget.data.downloads) || []) {
        addChunk(row.title);
      }
    }
  }
  return topics;
}

function overlayTopics(widget) {
  const ctx = widget && widget.context;
  if (ctx && Array.isArray(ctx.topics) && ctx.topics.length) {
    return ctx.topics.map((t) => String(t).toLowerCase());
  }
  const topics = new Set([String(widget.kind || "").toLowerCase()]);
  overlayEntityTopics(widget).forEach((t) => topics.add(t));
  return [...topics];
}

function textTouchesOverlay(text, widget) {
  if (!text || !widget) return false;
  if (widget.kind === "media") {
    for (const item of mediaItemsOf(widget)) {
      if (titleMentionedInText(text, item.title) || titleMentionedInText(text, item.show)) {
        return true;
      }
    }
  }
  const topics = overlayEntityTopics(widget);
  const tokens = String(text)
    .toLowerCase()
    .match(/[a-z0-9']{3,}/g);
  if (!tokens || !topics.size) return false;
  return tokens.some((t) => topics.has(t));
}

function titleMentionedInText(text, title) {
  const raw = String(text || "").toLowerCase();
  const titleL = String(title || "").trim().toLowerCase();
  if (!raw || !titleL || titleL.length < 3) return false;
  if (raw.includes(titleL)) return true;
  const parts = titleL.match(/[a-z0-9']{3,}/g) || [];
  const stop = new Set(["the", "and", "for", "part", "movie", "film", "show", "series"]);
  const meaningful = parts.filter((p) => !stop.has(p));
  if (meaningful.length >= 2) return meaningful.slice(0, 3).every((p) => raw.includes(p));
  if (meaningful.length === 1) {
    return new RegExp(`\\b${meaningful[0].replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&")}\\b`, "i").test(raw);
  }
  return false;
}

function overlayIsRelevant(widget) {
  if (!widget) return false;
  if (state.infoPinned) return true;
  const ctx = widget.context;
  if (ctx && typeof ctx.relevant === "boolean") return ctx.relevant;
  return true;
}

function scheduleOverlayIdleHide() {
  if (state.infoIdleTimer) {
    clearTimeout(state.infoIdleTimer);
    state.infoIdleTimer = null;
  }
  if (state.infoSoftHidden || state.infoPinned) return;
  const visual = pickVisualOverlay(state.widgets);
  if (!visual) return;
  state.infoIdleTimer = setTimeout(() => {
    state.infoIdleTimer = null;
    if (state.infoPinned) return;
    softHideInfoOverlay();
  }, OVERLAY_IDLE_HIDE_MS);
}

function softHideInfoOverlay() {
  const root = $("info-overlay");
  if (!root || root.hidden) return;
  if (state.infoSoftHidden) return;
  clearOverlayPolicyTimers();
  clearInfoCloseTimer();
  state.infoSoftHidden = true;
  state.infoPinned = false;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    root.classList.remove("is-open", "is-closing");
    root.classList.add("is-soft-hidden");
    root.setAttribute("aria-hidden", "true");
    return;
  }
  root.classList.add("is-closing");
  root.classList.remove("is-open");
  state.infoCloseTimer = setTimeout(() => {
    root.classList.remove("is-closing");
    root.classList.add("is-soft-hidden");
    root.setAttribute("aria-hidden", "true");
    state.infoCloseTimer = null;
  }, 300);
}

function revealInfoOverlay() {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual) {
    closeInfoOverlay({ animate: false });
    return;
  }
  openInfoOverlay(visual);
}

function looksUnrelatedToOverlay(text, widget) {
  if (!text || !widget) return false;
  if (isAckUtterance(text)) return false;
  if (textTouchesOverlay(text, widget)) return false;
  const kind = String(widget.kind || "");
  if (/\b(lights?|scenes?|turn (on|off)|dim |brightness|home assistant)\b/i.test(text)) return true;
  if (/\b(thuisbezorgd|just\s*eat|takeaway|hungry|restaurants?|pizza|burger|sushi|food cart)\b/i.test(text)) {
    return true;
  }
  if (/\b(docker|containers?)\b/i.test(text)) return true;
  if (/\b(workspace|chief of staff|open a pr|pull request)\b/i.test(text)) return true;
  if (kind === "weather" && /\b(movie|film|plex|playing|watch|radarr|sonarr|overseerr|infuse)\b/i.test(text)) {
    return true;
  }
  if (kind === "media" && /\b(weather|forecast|temperature|raining|humidity)\b/i.test(text)) {
    return true;
  }
  if (kind === "downloads" && /\b(weather|forecast|temperature|raining|humidity|lights?|scenes?)\b/i.test(text)) {
    return true;
  }
  // Another title while a media card is up, but not in the stack → leave media domain
  // only if it does not touch stacked titles (already checked). Generic "movie" talk
  // alone should not keep a stale title forever — hide after grace via idle.
  return false;
}

function isAckUtterance(text) {
  const raw = String(text || "").trim();
  if (!raw || raw.length > 48) return false;
  return /^(ok|okay|k|thanks|thank you|thx|got it|cool|nice|great|sure|yep|yeah|yup|alright|perfect|sweet|cheers|awesome|good|fine|noted|understood|sounds good|all good|no problem|np)[.!?]*$/i.test(
    raw
  );
}

/**
 * Focus a stacked media card by id (tap / keyboard / flick on recessed cards).
 */
function focusMediaById(mediaId, { reveal = true } = {}) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual || visual.kind !== "media" || !mediaId) return false;
  const items = mediaItemsOf(visual);
  const hit = items.find((row) => String(row.id || mediaItemKey(row)) === String(mediaId));
  if (!hit) return false;
  const data = visual.data || {};
  const prev = String((visual.context && visual.context.active_id) || data.active_id || "");
  if (prev === String(hit.id) && !state.infoSoftHidden) {
    if (reveal) scheduleOverlayIdleHide();
    return false;
  }
  data.active_id = hit.id;
  data.item = hit;
  // Preserve list order so the carousel can slide without remount thrash.
  data.items = items;
  visual.data = data;
  visual.title = hit.title || visual.title;
  const year = hit.year;
  const type = hit.type || "movie";
  visual.body = [type, year].filter(Boolean).join(" · ");
  if (!visual.context) visual.context = {};
  visual.context.active_id = hit.id;
  visual.context.relevant = true;
  if (reveal) {
    if (state.infoHideTimer) {
      clearTimeout(state.infoHideTimer);
      state.infoHideTimer = null;
    }
    openInfoOverlay(visual);
  }
  return true;
}

/**
 * Cycle the stacked media cards (flick / arrow keys). Positive = next.
 */
function cycleMedia(delta) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual || visual.kind !== "media") return false;
  const items = mediaItemsOf(visual);
  if (items.length < 2) return false;
  const activeId = String(
    (visual.context && visual.context.active_id) || (visual.data && visual.data.active_id) || items[0].id || ""
  );
  let idx = items.findIndex((row) => String(row.id) === activeId);
  if (idx < 0) idx = 0;
  const next = items[(idx + delta + items.length * 8) % items.length];
  if (!next) return false;
  return focusMediaById(next.id, { reveal: true });
}

async function playActiveInInfuse() {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual || visual.kind !== "media") return;
  const item = (visual.data && visual.data.item) || mediaItemsOf(visual)[0];
  if (!item || !item.title) {
    appendLog("system", "Nothing to open in Infuse.");
    return;
  }
  const args = { query: String(item.title) };
  if (item.tmdbId != null && item.tmdbId !== "") args.tmdbId = Number(item.tmdbId);
  if (item.ratingKey) args.ratingKey = String(item.ratingKey);
  if (item.type === "show" || item.type === "episode" || item.type === "season") {
    if (item.season != null) args.season = Number(item.season);
    if (item.episode != null) args.episode = Number(item.episode);
  }
  const btn = $("info-content")?.querySelector("[data-infuse-play]");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Opening…";
  }
  try {
    const out = await invoke("infuse_play", args);
    const data = out.data || out.output || out;
    const speak = (data && data.speak) || out.speak || "";
    if (speak) appendLog("hearth", speak);
    else if (out.ok === false || (data && data.ok === false)) {
      appendLog("system", (data && data.error) || "Infuse play failed.");
    }
    applyWidgetPayload(out);
    refresh();
  } catch (err) {
    appendLog("system", `Infuse play failed: ${err.message}`);
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Open in Infuse";
    }
  }
}

async function browseGenreCategory(genre, { mediaType = "movie" } = {}) {
  const title = String(genre || "").trim();
  if (!title) return;
  flashLocalActivity("thinking", `${title}…`, 20000);
  try {
    const out = await invoke("plex_browse_genre", {
      genre: title,
      type: mediaType === "show" ? "show" : "movie",
    });
    const data = out.data || out.output || out;
    const speak = (data && data.speak) || out.speak || "";
    if (speak) appendLog("hearth", speak);
    applyWidgetPayload(out);
    noteOverlayConversation(title);
  } catch (err) {
    appendLog("system", `Genre browse failed: ${err.message}`);
    flashLocalActivity("error", "Genre browse failed", 4000);
  } finally {
    clearLocalActivity();
    renderActivity(state.serverActivity);
  }
}

/**
 * Focus a stacked media card when live talk names its title.
 * Returns true when the active card changed.
 */
function focusMediaFromText(text, { reveal = true } = {}) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual || visual.kind !== "media" || !text) return false;
  const items = mediaItemsOf(visual);
  if (!items.length) return false;
  const ranked = [...items].sort((a, b) => String(b.title || "").length - String(a.title || "").length);
  let hit = null;
  for (const item of ranked) {
    if (titleMentionedInText(text, item.title) || titleMentionedInText(text, item.show)) {
      hit = item;
      break;
    }
  }
  if (!hit) return false;
  const data = visual.data || {};
  const prev = String((visual.context && visual.context.active_id) || data.active_id || "");
  if (prev === String(hit.id) && !state.infoSoftHidden) {
    if (reveal) scheduleOverlayIdleHide();
    return false;
  }
  data.active_id = hit.id;
  data.item = hit;
  data.items = items;
  visual.data = data;
  visual.title = hit.title || visual.title;
  if (!visual.context) visual.context = {};
  visual.context.active_id = hit.id;
  visual.context.relevant = true;
  if (reveal) {
    if (state.infoHideTimer) {
      clearTimeout(state.infoHideTimer);
      state.infoHideTimer = null;
    }
    openInfoOverlay(visual);
  }
  return true;
}

/**
 * New user/assistant utterance — hide quickly when talk leaves the panel; keep/reveal
 * when it still touches on-screen topics. Server context on the next payload confirms.
 */
function noteOverlayConversation(text) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual) return;
  if (isAckUtterance(text)) {
    scheduleOverlayIdleHide();
    return;
  }
  const focused = focusMediaFromText(text, { reveal: true });
  if (focused) return;
  const related = textTouchesOverlay(text, visual);
  if (related) {
    if (state.infoHideTimer) {
      clearTimeout(state.infoHideTimer);
      state.infoHideTimer = null;
    }
    state.infoPinned = false;
    if (state.infoSoftHidden || !$("info-overlay")?.classList.contains("is-open")) {
      openInfoOverlay(visual);
    } else {
      scheduleOverlayIdleHide();
    }
    return;
  }
  if (!looksUnrelatedToOverlay(text, visual)) {
    // Side chat without a clear domain switch — leave visible until idle / server.
    scheduleOverlayIdleHide();
    return;
  }
  if (state.infoPinned) return;
  if (state.infoHideTimer) clearTimeout(state.infoHideTimer);
  state.infoHideTimer = setTimeout(() => {
    state.infoHideTimer = null;
    if (state.infoPinned) return;
    softHideInfoOverlay();
  }, OVERLAY_IRRELEVANT_GRACE_MS);
}

function pruneLocalMediaExtras(widget) {
  if (!widget || widget.kind !== "media") {
    state.localMediaExtras = [];
    return;
  }
  const data = widget.data || {};
  const serverItems =
    Array.isArray(data.items) && data.items.length ? data.items : data.item ? [data.item] : [];
  const known = new Set(serverItems.map((row) => mediaItemKey(row)));
  state.localMediaExtras = (state.localMediaExtras || []).filter(
    (row) => !known.has(mediaItemKey(row))
  );
}

function renderWidgets(widgets) {
  const list = Array.isArray(widgets) ? widgets : [];
  state.widgets = list;
  const visual = pickVisualOverlay(list);
  if (!visual) {
    state.localMediaExtras = [];
    closeInfoOverlay({ animate: true });
    return;
  }
  pruneLocalMediaExtras(visual);
  if (overlayIsRelevant(visual)) {
    if (state.infoHideTimer) {
      clearTimeout(state.infoHideTimer);
      state.infoHideTimer = null;
    }
    openInfoOverlay(visual);
    return;
  }
  // Keep content for reappear; fade out if currently visible.
  if (!state.infoSoftHidden) {
    if (state.infoSignature !== overlaySignature(visual)) {
      const content = $("info-content");
      if (content) content.innerHTML = overlayInnerHtml(visual);
      state.infoSignature = overlaySignature(visual);
    }
    softHideInfoOverlay();
  }
}

function weatherMarkup(widget) {
  const data = widget.data || {};
  const temp = data.temperature;
  const unit = data.temperature_unit || "°C";
  const condition = data.condition || widget.body || "—";
  const place = widget.title || data.place || "Outside";
  const stats = [];
  if (data.humidity != null) {
    stats.push(`<span>Humidity<strong>${escapeHtml(data.humidity)}%</strong></span>`);
  }
  if (data.wind_speed != null) {
    stats.push(
      `<span>Wind<strong>${escapeHtml(data.wind_speed)} ${escapeHtml(
        data.wind_unit || "km/h"
      )}</strong></span>`
    );
  }
  if (data.mode === "mock") {
    stats.push(`<span>Source<strong>mock</strong></span>`);
  }
  const tempLabel = temp != null ? `${escapeHtml(temp)}<span class="info-weather-unit">${escapeHtml(unit)}</span>` : "—";
  return `
    <div class="info-weather">
      <p class="info-kicker">Weather</p>
      <p class="info-title" id="info-title">${escapeHtml(place)}</p>
      <p class="info-weather-temp">${tempLabel}</p>
      <p class="info-weather-condition">${escapeHtml(condition)}</p>
      ${stats.length ? `<div class="info-weather-stats">${stats.join("")}</div>` : ""}
    </div>
  `;
}

function mediaArtUrl(item) {
  const params = new URLSearchParams();
  if (item.ratingKey) params.set("ratingKey", String(item.ratingKey));
  if (item.tmdbId != null && item.tmdbId !== "") params.set("tmdbId", String(item.tmdbId));
  if (item.posterPath) params.set("posterPath", String(item.posterPath));
  const type = item.type || "movie";
  if (type) params.set("mediaType", String(type));
  if (item.title) params.set("title", String(item.title));
  if (![...params.keys()].some((k) => k === "ratingKey" || k === "tmdbId" || k === "posterPath")) {
    return "";
  }
  return `/api/media/art?${params.toString()}`;
}

function mediaPosterFallback(title) {
  const initials = String(title || "")
    .split(/\s+/)
    .slice(0, 2)
    .map((p) => p[0] || "")
    .join("")
    .toUpperCase();
  return `<div class="info-poster info-poster-fallback" aria-hidden="true">${escapeHtml(
    initials || "·"
  )}</div>`;
}

function mediaCardMarkup(
  item,
  { active = false, slot = 0, labelled = false, genre = "" } = {}
) {
  const title = item.title || "Untitled";
  const year = item.year;
  const type = item.type || "movie";
  const summary = item.summary || "";
  const meta = [year, type !== "movie" ? type : "", item.show, item.contentRating]
    .filter(Boolean)
    .map((v) => escapeHtml(v))
    .join(" · ");
  const art = mediaArtUrl({ ...item, title });
  // /api/media/art returns real JPEG or SVG initials when the session cookie authorizes the GET.
  const poster = art
    ? `<img class="info-poster" src="${escapeHtml(art)}" alt="" width="120" height="180" loading="${
        active ? "eager" : "lazy"
      }" />`
    : mediaPosterFallback(title);
  const itemGenres = Array.isArray(item.genres)
    ? item.genres.map((g) => String(g || "").trim()).filter(Boolean)
    : [];
  const bits = [];
  if (active) {
    if (itemGenres.length) {
      bits.push(
        `<p class="info-media-genres">${itemGenres
          .slice(0, 4)
          .map((g) => `<span class="info-media-genre-tag">${escapeHtml(g)}</span>`)
          .join("")}</p>`
      );
    }
    if (item.skeleton && !summary) {
      bits.push(`<p class="info-detail info-media-skeleton-line">Looking this up…</p>`);
    } else if (summary) {
      bits.push(`<p class="info-detail">${escapeHtml(summary)}</p>`);
    }
    if (item.player) {
      bits.push(
        `<p class="info-detail">${escapeHtml(item.player)}${
          item.state ? ` · ${escapeHtml(item.state)}` : ""
        }</p>`
      );
    }
    if (item.rating != null) {
      bits.push(`<p class="info-detail">Rating ${escapeHtml(item.rating)}</p>`);
    }
    const playable = !item.skeleton && (item.title || item.tmdbId || item.ratingKey);
    if (playable) {
      bits.push(
        `<div class="info-media-actions">
        <button type="button" class="info-infuse-btn" data-infuse-play="1">
          Open in Infuse
        </button>
      </div>`
      );
    }
  }
  const links = item.links && typeof item.links === "object" ? item.links : null;
  const tmdbLink = links && links.tmdb ? String(links.tmdb) : "";
  const imdbLink = links && links.imdb ? String(links.imdb) : "";
  if (active && (tmdbLink || imdbLink)) {
    const linkBits = [];
    if (tmdbLink) {
      linkBits.push(
        `<a class="info-media-link" href="${escapeHtml(tmdbLink)}" target="_blank" rel="noopener noreferrer">TMDB</a>`
      );
    }
    if (imdbLink) {
      linkBits.push(
        `<a class="info-media-link" href="${escapeHtml(imdbLink)}" target="_blank" rel="noopener noreferrer">IMDb</a>`
      );
    }
    bits.push(`<p class="info-media-links">${linkBits.join(" · ")}</p>`);
  }
  const kicker = item.skeleton
    ? "Mentioned"
    : item.pending
      ? "Ready to play"
      : item.source === "suggest"
        ? "Suggested"
        : item.player && item.state === "opening"
          ? "Opening"
          : item.player
            ? "Now playing"
            : genre
              ? escapeHtml(genre)
              : "Library";
  const slotAbs = Math.abs(Number(slot) || 0);
  const classes = [
    "info-media-card",
    active ? "is-active is-front" : "is-recessed is-selectable",
    item.skeleton ? "is-skeleton" : "",
    item.source === "suggest" ? "is-suggest" : "",
    item.pending ? "is-pending" : "",
    slotAbs > 2 ? "is-far" : "",
    Number(slot) < 0 ? "is-peek-prev" : "",
    Number(slot) > 0 ? "is-peek-next" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return `
    <article
      class="${classes}"
      data-media-id="${escapeHtml(String(item.id || mediaItemKey(item)))}"
      style="--slot:${Number(slot) || 0};--slot-abs:${slotAbs}"
      aria-hidden="${active ? "false" : "true"}"
      ${!active ? 'tabindex="0" role="button" aria-label="Show this title"' : ""}
    >
      <div class="info-media">
        ${poster}
        <div class="info-media-copy">
          <p class="info-kicker">${kicker}</p>
          <h2 class="info-title"${labelled ? ' id="info-title"' : ""}>${escapeHtml(title)}</h2>
          ${meta ? `<p class="info-meta">${meta}</p>` : ""}
          ${bits.join("")}
        </div>
      </div>
    </article>
  `;
}

function mediaStackCounter(items, activeId, total) {
  if (!items || items.length < 2) return "";
  let idx = items.findIndex((row) => String(row.id) === String(activeId || ""));
  if (idx < 0) idx = 0;
  const shown = items.length;
  const totalN = total != null && Number(total) > shown ? Number(total) : shown;
  const label =
    totalN > shown ? `${idx + 1} / ${shown} · ${totalN} in library` : `${idx + 1} / ${shown}`;
  const dots = items
    .slice(0, Math.min(shown, 12))
    .map(
      (_, i) =>
        `<button type="button" class="info-media-dot${i === idx ? " is-on" : ""}" data-media-dot="${i}" aria-label="Show title ${i + 1}" ${i === idx ? 'aria-current="true"' : ""}></button>`
    )
    .join("");
  return `<div class="info-media-rail" aria-label="${escapeHtml(label)}">
    <p class="info-media-count">${escapeHtml(label)}</p>
    <div class="info-media-dots">${dots}</div>
    <p class="info-media-hint">Swipe or tap cards to browse</p>
  </div>`;
}

function mediaGenreChips(genres, activeGenre = "", { mediaType = "movie" } = {}) {
  if (!Array.isArray(genres) || !genres.length) return "";
  const active = String(activeGenre || "").toLowerCase();
  const chips = genres
    .slice(0, 18)
    .map((row) => {
      const title = String((row && row.title) || "").trim();
      if (!title) return "";
      const size = row && row.size != null ? Number(row.size) : null;
      const label =
        size != null && Number.isFinite(size) ? `${title} · ${size}` : title;
      const on = active && title.toLowerCase() === active;
      return `<button type="button" class="info-genre-chip${on ? " is-on" : ""}" data-genre-browse="${escapeHtml(
        title
      )}" data-media-type="${escapeHtml(mediaType)}" aria-pressed="${on ? "true" : "false"}">${escapeHtml(
        label
      )}</button>`;
    })
    .filter(Boolean)
    .join("");
  if (!chips) return "";
  return `<div class="info-genre-chips" role="list" aria-label="Browse by genre">${chips}</div>`;
}

function mediaGenresMarkup(widget) {
  const data = widget.data || {};
  const genres = Array.isArray(data.genres) ? data.genres : [];
  const mediaType = data.media_type || "movie";
  const kindLabel = mediaType === "show" ? "shows" : "movies";
  const chips = mediaGenreChips(genres, "", { mediaType });
  return `
    <div class="info-genre-browser">
      <p class="info-kicker">Library</p>
      <h2 class="info-title" id="info-title">${escapeHtml(widget.title || `${kindLabel} by genre`)}</h2>
      <p class="info-meta">${escapeHtml(widget.detail || `Tap a genre to see ${kindLabel} in that category.`)}</p>
      ${chips || `<p class="info-detail">No genres found in the Plex library.</p>`}
      <p class="info-detail info-genre-hint">Categories come from Plex metadata (e.g. Science Fiction).</p>
    </div>
  `;
}

function mediaMarkup(widget) {
  const data = widget.data || {};
  if (data.presentation === "genres" || data.listed_genres) {
    return mediaGenresMarkup(widget);
  }
  const items = mediaItemsOf(widget);
  const genre = data.genre || "";
  const total = data.total;
  const mediaType = data.media_type || "movie";
  const genreChips = mediaGenreChips(data.genres || [], genre, { mediaType });
  const activeId =
    (widget.context && widget.context.active_id) || data.active_id || (items[0] && items[0].id) || "";
  const statusBanner = mediaStatusBanner(widget);
  if (!items.length) {
    const item = data.item || {};
    const title = widget.title || item.title || "";
    if (!title && !genreChips) {
      return `${statusBanner}${emptyMediaMarkup(widget)}`;
    }
    return `${statusBanner}${genreChips}${mediaCardMarkup(
      { ...item, id: mediaItemKey(item), title: title || "Untitled" },
      { active: true, slot: 0, labelled: true, genre }
    )}`;
  }
  if (items.length === 1) {
    return `${statusBanner}${genreChips}<div class="info-media-stack is-single" data-count="1">${mediaCardMarkup(items[0], {
      active: true,
      slot: 0,
      labelled: true,
      genre,
    })}</div>`;
  }
  let activeIdx = items.findIndex((row) => String(row.id) === String(activeId));
  if (activeIdx < 0) activeIdx = 0;
  const cards = items
    .map((item, index) =>
      mediaCardMarkup(item, {
        active: index === activeIdx,
        slot: index - activeIdx,
        labelled: index === activeIdx,
        genre,
      })
    )
    .join("");
  return `${statusBanner}${genreChips}<div class="info-media-stack is-stacked is-carousel" data-count="${items.length}" data-flick="1" data-active-idx="${activeIdx}">
    ${mediaStackCounter(items, activeId, total)}
    <div class="info-media-carousel">
      <button type="button" class="info-media-nav info-media-prev" data-media-nav="-1" aria-label="Previous title">‹</button>
      <div class="info-media-deck">${cards}</div>
      <button type="button" class="info-media-nav info-media-next" data-media-nav="1" aria-label="Next title">›</button>
    </div>
  </div>`;
}

function mediaStatusBanner(widget) {
  if (!widget) return "";
  const data = widget.data || {};
  const status = String(widget.status || "");
  const body = String(widget.body || "").trim();
  const detail = String(widget.detail || "").trim();
  if (data.pick || status === "info") {
    const msg = body || "Which title should I play?";
    return `<div class="info-media-banner is-pick" role="status">
      <p class="info-media-banner-title">${escapeHtml(msg)}</p>
      ${detail ? `<p class="info-media-banner-detail">${escapeHtml(detail)}</p>` : ""}
    </div>`;
  }
  if (status === "error") {
    const msg = body || "Could not start playback.";
    return `<div class="info-media-banner is-error" role="alert">
      <p class="info-media-banner-title">${escapeHtml(msg)}</p>
      ${detail && detail !== msg ? `<p class="info-media-banner-detail">${escapeHtml(detail)}</p>` : ""}
    </div>`;
  }
  return "";
}

function emptyMediaMarkup(widget) {
  const title = (widget && widget.title) || "Nothing to play";
  const body =
    (widget && (widget.body || widget.detail)) ||
    "No playable title loaded. Dismiss and ask again, or name the movie.";
  return `<div class="info-media-empty" role="status">
    <p class="info-kicker">Play</p>
    <h2 class="info-title" id="info-title">${escapeHtml(title)}</h2>
    <p class="info-detail">${escapeHtml(body)}</p>
  </div>`;
}

function emptyOverlayMarkup(widget) {
  if (widget && widget.kind === "media") return emptyMediaMarkup(widget);
  const title = (widget && widget.title) || "Nothing to show";
  const body = (widget && (widget.body || widget.detail)) || "This panel had no content.";
  return `
    <p class="info-kicker">Info</p>
    <h2 class="info-title" id="info-title">${escapeHtml(title)}</h2>
    <p class="info-body">${escapeHtml(body)}</p>
  `;
}

function downloadStatusClass(status) {
  const key = String(status || "unknown").toLowerCase();
  if (
    key === "queued" ||
    key === "downloading" ||
    key === "paused" ||
    key === "importing" ||
    key === "stalled" ||
    key === "completed" ||
    key === "failed" ||
    key === "unknown"
  ) {
    return key;
  }
  return "unknown";
}

function downloadsMarkup(widget) {
  const data = widget.data || {};
  const downloads = Array.isArray(data.downloads) ? data.downloads : [];
  const service = data.service === "sonarr" ? "Sonarr" : "Radarr";
  const empty = data.empty;
  const kicker = empty === "idle" || empty === "missing" ? "Downloads" : `${service} queue`;

  if (!downloads.length) {
    const calmTitle =
      empty === "missing"
        ? widget.title || data.query || "Not downloading"
        : widget.title || service;
    const calmBody =
      empty === "missing"
        ? widget.detail || `Not in the ${service} queue right now.`
        : widget.body || "Nothing downloading";
    const calmHint =
      empty === "idle" ? widget.detail || "Queue is quiet." : empty === "missing" ? "" : widget.detail || "";
    return `
      <div class="info-downloads is-empty">
        <p class="info-kicker">${escapeHtml(kicker)}</p>
        <h2 class="info-title" id="info-title">${escapeHtml(calmTitle)}</h2>
        <p class="info-downloads-empty">${escapeHtml(calmBody)}</p>
        ${calmHint ? `<p class="info-detail">${escapeHtml(calmHint)}</p>` : ""}
      </div>
    `;
  }

  const rows = downloads
    .map((row) => {
      const status = row.status || "unknown";
      const pct = row.percent != null && !Number.isNaN(Number(row.percent)) ? Number(row.percent) : null;
      const pctLabel = pct != null ? `${pct}%` : "—";
      const width = pct != null ? Math.max(0, Math.min(100, pct)) : 0;
      const meta = [row.timeleft ? `${row.timeleft} left` : "", row.sizeleft_label ? `${row.sizeleft_label} left` : "", row.quality]
        .filter(Boolean)
        .map((v) => escapeHtml(v))
        .join(" · ");
      return `
        <li class="info-download-row status-${escapeHtml(downloadStatusClass(status))}">
          <div class="info-download-head">
            <p class="info-download-title">${escapeHtml(row.title || "Untitled")}</p>
            <p class="info-download-pct">${escapeHtml(String(pctLabel))}</p>
          </div>
          <div class="info-download-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"
            ${pct != null ? `aria-valuenow="${escapeHtml(String(width))}"` : 'aria-valuetext="unknown"'}
            aria-label="${escapeHtml(row.title || "Download")} progress">
            <span style="width:${width}%"></span>
          </div>
          <p class="info-download-meta">
            <span class="info-download-status">${escapeHtml(status)}</span>
            ${meta ? `<span class="info-download-extra">${meta}</span>` : ""}
          </p>
        </li>
      `;
    })
    .join("");

  const heading =
    downloads.length === 1 ? downloads[0].title || widget.title || service : widget.title || service;
  const sub = downloads.length === 1 ? widget.body || "" : widget.body || `${downloads.length} active`;

  return `
    <div class="info-downloads">
      <p class="info-kicker">${escapeHtml(kicker)}</p>
      <h2 class="info-title" id="info-title">${escapeHtml(heading)}</h2>
      ${sub ? `<p class="info-meta">${escapeHtml(sub)}</p>` : ""}
      <ul class="info-download-list">${rows}</ul>
      ${data.mode === "mock" ? `<p class="info-detail">mock</p>` : ""}
    </div>
  `;
}

function overlayInnerHtml(widget) {
  if (!widget) return emptyOverlayMarkup(null);
  if (widget.kind === "weather") return weatherMarkup(widget);
  if (widget.kind === "media") return mediaMarkup(widget);
  if (widget.kind === "downloads") return downloadsMarkup(widget);
  const html = `
    <p class="info-kicker">Info</p>
    <h2 class="info-title" id="info-title">${escapeHtml(widget.title || "")}</h2>
    <p class="info-body">${escapeHtml(widget.body || "")}</p>
    ${widget.detail ? `<p class="info-detail">${escapeHtml(widget.detail)}</p>` : ""}
  `;
  if (!String(widget.title || "").trim() && !String(widget.body || "").trim()) {
    return emptyOverlayMarkup(widget);
  }
  return html;
}

function openInfoOverlay(widget) {
  const root = $("info-overlay");
  const content = $("info-content");
  if (!root || !content || !widget) return;
  clearInfoCloseTimer();
  const signature = overlaySignature(widget);
  const alreadyOpen =
    root.classList.contains("is-open") &&
    !root.classList.contains("is-closing") &&
    !root.classList.contains("is-soft-hidden") &&
    !state.infoSoftHidden;
  const contentEmpty = !String(content.innerHTML || "").trim();
  // Never keep a blank glass open — remount when the DOM was cleared or markup is empty.
  if (alreadyOpen && state.infoSignature === signature && !contentEmpty) {
    scheduleOverlayIdleHide();
    return;
  }
  let html = "";
  try {
    html = overlayInnerHtml(widget);
  } catch (err) {
    html = emptyOverlayMarkup(widget);
  }
  if (!String(html || "").trim()) {
    html = emptyOverlayMarkup(widget);
  }
  // Still nothing meaningful → dismiss cleanly instead of a hollow popup.
  if (!String(html || "").trim()) {
    closeInfoOverlay({ animate: false });
    return;
  }
  content.innerHTML = html;
  state.infoSignature = signature;
  state.infoSoftHidden = false;
  root.hidden = false;
  root.setAttribute("aria-hidden", "false");
  root.classList.remove("is-closing", "is-soft-hidden");
  // Content swaps while the glass stays up should not replay enter/pop animations
  // (that was the main movie-card flicker when cycling or transcript-focusing).
  if (alreadyOpen && !contentEmpty) {
    content.dataset.settled = "1";
  } else {
    delete content.dataset.settled;
    // Force style flush so enter transition runs when opening from hidden / soft-hidden.
    void root.offsetWidth;
  }
  root.classList.add("is-open");
  scheduleOverlayIdleHide();
}

function closeInfoOverlay({ animate = true } = {}) {
  const root = $("info-overlay");
  clearOverlayPolicyTimers();
  state.infoSoftHidden = false;
  state.infoPinned = false;
  if (!root || root.hidden) {
    state.infoSignature = "";
    return;
  }
  clearInfoCloseTimer();
  state.infoSignature = "";
  root.classList.remove("is-soft-hidden");
  const content = $("info-content");
  if (content) delete content.dataset.settled;
  if (!animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    root.classList.remove("is-open", "is-closing");
    root.hidden = true;
    root.setAttribute("aria-hidden", "true");
    if (content) content.innerHTML = "";
    return;
  }
  root.classList.add("is-closing");
  root.classList.remove("is-open");
  state.infoCloseTimer = setTimeout(() => {
    root.classList.remove("is-closing");
    root.hidden = true;
    root.setAttribute("aria-hidden", "true");
    if (content) content.innerHTML = "";
    state.infoCloseTimer = null;
  }, 300);
}

async function dismissWidget(id, { silent = false } = {}) {
  const next = state.widgets.filter((w) => w.id !== id);
  state.localMediaExtras = [];
  renderWidgets(next);
  try {
    await api(`/api/widgets/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (err) {
    if (!silent) appendLog("system", `Dismiss failed: ${err.message}`);
  }
}

function applyWidgetPayload(payload) {
  if (payload && Array.isArray(payload.widgets)) {
    renderWidgets(payload.widgets);
  }
}

function bindInfoOverlay() {
  const dismiss = () => {
    const visual = pickVisualOverlay(state.widgets);
    if (visual) dismissWidget(visual.id);
    else closeInfoOverlay({ animate: true });
  };
  const pinFromUser = () => {
    const visual = pickVisualOverlay(state.widgets);
    if (!visual) return;
    state.infoPinned = true;
    if (state.infoHideTimer) {
      clearTimeout(state.infoHideTimer);
      state.infoHideTimer = null;
    }
    if (state.infoSoftHidden) openInfoOverlay(visual);
    else scheduleOverlayIdleHide();
  };
  $("info-dismiss")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    dismiss();
  });
  $("info-backdrop")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    dismiss();
  });
  $("info-glass")?.addEventListener("pointerdown", () => {
    pinFromUser();
  });
  // Selectable stacked cards + Infuse play (event delegation survives re-renders).
  $("info-content")?.addEventListener("click", (ev) => {
    const target = ev.target;
    if (!(target instanceof Element)) return;
    const genreBtn = target.closest("[data-genre-browse]");
    if (genreBtn) {
      ev.preventDefault();
      ev.stopPropagation();
      pinFromUser();
      browseGenreCategory(genreBtn.getAttribute("data-genre-browse") || "", {
        mediaType: genreBtn.getAttribute("data-media-type") || "movie",
      });
      return;
    }
    const playBtn = target.closest("[data-infuse-play]");
    if (playBtn) {
      ev.preventDefault();
      ev.stopPropagation();
      pinFromUser();
      playActiveInInfuse();
      return;
    }
    const nav = target.closest("[data-media-nav]");
    if (nav) {
      ev.preventDefault();
      ev.stopPropagation();
      pinFromUser();
      cycleMedia(Number(nav.getAttribute("data-media-nav")) || 1);
      return;
    }
    const dot = target.closest("[data-media-dot]");
    if (dot) {
      ev.preventDefault();
      ev.stopPropagation();
      const visual = pickVisualOverlay(state.widgets);
      const items = visual ? mediaItemsOf(visual) : [];
      const idx = Number(dot.getAttribute("data-media-dot"));
      if (items[idx]) {
        pinFromUser();
        focusMediaById(items[idx].id, { reveal: true });
      }
      return;
    }
    const card = target.closest(".info-media-card.is-selectable, .info-media-card.is-recessed");
    if (card && card.dataset.mediaId) {
      ev.preventDefault();
      pinFromUser();
      focusMediaById(card.dataset.mediaId, { reveal: true });
    }
  });
  $("info-content")?.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const target = ev.target;
    if (!(target instanceof Element)) return;
    const card = target.closest(".info-media-card.is-selectable");
    if (!card || !card.dataset.mediaId) return;
    ev.preventDefault();
    pinFromUser();
    focusMediaById(card.dataset.mediaId, { reveal: true });
  });

  // Flick / swipe through the stacked genre (or search) cards.
  let flick = null;
  let suppressClickUntil = 0;
  const content = $("info-content");
  content?.addEventListener(
    "pointerdown",
    (ev) => {
      if (!(ev.target instanceof Element)) return;
      if (ev.target.closest("[data-infuse-play], [data-genre-browse], .info-dismiss, button, a, [data-media-nav], [data-media-dot]")) return;
      const stack = ev.target.closest(".info-media-stack.is-stacked");
      if (!stack) return;
      flick = { x: ev.clientX, y: ev.clientY, id: ev.pointerId, moved: false };
      try {
        content.setPointerCapture(ev.pointerId);
      } catch {
        /* ignore capture failures on older engines */
      }
    },
    { passive: true }
  );
  content?.addEventListener(
    "pointermove",
    (ev) => {
      if (!flick || flick.id !== ev.pointerId) return;
      const dx = ev.clientX - flick.x;
      const dy = ev.clientY - flick.y;
      if (Math.abs(dx) > 12 || Math.abs(dy) > 12) flick.moved = true;
    },
    { passive: true }
  );
  const endFlick = (ev) => {
    if (!flick || flick.id !== ev.pointerId) return;
    const dx = ev.clientX - flick.x;
    const dy = ev.clientY - flick.y;
    const wasFlick = flick.moved;
    flick = null;
    if (!wasFlick) return;
    if (Math.abs(dx) < MEDIA_FLICK_PX && Math.abs(dy) < MEDIA_FLICK_PX) return;
    // Prefer the dominant axis: horizontal or upward vertical advances.
    suppressClickUntil = Date.now() + 400;
    pinFromUser();
    if (Math.abs(dx) >= Math.abs(dy)) {
      cycleMedia(dx < 0 ? 1 : -1);
    } else if (dy < 0) {
      cycleMedia(1);
    } else {
      cycleMedia(-1);
    }
  };
  content?.addEventListener("pointerup", endFlick);
  content?.addEventListener("pointercancel", () => {
    flick = null;
  });

  // Re-bind click handler path: skip synthetic click right after a flick.
  content?.addEventListener(
    "click",
    (ev) => {
      if (Date.now() < suppressClickUntil) {
        ev.preventDefault();
        ev.stopPropagation();
      }
    },
    true
  );

  document.addEventListener("keydown", (ev) => {
    const root = $("info-overlay");
    if (!root || root.hidden) return;
    if (!root.classList.contains("is-open") && !root.classList.contains("is-soft-hidden")) return;
    if (ev.key === "Escape") {
      dismiss();
      return;
    }
    if (ev.key === "ArrowRight" || ev.key === "ArrowDown") {
      const visual = pickVisualOverlay(state.widgets);
      if (!visual || visual.kind !== "media") return;
      if (mediaItemsOf(visual).length < 2) return;
      ev.preventDefault();
      pinFromUser();
      cycleMedia(1);
      return;
    }
    if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") {
      const visual = pickVisualOverlay(state.widgets);
      if (!visual || visual.kind !== "media") return;
      if (mediaItemsOf(visual).length < 2) return;
      ev.preventDefault();
      pinFromUser();
      cycleMedia(-1);
    }
  });
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
  if (state.micPermission === "denied" || micStorageGet(MIC_STORAGE.denied) === "1") {
    return phoneUi() ? "Mic blocked — check Settings." : "Microphone blocked. Enable it in browser or system settings, then tap the hearth.";
  }
  if (phoneUi()) return "Tap to talk.";
  const rt = state.realtime || {};
  return `Tap the hearth for a live conversation (${rt.model || "gpt-realtime-2.1"} · ${rt.path || "webrtc-ga"}). Real speech interrupts; noise should not.`;
}

const ACTIVITY_IDLE_PHASES = new Set(["idle", "listening", "speaking"]);

function renderActivity(activity) {
  const el = $("activity");
  const labelEl = $("activity-label");
  if (!el || !labelEl) return;
  const src = state.localActivity || activity || state.serverActivity || {};
  const phase = String(src.phase || "idle");
  const label = String(src.label || "").trim();
  const show = Boolean(label) && !ACTIVITY_IDLE_PHASES.has(phase);
  const classes = ["activity", `is-${phase || "idle"}`];
  if (!show) classes.push("is-idle");
  el.className = classes.join(" ");
  labelEl.textContent = show ? label : "";
  el.hidden = !show;
  el.setAttribute("aria-hidden", show ? "false" : "true");
}

function flashLocalActivity(phase, label, holdMs = 4000) {
  state.localActivity = { phase, label, tool: "" };
  renderActivity(state.serverActivity);
  if (state.localActivityTimer) {
    clearTimeout(state.localActivityTimer);
    state.localActivityTimer = null;
  }
  state.localActivityTimer = setTimeout(() => {
    state.localActivity = null;
    state.localActivityTimer = null;
    renderActivity(state.serverActivity);
  }, holdMs);
}

function clearLocalActivity() {
  if (state.localActivityTimer) {
    clearTimeout(state.localActivityTimer);
    state.localActivityTimer = null;
  }
  state.localActivity = null;
}

function renderStatus(status) {
  $("house").textContent = status.house || "VAULT";
  const activity = status.activity || {};
  state.serverActivity = {
    phase: activity.phase || status.agent || "idle",
    label: activity.label || "",
    tool: activity.tool || "",
  };
  // Server activity wins over optimistic "Working…" once the house is actually busy,
  // and always wins for error flashes.
  if (
    state.localActivity &&
    (state.serverActivity.phase === "error" ||
      (state.localActivity.phase !== "error" &&
        state.serverActivity.phase !== "idle" &&
        state.serverActivity.phase !== "listening"))
  ) {
    clearLocalActivity();
  }
  const pillLabel = state.serverActivity.label || status.agent || "idle";
  $("agent-pill").textContent =
    state.serverActivity.phase === "idle" || state.serverActivity.phase === "listening"
      ? status.agent || "idle"
      : pillLabel.replace(/…$/, "");
  renderActivity(state.serverActivity);
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
  const confirmBtn = $("confirm-btn");
  confirmBtn.classList.toggle("hidden", !status.pending);
  document.querySelector(".composer-dock")?.classList.toggle("has-confirm", Boolean(status.pending));
  if (status.pending) {
    if (status.pending.reason === "awaiting_client") {
      confirmBtn.textContent = "Try again — Plex is open";
    } else {
      confirmBtn.textContent = `Confirm ${status.pending.tool}`;
    }
  }
  if (Array.isArray(status.widgets)) {
    renderWidgets(status.widgets);
  }
  if (!state.call) {
    $("hint").textContent = idleHint();
    $("orb-label").textContent = "Tap to talk";
  }
}

function displayRole(role) {
  const raw = String(role || "").toLowerCase();
  if (raw === "user" || raw === "you") return "you";
  if (raw === "assistant" || raw === "hearth") return "hearth";
  if (raw === "system") return "system";
  return raw || "system";
}

function appendLog(role, text) {
  if (!text) return;
  const log = $("log");
  const li = document.createElement("li");
  li.dataset.role = displayRole(role);
  li.innerHTML = `<span class="who">${displayRole(role)}</span>${text}`;
  log.appendChild(li);
  log.scrollTop = log.scrollHeight;
  setEmpty("transcript", false);
}

async function invoke(tool, args) {
  const out = await api("/api/invoke", {
    method: "POST",
    body: JSON.stringify({ tool, args }),
  });
  applyWidgetPayload(out);
  return out;
}

async function talk(message, confirm = false) {
  flashLocalActivity("thinking", "Working…", 60000);
  const wasLive = Boolean(state.call);
  if (!wasLive) setRefreshInterval(900);
  try {
    const out = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, confirm }),
    });
    appendLog("hearth", out.reply);
    applyWidgetPayload(out);
    if (out.reply) noteOverlayConversation(out.reply);
    clearLocalActivity();
    renderActivity(state.serverActivity);
    return out;
  } catch (err) {
    flashLocalActivity("error", "Request failed", 4000);
    throw err;
  } finally {
    if (!state.call) setRefreshInterval(8000);
  }
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
      appendLog(displayRole(line.role), line.text);
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
  noteOverlayConversation(text);
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
    flashLocalActivity("thinking", "Working…", 12000);
    sendRealtime({ type: "response.create" });
    return;
  }
  try {
    await talk(text);
  } catch (err) {
    appendLog("system", err.message || "Request failed");
  }
  refresh();
});

$("confirm-btn").addEventListener("click", async () => {
  try {
    await talk("confirm", true);
  } catch (err) {
    appendLog("system", err.message || "Request failed");
  }
  refresh();
});

function ensureSpokenAnswer() {
  if (state.spokenAnswer) return state.spokenAnswer;
  try {
    if (globalThis.HearthSpokenAnswer?.createFromDocument) {
      state.spokenAnswer = globalThis.HearthSpokenAnswer.createFromDocument(document);
    }
  } catch (_) {
    state.spokenAnswer = null;
  }
  return state.spokenAnswer;
}

function noteSpokenAnswer(type, event) {
  try {
    const panel = ensureSpokenAnswer();
    panel?.onRealtimeEvent?.(type, event);
  } catch (_) {
    /* overlay must never break the voice path */
  }
}

function dismissSpokenAnswer(opts) {
  try {
    const panel = ensureSpokenAnswer();
    if (!panel) return;
    if (opts && opts.callEnded) panel.onCallEnded();
    else panel.dismiss?.(opts || { immediate: true });
  } catch (_) {
    /* ignore */
  }
}

function onRealtimeEvent(event) {
  const type = event.type;
  if (state.call?.bargeIn) {
    state.call.bargeIn.noteRealtimeEvent(type);
  }
  // Spoken read-along — same DC transcript events; fail-soft and independent of audio.
  noteSpokenAnswer(type, event);
  if (
    type === "response.output_audio_transcript.delta" ||
    type === "response.audio_transcript.delta"
  ) {
    const delta = event.delta || "";
    if (delta) {
      state.liveAssistantTranscript = `${state.liveAssistantTranscript || ""}${delta}`;
      noteOverlayConversation(state.liveAssistantTranscript);
    }
  }
  if (type === "response.output_audio_transcript.done" || type === "response.audio_transcript.done") {
    const text = event.transcript || state.liveAssistantTranscript || "";
    state.liveAssistantTranscript = "";
    appendLog("hearth", text);
    noteOverlayConversation(text);
  }
  if (type === "response.created") {
    state.liveAssistantTranscript = "";
  }
  // User speech — requires session audio.input.transcription (see webrtc.session_config).
  // User mic transcript is NOT shown on the spoken-answer panel (assistant only).
  if (
    type === "conversation.item.input_audio_transcription.completed" ||
    type === "conversation.item.audio_transcription.completed"
  ) {
    appendLog("you", event.transcript);
    noteOverlayConversation(event.transcript || "");
  }
  if (type === "error") {
    const message = event.error?.message || event.message || "realtime error";
    appendLog("system", message);
    flashLocalActivity("error", "Voice error", 4000);
  }
  if (type === "response.function_call_arguments.done" && event.name === "end_call") {
    if (state.call) state.call.pendingHangup = true;
  }
  if (type === "response.done" && state.call?.pendingHangup) {
    stopConversation();
    return;
  }
  // Sideband runs house tools on the server; still refresh overlays promptly so
  // media / weather panels appear during the live call (not only on the 8s poll).
  if (state.call?.sidebandOk) {
    if (
      type === "response.function_call_arguments.done" ||
      type === "response.done" ||
      type === "response.output_audio_transcript.done" ||
      type === "response.audio_transcript.done"
    ) {
      refresh();
    }
    return;
  }
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
  const endCall = event.name === "end_call";
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
    if (endCall) {
      if (state.call) state.call.pendingHangup = true;
      // Hang up when this response finishes (response.done); do not start another turn.
      return;
    }
    sendRealtime({ type: "response.create" });
    applyWidgetPayload(out);
    refresh();
  } catch (err) {
    appendLog("system", `Tool failed: ${err.message}`);
    flashLocalActivity("error", "Tool failed", 4000);
  }
}

function abandonCallSetup(pc) {
  try {
    if (pc) {
      for (const sender of pc.getSenders()) {
        try {
          pc.removeTrack(sender);
        } catch (_) {
          /* ignore */
        }
      }
      pc.close();
    }
  } catch (_) {
    /* ignore */
  }
  /* Keep the warm mic stream; only mute it. Stopping would re-prompt on iOS PWA. */
  releaseMicStream({ hard: false });
}

async function startConversation() {
  const remote = $("remote-audio");
  const pc = new RTCPeerConnection();
  let stream;
  try {
    stream = await acquireMicStream();
  } catch (err) {
    pc.close();
    throw err;
  }
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
    abandonCallSetup(pc);
    throw new Error(err.error || err.message || `realtime/calls ${sdpResponse.status}`);
  }
  if (path && path !== "webrtc-ga") {
    abandonCallSetup(pc);
    throw new Error(`unexpected realtime path ${path}`);
  }
  if (beta === "true") {
    abandonCallSetup(pc);
    throw new Error("beta realtime path is disabled");
  }
  const answer = await sdpResponse.text();
  await pc.setRemoteDescription({ type: "answer", sdp: answer });
  const callId = sdpResponse.headers.get("X-Hearth-Call-Id") || "";
  const sideband = sdpResponse.headers.get("X-Hearth-Sideband") || "";
  hideMicPanels();
  const micTrack = stream.getAudioTracks()[0] || null;
  let bargeIn = null;
  if (micTrack && globalThis.HearthVad?.SpeechBargeIn) {
    bargeIn = new HearthVad.SpeechBargeIn(micTrack, stream);
    await bargeIn.start();
  }
  state.call = {
    pc,
    dc,
    stream,
    callId,
    sidebandOk: sideband === "ok" || sideband === "starting",
    bargeIn,
    pendingHangup: false,
  };
  pc.addEventListener("connectionstatechange", () => {
    if (!state.call || state.call.pc !== pc) return;
    if (pc.connectionState === "failed" || pc.connectionState === "closed") {
      stopConversation();
    }
  });
  $("orb").classList.add("live", "hot");
  $("orb").setAttribute("aria-label", "End conversation");
  $("orb-label").textContent = "Listening";
  $("hint").textContent = phoneUi()
    ? "Listening. Speak to interrupt — background noise is ignored. Tap to hang up."
    : "Live WebRTC conversation. Real speech interrupts; TV/HVAC noise should not. Tap to hang up.";
  $("voice-pill").textContent = "voice webrtc-ga";
  $("voice-pill").classList.add("live");
  setRefreshInterval(1200);
}

async function stopConversation() {
  const call = state.call;
  state.call = null;
  setRefreshInterval(8000);
  // Conversation panel rule: voice session end → spoken read-along must go.
  dismissSpokenAnswer({ callEnded: true });
  $("orb").classList.remove("live", "hot");
  $("orb").setAttribute("aria-label", "Tap to talk");
  $("orb-label").textContent = "Tap to talk";
  $("hint").textContent = idleHint();
  $("voice-pill").classList.remove("live");
  if (!call) return;
  try {
    call.bargeIn && call.bargeIn.stop();
    /* Detach from PC first so close() does not end the local MediaStreamTrack. */
    if (call.pc) {
      for (const sender of call.pc.getSenders()) {
        try {
          call.pc.removeTrack(sender);
        } catch (_) {
          /* ignore */
        }
      }
    }
    /* Mute; do not track.stop() — that forces a fresh getUserMedia prompt on iOS PWAs. */
    if (call.stream) {
      for (const track of call.stream.getAudioTracks()) track.enabled = false;
    }
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

async function beginVoiceFromUserGesture() {
  hideMicPanels();
  $("orb").classList.add("hot");
  $("orb-label").textContent = "Connecting";
  $("hint").textContent = phoneUi() ? "Connecting…" : "Opening GA WebRTC session…";
  try {
    await startConversation();
  } catch (err) {
    $("orb").classList.remove("hot", "live");
    $("orb-label").textContent = "Tap to talk";
    const classified = classifyMicError(err);
    if (classified.kind === "denied") {
      showMicDenied(classified.message);
      appendLog("system", "Microphone permission denied.");
    } else {
      $("hint").textContent = idleHint();
      appendLog("system", classified.message);
    }
  }
}

async function handleOrbTap() {
  if (state.call) {
    await stopConversation();
    return;
  }
  if (!$("mic-gate")?.classList.contains("hidden")) {
    /* Second tap while gate is open = continue (same as the Continue button). */
    await beginVoiceFromUserGesture();
    return;
  }
  const permission = await queryMicPermission();
  state.micPermission = permission;
  if (permission === "granted" || permission === "prompt") {
    micStorageClear(MIC_STORAGE.denied);
  }
  if (permission === "denied" || (permission === "unknown" && micStorageGet(MIC_STORAGE.denied) === "1")) {
    showMicDenied();
    return;
  }
  if (shouldShowMicGate(permission)) {
    showMicGate();
    $("orb-label").textContent = "Allow mic";
    $("hint").textContent = phoneUi() ? "Mic needed once." : "Microphone access is required for live voice.";
    return;
  }
  await beginVoiceFromUserGesture();
}

$("orb").addEventListener("click", () => {
  handleOrbTap();
});

$("mic-gate-continue")?.addEventListener("click", async () => {
  await beginVoiceFromUserGesture();
});

$("mic-gate-not-now")?.addEventListener("click", () => {
  hideMicPanels();
  micStorageSet(MIC_STORAGE.gateAt, String(Date.now()));
  $("orb").classList.remove("hot", "live");
  $("orb-label").textContent = "Tap to talk";
  $("hint").textContent = idleHint();
});

$("mic-denied-dismiss")?.addEventListener("click", () => {
  hideMicPanels();
  $("orb").classList.remove("hot", "live");
  $("orb-label").textContent = "Tap to talk";
  $("hint").textContent = idleHint();
});

$("mic-denied-retry")?.addEventListener("click", async () => {
  micStorageClear(MIC_STORAGE.denied);
  state.micPermission = "unknown";
  await beginVoiceFromUserGesture();
});

$("logout-btn").addEventListener("click", async () => {
  try {
    await fetch("/auth/session/logout", { method: "POST" });
  } catch (_) {
    /* still leave */
  }
  bounceToLogin();
});

function onPageHide() {
  /* Document is going away — release hardware. A warm mute is useless across navigations. */
  if (state.call) {
    try {
      state.call.pc && state.call.pc.close();
    } catch (_) {
      /* ignore */
    }
    state.call = null;
  }
  releaseMicStream({ hard: true });
}

window.addEventListener("pagehide", onPageHide);

function setRefreshInterval(ms) {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
  state.refreshTimer = setInterval(refresh, ms);
}

async function boot() {
  if (window.HearthSettings) {
    window.HearthSettings.mount();
    window.HearthSettings.setSpendFetcher(() => api("/api/openai/spend?days=30"));
  }
  bindInfoOverlay();
  const ok = await refreshAccessToken();
  if (!ok) {
    bounceToLogin();
    return;
  }
  /* Permissions API only — never probe with getUserMedia on boot. */
  state.micPermission = await queryMicPermission();
  if (state.micPermission === "granted") {
    micStorageSet(MIC_STORAGE.granted, "1");
    micStorageClear(MIC_STORAGE.denied);
  }
  refresh();
  setRefreshInterval(8000);
}

boot();
