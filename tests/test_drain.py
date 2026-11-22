import signal

import uvicorn

from receptionist.app import AppDeps, create_app
from receptionist.drain import DrainingServer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(min_seconds: float = 30, max_seconds: float = 780):
    app = create_app(AppDeps())
    clock = Clock()
    server = DrainingServer(
        uvicorn.Config(app),
        app,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        clock=clock,
    )
    return app, server, clock


async def test_sigterm_starts_draining_without_exiting():
    app, server, _ = make()
    server.handle_exit(signal.SIGTERM, None)
    assert app.state.draining is True
    assert server.should_exit is False
    assert await server.on_tick(1) is False


async def test_exits_once_no_calls_are_left_after_min_drain():
    app, server, clock = make()
    app.state.relays.add(object())
    server.handle_exit(signal.SIGTERM, None)
    clock.now += 31
    assert await server.on_tick(1) is False  # a live call keeps the pod up
    app.state.relays.clear()
    assert await server.on_tick(2) is True


async def test_waits_min_drain_even_without_calls():
    _, server, clock = make()
    server.handle_exit(signal.SIGTERM, None)
    clock.now += 5
    assert await server.on_tick(1) is False
    clock.now += 30
    assert await server.on_tick(2) is True


async def test_gives_up_after_max_drain():
    app, server, clock = make()
    app.state.relays.add(object())
    server.handle_exit(signal.SIGTERM, None)
    clock.now += 781
    assert await server.on_tick(1) is True


def test_second_sigterm_or_sigint_exits_now():
    _, server, _ = make()
    server.handle_exit(signal.SIGTERM, None)
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit is True
    _, server, _ = make()
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit is True
