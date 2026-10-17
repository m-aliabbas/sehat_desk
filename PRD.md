# SehatDesk — Product Requirements Document (PRD)

**Product:** SehatDesk  
**Category:** AI-enabled Hospital Management System (HMS)  
**Document Version:** 1.0  
**Status:** Development Ready Draft  
**Primary Platforms:** Web App first, mobile companion later  
**Primary Market:** Clinics, labs, pharmacies, small-to-medium hospitals in Pakistan  
**Product Goal:** Replace fragmented hospital workflows with one simple, role-based, AI-assisted operating system for patient care and hospital administration.

---

## 1. Product Vision

SehatDesk is an AI-enabled Hospital Management System that centralizes patient registration, token/queue management, appointments, OPD/IPD workflows, doctor consultation, laboratory operations, pharmacy/POS, invoicing, payments, patient history, reporting, and AI-assisted clinical productivity.

The product should be fast enough for reception counters, simple enough for non-technical staff, structured enough for hospital administration, and useful enough for doctors to reduce repetitive documentation work.

SehatDesk should support both small clinics and multi-department hospitals without forcing every customer to enable every module.

---

## 2. Product Principles

1. **Patient-first workflow**
   - Patient data should not be entered repeatedly.
   - Previous history must be quickly available to authorized users.

2. **Fast front-desk operation**
   - Common reception actions should require minimum clicks.
   - Token generation and patient lookup should be optimized for keyboard-heavy use.

3. **Role-based simplicity**
   - Users only see screens and actions relevant to their role.

4. **Auditability**
   - Important patient, financial, inventory, and clinical changes must be traceable.

5. **AI assists; humans decide**
   - AI can summarize, suggest, draft, and highlight.
   - AI must not autonomously diagnose, prescribe, or finalize clinical decisions.

6. **Modular architecture**
   - Clinics can use only OPD + billing.
   - Hospitals can enable IPD, lab, pharmacy, inventory, etc.

7. **Pakistan-friendly**
   - PKR currency.
   - CNIC/passport support.
   - Local phone formats.
   - Urdu/English UI readiness.
   - Printed receipts/prescriptions/reports.

---

## 3. Target Users

### 3.1 Super Admin
- Configure organization/hospital.
- Manage branches.
- Manage modules and permissions.
- Access global reports and audit logs.

### 3.2 Hospital Admin / Manager
- Manage departments, doctors, users, services, rates.
- Monitor daily operations and reports.
- View revenue, patient count, bed occupancy, lab/pharmacy summaries.

### 3.3 Receptionist
- Register/search patients.
- Create appointments.
- Generate tokens.
- Check doctor availability.
- Collect registration/consultation payments.
- Print receipts/token slips.

### 3.4 Doctor
- View patient queue.
- Review patient history.
- Record complaints, vitals, diagnosis, notes.
- Add prescriptions.
- Request lab tests.
- Recommend admission.
- Use AI assistant for summarization and documentation support.

### 3.5 Nurse
- Record vitals.
- Update nursing notes.
- Track admitted patients.
- Record medication administration where enabled.

### 3.6 Lab Reception / Technician
- Receive lab orders.
- Collect sample information.
- Update test status.
- Enter results.
- Validate/print reports depending on permissions.

### 3.7 Pharmacist / Pharmacy Cashier
- Process prescriptions.
- Sell OTC/non-prescription products.
- Manage stock/batches/expiry.
- Handle returns.
- Print invoices.

### 3.8 Cashier / Accounts User
- Collect payments.
- Handle refunds/discounts based on permission.
- View shift/day closing.
- Access financial reports.

### 3.9 Patient (Future / optional portal)
- View appointments.
- View prescriptions.
- Download lab reports.
- View invoices.
- Receive notifications.

---

## 4. Scope

