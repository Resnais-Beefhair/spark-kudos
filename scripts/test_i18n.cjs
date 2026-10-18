const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const web = path.join(root, "src/sisu_kudos/web");
const i18nSource = fs.readFileSync(path.join(web, "i18n.js"), "utf8");
const appSource = fs.readFileSync(path.join(web, "app.js"), "utf8");
const htmlSource = fs.readFileSync(path.join(web, "index.html"), "utf8");
const hosts = require(path.join(web, "hosts.js"));

function memoryStorage(initial = null, fail = false) {
  const values = new Map(initial === null ? [] : [["kudos.ui-language.v1", initial]]);
  return {
    getItem(key) {
      if (fail) throw new Error("storage blocked");
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      if (fail) throw new Error("storage blocked");
      values.set(key, String(value));
    },
    value(key) { return values.get(key) ?? null; },
  };
}

function createI18n(saved = null, storageBlocked = false) {
  const document = { documentElement: { lang: "" } };
  const storage = memoryStorage(saved, storageBlocked);
  const sandbox = { module: { exports: {} }, document };
  Object.defineProperty(sandbox, "localStorage", {
    get() {
      if (storageBlocked) throw new Error("storage blocked");
      return storage;
    },
  });
  vm.runInNewContext(i18nSource, sandbox, { filename: "i18n.js" });
  return { api: sandbox.module.exports, document, storage };
}

const { catalog } = createI18n().api;
const enKeys = Object.keys(catalog.en).sort();
const fiKeys = Object.keys(catalog.fi).sort();
assert.deepEqual(fiKeys, enKeys, "English and Finnish catalogs must have identical keys");
assert.ok(enKeys.length > 300, "catalog should cover the full UI, not a partial subset");
for (const key of enKeys) {
  assert.equal(typeof catalog.en[key], "string", `${key} English message must be text`);
  assert.equal(typeof catalog.fi[key], "string", `${key} Finnish message must be text`);
  const variables = (text) => [...text.matchAll(/\{([a-zA-Z0-9_]+)\}/g)].map((match) => match[1]).sort();
  assert.deepEqual(variables(catalog.fi[key]), variables(catalog.en[key]), `${key} interpolation placeholders differ`);
}

