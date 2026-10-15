"""Offline regressions for #165/#166; run through test_verify_suites.py only.

GATE R6 LOW-2: the canonical selector for this area lives HERE, because four review rounds
each picked one by hand and each was short in a different place. Copy it, do not invent one.

    pytest tests/test_verify_suites.py -k "sec_connector_boundary or connector or strapi or
    salesforce or zendesk or freshdesk or shopify or airtable or contentful or document360
    or helpscout or hubspot or intercom or notion or akeneo or confluence or jira or
    wordpress or zoho or rbac or roles or audit"

That is every registered connector kind by name plus the shared connector suites plus the
role and audit suites the token gate touches. Name a new connector kind here on the day you
add it. And check what pytest COLLECTED before trusting the run: a -k filter is not a gate,
and "-k connector" alone silently misses every vendor suite.
"""

import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import suppress
from unittest.mock import patch
from urllib.parse import urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sec_boundary_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role
from app.services import security, settings
from app.services.connectors import base, connector_class, store
from app.services.connectors.base import ConnectorError
from tests._csrf import csrf_for
from tests._piece_patch import patch_pieces

HOST_KEYS = {
    "strapi": "base_url",
    "akeneo": "base_url",
    "drupal": "base_url",
    "github": "base_url",
    "gitlab": "base_url",
    "wordpress": "base_url",
    "zoho": "api_domain",
    "jira": "base_url",
    "confluence": "base_url",
}
# Offline fixtures. None of the values below is a real credential; they exist only to
# prove the store drops a stored secret when the destination host changes. They are
# hoisted into constants so the pregate secret-scan exemption lives in one place
# instead of on every call site (an inline pragma does not fit in 100 columns).
SEC_OLD = {"api_token": "old"}  # pregate: allow-secret offline fixture
SEC_NEW = {"api_token": "new"}  # pregate: allow-secret offline fixture
SEC_SPACE = {"api_token": " "}  # pregate: allow-secret offline fixture
SEC_BLANK_PAIR = {"api_token": "", "email": "  "}  # pregate: allow-secret offline fixture
SEC_ZOHO_OLD = {"access_token": "old"}  # pregate: allow-secret offline fixture
SEC_OAUTH = {
    "auth_mode": "oauth",
    "oauth": {"access_token": "keep"},  # pregate: allow-secret offline fixture
}
SEC_LIVE = {
    "email": "admin@corp.example",
    "api_token": "LIVE-TOKEN",  # pregate: allow-secret offline fixture
}
SEC_PARTIAL = {
    "email": "attacker@evil.example",
    "api_token": "",  # pregate: allow-secret offline fixture
}
PASSWORD = "boundary-password-123"  # pregate: allow-secret offline fixture
# The literal the store writes into its audit detail. Hoisted so the scan exemption sits
# in one place instead of on every assertion that looks for it.
CLEARED_MARK = "secret_cleared=True"  # pregate: allow-secret audit field name
TOKEN = "lsk_boundary_fixture_only"  # pregate: allow-secret offline fixture
SEC_FRESH = {"api_key": "fixture"}  # pregate: allow-secret offline fixture
SEC_SHOP = {"access_token": "fixture"}  # pregate: allow-secret offline fixture
TENANTS = {
    "freshdesk": ("domain", "freshdesk.com", SEC_FRESH),
    "zendesk": ("subdomain", "zendesk.com", SEC_LIVE),
    "shopify": ("store_name", "myshopify.com", SEC_SHOP),
}


def test_tenant_request_destinations():
    failures = []
    base.set_test_resolver(lambda host: ["93.184.216.34"])
    for kind, (key, suffix, credential) in TENANTS.items():
        valid = {
            "Acme-1": f"acme-1.{suffix}",
            "a": f"a.{suffix}",
            "a" * 63: f"{'a' * 63}.{suffix}",
            f"ACME.{suffix}": f"acme.{suffix}",
        }
        for handle in (
            *valid,
            "attacker\u3002example#",
            "attacker#",
            "attacker?",
            "attacker@",
            "attacker\uff0eexample#",
            "attacker\uff61example#",
            " acme",
            "acme ",
            "acme\n",
            "acme\t",
            "-acme",
            "acme-",
            "a" * 64,
            "acme/path",
            "acme\x00",
            "\u212aacme",
            "acme.evil.example",
            f"acme.{suffix}#",
            123,
            None,
        ):
            reached = []

            def respond(request, reached=reached):
                assert request.headers.get("Authorization") or request.headers.get(
                    "X-Shopify-Access-Token"
                )
                reached.append(request.url.host)
                return httpx.Response(200, json={"data": {"shop": {"name": "fixture"}}})

            obj = connector_class(kind)(
                {key: handle}, credential, transport=httpx.MockTransport(respond)
            )
            obj.test()
            expected = [valid[handle]] if handle in valid else []
            # Shopify test() makes two calls to the same tenant host (locales, then granted
            # scopes), so it is compared by hosts reached; every other kind keeps the exact
            # one-request comparison.
            got = sorted(set(reached)) if kind == "shopify" else reached
            if got != expected:
                failures.append(f"{kind} {handle!r}: reached {reached}, expected {expected}")
    assert not failures, "; ".join(failures)


def test_tenant_host_pin():
    for kind, (key, _suffix, credential) in TENANTS.items():
        for writer in ("update", "patch"):
            for handle in ("other", "attacker\u3002example#"):
                c = store.create(kind, kind, {key: "acme"}, credential)
                store.update(c.id, kind, {key: "ACME"}, None)
                assert store.dec_secret(store.get(c.id).secret_enc) == credential
                if writer == "update":
                    store.update(c.id, kind, {key: handle}, None)
                else:
                    store.patch_config(c.id, {key: handle})
                assert not store.dec_secret(store.get(c.id).secret_enc), (kind, writer)
                assert store.get(c.id).status == "error"


