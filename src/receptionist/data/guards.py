"""Cross-call counters in Redis. Fail-open: Redis trouble never blocks a caller."""

import json
import logging

import redis.asyncio as redis

from receptionist.config import Settings
from receptionist.identity.phone import mask_phone

log = logging.getLogger(__name__)

_CALLS_TTL = 3600
_FAIL_TTL = 86400
_META_TTL = 3600


class Guards:
    def __init__(self, client: redis.Redis | None, settings: Settings) -> None:
        self._r = client
        self._settings = settings
        self._warned = False

    def _warn(self, e164: str | None, why: str) -> None:
        if self._warned:
            return
        self._warned = True
        who = f" for {mask_phone(e164)}" if e164 else ""
        log.warning("redis guards failing open%s: %s", who, why)

    async def register_call(self, e164: str) -> bool:
        if self._r is None:
            self._warn(e164, "no redis client")
            return True
        key = f"rcpt:calls:{e164}"
        try:
            count = await self._r.incr(key)
            await self._r.expire(key, _CALLS_TTL, nx=True)
        except Exception as exc:
            self._warn(e164, type(exc).__name__)
            return True
        return count <= self._settings.calls_per_hour_per_number

    async def is_locked(self, e164: str) -> bool:
        if self._r is None:
            self._warn(e164, "no redis client")
            return False
        try:
            value = await self._r.get(f"rcpt:verify-fail:{e164}")
        except Exception as exc:
            self._warn(e164, type(exc).__name__)
            return False
        return int(value or 0) >= self._settings.verify_max_failures_per_day

    async def record_failure(self, e164: str) -> int:
        if self._r is None:
            self._warn(e164, "no redis client")
            return 0
        key = f"rcpt:verify-fail:{e164}"
        try:
            count = await self._r.incr(key)
            # The failure that locks the number restarts the 24 h window, so a lock lasts
            # 24 h from the locking failure, not from the first one.
            locking = count == self._settings.verify_max_failures_per_day
            await self._r.expire(key, _FAIL_TTL, nx=not locking)
        except Exception as exc:
            self._warn(e164, type(exc).__name__)
            return 0
        return int(count)

    async def set_call_meta(self, call_sid: str, data: dict) -> None:
        if self._r is None:
            self._warn(None, "no redis client")
            return
        try:
            await self._r.set(f"rcpt:call:{call_sid}", json.dumps(data), ex=_META_TTL)
        except Exception as exc:
            self._warn(None, type(exc).__name__)

    async def ping(self) -> bool:
        if self._r is None:
            return False
        try:
            return bool(await self._r.ping())
        except Exception:
            return False
