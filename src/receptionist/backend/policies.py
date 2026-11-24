"""Appointment rules for patients, ported from express-api's policies/appointmentStatus.ts and
policies/slots.ts. Keep both copies in step: express-api serves the same appointments."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from receptionist.backend.errors import BadRequest, Conflict

# Patients may cancel or reschedule only up to this many hours before the start.
PATIENT_CHANGE_CUTOFF_HOURS = 2
# Statuses a patient may cancel from (express-api: TRANSITIONS[from].CANCELLED includes PATIENT).
_PATIENT_CANCELLABLE = frozenset({"BOOKED", "CONFIRMED"})


@dataclass(frozen=True)
class Slot:
    start_at: datetime
    end_at: datetime
    clinic_id: str


def is_active(status: str) -> bool:
    return status in ("BOOKED", "CONFIRMED")


def assert_patient_can_change(start_at: datetime, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    if start_at - now < timedelta(hours=PATIENT_CHANGE_CUTOFF_HOURS):
        raise BadRequest(
            400,
            f"Appointments can only be changed up to {PATIENT_CHANGE_CUTOFF_HOURS} hours before "
            "they start",
        )


def assert_patient_can_cancel(status: str, start_at: datetime, now: datetime | None = None) -> None:
    if status == "CANCELLED":
        raise Conflict(409, "Appointment is already CANCELLED")
    if status not in _PATIENT_CANCELLABLE:
        raise Conflict(409, f"Cannot change appointment from {status} to CANCELLED")
    assert_patient_can_change(start_at, now)


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def filter_available(
    candidates: list[Slot], busy: list[tuple[datetime, datetime]], now: datetime | None = None
) -> list[Slot]:
    """Drops past slots and slots overlapping a busy interval; sorted by start."""
    now = now or datetime.now(UTC)
    free = [
        s
        for s in candidates
        if s.start_at >= now and not any(_overlaps(s.start_at, s.end_at, b, e) for b, e in busy)
    ]
    return sorted(free, key=lambda s: s.start_at)
