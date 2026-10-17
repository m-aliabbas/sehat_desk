# SehatDesk — Working Rules (read first)

## Environment
- ALWAYS run Python, pip, Django and build commands inside the conda env:
  `conda activate agent_dev311`
  Never use the system Python or a plain venv for this project.

## Product basics
- SehatDesk is a Django (single-language, Python) hospital management system.
- Database: SQLite for now (see docs/PROJECT_NOTES.md). Keep ORM code portable to PostgreSQL later.
- Frontend: Django templates + HTMX + Alpine.js + Tailwind, styled with the design tokens in
  ui_design_reference/SehatDesk-Expanded-Workflow-Kit/03-handoff/DESIGN-TOKENS.json.
- Source of truth: PRD.md (requirements) and TODO_TASKS.md (build checklist, complete in phase order).
- Screen reference: ui_design_reference/.../03-handoff/PAGE-CHECKLIST.csv (134 screens).

## Deployment
- Customers get a Windows and a macOS installer. A desktop shortcut starts the local server and opens the browser.
- The app is served on the hospital LAN at http://sehatdesk.local
- Full installer and network details are in docs/PROJECT_NOTES.md. Keep that file up to date
  whenever a deployment or installer decision changes.

## Rules for code work
- Every API/view must enforce organization and branch scoping and permission checks on the backend.
- Financial and stock writes must be transactional. Clinical records are corrected by versioning, not overwritten.
- AI output is a draft only. Never auto-save AI output into clinical records.
- No patient data in normal debug logs.
