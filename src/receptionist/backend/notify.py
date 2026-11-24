"""Appointment notifications, ported from express-api's notifications/sms/mailer services.

Phone-registered patients (placeholder email) get an SMS through Twilio. Everyone else gets
an email through Resend, and the doctor is emailed about new, moved and cancelled bookings.
Every attempt is recorded in app.notifications_log. Without credentials a message is only
logged and recorded as SKIPPED. Nothing here raises: a notification problem must never fail
the booking that triggered it.
"""

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from receptionist.config import Settings
from receptionist.identity.phone import mask_phone

log = logging.getLogger(__name__)

Event = Literal["BOOKED", "CANCELLED", "RESCHEDULED"]
Status = Literal["SENT", "FAILED", "SKIPPED"]

PHONE_PLACEHOLDER_DOMAIN = "phone.medicare.invalid"

_SUBJECTS: dict[str, str] = {
    "BOOKED": "Appointment confirmed",
    "CANCELLED": "Appointment cancelled",
    "RESCHEDULED": "Appointment rescheduled",
}

_LOAD_SQL = """
SELECT a.id::text, a.start_at, c.timezone, c.name AS clinic_name,
       c.address_line || ', ' || c.city AS clinic_address,
       p.id::text AS patient_id, p.full_name AS patient_name, p.email::text AS patient_email,
       p.phone AS patient_phone,
       d.id::text AS doctor_id, d.full_name AS doctor_name, d.email::text AS doctor_email,
       a.cancel_reason
  FROM app.appointments a
  JOIN app.clinics c ON c.id = a.clinic_id
  JOIN auth.users p ON p.id = a.patient_id
  JOIN auth.users d ON d.id = a.doctor_id
 WHERE a.id = %s
"""

