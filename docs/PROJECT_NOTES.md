# SehatDesk — Project Notes (persistent)

This file records decisions that must survive between sessions. Update it whenever a decision changes.

## 1. Working environment
- Always use the conda env: `conda activate agent_dev311`
- Python: 3.11 (env name agent_dev311). Install runtime deps with `pip install -r requirements.txt`.

## 2. Stack decisions
| Area | Decision | Notes |
|---|---|---|
| Language | Python only (single language) | Django renders the UI; HTML/CSS/small JS only in the browser |
| Backend and frontend | Django + templates + HTMX + Alpine.js + Tailwind | Next.js not used |
| Database | SQLite for now | Switch to PostgreSQL later. Keep ORM code portable. `select_for_update` is a no-op on SQLite, so verify stock/billing locking when moving to PostgreSQL |
| Production server | Waitress | Pure Python, runs on Windows and macOS |
| Static files | WhiteNoise | Serves the UI from the packaged app with no separate web server |
| Optional later | Tauri | Not needed for the first release. Revisit only if a native shell is required |

## 3. Installer and shortcut (customer delivery)
Goal: a customer installs one package, gets a desktop shortcut, double-clicks it, and the app opens in the browser. No terminal, no Python install on the customer machine.

### Targets
- **Windows:** `SehatDesk-Setup-<version>.exe` built with Inno Setup from a PyInstaller bundle.
  - Installs to `%ProgramFiles%\SehatDesk`
  - Creates a desktop shortcut and a Start menu entry
  - Optional: register as a Windows service or a startup task, so the server runs without the shortcut
- **macOS:** `SehatDesk-<version>.pkg` built with pkgbuild from a PyInstaller `.app` bundle.
  - Installs to `/Applications/SehatDesk.app`
  - Post-install script creates a Desktop alias for the console user
  - Optional: a launchd agent to keep the server running

### Build rules
- Each installer must be built on its own OS. PyInstaller does not cross-compile.
- Plan: GitHub Actions matrix with `windows-latest` and `macos-latest` runners. A single build script takes a target option (`--target windows|macos|all`) and calls the right packaging step.
- Build inputs: `requirements.txt` + `requirements-build.txt`. Use the lock file once it exists.
- Output: versioned installers under `dist/`, plus SHA-256 checksums for each file.

### Launcher behaviour (what the shortcut does)
1. Choose a data folder. Windows: `%LOCALAPPDATA%\SehatDesk`. macOS: `~/Library/Application Support/SehatDesk`. Holds the SQLite DB, media, private patient documents (`private/documents`), logs, and the generated secret key. Back up the whole folder, not only the database.
2. Start the server on `0.0.0.0:80` (fallback `8000` if port 80 is busy or the user lacks rights).
3. Run migrations on start. Run the first-time setup page if no admin user exists.
4. Advertise `sehatdesk.local` on the LAN (see section 4).
5. Open the default browser at `http://sehatdesk.local` (fallback `http://localhost`).
6. If the server is already running, just open the browser. Do not start a second copy.

### Known risks to handle
- WeasyPrint needs GTK/Pango libraries on Windows. Bundle them in the installer, or test PDF output on a clean Windows machine before release.
- Windows SmartScreen and macOS Gatekeeper will warn on unsigned installers. Plan code signing (Windows code-signing certificate, Apple Developer ID plus notarization) before wider rollout.
- Port 80 needs admin rights on some systems. The installer should request elevation once.
- Firewall: the installer must add inbound rules for the chosen port (Windows Defender Firewall).

## 4. Network: the app runs at sehatdesk.local
- Staff on the hospital LAN open **http://sehatdesk.local** in any browser.
- The launcher advertises the hostname over mDNS (the `zeroconf` package), so the server works on Windows without Bonjour installed.
- `.local` resolution depends on the client:
  - macOS, iOS, Linux with Avahi: works natively.
  - Windows 10/11: usually works. If not, install Bonjour or use the fallback below.
  - Android Chrome: `.local` resolution is unreliable. Use the fallback below.
- **Fallback:** give the server a static LAN IP (for example 192.168.1.50) and add a router DNS entry or a hosts-file entry `192.168.1.50  sehatdesk.local`.
- Django settings must include:
  - `ALLOWED_HOSTS`: `sehatdesk.local`, `localhost`, `127.0.0.1`, and the server's LAN IP
  - `CSRF_TRUSTED_ORIGINS`: `http://sehatdesk.local` (and the LAN IP origin)
  - Bind to `0.0.0.0`, never only `127.0.0.1`, or other computers cannot reach it
- Plain HTTP on the LAN is acceptable for the first pilot, with secure cookies off. Before production, add HTTPS (self-signed or internal CA certificate) and turn on secure cookies, HSTS, and the other settings in the PRD's security section.
- Staff machines and the server must be on the same subnet. Clinics with guest Wi-Fi isolation will need the admin to allow LAN device traffic.

### Background checks (notifications)
- Time-based notifications (upcoming appointments, low stock, expiry, overdue ward tasks) run inside the server while staff use the app, at most every `SEHATDESK_NOTIFICATION_SWEEP_SECONDS` (default 300) per branch. No separate job runner is needed.
- Optional: the launcher or OS scheduler can also run `python manage.py notifications_sweep` every few minutes, so alerts are raised even when nobody has the app open.

