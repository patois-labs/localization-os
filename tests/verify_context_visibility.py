"""Context Package phase 4a verification — HUMAN SURFACES (SPEC-context-metadata).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network).
Proves the "what the model saw" transparency surfaces + the doc-level edit path:

  1. Document page renders the EFFECTIVE (resolved) package with per-field PROVENANCE
     labels (a doc override + a customer card -> BOTH fields show, correctly attributed),
     and the collapsible "what the model sees" block equals render_block(effective).
  2. POST /documents/{id}/context sets Document.context (valid), redirects with a clean
     error + leaves it UNCHANGED on invalid, and CLEARS it (None) on an empty form.
  3. Web org-guard: a foreign-org document's context route is blocked (404) under the
     sacred gate (auth ON + multi_org).
  4. CLI parity: `context set-document` / `show-document` round-trip (set, show, clear).
  5. Editor page renders the context panel when context exists, omits it when none, and
     surfaces per-SEGMENT unit fields (Segment.meta char_limit/key/notes) when present
     WITHOUT disturbing the CAT grid anchors.

Final line: "CONTEXT VISIBILITY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ctxvis_")

from fastapi.testclient import TestClient
from markupsafe import escape
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    Role,
    Segment,
    SegStatus,
    User,
)
from app.services import context_pkg as cp
from app.services import security, settings

init_db()
_CLIENT = TestClient(app)


def _seed_project(org_id=1, customer_id=None, content_type_id=None, name="p"):
    with Session(engine) as s:
        proj = Project(
            name=name,
            org_id=org_id,
            customer_id=customer_id,
            content_type_id=content_type_id,
            source_locale="en-US",
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        return proj.id


def _seed_doc(project_id, context=None, filename="d.txt"):
    with Session(engine) as s:
        doc = Document(project_id=project_id, filename=filename, fmt="txt", context=context)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc.id


# ---------------------------------------------------------------------------
# 1. Document page: EFFECTIVE package + provenance + "what the model sees"
# ---------------------------------------------------------------------------
def test_document_page_effective_and_provenance():
    with Session(engine) as s:
        org = s.exec(select(Organization)).first()
        oid = org.id
        cust = Customer(name="Acme-vis", org_id=oid)
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
    # A customer card supplies audience; the document override supplies domain.
    # Card + project share the customer's org (production invariant — resolve now
    # scopes to the document's project org, so an org-less card would be a fixture
    # that a real same-org request could never produce).
    with Session(engine) as s:
        cp.upsert_profile(s, org_id=oid, customer_id=cid, context_raw={"audience": "end customers"})
    pid = _seed_project(org_id=oid, customer_id=cid, name="Vis-proj")
    doc_id = _seed_doc(pid, context={"domain": "software UI"})

    page = _CLIENT.get(f"/documents/{doc_id}").text
    # Both effective fields render, each attributed to its resolving layer.
    assert "software UI" in page and "end customers" in page, "effective fields missing"
    assert "from this document" in page, "document provenance label missing"
    assert "from customer card" in page, "customer provenance label missing"

    # The "what the model sees" block equals render_block(effective) (HTML-escaped).
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        project = s.get(Project, doc.project_id)
        effective, _prov = cp.resolve(s, document=doc, project=project)
        block = cp.render_block(effective)
    assert block, "expected a non-empty effective block"
    assert str(escape(block)) in page, "the rendered block does not match render_block(effective)"
    print("  document page: effective card + provenance labels + model-sees block OK")


# ---------------------------------------------------------------------------
# 2. POST /documents/{id}/context — set / invalid / clear
# ---------------------------------------------------------------------------
def test_post_context_set_invalid_clear():
    pid = _seed_project(name="Edit-proj")
    doc_id = _seed_doc(pid, context=None)

    # valid set
    r = _CLIENT.post(
        f"/documents/{doc_id}/context",
        data={
            "domain": "billing",
            "formality": "formal",
            "tone": "friendly, concise",
            "audience": "SMB admins",
            "source_labels": "faq, billing",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.status_code
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        assert doc.context and doc.context["domain"] == "billing", doc.context
        assert doc.context["formality"] == "formal"
        assert doc.context["tone"] == ["friendly", "concise"]
        assert doc.context["source"]["labels"] == ["faq", "billing"], doc.context

    # invalid formality -> redirect with err, override UNCHANGED (nothing saved)
    r = _CLIENT.post(
        f"/documents/{doc_id}/context",
        data={"domain": "billing", "formality": "bogus-enum"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert "err=" in r.headers.get("location", ""), r.headers.get("location")
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        assert doc.context and doc.context["formality"] == "formal", (
            "invalid POST must not mutate the stored override"
        )

    # empty form -> override cleared (None)
    r = _CLIENT.post(f"/documents/{doc_id}/context", data={}, follow_redirects=False)
    assert r.status_code in (302, 303)
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        assert doc.context is None, "empty form must clear the override to None"
    print("  POST /documents/{id}/context: set / invalid-unchanged / clear OK")


# ---------------------------------------------------------------------------
# 3. Web org-guard — a foreign-org document's context route is blocked (404)
# ---------------------------------------------------------------------------
def test_web_org_guard():
    from tests._csrf import csrf_for

    with Session(engine) as s:
        org_a = Organization(name="VisOrg A")
        org_b = Organization(name="VisOrg B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id
    ua = security.create_user("vis-a@x.com", "Vis A", Role.ADMIN, "vis-a-password-123")
    with Session(engine) as s:
        row = s.get(User, ua.id)
        row.org_id = oa
        s.add(row)
        s.commit()
    # A document living in ORG B.
    pid_b = _seed_project(org_id=ob, name="OrgB-proj")
    doc_b = _seed_doc(pid_b, context=None)
    # A document in ORG A (positive control).
    pid_a = _seed_project(org_id=oa, name="OrgA-proj")
    doc_a = _seed_doc(pid_a, context=None)

    # Engage the sacred gate (auth ON + multi_org).
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        csrf_for(c)
        r = c.post(
            "/auth/login",
            data={"email": "vis-a@x.com", "password": "vis-a-password-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, f"login -> {r.status_code}"
        csrf_for(c)
        # Foreign-org doc: 404 (not enumerable).
        r = c.post(f"/documents/{doc_b}/context", data={"domain": "x"}, follow_redirects=False)
        assert r.status_code == 404, f"foreign-org context route not blocked: {r.status_code}"
        with Session(engine) as s:
            assert s.get(Document, doc_b).context is None, "foreign-org doc was mutated"
        # Own-org doc: allowed (303 redirect, override written).
        r = c.post(
            f"/documents/{doc_a}/context", data={"domain": "allowed"}, follow_redirects=False
        )
        assert r.status_code in (302, 303), f"own-org context route blocked: {r.status_code}"
        with Session(engine) as s:
            assert s.get(Document, doc_a).context["domain"] == "allowed"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  web org-guard: foreign-org context route blocked (404), own-org allowed OK")


# ---------------------------------------------------------------------------
# 4. CLI parity — context set-document / show-document round-trip
# ---------------------------------------------------------------------------
def test_cli_set_show_document():
    from app.cli.__main__ import main

    pid = _seed_project(name="Cli-proj")
    doc_id = _seed_doc(pid, context=None)

    rc = main(
        [
            "--json",
            "context",
            "set-document",
            "--document",
            str(doc_id),
            "--domain",
            "cli-domain",
            "--audience",
            "cli-aud",
            "--tone",
            "confident,professional",
            "--source-title",
            "Reset guide",
        ]
    )
    assert rc == 0
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        assert doc.context["domain"] == "cli-domain", doc.context
        assert doc.context["tone"] == ["confident", "professional"]
        assert doc.context["source"]["title"] == "Reset guide", doc.context

    rc = main(["context", "show-document", "--document", str(doc_id)])
    assert rc == 0

    # empty (no field flags) -> clears the override to None.
    rc = main(["--json", "context", "set-document", "--document", str(doc_id)])
    assert rc == 0
    with Session(engine) as s:
        assert s.get(Document, doc_id).context is None, "empty set-document must clear override"
    print("  cli: context set-document / show-document round-trip (set, show, clear) OK")


# ---------------------------------------------------------------------------
# 5. Editor page — context panel present/absent + per-segment unit fields
# ---------------------------------------------------------------------------
def test_editor_context_panel_and_unit_fields():
    # (a) a doc WITH context + a segment carrying unit meta -> panel + unit fields show.
    pid = _seed_project(name="Ed-proj")
    doc_id = _seed_doc(pid, context={"domain": "software UI", "audience": "admins"})
    with Session(engine) as s:
        seg = Segment(
            document_id=doc_id,
            target_locale="fr-FR",
            order_index=0,
            source_text="Reset your password.",
            target_text="",
            status=SegStatus.NEW,
            meta={"char_limit": 40, "key": "title.hero", "notes": "keep short"},
        )
        s.add(seg)
        # a second plain segment (no meta) to confirm absence renders nothing.
        s.add(
            Segment(
                document_id=doc_id,
                target_locale="fr-FR",
                order_index=1,
                source_text="Welcome back.",
                target_text="",
                status=SegStatus.NEW,
            )
        )
        s.commit()

    page = _CLIENT.get(f"/documents/{doc_id}/editor?locale=fr-FR").text
    assert "what the model saw" in page, "editor context panel missing when context exists"
    # per-segment unit fields render for the seg that has meta.
    assert "title.hero" in page and "40 chars" in page and "keep short" in page, (
        "per-segment unit fields not surfaced"
    )
    # CAT-contract anchors intact (the panel must not disturb the grid).
    with Session(engine) as s:
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
    for seg in segs:
        assert f'id="tgt-{seg.id}"' in page and f'id="st-{seg.id}"' in page, (
            "editor grid anchors disturbed"
        )

    # (b) a fresh org/customer/project with NO cards + a doc with NO context ->
    #     effective is empty -> the panel is ABSENT.
    with Session(engine) as s:
        org = Organization(name="Empty-vis-org")
        s.add(org)
        s.commit()
        s.refresh(org)
        oid = org.id
    pid2 = _seed_project(org_id=oid, name="Empty-proj")
    doc2 = _seed_doc(pid2, context=None)
    with Session(engine) as s:
        s.add(
            Segment(
                document_id=doc2,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hi.",
                target_text="",
                status=SegStatus.NEW,
            )
        )
        s.commit()
    page2 = _CLIENT.get(f"/documents/{doc2}/editor?locale=fr-FR").text
    assert "what the model saw" not in page2, "context panel shown for a no-context doc"
    print("  editor: context panel present/absent + unit fields, grid anchors intact OK")


def main() -> None:
    test_document_page_effective_and_provenance()
    test_post_context_set_invalid_clear()
    test_cli_set_show_document()
    test_editor_context_panel_and_unit_fields()
    test_web_org_guard()  # LAST: toggles the global auth gate (restored in finally)
    print("CONTEXT VISIBILITY OK")


if __name__ == "__main__":
    main()
