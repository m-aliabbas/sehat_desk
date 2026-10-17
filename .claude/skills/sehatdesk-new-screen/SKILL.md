---
name: sehatdesk-new-screen
description: Add a SehatDesk page, form or htmx endpoint correctly - view with scoping and permission checks, URL, template on the design system, sidebar entry, module switch, and tests. Use whenever adding or changing a view or URL.
---

# Add a screen or endpoint

1. **Service first.** Any write calls a function in the app's `services.py` (or its named module).
   The view never sets status, money, stock or clinical fields itself.
2. **View** in `apps/<app>/views.py`:
   - Decorate with `@permission_required("code", ...)` from `apps.accounts.decorators`
     (`any_of=True` when one of several codes is enough). Add `require_GET`/`require_POST`.
   - Load records only with `scoped(Model, request)` / `get_scoped_or_404(Model, request, pk=pk)`
     (`apps/core/scoping.py`); add `all_branches=True` only for org-wide screens that checked a
     suitable permission.
   - Catch the service's `ValidationError` subclass and show its message (form error or toast).
   - Write an audit event for sensitive actions: `audit.record(request, "area.action", obj, ...)`
     with no clinical text.
   - Lists: `paginate(request, qs)` and `select_related`/`prefetch_related` (N+1 guard test).
3. **URL** in `apps/<app>/urls.py` under the app namespace. The URL inventory test
   (`apps/core/tests/test_url_inventory.py`) checks it automatically: it must redirect anonymous
   users and refuse users without permission. Only add it to `PUBLIC`/personal lists there with a
   written reason.
4. **Template** in `templates/<app>/` extending `templates/layouts/app.html`; htmx partials start with `_`.
   Follow `templates/CLAUDE.md` (component classes, tokens, `{% icon %}`, no external assets).
   Rebuild CSS if new classes were used.
5. **Navigation:** for a new top-level screen add a `NavItem` in `apps/core/navigation.py` with its
   permission codes. If it belongs to lab, pharmacy or IPD/nursing, add the namespace/view/nav key
   to `FEATURES` in `apps/core/features.py` so the module switch hides it.
6. **Tests** in `apps/<app>/tests/`: allowed role gets 200, role without the code gets 403, another
   organization's record gets 404, the write path changes data through the service. If the screen
   should be in a role's menu, check `apps/core/tests/test_journeys.py`.
7. Run skill `sehatdesk-checks`, then skill `sehatdesk-docs-sync` (update `apps/<app>/CLAUDE.md`
   if entry points changed).
