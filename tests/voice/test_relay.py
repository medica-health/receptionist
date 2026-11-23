import asyncio
import logging
import threading
import time

import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect
from voice_fakes import (
    RELAY_URL,
    FakeHold,
    FakeSession,
    Harness,
    prompt,
    receive_until_last,
    setup_msg,
    sign,
    tokens,
    wait_for,
)

from receptionist.session import Speech
from receptionist.voice.relay import RelayConnection


def wait_until(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def test_setup_then_prompt_streams_tokens_and_last():
    h = Harness(tokens("Hello", " there"))
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("hi"))
        frames = receive_until_last(ws)
    assert frames == [
        {"type": "text", "token": "Hello", "last": False, "lang": "en-IN"},
        {"type": "text", "token": " there", "last": False, "lang": "en-IN"},
        {"type": "text", "token": "", "last": True, "lang": "en-IN"},
    ]
    assert h.session.ctx.call_sid == "CA1"
    assert h.session.raw_from == "+919876543210"


def test_filler_is_spoken_with_a_trailing_space():
    async def script(_session, _text):
        yield Speech("filler", "One moment, let me check.")
        yield Speech("token", "Found it.")

    h = Harness(script)
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("hi"))
        frames = receive_until_last(ws)
    assert [f["token"] for f in frames] == ["One moment, let me check. ", "Found it.", ""]


def test_non_final_prompt_ignored():
    h = Harness(tokens("ok"))
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("hel", last=False))
        ws.send_json(prompt("hello"))
        receive_until_last(ws)
    assert h.session.log == ["start:hello", "end:hello"]


def test_prompt_before_setup_ignored():
    h = Harness(tokens("ok"))
    with h.connect() as ws:
        ws.send_json(prompt("early"))
        ws.send_json(setup_msg())
        ws.send_json(prompt("later"))
        receive_until_last(ws)
    assert h.session.log == ["start:later", "end:later"]


def test_switch_language_sends_language_message():
    async def script(session, _text):
        session.ctx.language.current = "hi"
        yield Speech("switch_language", lang="hi")
        yield Speech("token", "नमस्ते")

    h = Harness(script)
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("namaste"))
        frames = receive_until_last(ws)
    assert frames == [
        {"type": "language", "ttsLanguage": "hi-IN", "transcriptionLanguage": "hi-IN"},
        {"type": "text", "token": "नमस्ते", "last": False, "lang": "hi-IN"},
        {"type": "text", "token": "", "last": True, "lang": "hi-IN"},
    ]


def test_end_sends_end_message():
    h = Harness(tokens("Goodbye!", end=True))
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("bye"))
        frames = receive_until_last(ws)
        assert [f["token"] for f in frames] == ["Goodbye!", ""]
        assert ws.receive_json() == {"type": "end"}


def test_interrupt_mutes_rest_and_trims(monkeypatch):
    interrupted = threading.Event()
    original = RelayConnection._on_interrupt

    async def spy(self, msg):
        await original(self, msg)
        interrupted.set()

    monkeypatch.setattr(RelayConnection, "_on_interrupt", spy)

    async def script(_session, text):
        if text == "first":
            yield Speech("token", "Your appointment is")
            await wait_for(interrupted)
            yield Speech("token", " on Monday")
            yield Speech("token", " at ten.")
        else:
            yield Speech("token", "Sure.")

    h = Harness(script)
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("first"))
        assert ws.receive_json()["token"] == "Your appointment is"
        ws.send_json(
            {
                "type": "interrupt",
                "utteranceUntilInterrupt": "Your appointment is",
                "durationUntilInterruptMs": 900,
            }
        )
        wait_until(lambda: h.session.trims == ["Your appointment is"])
        assert h.session.log == ["start:first", "end:first"]
        ws.send_json(prompt("second"))
        # Nothing from the muted first turn (no rest of it, no last frame) comes first.
        frames = receive_until_last(ws)
    assert [f["token"] for f in frames] == ["Sure.", ""]
    assert h.session.trims == ["Your appointment is"]


def test_interrupt_after_run_finished_trims_right_away():
    h = Harness(tokens("Your slot is booked for Monday."))
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("book"))
        receive_until_last(ws)
        ws.send_json({"type": "interrupt", "utteranceUntilInterrupt": "Your slot"})
        wait_until(lambda: h.session.trims == ["Your slot"])


def test_prompts_queue_while_running():
    async def script(_session, text):
        if text == "first":
            yield Speech("token", "a")
            await asyncio.sleep(0.05)
            yield Speech("token", "b")
        else:
            yield Speech("token", "c")

    h = Harness(script)
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("first"))
        ws.send_json(prompt("second"))
        first = receive_until_last(ws)
        second = receive_until_last(ws)
    assert [f["token"] for f in first] == ["a", "b", ""]
    assert [f["token"] for f in second] == ["c", ""]
    assert h.session.log == ["start:first", "end:first", "start:second", "end:second"]


