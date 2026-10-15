"""SAML 2.0 SP verification (Train I, BETA; offline, hermetic).

Drives the real SP (app/services/saml.py + the /auth/saml routes) against MOCKED
signed-assertion fixtures: a throwaway RSA keypair stands in for the IdP, so a valid
SAML Response is signed at runtime with that key and the matching cert is published in
the IdP metadata. Every security gate the spec names is probed:

  FLAG-OFF          every /auth/saml path 404s (opt_in, non-enumerable). No lib needed.
  METADATA          the SP metadata endpoint serves valid XML; the login page shows the
                    SAML button only when enabled.
  VALID LOGIN       a signed, audience-correct, InResponseTo-correlated assertion logs in
                    and JIT-provisions the account (auth_source=saml).
  UNSIGNED          an unsigned response is refused (signature required).
  NAMEID-ONLY       a signed assertion with NO AttributeStatement logs in (email from the
                    NameID); the security dict pins wantAttributeStatement=False together with
                    wantAssertionsSigned/wantNameId=True; an UNSIGNED NameID-only one is refused.
  TAMPERED          a byte flipped after signing breaks the signature -> refused.
  AUDIENCE          a wrong Audience -> refused.
  EXPIRED           a past NotOnOrAfter -> refused.
  INRESPONSETO      a response that does not answer our AuthnRequest -> refused.
  IDP-INITIATED     an unsolicited response (no InResponseTo) is refused unless opted in.
  REPLAY            a re-POSTed assertion is refused by the replay cache.
  RELAYSTATE        an off-site RelayState is sanitized to '/' (no open redirect).
  SCIM LIFECYCLE    a SCIM-deactivated (active=false) account cannot log in via SAML.

If python3-saml (the opt-in extra) is NOT importable, only FLAG-OFF runs and the suite
notes that interop tests are skipped (INSIDE the sprint cut line) — still ending "OK".

ASCII-only output; standalone one-process-per-suite script (LOC_TMS_DATA_DIR is a
throwaway temp dir, provider keys cleared -> MockProvider, deterministic + free).

Run standalone: python tests/verify_saml.py  ->  prints "... SAML OK".
"""

import base64
import os
import re
import sys
import time
import urllib.parse
import zlib
from datetime import datetime, timedelta
from unittest.mock import patch
from xml.etree import ElementTree

import itsdangerous

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_saml_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Role, User
from app.services import saml as saml_svc
from app.services import security, settings

_BASE = "https://sp.example.com"  # dotted https host: OneLogin rejects bare "testserver"
_IDP_ENTITY = "https://idp.saml.test/metadata"
_IDP_SSO = "https://idp.saml.test/sso"
_ACS = _BASE + "/auth/saml/acs"
_SP_ENTITY = _BASE + "/auth/saml/metadata"


def _client():
    return TestClient(app, base_url=_BASE)