### 4.1 MVP Modules
- Authentication and role-based access control
- Hospital/branch configuration
- User/staff management
- Patient registration and profile
- Appointment management
- Token/queue management
- Reception dashboard
- Doctor/OPD consultation
- Vitals
- Prescription management
- Lab test ordering and basic lab workflow
- Pharmacy POS and basic inventory
- Billing/invoicing/payments
- Patient medical history
- Operational dashboards and basic reports
- Audit logs
- Basic notification framework
- AI clinical documentation assistant with strict guardrails

### 4.2 Phase 2 Modules
- Full IPD/admission workflow
- Bed/room/ward management
- Nursing module
- Advanced lab workflows
- Pharmacy purchase/vendor management
- Insurance/corporate billing
- Doctor scheduling and leave management
- WhatsApp/SMS notifications
- Expense management
- Advanced analytics
- Patient portal
- Mobile companion app
- Multi-branch consolidation

### 4.3 Future Scope
- Telemedicine
- PACS/radiology integration
- Device integrations
- Claims integration
- HL7/FHIR interoperability
- Accounting software integrations
- Voice dictation
- AI coding assistance for ICD/CPT-like workflows where applicable
- Predictive operational analytics

---

# 5. Core Workflows

## 5.1 Patient Registration

### New Patient
1. Search first to prevent duplicate registration.
2. Capture:
   - Full name
   - Gender
   - Date of birth / age
   - Mobile number
   - CNIC / passport / guardian CNIC
   - Address
   - Emergency contact
   - Blood group
   - Optional photo
3. Generate unique MRN/patient ID.
4. Save patient profile.
5. Continue to appointment/token/visit.

### Existing Patient
1. Search by:
   - MRN
   - Name
   - Phone
   - CNIC
2. Open patient profile.
3. Show latest visits and alerts.
4. Start new encounter.

### Duplicate Detection
System should warn when phone/CNIC/name+DOB closely matches an existing record.

---

## 5.2 Appointment Workflow

### Appointment fields
- Patient
- Doctor
- Department
- Date
- Time slot
- Visit type
- Reason
- Fee
- Status

### Appointment statuses
- Scheduled
- Confirmed
- Checked In
- In Queue
- With Doctor
- Completed
- Cancelled
- No Show

### Requirements
- Doctor availability calendar.
- Slot duration configurable.
- Emergency/walk-in support.
- Appointment rescheduling.
- Cancellation reason.
- Appointment notes.

---

## 5.3 Token / Queue Management

### Token generation
Token should be generated after check-in or walk-in registration.

Token fields:
- Token number
- Patient
- Doctor
- Department
- Queue timestamp
- Priority
- Status

### Queue statuses
- Waiting
- Called
- In Consultation
- Skipped
- Completed
- Cancelled

### Priority
- Normal
- Urgent
- Emergency
- Special priority if authorized

### UX
Reception dashboard should show:
- Current token
- Waiting count
- Average waiting time
- Doctor status
- Queue list

Doctor dashboard should show:
- Current patient
- Next patient
- Waiting queue
- Quick call/skip/complete controls

---

# 6. OPD / Doctor Consultation Module

## 6.1 Consultation Workspace

The doctor's patient screen should present all important information without excessive navigation.

### Patient header
- Name
- Age / DOB
- Gender
- MRN
- Allergies
- Known conditions
- Current visit reason
- Vitals
- Last visit date

### Consultation sections
- Chief complaint
- History of present illness
- Past medical history
- Surgical history
- Family history
- Allergies
- Current medicines
- Vitals
- Examination notes
- Provisional diagnosis
- Final diagnosis
- Treatment plan
- Prescription
- Lab/radiology orders
- Follow-up date
- Referral
- Admission recommendation
- Doctor notes

### Save behavior
Support:
- Draft
- Complete consultation
- Reopen with permission
- Auto-save

---

## 6.2 Vitals

Capture:
- Temperature
- Blood pressure
- Pulse
- Respiratory rate
- Oxygen saturation
- Weight
- Height
- BMI
- Blood glucose
- Optional custom vital fields