## 5. Open decisions
- [ ] Customer OS priority: Windows and macOS both (decided). Confirm which one ships first.
- [ ] Code signing: certificates to buy and who owns them.
- [ ] Port policy: 80 with fallback to 8000 (current plan) vs. always 8000.
- [ ] Run as a background service vs. only when the shortcut is opened.
- [ ] Update mechanism: manual reinstall vs. an in-app update check.
- [x] Backup: daily automatic backups in the data folder, encrypted when SEHATDESK_BACKUP_KEY is set (§21). Still open: who copies them off-site at each hospital (USB vs cloud).
- [ ] SMS / WhatsApp provider for patient messages (adapter interface exists; no provider chosen).

## 6. Decision log
- Django-only (no Next.js) — single language, faster delivery.
- SQLite for now, PostgreSQL later.
- Local server per clinic (on-premise) rather than cloud-only, because internet can be unreliable in Pakistan.
- Installer for both Windows and macOS, built on their own runners.
- App address: http://sehatdesk.local on the LAN.

## 7. Repository layout and conventions (Phase 0, updated Phase 1)
Layout:
```
manage.py            Django CLI
serve.py             Production entry point (Waitress), used by the launcher
config/              settings.py (env-driven), urls.py, wsgi.py
apps/core/           Shared conventions, health endpoints, abstract models
apps/...             One Django app per domain, added phase by phase
templates/           base.html, layouts/ (app shell, error), partials/ (sidebar, topbar...), components/
static/src/app.css   Tailwind source with design tokens; built to static/css/app.css (committed)
static/js/app.js     Alpine stores/components and htmx wiring (toasts, confirm, overlays, tabs)
static/vendor/       Pinned htmx 2.0.4 and Alpine.js 3.14.8 (offline LAN, no CDN)
docs/                Persistent decisions (this file)
ui_design_reference/ Design kit (read-only source for tokens and screens)
```

Configuration:
- All environment values come from env vars or `.env` (template: `.env.example`).
- `SEHATDESK_ENV` is `development`, `staging` or `production` (see §15). Staging and production refuse
  `DJANGO_DEBUG=true` and use `DJANGO_SECRET_KEY` or the generated `DATA_DIR/secret.key` (§19).
- `SEHATDESK_ENV_FILE` points at an env file other than `./.env` (e.g. `deploy/staging.env`).
- `SEHATDESK_DATA_DIR` sets where the SQLite DB and logs live. The launcher sets it per OS (§3).

Health endpoints (no auth, no patient data):
- `GET /health/live/` process is up, no DB access.
- `GET /health/ready/` process plus a DB round trip. Returns 503 when the DB is down.

Model conventions:
- `TimeStampedModel`: `created_at`, `updated_at` on every table.
- `ArchivableModel`: soft delete via `archived_at`. Use `.active()` in list views.
- Financial and stock writes must be transactional (`transaction.atomic`). Clinical records are versioned, not overwritten.

Organization and branch scoping (standard, applied from Phase 2 when the Organization model exists):
- Every business table carries `organization` and, where relevant, `branch` foreign keys.
- Every queryset comes through a scoped manager or helper that filters by the request user's organization and branch. Views never query the unscoped manager directly.
- Scope and permission checks run in the backend. Templates and HTMX hide UI only, they never authorize.

Tooling:
- Ruff for lint and format: `ruff check .` and `ruff format .` (config in `pyproject.toml`).
- Tests: `pytest` (pytest-django, settings from `pyproject.toml`).
- Pre-commit: `pip install -r requirements-dev.txt && pre-commit install` (needs a git repo; run from the
  activated conda env, since hooks use its ruff/python). Runs ruff check/format, `manage.py check`,
  `makemigrations --check`, and blocks committing `.env`, `deploy/*.env`, `*.sqlite3` and `data/`.


## 8. Frontend and design system (Phase 1)
- **No CDN, no internet at runtime.** htmx and Alpine are vendored in `static/vendor/`. Icons are inline
  SVG (Lucide, `apps/core/icons.py`, `{% icon "name" %}`). Fonts use the Inter / DejaVu Sans / system stack;
  Inter is not bundled yet (add woff2 files to `static/` if exact Inter rendering is required).
- **Tailwind v4 standalone CLI** via `pytailwindcss` (dev dependency only, no Node.js). Version pinned in
  `apps/core/management/commands/tailwind.py`.
  - `python manage.py tailwind build` after changing templates or `static/src/app.css`
  - `python manage.py tailwind watch` while developing
  - `static/css/app.css` is committed, so installers never run Tailwind.
- Design tokens live in `@theme` in `static/src/app.css` (from DESIGN-TOKENS.json): colors `ink, muted, line,
  page, teal, teal-deep, mint, blue, amber, red` (+ `*-soft` surfaces), radius `card`/`control`,
  type sizes `page-title / section-title / body / label`, shell sizes `sidebar` (246px) / `topbar` (76px).
- Component classes: `btn` + `btn-primary|secondary|ghost|danger` (+ `btn-sm`, `btn-icon`); `input`, `select`,
  `field`, `field-label`, `field-error`; `card`; `chip` + `chip-success|info|warning|danger|neutral`
  (use `{% status_chip status %}`); `table`; `tabs`/`tab`; `skeleton`. Live reference: `/design-system/` (staff).
- Templates: pages extend `layouts/app.html`. Modals/drawers are server fragments that extend
  `components/modal_base.html` / `drawer_base.html`, loaded with `hx-target="#overlay-root"`.
  Lists use `components/pagination.html`, `empty_state.html`, `error_state.html`, `skeleton_table.html`.
