from datetime import UTC, datetime

from receptionist.backend.notify import format_when, is_phone_placeholder


def test_format_when_matches_en_in_full_short():
    when = datetime(2026, 11, 25, 5, 30, tzinfo=UTC)
    assert format_when(when, "Asia/Kolkata") == "Wednesday, 25 November 2026 at 11:00 am"
    assert format_when(datetime(2026, 11, 25, 12, 5, tzinfo=UTC), "Asia/Kolkata").endswith(
        "5:35 pm"
    )


def test_phone_placeholder():
    assert is_phone_placeholder("p-abc@phone.medicare.invalid")
    assert is_phone_placeholder(" P-ABC@PHONE.MEDICARE.INVALID ")
    assert not is_phone_placeholder("asha@example.com")
