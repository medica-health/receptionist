# receptionist

Twilio voice receptionist for the MediCare appointment system. A Python service (FastAPI,
LangGraph, Twilio ConversationRelay) that answers calls, verifies the caller against
spring-api's `auth.users`, and books/changes appointments through express-api.

Design: `docs/superpowers/specs/2026-11-22-voice-receptionist-design.md`.

Python 3.14 (all dependencies install cleanly on it; no 3.13 fallback was needed). Managed with
[uv](https://docs.astral.sh/uv/).

## Run

```bash
cp .env.example .env     # fill in values
uv sync
uv run receptionist serve   # http://localhost:7000
```

`GET /` returns `{"message":"Running"}`. `GET /api/health` returns the standard envelope
`{success, message, data, timestamp}` with `data.dependencies` (postgres, redis, express, spring)
each `up`, `down` or `disabled`. `GET /ready` is the cheap readiness check for Kubernetes and
the ALB: it calls no dependency and returns 200, or 503 while draining.

## Voice (Twilio ConversationRelay)

| Endpoint | What it does |
|---|---|
| `POST /twilio/voice` | Call webhook. Checks `X-Twilio-Signature` (403 otherwise), returns 503 while draining, applies the per-number rate limit (`CALLS_PER_HOUR_PER_NUMBER`; over it, a bilingual "too many calls" message and hang-up), then returns `<Connect><ConversationRelay>` TwiML with `en-IN` and `hi-IN` `<Language>`s. Withheld or invalid caller ID still connects, for general information only. |
| `WS /twilio/relay` | The ConversationRelay socket. The handshake must carry a valid Twilio signature. |
| `POST /twilio/status` | Call status callback (signed). On `completed`, `failed`, `no-answer`, `busy` or `canceled` it releases any slot still held for the call. |

Every signature is checked against `PUBLIC_BASE_URL` + path, never the request's Host, so
`PUBLIC_BASE_URL` must be exactly the origin configured in Twilio.

**Graceful drain.** The first SIGTERM doesn't stop the server: `/twilio/voice`, `/ready` and
`/api/health` return 503 (so the load balancer stops sending new calls) while live calls carry
on. The process exits when no call is open (after at least 30 s), or after 13 minutes at most;
a second SIGTERM or Ctrl+C exits at once. Deployment requirements for this to work:

- The pod's `terminationGracePeriodSeconds` must be at least 900.
- The ALB target group's `deregistration_delay.timeout_seconds` must be at least 780 (the
  default 300 drops live calls 5 minutes into a drain).
- The readiness probe and the ALB health check use `/ready` (not `/api/health`, which waits on
  each dependency and would fail readiness on every replica when one is slow); the **liveness**
  probe must use `/`, because `/ready` returns 503 while draining and a failing liveness probe
  would kill the pod.
- Set the Twilio number's fallback URL ("Primary handler fails") to the same
  `<PUBLIC_BASE_URL>/twilio/voice`, so a call that hits a draining pod is retried.

**Relay token.** ConversationRelay's `setup` message is unsigned, so the signed
`/twilio/voice` response carries a `relayToken` `<Parameter>` (HMAC-SHA256 over
`callSid|caller|expiry` with `TWILIO_AUTH_TOKEN`, valid 5 minutes). The relay takes the CallSid
and caller number only from that token; a setup without a valid token, or for another CallSid,
is closed with 1008.

### Local phone test

Needs a Twilio number with ConversationRelay enabled (Console → Voice → Settings), the stack
running (Postgres, Redis, express-api, spring-api), a real `ANTHROPIC_API_KEY`, and ElevenLabs
voice ids for `TTS_VOICE_EN` / `TTS_VOICE_HI` (`<voice id>-v4`; pick a Hindi-capable voice for
`TTS_VOICE_HI`).

1. Start a tunnel to port 7000: `cloudflared tunnel --url http://localhost:7000` (or
   `ngrok http 7000`). Note the `https://…` URL.
2. In `.env` set `PUBLIC_BASE_URL=<that https URL>` (no trailing slash), `TWILIO_AUTH_TOKEN`
   (Console → Account → API keys & tokens → Auth token) and the voice ids. Run
   `uv run receptionist serve`.
3. Console → Phone Numbers → your number → Voice Configuration: "A call comes in" → Webhook,
   `HTTP POST`, `<PUBLIC_BASE_URL>/twilio/voice`; "Call status changes" →
   `<PUBLIC_BASE_URL>/twilio/status`, `HTTP POST`.
4. Call the number from a phone whose number is on a seeded patient (`auth.users.phone`), and
   once from an unknown number. Repeat the text-mode scenarios by voice, in English and in
   Hindi: verification (name + DOB), booking with read-back and "yes", cancel, reschedule,
   prescriptions, switching language mid-call, an emergency phrase, talking over the
   receptionist (it should stop and carry on from what you heard), and saying goodbye (the
   call should end).
5. Hang up in the middle of a booking read-back; the held slot should be free again in the app.
6. A 403 in the logs ("without a valid Twilio signature") means `PUBLIC_BASE_URL` doesn't match
   the URL configured in Twilio, or the auth token is wrong.

Text-mode call against the real stack (Postgres, Redis, both APIs, a real `ANTHROPIC_API_KEY`):

```bash
uv run receptionist chat --from +919876543210 [--call-sid cli-<uuid>]
```

Each stdin line is one caller utterance. Fillers print in brackets, language switches as
`[switch → hi]` and the end of the call as `[end]`.

## Test

```bash
uv run pytest
uv run ruff check .
```

Tests set `APP_ENV=test`, which relaxes the required-secret checks.

## Docker

```bash
docker build -t receptionist .
docker run --env-file .env -p 7000:7000 receptionist
```

## Environment variables

Missing required variables make startup fail (outside `APP_ENV=test`).

| Variable | Default | Notes |
|---|---|---|
| `PORT` | `7000` | |
| `APP_ENV` | `development` | `test` relaxes required secrets |
| `PUBLIC_BASE_URL` | required | https origin Twilio reaches |
| `DATABASE_URL` | required | read-only Postgres role |
| `REDIS_URL` | required | |
| `EXPRESS_API_URL`, `SPRING_API_URL` | required | |
| `API_KEY` | required | |
| `JWT_SECRET` | required | at least 32 chars, same as the APIs |
| `JWT_ISSUER`, `JWT_AUDIENCE` | `cdac-auth`, `cdac-api` | |
| `TWILIO_ACCOUNT_SID` | optional | |
| `TWILIO_AUTH_TOKEN` | required | |
| `ANTHROPIC_API_KEY` | required | |
| `LLM_MODEL` | `claude-opus-5-5` | |
| `DEFAULT_TIMEZONE` | `Asia/Kolkata` | |
| `VERIFY_MAX_ATTEMPTS_PER_CALL` | `2` | |
| `VERIFY_MAX_FAILURES_PER_DAY` | `5` | |
| `CALLS_PER_HOUR_PER_NUMBER` | `10` | |
| `TTS_PROVIDER`, `TTS_VOICE_EN`, `TTS_VOICE_HI` | `ElevenLabs`, unset | ConversationRelay voice |
| `STT_PROVIDER`, `STT_MODEL` | `Deepgram`, `nova-3-general` | ConversationRelay speech-to-text (`speechModel`) |

**Keep LangSmith tracing off.** Don't set `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2`: traces
would record tool arguments (dates of birth) and tool results (prescriptions). Logs never
contain transcripts, dates of birth or prescription text, and phone numbers are masked.
