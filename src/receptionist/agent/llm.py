"""The Claude chat model used for every call.

How each request field reaches the API with langchain-anthropic 1.7 (read from its source,
`ChatAnthropic._get_request_payload`):
- `output_config` and `betas` are ChatAnthropic fields and are copied into the payload. A
  payload with `betas` is sent through `client.beta.messages.create`.
- `fallbacks` has no ChatAnthropic field. Everything in `model_kwargs` is spread into the
  payload as top-level keys, and the beta `messages.create` of anthropic 2.x accepts
  `fallbacks` as a named parameter.
- `thinking` is never set: it can't be disabled on claude-opus-5-5, and adaptive thinking is
  the default. Depth is controlled by `output_config.effort` only. (Setting
  `reasoning_effort` instead would also make langchain-anthropic send an explicit
  summarized-thinking config, which we don't want.)

Tracing: LangSmith tracing must stay off (don't set LANGSMITH_TRACING / LANGCHAIN_TRACING_V2).
Tool arguments carry dates of birth and tool results carry prescriptions.
"""

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from receptionist.config import Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 4096
REQUEST_TIMEOUT_S = 30.0
# One retry covers a transient overload; with the 30 s timeout the caller hears the
# apology within about 60 s in the worst case instead of sitting through 3 attempts.
MAX_RETRIES = 1


def make_llm(settings: Settings) -> BaseChatModel:
    kwargs = {}
    if settings.anthropic_api_key:
        kwargs["api_key"] = settings.anthropic_api_key
    return ChatAnthropic(
        model=settings.llm_model,
        max_tokens=MAX_TOKENS,
        streaming=True,
        output_config={"effort": "low"},
        betas=[FALLBACK_BETA],
        model_kwargs={"fallbacks": "default"},
        default_request_timeout=REQUEST_TIMEOUT_S,
        max_retries=MAX_RETRIES,
        **kwargs,
    )
