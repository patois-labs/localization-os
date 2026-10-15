"""HMAC verification, the SSO handoff token, and the session cookie.

SECURITY LAWS (mirroring the Localization OS side, vendor_return.py):
  * HMAC verification happens over the RAW body BEFORE any deserialization.
  * All secret/token comparisons are CONSTANT-TIME over UTF-8 bytes (hmac.compare_digest
    raises TypeError on a non-ASCII str, and both sides are attacker-supplied).
  * No secret or claim token is ever logged or rendered.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from vstudio.config import settings
from vstudio.contract import SIGNATURE_PREFIX


# --- constant-time helpers ---------------------------------------------------
def ct_eq(a, b) -> bool:
    """Constant-time equality over UTF-8 bytes (never the str objects)."""
    return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))


# --- inbound dispatch signature (verify BEFORE parse) ------------------------
def compute_signature(secret: str, raw_body: bytes) -> str:
    """The `sha256=<hex>` signature for a raw body, matching vendor_dispatch.dispatch."""
    mac = hmac.new((secret or "").encode("utf-8"), raw_body or b"", hashlib.sha256)
    return SIGNATURE_PREFIX + mac.hexdigest()


def verify_signature(secret: str, raw_body: bytes, header_value: str) -> bool:
    """Constant-time HMAC-SHA256 verification over the RAW body. Accepts the
    `sha256=<hex>` form (and a bare hex digest). Every failure mode — no secret, no
    header, bad prefix, non-hex, wrong length, wrong value — returns False through the
    same path; the compare is over the raw digest BYTES so a length mismatch cannot be
    timed either. Identical semantics to vendor_return.verify_signature."""
    if not secret or not header_value:
        return False
    sent = str(header_value).strip()
    if sent.lower().startswith(SIGNATURE_PREFIX):
        sent = sent[len(SIGNATURE_PREFIX) :].strip()
    if not sent:
        return False
    try:
        sent_bytes = bytes.fromhex(sent)
    except ValueError:
        return False
    expect = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).digest()
    return hmac.compare_digest(sent_bytes, expect)


# --- SSO handoff token -------------------------------------------------------
# FORMAT (documented in README for the Localization OS "Open Vendor Studio" mint):
#   token = b64url(json_payload) + "." + hex(hmac_sha256(sso_secret, b64url_payload))
#   json_payload = {"user": "<display or email>", "exp": <unix epoch seconds>,
#                   "aud": "vendor-studio", "jti": "<unique id>", "role": "<Loc OS role>",
#                   "org": <org id or null>, "mo": <true when multi-org is active>}
# The payload is signed, not secret. Enforced here (#557): audience, a signed role claim
# (mapped like the API-key path), exp <= now + _SSO_MAX_TTL_SECONDS (a captured token cannot be
# minted long-lived), single use per jti, and org scope under multi-org. Anything missing or
# invalid fails closed.
_SSO_TTL_SECONDS = 60
_SSO_MAX_TTL_SECONDS = 120  # TTL plus clock-skew allowance; a longer exp is refused
SSO_AUDIENCE = "vendor-studio"

#: Localization OS role -> Vendor Studio persona (#516). An ALLOW-LIST: any role not named
#: here (viewer, an unknown/new role, a missing field) gets NO session. A vendor-side key
#: or token never maps to PM unless the role is admin or manager.
LOC_ROLE_TO_STUDIO = {
    "admin": "PM",
    "manager": "PM",
    "reviewer": "Reviewer",
    "linguist": "Reviewer",
    "translator": "Translator",
}


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def org_scope_ok(org, multi_org) -> bool:
    """Org scoping for a login (SSO token or API key). VSTUDIO_ORG_ID pins this Studio to ONE
    Localization OS org: the identity's org must equal it. When the Localization OS install
    is multi-org and no org is pinned, refuse (fail closed): otherwise any org's admin would
    map to PM. A single-tenant install (multi_org false) with no pin keeps working."""
    pinned = str(settings.org_id or "").strip()
    if pinned:
        if isinstance(org, bool) or not isinstance(org, (int, str)):
            return False
        return str(org).strip() == pinned
    return not multi_org


def mint_sso_token(
    user: str,
    *,
    secret: str | None = None,
    ttl: int = _SSO_TTL_SECONDS,
    now: int | None = None,
    role: str | None = "admin",
    org=None,
    multi_org: bool = False,
    aud: str | None = SSO_AUDIENCE,
    jti: str | None = None,
) -> str:
    """Mint an SSO token. Provided so tests (and any co-located tooling) can produce a
    token in the exact format the verifier expects; the Localization OS mint
    (app/services/vendor_studio_sso.py) implements the same spec. `role=None`/`aud=None`
    omit the claim (tests of the fail-closed paths)."""
    sec = settings.sso_secret if secret is None else secret
    t = int(now if now is not None else time.time())
    payload: dict = {"user": user, "exp": t + ttl, "iat": t, "mo": bool(multi_org), "org": org}
    payload["jti"] = jti if jti is not None else secrets.token_hex(16)
    if role is not None:
        payload["role"] = role
    if aud is not None:
        payload["aud"] = aud
    body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(sec.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_sso_claims(
    token: str, *, secret: str | None = None, now: float | None = None
) -> dict | None:
    """Return {"user", "role" (the Studio persona), "jti", "exp"} on success, else None.
    Constant-time signature check; every failure (blank secret, malformed or non-ASCII
    token, bad signature, expired, exp too far out, wrong or missing audience, missing or
    unmapped role, missing jti, wrong org scope) fails closed. A blank SSO secret means SSO
    is DISABLED (no token can verify) - never "no signature required". Pure: it does NOT
    consume the jti (see consume_sso_jti)."""
    sec = settings.sso_secret if secret is None else secret
    if not sec or not isinstance(token, str) or not token or "." not in token:
        return None
    # Non-ASCII input would raise UnicodeEncodeError below (a 500, not a refusal): both the
    # body and the signature are attacker-supplied, so refuse it up front.
    if not token.isascii():
        return None
    body, _, sig = token.partition(".")
    if not body or not sig:
        return None
    expect = hmac.new(sec.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    # compare_digest over the hex bytes; both are ASCII hex so this is safe.
    if not hmac.compare_digest(sig.encode("ascii"), expect.encode("ascii")):
        return None
    try:
        payload = json.loads(_b64u_decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    user = payload.get("user")
    jti = payload.get("jti")
    role = payload.get("role")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    if not isinstance(user, str) or not user:
        return None
    if not isinstance(jti, str) or not jti or len(jti) > 128:
        return None
    if payload.get("aud") != SSO_AUDIENCE:
        return None
    studio_role = LOC_ROLE_TO_STUDIO.get(role) if isinstance(role, str) else None
    if not studio_role:
        return None
    t = now if now is not None else time.time()
    # json.loads accepts NaN/Infinity: a NaN exp would pass both comparisons below and then
    # never expire from the replay cache. Only a finite exp can be checked, so refuse the rest.
    if not math.isfinite(float(exp)):
        return None
    if t > float(exp) or float(exp) > t + _SSO_MAX_TTL_SECONDS:
        return None
    # An absent or odd "mo" is treated as multi-org: only an explicit false is single-tenant.
    if not org_scope_ok(payload.get("org"), payload.get("mo") is not False):
        return None
    return {"user": user, "role": studio_role, "jti": jti, "exp": float(exp)}


def verify_sso_token(
    token: str, *, secret: str | None = None, now: float | None = None
) -> str | None:
    """The token's `user` on success, else None (see verify_sso_claims)."""
    claims = verify_sso_claims(token, secret=secret, now=now)
    return claims["user"] if claims else None


