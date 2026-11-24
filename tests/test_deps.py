import asyncio
import sys

from fastapi.testclient import TestClient
from langchain_anthropic import ChatAnthropic

from receptionist.app import AppDeps, create_app
from receptionist.backend.store import Backend
from receptionist.config import Settings
from receptionist.data.db import PatientDirectory
from receptionist.data.guards import Guards
from receptionist.deps import build_deps


def full_settings() -> Settings:
    return Settings(
        database_url="postgresql://u:p@127.0.0.1:1/none",
        redis_url="redis://127.0.0.1:1/0",
    )


async def test_build_deps_wires_real_clients():
    deps = build_deps(full_settings())
    try:
        assert isinstance(deps.directory, PatientDirectory)
        assert isinstance(deps.guards, Guards)
        assert isinstance(deps.backend, Backend)
        kwargs = deps.guards._r.connection_pool.connection_kwargs
        assert kwargs["socket_connect_timeout"] == 2
        assert kwargs["socket_timeout"] == 1
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
    }


async def test_unconfigured_dependencies_are_disabled():
    deps = build_deps(Settings())
    try:
        assert deps.directory is None and deps.backend is None
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


class RecordingPool:
    def __init__(self):
        self.timeouts = []

    def connection(self, timeout=None):
        self.timeouts.append(timeout)
        raise TimeoutError("pool timeout")


async def test_directory_waits_briefly_for_a_connection():
    directory = PatientDirectory("postgresql://u:p@127.0.0.1:1/none")
    directory._pool = RecordingPool()
    try:
        await directory.find_by_phone("+919876543210")
    except TimeoutError:
        pass
    assert await directory.ping() is False
    assert directory._pool.timeouts == [3.0, 2.5]
