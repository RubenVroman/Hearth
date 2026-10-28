/* Living-room shell.
   Magic Remote / D-pad: arrows move the ember focus ring, Enter activates,
   Back (webOS keyCode 461) closes Setup or returns to the webOS address screen.
   Pointer clicks work because every control is a button. */

const ACCESS_KEY = "hearth.tv.accessToken";
const MACHINE_KEY = "hearth.tv.machineToken";

const $ = (id) => document.getElementById(id);

let board = null;
let toastTimer = 0;

function isBackKey(event) {
  return event.keyCode === 461 || event.key === "GoBack" || event.key === "XF86Back" || event.key === "BrowserBack";
}

function isTypingTarget(node) {
  if (!node || !node.tagName) return false;
  const tag = node.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || node.isContentEditable;
}

function focusables(root) {
  const scope = root || document;
  return [...scope.querySelectorAll("[data-tv-focus]")].filter((el) => {
    if (el.disabled || el.hidden) return false;
    if (el.closest("[hidden]")) return false;
    return el.getClientRects().length > 0;
  });
}

function moveFocus(direction) {
  const items = focusables().map((el) => {
    const rect = el.getBoundingClientRect();
    return { el, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  });
  if (!items.length) return;
  const current = document.activeElement;
  const from = items.find((item) => item.el === current) || items[0];
  const horizontal = direction === "left" || direction === "right";
  const forward = direction === "right" || direction === "down";
  let best = null;
  let bestScore = Infinity;
  for (const item of items) {
    if (item.el === from.el) continue;
    const dx = item.x - from.x;
    const dy = item.y - from.y;
    if (horizontal) {
      if (forward ? dx <= 8 : dx >= -8) continue;
      const score = Math.abs(dx) + Math.abs(dy) * 4;
      if (score < bestScore) {
        best = item;
        bestScore = score;
      }
    } else if (forward ? dy > 8 : dy < -8) {
      const score = Math.abs(dy) + Math.abs(dx) * 2;
      if (score < bestScore) {
        best = item;
        bestScore = score;
      }
    }
  }
  (best || from).el.focus();
}

function focusFirst(root) {
  const items = focusables(root ? $(root) : document);
  if (items[0]) items[0].focus();
}

function focusIfLost(root) {
  const items = focusables(root ? $(root) : document);
  if (!items.length) return;
  if (!items.includes(document.activeElement)) items[0].focus();
}

function showToast(text, warn) {
  const toast = $("toast");
  toast.textContent = text || "";
  toast.classList.toggle("show", Boolean(text));
  toast.classList.toggle("warn", Boolean(warn));
  window.clearTimeout(toastTimer);
  if (text) toastTimer = window.setTimeout(() => toast.classList.remove("show"), 6000);
}

function credentialHeaders() {
  const headers = { "Content-Type": "application/json" };
  const access = localStorage.getItem(ACCESS_KEY) || "";
  const machine = localStorage.getItem(MACHINE_KEY) || "";
  if (access) headers["X-Auth-Token"] = access;
  if (machine) headers["X-Hearth-Token"] = machine;
  return headers;
}

function hasCredential() {
  return Boolean(localStorage.getItem(ACCESS_KEY) || localStorage.getItem(MACHINE_KEY));
}

async function tryRefresh() {
  try {
    const response = await fetch("/auth/session/refresh", { method: "POST" });
    if (!response.ok) return false;
    const body = await response.json();
    if (!body.access_token) return false;
    localStorage.setItem(ACCESS_KEY, body.access_token);
    return true;
  } catch (_) {
    return false;
  }
}

async function api(path, options) {
  const opts = options || {};
  let response = await fetch(path, { ...opts, headers: { ...credentialHeaders(), ...(opts.headers || {}) } });
  if (response.status === 401 && localStorage.getItem(ACCESS_KEY)) {
    localStorage.removeItem(ACCESS_KEY);
    if (await tryRefresh()) {
      response = await fetch(path, { ...opts, headers: { ...credentialHeaders(), ...(opts.headers || {}) } });
    }
  }
  if (response.status === 401) {
    showGate();
    const error = new Error("unauthorized");
    error.status = 401;
    throw error;
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail || body.error || `${path} ${response.status}`;
    const error = new Error(typeof detail === "string" ? detail : "Request failed");
    error.status = response.status;
    throw error;
  }
  return body;
}

function showGate() {
  $("dashboard").hidden = true;
  $("settings").hidden = true;
  $("gate").hidden = false;
  $("mode-pill").textContent = "signed out";
  focusFirst("gate");
}

function showDashboard() {
  $("gate").hidden = true;
  $("settings").hidden = true;
  $("dashboard").hidden = false;
}

function showSettings() {
  $("settings").hidden = false;
  $("origin-line").textContent = `This screen is ${location.origin}. Leave the field as-is unless you are moving to another Hearth.`;
  const field = $("base-url");
  if (!field.value) field.value = location.origin;
  $("machine-token").value = "";
  $("token-state").textContent = localStorage.getItem(MACHINE_KEY)
    ? "A machine token is saved on this TV. It is not shown again."
    : "Not saved on this TV.";
  focusFirst("settings");
}

function platformBack() {
  const palm = window.PalmSystem;
  if (palm && typeof palm.platformBack === "function") {
    palm.platformBack();
    return;
  }
  if (window.webOS && typeof window.webOS.platformBack === "function") {
    window.webOS.platformBack();
  }
}

function handleBack() {
  if (!$("settings").hidden) {
    $("settings").hidden = true;
    if (!$("dashboard").hidden) focusFirst("dashboard");
    else focusFirst("gate");
    return;
  }
  if (history.length > 1) {
    history.back();
    return;
  }
  platformBack();
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "glyph");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  use.setAttributeNS("http://www.w3.org/1999/xlink", "href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function glyphFor(kind, id) {
  if (kind === "transport") return id === "pause" || id === "stop" ? id : "play";
  if (kind === "volume") {
    if (id === "down") return "speaker-down";
    if (id === "up") return "speaker-up";
    return id === "unmute" ? "speaker-slash" : "speaker";
  }
  if (kind === "activity") {
    if (id === "movie_night") return "moon";
    if (id === "apple_tv") return "tv";
    if (id === "tv") return "waveform";
    if (id === "off") return "power";
  }
  if (kind === "scene") return "light";
  if (kind === "launch") {
    if (id === "infuse") return "play";
    if (id === "videoland") return "film";
    if (id === "netflix") return "app";
    if (id === "prime") return "play-circle";
  }
  if (kind === "continue") return "play";
  return "app";
}

function setIcon(button, name) {
  const use = button.querySelector("use");
  if (!use) return;
  use.setAttribute("href", `#i-${name}`);
  use.setAttributeNS("http://www.w3.org/1999/xlink", "href", `#i-${name}`);
}

function actionButton(label, hint, attrs) {
  const button = el("button");
  button.type = "button";
  button.setAttribute("data-tv-focus", "");
  Object.entries(attrs).forEach(([key, value]) => {
    button.dataset[key] = value;
  });
  button.appendChild(icon(glyphFor(attrs.kind, attrs.id)));
  const copy = el("span", null, "copy");
  copy.appendChild(el("span", label, "label"));
  if (hint) copy.appendChild(el("small", hint));
  button.appendChild(copy);
  return button;
}

function renderPlayers(players) {
  const host = $("players");
  host.replaceChildren();
  [
    ["tv", "LG"],
    ["avr", "Denon"],
    ["apple_tv", "Apple TV"],
  ].forEach(([role, fallback]) => {
    const row = players[role] || {};
    const card = el("p", null, "player");
    const glyph = role === "avr" ? "speaker" : role === "apple_tv" ? "play" : "tv";
    const title = el("strong", row.name || fallback);
    const bits = [row.state || "unknown"];
    if (row.source) bits.push(String(row.source));
    if (row.media_title) bits.push(String(row.media_title));
    if (row.volume_pct != null) bits.push(`${row.volume_pct}%`);
    if (row.muted) bits.push("muted");
    const copy = el("span", null, "copy");
    copy.append(title, document.createTextNode(bits.join(" · ")));
    card.append(icon(glyph), copy);
    host.appendChild(card);
  });
}

function renderBoard(next) {
  board = next;
  showDashboard();
  const house = next.house || "VAULT";
  $("house-line").textContent = `${house} · ${next.target_tv || "LG webOS"}`;
  $("mode-pill").textContent = next.mode === "live" ? "Live" : next.mode === "mock" ? "Fixture" : next.mode || "Hearth";

  const now = next.now;
  $("now-title").textContent = now ? now.show || now.title : "Nothing on";
  const meta = [];
  if (now) {
    if (now.show && now.title) meta.push(now.title);
    if (now.state) meta.push(now.state);
    if (now.player) meta.push(now.player);
    if (now.year) meta.push(String(now.year));
  } else {
    meta.push("Plex is quiet. The house buttons below still talk to Home Assistant.");
  }
  $("now-meta").textContent = meta.join(" · ");
  const progress = $("now-progress");
  if (now && now.progress_pct) {
    progress.hidden = false;
    $("now-progress-bar").style.width = `${now.progress_pct}%`;
  } else {
    progress.hidden = true;
  }

  renderPlayers(next.players || {});
  const avr = (next.players || {}).avr || {};
  const volume = avr.volume_pct == null ? "—" : `${avr.volume_pct}%`;
  $("volume-readout").textContent = avr.muted ? `${volume} muted` : volume;
  const mute = $("mute-btn");
  const muted = Boolean(avr.muted);
  mute.dataset.id = muted ? "unmute" : "mute";
  mute.querySelector(".label").textContent = muted ? "Unmute" : "Mute";
  setIcon(mute, muted ? "speaker-slash" : "speaker");
  const sources = next.avr_sources || {};
  $("volume-note").textContent = next.receiver_centric
    ? `Louder and quieter go to the Denon. Apple TV selects ${sources.apple_tv || "Media Player"}. TV Audio selects ${sources.tv || "TV Audio"}.`
    : "Volume goes to the Denon.";

  const activities = $("activities");
  activities.replaceChildren();
  (next.activities || []).forEach((row) => {
    activities.appendChild(actionButton(row.label, row.hint, { kind: "activity", id: row.id }));
  });

  const scenes = $("scenes");
  scenes.replaceChildren();
  (next.presets || []).forEach((row) => {
    if (row.preset === "movie_night") return;
    const button = actionButton(row.label || row.preset, row.available ? "Lights" : "Not in Home Assistant", {
      kind: "scene",
      id: row.preset,
    });
    scenes.appendChild(button);
  });

  const launches = $("launches");
  launches.replaceChildren();
  (next.launches || []).forEach((row) => {
    const hint = row.available
      ? row.hint
      : row.status === "not_in_source_list"
        ? "Not on this TV"
        : "Needs HA pairing";
    const button = actionButton(row.label, hint, { kind: "launch", id: row.id });
    button.title = row.pairing || "";
    launches.appendChild(button);
  });

  const shelf = $("shelf");
  shelf.replaceChildren();
  const items = next.continue_watching || [];
  if (!items.length) {
    shelf.appendChild(el("p", "Nothing half-finished on the Plex shelf.", "fine"));
  }
  items.forEach((item) => {
    const hint = item.progress_pct ? `${item.progress_pct}% · Infuse` : "Infuse";
    const button = actionButton(item.label || item.title || "Untitled", hint, {
      kind: "continue",
      id: item.ratingKey || "",
      title: item.title || item.label || "",
    });
    if (item.season != null) button.dataset.season = String(item.season);
    if (item.episode != null) button.dataset.episode = String(item.episode);
    if (!item.ratingKey) button.disabled = true;
    shelf.appendChild(button);
  });

  const requests = next.overseerr || {};
  $("overseerr").querySelector(".label").textContent = requests.todo || "Requests are not on this screen.";
  focusIfLost("dashboard");
}

async function loadBoard() {
  const next = await api("/api/tv/board");
  renderBoard(next);
}

async function runControl(button) {
  const kind = button.dataset.kind;
  const id = button.dataset.id || "";
  if (!kind) return;
  button.disabled = true;
  try {
    const payload = {
      kind,
      id,
      title: button.dataset.title || "",
    };
    if (button.dataset.season) payload.season = Number(button.dataset.season);
    if (button.dataset.episode) payload.episode = Number(button.dataset.episode);
    const result = await api("/api/tv/action", { method: "POST", body: JSON.stringify(payload) });
    showToast(result.speak || (result.ok ? "Done." : "That didn't work."), result.ok === false);
    await loadBoard();
  } catch (error) {
    if (error.status !== 401) showToast(error.message || "Could not reach Hearth.", true);
  } finally {
    button.disabled = false;
  }
}

function parseBase(raw) {
  let url;
  try {
    url = new URL(String(raw || "").trim());
  } catch (_) {
    return null;
  }
  if (url.username || url.password || url.search || url.hash) return null;
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.pathname && url.pathname !== "/") return null;
  return url.origin;
}