- `hx-confirm` automatically uses the branded confirm dialog (`data-confirm-title`, `data-confirm-action`,
  `data-confirm-danger`). Servers trigger toasts with `HX-Trigger: {"toast": {"level": ..., "message": ...}}`;
  Django `messages` also render as toasts.
- **Auth default:** login is required everywhere. Public views (health checks, login, setup, password
  reset) must use `@login_not_required`. See §9 for sign-in, roles and scoping.
- Navigation is defined in `apps/core/navigation.py`. Unbuilt modules link to a "Coming in a later phase"
  placeholder. Hiding a link is never authorization: views check permissions themselves.
- `DATA_DIR` is created on startup if missing (fresh customer machines).
- Local dev login: on an empty database, open the app and complete `/setup/`. Dev credentials, if
  generated, live in `data/DEV_LOGIN.txt` (gitignored).

## 9. Authentication, organizations and RBAC (Phase 2)
- **Custom user model** `accounts.User` (`AUTH_USER_MODEL`). One user belongs to one `Organization`.
  Django's `is_staff` / `is_superuser` are **not** used for authorization anywhere.
- **First run:** with no users in the database, every page leads to `/setup/`, which creates the
  organization, main branch, default roles and the first **Super admin**. After that `/setup/` is a 404.
  `createsuperuser` is not needed (and would create a user with no organization or role).
- **Permissions** are action codes (`patient.create`, `billing.refund`, ...) defined in
  `apps/accounts/permissions.py`, synced to the `Permission` table after every `migrate`.
  Add new codes there when building a module, then guard views with them.
- **Roles** are per organization. 8 system roles are seeded per new organization (Super admin, Hospital
  admin, Reception, Doctor, Nursing, Lab technician, Pharmacist, Cashier). System roles can be edited except
  Super admin, which is locked to all permissions. Custom roles can be added. Hospital admin has no clinical
  access by default (design kit matrix).
- **RoleAssignment(user, role, branch)**: branch empty = all branches. A user's permissions in a request are
  the union of roles assigned for all branches or for the **current branch** (session, switchable in the
  top bar). No active role in an active branch → "Access not assigned" page.
- **Per request** (`WorkspaceMiddleware`): `request.organization`, `request.branch`, `request.branches`,
  `request.sd_perms`. Templates get `sd_perms`, `organization`, `current_branch` (UI hints only).
- **Guards:** `@permission_required("code", ...)` (all) or `any_of=True` in `apps/accounts/decorators.py`.
  Queries go through `apps/core/scoping.py`: `scoped(Model, request)` / `get_scoped_or_404(...)`.
  New business models inherit `OrgScopedModel` or `BranchScopedModel`. Cross-organization access is a 404.
  Staff, roles and branches are organization-level records (no branch filter).
- **Escalation rules:** you can only assign roles, and add/remove permissions, that you hold yourself.
  Only a Super admin can reset/deactivate another Super admin. The last Super admin cannot be deactivated
  or lose the role. You cannot deactivate yourself. The main branch cannot be deactivated.
- **Sign-in protection:** django-axes locks a username+IP after `SEHATDESK_LOGIN_FAILURE_LIMIT` (5) failures
  for `SEHATDESK_LOCKOUT_MINUTES` (15). Admins can clear it from the staff profile ("Unlock sign-in").
- **Sessions:** idle timeout `SEHATDESK_SESSION_IDLE_MINUTES` (60), refreshed on every request. Expired users
  see a "Session expired" banner; htmx requests get `HX-Redirect` to login instead of a broken fragment.
  Deactivating a user or organization ends their sessions on the next request.
- **Passwords:** minimum 10 characters plus Django validators. Admin-created accounts and admin resets set
  `must_change_password`, which blocks every page until the user picks a new password. "Forgot password"
  emails a reset link only when `SEHATDESK_EMAIL_HOST` is configured; otherwise it tells the user to ask an admin.
- **Audit:** `apps/audit` `AuditLog` is append-only. `audit.record(request, "action", obj, changes=...)`.
  Logged now: login, logout, failed login (username only), setup, password change/reset, staff create/edit/
  activate/deactivate/unlock, role assign/remove, role create/update/(de)activate, organization and branch
  changes, branch switch. The audit viewer is Settings → Audit trail (Phase 23, see §14).
- **Media:** uploads live in `DATA_DIR/media`, served by Django behind login (`/media/...`). Logos only
  allow PNG/JPG/WebP ≤ 2 MB. Patient documents (Phase 18) will need a per-record permission-checked view
  instead of the plain media URL.
- **Timezone/language:** the organization's timezone and language are activated per request.

## 10. Master data, patients and appointments (Phases 3–5)
- **Scope:** Department, Specialty, DoctorProfile, ServiceCategory, Service and Patient are
  organization-wide (`OrgScopedModel`). DoctorSchedule and ServiceBranchPrice carry a branch.
  Appointment is branch-scoped (`BranchScopedModel`). Patients keep one MRN across all branches and
  remember `registered_branch`.
- **New permission** `admin.masterdata.manage` (Settings → Departments / Doctors / Services).
  `sync_permissions()` now grants a *newly added* code to existing system roles whose default includes it,
  so new modules reach existing hospitals without overwriting admins' role edits.
