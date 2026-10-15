"""Offline regressions for #218 (and the shared rule behind #165/#265); run through
tests/test_verify_suites.py only.

The rule (app/services/credential_origin.py): when the ORIGIN (scheme, host, effective
port) of a credential's destination changes, the stored credential is cleared unless the
same request supplies a new one, and the move is audited with the old and new origin
(never the key).

Since #375, successful web configuration cases use an administrator; linguist
refusal and no-mutation checks live in verify_engine_admin_boundary.py.

  * An ADMIN repoints an admin-keyed engine profile (openai_compatible, deepl, cohere,
    lara, amazon) through the web Setup AI form with a blank key: the key and the paired
    secret are cleared, Test never sends the admin key to the new host, an audit row names
    both origins and not the key, and the form says why the key is gone. (Reviewer probe
    probe_r2ai_engine_repoint.py.)
  * Scheme and port changes count as a move; a path change or an explicit default port
    does not.
  * An admin re-entering the key on the new origin keeps it, and Test sends it there.
  * Amazon: a change between real AWS regions is not a move; a region that splices a
    foreign host into the endpoint is.
  * The legacy local slot (probe_r2ai_local_slot.py): an ADMIN saving /setup-ai/local
    with a new URL and a blank key clears the saved key, the connection test does not
    send it, and the move is audited; a local profile with a blank base_url (which
    inherits the slot URL) loses its key too. Same-origin edits keep the key.
  * CLI `engine update --base-url` says the key was cleared.
  * Vendor endpoints follow the same rule.

Every case FAILED on base 9bc3c6a1 except test_local_slot_same_origin_or_new_key_keeps,
which guards against a FALSE clear (it passes on base by design). Hermetic: throwaway data
dir, a fake public resolver, httpx.MockTransport for every request. ASCII-only output.
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_engine_repoint_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine as db_engine
from app.db import init_db
from app.main import app
from app.models import AuditLog, Role
from app.services import egress, engines, security, settings, vendors
from tests._csrf import csrf_for

PASSWORD = "repoint-password-123456"  # pregate: allow-secret offline fixture
ADMIN_KEY = "sk-ADMIN-SET-SECRET-KEY"  # pregate: allow-secret offline fixture
ADMIN_PAIR = "ADMIN-PAIRED-SECRET-VALUE"  # pregate: allow-secret offline fixture
NEW_KEY = "sk-ADMIN-REENTERED-KEY"  # pregate: allow-secret offline fixture
LOCAL_SAVED = "sk-local-admin-saved"  # pregate: allow-secret offline fixture
VENDOR_SECRET = {"auth_token": "vendor-admin-token"}  # pregate: allow-secret offline fixture
ATTACKER = "https://collector.attacker.example"

init_db()
egress.set_test_resolver(lambda host: ["93.184.216.34"])
settings.set_many({"security_auth_required": "1"})
settings.invalidate_cache()

_clients: dict = {}


def _login(role):
    if role in _clients:
        return _clients[role]
    email = f"{role}@repoint.test"
    security.create_user(email, email, role, PASSWORD)
    client = TestClient(app)
    csrf_for(client)
    response = client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.status_code
    csrf_for(client)
    _clients[role] = client
    return client


def _audit_rows(action, target):
    with Session(db_engine) as s:
        return s.exec(
            select(AuditLog).where(AuditLog.action == action).where(AuditLog.target == target)
        ).all()


def _recording_transport(seen):
    def handler(request):
        seen.append((str(request.url), dict(request.headers), str(request.url.params)))
        return httpx.Response(
            200,
            json={
                "data": [],
                "character_count": 0,
                "translations": [{"text": "x"}],
                "TerminologyPropertiesList": [],
            },
        )

    return httpx.MockTransport(handler)


def _leaked(seen, *secrets):
    blob = repr(seen)
    return [s for s in secrets if s in blob]


def _web_update(client, prof, base_url, api_key=""):
    return client.post(
        f"/setup-ai/engines/{prof.id}",
        data={
            "name": prof.name,
            "base_url": base_url,
            "model": prof.model or "m",
            "api_key": api_key,
            "region": (prof.config or {}).get("region", ""),
            "enabled": "1",
        },
        follow_redirects=False,
    )


KEYED = (
    ("openai_compatible", "https://openrouter.ai/api/v1", {}),
    ("deepl", "https://api.deepl.com", {}),
    ("cohere", "https://api.cohere.com", {}),
    ("lara", "", {"access_key_secret": ADMIN_PAIR}),
    ("amazon", "", {"region": "us-east-1", "access_key_secret": ADMIN_PAIR}),
)


def test_admin_repoint_clears_key_every_kind():
    lin = _login(Role.ADMIN)
    for kind, url, cfg in KEYED:
        prof = engines.create_profile(
            f"{kind}-prod",
            kind,
            base_url=url,
            api_key=ADMIN_KEY,
            model="m",
            cfg=dict(cfg),
            actor="admin@repoint.test",
        )
        old_origin = engines.credential_destination(kind, url, prof.config)
        r = _web_update(lin, prof, ATTACKER)
        assert r.status_code == 303, (kind, r.status_code)
        location = r.headers.get("location", "")
        assert "engine_saved=1" in location, (kind, location)
        assert "credentials%20were%20cleared" in location, (kind, location)
        after = engines.get_profile(prof.id)
        assert after.base_url == ATTACKER, (kind, after.base_url)
        assert engines.dec_key(after.api_key_enc) == "", f"{kind}: admin key kept"
        assert not (after.config or {}).get("access_key_secret_enc"), f"{kind}: pair kept"
        seen = []
        engines.test_profile(prof.id, transport=_recording_transport(seen))
        assert not _leaked(seen, ADMIN_KEY), f"{kind}: admin key sent to {seen}"
        rows = _audit_rows("engine.destination_changed", f"engine:{prof.id}")
        assert len(rows) == 1, (kind, len(rows))
        detail = rows[0].detail
        assert f"{old_origin} -> {ATTACKER}" in detail, (kind, detail)
        assert "credential_cleared=True" in detail, (kind, detail)
        assert ADMIN_KEY not in detail and ADMIN_PAIR not in detail
        assert rows[0].actor == "admin@repoint.test", rows[0].actor


def test_scheme_and_port_moves_clear_same_origin_keeps():
    cases = (
        ("http://api.example.com/v1", True),
        ("https://api.example.com:8443/v1", True),
        ("https://other.example.com/v1", True),
        ("https://api.example.com:443/v1", False),
        ("https://API.example.com/other/path", False),
    )
    for new, should_clear in cases:
        prof = engines.create_profile(
            f"origin-{new}",
            "openai_compatible",
            base_url="https://api.example.com/v1",
            api_key=ADMIN_KEY,
            model="m",
        )
        engines.update_profile(prof.id, base_url=new, actor="t")
        kept = engines.dec_key(engines.get_profile(prof.id).api_key_enc) == ADMIN_KEY
        assert kept is not should_clear, (new, kept)
        rows = _audit_rows("engine.destination_changed", f"engine:{prof.id}")
        assert len(rows) == (1 if should_clear else 0), (new, len(rows))


def test_admin_reentering_key_on_new_origin_works():
    admin = _login(Role.ADMIN)
    prof = engines.create_profile(
        "reentry",
        "openai_compatible",
        base_url="https://api.example.com/v1",
        api_key=ADMIN_KEY,
        model="m",
    )
    r = _web_update(admin, prof, "https://api.newhost.example/v1", api_key=NEW_KEY)
    assert r.status_code == 303 and "credentials%20were%20cleared" not in r.headers["location"]
    after = engines.get_profile(prof.id)
    assert engines.dec_key(after.api_key_enc) == NEW_KEY
    seen = []
    ok, _msg = engines.test_profile(prof.id, transport=_recording_transport(seen))
    assert ok, _msg
    assert seen and seen[0][0].startswith("https://api.newhost.example/")
    assert seen[0][1].get("authorization") == f"Bearer {NEW_KEY}"
    assert not _leaked(seen, ADMIN_KEY)
    rows = _audit_rows("engine.destination_changed", f"engine:{prof.id}")
    assert len(rows) == 1 and "credential_cleared=False" in rows[0].detail, rows


def test_new_key_without_new_pair_clears_the_old_pair():
    prof = engines.create_profile(
        "pair", "lara", api_key=ADMIN_KEY, cfg={"access_key_secret": ADMIN_PAIR}
    )
    engines.update_profile(prof.id, base_url=ATTACKER, api_key=NEW_KEY, actor="t")
    after = engines.get_profile(prof.id)
    assert engines.dec_key(after.api_key_enc) == NEW_KEY
    assert not (after.config or {}).get("access_key_secret_enc"), "old paired secret kept"


def test_amazon_region():
    prof = engines.create_profile(
        "aws",
        "amazon",
        api_key=ADMIN_KEY,
        cfg={"region": "us-east-1", "access_key_secret": ADMIN_PAIR},
    )
    engines.update_profile(prof.id, cfg={"region": "eu-west-2"}, actor="t")
    assert engines.dec_key(engines.get_profile(prof.id).api_key_enc) == ADMIN_KEY, (
        "a move between real AWS regions must keep the key"
    )
    engines.update_profile(prof.id, cfg={"region": "attacker.example/x?"}, actor="t")
    after = engines.get_profile(prof.id)
    assert engines.dec_key(after.api_key_enc) == "", "a host spliced in via region kept the key"
    assert not (after.config or {}).get("access_key_secret_enc")


def test_cli_update_says_key_cleared():
    from app.cli.__main__ import main

    prof = engines.create_profile(
        "cli-repoint", "cohere", base_url="https://api.cohere.com", api_key=ADMIN_KEY
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["engine", "update", str(prof.id), "--base-url", ATTACKER])
    assert code == 0, (out.getvalue(), err.getvalue())
    assert "credentials were cleared" in out.getvalue() + err.getvalue()
    assert engines.dec_key(engines.get_profile(prof.id).api_key_enc) == ""
    rows = _audit_rows("engine.destination_changed", f"engine:{prof.id}")
    assert len(rows) == 1 and rows[0].actor == "cli", rows


def _set_local_slot(url, key):
    settings.set_many({"local_base_url": url, "local_api_key": key})
    settings.invalidate_cache()
    settings.apply_to_config()


def test_local_slot_repoint_by_admin():
    import app.web.admin as admin_mod

    _set_local_slot("http://10.0.0.5:1234", LOCAL_SAVED)
    inheriting = engines.create_profile("inherits-slot", "local", api_key=ADMIN_KEY)
    lin = _login(Role.ADMIN)
    sent = []
    original = admin_mod.local_complete
    admin_mod.local_complete = lambda base, key, model, *a, **k: sent.append((base, key)) or "ok"
    try:
        r = lin.post(
            "/setup-ai/local",
            data={"base_url": ATTACKER, "api_key": "", "local_model": "m"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        settings.invalidate_cache()
        settings.apply_to_config()
        assert settings.get("local_base_url") == ATTACKER
        assert settings.get("local_api_key") != LOCAL_SAVED, "saved local key kept"
        r = lin.post(
            "/setup-ai/local/test",
            data={"base_url": ATTACKER, "api_key": "", "local_model": "m"},
        )
        assert r.status_code == 200
        assert not [k for _b, k in sent if k == LOCAL_SAVED], f"saved key sent: {sent}"
    finally:
        admin_mod.local_complete = original
    rows = _audit_rows("settings.local_engine_destination_changed", "setting:local_base_url")
    assert rows, "local slot move not audited"
    detail = rows[-1].detail
    assert f"http://10.0.0.5:1234 -> {ATTACKER}" in detail and "credential_cleared=True" in detail
    assert LOCAL_SAVED not in detail
    assert rows[-1].actor == "admin@repoint.test", rows[-1].actor
    assert engines.dec_key(engines.get_profile(inheriting.id).api_key_enc) == "", (
        "a blank-URL local profile inherits the slot, so its key moved too"
    )
    assert _audit_rows("engine.destination_changed", f"engine:{inheriting.id}")


def test_local_slot_same_origin_or_new_key_keeps():
    _set_local_slot("http://10.0.0.5:1234", LOCAL_SAVED)
    settings.set_many({"local_base_url": "http://10.0.0.5:1234/v1"})
    settings.invalidate_cache()
    assert settings.get("local_api_key") == LOCAL_SAVED, "same-origin path edit cleared the key"
    settings.set_many({"local_base_url": "http://10.0.0.6:1234", "local_api_key": NEW_KEY})
    settings.invalidate_cache()
    assert settings.get("local_api_key") == NEW_KEY, "a key supplied with the move was dropped"


def test_vendor_endpoint_move_clears_secret():
    with Session(db_engine) as s:
        v = vendors.create(
            s,
            "repoint-vendor",
            endpoint_url="https://intake.vendor.example/hook",
            secret=dict(VENDOR_SECRET),
        )
        vendors.update(s, v.id, endpoint_url="https://intake.vendor.example/other")
        assert vendors.secret_dict(s.get(type(v), v.id)) == VENDOR_SECRET, "same origin cleared"
        vendors.update(
            s,
            v.id,
            endpoint_url="https://intake.vendor.example:8443/hook",
            actor="admin@repoint.test",
        )
        assert not vendors.secret_dict(s.get(type(v), v.id)), "vendor secret followed the move"
        rows = _audit_rows("vendor.destination_changed", f"vendor {v.id} ({v.name})")
        assert len(rows) == 1 and "credential_cleared=True" in rows[0].detail, rows
        assert "vendor-admin-token" not in rows[0].detail


# --- gate round 1 -----------------------------------------------------------------------
SLOT_URL = "http://100.64.0.5:1234"
SLOT_KEY = "sk-SLOT-ADMIN-SECRET"  # pregate: allow-secret offline fixture
PROF_KEY = "sk-PROFILE-ADMIN-SECRET"  # pregate: allow-secret offline fixture


_REAL_CLIENT_GET = httpx.Client.get


def _record_models_get(sent):
    """Replace the model-list transport with a recorder of (url, bearer): httpx.get (the old
    unpinned call) and, since #354, the local door's own httpx.Client for the same /models read.
    Undo with _restore_models_get(<the returned value>)."""
    original = httpx.get

    def fake_get(url, headers=None, **kw):
        sent.append((str(url), (headers or {}).get("Authorization", "")))
        return httpx.Response(200, json={"data": [{"id": "m"}]}, request=httpx.Request("GET", url))

    def fake_client_get(self, url, *, headers=None, **kw):
        if str(url).endswith("/models") and str(url).startswith("http"):
            return fake_get(url, headers)
        return _REAL_CLIENT_GET(self, url, headers=headers, **kw)

    httpx.get = fake_get
    httpx.Client.get = fake_client_get
    return original


def _restore_models_get(original):
    httpx.get = original
    httpx.Client.get = _REAL_CLIENT_GET


def _slot_key_at(sent, origin):
    return [u for u, auth in sent if u.startswith(origin) and SLOT_KEY in auth]


def test_gate_slot_key_never_follows_a_profile_off_the_slot():
    """Gate round 1 HIGH-1 (probe_gate_slot_fallback.py). A: an ADMIN's new keyless
    openai_compatible engine at their host. B: an admin-keyed `local` engine the ADMIN
    repoints (the #218 rule clears its key). C: the translation-time provider for B. In
    every case the slot key must not reach the attacker origin: the recorded outbound
    Authorization header carries the `lm-studio` placeholder instead. A failed on base;
    B and C failed on the round-1 tip."""
    _set_local_slot(SLOT_URL, SLOT_KEY)
    lin = _login(Role.ADMIN)
    sent: list = []
    original = _record_models_get(sent)
    try:
        r = lin.post(
            "/setup-ai/engines",
            data={"name": "gate-a", "kind": "openai_compatible", "base_url": ATTACKER},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        mine = engines.get_by_name("gate-a")
        assert lin.post("/workflows/models", data={"provider": f"engine:{mine.id}"}).status_code
        assert sent and sent[-1][0].startswith(ATTACKER), sent
        assert not _slot_key_at(sent, ATTACKER), f"A: slot key sent to the attacker: {sent}"
        assert sent[-1][1] == "Bearer lm-studio", sent[-1]

        sent.clear()
        adm = engines.create_profile(
            "gate-b", "local", base_url=SLOT_URL, api_key=PROF_KEY, actor="admin"
        )
        r = _web_update(lin, adm, ATTACKER)
        assert r.status_code == 303 and not engines.get_profile(adm.id).api_key_enc
        lin.post("/workflows/models", data={"provider": f"engine:{adm.id}"})
        assert sent and sent[-1][0].startswith(ATTACKER), sent
        assert not _slot_key_at(sent, ATTACKER), f"B: slot key sent to the attacker: {sent}"
        assert PROF_KEY not in repr(sent)
    finally:
        _restore_models_get(original)

    from app.services.providers_hub import _provider_for_profile

    seen: list = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization", "")))
        return httpx.Response(200, json={"object": "list", "data": []})

    provider = _provider_for_profile(engines.get_profile(adm.id))
    provider._transport = httpx.MockTransport(handler)
    provider._client().models.list()
    assert seen and seen[0][0].startswith(ATTACKER), seen
    assert not _slot_key_at(seen, ATTACKER), f"C: slot key on the translation path: {seen}"
    assert seen[0][1] == "Bearer lm-studio", seen


def test_gate_slot_key_still_reaches_the_slot():
    """The pin must not break the slot itself: a blank-URL local profile, and one whose
    own URL has the slot's origin, still send the slot key to the slot."""
    _set_local_slot(SLOT_URL, SLOT_KEY)
    blank = engines.create_profile("gate-slot-blank", "local")
    same = engines.create_profile("gate-slot-same", "local", base_url=SLOT_URL + "/v1")
    sent: list = []
    original = _record_models_get(sent)
    try:
        for prof in (blank, same):
            sent.clear()
            _login(Role.LINGUIST).post("/workflows/models", data={"provider": f"engine:{prof.id}"})
            assert sent and sent[-1][0].startswith(SLOT_URL), (prof.name, sent)
            assert sent[-1][1] == f"Bearer {SLOT_KEY}", (prof.name, sent)
    finally:
        _restore_models_get(original)


def test_gate_cross_process_slot_move_drops_the_profile_memo():
    """Gate round 1 LOW-3. Another process adopts a moved slot URL only through
    settings.apply_to_config(); at that moment its engine-profile memo must go, or it
    serves the pre-move row (with the key the same commit cleared) for up to the TTL.
    Simulated with raw SQL, which fires no ORM events, as another process's commit."""
    from sqlalchemy import text

    _set_local_slot(SLOT_URL, SLOT_KEY)
    prof = engines.create_profile("gate-memo", "local", api_key=PROF_KEY)
    assert engines.get_profile(prof.id).api_key_enc  # primes the memo
    with db_engine.begin() as conn:
        conn.execute(
            text("UPDATE engineprofile SET api_key_enc = '' WHERE id = :i"), {"i": prof.id}
        )
        conn.execute(
            text("UPDATE setting SET value = :v WHERE key = 'local_base_url'"),
            {"v": "http://10.9.9.9:1234"},
        )
    settings.invalidate_cache()
    settings.apply_to_config()
    assert not engines.get_profile(prof.id).api_key_enc, "memo served the pre-move key"


def test_gate_cli_config_set_names_the_actor():
    """Gate round 1 LOW-2: an admin door's slot move is attributed to its caller."""
    from app.cli.__main__ import main

    _set_local_slot(SLOT_URL, SLOT_KEY)
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        # --yes: local_base_url is a security-sensitive key since fix-audit-settings (L4),
        # so without it `config set` asks to confirm and a non-interactive run aborts.
        assert main(["config", "set", "--yes", "local_base_url", "http://10.7.7.7:1234"]) == 0
    rows = _audit_rows("settings.local_engine_destination_changed", "setting:local_base_url")
    assert rows and rows[-1].actor == "cli", [r.actor for r in rows]
    assert "http://10.7.7.7:1234" in rows[-1].detail


# --- gate round 2 -----------------------------------------------------------------------
UNREADABLE_SLOT = "http://100.64.0.5:+1234"  # httpx reaches :1234; urlsplit refuses
UNREADABLE_ATT = "https://collector.attacker.example:+443"
SEC_JIRA = {
    "email": "admin@corp.example",
    "api_token": "JIRA-ADMIN",  # pregate: allow-secret offline fixture
}


def _plant(sql, **params):
    """Write a row the way a pre-fix install (or a raw DB edit) left it: no ORM events,
    no save door. Drops the engine memo and the settings cache afterwards."""
    from sqlalchemy import text

    with db_engine.begin() as conn:
        conn.execute(text(sql), params)
    engines.invalidate_profile_cache()
    settings.invalidate_cache()
    settings.apply_to_config()


def test_gate2_doors_refuse_an_unreadable_origin():
    """Gate round 2 MEDIUM-1 (probe_empty_origin_repoint.py D1/D2, as an ADMIN): every
    save door refuses a URL whose origin the pin cannot read, so nothing is stored and no
    key moves. On the round-1 tip both saves succeeded and the key reached the attacker."""
    from app.services.connectors import store
    from app.services.connectors.base import ConnectorError

    _set_local_slot(SLOT_URL, SLOT_KEY)
    lin = _login(Role.ADMIN)
    sent: list = []
    original = _record_models_get(sent)
    try:
        # D2: repoint an admin-keyed local engine to the unreadable attacker URL.
        adm = engines.create_profile(
            "gate2-d2", "local", base_url=SLOT_URL, api_key=PROF_KEY, actor="admin"
        )
        r = _web_update(lin, adm, UNREADABLE_ATT)
        assert r.status_code == 303 and "engine_error=" in r.headers["location"], r.headers
        after = engines.get_profile(adm.id)
        assert after.base_url == SLOT_URL and engines.dec_key(after.api_key_enc) == PROF_KEY
        # D1: move the slot to the unreadable attacker URL with no key.
        r = lin.post(
            "/setup-ai/local",
            data={"base_url": UNREADABLE_ATT, "api_key": "", "local_model": "m"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "key_error=" in r.headers["location"], r.headers
        settings.invalidate_cache()
        assert settings.get("local_base_url") == SLOT_URL
        lin.post("/workflows/models", data={"provider": "local"})
        lin.post("/workflows/models", data={"provider": f"engine:{adm.id}"})
        assert not [u for u, _a in sent if "attacker" in u], sent
    finally:
        _restore_models_get(original)
    for bad in ("http://h:+1234", UNREADABLE_ATT):
        for call in (
            lambda bad=bad: engines.create_profile(
                f"gate2-{bad}", "openai_compatible", base_url=bad
            ),
            lambda bad=bad: settings.set_many({"local_base_url": bad}),
            lambda bad=bad: settings.validate_value("local_base_url", bad),
        ):
            try:
                call()
            except ValueError as exc:  # InvalidSettingValue is a ValueError too
                assert "could not be read" in str(exc), exc
            else:
                raise AssertionError(f"accepted an unreadable origin: {bad}")
    c = store.create("jira", "gate2", {"base_url": "https://acme.atlassian.net"}, {})
    try:
        store.update(c.id, "gate2", {"base_url": "https://acme.atlassian.net:+443"}, None)
        raise AssertionError("connector accepted an unreadable origin")
    except ConnectorError as exc:
        assert "could not be read" in str(exc), exc
    with Session(db_engine) as s:
        for kw in ({"endpoint_url": UNREADABLE_ATT}, {"config_json": {"poll_url": UNREADABLE_ATT}}):
            try:
                vendors.create(s, f"gate2-vendor-{len(kw)}-{sorted(kw)[0]}", **kw)
                raise AssertionError(f"vendor accepted an unreadable origin: {kw}")
            except vendors.VendorError as exc:
                assert "could not be read" in str(exc), exc


def test_gate2_grandfathered_unreadable_rows_fail_closed():
    """Gate round 2 MEDIUM-1, the comparison half: rows stored with an unreadable origin
    before the door existed. Repointing them clears the key and writes the audit row
    (naming the old origin as unreadable), and no comparison treats "" == "" as the same
    server."""
    from app.services.connectors import store
    from app.services.connectors.store_part2 import _pin_secret_to_host
    from app.services.credential_origin import same_origin, slot_key_for

    lin = _login(Role.ADMIN)
    # D2 grandfathered: a keyed local profile stored at the unreadable slot URL.
    adm = engines.create_profile(
        "gate2-gf", "local", base_url=SLOT_URL, api_key=PROF_KEY, actor="admin"
    )
    _plant("UPDATE engineprofile SET base_url = :u WHERE id = :i", u=UNREADABLE_SLOT, i=adm.id)
    r = _web_update(lin, engines.get_profile(adm.id), ATTACKER)
    assert r.status_code == 303, r.status_code
    assert engines.dec_key(engines.get_profile(adm.id).api_key_enc) == "", "key kept"
    rows = _audit_rows("engine.destination_changed", f"engine:{adm.id}")
    assert rows and f"(unreadable) -> {ATTACKER}" in rows[-1].detail, [r.detail for r in rows]
    assert "credential_cleared=True" in rows[-1].detail
    # D1 grandfathered: the slot itself stored at an unreadable URL with the admin key.
    _set_local_slot(SLOT_URL, SLOT_KEY)
    _plant("UPDATE setting SET value = :u WHERE key = 'local_base_url'", u=UNREADABLE_SLOT)
    assert slot_key_for(UNREADABLE_ATT) == "lm-studio", "unreadable slot matched unreadable URL"
    r = lin.post(
        "/setup-ai/local",
        data={"base_url": ATTACKER, "api_key": "", "local_model": "m"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    settings.invalidate_cache()
    assert settings.get("local_api_key") != SLOT_KEY, "slot key kept"
    rows = _audit_rows("settings.local_engine_destination_changed", "setting:local_base_url")
    assert f"(unreadable) -> {ATTACKER}" in rows[-1].detail, rows[-1].detail
    # The shared comparison, and the connector pin, fail closed on two unreadable origins.
    assert not same_origin(UNREADABLE_SLOT, UNREADABLE_ATT)
    assert not same_origin("https://good.example:+443", "https://evil.example:+443")
    assert same_origin("", "") and same_origin("https://h", "https://H:443/x")
    c = store.create("jira", "gate2-conn", {"base_url": "https://acme.atlassian.net"}, SEC_JIRA)
    conn = store.get(c.id)
    _o, _n, changed, dropped = _pin_secret_to_host(
        conn, {"base_url": "https://good.example:+443"}, {"base_url": "https://evil.example:+443"}
    )
    assert changed and dropped, "connector pin treated two unreadable origins as the same"


def test_gate2_apply_to_config_never_pairs_new_url_with_old_key():
    """Gate round 2 LOW-1 (probe_apply_window.py): at EVERY assignment apply_to_config
    makes to the slot, a racing reader must never send the old slot key or a memoized
    profile key to the new URL. The reader runs from a hook on the config module."""
    import types

    from app import config
    from app.services import providers_hub

    _set_local_slot(SLOT_URL, SLOT_KEY)
    blank = engines.create_profile("gate2-window", "local", api_key=PROF_KEY, actor="admin")
    seen: list = []
    sent: list = []
    original_get = _record_models_get(sent)

    class Hooked(types.ModuleType):
        armed = False

        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if Hooked.armed and name in ("LOCAL_BASE_URL", "LOCAL_API_KEY"):
                Hooked.armed = False
                try:
                    providers_hub.list_models_for_ref("local")
                    p = providers_hub._provider_for_profile(engines.get_profile(blank.id))
                    seen.append((name, p._profile_base, p._profile_key))
                finally:
                    Hooked.armed = True

    original_cls = config.__class__
    config.__class__ = Hooked
    try:
        engines.get_profile(blank.id)  # memo primed with the keyed row
        Hooked.armed = True
        settings.set_many({"local_base_url": ATTACKER}, actor="linguist")  # W1
        Hooked.armed = False
        _set_local_slot(SLOT_URL, SLOT_KEY)
        engines.update_profile(blank.id, api_key=PROF_KEY, actor="admin")
        engines.get_profile(blank.id)
        from sqlalchemy import text

        with db_engine.begin() as conn:  # W2: another process's commit
            conn.execute(
                text("UPDATE engineprofile SET api_key_enc = '' WHERE id = :i"), {"i": blank.id}
            )
            conn.execute(
                text("UPDATE setting SET value = :v WHERE key = 'local_base_url'"), {"v": ATTACKER}
            )
            conn.execute(text("UPDATE setting SET value = '' WHERE key = 'local_api_key'"))
        settings.invalidate_cache()
        Hooked.armed = True
        settings.apply_to_config()
    finally:
        Hooked.armed = False
        config.__class__ = original_cls
        _restore_models_get(original_get)
    assert seen, "the hook never ran"
    assert not _slot_key_at(sent, ATTACKER), f"slot key paired with the new URL: {sent}"
    leaked = [s for s in seen if s[1] and ATTACKER in s[1] and s[2] in (SLOT_KEY, PROF_KEY)]
    assert not leaked, f"a key paired with the new URL: {leaked}"


def test_gate3_admin_rekey_during_engine_validation():
    """R1: the admin repair commits while the attacker's DNS check is in flight."""
    from unittest.mock import patch

    good = "https://llm.good.example"
    prof = engines.create_profile("gate3-race", "openai_compatible", base_url=ATTACKER)
    original = engines._validate_base_url
    fired = []

    def validate(url, **kwargs):
        result = original(url, **kwargs)
        if url == ATTACKER + "/v1" and not fired:
            fired.append(True)
            engines.update_profile(prof.id, base_url=good, api_key=NEW_KEY, actor="admin")
        return result

    refused = False
    with patch.object(engines, "_validate_base_url", validate):
        try:
            engines.update_profile(prof.id, base_url=ATTACKER + "/v1", actor="linguist")
        except ValueError as exc:
            assert "reload" in str(exc).lower(), exc
            refused = True
    assert fired, "the concurrent admin repair never ran"
    engines.invalidate_profile_cache()
    after = engines.get_profile(prof.id)
    if refused:
        assert after.base_url == good and engines.dec_key(after.api_key_enc) == NEW_KEY
    else:
        assert not after.api_key_enc, "the concurrent admin key followed the attacker URL"
        rows = _audit_rows("engine.destination_changed", f"engine:{prof.id}")
        assert rows[-1].actor == "linguist" and "credential_cleared=True" in rows[-1].detail


def test_connection_test_blank_key_never_leaks_saved_slot():
    """#389: a caller running the legacy local connection test with a blank key field must
    NOT get the saved slot key back when the engine echoes it (a LINGUIST is refused since
    #375; an ADMIN reaches the probe and must get ok:false with no key in the body). This
    drives the REAL route (blank field -> _local_probe_key -> saved key -> local_complete)
    and the REAL guard_answer inside local_complete; local_complete itself is NOT stubbed."""
    import app.services.providers_part3 as p3
    from app import config

    base = "http://10.0.0.5:1234"
    _set_local_slot(base, LOCAL_SAVED)
    assert config.LOCAL_API_KEY == LOCAL_SAVED, "saved slot key not mirrored to config"

    def handler(request):
        auth = request.headers.get("authorization", "")
        tok = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Bonjour " + tok},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    real = p3._local_client

    def patched(base_url=None, api_key=None, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(base_url, api_key, **kw)

    # #375 (merged after this test was written) made the route administrator-only, so a
    # LINGUIST is now refused outright; the echo guard is exercised through an ADMIN, who
    # must still never see the saved key in the 200 body.
    lin = _login(Role.LINGUIST)
    admin = _login(Role.ADMIN)
    p3._local_client = patched
    try:
        denied = lin.post(
            "/setup-ai/local/test",
            data={"base_url": base, "api_key": "", "local_model": "m"},
        )
        r = admin.post(
            "/setup-ai/local/test",
            data={"base_url": base, "api_key": "", "local_model": "m"},
        )
    finally:
        p3._local_client = real
    assert denied.status_code == 403, denied.status_code
    assert LOCAL_SAVED not in denied.text, "the saved slot key leaked in the refusal body"
    assert r.status_code == 200, r.status_code
    assert r.json().get("ok") is False, f"echoed key accepted as a pass: {r.text}"
    assert LOCAL_SAVED not in r.text, "the saved slot key leaked in the connection-test body"


if __name__ == "__main__":
    failed = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failed.append(name)
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    assert not failed, f"failed cases: {failed}"
    print("ENGINE REPOINT OK")
