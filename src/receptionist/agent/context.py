"""Per-call mutable state shared by the graph and the tools."""

from dataclasses import dataclass, field
from datetime import datetime

from langchain_core.tools import ToolException

from receptionist.agent.language import LanguageTracker
from receptionist.clients.express import ExpressClient, PatientSession
from receptionist.identity.verify import Candidate, Verifier


@dataclass
class PendingHold:
    """A slot read back to the caller, waiting for their yes (book, or reschedule when
    `appointment_id` is set). `turn` is the turn it was created in."""

    doctor_id: str
    clinic_id: str
    start_at: datetime
    hold_id: str | None
    summary: str
    turn: int
    appointment_id: str | None = None


@dataclass
class PendingCancel:
    appointment_id: str
    summary: str
    turn: int


@dataclass
class CallContext:
    call_sid: str
    caller_e164: str | None
    candidates: list[Candidate]
    verifier: Verifier
    language: LanguageTracker
    turn: int = 0
    verified: Candidate | None = None
    registered_this_call: bool = False
    locked: bool = False
    pending_hold: PendingHold | None = None
    pending_cancel: PendingCancel | None = None
    emergency: bool = False
    end_requested: bool = False
    # Set once verify_identity returned NOT_FOUND on this call; registration requires it.
    not_found_seen: bool = False
    # Bound by build_tools; used to build the patient session.
    express: ExpressClient | None = field(default=None, repr=False)
    _session: PatientSession | None = field(default=None, repr=False)

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
