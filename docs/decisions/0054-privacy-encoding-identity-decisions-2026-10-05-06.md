# 0054. Privacy erasure, encoding and identity decisions, 2026-10-05/06

Status: Accepted
Date: 2026-10-06

## Context

The security review, cloud reviews and issue campaign raised product and policy questions that needed a
maintainer decision. They were answered on 2026-10-05/06 and are recorded here.

## Decision

Privacy and data:
- Document privacy erasure deletes only that document's TM rows (identical source rows shared with another document
  go too); the rest of the project TM stays. User privacy erasure purges only exports the user requested plus
  workspace/self exports. Project privacy erasure purges LLMemory learned from it; plain delete keeps it (#441).
- Windows-1252 and other legacy-encoded text uploads are auto-converted (UTF-8, then BOM UTF-16, then declared
  charset, then Windows-1252); only unreadable files are refused. Translations export as UTF-8.

Identity and access:
- #468: customer-bound accounts may still add their own workflow variants and pairs to a shared group.
- Lab promotion by a customer account becomes that customer's workflow; staff promotions unchanged.
- #407: the pending SAML fallback state becomes non-blocking (warning only).
- #420: add an admin "Unlink SSO identity" action on all four surfaces. #423: no local TOTP factor for
  no-password SCIM/SAML accounts. #421: keep revoking a SCIM token when its owner is disabled.
- #480: session secrets shorter than 16 characters refuse boot (deploy checklist checks length).
- Multi-org: per-org SRX via segmentation profiles; rulebooks stay host-only; install-wide settings writable by an
  org admin become host-only before multi-org is ever enabled.

UX:
- Command palette says "No matches" when only the search row remains. Run "pending" grey and "paused" amber. The
  editor stays compact (about 8 segments per 1440x900 screen). One primary button per card on the document page.
  Glossary Content type lives in the open Status card. Banner text uses no em dash.

## Consequences

Each decision is implemented in its own issue. Documentation that describes the erasure scope follows the
rules above.

## Links

ADR 0052.
