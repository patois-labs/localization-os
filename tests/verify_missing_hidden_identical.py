"""#570: a MISSING id and an id HIDDEN by the customer ring answer identically -- hermetic.

THE BUG: older web routes answered "Project not found." / "Document not found." /
"Run not found." / the BatchMissing text for an id that does not exist, while an id that
exists but belongs to another customer got the ring's 404 "Not found.". A customer-bound
caller could tell the two apart, so the route was an existence oracle.

Pinned (auth ON, caller bound to Customer B, ids owned by Customer A vs ids that never
existed): for every route below the (status, body) pair is identical, on the web surface
and on the /api surface.

Final line: "MISSING HIDDEN IDENTICAL OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_misshid_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    Role,
    TransMode,
    User,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

init_db()

_PW = "misshid-fixture-pass-1"
_GONE = 987654


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _norm(text):
    return re.sub(r'nonce="[^"]*"|name="csrf[^>]*>|csrf_token[^>]*>', "", text)


def main():
    with Session(engine) as s:
        a, b = Customer(name="Cust A"), Customer(name="Cust B")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        cust_a, cust_b = a.id, b.id
        proj = Project(name="misshid-a", source_locale="en-US", customer_id=cust_a)
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(project_id=proj.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
        wf = Workflow(name="misshid-wf", scope="global")
        s.add(doc)
        s.add(wf)
        s.commit()
        s.refresh(doc)
        s.refresh(wf)
        batch = WorkflowBatch(workflow_id=wf.id, document_id=doc.id, requested_locales=["es-ES"])
        run = WorkflowRun(workflow_id=wf.id, document_id=doc.id, target_locale="es-ES")
        s.add(batch)
        s.add(run)
        s.commit()
        s.refresh(batch)
        s.refresh(run)
        pid, did, wid, bid, rid = proj.id, doc.id, wf.id, batch.id, run.id
    bound = security.create_user("misshid-b@x.com", "B", Role.ADMIN, _PW)
    with Session(engine) as s:
        u = s.get(User, bound.id)
        u.customer_id = cust_b
        s.add(u)
        s.commit()
    key = apikeys.create_key(bound.id, "b", scopes="api docs")[1]
    h = {"X-Loc-Token": key}
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)  # /api: the bound key
        w = TestClient(app)  # web: the bound user's cookie session
        csrf_for(w)
        r = w.post(
            "/auth/login",
            data={"email": "misshid-b@x.com", "password": _PW, "next": "/"},
            follow_redirects=False,
        )
        if r.status_code != 303:
            _fail(f"web login of the bound user failed: {r.status_code}")
        csrf_for(w)

        def pair(method, path_tpl, hidden_id, web=False, **kw):
            cl, hd = (w, {}) if web else (c, h)
            hid = cl.request(
                method, path_tpl.format(id=hidden_id), headers=hd, follow_redirects=False, **kw
            )
            mis = cl.request(
                method, path_tpl.format(id=_GONE), headers=hd, follow_redirects=False, **kw
            )
            return hid, mis

        def same(label, hid, mis):
            if hid.status_code != 404:
                _fail(f"{label}: hidden id must be a 404, got {hid.status_code}")
            if (hid.status_code, _norm(hid.text)) != (mis.status_code, _norm(mis.text)):
                _fail(
                    f"{label}: missing and hidden differ: "
                    f"hidden {hid.status_code} {_norm(hid.text)[:160]!r} vs "
                    f"missing {mis.status_code} {_norm(mis.text)[:160]!r}"
                )

        web = [
            ("POST", "/projects/{id}/archive", pid, {}),
            ("POST", "/projects/{id}/unarchive", pid, {}),
            ("POST", "/projects/{id}/ai-opt-out", pid, {"data": {"ai_opt_out": "1"}}),
            (
                "POST",
                "/documents/{id}/run",
                did,
                {"data": {"workflow_id": str(wid), "locale": "es-ES"}},
            ),
            ("POST", "/documents/{id}/run-batch", did, {"data": {"workflow_id": str(wid)}}),
            ("GET", "/batches/{id}", bid, {}),
            ("GET", "/runs/stream?run_id={id}", rid, {}),
        ]
        for method, tpl, hidden_id, kw in web:
            hid, mis = pair(method, tpl, hidden_id, web=True, **kw)
            same("web " + method + " " + tpl, hid, mis)
        # the workflow plan preview names the document through ?doc_id=
        hid = w.get(f"/workflows/{wid}/plan?doc_id={did}")
        mis = w.get(f"/workflows/{wid}/plan?doc_id={_GONE}")
        same("web GET /workflows/{id}/plan?doc_id=", hid, mis)
        print("  1. web: missing id == hidden id (status and body) on every older route: OK")

        api = [
            ("GET", "/api/projects/{id}", pid),
            ("POST", "/api/projects/{id}/archive", pid),
            ("POST", "/api/projects/{id}/unarchive", pid),
            ("POST", "/api/projects/{id}/ai-opt-out", pid),
            ("GET", "/api/documents/{id}/segments", did),
            ("GET", "/api/batches/{id}", bid),
            ("GET", "/api/runs/{id}", rid),
        ]
        for method, tpl, hidden_id in api:
            hid, mis = pair(method, tpl, hidden_id)
            same("api " + method + " " + tpl, hid, mis)
        print("  2. /api: missing id == hidden id on the equivalent reads and writes: OK")

        # The one deliberate exception: a caller with NO ring (sees every batch anyway) keeps
        # the typed batch_missing text for a missing batch; a ring-scoped caller never does.
        security.create_user("misshid-free@x.com", "F", Role.ADMIN, _PW)
        f = TestClient(app)
        csrf_for(f)
        r = f.post(
            "/auth/login",
            data={"email": "misshid-free@x.com", "password": _PW, "next": "/"},
            follow_redirects=False,
        )
        if r.status_code != 303:
            _fail(f"web login of the unrestricted user failed: {r.status_code}")
        csrf_for(f)
        free_gone = f.get(f"/batches/{_GONE}", follow_redirects=False)
        if free_gone.status_code != 404 or "batch_missing" not in free_gone.text:
            _fail(f"unrestricted caller lost the typed batch_missing text: {free_gone.status_code}")
        bound_gone = w.get(f"/batches/{_GONE}", follow_redirects=False)
        if "batch_missing" in bound_gone.text:
            _fail("a ring-scoped caller was handed the typed batch_missing text")
        print("  3. batch_missing text only for a caller with no ring: OK")
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("MISSING HIDDEN IDENTICAL OK")


if __name__ == "__main__":
    main()