Display trends when historical values exist.

---

## 6.3 Prescription

Prescription builder should support:
- Search medicine
- Generic/brand name
- Strength
- Dosage
- Route
- Frequency
- Duration
- Quantity
- Instructions
- Before/after food
- Notes

Functions:
- Reuse previous medicines.
- Favorite prescription templates.
- Print prescription.
- Send prescription to internal pharmacy.
- Warn on allergy conflicts where structured information is available.
- AI may suggest formatting/draft text but doctor confirms final prescription.

---

# 7. AI Assistant

## 7.1 Purpose

AI should reduce documentation time and improve information retrieval without replacing clinician judgment.

## 7.2 MVP AI Features

### A. Patient History Summary
Generate a concise summary from:
- Previous visits
- Diagnoses
- Medicines
- Lab results
- Admissions

### B. Doctor Note Drafting
Convert structured consultation inputs into a clean SOAP-style or custom clinical note.

### C. Consultation Summary
Draft:
- Summary
- Follow-up instructions
- Patient-friendly explanation

### D. Suggested Follow-up Questions
Based on current complaint/history, suggest questions the doctor may want to ask.

### E. Record Search Assistant
Examples:
- “Show latest HbA1c.”
- “What medicines were prescribed in last visit?”
- “Summarize previous admissions.”

## 7.3 Safety Requirements

- AI outputs clearly labeled as AI-generated.
- Doctor must review before saving.
- No autonomous diagnosis.
- No autonomous prescription.
- No silent modification of records.
- Log AI interactions associated with patient record.
- Do not expose data across organizations.
- Configurable AI enable/disable by hospital.
- Avoid AI recommendations when required patient context is unavailable.
- Display safety disclaimer on clinical suggestion surfaces.

---

# 8. Laboratory Module

## 8.1 Lab Test Catalog

Each test:
- Test code
- Test name
- Category
- Sample type
- Price
- Expected turnaround time
- Reference ranges
- Units
- Active/inactive status

Panels/packages supported.

## 8.2 Lab Order Workflow

1. Doctor or lab desk creates order.
2. Invoice/payment if applicable.
3. Sample collection.
4. Sample marked collected.
5. Processing.
6. Result entry.
7. Validation/approval.
8. Report issued.
9. Patient/doctor notified.

### Statuses
- Ordered
- Payment Pending
- Paid
- Sample Pending
- Sample Collected
- Processing
- Result Entered
- Verified
- Released
- Cancelled

## 8.3 Lab Result Entry

Support:
- Numeric
- Text
- Positive/negative
- Dropdown
- Multi-line interpretation
- Attachment

Flag:
- High
- Low
- Critical

Critical result workflow should visibly alert permitted clinical users.

## 8.4 Lab Report
Include:
- Hospital details
- Patient
- MRN
- Ordering doctor
- Sample date
- Result date
- Tests/results
- Reference range
- Technician/pathologist
- QR/report verification future-ready
- Printable PDF layout

---

# 9. Pharmacy & POS

## 9.1 Medicine / Product Master

Fields:
- SKU
- Barcode
- Brand name
- Generic name
- Manufacturer
- Category
- Unit
- Pack size
- Purchase price
- Sale price
- Tax where applicable
- Reorder level

## 9.2 Batch Management

Track:
- Batch number
- Expiry date
- Purchase quantity
- Available quantity
- Purchase cost
- Sale price

System should follow FEFO (First Expiry, First Out) recommendation.

## 9.3 Pharmacy POS

Features:
- Search by medicine / barcode
- Prescription-linked sale
- Walk-in sale
- Quantity adjustment
- Discount with permission
- Multiple payment methods
- Print receipt
- Return/refund
- Hold/resume cart
- Customer/patient attach
- Expiry warning
- Low stock warning

## 9.4 Pharmacy Inventory

