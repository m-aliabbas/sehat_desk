# apps/notifications — Notifications

In-app notifications, time-based sweeps and the external message foundation (SMS/WhatsApp/email).
Phase 19.

## Files
- `services.py` — `notify(org, Event.X, text, branch=, users=, roles=, permission=, url=,
  dedupe_key=)`, `for_user`, `unread_count`, `mark_read`, `mark_all_read`.
- `signals.py` — event notifications raised on record changes (idempotent via `dedupe_key`).
- `sweeps.py` + `middleware.py` — time-based checks run during normal requests (no job runner).
- `channels.py` — `send_external` with message templates; providers come later.
- `models.py` — `Event` choices, `Notification` (one row per recipient), templates, delivery log.

## Rules
- Never put patient clinical detail in notification text beyond what the recipient's role sees.
- Use a `dedupe_key` so repeated saves notify once.
- Staging forces external messages off unless explicitly enabled.

## Tests
`tests/test_notifications.py` (sweeps are disabled in tests by a root conftest fixture).

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
