"""#586: on every route that takes a project / document / segment / key / customer id in the
FORM, JSON body or QUERY STRING (not the path), a MISSING id and an id the caller cannot SEE
answer identically -- hermetic.

#575 covered path ids (verify_missing_hidden_web_routes). POST /queries and POST /api/queries
still let a body-borne id leak: `resolve_scope` raised "no such segment." for a missing id
before the visibility ring ran, so a hidden id answered 404 "Not found." / 403 / "no such
query thread." instead.

The route table below lists every body / query id route that was checked (the survey is in
the queries-586 survey notes). Each entry is called once with an id of an object owned by Customer A
(hidden from the caller) and once with an id that never existed, as the same three callers
#575 uses:
  customer: ADMIN bound to Customer B      viewer: VIEWER bound to Customer B
  scoped:   TRANSLATOR, project_scoping ON, not a member of the project (hidden = 403 today)
and the status, body (ids and CSP nonces normalised), Location, content type and every other
response header must match. /api routes are called with an API key of the same caller.

Final line: "MISSING HIDDEN BODY IDS OK".
"""

import os
import re
import sys
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_misshidbody_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Glossary,
    Project,
    ProjectMember,
    Role,
    Segment,
    StringKey,
    TransMode,
    User,
)
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

init_db()

_PW = "misshidbody-fixture-pass-1"
_GONE = 987654

