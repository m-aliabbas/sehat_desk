# SehatDesk — TODO_TASKS.md

**Purpose:** Developer execution checklist for building SehatDesk.  
**Recommended delivery style:** Complete phases in order; do not build advanced AI or IPD before patient, OPD, billing, RBAC, and audit foundations are stable.

Legend:
- [ ] Not started
- [~] In progress
- [x] Completed

---

# PHASE 0 — Repository & Engineering Foundation

## Project Setup
- [x] Create monorepo or agreed repository structure.
- [x] Initialize frontend application.
- [x] Initialize backend application.
- [x] Add TypeScript strict mode. (N/A: Django templates + HTMX/Alpine, no TypeScript)
- [x] Configure environment variables.
- [x] Add `.env.example`.
- [x] Configure linting.
- [x] Configure formatter.
- [x] Add pre-commit checks. (`.pre-commit-config.yaml`: ruff, Django check, migrations, forbidden files)
- [x] Add GitHub Actions CI. (`.github/workflows/ci.yml`: lint, tests on Ubuntu/Windows/macOS, staging+production boot)
- [x] Add staging environment. (`SEHATDESK_ENV=staging`, `deploy/staging.env.example`, banner, messages off)
- [x] Add production environment configuration. (`deploy/production.env.example`, debug refused, file logs, HTTPS switch)
- [x] Add Dockerfiles. (`Dockerfile`: staging/demo/hosted image; hospitals still use the installers)
- [x] Add Docker Compose for local dependencies. (`docker-compose.yml`: PostgreSQL + app)
- [x] Add application health endpoints.

## Database
- [x] Provision PostgreSQL. (optional via SEHATDESK_DATABASE_URL; full suite passes on PostgreSQL 16 locally and in CI; SQLite stays the default)
- [x] Select ORM/query layer.
- [x] Configure migrations.
- [~] Configure seed framework. (permissions/default roles seeded; demo data in Phase 31)
- [x] Add created_at / updated_at conventions.
- [x] Add soft-delete/archive conventions.
- [x] Define organization/tenant scoping standard.
- [x] Define branch scoping standard.
- [x] Add transactional helper for billing/inventory workflows. (`apps/core/db.py`; SQLite BEGIN IMMEDIATE + WAL)

## Observability
- [x] Add structured backend logs. (JSON log file on deployed machines; SEHATDESK_LOG_FORMAT)
- [x] Add frontend error reporting. (browser errors → Settings → System)
- [x] Add backend exception reporting. (ErrorReport, 90 days, no patient data)
- [x] Add request correlation IDs. (X-Request-ID in logs, error reports and the 500 page)
- [x] Add basic metrics. (`/health/metrics/` Prometheus text; System page)
- [x] Define PHI-safe logging rules. (CLAUDE.md rule + redaction filter + safe tracebacks)

---

# PHASE 1 — Design System & Application Shell

## Design System
- [x] Define SehatDesk brand palette.
- [x] Define typography.
- [x] Define spacing scale.
- [x] Define border/radius conventions.
- [x] Define status colors.
- [x] Build button component.
- [x] Build input component.
- [x] Build select component.
- [x] Build date/time picker.
- [x] Build modal/dialog.
- [x] Build drawer.
- [x] Build table.
- [x] Build pagination.
- [x] Build tabs.
- [x] Build cards.
- [x] Build badges/status chips.
- [x] Build toast/notification.
- [x] Build empty state.
- [x] Build loading skeleton.
- [x] Build error state.
- [x] Build confirmation dialog.

## App Shell
- [x] Sidebar navigation.
- [x] Topbar.
- [x] Global search placeholder.
- [x] Notifications menu.
- [x] Profile menu.
- [x] Branch selector.
- [x] Breadcrumbs.
- [x] Permission-aware navigation.
- [x] Tablet responsiveness.

---

# PHASE 2 — Authentication, Organization & RBAC

## Authentication
- [x] Login screen.
- [x] Login API.
- [x] Password hashing.
- [x] Auth token/session strategy.
- [x] Logout.
- [x] Forgot password. (email link when SMTP is configured; otherwise admin reset)
- [x] Reset password.
- [x] Session expiration.
- [x] Login attempt rate limiting.
- [x] Account disabled handling.

## Organization
- [x] Organization database model.
- [x] Branch database model.
- [x] Organization settings CRUD.
- [x] Branch CRUD.
- [x] Branch switcher.
- [x] Hospital logo upload. (PNG/JPG/WebP ≤ 2 MB; SVG rejected)
- [x] Receipt/report header configuration.
- [x] Timezone setting.
- [x] Currency setting.
- [x] Language setting foundation. (stored and activated; Urdu translations later)

## Staff
- [x] Staff/user model.
- [x] Staff list screen.
- [x] Create user screen.
- [x] Edit user.
- [x] Activate/deactivate user.
- [x] Assign branch.
- [x] Assign role.
- [x] Staff profile.

## RBAC
- [x] Permission model.
- [x] Role model.
- [x] Role-permission mapping.
- [x] Seed system permissions.
- [x] Seed default roles.
- [x] Custom role creation.
- [x] Role edit screen.
- [x] Permission matrix screen.
- [x] Backend permission guard.
- [x] Frontend permission helper.
- [x] Verify no UI-only authorization.

---

# PHASE 3 — Master Data

## Departments
- [x] Department model.
- [x] Department list.
- [x] Create department.
- [x] Edit department.
- [x] Activate/deactivate department.

