"""Fault-isolated live state collection and bounded history."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime
from typing import Any

from .adapters import (
    load_agent_service,
    load_sisu_collectors,
    opaque_id,
    safe_agent_snapshot,
    safe_gpu_snapshot,
)
from .config import Settings
from .crons import CronCollector, compose_crons, is_cron_session
from .diagnostics import (
    ProcessViewCollector,
    scan_largest_files_process,
    validate_mount_identity,
)
from .logs import collect_all_logs
from .origins import collect_with_origins
from .remote import RemoteCollector

log = logging.getLogger(__name__)


class LiveState:
    """Compose cached collector values into the browser contract.

    Blocking source collectors run only through the package's bounded daemon
    scheduler. Optional private integrations are imported only when enabled.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.collectors: Any = None
        self.agent_service: Any = None
        self._base_agents: dict[str, Any] = {
            "sessions": [],
            "events": [],
            "health": [],
            "quotas": {},
        }
        self.scheduler: Any = None
        self._host_histories: dict[str, deque[dict[str, Any]]] = {
            settings.local_host_id: deque(maxlen=450)
        }
        self._host_updated: dict[str, float | None] = {settings.local_host_id: None}
        self._host_generated_at: dict[str, str] = {settings.local_host_id: ""}
        if settings.remote_enabled:
            self._host_histories[settings.remote_host_id] = deque(maxlen=450)
            self._host_updated[settings.remote_host_id] = None
            self._host_generated_at[settings.remote_host_id] = ""
        self._remote_collector_name = f"remote:{settings.remote_host_id}"
        self._service_order: tuple[str, ...] = tuple(item.name for item in settings.services)
        self.data: dict[str, Any] = {
            "generated_at": "",
            "system": {},
            "agents": {"sessions": [], "events": [], "health": [], "quotas": {}},
            "logs": {"records": [], "counts": {"warning": 0, "error": 0}, "sources": []},
            "history": [],
            "collector_health": {},
            "ui": self._ui_metadata(),
        }
        self._history: deque[dict[str, Any]] = deque(maxlen=450)
        self._history_at = 0.0
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._file_scan_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._file_scan_locks: dict[str, asyncio.Lock] = {}
        self._file_scan_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.revision = 0

    async def start(self) -> None:
        from . import scheduler as scheduler_module

        CollectorSpec = scheduler_module.CollectorSpec
        CollectorScheduler = scheduler_module.CollectorScheduler
        if self.settings.sisu_monitor_repo is not None:
            # Kept solely for explicitly configured private legacy deployments.
            self.collectors = load_sisu_collectors(self.settings.sisu_monitor_repo)
        else:
            from .builtin import BuiltinCollectors

            self.collectors = BuiltinCollectors(self.settings)
        self._service_order = tuple(getattr(self.collectors, "SERVICE_ORDER", self._service_order))

        if self.settings.agents_enabled or self.settings.quotas_enabled:
            if self.settings.agentmon_repo is None:
                self.data["collector_health"]["agents"] = self._unavailable_health(
                    RuntimeError("agent source not configured")
                )
            else:
                try:
                    self.agent_service = load_agent_service(
                        self.settings.agentmon_repo, self.settings.openclaw_home
                    )
                except Exception as exc:
                    log.warning("optional agent source unavailable (%s)", type(exc).__name__)
                    self.data["collector_health"]["agents"] = self._unavailable_health(exc)

        prime = getattr(self.collectors, "prime_process_cpu", None)
        if prime is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(prime)

        specs = [
            CollectorSpec("cpu", self.collectors.collect_cpu, self.settings.system_interval_s, 4.0),
            CollectorSpec("ram", self.collectors.collect_ram, self.settings.system_interval_s, 4.0),
            # Read the configured shared GPU-state source; never start a poller.
            CollectorSpec("gpu", self.collectors.collect_gpu, 5.0, 5.0),
            CollectorSpec(
                "network", self.collectors.collect_network, self.settings.system_interval_s, 4.0
            ),
            CollectorSpec("system", self.collectors.collect_system, 30.0, 6.0),
            CollectorSpec(
                "disks", self.collectors.collect_disks, self.settings.slow_interval_s, 6.0
            ),
            CollectorSpec(
                "processes",
                ProcessViewCollector(),
                max(6.0, self.settings.system_interval_s * 3),
                7.0,
            ),
        ]
        if self.settings.model_url:
            collect_model = getattr(self.collectors, "collect_model", None)
            if collect_model is not None:
                specs.append(CollectorSpec("model", collect_model, 10.0, 5.0))
        if self.settings.remote_enabled:
            specs.append(
                CollectorSpec(
                    self._remote_collector_name, RemoteCollector(self.settings), 10.0, 8.0
                )
            )
            self._host_histories[self.settings.remote_host_id] = deque(maxlen=450)
            self._host_updated[self.settings.remote_host_id] = None
            self._host_generated_at[self.settings.remote_host_id] = ""
        if self.settings.logs_enabled:
            specs.append(
                CollectorSpec(
                    "logs",
                    lambda: collect_all_logs(dict(self.settings.hermes_profile_log_dirs)),
                    max(8.0, self.settings.system_interval_s * 4),
                    3.0,
                )
            )
        if self.settings.crons_enabled:
            specs.append(
                CollectorSpec(
                    "crons",
                    CronCollector(
                        tuple(
                            (name, path.parent)
                            for name, path in self.settings.hermes_profile_log_dirs
                        ),
                        self.settings.openclaw_home,
                    ),
                    self.settings.cron_interval_s,
                    4.0,
                )
            )
        if self.agent_service is not None:
            specs.append(
                CollectorSpec(
                    "agents",
                    lambda: collect_with_origins(self.agent_service),
                    self.settings.agent_interval_s,
                    25.0,
                )
            )
        for name in self._service_order:
            timeout = 10.0 if name == "buzz" else 6.0
            specs.append(
                CollectorSpec(
                    f"service:{name}",
                    lambda name=name: self.collectors.collect_service(name),
                    10.0,
                    timeout,
                )
            )

        self.scheduler = CollectorScheduler(specs, max_concurrency=6)
        core = ["cpu", "ram", "gpu", "network", "system", "disks", "processes"]
        core.extend(
            name for name in ("logs", "crons", "agents", "model") if name in self.scheduler.names()
        )
        await self.scheduler.wait_for(core, timeout=15.0)
        await self._publish(self.scheduler.pop_dirty() | set(core))
        self._tasks = [asyncio.create_task(self._scheduler_loop())]

    def _ui_metadata(self) -> dict[str, Any]:
        return {
            "language": self.settings.language,
            "local_host_id": self.settings.local_host_id,
            "features": {
                "agents": self.settings.agents_enabled,
                "logs": self.settings.logs_enabled,
                "crons": self.settings.crons_enabled,
                "resources": self.settings.resources_enabled,
                "quotas": self.settings.quotas_enabled,
            },
        }

    async def stop(self) -> None:
        if self.scheduler is not None:
            self.scheduler.close()
        for task in self._tasks:
            task.cancel()
        for task in self._file_scan_tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks, *self._file_scan_tasks.values(), return_exceptions=True)

    async def _scheduler_loop(self) -> None:
        while True:
            self.scheduler.schedule_due()
            dirty = self.scheduler.pop_dirty()
            if dirty:
                await self._publish(dirty)
            await asyncio.sleep(0.2)

    @staticmethod
    def _unavailable_health(exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "stale": True,
            "age_seconds": None,
            "error": type(exc).__name__,
            "duration_ms": 0,
            "failures": 1,
        }

    def _health(self, name: str) -> dict[str, Any]:
        if self.scheduler is None or name not in self.scheduler.names():
            return {
                "ok": False,
                "stale": True,
                "age_seconds": None,
                "error": "collector_unavailable",
                "duration_ms": None,
                "failures": 0,
            }
        state = self.scheduler.state(name)
        age = state.age_s()
        return {
            "ok": state.updated_at is not None and not state.error,
            "stale": state.updated_at is None or bool(state.error) or state.is_stale(),
            "age_seconds": None if age is None else round(age, 1),
            # Scheduler exceptions can contain private paths or command output.
            "error": "collection_failed" if state.error else "",
            "duration_ms": (
                None if state.last_duration_s is None else round(state.last_duration_s * 1000)
            ),
            "failures": state.failures,
        }

    def _retain_last_good_quotas(
        self, safe: dict[str, Any], previous_agents: dict[str, Any]
    ) -> None:
        previous_quotas = previous_agents.get("quotas", {})
        for key, quota in safe.get("quotas", {}).items():
            previous = previous_quotas.get(key, {})
            if not quota.get("available") and previous.get("available"):
                retained = dict(previous)
                retained["stale"] = True
                retained["status"] = f"{previous.get('status', key)} · last known data"
                safe["quotas"][key] = retained

    async def _publish(self, names: set[str]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        async with self._lock:
            current = dict(self.data.get("system", {}))
            scheduler_names = set(self.scheduler.names())
            for name in names & scheduler_names:
                if name in scheduler_names:
                    self.data["collector_health"][name] = self._health(name)

            cpu = self.scheduler.value("cpu")
            ram = self.scheduler.value("ram")
            gpu = self.scheduler.value("gpu")
            network = self.scheduler.value("network")
            identity = self.scheduler.value("system")
            disks = self.scheduler.value("disks")
            processes = self.scheduler.value("processes")
            logs = self.scheduler.value("logs") if "logs" in scheduler_names else None
            model = self.scheduler.value("model") if "model" in scheduler_names else None

            if cpu is not None:
                current["cpu"] = asdict(cpu)
            if ram is not None:
                current["ram"] = asdict(ram) | {
                    "used_percent": ram.used_percent,
                    "swap_percent": ram.swap_percent,
                }
            if gpu is not None:
                current["gpu"] = safe_gpu_snapshot(gpu, self._health("gpu"))
            if network is not None:
                current["network"] = asdict(network)
            if identity is not None:
                current["identity"] = asdict(identity)
            if disks is not None:
                current["disks"] = [asdict(row) for row in disks]
            if processes is not None:
                current["processes"] = processes
                current["process_count"] = processes["count"]
            if model is not None:
                current["model"] = model
            if logs is not None:
                self.data["logs"] = dict(logs)
                self.data["logs"]["revision"] = opaque_id(str(logs))

            service_values = []
            for service_name in self._service_order:
                collector_name = f"service:{service_name}"
                value = self.scheduler.value(collector_name)
                if value is not None:
                    service_values.append(
                        {
                            "name": value.name,
                            "status": value.status,
                            "memory_mb": value.memory_mb,
                            "activity": " ".join(str(value.activity or "").split())[:120],
                            "collector": self._health(collector_name),
                        }
                    )
            if service_values:
                current["services"] = service_values

            if "agents" in names and "agents" in self.scheduler.names():
                agent_snapshot = self.scheduler.value("agents")
                if agent_snapshot is not None:
                    safe = safe_agent_snapshot(
                        agent_snapshot,
                        sessions=[s for s in agent_snapshot.sessions if not is_cron_session(s)],
                    )
                    if not self.settings.agents_enabled:
                        safe["sessions"] = []
                        safe["events"] = []
                        safe["health"] = []
                    if self.settings.quotas_enabled:
                        self._retain_last_good_quotas(safe, self.data.get("agents", {}))
                    else:
                        safe["quotas"] = {}
                    self._base_agents = safe

            if names & {"agents", "crons"} and (
                "agents" in scheduler_names or "crons" in scheduler_names
            ):
                cron_health = self.data["collector_health"].get("crons", {})
                self.data["agents"] = compose_crons(
                    self._base_agents,
                    self.scheduler.value("agents") if "agents" in self.scheduler.names() else None,
                    self.scheduler.value("crons") if "crons" in self.scheduler.names() else None,
                    stale=not cron_health.get("ok") or cron_health.get("stale", True),
                )

            if names & {"cpu", "ram", "gpu", "network"} and (
                time.monotonic() - self._history_at >= self.settings.system_interval_s
            ):
                self._history_at = time.monotonic()
                rx = current.get("network", {}).get("rx_bytes_sec")
                tx = current.get("network", {}).get("tx_bytes_sec")
                point = {
                    "at": now,
                    "cpu": current.get("cpu", {}).get("usage_percent"),
                    "cpu_temp": current.get("cpu", {}).get("temperature"),
                    "gpu": current.get("gpu", {}).get("utilization_percent"),
                    "gpu_temp": current.get("gpu", {}).get("temperature"),
                    "gpu_power": current.get("gpu", {}).get("power_watts"),
                    "ram": current.get("ram", {}).get("used_percent"),
                    "network_rx": rx,
                    "network_tx": tx,
                    "network_total": (
                        (rx or 0) + (tx or 0) if rx is not None or tx is not None else None
                    ),
                }
                self._history.append(point)
                self.data["history"] = list(self._history)

            self.data["system"] = current
            if self.settings.remote_enabled and self._remote_collector_name in names:
                state = self.scheduler.state(self._remote_collector_name)
                value = self.scheduler.value(self._remote_collector_name)
                host_id = self.settings.remote_host_id
                if (
                    value is not None
                    and state.updated_at is not None
                    and state.updated_at != self._host_updated[host_id]
                ):
                    self._host_updated[host_id] = state.updated_at
                    self._host_generated_at[host_id] = (
                        datetime.fromtimestamp(state.updated_at)
                        .astimezone()
                        .isoformat(timespec="seconds")
                    )
                    self._host_histories[host_id].append(self._host_history_point(value, now))
            self.data["generated_at"] = now
            self.revision += 1

    @staticmethod
    def _host_history_point(value: dict[str, Any], at: str) -> dict[str, Any]:
        return {
            "at": at,
            "cpu": value.get("cpu", {}).get("usage_percent"),
            "cpu_temp": value.get("cpu", {}).get("temperature"),
            "gpu": value.get("gpu", {}).get("utilization_percent"),
            "gpu_temp": value.get("gpu", {}).get("temperature"),
            "gpu_power": value.get("gpu", {}).get("power_watts"),
            "ram": value.get("ram", {}).get("used_percent"),
            "network_rx": value.get("network", {}).get("rx_bytes_sec"),
            "network_tx": value.get("network", {}).get("tx_bytes_sec"),
        }

    def _hosts(self, compact: bool = False) -> dict[str, Any]:
        core_health = [self._health(n) for n in ("cpu", "ram", "network", "system")]
        # GPU failure is a subsystem fault, not a disconnected host.
        local_health = {
            "ok": all(h["ok"] for h in core_health),
            "stale": any(h["stale"] for h in core_health),
            "age_seconds": max((h["age_seconds"] or 0 for h in core_health), default=0)
            if any(h["age_seconds"] is not None for h in core_health)
            else None,
            "error": "" if all(h["ok"] for h in core_health) else "host_telemetry_partial",
        }
        local_id = self.settings.local_host_id
        hosts = {
            local_id: {
                "id": local_id,
                "label": self.settings.local_host_label,
                "role": self.settings.local_host_role,
                "system": self.data.get("system", {}),
                "health": local_health,
                "history": list(self._history)[-1:] if compact else list(self._history),
                "generated_at": self.data["generated_at"],
            }
        }
        if self.settings.remote_enabled and self.scheduler is not None:
            remote_id = self.settings.remote_host_id
            hosts[remote_id] = {
                "id": remote_id,
                "label": self.settings.remote_host_label,
                "role": self.settings.remote_host_role,
                "system": self.scheduler.value(self._remote_collector_name) or {},
                "health": self._health(self._remote_collector_name),
                "history": (
                    list(self._host_histories[remote_id])[-1:]
                    if compact
                    else list(self._host_histories[remote_id])
                ),
                "generated_at": self._host_generated_at[remote_id],
            }
        return hosts

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self.data.copy() | {
                "revision": self.revision,
                "hosts": self._hosts(),
                "ui": self._ui_metadata(),
            }

    async def stream_snapshot(self) -> dict[str, Any]:
        """Return a compact live update; the initial HTTP snapshot carries history and logs."""
        async with self._lock:
            payload = self.data.copy() | {"revision": self.revision}
            payload["hosts"] = self._hosts(compact=True)
            payload["ui"] = self._ui_metadata()
            logs = self.data.get("logs", {})
            payload["logs"] = {
                "revision": logs.get("revision", ""),
                "counts": logs.get("counts", {}),
                "sources": logs.get("sources", []),
            }
            payload["history"] = list(self._history)[-1:]
            return payload

    async def largest_files(self, mount: str) -> dict[str, Any]:
        """Scan an allowlisted mounted filesystem on demand and cache the result."""
        async with self._lock:
            disks = self.data.get("system", {}).get("disks", [])
            expected_sources = {row.get("mount"): row.get("source") for row in disks}
        expected_source = expected_sources.get(mount)
        if not expected_source:
            raise ValueError("unknown mount")
        root = validate_mount_identity(mount, expected_source)
        cached = self._file_scan_cache.get(mount)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1] | {"cached": True}

        lock = self._file_scan_locks.setdefault(mount, asyncio.Lock())
        async with lock:
            cached = self._file_scan_cache.get(mount)
            if cached and time.monotonic() - cached[0] < 600:
                return cached[1] | {"cached": True}
            task = self._file_scan_tasks.get(mount)
            if task is None or task.done():
                task = asyncio.create_task(
                    scan_largest_files_process(root, limit=12, max_seconds=10.0)
                )
                self._file_scan_tasks[mount] = task
                task.add_done_callback(lambda done, key=mount: self._finish_file_scan(key, done))
            # Shield the worker so a stalled remote filesystem cannot be relaunched
            # by every request after the HTTP timeout.
            result = await asyncio.wait_for(asyncio.shield(task), timeout=12.0)
            if result.get("complete") or result.get("scanned_files", 0) > 0:
                self._file_scan_cache[mount] = (time.monotonic(), result)
            else:
                raise RuntimeError("filesystem scan is unavailable in the service sandbox")
            return result | {"cached": False}

    def _finish_file_scan(self, mount: str, task: asyncio.Task[dict[str, Any]]) -> None:
        if self._file_scan_tasks.get(mount) is task:
            self._file_scan_tasks.pop(mount, None)
        if task.cancelled():
            return
        try:
            result = task.result()
        except Exception:
            log.exception("largest-file scan failed for %s", mount)
            return
        if result.get("complete") or result.get("scanned_files", 0) > 0:
            self._file_scan_cache[mount] = (time.monotonic(), result)