# (surface, method, path, {field: kind}, fixed fields). `kind` picks which fixture id is
# the hidden one: project / doc / seg / key / cust. Fixed fields are sent as-is.
ROUTES = [
    # the routes #586 names
    ("web", "POST", "/queries", {"segment_id": "seg"}, {"subject": "s", "body": "b"}),
    ("web", "POST", "/queries", {"document_id": "doc"}, {"subject": "s", "body": "b"}),
    ("web", "POST", "/queries", {"project_id": "project"}, {"subject": "s", "body": "b"}),
    ("web", "POST", "/queries", {"key_id": "key"}, {"subject": "s", "body": "b"}),
    # a hidden segment next to a project id that does not match it: the 400 "anchors
    # disagree" must not tell a hidden segment from a missing one
    ("web", "POST", "/queries", {"segment_id": "seg", "project_id": "other"}, {"subject": "s"}),
    # gate: a VISIBLE anchor (the caller's own segment / document) next to a coarser anchor
    # that is hidden or missing: a missing one must not answer "anchors disagree" while a
    # hidden one answers the ring
    (
        "web",
        "POST",
        "/queries",
        {"segment_id": "ownseg", "project_id": "project"},
        {"subject": "s"},
    ),
    ("web", "POST", "/queries", {"segment_id": "ownseg", "document_id": "doc"}, {"subject": "s"}),
    (
        "web",
        "POST",
        "/queries",
        {"document_id": "owndoc", "project_id": "project"},
        {"subject": "s"},
    ),
    (
        "api",
        "POST",
        "/api/queries",
        {"segment_id": "ownseg", "project_id": "project"},
        {"subject": "s"},
    ),
    (
        "api",
        "POST",
        "/api/queries",
        {"segment_id": "ownseg", "document_id": "doc"},
        {"subject": "s"},
    ),
    (
        "api",
        "POST",
        "/api/queries",
        {"document_id": "owndoc", "project_id": "project"},
        {"subject": "s"},
    ),
    ("api", "POST", "/api/queries", {"segment_id": "seg"}, {"subject": "s", "body": "b"}),
    ("api", "POST", "/api/queries", {"document_id": "doc"}, {"subject": "s", "body": "b"}),
    ("api", "POST", "/api/queries", {"project_id": "project"}, {"subject": "s", "body": "b"}),
    ("api", "POST", "/api/queries", {"key_id": "key"}, {"subject": "s", "body": "b"}),
    ("api", "POST", "/api/queries", {"segment_id": "seg", "project_id": "other"}, {"subject": "s"}),
    # the other body / query id routes surveyed (see the survey notes)
    ("web", "POST", "/tm/bulk-delete", {"project_id": "project"}, {"confirm": "yes"}),
    ("web", "POST", "/routing/project-budgets", {"project_id": "project"}, {"budget": "5"}),
    ("web", "POST", "/projects", {"customer_id": "cust"}, {"name": "new-p"}),
    ("web", "POST", "/projects/from-template", {"customer_id": "cust"}, {"name": "new-p"}),
    ("web", "POST", "/workflows", {"customer_id": "cust"}, {"name": "new-w"}),
    ("web", "POST", "/glossaries", {"customer_id": "cust"}, {"name": "new-g"}),
    ("web", "POST", "/style-guides", {"customer_id": "cust"}, {"name": "new-s"}),
    ("web", "POST", "/content-types", {"customer_id": "cust"}, {"name": "new-c"}),
    ("web", "POST", "/templates/prompts", {"customer_id": "cust"}, {"name": "new-t"}),
    ("web", "POST", "/workflow-groups", {"customer_id": "cust"}, {"name": "new-wg"}),
    ("web", "POST", "/settings/segprofiles", {"customer_id": "cust"}, {"name": "new-sp"}),
    ("web", "POST", "/users", {"customer_id": "cust"}, {"email": "n@x.com", "name": "n"}),
    ("web", "POST", "/users/{victim}/customer", {"customer_id": "cust"}, {}),
    (
        "web",
        "POST",
        "/settings/segprofiles/{sprof}/clone",
        {"customer_id": "cust"},
        {"new_name": "cl"},
    ),
    # found by the route-table guard below (query-string and form ids the first survey skipped)
    ("web", "POST", "/projects/templates/from-draft", {"customer_id": "cust"}, {"name": "tpl"}),
    ("web", "POST", "/tm/bulk-delete", {"customer": "cust"}, {"confirm": "yes"}),
    ("web", "POST", "/routing", {"project_id": "project"}, {"name": "r", "match_text": "x"}),
    ("web", "GET", "/concordance", {"project_id": "project"}, {"q": "hello"}),
    ("web", "GET", "/jobs", {"document": "doc"}, {}),
    ("api", "GET", "/api/jobs", {"document": "doc"}, {}),
    ("api", "GET", "/api/context/resolve", {"project": "project"}, {}),
    ("api", "GET", "/api/context/resolve", {"customer": "cust"}, {}),
    ("api", "GET", "/api/context/resolve", {"document": "doc"}, {}),
    ("api", "GET", "/api/audit/export", {"customer_id": "cust"}, {}),
    # multipart uploads: the project id rides in the form beside the file
    ("apiform", "POST", "/api/documents", {"project_id": "project"}, {"mode": "segmented"}),
    ("apiform", "POST", "/api/images", {"project_id": "project"}, {}),
    ("form", "POST", "/images/upload", {"project_id": "project"}, {}),
    # a body id naming a SECOND object next to a path id the caller CAN see (their own
    # project / a global glossary): the second id must not tell hidden from missing either
    ("web", "POST", "/projects/{own}/tm-sources/add", {"source_project_id": "project"}, {}),
    ("web", "POST", "/projects/{own}/tm-sources/remove", {"source_project_id": "project"}, {}),
    ("web", "POST", "/projects/{own}/glossaries/add", {"glossary_id": "gloss"}, {}),
    ("web", "POST", "/projects/{own}/glossaries/remove", {"glossary_id": "gloss"}, {}),
    ("web", "POST", "/projects/{own}/set-customer", {"customer_id": "cust"}, {}),
    ("web", "POST", "/glossaries/{gglob}/extract", {"document_id": "doc"}, {}),
    ("web", "POST", "/glossaries/{gglob}/assign-customer", {"customer_id": "cust"}, {}),
    # custom project fields (SPEC-project-automation part C): a customer scope in the form
    (
        "web",
        "POST",
        "/settings/custom-fields",
        {"customer_id": "cust"},
        {"key": "probe", "label": "Probe", "type": "text"},
    ),
]

