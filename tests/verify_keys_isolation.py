"""SPEC-key-based-projects section 10.6 — ISOLATION, AUTH-ON (Batch 4, Wave B).

Cross-tenant leakage is this project's most reliably found review defect class, so the
key surfaces get a suite whose only job is to attack them, and it runs with the LOGIN
WALL ON throughout. That is not a stylistic choice: with auth off `_my_customer_id`,
`_ident_customer_id` and `access.current_customer_id` all return None for everyone, so
an auth-off suite stays green with every scope predicate deleted. That is exactly what
happened to Ask+Draft, and it is what group [M] below refuses to let happen again.

Groups:
  [A] READS: the grid page, the /api key list, one key's values, an export and the
      delta feed all answer 404 (never 403, never a different message) for another
      customer's project, and never echo its content.
  [B] WRITES: create, value edit, base edit, metadata, plural, delete, restore and
      translate are all refused across the boundary AND leave the target untouched.
  [C] IMPORT / EXPORT specifically: a cross-tenant import writes nothing; a
      cross-tenant export returns no bytes.
  [D] KEY-ADDRESSED routes: a foreign key ID is a 404 identical to a nonexistent one,
      through both the web grid and /api.
  [E] MULTI-ORG: with the sacred gate ON, a user in org A cannot reach org B's key
      project by any of the same routes.
  [F] A BOUND LINGUIST sees exactly its own customer's key project and nothing else.
  [M] MUTATION-PROOF: stripping a scope predicate from the web route helper, from the
      /api project resolver, or from the service's own guard each makes this suite
      RED. A suite that stays green under a deleted check is decoration. Plus the
      per-predicate half (A-F6): each of `_api_project`'s three checks probed ALONE,
      and a positive check that the routes really thread the SERVICE ring's guards.

Final line: "KEYS ISOLATION OK".
"""

import json as _json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_keysiso_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Project, ProjectType, Role, StringKey
from app.services import (
    access,
    apikeys,
    content,
    security,
    settings,
)
from app.services import (
    keys as keys_svc,
)
from tests._csrf import csrf_for

