"""Graceful drain on SIGTERM, so a rolling deploy doesn't hang up on live calls.

Plain uvicorn closes every WebSocket (code 1012) as soon as it starts shutting down. Instead,
the first SIGTERM only marks the app draining: `/twilio/voice`, `/ready` and `/api/health`
return 503, so the load balancer stops sending new calls, while open relay sockets carry on.
The server exits once no call is left (after at least `min_seconds`, giving the load balancer
time to see the 503s), or after `max_seconds` at most. A second SIGTERM, or SIGINT, exits now.
The pod's terminationGracePeriodSeconds must exceed `max_seconds` plus uvicorn's
`timeout_graceful_shutdown`.
"""

import logging
import signal
import time
from collections.abc import Callable
from types import FrameType
from typing import Any

import uvicorn

log = logging.getLogger(__name__)

DRAIN_MIN_SECONDS = 30.0
DRAIN_MAX_SECONDS = 780.0


class DrainingServer(uvicorn.Server):
    def __init__(
        self,
        config: uvicorn.Config,
        app: Any,
        *,
        min_seconds: float = DRAIN_MIN_SECONDS,
        max_seconds: float = DRAIN_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(config)
        self._app = app
        self._min = min_seconds
        self._max = max_seconds
        self._clock = clock
        self._drain_started: float | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if sig == signal.SIGTERM and self._drain_started is None and not self.should_exit:
            self._drain_started = self._clock()
            self._app.state.draining = True
            log.info("SIGTERM: draining; %d call(s) open", len(self._app.state.relays))
            return
        super().handle_exit(sig, frame)

    async def on_tick(self, counter: int) -> bool:
        if self._drain_started is not None and not self.should_exit:
            elapsed = self._clock() - self._drain_started
            if elapsed >= self._max:
                log.warning("drain timed out with %d call(s) open", len(self._app.state.relays))
                self.should_exit = True
            elif elapsed >= self._min and not self._app.state.relays:
                self.should_exit = True
        return await super().on_tick(counter)
