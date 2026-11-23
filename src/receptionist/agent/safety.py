"""Emergency red-flag screen on each caller utterance (pure, no LLM)."""

import re
import unicodedata
from typing import Literal

Lang = Literal["en", "hi"]

EMERGENCY_MESSAGE: dict[Lang, str] = {
    "en": (
        "This sounds like it could be an emergency. Please hang up and call 112 right now, "
        "or go to the nearest emergency room."
    ),
    "hi": ("यह आपातकालीन स्थिति लग रही है। कृपया अभी फ़ोन रखकर 112 पर कॉल करें, या नज़दीकी इमरजेंसी में जाएँ।"),
}

# Devanagari patterns avoid \b: Python treats vowel signs (matras) as non-word characters.
# Patterns and input are both passed through _norm (nukta removed), so ज़/ज and ख़/ख match
# alike whether the nukta letter is precomposed (U+0958-U+095F) or decomposed.

# Chest pain is the only category the exclusion words may cancel ("my chest x-ray report",
# "an appointment for chest pain"): callers often mention it when booking or asking about records.
_CHEST = [
    r"\bchest\s+(pain|tightness|pressure|hurts?|is\s+(hurting|tight))",
    r"\bpain\s+in\s+(my|his|her|the)\s+chest",
    r"\bheart\s+attack",
    r"\b(seene|sine|seena|chhati|chati|chhaati)\s+(mein|me|main)\s+(dard|dabav|dabaav|jalan)",
    r"\bdil\s+ka\s+daura",
    r"(सीने|सीना|छाती)\s*(में|मे)\s*(दर्द|दबाव|जलन)",
    r"दिल\s*का\s*दौरा",
]

