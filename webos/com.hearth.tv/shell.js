/* webOS wrapper. Stores the Hearth origin only — never a token.
   Back (keyCode 461) leaves the app. Open Hearth is one OK press so Back
   from the dashboard can return here without an immediate redirect loop. */

const STORAGE_KEY = "hearth.tv.baseUrl";
const DEFAULT_BASE = "http://192.168.2.26:8787";

const field = document.getElementById("base-url");
const error = document.getElementById("error");

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

function showError(text) {
  error.textContent = text || "";
}

function savedBase() {
  return parseBase(localStorage.getItem(STORAGE_KEY) || "");
}

function openHearth(base) {
  localStorage.setItem(STORAGE_KEY, base);
  location.assign(`${base}/tv`);
}

function focusables() {
  return [...document.querySelectorAll("[data-tv-focus]")].filter((el) => !el.disabled);
}

function moveFocus(direction) {
  const items = focusables();
  const index = Math.max(0, items.indexOf(document.activeElement));
  const next = direction === "up" || direction === "left" ? index - 1 : index + 1;
  const target = items[Math.max(0, Math.min(items.length - 1, next))] || items[0];
  if (target) target.focus();
}

function platformBack() {
  const palm = window.PalmSystem;
  if (palm && typeof palm.platformBack === "function") {
    palm.platformBack();
    return;
  }
  if (window.webOS && typeof window.webOS.platformBack === "function") window.webOS.platformBack();
}

document.getElementById("open-btn").addEventListener("click", () => {
  const base = parseBase(field.value);
  if (!base) {
    showError("Use an http or https address with no password, query, or token.");
    return;
  }
  showError("");
  openHearth(base);
});

document.getElementById("forget-btn").addEventListener("click", () => {
  localStorage.removeItem(STORAGE_KEY);
  field.value = DEFAULT_BASE;
  showError("Forgotten on this TV. The default LAN address is filled in again.");
});

document.addEventListener("keydown", (event) => {
  const back = event.keyCode === 461 || event.key === "GoBack" || event.key === "XF86Back";
  if (back) {
    event.preventDefault();
    platformBack();
    return;
  }
  const map = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
  const direction = map[event.key];
  if (!direction) return;
  if (event.target && event.target.tagName === "INPUT" && (direction === "left" || direction === "right")) return;
  event.preventDefault();
  moveFocus(direction);
});

document.addEventListener("webOSRelaunch", () => {
  const base = savedBase();
  if (base) openHearth(base);
});

const remembered = savedBase();
if (remembered) field.value = remembered;
else field.value = field.value || DEFAULT_BASE;
document.getElementById("open-btn").focus();
