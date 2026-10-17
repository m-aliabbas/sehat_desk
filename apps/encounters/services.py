"""Encounter lifecycle. Views call these; they never change status or versions directly.

Starting a consultation from a queue token also moves the token to "in consultation" (and its
appointment to "with doctor"). Completing it completes the token and the appointment.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.core.models import next_number
from apps.queue import services as queue_services
from apps.queue.models import QueueToken

from .diagnosis_codes import STARTER_CODES
from .models import NOTE_FIELD_NAMES, DiagnosisCode, Encounter, EncounterDiagnosis, EncounterVersion

Status = Encounter.Status
TokenStatus = QueueToken.Status
ApptStatus = Appointment.Status


class EncounterError(ValidationError):
    pass


class ConflictError(EncounterError):
    """The note changed in another tab or window since this one loaded it."""


def doctor_for(request):
    """The signed-in user's doctor profile in this organization, or None."""
    profile = getattr(request.user, "doctor_profile", None)
    if profile is None or profile.organization_id != request.organization.id:
        return None
    return profile


def can_write(request, encounter) -> bool:
    """Only the encounter's own doctor writes its note (clinical authorship)."""
    doctor = doctor_for(request)
    return (
        "consultation.create" in request.sd_perms
        and doctor is not None
        and encounter.doctor_id == doctor.pk
    )


@transaction.atomic
def start(*, request, patient, token=None, appointment=None) -> tuple[Encounter, bool]:
    """Open (or continue) this doctor's consultation for a patient. Returns (encounter, created)."""
    doctor = doctor_for(request)
    if doctor is None or "consultation.create" not in request.sd_perms:
        raise EncounterError("Only a doctor can start a consultation.")
    if token is not None:
        if token.patient_id != patient.pk:
            raise EncounterError("That token belongs to another patient.")
        if hasattr(token, "encounter"):
            return token.encounter, False
        if token.status not in (TokenStatus.CALLED, TokenStatus.IN_CONSULTATION):
            raise EncounterError(f"Call {token.label} before starting the consultation.")
        appointment = appointment or token.appointment
    if appointment is not None:
        if appointment.patient_id != patient.pk:
            raise EncounterError("That appointment belongs to another patient.")
        if hasattr(appointment, "encounter"):
            return appointment.encounter, False
        if appointment.status in Appointment.RELEASED_STATUSES:
            raise EncounterError("This appointment was cancelled or missed.")

    open_draft = Encounter.objects.filter(
        branch=request.branch, patient=patient, doctor=doctor, status=Status.DRAFT
    ).first()
    if open_draft is not None and token is None and appointment is None:
        return open_draft, False  # continue the draft instead of opening a second one

    year = timezone.localdate().year
    number = next_number(request.organization, f"encounter:{year}")
    encounter = Encounter.objects.create(
        organization=request.organization,
        branch=request.branch,
        number=f"VIS-{year}-{number:05d}",
        patient=patient,
        doctor=doctor,
        department=doctor.department,
        appointment=appointment,
        token=token,
        visit_reason=(appointment.reason if appointment else "")[:200],
        created_by=request.user,
    )
    if token is not None and token.status == TokenStatus.CALLED:
        queue_services.start(token)
    elif appointment is not None:
        _advance_appointment(appointment, ApptStatus.WITH_DOCTOR)
    return encounter, True


# The patient reaching the doctor implies arrival: scheduled → checked in → with doctor → done.
_APPT_PATH = [ApptStatus.CHECKED_IN, ApptStatus.WITH_DOCTOR, ApptStatus.COMPLETED]


def _advance_appointment(appt, target):
    appt.refresh_from_db()
    for step in _APPT_PATH[: _APPT_PATH.index(target) + 1]:
        if appt.can_transition(step):
            appt.status = step
    if appt.status in _APPT_PATH:
        appt.checked_in_at = appt.checked_in_at or timezone.now()
        appt.save(update_fields=["status", "checked_in_at", "updated_at"])


def _require_writable(request, encounter):
    if not encounter.is_draft:
        raise EncounterError(f"{encounter.number} is completed. Reopen it to amend.")
    if not can_write(request, encounter):
        raise EncounterError("Only the treating doctor can edit this consultation.")


@transaction.atomic
def save_note(encounter, *, request, data: dict, revision: int | None = None) -> Encounter:
    """Save draft note fields (autosave or Save draft). Unknown keys are ignored."""
    encounter = Encounter.objects.select_for_update(of=("self",)).get(pk=encounter.pk)
    _require_writable(request, encounter)
    if revision is not None and revision != encounter.revision:
        raise ConflictError(
            "This consultation was changed in another window. Reload to see the latest note."
        )
    changed = []
    for field in NOTE_FIELD_NAMES:
        if field in data and getattr(encounter, field) != data[field]:
            setattr(encounter, field, data[field])
            changed.append(field)
    if changed:
        encounter.revision += 1
        encounter.last_saved_at = timezone.now()
        encounter.save(update_fields=[*changed, "revision", "last_saved_at", "updated_at"])
        _audit_note_save(request, encounter, changed)
    return encounter