const staticKeys = [...htmlSource.matchAll(/data-i18n(?:-aria|-title)?="([^"]+)"/g)].map((match) => match[1]);
for (const key of staticKeys) assert.ok(catalog.en[key], `HTML references missing message ${key}`);
const literalKeys = [...appSource.matchAll(/\bt\(["'`]([^"'`]+)["'`]/g)].map((match) => match[1]).filter((key) => !key.includes("${"));
for (const key of literalKeys) assert.ok(catalog.en[key], `app.js references missing message ${key}`);
for (const status of ["critical", "warning", "unknown", "normal"])
  assert.ok(catalog.en[`resources.status.${status}`] && catalog.fi[`resources.status.${status}`], `resource status ${status} must be translated`);
assert.match(htmlSource, /<html lang="en">/);
assert.match(htmlSource, /id="language-select"[\s\S]*?<option value="en">[\s\S]*?<option value="fi">/);
assert.doesNotMatch(appSource, /MutationObserver|\.innerHTML\s*=|\["sisu",\s*"kide"\]|id\s*===\s*["'](?:sisu|kide)["']/);
assert.doesNotMatch(i18nSource, /MutationObserver|\.innerHTML\s*=/);
// safe_event emits only agent events; cron cards have their own ledger.
assert.doesNotMatch(appSource, /event\.(?:work_kind|execution_kind)/);

{
  const { api, document } = createI18n();
  assert.equal(api.language(), "en");
  assert.equal(document.documentElement.lang, "en");
  assert.equal(api.hasExplicitPreference(), false);
  assert.equal(api.useDefault("fi"), "fi");
  assert.equal(document.documentElement.lang, "fi");
  assert.equal(api.useDefault("en"), "en");
  assert.equal(api.setLanguage("fi", { explicit: true }), "fi");
  assert.equal(api.hasExplicitPreference(), true);
  assert.equal(api.useDefault("en"), "fi", "site default must not override a saved choice");
  assert.equal(api.locale(), "fi-FI");
  assert.match(api.formatPercent(50), /^50\s+%$/);
  assert.equal(api.formatPercent(null), "–");
  assert.equal(api.t("units.gib"), "GiB");
  api.setLanguage("en");
  assert.equal(api.formatPercent(50), "50%");
  assert.equal(api.t("units.gib"), "GiB");
}
{
  const { api, document, storage } = createI18n("en");
  assert.equal(api.hasExplicitPreference(), true);
  assert.equal(api.useDefault("fi"), "en", "saved English must override the site default");
  assert.equal(document.documentElement.lang, "en");
  api.setLanguage("fi", { explicit: true });
  assert.equal(storage.value("kudos.ui-language.v1"), "fi");
  assert.equal(document.documentElement.lang, "fi");
}
{
  const { api } = createI18n(null, true);
  assert.equal(api.language(), "en");
  assert.equal(api.useDefault("fi"), "fi", "blocked storage must not break site defaults");
  assert.equal(api.formatNumber(null), "–");
  assert.notEqual(api.formatNumber(1234.5), createI18n().api.formatNumber(1234.5));
}
{
  const { api } = createI18n();
  api.setLanguage("fi");
  const node = {
    dataset: {
      i18n: "host.unavailable",
      i18nAria: "host.ariaModelUse",
      i18nTitle: "host.chartTitle",
    },
    attrs: {},
    textContent: "",
    title: "",
    hasAttribute(name) { return name === "data-i18n" || name === "data-i18n-aria" || name === "data-i18n-title"; },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
  const fakeRoot = { querySelectorAll: () => [node] };
  api.applyStatic(fakeRoot);
  assert.equal(node.textContent, api.t("host.unavailable"));
  assert.equal(node.attrs["aria-label"], api.t("host.ariaModelUse"));
  assert.equal(node.title, api.t("host.chartTitle"));
  const userText = "<img src=x onerror=alert(1)>";
  assert.equal(api.t("work.started", { time: userText }), `alkanut ${userText}`);
  assert.equal(api.knownProductText("Discord connection recovered"), api.t("logs.discordRecovered"));
  const arbitrary = "Discord connection recovered by agent team";
  assert.equal(api.knownProductText(arbitrary), arbitrary, "unknown content must remain unchanged");
}

{
  const { api } = createI18n();
  api.setLanguage("fi");
  const apiError = { message: "REST fallback", diagnostic: { kind: "api_request",
    cause: "service_unavailable", http_status: 503, attempt: 3, max_attempts: 3,
    error_type: "InternalServerError" } };
  assert.equal(api.logMessage(apiError), "Mallin API-pyyntö epäonnistui: HTTP 503 (palvelu ei saatavilla); yritys 3/3; InternalServerError");
  assert.equal(api.logMessage({ diagnostic: { kind: "vision", cause: "broken_pipe", errno: 32 } }),
    "Kuvan analysointi epäonnistui: katkennut tiedonsiirtoputki (EPIPE); errno 32");
  assert.match(api.logMessage({ diagnostic: { kind: "api_request", cause: "rate_limit",
    http_status: 429, retries_exhausted: 3 } }), /HTTP 429.*käyttö- tai nopeusraja.*käytetty \(3\)/);
  assert.equal(api.logMessage({ message: "Useful fallback", diagnostic: { kind: "unrecognized" } }), "Useful fallback");
  assert.equal(api.logMessage({ diagnostic: { kind: "vision", cause: "unknown", errno: "PRIVATE",
    error_type: "PRIVATE_EXCEPTION", attempt: 99999, max_attempts: 3 } }).includes("PRIVATE"), false);
  api.setLanguage("en");
  assert.equal(api.logMessage(apiError), "Model API request failed: HTTP 503 (service unavailable); attempt 3/3; InternalServerError");
  assert.match(api.logMessage({ diagnostic: { kind: "api_stream", cause: "service_unavailable", http_status: 503 } }),
    /stream failed before delivery: HTTP 503/);
}

const sample = {
  ui: { local_host_id: "spark-west", features: { agents: true, logs: false, crons: false } },
  hosts: {
    "gpu-edge": { label: "Second node", role: "compute", system: { cpu: { usage_percent: 12 } } },
    "spark-west": { label: "First node", role: "local", system: { cpu: { usage_percent: 7 } } },
  },
};
assert.deepEqual(hosts.hostEntries(sample).map(([id]) => id), ["spark-west", "gpu-edge"]);
assert.equal(hosts.hostFor(sample, "gpu-edge").label, "Second node");
assert.equal(hosts.hasModelTelemetry(hosts.hostFor(sample, "spark-west")), false);
assert.equal(hosts.featureEnabled(sample, "agents"), true);
assert.equal(hosts.featureEnabled(sample, "logs"), false);
assert.equal(hosts.featureEnabled(sample, "quotas"), false);
assert.deepEqual(hosts.hostEntries({ system: { cpu: {} }, ui: { local_host_id: "custom-local" } }).map(([id]) => id), ["custom-local"]);
const renderHostsSource = appSource.match(/function renderHosts\(d\)\s*\{[\s\S]*?\n  \}/)?.[0] || "";
assert.match(renderHostsSource, /hostEntries\(d\)/, "panels must follow configured host entries");

console.log(`frontend i18n + host contract: PASS (${enKeys.length} catalog keys, ${staticKeys.length} HTML references, ${literalKeys.length} app translations)`);