def test_rotation_destination_race():
    c = store.create("strapi", "rotation", {"base_url": "https://old.example"}, SEC_OLD)

    def during_test(candidate):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from app.services.connectors import store; "
                    f"store.update({c.id}, 'rotation', "
                    "{'base_url': 'https://attacker.example'}, None)"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return True, "tested"

    with patch.object(connector_class("strapi"), "test", during_test):
        ok, msg = store.rotate_secret(c.id, SEC_NEW)
    assert not ok and "destination changed" in msg, (ok, msg)
    assert not store.dec_secret(store.get(c.id).secret_enc)


def test_rotation_database_lock_and_success():
    from app.services.connectors import store_part2

    c = store.create("strapi", "rotation-ok", {"base_url": "https://old.example"}, SEC_OLD)
    original = store_part2.enc_secret
    checked = []

    def check_lock(value):
        with sqlite3.connect(engine.url.database, timeout=0) as other:
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                checked.append(True)
            else:
                other.rollback()
                raise AssertionError("rotation persisted without a database write lock")
        return original(value)

    with (
        patch.object(connector_class("strapi"), "test", return_value=(True, "tested")),
        patch.object(store_part2, "enc_secret", side_effect=check_lock),
    ):
        assert store.rotate_secret(c.id, SEC_NEW) == (True, "tested")
    assert checked == [True]
    row = store.get(c.id)
    assert store.dec_secret(row.secret_enc) == SEC_NEW
    assert row.last_test and row.status == "connected"


def test_invalid_api_token_does_not_mint():
    settings.set_many({"connector_api_token": ""})
    response = TestClient(app).post(
        "/api/connectors/999999/jira/translate",
        headers={"X-Loc-Token": TOKEN},
        json={"issue_key": "TEST-1"},
    )
    assert response.status_code == 401, response.text
    assert not settings.read_secret_setting("connector_api_token"), (
        "invalid API request minted shared token"
    )


def test_credential_less_audit_assertion_detects_regression():
    from app.services import audit

    original = audit.log

    def corrupt(**kwargs):
        if kwargs.get("action") == "connector.update":
            kwargs["detail"] = CLEARED_MARK
        return original(**kwargs)

    with patch.object(audit, "log", side_effect=corrupt):
        try:
            test_credential_less_connection_is_not_flagged()
        except AssertionError:
            return
    raise AssertionError("audit assertion failed to detect injected false clearance")


def test_host_swap():
    failures = []
    for kind, key in HOST_KEYS.items():
        for secret in (None, SEC_BLANK_PAIR):
            c = store.create(kind, kind, {key: "https://old.example"}, SEC_OLD)
            store.update(c.id, kind, {key: "https://new.example"}, secret)
            if store.dec_secret(store.get(c.id).secret_enc):
                failures.append(kind)
    assert not failures, f"host swaps retained stored credentials: {failures}"


def test_host_swap_with_partial_secret():
    """#165, the partial-submission path: a Jira/Confluence secret is a DICT
    ({"email": ..., "api_token": ...}), so a caller can make `new_secret` true with a
    junk email while leaving the token blank. If the stored secret is only cleared when
    NO new secret is supplied, the merge inherits the old api_token and carries the live
    credential to the attacker's host -- the attack this train exists to stop."""
    failures = []
    for kind in ("jira", "confluence"):
        c = store.create(
            kind,
            kind,
            {HOST_KEYS[kind]: "https://old.example"},
            SEC_LIVE,
        )
        store.update(
            c.id,
            kind,
            {HOST_KEYS[kind]: "https://attacker.example"},
            SEC_PARTIAL,
        )
        stored = store.dec_secret(store.get(c.id).secret_enc)
        if "LIVE-TOKEN" in stored.values():
            failures.append(f"{kind}: old api_token survived a host change ({stored!r})")
    assert not failures, "; ".join(failures)


def test_same_host_and_new_secret():
    for kind, key in HOST_KEYS.items():
        c = store.create(kind, kind, {key: "https://old.example"}, SEC_OLD)
        # #265: same ORIGIN only. This list used to include "http://OLD.example:8443/path/"
        # and so asserted the hostname-only pin that let a scheme or port change keep
        # the credential; test_origin_pin_scheme_and_port now proves that case clears.
        for url in ("https://OLD.example:443/path/", "https://old.example/", "https://old.example"):
            store.update(c.id, kind, {key: url}, SEC_SPACE)
            assert store.dec_secret(store.get(c.id).secret_enc)["api_token"] == "old"
        store.update(c.id, kind, {key: "https://new.example"}, SEC_NEW)
        assert store.dec_secret(store.get(c.id).secret_enc)["api_token"] == "new"
    c = store.create("zoho", "bare", {"api_domain": "old.example"}, SEC_ZOHO_OLD)
    store.update(c.id, "bare", {"api_domain": "https://OLD.example/"}, None)
    assert store.dec_secret(store.get(c.id).secret_enc)["access_token"] == "old"
    store.update(c.id, "bare", {"api_domain": "new.example"}, None)
    assert not store.dec_secret(store.get(c.id).secret_enc), "Zoho bare host swap retained secret"


