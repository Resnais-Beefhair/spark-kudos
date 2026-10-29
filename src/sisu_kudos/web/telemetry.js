/* Pure telemetry presentation rules, shared by browser and node regression tests. */
(function (root) {
  "use strict";
  const finite = (value) =>
    value === null ||
    value === undefined ||
    value === "" ||
    !Number.isFinite(Number(value))
      ? null
      : Number(value);
  function leaders(rows, field) {
    return Array.isArray(rows)
      ? rows
          .slice()
          .sort(
            (a, b) =>
              (finite(b[field]) ?? -Infinity) -
                (finite(a[field]) ?? -Infinity) ||
              (finite(b.memory_mb) ?? 0) - (finite(a.memory_mb) ?? 0) ||
              (a.pid ?? 0) - (b.pid ?? 0),
          )
      : [];
  }
  function gpuLeaders(rows) {
    const field =
      rows?.length && rows.every((r) => finite(r.gpu_util) !== null)
        ? "gpu_util"
        : "memory_mb";
    return { rows: leaders(rows, field), field };
  }
  function usageBand(value, warn = 75, crit = 90) {
    const n = finite(value);
    return n === null
      ? "unknown"
      : n >= crit
        ? "critical"
        : n >= warn
          ? "warning"
          : "normal";
  }
  function historyPoints(history, key, now = Date.now(), windowMs = 60000) {
    return (Array.isArray(history) ? history : [])
      .map((p) => ({ t: Date.parse(p.at), v: finite(p[key]) }))
      .filter(
        (p) => Number.isFinite(p.t) && p.t >= now - windowMs && p.t <= now,
      )
      .sort((a, b) => a.t - b.t);
  }
  function historyPath(
    history,
    key,
    now = Date.now(),
    windowMs = 60000,
    width = 180,
    height = 32,
  ) {
    const points = historyPoints(history, key, now, windowMs);
    let previous = null;
    return points
      .map((p) => {
        if (p.v === null) {
          previous = null;
          return "";
        }
        const command = previous === null || p.t - previous > 25000 ? "M" : "L";
        previous = p.t;
        return `${command}${(((p.t - (now - windowMs)) / windowMs) * width).toFixed(2)},${(height - (Math.max(0, Math.min(100, p.v)) / 100) * height).toFixed(2)}`;
      })
      .filter(Boolean)
      .join(" ");
  }
  function originLabel(value, fallback = "") {
    if (typeof value !== "string" || value === "" || ["—", "–"].includes(value))
      return fallback;
    return String(value);
  }
  function taskLabel(session, fallback = "") {
    return session.task || (session.runtime !== "hermes" ? session.title : "") || fallback;
  }
  function agentActivity(session, context = {}) {
    const state = (mode, labelKey, animated = false) => ({ mode, labelKey, animated });
    if (session.status === "error") return state("error", "activity.error");
    if (!["running", "tool", "waiting"].includes(session.status))
      return state("unknown", "activity.none");
    const age = (context.now ?? Date.now()) - Date.parse(context.generatedAt),
      source = context.sourceHealth?.find((h) => h.source ===
        (session.work_kind === "cron" ? `${session.runtime}-cron` : session.runtime));
    // Session age is NOT freshness: a real tool/model call can take minutes.
    if (
      !Number.isFinite(age) || age < -5000 || age > 45000 ||
      context.collectorHealth?.ok !== true || context.collectorHealth.stale ||
      source?.ok !== true || session.stale
    ) return state("stale", "activity.stale");
    if (session.status === "waiting") return state("waiting", "activity.waiting");
    if (session.work_kind === "cron" && session.execution_kind === "script")
      return state("tool", "activity.script", true);
    if (session.status === "running") return state("working", "activity.working", true);
    // Exact known tool names only. Never guess from titles, DONE events or wrappers.
    const tool = String(session.current_tool || "").replace(/^functions\./, "");
    if (["web_search", "x_search"].includes(tool))
      return state("search", "activity.search", true);
    if (["web_extract", "web_fetch"].includes(tool))
      return state("search", "activity.reading", true);
    if (["read_file", "write_file", "patch", "search_files", "read", "write", "edit"].includes(tool))
      return state("file", "activity.files", true);
    return state("tool", "activity.tool", true);
  }
  function thermalSummary(host, now = Date.now(), connected = true) {
    const reading = (value) => typeof value === "number" && Number.isFinite(value) &&
        value >= 0 && value <= 150 ? value : null,
      peak = (...values) => {
        const known = values.map(reading).filter((value) => value !== null);
        return known.length ? Math.max(...known) : null;
      },
      cpu = reading(host?.system?.cpu?.temperature),
      gpu = reading(host?.system?.gpu?.temperature),
      value = peak(cpu, gpu),
      age = now - Date.parse(host?.generated_at),
      stale = !connected || host?.health?.ok !== true || host?.health?.stale ||
        !Number.isFinite(age) || age < -5000 || age > 45000,
      state = value === null ? "unknown" : stale ? "stale" : "fresh";
    return {
      cpu, gpu, peak: value, state,
      band: state !== "fresh" ? "unknown" : value >= 85 ? "hot" : value >= 70 ? "warm" : "safe",
      history: (Array.isArray(host?.history) ? host.history : []).slice(-450).map((point) => ({
        at: point.at, value: peak(point.cpu_temp, point.gpu_temp),
      })),
    };
  }
  const api = {
    finite,
    leaders,
    gpuLeaders,
    usageBand,
    historyPoints,
    historyPath,
    originLabel,
    taskLabel,
    agentActivity,
    thermalSummary,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KudosTelemetry = api;
})(globalThis);
