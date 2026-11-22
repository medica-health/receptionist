from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from receptionist.agent.graph import bind
from receptionist.agent.llm import FALLBACK_BETA, make_llm
from receptionist.config import Settings


def payload(*messages):
    return make_llm(Settings())._get_request_payload(list(messages))


def test_payload_carries_model_effort_and_fallbacks():
    p = payload(HumanMessage("hi"))
    assert p["model"] == "claude-opus-5-5"
    assert p["max_tokens"] == 4096
    assert p["output_config"]["effort"] == "low"
    assert p["fallbacks"] == "default"
    assert FALLBACK_BETA in p["betas"]
    # Thinking can't be disabled on this model, and forced tool choice is rejected.
    assert "thinking" not in p
    assert "tool_choice" not in p


def test_streaming_is_on_with_bounded_retries_and_timeout():
    llm = make_llm(Settings())
    assert llm.streaming is True
    # Worst case before the caller hears the apology: 2 attempts x 30 s.
    assert llm.max_retries == 1
    assert llm.default_request_timeout == 30.0


def test_system_cache_control_reaches_the_payload():
    system = SystemMessage(
        content=[
            {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "volatile"},
        ]
    )
    p = payload(system, HumanMessage("hi"))
    assert p["system"][0] == {
        "type": "text",
        "text": "stable",
        "cache_control": {"type": "ephemeral"},
    }
    assert "cache_control" not in p["system"][1]


def test_bound_tools_use_auto_choice_without_parallel_calls():
    llm = make_llm(Settings())

    async def noop() -> str:
        return "ok"

    tool = StructuredTool.from_function(coroutine=noop, name="noop", description="d")
    bound = bind(llm, [tool])
    p = llm._get_request_payload([HumanMessage("hi")], **bound.kwargs)
    assert p["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
