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
- `decorators.py` — `permission_required(*codes, any_of=False)`, `workspace_exempt`, `has_perm`.
- `views_auth.py` (login, setup, passwords, branch pick), `views_roles.py` (permission matrix),
  `views_staff.py` (staff directory).

## Rules
- Django's `is_staff` / `is_superuser` are not used for authorization; only role assignments.
- An assignment with `branch=None` means every branch. Permissions are evaluated per branch.
- Super admin role is locked and always holds every code; there must always be one active super admin.
- Nobody can grant a role holding codes they do not hold themselves.

## Adding a permission
Use the `sehatdesk-permission` skill (catalogue entry, default roles, nav, tests).

## Tests
`tests/test_auth.py`, `tests/test_rbac.py`; plus `apps/core/tests/test_url_inventory.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
