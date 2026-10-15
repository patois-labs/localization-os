# Tenancy Confinement - Architecture (L1)

## 1. Role

This subsystem owns the **isolation rings** that keep one tenant's data away from another's:
the customer ring (a customer-360 view scoped entirely to one customer) and the opt-in
multi-org gate. The organising principle is **fail-closed org resolution** - an ambiguous or
un-stamped scope reads *nothing* rather than *everything*.

**There is no cross-tenant door on the web.** Organizations run side by side; changing one
means signing in to that one. Org lifecycle (create, rename, suspend, delete, move a user)
is a host operator action over the CLI, authorized by an operator KEY rather than by any
account flag.

Multi-tenancy is off by default. With the sacred gate off (on-prem single-tenant) every
org predicate is a strict no-op, so a normal install behaves exactly as it always has. With
the gate on, every row belongs to exactly one org and a user only ever sees their own org's
rows - with no exceptions, which is a stronger statement than the one this subsystem used to
make.

What it does **not** own. The RBAC project-scoping predicates and the org helper functions
themselves live in [identity-and-access](identity-and-access.md)'s `app/services/access.py`
(this subsystem consumes them, cross-referenced below); the tamper-evidence of the audit
rows an operator action writes is the [audit-chain](audit-chain.md) subsystem's guarantee.

## 2. Invariants

- **Multi-tenant isolation is opt-in and fail-closed.** The gate is
  `app/services/access.py::multi_org_active` behind `setting:multi_org_enabled` (default
  off); with it on, `app/services/access.py::current_org_id` maps a NULL-org principal to
  the poison `app/services/access.py::BLOCKED_ORG_ID` and
  `app/services/access.py::org_scope_blocked` makes a bare-`None` read return nothing -
  enforced by `verify_multiorg`.
- **No web surface reads across orgs.** There is no mount, route, or account that can; the
  property is structural rather than enforced, which is why there is nothing here to gate.
- **Cross-org authority requires the host, not an account.** The `org` and `user org` CLI
  verbs are the only cross-org writes, gated by `require_operator` against
  `setting:operator_key_hash` and `LOC_TMS_OPERATOR_KEY`. There is no credential a web
  session can hold that reaches another org.
- **`User.is_superadmin` is dormant and must stay that way.** `verify_multiorg`'s
  `test_superadmin_stays_dormant` fails if anything under `app/` reads or writes it, or if
  the deleted back-office package reappears. Nothing still enforces the linchpin that made a
  super-admin safe, so a resurrected flag would be an unguarded cross-tenant marker.