## Doctors
- [x] Doctor profile model.
- [x] Specialty.
- [x] Department mapping.
- [x] Consultation fee.
- [x] Follow-up fee/rule.
- [x] Doctor availability.
- [x] Doctor working days.
- [x] Doctor slot duration.
- [x] Doctor profile UI.

## Services
- [x] Service category.
- [x] Service master.
- [x] Service pricing.
- [x] Branch-specific pricing.
- [x] Active/inactive state.

---

# PHASE 4 — Patient Management

## Patient Model
- [x] Patient table.
- [x] MRN generator.
- [x] Patient identifiers.
- [x] CNIC field.
- [x] Phone normalization.
- [x] Emergency contact.
- [x] Address.
- [x] Blood group.
- [x] Allergy storage foundation.
- [x] Patient photo support.

## Patient Registration
- [x] New patient page.
- [x] Validation.
- [x] Duplicate patient warning.
- [x] Register patient API.
- [x] Generate MRN.
- [x] Print registration slip. (browser print, A5; PDF templates in Phase 24)
- [x] Quick registration mode.

## Patient Search
- [x] Search by MRN.
- [x] Search by phone.
- [x] Search by CNIC.
- [x] Search by name.
- [x] Fuzzy duplicate search.
- [x] Fast search indexing. (indexed MRN/phone/CNIC + lower-cased name column)
- [x] Permission-scoped results.

## Patient Profile
- [x] Patient header.
- [x] Demographics tab.
- [x] Visits tab.
- [x] Prescriptions tab. (empty state until Phase 10)
- [x] Lab reports tab. (empty state until Phase 11)
- [x] Billing tab. (empty state until Phase 7)
- [x] Documents tab. (empty state until Phase 18)
- [x] Timeline tab.
- [x] Edit demographics.
- [x] Patient alerts section.

---

# PHASE 5 — Appointment Management

## Appointment Backend
- [x] Appointment model.
- [x] Appointment status enum.
- [x] Doctor slot calculation.
- [x] Create appointment.
- [x] Reschedule appointment.
- [x] Cancel appointment.
- [x] Check-in appointment.
- [x] No-show handling.
- [x] Prevent conflicting slots where configured.

## Appointment UI
- [x] Appointment calendar.
- [x] Day view.
- [x] Doctor filter.
- [x] Department filter.
- [x] New appointment modal/page.
- [x] Reschedule flow.
- [x] Cancellation flow.
- [x] Today's appointment list.
- [x] Patient appointment history.

---

# PHASE 6 — Reception & Token Queue

## Reception Dashboard
- [x] Today's appointments widget.
- [x] Walk-in shortcut.
- [x] Register patient shortcut.
- [x] Doctors available widget.
- [x] Waiting patients.
- [x] Today's collection placeholder. (filled by billing in Phase 7)
- [x] Queue panels.

## Token System
- [x] Queue token model.
- [x] Token numbering strategy.
- [x] Doctor-specific queue.
- [x] Department-specific queue support.
- [x] Generate token.
- [x] Print token slip.
- [x] Call token.
- [x] Skip token.
- [x] Recall token.
- [x] Cancel token.
- [x] Complete token.
- [x] Priority handling.
- [x] Queue timestamps.
- [x] Waiting time calculation.

## Live Queue UI
- [x] Reception queue screen.
- [x] Doctor queue screen. (full consultation workspace in Phase 8)
- [x] Auto-refresh/realtime strategy. (htmx polling every 5 s; pauses in hidden tabs)
- [x] Current token.
- [x] Next patients.
- [x] Waiting count.
- [x] Queue status chips.

---

# PHASE 7 — Billing Foundation

## Invoice
- [x] Invoice model.
- [x] Invoice item model.
- [x] Invoice numbering.
- [x] Create invoice.
- [x] Add service items.
- [x] Calculate subtotal.
- [x] Discount logic.
- [x] Tax foundation. (per-item rate from Settings → Billing; default 0%)
- [x] Grand total.
- [x] Outstanding balance.
- [x] Invoice status.

## Payment
- [x] Payment model.
- [x] Payment methods configuration.
- [x] Receive payment.
- [x] Partial payment.
- [x] Multiple payments per invoice.
- [x] Payment receipt.
- [x] Payment history.

## Refund
- [x] Refund model.
- [x] Permission check. (billing.refund; no two-step supervisor approval yet)
- [x] Refund reason.
- [x] Refund against original payment/invoice.
- [x] Audit refund.

## Billing UI
- [x] Invoice detail.
- [x] Payment modal. (htmx; double-submit safe via idempotency key)
- [x] Print invoice.
- [x] Print receipt.
- [x] Outstanding invoice list.
- [x] Daily payment list.

---

# PHASE 8 — OPD / Encounter

## Encounter Foundation
- [x] Encounter model.
- [x] Link encounter to appointment/token.
- [x] Encounter status.
- [x] Encounter start time.
- [x] Encounter complete time.
- [x] Draft support. (signed versions kept in EncounterVersion; reopen creates a new version)

## Doctor Dashboard
- [x] Today's appointments.
- [x] Waiting queue.
- [x] Current patient.
- [x] Follow-ups.
- [x] Pending drafts.

