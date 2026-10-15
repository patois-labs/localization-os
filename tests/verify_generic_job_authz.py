"""GitHub #370 (apiauth-01): the generic POST /api/jobs/run door must not hand out the
internal jobs.HANDLERS registry after only the docs scope + can_write.

Each registered kind now has to clear the SAME scope/role/switch checks its dedicated
launch endpoint enforces. This suite drives the REAL endpoint with the login wall ON and
multi-org OFF (so the behaviour under test is the per-kind role/scope/switch gate, not
org scoping), for EVERY registered kind:

  backfill_embeddings -> gone with Semantic TM: an unknown job kind (400) for every role.
  connector_translate -> can_tune_ai (the /api/connectors/{cid}/* role) and a payload
    actor that is overwritten with the authenticated identity (no forged attribution).
  translate_document  -> the door's own can_write (a viewer is 403'd).
  data_export         -> internal-only, rejected before dispatch even for an existing run.

The connector egress seam (conn_store.translate_now) is stubbed to an offline recorder so
no paid MT / network runs; the recorder is DOWNSTREAM of the actor-overwrite guard, so it
proves the guard fired without weakening it. Every failing case here FAILS without the fix
(a translator would otherwise reach the handler: 400, not 403; and a forged actor would
reach the connector). Standalone one-process-per-suite: LOC_TMS_DATA_DIR is a throwaway,
provider keys cleared.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_gjobauthz_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Connection, Customer, DataExportRun, Document, Project, Role, User
from app.services import apikeys, data_export, security, settings
from app.services.connectors import store as conn_store

_FORGED = "forged@evil.test"


def _set(**kv):
    settings.set_many(kv)
    settings.invalidate_cache()


def _auth_on():
    # Login wall ON, multi-org OFF -> isolate the role/scope/switch gate from org scoping.
    _set(security_auth_required="1", multi_org_enabled="0", remote_admin_enabled="1")


def _auth_off():
    _set(security_auth_required="0", multi_org_enabled="0", remote_admin_enabled="0")


def _h(raw):
    return {"X-Loc-Token": raw}


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:200]})"


_SEED = {}


def _seed():
    if _SEED:
        return _SEED
    init_db()
    admin = security.create_user("gjob-admin@x.com", "gjob-admin", Role.ADMIN, "gjob-password-1234")
    linguist = security.create_user(
        "gjob-ling@x.com", "gjob-ling", Role.LINGUIST, "gjob-password-1234"
    )
    translator = security.create_user(
        "gjob-trans@x.com", "gjob-trans", Role.TRANSLATOR, "gjob-password-1234"
    )
    viewer = security.create_user("gjob-view@x.com", "gjob-view", Role.VIEWER, "gjob-password-1234")

    def _key(u, scopes):
        _row, raw = apikeys.create_key(u.id, "gjob", scopes=scopes)
        return raw

    conn = conn_store.create("airtable", "gjob-conn", {}, {"api_key": "x"})
    _SEED.update(
        {
            # admin carries the admin SCOPE (R3 grant) + docs (the door's own grant).
            "admin": _key(admin, "api docs admin"),
            "linguist": _key(linguist, "api docs"),
            "translator": _key(translator, "api docs"),
            "viewer": _key(viewer, "api docs"),
            "linguist_email": linguist.email,
            "conn_id": conn.id,
            "admin_id": admin.id,
            "linguist_id": linguist.id,
        }
    )
    return _SEED


def _client():
    from app.main import app

    return TestClient(app)


def test_connector_role_and_actor(api, keys):
    """connector_translate through the door needs can_tune_ai (a translator is 403'd) and
    must attribute to the AUTHENTICATED caller, never a payload-forged actor."""
    _auth_on()
    seen = {}
    orig = conn_store.translate_now

    def _fake(cid, actor=""):
        seen["actor"] = actor
        seen["cid"] = cid
        return True, "stubbed", {}  # COMPLETED job (no real egress); actor already recorded

    conn_store.translate_now = _fake
    try:
        _is(
            api.post(
                "/api/jobs/run",
                headers=_h(keys["translator"]),
                json={"kind": "connector_translate", "payload": {"connection_id": keys["conn_id"]}},
            ),
            403,
            "connector_translate by translator (not can_tune_ai)",
        )
        _is(
            api.post(
                "/api/jobs/run",
                headers=_h(keys["viewer"]),
                json={"kind": "connector_translate", "payload": {"connection_id": keys["conn_id"]}},
            ),
            403,
            "connector_translate by viewer (can_write)",
        )
        # A linguist clears can_tune_ai, so the run reaches the (stubbed) connector handler;
        # the door must have replaced the forged actor with the linguist's own identity.
        r = api.post(
            "/api/jobs/run",
            headers=_h(keys["linguist"]),
            json={
                "kind": "connector_translate",
                "payload": {"connection_id": keys["conn_id"], "actor": _FORGED},
            },
        )
        assert r.status_code == 200, f"linguist connector run should reach the stub: {r.text[:200]}"
    finally:
        conn_store.translate_now = orig
    assert seen.get("actor") == keys["linguist_email"], (
        f"actor not overwritten to the caller: saw {seen.get('actor')!r}"
    )
    assert seen.get("actor") != _FORGED, "forged actor reached the connector handler"
    print("  [connector] translator 403 / viewer 403 / linguist forged actor overwritten OK")


def test_translate_document_role(api, keys):
    """translate_document keeps the door's can_write gate (no extra role): a viewer is 403'd."""
    _auth_on()
    _is(
        api.post(
            "/api/jobs/run",
            headers=_h(keys["viewer"]),
            json={"kind": "translate_document", "payload": {"document_id": 1, "locale": "fr-FR"}},
        ),
        403,
        "translate_document by viewer (can_write)",
    )
    print("  [translate_document] viewer 403 (can_write) OK")


def test_data_export_fail_closed(api, keys):
    """An existing foreign workspace export with a matching tuple cannot be replayed."""
    _auth_on()
    _set(data_export_enabled="1", encrypt_uploads="0", encrypt_db="0")
    with Session(engine) as s:
        admin = s.get(User, keys["admin_id"])
    row = data_export.run_export(user=admin, scope_kind="workspace")
    assert row.status == "ok", row.error
    before = row.model_dump()
    from app.ports.exportstore import get

    store = get()
    artifact = b"".join(store.open_range(row.artifact_key, 0, None))
    r = api.post(
        "/api/jobs/run",
        headers=_h(keys["translator"]),
        json={
            "kind": "data_export",
            "payload": {
                "run_uuid": row.run_uuid,
                "scope_kind": row.scope_kind,
                "scope_ref": row.scope_ref,
                "org": row.org_id,
                "customer_id": row.customer_id,
            },
        },
    )
    assert r.status_code == 400, (
        f"foreign data_export run_uuid must fail closed (400), got {r.status_code} ({r.text[:200]})"
    )
    assert r.json().get("ok") is False, f"expected ok=false, got {r.text[:200]}"
    assert r.json()["error"] == "unknown job kind: data_export"
    with Session(engine) as s:
        assert s.get(DataExportRun, row.id).model_dump() == before
    assert b"".join(store.open_range(row.artifact_key, 0, None)) == artifact
    print("  [data_export] existing foreign export refused without mutation OK")


def test_connector_project(api, keys):
    for ring in ("customer", "org", "membership"):
        _test_connector_project_ring(api, keys, ring)


def _test_connector_project_ring(api, keys, ring):
    """A global connection must not grant access to its foreign-customer project."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.models import Organization
    from app.web.connectors import _conn_or_404

    _auth_on()
    with Session(engine) as s:
        own, foreign = Customer(name="job-own"), Customer(name="job-foreign")
        s.add(own)
        s.add(foreign)
        s.commit()
        user = s.get(User, keys["linguist_id"])
        old_org, old_role = user.org_id, user.role
        user.customer_id = own.id if ring == "customer" else None
        project = Project(
            name="inaccessible", customer_id=foreign.id if ring == "customer" else None
        )
        if ring == "org":
            orgs = [Organization(name="conn-a"), Organization(name="conn-b")]
            s.add_all(orgs)
            s.commit()
            user.org_id = orgs[0].id
            project.org_id = orgs[1].id
        if ring == "membership":
            user.role = Role.TRANSLATOR
        s.add(user)
        s.add(project)
        s.commit()
        s.refresh(user)
        s.refresh(project)
        conn = s.get(Connection, keys["conn_id"])
        old_conn_org = conn.org_id
        conn.org_id = user.org_id
        conn.project_id = project.id
        s.add(conn)
        s.commit()
        s.refresh(user)
        pid = project.id
    _set(multi_org_enabled="1" if ring == "org" else "0", project_scoping="1")
    failures = []
    try:
        with patch.object(conn_store, "translate_now", return_value=(True, "stubbed", {})) as run:
            for explicit in (True, False):
                payload = {"connection_id": keys["conn_id"]}
                if explicit:
                    payload["project_id"] = pid
                r = api.post(
                    "/api/jobs/run",
                    headers=_h(keys["linguist"]),
                    json={"kind": "connector_translate", "payload": payload},
                )
                if r.status_code != 404:
                    failures.append(f"generic explicit={explicit}: {r.status_code}")
            r = api.post(f"/api/connectors/{keys['conn_id']}/sync", headers=_h(keys["linguist"]))
            # Dedicated connector actions apply the ID-independent AI role gate first.
            if r.status_code != (403 if ring == "membership" else 404):
                failures.append(f"dedicated: {r.status_code}")
            with patch.object(security, "session_user", return_value=user):
                request = SimpleNamespace(session={}, scope={"path": "/api/test"})
                _, blocked = _conn_or_404(request, keys["conn_id"])
                if blocked is None or blocked.status_code != 404:
                    failures.append("web helper allowed foreign project")
            if run.called:
                failures.append("connector egress seam reached")
        assert not failures, failures
    finally:
        _set(multi_org_enabled="0")
        with Session(engine) as s:
            user = s.get(User, keys["linguist_id"])
            user.customer_id = None
            user.org_id, user.role = old_org, old_role
            conn = s.get(Connection, keys["conn_id"])
            conn.project_id = None
            conn.org_id = old_conn_org
            s.add(user)
            s.add(conn)
            s.commit()
    print("  [connector] stored project confined across generic, dedicated and web doors OK")


def test_removed_kind_is_unknown(api, keys):
    """backfill_embeddings left with Semantic TM: the generic door answers a plain 400 unknown
    kind (never a launch) for a writer and an admin; a viewer never gets that far (403)."""
    _auth_on()
    for who in ("admin", "translator"):
        r = api.post(
            "/api/jobs/run",
            headers=_h(keys[who]),
            json={"kind": "backfill_embeddings", "payload": {}},
        )
        assert r.status_code == 400 and "unknown job kind" in r.text, (
            who,
            r.status_code,
            r.text[:200],
        )


def test_malformed_job_ids(api, keys):
    from unittest.mock import patch

    from app.services import jobs

    _auth_on()
    with patch.object(jobs, "run_sync", side_effect=AssertionError("malformed ID dispatched")):
        for field in ("connection_id", "project_id", "document_id"):
            for value in ([1], {"id": 1}, True, -1, 2**70):
                _is(
                    api.post(
                        "/api/jobs/run",
                        headers=_h(keys["admin"]),
                        json={"kind": "translate_document", "payload": {field: value}},
                    ),
                    400,
                    f"malformed {field}",
                )


def _seed_translate_doc():
    from app.models import Segment

    with Session(engine) as s:
        project = Project(name="gjob-budget", default_provider="mock")
        s.add(project)
        s.commit()
        s.refresh(project)
        doc = Document(project_id=project.id, filename="d.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                order_index=0,
                source_text="hello world " * 10,
                target_locale="fr-FR",
            )
        )
        s.commit()
        return doc.id