Transactions:
- Purchase
- Sale
- Sale return
- Purchase return
- Stock adjustment
- Damage/expired stock
- Transfer (future multi-branch)

---

# 10. IPD / Admission Module

## 10.1 Admission

Capture:
- Patient
- Admitting doctor
- Department
- Admission reason
- Diagnosis
- Ward
- Room
- Bed
- Admission date/time
- Attendant details
- Deposit
- Notes

## 10.2 Bed Management

Hierarchy:
- Branch
- Ward
- Room
- Bed

Bed statuses:
- Available
- Occupied
- Reserved
- Cleaning
- Maintenance
- Blocked

## 10.3 Inpatient Record

Include:
- Doctor rounds
- Nursing notes
- Vitals chart
- Medication chart
- Lab orders
- Procedures
- Service charges
- Pharmacy usage
- Diet/other service entries
- Transfers
- Discharge planning

## 10.4 Discharge

Generate discharge summary:
- Admission reason
- Diagnoses
- Major findings
- Treatments
- Procedures
- Discharge medicines
- Follow-up
- Final bill status

---

# 11. Billing & Payments

## 11.1 Billable Items

- Registration
- Consultation
- Lab
- Procedure
- Room/bed
- Pharmacy
- Misc services
- Packages

## 11.2 Invoice

Fields:
- Invoice number
- Patient
- Visit/admission
- Line items
- Quantity
- Unit price
- Discount
- Tax
- Grand total
- Paid
- Outstanding
- Payment status

## 11.3 Payment Methods

Configurable:
- Cash
- Card
- Bank transfer
- Easypaisa
- JazzCash
- Other

## 11.4 Discounts

Permission-based:
- Line discount
- Invoice discount
- Discount reason
- Authorized by

## 11.5 Refunds

Require:
- Original invoice
- Refund amount/item
- Reason
- Authorized user
- Audit log

---

# 12. Patient Medical Record

Patient profile should provide a longitudinal timeline containing:

- Registrations
- Appointments
- OPD visits
- Vitals
- Diagnoses
- Prescriptions
- Lab orders
- Lab reports
- Procedures
- Admissions
- Discharges
- Pharmacy transactions
- Invoices
- Payments
- Uploaded documents

Filters by date/type/doctor/department.

---

# 13. Dashboards

## 13.1 Reception Dashboard
- Today's appointments
- Walk-ins
- Waiting patients
- Doctors available
- Token queues
- Registration shortcuts

## 13.2 Doctor Dashboard
- Today's queue
- Upcoming appointments
- Pending notes
- Follow-ups
- Recent patient activity

## 13.3 Admin Dashboard
- Patient count
- OPD visits
- Admissions
- Revenue
- Outstanding amount
- Lab orders
- Pharmacy sales
- Bed occupancy
- Doctor performance metrics
- Trends

## 13.4 Lab Dashboard
- Pending samples
- Processing
- Awaiting verification
- Critical results
- Released today

## 13.5 Pharmacy Dashboard
- Sales today
- Low stock
- Expiring stock
- Out of stock
- Top-selling medicines
- Gross margin indicators where allowed

---

# 14. Reports

## MVP Reports

### Clinical / Patient
- New patients
- Returning patients
- OPD visits
- Doctor-wise patient count
- Department-wise visits
- Diagnosis report

### Financial
- Daily collection
- Invoice report
- Payment method report
- Outstanding balance
- Discount report
- Refund report

### Lab
- Test volume
- Revenue
- Pending tests
- Test-wise count

### Pharmacy
- Sales report
- Stock report
- Low stock
- Expiry report
- Purchase/sales movement

### IPD
- Admissions
- Discharges
- Bed occupancy
- Length of stay

Export:
- PDF
- CSV/XLSX

---

# 15. Notifications

