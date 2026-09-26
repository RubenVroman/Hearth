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
  infoReadingKey: "",
  infoEnterTimer: null,
  infoCloseTimer: null,
  /** Soft-hidden by context/idle — widget stays; can reappear without refetch. */
  infoSoftHidden: false,
  /** User focused the glass — keep visible until idle or hard dismiss. */
  infoPinned: false,
  infoHideTimer: null,
  infoIdleTimer: null,
  /**
   * User-chosen board card. Survives /api/status polls + transcript sync so
   * browsing does not jump back to "page 1" while the assistant narrates.
   */
  clientMediaFocusId: null,
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
  ambientReader: null,
  /** Latest mic transcript (final preferred, partial while STT is still streaming). */
  userUtterance: { final: "", partial: "" },
};

/** Epoch + one-reconnect budget for the live WebRTC call. Policy lives in voice-session.js. */
const voiceLife = new HearthVoiceSession.VoiceLifecycle();

/** Client grace before fading when talk is clearly unrelated (ms). */
const OVERLAY_IRRELEVANT_GRACE_MS = 650;
/** Max media cards on the reading board (genre browse + search). */
const MEDIA_STACK_CAP = 12;

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
  /* Browser will show its own permission dialog — do not stack an in-app gate on top. */
  if (permission === "prompt") return false;
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
  return kind === "weather" || kind === "media" || kind === "downloads" || kind === "information";
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
  // Keep server order. Active title is indicated via active_id + board slots —
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
  // Content, not poll timestamps or spoken focus, decides when the board remounts.
  const data = { ...(widget.data || {}) };
  if (widget.kind === "media") {
    data.items = mediaItemsOf(widget);
    delete data.active_id;
    delete data.item;
  }
  return JSON.stringify([widget.id, widget.kind, widget.status, data,
    widget.kind === "media" && data.items.length ? "" : widget.title,
    widget.status === "error" || widget.status === "info" || widget.kind !== "media" ? widget.body : "",
    widget.kind !== "media" || widget.status === "error" ? widget.detail : ""]);
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
  // Results remain readable until a new topic replaces them or the user dismisses.
  // An arbitrary timeout used to erase long lists before they could be read.
  if (state.infoIdleTimer) clearTimeout(state.infoIdleTimer);
  state.infoIdleTimer = null;
}

function ensureAmbientReader() {
  if (!state.ambientReader && globalThis.HearthPresentation?.AmbientReader) {
    state.ambientReader = new globalThis.HearthPresentation.AmbientReader({
      viewport: $("info-glass-inner"), content: $("info-content"),
      button: $("info-reading-toggle"), status: $("info-reading-status"),
    });
  }
  return state.ambientReader;
}

function presentationKey(widget) {
  return JSON.stringify([widget.id, widget.kind, widget.kind === "media"
    ? [mediaItemsOf(widget).map(mediaItemKey), widget.data?.genre, widget.data?.listed_genres]
    : widget.data?.query || widget.title]);
}

function presentationMotionIsStill() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches || document.documentElement.dataset.motion === "still";
}

function setPresentationVisible(visible) {
  document.body.classList.toggle("has-presentation", visible);
  const stage = document.querySelector(".stage");
  if (stage) stage.inert = visible;
  const root = $("info-overlay");
  if (root) root.inert = !visible;
}

function clearInfoEntrance() {
  if (state.infoEnterTimer != null) clearTimeout(state.infoEnterTimer);
  state.infoEnterTimer = null;
  $("info-content")?.classList.remove("is-entering");
}

function animateInfoContent(content) {
  clearInfoEntrance();
  // Narration can change the active card without re-running any entrance animation.
  content.dataset.settled = "1";
  if (presentationMotionIsStill()) return;
  content.querySelectorAll(".info-media-card, .info-fact, .info-download-row").forEach((card, index) => {
    card.style.setProperty("--reveal-index", String(Math.min(index, 5)));
  });
  void content.offsetWidth;
  content.classList.add("is-entering");
  state.infoEnterTimer = setTimeout(() => {
    content.classList.remove("is-entering");
    state.infoEnterTimer = null;
  }, 700);
}

function focusPresentation(widget) {
  const activeId = String(widget.context?.active_id || widget.data?.active_id || "");
  $("info-content")?.querySelectorAll(".info-media-card").forEach((card) => {
    const active = card.dataset.mediaId === activeId;
    card.classList.toggle("is-active", active);
    card.classList.toggle("is-front", active);
    if (active) card.setAttribute("aria-current", "true");
    else card.removeAttribute("aria-current");
  });
}

