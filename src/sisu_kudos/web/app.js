(() => {
  "use strict";
  const $ = (s) => document.querySelector(s),
    $$ = (s) => [...document.querySelectorAll(s)],
    NS = "http://www.w3.org/2000/svg",
    LOG_ACK_KEY = "sisu-kudos.log-error-ack.v2",
    t = KudosI18n.t,
    hostEntries = KudosHosts.hostEntries,
    hostFor = KudosHosts.hostFor,
    hasModelTelemetry = KudosHosts.hasModelTelemetry,
    featureEnabled = KudosHosts.featureEnabled,
    knownText = KudosI18n.knownProductText;
  const hostViews = new Map(),
    workRows = new Map(),
    agentResourceRows = new Map(),
    RESOURCE_FETCH_TIMEOUT_MS = 5000,
    AGENT_RESOURCE_STALE_AFTER_SECONDS = 60;
  const ui = {
    data: null,
    agentResources: null,
    agentResourcesReceivedAt: 0,
    agentResourcesError: "",
    agentResourceWarningsKey: null,
    agentResourcesPending: false,
    logLevel: "all",
    logSource: "all",
    logRuntime: "all",
    logStatus: "all",
    logFetchPending: false,
    hostInspector: null,
    eventKind: "all",
    lastLogKey: "",
    lastEventKey: "",
    lastWorkKey: "",
    lastStorageKey: "",
    lastServiceKey: "",
    stream: null,
    retry: 2e3,
    reconnectTimer: 0,
  };
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== void 0) n.textContent = text;
    return n;
  }
  function sv(tag, attrs = {}, text = "") {
    const n = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, String(v)));
    if (text) n.textContent = text;
    return n;
  }
  KudosI18n.applyStatic(document);
  function invalidateTranslatedViews() {
    ui.lastStorageKey = "";
    ui.lastServiceKey = "";
    ui.lastLogKey = "";
    ui.lastEventKey = "";
  }
  const languageSelect = $("#language-select");
  languageSelect.value = KudosI18n.language();
  languageSelect.addEventListener("change", () => {
    KudosI18n.setLanguage(languageSelect.value, { explicit: true });
    KudosI18n.applyStatic(document);
    invalidateTranslatedViews();
    if (ui.data) render(ui.data);
  });
  const fmt = (value, options = {}) => KudosI18n.formatNumber(value, options);
  const num = (v) =>
      v === null || v === void 0 || v === "" || !Number.isFinite(Number(v))
        ? null
        : Number(v),
    clamp = (v) => Math.max(0, Math.min(100, num(v) || 0));
  const pct = (v) => KudosI18n.formatPercent(v),
    temp = (v) => (num(v) === null ? "\u2013" : `${fmt(Math.round(num(v)), { maximumFractionDigits: 0 })} \xB0C`),
    gb = (v) => (num(v) === null ? "\u2013" : `${fmt(num(v), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${t("units.gib")}`);
  const clip = (v, n = 90) =>
    String(v || "\u2013").length > n
      ? `${String(v).slice(0, n - 1)}\u2026`
      : String(v || "\u2013");
  function instant(v) {
    const numeric =
      typeof v === "number"
        ? v
        : /^\d+(?:\.\d+)?$/.test(String(v))
          ? Number(v)
          : null;
    return new Date(
      numeric === null ? v : Math.abs(numeric) < 1e12 ? numeric * 1e3 : numeric,
    );
  }
  function clock(v, seconds = true) {
    if (!v) return "\u2013";
    const d = instant(v);
    if (Number.isNaN(d.valueOf())) return "\u2013";
    return new Intl.DateTimeFormat(
      KudosI18n.locale(),
      seconds
        ? { hour: "2-digit", minute: "2-digit", second: "2-digit" }
        : {
            day: "numeric",
            month: "numeric",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          },
    ).format(d);
  }
  function duration(s) {
    const n = num(s);
    if (n === null) return "\u2013";
    if (n < 60) return t("number.seconds", { count: fmt(Math.round(n), { maximumFractionDigits: 0 }) });
    if (n < 3600) return t("number.minutes", { count: fmt(Math.round(n / 60), { maximumFractionDigits: 0 }) });
    return t("number.hoursMinutes", {
      hours: fmt(Math.floor(n / 3600), { maximumFractionDigits: 0 }),
      minutes: fmt(Math.round((n % 3600) / 60), { maximumFractionDigits: 0 }),
    });
  }
  function rate(v) {
    const n = num(v);
    if (n === null) return "\u2013";
    if (n >= 1024 ** 3) return `${fmt(n / 1024 ** 3, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${t("units.gib")}/s`;
    if (n >= 1024 ** 2) return `${fmt(n / 1024 ** 2, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${t("units.mib")}/s`;
    if (n >= 1024) return `${fmt(n / 1024, { maximumFractionDigits: 0 })} ${t("units.kib")}/s`;
    return `${fmt(Math.round(n), { maximumFractionDigits: 0 })} ${t("units.bytes")}/s`;
  }
  function capacity(v) {
    const n = num(v);
    if (n === null) return "\u2013";
    return n >= 1024
      ? `${fmt(n / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${t("units.tib")}`
      : `${fmt(Math.round(n), { maximumFractionDigits: 0 })} ${t("units.gib")}`;
  }
  function bytes(v) {
    const n = num(v);
    if (n === null) return "\u2013";
    if (n >= 1024 ** 4) return `${fmt(n / 1024 ** 4, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${t("units.tib")}`;
    if (n >= 1024 ** 3) return `${fmt(n / 1024 ** 3, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${t("units.gib")}`;
    if (n >= 1024 ** 2) return `${fmt(n / 1024 ** 2, { maximumFractionDigits: 0 })} ${t("units.mib")}`;
    if (n >= 1024) return `${fmt(n / 1024, { maximumFractionDigits: 0 })} ${t("units.kib")}`;
    return `${fmt(Math.round(n), { maximumFractionDigits: 0 })} ${t("units.bytes")}`;
  }
  function band(v, warn = 75, crit = 90) {
    return KudosTelemetry.usageBand(v, warn, crit);
  }
  function tempBand(v) {
    const n = num(v);
    return n === null ? "unknown" : n >= 85 ? "hot" : n >= 70 ? "warm" : "safe";
  }
  function initials(name) {
    return String(name || "?")
      .split(/\s+/)
      .map((x) => x[0])
      .join("")
      .slice(0, 2)
      .toUpperCase();
  }
  function setText(sel, text) {
    const n = $(sel);
    if (n && n.textContent !== String(text)) n.textContent = String(text);
  }
  setInterval(
    () =>
      setText(
        "#clock",
        new Intl.DateTimeFormat(KudosI18n.locale(), {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }).format(new Date()),
      ),
    1e3,
  );
  function setConnection(mode, key) {
    ui.connectionMode = mode;
    ui.connectionTextKey = key;
    document.documentElement.dataset.activityConnection = mode;
    const n = $("#connection");
    n.className = `connection ${mode}`;
    const label = n.querySelector("span");
    if (label) label.textContent = t(key);
    else n.textContent = t(key);
  }
  function quota(key) {
    return ui.data?.agents?.quotas?.[key] || {};
  }
  function quotaWindow(q, id, durationMinutes) {
    const windows = Array.isArray(q?.windows) ? q.windows : [];
    return windows.find((w) => w?.id === id) ||
      windows.find((w) => num(w?.duration_minutes) === durationMinutes) || null;
  }
  function quotaWindowLabel(providerId, window) {
    const known = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "seven_day_oauth_apps"];
    return providerId === "claude" && known.includes(window.id)
      ? t(`quota.window.${window.id}`)
      : window.label || t("inspector.window");
  }
  function quotaStatusText(q, hasData) {
    const stateKey = {
      available: null,
      rate_limited: "quota.status.rateLimited",
      rate_limit: "quota.status.rateLimited",
      auth: "quota.status.loginRequired",
      login_required: "quota.status.loginRequired",
      unavailable: "quota.status.unavailable",
      invalid_response: "quota.status.invalidResponse",
    }[q?.state];
    let status = stateKey
      ? t(stateKey)
      : q?.state === "available"
        ? hasData ? q.status || t("quota.status.available") : t("quota.noData")
        : q?.available === false
          ? t("quota.status.unavailable")
          : hasData
            ? q?.status || t("quota.status.available")
            : t("quota.status.unavailable");
    if (q?.stale) status = `${status} · ${t("quota.status.stale")}`;
    return status;
  }
  function renderCapacity() {
    const enabled = featureEnabled(ui.data, "quotas"),
      control = $("#quota-control"),
      claudeEnabled = featureEnabled(ui.data, "claude_quota"),
      claudeControl = $("#claude-quota-control");
    control.hidden = !enabled;
    claudeControl.hidden = !claudeEnabled;
    if (enabled) {
      const q = quota("chatgpt"),
        w = q.windows?.[0],
        remaining = num(w?.remaining_percent);
      setText(
        "#chatgpt-value",
        w
          ? remaining === null
            ? "\u2013"
            : `${fmt(Math.round(remaining), { maximumFractionDigits: 0 })} %`
          : q.available
            ? "\u2013"
            : t("quota.noData"),
      );
      const chatgptNote = w
        ? `${t("quota.resetAt", { time: clock(w.resets_at, false) })}${q.resets_available == null ? "" : ` · ${t("quota.resets", { count: fmt(q.resets_available, { maximumFractionDigits: 0 }) })}`}`
        : q.status || t("quota.unavailable");
      setText("#chatgpt-note", `${chatgptNote}${q.stale ? ` · ${t("quota.status.stale")}` : ""}`);
      $("#chatgpt-life").style.width = `${remaining === null ? 0 : clamp(remaining)}%`;
      $("#chatgpt-life").parentElement.classList.toggle("unknown", remaining === null);
    }
    if (claudeEnabled) {
      const q = quota("claude"),
        fiveHour = quotaWindow(q, "five_hour", 300),
        sevenDay = quotaWindow(q, "seven_day", 10080),
        windows = [fiveHour, sevenDay],
        remaining = windows.map((w) => num(w?.remaining_percent)),
        hasData = windows.some(Boolean),
        value = windows.map((w, index) => {
          const percent = num(w?.remaining_percent);
          return `${t(index === 0 ? "quota.fiveHourShort" : "quota.sevenDayShort")} ${percent === null ? "–" : `${fmt(Math.round(percent), { maximumFractionDigits: 0 })}%`}`;
        }).join(" · ");
      setText("#claude-value", hasData ? value : "–");
      const resets = hasData
        ? t("quota.claudeResetNotes", {
            fiveHour: fiveHour ? clock(fiveHour.resets_at, false) : "–",
            sevenDay: sevenDay ? clock(sevenDay.resets_at, false) : "–",
          })
        : "";
      const stateNote = !hasData || q.state !== "available" || q.available === false || q.stale
        ? quotaStatusText(q, hasData)
        : "";
      const note = [resets, stateNote].filter(Boolean).join(" · ") || t("quota.status.available");
      setText("#claude-note", note);
      ["#claude-life", "#claude-week-life"].forEach((selector, index) => {
        const life = $(selector),
          track = life.parentElement;
        life.style.width = `${remaining[index] === null ? 0 : clamp(remaining[index])}%`;
        track.classList.toggle("unknown", remaining[index] === null);
      });
      claudeControl.title = t("quota.remainingTitle");
      claudeControl.setAttribute("aria-label", t("quota.claudeAriaValues", {
        fiveHour: remaining[0] === null ? "–" : fmt(Math.round(remaining[0]), { maximumFractionDigits: 0 }),
        sevenDay: remaining[1] === null ? "–" : fmt(Math.round(remaining[1]), { maximumFractionDigits: 0 }),
        status: note,
      }));
    }
    const readings = $("#power-readings");
    readings.replaceChildren(...hostEntries(ui.data).map(([id, host]) => {
      const value = num(host.system?.gpu?.power_watts),
        stale = host.health?.stale || host.health?.ok === false,
        row = el("span"),
        reading = el("b", "", value === null ? "–" : `${fmt(value, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} W${stale ? " *" : ""}`);
      row.append(document.createTextNode(`${host.label || id} `), reading);
      return row;
    }));
    setText("#power-note", t("power.note"));
  }
  function logErrors() {
    if (!featureEnabled(ui.data, "logs")) return [];
    return (ui.data?.logs?.records || []).filter(
      (x) =>
        ["ERROR", "CRITICAL"].includes(x.level) &&
        x.status !== "recovered",
    );
  }
  function logSignature(x) {
    return [
      x.timestamp,
      x.runtime,
      x.agent,
      x.source,
      x.level,
      x.component,
      x.message,
    ].join("|");
  }
  function readLogAck() {
    try {
      const value = JSON.parse(localStorage.getItem(LOG_ACK_KEY) || "null");
      return value &&
        typeof value.timestamp === "string" &&
        Number.isFinite(Date.parse(value.timestamp)) &&
        Array.isArray(value.signatures) &&
        (value.ids === void 0 || Array.isArray(value.ids))
        ? value
        : null;
    } catch (_error) {
      return null;
    }
  }
  function isLogErrorUnread(x, ack) {
    if (!ack) return true;
    const timestamp = Date.parse(x.timestamp),
      cutoff = Date.parse(ack.timestamp);
    if (x.id != null && Array.isArray(ack.ids)) {
      if (ack.ids.includes(String(x.id))) return false;
      if (timestamp < cutoff) return false;
      return true;
    }
    return timestamp > cutoff ||
      (timestamp === cutoff && !ack.signatures.includes(logSignature(x)));
  }
  function unreadLogErrors() {
    const rows = logErrors(),
      ack = readLogAck();
    return rows.filter((x) => isLogErrorUnread(x, ack)).length;
  }
  function acknowledgeLogErrors() {
    const rows = logErrors();
    if (!rows.length) return;
    const timestamp = rows.reduce(
      (latest, x) =>
        Date.parse(x.timestamp) > (Date.parse(latest) || 0)
          ? String(x.timestamp)
          : latest,
      "",
    );
    const signatures = rows
      .filter((x) => Date.parse(x.timestamp) === Date.parse(timestamp))
      .map(logSignature);
    const ids = rows.filter((x) => x.id != null).map((x) => String(x.id));
    try {
      localStorage.setItem(
        LOG_ACK_KEY,
        JSON.stringify({ timestamp, signatures, ids }),
      );
    } catch (_error) {
      setText("#sr-announcer", t("attention.ackSaveFailed"));
      return;
    }
    renderAttention();
    setText(
      "#sr-announcer",
      t("attention.acknowledgedLive", { count: fmt(rows.length, { maximumFractionDigits: 0 }) }),
    );
  }
  const LIVE_AGENT_ERROR_SECONDS = 120;
  function isLiveSession(s) {
    return (
      ["running", "tool", "waiting"].includes(s.status) ||
      (s.status === "error" &&
        (num(s.age_seconds) ?? Infinity) <= LIVE_AGENT_ERROR_SECONDS)
    );
  }
  function alerts() {
    const d = ui.data || {},
      health = Object.values(d.collector_health || {}),
      sessions = d.agents?.sessions || [],
      logs = featureEnabled(d, "logs") ? d.logs?.counts || {} : {},
      hostStates = hostEntries(d).map(([, host]) => host),
      hostFailures = hostStates.filter((host) => !host.health?.ok || host.health?.stale).length,
      gpuStates = hostStates
        .map((host) => String(host.system?.gpu?.health_status ?? host.system?.gpu?.health ?? "").toLowerCase())
        .filter(Boolean),
      sessionErrors = sessions.filter((session) => session.status === "error" && isLiveSession(session));
    return {
      collectors:
        health.filter((x) => !x.ok || x.stale).length +
        (featureEnabled(d, "crons") ? (d.agents?.health || []).filter((x) => x.source?.endsWith("-cron") && !x.ok).length : 0) +
        hostFailures,
      agents: sessionErrors.filter((session) => session.work_kind === "cron"
        ? featureEnabled(d, "crons")
        : featureEnabled(d, "agents")).length,
      errors: logErrors().length,
      unreadErrors: unreadLogErrors(),
      warnings: logs.warning || 0,
      gpu: gpuStates.filter((status) => ["error", "unhealthy", "unsafe", "reset_required", "reset-required"].includes(status)).length,
    };
  }
  function renderAttention() {
    const a = alerts(),
      critical = a.collectors + a.agents + a.unreadErrors + a.gpu,
      acknowledged = Math.max(0, a.errors - a.unreadErrors),
      n = $("#attention-button"),
      ack = $("#acknowledge-errors");
    n.className = `attention-cell ${critical ? "critical" : acknowledged ? "acknowledged" : ""}`;
    setText(
      "#attention-stamp",
      critical ? t("attention.stampIssue") : acknowledged ? t("attention.stampAcknowledged") : t("attention.stamp"),
    );
    const count = (value) => fmt(value, { maximumFractionDigits: 0 });
    const title = a.unreadErrors
      ? t(a.unreadErrors === 1 ? "attention.titleLogOne" : "attention.titleLogMany", { count: count(a.unreadErrors) })
      : a.agents
        ? t(a.agents === 1 ? "attention.titleAgentOne" : "attention.titleAgentMany", { count: count(a.agents) })
        : a.collectors
          ? t(a.collectors === 1 ? "attention.titleCollectorOne" : "attention.titleCollectorMany", { count: count(a.collectors) })
          : a.gpu
            ? t("attention.titleGpu")
            : t("attention.titleClear");
    setText("#attention-title", title);
    setText(
      "#attention-note",
      critical
        ? t("attention.summary", { logs: count(a.unreadErrors), collectors: count(a.collectors), agents: count(a.agents), gpu: count(a.gpu) })
        : acknowledged
          ? t("attention.acknowledgedSummary", { count: count(acknowledged) })
          : t("attention.clearSummary"),
    );
    setText("#log-badge", a.errors + a.warnings);
    if (ack) {
      ack.disabled = !a.unreadErrors;
      ack.textContent = a.unreadErrors
        ? t(a.unreadErrors === 1 ? "attention.logsButtonOne" : "attention.logsButtonMany", { count: count(a.unreadErrors) })
        : a.errors
          ? t("attention.logsAcknowledged")
          : t("attention.logsNone");
    }
    const age = Math.max(
      0,
      (Date.now() - new Date(ui.data.generated_at).valueOf()) / 1e3,
    );
    setText("#data-age", t("legend.data", { age: duration(age) }));
  }
  function activeSessions() {
    return (ui.data?.agents?.sessions || []).filter((session) =>
      (session.work_kind === "cron"
        ? featureEnabled(ui.data, "crons")
        : featureEnabled(ui.data, "agents")) && isLiveSession(session),
    );
  }
  function activityGlyph() {
    const svg = sv("svg", { viewBox: "0 0 64 44", "aria-hidden": "true", focusable: "false" });
    const scene = (name) => {
      const g = sv("g", { class: `activity-scene activity-${name}` });
      svg.append(g);
      return g;
    };
    const path = (g, d, cls = "") => g.append(sv("path", { d, class: cls }));
    const circle = (g, cx, cy, r, cls = "") => g.append(sv("circle", { cx, cy, r, class: cls }));
    const work = scene("working");
    path(work, "M11 22 31 10 53 22 31 34Z", "activity-track");
    for (const [x, y] of [[11, 22], [31, 10], [53, 22], [31, 34]]) circle(work, x, y, 3);
    circle(work, 11, 22, 2, "activity-signal activity-work-dot");
    circle(work, 31, 34, 2, "activity-signal activity-work-dot-alt");
    const search = scene("search");
    for (const x of [9, 25, 41]) {
      path(search, `M${x} 11h12v19H${x}Z`);
      path(search, `M${x + 3} 16h6m-6 4h6m-6 4h6`, "activity-track");
    }
    const lens = sv("g", { class: "activity-search-lens" });
    search.append(lens);
    circle(lens, 31, 19, 7, "activity-lens");
    path(lens, "M36 24l6 6", "activity-accent");
    const tool = scene("tool");
    path(tool, "M5 12h13v19H5ZM45 12h13v19H45Z");
    path(tool, "M10 18l4 4-4 4M49 18h5v7h-5");
    path(tool, "M20 22h23", "activity-track");
    circle(tool, 22, 22, 2.5, "activity-signal activity-tool-dot");
    const file = scene("file");
    path(file, "M19 6h19l7 7v25H19ZM38 6v7h7");
    for (const [i, y] of [18, 23, 28, 33].entries()) {
      const row = sv("path", { d: `M25 ${y}h13`, class: `activity-file-row activity-row-${i}` });
      file.append(row);
    }
    path(scene("waiting"), "M27 13v18M37 13v18", "activity-accent");
    const error = scene("error");
    circle(error, 32, 22, 12);
    path(error, "M32 15v9m0 4v1");
    const stale = scene("stale");
    circle(stale, 32, 22, 12);
    path(stale, "M32 14v9l6 3");
    circle(scene("unknown"), 32, 22, 3);
    return svg;
  }
  function updateWorkActivity() {
    const context = {
      generatedAt: ui.data?.agents?.generated_at,
      collectorHealth: ui.data?.collector_health?.agents,
      sourceHealth: ui.data?.agents?.health,
    };
    for (const row of workRows.values()) {
      const activity = KudosTelemetry.agentActivity(row.session,
        row.session.work_kind === "cron" ? {
          ...context,
          generatedAt: ui.data?.agents?.cron_generated_at,
          collectorHealth: ui.data?.collector_health?.crons,
        } : context);
      // Keep the SVG nodes and animation timeline stable across live snapshots.
      if (row.activity.dataset.mode !== activity.mode)
        row.activity.dataset.mode = activity.mode;
      const label = t(activity.labelKey);
      row.activity.title = label;
    }
  }
  let activityMotion = true;
  try { activityMotion = localStorage.getItem("kudos.activity-motion.v1") !== "off"; }
  catch (_error) { /* An unavailable preference store must not break the dashboard. */ }
  function updateActivityMotion() {
    document.documentElement.dataset.activityMotion = activityMotion ? "on" : "off";
    document.documentElement.dataset.activityHidden = String(document.hidden);
    const control = $("#activity-motion"),
      reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    control.setAttribute("aria-pressed", String(activityMotion && !reduced));
    control.disabled = reduced;
    control.textContent = activityMotion && !reduced ? t("work.motionOn") : t("work.motionOff");
    control.title = reduced ? t("work.motionSystem") : t("work.motionTooltip");
  }
  $("#activity-motion").addEventListener("click", () => {
    activityMotion = !activityMotion;
    try { localStorage.setItem("kudos.activity-motion.v1", activityMotion ? "on" : "off"); }
    catch (_error) { /* Session-only control still works. */ }
    updateActivityMotion();
  });
  matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", updateActivityMotion);
  updateActivityMotion();
  // Freshness must age out even when no new snapshot arrives.
  setInterval(() => { if (!document.hidden) updateWorkActivity(); }, 1000);
  function createWorkTicket(uid) {
    const button = el("button", "ticket");
    button.type = "button";
    button.dataset.inspect = `agent:${uid}`;
    const identity = el("span", "agent-id"),
      avatar = el("span", "avatar"),
      text = el("span"),
      agent = el("strong"),
      state = el("small");
    text.append(agent, state);
    identity.append(avatar, text);
    const line = el("span", "ticket-line"),
      origin = el("span", "ticket-origin"),
      task = el("span", "task"),
      meta = el("span", "ticket-meta"),
      started = el("small", "ticket-start"),
      activity = el("span", "ticket-activity");
    activity.append(activityGlyph());
    line.append(task, origin);
    button.append(identity, line, meta, started, activity);
    return {
      button,
      avatar,
      agent,
      state,
      origin,
      task,
      line,
      meta,
      started,
      activity,
      session: null,
      metaKey: null,
    };
  }
  function renderWork() {
    const sessions = activeSessions(),
      visible = sessions,
      gpuProcesses = ui.data?.system?.gpu?.gpu_processes,
      cpuLoad = num(ui.data?.system?.cpu?.usage_percent),
      gpuLoad = num(ui.data?.system?.gpu?.utilization_percent),
      load =
        cpuLoad === null && gpuLoad === null
          ? null
          : Math.max(cpuLoad ?? 0, gpuLoad ?? 0),
      box = $("#work-list");
    setText(
      "#active-count",
      featureEnabled(ui.data, "agents") || featureEnabled(ui.data, "crons")
        ? fmt(sessions.filter((s) => s.status !== "error" && !s.stale).length, { maximumFractionDigits: 0 })
        : "–",
    );
    setText(
      "#work-title",
      sessions.some((s) => s.status === "error")
        ? t("work.titleErrors")
        : t("work.title"),
    );
    setText(
      "#gpu-process-count",
      Array.isArray(gpuProcesses) ? fmt(gpuProcesses.length, { maximumFractionDigits: 0 }) : "–",
    );
    setText("#unbound-load", pct(load));
    const keys = new Set(
      visible.map((s, i) => String(s.uid ?? `session-${i}`)),
    );
    for (const [key, row] of workRows) {
      if (!keys.has(key)) {
        row.button.remove();
        workRows.delete(key);
      }
    }
    if (!visible.length) {
      if (!box.querySelector(".empty"))
        box.replaceChildren(
          el(
            "p",
            "empty",
            t("work.empty"),
          ),
        );
      return;
    }
    box.querySelector(".empty")?.remove();
    visible.forEach((s, index) => {
      const key = String(s.uid ?? `session-${index}`),
        row = workRows.get(key) || createWorkTicket(key);
      workRows.set(key, row);
      row.session = s;
      row.button.dataset.workKind = s.work_kind || "session";
      row.button.classList.toggle("error", s.status === "error");
      setNodeText(row.avatar, initials(s.agent));
      setNodeText(row.agent, s.agent || t("work.agentFallback"));
      const statusKey = {
          error: "activity.error",
          running: "activity.working",
          tool: "activity.tool",
          waiting: "activity.waiting",
        }[String(s.status || "").toLowerCase()],
        status = s.stale
          ? t("activity.stale")
          : statusKey
            ? t(statusKey)
            : s.status || t("work.statusUnknown");
      setNodeText(
        row.state,
        `${s.runtime || t("work.runtimeUnknown")}${s.work_kind === "cron" ? ` · ${t("work.cron")}` : ""} · ${status}`,
      );
      setNodeText(
        row.task,
        clip(s.task || s.title || t("work.taskUnavailable"), 105),
      );
      const origin = KudosTelemetry.originLabel(s.location, t("work.originUnavailable"));
      setNodeText(row.origin, origin);
      row.origin.title = origin;
      row.task.title = s.task || s.title || "";
      const metaKey = [s.model, s.reasoning, s.service_tier, KudosI18n.language()].join("|");
      if (metaKey !== row.metaKey) {
        const tags = [el("span", "tag model", s.model || t("inspector.modelUnavailable"))];
        if (s.reasoning)
          tags.push(
            el("span", "tag", t("work.reasoning", { value: String(s.reasoning) })),
          );
        if (s.service_tier)
          tags.push(
            el(
              "span",
              `tag ${s.service_tier === "fast" ? "fast" : ""}`,
              t("work.fast", {
                state: s.service_tier === "fast" ? t("work.fastOn") : t("work.fastOff"),
              }),
            ),
          );
        row.meta.replaceChildren(...tags);
        row.metaKey = metaKey;
      }
      setNodeText(
        row.started,
        t("work.runningFor", {
          age: duration(
            s.started_at || s.updated_at
              ? Math.max(0, (Date.now() - instant(s.started_at || s.updated_at)) / 1000)
              : s.age_seconds,
          ),
        }),
      );
      const current = box.children[index];
      if (current !== row.button) box.insertBefore(row.button, current || null);
    });
    updateWorkActivity();
  }
  function path(values, w, h, min = 0, max = 100) {
    const range = max - min || 1,
      segments = [];
    let current = [];
    (values || []).forEach((value, i) => {
      const v = num(value);
      if (v === null) {
        if (current.length) segments.push(current);
        current = [];
      } else current.push({ v, i });
    });
    if (current.length) segments.push(current);
    return segments
      .map((points) =>
        points
          .map(
            (p, j) =>
              `${j ? "L" : "M"}${(p.i / Math.max(1, values.length - 1)) * w},${h - ((p.v - min) / range) * h}`,
          )
          .join(" "),
      )
      .join(" ");
  }
  function renderTimeline() {
    const hist = (ui.data?.history || []).slice(-450),
      events = (featureEnabled(ui.data, "agents")
        ? ui.data?.agents?.events || []
        : []).slice(-20),
      root = $("#timeline-chart"),
      svg = sv("svg", {
        viewBox: "0 0 1000 420",
        preserveAspectRatio: "none",
        "aria-hidden": "true",
      }),
      thermal = hist.map((x) => {
        const values = [num(x.cpu_temp), num(x.gpu_temp)].filter(
          (v) => v !== null,
        );
        return values.length ? Math.max(...values) : null;
      }),
      series = [
        ["CPU", "cpu", varColor("--signal"), 20, 85, 100, "pct"],
        ["GPU", "gpu", varColor("--copper"), 116, 181, 100, "pct"],
        [
          t("timeline.temperature"),
          "temperature",
          varColor("--hot"),
          212,
          277,
          100,
          "temp",
        ],
        [t("timeline.network"), "network_total", varColor("--blue"), 308, 373, null, "rate"],
      ];
    series.forEach(([label, key, color, top, bottom, fixed, format]) => {
      const vals = key === "temperature" ? thermal : hist.map((x) => x[key]),
        valid = vals.map(num).filter((v) => v !== null),
        max = fixed ?? Math.max(1, ...valid),
        latest = vals.at(-1);
      svg.append(
        sv("text", { x: 10, y: top, class: "chart-label" }, label),
        sv(
          "text",
          { x: 940, y: top, class: "chart-value" },
          format === "rate"
            ? safeRate(latest)
            : format === "temp"
              ? temp(latest)
              : pct(latest),
        ),
      );
      const d = KudosTelemetry.historyPath(
        hist.map((p, i) => ({
          at: p.at,
          value: num(vals[i]) === null ? null : (num(vals[i]) / max) * 100,
        })),
        "value",
        Date.now(),
        15 * 60000,
        1000,
        bottom - top - 20,
      );
      if (d)
        svg.append(
          sv("path", {
            d,
            fill: "none",
            stroke: color,
            "stroke-width": 2,
            transform: `translate(0 ${top + 10})`,
          }),
        );
    });
    const now = Date.now(),
      start = now - 15 * 60 * 1e3;
    events.forEach((e) => {
      const t = new Date(e.timestamp).valueOf();
      if (!Number.isFinite(t) || t < start || t > now) return;
      const x = ((t - start) / (now - start)) * 1e3;
      svg.append(
        sv("line", {
          x1: x,
          y1: 0,
          x2: x,
          y2: 397,
          stroke: e.is_error ? varColor("--hot") : varColor("--paper"),
          "stroke-width": 1,
          "stroke-dasharray": "3 5",
          opacity: 0.5,
        }),
        sv("circle", {
          cx: x,
          cy: 397,
          r: 3,
          fill: e.is_error ? varColor("--hot") : varColor("--copper"),
        }),
      );
    });
    [0, 5, 10, 15].forEach((m, i) =>
      svg.append(
        sv(
          "text",
          { x: i * 333, y: 416, class: "chart-axis" },
          i === 3 ? t("timeline.now") : clock(start + m * 6e4).slice(0, 5),
        ),
      ),
    );
    root.replaceChildren(svg);
  }
  function varColor(name) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
  }
  function spark(values, color = "signal") {
    const svg = sv("svg", {
        class: "mini",
        viewBox: "0 0 180 44",
        "aria-hidden": "true",
      }),
      nums = values.map(num),
      valid = nums.filter((v) => v !== null),
      max = Math.max(1, ...valid),
      d = path(nums, 180, 38, 0, max);
    if (d) {
      if (color) svg.classList.add(color);
      svg.append(
        sv("path", { class: "area", d: `${d} L180 44 L0 44 Z` }),
        sv("path", { d }),
      );
    }
    return svg;
  }
  function renderResources() {
    const d = ui.data || {},
      s = d.system || {},
      h = d.history || [],
      c = s.cpu || {},
      g = s.gpu || {},
      r = s.ram || {},
      n = s.network || {},
      temps = [num(c.temperature), num(g.temperature)].filter(
        (v) => v !== null,
      ),
      maxTemp = temps.length ? Math.max(...temps) : null,
      rx = num(n.rx_bytes_sec),
      tx = num(n.tx_bytes_sec),
      processes = Array.isArray(g.gpu_processes) ? g.gpu_processes : null,
      gpuState = String(g.health_status ?? g.health ?? "").toLowerCase(),
      gpuBad = ["error", "unhealthy", "unsafe", "reset_required", "reset-required"].includes(gpuState),
      gpuUnavailable = ["unavailable", "not_configured", "not-configured", "unknown"].includes(gpuState) ||
        (!gpuState && num(g.utilization_percent) === null && processes === null),
      temperatureHistory = h.map((x) => {
        const values = [num(x.cpu_temp), num(x.gpu_temp)].filter(
          (v) => v !== null,
        );
        return values.length ? Math.max(...values) : null;
      });
    const rows = [
      {
        id: "cpu",
        name: t("host.cpu"),
        value: pct(c.usage_percent),
        sub: num(c.load_1) === null ? "–" : t("host.loadOneMinute", { value: fmt(c.load_1, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) }),
        vals: h.map((x) => x.cpu),
        condition: band(c.usage_percent),
        detail: temp(c.temperature),
        tb: tempBand(c.temperature),
        owner: t("resources.usageNow", { value: pct(c.usage_percent) }),
      },
      {
        id: "gpu",
        name: t("host.gpu"),
        value: pct(gpuBad ? null : g.utilization_percent),
        sub:
          g.is_unified_memory === true
            ? t("host.gpuUma")
            : g.is_unified_memory === false
              ? num(g.memory_used_mb) === null ||
                num(g.memory_total_mb) === null
                ? t("host.telemetryMissing")
                : `${gb(num(g.memory_used_mb) / 1024)} / ${gb(num(g.memory_total_mb) / 1024)}`
              : t("host.telemetryMissing"),
        vals: h.map((x) => x.gpu),
        condition: gpuBad ? "critical" : gpuUnavailable ? "unknown" : band(g.utilization_percent),
        detail: `${temp(g.temperature)} · ${num(g.power_watts) === null ? "–" : `${fmt(Math.round(num(g.power_watts)), { maximumFractionDigits: 0 })} W`}`,
        tb: tempBand(g.temperature),
        owner: gpuBad
          ? hostStatusName(gpuState)
          : processes === null
            ? t("resources.gpuProcessesUnavailable")
            : processes.length
              ? t("resources.gpuProcessesMeasured", { count: fmt(processes.length, { maximumFractionDigits: 0 }) })
              : t("resources.gpuProcessesNone"),
      },
      {
        id: "ram",
        name: t("host.ram"),
        value: pct(r.used_percent),
        sub: `${gb(r.used_gb)} / ${gb(r.total_gb)}`,
        vals: h.map((x) => x.ram),
        condition: band(r.used_percent),
        detail: t("host.sharedMemory"),
        owner: t("resources.totalMeasured"),
      },
      {
        id: "network",
        name: t("host.network"),
        value: rx === null || tx === null ? "\u2013" : rate(rx + tx),
        sub: `\u2193 ${safeRate(rx)} \xB7 \u2191 ${safeRate(tx)}`,
        vals: h.map((x) => x.network_total),
        condition: rx === null || tx === null ? "unknown" : "normal",
        detail: n.interface || t("resources.interfaceUnknown"),
        owner: t("resources.sourceUnbound"),
        chart: "net",
      },
      {
        id: "thermal",
        name: t("resources.thermal"),
        value: temp(maxTemp),
        sub: t("resources.max"),
        vals: temperatureHistory,
        condition:
          maxTemp === null
            ? "unknown"
            : maxTemp >= 85
              ? "critical"
              : maxTemp >= 70
                ? "warning"
                : "normal",
        detail: t("resources.tempLimit", { value: fmt(70, { maximumFractionDigits: 0 }) }),
        tb: tempBand(maxTemp),
        owner: `${t("host.cpu")} ${temp(c.temperature)} · ${t("host.gpu")} ${temp(g.temperature)}`,
        chart: "therm",
      },
    ];
    const box = $("#resource-channels");
    box.replaceChildren();
    rows.forEach((x) => {
      const b = el(
        "button",
        `channel ${x.id === "network" ? "net" : x.id === "thermal" ? "therm" : ""}`,
      );
      b.type = "button";
      b.dataset.inspect = `resource:${x.id}`;
      b.append(el("span", "channel-name", x.name));
      const value = el("span", "channel-value");
      value.append(el("b", "", x.value), el("span", "", x.sub));
      b.append(value, spark(x.vals, x.chart || ""));
      const cond = el("span", `condition ${x.condition}`);
      cond.append(
        el(
          "b",
          "",
          t(`resources.status.${x.condition}`),
        ),
      );
      const detail = el("span", x.tb ? `temperature ${x.tb}` : "", x.detail);
      cond.append(detail);
      b.append(cond, el("span", "ownership", x.owner));
      box.append(b);
    });
  }
  function renderStorage() {
    const disks = (ui.data?.system?.disks || []).filter((d) => d.is_remote),
      key = disks
        .map((x) => [x.label, x.mount, x.free_gb, x.total_gb, x.use_percent].join("|"))
        .join("::");
    if (key === ui.lastStorageKey) return;
    const box = $("#volume-map");
    box.replaceChildren();
    disks.forEach((d) => {
      const use = num(d.use_percent),
        free = num(d.free_gb),
        b = el(
          "button",
          `volume ${band(use, 85, 95)}${use === null ? " unknown" : ""}`,
        );
      b.type = "button";
      b.dataset.inspect = `disk:${d.mount}`;
      b.style.flexGrow = String(Math.max(1, num(d.total_gb) || 1));
      const label = d.label || d.mount || t("inspector.diskTitle"),
        freeText = free === null ? "–" : capacity(free),
        usedText = use === null ? "–" : pct(use);
      b.setAttribute("aria-label", t("resources.diskAria", {
        label,
        free: free === null ? t("resources.freeUnknown") : freeText,
        used: usedText,
      }));
      b.append(
        el("b", "", label),
        el(
          "span",
          "",
          `${free === null ? t("resources.freeUnknown") : t("resources.storageFree", { value: freeText })} · ${use === null ? "–" : t("resources.storageUsed", { value: pct(use) })}`,
        ),
      );
      const fill = el("i");
      fill.style.width = `${use === null ? 0 : clamp(use)}%`;
      b.append(fill);
      box.append(b);
    });
    const allFreeKnown =
        disks.length > 0 && disks.every((d) => num(d.free_gb) !== null),
      free = allFreeKnown
        ? disks.reduce((a, d) => a + num(d.free_gb), 0)
        : null;
    setText(
      "#storage-summary",
      t("resources.storageSummary", {
        count: fmt(disks.length, { maximumFractionDigits: 0 }),
        free: free === null ? t("resources.freeUnknown") : t("resources.storageFree", { value: capacity(free) }),
      }),
    );
    ui.lastStorageKey = key;
  }
  function serviceState(s) {
    const h = s.collector || {};
    const status = String(s.status || "unknown").toLowerCase();
    if (["failed", "dead", "inactive", "stopped"].includes(status) || h.ok === false) return "bad";
    if (h.stale === true) return "stale";
    if (h.ok !== true) return "unknown";
    if ((num(h.duration_ms) || 0) > 1e3) return "slow";
    return "";
  }
  function renderServices() {
    const rows = ui.data?.system?.services || [],
      key = rows
        .map((x) =>
          [
            x.name,
            x.label,
            x.status,
            x.activity,
            x.collector?.duration_ms,
            x.collector?.ok,
            x.collector?.stale,
          ].join("|"),
        )
        .join("::");
    if (key === ui.lastServiceKey) return;
    const box = $("#service-vitals");
    box.replaceChildren();
    rows.forEach((s) => {
      const state = serviceState(s),
        b = el("button", `vital ${state}`);
      b.type = "button";
      b.dataset.inspect = `service:${s.name}`;
      b.title = s.activity || s.status || t("services.noTelemetry");
      const serviceStatus = String(s.status || "unknown").toLowerCase(),
        serviceStatusKey = {
          active: "services.status.active",
          running: "services.status.active",
          inactive: "services.status.inactive",
          stopped: "services.status.inactive",
          failed: "services.status.failed",
        }[serviceStatus];
      b.append(
        el("b", "", s.label || s.name),
        el(
          "span",
          "",
          s.collector?.stale
            ? t("services.stale")
            : s.collector?.ok === false
              ? t("services.notMeasured")
              : s.collector?.ok !== true
                ? t("services.noTelemetry")
                : serviceStatusKey
                  ? t(serviceStatusKey)
                  : num(s.collector?.duration_ms) !== null
                    ? `${fmt(s.collector.duration_ms, { maximumFractionDigits: 0 })} ms`
                    : s.status || t("services.status.unknown"),
        ),
      );
      box.append(b);
    });
    if (!rows.length) box.append(el("p", "diagnostic-note", t("services.none")));
    ui.lastServiceKey = key;
  }
  function metric(label, value, wide = false, detail = "") {
    const m = el("div", `metric ${wide ? "wide" : ""}`),
      display = value === null || value === undefined || value === ""
        ? "–"
        : typeof value === "number"
          ? fmt(value)
          : typeof value === "boolean"
            ? t(value ? "inspector.yes" : "inspector.no")
            : String(value);
    m.append(el("span", "", label), el("b", "", display));
    if (detail) m.append(el("p", "", detail));
    return m;
  }
  function processList(title, rows, valueFor, note = "") {
    const section = el("section", "diagnostic-list wide");
    section.append(el("h3", "", title));
    if (note) section.append(el("p", "diagnostic-note", note));
    const list = el("ol", "process-list");
    (Array.isArray(rows) ? rows : []).slice(0, 10).forEach((row) => {
      const li = el("li");
      li.append(
        el("span", "process-name", row.name || t("inspector.unknownProcessName")),
        el("span", "process-pid", `PID ${fmt(row.pid, { maximumFractionDigits: 0 })}`),
        el("b", "", valueFor(row)),
      );
      list.append(li);
    });
    if (!Array.isArray(rows))
      list.append(el("li", "empty", t("inspector.processesNotInSnapshot")));
    else if (!list.children.length)
      list.append(el("li", "empty", t("inspector.noProcesses")));
    section.append(list);
    return section;
  }
  async function loadLargestFiles(mount, section) {
    try {
      const response = await fetch(
        `/api/disks/largest-files?${new URLSearchParams({ mount })}`,
        { cache: "no-store" },
      );
      if (!response.ok) throw Error(response.status);
      const data = await response.json();
      section.replaceChildren(el("h3", "", t("inspector.largestFiles")));
      section.append(
        el(
          "p",
          "diagnostic-note",
          data.complete
            ? t("inspector.largestFilesDone", { count: fmt(data.scanned_files, { maximumFractionDigits: 0 }), cache: data.cached ? t("inspector.fromCache") : `${fmt(data.duration_ms, { maximumFractionDigits: 0 })} ms` })
            : t("inspector.partialFiles", { count: fmt(data.scanned_files, { maximumFractionDigits: 0 }) }),
        ),
      );
      const list = el("ol", "file-list");
      (data.files || []).forEach((file) => {
        const li = el("li");
        li.append(
          el("span", "", file.path),
          el("b", "", bytes(file.size_bytes)),
        );
        list.append(li);
      });
      if (!list.children.length)
        list.append(el("li", "empty", t("inspector.filesNone")));
      section.append(list);
    } catch (_error) {
      section.replaceChildren(
        el("h3", "", t("inspector.largestFiles")),
        el(
          "p",
          "diagnostic-note error",
          t("inspector.fileSearchFailed")
        ),
      );
    }
  }
  function openInspector(spec, trigger) {
    ui.hostInspector = null;
    const separator = spec.indexOf(":"),
      type = separator < 0 ? spec : spec.slice(0, separator),
      id = separator < 0 ? "" : spec.slice(separator + 1);
    const d = ui.data,
      s = d.system || {},
      box = $("#inspector-content");
    box.replaceChildren();
    let title = t("inspector.defaultTitle"),
      kicker = type.toUpperCase(),
      fileSection = null;
    if (type === "power") {
      title = t("inspector.powerTitle");
      kicker = t("inspector.measured");
      for (const [hostId, h] of hostEntries(d)) {
        const watts = num(h.system?.gpu?.power_watts),
          label = h.label || hostId,
          stale = h.health?.stale === true || h.health?.ok === false;
        box.append(
          metric(
            label,
            watts === null ? "–" : `${fmt(watts, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} W`,
            false,
            t("inspector.measurement", {
              time: clock(h.generated_at),
              stale: stale ? t("inspector.measurementUncertain") : "",
            }),
          ),
        );
      }
      box.append(
        metric(
          t("inspector.scope"),
          t("inspector.powerScopeValue"),
          true,
          t("inspector.powerScopeNote"),
        ),
      );
    } else if (type === "agent") {
      const a = (d.agents?.sessions || []).find((x) => x.uid === id);
      if (!a) return;
      title = a.agent || t("work.agentFallback");
      kicker = `${String(a.runtime || "").toUpperCase()}${a.work_kind === "cron" ? ` · ${t("inspector.cronType").toUpperCase()}` : ""} · ${a.stale ? t("inspector.statusUncertain") : String(a.status || "").toUpperCase()}`;
      box.append(
        metric(t("inspector.task"), a.task || a.title || "\u2013", true),
        metric(t("inspector.model"), a.model || "\u2013"),
        metric(t("inspector.provider"), a.provider || "\u2013"),
        metric(
          a.work_kind === "cron" ? t("inspector.cronType") : t("inspector.origin"),
          KudosTelemetry.originLabel(a.location, t("work.originUnavailable")),
          true,
        ),
        metric(t("inspector.tool"), a.current_tool || "\u2013"),
        metric(t("inspector.age"), duration(a.age_seconds)),
        metric(t("inspector.tokens"), num(a.task_total_tokens) === null ? "\u2013" : fmt(a.task_total_tokens, { maximumFractionDigits: 0 })),
      );
    } else if (type === "resource") {
      title =
        {
          cpu: t("host.cpu"),
          gpu: t("host.gpu"),
          ram: t("host.ram"),
          network: t("inspector.network"),
          thermal: t("inspector.temperature"),
          self: t("inspector.self"),
        }[id] || id;
      const v =
        {
          cpu: s.cpu,
          gpu: s.gpu,
          ram: s.ram,
          network: s.network,
          thermal: { cpu: s.cpu?.temperature, gpu: s.gpu?.temperature },
          self: s.processes?.self,
        }[id] || {};
      Object.entries(v)
        .filter(([, x]) => ["string", "number", "boolean"].includes(typeof x))
        .slice(0, 14)
        .forEach(([k, x]) => {
          const labels = {
              usage_percent: "inspector.usage",
              used_percent: "inspector.usage",
              load_1: "inspector.cpuLoad",
              temperature: "inspector.temperature",
              power_watts: "inspector.gpuPower",
              cap_mhz: "inspector.gpuClockCap",
              memory_used_mb: "inspector.gpuMemory",
              memory_total_mb: "inspector.total",
              used_gb: "inspector.used",
              free_gb: "inspector.free",
              available_gb: "inspector.available",
              total_gb: "inspector.total",
              rx_bytes_sec: "inspector.receive",
              tx_bytes_sec: "inspector.transmit",
              interface: "inspector.interface",
              is_unified_memory: "inspector.sharedMemory",
              health_status: "inspector.state",
              health: "inspector.state",
              hostname: "inspector.hostname",
            },
            labelsKey = labels[k],
            value = {
              usage_percent: pct,
              used_percent: pct,
              temperature: temp,
              power_watts: (n) => num(n) === null ? "–" : `${fmt(n, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} W`,
              memory_used_mb: (n) => num(n) === null ? "–" : `${fmt(n, { maximumFractionDigits: 0 })} MiB`,
              memory_total_mb: (n) => num(n) === null ? "–" : `${fmt(n, { maximumFractionDigits: 0 })} MiB`,
              rx_bytes_sec: safeRate,
              tx_bytes_sec: safeRate,
            }[k];
          box.append(metric(labelsKey ? t(labelsKey) : k.replaceAll("_", " "), value ? value(x) : x));
        });
      if (id === "cpu") {
        box.append(
          processList(
            t("inspector.cpuLeaders"),
            s.processes?.cpu == null ? null : KudosTelemetry.leaders(s.processes.cpu, "cpu_percent"),
            (row) => pct(row.cpu_percent),
          ),
        );
      } else if (id === "ram") {
        box.append(
          processList(
            t("inspector.memoryLeaders"),
            s.processes?.memory == null ? null : KudosTelemetry.leaders(s.processes.memory, "memory_mb"),
            (row) => `${fmt(row.memory_mb, { maximumFractionDigits: 0 })} MiB`,
          ),
        );
      } else if (id === "gpu") {
        box.append(
          processList(
            t("inspector.gpuLeaders"),
            s.gpu?.gpu_processes,
            (row) =>
              num(row.gpu_util) === null
                ? `${fmt(row.memory_mb, { maximumFractionDigits: 0 })} MiB`
                : `${fmt(Math.round(row.gpu_util), { maximumFractionDigits: 0 })} % · ${fmt(row.memory_mb, { maximumFractionDigits: 0 })} MiB`,
          ),
        );
      } else if (id === "thermal") {
        box.append(
          processList(
            t("inspector.thermalLeaders"),
            s.processes?.cpu == null ? null : KudosTelemetry.leaders(s.processes.cpu, "cpu_percent"),
            (row) => pct(row.cpu_percent),
            t("inspector.thermalNote"),
          ),
        );
      } else if (id === "network") {
        const notice = el("section", "diagnostic-list wide");
        notice.append(
          el("h3", "", t("inspector.networkProcessTitle")),
          el(
            "p",
            "diagnostic-note",
            t("inspector.networkProcessNote"),
          ),
        );
        box.append(notice);
      }
    } else if (type === "quota") {
      const q = d.agents?.quotas?.[id] || {};
      title = q.provider || id;
      kicker = t("inspector.quotaTitle");
      (q.windows || []).forEach((w) =>
        box.append(
          metric(
            quotaWindowLabel(id, w),
            num(w.remaining_percent) === null ? "–" : t("inspector.remaining", { percent: fmt(w.remaining_percent, { maximumFractionDigits: 0 }) }),
            true,
            [num(w.used_percent) === null ? "" : t("quota.used", { percent: fmt(w.used_percent, { maximumFractionDigits: 0 }) }),
              t("inspector.reset", { time: clock(w.resets_at, false) })].filter(Boolean).join(" · "),
          ),
        ),
      );
      if (q.collected_at) box.append(metric(t("quota.lastUpdated"), clock(q.collected_at, false), true));
      if (q.resets_available != null || id === "chatgpt")
        box.append(metric(t("inspector.resets"), num(q.resets_available) === null ? t("quota.unavailable") : fmt(q.resets_available, { maximumFractionDigits: 0 })));
      box.append(metric(t("inspector.sourceStatus"), quotaStatusText(q, Boolean(q.windows?.length)), true));
    } else if (type === "disk") {
      const disk = (s.disks || []).find((x) => x.mount === id);
      if (!disk) return;
      title = disk.label || disk.mount;
      kicker = t("inspector.storage");
      box.append(
        metric(t("inspector.used"), pct(disk.use_percent)),
        metric(t("inspector.free"), gb(disk.free_gb)),
        metric(t("inspector.total"), gb(disk.total_gb)),
        metric(t("inspector.mount"), disk.mount, true),
      );
      fileSection = el("section", "diagnostic-list wide");
      fileSection.append(
        el("h3", "", t("inspector.largestFiles")),
        el(
          "p",
          "diagnostic-note",
          t("inspector.fileSearchRunning"),
        ),
      );
      box.append(fileSection);
    } else if (type === "service") {
      const x = (s.services || []).find((v) => v.name === id);
      if (!x) return;
      title = x.label || x.name;
      kicker = t("inspector.coreService");
      const serviceStatus = String(x.status || "unknown").toLowerCase(),
        serviceStatusKey = {
          active: "services.status.active",
          running: "services.status.active",
          inactive: "services.status.inactive",
          stopped: "services.status.inactive",
          failed: "services.status.failed",
        }[serviceStatus];
      box.append(
        metric(t("inspector.state"), serviceStatusKey ? t(serviceStatusKey) : x.status || t("services.status.unknown")),
        metric(
          t("inspector.memory"),
          num(x.memory_mb) === null
            ? "\u2013"
            : `${fmt(x.memory_mb, { maximumFractionDigits: 0 })} MiB`,
        ),
        metric(
          t("inspector.collection"),
          num(x.collector?.duration_ms) === null
            ? "\u2013"
            : `${fmt(x.collector.duration_ms, { maximumFractionDigits: 0 })} ms`,
        ),
        metric(t("inspector.dataAge"), duration(x.collector?.age_seconds)),
        metric(t("inspector.activity"), x.activity || "–", true),
        metric(t("inspector.collector"), x.collector?.error || t("services.healthy"), true),
      );
    }
    setText("#inspector-kicker", kicker);
    setText("#inspector-title", title);
    showDialog($("#inspector"), trigger);
    if (fileSection) loadLargestFiles(id, fileSection);
  }
  function showDialog(dialog, trigger) {
    dialog._returnFocus = trigger || document.activeElement;
    if (!dialog.open) dialog.showModal();
  }
  function closeDialog(dialog) {
    if (dialog.id === "inspector") ui.hostInspector = null;
    if (dialog.open) dialog.close();
    dialog._returnFocus?.focus?.();
  }
  function logSources(x) {
    const values = Array.isArray(x.sources) ? x.sources : [],
      all = [...values];
    if (x.source && !all.includes(x.source)) all.push(x.source);
    return all.map((s) => String(s).toLowerCase());
  }
  function logCategoryLabel(category) {
    const keys = {
      discord_connection: "dialog.category.discordConnection",
      discord_recovered: "dialog.category.discordRecovered",
      proxy_client_closed: "dialog.category.proxyClosed",
      operational: "dialog.category.operational",
    };
    return keys[category] ? t(keys[category]) : category || t("dialog.category.event");
  }
  function renderLogs() {
    const enabled = featureEnabled(ui.data, "logs");
    $("#logs-button").hidden = !enabled;
    if (!enabled) {
      $("#log-list").replaceChildren();
      return;
    }
    const records = ui.data?.logs?.records || [],
      visible = records.filter((x) => {
        const level = String(x.level || "").toUpperCase(),
          sourceNames = logSources(x),
          isError = ["ERROR", "CRITICAL"].includes(level);
        return (
          (ui.logLevel === "all" ||
            (ui.logLevel === "WARNING" &&
              ["WARNING", "ERROR", "CRITICAL"].includes(level)) ||
            (ui.logLevel === "ERROR" && isError)) &&
          (ui.logSource === "all" ||
            sourceNames.includes(ui.logSource) ||
            (ui.logSource === "gateway" &&
              sourceNames.includes("openclaw-gateway"))) &&
          (ui.logRuntime === "all" || x.runtime === ui.logRuntime) &&
          (ui.logStatus === "all" ||
            (ui.logStatus === "recovered"
              ? x.status === "recovered"
              : x.status !== "recovered" && ["ERROR", "CRITICAL"].includes(level)))
        );
      });
    const key = [
      ui.logRuntime,
      ui.logLevel,
      ui.logSource,
      ui.logStatus,
      ...visible.map((x) =>
        [
          x.id,
          logSignature(x),
          x.status,
          x.resolved_at,
          x.category,
          logSources(x).join(","),
          x.event_count,
        ].join("|"),
      ),
    ].join("::");
    const missing = (ui.data?.logs?.sources || []).filter(
      (s) => s.available === false || s.stale,
    );
    setText(
      "#log-source-status",
      missing.length
        ? t("dialog.noLogSources", { count: fmt(missing.length, { maximumFractionDigits: 0 }) })
        : t("dialog.logSourcesReady"),
    );
    if (key === ui.lastLogKey) return;
    const list = $("#log-list");
    list.replaceChildren();
    visible.forEach((x) => {
      const li = el("li", `level-${x.level || "INFO"}`);
      if (x.id != null) li.dataset.incidentId = String(x.id);
      const sourceText = logSources(x)
          .map((s) => s.toUpperCase())
          .join(" · ") || "–",
        detail = el("span", "message"),
        state = el("span", `log-state status-${x.status || "recorded"}`),
        category = el("span", "log-category", logCategoryLabel(x.category));
      if (x.status === "recovered") {
        state.textContent = x.resolved_at
          ? t("dialog.recoveredAt", { time: clock(x.resolved_at) })
          : t("dialog.recovered");
      } else if (x.status === "unresolved") {
        state.textContent = t("dialog.unresolved");
      } else {
        state.textContent = t("dialog.recorded");
      }
      detail.append(category, state);
      if (Number(x.event_count) > 1)
        detail.append(
          el("span", "log-event-count", t("dialog.eventsInIncident", { count: fmt(x.event_count, { maximumFractionDigits: 0 }) })),
        );
      detail.append(el("span", "log-message-text", knownText(x.message)));
      const levelBadge = el("span", "", x.level || "INFO");
      if (x.original_level && x.original_level !== x.level)
        levelBadge.title = t("dialog.originalLevel", { level: x.original_level });
      li.append(
        el("time", "", clock(x.timestamp)),
        levelBadge,
        el("span", "agent", x.agent || "HERMES"),
        el("span", "source", `${(x.runtime || "hermes").toUpperCase()} · ${sourceText}`),
        el("span", "component", x.component || "–"),
        detail,
      );
      list.append(li);
    });
    if (!visible.length)
      list.append(el("li", "", t("dialog.noMatchingLogs")));
    setText(
      "#log-result-count",
      t("dialog.logCount", { shown: fmt(visible.length, { maximumFractionDigits: 0 }), total: fmt(records.length, { maximumFractionDigits: 0 }) }),
    );
    ui.lastLogKey = key;
  }
  async function refreshLogs() {
    if (!featureEnabled(ui.data, "logs")) return;
    if (ui.logFetchPending) return;
    ui.logFetchPending = true;
    try {
      const response = await fetch("/api/logs", { cache: "no-store" });
      if (!response.ok) throw Error(response.status);
      const logs = await response.json();
      ui.data.logs = logs;
      ui.lastLogKey = "";
      renderLogs();
      renderAttention();
    } catch (_error) {
      setText("#log-result-count", t("dialog.logRefreshFailed"));
    } finally {
      ui.logFetchPending = false;
    }
  }
  function renderEvents() {
    const enabled = featureEnabled(ui.data, "agents");
    $("#chronicle-button").hidden = !enabled;
    if (!enabled) {
      $("#event-list").replaceChildren();
      return;
    }
    // The event allowlist contains agent events; cron runs have a separate ledger.
    let rows = (ui.data?.agents?.events || []).slice().reverse();
    if (ui.eventKind !== "all")
      rows = rows.filter((e) =>
        ui.eventKind === "ERROR" ? e.is_error : e.kind === ui.eventKind,
      );
    const key = [
      ui.eventKind,
      ...rows.map((x) =>
        [x.timestamp, x.kind, x.summary, x.duration_ms].join("|"),
      ),
    ].join("::");
    if (key === ui.lastEventKey) return;
    const list = $("#event-list");
    list.replaceChildren();
    rows.slice(0, 100).forEach((e) => {
      const li = el("li", e.is_error ? "level-ERROR" : "");
      li.append(
        el(
          "time",
          "",
          `${e.timestamp_approximate ? "\u2248 " : ""}${clock(e.timestamp)}`,
        ),
        el("span", "", e.agent),
        el("span", "kind", e.tool_name || e.kind),
        el("span", "", knownText(e.summary)),
        el(
          "span",
          "meta",
          [
            String(e.runtime || "").toUpperCase(),
            e.model,
            e.duration_ms == null ? "" : `${fmt(e.duration_ms, { maximumFractionDigits: 0 })} ms`,
          ]
            .filter(Boolean)
            .join(" \xB7 "),
        ),
      );
      list.append(li);
    });
    if (!rows.length)
      list.append(el("li", "", t("dialog.noMatchingEvents")));
    ui.lastEventKey = key;
  }
  function historyKey(point) {
    const value =
      point?.at ??
      point?.timestamp ??
      point?.collected_at ??
      point?.generated_at;
    return value === null || value === void 0 ? null : String(value);
  }
  function mergeHistory(previous = [], incoming = []) {
    const rows = Array.isArray(previous) ? previous.slice() : [];
    (Array.isArray(incoming) ? incoming : []).forEach((point) => {
      const key = historyKey(point),
        index =
          key === null
            ? rows.findIndex(
                (row) =>
                  historyKey(row) === null &&
                  JSON.stringify(row) === JSON.stringify(point),
              )
            : rows.findIndex((row) => historyKey(row) === key);
      if (index < 0) rows.push(point);
      else rows[index] = point;
    });
    return rows.slice(-450);
  }
  function mergeSnapshot(incoming) {
    if (!ui.data) return incoming;
    const history = mergeHistory(ui.data.history, incoming.history),
      hasHostSnapshot = Object.prototype.hasOwnProperty.call(incoming, "hosts"),
      hosts = hasHostSnapshot ? {} : { ...(ui.data.hosts || {}) };
    Object.entries(incoming.hosts || {}).forEach(([id, host]) => {
      const previous = ui.data.hosts?.[id] || {};
      hosts[id] = {
        ...previous,
        ...host,
        history: mergeHistory(previous.history, host.history),
      };
    });
    const logs = {
      ...(ui.data.logs || {}),
      ...(incoming.logs || {}),
      records: ui.data.logs?.records || [],
    };
    return { ...ui.data, ...incoming, history, hosts, logs };
  }
  function hostPayload(id, d = ui.data) {
    return hostFor(d, id);
  }
  function setNodeText(node, value) {
    const text = String(value);
    if (node && node.textContent !== text) node.textContent = text;
  }
  function gib(v) {
    const n = num(v);
    return n === null ? "–" : `${fmt(n, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} GiB`;
  }
  function safeRate(v) {
    return num(v) === null ? "–" : rate(v);
  }
  function hostGpuProcesses(id, gpu) {
    const rows = Array.isArray(gpu?.gpu_processes) ? gpu.gpu_processes : null;
    return rows === null ? null : KudosTelemetry.gpuLeaders(rows).rows;
  }
  function hostStatusName(value) {
    const key = {
      ok: "host.status.ok",
      healthy: "host.status.ok",
      available: "host.status.ok",
      partial: "host.status.partial",
      stale: "host.status.stale",
      unavailable: "host.status.unavailable",
      unsafe: "host.status.unsafe",
      reset_required: "host.status.reset",
      "reset-required": "host.status.reset",
    }[String(value || "").toLowerCase()] || "host.status.unknown";
    return t(key);
  }
  function createHostMetric(id, key, label, sub) {
    const button = el("button", `host-resource ${key}`);
    button.type = "button";
    button.dataset.hostInspect = id;
    button.dataset.hostMetric = key;
    button.setAttribute("aria-haspopup", "dialog");
    const labelBox = el("span", "host-label");
    const title = el("strong", "", label),
      caption = el("small", "", sub);
    labelBox.append(title, caption);
    const value = el("span", "host-value", "–"),
      chart = el("span", "host-chart"),
      meter = el("span", "host-meter"),
      fill = el("i"),
      scale = el("span", "host-scale");
    // The enclosing button exposes the numeric value and unknown state to AT.
    chart.setAttribute("aria-hidden", "true");
    meter.append(fill);
    scale.append(el("span", "", "0 %"), el("span", "", "100 %"));
    chart.append(meter, scale);
    chart.title = t("host.chartTitle");
    const capacity = el("span", "host-capacity");
    const meta = el("strong", "", "–"),
      note = el("small", "", "–");
    capacity.append(meta, note);
    button.append(labelBox, value, chart, capacity);
    return {
      button,
      label: title,
      caption,
      value,
      fill,
      meter,
      meta,
      note,
    };
  }
  function createHostPanel(id) {
    const article = el("article", "host-panel");
    article.dataset.host = id;
    const header = el("header", "host-panel-head"),
      identity = el("div", "host-identity"),
      title = el("h2", "", id),
      role = el("span", "host-role", ""),
      state = el("div", "host-state"),
      indicator = el("i"),
      stateText = el("span", "", t("host.waiting"));
    state.append(indicator, stateText);
    const hostName = el("small", "host-name", ""),
      headRight = el("div", "host-state-wrap");
    headRight.append(state, hostName);
    identity.append(title, role);
    header.append(identity, headRight);
    const alert = el("p", "host-alert", ""),
      body = el("div", "host-data");
    const metrics = {
      cpu: createHostMetric(id, "cpu", t("host.cpu"), t("host.processor")),
      gpu: createHostMetric(id, "gpu", t("host.gpu"), t("host.compute")),
      ram: createHostMetric(id, "ram", t("host.ram"), t("host.sharedMemory")),
    };
    Object.values(metrics).forEach((m) => body.append(m.button));
    const stats = el("div", "host-stats"),
      statNodes = {};
    [
      ["thermal", t("host.thermal")],
      ["power", t("host.power")],
      ["network", t("host.network")],
    ].forEach(([key, label]) => {
      const box = el("button", "host-stat host-stat-button"),
        eyebrow = el("span", "eyebrow", label),
        value = el("strong", "", "–"),
        note = el("small", "", label);
      box.type = "button";
      box.dataset.hostInspect = id;
      box.dataset.hostMetric = key;
      box.setAttribute("aria-haspopup", "dialog");
      box.append(eyebrow, value, note);
      stats.append(box);
      statNodes[key] = { box, eyebrow, value, note };
    });
    body.append(stats);
    const disks = el("section", "host-disks"),
      diskHead = el("div", "host-disks-head");
    const diskTitle = el("h3", "", t("host.localDisks")),
      diskHelp = el("small", "", t("host.diskHelp"));
    diskHead.append(diskTitle, diskHelp);
    const diskList = el("div", "host-disk-list");
    disks.append(diskHead, diskList);
    body.append(disks);
    const load = el("div", "host-load"),
      loadCopy = el("div", "host-load-copy"),
      loadTitle = el("strong", "", ""),
      loadNote = el("small", "", ""),
      processButton = el("button", "host-processes", t("host.gpuProcesses"));
    processButton.type = "button";
    processButton.dataset.hostInspect = id;
    processButton.dataset.hostMetric = "processes";
    processButton.setAttribute("aria-haspopup", "dialog");
    loadCopy.append(loadTitle, loadNote);
    load.append(loadCopy, processButton);
    body.append(load);
    article.append(header, alert, body);
    return {
      article,
      title,
      role,
      state,
      stateText,
      hostName,
      alert,
      body,
      metrics,
      stats: statNodes,
      diskTitle,
      diskHelp,
      diskList,
      diskKey: null,
      diskRows: new Map(),
      loadTitle,
      loadNote,
      processButton,
    };
  }
  function hostHistory(host, key) {
    const section = el("section", "diagnostic-list resource-history"),
      now = Date.now(),
      history = host.history || [],
      points = KudosTelemetry.historyPoints(history, key, now),
      measured = points.filter((p) => p.v !== null),
      plot = el("div", "history-plot"),
      axis = el("div", "history-y"),
      svg = sv("svg", {
        viewBox: "0 0 600 120",
        preserveAspectRatio: "none",
        "aria-hidden": "true",
      }),
      ticks = el("div", "history-x");
    section.append(el("h3", "", t("history.title")));
    axis.append(...["100 %", "50 %", "0 %"].map((s) => el("span", "", s)));
    ticks.append(...["−60 s", "−30 s", t("history.now")].map((s) => el("span", "", s)));
    svg.append(
      sv("path", {
        class: "history-grid",
        d: "M0 0 H600 M0 60 H600 M0 120 H600",
      }),
    );
    if (measured.length) {
      svg.append(
        sv("path", {
          class: "history-trace",
          d: KudosTelemetry.historyPath(history, key, now, 60000, 600, 120),
        }),
      );
      // Dots make a lone sample visible without inventing a continuous series.
      measured.forEach((p) =>
        svg.append(
          sv("circle", {
            class: "history-sample",
            cx: ((p.t - (now - 60000)) / 60000) * 600,
            cy: 120 - clamp(p.v) * 1.2,
            r: 2.3,
          }),
        ),
      );
    }
    plot.append(axis, svg, ticks);
    section.append(plot);
    section.append(
      el(
        "p",
        "diagnostic-note history-note",
        measured.length
          ? t("history.measured", { start: clock(now - 60000), end: clock(now), count: fmt(measured.length, { maximumFractionDigits: 0 }) })
          : t("history.empty"),
      ),
    );
    if (host.health?.stale || host.health?.ok === false)
      section.append(
        el(
          "p",
          "diagnostic-note history-stale",
          t("history.stale"),
        ),
      );
    return section;
  }
  function updateHostMetric(view, key, value, format, meta, note) {
    const row = view.metrics[key];
    setNodeText(row.value, format);
    setNodeText(row.meta, meta);
    setNodeText(row.note, note);
    const n = num(value);
    row.button.dataset.usageBand = band(n);
    row.button.dataset.usage = n === null ? "unknown" : String(clamp(n));
    row.meter.classList.toggle("unknown", n === null);
    row.fill.style.width = n === null ? "0%" : `${clamp(n)}%`;
    row.button.setAttribute(
      "aria-label",
      t("host.ariaMetric", {
        host: view.article.querySelector("h2").textContent,
        metric: row.label.textContent,
        reading: n === null ? t("host.notMeasured") : t("host.used", { value: format }),
      }),
    );
  }
  function hostAvailable(ram) {
    const explicit = num(ram?.available_gb);
    if (explicit !== null) return explicit;
    const total = num(ram?.total_gb),
      used = num(ram?.used_gb);
    return total === null || used === null ? null : Math.max(0, total - used);
  }
  function hostDiskRow(view, index) {
    const row = el("button", "host-disk");
    row.type = "button";
    row.dataset.hostInspect = view.article.dataset.host;
    row.dataset.hostMetric = "disk";
    row.dataset.hostDiskIndex = String(index);
    row.setAttribute("aria-haspopup", "dialog");
    const label = el("strong", "disk-label", ""),
      meter = el("span", "host-meter"),
      fill = el("i"),
      value = el("span", "disk-value", "–");
    meter.append(fill);
    row.append(label, meter, value);
    return { row, label, meter, fill, value };
  }
  function renderHostDisks(view, disks) {
    const rows = Array.isArray(disks) ? disks : [],
      key = rows.map((d, i) => String(d.mount ?? d.label ?? i)).join("|");
    if (!rows.length) {
      const empty = view.diskList.querySelector(".host-disks-empty");
      if (empty) setNodeText(empty, t("host.disksUnavailable"));
      else view.diskList.replaceChildren(el("p", "host-disks-empty", t("host.disksUnavailable")));
      view.diskRows.clear();
      view.diskKey = null;
      return;
    }
    if (key !== view.diskKey) {
      view.diskRows.clear();
      const nodes = rows.map((d, i) => {
        const row = hostDiskRow(view, i);
        view.diskRows.set(String(d.mount ?? d.label ?? i), row);
        return row.row;
      });
      view.diskList.replaceChildren(...nodes);
      view.diskKey = key;
    }
    rows.forEach((d, i) => {
      const key = String(d.mount ?? d.label ?? i),
        row = view.diskRows.get(key),
        used = num(d.use_percent),
        free = num(d.free_gb),
        total = num(d.total_gb),
        use =
          used ??
          (free === null || total === null || total <= 0
            ? null
            : ((total - free) / total) * 100);
      const diskLabel = d.label || d.mount || t("host.diskFallback", { number: fmt(i + 1, { maximumFractionDigits: 0 }) });
      setNodeText(row.label, diskLabel);
      setNodeText(
        row.value,
        `${free === null ? "–" : fmt(Math.round(free), { maximumFractionDigits: 0 })} / ${total === null ? "–" : fmt(Math.round(total), { maximumFractionDigits: 0 })} GiB`,
      );
      row.meter.classList.toggle("unknown", use === null);
      row.fill.style.width = use === null ? "0%" : `${clamp(use)}%`;
      row.row.dataset.hostDiskIndex = String(i);
      row.row.setAttribute(
        "aria-label",
        t("host.diskAria", { label: diskLabel, free: gib(free), total: gib(total) }),
      );
    });
  }
  function renderHostPanel(id, d) {
    const host = hostPayload(id, d);
    if (!host) return null;
    const system = host.system || {},
      cpu = system.cpu || {},
      gpu = system.gpu || {},
      ram = system.ram || {},
      network = system.network || {},
      view = hostViews.get(id) || createHostPanel(id),
      health = host.health || {},
      age = num(health.age_seconds),
      hostName = system.identity?.hostname || system.hostname || id,
      hostAge = age === null && host.generated_at
        ? (Date.now() - instant(host.generated_at).valueOf()) / 1000
        : age;
    hostViews.set(id, view);
    setNodeText(view.title, host.label || id);
    setNodeText(view.role, ["agents-services", "model-server"].includes(host.role) ? t(`host.role.${host.role}`) : host.role || "");
    setNodeText(view.metrics.cpu.label, t("host.cpu"));
    setNodeText(view.metrics.cpu.caption, t("host.processor"));
    setNodeText(view.metrics.gpu.label, t("host.gpu"));
    setNodeText(view.metrics.gpu.caption, t("host.compute"));
    setNodeText(view.metrics.ram.label, t("host.ram"));
    setNodeText(view.metrics.ram.caption, t("host.sharedMemory"));
    setNodeText(view.diskTitle, t("host.localDisks"));
    setNodeText(view.diskHelp, t("host.diskHelp"));
    setNodeText(view.stats.thermal.eyebrow, t("host.thermal"));
    setNodeText(view.stats.power.eyebrow, t("host.power"));
    setNodeText(view.stats.network.eyebrow, t("host.network"));
    setNodeText(
      view.hostName,
      `${hostName}${host.generated_at ? ` · ${clock(host.generated_at, false)}` : ""}`,
    );
    const stale = health.stale === true,
      ok = health.ok !== false;
    view.article.classList.toggle("stale", stale);
    view.article.classList.toggle("offline", !ok);
    view.state.classList.toggle("stale", stale || !ok);
    setNodeText(
      view.stateText,
      !ok ? t("host.disconnected") : stale ? t("host.stale") : t("host.current"),
    );
    const ageText = hostAge === null
      ? t("host.ageUnknown")
      : t("host.age", { age: duration(Math.max(0, hostAge)) });
    view.alert.hidden = ok && !stale;
    setNodeText(
      view.alert,
      !ok
        ? t("host.unavailable", { host: host.label || id, age: ageText })
        : t("host.staleNotice", { age: ageText }),
    );
    const available = hostAvailable(ram),
      ramUsed = num(ram.used_percent),
      ramUsage = ramUsed ?? (
        num(ram.used_gb) !== null && num(ram.total_gb) !== null && num(ram.total_gb) > 0
          ? (num(ram.used_gb) / num(ram.total_gb)) * 100
          : null
      ),
      ramDesc = num(ram.used_gb) === null || num(ram.total_gb) === null
        ? t("host.telemetryMissing")
        : t("host.usedOfTotal", { used: gib(ram.used_gb), total: gib(ram.total_gb) });
    const processRows = hostGpuProcesses(id, gpu),
      gpuHealth = String(gpu.health_status ?? gpu.health ?? "").toLowerCase(),
      gpuBad = ["error", "unhealthy", "unsafe", "reset_required", "reset-required"].includes(gpuHealth),
      gpuUnavailable = ["unavailable", "not_configured", "not-configured", "unknown"].includes(gpuHealth) ||
        (!gpuHealth && ![gpu.utilization_percent, gpu.temperature, gpu.power_watts, gpu.memory_used_mb, gpu.memory_total_mb].some((v) => num(v) !== null) && processRows === null);
    const gpuNote = gpuBad
      ? hostStatusName(gpuHealth)
      : gpuUnavailable
        ? gpuHealth === "unavailable" ? hostStatusName(gpuHealth) : t("host.telemetryMissing")
        : ["partial", "stale"].includes(gpuHealth)
          ? hostStatusName(gpuHealth)
          : gpu.is_unified_memory === true
            ? t("host.gpuUma")
            : gpu.is_unified_memory === false && num(gpu.memory_total_mb) !== null && num(gpu.memory_used_mb) !== null
              ? t("host.gpuMemory", {
                  used: fmt(Math.round(num(gpu.memory_used_mb)), { maximumFractionDigits: 0 }),
                  total: fmt(Math.round(num(gpu.memory_total_mb)), { maximumFractionDigits: 0 }),
                })
              : t("host.gpuCompute");
    updateHostMetric(view, "cpu", cpu.usage_percent, pct(cpu.usage_percent),
      num(cpu.load_1) === null ? "–" : t("host.loadOneMinute", { value: fmt(num(cpu.load_1), { minimumFractionDigits: 1, maximumFractionDigits: 1 }) }),
      t("host.loadNow"));
    updateHostMetric(view, "gpu", gpuBad ? null : gpu.utilization_percent,
      pct(gpuBad ? null : gpu.utilization_percent),
      processRows === null ? t("host.processUnavailable") : t(processRows.length === 1 ? "host.processCountOne" : "host.processCount", { count: fmt(processRows.length, { maximumFractionDigits: 0 }) }),
      gpuNote);
    view.metrics.gpu.button.classList.toggle("partial", gpuBad || gpuUnavailable);
    updateHostMetric(view, "ram", ramUsage, pct(ramUsage),
      t("host.availableMemory", { value: gib(available) }), ramDesc);
    view.metrics.gpu.caption.textContent = gpuBad || gpuUnavailable ? t("host.telemetry") : t("host.compute");
    setNodeText(view.stats.thermal.value, temp(cpu.temperature));
    setNodeText(view.stats.thermal.note, t("host.gpuTemperatureValue", { value: temp(gpu.temperature) }));
    setNodeText(view.stats.power.value,
      num(gpu.power_watts) === null ? "–" : `${fmt(num(gpu.power_watts), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} W`);
    setNodeText(view.stats.power.note, t("host.powerNotWall"));
    setNodeText(view.stats.network.value,
      `↓ ${safeRate(network.rx_bytes_sec)} · ↑ ${safeRate(network.tx_bytes_sec)}`);
    setNodeText(view.stats.network.note, network.interface || t("host.networkRate"));
    view.stats.thermal.box.setAttribute("aria-label", t("host.thermalCache", { host: host.label || id }));
    view.stats.power.box.setAttribute("aria-label", t("host.powerCache", { host: host.label || id }));
    view.stats.network.box.setAttribute("aria-label", t("host.networkCache", { host: host.label || id }));
    renderHostDisks(view, (system.disks || []).filter((disk) => !disk.is_remote));
    const processCount = processRows === null ? null : processRows.length,
      model = system.model || {},
      modelAvailable = hasModelTelemetry(host);
    setNodeText(view.loadTitle, modelAvailable
      ? model.name || t("host.modelUnavailable")
      : t("host.gpuProcesses"));
    setNodeText(view.loadNote, modelAvailable
      ? t("host.modelRequests", {
          running: num(model.requests_running) === null ? "–" : fmt(model.requests_running, { maximumFractionDigits: 0 }),
          waiting: num(model.requests_waiting) === null ? "–" : fmt(model.requests_waiting, { maximumFractionDigits: 0 }),
          speed: num(model.generation_tokens_per_second) === null ? "–" : fmt(model.generation_tokens_per_second, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
          cache: pct(model.kv_cache_percent).replace(" ", "\u00a0"),
        }) + (model.health && model.health !== "ok" ? t("host.modelPartial") : "")
      : processCount === null
        ? t("host.processUnavailable")
        : t("host.processMetrics", { count: fmt(processCount, { maximumFractionDigits: 0 }) }));
    view.processButton.dataset.hostMetric = modelAvailable ? "model" : "processes";
    setNodeText(view.processButton, modelAvailable ? t("host.modelUse") : t("host.gpuProcesses"));
    view.processButton.disabled = !modelAvailable && processRows === null;
    view.processButton.setAttribute("aria-label", modelAvailable
      ? t("host.ariaModelUse", { host: host.label || id })
      : t("host.processesCached", { host: host.label || id }));
    return view;
  }
  function renderHosts(d) {
    const entries = hostEntries(d),
      ids = new Set(entries.map(([id]) => id)),
      container = $("#host-panels");
    for (const [id, view] of hostViews) {
      if (!ids.has(id)) {
        view.article.remove();
        hostViews.delete(id);
      }
    }
    entries.forEach(([id], index) => {
      const view = renderHostPanel(id, d);
      if (!view) return;
      const current = container.children[index];
      if (current !== view.article) container.insertBefore(view.article, current || null);
    });
  }
  function openHostInspector(id, key, trigger, diskIndex) {
    const host = hostPayload(id);
    if (!host) return;
    const system = host.system || {},
      cpu = system.cpu || {},
      gpu = system.gpu || {},
      ram = system.ram || {},
      network = system.network || {},
      label = host.label || id,
      box = $("#inspector-content"),
      rows = [];
    let title = t("inspector.hostTitle"),
      kicker = `${label.toUpperCase()} · ${t("inspector.cached")}`,
      note = t("inspector.hostCachedNote");
    if (key === "cpu") {
      title = t("inspector.cpuTitle");
      rows.push(
        metric(t("inspector.usage"), pct(cpu.usage_percent)),
        metric(
          t("inspector.cpuLoad"),
          num(cpu.load_1) === null ? "–" : fmt(cpu.load_1, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
        ),
        metric(t("inspector.cpuTemperature"), temp(cpu.temperature)),
      );
    } else if (key === "gpu" || key === "processes") {
      title = key === "processes" ? t("inspector.gpuProcessesTitle") : t("inspector.gpuTitle");
      const gh = String(gpu.health_status ?? gpu.health ?? "unknown");
      rows.push(
        metric(t("inspector.gpuUse"), pct(gpu.utilization_percent)),
        metric(t("inspector.state"), hostStatusName(gh)),
        metric(t("inspector.gpuTemperature"), temp(gpu.temperature)),
        metric(
          t("inspector.gpuPower"),
          num(gpu.power_watts) === null
            ? "–"
            : `${fmt(num(gpu.power_watts), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} W`,
        ),
        metric(
          t("inspector.gpuClockCap"),
          num(gpu.cap_mhz) === null
            ? "–"
            : `${fmt(Math.round(num(gpu.cap_mhz)), { maximumFractionDigits: 0 })} MHz`,
        ),
      );
      if (gpu.is_unified_memory === true) {
        rows.push(
          metric(
            t("inspector.sharedMemory"),
            t("inspector.sharedUma"),
            true,
            t("inspector.gpuUmaExplain"),
          ),
        );
        rows.push(
          metric(
            t("inspector.gpuMemory"),
            `${num(gpu.memory_used_mb) === null ? "–" : fmt(Math.round(num(gpu.memory_used_mb)), { maximumFractionDigits: 0 })} / ${num(gpu.memory_total_mb) === null ? "–" : fmt(Math.round(num(gpu.memory_total_mb)), { maximumFractionDigits: 0 })} MiB`,
            true,
            t("inspector.gpuUmaSeparate"),
          ),
        );
      }
      const processes = hostGpuProcesses(id, gpu),
        section = el("section", "diagnostic-list wide");
      section.append(
        el("h3", "", t("inspector.gpuProcessesTitle")),
        el(
          "p",
          "diagnostic-note",
          processes === null
            ? t("inspector.gpuProcessesMissing")
            : processes.length
              ? KudosTelemetry.gpuLeaders(processes).field === "gpu_util"
                ? t("inspector.gpuLargestCompute")
                : t("inspector.gpuLargestMemory")
              : t("inspector.gpuNoneDetected"),
        ),
      );
      if (Array.isArray(processes) && processes.length) {
        const list = el("ol", "process-list");
        processes.slice(0, 12).forEach((p) => {
          const item = el("li");
          item.append(
            el("span", "process-name", p.name || t("inspector.unknownProcessName")),
            el(
              "span",
              "process-pid",
              num(p.gpu_util) === null
                ? t("host.gpu")
                : `${fmt(Math.round(num(p.gpu_util)), { maximumFractionDigits: 0 })} %`,
            ),
            el(
              "b",
              "",
              num(p.memory_mb) === null
                ? "–"
                : `${fmt(num(p.memory_mb) / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} GiB`,
            ),
          );
          list.append(item);
        });
        section.append(list);
      }
      rows.push(section);
    } else if (key === "ram") {
      title = t("inspector.ramTitle");
      const available = hostAvailable(ram);
      rows.push(
        metric(t("inspector.used"), gib(ram.used_gb)),
        metric(t("inspector.available"), gib(available)),
        metric(t("inspector.total"), gib(ram.total_gb)),
        metric(t("inspector.usage"), pct(ram.used_percent)),
        metric(
          t("inspector.sharedMemory"),
          gpu.is_unified_memory === true ? t("inspector.yes") : gpu.is_unified_memory === false ? t("inspector.no") : t("inspector.notConfirmed"),
          true,
          t("inspector.ramUmaNote"),
        ),
      );
    } else if (key === "network") {
      title = t("inspector.network");
      rows.push(
        metric(t("inspector.receive"), safeRate(network.rx_bytes_sec)),
        metric(t("inspector.transmit"), safeRate(network.tx_bytes_sec)),
        metric(t("inspector.interface"), network.interface || "–", true),
      );
    } else if (key === "thermal" || key === "power") {
      title = key === "thermal" ? t("inspector.thermalTitle") : t("inspector.gpuPower");
      rows.push(
        metric(t("host.cpu"), temp(cpu.temperature)),
        metric(t("host.gpu"), temp(gpu.temperature)),
        metric(
          t("inspector.gpuPower"),
          num(gpu.power_watts) === null
            ? "–"
            : `${fmt(num(gpu.power_watts), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} W`,
          true,
          t("inspector.wallPowerNote"),
        ),
      );
    } else if (key === "disk") {
      const disk = (system.disks || []).filter((d) => !d.is_remote)[
        Number(diskIndex)
      ];
      if (!disk) return;
      title = disk.label || disk.mount || t("inspector.diskTitle");
      rows.push(
        metric(t("inspector.used"), pct(disk.use_percent)),
        metric(t("inspector.free"), gib(disk.free_gb)),
        metric(t("inspector.total"), gib(disk.total_gb)),
        metric(t("inspector.mount"), disk.mount || "–", true),
      );
    }
    if (key === "cpu" || key === "ram") {
      const field = key === "cpu" ? "cpu_percent" : "memory_mb",
        list = key === "cpu" ? system.processes?.cpu : system.processes?.memory;
      rows.push(
        processList(
          key === "cpu"
            ? t("inspector.cpuLeaders")
            : t("inspector.memoryLeaders"),
          list === null || list === undefined ? null : KudosTelemetry.leaders(list, field),
          (r) =>
            key === "cpu"
              ? pct(r.cpu_percent)
              : `${num(r.memory_mb) === null ? "–" : fmt(num(r.memory_mb), { maximumFractionDigits: 0 })} MiB`,
          key === "cpu"
            ? t("inspector.processCpuNote")
            : t("inspector.processMemoryNote"),
        ),
      );
      note = t("inspector.processSample", {
        time: clock(system.processes?.sampled_at || host.generated_at),
        stale: host.health?.stale || host.health?.ok === false ? t("inspector.processStale") : "",
      });
    }
    if (key === "network")
      note = t("inspector.networkProcessNote");
    if (key === "disk")
      rows.push(
        processList(
          t("inspector.processIoTitle"),
          system.processes?.io == null ? null : KudosTelemetry.leaders(
            (system.processes?.io || []).map((r) => ({
              ...r,
              io_bytes_sec:
                num(r.read_bytes_sec) === null ||
                num(r.write_bytes_sec) === null
                  ? null
                  : num(r.read_bytes_sec) + num(r.write_bytes_sec),
            })),
            "io_bytes_sec",
          ),
          (r) => safeRate(r.io_bytes_sec),
          t("inspector.processIoNote"),
        ),
      );
    if (key === "thermal" || key === "power")
      note = t("inspector.deviceNote");
    if (key === "model") {
      title = t("inspector.modelUsage");
      const m = system.model || {};
      rows.push(
        metric(t("inspector.model"), m.name || t("inspector.modelUnavailable"), true),
        metric(t("inspector.state"), hostStatusName(m.health)),
        metric(t("inspector.context"), m.context_length),
        metric(t("inspector.running"), num(m.requests_running) === null ? "–" : fmt(m.requests_running, { maximumFractionDigits: 0 })),
        metric(t("inspector.queued"), num(m.requests_waiting) === null ? "–" : fmt(m.requests_waiting, { maximumFractionDigits: 0 })),
        metric(t("inspector.kvCache"), pct(m.kv_cache_percent)),
        metric(
          t("inspector.input"),
          num(m.prompt_tokens_per_second) === null
            ? "–"
            : `${fmt(num(m.prompt_tokens_per_second), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} tok/s`,
        ),
        metric(
          t("inspector.generation"),
          num(m.generation_tokens_per_second) === null
            ? "–"
            : `${fmt(num(m.generation_tokens_per_second), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} tok/s`,
        ),
      );
      note = t("inspector.modelNote", { time: clock(m.sampled_at) });
    }
    if (["cpu", "gpu", "processes", "ram"].includes(key))
      rows.unshift(hostHistory(host, key === "processes" ? "gpu" : key));
    box.replaceChildren(...rows, el("p", "diagnostic-note wide", note));
    ui.hostInspector = { id, key, trigger, diskIndex };
    setText("#inspector-kicker", kicker);
    setText("#inspector-title", title);
    showDialog($("#inspector"), trigger);
  }
  function createAgentResourceRow() {
    const card = el("article", "agent-resource-card"),
      heading = el("div", "agent-resource-heading"),
      name = el("strong"),
      kind = el("span"),
      meta = el("div", "agent-resource-meta"),
      values = Array.from({ length: 7 }, () => el("span")),
      reasons = el("div", "agent-resource-reasons"),
      reasonValues = [el("span"), el("span")];
    heading.append(name, kind);
    meta.append(...values);
    reasons.append(...reasonValues);
    card.append(heading, meta, reasons);
    return { card, name, kind, values, reasonValues };
  }
  function renderAgentResources(payload) {
    const enabled = featureEnabled(ui.data, "resources");
    $("#agent-resources-section").hidden = !enabled;
    if (!enabled) return;
    const status = $("#agent-resource-status"),
      rowsBox = $("#agent-resource-rows"),
      warningsBox = $("#agent-resource-warnings"),
      allResources = Array.isArray(payload?.resources) ? payload.resources : [],
      isClosed = (resource) => ["closed", "exited"].includes(resource.state) &&
        !(num(resource.cpu_percent) > 0) && !(num(resource.memory_bytes) > 0),
      closedCount = allResources.filter(isClosed).length,
      resources = allResources.filter((resource) =>
        $("#agent-resource-show-closed").checked || !isClosed(resource))
        .sort((a, b) => Number(isClosed(a)) - Number(isClosed(b))),
      warnings = Array.isArray(payload?.warnings) ? payload.warnings : [],
      sampledAt = Date.parse(payload?.sampled_at || ""),
      elapsed = (Date.now() - ui.agentResourcesReceivedAt) / 1000,
      reportedAge = num(payload?.age_seconds),
      rawAge = Number.isFinite(sampledAt)
        ? (Date.now() - sampledAt) / 1000
        : reportedAge === null
          ? null
          : reportedAge + Math.max(0, elapsed),
      age = rawAge === null ? null : Math.max(0, Math.floor(rawAge)),
      available = payload?.available === true,
      stale = available && (
        payload?.status === "stale" || payload?.stale === true ||
        (rawAge !== null && (rawAge > AGENT_RESOURCE_STALE_AFTER_SECONDS || rawAge < -30)) ||
        !!ui.agentResourcesError
      ),
      state = available ? (stale ? "stale" : "available") : "unavailable",
      ageText = age === null ? t("agentResources.ageUnknown") : t("agentResources.age", { age: duration(age) });
    status.className = `resource-registry-status ${state}`;
    status.textContent = !available
      ? ui.agentResourcesError
        ? t("agentResources.connectionError", { error: ui.agentResourcesError })
        : t("agentResources.unavailable")
      : ui.agentResourcesError
        ? t("agentResources.lastKnown", { age: ageText, visible: fmt(resources.length, { maximumFractionDigits: 0 }), closed: fmt(closedCount, { maximumFractionDigits: 0 }) })
        : t("agentResources.updated", { state: t(stale ? "agentResources.stale" : "agentResources.refreshed"), age: ageText, visible: fmt(resources.length, { maximumFractionDigits: 0 }), closed: fmt(closedCount, { maximumFractionDigits: 0 }) });

    const occurrences = new Map(),
      keys = resources.map((resource, index) => {
        const base = String(resource.id ?? `${resource.platform || ""}:${resource.profile || ""}:${resource.task_id || index}`),
          occurrence = occurrences.get(base) || 0;
        occurrences.set(base, occurrence + 1);
        return occurrence ? `${base}#${occurrence}` : base;
      }),
      active = new Set(keys);
    for (const [key, row] of agentResourceRows) {
      if (!active.has(key)) {
        row.card.remove();
        agentResourceRows.delete(key);
      }
    }
    if (!resources.length) {
      if (!rowsBox.querySelector(".empty"))
        rowsBox.append(el("p", "empty", available
          ? t("agentResources.empty")
          : t("agentResources.waiting")));
    } else {
      rowsBox.querySelector(".empty")?.remove();
      resources.forEach((resource, index) => {
        const key = keys[index],
          row = agentResourceRows.get(key) || createAgentResourceRow(),
          memory = num(resource.memory_bytes),
          memoryText = memory === null
            ? "–"
            : memory >= 1024 ** 3
              ? `${fmt(memory / 1024 ** 3, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} GiB`
              : memory >= 1024 ** 2
                ? `${fmt(memory / 1024 ** 2, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} MiB`
                : memory >= 1024
                  ? `${fmt(memory / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} KiB`
                  : `${fmt(Math.round(memory), { maximumFractionDigits: 0 })} B`,
          meta = [
            t("agentResources.agent", { name: clip(resource.profile, 40) }),
            t("agentResources.task", { name: clip(resource.task_id, 52) }),
            t("agentResources.ageLabel", { age: num(resource.age_seconds) === null ? "–" : duration(resource.age_seconds) }),
            t("agentResources.cpu", { value: num(resource.cpu_percent) === null ? "–" : `${fmt(num(resource.cpu_percent), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %` }),
            t("agentResources.ram", { value: memoryText }),
            resource.pid == null ? "PID –" : `PID ${fmt(resource.pid, { maximumFractionDigits: 0 })}`,
            resource.managed === true ? t("agentResources.managed") : t("agentResources.unmanaged"),
          ];
        agentResourceRows.set(key, row);
        setNodeText(row.name, resource.label || t("agentResources.unnamed"));
        setNodeText(row.kind, `${resource.platform || "–"} · ${resource.kind || "–"}`);
        meta.forEach((text, i) => setNodeText(row.values[i], text));
        setNodeText(row.reasonValues[0], t("agentResources.state", { state: clip(resource.state, 60), reason: clip(resource.cleanup_reason || t("agentResources.noCleanupReason"), 90) }));
        setNodeText(row.reasonValues[1], resource.persistent
          ? t("agentResources.persistent", { reason: clip(resource.persistence_reason || t("agentResources.reasonUnknown"), 140) })
          : t("agentResources.notPersistent"));
        const current = rowsBox.children[index];
        if (current !== row.card) rowsBox.insertBefore(row.card, current || null);
      });
    }
    const warningsKey = JSON.stringify(warnings.slice(0, 50));
    if (warningsKey !== ui.agentResourceWarningsKey) {
      warningsBox.replaceChildren(
        ...warnings.slice(0, 50).map((warning) => el("li", "", warning)),
      );
      ui.agentResourceWarningsKey = warningsKey;
    }
  }
  function applyFeatureVisibility(d) {
    const agents = featureEnabled(d, "agents"),
      crons = featureEnabled(d, "crons"),
      logs = featureEnabled(d, "logs");
    $("#work-section").hidden = !(agents || crons);
    $("#agent-resources-section").hidden = !featureEnabled(d, "resources");
    $("#quota-control").hidden = !featureEnabled(d, "quotas");
    $("#logs-button").hidden = !logs;
    $("#chronicle-button").hidden = !(agents || crons);
    $("#attention-button").disabled = !logs;
  }
  function render(d) {
    const previousLanguage = KudosI18n.language();
    ui.data = d;
    KudosI18n.useDefault(d.ui?.language);
    languageSelect.value = KudosI18n.language();
    if (previousLanguage !== KudosI18n.language()) {
      KudosI18n.applyStatic(document);
      invalidateTranslatedViews();
    }
    setConnection(ui.connectionMode || "stale", ui.connectionTextKey || "connection.connecting");
    applyFeatureVisibility(d);
    setText("#version", `v${d.app?.version || "?"}`);
    renderAgentResources(ui.agentResources);
    renderCapacity();
    renderAttention();
    renderHosts(d);
    renderWork();
    renderTimeline();
    renderResources();
    renderStorage();
    renderServices();
    renderLogs();
    renderEvents();
    const resourcesEnabled = featureEnabled(d, "resources");
    if (resourcesEnabled && !ui.resourceFeatureWasEnabled) refreshAgentResources();
    ui.resourceFeatureWasEnabled = resourcesEnabled;
    if (ui.hostInspector && $("#inspector").open) {
      const a = ui.hostInspector,
        dialog = $("#inspector"),
        hadFocus = dialog.contains(document.activeElement);
      openHostInspector(a.id, a.key, a.trigger, a.diskIndex);
      if (hadFocus && !dialog.contains(document.activeElement)) $("#inspector-title").focus({ preventScroll: true });
    }
  }
  document.addEventListener("click", (e) => {
    const hostButton = e.target.closest("[data-host-inspect]");
    if (hostButton) {
      const id = hostButton.dataset.hostInspect,
        key = hostButton.dataset.hostMetric;
      if (!(key === "processes" && hostButton.disabled))
        openHostInspector(
          id,
          key,
          hostButton,
          hostButton.dataset.hostDiskIndex,
        );
    }
    const inspect = e.target.closest("[data-inspect]");
    if (inspect) openInspector(inspect.dataset.inspect, inspect);
    const close = e.target.closest("[data-close]");
    if (close) closeDialog($(`#${close.dataset.close}`));
  });
  $("#logs-button").addEventListener("click", (e) => {
    showDialog($("#logs-dialog"), e.currentTarget);
    refreshLogs();
  });
  $("#attention-button").addEventListener("click", (e) => {
    showDialog($("#logs-dialog"), e.currentTarget);
    refreshLogs();
  });
  $("#acknowledge-errors").addEventListener("click", acknowledgeLogErrors);
  $("#chronicle-button").addEventListener("click", (e) =>
    showDialog($("#chronicle-dialog"), e.currentTarget),
  );
  $("#services-button").addEventListener("click", (e) => {
    ui.hostInspector = null;
    const rows = ui.data?.system?.services || [],
      box = $("#inspector-content");
    box.replaceChildren();
    rows.forEach((s) =>
      box.append(
        metric(
          s.label || s.name,
          hostStatusName(s.status).toUpperCase(),
          false,
          [
            s.activity,
            num(s.memory_mb) !== null ? `${fmt(Math.round(s.memory_mb))} ${t("units.mib")}` : null,
            num(s.collector?.duration_ms) !== null
              ? `${s.collector.duration_ms} ms`
              : null,
            s.collector?.error,
          ]
            .filter(Boolean)
            .join(" \xB7 "),
        ),
      ),
    );
    setText("#inspector-kicker", t("inspector.serviceStatus"));
    setText("#inspector-title", t("services.title"));
    showDialog($("#inspector"), e.currentTarget);
  });
  $$(".log-filter").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.runtime) {
        ui.logRuntime = b.dataset.runtime;
        $$(".log-filter[data-runtime]").forEach((x) => {
          x.classList.toggle("active", x === b);
          x.setAttribute("aria-pressed", String(x === b));
        });
      }
      if (b.dataset.level) {
        ui.logLevel = b.dataset.level;
        $$(".log-filter[data-level]").forEach((x) => {
          x.classList.toggle("active", x === b);
          x.setAttribute("aria-pressed", String(x === b));
        });
      }
      if (b.dataset.source) {
        ui.logSource = b.dataset.source;
        $$(".log-filter[data-source]").forEach((x) => {
          x.classList.toggle("active", x === b);
          x.setAttribute("aria-pressed", String(x === b));
        });
      }
      if (b.dataset.status) {
        ui.logStatus = b.dataset.status;
        $$(".log-filter[data-status]").forEach((x) => {
          x.classList.toggle("active", x === b);
          x.setAttribute("aria-pressed", String(x === b));
        });
      }
      ui.lastLogKey = "";
      renderLogs();
    }),
  );
  $$(".event-filter").forEach((b) =>
    b.addEventListener("click", () => {
      ui.eventKind = b.dataset.kind;
      $$(".event-filter").forEach((x) => {
        x.classList.toggle("active", x === b);
        x.setAttribute("aria-pressed", String(x === b));
      });
      ui.lastEventKey = "";
      renderEvents();
    }),
  );
  $$("dialog").forEach((d) =>
    d.addEventListener("click", (e) => {
      if (e.target === d) closeDialog(d);
    }),
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $$("dialog[open]").forEach(closeDialog);
  });
  $("#agent-resource-show-closed").addEventListener("change", () => renderAgentResources(ui.agentResources));
  async function refreshAgentResources() {
    if (!featureEnabled(ui.data, "resources")) return;
    if (document.hidden) return;
    if (!navigator.onLine) {
      ui.agentResourcesError = t("agentResources.networkUnavailable");
      renderAgentResources(ui.agentResources);
      return;
    }
    if (ui.agentResourcesPending) return;
    ui.agentResourcesPending = true;
    const controller = new AbortController(),
      timeout = setTimeout(() => controller.abort(), RESOURCE_FETCH_TIMEOUT_MS);
    try {
      const response = await fetch("/api/agent-resources", {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw Error(t("agentResources.unavailableError"));
      const payload = await response.json();
      if (!payload || typeof payload !== "object" || !Array.isArray(payload.resources))
        throw Error(t("agentResources.invalidResponse"));
      if (payload.available === true || ui.agentResources?.available !== true) {
        ui.agentResources = payload;
        ui.agentResourcesReceivedAt = Date.now();
      }
      ui.agentResourcesError = payload.available === true || !ui.agentResources?.available
        ? ""
        : t("agentResources.noNewSample");
    } catch (error) {
      ui.agentResourcesError = error?.name === "AbortError"
        ? t("agentResources.timeout")
        : t("agentResources.apiError");
    } finally {
      clearTimeout(timeout);
      ui.agentResourcesPending = false;
      renderAgentResources(ui.agentResources);
    }
  }
  function stopStream() {
    if (ui.stream) {
      ui.stream.close();
      ui.stream = null;
    }
    if (ui.reconnectTimer) {
      clearTimeout(ui.reconnectTimer);
      ui.reconnectTimer = 0;
    }
  }
  function scheduleReconnect(ms = ui.retry) {
    if (document.hidden || !navigator.onLine) return;
    ui.reconnectTimer = setTimeout(() => {
      ui.reconnectTimer = 0;
      connect();
    }, ms);
  }
  function connect() {
    if (ui.stream || document.hidden || !navigator.onLine) return;
    const es = new EventSource("/api/events");
    ui.stream = es;
    es.addEventListener("snapshot", (e) => {
      ui.retry = 2e3;
      const incoming = JSON.parse(e.data);
      const changed = incoming.logs?.revision !== ui.data?.logs?.revision;
      render(mergeSnapshot(incoming));
      if (changed || !ui.data.logs?.records?.length) refreshLogs();
      setConnection("live", "connection.live");
    });
    es.addEventListener("reconnect", () => {
      es.close();
      ui.stream = null;
      scheduleReconnect(250);
    });
    es.onerror = () => {
      es.close();
      ui.stream = null;
      setConnection("stale", "connection.disconnected");
      const wait = ui.retry;
      ui.retry = Math.min(6e4, ui.retry * 2);
      scheduleReconnect(wait);
    };
  }
  fetch("/api/snapshot", { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw Error(r.status);
      return r.json();
    })
    .then((d) => {
      if (!ui.data || d.revision >= ui.data.revision) render(d);
      setConnection("live", "connection.live");
    })
    .catch(() => setConnection("stale", "connection.unavailable"));
  document.addEventListener("visibilitychange", () => {
    updateActivityMotion();
    if (document.hidden) stopStream();
    else {
      ui.retry = 2e3;
      connect();
      refreshAgentResources();
    }
  });
  window.addEventListener("offline", () => {
    stopStream();
    setConnection("stale", "connection.offline");
    ui.agentResourcesError = t("agentResources.networkUnavailable");
    renderAgentResources(ui.agentResources);
  });
  window.addEventListener("online", () => {
    ui.retry = 2e3;
    connect();
    refreshAgentResources();
  });
  connect();
  refreshAgentResources();
  setInterval(refreshAgentResources, 1e4);
  setInterval(() => {
    if (!document.hidden) renderAgentResources(ui.agentResources);
  }, 1e3);
})();
