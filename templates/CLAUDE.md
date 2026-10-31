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
  Pharmacy quantities: `{% load pharmacy %}` → `{{ qty|in_packs:product }}` ("25 tablets (2 strips + 5)"),
  `as_packs`, `unit_label`, `pack_label`, `split`, `abs_value` (`apps/pharmacy/templatetags/pharmacy.py`).
- Folder per app (`templates/<app>/`). Partials returned to htmx requests start with `_`
  (e.g. `procedures/_encounter_panel.html`).
- Shared pieces: `components/` (alert, drawer_base, modal_base, empty_state, error_state, field,
  pagination, skeleton_table) and `partials/` (sidebar, topbar, breadcrumbs, toasts,
  confirm_dialog, settings_tabs, env_banner).
- Print pages are forced to black and white in `print/base.html`. Layout settings come from
  `{% print_layout %}` + `print/_layout_css.html`.
- Printing: `print/a4.html`, `print/a5.html`, `print/thermal.html` with `_letterhead`/`_footer`;
  staging prints are stamped "not a valid document".

## Sidebar
`partials/sidebar.html` pins `settings` to the bottom and shows `more` (All menu) as a separate
button. That page is `core/menu.html`, built from `apps/core/menu_hub.py`. Colours per module are
`NAV_COLORS` in `apps/core/navigation.py`.

## Styling
- Use the component classes in `static/src/app.css`: `btn btn-primary|secondary|danger|ghost`,
  `btn-sm`, `card`, `card-header`, `input`, `select`, `chip chip-success|info|warning|danger|neutral`,
  `table-wrap`, `table`, `page-title`, `page-subtitle`.
- Colours and radii are tokens from
  `ui_design_reference/SehatDesk-Expanded-Workflow-Kit/03-handoff/DESIGN-TOKENS.json`, mirrored in
  `@theme` in `static/src/app.css` (`text-ink`, `text-muted`, `bg-page`, `bg-teal`, `border-line`...).
  Do not hard-code hex colours in templates.
- Colour meaning: green = go/save/active (`btn-primary`, `chip-success`), yellow = wait/pending
  (`btn-warning`, `chip-warning`), red = stop/close/delete (`btn-close` for Close and Cancel,
  `btn-danger` for destructive). `tint-<colour>` and `band-<colour>` colour-code a module.
- Status is never shown by colour alone; pair it with a word or icon.
- After adding classes not used before, rebuild and commit the CSS:
  `python manage.py tailwind build` (CI fails if `static/css/app.css` is stale).

## Behaviour
- Number boxes have no spinner arrows (`app.css`); table inputs are compact. Entry screens: one line per
  record, Enter moves on, F8 saves, no second row of settings.
- Reception's booking form is one partial, `appointments/_visit_form.html`, included on the desk. It
  runs by keyboard: Enter picks and moves on, F2 patient, F3 doctor, F4 time, F8 save and print. Keep
  new entry screens usable the same way: focus moves to the next field, no popups or extra pages.
- `static/js/app.js` turns every `select.select` with 6+ options (or `data-searchable`) into a
  type-to-search box; the real select stays, hidden, in the form. `data-native` opts out.
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
