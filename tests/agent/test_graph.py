from datetime import date

import pytest
from fakes import NOW, FakeDirectory, FakeGuards, call, make_app_deps, say
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from receptionist.agent.language import FILLER
from receptionist.agent.safety import EMERGENCY_MESSAGE
from receptionist.identity.verify import Candidate
from receptionist.session import APOLOGY, REFUSAL_LINE, CallSession, Speech

DOB = date(1990, 1, 2)
ABHINAV = Candidate("u1", "Abhinav Singh", "a@x.y", DOB)
CALLER = "+919876543210"


async def start(script=None, candidates=None, raw_from=CALLER, **kw) -> CallSession:
    deps = make_app_deps(candidates=candidates, script=script, **kw)
    return await CallSession.start("CA1", raw_from, deps, clock=lambda: NOW)


async def turn(session: CallSession, text: str, stt_lang: str | None = None) -> list[Speech]:
    return [s async for s in session.respond(text, stt_lang)]


def spoken(out: list[Speech]) -> str:
    return "".join(s.text for s in out if s.kind == "token")


def kinds(out: list[Speech]) -> list[str]:
    return [s.kind for s in out]


def model(session: CallSession):
    return session.llm


async def history(session: CallSession):
    state = await session.graph.aget_state(session.config)
    return state.values["messages"]


# ---------------------------------------------------------------- start


async def test_start_looks_up_candidates_and_lock():
    s = await start(candidates=[ABHINAV], guards=FakeGuards(locked=True))
    assert s.ctx.caller_e164 == CALLER
    assert s.ctx.candidates == [ABHINAV]
    assert s.ctx.locked is True
    assert s.ctx.call_sid == "CA1"


async def test_start_with_withheld_caller_id_skips_lookup():
    directory = FakeDirectory([ABHINAV])
    s = await start(raw_from="anonymous", directory=directory)
    assert s.ctx.caller_e164 is None
    assert s.ctx.candidates == []
    assert directory.lookups == []


async def test_start_fails_closed_when_the_directory_is_down():
    s = await start(directory=FakeDirectory(error=OSError("db down")))
    # No candidates must not read as "unknown number, offer registration".
    assert s.ctx.candidates == []
    assert s.ctx.locked is True


# ---------------------------------------------------------------- emergency


async def test_emergency_short_circuits():
    s = await start(script=[])
    out = await turn(s, "I have chest pain")
    assert spoken(out) == EMERGENCY_MESSAGE["en"]
    assert model(s).calls == []
    assert s.ctx.emergency is True
    assert s.ctx.turn == 1


async def test_emergency_message_in_hindi():
    s = await start(script=[])
    s.ctx.language.force("hi")
    out = await turn(s, "सीने में दर्द हो रहा है")
    assert spoken(out) == EMERGENCY_MESSAGE["hi"]


async def test_emergency_does_not_lock_the_call():
    s = await start(script=[say("I'm glad you're safe. How can I help?")])
    await turn(s, "my father had a stroke last year")
    out = await turn(s, "no, he is fine now, I want an appointment")
    assert spoken(out) == "I'm glad you're safe. How can I help?"
    sent = model(s).calls[0]
    system = sent[0]
    assert isinstance(system, SystemMessage)
    assert "safe" in system.content[1]["text"]
    # The model sees that the 112 message was given.
    assert any(isinstance(m, AIMessage) and "112" in m.text for m in sent)


# ---------------------------------------------------------------- system prompt


async def test_system_prompt_is_cached_and_turn_context_volatile():
    s = await start(script=[say("Hello.")])
    await turn(s, "hello")
    system = model(s).calls[0][0]
    stable, volatile = system.content
    assert stable["cache_control"] == {"type": "ephemeral"}
    assert "MediCare" in stable["text"]
    assert "cache_control" not in volatile
    assert "Caller not verified." in volatile["text"]
    assert "1990" not in volatile["text"]


# ---------------------------------------------------------------- tools


