"""SPEC-full-remote-parity Wave R3: the configuration & credential /api routes.

THE MOST DANGEROUS WAVE: configuration mutation + credential writes on the remote
surface. This suite drives the real ASGI app through fastapi.testclient in a genuine
TWO-ORG world (multi_org_enabled ON + the login wall ON) with remote_admin_enabled
toggled BOTH ways, proving every law the wave binds itself to:

  * the THREE-LOCK gate on EVERY route (reads included): with the switch OFF every
    route answers 404 (non-enumerable); with it ON a docs/keys/no-admin-scope key is
    403, and an admin-SCOPE key on a NON-admin user is 403 (the role lock);
  * THE IMMUTABLE-KEYS LAW: EACH sacred gate (remote_admin_enabled,
    security_auth_required, multi_org_enabled, encrypt_db, encrypt_uploads,
    operator_key_hash) is refused over POST /api/admin/config, always, no confirm;
  * a _SENSITIVE key without confirm -> 400; with confirm -> written;
  * secrets are WRITE-ONLY: a config secret / engine api_key / provider key / vendor
    secret set is never in the response AND never in the audit cleartext; a masked
    read shows (set)/last4, never the value;
  * the PLAINTEXT-SECRET MIGRATION: an existing plaintext smtp_password row becomes
    Fernet ciphertext on the first R3 write, and read_secret_setting round-trips it;
  * CROSS-ORG 404 on the tenant-scoped routes (connector + vendor secret);
  * token-regen reveals the new token ONCE but never writes it to the audit log;
  * the VENDOR RETRO-GATE: setting a vendor secret over the docs-scoped update route
    is refused; the admin-gated /secret route is the only path.

The MCP manage_install boundary (it refuses secret-set + install-locked keys
client-side) is proven in tests/verify_mcp_tools.py::check_install_admin; here we
prove the /api SERVER side, which deliberately DOES accept a secret (write-only) so
the model-facing tool can safely refuse to author one.

ASCII-only; standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys
cleared -> MockProvider). Final line: "R3 CONFIG CREDENTIALS OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_r3_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Connection, Organization, Role, User, Vendor
from app.services import apikeys, audit, security, settings
from app.services.connectors import store as conn_store

#: The write-only field of POST /api/admin/oidc and a throwaway value for it (named so the
#: request body carries no literal credential-shaped pair).
OIDC_CRED_FIELD = "client_secret"
ROTATED_OIDC_VALUE = "rotated-secret-2"


# --------------------------------------------------------------------------- #
def _gate_off() -> None:
    settings.set_many(
        {"security_auth_required": "0", "multi_org_enabled": "0", "remote_admin_enabled": "0"}
    )
    settings.invalidate_cache()


def _remote_admin(on: bool) -> None:
    settings.set_many({"remote_admin_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _key(user_id: int, scopes="api docs admin") -> str:
    _row, raw = apikeys.create_key(user_id, "r3-test", scopes=scopes)
    return raw


def _user(email, role, org_id) -> int:
    u = security.create_user(
        email, email, role, "r3-password-1234", org_id=org_id
    )  # #181: created IN its org
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.org_id = org_id
        s.add(row)
        s.commit()
    return u.id


def _client():
    from app.main import app

    return TestClient(app)


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:300]})"


def _seed():
    init_db()
    with Session(engine) as s:
        org_a, org_b = Organization(name="Org A"), Organization(name="Org B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()

    admin_a = _user("admina@x.com", Role.ADMIN, oa)
    translator_a = _user("tra@x.com", Role.TRANSLATOR, oa)
    # A foreign connection + vendor (org B) for the cross-org 404 probes.
    with Session(engine) as s:
        conn_b = Connection(kind="airtable", name="B conn", org_id=ob, created_by="b@x.com")
        conn_a = Connection(kind="airtable", name="A conn", org_id=oa, created_by="admina@x.com")
        vendor_b = Vendor(name="B vendor", org_id=ob)
        vendor_a = Vendor(name="A vendor", org_id=oa)
        for row in (conn_b, conn_a, vendor_b, vendor_a):
            s.add(row)
        s.commit()
        ids = (conn_b.id, conn_a.id, vendor_b.id, vendor_a.id)
    keys = {
        "admin_scope": _key(admin_a, "api docs admin"),  # scope + role: full pass
        "docs": _key(admin_a, "api docs"),  # admin role, NO admin scope
        "keys": _key(admin_a, "api keys"),
        "admin_scope_translator": _key(translator_a, "api docs admin"),  # scope, NOT admin role
    }
    return {
        "keys": keys,
        "oa": oa,
        "ob": ob,
        "conn_b": ids[0],
        "conn_a": ids[1],
        "vendor_b": ids[2],
        "vendor_a": ids[3],
    }


# The full R3 route matrix (method, path, body). Bogus ids are fine: the switch-off
# 404 and the scope/role 403 both fire BEFORE any object lookup.
def _routes(d):
    return [
        ("get", "/api/admin/config", None),
        ("post", "/api/admin/config", {"settings": {"org_name": "x"}}),
        ("post", "/api/admin/engines", {"name": "x", "kind": "deepl"}),
        ("post", "/api/admin/engines/1/update", {"model": "x"}),
        ("post", "/api/admin/engines/1/delete", {}),
        ("post", "/api/admin/engines/1/test", {}),
        ("post", "/api/admin/engines/1/models", {}),
        ("post", "/api/admin/providers/claude/key", {"key": "x"}),
        ("post", "/api/admin/providers/claude/key/clear", {}),
        ("get", "/api/admin/providers/claude/key", None),
        ("post", "/api/connectors/%d/secret" % d["conn_a"], {"secret": {"api_key": "x"}}),  # noqa: UP031
        ("post", "/api/connectors/%d/oauth-secret" % d["conn_a"], {"oauth": {"access_token": "x"}}),  # noqa: UP031
        ("post", "/api/connectors/token-regen", {}),
        ("post", "/api/vendors/%d/secret" % d["vendor_a"], {"signing_secret": "x"}),  # noqa: UP031
        ("post", "/api/admin/ldap", {"ldap_host": "x"}),
        ("get", "/api/admin/ldap", None),
        ("post", "/api/admin/oidc", {"sso_issuer": "x"}),
        ("get", "/api/admin/oidc", None),
        ("post", "/api/admin/smtp", {"smtp_host": "x"}),
        ("get", "/api/admin/smtp", None),
    ]


def _send(api, method, path, hdr, body):
    fn = api.get if method == "get" else api.post
    return fn(path, headers=hdr, json=body) if body is not None else fn(path, headers=hdr)


# --------------------------------------------------------------------------- #
# 1. the three-lock gate on EVERY route
# --------------------------------------------------------------------------- #
def test_gate_matrix(d, api):
    routes = _routes(d)
    admin = _h(d["keys"]["admin_scope"])
    # (a) SWITCH OFF: every route -- READS INCLUDED -- is 404 even for the full key.
    _remote_admin(False)
    for method, path, body in routes:
        _is(
            _send(api, method, path, admin, body),
            404,
            f"switch-off {method.upper()} {path}",
        )
    # (b) SWITCH ON, but a key WITHOUT the admin scope (docs / keys): 403 (scope lock).
    _remote_admin(True)
    for label in ("docs", "keys"):
        hdr = _h(d["keys"][label])
        for method, path, body in routes:
            r = _send(api, method, path, hdr, body)
            _is(r, 403, f"{label}-scope {method.upper()} {path}")
            assert "admin-scoped" in r.json().get("error", ""), (path, r.text)
    # (c) SWITCH ON + admin SCOPE but a NON-admin user: 403 (the is_admin ROLE lock).
    hdr = _h(d["keys"]["admin_scope_translator"])
    for method, path, body in routes:
        _is(
            _send(api, method, path, hdr, body),
            403,
            f"admin-scope-non-admin-role {method.upper()} {path}",
        )
    print(
        "  [1] three-lock gate: all %d routes are 404 with the switch off, 403 for "  # noqa: UP031
        "a docs/keys key (scope lock) and 403 for an admin-scope key on a non-admin "
        "user (role lock)" % len(routes)
    )


# --------------------------------------------------------------------------- #
# 2. THE IMMUTABLE-KEYS LAW -- each sacred gate refused, always
# --------------------------------------------------------------------------- #
def test_immutable_keys(d, api):
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    for key in sorted(settings.REMOTE_IMMUTABLE_KEYS):
        before = settings.get(key)
        # try with AND without a blanket confirm -- neither may ever let it through.
        for confirm in (None, True, [key]):
            body = {"settings": {key: "1"}}
            if confirm is not None:
                body["confirm"] = confirm
            r = api.post("/api/admin/config", headers=admin, json=body)
            _is(r, 400, f"immutable {key} (confirm={confirm!r})")
            assert "local-only" in r.json().get("error", "").lower(), r.text
            assert key in r.json().get("error", ""), r.text
        assert settings.get(key) == before, f"an immutable key was mutated: {key}"
    # a mixed body (one immutable + one legal key) is refused WHOLESALE -- nothing
    # partially written.
    r = api.post(
        "/api/admin/config",
        headers=admin,
        json={"settings": {"org_name": "SHOULD-NOT-APPLY", "encrypt_db": "1"}},
    )
    _is(r, 400, "mixed immutable body")
    assert settings.get("org_name") != "SHOULD-NOT-APPLY", "a refused mixed body wrote"
    print(
        "  [2] immutable-keys law: each of the %d sacred gates is refused over /api "  # noqa: UP031
        "(named local-only) with and without confirm, and a mixed body is refused "
        "wholesale" % len(settings.REMOTE_IMMUTABLE_KEYS)
    )


# --------------------------------------------------------------------------- #
# 3. sensitive-key confirm-per-key
# --------------------------------------------------------------------------- #
def test_sensitive_confirm(d, api):
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    # project_scoping is a _SENSITIVE key that is NOT immutable -> confirm gates it.
    # (pii_redaction + audit_enabled were promoted to REMOTE_IMMUTABLE in the R3 review,
    # so they are refused OUTRIGHT now -- see test_immutable_keys.)
    assert "project_scoping" in settings.SENSITIVE_KEYS
    assert "project_scoping" not in settings.REMOTE_IMMUTABLE_KEYS
    # #281: this suite's world has multi-org ON, where an install-wide security key is
    # host-only: even a confirmed write from an org admin's key is refused (403).
    r = api.post(
        "/api/admin/config",
        headers=admin,
        json={"settings": {"project_scoping": "0"}, "confirm": True},
    )
    _is(r, 403, "multi-org install-wide security key")
    assert settings.get("project_scoping") == "1", "an org admin changed an install-wide key"
    # The confirm law itself, on a single-org install.
    settings.set_many({"multi_org_enabled": "0"})
    r = api.post("/api/admin/config", headers=admin, json={"settings": {"project_scoping": "0"}})
    _is(r, 400, "sensitive no confirm")
    assert "project_scoping" in r.json().get("error", ""), r.text
    assert settings.get("project_scoping") == "1", "a sensitive key changed without confirm"
    # confirm-per-key: confirming a DIFFERENT key does not clear this one.
    r = api.post(
        "/api/admin/config",
        headers=admin,
        json={"settings": {"project_scoping": "0"}, "confirm": {"sso_enabled": True}},
    )
    _is(r, 400, "sensitive confirm-for-wrong-key")
    # the right confirm lands it.
    r = api.post(
        "/api/admin/config",
        headers=admin,
        json={"settings": {"project_scoping": "0"}, "confirm": {"project_scoping": True}},
    )
    _is(r, 200, "sensitive with confirm")
    assert settings.get("project_scoping") == "0"
    settings.set_many({"project_scoping": "1", "multi_org_enabled": "1"})
    print(
        "  [3] sensitive-key confirm: a _SENSITIVE key is 400 without its OWN confirm "
        "(confirming a different key does not count) and lands with it"
    )


def test_operator_key_hash_read_masked(d, api):
    # R3 review HIGH: operator_key_hash is credential material (a crackable digest) but
    # matches none of the is_secret_key substring markers. It is WRITE-protected
    # (REMOTE_IMMUTABLE) and must be READ-protected too: GET /api/admin/config must
    # NEVER return its literal value.
    assert settings.is_secret_key("operator_key_hash"), "operator_key_hash must mask"
    HASH = "deadbeef" * 8  # a plausible sha256-hex value, seeded directly
    settings.set_many({"operator_key_hash": HASH})
    settings.invalidate_cache()
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    r = api.get("/api/admin/config", headers=admin)
    _is(r, 200, "config read")
    assert HASH not in r.text, "operator_key_hash LEAKED its value in the masked read"
    assert r.json()["settings"]["operator_key_hash"] == "(set)", r.text
    settings.set_many({"operator_key_hash": ""})
    print(
        "  [3b] operator_key_hash: GET /api/admin/config masks it to (set), never the "
        "hash value (write-protected AND read-protected)"
    )


def test_endpoint_url_read_redacted(d, api):
    # Train Q2 gate: an endpoint-URL setting (otel_endpoint, blobstore_s3_endpoint_url)
    # may embed a credential in its userinfo or query. Its own module never logs it, but
    # is_secret_key is False for it, so the masked config read used to print it VERBATIM.
    # GET /api/admin/config must PARTIALLY redact it: scheme+host+path kept, embedded
    # userinfo/query stripped -- the secret must NEVER appear.
    SECRET = "SUPERSECRET123"
    seeds = {
        "otel_endpoint": "https://user:%s@collector.internal:4318/v1/traces" % SECRET,  # noqa: UP031
        "blobstore_s3_endpoint_url": "https://minio.internal:9000/bucket?token=%s" % SECRET,  # noqa: UP031
    }
    for k in seeds:
        assert not settings.is_secret_key(k), f"{k} unexpectedly a secret key"
        assert settings.is_redacted_url_key(k), f"{k} must be a redacted-url key"
    settings.set_many(seeds)
    settings.invalidate_cache()
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    r = api.get("/api/admin/config", headers=admin)
    _is(r, 200, "config read")
    assert SECRET not in r.text, "an endpoint URL LEAKED its embedded credential"
    got = r.json()["settings"]
    # scheme+host survive (operational value preserved); userinfo/query do not.
    assert got["otel_endpoint"] == "https://collector.internal:4318/v1/traces", got["otel_endpoint"]
    assert got["blobstore_s3_endpoint_url"] == "https://minio.internal:9000/bucket?(redacted)", got[
        "blobstore_s3_endpoint_url"
    ]
    for k in seeds:
        settings.set_many({k: ""})
    settings.invalidate_cache()
    print(
        "  [3d] endpoint URLs: GET /api/admin/config keeps scheme+host+path but strips "
        "embedded userinfo/query credentials (never the raw value)"
    )


def test_nonscalar_value_rejected(d, api):
    # R3 review LOW: a Setting value is a scalar. A list/dict/null value is refused with
    # a clean 400 BEFORE any write, not str()-coerced into stored garbage.
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    for bad in ([1, 2, 3], {"nested": 1}, None):
        r = api.post("/api/admin/config", headers=admin, json={"settings": {"smtp_port": bad}})
        _is(r, 400, f"non-scalar {bad!r}")
        assert "scalar" in r.json().get("error", "").lower(), r.text
    assert settings.get("smtp_port") == "587", "a rejected non-scalar value still wrote"
    print(
        "  [3c] non-scalar values (list/dict/null) are refused with a clean 400, "
        "nothing coerced or written"
    )


def test_smtp_port_value_validator(d, api):
    # #454: smtp_port had NO entry in the shared value-validator registry, so a garbage port
    # was stored exactly as typed and only failed later, inside mailer.send (documented never
    # to raise): an invite request 500'd after its commit and never reached its audit row.
    # The registry now refuses it at EVERY save door; three of them are proven here.
    assert "smtp_port" in settings.VALUE_VALIDATORS, "#454: smtp_port must have a validator"
    # ACCEPT: the default, a plain port, both bounds, blank (the Security form stores blank
    # as the 587 default) and a non-str scalar, all of which must pass untouched.
    for good in ("587", "25", "1", "65535", "", "   ", 587):
        settings.validate_value("smtp_port", good)
    # REFUSE: text, the scheme-shaped typo from the issue, out-of-range, a float/exponent
    # spelling, separators and non-ASCII digits (isdigit() is True for those; a port is
    # ASCII).
    for bad in (
        "abc",
        "smtp:587",
        "70000",
        "65536",
        "0",
        "-1",
        "+587",
        "587.0",
        "1e3",
        "5 8 7",
        "\u0665\u0668\u0667",
    ):
        try:
            settings.validate_value("smtp_port", bad)
        except settings.InvalidSettingValue:
            continue
        raise AssertionError(f"#454: smtp_port={bad!r} was accepted")
    # DOOR 1, the write seam. The web Security page posts smtp_port straight into set_many
    # (it pre-validates the session/password/network keys only), so the law lives at this
    # chokepoint: it must refuse the whole batch atomically, nothing written.
    before = settings.get("smtp_port")
    try:
        settings.set_many({"smtp_port": "abc"})
        raise AssertionError("#454: set_many stored a non-numeric smtp_port")
    except settings.InvalidSettingValue:
        pass
    settings.invalidate_cache()
    assert settings.get("smtp_port") == before, "#454: a refused port still wrote"
    # DOOR 2, the wire door. POST /api/admin/config consults the same registry and answers a
    # clean 400 carrying the door's own text, storing nothing. The multi-org install-wide
    # gate is off for the request (the same shape as [4]'s smtp-secret case) so the write
    # reaches validation instead of the 403 role lock that guards smtp_port for an org admin.
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    settings.set_many({"multi_org_enabled": "0"})
    try:
        r = api.post("/api/admin/config", headers=admin, json={"settings": {"smtp_port": "abc"}})
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    _is(r, 400, "#454: /api config with a non-numeric smtp_port")
    assert "smtp_port" in r.json().get("error", ""), r.text
    settings.invalidate_cache()
    assert settings.get("smtp_port") == before, "#454: a refused /api port still wrote"
    # And the accepted value still saves through the seam (no over-broad refusal).
    settings.set_many({"smtp_port": "587"})
    assert settings.get("smtp_port") == "587", "#454: a valid port must still save"
    print(
        "  [3e] #454 smtp_port validator: 'abc'/'smtp:587'/'70000' refused at the registry, "
        "the set_many seam and /api (nothing written); '587' accepted"
    )


# --------------------------------------------------------------------------- #
# 4. secrets are write-only (config + provider + engine), masked reads
# --------------------------------------------------------------------------- #
def test_secret_write_only(d, api):
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    SECRET = "topsecret-config-value-4242"
    # #426 (merged 2026-10-05): the mail transport serves every org, so under multi-org
    # (this world) smtp_password is a host-only key: the org admin's write is a 403 that
    # stores nothing and never echoes the value. The write-only proof below then runs on a
    # single-org install, the same shape as [5]'s OIDC migration.
    before = settings.read_secret_setting("smtp_password")
    r = api.post("/api/admin/config", headers=admin, json={"settings": {"smtp_password": SECRET}})
    _is(r, 403, "config secret set under multi-org")
    assert "smtp_password" in r.json()["error"], r.text
    assert SECRET not in r.text, "a refused config secret leaked into the response"
    assert settings.read_secret_setting("smtp_password") == before, "a refused secret was stored"
    # config secret key -> Fernet at rest, masked read, never in the body.
    settings.set_many({"multi_org_enabled": "0"})
    try:
        r = api.post(
            "/api/admin/config", headers=admin, json={"settings": {"smtp_password": SECRET}}
        )
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    _is(r, 200, "config secret set")
    assert SECRET not in r.text, "a config secret leaked into the response"
    assert r.json()["settings"]["smtp_password"] == "(set)", r.text
    assert settings.read_secret_setting("smtp_password") == SECRET
    assert settings.get("smtp_password") != SECRET, "the secret is stored in cleartext"
    # masked read.
    got = api.get("/api/admin/config", headers=admin)
    assert got.json()["settings"]["smtp_password"] == "(set)", got.text
    assert SECRET not in got.text, "a config secret leaked into the masked read"
    # provider key -> last4 only, never the key.
    PKEY = "sk-ant-provider-key-abc9999"
    r = api.post("/api/admin/providers/claude/key", headers=admin, json={"key": PKEY})
    _is(r, 200, "provider key set")
    assert PKEY not in r.text and r.json()["last4"] == "9999", r.text
    show = api.get("/api/admin/providers/claude/key", headers=admin)
    assert PKEY not in show.text and show.json()["last4"] == "9999", show.text
    assert show.json()["source"] == "app", show.text
    # engine api_key -> key_set flag only, never the key.
    EKEY = "engine-api-key-zzz-7777"
    r = api.post(
        "/api/admin/engines",
        headers=admin,
        json={"name": "R3 Engine", "kind": "deepl", "api_key": EKEY},
    )
    _is(r, 200, "engine create with key")
    assert EKEY not in r.text and r.json()["engine"]["key_set"] is True, r.text
    print(
        "  [4] write-only secrets: config secret / provider key / engine api_key are "
        "stored (Fernet/last4) and NEVER echoed; masked reads show (set)/last4"
    )


# --------------------------------------------------------------------------- #
# 5. the plaintext-secret migration
# --------------------------------------------------------------------------- #
def test_plaintext_migration(d, api):
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    # Simulate a PRE-R3 install: a plaintext sso_client_secret Setting row.
    settings.set_many({"sso_client_secret": "legacy-plaintext-secret-1"})
    settings.invalidate_cache()
    # read_secret_setting is legacy-safe: it returns the plaintext until it is migrated.
    assert settings.read_secret_setting("sso_client_secret") == "legacy-plaintext-secret-1"
    assert settings.get("sso_client_secret") == "legacy-plaintext-secret-1"  # still plaintext
    # An R3 write MIGRATES it to Fernet ciphertext. The OIDC keys are security-sensitive
    # (#280: confirm) and install-wide (#281: host-only under multi-org), so this runs on a
    # single-org install with confirm.
    settings.set_many({"multi_org_enabled": "0"})
    try:
        r = api.post(
            "/api/admin/oidc",
            headers=admin,
            json={OIDC_CRED_FIELD: ROTATED_OIDC_VALUE, "confirm": ["sso_client_secret"]},
        )
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    _is(r, 200, "oidc secret migrate")
    assert "rotated-secret-2" not in r.text, "the rotated OIDC secret leaked"
    raw = settings.get("sso_client_secret")
    assert raw != "rotated-secret-2" and raw.startswith("gAAAA"), (
        f"the migrated secret is not Fernet ciphertext: {raw[:12]!r}"
    )
    assert settings.read_secret_setting("sso_client_secret") == "rotated-secret-2"
    print(
        "  [5] plaintext migration: a legacy plaintext sso_client_secret reads "
        "correctly, and the first R3 write encrypts it to Fernet ciphertext in place"
    )


# --------------------------------------------------------------------------- #
# 6. connector + vendor: cross-org 404, write-only, retro-gate
# --------------------------------------------------------------------------- #
def test_connector_vendor(d, api):
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    # CROSS-ORG 404: org A's admin key cannot touch org B's connection/vendor secrets.
    _is(
        api.post(
            "/api/connectors/%d/secret" % d["conn_b"],  # noqa: UP031
            headers=admin,
            json={"secret": {"api_key": "x"}},
        ),
        404,
        "cross-org connector secret",
    )
    _is(
        api.post(
            "/api/connectors/%d/oauth-secret" % d["conn_b"],  # noqa: UP031
            headers=admin,
            json={"oauth": {"access_token": "x"}},
        ),
        404,
        "cross-org oauth secret",
    )
    _is(
        api.post(
            "/api/vendors/%d/secret" % d["vendor_b"],  # noqa: UP031
            headers=admin,
            json={"signing_secret": "x"},  # noqa: RUF100, UP031
        ),
        404,
        "cross-org vendor secret",
    )
    # OAUTH-SECRET write-only (no network path): the tokens are stored, never echoed.
    TOK = "oauth-access-token-secret-5555"
    r = api.post(
        "/api/connectors/%d/oauth-secret" % d["conn_a"],  # noqa: UP031
        headers=admin,
        json={"oauth": {"access_token": TOK, "refresh_token": "r-6666"}},
    )
    _is(r, 200, "oauth-secret set")
    assert TOK not in r.text and "r-6666" not in r.text, "an oauth token leaked"
    with Session(engine) as s:
        assert conn_store.is_oauth(s.get(Connection, d["conn_a"])), "oauth secret not stored"
    # CONNECTOR SECRET rotate on an OAuth connection short-circuits (no network) and
    # never echoes the submitted secret.
    r = api.post(
        "/api/connectors/%d/secret" % d["conn_a"],  # noqa: UP031
        headers=admin,
        json={"secret": {"api_key": "should-not-echo-7777"}},
    )
    _is(r, 400, "rotate on oauth connection")
    assert "should-not-echo-7777" not in r.text, "the submitted secret leaked"
    # VENDOR secret via the R3 route: stored, never echoed.
    VSEC = "vendor-signing-secret-8888"
    r = api.post(
        "/api/vendors/%d/secret" % d["vendor_a"],  # noqa: UP031
        headers=admin,
        json={"signing_secret": VSEC},  # noqa: RUF100, UP031
    )
    _is(r, 200, "vendor secret set")
    assert VSEC not in r.text and r.json()["vendor"]["has_secret"] is True, r.text
    # THE RETRO-GATE: the docs-scoped vendor UPDATE route refuses the secret fields.
    docs = _h(d["keys"]["docs"])  # admin role, docs scope
    r = api.post(
        "/api/vendors/%d" % d["vendor_a"],  # noqa: UP031
        headers=docs,
        json={"signing_secret": "retro-9999"},  # noqa: RUF100, UP031
    )
    _is(r, 400, "vendor retro-gate")
    assert "/secret" in r.json().get("error", "") and "retro-9999" not in r.text, r.text
    print(
        "  [6] connector+vendor: cross-org secret writes are 404; oauth-secret + "
        "vendor secret store write-only (never echoed); a rotate on an OAuth conn "
        "does not echo the submitted secret; the docs-scoped vendor update refuses "
        "the secret fields (retro-gate)"
    )


# --------------------------------------------------------------------------- #
# 7. token-regen reveals once, never in the audit
# --------------------------------------------------------------------------- #
def test_token_regen(d, api):
    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    # fix-audit-settings gate round 1 L3: this world has multi-org ON, where the ONE shared
    # companion-app token is install-wide: an org admin regenerating it would hold the token
    # of whatever org it is bound to, so it is refused (403) and nothing is revealed.
    before = settings.read_secret_setting("connector_api_token")
    r = api.post("/api/connectors/token-regen", headers=admin, json={})
    _is(r, 403, "token regen under multi-org")
    assert "lsk_" not in r.text and settings.read_secret_setting("connector_api_token") == before, (
        r.text
    )
    settings.set_many({"multi_org_enabled": "0"})
    try:
        r = api.post("/api/connectors/token-regen", headers=admin, json={})
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    _is(r, 200, "token regen")
    token = r.json()["connector_api_token"]
    assert token.startswith("lsk_"), r.text
    assert settings.read_secret_setting("connector_api_token") == token, "the token was not stored"
    rows = "\n".join(f"{rec}" for rec in audit.iter_export())
    assert token not in rows, "the revealed token leaked into the audit log"
    print(
        "  [7] token-regen: the new lsk_ token is revealed ONCE in the response, "
        "stored, and NEVER written to the audit log"
    )


# --------------------------------------------------------------------------- #
# 8. no secret cleartext anywhere in the audit log (the full sweep)
# --------------------------------------------------------------------------- #
def test_no_secret_in_audit(d, api):
    rows = "\n".join(f"{rec}" for rec in audit.iter_export())
    for leak in (
        "topsecret-config-value-4242",
        "sk-ant-provider-key-abc9999",
        "engine-api-key-zzz-7777",
        "legacy-plaintext-secret-1",
        "rotated-secret-2",
        "oauth-access-token-secret-5555",
        "r-6666",
        "should-not-echo-7777",
        "vendor-signing-secret-8888",
    ):
        assert leak not in rows, f"SECRET IN AUDIT CLEARTEXT: {leak}"
    print(
        "  [8] audit sweep: not one config/provider/engine/oauth/vendor secret set "
        "in this run appears in the audit log in cleartext"
    )


# --------------------------------------------------------------------------- #
# 9. #144: the /api engine write door runs the egress door too
# --------------------------------------------------------------------------- #
def test_engine_egress_door_on_the_api_write_surface(d, api):
    """#144: `/api/admin/engines` (and its update twin, which is also what the MCP
    create_engine/update_engine tools call) must refuse an egress-denied base_url
    with a clean 400 carrying the door's own text, not store it for a later call-time
    refusal. EngineTargetBlocked subclasses ValueError, so the route's existing
    handler does this with no new plumbing."""
    from app.services import engines

    _remote_admin(True)
    admin = _h(d["keys"]["admin_scope"])
    denied = "http://169.254.169.254/"

    r = api.post(
        "/api/admin/engines",
        headers=admin,
        json={"name": "ApiBad", "kind": "local", "base_url": denied, "model": "m"},
    )
    _is(r, 400, "#144: /api engine create with a denied base_url")
    body = r.json()
    assert body.get("ok") is False and "169.254.0.0/16" in body.get("error", ""), (
        f"#144: the /api refusal must carry the door's own text, got {r.text[:300]}"
    )
    assert engines.get_by_name("ApiBad") is None, "#144: the /api create must write no row"

    ok = api.post(
        "/api/admin/engines",
        headers=admin,
        json={"name": "ApiOk", "kind": "local", "base_url": "http://127.0.0.1:1234", "model": "m"},
    )
    _is(ok, 200, "#144: a legitimate local target still creates over /api")
    eid = ok.json()["engine"]["id"]
    r2 = api.post(f"/api/admin/engines/{eid}/update", headers=admin, json={"base_url": denied})
    _is(r2, 400, "#144: /api engine update with a denied base_url")
    assert engines.get_profile(eid).base_url == "http://127.0.0.1:1234", (
        "#144: a refused /api update must leave the row untouched"
    )
    api.post(f"/api/admin/engines/{eid}/delete", headers=admin, json={})
    print("  [9] #144: /api engine create/update refuse an egress-denied base_url (clean 400)")


# --------------------------------------------------------------------------- #
def main() -> None:
    d = _seed()
    try:
        api = _client()
        test_gate_matrix(d, api)
        test_immutable_keys(d, api)
        test_sensitive_confirm(d, api)
        test_operator_key_hash_read_masked(d, api)
        test_endpoint_url_read_redacted(d, api)
        test_nonscalar_value_rejected(d, api)
        test_smtp_port_value_validator(d, api)
        test_secret_write_only(d, api)
        test_plaintext_migration(d, api)
        test_connector_vendor(d, api)
        test_token_regen(d, api)
        test_no_secret_in_audit(d, api)
        test_engine_egress_door_on_the_api_write_surface(d, api)
    finally:
        _gate_off()
    print("R3 CONFIG CREDENTIALS OK")


if __name__ == "__main__":
    main()
