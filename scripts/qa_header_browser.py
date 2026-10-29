"""Synthetic browser checks for the compact masthead instruments."""

from pathlib import Path

from playwright.sync_api import expect


def run_header_checks(page, options: dict, output: Path) -> list[dict]:
    saved = options.copy()
    options.clear()
    options.update(dual=True, thermal_state="colors", language="fi")
    cards = page.locator("#header-temperatures .header-thermal")
    local, remote = cards.nth(0), cards.nth(1)
    expect(cards).to_have_count(2)
    expect(local.locator(".thermal-value")).to_have_text("48 °C")
    expect(remote.locator(".thermal-value")).to_have_text("88 °C")
    expect(local).to_have_attribute("data-band", "safe")
    expect(remote).to_have_attribute("data-band", "hot")
    expect(local.locator(".thermal-name")).to_have_text("Lab primary <GPU>")
    assert local.locator(".thermal-name gpu").count() == 0
    page.wait_for_function(
        "document.querySelector('.thermal-trace').getAttribute('d')?.includes('L')"
    )
    original = local.element_handle()
    page.wait_for_timeout(700)
    assert local.evaluate("(node, prior) => node === prior", original)
    local.focus()
    local.press("Enter")
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.get_by_role("dialog")).to_contain_text("LAB PRIMARY <GPU>")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).not_to_be_visible()
    assert local.evaluate("node => document.activeElement === node")
    rows = []
    for language in ("en", "fi"):
        page.locator("#language-select").select_option(language)
        assert "CPU/GPU" in local.get_attribute("aria-label")
        for width in (1920, 1366, 1101, 1100, 1000, 768, 390, 360, 320):
            page.set_viewport_size({"width": width, "height": 1080})
            metrics = page.locator(".masthead").evaluate("""header => {
              const nodes = [...header.children].filter(n => n.getBoundingClientRect().height > 0);
              const boxes = nodes.map(n => n.getBoundingClientRect());
              let overlaps = 0;
              for (let i=0;i<boxes.length;i++) for (let j=i+1;j<boxes.length;j++) {
                const a=boxes[i], b=boxes[j];
                if(a.left < b.right-1 && b.left < a.right-1 &&
                   a.top < b.bottom-1 && b.top < a.bottom-1) overlaps++;
              }
              return { overlaps, height: header.getBoundingClientRect().height,
                overflow: document.documentElement.scrollWidth-innerWidth,
                cards: [...header.querySelectorAll('.header-thermal')].map(n => ({
                  width:n.getBoundingClientRect().width, height:n.getBoundingClientRect().height,
                  overflow:n.scrollWidth-n.clientWidth
                }))};
            }""")
            assert metrics["overlaps"] == 0, (language, width, metrics)
            assert metrics["overflow"] <= 1, (language, width, metrics)
            assert all(c["height"] >= 44 and c["overflow"] <= 1 for c in metrics["cards"]), (
                language,
                width,
                metrics,
            )
            if width >= 1100:
                assert metrics["height"] <= 68, (width, metrics)
            if width in (1920, 390):
                page.locator(".masthead").screenshot(
                    path=str(output / f"thermal-{width}-{language}.png")
                )
            rows.append({"language": language, "width": width, **metrics})
    options["failed"] = True
    expect(remote).to_have_attribute("data-state", "stale")
    expect(remote).to_have_attribute("data-band", "unknown")
    expect(remote.locator(".thermal-state")).to_have_text("VANHA")
    expect(local).to_have_attribute("data-state", "fresh")
    options.update(failed=False, thermal_state="missing")
    expect(local).to_have_attribute("data-state", "unknown")
    expect(local.locator(".thermal-value")).to_have_text("–")
    expect(local.locator(".thermal-state")).to_have_text("EI DATAA")
    options["thermal_state"] = "empty"
    # Existing live history is intentionally retained across delta snapshots.
    # An initially empty history must therefore be checked in a fresh page.
    page.reload(wait_until="domcontentloaded")
    expect(local).to_have_attribute("data-state", "fresh")
    expect(local.locator(".thermal-trace")).to_have_attribute("d", "")
    expect(local.locator(".thermal-dot")).to_have_attribute("visibility", "hidden")
    options["thermal_state"] = "single"
    expect(local.locator(".thermal-dot")).to_have_attribute("visibility", "visible")
    # Each later fixture snapshot contributes a real new measured point.
    page.evaluate("window.dispatchEvent(new Event('offline'))")
    expect(local).to_have_attribute("data-state", "stale")
    expect(local).to_have_attribute("data-band", "unknown")
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(local).to_have_attribute("data-state", "fresh")
    options["dual"] = False
    expect(cards).to_have_count(1)
    options.clear()
    options.update(saved)
    return rows