def test_patch_config_pins_host():
    """GATE HIGH-1. `update()` is not the only writer of Connection.config.
    `patch_config()` is a second one, and POST /connectors/{kind}/oauth/start feeds it a
    caller-supplied base_url behind only _ai_block (can_tune_ai = ADMIN or LINGUIST) --
    the very role #165 is about. Guarding only update() left that route able to repoint a
    connection at an attacker host with the live credential intact."""
    failures = []
    for kind in ("jira", "confluence"):
        c = store.create(kind, kind, {"base_url": "https://old.example"}, SEC_LIVE)
        store.patch_config(c.id, {"base_url": "https://attacker.example"})
        row = store.get(c.id)
        if store.dec_secret(row.secret_enc):
            failures.append(f"{kind}: patch_config kept the credential across a host change")
        if row.status != "error":
            failures.append(f"{kind}: status not flagged after a credential drop ({row.status})")
    # A same-host patch must NOT destroy a working credential.
    c = store.create("strapi", "keep", {"base_url": "https://old.example"}, SEC_OLD)
    store.patch_config(c.id, {"base_url": "https://old.example/v2"})
    if not store.dec_secret(store.get(c.id).secret_enc):
        failures.append("strapi: same-host patch_config wrongly cleared the credential")
    assert not failures, "; ".join(failures)


def test_credential_less_connection_is_not_flagged():
    """GATE R2 MED-1. `bool(conn.secret_enc)` is not "has a credential": enc_secret({})
    is a NON-EMPTY Fernet token and create() always writes one, so every row has a truthy
    secret_enc. Testing it meant an operator fixing a typo'd URL on a credential-less
    connection got status=error, a message claiming their credentials were cleared, and a
    secret_cleared=True row in the HMAC-chained audit log."""
    failures = []
    targets = set()
    for secret in ({}, {"api_token": ""}, {"api_token": "   ", "email": ""}):
        c = store.create("strapi", "nocred", {"base_url": "https://old.example"}, secret)
        targets.add(f"connection:{c.id}")
        before = store.get(c.id).status
        store.update(c.id, "nocred", {"base_url": "https://new.example"}, None)
        row = store.get(c.id)
        if row.status == "error":
            failures.append(f"{secret!r}: credential-less connection flagged error")
        if row.status != before:
            failures.append(f"{secret!r}: status changed ({before} -> {row.status})")
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "connector.update")).all()
    rows = [row for row in rows if row.target in targets]
    assert len(rows) == len(targets), "expected one audit row per credential-less update"
    assert {row.target for row in rows} == targets, "missing credential-less audit targets"
    for row in rows:
        if CLEARED_MARK in (row.detail or ""):
            failures.append(f"credential-less host change claimed {CLEARED_MARK}")
    assert not failures, "; ".join(failures)


def test_reentering_credentials_clears_the_flag():
    """GATE R2 MED-2. The flag's own message says to re-enter credentials, so doing that
    must clear it. Before this, status stayed 'error' with the stale message while the web
    route redirected with a cheerful ?ok=1&msg=Saved."""
    c = store.create("strapi", "reflag", {"base_url": "https://old.example"}, SEC_OLD)
    store.update(c.id, "reflag", {"base_url": "https://new.example"}, None)
    assert store.get(c.id).status == "error", "host change should flag the connection"
    store.update(c.id, "reflag", {}, SEC_NEW)
    row = store.get(c.id)
    assert row.status != "error", f"re-entering credentials left status {row.status}"
    assert not (row.last_message or ""), f"stale message survived: {row.last_message!r}"
    # An UNRELATED error must NOT be cleared by an ordinary credential write.
    c2 = store.create("strapi", "other", {"base_url": "https://old.example"}, SEC_OLD)
    with Session(engine) as s:
        row2 = s.get(type(store.get(c2.id)), c2.id)
        row2.status = "error"
        row2.last_message = "Test connection failed: 401 from the upstream."
        s.add(row2)
        s.commit()
    store.update(c2.id, "other", {}, SEC_NEW)
    assert store.get(c2.id).status == "error", "an unrelated error must survive"


def test_non_string_config_value():
    """Reject malformed new URLs clearly; legacy malformed values remain correctable."""
    from app.models import Connection
    from app.output_safety import InvalidOutputURL

    for bad in (123, True, {"a": 1}, ["x"]):
        c = store.create("strapi", "bad", {"base_url": "https://old.example"}, SEC_OLD)
        # A bad (non-string) base_url is refused at the door by either guard: F6's URL
        # validation (InvalidOutputURL) or F1's unreadable-origin refusal (ConnectorError).
        try:
            store.update(c.id, "bad", {"base_url": bad}, None)
        except (ConnectorError, InvalidOutputURL):
            pass
        else:
            raise AssertionError("Non-string URL was accepted")
        assert store.get(c.id).config["base_url"] == "https://old.example"
        # Plant it directly (a legacy row the door never saw), then prove that reading a
        # stored non-string value back does not brick later updates either.
        with Session(engine) as session:
            legacy = session.get(Connection, c.id)
            legacy.config = {**(legacy.config or {}), "base_url": bad}
            session.add(legacy)
            session.commit()
        store.update(c.id, "bad", {"base_url": "https://other.example"}, None)


def test_excluded_modes():
    """Negative control: the two deliberately-excluded modes must KEEP their credential.

    Jira/Confluence OAuth pins to oauth.ATLASSIAN_API_HOST and never reads config
    base_url, so a base_url edit moves nothing and must not destroy a working token.

    Salesforce is NO LONGER excluded. This test used to assert that a login_url change
    KEPT the credential, which ratified the very hole gate round 3 proved: login_url is
    config (linguist-writable), the token exchange talks to it carrying client_id (a
    declared secret_field), and _mint_token then trusts the instance_url that response
    returns, so the attacker owns the REST base for everything afterwards. The reasoning
    that cleared it for three rounds -- "the REST host comes from the token exchange, not
    config" -- was backwards, because the token exchange IS the attacker once login_url
    moves."""
    for kind in ("jira", "confluence"):
        secret = SEC_OAUTH
        c = store.create(kind, kind, {"base_url": "https://old.example"}, secret)
        store.update(c.id, kind, {"base_url": "https://new.example"}, None)
        assert store.dec_secret(store.get(c.id).secret_enc) == secret
    c = store.create("salesforce", "sf", {"login_url": "https://old.example"}, SEC_OAUTH)
    store.update(c.id, "sf", {"login_url": "https://attacker.example"}, None)
    assert not store.dec_secret(store.get(c.id).secret_enc), (
        "a salesforce login_url repoint must clear the credential"
    )
    # A BLANK login_url means the sandbox default, so setting it for the first time is a
    # real repoint and must clear too.
    c2 = store.create("salesforce", "sf2", {}, SEC_OAUTH)
    store.update(c2.id, "sf2", {"login_url": "https://attacker.example"}, None)
    assert not store.dec_secret(store.get(c2.id).secret_enc), (
        "setting login_url from its blank default must count as a host change"
    )


