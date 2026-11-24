"""Production wiring: the real collaborators behind AppDeps, and their startup/shutdown."""

import httpx
import redis.asyncio as redis

from receptionist.agent.llm import make_llm
from receptionist.app import AppDeps
from receptionist.backend.holds import SlotHolds
from receptionist.backend.notify import Notifier
from receptionist.backend.store import Backend
from receptionist.config import Settings
from receptionist.data.db import PatientDirectory
from receptionist.data.guards import Guards
from receptionist.health import HealthChecker, Probe

# Twilio SMS and Resend email only; nothing else is reached over HTTP.
_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def build_deps(settings: Settings) -> AppDeps:
    """Real clients for every configured dependency. Nothing connects until `startup`
    (the Postgres pool) or first use (Redis, HTTP). Unconfigured dependencies are None and
    report "disabled" in /api/health; outside APP_ENV=test the settings require them all.
    The receptionist talks only to the shared Postgres and Redis (plus Twilio/Resend for
    confirmations), never to express-api or spring-api."""
    http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    # Short socket timeouts: Guards and holds fail open, so a dead Redis must fail fast.
    redis_client = (
        redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=1)
        if settings.redis_url
        else None
    )
    directory = PatientDirectory(settings.database_url) if settings.database_url else None
    guards = Guards(redis_client, settings)
    backend = None
    notifier = None
    if directory is not None:
        holds = SlotHolds(
            redis_client, settings.slot_hold_ttl_seconds, settings.slot_cache_ttl_seconds
        )
        notifier = Notifier(directory.pool, http, settings)
        backend = Backend(directory.pool, holds, notifier)

    probes: dict[str, Probe] = {}
    if directory is not None:
        probes["postgres"] = directory.ping
    if redis_client is not None:
        probes["redis"] = guards.ping

    async def startup() -> None:
        if directory is not None:
            await directory.open()  # doesn't wait for a connection: a DB outage isn't fatal

    async def shutdown() -> None:
        try:
            if notifier is not None:
                await notifier.drain()
        finally:
            try:
                if directory is not None:
                    await directory.close()
            finally:
                try:
                    await http.aclose()
                finally:
                    if redis_client is not None:
                        await redis_client.aclose()

    return AppDeps(
        settings=settings,
        directory=directory,
        guards=guards,
        backend=backend,
        llm_factory=lambda: make_llm(settings),
        health=HealthChecker(probes, timeout=3.0),
        startup=startup,
        shutdown=shutdown,
    )
