# SSO & Provisioning - Architecture (L1)

## 1. Role

This subsystem owns **inbound directory identity**: the four ways an enterprise IdP can
drive who signs in and which accounts exist. It is an OIDC Relying Party (authorization
code + PKCE against any spec-compliant IdP), a SAML 2.0 Service Provider, an LDAP / Active
Directory search-then-bind authenticator, and a SCIM 2.0 provisioning server for the
`/Users` and `/Groups` lifecycle. Each is off by default and opt-in behind its own
feature flag.

The login flows (OIDC, SAML, LDAP) *authenticate*; SCIM *provisions*. All of them,
however, converge on the same local account seam: account creation for every path funnels
through the shared `create_user` seam in [identity-and-access](identity-and-access.md), so
email validation, the reserved-actor check, the customer-binding invariant, and org
stamping are inherited rather than re-implemented. Just-in-time (JIT) account creation and
the F5 anti-takeover refusal (a federated login may never adopt an existing local-password
account) are the two behaviours this subsystem shares with that seam, cross-referenced
below.

Enablement is a **host-only** decision. The SAML and SCIM enable flags are on the
immutable-over-the-wire set, so no `/api` config route can flip the perimeter over the API
it guards; turning enterprise SSO on is done at the host, wired through the Phase-2
enterprise-enablement door (documented separately).

What it does **not** own. Password authentication, sessions, the RBAC ladder, and inbound
API keys live in [identity-and-access](identity-and-access.md). Organisation tenancy and
the multi-org gate that constrains SCIM confinement live in
[tenancy-confinement](tenancy-confinement.md); SCIM consumes org resolution but does not
define it.

## External URLs

`setting:app_base_url` is the authority for emitted external URLs; the request Host
is the fallback when unset. `app/services/settings.py::external_url` supplies OIDC
`/auth/oidc/callback`, connector OAuth `/connectors/oauth/callback`, and SCIM `/scim/v2`
locations, as well as invitations and the Security/Help copyable callback guidance.
SAML has three-deep precedence: explicit `setting:saml_sp_entity_id` or
`setting:saml_sp_acs_url`, then app_base_url, then the request base URL.
`app/services/saml.py::_prepare` derives scheme, host and port from the configured
base for correct Destination validation behind a TLS-terminating proxy; without
it the historical Host/ASGI calculations are unchanged. The host includes only a
non-default configured port; server_port always records the effective port.
The missing-proxy-trust warning remains independent of app_base_url: proxy trust
is still required for forwarded client identity and audit attribution.
When app_base_url is configured, its PATH is the external prefix: script_name is
that prefix plus `app.web.route_path.route_path(scope)` (passed into the SAML
service by the web caller), replacing ASGI root_path. When unset,
script_name retains security.request_path and its historical root_path behavior.
A non-empty root_path with a pathless app_base_url triggers a Security-page warning:
include the public prefix in the base if the app is externally served under it.
Legacy invalid bases warn once per episode and fall back as if unset; repair on
`/security` or with `config set app_base_url https://tms.example.com`.
OneLogin's host parsing does not support IPv6 literals (bracketed or unbracketed);
use a DNS hostname for SAML. The unset IPv6 http_host form remains unchanged.
RelayState guards and signature requirements are unchanged.

## 2. Invariants

- **A SAML assertion must be signed; an unsigned one is rejected.** OneLogin strict mode
  plus `wantAssertionsSigned`, enforced in `app/services/saml.py::process_acs` and pinned
  by `verify_saml`. A NameID-only assertion (no `AttributeStatement`) is accepted because
  SAML 2.0 makes attributes optional - but the assertion must still be signed and carry a
  NameID (`app/services/saml.py::build_settings`).
- **IdP-initiated SSO is refused unless explicitly allowed.** A response with no
  `InResponseTo` is rejected unless `setting:saml_allow_idp_initiated` is on (default off).
