(function () {
  const root = document.documentElement;

  function isTyping() {
    const el = document.activeElement;
    if (!el) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA";
  }

  function keyboardInset() {
    if (!isTyping()) return 0;
    const viewport = window.visualViewport;
    if (!viewport) return 0;
    return Math.max(0, window.innerHeight - (viewport.height + viewport.offsetTop));
  }

  /*
   * iOS Safari / Home Screen PWAs often keep 100svh/100dvh (and sometimes
   * innerHeight) stale across orientationchange until a later resize. The
   * orb-first fold is keyed off --phone-fold so the listening sphere stays
   * a full screen tall (under the fixed Look chrome) as soon as the layout
   * viewport updates.
   */
  function syncPhoneFold() {
    if (isTyping()) return;
    const height = Math.round(window.innerHeight);
    if (height > 0) root.style.setProperty("--phone-fold", `${height}px`);
  }

  function applyPhoneInsets() {
    syncPhoneFold();
    const keyboard = keyboardInset();
    root.style.setProperty("--keyboard-inset", `${keyboard}px`);
    root.style.setProperty(
      "--dock-safe-bottom",
      keyboard > 0 ? "0px" : "env(safe-area-inset-bottom, 0px)"
    );
    const dock = document.querySelector(".composer-dock");
    if (!dock) return;
    const height = Math.ceil(dock.getBoundingClientRect().height);
    if (height > 0) root.style.setProperty("--dock-space", `${height}px`);
  }

  /* orientationchange fires before iOS swaps the layout viewport — paint twice. */
  function afterOrientation() {
    requestAnimationFrame(() => {
      applyPhoneInsets();
      requestAnimationFrame(applyPhoneInsets);
    });
  }

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", applyPhoneInsets);
    window.visualViewport.addEventListener("scroll", applyPhoneInsets);
  }
  window.addEventListener("orientationchange", afterOrientation);
  if (window.screen && screen.orientation && typeof screen.orientation.addEventListener === "function") {
    screen.orientation.addEventListener("change", afterOrientation);
  }
  window.addEventListener("focusin", applyPhoneInsets);
  window.addEventListener("focusout", applyPhoneInsets);
  window.addEventListener("resize", applyPhoneInsets);
  window.addEventListener("pageshow", applyPhoneInsets);

  const dock = document.querySelector(".composer-dock");
  if (dock && typeof ResizeObserver === "function") {
    new ResizeObserver(applyPhoneInsets).observe(dock);
  }

  applyPhoneInsets();

  if (!("serviceWorker" in navigator)) return;
  const secure =
    location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1";
  if (!secure) return;
  navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
