# MediCare voice receptionist: design

Date: 2026-11-22. Status: draft for approval.

## 1. Intent

Real MediCare patients phone a number and talk, in Hindi or English, to an AI receptionist that
behaves like a good front-desk person. It recognises them by their phone number, confirms who they
are, and does the routine front-desk work on the call: booking, rescheduling and cancelling
appointments, telling them what is coming up, suggesting which kind of doctor to see for their
symptoms, reading back past prescriptions, and making sure they get a confirmation. Callers the
system doesn't know are registered on the call and can book straight away.

Success: a patient can phone, be recognised, and book or cancel an appointment end to end without a
human. They get the same confirmations and reminders as app users, and they never hear another
patient's data.

## 2. Goals, non-goals, anti-goals

**Goals**
- Inbound calls on a Twilio number, natural conversation in Hindi and English, including mixing
  the two (Hinglish), with barge-in (the caller can interrupt).
- Identify callers by caller ID plus date of birth (decided: option B).
- Register unknown callers on the call (name + date of birth, phone from caller ID).
- Book, reschedule, cancel, list upcoming/past appointments, suggest doctors from symptoms, read
  past prescriptions, resend a confirmation.
- Notifications for phone-registered patients go by SMS (Twilio); everyone else keeps email.
- Reuse the existing Postgres and Redis, and express-api's booking rules, unchanged in behaviour.

**Non-goals (this version)**
- Outbound calls (reminder calls, follow-up calls).
- Transfer to a human. There is no front-desk line to transfer to.
- Medical records/file upload by phone, reviews, payments.
- Doctor- or receptionist-staff use of the phone line (callers are patients only).
- Reading lab reports or other GridFS files aloud.
- Regional languages beyond Hindi and English.

**Anti-goals (a failure even if it "works")**
- Telling a caller anything about a patient they haven't verified as.
- Diagnosing, recommending medicines, or changing dosage. The receptionist routes, it doesn't treat.
- Missing an emergency: chest pain, breathing difficulty, stroke signs, heavy bleeding, suicidal
  intent, etc. must get "call 112 now" immediately, not a booking.
- Booking or cancelling without the caller clearly saying yes to a read-back.
- A second copy of booking rules that drifts from express-api's.

## 3. What exists today (constraints)

- `receptionist/` is an empty Python 3.14 uv project (`main.py`, `pyproject.toml`).
- `auth.users` (spring-api, Flyway): `email` NOT NULL UNIQUE lower-case, `password_hash` NOT NULL,
  `phone varchar(20)` nullable, **not unique, no fixed format**. spring-api's auth endpoints are
  documented TODOs; repositories/entities exist.
- `app.patient_profiles.date_of_birth` is nullable, so many patients have none.
- express-api already does: slot generation and exact-slot validation (`resolveSlot`), Redis slot
  holds (`POST /api/appointments/holds`, 5-min TTL), double-booking prevention (gist exclusion →
  409), the 2-hour patient change cutoff (`PATIENT_CHANGE_CUTOFF_HOURS`), audit logging, and
  confirmation/cancellation/reschedule/24h-reminder emails (`notifications.service.ts`).
  `PATIENT` can read their own prescriptions (`GET /api/patients/:id/prescriptions`,
  `GET /api/prescriptions/:id`) and update their profile (`PUT /api/patients/me/profile`).
- Doctor search supports `specializationId`, city/pincode/lat-lng, and `language`
  (`doctor_profiles.languages`), which lets us prefer Hindi-speaking doctors.
- Specializations exist as a list (`GET /api/specializations`); nothing maps symptoms to them.
- Redis in compose is `redis:7.4-alpine` (no RedisJSON/RediSearch modules).
- Twilio in India: inbound to a Twilio number works, but number availability/KYC must be checked,
  and **commercial SMS to Indian numbers requires TRAI DLT registration** (sender ID + message
  templates). Both are account/regulatory tasks for the owner, not code (see §10).
