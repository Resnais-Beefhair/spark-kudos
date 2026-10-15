const assert = require("node:assert/strict");
const { agentActivity } = require("../src/sisu_kudos/web/telemetry.js");
const now = Date.parse("2026-10-03T14:00:00Z");
const context = {
  now,
  generatedAt: "2026-10-03T13:59:45Z",
  collectorHealth: { ok: true, stale: false },
  sourceHealth: [{ source: "hermes", ok: true }, { source: "openclaw", ok: true }],
};
let checks = 0;
function expect(session, mode, label, extra = {}) {
  const labelKeys = {
    "Työskentelee": "activity.working",
    "Nettihaku": "activity.search",
    "Verkkoluku": "activity.reading",
    "Tiedostotyö": "activity.files",
    "Työkalukutsu": "activity.tool",
    "Odottaa": "activity.waiting",
    "Virhe": "activity.error",
    "Ei toimintaa": "activity.none",
    "Tieto vanhentunut": "activity.stale",
    "Skriptiajo": "activity.script",
  };
  const actual = agentActivity({ runtime: "hermes", ...session }, { ...context, ...extra });
  assert.equal(actual.mode, mode);
  assert.equal(actual.labelKey, labelKeys[label]);
  assert.equal(actual.animated, ["working", "search", "tool", "file"].includes(mode));
  checks++;
}
expect({ status: "running" }, "working", "Työskentelee");
// Old tool names/events must not turn current model work into a web search.
expect({ status: "running", current_tool: "web_search" }, "working", "Työskentelee");
expect({ status: "tool", current_tool: "web_search" }, "search", "Nettihaku");
expect({ status: "tool", current_tool: "functions.web_search" }, "search", "Nettihaku");
expect({ status: "tool", current_tool: "web_extract" }, "search", "Verkkoluku");
expect({ status: "tool", current_tool: "web_fetch", runtime: "openclaw" }, "search", "Verkkoluku");
for (const tool of ["read_file", "write_file", "patch", "search_files", "read", "write", "edit"]) {
  expect({ status: "tool", current_tool: tool }, "file", "Tiedostotyö");
}
for (const tool of ["tool_call", "execute_code", "terminal", "delegate_task", "unknown_search", "browser_exec", "", "some.web_search"] ) {
  expect({ status: "tool", current_tool: tool, events: [{ kind: "DONE", tool_name: "web_search" }] }, "tool", "Työkalukutsu");
}
// Long model work can be real; session age is not collector freshness.
expect({ runtime: "openclaw", status: "running", age_seconds: 3600 }, "working", "Työskentelee");
expect({ status: "waiting", current_tool: "web_search" }, "waiting", "Odottaa");
expect({ status: "error" }, "error", "Virhe");
expect({ status: "idle" }, "unknown", "Ei toimintaa");
expect({ status: "unknown" }, "unknown", "Ei toimintaa");
expect({ status: "running" }, "stale", "Tieto vanhentunut", { generatedAt: "2026-10-03T13:59:14Z" });
expect({ status: "tool", current_tool: "web_search" }, "stale", "Tieto vanhentunut", { generatedAt: "bad" });
expect({ status: "running" }, "stale", "Tieto vanhentunut", { collectorHealth: { ok: false } });
expect({ status: "running" }, "stale", "Tieto vanhentunut", { collectorHealth: { ok: true, stale: true } });
expect({ status: "running" }, "stale", "Tieto vanhentunut", { sourceHealth: [{ source: "hermes", ok: false }] });
expect({ status: "running" }, "stale", "Tieto vanhentunut", { sourceHealth: [] });
expect({ status: "running" }, "stale", "Tieto vanhentunut", { generatedAt: "2026-10-03T14:01:00Z" });
expect({ status: "running" }, "stale", "Tieto vanhentunut", { collectorHealth: undefined });
const cronContext = {sourceHealth: [{source: "hermes-cron", ok: true}]};
const script = {status: "running", work_kind: "cron", execution_kind: "script"};
expect(script, "tool", "Skriptiajo", cronContext);
expect({...script, stale: true}, "stale", "Tieto vanhentunut", cronContext);
expect(script, "stale", "Tieto vanhentunut"); // normal session health is NOT cron health
expect({...script, execution_kind: "agent", status: "tool", current_tool: "web_search"}, "search", "Nettihaku", cronContext);
expect(script, "stale", "Tieto vanhentunut", {...cronContext, collectorHealth: {ok: false}});
console.log(`activity pure rules: PASS (${checks} cases)`);
