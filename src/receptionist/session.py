"""One phone call, independent of the transport: text in, speech pieces out."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from receptionist.agent.context import CallContext
from receptionist.agent.graph import REFUSAL_LINE, build_graph
from receptionist.agent.language import FILLER, LanguageTracker, utterance_language
from receptionist.agent.safety import Lang
from receptionist.agent.tools import ToolDeps, build_tools
from receptionist.app import AppDeps
from receptionist.config import get_settings
from receptionist.identity.phone import mask_phone, normalize_phone
from receptionist.identity.verify import Candidate, Verifier

__all__ = ["APOLOGY", "REFUSAL_LINE", "CallSession", "Speech"]

log = logging.getLogger(__name__)

APOLOGY: dict[Lang, str] = {
    "en": "Sorry, I'm having trouble right now. Could you say that again?",
    "hi": "माफ़ कीजिए, अभी थोड़ी दिक्कत हो रही है। क्या आप फिर से बता सकते हैं?",
}
INTERRUPTED = (
    "Interrupted by the caller; the outcome is unknown. Check (for example with "
    "list_my_appointments) before retrying."
)


@dataclass
class Speech:
    kind: Literal["token", "filler", "switch_language", "end"]
    text: str = ""
    lang: Lang | None = None


def _text(content: Any) -> str:
    """Spoken text of a message or chunk: text blocks only, never thinking or tool input."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def _unanswered(messages: list[BaseMessage]) -> list[ToolMessage]:
    """Tool results for tool calls left unanswered by an interrupted turn. Every tool_use
    needs a tool_result in the next message, or the API rejects the history."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        ToolMessage(content=INTERRUPTED, tool_call_id=tc["id"], name=tc["name"], status="error")
        for m in messages
        if isinstance(m, AIMessage)
        for tc in m.tool_calls
        if tc["id"] not in answered
    ]


class CallSession:
    def __init__(
        self,
        ctx: CallContext,
        graph: CompiledStateGraph,
        llm: BaseChatModel,
        deps: AppDeps,
    ) -> None:
        self.ctx = ctx
        self.graph = graph
        self.llm = llm
        self.deps = deps
        self.config: RunnableConfig = {"configurable": {"thread_id": ctx.call_sid}}
        self._ended = False

    @classmethod
    async def start(
        cls,
        call_sid: str,
        raw_from: str | None,
        deps: AppDeps,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> CallSession:
        settings = deps.settings or get_settings()
        e164 = normalize_phone(raw_from)
        candidates: list[Candidate] = []
        locked = False
        if e164 is not None:
            try:
                candidates = await deps.directory.find_by_phone(e164)
            except Exception as exc:
                # Fail closed: an empty list would look like a new caller and invite a
                # duplicate registration. General information still works.
                log.warning("caller lookup failed for %s: %s", mask_phone(e164), type(exc).__name__)
                locked = True
            if not locked:
                locked = await deps.guards.is_locked(e164)
        ctx = CallContext(
            call_sid=call_sid,
            caller_e164=e164,
            candidates=candidates,
            verifier=Verifier(candidates, settings.verify_max_attempts_per_call),
            language=LanguageTracker(),
            locked=locked,
        )
        tool_deps = ToolDeps(
            directory=deps.directory,
            guards=deps.guards,
            backend=deps.backend,
            settings=settings,
        )
        if clock is not None:
            tool_deps.clock = clock
        tools = build_tools(ctx, tool_deps)
        llm = deps.llm_factory()
        graph = build_graph(llm, ctx, tools, now=tool_deps.clock)
        return cls(ctx, graph, llm, deps)

    async def respond(self, text: str, stt_lang: str | None = None) -> AsyncIterator[Speech]:
        """Run one caller turn and stream what to say. Blank input is ignored."""
        text = text.strip()
        if not text or self._ended:
            return
        ctx = self.ctx
        ctx.turn += 1
        announced = ctx.language.current

        def language_change() -> Speech | None:
            nonlocal announced
            if ctx.language.current == announced:
                return None
            announced = ctx.language.current
            return Speech("switch_language", lang=announced)

        ctx.stt_lang = stt_lang
        ctx.utterance_lang = utterance_language(text, stt_lang)
        ctx.language.observe(text, stt_lang)
        if switch := language_change():
            yield switch

        state = await self.graph.aget_state(self.config)
        inputs: list[BaseMessage] = _unanswered(state.values.get("messages", []))
        inputs.append(HumanMessage(content=text))
        said = False
        filler = False
        stream = self.graph.astream(
            {"messages": inputs}, self.config, stream_mode=["messages", "updates"]
        )
        try:
            # aclosing: if this generator is closed or cancelled, the graph run (and any
            # tool it started) is torn down before respond returns, not left running.
            async with aclosing(stream):
                async for mode, chunk in stream:
                    if mode == "updates":
                        safety = chunk.get("safety") if isinstance(chunk, dict) else None
                        if safety and safety.get("emergency"):
                            # A Hindi emergency forces Hindi: switch TTS before the message.
                            if switch := language_change():
                                yield switch
                            for message in safety.get("messages", []):
                                said = True
                                yield Speech("token", _text(message.content))
                        # switch_language changes the language inside the tools node.
                        if switch := language_change():
                            yield switch
                        continue
                    message, meta = chunk
                    if meta.get("langgraph_node") != "agent" or not isinstance(message, AIMessage):
                        continue
                    if piece := _text(message.content):
                        said = True
                        yield Speech("token", piece)
                    starts_tool = getattr(message, "tool_call_chunks", None) or message.tool_calls
                    if starts_tool and not said and not filler:
                        filler = True
                        yield Speech("filler", FILLER[ctx.language.current])
        except Exception as exc:
            # Never log the message: it may carry what the caller said.
            log.warning("turn failed call=%s: %s", ctx.call_sid, type(exc).__name__)
            yield Speech("token", APOLOGY[ctx.language.current])
        if switch := language_change():
            yield switch
        if ctx.end_requested and not self._ended:
            self._ended = True
            yield Speech("end")

    async def trim_last_reply(self, spoken_text: str) -> None:
        """After a barge-in, keep only what the caller heard of the last reply. Thinking
        blocks stay unchanged (the API needs them back as they were). Does nothing when the
        last message isn't a finished reply, e.g. the turn was cut off before it ended."""
        state = await self.graph.aget_state(self.config)
        messages = state.values.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].tool_calls:
            return
        last = messages[-1]
        spoken = spoken_text.strip() or "..."
        if isinstance(last.content, str):
            content: str | list = spoken
        else:
            content, placed = [], False
            for block in last.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    if not placed:
                        content.append({"type": "text", "text": spoken})
                        placed = True
                    continue
                content.append(block)
            if not placed:
                content.append({"type": "text", "text": spoken})
        await self.graph.aupdate_state(
            self.config,
            {"messages": [last.model_copy(update={"content": content})]},
            as_node="agent",
        )
