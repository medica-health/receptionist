"""Production wiring: the real collaborators behind AppDeps, and their startup/shutdown."""

import redis.asyncio as redis

from receptionist.agent.llm import make_llm
from receptionist.app import AppDeps
from receptionist.clients._http import make_http_client
from receptionist.clients.express import ExpressClient
from receptionist.clients.spring import SpringClient
from receptionist.clients.tokens import LocalJwtIssuer
from receptionist.config import Settings
from receptionist.data.db import PatientDirectory
from receptionist.data.guards import Guards
from receptionist.health import HealthChecker, Probe


def build_deps(settings: Settings) -> AppDeps:
    """Real clients for every configured dependency. Nothing connects until `startup`
    (the Postgres pool) or first use (Redis, HTTP). Unconfigured dependencies are None and
    report "disabled" in /api/health; outside APP_ENV=test the settings require them all."""
    http = make_http_client()
    redis_client = redis.from_url(settings.redis_url) if settings.redis_url else None
    directory = PatientDirectory(settings.database_url) if settings.database_url else None
    guards = Guards(redis_client, settings)
    express = (
        ExpressClient(settings.express_api_url, LocalJwtIssuer.from_settings(settings), http)
        if settings.express_api_url
        else None
    )
    spring = (
        SpringClient(settings.spring_api_url, settings.api_key or "", http)
        if settings.spring_api_url
        else None
    )

    probes: dict[str, Probe] = {}
    if directory is not None:
        probes["postgres"] = directory.ping
    if redis_client is not None:
        probes["redis"] = guards.ping
    if express is not None:
        probes["express"] = express.ping
    if spring is not None:
        probes["spring"] = spring.ping

    async def startup() -> None:
        if directory is not None:
            await directory.open()  # doesn't wait for a connection: a DB outage isn't fatal

    async def shutdown() -> None:
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
        express=express,
        spring=spring,
        llm_factory=lambda: make_llm(settings),
        health=HealthChecker(probes, timeout=3.0),
        startup=startup,
        shutdown=shutdown,
    )
