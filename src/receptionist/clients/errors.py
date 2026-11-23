"""Errors raised by the API clients."""

from typing import Any


class ApiError(Exception):
    """Non-success API response. 401/403 stay plain: they mean our token is wrong."""

    def __init__(self, status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data


class Unavailable(ApiError):
    """Timeout, connection error or 5xx."""

    spoken = "I'm having trouble reaching our system right now."


class Conflict(ApiError):
    """409."""


class NotFound(ApiError):
    """404."""


class BadRequest(ApiError):
    """400 or 422."""