async def test_verify_then_list_appointments():
    s = await start(
        candidates=[ABHINAV],
        script=[
            call("verify_identity", full_name="Abhinav Singh", dob="1990-01-02"),
            say("Thanks Abhinav, how can I help?"),
            call("list_my_appointments", scope="upcoming"),
            say("You have one appointment with Dr. Meera Rao."),
        ],
    )
    out1 = await turn(s, "I'm Abhinav Singh, born 2 January 1990")
    assert spoken(out1) == "Thanks Abhinav, how can I help?"
    assert s.ctx.verified == ABHINAV
    out2 = await turn(s, "what are my upcoming appointments")
    assert spoken(out2) == "You have one appointment with Dr. Meera Rao."
    express = s.deps.express
    assert express.names() == ["as_patient", "list_appointments"]
    # Patient tools are bound only after verification; identity tools disappear.
    bound = model(s).bound
    assert "list_my_appointments" not in bound[0]
    assert "verify_identity" in bound[0]
    assert "list_my_appointments" in bound[2]
    assert "verify_identity" not in bound[2]
    # The tool result reached the model.
    tool_msgs = [m for m in model(s).calls[1] if isinstance(m, ToolMessage)]
    assert tool_msgs and tool_msgs[-1].content.startswith("Verified.")


async def test_tool_turn_yields_one_filler():
    s = await start(
        script=[
            call("list_specializations"),
            call("find_doctors", specialization="Cardiology"),
            say("We have Dr. Meera Rao."),
        ]
    )
    out = await turn(s, "which heart doctors do you have")
    assert kinds(out).count("filler") == 1
    assert out[0] == Speech("filler", FILLER["en"])
    assert spoken(out) == "We have Dr. Meera Rao."


async def test_no_filler_when_text_came_first():
    s = await start(
        script=[
            AIMessage(
                content="Let me look.",
                tool_calls=[{"name": "list_specializations", "args": {}, "id": "t1"}],
            ),
            say("We have three."),
        ]
    )
    out = await turn(s, "what do you offer")
    assert "filler" not in kinds(out)
    assert spoken(out) == "Let me look.We have three."


async def test_book_across_two_turns():
    s = await start(
        candidates=[ABHINAV],
        script=[
            call("hold_slot", doctor_id="d1", clinic_id="c1", start_at="2026-11-24T10:00:00+05:30"),
            say("Dr. Meera Rao at City Heart Clinic on Tuesday at 10 AM. Shall I book it?"),
            call("book_appointment"),
            say("Booked."),
        ],
    )
    s.ctx.verified = ABHINAV
    await turn(s, "book Dr Rao on Tuesday at 10")
    express = s.deps.express
    assert "hold" in express.names()
    assert "book" not in express.names()
    assert s.ctx.pending_hold is not None
    out = await turn(s, "haan, book kar do")
    assert spoken(out) == "Booked."
    assert express.names().count("book") == 1
    args, _ = express.last("book")
    assert args[:2] == ("d1", "c1")
    assert args[2].isoformat() == "2026-11-24T10:00:00+05:30"
    assert s.ctx.pending_hold is None


async def test_end_call_yields_end():
    s = await start(
        script=[
            AIMessage(
                content="Thank you for calling. Goodbye.",
                tool_calls=[{"name": "end_call", "args": {}, "id": "e1"}],
            ),
            say(""),
        ]
    )
    out = await turn(s, "that's all, bye")
    assert out[-1] == Speech("end")
    assert kinds(out).count("end") == 1
    assert spoken(out) == "Thank you for calling. Goodbye."


# ---------------------------------------------------------------- language


async def test_hindi_switch_emitted_after_two_hindi_turns():
    s = await start(script=[say("Ji, bataiye."), say("ठीक है।")])
    out1 = await turn(s, "mujhe doctor chahiye kal")
    assert "switch_language" not in kinds(out1)
    out2 = await turn(s, "kya aap bata sakti hain")
    assert out2[0] == Speech("switch_language", lang="hi")
    assert kinds(out2).count("switch_language") == 1
    assert s.ctx.language.current == "hi"