- **A foreign id answers exactly like a missing one.** Under the gate a request that names another
  org's (or another customer's) project, document, engine, customer or query anchor gets the same
  404 or 400 as an id that does not exist, so an id is never an existence oracle (decision
  [0010](../decisions/0010-isolation-404-before-403.md)). Since 2026-10-06 that covers the
  vendor legs, workflow groups, default workflow, language catalog and templates (#488), the
  users and segmentation-profile create forms and the audit export (#586), and a query anchor
  next to a missing one. Enforced by `verify_multiorg_scoping_488` and `verify_missing_hidden_body_ids`.
- **An invitation never reaches an account in another org.** Under the gate
  `app/services/security_client_ip_one_authority_.py::invite_refusal` refuses to issue or accept
  an invite for an existing address that belongs to a different org (or to none), with the same
  non-enumerating sentence an active account gets, so an org admin cannot activate or set the
  password of a passwordless row another org pre-created (#536). Enforced by `verify_multiorg_batch1005`.
- **Every in-process CLI verb is classified, and the gate matches the class.** Under the gate an
  org-scoped verb needs `--org`, an operator-only verb (anything that writes a setting every org
  shares) needs `--operator`, and a verb with neither is install-wide host work; a new verb that is
  not in the table fails the suite (#515, #556). Claimed by
  [surfaces and parity](surfaces-and-parity.md); enforced by `verify_cli_scope_classification`.
- **A user moved to another org leaves the old org's history and projects behind.** A moved
  customer-bound user lists no project of the old org (#467), and org-stamped audit rows keep the
  org they were written in, so every org view filters on that column (#487, see [audit chain](audit-chain.md)).
- **Customer operations are org-guarded when the caller passes its org scope.**
  `app/services/customers.py::delete_customer` and `app/services/customers.py::selectable`
  return `None` for a foreign-org id when given an `org_id`; with `org_id=None` (the
  in-process operator, or the gate off) they are unconfined by design.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Customer 360 + lifecycle | `app/services/customers.py::get_customer_360` · `app/services/customers.py::customer_connections` · `app/services/customers.py::selectable` · `app/services/customers.py::delete_customer` | The per-customer aggregate view, its connection scoping, and the org-guarded pick/delete seams. |
| Org management CLI | `app/cli/cmd_org.py::register` | The `org` command group (create/list/rename/suspend/delete) plus `user org`, in-process and operator-gated under the sacred gate. The move seam itself is cross-referenced, not claimed here: identity-and-access owns that module. |

There is a SECOND operator-gated cross-org action, cross-referenced rather than claimed
here because [project-setup-and-templates](project-setup-and-templates.md) owns that
module: `template delete --operator` may remove a GLOBAL (unowned) project template,
which no org-scoped caller can. It may NOT touch another org's owned row. Gate round 2
caught this table asserting there was only one door while the same train added a second.

## 4. Data & settings

- **Models:** `app/models.py::Organization` - the tenant boundary every org-stamped table
  keys on. `app/models.py::Customer` - the inner customer ring within an org.
  The `is_superadmin` column on the user table is DORMANT (see section 6): it survives so no
  destructive migration is needed, and nothing reads or writes it.
- **Settings:** `setting:multi_org_enabled` (default `"0"`) - the sacred gate, **host-only**
  (on `app/services/settings.py::REMOTE_IMMUTABLE_KEYS`, so no `/api` write can flip it).
  `setting:remote_admin_enabled` (default `"0"`) - the remote-administration lock.
  `setting:operator_key_hash` - the SHA-256 of the in-process operator key that `--operator`
  must present. This is now the ONLY thing that authorizes a cross-org action, and it is
  deliberately not an account: operator power requires shell access on the host.
- **Install-wide resources are host-only under the gate (#424-#427).** Some settings and files
  carry no org dimension, so a write by one org would change it for every org. While the gate is
  on they are refused on every web and `/api` door and only the host operator keeps them
  (`config set`, `admin srx`, `admin lqa rulebook` on the host). They are: the custom SRX ruleset
  and `setting:srx_enabled`/`setting:srx_file` (`app/services/settings.py::ORG_ADMIN_REFUSED_EXTRA`);
  the per-language-pair LQA rulebooks (`app/services/lqa.py::rulebook_host_only`); the Databricks
  export target and the shared SMTP and connector OAuth credentials; the Advanced Workflows
  install ceilings (`advanced_install_max_*`); and the semantic TM switch and the
  enabled-language catalog (`semantic_tm_enabled`, `enabled_locales`, #479); and the prompt Ledger's
  capture switch and clear (`prompt_tap_enabled`, #538), one shared file. A strict no-op with the gate off (every install
  today).

## 5. Enforced by

- `verify_multiorg` - the sacred gate, fail-closed org resolution, and the read-family
  poison-id behaviour.
- `verify_multiorg_install_wide_files`: that install-wide files and settings (the SRX ruleset,
  the LQA rulebooks, the Databricks/SMTP/OAuth egress targets and shared credentials, and the
  Advanced Workflows install ceilings) are host-only while the gate is on, and unchanged with it
  off.
- `verify_admin_org_scope` - org scoping across the admin surfaces.
- `verify_audit_customer` - customer-scoped audit views (shared with the audit chain).
- `verify_customers` - the customer-360 assembly and the org-guarded lifecycle seams.
- `verify_cli_org_scope_leaves` - that in-process CLI leaves apply the `--org` scope.
- `verify_workflow_create_tenancy` - tenancy stamping on workflow creation.
- `verify_multiorg_scoping_488` and `verify_multiorg_batch1005`: the 2026-10-05/06 scoping gaps
  (vendor legs, workflow groups, templates, language catalog, cross-org invites, moved users, the
  Ledger switch).
- `verify_missing_hidden_body_ids`: a foreign or hidden id in a request body answers like a missing one.
- `verify_cli_scope_classification`: every CLI verb is in exactly one scope class.

## 6. Battle scars

- **The deployment ladder and tenancy posture are settled in an ADR.** The opt-in gate, the app-layer isolation stance, and the single-machine constraint are decided (ADR 0009 covers the opt-in gate).
- **A two-org discovery-and-log security sweep probed the isolation seams.** Lane F built two
  full orgs and probed cross-tenant reads over `/api` and the service seams - a discovery +
  log pass, not a fix pass.
- **A full `/api` regression found no confinement leak.** The route sweep confirmed no
  cross-org, scope, or role leak.
- **The cross-org console was removed on 2026-09-20, by decision, not by refactor.** It had
  been live-proven gate-on (separate-session walls, allow-listed impersonation, the
  audit-toggle-bypass guard), and it was deleted anyway
  because no hosted service exists to need it and a cross-tenant door is the most expensive
  kind of code to keep correct. Two of the 2026-09-18 audit's findings were specifically a
  super-admin leaking across orgs in the customer app; that class is now impossible rather
  than guarded. **The trade is real and should be re-read before any hosted offering:** the
  break-glass is now shell access, which is wider and less auditable than the narrow,
  time-boxed, audited-before-dispatch impersonation it replaced.

## 7. Limits & accepted risks

- **Isolation is app-layer, and never exercised live on Postgres outside CI.** The gate-on
  isolation has only run against SQLite and the `postgres-smoke` CI job; a real
  third-party-tenant deployment must re-run the isolation checklist against live Postgres
  first.
- **Single-machine is a deliberate constraint.** The in-memory job queue and the 30-second
  scheduler thread mean more than one machine is forbidden until a shared-state scale-out is
  built - the gate governs isolation, not horizontal scale.
- **There is no cross-org remediation path.** A stuck run or a broken document inside a
  tenant cannot be fixed without that tenant's credentials or direct host access. This is
  the accepted cost of removing impersonation; a hosted offering would need a designed,
  audited support-access flow rather than a revival of the console.
- **Client-IP resolution still matters for audit attribution.** An empty identity from a
  malformed proxy chain is denied wherever it is consumed.
  Fly uses Fly-Client-IP only from configured trusted socket peers. Off Fly, client_ip
  trusts X-Forwarded-For only from configured trusted_proxy_ips; otherwise it uses
  the socket peer. All uvicorn launch paths use --no-proxy-headers so an outer
  rewrite cannot erase that peer. See the ingress boundary in
  [identity-and-access](identity-and-access.md#ingress-boundary).
