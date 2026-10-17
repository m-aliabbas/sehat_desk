# apps/documents — Patient documents and attachments

Uploads attached to patients, encounters, admissions or lab orders. PRD §13. Phase 18.

## Files
- `services.py` — `validate_upload` (size, extension allow-list, magic-number sniff), `upload`,
  `archive`, `restore`, `signed_url`, `token_is_valid`, `panel_context`.
- `models.py` — `Document`; files in the private "documents" storage under random names.

## Rules
- Never serve documents from the public media route; only through a signed, expiring,
  permission-checked link.
- The browser's content type is ignored; the real type comes from the file's first bytes.
- No hard delete: archive with a reason.

## Permissions
`document.view`, `document.upload`, `document.archive`.

## Tests
`tests/test_documents.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