def test_fixed_host_kinds_really_are_fixed():
    """GATE R4 LOW-1. Prove the fixed-host claim by DRIVING each connector with hostile
    config, not by trusting the map.

    Every spec config field is set to an attacker host, then the connector is exercised
    through a MockTransport. Whatever host it actually reaches must be one of the hosts
    the map declares for that kind. A kind whose destination turns out to be
    config-controlled fails here rather than sitting in the map looking safe.

    The gate that asked for this also found the trap it has to avoid: its first HubSpot
    probe issued NO request at all and passed vacuously. So a probe that reaches nothing
    is a failure, not a pass."""
    from app.services.connectors import connector_class
    from app.services.connectors.store_part2 import _FIXED_HOST_KINDS

    base.set_test_resolver(lambda host: ["93.184.216.34"])
    # Kinds this harness cannot drive to a request today. Listed so the gap is visible and
    # bounded; this set may only SHRINK.
    #
    # GATE R5 MED-1: it used to hold contentful, hubspot and intercom, described as kinds
    # the harness "cannot drive". That was not true. They only LOOKED undrivable because
    # the probe set EVERY config field hostile at once, which trips their region and
    # account-shape validators before any request is built. An exemption that is really a
    # harness bug is worse than no exemption, because it parks the exact kinds a reviewer
    # would otherwise look at hardest: contentful's real EU host was missing from the
    # allowlist and two fabricated hosts were in it, and nothing noticed for two rounds.
    #
    # The probe now also drives each config field hostile ON ITS OWN, leaving the others
    # empty, which is what the round-4 gate did by hand, and it drives the LEGITIMATE
    # region shapes below so the map is checked in BOTH directions.
    KNOWN_UNDRIVABLE = set()
    # Legitimate configurations a real operator can set, per kind. Default: the empty
    # config. These exist so the map can be checked for FABRICATED entries, which is the
    # half that was missing: a declared host nothing can reach is not a harmless extra,
    # it is a pre-authorized destination waiting for a future caller. Both of round 5's
    # bad entries (cdn.contentful.com, api.helpscout.net) die on this assertion.
    # Values are OVERLAYS on a benign filler config; "" means "leave this field unset and
    # take the connector's own default", which is itself a legitimate operator shape.
    LEGITIMATE = {
        "contentful": [{"region": "US"}, {"region": "EU"}],
        "intercom": [{"region": r} for r in ("US", "EU", "AU")],
        "document360": [{"region": r} for r in ("global", "us", "ca")],
        # content_types is validated fail-closed against a SUPPORTED set, so the filler
        # value is refused before any request. Its default (site-pages) is the real shape.
        "hubspot": [{"content_types": "", "content_state": ""}],
        # Google Docs validates both folder ids, the key and the delegated user before any
        # request, so the filler "fixture" is refused; these are the real shapes. The secret
        # is a syntactically valid (never usable) service account key.
        "gdocs": [
            {
                "source_folder": "1AbCdEfGhIjKlMnOpQrStUvWx",
                "target_folder": "1ZyXwVuTsRqPoNmLkJiHgFeDc",
                "impersonate_user": "",
                "max_files": "",
            }
        ],
        # Microsoft 365 Files validates its tenant, client, drive and folder strings before any
        # request, so the filler "fixture" is refused; these are the real shapes.
        "msgraph": [
            {
                "tenant_id": "contoso.onmicrosoft.com",
                "client_id": "11111111-2222-3333-4444-555555555555",
                "site_url": "https://contoso.sharepoint.com/sites/Loc",
                "site_id": "",
                "library": "",
                "drive_id": "",
                "onedrive_user": "",
                "source_folder": "Source",
                "target_root": "Translations",
                "extensions": "",
                "max_file_mb": "",
                "max_files": "",
            }
        ],
        "bitbucket": [
            {
                "repo": "acme/site",
                "source_paths": "locales/{locale}.json",
                "base_branch": "main",
                "branch_pattern": "loc/{connection}",
                "locale_style": "bcp47",
                "source_locale": "en-US",
                "target_locales": "fr-FR",
            }
        ],
    }
    failures, unverified = [], []
    for kind, allowed in _FIXED_HOST_KINDS.items():
        cls = connector_class(kind)
        if cls is None:
            failures.append(f"{kind}: not in the registry")
            continue
        fields = [f.name for f in (cls.spec.config_fields or [])]
        # All fields hostile at once, then each field hostile ALONE. The second shape is
        # the one that gets past a validator that rejects a sibling field.
        hostile_configs = [dict.fromkeys(fields, "attacker.example")]
        hostile_configs += [{n: "attacker.example"} for n in fields]
        # Benign filler for every field, then the declared variant on top. A bare {} does
        # not drive a kind whose required fields are missing, and "nothing was reached"
        # must never be how the unreached-host check passes.
        legit_configs = [
            {**dict.fromkeys(fields, "fixture"), **variant}
            for variant in LEGITIMATE.get(kind, [{}])
        ]
        # Build the credential from the SPEC too. A fixture that does not match a kind's
        # expected secret keys makes test() raise before it ever issues a request, which
        # the vacuity guard below then reports -- correctly, but unhelpfully.
        credential = {f.name: "fixture" for f in (cls.spec.secret_fields or [])} or SEC_OLD

        def drive(configs, _cls=cls, _credential=credential):
            seen = []

            def respond(request, _seen=seen):
                _seen.append(request.url.host)
                return httpx.Response(200, json={"data": [], "results": [], "items": []})

            for cfg in configs:
                obj = _cls(cfg, _credential, transport=httpx.MockTransport(respond))
                with suppress(Exception):
                    obj.test()
                # Also record the CONSTRUCTED base host, not only hosts an issued request
                # happened to reach. A kind whose test() short-circuits before any request
                # still has a destination, and that destination is the thing that matters
                # -- the round-4 gate passed hubspot vacuously by not looking at it.
                base_fn = getattr(obj, "_base", None)
                if callable(base_fn):
                    with suppress(Exception):
                        host = urlsplit(str(base_fn())).hostname or ""
                        if host:
                            seen.append(host)
                # A kind that talks to several fixed hosts (Google Docs: Drive, Docs and the
                # token endpoint) declares them all, so the harness can see each one.
                hosts_fn = getattr(obj, "_fixed_hosts", None)
                if callable(hosts_fn):
                    with suppress(Exception):
                        seen.extend(str(h) for h in hosts_fn())
            return seen

        reached = drive(hostile_configs)
        legit_reached = drive(legit_configs)
        if not reached and not legit_reached:
            # Neither a request nor a resolvable base under any shape: this kind is not
            # verifiable through this harness. Record it EXPLICITLY rather than passing on
            # silence. The set may not grow: a NEW fixed-host kind has to be drivable, or
            # whoever adds it has to explain why not.
            #
            # GATE R6 LOW-5: this used to `continue`, which skipped BOTH checks below. An
            # exemption is a statement about what the harness can DRIVE, and it must not
            # also become an exemption from the map being honest. Exempting a kind is now
            # exactly as strict about a fabricated allowlist entry as not exempting it, so
            # the route that hid `cdn.contentful.com` for two rounds is closed even if
            # KNOWN_UNDRIVABLE grows again.
            unverified.append(kind)
        stray = [h for h in (*reached, *legit_reached) if h not in allowed]
        if stray:
            failures.append(
                f"{kind}: reached {sorted(set(stray))} with hostile config, but the map "
                f"declares {sorted(allowed)}. It is NOT fixed-host: pin it instead."
            )
        unreached = set(allowed) - set(legit_reached)
        if unreached:
            failures.append(
                f"{kind}: the map declares {sorted(unreached)}, which NO legitimate "
                "configuration reaches. Either it is fabricated (delete it) or a real "
                "shape is missing from LEGITIMATE (add it). A declared host nothing "
                "reaches is a pre-authorized destination, not a harmless extra."
            )
    assert not failures, "; ".join(failures)
    grew = set(unverified) - KNOWN_UNDRIVABLE
    assert not grew, (
        f"new fixed-host kinds that this harness cannot drive: {sorted(grew)}. "
        "Make them drivable, or justify the exemption explicitly."
    )
    shrank = KNOWN_UNDRIVABLE - set(unverified)
    assert not shrank, (
        f"these are drivable now, so remove them from KNOWN_UNDRIVABLE: {sorted(shrank)}"
    )


