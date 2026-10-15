# Vendor Studio - the other side of the Localization OS wire

A standalone, production-bound **mini-TMS-for-vendors** (a fake LSP: think Lionbridge /
TransPerfect in miniature). It receives Localization OS's real signed dispatches, shows them in
a **PM inbox**, lets a persona claim/assign the job, open a **minimal CAT editor** (grid,
statuses, TM/MT provenance badges, locked rows, glossary highlighting, comments, reviewer
mode), run a **QA pass** (deterministic checks + an LM-Studio-backed advisor), and deliver
a **signed return** back into Localization OS's guarded intake.

## Supply chain

The reviewed direct pins match the root release lock. The image installs the committed
closure in `requirements.lock` with `--no-deps --only-binary=:all:`. The local pregate and
the dependency workflow audit both this lock and the direct pins; Dependabot watches this
directory. Update both files together and run `verify_release_lock`.

After the first deploy behind a proxy, set `VSTUDIO_TRUSTED_PROXY_IPS` to the measured proxy
peers (/32 each; the boot ERROR and the access log show the peer addresses) and redeploy
before the app is shared. Until then every caller shares one API-key login rate-limit bucket.

The image copies application code as root and verifies that the runtime `app` user
cannot write under `/app`. Persistent writes belong in `/data`; temporary
files use the system temp directory. The build context is an allow-list containing
only the entry point, application package, templates, static assets and dependency lock.

It is **wire-only isolated**: nothing here imports `app/` or `integrations/`, and nothing
there imports `vstudio`. The two sides speak only over HTTP + HMAC. An import-scan guard
test enforces both directions (`tests/test_import_isolation.py`, and again in the repo's
`tests/verify_vendor_studio.py`).

Same stack as Localization OS (FastAPI + SQLModel/SQLite + Jinja2 + vanilla JS), its own package
(`vstudio/`), its own DB, its own port (default **8100**), its own container image.

---

## Configuration (env only - no secrets in code)

