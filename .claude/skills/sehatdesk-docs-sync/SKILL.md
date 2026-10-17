---
name: sehatdesk-docs-sync
description: Checklist of which SehatDesk docs and Claude guide files must be updated after a change (CLAUDE.md files, TODO_TASKS.md, PROJECT_NOTES.md, BACKUP_AND_RECOVERY.md, env examples, skills). Use at the end of every change, before committing.
---

# Keep docs and guides in sync

Go through the diff (`git diff --stat` and the changed files) and apply every row that matches.
Updates go in the same commit as the code.

| If the change... | Update |
|---|---|
| Adds, renames or removes an entry point (service function, view module, command) of an app | `apps/<app>/CLAUDE.md` → Files |
| Changes a status flow, money/stock/clinical rule, or validation an app enforces | `apps/<app>/CLAUDE.md` → Rules |
| Changes which permission codes an app checks | `apps/<app>/CLAUDE.md` → Permissions |
| Adds a signal or cross-app call | `apps/<app>/CLAUDE.md` of both apps → Connected |
| Adds a test file | `apps/<app>/CLAUDE.md` → Tests |
| Creates a new app | new `apps/<app>/CLAUDE.md` (same sections + "Keep this file current"); add to the index in `apps/CLAUDE.md` |
| Changes a convention every app follows (base models, scoping, audit, test fixtures) | `apps/CLAUDE.md` |
| Adds a layout, component, template tag, CSS class or token | `templates/CLAUDE.md` |
| Completes a checklist item | `TODO_TASKS.md`: `- [x]` plus a short note of where it lives |
| Makes or changes an architecture, deployment, installer, network or environment decision | `docs/PROJECT_NOTES.md` (right §, replace stale text) + a line in the §6 decision log; index line in `docs/CLAUDE.md` if a new § is added |
| Changes backup/restore behaviour or what is backed up | `docs/BACKUP_AND_RECOVERY.md` and PROJECT_NOTES §21 |
| Adds or changes an environment variable | `deploy/*.env.example` + `deploy/CLAUDE.md` |
| Changes a model or service the demo uses | `apps/core/demo_data.py` / `seed_demo.py`; run `test_seed_demo.py` |
| Changes how a procedure in a skill is done (commands, steps, file locations) | that `.claude/skills/<skill>/SKILL.md` |
| Introduces a rule that applies to all code | root `CLAUDE.md` (keep it short; details go in the area file) |

Rules for these files:
- Keep each `CLAUDE.md` short: what the code is, where things are, the rules. No change history.
- Replace stale statements instead of appending. Name real files and functions only; check they
  exist after a rename.
- Never write passwords, keys or real patient data into any doc.

Finish by listing which of these files you updated (or "none needed" with the reason).
