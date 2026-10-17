"""Sidebar navigation, filtered per user.

Hiding a link is a convenience only. Every view still checks permissions on the server
(CLAUDE.md, rules for code work). When a module is built, set its `url_name`.
`permissions` lists SehatDesk permission codes (apps/accounts/permissions.py); holding any one
of them in the current branch shows the item.
"""

from dataclasses import dataclass

from django.urls import NoReverseMatch, reverse


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    icon: str
    url_name: str | None = None  # None until the module exists; links to a placeholder page
    permissions: tuple[str, ...] = ()  # any one of these; empty = any signed-in user


NAV_ITEMS = [
    NavItem("dashboard", "Dashboard", "home", url_name="core:dashboard"),
    NavItem("reception", "Reception & tokens", "ticket", "queue:desk", ("queue.manage",)),
    NavItem("vitals", "Vitals station", "heart-pulse", "vitals:station", ("vitals.record",)),
    NavItem(
        "appointments", "Appointments", "calendar", "appointments:calendar", ("appointment.view",)
    ),
    NavItem("patients", "Patients", "user", "patients:list", ("patient.view",)),
    NavItem(
        "opd", "Doctor workspace", "stethoscope", "encounters:workspace", ("consultation.view",)
    ),
    NavItem(
        "ipd",
        "IPD & wards",
        "bed",
        "ipd:board",
        ("ipd.manage", "nursing.notes", "ipd.facility.manage"),
    ),
    NavItem("nursing", "Nursing", "clipboard", "nursing:workspace", ("nursing.notes",)),
    NavItem(
        "laboratory",
        "Laboratory",
        "flask",
        "lab:worklist",
        (
            "lab.order.create",
            "lab.sample.collect",
            "lab.result.enter",
            "lab.result.verify",
            "lab.catalog.manage",
        ),
    ),
    NavItem("pharmacy", "Pharmacy", "pill", "pharmacy:pos", ("pharmacy.sale",)),
    NavItem(
        "billing", "Billing & payments", "credit-card", "billing:invoice-list", ("billing.view",)
    ),
    NavItem(
        "inventory",
        "Inventory",
        "package",
        "pharmacy:dashboard",
        ("pharmacy.inventory.manage", "pharmacy.sale"),
    ),
    NavItem(
        "reports",
        "Reports",
        "bar-chart",
        "reports:hub",
        ("reports.view", "reports.financial.view", "pharmacy.inventory.manage"),
    ),
    NavItem("staff", "Staff", "users", "accounts:staff-list", ("admin.users.view",)),
    NavItem(
        "settings",
        "Settings",
        "sliders",
        "organizations:settings",
        (
            "admin.organization.manage",
            "admin.branches.manage",
            "admin.roles.manage",
            "admin.masterdata.manage",
            "admin.audit.view",
        ),
    ),
    NavItem(
        "design-system",
        "Design system",
        "palette",
        "core:design-system",
        ("admin.organization.manage",),
    ),
]

PLACEHOLDER_KEYS = {item.key for item in NAV_ITEMS if item.url_name is None}


def item_is_visible(item: NavItem, request) -> bool:
    if not request.user.is_authenticated:
        return False
    from .features import disabled_nav_keys

    if item.key in disabled_nav_keys(getattr(request, "organization", None)):
        return False
    if item.permissions:
        held = getattr(request, "sd_perms", frozenset())
        return any(code in held for code in item.permissions)
    return True


def item_url(item: NavItem) -> str:
    if item.url_name:
        try:
            return reverse(item.url_name)
        except NoReverseMatch:
            pass
    return reverse("core:module-placeholder", args=[item.key])


def build_nav(request) -> list[dict]:
    """Visible nav entries for this request, with the active one marked."""
    match = getattr(request, "resolver_match", None)
    current_key = None
    if match is not None:
        current_key = match.kwargs.get("module") or _NAV_KEY_BY_URL_NAME.get(match.view_name)
        if current_key is None and match.namespace and match.url_name:
            section = f"{match.namespace}:{match.url_name.split('-')[0]}"
            current_key = _NAV_KEY_BY_SECTION.get(section)

    return [
        {
            "key": item.key,
            "label": item.label,
            "icon": item.icon,
            "url": item_url(item),
            "active": item.key == current_key,
            "coming_soon": item.url_name is None,
        }
        for item in NAV_ITEMS
        if item_is_visible(item, request)
    ]


_NAV_KEY_BY_URL_NAME = {item.url_name: item.key for item in NAV_ITEMS if item.url_name}
# Sub-pages highlight their section, e.g. "accounts:staff-detail" -> Staff.
_NAV_KEY_BY_SECTION = {
    "accounts:staff": "staff",
    "accounts:role": "settings",
    "accounts:permission": "settings",
    "organizations:hospital": "settings",
    "organizations:branch": "settings",
    "organizations:settings": "settings",
    "queue:walk": "reception",
    "queue:counters": "reception",
    "queue:counter": "reception",
    "queue:slip": "reception",
    "queue:doctor": "opd",
    "encounters:detail": "opd",
    "encounters:reopen": "opd",
    "encounters:history": "opd",
    "vitals:record": "vitals",
    "prescriptions:medicine": "settings",
    "encounters:diagnosis": "settings",
    "lab:order": "laboratory",
    "lab:results": "laboratory",
    "lab:verify": "laboratory",
    "lab:report": "laboratory",
    "lab:sample": "laboratory",
    "lab:amend": "laboratory",
    "lab:catalog": "laboratory",
    "lab:test": "laboratory",
    "lab:panel": "laboratory",
    "pharmacy:product": "inventory",
    "pharmacy:category": "inventory",
    "pharmacy:receive": "inventory",
    "pharmacy:batch": "inventory",
    "pharmacy:ledger": "inventory",
    "pharmacy:vendor": "inventory",
    "pharmacy:purchase": "inventory",
    "pharmacy:pos": "pharmacy",
    "pharmacy:rx": "pharmacy",
    "pharmacy:sale": "pharmacy",
    "prescriptions:print": "opd",
    "billing:invoice": "billing",
    "billing:payment": "billing",
    "billing:receipt": "billing",
    "billing:settings": "settings",
    "billing:method": "settings",
    "masterdata:department": "settings",
    "masterdata:doctor": "settings",
    "masterdata:service": "settings",
    "masterdata:specialty": "settings",
    "masterdata:category": "settings",
    "masterdata:leave": "settings",
    "ipd:admission": "ipd",
    "ipd:admit": "ipd",
    "ipd:discharge": "ipd",
    "ipd:summary": "ipd",
    "ipd:bed": "ipd",
    "ipd:setup": "ipd",
    "ipd:round": "ipd",
    "ipd:medication": "ipd",
    "nursing:handover": "nursing",
    "notifications:messages": "settings",
    "reports:report": "reports",
    "reports:lab": "laboratory",
    "reports:pharmacy": "pharmacy",
    "audit:log": "settings",
    "audit:entry": "settings",
}
