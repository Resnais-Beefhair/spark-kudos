const assert = require("node:assert/strict");
const u = require("../src/sisu_kudos/web/telemetry.js");
const rows = [
  { pid: 1, cpu_percent: 3, memory_mb: 50 },
  { pid: 2, cpu_percent: 90, memory_mb: 2 },
  { pid: 3, cpu_percent: null, memory_mb: 80 },
];
assert.deepEqual(
  u.leaders(rows, "cpu_percent").map((r) => r.pid),
  [2, 1, 3],
);
assert.deepEqual(
  u.leaders(rows, "memory_mb").map((r) => r.pid),
  [3, 1, 2],
);
assert.deepEqual(
  rows.map((r) => r.pid),
  [1, 2, 3],
);
assert.equal(u.gpuLeaders(rows).field, "memory_mb");
assert.equal(
  u.gpuLeaders([{ gpu_util: 0 }, { gpu_util: 70 }]).rows[0].gpu_util,
  70,
);
assert.equal(u.finite(null), null);
assert.equal(u.finite("nan"), null);
const now = Date.parse("2026-10-01T12:01:00Z");
assert.equal(
  u.historyPath(
    [
      { at: "2026-10-01T12:00:00Z", cpu: 0 },
      { at: "2026-10-01T12:00:10Z", cpu: 100 },
      { at: "2026-10-01T12:00:20Z", cpu: null },
      { at: "2026-10-01T12:00:30Z", cpu: 50 },
    ],
    "cpu",
    now,
  ),
  "M0.00,32.00 L30.00,0.00 M90.00,16.00",
);
assert.equal(
  u.originLabel("Example workspace / #agents / Dashboard"),
  "Example workspace / #agents / Dashboard",
);
assert.equal(u.originLabel("Discord"), "Discord");
assert.equal(u.originLabel({ label: "Unexpected object" }, "Unknown origin"), "Unknown origin");
assert.equal(u.originLabel("–", "Origin not available"), "Origin not available");
// Current-use colors: one shared rule for the reading and its fill.
for (const [value, expected] of [
  [0, "normal"],
  [8, "normal"],
  [14, "normal"],
  [74.99, "normal"],
  [75, "warning"],
  [89.99, "warning"],
  [90, "critical"],
  [93, "critical"],
  [100, "critical"],
  [null, "unknown"],
  [undefined, "unknown"],
  ["", "unknown"],
  ["bad", "unknown"],
]) {
  assert.equal(u.usageBand(value), expected);
}
assert.deepEqual(
  u.historyPoints(
    [
      { at: "bad", cpu: 8 },
      { at: "2026-10-01T12:01:01Z", cpu: 8 },
      { at: "2026-10-01T11:59:59Z", cpu: 8 },
    ],
    "cpu",
    now,
  ),
  [],
);
assert.equal(
  u.historyPath(
    [
      { at: "2026-10-01T12:00:00Z", cpu: 0 },
      { at: "2026-10-01T12:00:30Z", cpu: 100 },
    ],
    "cpu",
    now,
  ),
  "M0.00,32.00 M90.00,0.00",
);
assert.equal(
  u.historyPath([{ at: "2026-10-01T12:00:30Z", cpu: 0 }], "cpu", now),
  "M90.00,32.00",
);
console.log("telemetry pure rules: PASS");
// Historical conversation titles are context, never evidence of current work.
assert.equal(u.taskLabel({ runtime: "hermes", title: "Old topic" }, "Unknown task"), "Unknown task");
assert.equal(u.taskLabel({ runtime: "hermes", title: "Old topic", task: "Current task" }), "Current task");
assert.equal(u.taskLabel({ runtime: "openclaw", title: "Observed task" }), "Observed task");
assert.equal(u.taskLabel({ runtime: "hermes", title: "Old topic" }), "");
