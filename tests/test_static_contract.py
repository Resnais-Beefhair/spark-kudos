"""Static safety/accessibility checks; interactions are exercised in browser QA."""

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1] / "src" / "sisu_kudos" / "web"
PROJECT_ROOT = Path(__file__).parents[1]


def test_layout_keeps_work_before_host_drilldowns():
    html = (ROOT / "index.html").read_text()
    assert html.index('class="capacity"') < html.index('class="work"')
    assert html.index('class="work"') < html.index('class="dual-hosts"')
    assert 'id="host-panels"' in html


def test_accessibility_and_safe_rendering():
    html = (ROOT / "index.html").read_text()
    css = (ROOT / "styles.css").read_text()
    js = (ROOT / "app.js").read_text()
    assert "skip-link" in html and 'aria-live="polite"' in html
    assert "prefers-reduced-motion" in css
    assert "innerHTML" not in js and "eval(" not in js
    assert 'for="language-select"' in html
    assert "MutationObserver" not in (ROOT / "i18n.js").read_text()


def test_release_versions_and_javascript_behavior_gates():
    version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert f'__version__ = "{version}"' in (ROOT.parent / "__init__.py").read_text()
    html = (ROOT / "index.html").read_text()
    assert f">v{version}</span>" in html
    assert len(re.findall(rf"[?]v={re.escape(version)}", html)) == 5
    for name in ("app.js", "telemetry.js", "i18n.js", "hosts.js"):
        subprocess.run(["node", "--check", str(ROOT / name)], check=True, capture_output=True)
    for name in ("test_telemetry.cjs", "test_activity.cjs", "test_i18n.cjs", "test_log_ack.cjs"):
        subprocess.run(["node", str(PROJECT_ROOT / "scripts" / name)], check=True)


def test_agent_resources_stay_read_only_and_ownership_is_not_inferred():
    html = (ROOT / "index.html").read_text()
    js = (ROOT / "app.js").read_text()
    assert 'id="agent-resources-title"' in html
    assert "ownership is not independently verified" in html
    assert "/api/agent-resources" in js
    for marker in (
        "task_id",
        "cpu_percent",
        "memory_bytes",
        "cleanup_reason",
        "persistence_reason",
    ):
        assert marker in js
    panel = html[
        html.index('id="agent-resources-section"') : html.index('<section class="dual-hosts"')
    ]
    assert "<button" not in panel


def test_english_default_and_optional_sections():
    html = (ROOT / "index.html").read_text()
    js = (ROOT / "app.js").read_text()
    assert '<html lang="en">' in html and "<title>KUDOS</title>" in html
    for section in ("work-section", "agent-resources-section", "quota-control"):
        assert re.search(rf'id="{section}"[^>]*\bhidden\b', html)
    assert "not wall power" in html
    assert "KudosHosts.hostEntries" in js
    assert "KudosHosts.featureEnabled" in js
    assert "hosts.sisu" not in js and "hosts.kide" not in js
    for marker in (
        "unreadLogErrors",
        "KudosTelemetry.leaders",
        "generation_tokens_per_second",
        "refreshLogs",
        "/api/disks/usage",
        "mergeSnapshot",
        "historyPath",
        "KudosTelemetry.agentActivity",
        "ticket-activity",
    ):
        assert marker in js


def test_work_cards_hide_missing_tasks_instead_of_placeholder_prose():
    js = (ROOT / "app.js").read_text()
    render = js[js.index("function renderWork()") : js.index("function path(")]
    assert 't("work.taskUnavailable")' not in render
    assert "row.task.hidden = !task" in render


def test_disk_space_popup_uses_usage_but_legacy_api_remains_available():
    js = (ROOT / "app.js").read_text()
    server = (ROOT.parent / "app.py").read_text()
    assert "/api/disks/usage" in js
    assert "/api/disks/largest-files" not in js
    assert "/api/disks/largest-files" in server
    assert "renderDiskUsage" in js and 'data.size_basis !== "allocated"' in js
