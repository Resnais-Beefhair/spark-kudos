# Security policy

## Exposure model

KUDOS is a private operations dashboard with **no built-in authentication**. The
safe default listener is `127.0.0.1:8787`. A LAN bind exposes the browser-safe but
still operationally sensitive data to everyone who can reach that port. Do not
publish it on the internet or rely on obscurity, CORS or security headers as access
control. Put an authenticated TLS reverse proxy in front of it or use an SSH tunnel.

The process sees the local files and process metadata its OS identity can read.
Run unprivileged. Do not use sudo, broaden file permissions or place credentials in
configuration to make a source work. SSH uses existing verified host keys and
noninteractive authentication; KUDOS never creates keys or disables host checking.

## Data boundaries

The API uses explicit bounded projections, not raw agent/session/database objects.
Prompts, command lines, tool payloads, raw exception prose and tracebacks must never
be added to browser telemetry. A field being readable on disk does not make it safe
for a browser. Private paths/identifiers may still be visible in deliberately opened
filesystem drilldowns and operator-provided labels: treat the whole dashboard as
private, even when logs have been sanitized.

Only explicitly configured mounts may be scanned. Mount identity is revalidated;
scans are bounded and do not cross filesystems. Source configuration itself is never
served. Service checks are read-only systemd queries or bounded HTTP GETs to
operator-declared URLs, not user-supplied commands. Enabled legacy adapters execute
trusted local Python, so never configure an untrusted source checkout.

GPU telemetry has one existing owner. The dashboard reads shared JSON and does not
add competing driver queries, clock changes, power control or thermal management.

## Reporting a problem

Do not post production logs, keys, prompts, host addresses or security-sensitive
reproductions in a public issue. Once hosted on GitHub, use the repository's private
vulnerability reporting channel **if enabled**, or contact its listed maintainer
privately. Until a private reporting route is configured, do not assume one exists.
Security fixes should include a redacted regression fixture.

## Supported versions

The latest 0.5.x release is the current beta series. Older versions and private
pre-0.5 builds are not supported public releases. There is no response-time SLA;
use this dashboard only within the documented private-access boundary.