Notification events:
- Appointment reminder
- Appointment changed/cancelled
- Token approaching
- Lab result ready
- Critical lab result
- Prescription ready
- Payment confirmation
- Admission/discharge
- Low stock
- Medicine expiry

Channels:
- In-app MVP
- SMS/WhatsApp/email later

---

# 16. Search

Global search should support:
- Patient name
- MRN
- Phone
- CNIC
- Invoice
- Appointment
- Lab order
- Medicine
- Staff

Search access must respect role permissions.

---

# 17. Role-Based Access Control (RBAC)

Permissions should be action-based.

Examples:
- patient.view
- patient.create
- patient.update
- consultation.view
- consultation.create
- prescription.create
- lab.order.create
- lab.result.enter
- lab.result.verify
- pharmacy.sale
- pharmacy.refund
- billing.discount
- billing.refund
- reports.financial.view
- admin.users.manage

Support:
- System roles
- Custom roles
- Per-branch scope
- Module permissions

---

# 18. Audit Logs

Audit:
- Login/logout
- Patient creation/edit
- Clinical note edit
- Prescription edit
- Result entry/edit/verification
- Invoice update
- Discount
- Refund
- Stock adjustment
- User/permission changes
- AI-generated action acceptance where appropriate

Log:
- Actor
- Action
- Entity
- Record ID
- Date/time
- Previous value where feasible
- New value where feasible
- IP/device metadata where available

Clinical records should avoid destructive deletion. Use correction/versioning where practical.

---

# 19. Documents & Printing

Printable templates:
- Patient registration slip
- Token slip
- Appointment slip
- Prescription
- Lab order
- Lab report
- Invoice
- Pharmacy receipt
- Admission form
- Discharge summary

Hospital admin should configure:
- Logo
- Hospital name
- Address
- Phone
- Footer
- Terms

---

# 20. Settings

## Organization
- Hospital name
- Logo
- Contact
- Address
- Registration details
- Currency
- Timezone
- Language

## Branch
- Branch details
- Departments
- Counters
- Working hours

## Clinical
- Departments
- Specialties
- Doctors
- Services
- Consultation fees
- Follow-up policy
- Prescription template

## Lab
- Test catalog
- Reference ranges
- Report configuration

## Pharmacy
- Categories
- Units
- Vendors
- Reorder settings

## Billing
- Payment methods
- Discount rules
- Invoice prefix
- Taxes

## Security
- Roles
- Permissions
- Session policies
- MFA future support

---

# 21. UX / UI Requirements

## Global layout
Desktop-first web application.

### Main layout
- Left sidebar
- Top search
- Notification center
- User/profile menu
- Context actions
- Breadcrumbs where required

### Suggested navigation
- Dashboard
- Reception
- Patients
- Appointments
- Queue
- OPD
- IPD
- Laboratory
- Pharmacy
- Billing
- Inventory
- Reports
- Staff
- Settings

### UI priorities
- Minimum clicks for frequent tasks
- Large readable patient identity block
- Clear status chips
- Keyboard shortcuts for reception/POS
- Confirmation for destructive/financial actions
- Responsive for tablets
- Accessibility-aware contrast and form labels

---

# 22. Key Screens

## Authentication
1. Login
2. Forgot password
3. Select branch if multi-branch
4. Role/workspace landing

## Admin
5. Admin dashboard
6. Staff list
7. Create/edit staff
8. Roles
9. Role permissions
10. Departments
11. Services
12. Branch settings
13. Organization settings
14. Audit logs

## Reception
15. Reception dashboard
16. Patient search
17. Register patient
18. Patient quick view
19. Appointment calendar
20. New appointment
21. Check-in
22. Queue/token dashboard
23. Payment/receipt

## Patient
24. Patient profile
25. Patient timeline
26. Documents
27. Visits
28. Prescriptions
29. Lab reports
30. Billing history

## Doctor
31. Doctor dashboard
32. Patient queue
33. Consultation workspace
34. Vitals
35. Diagnosis
36. Prescription builder
37. Lab order builder
38. Follow-up
39. AI assistant panel