- **Numbers:** `core.Sequence` + `next_number(org, key)` give per-organization counters inside the caller's
  transaction. MRN = `<patient_id_prefix>-000001`; appointments = `APT-000001`.
- **Doctors** are staff accounts plus a DoctorProfile (fees, follow-up window, slot length, overbooking).
  Weekly schedule per branch and weekday with an optional break; leave blocks slots. Adding leave over
  existing bookings warns; it never cancels them automatically.
- **Services:** default price + optional per-branch price (`service.price_for(branch)`). Billing uses them
  from Phase 7.
- **Patients:** phone stored normalized (`03001234567`), CNIC stored as 13 digits; both shown formatted.
  Search: MRN, phone, CNIC, other identifiers, name tokens, with "did you mean" fuzzy suggestions.
  Duplicate check (live panel + on save): same CNIC, or similar name with same phone/DOB = strong match and
  blocks saving until the user confirms "different person" (audited as `duplicate_override`).
  Age can be entered instead of DOB (`dob_is_estimated`). Gender and allergy status default to
  "not recorded"; allergy status distinguishes *not asked* / *none known* / *has allergies*.
- **Clinical safety:** allergies are never deleted, only marked "entered in error" with a reason.
  Opening a patient record writes a `patient.view` audit event. Demographic edits store field diffs.
- **Patient photos are PHI:** served only by `/patients/<id>/photo/` (permission + org checked). The public
  `/media/` route now serves **only** `organization-logos/`.
- **Appointments:** statuses Scheduled → Confirmed → Checked in → (In queue → With doctor: Phases 6/8) →
  Completed; Cancelled (reason required) and No-show (only after start time) release the slot. Allowed
  transitions live in `Appointment.TRANSITIONS`; views call `appointments/services.py`, never set status.
  Booking rejects past dates, leave days, non-slot times and clashes (a doctor is checked across all
  branches). Walk-in/emergency may book outside slots and overbook. Same patient + doctor + day is blocked.
  Follow-up visit type and fee are suggested when the last completed visit is within the doctor's window.
- **Double-booking race:** `_lock_bookings()` takes a write lock (SQLite) / row lock (PostgreSQL) through
  `next_number` before checking clashes, so two desks cannot take the same slot at the same moment.
- **Calendar:** day view (columns = doctors working or booked, rows = real slot times so every "+ Book" is
  valid), week view (bookings per doctor per day), list view with check-in / no-show.

## 11. Reception and token queue (Phase 6)
- **App `apps/queue`:** `Counter` (reception desks, branch-scoped) and `QueueToken` (branch-scoped).
  Counter IDs identify desks and never replace clinic token IDs.
- **Numbering:** per branch, per department, per day: `<Department.token_letters>-NNN` (e.g. G-001).
  `Department.token_prefix` sets the letters (defaults to the first letter of the department code).
  Uses `core.next_number` with key `token:<branch>:<department>:<yyyymmdd>`.
- **Queues:** a token belongs to a doctor's queue, or to the department queue (doctor empty); a doctor's
  queue view includes their department's unassigned tokens, and calling one assigns it to that doctor.
- **Order:** emergency → special → urgent → normal, then `queued_at`. Special priority needs the new
  permission `priority.special` (only Super admin by default). A skipped token put back in line gets a new
  `queued_at` (back of its priority group).
- **Lifecycle** (`apps/queue/services.py`): waiting → called → in_consultation → completed; skip / requeue /
  recall; cancel needs a reason. A doctor must finish (complete/skip/start) the current token before calling
  the next. The linked appointment follows: in_queue → with_doctor → completed / cancelled.
- **Check-in issues the token:** `appointments:check-in` now creates the token and moves the appointment to
  "In queue" in one step. Walk-ins with a doctor also create a walk-in (or emergency) appointment so the
  visit shows in patient history; department-only walk-ins create a token only. Walk-ins are refused when
  the doctor is on leave (emergency visits are still allowed).
- **Who can operate a queue:** `queue.manage` (reception) for any doctor; a doctor with
  `consultation.create` only for their own queue (plus their department queue).
- **Live updates:** htmx polling every 5 seconds on the desk, doctor queue and public display; polling pauses
  while the browser tab is hidden. No websocket server is needed on the clinic machine.
- **Public waiting-room display:** `/display/<branch.display_key>/`, no login. Shows token numbers, rooms and
  doctor names only — never patient names or MRNs. `Cache-Control: no-store`, `noindex`. Created and rotated
  from Reception → Counters & display (needs `admin.branches.manage`); rotating revokes the old link.
- **Token slip:** `/queue/tokens/<id>/slip/` sized for 80 mm receipt printers (browser print).
- **Average wait** = token issued → first call, averaged in Python (portable between SQLite and PostgreSQL).

## 12. Vitals, diagnoses, prescriptions, laboratory and pharmacy stock (Phases 9–12)
- **Vitals (`apps/vitals`):** one `VitalSign` row per set of readings, branch-scoped, linked to the patient
  and optionally a token/encounter. Temperature stored in °C (form accepts °F, default °F); BMI computed on
  save. Readings are never edited: wrong ones are marked *entered in error* with a reason. Flags use common
  adult ranges (`vitals/services.py BANDS`); pulse, breathing rate, BP and BMI are **not** flagged under 18.
  Vitals station `/vitals/` lists today's open tokens (`vitals.record`); reception desk rows get a Vitals
  button for users holding `vitals.record` (Reception role does not by default). Patient record → Vitals tab
  shows trend sparklines (inline SVG, no chart library).
