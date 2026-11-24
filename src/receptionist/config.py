"""Environment-driven settings. Fails fast on missing required config."""

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Required outside APP_ENV=test.
_REQUIRED_OUTSIDE_TEST = (
    "database_url",
    "redis_url",
    "twilio_auth_token",
    "anthropic_api_key",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: int = 7000
    app_env: str = "development"
    public_base_url: str
    database_url: str | None = None
    redis_url: str | None = None
    # Same Redis keys and TTLs as express-api: holds and the slot cache are shared with it.
    slot_hold_ttl_seconds: int = Field(300, ge=30, le=900)
    slot_cache_ttl_seconds: int = Field(60, ge=5, le=3600)

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # Appointment confirmations. SMS goes to phone-registered patients; email (Resend) to the
    # rest and to doctors. Unset means the message is only logged (SKIPPED).
    twilio_sms_from: str | None = None
    twilio_messaging_service_sid: str | None = None
    resend_api_key: str | None = None
    mail_from: str = "MediCare <onboarding@resend.dev>"
    mail_reply_to: str | None = None
    app_base_url: str = "http://localhost:3000"
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    default_timezone: str = "Asia/Kolkata"

    verify_max_attempts_per_call: int = 2
    verify_max_failures_per_day: int = 5
    calls_per_hour_per_number: int = 10

    tts_provider: str = "ElevenLabs"
    tts_voice_en: str | None = None
    tts_voice_hi: str | None = None
    stt_provider: str = "Deepgram"
    stt_model: str = "nova-3-general"

    @model_validator(mode="after")
    def _require_config(self) -> Self:
        if self.app_env != "test":
            missing = [n.upper() for n in _REQUIRED_OUTSIDE_TEST if not getattr(self, n)]
            if missing:
                raise ValueError(f"missing required env vars: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
