"""Errors raised by the backend. Statuses mirror express-api's, so callers can tell them apart."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import errors as pg
from psycopg_pool import PoolTimeout
from redis.exceptions import RedisError


class BackendError(Exception):
    """A request the backend couldn't complete."""

    def __init__(self, status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data


class Unavailable(BackendError):
    """Postgres or Redis is unreachable or timed out."""

    spoken = "I'm having trouble reaching our system right now."


class Conflict(BackendError):
    """409: the slot was taken, the appointment can't move to that status, and so on."""


class NotFound(BackendError):
    """404."""


class BadRequest(BackendError):
    """400: the request breaks a booking rule."""


# Same messages express-api returns for these constraints (middlewares/errorHandler.ts).
CONSTRAINT_MESSAGES = {
    "appointments_doctor_no_overlap": "This time slot is no longer available",
    "appointments_patient_no_overlap": "The patient already has an appointment at this time",
}


@contextmanager
def db_errors() -> Iterator[None]:
    """Maps database errors to backend errors, the way express-api's error handler does."""
    try:
        yield
    except (pg.UniqueViolation, pg.ExclusionViolation) as exc:
        constraint = exc.diag.constraint_name or ""
        raise Conflict(409, CONSTRAINT_MESSAGES.get(constraint, "Resource already exists")) from exc
    except pg.ForeignKeyViolation as exc:
        raise BadRequest(400, "Referenced resource does not exist") from exc
    except (pg.InvalidTextRepresentation, pg.InvalidDatetimeFormat) as exc:
        raise BadRequest(400, "Invalid identifier format") from exc
    except (PoolTimeout, psycopg.OperationalError, RedisError, TimeoutError) as exc:
        raise Unavailable(503, f"backend unavailable: {type(exc).__name__}") from exc
    except psycopg.Error as exc:
        raise BackendError(500, f"database error: {type(exc).__name__}") from exc
