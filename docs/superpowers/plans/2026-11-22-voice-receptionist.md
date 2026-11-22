# MediCare Voice Receptionist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Twilio phone receptionist in Hindi and English that identifies MediCare patients by
caller ID plus date of birth, registers unknown callers, and books, reschedules and cancels
appointments, lists schedules, suggests doctors and reads prescriptions through the existing APIs.

**Architecture:** A new Python service (`receptionist/`, FastAPI + LangGraph + Claude) sits behind
Twilio ConversationRelay, which exchanges text over a WebSocket. It reads Postgres read-only for
caller matching. It acts as the patient against express-api (short-lived JWT) and creates phone
patients through a new spring-api internal endpoint. express-api gains an SMS channel. Terraform
and Helm add Redis (ElastiCache), secrets and the new app.

**Tech Stack:** Python 3.14 (fall back to 3.13 only if a dependency won't install; record it in
README), uv, FastAPI, uvicorn, langgraph, langchain-anthropic, twilio, psycopg[binary,pool],
redis, httpx, pyjwt, pydantic-settings, rapidfuzz, pytest, pytest-asyncio, ruff. Bun/Express 5/
zod (express-api), Spring Boot 4 / Java 21 (spring-api), Terraform, Helm.

**Spec:** `receptionist/docs/superpowers/specs/2026-11-22-voice-receptionist-design.md`. Read it
before any task.

## Global Constraints

- Every commit is authored by the existing global git identity. **No `Co-Authored-By` or
  "Generated with" lines** in commits or PRs (workspace CLAUDE.md). Each repo works on its own
  branch: receptionist `feature/voice-receptionist`, spring-api `feature/phone-patients`,
  express-api `feature/sms-notifications`, infra `feature/receptionist-infra`, helm
  `feature/receptionist-app`, `.github` `chore/compose-receptionist`.
- Response envelope everywhere: `{ success, message, data, timestamp }`. `GET /` →
  `{"message":"Running"}`, `GET /api/health` reports each dependency `up`/`down`/`disabled`.
- Placeholder email domain: `phone.medicare.invalid`; address form `p-<24 hex>@phone.medicare.invalid`.
- JWT: HS256 over `JWT_SECRET`, claims `sub`, `email`, `name`, `role`, `iss` (`JWT_ISSUER`,
  default `cdac-auth`), `aud` (`JWT_AUDIENCE`, default `cdac-api`), `iat`, `exp`. Receptionist
  tokens: `role=PATIENT` only, TTL 300 s.
- Verification limits: 2 failed attempts per call, 5 failures per number per 24 h → 24 h lock.
  Calls per number per hour: 10.
- Redis key prefix `rcpt:`; keys `rcpt:verify-fail:{e164}` (TTL 86400), `rcpt:calls:{e164}`
  (TTL 3600), `rcpt:call:{CallSid}` (TTL 3600).
- Default model `claude-opus-5-5`, effort `low`, refusal fallback beta
  `server-side-fallback-2026-07-01` with `fallbacks: "default"`. Model from `LLM_MODEL`.
- Timezone for "today"/"tomorrow": `Asia/Kolkata` (`DEFAULT_TIMEZONE`). Appointment times are
  always spoken in the clinic's `timezone`.
- Receptionist port 7000. Outbound HTTP timeout 5 s (connect 2 s).
- Logs never contain DOB, prescription content or full transcripts. Phone numbers are logged
  masked (`+91******3210`).
- Emergency message (fixed copy):
  - en: "This sounds like it could be an emergency. Please hang up and call 112 right now, or go to the nearest emergency room."
  - hi: "यह आपातकालीन स्थिति लग रही है। कृपया अभी फ़ोन रखकर 112 पर कॉल करें, या नज़दीकी इमरजेंसी में जाएँ।"
- Fillers while a tool runs: en "One moment, let me check." / hi "एक सेकंड, मैं देख रही हूँ।"
- Welcome greeting: "Namaste! MediCare mein aapka swagat hai. Welcome to MediCare. Main aapki kaise madad kar sakti hoon? How can I help you today? This is an automated assistant."

## Review Focus

1. **Withheld or invalid caller ID** (`From` is `anonymous`, `+266696687`, empty, or not a phone):
   the call proceeds with general info only. No lookup, no registration, no account actions.
   Pinned in Task 2 (`normalize_phone`) and Task 10 (webhook/setup).
2. **Slot taken between read-back and "yes"** (express returns 409 on hold or book): the tool
   says the slot was just taken and offers to find another. `pending_hold` is cleared and the
   call never crashes. Pinned in Task 8.
3. **Caller hangs up mid-booking** (WebSocket closes while the graph is running): the run
   finishes or is abandoned without an unhandled exception, the hold is released on close, and
   nothing is spoken to a closed socket. Pinned in Task 10.
4. **Date boundaries**: "tomorrow" at 23:30 UTC (05:00 IST next day) means the IST tomorrow. A
   slot at 10:00 IST is spoken as "10:00 AM", not 04:30. Pinned in Task 8 (`turn_context`,
   `speak_time` and slot formatting).
5. **express-api/spring-api down or slow**: a timeout or 5xx becomes a short apology the caller
   hears ("I'm having trouble reaching our system…"), not silence or a stack trace. Pinned in
   Task 5 (client error mapping) and Task 8.

---

### Task 0: Spike: ConversationRelay Hindi + latency (throwaway, go/no-go)

Not TDD. No code is kept. Lives in `receptionist/spike/` and is deleted before Task 1 merges.

**Files:** Create `receptionist/spike/relay_echo.py`, `receptionist/spike/script_hi_en.md`

- [ ] **Step 1:** Write a single-file FastAPI app. `POST /voice` returns TwiML
  `<Connect><ConversationRelay url="wss://…/ws" welcomeGreeting="…" interruptible="any">` with two
  `<Language>` children: `code="en-IN"` and `code="hi-IN"`. Each child sets a
  `ttsProvider`/`voice` and `transcriptionProvider`/`speechModel`. Start with ElevenLabs TTS and
  Deepgram `nova-3` STT, and confirm the attribute names against
  https://www.twilio.com/docs/voice/twiml/connect/conversationrelay. `WS /ws` sends each `prompt`
  to Claude (`claude-opus-5-5`, effort low, streaming) and streams tokens back as
  `{"type":"text","token":…,"last":…}`. Log the time from receiving `prompt` to sending the
  first token.
