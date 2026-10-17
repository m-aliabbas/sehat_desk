"""Patient registration, search and duplicate detection."""

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from django.db import transaction
from django.db.models import Q

from apps.core.models import next_number

from .identity import digits, normalize_cnic, normalize_phone
from .models import Patient, PatientIdentifier


def format_mrn(organization, number: int) -> str:
    return f"{organization.patient_id_prefix}-{number:06d}"


@transaction.atomic
def register_patient(*, organization, branch, user, data: dict) -> Patient:
    patient = Patient(organization=organization, registered_branch=branch, created_by=user, **data)
    patient.mrn = format_mrn(organization, next_number(organization, "patient"))
    patient.full_clean(exclude=["photo"])
    patient.save()
    return patient


# --- Duplicate detection ---------------------------------------------------------------------


@dataclass
class DuplicateMatch:
    patient: Patient
    reasons: list[str] = field(default_factory=list)
    strong: bool = False


def _name_similarity(a: str, b: str) -> float:
    a, b = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not a or not b:
        return 0.0
    # Token order varies ("Khan Ayesha"), so compare sorted tokens as well.
    sorted_a, sorted_b = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    return max(
        SequenceMatcher(None, a, b).ratio(), SequenceMatcher(None, sorted_a, sorted_b).ratio()
    )


def find_duplicates(
    organization, *, full_name="", phone="", cnic="", date_of_birth=None, exclude_pk=None
):
    """Existing patients that may be the same person (PRD §5.1 duplicate detection).

    Strong: same CNIC, or a similar name with the same phone or date of birth.
    Weak: same phone only (families often share one number).
    """
    phone = normalize_phone(phone)
    cnic = normalize_cnic(cnic)
    name_tokens = [t for t in full_name.lower().split() if len(t) >= 2]

    candidates = Q()
    if cnic:
        candidates |= Q(cnic=cnic)
    if phone:
        candidates |= Q(phone=phone) | Q(emergency_contact_phone=phone)
    if date_of_birth:
        candidates |= Q(date_of_birth=date_of_birth)
    if name_tokens:
        # Cheap pre-filter: share the first three letters of any name token.
        for token in name_tokens:
            candidates |= Q(name_search__contains=token[:3])
    if not candidates:
        return []

    qs = Patient.objects.filter(organization=organization, is_active=True).filter(candidates)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)

    matches = []
    for p in qs[:300]:
        similar = bool(full_name) and _name_similarity(full_name, p.full_name) >= 0.82
        m = DuplicateMatch(p)
        if cnic and p.cnic == cnic:
            m.reasons.append("Same CNIC")
            m.strong = True
        if phone and phone in (p.phone, p.emergency_contact_phone):
            m.reasons.append("Same mobile number")
            m.strong = m.strong or similar
        if date_of_birth and p.date_of_birth == date_of_birth and similar:
            m.reasons.append("Same date of birth")
            m.strong = True
        if similar:
            m.reasons.append("Similar name")
        if m.reasons:
            matches.append(m)
    matches.sort(key=lambda m: (not m.strong, -len(m.reasons)))
    return matches[:10]


# --- Search ----------------------------------------------------------------------------------


def search_patients(qs, query: str):
    """Search by MRN, phone, CNIC or name. Returns (queryset, suggestions)."""
    q = (query or "").strip()
    if not q:
        return qs, []
    d = digits(q)
    cond = Q(mrn__iexact=q) | Q(mrn__icontains=q)
    if d and len(d) >= 4:
        cond |= Q(phone__contains=normalize_phone(q) or d) | Q(phone__contains=d)
        cond |= Q(cnic__contains=d) | Q(emergency_contact_phone__contains=d)
        cond |= Q(identifiers__value__icontains=q)
    letters = [t for t in q.lower().split() if not t.isdigit()]
    if letters:
        name_cond = Q()
        for token in letters:
            name_cond &= Q(name_search__contains=token)
        cond |= name_cond
    results = qs.filter(cond).distinct()

    suggestions = []
    if letters and not results.exists():
        # "Did you mean": tolerate spelling variants (Ayesha / Aisha, Muhammad / Mohammad).
        pool = qs.filter(name_search__startswith=letters[0][:1])[:2000]
        typed = " ".join(letters)

        def score(p):
            # Compare with the whole name and with each word, so "Aysha" finds "Ayesha Khan".
            words = p.name_search.split()
            return max(
                [_name_similarity(typed, p.full_name)] + [_name_similarity(typed, w) for w in words]
            )

        scored = [(p, score(p)) for p in pool]
        suggestions = [p for p, score in sorted(scored, key=lambda x: -x[1]) if score >= 0.7][:5]
    return results, suggestions


def add_identifier(patient, kind, value):
    value = value.strip()
    if kind == PatientIdentifier.Kind.GUARDIAN_CNIC:
        value = normalize_cnic(value)
    return PatientIdentifier.objects.get_or_create(patient=patient, kind=kind, value=value)[0]
