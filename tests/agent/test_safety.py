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
        "papa ko lakwa maar gaya lagta hai",
    ],
)
def test_emergencies_detected(text):
    assert detect_emergency(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "I need a cardiologist appointment",
        "my chest X-ray report",
        "pain in my knee for two weeks",
        "I want an appointment for chest pain follow-up",
        "",
    ],
)
def test_non_emergencies(text):
    assert detect_emergency(text) is False


def test_self_harm_is_never_cancelled_by_exclusions():
    assert detect_emergency("I got my report and I want to kill myself") is True


def test_messages_are_fixed_copy():
    assert EMERGENCY_MESSAGE["en"] == (
        "This sounds like it could be an emergency. Please hang up and call 112 right now, "
        "or go to the nearest emergency room."
    )
    assert EMERGENCY_MESSAGE["hi"] == (
        "यह आपातकालीन स्थिति लग रही है। कृपया अभी फ़ोन रखकर 112 पर कॉल करें, या नज़दीकी इमरजेंसी में जाएँ।"
    )
