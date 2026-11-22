"""Twilio endpoints: the voice webhook, the call status callback and the relay WebSocket."""

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from receptionist.app import envelope
from receptionist.identity.phone import mask_phone, normalize_phone
from receptionist.session import CallSession
from receptionist.voice.relay import RelayConnection, release_pending_hold
from receptionist.voice.twilio_auth import public_url, verify_twilio_signature
from receptionist.voice.twiml import build_connect_twiml, build_reject_twiml

log = logging.getLogger(__name__)

VOICE_PATH = "/twilio/voice"
STATUS_PATH = "/twilio/status"
RELAY_PATH = "/twilio/relay"

REJECT_EN = "We're receiving too many calls from this number. Please try again later."
REJECT_HI = "इस नंबर से बहुत ज़्यादा कॉल आ रही हैं। कृपया थोड़ी देर बाद फिर से कोशिश करें।"
ENDED_STATUSES = frozenset({"completed", "failed", "no-answer", "busy", "canceled"})

router = APIRouter()


def init_state(app: Any) -> None:
    """Per-app call state the routes share."""
    app.state.draining = False
    app.state.calls = {}  # CallSid -> CallSession, while its relay socket is open
    app.state.busy_calls = set()  # CallSids with a turn running right now
    app.state.relays = set()  # open RelayConnections (graceful drain waits for these)
    app.state.session_factory = CallSession.start


def _envelope_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(envelope(False, message), status_code=status)


async def _signed_params(request: Request, path: str) -> dict[str, str] | None:
    """The form parameters when the request carries a valid Twilio signature, else None."""
    settings = request.app.state.settings
    body = (await request.body()).decode("latin-1")
    params = dict(parse_qsl(body, keep_blank_values=True, encoding="utf-8"))
    url = public_url(settings, path, request.url.query)
    signature = request.headers.get("x-twilio-signature", "")
    if verify_twilio_signature(url, params, signature, settings.twilio_auth_token or ""):
        return params
    log.warning("rejected a request without a valid Twilio signature path=%s", path)
    return None


def _xml(twiml: str) -> Response:
    return Response(twiml, media_type="text/xml")


@router.post(VOICE_PATH)
async def voice(request: Request) -> Response:
    params = await _signed_params(request, VOICE_PATH)
    if params is None:
        return _envelope_error(403, "Invalid signature")
    state = request.app.state
    if state.draining:
        return _envelope_error(503, "Draining")
    settings = state.settings
    guards = state.deps.guards
    call_sid = params.get("CallSid", "")
    e164 = normalize_phone(params.get("From"))
    # Withheld or invalid caller ID still connects (general information only), unthrottled
    # because there is no number to count against.
    if e164 is not None and guards is not None and not await guards.register_call(e164):
        log.info("call rejected: rate limit call=%s caller=%s", call_sid, mask_phone(e164))
        return _xml(build_reject_twiml(REJECT_EN, REJECT_HI))
    if guards is not None and call_sid:
        meta = {"from": e164, "started_at": datetime.now(UTC).isoformat()}
        await guards.set_call_meta(call_sid, meta)
    ws_url = public_url(settings, RELAY_PATH, websocket=True)
    return _xml(build_connect_twiml(ws_url, settings, {"callSid": call_sid}))


@router.post(STATUS_PATH)
async def status(request: Request) -> Response:
    params = await _signed_params(request, STATUS_PATH)
    if params is None:
        return _envelope_error(403, "Invalid signature")
    state = request.app.state
    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "")
    if call_status in ENDED_STATUSES:
        session = state.calls.get(call_sid)
        # A running turn may be using the hold (booking it); the relay releases any hold
        # left once that turn finishes and the socket closes.
        if session is not None and call_sid not in state.busy_calls:
            await release_pending_hold(session)
        log.info("call ended call=%s status=%s", call_sid, call_status)
    return Response(status_code=204)


@router.websocket(RELAY_PATH)
async def relay(ws: WebSocket) -> None:
    state = ws.app.state
    settings = state.settings
    signature = ws.headers.get("x-twilio-signature", "")
    token = settings.twilio_auth_token or ""
    query = ws.url.query
    # Twilio signs the handshake URL; accept its wss:// form and the https:// form.
    urls = (public_url(settings, RELAY_PATH, query, websocket=w) for w in (True, False))
    if not any(verify_twilio_signature(url, {}, signature, token) for url in urls):
        log.warning("rejected a relay socket without a valid Twilio signature")
        await ws.close(code=1008)
        return
    await ws.accept()
    connection = RelayConnection(
        ws,
        state.deps,
        session_factory=state.session_factory,
        registry=state.calls,
        busy=state.busy_calls,
    )
    state.relays.add(connection)
    try:
        await connection.run()
    finally:
        state.relays.discard(connection)
