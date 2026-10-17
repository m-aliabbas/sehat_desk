"""Global search (Phase 22): one query across patients, invoices, appointments, lab orders,
medicines and staff.

Each provider declares the permissions that may see it (any one) and searches only through
`scoped()` querysets, so results are filtered by organization, branch and role on the server.
A provider the user may not use is skipped entirely, not hidden in the template.
The query text is never logged (it often contains patient names or phone numbers).
"""

from dataclasses import dataclass
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from apps.core.scoping import scoped

MIN_LENGTH = 2
LAB_VIEW = (
    "consultation.view",
    "lab.order.create",
    "lab.sample.collect",
    "lab.result.enter",
    "lab.result.verify",
    "lab.catalog.manage",
)
PRODUCT_VIEW = ("pharmacy.inventory.manage", "pharmacy.sale")
MEDICINE_VIEW = ("admin.masterdata.manage", "pharmacy.inventory.manage")


@dataclass(frozen=True)
class Hit:
    title: str
    subtitle: str
    url: str
    badge: str = ""
    status: str = ""


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    icon: str
    permissions: tuple[str, ...]
    find: object  # (request, q, limit) -> list[Hit]


def _words(q):
    return [w for w in q.lower().split() if w]


def _patient_q(q, prefix="patient__"):
    """Match a related patient by MRN (exact or partial) or every word of the name."""
    cond = Q(**{f"{prefix}mrn__iexact": q}) | Q(**{f"{prefix}mrn__icontains": q})
    words = [w for w in _words(q) if not w.isdigit()]
    if words:
        name = Q()
        for w in words:
            name &= Q(**{f"{prefix}name_search__contains": w})
        cond |= name
    return cond


def _patients(request, q, limit):
    from apps.patients.models import Patient
    from apps.patients.services import search_patients

    results, _ = search_patients(scoped(Patient, request).filter(is_active=True), q)
    return [
        Hit(
            title=p.full_name,
            subtitle=" · ".join(x for x in [p.mrn, p.age_label, p.phone_display] if x),
            url=reverse("patients:detail", args=[p.pk]),
            badge="Patient",
        )
        for p in results.order_by("full_name")[:limit]
    ]


def _invoices(request, q, limit):
    from apps.billing.models import Invoice

    qs = scoped(Invoice, request).filter(
        Q(number__icontains=q) | Q(customer_name__icontains=q) | _patient_q(q)
    )
    return [
        Hit(
            title=f"{inv.number} · {inv.bill_to}",
            subtitle=f"{inv.created_at:%d %b %Y} · total {inv.grand_total:,.2f}"
            + (f" · balance {inv.balance_due:,.2f}" if inv.is_outstanding else ""),
            url=reverse("billing:invoice-detail", args=[inv.pk]),
            badge="Invoice",
            status=inv.status,
        )
        for inv in qs.select_related("patient").order_by("-created_at")[:limit]
    ]


def _appointments(request, q, limit):
    from apps.appointments.models import Appointment

    qs = scoped(Appointment, request).filter(Q(number__icontains=q) | _patient_q(q))
    return [
        Hit(
            title=f"{a.number} · {a.patient.full_name}",
            subtitle=f"{a.date:%d %b %Y} {a.start_time:%H:%M} · {a.doctor}",
            url=reverse("appointments:detail", args=[a.pk]),
            badge="Appointment",
            status=a.status,
        )
        for a in qs.select_related("patient", "doctor__user").order_by("-date", "-start_time")[
            :limit
        ]
    ]


def _lab_orders(request, q, limit):
    from apps.lab.models import LabOrder

    qs = scoped(LabOrder, request).filter(
        Q(number__icontains=q) | Q(samples__accession__iexact=q) | _patient_q(q)
    )
    return [
        Hit(
            title=f"{o.number} · {o.patient.full_name}",
            subtitle=f"{o.created_at:%d %b %Y} · {o.get_priority_display()}",
            url=reverse("lab:order-detail", args=[o.pk]),
            badge="Lab order",
            status=o.status,
        )
        for o in qs.select_related("patient").distinct().order_by("-created_at")[:limit]
    ]


