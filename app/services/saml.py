"""SAML 2.0 Service Provider (Train I, BETA — the designated sprint overflow).

An SP-initiated SAML 2.0 login built on python3-saml (OneLogin), which is an OPT-IN
extra (it needs the xmlsec native bits; NOT in base requirements — see
requirements-dev.txt). The library is imported LAZILY inside the flag-guarded paths, so
importing this module at boot costs nothing when SAML is off. ``configure_at_boot`` is
the s3_blobstore-style capability check: with the flag ON and the library missing it
FAILS THE BOOT LOUDLY rather than letting the install serve a login button that 500s.

SECURITY POSTURE (the gates the adversarial review probes):
  * signature REQUIRED on the response OR the assertion — an unsigned assertion is
    rejected (OneLogin strict mode + wantAssertionsSigned).
  * audience restriction enforced (strict mode compares Audience to the SP entityId).
  * InResponseTo correlation for SP-initiated: the stored AuthnRequest id is passed to
    process_response, so a response that does not answer our request is rejected.
  * a replay cache on assertion IDs (with expiry) rejects a re-POSTed assertion.
  * IdP-initiated SSO (a response with no InResponseTo) is REFUSED unless
    saml_allow_idp_initiated is explicitly on (default refuse).
  * RelayState is sanitized to same-origin paths only (no open redirect).
  * the ACS is CSRF-exempt (an IdP POST carries no CSRF token) but validates the signed
    assertion ONLY — the assertion IS the credential.
  * a NameID-only assertion (no AttributeStatement) is ACCEPTED (SAML 2.0 makes the
    AttributeStatement optional; identity comes from the signed NameID). This relaxes a
    data-completeness default, NOT authenticity: the assertion must still be signed
    (wantAssertionsSigned) and carry a NameID (wantNameId) — see build_settings.

MULTI-ORG: a brand-new JIT account has no org context on the SAML path (unlike SCIM,
whose key names the org), so under the sacred multi-org gate NEW JIT provisioning is
REFUSED (accounts are provisioned via SCIM there); an already-provisioned account still
logs in. Single-tenant (gate off) does full JIT like the OIDC path.

Documented in: docs/architecture/sso-provisioning.md
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import itsdangerous

from app.services import security, settings

_logger = logging.getLogger("loc_tms")
PROXY_WARNING = ""

# assertion-id -> unix expiry; a consumed assertion cannot be replayed until it expires.
_REPLAY: dict[str, float] = {}
_REPLAY_LOCK = threading.Lock()

# The clock-skew tolerance (seconds) applied to the assertion Conditions NotBefore /
# NotOnOrAfter validation. Pinned to the spec's +/-5 min EXPLICITLY (below, in _auth) so
# it never silently drifts with a python3-saml version change (its default happens to be
# 300 today, but we do not rely on that default).
_CLOCK_SKEW_SECONDS = 300


class SamlError(Exception):
    """A SAML configuration or validation failure (surfaced as a clean login error)."""


class SamlUnavailable(SamlError):
    """The python3-saml library (OneLogin) is not installed but SAML is enabled."""


# --------------------------------------------------------------------------- #
# capability + flag
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """Strict on-value whitelist (matches the delegated feature services)."""
    return str(settings.get("saml_enabled", "0")) == "1"


def available() -> bool:
    """Whether the OneLogin python3-saml library can be imported (the opt-in extra)."""
    try:
        import onelogin.saml2.auth  # noqa: F401

        return True
    except Exception:
        return False


def configure_at_boot() -> None:
    """s3_blobstore pattern: FAIL THE BOOT LOUDLY when SAML is enabled but the library
    is missing, so an install never serves a SAML button that 500s. A no-op when the
    flag is off (the library is never imported)."""
    global PROXY_WARNING
    PROXY_WARNING = ""
    configured = settings.external_base_url() if enabled() else ""
    if enabled() and not security.proxy_networks(settings.get("trusted_proxy_ips")):
        PROXY_WARNING = (
            "SAML behind a proxy needs trusted_proxy_ips; X-Forwarded-Proto is not honored"
        )
        if configured:
            PROXY_WARNING = (
                "trusted_proxy_ips is unset: forwarded client identity is not honored; "
                "audit rows behind a proxy record the proxy IP. "
                "SAML external origin comes from app_base_url."
            )
        _logger.warning(PROXY_WARNING)
    if not enabled():
        return
    if not available():
        raise SamlUnavailable(
            "saml_enabled=1 but the python3-saml library is not installed. In the "
            "container image, rebuild it with --build-arg WITH_SAML=1; on a host install, "
            "pip install --no-deps -r requirements-saml.lock over requirements.lock. "
            "Or set saml_enabled=0."
        )


# --------------------------------------------------------------------------- #
# settings assembly
# --------------------------------------------------------------------------- #
def _sp_entity_id(request) -> str:
    val = (settings.get("saml_sp_entity_id") or "").strip()
    if val:
        return val
    return settings.external_url(request, "/auth/saml/metadata")


def _acs_url(request) -> str:
    val = (settings.get("saml_sp_acs_url") or "").strip()
    if val:
        return val
    return settings.external_url(request, "/auth/saml/acs")


def _sp_private_key() -> str:
    """The SP private key (Fernet-decrypted from saml_sp_private_key_enc), or ''."""
    try:
        return settings.read_secret_setting("saml_sp_private_key_enc") or ""
    except Exception:
        return settings.get("saml_sp_private_key_enc") or ""


def build_settings(request, require_idp: bool = True) -> dict:
    """Assemble the OneLogin settings dict from our stored settings. With require_idp
    (login / ACS) a missing/invalid IdP metadata is a clean SamlError; the SP-metadata
    endpoint passes require_idp=False so an admin can fetch the SP metadata to hand to
    the IdP BEFORE the IdP metadata is configured (an empty idp block is fine there)."""
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

    idp: dict = {"entityId": "", "singleSignOnService": {"url": "", "binding": ""}, "x509cert": ""}
    idp_xml = (settings.get("saml_idp_metadata_xml") or "").strip()
    if idp_xml:
        try:
            parsed = OneLogin_Saml2_IdPMetadataParser.parse(idp_xml)
        except Exception as exc:
            raise SamlError("The IdP metadata XML could not be parsed.") from exc
        idp = parsed.get("idp") or idp
    if require_idp and (not idp.get("entityId") or not idp.get("singleSignOnService")):
        raise SamlError("SAML is not configured: paste the IdP metadata XML on the Security page.")

    sp_cert = (settings.get("saml_sp_x509_cert") or "").strip()
    sp_key = _sp_private_key().strip()
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": _sp_entity_id(request),
            "assertionConsumerService": {
                "url": _acs_url(request),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            "x509cert": sp_cert,
            "privateKey": sp_key,
        },
        "idp": idp,
        "security": {
            # Signature REQUIRED on the assertion (unsigned -> rejected). We accept a
            # signed RESPONSE or a signed ASSERTION (wantMessagesSigned stays False so a
            # response that signs only the assertion is fine); wantAssertionsSigned makes
            # an entirely-unsigned response fail.
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "wantNameId": True,
            # AttributeStatement is OPTIONAL in SAML 2.0: an assertion may carry identity in
            # the NameID alone (emailAddress format), with NO attributes. python3-saml's
            # default (wantAttributeStatement=True) REJECTS any such assertion, which breaks
            # real NameID-only IdPs — Okta with no attribute statements configured sends
            # exactly this and every login was refused with "There is no AttributeStatement on
            # the Response". Relaxing it is a DATA-COMPLETENESS relaxation, NOT a security one:
            # authenticity is unchanged because wantAssertionsSigned=True still requires the
            # assertion (or response) to be signed and wantNameId=True still requires a NameID,
            # so an attacker cannot inject identity — the NameID we trust is inside the signed
            # assertion. _email_from() falls back to the NameID when no email attribute is
            # present, and _display_name_from() returns "" (create_user tolerates it), so a
            # NameID-only assertion still yields a usable identity.
            "wantAttributeStatement": False,
            "requestedAuthnContext": False,
            "rejectUnsolicitedResponsesWithInResponseTo": False,
        },
    }


def _prepare(request, post_data: dict | None = None, *, routed_path: str) -> dict:
    """Prepare OneLogin's external origin for SAML Destination validation.

    A configured app_base_url supplies scheme, host and port, making Destination
    validation correct behind a TLS-terminating proxy. Without it, retain the
    historical Host/ASGI calculations exactly, including the IPv6 http_host form.
    When configured, the base's PATH is the external prefix, replacing root_path.
    The web caller supplies the canonical mount-relative routed_path.
    OneLogin cannot handle IPv6 literal hosts; use a DNS hostname for SAML.
    """
    configured = settings.external_base_url()
    if configured:
        url = urlsplit(configured)
        scheme = url.scheme
        host = url.hostname
        if ":" in host:
            host = f"[{host}]"
        if url.port and url.port != (443 if scheme == "https" else 80):
            host = f"{host}:{url.port}"
        script_name = url.path.rstrip("/") + routed_path
    else:
        # Host syntax is validated at ingress; the scheme comes only from ASGI.
        url = urlsplit("//" + request.headers.get("host", "localhost"))
        scheme = request.scope.get("scheme", "http")
        host = url.hostname or "localhost"
        if url.port:
            host = f"{host}:{url.port}"
        script_name = security.request_path(request)
    return {
        "https": "on" if scheme == "https" else "off",
        "http_host": host,
        "server_port": str(url.port or (443 if scheme == "https" else 80)),
        "script_name": script_name,
        "get_data": dict(request.query_params),
        "post_data": post_data or {},
    }


def _auth(request, post_data: dict | None = None, *, routed_path: str):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    from onelogin.saml2.constants import OneLogin_Saml2_Constants

    # Pin the clock-skew window to the spec's +/-5 min explicitly (the Conditions timestamp
    # validation in OneLogin_Saml2_Response reads this constant), rather than relying on the
    # library's default. Idempotent + cheap; set before any response validation runs.
    OneLogin_Saml2_Constants.ALLOWED_CLOCK_DRIFT = _CLOCK_SKEW_SECONDS
    return OneLogin_Saml2_Auth(
        _prepare(request, post_data, routed_path=routed_path), build_settings(request)
    )


# --------------------------------------------------------------------------- #
# RelayState sanitizer (open-redirect guard) — same rule as oidc._sanitize_next
# --------------------------------------------------------------------------- #
def sanitize_next(nxt: str) -> str:
    """Only same-site absolute paths survive; anything else becomes '/'. The ONE rule,
    shared with OIDC and the password sign-in (gate fix-auth-easy round 1): it also drops
    message parameters (?err=, ?msg=, ...) so RelayState cannot carry a phishing message
    through a real sign-in."""
    from app.services import oidc

    return oidc._sanitize_next(nxt)


# --------------------------------------------------------------------------- #
# SP-initiated correlation cookie (the cross-site-POST fix)
# --------------------------------------------------------------------------- #
# WHY THIS EXISTS: SP-initiated correlation state (the AuthnRequest id, for InResponseTo
# enforcement, + the post-login `next`) used to live ONLY in the Starlette session, whose
# cookie is deliberately SameSite=Lax (app/main.py — a load-bearing CSRF layer we must not
# weaken). But the IdP delivers the assertion as a CROSS-SITE top-level POST to the ACS, and
# browsers withhold an explicit-Lax cookie on a cross-site POST (Chrome's 2-minute Lax grace
# applies only to cookies with NO SameSite attribute, not to an explicit Lax one). So the
# session was absent at the ACS and every REAL SP-initiated login was refused as "unsolicited"
# (a same-site test client never reproduces this). The fix is a DEDICATED, signed, one-shot
# cookie that DOES ride the cross-site POST (SameSite=None; Secure), carrying only the
# correlation state — the global session cookie keeps its Lax CSRF posture untouched.
#
# SECURITY POSTURE (the gate probes forged / replayed / fixated cookies + open-redirect):
#   * SIGNED with itsdangerous under the install's SESSION_SECRET and a SAML-specific salt
#     (domain separation), so it is neither forgeable without the secret nor reusable across
#     installs (each install has its own session secret). The salt keeps it distinct from the
#     session cookie's own signing even though both derive from SESSION_SECRET.
#   * A bad-signature OR expired OR malformed cookie is treated as ABSENT — we fall through to
#     the session lookup and, failing that, the SAME non-enumerable unsolicited refusal. It is
#     NEVER trusted and NEVER produces a distinguishable error (refusal non-enumerability).
#   * TTL of 300s (an in-flight login is seconds-to-minutes); a future-dated `iat` (clock games)
#     is rejected the same way.
#   * The `next` in the payload is STILL run through sanitize_next() at consumption — a valid
#     signature does not make a poisoned redirect target trustworthy (defense in depth).
#   * One-shot: the ACS route DELETES the cookie on every outcome (success and refusal). The
#     assertion replay cache + InResponseTo correlation remain the actual anti-replay controls;
#     a captured cookie alone buys an attacker nothing (it still needs a fresh, correlated,
#     signed assertion answering that request id, which the replay cache burns on first use).
TXN_COOKIE_NAME = "loc_saml_txn"
_TXN_TTL_SECONDS = 300
# Domain-separation label mixed into the HMAC key (itsdangerous `salt`), versioned so a future
# format change can rotate it. Distinct from any other signed-cookie use of SESSION_SECRET.
_TXN_SALT = "loc-tms:saml-txn:v1"


def _txn_serializer() -> itsdangerous.URLSafeSerializer:
    """The signer for the correlation cookie, keyed on the install's SESSION_SECRET with a
    SAML-specific salt. config is imported lazily (like the onelogin bits) so importing this
    module never forces a config resolution it does not already need."""
    from app import config

    return itsdangerous.URLSafeSerializer(config.SESSION_SECRET, salt=_TXN_SALT)


def encode_txn(request_id: str, next_path: str) -> str:
    """Serialize + sign the correlation payload {rid, next, iat} into the cookie value."""
    payload = {"rid": request_id or "", "next": next_path or "/", "iat": int(time.time())}
    return _txn_serializer().dumps(payload)


def decode_txn(token: str | None) -> tuple[str | None, str | None]:
    """Verify + parse the correlation cookie. Return (request_id, next_path) on a valid,
    unexpired, well-formed cookie, else (None, None). A missing / tampered / expired /
    malformed cookie yields (None, None) INDISTINGUISHABLY — the caller then falls back to the
    session and, failing that, the standard unsolicited refusal. `next` is returned raw here
    and MUST be sanitize_next()'d by the caller before use."""
    if not token:
        return None, None
    try:
        data = _txn_serializer().loads(token)
    except Exception:
        # BadSignature / malformed base64 / anything: treat as absent, never trusted.
        return None, None
    if not isinstance(data, dict):
        return None, None
    iat = data.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool):
        return None, None
    now = time.time()
    # Expired OR implausibly future-dated (clock games) -> treat as absent.
    if now - iat > _TXN_TTL_SECONDS or iat - now > _TXN_TTL_SECONDS:
        return None, None
    rid = data.get("rid")
    if not isinstance(rid, str) or not rid:
        return None, None
    nxt = data.get("next")
    if not isinstance(nxt, str):
        nxt = "/"
    return rid, nxt


