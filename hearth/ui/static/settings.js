/**
 * Client-only look/feel knobs for Hearth.
 * Extensible: add entries to KNOBS — the panel renders them automatically.
 * Defaults match the current shipping UI; nothing is sent to the server.
 */
(function (global) {
  const STORAGE_KEY = "hearth.look.v1";

  /** @typedef {{ value: string, label: string }} Choice */
  /** @typedef {{ id: string, label: string, group: string, type: 'choice', default: string, options: Choice[] }} Knob */

  /** @type {Knob[]} */
  const KNOBS = [
    {
      id: "look",
      label: "Style",
      group: "Look",
      type: "choice",
      default: "hearth",
      options: [
        { value: "hearth", label: "Hearth" },
        { value: "jarvis", label: "Jarvis" },
        { value: "forge", label: "Forge" },
      ],
    },
    {
      id: "theme",
      label: "Palette",
      group: "Look",
      type: "choice",
      default: "ember",
      options: [
        { value: "ember", label: "Ember" },
        { value: "ash", label: "Ash" },
        { value: "moss", label: "Moss" },
        { value: "ink", label: "Ink" },
      ],
    },
    {
      id: "atmosphere",
      label: "Atmosphere",
      group: "Look",
      type: "choice",
      default: "warm",
      options: [
        { value: "calm", label: "Calm" },
        { value: "warm", label: "Warm" },
        { value: "vivid", label: "Vivid" },
      ],
    },
    {
      id: "density",
      label: "Density",
      group: "Feel",
      type: "choice",
      default: "cozy",
      options: [
        { value: "compact", label: "Compact" },
        { value: "cozy", label: "Cozy" },
        { value: "roomy", label: "Roomy" },
      ],
    },
    {
      id: "orb",
      label: "Orb",
      group: "Feel",
      type: "choice",
      default: "default",
      options: [
        { value: "small", label: "Small" },
        { value: "default", label: "Default" },
        { value: "large", label: "Large" },
      ],
    },
    {
      id: "motion",
      label: "Motion",
      group: "Feel",
      type: "choice",
      default: "alive",
      options: [
        { value: "still", label: "Still" },
        { value: "alive", label: "Alive" },
      ],
    },
  ];

  const THEME_COLORS = {
    ember: "#070604",
    ash: "#0a0b0c",
    moss: "#060806",
    ink: "#05060a",
  };

  const LOOK_COLORS = {
    hearth: null,
    jarvis: "#03080f",
    forge: "#0b0908",
  };

  function defaults() {
    const out = {};
    for (const knob of KNOBS) out[knob.id] = knob.default;
    return out;
  }

  function readStore() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function writeStore(values) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(values));
    } catch (_) {
      /* private mode / quota — still apply in-session */
    }
  }

  function sanitize(partial) {
    const base = defaults();
    const next = { ...base };
    for (const knob of KNOBS) {
      const value = partial[knob.id];
      if (value == null) continue;
      if (knob.type === "choice" && knob.options.some((o) => o.value === value)) {
        next[knob.id] = value;
      }
    }
    return next;
  }

  function apply(values) {
    const root = document.documentElement;
    for (const knob of KNOBS) {
      const value = values[knob.id] ?? knob.default;
      root.dataset[knob.id] = value;
    }
    const look = values.look || "hearth";
    const theme = values.theme || "ember";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      const fromLook = LOOK_COLORS[look];
      meta.setAttribute("content", fromLook || THEME_COLORS[theme] || THEME_COLORS.ember);
    }
  }

  let current = sanitize({ ...defaults(), ...readStore() });
  apply(current);

  function get() {
    return { ...current };
  }

  function set(partial) {
    current = sanitize({ ...current, ...partial });
    writeStore(current);
    apply(current);
    syncPanel();
    return get();
  }

  function reset() {
    current = defaults();
    writeStore(current);
    apply(current);
    syncPanel();
    return get();
  }

  function groupKnobs() {
    const groups = [];
    const seen = new Map();
    for (const knob of KNOBS) {
      if (!seen.has(knob.group)) {
        const list = [];
        seen.set(knob.group, list);
        groups.push({ name: knob.group, knobs: list });
      }
      seen.get(knob.group).push(knob);
    }
    return groups;
  }

  function syncPanel() {
    const panel = document.getElementById("settings-panel");
    if (!panel) return;
    for (const knob of KNOBS) {
      const value = current[knob.id];
      panel.querySelectorAll(`[data-knob="${knob.id}"]`).forEach((btn) => {
        const on = btn.getAttribute("data-value") === value;
        btn.classList.toggle("is-on", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
  }

  function buildPanelBody(body) {
    body.replaceChildren();
    for (const group of groupKnobs()) {
      const section = document.createElement("section");
      section.className = "settings-group";
      const title = document.createElement("h3");
      title.className = "settings-group-title";
      title.textContent = group.name;
      section.appendChild(title);

      for (const knob of group.knobs) {
        const row = document.createElement("div");
        row.className = "settings-row";
        const label = document.createElement("p");
        label.className = "settings-label";
        label.textContent = knob.label;
        row.appendChild(label);

        const choices = document.createElement("div");
        choices.className = "settings-choices";
        choices.setAttribute("role", "group");
        choices.setAttribute("aria-label", knob.label);
        for (const option of knob.options) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "settings-choice";
          btn.dataset.knob = knob.id;
          btn.dataset.value = option.value;
          btn.textContent = option.label;
          btn.setAttribute("aria-pressed", "false");
          btn.addEventListener("click", () => set({ [knob.id]: option.value }));
          choices.appendChild(btn);
        }
        row.appendChild(choices);
        section.appendChild(row);
      }
      body.appendChild(section);
    }
  }

  function openPanel() {
    const sheet = document.getElementById("settings-sheet");
    if (!sheet) return;
    sheet.classList.remove("hidden");
    sheet.setAttribute("aria-hidden", "false");
    document.body.classList.add("settings-open");
    const close = document.getElementById("settings-close");
    if (close) close.focus();
  }

  function closePanel() {
    const sheet = document.getElementById("settings-sheet");
    if (!sheet) return;
    sheet.classList.add("hidden");
    sheet.setAttribute("aria-hidden", "true");
    document.body.classList.remove("settings-open");
    const open = document.getElementById("settings-btn");
    if (open) open.focus();
  }

  function isOpen() {
    const sheet = document.getElementById("settings-sheet");
    return Boolean(sheet && !sheet.classList.contains("hidden"));
  }

  function mount() {
    const body = document.getElementById("settings-body");
    if (body) buildPanelBody(body);
    syncPanel();

    const openBtn = document.getElementById("settings-btn");
    const closeBtn = document.getElementById("settings-close");
    const resetBtn = document.getElementById("settings-reset");
    const backdrop = document.getElementById("settings-backdrop");

    if (openBtn) openBtn.addEventListener("click", openPanel);
    if (closeBtn) closeBtn.addEventListener("click", closePanel);
    if (resetBtn) resetBtn.addEventListener("click", () => reset());
    if (backdrop) backdrop.addEventListener("click", closePanel);

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && isOpen()) {
        ev.preventDefault();
        closePanel();
      }
    });
  }

  global.HearthSettings = {
    KNOBS,
    STORAGE_KEY,
    defaults,
    get,
    set,
    reset,
    apply,
    mount,
    open: openPanel,
    close: closePanel,
  };
})(window);
