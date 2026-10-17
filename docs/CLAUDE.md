# docs/ — where decisions are written down

Open only the section you need; these files are long.

| File | Holds | Update when |
|---|---|---|
| `PROJECT_NOTES.md` | Architecture and deployment decisions, by numbered § (env §1, stack §2, installer §3, network §4, open decisions §5, decision log §6, repo conventions §7, then one § per phase group: frontend §8, auth §9, masterdata/patients §10, queue §11, clinical/lab/stock §12, POS/purchasing §13, reports/audit §14, environments/CI §15, observability/PostgreSQL/Docker §16, printing §17, preferences §18, security §19, performance §20, backup §21, demo data §22) | a design, deployment, installer, network or environment decision is made or changes; add a line to the §6 decision log too |
| `BACKUP_AND_RECOVERY.md` | What a backup holds, settings, retention, commands, restore and DR procedures | backup/restore code or settings change |
| `../PRD.md` | Product requirements (§ per module) | only when the product owner changes a requirement |
| `../TODO_TASKS.md` | Build checklist by phase | tick items as they are done, with a short note of where they live |

Per-module working notes live next to the code in `apps/<app>/CLAUDE.md`, not here.

Rules: write decisions, not diaries. One § per topic, newest facts replace stale ones (do not
append contradictions). Never put real patient data, passwords or keys in docs.
