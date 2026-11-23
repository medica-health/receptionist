import json

import httpx
import pytest

from receptionist.clients.errors import Unavailable
from receptionist.clients.spring import SpringClient


def make(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SpringClient("http://spring", "key123", http)


async def test_spring_sends_api_key_and_body():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("x-api-key")
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "success": True,
                "message": "ok",
                "timestamp": "t",
                "data": {"id": "1", "email": "e", "fullName": "A B"},
            },
        )

    out = await make(handler).create_phone_patient("A B", "+919999999999", "idem-1")
    assert out == {"id": "1", "email": "e", "fullName": "A B"}
    assert seen == {
        "key": "key123",
        "path": "/api/internal/phone-patients",
        "method": "POST",
        "body": {"fullName": "A B", "phoneE164": "+919999999999", "idempotencyKey": "idem-1"},
    }


async def test_spring_timeout_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("t", request=request)

    with pytest.raises(Unavailable):
        await make(handler).create_phone_patient("A", "+91", "k")


async def test_spring_ping():
    assert await make(lambda r: httpx.Response(200, json={"data": {}})).ping() is True
    assert await make(lambda r: httpx.Response(500)).ping() is False
