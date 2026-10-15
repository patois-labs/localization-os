"""#575: on EVERY web route that takes a project / customer / document / run / segment id,
a MISSING id and an id the caller cannot SEE answer identically -- hermetic.

THE BUG (follow-up to #570): most web handlers began `if not project: return Redirect(...)`
BEFORE the visibility check, so a customer-bound or project-scoped caller got 303 for an id
that does not exist but 404 (or 403) for one that exists and is hidden: an existence oracle.
JSON-vs-HTML body mismatches (/segments/*, paragraphs, estimate, status.json) leaked the
same way.

How this suite works: it ENUMERATES the route families from the app's own router (so a route
added later is covered automatically), calls each method+path once with ids of objects owned
by Customer A and once with an id that never existed, as three callers:
  customer: ADMIN bound to Customer B      viewer: VIEWER bound to Customer B
  scoped:   TRANSLATOR, project_scoping ON, not a member of the project (hidden = 403 today)
and compares status, body (ids and CSP nonces normalised), Location, content type and every
other response header. Any difference fails the suite.

Final line: "MISSING HIDDEN WEB ROUTES OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_misshidweb_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    LQAResult,
    Organization,
    Project,
    Role,
    Segment,
    TransMode,
    User,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import security, settings
from app.web._routes import walk_routes
from tests._csrf import csrf_for

init_db()

_PW = "misshidweb-fixture-pass-1"
_GONE = 987654

# Route families under test: EVERY non-/api route with a path parameter that names a
# project, customer, document, run, batch, segment or LQA finding (see _KIND). Selecting by
# parameter, not by URL prefix, means a new route such as /images/{doc_id} or
# /lqa/findings/{fid} is covered the day it is added.
# Never invoked: ends the session or the process.
_SKIP = re.compile(r"logout|shutdown|restart|/stream|/events")

_KIND = {
    "project_id": "project",
    "pid": "project",
    "doc_id": "doc",
    "did": "doc",
    "document_id": "doc",
    "run_id": "run",
    "rid": "run",
    "batch_id": "batch",
    "bid": "batch",
    "cid": "cust",
    "customer_id": "cust",
    "sid": "seg",
    "seg_id": "seg",
    "segment_id": "seg",
    "fid": "finding",
    "finding_id": "finding",
}

# Gate (#575): selection is by parameter NAME, so a new route that named a project /
# document / run / customer id differently (say {p_id}) would be skipped silently. Every
# path parameter on a non-/api route must therefore be CLASSIFIED: in _KIND (compared above)
# or here, as a name that does not identify an object of those families (its own family,
# a child id under a _KIND parent, or not an object id at all). An unclassified name FAILS
# the suite, so the author has to decide which list it belongs in.
_OTHER = {
    "candidate_id",
    "card_id",
    "concept_id",
    "connection_id",
    "ct_id",
    "dataset_id",
    "eid",
    "entry_id",
    # custom project field definition (its own family): custom_fields.visible() scopes it
    # by org + customer and answers the same 404 for a missing or foreign id.
    "field_id",
    "gid",
    "glossary_id",
    "group_id",
    "guide_id",
    "image_id",
    "index",
    "invite_id",
    "item_id",
    "jid",
    "key",
    "key_id",
    "kid",
    "kind",
    "lens_id",
    "locale",
    "memory_id",
    "mission_id",
    "module",
    "page_key",
    "provider",
    "qid",
    "run_uuid",
    "slug",
    "state",
    "step_id",
    "step_run_id",
    "stream",
    "template_id",
    "term_id",
    "token",
    "uid",
    "vid",
    "workflow_id",
}
_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[a-z]+)?\}")


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _mk(s, obj):
    s.add(obj)
    s.commit()
    s.refresh(obj)
    return obj


def _norm(text, idv):
    text = re.sub(r'nonce="[^"]*"|name="csrf[^>]*>|csrf_token[^>]*>|csrf-token[^>]*>', "", text)
    text = re.sub(r"\b" + re.escape(str(idv)) + r"\b", "ID", text)
    text = re.sub(r"'nonce-[^']*'", "'nonce-X'", text)
    return re.sub(r"\b" + str(_GONE) + r"\b", "ID", text)


_VOLATILE = {
    "date",
    "set-cookie",
    "x-request-id",
    "server-timing",
    "content-length",
    "etag",
    "x-response-time",
}


def main():
    with Session(engine) as s:
        a, b = _mk(s, Customer(name="Cust A")), _mk(s, Customer(name="Cust B"))
        proj = _mk(s, Project(name="mhw-a", source_locale="en-US", customer_id=a.id))
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
        wf = _mk(s, Workflow(name="mhw-wf", scope="global"))
        batch = _mk(
            s, WorkflowBatch(workflow_id=wf.id, document_id=doc.id, requested_locales=["es-ES"])
        )
        run = _mk(s, WorkflowRun(workflow_id=wf.id, document_id=doc.id, target_locale="es-ES"))
        idoc = _mk(
            s, Document(project_id=proj.id, filename="i.png", fmt="png", mode=TransMode.SEGMENTED)
        )
        finding = _mk(
            s,
            LQAResult(
                document_id=doc.id, segment_id=seg.id, dimension="accuracy", severity="minor"
            ),
        )
        ids = {
            "project": proj.id,
            "doc": doc.id,
            "seg": seg.id,
            "batch": batch.id,
            "run": run.id,
            "cust": a.id,
            "finding": finding.id,
            "image_doc": idoc.id,
        }
        cust_b = b.id

    callers = {}
    for tag, email, role, bound in (
        ("customer", "mhw-c@x.com", Role.ADMIN, True),
        ("viewer", "mhw-v@x.com", Role.VIEWER, True),
        ("scoped", "mhw-s@x.com", Role.TRANSLATOR, False),
    ):
        u = security.create_user(email, tag, role, _PW)
        if bound:
            with Session(engine) as s:
                row = s.get(User, u.id)
                row.customer_id = cust_b
                s.add(row)
                s.commit()
        callers[tag] = email
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
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

    clients = {tag: _login(tag, email) for tag, email in callers.items()}

    def val(name, hidden, path=""):
        kind = _KIND.get(name, "project")
        if kind == "doc" and path.startswith("/images/"):
            kind = "image_doc"  # the /images pages only open image documents
        return str(ids[kind]) if hidden else str(_GONE)

    routes = []
    unclassified = set()
    for rt in walk_routes(app):
        path = getattr(rt, "path", None)
        if not path or path.startswith("/api"):
            continue
        names = _PARAM.findall(path)
        unclassified.update(
            f"{{{n}}} in {path}" for n in names if n not in _KIND and n not in _OTHER
        )
        if _SKIP.search(path) or not any(n in _KIND for n in names):
            continue
        for m in getattr(rt, "methods", None) or ["GET"]:
            if m in ("HEAD", "OPTIONS"):
                continue
            body_names = [
                p.name for p in getattr(getattr(rt, "dependant", None), "body_params", [])
            ]
            routes.append((m, path, body_names))
    if unclassified:
        _fail(
            "unclassified path parameter(s), add each name to _KIND (it names a project, "
            "customer, document, run, batch, segment or finding id) or to _OTHER: "
            + "; ".join(sorted(unclassified))
        )
    if len(routes) < 100:
        _fail(
            f"route enumeration found only {len(routes)} web routes in the families; expected 100+"
        )

    extra = {
        "locale": "es-ES",
        "source": "en-US",
        "target": "es-ES",
        "tmx_b64": "AA==",
        "csv_b64": "AA==",
        "status": "open",
        "name": "x",
    }

    def compare(clients):
        problems = []
        tried = 0
        for m, path, body_names in sorted(routes):
            names = _PARAM.findall(path)
            for tag, cl in clients.items():
                if tag == "scoped" and "{customer_id}" in path:
                    continue  # project scoping hides projects only; every customer stays visible
                out = {}
                for kind, hidden in (("hidden", True), ("missing", False)):
                    p = path
                    for n in names:
                        p = re.sub(rf"\{{{n}(:[a-z]+)?\}}", val(n, hidden, path), p)
                    kw = {}
                    if m in ("POST", "PUT", "PATCH", "DELETE"):
                        kw["data"] = {**dict.fromkeys(body_names, "1"), **extra}
                    p += ("&" if "?" in p else "?") + "locale=es-ES&source=en-US&target=es-ES"
                    first = val(names[0], True, path)
                    try:
                        resp = cl.request(m, p, follow_redirects=False, **kw)
                        out[kind] = (
                            resp.status_code,
                            _norm(resp.text, first)[:4000],
                            _norm(resp.headers.get("location", ""), first),
                            tuple(
                                sorted(
                                    (k, _norm(v, first))
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
                        hb, gb = h[1][at : at + 40], g[1][at : at + 40]
                        why.append(f"body differs at {at}: {hb!r} vs {gb!r}")
                    if h[3] != g[3]:
                        why.append(f"headers {sorted(set(h[3]) ^ set(g[3]))[:3]}")
                    problems.append(f"{tag:8} {m:6} {path}: " + "; ".join(why))
        return tried, problems

    def report(label, tried, problems):
        n = len(clients)
        print(f"  [{label}] {len(routes)} web routes x {n} callers = {tried} comparisons")
        if problems:
            print(
                f"FAIL: [{label}] {len(problems)} comparisons differ"
                " between a missing and a hidden id:"
            )
            for line in problems:
                print("  ", line)
            sys.exit(1)

    tried, problems = compare(clients)
    report("single org", tried, problems)
    print("  missing id == hidden id on every web route of every family, for every caller: OK")

    # Gate (#575): the same comparison with MULTI-ORG on. Customer A's rows live in Org A; the
    # caller is an unbound ADMIN of Org B, so the org ring alone hides them (and every project
    # subtree under them). Under multi-org EVERY caller has a ring, so a missing id must answer
    # exactly like a cross-org one on every route, customers included.
    with Session(engine) as s:
        org_a = _mk(s, Organization(name="mhw Org A"))
        org_b = _mk(s, Organization(name="mhw Org B"))
        for model, rid in ((Project, ids["project"]), (Customer, ids["cust"])):
            row = s.get(model, rid)
            row.org_id = org_a.id
            s.add(row)
        s.commit()
        org_b_id = org_b.id
    security.create_user("mhw-o@x.com", "org", Role.ADMIN, _PW, org_id=org_b_id)
    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        clients = {"org": _login("org", "mhw-o@x.com")}
        tried, problems = compare(clients)
    finally:
        settings.set_many({"multi_org_enabled": "0"})
        settings.invalidate_cache()
    report("multi-org", tried, problems)
    print("  missing id == cross-org id on every web route with multi-org on: OK")

    settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
    settings.invalidate_cache()
    print("MISSING HIDDEN WEB ROUTES OK")


if __name__ == "__main__":
    main()
