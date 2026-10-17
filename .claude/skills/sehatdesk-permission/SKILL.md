---
name: sehatdesk-permission
description: Add or change a SehatDesk permission code or default role - catalogue entry, default role grants, view/service checks, navigation, and tests. Use when a feature needs a new access right or a role's default access changes.
---

# Add or change a permission

1. **Catalogue:** add `PermissionDef("module.action", "<Group>", "<Label shown in the matrix>")` to
   `PERMISSIONS` in `apps/accounts/permissions.py`, next to its module's codes. Codes are
   action-based (`lab.result.verify`), lower case, dotted.
2. **Default roles** (`DEFAULT_ROLES` in the same file): grant it to the roles that should have it
   on a new hospital. `_codes("prefix.")` grants a whole module; list single codes explicitly when
   only some roles should get them (e.g. discounts are not in the default pharmacist role).
   Hospital admin gets no clinical access. Super admin always gets every code.
3. **Existing hospitals:** nothing to write. `sync_permissions()` runs after every migrate, creates
   the new row and grants it to existing system roles whose default includes it. It never re-grants
   old codes, so admins' edits survive. Renaming a code is a removal plus an addition: existing
   grants of the old code are lost, so avoid renames or write a data migration.
4. **Enforce it on the server:** `@permission_required(...)` on the view and a check in the service
   (`if "code" not in request.sd_perms: raise <AppError>(...)`). Hiding buttons is never enough.
5. **Menu:** add the code to the relevant `NavItem.permissions` in `apps/core/navigation.py`.
   Global search providers declare their codes in `apps/search/services.py`.
6. **Tests:** a role with the code is allowed, one without is refused (403 / service error);
   `apps/accounts/tests/test_rbac.py` and `apps/core/tests/test_journeys.py` still pass.
7. **Docs:** if a module's access rules change, update that app's `CLAUDE.md` "Permissions" list.
   Update `apps/accounts/CLAUDE.md` only if RBAC rules themselves change.