def test_translate_document_budget_parity(api, keys):
    """The generic door must refuse a paid-MT launch exactly where the dedicated
    /api/documents/{id}/translate does: department token cap and project USD cap (429)
    with the job NOT dispatched, an unknown locale (400), and the same server-stamped payload
    when allowed (caller-forged est_tokens/department are not trusted)."""
    from unittest.mock import patch

    from app.kernel import budgets, project_budgets
    from app.models import Job, JobStatus
    from app.services import jobs

    _auth_on()
    doc_id = _seed_translate_doc()
    body = {"kind": "translate_document", "payload": {"document_id": doc_id, "locale": "fr-FR"}}
    ded = {"targets": ["fr-FR"]}
    with patch.object(jobs, "run_sync") as run, patch.object(jobs, "submit", return_value=1):
        run.return_value = Job(kind="translate_document", status=JobStatus.COMPLETED)
        with (
            patch.object(
                project_budgets,
                "project_check",
                return_value=(False, 0.0, "project budget exhausted"),
            ),
            patch.object(project_budgets, "evaluate_thresholds"),
        ):
            _is(
                api.post(
                    f"/api/documents/{doc_id}/translate", headers=_h(keys["linguist"]), json=ded
                ),
                429,
                "dedicated door, project cap",
            )
            _is(
                api.post("/api/jobs/run", headers=_h(keys["linguist"]), json=body),
                429,
                "generic door, project cap",
            )
        with patch.object(budgets, "check", return_value=(False, 0, "department budget exhausted")):
            _is(
                api.post(
                    f"/api/documents/{doc_id}/translate", headers=_h(keys["linguist"]), json=ded
                ),
                429,
                "dedicated door, department cap",
            )
            _is(
                api.post("/api/jobs/run", headers=_h(keys["linguist"]), json=body),
                429,
                "generic door, department cap",
            )
        assert not run.called, "generic door dispatched a translate job past a budget cap"
        bad = {
            "kind": "translate_document",
            "payload": {"document_id": doc_id, "locale": "zz-NOPE"},
        }
        _is(api.post("/api/jobs/run", headers=_h(keys["linguist"]), json=bad), 400, "bad locale")
        _is(
            api.post(
                "/api/jobs/run",
                headers=_h(keys["linguist"]),
                json={"kind": "translate_document", "payload": {"document_id": doc_id}},
            ),
            400,
            "missing locale",
        )
        assert not run.called
    # Allowed: reaches the (stubbed) handler with a server-built payload only.
    forged = {"document_id": doc_id, "locale": "fr-FR", "est_tokens": 1, "extra": "x"}
    with patch.object(jobs, "run_sync") as run:
        run.return_value = Job(kind="translate_document", status=JobStatus.COMPLETED)
        r = api.post(
            "/api/jobs/run",
            headers=_h(keys["linguist"]),
            json={"kind": "translate_document", "payload": forged},
        )
        assert run.called, f"allowed launch did not dispatch: {r.status_code} {r.text[:200]}"
        sent = run.call_args.args[1]
        assert "extra" not in sent and sent["est_tokens"] > 1, sent
    print(
        "  [translate_document] budget caps / locale / payload rebuilt like the dedicated door OK"
    )