def test_close_mid_run_releases_hold_and_no_send_after_close(monkeypatch, caplog):
    sent: list[str] = []
    original_send = WebSocket.send_text

    async def spy(self, data: str) -> None:
        sent.append(data)
        await original_send(self, data)

    monkeypatch.setattr(WebSocket, "send_text", spy)

    async def script(session: FakeSession, _text):
        session.ctx.pending_hold = FakeHold("hold-1")
        yield Speech("token", "one")
        await asyncio.sleep(0.3)
        yield Speech("token", "two")
        yield Speech("end")

    h = Harness(script)
    caplog.set_level(logging.WARNING)
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("book it"))
        assert ws.receive_json()["token"] == "one"
    wait_until(lambda: h.session.ctx.patient_session.released == ["hold-1"])
    assert h.session.log == ["start:book it", "end:book it"]  # the run was left to finish
    assert h.session.ctx.pending_hold is None
    assert h.app.state.calls == {}
    assert not any('"two"' in s or '"end"' in s for s in sent)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_registry_holds_session_while_open():
    h = Harness(tokens("ok"))
    with h.connect() as ws:
        ws.send_json(setup_msg("CA9"))
        ws.send_json(prompt("hi"))
        receive_until_last(ws)
        assert h.app.state.calls["CA9"] is h.session
    assert h.app.state.calls == {}


def test_bad_handshake_signature_rejected():
    h = Harness(tokens("ok"))
    with pytest.raises(WebSocketDisconnect):
        with h.client.websocket_connect("/twilio/relay", headers={"X-Twilio-Signature": "x"}):
            pass
    assert h.sessions == []


def test_https_form_of_relay_url_signature_accepted():
    h = Harness(tokens("ok"))
    url = RELAY_URL.replace("wss://", "https://")
    with h.client.websocket_connect(
        "/twilio/relay", headers={"X-Twilio-Signature": sign(url)}
    ) as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("hi"))
        receive_until_last(ws)


def test_dtmf_error_and_garbage_are_ignored(caplog):
    h = Harness(tokens("ok"))
    caplog.set_level(logging.INFO)
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_text("not json")
        ws.send_json({"type": "dtmf", "digit": "1"})
        ws.send_json(
            {"type": "error", "description": "Invalid message received, code: 64107, my DOB"}
        )
        ws.send_json(prompt("hi"))
        receive_until_last(ws)
    assert "64107" in caplog.text
    assert "my DOB" not in caplog.text


def test_failing_turn_still_closes_the_talk_cycle_and_keeps_serving():
    async def script(_session, text):
        yield Speech("token", "Partial")
        if text == "boom":
            raise RuntimeError("caller said something private")

    h = Harness(script)
    with h.connect() as ws:
        ws.send_json(setup_msg())
        ws.send_json(prompt("boom"))
        assert [f["token"] for f in receive_until_last(ws)] == ["Partial", ""]
        ws.send_json(prompt("again"))
        assert [f["token"] for f in receive_until_last(ws)] == ["Partial", ""]


def _rejected_setup(msg: dict) -> Harness:
    h = Harness(tokens("ok"))
    with h.connect() as ws:
        ws.send_json(msg)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    assert h.sessions == []
    assert h.app.state.calls == {}
    return h


def test_forged_from_is_ignored_in_favour_of_the_token():
    h = Harness(tokens("ok"))
    with h.connect() as ws:
        ws.send_json(setup_msg(from_="+911111111111", token_from="+919876543210"))
        ws.send_json(prompt("hi"))
        receive_until_last(ws)
    assert h.session.raw_from == "+919876543210"


def test_withheld_caller_token_gives_no_number():
    h = Harness(tokens("ok"))
    with h.connect() as ws:
        ws.send_json(setup_msg(from_="+919876543210", token_from=None))
        ws.send_json(prompt("hi"))
        receive_until_last(ws)
    assert h.session.raw_from is None


def test_setup_without_token_closes_1008():
    _rejected_setup(setup_msg(token=None))


def test_setup_with_invalid_token_closes_1008():
    _rejected_setup(setup_msg(token="bogus.token"))


def test_token_for_another_call_sid_closes_1008():
    _rejected_setup(setup_msg("CA1", token_sid="CA2"))


def test_expired_token_closes_1008():
    from voice_fakes import TOKEN

    from receptionist.voice.relay_token import mint_relay_token

    expired = mint_relay_token("CA1", "+919876543210", TOKEN, now=time.time() - 301)
    _rejected_setup(setup_msg(token=expired))


def test_reused_token_while_call_is_open_closes_1008():
    h = Harness(tokens("ok"))
    msg = setup_msg()
    with h.connect() as first:
        first.send_json(msg)
        first.send_json(prompt("hi"))
        receive_until_last(first)
        with h.connect() as second:
            second.send_json(msg)
            with pytest.raises(WebSocketDisconnect) as exc:
                second.receive_json()
            assert exc.value.code == 1008
    assert len(h.sessions) == 1
