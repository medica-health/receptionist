from receptionist.agent.language import FILLER, LanguageTracker, utterance_language


def test_devanagari_is_hindi():
    assert utterance_language("मुझे अपॉइंटमेंट चाहिए", None) == "hi"


def test_romanised_hindi_is_hindi():
    assert utterance_language("mujhe appointment chahiye kal", None) == "hi"


def test_english_is_english():
    assert utterance_language("I need an appointment", None) == "en"


def test_single_lexicon_hit_is_not_enough():
    assert utterance_language("kal is fine", None) == "en"


def test_stt_lang_used_as_fallback():
    assert utterance_language("okay", "hi-IN") == "hi"
    assert utterance_language("okay", "en-IN") == "en"
    assert utterance_language("okay", "fr-FR") == "en"


def test_one_hindi_utterance_does_not_switch():
    t = LanguageTracker()
    assert t.observe("mujhe appointment chahiye", None) is None
    assert t.current == "en"


def test_two_hindi_utterances_switch():
    t = LanguageTracker()
    assert t.observe("mujhe appointment chahiye", None) is None
    assert t.observe("kal subah theek hai ji", None) == "hi"
    assert t.current == "hi"
    assert t.observe("haan ji theek hai", None) is None


def test_hinglish_alternation_never_switches():
    t = LanguageTracker()
    turns = ["I need a doctor", "mujhe kal chahiye", "tomorrow morning please", "kya aap batao"] * 3
    assert all(t.observe(x, None) is None for x in turns)
    assert t.current == "en"


def test_force_switches_immediately():
    t = LanguageTracker()
    assert t.force("hi") == "hi"
    assert t.current == "hi"
    assert t.force("hi") is None


def test_fillers():
    assert FILLER == {"en": "One moment, let me check.", "hi": "एक सेकंड, मैं देख रही हूँ।"}