# Single-use jti cache. In-process, which is correct only because Vendor Studio runs as ONE
# process (one replica; same constraint as the API-key rate limit). A
# restart empties it, but a token lives at most _SSO_MAX_TTL_SECONDS, so the replay window
# across a restart is that short.
_SEEN_JTI: dict[str, float] = {}
_SEEN_JTI_LOCK = threading.Lock()
_SEEN_JTI_MAX = 10_000


def consume_sso_jti(jti: str, exp: float, *, now: float | None = None) -> bool:
    """Record a jti as used. True the first time, False for a replay (or when the cache is
    full of live entries, which fails closed rather than forgetting one)."""
    t = now if now is not None else time.time()
    with _SEEN_JTI_LOCK:
        for k in [k for k, e in _SEEN_JTI.items() if e < t]:
            _SEEN_JTI.pop(k, None)
        if jti in _SEEN_JTI or len(_SEEN_JTI) >= _SEEN_JTI_MAX:
            return False
        _SEEN_JTI[jti] = float(exp)
        return True


def reset_sso_replay_cache() -> None:
    with _SEEN_JTI_LOCK:
        _SEEN_JTI.clear()


def sso_origin_ok(origin: str | None, sec_fetch_site: str | None, own_origin: str) -> bool:
    """Login-CSRF gate for POST /sso (#557). The legitimate handoff IS a cross-site form
    POST from the Localization OS page, so "same-origin only" would break it; instead the
    browser-supplied Origin must be this Studio itself or the configured Localization OS
    origin. No Origin: a browser marks a cross-site request with Sec-Fetch-Site, which is
    refused; a request carrying neither header is a non-browser client (the signed,
    single-use token is its only credential)."""
    allowed = {own_origin.lower()} if own_origin else set()
    loc = (settings.locstudio_url or "").lower()
    if loc:
        from urllib.parse import urlsplit

        p = urlsplit(loc)
        if p.scheme and p.netloc:
            allowed.add(f"{p.scheme}://{p.netloc}")
    if origin is not None:
        return origin.strip().lower() in allowed
    sfs = (sec_fetch_site or "").strip().lower()
    return sfs in ("", "same-origin", "none")