# --------------------------------------------------------------------------- #
# fixture crypto (a throwaway IdP keypair + cert)
# --------------------------------------------------------------------------- #
def _make_idp_keys():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loc-test-idp")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(nm)
        .issuer_name(nm)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2020, 1, 1))
        .not_valid_after(datetime(2035, 1, 1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_b64 = "".join(cert_pem.strip().splitlines()[1:-1])
    return key_pem, cert_pem, cert_b64


def _idp_metadata(cert_b64: str) -> str:
    return (
        '<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{_IDP_ENTITY}">'
        '<IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        '<KeyDescriptor use="signing"><KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#">'
        f"<X509Data><X509Certificate>{cert_b64}</X509Certificate></X509Data>"
        "</KeyInfo></KeyDescriptor>"
        '<SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="{_IDP_SSO}"/>'
        "</IDPSSODescriptor></EntityDescriptor>"
    )


_SAME = object()  # _signed_response: Response InResponseTo defaults to the assertion's


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _signed_response(
    key_pem,
    cert_pem,
    *,
    email,
    in_response_to,
    audience=None,
    acs=None,
    not_on_or_after=None,
    sign=True,
    tamper=False,
    resp_in_response_to=_SAME,
    sign_response=False,
):
    """Build (and, by default, sign the assertion of) a SAML Response for `email`.
    `resp_in_response_to` sets the Response element's InResponseTo separately from the
    assertion's SubjectConfirmationData (default: the same value); `sign_response` also
    signs the outer Response (message signature)."""
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    now = datetime.utcnow()
    audience = audience or _SP_ENTITY
    acs = acs or _ACS
    noa = not_on_or_after or _iso(now + timedelta(minutes=5))
    nb = _iso(now - timedelta(minutes=5))
    aid = "_a" + os.urandom(8).hex()
    subj_inresp = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    assertion = (
        '<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{aid}" Version="2.0" IssueInstant="{_iso(now)}">'
        f"<saml:Issuer>{_IDP_ENTITY}</saml:Issuer>"
        "<saml:Subject>"
        '<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">'
        f"{email}</saml:NameID>"
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData{subj_inresp} NotOnOrAfter="{noa}" Recipient="{acs}"/>'
        "</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{noa}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
        "</saml:AudienceRestriction></saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_iso(now)}" SessionIndex="_s1">'
        "<saml:AuthnContext><saml:AuthnContextClassRef>"
        "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        "</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        '<saml:AttributeStatement><saml:Attribute Name="email">'
        f"<saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>"
        '<saml:Attribute Name="displayName">'
        "<saml:AttributeValue>SAML User</saml:AttributeValue></saml:Attribute>"
        "</saml:AttributeStatement></saml:Assertion>"
    )
    if sign:
        signed = OneLogin_Saml2_Utils.add_sign(assertion, key_pem, cert_pem)
        assertion = signed.decode() if isinstance(signed, bytes) else signed
    if tamper:
        # Flip the signed NameID: the enveloped signature no longer matches.
        assertion = assertion.replace(f">{email}<", ">tampered@x.com<", 1)
    if resp_in_response_to is _SAME:
        resp_in_response_to = in_response_to
    resp_inresp = f' InResponseTo="{resp_in_response_to}"' if resp_in_response_to else ""
    now2 = datetime.utcnow()
    response = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="_r{os.urandom(6).hex()}" Version="2.0" IssueInstant="{_iso(now2)}" '
        f'Destination="{acs}"{resp_inresp}>'
        f"<saml:Issuer>{_IDP_ENTITY}</saml:Issuer>"
        "<samlp:Status><samlp:StatusCode "
        'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
        f"{assertion}</samlp:Response>"
    )
    if sign_response:
        signed = OneLogin_Saml2_Utils.add_sign(response, key_pem, cert_pem)
        response = signed.decode() if isinstance(signed, bytes) else signed
    return base64.b64encode(response.encode()).decode()


def _signed_response_no_attrs(
    key_pem,
    cert_pem,
    *,
    email,
    in_response_to,
    sign=True,
):
    """Build (and sign) a SAML Response whose assertion has NO AttributeStatement: identity
    lives entirely in the emailAddress-format NameID. This is what Okta (no attribute
    statements configured) and many IdPs actually send; python3-saml's default
    wantAttributeStatement=True used to reject it outright."""
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    now = datetime.utcnow()
    noa = _iso(now + timedelta(minutes=5))
    nb = _iso(now - timedelta(minutes=5))
    aid = "_a" + os.urandom(8).hex()
    subj_inresp = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    assertion = (
        '<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{aid}" Version="2.0" IssueInstant="{_iso(now)}">'
        f"<saml:Issuer>{_IDP_ENTITY}</saml:Issuer>"
        "<saml:Subject>"
        '<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">'
        f"{email}</saml:NameID>"
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData{subj_inresp} NotOnOrAfter="{noa}" Recipient="{_ACS}"/>'
        "</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{noa}">'
        f"<saml:AudienceRestriction><saml:Audience>{_SP_ENTITY}</saml:Audience>"
        "</saml:AudienceRestriction></saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_iso(now)}" SessionIndex="_s1">'
        "<saml:AuthnContext><saml:AuthnContextClassRef>"
        "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        "</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        # NOTE: NO <saml:AttributeStatement> here — the NameID is the only identity.
        "</saml:Assertion>"
    )
    if sign:
        signed = OneLogin_Saml2_Utils.add_sign(assertion, key_pem, cert_pem)
        assertion = signed.decode() if isinstance(signed, bytes) else signed
    resp_inresp = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    now2 = datetime.utcnow()
    response = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="_r{os.urandom(6).hex()}" Version="2.0" IssueInstant="{_iso(now2)}" '
        f'Destination="{_ACS}"{resp_inresp}>'
        f"<saml:Issuer>{_IDP_ENTITY}</saml:Issuer>"
        "<samlp:Status><samlp:StatusCode "
        'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
        f"{assertion}</samlp:Response>"
    )
    return base64.b64encode(response.encode()).decode()


def _req_id_from_login(client) -> str:
    """Start SP-initiated login, return the AuthnRequest ID (for InResponseTo)."""
    r = client.get("/auth/saml/login", follow_redirects=False)
    assert r.status_code == 303, ("login should redirect to the IdP", r.status_code)
    loc = r.headers["location"]
    sr = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["SAMLRequest"][0]
    xml = zlib.decompress(base64.b64decode(sr), -15).decode()
    return re.search(r'\bID="([^"]+)"', xml).group(1)