def test_every_kind_is_classified():
    """GATE R3 MED-2, and the most valuable test in this train.

    Nothing asserted that every registered connector kind is covered by one of the pin
    maps, so a kind that was in neither simply fell through and its credential was never
    pinned. That silent fall-through is HOW the Freshdesk, Zendesk, Shopify and Salesforce
    holes each survived multiple review rounds: four passes reasoned about the connectors
    they happened to look at. A new connector added tomorrow gets the same free pass
    unless something fails.

    Each kind must be in exactly one of: TENANT_HOSTS (host composed from an
    operator-supplied handle), _CREDENTIAL_HOST_KEYS (host read from a config key), or
    _FIXED_HOST_KINDS (no config-controlled destination at all).

    GATE R4 LOW-1: membership alone is not enough. A future author could green this by
    dropping a base_url-driven kind into _FIXED_HOST_KINDS, leaving it unpinned AND
    blessed, which is worse than the fall-through this replaced. So the fixed-host claim
    is MEASURED in test_fixed_host_kinds_really_are_fixed below, not taken on trust."""
    from app.services.connectors import REGISTRY
    from app.services.connectors.store_part2 import (
        _CREDENTIAL_HOST_KEYS,
        _FIXED_HOST_KINDS,
    )
    from app.services.connectors.tenant_host import TENANT_HOSTS

    unclassified, ambiguous = [], []
    for kind in REGISTRY:
        buckets = [
            name
            for name, members in (
                ("TENANT_HOSTS", TENANT_HOSTS),
                ("_CREDENTIAL_HOST_KEYS", _CREDENTIAL_HOST_KEYS),
                ("_FIXED_HOST_KINDS", _FIXED_HOST_KINDS),
            )
            if kind in members
        ]
        if not buckets:
            unclassified.append(kind)
        elif len(buckets) > 1:
            ambiguous.append(f"{kind} in {buckets}")
    assert not unclassified, (
        "connector kinds with NO credential-destination classification, so a host change "
        f"would not clear their credential: {sorted(unclassified)}. Add each to "
        "TENANT_HOSTS, _CREDENTIAL_HOST_KEYS, or _FIXED_HOST_KINDS after checking how it "
        "builds its request host."
    )
    assert not ambiguous, f"connector kinds classified twice: {ambiguous}"


def test_service_cannot_authorize():
    c = store.create("strapi", "service", {"base_url": "https://old.example"}, SEC_OLD)
    store.update(c.id, "service", {"base_url": "https://new.example"}, {"api_token": ""})
    connector = store.build(store.get(c.id))
    try:
        headers = connector._headers()
    except ConnectorError:
        assert not connector.secret
    else:
        assert "Authorization" not in headers, "new destination can still receive Authorization"