def test_translate_new_locale(api, keys):
    from unittest.mock import patch

    from sqlmodel import select

    from app.models import Segment, TargetLocale
    from app.services import budgets

    _auth_on()
    _set(project_scoping="0")
    doc_id = _seed_translate_doc()
    body = {
        "kind": "translate_document",
        "payload": {"document_id": doc_id, "locale": "de-DE", "provider": "mock"},
    }
    _is(api.post("/api/jobs/run", headers=_h(keys["viewer"]), json=body), 403, "viewer")
    with patch.object(budgets, "check", return_value=(False, 0, "budget exhausted")):
        _is(api.post("/api/jobs/run", headers=_h(keys["translator"]), json=body), 429, "budget")
    with Session(engine) as s:
        assert not s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all(), "refused caller seeded segments"
    r = api.post("/api/jobs/run", headers=_h(keys["translator"]), json=body)
    _is(r, 200, "new locale translation")
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        targets = s.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == doc.project_id, TargetLocale.locale == "de-DE"
            )
        ).all()
        segments = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all()
        assert len(targets) == len(segments) == 1, "new locale was not registered and seeded"
        assert segments[0].target_text, "COMPLETED without translating the new locale"
    _is(api.post("/api/jobs/run", headers=_h(keys["translator"]), json=body), 200, "repeat")
    with Session(engine) as s:
        assert (
            len(
                s.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == "de-DE"
                    )
                ).all()
            )
            == 1
        )


