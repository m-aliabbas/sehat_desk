# apps/billing — Invoices, payments and refunds

All patient money: invoices (OPD, lab, pharmacy, procedures, IPD), payments, refunds and the
cashier's day closing. PRD §11. PROJECT_NOTES §12–13, §18.

## Files
- `services.py` — `create_invoice`, `add_item`, `remove_item`, `set_line_discount`,
  `set_invoice_discount`, `issue`, `cancel`, `void_unpaid`, `receive_payment`, `refund`,
  `day_totals`, `net_by_day`, `ensure_payment_methods`, `settings_for`.
- `models.py` — `Invoice` (draft → unpaid → partially_paid → paid → refunded; cancelled),
  `InvoiceItem.Kind` (consultation, service, procedure, lab, pharmacy, room, misc),
  `Payment`, `Refund`, `PaymentMethod`, `BillingSettings`.

## Rules
- Money fields change only through services, inside a transaction with `locked(invoice)`.
- Issued invoices lock their items. A paid invoice is corrected by refund, never by edit.
- Discounts need `billing.discount`, a reason, and stay under the cap in Billing settings.
- Non-cash methods need a reference. `idempotency_key` makes a double submit charge once.
- Other apps create their invoices through these services (lab orders, POS checkout, IPD stay,
  procedures). Lab and HR react to invoice saves via signals.
- `InvoiceItem.performed_by` names the doctor for commission (hr/commission.py).

## Permissions
`billing.view`, `billing.payment.collect`, `billing.discount`, `billing.refund`.

## Tests
`tests/test_billing.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
