# Architecture

```text
psutil + configured shared GPU JSON + configured service checks
                           |
optional SSH probe --------+-- bounded scheduler -- LiveState -- FastAPI -- SSE
optional read-only adapters|                         |          /          |
                           +-------------------------+  on-demand logs    DOM/SVG
                                                     |                   en / fi
                                              bounded memory history
```

## Standalone first

`builtin.py` supplies local Linux hardware collectors without importing another
repository. `scheduler.py` isolates blocking work in bounded daemon workers. A
failed collector retains last-good data, exposes freshness, and cannot repeatedly
spawn timed-out duplicates. One process-table pass supplies CPU, RSS and I/O leaders.

`config.py` validates operator-owned TOML before startup. Unknown keys and invalid
values fail clearly. Paths, service checks and remote settings are trusted local
configuration, not browser-controlled parameters. Browser metadata contains only
language, host labels and feature availability, never the configuration itself.

`remote.py` sends a fixed Python probe over an existing authenticated SSH route.
Configuration is encoded as data, never interpolated into a shell command. Host
identity and each source's freshness remain separate from local health. The remote
probe does not install an agent, change keys or write remote state.

## Optional integrations

The default installation has no agent/session/log/cron/quota/resource-registry
collectors. Enabled adapters read explicitly configured sources. The compatibility
adapter may load an operator-selected local telemetry repository; it is not needed
for base telemetry and is not a general plugin execution boundary.

`adapters.py` projects typed upstream values into browser-safe allowlists. Logs are
parsed into fixed summaries, not forwarded as prose. `logs.py`, `log_events.py`,
`crons.py`, `openclaw_sessions.py` and `resources.py` keep payloads bounded and avoid
platform recovery APIs that could write. Prompts, tool inputs/outputs, command lines,
raw tracebacks and routing/session keys are not part of the browser contract.

## Transport and interface

`app.py` serves a single-origin static UI, JSON snapshots and a bounded-lifetime
Server-Sent Events stream. A compact stream does not replay full histories or logs.
On-demand file scans validate the configured live mount and source identity before
starting a bounded, killable scan; no arbitrary path scan endpoint exists.

The interface renders with semantic DOM and bounded SVG. Text is assigned as text,
not HTML. Translation is explicit through a bilingual catalog, not a global DOM
rewriter. User labels and task content are not machine-translated. Reduced motion,
keyboard interaction and dialog focus restoration are maintained.

## Telemetry truthfulness

- GPU data comes only from the configured shared state. Missing or stale is not zero.
- GPU/UMA usage and process RSS have different scopes; do not add or equate them.
- Model throughput is derived only from matching model/server-identity counters.
- Processes are identified by PID and start time. A reused PID is not the old process.
- Remote failure cannot erase healthy local data.
- Device watts are not total-system or wall-plug watts.
- Browser log acknowledgements are local UI state, not incident recovery evidence.
