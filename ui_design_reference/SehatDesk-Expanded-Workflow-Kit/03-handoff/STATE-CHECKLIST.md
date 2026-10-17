# Cross-cutting state checklist for implementation

Delivered visual state pages: 037 shared interface states, 127 authentication/session states, 131 discard cart confirmation, 084 incomplete lab validation, 087 blocked report release, 106 incomplete prescription, 107 checks-not-run, 090 specimen rejection, 133 critical acknowledgement, 134 signed record amendment.

For each list/form: loading, no records, no filter results, read-only permission, field error, failed save/retry, stale version conflict, unsaved changes, success, and session expiry. Shared states are reusable patterns; they are not duplicated as separate artboards for all 134 pages.

Pharmacy: unknown barcode, insufficient stock, expired/recalled batch, price override denied, held-cart reservation expiry, split tender, payment declined, interrupted payment with status reconciliation, duplicate payment prevention, partial supply, partial return, refund exceeds balance, return inspection, adjustment approval, transfer discrepancy.

Laboratory: duplicate order, wrong specimen/container, insufficient specimen, sample missing, sample rejection/recollection, analyzer unavailable, failed quality control, missing result, method/range version, critical threshold, unacknowledged notification/escalation, verifier not authorized, amended released result, corrected-report recipient notification.

Clinical: wrong-patient prevention, allergy unknown versus none versus reported, missing vitals, incomplete medicine directions, alerts requiring review, encounter handoff, draft versus signed, failed signing, addendum, shared record permission, referral not acknowledged.

These are implementation acceptance cases. Cases not represented as unique pages use the shared visual patterns and are explicitly not counted as additional delivered screens.
