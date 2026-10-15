"""The wire contract Vendor Studio speaks — field names pinned in ONE place.

Vendor Studio is WIRE-ONLY isolated: it never imports `app/`. So the exact JSON
shape of the dispatch (inbound) and return (outbound) payloads, plus the header
and signature conventions, are transcribed here by hand and pinned. If Localization OS
ever changes the contract, this module is the single edit point.

SOURCE OF TRUTH (derived by reading, NOT importing):
  * app/services/vendor_dispatch.py  -> build_contract() / dispatch()  (the OUTBOUND
    dispatch shape Vendor Studio RECEIVES, and the signing header it carries).
  * app/services/vendor_return.py     -> check_contract() / admit_return() /
    authenticate_callback() / poll_leg()  (the RETURN shape Vendor Studio SENDS,
    and the poll transport it answers).
  * app/web/api.py  POST /webhooks/vendor-returns  (the return webhook Localization OS
    exposes; contract_version 1.0).

--------------------------------------------------------------------------------
INBOUND DISPATCH (Localization OS -> Vendor Studio /intake/dispatch)
--------------------------------------------------------------------------------
Headers (from vendor_dispatch.dispatch):
  content-type: application/json
  x-locstudio-claim: <claim_token>
  x-locstudio-contract-version: 1.0
  x-locstudio-signature: sha256=<hex>     # HMAC-SHA256 over the RAW body, signing_secret
  authorization: Bearer <auth_token>      # optional

Body (from vendor_dispatch.build_contract):
  {
    "contract_version": "1.0",
    "run_id": <int>,
    "step_key": "<str>",
    "step_name": "<str>",
    "claim_token": "<str>",
    "source_locale": "<str>",
    "target_locale": "<str>",
    "targets": ["<target_locale>", ...],
    "segments": [
      {
        "id": <int|str>,                 # "full:0" for a FULL_TEXT whole-body unit
        "source": "<str>",
        "target": "<str>",               # prefilled MT/TM, may be ""
        "state": "needs-review-translation" | "needs-translation",
        "tm_match_pct": <number>,        # OPTIONAL
        "placeholders": ["<tag>", ...],  # the ⟦xN⟧ tag inventory to preserve
        # --- VP-0 ENRICHMENT (not in the current contract; tolerate absence) ---
        "provenance": "tm_exact"|"tm_fuzzy"|"mt"|"new",   # OPTIONAL
        "match_score": <number>,                          # OPTIONAL (tm_fuzzy)
        "locked": <bool>,                                 # OPTIONAL
        "locked_reason": "<str>",                         # OPTIONAL
        "char_limit": <int>,                              # OPTIONAL
        # --- WAVE 3 TRAIN E: buyer query carry (OPTIONAL; present only when non-empty) ---
        "queries": [                                      # buyer threads on this segment
          {"ref": "<buyer thread id>", "subject": "<str>",
           "status": "open"|"answered",
           "posts": [{"body": "<str>", "author_role": "buyer"|"vendor",
                      "at": "<iso>"}]}   # NO buyer emails -- coarse author_role only
        ],
        "queries_truncated": <bool>,   # OPTIONAL; present only when posts/threads were cut
        "queries_omitted": <int>       # OPTIONAL; count of comments not shown
      }, ...
    ],
    "assets": {
      "glossary": [ {term, target|target_term, forbidden?, dnt?}, ... ] | "<str>",  # OPTIONAL
      "style_guide": "<str>",   # OPTIONAL
      "context": "<str>"        # OPTIONAL
    }
  }

--------------------------------------------------------------------------------
OUTBOUND RETURN (Vendor Studio -> Localization OS /webhooks/vendor-returns)
--------------------------------------------------------------------------------
Headers:
  content-type: application/json
  x-locstudio-claim: <claim_token>              # CLAIM_HEADER; selects the leg
  x-locstudio-signature: sha256=<hex>           # HMAC over the RAW body, signing_secret

Body (validated by vendor_return.check_contract):
  {
    "contract_version": "1.0",
    "claim_token": "<str>",        # MANDATORY; must equal the leg's token
    "target_locale": "<str>",      # checked against the leg when present
    "segments": [
      {"id": <int|str>, "target": "<str>",    # each dispatched id exactly ONCE, cumulative
       # --- WAVE 3 TRAIN E: vendor->buyer comment carry (OPTIONAL; shared comments only) ---
       "comments": [                          # <=10 per segment (else the return is refused)
         {"body": "<str, 1..4000>",
          "author_label": "<str, <=120>",     # vendor display name/role, never a User
          "ref": "<str, <=64>",               # the vendor's own comment id -> external_id
          "reply_to": <int|null>}             # a buyer thread id, or null = open a new thread
       ]},
      ...
    ],
    "actual_cost_usd": <number>,   # OPTIONAL; reconciliation
    "actual_currency": "<ISO-4217>"  # OPTIONAL (3 ASCII letters)
  }

The POLL transport (vendor_return.poll_leg GETs the vendor's poll_url):
  Request headers carry x-locstudio-claim (CLAIM_HEADER), x-locstudio-contract-version,
  accept: application/json, optional bearer.
  204 / 404 / empty body = "nothing ready". A 2xx JSON body = the RETURN shape above.
  A response MAY be signed (x-locstudio-signature); if signed it MUST verify.
"""

from __future__ import annotations

CONTRACT_VERSION = "1.0"

# Header names (lowercase; HTTP headers are case-insensitive). These match the
# constants in app/services/vendor_return.py and the headers vendor_dispatch sets.
CLAIM_HEADER = "x-locstudio-claim"
SIGNATURE_HEADER = "x-locstudio-signature"
CONTRACT_VERSION_HEADER = "x-locstudio-contract-version"

# The signature prefix vendor_return.verify_signature accepts (it also tolerates a
# bare hex digest, but we always emit the prefixed form dispatch() uses).
SIGNATURE_PREFIX = "sha256="

# The FULL_TEXT whole-body unit id Localization OS uses (vendor_return._segment_index).
FULL_TEXT_ID = "full:0"


def norm_id(value) -> str:
    """One canonical string form for a contract segment id, matching
    vendor_return._norm_id: JSON freely swaps 12 for "12", so both sides normalize."""
    return str(value).strip()
