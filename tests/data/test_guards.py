import fakeredis.aioredis
import pytest

from receptionist.config import Settings
from receptionist.data.guards import Guards

NUMBER = "+919876543210"


@pytest.fixture
def settings() -> Settings:
    return Settings(public_base_url="https://x.test", jwt_secret="x" * 32)


@pytest.fixture
async def client():
    c = fakeredis.aioredis.FakeRedis()
    yield c
    await c.aclose()


async def test_eleventh_call_in_hour_rejected(client, settings):
    guards = Guards(client, settings)
    for _ in range(10):
        assert await guards.register_call(NUMBER) is True
    assert await guards.register_call(NUMBER) is False
    ttl = await client.ttl(f"rcpt:calls:{NUMBER}")
    assert 0 < ttl <= 3600


async def test_lock_after_five_failures(client, settings):
    guards = Guards(client, settings)
    for _ in range(4):
        await guards.record_failure(NUMBER)
    assert await guards.is_locked(NUMBER) is False
    assert await guards.record_failure(NUMBER) == 5
    assert await guards.is_locked(NUMBER) is True
    ttl = await client.ttl(f"rcpt:verify-fail:{NUMBER}")
    assert 0 < ttl <= 86400


async def test_set_call_meta(client, settings):
    guards = Guards(client, settings)
    await guards.set_call_meta("CA1", {"a": 1})
    assert await client.get("rcpt:call:CA1") == b'{"a": 1}'
    assert 0 < await client.ttl("rcpt:call:CA1") <= 3600
    assert await guards.ping() is True


async def test_fail_open_without_redis(settings):
    guards = Guards(None, settings)
    assert await guards.register_call(NUMBER) is True
    assert await guards.is_locked(NUMBER) is False
    assert await guards.record_failure(NUMBER) == 0
    await guards.set_call_meta("CA1", {})
    assert await guards.ping() is False


class _Broken:
    async def incr(self, *a, **k):
        raise ConnectionError("down")

    get = expire = set = ping = incr


async def test_fail_open_on_error(settings, caplog):
    guards = Guards(_Broken(), settings)  # type: ignore[arg-type]
    with caplog.at_level("WARNING"):
        assert await guards.register_call(NUMBER) is True
        assert await guards.is_locked(NUMBER) is False
        assert await guards.record_failure(NUMBER) == 0
        assert await guards.ping() is False
    assert len(caplog.records) == 1
    assert NUMBER not in caplog.text