- [ ] **Step 2:** Write the 30-utterance script: 10 names (e.g. "Abhinav Singh", "Priyanka
  Chaturvedi"), 10 dates ("5 March 1990", "paanch March unnees sau nabbe", "15/08/1985"), and 10
  medicine/dosage lines ("Pantoprazole 40 mg once daily"), half Hindi, half English.
- [ ] **Step 3:** Expose it with a tunnel, point a Twilio number's voice webhook at it, and read
  the script on a real call. Record the transcription accuracy and latency p50/p90 from the logs.
- [ ] **Step 4:** Compare against the pass marks in spec §9: p50 ≤ 1.5 s, p90 ≤ 2.5 s, ≥ 90%
  names/dates correct. Write the results into `spike/RESULTS.md`, **stop, and report them to the
  owner**. The owner decides the model and whether ConversationRelay stays. If they switch to
  Media Streams, only Task 10's transport changes; re-plan Task 10 before continuing.

---

### Task 1: Receptionist project scaffold, config and health

**Files:**
- Modify: `receptionist/pyproject.toml`, `receptionist/.gitignore` (add `.env`, `.pytest_cache/`, `.ruff_cache/`)
- Delete: `receptionist/main.py`
- Create: `src/receptionist/__init__.py`, `src/receptionist/config.py`, `src/receptionist/app.py`,
  `src/receptionist/cli.py`, `src/receptionist/health.py`, `tests/conftest.py`, `tests/test_health.py`,
  `Dockerfile`, `.dockerignore`, `.env.example`, `.github/workflows/ci.yml`, `README.md` (rewrite)

**Interfaces:**
- Produces: `Settings` (pydantic-settings, env names exactly as spec §8; `jwt_secret` min length 32;
  `public_base_url` required; `database_url`, `redis_url`, `express_api_url`, `spring_api_url`,
  `api_key`, `twilio_auth_token`, `anthropic_api_key` required except in `APP_ENV=test`;
  `llm_model="claude-opus-5-5"`, `default_timezone="Asia/Kolkata"`, `verify_max_attempts_per_call=2`,
  `verify_max_failures_per_day=5`, `calls_per_hour_per_number=10`, `port=7000`).
  `get_settings() -> Settings` (lru_cache).
- Produces: `create_app(deps: AppDeps | None = None) -> FastAPI`, where `AppDeps` is a dataclass
  holding `settings`, `directory` (Task 3 `PatientDirectory`), `guards` (Task 3 `Guards`),
  `express` (Task 5), `spring` (Task 5) and `llm_factory: Callable[[], BaseChatModel]` (Task 9).
  Fields are typed as `Any` until the producing task lands, and tests inject fakes. `HealthChecker.status() -> dict[str, Literal["up","down","disabled"]]` with keys
  `postgres`, `redis`, `express`, `spring`.
- Produces: console script `receptionist` (`receptionist serve`, `receptionist chat` added in
  Task 9).

- [ ] **Step 1: Write failing tests** in `tests/test_health.py`:
  `test_root_running` (`GET /` → 200, `{"message":"Running"}`);
  `test_health_envelope` (fake checker returns `{"postgres":"up","redis":"down","express":"up","spring":"up"}`
  → 200, `success is True`, `data["dependencies"]["redis"] == "down"`, `timestamp` present);
  `test_unknown_route_404_envelope` (`success is False`).
- [ ] **Step 2:** `uv run pytest tests/test_health.py -v` → FAIL (module not found).
- [ ] **Step 3:** Set up `pyproject.toml` (src layout, hatchling, dependencies from the Tech
  Stack, dev group pytest/pytest-asyncio/ruff, `asyncio_mode = "auto"`). Implement `config.py`,
  `health.py`, `app.py` (envelope helpers `ok(message, data)` / error handler for 404/422/500 in
  the envelope shape), and `cli.py serve` (uvicorn, `timeout_graceful_shutdown=840`).
- [ ] **Step 4:** `uv run pytest -v && uv run ruff check .` → PASS.
- [ ] **Step 5:** Write the Dockerfile (python:3.14-slim, uv sync --frozen --no-dev, non-root
  uid 1000, `EXPOSE 7000`, `CMD ["receptionist","serve"]`). Write CI (`uv sync`, `ruff check`,
  `pytest`) and README (run, test, env vars). Run `docker build -t receptionist .` → succeeds.
- [ ] **Step 6: Commit** `feat: scaffold receptionist service with config and health`.

---

### Task 2: Identity rules: phone, names, verifier (pure)

**Files:**
- Create: `src/receptionist/identity/__init__.py`, `identity/phone.py`, `identity/names.py`, `identity/verify.py`
- Test: `tests/identity/test_phone.py`, `tests/identity/test_names.py`, `tests/identity/test_verify.py`

**Interfaces:**
- Produces: `normalize_phone(raw: str | None) -> str | None` (E.164 or None);
  `match_key(e164: str) -> str` (last 10 digits for `+91`, else digits without `+`);
  `mask_phone(e164: str) -> str`.
- Produces: `normalize_name(s: str) -> str`; `names_match(spoken: str, stored: str) -> bool`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class Candidate: id: str; full_name: str; email: str; dob: date | None
  class Outcome(StrEnum): VERIFIED, WRONG_DOB, LOCKED, NOT_FOUND, UNVERIFIABLE
  @dataclass(frozen=True)
  class VerifyResult: outcome: Outcome; candidate: Candidate | None = None; save_dob: bool = False; attempts_left: int = 0
  class Verifier:
      def __init__(self, candidates: list[Candidate], max_attempts: int = 2): ...
      failures: int  # failed attempts this call
      def attempt(self, full_name: str, dob: date) -> VerifyResult: ...
  def validate_dob(dob: date, today: date) -> bool  # not in future, age <= 120
  ```

- [ ] **Step 1: Write failing tests.**
  `test_phone.py`, parametrized `normalize_phone`: `"9876543210"`, `"98765 43210"`,
  `"+91-98765-43210"`, `"09876543210"`, `"919876543210"` → `"+919876543210"`;
  `"+14155550100"` → `"+14155550100"`; `"anonymous"`, `""`, `None`, `"+266696687"`, `"12345"`,
  `"5876543210"` (10 digits not starting 6–9) → `None`.
  `match_key("+919876543210") == "9876543210"`; `match_key("+14155550100") == "14155550100"`;
  `mask_phone("+919876543210") == "+91******3210"`.
  `test_names.py`: `names_match("abhinav singh", "Abhinav Singh")`;
  `names_match("Mr. Abhinav  Singh", "Abhinav Singh")`; `names_match("Singh Abhinav", "Abhinav Singh")`;
  `names_match("Abhinav Sing", "Abhinav Singh")` (STT variant); `not names_match("Ankit Singh", "Abhinav Singh")`;
  `not names_match("Priya", "Priyanka Sharma")`.
  `test_verify.py` (`A = Candidate("a","Abhinav Singh","a@x",date(1990,3,5))`,
  `B = Candidate("b","Ritu Singh","b@x",date(1992,1,2))`, `N = Candidate("n","Ravi Singh","n@x",None)`):
  - `test_single_match_verified`: `Verifier([A]).attempt("Abhinav Singh", date(1990,3,5))` → VERIFIED, candidate A, `save_dob False`.
  - `test_wrong_dob_counts_and_locks`: first wrong → WRONG_DOB `attempts_left == 1`; second wrong → LOCKED; a third, correct attempt → still LOCKED.
  - `test_family_number_picks_by_name`: `Verifier([A,B]).attempt("Ritu Singh", date(1992,1,2))` → VERIFIED B.
  - `test_unknown_name_is_not_found_and_not_a_failure`: `Verifier([A]).attempt("Meena Devi", date(1970,1,1))` → NOT_FOUND, `failures == 0`.
  - `test_sole_account_without_dob_saves_dob`: `Verifier([N]).attempt("Ravi Singh", date(1980,6,1))` → VERIFIED N, `save_dob True`.
  - `test_shared_number_without_dob_unverifiable`: `Verifier([A,N]).attempt("Ravi Singh", date(1980,6,1))` → UNVERIFIABLE, `failures == 0`.
  - `test_two_name_matches_same_dob_unverifiable`: two candidates "Abhinav Singh" with the same DOB → UNVERIFIABLE.
  - `test_empty_candidates_not_found`.
  - `validate_dob`: future date False; `date(1890,1,1)` False with today 2026-11-22; `date(1990,3,5)` True.
- [ ] **Step 2:** `uv run pytest tests/identity -v` → FAIL.
- [ ] **Step 3: Implement.**
  - Phone: strip everything but digits and a leading `+`. Indian mobiles: 10 digits `[6-9]…`,
    optionally prefixed `0`, `91` or `+91`. Other numbers that start with `+` and have 8–15
    digits are kept as-is, except the Twilio sentinels `+266696687`, `+7378742833`,
    `+2562533`, `+8656696` → None.
  - Names: lower-case, strip `.`/`,`, drop leading honorifics (`mr mrs ms miss dr shri smt sri kumari`)
    and a trailing `ji`, collapse spaces. Match when `rapidfuzz.fuzz.token_sort_ratio(a, b) >= 88`.
  - Verifier rules, in order:
    1. If locked → LOCKED.
    2. Name matches are the candidates that pass `names_match`. None → NOT_FOUND.
    3. Among the matches, those whose `dob == given`: exactly 1 → VERIFIED; 2 or more → UNVERIFIABLE.
    4. If a match has `dob None`: VERIFIED with `save_dob=True` only when `len(candidates) == 1`,
       else UNVERIFIABLE.
    5. Otherwise failures += 1, and return LOCKED if `failures >= max_attempts`, else WRONG_DOB.
- [ ] **Step 4:** `uv run pytest tests/identity -v` → PASS.
- [ ] **Step 5: Commit** `feat: add caller identity rules`.

---

### Task 3: Data access: caller lookup and Redis guards

**Files:**
- Create: `src/receptionist/data/__init__.py`, `data/db.py`, `data/guards.py`
- Test: `tests/data/test_guards.py`, `tests/data/test_db.py` (opt-in)

**Interfaces:**
- Consumes: `Candidate`, `match_key` (Task 2).
- Produces: `class PatientDirectory: async def find_by_phone(self, e164: str) -> list[Candidate]`
  (psycopg `AsyncConnectionPool`; `open()`, `close()`, `ping() -> bool`).
- Produces: `class Guards` over `redis.asyncio.Redis | None`:
  `async def register_call(e164) -> bool` (False when over `calls_per_hour_per_number`);
  `async def is_locked(e164) -> bool`; `async def record_failure(e164) -> int`;
  `async def set_call_meta(call_sid, data: dict) -> None`; `async def ping() -> bool`.
  Fail-open: a None client or a Redis error → `register_call` True, `is_locked` False,
  `record_failure` 0, and a warning is logged once.

- [ ] **Step 1: Write failing tests.** `test_guards.py` uses `fakeredis.aioredis` (add `fakeredis` to dev deps):
  `test_eleventh_call_in_hour_rejected` (10 × True, then False; TTL of `rcpt:calls:+91…` ≤ 3600);
  `test_lock_after_five_failures` (4 failures → `is_locked` False; 5th → True; TTL ≤ 86400);
  `test_fail_open_without_redis` (`Guards(None, …)`: register True, locked False);
  `test_fail_open_on_error` (client whose `incr` raises `ConnectionError` → True/False/0).
  `test_db.py` is marked `@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"))`:
  seed two PATIENT users with phones `"98765 43210"` and `"+91 9876543210"` plus a DOCTOR on the
  same number and a disabled patient. Assert `find_by_phone("+919876543210")` returns exactly the
  two enabled patients, with `dob` from `app.patient_profiles` (None when no profile row).
- [ ] **Step 2:** `uv run pytest tests/data -v` → FAIL.
- [ ] **Step 3: Implement.** The SQL (column-level grants in Task 6 allow exactly these columns):
  ```sql
  SELECT u.id::text, u.full_name, u.email, p.date_of_birth
    FROM auth.users u LEFT JOIN app.patient_profiles p ON p.user_id = u.id
   WHERE u.role = 'PATIENT' AND u.enabled
     AND CASE WHEN $2 THEN right(regexp_replace(coalesce(u.phone,''), '\D', '', 'g'), 10)
              ELSE regexp_replace(coalesce(u.phone,''), '\D', '', 'g') END = $1
  ```
  `$1 = match_key(e164)`, `$2 = e164.startswith("+91")`. `record_failure` uses `INCR`, then
  `EXPIRE … NX`.
- [ ] **Step 4:** `uv run pytest tests/data -v` → PASS (the DB test skipped unless
  `TEST_DATABASE_URL` is set; run it once against the compose Postgres after `bun run seed` and
  note the result in the commit body).
- [ ] **Step 5: Commit** `feat: add caller lookup and redis guards`.

---

### Task 4: spring-api: internal phone-patient endpoint

Repo `spring-api`, branch `feature/phone-patients`.

**Files:**
- Create: `src/main/java/com/cdac/api/internal/InternalPatientController.java`,
  `internal/PhonePatientService.java`, `internal/ApiKeyGuard.java`,
  `internal/dto/CreatePhonePatientRequest.java`, `internal/dto/PhonePatientResponse.java`
- Modify: `src/main/resources/application.yml` (add `app.internal.api-key: ${API_KEY:}`)
- Test: `src/test/java/com/cdac/api/internal/InternalPatientControllerTest.java`,
  `src/test/java/com/cdac/api/internal/PhonePatientServiceTest.java`

**Interfaces:**
- Produces: `POST /api/internal/phone-patients`, header `X-Api-Key`, body
  `{"fullName": string 2..150, "phoneE164": "^\\+[1-9]\\d{7,14}$", "idempotencyKey": string 1..100}`.
  Responses: `201` (created) or `200` (same idempotency key seen before), with `data` =
  `{"id","email","fullName"}`. `401` wrong or missing key, `503` when `app.internal.api-key` is
  empty (endpoint disabled), `400` validation, all in the `ApiResponse` envelope.
- Email = `"p-" + sha256Hex(idempotencyKey).substring(0, 24) + "@phone.medicare.invalid"`. The
  idempotency check is `findByEmail(email)`. No schema change.

- [ ] **Step 1: Write failing tests.** Controller test (`@WebMvcTest(InternalPatientController.class)`,
  `@Import(SecurityConfig.class)`, `@MockitoBean PhonePatientService`, property `app.internal.api-key=k`):
  `missingKeyIs401`, `wrongKeyIs401`, `invalidPhoneIs400`, `createdIs201WithEnvelope`
  (`$.data.email` ends with `@phone.medicare.invalid`), `replayIs200`. A second test class with an
  empty key: `disabledIs503`. Service test (Mockito `UserRepository`, real `BCryptPasswordEncoder(4)`):
  `createsPatientWithPlaceholderEmailAndUnusablePassword` (role PATIENT, phone as given,
  `emailVerified false`, `passwordHash` non-null and not matching `""`),
  `sameIdempotencyKeyReturnsExistingWithoutSaving`, `existingPhoneIsNotAConflict` (another user on
  that phone; still creates).
- [ ] **Step 2:** `mvnw.cmd test -Dtest=InternalPatientControllerTest,PhonePatientServiceTest` → FAIL.
- [ ] **Step 3: Implement.**
  - `ApiKeyGuard.check(String header)` uses `MessageDigest.isEqual` over UTF-8 bytes.
  - `PhonePatientService.create(req) -> Result(PhonePatientResponse, boolean created)` is
    `@Transactional`. The password is `passwordEncoder.encode(base64(32 SecureRandom bytes))`.
  - The controller returns `ApiResponse` the way `HealthController` does.
- [ ] **Step 4:** `mvnw.cmd test` → all PASS.
- [ ] **Step 5: Commit** `feat: add internal endpoint to create phone patients`.

---

### Task 5: Receptionist HTTP clients and token issuer

**Files:**
- Create: `src/receptionist/clients/__init__.py`, `clients/errors.py`, `clients/tokens.py`,
  `clients/express.py`, `clients/spring.py`
- Test: `tests/clients/test_tokens.py`, `tests/clients/test_express.py`, `tests/clients/test_spring.py`

**Interfaces:**
- Produces: `class ApiError(Exception): status: int; message: str; data: Any`. Its subclasses
  `Unavailable` (timeout, connection error or 5xx; `spoken = "I'm having trouble reaching our system right now."`),
  `Conflict` (409), `NotFound` (404), `BadRequest` (400/422).
- Produces: `class TokenIssuer(Protocol): def patient_token(self, user_id: str, email: str, name: str) -> str`;
  `class LocalJwtIssuer(TokenIssuer)` (HS256, TTL 300 s, settings issuer and audience).
- Produces: `class ExpressClient(base_url, issuer, http: httpx.AsyncClient)` with methods that
  return the envelope's `data`:
  `as_patient(user_id, email, name) -> PatientSession`. A `PatientSession` mints its token
  lazily and re-mints within 30 s of expiry, and has:
  `get_profile()`, `update_profile(date_of_birth: date)` (PUT `/api/patients/me/profile`),
  `list_appointments(scope: Literal["upcoming","past"], limit=5)`, `hold(doctor_id, clinic_id, start_at)`,
  `release_hold(hold_id)`, `book(doctor_id, clinic_id, start_at, reason)`,
  `cancel(appointment_id, reason)`, `reschedule(appointment_id, start_at, clinic_id)`,
  `prescriptions(patient_id, limit=3)`, `prescription(prescription_id)`, `resend_confirmation(appointment_id)`,
  `search_doctors(...)` (same arguments as the anonymous version; with a patient token, express
  falls back to the patient's saved location).
  Anonymous methods on `ExpressClient`: `specializations()`,
  `search_doctors(specialization_id=None, city=None, pincode=None, language=None, limit=5)`,
  `slots(doctor_id, date: date, clinic_id=None)`, `doctor(doctor_id)`, `ping() -> bool`.
- Produces: `class SpringClient(base_url, api_key, http)` with
  `create_phone_patient(full_name, phone_e164, idempotency_key) -> dict` and `ping()`.

- [ ] **Step 1: Write failing tests** with `httpx.MockTransport`:
  `test_patient_token_claims` (decode with the secret: `role == "PATIENT"`, `iss`/`aud` match,
  `exp - iat == 300`); `test_session_sends_bearer`; `test_upcoming_query`
  (`list_appointments("upcoming")` sends `from=<now ISO>`, `order=asc`, `limit=5`,
  `status=BOOKED,CONFIRMED`; `"past"` sends `to=<now>`, `order=desc`);
  `test_409_raises_conflict_with_message`; `test_timeout_raises_unavailable`
  (`httpx.ConnectTimeout` → `Unavailable`); `test_500_raises_unavailable`;
  `test_spring_sends_api_key_and_body`.
- [ ] **Step 2:** `uv run pytest tests/clients -v` → FAIL.
- [ ] **Step 3: Implement.** One `_request(method, path, token=None, **kw)` maps status codes to
  errors and unwraps `data`. httpx `Timeout(5.0, connect=2.0)`.
- [ ] **Step 4:** `uv run pytest tests/clients -v` → PASS.
- [ ] **Step 5: Commit** `feat: add express/spring clients and patient token issuer`.

---

### Task 6: express-api: SMS channel, placeholder guard, receptionist DB role

Repo `express-api`, branch `feature/sms-notifications`.

**Files:**
- Create: `src/services/sms.service.ts`, `src/utils/phonePatient.ts`,
  `src/db/migrations/003_notifications_channel.sql`, `src/db/migrations/004_receptionist_role.sql`,
  `src/test/sms.test.ts`, `src/test/notifications.test.ts`
- Modify: `src/config/env.ts`, `src/services/mailer.service.ts`, `src/services/notifications.service.ts`,
  `src/test/setup.ts`, `.env.example` (if present), `README.md`

**Interfaces:**
- Produces: `PHONE_PLACEHOLDER_DOMAIN = "phone.medicare.invalid"`,
  `isPhonePlaceholder(email: string): boolean` (`utils/phonePatient.ts`).
- Produces: `deliverSms(sms: Sms, maxAttempts = 3): Promise<DeliveryResult>` and
  `sendSms(sms: Sms): Promise<void>` (records in `notifications_log` with `channel='SMS'`), where
  `interface Sms { to: string; body: string; type: string; userId?: string; appointmentId?: string }`.
- Produces: env `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` (default ""), `TWILIO_SMS_FROM`,
  `TWILIO_MESSAGING_SERVICE_SID` (default ""). If both SID/token are empty → SKIPPED.
- Produces: `notify(appointmentId, event, opts?: { patientOnly?: boolean })` is exported
  (needed by Task 7).

- [ ] **Step 1: Write failing tests.**
  `sms.test.ts`, stubbing fetch the way `mailer.test.ts` does:
  `posts form to Twilio Messages API with basic auth`
  (URL `https://api.twilio.com/2010-04-01/Accounts/AC_test/Messages.json`, body has `To`, `Body`,
  `MessagingServiceSid` when set else `From`); `retries 429 then succeeds`;
  `does not retry 400`; `skips when credentials missing` (status SKIPPED, fetch not called).
  `notifications.test.ts`: mock `queryOne` (`mock.module("../db", …)`) to return an appointment
  row whose `patient_email` is `p-abc@phone.medicare.invalid` and `patient_phone` is
  `+919876543210`. Assert `notify(id,"BOOKED")` calls SMS (fetch to Twilio) and not Resend for the
  patient, and still emails the doctor. With a real email, Resend is called and Twilio isn't.
  `sendMail to a placeholder address is SKIPPED and never calls Resend`.
  Extend `setup.ts` with `TWILIO_ACCOUNT_SID=AC_test`, `TWILIO_AUTH_TOKEN=test`, `TWILIO_SMS_FROM=+15005550006`.
- [ ] **Step 2:** `bun test src/test/sms.test.ts src/test/notifications.test.ts` → FAIL.
- [ ] **Step 3: Implement.**
  - `003`: `ALTER TABLE app.notifications_log ADD COLUMN channel text NOT NULL DEFAULT 'EMAIL' CHECK (channel IN ('EMAIL','SMS'));`
  - `sendMail` inserts `channel='EMAIL'`. A placeholder `to` → record SKIPPED with error
    `"placeholder address"`, no send.
  - `loadAppointment` also selects `p.phone AS patient_phone`.
  - Patient SMS bodies (≤ 160 chars where possible), keyed by event:
    `MediCare: Appointment with Dr. {doctor} on {when} at {clinic} is booked.` /
    `…has moved to {when}…` / `…on {when} is cancelled.` / `Reminder: …on {when} at {clinic}.`
    Retry Twilio on 429/5xx with the mailer's backoff.
  - SMS is skipped when the patient has no phone (log `SKIPPED`, error `"no phone"`).
  - `004`, idempotent:
    ```sql
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receptionist_ro') THEN
        CREATE ROLE receptionist_ro NOLOGIN;
      END IF;
    END $$;
    GRANT USAGE ON SCHEMA auth, app TO receptionist_ro;
    GRANT SELECT (id, email, full_name, phone, role, enabled) ON auth.users TO receptionist_ro;
    GRANT SELECT (user_id, date_of_birth) ON app.patient_profiles TO receptionist_ro;
    ```
    The password and LOGIN are set per environment (Task 12 locally, Task 13 on RDS), never in a
    migration.
- [ ] **Step 4:** `bun test && bun run typecheck && bun run lint` → PASS. Also `bun run migrate`
  against compose Postgres → applies 003, 004 cleanly, and re-running is a no-op.
- [ ] **Step 5: Commit** `feat: send SMS notifications to phone-registered patients`.

---

### Task 7: express-api: resend confirmation endpoint

Same repo and branch as Task 6.

**Files:**
- Modify: `src/services/appointments.service.ts`, `src/controllers/appointments.controller.ts`,
  `src/routes/appointments.routes.ts`, `src/services/notifications.service.ts`
- Test: `src/test/http.test.ts` (extend)

**Interfaces:**
- Consumes: `notify(id, event, { patientOnly: true })` (Task 6).
- Produces: `POST /api/appointments/:id/resend-confirmation`, roles PATIENT/RECEPTIONIST/ADMIN,
  rate limit `rateLimit({ name: "resend", limit: 3, windowSeconds: 3600 })`, response
  `ok(res, "Confirmation resent", { appointmentId })`. 409 when the status is not BOOKED/CONFIRMED.
  Service `resendConfirmation(user: AuthUser, id: string): Promise<{ appointmentId: string }>`
  uses `loadVisible` (404 for others' appointments). Staff also go through `assertCanManageDoctor`.

- [ ] **Step 1: Write failing tests** in `http.test.ts`: `resend-confirmation requires auth` (401);
  `doctor role is 403`; `invalid id is 400`. Service-level tests with a mocked db:
  `cancelled appointment → 409`; `other patient's appointment → 404`.
- [ ] **Step 2:** `bun test src/test/http.test.ts` → FAIL.
- [ ] **Step 3:** Implement. `patientOnly` skips the doctor email.
- [ ] **Step 4:** `bun test && bun run typecheck && bun run lint` → PASS.
- [ ] **Step 5: Commit** `feat: add resend-confirmation endpoint`, then push the branch and open the
  PR with the org template (no attribution lines).

---

### Task 8: Agent core: safety, language, prompts, call context and tools

**Files:**
- Create: `src/receptionist/agent/__init__.py`, `agent/safety.py`, `agent/language.py`,
  `agent/prompts.py`, `agent/context.py`, `agent/tools.py`, `agent/speech.py`
- Test: `tests/agent/test_safety.py`, `tests/agent/test_language.py`, `tests/agent/test_prompts.py`,
  `tests/agent/test_tools.py`

**Interfaces:**
- Consumes: `Verifier`, `Outcome`, `Candidate`, `validate_dob` (Task 2); `PatientDirectory`,
  `Guards` (Task 3); `ExpressClient`, `SpringClient`, errors (Task 5).
- Produces: `detect_emergency(text: str) -> bool`; `EMERGENCY_MESSAGE: dict[Lang, str]`;
  `Lang = Literal["en","hi"]`.
- Produces: `utterance_language(text: str, stt_lang: str | None) -> Lang`;
  `class LanguageTracker(initial: Lang = "en")` with `observe(text, stt_lang) -> Lang | None`
  (returns the new language when it switches) and `force(lang)`.
- Produces: `SYSTEM_PROMPT: str`; `turn_context(ctx: CallContext, now: datetime) -> str`.
- Produces: `speak_time(dt: datetime, tz: str, lang: Lang) -> str` and
  `speak_date(d: date, lang: Lang) -> str` (`agent/speech.py`).
- Produces:
  ```python
  @dataclass
  class PendingHold: doctor_id: str; clinic_id: str; start_at: datetime; hold_id: str | None; summary: str; turn: int; appointment_id: str | None = None
  @dataclass
  class PendingCancel: appointment_id: str; summary: str; turn: int
  @dataclass
  class CallContext:
      call_sid: str; caller_e164: str | None; candidates: list[Candidate]; verifier: Verifier
      language: LanguageTracker; turn: int = 0
      verified: Candidate | None = None; registered_this_call: bool = False; locked: bool = False
      pending_hold: PendingHold | None = None; pending_cancel: PendingCancel | None = None
      emergency: bool = False; end_requested: bool = False
      def patient(self) -> PatientSession  # raises ToolException("not verified") if not verified
  def build_tools(ctx: CallContext, deps: ToolDeps) -> list[BaseTool]
  def tools_for(ctx: CallContext, all_tools: list[BaseTool]) -> list[BaseTool]
  ```
  `ToolDeps` is a dataclass of `directory`, `guards`, `express`, `spring` and `settings`. Tool
  names and arguments are exactly as in spec §6.4, with these changes, which are how code
  enforces the read-back:
  - `book_appointment(reason: str | None)` books `ctx.pending_hold` only if `ctx.pending_hold.turn < ctx.turn`.
  - `prepare_cancel(appointment_id)` → `cancel_appointment(reason: str | None)` requires `pending_cancel.turn < ctx.turn`.
  - `reschedule_appointment(appointment_id, start_at, clinic_id=None)` holds and sets
    `pending_hold.appointment_id`, then `confirm_reschedule()` requires the next turn.
  - `switch_language(lang: Lang)` and `end_call()` are added. `end_call` sets
    `ctx.end_requested = True`.
  - The DOB argument is `dob: date` (ISO `YYYY-MM-DD`). The model turns spoken dates into ISO,
    and code validates them. This replaces the spec's spoken-date parser tests; the
    scripted-conversation tests in Task 9 cover it.

- [ ] **Step 1: Write failing tests.**
  - `test_safety.py`: True for "I have chest pain", "mujhe saans lene mein dikkat ho rahi hai",
    "सीने में दर्द", "he is unconscious", "I want to kill myself", "bahut khoon beh raha hai";
    False for "I need a cardiologist appointment", "my chest X-ray report", "pain in my knee for two weeks".
  - `test_language.py`: `utterance_language("मुझे अपॉइंटमेंट चाहिए", None) == "hi"`;
    `utterance_language("mujhe appointment chahiye kal", None) == "hi"`;
    `utterance_language("I need an appointment", None) == "en"`. Tracker: one Hindi utterance
    → no switch; two in a row → returns `"hi"`; Hinglish alternating en/hi/en/hi → never
    switches; `force("hi")` switches immediately.
  - `test_prompts.py`: `turn_context` at `datetime(2026,11,22,23,30,tzinfo=UTC)` contains
    `"Today is Monday, 23 November 2026"` (IST) and the verification state (`"Caller not verified"`
    / `"Verified as Abhinav Singh"`), and never the DOB. `SYSTEM_PROMPT` mentions `112`,
    "one question at a time", and "read back". `speak_time(datetime(2026,11,24,4,30,tzinfo=UTC),"Asia/Kolkata","en") == "10:00 AM"`.
  - `test_tools.py`, with fake directory, guards and clients (in-memory classes recording calls):
    - `test_patient_tools_refuse_before_verify` (`list_my_appointments` → error text, no express call).
    - `test_tools_for_hides_patient_tools_until_verified` (names before:
      `{verify_identity, register_patient, list_specializations, find_doctors, clinic_info, suggest_specialization, get_available_slots, switch_language, end_call}`).
    - `test_verify_success_sets_verified` and `test_verify_save_dob_calls_update_profile`.
    - `test_wrong_dob_records_failure_in_guards` and `test_locked_number_refuses_verify`
      (`guards.is_locked` True → "locked" message, Verifier not called).
    - `test_register_only_when_not_found` (before any NOT_FOUND → refused; after → spring called
      with `idempotency_key == call_sid`, DOB saved, `ctx.verified` set,
      `registered_this_call True`).
    - `test_register_refused_without_caller_id` (`caller_e164 None` → refused).
    - `test_book_requires_new_turn` (hold at turn 3, book at turn 3 → refused with "confirm first";
      at turn 4 → express.book called with hold's slot, `pending_hold` cleared).
    - `test_book_conflict_clears_hold` (express.book raises `Conflict` → result text says the slot
      was just taken, `pending_hold None`).
    - `test_hold_503_continues_without_hold_id`.
    - `test_cancel_two_phase` and `test_cancel_cutoff_message` (BadRequest with the 2-hour message
      is passed through in plain words).
    - `test_unavailable_maps_to_apology`.
    - `test_slots_are_few_and_spoken_in_clinic_tz` (express returns 30 slots → tool returns ≤ 5,
      times via `speak_time`).
    - `test_read_prescription_returns_items_verbatim`.
- [ ] **Step 2:** `uv run pytest tests/agent -v` → FAIL.
- [ ] **Step 3: Implement.**
  - Emergency patterns: case-insensitive regex lists for en, romanised hi and Devanagari covering
    chest pain, breathing difficulty, unconscious/fainted, stroke signs (face droop, slurred
    speech, one side weak), severe bleeding, suicide/self-harm, poisoning/overdose, seizure.
    Exclusion words that cancel a match: `report`, `x-ray`, `appointment for`.
  - Language: Devanagari ≥ 30% of letters → hi. Otherwise ≥ 2 hits from the romanised lexicon
    `{hai, hain, nahi, nahin, mujhe, mera, meri, kya, aap, chahiye, karna, karni, hoon, kal, aaj, doctor sahab, ji, theek, kab, kitne, batao, bataiye}`
    → hi. Otherwise use the STT `lang` prefix if given, else en.
  - Tools: `StructuredTool.from_function(coroutine=…, args_schema=<pydantic model>, handle_tool_error=True)`.
    Every tool returns a short `str` (≤ 400 chars) meant to be paraphrased aloud. Map `Unavailable`
    to its `spoken` text.
  - `suggest_specialization` makes no LLM call. It returns the live specialization list with
    descriptions and the instruction `"Choose 1-2 from this list only; never diagnose."`; the
    main model does the choosing.
  - `find_doctors` passes `language="Hindi"` when the call language is hi and the caller didn't
    say otherwise.
  - The system prompt must cover: persona (warm female front-desk voice, brief, spoken style,
    no lists/markdown, one question at a time); disclosure that it's automated; emergency rule;
    no diagnosis, medicine advice or dosage changes; verify before any personal info; never
    reveal whether a number/name has an account until verified (say "I couldn't verify that");
    read back and wait for yes before book/cancel/reschedule; times in the clinic timezone;
    Hindi replies in Devanagari with names and medicines as-is; keep replies under ~40 words;
    use `end_call` after goodbye.
- [ ] **Step 4:** `uv run pytest tests/agent -v` → PASS.
- [ ] **Step 5: Commit** `feat: add agent safety, language, prompts and tools`.

---

### Task 9: Agent graph, LLM factory and text-mode CLI

**Files:**
- Create: `src/receptionist/agent/llm.py`, `agent/graph.py`, `src/receptionist/session.py`
- Modify: `src/receptionist/cli.py` (add `chat`)
- Test: `tests/agent/test_llm.py`, `tests/agent/test_graph.py`, `tests/agent/fakes.py`

**Interfaces:**
- Consumes: Task 8 (all), Task 3, Task 5.
- Produces: `make_llm(settings) -> BaseChatModel`: `ChatAnthropic(model=settings.llm_model,
  max_tokens=4096, streaming=True)`, with `output_config={"effort":"low"}`, `betas=["server-side-fallback-2026-07-01"]`
  and `fallbacks="default"` passed through whatever the installed langchain-anthropic accepts
  (`model_kwargs`/`extra_body`/`betas`). Confirm which by reading the installed package source,
  and don't guess.
- Produces: `build_graph(llm: BaseChatModel, ctx: CallContext, tools: list[BaseTool]) -> CompiledStateGraph`.
  State: `MessagesState` + `emergency: bool`. Nodes `safety` → (`END` if emergency) → `agent` ⇄
  `tools` (`ToolNode(tools)`, `tools_condition`). Checkpointer `InMemorySaver`, `thread_id = ctx.call_sid`.
  The agent node binds `tools_for(ctx, tools)` each turn and prepends
  `SystemMessage(content=[{"type":"text","text":SYSTEM_PROMPT,"cache_control":{"type":"ephemeral"}},{"type":"text","text":turn_context(ctx, now)}])`.
  A `stop_reason == "refusal"` response becomes the fixed line "Sorry, I can't help with that on the phone."
- Produces: `class CallSession` (transport-independent, used by the CLI and by Task 10):
  ```python
  class CallSession:
      @classmethod
      async def start(cls, call_sid: str, raw_from: str | None, deps: AppDeps) -> "CallSession"
      async def respond(self, text: str, stt_lang: str | None = None) -> AsyncIterator[Speech]
      def trim_last_reply(self, spoken_text: str) -> None
      ctx: CallContext
  @dataclass
  class Speech: kind: Literal["token","filler","switch_language","end"]; text: str = ""; lang: Lang | None = None
  ```
  `respond` increments `ctx.turn`, runs `LanguageTracker.observe` (yields `switch_language` on a
  change), and streams `graph.astream(..., stream_mode=["messages","updates"])`. It yields
  agent-node text tokens and emergency text from `safety` updates. It yields one `filler` when a
  tool call starts before any text this turn, and `end` once `ctx.end_requested`.
  `trim_last_reply` replaces only the text blocks of the last AIMessage (it keeps thinking
  blocks) with `spoken_text`. If the last AIMessage has tool_calls, it does nothing.

- [ ] **Step 1: Write failing tests.** `fakes.py` provides `ScriptedChatModel(BaseChatModel)`,
  which returns a queued list of `AIMessage`s (text and/or `tool_calls`) and supports `bind_tools`.
  - `test_llm.py`: `make_llm(settings)._get_request_payload([HumanMessage("hi")])` (or the
    package's equivalent) contains `model == "claude-opus-5-5"`, `output_config.effort == "low"`,
    and `fallbacks == "default"`.
  - `test_graph.py`:
    - `test_emergency_short_circuits` (respond("I have chest pain") yields the en emergency text;
      the scripted model was never called).
    - `test_verify_then_list_appointments` (script: tool_call verify_identity → text → tool_call
      list_my_appointments → text; asserts express fake calls in order).
    - `test_tool_turn_yields_one_filler`.
    - `test_hindi_switch_emitted_after_two_hindi_turns`.
    - `test_trim_last_reply`.
    - `test_end_call_yields_end`.
    - `test_book_across_two_turns` (turn 1: hold + read-back; turn 2 "haan, book kar do": book
      succeeds).
    - `test_refusal_becomes_fixed_line`.
- [ ] **Step 2:** `uv run pytest tests/agent -v` → FAIL.
- [ ] **Step 3:** Implement `llm.py`, `graph.py` and `session.py`. `CallSession.start`:
  `normalize_phone` → if None, candidates `[]`; else `directory.find_by_phone` → `Verifier(candidates, settings.verify_max_attempts_per_call)`.
  Then `guards.is_locked` → `ctx.locked`.
- [ ] **Step 4:** `uv run pytest -v` → PASS.
- [ ] **Step 5:** `receptionist chat --from +919876543210 [--call-sid cli-<uuid>]` reads stdin
  lines and prints speech (fillers in brackets, `[switch → hi]`, `[end]`). Manually run against the
  compose stack (seed data, real `ANTHROPIC_API_KEY`) and go through these English and Hindi
  scenarios: verify → book → cancel; unknown number → register → book; wrong DOB twice; chest
  pain; prescription read-back. Note the outcomes in the commit body.
- [ ] **Step 6: Commit** `feat: add call graph, session and text chat CLI`.

---

### Task 10: Twilio voice transport

**Files:**
- Create: `src/receptionist/voice/__init__.py`, `voice/twiml.py`, `voice/twilio_auth.py`, `voice/relay.py`, `voice/routes.py`
- Modify: `src/receptionist/app.py` (mount routes, drain flag), `config.py` (TTS/STT settings)
- Test: `tests/voice/test_twiml.py`, `tests/voice/test_webhook.py`, `tests/voice/test_relay.py`

**Interfaces:**
- Consumes: `CallSession`, `Speech` (Task 9); `Guards`, `normalize_phone`, `mask_phone`.
- Produces: `build_connect_twiml(ws_url: str, settings, custom: dict[str,str]) -> str` (en-IN +
  hi-IN `<Language>` elements from `TTS_*`/`STT_*` settings, `welcomeGreeting` as in Global
  Constraints, `interruptible="any"`, `<Parameter>`s for `custom`); `build_reject_twiml(message: str) -> str`.
- Produces: `verify_twilio_signature(url: str, params: dict, signature: str, auth_token: str) -> bool`
  (twilio `RequestValidator`). The URL is rebuilt from `PUBLIC_BASE_URL` + path, never from the
  request host.
- Produces routes: `POST /twilio/voice`, `POST /twilio/status`, `WS /twilio/relay`.
- Produces: `class RelayConnection(ws, deps)`. It handles ConversationRelay messages `setup`,
  `prompt` (only `last: true` prompts are processed), `interrupt`, `dtmf` (ignored), and `error`
  (logged). It sends `text` (`token`, `last`, `lang`), `language` (`ttsLanguage`,
  `transcriptionLanguage`: `"hi-IN"`/`"en-IN"`) and `end`. Verify the field names against the
  Twilio ConversationRelay WebSocket docs before coding.

- [ ] **Step 1: Write failing tests.**
  - `test_twiml.py`: the output parses as XML; it has two `Language` elements with codes `en-IN`
    and `hi-IN`; `url` starts with `wss://`; the custom parameter `callSid` is present.
  - `test_webhook.py` (TestClient, signatures computed with `RequestValidator(token).compute_signature`):
    `test_bad_signature_403`; `test_valid_signature_returns_connect_twiml` (content-type
    `text/xml`); `test_over_rate_limit_returns_reject_twiml` (fake guards `register_call` False →
    `<Say>` + `<Hangup/>`); `test_anonymous_caller_still_connects` (`From=anonymous` →
    connect TwiML, `register_call` not called); `test_draining_returns_503`.
  - `test_relay.py` (TestClient websocket, `CallSession` replaced by a fake yielding scripted `Speech`):
    `test_setup_then_prompt_streams_tokens_and_last` (the final frame has `last: true`);
    `test_non_final_prompt_ignored`; `test_switch_language_sends_language_message`;
    `test_end_sends_end_message`; `test_interrupt_mutes_rest_and_trims`
    (after `interrupt` with `utteranceUntilInterrupt: "Your appointment is"`, no more `text` frames
    for that turn and `trim_last_reply("Your appointment is")` is called once the run completes);
    `test_prompts_queue_while_running` (the second prompt is processed after the first finishes,
    in order); `test_close_mid_run_releases_hold_and_no_send_after_close` (socket closed while the
    fake is mid-stream → no exception escapes, `express` `release_hold` called when the ctx has a
    `pending_hold.hold_id`).
- [ ] **Step 2:** `uv run pytest tests/voice -v` → FAIL.
- [ ] **Step 3: Implement.**
  - Webhook: validate the signature → 403. Draining → 503. `normalize_phone(From)`; when not
    None, `guards.register_call` and on False return the reject TwiML (en+hi "We're receiving too
    many calls from this number. Please try again later."). Then return connect TwiML with
    `ws_url = PUBLIC_BASE_URL` (`https`→`wss`) + `/twilio/relay`, and set `guards.set_call_meta`.
  - Relay: one asyncio queue of prompts and one worker task per connection. The interrupt sets
    `muted` for the current run. There's no cancellation of graph runs (spec §6.6 interrupts,
    resolved this way so write tools are never cut mid-call).
  - Status callback: on `completed`/`failed`/`no-answer`, it releases any hold of a still-open
    session (sessions are kept in a dict keyed by CallSid, cleared on WebSocket close).
  - On SIGTERM: `app.state.draining = True`. `/api/health` returns 503 while draining, so the ALB
    stops sending new calls. Existing sockets continue.
- [ ] **Step 4:** `uv run pytest -v && uv run ruff check .` → PASS.
- [ ] **Step 5:** Run a local end-to-end call: the compose stack (Task 12 if done first, else run
  the services locally) plus a tunnel, with `PUBLIC_BASE_URL` set to the tunnel and the Twilio
  number's webhook pointed at `/twilio/voice` and the status callback at `/twilio/status`. Repeat
  the Task 9 scenarios by phone in both languages.
- [ ] **Step 6: Commit** `feat: add Twilio ConversationRelay transport`, then push and open the PR.

---

### Task 11: spring-api PR

- [ ] **Step 1:** Push `feature/phone-patients` and open the PR with the org template, linking
  the receptionist spec. Merge order: spring-api and express-api before receptionist deploys.

---

### Task 12: Local stack wiring and docs

Repos `.github` (`chore/compose-receptionist`) and workspace `CLAUDE.md` (not a repo; edit in place).

**Files:**
- Modify: `.github/docker-compose.yml`, `CLAUDE.md` (workspace root), `express-api/README.md`
- Create: `.github/postgres-init/10-receptionist-role.sql` (only if compose already mounts an
  init dir; otherwise document the manual `ALTER ROLE`)

- [ ] **Step 1:** Add a `receptionist` service to the compose file: build `./receptionist`,
  port `7000:7000`, `depends_on` postgres, redis, express-api, spring-api (healthy). Env:
  `DATABASE_URL` uses the main user locally (spec §7.4), `REDIS_URL=redis://redis:6379/0`,
  `EXPRESS_API_URL=http://express-api:5000`, `SPRING_API_URL=http://spring-api:8080`, and
  `PUBLIC_BASE_URL`, `TWILIO_*`, `ANTHROPIC_API_KEY`, `API_KEY` from the root `.env`. Add
  `TWILIO_*` and `API_KEY` to express-api and spring-api.
- [ ] **Step 2:** `docker compose -f .github/docker-compose.yml --project-directory . up --build`
  → `curl localhost:7000/api/health` shows all four dependencies `up`.
- [ ] **Step 3:** Update the workspace `CLAUDE.md`: add the `receptionist` row to the layout
  table and its commands (`uv sync`, `uv run pytest`, `receptionist chat`). Add the new env vars
  (receptionist, express SMS, spring `API_KEY` use), the placeholder-email/SMS rule, and the
  `receptionist_ro` role to Gotchas.
- [ ] **Step 4: Commit** (in `.github`) `chore: run receptionist in the local stack`.

---

### Task 13: Terraform: Redis, secrets, ECR, receptionist DB role

Repo `infra`, branch `feature/receptionist-infra`.

**Files:**
- Create: `infra/elasticache.tf`
- Modify: `infra/secrets.tf`, `infra/variables.tf`, `infra/outputs.tf`, `infra/terraform.tfvars.example`, `infra/README.md`

**Interfaces:**
- Produces: `aws_elasticache_replication_group.redis` (engine redis 7.x, one node,
  `transit_encryption_enabled = true`, at-rest encryption on, subnet group from
  `module.vpc.database_subnets` or private subnets, SG ingress 6379 from
  `module.eks.node_security_group_id` only). `local.redis_url = "rediss://${primary_endpoint}:6379/0"`.
- Produces: `REDIS_URL` added to `local.db_connection`. A new secret `${local.name}/receptionist`
  (Terraform-managed) with `DATABASE_URL = postgres://receptionist_ro:<random_password.receptionist_db>@host:5432/db?sslmode=require`
  and `REDIS_URL`. `ecr_repositories` default includes `"receptionist"`. Output
  `receptionist_db_role_sql` (sensitive) = `ALTER ROLE receptionist_ro LOGIN PASSWORD '<pw>';`.
- `app` secret: document (README) the keys to add by CLI. Terraform doesn't own its value:
  `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM`/`TWILIO_MESSAGING_SERVICE_SID`,
  `ANTHROPIC_API_KEY`, `PUBLIC_BASE_URL`, `API_KEY`, plus the keys CLAUDE.md lists as missing.

- [ ] **Step 1:** Get provider/module versions with the Terraform registry tools, then write the
  resources.
- [ ] **Step 2:** `terraform fmt -check && terraform validate` → success.
- [ ] **Step 3:** `terraform plan -out tfplan` → review: only additions plus the in-place
  `db-connection` secret version change. **Ask the owner before `terraform apply`.**
- [ ] **Step 4:** README runbook: after apply, run
  `terraform output -raw receptionist_db_role_sql` and execute it once with a temporary pod
  (`kubectl run psql --rm -it --image=postgres:16 -- psql "$MASTER_URL"`), then
  `aws secretsmanager put-secret-value` for the manual app keys.
- [ ] **Step 5: Commit** `feat: add redis, receptionist secret and ecr repo`.

---

### Task 14: Helm: receptionist app

Repo `helm`, branch `feature/receptionist-app`.

**Files:**
- Modify: `helm/cdac-apis/values.yaml`, `helm/cdac-apis/templates/ingress.yaml`

- [ ] **Step 1:** Add `secrets.receptionist: { awsSecretName: cdac-dev/receptionist, k8sSecretName: receptionist-secrets }`
  and the `apps.receptionist` entry: port 7000, `replicas: 2`, `secrets: [app, receptionist]`
  (**not** `db`: it holds the master URL), `env: { APP_ENV: production }`,
  `javaTruststore: false`, probes liveness `/` and readiness `/api/health`,
  `terminationGracePeriodSeconds: 900`, autoscaling min 2 / max 6, pdb minAvailable 1,
  `ingress.listenPort: 7000`, `ingress.healthcheckPath: /`.
- [ ] **Step 2:** In `ingress.yaml`, merge an optional per-app `$app.ingress.annotations` map, and
  set for the receptionist `alb.ingress.kubernetes.io/load-balancer-attributes: idle_timeout.timeout_seconds=3600`.
  It applies to the shared ALB; note that in values comments. Twilio needs HTTPS with a valid
  cert, so `global.ingress.certificateArn` must be set and a DNS name must point at the ALB.
  Document that in NOTES.txt.
- [ ] **Step 3:** `helm lint helm/cdac-apis && helm template x helm/cdac-apis | grep -A3 "name: receptionist"`
  → lint clean; the Deployment, Service and Ingress render with port 7000, and the ExternalSecret
  `receptionist-secrets` renders.
- [ ] **Step 4: Commit** `feat: deploy receptionist`.

---

### Task 15: Deploy to cdac-dev and a real call (owner-gated)

- [ ] **Step 1:** With the owner's go-ahead: apply Terraform (Task 13), run the role SQL and
  secret updates, push images (receptionist, express-api, spring-api) to ECR, and run
  `helm upgrade --install` with `terraform output -raw helm_install_command`.
- [ ] **Step 2:** `curl https://<receptionist-host>:7000/api/health` → all `up`.
- [ ] **Step 3:** Point the Twilio number's voice webhook at `https://<host>:7000/twilio/voice`
  and its status callback at `/twilio/status`. Run the Task 9 scenarios on a real call in both
  languages. Confirm an SMS arrives for a phone-registered patient; this needs DLT registration
  done, so note it as blocked if not.
- [ ] **Step 4:** Report the results to the owner, along with the outstanding owner actions from
  spec §10.
