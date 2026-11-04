# apps/recyclebin — Edit and delete setup records, Recycle bin, restore, erase

Every setup list can be edited and deleted, each behind its module's permission: products, product
categories, units of measure, suppliers, departments, specialties, services, service categories,
expense categories, payment methods, wards, rooms, beds, lab test categories, lab tests, lab panels,
diagnosis codes, medicines and reception counters. Deleting moves a record to the Recycle bin; nothing
is erased. Old bills, stock history and reports keep pointing at it. Patients, bills, purchases and
clinical records are never deleted here: they keep their cancel, void or "entered in error" actions.
Doctors and roles keep Deactivate (a doctor is tied to a staff login).

## Files
- `registry.py` — `KINDS`: each kind (key, model, label, module permission(s) — any one allows
  editing, list/detail URL, `edit_url` for its own edit page or `edit_fields` for the shared one, and a
  `blocker` that says why a record cannot go yet: stock on the shelf, an unsettled supplier account,
  open purchases, records still using it, an occupied bed, the Cash payment method).
- `forms.py` — `edit_form(kind, request)`: the shared edit form; dropdowns limited to this hospital,
  per-hospital unique names checked. `templatetags/recyclebin.py` — `{% edit_link kind obj as url %}`.
- `services.py` — `can_edit` / `can_delete`, `editable_keys` / `deletable_keys` (context processor:
  `editable`, `deletable`), `save_edit` (audit `<kind>.update` with the changed fields), `delete` /
  `delete_many`, `deleted` (bin rows), `restore` (refuses if another active record now has the same name
  or code), `erase` (only when nothing else points at it).
- `views.py` — `recyclebin:edit` (`records/<kind>/<pk>/edit/`, opens in the side panel; the kind's
  permission is checked inside), `recyclebin:delete` (one row; the list's bin button, `reason` optional) and
  `recyclebin:delete-selected` (ticked rows, `next` brings you back), `recyclebin:list`, `restore`,
  `restore-selected`, `erase`.
- Templates: `recyclebin/list.html`, `recyclebin/edit.html`, `recyclebin/_actions.html` (pencil + bin for
  one record in a chip row or list: `with kind=... obj=...`, `no_edit=True`, `no_delete=True` inside another
  form), `recyclebin/_select_bar.html` (the "Delete selected" bar any list
  includes; rows tick `<input name="ids" form="pick-<kind>">`, state from `x-data="sdPick"` in `app.js`).

## How a model joins
Mix in `core.models.RecycleBinMixin` first (`class Product(RecycleBinMixin, OrgScopedModel)`):
`objects` hides deleted rows everywhere, `all_objects` sees them, related lookups (`sale.product`) still
work. Make unique constraints conditional on `deleted_at__isnull=True` so the name can be used again.
Add a `Kind` to `registry.py` and `_actions.html` (or `data-edit` / tick boxes) to its list.
`masterdata/defaults.py` counts deleted rows as existing, so an update never brings one back.

## Permissions
The kind's module permission to edit; `records.delete` plus that permission to delete; `admin.recyclebin.manage` to open the bin,
restore and erase. Both can be given to one person on their staff page ("Extra access").

## Tests
`tests/test_recyclebin.py`, `tests/test_edit_everywhere.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
