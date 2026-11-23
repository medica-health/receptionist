"""Client for spring-api's internal endpoints."""

from typing import Any

import httpx

from receptionist.clients._http import send
from receptionist.clients.errors import ApiError


class SpringClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._http = http

    async def create_phone_patient(
        self, full_name: str, phone_e164: str, idempotency_key: str
    ) -> dict[str, Any]:
        return await send(
            self._http,
            "POST",
            f"{self._base}/api/internal/phone-patients",
            headers={"X-Api-Key": self._key},
            json={
                "fullName": full_name,
                "phoneE164": phone_e164,
                "idempotencyKey": idempotency_key,
            },
        )

    async def ping(self) -> bool:
        try:
            await send(self._http, "GET", f"{self._base}/api/health")
        except ApiError:
            return False
        return True
