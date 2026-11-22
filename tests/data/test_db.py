import os

import pytest
from psycopg import AsyncConnection

from receptionist.data.db import PatientDirectory

URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

EMAILS = ("t3-a@test.local", "t3-b@test.local", "t3-doc@test.local", "t3-off@test.local")


@pytest.fixture
async def seeded():
    # Only ever point TEST_DATABASE_URL at a disposable database.
    async with await AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS auth")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY DEFAULT "
            "gen_random_uuid(), email text, full_name text, phone varchar(20), "
            "role text, enabled bool)"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS app.patient_profiles "
            "(user_id uuid PRIMARY KEY, date_of_birth date)"
        )
        await conn.execute("DELETE FROM auth.users WHERE email = ANY(%s)", (list(EMAILS),))
        rows = [
            (EMAILS[0], "Asha Rao", "98765 43210", "PATIENT", True),
            (EMAILS[1], "Asha Rao Jr", "+91 9876543210", "PATIENT", True),
            (EMAILS[2], "Dr Rao", "9876543210", "DOCTOR", True),
            (EMAILS[3], "Off Rao", "9876543210", "PATIENT", False),
        ]
        for r in rows:
            await conn.execute(
                "INSERT INTO auth.users (email, full_name, phone, role, enabled) "
                "VALUES (%s,%s,%s,%s,%s)",
                r,
            )
        await conn.execute(
            "INSERT INTO app.patient_profiles (user_id, date_of_birth) "
            "SELECT id, DATE '1990-05-17' FROM auth.users WHERE email = %s",
            (EMAILS[0],),
        )
        yield
        await conn.execute(
            "DELETE FROM app.patient_profiles WHERE user_id IN "
            "(SELECT id FROM auth.users WHERE email = ANY(%s))",
            (list(EMAILS),),
        )
        await conn.execute("DELETE FROM auth.users WHERE email = ANY(%s)", (list(EMAILS),))


async def test_find_by_phone(seeded):
    directory = PatientDirectory(URL)
    await directory.open()
    try:
        assert await directory.ping() is True
        found = await directory.find_by_phone("+919876543210")
        by_email = {c.email: c for c in found}
        assert set(by_email) == {EMAILS[0], EMAILS[1]}
        assert str(by_email[EMAILS[0]].dob) == "1990-05-17"
        assert by_email[EMAILS[1]].dob is None
        assert isinstance(by_email[EMAILS[0]].id, str)
    finally:
        await directory.close()
