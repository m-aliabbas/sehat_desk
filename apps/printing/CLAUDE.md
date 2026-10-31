# apps/printing — Automatic print routing

Print goes to the right printer on the PC it was pressed on, with no print window. PROJECT_NOTES §4b.

## Flow
1. `static/js/print_router.js` replaces `window.print`, works out the paper (thermal, label, a5, a4) and
   posts a self-contained copy of the page to `job-create`.
2. `services.enqueue` finds the station on the browser's IP (`station_for_request`) and its printer for
   that paper. None of either: `{"queued": false}` and the browser's print window opens as before.
3. The helper (`print_helper/`, one per PC) polls `agent-next` with its token, prints, reports back.
4. The browser polls `job-status`, then fires `afterprint`, so pages that print copies in turn still work.

## Token printer (no helper)
`direct.py`: an 80 mm job with no helper route goes to the `TokenPrinter` for this PC (one printer, or the `sd_token_printer`
cookie set by `token-printer-use`). The server draws it with `print_helper.render` and sends it to an IP (port 9100) or a
printer installed on the server PC, within the request. The page is sealed by a CSP (no disk, no network), never stored.
On failure the browser's print window opens. A page with `<meta name="sd-print-dialog">` (prescription) always opens the window.

## Connecting and choosing printers
- An admin presses "Allow new PCs" (`PrintEnrollment`, 24 hours). A helper posts its PC name to
  `agent-join` every 10 s and gets its token; nothing is typed on the PC. Refused when closed, when the
  address is not private, or when two hospitals on one server are open at once.
- The helper's `hello` reports printers and their paper sizes. `detect.py` sorts them into office,
  thermal and label, by name first and then by size. `services.assign_routes` fills each paper:
  one match is used, several ask in Settings, and a working route is never moved by a new printer.
- A paper in `manual_routes` was set by hand and detection leaves it alone until that printer is gone.
  A5 with no route of its own follows the A4 printer.

## Files
- `models.py` — `PrintStation` (a PC, its printers, `route_<paper>`), `PrintEnrollment`, `PrintJob`, `Paper`.
- `detect.py` — which printer suits which paper.
- `services.py` — joining, tokens, `assign_routes`, `enqueue`, `next_job`, `finish_job`.
- `views.py` — browser endpoints, helper endpoints (`_agent`), Settings → Printers.
- `print_helper/` (repo root) — `render.py` (Edge/Chrome to PDF; thermal to ESC/POS with cut),
  `printers.py`, `agent.py`, `main.py` (window and tray icon, with no Exit on purpose). Packaging: `packaging/sehat_print.spec`, `print_helper.iss`.

## Rules
- Job html is patient data. It is blanked the moment the helper collects it; it is never logged.
- Jobs not collected in `COLLECT_WINDOW` expire, so a helper coming back never prints old pages.
- Tokens are stored hashed. Removing a PC cancels its token; its helper then waits to join again.
- A station only gets jobs from browsers on its own IP, so a PC that joins cannot read other PCs' pages.
- Helper endpoints use the token, not a session; they only ever see their own station's jobs.

## Permissions
`admin.organization.manage` for Settings → Printers. Sending a job needs only sign-in: it is the page
the user already has open (listed as PERSONAL in the URL inventory).

## Tests
`tests/test_printing.py`, `tests/test_helper.py` (needs Edge or Chrome, skips otherwise).

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
