---
paths:
  - "app/templates/**"
  - "app/static/**"
  - "app/web/page_layouts.py"
  - "tests/verify_a11y*"
  - "tests/fixtures/a11y*"
---

# Templates, static assets and accessibility (WCAG 2.2 AA)

Why: the VPAT promises WCAG 2.2 AA (ADR 0024). Architecture: `docs/architecture/browser-surface.md`.

- Static a11y allowlists (C1..C26) shrink only: a new violation OR a stale entry fails. Fix the markup; never add an entry to pass. Baseline rewrite only with `LOC_TMS_A11Y_BASELINE_WRITE=1` and a reason.
- `tests/fixtures/axe/axe.min.js` is binary in `.gitattributes` and sha256-pinned in `verify_a11y_static`; never reformat it.
- A checkout with `core.autocrlf=true` has no eol rule for CSS/HTML, so merges can write CRLF: run the a11y suites after merging, before pushing.
- Never resolve CSS or template merge conflicts with a union script (it corrupted `style.css`); resolve by hand and grep for conflict markers. `memories.html`, `security.html` and page layouts conflict often.
- No inline `on*=` handlers (stored-XSS fix #179): use `data-` attributes and `app/static/handlers.js`; `verify_sec_xss` ratchets it. The script CSP is enforced.
- Password inputs use `autocomplete="new-password"` (or `current-password`); `autocomplete="off"` on a password field is forbidden.
- Busy controls use `aria-disabled` plus a click guard, not `disabled`; busy animations run only while a user action runs and stop under reduced motion.
- The live announcer keeps user-requested outcomes, last one wins.
- A new settings page card needs its a11y layout fixture. Page layouts are one per page type, saved per USER on the server.
- The secret scan cannot see past Jinja: no literal may follow a credential-like word plus colon inside a template expression; wrap the line.
- `?err=` redirects: known codes keep byte-identical values; free text is percent-encoded.
- Use `static_v()` for asset URLs (mtime cache-bust). No em dashes in any user-visible copy.
