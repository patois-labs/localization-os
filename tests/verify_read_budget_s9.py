"""S9 read diet: SQL statements per request of the load harness's four read-cycle GETs.

The load harness loops these with one docs-scoped key:

    GET /api/projects?limit=20
    GET /api/projects/{pid}
    GET /api/projects/{pid}/documents
    GET /api/documents/{did}/segments?detail=segments

At the start of S9 the four cost 5, 10, 3 and 5 statements (not counting the API key lookup,
which is the auth layer's) at the steady state, plus pool checkouts for each. The S9 reads
package cut that by loading only the columns a handler uses, folding the document status into
the documents read, deriving the segments route's status from the segments it already loaded,
reading the project and document in one statement, and counting both hidden-project groups in
one statement.

This suite seeds a small project (two documents, two locales, a customer, a custom field) and
pins the statements per request and the pool checkouts per request. The numbers are exact
ceilings: a new read added to one of these handlers must be argued for by raising its number
here in the same change. It also pins the response SHAPE of each route (key sets), so a column
that was dropped from a read cannot silently drop a field from a payload.

Fully offline: mock engine, a throwaway LOC_TMS_DATA_DIR, auth on, an admin key.
"""

import os
import sys
from datetime import UTC, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_readbudget_")

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Project, Role, Segment
from app.services import apikeys, security, settings
from tests._stmt_counter import StatementCounter

FIXTURE_PASSWORD = "offline-fixture-password-123"  # pregate: allow-secret offline fixture

# Statements per request, NOT counting the API key lookup (the auth layer's own read), measured
# in the steady state (caches warm). Ceilings: raise one only with a reason in the same change.
BUDGET = {
    "list_projects": 4,  # field defs, page with customer names, hidden counts, field values
    "get_project": 9,
    "list_jobs": 1,  # project ring columns and the documents with their statuses
    "list_segments": 2,
}
# Pool checkouts per request including the key lookup's.
CHECKOUT_BUDGET = {
    "list_projects": 2,
    "get_project": 2,
    "list_jobs": 2,
    "list_segments": 2,
}


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


class _NoParams:
    """A request stand-in with no query parameters (what _projects_page_general reads)."""

    class query_params:
        @staticmethod
        def multi_items():
            return []

        @staticmethod
        def get(_key, default=None):
            return default


def _equivalence_checks(client, h, pid, cust, doc_ids) -> list:
    """The reads the budget cut must answer exactly what the old ones did. Each check computes the
    old answer with the old code path (still in the tree) and compares."""
    from app.kernel import delivery, lifecycle
    from app.models import Document
    from app.web import api_cli_projects_1 as p1

    bad = []
    with Session(engine) as s:
        # 1. document statuses: spaces-only and newline-only targets, approved, empty, no segments.
        extra = Document(project_id=pid, filename="edge.html", fmt="html")
        empty = Document(project_id=pid, filename="empty.html", fmt="html")
        s.add(extra)
        s.add(empty)
        s.commit()
        s.refresh(extra)
        s.refresh(empty)
        for i, (tgt, st) in enumerate(
            [("   ", "new"), ("\n", "new"), ("", "new"), ("x", "new"), ("y", "approved")]
        ):
            s.add(
                Segment(
                    document_id=extra.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text="s",
                    target_text=tgt,
                    status=st,
                )
            )
        # a document whose zip-batch stamp the lists must still count
        extra.skeleton = {"zip_batch": {"id": "bx", "archive": "x.zip", "path": "edge.html"}}
        s.add(extra)
        s.commit()
        listed = {
            d["id"]: d["status"]
            for d in client.get(f"/api/projects/{pid}/documents", headers=h).json()["documents"]
        }
        want = lifecycle.document_status_map(s, list(listed))
        if listed != want:
            bad.append(f"document statuses differ from document_status_map: {listed} vs {want}")
        for did in (*doc_ids, extra.id, empty.id):
            rows = s.exec(select(Segment).where(Segment.document_id == did)).all()
            doc = s.get(Document, did)
            if lifecycle.document_status_from_segments(rows) != lifecycle.document_status(s, doc):
                bad.append(f"document_status_from_segments differs for document {did}")
        body = client.get(f"/api/projects/{pid}/documents", headers=h).json()
        if body["zip_batches"] != delivery.zip_batches(s, pid) or not body["zip_batches"]:
            bad.append(
                f"zip_batches differ: {body['zip_batches']} vs {delivery.zip_batches(s, pid)}"
            )
        seg_status = client.get(f"/api/documents/{extra.id}/segments", headers=h).json()["status"]
        if seg_status != want[extra.id]:
            bad.append(f"segments route status {seg_status} != {want[extra.id]}")
        # 2. the list: the built-once page and hidden counts against the per-call statements.
        s.add(
            Project(
                name="Old one", customer_id=cust, archived_at=datetime.now(UTC).replace(tzinfo=None)
            )
        )
        s.add(Project(name="Done one", customer_id=cust, status="completed"))
        s.commit()
        ident = {"user": None, "_cust_id": None}
        for intake in ("", "upload", "connected"):
            for archived in ("", "all", "only"):
                for status in ("", "default", "all", "completed", "in_translation"):
                    fast = p1._projects_page_fast(s, ident, None, intake, archived, status, 20, 0)
                    slow = p1._projects_page_general(
                        s, _NoParams, ident, None, intake, archived, status, 20, 0, {}
                    )
                    if [tuple(r) for r in fast[0]] != [tuple(r) for r in slow[0]] or fast[
                        1
                    ] != slow[1]:
                        bad.append(
                            f"list differs for {(intake, archived, status)}: {fast[1]} vs {slow[1]}"
                        )
                    if archived not in ("all", "only") or status in ("", "default"):
                        old_hidden = _old_hidden(s, p1, ident, None, archived, status)
                        if fast[1] != old_hidden:
                            bad.append(
                                f"hidden differs {(archived, status)}: {fast[1]} != {old_hidden}"
                            )
    return bad


