"""Catalogue of SehatDesk permissions and default roles (PRD §17).

Permissions are action-based codes such as "patient.create". This file is the source of truth:
`sync_permissions()` copies it into the Permission table after every migrate, and default roles
are created for each new organization. Add new codes here as modules are built.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDef:
    code: str
    module: str
    label: str


PERMISSIONS: list[PermissionDef] = [
    # Administration
    PermissionDef("admin.organization.manage", "Administration", "Manage hospital settings"),
    PermissionDef("admin.branches.manage", "Administration", "Manage branches"),
    PermissionDef("admin.users.view", "Administration", "View staff directory"),
    PermissionDef("admin.users.manage", "Administration", "Create and edit staff accounts"),
    PermissionDef("admin.roles.manage", "Administration", "Manage roles and permissions"),
    PermissionDef("admin.audit.view", "Administration", "View audit logs"),
    PermissionDef("admin.system.view", "Administration", "View system health, errors and backups"),
    PermissionDef(
        "admin.masterdata.manage", "Administration", "Manage departments, doctors and services"
    ),
    # Patients
    PermissionDef("patient.view", "Patients", "View patients"),
    PermissionDef("patient.create", "Patients", "Register patients"),
    PermissionDef("patient.update", "Patients", "Edit patient details"),
    # Reception
    PermissionDef("appointment.view", "Reception", "View appointments"),
    PermissionDef("appointment.manage", "Reception", "Book and change appointments"),
    PermissionDef("queue.manage", "Reception", "Issue and manage tokens"),
    PermissionDef("priority.special", "Reception", "Give special queue priority"),
    # Clinical
    PermissionDef("consultation.view", "Clinical", "View clinical records"),
    PermissionDef("consultation.create", "Clinical", "Write consultations"),
    PermissionDef("consultation.sign", "Clinical", "Sign consultations"),
    PermissionDef(
        "consultation.reopen", "Clinical", "Reopen completed consultations for amendment"
    ),
    PermissionDef("vitals.record", "Clinical", "Record vitals"),
    PermissionDef("prescription.create", "Clinical", "Write prescriptions"),
    PermissionDef("ai.use", "Clinical", "Use AI assistant drafts"),
    # Laboratory
    PermissionDef("lab.order.create", "Laboratory", "Create lab orders"),
    PermissionDef("lab.sample.collect", "Laboratory", "Collect samples"),
    PermissionDef("lab.result.enter", "Laboratory", "Enter lab results"),
    PermissionDef("lab.result.verify", "Laboratory", "Verify and release lab reports"),
    PermissionDef("lab.catalog.manage", "Laboratory", "Manage the lab test catalogue"),
    # Pharmacy
    PermissionDef("pharmacy.sale", "Pharmacy", "Sell and dispense medicines"),
    PermissionDef("pharmacy.refund", "Pharmacy", "Process pharmacy returns and refunds"),
    PermissionDef("pharmacy.discount", "Pharmacy", "Give discounts on pharmacy sales"),
    PermissionDef("pharmacy.inventory.manage", "Pharmacy", "Manage stock and batches"),
    # Billing
    PermissionDef("billing.view", "Billing", "View invoices"),
    PermissionDef("billing.payment.collect", "Billing", "Record payments"),
    PermissionDef("billing.discount", "Billing", "Give discounts"),
    PermissionDef("billing.refund", "Billing", "Issue refunds"),
    # Inpatient
    PermissionDef("ipd.manage", "Inpatient", "Manage admissions and beds"),
    PermissionDef("ipd.facility.manage", "Inpatient", "Set up wards, rooms and beds"),
    PermissionDef("ipd.discharge.override", "Inpatient", "Discharge a patient with an unpaid bill"),
    PermissionDef("nursing.notes", "Inpatient", "Write nursing notes and medication charting"),
    # Documents
    PermissionDef("document.view", "Documents", "View patient documents"),
    PermissionDef("document.upload", "Documents", "Upload patient documents"),
    PermissionDef("document.archive", "Documents", "Archive patient documents"),
    # Reports
    PermissionDef("reports.view", "Reports", "View operational reports"),
    PermissionDef("reports.financial.view", "Reports", "View financial reports"),
    # Procedures
    PermissionDef("procedure.view", "Procedures", "View patient procedures"),
    PermissionDef("procedure.order", "Procedures", "Add procedures to patients"),
    PermissionDef("procedure.perform", "Procedures", "Mark procedures done"),
    PermissionDef("procedure.price.override", "Procedures", "Charge a different procedure price"),
    # HR & payroll
    PermissionDef("hr.self", "HR & payroll", "Check in/out and view own attendance and pay"),
    PermissionDef("hr.attendance.manage", "HR & payroll", "Mark and correct staff attendance"),
    PermissionDef(
        "hr.pay.manage", "HR & payroll", "Manage pay profiles, commission, payroll and staff pay"
    ),
    # Finance
    PermissionDef("finance.view", "Finance", "View profit & loss, ledgers and finance dashboard"),
    PermissionDef("finance.expense.manage", "Finance", "Record expenses and pay suppliers"),
]

ALL_CODES = frozenset(p.code for p in PERMISSIONS)

SUPER_ADMIN_KEY = "super_admin"


@dataclass(frozen=True)
class RoleDef:
    key: str
    name: str
    description: str
    permissions: frozenset


def _codes(*prefixes: str) -> frozenset:
    """All codes starting with any prefix, so new codes in a module flow into matching roles."""
    return frozenset(c for c in ALL_CODES if c.startswith(prefixes))


# Admin is deliberately not given clinical access (design kit, role permission matrix).
DEFAULT_ROLES: list[RoleDef] = [
    RoleDef(SUPER_ADMIN_KEY, "Super admin", "Full access to every module. Locked.", ALL_CODES),
    RoleDef(
        "hospital_admin",
        "Hospital admin",
        "Operations, staff, settings and reports. No clinical record access.",
        _codes(
            "admin.",
            "reports.",
            "hr.",
            "finance.",
            "appointment.view",
            "billing.view",
            "patient.view",
        )
        | {"patient.create", "ipd.facility.manage", "procedure.view"},
    ),
    RoleDef(
        "reception",
        "Reception",
        "Registration, appointments, tokens and consultation payments.",
        _codes("patient.", "appointment.", "queue.")
        | {"billing.view", "billing.payment.collect", "document.view", "document.upload"}
        | {"procedure.view", "procedure.order", "hr.self"},
    ),
    RoleDef(
        "doctor",
        "Doctor",
        "Consultations, prescriptions, lab orders and AI drafting.",
        _codes("consultation.", "prescription.", "vitals.", "ai.", "document.", "procedure.")
        | {"patient.view", "appointment.view", "lab.order.create", "ipd.manage", "hr.self"},
    ),
    RoleDef(
        "nurse",
        "Nursing",
        "Vitals, admissions and ward care.",
        frozenset(
            {
                "patient.view",
                "consultation.view",
                "vitals.record",
                "ipd.manage",
                "nursing.notes",
                "document.view",
                "document.upload",
                "procedure.view",
                "procedure.perform",
                "hr.self",
            }
        ),
    ),
    RoleDef(
        "lab",
        "Lab technician",
        "Samples, results and verification.",
        _codes("lab.") | {"patient.view", "document.view", "document.upload", "hr.self"},
    ),
    RoleDef(
        "pharmacy",
        "Pharmacist",
        "Prescriptions, dispensing, stock, purchases and returns.",
        # Discounts are a supervisor decision: granted per hospital, not by default.
        (_codes("pharmacy.") - {"pharmacy.discount"})
        | {"patient.view", "billing.payment.collect", "hr.self"},
    ),
    RoleDef(
        "cashier",
        "Cashier",
        "Payments, refunds and day closing.",
        _codes("billing.")
        | {"patient.view", "reports.financial.view", "procedure.view"}
        | {"hr.self", "finance.expense.manage"},
    ),
]
