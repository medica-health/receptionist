"""Client for express-api. Patient calls go through a PatientSession that mints its own JWT."""

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx

from receptionist.clients._http import send
from receptionist.clients.errors import ApiError
from receptionist.clients.tokens import TOKEN_TTL_SECONDS, TokenIssuer

_REMINT_MARGIN = 30


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat()


class ExpressClient:
    def __init__(
        self,
        base_url: str,
        issuer: TokenIssuer,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._issuer = issuer
        self._http = http
        self._clock = clock

    async def call(
        self,
        method: str,
        path: str,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if token:
            kwargs["headers"] = {"Authorization": f"Bearer {token}"}
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if json is not None:
            kwargs["json"] = json
        return await send(self._http, method, f"{self._base}/api{path}", **kwargs)

    def as_patient(self, user_id: str, email: str, name: str) -> PatientSession:
        return PatientSession(self, user_id, email, name)

    # Anonymous endpoints.
    async def specializations(self) -> Any:
        return await self.call("GET", "/specializations")

    async def search_doctors(
        self,
        specialization_id: int | None = None,
        city: str | None = None,
        pincode: str | None = None,
        language: str | None = None,
        limit: int = 5,
        token: str | None = None,
    ) -> Any:
        params = {
            "specializationId": specialization_id,
            "city": city,
            "pincode": pincode,
            "language": language,
            "limit": limit,
        }
        return await self.call("GET", "/doctors", token, params)

    async def slots(self, doctor_id: str, date: date, clinic_id: str | None = None) -> Any:
        params = {"date": date.isoformat(), "clinicId": clinic_id}
        return await self.call("GET", f"/doctors/{doctor_id}/slots", params=params)

    async def doctor(self, doctor_id: str) -> Any:
        return await self.call("GET", f"/doctors/{doctor_id}")

    async def ping(self) -> bool:
        try:
            await self.call("GET", "/health")
        except ApiError:
            return False
        return True


class PatientSession:
    """Calls express-api as one verified patient. Mints its token lazily, re-mints near expiry."""

    def __init__(self, client: ExpressClient, user_id: str, email: str, name: str) -> None:
        self._client = client
        self._identity = (user_id, email, name)
        self._token: str | None = None
        self._expires_at = 0.0

    def _bearer(self) -> str:
        now = self._client._clock()
        if self._token is None or now >= self._expires_at - _REMINT_MARGIN:
            self._token = self._client._issuer.patient_token(*self._identity)
            self._expires_at = now + TOKEN_TTL_SECONDS
        return self._token

    async def _call(
        self, method: str, path: str, params: dict[str, Any] | None = None, json: Any = None
    ) -> Any:
        return await self._client.call(method, path, self._bearer(), params, json)

    async def get_profile(self) -> Any:
        return await self._call("GET", "/patients/me/profile")

    async def update_profile(self, date_of_birth: date) -> Any:
        return await self._call(
            "PUT", "/patients/me/profile", json={"dateOfBirth": date_of_birth.isoformat()}
        )

    async def list_appointments(self, scope: Literal["upcoming", "past"], limit: int = 5) -> Any:
        now = datetime.now(UTC).isoformat()
        if scope == "upcoming":
            params = {"status": "BOOKED,CONFIRMED", "from": now, "order": "asc", "limit": limit}
        else:
            params = {"to": now, "order": "desc", "limit": limit}
        return await self._call("GET", "/appointments", params)

    async def hold(self, doctor_id: str, clinic_id: str, start_at: datetime) -> Any:
        body = {"doctorId": doctor_id, "clinicId": clinic_id, "startAt": _iso(start_at)}
        return await self._call("POST", "/appointments/holds", json=body)

    async def release_hold(self, hold_id: str) -> Any:
        return await self._call("DELETE", f"/appointments/holds/{hold_id}")

    async def book(
        self, doctor_id: str, clinic_id: str, start_at: datetime, reason: str | None = None
    ) -> Any:
        body: dict[str, Any] = {
            "doctorId": doctor_id,
            "clinicId": clinic_id,
            "startAt": _iso(start_at),
        }
        if reason:
            body["reason"] = reason
        return await self._call("POST", "/appointments", json=body)

    async def cancel(self, appointment_id: str, reason: str | None = None) -> Any:
        body = {"reason": reason} if reason else {}
        return await self._call("POST", f"/appointments/{appointment_id}/cancel", json=body)

    async def reschedule(
        self, appointment_id: str, start_at: datetime, clinic_id: str | None = None
    ) -> Any:
        body: dict[str, Any] = {"startAt": _iso(start_at)}
        if clinic_id:
            body["clinicId"] = clinic_id
        return await self._call("POST", f"/appointments/{appointment_id}/reschedule", json=body)

    async def prescriptions(self, patient_id: str, limit: int = 3) -> Any:
        return await self._call("GET", f"/patients/{patient_id}/prescriptions", {"limit": limit})

    async def prescription(self, prescription_id: str) -> Any:
        return await self._call("GET", f"/prescriptions/{prescription_id}")

    async def resend_confirmation(self, appointment_id: str) -> Any:
        return await self._call("POST", f"/appointments/{appointment_id}/resend-confirmation")

    async def search_doctors(
        self,
        specialization_id: int | None = None,
        city: str | None = None,
        pincode: str | None = None,
        language: str | None = None,
        limit: int = 5,
    ) -> Any:
        return await self._client.search_doctors(
            specialization_id, city, pincode, language, limit, token=self._bearer()
        )