def _post_acs(client, saml_response, relay_state="/"):
    data = {"SAMLResponse": saml_response}
    # relay_state=None => omit RelayState entirely (so a test can exercise the cookie/session
    # `next` in isolation, since RelayState takes precedence over both when present).
    if relay_state is not None:
        data["RelayState"] = relay_state
    return client.post("/auth/saml/acs", data=data, follow_redirects=False)


# Exact refusal message for a missing/unusable correlation state. A tampered / expired /
# forged correlation cookie MUST yield THIS same message (non-enumerable: indistinguishable
# from the plain no-correlation case).
_UNSOLICITED_MSG = "Unsolicited (IdP-initiated) SAML responses are not accepted."


def _set_txn_cookie(client, value):
    """Attach a raw correlation-cookie value to the client's jar (domain-scoped so httpx
    sends it to the ACS). Simulates the browser presenting the cookie on the cross-site POST."""
    client.cookies.set(saml_svc.TXN_COOKIE_NAME, value, domain="sp.example.com")


def _assert_unsolicited_refusal(r, label):
    """A refusal that is BYTE-indistinguishable (same message) from the no-correlation case."""
    assert r.status_code == 401, (f"{label} must be refused (401)", r.status_code, r.text[:200])
    assert _UNSOLICITED_MSG in r.text, (
        f"{label} must show the SAME non-enumerable unsolicited message",
        r.text[:300],
    )


def _configure(**overrides):
    base = {
        "saml_enabled": "1",
        "saml_idp_metadata_xml": _META[0],
        "saml_sp_entity_id": _SP_ENTITY,
        "saml_sp_acs_url": _ACS,
        "saml_allow_idp_initiated": "0",
        "security_auth_required": "1",
        "multi_org_enabled": "0",
    }
    base.update(overrides)
    settings.set_many(base)


# module-level fixture handles (filled in main)
_KEY = None
_CERT = None
_META = [None]


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_flag_off():
    init_db()
    settings.set_many({"saml_enabled": "0"})
    c = _client()
    for method, path in (
        ("get", "/auth/saml/login"),
        ("post", "/auth/saml/acs"),
        ("get", "/auth/saml/metadata"),
    ):
        r = getattr(c, method)(path, follow_redirects=False)
        assert r.status_code == 404, f"flag-off {method} {path} = {r.status_code}"
    # the login page hides the SAML button when off.
    assert "/auth/saml/login" not in _client().get("/auth/login").text
    print("  flag-off: every /auth/saml path 404s; login page hides the SAML button")


def check_metadata_and_button():
    _configure()
    c = _client()
    r = c.get("/auth/saml/metadata")
    assert r.status_code == 200 and "EntityDescriptor" in r.text, (r.status_code, r.text[:200])
    assert "/auth/saml/login" in c.get("/auth/login").text, "login page must show the SAML button"
    # SP metadata is available BEFORE the IdP metadata is configured (an admin needs it to
    # hand to the IdP first).
    _configure(saml_idp_metadata_xml="")
    r2 = _client().get("/auth/saml/metadata")
    assert r2.status_code == 200 and "EntityDescriptor" in r2.text, (
        "SP metadata must be served even before the IdP metadata is configured",
        r2.status_code,
    )
    _configure()  # restore
    print("  metadata: SP metadata XML served (even pre-IdP-config); login shows the SAML button")


def check_rooted_destination_handoff():
    """Real login + signed ACS round trips pin both external-prefix authorities."""
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    original_init = OneLogin_Saml2_Auth.__init__
    seen = []

    def observe(auth, request_data, old_settings=None, custom_base_path=None):
        seen.append(OneLogin_Saml2_Utils.get_self_url_no_query(request_data))
        original_init(auth, request_data, old_settings, custom_base_path)

    try:
        # Root-only fallback, configured pathless origin, configured public prefix.
        for index, base in enumerate(("", _BASE, _BASE + "/tms")):
            _configure(app_base_url=base, saml_sp_acs_url="", saml_sp_entity_id="")
            with TestClient(app, base_url=_BASE, root_path="/tms") as client:
                metadata = client.get("/auth/saml/metadata")
                assert metadata.status_code == 200, metadata.text[:200]
                entity = ElementTree.fromstring(metadata.text)
                published = entity.find(
                    ".//{urn:oasis:names:tc:SAML:2.0:metadata}AssertionConsumerService"
                ).attrib["Location"]
                expected_base = base or _BASE + "/tms"
                assert published == expected_base + "/auth/saml/acs"
                seen.clear()
                with patch.object(OneLogin_Saml2_Auth, "__init__", observe):
                    rid = _req_id_from_login(client)
                    response = _signed_response(
                        _KEY,
                        _CERT,
                        email=f"rooted{index}@saml.test",
                        in_response_to=rid,
                        audience=entity.attrib["entityID"],
                        acs=published,
                    )
                    result = _post_acs(client, response)
                assert seen == [
                    expected_base + "/auth/saml/login",
                    published,
                ], ("OneLogin current_url must match published ACS", base, seen, published)
                assert result.status_code == 303, (base, result.status_code, result.text[:300])
                print(f"  rooted SAML base={base!r}: current_url = published ACS = {published}")
    finally:
        _configure(app_base_url="")


