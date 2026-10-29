/* Synthetic thermal presentation contract; no hardware is contacted. */
const assert = require('node:assert/strict');
const { thermalSummary, historyPath } = require('../src/sisu_kudos/web/telemetry.js');
const { hostEntries } = require('../src/sisu_kudos/web/hosts.js');
const now = Date.parse('2026-10-01T12:00:00Z');
const host = (cpu, gpu, patch = {}) => ({
  generated_at: new Date(now).toISOString(),
  health: { ok: true, stale: false },
  system: { cpu: { temperature: cpu }, gpu: { temperature: gpu } },
  history: [], ...patch,
});
assert.equal(thermalSummary(host(45, 69), now).peak, 69);
for (const [value, band] of [[0, 'safe'], [69, 'safe'], [70, 'warm'], [84.9, 'warm'], [85, 'hot']]) {
  const result = thermalSummary(host(value, null), now);
  assert.equal(result.peak, value);
  assert.equal(result.band, band);
  assert.equal(result.state, 'fresh');
}
for (const bad of [null, undefined, '', true, false, {}, [], '78', NaN, Infinity, -1, 151]) {
  const result = thermalSummary(host(bad, bad), now);
  assert.equal(result.peak, null);
  assert.equal(result.state, 'unknown');
  assert.equal(result.band, 'unknown');
}
for (const patch of [
  { health: { ok: false } }, { health: { ok: true, stale: true } },
  { generated_at: new Date(now - 46000).toISOString() },
  { generated_at: 'bad timestamp' },
]) {
  const result = thermalSummary(host(90, 80, patch), now);
  assert.equal(result.peak, 90); // retained, not a new healthy measurement
  assert.equal(result.state, 'stale');
  assert.equal(result.band, 'unknown');
}
assert.equal(thermalSummary(host(78, 70), now, false).state, 'stale');
const result = thermalSummary(host(78, 70, { history: [
  { at: new Date(now - 50000).toISOString(), cpu_temp: 50, gpu_temp: null },
  { at: new Date(now - 10000).toISOString(), cpu_temp: null, gpu_temp: 72 },
  { at: new Date(now - 5000).toISOString(), cpu_temp: null, gpu_temp: null },
  { at: new Date(now).toISOString(), cpu_temp: 82, gpu_temp: 69 },
] }), now);
assert.deepEqual(result.history.map(p => p.value), [50, 72, null, 82]);
assert.equal((historyPath(result.history, 'value', now, 300000, 96, 32).match(/M/g) || []).length, 3);
assert.equal(thermalSummary(host(null, 57), now).peak, 57);
assert.equal(thermalSummary(host(62, null), now).peak, 62);
const entries = hostEntries({ ui: { local_host_id: 'arbitrary' }, hosts: {
  remote: host(71, 78), arbitrary: host(45, 55),
} });
assert.deepEqual(entries.map(([id]) => id), ['arbitrary', 'remote']);
assert.equal(thermalSummary(entries[0][1], now).peak, 55);
assert.deepEqual(thermalSummary(host(50, 60), now).history, []);
console.log('Thermal max, color boundaries, missing/invalid data, freshness, gaps and arbitrary hosts: PASS');