def set_txn_cookie(response, request, request_id: str, next_path: str) -> None:
    """Set the one-shot correlation cookie on `response`. ONLY on https: SameSite=None
    REQUIRES Secure and browsers reject None-without-Secure, so on a plain-http install we
    skip it entirely and the session fallback keeps status-quo behavior. Path-scoped to
    /auth/saml/ so it rides ONLY the SAML endpoints, never the rest of the app."""
    if request.scope.get("scheme") != "https":
        return
    response.set_cookie(
        TXN_COOKIE_NAME,
        encode_txn(request_id, next_path),
        max_age=_TXN_TTL_SECONDS,
        path="/auth/saml/",
        httponly=True,
        secure=True,
        samesite="none",
    )


def clear_txn_cookie(response) -> None:
    """Delete the one-shot correlation cookie (matching name + path + attributes so the
    browser reliably overwrites it). Called on EVERY ACS outcome — success and refusal."""
    response.delete_cookie(
        TXN_COOKIE_NAME,
        path="/auth/saml/",
        httponly=True,
        secure=True,
        samesite="none",
    )


# --------------------------------------------------------------------------- #
# SP-initiated login
# --------------------------------------------------------------------------- #
def begin_login(request, *, routed_path: str) -> tuple[str, str, str]:
    """Build the AuthnRequest redirect URL, stash the request id + sanitized RelayState in the
    session (the same-site / http fallback for InResponseTo correlation + the post-login
    redirect), AND return (url, request_id, next_path) so the route can also mint the signed
    cross-site correlation cookie (the primary path for a real IdP's cross-site ACS POST)."""
    auth = _auth(request, routed_path=routed_path)
    next_path = sanitize_next(request.query_params.get("next", "/"))
    url = auth.login(return_to=next_path)
    request_id = auth.get_last_request_id()
    request.session["saml_request_id"] = request_id
    request.session["saml_next"] = next_path
    return url, request_id, next_path


