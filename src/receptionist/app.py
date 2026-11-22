"""FastAPI app factory and the response envelope shared with the sibling APIs."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from receptionist.config import Settings, get_settings
from receptionist.health import HealthChecker

log = logging.getLogger("receptionist")


@dataclass
class AppDeps:
    """Injectable collaborators. Typed Any until the producing task lands."""

    settings: Any = None
    directory: Any = None  # PatientDirectory (Task 3)
    guards: Any = None  # Guards (Task 3)
    express: Any = None  # Express client (Task 5)
    spring: Any = None  # Spring client (Task 5)
    llm_factory: Any = None  # Callable[[], BaseChatModel] (Task 9)
    health: Any = None  # HealthChecker or anything with async status()
    startup: Any = None  # async () -> None, run when the app starts (build_deps sets it)
    shutdown: Any = None  # async () -> None, run when the app stops


def envelope(success: bool, message: str, data: Any = None) -> dict[str, Any]:
    return {
        "success": success,
        "message": message,
        "data": data,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def ok(message: str, data: Any = None) -> dict[str, Any]:
    return envelope(True, message, data)


def _error(status: int, message: str, data: Any = None) -> JSONResponse:
    return JSONResponse(envelope(False, message, data), status_code=status)


def create_app(deps: AppDeps | None = None) -> FastAPI:
    deps = deps or AppDeps()
    settings: Settings = deps.settings or get_settings()
    health = deps.health or HealthChecker()

    from receptionist.voice import routes as voice  # voice -> session -> this module

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps.startup is not None:
            await deps.startup()
        try:
            yield
        finally:
            app.state.draining = True
            if deps.shutdown is not None:
                await deps.shutdown()

    app = FastAPI(title="MediCare receptionist", lifespan=lifespan)
    app.state.deps = deps
    app.state.settings = settings
    voice.init_state(app)
    app.include_router(voice.router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"message": "Running"}

    @app.get("/api/health", response_model=None)
    async def api_health() -> dict[str, Any] | JSONResponse:
        if app.state.draining:
            # The load balancer stops routing new calls here; live calls carry on.
            data = {"environment": settings.app_env, "draining": True}
            return _error(503, "Draining", data)
        return ok(
            "Health check",
            {"environment": settings.app_env, "dependencies": await health.status()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            message = "Route not found"
        elif exc.status_code == 405:
            message = "Method not allowed"
        elif exc.status_code >= 500:
            message = "Internal server error"
        else:
            message = str(exc.detail)
        return _error(exc.status_code, message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return _error(422, "Validation failed", errors)

    @app.exception_handler(Exception)
    async def unhandled_error(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return _error(500, "Internal server error")

    return app
