# 0024. WCAG 2.2 AA, enforced by shrink-only ratchets

- Status: Accepted
- Date: 2026-09-23 to 2026-09-24 (remediation T0 to T10)

## Context

Public-sector and education buyers need an accessibility conformance report (VPAT, WCAG 2.2 A/AA).
Accessibility is an entry requirement for the product, so it is always part of the free core.

## Decision

- Target WCAG 2.2 AA. Static checks C1 to C26 plus an axe browser suite; allowlists shrink only.
- Specific rulings: busy animations stay infinite only while a user action runs and stop under reduced
  motion; busy controls use `aria-disabled` + click guard; password fields `autocomplete="new-password"`
  (never `off`); editor alternates shortcut Alt+A (Ctrl+Option+A on Mac); the announcer keeps
  user-requested outcomes, last wins; an absolute session limit `session_max_hours`
  (first 20 hours for the WCAG 2.2.1 20-hour exception, default 24 in code since T11), which any admin
  may change (audited).
- Editable page layouts: one per page type, saved per user on the server.
- Fix targets for accessibility defects: blocking within 7 days, serious within 14 days, minor in the
  next release.

## Links

- .claude/rules/templates-a11y.md