NOTE_AUDIT_WINDOW = timedelta(minutes=10)


def _audit_note_save(request, encounter, changed):
    """Audit draft note edits by field name only (never the note text).

    Autosave runs while the doctor types, so one event per editor per encounter is written per
    window unless a field not yet listed in the last event changes.
    """
    from apps.audit import services as audit
    from apps.audit.models import AuditLog

    last = (
        AuditLog.objects.filter(
            action="encounter.note_save",
            entity="Encounter",
            record_id=str(encounter.pk),
            actor=request.user,
            created_at__gte=timezone.now() - NOTE_AUDIT_WINDOW,
        )
        .order_by("-created_at")
        .first()
    )
    if last is not None and set(changed) <= set(last.changes.get("fields", [])):
        return
    audit.record(
        request,
        "encounter.note_save",
        encounter,
        changes={"fields": sorted(changed), "revision": encounter.revision},
        summary=f"{encounter.number} draft edited: {', '.join(sorted(changed))}"[:255],
    )


@transaction.atomic
def complete(encounter, *, request) -> EncounterVersion:
    """Sign the note: store a read-only version, close the token and the appointment."""
    encounter = Encounter.objects.select_for_update(of=("self",)).get(pk=encounter.pk)
    _require_writable(request, encounter)
    if "consultation.sign" not in request.sd_perms:
        raise EncounterError("You are not allowed to sign consultations.")
    if not encounter.chief_complaint.strip():
        raise EncounterError("Record the chief complaint before completing.")
    if not (
        encounter.final_diagnosis.strip()
        or encounter.provisional_diagnosis.strip()
        or encounter.diagnoses.exists()
    ):
        raise EncounterError("Record a provisional or final diagnosis before completing.")
    if encounter.follow_up_date and encounter.follow_up_date < timezone.localdate():
        raise EncounterError("The follow-up date is in the past.")
    if encounter.referral_to.strip() and not encounter.referral_reason.strip():
        raise EncounterError("Give a reason for the referral.")

    from apps.prescriptions import services as rx_services

    rx = rx_services.prescription_for(encounter)
    content = encounter.note()
    content["diagnoses"] = [d.snapshot() for d in encounter.diagnoses.select_related("code")]
    content["prescription"] = rx.snapshot() if rx else []
    content["advice"] = rx.advice if rx else ""
    last = encounter.versions.aggregate(m=Max("version"))["m"] or 0
    version = EncounterVersion.objects.create(
        encounter=encounter,
        version=last + 1,
        content=content,
        signed_by=request.user,
        amendment_reason=encounter.reopen_reason,
    )
    encounter.status = Status.COMPLETED
    encounter.completed_at = version.signed_at
    encounter.completed_by = request.user
    encounter.reopen_reason = ""
    encounter.save(
        update_fields=["status", "completed_at", "completed_by", "reopen_reason", "updated_at"]
    )
    rx_services.sign_for(encounter)

    token = encounter.token
    if token is not None and token.status in (TokenStatus.CALLED, TokenStatus.IN_CONSULTATION):
        queue_services.complete(token)  # also completes its appointment
    if encounter.appointment is not None:
        _advance_appointment(encounter.appointment, ApptStatus.COMPLETED)
    return version


@transaction.atomic
def reopen(encounter, *, request, reason: str) -> Encounter:
    """Open a completed encounter for amendment. The signed versions stay unchanged."""
    if "consultation.reopen" not in request.sd_perms:
        raise EncounterError("You are not allowed to reopen consultations.")
    if not reason.strip():
        raise EncounterError("Give the reason for the amendment.")
    encounter = Encounter.objects.select_for_update(of=("self",)).get(pk=encounter.pk)
    if encounter.is_draft:
        raise EncounterError(f"{encounter.number} is already open.")
    if not can_write(request, encounter):
        raise EncounterError("Only the treating doctor can amend this consultation.")
    from apps.prescriptions.models import Prescription

    if (
        Prescription.objects.filter(encounter=encounter)
        .exclude(dispense_status=Prescription.Dispense.PENDING)
        .exists()
    ):
        raise EncounterError(
            "The pharmacy has already dispensed this prescription, so it cannot be amended. "
            "Record changes in a new consultation."
        )
    encounter.status = Status.DRAFT
    encounter.reopened_at = timezone.now()
    encounter.reopened_by = request.user
    encounter.reopen_reason = reason.strip()[:300]
    encounter.revision += 1
    encounter.save(
        update_fields=[
            "status",
            "reopened_at",
            "reopened_by",
            "reopen_reason",
            "revision",
            "updated_at",
        ]
    )
    from apps.prescriptions import services as rx_services

    rx_services.reopen_for(encounter)
    return encounter


