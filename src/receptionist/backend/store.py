"""The receptionist's own data access over the shared Postgres and Redis.

This replaces the calls it used to make to express-api (doctors, slots, bookings,
prescriptions) and spring-api (phone-patient registration). The SQL and rules are ported
from express-api's services (doctors, appointments, clinics, patients, prescriptions) and
spring-api's PhonePatientService. Keep them in step with those: the web app reads and writes
the same rows. Results use the same JSON shapes the APIs returned, so the tools stayed the
same.
"""

import hashlib
import logging
import secrets
from datetime import UTC, date, datetime
from typing import Any, Literal

import bcrypt
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from receptionist.backend.errors import BadRequest, Conflict, NotFound, db_errors
from receptionist.backend.holds import SlotHolds
from receptionist.backend.notify import Notifier
from receptionist.backend.policies import (
    Slot,
    assert_patient_can_cancel,
    assert_patient_can_change,
    filter_available,
    is_active,
)

log = logging.getLogger(__name__)

PHONE_EMAIL_DOMAIN = "@phone.medicare.invalid"
SEARCH_RADIUS_KM = 10  # express-api's default radiusKm for the saved-location fallback
_POOL_WAIT = 3.0  # seconds to wait for a pooled connection, so an outage can't stall a call


def _jsonable(value: Any) -> Any:
    """datetime -> ISO string, date -> YYYY-MM-DD, recursively (what the APIs' JSON held)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise BadRequest(400, "datetime must be timezone-aware")
    return value


# ---------------------------------------------------------------------- SQL

_SPECIALIZATIONS_SQL = """
SELECT s.id, s.name::text AS name, s.description
  FROM app.specializations s ORDER BY s.name
"""

_CLINICS_SQL = """
SELECT c.id::text, c.name, c.address_line, c.city, c.state, c.pincode, c.country, c.phone,
       c.timezone
  FROM app.clinics c
 WHERE c.name ILIKE %(q)s
 ORDER BY c.name
 LIMIT %(limit)s
"""

_DOCTOR_SQL = """
SELECT dp.user_id::text AS id, u.full_name AS name, dp.specialization_id,
       s.name::text AS specialization, dp.qualifications, dp.experience_years,
       dp.consultation_fee::float AS consultation_fee, dp.bio, dp.languages,
       dp.avg_rating::float AS avg_rating, dp.rating_count
  FROM app.doctor_profiles dp
  JOIN auth.users u ON u.id = dp.user_id
  LEFT JOIN app.specializations s ON s.id = dp.specialization_id
 WHERE dp.user_id = %s AND dp.verification_status = 'APPROVED' AND u.enabled
"""

_DOCTOR_CLINICS_SQL = """
SELECT c.id::text, c.name, c.address_line, c.city, c.state, c.pincode, c.country, c.phone,
       c.timezone
  FROM app.doctor_clinics dc JOIN app.clinics c ON c.id = dc.clinic_id
 WHERE dc.doctor_id = %s ORDER BY c.name
"""

_SAVED_LOCATION_SQL = """
SELECT ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lng
  FROM app.patient_profiles WHERE user_id = %s AND location IS NOT NULL
"""

_CANDIDATE_SLOTS_SQL = """
SELECT gs AS start_at, gs + make_interval(mins => a.slot_minutes) AS end_at, a.clinic_id::text
  FROM app.doctor_availability a
  JOIN app.clinics c ON c.id = a.clinic_id
  JOIN app.doctor_clinics dc ON dc.doctor_id = a.doctor_id AND dc.clinic_id = a.clinic_id
 CROSS JOIN LATERAL generate_series(
         (%(date)s::date + a.start_time) AT TIME ZONE c.timezone,
         (%(date)s::date + a.end_time) AT TIME ZONE c.timezone - make_interval(mins => a.slot_minutes),
         make_interval(mins => a.slot_minutes)) AS gs
 WHERE a.doctor_id = %(doctor)s
   AND a.is_active
   AND a.day_of_week = extract(dow FROM %(date)s::date)
   AND (%(clinic)s::uuid IS NULL OR a.clinic_id = %(clinic)s::uuid)
"""

_BUSY_SQL = """
SELECT start_at, end_at FROM app.appointments
 WHERE doctor_id = %(doctor)s AND status NOT IN ('CANCELLED', 'NO_SHOW')
   AND start_at < %(to)s AND end_at > %(from)s