- Twilio ConversationRelay supports per-language STT/TTS config and mid-call language switching;
  Hindi (`hi-IN`) support with a natural voice must be confirmed in the spike (§9, step 0).

## 4. Approaches considered

**Voice pipeline**
1. **Twilio ConversationRelay (chosen).** Twilio does speech-to-text, text-to-speech and barge-in,
   and exchanges *text* with our service over a WebSocket. It fits LangGraph directly (text in,
   streamed text out), with the least audio engineering and good latency.
2. Twilio Media Streams + our own STT/TTS (e.g. Deepgram + ElevenLabs). Full control of voices
   and Hindi models, but we own audio buffering, voice activity detection, barge-in and echo. Kept
   as the **fallback if ConversationRelay's Hindi quality fails the spike**. Only the transport
   layer changes; the graph and tools stay the same.
3. Speech-to-speech realtime model. Lowest latency, but it doesn't fit LangGraph/LangChain (the
   user's chosen framework), and tool-gating is weaker. Rejected.

**How the receptionist touches data**
1. **Hybrid (chosen):** read-only direct Postgres for the lookups no API offers (caller-ID match,
   DOB check). *Every write and every domain read goes through the existing APIs, acting as the
   patient.* Booking rules, holds, audit and notifications stay in one place.
2. Everything direct to Postgres. Fast to write, but it duplicates `resolveSlot`, status rules,
   holds and notifications, and skips the audit trail. Violates the "no second copy" anti-goal.
3. Everything via APIs. Purest, but needs new internal lookup endpoints for something that's a
   two-line read-only query. Not worth it now; can move later.

**Who creates accounts for new callers**
1. **New internal endpoint on spring-api (chosen)**, `POST /api/internal/phone-patients`,
   authenticated with the existing shared `API_KEY`. spring-api owns `auth.users`, so it stays the
   only writer to the `auth` schema.
2. Receptionist inserts into `auth.users` directly. It works, but it breaks the ownership rule
   in CLAUDE.md, and spring's eventual AuthService would not know about these users. Rejected.

**Conversation state**
1. **LangGraph in-memory checkpointer per call (chosen)**, `thread_id = CallSid`. A call is one
   WebSocket pinned to one pod, so state doesn't need to outlive the connection. Redis is used for
   what *does* cross calls (see §6.5).
2. `langgraph-checkpoint-redis`. Needs Redis 8 / Redis Stack modules, which compose and
   ElastiCache don't have today. Revisit if calls must survive pod restarts.

## 5. Decisions made with the owner

- Real patients will call (production-grade identity, safety and privacy).
- Identity = caller ID + date of birth (B). Shared family number → ask name, match name + DOB.
- Unknown caller → register as a new patient on the call.
- **Several patients may share one phone number** (option a). A caller who is not one of the
  accounts already on that number registers as a new patient on it. Each person books and hears
  only their own data, after their own name + DOB check. Booking on behalf of family members
  (option b) is out of scope.
- Existing account with no DOB on file → ask name + DOB, and save the DOB only if the name
  matches (first-call setup; trusts caller ID for this one step). *(My call, offered during the
  conversation without objection.)*
- On a shared number, a candidate account with no DOB on file can't be verified by phone. A name
  match alone never opens an account. *(My call.)*
- AWS deployment is in scope for this version.
- New phone-registered users get **SMS** notifications instead of email.
- Languages: Hindi and English.
- Framework: LangChain / LangGraph, in the existing `receptionist/` Python project.

## 6. Design

### 6.1 Components