# --------------------------------------------------------------------------- #
# ACS (assertion consumer)
# --------------------------------------------------------------------------- #
def _prune_replay(now: float) -> None:
    dead = [k for k, exp in _REPLAY.items() if exp <= now]
    for k in dead:
        _REPLAY.pop(k, None)


def _remember_assertion(assertion_id: str, not_on_or_after: str | None) -> bool:
    """Record a consumed assertion id; return False if it was already consumed (replay).
    Expiry is the assertion's NotOnOrAfter (parsed) or now+10min as a floor."""
    now = time.time()
    exp = now + 600.0
    if not_on_or_after:
        try:
            dt = datetime.fromisoformat(not_on_or_after.replace("Z", "+00:00"))
            exp = max(exp, dt.timestamp())
        except Exception as exc:
            # Replay-guard expiry falls back to the now+10min floor; surface the bad value.
            _logger.warning(
                "SAML NotOnOrAfter unparsable (%s); replay guard uses the 10-minute floor",
                type(exc).__name__,
            )
    with _REPLAY_LOCK:
        _prune_replay(now)
        if assertion_id and assertion_id in _REPLAY:
            return False
        if assertion_id:
            _REPLAY[assertion_id] = exp
    return True


#: python3-saml's validation reasons, by their fixed leading text, mapped to a fixed label.
#: The label is what gets logged; the reason itself can quote sender-chosen fields (gate
#: ldap-logpriv round 1, INFO-1: a Destination carrying a newline forged a log line).
_REASON_CLASSES = (
    ("The InResponseTo of the Response", "in_response_to_mismatch"),
    ("The response was received at", "destination_mismatch"),
    ("The response has an empty Destination", "destination_missing"),
    ("Invalid issuer", "issuer_mismatch"),
    ("Issuer of the", "issuer_invalid"),
    ("Signature validation failed", "signature_invalid"),
    ("No Signature found", "signature_missing"),
    ("The Assertion of the Response is not signed", "signature_missing"),
    ("The Message of the Response is not signed", "signature_missing"),
    ("Could not validate timestamp: expired", "expired"),
    ("Could not validate timestamp: not yet valid", "not_yet_valid"),
    ("The attributes have expired", "expired"),
    ("A valid SubjectConfirmation was not found", "subject_confirmation_invalid"),
    ("The status code of the Response was not Success", "status_not_success"),
    ("Invalid SAML Response. Not match the saml-schema", "schema_invalid"),
    ("Unsupported SAML version", "unsupported_version"),
    ("Missing ID attribute", "missing_id"),
    ("SAML Response must contain 1 assertion", "assertion_count"),
    ("Unexpected number of", "signature_count"),
    ("The Assertion must include", "assertion_incomplete"),
    ("The AuthnContext", "authn_context_mismatch"),
    ("NameID not found", "nameid_invalid"),
    ("An empty NameID", "nameid_invalid"),
    ("Deprecated", "deprecated_algorithm"),
    ("Duplicated", "duplicated_element"),
)
_ERROR_CODE_RE = re.compile(r"[a-z_]{1,40}")
_MAX_ERROR_CODES = 5


