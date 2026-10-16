const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const web = path.join(root, "src/sisu_kudos/web");
const app = fs.readFileSync(path.join(web, "app.js"), "utf8");
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");
const i18n = fs.readFileSync(path.join(web, "i18n.js"), "utf8");

const helperStart = app.indexOf("  function quotaWindow(");
const helperEnd = app.indexOf("  function renderCapacity()", helperStart);
assert.ok(helperStart >= 0 && helperEnd > helperStart, "quota helpers must remain independently testable");
const helperSource = app.slice(helperStart, helperEnd);
const messages = {
  en: {
    "quota.status.available": "Available",
    "quota.status.loginRequired": "Sign-in required",
    "quota.status.rateLimited": "Rate limited",
    "quota.status.unavailable": "Unavailable",
    "quota.status.invalidResponse": "Invalid provider response",
    "quota.status.stale": "Stale · last confirmed data",
    "quota.noData": "NO DATA",
  },
  fi: {
    "quota.status.available": "Saatavilla",
    "quota.status.loginRequired": "Kirjautuminen vaaditaan",
    "quota.status.rateLimited": "Käyttöraja saavutettu",
    "quota.status.unavailable": "Ei saatavilla",
    "quota.status.invalidResponse": "Virheellinen palveluntarjoajan vastaus",
    "quota.status.stale": "Vanhentunut · viimeisin vahvistettu tieto",
    "quota.noData": "EI MITTARIA",
  },
};
let language = "en";
const sandbox = {
  num: (value) => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value),
  t: (key) => messages[language][key] || key,
};
vm.runInNewContext(`${helperSource}\nthis.quotaWindow = quotaWindow; this.quotaStatusText = quotaStatusText;`, sandbox);

const fiveHour = { id: "five_hour", duration_minutes: 300, remaining_percent: 96 };
const sevenDay = { id: "seven_day", duration_minutes: 10080, remaining_percent: 88 };
assert.equal(sandbox.quotaWindow({ windows: [sevenDay, fiveHour] }, "five_hour", 300), fiveHour, "selects the five-hour window by ID, not array order");
assert.equal(sandbox.quotaWindow({ windows: [sevenDay] }, "seven_day", 10080), sevenDay);
assert.equal(sandbox.quotaWindow({ windows: [{ id: "other", duration_minutes: 10080 }] }, "seven_day", 10080).duration_minutes, 10080, "duration is a fallback when provider IDs vary");
assert.equal(sandbox.quotaWindow({}, "five_hour", 300), null, "missing telemetry remains unknown");

for (const [state, expected] of [
  ["login_required", "Sign-in required"],
  ["auth", "Sign-in required"],
  ["rate_limited", "Rate limited"],
  ["rate_limit", "Rate limited"],
  ["unavailable", "Unavailable"],
  ["invalid_response", "Invalid provider response"],
]) assert.equal(sandbox.quotaStatusText({ state }, false), expected, `${state} is localized as a known state`);
assert.equal(sandbox.quotaStatusText({ state: "available", status: "Max 5×" }, true), "Max 5×", "safe provider plan label is preserved");
assert.equal(sandbox.quotaStatusText({ state: "available" }, false), "NO DATA", "availability without windows is not presented as usage");
assert.equal(sandbox.quotaStatusText({}, false), "Unavailable", "configured-but-missing snapshot is unavailable");
assert.equal(sandbox.quotaStatusText({ state: "available", stale: true }, true), "Available · Stale · last confirmed data");
language = "fi";
assert.equal(sandbox.quotaStatusText({ state: "auth", stale: true }, true), "Kirjautuminen vaaditaan · Vanhentunut · viimeisin vahvistettu tieto");
assert.equal(sandbox.quotaStatusText({ state: "rate_limit" }, false), "Käyttöraja saavutettu");

assert.match(html, /id="quota-control"[^>]*data-inspect="quota:chatgpt"/);
assert.match(html, /id="claude-quota-control"[^>]*data-inspect="quota:claude"/);
for (const id of ["claude-value", "claude-note", "claude-life", "claude-week-life"])
  assert.match(html, new RegExp(`id="${id}"`), `${id} must exist for the compact two-window card`);
assert.match(app, /featureEnabled\(ui\.data, "quotas"\)/, "ChatGPT keeps its existing independent feature gate");
assert.match(app, /featureEnabled\(ui\.data, "claude_quota"\)/, "Claude has an independent feature gate");
assert.match(app, /quotaWindow\(q, "five_hour", 300\)/);
assert.match(app, /quotaWindow\(q, "seven_day", 10080\)/);
assert.match(app, /title = q\.provider \|\| id/, "quota inspector title follows the selected provider");
assert.match(app, /const q = d\.agents\?\.quotas\?\.\[id\]/, "inspector reads the routed provider quota");
assert.match(app, /quotaStatusText\(q, Boolean\(q\.windows\?\.length\)\)/, "inspector localizes status and stale state");
assert.match(app, /if \(ui\.data\) render\(ui\.data\)/, "language changes rerender live quota values");
for (const key of ["quota.claudeName", "quota.claudeAriaValues", "quota.claudeResetNotes", "quota.status.loginRequired", "quota.status.rateLimited", "quota.status.stale"])
  assert.ok(i18n.includes(`"${key}"`), `English and Finnish catalogs define ${key}`);