```
Caller ──PSTN──> Twilio number
                   │ POST /twilio/voice (signed webhook)
                   ▼
           receptionist (Python, FastAPI)
             ├─ returns TwiML <Connect><ConversationRelay url="wss://…/twilio/relay"> with en-IN + hi-IN <Language>s
             ├─ WS /twilio/relay  ◄──text──►  LangGraph call graph (Claude via langchain-anthropic)
             ├─ Postgres (read-only role): caller lookup, DOB check
             ├─ Redis: verification lockouts, per-number call rate limit, call metadata
             ├─ express-api (as the patient, short-lived JWT): slots, holds, booking, prescriptions…
             └─ spring-api (API_KEY): create phone patient
express-api ── notifications: email (Resend) OR SMS (Twilio) per user
```

### 6.2 Receptionist service (`receptionist/`)

- Python (3.14 if LangGraph and its dependencies install cleanly; otherwise pin 3.13 and say so),
  uv-managed. FastAPI + uvicorn, `langgraph`, `langchain-anthropic`, `twilio` (signature
  validation, TwiML), `psycopg` (async, pool), `redis` (asyncio), `httpx`, `pyjwt`,
  `pydantic-settings`.
- Endpoints: `POST /twilio/voice` (validates `X-Twilio-Signature`, rejects otherwise),
  `WS /twilio/relay`, `POST /twilio/status` (call status callback, for cleanup/metrics),
  `GET /` → `{"message":"Running"}`, `GET /api/health` reporting postgres/redis/express/spring
  `up`/`down`, matching the other services' conventions.
- All config from env vars (§8). Fail fast on missing required config, like express-api's `env.ts`.
- Port 7000.

### 6.3 The call flow

