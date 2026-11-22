from datetime import UTC, date, datetime, timedelta

import pytest
from fakes import APPOINTMENT, Harness, make_ctx, make_deps

from receptionist.agent.context import PendingCancel, PendingHold, PendingRegistration
from receptionist.agent.safety import EMERGENCY_MESSAGE
from receptionist.agent.tools import API_FAILED, tools_for
from receptionist.clients.errors import ApiError, BadRequest, Conflict, Unavailable
from receptionist.identity.verify import Candidate

DOB = date(1990, 1, 2)
ABHINAV = Candidate("u1", "Abhinav Singh", "a@x.y", DOB)
NO_DOB = Candidate("u2", "Priya Sharma", "p@x.y", None)
SLOT = datetime(2026, 11, 24, 4, 30, tzinfo=UTC)

GENERAL = {
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
IDENTITY = {"verify_identity", "prepare_registration", "register_patient"}

# Sample arguments for every patient-only tool.
PATIENT_TOOL_ARGS = {
    "hold_slot": {"doctor_id": "d1", "clinic_id": "c1", "start_at": SLOT.isoformat()},
    "book_appointment": {},
    "list_my_appointments": {"scope": "upcoming"},
    "prepare_cancel": {"appointment_id": "a1"},
    "cancel_appointment": {},
    "reschedule_appointment": {"appointment_id": "a1", "start_at": SLOT.isoformat()},
    "confirm_reschedule": {},
    "list_prescriptions": {},
    "read_prescription": {"prescription_id": "rx1"},
    "resend_confirmation": {"appointment_id": "a1"},
}


def verified(h: Harness, who: Candidate = ABHINAV) -> Harness:
    h.ctx.verified = who
    return h


# ---------------------------------------------------------------- visibility / gating


def test_every_non_general_tool_has_sample_args():
    assert {t.name for t in Harness().tools} - GENERAL == set(PATIENT_TOOL_ARGS)


@pytest.mark.parametrize("name", sorted(PATIENT_TOOL_ARGS))
async def test_patient_tools_refuse_before_verify(name):
    h = Harness(make_ctx(candidates=[ABHINAV], turn=5))
    # Even with pending state planted, nothing reaches express-api before verification.
    h.ctx.pending_hold = PendingHold("d1", "c1", SLOT, "h1", "s", 1)
    h.ctx.pending_cancel = PendingCancel("a1", "s", 1)
    text = await h.run(name, **PATIENT_TOOL_ARGS[name])
    assert "verif" in text.lower()
    assert h.express.calls == []


def test_tools_for_hides_patient_tools_until_verified():
    h = Harness(make_ctx(candidates=[ABHINAV]))
    assert {t.name for t in tools_for(h.ctx, h.tools)} == GENERAL
    verified(h)
    names = {t.name for t in tools_for(h.ctx, h.tools)}
    assert {"book_appointment", "list_my_appointments", "read_prescription"} <= names
    assert not names & IDENTITY


def test_tools_for_hides_identity_tools_when_locked():
    h = Harness(make_ctx(candidates=[ABHINAV]))
    h.ctx.locked = True
    assert {t.name for t in tools_for(h.ctx, h.tools)} == GENERAL - IDENTITY


def test_repr_never_contains_dob():
    h = Harness(make_ctx(candidates=[ABHINAV, NO_DOB]))
    h.ctx.verified = ABHINAV
    h.ctx.pending_registration = PendingRegistration("Rohan Gupta", date(1995, 3, 4), 1)
    text = repr(h.ctx)
    for leak in ("1990", "1995", "dob", "Rohan"):
        assert leak not in text
    assert "1990" not in repr(ABHINAV) and "dob" not in repr(ABHINAV)


# ---------------------------------------------------------------- verification


async def test_verify_success_sets_verified():
    h = Harness(make_ctx(candidates=[ABHINAV]))
    text = await h.run("verify_identity", full_name="abhinav singh", dob="1990-01-02")
    assert h.ctx.verified == ABHINAV
    assert "Abhinav" in text and "1990" not in text
    assert h.guards.failures == []
    assert h.guards.meta["CA123"]["verified_user_id"] == "u1"


async def test_verify_save_dob_calls_update_profile():
    h = Harness(make_ctx(candidates=[NO_DOB]))
    await h.run("verify_identity", full_name="Priya Sharma", dob="1985-06-15")
    assert h.ctx.verified == NO_DOB
    args, _ = h.express.last("update_profile")
    assert args == (date(1985, 6, 15),)
    assert ("as_patient", ("u2", "p@x.y", "Priya Sharma"), {}) in h.express.calls


async def test_wrong_dob_records_failure_in_guards():
    h = Harness(make_ctx(candidates=[ABHINAV]))
    text = await h.run("verify_identity", full_name="Abhinav Singh", dob="1991-01-02")
    assert "couldn't verify" in text.lower()
    assert h.guards.failures == ["+919876543210"]
    assert h.ctx.verified is None
    text = await h.run("verify_identity", full_name="Abhinav Singh", dob="1992-01-02")
    assert h.guards.failures == ["+919876543210"] * 2
    assert h.ctx.locked is True
    assert "can't access accounts" in text


async def test_locked_number_refuses_verify():
    h = Harness(make_ctx(candidates=[ABHINAV]), make_deps())
    h.guards.locked = True
    text = await h.run("verify_identity", full_name="Abhinav Singh", dob="1990-01-02")
    assert "For your security I can't access accounts on this call" in text
    assert h.ctx.verified is None
    assert h.ctx.verifier.failures == 0  # Verifier not consulted
    assert h.ctx.locked is True


async def test_invalid_dob_is_not_a_failed_attempt():
    h = Harness(make_ctx(candidates=[ABHINAV]))
    text = await h.run("verify_identity", full_name="Abhinav Singh", dob="2030-01-01")
    assert "ask" in text.lower()
    assert h.guards.failures == [] and h.ctx.verifier.failures == 0


async def test_not_found_and_unverifiable_wording():
    h = Harness(make_ctx(candidates=[ABHINAV]))
    text = await h.run("verify_identity", full_name="Rohan Gupta", dob="1990-01-02")
    assert text == "I couldn't verify that. If you're new to MediCare, I can register you."
    two = [NO_DOB, Candidate("u3", "Priya Sharma", "q@x.y", None)]
    h = Harness(make_ctx(candidates=two))
    text = await h.run("verify_identity", full_name="Priya Sharma", dob="1990-01-02")
    assert text == "I can't verify this over the phone. Please use the MediCare app."


async def test_verify_refused_without_caller_id():
    h = Harness(make_ctx(caller_e164=None))
    text = await h.run("verify_identity", full_name="Abhinav Singh", dob="1990-01-02")
    assert "caller id" in text.lower()
    assert h.ctx.verified is None


# ---------------------------------------------------------------- registration


async def test_register_only_when_not_found():
    h = Harness(make_ctx(candidates=[ABHINAV], turn=1))
    text = await h.run("prepare_registration", full_name="Rohan Gupta", dob="1995-03-04")
    assert "verify_identity" in text and h.ctx.pending_registration is None
    h.ctx.turn = 2
    assert "verify_identity" in await h.run("register_patient")
    assert h.spring.calls == []
    await h.run("verify_identity", full_name="Rohan Gupta", dob="1995-03-04")
    text = await h.run("prepare_registration", full_name="Rohan Gupta", dob="1995-03-04")
    assert "Rohan Gupta" in text and "4 March 1995" in text
    h.ctx.turn = 3
    text = await h.run("register_patient")
    args, kwargs = h.spring.last("create_phone_patient")
    assert args == ("Rohan Gupta", "+919876543210")
    assert kwargs == {"idempotency_key": "CA123"}
    assert h.express.last("update_profile")[0] == (date(1995, 3, 4),)
    assert h.ctx.verified is not None and h.ctx.verified.id == "new1"
    assert h.ctx.registered_this_call is True
    assert h.ctx.pending_registration is None
    assert "1995" not in text


async def test_register_requires_new_turn():
    h = Harness(make_ctx(candidates=[ABHINAV], turn=4))
    await h.run("verify_identity", full_name="Rohan Gupta", dob="1995-03-04")
    await h.run("prepare_registration", full_name="Rohan Gupta", dob="1995-03-04")
    text = await h.run("register_patient")
    assert "confirm first" in text.lower()
    assert h.spring.calls == [] and h.ctx.verified is None
    h.ctx.turn = 5
    await h.run("register_patient")
    assert h.spring.names() == ["create_phone_patient"]


async def test_prepare_registration_refuses_existing_name_and_bad_dob():
    h = Harness(make_ctx(candidates=[ABHINAV], turn=1))
    await h.run("verify_identity", full_name="Rohan Gupta", dob="1995-03-04")
    text = await h.run("prepare_registration", full_name="Abhinav Singh", dob="1995-03-04")
    assert "already registered" in text and h.ctx.pending_registration is None
    text = await h.run("prepare_registration", full_name="Rohan Gupta", dob="2031-01-01")
    assert "ask" in text.lower() and h.ctx.pending_registration is None


async def test_register_refused_without_caller_id():
    h = Harness(make_ctx(caller_e164=None, turn=1))
    await h.run("verify_identity", full_name="Rohan Gupta", dob="1995-03-04")
    text = await h.run("prepare_registration", full_name="Rohan Gupta", dob="1995-03-04")
    assert "caller id" in text.lower()
    h.ctx.turn = 2
    assert "caller id" in (await h.run("register_patient")).lower()
    assert h.spring.calls == []


# ---------------------------------------------------------------- booking


async def test_book_requires_new_turn():
    h = verified(Harness(make_ctx(turn=3)))
    text = await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    assert "10:00 AM" in text
    hold = h.ctx.pending_hold
    assert hold is not None and hold.turn == 3 and hold.hold_id == "h1"
    text = await h.run("book_appointment")
    assert "confirm first" in text.lower()
    assert "book" not in h.express.names()
    h.ctx.turn = 4
    text = await h.run("book_appointment", reason="chest checkup")
    args, _ = h.express.last("book")
    assert args == ("d1", "c1", SLOT, "chest checkup")
    assert h.ctx.pending_hold is None
    assert "booked" in text.lower()


async def test_book_conflict_clears_hold():
    h = verified(Harness(make_ctx(turn=3)))
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    h.ctx.turn = 4
    h.express.responses["book"] = Conflict(409, "Slot taken")
    text = await h.run("book_appointment")
    assert "just taken" in text
    assert h.ctx.pending_hold is None


async def test_book_unavailable_is_unknown_outcome_and_no_retry():
    h = verified(Harness(make_ctx(turn=3)))
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    h.ctx.turn = 4
    h.express.responses["book"] = Unavailable(503, "down")
    text = await h.run("book_appointment")
    assert h.express.names().count("book") == 1
    assert "list_my_appointments" in text
    assert h.ctx.pending_hold is not None


async def test_hold_conflict_says_taken():
    h = verified(Harness(make_ctx(turn=1)))
    h.express.responses["hold"] = Conflict(409, "Another patient is holding this slot.")
    text = await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    assert "just taken" in text
    assert h.ctx.pending_hold is None


async def test_hold_503_continues_without_hold_id():
    h = verified(Harness(make_ctx(turn=1)))
    h.express.responses["hold"] = Unavailable(503, "Slot holds are unavailable")
    text = await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    assert h.ctx.pending_hold is not None and h.ctx.pending_hold.hold_id is None
    assert "Dr. Meera Rao" in text
    h.ctx.turn = 2
    await h.run("book_appointment")
    assert "book" in h.express.names()


async def test_new_hold_releases_previous():
    h = verified(Harness(make_ctx(turn=1)))
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    later = SLOT + timedelta(minutes=30)
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=later.isoformat())
    assert h.express.last("release_hold")[0] == ("h1",)
    assert h.ctx.pending_hold is not None and h.ctx.pending_hold.start_at == later


