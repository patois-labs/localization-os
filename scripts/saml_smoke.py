"""Build-time SAML smoke for the WITH_SAML=1 container image (see the Dockerfile).

Run by the Dockerfile as the app user when the image is built with --build-arg WITH_SAML=1.
Any failure exits non-zero, so the image build fails instead of the boot capability check
failing later on the hosted instance. It checks, in order:

  1. python3-saml, xmlsec and lxml import.
  2. lxml and xmlsec run the same libxml2 major.minor. This mirrors python-xmlsec's own
     compatibility rule (a patch-level difference is accepted, as upstream accepts it);
     the full versions are printed for the build log either way.
  3. The crypto actually works: a tiny SAML-shaped document is signed and verified with
     python3-saml's own helpers (OneLogin_Saml2_Utils.add_sign / validate_sign), using a
     throwaway RSA key and self-signed certificate generated here in memory and discarded.
     A tampered copy must then FAIL verification.

Standard output is ASCII only. Exit 0 = the SAML stack is usable.
"""

from __future__ import annotations

import datetime
import sys

_DOC = (
    '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" ID="_smoke" '
    'Version="2.0" IssueInstant="2026-01-01T00:00:00Z">'
    '<saml:Issuer xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">smoke-1</saml:Issuer>'
    '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
    "</samlp:Status></samlp:Response>"
)


def libxml2_versions():
    """(lxml's libxml2, xmlsec's libxml2) as full version tuples."""
    import lxml.etree
    import xmlsec

    return tuple(lxml.etree.LIBXML_VERSION), tuple(xmlsec.get_libxml_version())


def libxml2_compatible(lxml_version, xmlsec_version) -> bool:
    """python-xmlsec's rule: the same libxml2 major.minor."""
    return tuple(lxml_version)[:2] == tuple(xmlsec_version)[:2]


def throwaway_pair():
    """A fresh RSA key + self-signed certificate (PEM strings), never stored anywhere."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "saml-build-smoke")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(private, hashes.SHA256())
    )
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return private_pem, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def sign_and_verify() -> None:
    """Sign, verify, then prove a tampered copy is rejected. Raises on any failure."""
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    private_pem, cert_pem = throwaway_pair()
    signed = OneLogin_Saml2_Utils.add_sign(_DOC, private_pem, cert_pem)
    if isinstance(signed, str):
        signed = signed.encode("utf-8")
    if b"SignatureValue" not in signed:
        raise RuntimeError("add_sign produced no signature")
    if not OneLogin_Saml2_Utils.validate_sign(signed, cert_pem, raise_exceptions=True):
        raise RuntimeError("a freshly signed document did not verify")
    tampered = signed.replace(b"smoke-1", b"smoke-2")
    if tampered == signed:
        raise RuntimeError("tamper probe did not change the document")
    if OneLogin_Saml2_Utils.validate_sign(tampered, cert_pem, raise_exceptions=False):
        raise RuntimeError("a tampered document verified")


def main() -> int:
    try:
        import lxml
        import onelogin.saml2.auth  # noqa: F401
        import xmlsec
    except Exception as exc:
        print(f"SAML smoke FAILED: import: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    lxml_libxml2, xmlsec_libxml2 = libxml2_versions()
    print(
        f"SAML smoke: lxml {lxml.__version__} libxml2 {lxml_libxml2}; "
        f"xmlsec {xmlsec.__version__} libxml2 {xmlsec_libxml2}"
    )
    if not libxml2_compatible(lxml_libxml2, xmlsec_libxml2):
        print(
            "SAML smoke FAILED: lxml and xmlsec use different libxml2 major.minor versions",
            file=sys.stderr,
        )
        return 1
    try:
        sign_and_verify()
    except Exception as exc:
        print(f"SAML smoke FAILED: sign/verify: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("SAML smoke: sign + verify OK, tampered copy rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