| Env var | Purpose | Default |
|---|---|---|
| `VSTUDIO_SIGNING_SECRET` | Shared HMAC secret - **must equal** the Localization OS Vendor entity's `signing_secret`. Verifies inbound dispatch signatures and signs outbound returns. | *(unset - the wire will reject everything)* |
| `VSTUDIO_SSO_SECRET` | HMAC secret for the SSO handoff token minted by Localization OS. Blank = SSO disabled. | *(unset)* |
| `VSTUDIO_LOCSTUDIO_URL` | Base URL of the Localization OS instance (return webhook + API-key verification live under it). | *(unset)* |
| `VSTUDIO_DB_PATH` | SQLite file path. | `<pkg>/data/vendor_studio.db` |
| `VSTUDIO_SESSION_SECRET` | Signs Vendor Studio's own session cookie. **Required** for a non-loopback bind (startup hard-fails without it). | *(random per process; loopback only)* |
| `VSTUDIO_SESSION_MAX_AGE` | Session cookie lifetime (seconds). | `43200` (12h) |
| `VSTUDIO_ROLE_RECHECK_SECONDS` | A session older than this must sign in again so the role is re-read from Localization OS. `0` = off. | `3600` |
| `VSTUDIO_ORG_ID` | The one Localization OS org this Studio serves. Required on a multi-org install (any other org is refused). | *(unset)* |
| `VSTUDIO_COOKIE_INSECURE` | Set `1` to drop the `Secure` cookie flag - **loopback-http dev only**. | *(unset → Secure)* |
| `VSTUDIO_ADVISOR_BASE_URL` | LM Studio / OpenAI-compatible base URL (live advisor). | *(unset)* |
| `VSTUDIO_ADVISOR_MODEL` | Advisor model id. | `local-model` |
| `VSTUDIO_ADVISOR_MODE` | `mock` (deterministic, hermetic, $0) or `live`. | `mock` |
| `VSTUDIO_AUTOPILOT_ENABLED` | Enable the `POST /autopilot/run` HTTP route (delivers adversarial payloads Localization OS's admission seam must refuse or flag - a TEST affordance). Off → the route 404s. The CLI + roundtrip harness are unaffected. | *(unset → OFF)* |
| `VSTUDIO_HOST` / `VSTUDIO_PORT` | Bind. | `127.0.0.1` / `8100` |
| `VSTUDIO_TRUSTED_PROXY_IPS` | Comma-separated IPs/CIDRs of a fronting proxy whose `X-Forwarded-For` may be trusted (validated, then passed to uvicorn as `forwarded_allow_ips` with proxy headers enabled). Set **only** when a real proxy fronts the app; behind a platform proxy (when `FLY_APP_NAME` is set) it is required (the measured proxy /32s), and run.py logs a boot ERROR while it is empty there. A wildcard (`*`), an unparseable entry, or any network broader than IPv4 `/8` or IPv6 `/32` **refuses to start**. | *(unset → proxy headers OFF; the socket peer IS the client IP - never trust loopback implicitly)* |

---

## The wire contract (what Vendor Studio speaks)

Pinned in one module - `vstudio/contract.py` - transcribed by hand from (never importing)
`app/services/vendor_dispatch.py`, `app/services/vendor_return.py`, and the
`POST /webhooks/vendor-returns` route in `app/web/api.py`. Contract version **1.0**.

- **Inbound dispatch** → `POST /intake/dispatch`. Headers: `x-locstudio-signature: sha256=<hex>`
  (HMAC-SHA256 over the RAW body), `x-locstudio-claim`, `x-locstudio-contract-version`.
  Body: `{contract_version, run_id, step_key, step_name, claim_token, source_locale,
  target_locale, targets, segments:[{id, source, target, state, tm_match_pct?,
  placeholders?, provenance?, match_score?, locked?, locked_reason?, char_limit?}],
  assets:{glossary?, style_guide?, context?}}`. The `provenance/match_score/locked/
  locked_reason/char_limit` fields and the glossary block are **VP-0 enrichment** - the
  current contract does not carry them yet; Vendor Studio reads them when present and
  infers/degrades gracefully when absent.
- **Outbound return** → `POST {VSTUDIO_LOCSTUDIO_URL}/webhooks/vendor-returns`, signed the
  same way, with `x-locstudio-claim`. Body: `{contract_version, claim_token, target_locale,
  segments:[{id, target}], actual_cost_usd?, actual_currency?}`. Every dispatched id returns
  exactly once, cumulatively across partial tranches; locked/unedited ids are returned as
  dispatched.
- **Poll target** → `GET /intake/poll` with `x-locstudio-claim`. Serves a signed completed
  return, or `204` when nothing is ready - compatible with Localization OS's `poll_leg`.
- **XLIFF export (Vendor Wave D)** → `GET /projects/{id}/package?format=xliff` and
  `GET /projects/{id}/export?format=xliff` render the package / return as **XLIFF 1.2**
  (`.xlf`) via Vendor Studio's own independent writer (`vstudio/xliff.py`, stdlib
  ElementTree - no shared code with Localization OS). **Intake stays JSON-canonical**; XLIFF is a
  download convenience. **Contract v1 rule:** placeholder tokens (`⟦xN⟧`) are exported as
  **literal text** - inline XLIFF tags (`<ph>`, `<g>`, `<bpt>`, …) inside a `<source>`/
  `<target>` are **not supported** and a file carrying them is refused by the parser, as is
  any file declaring a DOCTYPE/DTD.

---

## SSO handoff token spec (for the Localization OS "Open Vendor Studio" train)

That mint endpoint is a **separate train** (not built here). It must **POST** a form to
`/sso` with the token in the request **body** (never the query string - a token in a URL
lands in access logs, referrers and history). The token format:

