"""#542 (follow-ups to #468) -- hermetic.

Item 1: a bound customer's added language pair in a SHARED (global) workflow group was
never dispatched. #468 stamps the customer's copy with that customer's id; dispatch
(`_variant_match._compatible`) rejected every variant whose customer differed from the
REQUESTED workflow's (the global default has none), so the German run fell back to the
default's French-pair config ("I added a language and nothing happened").

(Item 2 of the original suite, gold-label import into an Evaluations dataset, belongs to the
paid Evaluations Lab and is not part of the open-core tree.)

Pinned:
  1. resolve_variant with the document's customer dispatches that customer's stamped
     variant; the SAME request for another customer's document, or with no document
     customer, never gets it (fail closed); archived and foreign-org variants stay out.

Final line: "WORKFLOW CUSTOMER VARIANT 542 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cv542_")

from datetime import datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Organization, Role, Workflow
from app.services import apikeys, security
from app.services import workflows as wf

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


# --------------------------------------------------------------------------- item 1
def _item1():
    cust_a, cust_b = _customer("Cust A 542"), _customer("Cust B 542")
    with Session(engine) as s:
        g = wf.create_workflow(s, "Shared WF 542", scope="global")
        wf.add_step(s, g.id, name="t", provider="mock", prompt_text="Translate {text}.")
        grp = wf.create_group(s, "Shared group 542")
        wf.add_to_group(
            s, g.id, grp.id, source_locale="en-US", target_locale="fr-FR", make_default=True
        )
        # the #468 path: customer B (bound) adds de-DE to the shared group
        res = wf.copy_to_pairs(
            s,
            g.id,
            [("en-US", "de-DE")],
            group_id=grp.id,
            bound_customer_id=cust_b,
        )
        if len(res["created"]) != 1:
            _fail(f"add-pair did not create the de-DE variant: {res}")
        de = s.exec(
            select(Workflow).where(Workflow.group_id == grp.id, Workflow.target_locale == "de-DE")
        ).all()
        if len(de) != 1 or de[0].customer_id != cust_b:
            _fail("the bound add-pair copy must be stamped with customer B")
        de_id = de[0].id
        gid = g.id

        got = wf.resolve_variant(s, gid, "de-DE", "en-US", doc_customer_id=cust_b)
        if got != de_id:
            _fail(f"customer B's document must dispatch its de-DE variant #{de_id}, got #{got}")
        for label, kw in (
            ("customer A's document", {"doc_customer_id": cust_a}),
            ("a global document", {"doc_customer_id": None}),
            ("no document customer passed", {}),
        ):
            got = wf.resolve_variant(s, gid, "de-DE", "en-US", **kw)
            if got == de_id:
                _fail(f"{label} must NEVER get customer B's variant")
            if got != gid:
                _fail(f"{label} must fall back to the global default #{gid}, got #{got}")
        # the exact-pair global variant still resolves for everyone, and B's fr-FR run
        # does not pick B's de-DE variant
        if wf.resolve_variant(s, gid, "fr-FR", "en-US", doc_customer_id=cust_b) != gid:
            _fail("fr-FR for customer B must still resolve to the global fr-FR default")
    # Review gate: the run-starting callers must pass the DOCUMENT's customer, not the
    # caller's or the workflow's: /api run, the plan preview and the batch planner.
    _item1_callers(gid, de_id, cust_a, cust_b)
    with Session(engine) as s:
        # archived check unchanged: an archived customer variant is not dispatched
        v = s.get(Workflow, de_id)
        v.archived_at = datetime.utcnow()
        s.add(v)
        s.commit()
        if wf.resolve_variant(s, gid, "de-DE", "en-US", doc_customer_id=cust_b) == de_id:
            _fail("an archived variant must not be dispatched")
        v.archived_at = None
        s.add(v)
        s.commit()
        # org check unchanged: a variant from another org is not dispatched
        other_org = Organization(name="Other org 542")
        s.add(other_org)
        s.commit()
        s.refresh(other_org)
        v.org_id = other_org.id
        s.add(v)
        s.commit()
        if wf.resolve_variant(s, gid, "de-DE", "en-US", doc_customer_id=cust_b) == de_id:
            _fail("a foreign-org variant must not be dispatched")
        v.org_id = None
        s.add(v)
        s.commit()
    print("  1. dispatch: B's added pair runs for B's documents only, archived/org unchanged: OK")


def _doc_for(customer_id, name):
    from app.models import Document, Segment, TransMode
    from app.services import content

    with Session(engine) as s:
        proj = content.project_create(
            s, name, source_locale="en-US", target_locales="de-DE", customer_id=customer_id
        )
        doc = Document(
            project_id=proj.id,
            filename="d.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="de-DE",
                order_index=0,
                source_text="Hello world.",
                target_text="",
            )
        )
        s.commit()
        return proj.id, doc.id


def _item1_callers(gid, de_id, cust_a, cust_b):
    from app.models import Document, Project
    from app.services import batch_runs_hub, workflow_plan

    staff = security.create_user("cv542-runner@x.com", "Runner", Role.ADMIN, "cv542-run-pass-1")
    _r, key = apikeys.create_key(staff.id, "cv542-runner", scopes="api docs")
    c = TestClient(app, raise_server_exceptions=False)
    for label, cust, want in (
        ("customer B's document", cust_b, de_id),
        ("customer A's document", cust_a, gid),
        ("a global document", None, gid),
    ):
        pid, did = _doc_for(cust, f"CV542 {label}")
        r = c.post(
            f"/api/workflows/{gid}/run",
            headers={"X-Loc-Token": key},
            json={"doc_id": did, "locale": "de-DE"},
        )
        if r.status_code != 200:
            _fail(f"/api run for {label} failed: {r.status_code} {r.text[:200]}")
        got = r.json().get("workflow_id")
        if got != want:
            _fail(f"/api run for {label} must dispatch #{want}, got #{got}")
        with Session(engine) as s:
            doc = s.get(Document, did)
            proj = s.get(Project, pid)
            p = workflow_plan.plan(s, gid, doc, "de-DE")
            got_plan = p.get("workflow_id")
            if got_plan != want:
                _fail(f"plan for {label} must resolve #{want}, got #{got_plan}")
            got_batch = dict(batch_runs_hub._plan(s, s.get(Workflow, gid), proj, ["de-DE"]))
            if got_batch.get("de-DE") != want:
                _fail(f"batch plan for {label} must resolve #{want}, got {got_batch}")


# --------------------------------------------------------------------------- item 2
def main():
    _item1()
    print("WORKFLOW CUSTOMER VARIANT 542 OK")


if __name__ == "__main__":
    main()