def test_translate_new_locale_no_foreign_seed(api, keys):
    """#436 must not turn the shared seeding helper into a write for a caller the job API
    refuses: a non-member (foreign customer) and a key without the docs scope get a refusal
    and NOTHING is registered or seeded for the new locale."""
    from sqlmodel import select

    from app.models import Segment, TargetLocale

    _auth_on()
    doc_id = _seed_translate_doc()
    with Session(engine) as s:
        own, foreign = Customer(name="seed-own"), Customer(name="seed-foreign")
        s.add(own)
        s.add(foreign)
        s.commit()
        user = s.get(User, keys["linguist_id"])
        old_customer = user.customer_id
        user.customer_id = own.id
        doc = s.get(Document, doc_id)
        project = s.get(Project, doc.project_id)
        project.customer_id = foreign.id
        s.add(user)
        s.add(project)
        s.commit()
        pid = project.id
    _, narrow = apikeys.create_key(keys["linguist_id"], "gjob-narrow", scopes="api")
    _set(project_scoping="1")
    body = {
        "kind": "translate_document",
        "payload": {"document_id": doc_id, "locale": "es-ES", "provider": "mock"},
    }
    try:
        r = api.post("/api/jobs/run", headers=_h(keys["linguist"]), json=body)
        assert r.status_code in (403, 404), f"non-member: {r.status_code} {r.text[:200]}"
        r = api.post("/api/jobs/run", headers=_h(narrow), json=body)
        assert r.status_code in (401, 403, 404), f"narrow key: {r.status_code} {r.text[:200]}"
        with Session(engine) as s:
            assert not s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "es-ES"
                )
            ).all(), "refused caller seeded segments"
            assert not s.exec(
                select(TargetLocale).where(
                    TargetLocale.project_id == pid, TargetLocale.locale == "es-ES"
                )
            ).all(), "refused caller registered a target locale"
    finally:
        _set(project_scoping="0")
        with Session(engine) as s:
            user = s.get(User, keys["linguist_id"])
            user.customer_id = old_customer
            s.add(user)
            s.commit()


