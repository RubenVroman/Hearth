/* Minimal shell cache. Documents and API stay on the network so the cookie session stays live. */
const VERSION = "hearth-shell-v12";
const SHELL = [
  "/static/styles.css",
  "/static/app.js",
  "/static/vad.js",
  "/static/settings.js",
  "/static/login.js",
  "/static/pwa.js",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/auth/") ||
    url.pathname.startsWith("/ws/") ||
    url.pathname === "/" ||
    url.pathname === "/login" ||
    url.pathname === "/sw.js"
  ) {
    return;
  }
  event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
});