_LOG_SQL = """
INSERT INTO app.notifications_log
  (user_id, appointment_id, type, channel, recipient, subject, status, error, provider_message_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def is_phone_placeholder(email: str) -> bool:
    return email.strip().lower().endswith(f"@{PHONE_PLACEHOLDER_DOMAIN}")


def format_when(start_at: datetime, tz: str) -> str:
    """Like Intl.DateTimeFormat("en-IN", {dateStyle: "full", timeStyle: "short"})."""
    d = start_at.astimezone(ZoneInfo(tz))
    hour = d.hour % 12 or 12
    return f"{d:%A}, {d.day} {d:%B %Y} at {hour}:{d:%M} {'am' if d.hour < 12 else 'pm'}"


@dataclass
class Delivery:
    status: Status
    provider_message_id: str | None = None
    error: str | None = None


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class Notifier:
    def __init__(
        self, pool: AsyncConnectionPool, http: httpx.AsyncClient, settings: Settings
    ) -> None:
        self._pool = pool
        self._http = http
        self._s = settings
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ public

    def fire(self, appointment_id: str, event: Event) -> None:
        """Sends in the background (the caller shouldn't wait on Twilio or Resend)."""
        task = asyncio.create_task(self._safe(appointment_id, event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def notify(
        self, appointment_id: str, event: Event, patient_only: bool = False
    ) -> Status | None:
        """Sends the patient (and, unless patient_only, the doctor) notification. Returns the
        patient's delivery status, or None when the appointment wasn't found."""
        async with self._pool.connection(timeout=3.0) as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(_LOAD_SQL, (appointment_id,))
            a = await cur.fetchone()
        if a is None:
            return None
        when = format_when(a["start_at"], a["timezone"])
        where = f"{a['clinic_name']}, {a['clinic_address']}"
        subject = _SUBJECTS[event]
        doctor, patient = a["doctor_name"], a["patient_name"]
        patient_lines = {
            "BOOKED": [
                f"Hi {patient},",
                f"Your appointment with Dr. {doctor} is booked for {when}.",
                f"Location: {where}",
            ],
            "RESCHEDULED": [
                f"Hi {patient},",
                f"Your appointment with Dr. {doctor} has moved to {when}.",
                f"Location: {where}",
            ],
            "CANCELLED": [
                f"Hi {patient},",
                f"Your appointment with Dr. {doctor} on {when} has been cancelled.",
                *([f"Reason: {a['cancel_reason']}"] if a["cancel_reason"] else []),
            ],
        }[event]
        sms_body = {
            "BOOKED": f"MediCare: Appointment with Dr. {doctor} on {when} at "
            f"{a['clinic_name']} is booked.",
            "RESCHEDULED": f"MediCare: Appointment with Dr. {doctor} has moved to {when} at "
            f"{a['clinic_name']}.",
            "CANCELLED": f"MediCare: Appointment with Dr. {doctor} on {when} is cancelled.",
        }[event]
        kind = f"APPOINTMENT_{event}"

        if is_phone_placeholder(a["patient_email"]):
            patient_job = self._sms(
                a["patient_phone"] or "", sms_body, kind, subject, a["patient_id"], a["id"]
            )
        else:
            patient_job = self._mail(
                a["patient_email"],
                subject,
                patient_lines,
                ("View appointment", f"/patient/appointments/{a['id']}"),
                kind,
                a["patient_id"],
                a["id"],
            )
        jobs = [patient_job]
        if not patient_only:
            doctor_lines = [
                f"Dr. {doctor},",
                f"{patient}'s appointment on {when} was {event.lower()}.",
            ]
            jobs.append(
                self._mail(
                    a["doctor_email"],
                    f"{subject}: {patient}",
                    doctor_lines,
                    ("Open schedule", f"/doctor/appointments/{a['id']}"),
                    f"{kind}_DOCTOR",
                    a["doctor_id"],
                    a["id"],
                )
            )
        results = await asyncio.gather(*jobs)
        return results[0].status

    async def drain(self, timeout: float = 5.0) -> None:
        """Waits briefly for background notifications at shutdown."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # ------------------------------------------------------------------ channels

    async def _safe(self, appointment_id: str, event: Event) -> None:
        try:
            await self.notify(appointment_id, event)
        except Exception as exc:
            log.error("notification failed: %s", type(exc).__name__)

    async def _sms(
        self, to: str, body: str, kind: str, subject: str, user_id: str, appt_id: str
    ) -> Delivery:
        result = await self._deliver_sms(to, body) if to else Delivery("SKIPPED", error="no phone")
        await self._log(user_id, appt_id, kind, "SMS", to, subject, result)
        return result

    async def _deliver_sms(self, to: str, body: str, attempts: int = 3) -> Delivery:
        s = self._s
        if not s.twilio_account_sid or not s.twilio_auth_token:
            log.info("[sms skipped: Twilio credentials not set] to=%s", mask_phone(to))
            return Delivery("SKIPPED")
        form = {"To": to, "Body": body}
        if s.twilio_messaging_service_sid:
            form["MessagingServiceSid"] = s.twilio_messaging_service_sid
        else:
            form["From"] = s.twilio_sms_from or ""
        url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
        auth = (s.twilio_account_sid, s.twilio_auth_token)
        last = "unknown error"
        for attempt in range(1, attempts + 1):
            try:
                res = await self._http.post(url, data=form, auth=auth)
                data = _json(res)
                if res.is_success and data.get("sid"):
                    return Delivery("SENT", data["sid"])
                code = f" {data['code']}" if data.get("code") else ""
                last = f"Twilio {res.status_code}{code}: {data.get('message', 'request failed')}"
                if not _retryable(res.status_code):
                    break
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            if attempt < attempts:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
        log.error("SMS to %s failed: %s", mask_phone(to), last)
        return Delivery("FAILED", error=last)

    async def _mail(
        self,
        to: str,
        subject: str,
        lines: list[str],
        cta: tuple[str, str],
        kind: str,
        user_id: str,
        appt_id: str,
    ) -> Delivery:
        if is_phone_placeholder(to):
            result = Delivery("SKIPPED", error="placeholder address")
        else:
            result = await self._deliver_mail(to, subject, lines, cta, kind)
        await self._log(user_id, appt_id, kind, "EMAIL", to, subject, result)
        return result

    async def _deliver_mail(
        self,
        to: str,
        subject: str,
        lines: list[str],
        cta: tuple[str, str],
        kind: str,
        attempts: int = 3,
    ) -> Delivery:
        s = self._s
        if not s.resend_api_key:
            log.info("[mail skipped: RESEND_API_KEY not set] subject=%r", subject)
            return Delivery("SKIPPED")
        body: dict[str, Any] = {
            "from": s.mail_from,
            "to": [to],
            "subject": subject,
            "html": _layout(subject, lines, cta, s.app_base_url),
            "text": "\n".join(lines),
            "tags": [{"name": "type", "value": kind}],
        }
        if s.mail_reply_to:
            body["reply_to"] = s.mail_reply_to
        headers = {"Authorization": f"Bearer {s.resend_api_key}"}
        last = "unknown error"
        for attempt in range(1, attempts + 1):
            try:
                res = await self._http.post(
                    "https://api.resend.com/emails", json=body, headers=headers
                )
                data = _json(res)
                if res.is_success and data.get("id"):
                    return Delivery("SENT", data["id"])
                last = f"Resend {res.status_code}: {data.get('message', 'request failed')}"
                if not _retryable(res.status_code):
                    break
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            if attempt < attempts:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
        log.error("email failed: %s", last)
        return Delivery("FAILED", error=last)

    async def _log(
        self,
        user_id: str,
        appt_id: str,
        kind: str,
        channel: str,
        recipient: str,
        subject: str,
        result: Delivery,
    ) -> None:
        try:
            async with self._pool.connection(timeout=3.0) as conn:
                await conn.execute(
                    _LOG_SQL,
                    (
                        user_id,
                        appt_id,
                        kind,
                        channel,
                        recipient,
                        subject,
                        result.status,
                        result.error,
                        result.provider_message_id,
                    ),
                )
        except Exception as exc:
            log.error("notifications_log insert failed: %s", type(exc).__name__)


def _json(res: httpx.Response) -> dict[str, Any]:
    try:
        data = res.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _layout(title: str, paragraphs: list[str], cta: tuple[str, str], base_url: str) -> str:
    """Same minimal branded layout as express-api's mailer."""
    body = "".join(f'<p style="margin:0 0 12px">{html.escape(p)}</p>' for p in paragraphs)
    label, path = cta
    button = (
        f'<p><a href="{base_url}{path}" style="display:inline-block;background:#0f766e;color:#fff;'
        f'padding:10px 18px;border-radius:6px;text-decoration:none">{html.escape(label)}</a></p>'
    )
    return (
        '<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#1f2937">'
        f'<h2 style="color:#0f766e">{html.escape(title)}</h2>{body}{button}'
        '<p style="color:#6b7280;font-size:12px;margin-top:24px">This is an automated message '
        "from MediCare.</p></div>"
    )