# Fields that carry a project / document / segment / key / customer id (or a list of them) in
# a form, query string or JSON body. The route-table guard below enumerates the live app for
# these and fails when a route that takes one is neither in ROUTES nor in EXEMPT.
_FAMILY_FIELDS = {
    "project_id",
    "project",
    "document_id",
    "document",
    "document_ids",
    "doc_id",
    "segment_id",
    "segment_ids",
    "key_id",
    "customer_id",
    "customer",
    "source_project_id",
}

# (METHOD, path with every {param} written {}) -> why the route is not probed here. A NEW
# route with a family field must be added to ROUTES (preferred) or listed here with a reason.
EXEMPT = {
    (
        "GET",
        "/workflows/{}/plan",
    ): "global workflow path id + doc_id: covered by verify_missing_hidden_web_routes",
    (
        "GET",
        "/api/workflows/{}/plan",
    ): "global workflow path id + doc_id: covered by verify_missing_hidden_web_routes",
    (
        "POST",
        "/tm/{}/delete",
    ): "path TM entry id; the customer field is a filter echo, not a referent",
    (
        "POST",
        "/vendors/{}/link-customer",
    ): "vendor catalog: the service owns the customer ring (vendors.link_customer)",
    (
        "POST",
        "/vendors/{}/unlink-customer",
    ): "vendor catalog: the service owns the customer ring (vendors.unlink_customer)",
}

# kinds whose id is the same on the hidden and the missing call
_FIXED_KINDS = {"other", "ownseg", "owndoc"}

# create routes that redirect to /<family>/<new id>
_CREATES = {"/glossaries"}

_VOLATILE = {
    "date",
    "set-cookie",
    "x-request-id",
    "server-timing",
    "content-length",
    "etag",
    "x-response-time",
}


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _mk(s, obj):
    s.add(obj)
    s.commit()
    s.refresh(obj)
    return obj


def _norm(text, ids):
    text = unquote(text)
    text = re.sub(r'nonce="[^"]*"|name="csrf[^>]*>|csrf_token[^>]*>|csrf-token[^>]*>', "", text)
    text = re.sub(r"'nonce-[^']*'", "'nonce-X'", text)
    for v in (*ids, _GONE):
        text = re.sub(r"\b" + re.escape(str(v)) + r"\b", "ID", text)
    return text


def _loc(value, path, ids):
    """A Location header, normalised. A create route redirects to the NEW row, whose id
    differs on every call, so it is masked there."""
    out = _norm(value, ids)
    if path in _CREATES:
        out = re.sub(r"/[0-9]+$", "/N", out)
    return out


def _norm_path(p):
    return re.sub(r"\{[^}]*\}", "{}", p)