def test_host_audit():
    c = store.create("strapi", "audit", {"base_url": "https://old.example/private"}, SEC_OLD)
    store.update(c.id, "audit", {"base_url": "https://new.example/private"}, None)
    with Session(engine) as s:
        rows = s.exec(
            select(AuditLog)
            .where(AuditLog.action == "connector.update")
            .where(AuditLog.target == f"connection:{c.id}")
        ).all()
    assert len(rows) == 1, "store update must produce one connector.update audit row"
    assert "https://old.example -> https://new.example" in rows[0].detail  # #265: origins
    assert "secret_cleared=True" in rows[0].detail  # pregate: allow-secret offline fixture
    assert "/private" not in rows[0].detail


def test_origin_pin_scheme_and_port():
    """#265: the pin compares the ORIGIN, not the hostname. The reviewer's probe
    (probe_r2c_scheme.py) kept the admin's Jira credential for an https -> http downgrade
    and for a port change, and sent Basic auth there. A same-origin path edit and an
    explicit default port still keep it."""
    base.set_test_resolver(lambda host: ["93.184.216.34"])
    for new, should_clear in (
        ("http://acme.atlassian.net", True),
        ("https://acme.atlassian.net:8443", True),
        ("http://acme.atlassian.net:80/x", True),
        ("https://acme.atlassian.net:443", False),
        ("https://ACME.atlassian.net/wiki", False),
    ):
        c = store.create("jira", "origin", {"base_url": "https://acme.atlassian.net"}, SEC_LIVE)
        store.update(c.id, "origin", {"base_url": new}, None, actor="linguist@corp.example")
        kept = store.dec_secret(store.get(c.id).secret_enc).get("api_token")
        assert (kept is None) is should_clear, (new, kept)
        if not should_clear:
            continue
        seen = []

        def respond(req, seen=seen):
            seen.append(req.headers.get("authorization", ""))
            return httpx.Response(200, json={"accountId": "x", "displayName": "x"})

        obj = store.build_fresh(c.id, transport=httpx.MockTransport(respond))
        with suppress(Exception):
            obj.test()
        assert not any(h.startswith("Basic") for h in seen), (new, seen)
        with Session(engine) as s:
            row = s.exec(
                select(AuditLog)
                .where(AuditLog.action == "connector.update")
                .where(AuditLog.target == f"connection:{c.id}")
            ).first()
        assert row is not None and CLEARED_MARK in row.detail, (new, row)


def test_strapi_error_redacted():
    c = store.create("strapi", "error", {"base_url": "https://old.example"}, SEC_OLD)
    connector = store.build(store.get(c.id))
    with patch.object(connector, "_get", return_value=httpx.Response(403, text="UPSTREAM_SECRET")):
        try:
            connector._fetch_locales()
        except ConnectorError as exc:
            assert "UPSTREAM_SECRET" not in str(exc), "raw upstream body disclosed"
            assert "403" in str(exc) and "old.example" in str(exc)
        else:
            assert False, "expected upstream failure"


def _login(role, suffix=""):
    email = f"{role}{suffix}@boundary.test"
    security.create_user(email, email, role, PASSWORD)
    client = TestClient(app)
    csrf_for(client)
    response = client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    csrf_for(client)
    return client


def test_token_pages():
    settings.set_many({"security_auth_required": "1", "connector_api_token": TOKEN})
    settings.invalidate_cache()
    c = store.create("jira", "pages", {}, {})
    failures = []
    # GATE R5 HIGH-1: loop EVERY role, not a chosen three. The predicate was
    # security.can_manage while the comment beside it claimed can_manage meant admin, and
    # this loop ran (VIEWER, ADMIN, LINGUIST) -- none of which can distinguish can_manage
    # from is_admin. MANAGER is the one role that does, and it was the one role absent.
    # A role matrix that omits a role is not a role matrix.
    assert set(Role.ALL) == {
        Role.ADMIN,
        Role.MANAGER,
        Role.REVIEWER,
        Role.LINGUIST,
        Role.TRANSLATOR,
        Role.VIEWER,
    }, "a new role was added; decide explicitly whether it may see the install-wide token"
    for role in Role.ALL:
        client = _login(role)
        for path in ("/connectors/jira", f"/connectors/connection/{c.id}"):
            response = client.get(path)
            assert response.status_code == 200, response.status_code
            # ADMIN only. The token is not customer-confined (it resolves to
            # {"kind": "connector", "org_id": None} with no user, so _ident_customer_id is
            # None and _ai_role_block no-ops), so a customer-BOUND LINGUIST holding it
            # escapes the R2-3 customer ring across the whole /api/connectors surface, and
            # a MANAGER holding it bypasses the can_tune_ai gate on every connector write.
            if (TOKEN in response.text) != (role == Role.ADMIN):
                failures.append(f"{role} {path}")
            if role != Role.ADMIN:
                if response.context["api_token"] != "":
                    failures.append(f"token in viewer context {path}")
                if "<label>Connector API token" in response.text:
                    failures.append(f"empty token row {path}")
                # Even an accidentally populated future context cannot disclose it.
                rendered = response.template.render({**response.context, "api_token": TOKEN})
                if TOKEN in rendered:
                    failures.append(f"template lacks role guard {path}")
    assert not failures, f"incorrect token disclosure: {failures}"


