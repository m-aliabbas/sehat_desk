---
name: sehatdesk-phase
description: Work through a SehatDesk build phase or checklist item from TODO_TASKS.md end to end (read the PRD section, plan, build through services, test, tick the checklist, update docs). Use when asked to "work on PHASE N" or a TODO item.
---

# Work a phase from TODO_TASKS.md

1. **Read only what the phase needs.** In `TODO_TASKS.md` find `# PHASE <n>` and read that block.
   Find the matching PRD section in `PRD.md` (search the module name or §). Read the
   `apps/<app>/CLAUDE.md` of every app it touches, and `apps/CLAUDE.md`.
2. **Check phase order.** Phases are done in order; if the phase depends on unchecked items in an
   earlier phase, say so before starting.
3. **Find the screens.** Search `ui_design_reference/.../03-handoff/PAGE-CHECKLIST.csv` for the
   screens of this module; follow their layout with the existing components.
4. **Build through the layers:** models (skill `sehatdesk-model-change`), services that hold every
   state change, then views/templates (skill `sehatdesk-new-screen`), permissions (skill
   `sehatdesk-permission`). Follow the nearest existing app's patterns instead of inventing new ones.
5. **Tests:** service tests for each rule (scoping, permission refusal, transactional money/stock,
   versioned clinical corrections) and at least one view test per screen. Run skill
   `sehatdesk-checks`.
6. **Demo data:** if the phase adds a workflow worth showing, extend `seed_demo` (skill
   `sehatdesk-demo-data`).
7. **Close out** (skill `sehatdesk-docs-sync`): tick the items in `TODO_TASKS.md` with a short note
   of where each lives; update `apps/<app>/CLAUDE.md`; add a § or decision-log line to
   `docs/PROJECT_NOTES.md` for any design decision.
8. Summarise what was built, what was left unchecked and why.
