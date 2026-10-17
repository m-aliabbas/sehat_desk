# Linked wizard inventory

Open START-HERE.html → Wizard preview. These are screen-navigation prototypes; they do not execute transactions.

## POS checkout

043 Cart → 044 Patient → 045 Prescription → 046 Batches → 047 Discount → 048 Payment → 049 Receipt

## Sales return

051 Find receipt → 052 Review items → 053 Refund

## Medicine setup

055 Identity → 056 Pricing → 057 Opening stock

## Purchase order

059 Supplier → 060 Items → 061 Review

## Goods receipt

062 Purchase order → 063 Batches → 064 Post stock

## Stock adjustment

066 Count → 067 Approve

## Lab test setup

072 Test identity → 073 Method & range → 074 Review

## Lab order

076 Patient → 077 Tests → 078 Confirm

## Sample collection

079 Verify identity → 080 Collect & label

## Lab result entry

083 Enter results → 084 Validate

## Lab verification

086 Review → 087 Release

## OPD consultation

095 Identity → 096 History → 097 Exam → 098 Assessment → 099 Orders → 100 Rx → 101 Plan → 102 Review → 103 Signed

## Prescription item

104 Medicine → 105 Directions → 106 Review

## Doctor onboarding

109 Profile → 110 Clinic → 111 Access

## Referral

114 Destination → 115 Handover

## Inpatient discharge

119 Clinical → 120 Clearance → 121 Release

## Non-linear branch links

- POS → Held sales, product catalog, return wizard, cancellation.
- Consultation prescription step → Prescription item wizard → Return to prescription.
- Doctor home → Lab reports, referral wizard, ward rounds, templates, certificates.
- Signed encounter → Patient history, next patient, amendment, follow-up booking.
- Lab operations → Test setup, order, collection, processing, verification, billing.
- Critical alert → Communication log → Critical alerts list.

## Incomplete versus completed states

The consultation review/signed screens show a later completed state; they are not claims that entering the earlier example placeholders created a valid medical record. The lab verification sequence deliberately demonstrates a blocked release state. Use the released-reports screen for already released examples. Flow navigation is for reviewing designs and does not enforce business validation.
