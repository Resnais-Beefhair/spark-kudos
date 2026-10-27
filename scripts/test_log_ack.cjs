/* Exercise the actual dashboard acknowledgement functions, without a DOM. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/sisu_kudos/web/app.js"), "utf8");
const start = source.indexOf("  function logSignature(");
const end = source.indexOf("  const LIVE_AGENT_ERROR_SECONDS", start);
assert.ok(start >= 0 && end > start);
let current = [];
const storage = new Map();
const context = vm.createContext({
  LOG_ACK_KEY: "test-log-ack",
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
  },
  logErrors: () => current,
  renderAttention() {},
  setText() {},
  t: (key) => key,
  fmt: String,
});
vm.runInContext(source.slice(start, end), context);
const run = (code) => vm.runInContext(code, context);
const acknowledge = (rows) => { current = rows; run("acknowledgeLogErrors()"); };
const unread = (rows) => { current = rows; return run("unreadLogErrors()"); };
const base = {
  timestamp: "2026-01-19T10:35:51.497Z", runtime: "hermes", agent: "Example worker",
  source: "errors", level: "ERROR", component: "asyncio", status: "recorded",
};
const a = { ...base, id: "session-event", message: "client session" };
const b = { ...base, id: "connector-event", message: "connector" };
const c = { ...base, id: "new-distinct-event", message: "new event" };

// A bounded/fairly balanced history can alternate peers at exactly the same time.
acknowledge([a]);
assert.equal(unread([b]), 1, "a genuinely unseen peer must still alert");
acknowledge([b]);
assert.equal(unread([a]), 0, "an acknowledged peer must not re-alert after history rotation");
assert.equal(unread([b]), 0);
for (let i = 0; i < 20; i++) {
  acknowledge(i % 2 ? [a] : [b]);
  assert.equal(unread(i % 2 ? [b] : [a]), 0, "repeated rotations must retain peer acknowledgements");
}
assert.equal(unread([c]), 1, "retaining known peers must not suppress a new same-time error");

// Recovered/evicted newer rows must not make the cursor move backwards.
const older = { ...a, id: "older-event", timestamp: "2026-01-19T10:30:00Z" };
acknowledge([older]);
assert.equal(unread([a, b]), 0, "an older visible sample must not rewind acknowledgement");
assert.equal(run("readLogAck().timestamp"), base.timestamp);

// Message enrichment, source unions and equivalent offsets do not change an ID.
assert.equal(unread([{ ...a, message: "more useful diagnostic", sources: ["agent", "errors"] }]), 0);
assert.equal(unread([{ ...a, timestamp: "2026-01-19T13:35:51.497+03:00" }]), 0);
const newer = { ...c, id: "newer-event", timestamp: "2026-01-19T10:36:00Z" };
assert.equal(unread([newer]), 1);
acknowledge([newer]);
assert.equal(unread([a, b, newer]), 0);

// Keep the legacy signature-only path monotonic and retain its same-time peers.
storage.clear();
const { id: _aId, ...legacyA } = a;
const { id: _bId, ...legacyB } = b;
acknowledge([legacyA]);
acknowledge([legacyB]);
assert.equal(unread([legacyA, legacyB]), 0);
acknowledge([{ ...legacyA, timestamp: older.timestamp }]);
assert.equal(unread([legacyA, legacyB]), 0);
assert.equal(unread([{ ...legacyA, message: "new legacy error" }]), 1);
console.log("log acknowledgements: PASS (rotating peers, monotonic cursor, enrichment, offsets, new errors, legacy)");
