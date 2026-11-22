"""Relay tokens: the signed webhook vouches for a call's CallSid and caller number to the
relay socket. Twilio's `setup` message itself is unsigned, so anything taken from it could be
forged by whoever opens a socket; the token is minted only in the signed `/twilio/voice`
response and travels back to us as a ConversationRelay `<Parameter>`.

token = base64url(payload) + "." + base64url(HMAC-SHA256(key, payload)),
payload = "callSid|from_e164_or_empty|exp_unix".
"""

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

TTL_SECONDS = 300


@dataclass(frozen=True)
class RelayClaims:
    call_sid: str
    caller_e164: str | None
    exp: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: str, payload: bytes) -> bytes:
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()


def mint_relay_token(
    call_sid: str,
    caller_e164: str | None,
    key: str,
    *,
    now: float | None = None,
    ttl: int = TTL_SECONDS,
) -> str:
    if not key:
        raise ValueError("relay token key is not configured")
    exp = int(now if now is not None else time.time()) + ttl
    payload = f"{call_sid}|{caller_e164 or ''}|{exp}".encode()
    return f"{_b64(payload)}.{_b64(_mac(key, payload))}"


def verify_relay_token(
    token: str, key: str, *, call_sid: str | None = None, now: float | None = None
) -> RelayClaims | None:
    """The claims of a genuine, unexpired token (minted for `call_sid`, when given), else
    None."""
    if not token or not key or not isinstance(token, str):
        return None
    try:
        encoded_payload, encoded_mac = token.split(".")
        payload = _unb64(encoded_payload)
        mac = _unb64(encoded_mac)
    except ValueError:
        return None
    if not hmac.compare_digest(mac, _mac(key, payload)):
        return None
    try:
        sid, caller, exp_text = payload.decode("utf-8").split("|")
        exp = int(exp_text)
    except ValueError:
        return None
    if not sid or exp <= int(now if now is not None else time.time()):
        return None
    if call_sid is not None and sid != call_sid:  # not secret; the MAC is checked above
        return None
    return RelayClaims(sid, caller or None, exp)