document.addEventListener("keydown", (event) => {
  if (isBackKey(event)) {
    event.preventDefault();
    handleBack();
    return;
  }
  const map = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
  if (event.key === "Enter" || event.keyCode === 13) {
    const current = document.activeElement;
    if (current && current.tagName === "BUTTON" && !current.disabled) {
      event.preventDefault();
      current.click();
    }
    return;
  }
  const direction = map[event.key];
  if (!direction) return;
  if (isTypingTarget(event.target) && (direction === "left" || direction === "right")) return;
  event.preventDefault();
  moveFocus(direction);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-kind]");
  if (!button || button.disabled) return;
  runControl(button);
});

$("setup-btn").addEventListener("click", () => showSettings());
$("gate-setup").addEventListener("click", () => showSettings());
$("close-settings").addEventListener("click", () => {
  $("settings").hidden = true;
  if (!$("dashboard").hidden) focusFirst("dashboard");
  else focusFirst("gate");
});

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  const email = $("email").value.trim();
  const password = $("password").value;
  $("password").value = "";
  try {
    const response = await fetch("/auth/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!response.ok) {
      $("login-error").textContent = "That login did not work.";
      return;
    }
    const body = await response.json();
    if (body.access_token) localStorage.setItem(ACCESS_KEY, body.access_token);
    await loadBoard();
  } catch (_) {
    $("login-error").textContent = "Could not reach Hearth.";
  }
});

