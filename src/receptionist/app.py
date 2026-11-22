"""FastAPI app factory and the response envelope shared with the sibling APIs."""

import logging
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

    app = FastAPI(title="MediCare receptionist")
    app.state.deps = deps
    app.state.settings = settings

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"message": "Running"}

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        return ok(
            "Health check",
            {"environment": settings.app_env, "dependencies": await health.status()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = "Route not found" if exc.status_code == 404 else str(exc.detail)
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
