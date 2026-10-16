# Changelog

## 0.5.6

Includes the previously unpublished 0.5.2–0.5.5 development changes.

- Compact the work register to two content lines per desktop card; keep task and origin on separate lines on mobile.
- Keep agent, runtime, live state, elapsed time, task and origin visible. Model, reasoning, Fast mode and exact tool details are available in the card's dialog; fix missing reasoning/Fast fields found during release QA.
- Retain the activity glyph, tooltip captions and reduced-motion behavior; show elapsed time instead of clock start time.
- Compact the capacity strip without shrinking quota values. Keep Claude tracks side by side, preserve complete reset dates, and let failure details expand instead of clipping.
- Add independent, opt-in Claude subscription usage using an existing local Claude Code login, without refreshing or modifying credentials.
- Show provider-correct English/Finnish details, five-hour/weekly windows and reset times. Preserve last-confirmed data visibly as stale during failures.
- Keep provider polling independent of agent/cron refresh; bound timeouts, response sizes and cooldowns, reject redirects, and keep credentials out of browser payloads.
- Distinguish usage-query throttling from subscription-window exhaustion.
- Add synthetic collector, state, localization, responsive density/collision, dialog and frontend CI regressions.

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
