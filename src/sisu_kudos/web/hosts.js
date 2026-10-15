/* Snapshot helpers for ordered, configurable host identifiers. */
(function (root) {
  "use strict";
  function hostEntries(snapshot) {
    const hosts = snapshot?.hosts;
    if (hosts && typeof hosts === "object" && !Array.isArray(hosts)) {
      const entries = Object.entries(hosts).map(([id, value]) => [
          id,
          { ...(value || {}), id },
        ]),
        localId = snapshot.ui?.local_host_id;
      if (localId !== undefined && Object.prototype.hasOwnProperty.call(hosts, localId)) {
        const localIndex = entries.findIndex(([id]) => id === String(localId));
        if (localIndex > 0) entries.unshift(...entries.splice(localIndex, 1));
      }
      return entries;
    }
    if (!snapshot?.system || typeof snapshot.system !== "object") return [];
    const id = String(snapshot.ui?.local_host_id || "local");
    const collector = snapshot.collector_health?.system || {};
    return [[id, {
      id,
      label: id,
      role: "",
      system: snapshot.system,
      history: Array.isArray(snapshot.history) ? snapshot.history : [],
      generated_at: snapshot.generated_at,
      health: {
        ok: collector.ok !== false,
        stale: collector.stale === true,
        age_seconds: collector.age_seconds ?? null,
      },
    }]];
  }
  function hostFor(snapshot, id) {
    return hostEntries(snapshot).find(([key]) => key === String(id))?.[1] || null;
  }
  function hasModelTelemetry(host) {
    const model = host?.system?.model;
    return !!model && typeof model === "object" && Object.keys(model).length > 0;
  }
  function featureEnabled(snapshot, name) {
    return snapshot?.ui?.features?.[name] === true;
  }
  const api = { hostEntries, hostFor, hasModelTelemetry, featureEnabled };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KudosHosts = api;
})(globalThis);