## Consultation Workspace
- [x] Patient summary header.
- [x] Chief complaint.
- [x] History of present illness.
- [x] Past medical history.
- [x] Surgical history.
- [x] Family history.
- [x] Allergies. (structured allergies stay on the patient record; free-text allergy review in the note)
- [x] Current medicines.
- [x] Examination notes.
- [x] Provisional diagnosis. (free text; coded diagnoses in Phase 10)
- [x] Final diagnosis.
- [x] Treatment plan.
- [x] Follow-up.
- [x] Referral. (destination + reason on the note; referral workflow screens later)
- [x] Doctor notes.
- [x] Auto-save. (htmx, 2 s after typing stops; revision check blocks stale-tab overwrites)
- [x] Draft save.
- [x] Complete encounter.
- [x] Reopen permission. (consultation.reopen + reason; treating doctor only)

---

# PHASE 9 — Vitals

## Data
- [x] Vital model.
- [x] Temperature.
- [x] BP systolic/diastolic.
- [x] Pulse.
- [x] Respiratory rate.
- [x] SpO2.
- [x] Weight.
- [x] Height.
- [x] BMI calculation.
- [x] Blood glucose.
- [x] Custom notes. (free-text notes per reading; configurable custom vital fields later)

## UI
- [x] Vitals entry component.
- [x] Nurse/reception workflow.
- [x] Doctor read-only summary.
- [x] Historical trends.
- [x] Abnormal value styling. (adult ranges; pulse/RR/BP/BMI not flagged under 18)

---

# PHASE 10 — Diagnosis & Prescription

## Diagnosis
- [x] Diagnosis model.
- [x] Diagnosis search.
- [x] Provisional/final state.
- [x] Free-text fallback.
- [x] Diagnosis history.

## Medicine Master
- [x] Shared medicine master foundation.
- [x] Brand name.
- [x] Generic name.
- [x] Strength.
- [x] Form.
- [x] Route.

## Prescription
- [x] Prescription model.
- [x] Prescription item model.
- [x] Medicine search.
- [x] Dosage.
- [x] Frequency.
- [x] Duration.
- [x] Quantity.
- [x] Instructions.
- [x] Before/after food.
- [x] Favorite prescriptions.
- [x] Previous medication quick reuse.
- [x] Print prescription.
- [x] Send prescription to pharmacy. (pharmacy inbox; dispensing in Phase 13)
- [x] Prescription history.
- [x] Doctor signature/details template.

---

# PHASE 11 — Laboratory

## Lab Master
- [x] Lab test category.
- [x] Test model.
- [x] Test code.
- [x] Sample type.
- [x] Price.
- [x] Turnaround time.
- [x] Units.
- [x] Reference ranges.
- [x] Panels/packages.

## Lab Orders
- [x] Lab order model.
- [x] Lab order items.
- [x] Create from doctor.
- [x] Create from lab desk.
- [x] Link billing.
- [x] Payment status.
- [x] Order statuses.

## Sample Collection
- [x] Sample collection screen.
- [x] Mark sample collected.
- [x] Collection time.
- [x] Collector.
- [x] Sample identifier/barcode foundation. (accession S<yymmdd>-NNNN, Code 39 SVG label)
- [x] Recollection status.

## Lab Worklist
- [x] Pending samples.
- [x] Processing list.
- [x] Result pending list.
- [x] Verification pending list.
- [x] Filters.

## Result Entry
- [x] Numeric result.
- [x] Text result.
- [x] Boolean/positive-negative.
- [x] Dropdown result.
- [x] Interpretation text.
- [x] High/low calculation.
- [x] Critical flag.
- [x] Attachments.

## Verification
- [x] Verification permission.
- [x] Verify result.
- [x] Prevent unauthorized post-verification editing.
- [x] Correction/amendment flow.
- [x] Audit changes.

## Lab Report
- [x] Printable lab report.
- [x] Patient details.
- [x] Test details.
- [x] Reference range.
- [x] Result flags.
- [x] Technician.
- [x] Verifier/pathologist.
- [x] Hospital header/footer.
- [x] PDF export. (WeasyPrint when its system libraries exist; otherwise browser Print → PDF)

---

# PHASE 12 — Pharmacy Inventory

## Product Master
- [x] Product model.
- [x] SKU.
- [x] Barcode.
- [x] Brand.
- [x] Generic.
- [x] Manufacturer.
- [x] Category.
- [x] Unit.
- [x] Pack size.
- [x] Sale price.
- [x] Reorder level.

## Batch
- [x] Inventory batch model.
- [x] Batch number.
- [x] Expiry date.
- [x] Purchase cost.
- [x] Sale price.
- [x] Quantity.
- [x] Available quantity.
- [x] FEFO selection logic.

## Stock Ledger
- [x] Stock transaction model.
- [x] Purchase stock-in.
- [x] Sale stock-out. (stock.sell service; POS screens in Phase 13)
- [x] Sale return. (stock.sale_return service; POS return screens in Phase 13)
- [x] Purchase return.
- [x] Adjustment.
- [x] Expired/damaged.
- [x] Prevent unlogged stock mutation.
- [x] Negative stock protection.

## Alerts
- [x] Low stock query.
- [x] Near-expiry query.
- [x] Expired stock query.
- [x] Dashboard alerts.

---

# PHASE 13 — Pharmacy POS

## POS UI
- [x] Barcode input. (scan + Enter adds by barcode/SKU)
- [x] Product search.
- [x] Cart.
- [x] Quantity editing.
- [x] Batch selection.
- [x] Discount permission. (new pharmacy.discount; not in the Pharmacist role by default)
- [x] Patient attach.
- [x] Prescription attach.
- [x] Hold cart.
- [x] Resume cart.
- [x] Checkout.
- [x] Payment modal.
- [x] Print receipt.

