import json
from datetime import UTC, date, datetime

import httpx
import pytest

from receptionist.clients.errors import ApiError, BadRequest, Conflict, NotFound, Unavailable
from receptionist.clients.express import ExpressClient


class FakeIssuer:
    def __init__(self):
        self.calls = 0

    def patient_token(self, user_id, email, name):
        self.calls += 1
        return f"tok{self.calls}"


def envelope(data, status=200, message="ok"):
    return httpx.Response(
        status, json={"success": status < 400, "message": message, "data": data, "timestamp": "t"}
    )


def make(handler, issuer=None, clock=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw = {"clock": clock} if clock else {}
    return ExpressClient("http://express", issuer or FakeIssuer(), http, **kw)


async def test_session_sends_bearer():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return envelope({"id": "p"})

    client = make(handler)
    data = await client.as_patient("u", "e@x.y", "N").get_profile()
    assert data == {"id": "p"}
    assert seen == {"auth": "Bearer tok1", "path": "/api/patients/me/profile"}


async def test_anonymous_has_no_auth_and_params():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["q"] = dict(request.url.params)
        return envelope({"items": []})

    await make(handler).search_doctors(specialization_id=3, city="Pune")
    assert seen["auth"] is None
    assert seen["q"] == {"specializationId": "3", "city": "Pune", "limit": "5"}


async def test_slots_query():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["q"] = dict(request.url.params)
        return envelope([])

    await make(handler).slots("d1", date(2026, 11, 24))
    assert seen == {"path": "/api/doctors/d1/slots", "q": {"date": "2026-11-24"}}


async def test_upcoming_query():
    seen = {}

    def handler(request):
        seen["q"] = dict(request.url.params)
        return envelope({"items": []})

    s = make(handler).as_patient("u", "e", "n")
    await s.list_appointments("upcoming")
    q = seen["q"]
    assert q["order"] == "asc" and q["limit"] == "5" and q["status"] == "BOOKED,CONFIRMED"
    assert "to" not in q
    assert abs(datetime.fromisoformat(q["from"]) - datetime.now(UTC)).total_seconds() < 5


async def test_past_query():
    seen = {}

    def handler(request):
        seen["q"] = dict(request.url.params)
        return envelope({"items": []})

    await make(handler).as_patient("u", "e", "n").list_appointments("past")
    q = seen["q"]
    assert q["order"] == "desc" and "from" not in q and "to" in q
    datetime.fromisoformat(q["to"])


async def test_book_body_and_iso_offset():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["method"] = request.method
        return envelope({"id": "a"}, 201)

    start = datetime(2026, 11, 24, 4, 30, tzinfo=UTC)
    await make(handler).as_patient("u", "e", "n").book("d", "c", start, "checkup")
    assert seen["method"] == "POST"
    assert seen["body"] == {
        "doctorId": "d",
        "clinicId": "c",
        "startAt": "2026-11-24T04:30:00+00:00",
        "reason": "checkup",
    }


async def test_update_profile_put():
    seen = {}

    def handler(request):
        seen["m"] = request.method
        seen["body"] = json.loads(request.content)
        return envelope({})

    await make(handler).as_patient("u", "e", "n").update_profile(date(1990, 1, 2))
    assert seen == {"m": "PUT", "body": {"dateOfBirth": "1990-01-02"}}


async def test_hold_release_cancel_reschedule_resend():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        return envelope({})

    s = make(handler).as_patient("u", "e", "n")
    await s.release_hold("h1")
    await s.cancel("a1", "busy")
    await s.reschedule("a1", datetime(2026, 11, 24, 4, 30, tzinfo=UTC), "c1")
    await s.resend_confirmation("a1")
    assert [(m, p) for m, p, _ in seen] == [
        ("DELETE", "/api/appointments/holds/h1"),
        ("POST", "/api/appointments/a1/cancel"),
        ("POST", "/api/appointments/a1/reschedule"),
        ("POST", "/api/appointments/a1/resend-confirmation"),
    ]
    assert json.loads(seen[1][2]) == {"reason": "busy"}
    assert json.loads(seen[2][2]) == {"startAt": "2026-11-24T04:30:00+00:00", "clinicId": "c1"}


async def test_token_reminted_near_expiry():
    issuer = FakeIssuer()
    now = [1000.0]
    client = make(lambda r: envelope({}), issuer, clock=lambda: now[0])
    s = client.as_patient("u", "e", "n")
    await s.get_profile()
    await s.get_profile()
    assert issuer.calls == 1
    now[0] += 280  # within 30s of the 300s expiry
    await s.get_profile()
    assert issuer.calls == 2


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (409, Conflict),
        (404, NotFound),
        (400, BadRequest),
        (422, BadRequest),
        (500, Unavailable),
        (503, Unavailable),
        (401, ApiError),
        (403, ApiError),
    ],
)
async def test_status_mapping(status, exc):
    client = make(lambda r: envelope({"x": 1}, status, message="boom"))
    with pytest.raises(exc) as ei:
        await client.specializations()
    assert type(ei.value) is exc
    assert ei.value.status == status
    assert ei.value.message == "boom"
    assert ei.value.data == {"x": 1}


async def test_409_raises_conflict_with_message():
    client = make(lambda r: envelope(None, 409, message="Slot already taken"))
    with pytest.raises(Conflict, match="Slot already taken"):
        await client.doctor("d")


async def test_non_json_error_body():
    client = make(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(Unavailable):
        await client.specializations()


async def test_timeout_raises_unavailable():
    def handler(request):
        raise httpx.ConnectTimeout("t", request=request)

    with pytest.raises(Unavailable) as ei:
        await make(handler).specializations()
    assert ei.value.spoken == "I'm having trouble reaching our system right now."


async def test_connect_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("x", request=request)

    with pytest.raises(Unavailable):
        await make(handler).specializations()


async def test_500_raises_unavailable():
    with pytest.raises(Unavailable):
        await make(lambda r: envelope(None, 500)).specializations()


async def test_ping():
    assert await make(lambda r: envelope({})).ping() is True
    assert await make(lambda r: envelope(None, 500)).ping() is False

    def boom(request):
        raise httpx.ConnectError("x", request=request)

    assert await make(boom).ping() is False
