# 0089. Apache 2.0 licence, DCO, CC BY 4.0 docs, narrow open core

- Status: Accepted
- Date: 2026-10-14

## Context

Localization OS was developed as proprietary, source-available software. The product is moving to open
source. The cost of software is heading toward zero, and the project earns from work, infrastructure and
accountability around the software (hosting, support, compliance paperwork, private inference, custom
work), not from licensing the code. Reach matters more than licence revenue: every self-hosted install is
a possible hosting, support or custom-work customer, and many enterprises ban or restrict copyleft
licences such as the AGPL.

The one-way nature of the choice was weighed: code released under Apache 2.0 stays Apache 2.0, and
tightening a licence later causes forks and backlash. Protection comes from what a fork cannot copy: the
trademarks, the compliance paperwork, private inference hardware, being the original maintainer, and
shipping speed.

This ADR replaces the earlier source-available licence decision.

## Decision

Licence and contribution terms:
- **The core is licensed under Apache 2.0** (`LICENSE`, `NOTICE`). Apache 2.0 grants no trademark rights;
  the names Localization OS, Patois and Patois Labs are covered by a separate trademark policy.
- **Contributions use the Developer Certificate of Origin (DCO).** Every commit carries a
  `Signed-off-by` line (`git commit -s`). There is no contributor licence agreement (CLA).
- **Documentation is licensed under CC BY 4.0.**
- **The default install stays copyleft-free.** Dependencies are MIT, Apache, BSD or similarly permissive and
  library-shaped (see ADR 0043). PyMuPDF was replaced by pypdfium2 and typst; psycopg and ldap3 are opt-in
  requirement files. `tests/verify_licenses.py` fails any GPL-family licence in the default lock.

Open core, kept narrow:
- Everything that ships today is the Apache 2.0 core unless it is listed as a paid add-on below.
- **Paid add-ons:** Advanced Workflows and web verification; Evaluations Lab (its metrics library stays in
  the core); Semantic TM; Databricks export; LLMemory and workflow suggestions; and the enterprise
  infrastructure adapters (OpenTelemetry, Prometheus metrics, PII name detection, the durable job queue and
  S3 storage). Custom connectors and a hosted MCP service are also sold as services.
- **Core:** everything else, including SSO (SAML and OIDC), SCIM, the audit chain, accessibility, Queries,
  the mock-egress guard, Ask and Draft, image translation, Setup AI defaults, all connectors, the MCP
  server and the Enterprise settings page.
- New features from now on are paid add-ons by default unless the maintainer places them in the core. Bug
  fixes, security fixes and maintenance always go to the core.
- Existing database tables stay in the core schema even where the feature that uses them is an add-on.

Stewardship promise (published with the repository):
1. Nothing that is free ever becomes paid.
2. SSO, SCIM, the audit chain, accessibility and security fixes are always free. New work in those four
   areas goes to the core.
3. Paid add-ons may join the core later.

## Consequences

- Licence checks, add-on packaging and delivery, and two-edition testing are built after the first public
  release. Until then the public tree carries the core, and add-on code is kept out of it behind small
  hooks that let the core start and run without it.
- Never paywall SSO, SCIM, the audit chain, accessibility or security fixes: they are entry requirements
  for the buyers this product serves.
- Never move a free feature to paid.
- Docs, help text and release notes describe the core as Apache 2.0 and carry no pricing, "perpetual
  licence" or "source-available" language.
- Trademark policy and the NOTICE wording are maintained outside this ADR.
