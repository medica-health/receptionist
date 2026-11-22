"""Fakes for the voice transport tests: settings, guards, a scripted CallSession."""

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from receptionist.agent.language import LanguageTracker
from receptionist.app import AppDeps, create_app
from receptionist.config import Settings
from receptionist.session import Speech
from receptionist.voice.relay_token import mint_relay_token

TOKEN = "test-auth-token"
BASE = "https://receptionist.test"
RELAY_URL = "wss://receptionist.test/twilio/relay"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "public_base_url": BASE,
        "jwt_secret": "x" * 32,
        "twilio_auth_token": TOKEN,
        "tts_voice_en": "en-voice-v4",
        "tts_voice_hi": "hi-voice-v4",
    }
    values.update(overrides)
    return Settings(**values)


def sign(url: str, params: dict[str, str] | None = None) -> str:
    return RequestValidator(TOKEN).compute_signature(url, params or {})


class FakeGuards:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.registered: list[str] = []
        self.meta: dict[str, dict] = {}

    async def register_call(self, e164: str) -> bool:
        self.registered.append(e164)
        return self.allow

    async def set_call_meta(self, call_sid: str, data: dict) -> None:
        self.meta[call_sid] = data


class FakePatient:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def release_hold(self, hold_id: str) -> None:
        self.released.append(hold_id)


@dataclass
class FakeHold:
    hold_id: str | None


@dataclass
class FakeCtx:
    call_sid: str
    language: LanguageTracker = field(default_factory=LanguageTracker)
    pending_hold: FakeHold | None = None
    patient_session: FakePatient = field(default_factory=FakePatient)

    def patient(self) -> FakePatient:
        return self.patient_session


Script = Callable[["FakeSession", str], Any]  # async generator function (session, text)


class FakeSession:
    """Stands in for CallSession. `script(session, text)` is an async generator of Speech."""

    def __init__(self, call_sid: str, raw_from: str | None, script: Script) -> None:
        self.ctx = FakeCtx(call_sid)
        self.raw_from = raw_from
        self.script = script
        self.log: list[str] = []
        self.trims: list[str] = []

    async def respond(self, text: str, stt_lang: str | None = None):
        self.log.append(f"start:{text}")
        async for speech in self.script(self, text):
            yield speech
        self.log.append(f"end:{text}")

    async def trim_last_reply(self, spoken_text: str) -> None:
        self.trims.append(spoken_text)


class Harness:
    def __init__(self, script: Script, guards: FakeGuards | None = None) -> None:
        self.guards = guards or FakeGuards()
        self.app: FastAPI = create_app(AppDeps(settings=settings(), guards=self.guards))
        self.sessions: list[FakeSession] = []
        self.started = threading.Event()

        async def factory(call_sid: str, raw_from: str | None, deps: AppDeps) -> FakeSession:
            session = FakeSession(call_sid, raw_from, script)
            self.sessions.append(session)
            return session

        self.app.state.session_factory = factory
        self.client = TestClient(self.app)

    @property
    def session(self) -> FakeSession:
        return self.sessions[0]

    def connect(self):
        return self.client.websocket_connect(
            "/twilio/relay", headers={"X-Twilio-Signature": sign(RELAY_URL)}
        )


def setup_msg(
    call_sid: str = "CA1",
    from_: str = "+919876543210",
    *,
    token_from: str | None = "+919876543210",
    token_sid: str | None = None,
    token: str | None = "mint",
) -> dict:
    """A setup message carrying a relay token minted (as the webhook does) for `token_sid`
    (default `call_sid`) and `token_from`. `token=None` leaves it out."""
    custom = {"callSid": call_sid}
    if token == "mint":
        token = mint_relay_token(token_sid or call_sid, token_from, TOKEN)
    if token is not None:
        custom["relayToken"] = token
    return {
        "type": "setup",
        "callSid": call_sid,
        "from": from_,
        "to": "+911234567890",
        "customParameters": custom,
    }


def prompt(text: str, last: bool = True, lang: str = "en-IN") -> dict:
    return {"type": "prompt", "voicePrompt": text, "lang": lang, "last": last}


def tokens(*pieces: str, end: bool = False):
    async def script(session: FakeSession, text: str):
        for piece in pieces:
            yield Speech("token", piece)
            await asyncio.sleep(0)
        if end:
            yield Speech("end")

    return script


def receive_until_last(ws) -> list[dict]:
    """Frames up to and including the next `text` frame with last=true."""
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") == "text" and frame.get("last") is True:
            return frames


async def wait_for(event: threading.Event, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() > deadline:
            raise TimeoutError("event not set")
        await asyncio.sleep(0.005)
