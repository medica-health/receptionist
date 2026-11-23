import pytest

from receptionist.voice.relay_token import RelayClaims, mint_relay_token, verify_relay_token

KEY = "auth-token"
NOW = 1_800_000_000


def test_round_trip():
    token = mint_relay_token("CA1", "+919876543210", KEY, now=NOW)
    assert verify_relay_token(token, KEY, now=NOW + 10) == RelayClaims(
        "CA1", "+919876543210", NOW + 300
    )


def test_withheld_caller_round_trips_as_none():
    token = mint_relay_token("CA1", None, KEY, now=NOW)
    assert verify_relay_token(token, KEY, now=NOW).caller_e164 is None


def test_tampered_payload_rejected():
    token = mint_relay_token("CA1", "+919876543210", KEY, now=NOW)
    _, mac = token.split(".")
    forged = mint_relay_token("CA1", "+911111111111", KEY, now=NOW).split(".")[0]
    assert verify_relay_token(f"{forged}.{mac}", KEY, now=NOW) is None


def test_wrong_key_rejected():
    token = mint_relay_token("CA1", "+919876543210", KEY, now=NOW)
    assert verify_relay_token(token, "other-key", now=NOW) is None


def test_expired_rejected():
    token = mint_relay_token("CA1", "+919876543210", KEY, now=NOW)
    assert verify_relay_token(token, KEY, now=NOW + 299) is not None
    assert verify_relay_token(token, KEY, now=NOW + 300) is None


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "!!.??", "abc."])
def test_malformed_rejected(token):
    assert verify_relay_token(token, KEY, now=NOW) is None


def test_empty_key_never_verifies_or_mints():
    with pytest.raises(ValueError):
        mint_relay_token("CA1", None, "", now=NOW)
    token = mint_relay_token("CA1", None, KEY, now=NOW)
    assert verify_relay_token(token, "", now=NOW) is None


def test_call_sid_mismatch_rejected():
    token = mint_relay_token("CA1", "+919876543210", KEY, now=NOW)
    assert verify_relay_token(token, KEY, call_sid="CA1", now=NOW) is not None
    assert verify_relay_token(token, KEY, call_sid="CA2", now=NOW) is None