```
POST /sso   (application/x-www-form-urlencoded)   token=<the token below>

token       = b64url(payload) + "." + hex( HMAC_SHA256(VSTUDIO_SSO_SECRET, b64url(payload)) )
payload     = {"user": "<display name or email>", "exp": <unix epoch seconds>,
               "iat": <epoch>, "aud": "vendor-studio", "jti": "<random, unique per token>",
               "role": "<the operator's Localization OS role>",
               "org": <org id or null>, "mo": <true when multi-org is active>}
b64url      = URL-safe base64 of the compact JSON (no padding)
TTL         = mint with exp = now + 60s   (Vendor Studio refuses an exp more than 120s ahead)
```

The role claim is signed, so it cannot be edited in transit. It maps with the same table
as the API-key path (admin and manager become PM, reviewer and linguist become Reviewer,
translator becomes Translator); a missing, unknown or viewer role is refused, so there is
no default PM session. A token is also refused when `aud` is not `vendor-studio`, when
`jti` is missing, or when it has been used before (single use; the cache lives in the one
Studio process). `POST /sso` checks the browser `Origin`: only this Studio or the
configured `VSTUDIO_LOCSTUDIO_URL` origin may submit a token (a cross-site page cannot log
a victim in as the attacker); a non-browser client with no `Origin` relies on the token. `VSTUDIO_LOCSTUDIO_URL` must therefore be set for the SSO handoff to work
(without it only a same-origin POST passes, so the real handoff is refused).
A token with non-ASCII characters is a normal 403.

**Multi-org.** On a multi-org Localization OS install the token and `/api/account` carry
the operator's org. Set `VSTUDIO_ORG_ID` to the one org this Studio serves: an identity from
any other org, or one that states no org, is refused. Without it, a multi-org identity is
refused (fail closed) rather than any org's admin becoming PM. Single-tenant installs need
no setting.

Vendor Studio verifies the signature constant-time over the shared `VSTUDIO_SSO_SECRET`,
checks `exp`, and on success sets its own session cookie (no cookie/code sharing with Loc
Studio). A bare `GET /sso` just shows the login wall. **Fallback:** a user can paste a Loc
Studio API key at `/login`, which Vendor Studio verifies by calling
`{VSTUDIO_LOCSTUDIO_URL}/api/account` over the wire (rate-limited per IP; the key is
never logged). The session role is bound to the key owner's real role there: admin and
manager become PM, reviewer and linguist become Reviewer, translator becomes Translator;
anything else (viewer, unknown, an unreadable answer) is refused. `/persona` can only
switch a PM session between personas; every other session is pinned to its own role.
PM-only routes (assign, cost, quote, cancel, deliver, complete, QA settings, wire log,
autopilot) return 403 to other roles, and approve/reject need the Reviewer persona.
The role is read at login and kept in the signed session cookie. Vendor Studio does not
keep the API key, so it cannot ask Localization OS again later; instead a session older
than `VSTUDIO_ROLE_RECHECK_SECONDS` (default 3600) is refused and the user signs in again
(one click through "Open Vendor Studio", or pasting the key), which re-reads the role. A
demoted user therefore loses rights within that interval, not 12 hours. `0` turns the
re-check off and leaves only `VSTUDIO_SESSION_MAX_AGE`. The SSO token is a short-lived,
single-use bearer credential; the POST body keeps it out of shared logs.

---

## Run

### Local

```powershell
$env:VSTUDIO_SIGNING_SECRET = "<same as the Localization OS vendor's signing_secret>"
$env:VSTUDIO_SSO_SECRET     = "<shared SSO secret>"
$env:VSTUDIO_LOCSTUDIO_URL  = "https://localization-os.example.com"
python tools\vendor_studio\run.py     # serves 127.0.0.1:8100
```

### Container deploy (separate app)

```bash
cd tools/vendor_studio
docker build -t vendor-studio .
docker run -d --name vendor-studio -p 127.0.0.1:8100:8100 -v vstudio_data:/data \
    -e VSTUDIO_SIGNING_SECRET=... -e VSTUDIO_SSO_SECRET=... \
    -e VSTUDIO_LOCSTUDIO_URL=https://localization-os.example.com \
    -e VSTUDIO_SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))") \
    vendor-studio
```