## Prescription Queue
- [x] Pending prescriptions.
- [x] Open prescription.
- [x] Check availability.
- [x] Partial fulfillment. (plus "Close prescription" with a reason for the rest)
- [x] Complete fulfillment.
- [x] Record substitutions with required controls. (reason required, same generic only, audited)

## Sale Backend
- [x] Pharmacy sale model.
- [x] Sale item model.
- [x] Transactional stock reduction.
- [x] Payment link.
- [x] Invoice integration. (walk-in invoices allowed: Invoice.patient is optional, customer_name used)
- [x] Sale return.
- [x] Refund integration.

---

# PHASE 14 — Pharmacy Purchasing

- [x] Vendor model.
- [x] Vendor list.
- [x] Purchase order/purchase model.
- [x] Add purchase items.
- [x] Add batch/expiry.
- [x] Update stock transactionally.
- [x] Purchase invoice reference.
- [x] Purchase return.
- [x] Supplier balance future-ready. (VendorLedgerEntry; supplier payments to be added later)
- [x] Purchase report.

---

# PHASE 15 — AI Assistant (we will do this in future on demand)

## AI Infrastructure
- [ ] Create AI service abstraction.
- [ ] Define approved model providers.
- [ ] Configure environment keys securely.
- [ ] Create structured output schemas.
- [ ] Add timeout handling.
- [ ] Add retries.
- [ ] Add fallbacks.
- [ ] Add AI usage logging.
- [ ] Ensure tenant boundaries.

## Patient Summary
- [ ] Build patient history context loader.
- [ ] Limit context to authorized patient.
- [ ] Generate structured summary.
- [ ] Show source record dates.
- [ ] Label AI output.
- [ ] Add regenerate.
- [ ] Add copy-to-note.
- [ ] Never save automatically.

## Clinical Note Drafting
- [ ] SOAP note prompt.
- [ ] Custom note prompt.
- [ ] Use current encounter only unless history requested.
- [ ] Doctor review flow.
- [ ] Accept/edit/reject.
- [ ] Log accepted AI content.

## Follow-up Question Suggestions
- [ ] Generate optional questions.
- [ ] Do not present as diagnosis.
- [ ] Dismiss action.
- [ ] Disable per organization.

## Record Q&A
- [ ] Query patient's structured records.
- [ ] Return concise response.
- [ ] Cite internal record date/type.
- [ ] No cross-patient leakage.
- [ ] Permission checks.

## Safety
- [ ] AI disclaimer.
- [ ] Human confirmation for clinical record save.
- [ ] No automatic prescribing.
- [ ] No autonomous diagnosis.
- [ ] No autonomous orders.
- [ ] Add content safety/error fallback.
- [ ] Configure prompt versioning.

---

# PHASE 16 — IPD / Admission

## Facility
- [x] Ward model.
- [x] Room model.
- [x] Bed model.
- [x] Bed status.
- [x] Bed board UI.
- [x] Bed filters.
- [x] Occupancy indicator.

## Admission
- [x] Admission model.
- [x] Admission form.
- [x] Admitting doctor.
- [x] Ward/room/bed.
- [x] Admission diagnosis.
- [x] Attendant.
- [x] Deposit. (AdmissionDeposit; applied to the final bill as payments, leftover returned. Day totals count a deposit on the day taken and a returned balance as money out on the day returned; applied-deposit payments are not counted twice)
- [x] Admission number.
- [x] Prevent double bed allocation. (row lock + partial unique constraints on open occupancy)

## Inpatient Chart
- [x] Patient admission header.
- [x] Current bed.
- [x] Doctor rounds.
- [x] Nursing notes.
- [x] Vitals chart.
- [x] Lab orders.
- [x] Prescriptions.
- [x] Medication chart.
- [x] Procedures/services.
- [x] Billing summary.

## Transfers
- [x] Bed transfer.
- [x] Ward transfer.
- [x] Transfer reason.
- [x] Bed occupancy transaction.
- [x] Transfer history.

## Discharge
- [x] Discharge workflow.
- [x] Finalize pending services.
- [x] Final bill check.
- [x] Discharge diagnosis.
- [x] Discharge medicines.
- [x] Follow-up.
- [x] Discharge summary. (signed versions; amendments add a version; print/save as PDF)
- [x] Mark bed cleaning/available.

---

# PHASE 17 — Nursing 

- [x] Nursing dashboard.
- [x] Assigned patients.
- [x] Nursing notes.
- [x] Vitals schedule.
- [x] Intake/output foundation.
- [x] Medication administration record.
- [x] Dose administration status.
- [x] Missed/refused reason.
- [x] Handover notes.
- [x] Nursing audit trail.

---

# PHASE 18 — Documents & Attachments 

- [x] Secure file upload.
- [x] Allowed MIME types.
- [x] File size limit.
- [x] Object storage. (STORAGES["documents"]: private data folder now, S3-compatible via SEHATDESK_DOCUMENT_STORAGE)
- [x] Patient documents.
- [x] Encounter attachments.
- [x] Lab attachments.
- [x] Permission checks.
- [x] Signed/private access URLs. (per-user, expiring TimestampSigner links)
- [x] Audit document deletion/archive.

---

# PHASE 19 — Notifications 

## In-App
- [x] Notification model.
- [x] Notification center.
- [x] Mark read.
- [x] Mark all read.
- [x] Role-targeted notifications.

