---
name: sehatdesk-checks
description: Run SehatDesk's lint, format, Django checks, migration check and tests in the agent_dev311 conda env. Use before finishing any code change, before a commit, or when asked to run tests.
---

# Run the checks

All commands run from the repo root in the `agent_dev311` conda env. If `conda activate
agent_dev311` is not available (non-interactive shell), use the interpreter directly:
`PY=/opt/miniconda3/envs/agent_dev311/bin/python` and call `$PY -m ...`.

1. Lint and format (ruff, line length 100):
   - `python -m ruff check .`
   - `python -m ruff format --check .` (fix with `python -m ruff format <files>`; lines inside
     `# fmt: skip` blocks must be wrapped by hand)
2. Django and migrations:
   - `python manage.py check`
   - `python manage.py makemigrations --check --dry-run` (must say "No changes detected")
3. Tests:
   - Fast loop: `python -m pytest apps/<app>/tests -q`
   - Before finishing: `python -m pytest -q` (≈1,150 tests, under 2 minutes). Run it in the
     background and wait for it once; do not poll.
   - Always include `apps/core/tests` after cross-cutting changes (URL inventory, isolation,
     journeys, query scaling, seed_demo).
4. CSS: if templates gained new Tailwind classes, `python manage.py tailwind build` and commit
   `static/css/app.css`.
5. Pre-commit runs the same hooks (`pre-commit run --all-files`), including `forbid-local-data`
   (never commit `data/`) and `missing-migrations`.

Report failures with their output. Do not mark work done while any of these fail.