## Lab
40. Lab dashboard
41. Orders
42. Sample collection
43. Worklist
44. Result entry
45. Result verification
46. Lab report
47. Test catalog

## Pharmacy
48. Pharmacy dashboard
49. POS
50. Prescription queue
51. Products
52. Batches
53. Stock
54. Purchases
55. Returns
56. Expiry
57. Low stock

## IPD
58. Bed board
59. Admission
60. Inpatient dashboard
61. Patient inpatient chart
62. Nursing notes
63. Doctor round
64. Medication chart
65. Transfer bed
66. Discharge
67. Discharge summary

## Billing
68. Invoice
69. Payment
70. Refund
71. Day closing
72. Outstanding patients

## Reports
73. Reports home
74. Financial
75. Clinical
76. Lab
77. Pharmacy
78. IPD
79. Export dialog

---

# 23. Data Model — High-Level

Core entities:

- Organization
- Branch
- User
- Staff
- Role
- Permission
- Department
- DoctorProfile
- Patient
- PatientIdentifier
- Appointment
- Encounter
- QueueToken
- Vital
- Diagnosis
- ClinicalNote
- Prescription
- PrescriptionItem
- LabTest
- LabOrder
- LabOrderItem
- LabResult
- PharmacyProduct
- InventoryBatch
- StockTransaction
- PharmacySale
- PharmacySaleItem
- Vendor
- Purchase
- Admission
- Ward
- Room
- Bed
- NursingNote
- MedicationAdministration
- Invoice
- InvoiceItem
- Payment
- Refund
- Document
- Notification
- AuditLog
- AIInteraction

All transactional records should include:
- organization_id
- branch_id where applicable
- created_at
- updated_at
- created_by where relevant

---

# 24. Suggested Technical Architecture

This section is a recommendation and can be adjusted to the team's preferred stack.

## Frontend
- Next.js / React
- TypeScript
- Component library + internal design system
- React Query/TanStack Query or equivalent
- Form validation with schema-based validation

## Backend
- NestJS / Node.js
- REST API initially
- Background jobs for notifications/reports/AI tasks

## Database
- PostgreSQL for production
- Strong relational model
- Database migrations
- Transaction support for billing/inventory

## Cache / Queue
- Redis when required
- Background job processor

## File storage
- S3-compatible object storage for:
  - reports
  - patient documents
  - attachments

## AI Service
Separate AI orchestration layer:
- prompt templates
- PHI-safe logging policy
- structured outputs
- model abstraction
- retry/fallback
- permission checks

## Deployment
- Dockerized services
- Staging + production environments
- Automated backups
- TLS
- Secrets management
- Centralized logging

---

# 25. API Domains

Suggested endpoint domains:

- `/auth`
- `/users`
- `/roles`
- `/branches`
- `/departments`
- `/patients`
- `/appointments`
- `/queue`
- `/encounters`
- `/vitals`
- `/prescriptions`
- `/lab`
- `/pharmacy`
- `/inventory`
- `/admissions`
- `/beds`
- `/billing`
- `/payments`
- `/reports`
- `/notifications`
- `/ai`
- `/audit`
- `/settings`

Use organization/branch scoping at backend authorization level, not only UI level.

---

# 26. Non-Functional Requirements

## Performance
- Common list pages target <2 sec under expected operational load.
- Patient search should feel near-instant.
- POS actions optimized for low latency.
- Long reports should run asynchronously when needed.

## Reliability
- Prevent duplicate billing transactions.
- Prevent negative inventory except explicit authorized override.
- Use transactions for critical financial/stock operations.
- Automatic database backups.

## Security
- Password hashing using modern algorithm.
- Role/permission enforcement backend-side.
- TLS.
- Secure session/token handling.
- Rate limiting.
- Input validation.
- File upload restrictions.
- Audit sensitive operations.
- Encrypt sensitive data at rest where appropriate.
- Organization-level tenant isolation.

