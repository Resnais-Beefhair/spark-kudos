"""Strict TOML settings and CLI behavior."""

from pathlib import Path

import pytest

from sisu_kudos.__main__ import main
from sisu_kudos.config import ConfigurationError, Settings, load_settings


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kudos.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_loopback_safe_and_optional_integrations_are_off():
    settings = Settings()
    assert (settings.host, settings.port, settings.language) == ("127.0.0.1", 8787, "en")
    assert settings.local_host_id == "local"
    assert not settings.remote_enabled
    assert settings.sisu_monitor_repo is None
    assert settings.agentmon_repo is None
    assert not any(
        (
            settings.agents_enabled,
            settings.logs_enabled,
            settings.crons_enabled,
            settings.resources_enabled,
            settings.quotas_enabled,
        )
    )
    assert settings.hermes_profile_log_dirs == ()


def test_loads_custom_one_or_two_host_and_optional_integration_values(tmp_path):
    path = write_config(
        tmp_path,
        """
[server]
host = "127.0.0.1"
port = 9001
[site]
language = "fi"
[hosts.local]
id = "gpu-a"
label = "Compute A"
role = "services"
[hosts.remote]
enabled = true
target = "ops@spark-b"
expected_hostname = "spark-b"
id = "gpu-b"
label = "Compute B"
role = "inference"
python = "/usr/bin/python3"
gpu_state_path = "/run/guard/state.json"
model_url = "http://127.0.0.1:9000"
[system]
gpu_state_path = "/run/local-gpu.json"
network_interfaces = ["enp2s0", "eth0"]
disk_mounts = ["/", "/data"]
model_url = "http://127.0.0.1:8000"
[integrations]
logs = true
resources = true
[paths]
resource_snapshot_path = "state/resources.json"
agentmon_repo = "../agent-monitor"
sisu_monitor_repo = "../legacy-monitor"
[intervals]
system_interval_s = 3.5
[[services]]
name = "api"
label = "API"
kind = "http"
url = "https://example.invalid/health"
[[logs.hermes_profiles]]
label = "Worker"
path = "profiles/worker/logs"
""",
    )
    settings = load_settings(path)
    assert settings.language == "fi"
    assert (settings.local_host_id, settings.local_host_label, settings.local_host_role) == (
        "gpu-a",
        "Compute A",
        "services",
    )
    assert settings.remote_enabled
    assert (settings.remote_host, settings.remote_hostname, settings.remote_host_id) == (
        "ops@spark-b",
        "spark-b",
        "gpu-b",
    )
    assert settings.remote_python == "/usr/bin/python3"
    assert settings.remote_gpu_state_path == "/run/guard/state.json"
    assert settings.remote_model_url == "http://127.0.0.1:9000"
    assert settings.gpu_state_path == Path("/run/local-gpu.json")
    assert settings.network_interfaces == ("enp2s0", "eth0")
    assert settings.disk_mounts == ("/", "/data")
    assert settings.model_url == "http://127.0.0.1:8000"
    assert settings.logs_enabled and settings.resources_enabled
    assert settings.resource_snapshot_path == (tmp_path / "state/resources.json")
    assert settings.agentmon_repo == (tmp_path / "../agent-monitor").resolve()
    assert settings.sisu_monitor_repo == (tmp_path / "../legacy-monitor").resolve()
    assert settings.system_interval_s == 3.5
    assert settings.services[0].name == "api"
    assert settings.hermes_profile_log_dirs == (("Worker", tmp_path / "profiles/worker/logs"),)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[server]\nport = 8787\nunknown = 1\n", "unknown field"),
        ('[site]\nlanguage = "sv"\n', "must be 'en' or 'fi'"),
        ("[server]\nport = true\n", "integer"),
        (
            "[hosts.remote]\n"
            'enabled = true\ntarget = "node; touch /tmp/x"\n'
            'expected_hostname = "node"\n',
            "safe SSH destination",
        ),
        (
            "[hosts.remote]\n"
            'enabled = true\ntarget = "node"\n'
            'expected_hostname = "node"\npython = "python3 -c id"\n',
            "one executable",
        ),
        ('[system]\nmodel_url = "http://user:pass@localhost:8000"\n', "without credentials"),
        ('[hosts.remote]\nenabled = true\ntarget = "node"\n', "expected_hostname"),
        ("[unknown]\nsetting = 1\n", "unknown field"),
        ("[integrations]\nlogs = 1\n", "must be a boolean"),
    ],
)
def test_invalid_or_unknown_configuration_fails_clearly(tmp_path, body, message):
    with pytest.raises(ConfigurationError, match=message):
        load_settings(write_config(tmp_path, body))


def test_remote_host_id_must_not_collide_with_local_id(tmp_path):
    path = write_config(
        tmp_path,
        """
[hosts.local]
id = "same"
[hosts.remote]
enabled = true
target = "node"
expected_hostname = "node"
id = "same"
""",
    )
    with pytest.raises(ConfigurationError, match="must differ"):
        load_settings(path)


def test_load_settings_is_runtime_and_default_missing_config_is_safe(tmp_path, monkeypatch):
    path = write_config(tmp_path, '[site]\nlanguage = "fi"\n')
    monkeypatch.setenv("SISU_KUDOS_CONFIG", str(path))
    assert load_settings().language == "fi"
    path.write_text('[site]\nlanguage = "en"\n', encoding="utf-8")
    assert load_settings().language == "en"
    monkeypatch.delenv("SISU_KUDOS_CONFIG")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    assert load_settings().language == "en"


def test_cli_config_check_validates_without_starting_server(tmp_path, capsys, monkeypatch):
    path = write_config(tmp_path, '[site]\nlanguage = "fi"\n')
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: pytest.fail("server started"))
    main(["--config", str(path), "--check-config"])
    assert capsys.readouterr().out.strip() == "Configuration valid."


def test_cli_passes_loaded_server_settings_to_uvicorn(tmp_path, monkeypatch):
    path = write_config(tmp_path, '[server]\nhost = "127.0.0.1"\nport = 9012\n')
    called = {}

    def capture(app, **kwargs):
        called.update(app=app, **kwargs)

    monkeypatch.setattr("uvicorn.run", capture)
    main(["--config", str(path)])
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 9012
    assert called["log_level"] == "info"


def test_cli_version_exits_with_package_version(capsys):
    with pytest.raises(SystemExit) as error:
        main(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == "sisu-kudos 0.5.0"


def test_cli_bad_config_exits_with_parse_error(tmp_path):
    path = write_config(tmp_path, "[unknown]\nfield = 1\n")
    with pytest.raises(SystemExit) as error:
        main(["--config", str(path), "--check-config"])
    assert error.value.code == 2
