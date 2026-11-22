"""The per-call LangGraph graph: safety screen, then the agent and its tools."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from receptionist.agent.context import CallContext
from receptionist.agent.prompts import SYSTEM_PROMPT, turn_context
from receptionist.agent.safety import EMERGENCY_MESSAGE, detect_emergency
from receptionist.agent.tools import tools_for

log = logging.getLogger(__name__)

REFUSAL_LINE = "Sorry, I can't help with that on the phone."


class CallState(MessagesState):
    emergency: bool


def _utcnow() -> datetime:
    return datetime.now(UTC)


def bind(llm: BaseChatModel, tools: list[BaseTool]) -> Runnable:
    """Tool choice stays auto (forced choice is rejected with thinking on). One tool call per
    message, so read-back and write steps can't run side by side."""
    return llm.bind_tools(tools, parallel_tool_calls=False)


def system_message(ctx: CallContext, now: datetime) -> SystemMessage:
    return SystemMessage(
        content=[
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": turn_context(ctx, now)},
        ]
    )


def build_graph(
    llm: BaseChatModel,
    ctx: CallContext,
    tools: list[BaseTool],
    *,
    now: Callable[[], datetime] = _utcnow,
) -> CompiledStateGraph:
    async def safety(state: CallState) -> dict:
        last = state["messages"][-1]
        text = last.text if isinstance(last, HumanMessage) else ""
        if detect_emergency(text):
            ctx.emergency = True
            log.info("emergency message given call=%s turn=%s", ctx.call_sid, ctx.turn)
            message = AIMessage(content=EMERGENCY_MESSAGE[ctx.language.current])
            return {"emergency": True, "messages": [message]}
        return {"emergency": False}

    async def agent(state: CallState) -> dict:
        model = bind(llm, tools_for(ctx, tools))
        response = await model.ainvoke([system_message(ctx, now()), *state["messages"]])
        if response.response_metadata.get("stop_reason") == "refusal":
            log.warning("model refused call=%s turn=%s", ctx.call_sid, ctx.turn)
            # The refused message (and any thinking in it) is dropped from the history.
            response = AIMessage(content=REFUSAL_LINE)
        return {"messages": [response]}

    graph = StateGraph(CallState)
    graph.add_node("safety", safety)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "safety")
    graph.add_conditional_edges(
        "safety", lambda s: END if s["emergency"] else "agent", {END: END, "agent": "agent"}
    )
    graph.add_conditional_edges("agent", tools_condition)
    # After end_call the call is over: no further model call.
    graph.add_conditional_edges(
        "tools", lambda _: END if ctx.end_requested else "agent", {END: END, "agent": "agent"}
    )
    return graph.compile(checkpointer=InMemorySaver())
