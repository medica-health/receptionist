"""Dependency health reporting for /api/health."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

DependencyStatus = Literal["up", "down", "disabled"]
DEPENDENCIES = ("postgres", "redis")

Probe = Callable[[], Awaitable[bool]]


class HealthChecker:
    """Runs one probe per dependency; a dependency without a probe is "disabled"."""

    def __init__(self, probes: dict[str, Probe] | None = None, timeout: float = 2.0) -> None:
        self._probes = probes or {}
        self._timeout = timeout

    async def _run(self, probe: Probe) -> DependencyStatus:
        try:
            return "up" if await asyncio.wait_for(probe(), self._timeout) else "down"
        except Exception:
            return "down"

    async def status(self) -> dict[str, DependencyStatus]:
        async def one(name: str) -> DependencyStatus:
            probe = self._probes.get(name)
            return "disabled" if probe is None else await self._run(probe)

        results = await asyncio.gather(*(one(n) for n in DEPENDENCIES))
        return dict(zip(DEPENDENCIES, results, strict=True))
