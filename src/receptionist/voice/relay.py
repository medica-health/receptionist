"""The ConversationRelay WebSocket: one connection is one call.

Twilio sends `setup`, then `prompt`s (caller speech, transcribed), `interrupt`s (barge-in),
`dtmf` and `error`. We answer with `text` tokens, `language` switches and `end`.

One worker task per connection runs prompts in order, one at a time. A graph run is never
cancelled, so a write tool is never cut off mid-call: on `interrupt` the rest of that turn is
muted, and once the run finishes the reply is trimmed to what the caller heard.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import anyio
from starlette.websockets import WebSocket

from receptionist.identity.phone import mask_phone
from receptionist.voice.relay_token import verify_relay_token
from receptionist.voice.twiml import LANG_CODES

log = logging.getLogger(__name__)

# After the caller hangs up, how long a turn already running may take to finish before it is
# torn down (its graph run is closed, cancelling any tool still in flight).
FINISH_TIMEOUT = 60.0

SessionFactory = Callable[[str, str | None, Any], Awaitable[Any]]


@dataclass
class _Prompt:
    text: str
    lang: str | None


_WAKE = object()  # wakes an idle worker so it applies a pending trim
_STOP = object()


async def release_pending_hold(session: Any) -> None:
    """Best effort: give back a slot held for this call. It also expires on its own."""
    ctx = session.ctx
    hold = ctx.pending_hold
    if hold is None or not hold.hold_id:
        return
    ctx.pending_hold = None
    try:
        await ctx.patient().release_hold(hold.hold_id)
    except Exception as exc:
        log.warning("hold release failed call=%s: %s", ctx.call_sid, type(exc).__name__)


def _error_code(description: Any) -> str:
    """Twilio error descriptions may quote our own text (which may read back a DOB), so
    only the numeric code is logged."""
    match = re.search(r"\b(\d{5})\b", str(description or ""))
    return match.group(1) if match else "unknown"


class RelayConnection:
    def __init__(
        self,
        ws: WebSocket,
        deps: Any,
        *,
        session_factory: SessionFactory,
        registry: dict[str, Any],
        busy: set[str],
        token_key: str,
    ) -> None:
        self.ws = ws
        self.deps = deps
        self.session: Any = None
        self.call_sid: str | None = None
        self._factory = session_factory
        self._registry = registry
        self._busy = busy
        self._token_key = token_key
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        self._ended = False
        self._running = False
        self._muted = False
        self._pending_trim: str | None = None

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        """Read messages until the socket closes, then stop the worker and clean up.
        Expects an accepted socket. Never raises."""
        worker = asyncio.create_task(self._work())
        try:
            while not self._closed:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    log.warning("ignored a non-JSON relay message call=%s", self.call_sid)
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except Exception as exc:
            log.warning("relay read failed call=%s: %s", self.call_sid, type(exc).__name__)
        finally:
            self._closed = True
            # Shielded: even if this handler is cancelled, the hold is released and the
            # registry cleared.
            with anyio.CancelScope(shield=True):
                await self._stop(worker)
                await self._cleanup()

    async def _stop(self, worker: asyncio.Task) -> None:
        while not self._queue.empty():  # nobody is listening to queued prompts any more
            self._queue.get_nowait()
        self._queue.put_nowait(_STOP)
        try:
            done, _ = await asyncio.wait({worker}, timeout=FINISH_TIMEOUT)
            if not done:
                log.warning("turn still running after hang-up call=%s; closing it", self.call_sid)
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        except Exception as exc:
            log.warning("relay worker stop failed call=%s: %s", self.call_sid, type(exc).__name__)

    async def _cleanup(self) -> None:
        if self.session is None:
            return
        try:
            await release_pending_hold(self.session)
        finally:
            if self.call_sid and self._registry.get(self.call_sid) is self.session:
                del self._registry[self.call_sid]
            log.info("relay closed call=%s", self.call_sid)

    # ------------------------------------------------------------ incoming

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "setup":
            await self._on_setup(msg)
        elif kind == "prompt":
            self._on_prompt(msg)
        elif kind == "interrupt":
            await self._on_interrupt(msg)
        elif kind == "error":
            log.warning(
                "conversationrelay error call=%s code=%s",
                self.call_sid,
                _error_code(msg.get("description")),
            )
        # dtmf (detection is off) and unknown types are ignored.

    async def _on_setup(self, msg: dict[str, Any]) -> None:
        if self.session is not None:
            return
        # The setup message is unsigned: the CallSid and caller number come only from the
        # relay token our signed webhook minted (setup's own `from` is ignored).
        custom = msg.get("customParameters")
        custom = custom if isinstance(custom, dict) else {}
        call_sid = msg.get("callSid")
        token = custom.get("relayToken")
        claims = (
            verify_relay_token(token, self._token_key, call_sid=call_sid)
            if isinstance(token, str) and isinstance(call_sid, str)
            else None
        )
        if claims is None:
            log.warning("relay setup without a valid relay token; closing")
            await self._close(1008)
            return
        if claims.call_sid in self._registry:
            log.warning("relay token reused call=%s; closing", claims.call_sid)
            await self._close(1008)
            return
        call_sid, e164 = claims.call_sid, claims.caller_e164
        try:
            session = await self._factory(call_sid, e164, self.deps)
        except Exception as exc:
            log.error("call setup failed call=%s: %s", call_sid, type(exc).__name__)
            await self._close(1011)
            return
        self.session, self.call_sid = session, call_sid
        self._registry[call_sid] = session
        log.info(
            "relay connected call=%s caller=%s", call_sid, mask_phone(e164) if e164 else "withheld"
        )

    def _on_prompt(self, msg: dict[str, Any]) -> None:
        if self.session is None or self._ended or msg.get("last") is not True:
            return
        text = msg.get("voicePrompt")
        if isinstance(text, str) and text.strip():
            lang = msg.get("lang")
            self._queue.put_nowait(_Prompt(text, lang if isinstance(lang, str) else None))

    async def _on_interrupt(self, msg: dict[str, Any]) -> None:
        if self.session is None:
            return
        heard = msg.get("utteranceUntilInterrupt")
        heard = heard if isinstance(heard, str) else ""
        if self._running:
            if self._muted:
                return  # already cut off; nothing more was spoken after the first interrupt
            self._muted = True
            self._pending_trim = heard
        else:
            # The reply finished generating but was still being spoken.
            self._pending_trim = heard
            self._queue.put_nowait(_WAKE)

    # ------------------------------------------------------------ worker

    async def _work(self) -> None:
        while True:
            await self._apply_trim()
            item = await self._queue.get()
            if item is _STOP:
                return
            if item is _WAKE or self._closed:
                continue
            await self._apply_trim()
            await self._turn(item)

    async def _apply_trim(self) -> None:
        heard, self._pending_trim = self._pending_trim, None
        if heard is None or self.session is None:
            return
        try:
            await self.session.trim_last_reply(heard)
        except Exception as exc:
            log.warning("trim failed call=%s: %s", self.call_sid, type(exc).__name__)

    async def _turn(self, prompt: _Prompt) -> None:
        self._running, self._muted = True, False
        assert self.call_sid is not None
        self._busy.add(self.call_sid)
        spoke = end = False
        try:
            async with aclosing(self.session.respond(prompt.text, prompt.lang)) as speech:
                async for piece in speech:
                    if piece.kind in ("token", "filler"):
                        text = f"{piece.text} " if piece.kind == "filler" else piece.text
                        if text and not self._muted and await self._send_text(text, last=False):
                            spoke = True
                    elif piece.kind == "switch_language":
                        code = LANG_CODES.get(
                            piece.lang or self.session.ctx.language.current, "en-IN"
                        )
                        await self._send(
                            {"type": "language", "ttsLanguage": code, "transcriptionLanguage": code}
                        )
                    elif piece.kind == "end":
                        end = True
        except Exception as exc:
            # CallSession handles graph failures itself; this is a last resort. The caller
            # still gets the end of the talk cycle below.
            log.warning("turn failed call=%s: %s", self.call_sid, type(exc).__name__)
        finally:
            self._running = False
            self._busy.discard(self.call_sid)
        if spoke and not self._muted:
            await self._send_text("", last=True)
        if end:
            # Even after a barge-in: the call was ended in the graph, so leaving it open
            # would only leave the caller in silence.
            self._ended = True
            await self._send({"type": "end"})

    # ------------------------------------------------------------ outgoing

    async def _send_text(self, token: str, *, last: bool) -> bool:
        lang = LANG_CODES.get(self.session.ctx.language.current, "en-IN")
        return await self._send({"type": "text", "token": token, "last": last, "lang": lang})

    async def _send(self, payload: dict[str, Any]) -> bool:
        if self._closed:
            return False
        try:
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            self._closed = True  # the socket went away; never try again
            return False

    async def _close(self, code: int) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.ws.close(code=code)
        except Exception:
            pass
