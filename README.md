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
each `up`, `down` or `disabled`.

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
| `STT_PROVIDER`, `STT_MODEL` | `Deepgram`, `nova-3` | ConversationRelay speech-to-text |

**Keep LangSmith tracing off.** Don't set `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2`: traces
would record tool arguments (dates of birth) and tool results (prescriptions). Logs never
contain transcripts, dates of birth or prescription text, and phone numbers are masked.
