"""TwiML responses for the voice webhook (pure)."""

from twilio.twiml.voice_response import Connect, VoiceResponse

from receptionist.config import Settings

WELCOME_GREETING = (
    "Namaste! MediCare mein aapka swagat hai. Welcome to MediCare. Main aapki kaise madad kar "
    "sakti hoon? How can I help you today? This is an automated assistant."
)
# Language codes ConversationRelay uses for the two languages the agent speaks.
LANG_CODES = {"en": "en-IN", "hi": "hi-IN"}
# <Say> voice for the reject message; Polly Aditi speaks both en-IN and hi-IN.
REJECT_VOICE = "Polly.Aditi"


def _present(**attrs: str | None) -> dict[str, str]:
    """Drop unset attributes: the twilio builder would render them as "None"."""
    return {k: v for k, v in attrs.items() if v}


def build_connect_twiml(ws_url: str, settings: Settings, custom: dict[str, str]) -> str:
    """Open a ConversationRelay session on `ws_url`, English first, Hindi available."""
    voices = {"en": settings.tts_voice_en, "hi": settings.tts_voice_hi}
    speech = {
        "tts_provider": settings.tts_provider,
        "transcription_provider": settings.stt_provider,
        "speech_model": settings.stt_model,
    }
    response = VoiceResponse()
    connect = Connect()
    relay = connect.conversation_relay(
        **_present(
            url=ws_url,
            welcome_greeting=WELCOME_GREETING,
            interruptible="any",
            language=LANG_CODES["en"],
            voice=voices["en"],
            **speech,
        )
    )
    for lang, code in LANG_CODES.items():
        relay.language(**_present(code=code, voice=voices[lang], **speech))
    for name, value in custom.items():
        relay.parameter(name=name, value=value)
    response.append(connect)
    return response.to_xml()


def build_reject_twiml(message: str, hindi: str | None = None) -> str:
    """Say `message` (and its Hindi version) and hang up without connecting."""
    response = VoiceResponse()
    response.say(message, language=LANG_CODES["en"], voice=REJECT_VOICE)
    if hindi:
        response.say(hindi, language=LANG_CODES["hi"], voice=REJECT_VOICE)
    response.hangup()
    return response.to_xml()