- **Diagnoses (`encounters.DiagnosisCode`, `EncounterDiagnosis`):** ICD-10 style catalogue per organization,
  a starter list loads on first search (`encounters/diagnosis_codes.py`), Settings → Diagnosis codes adds more.
  Free text is always allowed. A coded diagnosis now satisfies the "diagnosis required to complete" rule.
- **Medicine master (`prescriptions.Medicine`):** shared by prescribing and pharmacy products. Settings →
  Medicines (`admin.masterdata.manage` or `pharmacy.inventory.manage`).
- **Prescriptions:** one per encounter, draft while the encounter is a draft, **signed with it**. Each
  `EncounterVersion.content` now also stores `diagnoses`, `prescription` and `advice`, so amendments keep
  what was signed. Reopening returns the prescription to draft and clears "sent to pharmacy" (it must be
  re-sent after re-signing). Reopening is refused once the pharmacy has dispensed any of it (Phase 13). Quantity is suggested for
  countable forms (dose × frequency × duration). Allergy screen = word match between recorded allergy
  substances and brand/generic names; prescribing anyway needs a reason (audited). Favourites and "reuse
  previous" skip allergy matches. Print is A5 with doctor details; drafts print with a DRAFT watermark.
- **Laboratory (`apps/lab`):** catalogue (categories, tests with sex/age reference ranges + critical limits,
  panels at a package price), orders (doctor from the consultation, or lab desk), one sample per sample
  type with accession `S<yymmdd>-NNNN` and Code 39 SVG label, results versioned (`LabResult`).
  Order status is **derived** (`lab/services.refresh_status`), never set by views. Creating an order issues
  an invoice with `lab` lines; paying it moves the order out of *payment pending* via an `Invoice`
  post_save signal (`lab/signals.py`), so billing does not import the lab. Routine samples wait for payment
  unless a reason is given; urgent/STAT collect immediately. Unverified results are corrected in place;
  verified results only by amendment (`lab.result.verify` + reason → new version, re-verify, re-release).
  Critical results show on the lab worklist, the order page and the ordering doctor's workspace until
  acknowledged. Clinicians see released reports only; lab staff can preview. Cancelling an order voids its
  unpaid invoice (`billing.void_unpaid`); paid ones must be refunded first.
  New permission `lab.catalog.manage` (Lab technician role gets it).
- **PDF:** lab report `?format=pdf` uses WeasyPrint when its native libraries load; otherwise it shows
  "use Print → Save as PDF". On this dev Mac WeasyPrint's GTK/Pango libraries are **not** installed
  (`brew install pango` would fix it); Windows installers must bundle them (§3).
- **Pharmacy inventory (`apps/pharmacy`):** `Product` (links to Medicine), branch-scoped `InventoryBatch`,
  append-only `StockTransaction`. All stock changes go through `pharmacy/stock.py` (row lock, negative-stock
  check, ledger row in one transaction); `InventoryBatch.save()` refuses quantity edits and a DB check
  constraint blocks negative stock. Quantities are in base units (tablets); receiving takes packs × pack
  size + loose units. FEFO = `stock.saleable()` / `stock.fefo_allocate()` (expired batches excluded; sale of
  an expired batch is refused). Alerts: low stock (≤ reorder level), near expiry (90 days), expired on shelf;
  shown on `/inventory/` and the main dashboard. Sale/sale-return services exist for the POS (Phase 13).

## 13. Pharmacy POS and purchasing (Phases 13–14)
- **POS (`apps/pharmacy/pos.py`, screens under `/pharmacy/`):** `PharmacySale` is a cart (open / held) until
  checkout; one open cart per cashier per branch. Each `PharmacySaleItem` is one product from **one batch**
  (picked FEFO when added, changeable per line), so the cart shows the exact batch and price sold. Carts do
  not reserve stock; checkout re-locks each batch (`stock.sell`).
- **Checkout is one transaction:** stock-out per line + a billing `Invoice` (items kind `pharmacy`) issued and
  **paid in full** (split across up to 3 payment methods). Any failure rolls back everything. A completed sale
  replayed (double click) returns the same sale. Change for cash is worked out in the browser only.
- **Walk-in sales:** `Invoice.patient` is now optional; `Invoice.customer_name` names the walk-in customer and
  `Invoice.bill_to` is what templates show. Visit invoices still require a patient.
- **Permissions:** new `pharmacy.discount` (sale-level discount, reason required, recorded as `discount_by`).
  It is **not** in the default Pharmacist role — grant it per hospital. A supervisor holding it can set the
  discount on another cashier's open cart; `billing.set_invoice_discount(..., authorized_by=...)` carries the
  approval into the invoice. Pharmacy returns refund money with `pharmacy.refund`
  (`billing.refund(..., permission="pharmacy.refund")`); checkout needs `pharmacy.sale` + `billing.payment.collect`.
- **Prescription-only products** (`Product.requires_prescription`) cannot be checked out unless the line is
  linked to a line of an attached, signed, sent prescription.
- **Dispensing:** `Prescription.dispense_status` = pending / partial / dispensed / closed, recomputed from
  completed sales minus returns (`pos.refresh_dispense_status`). The pharmacy queue is `/pharmacy/dispense/`
  (the old `/pharmacy/prescriptions/` inbox redirects there). Partial fulfilment is allowed; "Close
  prescription" (reason) ends it. **Substitution controls:** a different product needs a reason, and when both
  generic names are known they must match; substitutions are audited. Encounters with a dispensed prescription
  can no longer be reopened.
