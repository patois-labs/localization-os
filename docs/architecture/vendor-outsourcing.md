# Vendor outsourcing (external human translation/review): Architecture (L1)

## 1. Role

This subsystem is the **external-vendor dispatch-and-wait step**: sending packaged content out to a
human translation/review vendor and admitting the result back through the same guarded write seam a
model reply uses. It is where AI spend meets human spend.

A vendor (`app/services/vendors.py`) is a first-class entity: a name, a connection (endpoint URL
plus a Fernet-encrypted signing secret, stored exactly like a connector credential and never
echoed), an MSA rate card (`app/services/vendor_cost.py`, priced per TM-match band), and its
customer links, the fail-closed scoping seam a workflow's external step selects through. The
dispatch engine (`app/services/vendor_dispatch.py`) resolves the step's vendor fail-closed against
the document's customer, builds a versioned JSON contract with a per-segment state vocabulary
aligned to XLIFF 1.2, mints a claim token, and dispatches through the existing SSRF guard with an
HMAC signature. The return path (`app/services/vendor_return.py`) is the point of the feature:
anyone can POST a payload, and the value is that returning content re-enters through the same
guarded admission the provider reply does, HMAC-authenticated and matched to the exact run leg by
claim token.

A vendor is dispatched over one of two registered protocols: `native` (the signed JSON wire) or
`manual` (file exchange with no wire). A return arrives by one of three transports that all feed the
same validation: the signed webhook callback, polling the vendor's declared `poll_url` (through the
same SSRF guard), or a manual re-upload on the vendor panel, CLI or API. A leg whose vendor never
answers is never hard-failed: on expiry it escalates to a human-review pause so a person decides,
and transient send failures retry on a backoff schedule before dead-lettering.

Around that spine sit the optional lifecycle stages: a quote-approval gate
(`app/services/vendor_quote.py`), a reject-and-rework transition
(`app/services/vendor_rework.py`), the AI-vs-human spend split the surfaces render
(`app/services/vendor_spend.py`), the XLIFF 1.2 rendition of the contract
(`app/services/vendor_xliff.py`), and the SSO handoff token for the separate Vendor Studio app
(`app/services/vendor_studio_sso.py`).

It does not own the model translation path (translation-pipeline), the run engine that parks a leg
(workflow-engine), the reviewer worklist and query threads (collaboration-and-review), or Vendor
Studio itself (a separate app, wire-isolated). The reviewer-side worklist that surfaces vendor
legs is documented in collaboration-and-review.

## 2. Invariants

- **A vendor is selectable for a step only when linked to the document's customer (fail-closed).**
  `app/services/vendors.py::vendor_for_customer` is the one scoping seam, and dispatch resolves
  through it (`app/services/vendor_dispatch_hub.py::resolve_vendor`). Enforced by `verify_vendors` and
  `verify_external_step`.
- **A vendor's signing secret is write-only and never echoed.**
  `app/services/vendors.py::set_secret` stores Fernet ciphertext and `to_dict` carries only a
  presence flag, mirroring connector credentials. Enforced by `verify_vendors` and
  `verify_vendors_web`.
- **A return re-enters through the one guarded admission seam, HMAC-authenticated.**
  `app/services/vendor_return_hub.py::authenticate_callback` and
  `app/services/vendor_return_common.py::verify_signature` gate every transport, and
  `app/services/vendor_return_common.py::leg_for_claim` matches the return to its exact leg. Enforced by
  `verify_vendor_return`.
- **Outbound dispatch rides the connector SSRF guard and is signed.**
  `app/services/vendor_dispatch_hub.py::assert_dispatch_egress_allowed` reuses the connector target
  validation and the body is HMAC-signed. Enforced by `verify_vendor_return` (the shared-secret
  both-directions path).
- **A vendor at a quoted or reworked leg reuses the one parked-leg machine.**
  `app/services/vendor_quote.py::approve_quote` and
  `app/services/vendor_rework.py::reject_and_rework` transition a parked leg without a second
  parking mechanism; with the quote flag off a leg is never QUOTED and the lifecycle is
  byte-identical. Enforced by `verify_vendor_wave_e`.