UNION ALL
SELECT starts_at, ends_at FROM app.doctor_time_off
 WHERE doctor_id = %(doctor)s AND starts_at < %(to)s AND ends_at > %(from)s
"""

_APPOINTMENT_SELECT = """
SELECT a.id::text, a.status, a.start_at, a.end_at, a.reason, a.cancel_reason, a.created_at,
       a.patient_id::text, a.doctor_id::text, d.full_name AS doctor_name,
       s.name::text AS doctor_specialization,
       a.clinic_id::text, c.name AS clinic_name, c.address_line AS clinic_address,
       c.city AS clinic_city, c.timezone AS clinic_timezone
  FROM app.appointments a
  JOIN auth.users d ON d.id = a.doctor_id
  JOIN app.doctor_profiles dp ON dp.user_id = a.doctor_id
  LEFT JOIN app.specializations s ON s.id = dp.specialization_id
  JOIN app.clinics c ON c.id = a.clinic_id
"""

_RX_SELECT = """
SELECT rx.id::text, rx.appointment_id::text, rx.diagnosis, rx.notes, rx.advice,
       rx.follow_up_date, rx.created_at,
       rx.doctor_id::text, d.full_name AS doctor_name, s.name::text AS doctor_specialization,
       rx.patient_id::text, a.start_at AS appointment_start_at, c.name AS clinic_name,
       COALESCE((SELECT json_agg(json_build_object(
                  'medicine', i.medicine, 'dosage', i.dosage, 'frequency', i.frequency,
                  'duration', i.duration, 'instructions', i.instructions) ORDER BY i.position)
                 FROM app.prescription_items i WHERE i.prescription_id = rx.id), '[]'::json) AS items
  FROM app.prescriptions rx
  JOIN auth.users d ON d.id = rx.doctor_id
  JOIN app.doctor_profiles dp ON dp.user_id = rx.doctor_id
  LEFT JOIN app.specializations s ON s.id = dp.specialization_id
  JOIN app.appointments a ON a.id = rx.appointment_id
  JOIN app.clinics c ON c.id = a.clinic_id
