"""Feature-flag admin surfaces: the /api door, the CLI write verb, and the MCP exclusion.

WHY THIS EXISTS. Feature flags had two INTENDED write doors -- the Settings > Features
card (session-auth, a browser) and `config set <key> 0|1` (require_local, the host). A
headless or remote operator had no NAMED door and no refusal vocabulary, which is how a
prod install ended up with a feature that could only be sanctioned-flipped from a
browser. (A flag COULD be written over the wire by spelling its raw setting key into
POST /api/admin/config -- an unnamed second door that check [9] now closes.) This train
adds GET/POST /api/admin/features(/{slug}) plus `config feature enable|disable <slug>`
and routes all three surfaces (web form included) through ONE seam,
app/services/features_admin.py.

What this pins, each as a LAW that fails if the behaviour is reverted:

  [1] SCOPE TIER, on BOTH routes: no token -> 401; a `docs`-scope key EVEN WITH the
      admin role -> 403 on SCOPE (the 2026-08-27 F3 docs-scope-privilege precedent: the
      weak scope must never reach an install-global config mutation); a below-role docs
      key -> 403 on scope too (scope is checked BEFORE role); an admin-scope key with
      remote_admin_enabled OFF -> 404, non-enumerable; with it ON -> 200.
  [2] REFUSALS: unknown slug 404 ("not found"); a door-moved feature (scim/saml, whose
      switch moved to /settings/enterprise) 409 naming that door; an INSTALL-LOCKED
      feature key 400 "LOCAL-ONLY" -- the load-bearing one: without it the new door
      would be a straight bypass of the REMOTE_IMMUTABLE law POST /api/admin/config
      enforces; a non-bool `enabled` ("1", "true", 1, null, absent) 400.
  [3] AUDIT: a real change writes exactly ONE feature.toggle row (target=slug,
      detail="=1"/"=0", the caller's actor); a SAME-STATE write writes NOTHING and
      audits NOTHING and reports changed=false.
  [4] PARITY: the web form and the API land the SAME stored value and the SAME audit
      row shape for the same input, and the web form's own refusal ORDER is unchanged
      (unknown 404; door-moved 303 EVEN with a bad `enabled`; bad `enabled` 400).
  [5] THE DOOR IS NEVER BEHIND A FEATURE: with EVERY flag off, both routes still answer
      200 (kill-switch law -- you must always be able to turn things back on), and the
      route table classifies /api/admin/features as CORE, not as any feature's.
  [6] CLI: local and --remote produce byte-identical --json for both the list and the
      flip; the local arm still reaches an install-locked key (a host write) while the
      remote arm refuses it.
  [7] MCP EXCLUSION, deliberate and tested: both routes are in NEVER_COVER, no tool
      declares them, no tool mentions the feature-flag seam, and the pinned tool count
      is unchanged. Flags belong to the registry's excluded class (config/credentials/
      identity/retention) -- an agent driving the install must not be able to change
      what the install is ALLOWED to do.
  [8] RATCHET: every feature whose setting_key is SENSITIVE is either door-moved or
      install-locked, and every DOOR_MOVED feature's key is install-locked. The first
      makes "no confirm prompt on this verb" a checked fact instead of a hole; the
      second means a switch whose door deliberately moved is backed by the host-only
      law, not by one refusal alone.
  [9] THE GENERIC CONFIG DOOR IS CLOSED TO FLAGS. A feature setting_key is an ordinary
      DEFAULTS key, so POST /api/admin/config was a SECOND, unnamed wire flag door --
      and an MCP one, since manage_install(set_config) forwards to it. A gate probe
      flipped 15 of 19 flags that way, recorded as `config.set`, while six doc sites
      claimed MCP could not touch flags. The shared spine now refuses every registry
      feature key (derived live, never hand-listed): the 7-flag probe and all 19 are
      400 with nothing written and nothing audited, a non-feature key still writes, a
      mixed body is refused whole, and the READ is untouched. Host-local `config set`
      is deliberately unchanged and still writes its own `config.set` row.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, no provider calls, the --remote arm
rides an httpx.MockTransport into the in-process TestClient). ASCII-only.
Final line: "FEATURE FLAG ADMIN OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_flagadmin_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import features
from app.cli import _remote
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import AuditLog, Organization, Role, User
from app.services import apikeys, features_admin, security, settings
from tests._csrf import csrf_for

PW = "flagadmin-pw-123456"
_SCOPE_MSG = "This endpoint requires an admin-scoped user API key."
_NOT_FOUND = "not found"
#: The two routes this train adds. Both must clear the same three locks.
_LIST = "/api/admin/features"
#: A default-ON "removed"-semantics kill switch with a plain (non-locked) key -- the
#: ordinary case every assertion below uses unless it is testing a special one.
_SLUG = "reports"
#: An install-locked feature key (settings.REMOTE_IMMUTABLE_KEYS): host-only forever.
_LOCKED_SLUG = "data_export"
#: A feature whose SWITCH moved to /settings/enterprise (still in the registry).
_MOVED_SLUG = "saml"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _is(r, code, what) -> None:
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:250]})"


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _remote_admin(on: bool) -> None:
    settings.set_many({"remote_admin_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _flag(slug: str, on: bool) -> None:
    settings.set_many({features.feature_by_slug(slug).setting_key: "1" if on else "0"})
    settings.invalidate_cache()


def _reads(slug: str) -> bool:
    settings.invalidate_cache()
    return features.feature_by_slug(slug).is_enabled()


def _audit_rows(action="feature.toggle") -> list:
    with Session(engine) as s:
        return list(s.exec(select(AuditLog).where(AuditLog.action == action)).all())


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _run_json(argv):
    rc, out = _run(["--json", *argv])
    return rc, (_json.loads(out) if out.strip() else {})


def _mock_transport(client):
    """httpx.MockTransport forwarding the CLI's --remote arm into the in-process
    TestClient, so `--remote` really crosses the HTTP boundary with no network."""
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


def _seed() -> dict:
    init_db()
    with Session(engine) as s:
        org = Organization(name="Flag Org")
        s.add(org)
        s.commit()
        s.refresh(org)
        oid = org.id

    def _user(email, role):
        u = security.create_user(email, email, role, PW)
        with Session(engine) as s:
            row = s.get(User, u.id)
            row.org_id = oid
            s.add(row)
            s.commit()
        return u.id

    admin_id = _user("flagadmin@x.com", Role.ADMIN)
    tr_id = _user("flagtr@x.com", Role.TRANSLATOR)

    def _key(uid, scopes):
        _row, raw = apikeys.create_key(uid, "flagadmin-test", scopes=scopes)
        return raw

    return {
        "admin_scope": _key(admin_id, "api docs admin"),
        "admin_docs": _key(admin_id, "api docs"),
        "translator_docs": _key(tr_id, "api docs"),
        "admin_email": "flagadmin@x.com",
    }


def _web_client(email="flagadmin@x.com"):
    """An admin session client for the Settings > Features form."""
    from app.main import app

    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code, r.text[:200])
    csrf_for(c)
    return c


# --------------------------------------------------------------------------- #
# [1] the three locks, on BOTH routes
# --------------------------------------------------------------------------- #
def test_scope_tier(keys, api) -> None:
    admin_scope, admin_docs = _h(keys["admin_scope"]), _h(keys["admin_docs"])
    tr_docs = _h(keys["translator_docs"])
    probes = [
        ("GET", _LIST, None),
        ("POST", f"{_LIST}/{_SLUG}", {"enabled": True}),
    ]

    def _probe(method, path, body, headers):
        if method == "GET":
            return api.get(path, headers=headers)
        return api.post(path, headers=headers, json=body)

    for method, path, body in probes:

        def call(h, _m=method, _p=path, _b=body):
            return _probe(_m, _p, _b, h)

        # No credential at all -> 401 (the auth wall, before any scope talk).
        r = call({})
        _is(r, 401, f"{method} {path}: tokenless")

        _remote_admin(False)
        # A docs-scope key WITH the admin role -> 403 on SCOPE. This is the F3
        # docs-scope-privilege law: the weakest scope may not reach install-global config.
        r = call(admin_docs)
        _is(r, 403, f"{method} {path}: admin-ROLE docs key")
        assert r.json().get("error") == _SCOPE_MSG, r.text
        # A below-role docs key fails the SAME way -> scope is checked before role.
        r = call(tr_docs)
        _is(r, 403, f"{method} {path}: translator docs key")
        assert r.json().get("error") == _SCOPE_MSG, r.text
        # admin scope, switch OFF -> 404 (non-enumerable), never a 200 write.
        r = call(admin_scope)
        _is(r, 404, f"{method} {path}: admin-scope, remote-admin off")
        assert r.json().get("error") == _NOT_FOUND, r.text
        # admin scope, switch ON -> served.
        _remote_admin(True)
        r = call(admin_scope)
        _is(r, 200, f"{method} {path}: admin-scope, remote-admin on")
    print(
        "  [1] both routes: 401 tokenless; 403 on SCOPE for a docs key (admin ROLE and "
        "translator alike); 404 with remote_admin off; 200 for admin scope + switch on"
    )


# --------------------------------------------------------------------------- #
# [2] refusals
# --------------------------------------------------------------------------- #
def test_refusals(keys, api) -> None:
    h = _h(keys["admin_scope"])
    _remote_admin(True)

    r = api.post(f"{_LIST}/no_such_feature", headers=h, json={"enabled": True})
    _is(r, 404, "unknown slug")
    assert r.json().get("error") == _NOT_FOUND, r.text

    # Door moved: the feature is REAL, so it is not a 404 -- it is a 409 that says where.
    before = _reads(_MOVED_SLUG)
    r = api.post(f"{_LIST}/{_MOVED_SLUG}", headers=h, json={"enabled": True})
    _is(r, 409, "door-moved slug")
    assert "/settings/enterprise" in r.json().get("error", ""), r.text
    assert _reads(_MOVED_SLUG) is before, "a door-moved refusal must write nothing"

    # THE LOAD-BEARING ONE. data_export_enabled is on settings.REMOTE_IMMUTABLE_KEYS;
    # POST /api/admin/config refuses it outright, so this door must refuse it too or it
    # is a bypass of the immutable-key law.
    assert features.feature_by_slug(_LOCKED_SLUG).setting_key in settings.REMOTE_IMMUTABLE_KEYS, (
        "fixture drift: the locked-slug fixture must name an install-locked setting key"
    )
    before = _reads(_LOCKED_SLUG)
    r = api.post(f"{_LIST}/{_LOCKED_SLUG}", headers=h, json={"enabled": not before})
    _is(r, 400, "install-locked slug")
    assert "LOCAL-ONLY" in r.json().get("error", ""), r.text
    assert _reads(_LOCKED_SLUG) is before, "an install-locked refusal must write nothing"
    # ...and the SAME refusal really is what POST /api/admin/config gives for that key,
    # so the two doors agree rather than merely both saying no.
    r = api.post(
        "/api/admin/config",
        headers=h,
        json={"settings": {features.feature_by_slug(_LOCKED_SLUG).setting_key: "1"}},
    )
    _is(r, 400, "config route refuses the same locked key")
    assert "LOCAL-ONLY" in r.json().get("error", ""), r.text

    # A flag is true or false, never a spelling of one.
    before = _reads(_SLUG)
    for bad in ({"enabled": "1"}, {"enabled": "true"}, {"enabled": 1}, {"enabled": None}, {}):
        r = api.post(f"{_LIST}/{_SLUG}", headers=h, json=bad)
        _is(r, 400, f"non-bool enabled {bad!r}")
        assert r.json().get("error") == "enabled must be true or false.", r.text
    assert _reads(_SLUG) is before, "a non-bool refusal must write nothing"
    print(
        "  [2] refusals: unknown 404; door-moved 409 naming /settings/enterprise; "
        "install-locked 400 LOCAL-ONLY (the same refusal POST /api/admin/config gives); "
        "non-bool enabled 400 -- and every refusal writes nothing"
    )


# --------------------------------------------------------------------------- #
# [3] audit rows + no-op semantics
# --------------------------------------------------------------------------- #
def test_audit_and_noop(keys, api) -> None:
    h = _h(keys["admin_scope"])
    _remote_admin(True)
    _flag(_SLUG, True)

    before = len(_audit_rows())
    r = api.post(f"{_LIST}/{_SLUG}", headers=h, json={"enabled": False})
    _is(r, 200, "real change")
    body = r.json()
    assert body["changed"] is True and body["enabled"] is False, body
    assert _reads(_SLUG) is False, "the flag must actually be off"
    rows = _audit_rows()
    assert len(rows) == before + 1, ("exactly one audit row per real change", before, len(rows))
    row = rows[-1]
    assert (row.action, row.target, row.detail) == ("feature.toggle", _SLUG, "=0"), (
        row.action,
        row.target,
        row.detail,
    )
    assert row.actor == keys["admin_email"], row.actor

    # Same-state write: honest no-op. No write, NO audit row (an unchanged toggle is not
    # a forensics event), and the caller is told so.
    before = len(_audit_rows())
    r = api.post(f"{_LIST}/{_SLUG}", headers=h, json={"enabled": False})
    _is(r, 200, "same-state write")
    assert r.json()["changed"] is False, r.text
    assert len(_audit_rows()) == before, "a no-op must not write an audit row"
    _flag(_SLUG, True)
    print(
        "  [3] one feature.toggle row per real change (actor/target/detail pinned); a "
        "same-state write is a no-op: nothing written, nothing audited, changed=false"
    )


# --------------------------------------------------------------------------- #
# [4] web-form parity + the web form's unchanged refusal order
# --------------------------------------------------------------------------- #
def test_web_form_parity(keys, api) -> None:
    h = _h(keys["admin_scope"])
    _remote_admin(True)
    _auth(True)
    try:
        web = _web_client()
        # The SAME input through both doors lands the same stored value...
        for value, enabled in (("0", False), ("1", True)):
            _flag(_SLUG, not enabled)
            r = web.post(
                f"/settings/features/{_SLUG}", data={"enabled": value}, follow_redirects=False
            )
            _is(r, 303, "web form flip")
            assert r.headers["location"] == "/settings?saved=1#features", r.headers
            web_state = _reads(_SLUG)

            _flag(_SLUG, not enabled)
            r = api.post(f"{_LIST}/{_SLUG}", headers=h, json={"enabled": enabled})
            _is(r, 200, "api flip")
            assert _reads(_SLUG) is web_state is enabled, (web_state, enabled)

        # ...and the same audit row SHAPE (the two surfaces call the same seam).
        _flag(_SLUG, True)
        n = len(_audit_rows())
        web.post(f"/settings/features/{_SLUG}", data={"enabled": "0"}, follow_redirects=False)
        web_row = _audit_rows()[-1]
        assert len(_audit_rows()) == n + 1, "web form writes exactly one row"
        _flag(_SLUG, True)
        api.post(f"{_LIST}/{_SLUG}", headers=h, json={"enabled": False})
        api_row = _audit_rows()[-1]
        assert (web_row.action, web_row.target, web_row.detail) == (
            api_row.action,
            api_row.target,
            api_row.detail,
        ), (web_row.action, web_row.target, web_row.detail, api_row.detail)

        # The web form's REFUSAL ORDER is unchanged by the extraction. An unknown slug
        # 404s; a door-moved slug REDIRECTS to its door EVEN with a bogus `enabled`
        # (the door check runs BEFORE the "0"/"1" form check, as it always has); only
        # then does a bad value 400.
        r = web.post("/settings/features/nope", data={"enabled": "1"}, follow_redirects=False)
        _is(r, 404, "web form unknown slug")
        r = web.post(
            f"/settings/features/{_MOVED_SLUG}", data={"enabled": "bogus"}, follow_redirects=False
        )
        _is(r, 303, "web form door-moved with a bad value still redirects")
        assert r.headers["location"] == f"/settings/enterprise#feature-{_MOVED_SLUG}", r.headers
        r = web.post(
            f"/settings/features/{_SLUG}", data={"enabled": "bogus"}, follow_redirects=False
        )
        _is(r, 400, "web form bad value")
        # The web form is a LOCAL door: it still flips an install-locked key, which the
        # remote door refuses. (Same seam, different `remote` posture -- not an accident.)
        locked_before = _reads(_LOCKED_SLUG)
        r = web.post(
            f"/settings/features/{_LOCKED_SLUG}",
            data={"enabled": "0" if locked_before else "1"},
            follow_redirects=False,
        )
        _is(r, 303, "web form still flips an install-locked key")
        assert _reads(_LOCKED_SLUG) is (not locked_before), "the local door must still write"
        _flag(_LOCKED_SLUG, locked_before)
    finally:
        _auth(False)
    _flag(_SLUG, True)
    print(
        "  [4] web form and API land identical state + identical audit-row shape; the "
        "form's refusal order (404 / door-303 even on a bad value / 400) is unchanged; "
        "the LOCAL door still writes an install-locked key the remote door refuses"
    )


# --------------------------------------------------------------------------- #
# [5] the door is never behind a feature
# --------------------------------------------------------------------------- #
def test_never_feature_gated(keys, api) -> None:
    h = _h(keys["admin_scope"])
    _remote_admin(True)
    saved = {f.slug: f.is_enabled() for f in features.FEATURES}
    try:
        settings.set_many({f.setting_key: "0" for f in features.FEATURES})
        settings.invalidate_cache()
        r = api.get(_LIST, headers=h)
        _is(r, 200, "list with EVERY flag off")
        assert not any(row["enabled"] for row in r.json()["features"]), r.text[:200]
        # The middleware's own question, asked WHILE everything is genuinely off -- which
        # is HERE, before the re-enable below (outside this block path_is_off is vacuously
        # False for EVERY path, and after the flip `reports` is on again). Paired with a
        # positive control in the same state, so a False answer means "this path is not
        # off" rather than "this function is answering False to everything": /reports is a
        # removed-semantics default-ON feature path and must read True right now.
        assert features.path_is_off("/reports"), (
            "positive control: with every flag off, a removed feature's own web path "
            "must read as off -- otherwise the negative assertions below prove nothing"
        )
        for path in (_LIST, f"{_LIST}/{{slug}}"):
            assert not features.path_is_off(path), (
                f"{path} must never be middleware-404'd: the door that turns features "
                "off may not be switched off by a feature"
            )
        # ...and the write door still opens, which is the whole point: you must always be
        # able to turn things back ON.
        r = api.post(f"{_LIST}/{_SLUG}", headers=h, json={"enabled": True})
        _is(r, 200, "flip with EVERY flag off")
        assert r.json()["changed"] is True and _reads(_SLUG) is True, r.text
    finally:
        settings.set_many(
            {
                features.feature_by_slug(s).setting_key: ("1" if on else "0")
                for s, on in saved.items()
            }
        )
        settings.invalidate_cache()

    # Structurally, too: the route table classifies it as CORE, owned by the always-on
    # /api/admin prefix -- not claimed by any feature's api patterns.
    assert features.api_owner(_LIST) == ("core", "/api/admin"), features.api_claimants(_LIST)
    assert features.api_owner(f"{_LIST}/{{slug}}") == ("core", "/api/admin"), (
        features.api_claimants(f"{_LIST}/{{slug}}")
    )
    print(
        "  [5] with EVERY flag off both routes still answer 200 and the flip still lands; "
        "the route table owns /api/admin/features as CORE, never as a feature"
    )


# --------------------------------------------------------------------------- #
# [6] CLI: local vs --remote
# --------------------------------------------------------------------------- #
def test_cli_local_and_remote(keys) -> None:
    from app.main import app

    _remote_admin(True)
    _flag(_SLUG, True)

    rc, probe = _run_json(["config", "features"])
    assert rc == 0 and probe["ok"] and probe["features"], probe
    rc, local_off = _run_json(["config", "feature", "disable", _SLUG])
    assert rc == 0 and local_off["enabled"] is False and local_off["changed"] is True, local_off
    rc, local_noop = _run_json(["config", "feature", "disable", _SLUG])
    assert rc == 0 and local_noop["changed"] is False, local_noop
    # A door-moved feature is refused on the LOCAL arm too, and this is the ONE probe
    # that isolates the door-moved refusal: over the wire scim/saml are ALSO install-
    # locked, so a remote probe would still refuse them with the immutable arm gone.
    moved_before = _reads(_MOVED_SLUG)
    rc, out = _run_json(["config", "feature", "enable", _MOVED_SLUG])
    assert rc == 1 and "/settings/enterprise" in out.get("error", ""), (rc, out)
    assert _reads(_MOVED_SLUG) is moved_before, "a door-moved refusal must write nothing"
    rc, out = _run_json(["config", "feature", "enable", "nope"])
    assert rc == 2, ("an unknown slug is a USAGE error on the local arm", rc, out)
    # The LOCAL arm is a host write: an install-locked key is still reachable here.
    locked_before = _reads(_LOCKED_SLUG)
    rc, out = _run_json(
        ["config", "feature", "disable" if locked_before else "enable", _LOCKED_SLUG]
    )
    assert rc == 0 and out["changed"] is True, out
    _flag(_LOCKED_SLUG, locked_before)

    _auth(True)
    try:
        client = TestClient(app)
        _remote._set_transport(_mock_transport(client))
        try:
            argv = ["--remote", "http://127.0.0.1", "--token", keys["admin_scope"]]
            # Re-read the LOCAL list here so both arms see the same install state (the
            # flips above moved it), then compare the two reads of that one state.
            rc, local_list = _run_json(["config", "features"])
            assert rc == 0, local_list
            rc, remote_list = _run_json([*argv, "config", "features"])
            assert rc == 0, remote_list
            # Same rows, same keys, same order -- transport-independent by construction.
            assert remote_list == local_list, (
                "config features must read identically local vs --remote",
                remote_list.get("features", [{}])[:1],
                local_list.get("features", [{}])[:1],
            )
            _flag(_SLUG, True)
            rc, remote_off = _run_json([*argv, "config", "feature", "disable", _SLUG])
            assert rc == 0 and remote_off == local_off, (remote_off, local_off)
            rc, remote_noop = _run_json([*argv, "config", "feature", "disable", _SLUG])
            assert rc == 0 and remote_noop == local_noop, (remote_noop, local_noop)
            # The REMOTE arm refuses the install-locked key the local arm just wrote, and
            # surfaces the server's own message rather than inventing one.
            rc, out = _run_json(
                [*argv, "config", "feature", "enable" if locked_before else "disable", _LOCKED_SLUG]
            )
            assert rc == 1 and "LOCAL-ONLY" in out.get("error", ""), (rc, out)
            assert _reads(_LOCKED_SLUG) is locked_before, "the refused remote flip wrote nothing"
            rc, out = _run_json([*argv, "config", "feature", "enable", _MOVED_SLUG])
            assert rc == 1 and "/settings/enterprise" in out.get("error", ""), (rc, out)
            rc, out = _run_json([*argv, "config", "feature", "enable", "nope"])
            assert rc == 1, (rc, out)
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    _flag(_SLUG, True)
    print(
        "  [6] CLI local and --remote agree byte-for-byte on the list, the flip and the "
        "no-op; the local arm writes an install-locked key, the remote arm refuses it"
    )


# --------------------------------------------------------------------------- #
# [7] MCP exclusion -- deliberate, not an omission
# --------------------------------------------------------------------------- #
def test_mcp_exclusion() -> None:
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import registry

    routes = [("GET", _LIST), ("POST", f"{_LIST}/{{slug}}")]
    for route in routes:
        assert route in registry.NEVER_COVER, (
            f"{route} must be in NEVER_COVER: a feature flag decides what the install is "
            "ALLOWED to do, which is the registry's excluded class"
        )
        assert route in registry.EXEMPT, f"{route} needs an EXEMPT entry (the accounting closes)"
    covered = {r for tool in registry.TOOLS for r in getattr(tool, "routes", ())}
    for route in routes:
        assert route not in covered, f"a tool claims {route}; feature flags stay off MCP"
    # Nothing on the MCP surface names the flag door by any spelling.
    blob = " ".join(
        [t.name for t in registry.TOOLS]
        + [str(getattr(t, "routes", ())) for t in registry.TOOLS]
        + [str(getattr(t, "description", "")) for t in registry.TOOLS]
    )
    for needle in ("/api/admin/features", "feature.toggle", "features_admin"):
        assert needle not in blob, f"an MCP tool mentions {needle!r}; the exclusion is not real"
    # And the ratchet: THIS train adds no tool. The number is the live registry count, not
    # this train's contribution -- so it moves whenever any LATER train legitimately adds one,
    # and the assertion above (no tool claims a flag route, and nothing on the tool surface
    # names the flag door) is what actually holds this train's exclusion. Bumped 75 -> 77 by
    # SPEC-quality-lenses-p2 T2's run_meta_evaluation, which is in the evaluations family and
    # touches no flag route. THIS IS THE TENTH COUNT SITE: SPEC-quality-lenses-p2 section 5.8
    # lists nine, written before the flag-API train added this one. Bumped 77 -> 78 by run-190
    # Item C's `ledger_summary` (workflows family, read tier, docs scope); it claims
    # GET /api/prompt-tap/actions[/{id}/summary], neither of which is a feature-flag route.
    # Bumped 78 -> 79 by the advanced-UX train's `advanced_templates` (workflows family, read tier,
    # docs scope); it claims GET /api/advanced/templates, which is not a feature-flag route either.
    # Bumped 79 -> 80 by the prompt-tap-by-run merge's `ledger_runs` (workflows family, read tier,
    # docs scope); it claims GET /api/prompt-tap/runs, which is not a feature-flag route either.
    # Bumped 80 -> 81 by P2a recovery's `release_manifest` (orientation family, read tier, docs
    # scope); it claims GET /api/release, which is not a feature-flag route either.
    # 2026-09-14: 81 -> 82 for #91's get_advanced_mission (3a685b1d); its sole
    # GET /api/advanced/missions/{mission_id} route claims no feature-flag admin route.
    # 2026-10-07: 82 -> 83 for #595's search_workspace (38f18c772, assets family); its sole
    # GET /api/search route is not a feature-flag route either.
    # 2026-10-14 (open-core S2): 89 -> 72 tools and 9 -> 7 families when the 17 tools of the paid
    # add-ons (LLMemory, workflow suggestions, Evaluations Lab, Advanced Workflows) left with them.
    assert (len(registry.TOOLS), len(registry.FAMILIES)) == (72, 7), (
        "the pinned MCP tool/family counts moved; if a later train added a tool, bump this "
        "line and confirm it claims no feature-flag route",
        len(registry.TOOLS),
        len(registry.FAMILIES),
    )
    print(
        "  [7] MCP: both routes in NEVER_COVER + EXEMPT, claimed by no tool, named "
        "nowhere on the tool surface, and the 85/9 count ratchet is unchanged"
    )


# --------------------------------------------------------------------------- #
# [9] the generic config door is CLOSED to feature keys (the gate's HIGH)
# --------------------------------------------------------------------------- #
def test_config_door_refuses_feature_keys(keys, api) -> None:
    """The second, unnamed wire door.

    Every feature setting_key is an ordinary DEFAULTS key, so POST /api/admin/config
    wrote it like any other setting -- and because MCP's manage_install(set_config)
    forwards to that route, the "not exposed over MCP" posture was documented but not
    enforced. The gate probe flipped 15 of 19 flags this way, recorded as `config.set`.
    Now the shared spine refuses every feature key and names the feature door."""
    h = _h(keys["admin_scope"])
    _remote_admin(True)

    # (a) The gate's own seven-flag probe: each must now be a 400 that names the door,
    # with the stored value untouched and NO audit row written.
    probe = [
        "reports",
        "advanced_workflows",
        "llmemory",
        "evaluations",
        "assist",
        "utcp",
        "worklist",
    ]
    for slug in probe:
        feat = features.feature_by_slug(slug)
        before, n_audit = _reads(slug), len(_audit_rows("config.set"))
        r = api.post(
            "/api/admin/config",
            headers=h,
            json={"settings": {feat.setting_key: "0" if before else "1"}, "confirm": True},
        )
        _is(r, 400, f"config door refuses {feat.setting_key}")
        err = r.json().get("error", "")
        assert "/api/admin/features/" in err and "feature.toggle" in err, (slug, err)
        assert feat.setting_key in err and slug in err, ("the refusal names the key + slug", err)
        assert _reads(slug) is before, f"{slug}: a refused config write must change nothing"
        assert len(_audit_rows("config.set")) == n_audit, f"{slug}: refusal must not audit"

    # (b) THE RATCHET: every registry feature key is refused, not just the seven probed.
    # A feature added later closes this door on the same commit or turns this red.
    for feat in features.FEATURES:
        r = api.post(
            "/api/admin/config",
            headers=h,
            json={"settings": {feat.setting_key: "1"}, "confirm": True},
        )
        _is(r, 400, f"config door refuses every feature key ({feat.slug})")
        # The four install-locked keys keep the stronger LOCAL-ONLY refusal (it is the
        # more actionable statement for them and the features route refuses them too);
        # every other feature key gets the feature-door message.
        err = r.json().get("error", "")
        if feat.setting_key in settings.REMOTE_IMMUTABLE_KEYS:
            assert "LOCAL-ONLY" in err, (feat.slug, err)
        else:
            assert "/api/admin/features/" in err, (feat.slug, err)

    # (c) A NON-feature setting still writes through that door -- the arm is narrow, and
    # this is what makes (a)+(b) meaningful rather than "the route is just broken".
    r = api.post("/api/admin/config", headers=h, json={"settings": {"mt_concurrency": "7"}})
    _is(r, 200, "a non-feature key still writes")
    assert settings.get("mt_concurrency") == "7", settings.get("mt_concurrency")
    # ...and a MIXED body is refused WHOLE: the non-feature key must not land either.
    r = api.post(
        "/api/admin/config",
        headers=h,
        json={"settings": {"mt_concurrency": "9", "reports_enabled": "0"}, "confirm": True},
    )
    _is(r, 400, "a mixed body is refused whole")
    assert settings.get("mt_concurrency") == "7", "the whole request must be rejected"

    # (c2) THE BLAST RADIUS. _write_validated_settings is SHARED: the semantic/SRX/
    # retention admin routes funnel through it too. None of their keys is a feature key,
    # and an over-broad arm (say, "refuse anything ending in _enabled") would silently
    # break them, so pin that they still write through the spine after this change.
    for path, body in (
        ("/api/admin/srx/enable", {}),
        # #280: the retention keys are security-sensitive, so the body confirms them.
        ("/api/admin/retention/config", {"enabled": True, "days": 7, "confirm": True}),
    ):
        r = api.post(path, headers=h, json=body)
        _is(r, 200, f"{path} still writes through the shared spine")

    # (d) READING flags through the config door is untouched (only the write is closed).
    r = api.get("/api/admin/config", headers=h)
    _is(r, 200, "config read still works")
    body = r.json()["settings"]
    for feat in features.FEATURES:
        assert feat.setting_key in body, f"{feat.setting_key} must still be readable"

    # (e) The MCP path is the SAME door, so the refusal relays. manage_install(set_config)
    # forwards its body to POST /api/admin/config verbatim (server.py), so proving the
    # route refuses IS proving the tool refuses -- but assert the forwarding shape too,
    # so a future handler that stopped forwarding could not silently reopen the door.
    server_py = os.path.join(_ROOT, "integrations", "mcp-server", "server.py")
    with open(server_py, encoding="utf-8") as fh:
        src = fh.read()
    assert 'act == "set_config"' in src and '_post_json(client, "/api/admin/config"' in src, (
        "manage_install(set_config) must still forward to POST /api/admin/config; if it "
        "grew its own write path, the feature-key refusal would no longer cover MCP"
    )
    print(
        "  [9] POST /api/admin/config refuses EVERY feature setting_key (the 7-flag gate "
        "probe + all 19), writes nothing and audits nothing; a non-feature key still "
        "writes; a mixed body is refused whole; the read is untouched; MCP relays"
    )


# --------------------------------------------------------------------------- #
# [8] the sensitive-key ratchet (what makes "no confirm prompt" a checked fact)
# --------------------------------------------------------------------------- #
def test_sensitive_feature_keys_are_ring_fenced() -> None:
    offenders = [
        f.slug
        for f in features.FEATURES
        if f.setting_key in settings.SENSITIVE_KEYS
        and f.slug not in features_admin.DOOR_MOVED
        and f.setting_key not in settings.REMOTE_IMMUTABLE_KEYS
    ]
    assert not offenders, (
        "these features carry a security-SENSITIVE setting key but are neither "
        "door-moved nor install-locked, so `config feature enable|disable` and POST "
        "/api/admin/features would flip them with NO confirm, unlike POST "
        "/api/admin/config which demands confirm-per-key. Decide on purpose: move the "
        "door, lock the key, or add a confirm arm to the seam: " + ", ".join(offenders)
    )
    # The door-moved set must stay honest: every slug in it is a real feature.
    unknown = [s for s in features_admin.DOOR_MOVED if features.feature_by_slug(s) is None]
    assert not unknown, f"DOOR_MOVED names slugs that are not features: {unknown}"
    # ...and every door-moved feature's key is ALSO install-locked. Belt to the config
    # door's braces (that door now refuses every feature key outright): if a future
    # door-moved feature's key were not install-locked, the ONLY thing standing between
    # the wire and a switch whose door deliberately moved would be that one refusal.
    unlocked = sorted(
        s
        for s in features_admin.DOOR_MOVED
        if features.feature_by_slug(s).setting_key not in settings.REMOTE_IMMUTABLE_KEYS
    )
    assert not unlocked, (
        "these features' switches were moved to another door, but their setting keys are "
        "NOT on settings.REMOTE_IMMUTABLE_KEYS, so the host-only law does not back the "
        "move up: " + ", ".join(unlocked)
    )
    # And the seam reads the immutable set LIVE, never a copy that could drift.
    assert getattr(features_admin, "settings", None) is settings, (
        "features_admin must read settings.REMOTE_IMMUTABLE_KEYS off the live settings "
        "module, not a local copy that could drift out of sync with it"
    )
    print(
        "  [8] every SENSITIVE feature key is door-moved or install-locked; DOOR_MOVED "
        "names only real features; the seam reads the immutable set live"
    )


def main() -> None:
    keys = _seed()
    from app.main import app

    api = TestClient(app)
    _auth(True)
    try:
        test_scope_tier(keys, api)
        test_refusals(keys, api)
        test_audit_and_noop(keys, api)
        test_config_door_refuses_feature_keys(keys, api)
    finally:
        _auth(False)
    test_web_form_parity(keys, api)
    test_never_feature_gated(keys, api)
    test_cli_local_and_remote(keys)
    test_mcp_exclusion()
    test_sensitive_feature_keys_are_ring_fenced()
    _remote_admin(False)
    print("FEATURE FLAG ADMIN OK")


if __name__ == "__main__":
    main()
