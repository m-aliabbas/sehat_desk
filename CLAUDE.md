# SehatDesk — Working Rules (read first)

This file holds only the rules that apply everywhere. Details live next to the code in smaller
`CLAUDE.md` files that load on their own when you work in that folder (see "Where to look").
Do not paste their content back here.

## Environment DEV ALI LATOP
- ALWAYS run Python, pip, Django and build commands inside the conda env:
  `conda activate agent_dev311`
  Never use the system Python or a plain venv for this project. If `conda activate` is unavailable
  in a non-interactive shell, call the env's interpreter directly
  (`/opt/miniconda3/envs/agent_dev311/bin/python manage.py ...`); that is the same environment.

## Product basics
- SehatDesk is a Django (single-language, Python) hospital management system.
- Database: SQLite for now (see docs/PROJECT_NOTES.md). Keep ORM code portable to PostgreSQL later.
- Frontend: Django templates + HTMX + Alpine.js + Tailwind, styled with the design tokens in
  ui_design_reference/SehatDesk-Expanded-Workflow-Kit/03-handoff/DESIGN-TOKENS.json.
- Source of truth: PRD.md (requirements) and TODO_TASKS.md (build checklist, complete in phase order).
  Both are long: open only the section you need (search for `# PHASE <n>` or the PRD § number).
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

## Where to look (load only what the task needs)
| Topic | File |
|---|---|
| Conventions every app shares (models, services, scoping, permissions, audit, tests) | `apps/CLAUDE.md` |
| One module (billing, lab, pharmacy, ipd, ...) | `apps/<app>/CLAUDE.md` |
| Templates, components, HTMX/Alpine, Tailwind, design tokens | `templates/CLAUDE.md` |
| Which doc holds which decision | `docs/CLAUDE.md` |
| Environment files (staging, production) | `deploy/CLAUDE.md` |
| Architecture and deployment decisions, by section | `docs/PROJECT_NOTES.md` (read one § at a time) |
| Backup and restore procedures | `docs/BACKUP_AND_RECOVERY.md` |

Skills in `.claude/skills/` hold step-by-step procedures: `sehatdesk-checks`, `sehatdesk-phase`,
`sehatdesk-new-screen`, `sehatdesk-permission`, `sehatdesk-model-change`, `sehatdesk-demo-data`,
`sehatdesk-docs-sync`.

## Keep these files current (part of every change, not a follow-up)
Before finishing any change, update the files it affects in the same commit. The
`sehatdesk-docs-sync` skill has the full checklist; the short form:

| When you change... | Also update |
|---|---|
| Anything in `apps/<app>/` that changes its entry points, flow or rules | `apps/<app>/CLAUDE.md` |
| A rule shared by every app (base models, scoping, audit, test pattern) | `apps/CLAUDE.md` |
| Add a new app | create `apps/<app>/CLAUDE.md` and add it to the list in `apps/CLAUDE.md` |
| Templates, components, tokens, CSS build | `templates/CLAUDE.md` |
| A phase checklist item | tick it in `TODO_TASKS.md` with a one-line note of where it lives |
| An architecture, deployment, installer or network decision | `docs/PROJECT_NOTES.md` (right §) + decision log |
| Backup or restore behaviour | `docs/BACKUP_AND_RECOVERY.md` |
| Permission codes or default roles | `apps/accounts/CLAUDE.md` if the rules change; nothing else (the catalogue is the code) |
| Models a demo flow touches | run `seed_demo` tests; adjust `apps/core/demo_data.py` / `seed_demo.py` |
| Env settings | `deploy/*.env.example` and `deploy/CLAUDE.md` |
| How a procedure in a skill is done | that skill's `SKILL.md` |
| A rule that applies everywhere | this file (keep it short) |
