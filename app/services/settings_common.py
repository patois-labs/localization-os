"""settings: shared leaf helpers (layered pure move)

Documented in: docs/architecture/configuration.md
"""

import math
from urllib.parse import urlsplit, urlunsplit

from app import config

_REDACTED_URL_MARKER = "_endpoint"


_REDACTED_URL_KEYS = frozenset({"local_base_url"})


#: Substring marking a settings key as an ENDPOINT URL that may embed credentials in
#: its userinfo (https://user:pass@host) or query (?X-Amz-Security-Token=...). Such a
#: value is NOT a whole secret — the scheme+host is legitimate operator info (WHICH
#: collector / object store is configured) — so it is neither is_secret_key (which would
#: collapse it to (set)/(unset)) nor stored encrypted. It IS credential-bearing on a
#: READ surface, though: its own module deliberately never logs it, yet the config read
#: (GET /api/admin/config, `config list`/`get`) printed the value verbatim. So these
#: keys are PARTIALLY redacted on read — see redact_url_value. Currently matches exactly
#: `otel_endpoint` and `blobstore_s3_endpoint_url`; any future `*_endpoint`/`*_endpoint_url`
#: is covered by construction. (A plain base URL like `vendor_studio_url` is out of scope
#: here; add it to _REDACTED_URL_KEYS if it ever carries embedded credentials.)
#: EXPLICIT credential-bearing base-URL keys that carry no `_endpoint` marker (P3 T1 round-2
#: M2). A private SearXNG instance is normally reached at `https://user:pass@host`, so its base
#: URL must be partially redacted on read exactly like an `_endpoint` key -- this is the "add it
#: to an explicit set if it ever carries embedded credentials" case the marker note names. The
#: write door (`_v_searxng_base_url`) already REFUSES userinfo, so this is defense in depth for a
#: value planted straight into `set_many` (which bypasses the door).
def is_redacted_url_key(key: str) -> bool:
    """True when `key` names a base/endpoint URL whose value must be PARTIALLY redacted on
    any read surface (userinfo + query stripped, scheme+host+path kept). See
    _REDACTED_URL_MARKER and _REDACTED_URL_KEYS. Mutually exclusive with is_secret_key by
    construction (no such key carries a secret-marker substring)."""
    k = (key or "").lower()
    return _REDACTED_URL_MARKER in k or k in _REDACTED_URL_KEYS


def redact_url_value(value: str) -> str:
    """Render an endpoint-URL setting for a READ surface: keep scheme://host[:port]/path
    so an operator can see WHERE telemetry/blobs go, but strip any embedded credential
    material — userinfo (`user:pass@`), query string, and fragment — that a module keeps
    out of its own logs. An empty value renders empty (an unset key, like any plain key).

    Only a well-formed absolute URL (scheme://host…) is partially shown; anything else
    — a scheme-less `user:pass@host:port`, a garbled value — is masked WHOLE to "(set)"
    so credential material sitting in an unexpected position can never leak. Never raises."""
    if not value:
        return value
    try:
        if "://" not in value:
            return "(set)"
        parts = urlsplit(value)
        host = parts.hostname
        if not parts.scheme or not host:
            return "(set)"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        shown = urlunsplit((parts.scheme, host, parts.path or "", "", ""))
        # Signal a present-and-stripped query so the operator knows the stored value
        # carries more than what is shown (a token could hide only in the query).
        if parts.query or parts.fragment:
            shown += "?(redacted)"
        return shown
    except Exception:
        return "(set)"


class InvalidSettingValue(ValueError):
    """A money-bearing / egress setting failed its per-key value validator (bad number,
    unknown provider, or an SSRF-blocked SearXNG host). Every one of the FOUR write doors
    (the /api config spine, `config set`, `config apply`, the admin web-verification form)
    calls validate_value() before writing, so this surfaces as a clean 4xx / usage error at
    each, never a silently-stored garbage value (P3 round-2 M1 / round-3 M5)."""


def _v_nonneg_number(key: str, value) -> None:
    """A finite, non-negative number (the spend.cap_usd read-side clamp precedent applied at
    the WRITE door): rejects 'abc', '-1', 'inf'/'nan', and an absurd overflow."""
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError) as e:
        raise InvalidSettingValue(f"{key} must be a number (got {value!r}).") from e
    if not math.isfinite(v) or v < 0:
        raise InvalidSettingValue(f"{key} must be a finite non-negative number (got {value!r}).")