Put it behind a TLS-terminating reverse proxy and set `VSTUDIO_TRUSTED_PROXY_IPS` to that proxy's address.
Run one replica only: the rate limits and the SSO replay cache live in process memory.

The image ships only `tools/vendor_studio` (the build context), so it cannot import Loc
Studio code - the isolation is structural at the image level too. SQLite lives on the
`vstudio_data` volume at `/data`.

---

## Headless autopilot

`python -m vstudio.autopilot <scenario> [--project-id N] [--contract dispatch.json]`, or
`POST /autopilot/run {"scenario": "...", "project_id": N}`. Each scenario scripts the
**vendor side only**; Localization OS's reaction is asserted by the repo's
`tests/verify_vendor_studio.py` roundtrip suite.

`happy_path` · `partial_2of3` · `bad_id_missing` · `bad_id_extra` · `bad_id_foreign` ·
`source_echo` · `blank_targets` · `silence` · `late_after_cancel` · `wrong_claim` ·
`bad_signature` · `replay`.

---

## Security notes

- **HMAC-before-parse**: `/intake/dispatch` verifies the signature over the raw body (and
  caps body size) *before* any JSON is deserialized, so an unsigned caller never reaches the
  parser. Unsigned/mis-signed dispatches are `401` with a visible `WireLog` row.
- **Constant-time** comparisons over UTF-8 bytes for every secret/token/signature.
- **No secret is logged or rendered**: the claim token is a bearer credential - it is stored
  to enable delivery but never appears in any template, API response, or `WireLog` reason.
- **Session wall**: every app page requires the session; an unauthenticated request gets a
  minimal 404-style wall that leaks no app detail. Intake/poll are wire-authenticated
  (signature / claim token), not session-authenticated.
- **The return builder never fabricates an id**: it only emits ids that were dispatched;
  adversarial ids come from autopilot corrupting a copy of a correctly-built payload.
- **QA is advisory-only** (decision record in `vstudio/qa_engine.py`):
  the deterministic checks are computed and rendered as context for the human, but they
  **never block Complete or Deliver** - no 409s, no unignorable check, no dismissal
  requirement. `delivery.deliver()` records the findings **snapshot** on the delivery for
  the audit trail (returned as `qa_snapshot`, noted in the outbound `WireLog`) instead of
  refusing. This is safe because the **enforcing side is Localization OS's buyer-side admission
  seam**: its tag-integrity / glossary / source-echo lanes flag a placeholder-broken or
  glossary-violating return `needs_review` on admit. The former `unedited_mt` check was
  removed outright (an unchanged MT means the MT was good). Partial delivery still ships
  only the server-computed CONFIRMED/APPROVED subset, and the return builder still never
  fabricates an id - those are contract guards, not QA guards.
- **Signed vs unsigned returns**: the webhook + poll transports are HMAC-signed. The
  **manual re-upload / export file is unsigned by design** - Localization OS's manual path
  authenticates the *operator's* session/API identity instead (see
  `vendor_return.admit_manual`), and runs the identical content validation. The export
  file (`GET /projects/{id}/export`) and the signed webhook body come from the **one**
  builder (`delivery.build_return_payload`), differing only in transport.
- **Security headers** (CSP with no inline script, `X-Frame-Options: DENY`, nosniff,
  `Referrer-Policy: no-referrer`, HSTS when Secure) are set on every response. Session
  cookies are `Secure`+`HttpOnly`+`SameSite=Lax` with a 12h max-age; set
  `VSTUDIO_COOKIE_INSECURE=1` **only** for a loopback-http dev bind. A non-loopback bind
  hard-fails at startup without `VSTUDIO_SESSION_SECRET` and an `https://` Localization OS URL.

---

## Tests

```powershell
# unit suite (self-contained, main venv)
python -m pytest tools\vendor_studio\tests -q
# the roundtrip acceptance suite (boots both sides; runs via the repo harness)
python -m pytest "tests/test_verify_suites.py::test_verify_suite[verify_vendor_studio]" -q
```