# ---------------------------------------------------------------- cancel / reschedule


async def test_cancel_two_phase():
    h = verified(Harness(make_ctx(turn=5)))
    text = await h.run("cancel_appointment")
    assert "prepare_cancel" in text and "cancel" not in h.express.names()
    text = await h.run("prepare_cancel", appointment_id="a1")
    assert "Dr. Meera Rao" in text and "11:00 AM" in text
    assert h.ctx.pending_cancel == PendingCancel("a1", h.ctx.pending_cancel.summary, 5)
    text = await h.run("cancel_appointment", reason="feeling better")
    assert "confirm first" in text.lower() and "cancel" not in h.express.names()
    h.ctx.turn = 6
    text = await h.run("cancel_appointment", reason="feeling better")
    assert h.express.last("cancel")[0] == ("a1", "feeling better")
    assert h.ctx.pending_cancel is None
    assert "cancelled" in text.lower()


async def test_prepare_cancel_unknown_appointment():
    h = verified(Harness(make_ctx(turn=5)))
    text = await h.run("prepare_cancel", appointment_id="zzz")
    assert "couldn't find" in text.lower()
    assert h.ctx.pending_cancel is None


async def test_cancel_cutoff_message():
    h = verified(Harness(make_ctx(turn=5)))
    await h.run("prepare_cancel", appointment_id="a1")
    h.ctx.turn = 6
    h.express.responses["cancel"] = BadRequest(
        400, "Appointments can only be changed up to 2 hours before they start"
    )
    text = await h.run("cancel_appointment")
    assert "only be changed up to 2 hours before they start" in text
    assert h.ctx.pending_cancel is None


