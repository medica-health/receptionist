"""Twilio request signatures. The signed URL is always rebuilt from PUBLIC_BASE_URL, never
from the request's Host header, which a client controls."""

from twilio.request_validator import RequestValidator

from receptionist.config import Settings


def verify_twilio_signature(url: str, params: dict, signature: str, auth_token: str) -> bool:
    if not signature or not auth_token:
        return False
    return RequestValidator(auth_token).validate(url, params, signature)


def public_url(settings: Settings, path: str, query: str = "", websocket: bool = False) -> str:
    """`PUBLIC_BASE_URL` + `path` (+ `?query`); `websocket` turns https into wss."""
    base = settings.public_base_url.rstrip("/")
    if websocket:
        if base.startswith("https://"):
            base = "wss://" + base.removeprefix("https://")
        elif base.startswith("http://"):
            base = "ws://" + base.removeprefix("http://")
    return f"{base}{path}?{query}" if query else f"{base}{path}"