def test_translate_empty_job(api, keys):
    from app.models import JobStatus
    from app.services import jobs

    _auth_on()
    _set(project_scoping="0")
    doc_id = _seed_translate_doc()
    # Host-local CLI job run uses this service directly. It must refuse an
    # unseeded locale explicitly instead of reporting a zero-work success.
    job = jobs.run_sync(
        "translate_document", {"document_id": doc_id, "locale": "it-IT", "provider": "mock"}
    )
    assert job.status == JobStatus.FAILED, "zero-segment job reported COMPLETED"
    assert "segments" in job.error.lower(), job.error
    with Session(engine) as s:
        source = s.get(Document, doc_id)
        empty = Document(project_id=source.project_id, filename="empty.txt", fmt="txt")
        s.add(empty)
        s.commit()
        empty_id = empty.id
    for endpoint, body in (
        (
            "/api/jobs/run",
            {
                "kind": "translate_document",
                "payload": {"document_id": empty_id, "locale": "de-DE", "provider": "mock"},
            },
        ),
        (f"/api/documents/{empty_id}/translate", {"targets": ["de-DE"], "provider": "mock"}),
    ):
        response = api.post(endpoint, headers=_h(keys["translator"]), json=body)
        _is(response, 400, "empty source document")
        assert "segments" in response.json()["error"].lower()


def test_connector_payload_parity(api, keys):
    """connector_translate through the generic door: retry_failures is capped (1000) and
    sanitized by the dedicated retry door's own function, webhook-only keys are a 400, and
    only the allowlisted keys reach the handler."""
    from unittest.mock import patch

    _auth_on()
    cid = keys["conn_id"]
    junk = [{"record_id": f"rec{i}", "evil": "x" * 10, "locale": "fr-FR"} for i in range(5000)]
    with patch.object(conn_store, "retry_failed", return_value=(True, "stub", {})) as retry:
        r = api.post(
            f"/api/connectors/{cid}/retry", headers=_h(keys["linguist"]), json={"failures": junk}
        )
        _is(r, 400, "dedicated retry door, 5000 failures")
        r = api.post(
            "/api/jobs/run",
            headers=_h(keys["linguist"]),
            json={
                "kind": "connector_translate",
                "payload": {"connection_id": cid, "retry_failures": junk},
            },
        )
        _is(r, 400, "generic door, 5000 failures")
        assert not retry.called, "5000 raw failures reached the connector"
        for key, val in (
            ("webhook_record_ids", ["a"]),
            ("webhook_article_ids", ["a"]),
            ("webhook_ping_pull", True),
        ):
            r = api.post(
                "/api/jobs/run",
                headers=_h(keys["linguist"]),
                json={"kind": "connector_translate", "payload": {"connection_id": cid, key: val}},
            )
            _is(r, 400, f"generic door, {key}")
        r = api.post(
            "/api/jobs/run",
            headers=_h(keys["linguist"]),
            json={
                "kind": "connector_translate",
                "payload": {"connection_id": cid, "retry_failures": [*junk[:3], {"nope": 1}]},
            },
        )
        _is(r, 200, "generic door, small sanitized retry")
        got = retry.call_args.args[1]
        assert got == [{"record_id": f"rec{i}", "locale": "fr-FR"} for i in range(3)], got
        r = api.post(
            "/api/jobs/run",
            headers=_h(keys["linguist"]),
            json={
                "kind": "connector_translate",
                "payload": {"connection_id": cid, "retry_failures": [{"nope": 1}]},
            },
        )
        _is(r, 400, "generic door, retry with no usable id")
    print("  [connector] retry cap/sanitize + webhook-only keys refused on the generic door OK")


def main():
    keys = _seed()
    api = _client()
    try:
        test_connector_role_and_actor(api, keys)
        test_translate_document_role(api, keys)
        failures = []
        for test in (
            test_connector_project,
            test_data_export_fail_closed,
            test_malformed_job_ids,
            test_removed_kind_is_unknown,
            test_translate_document_budget_parity,
            test_connector_payload_parity,
            test_translate_new_locale,
            test_translate_empty_job,
            test_translate_new_locale_no_foreign_seed,
        ):
            try:
                test(api, keys)
            except AssertionError as exc:
                failures.append(f"{test.__name__}: {exc}")
        assert not failures, failures
    finally:
        _auth_off()
    print("GENERIC JOB AUTHZ OK")


if __name__ == "__main__":
    main()