- **Returns:** per-line quantities up to sold − already returned; stock goes back to the original batch
  (`stock.sale_return`), money is refunded from the original payment(s) pro rata to any sale discount.
  Returned packs are restocked; damaged ones must be written off from Inventory.
- **Receipt:** `/pharmacy/sales/<id>/receipt/`, 80 mm. Keyboard on the till: F2 search, F4 hold, F8 pay.
- **Purchasing (`apps/pharmacy/purchasing.py`, screens under `/inventory/`):** `Vendor` (org-wide),
  `Purchase` (branch): draft → ordered (optional) → received, or cancelled before receipt. Lines carry
  quantity, free/bonus units and cost; batch, expiry and sale price are filled in at goods receipt. Posting
  (`receive`) needs the supplier invoice number and a batch + future expiry on every line, then posts each
  line through `stock.receive` (shelf cost = line cost ÷ (qty + free)), updates the product's default cost, and
  adds the purchase total to the supplier ledger — all in one transaction. Unreceived quantities are never
  stock. Purchase returns use `stock.purchase_return` and credit the ledger at shelf cost.
- **Supplier balance:** `VendorLedgerEntry` (append-only; + = owed more). Kinds: opening, purchase,
  purchase_return, payment, adjustment. Supplier **payments are not built yet** (future phase) — the kind
  exists so balances need no migration later.
- **Purchase report:** `/inventory/reports/purchases/` (date range, supplier filter, CSV export); opens for
  `pharmacy.inventory.manage` or `reports.financial.view`. All purchasing writes need `pharmacy.inventory.manage`.
- Patient documents are stored privately in the data folder (no public media URL) and served only through signed, expiring, permission-checked links. Object storage (S3-compatible) can be switched on with `SEHATDESK_DOCUMENT_STORAGE` without code changes.
- Notifications are in-app first; time-based checks run inside the server process (no Celery/Redis on the hospital machine).

## 14. Reports, dashboards, global search and audit trail (Phases 20–23)
- **Reports (`apps/reports`, `/reports/`):** one registry (`engine.py`, `@report(...)`) with 30 reports in
  `definitions/` (clinical, financial, lab, pharmacy, IPD). Each declares its permissions (any one) and which
  filters it takes (date / branch / department / doctor). Screen, CSV, XLSX and PDF all use the same result.
  - Permissions: operational reports `reports.view`; money reports `reports.financial.view`; stock reports
    also open to `pharmacy.inventory.manage`; purchases to `reports.financial.view` or stock managers.
  - **Branch filter never widens access:** it lists only branches where the user holds the report's permission
    (checked per branch with `effective_permissions`, not only in the current branch). Another id is a 403.
  - Ranges are capped at 366 days. Dates filter on local-day boundaries (`start_dt` / `end_dt`), portable to
    PostgreSQL. Collections reuse `billing.day_totals`, so they match the cashier's day closing; billed revenue
    (issued invoice lines by issue date) is a separate number and says so on screen.
  - Revenue attribution: department = service's department → appointment's → admission's, else Laboratory /
    Pharmacy / Inpatient rooms. Doctor = appointment doctor → admitting doctor → ordering lab doctor.
    Invoice-level discounts are shown separately (not split across lines).
  - Exports: CSV (UTF-8 with BOM for Excel), XLSX via **openpyxl** (new runtime dependency, pure Python),
    PDF via WeasyPrint (A4 landscape) with the print page as fallback. Every export writes `report.export`.
- **Dashboards (Phase 21):** admin KPIs on the main dashboard for `reports.view` / `reports.financial.view`
  (money tiles need the financial permission), with a 14-day trend. Lab dashboard `/dashboards/lab/` (lab staff
  or `reports.view`); pharmacy dashboard `/dashboards/pharmacy/` (pharmacy staff or `reports.view`). KPIs live
  in `apps/reports/kpis.py` and are for the current branch.
- **Global search (`apps/search`):** top-bar box (`/` or Ctrl/Cmd+K, arrows to move, Esc to close), htmx
  suggestions (`/search/suggest/`) and a full page (`/search/`). Providers: patients, appointments, invoices,
  lab orders (number, accession, patient), medicines (products for pharmacy staff, else the medicine master),
  staff. A provider runs only when the user holds one of its permissions, and only on `scoped()` querysets.
  Recent searches are kept in the user's session only. Query text is never logged.
- **Audit trail (Phase 23):** `AuditLog` is append-only at three levels: `save()`/`delete()` refuse changes, the
  queryset refuses `update()`/`delete()`, and migration `audit.0002` adds database triggers (SQLite and
  PostgreSQL) that abort any UPDATE/DELETE — so `manage.py flush` on a database with audit rows will fail by
  design. Viewer: Settings → Audit trail (`admin.audit.view`), filters by staff, event type, action/module,
  record type and ID, branch, date, text; details drawer; CSV export (audited as `audit.export`).
  New events: `encounter.note_save` (field names only, coalesced per editor per 10 minutes),
  `prescription.item_add/item_remove/advice/copy`. `audit.record_ai_acceptance()` is ready for the AI assistant
  (Phase 15) and must only be called when a clinician saves reviewed text.