def test_token_rotation_is_admin_only_and_audited():
    """GATE R5 MED-2. The round-4 fix admin-gated and audited
    POST /connectors/token/regenerate, and the round-4 reply said it landed "with a
    role-matrix case asserting LINGUIST gets 403 and the setting is unchanged".

    That case did not exist. Reverting _admin_block to _ai_block left the suite green, and
    so did deleting the audit.log call outright. This is the case the reply claimed, and it
    covers BOTH halves, because a confident and wrong statement about coverage is how the
    can_manage / is_admin confusion in HIGH-1 survived four rounds.

    Every rotation breaks the Airtable Extension, the Airtable Automation webhook and the
    Jira Forge app install-wide until each is re-paired, so an unaudited rotation by a
    non-admin is destruction with no record of who did it."""
    from app.services import audit

    settings.set_many({"security_auth_required": "1", "connector_api_token": TOKEN})
    settings.invalidate_cache()
    failures = []
    for role in Role.ALL:
        client = _login(role, "-rotate")
        settings.set_many({"connector_api_token": TOKEN})
        settings.invalidate_cache()
        seen = []
        with patch.object(audit, "log", side_effect=lambda _s=seen, **kw: _s.append(kw)):
            response = client.post("/connectors/token/regenerate", follow_redirects=False)
        settings.invalidate_cache()
        rotated = settings.read_secret_setting("connector_api_token") != TOKEN
        if role == Role.ADMIN:
            if response.status_code != 303 or not rotated:
                failures.append(f"admin could not rotate: {response.status_code} {rotated}")
            actions = [k.get("action") for k in seen]
            if "connector.token_regenerate" not in actions:
                failures.append(f"admin rotation wrote no audit row: {actions}")
            actor = next(
                (k.get("actor") for k in seen if k.get("action") == "connector.token_regenerate"),
                None,
            )
            if not actor or "admin" not in str(actor):
                failures.append(f"audit row has no real actor: {actor!r}")
        else:
            if rotated:
                failures.append(f"{role} rotated the install-wide token")
            if response.status_code == 303:
                failures.append(f"{role} was admitted to the rotation route")
            # _admin_block writes its own access-denied row, which is correct and wanted.
            # What must never appear is a rotation row, because that would claim the
            # credential changed. The first cut of this case asserted "no audit row at
            # all" and reported four false failures.
            if any(k.get("action") == "connector.token_regenerate" for k in seen):
                failures.append(f"{role} refusal claimed a rotation in the audit log: {seen}")
    assert not failures, f"token rotation gate: {failures}"


def test_viewer_does_not_mint():
    settings.set_many({"security_auth_required": "1", "connector_api_token": ""})
    client = _login(Role.VIEWER, "-no-mint")
    c = store.create("jira", "no-mint", {}, {})
    failures = []
    for path in ("/connectors/jira", f"/connectors/connection/{c.id}"):
        settings.set_many({"connector_api_token": ""})
        assert client.get(path).status_code == 200
        if settings.read_secret_setting("connector_api_token"):
            failures.append(path)
    assert not failures, f"viewer GET minted a credential for: {failures}"


def test_companion_identity():
    settings.set_many({"security_auth_required": "1", "connector_api_token": TOKEN})
    settings.invalidate_cache()
    c = store.create("jira", "companion", {}, {})
    with patch_pieces(
        "app.services.connectors.store",
        "jira_translate",
        return_value=(True, "stubbed", {"translated": 1}),
    ) as translate:
        response = TestClient(app).post(
            f"/api/connectors/{c.id}/jira/translate",
            headers={"X-Loc-Token": TOKEN},
            json={"issue_key": "TEST-1"},
        )
        assert response.status_code == 403, response.text
        translate.assert_not_called()


def test_shared_token_has_no_user_authority():
    from types import SimpleNamespace

    from app.services import apikeys
    from app.web.api_discovery import _job_readable

    settings.set_many({"connector_api_token": TOKEN})
    c = store.create("jira", "protected", {}, {})
    client = TestClient(app)
    for auth in ("1", "0"):
        settings.set_many({"security_auth_required": auth})
        settings.invalidate_cache()
        paths = [
            ("GET", "/api/connectors"),
            ("GET", "/api/capabilities/glossary"),
            ("GET", f"/api/connectors/{c.id}/airtable/webhook"),
            ("POST", "/api/connectors"),
            ("POST", "/api/translate/text"),
        ]
        paths += [
            ("POST", f"/api/connectors/{c.id}/{suffix}")
            for suffix in (
                "delete",
                "prune-config",
                "schedule",
                "update",
                "test",
                "sync",
                "pull",
                "push",
                "airtable/records",
                "airtable/records/delete",
                "airtable/webhook/register",
                "airtable/webhook/unregister",
                "airtable/webhook",
            )
        ]
        for method, path in paths:
            r = client.request(method, path, headers={"X-Loc-Token": TOKEN}, json={})
            assert r.status_code == 403, (auth, path, r.status_code, r.text)
        assert store.get(c.id) is not None
        assert not _job_readable({"kind": "connector"}, SimpleNamespace())
    settings.set_many({"security_auth_required": "1"})
    user = security.create_user("train-admin@example.test", "Train", Role.ADMIN, PASSWORD)
    key = apikeys.create_key(user.id, "train")[1]
    r = client.post(f"/api/connectors/{c.id}/prune-config", headers={"X-Loc-Token": key})
    assert r.status_code == 200, r.text


