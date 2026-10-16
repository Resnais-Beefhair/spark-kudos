"""Strict, runtime-loaded TOML configuration for standalone KUDOS."""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """A user-facing configuration error with no secret values included."""


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    name: str
    label: str = ""
    kind: str = "systemd"
    unit: str = ""
    scope: str = "user"
    url: str = ""


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    language: str = "en"
    local_host_id: str = "local"
    local_host_label: str = "Spark 1"
    local_host_role: str = "agents-services"
    remote_host: str | None = None
    remote_hostname: str | None = None
    remote_host_id: str = "remote"
    remote_host_label: str = "Spark 2"
    remote_host_role: str = "model-server"
    remote_python: str = "python3"
    remote_gpu_state_path: str = ""
    remote_model_url: str = ""
    gpu_state_path: Path | None = None
    network_interfaces: tuple[str, ...] = ()
    disk_mounts: tuple[str, ...] = ("/",)
    services: tuple[ServiceConfig, ...] = ()
    model_url: str = ""
    agents_enabled: bool = False
    logs_enabled: bool = False
    crons_enabled: bool = False
    resources_enabled: bool = False
    quotas_enabled: bool = False
    claude_quota_enabled: bool = False
    claude_credentials_path: Path | None = None
    agentmon_repo: Path | None = None
    sisu_monitor_repo: Path | None = None
    hermes_log_dir: Path = field(default_factory=lambda: Path.home() / ".hermes" / "logs")
    openclaw_home: Path = field(default_factory=lambda: Path.home() / ".openclaw")
    resource_snapshot_path: Path | None = None
    hermes_profiles: tuple[tuple[str, Path], ...] = ()
    system_interval_s: float = 2.0
    agent_interval_s: float = 15.0
    cron_interval_s: float = 5.0
    slow_interval_s: float = 12.0

    @property
    def remote_enabled(self) -> bool:
        return self.remote_host is not None

    @property
    def hermes_profile_log_dirs(self) -> tuple[tuple[str, Path], ...]:
        """Return only explicitly configured profiles, or generic default when opted in."""
        if self.hermes_profiles:
            return self.hermes_profiles
        if self.logs_enabled or self.crons_enabled:
            return (("default", self.hermes_log_dir),)
        return ()


def _fail(where: str, detail: str) -> ConfigurationError:
    return ConfigurationError(f"{where}: {detail}")


def _table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(name, "must be a TOML table")
    return value


