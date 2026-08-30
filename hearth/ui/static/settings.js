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
      // Always on <html> so selectors like html[data-look="jarvis"] match.
      root.setAttribute(`data-${knob.id}`, value);
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

  function buildSpendSection(body) {
    const section = document.createElement("section");
    section.className = "settings-group spend-group";
    section.id = "openai-spend-section";

    const title = document.createElement("h3");
    title.className = "settings-group-title";
    title.textContent = "OpenAI spend";
    section.appendChild(title);

    const intro = document.createElement("p");
    intro.className = "spend-intro";
    intro.textContent =
      "Live org costs and token usage from OpenAI — never invented numbers. Keys stay on VAULT.";
    section.appendChild(intro);

    const status = document.createElement("p");
    status.className = "spend-status";
    status.id = "openai-spend-status";
    status.textContent = "Open Look to load.";
    section.appendChild(status);

    const content = document.createElement("div");
    content.className = "spend-content";
    content.id = "openai-spend-content";
    section.appendChild(content);

    const actions = document.createElement("div");
    actions.className = "spend-actions";
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "settings-choice spend-refresh";
    refresh.id = "openai-spend-refresh";
    refresh.textContent = "Refresh";
    refresh.addEventListener("click", () => loadSpend({ force: true }));
    actions.appendChild(refresh);
    section.appendChild(actions);

    body.appendChild(section);
  }

  function formatUsd(value, currency) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const cur = (currency || "usd").toUpperCase();
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: cur,
        maximumFractionDigits: 4,
      }).format(Number(value));
    } catch (_) {
      return `${Number(value).toFixed(4)} ${cur}`;
    }
  }

  function formatTokens(n) {
    if (n == null) return "—";
    return Number(n).toLocaleString();
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function renderSpend(payload) {
    const status = document.getElementById("openai-spend-status");
    const content = document.getElementById("openai-spend-content");
    if (!status || !content) return;
    content.replaceChildren();

    const mode = payload.mode || "unavailable";
    const modeLabels = {
      openai_billed: "OpenAI billed costs available",
      openai_usage_only: "OpenAI usage available (costs unavailable)",
      local_estimate_only: "Local measured estimates only",
      unavailable: "Spend data unavailable",
    };
    status.textContent = modeLabels[mode] || mode;
    status.dataset.mode = mode;

    const meta = el("p", "spend-meta");
    meta.textContent =
      `Project key: ${payload.openai_project_key_configured ? "set" : "missing"} · ` +
      `Admin key: ${payload.openai_admin_key_configured ? "set" : "missing"} · ` +
      `Window: ${payload.days || 30}d`;
    content.appendChild(meta);

    const costs = payload.costs || {};
    const costCard = el("div", "spend-card");
    costCard.appendChild(el("p", "spend-card-title", "Organization costs"));
    if (costs.available) {
      const summary = costs.summary || {};
      costCard.appendChild(
        el(
          "p",
          "spend-metric",
          `${formatUsd(summary.total, summary.currency)} · last ${payload.days || 30} days`
        )
      );
      costCard.appendChild(
        el("p", "spend-note", costs.label || "OpenAI organization costs")
      );
      const lines = summary.by_line_item || [];
      if (lines.length) {
        const list = el("ul", "spend-list");
        for (const row of lines.slice(0, 8)) {
          list.appendChild(
            el(
              "li",
              null,
              `${row.line_item}: ${formatUsd(row.amount, summary.currency)}`
            )
          );
        }
        costCard.appendChild(list);
      }
    } else {
      costCard.classList.add("is-unavailable");
      costCard.appendChild(
        el(
          "p",
          "spend-unavailable",
          costs.message || "Costs unavailable — Admin API key required."
        )
      );
      if (costs.requires_admin_key) {
        costCard.appendChild(
          el(
            "p",
            "spend-note",
            "Create OPENAI_ADMIN_KEY at platform.openai.com → Organization → Admin keys."
          )
        );
      }
    }
    content.appendChild(costCard);

    const usage = payload.usage || {};
    const usageCard = el("div", "spend-card");
    usageCard.appendChild(el("p", "spend-card-title", "Organization usage (tokens)"));
    if (usage.available) {
      const totals = (usage.summary && usage.summary.totals) || {};
      usageCard.appendChild(
        el(
          "p",
          "spend-metric",
          `In ${formatTokens(totals.input_tokens)} · Out ${formatTokens(totals.output_tokens)} · Requests ${formatTokens(totals.num_model_requests)}`
        )
      );
      usageCard.appendChild(
        el("p", "spend-note", usage.label || "OpenAI organization usage")
      );
      const models = (usage.summary && usage.summary.by_model) || [];
      if (models.length) {
        const list = el("ul", "spend-list");
        for (const row of models.slice(0, 8)) {
          list.appendChild(
            el(
              "li",
              null,
              `${row.model}: ${formatTokens(row.input_tokens)} in / ${formatTokens(row.output_tokens)} out`
            )
          );
        }
        usageCard.appendChild(list);
      }
    } else {
      usageCard.classList.add("is-unavailable");
      usageCard.appendChild(
        el(
          "p",
          "spend-unavailable",
          usage.message || "Usage unavailable — Admin API key required."
        )
      );
    }
    content.appendChild(usageCard);

    const local = payload.local || {};
    const localCard = el("div", "spend-card");
    localCard.appendChild(el("p", "spend-card-title", "Hearth local ledger"));
    const localTotals = local.totals || {};
    if (localTotals.total_tokens) {
      localCard.appendChild(
        el(
          "p",
          "spend-metric",
          `Measured ${formatTokens(localTotals.total_tokens)} tokens · ${formatTokens(localTotals.requests)} calls`
        )
      );
      const est = local.list_price_estimate || {};
      if (est.available && est.estimated_usd != null) {
        localCard.appendChild(
          el(
            "p",
            "spend-metric soft",
            `List-price estimate ${formatUsd(est.estimated_usd, "usd")}`
          )
        );
      }
      localCard.appendChild(
        el(
          "p",
          "spend-note",
          "Local estimate from measured tokens × official list pricing — not OpenAI-billed."
        )
      );
    } else {
      localCard.appendChild(
        el(
          "p",
          "spend-unavailable",
          "No local measured tokens yet. Counts appear after Hearth’s own OpenAI calls return usage."
        )
      );
    }
    content.appendChild(localCard);

    const pricing = payload.list_pricing || {};
    const priceCard = el("div", "spend-card");
    priceCard.appendChild(el("p", "spend-card-title", "Official list pricing"));
    priceCard.appendChild(
      el(
        "p",
        "spend-note",
        `${pricing.label || "official list pricing (not your invoice)"} · as of ${pricing.as_of || "—"}`
      )
    );
    const models = pricing.models || [];
    if (models.length) {
      const list = el("ul", "spend-list");
      for (const row of models) {
        let line = row.id;
        if (row.input_per_1m != null) {
          line += ` · in $${row.input_per_1m}/1M`;
        }
        if (row.output_per_1m != null) {
          line += ` · out $${row.output_per_1m}/1M`;
        }
        if (row.audio_input_per_1m != null) {
          line += ` · audio in $${row.audio_input_per_1m}/1M`;
        }
        list.appendChild(el("li", null, line));
      }
      priceCard.appendChild(list);
    }
    if (pricing.source) {
      const link = el("a", "spend-link", "OpenAI pricing docs");
      link.href = pricing.source;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      priceCard.appendChild(link);
    }
    content.appendChild(priceCard);

    const guidance = payload.guidance || [];
    if (guidance.length) {
      const guide = el("div", "spend-guidance");
      for (const tip of guidance) {
        guide.appendChild(el("p", "spend-note", tip));
      }
      content.appendChild(guide);
    }
  }

  let spendFetcher = null;
  let spendLoaded = false;
  let spendLoading = false;

  function setSpendFetcher(fn) {
    spendFetcher = typeof fn === "function" ? fn : null;
  }

  async function loadSpend({ force = false } = {}) {
    const status = document.getElementById("openai-spend-status");
    if (!spendFetcher) {
      if (status) status.textContent = "Spend monitor not wired.";
      return;
    }
    if (spendLoading) return;
    if (spendLoaded && !force) return;
    spendLoading = true;
    if (status) status.textContent = "Loading OpenAI spend…";
    try {
      const payload = await spendFetcher();
      spendLoaded = true;
      renderSpend(payload || {});
    } catch (err) {
      if (status) {
        status.textContent = "Could not load spend data.";
        status.dataset.mode = "unavailable";
      }
      const content = document.getElementById("openai-spend-content");
      if (content) {
        content.replaceChildren();
        content.appendChild(
          el(
            "p",
            "spend-unavailable",
            err && err.message ? err.message : "Request failed. Try again when logged in."
          )
        );
      }
    } finally {
      spendLoading = false;
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
    buildSpendSection(body);
  }

  function openPanel() {
    const sheet = document.getElementById("settings-sheet");
    if (!sheet) return;
    sheet.classList.remove("hidden");
    sheet.setAttribute("aria-hidden", "false");
    document.body.classList.add("settings-open");
    const close = document.getElementById("settings-close");
    if (close) close.focus();
    loadSpend();
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
    setSpendFetcher,
    loadSpend,
  };
})(window);
