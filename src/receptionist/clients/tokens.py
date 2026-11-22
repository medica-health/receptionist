"""Mints short-lived patient JWTs that express-api accepts (same shape spring-api issues)."""

import time
from typing import Protocol

import jwt

from receptionist.config import Settings

TOKEN_TTL_SECONDS = 300


class TokenIssuer(Protocol):
    def patient_token(self, user_id: str, email: str, name: str) -> str: ...


class LocalJwtIssuer:
    def __init__(
        self, secret: str, issuer: str, audience: str, ttl: int = TOKEN_TTL_SECONDS
    ) -> None:
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl

    @classmethod
    def from_settings(cls, settings: Settings) -> LocalJwtIssuer:
        return cls(settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience)

    def patient_token(self, user_id: str, email: str, name: str) -> str:
        now = int(time.time())
        claims = {
            "sub": user_id,
            "email": email,
            "name": name,
            "role": "PATIENT",
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "exp": now + self._ttl,
        }
        return jwt.encode(claims, self._secret, algorithm="HS256")