def _reason_class(reason) -> str:
    """A fixed label for a python3-saml validation reason; never the reason text itself."""
    text = str(reason or "")
    if not text:
        return "none"
    if text.endswith("is not a valid audience for this Response"):
        return "audience_mismatch"
    if "is not encrypted and the SP require" in text:
        return "encryption_required"
    for prefix, label in _REASON_CLASSES:
        if text.startswith(prefix):
            return label
    return "other"


def _safe_error_codes(errors) -> str:
    """python3-saml's error codes (fixed snake_case words) joined by commas; anything not of
    that shape is written as `other`, and at most five are kept."""
    codes = [
        c if isinstance(c, str) and _ERROR_CODE_RE.fullmatch(c) else "other"
        for c in list(errors or [])[:_MAX_ERROR_CODES]
    ]
    return ",".join(codes) or "none"


def _answers_request(auth, post_data: dict, stored_request_id) -> bool:
    """True only when SIGNED content of an already-validated response names
    `stored_request_id`: the InResponseTo of a signed Response (message signature), or the
    InResponseTo of a bearer SubjectConfirmationData inside the signed assertion.

    The Response element's own InResponseTo is NOT enough on its own: wantMessagesSigned is
    False, so an assertion-only signature leaves the outer Response unsigned, and a captured
    IdP-initiated assertion could be rewrapped in a forged Response naming the attacker's own
    started login (#482 gate). SAML Web SSO profile 4.1.4.2 requires the bearer
    SubjectConfirmationData InResponseTo in an SP-initiated answer, so real IdPs send it.
    Call only after auth.process_response succeeded (signatures already verified on these
    exact bytes)."""
    if not stored_request_id:
        return False
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.response import OneLogin_Saml2_Response
    from onelogin.saml2.utils import OneLogin_Saml2_Utils
    from onelogin.saml2.xml_utils import OneLogin_Saml2_XML

    resp = OneLogin_Saml2_Response(auth.get_settings(), post_data.get("SAMLResponse"))
    # A signature on the ROOT Response (the exact xpath process_response verified) covers
    # the Response's InResponseTo.
    root_signed = bool(
        OneLogin_Saml2_XML.query(resp.document, OneLogin_Saml2_Utils.RESPONSE_SIGNATURE_XPATH)
    )
    if root_signed and resp.get_in_response_to() == stored_request_id:
        return True
    # _query_assertion selects the assertion by the ID its signature references (the
    # library's own wrapping-safe lookup, used by its SubjectConfirmation check).
    for scn in resp._query_assertion("/saml:Subject/saml:SubjectConfirmation"):
        method = scn.get("Method")
        if method and method != C.CM_BEARER:
            continue
        scd = scn.find("saml:SubjectConfirmationData", namespaces=C.NSMAP)
        if scd is not None and scd.get("InResponseTo") == stored_request_id:
            return True
    return False


