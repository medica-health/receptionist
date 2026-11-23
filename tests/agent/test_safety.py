import pytest

from receptionist.agent.safety import EMERGENCY_MESSAGE, detect_emergency


@pytest.mark.parametrize(
    "text",
    [
        "I have chest pain",
        "mujhe saans lene mein dikkat ho rahi hai",
        "सीने में दर्द",
        "he is unconscious",
        "I want to kill myself",
        "bahut khoon beh raha hai",
        "My father's face is drooping and his speech is slurred",
        "she took too many sleeping pills",
        "he is having a seizure",
        "मेरी माँ बेहोश हो गई हैं",
        "I can't breathe",
        "I can’t breathe",
        "papa ko lakwa maar gaya lagta hai",
        # precomposed nukta letters: U+095B (za), U+0959 (kha)
        "ज़हर खा लिया",
        "ख़ून बह रहा",
        "मैं ख़ुद को मार दूंगा",
        # romanised / English self-harm
        "main marna chahta hoon",
        "marna chahti hu",
        "jeena nahi chahta",
        "mujhe jeena nahi hai",
        "main mar jaunga",
        "I don't want to live anymore",
        "I want to end it all",
        # exclusion words only cancel chest pain
        "I need an appointment for my father, he is unconscious",
        "I want to report that my mother collapsed",
        "got the xray, now he cannot breathe",
        # more phrasings
        "breathing difficulty",
        "he is not waking up",
        "swallowed a bottle of pills",
        "blacked out",
        "gasping",
        "numb on one side",
        "fits aa rahe",
    ],
)
def test_emergencies_detected(text):
    assert detect_emergency(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "I need a cardiologist appointment",
        "my chest X-ray report",
        "my chest xray report",
        "pain in my knee for two weeks",
        "I want an appointment for chest pain follow-up",
        "I feel fit and want a check-up",
        "",
    ],
)
def test_non_emergencies(text):
    assert detect_emergency(text) is False


def test_exclusions_only_cancel_chest_pain():
    assert detect_emergency("chest pain") is True
    assert detect_emergency("appointment for chest pain") is False
    assert detect_emergency("I got my report and I want to kill myself") is True
    assert detect_emergency("appointment for my son, he had a seizure") is True


def test_nukta_spellings_all_match():
    assert detect_emergency("ज़हर खा लिया") is True  # decomposed ja + nukta
    assert detect_emergency("जहर खा लिया") is True  # no nukta
    assert detect_emergency("ज़हर खा लिया") is True  # precomposed


def test_messages_are_fixed_copy():
    assert EMERGENCY_MESSAGE["en"] == (
        "This sounds like it could be an emergency. Please hang up and call 112 right now, "
        "or go to the nearest emergency room."
    )
    assert EMERGENCY_MESSAGE["hi"] == (
        "यह आपातकालीन स्थिति लग रही है। कृपया अभी फ़ोन रखकर 112 पर कॉल करें, या नज़दीकी इमरजेंसी में जाएँ।"
    )
