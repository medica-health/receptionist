"""LangChain tools for the receptionist. Verification and read-back gating are enforced here.

Every tool returns a short plain-text string (at most 400 characters) for the model to
paraphrase aloud. Tool results never contain a date of birth, and nothing here logs one.
"""

import asyncio
import datetime as _dt
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, Field

from receptionist.agent.context import (
    CallContext,
    PendingCancel,
    PendingHold,
    PendingRegistration,
)
from receptionist.agent.language import emergency_language
from receptionist.agent.safety import EMERGENCY_MESSAGE, Lang, detect_emergency
from receptionist.agent.speech import speak_date, speak_time, speak_when
from receptionist.clients.errors import ApiError, BadRequest, Conflict, NotFound, Unavailable
from receptionist.clients.express import ExpressClient
from receptionist.clients.spring import SpringClient
from receptionist.config import Settings
from receptionist.data.db import PatientDirectory
from receptionist.data.guards import Guards
from receptionist.identity.names import names_match
from receptionist.identity.phone import mask_phone
from receptionist.identity.verify import Candidate, Outcome, validate_dob

log = logging.getLogger(__name__)

MAX_LEN = 400
LOCKED = (
    "For your security I can't access accounts on this call. "
    "Please use the MediCare app or call back later."
)
NO_CALLER_ID = (
    "I can't access or create accounts when the caller ID is hidden. Please call from your "
    "own number, or use the MediCare app. I can still help with doctors and clinics."
)
NOT_VERIFIED = (
    "The caller isn't verified yet. Ask for their full name and date of birth, then call "
    "verify_identity."
)
UNKNOWN_OUTCOME = (
    "Our system didn't respond, so I don't know whether that went through. Don't try again "
    "yet: check with list_my_appointments first."
)
CONFIRM_FIRST = (
    "Confirm first: read the details back to the caller and wait for their yes in their next "
    "reply before calling this."
)
CHECK_FIRST = (
    "The last attempt's outcome is unknown, so it won't be repeated. Please check your "
    "appointments first with list_my_appointments, then start again if it isn't there."
)
API_FAILED = "Sorry, that didn't work on our side. Please try again later or use the MediCare app."
TAKEN = "That time was just taken by someone else. Offer to find another time."
GENERAL_TOOLS = frozenset(
    {
        "verify_identity",
        "prepare_registration",
        "register_patient",
        "list_specializations",
        "find_doctors",
        "clinic_info",
        "suggest_specialization",
        "get_available_slots",
        "switch_language",
        "end_call",
    }
)
_IDENTITY_TOOLS = frozenset({"verify_identity", "prepare_registration", "register_patient"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ToolDeps:
    directory: PatientDirectory
    guards: Guards
    express: ExpressClient
    spring: SpringClient
    settings: Settings
    clock: Callable[[], datetime] = _utcnow


def _cap(text: str) -> str:
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 3].rstrip() + "..."


def _fit(head: str, parts: list[str]) -> str:
    """Joins whole parts after `head` within MAX_LEN, saying how many didn't fit."""

    def more(n: int) -> str:
        return f" {n} more not listed." if n else ""

    text = head
    for i, part in enumerate(parts):
        sep = " " if i == 0 else "; "
        if len(text) + len(sep) + len(part) + 1 + len(more(len(parts) - i - 1)) > MAX_LEN:
            return text.rstrip(":") + "." + more(len(parts) - i)
        text += sep + part
    return text + "."


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _items(data: Any) -> list[dict]:
    if isinstance(data, dict):
        return list(data.get("items") or [])
    return list(data or [])


def _guarded(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Maps errors to short spoken text; patient tools fail closed when not verified."""

    @functools.wraps(fn)
    async def run(**kwargs: Any) -> str:
        try:
            return _cap(await fn(**kwargs))
        except ToolException as exc:
            if str(exc) == "not verified":
                return NOT_VERIFIED
            raise
        except Unavailable:
            return f"{Unavailable.spoken} Please try again in a moment."
        except NotFound:
            return "I couldn't find that. Please check the details with the caller."
        except (BadRequest, Conflict) as exc:
            log.warning("tool %s failed with status %s", fn.__name__, exc.status)
            return _cap(f"Sorry, that didn't work: {exc.message}")
        except ApiError as exc:
            # Never speak other API messages: they can be internal (e.g. "Invalid or
            # missing API key").
            log.warning("tool %s failed with status %s", fn.__name__, exc.status)
            return API_FAILED

    return run


# ------------------------------------------------------------------ argument schemas


class NoArgs(BaseModel):
    pass


class IdentityArgs(BaseModel):
    full_name: str = Field(description="The caller's full name as they said it.")
    dob: date = Field(description="Date of birth as YYYY-MM-DD.")


class FindDoctorsArgs(BaseModel):
    specialization: str | None = Field(None, description="Specialization name from the list.")
    city: str | None = None
    pincode: str | None = None
    language: str | None = Field(None, description="Language the doctor speaks, e.g. Hindi.")


class ClinicArgs(BaseModel):
    clinic: str = Field(description="Clinic name, or part of it.")


class SymptomsArgs(BaseModel):
    symptoms: str = Field(description="The caller's symptoms in their words.")


class SlotsArgs(BaseModel):
    doctor_id: str
    date: _dt.date = Field(description="YYYY-MM-DD, the clinic's local date.")
    clinic_id: str | None = None


class HoldArgs(BaseModel):
    doctor_id: str
    clinic_id: str
    start_at: datetime = Field(description="Exact start_at from get_available_slots.")


class ReasonArgs(BaseModel):
    reason: str | None = Field(None, description="Short reason for the visit or change.")


class ScopeArgs(BaseModel):
    scope: Literal["upcoming", "past"] = "upcoming"


class AppointmentArgs(BaseModel):
    appointment_id: str


class RescheduleArgs(BaseModel):
    appointment_id: str
    start_at: datetime = Field(description="Exact start_at from get_available_slots.")
    clinic_id: str | None = None


class LimitArgs(BaseModel):
    limit: int = Field(3, ge=1, le=5)


class PrescriptionArgs(BaseModel):
    prescription_id: str


class LanguageArgs(BaseModel):
    lang: Literal["en", "hi"]


# ------------------------------------------------------------------ tools


def build_tools(ctx: CallContext, deps: ToolDeps) -> list[BaseTool]:
    ctx.express = deps.express
    settings = deps.settings
    default_tz = settings.default_timezone

    def lang() -> Lang:
        return ctx.language.current

    def today() -> date:
        return deps.clock().astimezone(ZoneInfo(default_tz)).date()

    async def mark_verified(who: Candidate) -> None:
        ctx.verified = who
        ctx.not_found_seen = False
        await deps.guards.set_call_meta(
            ctx.call_sid, {"verified_user_id": who.id, "language": lang()}
        )

    # ---------------------------------------------------------- identity

    async def verify_identity(full_name: str, dob: date) -> str:
        if ctx.caller_e164 is None:
            return NO_CALLER_ID
        if ctx.verified is not None:
            return f"Already verified as {ctx.verified.full_name} on this call."
        caller = ctx.caller_e164
        if ctx.locked or await deps.guards.is_locked(caller):
            ctx.locked = True
            return LOCKED
        if not validate_dob(dob, today()):
            return "That date of birth doesn't look right. Please ask the caller for it again."
        before = ctx.verifier.failures
        result = ctx.verifier.attempt(full_name, dob)
        log.info("verify outcome=%s caller=%s", result.outcome, mask_phone(caller))
        if result.outcome is Outcome.VERIFIED and result.candidate is not None:
            await mark_verified(result.candidate)
            first = result.candidate.full_name.split()[0]
            if result.save_dob:
                try:
                    await ctx.patient().update_profile(dob)
                except ApiError as exc:
                    log.warning("saving DOB after verification failed: %s", exc.status)
            return f"Verified. You're speaking with {first}. Ask how you can help."
        if ctx.verifier.failures > before:
            count = await deps.guards.record_failure(caller)
            if result.outcome is Outcome.LOCKED or count >= settings.verify_max_failures_per_day:
                ctx.locked = True
                return LOCKED
            return (
                "I couldn't verify that. Ask the caller to check their full name and date of "
                "birth and try once more. Don't say which detail was wrong."
            )
        if result.outcome is Outcome.LOCKED:
            ctx.locked = True
            return LOCKED
        if result.outcome is Outcome.UNVERIFIABLE:
            return "I can't verify this over the phone. Please use the MediCare app."
        ctx.not_found_seen = True
        return "I couldn't verify that. If you're new to MediCare, I can register you."

    async def _registration_refusal() -> str | None:
        """Why registration can't go ahead on this call, or None."""
        if ctx.caller_e164 is None:
            return NO_CALLER_ID
        if ctx.verified is not None:
            return f"Already verified as {ctx.verified.full_name}; no registration needed."
        if ctx.locked or await deps.guards.is_locked(ctx.caller_e164):
            ctx.locked = True
            return LOCKED
        if not ctx.not_found_seen:
            return (
                "Registration is only possible after verify_identity couldn't find the name. "
                "Call verify_identity with the caller's full name and date of birth first."
            )
        return None

    async def prepare_registration(full_name: str, dob: date) -> str:
        if refusal := await _registration_refusal():
            return refusal
        if any(names_match(full_name, c.full_name) for c in ctx.candidates):
            return (
                "Ask the caller whether they're already registered with MediCare. If they are, "
                "verify them with verify_identity instead of registering again."
            )
        if not validate_dob(dob, today()):
            return "That date of birth doesn't look right. Please ask the caller for it again."
        await _clear_pending(keep="registration")
        ctx.pending_registration = PendingRegistration(full_name.strip(), dob, ctx.turn)
        return (
            f"Read back: full name {full_name.strip()}, date of birth "
            f"{speak_date(dob, lang())}. Ask the caller to confirm; call register_patient only "
            "after they say yes."
        )

    async def register_patient() -> str:
        if refusal := await _registration_refusal():
            return refusal
        pending = ctx.pending_registration
        if pending is None:
            return "Nothing is ready to register. Call prepare_registration first."
        if pending.turn >= ctx.turn:
            return CONFIRM_FIRST
        assert ctx.caller_e164 is not None  # checked by _registration_refusal
        # Idempotent on the call SID, so a retry after Unavailable can't create a duplicate.
        created = await deps.spring.create_phone_patient(
            pending.full_name, ctx.caller_e164, idempotency_key=ctx.call_sid
        )
        ctx.pending_registration = None
        who = Candidate(created["id"], created["fullName"], created["email"], pending.dob)
        await mark_verified(who)
        ctx.registered_this_call = True
        log.info("registered phone patient caller=%s", mask_phone(ctx.caller_e164))
        try:
            await ctx.patient().update_profile(pending.dob)
        except ApiError as exc:
            log.warning("saving DOB after registration failed: %s", exc.status)
        return f"Registered and verified as {who.full_name}. Ask how you can help."

    # ---------------------------------------------------------- general info

    async def list_specializations() -> str:
        names = [s["name"] for s in await deps.express.specializations()]
        if not names:
            return "No specializations are listed right now."
        return "Specializations: " + ", ".join(names) + "."

    async def _specialization_id(name: str) -> int | str:
        specs = await deps.express.specializations()
        want = name.strip().lower()
        for test in (
            lambda s: s.lower() == want,
            lambda s: want in s.lower() or s.lower() in want,
        ):
            for s in specs:
                if test(s["name"]):
                    return int(s["id"])
        return "I couldn't match that specialty. We have: " + ", ".join(s["name"] for s in specs)

    async def find_doctors(
        specialization: str | None = None,
        city: str | None = None,
        pincode: str | None = None,
        language: str | None = None,
    ) -> str:
        spec_id: int | None = None
        if specialization:
            found = await _specialization_id(specialization)
            if isinstance(found, str):
                return found
            spec_id = found
        auto_hindi = language is None and lang() == "hi"
        if auto_hindi:
            language = "Hindi"

        async def search(language: str | None) -> list[dict]:
            if ctx.verified is not None:
                data = await ctx.patient().search_doctors(spec_id, city, pincode, language, 3)
            else:
                data = await deps.express.search_doctors(
                    specialization_id=spec_id,
                    city=city,
                    pincode=pincode,
                    language=language,
                    limit=3,
                )
            return _items(data)

        doctors = await search(language)
        if not doctors and auto_hindi:
            doctors = await search(None)
        if not doctors:
            return "I couldn't find doctors for that. Offer another city, pincode or specialty."
        parts = []
        for d in doctors[:3]:
            clinic = d.get("nearestClinic") or {}
            where = ", ".join(x for x in (clinic.get("name"), clinic.get("city")) if x)
            langs = ", ".join(d.get("languages") or [])
            speaks = f", speaks {langs}" if langs else ""
            parts.append(
                f"{d['name']} ({d.get('specialization') or 'doctor'}{speaks}) at {where} "
                f"[doctor_id {d['id']}]"
            )
        return _fit("Doctors:", parts)

    async def clinic_info(clinic: str) -> str:
        data = await deps.express.call("GET", "/clinics", params={"q": clinic, "limit": 1})
        found = _items(data)
        if not found:
            return "I couldn't find a clinic by that name."
        c = found[0]
        address = ", ".join(
            x for x in (c.get("address_line"), c.get("city"), c.get("pincode")) if x
        )
        phone = f" Phone {c['phone']}." if c.get("phone") else ""
        return (
            f"{c['name']}: {address}.{phone} Timings depend on the doctor; use "
            f"get_available_slots for a doctor's times. [clinic_id {c['id']}]"
        )

    async def suggest_specialization(symptoms: str) -> str:
        if detect_emergency(symptoms):
            ctx.mark_emergency()
            # The model's summary may be in English even when the caller spoke Hindi.
            if ctx.utterance_lang == "hi":
                ctx.language.force("hi")
            message_lang = emergency_language(ctx.language, symptoms, ctx.stt_lang)
            return f"Emergency red flag. Say this now: {EMERGENCY_MESSAGE[message_lang]}"
        specs = await deps.express.specializations()
        text = "Choose 1-2 from this list only; never diagnose."
        for s in specs:
            desc = (s.get("description") or "").strip()
            entry = f" {s['name']}" + (f": {desc[:60]}." if desc else ".")
            if len(text) + len(entry) > MAX_LEN:
                entry = f" {s['name']}."
                if len(text) + len(entry) > MAX_LEN:
                    break
            text += entry
        return text

    async def get_available_slots(
        doctor_id: str, date: _dt.date, clinic_id: str | None = None
    ) -> str:
        slots = await deps.express.slots(doctor_id, date, clinic_id)
        doctor = await deps.express.doctor(doctor_id)
        clinics = {c["id"]: c for c in doctor.get("clinics") or []}
        now = deps.clock()
        free = [s for s in slots if not s.get("held") and _parse(s["startAt"]) > now][:5]
        name = doctor.get("name", "The doctor")
        if not free:
            return (
                f"{name} has no free times on {speak_date(date, lang())}. "
                "Offer another day or doctor."
            )
        groups: dict[str, list[str]] = {}
        for s in free:
            clinic = clinics.get(s["clinicId"], {})
            start = _parse(s["startAt"])
            spoken = speak_time(start, clinic.get("timezone") or default_tz, lang())
            groups.setdefault(s["clinicId"], []).append(f"{spoken} [{_iso(start)}]")
        parts = [
            f"at {clinics.get(cid, {}).get('name', 'the clinic')} [clinic_id {cid}]: "
            + ", ".join(times)
            for cid, times in groups.items()
        ]
        return f"{name} on {speak_date(date, lang())}, " + "; ".join(parts) + "."

    # ---------------------------------------------------------- bookings

    async def _release_pending() -> None:
        hold = ctx.pending_hold
        ctx.pending_hold = None
        if hold and hold.hold_id:
            try:
                await ctx.patient().release_hold(hold.hold_id)
            except ApiError:
                pass  # it expires on its own

    async def _clear_pending(keep: str | None = None) -> None:
        """Only one write may wait for a yes at a time, so one "yes" can't trigger two writes."""
        if keep != "hold":
            await _release_pending()
        if keep != "cancel":
            ctx.pending_cancel = None
        if keep != "registration":
            ctx.pending_registration = None

    async def _hold(
        doctor_id: str,
        clinic_id: str,
        start_at: datetime,
        appointment_id: str | None = None,
        old: str | None = None,
    ) -> str:
        session = ctx.patient()
        doctor = await deps.express.doctor(doctor_id)
        clinic = next((c for c in doctor.get("clinics") or [] if c["id"] == clinic_id), None)
        if clinic is None:
            return "That doctor doesn't practise at that clinic. Check the slots again."
        tz = clinic.get("timezone") or default_tz
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=ZoneInfo(tz))
        await _clear_pending()
        hold_id: str | None = None
        try:
            held = await session.hold(doctor_id, clinic_id, start_at)
            hold_id = held.get("holdId") if isinstance(held, dict) else None
        except Conflict:
            return TAKEN
        except BadRequest as exc:
            return f"That time can't be booked: {exc.message}. Check the slots again."
        except Unavailable:
            log.warning("slot hold unavailable; continuing without a hold")
        when = speak_when(start_at, tz, lang())
        place = f"{doctor.get('name', 'the doctor')} at {clinic.get('name', 'the clinic')}"
        if appointment_id:
            summary = f"move the appointment with {place} from {old} to {when}"
        else:
            summary = f"{place} on {when}"
        ctx.pending_hold = PendingHold(
            doctor_id, clinic_id, start_at, hold_id, summary, ctx.turn, appointment_id
        )
        tool = "confirm_reschedule" if appointment_id else "book_appointment"
        return (
            f"Ready to {summary}." if appointment_id else f"Held: {summary}."
        ) + f" Read this back and ask the caller to confirm. Call {tool} only after they say yes."

    async def hold_slot(doctor_id: str, clinic_id: str, start_at: datetime) -> str:
        return await _hold(doctor_id, clinic_id, start_at)

    async def _write(
        action: Callable[[], Awaitable[Any]], done: str, kind: Literal["hold", "cancel"]
    ) -> str:
        """Runs a booking write once, never retrying. Pending state is cleared only when the
        outcome is known: after Unavailable, or a cancellation (the caller cut the turn off),
        the write may have gone through."""

        def mark_unknown() -> None:
            pending = ctx.pending_hold if kind == "hold" else ctx.pending_cancel
            if pending is not None:
                pending.outcome_unknown = True

        try:
            await action()
        except asyncio.CancelledError:
            log.warning("%s outcome unknown after cancellation", kind)
            mark_unknown()
            raise
        except Unavailable:
            log.warning("%s outcome unknown after Unavailable", kind)
            mark_unknown()
            return UNKNOWN_OUTCOME
        except (Conflict, BadRequest, NotFound) as exc:
            if kind == "hold":
                await _release_pending()
            else:
                ctx.pending_cancel = None
            if isinstance(exc, Conflict) and "slot" in exc.message.lower():
                return TAKEN
            return f"That can't be done: {exc.message}."
        if kind == "hold":
            ctx.pending_hold = None  # express-api drops the hold once the slot is taken
        else:
            ctx.pending_cancel = None
        return done

    async def book_appointment(reason: str | None = None) -> str:
        session = ctx.patient()
        hold = ctx.pending_hold
        if hold is None:
            return "Nothing is ready to book. Find a slot and call hold_slot first."
        if hold.appointment_id:
            return "This is a reschedule; use confirm_reschedule instead."
        if hold.outcome_unknown:
            return CHECK_FIRST
        if hold.turn >= ctx.turn:
            return CONFIRM_FIRST
        return await _write(
            lambda: session.book(hold.doctor_id, hold.clinic_id, hold.start_at, reason),
            f"Booked: {hold.summary}. A confirmation will be sent to the patient.",
            "hold",
        )

    async def _upcoming(appointment_id: str) -> dict | None:
        data = await ctx.patient().list_appointments("upcoming", 10)
        return next((a for a in _items(data) if a.get("id") == appointment_id), None)

    def _describe(a: dict) -> str:
        when = speak_when(_parse(a["start_at"]), a.get("clinic_timezone") or default_tz, lang())
        return f"{a.get('doctor_name')} at {a.get('clinic_name')} on {when}"

    async def list_my_appointments(scope: Literal["upcoming", "past"] = "upcoming") -> str:
        data = await ctx.patient().list_appointments(scope, 3)
        items = _items(data)
        if not items:
            return f"No {scope} appointments."
        parts = [
            f"{_describe(a)}, {str(a.get('status', '')).lower()} [id {a['id']}]" for a in items
        ]
        return _fit(f"{scope.capitalize()}:", parts)

    async def prepare_cancel(appointment_id: str) -> str:
        appt = await _upcoming(appointment_id)
        if appt is None:
            return "I couldn't find that upcoming appointment. Check list_my_appointments."
        summary = _describe(appt)
        await _clear_pending(keep="cancel")
        ctx.pending_cancel = PendingCancel(appointment_id, summary, ctx.turn)
        return (
            f"Ready to cancel: {summary}. Read this back and ask the caller to confirm. "
            "Call cancel_appointment only after they say yes."
        )

    async def cancel_appointment(reason: str | None = None) -> str:
        session = ctx.patient()
        pending = ctx.pending_cancel
        if pending is None:
            return "Nothing is ready to cancel. Call prepare_cancel with the appointment first."
        if pending.outcome_unknown:
            return CHECK_FIRST
        if pending.turn >= ctx.turn:
            return CONFIRM_FIRST
        return await _write(
            lambda: session.cancel(pending.appointment_id, reason),
            f"Cancelled: {pending.summary}. A confirmation will be sent to the patient.",
            "cancel",
        )

    async def reschedule_appointment(
        appointment_id: str, start_at: datetime, clinic_id: str | None = None
    ) -> str:
        appt = await _upcoming(appointment_id)
        if appt is None:
            return "I couldn't find that upcoming appointment. Check list_my_appointments."
        old = speak_when(
            _parse(appt["start_at"]), appt.get("clinic_timezone") or default_tz, lang()
        )
        return await _hold(
            appt["doctor_id"], clinic_id or appt["clinic_id"], start_at, appointment_id, old
        )

    async def confirm_reschedule() -> str:
        session = ctx.patient()
        hold = ctx.pending_hold
        if hold is None or hold.appointment_id is None:
            return "Nothing is ready to reschedule. Call reschedule_appointment first."
        if hold.outcome_unknown:
            return CHECK_FIRST
        if hold.turn >= ctx.turn:
            return CONFIRM_FIRST
        appointment_id = hold.appointment_id
        return await _write(
            lambda: session.reschedule(appointment_id, hold.start_at, hold.clinic_id),
            f"Rescheduled. The request was to {hold.summary}. A confirmation will be sent.",
            "hold",
        )

    async def resend_confirmation(appointment_id: str) -> str:
        result = await ctx.patient().resend_confirmation(appointment_id)
        if not result.get("sent"):
            return "I couldn't resend the confirmation right now. Please try again later."
        return "The confirmation has been sent again."

    # ---------------------------------------------------------- prescriptions

    async def list_prescriptions(limit: int = 3) -> str:
        session = ctx.patient()
        patient_id = ctx.verified.id if ctx.verified else ""
        items = _items(await session.prescriptions(patient_id, limit))
        if not items:
            return "No prescriptions on file."
        parts = []
        for rx in items:
            when = ""
            if rx.get("appointment_start_at"):
                local = _parse(rx["appointment_start_at"]).astimezone(ZoneInfo(default_tz))
                when = f" after the visit on {speak_date(local.date(), lang())}"
            parts.append(f"from {rx.get('doctor_name')}{when} [id {rx['id']}]")
        return _fit("Prescriptions:", parts)

    async def read_prescription(prescription_id: str) -> str:
        rx = await ctx.patient().prescription(prescription_id)
        items = rx.get("items") or []
        if not items:
            return "That prescription has no medicines listed."
        doctor = rx.get("doctor_name") or "the doctor"
        head = f"Prescription from {doctor}. Read exactly as written:"
        fields = ("medicine", "dosage", "frequency", "duration", "instructions")
        lines = [", ".join(str(i[f]).strip() for f in fields if i.get(f)) + "." for i in items]

        def more(n: int) -> str:
            return f" There are {n} more medicines; ask the caller to check the MediCare app."

        # Whole items only: a medicine line is never cut short.
        text = head
        for n, line in enumerate(lines):
            left_after = len(lines) - n - 1
            reserve = len(more(left_after)) if left_after else 0
            if len(text) + 1 + len(line) + reserve > MAX_LEN:
                return text + more(len(lines) - n)
            text += " " + line
        return text

    # ---------------------------------------------------------- call control

    async def switch_language(lang: Literal["en", "hi"]) -> str:
        ctx.language.force(lang)
        if lang == "hi":
            return "Switched to Hindi. Reply in Hindi, written in Devanagari."
        return "Switched to English. Reply in English."

    async def end_call() -> str:
        ctx.end_requested = True
        return "The call will end now."

    specs: list[tuple[Callable[..., Awaitable[str]], type[BaseModel], str]] = [
        (verify_identity, IdentityArgs, "Verify the caller by full name and date of birth."),
        (
            prepare_registration,
            IdentityArgs,
            "Prepare to register a new patient on the caller's number (read back next). Only "
            "after verify_identity couldn't verify the name.",
        ),
        (register_patient, NoArgs, "Register the prepared patient, after the caller said yes."),
        (list_specializations, NoArgs, "List the medical specializations we offer."),
        (find_doctors, FindDoctorsArgs, "Find doctors by specialization, city, pincode, language."),
        (clinic_info, ClinicArgs, "A clinic's address and phone."),
        (
            suggest_specialization,
            SymptomsArgs,
            "Get the live specialization list to pick 1-2 for the caller's symptoms. Not a "
            "diagnosis.",
        ),
        (get_available_slots, SlotsArgs, "A doctor's next free times on a date (a few only)."),
        (hold_slot, HoldArgs, "Hold a slot for the verified caller and prepare the booking."),
        (book_appointment, ReasonArgs, "Book the held slot, after the caller said yes."),
        (list_my_appointments, ScopeArgs, "The verified caller's upcoming or past appointments."),
        (prepare_cancel, AppointmentArgs, "Prepare to cancel an appointment (read back next)."),
        (cancel_appointment, ReasonArgs, "Cancel the prepared appointment, after a yes."),
        (
            reschedule_appointment,
            RescheduleArgs,
            "Hold a new time for an existing appointment (read back next).",
        ),
        (confirm_reschedule, NoArgs, "Move the appointment to the held time, after a yes."),
        (list_prescriptions, LimitArgs, "The verified caller's recent prescriptions."),
        (read_prescription, PrescriptionArgs, "The medicines on one prescription, verbatim."),
        (resend_confirmation, AppointmentArgs, "Send an appointment confirmation again."),
        (switch_language, LanguageArgs, "Switch the call language when the caller asks."),
        (end_call, NoArgs, "End the call after saying goodbye."),
    ]
    return [
        StructuredTool.from_function(
            coroutine=_guarded(fn),
            name=fn.__name__,
            description=description,
            args_schema=schema,
            handle_tool_error=True,
            handle_validation_error=(
                "Invalid arguments. Dates are YYYY-MM-DD and times are ISO 8601 with an "
                "offset. Ask the caller again if unsure."
            ),
        )
        for fn, schema, description in specs
    ]


def tools_for(ctx: CallContext, all_tools: list[BaseTool]) -> list[BaseTool]:
    """Tools bound for this turn. Patient tools also refuse in code when not verified."""
    if ctx.verified is not None:
        return [t for t in all_tools if t.name not in _IDENTITY_TOOLS]
    allowed = GENERAL_TOOLS - _IDENTITY_TOOLS if ctx.locked else GENERAL_TOOLS
    return [t for t in all_tools if t.name in allowed]