def process_acs(request, post_data: dict, *, routed_path: str):
    """Validate a POSTed SAML Response. Returns (attrs, error) where attrs carries
    {"nameid", "email", "attributes", "next"} on success, or (None, "<reason>") on any
    validation failure. The stored request id enforces InResponseTo correlation for
    SP-initiated flows; an unsolicited (IdP-initiated) response is refused unless
    saml_allow_idp_initiated is on."""
    # Correlation state resolution: PREFER the signed cross-site cookie (the only state that
    # survives the IdP's cross-site top-level POST — the session's SameSite=Lax cookie is
    # withheld there), then fall back to the session (same-site clients / plain-http installs).
    # A bad / expired / forged cookie decodes to (None, None) and falls through to the session
    # and then the standard refusal — never trusted, never a distinguishable error.
    cookie_rid, cookie_next = decode_txn(request.cookies.get(TXN_COOKIE_NAME))
    if cookie_rid is not None:
        stored_request_id = cookie_rid
        # sanitize_next even on a validly-signed payload (defense in depth vs a poisoned next).
        next_path = sanitize_next(cookie_next or "/")
    else:
        stored_request_id = request.session.get("saml_request_id")
        next_path = sanitize_next(request.session.get("saml_next") or "/")
    # RelayState may also carry the post-login target (IdP echoes it back) — it takes precedence.
    relay = post_data.get("RelayState")
    if relay:
        next_path = sanitize_next(relay)
    # burn the one-shot SESSION correlation keys regardless of outcome (the cookie is deleted
    # by the ACS route on every outcome; see clear_txn_cookie).
    request.session.pop("saml_request_id", None)
    request.session.pop("saml_next", None)

    try:
        auth = _auth(request, post_data, routed_path=routed_path)
    except SamlError as exc:
        return None, str(exc)

    # IdP-initiated refusal: a response with NO InResponseTo is unsolicited. Refuse it
    # unless the operator explicitly opted in.
    allow_idp_initiated = str(settings.get("saml_allow_idp_initiated", "0")) == "1"
    if stored_request_id is None and not allow_idp_initiated:
        return None, "Unsolicited (IdP-initiated) SAML responses are not accepted."

    try:
        # request_id enforces InResponseTo correlation (None => IdP-initiated, only
        # reached when explicitly allowed above).
        auth.process_response(request_id=stored_request_id)
    except Exception as exc:
        _logger.info("SAML process_response raised: %s", type(exc).__name__)
        return None, "The SAML response could not be validated."

    errors = auth.get_errors()
    if not errors and not allow_idp_initiated:
        # #482: python3-saml compares InResponseTo only when the response carries one, so a
        # signed IdP-initiated assertion (no InResponseTo) would pass whenever ANY login had
        # started. With IdP-initiated off, the response must answer THIS request, and the
        # answer must come from SIGNED bytes (see _answers_request).
        answered = False
        with contextlib.suppress(Exception):
            answered = _answers_request(auth, post_data, stored_request_id)
        if not answered:
            return None, "Unsolicited (IdP-initiated) SAML responses are not accepted."
    if errors:
        # Whoever POSTs to the (unauthenticated) ACS controls parts of the library's reason
        # text (Destination, Issuer, InResponseTo), so only fixed labels reach the log.
        _logger.info(
            "SAML validation errors: codes=%s reason=%s",
            _safe_error_codes(errors),
            _reason_class(auth.get_last_error_reason()),
        )
        return None, "The SAML response failed validation."
    if not auth.is_authenticated():
        return None, "The SAML response did not authenticate a user."

    # Replay guard on the assertion id (with expiry from NotOnOrAfter).
    assertion_id = None
    with contextlib.suppress(Exception):
        assertion_id = auth.get_last_assertion_id()
    not_after = None
    try:
        ts = auth.get_session_expiration()
        if ts:
            not_after = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception as exc:
        _logger.warning(
            "SAML session expiration unreadable (%s); replay guard uses the 10-minute floor",
            type(exc).__name__,
        )
    if assertion_id and not _remember_assertion(assertion_id, not_after):
        return None, "This SAML assertion has already been used (replay refused)."

    nameid = auth.get_nameid()
    attributes = auth.get_attributes() or {}
    return (
        {
            "nameid": nameid,
            "email": _email_from(nameid, attributes),
            "attributes": attributes,
            "next": next_path,
        },
        None,
    )


