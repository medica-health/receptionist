"""Slot holds and the free-slot cache in Redis, shared with express-api.

Ported from express-api's services/slotHolds.service.ts and slotCache.service.ts. The key
names, member format and Lua scripts must stay identical: a hold the receptionist takes has
to block a web booking, and the reverse.

  slot-holds:{doctorId}    sorted set, score = expiry (ms), member = "holdId|startMs|endMs|holderId|clinicId"
  slot-hold:user:{userId}  "doctorId|member": the holder's current hold (one per user)
  slots:v:{doctorId}       cache version; bumping it orphans every cached slot list
  slots:{doctorId}:{version}:{date}:{clinicId|all}

Holds and the cache are advisory. Postgres' exclusion constraints are the guarantee against
double booking, so every read here fails open. The hold scripts touch keys outside KEYS, so
Redis must be single-node.
"""

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as redis

from receptionist.backend.errors import Conflict, Unavailable
from receptionist.backend.policies import Slot

log = logging.getLogger(__name__)

HOLDS_UNAVAILABLE = "Slot holds are unavailable right now. You can still book the slot directly."
HELD_BY_OTHER = "Another patient is holding this slot. Pick another time or try again shortly."

# KEYS[1] doctor zset, KEYS[2] holder key
# ARGV: ttlMs, holdId, startMs, endMs, holderId, doctorId, clinicId
_CREATE_HOLD = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local ttl = tonumber(ARGV[1])
local s, e = tonumber(ARGV[3]), tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
for _, m in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
  local _, ms, me, holder = string.match(m, '^([^|]+)|(%d+)|(%d+)|([^|]+)|')
  if holder ~= ARGV[5] and tonumber(ms) < e and tonumber(me) > s then
    return {0, tonumber(redis.call('ZSCORE', KEYS[1], m))}
  end
end

-- One hold per user: picking a new slot releases the previous one.
local prev = redis.call('GET', KEYS[2])
if prev then
  local sep = string.find(prev, '|', 1, true)
  redis.call('ZREM', 'slot-holds:' .. string.sub(prev, 1, sep - 1), string.sub(prev, sep + 1))
end

local member = ARGV[2] .. '|' .. ARGV[3] .. '|' .. ARGV[4] .. '|' .. ARGV[5] .. '|' .. ARGV[7]
redis.call('ZADD', KEYS[1], now + ttl, member)
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('SET', KEYS[2], ARGV[6] .. '|' .. member, 'PX', ttl)
return {1, now + ttl}
"""

# KEYS[1] holder key; ARGV[1] holdId ('' = whatever the user holds). Returns 1 if released.
_RELEASE_HOLD = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local sep = string.find(cur, '|', 1, true)
local member = string.sub(cur, sep + 1)
if ARGV[1] ~= '' and string.sub(member, 1, string.len(ARGV[1]) + 1) ~= ARGV[1] .. '|' then return 0 end
redis.call('ZREM', 'slot-holds:' .. string.sub(cur, 1, sep - 1), member)
redis.call('DEL', KEYS[1])
return 1
"""


def _doctor_key(doctor_id: str) -> str:
    return f"slot-holds:{doctor_id}"


def _holder_key(user_id: str) -> str:
    return f"slot-hold:user:{user_id}"


def _ms(dt: datetime) -> int:
    return round(dt.timestamp() * 1000)


