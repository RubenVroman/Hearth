/** Hands-free reading of verified tool results. No model HTML or inferred actions. */
(function (global) {
  "use strict";

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function safeUrl(value) {
    try {
      const url = new URL(String(value || ""));
      return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
    } catch (_) { return ""; }
  }

  function informationMarkup(widget) {
    const data = widget.data || {};
    const items = Array.isArray(data.items) ? data.items.filter((row) => row && typeof row === "object") : [];
    const sources = Array.isArray(data.sources) ? data.sources : [];
    const summary = data.summary || widget.body || "";
    const sourceLink = (source) => {
      const url = safeUrl(source && source.url);
      if (!url) return "";
      return `<a class="info-source" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title || new URL(url).hostname)}</a>`;
    };
    return `<section class="info-knowledge">
      <header class="info-board-heading"><p class="info-kicker">${widget.status === "error" ? "Couldn’t complete this" : "Found for you"}</p>
      <h2 class="info-title" id="info-title">${escapeHtml(widget.title || data.query || "Here’s what I found")}</h2>
      ${summary ? `<p class="info-board-summary">${escapeHtml(summary)}</p>` : ""}</header>
      ${items.length ? `<div class="info-facts">${items.map((row, i) => `<article class="info-fact">
        <span class="info-result-number" aria-hidden="true">${String(i + 1).padStart(2, "0")}</span>
        <div><h3>${escapeHtml(row.title || "Result")}</h3>
        <p>${escapeHtml(row.body || row.detail || row.snippet || "")}</p>
        ${sourceLink({ title: row.source || "Source", url: row.url })}</div>
      </article>`).join("")}</div>` : ""}
      ${widget.detail && widget.detail !== summary ? `<p class="info-detail">${escapeHtml(widget.detail)}</p>` : ""}
      ${sources.length ? `<div class="info-sources" aria-label="Sources">${sources.map(sourceLink).join("")}</div>` : ""}
    </section>`;
  }

  /** Move by a readable page with overlap; always dwell at the end before looping. */
  function nextReadingPosition(scrollTop, height, scrollHeight) {
    const max = Math.max(0, scrollHeight - height);
    if (max < 8 || height < 1) return 0;
    if (scrollTop >= max - 4) return 0;
    return Math.min(max, scrollTop + Math.max(1, Math.floor(height * 0.72)));
  }

  class AmbientReader {
    constructor({ viewport, button, status, document: doc = global.document, delay = 15000 }) {
      this.viewport = viewport;
      this.button = button;
      this.status = status;
      this.document = doc;
      this.delay = delay;
      this.timer = null;
      this.key = "";
      this.paused = false;
      this.running = false;
      this.button?.addEventListener("click", () => this.setPaused(!this.paused));
      for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) {
        viewport?.addEventListener(event, (ev) => {
          if (ev.target === this.button || this.button?.contains?.(ev.target)) return;
          this.setPaused(true);
        }, { passive: true });
      }
      doc?.addEventListener("visibilitychange", () => {
        this.clear();
        if (!doc.hidden) this.schedule();
      });
    }

    clear() { if (this.timer != null) clearTimeout(this.timer); this.timer = null; }

    setPaused(paused) {
      this.paused = paused;
      this.clear();
      this.updateStatus();
      this.schedule();
    }

    updateStatus() {
      const overflow = this.viewport && this.viewport.scrollHeight > this.viewport.clientHeight + 8;
      if (this.button) {
        this.button.hidden = !overflow;
        this.button.textContent = this.paused ? "Resume reading" : "Pause reading";
        this.button.setAttribute("aria-pressed", String(this.paused));
      }
      const text = overflow
        ? this.paused ? "Reading paused" : "All results · advances automatically"
        : "All results · at a glance";
      if (this.status && this.status.textContent !== text) this.status.textContent = text;
    }

    show(key) {
      if (key !== this.key) {
        this.clear();
        this.key = key;
        this.paused = false;
        if (this.viewport) this.viewport.scrollTop = 0;
      }
      this.running = true;
      this.updateStatus();
      this.schedule();
    }

    stop() { this.running = false; this.clear(); }

    schedule() {
      if (!this.running || this.paused || this.timer != null || this.document?.hidden) return;
      this.timer = setTimeout(() => {
        this.timer = null;
        if (!this.running || this.paused || this.document?.hidden) return;
        const el = this.viewport;
        if (el) {
          const top = nextReadingPosition(el.scrollTop, el.clientHeight, el.scrollHeight);
          const still = global.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches || this.document?.documentElement?.dataset?.motion === "still";
          el.scrollTo({ top, behavior: still || top === 0 ? "instant" : "smooth" });
        }
        this.updateStatus();
        this.schedule();
      }, this.delay);
    }
  }

  const api = { escapeHtml, safeUrl, informationMarkup, nextReadingPosition, AmbientReader };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.HearthPresentation = api;
})(typeof window !== "undefined" ? window : globalThis);
