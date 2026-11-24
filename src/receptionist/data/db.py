"""Caller lookup against the shared Postgres (auth + app schemas). Owns the connection pool,
which the backend shares."""

from psycopg_pool import AsyncConnectionPool

from receptionist.identity.phone import match_key
from receptionist.identity.verify import Candidate

_SQL = r"""
SELECT u.id::text, u.full_name, u.email, p.date_of_birth
  FROM auth.users u LEFT JOIN app.patient_profiles p ON p.user_id = u.id
 WHERE u.role = 'PATIENT' AND u.enabled
   AND CASE WHEN %(indian)s THEN right(regexp_replace(coalesce(u.phone,''), '\D', '', 'g'), 10)
            ELSE regexp_replace(coalesce(u.phone,''), '\D', '', 'g') END = %(key)s
"""


class PatientDirectory:
    def __init__(self, database_url: str) -> None:
        self._pool = AsyncConnectionPool(database_url, min_size=1, max_size=10, open=False)

    @property
    def pool(self) -> AsyncConnectionPool:
        return self._pool

    async def open(self) -> None:
        await self._pool.open()

    async def close(self) -> None:
        await self._pool.close()

    async def ping(self, timeout: float = 2.5) -> bool:
        """SELECT 1, waiting at most `timeout` seconds for a pooled connection."""
        try:
            async with self._pool.connection(timeout=timeout) as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def find_by_phone(self, e164: str, timeout: float = 3.0) -> list[Candidate]:
        """Candidates on this number. Waits at most `timeout` seconds for a pooled connection,
        so a database outage can't leave the caller in silence."""
        params = {"key": match_key(e164), "indian": e164.startswith("+91")}
        async with self._pool.connection(timeout=timeout) as conn:
            cur = await conn.execute(_SQL, params)
            rows = await cur.fetchall()
        return [Candidate(id=r[0], full_name=r[1], email=r[2], dob=r[3]) for r in rows]
