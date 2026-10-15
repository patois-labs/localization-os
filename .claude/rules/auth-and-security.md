---
paths:
  - "app/main.py"
  - "app/web/auth*.py"
  - "app/web/security_headers.py"
  - "app/web/apikeys.py"
  - "app/web/scim.py"
  - "app/web/enterprise.py"
  - "app/services/security*.py"
  - "app/services/access.py"
  - "app/services/login_throttle.py"
  - "app/services/ratelimit.py"
  - "app/services/apikeys.py"
  - "app/services/mfa.py"
  - "app/services/password_policy.py"
  - "app/services/saml.py"
  - "app/services/oidc.py"
  - "app/services/ldap_auth.py"
  - "app/services/scim.py"
  - "app/services/trust_auth.py"
  - "app/services/audit*.py"
  - "app/services/pii*.py"
  - "app/services/egress.py"
  - "app/services/ai_optout.py"
---

# Auth, access, audit and privacy

Architecture: `docs/architecture/identity-and-access.md`, `tenancy-confinement.md`, `audit-chain.md`. ADRs 0009, 0010, 0022, 0023.

- `/api` skips Origin + CSRF by design (`app/main.py::_security_audit`), so no `/api` route may read `request.session`. `_csrf_ok` must never call `request.form()` (consumes the body, 422).
- `app/services/access.py` is the single visibility authority; do not add a second scoping path.
- Never enable `multi_org_enabled` on any install (ADR 0009). The super-admin is gone: `is_superadmin` stays dormant and is cleared at every boot.
- A local account with a password can never be taken over by SSO/LDAP sharing its email; non-local accounts lose local password sign-in.
- Audit is fail-closed: a request whose audit row cannot be written gets a retryable 503. The actor is never "anonymous" (`unauthenticated <ip>`, `refused <status>`, ...). Deleting a caller must keep the audit rows it wrote.
- Account lockout is per ACCOUNT in the DB: 20 fails/hour then 15 min doubling to 60, known-device bypass, admin unlock + email. The old in-memory per-process note is stale.
- Rate limits: 120/min on spend routes, 600/min on reads, admin settings, 0 = off.
- Client IP comes from one authority honouring `trusted_proxy_ips` only; a wrong value collapses every client to the proxy address.
- That authority is `security.client_ip(request)`: never read raw `X-Forwarded-For`, `CF-Connecting-IP` or any other proxy header anywhere else (login budgets and the trusted-network exemption depend on it).
- OIDC accounts link by (issuer, `sub`); email is used only for the FIRST link and only when `email_verified` is true or the admin trust switch is on. Never match by `preferred_username`.
- Disabling an account permanently revokes its sessions and API keys, including a SCIM token it owns (audited `scim.token_revoked`); re-enabling revives nothing.
- Every `next`-style redirect target goes through `oidc.is_unsafe_local_target`; success banners show only server-recorded text (`app/web/page_guard.py`), never raw query text.
- ORG-DEFAULT context cards (no customer/project/content-type scope) are admin-only for write and delete on every surface.
- The AI opt-out switch means "this instance's AI engines never process this project's content"; content exported or connected to the user's own AI tools (MCP) is outside it. Images sent to vision engines are not PII-redacted: say "text" in claims.
- PII redaction is a per-step choice (`WorkflowStep.redact_pii`); `pii.redact` restarts its placeholder counter per call (no seed map), so never merge redacted chunks by placeholder. The EMAIL regex is quadratic on long inputs.
- Never log raw typed usernames or POST bodies on auth failures (`ldap_auth._log_outage`, SAML ACS errors were fixed for this).
- Shared `lsk_` connector token is retired: per-user `lsu_` keys only. Optional key expiry `expires_at` (1 to 3650 days); enabling a key needs the admin scope.
