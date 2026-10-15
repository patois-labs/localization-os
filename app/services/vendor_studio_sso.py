"""SSO handoff-token mint for the "Open Vendor Studio" operator button (VP-0 / SSO train).

Vendor Studio (the vendor-side mini-TMS, a SEPARATE Fly app, wire-only isolated from
`app/`) accepts a short-lived signed handoff token so an operator already authenticated
here lands authenticated there without re-entering a credential. The token spec is pinned
in Vendor Studio's README (transcribed here, never imported — the two sides share only the
secret + the algorithm):

    token   = b64url(payload) + "." + hex( HMAC_SHA256(sso_secret, b64url(payload)) )
    payload = {"user": "<display name or email>", "exp": <unix epoch seconds>,
               "iat": <epoch>, "aud": "vendor-studio", "jti": "<random, single use>",
               "role": "<the operator's Localization OS role>", "org": <org id or null>,
               "mo": <true when multi-org is active>}
    b64url  = URL-safe base64 of the COMPACT json, NO padding
    TTL     = mint with exp = now + 60s  (Vendor Studio refuses an exp more than 120s out)

#557: the role claim is signed by this app, so Vendor Studio maps it (admin/manager -> PM,
reviewer/linguist -> Reviewer, translator -> Translator) instead of granting PM to every SSO
session; the jti makes the token single use; `aud` stops a token minted for another consumer
of the same secret; `org` + `mo` let Vendor Studio pin itself to one org under multi-org.

The token is delivered by a POST FORM BODY to `{vendor_studio_url}/sso` (never a query
string — a token in a URL lands in access logs / referrers / history), by the mint route.

CONFIG (host/env + local CLI only, deliberately NOT settable over /api — see
settings.REMOTE_IMMUTABLE_KEYS): `vendor_studio_url` (plain) and `vendor_studio_sso_secret`
(Fernet-encrypted at rest via settings.SECRET_SETTING_KEYS). Either may also be supplied
as an env var (VENDOR_STUDIO_URL / VENDOR_STUDIO_SSO_SECRET) which takes precedence, so a
Fly-secret deploy needs no DB write. When EITHER is unset the handoff is disabled: the
button is hidden and the mint route 404s (non-enumerable).

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

#: The handoff-token lifetime (seconds). Vendor Studio enforces the same 60s window; a
#: token is a replay-sensitive bearer credential only within it.
TTL_SECONDS = 60


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("localhost", "127.0.0.1", "::1", "[::1]")


def studio_url() -> str:
    """The configured Vendor Studio base URL (no trailing slash), env-first. "" when unset
    OR when the scheme is not https on a non-loopback host: the mint route POSTs a LIVE
    bearer credential to this URL, so it must never travel over cleartext http to a remote
    host (http is tolerated only for a loopback dev bind). A rejected URL degrades to
    "unconfigured" (button hidden, route 404s) rather than shipping a credential in the
    clear."""
    raw = os.environ.get("VENDOR_STUDIO_URL")
    if not raw:
        from app.services import settings

        raw = settings.get("vendor_studio_url") or ""
    url = str(raw or "").strip().rstrip("/")
    if not url:
        return ""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme == "https":
        return url
    if scheme == "http" and _is_loopback_host(parts.hostname or ""):
        return url
    return ""  # non-https to a remote host (or a scheme-less/garbage value): refuse


def sso_secret() -> str:
    """The shared HMAC secret for the handoff token, env-first, else the Fernet-decrypted
    setting. "" when unset (which disables the handoff — never a blank-secret token)."""
    raw = os.environ.get("VENDOR_STUDIO_SSO_SECRET")
    if raw:
        return str(raw).strip()
    from app.services import settings

    return (settings.read_secret_setting("vendor_studio_sso_secret") or "").strip()


def is_configured() -> bool:
    """True IFF BOTH the URL and the shared secret are set — the gate the button render
    and the mint route both consult (unconfigured -> button hidden, route 404s)."""
    return bool(studio_url()) and bool(sso_secret())


def _b64url(raw: bytes) -> str:
    """URL-safe base64 with the padding stripped (the token format's `b64url`)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


#: The audience claim; Vendor Studio refuses a token whose `aud` differs.
AUDIENCE = "vendor-studio"


def mint_token(
    user: str,
    *,
    role: str,
    org_id: int | None = None,
    multi_org: bool = False,
    secret: str | None = None,
    now: float | None = None,
    ttl: int = TTL_SECONDS,
) -> str:
    """Mint a handoff token for `user` carrying the signed `role`, exactly per the README
    algorithm. `role` is the operator's Localization OS role (required: Vendor Studio fails
    closed on a token without one). `secret` defaults to the configured shared secret; `now`
    (epoch seconds) is injectable for tests. Raises ValueError when no secret or no role is
    available (never mints an unsigned, blank-secret or role-less token)."""
    sec = secret if secret is not None else sso_secret()
    if not sec:
        raise ValueError("no vendor_studio_sso_secret configured; cannot mint a token.")
    if not str(role or "").strip():
        raise ValueError("a role is required to mint a Vendor Studio token.")
    t = int(now if now is not None else time.time())
    payload = json.dumps(
        {
            "user": str(user or ""),
            "exp": t + int(ttl),
            "iat": t,
            "aud": AUDIENCE,
            "jti": secrets.token_hex(16),
            "role": str(role).strip().lower(),
            "org": org_id,
            "mo": bool(multi_org),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    body = _b64url(payload)
    sig = hmac.new(sec.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"