def check_valid_login():
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response(_KEY, _CERT, email="alice@saml.test", in_response_to=rid)
    r = _post_acs(c, resp)
    assert r.status_code == 303, ("valid SAML should log in (303)", r.status_code, r.text[:300])
    assert r.headers["location"] == "/", r.headers.get("location")
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "alice@saml.test")).first()
        assert u is not None and u.auth_source == "saml", (
            "valid SAML must JIT-provision the account"
        )
    print(
        "  valid login: signed + correlated assertion logs in and JIT-provisions (auth_source=saml)"
    )


def _assert_refused(r, label):
    assert r.status_code == 401, (f"{label} must be refused (401)", r.status_code, r.text[:200])


def check_unsigned_refused():
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response(_KEY, _CERT, email="unsigned@saml.test", in_response_to=rid, sign=False)
    _assert_refused(_post_acs(c, resp), "unsigned assertion")
    print("  unsigned: an unsigned assertion is refused (signature required)")


def check_tampered_refused():
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response(_KEY, _CERT, email="tamper@saml.test", in_response_to=rid, tamper=True)
    _assert_refused(_post_acs(c, resp), "tampered assertion")
    print("  tampered: a byte flipped after signing breaks the signature -> refused")


def check_wrong_audience():
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response(
        _KEY, _CERT, email="aud@saml.test", in_response_to=rid, audience="https://evil.test/sp"
    )
    _assert_refused(_post_acs(c, resp), "wrong audience")
    print("  audience: a wrong Audience restriction -> refused")


def check_expired():
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    past = _iso(datetime.utcnow() - timedelta(minutes=10))
    resp = _signed_response(
        _KEY, _CERT, email="exp@saml.test", in_response_to=rid, not_on_or_after=past
    )
    _assert_refused(_post_acs(c, resp), "expired assertion")
    print("  expired: a past NotOnOrAfter -> refused")


def check_inresponseto_mismatch():
    _configure()
    c = _client()
    _req_id_from_login(c)  # establishes a stored request id
    resp = _signed_response(_KEY, _CERT, email="mism@saml.test", in_response_to="_does_not_match")
    _assert_refused(_post_acs(c, resp), "InResponseTo mismatch")
    print("  correlation: a response that does not answer our AuthnRequest -> refused")


#: A forged log line smuggled into sender-chosen response fields (a character reference so
#: XML attribute normalization keeps the newline), plus non-ASCII text.
_FORGED = "FORGED-LINE loc_tms INFO admin signed in ☃"


