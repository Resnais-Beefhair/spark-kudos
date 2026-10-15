"""Async-friendly bounded collector scheduler for KUDOS."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


def _callable_source(fn: Callable[[], Any]) -> str:
    module = getattr(fn, "__module__", fn.__class__.__module__)
    name = getattr(fn, "__qualname__", fn.__class__.__name__)
    return f"{module}.{name}"


@dataclass(slots=True)
class CollectorSpec:
    name: str
    fn: Callable[[], Any]
    interval_s: float
    timeout_s: float = 5.0
    stale_after_s: float | None = None
    source: str = ""


@dataclass(slots=True)
class CollectorState:
    name: str
    interval_s: float
    timeout_s: float
    stale_after_s: float
    source: str = ""
    value: Any = None
    updated_at: float | None = None
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_duration_s: float | None = None
    running: bool = False
    error: str = ""
    failures: int = 0
    successes: int = 0
    refresh_requested: bool = True

    @property
    def ready(self) -> bool:
        return self.last_finished_at is not None

    def age_s(self, now: float | None = None) -> float | None:
        if self.updated_at is None:
            return None
        current = now if now is not None else time.time()
        return max(0.0, current - self.updated_at)

    def is_stale(self, now: float | None = None) -> bool:
        age = self.age_s(now)
        return age is not None and age > self.stale_after_s

    def running_for_s(self, now: float | None = None) -> float:
        if not self.running or self.last_started_at is None:
            return 0.0
        current = now if now is not None else time.monotonic()
        return max(0.0, current - self.last_started_at)

    def is_slow(self, now: float | None = None) -> bool:
        return self.running and self.running_for_s(now) > self.timeout_s


class CollectorScheduler:
    """Run blocking collectors in bounded, disposable daemon workers.

    Python cannot cancel a running thread. A timed-out worker is therefore
    detached and its result discarded. The collector is not started again
    until that daemon worker exits, but it no longer consumes scheduler
    capacity or prevents the interpreter from shutting down.
    """

    def __init__(self, specs: Iterable[CollectorSpec], *, max_concurrency: int = 6):
        self._specs = {spec.name: spec for spec in specs}
        self._states = {
            spec.name: CollectorState(
                name=spec.name,
                interval_s=spec.interval_s,
                timeout_s=spec.timeout_s,
                stale_after_s=spec.stale_after_s
                or max(spec.interval_s * 2.5, spec.timeout_s + 1.0),
                source=spec.source or _callable_source(spec.fn),
            )
            for spec in self._specs.values()
        }
        self._max_concurrency = max(1, int(max_concurrency))
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._orphaned: set[str] = set()
        self._watchers: set[asyncio.Task[Any]] = set()
        self._dirty: set[str] = set()
        self._closed = False

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def state(self, name: str) -> CollectorState:
        return self._states[name]

    def value(self, name: str, default: Any = None) -> Any:
        state = self._states[name]
        return state.value if state.updated_at is not None else default

    def request_refresh(self, *names: str) -> None:
        targets = names or tuple(self._specs)
        for name in targets:
            if name in self._states and name not in self._orphaned:
                self._states[name].refresh_requested = True

    def pop_dirty(self) -> set[str]:
        dirty = set(self._dirty)
        self._dirty.clear()
        return dirty

    def schedule_due(self, now: float | None = None) -> None:
        if self._closed:
            return
        current = time.monotonic() if now is None else now
        active = sum(1 for state in self._states.values() if state.running)
        for name, spec in self._specs.items():
            if active >= self._max_concurrency:
                break
            state = self._states[name]
            if state.running or name in self._orphaned:
                continue
            due = (
                state.refresh_requested
                or state.last_finished_at is None
                or current - state.last_finished_at >= spec.interval_s
            )
            if not due:
                continue
            state.refresh_requested = False
            state.running = True
            state.last_started_at = current
            task = asyncio.create_task(self._run_spec(spec))
            self._tasks[name] = task
            active += 1

    @staticmethod
    def _daemon_future(fn: Callable[[], Any]) -> asyncio.Future[Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def worker() -> None:
            try:
                result = fn()
            except BaseException as exc:  # delivered to the event loop
                outcome: tuple[bool, Any] = (False, exc)
            else:
                outcome = (True, result)

            def deliver() -> None:
                if future.done():
                    return
                ok, payload = outcome
                if ok:
                    future.set_result(payload)
                else:
                    future.set_exception(payload)

            with suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver)

        threading.Thread(
            target=worker,
            name="kudos-collector-daemon",
            daemon=True,
        ).start()
        return future

    async def _watch_orphan(self, name: str, worker: asyncio.Future[Any]) -> None:
        try:
            await asyncio.shield(worker)
        except BaseException:
            pass
        finally:
            self._orphaned.discard(name)
            if not self._closed:
                self._states[name].refresh_requested = True

    async def _run_spec(self, spec: CollectorSpec) -> None:
        state = self._states[spec.name]
        started_mono = state.last_started_at or time.monotonic()
        worker = self._daemon_future(spec.fn)
        timed_out = False
        try:
            value = await asyncio.wait_for(asyncio.shield(worker), timeout=spec.timeout_s)
        except TimeoutError:
            timed_out = True
            if not self._closed:
                state.error = f"timeout after {spec.timeout_s:.1f}s"
                state.failures += 1
                state.last_duration_s = time.monotonic() - started_mono
                state.last_finished_at = time.monotonic()
                state.running = False
                self._dirty.add(spec.name)
                self._orphaned.add(spec.name)
                watcher = asyncio.create_task(self._watch_orphan(spec.name, worker))
                self._watchers.add(watcher)
                watcher.add_done_callback(self._watchers.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                state.error = type(exc).__name__
                state.failures += 1
        else:
            if not self._closed:
                state.value = value
                state.updated_at = time.time()
                state.error = ""
                state.successes += 1
        finally:
            if not timed_out and not self._closed:
                state.last_duration_s = time.monotonic() - started_mono
                state.last_finished_at = time.monotonic()
                state.running = False
                self._dirty.add(spec.name)
            current = self._tasks.get(spec.name)
            if current is asyncio.current_task():
                self._tasks.pop(spec.name, None)

    async def wait_for(self, names: Iterable[str] | None = None, timeout: float = 10.0) -> bool:
        targets = tuple(names or self._specs)
        self.request_refresh(*targets)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            self.schedule_due()
            if all(self._states[name].ready and not self._states[name].running for name in targets):
                return True
            await asyncio.sleep(0.01)
        return all(self._states[name].ready and not self._states[name].running for name in targets)

    def close(self) -> None:
        self._closed = True
        for task in list(self._tasks.values()):
            task.cancel()
        for watcher in list(self._watchers):
            watcher.cancel()
        self._tasks.clear()
        self._watchers.clear()
        for state in self._states.values():
            state.running = False
        self._dirty.clear()
