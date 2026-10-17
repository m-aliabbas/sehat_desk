# apps/queue — Reception counters and OPD tokens

Phase 6. Tokens per branch, department and day (A-001...). PROJECT_NOTES §11.

## Files
- `services.py` — `issue_token`, `queue_for`, `waiting`, `current_for`, `call_next`, `call`,
  `recall`, `skip`, `requeue`, `start`, `complete`, `cancel`, `stats`.
- `views.py` — reception desk, walk-ins, doctor queue, counters, public waiting-room display.

## Rules
- waiting → called → in_consultation → completed (skip/requeue, cancel from any open state).
  The linked appointment follows.
- A doctor has at most one called/in-consultation token at a time.
- Special priority needs `priority.special`.
- The public display (`queue:display`) uses the branch `display_key`, shows token numbers only.

## Permissions
`queue.manage`, `priority.special`.

## Tests
`tests/test_queue.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