"""


class Backend:
    """Everything the call tools need, straight from Postgres and Redis."""

    def __init__(self, pool: AsyncConnectionPool, holds: SlotHolds, notifier: Notifier) -> None:
        self._pool = pool
        self.holds = holds
        self.notifier = notifier

    # ------------------------------------------------------------------ plumbing

    async def _all(self, sql: str, params: Any = None, conn: AsyncConnection | None = None):
        with db_errors():
            if conn is not None:
                cur = await conn.cursor(row_factory=dict_row).execute(sql, params)
                return await cur.fetchall()
            async with self._pool.connection(timeout=_POOL_WAIT) as c:
                cur = await c.cursor(row_factory=dict_row).execute(sql, params)
                return await cur.fetchall()

    async def _one(self, sql: str, params: Any = None, conn: AsyncConnection | None = None):
        rows = await self._all(sql, params, conn)
        return rows[0] if rows else None

    def as_patient(self, user_id: str, email: str, name: str) -> PatientSession:
        return PatientSession(self, user_id)

    async def ping(self) -> bool:
        try:
            await self._one("SELECT 1 AS ok")
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------ general info

    async def specializations(self) -> list[dict]:
        return _jsonable(await self._all(_SPECIALIZATIONS_SQL))

    async def search_clinics(self, q: str, limit: int = 1) -> dict:
        rows = await self._all(_CLINICS_SQL, {"q": f"%{q}%", "limit": limit})
        return {"items": _jsonable(rows)}

    async def search_doctors(
        self,
        specialization_id: int | None = None,
        city: str | None = None,
        pincode: str | None = None,
        language: str | None = None,
        limit: int = 5,
        viewer_id: str | None = None,
    ) -> dict:
        """Approved doctors, one row per doctor at their nearest matching clinic. A patient
        without a place filter is searched around their saved location (as express-api does)."""
        where = ["dp.verification_status = 'APPROVED'", "u.enabled"]
        params: dict[str, Any] = {"limit": limit}
        distance = "NULL::float"
        if viewer_id and not city and not pincode:
            saved = await self._one(_SAVED_LOCATION_SQL, (viewer_id,))
            if saved:
                params.update(lat=saved["lat"], lng=saved["lng"], radius=SEARCH_RADIUS_KM * 1000)
                point = "ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography"
                distance = f"ST_Distance(c.location, {point}) / 1000.0"
                where.append(f"ST_DWithin(c.location, {point}, %(radius)s)")
        if city:
            where.append("lower(c.city) = lower(%(city)s)")
            params["city"] = city
        if pincode:
            where.append("c.pincode = %(pincode)s")
            params["pincode"] = pincode
        if specialization_id:
            where.append("dp.specialization_id = %(spec)s")
            params["spec"] = specialization_id
        if language:
            where.append("%(language)s ILIKE ANY (dp.languages)")
            params["language"] = language
        order = (
            "distance_km ASC, avg_rating DESC"
            if distance != "NULL::float"
            else "avg_rating DESC, rating_count DESC"
        )
        sql = f"""
        WITH matches AS (
          SELECT DISTINCT ON (dp.user_id)
                 dp.user_id::text AS id, u.full_name AS name, s.id AS specialization_id,
                 s.name::text AS specialization, dp.experience_years,
                 dp.consultation_fee::float AS consultation_fee, dp.languages,
                 dp.avg_rating::float AS avg_rating, dp.rating_count,
                 c.id::text AS clinic_id, c.name AS clinic_name, c.address_line AS clinic_address,
                 c.city AS clinic_city, c.state AS clinic_state, c.pincode AS clinic_pincode,
                 {distance} AS distance_km
            FROM app.doctor_profiles dp
            JOIN auth.users u ON u.id = dp.user_id
            LEFT JOIN app.specializations s ON s.id = dp.specialization_id
            JOIN app.doctor_clinics dc ON dc.doctor_id = dp.user_id
            JOIN app.clinics c ON c.id = dc.clinic_id
           WHERE {" AND ".join(where)}
           ORDER BY dp.user_id, distance_km ASC NULLS LAST
        )
        SELECT * FROM matches ORDER BY {order} LIMIT %(limit)s
        """
        rows = await self._all(sql, params)
        items = [
            {
                "id": r["id"],
                "name": r["name"],
                "specialization": r["specialization"],
                "specializationId": r["specialization_id"],
                "experienceYears": r["experience_years"],
                "consultationFee": r["consultation_fee"],
                "languages": r["languages"],
                "avgRating": r["avg_rating"],
                "ratingCount": r["rating_count"],
                "distanceKm": None
                if r["distance_km"] is None
                else round(float(r["distance_km"]), 1),
                "nearestClinic": {
                    "id": r["clinic_id"],
                    "name": r["clinic_name"],
                    "address": r["clinic_address"],
                    "city": r["clinic_city"],
                    "state": r["clinic_state"],
                    "pincode": r["clinic_pincode"],
                },
            }
            for r in rows
        ]
        return {"items": items}

    async def doctor(self, doctor_id: str) -> dict:
        """An approved, enabled doctor with their clinics. NotFound otherwise."""
        profile = await self._one(_DOCTOR_SQL, (doctor_id,))
        if profile is None:
            raise NotFound(404, "Doctor not found")
        clinics = await self._all(_DOCTOR_CLINICS_SQL, (doctor_id,))
        return _jsonable({**profile, "clinics": clinics})

    # ------------------------------------------------------------------ slots

    async def candidate_slots(
        self, doctor_id: str, day: str, clinic_id: str | None, conn: AsyncConnection | None = None
    ) -> list[Slot]:
        rows = await self._all(
            _CANDIDATE_SLOTS_SQL, {"doctor": doctor_id, "date": day, "clinic": clinic_id}, conn
        )
        return [Slot(r["start_at"], r["end_at"], r["clinic_id"]) for r in rows]

    async def busy(
        self, doctor_id: str, start: datetime, end: datetime, conn: AsyncConnection | None = None
    ) -> list[tuple[datetime, datetime]]:
        rows = await self._all(_BUSY_SQL, {"doctor": doctor_id, "from": start, "to": end}, conn)
        return [(r["start_at"], r["end_at"]) for r in rows]

    async def _free_slots(self, doctor_id: str, day: str, clinic_id: str | None) -> list[Slot]:
        candidates = await self.candidate_slots(doctor_id, day, clinic_id)
        if not candidates:
            return []
        start = min(s.start_at for s in candidates)
        end = max(s.end_at for s in candidates)
        return filter_available(candidates, await self.busy(doctor_id, start, end))

    async def slots(
        self, doctor_id: str, day: date, clinic_id: str | None = None, viewer_id: str | None = None
    ) -> list[dict]:
        """Free slots on a local date, each flagged `held` when someone other than the viewer
        holds it."""
        approved = await self._one(
            "SELECT 1 AS ok FROM app.doctor_profiles "
            "WHERE user_id = %s AND verification_status = 'APPROVED'",
            (doctor_id,),
        )
        if approved is None:
            raise NotFound(404, "Doctor not found")
        iso = day.isoformat()
        free = await self.holds.cached_slots(
            doctor_id, iso, clinic_id, lambda: self._free_slots(doctor_id, iso, clinic_id)
        )
        if not free:
            return []
        start = free[0].start_at
        end = max(s.end_at for s in free)
        others = [
            h for h in await self.holds.held(doctor_id, start, end) if h.holder_id != viewer_id
        ]
        out = []
        for s in free:
            hold = next(
                (h for h in others if h.start_at < s.end_at and s.start_at < h.end_at), None
            )
            out.append(
                {
                    "startAt": s.start_at.astimezone(UTC).isoformat(),
                    "endAt": s.end_at.astimezone(UTC).isoformat(),
                    "clinicId": s.clinic_id,
                    "held": hold is not None,
                    "heldUntil": hold.expires_at.isoformat() if hold else None,
                }
            )
        return out

    async def resolve_slot(
        self, conn: AsyncConnection | None, doctor_id: str, clinic_id: str, start_at: datetime
    ) -> Slot:
        """`start_at` must be exactly one of the doctor's generated slots at that clinic, in the
        future and outside time off. Overlaps are left to the exclusion constraints."""
        if start_at <= datetime.now(UTC):
            raise BadRequest(400, "The appointment time must be in the future")
        clinic = await self._one(
            """SELECT to_char((%(start)s::timestamptz AT TIME ZONE c.timezone)::date,
                              'YYYY-MM-DD') AS local_date
                 FROM app.clinics c
                 JOIN app.doctor_clinics dc ON dc.clinic_id = c.id AND dc.doctor_id = %(doctor)s
                WHERE c.id = %(clinic)s""",
            {"start": start_at, "clinic": clinic_id, "doctor": doctor_id},
            conn,
        )
        if clinic is None:
            raise BadRequest(400, "The doctor does not practise at this clinic")
        slots = await self.candidate_slots(doctor_id, clinic["local_date"], clinic_id, conn)
        slot = next((s for s in slots if s.start_at == start_at), None)
        if slot is None:
            raise BadRequest(400, "The selected time is not one of the doctor's available slots")
        off = await self._one(
            "SELECT 1 AS ok FROM app.doctor_time_off "
            "WHERE doctor_id = %s AND starts_at < %s AND ends_at > %s LIMIT 1",
            (doctor_id, slot.end_at, slot.start_at),
            conn,
        )
        if off is not None:
            raise Conflict(409, "The doctor is unavailable at this time")
        return slot

    async def assert_bookable_doctor(self, doctor_id: str, conn: AsyncConnection | None = None):
        row = await self._one(
            "SELECT dp.verification_status, u.enabled FROM app.doctor_profiles dp "
            "JOIN auth.users u ON u.id = dp.user_id WHERE dp.user_id = %s",
            (doctor_id,),
            conn,
        )
        if row is None or not row["enabled"] or row["verification_status"] != "APPROVED":
            raise NotFound(404, "Doctor not found")

    # ------------------------------------------------------------------ registration

    async def create_phone_patient(
        self, full_name: str, phone_e164: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Registers a phone caller as a PATIENT (what spring-api's PhonePatientService did).
        Idempotent on the key: the placeholder email is derived from it, so a retry returns
        the same user even if the name differs."""
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        email = f"p-{digest}{PHONE_EMAIL_DOMAIN}"
        # A random password nobody knows: phone patients can't sign in until they reset it.
        password_hash = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt()).decode()
        row = await self._one(
            """INSERT INTO auth.users (email, password_hash, full_name, phone, role, enabled,
                                       email_verified)
               VALUES (%s, %s, %s, %s, 'PATIENT', true, false)
               ON CONFLICT (email) DO NOTHING
               RETURNING id::text, email, full_name""",
            (email, password_hash, full_name.strip(), phone_e164),
        )
        if row is None:
            row = await self._one(
                "SELECT id::text, email, full_name FROM auth.users WHERE email = %s", (email,)
            )
            if row is None:
                raise Conflict(409, "Registration conflicted; please try again")
        return {"id": row["id"], "email": row["email"], "fullName": row["full_name"]}


