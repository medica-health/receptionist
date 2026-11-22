import asyncio
import sys

from fastapi.testclient import TestClient
from langchain_anthropic import ChatAnthropic

from receptionist.app import AppDeps, create_app
from receptionist.clients.express import ExpressClient
from receptionist.clients.spring import SpringClient
from receptionist.config import Settings
from receptionist.data.db import PatientDirectory
from receptionist.data.guards import Guards
from receptionist.deps import build_deps


def full_settings() -> Settings:
    return Settings(
        database_url="postgresql://u:p@127.0.0.1:1/none",
        redis_url="redis://127.0.0.1:1/0",
        express_api_url="http://127.0.0.1:1",
        spring_api_url="http://127.0.0.1:1",
        api_key="k",
    )


async def test_build_deps_wires_real_clients():
    deps = build_deps(full_settings())
    try:
        assert isinstance(deps.directory, PatientDirectory)
        assert isinstance(deps.guards, Guards)
        assert isinstance(deps.express, ExpressClient)
        assert isinstance(deps.spring, SpringClient)
        llm = deps.llm_factory()
        assert isinstance(llm, ChatAnthropic)
        assert llm.model == "claude-opus-5-5"
    finally:
        await deps.shutdown()


def test_health_reports_down_for_unreachable_dependencies():
    # Nothing listens on port 1, and the pool isn't opened: every probe reports "down".
    deps = build_deps(full_settings())
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None

    async def check():
        try:
            return await deps.health.status()
        finally:
            await deps.shutdown()

    assert asyncio.run(check(), loop_factory=loop_factory) == {
        "postgres": "down",
        "redis": "down",
        "express": "down",
        "spring": "down",
    }


async def test_unconfigured_dependencies_are_disabled():
    deps = build_deps(Settings())
    try:
        assert deps.directory is None and deps.express is None and deps.spring is None
        assert set((await deps.health.status()).values()) == {"disabled"}
    finally:
        await deps.shutdown()


def test_lifespan_runs_startup_and_shutdown():
    events = []

    async def startup():
        events.append("startup")

    async def shutdown():
        events.append("shutdown")

    with TestClient(create_app(AppDeps(startup=startup, shutdown=shutdown))) as client:
        assert events == ["startup"]
        assert client.get("/").status_code == 200
    assert events == ["startup", "shutdown"]
