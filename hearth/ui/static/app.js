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
};

/** Client grace before fading when talk is clearly unrelated (ms). */
const OVERLAY_IRRELEVANT_GRACE_MS = 900;
/** Client idle soft-hide while still "relevant" but conversation is quiet (ms). */
const OVERLAY_IDLE_HIDE_MS = 55000;

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
  // Most recently updated wins (list is insertion-ordered from the server).
  return list[list.length - 1];
}

function overlaySignature(widget) {
  if (!widget) return "";
  const downloads = widget.data && Array.isArray(widget.data.downloads) ? widget.data.downloads : [];
  const progressKey = downloads
    .map((row) => `${row.title || ""}:${row.status || ""}:${row.percent ?? ""}`)
    .join(",");
  return [
    widget.id,
    widget.kind,
    widget.updated_at || "",
    widget.status || "",
    widget.title || "",
    widget.body || "",
    widget.detail || "",
    progressKey,
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

function overlayTopics(widget) {
  const ctx = widget && widget.context;
  if (ctx && Array.isArray(ctx.topics) && ctx.topics.length) {
    return ctx.topics.map((t) => String(t).toLowerCase());
  }
  const topics = new Set([String(widget.kind || "").toLowerCase()]);
  for (const chunk of [widget.title, widget.body, widget.detail]) {
    String(chunk || "")
      .toLowerCase()
      .match(/[a-z0-9']{3,}/g)
      ?.forEach((t) => topics.add(t));
  }
  return [...topics];
}

function textTouchesOverlay(text, widget) {
  if (!text || !widget) return false;
  const topics = new Set(overlayTopics(widget));
  const tokens = String(text)
    .toLowerCase()
    .match(/[a-z0-9']{3,}/g);
  if (!tokens) return false;
  return tokens.some((t) => topics.has(t));
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
  const root = $("info-overlay");
  if (!root) return;
  clearInfoCloseTimer();
  state.infoSoftHidden = false;
  root.hidden = false;
  root.classList.remove("is-soft-hidden", "is-closing");
  root.setAttribute("aria-hidden", "false");
  void root.offsetWidth;
  root.classList.add("is-open");
  scheduleOverlayIdleHide();
}

function looksUnrelatedToOverlay(text, widget) {
  if (!text || !widget) return false;
  if (textTouchesOverlay(text, widget)) return false;
  const kind = String(widget.kind || "");
  if (/\b(lights?|scenes?|turn (on|off)|dim |brightness|home assistant)\b/i.test(text)) return true;
  if (/\b(thuisbezorgd|just\s*eat|takeaway|hungry|restaurants?|pizza|burger|sushi|food cart)\b/i.test(text)) {
    return true;
  }
  if (/\b(docker|containers?)\b/i.test(text)) return true;
  if (kind === "weather" && /\b(movie|film|plex|playing|watch|radarr|sonarr|overseerr|infuse)\b/i.test(text)) {
    return true;
  }
  if (kind === "media" && /\b(weather|forecast|temperature|raining|humidity)\b/i.test(text)) {
    return true;
  }
  if (kind === "downloads" && /\b(weather|forecast|temperature|raining|humidity|lights?|scenes?)\b/i.test(text)) {
    return true;
  }
  return false;
}

/**
 * New user utterance — hide quickly when talk leaves the panel; keep/reveal when
 * it still touches on-screen topics. Server context on the next payload confirms.
 */
function noteOverlayConversation(text) {
  const visual = pickVisualOverlay(state.widgets);
  if (!visual) return;
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
    // Acknowledgments / side chat — leave visible until idle or server says otherwise.
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

function mediaMarkup(widget) {
  const item = (widget.data && widget.data.item) || {};
  const title = widget.title || item.title || "Untitled";
  const year = item.year;
  const type = item.type || "movie";
  const summary = item.summary || "";
  const meta = [type, year, item.show, item.contentRating]
    .filter(Boolean)
    .map((v) => escapeHtml(v))
    .join(" · ");
  const art = mediaArtUrl({ ...item, title });
  // /api/media/art returns real JPEG or SVG initials when the session cookie authorizes the GET.
  const poster = art
    ? `<img class="info-poster" src="${escapeHtml(art)}" alt="" width="108" height="162" loading="lazy" />`
    : mediaPosterFallback(title);
  const bits = [];
  if (summary) bits.push(`<p class="info-detail">${escapeHtml(summary)}</p>`);
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
  return `
    <div class="info-media">
      ${poster}
      <div class="info-media-copy">
        <p class="info-kicker">${item.pending ? "Ready to play" : "Library"}</p>
        <h2 class="info-title" id="info-title">${escapeHtml(title)}</h2>
        ${meta ? `<p class="info-meta">${meta}</p>` : ""}
        ${bits.join("")}
      </div>
    </div>
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
  if (widget.kind === "weather") return weatherMarkup(widget);
  if (widget.kind === "media") return mediaMarkup(widget);
  if (widget.kind === "downloads") return downloadsMarkup(widget);
  return `
    <p class="info-kicker">Info</p>
    <h2 class="info-title" id="info-title">${escapeHtml(widget.title || "")}</h2>
    <p class="info-body">${escapeHtml(widget.body || "")}</p>
    ${widget.detail ? `<p class="info-detail">${escapeHtml(widget.detail)}</p>` : ""}
  `;
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
  if (alreadyOpen && state.infoSignature === signature) {
    scheduleOverlayIdleHide();
    return;
  }
  content.innerHTML = overlayInnerHtml(widget);
  state.infoSignature = signature;
  state.infoSoftHidden = false;
  root.hidden = false;
  root.setAttribute("aria-hidden", "false");
  root.classList.remove("is-closing", "is-soft-hidden");
  // Force style flush so enter transition runs when opening from hidden / soft-hidden.
  if (!alreadyOpen) {
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
  if (!animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    root.classList.remove("is-open", "is-closing");
    root.hidden = true;
    root.setAttribute("aria-hidden", "true");
    const content = $("info-content");
    if (content) content.innerHTML = "";
    return;
  }
  root.classList.add("is-closing");
  root.classList.remove("is-open");
  state.infoCloseTimer = setTimeout(() => {
    root.classList.remove("is-closing");
    root.hidden = true;
    root.setAttribute("aria-hidden", "true");
    const content = $("info-content");
    if (content) content.innerHTML = "";
    state.infoCloseTimer = null;
  }, 300);
}

function renderWidgets(widgets) {
  const list = Array.isArray(widgets) ? widgets : [];
  state.widgets = list;
  const visual = pickVisualOverlay(list);
  if (!visual) {
    closeInfoOverlay({ animate: true });
    return;
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
      if (content) content.innerHTML = overlayInnerHtml(visual);
      state.infoSignature = overlaySignature(visual);
    }
    softHideInfoOverlay();
  }
}

async function dismissWidget(id, { silent = false } = {}) {
  const next = state.widgets.filter((w) => w.id !== id);
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
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const root = $("info-overlay");
    if (!root || root.hidden) return;
    if (!root.classList.contains("is-open") && !root.classList.contains("is-soft-hidden")) return;
    dismiss();
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
  const out = await api("/api/chat", {
    method: "POST",
    body: JSON.stringify({ message, confirm }),
  });
  appendLog("hearth", out.reply);
  applyWidgetPayload(out);
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
  if (state.call?.bargeIn) {
    state.call.bargeIn.noteRealtimeEvent(type);
  }
  if (type === "response.output_audio_transcript.done" || type === "response.audio_transcript.done") {
    appendLog("hearth", event.transcript);
  }
  // User speech — requires session audio.input.transcription (see webrtc.session_config).
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
  }
  if (type === "response.function_call_arguments.done" && event.name === "end_call") {
    if (state.call) state.call.pendingHangup = true;
  }
  if (type === "response.done" && state.call?.pendingHangup) {
    stopConversation();
    return;
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

async function boot() {
  if (window.HearthSettings) window.HearthSettings.mount();
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
  setInterval(refresh, 8000);
}

boot();
