"""Archived-customer scope refusal (glossaries/content-types/connectors) + glossary
locale validation -- verification for bug-log entries T2-01 (connectors part only),
T2-02, T2-03 (the planning notes).

Offline + hermetic (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network
is ever touched). Auth stays OFF throughout: these are service/web-layer scope bugs,
not RBAC bugs, and the pre-fix code path being fixed is reachable byte-identically
with the gate off (see the T2-01 fail-pre-fix proof below).

T2-02 (MED) -- POST /glossaries and POST /content-types with an ARCHIVED customer_id
used to silently store the row as customer_id=NULL (global) instead of refusing.
Fixed at app/web/glossaries.py::_valid_customer_id and
app/web/content_types.py::_valid_customer_id: a supplied customer_id that names an
archived (or otherwise not-selectable) customer now RAISES a clean
GlossaryModuleError / ContentTypeError -- mirroring content.project_create's
ArchivedCustomerError contract -- instead of falling back to None (global). A blank
customer_id (global) and a valid ACTIVE customer_id (scoped) are unaffected.
  FAIL-PRE-FIX PROOF: the pre-fix helper (git show HEAD:app/web/glossaries.py /
  app/web/content_types.py) was `cid if cid in ok else None`, where `ok` is the
  active_only-filtered id set. Reproduced inline below: an archived customer's id is
  provably ABSENT from that set, so the OLD expression evaluates to None (silently
  global) rather than raising.

T2-01 (HIGH, connectors part) -- POST /connectors/<kind> (create) and the connection
UPDATE path with customer_id=<archived> used to succeed and stamp the archived
customer onto the connection. Fixed at app/web/connectors.py::_valid_customer_id (new
ArchivedCustomerScopeError), wired into connector_create + connection_update. The
workflow/workflow-group part of T2-01 is owned by a different branch and is untouched
here (app/web/workflows.py is not touched by this branch).
  FAIL-PRE-FIX PROOF: the pre-fix helper (git show HEAD:app/web/connectors.py) refused
  only on `cust is None or not access.same_org(me, cust.org_id)` -- no archived_at
  check anywhere. Reproduced inline below: for an archived-but-same-org customer that
  guard evaluates False (same_org is a byte-identical no-op with the multi-org gate
  off, the default posture), so the OLD code fell through to `return raw` -- the
  archived id, unblocked.

T2-03 (MED) -- POST /glossaries/{id}/pairs accepted unknown/garbage locale codes
(e.g. stored 'not-A-LOCALE' verbatim) because `_canon`'s fallback coerced any
catalog-unknown code into a plausible-looking string instead of rejecting it. Fixed
at app/services/glossary_module.py::_canon, which now raises GlossaryModuleError for
a non-blank code absent from languages_catalog (mirroring customers.add_pair's
validate_codes gate); the same validation is applied to the glossary TERM-add locales
(glossary_module.add_term), which were equally unchecked pre-fix (blank stays
legitimate there -- a wildcard "any locale" term).
  FAIL-PRE-FIX PROOF: the pre-fix `_canon` (git show HEAD:app/services/glossary_module.py)
  ended in a separator/casing-only fallback with no catalog check. Reproduced inline
  below: that fallback turns 'not-A-LOCALE' into 'not-A-LOCALE' UNCHANGED (it passes
  straight through, canonicalization or not) -- i.e. the exact verbatim-storage bug
  from the bug log.

Positive/regression cases proven throughout: no customer_id supplied = global (fine);
a valid ACTIVE customer_id = scoped (fine); a valid catalog locale pair/term is
accepted and canonicalized.

Final line: "ARCHIVED SCOPE GLOSSARY VALIDATION OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_archscope_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Connection,
    ContentType,
    Customer,
    Glossary,
    Organization,
    Role,
)
from app.services import (
    access,
    languages_catalog,
    security,
    settings,
)
from app.services import customers as customer_service
from app.services import glossary_module as gm
from tests._csrf import csrf_for

init_db()


# --- helpers -----------------------------------------------------------------
def _auth_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_on(multi_org=False):
    settings.set_many(
        {"security_auth_required": "1", "multi_org_enabled": "1" if multi_org else "0"}
    )
    settings.invalidate_cache()


def _org(name):
    with Session(engine) as s:
        o = Organization(name=name, active=True)
        s.add(o)
        s.commit()
        s.refresh(o)
        return o.id


def _customer(name, org_id=None):
    with Session(engine) as s:
        c = Customer(name=name, org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _login(email, *, org_id=None, role=Role.LINGUIST, password="scope-pass-123456"):
    """A real logged-in TestClient for an org-scoped staff user (no customer binding),
    carrying its session cookie + CSRF header. Auth must already be ON. create_user
    takes no org_id, so the org binding is stamped on the row post-create."""
    u = security.create_user(
        email, email, role, password, org_id=org_id
    )  # #181: created IN its org
    if org_id is not None:
        with Session(engine) as s:
            row = s.get(type(u), u.id)
            row.org_id = org_id
            s.add(row)
            s.commit()
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"login failed for {email}: {r.status_code}"
    csrf_for(c)
    return c


def _archive(cid):
    with Session(engine) as s:
        customer_service.archive_customer(s, cid, actor="a@x.test")


def _glossary_count():
    with Session(engine) as s:
        return len(s.exec(select(Glossary.id)).all())


def _ct_count():
    with Session(engine) as s:
        return len(s.exec(select(ContentType.id)).all())


def _location(r):
    return r.headers.get("location", "")


# ============================================================================
# T2-02 -- glossary create with an archived customer_id
# ============================================================================
def test_glossary_create_archived_customer_refused_fail_pre_fix():
    _auth_off()
    cid = _customer("GlossArchived")
    _archive(cid)

    # FAIL-PRE-FIX PROOF: the pre-fix web helper was
    #   cid if cid in {c.id for c in _customers(request, session)} else None
    # where _customers() is active_only-filtered. Reproduce the selection set inline:
    with Session(engine) as s:
        old_ok = {c.id for c in s.exec(customer_service.active_only(select(Customer))).all()}
    assert cid not in old_ok, "archived customer must be excluded from the picker set (setup)"
    old_result = cid if cid in old_ok else None
    assert old_result is None, (
        "pre-fix expression silently resolves an archived customer_id to None "
        "(global) instead of raising -- this IS the T2-02 bug"
    )

    c = TestClient(app)
    n_before = _glossary_count()
    r = c.post(
        "/glossaries", data={"name": "GArchTest", "customer_id": str(cid)}, follow_redirects=False
    )
    assert r.status_code == 303 and "err=" in _location(r), (
        "creating a glossary for an archived customer must redirect with an error "
        f"(got {r.status_code} -> {_location(r)!r})"
    )
    assert _glossary_count() == n_before, (
        "no glossary row may be created (not even a silently-global one) when the "
        "supplied customer_id is archived"
    )
    with Session(engine) as s:
        assert s.exec(select(Glossary).where(Glossary.name == "GArchTest")).first() is None, (
            "the archived-scope create must not have slipped through as a global row"
        )

    print(
        "  T2-02 glossary create: archived customer_id refused, no row created; "
        "fail-pre-fix proven OK"
    )


def test_glossary_create_positive_cases():
    _auth_off()
    active_id = _customer("GlossActive")
    c = TestClient(app)

    # no customer_id supplied is now REFUSED (2026-07-14: every glossary belongs
    # to a customer — no "global" glossaries from any create seam). Clean redirect with
    # an error, no row created.
    r = c.post("/glossaries", data={"name": "GGlobalTest"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in _location(r), (r.status_code, _location(r))
    with Session(engine) as s:
        assert s.exec(select(Glossary).where(Glossary.name == "GGlobalTest")).first() is None

    # a valid ACTIVE customer = scoped (fine)
    r = c.post(
        "/glossaries",
        data={"name": "GScopedTest", "customer_id": str(active_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in _location(r)
    with Session(engine) as s:
        g = s.exec(select(Glossary).where(Glossary.name == "GScopedTest")).first()
        assert g is not None and g.customer_id == active_id
    print(
        "  glossary create: no-customer now REFUSED (customer-first); "
        "active-customer=scoped works OK"
    )


# ============================================================================
# T2-02 -- content-type create with an archived customer_id
# ============================================================================
def test_content_type_create_archived_customer_refused_fail_pre_fix():
    _auth_off()
    cid = _customer("CtArchived")
    _archive(cid)

    # FAIL-PRE-FIX PROOF: identical shape to the glossary helper above (git show
    # HEAD:app/web/content_types.py) -- `cid if cid in ok else None`.
    with Session(engine) as s:
        old_ok = {c.id for c in s.exec(customer_service.active_only(select(Customer))).all()}
    assert cid not in old_ok
    assert (cid if cid in old_ok else None) is None, (
        "pre-fix expression silently resolves an archived customer_id to None -- "
        "the T2-02 bug for content types"
    )

    c = TestClient(app)
    n_before = _ct_count()
    r = c.post(
        "/content-types",
        data={"name": "CTArchTest", "customer_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in _location(r), (
        "creating a content type for an archived customer must redirect with an "
        f"error (got {r.status_code} -> {_location(r)!r})"
    )
    assert _ct_count() == n_before, "no content-type row may be created for an archived customer"
    with Session(engine) as s:
        assert s.exec(select(ContentType).where(ContentType.name == "CTArchTest")).first() is None
    print(
        "  T2-02 content-type create: archived customer_id refused, no row created; "
        "fail-pre-fix proven OK"
    )


def test_content_type_create_positive_cases():
    _auth_off()
    active_id = _customer("CtActive")
    c = TestClient(app)

    r = c.post("/content-types", data={"name": "CTGlobalTest"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in _location(r)
    with Session(engine) as s:
        ct = s.exec(select(ContentType).where(ContentType.name == "CTGlobalTest")).first()
        assert ct is not None and ct.customer_id is None

    r = c.post(
        "/content-types",
        data={"name": "CTScopedTest", "customer_id": str(active_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in _location(r)
    with Session(engine) as s:
        ct = s.exec(select(ContentType).where(ContentType.name == "CTScopedTest")).first()
        assert ct is not None and ct.customer_id == active_id
    print(
        "  T2-02 content-type create: no-customer=global and active-customer=scoped "
        "both still work OK"
    )


# ============================================================================
# T2-01 (connectors part) -- create + update with an archived customer_id
# ============================================================================
def test_connector_create_archived_customer_refused_fail_pre_fix():
    _auth_off()
    cid = _customer("ConnArchived")
    _archive(cid)

    # FAIL-PRE-FIX PROOF: the pre-fix guard (git show HEAD:app/web/connectors.py) was
    #   if cust is None or not access.same_org(me, cust.org_id): return ""
    # with NO archived_at check. Reproduce that exact guard inline for an
    # archived-but-existing, same-org customer -- with the multi-org gate off (the
    # verify-suite default AND every single-tenant install), same_org is a
    # byte-identical no-op (always True), so the guard is False and the OLD code
    # would fall through to `return raw` (the archived id), unblocked.
    with Session(engine) as s:
        old_cust = s.get(Customer, cid)
    assert old_cust is not None
    old_guard_refuses = old_cust is None or not access.same_org(
        None, getattr(old_cust, "org_id", None)
    )
    assert old_guard_refuses is False, (
        "pre-fix guard does not refuse an archived-but-existing customer -- this IS "
        "the T2-01 connectors bug (an archived customer_id sails through to be "
        "stamped on the new connection)"
    )

    c = TestClient(app)
    with Session(engine) as s:
        n_before = len(s.exec(select(Connection.id)).all())
    r = c.post(
        "/connectors/jira",
        data={"name": "ConnArchTest", "customer_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/connectors/jira?msg=" in _location(r), (
        "creating a connector for an archived customer must redirect back to the "
        f"add-form with a clean error (got {r.status_code} -> {_location(r)!r})"
    )
    with Session(engine) as s:
        n_after = len(s.exec(select(Connection.id)).all())
        assert n_after == n_before, "no connection row may be created for an archived customer"
        assert s.exec(select(Connection).where(Connection.name == "ConnArchTest")).first() is None
    print(
        "  T2-01 connector create: archived customer_id refused, no row created; "
        "fail-pre-fix proven OK"
    )


def test_connector_update_archived_customer_refused():
    _auth_off()
    cid = _customer("ConnArchivedUpd")
    _archive(cid)

    # Create a legitimate GLOBAL connection first (the pre-existing row an update
    # would try to re-scope).
    c = TestClient(app)
    r = c.post("/connectors/jira", data={"name": "ConnUpdTarget"}, follow_redirects=False)
    assert r.status_code == 303
    conn_id = int(_location(r).rstrip("/").rsplit("/", 1)[-1])
    with Session(engine) as s:
        assert s.get(Connection, conn_id).customer_id is None

    r = c.post(
        f"/connectors/connection/{conn_id}",
        data={"name": "ConnUpdTarget", "customer_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "ok=0" in _location(r) and "msg=" in _location(r), (
        "updating a connection's scope to an archived customer must redirect with "
        f"ok=0 + a clean error (got {r.status_code} -> {_location(r)!r})"
    )
    with Session(engine) as s:
        assert s.get(Connection, conn_id).customer_id is None, (
            "the connection's customer scope must NOT be changed to the archived "
            "customer -- it must keep its prior (global) scope"
        )
    print("  T2-01 connector update: archived customer_id refused, existing scope preserved OK")


def test_connector_create_and_update_positive_cases():
    _auth_off()
    active_id = _customer("ConnActive")
    c = TestClient(app)

    # global create (no customer_id) still works
    r = c.post("/connectors/jira", data={"name": "ConnGlobalTest"}, follow_redirects=False)
    assert r.status_code == 303 and "/connectors/connection/" in _location(r)
    gconn_id = int(_location(r).rstrip("/").rsplit("/", 1)[-1])
    with Session(engine) as s:
        assert s.get(Connection, gconn_id).customer_id is None

    # scoped create with a valid ACTIVE customer still works
    r = c.post(
        "/connectors/jira",
        data={"name": "ConnScopedTest", "customer_id": str(active_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/connectors/connection/" in _location(r)
    sconn_id = int(_location(r).rstrip("/").rsplit("/", 1)[-1])
    with Session(engine) as s:
        assert s.get(Connection, sconn_id).customer_id == active_id

    # update to a valid active customer still works
    r = c.post(
        f"/connectors/connection/{gconn_id}",
        data={"name": "ConnGlobalTest", "customer_id": str(active_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "ok=1" in _location(r)
    with Session(engine) as s:
        assert s.get(Connection, gconn_id).customer_id == active_id
    print(
        "  T2-01 connector create/update: global + valid-active-customer scoping both still work OK"
    )


# ============================================================================
# T2-03 -- glossary pair / term locale catalog validation
# ============================================================================
def test_glossary_pair_invalid_locale_refused_fail_pre_fix():
    _auth_off()

    # FAIL-PRE-FIX PROOF: the pre-fix `_canon` (git show
    # HEAD:app/services/glossary_module.py) fell back to a separator/casing-only
    # transform for any catalog-unknown code. Reproduce that fallback inline:
    def _old_canon_fallback(s):
        parts = s.replace("_", "-").split("-")
        parts[0] = parts[0].lower()
        for i in range(1, len(parts)):
            parts[i] = parts[i].upper()
        return "-".join(parts)

    valid, invalid = languages_catalog.validate_codes(["not-A-LOCALE"])
    assert invalid and not valid, "'not-A-LOCALE' must not be a catalog-known code (setup)"
    old_stored = _old_canon_fallback("not-A-LOCALE")
    assert old_stored == "not-A-LOCALE", (
        "pre-fix fallback stores the garbage code VERBATIM -- reproduces the exact "
        "bug-log symptom ('stores not-A-LOCALE verbatim')"
    )

    with Session(engine) as s:
        g = gm.create(s, "PairLocaleTest")
        gid = g.id

    c = TestClient(app)
    r = c.post(
        f"/glossaries/{gid}/pairs",
        data={"source_locale": ["en-US"], "target_locale": ["not-A-LOCALE"]},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in _location(r), (
        "posting a pair with an unknown locale code must redirect with an error "
        f"(got {r.status_code} -> {_location(r)!r})"
    )
    with Session(engine) as s:
        assert gm.pairs_for(s, gid) == [], (
            "no pair may be stored when either side is not a catalog locale code"
        )

    # service-level guard directly too (not just the route)
    with Session(engine) as s:
        try:
            gm.set_pairs(s, gid, [("en-US", "not-A-LOCALE")])
            assert False, "gm.set_pairs must raise for an unknown locale code"
        except gm.GlossaryModuleError:
            pass
        assert gm.pairs_for(s, gid) == []
    print(
        "  T2-03 glossary pairs: unknown locale code refused on service + web; "
        "fail-pre-fix proven OK"
    )


def test_glossary_pair_valid_locale_accepted():
    _auth_off()
    with Session(engine) as s:
        g = gm.create(s, "PairValidTest")
        gid = g.id
    c = TestClient(app)
    r = c.post(
        f"/glossaries/{gid}/pairs",
        data={"source_locale": ["en-US"], "target_locale": ["es-ES"]},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in _location(r)
    with Session(engine) as s:
        pairs = gm.pairs_for(s, gid)
        assert any(p["target_locale"] == "es-ES" for p in pairs)
    print("  T2-03 glossary pairs: a valid catalog locale pair is still accepted OK")


def test_glossary_term_invalid_locale_refused():
    """The bug-log follow-up: glossary TERM-add locales were equally unchecked
    pre-fix (add_term stored source_locale/target_locale verbatim, no catalog
    check) -- now validated via the same _canon gate as pairs."""
    _auth_off()
    with Session(engine) as s:
        g = gm.create(s, "TermLocaleTest")
        gid = g.id
        try:
            gm.add_term(
                s,
                gid,
                "widget",
                target_term="dispositivo",
                source_locale="en-US",
                target_locale="not-A-LOCALE",
            )
            assert False, "add_term must raise for an unknown target locale code"
        except gm.GlossaryModuleError:
            pass
        # nothing was stored
        assert gm.list_entries(s, gid) == []

    c = TestClient(app)
    r = c.post(
        f"/glossaries/{gid}/terms/add",
        data={
            "source_term": "widget",
            "target_term": "dispositivo",
            "source_locale": "en-US",
            "target_locale": "not-A-LOCALE",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in _location(r), (
        "adding a term with an unknown locale code must redirect with an error "
        f"(got {r.status_code} -> {_location(r)!r})"
    )
    with Session(engine) as s:
        assert gm.list_entries(s, gid) == []
    print("  T2-03 glossary term-add: unknown locale code refused on service + web OK")


def test_glossary_term_blank_and_valid_locale_still_accepted():
    """Blank locale (the 'any locale' wildcard term) and a valid catalog code must
    both keep working -- the fix must not regress the legitimate cases."""
    _auth_off()
    with Session(engine) as s:
        g = gm.create(s, "TermPositiveTest")
        gid = g.id
        # blank/any locale
        e1 = gm.add_term(s, gid, "anylocale-term", target_term="x")
        assert e1 is not None and e1.source_locale == "" and e1.target_locale == ""
        # valid catalog code
        e2 = gm.add_term(
            s,
            gid,
            "widget",
            target_term="dispositivo",
            source_locale="en-US",
            target_locale="es-ES",
        )
        assert e2 is not None and e2.source_locale == "en-US" and e2.target_locale == "es-ES"
    print(
        "  T2-03 glossary term-add: blank ('any locale') and a valid catalog code "
        "both still work OK"
    )


# ============================================================================
# T2-02 non-disclosure (adversarial-review fix) -- the archived-refusal message
# must not become a cross-org existence/name oracle under multi_org ON.
# ============================================================================
def test_archived_refusal_no_cross_org_disclosure():
    """Adversarial-review defect: `_valid_customer_id` resolved the customer with
    session.get(Customer, cid) WITHOUT a same-org check, then put cust.name in the
    "archived" message. Under multi_org ON, a forged POST from an org-A caller naming
    an org-B ARCHIVED customer leaked (a) that the foreign customer exists and (b) its
    NAME (into ?err=) -- an oracle a foreign-active/nonexistent id never gave.

    Post-fix: any foreign-org id (archived/active/nonexistent alike) falls through to
    the IDENTICAL generic "not available in this scope" response, and the same-org
    archived refusal echoes the ID ONLY (never the name).
    """
    _auth_on(multi_org=True)
    try:
        org_a = _org("DiscOrgA")
        org_b = _org("DiscOrgB")
        # A DISTINCTIVE org-B customer name so a leak is unmistakable in ?err=.
        b_secret_name = "ZZ-OrgB-Secret-Acme-Corp"
        b_archived = _customer(b_secret_name, org_id=org_b)
        _archive(b_archived)
        b_active = _customer("OrgB-Active-Customer", org_id=org_b)
        # a same-org (org-A) archived customer, for the positive archived-refusal case
        a_archived = _customer("OrgA-Archived-Customer", org_id=org_a)
        _archive(a_archived)
        nonexistent = 9_000_000  # no such row

        # FAIL-PRE-THIS-FIX PROOF: reproduce the pre-review-fix branch inline (it
        # resolved the row with NO org check and interpolated cust.name). It LEAKS the
        # org-B name to an org-A caller.
        with Session(engine) as s:
            leaked_row = s.get(Customer, b_archived)
            old_msg = (
                f"customer {b_archived} ({leaked_row.name}) is archived and cannot be selected."
            )
        assert b_secret_name in old_msg, (
            "pre-fix message interpolated the FOREIGN customer's name -- the exact "
            "cross-org disclosure oracle this fix closes"
        )

        # --- glossaries: org-A caller POSTs with the org-B archived id --------------
        ga = _login("disc-gloss-a@x.test", org_id=org_a)
        r_arch = ga.post(
            "/glossaries",
            data={"name": "DiscGlossArch", "customer_id": str(b_archived)},
            follow_redirects=False,
        )
        r_active = ga.post(
            "/glossaries",
            data={"name": "DiscGlossActive", "customer_id": str(b_active)},
            follow_redirects=False,
        )
        r_none = ga.post(
            "/glossaries",
            data={"name": "DiscGlossNone", "customer_id": str(nonexistent)},
            follow_redirects=False,
        )
        loc_arch, loc_active, loc_none = (_location(r_arch), _location(r_active), _location(r_none))
        # all three redirect with an error, none create a row
        for loc in (loc_arch, loc_active, loc_none):
            assert "err=" in loc, "a foreign/invalid customer_id must redirect with err"

        # Decode the err message. glossaries redirects the raw str(exc); content-types
        # quote()s it -- unquote handles both uniformly (a no-op on the unencoded one).
        from urllib.parse import unquote

        def _errmsg(loc):
            return unquote(loc.split("err=", 1)[1]) if "err=" in loc else ""

        m_arch, m_active, m_none = _errmsg(loc_arch), _errmsg(loc_active), _errmsg(loc_none)
        assert b_secret_name not in m_arch, (
            "the org-B customer NAME must NOT appear in the ?err= redirect "
            f"(cross-org name disclosure): {m_arch!r}"
        )
        assert "archived" not in m_arch.lower(), (
            "a FOREIGN-org archived id must not reveal the archived-specific reason "
            f"(existence oracle): {m_arch!r}"
        )
        # The REASON must be the generic scope message for EVERY foreign/invalid id
        # (the echoed id is the caller's OWN input, not disclosure) -- so
        # foreign-archived is byte-indistinguishable from foreign-active + nonexistent.
        assert m_arch == f"customer {b_archived} is not available in this scope."
        assert m_active == f"customer {b_active} is not available in this scope."
        assert m_none == f"customer {nonexistent} is not available in this scope.", (
            "foreign-archived / foreign-active / nonexistent must all yield the SAME "
            f"generic reason (only the caller's own id differs): {m_arch!r} / {m_active!r} / {m_none!r}"  # noqa: E501
        )
        with Session(engine) as s:
            assert (
                s.exec(
                    select(Glossary).where(
                        Glossary.name.in_(["DiscGlossArch", "DiscGlossActive", "DiscGlossNone"])
                    )
                ).first()
                is None
            ), "no glossary row may be created for any foreign/invalid customer_id"

        # --- content-types: same three POSTs, same assertions ----------------------
        r_arch = ga.post(
            "/content-types",
            data={"name": "DiscCtArch", "customer_id": str(b_archived)},
            follow_redirects=False,
        )
        r_active = ga.post(
            "/content-types",
            data={"name": "DiscCtActive", "customer_id": str(b_active)},
            follow_redirects=False,
        )
        r_none = ga.post(
            "/content-types",
            data={"name": "DiscCtNone", "customer_id": str(nonexistent)},
            follow_redirects=False,
        )
        loc_arch, loc_active, loc_none = (_location(r_arch), _location(r_active), _location(r_none))
        m_arch, m_active, m_none = _errmsg(loc_arch), _errmsg(loc_active), _errmsg(loc_none)
        assert b_secret_name not in m_arch, (
            f"content-type ?err= leaked the org-B customer name: {m_arch!r}"
        )
        assert "archived" not in m_arch.lower(), (
            f"content-type foreign-archived id revealed archived reason: {m_arch!r}"
        )
        assert m_arch == f"customer {b_archived} is not available in this scope."
        assert m_active == f"customer {b_active} is not available in this scope."
        assert m_none == f"customer {nonexistent} is not available in this scope.", (
            "content-type foreign responses must be the same generic reason (no oracle)"
        )

        # --- positive: a SAME-ORG (org-A) archived customer STILL gets the clean
        #     archived refusal (id only, no name) --------------------------------
        r_same = ga.post(
            "/glossaries",
            data={"name": "DiscGlossSameArch", "customer_id": str(a_archived)},
            follow_redirects=False,
        )
        loc_same = _location(r_same)
        assert "err=" in loc_same and "archived" in loc_same.lower(), (
            "a SAME-ORG archived customer must still get the archived-specific "
            f"refusal: {loc_same!r}"
        )
        assert "OrgA-Archived-Customer" not in loc_same, (
            "even the same-org archived message must echo the ID ONLY, never the "
            f"name (matching content.project_create's contract): {loc_same!r}"
        )
        assert str(a_archived) in loc_same, "the archived message should carry the id"
        print(
            "  T2-02 non-disclosure: foreign-org archived id gives the generic "
            "non-disclosing response (no name, no archived oracle) on glossaries + "
            "content-types; same-org archived still refused (id only); "
            "fail-pre-fix proven OK"
        )
    finally:
        _auth_off()


def main():
    # T2-02
    test_glossary_create_archived_customer_refused_fail_pre_fix()
    test_glossary_create_positive_cases()
    test_content_type_create_archived_customer_refused_fail_pre_fix()
    test_content_type_create_positive_cases()
    # T2-02 non-disclosure (adversarial-review fix)
    test_archived_refusal_no_cross_org_disclosure()
    # T2-01 (connectors part)
    test_connector_create_archived_customer_refused_fail_pre_fix()
    test_connector_update_archived_customer_refused()
    test_connector_create_and_update_positive_cases()
    # T2-03
    test_glossary_pair_invalid_locale_refused_fail_pre_fix()
    test_glossary_pair_valid_locale_accepted()
    test_glossary_term_invalid_locale_refused()
    test_glossary_term_blank_and_valid_locale_still_accepted()
    print("ARCHIVED SCOPE GLOSSARY VALIDATION OK")


if __name__ == "__main__":
    main()