## 15. Environments, staging and CI (Phase 0)
- **Three environments** (`SEHATDESK_ENV`, validated at startup):
  - `development`: local work and tests. Debug allowed, dev secret key, console logs.
  - `staging`: the release build on a test machine with **demo data only**, to try a version before it
    reaches a hospital. Same rules as production, plus an amber "Staging" strip on every page
    (`templates/partials/env_banner.html`), its own data folder (`./data-staging`) and port 8001, and patient
    SMS/WhatsApp forced off unless `SEHATDESK_STAGING_SEND_MESSAGES=true`. Template: `deploy/staging.env.example`.
  - `production`: hospital servers. Template: `deploy/production.env.example` (the launcher will write the
    real values and secret into the data folder, §3).
- **Staging and production:** refuse to start with `DJANGO_DEBUG=true`; generate a secret key file on first
  start when `DJANGO_SECRET_KEY` is not set (§19); log to
  `DATA_DIR/logs/sehatdesk.log` (rotating, 5 × 5 MB) as well as the console. Static files are served from
  `STATIC_ROOT`, so `collectstatic` is a release/installer build step.
- **HTTPS switch:** `SEHATDESK_HTTPS=true` once a TLS proxy terminates HTTPS in front of Waitress: secure
  cookies, https redirect (except `/health/`), HSTS (`SEHATDESK_HSTS_SECONDS`, default 1 year), and trusts the
  proxy's `X-Forwarded-Proto`. Only turn it on behind a proxy that sets that header.
- **`check --deploy`:** while the LAN is plain HTTP, the four known pilot warnings (HSTS, https redirect,
  insecure session/CSRF cookies) are silenced in staging/production; every other deploy warning must pass.
  Run before go-live: `SEHATDESK_ENV_FILE=... python manage.py check --deploy --fail-level WARNING`.
- **Run staging locally:** `cp deploy/staging.env.example deploy/staging.env`, set the secret, then
  `SEHATDESK_ENV_FILE=deploy/staging.env python manage.py migrate`, `... collectstatic --noinput`,
  `... python serve.py --port 8001`.
- **CI (`.github/workflows/ci.yml`)**, on pushes to `main` and every PR, in a conda env named `agent_dev311`:
  `lint` (all pre-commit hooks + committed Tailwind CSS is up to date), `test` (pytest on Ubuntu, Windows and
  macOS), `test-postgres` (pytest on PostgreSQL 16), `dependency-audit` (pip-audit), `docker` (image builds and
  serves with PostgreSQL), `deploy-config` (staging and production: `check --deploy`, migrate,
  collectstatic, start `serve.py`, probe `/health/ready/` and a static file, confirm the log file and the
  generated secret key). Installer build jobs are added with the installer phase (§3).

## 16. Observability, PostgreSQL and Docker (Phase 0 completion)
- **Request IDs:** `apps.core.monitoring.RequestContextMiddleware` (first middleware) gives every request an
  ID (reuses a well-formed `X-Request-ID`), puts it in every log line and error report, returns it as
  `X-Request-ID`, and shows it on the 500 page as "Reference" so staff can quote it.
- **Logs:** every handler runs `RedactFilter` (masks phone numbers, CNICs, emails) and adds the request ID.
  Deployed machines write JSON lines to `DATA_DIR/logs/sehatdesk.log`; their tracebacks keep stack frames but
  drop exception messages (messages can quote patient data). `SEHATDESK_LOG_FORMAT=json` for the console too.
- **Errors:** unexpected server exceptions (`got_request_exception`) and browser script errors
  (`static/js/app.js` → `POST /telemetry/client-error/`, same-origin, 20/min/IP) are stored in `core.ErrorReport`
  for 90 days: path without query string, exception type, stack frames, redacted browser message.
  **Settings → System** (`admin.system.view`, new; Hospital admin gets it) lists them with metrics and backups.
- **Metrics:** in-process counters (requests by status class, latency histogram, exceptions, browser errors)
  at `/health/metrics/` in Prometheus text format: open to the server itself (loopback), otherwise only to
  `admin.system.view`.
