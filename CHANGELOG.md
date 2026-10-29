# Changelog

## 0.5.20

Includes the previously unpublished local-development changes listed under 0.5.19 below. That version was not a separate public release.

- Add named machine-temperature instruments next to the masthead identity: highest available CPU/GPU reading and a real five-minute trend on a shared 0–100°C scale. Green/amber/red bands agree with the existing temperature thresholds.
- Keep cached, disconnected and missing readings visibly neutral; leave gaps and missing history empty instead of inventing samples. Temperature instruments open the existing per-host thermal details and retain their DOM nodes during live updates.
- Adapt the instrument strip below the identity at narrow widths; keep English/Finnish labels, keyboard access and collector behavior intact.

## 0.5.19 (unreleased)

- Restore OpenClaw active-work cards when current lifecycle/writer ownership replaces the legacy running status field. Require a live PID/start-identity lease and exclude lifecycle claims from before that owner started.
- Derive current tool and observed model/provider only from bounded structured trajectory metadata for the exact current session and run. Never read arguments, results or message bodies; completed, aborted and mismatched runs do not animate as active work.
- Retain legacy-state compatibility and visible source failures, and cover missing optional trajectory tables, terminal transitions and stale/reused owners.

## 0.5.18

Includes the previously unpublished local-development changes listed under 0.5.12–0.5.17 below. Those versions were not separate public releases.

- Omit missing task names from active-work cards instead of displaying unavailable-text filler. Give the observed origin the full content line and compact taskless mobile cards to two lines.
- Preserve real task names, historical-conversation context in details, live node identity, and restoration when a current task becomes available. Cover English/Finnish and desktop/mobile transitions.

## 0.5.17 (unreleased)

- In owner-detail mode, expose credential-filtered original text in the standard log `message` field as well as `original_message`, so older clients no longer show blanket redaction for unknown errors.
- Signal the presentation contract through matching REST/compact-stream log revisions so already-open clients fetch the corrected records without changing event identity, acknowledgement or internal correlation data.
- Keep the public/default mode unchanged and compact streams free of log bodies. Cover REST/snapshot consistency, source-state immutability and previous-renderer compatibility.

## 0.5.16 (unreleased)

- Add explicit owner-only `logs.show_details` for original Hermes/OpenClaw messages and bounded tracebacks, including unknown error formats. Mask recognized credentials without removing arbitrary error prose, paths or identities; keep public/default summaries unchanged. Render details as plain text and disable API caching in this mode.
- Recognize supported agent API/retry/stream errors behind Hermes subagent and batch-worker log prefixes. Normalize only known bounded execution labels in the diagnostic view; preserve original event identity, severity, status and acknowledgement.
- Reject arbitrary or nested labels and private-body keyword matches. Add prefix-family/privacy regressions and verify the exact reported source event against both installed runtimes and the deployed browser.

## 0.5.15 (unreleased)

- Identify image-analysis cancellations, timeouts, connection failures, outer HTTP failures and common errno causes instead of only recognizing EPIPE. Unsupported exception details retain the safely identified image operation and an explicit unknown cause, never arbitrary prose.
- Preserve private traceback/provider/tool content, historical event identity, severity, status and acknowledgement. Add cancellation-chain, malformed-format, privacy and bilingual browser regressions.

## 0.5.14 (unreleased)

- Preserve acknowledged same-timestamp peers when a bounded log history rotates them out and back in. Never move the acknowledgement cursor backwards when newer rows disappear. Continue alerting on genuinely new events; retain legacy cursors and bound stored peer metadata.
- Explain asyncio's unclosed HTTP client session and connector errors with bilingual, typed resource-lifecycle diagnostics instead of a blanket redacted message.
- Keep private connection/object details excluded. Preserve event identity, severity, status and acknowledgement behavior; this does not claim the upstream resource leak is repaired.
- Add exact-format, malformed-input, private-continuation, deduplication and EN/FI desktop/mobile log regressions.
- The staged 0.5.13 build was never deployed; the acknowledgement defect was reproduced and fixed before activation.

## 0.5.12 (unreleased)

- Cap active-work cards at 600 px on desktop instead of stretching a single card across the register; retain mobile width, compact heights and live details.
- Replace whole-machine process I/O in volume popups with mount-scoped directory/file allocation sizes, largest first. Directory totals include descendants; display only direct children to avoid overlapping totals.
- Run bounded on-demand GNU du scans outside telemetry collectors, with single-flight requests, serialized traversal, source-device validation and explicit partial/unavailable states. Keep local and configured SSH-host scans read-only.
- Cache non-empty results for ten minutes, including permission-limited results, so a large tree is not traversed every minute. Refresh an open view at expiry, not on every live snapshot. Keep late responses out of another volume's popup.
- Add filesystem, sparse-file, nested-mount, cache/cancellation, API, SSH and responsive EN/FI browser regressions. Document names/sizes disclosure and scan limits.
- Escape non-UTF-8/control filename bytes, bound labels by browser UTF-16 units, and distinguish truncated names with stable suffixes. The unactivated 0.5.10 candidate was rejected during review; it was never deployed or published.
- Allow a bounded 60-second local traversal after installed validation showed that the earlier 20-second budget omitted the largest directories. Remote scans remain capped at 20 seconds. Label partial counts as measured items; preserve literal SSH-probe marker names.

## 0.5.9

Includes the previously unpublished 0.5.7–0.5.8 development changes listed below.

- Replace blanket Hermes error summaries with structured, bilingual diagnostics for model API/stream failures and image-analysis errors: HTTP status, bounded retry/attempt counts, allowlisted exception types and OS errno.
- Keep arbitrary provider bodies, prompts, tool content, credentials, URLs and routing IDs out of browser payloads; only trusted logger prefixes and typed diagnostic fields cross the boundary.
- Preserve historical event identities, severities, status and acknowledgements; this improves error visibility, not the underlying model or tool failures.
- Add privacy, malformed-input, localization and responsive log-view regressions.

## 0.5.8 (unreleased)

- Treat Hermes turn retries as serialized attempts, not nested active counters. A successful end closes previous failed starts; late tool completions cannot resurrect completed work.
- Preserve gateway/process-identity checks, concurrent sessions and compression-child activity. No agent profiles or gateways are modified.
- Separate persistent Hermes conversation titles from current task labels; show historical titles explicitly as context in details.
- Add lifecycle, projection, localization and browser regressions.

## 0.5.7 (unreleased)

- Show the observed model name between work status and elapsed time, without adding a card row. Keep full names in a tooltip and details; omit absent and script-only models.
- Double the activity glyph to 56 × 40 px while retaining 60 px desktop cards and 78 px mobile cards.
- Cover long/missing/literal model names, live updates, stable glyph identity, motion controls, reduced motion and responsive collisions in browser QA.

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