function softHideInfoOverlay() {
  state.ambientReader?.stop();
  clearInfoEntrance();
  setPresentationVisible(false);
  const root = $("info-overlay");
  if (!root || root.hidden) return;
  if (state.infoSoftHidden) return;
  clearOverlayPolicyTimers();
  clearInfoCloseTimer();
  state.infoSoftHidden = true;
  state.infoPinned = false;
  if (presentationMotionIsStill()) {
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
 * Remember a user-chosen board card across status polls / transcript sync.
 */
function rememberClientMediaFocus(mediaId) {
  state.clientMediaFocusId = mediaId != null && mediaId !== "" ? String(mediaId) : null;
}

/**
 * Re-apply the user's board selection onto a fresh server widget payload.
 * Returns true when focus was restored.
 */
function reconcileClientMediaFocus(visual) {
  if (!visual || visual.kind !== "media") return false;
  const focusId = state.clientMediaFocusId;
  if (!focusId) return false;
  const items = mediaItemsOf(visual);
  const hit = items.find((row) => String(row.id || mediaItemKey(row)) === String(focusId));
  if (!hit) {
    state.clientMediaFocusId = null;
    return false;
  }
  const data = visual.data || {};
  data.active_id = hit.id;
  data.item = hit;
  data.items = items;
  visual.data = data;
  visual.title = hit.title || visual.title;
  const year = hit.year;
  const type = hit.type || "movie";
  visual.body = [type, year].filter(Boolean).join(" · ");
  if (!visual.context) visual.context = {};
  visual.context.active_id = hit.id;
  visual.context.relevant = true;
  return true;
}

/**
 * Focus a stacked media card by id (tap / keyboard / flick on recessed cards).
 */
function focusMediaById(mediaId, { reveal = true, fromUser = true } = {}) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual || visual.kind !== "media" || !mediaId) return false;
  const items = mediaItemsOf(visual);
  const hit = items.find((row) => String(row.id || mediaItemKey(row)) === String(mediaId));
  if (!hit) return false;
  const data = visual.data || {};
  const prev = String((visual.context && visual.context.active_id) || data.active_id || "");
  if (fromUser) rememberClientMediaFocus(hit.id);
  if (prev === String(hit.id) && !state.infoSoftHidden) {
    if (reveal) scheduleOverlayIdleHide();
    return false;
  }
  data.active_id = hit.id;
  data.item = hit;
  // Preserve list order so the board can slide without remount thrash.
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

async function playActiveInInfuse(mediaId) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual || visual.kind !== "media") {
    appendLog("system", "Nothing to open in Infuse.");
    return;
  }
  const item = activeMediaItem(visual, mediaId);
  if (!item || !item.title) {
    appendLog("system", "Nothing to open in Infuse.");
    showLocalMediaOverlay({
      title: visual.title || "Play",
      status: "error",
      body: "No playable title is selected.",
      detail: "Pick a card, then try Open in Infuse again.",
      data: { ...(visual.data || {}), pick: Boolean((visual.data || {}).pick) },
    });
    return;
  }
  // Keep the focused card in sync before the round-trip remounts the overlay.
  if (item.id) focusMediaById(item.id, { reveal: true });
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
    // If the tool returned no visual (miss / misbind), show a clear non-empty error glass.
    const visualAfter = pickVisualOverlay(state.widgets);
    if (!visualAfter || visualAfter.kind !== "media") {
      const localItem = { ...item, pending: true, id: item.id || mediaItemKey(item) };
      showLocalMediaOverlay({
        title: item.title,
        status: "error",
        body: (data && (data.error || data.speak)) || "Could not open that title in Infuse.",
        detail: "Try another title, or say play again.",
        data: {
          item: localItem,
          items: [localItem],
          active_id: localItem.id,
          presentation: "board",
          tool: "infuse_play",
        },
      });
    } else {
      refresh();
    }
  } catch (err) {
    appendLog("system", `Infuse play failed: ${err.message}`);
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Open in Infuse";
    }
    showLocalMediaOverlay({
      title: item.title || visual.title || "Play",
      status: "error",
      body: `Infuse play failed: ${err.message}`,
      detail: "Check Apple TV / Infuse, then try again.",
      data: {
        ...(visual.data || {}),
        item: { ...item, id: item.id || mediaItemKey(item) },
        active_id: item.id || mediaItemKey(item),
      },
    });
  }
}

/**
 * Client-only media glass when a play round-trip leaves no server widget.
 * Stays until dismiss / next real media payload — never a blank popup.
 */
function showLocalMediaOverlay({ title, status, body, detail, data }) {
  const items = Array.isArray(data && data.items) ? data.items : [];
  const item = (data && data.item) || items[0] || { title: title || "Play" };
  const widget = {
    id: "media",
    kind: "media",
    title: title || item.title || "Play",
    status: status || "error",
    body: body || "",
    detail: detail || "",
    data: {
      presentation: "board",
      tool: (data && data.tool) || "infuse_play",
      item,
      items: items.length ? items : item.title ? [item] : [],
      active_id: (data && data.active_id) || item.id || mediaItemKey(item),
      ...(data || {}),
    },
    context: { relevant: true, active_id: (data && data.active_id) || item.id || "" },
    dismissible: true,
    sticky: false,
    updated_at: new Date().toISOString(),
  };
  state.widgets = [...state.widgets.filter((w) => w.id !== "media"), widget];
  openInfoOverlay(widget);
}

/**
 * Resolve the focused / requested media card for Infuse play.
 * Prefer explicit mediaId → active_id → data.item → first stack card.
 */
function activeMediaItem(visual, mediaId) {
  if (!visual || visual.kind !== "media") return null;
  const items = mediaItemsOf(visual);
  const wanted = mediaId != null && String(mediaId) !== "" ? String(mediaId) : "";
  if (wanted) {
    const hit = items.find((row) => String(row.id || mediaItemKey(row)) === wanted);
    if (hit) return hit;
  }
  const activeId = String(
    (visual.context && visual.context.active_id) || (visual.data && visual.data.active_id) || ""
  );
  if (activeId) {
    const hit = items.find((row) => String(row.id || mediaItemKey(row)) === activeId);
    if (hit) return hit;
  }
  const dataItem = visual.data && visual.data.item;
  if (dataItem && dataItem.title) {
    return { ...dataItem, id: dataItem.id || mediaItemKey(dataItem) };
  }
  return items[0] || null;
}