## Events
- [x] Appointment upcoming.
- [x] Appointment cancelled.
- [x] Lab result ready.
- [x] Critical lab result.
- [x] Pharmacy low stock.
- [x] Expiry alert.
- [x] Outstanding workflow reminders. (unsigned consultations, unverified lab results, overdue ward tasks, unacknowledged handovers)

## External — Later
- [x] SMS provider adapter. (adapter interface only; no provider wired — messages logged as skipped)
- [x] WhatsApp provider adapter. (adapter interface only; no provider wired)
- [x] Email provider adapter. (Django SMTP settings)
- [x] Template system.
- [x] Delivery status. (MessageDelivery log; record_status() for provider receipts)

---

# PHASE 20 — Reports

## Report Engine
- [x] Date range filters.
- [x] Branch filters.
- [x] Department filters.
- [x] Doctor filters.
- [x] Export CSV.
- [x] Export XLSX.
- [x] Export PDF. (WeasyPrint, A4 landscape; without its native libraries the print page opens with "Print → Save as PDF")
- [x] Permission checks. (per report; branch filter only offers branches where the user holds the report permission)

## Clinical Reports
- [x] New patients.
- [x] Returning patients.
- [x] OPD visits.
- [x] Doctor-wise visits.
- [x] Department-wise visits.
- [x] Diagnosis count.

## Financial Reports
- [x] Daily collection. (built on billing day_totals: IPD deposits on the day taken, deposit returns as money out)
- [x] Revenue by department.
- [x] Revenue by doctor.
- [x] Payment methods.
- [x] Outstanding.
- [x] Discounts.
- [x] Refunds.

## Lab Reports
- [x] Test volume.
- [x] Lab revenue.
- [x] Pending tests.
- [x] TAT report.

## Pharmacy Reports
- [x] Sales.
- [x] Gross sales summary.
- [x] Product movement.
- [x] Current stock.
- [x] Low stock.
- [x] Expiry.
- [x] Purchase.

## IPD Reports
- [x] Admissions.
- [x] Discharges.
- [x] Occupancy.
- [x] Average stay.

---

# PHASE 21 — Dashboards & KPIs

## Admin
- [x] Today's patients.
- [x] Today's revenue.
- [x] OPD count.
- [x] Admission count.
- [x] Lab count.
- [x] Pharmacy sales.
- [x] Outstanding.
- [x] Bed occupancy.
- [x] Trends. (last 14 days: OPD visits and net collection)

## Lab
- [x] Pending samples.
- [x] Processing.
- [x] Verification pending.
- [x] Critical results.
- [x] Completed today.

## Pharmacy
- [x] Sales today.
- [x] Low stock.
- [x] Near expiry.
- [x] Out of stock.
- [x] Top products.

---

# PHASE 22 — Global Search

- [x] Search service.
- [x] Patient results.
- [x] Invoice results.
- [x] Appointment results.
- [x] Lab order results.
- [x] Medicine results.
- [x] Staff results.
- [x] Permission filtering.
- [x] Keyboard shortcut. (/ or Ctrl/Cmd+K; arrow keys move through results)
- [x] Recent searches optional. (per user, session only)

---

# PHASE 23 — Audit Trail

- [x] Audit log table.
- [x] Login events.
- [x] Patient create/update.
- [x] Clinical note changes. (encounter.note_save: field names only, coalesced per editor per 10 min)
- [x] Prescription changes. (line add/remove, advice, copy)
- [x] Lab result changes.
- [x] Result verification.
- [x] Invoice change.
- [x] Discount.
- [x] Refund.
- [x] Stock adjustment.
- [x] Permission changes.
- [x] AI acceptance event. (audit.record_ai_acceptance() ready; called when the AI assistant ships, Phase 15)
- [x] Admin audit viewer.
- [x] Filters by actor/entity/date/action.
- [x] Prevent normal users modifying logs. (no write screens; model/queryset guards; SQLite/PostgreSQL triggers refuse UPDATE/DELETE)

---

# PHASE 24 — Printing & PDF Templates

- [x] Shared print layout engine. (`templates/print/`: base + a4/a5/thermal layouts)
- [x] Hospital branding header. (`print/_letterhead.html`: logo, name, branch, address, phone, reg. no., header; footer + terms)
- [x] Token slip.
- [x] Registration slip.
- [x] Appointment slip. (`/appointments/<id>/slip/`)
- [x] Prescription.
- [x] Lab order. (`/lab/orders/<id>/print/`)
- [x] Lab report.
- [x] Invoice.
- [x] Pharmacy receipt.
- [x] Admission form.
- [x] Discharge summary.
- [x] A4 layouts.
- [x] Thermal receipt layouts where required.

---

# PHASE 25 — Settings

## General
- [x] Hospital settings.
- [x] Branch settings.
- [x] Numbering prefixes. (MRN and invoice prefixes; other numbers use fixed APT-/LAB-/ADM- formats)
- [x] Timezone.
- [x] Currency.
- [x] Logo.
- [x] Print footer. (footer plus new printed terms and conditions)

## OPD
- [x] Consultation duration. (per doctor; hospital default in Settings → Preferences)
- [x] Follow-up rules. (per doctor; hospital default in Settings → Preferences)
- [x] Token format. (token letters per department)
- [x] Queue settings. (display refresh, doctor names on display)

## Billing
- [x] Payment methods.
- [x] Discount rules. (largest discount %, enforced in billing and POS; permission + reason)
- [x] Tax settings.
- [x] Receipt settings. (receipt message; printed terms)

