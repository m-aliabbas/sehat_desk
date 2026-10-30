"""The All menu page: every screen outside the short admin sidebar, grouped by category.

Hiding a tile is a convenience only; each screen checks its own permission. A tile needs one of
its `permissions`, and disappears when its `feature` module is switched off. `locked_if_denied`
tiles stay on the page without a link, so an admin can see the terminal exists but is not in
their role (clinical screens are not part of the default admin role).
"""

from dataclasses import dataclass

from django.urls import reverse

from .features import enabled_features


@dataclass(frozen=True)
class HubLink:
    label: str
    description: str
    icon: str
    url_name: str
    permissions: tuple[str, ...]
    query: str = ""
    feature: str | None = None
    locked_if_denied: bool = False


LAB_DESK = ("lab.order.create", "lab.sample.collect", "lab.result.enter", "lab.result.verify")
MASTERDATA = ("admin.masterdata.manage",)
INVENTORY = ("pharmacy.inventory.manage",)

HUB_GROUPS: list[tuple[str, str, str, list[HubLink]]] = [
    (
        "HR & accounts",
        "users",
        "blue",
        [
            HubLink("Staff", "Accounts, roles and access", "users", "accounts:staff-list",
                    ("admin.users.view",)),
            HubLink("Attendance & pay", "Attendance, leave and payroll", "calendar", "hr:home",
                    ("hr.self", "hr.attendance.manage", "hr.pay.manage")),
            HubLink("Billing & payments", "Invoices, payments and refunds", "credit-card",
                    "billing:invoice-list", ("billing.view",)),
        ],
    ),
    (
        "Doctors, nurses & wards",
        "stethoscope",
        "green",
        [
            HubLink("Doctors", "Fees, schedules and leave", "stethoscope",
                    "masterdata:doctor-list", MASTERDATA),
            HubLink("Departments", "Departments and token letters", "building",
                    "masterdata:department-list", MASTERDATA),
            HubLink("Nursing", "Ward tasks and shift handover", "clipboard", "nursing:workspace",
                    ("nursing.notes",), feature="nursing", locked_if_denied=True),
            HubLink("IPD & wards", "Bed board and admissions", "bed", "ipd:board",
                    ("ipd.manage", "nursing.notes", "ipd.facility.manage"), feature="ipd"),
            HubLink("Ward & bed setup", "Wards, rooms and beds", "bed", "ipd:setup",
                    ("ipd.facility.manage",), feature="ipd"),
            HubLink("Patients", "Patient records", "user", "patients:list", ("patient.view",)),
            HubLink("Appointments", "Calendar and bookings", "calendar", "appointments:calendar",
                    ("appointment.view",)),
            HubLink("Diagnosis codes", "Codes used in consultations", "file",
                    "encounters:diagnosis-code-list", MASTERDATA),
            HubLink("Medicines", "Medicines doctors can prescribe", "pill",
                    "prescriptions:medicine-list", MASTERDATA),
        ],
    ),
    (
        "Inventory",
        "package",
        "orange",
        [
            HubLink("Stock overview", "Low stock and expiry alerts", "package",
                    "pharmacy:dashboard", (*INVENTORY, "pharmacy.sale"), feature="pharmacy"),
            HubLink("Products", "Items, packs and reorder levels", "clipboard",
                    "pharmacy:product-list", INVENTORY, feature="pharmacy"),
            HubLink("Purchases", "Purchase orders and receiving", "inbox",
                    "pharmacy:purchase-list", INVENTORY, feature="pharmacy"),
            HubLink("Suppliers", "Vendors and contacts", "building", "pharmacy:vendor-list",
                    INVENTORY, feature="pharmacy"),
        ],
    ),
    (
        "Terminals",
        "ticket",
        "purple",
        [
            HubLink("Reception desk", "Walk-ins and tokens", "ticket", "queue:desk",
                    ("queue.manage",), locked_if_denied=True),
            HubLink("Vitals station", "Record patient vitals", "heart-pulse", "vitals:station",
                    ("vitals.record",), locked_if_denied=True),
            HubLink("Doctor workspace", "Queue and consultations", "stethoscope",
                    "encounters:workspace", ("consultation.view",), locked_if_denied=True),
            HubLink("Lab desk", "Samples, results and verification", "flask", "lab:worklist",
                    LAB_DESK, feature="lab", locked_if_denied=True),
            HubLink("Pharmacy till", "Sales and dispensing", "pill", "pharmacy:pos",
                    ("pharmacy.sale",), feature="pharmacy", locked_if_denied=True),
            HubLink("Cashier", "Collect patient payments", "credit-card", "billing:invoice-list",
                    ("billing.payment.collect",), locked_if_denied=True),
        ],
    ),
    (
        "Procedures",
        "heart-pulse",
        "red",
        [
            HubLink("Add new procedure", "Add ECG, ultrasound, an operation and more", "plus",
                    "masterdata:service-create", MASTERDATA, query="kind=procedure"),
            HubLink("Existing procedures", "Edit names, prices or deactivate", "clipboard",
                    "masterdata:service-list", MASTERDATA, query="kind=procedure"),
            HubLink("Patient procedures", "Procedures added to patients", "heart-pulse",
                    "procedures:procedure-list", ("procedure.view",)),
        ],
    ),
    (
        "Lab tests",
        "flask",
        "yellow",
        [
            HubLink("Tests, fees & reference values", "See and edit every test", "flask",
                    "lab:catalog", ("lab.catalog.manage",), feature="lab"),
            HubLink("Add new test", "Fee, unit and reference ranges", "plus", "lab:test-create",
                    ("lab.catalog.manage",), feature="lab"),
            HubLink("Add test panel", "Group tests at one price", "plus", "lab:panel-create",
                    ("lab.catalog.manage",), feature="lab"),
        ],
    ),
]  # fmt: skip


def build_hub(request) -> list[dict]:
    """Categories with the tiles this user may see; empty categories are dropped."""
    held = getattr(request, "sd_perms", frozenset())
    flags = enabled_features(getattr(request, "organization", None))
    groups = []
    for title, icon, color, links in HUB_GROUPS:
        tiles = []
        for link in links:
            if link.feature and not flags[link.feature]:
                continue
            allowed = any(code in held for code in link.permissions)
            if not allowed and not link.locked_if_denied:
                continue
            url = None
            if allowed:
                url = reverse(link.url_name)
                if link.query:
                    url = f"{url}?{link.query}"
            tiles.append(
                {
                    "label": link.label,
                    "description": link.description,
                    "icon": link.icon,
                    "url": url,
                }
            )
        if tiles:
            groups.append({"title": title, "icon": icon, "color": color, "tiles": tiles})
    return groups