def _only(table: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        names = ", ".join(extra)
        raise _fail(where, f"unknown field(s): {names}")


def _string(value: Any, where: str, *, default: str = "", nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(where, "must be a string")
    if "\x00" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _fail(where, "must not contain control characters")
    if nonempty and not value.strip():
        raise _fail(where, "must not be empty")
    return value


def _label(value: Any, where: str, *, default: str) -> str:
    result = _string(value, where, default=default, nonempty=True)
    if len(result) > 80 or not all(ch.isprintable() for ch in result):
        raise _fail(where, "must be printable and at most 80 characters")
    return result


def _identifier(value: Any, where: str, *, default: str) -> str:
    result = _string(value, where, default=default, nonempty=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", result):
        raise _fail(where, "must be 1-32 letters, digits, '_' or '-' and start alphanumeric")
    return result


def _path(value: Any, where: str, base: Path, *, default: Path | None = None) -> Path | None:
    if value is None and default is not None:
        return default
    raw = _string(value, where)
    if raw == "":
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).resolve(strict=False)


def _optional_url(value: Any, where: str) -> str:
    raw = _string(value, where)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise _fail(where, "must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
        or any(ch.isspace() for ch in raw)
    ):
        raise _fail(where, "must be an HTTP(S) URL without credentials, query or fragment")
    return raw.rstrip("/")


def _interval(value: Any, where: str, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(where, "must be a number")
    result = float(value)
    if not 0.1 <= result <= 3600:
        raise _fail(where, "must be between 0.1 and 3600 seconds")
    return result


def _tuple_strings(value: Any, where: str, *, mounts: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _fail(where, "must be an array of strings")
    result = []
    for item in value:
        if not item or any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
            raise _fail(where, "entries must be nonempty and contain no control characters")
        if mounts:
            path = Path(item)
            if not path.is_absolute() or ".." in path.parts:
                raise _fail(where, "mounts must be absolute paths without '..'")
        elif not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", item):
            raise _fail(where, "interface names contain unsupported characters")
        if item in result:
            raise _fail(where, f"duplicate entry: {item}")
        result.append(item)
    return tuple(result)


def _service_rows(value: Any, base: Path) -> tuple[ServiceConfig, ...]:
    if not isinstance(value, list):
        raise _fail("services", "must be an array of tables")
    result: list[ServiceConfig] = []
    seen: set[str] = set()
    allowed = {"name", "label", "kind", "unit", "scope", "url"}
    for index, raw in enumerate(value):
        where = f"services[{index}]"
        row = _table(raw, where)
        _only(row, allowed, where)
        name = _identifier(row.get("name"), f"{where}.name", default="")
        if name in seen:
            raise _fail(f"{where}.name", "must be unique")
        seen.add(name)
        label = (
            _label(row.get("label", ""), f"{where}.label", default="")
            if row.get("label", "")
            else ""
        )
        kind = _string(row.get("kind", "systemd"), f"{where}.kind", nonempty=True)
        if kind not in {"systemd", "http"}:
            raise _fail(f"{where}.kind", "must be 'systemd' or 'http'")
        unit = _string(row.get("unit", ""), f"{where}.unit")
        scope = _string(row.get("scope", "user"), f"{where}.scope", nonempty=True)
        if scope not in {"user", "system"}:
            raise _fail(f"{where}.scope", "must be 'user' or 'system'")
        url = _optional_url(row.get("url", ""), f"{where}.url")
        if kind == "http" and not url:
            raise _fail(f"{where}.url", "is required for an HTTP service")
        if kind == "systemd" and not unit:
            raise _fail(f"{where}.unit", "is required for a systemd service")
        result.append(ServiceConfig(name, label, kind, unit, scope, url))
    return tuple(result)


def _remote_target(value: Any, where: str) -> str:
    target = _string(value, where, nonempty=True)
    # ssh treats the final remote command through a shell. Keep both the route
    # and command token to a deliberately narrow, whitespace-free alphabet.
    if (
        len(target) > 255
        or target.startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*", target)
        or target.count("@") > 1
    ):
        raise _fail(where, "must be a safe SSH destination (host, alias, or user@host)")
    return target


def _remote_python(value: Any, where: str) -> str:
    command = _string(value, where, nonempty=True)
    if (
        len(command) > 128
        or command.startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9_/][A-Za-z0-9_./+-]*", command)
    ):
        raise _fail(where, "must be one executable path/name, not arguments or shell syntax")
    return command


def _remote_state_path(value: Any, where: str) -> str:
    raw = _string(value, where)
    if raw and (not PurePosixPath(raw).is_absolute() or ".." in PurePosixPath(raw).parts):
        raise _fail(where, "must be an absolute remote path without '..', or empty")
    return raw


def validate_remote_settings(settings: Settings) -> tuple[str, str, str, str, str]:
    """Recheck the remote boundary for programmatically constructed settings."""
    if not settings.remote_enabled:
        raise ConfigurationError("Remote host is not configured")
    target = _remote_target(settings.remote_host, "hosts.remote.target")
    python = _remote_python(settings.remote_python, "hosts.remote.python")
    hostname = _string(settings.remote_hostname, "hosts.remote.expected_hostname", nonempty=True)
    if len(hostname) > 255 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", hostname):
        raise _fail("hosts.remote.expected_hostname", "must be a hostname token")
    gpu_state_path = _remote_state_path(
        settings.remote_gpu_state_path, "hosts.remote.gpu_state_path"
    )
    model_url = _optional_url(settings.remote_model_url, "hosts.remote.model_url")
    return target, python, hostname, gpu_state_path, model_url


def _parse(data: dict[str, Any], base: Path) -> Settings:
    sections = {
        "server",
        "site",
        "hosts",
        "system",
        "integrations",
        "paths",
        "intervals",
        "services",
        "logs",
    }
    _only(data, sections, "configuration")

    server = _table(data.get("server", {}), "server")
    _only(server, {"host", "port"}, "server")
    host = _string(server.get("host", "127.0.0.1"), "server.host", nonempty=True)
    try:
        ipaddress.ip_address(host)
        valid_host = True
    except ValueError:
        valid_host = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host))
    if not valid_host:
        raise _fail("server.host", "must be an IP address or DNS host name")
    port = server.get("port", 8787)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise _fail("server.port", "must be an integer from 1 to 65535")

    site = _table(data.get("site", {}), "site")
    _only(site, {"language"}, "site")
    language = _string(site.get("language", "en"), "site.language", nonempty=True)
    if language not in {"en", "fi"}:
        raise _fail("site.language", "must be 'en' or 'fi'")

    hosts = _table(data.get("hosts", {}), "hosts")
    _only(hosts, {"local", "remote"}, "hosts")
    local = _table(hosts.get("local", {}), "hosts.local")
    _only(local, {"id", "label", "role"}, "hosts.local")
    local_id = _identifier(local.get("id", "local"), "hosts.local.id", default="local")
    local_label = _label(local.get("label", "Spark 1"), "hosts.local.label", default="Spark 1")
    local_role = _label(
        local.get("role", "agents-services"), "hosts.local.role", default="agents-services"
    )

    remote_table = _table(hosts.get("remote", {}), "hosts.remote")
    _only(
        remote_table,
        {
            "enabled",
            "target",
            "expected_hostname",
            "id",
            "label",
            "role",
            "python",
            "gpu_state_path",
            "model_url",
        },
        "hosts.remote",
    )
    enabled = remote_table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _fail("hosts.remote.enabled", "must be a boolean")
    remote_id = _identifier(remote_table.get("id", "remote"), "hosts.remote.id", default="remote")
    remote_label = _label(
        remote_table.get("label", "Spark 2"), "hosts.remote.label", default="Spark 2"
    )
    remote_role = _label(
        remote_table.get("role", "model-server"), "hosts.remote.role", default="model-server"
    )
    remote_host = remote_hostname = None
    remote_python = _remote_python(remote_table.get("python", "python3"), "hosts.remote.python")
    remote_gpu_state_path = _remote_state_path(
        remote_table.get("gpu_state_path", ""), "hosts.remote.gpu_state_path"
    )
    remote_model_url = _optional_url(remote_table.get("model_url", ""), "hosts.remote.model_url")
    if enabled:
        if "target" not in remote_table:
            raise _fail("hosts.remote.target", "is required when the remote host is enabled")
        if "expected_hostname" not in remote_table:
            raise _fail(
                "hosts.remote.expected_hostname",
                "is required when the remote host is enabled",
            )
        remote_host = _remote_target(remote_table.get("target"), "hosts.remote.target")
        remote_hostname = _string(
            remote_table.get("expected_hostname"), "hosts.remote.expected_hostname", nonempty=True
        )
        if len(remote_hostname) > 255 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote_hostname
        ):
            raise _fail("hosts.remote.expected_hostname", "must be a hostname token")
        if remote_id == local_id:
            raise _fail("hosts.remote.id", "must differ from hosts.local.id")
    else:
        if "target" in remote_table:
            _remote_target(remote_table["target"], "hosts.remote.target")
        if "expected_hostname" in remote_table:
            expected_hostname = _string(
                remote_table["expected_hostname"],
                "hosts.remote.expected_hostname",
                nonempty=True,
            )
            if len(expected_hostname) > 255 or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*", expected_hostname
            ):
                raise _fail("hosts.remote.expected_hostname", "must be a hostname token")

    system = _table(data.get("system", {}), "system")
    _only(system, {"gpu_state_path", "network_interfaces", "disk_mounts", "model_url"}, "system")
    gpu_state_raw = _string(system.get("gpu_state_path", ""), "system.gpu_state_path")
    gpu_state_path = _path(gpu_state_raw, "system.gpu_state_path", base) if gpu_state_raw else None
    network_interfaces = _tuple_strings(
        system.get("network_interfaces", []), "system.network_interfaces"
    )
    disk_mounts = _tuple_strings(
        system.get("disk_mounts", ["/"]), "system.disk_mounts", mounts=True
    )
    model_url = _optional_url(system.get("model_url", ""), "system.model_url")

    integrations = _table(data.get("integrations", {}), "integrations")
    integration_names = {"agents", "logs", "crons", "resources", "quotas", "claude_quota"}
    _only(integrations, integration_names, "integrations")
    flags: dict[str, bool] = {}
    for name in integration_names:
        value = integrations.get(name, False)
        if not isinstance(value, bool):
            raise _fail(f"integrations.{name}", "must be a boolean")
        flags[f"{name}_enabled"] = value

    paths = _table(data.get("paths", {}), "paths")
    _only(
        paths,
        {
            "agentmon_repo",
            "sisu_monitor_repo",
            "hermes_log_dir",
            "openclaw_home",
            "resource_snapshot_path",
            "claude_credentials",
        },
        "paths",
    )
    home = Path.home()
    agentmon_repo = _path(paths.get("agentmon_repo", ""), "paths.agentmon_repo", base)
    sisu_monitor_repo = _path(paths.get("sisu_monitor_repo", ""), "paths.sisu_monitor_repo", base)
    hermes_log_dir = _path(
        paths.get("hermes_log_dir", str(home / ".hermes" / "logs")),
        "paths.hermes_log_dir",
        base,
    )
    openclaw_home = _path(
        paths.get("openclaw_home", str(home / ".openclaw")), "paths.openclaw_home", base
    )
    resource_snapshot_path = _path(
        paths.get("resource_snapshot_path", ""), "paths.resource_snapshot_path", base
    )

    claude_credentials_path = _path(
        paths.get("claude_credentials", ""), "paths.claude_credentials", base
    )

    logs = _table(data.get("logs", {}), "logs")
    _only(logs, {"hermes_profiles"}, "logs")
    profiles_value = logs.get("hermes_profiles", [])
    if not isinstance(profiles_value, list):
        raise _fail("logs.hermes_profiles", "must be an array of tables")
    profiles: list[tuple[str, Path]] = []
    profile_names: set[str] = set()
    for index, raw in enumerate(profiles_value):
        where = f"logs.hermes_profiles[{index}]"
        row = _table(raw, where)
        _only(row, {"label", "path"}, where)
        label = _label(row.get("label"), f"{where}.label", default="")
        path = _path(row.get("path"), f"{where}.path", base)
        if path is None:
            raise _fail(f"{where}.path", "must not be empty")
        if label in profile_names:
            raise _fail(f"{where}.label", "must be unique")
        profile_names.add(label)
        profiles.append((label, path))

    intervals = _table(data.get("intervals", {}), "intervals")
    interval_names = {"system_interval_s", "agent_interval_s", "cron_interval_s", "slow_interval_s"}
    _only(intervals, interval_names, "intervals")
    interval_values = {
        name: _interval(intervals.get(name, default), f"intervals.{name}", default)
        for name, default in (
            ("system_interval_s", 2.0),
            ("agent_interval_s", 15.0),
            ("cron_interval_s", 5.0),
            ("slow_interval_s", 12.0),
        )
    }

    services = _service_rows(data.get("services", []), base)
    return Settings(
        host=host,
        port=port,
        language=language,
        local_host_id=local_id,
        local_host_label=local_label,
        local_host_role=local_role,
        remote_host=remote_host,
        remote_hostname=remote_hostname,
        remote_host_id=remote_id,
        remote_host_label=remote_label,
        remote_host_role=remote_role,
        remote_python=remote_python,
        remote_gpu_state_path=remote_gpu_state_path,
        remote_model_url=remote_model_url,
        gpu_state_path=gpu_state_path,
        network_interfaces=network_interfaces,
        disk_mounts=disk_mounts,
        services=services,
        model_url=model_url,
        agentmon_repo=agentmon_repo,
        sisu_monitor_repo=sisu_monitor_repo,
        hermes_log_dir=hermes_log_dir or home / ".hermes" / "logs",
        openclaw_home=openclaw_home or home / ".openclaw",
        resource_snapshot_path=resource_snapshot_path,
        claude_credentials_path=claude_credentials_path,
        hermes_profiles=tuple(profiles),
        **flags,
        **interval_values,
    )


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Load the selected TOML at the call site; absent default file means safe defaults."""
    selected: Path | None
    explicit = config_path is not None
    if explicit:
        selected = Path(config_path).expanduser()
    else:
        env_path = os.environ.get("SISU_KUDOS_CONFIG")
        selected = (
            Path(env_path).expanduser()
            if env_path
            else Path.home() / ".config" / "sisu-kudos" / "config.toml"
        )
    if selected is None:
        return Settings()
    try:
        raw = selected.read_bytes()
    except FileNotFoundError:
        if explicit or os.environ.get("SISU_KUDOS_CONFIG"):
            raise ConfigurationError(f"Configuration file not found: {selected}") from None
        return Settings()
    except OSError as exc:
        raise ConfigurationError(f"Configuration file cannot be read: {selected}") from exc
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Invalid TOML in {selected}: {exc}") from None
    if not isinstance(parsed, dict):
        raise ConfigurationError("Configuration root must be a TOML table")
    return _parse(parsed, selected.parent.resolve(strict=False))
