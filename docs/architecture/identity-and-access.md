# Identity & Access - Architecture (L1)

## 1. Role

Credential hardening (#225, #249, #250): current-password checks go through
`security.check_current_password`, reserving the sign-in address budget and a per-account
budget before hashing. Five failures lock checks for 15 minutes and revoke sessions;
correct guesses while locked are refused and audited. These budgets, like sign-in's,
are process-local. Password change has two web doors; no self-service API/CLI password
or email-change door exists. Admin resets remain separate privileged operations.

Invitations on web, API and CLI use `create_invite`, which refuses active accounts.
`accept_invite` checks again for links issued before activation. The public accept form
returns a generic refusal before checking password policy against any stored credential.

Local and LDAP self-service MFA setup require the current password before showing the secret;
its signed-session grant lasts five minutes. A local account with no password is refused
set-up (and turn-off) instead of skipping re-authentication (#423), and `mfa.applies_to`
treats a local row with a SCIM link and no password as an identity-provider account
(`security.signs_in_through_idp`): no local factor, never ended by the sign-in wall under
`mfa_required`, and SAML keeps signing it in, even when a factor was set up on it before
#423 (`_mfa_takeover_refusal` ignores a factor that `applies_to` says guards nothing). Forced enrollment uses the recent password
sign-in pending state. A started set-up secret belongs to the one flow that was shown it
(#421): the pending state or session keeps a one-way tag of it, a reload in that flow
resumes it, and any new password step, new self-service grant or other session gets a fresh
secret, so a person who opened set-up with a known password and left cannot plant the
secret the owner later scans. Enrollment and disabling bump the session epoch transactionally;
the acting session receives that exact epoch. Disabling also requires the current password,
checked before consuming the code. LDAP uses the existing directory bind with the same
per-address and per-account budget, without provisioning or falling back to a local hash.
Blank input spends no attempt. Checks serialize per account, never across all accounts.
SMTP notifications report enrollment, disabling and replacement recovery codes.
Without SMTP no email is sent. Pure SSO/SAML/OIDC accounts continue to use provider MFA;
they cannot enroll or disable a factor here. API/CLI expose only MFA status
and privileged reset, not self-service enrollment or disable. MCP deliberately excludes
these user-administration routes, including invitations and MFA status/reset.

This subsystem owns **who a caller is and what they may do** for locally-authenticated
identities. It covers four things: password authentication with signed-cookie sessions;
the role-based access-control (RBAC) ladder; per-user inbound API keys for the token
`/api` surface; and API-abuse defences (login lockout + per-identity rate limiting). It
also owns the one *tokenless* inbound door - the embedded **trust-host-auth** principal -
because that door resolves an `/api` identity, which is this subsystem's concern.

Authentication is gated by a single switch: with `setting:security_auth_required` off
(the default) the app runs as one trusted `system` operator and every RBAC predicate is a
no-op; with it on, login is required and roles are enforced. The RBAC ladder is
viewer < translator < reviewer < manager < admin (linguist shares the reviewer rank),
resolved by `app/services/security_client_ip_one_authority_.py::is_admin` and its sibling predicates, and the web
and `/api` surfaces mirror the *same* predicates so they cannot drift.

What it deliberately does **not** own. Directory-federated sign-in (OIDC, SAML, LDAP,
SCIM provisioning) and just-in-time account creation live in
[sso-provisioning](sso-provisioning.md); this subsystem only holds the two anti-takeover
seams (`app/services/security.py::get_or_create_sso_user` and
`app/services/security.py::get_or_create_saml_user`) that decide whether a federated login
may adopt an existing local account. Organisation/customer tenancy semantics - the
multi-org gate and fail-closed org resolution - live in
[tenancy-confinement](tenancy-confinement.md); this subsystem consumes org resolution
(`app/services/access.py::current_org_id`) but does not define it. Project *visibility*
scoping does live here, in `app/services/access.py`, because it is the membership/role
half of access control.

## Ingress boundary

### Project membership on every surface

With authentication enabled, `project_scoping` defaults to on. Translators, reviewers and
viewers can access projects they created or joined. Admins, managers and linguists retain
their existing visibility; customer-bound accounts see their customer's projects, and org
isolation still applies. `access.can_view_project` and its list twin `visible_project_ids`
own these rules.

Shared assets have a separate write ring (#468). Global workflows, workflow groups and prompt
templates (`customer_id` NULL), org-global style guides and language sets (org-level, no
customer column) are readable by a customer-bound account but not writable by it.
`access.customer_ring_blocks` is the read ring; `access.customer_write_blocks` is the write
ring, called by every mutating route through `api_cli_common._shared_write_block` (/api,
remote CLI, MCP) and the web `write=True` mode of `_wf_or_404`, `_grp_or_404`,
`_prompt_template_or_404` and the style guide `_visible_or_404` (plus
`_langset_write_block`). The refusal is a 403 with `access.SHARED_ASSET_READ_ONLY`. A bound
account's clone of a global workflow, and a workflow or group it creates with no customer, is
stamped with its own customer, so it can never add a global row either.

API keys carry their owner's current visibility. The remote CLI and MCP use the same API
guards, so minting a key does not widen project access. Restricted project resources return
the same 404 as missing resources. Lists exclude them before pagination. This includes
documents, segments, project TM and glossary, project workflows, background jobs, Ledger
records, and project-bound connector connections (web and `/api/connectors` lists, #474).
Text copied out of a project follows it too (#478): a glossary term candidate's
`context_snippet` and `source_document_id` are blanked for a caller who cannot see the source
document's project (`term_extraction.hidden_source_document_ids`), and a Lab dataset copied
from a document (`source_project_id`) answers 404 and drops out of lists for such a caller
(`evaluations.dataset_source_visible`).
Removing a membership takes effect on existing keys without reissuing them.

Advanced missions and runs use the same rule on web, API, remote CLI and MCP. A mission's
project must be visible to create or run it, or read its status, transcript and staged targets.
If a project is missing from a list, ask its owner or a manager to add you on the project's
Members panel. Turning project scoping off restores org/customer-wide visibility; turning
authentication off preserves the trusted local operator behavior.

`scripts/project_scope_routes.py` inventories the mounted API routes. The offline
`verify_project_scoping` suite exercises real non-member requests and ratchets call paths
to the shared visibility policy. Exact route exceptions and their reasons live in
`tests/project_scope_exceptions.json`; new ID routes require a guard or an explicit review.

Main-app security walls, feature removal, audit actions and error shaping use
`app.web.route_path.route_path`, a thin wrapper over Starlette's private
`get_route_path(scope)`, exactly like its router, including stripping
and non-stripping proxies with a non-empty root_path. The boundary normalizes
stripped paths before mounts extend root_path. SAML uses the external ASGI
path through `app/services/security_client_ip_one_authority_.py::request_path`, which deliberately reattaches
root_path and is not interchangeable with route_path. The private Starlette import
is isolated in app/web/route_path.py to contain upgrade risk. `verify_security_external_origin`
ratchets that single import; `verify_ingress_boundary` pins router equivalence.
None reconstruct security paths from Host or percent-decode a second time.

`setting:app_base_url` is the external URL authority through
`app/services/settings.py::external_base_url` and `app/services/settings.py::external_url`:
invitations, SAML, SCIM, UTCP, OAuth callbacks and operator guidance use it when set.
The request base URL (Host) is the fallback when unset; background notifications
still omit their link without a configured base. This does not change ingress trust,
authentication, feature gates or the Host allowlist.

The outer Host boundary validates HTTP and websocket authorities before routing.
Invalid, duplicate or missing HTTP/1.1 Host gets 400 (websocket closes with 1008).
`setting:security_allowed_hosts` pins case-insensitive, port-independent hostnames;
blank accepts valid syntax. There is no loopback exemption. The exact `/health` and
`/ready` probes bypass only the Host allowlist; other paths including metrics
scrapes need a listed Host. Malformed syntax always returns 400. Recovery:
correct the host's LOC_TMS_SECURITY_ALLOWED_HOSTS override (empty clears), or import
an operator TOML settings file with config apply / use config set, then restart.
The database Setting table holds persisted values. Invalid ingress overrides log
ERROR and resolve to empty with no stored fallback; /health detail reports
`ingress_config: invalid <key>`. Non-empty all-separator Host lists are invalid.
See DEPLOY.md for the host-only recovery steps.
The default pin stays empty. When `setting:app_base_url` is configured and the
effective pin is empty, `/health` detail includes `"host_pin":"unset"` and boot
logs one WARNING; otherwise that key is absent. The handler uses mirrored config
attributes and takes no DB connection. Non-empty Host list writes must include
the configured `app_base_url` host, case-insensitively and independently of port:
add that host, or clear/repoint `app_base_url`. Empty remains settable from the
host shell with `config set security_allowed_hosts ""`. Existing pins and
environment overrides retain syntax-only read validation; no pin is auto-populated.

`app/services/security.py::client_ip` is shared by deps and audit. Fly runtime selects
Fly-Client-IP only for socket peers in `setting:trusted_proxy_ips`; no peer is
trusted automatically. Off Fly, only socket peers in
`setting:trusted_proxy_ips` enable the right-to-left X-Forwarded-For walk, stopping
at the first untrusted address. A typical reverse-proxy install lists only the proxy's
single address, so the client address in the audit log is meant to be the one the CDN passed
through the proxy (decision [0037](../decisions/0037-trusted-proxy-ips-measured.md)). Bare IPs, IPv4:port and [IPv6]:port are supported.
All X-Forwarded-For header lines are joined in wire order before parsing.
The walk parses one hop at a time: a malformed or empty hop at or to the right of the
first untrusted address (where only a trusted proxy writes) returns empty identity, while
tokens to the LEFT of the resolved client are caller-controlled and ignored, malformed or
not (#289). With no identity, the audit detail keeps the right-most 128 characters of the
raw header, JSON-escaped and marked truncated. An absent header uses the peer; valid
caller-controlled hops to the left cannot override the resolved identity.
All uvicorn launches use --no-proxy-headers; the boundary alone handles scheme
and identity using trusted_proxy_ips, with no implicit loopback trust.
The access log reuses that identity: `_security_audit` stores `client_ip` in a
per-request holder (`app.main.ACCESS_LOG_CLIENT`) and the host boundary copies it
into the server's own scope at response start, which only uvicorn's access line
reads. The app's scope keeps the socket peer; liveness paths log the peer.

Both ingress settings are REMOTE_IMMUTABLE and SENSITIVE: host CLI confirmation
and env overrides only; every API config door and MCP refuse writes. Comma lists
tolerate trailing separators, but a non-empty Host list needs at least one host.
IPv4 prefixes shorter than /8 and IPv6 prefixes shorter than /32 are refused,
since broad proxy trust restores forged audit identity. The entrypoint disables
uvicorn forwarding; only the application reads proxy trust. Proxies must append
or overwrite with the actual sender;
restrict direct access. `verify_ingress_boundary` owns the regressions.

## 2. Invariants

- **Concurrent account changes preserve the last active administrator (#400).**
  `app/services/admin_invariant.py::lock_user_mutations` serializes GDPR erasure,
  role changes, deactivation (including SCIM), and organization moves before
  reading the target and counting administrators. The lock lasts through commit
  or rollback. SQLite reserves its database writer; PostgreSQL uses a transaction
  advisory lock and requires READ COMMITTED isolation. This lock is install-wide,
  including multi-org installs, so cross-org moves need no lock ordering. Counts
  remain scoped to the target's own org when multi-org is active, including NULL
  orgs, and install-wide otherwise; SCIM also retains its explicit org scope.
  Erasure revokes API keys in the same transaction as the scrub. Web, CLI and API
  inherit the shared service guards. `tests/verify_admin_mutation_race.py` covers
  competing removals and mixed operations; its PostgreSQL matrix is opt-in.

- **Password sign-in has two lockout layers.** The per-(typed name, source address)
  layer blocks five failures in 15 minutes for 15 minutes. It stays in process memory;
  native IPv6 shares a /64 and mapped IPv4 shares the IPv4 budget. Names enter this
  layer only as SHA-256 digests of the normalized name, its entries expire past their
  window and every limiter dict is capped globally at 10,000 keys on admission and
  settlement (#384). Pending checks pin their entries; admission refuses when all
  capacity is pinned. A source address with 200 completed failures plus pending
  checks across all names is refused before any verifier runs. Success and unchecked
  or error outcomes release pending capacity. Completed failures expire with the
  window; pending checks remain counted until settlement or until PENDING_TTL_S
  (5 minutes) passes, so a reservation leaked by a server fault frees its slots on its
  own instead of holding them until restart (#411). An administrator can list
  shared-NAT egress networks in `login_trusted_networks` (comma list of IPs or CIDRs,
  IPv4 /8 or narrower, IPv6 /32 or narrower; a sensitive setting, confirmed on write):
  a trusted source skips ONLY the 200-failure source budget. The per-(name, source)
  lockout, the MFA code budget and the account lockout still apply to it. An empty,
  malformed or too-broad stored value trusts nothing and is named in the startup log
  (`security.check_login_trusted_networks_at_boot`); IPv4-mapped IPv6 entries are refused
  on write. The exemption keys on `security.client_ip`, so a forwarded header counts only
  from a `trusted_proxy_ips` peer. Doors: Security page (Access control), `config set`,
  /api config, MCP `manage_install set_config` (#411). The synthetic reauthentication
  account bucket enforces each account's budget without aggregating unrelated accounts.
  Rotating names cannot buy unbounded work (#384). The sign-in form refuses
  a typed name longer than 254 characters (EMAIL_MAX_LEN) with the ordinary
  invalid-credentials answer, before any lookup, reservation or verifier touches it
  (#384). Above it,
  `app/services/login_throttle.py` persists a rolling 20-failure, 60-minute account
  budget in `LoginThrottle`. The first lock lasts 15 minutes; consecutive locks double
  to 30 then 60 minutes, capped at 60. Successful sign-in resets the account budget.
  Reservations precede local verification and LDAP binding; conditional updates prevent
  concurrent guesses from overshooting. Tracked unknown typed names receive the same
  budget and refusal. Normalized names are hashed before storage. Only UNKNOWN-name
  creation is capped at 100 per source address per day and 10,000 global admissions
  per day. Over-quota unknown names still run ordinary password verification and
  receive the identical failed-sign-in status/body and Argon2 work as a wrong password
  for a real account (apart from echoed name and CSRF token), without creating a row.
  Local users and directory-resolved canonical identities always receive a row,
  regardless of creation quotas. LDAP aliases reserve the canonical budget before bind.
  The scheduler purges expired probe rows and admission buckets. Real accounts retain escalation.
  A signed, user-bound known-device cookie lasts 30 days (HttpOnly, SameSite=Lax, Secure
  when SECURE_COOKIES is enabled or on HTTPS). Its MAC binds the user ID, email,
  creation time and session epoch, so password changes, admin resets, logout and user
  deletion invalidate it, including when a deleted ID and email are reused.
  Issued only after complete sign-in, it bypasses the account layer while
  leaving the address layer enforced. An administrator can use Users > Unlock sign-in,
  `POST /api/users/{uid}/unlock`, host CLI `user unlock <email>` (break-glass), or MCP
  `manage_users` with action `unlock`. All create-or-reset the account row, as does
  admin password reset. Host break-glass always clears the account layer even with
  no existing row and exhausted creation quotas; CLI success requires a committed
  clear. The address and MFA limits remain separate. Lock/unlock actions are audited;
  lock actor is the anonymous requester, and target is the victim account. A lock
  emails the owner when mail is configured. The MFA
  account code budget remains separate. OIDC and SAML authentication are unchanged.
- **One password policy is enforced at every public seam.**
  `app/services/security_client_ip_one_authority_.py::password_issue` is the one authority;
  it applies `app/services/password_policy.py::issue` (minimum length, required character
  classes, the bundled common list (compared on a normalized core, plus repetitive and
  sequential strings) with the email / username / full product-name rule, the always-on
  "not your current password" rule, and the
  reuse history) and returns one plain sentence naming the broken rule. It is called from
  create_user, admin_set_password, change_password, the setup wizard, invite accept and the
  forced change page, so account creation, admin reset (web-less: CLI and /api), and
  self-service change all inherit it (dev seeders opt out explicitly). Every local password
  is written by `app/services/password_policy.py::record_password` (hash, max-age clock,
  history). SSO / LDAP / SAML / SCIM accounts have no local password and are never checked.
- **A password that must change blocks everything else.** After an admin reset
  (`password_must_change`) or past `password_max_age_days`,
  `app/services/password_policy.py::change_required` makes the sign-in completion in
  `app/web/auth_mfa.py::complete_sign_in` set the session flag `pwc` (after the second factor,
  when there is one); the sign-in wall in
  `app/main.py` then sends every page to `/auth/password-change` until
  `app/web/auth.py::password_change_post` succeeds (or the person signs out).
- **No session exists before the second factor (TOTP).** Every
  password door (the sign-in form for local and LDAP accounts, and the setup wizard) ends in
  `app/web/auth_mfa.py::finish_password_step`. When two-step sign-in is on for the account,
  or `setting:mfa_required` makes it mandatory, that stores only a signed, short-lived
  pending state in the session (user id, session epoch, issued time; at most
  `app/services/mfa.py::PENDING_MAX_SECONDS`) and never a `uid`, because every `/auth/*`
  path is exempt from the sign-in wall and a "signed in plus a flag" design would leak.
  Only `app/web/auth_mfa.py::complete_sign_in` calls `login`. Wrong codes count toward the
  same (email, ip) lockout as wrong passwords, and the counter is cleared only when the
  whole sign-in succeeds. A code must be for a later 30-second step than the last accepted
  one (`app/services/mfa.py::verify`, one conditional UPDATE), recovery codes are Argon2id
  hashes used once, and the secret is a Fernet ciphertext. An admin reset
  (`app/services/mfa.py::reset`) bumps the session epoch in the same transaction.
  On top of the per-IP lockout, every code check (sign-in, set-up, new recovery codes,
  turning it off) spends a PER-ACCOUNT budget stored on the `UserMfa` row
  (`app/services/mfa.py::ACCOUNT_FAIL_LIMIT` wrong codes in 15 minutes from any address or
  spelling of the name lock every code check for 15 minutes, audited `mfa.locked`, and
  the owner is emailed when SMTP is configured; it survives a restart). Every attempt is
  RESERVED before its code is checked (`app/services/mfa.py::verify` takes one atomic
  conditional UPDATE first; the per-address count does the same under a lock in
  `app/services/security_client_ip_one_authority_.py::reserve_login_attempt`), so a burst
  of concurrent requests never checks more than the limit. A check that raises (a database
  or decrypt fault) gives both reservations back and re-raises, so a server fault never
  counts as a wrong code or password; a code refused unchecked because the budget is held
  (`app/services/mfa.py::REFUSED`) is shown the locked message and counts no per-address
  failure. Recovery codes (about 8 x 10^14
  values, Argon2id) bypass the per-account lock and do not count toward it, so the real
  owner can always get in while someone holding the password keeps it locked; the
  per-address limit still applies to them. **Recovery order when abuse is suspected** (an
  unexpected `mfa.locked`, or a lost phone together with a leaked password): an admin
  resets the person's PASSWORD first, then their two-step sign-in. Resetting two-step
  sign-in alone under `mfa_required` lets whoever next signs in with the old password
  enroll their own authenticator. The policy is enforced on every request, not only at sign-in: the
  wall in `app/main.py` ends a session that never proved a second factor
  (`app/web/auth_mfa.py::VERIFIED_KEY`) as soon as `mfa_required` applies to its user (a
  raised policy or a promotion to admin), audited `mfa.session_ended`. OIDC/SAML never adopt
  an account with two-step sign-in on (`app/services/security.py::_mfa_takeover_refusal`,
  audited `login.sso_refused`). Every page with a code form, the setup key or recovery codes
  is sent `Cache-Control: no-store`.
- **Sign-in costs the same whether or not the account exists.**
  `app/services/security_client_ip_one_authority_.py::authenticate` runs
  `app/services/security_client_ip_one_authority_.py::dummy_verify` (one Argon2 verify
  against a fixed hash) for an unknown email, a disabled account and an account without a
  local password, so response time is no account-existence oracle.
- **A placeholder address is never an identity (#520).** The app writes stand-in addresses at
  `dedup.invalid` (migration `a1f4c8b2d6e9`: a retired duplicate, or an emailless account kept
  active) and `erased.invalid` (GDPR erasure).
  `app/services/security_client_ip_one_authority_.py::is_placeholder_email` makes `get_by_email`
  resolve nobody for them, so password sign-in, invite acceptance and OIDC/SAML/LDAP/SCIM
  linking treat them as unknown; `validate_email` and `create_user` refuse them, and
  `app/services/mailer.py::send` never hands one to the relay. An emailless account still
  signs in through an OIDC (issuer, subject) binding, exactly as before the migration; it
  never signs in by typing its placeholder. Enforced by `verify_placeholder_email_refused`.
- **API keys are stored hashed and shown once.** `app/services/apikeys.py::create_key`
  returns the plaintext key exactly once; only its sha256 digest is stored, and
  `app/services/apikeys.py::verify_key` looks up by digest - enforced by `verify_apikeys`.
- **Key scopes are validated at mint, and `admin` is local-only.** An unknown grant is
  refused by `app/services/apikeys.py::validate_scopes` against
  `app/services/apikeys.py::ALL_SCOPES`; the `admin` grant is never mintable over `/api`,
  so a stolen key can never widen its own reach.
- **Keys carry an optional expiry and an install-level maximum age (#270
  r2-apiauth-10).** `ApiKey.expires_at` is nullable: NULL = no expiry, so every
  already-issued key keeps working unchanged. A past-or-equal `expires_at` is
  refused in `app/services/apikeys.py::verify_key` exactly like a revoked key
  (same 401 shape; nothing reveals the key existed). All mint surfaces accept an
  optional lifetime and show the expiry; the setting `apikey_max_age_days`
  (blank = no maximum, the default) caps NEW mints at the one shared seam
  (`app/services/apikeys.py::resolve_expiry`) with a clear refusal.
- **Account-security changes over `/api` need the `admin` scope (#270
  r2-apiauth-10).** Creating a user, changing a role, resetting a password,
  resetting MFA, creating an invite (it creates users with a role, admin
  included) and re-enabling a disabled account (an account-security action; since #422
  disable revokes its keys and ends its sessions, so enable revives none) are gated by
  `_gate_admin`; a
  docs-scoped (MCP) key gets the
  standard "This endpoint requires an admin-scoped user API key." refusal.
  Web UI sessions for admins are unaffected (this is API-key scope only).
- **A federated login never silently takes over a local-password account (finding F5).**
  Cross-ref `app/services/security.py::get_or_create_sso_user` and
  `app/services/security.py::get_or_create_saml_user`: an existing local-password (or
  trust) identity is refused, not adopted.
- **Only a local account has a local password (train sso-localpw).** Who can change a
  password? A person whose account signs in with a password stored here (`auth_source`
  "local" with a password) changes it on the Account page or at `/auth/password-change`,
  and must type the current one. Everyone else is refused with a plain sentence and an
  audited `account.change_password` row whose detail says `refused: ...`, and the Account
  page shows that sentence instead of the form:
  `app/services/security_client_ip_one_authority_.py::password_change_refusal`. SSO, SAML
  and SCIM accounts change their password at the identity provider; LDAP accounts change
  their directory password in the directory (a local change would plant a second
  credential). `app/services/security_client_ip_one_authority_.py::authenticate` accepts a
  local password only when
  `app/services/security_client_ip_one_authority_.py::signs_in_locally` is true, so a hash
  left on an identity-provider account (the old self-service bug, or a pre-F5 adoption)
  never signs it in, so a person disabled at the IdP has no local way in unless an admin
  resets their password. The auth_source is normalized once
  (`app/services/security_client_ip_one_authority_.py::auth_source_of`: stripped,
  lowercased, blank = local) for these decisions and for their neighbours: the F5 adoption
  checks, `app/services/mfa.py::applies_to`, `app/services/password_policy.py::change_required`
  and the trust-principal boot check. During an LDAP outage the
  sign-in form still falls through to local authentication, so local accounts (the seeded
  admin included) sign in; an LDAP account does not, even with a stray local hash.
- **Only an admin reset moves an account off its identity provider, and never a trust
  identity.** `app/services/security_client_ip_one_authority_.py::admin_set_password` (CLI
  `user reset-password`, `/api/users/{uid}/reset-password`; admin-only over `/api` by the
  `admin` scope + admin role, with a named admin actor) switches an SSO / SAML / SCIM /
  LDAP account to local, audited
  `user.auth_source_changed` (`sso -> local (admin password reset)`). After that the IdP no
  longer signs the account in (the F5 rule above); a SCIM deactivation (`active` false) still
  blocks it, and nothing but an admin re-enables it. An invitation never does either:
  `app/services/security_client_ip_one_authority_.py::invite_refusal` makes
  `app/services/security_client_ip_one_authority_.py::create_invite` refuse to issue, and
  `app/services/security.py::accept_invite` refuse to accept (revoke the invitation, audit
  `user.invite.refused` with the caller IP, show the sentence), an invitation for an account
  that signs in through an IdP
  (`app/services/security_client_ip_one_authority_.py::signs_in_through_idp`: a non-local
  auth_source, or a SCIM link on a row with no local password, which is how a pre-created
  local row looks after SCIM adopts it and SAML signs it in) and for an inactive account that
  has a password or a SCIM link, because the link may predate the account joining the IdP or
  being deactivated, and whoever holds it picks the password. The pre-created inactive local
  row with no password and no SCIM link (an admin creates it, then invites) is still
  activated, and a SCIM-adopted local row WITH a password may still be re-invited. The
  trust-host-auth principal (any `trust` row) is never reset or
  invited (audited `user.password_reset` or `user.invite.refused`, detail
  `refused: trust service identity`): it holds no password, and a switch to local would
  stop the next embedded boot (`app/services/trust_auth.py::configure_at_boot`). That boot
  re-asserts only a row whose sign-in source is exactly `trust`; a hand-edited spelling
  such as ` Trust ` is refused with an error naming the value, never repaired (repairing
  would re-activate a row someone disabled). The rotation cleanup
  (`app/services/trust_auth.py::_deactivate_superseded_trust_users`) matches the normalized
  value, so such a row is still deactivated once it is no longer the principal.
- **The tokenless door is doubly-gated and can only subtract trust.** Trust-host-auth is
  active only under `app/services/trust_auth.py::active` (an embedded boot *and* the
  switch), and `app/services/trust_auth.py::source_permitted` can turn a would-be-trusted
  request into a 401 but never the reverse - enforced by `verify_trust_host_auth`.
- **`/api` mirrors the web RBAC predicate through explicit scope gates.**
  `app/web/deps.py::_require_admin_scope` and `app/web/deps.py::_require_doc_scope` gate
  the sharp surfaces; the shared connector token is rejected outright by both.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Account lockout | `app/services/login_throttle.py` | Durable password budget and device cookies. |
| Canonical request path | `app/web/route_path.py::route_path` | Isolates the private Starlette path API used by web ingress and SAML handoffs. |
| Password login form | `app/web/auth.py::auth_login_post` | Handles lockout, LDAP-then-local auth, and session establishment on the login POST. |
| Authentication + RBAC core | `app/services/security_client_ip_one_authority_.py::authenticate` · `app/services/security.py::session_user` · `app/services/security_client_ip_one_authority_.py::is_admin` | Verifies a password, resolves the current user, and answers the admin predicate. |
| Administrator mutation serialization | `app/services/admin_invariant.py::lock_user_mutations` | Database transaction lock shared by erasure, role, active-state and org changes. |
| Session hygiene + lockout | `app/services/security_client_ip_one_authority_.py::touch_session` · `app/services/security_client_ip_one_authority_.py::reserve_login_attempt` · `app/services/security_client_ip_one_authority_.py::settle_login_failure` | Enforces idle timeout on each request, reserves each login attempt before the check, and settles failed logins toward the lock. |
| Session lifetime (a11y T9) | `app/services/security_client_ip_one_authority_.py::session_clock` · `app/services/security_client_ip_one_authority_.py::enforce_session` · `app/services/security.py::session_status` · `app/web/auth.py::auth_session` · `app/web/auth.py::auth_keepalive` | Idle + absolute expiry from one clock; the read-only status and the CSRF-protected keepalive behind the timeout warning. |
| Project visibility + membership | `app/services/access.py::visible_project_ids` · `app/services/access.py::can_view_project` · `app/services/access.py::same_org` | The single authority for which projects a user may see (membership + org + customer rings). |
| API keys (service) | `app/services/apikeys.py::create_key` · `app/services/apikeys.py::verify_key` · `app/services/apikeys.py::validate_scopes` · `app/services/apikeys.py::ALL_SCOPES` | Mints, verifies, and scope-validates per-user inbound keys; the scope vocabulary. |
| API keys (self-service web) | `app/web/apikeys.py::apikeys_create` · `app/web/apikeys.py::apikeys_revoke` | The `/account` routes that mint and revoke a signed-in user's own keys. |
| One-time secret display | `app/web/one_time_stash.py::put` · `app/web/one_time_stash.py::pop` | Keeps a new API key or invite link server-side for one render; only a random reference rides in the signed, unencrypted session cookie (#476). |
| Inbound `/api` identity + gates | `app/web/deps.py::api_identity` · `app/web/deps.py::_require_admin_scope` · `app/web/deps.py::_require_doc_scope` · `app/web/deps.py::_rate_limit_block` | Resolves the `X-Loc-Token` identity and applies the scope + rate-limit gates. |
| Per-identity rate limit | `app/services/ratelimit.py::check` | Sliding-window request cap keyed on the calling identity. |
| Blocking work off the event loop | `app/web/deps.py::offload_parse` · `app/web/deps.py::offload_blocking` | An async route awaits every engine call, connector run and file parse on the bounded stream-worker pool (503 with Retry-After when full), never on the event loop, so one slow request cannot stall /health (#599; ratchet in verify_pathological_text_599). |
| Trust-host-auth (tokenless) | `app/services/trust_auth.py::configure_at_boot` · `app/services/trust_auth.py::principal` · `app/services/trust_auth.py::source_permitted` | Boot-validates and resolves the embedded trust principal; the shared-secret source gate. |
| Password policy | `app/services/password_policy.py::issue` · `app/services/password_policy.py::record_password` · `app/services/password_policy.py::change_required` · `app/web/auth.py::password_change_post` | The configurable rules behind password_issue, the one local-password writer (hash, clock, reuse history), and the forced change at sign-in. |
| Two-step sign-in (service) | `app/services/mfa.py::verify` · `app/services/mfa.py::confirm_enrollment` · `app/services/mfa.py::reset` · `app/services/mfa.py::policy` · `app/services/mfa.py::required_for` | RFC 6238 codes, the encrypted secret, replay refusal, recovery codes, the admin reset and the mfa_required policy. |
| Two-step sign-in (web) | `app/web/auth_mfa.py::finish_password_step` · `app/web/auth_mfa.py::complete_sign_in` · `app/web/auth_mfa.py::mfa_verify_post` · `app/web/auth_mfa.py::mfa_enroll_post` · `app/web/auth_mfa.py::account_mfa_disable` | The pending state after a password, the code step, forced set-up, and the Account page self-service. |
| Identity CLI | `app/cli/cmd_user.py::register` | The `user` / `apikey` / `member` command groups (create, list, role, keys, membership). |

Invitation lifecycle: `app/services/security_client_ip_one_authority_.py::revoke_invite` invalidates and audits a live token, `app/services/security_client_ip_one_authority_.py::create_invite` supersedes prior live invitations for the normalized email within the stored org when multi-org is enabled. Invite.org_id is stamped from caller context, independent of mutable inviter email; NULL ownership belongs only to NULL/operator scope under the gate, and gate-off access is global. Expired invitations appear separately on the Users page, CLI `user invites --expired`, and API `GET /api/users/invites?expired=1`. Users-page Resend revokes the selected pending/expired invitation and creates its replacement through the same service seams, auditing both with the request IP. Users-page Delete (expired rows, confirm first), API DELETE and CLI `user invite-revoke` also accept an expired invitation: a soft delete (`revoked_at`, row kept), audited as `user.invite.revoke` with detail `expired`.

## 4. Data & settings

- **Models:** `app/models.py::User` - accounts, role, `auth_source`, active flag, and org
  / customer binding. `app/models.py::ApiKey` - the sha256 key digest, prefix, and scope
  string (a key has no org column; it belongs to its owner's org). `app/models.py::Invite`
  holds the email invitation handshake. `app/models.py::PasswordHistory` holds the Argon2id hashes
  of an account's previous local passwords (the reuse rule; never exported, deleted by GDPR
  erase); `User.password_changed_at` and `User.password_must_change` carry the max-age clock
  and the admin-reset flag (alembic `e5a1c8d3f7b2`). `app/models.py::UserMfa` (one per
  account: the Fernet-encrypted TOTP secret, `enabled_at`, the replay guard `last_step`) and
  `app/models.py::MfaRecoveryCode` (Argon2id hashes, single use) hold two-step sign-in
  (alembic `c3d9a6f1e2b7`); both are credential material, never exported, deleted by GDPR
  erase and by an admin reset. There is **no** session table: sessions are
  signed-cookie only (Starlette `SessionMiddleware`), and the synchronizer CSRF token is
  minted per session by `app/web/deps.py::csrf_token`.
- **Settings:** `setting:security_auth_required` (default `"0"`) - the master authn/RBAC
  switch. `setting:session_idle_minutes` (default `"240"`, `0` = off) - the idle timeout.
  `setting:session_max_hours` (default `"24"` since a11y T11, was `"20"`; `0` = no separate limit): the absolute
  session limit, counted from sign-in and never renewed (a11y T9). Validated to 0..24 hours
  (idle: 0..1440 minutes) on every write door, because the session cookie's signature window
  (`app/main.py::SESSION_COOKIE_MAX_AGE`, 24 h) bounds both. Both are on the web Security
  page's Sessions card as well as `config set` and /api config, with the same validators.
  Password policy: `setting:password_min_length` (`"12"`, 8..128),
  `setting:password_require_classes` (`"0"`, 0..4), `setting:password_block_common` (`"1"`),
  `setting:password_history` (`"5"`, 0..24) and `setting:password_max_age_days` (`"0"` =
  never). On the Security page's Passwords card, `config set`, /api config and MCP
  set_config; "password" in their names does not make them secrets
  (`app/services/settings.py::NOT_SECRET_KEYS`).
  Two-step sign-in: `setting:mfa_required` (`"off"` default, `"admins"`, `"all"`; a
  SENSITIVE key) decides who MUST use it; anyone signing in with a password may turn it on
  for themselves. On the Security page's Two-step sign-in card, `config set` and /api config.
  OIDC/SAML accounts are never asked (their identity provider's MFA applies); API keys are
  unaffected; there is no remember-this-device.
  `setting:api_rate_limit_per_minute` (default `"120"`) caps uploads and engine work.
  `setting:api_read_rate_limit_per_minute` (default `"600"`) caps reads and cheap writes.
  Each authenticated `/api` request counts once in its per-identity budget; 0 disables
  either. Both accept at most 100000 requests/minute; corrupt stored limits use the default.
  The Security page, API admin config, and CLI config expose both settings.
  `app/services/ratelimit.py::api_budget` classifies POST engine action suffixes
  (`/translate`, `/run`, `/sync`, and the other `SPEND_ACTIONS`) as spend, so new factory
  connectors inherit the spend budget. `SPEND_ROUTES` covers irregular paths such as uploads
  and embedding writes. A live-route engine-call ratchet checks both forms of classification.
  Slash-only routing redirects do not consume a budget; the followed request counts once.
  Unauthenticated requests and session-only web requests have no API identity budget.
  `setting:sso_default_role` and `setting:saml_default_role` (both `"translator"`) set
  the role a first-time federated sign-in is granted, consumed by the anti-takeover seams
  above.

Session invariants (#138): there is still no session table and no revocation list, but a
session is no longer purely stateless. Every `app/models.py::User` row carries a generation
counter (`session_epoch`), and `app/services/security_client_ip_one_authority_.py::login`
stamps the current value into the session as `se`.
`app/services/security.py::session_user`, the one seam every web request resolves through,
refuses any session whose `se` is missing or does not match the user row, exactly as if the
session had been logged out. `app/services/security_client_ip_one_authority_.py::bump_session_epoch` is the ONE seam that
raises it, and it is the only place the arithmetic lives: a single atomic SQL increment that
reads the resulting value back in the same transaction, so two revocations that overlap each
count (a read-modify-write would let the second logout be a silent no-op). Logout,
self-service password change and admin password reset all call it, so **logging out (or
changing the password) invalidates every outstanding cookie for that account on every
device**, not just the copy in the browser that asked. A captured or cached pre-logout cookie
is therefore dead. For a password change the bump rides in the SAME transaction as the new
hash, so the credential and the revocation land together or not at all. The one exception is
deliberate: a self-service change through `app/web/auth.py::account_password` re-stamps the
session that performed it, with the exact epoch that change produced (never one re-read
afterwards), so only the OTHER devices are signed out. An admin RESET
(`app/services/security_client_ip_one_authority_.py::admin_set_password`) does not re-stamp
anything, so **an admin who resets their own password signs themselves out with it**; that is
intended, not a bug. Deactivation ENDS credentials (#422): `set_active(..., False)` (the
Users page toggle, `/api/users/{uid}/disable`, CLI `user disable`, SCIM DELETE) and the SCIM
PUT/PATCH `active: false` branch call `security.end_credentials` in the same transaction as
the flag, which bumps the epoch and revokes every active API key of the account. Re-enabling
therefore revives nothing: the person signs in again and creates new keys. (Before #422 the
`active` check only suspended old cookies and keys, so re-enabling brought every one back.)
Disabling the account that owns the SCIM bearer token revokes that token too, for good (it
is an admin-equivalent credential): identity provider sync with it stops until an admin mints
a new SCIM token for another active admin. The Users page toggle asks first for such an
account, the CLI prompt and output and the `/api` reply (`scim_tokens_revoked`, `warning`)
say so, and every such revocation writes a `scim.token_revoked` audit row, including when
the IdP deprovisions the token's own owner (that request is honored: offboarding wins). The
two other `User.active` writers are unchanged: GDPR erase already revokes keys and scrubs
the row, and trust-principal rotation (`trust_auth._deactivate_superseded_trust_users`)
still only flips the flag, so rotating back to an old principal re-asserts its row with
its old keys (operator-only, host env change). If the revocation itself fails, `logout` raises BEFORE clearing the client cookie and
the web seam answers a clean error: a session is never shown as signed out while it is still
live elsewhere. API keys (`X-Loc-Token`) are not sessions and are unaffected.
There used to be a SECOND session here: the back-office console had its own middleware and
cookie, and #143 made it honour this same counter so a revocation crossed both ways. The
console was removed on 2026-09-20, so there is one session cookie again and the
cross-surface revocation question no longer arises. One-time upgrade effect: sessions minted before this change carry no `se` at all, so anyone signed in
at the moment of the upgrade is asked to sign in once more. That happens once, not repeatedly.

Session lifetime (a11y T9, WCAG 2.2.1): `login` stamps `la` (last active) and `iat`
(issued at) from ONE clock, `app/services/security_client_ip_one_authority_.py::_now`;
`iat` is stored nowhere else. The login wall calls `enforce_session`, which clears the
session when it has been idle longer than `session_idle_minutes` OR when
`session_max_hours` have passed since `iat`, and otherwise refreshes `la` only. The
starlette cookie's own `max_age` (24 h since T9, 12 h before) slides with every write, so it
is a backstop, not an absolute limit; `session_max_hours` is. A session with no `iat`
(minted before T9), or one whose `iat` lies more than 300 s in the future, is expired while
the absolute limit is on, so everyone signs in once after the upgrade. Drafts of unsaved
text kept after a timeout carry the signed-in user id and are only offered back to that
user; an explicit "Sign out now" keeps no draft. The warning (dialog + `session_timeout.js`)
loads from `base.html` and from the one standalone signed-in page,
`mfa_codes_standalone.html` (the forced-enrollment recovery codes, #408), so a timed-out
recovery-code page is hidden too.
`GET /auth/session` (`session_status`) reads a COPY of the session and never writes it,
so the warning's polling cannot keep a session alive; `POST /auth/keepalive` is an
ordinary CSRF-checked /auth POST (same-origin + synchronizer token) and reports
`remaining_seconds` as the smaller of the idle and absolute remainders. An expired
request that carries `X-CSRF-Token` (fetch) gets `401 {"error": "session_expired"}`;
a navigation keeps the 303 to `/auth/login?...&expired=1` (plus `reason=max` for the
absolute limit), and an expired form POST's `next` is the sanitized same-origin Referer
page, never the POST route (`app/main.py::_expired_next`).

Security review 2026-09: the limits are also enforced INSIDE
`app/services/security.py::session_user` (#247), which reads the pure `session_clock` and
treats a session past either limit as signed out (and clears it). The wall skips every
`/auth` path, so before this `/auth/password-change` accepted a session the product had
already ended. Resolving the user never refreshes `la`; only `enforce_session` does.
Sign-out (#251) is `POST /auth/logout` behind the same-origin + synchronizer CSRF checks:
it ends every session of the account (the epoch bump above), so another site must not be
able to trigger it by navigation. `GET /auth/logout` changes nothing: a signed-in person
gets a confirmation page (`logout_confirm.html`) whose button posts the form, anyone else is
sent to `/auth/login`. The profile menu, the forced password-change page and the session
dialog's "Sign out now" are all POST forms. When a session times out, the signed-out
dialog cannot be dismissed and the page behind it is hidden (#260), and every HTML response
to a signed-in session carries `Cache-Control: no-store` (`app/web/page_guard.py`).
The sign-in wall and the sign-in page ask `security.has_users()` (a one-row existence
probe) instead of loading every account (#252); `user_count()` is a `count(*)`.

First-run setup (#414). `/auth/setup` and `seed_admin` (the `init` CLI) create the first
account through `create_user(..., first_account_only=True)`: it takes the install-wide
user-mutation lock (`admin_invariant.lock_user_mutations`, the SQLite writer reservation or
the PostgreSQL advisory lock) and re-checks "no account yet" in the insert's own
transaction, so concurrent submissions create exactly one admin (`FirstAccountExists` for
the rest, sent to `/auth/login`). In server posture (`LOC_TMS_SERVER=1` or a non-loopback
bind, never test mode) the wizard also needs a one-time setup token
(`security.setup_token_ok`): `LOC_TMS_SETUP_TOKEN` when it holds 16+ characters, otherwise
a random value generated at startup into `DATA_DIR/.setup_token` (0600) and printed once to
the server log. The file is removed once an account exists. Loopback single-user installs
never ask for it.

## 5. Enforced by

- `verify_docker_auth`: a server install's first-run wizard: the setup token (log and
  file, 403 without it, file removed afterwards) and concurrent setup posts creating
  exactly one admin (#414).
- `verify_session_timeout`: the T9 invariants: read-only `/auth/session`, the
  CSRF-protected keepalive, `iat` at every sign-in path, the absolute limit, 401 JSON for
  an expired fetch and the Referer-based `next`; since the 2026-09 review also the
  limits on `/auth/password-change` (#247), POST-only sign-out (#251) and `no-store`
  on signed-in pages (#260).
- `verify_security` - password policy, hashing, lockout, session idle, the client-IP
  binding, and the non-ASCII-safe constant-time compare.
- `verify_accounts` - the create/role/enable/reset user lifecycle across web + CLI.
- `verify_password_policy`: every rule, every door (web setup, invite accept, account
  change, forced change, admin create, CLI, /api, MCP set_config), history, max age and the
  admin-reset forced change, settings validation on every door, audit rows, GDPR export and
  erase, escaping, and SSO/LDAP accounts unaffected.
- `verify_totp_mfa`: RFC 6238 vectors, no session before the code (walled pages,
  password change, keepalive), replay refused, the shared lockout, the 5-minute window,
  single-use recovery codes, admin reset on web / `/api` / CLI bumping the epoch,
  mfa_required admins vs all with forced set-up (setup wizard, invite then sign-in), LDAP
  required, OIDC and API keys unaffected, Account self-service, the audited setting, GDPR
  erase, and the dummy verify for unknown accounts.
- `verify_roles` - the RBAC ladder predicates and their web/`/api` parity.
- `verify_apikeys` - key mint/verify/scope-validation and the once-only plaintext rule.
- `verify_apikey_expiry_admin_scope`: the optional key expiry (expired = revoked
  exactly, not-yet-expired and no-expiry keys work, the `apikey_max_age_days` cap on
  every mint surface, expiry shown on the listings) and the admin-scope gate on the
  account-security routes (user create, role, re-enable, password reset, MFA reset,
  invite).
- `verify_membership` - project membership CRUD and `visible_project_ids`.
- `verify_identity_port` - the Identity port conformance the auth seam satisfies.
- `verify_trust_host_auth` - the embedded tokenless door: the double condition, the
  source-secret gate, the boot-time validation, and the fail-closed behaviour.

## 6. Battle scars

- **Trust-mode hardening.** The tokenless trust door was tightened with a host-injected
  shared-secret source restriction, a hard-coupled attribution annotation, and fail-loud
  boot observability, so accidental exposure of a tokenless `/api` port does not grant
  trust to every peer that can reach it.
- **The admin/identity surface held under adversarial probing.** A full-test pass over
  ~150 identity/admin/config probes (scope enforcement, the remote-admin triple gate,
  cross-tenant gates) passed with zero deviations.
- **Full `/api` regression stayed clean.** A sweep of every `/api` route found no 5xx,
  auth, scope, role, or confinement leak.
- **Controls are inventoried against real frameworks.** The AuthN/AuthZ + RBAC controls
  are mapped to code evidence.

## 7. Limits & accepted risks

- **Address lockout and API rate limits remain in-memory, per process.** A restart
  clears those windows. Password account locks and MFA account code locks persist in
  the database across restarts and processes. Lock email is queued in the database and
  sent by the scheduler so SMTP timing does not reveal whether a typed account exists.
- **Forced two-step set-up trusts the first sign-in.** When `mfa_required` applies to an
  account that has not set it up, whoever next signs in with the password sets it up (an
  abandoned, unconfirmed set-up is never shown to the next person, #421). The
  hosted-instance runbook therefore turns it on and enrolls the first admin immediately;
  after an admin reset the same window reopens until the person signs in. A sole admin who
  loses the phone and every recovery code is recovered only from the host
  (`user mfa-reset` in the in-process CLI). A lost `ENCRYPTION_KEY` makes every stored
  secret unreadable, which fails closed (codes refused) until an admin reset.
- **Key verification is a digest index lookup, not a constant-time compare.** The presented
  key never meets a stored secret byte-by-byte (so there is no per-byte timing oracle), but
  the index lookup itself is not constant-time - stated plainly in `app/services/apikeys.py`.
- **Trust-host-auth trusts the host boundary.** Its source secret is configured through the
  `LOC_TMS_TRUST_HEADER_SECRET` environment variable (or the plural
  `LOC_TMS_TRUST_HEADER_SECRETS` rotation accept-list, which takes precedence) - *not* a
  settings key; when unset, the door trusts any peer that reaches the port and only a boot
  warning marks it, so the network perimeter is load-bearing there.
