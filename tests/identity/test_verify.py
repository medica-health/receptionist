from datetime import date

from receptionist.identity.verify import Candidate, Outcome, Verifier, validate_dob

A = Candidate("a", "Abhinav Singh", "a@x", date(1990, 3, 5))
B = Candidate("b", "Ritu Singh", "b@x", date(1992, 1, 2))
N = Candidate("n", "Ravi Singh", "n@x", None)


def test_single_match_verified():
    r = Verifier([A]).attempt("Abhinav Singh", date(1990, 3, 5))
    assert r.outcome == Outcome.VERIFIED
    assert r.candidate == A
    assert r.save_dob is False


def test_wrong_dob_counts_and_locks():
    v = Verifier([A])
    r1 = v.attempt("Abhinav Singh", date(1990, 3, 6))
    assert r1.outcome == Outcome.WRONG_DOB
    assert r1.attempts_left == 1
    r2 = v.attempt("Abhinav Singh", date(1990, 3, 7))
    assert r2.outcome == Outcome.LOCKED
    assert r2.attempts_left == 0
    r3 = v.attempt("Abhinav Singh", date(1990, 3, 5))
    assert r3.outcome == Outcome.LOCKED
    assert r3.attempts_left == 0


def test_family_number_picks_by_name():
    r = Verifier([A, B]).attempt("Ritu Singh", date(1992, 1, 2))
    assert r.outcome == Outcome.VERIFIED
    assert r.candidate == B


def test_unknown_name_is_not_found_and_not_a_failure():
    v = Verifier([A])
    r = v.attempt("Meena Devi", date(1970, 1, 1))
    assert r.outcome == Outcome.NOT_FOUND
    assert v.failures == 0


def test_sole_account_without_dob_saves_dob():
    r = Verifier([N]).attempt("Ravi Singh", date(1980, 6, 1))
    assert r.outcome == Outcome.VERIFIED
    assert r.candidate == N
    assert r.save_dob is True


def test_shared_number_without_dob_unverifiable():
    v = Verifier([A, N])
    r = v.attempt("Ravi Singh", date(1980, 6, 1))
    assert r.outcome == Outcome.UNVERIFIABLE
    assert v.failures == 0


def test_two_name_matches_same_dob_unverifiable():
    c1 = Candidate("1", "Abhinav Singh", "1@x", date(1990, 3, 5))
    c2 = Candidate("2", "Abhinav Singh", "2@x", date(1990, 3, 5))
    r = Verifier([c1, c2]).attempt("Abhinav Singh", date(1990, 3, 5))
    assert r.outcome == Outcome.UNVERIFIABLE


def test_wrong_dob_counted_even_with_undated_namesake():
    n2 = Candidate("n2", "Abhinav Singh", "n2@x", None)
    v = Verifier([A, n2])
    r1 = v.attempt("Abhinav Singh", date(1991, 1, 1))
    assert r1.outcome == Outcome.WRONG_DOB
    assert v.failures == 1
    assert v.attempt("Abhinav Singh", date(1991, 1, 2)).outcome == Outcome.LOCKED


def test_exact_match_wins_over_undated_namesake():
    n2 = Candidate("n2", "Abhinav Singh", "n2@x", None)
    r = Verifier([A, n2]).attempt("Abhinav Singh", date(1990, 3, 5))
    assert r.outcome == Outcome.VERIFIED
    assert r.candidate == A


def test_empty_candidates_not_found():
    assert Verifier([]).attempt("Abhinav Singh", date(1990, 3, 5)).outcome == Outcome.NOT_FOUND


def test_validate_dob():
    today = date(2026, 11, 22)
    assert not validate_dob(date(2027, 1, 1), today)
    assert not validate_dob(date(1890, 1, 1), today)
    assert validate_dob(date(1990, 3, 5), today)
    assert validate_dob(date(1906, 11, 22), today)
    assert not validate_dob(date(1906, 11, 21), today)
    # Feb 29 boundary: 1904 is a leap year, so the bound is exactly 1904-02-29
    assert validate_dob(date(1904, 2, 29), date(2024, 2, 29))
    assert not validate_dob(date(1904, 2, 28), date(2024, 2, 29))
    # 1900 is not a leap year: falls back to Feb 28
    assert validate_dob(date(1900, 2, 28), date(2020, 2, 29))
    assert not validate_dob(date(1900, 2, 27), date(2020, 2, 29))
    # non-leap edge
    assert validate_dob(date(1905, 3, 1), date(2025, 3, 1))
    assert not validate_dob(date(1905, 2, 28), date(2025, 3, 1))
