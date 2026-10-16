# Claude subscription usage

Claude usage is optional and independent of the legacy ChatGPT/agent adapter. The
base installation never reads Claude credentials or queries Anthropic.

## Enable

Use an **existing local Claude Code subscription login** for the same operating
system user as KUDOS. KUDOS does not log in, refresh tokens, change subscriptions or
make model/inference requests. An API billing key is not a subscription usage source.

```toml
[integrations]
claude_quota = true

# Optional: only when Claude Code stores its existing credentials elsewhere.
# [paths]
# claude_credentials = "~/.claude/.credentials.json"
```

Validate the configuration and restart only KUDOS. Protect the dashboard as described
in [deployment](DEPLOYMENT.md): usage telemetry is operational data and KUDOS has no
built-in login. Never paste credentials into TOML, an issue, a chat or a screenshot.

## What is displayed

- The five-hour subscription window and aggregate seven-day window when the provider
  reports them. Optional model-specific weekly windows appear in details.
- Remaining percentage is explicitly labeled; used percentage and provider-supplied
  reset times appear in the details. Zero remaining is real zero, not missing data.
- Reset timestamps are timezone-aware and formatted by the browser. No reset time or
  quota is guessed when the provider omits it.
- The plan label comes from a bounded mapping of Claude Code subscription metadata.
  No account email, organization ID, credential path or token enters a snapshot.

Claude extra-usage billing, dollar spend and API credits are not estimated from these
percentages. The ChatGPT and Claude windows describe different subscriptions; their
percentages must not be added together.

## Read-only collection and failure behavior

KUDOS reads the local Claude Code OAuth credential file only after explicit opt-in
and sends the access token only to the fixed Anthropic OAuth usage endpoint. It does
not write the file, follow redirects or send requests to a configurable URL.

Polling normally runs once every two minutes, with bounded timeouts/response sizes
and rate-limit cooldown. A failed refresh retains last-good values **as stale**, with
the original collection time. Values older than five minutes are stale even if a
cached collector call succeeds. Unavailable credentials or a provider failure never
become a fictional 0%/100% window. A refreshed existing login is reread on later polls.

The OAuth usage endpoint is a provider-specific integration rather than a guaranteed
public API contract. It may become unavailable or change; KUDOS reports that state
without exposing provider error bodies or modifying authentication. If a login has
expired, renew it through your normal local Claude Code workflow, outside KUDOS.
