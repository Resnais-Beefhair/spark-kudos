"""Isolated Chromium QA using SYNTHETIC telemetry, never a live deployment.

Run: uv run --with playwright python scripts/qa_browser.py
Install the test browser first: uv run --with playwright playwright install chromium
All HTTP threads, browser contexts and the private server are owned and closed here.
"""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from qa_header_browser import run_header_checks
from qa_storage_browser import run_storage_checks

from sisu_kudos import __version__
from sisu_kudos.adapters import safe_agent_snapshot
from sisu_kudos.app import CONTENT_SECURITY_POLICY
from sisu_kudos.log_content import operator_message
from sisu_kudos.log_diagnostics import diagnose

WEB = Path(__file__).resolve().parents[1] / "src/sisu_kudos/web"


def fixture(
    language="en",
    dual=False,
    optional=False,
    failed=False,
    preview=False,
    empty_disks=False,
    claude=None,
    claude_state="available",
    chatgpt_sample=False,
    session_count=1,
    model="example-model",
    execution_kind="",
    conversation_only=False,
    session_status="running",
    log_diagnostics=False,
    log_owner_details=False,
    log_peer=None,
    log_peer_instance=0,
    thermal_state=None,
):
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
    if empty_disks:
        system["disks"] = []
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
    if thermal_state:
        for index, host in enumerate(hosts.values()):
            host["system"] = copy.deepcopy(system)
            host["system"]["cpu"]["temperature"] = 42 if index == 0 else 88
            host["system"]["gpu"]["temperature"] = 48 if index == 0 else 82
            host["history"] = [
                {
                    "at": (now - timedelta(seconds=10 * (29 - i))).isoformat(),
                    "cpu_temp": 42 if index == 0 else 60 + i,
                    "gpu_temp": 48 if index == 0 else 55 + i,
                }
                for i in range(30)
            ]
            if thermal_state == "missing":
                host["system"]["cpu"]["temperature"] = None
                host["system"]["gpu"]["temperature"] = None
            if thermal_state == "empty":
                host["history"] = []
            if thermal_state == "single":
                host["history"] = host["history"][-1:]
    sessions = (
        [
            {
                "id": "fixture-session",
                "agent": "Example worker",
                "backend": "hermes",
                "status": session_status,
                "work_kind": "agent",
                "execution_kind": execution_kind,
                "age_seconds": 2,
                "task": "" if conversation_only else "Model <preview> & analysis",
                "title": "Historical conversation topic",
                "model": model,
                "provider": "example-provider",
                "reasoning": "high",
                "service_tier": "fast",
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
    if optional and session_count > 1:
        sessions = [
            dict(
                sessions[0],
                uid=f"fixture-session-{index}",
                agent=f"Example worker {index + 1}",
                task=f"Synthetic analysis task {index + 1} with a deliberately long title",
                location="Synthetic demo / Example project / Example conversation",
                started_at=(now - timedelta(hours=index + 1)).isoformat(),
                model=(
                    model,
                    "example-provider/very-long-model-identifier-with-context-and-quantization[1m]",
                    None,
                    "model <safe> & literal",
                    "short-model",
                )[index % 5],
                status="tool" if index % 2 else "running",
            )
            for index in range(session_count)
        ]
    quotas = safe_agent_snapshot(SimpleNamespace())["quotas"]
    if chatgpt_sample:
        quotas["chatgpt"] = {
            "provider": "ChatGPT",
            "available": True,
            "status": "Example subscription",
            "windows": [
                {
                    "id": "codex",
                    "label": "Codex",
                    "remaining_percent": 61,
                    "used_percent": 39,
                    "duration_minutes": 10080,
                    "resets_at": int((now + timedelta(days=3)).timestamp()),
                }
            ],
            "collected_at": stamp,
            "resets_available": 0,
        }
    quotas["claude"] = {
        "provider": "Claude",
        "available": claude_state != "login_required",
        "status": "Max 5×",
        "state": claude_state,
        "stale": claude_state == "rate_limited",
        "collected_at": stamp,
        "windows": []
        if claude_state == "login_required"
        else [
            {
                "id": "five_hour",
                "label": "5-hour window",
                "remaining_percent": 96,
                "used_percent": 4,
                "duration_minutes": 300,
                "resets_at": (now + timedelta(hours=3)).isoformat(),
            },
            {
                "id": "seven_day",
                "label": "7-day window",
                "remaining_percent": 100,
                "used_percent": 0,
                "duration_minutes": 10080,
                "resets_at": (now + timedelta(days=3)).isoformat(),
            },
        ],
    }
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
                **{key: optional for key in ("agents", "crons", "logs", "quotas", "resources")},
                "claude_quota": optional if claude is None else claude,
            },
        },
        "hosts": hosts,
        "system": system,
        "history": history,
        "collector_health": {name: healthy for name in ("cpu", "ram", "gpu", "network", "agents")},
        "agents": {
            "sessions": sessions,
            "quotas": quotas,
            "health": [{"source": "hermes", "ok": True}],
            "generated_at": stamp,
        },
        "logs": {
            "revision": f"logs-{log_diagnostics}-{log_peer}-{log_peer_instance}",
            "records": [
                {
                    "id": f"synthetic-log-{i}"
                    + (f"-{log_peer_instance}" if diagnostic["kind"] == "async_resource" else ""),
                    "timestamp": "2026-01-19T15:28:21+00:00",
                    "runtime": "hermes",
                    "agent": "Example worker",
                    "source": "errors",
                    "sources": ["errors"],
                    "level": "ERROR",
                    "category": "operational",
                    "status": "recorded",
                    "component": "asyncio"
                    if diagnostic["kind"] == "async_resource"
                    else "tools.vision_tools"
                    if diagnostic["kind"] == "vision"
                    else "agent.conversation_loop",
                    "message": "Safe structured diagnostic",
                    "diagnostic": diagnostic,
                    **(
                        {
                            "diagnostic": None,
                            "original_message": operator_message(
                                "PreviouslyUnknownError: actual failure reason\n"
                                "/example/owner/config.py\npassword=EXAMPLE_CREDENTIAL\n"
                                "<img src=x onerror=window.ownerLogXss=true>"
                            ),
                        }
                        if log_owner_details and i == 7
                        else {}
                    ),
                }
                for i, diagnostic in enumerate(
                    [
                        {"kind": "vision", "cause": "broken_pipe", "errno": 32},
                        {"kind": "vision", "cause": "cancelled"},
                        {"kind": "vision", "cause": "unknown"},
                        {"kind": "vision", "cause": "service_unavailable", "http_status": 503},
                        {"kind": "async_resource", "cause": "unclosed_client_session"},
                        {"kind": "async_resource", "cause": "unclosed_connector"},
                        {
                            "kind": "api_request",
                            "cause": "service_unavailable",
                            "http_status": 503,
                            "attempt": 3,
                            "max_attempts": 3,
                            "error_type": "InternalServerError",
                        },
                        diagnose(
                            "agent.conversation_loop",
                            "[subagent-0] API call failed after 3 retries. "
                            "HTTP 429: synthetic withheld detail",
                            "ERROR",
                        ),
                    ]
                )
                if not log_peer
                or diagnostic["kind"] != "async_resource"
                or diagnostic["cause"] == log_peer
            ]
            if log_diagnostics
            else [],
            "counts": {"ERROR": 7 if log_peer else 8} if log_diagnostics else {},
            "sources": [{"name": "errors", "available": True}],
        },
        "events": [],
    }


