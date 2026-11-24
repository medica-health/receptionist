"""Backend against a real Postgres with the full schema (spring-api's V1 plus express-api's
migrations). Point TEST_DATABASE_URL only at a disposable database."""

import os
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import fakeredis
import httpx
import pytest
from psycopg import AsyncConnection

from receptionist.backend.errors import BadRequest, Conflict, NotFound
from receptionist.backend.holds import SlotHolds
from receptionist.backend.notify import Notifier
from receptionist.backend.store import Backend
from receptionist.config import Settings
from receptionist.data.db import PatientDirectory

URL = os.getenv("TEST_DATABASE_URL")  # seeds the fixtures (owner / superuser)
# Optional: the backend connects as this instead, e.g. the receptionist_app role, so the test
# also proves the role's grants (express-api migration 005) cover every query.
APP_URL = os.getenv("TEST_RECEPTIONIST_DATABASE_URL") or URL
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime.now(IST).date() + timedelta(days=3)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=IST).astimezone(UTC)


def phone() -> str:
    return "+9197" + "".join(random.choices("0123456789", k=8))


@pytest.fixture
async def world():
    tag = uuid.uuid4().hex[:8]
    async with await AsyncConnection.connect(URL, autocommit=True) as conn:

        async def user(name: str, role: str, number: str | None = None) -> str:
            email = f"{name.lower().replace(' ', '.')}.{tag}@test.local"
            cur = await conn.execute(
                "INSERT INTO auth.users (email, password_hash, full_name, phone, role) "
                "VALUES (%s, 'x', %s, %s, %s) RETURNING id::text",
                (email, name, number, role),
            )
            return (await cur.fetchone())[0]

        doctor = await user("Meera Rao", "DOCTOR")
        p1 = await user("Asha Rao", "PATIENT", phone())
        p2 = await user("Ravi Kumar", "PATIENT", phone())
        cur = await conn.execute(
            "INSERT INTO app.specializations (name, description) VALUES (%s, 'Heart') "
            "RETURNING id",
            (f"Cardiology {tag}",),
        )
        spec = (await cur.fetchone())[0]
        city = f"Pune{tag}"
        cur = await conn.execute(
            "INSERT INTO app.clinics (name, address_line, city, state, pincode, location, "
            "created_by) VALUES (%s, '12 MG Road', %s, 'MH', '411001', "
            "ST_SetSRID(ST_MakePoint(73.85, 18.52), 4326)::geography, %s) RETURNING id::text",
            (f"City Heart {tag}", city, doctor),
        )
        clinic = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO app.doctor_profiles (user_id, specialization_id, languages, "
            "verification_status) VALUES (%s, %s, '{English,Hindi}', 'APPROVED')",
            (doctor, spec),
        )
        await conn.execute(
            "INSERT INTO app.doctor_clinics (doctor_id, clinic_id) VALUES (%s, %s)",
            (doctor, clinic),
        )
        for dow in range(7):
            await conn.execute(
                "INSERT INTO app.doctor_availability (doctor_id, clinic_id, day_of_week, "
                "start_time, end_time, slot_minutes) VALUES (%s, %s, %s, '09:00', '11:00', 30)",
                (doctor, clinic, dow),
            )

        directory = PatientDirectory(APP_URL)
        await directory.open()
        redis = fakeredis.FakeAsyncRedis()
        http = httpx.AsyncClient()
        notifier = Notifier(directory.pool, http, Settings())
        backend = Backend(directory.pool, SlotHolds(redis, 300, 60), notifier)
        try:
            yield {
                "b": backend,
                "doctor": doctor,
                "clinic": clinic,
                "spec": spec,
                "city": city,
                "p1": p1,
                "p2": p2,
                "conn": conn,
                "directory": directory,
                "tag": tag,
            }
        finally:
            await notifier.drain()
            await directory.close()
            await http.aclose()
            await redis.aclose()


async def test_general_info(world):
    b = world["b"]
    assert any(s["id"] == world["spec"] for s in await b.specializations())
    found = await b.search_doctors(
        specialization_id=world["spec"], city=world["city"], language="hindi"
    )
    assert [d["id"] for d in found["items"]] == [world["doctor"]]
    assert found["items"][0]["nearestClinic"]["id"] == world["clinic"]
    assert (await b.search_doctors(city=world["city"], language="Tamil"))["items"] == []
    doc = await b.doctor(world["doctor"])
    assert doc["name"] == "Meera Rao" and doc["clinics"][0]["timezone"] == "Asia/Kolkata"
    clinics = await b.search_clinics(f"heart {world['tag']}")
    assert clinics["items"][0]["id"] == world["clinic"]
    with pytest.raises(NotFound):
        await b.doctor(str(uuid.uuid4()))
    with pytest.raises(BadRequest):
        await b.doctor("d1")  # not a uuid


