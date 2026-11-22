import pytest

from receptionist.identity.phone import mask_phone, match_key, normalize_phone


@pytest.mark.parametrize(
    "raw",
    ["9876543210", "98765 43210", "+91-98765-43210", "09876543210", "919876543210"],
)
def test_indian_mobile_variants(raw):
    assert normalize_phone(raw) == "+919876543210"


def test_international_kept():
    assert normalize_phone("+14155550100") == "+14155550100"


@pytest.mark.parametrize(
    "raw",
    [
        "anonymous",
        "",
        None,
        "+266696687",
        "+7378742833",
        "+2562533",
        "+8656696",
        "12345",
        "5876543210",
    ],
)
def test_invalid_or_sentinel(raw):
    assert normalize_phone(raw) is None


def test_match_key():
    assert match_key("+919876543210") == "9876543210"
    assert match_key("+14155550100") == "14155550100"


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91******3210"
