---
paths:
  - "docs/**"
  - "app/capabilities/**"
  - "app/templates/help.html"
  - "llms.txt"
  - "README.md"
---

# Docs and user-facing copy

Why: ADRs 0011 (four surfaces), 0021 (no em dashes, ASCII). Map: `docs/README.md`.

- No em dashes in anything a person reads; ASCII in anything that prints to a terminal. Pre-existing em dashes (e.g. `QUEUE_LAUNCH_FAILED_NOTE`, an `&mdash;` in help.html) are not a licence to add more.
- Several docs are CRLF in the working tree (`docs/CLI.md`, `docs/architecture/translation-pipeline.md`, `cost-and-budgets.md`, some JSON fixtures): edit them byte-wise or with the Edit tool; never whole-file rewrite with `pathlib.write_text`.
- Every CLI leaf and flag appears in `docs/CLI.md`; every module carries a `Documented in: docs/architecture/<name>.md` line (`verify_docs_architecture`).
- Claims are not policed by tests: a compliance doc once asserted a removed cross-org door. When behaviour changes, grep the compliance docs and public docs for the old claim.
- Fixed wording: the AI opt-out sentence and the account-lockout sentence are used verbatim in the compliance docs and the user docs; change them everywhere at once.