class PatientSession:
    """Reads and writes as one verified patient. Every query is scoped to `user_id`."""

    def __init__(self, backend: Backend, user_id: str) -> None:
        self._b = backend
        self.user_id = user_id

    # ------------------------------------------------------------------ profile

    async def update_profile(self, date_of_birth: date) -> None:
        with db_errors():
            async with self._b._pool.connection(timeout=_POOL_WAIT) as conn:
                await conn.execute(
                    """INSERT INTO app.patient_profiles (user_id, date_of_birth)
                       VALUES (%s, %s)
                       ON CONFLICT (user_id)
                       DO UPDATE SET date_of_birth = EXCLUDED.date_of_birth, updated_at = now()""",
                    (self.user_id, date_of_birth),
                )

    # ------------------------------------------------------------------ appointments

    async def list_appointments(self, scope: Literal["upcoming", "past"], limit: int = 5) -> dict:
        now = datetime.now(UTC)
        if scope == "upcoming":
            sql = (
                f"{_APPOINTMENT_SELECT} WHERE a.patient_id = %s "
                "AND a.status IN ('BOOKED', 'CONFIRMED') AND a.start_at >= %s "
                "ORDER BY a.start_at ASC LIMIT %s"
            )
        else:
            sql = (
                f"{_APPOINTMENT_SELECT} WHERE a.patient_id = %s AND a.start_at < %s "
                "ORDER BY a.start_at DESC LIMIT %s"
            )
        return {"items": _jsonable(await self._b._all(sql, (self.user_id, now, limit)))}

    async def search_doctors(
        self,
        specialization_id: int | None = None,
        city: str | None = None,
        pincode: str | None = None,
        language: str | None = None,
        limit: int = 5,
    ) -> dict:
        return await self._b.search_doctors(
            specialization_id, city, pincode, language, limit, viewer_id=self.user_id
        )

    async def hold(self, doctor_id: str, clinic_id: str, start_at: datetime) -> dict:
        b = self._b
        await b.assert_bookable_doctor(doctor_id)
        slot = await b.resolve_slot(None, doctor_id, clinic_id, _aware(start_at))
        if await b.busy(doctor_id, slot.start_at, slot.end_at):
            raise Conflict(409, "This time slot is no longer available")
        hold = await b.holds.create(self.user_id, doctor_id, slot)
        return {
            "holdId": hold.hold_id,
            "doctorId": doctor_id,
            "clinicId": clinic_id,
            "startAt": hold.start_at.isoformat(),
            "endAt": hold.end_at.isoformat(),
            "expiresAt": hold.expires_at.isoformat(),
        }

    async def release_hold(self, hold_id: str) -> None:
        if not await self._b.holds.release(self.user_id, hold_id):
            raise NotFound(404, "Hold not found or already expired")

    async def _after_slot_taken(self, doctor_id: str, start_at: datetime) -> None:
        hold = await self._b.holds.current(self.user_id)
        if hold and hold.doctor_id == doctor_id and hold.start_at == start_at:
            await self._b.holds.release(self.user_id, hold.hold_id)
        await self._b.holds.invalidate(doctor_id)

    async def book(
        self, doctor_id: str, clinic_id: str, start_at: datetime, reason: str | None = None
    ) -> dict:
        b = self._b
        start_at = _aware(start_at)
        with db_errors():
            async with b._pool.connection(timeout=_POOL_WAIT) as conn, conn.transaction():
                patient = await b._one(
                    "SELECT 1 AS ok FROM auth.users WHERE id = %s AND role = 'PATIENT' AND enabled",
                    (self.user_id,),
                    conn,
                )
                if patient is None:
                    raise NotFound(404, "Patient not found")
                await b.assert_bookable_doctor(doctor_id, conn)
                slot = await b.resolve_slot(conn, doctor_id, clinic_id, start_at)
                await b.holds.assert_not_held_by_others(doctor_id, slot, self.user_id)
                await conn.execute(
                    "INSERT INTO app.patient_profiles (user_id) VALUES (%s) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (self.user_id,),
                )
                row = await b._one(
                    """INSERT INTO app.appointments
                         (patient_id, doctor_id, clinic_id, booked_by, start_at, end_at, reason)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id::text, status""",
                    (
                        self.user_id,
                        doctor_id,
                        clinic_id,
                        self.user_id,
                        slot.start_at,
                        slot.end_at,
                        reason,
                    ),
                    conn,
                )
        assert row is not None
        await self._after_slot_taken(doctor_id, slot.start_at)
        b.notifier.fire(row["id"], "BOOKED")
        log.info("appointment booked id=%s", row["id"])
        return {"id": row["id"], "status": row["status"]}

    async def _load_own(self, conn: AsyncConnection, appointment_id: str, lock: bool) -> dict:
        row = await self._b._one(
            "SELECT id::text, patient_id::text, doctor_id::text, clinic_id::text, start_at, "
            f"end_at, status FROM app.appointments WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (appointment_id,),
            conn,
        )
        if row is None or row["patient_id"] != self.user_id:
            raise NotFound(404, "Appointment not found")
        return row

    async def cancel(self, appointment_id: str, reason: str | None = None) -> dict:
        b = self._b
        with db_errors():
            async with b._pool.connection(timeout=_POOL_WAIT) as conn, conn.transaction():
                appt = await self._load_own(conn, appointment_id, lock=True)
                assert_patient_can_cancel(appt["status"], appt["start_at"])
                await conn.execute(
                    """UPDATE app.appointments
                          SET status = 'CANCELLED', cancel_reason = %s, cancelled_by = %s,
                              cancelled_at = now(), updated_at = now()
                        WHERE id = %s""",
                    (reason, self.user_id, appointment_id),
                )
        await b.holds.invalidate(appt["doctor_id"])
        b.notifier.fire(appointment_id, "CANCELLED")
        log.info("appointment cancelled id=%s", appointment_id)
        return {"id": appointment_id, "status": "CANCELLED"}

    async def reschedule(
        self, appointment_id: str, start_at: datetime, clinic_id: str | None = None
    ) -> dict:
        b = self._b
        start_at = _aware(start_at)
        with db_errors():
            async with b._pool.connection(timeout=_POOL_WAIT) as conn, conn.transaction():
                appt = await self._load_own(conn, appointment_id, lock=True)
                assert_patient_can_change(appt["start_at"])
                if not is_active(appt["status"]):
                    raise Conflict(409, f"A {appt['status']} appointment cannot be rescheduled")
                target_clinic = clinic_id or appt["clinic_id"]
                slot = await b.resolve_slot(conn, appt["doctor_id"], target_clinic, start_at)
                await b.holds.assert_not_held_by_others(appt["doctor_id"], slot, self.user_id)
                # Moving resets confirmation and the reminder.
                await conn.execute(
                    """UPDATE app.appointments
                          SET start_at = %s, end_at = %s, clinic_id = %s, status = 'BOOKED',
                              reminder_sent_at = NULL, updated_at = now()
                        WHERE id = %s""",
                    (slot.start_at, slot.end_at, target_clinic, appointment_id),
                )
        await self._after_slot_taken(appt["doctor_id"], slot.start_at)
        b.notifier.fire(appointment_id, "RESCHEDULED")
        log.info("appointment rescheduled id=%s", appointment_id)
        return {"id": appointment_id, "status": "BOOKED"}

    async def resend_confirmation(self, appointment_id: str) -> dict:
        with db_errors():
            async with self._b._pool.connection(timeout=_POOL_WAIT) as conn:
                appt = await self._load_own(conn, appointment_id, lock=False)
        if not is_active(appt["status"]):
            raise Conflict(409, "Only upcoming appointments can be re-confirmed")
        try:
            sent = await self._b.notifier.notify(appointment_id, "BOOKED", patient_only=True)
        except Exception as exc:
            log.error("resend confirmation failed: %s", type(exc).__name__)
            sent = None
        return {"appointmentId": appointment_id, "sent": sent == "SENT"}

    # ------------------------------------------------------------------ prescriptions

    async def prescriptions(self, patient_id: str, limit: int = 3) -> dict:
        if patient_id != self.user_id:
            raise NotFound(404, "Not found")
        rows = await self._b._all(
            f"{_RX_SELECT} WHERE rx.patient_id = %s ORDER BY rx.created_at DESC LIMIT %s",
            (self.user_id, limit),
        )
        return {"items": _jsonable(rows)}

    async def prescription(self, prescription_id: str) -> dict:
        rx = await self._b._one(f"{_RX_SELECT} WHERE rx.id = %s", (prescription_id,))
        if rx is None or rx["patient_id"] != self.user_id:
            raise NotFound(404, "Prescription not found")
        return _jsonable(rx)
