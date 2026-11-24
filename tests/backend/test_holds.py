import json
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from receptionist.backend.errors import Conflict, Unavailable
from receptionist.backend.holds import HELD_BY_OTHER, SlotHolds, parse_member
from receptionist.backend.policies import Slot

START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
SLOT = Slot(START, START + timedelta(minutes=30), "c1")


@pytest.fixture
async def r():
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
def holds(r):
    return SlotHolds(r, hold_ttl_seconds=300, cache_ttl_seconds=60)


async def test_create_uses_express_keys_and_member_format(r, holds):
    hold = await holds.create("u1", "d1", SLOT)
    members = await r.zrange("slot-holds:d1", 0, -1)
    assert len(members) == 1
    hold_id, start_ms, end_ms, holder, clinic = parse_member(members[0].decode())
    assert (hold_id, holder, clinic) == (hold.hold_id, "u1", "c1")
    assert start_ms == int(START.timestamp() * 1000)
    assert end_ms - start_ms == 30 * 60 * 1000
    assert (await r.get("slot-hold:user:u1")).decode() == f"d1|{members[0].decode()}"


async def test_other_holder_conflicts_same_holder_replaces(holds):
    await holds.create("u1", "d1", SLOT)
    with pytest.raises(Conflict, match=HELD_BY_OTHER):
        await holds.create("u2", "d1", SLOT)
    later = Slot(SLOT.start_at + timedelta(hours=1), SLOT.end_at + timedelta(hours=1), "c1")
    second = await holds.create("u1", "d1", later)
    held = await holds.held("d1", SLOT.start_at, later.end_at)
    assert [h.start_at for h in held] == [later.start_at]
    assert (await holds.current("u1")).hold_id == second.hold_id


async def test_release_only_matching_hold(holds):
    hold = await holds.create("u1", "d1", SLOT)
    assert await holds.release("u1", "other") is False
    assert await holds.release("u1", hold.hold_id) is True
    assert await holds.current("u1") is None
    assert await holds.held("d1", SLOT.start_at, SLOT.end_at) == []


async def test_assert_not_held_by_others(holds):
    await holds.create("u1", "d1", SLOT)
    await holds.assert_not_held_by_others("d1", SLOT, "u1")
    with pytest.raises(Conflict):
        await holds.assert_not_held_by_others("d1", SLOT, "u2")


async def test_without_redis_holds_are_unavailable_and_reads_fail_open():
    holds = SlotHolds(None, 300, 60)
    with pytest.raises(Unavailable):
        await holds.create("u1", "d1", SLOT)
    assert await holds.held("d1", SLOT.start_at, SLOT.end_at) == []
    assert await holds.release("u1") is False
    await holds.assert_not_held_by_others("d1", SLOT, "u2")


async def test_slot_cache_round_trip_and_invalidation(r, holds):
    loads = []

    async def load():
        loads.append(1)
        return [SLOT]

    assert await holds.cached_slots("d1", "2026-11-25", None, load) == [SLOT]
    assert await holds.cached_slots("d1", "2026-11-25", None, load) == [SLOT]
    assert len(loads) == 1
    raw = json.loads(await r.get("slots:d1:0:2026-11-25:all"))
    assert raw[0]["startAt"].endswith(".000Z") and raw[0]["clinicId"] == "c1"
    await holds.invalidate("d1")
    assert (await r.get("slots:v:d1")) == b"1"
    await holds.cached_slots("d1", "2026-11-25", None, load)
    assert len(loads) == 2
