"""Per-call mutable state shared by the graph and the tools."""

from dataclasses import dataclass, field
from datetime import date, datetime

from langchain_core.tools import ToolException

from receptionist.agent.language import LanguageTracker
from receptionist.agent.safety import Lang
from receptionist.clients.express import ExpressClient, PatientSession
from receptionist.identity.verify import Candidate, Verifier


@dataclass
class PendingHold:
    """A slot read back to the caller, waiting for their yes (book, or reschedule when
    `appointment_id` is set). `turn` is the turn it was created in. `outcome_unknown` is set
    when the write was attempted but express-api didn't answer: it must not be retried."""

    doctor_id: str
    clinic_id: str
    start_at: datetime
    hold_id: str | None
    summary: str
    turn: int
    appointment_id: str | None = None
    outcome_unknown: bool = False


@dataclass
class PendingCancel:
    appointment_id: str
    summary: str
    turn: int
    outcome_unknown: bool = False


@dataclass
class PendingRegistration:
    full_name: str = field(repr=False)
    dob: date = field(repr=False)
    turn: int


@dataclass
class CallContext:
    call_sid: str
    caller_e164: str | None
    candidates: list[Candidate] = field(repr=False)
    verifier: Verifier = field(repr=False)
    language: LanguageTracker
    turn: int = 0
    verified: Candidate | None = field(default=None, repr=False)
    registered_this_call: bool = False
    locked: bool = False
    pending_hold: PendingHold | None = None
    pending_cancel: PendingCancel | None = None
    emergency: bool = False
    # Turn in which the emergency (112) message was last given; see prompts.turn_context.
    emergency_turn: int | None = None
    end_requested: bool = False
    pending_registration: PendingRegistration | None = field(default=None, repr=False)
    # Set once verify_identity returned NOT_FOUND on this call; registration requires it.
    not_found_seen: bool = False
    # This turn's STT language hint and the language of the caller's utterance, set by
    # CallSession.respond before the graph runs (the safety screen and tools read them).
    stt_lang: str | None = None
    utterance_lang: Lang = "en"
    # Bound by build_tools; used to build the patient session.
    express: ExpressClient | None = field(default=None, repr=False)
    _session: PatientSession | None = field(default=None, repr=False)

    def mark_emergency(self) -> None:
        self.emergency = True
        self.emergency_turn = self.turn

    def patient(self) -> PatientSession:
        """The express-api session of the verified caller. Fails closed when not verified."""
        if self.verified is None:
            raise ToolException("not verified")
        if self.express is None:
            raise RuntimeError("CallContext.express is not bound")
        if self._session is None:
            v = self.verified
            self._session = self.express.as_patient(v.id, v.email, v.full_name)
        return self._session
