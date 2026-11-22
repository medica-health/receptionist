from datetime import UTC, date, datetime

from fakes import make_ctx

from receptionist.agent.prompts import SYSTEM_PROMPT, turn_context
from receptionist.agent.speech import speak_date, speak_time
from receptionist.identity.verify import Candidate

NOW = datetime(2026, 11, 22, 23, 30, tzinfo=UTC)
ABHINAV = Candidate("u1", "Abhinav Singh", "a@x.y", date(1990, 1, 2))


def test_turn_context_uses_ist_today_and_unverified_state():
    text = turn_context(make_ctx(candidates=[ABHINAV]), NOW)
    assert "Today is Monday, 23 November 2026" in text
    assert "Tomorrow is Tuesday, 24 November 2026" in text
    assert "Caller not verified" in text


def test_turn_context_verified_never_has_dob():
    ctx = make_ctx(candidates=[ABHINAV])
    ctx.verified = ABHINAV
    text = turn_context(ctx, NOW)
    assert "Verified as Abhinav Singh" in text
    for leak in ("1990", "02-01", "01-02", "2 January"):
        assert leak not in text


def test_turn_context_withheld_and_language():
    ctx = make_ctx(caller_e164=None)
    ctx.language.force("hi")
    text = turn_context(ctx, NOW)
    assert "Caller ID withheld" in text
    assert "Hindi" in text


def test_system_prompt_must_haves():
    p = SYSTEM_PROMPT.lower()
    for phrase in (
        "112",
        "one question at a time",
        "read back",
        "automated",
        "devanagari",
        "end_call",
        "i couldn't verify that",
        "40 words",
    ):
        assert phrase in p


def test_speak_time_in_clinic_tz():
    assert speak_time(datetime(2026, 11, 24, 4, 30, tzinfo=UTC), "Asia/Kolkata", "en") == "10:00 AM"
    assert speak_time(datetime(2026, 11, 24, 9, 0, tzinfo=UTC), "Asia/Kolkata", "en") == "2:30 PM"
    assert speak_time(datetime(2026, 11, 24, 4, 30, tzinfo=UTC), "Asia/Kolkata", "hi") == (
        "सुबह 10:00 बजे"
    )


def test_speak_date():
    assert speak_date(date(2026, 11, 24), "en") == "Tuesday, 24 November 2026"
    assert speak_date(date(2026, 11, 24), "hi") == "मंगलवार, 24 नवंबर 2026"


def test_turn_context_after_emergency_asks_if_safe_without_locking():
    ctx = make_ctx()
    assert "112" not in turn_context(ctx, NOW)
    ctx.emergency = True
    text = turn_context(ctx, NOW)
    assert "already given" in text
    assert "safe" in text
    assert "continue helping normally" in text
