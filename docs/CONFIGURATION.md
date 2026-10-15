# Configuration reference

Configuration is TOML, loaded and validated at startup. Restart **only KUDOS** to
apply a backend change. The browser language switch applies immediately.

## Selection and validation

Precedence: `--config PATH`, then `SISU_KUDOS_CONFIG`, then
`~/.config/sisu-kudos/config.toml`. A missing default file uses safe standalone
defaults. A missing explicitly selected file is an error. Other legacy environment
variables do not override TOML. The CLI does not print your configuration values.

```bash
sisu-kudos --config /path/to/your/config.toml --check-config
sisu-kudos --version
```

Paths expand `~`; relative local paths are relative to the config file, not the
working directory. Remote paths are absolute remote paths. Unknown sections/keys,
wrong types, duplicate IDs/names, unsupported language, invalid ports and shell
syntax in SSH command tokens fail validation rather than silently being ignored.
Keep actual config **outside** this checkout. No passwords/tokens are supported in
service/model URLs. Never commit operational settings.

## `[server]`

- `host`: `"127.0.0.1"` by default. An IP address or DNS host name. Changing to
  `"0.0.0.0"` exposes operational data on every IPv4 interface. There is no login.
- `port`: integer, 1–65535; default `8787`.

See [deployment/security](DEPLOYMENT.md) before changing the bind address.

## `[site]`

- `language`: `"en"` (default) or `"fi"`. A saved explicit browser preference wins.

## `[hosts.local]`

- `id`: stable identifier, default `"local"`. 1–32 letters/digits/underscores/hyphens,
  starting with a letter or digit. Keep it stable across label changes.
- `label`: visible name, default `"Spark 1"`, printable, at most 80 characters.
- `role`: visible role, default `"agents-services"`. Built-in roles
  `agents-services` and `model-server` have translated labels; custom text is preserved.

## `[hosts.remote]`

- `enabled`: boolean, default `false`. No SSH process starts when disabled.
- `target`: existing safe SSH destination (`host`, alias or `user@host`), required
  when enabled. Configure port, key and jump route in your existing SSH config.
- `expected_hostname`: required when enabled, must match `socket.gethostname()`
  reported by the actual remote machine. The alias alone is not identity evidence.
- `id`, `label`, `role`: defaults `"remote"`, `"Spark 2"`, `"model-server"`;
  remote and local IDs must differ.
- `python`: default `"python3"`; one executable name or absolute path, no arguments.
  The remote environment needs `psutil`. KUDOS does not install it.
- `gpu_state_path`: absolute shared-state path on the remote machine, default `""`
  (disabled). Uses the [GPU contract](GPU-TELEMETRY.md).
- `model_url`: default `""` (disabled). Optional vLLM-compatible base HTTP(S) URL,
  accessed from the **remote** machine, usually its loopback endpoint.

The existing route must work with batch authentication and strict known-host
checking. KUDOS neither accepts host keys nor creates credentials. It sends a fixed
bounded probe; local health survives remote failure. The remote disk panel currently
covers the remote root filesystem; configured local mount/service lists are local only.

## `[system]` (local host)

- `network_interfaces`: string array, default `[]` (physical-interface autodetection,
  with a non-loopback fallback). Explicit missing interfaces fail visibly; first
  and reset counter samples have unknown rates.
- `disk_mounts`: exact mounted paths, default `["/"]`. No implicit home/NAS discovery.
  Missing configured mounts remain unavailable, rather than falling through to
  their parent filesystem. File scans are restricted to verified live mounts.
- `gpu_state_path`: default `""` (disabled), path to an **existing** shared producer.
- `model_url`: default `""` (disabled), local vLLM-compatible base HTTP(S) URL.
  `/v1/models`, `/health` and `/metrics` are queried. Single-model metrics are
  supported; unknown or mismatched counters do not become estimated throughput.

## `[[services]]` (repeatable, local host)

No service is checked by default. Each row has:

- `name`: unique stable identifier, required.
- `label`: optional visible name, otherwise `name`.
- `kind`: `"systemd"` (default) or `"http"`.
- `unit`: required for systemd, e.g. `"model-server.service"`.
- `scope`: `"user"` (default) or `"system"`.
- `url`: required for HTTP. A bounded GET is performed, without credentials or
  redirects; response bodies are not displayed. A reachability check does not
  prove application correctness or ownership of an agent.

There are no arbitrary shell-command checks. See
[optional service examples](../examples/optional-services.toml).

## `[integrations]`

All flags default to `false` and must be actual TOML booleans, not strings:

- `agents`: opt into sessions through the optional trusted local Agenttimonitori adapter.
- `quotas`: opt into supported quota telemetry through the same optional adapter.
- `logs`: structured privacy-filtered Hermes/OpenClaw logs; arbitrary prose is not served.
- `crons`: read-only running-execution metadata; not a scheduler or job editor.
- `resources`: an explicitly configured local resource registry snapshot.

Disabled integrations are not polled and their controls are hidden. A configured
but unavailable source fails visibly and retains last-good data where available.
Agents/quotas currently use the optional legacy `agentmon` source contract: they are
**not** included as a generic all-framework collector. The base install needs none
of these integrations. Review adapter compatibility before enabling them.

## `[paths]` (optional integrations/compatibility)

- `agentmon_repo`: default empty, optional trusted checkout exposing `agentmon.service`.
  Required for `agents` or `quotas`; not installed or discovered automatically.
- `sisu_monitor_repo`: default empty. Explicit legacy hardware collector override
  exposing `sisu_monitor.collectors`. Base telemetry uses the built-ins instead.
- `hermes_log_dir`: default `~/.hermes/logs`, only read after relevant opt-in.
- `openclaw_home`: default `~/.openclaw`, only read after relevant opt-in.
- `resource_snapshot_path`: default empty, required source for enabled resources.

Legacy Python repository paths execute trusted local Python. They are not an
untrusted plugin sandbox. These compatibility adapters intentionally make no
promise of supporting arbitrary future upstream versions.

## `[[logs.hermes_profiles]]` (repeatable)

Optional explicit roster of `{label, path}` where `path` is a profile's **log
directory**. Labels must be unique. Without a roster, enabled logs/crons use a single
`default` profile at `paths.hermes_log_dir`; disabled integrations read none.

```toml
[integrations]
logs = true
crons = true

[[logs.hermes_profiles]]
label = "Assistant"
path = "~/.hermes/logs"

[[logs.hermes_profiles]]
label = "Worker"
path = "~/.hermes/profiles/worker/logs"
```

For stable browser log acknowledgement identities, provision the private local
HMAC key once using `python scripts/provision_log_identity.py`. Never copy that key
into the repository or a QA artifact. Missing/unsafe keys use an ephemeral key;
acknowledgements can then change across process restarts.

## `[intervals]`

Numeric seconds, each between 0.1 and 3600:

- `system_interval_s`: `2.0` (CPU, RAM, network; process sampling has a six-second floor).
- `agent_interval_s`: `15.0`.
- `cron_interval_s`: `5.0`.
- `slow_interval_s`: `12.0` (local disks and declared services).

GPU shared-state reads use a five-second cadence, remote/model probes ten seconds
and local identity thirty seconds. Histories are bounded to 450 samples, not a
persistent database. Lowering intervals is not a way to make a stalled source safe.
