"""Spoken-style dates and times (pure)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from receptionist.agent.safety import Lang

_HI_DAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
_HI_MONTHS = [
    "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
    "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर",
]  # fmt: skip
_EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_EN_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip


def _hi_period(hour: int) -> str:
    if 4 <= hour < 12:
        return "सुबह"
    if 12 <= hour < 16:
        return "दोपहर"
    if 16 <= hour < 20:
        return "शाम"
    return "रात"


def local(dt: datetime, tz: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(ZoneInfo(tz))


def speak_time(dt: datetime, tz: str, lang: Lang) -> str:
    """Clock time in `tz`: "10:00 AM" (en) or "सुबह 10:00 बजे" (hi)."""
    t = local(dt, tz)
    h12 = t.hour % 12 or 12
    if lang == "hi":
        return f"{_hi_period(t.hour)} {h12}:{t.minute:02d} बजे"
    return f"{h12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def speak_date(d: date, lang: Lang) -> str:
    """Spoken date: "Tuesday, 24 November 2026" (en) or "मंगलवार, 24 नवंबर 2026" (hi)."""
    if lang == "hi":
        return f"{_HI_DAYS[d.weekday()]}, {d.day} {_HI_MONTHS[d.month - 1]} {d.year}"
    return f"{_EN_DAYS[d.weekday()]}, {d.day} {_EN_MONTHS[d.month - 1]} {d.year}"


def speak_when(dt: datetime, tz: str, lang: Lang) -> str:
    """Date and time of an appointment in its clinic's timezone."""
    joiner = "," if lang == "hi" else " at"
    return f"{speak_date(local(dt, tz).date(), lang)}{joiner} {speak_time(dt, tz, lang)}"