# --- session cookie ----------------------------------------------------------
COOKIE_NAME = "vstudio_session"
SESSION_SALT = "vstudio-session-v2"
_serializer: URLSafeTimedSerializer | None = None


def _ser() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        # "-v2" (#516): cookies minted before roles were bound to the key carried a
        # self-assigned PM role; the new salt makes every one of them fail to verify, so an
        # upgrade does not leave those sessions alive for the rest of session_max_age.
        _serializer = URLSafeTimedSerializer(settings.session_secret, salt=SESSION_SALT)
    return _serializer


def reset_serializer() -> None:
    global _serializer
    _serializer = None


def encode_session(data: dict) -> str:
    """Sign a session. `role_at` stamps when the role was last read from Localization OS
    (login); a persona switch carries the existing stamp forward, never refreshes it."""
    return _ser().dumps({**data, "role_at": data.get("role_at", int(time.time()))})


def decode_session(raw: str) -> dict | None:
    """Decode + verify the session cookie, enforcing the configured max-age (M1). An
    expired, tampered, or malformed cookie fails closed (None)."""
    if not raw:
        return None
    try:
        out = _ser().loads(raw, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(out, dict):
        return None
    # #557 role re-check: the role is read once at login and a demotion is not visible here,
    # so a session older than VSTUDIO_ROLE_RECHECK_SECONDS is refused and the user signs in
    # again (SSO or API key), which re-reads the role. No credential is stored server-side.
    limit = settings.role_recheck_seconds
    if limit > 0:
        role_at = out.get("role_at")
        if isinstance(role_at, bool) or not isinstance(role_at, (int, float)):
            return None
        if time.time() - float(role_at) > limit:
            return None
    return out
