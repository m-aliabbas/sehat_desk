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
    args: tuple = ()
    feature: str | None = None
    locked_if_denied: bool = False


LAB_DESK = ("lab.order.create", "lab.sample.collect", "lab.result.enter", "lab.result.verify")
MASTERDATA = ("admin.masterdata.manage",)
INVENTORY = ("pharmacy.inventory.manage",)
FINANCIAL = ("reports.financial.view",)
SUPPLIER_BOOKS = (*FINANCIAL, *INVENTORY)

HUB_GROUPS: list[tuple[str, str, str, list[HubLink]]] = [
    (
        "Front desk",
        "ticket",
        "teal",
        [
            HubLink("Register patient", "Full patient record", "user",
                    "patients:register", ("patient.create",)),
            HubLink("New booking", "Patient, doctor, token and fee", "calendar",
                    "appointments:visit-new", ("appointment.manage",)),
            HubLink("Patients", "Patient records", "user", "patients:list", ("patient.view",)),
            HubLink("Appointments", "Calendar and bookings", "calendar", "appointments:calendar",
                    ("appointment.view",)),
            HubLink("Appointment list", "Every booking, with filters", "calendar",
                    "appointments:list", ("appointment.view",)),
            HubLink("Doctor queue", "Call the next patient", "ticket",
                    "queue:doctor", ("queue.manage", "consultation.view")),
        ],
    ),
    (
        "Terminals",
        "ticket",
        "purple",
        [
            HubLink("Reception", "Walk-ins and tokens", "ticket", "queue:desk",
                    ("queue.manage",), locked_if_denied=True),
            HubLink("Vitals station", "Record patient vitals", "heart-pulse", "vitals:station",
                    ("vitals.record",), locked_if_denied=True),
            HubLink("Doctor workspace", "Queue and consultations", "stethoscope",
                    "encounters:workspace", ("consultation.view",), locked_if_denied=True),
            HubLink("Laboratory", "Samples, results and verification", "flask", "lab:worklist",
                    LAB_DESK, feature="lab", locked_if_denied=True),
            HubLink("Pharmacy", "Sales and dispensing", "pill", "pharmacy:pos",
                    ("pharmacy.sale",), feature="pharmacy", locked_if_denied=True),
            HubLink("Cashier", "Collect patient payments", "credit-card", "billing:invoice-list",
                    ("billing.payment.collect",), locked_if_denied=True),
            HubLink("Patient flow", "Who is where; take a patient by token", "ticket",
                    "flow:board", ("queue.manage", "pharmacy.sale", "lab.sample.collect",
                                   "billing.payment.collect")),
        ],
    ),
    (
        "Billing",
        "credit-card",
        "green",
        [
            HubLink("New invoice", "Bill a patient", "credit-card",
                    "billing:invoice-create", ("billing.payment.collect",)),
            HubLink("Payments", "Money received", "credit-card",
                    "billing:payment-list", ("billing.view",)),
        ],
    ),
    (
        "Wards",
        "bed",
        "green",
        [
            HubLink("IPD & wards", "Bed board and admissions", "bed", "ipd:board",
                    ("ipd.manage", "nursing.notes", "ipd.facility.manage"), feature="ipd"),
            HubLink("Admit patient", "Start an admission", "bed",
                    "ipd:admit", ("ipd.manage",), feature="ipd"),
            HubLink("Admissions", "Everyone admitted", "bed",
                    "ipd:admission-list", ("ipd.manage", "nursing.notes"), feature="ipd"),
            HubLink("Nursing", "Ward tasks and shift handover", "clipboard", "nursing:workspace",
                    ("nursing.notes",), feature="nursing", locked_if_denied=True),
            HubLink("Shift handover", "Notes between nurses", "clipboard",
                    "nursing:handover", ("nursing.notes",), feature="ipd"),
            HubLink("Ward & bed setup", "Wards, rooms and beds", "bed", "ipd:setup",
                    ("ipd.facility.manage",), feature="ipd"),
        ],
    ),
    (
        "Pharmacy & stock",
        "pill",
        "orange",
        [
            HubLink("Prescriptions to dispense", "Sent by doctors", "pill",
                    "pharmacy:rx-queue", ("pharmacy.sale",), feature="pharmacy"),
            HubLink("Held sales", "Sales put on hold", "pill",
                    "pharmacy:pos-held", ("pharmacy.sale",), feature="pharmacy"),
            HubLink("Sales and returns", "Past sales, take a return", "pill",
                    "pharmacy:sale-list", ("pharmacy.sale", "pharmacy.refund"), feature="pharmacy"),
        ],
    ),
    (
        "Inventory",
        "package",
        "orange",
        [
            HubLink("Stock overview", "Low stock and expiry alerts", "package",
                    "pharmacy:dashboard", (*INVENTORY, "pharmacy.sale"), feature="pharmacy"),
            HubLink("Receive stock", "Add stock from a bill", "package",
                    "pharmacy:receive", INVENTORY, feature="pharmacy"),
            HubLink("Purchases", "Purchase orders and receiving", "inbox",
                    "pharmacy:purchase-list", INVENTORY, feature="pharmacy"),
            HubLink("New purchase", "Enter a supplier's bill", "package",
                    "pharmacy:purchase-create", INVENTORY, feature="pharmacy"),
            HubLink("Automatic reorder", "Low stock into draft orders by supplier", "inbox",
                    "pharmacy:purchase-auto", INVENTORY, feature="pharmacy"),
            HubLink("Suppliers", "Vendors and contacts", "building", "pharmacy:vendor-list",
                    INVENTORY, feature="pharmacy"),
            HubLink("Stock ledger", "Every stock movement", "package",
                    "pharmacy:ledger", (*INVENTORY, "pharmacy.sale"), feature="pharmacy"),
        ],
    ),
    (
        "Products",
        "clipboard",
        "orange",
        [
            HubLink("Products", "Items, packs and reorder levels", "clipboard",
                    "pharmacy:product-list", INVENTORY, feature="pharmacy"),
            HubLink("Add many products", "Scan or type a whole list at once", "plus",
                    "pharmacy:product-bulk", INVENTORY, feature="pharmacy"),
            HubLink("Barcodes", "Generate for existing products, print labels", "clipboard",
                    "pharmacy:barcodes", INVENTORY, feature="pharmacy"),
            HubLink("Units of measure", "Packing suggested when adding products", "clipboard",
                    "pharmacy:uom", INVENTORY, feature="pharmacy"),
            HubLink("Categories & tax", "Tax and prescription defaults", "clipboard",
                    "pharmacy:categories", INVENTORY, feature="pharmacy"),
        ],
    ),
    (
        "Lab tests",
        "flask",
        "yellow",
        [
            HubLink("New lab order", "Order tests for a patient", "flask",
                    "lab:order-create", ("lab.order.create",), feature="lab"),
            HubLink("Lab test prices", "Tests, packages, prices and timing", "flask",
                    "lab:tests", ("queue.manage", "consultation.view", "lab.order.create"),
                    feature="lab"),
            HubLink("Manage lab tests", "See and edit every test", "flask",
                    "lab:catalog", ("lab.catalog.manage",), feature="lab"),
            HubLink("Add new test", "Fee, unit and reference ranges", "plus", "lab:test-create",
                    ("lab.catalog.manage",), feature="lab"),
            HubLink("Add test panel", "Group tests at one price", "plus", "lab:panel-create",
                    ("lab.catalog.manage",), feature="lab"),
        ],
    ),
    (
        "Procedures",
        "heart-pulse",
        "purple",
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
        "Clinical lists",
        "stethoscope",
        "green",
        [
            HubLink("Doctors", "Fees, schedules and leave", "stethoscope",
                    "masterdata:doctor-list", MASTERDATA),
            HubLink("Departments", "Departments and token letters", "building",
                    "masterdata:department-list", MASTERDATA),
            HubLink("Diagnosis codes", "Codes used in consultations", "file",
                    "encounters:diagnosis-code-list", MASTERDATA),
            HubLink("Medicines", "Medicines doctors can prescribe", "pill",
                    "prescriptions:medicine-list", MASTERDATA),
        ],
    ),
    (
        "Finance",
        "credit-card",
        "green",
        [
            HubLink("Finance overview", "Money in and out", "bar-chart",
                    "finance:dashboard", ("finance.view", "finance.expense.manage")),
            HubLink("New expense", "Record money spent", "plus",
                    "finance:expense-create", ("finance.expense.manage",)),
            HubLink("Expenses", "All expenses", "credit-card",
                    "finance:expense-list", ("finance.view", "finance.expense.manage")),
            HubLink("Pay a supplier", "Settle a supplier's bills", "credit-card",
                    "finance:supplier-pay", ("finance.expense.manage",)),
            HubLink("Profit and loss", "Income against costs", "bar-chart",
                    "finance:pnl", ("finance.view",)),
            HubLink("Ledger", "Accounts ledger", "file", "finance:ledger", ("finance.view",)),
            HubLink("Expense categories", "Kinds of expense", "sliders",
                    "finance:category-list", ("finance.expense.manage",)),
        ],
    ),
    (
        "Reports",
        "file",
        "purple",
        [
            HubLink("All reports", "Every report, with Excel and PDF export", "file",
                    "reports:hub", ("reports.view", *SUPPLIER_BOOKS)),
            HubLink("Daily sale and purchase", "Both side by side, day by day", "file",
                    "reports:report", FINANCIAL, args=("daily-sale-purchase",)),
            HubLink("Daily sales", "Pharmacy sales and hospital bills", "file",
                    "reports:report", FINANCIAL, args=("daily-sales",)),
            HubLink("Daily purchases", "Supplier bills, returns and payments", "file",
                    "reports:report", SUPPLIER_BOOKS, args=("daily-purchases",)),
            HubLink("Supplier ledger", "Khata of each supplier", "building",
                    "reports:report", SUPPLIER_BOOKS, args=("supplier-statement",)),
            HubLink("Supplier balances", "What is owed to each supplier", "building",
                    "reports:report", SUPPLIER_BOOKS, args=("supplier-balances",)),
            HubLink("Profit and loss", "Income, costs and profit by date", "file",
                    "reports:report", FINANCIAL, args=("profit-and-loss",)),
        ],
    ),
    (
        "HR & accounts",
        "users",
        "blue",
        [
            HubLink("Staff", "Accounts, roles and access", "users", "accounts:staff-list",
                    ("admin.users.view",)),
            HubLink("Attendance & pay", "Attendance, leave and payroll", "calendar", "hr:home",
                    ("hr.self", "hr.attendance.manage", "hr.pay.manage")),
            HubLink("Attendance", "Mark today's attendance", "calendar",
                    "hr:attendance-register", ("hr.attendance.manage",)),
            HubLink("Monthly attendance", "One month at a glance", "calendar",
                    "hr:attendance-month", ("hr.attendance.manage",)),
            HubLink("Pay setup", "Salary and commission per person", "users",
                    "hr:pay-list", ("hr.pay.manage",)),
            HubLink("Payroll", "Monthly pay runs", "credit-card",
                    "hr:payroll-list", ("hr.pay.manage",)),
            HubLink("My earnings", "Your own pay", "user", "hr:earnings", ("hr.self",)),
        ],
    ),
    (
        "Settings",
        "sliders",
        "neutral",
        [
            HubLink("Hospital details", "Name, logo, contact", "building",
                    "organizations:hospital", ("admin.organization.manage",)),
            HubLink("Preferences", "Defaults for the whole system", "sliders",
                    "organizations:preferences", ("admin.organization.manage",)),
            HubLink("Required fields", "Which boxes show or are required", "sliders",
                    "organizations:field-rules", ("admin.organization.manage",)),
            HubLink("Patient flow settings", "Where fees are paid, who sends patients on",
                    "sliders", "flow:settings", ("admin.organization.manage",)),
            HubLink("Billing settings", "Payment methods and rules", "credit-card",
                    "billing:settings", ("admin.organization.manage",)),
            HubLink("Print layouts", "Letterhead and paper", "file",
                    "organizations:print-layouts", ("admin.organization.manage",)),
            HubLink("Printers", "Which printer prints what", "file",
                    "printing:stations", ("admin.organization.manage",)),
            HubLink("Message wording", "Texts sent to patients", "file",
                    "notifications:messages", ("admin.organization.manage",)),
            HubLink("Branches", "Your hospital's branches", "building",
                    "organizations:branch-list", ("admin.branches.manage",)),
        ],
    ),
    (
        "Access & system",
        "lock",
        "neutral",
        [
            HubLink("Roles", "What each role may do", "users",
                    "accounts:role-list", ("admin.roles.manage",)),
            HubLink("Access matrix", "Who can view, edit and delete each area", "users",
                    "accounts:access-matrix", ("admin.roles.manage",)),
            HubLink("Permission matrix", "Roles and rights side by side", "users",
                    "accounts:permission-matrix", ("admin.roles.manage",)),
            HubLink("Audit log", "Who changed what", "file", "audit:log", ("admin.audit.view",)),
            HubLink("Approvals", "Stock edits waiting for a yes", "check-circle",
                    "approvals:list", ("admin.approvals.manage",)),
            HubLink("Recycle bin", "Restore deleted records", "trash",
                    "recyclebin:list", ("admin.recyclebin.manage",)),
            HubLink("System status", "Errors and health", "sliders",
                    "core:system", ("admin.system.view",)),
            HubLink("Backup and restore", "Google Drive backup, restore from a file", "sliders",
                    "core:backups", ("admin.organization.manage",)),
            HubLink("Application update", "Check for and download a new version", "sliders",
                    "core:updates", ("admin.organization.manage",)),
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
                url = reverse(link.url_name, args=link.args)
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


# Offered at the top of the All menu until someone has pinned or opened something of their own.
COMMON_SCREENS = ("Patients", "New booking", "New invoice", "Staff", "Products", "Daily sales")


def menu_page(request) -> tuple[list[dict], list[dict]]:
    """Groups for the All menu page, and the common screens this user may open.

    A screen already in the sidebar is marked, so the page lists it only while searching.
    """
    from .navigation import build_nav

    sidebar = {item["url"] for item in build_nav(request)}
    groups = build_hub(request)
    for group in groups:
        for tile in group["tiles"]:
            tile["in_sidebar"] = tile["url"] in sidebar
    tiles = {t["label"]: t for g in groups for t in g["tiles"] if t["url"] and not t["in_sidebar"]}
    common = [
        {"url": tiles[name]["url"], "label": name} for name in COMMON_SCREENS if name in tiles
    ]
    return groups, common


def find_screens(request, query: str, limit: int = 6) -> list[dict]:
    """Screens this user may open whose name or description has every typed word (Ctrl+K)."""
    words = query.lower().split()
    if not words:
        return []
    found = []
    for group in build_hub(request):
        for tile in group["tiles"]:
            text = f"{tile['label']} {tile['description']} {group['title']}".lower()
            if tile["url"] and all(word in text for word in words):
                found.append(tile)
    found.sort(key=lambda t: not t["label"].lower().startswith(words[0]))
    return found[:limit]