1. **Webhook.** Twilio posts `From`, `CallSid`. The service normalises `From` to E.164
   (`+91XXXXXXXXXX`), checks the per-number rate limit, and returns TwiML that opens
   ConversationRelay with a short bilingual welcome ("Namaste, MediCare mein aapka swagat hai.
   Welcome to MediCare…") and passes `CallSid`/caller number as custom parameters.
2. **Setup message on the WebSocket.** The service looks up `auth.users` where `role='PATIENT'`,
   `enabled`, and the phone matches. Matching compares digits only; for Indian numbers it compares
   the last 10 digits, so "98765 43210", "+91-9876543210" and "09876543210" all match. Outcome is
   one of: `none`, `one(user)`, `many(users)`. This, plus any profile DOB presence, seeds graph state.
3. **Verification** (deterministic code in tools; the model only collects the answers):
   Whenever the number has any accounts, the receptionist always asks "who am I speaking with?"
   first. That lets a different family member say they're someone else.
   - Name + DOB both match one candidate (full name case/space-insensitive, with a fuzzy match
     for transcription variants; DOB exact) → `verified`.
   - Name matches a candidate with **no DOB on file**: if it is the only account on the number,
     ask DOB, save it via `PUT /api/patients/me/profile` → `verified`. If the number has several
     accounts, it can't be verified by phone: no account actions, and the caller is pointed to the app.
   - Name matches nobody on the number (or `none`) → offer registration: collect full name and DOB,
     read back, confirm, then call spring-api's internal endpoint → `verified` as the new user.
     Before registering, if the spoken name fuzzily matches an existing account on that number, ask
     "are you already registered with us?" rather than creating a duplicate.
   - Name matches but DOB is wrong → a failed attempt (below). It never falls through to
     registration with the same name.
   - Failures: 2 wrong attempts on this call → account actions are locked for the rest of the call
     (general info still works). 5 failures for a number in 24h → that number is locked for 24h
     (Redis), the caller is told to use the app or call back later.
4. **Conversation.** The model handles everything else using tools (§6.4). Write actions always
   go: gather → read back the exact details (doctor, clinic, date, time in clinic timezone) →
   explicit yes → act → read back the result.
5. **End.** On goodbye, the model calls `end_call` (sends ConversationRelay `end`). On hang-up,
   any slot hold is released and Redis call metadata expires.

### 6.4 LangGraph graph and tools

State (per call): `call_sid`, `caller_phone`, `language` (`en`/`hi`), `match` (none/one/many with
candidate ids), `verified_user` (id, name, email, is_phone_user) or null, `failed_attempts`,
`messages`, `pending_hold` (hold id, slot), `emergency` flag.

Graph: `START → safety_screen → agent ⇄ tools → (respond) → END`, looping per caller utterance.
- `safety_screen`: a fast check on each caller utterance for emergency red flags (keyword list in
  both languages, plus the model's own instruction). If it hits, it short-circuits to a fixed
  bilingual "please call 112 immediately" message, sets `emergency`, and the agent then stays
  in a calm, minimal mode.
- `agent`: Claude with the tool set **filtered by state**: before `verified_user`, only the
  verification/registration tools and general info tools are bound. After, the full set.
  Enforcement is in code (each patient tool also checks `verified_user` and fails closed), not only
  in the prompt.
- Tool results are short structured dicts, not raw API JSON, so the model speaks concise answers.

Tools:

| Tool | Before verify | Does |
|---|---|---|
| `verify_identity(dob, full_name?)` | ✓ | §6.3 verification; increments failures |
| `register_patient(full_name, dob)` | ✓ (only when the spoken name matches no account on the number) | spring internal endpoint → set DOB via express → verified |
| `list_specializations()` | ✓ | `GET /api/specializations` |
| `find_doctors(specialization?, city?/pincode?, language?)` | ✓ | `GET /api/doctors` (uses the patient's saved location when verified) |
| `clinic_info(clinic)` | ✓ | address/timings |
| `suggest_specialization(symptoms)` | ✓ | model picks 1–2 specializations **from the live list only** + reason; never a diagnosis; red flags → safety path |
| `get_available_slots(doctor_id, date, clinic_id?)` | ✓ | `GET /api/doctors/:id/slots`; returns a few nearest options, not 40 |
| `hold_slot(doctor_id, clinic_id, start_at)` | verified | `POST /api/appointments/holds` (503 → continue without hold) |
| `book_appointment(reason?)` | verified | books the `pending_hold` slot via `POST /api/appointments`; requires a read-back confirmation turn first |
| `list_my_appointments(scope=upcoming/past)` | verified | `GET /api/appointments?from/to/order` |
| `cancel_appointment(appointment_id, reason?)` | verified | `POST /api/appointments/:id/cancel`; 2h cutoff errors are explained |
| `reschedule_appointment(appointment_id, start_at, clinic_id?)` | verified | `POST /api/appointments/:id/reschedule` |
| `list_prescriptions(limit=3)` | verified | `GET /api/patients/:id/prescriptions` |
| `read_prescription(prescription_id)` | verified | `GET /api/prescriptions/:id`; read medicines/dosage/frequency/duration exactly as written |
| `resend_confirmation(appointment_id)` | verified | new express endpoint (§7.3) |
| `end_call()` | ✓ | ends the session |

"Send the appointment confirmation" is satisfied automatically: express-api sends it on book,
cancel and reschedule, by email or SMS (§7.2). `resend_confirmation` covers "I didn't get it".

### 6.5 Redis usage (same instance, `rcpt:` prefix)

- `rcpt:verify-fail:{e164}`: failure counter, 24h TTL (cross-call DOB-guessing lockout).
- `rcpt:calls:{e164}`: calls per hour per number (abuse/cost guard; over the limit → polite
  refusal TwiML).
- `rcpt:call:{CallSid}`: small call metadata (verified user id, language), TTL 1h, for the status
  callback and debugging.
- Slot holds are express-api's existing Redis holds, created through its API, not duplicated.
- Everything fails open except the lockout counter. If Redis is down, verification still works
  but with only the per-call 2-attempt limit, and this is logged as a warning.

### 6.6 Model, prompting, language

- `ChatAnthropic` (langchain-anthropic) with **`claude-opus-5-5`** (the current default model),
  streaming, **low effort** for conversational latency. The model choice is configurable
  (`LLM_MODEL`). Moving to a faster/cheaper model (e.g. Haiku 5.5) is the owner's call after the
  latency test in the spike.
- Server-side refusal fallback enabled (`fallbacks: "default"` beta) so a classifier decline
  doesn't kill a call. If a refusal still happens, the caller gets a fixed polite line.
- Stable system prompt (cached): persona (warm, brief, one question at a time, spoken style: no
  lists or markdown, numbers and times said naturally), safety rules, verification rules, the
  confirm-before-write rule, privacy rules, clinic timezone handling. Volatile facts (today's date
  in Asia/Kolkata, caller match state) go in a per-turn system message, so the cache isn't broken.
- Language: reply in the caller's language. Hindi replies are written in Devanagari for TTS quality,
  with medicine names and doctor names kept as-is. When the model's language changes, the
  service sends ConversationRelay a `language` switch message (both TTS and STT). Hinglish input is
  normal and must not trigger flip-flopping. Switching requires two consecutive Hindi/English
  turns, or an explicit request.
- Responses are streamed token-by-token to ConversationRelay (`text` messages with `last` flag) so
  the caller hears the start of a sentence quickly. While a tool runs, a short filler is spoken
  ("ek second, main check karti hoon" / "one moment, let me check").
- Interrupts: on ConversationRelay `interrupt`, the in-flight generation is cancelled and the
  partial assistant message is trimmed to what was actually spoken (`utteranceUntilInterrupt`).

### 6.7 Acting as the patient against express-api

The receptionist mints a **short-lived (5 min) HS256 JWT** exactly like spring-api's
(`sub`, `email`, `name`, `role=PATIENT`, `iss`, `aud`, `exp`) using `JWT_SECRET`, only after
verification, only with `role=PATIENT`, only for the verified user's id. This sits behind a
`TokenIssuer` interface so it can later switch to asking spring-api for a token once spring's
`JwtService` exists. Holding `JWT_SECRET` is a real trust grant, and is recorded in §10.

Express-api's per-user rate limits apply naturally per patient.

## 7. Changes outside `receptionist/`

### 7.1 spring-api
- `POST /api/internal/phone-patients` (header `X-Api-Key: $API_KEY`; constant-time compare),
  body `{fullName, phoneE164}` → creates `auth.users` with role `PATIENT`, `phone=phoneE164`,
  `email = "p-<uuid>@phone.medicare.invalid"` (`.invalid` is a reserved TLD that can never deliver),
  `password_hash` = BCrypt of a random 32-byte secret (no usable password), `email_verified=false`.
  Returns `{id, email, fullName}`. Several patients may share a phone, so an existing phone is
  **not** a conflict. The body carries an `idempotencyKey` (the `CallSid`), so a retried request
  returns the same user instead of creating a second one. The receptionist then sets the DOB through
  express-api acting as the new user. If that step fails, the receptionist retries it on the call,
  and the account still exists without a DOB.
- `SecurityConfig` currently permits everything; this endpoint enforces the API key itself so it
  stays protected when security is tightened.
- No schema change. (A phone-format/uniqueness migration is future work, §11.)

### 7.2 express-api: SMS channel
- New `services/sms.service.ts` using the Twilio REST API (`TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM` or a Messaging Service SID). Like the mailer: never throws,
  retries 429/5xx, and without credentials it logs and records `SKIPPED`.
- `notifications.service.ts` picks the channel per recipient: if the patient email ends with
  `@phone.medicare.invalid` (constant `PHONE_PLACEHOLDER_DOMAIN`), send SMS to `auth.users.phone`,
  else email. This covers BOOKED/CANCELLED/RESCHEDULED/REMINDER, so the 24h cron also texts phone users.
  SMS bodies are short and bilingual-safe (English, or the patient's stored language later).
  Doctor notifications stay email.
- Migration `003_notifications_channel.sql`: add `channel text NOT NULL DEFAULT 'EMAIL' CHECK
  (channel IN ('EMAIL','SMS'))` to `app.notifications_log`.
- Never send anything to a placeholder address. Add a guard in `sendMail` itself as a
  belt-and-braces check.
- When the patient later adds a real email (future app flow), they switch to email automatically.

### 7.3 express-api: resend confirmation
- `POST /api/appointments/:id/resend-confirmation` (PATIENT for own appointment; RECEPTIONIST/ADMIN
  per existing access rules), rate-limited, re-sends the BOOKED/RESCHEDULED notice for an active
  appointment.

### 7.4 Workspace plumbing
- `.github/docker-compose.yml`: add `receptionist` service (depends on postgres, redis, express-api,
  spring-api). Local Twilio testing uses a tunnel (ngrok/cloudflared) set in `PUBLIC_BASE_URL`.
- Postgres read-only role for the receptionist (`SELECT` on `auth.users` and
  `app.patient_profiles`), created by an express migration or the infra bootstrap.
  Locally it may reuse the main user.
- CLAUDE.md env-var contract updated.

### 7.5 AWS deployment (in scope)
- **Terraform (`infra`)**: ElastiCache Redis (single node, cluster mode off, in-VPC, TLS on,
  security group open to the EKS nodes only). express-api needs it too, for slot holds and
  rate limits. Its URL goes into the `cdac-dev/app` secret as `REDIS_URL` (`rediss://`).
- New Secrets Manager keys in `cdac-dev/app`: the Twilio keys, `ANTHROPIC_API_KEY`,
  `PUBLIC_BASE_URL`, plus the keys CLAUDE.md already lists as missing (`JWT_ISSUER`,
  `JWT_AUDIENCE`, `RESEND_API_KEY`, `MAIL_FROM`, `MAIL_REPLY_TO`, `APP_BASE_URL`, `ADMIN_*`).
- Read-only Postgres role on RDS for the receptionist (`SELECT` on `auth.users`,
  `app.patient_profiles`), with its own `DATABASE_URL` in a receptionist-only secret.
- ECR repository for the receptionist image.
- **Helm**: add `receptionist` to `.Values.apps` on its own ALB listener port. HTTPS is required,
  because Twilio needs a public `https`/`wss` URL with a valid certificate. That means an ACM
  certificate and a DNS name for the receptionist, and the ALB idle timeout raised to 3600s for
  WebSockets. Probes use `/` and `/api/health`.
- Graceful shutdown: on SIGTERM, stop accepting new calls and let live calls finish (pod
  `terminationGracePeriodSeconds` up to ~15 min).

## 8. Environment variables

Receptionist: `PORT`, `APP_ENV`, `PUBLIC_BASE_URL` (https origin Twilio reaches; used for
signature validation and the wss URL), `DATABASE_URL` (read-only role), `REDIS_URL`,
`EXPRESS_API_URL`, `SPRING_API_URL`, `API_KEY`, `JWT_SECRET`, `JWT_ISSUER`, `JWT_AUDIENCE`,
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `LLM_MODEL` (default
`claude-opus-5-5`), `DEFAULT_TIMEZONE` (`Asia/Kolkata`), `VERIFY_MAX_ATTEMPTS_PER_CALL` (2),
`VERIFY_MAX_FAILURES_PER_DAY` (5), `CALLS_PER_HOUR_PER_NUMBER` (10).
ConversationRelay voice/provider settings: `TTS_PROVIDER`, `TTS_VOICE_EN`, `TTS_VOICE_HI`,
`STT_PROVIDER`, `STT_MODEL`.

express-api (new): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM` /
`TWILIO_MESSAGING_SERVICE_SID`.

spring-api: uses existing `API_KEY`.

## 9. Build order and testing

0. **Spike (throwaway):** a bare ConversationRelay echo-to-Claude call with `hi-IN` and `en-IN`.
   It measures Hindi STT accuracy (names, dates, "Pantoprazole 40 mg"), Hindi voice naturalness, and
   time-to-first-audio with `claude-opus-5-5` at low effort. Go/no-go for ConversationRelay vs
   Media Streams, and the model latency decision. Pass marks:
   - time from end of caller speech to first audio: p50 ≤ 1.5 s, p90 ≤ 2.5 s;
   - ≥ 90% of Hindi names and dates transcribed correctly on a fixed 30-utterance script (same
     script in English);
   - the owner listens to the Hindi voice and judges it natural enough.

   Results go back to the owner, who decides the model (e.g. keep Opus 5.5 vs a faster one) and,
   if STT/voice fails, approves switching to Media Streams. The builder doesn't switch on their own.
1. Receptionist skeleton: config, health, Twilio webhook with signature validation, WS relay
   loop.
2. Identity: phone normalisation, lookup, verification, lockouts (pure functions + Redis).
3. spring internal endpoint + registration tool.
4. Graph + read-only tools, then write tools with confirm-before-write and holds.
5. express-api SMS channel + migration + resend endpoint.
6. Compose wiring and docs.
7. AWS: Terraform (ElastiCache, secrets, ECR, read-only role, cert/DNS), Helm app entry, deploy
   to `cdac-dev`, then a real test call.

Testing:
- Pure unit tests (pytest): phone normalisation, DOB parsing from spoken forms ("5 March 1990",
  "paanch March unnees sau nabbe", "05/03/1990"), name matching, lockout counters, tool gating.
- Graph tests with a scripted fake chat model: verify-before-tools gating, confirm-before-write,
  emergency short-circuit, wrong-DOB lockout, family-number disambiguation.
- Contract tests of the express/spring HTTP clients against recorded responses.
- A **text-mode CLI** (`uv run receptionist chat --from +9198…`) that runs the real graph against a
  local stack without Twilio, for manual and scenario testing in both languages.
- express-api: `bun test` for channel selection and the placeholder-email guard. spring-api:
  `./mvnw test` for the internal endpoint (auth, creation, 409).

## 10. Risks and owner actions (must be resolved before real patients call)

- **Caller ID + DOB is weak authentication.** Caller ID can be spoofed, and DOBs are often known
  to others. Decided with that trade-off. Mitigations: lockouts, no sensitive data read before
  verification, audit of every action (express audit log). Prescriptions are the most sensitive
  thing exposed.
- **First-call DOB setup** on an existing account without DOB trusts caller ID alone for that
  call.
- **Twilio India:** provisioning a number that Indian callers can reach, KYC/regulatory bundle,
  and **DLT registration for SMS** (entity, sender ID/header, approved templates). Without DLT,
  SMS confirmations to Indian numbers will be blocked. SMS templates in §7.2 must match the
  registered DLT templates.
- **Privacy/consent:** an opening disclosure that the caller is talking to an automated assistant.
  No call recording by default. Transcripts are not persisted; logs exclude DOB and prescription
  contents. India's DPDP Act obligations are the owner's to confirm.
- **Receptionist holds `JWT_SECRET`**, so it can act as any user. Limited in code to PATIENT
  tokens for the verified caller. It should move to spring-issued tokens when spring's auth exists.
- **Hindi quality** depends on ConversationRelay's providers (spike gate).
- **Cost:** an LLM per turn + Twilio voice minutes + STT/TTS + SMS. The per-number call rate limit
  caps abuse.

## 11. Left to the builder / future work

- Exact prompt wording, filler phrases, the emergency keyword lists (reviewed for both
  languages), slot presentation (how many options, phrasing of times).
- Library versions and the Python version pin (3.14 vs 3.13), based on what installs.
- Read-only DB role mechanics per environment.
- Future: unique, normalised `auth.users.phone` (Flyway migration + backfill), SMS OTP step-up
  for prescriptions, outbound reminder calls, transfer to staff, Redis-backed checkpointer, letting
  phone users add an email and a password from the app.