async def test_reschedule_holds_then_confirms_next_turn():
    h = verified(Harness(make_ctx(turn=2)))
    text = await h.run("reschedule_appointment", appointment_id="a1", start_at=SLOT.isoformat())
    hold = h.ctx.pending_hold
    assert hold is not None and hold.appointment_id == "a1" and hold.clinic_id == "c1"
    assert h.express.last("hold")[0] == ("d1", "c1", SLOT)
    assert "10:00 AM" in text
    assert "book_appointment" not in text
    assert "confirm first" in (await h.run("confirm_reschedule")).lower()
    # book_appointment must not complete a reschedule
    h.ctx.turn = 3
    assert "confirm_reschedule" in await h.run("book_appointment")
    await h.run("confirm_reschedule")
    assert h.express.last("reschedule")[0] == ("a1", SLOT, "c1")
    assert h.ctx.pending_hold is None


async def test_unavailable_maps_to_apology():
    h = verified(Harness())
    h.express.responses["list_appointments"] = Unavailable(503, "boom")
    text = await h.run("list_my_appointments", scope="upcoming")
    assert text.startswith(Unavailable.spoken)
    h.express.responses["specializations"] = Unavailable(503, "boom")
    assert (await h.run("list_specializations")).startswith(Unavailable.spoken)