# --- Doctor dashboard --------------------------------------------------------------------------


def follow_ups(doctor, *, days_ahead=7, days_back=14):
    """Completed visits whose follow-up falls in the window and has no later visit yet."""
    today = timezone.localdate()
    qs = (
        Encounter.objects.filter(
            doctor=doctor,
            status=Status.COMPLETED,
            follow_up_date__gte=today - timedelta(days=days_back),
            follow_up_date__lte=today + timedelta(days=days_ahead),
        )
        .select_related("patient")
        .order_by("follow_up_date")
    )
    result = []
    for e in qs:
        if Encounter.objects.filter(patient=e.patient, started_at__gt=e.started_at).exists():
            continue
        result.append(e)
    return result


# --- Diagnoses (Phase 10) ---------------------------------------------------------------------


def ensure_diagnosis_codes(organization) -> None:
    """Load the starter code list the first time an organization searches."""
    if DiagnosisCode.objects.filter(organization=organization).exists():
        return
    DiagnosisCode.objects.bulk_create(
        DiagnosisCode(organization=organization, code=code, title=title)
        for code, title in STARTER_CODES
    )


def search_codes(organization, q: str, limit=12):
    ensure_diagnosis_codes(organization)
    q = q.strip()
    if len(q) < 2:
        return DiagnosisCode.objects.none()
    qs = DiagnosisCode.objects.filter(organization=organization, is_active=True)
    for word in q.split():
        qs = qs.filter(models.Q(code__istartswith=word) | models.Q(title__icontains=word))
    return qs[:limit]


@transaction.atomic
def add_diagnosis(encounter, *, request, code=None, text="", kind="provisional"):
    encounter = Encounter.objects.select_for_update(of=("self",)).get(pk=encounter.pk)
    _require_writable(request, encounter)
    if code is not None and code.organization_id != encounter.organization_id:
        raise EncounterError("That diagnosis code is not available.")
    text = (code.title if code is not None else text).strip()[:200]
    if not text:
        raise EncounterError("Choose a diagnosis or type one.")
    if kind not in EncounterDiagnosis.Kind.values:
        raise EncounterError("Choose provisional or final.")
    existing = (
        encounter.diagnoses.filter(code=code)
        if code
        else encounter.diagnoses.filter(code__isnull=True, text__iexact=text)
    )
    if existing.exists():
        raise EncounterError(f"{text} is already on this consultation.")
    return EncounterDiagnosis.objects.create(
        encounter=encounter,
        code=code,
        text=text,
        kind=kind,
        is_primary=not encounter.diagnoses.exists(),
        added_by=request.user,
    )


@transaction.atomic
def update_diagnosis(diagnosis, *, request, kind=None, primary=None):
    _require_writable(
        request, Encounter.objects.select_for_update(of=("self",)).get(pk=diagnosis.encounter_id)
    )
    if kind is not None:
        if kind not in EncounterDiagnosis.Kind.values:
            raise EncounterError("Choose provisional or final.")
        diagnosis.kind = kind
    if primary:
        diagnosis.encounter.diagnoses.exclude(pk=diagnosis.pk).update(is_primary=False)
        diagnosis.is_primary = True
    diagnosis.save()
    return diagnosis


@transaction.atomic
def remove_diagnosis(diagnosis, *, request):
    encounter = Encounter.objects.select_for_update(of=("self",)).get(pk=diagnosis.encounter_id)
    _require_writable(request, encounter)
    was_primary = diagnosis.is_primary
    diagnosis.delete()  # draft only; the signed list is kept in EncounterVersion.content
    if was_primary:
        first = encounter.diagnoses.first()
        if first:
            first.is_primary = True
            first.save(update_fields=["is_primary", "updated_at"])


def diagnosis_history(patient, organization):
    """Diagnoses from signed consultations, newest first, one row per diagnosis."""
    seen, rows = set(), []
    qs = (
        EncounterDiagnosis.objects.filter(
            encounter__organization=organization,
            encounter__patient=patient,
            encounter__status=Status.COMPLETED,
        )
        .select_related("code", "encounter__doctor__user")
        .order_by("-encounter__started_at")
    )
    for d in qs:
        key = d.code_id or d.text.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(d)
    return rows
