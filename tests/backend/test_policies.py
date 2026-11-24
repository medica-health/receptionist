from datetime import UTC, datetime, timedelta

import pytest

from receptionist.backend.errors import BadRequest, Conflict
from receptionist.backend.policies import (
    Slot,
    assert_patient_can_cancel,
    assert_patient_can_change,
    filter_available,
    is_active,
)

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


def test_is_active():
    assert is_active("BOOKED") and is_active("CONFIRMED")
    assert not any(is_active(s) for s in ("CHECKED_IN", "COMPLETED", "CANCELLED", "NO_SHOW"))


def test_patient_change_cutoff_is_two_hours():
    assert_patient_can_change(NOW + timedelta(hours=2), NOW)
    with pytest.raises(BadRequest, match="2 hours"):
        assert_patient_can_change(NOW + timedelta(hours=1, minutes=59), NOW)


@pytest.mark.parametrize(
    ("status", "error", "match"),
    [
        ("CANCELLED", Conflict, "already CANCELLED"),
        ("CHECKED_IN", Conflict, "from CHECKED_IN"),
        ("COMPLETED", Conflict, "from COMPLETED"),
        ("NO_SHOW", Conflict, "from NO_SHOW"),
    ],
)
def test_patient_cancel_rejects_inactive(status, error, match):
    with pytest.raises(error, match=match):
        assert_patient_can_cancel(status, NOW + timedelta(days=1), NOW)


def test_patient_cancel_allows_booked_and_confirmed_before_cutoff():
    for status in ("BOOKED", "CONFIRMED"):
        assert_patient_can_cancel(status, NOW + timedelta(days=1), NOW)
    with pytest.raises(BadRequest):
        assert_patient_can_cancel("BOOKED", NOW + timedelta(minutes=30), NOW)


def test_filter_available_drops_past_and_busy_and_sorts():
    def at(h: int, m: int = 0) -> datetime:
        return NOW.replace(hour=h, minute=m)

    slots = [
        Slot(at(9), at(9, 30), "c1"),
        Slot(at(5), at(5, 30), "c1"),  # past
        Slot(at(8), at(8, 30), "c1"),
        Slot(at(10), at(10, 30), "c1"),  # busy
    ]
    busy = [(at(10, 15), at(10, 45))]
    assert [s.start_at for s in filter_available(slots, busy, NOW)] == [at(8), at(9)]
