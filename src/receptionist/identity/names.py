"""Spoken-name matching (pure)."""

from rapidfuzz import fuzz

_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "kumari"}
_THRESHOLD = 88


def normalize_name(s: str) -> str:
    tokens = s.lower().replace(".", "").replace(",", "").split()
    while tokens and tokens[0] in _HONORIFICS:
        tokens.pop(0)
    if tokens and tokens[-1] == "ji":
        tokens.pop()
    return " ".join(tokens)


def names_match(spoken: str, stored: str) -> bool:
    a, b = normalize_name(spoken), normalize_name(stored)
    return bool(a and b) and fuzz.token_sort_ratio(a, b) >= _THRESHOLD
