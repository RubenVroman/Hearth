(function () {
  const root = document.documentElement;

  const viewport = window.visualViewport;
  const header = document.querySelector(".top");
  const controls = document.querySelector(".interaction-dock");
  const composer = document.querySelector(".composer-dock");
  let frame = 0;
  let orientationFrames = 0;
  let orientationTimer = 0;
  let layoutWidth = window.innerWidth;
  let unoccludedHeight = window.innerHeight;

  function isTyping() {
    const el = document.activeElement;
    if (!el || el.disabled || el.readOnly) return false;
    if (el.isContentEditable || el.tagName === "TEXTAREA") return true;
    return el.tagName === "INPUT" && !/^(button|checkbox|color|file|hidden|image|radio|range|reset|submit)$/i.test(el.type || "text");
  }

  function setProperty(name, value) {
    // Unchanged ResizeObserver measurements must not trigger a layout loop.
    if (root.style.getPropertyValue(name) !== value) root.style.setProperty(name, value);
  }

  function measure(element, property) {
    if (!element) return;
    const height = Math.ceil(element.getBoundingClientRect().height);
    if (Number.isFinite(height)) setProperty(property, `${Math.max(0, height)}px`);
  }

  function applyPhoneInsets() {
    const layoutHeight = window.innerHeight || root.clientHeight;
    const height = viewport && viewport.height > 0 ? viewport.height : layoutHeight;
    const top = viewport ? Math.max(0, viewport.offsetTop || 0) : 0;
    if (!(height > 0)) return;

    // Width changes invalidate the portrait keyboard baseline. Android may
    // resize innerHeight with the keyboard; iOS resizes only visualViewport.
    if (layoutWidth !== window.innerWidth) {
      layoutWidth = window.innerWidth;
      unoccludedHeight = layoutHeight;
    }
    const typing = isTyping();
    if (!typing) unoccludedHeight = layoutHeight;
    const unzoomed = !viewport || Math.abs((viewport.scale || 1) - 1) < 0.05;
    const occlusion = typing && unzoomed
      ? Math.max(0, Math.max(unoccludedHeight, layoutHeight) - height - top)
      : 0;
    // Browser chrome and fractional viewport changes are not a keyboard.
    const keyboard = occlusion > 80 ? Math.round(occlusion) : 0;

    // --app-height already excludes the keyboard. Never subtract its inset
    // again when positioning content inside this viewport.
    setProperty("--app-height", `${Math.round(height)}px`);
    setProperty("--viewport-top", `${Math.round(top)}px`);
    setProperty("--keyboard-inset", `${keyboard}px`);
    setProperty("--dock-safe-bottom", keyboard ? "0px" : "env(safe-area-inset-bottom, 0px)");
    const keyboardState = keyboard ? "true" : "false";
    if (root.dataset.keyboard !== keyboardState) root.dataset.keyboard = keyboardState;

    // Compatibility for the login screen and cached shell styles.
    syncPhoneFold(height);
    measure(header, "--header-height");
    measure(controls || composer, "--controls-height");
    measure(composer, "--dock-space");
  }

  function syncPhoneFold(height) {
    setProperty("--phone-fold", `${Math.round(height)}px`);
  }

  function scheduleInsets() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      applyPhoneInsets();
      if (orientationFrames > 0) {
        orientationFrames -= 1;
        scheduleInsets();
      }
    });
  }

  function afterOrientation() {
    // Safari can report the old viewport for the first rotation frame.
    unoccludedHeight = window.innerHeight;
    orientationFrames = 2;
    scheduleInsets();
    clearTimeout(orientationTimer);
    orientationTimer = setTimeout(scheduleInsets, 250);
  }

  if (viewport) {
    viewport.addEventListener("resize", scheduleInsets, { passive: true });
    viewport.addEventListener("scroll", scheduleInsets, { passive: true });
  }
  window.addEventListener("orientationchange", afterOrientation);
  if (window.screen && window.screen.orientation && typeof window.screen.orientation.addEventListener === "function") {
    window.screen.orientation.addEventListener("change", afterOrientation);
  }
  for (const event of ["focusin", "focusout", "resize", "pageshow"]) {
    window.addEventListener(event, scheduleInsets, { passive: true });
  }

  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(scheduleInsets);
    for (const element of new Set([header, controls, composer])) {
      if (element) observer.observe(element);
    }
  }
  applyPhoneInsets();

  if (!("serviceWorker" in navigator)) return;
  const secure =
    location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1";
  if (!secure) return;
  navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
