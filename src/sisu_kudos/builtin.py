"""Standalone, read-only Linux collectors. GPU telemetry is read from shared JSON.

No private repository, agent framework, GPU driver client or service is required.
The dataclasses deliberately expose only the established dashboard contract.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil


@dataclass
class CpuInfo:
    usage_percent: float | None = None
    load_1: float = 0.0
    load_5: float = 0.0
    load_15: float = 0.0
    temperature: float | None = None
    per_core: list[float] = field(default_factory=list)


@dataclass
class RamInfo:
    total_gb: float = 0.0
    used_gb: float = 0.0
    available_gb: float = 0.0
    cached_gb: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0

    @property
    def used_percent(self) -> float:
        return self.used_gb / self.total_gb * 100 if self.total_gb else 0.0

    @property
    def swap_percent(self) -> float:
        return self.swap_used_gb / self.swap_total_gb * 100 if self.swap_total_gb else 0.0


@dataclass
class GpuProcess:
    pid: int = 0
    name: str = "process"
    memory_mb: float | None = None
    gpu_util: float | None = None


@dataclass
class GpuInfo:
    health: str = "unavailable"
    health_detail: str = "GPU telemetry is not configured"
    telemetry_source: str = "shared JSON"
    utilization_percent: float | None = None
    temperature: float | None = None
    power_watts: float | None = None
    power_limit_watts: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    is_unified_memory: bool = True
    cap_mhz: float | None = None
    gpu_processes: list[GpuProcess] | None = field(default_factory=list)
    history: list[float] = field(default_factory=list)
    mem_history: list[float] = field(default_factory=list)
    temp_history: list[float] = field(default_factory=list)
    power_history: list[float] = field(default_factory=list)

    @property
    def memory_percent(self) -> float | None:
        if self.memory_total_mb and self.memory_used_mb is not None:
            return self.memory_used_mb / self.memory_total_mb * 100
        return None


@dataclass
class NetworkInfo:
    interface: str = ""
    rx_bytes_sec: float | None = None
    tx_bytes_sec: float | None = None
    rx_total_gb: float = 0.0
    tx_total_gb: float = 0.0


@dataclass
class SystemInfo:
    hostname: str = ""
    kernel: str = ""
    uptime_seconds: float = 0.0
    cpu_model: str = ""
    cpu_cores: int = 0
    gpu_name: str = ""


@dataclass
class DiskInfo:
    mount: str = ""
    source: str = ""
    label: str = ""
    total_gb: float | None = None
    used_gb: float | None = None
    free_gb: float | None = None
    use_percent: float | None = None
    is_remote: bool = False
    temperature: float | None = None
    read_mb_s: float | None = None
    write_mb_s: float | None = None
    health: str = "ok"


@dataclass
class ServiceInfo:
    name: str = ""
    status: str = "unknown"
    memory_mb: float | None = None
    activity: str = ""


def finite(value: Any, maximum: float = 1e18) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= maximum else None


def shared_gpu(
    path: Path | None, *, now: datetime | None = None, boot_id: str | None = None
) -> GpuInfo:
    """Read a bounded allowlist; a stale or foreign-boot sample is never a zero."""
    if path is None:
        return GpuInfo()
    try:
        with path.open("rb") as handle:
            raw = handle.read(131_073)
        if len(raw) > 131_072:
            raise ValueError("oversized")
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise ValueError("object required")
        boot = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        updated = datetime.fromisoformat(str(row.get("updated_at", "")).replace("Z", "+00:00"))
        if updated.tzinfo is None or row.get("boot_id") != boot:
            raise ValueError("identity mismatch")
        age = ((now or datetime.now(UTC)) - updated).total_seconds()
    except (OSError, UnicodeError, ValueError, TypeError):
        return GpuInfo(health_detail="Shared GPU telemetry is missing or invalid")
    if not -5 <= age <= 60:
        return GpuInfo(health="stale", health_detail="Shared GPU telemetry is stale")
    health = row.get("health")
    if health != "ok":
        safe_health = (
            health if health in {"unsafe", "reset_required", "reset-required"} else "unavailable"
        )
        return GpuInfo(health=safe_health, health_detail="Shared GPU telemetry is not healthy")
    utilization = finite(row.get("gpu_util_percent"), 100)
    temperature = finite(row.get("gpu_c"), 150)
    power = finite(row.get("power_watts"), 2000)
    if any(value is None for value in (utilization, temperature, power)):
        return GpuInfo(health="partial", health_detail="Shared GPU measurements are incomplete")
    vm = psutil.virtual_memory()
    info = GpuInfo(
        health="ok",
        health_detail="",
        utilization_percent=utilization,
        temperature=temperature,
        power_watts=power,
        power_limit_watts=finite(row.get("power_limit_watts"), 2000),
        memory_used_mb=(vm.total - vm.available) / 1024**2,
        memory_total_mb=vm.total / 1024**2,
        cap_mhz=finite(row.get("cap_mhz"), 10000),
    )
    raw_processes = row.get("gpu_processes")
    if isinstance(raw_processes, list):
        for process in raw_processes[:1024]:
            if not isinstance(process, dict):
                continue
            pid = finite(process.get("pid"), 2**31 - 1)
            if pid is None or not pid.is_integer() or pid < 1:
                continue
            name = process.get("name")
            name = (
                name
                if isinstance(name, str) and re.fullmatch(r"[\w.:-]{1,64}", name)
                else "process"
            )
            info.gpu_processes.append(
                GpuProcess(
                    pid=int(pid),
                    name=name,
                    memory_mb=finite(process.get("memory_mb")),
                    gpu_util=finite(process.get("gpu_util"), 100),
                )
            )
        info.gpu_processes.sort(key=lambda item: item.memory_mb or 0, reverse=True)
        info.gpu_processes = info.gpu_processes[:12]
    else:
        info.gpu_processes = None
    return info


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_read(url: str, *, limit: int = 131072) -> bytes:
    """Bounded GET to an operator-configured URL; never follow a redirect."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(
        urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=1
    ) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("response too large")
        return data