def check_hostile_acs_log():
    """Gate ldap-logpriv round 1, INFO-1: the ACS is unauthenticated, and python3-saml's
    reason text quotes the Destination and InResponseTo the sender chose. The log line
    carries only fixed labels: no sender text, no newline, ASCII only."""
    import logging

    class _Grab(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    grab = _Grab()
    log = logging.getLogger("loc_tms")
    old_level = log.level
    log.addHandler(grab)
    log.setLevel(logging.DEBUG)
    try:
        _configure()
        c = _client()
        rid = _req_id_from_login(c)
        hostile_acs = "https://evil.test/acs&#10;" + _FORGED
        resp = _signed_response(
            _KEY, _CERT, email="hostile@saml.test", in_response_to=rid, acs=hostile_acs
        )
        _assert_refused(_post_acs(c, resp), "hostile Destination")
        c2 = _client()
        _req_id_from_login(c2)
        resp2 = _signed_response(
            _KEY, _CERT, email="hostile2@saml.test", in_response_to="_x&#10;" + _FORGED
        )
        _assert_refused(_post_acs(c2, resp2), "hostile InResponseTo (not an NCName)")
    finally:
        log.removeHandler(grab)
        log.setLevel(old_level)
    lines = [m for m in grab.lines if m.startswith("SAML validation errors")]
    assert lines == [
        "SAML validation errors: codes=invalid_response reason=destination_mismatch",
        "SAML validation errors: codes=invalid_response reason=schema_invalid",
    ], lines
    for m in grab.lines:
        assert "FORGED" not in m and "evil.test" not in m and "\n" not in m, repr(m)
        assert m.isascii(), repr(m)
    # Unrecognized reasons and odd codes collapse to fixed words; the code list is capped.
    assert saml_svc._reason_class("anything\nFORGED") == "other"
    assert saml_svc._reason_class(None) == "none"
    assert saml_svc._reason_class("https://x is not a valid audience for this Response") == (
        "audience_mismatch"
    )
    assert saml_svc._safe_error_codes(["invalid_response", "x\ny", "é", 7] * 3) == (
        "invalid_response,other,other,other,invalid_response"
    )
    assert saml_svc._safe_error_codes([]) == "none"
    print("  hostile ACS POST: the validation log carries fixed labels only (no sender text)")


def check_idp_initiated_refused():
    _configure(saml_allow_idp_initiated="0")
    c = _client()  # FRESH client: no prior AuthnRequest -> unsolicited
    resp = _signed_response(_KEY, _CERT, email="idp@saml.test", in_response_to=None)
    _assert_refused(_post_acs(c, resp), "IdP-initiated (default refuse)")
    print("  idp-initiated: an unsolicited response is refused unless explicitly opted in")


def check_idp_initiated_refused_after_login_started():
    """#482: python3-saml compares InResponseTo only when the response carries one, so a
    signed assertion WITHOUT InResponseTo used to pass whenever any login had been started
    (a stored request id). With IdP-initiated off it must be refused anyway."""
    _configure(saml_allow_idp_initiated="0")
    c = _client()
    _req_id_from_login(c)  # a login is started and never finished at the IdP
    resp = _signed_response(_KEY, _CERT, email="bypass@saml.test", in_response_to=None)
    _assert_refused(_post_acs(c, resp), "unsolicited response after a started login")
    with Session(engine) as s:
        assert s.exec(select(User).where(User.email == "bypass@saml.test")).first() is None, (
            "the bypass must not provision or sign in the subject"
        )
    # with the operator opt-in the same shape is accepted (explicit choice)
    _configure(saml_allow_idp_initiated="1")
    c2 = _client()
    _req_id_from_login(c2)
    resp2 = _signed_response(_KEY, _CERT, email="optin@saml.test", in_response_to=None)
    assert _post_acs(c2, resp2).status_code == 303
    print("  idp-initiated: an unsolicited response is refused even after a login was started")


def check_rewrapped_idp_assertion_refused():
    """#482 gate: wantMessagesSigned is False, so an assertion-only signature does NOT cover
    the outer Response element where InResponseTo lives. A captured IdP-initiated assertion
    (no InResponseTo inside it) rewrapped in a forged Response whose InResponseTo names the
    attacker's own started login must still be refused. Correlation must come from signed
    bytes: the assertion's SubjectConfirmationData, or a signed Response."""
    _configure(saml_allow_idp_initiated="0")
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response(
        _KEY, _CERT, email="rewrap@saml.test", in_response_to=None, resp_in_response_to=rid
    )
    _assert_unsolicited_refusal(_post_acs(c, resp), "rewrapped IdP-initiated assertion")
    with Session(engine) as s:
        assert s.exec(select(User).where(User.email == "rewrap@saml.test")).first() is None, (
            "the rewrapped assertion must not provision or sign in the subject"
        )
    # a SIGNED Response authenticates its own InResponseTo: accepted even when the assertion's
    # SubjectConfirmationData carries none (IdPs that sign the message, not the assertion IRT)
    c2 = _client()
    rid2 = _req_id_from_login(c2)
    resp2 = _signed_response(
        _KEY,
        _CERT,
        email="msgsigned@saml.test",
        in_response_to=None,
        resp_in_response_to=rid2,
        sign_response=True,
    )
    r2 = _post_acs(c2, resp2)
    assert r2.status_code == 303, ("signed Response answering our request", r2.status_code)
    # the signed assertion's own InResponseTo is enough when the Response carries none
    c3 = _client()
    rid3 = _req_id_from_login(c3)
    resp3 = _signed_response(
        _KEY, _CERT, email="scdonly@saml.test", in_response_to=rid3, resp_in_response_to=None
    )
    r3 = _post_acs(c3, resp3)
    assert r3.status_code == 303, ("signed assertion answering our request", r3.status_code)
    # a signed assertion answering ANOTHER request, rewrapped to name ours: refused
    c4 = _client()
    _req_id_from_login(c4)
    resp4 = _signed_response(
        _KEY, _CERT, email="otherreq@saml.test", in_response_to="_victim", resp_in_response_to=None
    )
    _assert_refused(_post_acs(c4, resp4), "assertion answering another request")
    c5 = _client()
    rid5 = _req_id_from_login(c5)
    resp5 = _signed_response(
        _KEY, _CERT, email="otherreq@saml.test", in_response_to="_victim", resp_in_response_to=rid5
    )
    _assert_refused(_post_acs(c5, resp5), "assertion answering another request, rewrapped")
    print("  idp-initiated: a rewrapped assertion cannot borrow an unsigned InResponseTo")


def check_replay():
    _configure(saml_allow_idp_initiated="1")  # isolate the replay cache from correlation
    c = _client()
    resp = _signed_response(_KEY, _CERT, email="replay@saml.test", in_response_to=None)
    r1 = _post_acs(c, resp)
    assert r1.status_code == 303, ("first idp-initiated login should succeed", r1.status_code)
    c2 = _client()  # fresh session; the SAME assertion id must still be refused
    _assert_refused(_post_acs(c2, resp), "replayed assertion")
    print("  replay: a re-POSTed assertion is refused by the replay cache")


def check_relaystate_open_redirect():
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response(_KEY, _CERT, email="relay@saml.test", in_response_to=rid)
    r = _post_acs(c, resp, relay_state="https://evil.example/phish")
    assert r.status_code == 303, ("login should still succeed", r.status_code)
    assert r.headers["location"] == "/", (
        "an off-site RelayState must be sanitized to '/'",
        r.headers.get("location"),
    )
    print("  relaystate: an off-site RelayState is sanitized to '/' (no open redirect)")


def check_scim_managed_disabled_blocks():
    _configure()
    # a SCIM-provisioned account that SCIM deactivated (active=false).
    security.create_user("scimoff@saml.test", "Scim Off", Role.TRANSLATOR, "", auth_source="scim")
    with Session(engine) as s:
        row = s.exec(select(User).where(User.email == "scimoff@saml.test")).first()
        row.active = False
        s.add(row)
        s.commit()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response(_KEY, _CERT, email="scimoff@saml.test", in_response_to=rid)
    _assert_refused(_post_acs(c, resp), "SCIM-deactivated account")
    print("  scim lifecycle: a SCIM-deactivated (active=false) account cannot log in via SAML")


def check_want_attribute_statement_setting():
    """PIN the NameID-only relaxation TOGETHER WITH the authenticity guarantee: the security
    dict must set wantAttributeStatement=False (so a NameID-only assertion is accepted) AND keep
    wantAssertionsSigned=True and wantNameId=True. Asserting all three in one place means a
    future edit cannot silently flip the SIGNING requirement while 'cleaning up' the
    AttributeStatement line — the security one and the data-completeness one move together."""
    import types

    _configure()
    # build_settings only reads request.base_url via _sp_entity_id/_acs_url, and _configure sets
    # both saml_sp_entity_id + saml_sp_acs_url explicitly, so a lightweight stub is enough here.
    req = types.SimpleNamespace(base_url=_BASE + "/", query_params={})
    sec = saml_svc.build_settings(req)["security"]
    assert sec["wantAttributeStatement"] is False, ("NameID-only must be accepted", sec)
    assert sec["wantAssertionsSigned"] is True, ("authenticity (signing) must stay enforced", sec)
    assert sec["wantNameId"] is True, ("a NameID must still be required", sec)
    print(
        "  attrstmt setting: wantAttributeStatement=False pinned WITH "
        "wantAssertionsSigned/wantNameId=True"
    )


def check_nameid_only_login():
    """A signed assertion with NO AttributeStatement (identity in the emailAddress NameID) logs
    in and derives the email FROM THE NAMEID — the real Okta/NameID-only path that the old
    wantAttributeStatement=True default rejected with 'There is no AttributeStatement on the
    Response'. The provisioned account's email == the NameID proves the NameID fallback."""
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response_no_attrs(_KEY, _CERT, email="nameonly@saml.test", in_response_to=rid)
    r = _post_acs(c, resp)
    assert r.status_code == 303, ("NameID-only login must succeed", r.status_code, r.text[:300])
    assert r.headers["location"] == "/", r.headers.get("location")
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "nameonly@saml.test")).first()
        assert u is not None and u.auth_source == "saml", (
            "NameID-only login must JIT-provision with the email taken from the NameID"
        )
    print(
        "  nameid-only: a signed assertion with NO AttributeStatement logs in (email from NameID)"
    )


