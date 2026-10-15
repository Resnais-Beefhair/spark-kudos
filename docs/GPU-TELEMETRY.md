# Shared GPU telemetry

KUDOS intentionally does not query NVML or run `nvidia-smi`. On a Spark, GPU telemetry
may already have a thermal/health owner. A second competing poller is a poor default.
The dashboard accepts an existing shared JSON state file, configured by
`gpu_state_path` for the local host and `gpu_state_path` in the remote-host section.
It does not install or start that producer.

## Producer contract

Publish a JSON object atomically (write a temporary file then rename) readable by the
unprivileged dashboard user. Maximum accepted size: 128 KiB locally. Required fields:

```json
{
  "boot_id": "the-current-linux-boot-id",
  "updated_at": "2026-01-01T12:00:00+00:00",
  "health": "ok",
  "gpu_util_percent": 0,
  "gpu_c": 42.0,
  "power_watts": 12.0,
  "power_limit_watts": 140.0,
  "gpu_processes": []
}
```

This is a schema illustration, **not a current measurement**. Read the real boot ID
from `/proc/sys/kernel/random/boot_id` and write a timezone-aware timestamp. The
sample must belong to the current boot and be no more than 60 seconds old (up to
five seconds of clock skew is allowed). Fields are finite nonnegative numbers;
utilization is at most 100%, temperature at most 150 °C and device power at most
2000 W. A missing measurement is not a measured zero.

The producer may report `unsafe`, `reset_required` or `stale`. Unhealthy, incomplete,
wrong-boot or stale state must not display current GPU measurements. Do not put raw
exception prose, prompts, command lines or private paths in shared telemetry.

Optional `gpu_processes` rows contain only `pid`, executable `name`, `memory_mb` and
optional `gpu_util`. Unknown process utilization stays unknown. An empty list means
a measured empty inventory; omitting the field means unavailable inventory.

## Unified memory

Spark CPU and GPU share UMA. The dashboard's shared-memory gauge is system memory
pressure, not a measured private GPU allocation. Process RSS and GPU allocations
have different accounting scopes and may overlap. Never add the two. Power is
GPU/device telemetry, not wall-plug or complete-system power.

## Missing source

CPU, RAM, network, storage and configured services still work without GPU telemetry.
The GPU instruments show unavailable, not zero. Configure an existing compatible
producer to enable them; do not grant root access or install a competing GPU poller
merely to fill a dashboard card.
