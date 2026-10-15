# Browser Surface - Architecture (L1)

## 1. Role

Automated polls and fetch-based SSE streams send `X-Loc-Background` with value `1`.
The session service checks idle and absolute expiry but does not advance last activity for
these requests. Status transitions reload the page once. Trusted human input retains the
existing keepalive behavior. Pregate requires Chromium keyboard tests for every template or
static-file change. An explicit `a11y-browser-waive: <reason>` brief line records a waiver.

Anonymous `/health` is DB-free and returns only status and coarse version. Operational detail
is at `/admin/health` for a signed-in administrator. The container sends tailscaled output to
container stderr so the platform's log rotation applies, disables uvicorn access logs to keep
invite tokens out of them, and caps uvicorn concurrency at 256. External proxy access logs
must also scrub invite paths. Existing OpenAPI authentication and disabled Swagger/ReDoc remain.

This subsystem owns the **browser-hardening posture** of the two web apps: the app-wide HTTP
security-header middleware. It stamps a baseline set of hardening headers onto every response
from the customer app, the only mount since the back-office sub-app was removed on 2026-09-20 - `X-Content-Type-Options: nosniff`,
`Referrer-Policy`, a restrictive `Permissions-Policy`, framing protection, and HSTS on HTTPS
responses - without ever clobbering a header a route already set for itself.

The one subtle, load-bearing design choice is *where* the middleware is installed.
`app/web/security_headers.py::apply_security_headers` wraps the app's
`build_middleware_stack` rather than calling `add_middleware`, so the header layer becomes the
**outer response-header layer** - outside Starlette's `ServerErrorMiddleware`, which renders
unhandled 500s. A plain `add_middleware` layer never sees a 500 response, so those error pages
would otherwise ship with zero security headers.

Framing defaults to `X-Frame-Options: DENY`;
a deployment that must embed the web UI sets an origin allowlist, and the middleware emits a
`frame-ancestors` CSP directive instead (an allowlist that `X-Frame-Options` cannot express),
appending it to a route's own CSP when one exists so framing protection is never dropped.

What it does **not** own. It does not own the two other browser-security seams that live in
the spine `app/main.py`: the CSRF *validation* (`app/main.py::_csrf_ok`, inside the
`app/main.py::_security_audit` middleware) and the Secure/HttpOnly session cookie
(`starlette.middleware.sessions.SessionMiddleware`). It does not own CSRF *token generation*, which is
`app/web/deps.py::csrf_token` - a module [identity-and-access](identity-and-access.md) claims.
Script CSP is enforced on HTML; style CSP remains out of scope. The latin-1 encoding
hardening of non-ASCII *request* headers
and download `Content-Disposition` filenames is a separate sibling concern (`verify_nonascii_headers`),
not this subsystem. Those are cross-referenced here, not re-claimed.

## 2. Invariants

- **Every response carries the baseline headers, and a route's own header always wins.**
  `app/web/security_headers.py::SecurityHeadersMiddleware` sets each baseline header only when
  the route did not already set it (case-insensitive), so a per-download `nosniff` or an SVG
  CSP is never overwritten - enforced by `verify_security_headers`.
- **Headers reach even unhandled 500s.** `app/web/security_headers.py::apply_security_headers`
  wraps `build_middleware_stack`, placing the normal header middleware outside request processing - outside
  `ServerErrorMiddleware` and any later-added middleware (e.g. the observability layer).
- **HSTS is emitted only on HTTPS, and never over-pins subdomains.** The middleware adds
  `Strict-Transport-Security` (max-age 180 days) only when the request scheme is https, with
  no `preload` and no `includeSubDomains`, so tunnel subdomains are not force-pinned to https.