## Pharmacy
- [x] Reorder defaults. (default for new products)
- [x] Expiry alert threshold. (Settings → Preferences, default 90 days)
- [x] Negative stock policy. (never allowed: service check + database constraint; documented on Preferences)

## AI
- [ ] AI enable/disable. (with Phase 15, AI deferred)
- [x] Feature toggles. (module switches for lab, pharmacy, IPD/nursing, enforced on the server; AI toggles come with Phase 15)
- [ ] Model configuration admin-only. (with Phase 15, AI deferred)
- [ ] Disclaimer configuration. (with Phase 15, AI deferred)

---

# PHASE 26 — Security Hardening 

- [x] Backend authorization review. (`apps/core/tests/test_url_inventory.py` checks every URL, anonymous and no-permission)
- [x] Tenant isolation tests.
- [x] Branch isolation tests.
- [x] Rate limiting. (sign-in lockout, password reset, setup, browser error reports)
- [x] CSRF strategy if cookie auth. (Django CSRF + HttpOnly cookie; token via page for htmx; Origin checked)
- [x] CORS configuration. (no CORS: no cross-site browser access; tested)
- [x] Secure cookies. (SEHATDESK_HTTPS switch: secure cookies, HSTS, https redirect)
- [x] Security headers. (CSP, Permissions-Policy, Referrer-Policy, COOP, nosniff, frame deny, no-store on signed-in pages)
- [x] SQL injection protections verified. (ORM only; one static health-check query)
- [x] XSS review. (auto-escaping everywhere; only static icon SVG marked safe)
- [x] File upload security. (size, extension and magic-byte checks; private storage; permission-checked serving)
- [x] Password policy.
- [x] Secrets management. (secret key generated into the data folder with owner-only permissions; backup key off-machine)
- [x] Dependency vulnerability scan. (pip-audit in CI; WeasyPrint 70 and pytest 9.0.3 upgrades fixed known CVEs)
- [x] Audit privileged actions.
- [x] Session invalidation. (password change, deactivation; tested)
- [x] Backup encryption where appropriate. (AES-256-GCM, SEHATDESK_BACKUP_KEY)
- [x] Sensitive logging review. (phone/CNIC/email redaction filter; tracebacks drop exception messages on deployed machines)

---

# PHASE 27 — Testing

## Unit Tests
- [x] Billing calculations.
- [x] Discount calculations.
- [x] Payment balance.
- [x] Refund logic.
- [x] Queue ordering.
- [x] Appointment slot logic.
- [x] Inventory stock movement.
- [x] FEFO.
- [x] Lab reference range logic.
- [x] RBAC helpers.

## Integration Tests
- [x] Register patient → appointment.
- [x] Appointment → check-in → token.
- [x] Token → consultation.
- [x] Consultation → prescription.
- [x] Consultation → lab.
- [x] Lab → billing → result.
- [x] Prescription → pharmacy sale.
- [x] Pharmacy sale → inventory.
- [x] Admission → bed occupancy.
- [x] Discharge → bed release.

## End-to-End
- [x] Reception user journey. (role journeys sign in through the real form and open every menu screen: `test_journeys.py`)
- [x] Doctor user journey.
- [x] Lab user journey.
- [x] Pharmacy user journey.
- [x] Admin user journey.
- [x] IPD journey.

## Security Tests
- [x] Cross-organization record access.
- [x] Cross-branch access.
- [x] Unauthorized endpoint access.
- [x] Privilege escalation attempts.
- [x] Direct record ID access.

---

# PHASE 28 — Performance

- [x] Add DB indexes for patient search.
- [x] Add DB indexes for appointment date/doctor.
- [x] Add indexes for queue.
- [x] Add indexes for invoices/payments.
- [x] Add indexes for lab worklist.
- [x] Add indexes for pharmacy stock.
- [x] Pagination for large lists. (products, admissions, purchases now paginated; others already were)
- [x] Avoid N+1 database queries.
- [x] Query profiling. (dashboard 279 → under 80 queries; N+1 guard tests on list pages)
- [x] Cache stable master data where useful. (preferences cached per request; measured, no cross-request cache needed at clinic scale)
- [x] Load test reception search. (`manage.py loadtest`: 20k patients, search ~12–18 ms median)
- [x] Load test POS checkout. (~10 ms median)
- [x] Load test dashboard queries. (~70 ms median)

---

# PHASE 29 — Backup & Recovery

- [x] Automated DB backups. (serve.py, every SEHATDESK_BACKUP_HOURS; Settings → System → Back up now)
- [x] Backup retention. (SEHATDESK_BACKUP_KEEP, default 14)
- [x] Object storage backup policy.
- [x] Restore procedure. (`manage.py restore`, docs/BACKUP_AND_RECOVERY.md)
- [x] Test database restore. (restore drill in CI, plain and encrypted)
- [x] Disaster recovery documentation.
- [x] Production rollback procedure.

---

# PHASE 30 — Deployment (we will do this in future on demand)

## Staging
- [ ] Staging database.
- [ ] Staging storage.
- [ ] Staging domain.
- [ ] TLS.
- [ ] Seed demo users.
- [ ] Seed demo patients.
- [ ] Seed demo lab tests.
- [ ] Seed demo pharmacy stock.

## Production
- [ ] Production database.
- [ ] Production storage.
- [ ] Secrets.
- [ ] TLS/domain.
- [ ] Logging.
- [ ] Monitoring.
- [ ] Backups.
- [ ] Error alerts.
- [ ] Health checks.
- [ ] Deployment pipeline.