async function browseGenreCategory(genre, { mediaType = "movie" } = {}) {
  const title = String(genre || "").trim();
  if (!title) return;
  // New genre stack — drop prior browse selection so page 1 of this category shows.
  state.clientMediaFocusId = null;
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
 * Does not steal a user-pinned browse selection.
 */
function focusMediaFromText(text, { reveal = true } = {}) {
  if (state.infoPinned && state.clientMediaFocusId) return false;
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
 *
 * @param {string} text
 * @param {{ live?: boolean }} [opts] live streaming deltas skip card-focus thrash
 */
function noteOverlayConversation(text, { live = false } = {}) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual) return;
  if (isAckUtterance(text)) {
    scheduleOverlayIdleHide();
    return;
  }
  // Streaming deltas only keep the panel policy warm — focusing mid-word remounts
  // the board and yanks scroll back to card 1 / top of the glass.
  if (!live) {
    const focused = focusMediaFromText(text, { reveal: true });
    if (focused) return;
  } else if (state.infoPinned && state.clientMediaFocusId) {
    scheduleOverlayIdleHide();
    return;
  }
  const related = textTouchesOverlay(text, visual);
  if (related) {
    if (state.infoHideTimer) {
      clearTimeout(state.infoHideTimer);
      state.infoHideTimer = null;
    }
    // Do not clear infoPinned — browsing must survive narration.
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
    state.clientMediaFocusId = null;
    closeInfoOverlay({ animate: true });
    return;
  }
  pruneLocalMediaExtras(visual);
  if (visual.kind === "media") {
    reconcileClientMediaFocus(visual);
  }
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
      const glass = $("info-glass-inner");
      const scrollTop = glass ? glass.scrollTop : 0;
      if (content) content.innerHTML = overlayInnerHtml(visual);
      state.infoSignature = overlaySignature(visual);
      if (glass) {
        glass.scrollTop = scrollTop;
      }
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

function mediaCardMarkup(item, { active = false, labelled = false, genre = "" } = {}) {
  const title = item.title || "Untitled";
  const type = ({ show: "Series", tv: "Series", movie: "Film", episode: "Episode", season: "Season" })[item.type] || "Film";
  const meta = [item.year, type, item.contentRating].filter(Boolean).map(escapeHtml).join(" · ");
  const art = mediaArtUrl({ ...item, title });
  const poster = art ? '<img class="info-poster" src="' + escapeHtml(art) + '" alt="" width="120" height="180" loading="eager" />' : mediaPosterFallback(title);
  const links = item.links && typeof item.links === "object" ? item.links : {};
  const linkMarkup = Object.entries({ TMDB: links.tmdb, IMDb: links.imdb }).map(([label, url]) => {
    const safe = globalThis.HearthPresentation.safeUrl(url);
    return safe ? '<a class="info-media-link" href="' + escapeHtml(safe) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>' : "";
  }).filter(Boolean).join(" · ");
  const genres = Array.isArray(item.genres) ? item.genres.slice(0, 3).map(escapeHtml).join(" · ") : "";
  const availability = typeof item.availability === "string" ? item.availability : item.status || "";
  const unresolved = item.status === "unresolved";
  const playbackLabels = { opening: "Opening", playing: "Now playing", paused: "Paused", stopped: "Stopped", buffering: "Buffering", ready: "Ready to play" };
  const label = unresolved ? "Metadata unavailable" : item.skeleton ? "Looking this up…" : item.pending ? "Ready to play" : item.player ? playbackLabels[item.state] || "Playback not confirmed" : item.source === "suggest" ? "Suggested" : availability || (item.source === "plex" ? "In your library" : "Catalog match");
  const id = escapeHtml(String(item.id || mediaItemKey(item)));
  const summary = item.summary || item.overview || "";
  const rating = item.rating != null ? '<span class="info-rating">★ ' + escapeHtml(item.rating) + '</span>' : "";
  return '<article class="info-media-card' + (active ? ' is-active is-front' : '') + (item.skeleton ? ' is-skeleton' : '') + '" data-media-id="' + id + '"' + (active ? ' aria-current="true"' : '') + '>' +
    '<div class="info-media">' + poster + '<div class="info-media-copy">' +
    '<p class="info-kicker">' + escapeHtml(label) + '</p>' +
    '<h3 class="info-title"' + (labelled ? ' id="info-title"' : '') + '>' + escapeHtml(title) + '</h3>' +
    '<p class="info-meta">' + meta + rating + '</p>' +
    (genres ? '<p class="info-media-genres">' + genres + '</p>' : '') +
    (item.reason ? '<p class="info-media-reason">' + escapeHtml(item.reason) + '</p>' : '') +
    (summary ? '<p class="info-detail">' + escapeHtml(summary) + '</p>' : '') +
    (item.skeleton && !unresolved ? '<p class="info-detail">Finding the details for you.</p>' : '') +
    (linkMarkup ? '<p class="info-media-links">' + linkMarkup + '</p>' : '') +
    (!item.skeleton && (item.title || item.ratingKey || item.tmdbId) ? '<div class="info-media-actions"><button type="button" class="info-infuse-btn" data-infuse-play="1" data-media-id="' + id + '">Open in Infuse</button></div>' : '') +
    '</div></div></article>';
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
      <p class="info-meta">${escapeHtml(widget.detail || `Name a genre to see ${kindLabel} in that category.`)}</p>
      ${chips || `<p class="info-detail">No genres found in the Plex library.</p>`}
      <p class="info-detail info-genre-hint">Categories come from Plex metadata (e.g. Science Fiction).</p>
    </div>
  `;
}

function mediaMarkup(widget) {
  const data = widget.data || {};
  if (data.presentation === "genres" || data.listed_genres) return mediaGenresMarkup(widget);
  const items = mediaItemsOf(widget);
  if (!items.length) return emptyMediaMarkup(widget);
  const activeId = widget.context?.active_id || data.active_id || items[0]?.id || "";
  const heading = data.heading || (items.length === 1 ? "A closer look" : data.genre ? data.genre + " for your evening" : "Your next great watch");
  const total = Number(data.total);
  const count = Number.isFinite(total) && total > items.length
    ? items.length + " of " + total + " titles"
    : items.length + (items.length === 1 ? " title" : " titles");
  return '<header class="info-board-heading"><p class="info-kicker">Curated by Hearth · ' + escapeHtml(count) + '</p>' +
    '<h2 class="info-title" id="info-title">' + escapeHtml(heading) + '</h2>' +
    '<p class="info-board-summary">' + escapeHtml(data.summary || "Just keep talking. Everything we find appears here.") + '</p></header>' +
    mediaStatusBanner(widget) +
    '<div class="info-media-stack info-media-board' + (items.length === 1 ? ' is-single' : '') + '" data-count="' + items.length + '">' +
    items.map((item) => mediaCardMarkup(item, { active: String(item.id) === String(activeId), genre: data.genre || "" })).join("") + '</div>';
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
    "No titles found yet. Try a different title, year, or genre.";
  return `<div class="info-media-empty" role="status">
    <p class="info-kicker">Your results</p>
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
  if (widget.kind === "information") return globalThis.HearthPresentation.informationMarkup(widget);
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
  const glass = $("info-glass-inner");
  if (!root || !content || !widget) return;
  clearInfoCloseTimer();
  if (widget.kind === "media") {
    reconcileClientMediaFocus(widget);
  }
  const signature = overlaySignature(widget);
  const readingKey = presentationKey(widget);
  const alreadyOpen =
    root.classList.contains("is-open") &&
    !root.classList.contains("is-closing") &&
    !root.classList.contains("is-soft-hidden") &&
    !state.infoSoftHidden;
  const contentEmpty = !String(content.innerHTML || "").trim();
  // Never keep a blank glass open — remount when the DOM was cleared or markup is empty.
  if (alreadyOpen && state.infoSignature === signature && !contentEmpty) {
    focusPresentation(widget);
    ensureAmbientReader()?.show(readingKey);
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
  // Reopening an unchanged board retains its DOM, focus, and reading position.
  const scrollTop = glass ? glass.scrollTop : 0;
  const replaced = contentEmpty || state.infoSignature !== signature;
  const newBoard = state.infoReadingKey !== readingKey;
  if (replaced) content.innerHTML = html;
  state.infoSignature = signature;
  state.infoReadingKey = readingKey;
  state.infoSoftHidden = false;
  root.hidden = false;
  root.setAttribute("aria-hidden", "false");
  root.classList.remove("is-closing", "is-soft-hidden");
  content.dataset.settled = "1";
  if (!alreadyOpen) {
    // Force style flush so enter transition runs when opening from hidden / soft-hidden.
    void root.offsetWidth;
  }
  root.classList.add("is-open");
  root.dataset.kind = widget.kind;
  setPresentationVisible(true);
  if (glass && scrollTop > 0) {
    glass.scrollTop = scrollTop;
  }
  focusPresentation(widget);
  if (replaced && (newBoard || contentEmpty)) animateInfoContent(content);
  else if (replaced) clearInfoEntrance();
  ensureAmbientReader()?.show(readingKey);
  scheduleOverlayIdleHide();
}

function closeInfoOverlay({ animate = true } = {}) {
  state.ambientReader?.stop();
  clearInfoEntrance();
  setPresentationVisible(false);
  const root = $("info-overlay");
  clearOverlayPolicyTimers();
  state.infoSoftHidden = false;
  state.infoPinned = false;
  state.clientMediaFocusId = null;
  if (!root || root.hidden) {
    state.infoSignature = "";
    state.infoReadingKey = "";
    return;
  }
  clearInfoCloseTimer();
  state.infoSignature = "";
  state.infoReadingKey = "";
  root.classList.remove("is-soft-hidden");
  const content = $("info-content");
  if (content) delete content.dataset.settled;
  if (!animate || presentationMotionIsStill()) {
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
  state.clientMediaFocusId = null;
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
      playActiveInInfuse(playBtn.getAttribute("data-media-id") || "");
      return;
    }
  });

  document.addEventListener("keydown", (ev) => {
    const root = $("info-overlay");
    if (!root || root.hidden) return;
    if (!root.classList.contains("is-open") && !root.classList.contains("is-soft-hidden")) return;
    if (ev.key === "Escape") {
      dismiss();
      return;
    }
  });
}

