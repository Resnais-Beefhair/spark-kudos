"""Synthetic storage-popup browser checks shared by the main QA runner."""

from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect


def run_storage_checks(page, checks, output, console_errors):
    previous_viewport = page.viewport_size
    previous_language = page.locator("html").get_attribute("lang")
    base = {
        "entries": [
            {"path": "snapshot.bin", "kind": "file", "size_bytes": 4 * 1024**3},
            {"path": "models", "kind": "directory", "size_bytes": 130 * 1024**3},
            {"path": "projects", "kind": "directory", "size_bytes": 47 * 1024**3},
        ],
        "complete": True,
        "scanned_entries": 3,
        "duration_ms": 12,
        "sampled_at": "2026-01-19T15:28:21+00:00",
        "scope": "mount_children",
        "size_basis": "allocated",
        "cached": False,
        "cache_age_seconds": 0,
    }
    mode = {"payload": copy.deepcopy(base), "status": 200, "hold_local": False}
    requests, pending, measurements = [], [], []
    expected_errors = []

    def intercept(route):
        params = parse_qs(urlsplit(route.request.url).query)
        requests.append(params)
        if mode["hold_local"] and params.get("host") == ["lab-primary"]:
            pending.append(route)
            return
        route.fulfill(
            status=mode["status"], content_type="application/json", body=json.dumps(mode["payload"])
        )

    pattern = "**/api/disks/usage?*"
    page.route(pattern, intercept)
    try:
        for host in ("lab-primary", "compute-node"):
            for language in ("en", "fi"):
                page.locator("#language-select").select_option(language)
                for width in (1920, 390):
                    page.set_viewport_size({"width": width, "height": 1080})
                    trigger = page.locator(
                        f'[data-host-inspect="{host}"][data-host-metric="disk"]'
                    ).first
                    before = len(requests)
                    trigger.click()
                    names = page.locator(".disk-usage-name")
                    expect(names).to_have_text(["models/", "projects/", "snapshot.bin"])
                    expect(page.locator(".disk-usage-kind")).to_have_text(
                        ["Hakemisto", "Hakemisto", "Tiedosto"]
                        if language == "fi"
                        else ["Directory", "Directory", "File"]
                    )
                    expect(page.locator("#inspector .process-list")).to_have_count(0)
                    expect(page.locator("#inspector .process-pid")).to_have_count(0)
                    assert requests[-1] == {"host": [host], "mount": ["/"]}
                    page.wait_for_timeout(450)
                    assert len(requests) == before + 1, "SSE must not repeat the scan"
                    measured = page.locator(".disk-usage-list").evaluate("""list => {
                      const dialog = document.querySelector('#inspector');
                      return {
                        overflow: dialog.scrollWidth - dialog.clientWidth,
                        overlap: [...list.querySelectorAll('li')].some(li =>
                          li.querySelector('.disk-usage-path').getBoundingClientRect().right >
                          li.querySelector('b').getBoundingClientRect().left - 4)
                      };
                    }""")
                    assert measured["overflow"] <= 1 and not measured["overlap"], measured
                    measurements.append(
                        {"host": host, "language": language, "viewport": width, **measured}
                    )
                    page.screenshot(
                        path=str(output / f"storage-{host}-{language}-{width}.png"), full_page=True
                    )
                    page.locator("#inspector [data-close]").click()
                    expect(trigger).to_be_focused()
        checks.append(
            "mount-scoped sorted directory/file sizes, no PIDs, both hosts EN/FI "
            "desktop/mobile; no repeated SSE scans"
        )

        page.locator("#language-select").select_option("fi")
        local = page.locator('[data-host-inspect="lab-primary"][data-host-metric="disk"]').first
        mode["payload"] = copy.deepcopy(base)
        before_refresh = len(requests)
        local.click()
        expect(page.locator(".disk-usage-name").first).to_have_text("models/")
        mode["payload"]["entries"][1]["path"] = "refreshed-models"
        try:
            page.evaluate("""() => {
              window.qaStorageNow = Date.now;
              Date.now = () => window.qaStorageNow() + 601000;
            }""")
            expect(page.locator(".disk-usage-name").first).to_have_text("refreshed-models/")
            page.wait_for_timeout(400)
            assert len(requests) == before_refresh + 2
        finally:
            page.evaluate("Date.now = window.qaStorageNow")
        page.locator("#inspector [data-close]").click()
        checks.append("open storage popup refreshes once at cache expiry, not on every live update")

        mode["payload"] = copy.deepcopy(base) | {"complete": False, "cached": True}
        local.click()
        expect(page.locator(".disk-usage h3")).to_have_text("Suurimmat mitatut kohteet")
        expect(page.locator(".disk-usage [role=status]")).to_contain_text("Osittainen tulos")
        expect(page.locator(".disk-usage [role=status]")).to_contain_text("välimuistista")
        page.locator("#inspector [data-close]").click()
        mode["payload"] = copy.deepcopy(base) | {
            "complete": False,
            "entries": [],
            "scanned_entries": 0,
        }
        local.click()
        expect(page.locator(".disk-usage-list")).to_contain_text("Levy ei välttämättä ole tyhjä")
        page.locator("#inspector [data-close]").click()
        checks.append(
            "partial/cached and unreadable-empty scans are not presented "
            "as complete or empty volumes"
        )

        for status, payload in (
            (503, {"detail": "private error body must not render"}),
            (200, copy.deepcopy(base)),
        ):
            if status == 200:
                payload["entries"][0]["size_bytes"] = "invalid-size"
            errors_before = len(console_errors)
            mode.update(status=status, payload=payload)
            local.click()
            retry = page.locator(".usage-retry")
            expect(retry).to_be_visible()
            expect(page.locator("#inspector-content")).not_to_contain_text("private error body")
            expect(page.locator(".disk-usage-list")).to_have_count(0)
            retry.focus()
            page.wait_for_timeout(450)
            expect(retry).to_be_focused()
            if status == 503:
                observed = console_errors[errors_before:]
                assert observed == [
                    "Failed to load resource: the server responded with a status "
                    "of 503 (Service Unavailable)"
                ], observed
                expected_errors.extend(observed)
                del console_errors[errors_before:]
            mode.update(status=200, payload=copy.deepcopy(base))
            retry.click()
            expect(page.locator(".disk-usage-name")).to_have_count(3)
            page.locator("#inspector [data-close]").click()
        checks.append(
            "HTTP/malformed storage payload fail closed, retry survives SSE focus and recovers"
        )

        hostile = '<img src=x onerror="window.storageXSS=1">' + "long-name-" * 17
        mode["payload"] = copy.deepcopy(base)
        mode["payload"]["entries"][0]["path"] = hostile
        local.click()
        expect(page.locator(".disk-usage-name").last).to_have_text(hostile)
        assert page.locator(".disk-usage img").count() == 0
        assert page.evaluate("window.storageXSS || 0") == 0
        assert page.locator("#inspector").evaluate("n => n.scrollWidth - n.clientWidth") <= 1
        page.screenshot(path=str(output / "storage-long-filename-mobile.png"), full_page=True)
        page.locator("#inspector [data-close]").click()
        checks.append(
            "long HTML-like filenames stay literal and fit mobile without horizontal overflow"
        )

        mode.update(hold_local=True, payload=copy.deepcopy(base))
        local.click()
        expect(page.locator(".disk-usage")).to_have_attribute("aria-busy", "true")
        page.keyboard.press("Escape")
        expect(page.locator("#inspector")).not_to_be_visible()
        mode.update(hold_local=False)
        mode["payload"]["entries"] = [
            {"path": "remote-only", "kind": "directory", "size_bytes": 8192}
        ]
        mode["payload"]["scanned_entries"] = 1
        page.locator('[data-host-inspect="compute-node"][data-host-metric="disk"]').first.click()
        expect(page.locator(".disk-usage-name")).to_have_text(["remote-only/"])
        assert len(pending) == 1
        pending.pop().fulfill(status=200, content_type="application/json", body=json.dumps(base))
        page.wait_for_timeout(350)
        expect(page.locator(".disk-usage-name")).to_have_text(["remote-only/"])
        page.locator("#inspector [data-close]").click()
        checks.append("late disk-scan response cannot overwrite a different volume's popup")
        return {"layouts": measurements, "intentional_http_errors": expected_errors}
    finally:
        for route in pending:
            route.fulfill(status=503, body="{}")
        page.unroute(pattern, intercept)
        page.locator("#language-select").select_option(previous_language)
        page.set_viewport_size(previous_viewport)