# Never cancelled by the exclusion words.
_ALWAYS = [
    # breathing
    r"\b(can'?t|cannot|can\s+not|unable\s+to|difficulty|trouble|hard\s+to|struggling\s+to)"
    r"\s+(breath|breathe|breathing)",
    r"\bbreathing\s+(difficulty|difficulties|problem|problems|trouble)",
    r"\bshort(ness)?\s+of\s+breath",
    r"\bnot\s+breathing",
    r"\bbreathless",
    r"\bgasping",
    r"\bchoking",
    r"\bsaa?ns\s+(lene\s+)?(mein|me|main)\s+(dikkat|dikkath|takleef|taklif|pareshani|problem)",
    r"\bsaa?ns\s+(nahi|nahin)\s+(aa|le|ho)",
    r"\bsaa?ns\s+(ruk|phool|ful)",
    r"(सांस|साँस)\s*(लेने\s*)?(में|मे)\s*(दिक्कत|तकलीफ|परेशानी)",
    r"(सांस|साँस)\s*(नहीं|रुक|फूल)",
    # unconscious / fainted
    r"\b(unconscious|unresponsive|fainted|passed\s+out|collapsed|not\s+responding)\b",
    r"\bblack(ed|s)?\s+out",
    r"\b(won'?t|will\s+not|isn'?t|is\s+not|not)\s+wak(e|ing)\s+up",
    r"\bbehosh",
    r"\bhosh\s+(nahi|nahin|mein\s+nahi)",
    r"बेहोश",
    r"होश\s*(नहीं|में\s*नहीं)",
    # stroke signs
    r"\bstroke\b",
    r"\bface\s+(is\s+)?(droop|drooping|drooped|numb)",
    r"\bslurr(ed|ing)",
    r"\b(one|left|right)\s+side\s+(of\s+(my|his|her|the)\s+(body|face)\s+)?(is\s+)?"
    r"(weak|numb|paraly)",
    r"\b(numb|weak|paralysed|paralyzed)\s+on\s+(one|the\s+left|the\s+right|left|right)\s+side",
    r"\bparaly(sed|sis)\b",
    r"\b(lakwa|lakva|laqwa)\b",
    r"\b(chehra|chehara|muh|munh)\s+(tedha|terha|latak)",
    r"\b(zuban|zubaan|jubaan|juban|awaaz|awaz)\s+(lad|ladkha|ladkhada)",
    r"लकवा",
    r"(चेहरा|मुँह|मुंह)\s*(टेढ़ा|टेढा|लटक)",
    r"(ज़ुबान|जुबान|आवाज़|आवाज)\s*(लड़खड़|लडखड)",
    # severe bleeding
    r"\b(severe|heavy|heavily|uncontrolled|lots\s+of|a\s+lot\s+of)\s+(bleeding|blood)",
    r"\bbleeding\s+(heavily|badly|a\s+lot|nonstop|non-stop)",
    r"\bbleeding\s+(that\s+)?(won'?t|will\s+not|doesn'?t|does\s+not)\s+stop",
    r"\b(coughing|vomiting|throwing)\s+(up\s+)?blood",
    r"\b(bahut|zyada|jyada|kaafi|kafi)\s+(khoon|khun)",
    r"\b(khoon|khun)\s+(ruk\s+nahi|band\s+nahi|beh|bah)",
    r"(बहुत|ज़्यादा|ज्यादा|काफी|काफ़ी)\s*(खून|ख़ून)",
    r"(खून|ख़ून)\s*(बह|रुक\s*नहीं|बंद\s*नहीं)",
    # poisoning / overdose
    r"\boverdos",
    r"\bpoison",
    r"\b(took|taken|swallowed|ate)\s+(too\s+many|a\s+lot\s+of|lots\s+of)\s+(\w+\s+)?"
    r"(pills|tablets|medicines?)",
    r"\b(swallowed|took|taken|ate)\s+(a\s+)?(whole\s+)?(bottle|strip|packet|box)\s+of",
    r"\bswallowed\s+(bleach|acid|poison|phenyl|kerosene)",
    r"\b(zeher|zehar|zahar|jahar)\b",
    r"\b(goli|goliyan|goliyaan|dawai|dawa)\s+(zyada|jyada|bahut)\s+(kha|le)",
    r"ज़हर|जहर",
    r"(ज़्यादा|ज्यादा|बहुत)\s*(गोलियां|गोलियाँ|गोली|दवा|दवाई)\s*(खा|ले)",
    # seizure
    r"\bseizure",
    r"\bconvuls",
    r"\b(having|had)\s+(a\s+)?fits?\b",
    r"\bfits?\s+(aa|aaye|aye|aaya|aya|pad)",
    r"\bdaura\s+(pad|pada|padh|aa)",
    r"\b(mirgi|jhatke)\b",
    r"दौरा\s*(पड़|पड|आ)",
    r"मिर्गी|झटके",
    # suicide / self-harm
    r"\b(kill|hurt|harm|cut)\s+(myself|himself|herself|themselves)\b",
    r"\bsuicid",
    r"\bend\s+(my|his|her)\s+life",
    r"\bend\s+it\s+all\b",
    r"\bwant\s+to\s+die\b",
    r"\b(don'?t|do\s+not|doesn'?t|does\s+not)\s+want\s+to\s+live",
    r"\bno\s+reason\s+to\s+live",
    r"\bself[-\s]?harm",
    r"\b(khudkushi|khudkhushi|aatmhatya|atmhatya|aatmahatya)\b",
    r"\bmarna\s+chaht",
    r"\bmar\s+(jaana|jana)\s+chaht",
    r"\bmar\s+jaa?(u|oo)n?(ga|gi)\b",
    r"\b(jeena|jina)\s+(nahi|nahin)",
    r"\bjeene\s+ka\s+(mann|man)\s+(nahi|nahin)",
    r"\b(apne\s+aap|khud)\s+ko\s+(maar|maarna|marna|nuksan|nuksaan|khatam)",
    r"आत्महत्या|ख़ुदकुशी|खुदकुशी",
    r"(खुद|ख़ुद|अपने\s*आप)\s*को\s*(मार|ख़त्म|खत्म|नुकसान)",
    r"मरना\s*चाह",
    r"जीना\s*(नहीं|नही)",
]

_NUKTA = "़"
_CURLY_APOSTROPHE = "’"


def _norm(text: str) -> str:
    """NFC leaves the nukta letters U+0958-U+095F decomposed (base + nukta); the nukta is then
    dropped so every spelling matches. Curly apostrophes from STT become plain."""
    return unicodedata.normalize("NFC", text).replace(_NUKTA, "").replace(_CURLY_APOSTROPHE, "'")


def _compile(patterns: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{_norm(p)})" for p in patterns), re.I)


_EXCLUSIONS = _compile([r"\breport", r"x[-\s]?ray", r"appointment\s+for", "रिपोर्ट", r"एक्स[-\s]?रे"])
_CHEST_RE = _compile(_CHEST)
_ALWAYS_RE = _compile(_ALWAYS)


def detect_emergency(text: str) -> bool:
    """True when the utterance carries an emergency red flag.

    The exclusion words (`report`, `x-ray`, `appointment for`) cancel only chest-pain matches,
    where the caller is usually talking about records or booking. Every other red flag stands.
    """
    if not text:
        return False
    text = _norm(text)
    if _ALWAYS_RE.search(text):
        return True
    return bool(_CHEST_RE.search(text)) and not _EXCLUSIONS.search(text)
