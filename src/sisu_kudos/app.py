"""FastAPI application and same-origin live stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, load_settings
from .resources import read_resource_snapshot
from .state import LiveState

WEB = Path(__file__).parent / "web"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create an ASGI app; implicit configuration is loaded only at startup."""

    @asynccontextmanager
    async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
        actual = settings if settings is not None else load_settings()
        live_state = LiveState(actual)
        instance.state.kudos_settings = actual
        instance.state.kudos_state = live_state
        try:
            await live_state.start()
            yield
        finally:
            await live_state.stop()

    def configured_settings(instance: FastAPI) -> Settings:
        loaded = getattr(instance.state, "kudos_settings", None)
        if loaded is not None:
            return loaded
        return settings if settings is not None else load_settings()

    instance = FastAPI(
        title="KUDOS",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    instance.add_middleware(GZipMiddleware, minimum_size=1_000, compresslevel=4)

    @instance.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response

    @instance.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    @instance.get("/api/snapshot")
    async def snapshot(request: Request) -> JSONResponse:
        payload = await request.app.state.kudos_state.snapshot()
        payload["app"] = {"name": "KUDOS", "version": __version__}
        return JSONResponse(payload)

    @instance.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        payload = await request.app.state.kudos_state.snapshot()
        collector_health = payload.get("collector_health", {})
        ok = bool(payload.get("generated_at")) and any(
            item.get("ok") for item in collector_health.values()
        )
        return JSONResponse(
            {
                "ok": ok,
                "version": __version__,
                "revision": payload.get("revision", 0),
                "collectors": collector_health,
            },
            status_code=200 if ok else 503,
        )

    @instance.get("/api/agent-resources")
    async def agent_resources(request: Request) -> JSONResponse:
        configured = configured_settings(request.app)
        if not configured.resources_enabled:
            return JSONResponse(read_resource_snapshot(None, use_environment=False, disabled=True))
        return JSONResponse(
            read_resource_snapshot(configured.resource_snapshot_path, use_environment=False)
        )

    @instance.get("/api/logs")
    async def logs(request: Request) -> JSONResponse:
        payload = await request.app.state.kudos_state.snapshot()
        return JSONResponse(payload.get("logs", {}))

    @instance.get("/api/disks/largest-files")
    async def largest_files(request: Request, mount: str) -> JSONResponse:
        try:
            payload = await request.app.state.kudos_state.largest_files(mount)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="filesystem scan timed out") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(payload)

    @instance.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            sent = -1
            # Bound each stream lifetime. This gives systemd a deterministic drain
            # path while the browser reconnects deliberately without console noise.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20
            while loop.time() < deadline and not await request.is_disconnected():
                payload = await request.app.state.kudos_state.stream_snapshot()
                revision = int(payload.get("revision", 0))
                if revision != sent:
                    payload["app"] = {"name": "KUDOS", "version": __version__}
                    body = json.dumps(payload, ensure_ascii=False)
                    yield f"id: {revision}\nevent: snapshot\ndata: {body}\n\n"
                    sent = revision
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(3)
            yield "event: reconnect\ndata: {}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    instance.mount("/assets", StaticFiles(directory=WEB), name="assets")
    return instance


# Importable ASGI target. Its config is read by the lifespan, not module import.
app = create_app()
