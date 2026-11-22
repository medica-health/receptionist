"""Emergency red-flag screen on each caller utterance (pure, no LLM)."""

import re
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
_GENERAL = [
    # chest pain / heart attack
    r"\bchest\s+(pain|tightness|pressure|hurts?|is\s+(hurting|tight))",
    r"\bpain\s+in\s+(my|his|her|the)\s+chest",
    r"\bheart\s+attack",
    r"\b(seene|sine|seena|chhati|chati|chhaati)\s+(mein|me|main)\s+(dard|dabav|dabaav|jalan)",
    r"\bdil\s+ka\s+daura",
    r"(सीने|सीना|छाती)\s*(में|मे)\s*(दर्द|दबाव|जलन)",
    r"दिल\s*का\s*दौरा",
    # breathing
    r"\b(can'?t|cannot|can\s+not|unable\s+to|difficulty|trouble|hard\s+to|struggling\s+to)"
    r"\s+(breath|breathe|breathing)",
    r"\bshort(ness)?\s+of\s+breath",
    r"\bnot\s+breathing",
    r"\bbreathless",
    r"\bchoking",
    r"\bsaa?ns\s+(lene\s+)?(mein|me|main)\s+(dikkat|dikkath|takleef|taklif|pareshani|problem)",
    r"\bsaa?ns\s+(nahi|nahin)\s+(aa|le|ho)",
    r"\bsaa?ns\s+(ruk|phool|ful)",
    r"(सांस|साँस)\s*(लेने\s*)?(में|मे)\s*(दिक्कत|तकलीफ़|तकलीफ|परेशानी)",
    r"(सांस|साँस)\s*(नहीं|रुक|फूल)",
    # unconscious / fainted
    r"\b(unconscious|unresponsive|fainted|passed\s+out|collapsed|not\s+responding)\b",
    r"\bwon'?t\s+wake\s+up",
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
    r"\bswallowed\s+(bleach|acid|poison|phenyl|kerosene)",
    r"\b(zeher|zehar|zahar|jahar)\b",
    r"\b(goli|goliyan|goliyaan|dawai|dawa)\s+(zyada|jyada|bahut)\s+(kha|le)",
    r"ज़हर|जहर",
    r"(ज़्यादा|ज्यादा|बहुत)\s*(गोलियां|गोलियाँ|गोली|दवा|दवाई)\s*(खा|ले)",
    # seizure
    r"\bseizure",
    r"\bconvuls",
    r"\b(having|had)\s+(a\s+)?fits?\b",
    r"\bdaura\s+(pad|pada|padh|aa)",
    r"\b(mirgi|jhatke)\b",
    r"दौरा\s*(पड़|पड|आ)",
    r"मिर्गी|झटके",
]

# Never cancelled by the exclusion words: these are always treated as an emergency.
_SELF_HARM = [
    r"\b(kill|hurt|harm|cut)\s+(myself|himself|herself|themselves)\b",
    r"\bsuicid",
    r"\bend\s+(my|his|her)\s+life",
    r"\bwant\s+to\s+die\b",
    r"\bself[-\s]?harm",
    r"\b(khudkushi|khudkhushi|aatmhatya|atmhatya|aatmahatya)\b",
    r"\b(mar|marna)\s+(jaana|jana)\s+chaht",
    r"\b(apne\s+aap|khud)\s+ko\s+(maar|maarna|marna|nuksan|nuksaan|khatam)",
    r"आत्महत्या|ख़ुदकुशी|खुदकुशी",
    r"(खुद|ख़ुद|अपने\s*आप)\s*को\s*(मार|ख़त्म|खत्म|नुकसान)",
    r"मरना\s*चाह",
]

_EXCLUSIONS = re.compile(r"\breport|x[-\s]?ray|appointment\s+for|रिपोर्ट|एक्स[-\s]?रे", re.I)
_GENERAL_RE = re.compile("|".join(f"(?:{p})" for p in _GENERAL), re.I)
_SELF_HARM_RE = re.compile("|".join(f"(?:{p})" for p in _SELF_HARM), re.I)


def detect_emergency(text: str) -> bool:
    """True when the utterance carries an emergency red flag.

    The exclusion words (`report`, `x-ray`, `appointment for`) cancel symptom matches, since the
    caller is then talking about records or booking. They never cancel self-harm statements.
    """
    if not text:
        return False
    text = text.replace("’", "'")  # STT often emits curly apostrophes
    if _SELF_HARM_RE.search(text):
        return True
    return bool(_GENERAL_RE.search(text)) and not _EXCLUSIONS.search(text)