def test_unsigned_delivery_headers_and_zendesk_freshness():
    import base64
    import hashlib
    import hmac
    import json
    from datetime import datetime, timedelta, timezone

    from app.services.connectors import webhooks

    signing_key = "offline-webhook-fixture"  # pregate: allow-secret offline fixture
    body = json.dumps({"detail": {"id": "123"}}).encode()
    c = store.create("zendesk", "replay", {}, {"webhook_secret": signing_key})
    calls = []

    def enqueue(*args):
        calls.append(args)
        return 123

    def headers(at):
        timestamp = at.isoformat()
        digest = hmac.new(signing_key.encode(), timestamp.encode() + body, hashlib.sha256)
        return {
            "X-Zendesk-Webhook-Signature": base64.b64encode(digest.digest()).decode(),
            "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
        }

    now = datetime.now(timezone.utc)
    signed = headers(now)
    for i in range(4):
        result = webhooks.handle_intake(
            c.id, body, {**signed, "X-Zendesk-Webhook-Id": f"replay-{i}"}, enqueue=enqueue
        )
        assert result.status == 200, result.as_dict()
    assert len(calls) == 1, calls
    old_mac = base64.b64encode(hmac.new(signing_key.encode(), body, hashlib.sha256).digest())
    body_only = {**signed, "X-Zendesk-Webhook-Signature": old_mac.decode()}
    assert webhooks.handle_intake(c.id, body, body_only, enqueue=enqueue).status == 401
    for bad in (
        {},
        headers(now - timedelta(minutes=6)),
        headers(now + timedelta(minutes=6)),
        {**signed, "X-Zendesk-Webhook-Signature-Timestamp": "invalid"},
    ):
        result = webhooks.handle_intake(c.id, body, bad, enqueue=enqueue)
        assert result.status == 401, result.as_dict()
    tampered = {
        **signed,
        "X-Zendesk-Webhook-Signature-Timestamp": (now + timedelta(seconds=1)).isoformat(),
    }
    assert webhooks.handle_intake(c.id, body, tampered, enqueue=enqueue).status == 401
    for kind, name in (
        ("jira", "X-Atlassian-Webhook-Identifier"),
        ("confluence", "X-Hub-Delivery"),
    ):
        keys = {webhooks.dedup_key(kind, body, json.loads(body), {name: str(i)}) for i in range(4)}
        assert len(keys) == 1, (kind, keys)


def test_unsigned_header_replay_jobs():
    import hashlib
    import hmac
    import json

    from app.services.connectors import webhooks

    signing_key = "offline-replay-fixture"  # pregate: allow-secret offline fixture
    for kind, id_header, payload in (
        ("jira", "X-Atlassian-Webhook-Identifier", {"issue_key": "LOC-123"}),
        ("confluence", "X-Hub-Delivery", {"page": {"id": "123"}}),
    ):
        c = store.create(kind, "signed replay", {}, {"webhook_secret": signing_key})
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(signing_key.encode(), body, hashlib.sha256).hexdigest()
        mac_header = "X-Hub-Signature" if kind == "jira" else "X-Hub-Signature-256"
        calls = []
        for i in range(4):
            result = webhooks.handle_intake(
                c.id,
                body,
                {mac_header: signature, id_header: str(i)},
                enqueue=lambda *args, calls=calls: calls.append(args) or 123,
            )
            assert result.status == 200, (kind, result.as_dict())
        assert len(calls) == 1, (kind, calls)


def test_suspended_connector_entry_points():
    from datetime import datetime, timedelta

    from app.models import Connection, Organization
    from app.services import jobs, scheduler
    from app.services.connectors import webhooks

    with Session(engine) as session:
        org = Organization(name="Suspended fixture", active=False)
        session.add(org)
        session.commit()
        session.refresh(org)
        org_id = org.id
    c = store.create("airtable", "suspended", {}, {})
    now = datetime.utcnow()
    with Session(engine) as session:
        row = session.get(Connection, c.id)
        row.org_id = org_id
        row.config = {
            "sync_every_minutes": 5,
            "sync_next_at": (now - timedelta(minutes=1)).isoformat(),
        }
        session.add(row)
        session.commit()
    settings.set_many({"multi_org_enabled": "1", "sync_scheduler_enabled": "1"})
    settings.invalidate_cache()
    try:
        with (
            patch_pieces("app.services.connectors.store", "refresh_webhooks") as refresh,
            patch.object(jobs, "submit_unique", return_value=(123, True)) as submit,
        ):
            scheduler.tick(now=now)
            assert all(call.args[0] != c.id for call in refresh.call_args_list)
            assert not any(
                call.args[1].get("connection_id") == c.id for call in submit.call_args_list
            )
        with patch.object(webhooks, "verify_signature", return_value=True) as verify:
            result = webhooks.handle_intake(c.id, b"{}", {}, enqueue=lambda *a: 1)
            assert result.status == 401, result.as_dict()
            verify.assert_not_called()
        for extra in (
            {},
            {"retry_failures": ["x"]},
            {"webhook_ping_pull": True},
            {"jira_translate_pending": True},
            {"webhook_record_ids": ["x"]},
        ):
            result = jobs._run_connector_translate(
                {"connection_id": c.id, **extra}, lambda *a: None
            )
            assert result.get("skipped_inactive_org"), result
        with Session(engine) as session:
            org = session.get(Organization, org_id)
            org.active = True
            session.add(org)
            session.commit()
        with (
            patch_pieces("app.services.connectors.store", "refresh_webhooks") as refresh,
            patch.object(jobs, "submit_unique", return_value=(123, True)) as submit,
        ):
            scheduler.tick(now=now)
            assert any(call.args[0] == c.id for call in refresh.call_args_list)
            assert any(call.args[1].get("connection_id") == c.id for call in submit.call_args_list)
        with patch_pieces(
            "app.services.connectors.store", "translate_now", return_value=(True, "ok", {})
        ) as translate:
            jobs._run_connector_translate({"connection_id": c.id}, lambda *a: None)
            translate.assert_called_once()
    finally:
        settings.set_many({"multi_org_enabled": "0"})
        settings.invalidate_cache()


if __name__ == "__main__":
    init_db()
    failed = []
    # Block real HTTP and DNS; Windows asyncio uses local socket pairs internally.
    with (
        patch.object(
            httpx.HTTPTransport, "handle_request", side_effect=AssertionError("network forbidden")
        ),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")),
    ):
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                    print(f"PASS {name}")
                except Exception as exc:
                    failed.append(name)
                    print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    assert not failed, f"failed cases: {failed}"
    print("SEC CONNECTOR BOUNDARY OK")