let housePulse = null;

function askHouse(text) {
  const input = $("line");
  if (!input || !text) return;
  input.value = text;
  $("composer")?.requestSubmit();
}

function showHouseFault(failed) {
  const el = $("house-fault");
  const copy = $("house-fault-copy");
  if (!el || !copy) return;
  const names = new Set(failed.map((row) => row.name));
  let text = "Part of the house didn’t answer. Retry when you’re ready.";
  if (names.has("pulse") && names.size === 1) {
    text = "The shelf didn’t answer. The rest of the house is still here.";
  } else if (names.has("playing") && !names.has("status")) {
    text = "Plex didn’t answer. Retry, or ask what’s on tonight in a moment.";
  } else if (names.has("rooms")) {
    text = "Home Assistant didn’t answer. The lights were left alone — retry to look again.";
  }
  copy.textContent = text;
  el.hidden = false;
}

function clearHouseFault() {
  const el = $("house-fault");
  if (el) el.hidden = true;
}

async function runPreset(preset, label) {
  const note = $("preset-note");
  if (note) {
    note.hidden = false;
    note.textContent = `Running ${label || preset}…`;
  }
  try {
    const out = await api("/api/house/scene", {
      method: "POST",
      body: JSON.stringify({ preset }),
    });
    if (note) {
      note.hidden = false;
      note.textContent = out.speak || (out.ok ? "Scene is on." : "That scene didn’t run.");
    }
  } catch (err) {
    if (note) {
      note.hidden = false;
      note.textContent =
        "Home Assistant didn’t run that scene. Try again — the lights were left alone.";
    }
  }
  refresh();
}

function renderHousePulse(pulse) {
  housePulse = pulse || null;
  const chips = $("house-chips");
  if (chips) {
    const plex = pulse.plex || {};
    const home = pulse.ha || {};
    const chip = (label, kind) =>
      `<span class="house-chip${kind ? ` ${kind}` : ""}">${escapeHtml(label)}</span>`;
    const bits = [];
    if (plex.error) bits.push(chip("Plex quiet", "is-down"));
    else if (plex.live) bits.push(chip("Plex live", "is-live"));
    else bits.push(chip(plex.mode === "mock" ? "Plex fixture" : "Plex", ""));
    if (home.ok === false || home.error) bits.push(chip("HA quiet", "is-down"));
    else if (home.live) bits.push(chip("HA live", "is-live"));
    else bits.push(chip("HA fixture", ""));
    if (pulse.active_preset) {
      bits.push(chip(String(pulse.active_preset).replaceAll("_", " "), "is-live"));
    }
    const halfway = (pulse.continue_watching || []).length;
    if (halfway) bits.push(chip(`${halfway} half-watched`, ""));
    chips.innerHTML = bits.join("");
  }

  const list = $("shelf-list");
  if (list) {
    list.innerHTML = "";
    const rows = [
      ...(pulse.continue_watching || []).slice(0, 3).map((item) => ({ item, kind: "continue" })),
      ...(pulse.recently_added || []).slice(0, 2).map((item) => ({ item, kind: "new" })),
    ];
    for (const row of rows) {
      const btn = document.createElement("button");
      btn.type = "button";
      const percent = row.item.progress_pct;
      const meta =
        row.kind === "continue" ? (percent ? `${percent}% in · tap to play` : "continue") : "new on Plex";
      btn.innerHTML = `${escapeHtml(row.item.label || row.item.title || "Untitled")}<span class="meta">${escapeHtml(meta)}</span>`;
      const title = row.item.show || row.item.title || row.item.label;
      btn.addEventListener("click", () => askHouse(`play ${title}`));
      list.appendChild(btn);
    }
    setEmpty("shelf-block", list.childElementCount === 0);
  }

  const presets = $("scene-presets");
  if (presets && Array.isArray(pulse.presets) && pulse.presets.length) {
    presets.innerHTML = "";
    for (const preset of pulse.presets) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "preset";
      btn.dataset.preset = preset.preset;
      btn.textContent = preset.label || preset.preset;
      presets.appendChild(btn);
    }
  }
  const shelfCount = $("shelf-list")?.childElementCount || 0;
  const playingEmpty = $("now-playing-block")?.classList.contains("is-empty");
  const mediaEmpty = ($("media-stack")?.childElementCount || 0) === 0;
  if (!playingEmpty || shelfCount) setEmpty("rail-media", false);
  else if (mediaEmpty) setEmpty("rail-media", true);
}