- **Transactions:** `apps/core/db.py` `critical_write` / `locked()` / `after_commit()` for money and stock
  writes. SQLite now uses `transaction_mode=IMMEDIATE` (writers queue instead of failing with "database is
  locked") and WAL journal mode. All `select_for_update()` calls use `of=("self",)`: PostgreSQL refuses to lock
  the nullable side of an outer join (found by running the suite on PostgreSQL).
- **PostgreSQL:** optional, `SEHATDESK_DATABASE_URL=postgres://...` + `requirements-postgres.txt`. The whole
  suite passes on PostgreSQL 16 (locally via Docker and in CI `test-postgres`). SQLite stays the default for
  hospital installs.
- **Docker:** `Dockerfile` (python:3.11-slim, Pango for PDFs, pg_dump for backups, non-root, healthcheck,
  migrate on start) and `docker-compose.yml` (`db` = PostgreSQL 16; `app` = staging config on :8001). For
  staging, demos and future hosted servers; hospitals still get the installers (§3).
  `docker compose up -d db` gives a local PostgreSQL for tests.

## 17. Printing (Phase 24)
- One print engine: documents extend `print/a4.html` (invoice, lab report, admission form, discharge summary),
  `print/a5.html` (prescription, registration slip, appointment slip, lab order) or `print/thermal.html`
  (80 mm: token slip, payment receipt, pharmacy receipt); the sample label sets its own 50 × 25 mm page.
  `print/base.html` holds the toolbar (hidden on paper) and marks staging prints "not a valid document".
- Branding comes from `print/_letterhead.html` / `_thermal_header.html` and the footers: logo, hospital name,
  branch, branch address/phone (falling back to the hospital's), registration number, header text, footer
  text, and the new **printed terms** (`Organization.print_terms`, on invoices and receipts only).
- New documents: appointment slip `/appointments/<id>/slip/` (`appointment.view`), lab order form
  `/lab/orders/<id>/print/` (lab staff, clinicians, or `lab.order.create`).
- `reports/print.html` (report exports) keeps its own WeasyPrint stylesheet: it is a data export, not a
  patient document.

## 18. Settings → Preferences and billing rules (Phase 25)
- `organizations.OrganizationPreferences` (one row per hospital, `preferences_for(org)`, cached per request):
  - **Module switches** lab / pharmacy+inventory / IPD+nursing. `apps.core.features.FeatureMiddleware` answers 404
    ("turned off") for the module's URL namespaces and its dashboards; navigation, dashboard tiles, patient
    record tabs, the consultation lab panel and global search hide it. Data is kept.
  - Default consultation length and follow-up window for **new** doctors; waiting-room display refresh
    (3–60 s) and whether doctor names show; pharmacy expiry alert window (replaces the hard-coded 90 days
    everywhere, including the expiry report) and default reorder level for new products.
- Billing settings add **largest discount %** (`billing.check_discount_cap`, enforced for line, invoice and
  POS discounts) and the **receipt message**. Negative stock stays impossible (service + DB constraint).
- Every Preferences save is audited as `organization.preferences` with field changes.

## 19. Security hardening (Phase 26)
- `apps/core/security.py` `SecurityHeadersMiddleware`: CSP (`default-src 'self'`, `connect-src 'self'`,
  `form-action 'self'`, `frame-ancestors 'none'`; scripts need `'unsafe-inline'` for print buttons and
  `'unsafe-eval'` for Alpine), Permissions-Policy, and `Cache-Control: private, no-store` on signed-in pages
  (no patient data in a shared PC's browser cache). Also Referrer-Policy and COOP `same-origin`,
  `CSRF_COOKIE_HTTPONLY`, no CORS headers.
- `rate_limit()` decorator (per IP, cache-based): password reset 5/15 min, reset confirm 10/15 min, setup
  10/15 min; sign-in keeps django-axes.
- **Secret key:** staging/production create `DATA_DIR/secret.key` (random, mode 600, O_EXCL so two processes
  agree) on first start and reuse it; `DJANGO_SECRET_KEY` still wins when set. Development keeps the dev key.
- **Dependencies:** pip-audit (CI job `dependency-audit`). WeasyPrint raised to 70.x and pytest to 9.0.3+ for
  known CVEs; test tools moved out of `requirements.txt` into `requirements-dev.txt`.
- **Tests:** `test_url_inventory.py` walks every URL (≈270): anonymous → sign-in, signed-in user with an empty
  role → 403/404/405 (public and personal pages are an explicit, reviewed allow-list). `test_isolation.py`
  opens real records by ID as another hospital's Super admin (all 404) and another branch's admin (branch
  records 404). `test_security.py`: headers, CSRF, Origin check, rate limits, session invalidation, no PHI in
  logs.
- Fix found by the journey tests: Hospital admin's "IPD & wards" link led to a 403; the bed board now sends
  facility-only admins to IPD setup.

## 20. Performance (Phase 28)
- New indexes: patients (org + newest, org + name), appointments (patient + date), tokens (doctor + day +
  status), invoices (org + patient + newest), lab results (flag + current).
- Product, admission and purchase lists are paginated (`core.scoping.paginate`) instead of silently cut at
  200–300 rows.
- Dashboard trend used 16 × `day_totals` (≈280 queries per load); `billing.net_by_day()` gives the same numbers
  in 4 queries (tested equal to the cashier's day closing). Dashboard now < 80 queries.
- `test_query_scaling.py` fails if a main list page runs more queries for 12 rows than for 3 (N+1 guard).
- `python manage.py loadtest` (empty data folder only) seeds 20,000 patients and times the hot paths. On the
  dev Mac (SQLite): patient search 12–18 ms median, global search 12 ms, dashboard 70 ms, POS checkout 10 ms.

## 21. Backup and recovery (Phase 29)
- Full procedures: **docs/BACKUP_AND_RECOVERY.md** (what is in a backup, key handling, restore, restore
  tests, disaster recovery table, production rollback).
- `apps/core/backup.py`: SQLite online-backup snapshot (or `pg_dump` on PostgreSQL) + `private/documents` +
  `media` + manifest with SHA-256 per file, in one zip; optional AES-256-GCM chunked encryption with
  `SEHATDESK_BACKUP_KEY` (tamper, truncation and wrong key are detected). The secret key is not included.
- `serve.py` backs up at start when the last backup is older than `SEHATDESK_BACKUP_HOURS` (24), then checks
  hourly; keeps `SEHATDESK_BACKUP_KEEP` (14). Settings → System lists backups, warns when overdue or
  unencrypted, and has "Back up now" (audited; never downloads through the browser).
- Commands: `backup`, `backup --list | --verify FILE | --generate-key`, `restore FILE --yes` (server stopped;
  current files moved to `DATA_DIR/pre-restore-<timestamp>/`, then migrate).
- CI restore drill (`test_backup.py`): plain and encrypted backup → data loss → restore → data back.
