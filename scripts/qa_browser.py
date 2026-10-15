"""Isolated Chromium QA using SYNTHETIC telemetry, never a live deployment.

Run: uv run --with playwright python scripts/qa_browser.py
Install the test browser first: uv run --with playwright playwright install chromium
All HTTP threads, browser contexts and the private server are owned and closed here.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from sisu_kudos import __version__

WEB = Path(__file__).resolve().parents[1] / "src/sisu_kudos/web"


def fixture(language="en", dual=False, optional=False, failed=False, preview=False):
    """Deliberate test fixtures; none of these numbers are real measurements."""
    now = datetime.now(UTC)
    stamp = now.isoformat()
    system = {
        "cpu": {"usage_percent": 24.5, "temperature": 48, "load_1": 2.1},
        "ram": {"total_gb": 128, "used_gb": 72, "available_gb": 56, "used_percent": 56.25},
        "gpu": {
            "health": "ok",
            "health_status": "ok",
            "utilization_percent": 43.5,
            "temperature": 58,
            "power_watts": 66.5,
            "is_unified_memory": True,
            "gpu_processes": [{"pid": 1234, "name": "python", "memory_mb": 8192}],
        },
        "network": {"rx_bytes_sec": 1048576, "tx_bytes_sec": 524288},
        "disks": [
            {
                "label": "Example NVMe",
                "mount": "/",
                "total_gb": 1000,
                "used_gb": 420,
                "free_gb": 580,
                "use_percent": 42,
            }
        ],
        "services": [
            {
                "name": "model",
                "label": "Example model service",
                "status": "running",
                "memory_mb": 70,
                "activity": "",
                "collector": {"ok": True},
            }
        ],
        "processes": {
            "cpu": [{"pid": 1234, "name": "python", "cpu_percent": 24.5}],
            "memory": [{"pid": 1234, "name": "python", "memory_mb": 8192}],
            "io": [],
        },
    }
    history = [
        {
            "at": (now - timedelta(seconds=2 * i)).isoformat(),
            "cpu": 24.5,
            "gpu": 43.5,
            "ram": 56.25,
            "temp": 58,
            "net": 1048576,
        }
        for i in reversed(range(30))
    ]
    healthy = {"ok": True, "stale": False, "age_seconds": 0.2, "error": ""}
    hosts = {
        "lab-primary": {
            "id": "lab-primary",
            "label": "Spark One" if preview else "Lab primary <GPU>",
            "role": "agents-services",
            "is_remote": False,
            "health": healthy,
            "system": system,
            "history": history,
            "generated_at": stamp,
        }
    }
    if dual:
        hosts["compute-node"] = {
            "id": "compute-node",
            "label": "Compute node",
            "role": "model-server",
            "is_remote": True,
            "system": system,
            "generated_at": stamp,
            "history": history,
            "health": {
                **healthy,
                "ok": not failed,
                "stale": failed,
                "error": "Remote SSH unavailable" if failed else "",
            },
        }
    sessions = (
        [
            {
                "id": "fixture-session",
                "agent": "Example worker",
                "backend": "hermes",
                "status": "running",
                "work_kind": "agent",
                "age_seconds": 2,
                "task": "Model <preview> & analysis",
                "model": "example-model",
                "runtime": "hermes",
                "uid": "fixture-session",
                "updated_at": stamp,
                "started_at": stamp,
                "location": "Synthetic demo",
                "origin_label": "Synthetic demo",
                "current_tool": "read_file",
            }
        ]
        if optional
        else []
    )
    return {
        "schema_version": 3,
        "revision": 1,
        "sequence": 1,
        "generated_at": stamp,
        "app": {"name": "KUDOS", "version": __version__},
        "ui": {
            "language": language,
            "local_host_id": "lab-primary",
            "features": {
                key: optional for key in ("agents", "crons", "logs", "quotas", "resources")
            },
        },
        "hosts": hosts,
        "system": system,
        "history": history,
        "collector_health": {name: healthy for name in ("cpu", "ram", "gpu", "network", "agents")},
        "agents": {
            "sessions": sessions,
            "quotas": {},
            "health": [{"source": "hermes", "ok": True}],
            "generated_at": stamp,
        },
        "logs": {"records": [], "counts": {}},
        "events": [],
    }


def run(output):
    options = {"language": "en", "dual": False, "optional": False, "failed": False}
    stop = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            requests.append(path)
            if path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    while not stop.is_set():
                        body = json.dumps(fixture(**options))
                        self.wfile.write(f"event: snapshot\ndata: {body}\n\n".encode())
                        self.wfile.flush()
                        stop.wait(0.2)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if path == "/api/snapshot":
                data, kind = json.dumps(fixture(**options)).encode(), "application/json"
            elif path == "/api/logs":
                data, kind = b'{"records":[],"counts":{},"sources":[]}', "application/json"
            elif path == "/api/agent-resources":
                data, kind = (
                    b'{"available":true,"resources":[],"counts":{},"warnings":[]}',
                    "application/json",
                )
            elif path in (
                "/",
                "/assets/styles.css",
                "/assets/app.js",
                "/assets/telemetry.js",
                "/assets/i18n.js",
                "/assets/hosts.js",
            ):
                file = WEB / ("index.html" if path == "/" else path.rsplit("/", 1)[-1])
                data, kind = file.read_bytes(), mimetypes.guess_type(file)[0] or "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, failures, checks = [], [], []
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080}, reduced_motion="reduce"
                )
                page = context.new_page()
                page.on(
                    "pageerror",
                    lambda error: (errors.append(str(error)), print("PAGE ERROR:", str(error))),
                )
                page.on(
                    "requestfailed",
                    lambda req: failures.append({"url": req.url, "error": req.failure}),
                )
                url = f"http://127.0.0.1:{server.server_port}"
                page.goto(url)
                expect(page.locator(".host-panel")).to_have_count(1)
                expect(page.locator("html")).to_have_attribute("lang", "en")
                expect(page.locator("#language-select")).to_have_value("en")
                expect(page.locator(".host-panel h2")).to_have_text("Lab primary <GPU>")
                for name in (
                    "work-section",
                    "agent-resources-section",
                    "quota-control",
                    "logs-button",
                ):
                    expect(page.locator(f"#{name}")).to_be_hidden()
                expect(page.locator("#active-count")).to_have_text("–")
                checks.append("one host; English default; optional controls hidden; labels escaped")
                page.locator("#language-select").select_option("fi")
                expect(page.locator("html")).to_have_attribute("lang", "fi")
                expect(page.locator("#power-note")).to_contain_text("GPU-teho")
                page.reload()
                expect(page.locator("html")).to_have_attribute("lang", "fi")
                checks.append("Finnish switch and explicit preference survive reload")
                options.update(language="en", dual=True, optional=True)
                page.reload()
                expect(page.locator(".host-panel")).to_have_count(2)
                expect(page.locator("#work-section")).to_be_visible()
                expect(page.locator("#logs-button")).to_be_visible()
                expect(page.locator("html")).to_have_attribute("lang", "fi")
                expect(page.locator("#work-list")).to_contain_text("Model <preview> & analysis")
                trigger = page.locator('[data-host-inspect="compute-node"][data-host-metric="ram"]')
                trigger.click()
                expect(page.locator("#inspector")).to_be_visible()
                expect(page.locator("#inspector-kicker")).to_contain_text("COMPUTE NODE")
                # A live update must not close a focused details dialog.
                page.wait_for_timeout(500)
                expect(page.locator("#inspector")).to_be_visible()
                page.locator("#inspector [data-close]").click()
                expect(trigger).to_be_focused()
                checks.append("arbitrary dual-host IDs; remote details; dialog/focus survive SSE")
                page.locator("#language-select").select_option("en")
                page.locator("#services-button").click()
                expect(page.locator("#inspector-title")).to_have_text("Continuous service status")
                expect(page.locator("#inspector-content")).to_contain_text("Example model service")
                expect(page.locator("#inspector-content")).not_to_contain_text("YDINTOIMINNOT")
                page.locator("#inspector [data-close]").click()
                page.locator("#logs-button").click()
                expect(page.locator("#logs-dialog")).to_be_visible()
                page.locator("#logs-dialog [data-close]").click()
                checks.append("English service and log dialogs")
                assert "[object Object]" not in page.locator("body").inner_text()
                assert (
                    page.locator(".skip-link").evaluate("n => getComputedStyle(n).clipPath")
                    == "inset(50%)"
                )
                options.update(preview=True)
                expect(page.locator(".host-panel h2").first).to_have_text("Spark One")
                page.screenshot(path=str(output / "desktop-en.png"), full_page=True)
                page.locator("#language-select").select_option("fi")
                page.screenshot(path=str(output / "desktop-fi.png"), full_page=True)
                for width, height in ((1366, 768), (768, 1024), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    overflow = page.evaluate(
                        "document.documentElement.scrollWidth-document.documentElement.clientWidth"
                    )
                    assert overflow <= 1, (width, "horizontal overflow", overflow)
                    page.screenshot(path=str(output / f"viewport-{width}.png"), full_page=True)
                checks.append("no horizontal overflow at 1366, 768 and 390 px")
                options.update(failed=True)
                expect(page.locator('[data-host="compute-node"] .host-state')).to_contain_text(
                    "Yhteys katkesi"
                )
                expect(page.locator('[data-host="lab-primary"] .host-state')).not_to_contain_text(
                    "Yhteys katkesi"
                )
                checks.append("remote failure leaves local host live")
                # A fresh browser uses the configured site language, not the earlier preference.
                options.update(language="fi", optional=False, failed=False)
                fresh = browser.new_context()
                newpage = fresh.new_page()
                newpage.on(
                    "pageerror",
                    lambda error: (errors.append(str(error)), print("PAGE ERROR:", str(error))),
                )
                newpage.goto(url)
                expect(newpage.locator("html")).to_have_attribute("lang", "fi")
                fresh.close()
                checks.append("configured Finnish default in a fresh browser")
                assert not errors, errors
                assert "/api/events" in requests
                report = {
                    "version": __version__,
                    "fixtures": "synthetic only",
                    "checks": checks,
                    "page_errors": errors,
                    "request_failures": failures,
                }
                (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report, indent=2))
                context.close()
            finally:
                browser.close()
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "QA HTTP thread did not close"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/browser"))
    run(parser.parse_args().output)
