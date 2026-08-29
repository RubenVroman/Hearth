(function () {
  const root = document.documentElement;

  function applyKeyboardInset() {
    const viewport = window.visualViewport;
    if (!viewport) return;
    const inset = Math.max(0, window.innerHeight - (viewport.height + viewport.offsetTop));
    root.style.setProperty("--keyboard-inset", `${inset}px`);
  }

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", applyKeyboardInset);
    window.visualViewport.addEventListener("scroll", applyKeyboardInset);
  }
  window.addEventListener("orientationchange", applyKeyboardInset);
  window.addEventListener("focusin", applyKeyboardInset);
  window.addEventListener("focusout", applyKeyboardInset);
  applyKeyboardInset();

  if (!("serviceWorker" in navigator)) return;
  const secure =
    location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1";
  if (!secure) return;
  navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