def check_nameid_only_unsigned_refused():
    """Authenticity is STILL enforced on the NameID-only path: relaxing wantAttributeStatement
    did NOT relax signing. An UNSIGNED NameID-only assertion is refused — an attacker cannot
    inject identity by POSTing an unsigned NameID-only assertion (the exact gate probe)."""
    _configure()
    c = _client()
    rid = _req_id_from_login(c)
    resp = _signed_response_no_attrs(
        _KEY, _CERT, email="nameonly-unsigned@saml.test", in_response_to=rid, sign=False
    )
    _assert_refused(_post_acs(c, resp), "unsigned NameID-only assertion")
    print(
        "  nameid-only unsigned: an unsigned NameID-only assertion is still refused "
        "(signing enforced)"
    )


def check_login_sets_txn_cookie_https():
    """The /auth/saml/login 303 sets the signed one-shot correlation cookie on https, with the
    SameSite=None; Secure; HttpOnly; path=/auth/saml/ attributes the cross-site POST needs."""
    _configure()
    r = _client().get("/auth/saml/login", follow_redirects=False)
    assert r.status_code == 303, ("login should redirect to the IdP", r.status_code)
    setc = " ".join(r.headers.get_list("set-cookie")).lower()
    assert saml_svc.TXN_COOKIE_NAME in setc, ("https login must set the correlation cookie", setc)
    assert "samesite=none" in setc, ("correlation cookie must be SameSite=None", setc)
    assert "secure" in setc, ("correlation cookie must be Secure", setc)
    assert "httponly" in setc, ("correlation cookie must be HttpOnly", setc)
    assert "path=/auth/saml/" in setc, ("correlation cookie must be path-scoped", setc)
    print("  txn-cookie set: https login sets SameSite=None; Secure; HttpOnly; path-scoped cookie")