- **The whole group ships on as a kill switch and is non-enumerable when an operator turns it off.**
  `setting:vendors_enabled` (default on, a kill switch) 404s the `/vendors` surface and refuses the
  service only when off. Enforced by `verify_features` (the flag-off refusal arms for the vendors
  family).

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Vendor entity | `app/services/vendors.py::create` · `app/services/vendors.py::vendor_for_customer` · `app/services/vendors.py::set_secret` | The vendor CRUD, the fail-closed customer scoping seam, and the write-only secret. |
| MSA cost model | `app/services/vendor_cost.py::classify_band` · `app/services/vendor_cost.py::estimate_from_segments` · `app/services/vendor_cost.py::project_leverage` | The per-band rate card and the scoping estimate of human post-edit cost. |
| Dispatch engine | `app/services/vendor_dispatch.py` · `app/services/vendor_dispatch_hub.py::build_contract` · `app/services/vendor_dispatch_hub.py::resolve_vendor` · `app/services/vendor_dispatch_common.py::new_claim_token` · `app/services/vendor_dispatch_part2.py::expiry_for` · `app/services/vendor_dispatch_hub2.py::_clear_or_keep_frozen` | The versioned JSON contract, fail-closed vendor resolution, and the claim token. |
| Return admission | `app/services/vendor_return.py` · `app/services/vendor_return_hub.py::authenticate_callback` · `app/services/vendor_return_hub.py::check_contract` · `app/services/vendor_return_common.py::leg_for_claim` · `app/services/vendor_return_part2.py::redrive_returned_leg` | HMAC auth, contract validation, and leg matching for the guarded return. |
| Quote gate | `app/services/vendor_quote.py::record_quote` · `app/services/vendor_quote.py::approve_quote` | The optional dispatched to quoted to work lifecycle stage. |
| Reject / rework | `app/services/vendor_rework.py::reject_and_rework` · `app/services/vendor_rework.py::build_rework_feedback` | The returned to rejected to re-dispatch transition with structured feedback. |
| AI vs human spend | `app/services/vendor_spend.py::spend_summary` · `app/services/vendor_spend.py::spend_by_run` | The disjoint AI-model and human-vendor spend split the surfaces render. |
| XLIFF rendition | `app/services/vendor_xliff.py::render_contract_xliff` · `app/services/vendor_xliff.py::parse_return_xliff` | The XLIFF 1.2 rendition of the same canonical contract. |
| Studio SSO | `app/services/vendor_studio_sso.py::mint_token` | The short-lived, single-use signed handoff token for the separate Vendor Studio app; carries the operator's role, org scope, audience and a jti (#557). |
| Web surface | `app/web/vendors.py::vendors_index` · `app/web/vendors.py::vendor_dispatch_return` | The operator-admin `/vendors` pages and the dispatch actions. |
| CLI surface | `app/cli/cmd_vendor.py::register` · `app/cli/vendor_common.py::_flag_gate` · `app/cli/vendor_jobs.py::register_jobs` | The `vendor` command group (local-only, credential-bearing). |

## 4. Data & settings

- **Models:** `app/models.py::Vendor` (the vendor + its encrypted secret + rate card),
  `app/models.py::VendorCustomer` (the customer link), and `app/models.py::VendorDispatch` (a
  dispatch leg with its estimate/actual cost, claim token and state).
- **Settings:** `setting:vendors_enabled` (default on, a kill switch) gates the whole subsystem.
  `setting:vendor_dispatch_ttl_hours` (default 168, 0 means wait indefinitely; a step's
  `external_wait_hours` overrides it) bounds how long a parked leg waits before it escalates. The
  rework cap `setting:vendor_rework_max_rounds` (default 1, a whole number from 1 to 10, read by
  `app/services/vendor_rework.py::rework_cap`) is set like any other key (`config set`, POST
  `/api/admin/config`, MCP `manage_install set_config`); every write door refuses a value outside 1..10. `vendor_studio_url` and `vendor_studio_sso_secret` configure the Studio handoff. Per-vendor flags
  (`requires_quote_approval`, `include_xliff`, `omit_locked_targets`) live on the vendor row, not
  in install settings, since they are per-vendor policy.

## 5. Enforced by

- `verify_vendors`, `verify_vendors_web`, `verify_vendor_api`, `verify_vendor_cli`: the entity, the
  scoping seam, the write-only secret and the four surfaces.
- `verify_external_step`: the fail-closed vendor selection on a workflow's external step.
- `verify_vendor_return` and `verify_vendor_return_web`: the HMAC-authenticated guarded return.
- `verify_vendor_wave_e`: the quote gate and the reject/rework lifecycle.
- `verify_vendor_xliff`: the XLIFF 1.2 rendition round-trip.
- `verify_vendor_studio`: the SSO handoff token.

## 6. Battle scars

- **Returning content must re-enter through the same guarded seam as a model reply.** The feature's
  whole value is that a vendor return is validated exactly like a provider reply rather than trusted
  because it arrived over a webhook, stated in the `app/services/vendor_return.py` module docstring
  ("the value is that returning content re-enters through the exact same guarded write seam as a
  provider reply").
- **Human spend is a separate ledger from AI spend.** Vendor work is priced by the MSA rate card
  per TM-match band and kept disjoint from model spend so the surfaces can show both honestly,
  recorded in the `app/services/vendor_spend.py` module docstring.

## 7. Limits & accepted risks

- **The CLI vendor group is local-only.** It manages credentials and rate cards on the host, so
  every verb refuses `--remote` rather than run a credential path over the wire, stated in the
  `app/cli/cmd_vendor.py` module docstring.
- **Vendor Studio is a separate app, shared only by secret and algorithm.** The SSO token spec is
  transcribed, never imported, so the two sides stay wire-isolated, stated in the
  `app/services/vendor_studio_sso.py` module docstring.