async def test_slots_hold_book_reschedule_cancel(world):
    b, doctor, clinic = world["b"], world["doctor"], world["clinic"]
    me, other = b.as_patient(world["p1"], "", ""), b.as_patient(world["p2"], "", "")

    slots = await b.slots(doctor, DAY)
    assert [s["startAt"] for s in slots] == [
        at(h, m).isoformat() for h, m in ((9, 0), (9, 30), (10, 0), (10, 30))
    ]

    hold = await me.hold(doctor, clinic, at(9, 30))
    assert hold["holdId"]
    seen = {s["startAt"]: s["held"] for s in await b.slots(doctor, DAY, viewer_id=world["p2"])}
    assert seen[at(9, 30).isoformat()] is True
    mine = {s["startAt"]: s["held"] for s in await b.slots(doctor, DAY, viewer_id=world["p1"])}
    assert mine[at(9, 30).isoformat()] is False
    with pytest.raises(Conflict):
        await other.hold(doctor, clinic, at(9, 30))
    with pytest.raises(Conflict):
        await other.book(doctor, clinic, at(9, 30))  # held by someone else

    booked = await me.book(doctor, clinic, at(9, 30), "chest pain")
    assert booked["status"] == "BOOKED"
    assert await b.holds.current(world["p1"]) is None  # booking released the hold
    assert at(9, 30).isoformat() not in {s["startAt"] for s in await b.slots(doctor, DAY)}

    upcoming = (await me.list_appointments("upcoming"))["items"]
    assert [a["id"] for a in upcoming] == [booked["id"]]
    assert upcoming[0]["clinic_timezone"] == "Asia/Kolkata"
    assert (await other.list_appointments("upcoming"))["items"] == []

    with pytest.raises(BadRequest, match="not one of"):
        await me.book(doctor, clinic, at(9, 10))
    with pytest.raises(BadRequest, match="future"):
        await me.book(doctor, clinic, datetime.now(UTC) - timedelta(hours=1))

    # With no hold in the way, Postgres' exclusion constraint still stops a double booking.
    with pytest.raises(Conflict, match="no longer available"):
        await other.book(doctor, clinic, at(9, 30))

    with pytest.raises(NotFound):
        await other.cancel(booked["id"])
    await me.reschedule(booked["id"], at(10, 30))
    upcoming = (await me.list_appointments("upcoming"))["items"]
    assert upcoming[0]["start_at"] == at(10, 30).isoformat()

    await me.cancel(booked["id"], "feeling better")
    with pytest.raises(Conflict, match="already CANCELLED"):
        await me.cancel(booked["id"])
    with pytest.raises(Conflict):
        await me.reschedule(booked["id"], at(9, 0))
    with pytest.raises(Conflict):
        await me.resend_confirmation(booked["id"])
    assert at(10, 30).isoformat() in {s["startAt"] for s in await b.slots(doctor, DAY)}

    await b.notifier.drain()
    cur = await world["conn"].execute(
        "SELECT type, channel, status FROM app.notifications_log WHERE appointment_id = %s",
        (booked["id"],),
    )
    rows = set(await cur.fetchall())
    for kind in ("BOOKED", "RESCHEDULED", "CANCELLED"):
        assert (f"APPOINTMENT_{kind}", "EMAIL", "SKIPPED") in rows
        assert (f"APPOINTMENT_{kind}_DOCTOR", "EMAIL", "SKIPPED") in rows


async def test_resend_confirmation_and_prescriptions(world):
    b, doctor, clinic, conn = world["b"], world["doctor"], world["clinic"], world["conn"]
    me, other = b.as_patient(world["p1"], "", ""), b.as_patient(world["p2"], "", "")
    appt = await me.book(doctor, clinic, at(10, 0))
    assert (await me.resend_confirmation(appt["id"]))["sent"] is False  # no credentials
    with pytest.raises(NotFound):
        await other.resend_confirmation(appt["id"])

    cur = await conn.execute(
        "INSERT INTO app.prescriptions (appointment_id, doctor_id, patient_id, diagnosis) "
        "VALUES (%s, %s, %s, 'Hypertension') RETURNING id::text",
        (appt["id"], doctor, world["p1"]),
    )
    rx_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO app.prescription_items (prescription_id, medicine, dosage, frequency, "
        "duration, position) VALUES (%s, 'Amlodipine 5 mg', '1 tablet', 'daily', '30 days', 0)",
        (rx_id,),
    )
    listed = (await me.prescriptions(world["p1"]))["items"]
    assert [r["id"] for r in listed] == [rx_id]
    assert listed[0]["items"][0]["medicine"] == "Amlodipine 5 mg"
    assert (await me.prescription(rx_id))["doctor_name"] == "Meera Rao"
    with pytest.raises(NotFound):
        await other.prescription(rx_id)
    with pytest.raises(NotFound):
        await other.prescriptions(world["p1"])


async def test_phone_registration_is_idempotent_and_findable(world):
    b, directory = world["b"], world["directory"]
    number = phone()
    key = f"CA{world['tag']}"
    first = await b.create_phone_patient("  Rohan Gupta ", number, key)
    again = await b.create_phone_patient("Someone Else", number, key)
    assert first == again
    assert first["email"].endswith("@phone.medicare.invalid")
    assert first["fullName"] == "Rohan Gupta"
    await b.as_patient(first["id"], "", "").update_profile(date(1995, 3, 4))
    found = await directory.find_by_phone(number)
    assert [(c.id, c.dob) for c in found] == [(first["id"], date(1995, 3, 4))]
    cur = await world["conn"].execute(
        "SELECT left(password_hash, 2), role, email_verified FROM auth.users WHERE id = %s",
        (first["id"],),
    )
    assert await cur.fetchone() == ("$2", "PATIENT", False)