def check_cross_site_cookie_login():
    """THE LIVE-BUG REGRESSION. Simulate the real IdP's cross-site top-level POST: a FRESH
    client with NO session cookie (the SameSite=Lax session is withheld cross-site) but WITH a
    valid signed correlation cookie. Correlation must succeed (the request id reaches
    process_response) and the login must complete."""
    _configure()
    rid = "_xsite" + os.urandom(6).hex()
    resp = _signed_response(_KEY, _CERT, email="xsite@saml.test", in_response_to=rid)
    c = _client()  # never called /login -> no session cookie at all
    _set_txn_cookie(c, saml_svc.encode_txn(rid, "/"))
    r = _post_acs(c, resp)
    assert r.status_code == 303, ("cross-site login must succeed", r.status_code, r.text[:200])
    assert r.headers["location"] == "/", r.headers.get("location")
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "xsite@saml.test")).first()
        assert u is not None and u.auth_source == "saml", "cross-site login must JIT-provision"
    # one-shot: the ACS deletes the cookie on the way out.
    setc = " ".join(r.headers.get_list("set-cookie")).lower()
    assert saml_svc.TXN_COOKIE_NAME in setc and "max-age=0" in setc, (
        "ACS must delete the one-shot correlation cookie",
        setc,
    )
    print("  cross-site (LIVE BUG): a valid cookie w/ NO session logs in + is one-shot-deleted")


def check_txn_cookie_tampered_refused():
    """A correlation cookie whose signature does not verify is treated as ABSENT -> the same
    non-enumerable unsolicited refusal, never trusted."""
    _configure()
    rid = "_tamp" + os.urandom(6).hex()
    resp = _signed_response(_KEY, _CERT, email="tampcookie@saml.test", in_response_to=rid)
    good = saml_svc.encode_txn(rid, "/")
    flip = "A" if good[-4] != "A" else "B"  # flip a byte in the signature tail
    bad = good[:-4] + flip + good[-3:]
    c = _client()
    _set_txn_cookie(c, bad)
    _assert_unsolicited_refusal(_post_acs(c, resp), "tampered correlation cookie")
    print("  txn-cookie tampered: a bad-signature cookie is refused (same unsolicited message)")


def check_txn_cookie_wrong_key_refused():
    """A cookie signed with a DIFFERENT key (another install / forgery attempt) does not verify
    against this install's SESSION_SECRET -> treated as absent -> unsolicited refusal."""
    _configure()
    rid = "_wkey" + os.urandom(6).hex()
    resp = _signed_response(_KEY, _CERT, email="wkeycookie@saml.test", in_response_to=rid)
    forged = itsdangerous.URLSafeSerializer("a-different-installs-secret", salt=saml_svc._TXN_SALT)
    bad = forged.dumps({"rid": rid, "next": "/", "iat": int(time.time())})
    c = _client()
    _set_txn_cookie(c, bad)
    _assert_unsolicited_refusal(_post_acs(c, resp), "wrong-key (cross-install/forged) cookie")
    print("  txn-cookie wrong-key: a cookie signed with another key is refused (not reusable)")


def check_txn_cookie_expired_refused():
    """A validly-SIGNED but stale (iat beyond TTL) cookie is refused: the signature alone does
    not make an expired correlation usable."""
    _configure()
    rid = "_exp" + os.urandom(6).hex()
    resp = _signed_response(_KEY, _CERT, email="expcookie@saml.test", in_response_to=rid)
    stale = saml_svc._txn_serializer().dumps(
        {"rid": rid, "next": "/", "iat": int(time.time()) - (saml_svc._TXN_TTL_SECONDS + 60)}
    )
    c = _client()
    _set_txn_cookie(c, stale)
    _assert_unsolicited_refusal(_post_acs(c, resp), "expired correlation cookie")
    print("  txn-cookie expired: a signed-but-stale cookie (iat beyond TTL) is refused")