async def test_switch_language_tool_is_reported():
    s = await start(script=[call("switch_language", lang="hi"), say("ठीक है, हिंदी में बात करते हैं।")])
    out = await turn(s, "please speak in Hindi")
    assert Speech("switch_language", lang="hi") in out
    switch_at = out.index(Speech("switch_language", lang="hi"))
    first_token = next(i for i, x in enumerate(out) if x.kind == "token")
    assert switch_at < first_token
    assert kinds(out).count("switch_language") == 1


# ---------------------------------------------------------------- trim / refusal / errors


async def test_trim_last_reply():
    thinking = {"type": "thinking", "thinking": "pick slots", "signature": "sig"}
    s = await start(
        script=[
            AIMessage(
                content=[
                    thinking,
                    {"type": "text", "text": "Dr. Rao is free at ten, eleven and twelve."},
                ]
            ),
            say("Okay."),
        ]
    )
    out = await turn(s, "when is Dr Rao free")
    assert spoken(out) == "Dr. Rao is free at ten, eleven and twelve."
    await s.trim_last_reply("Dr. Rao is free at ten")
    last = (await history(s))[-1]
    assert last.content == [thinking, {"type": "text", "text": "Dr. Rao is free at ten"}]
    await turn(s, "ten is fine")
    sent = model(s).calls[1]
    assert sent[-2].content == last.content
    assert isinstance(sent[-1], HumanMessage)


async def test_trim_ignores_a_reply_with_tool_calls():
    s = await start(script=[say("Hello.")])
    await turn(s, "hi")
    pending = AIMessage(
        content="Checking", tool_calls=[{"name": "end_call", "args": {}, "id": "x"}]
    )
    await s.graph.aupdate_state(s.config, {"messages": [pending]}, as_node="agent")
    await s.trim_last_reply("Che")
    assert (await history(s))[-1].content == "Checking"


async def test_trim_does_not_touch_an_earlier_turn():
    s = await start(script=[say("Hello there, how can I help?")])
    await turn(s, "hi")
    await s.graph.aupdate_state(s.config, {"messages": [HumanMessage("wait")]}, as_node="safety")
    await s.trim_last_reply("Hello")
    msgs = await history(s)
    assert msgs[-2].content == "Hello there, how can I help?"


async def test_refusal_becomes_fixed_line():
    s = await start(script=[AIMessage(content="", response_metadata={"stop_reason": "refusal"})])
    out = await turn(s, "tell me something")
    assert spoken(out) == REFUSAL_LINE
    assert (await history(s))[-1].content == REFUSAL_LINE


async def test_llm_error_yields_apology_and_the_call_continues():
    s = await start(script=[RuntimeError("api down"), say("Sorry about that. How can I help?")])
    out = await turn(s, "hello")
    assert spoken(out) == APOLOGY["en"]
    out = await turn(s, "hello again")
    assert spoken(out) == "Sorry about that. How can I help?"


async def interrupt_at_filler(s: CallSession, text: str) -> None:
    stream = s.respond(text)
    async for item in stream:
        if item.kind == "filler":
            break
    await stream.aclose()


async def test_interrupted_turn_still_screens_the_next_utterance():
    s = await start(script=[call("list_specializations")])
    await interrupt_at_filler(s, "what do you offer")
    out = await turn(s, "I have chest pain")
    assert spoken(out) == EMERGENCY_MESSAGE["en"]
    assert len(model(s).calls) == 1


async def test_unanswered_tool_call_is_closed_before_the_next_turn():
    s = await start(script=[say("Hello."), say("Yes, I'm here.")])
    await turn(s, "hi")
    dangling = AIMessage(content="", tool_calls=[{"name": "end_call", "args": {}, "id": "x1"}])
    await s.graph.aupdate_state(s.config, {"messages": [dangling]}, as_node="agent")
    out = await turn(s, "hello?")
    assert spoken(out) == "Yes, I'm here."
    # The pending tool from the cut-off turn did not run.
    assert s.ctx.end_requested is False
    sent = model(s).calls[-1]
    results = [m for m in sent if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in results] == ["x1"]
    assert results[0].status == "error"


@pytest.mark.parametrize("text", ["", "   "])
async def test_blank_input_is_ignored(text):
    s = await start(script=[])
    out = await turn(s, text)
    assert out == []
    assert s.ctx.turn == 0