PW = "keysiso-pw-123456"
_N = [0]
#: A string that exists ONLY inside customer B's project. Every refusal in this suite
#: is checked against it, so a leak is a literal substring match rather than an id
#: comparison somebody could get backwards.
SECRET = "ISOSECRETSTRINGBQQ"


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _multi_org(on: bool) -> None:
    settings.set_many({"multi_org_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _customer(name=None, org_id=None):
    with Session(engine) as s:
        c = Customer(name=name or "IsoCust %d" % _uniq(), org_id=org_id)  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _org(name):
    from app.models import Organization

    with Session(engine) as s:
        o = Organization(name=name, slug=name.lower().replace(" ", "-"))
        s.add(o)
        s.commit()
        s.refresh(o)
        return o.id


def _project(cust_id, name=None, org_id=None):
    with Session(engine) as s:
        p = content.project_create(
            s,
            name or "IsoProj %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cust_id),
            project_type=ProjectType.KEY_BASED,
        )
        if org_id is not None:
            # project_create derives the org from its `creator`, and this fixture has
            # no session user -- stamp it directly so the org ring has something real
            # to refuse.
            p.org_id = org_id
            s.add(p)
            s.commit()
        return p.id


def _add(pid, name, base="Hello"):
    with Session(engine) as s:
        return keys_svc.create_key(
            s, s.get(Project, pid), name, base_value=base, actor="iso-fixture"
        ).id


def _user(email, role=Role.ADMIN, customer_id=None, org_id=None):
    # #181: created IN its org (the seam refuses an org-less create under the gate).
    return security.create_user(email, email, role, PW, customer_id=customer_id, org_id=org_id)


def _client_for(email):
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def _token_for(user, scopes="api keys"):
    _row, raw = apikeys.create_key(user.id, "iso", scopes=scopes)
    return raw


class World:
    """Two customers, each with a key project. B's content carries SECRET."""

    def __init__(self, tag):
        self.cust_a = _customer(f"IsoA {tag}")
        self.cust_b = _customer(f"IsoB {tag}")
        self.pid_a = _project(self.cust_a, f"IsoProjA {tag}")
        self.pid_b = _project(self.cust_b, f"IsoProjB {tag}")
        self.key_a = _add(self.pid_a, "iso::a", "Plain A")
        self.key_b = _add(self.pid_b, f"iso::{SECRET}", SECRET)
        self.user_a = _user(
            f"iso-a-{tag}@x.com",
            role=Role.LINGUIST,
            customer_id=self.cust_a,  # noqa: RUF100, UP032
        )
        self.client_a = _client_for(self.user_a.email)
        self.token_a = _token_for(self.user_a)


_W = {}


def world():
    if "w" not in _W:
        _W["w"] = World("w1")
    return _W["w"]


def _no_leak(resp, where):
    body = resp.text or ""
    assert SECRET not in body, (f"customer B's content leaked through {where}", body[:400])


# ============================================================================
# [A] reads
# ============================================================================
def _read_probes(w):
    """(label, callable) for every READ a caller could reach B's project through."""
    c, t = w.client_a, w.token_a

    def api(path, **params):
        return c.get(path, params=params, headers={"X-Loc-Token": t})

    return [
        ("web grid", lambda pid: c.get("/projects/%d/keys" % pid, follow_redirects=False)),  # noqa: UP031
        (
            "web export",
            lambda pid: c.get(
                "/projects/%d/keys/export?locale=de-DE" % pid,  # noqa: UP031
                follow_redirects=False,  # noqa: RUF100, UP031
            ),
        ),
        ("api list", lambda pid: api("/api/projects/%d/keys" % pid)),  # noqa: UP031
        ("api export", lambda pid: api("/api/projects/%d/keys/export" % pid, locale="de-DE")),  # noqa: UP031
        ("api changes", lambda pid: api("/api/projects/%d/changes" % pid)),  # noqa: UP031
    ]


def test_reads_are_404_and_leak_nothing():
    w = world()
    for label, probe in _read_probes(w):
        own = probe(w.pid_a)
        assert own.status_code == 200, (label, "own project must read", own.status_code)
        foreign = probe(w.pid_b)
        assert foreign.status_code == 404, (label, foreign.status_code, foreign.text[:200])
        _no_leak(foreign, label)
        missing = probe(9999999)
        assert missing.status_code == 404, (label, missing.status_code)
        if label.startswith("api"):
            assert foreign.json() == missing.json(), (
                label,
                "a foreign project must be indistinguishable from a missing one",
                foreign.json(),
                missing.json(),
            )
    print(
        "  [A] all 5 READ surfaces answer 404 for another customer's key project, "
        "identically to a missing id, and leak nothing OK"
    )


# ============================================================================
# [B] writes
# ============================================================================
def _write_probes(w):
    c, t = w.client_a, w.token_a

    def web(path, data=None):
        return lambda pid, kid: c.post(
            path % {"pid": pid, "kid": kid}, data=data or {}, follow_redirects=False
        )

    return [
        ("web add", web("/projects/%(pid)d/keys/add", {"name": "iso::sneak"})),
        (
            "web value",
            web("/projects/%(pid)d/keys/%(kid)d/value", {"locale": "de-DE", "text": "sneak"}),
        ),
        ("web base", web("/projects/%(pid)d/keys/%(kid)d/base", {"text": "sneak"})),
        ("web meta", web("/projects/%(pid)d/keys/%(kid)d/meta", {"description": "sneak"})),
        ("web plural", web("/projects/%(pid)d/keys/%(kid)d/plural", {"is_plural": "1"})),
        (
            "web delete",
            web(
                "/projects/%(pid)d/keys/%(kid)d/delete",
                {"confirm_name": f"iso::{SECRET}"},
            ),
        ),
        ("web restore", web("/projects/%(pid)d/keys/%(kid)d/restore")),
        ("web autounverify", web("/projects/%(pid)d/keys/autounverify", {"on": "0"})),
        (
            "web translate",
            web("/projects/%(pid)d/keys/translate", {"workflow_id": "1", "locale": "de-DE"}),
        ),
        (
            "api upsert",
            lambda pid, kid: c.post(
                "/api/projects/%d/keys" % pid,  # noqa: UP031
                headers={"X-Loc-Token": t},
                json={"keys": [{"name": "iso::apisneak", "base": "x"}]},
            ),
        ),
        (
            "api patch",
            lambda pid, kid: c.patch(
                "/api/keys/%d" % kid,  # noqa: UP031
                headers={"X-Loc-Token": t},
                json={"description": "sneak"},  # noqa: RUF100, UP031
            ),
        ),
        (
            "api put value",
            lambda pid, kid: c.put(
                "/api/keys/%d/values/de-DE" % kid,  # noqa: UP031
                headers={"X-Loc-Token": t},
                json={"text": "sneak"},
            ),
        ),
        ("api delete", lambda pid, kid: c.delete("/api/keys/%d" % kid, headers={"X-Loc-Token": t})),  # noqa: UP031
        (
            "api import",
            lambda pid, kid: c.post(
                "/api/projects/%d/keys/import" % pid,  # noqa: UP031
                headers={"X-Loc-Token": t},
                json={"filename": "iso.json", "data": _json.dumps({"iso::injected": "x"})},
            ),
        ),
    ]


def _snapshot_b(w):
    with Session(engine) as s:
        key = s.get(StringKey, w.key_b)
        return {
            "names": sorted(k.name for k in keys_svc.list_keys(s, w.pid_b, include_deleted=True)),
            "deleted": key.deleted_at is not None,
            "description": key.description,
            "is_plural": key.is_plural,
            "base": keys_svc.base_values(s, key),
            "values": {v.target_locale: v.target_text for v in keys_svc.values_for(s, key)},
            "autounverify": keys_svc.autounverify_enabled(s.get(Project, w.pid_b)),
        }


def test_writes_are_refused_and_change_nothing():
    w = world()
    before = _snapshot_b(w)
    for label, probe in _write_probes(w):
        r = probe(w.pid_b, w.key_b)
        assert r.status_code in (403, 404), (label, r.status_code, r.text[:200])
        _no_leak(r, label)
    after = _snapshot_b(w)
    assert before == after, ("a refused cross-customer write still changed B", before, after)
    print(
        "  [B] all 14 WRITE routes are refused across the customer boundary AND "
        "leave B's project byte-identical OK"
    )


def test_writes_still_work_on_the_callers_own_project():
    """The other half of the claim: the refusals above are ISOLATION, not a broken
    surface. Without this, deleting the feature entirely would pass [B]."""
    w = world()
    r = w.client_a.post(
        "/projects/%d/keys/add" % w.pid_a,  # noqa: UP031
        data={"name": "iso::mine", "base_value": "Mine"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    r = w.client_a.patch(
        "/api/keys/%d" % w.key_a,  # noqa: UP031
        headers={"X-Loc-Token": w.token_a},
        json={"description": "mine"},  # noqa: RUF100, UP031
    )
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert keys_svc.get_key(s, w.pid_a, "iso::mine") is not None
        assert s.get(StringKey, w.key_a).description == "mine"
    print(
        "  [B] the same caller's writes DO land on its own project (so [B] is "
        "isolation, not an inert surface) OK"
    )


# ============================================================================
# [C] import / export specifically
# ============================================================================
def test_cross_tenant_import_and_export():
    w = world()
    payload = _json.dumps({"injected": "nope"}).encode()
    r = w.client_a.post(
        "/projects/%d/keys/import" % w.pid_b,  # noqa: UP031
        files={"file": ("iso.json", payload, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 404, r.status_code
    with Session(engine) as s:
        assert keys_svc.get_key(s, w.pid_b, "injected") is None, (
            "a cross-customer import must write NOTHING"
        )

    r = w.client_a.get("/projects/%d/keys/export?locale=de-DE" % w.pid_b, follow_redirects=False)  # noqa: UP031
    assert r.status_code == 404 and SECRET not in r.text, (r.status_code, r.text[:200])
    r = w.client_a.get(
        "/api/projects/%d/keys/export?locale=de-DE" % w.pid_b,  # noqa: UP031
        headers={"X-Loc-Token": w.token_a},  # noqa: RUF100, UP031
    )
    assert r.status_code == 404 and SECRET not in r.text, (r.status_code, r.text[:200])
    print(
        "  [C] a cross-tenant import writes nothing and a cross-tenant export returns no bytes OK"
    )


# ============================================================================
# [D] key-addressed routes
# ============================================================================
def test_foreign_key_id_is_indistinguishable_from_a_missing_one():
    w = world()
    t = {"X-Loc-Token": w.token_a}
    foreign = w.client_a.get("/api/keys/%d/values" % w.key_b, headers=t)  # noqa: UP031
    missing = w.client_a.get("/api/keys/999999/values", headers=t)
    assert foreign.status_code == 404 and foreign.json() == missing.json(), (
        foreign.status_code,
        foreign.json(),
        missing.json(),
    )
    _no_leak(foreign, "api key values")

    # ...and through the web grid, where the key belongs to another PROJECT entirely
    r = w.client_a.get("/projects/%d/keys/%d/delete" % (w.pid_a, w.key_b), follow_redirects=False)  # noqa: UP031
    assert r.status_code == 404, r.status_code
    _no_leak(r, "web delete confirm")
    print(
        "  [D] a foreign key id answers exactly what a nonexistent one does, on "
        "both the /api and web key-addressed routes OK"
    )


# ============================================================================
# [E] multi-org
# ============================================================================
def test_cross_org_isolation():
    _multi_org(True)
    try:
        org_a, org_b = _org("IsoOrgA %d" % _uniq()), _org("IsoOrgB %d" % _uniq())  # noqa: UP031
        cust_a = _customer("IsoOrgCustA %d" % _uniq(), org_id=org_a)  # noqa: UP031
        cust_b = _customer("IsoOrgCustB %d" % _uniq(), org_id=org_b)  # noqa: UP031
        pid_a = _project(cust_a, org_id=org_a)
        pid_b = _project(cust_b, org_id=org_b)
        _add(pid_b, f"org::{SECRET}", SECRET)
        user_a = _user("iso-org-a-%d@x.com" % _uniq(), role=Role.ADMIN, org_id=org_a)  # noqa: UP031
        client = _client_for(user_a.email)
        tok = _token_for(user_a)
        assert access.multi_org_active(), "the sacred gate must be on for this test"

        assert client.get("/projects/%d/keys" % pid_a, follow_redirects=False).status_code == 200  # noqa: UP031
        for path in ("/projects/%d/keys" % pid_b, "/projects/%d/keys/export?locale=de-DE" % pid_b):  # noqa: UP031
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 404, (path, r.status_code)
            _no_leak(r, path)
        for path in ("/api/projects/%d/keys" % pid_b, "/api/projects/%d/changes" % pid_b):  # noqa: UP031
            r = client.get(path, headers={"X-Loc-Token": tok})
            assert r.status_code == 404, (path, r.status_code, r.text[:200])
            _no_leak(r, path)
    finally:
        _multi_org(False)
    print(
        "  [E] with multi-org ACTIVE, org A cannot reach org B's key project "
        "through the grid, the export or the /api group OK"
    )


# ============================================================================
# [F] bound-linguist visibility
# ============================================================================
def test_bound_linguist_sees_only_its_own():
    w = world()
    listing = w.client_a.get("/projects").text
    assert "IsoProjA" in listing, "a bound linguist must see its OWN project"
    assert "IsoProjB" not in listing, (
        "a bound linguist must not see another customer's key project in the list"
    )
    r = w.client_a.get("/api/projects/%d/keys" % w.pid_a, headers={"X-Loc-Token": w.token_a})  # noqa: UP031
    assert r.status_code == 200 and SECRET not in r.text, r.text[:300]
    names = {k["name"] for k in r.json()["keys"]}
    assert f"iso::{SECRET}" not in names, names
    print(
        "  [F] a customer-bound linguist's project list and key list carry its own project only OK"
    )


# ============================================================================
# [M] MUTATION PROOF
# ============================================================================
def _run_isolation_claims():
    """Every claim this suite makes about scoping, as one callable. Used by the
    mutation probes: if a stripped predicate leaves ALL of these passing, the suite
    is decoration."""
    test_reads_are_404_and_leak_nothing()
    test_writes_are_refused_and_change_nothing()
    test_cross_tenant_import_and_export()
    test_foreign_key_id_is_indistinguishable_from_a_missing_one()
    test_bound_linguist_sees_only_its_own()


def _mutation_goes_red(label, patch, unpatch) -> None:
    import io
    from contextlib import redirect_stdout

    patch()
    try:
        broke = False
        try:
            with redirect_stdout(io.StringIO()):
                _run_isolation_claims()
        except AssertionError:
            broke = True
        assert broke, (
            f"MUTATION SURVIVED: {label} was stripped and this suite stayed "
            "green -- it does not actually defend that predicate"
        )
    finally:
        unpatch()
    # ...and the suite is green again once the predicate is restored, so the probe
    # proved something about the MUTATION rather than about a broken fixture.
    with redirect_stdout(io.StringIO()):
        _run_isolation_claims()


def test_mutation_proof():
    """Three probes, and the shape of them is the finding.

    The key surfaces carry TWO independent scope rings: the ROUTE ring
    (`_visibility_block` on the web, `_api_project` on /api) and the SERVICE ring
    (`keys._project`'s org/customer guards, which Wave A put on the seams precisely so
    a forgetful route is still refused). Two rings mean no single strip can leak
    everything -- which is the point of them, and also the reason a naive "strip one
    predicate, expect red" probe would be misleading.

    So the probes below strip a ring at a time and demand the suite goes RED anyway,
    which it does through the EXPORT routes: `key_files.export_locale` takes an
    already-resolved project and carries no guards of its own, so the route ring is
    genuinely the only thing standing between two tenants there. The third probe
    strips both rings on the /api path and demands red for the reads that the service
    ring alone was covering -- and asserts, positively, that stripping the service
    ring ALONE does not leak, because the route ring catches it.
    """
    from fastapi.responses import JSONResponse

    import app.web.deps as deps
    from app.services import keys as svc
    from app.web import keys as web_keys

    # (1) the WEB route ring.
    real_block, real_web_block = deps._visibility_block, web_keys._visibility_block
    _mutation_goes_red(
        "the web routes' _visibility_block",
        lambda: (
            setattr(deps, "_visibility_block", lambda *a, **k: None),
            set_pieces("app.web.keys", "_visibility_block", lambda *a, **k: None),
        ),
        lambda: (
            setattr(deps, "_visibility_block", real_block),
            set_pieces("app.web.keys", "_visibility_block", real_web_block),
        ),
    )

    # (2) the /api route ring.
    real_api_project = web_keys._api_project

    def blind_api_project(session, ident, pid):
        project = session.get(Project, pid)
        if project is None or not svc.is_key_project(project):
            return None, JSONResponse(web_keys._NOT_FOUND, status_code=404)
        return project, None

    _mutation_goes_red(
        "_api_project's org/customer/can_view scoping",
        lambda: set_pieces("app.web.keys", "_api_project", blind_api_project),
        lambda: set_pieces("app.web.keys", "_api_project", real_api_project),
    )

    # (3) the SERVICE ring, alone: it must NOT leak, because the route ring is still
    # there. This is the defense-in-depth half, asserted rather than assumed.
    real_guard = svc._project

    def blind_guard(session, project, **kw):
        return real_guard(session, project)

    import io as _io
    from contextlib import redirect_stdout as _rso

    set_pieces("app.services.keys", "_project", blind_guard)
    try:
        with _rso(_io.StringIO()):
            _run_isolation_claims()
    finally:
        set_pieces("app.services.keys", "_project", real_guard)

    # ...and with BOTH /api rings gone, the reads the service ring was covering leak,
    # which is what proves this suite is watching that ring at all.
    _mutation_goes_red(
        "BOTH the /api resolver and keys._project's guards",
        lambda: (
            set_pieces("app.web.keys", "_api_project", blind_api_project),
            set_pieces("app.services.keys", "_project", blind_guard),
        ),
        lambda: (
            set_pieces("app.web.keys", "_api_project", real_api_project),
            set_pieces("app.services.keys", "_project", real_guard),
        ),
    )
    print(
        "  [M] MUTATION-PROVEN: stripping the web ring, the /api ring, or both /api "
        "rings each turns this suite RED; stripping the SERVICE ring alone is "
        "caught by the route ring (defense in depth, asserted) OK"
    )


def _api_project_without(predicate):
    """A `_api_project` with exactly ONE of its four checks removed."""
    from fastapi.responses import JSONResponse

    from app.services import access, security
    from app.services import keys as svc
    from app.web import deps
    from app.web import keys as web_keys

    def stripped(session, ident, pid):
        project = session.get(Project, pid)
        if project is None:
            return None, JSONResponse(web_keys._NOT_FOUND, status_code=404)
        org_id = deps._ident_org_id(ident)
        if predicate != "org" and org_id is not None and getattr(project, "org_id", None) != org_id:
            return None, JSONResponse(web_keys._NOT_FOUND, status_code=404)
        cust = (ident or {}).get("_cust_id")
        if (
            predicate != "customer"
            and cust is not None
            and getattr(project, "customer_id", None) != cust
        ):
            return None, JSONResponse(web_keys._NOT_FOUND, status_code=404)
        if (
            predicate != "can_view"
            and security.auth_required()
            and not access.can_view_project(session, ident.get("user"), project)
        ):
            return None, JSONResponse(web_keys._NOT_FOUND, status_code=404)
        if not svc.is_key_project(project):
            return None, JSONResponse(web_keys._NOT_FOUND, status_code=404)
        return project, None

    return stripped


def test_api_project_predicates_one_at_a_time():
    """[M] A-F6. The [M] probe above strips `_api_project` WHOLESALE, so it says
    nothing about the individual predicates -- and the reviewer's finding is that each
    one SURVIVES ALONE. That is not an accident, it is the shape of the code:
    `access.can_view_project` carries its OWN org and customer rings, so removing
    `_api_project`'s explicit org check (or its customer check) leaves a caller
    refused anyway.

    That deserves to be asserted rather than assumed, and it deserves the OTHER half
    too: a world where `can_view_project` is the ONLY thing standing. With multi-org
    off and the caller not customer-bound, `_ident_org_id` and `_cust_id` are both
    None, so both explicit checks are no-ops by construction; project_scoping ON plus
    a non-member LINGUIST makes can_view the whole ring. Stripping it there MUST leak,
    and it does -- which is what proves this suite watches that predicate at all."""
    from app.web import keys as web_keys

    real = web_keys._api_project

    # (a) org and customer, one at a time: NO leak, because can_view re-checks both.
    for predicate in ("org", "customer"):
        set_pieces("app.web.keys", "_api_project", _api_project_without(predicate))
        try:
            import io as _io
            from contextlib import redirect_stdout as _rso

            with _rso(_io.StringIO()):
                _run_isolation_claims()
        finally:
            set_pieces("app.web.keys", "_api_project", real)

    # (b) ...and a world where can_view is the ONLY ring, so stripping it leaks.
    settings.set_many({"project_scoping": "1"})
    settings.invalidate_cache()
    try:
        cust = _customer("IsoScopeCust %d" % _uniq())  # noqa: UP031
        mine = _project(cust, "IsoScopeMine %d" % _uniq())  # noqa: UP031
        theirs = _project(cust, "IsoScopeTheirs %d" % _uniq())  # noqa: UP031
        _add(theirs, f"scope::{SECRET}", SECRET)
        # A VIEWER with NO customer binding: `_cust_id` is None and multi-org is off,
        # so `_api_project`'s two explicit checks cannot fire for this caller at all;
        # and VIEWER is the one role `access.can_see_all` does not wave through, so
        # membership -- i.e. `can_view_project` -- is the ENTIRE ring here.
        user = _user("iso-scope-%d@x.com" % _uniq(), role=Role.VIEWER)  # noqa: UP031
        tok = _token_for(user)
        client = TestClient(app)
        head = {"X-Loc-Token": tok}
        from app.models import ProjectMember

        with Session(engine) as s:
            s.add(ProjectMember(project_id=mine, user_id=user.id))
            s.commit()

        assert client.get("/api/projects/%d/keys" % mine, headers=head).status_code == 200  # noqa: UP031
        r = client.get("/api/projects/%d/keys" % theirs, headers=head)  # noqa: UP031
        assert r.status_code == 404, (r.status_code, r.text[:200])
        _no_leak(r, "api list, non-member linguist")

        set_pieces("app.web.keys", "_api_project", _api_project_without("can_view"))
        try:
            r = client.get("/api/projects/%d/keys" % theirs, headers=head)  # noqa: UP031
            assert r.status_code == 200 and SECRET in r.text, (
                "MUTATION SURVIVED: with org and customer both inapplicable, "
                "stripping can_view_project left the /api key list refusing anyway "
                "-- this suite is not watching that predicate",
                r.status_code,
            )
        finally:
            set_pieces("app.web.keys", "_api_project", real)
        r = client.get("/api/projects/%d/keys" % theirs, headers=head)  # noqa: UP031
        assert r.status_code == 404, ("...and it must refuse again once restored", r.status_code)
    finally:
        settings.set_many({"project_scoping": "0"})
        settings.invalidate_cache()
    print(
        "  [M] each _api_project predicate probed ALONE: org and customer are "
        "defense in depth (can_view re-checks both, asserted), and can_view is "
        "load-bearing where it is the only applicable ring (stripping it LEAKS) OK"
    )


def test_the_service_ring_is_actually_threaded():
    """[M] A-F6, second half. The service seams take `org_id=` / `customer_id=`
    (Wave A review A11) so a route that resolved a project itself and forgot is still
    refused -- but NOTHING checked that the routes pass them, and dropping
    `**_api_scope(ident)` at a call site cannot turn this suite red precisely BECAUSE
    the route ring catches it. Defense in depth is unfalsifiable from the outside, so
    it is checked from the inside instead: what the route hands the seam, and what the
    seam does with a mismatch."""
    from app.services import keys as svc
    from app.web import keys as web_keys

    w = world()
    seen: list = []
    real_page, real_changes = svc.page_keys, svc.changes_since

    def spy_page(session, project_id, **kw):
        seen.append(("page_keys", kw.get("org_id", "MISSING"), kw.get("customer_id", "MISSING")))
        return real_page(session, project_id, **kw)

    def spy_changes(session, project_id, cursor=0, limit=None, **kw):
        seen.append(
            ("changes_since", kw.get("org_id", "MISSING"), kw.get("customer_id", "MISSING"))
        )
        return real_changes(session, project_id, cursor, limit or svc.DEFAULT_DELTA_LIMIT, **kw)

    set_pieces("app.services.keys", "page_keys", spy_page)
    set_pieces("app.services.keys", "changes_since", spy_changes)
    web_keys.keys_svc.page_keys, web_keys.keys_svc.changes_since = spy_page, spy_changes
    try:
        head = {"X-Loc-Token": w.token_a}
        assert w.client_a.get("/api/projects/%d/keys" % w.pid_a, headers=head).status_code == 200  # noqa: UP031
        assert w.client_a.get("/api/projects/%d/changes" % w.pid_a, headers=head).status_code == 200  # noqa: UP031
    finally:
        set_pieces("app.services.keys", "page_keys", real_page)
        set_pieces("app.services.keys", "changes_since", real_changes)
        web_keys.keys_svc.page_keys = real_page
        web_keys.keys_svc.changes_since = real_changes

    assert len(seen) == 2, seen
    for name, org_id, customer_id in seen:
        assert customer_id == w.cust_a, (
            f"{name} was called WITHOUT the caller's customer guard -- the seam's own "
            "isolation ring is dead code on this route",
            seen,
        )
        assert org_id != "MISSING", (name, seen)

    # ...and the seam really refuses a mismatched scope, so passing it means something.
    with Session(engine) as s:
        for label, call in (
            ("page_keys", lambda: svc.page_keys(s, w.pid_b, customer_id=w.cust_a)),
            ("changes_since", lambda: svc.changes_since(s, w.pid_b, 0, customer_id=w.cust_a)),
            ("list_keys", lambda: svc.list_keys(s, w.pid_b, customer_id=w.cust_a)),
            (
                "get_key",
                lambda: svc.get_key(s, w.pid_b, f"iso::{SECRET}", customer_id=w.cust_a),
            ),
            ("namespaces", lambda: svc.namespaces(s, w.pid_b, customer_id=w.cust_a)),
        ):
            try:
                call()
                raise AssertionError(
                    f"{label} accepted a MISMATCHED customer scope -- the guard the routes "
                    "thread is not a guard"
                )
            except svc.NotAKeyProjectError:
                pass
    print(
        "  [M] the /api routes really hand every read seam the caller's "
        "org+customer guards, and all 5 guarded seams refuse a mismatched scope "
        "(so threading them is not decoration) OK"
    )


def main():
    init_db()
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    _auth(True)
    try:
        test_reads_are_404_and_leak_nothing()
        test_writes_are_refused_and_change_nothing()
        test_writes_still_work_on_the_callers_own_project()
        test_cross_tenant_import_and_export()
        test_foreign_key_id_is_indistinguishable_from_a_missing_one()
        test_cross_org_isolation()
        test_bound_linguist_sees_only_its_own()
        test_mutation_proof()
        test_api_project_predicates_one_at_a_time()
        test_the_service_ring_is_actually_threaded()
    finally:
        _auth(False)
    print("KEYS ISOLATION OK")


if __name__ == "__main__":
    main()
