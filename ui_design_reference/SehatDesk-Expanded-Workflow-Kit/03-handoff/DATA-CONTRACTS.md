# Shared scenario data and state transitions

## Baseline hospital / clinical context
Hospital: Sehat Medical Centre, Islamabad, Main branch. Patient: Ayesha Khan, SD-001248, 28/F. Clinician: Dr. Zain Ali. Clinic: General medicine, Room 03. Encounter: VIS-2026-0428. Current token: A-024. Next token: A-025. Reported allergy: Penicillin; verify reaction and severity.

Reception counters 01 and 02 are open; 03 is closed. Counter IDs identify desks and never replace clinic-token IDs. General medicine, cardiology, and pediatrics waiting counts are 8, 6, and 4 (18 total). There are 3 current consultations. Pharmacy uses counter P-01 and its own shift PH-SH-021, separate from reception cash closing.

## POS checkout — one connected sale scenario
Sale POS-00078: ORS 4 × Rs 50 = Rs 200; gauze 2 × Rs 100 = Rs 200; digital thermometer 1 × Rs 650 = Rs 650. Subtotal Rs 1,050; discount Rs 50; configured demo tax Rs 0; due Rs 1,000. Cash received Rs 1,000; change Rs 0. No tax rate is presented as statutory guidance.

Before sale: ORS 12, gauze 8, thermometer 15. After posted sale: ORS 8, gauze 6, thermometer 14. Held sale is an alternative pre-payment state of the same cart, not an additional sale. Receipt and print views are post-payment states.

Full return scenario: all 7 units from POS-00078, refund Rs 1,000 (net paid). Approval occurs before refund. Returns go into quarantine until inspection; no automatic saleable-stock increase. The separate earlier counter cash-closing example uses reception counter 01, not pharmacy P-01.

## Purchase / receipt — independent inventory scenario
PO-00281: 100 ORS sachets × Rs 35 = Rs 3,500. GRN-00118 receives accepted batch OR-188 with expiry 30 April 2028. A purchase order alone does not increase stock. The receipt preview uses an independent opening balance of 12; receipt adds 100, resulting in 112. If executed after the POS scenario instead, current balance 8 + 100 = 108; implementation must use the live ledger, never the static preview count.

## Laboratory
Baseline 36 orders = 8 awaiting samples + 12 processing + 4 verification + 12 released. Selected order L-0921 is for Ayesha Khan, CBC, Rs 800, clinician Dr. Zain Ali. Specimen SP-0921-01. CBC is already a line on invoice INV-02684, which also includes Rs 1,500 consultation (Rs 2,300 total). Confirming the order links this line without charging another Rs 800.

Result/verification screens show incomplete sample data; release is blocked. Released-reports list deliberately excludes L-0921. Print template 129 contains no measurements and is marked not for release. Amendment uses a separate released record L-0898. Recollection retains the original rejected specimen and creates a newly identified specimen under its order.

## Doctor
OPD consultation has nine reviewable steps. The completed signed-state scenario has no prescribed medication; the item wizard is an optional unfilled branch. Missing medication selection is “checks not run”, never “all clear”. A signed record can be corrected only via an audited amendment with original text retained.

## Inpatient
Amina Shah, SD-002092, G-03, episode IPD-0412 is used for discharge. The new discharge wizard depicts a later cleared state than the earlier pending checklist. Bed status becomes Cleaning after discharge; it becomes Available only after cleaning confirmation.

## Prototype boundaries
Screen navigation does not execute payments, clinical signatures, inventory changes, notifications, or data persistence. Displayed success states are visual variants only.
