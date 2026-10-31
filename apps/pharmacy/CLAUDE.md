# apps/pharmacy — Pharmacy: inventory, POS and purchasing

PRD §9. Phases 12–14. PROJECT_NOTES §12–13.

## Files
- `stock.py` — the ONLY code that changes stock: `receive`, `sell`, `sale_return`,
  `purchase_return`, `adjust_to_count`, `write_off`; queries `saleable` (FEFO), `on_hand`,
  `products_with_stock`, `low_stock`, `near_expiry`, `expired`, `stock_value`,
  `product_statement` (opening, running balance, closing), `ledger_mismatches`.
- `pos.py` — the till: `open_cart`, `add_product`, `attach_patient`, `attach_prescription`,
  `fill_from_prescription`, `set_discount`, `hold`/`resume`, `checkout`, `return_items`,
  `prescription_lines`, `close_prescription`.
- `purchasing.py` — suppliers and purchases: `create_purchase`, `add_item`, `mark_ordered`,
  `receive` (posts stock + supplier ledger), `cancel`, `return_items`, `post_opening_balance`.
- `pricing.py` — `change_price` (new stock, or new stock and shelf), `reprice_batch`, `record_edit`
  (logs what the product form or a purchase changed), `unit_value`. Writes `ProductPriceChange`.
- `catalog.py` — `save_product` (auto SKU and barcode), `add_many` (grid, all or nothing),
  `ensure_standard_categories`. `barcodes.py` — `new_barcode` (EAN-13 starting 20), `code128_svg` (digits use subset C, for wider bars).
- `models.py` — `Product` (base unit, pack size + pack unit, `allow_loose_sale`, reorder level,
  link to `prescriptions.Medicine`; `describe_quantity`/`pack_breakdown`/`loose_sale_error`),
  `InventoryBatch`, `StockTransaction` (ledger), `PharmacySale`, `Vendor`, `Purchase`,
  `VendorLedgerEntry`.
- Views split: `views.py` (inventory), `views_pos.py` (till, htmx), `views_purchasing.py`.
- `templatetags/pharmacy.py` — `in_packs`, `as_packs`, `unit_label`, `pack_label` filters.

## Rules
- Quantities are in the product's base unit. Every change locks the batch and writes a
  `StockTransaction`; balances must equal the ledger sum. No negative stock, no selling expired.
- Loose sale: a pack (strip/box) can be split; the till's "Sold as: full pack" multiplies by
  `pack_size`. If `allow_loose_sale` is off, the POS service refuses non-multiples of `pack_size`.
- Checkout is all-or-nothing: stock-out, invoice, payments in one transaction; paid in full.
- Prescription-only products need an attached prescription line.
- Discounts need `pharmacy.discount` (in the default pharmacist role, capped in Billing settings).

- Unit, pack and box names are free text (`unit`, `pack_unit`, `box_unit`); the `Unit`/`PackUnit`
  classes are only suggestions. `packs_per_box` adds the third level. Always count in base units.
- Tax is per product (`tax_percent`). A category only fills it in on new products.
- Expiry alert: `Product.expiry_alert_days`, else Preferences. `stock.near_expiry` handles both.
- Till returns: `PharmacyCartReturn` waits on the cart; `pos.add_return` matches it to past sale
  lines, checkout (or `settle_returns_only`) runs `return_items`. No negative invoices or lines.
- `purchasing.reorder_suggestions` / `create_reorder_purchases`: drafts per supplier, on request.
- Purchase lines keep 4-decimal unit costs so box prices add back up exactly.

- Rx-only medicines sell without a prescription unless `pharmacy_require_prescription` is on; each
  one is audit-logged (`pharmacy_sale.rx_only_no_prescription`).
- Checkout adds medicines not on the attached prescription as new lines (`added_at_pharmacy`, who
  added it; no dose or frequency). Existing lines never change. `attach_paper_prescription` makes an
  empty one (status `paper`, no encounter) for a doctor's handwritten prescription.
- Row discount: `discount_percent` (`pos.set_line_discount`, needs `pharmacy.discount`, under the cap).
- Purchase tax: `PurchaseItem.tax_percent` (`Purchase.line_tax`), plus `tax_amount`, `further_tax`,
  `advance_tax` on the bill. All count in `grand_total`; report `purchase-tax`.
- Entry grid `purchasing/_item_grid.html` (views_purchasing: `grid_lines`, `echo_rows`) is shared by
  purchases and Receive stock. Quantity, free, cost and sale price are per the row's chosen unit.

- Discount trail: `PharmacySaleItem.discount_by`, `PharmacySale.discount_asked_by` and
  `discount_reason`; report `discount-trail`.

- UoM: `UnitOfMeasure` is the hospital's own list (screen `uom`); `catalog.packing_suggestions` offers
  it with phrases already used on products, most used first.
- Packing is entered as sizes: smallest unit, a pack of those, a bigger pack of packs. Full form:
  `_packing_editor.html` (type a price on any row, the others fill; the price posts exactly as typed
  with `cost_per`/`price_per`). Add-many and purchase quick add have the same boxes on one line, with
  prices for the biggest size. `packing.py` phrases remain for presets, the UoM list and imports.
- Prices per base unit keep six decimals (`unit_money`) so a box price adds back up exactly; screens
  show two. One price per product: each size is that price times its units. No separate pack discount.
- The smallest unit cannot change once stock has moved (`ProductForm`). Pack sizes can.
- A price change is not a batch. New stock takes the product's price; the shelf keeps its own until
  repriced (`pricing`). Repricing never changes cost. Every change is a `ProductPriceChange` row, shown
  on the product page and its ledger, and moves no stock.
- Lots: stock under an existing batch number at the same cost and price joins that lot; a new cost or
  price opens a new `InventoryBatch` with the next `lot` (shown as `batch.label`). Same number with a
  different expiry is refused. A new sale price on a receipt also becomes the product's price.
- Ledger: `StockTransaction` keeps `unit_cost` and `unit_price` at the time; `document` links the bill or
  sale. Screens: `ledger` (all, dates, balance check) and `product-ledger` (one product's account).
  `balance_after` is the batch's; the running product balance comes from `product_statement`.
- Purchase lines have `discount_percent`: `line_cost` is after it, and it lowers the shelf cost.
- Till ergonomics: focus returns to the scan box after every action, unit chips sit on the row, price
  shows for the chosen unit, batch is first-expiry by default, paying goes straight back to the till.

- Reception visits at the pharmacy: `pos.visits_without_rx` lists today's tokens and appointments with
  nothing sent; `pos.tag_visit` puts the patient and an empty paper Rx on the sale (Prescriptions page
  and the till's Attach Rx). Product page has the barcode and labels (roll 1.5 x 1 inch by default, 50 x 25 mm, or A4; `core/labels.py`). The Barcodes page has the same choice. Each sticker is its own page, because a browser shrinks an A4 page onto a sticker.
- Entry grid: switching Tablet/Strip/Box converts the rate (`setUnit`). `Disc %` is per line.
- Supplier ledger: the supplier page shows the account across branches; the full dated version with
  exports is report `supplier-statement` (tab on the purchasing screens, `apps/reports`).

## Permissions
`pharmacy.sale`, `pharmacy.refund`, `pharmacy.discount`, `pharmacy.inventory.manage`,
`billing.payment.collect`. Module switch: `pharmacy`.

## Tests
`tests/test_inventory.py`, `tests/test_pos.py`, `tests/test_purchasing.py`, `tests/test_workflows.py`,
`tests/test_packing_pricing.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