def _from_ms(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


@dataclass(frozen=True)
class Held:
    start_at: datetime
    end_at: datetime
    holder_id: str
    expires_at: datetime


@dataclass(frozen=True)
class Hold:
    hold_id: str
    doctor_id: str
    clinic_id: str
    start_at: datetime
    end_at: datetime
    expires_at: datetime


def parse_member(member: str) -> tuple[str, int, int, str, str]:
    hold_id, start_ms, end_ms, holder_id, clinic_id = member.split("|")
    return hold_id, int(start_ms), int(end_ms), holder_id, clinic_id


class SlotHolds:
    def __init__(self, client: redis.Redis | None, hold_ttl_seconds: int, cache_ttl_seconds: int):
        self._r = client
        self._hold_ttl_ms = hold_ttl_seconds * 1000
        self._cache_ttl = cache_ttl_seconds

    # ------------------------------------------------------------------ holds

    async def create(self, holder_id: str, doctor_id: str, slot: Slot) -> Hold:
        """Holds `slot` for `holder_id`, replacing their previous hold. Conflict when someone
        else holds an overlapping slot, Unavailable when Redis is down."""
        if self._r is None:
            raise Unavailable(503, HOLDS_UNAVAILABLE)
        hold_id = str(uuid.uuid4())
        try:
            held, expires_ms = await self._r.eval(
                _CREATE_HOLD,
                2,
                _doctor_key(doctor_id),
                _holder_key(holder_id),
                self._hold_ttl_ms,
                hold_id,
                _ms(slot.start_at),
                _ms(slot.end_at),
                holder_id,
                doctor_id,
                slot.clinic_id,
            )
        except Exception as exc:
            log.warning("slot hold failed: %s", type(exc).__name__)
            raise Unavailable(503, HOLDS_UNAVAILABLE) from exc
        if not held:
            raise Conflict(409, HELD_BY_OTHER, {"heldUntil": _from_ms(expires_ms).isoformat()})
        return Hold(
            hold_id, doctor_id, slot.clinic_id, slot.start_at, slot.end_at, _from_ms(expires_ms)
        )

    async def current(self, holder_id: str) -> Hold | None:
        """The holder's current hold, or None (also when Redis is down)."""
        if self._r is None:
            return None
        try:
            cur = await self._r.get(_holder_key(holder_id))
            pttl = await self._r.pttl(_holder_key(holder_id))
        except Exception:
            return None
        if not cur or pttl <= 0:
            return None
        doctor_id, member = _text(cur).split("|", 1)
        hold_id, start_ms, end_ms, _, clinic_id = parse_member(member)
        expires = _from_ms(time.time() * 1000 + pttl)
        return Hold(hold_id, doctor_id, clinic_id, _from_ms(start_ms), _from_ms(end_ms), expires)

    async def release(self, holder_id: str, hold_id: str = "") -> bool:
        """Releases the holder's hold (only if it is `hold_id`, when given). Never raises."""
        if self._r is None:
            return False
        try:
            return await self._r.eval(_RELEASE_HOLD, 1, _holder_key(holder_id), hold_id) == 1
        except Exception:
            return False

    async def held(self, doctor_id: str, start: datetime, end: datetime) -> list[Held]:
        """Live holds overlapping [start, end). Empty when Redis is down."""
        if self._r is None:
            return []
        try:
            raw = await self._r.zrangebyscore(
                _doctor_key(doctor_id), round(time.time() * 1000), "+inf", withscores=True
            )
        except Exception:
            return []
        out: list[Held] = []
        for member, score in raw:
            _, start_ms, end_ms, holder, _ = parse_member(_text(member))
            if start_ms < _ms(end) and end_ms > _ms(start):
                out.append(Held(_from_ms(start_ms), _from_ms(end_ms), holder, _from_ms(score)))
        return out

    async def assert_not_held_by_others(self, doctor_id: str, slot: Slot, user_id: str) -> None:
        for h in await self.held(doctor_id, slot.start_at, slot.end_at):
            if h.holder_id != user_id:
                raise Conflict(409, HELD_BY_OTHER, {"heldUntil": h.expires_at.isoformat()})

    # ------------------------------------------------------------------ slot cache

    async def cached_slots(
        self,
        doctor_id: str,
        date: str,
        clinic_id: str | None,
        load: Callable[[], Awaitable[list[Slot]]],
    ) -> list[Slot]:
        if self._r is None:
            return await load()
        try:
            version = await self._r.get(f"slots:v:{doctor_id}")
            key = f"slots:{doctor_id}:{_text(version) if version else '0'}:{date}:{clinic_id or 'all'}"
            hit = await self._r.get(key)
        except Exception as exc:
            log.warning("slot cache read failed: %s", type(exc).__name__)
            return await load()
        if hit:
            now = datetime.now(UTC)
            slots = [
                Slot(
                    datetime.fromisoformat(s["startAt"]),
                    datetime.fromisoformat(s["endAt"]),
                    s["clinicId"],
                )
                for s in json.loads(hit)
            ]
            return [s for s in slots if s.start_at >= now]
        slots = await load()
        # Same JSON shape express-api writes (JSON.stringify of Dates: ...T05:30:00.000Z).
        payload = json.dumps(
            [
                {
                    "startAt": _js_iso(s.start_at),
                    "endAt": _js_iso(s.end_at),
                    "clinicId": s.clinic_id,
                }
                for s in slots
            ]
        )
        try:
            await self._r.set(key, payload, ex=self._cache_ttl)
        except Exception as exc:
            log.warning("slot cache write failed: %s", type(exc).__name__)
        return slots

    async def invalidate(self, *doctor_ids: str) -> None:
        """Drops cached free slots for these doctors. Call after the change has committed."""
        if self._r is None or not doctor_ids:
            return
        try:
            pipe = self._r.pipeline(transaction=True)
            for doctor_id in dict.fromkeys(doctor_ids):
                pipe.incr(f"slots:v:{doctor_id}")
                pipe.expire(f"slots:v:{doctor_id}", 86_400)
            await pipe.execute()
        except Exception as exc:
            log.warning("slot cache invalidation failed: %s", type(exc).__name__)


def _js_iso(dt: datetime) -> str:
    """ISO 8601 the way JavaScript's Date.toJSON writes it."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
