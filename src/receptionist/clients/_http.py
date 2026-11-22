"""Shared request helper: maps statuses to errors and unwraps the response envelope."""

from typing import Any

import httpx

from receptionist.clients.errors import (
    ApiError,
    BadRequest,
    Conflict,
    NotFound,
    Unavailable,
)

_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT)


def _error_for(status: int) -> type[ApiError]:
    if status >= 500:
        return Unavailable
    if status == 409:
        return Conflict
    if status == 404:
        return NotFound
    if status in (400, 422):
        return BadRequest
    return ApiError


async def send(http: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    """Send a request and return the envelope's `data`."""
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.TransportError as exc:  # timeouts, connect/read errors
        raise Unavailable(0, f"transport error: {type(exc).__name__}") from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    envelope = body if isinstance(body, dict) else {}
    if response.status_code >= 400:
        message = envelope.get("message") or f"HTTP {response.status_code}"
        raise _error_for(response.status_code)(
            response.status_code, str(message), envelope.get("data")
        )
    return envelope.get("data")
