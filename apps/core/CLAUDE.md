# apps/core — Shared infrastructure

Base models, scoping, the dashboard, navigation, observability, security headers, backups and
management commands.

## Files
- `models.py` — `TimeStampedModel`, `ArchivableModel`, `Sequence` + `next_number`, `ErrorReport`.
- `scoping.py` — `OrgScopedModel`, `BranchScopedModel`, `scoped()`, `get_scoped_or_404`, `paginate`.
- `db.py` — `critical_write`, `locked`, `after_commit` for money/stock writes.
- `navigation.py` — sidebar items (`NavItem`: key, label, icon, url_name, permissions).
- `menu_hub.py` — the All menu page (`core:menu`): every screen grouped by category (`HUB_GROUPS`).
  Admins (`admin.organization.manage`) get the short `ADMIN_SIDEBAR`; a new screen they need goes here.
- `features.py` — module switches (lab, pharmacy, ipd, nursing) for nav and URL namespaces.
- `observability.py`, `monitoring.py` — request IDs, JSON logs, PHI redaction, error reports.
- `security.py` — CSP and headers (no external hosts). `icons.py` — inline Lucide SVGs.
- `pdf.py` — `weasyprint_html()`: loads WeasyPrint (adds Homebrew/MacPorts lib dirs on macOS, the bundled `gtk` folder on Windows),
  None if unavailable. Every PDF export uses it.
- `updates.py` — Settings → Updates (`core:updates`, `admin.organization.manage`): latest GitHub Release,
  background download with SHA-256 check, opening the installer. Status in `DATA_DIR/update_job.json`.
- `licensing.py` — device licence through Firestore (`k1.json.enc`): one month on first start, offline cache,
  `LicenseMiddleware` sends every page to `core:license` when there is none. `worker_loop` runs in `serve.py`. Only
  installed copies enforce it (`SEHATDESK_LICENSE_ENFORCED`). Days-left banner in `layouts/app.html`.
- `safe_migrate.py` — used on every start and restore: undoes a failed migration, refuses data from a
  newer version. `tests/test_safe_migrate.py` fails on any new migration that drops a field or table.
- `backup.py` — backup/restore (docs/BACKUP_AND_RECOVERY.md).
- `backup_jobs.py` — background backup, live restore (SQLite file, backup file or Drive),
  `RestoreMaintenanceMiddleware`. Status in `DATA_DIR/backup_job.json`.
- `gdrive.py` — Google Drive sign-in, upload at `SEHATDESK_GDRIVE_TIMES`, retention; `worker_loop` runs in
  `serve.py`. All of it lives on Settings → Backup (`core:backups`, `templates/core/backups.html`) and
  needs `admin.organization.manage`. The upload worker runs under `serve.py`, not `runserver`.
- `demo_data.py` — catalogue for `seed_demo`.
- `management/commands/` — `backup`, `restore`, `gdrive_backup`, `tailwind`, `loadtest`, `seed_demo`.

## Rules
- Anything every app needs goes here; app-specific logic does not.
- New nav entry → `navigation.py`; if it belongs to lab/pharmacy/IPD also `features.py`.
- New icon → paste the Lucide inner SVG into `icons.py` (no icon fonts, no CDN).

## Tests
Project-wide guards live in `tests/` (url inventory, isolation, journeys, security, backup,
query scaling, seed_demo). Run all of them before finishing a cross-cutting change.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