---

# PHASE 31 — Demo Data (we will do this in future on demand)

Create realistic demo data for:
- [ ] 1 hospital.
- [ ] 2 branches.
- [ ] 5 departments.
- [ ] 8 doctors.
- [ ] 4 reception/cash users.
- [ ] 2 lab users.
- [ ] 2 pharmacy users.
- [ ] 50 patients.
- [ ] 30 appointments.
- [ ] 20 completed consultations.
- [ ] 15 prescriptions.
- [ ] 20 lab orders.
- [ ] 50 pharmacy products.
- [ ] 100 stock batches/transactions.
- [ ] 20 invoices/payments.
- [ ] 10 IPD beds.
- [ ] 3 active admissions.

---

# PHASE 32 — Mobile Companion App (Later) (we will do this in future on demand)

Initial mobile scope should remain restricted to workflows that benefit from mobility.

## Doctor Mobile
- [ ] Login.
- [ ] Today's appointments.
- [ ] Queue.
- [ ] Patient summary.
- [ ] History.
- [ ] Lab results.
- [ ] Quick note.
- [ ] Prescription review.
- [ ] Notifications.

## Admin Mobile
- [ ] KPI dashboard.
- [ ] Revenue summary.
- [ ] Patient count.
- [ ] Lab/pharmacy status.
- [ ] Alerts.

## Nurse Mobile
- [ ] Assigned patients.
- [ ] Vitals.
- [ ] Nursing note.
- [ ] Medication task list.

---

# PHASE 33 — Billable Procedures (ECG, EEG, ECHO, Ultrasound, operations, dental)

Procedures are services marked "procedure" in Settings → Services. Adding one to a patient
issues an invoice line (kind "procedure") that reception collects like any other bill.

## Catalogue
- [x] Mark a service as a procedure (ECG, EEG, ECHO, Ultrasound, operation, dental work...).
- [x] "Price can be changed per patient" switch per procedure.
- [x] Default price + branch prices (reuses service pricing).
- [x] Procedure catalogue filter in Settings → Services. (Settings → Services → "Procedures only")

## Patient procedures
- [x] Patient procedure model (patient, visit, procedure, performed by, quantity, list price, charged price, status).
- [x] Procedure numbering (PRC-00001).
- [x] Add procedure from the consultation (doctor workspace).
- [x] Add procedure from the patient profile / procedures desk.
- [x] Change the price for this patient (needs permission + reason; list price kept).
- [x] Performing doctor recorded on the invoice line (drives commission).
- [x] Issues an invoice the patient pays at any billing desk.
- [x] Mark procedure done (with findings/notes).
- [x] Cancel procedure (voids the unpaid invoice; paid ones must be refunded first).
- [x] Procedures list (status/date filters) and detail.
- [x] Audit every order, price change, completion and cancellation.

## Permissions
- [x] procedure.view, procedure.order, procedure.perform, procedure.price.override.

## Tests
- [x] Price override needs permission and reason; non-editable procedures refuse it.
- [x] Invoice line carries kind "procedure" and performing doctor.
- [x] Cancel blocked when money is held.
- [x] Branch/organization isolation.

---

# PHASE 34 — Staff Attendance

## Data
- [x] Attendance record per staff per day (present, late, half day, absent, leave, off/holiday).
- [x] Check-in / check-out times.
- [x] Who marked it (self or admin) and a note.
- [x] Work start time + late grace minutes + weekly off day per staff.

## Self service
- [x] My attendance page: check in / check out button.
- [x] Late auto-detected from the staff start time and grace. (default 09:00 + 15 min without a pay profile)
- [x] My month at a glance.

## Admin
- [x] Daily register: every active staff member, mark status in one save.
- [x] Monthly sheet: staff × days grid with totals.
- [x] Attendance in a finalized payroll month is locked.
- [x] Audit attendance changes.

## Permissions
- [x] hr.self (all staff), hr.attendance.manage.

## Tests
- [x] Check-in twice is refused; check-out before check-in refused.
- [x] Late detection.
- [x] Locked after payroll finalization.
- [x] Branch isolation.

---

# PHASE 35 — Staff Pay, Doctor Commission & Payroll

## Pay profiles
- [x] Pay profile per staff: monthly salary / daily wage / commission only.
- [x] Deduct absences from salary (on/off).
- [x] Doctor commission with the clinic: % of consultation, % of procedures, % of lab referrals.
- [x] Per-procedure commission override (percent or fixed amount).
- [x] Changes audit-logged; earned commission keeps the rate it was earned at.

## Commission earnings
- [x] Earned automatically when an invoice line with a doctor is fully paid. (partly paid bills earn nothing yet)
- [x] Reversed automatically on refund / cancellation (proportional for partial refunds).
- [x] Doctor share vs clinic share per line.
- [x] My earnings page for doctors (unsettled + settled).

## Payroll
- [x] Monthly payroll run per branch (draft → finalized).
- [x] Payslip per staff: base, attendance days, absence deduction, commission, bonus, other deduction, net.
- [x] Edit bonus/deduction on drafts; recalculate.
- [x] Finalize posts the staff ledger and settles commissions (transactional).
- [x] Print payslip.

## Staff ledger
- [x] Append-only ledger per staff (salary, commission, bonus, deduction, advance, payment, adjustment).
- [x] Pay staff (salary payout or advance) with payment method.
- [x] Balance owed to each staff member.

## Permissions
- [x] hr.pay.manage.