function extractFunction(name) {
  const start = app.indexOf(`function ${name}(`);
  const open = app.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < app.length; i += 1) {
    if (app[i] === "{") depth += 1;
    if (app[i] === "}" && --depth === 0) return app.slice(start, i + 1);
  }
  throw new Error(`Could not extract ${name}`);
}
const dynamicMessages = {
  en: {
    ...messages.en,
    "quota.fiveHourShort": "5h",
    "quota.sevenDayShort": "7d",
    "quota.claudeResetNotes": "5h resets {fiveHour} · 7d resets {sevenDay}",
    "quota.claudeAriaValues": "5-hour {fiveHour}% remaining; 7-day {sevenDay}% remaining. {status}",
    "quota.remainingTitle": "Percent remaining",
    "power.note": "GPU power · not wall power",
  },
  fi: {
    ...messages.fi,
    "quota.fiveHourShort": "5 h",
    "quota.sevenDayShort": "7 vrk",
    "quota.claudeResetNotes": "5 h nollaus {fiveHour} · 7 vrk nollaus {sevenDay}",
    "quota.claudeAriaValues": "5 tunnin jakso, {fiveHour} % jäljellä; 7 päivän jakso, {sevenDay} % jäljellä. {status}",
    "quota.remainingTitle": "Jäljellä oleva prosenttiosuus",
    "power.note": "GPU-teho · ei seinätehoa",
  },
};
function makeNode() {
  return {
    hidden: false,
    title: "",
    textContent: "",
    style: {},
    attrs: {},
    parentElement: { classList: { toggle(name, enabled) { this[name] = enabled; } } },
    setAttribute(name, value) { this.attrs[name] = value; },
    replaceChildren() {},
  };
}
const selectors = ["#quota-control", "#claude-quota-control", "#chatgpt-value", "#chatgpt-note", "#chatgpt-life", "#claude-value", "#claude-note", "#claude-life", "#claude-week-life", "#power-readings", "#power-note"];
const nodes = new Map(selectors.map((selector) => [selector, makeNode()]));
const renderedUi = {
  data: {
    ui: { features: { quotas: false, claude_quota: true } },
    agents: { quotas: { claude: {
      provider: "Claude", available: true, status: "Max 5×", state: "available", stale: true,
      windows: [
        { id: "seven_day", remaining_percent: 100, resets_at: "2026-10-16T20:00:00Z" },
        { id: "five_hour", remaining_percent: 96, resets_at: "2026-10-16T18:39:59Z" },
      ],
    } } },
  },
};
let renderLanguage = "en";
const renderContext = {
  ui: renderedUi,
  $: (selector) => nodes.get(selector),
  num: sandbox.num,
  fmt: (value) => String(value),
  clamp: (value) => Math.max(0, Math.min(100, Number(value) || 0)),
  t: (key, values = {}) => Object.entries(values).reduce((text, [name, value]) => text.replaceAll(`{${name}}`, String(value)), dynamicMessages[renderLanguage][key] || key),
  featureEnabled: (data, key) => data?.ui?.features?.[key] === true,
  hostEntries: () => [],
  quota: (key) => renderedUi.data?.agents?.quotas?.[key] || {},
  setText: (selector, value) => { nodes.get(selector).textContent = String(value); },
  clock: (value) => value || "–",
};
vm.runInNewContext(`${extractFunction("quotaWindow")}\n${extractFunction("quotaStatusText")}\n${extractFunction("renderCapacity")}\nthis.renderCapacity = renderCapacity;`, renderContext);
renderContext.renderCapacity();
assert.equal(nodes.get("#quota-control").hidden, true, "Claude opt-in does not enable ChatGPT quota");
assert.equal(nodes.get("#claude-quota-control").hidden, false);
assert.match(nodes.get("#claude-value").textContent, /5h 96% · 7d 100%/, "card summarizes both rolling windows");
assert.match(nodes.get("#claude-note").textContent, /5h resets .* · 7d resets .*Stale/, "card shows both reset hints and translated stale state");
assert.equal(nodes.get("#claude-life").style.width, "96%");
assert.equal(nodes.get("#claude-week-life").style.width, "100%");
assert.match(nodes.get("#claude-quota-control").attrs["aria-label"], /5-hour 96% remaining; 7-day 100% remaining/);
renderLanguage = "fi";
renderContext.renderCapacity();
assert.match(nodes.get("#claude-value").textContent, /5 h 96% · 7 vrk 100%/);
assert.match(nodes.get("#claude-note").textContent, /Vanhentunut/);
renderedUi.data.agents.quotas = {};
renderContext.renderCapacity();
assert.equal(nodes.get("#claude-value").textContent, "–", "missing configured telemetry remains unknown");
assert.equal(nodes.get("#claude-note").textContent, "Ei saatavilla");

console.log("Claude + ChatGPT quota contract: PASS (window selection, state/stale mapping, provider routing, bilingual rendering)");
