# SehatDesk — Expanded Desktop Workflow Kit

134 desktop artboards, including the prior 42 screens and 92 added screens. The added screens focus on pharmacy POS, laboratory operations, doctor workflows, role profiles, wizard steps, document layouts, and exception handling.

## Open the package
Extract the ZIP and open START-HERE.html in a browser. All resources work locally; no server, account connection, or internet is needed. Use the module filter and page search, or select Wizard preview. Previous / Next and visible action hotspots navigate between artboards. Form fields and business actions are visual examples, not functioning clinical or financial software.

## Import into Figma
Drag files from 01-editable-svg into a Figma design canvas. Import by module in manageable batches. The 02-png-previews folder contains flattened full-size visual references. Page dimensions are 1600 × 1000. Inter is the intended font, with DejaVu Sans used as fallback.

These are editable SVG vector artboards, not a native .fig file. SVG text import behavior varies: some importers turn text into outlines. Native Figma components, Auto Layout, variables, and prototype links are not embedded. Use the provided flow map to recreate links in Figma. The offline browser already provides wizard review navigation.

## Coverage
There are 134 explicitly indexed screens, not a claim to cover every possible hospital configuration. PAGE-CHECKLIST.csv is the authoritative delivered page list. WIZARD-FLOWS.md enumerates all 16 linked wizard sequences. Each listed screen has both SVG and PNG. For a new specialty or business rule, extend this checklist rather than assuming an undocumented page exists.

New pharmacy screens: POS cart, patient lookup, prescription linking, batch allocation, discount, payment, receipt, held sales, sale returns, product catalog, medicine setup, suppliers, purchasing, goods receipt, expiry/quarantine, adjustment, transfer, sales reporting, and cancellation.

New laboratory screens: operations dashboard, test catalog/setup/packages, order wizard, identity verification/collection/labels, accession, processing, result entry/validation, critical alert handling, verification/release gate, released reports, amendments, rejection/recollection, billing, and operational reporting.

New doctor screens: role home, queue, nine-stage consultation flow, prescription-item wizard, alert review, templates, onboarding, leave/rescheduling, referrals, certificates, ward notes/rounds, discharge wizard, and signed encounter amendments.

Added shared screens: reception, nurse, pharmacist, laboratory, and administrator profiles; authentication/session states; visit/report/receipt print layouts.

## Reference assets
04-image-references contains the earlier generated visual mockups as raster inspiration. The artboard collection is in 01-editable-svg. Reference images can contain different illustrative states; DATA-CONTRACTS.md defines authoritative scenario values for the added flows.

## Clinical data
All patients, transactions, and identifiers are fictional. No medication recommendations are supplied. Laboratory numeric fields intentionally use entry placeholders. Incomplete prescriptions cannot be submitted, incomplete laboratory results cannot be released, and AI drafts cannot auto-sign clinical records. Document previews with missing clinical values are labeled unverified/template.

## Mobile
Mobile remains the next design phase. See MOBILE-NEXT-PHASE.md. No mobile artboards are claimed in this ZIP.
