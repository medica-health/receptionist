"""Caller ID normalisation (pure)."""

import re

_SENTINELS = {"+266696687", "+7378742833", "+2562533", "+8656696"}


def _indian_mobile(digits: str) -> str | None:
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def normalize_phone(raw: str | None) -> str | None:
    """Return E.164, or None when the caller ID is unusable."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d+]", "", raw)
    has_plus = cleaned.startswith("+")
    digits = cleaned.replace("+", "")
    if has_plus:
        if f"+{digits}" in _SENTINELS:
            return None
        if digits.startswith("91") and len(digits) == 12:
            return _indian_mobile(digits[2:])
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 12 and digits.startswith("91"):
        return _indian_mobile(digits[2:])
    if len(digits) == 11 and digits.startswith("0"):
        return _indian_mobile(digits[1:])
    return _indian_mobile(digits)


def match_key(e164: str) -> str:
    """Key used to look numbers up: last 10 digits for +91, else all digits."""
    digits = e164.lstrip("+")
    return digits[-10:] if e164.startswith("+91") else digits


def mask_phone(e164: str) -> str:
    """Mask all but the last 4 digits, keeping a +91 prefix, for logs."""
    prefix = "+91" if e164.startswith("+91") else "+"
    rest = e164[len(prefix) :]
    return f"{prefix}{'*' * max(len(rest) - 4, 0)}{rest[-4:]}"