def _medicines(request, q, limit):
    """Pharmacy products (stock screens) for pharmacy staff, else the medicine master."""
    held = request.sd_perms
    words = _words(q)
    if any(c in held for c in PRODUCT_VIEW):
        from apps.pharmacy.models import Product

        qs = scoped(Product, request)
        for w in words:
            qs = qs.filter(search_text__contains=w)
        qs = qs | scoped(Product, request).filter(Q(sku__iexact=q) | Q(barcode=q))
        return [
            Hit(
                title=p.display_name,
                subtitle=" · ".join(x for x in [p.generic_name, p.sku] if x)
                + ("" if p.is_active else " · inactive"),
                url=reverse("pharmacy:product-detail", args=[p.pk]),
                badge="Medicine",
            )
            for p in qs.distinct().order_by("brand_name")[:limit]
        ]
    from apps.prescriptions.models import Medicine

    qs = scoped(Medicine, request)
    for w in words:
        qs = qs.filter(search_text__contains=w)
    url = reverse("prescriptions:medicine-list")
    return [
        Hit(
            title=str(m),
            subtitle="Medicine master",
            url=f"{url}?{urlencode({'q': m.brand_name})}",
            badge="Medicine",
        )
        for m in qs.order_by("brand_name")[:limit]
    ]


def _staff(request, q, limit):
    from apps.accounts.models import User

    qs = User.objects.filter(organization=request.organization)
    for w in _words(q):
        qs = qs.filter(
            Q(first_name__icontains=w)
            | Q(last_name__icontains=w)
            | Q(username__icontains=w)
            | Q(email__icontains=w)
            | Q(designation__icontains=w)
        )
    return [
        Hit(
            title=u.get_full_name() or u.username,
            subtitle=" · ".join(x for x in [u.designation, u.username] if x)
            + ("" if u.is_active else " · deactivated"),
            url=reverse("accounts:staff-detail", args=[u.pk]),
            badge="Staff",
        )
        for u in qs.order_by("first_name", "last_name")[:limit]
    ]


PROVIDERS = [
    Provider("patients", "Patients", "user", ("patient.view",), _patients),
    Provider("appointments", "Appointments", "calendar", ("appointment.view",), _appointments),
    Provider("invoices", "Invoices", "credit-card", ("billing.view",), _invoices),
    Provider("lab", "Lab orders", "flask", LAB_VIEW, _lab_orders),
    Provider("medicines", "Medicines", "pill", PRODUCT_VIEW + MEDICINE_VIEW, _medicines),
    Provider("staff", "Staff", "users", ("admin.users.view",), _staff),
]


def providers_for(request) -> list[Provider]:
    from apps.core.features import is_enabled

    lab_on = is_enabled(request.organization, "lab")
    return [
        p
        for p in PROVIDERS
        if any(c in request.sd_perms for c in p.permissions) and (lab_on or p.key != "lab")
    ]


def search(request, q: str, *, limit: int = 5, only: str = "") -> list[dict]:
    """Grouped results: [{"key", "label", "icon", "hits", "more"}] for the user's providers."""
    q = (q or "").strip()[:100]
    if len(q) < MIN_LENGTH:
        return []
    groups = []
    for p in providers_for(request):
        if only and p.key != only:
            continue
        hits = p.find(request, q, limit + 1)
        if hits:
            groups.append(
                {
                    "key": p.key,
                    "label": p.label,
                    "icon": p.icon,
                    "hits": hits[:limit],
                    "more": len(hits) > limit,
                }
            )
    return groups


# --- Recent searches (optional, per user, kept in the session only) ---------------------------

RECENT_KEY = "sd_recent_searches"
RECENT_MAX = 8


def remember(request, q: str) -> None:
    q = (q or "").strip()[:100]
    if len(q) < MIN_LENGTH:
        return
    recent = [r for r in request.session.get(RECENT_KEY, []) if r.lower() != q.lower()]
    request.session[RECENT_KEY] = [q, *recent][:RECENT_MAX]


def recent(request) -> list[str]:
    return request.session.get(RECENT_KEY, [])
