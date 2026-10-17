# apps/pharmacy — Pharmacy: inventory, POS and purchasing

PRD §9. Phases 12–14. PROJECT_NOTES §12–13.

## Files
- `stock.py` — the ONLY code that changes stock: `receive`, `sell`, `sale_return`,
  `purchase_return`, `adjust_to_count`, `write_off`; queries `saleable` (FEFO), `on_hand`,
  `products_with_stock`, `low_stock`, `near_expiry`, `expired`, `stock_value`.
- `pos.py` — the till: `open_cart`, `add_product`, `attach_patient`, `attach_prescription`,
  `fill_from_prescription`, `set_discount`, `hold`/`resume`, `checkout`, `return_items`,
  `prescription_lines`, `close_prescription`.
- `purchasing.py` — suppliers and purchases: `create_purchase`, `add_item`, `mark_ordered`,
  `receive` (posts stock + supplier ledger), `cancel`, `return_items`, `post_opening_balance`.
- `models.py` — `Product` (base unit, pack size, reorder level, link to `prescriptions.Medicine`),
  `InventoryBatch`, `StockTransaction` (ledger), `PharmacySale`, `Vendor`, `Purchase`,
  `VendorLedgerEntry`.
- Views split: `views.py` (inventory), `views_pos.py` (till, htmx), `views_purchasing.py`.

## Rules
- Quantities are in the product's base unit. Every change locks the batch and writes a
  `StockTransaction`; balances must equal the ledger sum. No negative stock, no selling expired.
- Checkout is all-or-nothing: stock-out, invoice, payments in one transaction; paid in full.
- Prescription-only products need an attached prescription line.
- Discounts need `pharmacy.discount` (not in the default pharmacist role).

## Permissions
`pharmacy.sale`, `pharmacy.refund`, `pharmacy.discount`, `pharmacy.inventory.manage`,
`billing.payment.collect`. Module switch: `pharmacy`.

## Tests
`tests/test_inventory.py`, `tests/test_pos.py`, `tests/test_purchasing.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
