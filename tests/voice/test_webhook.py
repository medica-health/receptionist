import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
from voice_fakes import BASE, FakeGuards, FakeHold, FakeSession, settings, sign

from receptionist.app import AppDeps, create_app
from receptionist.voice.routes import REJECT_EN, REJECT_HI

VOICE_URL = f"{BASE}/twilio/voice"
STATUS_URL = f"{BASE}/twilio/status"


def make(guards: FakeGuards | None = None, **overrides) -> tuple[TestClient, FakeGuards]:
    guards = guards or FakeGuards()
    app = create_app(AppDeps(settings=settings(**overrides), guards=guards))
    return TestClient(app), guards


def call_params(from_: str = "+919876543210", sid: str = "CA1") -> dict[str, str]:
    return {"CallSid": sid, "From": from_, "To": "+911234567890", "CallStatus": "ringing"}


def post(client: TestClient, url: str, params: dict[str, str], signature: str | None = None):
    path = url.removeprefix(BASE)
    headers = {"X-Twilio-Signature": signature if signature is not None else sign(url, params)}
    # Host differs from PUBLIC_BASE_URL on purpose: the signed URL must not come from it.
    return client.post(path, data=params, headers=headers)


def test_bad_signature_403():
    client, guards = make()
    res = post(client, VOICE_URL, call_params(), signature="bogus")
    assert res.status_code == 403
    assert guards.registered == []


def test_missing_signature_403():
    client, _ = make()
    res = client.post("/twilio/voice", data=call_params())
    assert res.status_code == 403


def test_signature_over_request_host_is_rejected():
    client, _ = make()
    params = call_params()
    res = post(client, VOICE_URL, params, signature=sign("http://testserver/twilio/voice", params))
    assert res.status_code == 403


def test_valid_signature_returns_connect_twiml():
    client, guards = make()
    res = post(client, VOICE_URL, call_params())
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/xml")
    relay = ET.fromstring(res.text).find("./Connect/ConversationRelay")
    assert relay is not None
    assert relay.get("url") == "wss://receptionist.test/twilio/relay"
    params = {p.get("name"): p.get("value") for p in relay.findall("Parameter")}
    assert params["callSid"] == "CA1"
    assert guards.registered == ["+919876543210"]
    assert guards.meta["CA1"]["from"] == "+919876543210"


def test_over_rate_limit_returns_reject_twiml():
    client, guards = make(FakeGuards(allow=False))
    res = post(client, VOICE_URL, call_params())
    assert res.status_code == 200
    root = ET.fromstring(res.text)
    assert [el.tag for el in root] == ["Say", "Say", "Hangup"]
    assert [el.text for el in root.findall("Say")] == [REJECT_EN, REJECT_HI]
    assert root.find("Connect") is None
    assert "CA1" not in guards.meta


def test_anonymous_caller_still_connects():
    client, guards = make()
    res = post(client, VOICE_URL, call_params(from_="anonymous"))
    assert res.status_code == 200
    assert ET.fromstring(res.text).find("./Connect/ConversationRelay") is not None
    assert guards.registered == []
    assert guards.meta["CA1"]["from"] is None


def test_draining_returns_503():
    client, guards = make()
    client.app.state.draining = True
    res = post(client, VOICE_URL, call_params())
    assert res.status_code == 503
    assert res.json()["success"] is False
    assert guards.registered == []


def test_status_bad_signature_403():
    client, _ = make()
    res = post(client, STATUS_URL, {"CallSid": "CA1", "CallStatus": "completed"}, "bogus")
    assert res.status_code == 403


def _registered_session(client: TestClient, hold_id: str | None) -> FakeSession:
    async def nothing(_session, _text):
        return
        yield

    session = FakeSession("CA1", "+919876543210", nothing)
    session.ctx.pending_hold = FakeHold(hold_id)
    client.app.state.calls["CA1"] = session
    return session


def test_status_completed_releases_hold_of_open_session():
    client, _ = make()
    session = _registered_session(client, "hold-1")
    res = post(client, STATUS_URL, {"CallSid": "CA1", "CallStatus": "completed"})
    assert res.status_code == 204
    assert session.ctx.patient_session.released == ["hold-1"]
    assert session.ctx.pending_hold is None


def test_status_in_progress_keeps_hold():
    client, _ = make()
    session = _registered_session(client, "hold-1")
    res = post(client, STATUS_URL, {"CallSid": "CA1", "CallStatus": "in-progress"})
    assert res.status_code == 204
    assert session.ctx.patient_session.released == []


def test_status_for_unknown_call_is_ok():
    client, _ = make()
    res = post(client, STATUS_URL, {"CallSid": "CA404", "CallStatus": "no-answer"})
    assert res.status_code == 204