def check_txn_cookie_hostile_next_sanitized():
    """Even a validly-signed cookie whose `next` payload is hostile is sanitize_next()'d at
    consumption -> the post-login redirect is '/' (no open redirect via the signed payload).
    RelayState is omitted here so the cookie's `next` is the value actually under test."""
    for i, hostile in enumerate(("https://evil.example", "//evil", "\\evil")):
        _configure()
        rid = f"_hn{i}_{os.urandom(4).hex()}"
        email = f"hostnext{i}@saml.test"
        resp = _signed_response(_KEY, _CERT, email=email, in_response_to=rid)
        c = _client()
        _set_txn_cookie(c, saml_svc.encode_txn(rid, hostile))
        r = _post_acs(c, resp, relay_state=None)  # no RelayState -> cookie `next` is used
        assert r.status_code == 303, ("login should still succeed", hostile, r.status_code)
        assert r.headers["location"] == "/", (
            "a hostile cookie `next` must be sanitized to '/'",
            hostile,
            r.headers.get("location"),
        )
    print("  txn-cookie hostile next: a poisoned signed `next` is sanitized to '/' (no redirect)")


def check_http_login_sets_no_cookie():
    """On a plain-http install the correlation cookie is NOT set (SameSite=None requires Secure,
    which browsers reject over http); the session fallback keeps status-quo behavior there."""
    _configure()
    ch = TestClient(app, base_url="http://sp.example.com")
    r = ch.get("/auth/saml/login", follow_redirects=False)
    assert r.status_code == 303, ("http login should still redirect to the IdP", r.status_code)
    setc = " ".join(r.headers.get_list("set-cookie")).lower()
    assert saml_svc.TXN_COOKIE_NAME not in setc, ("http must NOT set the correlation cookie", setc)
    print("  http: no correlation cookie is set over plain http (SameSite=None needs Secure)")


def check_session_fallback_when_no_cookie():
    """The session path still works when the correlation cookie is absent (same-site clients /
    http). Drop the cookie after login so process_acs must fall back to the session."""
    _configure()
    c = _client()
    rid = _req_id_from_login(c)  # sets BOTH the session keys and the correlation cookie
    c.cookies.delete(saml_svc.TXN_COOKIE_NAME)  # force the session fallback
    resp = _signed_response(_KEY, _CERT, email="sessfb@saml.test", in_response_to=rid)
    r = _post_acs(c, resp)
    assert r.status_code == 303, ("session-fallback must log in", r.status_code, r.text[:200])
    assert r.headers["location"] == "/", r.headers.get("location")
    print("  session fallback: with no correlation cookie, the session-stored request id works")


def _lib_available() -> bool:
    try:
        import onelogin.saml2.auth  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401

        return True
    except Exception:
        return False


def main():
    global _KEY, _CERT
    check_flag_off()
    if not _lib_available():
        print(
            "  NOTE: python3-saml (opt-in extra) not installed -- interop tests skipped "
            "(inside the sprint cut line; live Okta/Entra testing is post-demo)."
        )
        print("SAML OK")
        return
    _KEY, _CERT, cert_b64 = _make_idp_keys()
    _META[0] = _idp_metadata(cert_b64)
    check_metadata_and_button()
    check_rooted_destination_handoff()
    check_valid_login()
    check_unsigned_refused()
    check_tampered_refused()
    check_wrong_audience()
    check_expired()
    check_inresponseto_mismatch()
    check_hostile_acs_log()
    check_idp_initiated_refused()
    check_idp_initiated_refused_after_login_started()
    check_rewrapped_idp_assertion_refused()
    check_replay()
    check_relaystate_open_redirect()
    check_scim_managed_disabled_blocks()
    # NameID-only assertions (no AttributeStatement) — the Okta live-bug fix + authenticity guard.
    check_want_attribute_statement_setting()
    check_nameid_only_login()
    check_nameid_only_unsigned_refused()
    # SP-initiated correlation cookie (the cross-site-POST live-bug fix + its guards).
    check_login_sets_txn_cookie_https()
    check_cross_site_cookie_login()
    check_txn_cookie_tampered_refused()
    check_txn_cookie_wrong_key_refused()
    check_txn_cookie_expired_refused()
    check_txn_cookie_hostile_next_sanitized()
    check_http_login_sets_no_cookie()
    check_session_fallback_when_no_cookie()
    print("SAML OK")


if __name__ == "__main__":
    main()