async def test_generic_api_error_never_speaks_the_internal_message():
    h = verified(Harness())
    h.express.responses["list_appointments"] = ApiError(401, "Invalid or missing API key")
    text = await h.run("list_my_appointments", scope="upcoming")
    assert text == API_FAILED
    assert "API key" not in text


async def test_bad_request_message_is_still_spoken():
    h = verified(Harness())
    h.express.responses["list_appointments"] = BadRequest(400, "scope must be upcoming or past")
    text = await h.run("list_my_appointments", scope="upcoming")
    assert "scope must be upcoming or past" in text


async def test_list_appointments_spoken_in_clinic_tz():
    h = verified(Harness())
    text = await h.run("list_my_appointments", scope="upcoming")
    assert "Wednesday, 25 November" in text and "11:00 AM" in text and "a1" in text


async def test_resend_confirmation_not_sent():
    h = verified(Harness())
    h.express.responses["resend_confirmation"] = {"appointmentId": "a1", "sent": False}
    text = await h.run("resend_confirmation", appointment_id="a1")
    assert "couldn't" in text.lower()
    h.express.responses["resend_confirmation"] = {"appointmentId": "a1", "sent": True}
    assert "sent" in (await h.run("resend_confirmation", appointment_id="a1")).lower()


# ---------------------------------------------------------------- general info


async def test_slots_are_few_and_spoken_in_clinic_tz():
    h = Harness()
    start = datetime(2026, 11, 24, 3, 30, tzinfo=UTC)  # 09:00 IST
    slots = [
        {
            "startAt": (start + timedelta(minutes=15 * i)).isoformat().replace("+00:00", "Z"),
            "endAt": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "clinicId": "c1",
            "held": i == 1,
            "heldUntil": None,
        }
        for i in range(30)
    ]
    h.express.responses["slots"] = slots
    text = await h.run("get_available_slots", doctor_id="d1", date="2026-11-24")
    assert "9:00 AM" in text and "9:30 AM" in text
    assert "9:15 AM" not in text  # held by someone else
    assert "4:30 AM" not in text and "3:30 AM" not in text  # UTC never spoken
    assert text.count(" AM") + text.count(" PM") <= 5
    assert "2026-11-24T03:30:00Z" in text  # exact start_at for hold_slot
    assert h.express.last("slots")[0] == ("d1", date(2026, 11, 24), None)


async def test_slots_none_free():
    h = Harness()
    text = await h.run("get_available_slots", doctor_id="d1", date="2026-11-24")
    assert "no free" in text.lower()


