/**
 * Live-voice session policy.
 *
 * WebRTC and the DOM stay in app.js. This module decides when a dropped
 * peer should wait, reconnect once, or end — and whether live captions
 * are allowed to paint. Safe to require() from node tests.
 */
(function (global) {
  "use strict";

  /** One automatic new call after a terminal peer failure. Not a retry loop. */
  const RECONNECTS_ALLOWED = 1;

  function captionsVisible(value) {
    return value === "shown";
  }

  /**
   * @param {{
   *   connectionState?: string,
   *   iceConnectionState?: string,
   *   dataChannelState?: string,
   *   userEnded?: boolean,
   *   reconnectsUsed?: number,
   *   reconnectsAllowed?: number,
   * }} input
   * @returns {{ action: "keep" | "wait" | "reconnect" | "end", reason: string }}
   */
  function connectionAction(input) {
    const src = input || {};
    const connectionState = src.connectionState || "";
    const iceConnectionState = src.iceConnectionState || "";
    const dataChannelState = src.dataChannelState || "";
    const userEnded = Boolean(src.userEnded);
    const reconnectsUsed = Number(src.reconnectsUsed) || 0;
    const reconnectsAllowed =
      src.reconnectsAllowed == null ? RECONNECTS_ALLOWED : Number(src.reconnectsAllowed);

    if (userEnded) return { action: "end", reason: "user" };

    const dcDead =
      dataChannelState === "closed" &&
      (connectionState === "connected" || connectionState === "connecting");
    const terminal =
      connectionState === "failed" ||
      connectionState === "closed" ||
      iceConnectionState === "failed" ||
      iceConnectionState === "closed";
    const iceDown =
      connectionState === "disconnected" || iceConnectionState === "disconnected";

    if (connectionState === "connected" && !dcDead) {
      return { action: "keep", reason: "connected" };
    }
    // `disconnected` is the browser's own grace window. Do not invent a timer
    // and do not tear the call down — ICE often returns to `connected`.
    if (!terminal && iceDown) {
      return { action: "wait", reason: "ice_disconnected" };
    }
    if (terminal || dcDead) {
      if (reconnectsUsed < reconnectsAllowed) {
        return {
          action: "reconnect",
          reason: dcDead && !terminal ? "datachannel_closed" : "peer_failed",
        };
      }
      return { action: "end", reason: "reconnect_exhausted" };
    }
    if (
      iceConnectionState === "connected" ||
      iceConnectionState === "completed"
    ) {
      return { action: "keep", reason: "connected" };
    }
    return { action: "keep", reason: "in_progress" };
  }

  /**
   * Server sideband died out from under a call that had tools on Hearth.
   * The browser still looks "live" until we place a fresh call.
   */
  function shouldRecoverFromServer(input) {
    const src = input || {};
    if (!src.hasCall || src.userEnded || !src.sidebandOk) return false;
    const phase = src.phase || "idle";
    if (phase === "recovering" || phase === "connecting" || phase === "ending") return false;
    return src.voiceMode === "disconnected";
  }

  class VoiceLifecycle {
    constructor(opts) {
      const options = opts || {};
      this.reconnectsAllowed =
        options.reconnectsAllowed == null ? RECONNECTS_ALLOWED : options.reconnectsAllowed;
      this.epoch = 0;
      this.userEnded = false;
      this.reconnectsUsed = 0;
      this.phase = "idle";
    }

    beginUserStart() {
      this.epoch += 1;
      this.userEnded = false;
      this.reconnectsUsed = 0;
      this.phase = "connecting";
      return this.epoch;
    }

    beginReconnect() {
      if (this.userEnded) return null;
      if (this.phase === "recovering" || this.phase === "ending") return null;
      if (this.reconnectsUsed >= this.reconnectsAllowed) return null;
      this.reconnectsUsed += 1;
      this.epoch += 1;
      this.phase = "recovering";
      return this.epoch;
    }

    markLive(epoch) {
      if (epoch !== this.epoch || this.userEnded) return false;
      this.phase = "live";
      return true;
    }

    beginUserStop() {
      this.userEnded = true;
      this.epoch += 1;
      this.phase = "ending";
      return this.epoch;
    }

    markIdle() {
      // A newer start may already have replaced this ending generation.
      if (this.userEnded && this.phase === "ending") this.phase = "idle";
    }

    isCurrent(epoch) {
      return epoch === this.epoch && !this.userEnded;
    }

    onTransport(signal) {
      const src = signal || {};
      return connectionAction({
        connectionState: src.connectionState,
        iceConnectionState: src.iceConnectionState,
        dataChannelState: src.dataChannelState,
        userEnded: this.userEnded,
        reconnectsUsed: this.reconnectsUsed,
        reconnectsAllowed: this.reconnectsAllowed,
      });
    }
  }

  function mergeUserUtterance(prev, event, partial) {
    const current = prev && typeof prev === "object" ? prev : { final: "", partial: "" };
    const finalText = String(current.final || "");
    const partialText = String(current.partial || "");
    if (partial) {
      const delta = String((event && event.delta) || "");
      return { final: finalText, partial: `${partialText}${delta}`.slice(-800) };
    }
    const text = String((event && (event.transcript || event.text)) || "").trim();
    if (!text) return { final: finalText, partial: partialText };
    return { final: text.slice(0, 800), partial: "" };
  }

  function utteranceText(bucket) {
    if (!bucket) return "";
    const finalText = String(bucket.final || "").trim();
    if (finalText) return finalText;
    return String(bucket.partial || "").trim();
  }

  const api = {
    RECONNECTS_ALLOWED,
    captionsVisible,
    connectionAction,
    shouldRecoverFromServer,
    VoiceLifecycle,
    mergeUserUtterance,
    utteranceText,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  global.HearthVoiceSession = api;
})(typeof window !== "undefined" ? window : globalThis);