- **OIDC verifies everything before trusting a claim.** PKCE S256, constant-time `state`,
  and the `id_token` signature plus `iss` / `aud` / `exp` / `nonce` are all checked in
  `app/services/oidc.py::complete_login`; unverified claims are never trusted.
- **An LDAP failure never blocks login.** `app/services/ldap_auth.py::try_login` only
  signals success or failure; the fall-through to local password auth on any LDAP
  non-success (no match, wrong password, outage, or an anti-takeover refusal) is implemented
  in the login POST - cross-ref `app/web/auth.py::auth_login_post` (claimed by
  [identity-and-access](identity-and-access.md)). Pinned by `verify_ldap`.
- **The LDAP transport is always authenticated, not merely encrypted (#169).**
  `app/services/ldap_auth.py::_build_server` passes an explicit
  `Tls(validate=CERT_REQUIRED, sni=<host>)`, because ldap3 otherwise manufactures a default
  `Tls()` with `CERT_NONE` and `check_hostname=False`, which would send the decrypted
  service bind password and every user password over an unauthenticated channel. Referrals
  are never chased (`allowed_referral_hosts=[]`, `auto_referrals=False`): ldap3 would follow
  a directory-supplied referral on a fresh connection carrying the same credentials, and for
  an `ldap://` referral it builds that connection unencrypted with validation off.
  Credentials are not handed to the `Connection` until the transport is up; `ldaps://`
  connects directly, an explicit `ldap://` must complete StartTLS or no bind is attempted,
  and a bare host normalises to `ldaps://`. Host forms that cannot match a certificate,
  including **IP literals** (only `DNS` SANs are consulted), are refused at the settings
  write path. `ldap_ca_cert` names a PEM bundle; blank uses the system trust store.
  Pinned by `verify_ldap`'s transport cases; the operator-facing upgrade consequences are
  in [SECRETS.md](../SECRETS.md).
- **SAML rejects replayed and clock-skewed assertions.** `app/services/saml.py::process_acs`
  pins a ±5-minute clock-skew window via `app/services/saml.py::_CLOCK_SKEW_SECONDS` (300s,
  set explicitly rather than trusting the library default) and burns a consumed assertion id
  in the `app/services/saml.py::_REPLAY` cache, so a re-POSTed assertion is refused - pinned
  by `verify_saml`.
- **SCIM is org-confined with no cross-org oracle.** A SCIM key resolves to its owner's
  HOME org (`app/services/scim.py::resolve_identity`); every list/get/create/update is
  confined to that org and a cross-org id is a 404, never a 403.
- **SCIM lifecycle is soft, and never bypasses the last-admin guard.** A SCIM delete or
  `active=false` is a soft deactivation, and the last-active-admin invariant surfaces as a
  409 - `app/services/scim.py::patch_user`.
- **No federated login takes over a local-password account (F5).** Cross-ref
  `app/services/security.py::get_or_create_saml_user`,
  `app/services/security.py::get_or_create_sso_user`, and the LDAP twin
  `app/services/ldap_auth.py::get_or_create_ldap_user`.
- **An OIDC account is bound to its subject, not to an email (identity-01).**
  `app/services/security.py::get_or_create_sso_user` matches an `id_token` by
  `User.sso_issuer` + `User.sso_subject` first. Only when no account is bound does it link
  by the `email` claim (never `preferred_username`), and only when the IdP vouches for the
  address (`email_verified` true, or the admin switch `setting:sso_trust_unverified_email`,
  default off). Every first link records the binding (audited `login.sso_linked`). An
  account bound to another subject at the same issuer, and any LDAP or SAML account, is
  refused and audited `login.sso_refused`; a SCIM account is bound but keeps
  `auth_source=scim`. An account bound at a previous issuer re-binds on a verified email
  after the admin switches identity providers. `complete_login` requires `sub`. Upgrade:
  the columns arrive NULL (alembic `c5e2a8d4f1b7`) and each existing SSO account binds on
  its next verified sign-in. GDPR erase clears the binding. Pinned by `verify_oidc`.
  A bound account that LDAP or SAML later took over is refused on the bound path too.
  An IdP that never sends `email_verified` (Entra ID by default): every first link, including
  an existing pre-upgrade SSO account, is refused with "did not confirm this email address"
  and audited `login.sso_refused` ("email not verified by the identity provider") until an
  admin turns the switch on (Security page, `admin oidc config --trust-unverified-email on --yes`,
  `POST /api/admin/oidc`). Local password accounts, including the built-in admin, are not
  affected, so an admin can always sign in to do that; with no password admin, the local CLI
  on the host works without signing in. Turn the switch on only when the configured issuer
  is one tenant whose addresses your administrators assign: with it on, anyone who can set
  their own email at that issuer can claim an unbound account with that email. Known gap:
  there is no admin "unlink" action, so a subject that changes at the same issuer (Entra
  `sub` is per application, so a NEW app registration changes every user's `sub`) is
  refused as "linked to a different single sign-on identity" until the binding is cleared.
- **OIDC egress rides the SSRF guard (#354).** Discovery, JWKS and the token call resolve the
  issuer host once, validate every answer, dial only those addresses and re-check the peer; a
  deliberately private IdP needs its exact hostname on the host-only `egress_private_allowlist`.
  The plain-http loopback dev IdP (`localhost`, `127.0.0.1`, the IPv6 loopback) is the one documented
  exception. See the [outbound client inventory](secrets-and-crypto.md#outbound-client-inventory).

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| OIDC Relying Party | `app/services/oidc.py::begin_login` · `app/services/oidc.py::complete_login` · `app/services/oidc.py::discover` | PKCE authorization-code round trip with full `id_token` verification. |
| SAML 2.0 Service Provider | `app/services/saml.py::begin_login` · `app/services/saml.py::process_acs` · `app/services/saml.py::build_settings` · `app/services/saml.py::metadata` · `app/services/saml.py::TXN_COOKIE_NAME` | SP-initiated login, signed-assertion ACS, SP settings/metadata, and the transaction cookie. |
| LDAP / AD authentication | `app/services/ldap_auth.py::try_login` · `app/services/ldap_auth.py::get_or_create_ldap_user` · `app/services/ldap_auth.py::DEFAULT_USER_FILTER` | Search-then-bind against the directory, provisioning the local account on success. |
| SCIM 2.0 (service) | `app/services/scim.py::resolve_identity` · `app/services/scim.py::create_user` · `app/services/scim.py::patch_user` · `app/services/scim.py::create_group` · `app/services/scim.py::patch_group` | The org-confined `/Users` + `/Groups` provisioning logic behind the router. |
| SCIM 2.0 (router) | `app/web/scim.py::router` · `app/web/scim.py::create_user` · `app/web/scim.py::patch_group` · `app/web/scim.py::delete_group` | The Bearer-authenticated `/scim/v2` mount that renders the service seam and RFC-7644 errors. |
| SCIM CLI (read-only) | `app/cli/cmd_scim.py::register` | The `scim status` / `users` / `groups` operator-visibility commands. |

The browser-facing SSO routes live in `app/web/auth.py`, which
[identity-and-access](identity-and-access.md) claims, so they are cross-referenced here
rather than re-claimed: the signed SAML assertion arrives at `app/web/auth.py::saml_acs`,
the OIDC authorization code at `app/web/auth.py::oidc_callback`, and SP metadata is served by
`app/web/auth.py::saml_metadata` (with `/auth/saml/login`, `/auth/oidc/login`, and
`/auth/sso` initiating each flow).

## 4. Data & settings

- **Models:** federated logins provision `app/models.py::User` (with `auth_source` set to
  `sso` / `saml` / `scim` / `ldap`). SCIM groups are directory metadata in
  `app/models.py::UserGroup` and `app/models.py::UserGroupMember` - with zero authorization
  effect in v1 (a group member gains no role, visibility, or project scope).
- **Settings:** OIDC - `setting:sso_enabled`, `setting:sso_provider`, `setting:sso_issuer`.
  SAML - `setting:saml_enabled`, `setting:saml_sp_entity_id`, `setting:saml_allow_idp_initiated`.
  SCIM - `setting:scim_enabled`. LDAP - `setting:ldap_enabled`. The `saml_enabled` and
  `scim_enabled` flags are **host-only**: they sit on
  `app/services/settings.py::REMOTE_IMMUTABLE_KEYS`, so a remote `/api` config write can
  never turn the perimeter on. IdP client secrets and bind passwords are Fernet-at-rest via
  `app/services/settings.py::get_secret`.
- **LDAP is an opt-in (LGPL) extra.** The `ldap3` library is kept out of the default
  copyleft-free install; enable LDAP with `pip install -r requirements-ldap.txt` on a host
  install, or build the container image with `--build-arg WITH_LDAP=1`. Turning
  `setting:ldap_enabled` on while the package is absent is REFUSED at the write seam
  (`app/services/settings.py::set_many` calls `app/services/ldap_auth.py::require_installed`),
  so a directory login can never be armed against a missing driver. Pinned by `verify_ldap`.

## 5. Enforced by

- `verify_oidc` - the PKCE round trip, JWKS verification, and the one-shot session keys.
- `verify_saml` - signature enforcement, audience/`InResponseTo` correlation, replay
  rejection, the IdP-initiated refusal, and RelayState sanitisation.
- `verify_scim` - the org-confined `/Users` + `/Groups` lifecycle and the shared-seam
  invariants.
- `verify_ldap` - search-then-bind, the fall-through-to-local rule, and the anti-takeover
  refusal.

## 6. Battle scars

- **SCIM + SAML were built as the enterprise sprint's overflow train.** The interoperable
  server for Okta/Entra and the SP-initiated SAML login were delivered together as one overflow train.
- **Live-IdP interop found real bugs the offline suites could not.** Testing against a real
  Okta org surfaced a SAML cross-site session-cookie `SameSite` bug, NameID-only assertion
  handling, group-create atomicity, and a `delete_group` FK-500 plus member churn/dedup -
  each fixed and gated.

## 7. Limits & accepted risks

- **JIT provisioning is a single-tenant convenience.** Under the multi-org gate a brand-new
  SAML assertion is refused outright by `app/services/security.py::get_or_create_saml_user`
  (a SAML assertion carries no org context), so accounts must be provisioned first - e.g.
  via SCIM, whose key names the org.
- **SCIM groups carry no authorization in v1.** Group membership is directory metadata
  only; mapping groups to roles or scope is deferred policy work, not yet scheduled.
- **SAML needs an opt-in native dependency.** `python3-saml` (xmlsec) is not in the base
  requirements; with the flag on and the library missing, the boot fails loudly rather than
  serving a login button that 500s - a deliberate capability check, not a silent degrade.
  The container image carries it only when built with `--build-arg WITH_SAML=1`: the
  `Dockerfile` then installs `requirements-saml.lock` (binary wheels only for lxml and
  xmlsec), adds the `libxmlsec1-openssl` library, and fails the build if the SAML import
  smoke test does not pass. The boot error from `app/services/saml.py::configure_at_boot`
  names that build argument. The default image is built without it.

## Canonical inbound SAML path

`app/services/saml.py::_prepare` uses the ASGI scheme and
`app/services/security_client_ip_one_authority_.py::request_path` for the SAML script path. Host supplies
only the authority after the outer application Host validation. Pin public
hostnames with `setting:security_allowed_hosts` when required. HTTPS forwarding
and Fly runtime defaults are documented in [deployment](deployment.md).