## Tests
- [x] Commission earned only when fully paid; reversal on refund; idempotent.
- [x] Payslip math (absence deduction, commission, bonus).
- [x] Finalize is transactional and only once.
- [x] Staff ledger balance.

---

# PHASE 36 — Expenses, Ledgers & Profit/Loss Dashboard

Segments used everywhere: Hospital (system), Doctors/OPD, Laboratory, Pharmacy.

## Expenses
- [x] Expense categories (seeded defaults, editable).
- [x] Record expense (date, category, segment, amount, method, payee, reference).
- [x] Expense numbering (EXP-00001); double-submit safe.
- [x] Void expense with reason (never deleted).
- [x] Expense list with filters and totals.
- [x] Pay supplier (posts to the supplier ledger).

## Ledgers
- [x] Complete cash ledger: every money movement (payments, refunds, deposits, expenses, staff pay, supplier pay) with running balance.
- [x] Ledger by segment: Pharmacy, Doctors, Laboratory, Hospital (system).
- [x] Ledger per staff (Phase 35) and per doctor (commission + payouts).
- [x] Supplier ledger link (Phase 14).
- [x] CSV export.

## Profit & loss
- [x] Income by segment (issued invoices, discounts allocated, refunds deducted). (accrual, before tax)
- [x] Pharmacy cost of goods sold (from stock movements) and write-offs.
- [x] Staff salaries + doctor commission. (salaries from finalized payroll only)
- [x] Expenses by category and segment.
- [x] Net profit per segment and overall.

## Admin finance dashboard
- [x] Period picker (today, this month, last month, custom) and branch / all branches.
- [x] Net income, expenses, profit, collections, outstanding.
- [x] 12-month income vs expense trend.
- [x] Top expense categories.
- [x] Doctor share vs clinic share.

## Permissions
- [x] finance.view, finance.expense.manage.

## Tests
- [x] P&L math across segments.
- [x] Ledger running balance.
- [x] Void excluded from totals.
- [x] Isolation and permission checks.

---

# MVP CUT LINE

The first customer-testable MVP should include only:

- [ ] Authentication
- [ ] Organization + branch
- [ ] Staff + RBAC
- [ ] Departments/doctors/services
- [ ] Patient registration/search/profile
- [ ] Appointments
- [ ] Reception dashboard
- [ ] Token queue
- [ ] Billing + payments
- [ ] Doctor dashboard
- [ ] OPD consultation
- [ ] Vitals
- [ ] Diagnosis
- [ ] Prescription
- [ ] Lab orders/results/basic reporting
- [ ] Pharmacy product/batch/inventory
- [ ] Pharmacy POS
- [ ] Patient timeline/history
- [ ] Core print documents
- [ ] Audit logs
- [ ] Basic admin reports
- [ ] AI patient summary + note drafting
- [ ] Staging + backup + security review

Do **not** delay MVP for:
- Advanced IPD
- Patient mobile app
- Insurance
- PACS
- Full accounting
- Telemedicine
- Complex AI diagnostics
- External device integrations

---

# RECOMMENDED SPRINT ORDER

## Sprint 1
- [ ] Foundation
- [ ] App shell
- [ ] Auth
- [ ] Organization
- [ ] RBAC

## Sprint 2
- [ ] Master data
- [ ] Patients
- [ ] Patient search/profile

## Sprint 3
- [ ] Appointments
- [ ] Reception
- [ ] Token queues

## Sprint 4
- [ ] Billing
- [ ] Payments
- [ ] Receipts

## Sprint 5
- [ ] OPD
- [ ] Vitals
- [ ] Diagnosis
- [ ] Prescription

## Sprint 6
- [ ] Lab

## Sprint 7
- [ ] Pharmacy inventory
- [ ] POS

## Sprint 8
- [ ] Patient history
- [ ] Reports
- [ ] Audit
- [ ] Printing

## Sprint 9
- [ ] AI assistant
- [ ] Safety/permissions
- [ ] AI logs

## Sprint 10
- [ ] QA
- [ ] Security
- [ ] Performance
- [ ] Production readiness

## Post-MVP
- [ ] IPD
- [ ] Nursing
- [ ] WhatsApp/SMS
- [ ] Advanced reports
- [ ] Mobile app

---

# ENGINEERING RULES

- [ ] Every API endpoint must enforce organization scope.
- [ ] Every sensitive endpoint must enforce permission checks.
- [ ] Financial writes must be transactional.
- [ ] Inventory writes must be transactional.
- [ ] Clinical records should be versioned/corrected rather than silently overwritten.
- [ ] No patient data in normal debug logs.
- [ ] AI output never auto-saves into final clinical record.
- [ ] Never rely on frontend authorization only.
- [ ] Every list screen must support loading, empty, error, and pagination states.
- [ ] Every major financial/clinical action must produce an audit event.
- [ ] Add tests with each business-critical workflow, not at the end.

---

# FINAL RELEASE CHECKLIST

- [ ] All MVP acceptance criteria pass.
- [ ] Critical bugs = 0.
- [ ] High-severity security findings = 0.
- [ ] Tenant isolation verified.
- [ ] Backup restore tested.
- [ ] Production monitoring active.
- [ ] Admin onboarding documented.
- [ ] Reception workflow documented.
- [ ] Doctor workflow documented.
- [ ] Lab workflow documented.
- [ ] Pharmacy workflow documented.
- [ ] Privacy/AI disclaimer configured.
- [ ] Production seed/configuration reviewed.
- [ ] Go-live checklist signed off.
