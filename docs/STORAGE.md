# Volume-space details

Click a local-volume row in either configured host panel to see what occupies it.
The summary retains capacity, free space and the mountpoint. The list below it
shows up to 40 **direct children of that volume**, largest first:

- A directory includes the allocated space of its descendants.
- A file shows its allocated blocks, not its apparent length. Sparse files can
  therefore occupy much less than their logical size.
- Directory and file types are explicit; directory names also end in `/`.
- Non-printable filename bytes are escaped. Very long names have a short hash
  suffix so truncation does not turn distinct files into identical labels.

This is a one-level summary, not a flattened list of every descendant. It avoids
listing a directory and all its contents as separate, double-counted consumers.
GNU `du` deduplicates hard links. Filesystem metadata, reserved blocks, unreadable
paths and excluded mounts can make these totals differ from the capacity summary.

## Work and freshness limits

Sizes are measured on demand, outside the telemetry collectors and snapshot lock.
A local scan is limited to 60 seconds; a remote scan to 20 seconds. Concurrent
requests for the same host, mount and
device share one owned scan; only one size-summary traversal runs at a time. Leaving a
popup does not start another traversal or leave an unmanaged process behind.

Complete and non-empty partial results are cached for up to ten minutes. This
avoids continuously repeating a large traversal when permissions remain unchanged.
The measurement time and cache/partial state are visible. Live metric
updates do not trigger repeated scans; an open popup refreshes at cache expiry.
Closing and reopening it rechecks the mounted volume before cached data is used.

A timed-out or unreadable tree is **partial**, not a claim to have found the
largest objects on the entire volume. No results from an incomplete scan do not
mean an empty disk. Missing tools, a changed volume or a failed SSH connection
produce an unavailable state with a retry action, not invented zero sizes.

## Boundaries

- Requires GNU `du` from coreutils on the host being inspected.
- Only current, configured, non-network mounts from the dashboard snapshot are
  accepted. The live mount and source device are validated before scanning and
  when using cached data; a changed volume invalidates the result.
- Other mounts, including nested bind mounts, and symbolic links are excluded.
- A configured remote host uses its existing read-only SSH route, with hostname,
  source/mount checks, bounded command duration and subprocess cleanup. Nothing
  is installed or left running remotely.
- Only display names, kinds, sizes and measurement metadata are returned. File
  contents, command lines, environment variables and raw command output are not
  returned. Normal OS read permissions apply; no elevated access is requested.

The endpoint is `GET /api/disks/usage?host=<configured-id>&mount=<snapshot-mount>`.
It is not an arbitrary-path or arbitrary-host browser. A query still reveals
names and sizes within an allowed volume to anyone who can access this dashboard;
follow the [access-control guidance](../SECURITY.md).

The older `/api/disks/largest-files` endpoint remains for compatibility. The host
volume popup no longer substitutes whole-machine process I/O for volume usage.