class BuiltinCollectors:
    """Instance-local samplers and operator-declared optional sources."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.SERVICE_ORDER = tuple(item.name for item in settings.services)
        self._net: tuple[float, dict[str, tuple[int, int]]] | None = None
        self._cpu_previous: tuple[float, float] | None = None

    def prime_process_cpu(self) -> None:
        self._cpu_previous = self._cpu_counters()

    @staticmethod
    def _cpu_counters() -> tuple[float, float]:
        counters = psutil.cpu_times()._asdict()
        total = sum(value for key, value in counters.items() if key not in {"guest", "guest_nice"})
        return total, counters.get("idle", 0) + counters.get("iowait", 0)

    def collect_cpu(self) -> CpuInfo:
        load = os.getloadavg()
        counters = self._cpu_counters()
        previous = self._cpu_previous
        self._cpu_previous = counters
        usage = None
        if previous is not None and counters[0] > previous[0] and counters[1] >= previous[1]:
            usage = max(
                0.0,
                min(100.0, 100 * (1 - (counters[1] - previous[1]) / (counters[0] - previous[0]))),
            )
        temperature = None
        try:
            sensors = psutil.sensors_temperatures()
            samples = [
                entry.current
                for name, entries in sensors.items()
                if name in {"cpu_thermal", "coretemp", "k10temp", "acpitz"}
                for entry in entries
                if finite(entry.current, 150) is not None
            ]
            temperature = max(samples) if samples else None
        except (AttributeError, OSError):
            pass
        return CpuInfo(
            usage_percent=usage,
            load_1=load[0],
            load_5=load[1],
            load_15=load[2],
            temperature=temperature,
        )

    @staticmethod
    def collect_ram() -> RamInfo:
        vm, swap = psutil.virtual_memory(), psutil.swap_memory()
        return RamInfo(
            total_gb=vm.total / 1024**3,
            used_gb=(vm.total - vm.available) / 1024**3,
            available_gb=vm.available / 1024**3,
            cached_gb=getattr(vm, "cached", 0) / 1024**3,
            swap_total_gb=swap.total / 1024**3,
            swap_used_gb=swap.used / 1024**3,
        )

    def collect_gpu(self) -> GpuInfo:
        return shared_gpu(self.settings.gpu_state_path)

    def collect_network(self) -> NetworkInfo:
        counters = psutil.net_io_counters(pernic=True)
        configured = self.settings.network_interfaces
        if configured:
            names = tuple(name for name in configured if name in counters)
            if len(names) != len(configured):
                self._net = None
                raise RuntimeError("Configured network interface is unavailable")
        else:
            names = tuple(
                sorted(
                    name for name in counters if (Path("/sys/class/net") / name / "device").exists()
                )
            )
            if not names:
                names = tuple(
                    sorted(
                        name
                        for name in counters
                        if name != "lo" and not name.startswith(("veth", "br-", "docker"))
                    )
                )
        values = {name: (counters[name].bytes_recv, counters[name].bytes_sent) for name in names}
        now = time.monotonic()
        rx = tx = None
        if self._net is not None and values and self._net[0] < now:
            old_time, old = self._net
            if old.keys() == values.keys() and all(
                values[n][i] >= old[n][i] for n in values for i in (0, 1)
            ):
                rx = sum(values[n][0] - old[n][0] for n in values) / (now - old_time)
                tx = sum(values[n][1] - old[n][1] for n in values) / (now - old_time)
        self._net = (now, values)
        return NetworkInfo(
            interface=", ".join(names),
            rx_bytes_sec=rx,
            tx_bytes_sec=tx,
            rx_total_gb=sum(v[0] for v in values.values()) / 1024**3,
            tx_total_gb=sum(v[1] for v in values.values()) / 1024**3,
        )

    @staticmethod
    def collect_system() -> SystemInfo:
        return SystemInfo(
            hostname=socket.gethostname(),
            kernel=platform.release(),
            uptime_seconds=max(0, time.time() - psutil.boot_time()),
            cpu_model=platform.machine(),
            cpu_cores=psutil.cpu_count() or 0,
        )

    def collect_disks(self) -> list[DiskInfo]:
        partitions = {item.mountpoint: item for item in psutil.disk_partitions(all=True)}
        rows = []
        for mount in self.settings.disk_mounts:
            partition = partitions.get(mount)
            row = DiskInfo(mount=mount, label="Root" if mount == "/" else Path(mount).name)
            if partition is None:
                row.health = "unavailable"
                rows.append(row)
                continue
            row.source = partition.device
            row.is_remote = partition.fstype in {
                "cifs",
                "smb3",
                "nfs",
                "nfs4",
                "sshfs",
                "fuse.sshfs",
            }
            try:
                usage = psutil.disk_usage(mount)
                row.total_gb, row.used_gb, row.free_gb = (
                    v / 1024**3 for v in (usage.total, usage.used, usage.free)
                )
                row.use_percent = usage.percent
            except OSError:
                row.health = "unavailable"
            rows.append(row)
        return rows

    def collect_service(self, name: str) -> ServiceInfo:
        config = next(item for item in self.settings.services if item.name == name)
        row = ServiceInfo(name=config.label or name)
        if config.kind == "http":
            try:
                http_read(config.url, limit=16384)
                row.status, row.activity = "running", "HTTP endpoint reachable"
            except (OSError, ValueError, urllib.error.URLError):
                row.status, row.activity = "stopped", "HTTP endpoint unavailable"
            return row
        command = ["systemctl"] + (["--user"] if config.scope == "user" else [])
        command += ["show", config.unit, "--property=ActiveState,MemoryCurrent", "--no-pager"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False)
            if result.returncode:
                return row
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            state = values.get("ActiveState", "unknown")
            row.status = {
                "active": "running",
                "reloading": "running",
                "activating": "degraded",
                "inactive": "stopped",
                "failed": "stopped",
            }.get(state, "unknown")
            memory = values.get("MemoryCurrent", "")
            row.memory_mb = (
                int(memory) / 1024**2 if memory.isdigit() and int(memory) < 2**63 else None
            )
            row.activity = row.status
        except (OSError, subprocess.TimeoutExpired):
            pass
        return row

    def collect_model(self) -> dict[str, Any]:
        # Share the thoroughly fenced vLLM counter parser without launching SSH.
        from .remote import KideCollector

        if not hasattr(self, "_model_parser"):
            self._model_parser = KideCollector()
        url = self.settings.model_url.rstrip("/")
        model: dict[str, Any] = {
            "health": "unavailable",
            "sampled_at": datetime.now(UTC).isoformat(),
        }
        try:
            payload = json.loads(http_read(url + "/v1/models", limit=32768))
            items = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise ValueError("single model required")
            item = items[0]
            name = item.get("id")
            model.update(name=name, context_length=item.get("max_model_len"), health="partial")
            metrics = http_read(url + "/metrics").decode("utf-8", "replace")

            def metric(key: str, labelled: bool = True) -> float | None:
                for line in metrics.splitlines():
                    parts = line.split()
                    if len(parts) != 2:
                        continue
                    if labelled:
                        if not parts[0].startswith(key + "{"):
                            continue
                        found = re.search(r'(?:\{|,)model_name="([^"\\]*)"(?:,|\})', parts[0])
                        if found is None or found[1] != name:
                            continue
                    elif parts[0] != key:
                        continue
                    try:
                        return finite(float(parts[1]))
                    except ValueError:
                        return None
                return None

            kv = metric("vllm:kv_cache_usage_perc")
            model.update(
                requests_running=metric("vllm:num_requests_running"),
                requests_waiting=metric("vllm:num_requests_waiting"),
                kv_cache_percent=kv * 100 if kv is not None and kv <= 1 else None,
                _prompt_tokens_total=metric("vllm:prompt_tokens_total"),
                _generation_tokens_total=metric("vllm:generation_tokens_total"),
                _server_started=metric("process_start_time_seconds", False),
            )
            http_read(url + "/health", limit=1024)
            model["health"] = "ok"
        except (OSError, ValueError, TypeError, urllib.error.URLError):
            pass
        return self._model_parser._model(model, "local", time.monotonic())
