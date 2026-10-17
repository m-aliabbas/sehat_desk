# deploy/ — environment files

`staging.env.example` and `production.env.example` are templates. Copy one (never commit the copy:
`deploy/*.env` is ignored), fill in the secret, and point `SEHATDESK_ENV_FILE` at it. Settings are
read in `config/settings.py` with django-environ; defaults there are for development.

- `SEHATDESK_ENV`: `development` | `staging` | `production` (PROJECT_NOTES §15). Staging and
  production refuse unsafe settings (debug, missing secret, wildcard hosts).
- Staging: its own data folder and port 8001, a banner on every page, external messages off unless
  `SEHATDESK_STAGING_SEND_MESSAGES=true`, demo data only (`manage.py seed_demo`).
- Production: hospital LAN at http://sehatdesk.local; the installer writes the real env file and
  secret key into the data folder (PROJECT_NOTES §3, §4). Before go-live:
  `SEHATDESK_ENV_FILE=... python manage.py check --deploy --fail-level WARNING`.
- Backups: `SEHATDESK_BACKUP_HOURS`, `SEHATDESK_BACKUP_KEEP`, `SEHATDESK_BACKUP_KEY`
  (docs/BACKUP_AND_RECOVERY.md).
- Docker/PostgreSQL (`Dockerfile`, `docker-compose.yml`) are for staging, demos and hosted servers;
  hospitals get the installers (PROJECT_NOTES §16).

## Keep this file current
When you add or rename a setting read from the environment: add it to the matching
`*.env.example` with a comment, note it here if it changes behaviour, and record the decision in
`docs/PROJECT_NOTES.md`.