def _v_nonneg_int(key: str, value) -> None:
    """A non-negative INTEGER with a sane upper bound (P3 round-1 L6): the per-unit search cap
    is a small retry ceiling, so '2.7' (a float that then fails int() at read and silently falls
    back), a negative, and an absurd '999999' are all refused at the WRITE door rather than
    clamped only on read."""
    s = str(value).strip()
    try:
        v = int(s)
    except (TypeError, ValueError) as e:
        raise InvalidSettingValue(f"{key} must be a whole number (got {value!r}).") from e
    if v < 0 or v > 100:
        raise InvalidSettingValue(f"{key} must be an integer between 0 and 100 (got {value!r}).")


def _v_reconcile_grace_seconds(key: str, value) -> None:
    """A whole number of seconds in 0..86400 (one day) for the guarded-purge reconciler's grace
    window. Refuses negatives, non-numerics and an absurd bound at the WRITE door so a typo
    cannot silently disable (or unbound) a data-deletion safety guard; 0 is a legitimate value
    (reconcile immediately, used by the race tests). The read side (_reconcile_grace_s) still
    fails safe to the default, but a garbage store never reaches it."""
    s = str(value).strip()
    try:
        v = int(s)
    except (TypeError, ValueError) as e:
        raise InvalidSettingValue(
            f"{key} must be a whole number of seconds (got {value!r})."
        ) from e
    if v < 0 or v > 86400:
        raise InvalidSettingValue(
            f"{key} must be an integer between 0 and 86400 seconds (got {value!r})."
        )


def _v_local_engine_url(key, value):
    """Empty means unconfigured; other values pass the local engine egress door."""
    v = str(value or "").strip()
    if not v:
        return
    from app.services import egress, engines  # lazy: settings is imported very early
    from app.services.credential_origin import require_readable_origin

    try:
        engines._refuse_url_credentials(v)
        # Gate round 2 MEDIUM-1: a URL whose origin cannot be read is refused, so the
        # credential pin and httpx can never disagree on a stored slot URL.
        require_readable_origin(v, "The local engine URL")
        egress.validate_local_engine_target(v)
    except (egress.EgressBlocked, ValueError) as exc:
        raise InvalidSettingValue(f"{key}: {exc}") from exc


def _v_egress_list(key, value):
    from app.services.egress import parse_allowlist

    try:
        parse_allowlist(value)
    except ValueError as exc:
        raise InvalidSettingValue(f"{key}: {exc}") from exc


def _parse_external_base_url(value, *, legacy_host=False):
    """Shared external-origin parser; legacy host recovery is only for pin validation."""
    value = str(value or "").strip()
    url = urlsplit(value)
    if legacy_host:
        return url if url.netloc else urlsplit("//" + value)
    if not value:
        return url
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or "#" in value
        or "?" in value
        or "\\" in value
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value)
    ):
        raise ValueError(
            "expected an HTTP(S) base URL with a host and no credentials, query or fragment"
        )
    from app.services.security import host_name

    host_name(url.netloc)
    return url


def _v_app_base_url(key: str, value) -> None:
    """URL syntax only, not an egress policy: HTTP(S), host, no userinfo/fragment."""
    try:
        _parse_external_base_url(value)
    except ValueError as exc:
        raise InvalidSettingValue(f"{key}: {exc}") from exc


def _hours_on(raw) -> bool:
    """True when `raw` (a stored/default audit_chain_verify_hours value) means monitoring is
    effectively ON: a positive integer OR an unparseable value (which verify_hours() maps to
    the default 24h, i.e. ON). Only a literal non-positive integer is OFF."""
    try:
        return int(str(raw).strip()) > 0
    except (TypeError, ValueError):
        return True  # garbage -> default 24h -> ON


def _hours_off(raw) -> bool:
    """True when `raw` is a literal non-positive integer (scheduled verification OFF)."""
    try:
        return int(str(raw).strip()) <= 0
    except (TypeError, ValueError):
        return False


# --- encrypted-at-rest secret settings (Fernet, like connector credentials) ---
# A few Settings hold true secrets (e.g. the LDAP service-bind password) that
# must not sit in the DB in cleartext. These store the Fernet ciphertext in the
# ordinary Setting value; the plaintext only ever exists in memory. The key is
# config.ENCRYPTION_KEY (env-first / keyfile — the same key connector credentials
# and encrypted uploads use), so losing data/.enc_key makes them unrecoverable
# (a re-entry on the Security page fixes that — they are not primary data).
def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(config.ENCRYPTION_KEY.encode())