def _old_hidden(s, p1, ident, org_id, archived, status):
    """The two separate counts the list used before S9, verbatim."""
    from app.models import ProjectStatus
    from app.services import content

    base = p1._scoped_projects(ident, org_id)
    hidden = {"archived": 0, "finished": 0}
    if archived not in ("all", "only"):
        q = base.where(Project.archived_at.is_not(None))
        if status not in ("", "default", "all"):
            q = q.where(Project.status == status)
        hidden["archived"] = p1._count(s, q)
    if status in ("", "default"):
        fin = base.where(Project.status.in_(ProjectStatus.HIDDEN_BY_DEFAULT))
        if archived not in ("all", "only"):
            fin = content.project_active_only(fin)
        hidden["finished"] = p1._count(s, fin)
    return hidden


def main() -> None:
    init_db()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    user = security.create_user(
        "readbudget@example.test", "Read Budget", Role.ADMIN, FIXTURE_PASSWORD
    )
    key = apikeys.create_key(user.id, "readbudget", scopes="api docs")[1]
    client = TestClient(app)
    h = {"X-Loc-Token": key}
    counter = StatementCounter(engine)

    checkouts = {"n": 0}

    @event.listens_for(engine, "checkout")
    def _co(dbapi_conn, rec, proxy):
        checkouts["n"] += 1

    r = client.post("/api/customers", json={"name": "Read Co"}, headers=h)
    cust = (r.json() or {}).get("id")
    if not cust:
        _fail(f"customer create: {r.status_code} {r.text[:200]}")
    r = client.post(
        "/api/custom-fields",
        headers=h,
        json={"key": "po_number", "label": "PO number", "type": "text"},
    )
    if r.status_code >= 300:
        _fail(f"custom field create: {r.status_code} {r.text[:200]}")
    r = client.post(
        "/api/projects",
        headers=h,
        json={
            "name": "Read Budget",
            "customer_id": cust,
            "source_locale": "en-US",
            "targets": "es-ES,fr-FR",
            "provider": "mock",
        },
    )
    if r.status_code >= 300:
        _fail(f"project create: {r.status_code} {r.text[:200]}")
    pid = r.json()["id"]
    doc_ids = []
    for fname, body in (
        ("a.html", "<html><body><h1>Title</h1><p>One. Two.</p><p>Three.</p></body></html>"),
        ("b.md", "# Packing\n\nA list.\n\n1. First\n2. Second\n"),
    ):
        u = client.post(
            "/api/documents",
            headers=h,
            data={"project_id": str(pid)},
            files={"file": (fname, body.encode())},
        )
        if u.status_code >= 300:
            _fail(f"upload {fname}: {u.status_code} {u.text[:200]}")
        doc_ids.append(u.json()["document_id"])
    # One translated and one approved segment, so the derived status is not just "new".
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_ids[0]).order_by(Segment.id)
        ).all()
        if len(segs) < 2:
            _fail(f"expected at least two segments, got {len(segs)}")
        segs[0].target_text = "Titulo"
        segs[0].status = "approved"
        segs[1].target_text = "Uno. Dos."
        segs[1].status = "mt"
        s.add(segs[0])
        s.add(segs[1])
        s.commit()

    routes = {
        "list_projects": "/api/projects?limit=20",
        "get_project": f"/api/projects/{pid}",
        "list_jobs": f"/api/projects/{pid}/documents",
        "list_segments": f"/api/documents/{doc_ids[0]}/segments?detail=segments",
    }
    shapes = {
        "list_projects": ({"ok", "projects", "truncated", "limit", "offset", "hidden"}, None),
        "get_project": (
            {
                "ok",
                "id",
                "name",
                "documents",
                "tm_count",
                "ft_tm_count",
                "glossary_count",
                "custom_fields",
                "customer",
                "target_locales",
                "stopped_locales",
                "pair_providers",
                "segmentation_profile_resolved_id",
                "budget",
            },
            None,
        ),
        "list_jobs": ({"ok", "documents", "zip_batches"}, None),
        "list_segments": ({"ok", "id", "filename", "fmt", "mode", "status", "locales"}, None),
    }

    failures = []
    for name, url in routes.items():
        for _ in range(2):  # warm: settings probe, TM counts, budget meter
            warm = client.get(url, headers=h)
            if warm.status_code != 200:
                _fail(f"{name}: {warm.status_code} {warm.text[:200]}")
        before = checkouts["n"]
        with counter.measure() as m:
            resp = client.get(url, headers=h)
        used_checkouts = checkouts["n"] - before
        if resp.status_code != 200:
            _fail(f"{name}: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        need = shapes[name][0]
        if not need <= set(body):
            _fail(f"{name}: response lacks {sorted(need - set(body))}")
        # The key lookup is the auth layer's; the settings-version probe is a clock-driven
        # read that is not the handler's.
        own = sum(
            n
            for sql, n in m.by_statement.items()
            if " apikey" not in sql.lower() and "setting_key" not in sql
        )
        print(f"  {name}: {own} statements (+{m.total - own} key), {used_checkouts} checkouts")
        if own > BUDGET[name]:
            top = "\n".join(f"    {n:3d}  {sql[:140]}" for sql, n in m.top(15))
            failures.append(f"{name}: {own} statements > budget {BUDGET[name]}\n{top}")
        if used_checkouts > CHECKOUT_BUDGET[name]:
            failures.append(f"{name}: {used_checkouts} checkouts > budget {CHECKOUT_BUDGET[name]}")

    # Response content the dropped columns must not have changed.
    shown = client.get(routes["get_project"], headers=h).json()
    if [d["filename"] for d in shown["documents"]] != ["b.md", "a.html"]:
        failures.append(f"get_project documents order/names: {shown['documents']}")
    if [d["status"] for d in shown["documents"]] != ["new", "in_translation"]:
        failures.append(f"get_project document statuses: {shown['documents']}")
    if shown["customer"] != "Read Co":
        failures.append(f"get_project customer name: {shown['customer']!r}")
    listed = client.get(routes["list_projects"], headers=h).json()
    row = next((p for p in listed["projects"] if p["id"] == pid), None)
    if row is None or row["customer"] != "Read Co":
        failures.append(f"list_projects customer name: {row}")
    segs_body = client.get(routes["list_segments"], headers=h).json()
    if segs_body["status"] != "in_translation":
        failures.append(f"list_segments status: {segs_body['status']}")
    if any(
        "segment_details" not in entry or "target_present" not in entry["segment_details"][0]
        for entry in segs_body["locales"].values()
    ):
        failures.append("list_segments: segment_details missing")

    failures += _equivalence_checks(client, h, pid, cust, doc_ids)

    counter.close()
    if failures:
        _fail("\n".join(failures))
    print("verify_read_budget_s9: all checks OK")


if __name__ == "__main__":
    main()
