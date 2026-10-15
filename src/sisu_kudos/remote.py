"""Read-only telemetry for an explicitly configured remote host."""

from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

from .config import Settings, validate_remote_settings

# Fixed code sent over an explicitly configured authenticated SSH route. The
# only substitutions are JSON-quoted config values, never shell command parts.
PROBE = r"""
import json, math, pathlib, re, socket, time, urllib.request
from datetime import datetime, timezone
import psutil
p=pathlib.Path
gpu_state_path=__GPU_STATE_PATH_LITERAL__
model_url=__MODEL_URL_LITERAL__
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
http=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
boot=p('/proc/sys/kernel/random/boot_id').read_text().strip()
cpu=psutil.cpu_percent(interval=0.2)
m=psutil.virtual_memory(); d=psutil.disk_usage('/')
net={k:[v.bytes_recv,v.bytes_sent] for k,v in psutil.net_io_counters(pernic=True).items()
     if (p('/sys/class/net')/k/'device').exists()}

def endpoint(path, limit):
    if not model_url:
        return None
    try:
        request=urllib.request.Request(
            model_url+path,
            headers={'Accept':'application/json'},
        )
        with http.open(request, timeout=0.45) as response:
            if response.status != 200:
                return None
            body=response.read(limit+1)
            return body if len(body) <= limit else None
    except Exception:
        return None

def metric_value(text, metric, model_name=None):
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        fields=line.split()
        if len(fields) < 2:
            continue
        sample=fields[0]
        if model_name is None:
            if sample != metric:
                continue
        else:
            prefix=metric+'{'
            if not sample.startswith(prefix) or not sample.endswith('}'):
                continue
            labels=sample[len(prefix):-1]
            match=re.search(r'(?:^|,)model_name="([^\"]*)"(?:,|$)', labels)
            if not match or match.group(1) != model_name:
                continue
        try:
            value=float(fields[1])
            return value if math.isfinite(value) and value >= 0 else None
        except (ValueError, OverflowError):
            return None
    return None

sampled=datetime.now(timezone.utc).isoformat()
model={'name':None,'context_length':None,'requests_running':None,'requests_waiting':None,
       'kv_cache_percent':None,'health':'unknown','sampled_at':sampled,
       '_prompt_tokens_total':None,'_generation_tokens_total':None,'_server_started':None}
health_body=endpoint('/health', 1024)
models_body=endpoint('/v1/models', 32768)
metrics_body=endpoint('/metrics', 131072)
try:
    models=json.loads(models_body) if models_body is not None else None
    data=models.get('data') if isinstance(models,dict) else None
    if isinstance(data,list) and len(data)==1 and isinstance(data[0],dict):
        item=data[0]
        name=item.get('id')
        if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}', name):
            model['name']=name
            context=item.get('max_model_len')
            if isinstance(context,int) and not isinstance(context,bool) and 0 < context <= 10000000:
                model['context_length']=context
            if metrics_body is not None:
                metrics=metrics_body.decode('utf-8','replace')
                model['requests_running']=metric_value(metrics,'vllm:num_requests_running',name)
                model['requests_waiting']=metric_value(metrics,'vllm:num_requests_waiting',name)
                kv=metric_value(metrics,'vllm:kv_cache_usage_perc',name)
                model['kv_cache_percent']=kv*100 if kv is not None and kv <= 1 else None
                model['_prompt_tokens_total']=metric_value(metrics,'vllm:prompt_tokens_total',name)
                model['_generation_tokens_total']=metric_value(metrics,'vllm:generation_tokens_total',name)
                model['_server_started']=metric_value(metrics,'process_start_time_seconds')
            model['health']=(
                'ok' if health_body is not None and metrics_body is not None else 'partial'
            )
    elif models_body is not None:
        model['health']='partial'
except (ValueError, TypeError, AttributeError):
    model['health']='partial' if models_body is not None else 'unknown'

processes=[]
process_count=0
process_limit=1024
process_attrs=['pid','name','create_time','cpu_times','memory_info','io_counters']
for proc in psutil.process_iter(process_attrs):
    try:
        info=proc.info
        pid=info.get('pid')
        memory=info.get('memory_info')
        if not isinstance(pid,int) or memory is None:
            continue
        process_count += 1
        if len(processes) >= process_limit:
            continue
        created=info.get('create_time')
        cpu_times=info.get('cpu_times')
        cpu_time=None
        if cpu_times is not None:
            try:
                cpu_time=float(cpu_times.user)+float(cpu_times.system)
                if not math.isfinite(cpu_time) or cpu_time < 0:
                    cpu_time=None
            except (AttributeError, TypeError, ValueError, OverflowError):
                cpu_time=None
        io=info.get('io_counters')
        processes.append({'pid':pid,'name':str(info.get('name') or 'process')[:64],
            'create_time':created,'cpu_time':cpu_time,'rss':memory.rss,
            'read_bytes':getattr(io,'read_bytes',None),'write_bytes':getattr(io,'write_bytes',None)})
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        continue
g={}
if gpu_state_path:
    try:
        with open(gpu_state_path) as f:
            g=json.loads(f.read(131072))
    except (OSError,ValueError):
        g={}
print(json.dumps(dict(host=socket.gethostname(),boot_id=boot,mono=time.monotonic(),
    wall=time.time(),cpu=cpu,cores=psutil.cpu_count(),uptime=time.time()-psutil.boot_time(),
    ram=dict(total=m.total,available=m.available),
    disk=dict(total=d.total,used=d.used,free=d.free,percent=d.percent),net=net,guard=g,
    processes=processes,process_count=process_count,processes_truncated=process_count>process_limit,
    model=model)))
"""