$("save-settings").addEventListener("click", async () => {
  const token = $("machine-token").value.trim();
  if (token) {
    localStorage.setItem(MACHINE_KEY, token);
    $("machine-token").value = "";
  }
  const next = parseBase($("base-url").value);
  const here = location.origin;
  if ($("base-url").value.trim() && !next) {
    showToast("Use an http(s) address without a password or token in the URL.", true);
    return;
  }
  if (next && next !== here) {
    location.assign(`${next}/tv`);
    return;
  }
  if (!hasCredential() && !(await tryRefresh())) {
    showToast("Saved. Sign in, or paste a machine token.", true);
    showGate();
    return;
  }
  try {
    await loadBoard();
    showToast("Saved on this TV.");
  } catch (error) {
    if (error.status !== 401) showToast(error.message || "Could not reach Hearth.", true);
  }
});

$("clear-token").addEventListener("click", () => {
  localStorage.removeItem(MACHINE_KEY);
  $("machine-token").value = "";
  $("token-state").textContent = "Not saved on this TV.";
  showToast("Machine token cleared.");
});

$("logout-btn").addEventListener("click", async () => {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(MACHINE_KEY);
  $("machine-token").value = "";
  try {
    await fetch("/auth/session/logout", { method: "POST" });
  } catch (_) {
    /* The local session is already gone. */
  }
  showGate();
});

$("overseerr").addEventListener("click", () => {
  const label = $("overseerr").querySelector(".label");
  const text = (board && board.overseerr && board.overseerr.todo) || (label ? label.textContent : "");
  showToast(text, true);
});

async function boot() {
  if (!localStorage.getItem(ACCESS_KEY)) await tryRefresh();
  if (!hasCredential()) {
    showGate();
    return;
  }
  try {
    await loadBoard();
  } catch (error) {
    if (error.status !== 401) {
      showToast("Could not reach Hearth.", true);
      showGate();
    }
  }
}

boot();