## Privacy
- Minimum necessary access.
- No public patient URLs.
- No cross-tenant patient search.
- Log access to sensitive actions where practical.
- Configurable data retention.

## Availability
MVP target:
- Suitable for normal hospital business hours.
- Graceful error handling and recovery.

---

# 27. AI Privacy / Governance

- Patient data sent to AI must be minimized to what is required.
- Organization must be able to disable AI.
- Avoid training external models on customer patient data unless explicitly contracted and legally allowed.
- Store structured AI request metadata separately from unnecessary raw prompts where possible.
- AI output must identify source context used where practical.
- Clinical decisions remain clinician responsibility.

---

# 28. Success Metrics

## Operational
- Patient registration time
- Average reception processing time
- Average waiting time
- Consultation completion rate
- Lab turnaround time
- Pharmacy checkout time

## Product
- Daily active staff
- Active doctors
- Encounters/day
- Module adoption
- AI assistant usage
- Percentage of consultations completed digitally

## Financial
- Revenue captured through system
- Outstanding balance visibility
- Pharmacy stock loss reduction
- Expiry loss reduction

---

# 29. MVP Acceptance Criteria

The MVP is considered usable when:

1. Admin can create hospital configuration, branches, staff, roles, departments, doctors, services, and fees.
2. Reception can register/search a patient.
3. Reception can create/check-in an appointment or walk-in.
4. System can issue and manage a token.
5. Doctor can see queue and open the patient encounter.
6. Vitals can be recorded.
7. Doctor can complete consultation notes.
8. Doctor can create prescription.
9. Doctor can order lab tests.
10. Lab can process an order from collection to verified result.
11. Pharmacy can process prescription-linked and walk-in sales.
12. Pharmacy stock reduces correctly after sale.
13. Billing can create invoices and payments.
14. Patient profile shows longitudinal history.
15. Core reports are available.
16. Role-based access is enforced on backend and UI.
17. Sensitive actions appear in audit logs.
18. AI can summarize history and draft clinical documentation, with mandatory human review.
19. Core documents can be printed.
20. System is deployable to staging and production.

---

# 30. Release Plan

## Release 0 — Foundation
- Architecture
- Design system
- Auth
- RBAC
- Organization/branch
- Staff
- Database foundation

## Release 1 — Reception + Patient
- Patient registration
- Patient search
- Appointments
- Token queue
- Basic billing

## Release 2 — OPD
- Doctor dashboard
- Consultation
- Vitals
- Diagnosis
- Prescription
- Patient history

## Release 3 — Lab
- Catalog
- Orders
- Samples
- Results
- Reports

## Release 4 — Pharmacy
- Product master
- Batches
- Inventory
- POS
- Prescription integration

## Release 5 — AI
- Patient summary
- Note drafting
- Record Q&A
- Safety logging

## Release 6 — IPD
- Beds
- Admission
- Inpatient chart
- Nursing
- Discharge

## Release 7 — Reporting + Hardening
- Reports
- Audit
- Performance
- Security
- Backup/restore
- QA
- Deployment

---

# 31. Out of Scope for Initial MVP

Unless separately approved:
- Insurance claim automation
- PACS
- HL7/FHIR integrations
- Full accounting/GL
- Payroll
- Biometric attendance
- Telemedicine
- Patient-facing native mobile app
- Medical device integration
- Automated clinical diagnosis
- Automated prescribing
- Government integrations

---

# 32. Definition of Done

A feature is complete only when:
- UI is implemented.
- Backend/API is implemented.
- Permission checks are implemented.
- Validation and error states are handled.
- Audit requirement is considered.
- Automated tests cover critical logic.
- Manual QA passes.
- Responsive behavior is checked.
- Loading/empty/error states exist.
- Documentation is updated.
- Feature works in staging.