def measure_work_layout(page):
    return page.locator("#work-list").evaluate("""list => {
      const cards = [...list.querySelectorAll('.ticket')];
      const rects = cards.map(card => card.getBoundingClientRect());
      let overlaps = 0;
      for (const card of cards) {
        const boxes = [...card.querySelectorAll(
          '.agent-id strong,.agent-id small,.task,.ticket-origin,' +
          '.ticket-model,.ticket-start,.ticket-activity'
        )].map(n => n.getBoundingClientRect()).filter(b => b.width > 0);
        for (let i = 0; i < boxes.length; i++) {
          for (let j = i + 1; j < boxes.length; j++) {
            const a = boxes[i], b = boxes[j];
            if (a.left < b.right - 1 && b.left < a.right - 1 &&
                a.top < b.bottom - 1 && b.top < a.bottom - 1) overlaps++;
          }
        }
      }
      return {
        count: cards.length,
        overlaps,
        height: Math.max(...rects.map(r => r.height)),
        listWidth: list.getBoundingClientRect().width,
        listClientWidth: list.clientWidth,
        listScrollWidth: list.scrollWidth,
        containerOverflow: list.scrollWidth - list.clientWidth,
        cardWidths: rects.map(r => r.width),
        rows: new Set(rects.map(r => Math.round(r.top))).size,
        hiddenMeta: cards.every(c =>
          getComputedStyle(c.querySelector('.ticket-meta')).display === 'none'),
        activity: cards.every(c => c.querySelector('.ticket-activity svg') &&
          c.querySelector('.ticket-activity').title.length > 0),
        modelWidths: cards.map(c =>
          c.querySelector('.ticket-model').getBoundingClientRect().width),
        glyphWidths: cards.map(c => c.querySelector('.ticket-activity svg')
          .getBoundingClientRect().width),
        overflow: document.documentElement.scrollWidth - innerWidth
      };
    }""")


