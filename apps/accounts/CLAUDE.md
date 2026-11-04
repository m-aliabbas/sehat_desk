# apps/accounts — Staff accounts, sign-in and RBAC

Users, roles, permission codes, branch-scoped role assignments, sign-in, first-run setup and the
workspace (branch) picker. PRD §17. PROJECT_NOTES §9.

## Files
- `permissions.py` — the permission catalogue (`PERMISSIONS`) and `DEFAULT_ROLES`. Source of truth;
  `sync_permissions()` copies it to the database after every migrate.
- `services.py` — `create_organization_with_admin`, `seed_default_roles`, `effective_permissions`
  (codes for a user in one branch), `accessible_branches`, `can_grant_role` (no escalation),
  `super_admin_count` / `is_super_admin`.
- `middleware.py` — sets `request.organization`, `request.branch`, `request.sd_perms`.
- `decorators.py` — `permission_required(*codes, any_of=False)` (records `sd_permissions`/`sd_any_of` on the
  view), `workspace_exempt`, `has_perm`, `can_open(request, url)` (passes that view's guard; `{% openable %}`).
- `views_auth.py` (login, setup, passwords, branch pick; a chosen branch is remembered per computer in a signed cookie, so the picker shows only once), `views_roles.py` (permission matrix, access matrix),
  `views_staff.py` (staff directory).

## Rules
- Django's `is_staff` / `is_superuser` are not used for authorization; only role assignments.
- An assignment with `branch=None` means every branch. Permissions are evaluated per branch.
- Super admin role is locked and always holds every code; there must always be one active super admin.
- Nobody can grant a role holding codes they do not hold themselves.
- Access matrix (`matrix.py`, `AREAS`): each area (Products and stock, Lab tests, Wards, Reports…)
  with View / Edit / Delete cells, each an existing permission code. Roles act as departments.
  `accounts:access-matrix` (Settings → Access, a tab per role; `save_role` only touches matrix codes)
  and `accounts:staff-access` (one person; `save_person`). A person gets "+" (`User.extra_permissions`)
  for a ticked code their roles lack and "–" (`User.denied_permissions`) for an unticked code their
  roles give; `effective_permissions` and `all_permissions_anywhere` subtract denied codes in every
  branch. Only codes the actor holds can change; Super admin (role or person) is never changed.
  Audit `role.update` (summary "access matrix") and `staff.access_matrix`. The staff page lists the +/–.
- Delete is per area: `DELETE_AREAS` maps each delete code (`pharmacy.product.delete`,
  `lab.catalog.delete`…) to the edit codes of its area; recycle bin kinds name theirs (`delete_perm`).
  Prefix roles (`_codes`) never include delete codes; Hospital admin gets `DELETE_CODES`. The old single
  `records.delete` was replaced in 1.0.11: `sync_permissions` gave each new code to the roles and people
  who held it and could edit that area, then dropped it.

- Default Lab technician holds `billing.payment.collect`: lab tests are paid at the lab by default (apps/flow).
- Deactivating a role warns how many active staff hold it (they lose its access).

## Doctors
Creating staff with the Doctor role goes straight to Add doctor with that person chosen, for someone holding `admin.masterdata.manage`. Staff detail shows the doctor profile, or a button to add one.

## Adding a permission
Use the `sehatdesk-permission` skill (catalogue entry, default roles, nav, tests).

## Tests
`tests/test_auth.py`, `tests/test_rbac.py`; plus `apps/core/tests/test_url_inventory.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