async def test_find_doctors_hindi_default_language():
    h = Harness()
    h.ctx.language.force("hi")
    h.express.responses["search_doctors"] = {
        "items": [
            {
                "id": "d1",
                "name": "Dr. Meera Rao",
                "specialization": "Cardiology",
                "languages": ["English", "Hindi"],
                "nearestClinic": {"id": "c1", "name": "City Heart Clinic", "city": "Pune"},
            }
        ]
    }
    text = await h.run("find_doctors", specialization="cardiology", city="Pune")
    _, kwargs = h.express.last("search_doctors")
    assert kwargs["language"] == "Hindi" and kwargs["specialization_id"] == 1
    assert kwargs["city"] == "Pune"
    assert "Dr. Meera Rao" in text and "d1" in text
    await h.run("find_doctors", specialization="cardiology", language="English")
    assert h.express.last("search_doctors")[1]["language"] == "English"


async def test_find_doctors_verified_uses_patient_session():
    h = verified(Harness())
    await h.run("find_doctors", specialization="Orthopedics")
    assert "patient_search_doctors" in h.express.names()
    assert "search_doctors" not in h.express.names()


async def test_suggest_specialization_lists_live_options():
    h = Harness()
    text = await h.run("suggest_specialization", symptoms="knee pain for two weeks")
    assert "Choose 1-2 from this list only; never diagnose." in text
    assert "Orthopedics" in text and "Cardiology" in text


async def test_suggest_specialization_red_flag():
    h = Harness()
    text = await h.run("suggest_specialization", symptoms="crushing chest pain")
    assert "112" in text
    assert h.ctx.emergency is True
    assert "specializations" not in h.express.names()


async def test_suggest_specialization_red_flag_in_hindi_from_the_caller_utterance():
    h = Harness()
    # First turn: the tracker is still English, but the caller spoke Hindi.
    h.ctx.utterance_lang = "hi"
    text = await h.run("suggest_specialization", symptoms="trouble breathing")
    assert EMERGENCY_MESSAGE["hi"] in text
    assert h.ctx.language.current == "hi"


async def test_clinic_info():
    h = Harness()
    h.express.responses["call"] = {"items": [DOCTOR_CLINIC]}
    text = await h.run("clinic_info", clinic="City Heart")
    assert "12 MG Road" in text and "Pune" in text
    assert h.express.last("call")[0] == ("GET", "/clinics")


DOCTOR_CLINIC = {
    "id": "c1",
    "name": "City Heart Clinic",
    "address_line": "12 MG Road",
    "city": "Pune",
    "phone": "+912012345678",
    "timezone": "Asia/Kolkata",
}


async def test_switch_language_and_end_call():
    h = Harness()
    await h.run("switch_language", lang="hi")
    assert h.ctx.language.current == "hi"
    await h.run("end_call")
    assert h.ctx.end_requested is True


# ---------------------------------------------------------------- prescriptions


async def test_list_prescriptions_uses_verified_id():
    h = verified(Harness())
    text = await h.run("list_prescriptions")
    assert h.express.last("prescriptions")[0] == ("u1", 3)
    assert "rx1" in text and "Dr. Meera Rao" in text


async def test_read_prescription_returns_items_verbatim():
    h = verified(Harness())
    text = await h.run("read_prescription", prescription_id="rx1")
    for exact in (
        "Amlodipine 5 mg",
        "1 tablet",
        "once daily",
        "30 days",
        "after breakfast",
        "Telmisartan 40 mg",
        "at night",
    ):
        assert exact in text
    assert "None" not in text


async def test_read_prescription_long_list_never_cuts_an_item():
    h = verified(Harness())
    item = {
        "medicine": "Paracetamol 650 mg",
        "dosage": "1 tablet",
        "frequency": "three times a day",
        "duration": "5 days",
        "instructions": "after food",
    }
    h.express.responses["prescription"] = {"id": "rx2", "items": [item] * 8}
    text = await h.run("read_prescription", prescription_id="rx2")
    assert "more" in text and "app" in text
    assert text.count("Paracetamol 650 mg") == text.count("after food")


def test_pending_types_shape():
    p = PendingHold("d1", "c1", SLOT, None, "s", 1)
    assert p.appointment_id is None
    assert APPOINTMENT["id"] == "a1"


