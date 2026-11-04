# apps/approvals — Stock edits that wait for an approver

Counts, write-offs (expired/damaged), returns to supplier, batch corrections (number, expiry) and unit
corrections (whole product: `request_unit_correction`, `batch` empty) and "write off all and delete"
(`request_delete`, `batch` empty) asked
for on a product page do not move stock straight away. They become a `StockChangeRequest`; stock
moves only when someone holding `admin.approvals.manage` approves, so missing stock cannot be hidden.

## Files
- `models.py` — `StockChangeRequest` (branch-scoped): action, quantity, `stock_at_request`,
  new batch number/expiry, reason, reference, status pending/approved/rejected, who asked, who decided,
  the `StockTransaction` it posted. `change` (signed units) and `summary` for screens.
- `services.py` — `request_stock_change` (checks, then auto-approves when the asker can approve),
  `approve` (runs `pharmacy.stock`: `adjust_to_count`, `write_off`, `purchase_return`,
  `correct_batch`, `write_off_all` then `recyclebin.send_to_bin` for a delete), `reject` (needs a
  reason), `pending_count` (sidebar badge). `request_delete` needs `pharmacy.product.delete` +
  `pharmacy.inventory.manage`; `delete_blocker` refuses stock in another branch, an open purchase or a
  delete already waiting.
- `views.py` — `approvals:list` (Waiting / Decided tabs, tick rows to approve or reject many),
  `approve`, `reject` (row bin button asks the reason), `bulk`.

## Rules
- A count is applied as the difference found when counted (`quantity - stock_at_request`), so sales
  between asking and approving are not undone.
- The ledger row is posted by the approver; its reason ends "(asked by …)". Every step is audit-logged.
- Requests are per branch: approve from the branch the stock is in.
- Write off all and delete writes off whatever is left when approved (sales while waiting are kept) as
  expired or damaged (`write_off_as`), then moves the product to the Recycle bin with the asker's reason.
  Restoring it brings the product back, not the stock.

## Permissions
`pharmacy.inventory.manage` to ask; `admin.approvals.manage` to decide (hospital admin, super admin, or
given to one person on their staff page). Sidebar item `approvals` with a waiting count.

## Tests
`tests/test_approvals.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
