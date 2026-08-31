/**
 * Spoken-answer read-along panel.
 *
 * Shows the assistant's Realtime output-audio transcript while Hearth is
 * speaking so Ruben can read along. Driven only by the existing WebRTC
 * data-channel events (same stream as barge-in + overlay conversation notes).
 *
 * Lifetime mirrors the glass-overlay soft-hide language:
 *   - appear on first spoken delta
 *   - hold briefly after speech completes, then fade
 *   - dismiss on barge-in / cancel / call end / new turn
 *
 * Fail-safe: every public entry point swallows errors so the voice path
 * never breaks if the panel fails.
 */
(function (global) {
  "use strict";

  /** Tasteful hold after the spoken transcript finalizes (ms). */
  const HOLD_MS = 1600;
  /** Quick fade after barge-in / cancel before hard clear (ms). */
  const INTERRUPT_FADE_MS = 220;
  /** Enter/exit animation budget — keep in sync with CSS. */
  const CLOSE_MS = 300;
  /** Max characters kept in the live buffer (guard against runaway). */
  const MAX_CHARS = 4000;

  const DELTA_TYPES = new Set([
    "response.output_audio_transcript.delta",
    "response.audio_transcript.delta",
  ]);
  const DONE_TYPES = new Set([
    "response.output_audio_transcript.done",
    "response.audio_transcript.done",
  ]);
  /** Same stop-speaking set VAD uses for barge-in (vad.js noteRealtimeEvent). */
  const INTERRUPT_TYPES = new Set([
    "input_audio_buffer.speech_started",
    "response.cancelled",
  ]);
  const AUDIO_STOPPED_TYPES = new Set(["output_audio_buffer.stopped"]);

  /**
   * Pure lifetime classifier — unit-tested. Returns one of:
   *   reveal | finalize | interrupt | soft_stop | reset | ignore
   */
  function classifyEvent(type) {
    if (!type) return "ignore";
    if (DELTA_TYPES.has(type)) return "reveal";
    if (DONE_TYPES.has(type)) return "finalize";
    if (INTERRUPT_TYPES.has(type)) return "interrupt";
    if (AUDIO_STOPPED_TYPES.has(type)) return "soft_stop";
    if (type === "response.created") return "reset";
    return "ignore";
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /**
   * Build karaoke-style markup: settled prefix + live trailing chunk.
   * Never invents timing — live span is only the newest delta.
   */
  function renderMarkup(settled, liveChunk) {
    const settledHtml = escapeHtml(settled);
    const liveHtml = escapeHtml(liveChunk);
    if (!settledHtml && !liveHtml) return "";
    if (!liveHtml) {
      return `<span class="spoken-answer-settled">${settledHtml}</span>`;
    }
    return (
      `<span class="spoken-answer-settled">${settledHtml}</span>` +
      `<span class="spoken-answer-live">${liveHtml}</span>`
    );
  }

  function prefersReducedMotion() {
    try {
      return Boolean(global.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
    } catch (_) {
      return false;
    }
  }

  class SpokenAnswerPanel {
    /**
     * @param {{ root?: HTMLElement | null, textEl?: HTMLElement | null } | null} els
     */
    constructor(els) {
      this.root = (els && els.root) || null;
      this.textEl = (els && els.textEl) || null;
      this._full = "";
      this._liveChunk = "";
      this._holdTimer = null;
      this._closeTimer = null;
      this._visible = false;
      this._closing = false;
      this._generation = 0;
    }

    bind(root, textEl) {
      this.root = root || this.root;
      this.textEl = textEl || this.textEl;
      return this;
    }

    /** Feed a Realtime data-channel event. Never throws. */
    onRealtimeEvent(type, event) {
      try {
        const action = classifyEvent(type);
        if (action === "reveal") {
          const delta = (event && event.delta) || "";
          if (delta) this._appendDelta(delta);
          return;
        }
        if (action === "finalize") {
          const text =
            (event && (event.transcript || event.text)) || this._full || "";
          this._finalize(text);
          return;
        }
        if (action === "interrupt") {
          this.dismiss({ reason: "interrupt", holdMs: INTERRUPT_FADE_MS });
          return;
        }
        if (action === "soft_stop") {
          // Audio ended; if we never got .done, hold whatever was spoken then fade.
          if (this._visible && this._full && !this._holdTimer) {
            this._scheduleHold(HOLD_MS);
          }
          return;
        }
        if (action === "reset") {
          this.dismiss({ reason: "new_turn", immediate: true });
        }
      } catch (_) {
        /* fail closed — never break the voice path */
      }
    }

    /** Call hangup / session end — panel must never stick. */
    onCallEnded() {
      try {
        this.dismiss({ reason: "call_end", immediate: true });
      } catch (_) {
        /* ignore */
      }
    }

    clearTimers() {
      if (this._holdTimer) {
        clearTimeout(this._holdTimer);
        this._holdTimer = null;
      }
      if (this._closeTimer) {
        clearTimeout(this._closeTimer);
        this._closeTimer = null;
      }
    }

    /**
     * @param {{ reason?: string, immediate?: boolean, holdMs?: number }} [opts]
     */
    dismiss(opts) {
      try {
        const options = opts || {};
        this.clearTimers();
        this._liveChunk = "";
        if (options.immediate) {
          this._hardHide();
          this._full = "";
          this._paint();
          return;
        }
        const hold = typeof options.holdMs === "number" ? options.holdMs : 0;
        if (hold > 0 && this._visible) {
          this._paint({ settleAll: true });
          this._scheduleHold(hold);
          return;
        }
        this._beginClose();
      } catch (_) {
        try {
          this._hardHide();
        } catch (__) {
          /* ignore */
        }
      }
    }

    _appendDelta(delta) {
      this.clearTimers();
      this._closing = false;
      const next = `${this._full || ""}${delta}`;
      this._full = next.length > MAX_CHARS ? next.slice(next.length - MAX_CHARS) : next;
      this._liveChunk = delta;
      this._show();
      this._paint();
    }

    _finalize(text) {
      const finalText = String(text || this._full || "").trim();
      this._full = finalText.length > MAX_CHARS ? finalText.slice(0, MAX_CHARS) : finalText;
      this._liveChunk = "";
      if (!this._full) {
        this.dismiss({ reason: "empty", immediate: true });
        return;
      }
      this._show();
      this._paint({ settleAll: true });
      this._scheduleHold(HOLD_MS);
    }

    _scheduleHold(ms) {
      this.clearTimers();
      const gen = this._generation;
      this._holdTimer = setTimeout(() => {
        this._holdTimer = null;
        if (gen !== this._generation) return;
        this._beginClose();
      }, Math.max(0, ms));
    }

    _show() {
      const root = this.root;
      if (!root) return;
      root.hidden = false;
      root.setAttribute("aria-hidden", "false");
      root.classList.remove("is-closing");
      // Force style flush so enter transition runs when reopening.
      void root.offsetWidth;
      root.classList.add("is-open");
      this._visible = true;
      this._closing = false;
    }

    _beginClose() {
      const root = this.root;
      if (!root || !this._visible) {
        this._hardHide();
        return;
      }
      if (this._closing) return;
      this._closing = true;
      this.clearTimers();
      if (prefersReducedMotion()) {
        this._hardHide();
        this._full = "";
        this._liveChunk = "";
        this._paint();
        return;
      }
      root.classList.add("is-closing");
      root.classList.remove("is-open");
      const gen = ++this._generation;
      this._closeTimer = setTimeout(() => {
        this._closeTimer = null;
        if (gen !== this._generation) return;
        this._hardHide();
        this._full = "";
        this._liveChunk = "";
        this._paint();
      }, CLOSE_MS);
    }

    _hardHide() {
      this.clearTimers();
      this._generation += 1;
      this._visible = false;
      this._closing = false;
      const root = this.root;
      if (!root) return;
      root.classList.remove("is-open", "is-closing");
      root.hidden = true;
      root.setAttribute("aria-hidden", "true");
    }

    _paint(opts) {
      const el = this.textEl;
      if (!el) return;
      const settleAll = Boolean(opts && opts.settleAll);
      const live = settleAll ? "" : this._liveChunk;
      const settled = settleAll ? this._full : (this._full || "").slice(0, Math.max(0, (this._full || "").length - (live || "").length));
      el.innerHTML = renderMarkup(settled, live);
    }
  }

  function createFromDocument(doc) {
    const documentRef = doc || global.document;
    if (!documentRef) return new SpokenAnswerPanel(null);
    return new SpokenAnswerPanel({
      root: documentRef.getElementById("spoken-answer"),
      textEl: documentRef.getElementById("spoken-answer-text"),
    });
  }

  const api = {
    HOLD_MS,
    INTERRUPT_FADE_MS,
    CLOSE_MS,
    MAX_CHARS,
    DELTA_TYPES,
    DONE_TYPES,
    INTERRUPT_TYPES,
    AUDIO_STOPPED_TYPES,
    classifyEvent,
    renderMarkup,
    escapeHtml,
    SpokenAnswerPanel,
    createFromDocument,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  global.HearthSpokenAnswer = api;
})(typeof window !== "undefined" ? window : globalThis);
