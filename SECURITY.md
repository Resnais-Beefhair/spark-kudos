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

The default API uses explicit bounded projections, not raw agent/session/database
objects. It does not expose arbitrary log prose, prompts or tracebacks. An owner
may explicitly enable `[logs] show_details = true` on a trusted private dashboard
to see original log messages and bounded tracebacks. This mode preserves paths,
identities, prompt/tool text and provider content while masking recognized
credentials. Filtering is best effort, not a guarantee that arbitrary logs are
shareable. It does not add authentication or change who can reach the listener.
API responses are marked `no-store`; source configuration is never served.
Private paths/identifiers may also be visible in deliberately opened filesystem
drilldowns and operator-provided labels. Treat the whole dashboard as private.

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
