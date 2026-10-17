# apps/ — conventions every module shares

Each app has its own `CLAUDE.md` with its flow and rules. Open the one for the app you touch.

## Layering
- `models.py` holds data and simple derived properties. `services.py` (or named modules such as
  `pharmacy/stock.py`, `hr/payroll.py`) holds every state change. Views and other apps call the
  services; they never set status, money or stock fields directly.
- Services take `request` (or `user`) and read `request.organization`, `request.branch`,
  `request.user` and `request.sd_perms` (set by `apps/accounts/middleware.py`). They re-check the
  permission they need, so a service is safe even if a view forgets.
- Cross-app reactions use signals (`lab/signals.py`, `hr/signals.py`, `notifications/signals.py`)
  so the source app does not import the listener.

## Base models (`apps/core/models.py`, `apps/core/scoping.py`)
- `TimeStampedModel` (created_at/updated_at) on every table.
- `OrgScopedModel` / `BranchScopedModel` for business data. Patients, staff, catalogues are
  organization-wide; visits, stock, invoices, beds are per branch.
- `ArchivableModel` for soft delete. Clinical and financial records are never hard-deleted.
- Numbers (MRN, invoice, token...) come from `core.models.next_number(org, key)`, never `max()+1`.

## Non-negotiable rules
- **Scoping:** views query through `scoped(Model, request)` / `get_scoped_or_404(...)`. Another
  organization's record is a 404, never a 403.
- **Permissions:** every view has `@permission_required("code", ...)` (`apps/accounts/decorators.py`)
  or is listed as public/personal in `apps/core/tests/test_url_inventory.py`. Codes live in
  `apps/accounts/permissions.py` (skill: `sehatdesk-permission`).
- **Money and stock:** `@transaction.atomic` (or `core.db.critical_write`) and re-read the row with
  `core.db.locked(obj)` before changing it. Use `after_commit` for side effects.
- **Clinical records:** corrected by a new version or an "entered in error" mark with a reason,
  never overwritten (encounters, lab results, vitals, allergies, nursing, IPD rounds).
- **Audit:** `apps.audit.services.record(request, "area.action", obj, changes=..., summary=...)`.
  Field names only for clinical text, never the text itself. No patient data in logs.
- **Module switches:** lab, pharmacy and IPD/nursing can be turned off (`apps/core/features.py`).
  A new URL namespace or nav entry for those modules must be added there.
- **Portability:** ORM only, no raw SQLite SQL. `select_for_update(of=("self",))` is fine on both.

## Tests
- `pytest` from the repo root (skill: `sehatdesk-checks`). Each app has `tests/`; fixtures in the
  root `conftest.py`: `org`, `branch`, `branch2`, `make_user(role_key, branch=...)`, `admin_user`,
  `admin_client`, `department`, `doctor`, `patient`, `make_request(user)`, `cash`.
- Project-wide guards in `apps/core/tests/`: `test_url_inventory.py` (every URL needs login and a
  permission), `test_isolation.py` (no cross-organization access), `test_journeys.py` (each role's
  menu renders), `test_query_scaling.py` (no N+1 on list pages), `test_seed_demo.py` (demo data).

## App index
accounts · appointments · audit · billing · core · documents · encounters · finance · hr · ipd ·
lab · masterdata · notifications · nursing · organizations · patients · pharmacy · prescriptions ·
procedures · queue · reports · search · vitals — each has `apps/<app>/CLAUDE.md`.

When you add an app: create its `CLAUDE.md` (same shape as the others), add it to this index, and
add it to `INSTALLED_APPS`, `config/urls.py` and `apps/core/navigation.py` as needed.
