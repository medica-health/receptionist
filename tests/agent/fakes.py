"""In-memory fakes for the agent tests. They record every call they receive."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field as PydField

from receptionist.agent.context import CallContext
from receptionist.agent.language import LanguageTracker
from receptionist.agent.tools import ToolDeps, build_tools
from receptionist.app import AppDeps
from receptionist.config import Settings
from receptionist.identity.verify import Candidate, Verifier

NOW = datetime(2026, 11, 22, 23, 30, tzinfo=UTC)  # 05:00 IST on Monday 23 November

DOCTOR = {
    "id": "d1",
    "name": "Dr. Meera Rao",
    "specialization": "Cardiology",
    "clinics": [
        {
            "id": "c1",
            "name": "City Heart Clinic",
            "address_line": "12 MG Road",
            "city": "Pune",
            "phone": "+912012345678",
            "timezone": "Asia/Kolkata",
        }
    ],
}

APPOINTMENT = {
    "id": "a1",
    "status": "BOOKED",
    "start_at": "2026-11-25T05:30:00.000Z",
    "doctor_id": "d1",
    "doctor_name": "Dr. Meera Rao",
    "doctor_specialization": "Cardiology",
    "clinic_id": "c1",
    "clinic_name": "City Heart Clinic",
    "clinic_timezone": "Asia/Kolkata",
}

PRESCRIPTION = {
    "id": "rx1",
    "doctor_name": "Dr. Meera Rao",
    "appointment_start_at": "2026-11-10T05:30:00.000Z",
    "clinic_name": "City Heart Clinic",
    "diagnosis": "Hypertension",
    "items": [
        {
            "medicine": "Amlodipine 5 mg",
            "dosage": "1 tablet",
            "frequency": "once daily",
            "duration": "30 days",
            "instructions": "after breakfast",
        },
        {
            "medicine": "Telmisartan 40 mg",
            "dosage": "1 tablet",
            "frequency": "at night",
            "duration": "30 days",
            "instructions": None,
        },
    ],
}

SPECIALIZATIONS = [
    {"id": 1, "name": "Cardiology", "description": "Heart and blood vessels"},
    {"id": 2, "name": "Orthopedics", "description": "Bones, joints and muscles"},
    {"id": 3, "name": "General Medicine", "description": "Common illnesses and check-ups"},
]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.responses: dict[str, Any] = {}

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def last(self, name: str) -> tuple[tuple, dict]:
        for n, args, kwargs in reversed(self.calls):
            if n == name:
                return args, kwargs
        raise AssertionError(f"{name} was not called")

    async def _do(self, name: str, default: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        r = self.responses.get(name, default)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(*args, **kwargs)
        return r


class FakePatientSession:
    def __init__(self, rec: Recorder, user_id: str) -> None:
        self._rec = rec
        self.user_id = user_id

    async def get_profile(self):
        return await self._rec._do("get_profile", {})

    async def update_profile(self, date_of_birth: date):
        return await self._rec._do("update_profile", {}, date_of_birth)

    async def list_appointments(self, scope, limit=5):
        return await self._rec._do("list_appointments", {"items": [APPOINTMENT]}, scope, limit)

    async def hold(self, doctor_id, clinic_id, start_at):
        return await self._rec._do(
            "hold", {"holdId": "h1", "expiresAt": "x"}, doctor_id, clinic_id, start_at
        )

    async def release_hold(self, hold_id):
        return await self._rec._do("release_hold", None, hold_id)

    async def book(self, doctor_id, clinic_id, start_at, reason=None):
        return await self._rec._do(
            "book", {"id": "a9", "status": "BOOKED"}, doctor_id, clinic_id, start_at, reason
        )

    async def cancel(self, appointment_id, reason=None):
        return await self._rec._do("cancel", {"id": appointment_id}, appointment_id, reason)

    async def reschedule(self, appointment_id, start_at, clinic_id=None):
        return await self._rec._do(
            "reschedule", {"id": appointment_id}, appointment_id, start_at, clinic_id
        )

    async def prescriptions(self, patient_id, limit=3):
        return await self._rec._do("prescriptions", {"items": [PRESCRIPTION]}, patient_id, limit)

    async def prescription(self, prescription_id):
        return await self._rec._do("prescription", PRESCRIPTION, prescription_id)

    async def resend_confirmation(self, appointment_id):
        return await self._rec._do(
            "resend_confirmation", {"appointmentId": appointment_id, "sent": True}, appointment_id
        )

    async def search_doctors(
        self, specialization_id=None, city=None, pincode=None, language=None, limit=5
    ):
        return await self._rec._do(
            "patient_search_doctors",
            {"items": []},
            specialization_id=specialization_id,
            city=city,
            pincode=pincode,
            language=language,
            limit=limit,
        )


class FakeExpress(Recorder):
    async def specializations(self):
        return await self._do("specializations", SPECIALIZATIONS)

    async def search_doctors(
        self, specialization_id=None, city=None, pincode=None, language=None, limit=5, token=None
    ):
        return await self._do(
            "search_doctors",
            {"items": []},
            specialization_id=specialization_id,
            city=city,
            pincode=pincode,
            language=language,
            limit=limit,
        )

    async def slots(self, doctor_id, date, clinic_id=None):
        return await self._do("slots", [], doctor_id, date, clinic_id)

    async def doctor(self, doctor_id):
        return await self._do("doctor", DOCTOR, doctor_id)

    async def call(self, method, path, token=None, params=None, json=None):
        return await self._do("call", {"items": []}, method, path, params=params)

    def as_patient(self, user_id, email, name):
        self.calls.append(("as_patient", (user_id, email, name), {}))
        return FakePatientSession(self, user_id)


class FakeSpring(Recorder):
    async def create_phone_patient(self, full_name, phone_e164, idempotency_key):
        return await self._do(
            "create_phone_patient",
            lambda *a, **k: {
                "id": "new1",
                "email": "p919876543210@phone.local",
                "fullName": full_name,
            },
            full_name,
            phone_e164,
            idempotency_key=idempotency_key,
        )


class FakeGuards:
    def __init__(self, locked: bool = False) -> None:
        self.locked = locked
        self.failures: list[str] = []
        self.meta: dict[str, dict] = {}

    async def is_locked(self, e164: str) -> bool:
        return self.locked

    async def record_failure(self, e164: str) -> int:
        self.failures.append(e164)
        return len(self.failures)

    async def set_call_meta(self, call_sid: str, data: dict) -> None:
        self.meta[call_sid] = data

    async def register_call(self, e164: str) -> bool:
        return True

    async def ping(self) -> bool:
        return True


class FakeDirectory:
    def __init__(
        self, candidates: list[Candidate] | None = None, error: Exception | None = None
    ) -> None:
        self.candidates = candidates or []
        self.error = error
        self.lookups: list[str] = []

    async def find_by_phone(self, e164: str) -> list[Candidate]:
        self.lookups.append(e164)
        if self.error is not None:
            raise self.error
        return list(self.candidates)


def make_ctx(
    candidates: list[Candidate] | None = None,
    caller_e164: str | None = "+919876543210",
    call_sid: str = "CA123",
    turn: int = 0,
) -> CallContext:
    cands = candidates or []
    return CallContext(
        call_sid=call_sid,
        caller_e164=caller_e164,
        candidates=cands,
        verifier=Verifier(cands, 2),
        language=LanguageTracker(),
        turn=turn,
    )


def make_deps(guards: FakeGuards | None = None, clock: Callable[[], datetime] = lambda: NOW):
    return ToolDeps(
        directory=FakeDirectory(),
        guards=guards or FakeGuards(),
        express=FakeExpress(),
        spring=FakeSpring(),
        settings=Settings(),
        clock=clock,
    )


class Harness:
    """A context, deps and the built tools, with a helper to invoke one tool by name."""

    def __init__(self, ctx: CallContext | None = None, deps: ToolDeps | None = None) -> None:
        self.ctx = ctx or make_ctx()
        self.deps = deps or make_deps()
        self.tools = build_tools(self.ctx, self.deps)

    @property
    def express(self) -> FakeExpress:
        return self.deps.express  # type: ignore[return-value]

    @property
    def spring(self) -> FakeSpring:
        return self.deps.spring  # type: ignore[return-value]

    @property
    def guards(self) -> FakeGuards:
        return self.deps.guards  # type: ignore[return-value]

    async def run(self, name: str, **args: Any) -> str:
        tool = next(t for t in self.tools if t.name == name)
        result = await tool.ainvoke(args)
        assert isinstance(result, str)
        assert len(result) <= 400, result
        return result


# ---------------------------------------------------------------- chat model


class ScriptedChatModel(BaseChatModel):
    """Returns queued AIMessages in order and records what it was sent.

    Streams like ChatAnthropic: text in word chunks, tool calls as tool_call_chunks, list
    content (thinking + text) as one chunk, response_metadata on the last chunk. An entry may
    be an Exception, which is raised instead.
    """

    script: list[Any] = PydField(default_factory=list)
    calls: list[list[BaseMessage]] = PydField(default_factory=list)
    bound: list[list[str]] = PydField(default_factory=list)
    bind_kwargs: list[dict] = PydField(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        self.bound.append([t.name for t in tools])
        self.bind_kwargs.append(kwargs)
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("ScriptedChatModel ran out of responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self._next(messages)
        chunks: list[AIMessageChunk] = []
        if isinstance(msg.content, list):
            chunks.append(AIMessageChunk(content=msg.content))
        elif msg.content:
            words = msg.content.split(" ")
            chunks += [
                AIMessageChunk(content=w if i == 0 else " " + w) for i, w in enumerate(words)
            ]
        for i, tc in enumerate(msg.tool_calls):
            chunks.append(
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": tc["name"],
                            "args": json.dumps(tc["args"]),
                            "id": tc.get("id") or f"call_{i}",
                            "index": i,
                        }
                    ],
                )
            )
        if not chunks:
            chunks.append(AIMessageChunk(content=""))
        chunks[-1].response_metadata = dict(msg.response_metadata)
        for chunk in chunks:
            gen = ChatGenerationChunk(message=chunk)
            if run_manager:
                await run_manager.on_llm_new_token(gen.text, chunk=gen)
            yield gen


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


def call(name: str, call_id: str | None = None, **args: Any) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id or f"id_{name}"}]
    )


def make_app_deps(
    candidates: list[Candidate] | None = None,
    script: list[Any] | None = None,
    guards: FakeGuards | None = None,
    directory: Any = None,
) -> AppDeps:
    model = ScriptedChatModel(script=list(script or []))
    return AppDeps(
        settings=Settings(),
        directory=directory or FakeDirectory(candidates),
        guards=guards or FakeGuards(),
        express=FakeExpress(),
        spring=FakeSpring(),
        llm_factory=lambda: model,
    )