def run(output, *, header_only=False):
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
                data, kind = json.dumps(fixture(**options)["logs"]).encode(), "application/json"
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
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.end_headers()
            # The owned test browser can close an in-flight fixture request.
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, failures, checks, capacity_layout, work_layout = [], [], [], [], []
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
                    "console", lambda msg: errors.append(msg.text) if msg.type == "error" else None
                )
                page.on(
                    "requestfailed",
                    lambda req: failures.append({"url": req.url, "error": req.failure}),
                )
                url = f"http://127.0.0.1:{server.server_port}"
                page.goto(url)
                if header_only:
                    thermal_layout = run_header_checks(page, options, output)
                    assert not errors, errors
                    report = {
                        "version": __version__,
                        "fixtures": "synthetic only",
                        "thermal_layout": thermal_layout,
                        "page_errors": errors,
                    }
                    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
                    print(json.dumps(report, indent=2))
                    return
                expect(page.locator(".host-panel")).to_have_count(1)
                expect(page.locator("html")).to_have_attribute("lang", "en")
                expect(page.locator("#language-select")).to_have_value("en")
                expect(page.locator(".host-panel h2")).to_have_text("Lab primary <GPU>")
                for name in (
                    "work-section",
                    "agent-resources-section",
                    "quota-control",
                    "claude-quota-control",
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
                options.update(claude=True)
                expect(page.locator("#claude-quota-control")).to_be_visible()
                expect(page.locator("#quota-control")).to_be_hidden()
                expect(page.locator("#work-section")).to_be_hidden()
                expect(page.locator("#claude-value")).to_contain_text("96")
                expect(page.locator("#claude-quota-control")).to_contain_text("100")
                page.locator("#claude-quota-control").click()
                expect(page.locator("#inspector-title")).to_contain_text("Claude")
                expect(page.locator("#inspector-content")).to_contain_text("96")
                expect(page.locator("#inspector-content")).not_to_contain_text("1970")
                page.locator("#inspector [data-close]").click()
                expect(page.locator("#claude-quota-control")).to_be_focused()
                checks.append("Claude-only opt-in, five-hour/weekly values, ISO reset and focus")
                options.update(language="en", dual=True, optional=True, claude=None)
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
                storage_layout = run_storage_checks(page, checks, output, errors)
                page.locator("#work-list .ticket").first.click()
                expect(page.locator("#inspector-content")).to_contain_text("example-model")
                expect(page.locator("#inspector-content")).to_contain_text("Päättely")
                expect(page.locator("#inspector-content")).to_contain_text("high")
                expect(page.locator("#inspector-content")).to_contain_text("Fast-tila")
                expect(page.locator("#inspector-content")).to_contain_text("päällä")
                page.locator("#inspector [data-close]").click()
                expect(page.locator("#work-list .ticket").first).to_be_focused()
                checks.append("compact work details retain model, reasoning and Fast mode")
                ticket = page.locator("#work-list .ticket").first
                expect(ticket.locator(".ticket-model")).to_have_text("example-model")
                ticket.evaluate(
                    "n => { window.qaTicket = n; window.qaGlyph = n.querySelector('svg'); }"
                )
                options.update(model="model <safe> & literal")
                expect(ticket.locator(".ticket-model")).to_have_text("model <safe> & literal")
                assert ticket.locator(".ticket-model").evaluate("n => n.childElementCount") == 0
                options.update(model=None)
                expect(ticket.locator(".ticket-model")).to_be_hidden()
                options.update(model="example-model", execution_kind="script")
                expect(ticket.locator(".ticket-model")).to_be_hidden()
                options.update(execution_kind="")
                options.update(model="example-model")
                expect(ticket.locator(".ticket-model")).to_have_attribute("title", "example-model")
                assert ticket.evaluate(
                    "n => n === window.qaTicket && n.querySelector('svg') === window.qaGlyph"
                )
                page.emulate_media(reduced_motion="no-preference")
                ticket.scroll_into_view_if_needed()
                animated = ticket.locator(".activity-work-dot")
                transform = animated.evaluate("n => getComputedStyle(n).transform")
                page.wait_for_timeout(350)
                assert animated.evaluate("n => getComputedStyle(n).transform") != transform
                page.locator("#activity-motion").click()
                expect(animated).to_have_css("animation-play-state", "paused")
                page.reload()
                expect(page.locator("html")).to_have_attribute("data-activity-motion", "off")
                page.locator("#activity-motion").click()
                page.emulate_media(reduced_motion="reduce")
                expect(page.locator(".activity-work-dot").first).to_have_css(
                    "animation-name", "none"
                )
                checks.append(
                    "inline model updates/absence/escaping, stable SVG, "
                    "enlarged motion/pause/reload/reduced motion"
                )
                page.locator("#language-select").select_option("en")
                ticket.evaluate(
                    "n => { window.qaTaskTicket = n; window.qaTaskGlyph = n.querySelector('svg'); }"
                )
                options.update(conversation_only=True)
                for language, unavailable, context_label in (
                    ("en", "Task name not available", "Conversation title"),
                    ("fi", "Tehtävän nimeä ei saatavilla", "Keskustelun otsikko"),
                ):
                    page.locator("#language-select").select_option(language)
                    expect(ticket.locator(".task")).to_be_hidden()
                    expect(ticket.locator(".task")).to_have_text("")
                    expect(ticket).not_to_contain_text(unavailable)
                    expect(ticket).not_to_contain_text("Historical conversation topic")
                    for width in (1440, 390):
                        page.set_viewport_size({"width": width, "height": 1080})
                        expect(ticket.locator(".ticket-origin")).to_be_visible()
                        assert ticket.evaluate("n => n.scrollWidth <= n.clientWidth + 1")
                        assert ticket.locator(".ticket-origin").evaluate(
                            "n => n.getBoundingClientRect().width > "
                            "n.parentElement.getBoundingClientRect().width * 0.95"
                        )
                        assert ticket.bounding_box()["height"] <= 61
                    expect(ticket.locator(".task")).to_be_hidden()
                    ticket.click()
                    expect(page.locator("#inspector-content")).to_contain_text(context_label)
                    expect(page.locator("#inspector-content")).to_contain_text(
                        "Historical conversation topic"
                    )
                    page.locator("#inspector [data-close]").click()
                options.update(conversation_only=False)
                expect(ticket.locator(".task")).to_be_visible()
                expect(ticket.locator(".task")).to_have_text("Model <preview> & analysis")
                assert ticket.evaluate(
                    "n => n === window.qaTaskTicket && "
                    "n.querySelector('svg') === window.qaTaskGlyph"
                )
                checks.append(
                    "missing task omitted; full-width origin; "
                    "live restoration, EN/FI desktop/mobile"
                )
                options.update(session_status="idle")
                expect(page.locator("#work-list .ticket")).to_have_count(0)
                options.update(session_status="running", conversation_only=False)
                expect(page.locator("#work-list .ticket")).to_have_count(1)
                expect(ticket.locator(".task")).to_have_text("Model <preview> & analysis")
                checks.append("closed turn removed; historical Hermes title is context, EN/FI")
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
                # Exercise adapter defaults in both quota surfaces, not a hand-made status.
                for language, unavailable in (("en", "not available"), ("fi", "ei saatavilla")):
                    page.locator("#language-select").select_option(language)
                    expect(page.locator("#chatgpt-note")).to_have_text(unavailable)
                    page.locator("#quota-control").click()
                    expect(page.locator("#inspector-content")).to_contain_text(unavailable)
                    page.locator("#inspector [data-close]").click()
                checks.append("missing quota: localized card and inspector in English and Finnish")
                options.update(log_diagnostics=True)
                expect(page.locator("#log-badge")).to_have_text("8")
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1080})
                    for language in ("en", "fi"):
                        page.locator("#language-select").select_option(language)
                        page.locator("#logs-button").click()
                        expect(page.locator("#logs-dialog")).to_be_visible()
                        page.get_by_role(
                            "button", name="Errors" if language == "en" else "Virheet", exact=True
                        ).click()
                        expect(page.locator("#log-list li")).to_have_count(8)
                        expect(page.locator("#log-list")).to_contain_text("HTTP 503")
                        expect(page.locator("#log-list")).to_contain_text("HTTP 429")
                        expect(page.locator("#log-list")).to_contain_text("errno 32")
                        expect(page.locator("#log-list")).to_contain_text(
                            "Kuvan analysointi epäonnistui: analyysipyyntö peruttiin"
                            if language == "fi"
                            else "Image analysis failed: request cancelled"
                        )
                        expect(page.locator("#log-list")).to_contain_text(
                            "Kuvan analysointi epäonnistui: virhesyytä ei tunnistettu"
                            if language == "fi"
                            else "Image analysis failed: cause not classified"
                        )
                        expect(page.locator("#log-list")).to_contain_text(
                            "HTTP-asiakasresurssi jäi sulkematta: asiakasistunto"
                            if language == "fi"
                            else "HTTP client cleanup incomplete: client session left unclosed"
                        )
                        expect(page.locator("#log-list")).to_contain_text(
                            "HTTP-asiakasresurssi jäi sulkematta: yhteyspooli"
                            if language == "fi"
                            else "HTTP client cleanup incomplete: connection pool left unclosed"
                        )
                        expect(page.locator("#log-list")).to_contain_text(
                            "yritys 3/3" if language == "fi" else "attempt 3/3"
                        )
                        expect(page.locator("#log-list")).to_contain_text(
                            "Kuvan analysointi" if language == "fi" else "Image analysis"
                        )
                        expect(page.locator("#log-list")).not_to_contain_text("[REDACTED]")
                        expect(page.locator("#log-list")).not_to_contain_text(
                            "Safe structured diagnostic"
                        )
                        assert page.locator("#logs-dialog").evaluate(
                            "n => n.scrollWidth <= n.clientWidth + 1"
                        )
                        page.screenshot(
                            path=str(output / f"logs-{width}-{language}.png"), full_page=True
                        )
                        page.locator("#logs-dialog [data-close]").click()
                checks.append(
                    "typed log causes, HTTP/retries/errno, Errors filter, EN/FI, desktop/mobile"
                )
                options.update(log_owner_details=True)
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1080})
                    for language in ("en", "fi"):
                        page.locator("#language-select").select_option(language)
                        page.locator("#logs-button").click()
                        owner = page.locator('#log-list li[data-incident-id="synthetic-log-7"]')
                        expect(owner).to_contain_text(
                            "PreviouslyUnknownError: actual failure reason"
                        )
                        expect(owner).to_contain_text("/example/owner/config.py")
                        expect(owner).not_to_contain_text("EXAMPLE_CREDENTIAL")
                        expect(owner.locator("img")).to_have_count(0)
                        assert not page.evaluate("Boolean(window.ownerLogXss)")
                        # Log rows can refresh between animation frames; scroll the
                        # current node atomically rather than pinning an element handle.
                        owner.evaluate("n => n.scrollIntoView({block: 'nearest'})")
                        assert page.locator("#logs-dialog").evaluate(
                            "n => n.scrollWidth <= n.clientWidth + 1"
                        )
                        page.locator("#logs-dialog").screenshot(
                            path=str(output / f"owner-logs-{width}-{language}.png")
                        )
                        page.locator("#logs-dialog [data-close]").click()
                checks.append(
                    "owner unknown error/traceback, credentials masked, HTML literal, "
                    "EN/FI, desktop/mobile"
                )
                options.update(log_owner_details=False, log_diagnostics=False)
                page.evaluate("localStorage.removeItem('sisu-kudos.log-error-ack.v2')")
                options.update(log_diagnostics=True, log_peer="unclosed_client_session")
                page.locator("#logs-button").click()
                expect(page.locator("#log-list li")).to_have_count(7)
                page.locator("#acknowledge-errors").click()
                expect(page.locator("#acknowledge-errors")).to_be_disabled()
                options.update(log_peer="unclosed_connector")
                expect(page.locator("#log-list")).to_contain_text("yhteyspooli")
                expect(page.locator("#acknowledge-errors")).to_be_enabled()
                page.locator("#acknowledge-errors").click()
                options.update(log_peer="unclosed_client_session")
                expect(page.locator("#log-list")).to_contain_text("asiakasistunto")
                expect(page.locator("#log-list")).not_to_contain_text("yhteyspooli")
                expect(page.locator("#acknowledge-errors")).to_be_disabled()
                page.reload()
                page.locator("#logs-button").click()
                expect(page.locator("#log-list")).to_contain_text("asiakasistunto")
                expect(page.locator("#acknowledge-errors")).to_be_disabled()
                options.update(log_peer_instance=1)
                expect(page.locator("#acknowledge-errors")).to_be_enabled()
                page.locator("#logs-dialog [data-close]").click()
                options.update(log_diagnostics=False, log_peer=None, log_peer_instance=0)
                checks.append(
                    "ACK survives rotating same-time peers/reload; genuinely new errors still alert"
                )
                page.set_viewport_size({"width": 1920, "height": 1080})
                page.locator("#language-select").select_option("en")
                options.update(empty_disks=True)
                for language, unavailable in (
                    ("en", "Disk information not available"),
                    ("fi", "Levytietoa ei saatavilla"),
                ):
                    page.locator("#language-select").select_option(language)
                    expect(page.locator(".host-disks-empty")).to_have_count(2)
                    for empty in page.locator(".host-disks-empty").all():
                        expect(empty).to_have_text(unavailable)
                    expect(page.locator(".host-disk")).to_have_count(0)
                options.update(empty_disks=False)
                expect(page.locator(".host-disks-empty")).to_have_count(0)
                expect(page.locator(".host-disk")).to_have_count(2)
                page.locator("#language-select").select_option("en")
                checks.append("disk rows transition through localized empty states and recover")
                assert "[object Object]" not in page.locator("body").inner_text()
                assert (
                    page.locator(".skip-link").evaluate("n => getComputedStyle(n).clipPath")
                    == "inset(50%)"
                )
                for language in ("en", "fi"):
                    page.locator("#language-select").select_option(language)
                    options.update(claude_state="rate_limited")
                    expect(page.locator("#claude-value")).to_contain_text("96")
                    page.locator("#claude-quota-control").click()
                    expect(page.locator("#inspector-content")).not_to_contain_text("1970")
                    expect(page.locator("#inspector-content")).not_to_contain_text("ChatGPT")
                    page.locator("#inspector [data-close]").click()
                    options.update(claude_state="login_required")
                    expect(page.locator("#claude-value")).not_to_contain_text("96")
                    expect(page.locator("#claude-value")).not_to_contain_text("100")
                    options.update(claude_state="available")
                    expect(page.locator("#claude-value")).to_contain_text("96")
                checks.append(
                    "Claude rate-limit stale retention, login-required and recovery in EN/FI"
                )
                for session_count in (1, 2):
                    options.update(session_count=session_count)
                    expect(page.locator("#work-list .ticket")).to_have_count(session_count)
                    for language in ("en", "fi"):
                        page.locator("#language-select").select_option(language)
                        for width in (1920, 1024, 390):
                            page.set_viewport_size({"width": width, "height": 1080})
                            layout = measure_work_layout(page)
                            assert layout["count"] == session_count, (language, width, layout)
                            assert layout["overlaps"] == 0, (language, width, layout)
                            assert layout["overflow"] <= 1, (language, width, layout)
                            assert layout["containerOverflow"] <= 1, (language, width, layout)
                            expected_height = (76, 80) if width < 760 else (58, 62)
                            assert expected_height[0] <= layout["height"] <= expected_height[1], (
                                language,
                                width,
                                layout,
                            )
                            if width < 760:
                                assert all(
                                    abs(card_width - layout["listWidth"]) <= 1
                                    for card_width in layout["cardWidths"]
                                ), (language, width, layout)
                            else:
                                assert all(
                                    300 <= card_width <= 620 for card_width in layout["cardWidths"]
                                ), (language, width, layout)
                                if session_count == 1 and layout["listWidth"] >= 600:
                                    assert all(
                                        560 <= card_width <= 620
                                        for card_width in layout["cardWidths"]
                                    ), (language, width, layout)
                            if width == 1920:
                                page.screenshot(
                                    path=str(
                                        output / f"work-cards-{session_count}-{language}-1920.png"
                                    ),
                                    full_page=True,
                                )
                            work_layout.append(
                                {
                                    "session_count": session_count,
                                    "language": language,
                                    "viewport": width,
                                    **layout,
                                }
                            )
                checks.append(
                    "one/two work cards: bounded desktop widths, full mobile width, "
                    "60/78px heights and no overlap/overflow in EN/FI"
                )
                options.update(session_count=5)
                expect(page.locator("#work-list .ticket")).to_have_count(5)
                for language in ("en", "fi"):
                    page.locator("#language-select").select_option(language)
                    for width in (1920, 1440, 1366, 1024, 768, 390):
                        page.set_viewport_size({"width": width, "height": 1080})
                        layout = measure_work_layout(page)
                        assert layout["count"] == 5, layout
                        assert layout["overlaps"] == 0, (language, width, layout)
                        assert layout["overflow"] <= 1, (language, width, layout)
                        assert layout["containerOverflow"] <= 1, (language, width, layout)
                        assert 44 <= layout["height"] <= (84 if width < 760 else 62), layout
                        assert layout["hiddenMeta"] and layout["activity"], layout
                        assert all(
                            (300 if width >= 760 else 0) <= w <= 620 for w in layout["cardWidths"]
                        ), (
                            language,
                            width,
                            layout,
                        )
                        if width < 760:
                            assert all(
                                abs(w - layout["listWidth"]) <= 1 for w in layout["cardWidths"]
                            ), (language, width, layout)
                        if width == 1920:
                            assert layout["rows"] == 1, (language, width, layout)
                        else:
                            assert layout["rows"] >= 2, (language, width, layout)
                        assert all(
                            w >= 24 for i, w in enumerate(layout["modelWidths"]) if i != 2
                        ), layout
                        assert all(w == 56 for w in layout["glyphWidths"]), layout
                        work_layout.append({"language": language, "viewport": width, **layout})
                    page.locator("#work-list .ticket").first.click()
                    for text in ("example-model", "high", "read_file"):
                        expect(page.locator("#inspector-content")).to_contain_text(text)
                    expect(page.locator("#inspector-content")).to_contain_text(
                        "Fast mode" if language == "en" else "Fast-tila"
                    )
                    page.locator("#inspector [data-close]").click()
                    expect(page.locator("#work-list .ticket").first).to_be_focused()
                checks.append(
                    "five compact work cards: EN/FI at1920/1440/1366/1024/768/390px, "
                    "bounded heights, no collisions, activity and details retained"
                )
                page.set_viewport_size({"width": 1920, "height": 1080})
                page.locator("#language-select").select_option("en")
                options.update(preview=True, chatgpt_sample=True)
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
                    for selector in ("#quota-control", "#claude-quota-control"):
                        metrics = page.locator(selector).evaluate(
                            "n => ({width:n.clientWidth, content:n.scrollWidth})"
                        )
                        assert metrics["content"] <= metrics["width"] + 1, (
                            width,
                            selector,
                            metrics,
                        )
                    # Density is a measurable contract, not just zero horizontal overflow.
                    strip_height = page.locator(".capacity").bounding_box()["height"]
                    bars = [bar.bounding_box() for bar in page.locator(".quota-lives .life").all()]
                    assert len(bars) == 2 and abs(bars[0]["y"] - bars[1]["y"]) < 1
                    assert bars[0]["x"] + bars[0]["width"] <= bars[1]["x"]
                    for card in page.locator(".capacity > button").all():
                        if card.is_visible():
                            assert card.bounding_box()["height"] >= 44
                    capacity_layout.append({"viewport": width, "height_px": strip_height})
                    if width >= 1366:
                        # Longer configured host names may legitimately wrap the GPU row.
                        assert strip_height <= 100, (width, strip_height)
                        boxes = [
                            page.locator(selector).bounding_box()
                            for selector in (
                                "#quota-control",
                                "#claude-quota-control",
                                ".dual-power",
                            )
                        ]
                        assert boxes[0]["x"] < boxes[1]["x"] < boxes[2]["x"]
                        assert abs(boxes[0]["y"] - boxes[2]["y"]) < 1
                    page.screenshot(path=str(output / f"viewport-{width}.png"), full_page=True)
                checks.append(
                    "compact strip <=100px desktop, side-by-side Claude tracks, "
                    ">=44px targets and no overflow at1366/768/390px"
                )
                options.update(failed=True)
                expect(page.locator('[data-host="compute-node"] .host-state')).to_contain_text(
                    "Yhteys katkesi"
                )
                expect(page.locator('[data-host="lab-primary"] .host-state')).not_to_contain_text(
                    "Yhteys katkesi"
                )
                checks.append("remote failure leaves local host live")
                # A fresh browser uses the configured site language, not the earlier preference.
                options.update(language="fi", optional=False, failed=False, claude=None)
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
                thermal_layout = run_header_checks(page, options, output)
                checks.append(
                    "masthead thermals: real-value contract, colors, stale/missing, "
                    "history gaps, keyboard drilldown and responsive layout"
                )
                assert not errors, errors
                assert "/api/events" in requests
                report = {
                    "version": __version__,
                    "fixtures": "synthetic only",
                    "checks": checks,
                    "capacity_layout": capacity_layout,
                    "work_layout": work_layout,
                    "storage_layout": storage_layout,
                    "thermal_layout": thermal_layout,
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
    parser.add_argument("--header-only", action="store_true")
    args = parser.parse_args()
    run(args.output, header_only=args.header_only)
