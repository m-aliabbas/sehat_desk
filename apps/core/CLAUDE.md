# apps/core — Shared infrastructure

Base models, scoping, the dashboard, navigation, observability, security headers, backups and
management commands.

## Files
- `models.py` — `TimeStampedModel`, `ArchivableModel`, `Sequence` + `next_number`, `ErrorReport`.
- `scoping.py` — `OrgScopedModel`, `BranchScopedModel`, `scoped()`, `get_scoped_or_404`, `paginate`.
- `db.py` — `critical_write`, `locked`, `after_commit` for money/stock writes.
- `navigation.py` — sidebar items (`NavItem`: key, label, icon, url_name, permissions).
- `features.py` — module switches (lab, pharmacy, ipd) for nav and URL namespaces.
- `observability.py`, `monitoring.py` — request IDs, JSON logs, PHI redaction, error reports.
- `security.py` — CSP and headers (no external hosts). `icons.py` — inline Lucide SVGs.
- `backup.py` — backup/restore (docs/BACKUP_AND_RECOVERY.md).
- `demo_data.py` — catalogue for `seed_demo`.
- `management/commands/` — `backup`, `restore`, `tailwind`, `loadtest`, `seed_demo`.

## Rules
- Anything every app needs goes here; app-specific logic does not.
- New nav entry → `navigation.py`; if it belongs to lab/pharmacy/IPD also `features.py`.
- New icon → paste the Lucide inner SVG into `icons.py` (no icon fonts, no CDN).

## Tests
Project-wide guards live in `tests/` (url inventory, isolation, journeys, security, backup,
query scaling, seed_demo). Run all of them before finishing a cross-cutting change.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
