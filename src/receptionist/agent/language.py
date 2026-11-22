"""Utterance language detection and switching hysteresis (pure)."""

import re

from receptionist.agent.safety import Lang

FILLER: dict[Lang, str] = {
    "en": "One moment, let me check.",
    "hi": "एक सेकंड, मैं देख रही हूँ।",
}

_LEXICON = {
    "hai", "hain", "nahi", "nahin", "mujhe", "mera", "meri", "kya", "aap", "chahiye", "karna",
    "karni", "hoon", "kal", "aaj", "ji", "theek", "kab", "kitne", "batao", "bataiye",
    "ko", "rahi", "raha", "rahe", "mein", "saans",
}  # fmt: skip
_PHRASES = ("doctor sahab",)
_WORD = re.compile(r"[a-z']+")


def _is_devanagari(ch: str) -> bool:
    return "ऀ" <= ch <= "ॿ"


def utterance_language(text: str, stt_lang: str | None) -> Lang:
    """Devanagari >= 30% of letters, or >= 2 romanised Hindi words -> hi; else the STT hint."""
    letters = [ch for ch in text if ch.isalpha() or _is_devanagari(ch)]
    if letters and sum(map(_is_devanagari, letters)) / len(letters) >= 0.3:
        return "hi"
    lowered = text.lower()
    hits = sum(1 for w in _WORD.findall(lowered) if w in _LEXICON)
    hits += sum(lowered.count(p) for p in _PHRASES)
    if hits >= 2:
        return "hi"
    if stt_lang and stt_lang.lower().startswith("hi"):
        return "hi"
    return "en"


class LanguageTracker:
    """Switches only after two consecutive utterances in the other language, or on request."""

    def __init__(self, initial: Lang = "en") -> None:
        self.current: Lang = initial
        self._streak = 0

    def observe(self, text: str, stt_lang: str | None) -> Lang | None:
        lang = utterance_language(text, stt_lang)
        if lang == self.current:
            self._streak = 0
            return None
        self._streak += 1
        if self._streak >= 2:
            return self.force(lang)
        return None

    def force(self, lang: Lang) -> Lang | None:
        """Switch now. Returns the new language, or None when it was already current."""
        self._streak = 0
        if lang == self.current:
            return None
        self.current = lang
        return lang


def emergency_language(tracker: LanguageTracker, text: str, stt_lang: str | None) -> Lang:
    """Language for the emergency message, chosen from the utterance that triggered it, not
    the tracker (which needs two Hindi turns to switch). A Hindi utterance also forces the
    tracker to Hindi, so TTS switches before the message is spoken."""
    if utterance_language(text, stt_lang) == "hi":
        tracker.force("hi")
        return "hi"
    return tracker.current
