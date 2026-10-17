# templates/ — frontend conventions

Django templates + HTMX + Alpine.js + Tailwind v4. Everything is served from this machine: the app
must work offline on the hospital LAN (no CDNs, fonts or icons from other hosts; CSP enforces it,
`apps/core/security.py`). PROJECT_NOTES §8, §17.

## Page structure
- Screens extend `layouts/app.html` and fill `title`, `page_title`, `page_subtitle`,
  `page_actions`, `content` (plus `breadcrumbs_wrap` when needed). Sign-in pages use
  `layouts/auth.html`; error pages `layouts/error.html`.
- Load `{% load ui %}` for `{% icon "name" "h-4 w-4" %}`, `{% status_chip status label %}` and the
  `initials`, `get_item`, `sum_values` filters (`apps/core/templatetags/ui.py`).
- Folder per app (`templates/<app>/`). Partials returned to htmx requests start with `_`
  (e.g. `procedures/_encounter_panel.html`).
- Shared pieces: `components/` (alert, drawer_base, modal_base, empty_state, error_state, field,
  pagination, skeleton_table) and `partials/` (sidebar, topbar, breadcrumbs, toasts,
  confirm_dialog, settings_tabs, env_banner).
- Printing: `print/a4.html`, `print/a5.html`, `print/thermal.html` with `_letterhead`/`_footer`;
  staging prints are stamped "not a valid document".

## Styling
- Use the component classes in `static/src/app.css`: `btn btn-primary|secondary|danger|ghost`,
  `btn-sm`, `card`, `card-header`, `input`, `select`, `chip chip-success|info|warning|danger|neutral`,
  `table-wrap`, `table`, `page-title`, `page-subtitle`.
- Colours and radii are tokens from
  `ui_design_reference/SehatDesk-Expanded-Workflow-Kit/03-handoff/DESIGN-TOKENS.json`, mirrored in
  `@theme` in `static/src/app.css` (`text-ink`, `text-muted`, `bg-page`, `bg-teal`, `border-line`...).
  Do not hard-code hex colours in templates.
- Status is never shown by colour alone; pair it with a word or icon.
- After adding classes not used before, rebuild and commit the CSS:
  `python manage.py tailwind build` (CI fails if `static/css/app.css` is stale).

## Behaviour
- HTMX for partial updates (`hx-get`/`hx-post` return a partial; errors come back as a toast).
  Every htmx endpoint is a normal view with the same permission checks.
- Alpine for small local state (`x-data`). No other JS frameworks; shared JS in `static/js/app.js`;
  vendored `static/vendor/htmx.min.js` and `alpine.min.js`.
- Forms post with `{% csrf_token %}`. Hiding a button is a convenience; the view still checks
  the permission.
- Screen designs: `ui_design_reference/.../03-handoff/PAGE-CHECKLIST.csv` (134 screens).

## Keep this file current
Update it when you add a layout, component, template tag, CSS component class or token, or change
how htmx/Alpine/print templates are structured.
