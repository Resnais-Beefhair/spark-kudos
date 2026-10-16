# Changelog

## 0.5.5 (unreleased)

- Compact the work register: two lines per card instead of four, so several simultaneous jobs take about half the vertical space.
- Keep agent, runtime, live state, elapsed time, task and origin on the card; the model, reasoning level, speed tier and exact tool name move to the card's detail view, and the activity glyph keeps its meaning as a tooltip.
- Show elapsed time instead of the clock start time, and let cards share the row width so task titles stay readable.

## 0.5.4 (unreleased)

- Compact the capacity strip: put quota headings and values on one desktop baseline, allocate more width to Claude, and tighten spacing without shrinking quota numbers.
- Keep both Claude tracks side by side, preserve complete reset dates, and remove the oversized status badge; allow failure details to expand rather than clip.
- Keep tablet cards in a two-by-two grid and add browser density, track-alignment and click-target regressions.

## 0.5.3 (unreleased)

- Clarify that a usage-endpoint request-rate limit is not exhaustion of the subscription window.

## 0.5.2 (unreleased)

- Add an independent, opt-in Claude subscription usage collector using the existing local Claude Code login; never refresh or modify credentials.
- Place compact ChatGPT and Claude capacity cards next to GPU power, with provider-correct English/Finnish details, five-hour/weekly windows and reset times.
- Preserve last-known quota data visibly as stale during auth/network/rate-limit failures; keep provider refresh independent of agent and cron refreshes.
- Bound polling, response size and timeouts; reject redirected requests and never send credentials to the browser.
- Add synthetic collector, state, localization and responsive browser regressions.

## 0.5.1

- Localize missing ChatGPT quota status in both the summary and inspector (#1).
- Render missing-disk text in the English/Finnish catalog, not CSS; handle empty-to-populated transitions (#2).
- Keep successful cron health messages empty and language-neutral; retain sanitized source errors (#3).
- Add CSP frame-ancestors, base-uri and form-action restrictions without weakening existing headers (#4).
- Match event filtering to the agent-only event allowlist; cron runs retain their separate ledger (#5).
- Isolate process-rate history to each collector lifetime, remove a duplicate import, and validate remote Linux paths with POSIX semantics (#5).
- Add pre-fix reproductions, synthetic API regressions and real browser checks under the application CSP.

## 0.5.0

First public release series. Distribution is through GitHub source and release packages;
there is no PyPI publication.

- Standalone local collectors and bounded scheduler remove mandatory private repositories.
- Validated TOML settings for one or two hosts, labels, volumes, network interfaces,
  service checks, existing shared GPU state and opt-in integrations.
- English-default interface with complete Finnish selection and per-browser persistence.
- Configurable host panels and disabled-by-default agent/log/cron/quota/resource views.
- Loopback listener by default; no automatic SSH probing or private source discovery.
- English documentation, example configurations, contribution/security policies,
  packaging, CI configuration, privacy checks and isolated verification.
- Fresh publication tree intentionally excludes prior private history, production
  captures and site configuration. Technical package/CLI remain `sisu-kudos`.
