import xml.etree.ElementTree as ET

from voice_fakes import settings

from receptionist.voice.twiml import WELCOME_GREETING, build_connect_twiml, build_reject_twiml


def _relay(xml: str) -> ET.Element:
    root = ET.fromstring(xml)
    relay = root.find("./Connect/ConversationRelay")
    assert relay is not None
    return relay


def test_connect_twiml_has_both_languages_wss_url_and_custom_params():
    xml = build_connect_twiml(
        "wss://receptionist.test/twilio/relay", settings(), {"callSid": "CA1"}
    )
    relay = _relay(xml)
    assert relay.get("url", "").startswith("wss://")
    assert relay.get("welcomeGreeting") == WELCOME_GREETING
    assert relay.get("interruptible") == "any"
    languages = {el.get("code"): el for el in relay.findall("Language")}
    assert set(languages) == {"en-IN", "hi-IN"}
    assert languages["en-IN"].get("voice") == "en-voice-v4"
    assert languages["hi-IN"].get("voice") == "hi-voice-v4"
    for el in languages.values():
        assert el.get("ttsProvider") == "ElevenLabs"
        assert el.get("transcriptionProvider") == "Deepgram"
        assert el.get("speechModel") == "nova-3-general"
    params = {p.get("name"): p.get("value") for p in relay.findall("Parameter")}
    assert params == {"callSid": "CA1"}


def test_welcome_greeting_is_verbatim():
    assert WELCOME_GREETING == (
        "Namaste! MediCare mein aapka swagat hai. Welcome to MediCare. Main aapki kaise madad "
        "kar sakti hoon? How can I help you today? This is an automated assistant."
    )


def test_unset_voice_is_left_out():
    relay = _relay(
        build_connect_twiml("wss://x.test/twilio/relay", settings(tts_voice_hi=None), {})
    )
    hindi = next(el for el in relay.findall("Language") if el.get("code") == "hi-IN")
    assert "voice" not in hindi.attrib


def test_reject_twiml_says_then_hangs_up():
    root = ET.fromstring(build_reject_twiml("Too many calls.", "बहुत ज़्यादा कॉल।"))
    assert [el.tag for el in root] == ["Say", "Say", "Hangup"]
    assert root[0].text == "Too many calls."
    assert root[0].get("language") == "en-IN"
    assert root[1].get("language") == "hi-IN"
