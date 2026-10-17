# SehatDesk — Backup, Restore and Disaster Recovery

For the hospital IT person or the SehatDesk support engineer. Keep a printed copy next to the
server, together with the backup key (see §2), in a locked place.

## 1. What is backed up

One backup file, `sehatdesk-<yyyymmdd-hhmmss>.zip` (`.zip.enc` when encrypted), contains:

| Inside the backup | What it is |
|---|---|
| `database.sqlite3` | All records: patients, visits, bills, stock, audit log. A consistent snapshot taken while staff keep working. |
| `private/documents/` | Uploaded patient documents (scans, reports). |
| `media/` | Hospital logo, patient photos, lab attachments. |
| `manifest.json` | When it was made, app migrations, a SHA-256 checksum for every file. |

**Not included:** the secret key (`secret.key`) and the logs. Restoring without the secret key
only signs everyone out and expires open document links; nothing is lost.

## 2. Settings

| Setting | Default | Meaning |
|---|---|---|
| `SEHATDESK_BACKUP_HOURS` | `24` | Automatic backup interval while the server runs. `0` turns automatic backups off. |
| `SEHATDESK_BACKUP_KEEP` | `14` | How many backups to keep; older ones are deleted after each new backup. |
| `SEHATDESK_BACKUP_DIR` | `<data folder>/backups` | Where backups are written. Point it at a second disk if the server has one. |
| `SEHATDESK_BACKUP_KEY` | empty | Encryption key. When set, every backup is AES-256-GCM encrypted. |

**Encryption key.** Create one with `python manage.py backup --generate-key`, put it in the
server's environment / env file as `SEHATDESK_BACKUP_KEY`, and store a copy **off the server**
(printed in the safe, or the hospital owner's password manager). Without the key an encrypted
backup cannot be restored by anyone, including SehatDesk support.

Encrypt whenever a backup leaves the server (USB drive, cloud storage). Unencrypted backups are
full copies of every patient record.

## 3. Automatic backups and retention

- `serve.py` (what the desktop shortcut starts) makes a backup at start-up if the last one is
  older than `SEHATDESK_BACKUP_HOURS`, then checks every hour.
- After each backup the oldest files beyond `SEHATDESK_BACKUP_KEEP` are deleted.
- **Settings → System** lists the latest backups, says whether they are encrypted, warns when
  no backup is newer than twice the interval, and has **Back up now** (writes to the server's
  backup folder; backups are never downloaded through the browser).
- Every manual backup is in the audit log (`system.backup`).

**Off-site copy (the 3-2-1 rule).** Keep at least one recent encrypted backup away from the
server: a weekly USB copy taken home by the owner, or a synced cloud folder. A fire or theft
takes the server and the backup folder together.

**Object storage.** If patient documents are kept in S3-compatible storage
(`SEHATDESK_DOCUMENT_STORAGE`), they are not inside the backup file. Turn on bucket versioning
and a lifecycle rule that keeps deleted versions for at least 30 days, and back the bucket up
with the provider's tools. The database backup still holds every document's record and path.

## 4. Commands

Run from the SehatDesk folder with the same environment as the server
(`SEHATDESK_ENV_FILE=...` or the installer's data folder).

```
python manage.py backup                  # back up now
python manage.py backup --list           # list backups, newest first
python manage.py backup --verify FILE    # prove a backup can be restored
python manage.py backup --generate-key   # new encryption key
python manage.py restore FILE --yes      # restore (server stopped)
```

`--verify` decrypts the file, checks every checksum, and runs SQLite's integrity check on the
database inside, without touching live data.

## 5. Restore procedure

1. **Stop the server.** Close the SehatDesk window / stop the service, so nobody saves anything
   during the restore. Tell staff to stop entering data.
2. **Pick the backup**: `python manage.py backup --list`. For an encrypted backup, make sure
   `SEHATDESK_BACKUP_KEY` is set to the key it was made with.
3. **Verify it**: `python manage.py backup --verify <file>`. Do not restore a file that fails.
4. **Restore**: `python manage.py restore <file> --yes`. The current database, documents and
   media are first moved to `<data folder>/pre-restore-<timestamp>/`, then the backup is
   unpacked and migrations run (a backup from an older version opens on this version).
5. **Start the server** and sign in. Check: the newest patient, today's invoices, a document
   opens. Staff will need to sign in again if the secret key changed.
6. **Record it**: note the time, the backup used, and which work since that backup must be
   re-entered (anything after the backup time is not in it).

**Undo a restore:** stop the server, move the files from `pre-restore-<timestamp>/` back into
the data folder (database file, `private/`, `media/`), start the server.

**PostgreSQL servers** (Docker / multi-branch): backups contain `database.pgdump`. Restore with
`pg_restore --clean --if-exists -d <database> database.pgdump` after unzipping/decrypting, then
copy `private/` and `media/` back, then `python manage.py migrate`.

## 6. Restore test (do it, not just plan it)

- **Every release**: CI runs a restore drill (`apps/core/tests/test_backup.py`): migrate, add a
  patient and a document, back up (plain and encrypted), lose the data, restore, and check that
  everything came back.
- **Every month at each hospital**: run `backup --verify` on the newest backup and on the
  off-site copy. **Every 3 months**: restore the newest backup onto a spare PC (or a staging
  install), sign in and open a few records. Write the date and result in the go-live / support
  log.

## 7. Disaster recovery

| Situation | What to do | Data lost |
|---|---|---|
| Server PC dies, disk is fine | Install SehatDesk on another PC, copy the old data folder over, start. | None |
| Disk dies or data folder deleted | New/repaired PC: install SehatDesk, set the same `SEHATDESK_BACKUP_KEY`, restore the newest backup (§5). | Work since that backup |
| Ransomware / theft / fire | Clean PC, install, restore the newest **off-site** encrypted backup. Change all staff passwords afterwards. | Work since the off-site copy |
| Database damaged ("malformed") | Stop server, `backup --verify` the newest backups, restore the newest good one. | Work since that backup |
| Bad update | Roll back (§8). | None |

**Targets for a single hospital:** recovery point (RPO) ≤ 24 hours with daily automatic
backups, ≤ 7 days for the off-site copy; recovery time (RTO) ≈ 2 hours with a spare PC and
the installer at hand.

## 8. Production rollback (bad update)

Every update is installed over the previous version and keeps the data folder.

1. **Before updating**: `python manage.py backup` (or press Back up now) and note the file name.
   Keep the previous installer.
2. **If the new version misbehaves**: stop the server, reinstall the **previous** installer.
3. If the new version had already migrated the database (most updates do), restore the backup
   from step 1 (§5), because an older version cannot read a newer schema.
4. Start the server, confirm it works, and report the problem with the request ID shown on the
   error page (Settings → System lists recent errors with the same ID).

Work done between the update and the rollback must be re-entered; keep the rollback window
short by testing each release on staging first (docs/PROJECT_NOTES.md §15).
