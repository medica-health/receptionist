"""Caller verification by name + date of birth (pure)."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from receptionist.identity.names import names_match


@dataclass(frozen=True)
class Candidate:
    id: str
    full_name: str
    email: str
    dob: date | None


class Outcome(StrEnum):
    VERIFIED = "VERIFIED"
    WRONG_DOB = "WRONG_DOB"
    LOCKED = "LOCKED"
    NOT_FOUND = "NOT_FOUND"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class VerifyResult:
    outcome: Outcome
    candidate: Candidate | None = None
    save_dob: bool = False
    attempts_left: int = 0


class Verifier:
    def __init__(self, candidates: list[Candidate], max_attempts: int = 2):
        self._candidates = candidates
        self._max_attempts = max_attempts
        self.failures = 0

    def attempt(self, full_name: str, dob: date) -> VerifyResult:
        if self.failures >= self._max_attempts:
            return VerifyResult(Outcome.LOCKED)
        matches = [c for c in self._candidates if names_match(full_name, c.full_name)]
        if not matches:
            return VerifyResult(Outcome.NOT_FOUND)
        exact = [c for c in matches if c.dob == dob]
        if len(exact) == 1:
            return VerifyResult(Outcome.VERIFIED, exact[0])
        if len(exact) > 1:
            return VerifyResult(Outcome.UNVERIFIABLE)
        if all(c.dob is None for c in matches):
            if len(self._candidates) == 1:
                return VerifyResult(Outcome.VERIFIED, matches[0], save_dob=True)
            return VerifyResult(Outcome.UNVERIFIABLE)
        self.failures += 1
        left = self._max_attempts - self.failures
        if left <= 0:
            return VerifyResult(Outcome.LOCKED)
        return VerifyResult(Outcome.WRONG_DOB, attempts_left=left)


def validate_dob(dob: date, today: date) -> bool:
    """Not in the future and age at most 120."""
    if dob > today:
        return False
    try:
        oldest = today.replace(year=today.year - 120)
    except ValueError:  # today is Feb 29 and the year 120 ago was not a leap year
        oldest = date(today.year - 120, 2, 28)
    return dob >= oldest
