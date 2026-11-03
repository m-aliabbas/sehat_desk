# apps/recyclebin — Delete to the Recycle bin, restore, erase

Deleting a product, product category, unit of measure, supplier, department or service moves it to the
Recycle bin; nothing is erased. Old bills, stock history and reports keep pointing at it.

## Files
- `registry.py` — `KINDS`: each deletable kind (key, model, label, module permission, list/detail URL,
  `blocker` that says why a record cannot go yet: stock on the shelf, an unsettled supplier account,
  open purchases, products still in a category, active doctors/services in a department).
- `services.py` — `delete` / `delete_many`, `deleted` (bin rows), `restore` (refuses if another active
  record now has the same name or code), `erase` (only when nothing else points at it).
- `views.py` — `recyclebin:delete` (one row; the list's bin button, `reason` optional) and
  `recyclebin:delete-selected` (ticked rows, `next` brings you back), `recyclebin:list`, `restore`,
  `restore-selected`, `erase`.
- Templates: `recyclebin/list.html`, `recyclebin/_select_bar.html` (the "Delete selected" bar any list
  includes; rows tick `<input name="ids" form="pick-<kind>">`, state from `x-data="sdPick"` in `app.js`).

## How a model joins
Mix in `core.models.RecycleBinMixin` first (`class Product(RecycleBinMixin, OrgScopedModel)`):
`objects` hides deleted rows everywhere, `all_objects` sees them, related lookups (`sale.product`) still
work. Make unique constraints conditional on `deleted_at__isnull=True` so the name can be used again.
Add a `Kind` to `registry.py` and the delete buttons to its list.

## Permissions
`records.delete` plus the kind's module permission to delete; `admin.recyclebin.manage` to open the bin,
restore and erase. Both can be given to one person on their staff page ("Extra access").

## Tests
`tests/test_recyclebin.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