def _email_from(nameid: str, attributes: dict) -> str:
    """Prefer an explicit email attribute, then a NameID that looks like an email."""
    for key in (
        "email",
        "emailAddress",
        "mail",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    ):
        vals = attributes.get(key)
        if vals:
            v = vals[0] if isinstance(vals, list) else vals
            if v and "@" in str(v):
                return str(v).lower().strip()
    if nameid and "@" in nameid:
        return nameid.lower().strip()
    return ""


def _display_name_from(attributes: dict) -> str:
    for key in (
        "displayName",
        "name",
        "cn",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    ):
        vals = attributes.get(key)
        if vals:
            v = vals[0] if isinstance(vals, list) else vals
            if v:
                return str(v).strip()
    return ""


# --------------------------------------------------------------------------- #
# SP metadata
# --------------------------------------------------------------------------- #
def metadata(request) -> str:
    """The SP metadata XML (for handing to the IdP). Raises SamlError if the assembled
    SP settings are invalid."""
    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    saml_settings = OneLogin_Saml2_Settings(
        build_settings(request, require_idp=False), sp_validation_only=True
    )
    meta = saml_settings.get_sp_metadata()
    errors = saml_settings.validate_metadata(meta)
    if errors:
        raise SamlError("The SP metadata is invalid: " + ", ".join(errors))
    return meta.decode("utf-8") if isinstance(meta, bytes) else meta