def number(value: Any, low: float = 0, high: float = 1e18) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and low <= value <= high else None


def timestamp(value: Any) -> float | None:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


class RemoteCollector:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.previous: dict[str, Any] | None = None
        self.previous_processes: dict[tuple[int, float], dict[str, float | None]] = {}
        self.process_sample: tuple[str, float] | None = None
        self.previous_model: dict[str, Any] | None = None

    def __call__(self) -> dict[str, Any]:
        if not self.settings.remote_enabled:
            raise RuntimeError("Remote host is not configured")
        target, python, _, gpu_state_path, model_url = validate_remote_settings(self.settings)
        probe = PROBE.replace("__GPU_STATE_PATH_LITERAL__", json.dumps(gpu_state_path)).replace(
            "__MODEL_URL_LITERAL__", json.dumps(model_url)
        )
        # Strict host checking, no multiplexed control socket writes in the service sandbox.
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "ConnectionAttempts=1",
                    "-o",
                    "ControlMaster=no",
                    "-o",
                    "ControlPath=none",
                    "-o",
                    "ServerAliveInterval=2",
                    "-o",
                    "ServerAliveCountMax=1",
                    target,
                    python,
                    "-",
                ],
                input=probe,
                text=True,
                capture_output=True,
                timeout=6,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Remote SSH timed out") from None
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("Remote SSH unavailable") from None
        if result.returncode:
            raise RuntimeError("Remote SSH unavailable")  # never expose SSH stderr/paths
        if len(result.stdout) > 262144:
            raise ValueError("Remote response too large")
        return self.parse(json.loads(result.stdout))

    def _model(self, value: Any, boot: str, mono: float) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        name = source.get("name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}", name
        ):
            name = None
        context_length = source.get("context_length")
        if (
            isinstance(context_length, bool)
            or not isinstance(context_length, int)
            or not 0 < context_length <= 10_000_000
        ):
            context_length = None
        health = source.get("health")
        if health not in {"ok", "partial", "unknown", "unavailable"}:
            health = "unknown"
        sampled_at = source.get("sampled_at")
        if not isinstance(sampled_at, str) or timestamp(sampled_at) is None:
            sampled_at = None

        prompt_total = number(source.get("_prompt_tokens_total"))
        generation_total = number(source.get("_generation_tokens_total"))
        server_started = number(source.get("_server_started"), 1)
        prompt_rate = generation_rate = None
        previous = self.previous_model
        if (
            name is not None
            and server_started is not None
            and previous is not None
            and previous["identity"] == (boot, name, context_length, server_started)
            and mono > previous["mono"]
        ):
            elapsed = mono - previous["mono"]
            old_prompt, old_generation = previous["prompt"], previous["generation"]
            if prompt_total is not None and old_prompt is not None and prompt_total >= old_prompt:
                prompt_rate = round((prompt_total - old_prompt) / elapsed, 3)
            if (
                generation_total is not None
                and old_generation is not None
                and generation_total >= old_generation
            ):
                generation_rate = round((generation_total - old_generation) / elapsed, 3)
        if name is not None and server_started is not None:
            self.previous_model = {
                "identity": (boot, name, context_length, server_started),
                "mono": mono,
                "prompt": prompt_total,
                "generation": generation_total,
            }
        else:
            self.previous_model = None

        def gauge(key: str, maximum: float = 1_000_000) -> int | None:
            raw = number(source.get(key), 0, maximum)
            return int(raw) if raw is not None and raw.is_integer() else None

        kv_cache_percent = number(source.get("kv_cache_percent"), 0, 100)
        if name is None and health == "ok":
            health = "partial"
        return {
            "name": name,
            "context_length": context_length,
            "requests_running": gauge("requests_running"),
            "requests_waiting": gauge("requests_waiting"),
            "kv_cache_percent": kv_cache_percent,
            "prompt_tokens_per_second": prompt_rate,
            "generation_tokens_per_second": generation_rate,
            "health": health,
            "sampled_at": sampled_at,
        }

    def _process_views(
        self, row: dict[str, Any], boot: str, mono: float, wall: float
    ) -> dict[str, Any]:
        source = row.get("processes")
        source = source[:1024] if isinstance(source, list) else []
        previous = self.previous_processes
        previous_sample = self.process_sample
        elapsed = (
            mono - previous_sample[1]
            if previous_sample is not None
            and previous_sample[0] == boot
            and mono > previous_sample[1]
            else None
        )
        current: dict[tuple[int, float], dict[str, float | None]] = {}
        rows: list[dict[str, Any]] = []
        for raw in source:
            if not isinstance(raw, dict):
                continue
            pid_value = number(raw.get("pid"), 1, 2**31)
            created = number(raw.get("create_time"), 1)
            rss = number(raw.get("rss"))
            if pid_value is None or rss is None:
                continue
            pid = int(pid_value)
            cpu_total = number(raw.get("cpu_time"))
            read_total = number(raw.get("read_bytes"))
            write_total = number(raw.get("write_bytes"))
            identity = (pid, created) if created is not None else None
            old = previous.get(identity) if identity is not None and elapsed is not None else None
            cpu_percent = read_rate = write_rate = None
            if old is not None and elapsed is not None:
                old_cpu = old.get("cpu")
                if cpu_total is not None and old_cpu is not None and cpu_total >= old_cpu:
                    cpu_percent = round(100 * (cpu_total - old_cpu) / elapsed, 1)
                old_read, old_write = old.get("read"), old.get("write")
                if read_total is not None and old_read is not None and read_total >= old_read:
                    read_rate = round((read_total - old_read) / elapsed, 1)
                if write_total is not None and old_write is not None and write_total >= old_write:
                    write_rate = round((write_total - old_write) / elapsed, 1)
            if identity is not None:
                current[identity] = {"cpu": cpu_total, "read": read_total, "write": write_total}
            name = raw.get("name")
            name = (
                name
                if isinstance(name, str) and re.fullmatch(r"[\w.:-]{1,64}", name)
                else "process"
            )
            rows.append(
                {
                    "pid": pid,
                    "name": name,
                    "cpu_percent": cpu_percent,
                    "memory_mb": round(rss / 1024**2, 1),
                    "read_bytes_sec": read_rate,
                    "write_bytes_sec": write_rate,
                }
            )
        self.previous_processes = current
        self.process_sample = (boot, mono)
        by_cpu = sorted(
            rows,
            key=lambda item: (
                item["cpu_percent"] is not None,
                item["cpu_percent"] or 0,
                item["memory_mb"],
            ),
            reverse=True,
        )
        by_memory = sorted(
            rows, key=lambda item: (item["memory_mb"], item["cpu_percent"] or -1), reverse=True
        )
        by_io = sorted(
            rows,
            key=lambda item: (
                item["read_bytes_sec"] is not None or item["write_bytes_sec"] is not None,
                (item["read_bytes_sec"] or 0) + (item["write_bytes_sec"] or 0),
            ),
            reverse=True,
        )
        process_count = number(row.get("process_count"), 0, 1_000_000)
        truncated = row.get("processes_truncated") is True or (
            process_count is not None and process_count > 1024
        )
        note = (
            "CPU and disk I/O rates are measured intervals tied to PID and start time; "
            "the first or reset counter is unknown. Disk I/O is per process, "
            "not per mount; per-process network I/O is unavailable."
        )
        if truncated:
            note += " Process sample is limited to 1024 rows."
        return {
            "cpu": by_cpu[:12],
            "memory": by_memory[:12],
            "io": by_io[:12],
            "count": int(process_count) if process_count is not None else len(rows),
            "sampled_at": datetime.fromtimestamp(wall, UTC).isoformat(),
            "sample_note": note,
        }

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        expected = self.settings.remote_hostname
        actual = row.get("host") if isinstance(row, dict) else None
        if (
            not isinstance(actual, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", actual)
            or expected is not None
            and actual != expected
        ):
            raise ValueError("Remote identity mismatch")
        boot, mono, wall = row.get("boot_id"), number(row.get("mono")), number(row.get("wall"))
        if not isinstance(boot, str) or not re.fullmatch(r"[a-f0-9-]{36}", boot):
            raise ValueError("Remote boot identity missing")
        if mono is None or wall is None:
            raise ValueError("Remote timestamp missing")
        ram, disk = row.get("ram", {}), row.get("disk", {})
        if not isinstance(ram, dict) or not isinstance(disk, dict):
            raise ValueError("Remote memory/storage invalid")
        total, available = number(ram.get("total"), 1), number(ram.get("available"))
        if total is None or available is None or available > total:
            raise ValueError("Remote memory invalid")
        cpu = number(row.get("cpu"), 0, 100)
        if cpu is None:
            raise ValueError("Remote CPU invalid")
        guard = row.get("guard")
        guard = guard if isinstance(guard, dict) else {}
        updated = timestamp(guard.get("updated_at"))
        valid = (
            guard.get("boot_id") == boot
            and updated is not None
            and -5 <= wall - updated <= 60
            and guard.get("health") == "ok"
        )
        health = "ok" if valid else "unavailable"
        if updated is not None and (wall - updated > 60 or wall - updated < -5):
            health = "stale"
        if valid and any(
            number(guard.get(k), 0, hi) is None
            for k, hi in (("gpu_util_percent", 100), ("gpu_c", 150), ("power_watts", 2000))
        ):
            health = "partial"
        if not valid and guard.get("health") in ("unsafe", "reset_required", "stale"):
            health = guard["health"]
        gpu = {
            "health": health,
            "health_status": health,
            "health_detail": "" if valid else "GPU telemetry is missing or stale",
            "telemetry_source": "configured remote GPU state",
            "is_unified_memory": True,
            "utilization_percent": number(guard.get("gpu_util_percent"), 0, 100) if valid else None,
            "temperature": number(guard.get("gpu_c"), 0, 150) if valid else None,
            "power_watts": number(guard.get("power_watts"), 0, 2000) if valid else None,
            "cap_mhz": number(guard.get("cap_mhz"), 0, 10000) if valid else None,
            "memory_used_mb": (total - available) / 1024**2,
            "memory_total_mb": total / 1024**2,
            "memory_percent": 100 * (total - available) / total,
            "gpu_processes": None,
        }
        processes = guard.get("gpu_processes")
        if valid and isinstance(processes, list):
            gpu["gpu_processes"] = []
            for p in processes:
                if not isinstance(p, dict):
                    continue
                name = p.get("name", "")
                name = name if isinstance(name, str) else ""
                # Executable label, never path, command line or arbitrary guard detail.
                name = name if re.fullmatch(r"[\w.:-]{1,64}", name) else "process"
                pid = number(p.get("pid"), 1, 2**31)
                if pid is not None:
                    gpu["gpu_processes"].append(
                        {
                            "pid": int(pid),
                            "name": name,
                            "memory_mb": number(p.get("memory_mb")),
                            # The shared thermal guard reports allocation, not process utilization.
                            "gpu_util": None,
                        }
                    )
            gpu["gpu_processes"].sort(
                key=lambda process: (
                    process["memory_mb"] is not None,
                    process["memory_mb"] or 0,
                ),
                reverse=True,
            )
            gpu["gpu_processes"] = gpu["gpu_processes"][:12]
        net = row.get("net", {})
        net = (
            {
                k: v
                for k, v in net.items()
                if isinstance(k, str)
                and re.fullmatch(r"[\w.-]{1,32}", k)
                and isinstance(v, list)
                and len(v) == 2
                and all(number(n) is not None for n in v)
            }
            if isinstance(net, dict)
            else {}
        )
        rx = tx = None
        prev = self.previous
        if prev and prev["boot_id"] == boot and mono > prev["mono"] and net:
            old = prev["net"]
            if old.keys() == net.keys() and all(
                net[k][i] >= old[k][i] for k in net for i in (0, 1)
            ):
                dt = mono - prev["mono"]
                rx = sum(net[k][0] - old[k][0] for k in net) / dt
                tx = sum(net[k][1] - old[k][1] for k in net) / dt
        self.previous = {"boot_id": boot, "mono": mono, "net": {k: list(v) for k, v in net.items()}}
        process_views = self._process_views(row, boot, mono, wall)
        model = self._model(row.get("model"), boot, mono)
        gb = 1024**3
        disks = []
        dt, df, du = (number(disk.get(k)) for k in ("total", "free", "used"))
        if dt and df is not None and du is not None and df <= dt and du <= dt:
            disks = [
                {
                    "label": "NVMe",
                    "mount": "/",
                    "is_remote": False,
                    "total_gb": dt / gb,
                    "free_gb": df / gb,
                    "used_gb": du / gb,
                    "use_percent": number(disk.get("percent"), 0, 100),
                    "temperature": number(guard.get("nvme_max_c"), 0, 150) if valid else None,
                }
            ]
        return {
            "identity": {
                "hostname": actual,
                "cpu_cores": number(row.get("cores"), 1, 4096),
                "uptime_seconds": number(row.get("uptime")),
            },
            "cpu": {
                "usage_percent": cpu,
                "temperature": number(guard.get("acpi_max_c"), 0, 150) if valid else None,
            },
            "ram": {
                "total_gb": total / gb,
                "available_gb": available / gb,
                "used_gb": (total - available) / gb,
                "used_percent": 100 * (total - available) / total,
            },
            "gpu": gpu,
            "model": model,
            "processes": process_views,
            "network": {
                "interface": ", ".join(sorted(net)),
                "rx_bytes_sec": rx,
                "tx_bytes_sec": tx,
            },
            "disks": disks,
        }


# Backward-compatible technical class name; IDs and labels now come from config.
KideCollector = RemoteCollector