- **The edge owns HSTS in a hosted deployment (#614).** Behind Cloudflare, Caddy or nginx
  (`deploy/Caddyfile`, `deploy/aws-hosted/caddy/Caddyfile.cloudflare`, `deploy/nginx.conf`) the
  proxy sends `max-age=31536000; includeSubDomains` and that value is the one browsers see:
  a whole-domain policy belongs to the layer that owns the domain (Caddy's deferred `header`
  block replaces the app's value; nginx drops it with `proxy_hide_header`, since browsers
  honour only the first `Strict-Transport-Security` header). The app's 180-day,
  no-`includeSubDomains` header is only the baseline for an install with no such edge (a single
  host, or a tunnel), and it never emits `includeSubDomains` because the app cannot know what
  else lives under the domain. The two values differ on purpose; a stricter edge wins. To
  change the production policy, change the edge config, not `_HSTS_VALUE`.
- **Framing protection survives a route's own CSP.** When an origin allowlist is configured
  and a route already set a CSP lacking `frame-ancestors`, the middleware appends the
  directive rather than replacing the header - so an SVG CSP never silently drops framing.
- **The one env-derived header value is validated at boot, so a mis-set knob fails the boot
  instead of corrupting or injecting a response header.** The framing allowlist is the only
  header value that is not a fixed constant - it is emitted verbatim into the `frame-ancestors`
  CSP directive - so `app/config.py::_validate_frame_ancestors` rejects CR/LF and other
  control characters, a `;` CSP-directive separator, and non-ASCII in `LOC_TMS_FRAME_ANCESTORS`
  at import (raising `app/config.py::BrowserSurfaceConfigError`) - enforced by
  `verify_security_headers`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Presence-sensitive form fields | `app/web/_forms.py::supplied_form` | None means absent; supplied empty strings reach workflow, settings and vendor edit contracts unchanged. |
| Page errors (WCAG 3.3.1 / 3.3.3) | `app/web/form_errors.py::redirect_with_error` | One code-to-sentence map and resolver behind every `?err=` / `?error=` redirect; messages are percent-encoded onto a same-site path, and `app/templates/_form_errors.html` renders the focused `role="alert"` summary plus `aria-invalid` on the field. When that field is below the fold, `app/static/form_errors.js` focuses the field instead and adds the summary sentence to its `aria-describedby` (#564, WCAG 2.2 SC 2.4.11). |
| Query-string messages and signed-in caching | `app/web/page_guard.py::PageGuardMiddleware` | Free text in `?err=` / `?error=` / `?msg=` / `?notice=` and the other `*_error` / `*_msg` / `*_warning` parameters is shown only when this session was redirected there by a mutating request (recorded as a hash in the session) or a GET route recorded it with `page_guard.attest`; the `/auth` pages take known codes only. Unrecorded, only KNOWN codes pass (an unknown code or a short token from a link prints nothing). Redirects answering `/auth` requests are never recorded, and the one post-sign-in `next` sanitizer (the OIDC module's `_sanitize_next`, shared by SAML, MFA and the password change; documented in sso-provisioning) strips every message parameter. Count parameters in success banners render through `|int`. Signed-in HTML is sent `Cache-Control: no-store`. |
| The header middleware | `app/web/security_headers.py::SecurityHeadersMiddleware` | Pure-ASGI middleware that stamps the baseline headers, framing, and HSTS on every response. |
| Outermost installation | `app/web/security_headers.py::apply_security_headers` | Wraps `build_middleware_stack` so headers also cover Starlette's 500-rendering `ServerErrorMiddleware`. |
| Request body caps | `app/web/body_limit.py::BodyLimitMiddleware` | Pure-ASGI middleware just inside the session middleware: a body over its cap is a 413 before any of it is read (a declared `Content-Length` is refused unread, a chunked body is counted as it streams). Sign-in, webhook and SCIM paths and every caller that is not signed in get `REQUEST_MAX_ANON_BODY_BYTES` (1 MiB); a signed-in caller, or any caller while sign-in is off, gets `REQUEST_MAX_BODY_BYTES` (the upload cap sent base64 in JSON, plus room). |

A supplied empty string on a field with a non-empty `Form(default)` is folded to that default by FastAPI (unchanged since 0.115.6); only the presence-sensitive sites use `supplied_form`. Starlette 1.6.0 enforces `max_fields` and `max_part_size` for both urlencoded and multipart forms. Routes that call `request.form()` rely on Starlette's defaults (1,000 files, 1,000 fields and 1 MiB per part); binary upload routes retain their separate decoded-byte caps through `read_capped_upload`.

The CSRF and cookie seams this posture depends on live in the spine `app/main.py`, which the
L0 map covers, and are cross-referenced rather than claimed: CSRF requests are validated by
`app/main.py::_csrf_ok` (an Origin/Referer-equals-Host check inside `app/main.py::_security_audit`),
the session cookie is marked Secure/HttpOnly by `starlette.middleware.sessions.SessionMiddleware`, and
the per-session CSRF token is minted by `app/web/deps.py::csrf_token` (claimed by
[identity-and-access](identity-and-access.md)).

## 4. Data & settings

- **No settings key owns this.** There is deliberately **no** `settings.get` key gating the
  security headers; the baseline is unconditional. Framing
  is set at the host by `app/config.py::FRAME_ANCESTORS` (from the environment variable
  `LOC_TMS_FRAME_ANCESTORS`, validated at boot): blank means `X-Frame-Options: DENY`, an
  origin list switches to a `frame-ancestors` CSP directive.
- **Script policy.** `LOC_TMS_CSP_MODE` accepts `enforce` (default), `report-only`, or `off`.
  Empty or whitespace-only values use `enforce`; other invalid values fail boot.
  Each request gets `secrets.token_urlsafe(16)` in
  `request.state.csp_nonce`; the Jinja global `csp_nonce()` reads the same value.
  Every inline script carries it. Event handlers use listeners, never HTML attributes.
  HTML receives `script-src 'self' 'nonce-<request nonce>'; object-src 'none';`
  and `base-uri 'none'; form-action 'self';`. Route policies, including SVG downloads, survive;
  JSON and downloads get no script policy. Framing merges into the enforced header and
  remains enforced even in report-only/off mode. The admin Security page displays the mode.
  This is host configuration, with no API, CLI/MCP, or web setting toggle.
- **External forms.** Vendor Studio's handoff allows only its configured studio origin on
  that response. Connector forms allow only their configured OAuth provider's authorization
  origin, including POST-to-303 redirect checks. The validator rejects wildcards,
  credentials, whitespace, and directive injection. SAML starts with a GET redirect and
  receives an inbound ACS POST; IdP-hosted forms use the IdP's policy. The OIDC control is
  a button-styled link to `/auth/oidc/login?next=...`, with the return path URL-encoded.
  It navigates directly with GET: a form submission would make Chromium enforce
  `form-action` on the IdP redirect too. Session-bound, one-use state, nonce validation,
  and PKCE protect the OIDC flow. Neither sign-in link needs another form origin here.
- **No model.** Only request-local nonce and form-origin state is held. The
  HSTS max-age and the baseline header set are module constants in
  `app/web/security_headers.py`.

## 5. Enforced by

- `verify_script_csp` pins markup, fresh matching nonces, modes, HTML/error coverage,
  JSON/download/SVG preservation, and configured form origins. `verify_a11y_browser`
  checks browser console CSP violations while visiting key pages.
- `verify_request_limits` - the request body caps (anonymous 413 before reading, including a
  chunked body; signed-in callers keep the upload cap and are refused past it) and the upload
  read caps shared with the document filters.
- `verify_security_headers` - the baseline header set, the never-clobber-a-route rule, the
  https-only HSTS, the framing default vs. allowlist behaviour, the outermost installation that
  covers 500s, and the boot-time `LOC_TMS_FRAME_ANCESTORS` validation (a bad value fails loud).

## 6. Battle scars

- **App-wide security headers were a HIGH finding, now remediated.** The middleware
  (nosniff, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options` DENY / the
  `LOC_TMS_FRAME_ANCESTORS` CSP, HSTS on https), installed outside error handling so it
  stamps 500s too, took gap G-01 from HIGH to LOW - with the residual noted below.
- **The headers + secure-cookie posture was inventoried across all four surfaces.** The
  four-surface coverage audit recorded security headers and secure cookies as middleware
  hardening present on every surface (row A27).

## 7. Limits & accepted risks

- **Style CSP is not enforced.** Script CSP allows same-origin assets and nonced inline
  blocks. Output escaping remains required. Selecting report-only or off gives up script
  enforcement until the operator restores enforce.
- **The header layer hardens the browser edge, not the transport.** It sets HSTS but does not
  terminate TLS or redirect http→https itself; the deployment (the reverse proxy) owns
  the actual HTTPS boundary, and HSTS is advisory to a browser that already arrived over https.

## Host rejection before request processing

P1a adds `app/main.py::HostBoundaryMiddleware` outside the normal security-header
stack. It rejects malformed or disallowed Host before any routing or session work.
Its separate rejection response is also wrapped in SecurityHeadersMiddleware, so
400 responses retain the same baseline headers. The normal header stack remains
outside ServerErrorMiddleware and covers 500s. `verify_security_headers` pins both
branches; `verify_ingress_boundary` checks rejection and the nosniff header.

The runtime is Starlette 1.7.0, but this boundary intentionally validates the raw
ASGI `Host` header list before Starlette constructs `request.url`. HTTP/1.0 without
Host still falls back to `scope["server"]`; malformed, duplicate, missing HTTP/1.1,
and allowlist-forged Host values retain the documented 400 `Invalid Host` refusal.
