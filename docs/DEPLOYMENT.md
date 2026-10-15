# Deployment

Run KUDOS as an unprivileged Linux user. The safe default is loopback only.
A second Spark needs an existing verified SSH route and Python 3 with `psutil`;
KUDOS never provisions host keys, installs remote software or changes authentication.

## Persistent user service

Install a verified source checkout as a user tool:

```bash
uv tool install /path/to/this/checkout
mkdir -p ~/.config/sisu-kudos ~/.config/systemd/user
cp examples/single-spark.toml ~/.config/sisu-kudos/config.toml
cp deploy/sisu-kudos.service ~/.config/systemd/user/sisu-kudos.service
~/.local/bin/sisu-kudos --check-config
systemctl --user daemon-reload
systemctl --user enable --now sisu-kudos.service
```

The template expects the tool at `~/.local/bin/sisu-kudos`. Adjust `ExecStart` if
your installation path differs. Check access to explicitly configured sources under
the service sandbox. Do not relax unrelated permissions or run as root to make an
optional source work. `systemctl --user status sisu-kudos.service` and
`curl --fail http://127.0.0.1:8787/api/health` verify the running instance.
This example requires a user systemd session; system boot persistence/user lingering
is an administrator decision, not something the dashboard changes.

## Browser access

For access from another machine, keep the listener on loopback and use an existing
SSH route (run this on your client):

```bash
ssh -N -L 8787:127.0.0.1:8787 your-dashboard-host
```

Then open <http://127.0.0.1:8787> on the client. Or use a separately configured,
authenticated HTTPS reverse proxy that supports Server-Sent Events. Disable proxy
response buffering for `/api/events`; keep all assets and API on the same origin.
No reverse proxy or authentication setup is silently created by KUDOS.

A bind to `0.0.0.0` is an explicit private-network exposure, not a secure sharing
feature. The API contains process names, resource use, configured labels and
filesystem details. Do not expose it publicly. [Security model](../SECURITY.md).

## Upgrade and rollback

Back up the current executable environment and private configuration first. Build
and test separately; static assets are served directly and must be swapped only at
the deployment boundary. Install the verified release, restart **only KUDOS**, and
check version, collectors, SSE, browser language and optional-source freshness.
Rollback means restoring the previous verified environment/assets and restarting
only that dashboard service. Never restart agent gateways as part of this procedure.

RAM-only histories restart after deployment. Existing browser language preference
and privacy-safe log acknowledgements are stored in that browser, not the server.