async def test_long_ids_are_never_cut_mid_item():
    h = verified(Harness())
    uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    h.express.responses["list_appointments"] = {
        "items": [dict(APPOINTMENT, id=f"{uuid[:-1]}{i}") for i in range(3)]
    }
    text = await h.run("list_my_appointments", scope="upcoming")
    assert "..." not in text
    shown = text.count(uuid[:-1])
    assert shown >= 1 and (shown == 3 or f"{3 - shown} more not listed." in text)


async def test_cancel_unavailable_then_book_conflict_releases_hold():
    h = verified(Harness(make_ctx(turn=1)))
    await h.run("prepare_cancel", appointment_id="a1")
    h.ctx.turn = 2
    h.express.responses["cancel"] = Unavailable(503, "down")
    assert "list_my_appointments" in await h.run("cancel_appointment")
    assert h.ctx.pending_cancel is not None and h.ctx.pending_cancel.outcome_unknown
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    assert h.ctx.pending_cancel is None  # a new hold replaces any other pending write
    h.ctx.turn = 3
    h.express.responses["book"] = Conflict(409, "This time slot is no longer available")
    await h.run("book_appointment")
    assert h.express.last("release_hold")[0] == ("h1",)
    assert h.ctx.pending_hold is None


async def test_unknown_outcome_blocks_every_write_until_fresh_prepare():
    h = verified(Harness(make_ctx(turn=1)))
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    h.ctx.turn = 2
    h.express.responses["book"] = Unavailable(503, "down")
    await h.run("book_appointment")
    h.ctx.turn = 3
    del h.express.responses["book"]
    assert "check your appointments first" in await h.run("book_appointment")
    assert h.express.names().count("book") == 1
    # cancel
    await h.run("prepare_cancel", appointment_id="a1")
    h.ctx.turn = 4
    h.express.responses["cancel"] = Unavailable(503, "down")
    await h.run("cancel_appointment")
    h.ctx.turn = 5
    assert "check your appointments first" in await h.run("cancel_appointment")
    assert h.express.names().count("cancel") == 1
    # reschedule
    await h.run("reschedule_appointment", appointment_id="a1", start_at=SLOT.isoformat())
    h.ctx.turn = 6
    h.express.responses["reschedule"] = Unavailable(503, "down")
    await h.run("confirm_reschedule")
    h.ctx.turn = 7
    assert "check your appointments first" in await h.run("confirm_reschedule")
    assert h.express.names().count("reschedule") == 1
    # a fresh hold starts over
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    h.ctx.turn = 8
    assert "booked" in (await h.run("book_appointment")).lower()


async def test_one_yes_cannot_trigger_two_writes():
    h = verified(Harness(make_ctx(turn=1)))
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    await h.run("prepare_cancel", appointment_id="a1")
    assert h.ctx.pending_hold is None
    assert h.express.last("release_hold")[0] == ("h1",)
    h.ctx.turn = 2
    assert "nothing is ready" in (await h.run("book_appointment")).lower()
    await h.run("cancel_appointment")
    assert "book" not in h.express.names() and h.express.names().count("cancel") == 1


async def test_prepare_registration_clears_other_pending():
    h = Harness(make_ctx(candidates=[ABHINAV], turn=1))
    h.ctx.pending_cancel = PendingCancel("a1", "s", 1)
    await h.run("verify_identity", full_name="Rohan Gupta", dob="1995-03-04")
    await h.run("prepare_registration", full_name="Rohan Gupta", dob="1995-03-04")
    assert h.ctx.pending_cancel is None and h.ctx.pending_registration is not None
    verified(h)
    await h.run("hold_slot", doctor_id="d1", clinic_id="c1", start_at=SLOT.isoformat())
    assert h.ctx.pending_registration is None


async def test_bad_dob_format_is_a_validation_message_not_a_failure():
    h = Harness(make_ctx(candidates=[ABHINAV]))
    text = await h.run("verify_identity", full_name="Abhinav Singh", dob="02/01/1990")
    assert "YYYY-MM-DD" in text
    assert h.ctx.verifier.failures == 0 and h.guards.failures == []


async def test_register_without_prepare_is_refused():
    h = Harness(make_ctx(candidates=[ABHINAV], turn=1))
    await h.run("verify_identity", full_name="Rohan Gupta", dob="1995-03-04")
    h.ctx.turn = 2
    assert "prepare_registration" in await h.run("register_patient")
    assert h.spring.calls == []