def _live_family_routes():
    """(METHOD, path) of every route in the live app that declares a family id field in its
    form, query string or body schema (read from the OpenAPI document, which flattens the
    included routers). Routes that parse a JSON body by hand (/api/queries) do not declare
    fields and are listed in ROUTES explicitly."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        schema = app.openapi()
    comps = schema.get("components", {}).get("schemas", {})
    found = set()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            names = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
            for c in op.get("requestBody", {}).get("content", {}).values():
                sch = c.get("schema", {})
                if "$ref" in sch:
                    sch = comps.get(sch["$ref"].split("/")[-1], {})
                names |= set(sch.get("properties", {}))
            if names & _FAMILY_FIELDS:
                found.add((method.upper(), _norm_path(path)))
    return found


def _guard_route_table():
    """Fail when the app gained (or already has) a route taking a project / document /
    segment / key / customer id in the body or query string that this suite neither probes
    nor exempts with a reason; also fail on stale EXEMPT entries."""
    listed = {(m, _norm_path(p)) for _s, m, p, _f, _x in ROUTES}
    live = _live_family_routes()
    unlisted = sorted(live - listed - set(EXEMPT))
    stale = sorted(set(EXEMPT) - live)
    if unlisted:
        _fail(
            "body/query-id routes neither probed in ROUTES nor in EXEMPT (add them): "
            + "; ".join(f"{m} {p}" for m, p in unlisted)
        )
    if stale:
        _fail("EXEMPT entries no longer match a route: " + "; ".join(f"{m} {p}" for m, p in stale))


def _legit_callers_still_work(ids, own_id, cust_b, keys):
    """The refusals above must not block a caller who may do the thing: an UNBOUND admin
    (internal staff), a customer-bound admin acting on its OWN customer / project, and (with
    multi-org on) an org admin acting on a customer of its OWN org."""
    from app.models import Organization

    def _login(email):
        w = TestClient(app)
        csrf_for(w)
        r = w.post(
            "/auth/login",
            data={"email": email, "password": _PW, "next": "/"},
            follow_redirects=False,
        )
        if r.status_code != 303:
            _fail(f"login of {email} failed: {r.status_code}")
        csrf_for(w)
        return w

    def _loc(r):
        return unquote(r.headers.get("location", ""))

    security.create_user("mhb-staff@x.com", "staff", Role.ADMIN, _PW)
    staff = _login("mhb-staff@x.com")
    bound = _login("mhb-c@x.com")  # ADMIN bound to Customer B
    staff_key = apikeys.create_key(
        security.get_by_email("mhb-staff@x.com").id, "mhb-staff", scopes="api docs"
    )[1]
    bound_key = keys["customer"]
    # a Customer A audit row, so the export leak checks below are not vacuous
    from app.services import audit as _audit

    _audit.log(actor="mhb-staff@x.com", action="mhb.probe.customer-a", customer_id=ids["cust"])

    def _post(w, path, **data):
        return w.post(path, data=data, follow_redirects=False)

    # unbound admin: any customer / project
    r = _post(
        staff, "/users", email="legit1@x.com", name="l", customer_id=str(ids["cust"]), role="viewer"
    )
    if "error" in _loc(r):
        _fail(f"unbound admin could not bind a new user to a customer: {_loc(r)}")
    r = _post(staff, "/settings/segprofiles", name="legit-sp", customer_id=str(ids["cust"]))
    if "srx_error" in _loc(r):
        _fail(f"unbound admin could not create a customer profile: {_loc(r)}")
    r = _post(
        staff, "/routing/project-budgets", project_id=str(ids["project"]), monthly_usd_limit="5"
    )
    if "error" in _loc(r):
        _fail(f"unbound admin could not save a project budget: {_loc(r)}")
    r = _post(staff, "/routing", project_id=str(ids["project"]), priority="100", match_ext="docx")
    if "error" in _loc(r):
        _fail(f"unbound admin could not create a routing rule for a project: {_loc(r)}")
    r = staff.get("/queries", follow_redirects=False)
    r = _post(staff, "/queries", segment_id=str(ids["seg"]), subject="legit", body="hello")
    if r.status_code >= 400:
        _fail(f"unbound admin could not open a query on a visible segment: {r.status_code}")
    anon = TestClient(app)
    r = anon.get(
        "/api/audit/export",
        params={"customer_id": ids["cust"]},
        headers={"X-Loc-Token": staff_key},
    )
    if r.status_code != 200:
        _fail(f"unbound admin audit export for a customer answered {r.status_code}")
    r = anon.post(
        "/api/queries",
        json={"segment_id": ids["seg"], "subject": "s", "body": "b"},
        headers={"X-Loc-Token": staff_key},
    )
    if r.status_code != 200:
        _fail(f"unbound admin API query open on a visible segment answered {r.status_code}")

    # custom fields: staff may scope a field to Customer A; a bound admin naming Customer A
    # gets a field of its OWN customer (never A's), exactly as naming a missing id would
    from app.models import CustomFieldDef

    r = _post(
        staff, "/settings/custom-fields", key="legit_cf1", label="L", customer_id=str(ids["cust"])
    )
    if "err=" in _loc(r):
        _fail(f"unbound admin could not define a customer field: {_loc(r)}")
    r = _post(
        bound, "/settings/custom-fields", key="legit_cf2", label="L", customer_id=str(ids["cust"])
    )
    if "err=" in _loc(r):
        _fail(f"bound admin could not define a field: {_loc(r)}")
    with Session(engine) as s:
        rows = {d.key: d.customer_id for d in s.exec(select(CustomFieldDef)).all()}
    if rows.get("legit_cf1") != ids["cust"]:
        _fail(f"unbound admin field scope wrong: {rows.get('legit_cf1')!r}")
    if rows.get("legit_cf2") != cust_b:
        _fail(f"bound admin field landed outside its own customer: {rows.get('legit_cf2')!r}")

    # customer-bound admin: its OWN customer
    r = _post(
        bound, "/users", email="legit2@x.com", name="l", customer_id=str(cust_b), role="viewer"
    )
    if "error" in _loc(r):
        _fail(f"bound admin could not bind a user to its own customer: {_loc(r)}")
    r = _post(bound, "/settings/segprofiles", name="legit-sp2", customer_id=str(cust_b))
    if "srx_error" in _loc(r):
        _fail(f"bound admin could not create a profile for its own customer: {_loc(r)}")
    r = _post(bound, "/routing/project-budgets", project_id=str(own_id), monthly_usd_limit="5")
    if "error" in _loc(r):
        _fail(f"bound admin could not save a budget for its own project: {_loc(r)}")
    r = _post(bound, "/routing", project_id=str(own_id), priority="100", match_ext="pdf")
    if "error" in _loc(r):
        _fail(f"bound admin could not create a rule for its own project: {_loc(r)}")
    # export rows carry no customer_id field, so the leak checks look for the Customer A
    # marker row written above (gate: the earlier customer_id text match was vacuous)
    foreign = "mhb.probe.customer-a"
    r = anon.get("/api/audit/export", headers={"X-Loc-Token": staff_key})
    if r.status_code != 200 or foreign not in r.text:
        _fail(f"unbound admin API audit export lacks the Customer A row ({r.status_code})")
    for qs in ({}, {"customer_id": cust_b}):
        r = anon.get("/api/audit/export", params=qs, headers={"X-Loc-Token": bound_key})
        if r.status_code != 200:
            _fail(f"bound admin audit export {qs} answered {r.status_code}")
        if foreign in r.text:
            _fail(f"bound admin audit export {qs} leaked another customer's rows")
    # the web /audit/export twin (gate): staff see Customer A's rows, the bound admin does not
    r = staff.get("/audit/export", follow_redirects=False)
    if r.status_code != 200 or foreign not in r.text:
        _fail(f"unbound admin web audit export lacks the Customer A row ({r.status_code})")
    r = bound.get("/audit/export", follow_redirects=False)
    if r.status_code != 200:
        _fail(f"bound admin web audit export answered {r.status_code}")
    if foreign in r.text:
        _fail("bound admin web audit export leaked another customer's rows")

    # multi-org on: an org admin acting on a customer of its OWN org
    with Session(engine) as s:
        org = _mk(s, Organization(name="mhb Org"))
        org_id = org.id
        for model, pk in ((Customer, ids["cust"]), (Project, ids["project"])):
            row = s.get(model, pk)
            row.org_id = org_id
            s.add(row)
        s.commit()
    security.create_user("mhb-orgadmin@x.com", "oa", Role.ADMIN, _PW, org_id=org_id)
    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        oa = _login("mhb-orgadmin@x.com")
        oa_key = apikeys.create_key(
            security.get_by_email("mhb-orgadmin@x.com").id, "mhb-oa", scopes="api docs"
        )[1]
        r = _post(
            oa,
            "/users",
            email="legit3@x.com",
            name="l",
            customer_id=str(ids["cust"]),
            role="viewer",
        )
        if "error" in _loc(r):
            _fail(f"org admin could not bind a user to a customer of its own org: {_loc(r)}")
        r = _post(
            oa, "/routing/project-budgets", project_id=str(ids["project"]), monthly_usd_limit="5"
        )
        if "error" in _loc(r):
            _fail(f"org admin could not save a budget for a project of its own org: {_loc(r)}")
        r = anon.get(
            "/api/audit/export",
            params={"customer_id": ids["cust"]},
            headers={"X-Loc-Token": oa_key},
        )
        if r.status_code != 200:
            _fail(f"org admin audit export of a customer of its own org answered {r.status_code}")
    finally:
        settings.set_many({"multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  legitimate admins (unbound, bound-on-own, org-on-own-org) still pass")


def main():
    _guard_route_table()
    with Session(engine) as s:
        a, b = _mk(s, Customer(name="Cust A")), _mk(s, Customer(name="Cust B"))
        proj = _mk(s, Project(name="mhb-a", source_locale="en-US", customer_id=a.id))
        other = _mk(s, Project(name="mhb-other", source_locale="en-US", customer_id=a.id))
        doc = _mk(
            s, Document(project_id=proj.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
        )
        seg = _mk(
            s,
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=0,
                source_text="hello",
                target_text="",
            ),
        )
        own = _mk(s, Project(name="mhb-own", source_locale="en-US", customer_id=b.id))
        owndoc = _mk(
            s, Document(project_id=own.id, filename="o.txt", fmt="txt", mode=TransMode.SEGMENTED)
        )
        ownseg = _mk(
            s,
            Segment(
                document_id=owndoc.id,
                target_locale="es-ES",
                order_index=0,
                source_text="own",
                target_text="",
            ),
        )
        gloss = _mk(s, Glossary(name="mhb-gloss-a", customer_id=a.id))
        gglob = _mk(s, Glossary(name="mhb-gloss-global"))
        key = _mk(s, StringKey(project_id=proj.id, name="mhb.k"))
        ids = {
            "project": proj.id,
            "doc": doc.id,
            "seg": seg.id,
            "key": key.id,
            "cust": a.id,
            "other": other.id,
            "gloss": gloss.id,
            "ownseg": ownseg.id,
            "owndoc": owndoc.id,
        }
        from app.models import SegmentationProfile

        sprof = _mk(s, SegmentationProfile(name="mhb-prof"))
        victim = _mk(
            s, User(email="mhb-victim@x.com", name="v", role=Role.VIEWER, password_hash="x")
        )
        fixed_path = {"own": own.id, "gglob": gglob.id, "sprof": sprof.id, "victim": victim.id}
        own_id = own.id
        cust_b = b.id

    callers = {}
    keys = {}
    for tag, email, role, bound in (
        ("customer", "mhb-c@x.com", Role.ADMIN, True),
        ("viewer", "mhb-v@x.com", Role.VIEWER, True),
        ("scoped", "mhb-s@x.com", Role.TRANSLATOR, False),
    ):
        u = security.create_user(email, tag, role, _PW)
        if bound:
            with Session(engine) as s:
                row = s.get(User, u.id)
                row.customer_id = cust_b
                s.add(row)
                s.commit()
        callers[tag] = email
        if not bound:  # the scoped caller is a member of its OWN project only
            with Session(engine) as s:
                _mk(s, ProjectMember(project_id=own_id, user_id=u.id))
        keys[tag] = apikeys.create_key(u.id, "mhb-" + tag, scopes="api docs")[1]
    settings.set_many(
        {"security_auth_required": "1", "project_scoping": "1", "queries_enabled": "1"}
    )
    settings.invalidate_cache()

    def _login(tag, email):
        w = TestClient(app)
        csrf_for(w)
        r = w.post(
            "/auth/login",
            data={"email": email, "password": _PW, "next": "/"},
            follow_redirects=False,
        )
        if r.status_code != 303:
            _fail(f"login of {tag} failed: {r.status_code}")
        csrf_for(w)
        return w

    web = {tag: _login(tag, email) for tag, email in callers.items()}
    anon = TestClient(app)

    problems = []
    tried = 0
    seq = [0]
    hidden_ids = [*ids.values()]
    for surface, method, path, fields, fixed in ROUTES:
        for tag in callers:
            if tag == "scoped" and set(fields.values()) <= {"cust"}:
                continue  # project scoping hides projects only; every customer stays visible
            out = {}
            for kind, hidden in (("hidden", True), ("missing", False)):
                # unique per call so a create route never collides with its own earlier row
                seq[0] += 1
                data = {
                    k: (v.replace("@x.com", f"{seq[0]}@x.com") + f"-{seq[0]}" if k == "name" else v)
                    for k, v in fixed.items()
                }
                if "email" in data:
                    data["email"] = f"n{seq[0]}@x.com"
                if path == "/settings/custom-fields":
                    data["key"] = f"{data['key']}_{seq[0]}"  # a field key is unique per scope
                for name, k in fields.items():
                    # an "other" anchor is a REAL project the caller also cannot see; an
                    # "own*" anchor is one the caller CAN see: both stay fixed on each side
                    data[name] = ids[k] if (hidden or k in _FIXED_KINDS) else _GONE
                try:
                    url = path.format(**fixed_path)
                    if surface in ("apiform", "form"):
                        upl = {"file": ("a.png", b"not-really-a-png", "image/png")}
                        if path == "/api/documents":
                            upl = {"file": ("a.txt", b"hello world", "text/plain")}
                    if surface == "apiform":
                        resp = anon.request(
                            method,
                            url,
                            data=data,
                            files=upl,
                            headers={"X-Loc-Token": keys[tag]},
                            follow_redirects=False,
                        )
                    elif surface == "form":
                        resp = web[tag].request(
                            method, url, data=data, files=upl, follow_redirects=False
                        )
                    elif surface == "api" and method == "GET":
                        resp = anon.request(
                            method,
                            url,
                            params=data,
                            headers={"X-Loc-Token": keys[tag]},
                            follow_redirects=False,
                        )
                    elif surface == "web" and method == "GET":
                        resp = web[tag].request(method, url, params=data, follow_redirects=False)
                    elif surface == "api":
                        resp = anon.request(
                            method,
                            url,
                            json=data,
                            headers={"X-Loc-Token": keys[tag]},
                            follow_redirects=False,
                        )
                    else:
                        resp = web[tag].request(method, url, data=data, follow_redirects=False)
                    out[kind] = (
                        resp.status_code,
                        _norm(resp.text, hidden_ids)[:4000],
                        _loc(resp.headers.get("location", ""), path, hidden_ids),
                        tuple(
                            sorted(
                                (
                                    k,
                                    _loc(v, path, hidden_ids)
                                    if k.lower() == "location"
                                    else _norm(v, hidden_ids),
                                )
                                for k, v in resp.headers.items()
                                if k.lower() not in _VOLATILE
                            )
                        ),
                    )
                except Exception as e:  # a crash on one side only is itself a difference
                    out[kind] = ("EXC", repr(e)[:80], "", ())
            tried += 1
            if out["hidden"] != out["missing"]:
                h, g = out["hidden"], out["missing"]
                why = []
                if h[0] != g[0]:
                    why.append(f"status {h[0]} vs {g[0]}")
                if h[2] != g[2]:
                    why.append(f"location {h[2][:40]!r} vs {g[2][:40]!r}")
                if h[1] != g[1]:
                    at = next(
                        (i for i, (x, y) in enumerate(zip(h[1], g[1], strict=False)) if x != y),
                        min(len(h[1]), len(g[1])),
                    )
                    why.append(f"body differs: {h[1][at : at + 50]!r} vs {g[1][at : at + 50]!r}")
                if h[3] != g[3]:
                    why.append(f"headers {sorted(set(h[3]) ^ set(g[3]))[:3]}")
                problems.append(
                    f"{tag:8} {method:5} {path} {sorted(fields.items())}: " + "; ".join(why)
                )

    print(f"  {len(ROUTES)} body-id routes x {len(callers)} callers = {tried} comparisons")
    if problems:
        print(f"FAIL: {len(problems)} comparisons differ between a missing and a hidden id:")
        for line in problems:
            print("  ", line)
        sys.exit(1)

    _legit_callers_still_work(ids, own_id, cust_b, keys)

    settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
    settings.invalidate_cache()
    print("MISSING HIDDEN BODY IDS OK")


if __name__ == "__main__":
    main()
