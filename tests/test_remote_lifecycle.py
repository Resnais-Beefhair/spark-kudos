"""Remote failure lifecycle without touching any live host or service."""

import asyncio
import importlib
import threading
import time


async def test_slow_remote_cannot_overlap_or_starve_sisu():
    module = importlib.import_module("sisu_kudos.scheduler")
    active = 0
    peak = 0
    calls = 0
    lock = threading.Lock()
    release = threading.Event()

    def slow():
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
        release.wait(1)
        with lock:
            active -= 1
        return 1

    scheduler = module.CollectorScheduler(
        [
            module.CollectorSpec("kide", slow, 0.01, 0.03),
            module.CollectorSpec("cpu", lambda: 25, 0.01, 0.03),
        ],
        max_concurrency=2,
    )
    try:
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            scheduler.schedule_due()
            await asyncio.sleep(0.005)
        assert scheduler.value("cpu") == 25
        assert scheduler.state("cpu").successes > 2
        assert scheduler.state("kide").error
        assert peak == 1 and calls == 1
    finally:
        release.set()
        scheduler.close()
        await asyncio.sleep(0.02)