function renderNowPlaying(payload) {
  const root = $("now-playing");
  const session = (payload.sessions || [])[0];
  const last = housePulse && housePulse.last_played;
  const lastLine = last
    ? `<p class="meta">Last finished · ${escapeHtml(last.label || last.title || "")}</p>`
    : "";
  if (!session) {
    if (last) {
      root.innerHTML = `
        <p class="kicker" style="margin:0 0 8px">quiet wire</p>
        <h2>${escapeHtml(last.label || last.title || "Last play")}</h2>
        <p class="meta">Last finished${last.year ? ` · ${escapeHtml(last.year)}` : ""}</p>
      `;
      setEmpty("now-playing-block", false);
      setEmpty("rail-media", false);
      return;
    }
    root.innerHTML = `<p class="muted">Nothing on the wire.</p>`;
    setEmpty("now-playing-block", true);
    return;
  }
  const pct = session.duration_ms
    ? Math.min(100, Math.round((session.progress_ms / session.duration_ms) * 100))
    : 0;
  const show = session.show ? `${escapeHtml(session.show)} · ` : "";
  root.innerHTML = `
    <p class="kicker" style="margin:0 0 8px">${escapeHtml(payload.mode || "plex")}</p>
    <h2>${show}${escapeHtml(session.title || "Untitled")}</h2>
    <p class="meta">${escapeHtml(session.player || "player")} · ${escapeHtml(session.state || "idle")} · ${fmtMs(session.remaining_ms)} left</p>
    ${lastLine}
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
  syncRoomsRail();
  setEmpty("media-block", media.childElementCount === 0);
  setEmpty(
    "rail-media",
    $("now-playing-block")?.classList.contains("is-empty") &&
      media.childElementCount === 0 &&
      ($("shelf-list")?.childElementCount || 0) === 0
  );
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
  syncRoomsRail();
}

function comfortHasChips() {
  return ($("comfort-chips")?.childElementCount || 0) > 0;
}

function syncRoomsRail() {
  const roomsEmpty =
    ($("lights")?.childElementCount || 0) === 0 &&
    ($("scenes")?.childElementCount || 0) === 0 &&
    ($("scene-presets")?.childElementCount || 0) === 0 &&
    ($("memory-list")?.childElementCount || 0) === 0 &&
    !comfortHasChips();
  setEmpty("rail-rooms", roomsEmpty);
}

function renderComfort(payload) {
  const root = $("comfort-chips");
  if (!root) return;
  root.innerHTML = "";
  const data = payload || {};

  for (const ritual of data.rituals || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "comfort-chip";
    btn.textContent = ritual.label || ritual.id;
    btn.addEventListener("click", () =>
      invoke("house_ritual", { ritual: ritual.id, confirm: true }).then(refresh)
    );
    root.appendChild(btn);
  }

  for (const climate of data.climate || []) {
    const chip = document.createElement("div");
    chip.className = "comfort-chip";
    const read = document.createElement("span");
    read.className = "comfort-read";
    const current = climate.current != null ? climate.current : "–";
    const target = climate.target != null ? `→${climate.target}` : "";
    const unit = climate.unit || "°C";
    read.textContent = `${climate.name || "Climate"} ${current}${target}${unit} · ${climate.state || ""}`;
    const nudges = document.createElement("span");
    nudges.className = "comfort-nudge";
    for (const [label, action] of [
      ["−", "cooler"],
      ["+", "warmer"],
    ]) {
      const nudge = document.createElement("button");
      nudge.type = "button";
      nudge.textContent = label;
      nudge.setAttribute("aria-label", action === "warmer" ? "Warmer" : "Cooler");
      nudge.addEventListener("click", () =>
        invoke("house_climate", {
          action,
          entity: climate.entity_id,
          confirm: true,
        }).then(refresh)
      );
      nudges.appendChild(nudge);
    }
    chip.appendChild(read);
    chip.appendChild(nudges);
    root.appendChild(chip);
  }

  for (const air of data.air || []) {
    const chip = document.createElement("span");
    const tone = air.tone && air.tone !== "info" ? ` tone-${air.tone}` : "";
    chip.className = `comfort-chip${tone}`;
    const unit = air.unit ? ` ${air.unit}` : "";
    chip.textContent = `${air.label || air.name} ${air.state}${unit}`;
    root.appendChild(chip);
  }

  for (const unit of data.purifiers || []) {
    const on = String(unit.state || "").toLowerCase() === "on";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "comfort-chip";
    btn.textContent = `${unit.name || "Purifier"} · ${on ? "on" : "off"}`;
    btn.addEventListener("click", () =>
      invoke("house_purifier", {
        action: on ? "off" : "on",
        entity: unit.entity_id,
        confirm: true,
      }).then(refresh)
    );
    root.appendChild(btn);
  }

  for (const feeder of data.feeders || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "comfort-chip";
    btn.textContent = `Feed ${feeder.name || "feeder"}`;
    btn.addEventListener("click", () =>
      invoke("house_feeder", { action: "feed", entity: feeder.entity_id, confirm: true }).then(refresh)
    );
    root.appendChild(btn);
  }

  setEmpty("comfort-block", root.childElementCount === 0);
  syncRoomsRail();
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
  if (
    HearthVoiceSession.shouldRecoverFromServer({
      hasCall: Boolean(state.call),
      voiceMode: voice.mode,
      sidebandOk: Boolean(state.call && state.call.sidebandOk),
      phase: voiceLife.phase,
      userEnded: voiceLife.userEnded || Boolean(state.call?.pendingHangup),
    })
  ) {
    void recoverConversation("sideband_disconnected");
  }
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
  // A reconnect briefly clears state.call. Don't paint "Tap to talk" over it.
  if (
    !state.call &&
    voiceLife.phase !== "connecting" &&
    voiceLife.phase !== "recovering" &&
    voiceLife.phase !== "ending"
  ) {
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
  li.innerHTML = `<span class="who">${escapeHtml(displayRole(role))}</span>${escapeHtml(text)}`;
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
    flashLocalActivity("error", "The house didn’t answer", 4000);
    throw err;
  } finally {
    if (!state.call) setRefreshInterval(8000);
  }
}

async function refresh() {
  const jobs = [
    ["status", () => api("/api/status")],
    ["playing", () => api("/api/now-playing")],
    ["rooms", () => api("/api/rooms")],
    ["transcript", () => api("/api/transcript")],
    ["memory", () => api("/api/memory")],
    ["pulse", () => api("/api/house/pulse")],
    ["comfort", () => api("/api/comfort")],
  ];
  const settled = await Promise.all(
    jobs.map(async ([name, run]) => {
      try {
        return { name, ok: true, value: await run() };
      } catch (error) {
        return { name, ok: false, error };
      }
    })
  );
  const byName = Object.fromEntries(settled.map((row) => [row.name, row]));
  if (byName.pulse?.ok) renderHousePulse(byName.pulse.value);
  if (byName.status?.ok) renderStatus(byName.status.value);
  if (byName.playing?.ok) renderNowPlaying(byName.playing.value);
  if (byName.rooms?.ok) renderRooms(byName.rooms.value);
  if (byName.memory?.ok) renderMemory(byName.memory.value);
  if (byName.comfort?.ok) renderComfort(byName.comfort.value);
  if (byName.transcript?.ok && $("log").childElementCount === 0) {
    for (const line of byName.transcript.value.lines || []) {
      if (line.kind === "delta") continue;
      appendLog(displayRole(line.role), line.text);
    }
  }
  setEmpty("transcript", $("log").childElementCount === 0);
  const failed = settled.filter((row) => !row.ok);
  if (failed.length) showHouseFault(failed);
  else clearHouseFault();
}

function sendRealtime(event) {
  const dc = state.call && state.call.dc;
  if (dc && dc.readyState === "open") {
    dc.send(JSON.stringify(event));
    return true;
  }
  return false;
}

$("scene-presets")?.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-preset]");
  if (!btn) return;
  runPreset(btn.dataset.preset, btn.textContent.trim());
});

$("house-fault-retry")?.addEventListener("click", () => {
  refresh();
});

$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("line");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendLog("you", text);
  noteOverlayConversation(text);
  if (state.call) {
    // Invalidate any older tool batch before its next awaited action resumes.
    state.call.responseGeneration += 1;
    state.call.said = text;
    state.call.inputItemId = "typed-message";
    state.userUtterance = { final: text, partial: "" };
  }
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
    appendLog("system", "The reply didn’t arrive. Check the current status before trying again.");
  }
  refresh();
});

$("confirm-btn").addEventListener("click", async () => {
  try {
    await talk("confirm", true);
  } catch (err) {
    appendLog("system", "The confirmation response didn’t arrive. Check the current status before trying again.");
  }
  refresh();
});

function finishCallAfterAudio(call) {
  if (!call || state.call !== call || call.pendingHangup) return;
  call.pendingHangup = true;
  // A completed tool response may precede the last audio packet. Give playback
  // a moment to start; normally output_audio_buffer.stopped ends the call.
  call.endAudioGrace = setTimeout(() => {
    if (state.call === call && call.pendingHangup && !call.audioPlaying) stopConversation();
  }, 350);
  call.endAudioFallback = setTimeout(() => {
    if (state.call === call && call.pendingHangup) stopConversation();
  }, 30000);
}

function isEndCallTool(item) {
  if (!item || item.type !== "function_call" || item.name !== "end_call" || !item.call_id) return false;
  try {
    const args = JSON.parse(item.arguments || "{}");
    return args !== null && typeof args === "object" && !Array.isArray(args);
  } catch (_) { return false; }
}

function onRealtimeEvent(event) {
  const type = event.type;
  if (state.call && type === "output_audio_buffer.started") state.call.audioPlaying = true;
  if (state.call && type === "output_audio_buffer.stopped") {
    state.call.audioPlaying = false;
    if (state.call.pendingHangup) { stopConversation(); return; }
  }
  if (state.call?.bargeIn) {
    state.call.bargeIn.noteRealtimeEvent(type);
  }
  if (
    type === "response.output_audio_transcript.delta" ||
    type === "response.audio_transcript.delta"
  ) {
    const delta = event.delta || "";
    if (delta) {
      state.liveAssistantTranscript = `${state.liveAssistantTranscript || ""}${delta}`.slice(-12000);
      noteOverlayConversation(state.liveAssistantTranscript, { live: true });
    }
  }
  if (type === "response.output_audio_transcript.done" || type === "response.audio_transcript.done") {
    const text = event.transcript || state.liveAssistantTranscript || "";
    state.liveAssistantTranscript = "";
    appendLog("hearth", text);
    noteOverlayConversation(text, { live: false });
  }
  if (type === "response.created") {
    state.liveAssistantTranscript = "";
  }
  if (state.call && (type === "response.created" || type === "input_audio_buffer.speech_started")) {
    state.call.responseGeneration += 1;
    if (type === "response.created") {
      state.call.responseId = event.response?.id || "";
      state.call.responseStartedGeneration = state.call.responseGeneration;
    }
  }
  if (state.call && type === "input_audio_buffer.speech_started") {
    state.call.said = "";
    state.userUtterance = { final: "", partial: "" };
    state.call.inputItemId = event.item_id || "";
  }
  // User speech — requires session audio.input.transcription (see webrtc.session_config).
  // Only a final transcript from this input may authorize a tool call.
  if (
    type === "conversation.item.input_audio_transcription.delta" ||
    type === "conversation.item.audio_transcription.delta"
  ) {
    if (state.call && (!state.call.inputItemId || state.call.inputItemId === event.item_id)) {
      state.userUtterance = HearthVoiceSession.mergeUserUtterance(state.userUtterance, event, true);
    }
  }
  if (
    type === "conversation.item.input_audio_transcription.completed" ||
    type === "conversation.item.audio_transcription.completed"
  ) {
    if (state.call && (!state.call.inputItemId || state.call.inputItemId === event.item_id)) {
      state.userUtterance = HearthVoiceSession.mergeUserUtterance(state.userUtterance, event, false);
      state.call.said = String(event.transcript || event.text || "").trim()
        ? HearthVoiceSession.utteranceText(state.userUtterance) : "";
    }
    appendLog("you", event.transcript);
    noteOverlayConversation(event.transcript || "");
  }
  if (type === "error") {
    const message = event.error?.message || event.message || "realtime error";
    appendLog("system", message);
    flashLocalActivity("error", "Voice error", 4000);
  }
  // Sideband runs house tools on the server; still refresh overlays promptly so
  // media / weather panels appear during the live call (not only on the 8s poll).
  if (state.call?.sidebandOk) {
    if (type === "response.done" && event.response?.status === "completed" &&
        Array.isArray(event.response.output) && event.response.output.some(isEndCallTool)) {
      finishCallAfterAudio(state.call);
    }
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
  // Completed response output is authoritative. Partial/cancelled tool arguments
  // must never execute, and a multi-tool response gets just one continuation.
  if (type === "response.done") relayCompletedTools(event);
}

async function relayCompletedTools(event) {
  const call = state.call;
  const response = event.response || {};
  if (!call || call.sidebandOk || response.status !== "completed") return;
  if (response.id && (response.id !== call.responseId ||
      call.responseStartedGeneration !== call.responseGeneration)) return;
  const tools = Array.isArray(response.output) ? response.output.filter((item) => item.type === "function_call") : [];
  const generation = call.responseGeneration;
  const said = call.said || "";
  let completed = false;
  for (const tool of tools) {
    if (state.call !== call || call.responseGeneration !== generation) break;
    if (!tool.call_id || call.toolCalls.has(tool.call_id)) continue;
    call.toolCalls.add(tool.call_id);
    completed = await relayTool(tool, call, said) || completed;
    if (isEndCallTool(tool)) {
      finishCallAfterAudio(call);
      return;
    }
  }
  if (completed && state.call === call && call.responseGeneration === generation) {
    sendRealtime({ type: "response.create" });
  }
}

async function relayTool(event, call = state.call, said = call?.said || "") {
  if (!call || state.call !== call) return false;
  let args = {};
  try {
    args = JSON.parse(event.arguments || "{}");
    if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("Invalid tool arguments");
  } catch (_) {
    sendRealtime({ type: "conversation.item.create", item: { type: "function_call_output", call_id: event.call_id,
      output: JSON.stringify({ ok: false, error: "Tool arguments were invalid; no action was taken." }) } });
    return true;
  }
  try {
    const out = await api("/api/realtime/tools", {
      method: "POST",
      body: JSON.stringify({
        name: event.name,
        arguments: args,
        call_id: event.call_id || "",
        session_id: call.sessionId || call.callId,
        said,
      }),
    });
    if (state.call !== call) return false;
    sendRealtime({
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: event.call_id,
        output: JSON.stringify(out.output || out),
      },
    });
    applyWidgetPayload(out);
    refresh();
    return true;
  } catch (err) {
    if (state.call !== call) return false;
    appendLog("system", `Tool failed: ${err.message}`);
    flashLocalActivity("error", "Tool failed", 4000);
    sendRealtime({
      type: "conversation.item.create",
      item: { type: "function_call_output", call_id: event.call_id,
        output: JSON.stringify({ ok: false, error: "The tool response could not be received. Its outcome is unknown; check current state before retrying." }) },
    });
    return true;
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

function showListeningChrome() {
  $("orb").classList.add("live", "hot");
  $("orb").setAttribute("aria-label", "End conversation");
  $("orb-label").textContent = "Listening";
  $("hint").textContent = "Listening. Speak naturally. Tap to end the conversation.";
  $("voice-pill").textContent = "voice webrtc-ga";
  $("voice-pill").classList.add("live");
}

function showIdleVoiceChrome() {
  $("orb").classList.remove("live", "hot");
  $("orb").setAttribute("aria-label", "Tap to talk");
  $("orb-label").textContent = "Tap to talk";
  $("hint").textContent = idleHint();
  $("voice-pill").classList.remove("live");
}

function showReconnectingChrome() {
  $("orb").classList.add("live", "hot");
  $("orb-label").textContent = "Reconnecting";
  $("hint").textContent = phoneUi() ? "Reconnecting…" : "Reconnecting the live call…";
}

async function hangupCallId(callId) {
  if (!callId) return;
  try {
    await request(`/api/realtime/calls/${encodeURIComponent(callId)}/hangup`, { method: "POST" });
  } catch (_) {
    /* ignore */
  }
}

async function teardownCall(call) {
  if (!call) return;
  clearTimeout(call.endAudioGrace);
  clearTimeout(call.endAudioFallback);
  call.superseded = true;
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
  const remote = $("remote-audio");
  if (remote) remote.srcObject = null;
  await hangupCallId(call.callId);
}

function applyTransportDecision(decision) {
  if (!decision || !state.call || state.call.superseded) return;
  if (state.call.pendingHangup) return;
  if (decision.action === "wait") {
    showReconnectingChrome();
    return;
  }
  if (decision.action === "keep") {
    if (decision.reason === "connected" && $("orb-label").textContent === "Reconnecting") {
      showListeningChrome();
    }
    return;
  }
  if (decision.action === "reconnect") {
    void recoverConversation();
    return;
  }
  if (decision.action === "end" && decision.reason !== "user") {
    appendLog("system", "Voice dropped. Tap the hearth to try again.");
    void stopConversation();
  }
}

function bindCallTransport(call) {
  const onSignal = () => {
    if (!state.call || state.call !== call || call.superseded) return;
    if (!voiceLife.isCurrent(call.epoch)) return;
    applyTransportDecision(
      voiceLife.onTransport({
        connectionState: call.pc ? call.pc.connectionState : "",
        iceConnectionState: call.pc ? call.pc.iceConnectionState : "",
        dataChannelState: call.dc ? call.dc.readyState : "",
      })
    );
  };
  call.pc.addEventListener("connectionstatechange", onSignal);
  call.pc.addEventListener("iceconnectionstatechange", onSignal);
  if (call.dc) call.dc.addEventListener("close", onSignal);
}

async function onMicTrackEnded(call) {
  if (!call || call.superseded || state.call !== call) return;
  if (!voiceLife.isCurrent(call.epoch)) return;
  if ((call.micSwaps || 0) >= 1) {
    appendLog("system", "Microphone ended. Tap the hearth to talk again.");
    await stopConversation();
    return;
  }
  call.micSwaps += 1;
  try {
    const fresh = await acquireMicStream();
    if (state.call !== call || !voiceLife.isCurrent(call.epoch)) return;
    const next = fresh.getAudioTracks()[0];
    const sender = call.pc.getSenders().find((s) => !s.track || s.track.kind === "audio");
    if (!sender || !next) throw new Error("no audio sender");
    await sender.replaceTrack(next);
    call.stream = fresh;
    if (call.bargeIn && call.bargeIn.retarget) await call.bargeIn.retarget(next, fresh);
    next.addEventListener("ended", () => {
      void onMicTrackEnded(call);
    }, { once: true });
  } catch (_) {
    if (state.call !== call) return;
    appendLog("system", "Microphone ended. Tap the hearth to talk again.");
    await stopConversation();
  }
}

async function recoverConversation() {
  const epoch = voiceLife.beginReconnect();
  if (epoch == null) {
    if (voiceLife.phase === "recovering" || voiceLife.phase === "ending" || voiceLife.userEnded) return;
    appendLog("system", "Voice dropped. Tap the hearth to try again.");
    await stopConversation();
    return;
  }
  const previous = state.call;
  if (previous) previous.superseded = true;
  state.call = null;
  showReconnectingChrome();
  await teardownCall(previous);
  if (!voiceLife.isCurrent(epoch)) return;
  try {
    await startConversation({ epoch });
  } catch (err) {
    if (!voiceLife.isCurrent(epoch)) return;
    voiceLife.beginUserStop();
    voiceLife.markIdle();
    showIdleVoiceChrome();
    setRefreshInterval(8000);
    const classified = classifyMicError(err);
    appendLog("system", classified.message || "Voice dropped. Tap the hearth to try again.");
  }
}

async function startConversation({ epoch } = {}) {
  state.userUtterance = { final: "", partial: "" };
  const lifeEpoch = epoch == null ? voiceLife.beginUserStart() : epoch;
  const remote = $("remote-audio");
  const pc = new RTCPeerConnection();
  let callId = "";
  let bargeIn = null;
  const stale = () => !voiceLife.isCurrent(lifeEpoch);
  const bail = async () => {
    try {
      bargeIn && bargeIn.stop();
    } catch (_) {
      /* ignore */
    }
    abandonCallSetup(pc);
    await hangupCallId(callId);
  };

  let stream;
  try {
    stream = await acquireMicStream();
  } catch (err) {
    try {
      pc.close();
    } catch (_) {
      /* ignore */
    }
    throw err;
  }
  if (stale()) {
    await bail();
    return;
  }
  for (const track of stream.getAudioTracks()) {
    pc.addTrack(track, stream);
  }
  pc.ontrack = (ev) => {
    if (stale()) return;
    remote.srcObject = ev.streams[0];
    remote.play().catch(() => {});
  };
  const dc = pc.createDataChannel("oai-events");
  dc.addEventListener("message", (ev) => {
    try {
      if (stale()) return;
      onRealtimeEvent(JSON.parse(ev.data));
    } catch (_) {
      /* ignore non-json */
    }
  });
  try {
    const offer = await pc.createOffer();
    if (stale()) {
      await bail();
      return;
    }
    await pc.setLocalDescription(offer);
    if (stale()) {
      await bail();
      return;
    }
    const sdpResponse = await request("/api/realtime/calls", {
      method: "POST",
      body: offer.sdp,
      headers: { "Content-Type": "application/sdp" },
    });
    const path = sdpResponse.headers.get("X-Hearth-Realtime-Path") || "";
    const beta = sdpResponse.headers.get("X-Hearth-Realtime-Beta") || "";
    callId = sdpResponse.headers.get("X-Hearth-Call-Id") || "";
    if (stale()) {
      await bail();
      return;
    }
    if (!sdpResponse.ok) {
      let err = { error: `calls ${sdpResponse.status}` };
      try {
        err = await sdpResponse.json();
      } catch (_) {
        /* ignore */
      }
      throw new Error(err.error || err.message || `realtime/calls ${sdpResponse.status}`);
    }
    if (path && path !== "webrtc-ga") {
      throw new Error(`unexpected realtime path ${path}`);
    }
    if (beta === "true") {
      throw new Error("beta realtime path is disabled");
    }
    const answer = await sdpResponse.text();
    if (stale()) {
      await bail();
      return;
    }
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
    if (stale()) {
      await bail();
      return;
    }
    const sideband = sdpResponse.headers.get("X-Hearth-Sideband") || "";
    hideMicPanels();
    const micTrack = stream.getAudioTracks()[0] || null;
    if (micTrack && globalThis.HearthVad?.SpeechBargeIn) {
      bargeIn = new HearthVad.SpeechBargeIn(micTrack, stream);
      await bargeIn.start();
    }
    if (stale() || !voiceLife.markLive(lifeEpoch)) {
      await bail();
      return;
    }
    const call = {
      pc,
      dc,
      stream,
      callId,
      sidebandOk: sideband === "ok" || sideband === "starting",
      bargeIn,
      pendingHangup: false,
      audioPlaying: false,
      responseGeneration: 0,
      toolCalls: new Set(),
      said: "",
      inputItemId: "",
      sessionId: callId || crypto.randomUUID(),
      superseded: false,
      micSwaps: 0,
      epoch: lifeEpoch,
    };
    state.call = call;
    bindCallTransport(call);
    if (micTrack) {
      micTrack.addEventListener("ended", () => {
        void onMicTrackEnded(call);
      }, { once: true });
    }
    showListeningChrome();
    setRefreshInterval(1200);
    applyTransportDecision(
      voiceLife.onTransport({
        connectionState: pc.connectionState,
        iceConnectionState: pc.iceConnectionState,
        dataChannelState: dc.readyState,
      })
    );
  } catch (err) {
    if (state.call && state.call.pc === pc) state.call = null;
    await bail();
    throw err;
  }
}

async function stopConversation() {
  voiceLife.beginUserStop();
  const call = state.call;
  if (call) call.superseded = true;
  state.call = null;
  setRefreshInterval(8000);
  showIdleVoiceChrome();
  state.userUtterance = { final: "", partial: "" };
  await teardownCall(call);
  voiceLife.markIdle();
  refresh();
}

async function beginVoiceFromUserGesture() {
  if (
    state.call ||
    voiceLife.phase === "connecting" ||
    voiceLife.phase === "recovering" ||
    voiceLife.phase === "live" ||
    voiceLife.phase === "ending"
  ) {
    return;
  }
  const epoch = voiceLife.beginUserStart();
  hideMicPanels();
  $("orb").classList.add("hot");
  $("orb-label").textContent = "Connecting";
  $("hint").textContent = "Connecting…";
  try {
    await startConversation({ epoch });
  } catch (err) {
    if (!voiceLife.isCurrent(epoch)) return;
    voiceLife.beginUserStop();
    voiceLife.markIdle();
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
  if (voiceLife.phase === "ending") return;
  if (
    state.call ||
    voiceLife.phase === "connecting" ||
    voiceLife.phase === "recovering" ||
    voiceLife.phase === "live"
  ) {
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
  state.ambientReader?.stop();
  clearInfoEntrance();
  /* Document is going away — release hardware and tell Hearth to drop the sideband.
     A warm mute is useless across navigations, and an async hangup will not finish. */
  const call = state.call;
  voiceLife.beginUserStop();
  if (call) call.superseded = true;
  state.call = null;
  if (call && call.callId && navigator.sendBeacon) {
    try {
      navigator.sendBeacon(`/api/realtime/calls/${encodeURIComponent(call.callId)}/hangup`);
    } catch (_) {
      /* ignore */
    }
  }
  try {
    call && call.bargeIn && call.bargeIn.stop();
  } catch (_) {
    /* ignore */
  }
  try {
    call && call.pc && call.pc.close();
  } catch (_) {
    /* ignore */
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
