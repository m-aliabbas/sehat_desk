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
  confirm_dialog, settings_tabs, env_banner, license_contact).
- Print pages are forced to black and white in `print/base.html`. Layout settings come from
  `{% print_layout %}` + `print/_layout_css.html`. Paper may be 80 mm thermal per document. `print/_credit.html` is the "SehatDesk by Vibe.Exe" line: `print/base.html` puts it at the end of every document, and sticker labels turn it off with an empty `credit` block.
- `print/thermal.html` (tokens, receipts): all text bold in Arial for thermal heads, 72 mm wide, no `@page` size or margin (the driver's roll, no browser header/footer); The fee slip prints each copy as its own job (`printSlips` in `papers.html`), so the printer cuts between them. Visit papers: `appointments/papers.html` (roll), `papers_rx.html` (A5, `?part=rx`).
- Saving a visit prints slips by itself; the papers page has "Print Rx" (F9, always the print window via `sd-print-dialog`) and no token-only button.
- One typeface on all paper (`print/base.html`), one roll header (`print/_thermal_header.html`), one prescription header (`print/_rx_header.html`), `print-token` for the token number, `print/_emergency_mark.html` on emergency papers. The blank Rx height is calculated to stay on one page.
- The pharmacy sale receipt (`pharmacy/pos/receipt.html`) is a five-column slip: SN, product, rate, qty, amount, then totals, amount in words (`in_words` in `ui.py`) and how it was paid. No barcode or QR.
- Print routing: `print/_router.html` + `static/js/print_router.js` replace `window.print` (loaded by `print/base.html`, reports and the product ledger; a new page that prints outside `print/base.html` must include it). Paper comes from `@page` or `<meta name="sd-paper">`. `apps/printing`.
- Printing: `print/a4.html`, `print/a5.html`, `print/thermal.html` with `_letterhead`/`_footer`;
  staging prints are stamped "not a valid document".

## Sidebar
`partials/sidebar.html` pins `settings` to the bottom and shows `more` (All menu) as a separate
button, which also names the current screen when that screen is not in the admin sidebar. That page is `core/menu.html`, built from
`apps/core/menu_hub.py`: one screen of names under dark group bars, a search box that also searches descriptions (Enter opens the first match,
arrows walk the list), and a top row of pinned, then recent, then common screens (Enter or 1 to 6 with the box empty; star or P pins; per person on this computer).
A screen already in the sidebar shows there only while searching (`menu_page`), and Ctrl+K still finds everything. Keep a group to about seven tiles, and name groups by what people look for.
Settings pages share `partials/settings_tabs.html`: five groups, then the pages in the current group (`settings-sub`). Colours per module are
`NAV_COLORS` in `apps/core/navigation.py`.
Below 768px it is a drawer. From 768px it is full or an icon rail (`html[data-nav="rail"]`, CSS at the end
of `app.css`): the rail by default below 1280px; the topbar button switches it, remembered in `localStorage`
(`sd-nav`). Words in the sidebar carry `sidebar-text` so the rail hides them; links keep `title` and `aria-label`.

Ctrl+K search also offers screens: `find_screens` in `apps/core/menu_hub.py` matches the All menu's tiles the user may
open. A new screen people need goes in `HUB_GROUPS`, so it shows in both.

## Screen density
Pages are compact by default, from shared CSS: `stat-card` is one short strip (label and number on a line), so do not give it big padding or a big number.
Tables show one status chip colour only for exceptions: a chip most rows share becomes plain words (`chip-quiet`, `quietChips` in `app.js`).
Row buttons and the word Open appear on the row you point at. Clicking a read-only row opens its main link. Keep a screen's first rows in the first view.
A summary panel inside a page shows at most 20 rows in a short scrolling box, with a "See all N" link to the full list (`pharmacy/dashboard.html`).
Every page has `#page-trail` (`sdTrail` in `app.js`): Back to the page you came from, with its filters and scroll, then Home and breadcrumbs. Pages with no `breadcrumbs` context get crumbs from the active sidebar item and the page title. A saved form page leaves the trail (`FORM_PAGE`).

## Styling
- Use the component classes in `static/src/app.css`: `btn btn-primary|secondary|danger|ghost`,
  `btn-sm`, `card`, `card-header`, `input`, `select`, `chip chip-success|info|warning|danger|neutral`,
  `table-wrap`, `table`, `page-title`, `page-subtitle`.
- Entry grids use `sheet-wrap` + `sheet` (Excel-style: cells touch, boxes fill the cell), `sheet-freeze` on the
  first column, `cell-text`, `cell-unit`, `sheet-remove`. Save buttons go in `action-bar` (sticks to the window
  bottom, last child of a `.card`). Used on Add products and the purchase/receive `_item_grid.html`.
- Colours and radii are tokens from
  `ui_design_reference/SehatDesk-Expanded-Workflow-Kit/03-handoff/DESIGN-TOKENS.json`, mirrored in
  `@theme` in `static/src/app.css` (`text-ink`, `text-muted`, `bg-page`, `bg-teal`, `border-line`...).
  Do not hard-code hex colours in templates.
- High contrast: on screen nothing is smaller than `text-label` (13px). Field labels and table headers are bold white on `bg-head` (white on paper).
  `text-muted` is 8.5:1 for secondary text, `text-subtle` is for placeholders only, `border-edge` for box borders.
  `print/base.html` pins the old label size and colours, so paper layouts do not move.
- Shape: only two radii, `rounded-card` (16px, cards and dialogs) and `rounded-control` (8px, buttons, boxes, table frames);
  `rounded-full` is for chips and avatars. A label and its box are one joined shape; table and form-grid headers follow the
  frame's corners. Buttons and boxes share one height (h-9, `btn-sm` h-8). No square box next to a rounded one.
- Colour meaning: green = go/save/active (`btn-primary`, `chip-success`), yellow = wait/pending
  (`btn-warning`, `chip-warning`), red = stop/close/delete (`btn-close` for Close and Cancel,
  `btn-danger` for destructive). `tint-<colour>` and `band-<colour>` colour-code a module.
- Status is never shown by colour alone; pair it with a word or icon.
- After adding classes not used before, rebuild and commit the CSS:
  `python manage.py tailwind build` (CI fails if `static/css/app.css` is stale).

## Behaviour
- Ergonomics come first. Every screen: the cursor starts in the first box, the whole job works from the keyboard, Enter moves on, F8 saves,
  the fewest keystrokes win, and nothing a person needs (a search list, a save button, a column they must fill) is ever hidden or clipped.
- Number boxes have no spinner arrows (`app.css`); table inputs are compact. Entry screens: one line per
  record, Enter moves on, F8 saves, no second row of settings.
- Reception's booking form is one partial, `appointments/_visit_form.html`, included on the desk. It
  runs by keyboard: Enter picks and moves on, F2 patient, F3 doctor, Alt+T time, F8 save and print. Keep
  new entry screens usable the same way: focus moves to the next field, no popups or extra pages.
- `static/js/app.js` turns every `select.select` into the one dropdown: a type-to-search box with a floating list; the
  real select stays, hidden, in the form. Nothing typed starts on the current value, so Enter keeps it and moves on.
- Every `table.table` and `table.sheet` in `#main` gets the shared layer in `app.js` (`sdTables`), with no template
  change: an expand column, Ctrl+E popup (boxes move in and back; a row with a link shows that page's content;
  otherwise the row's values), arrow keys, Ctrl+Delete, and on read-only tables columns that do not fit move into the popup (entry tables scroll instead).
  `class="cell-detail"` on a th and td keeps a column for the popup only. `data-plain` opts a table out.
  A remove button is found by `aria-label` or text starting Remove/Delete. Shortcuts are listed in `SHORTCUTS` (press ?).
  Keep required fields out of `cell-detail`.
- Single-record entry forms: every `.field` in a form is a borderless cell with a line under it (`app.css`, no template
  change), and a `grid` of them touches like a sheet. Enter moves to the next box, Up/Down to the box before or after, F8 saves.
  `data-enter-submits` on a form (sign-in) makes Enter on the last box send it.
- Every screen and popup puts the cursor in its first box, or the first with an error (`sdFocusFirst` in `app.js`), without
  scrolling or opening a dropdown. A screen that focuses something itself keeps that.
  With no box on the first view, `#main` takes the cursor, so Tab starts at the content.
- F8 and Ctrl+S save the form the cursor is in, including popups and `hx-post` forms; with the cursor elsewhere, the only form with boxes on
  the screen or popup. A form with `hx-trigger="change"` sends on leaving the box. A form of buttons alone is never saved this way.
- Alt+N follows the first link starting New, Add, Register, Create, Book, Admit, Record, Receive, Start or Issue. A button that posts a form goes to its first box instead, so the key never creates a record.
- A save button below the window in a form that starts on screen gets `save-bar` (`sdStickySave`), no template change. Tab stays inside any open
  `role=dialog` or `role=alertdialog`.
- A table of 12+ rows with no search box or pagination on the page gets a filter box (`addFilter`, Alt+F). A table of 50+ tick boxes is one Tab stop; arrows walk it.
- Side panel (`openSide` in `app.js`): a link ending `/edit/`, `/pay/` or `/results/` inside `#main`, a row pencil, or any link with `data-panel` opens its page beside the list, with no view change.
  The view stays a normal page: errors re-render in the panel; a redirect closes it, reloads the list and keeps the toast; a print page or sign-in opens normally. A page with an inline script (JSON data scripts are fine) opens as a full page.
  `data-panel="off"` opts out. Esc asks first if something was typed. A screen with its own action bar marks it `data-page-actions`, so no second sticky save bar is added.
- Consultation (`encounters/detail.html`): patient strip stays in view, note on the left, orders in tabs on the right, sign bar always visible. It uses the icon rail (`nav_mode` block in `layouts/app.html`). `lab/_test_picker.html` with `compact=True` lists a test only while typing or once ticked.
- Hold Alt to see menu numbers for Alt + 1 to 9.
- Any `title` shows as an instant hint on hover or keyboard focus (`sd-tip`, end of `app.js`). Add a `title` to any column or label
  that needs explaining. `data-ref-low` / `data-ref-high` on a number box add a Low or High word to its label as you type.
- Row buttons: `data-edit="url"` and `data-remove="url"` on a `<tr>` add small pencil and bin buttons (keys E and Delete). Write them
  only inside the permission check. Add `data-remove-label`; `data-remove-reason` asks for a reason and posts it as `reason`. Removal
  always goes to a deactivate or cancel view; clinical and money records are never erased.
- Entry grids (`entry-row` or `table.sheet`) fit the window (`sdFitGrids`, `grid-fits`): rows scroll inside the table, header and entry row stay, and the page does not grow. Their search list floats.
- Drafts (`sdDrafts`, `apps/core/drafts.py`): any posting form with two or more boxes saves what is typed for its owner and offers Restore on return; My drafts is in the user menu. `data-draft="off"` opts out, htmx forms and passwords are skipped, and `rows`/`lines` in Alpine are kept.
- Search dropdowns use `pick-list` and `pick-row` (`pick-title`, `pick-sub`, `pick-side`, `pick-side-sub`): 52 to 60px, two lines, mint with a teal edge on the keyboard's row. A new dropdown uses these.
- Any table people scan or add into has a fixed first row, `<tr class="entry-row">`, and lines land below it (POS, stock grid, Add
  products). Forms that add a line, such as the invoice's, sit above the table. A new entry screen follows this.
- Scan boxes: `<input data-scan>` (`sdScan` in `app.js`) keeps a scanner's group separator (Ctrl+]) and line breaks inside
  a code, so one code stays one value; the server reads it with `pharmacy/scan.py`. `data-scan="fill"` (with `data-scan-url`)
  swaps a QR code for its product code; `data-scan="detect"` on a name box hands a scanned code to the `barcode` box beside it.
  Any new box that takes a product code uses this. Hardware scanners only; no camera.
- `<input data-list="datalist-id">` is a free-text box with a dropdown that floats above scrolling tables
  (`sdCombo` in `app.js`). It opens on click, typing or Alt+Down. 
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
