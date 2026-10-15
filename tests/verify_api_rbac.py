"""Day-2 hardening #7: /api remote-CLI write-role RBAC.

The /api surface (app/web/api_cli.py) is the WEB side of the remote CLI. It has
always been org- and customer-bound (a token can never reach another tenant's
rows), but it historically enforced NO ROLE: any valid docs-scoped lsu_ key could
call every write endpoint regardless of the owner's role. A viewer's token could
create projects, import TM/glossary, run workflows, etc.

This suite proves the role gate now added to every /api WRITE endpoint mirrors the
equivalent web route's predicate (the parity principle):

  * can_write  (>= translator): project create / add-locale / apply-langset,
                 tm import, glossary import / add, workflow run, run resume,
                 job run.
  * can_manage (>= manager):    customer create.
  * can_tune_ai (admin/linguist): language-set create / update / delete
                 (mirrors the web /languages routes, which use _ai_block).

It asserts, with the login wall ON but multi-org OFF (so the behavior under test
is PURELY the role gate, isolated from org scoping):
  - a VIEWER token is 403'd on every write class, but can still READ;
  - a TRANSLATOR token is allowed on can_write writes, but 403'd on the
    can_manage (customer create) and can_tune_ai (language set) writes;
  - a LINGUIST token is allowed on the language-set writes (can_tune_ai);
  - an ADMIN token is allowed everywhere;
  - with the login wall OFF (single-tenant dev default), the gate is a strict
    no-op -- a viewer's token writes exactly as before (byte-identical).

It ALSO covers the OTHER /api router (app/web/api.py, the docs/MCP front door),
which had the SAME gap on its 7 connector write endpoints (pull/push/sync/retry/
jira-translate/confluence-translate/airtable-webhook): a valid docs-scoped lsu_
key of ANY role could pull/push/sync/retry a connector. Their web equivalents
(app/web/connectors.py pull/push/sync/retry) all require _ai_block (can_tune_ai:
admin or linguist), so api.py now gates the same 7 endpoints with the same
predicate (_ai_role_block). Per-user (lsu_) callers: viewer/translator/manager
403, linguist/admin pass the role gate (they may then still fail deeper with a
connector-level error like "unknown connection" -- these tests assert only that
the failure is NOT a 403, i.e. the role gate itself let them through).

ASCII-only output; standalone one-process-per-suite script (LOC_TMS_DATA_DIR is a
throwaway temp dir, provider keys cleared -> MockProvider, deterministic + free).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_apirbac_")

import base64

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    Role,
    Segment,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access, apikeys, content, languages, security, settings


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _auth_on() -> None:
    """Login wall ON, multi-org OFF -- isolate the role gate from org scoping."""
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_off() -> None:
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _key_for(user) -> str:
    """A docs-scoped lsu_ key for a user (the /api surface requires the docs scope)."""
    _row, raw = apikeys.create_key(user.id, "rbac-test", scopes="api docs")
    return raw


def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


_TMX = (
    b'<?xml version="1.0"?><tmx version="1.4"><body>'
    b'<tu><tuv xml:lang="en-US"><seg>hello</seg></tuv>'
    b'<tuv xml:lang="fr-FR"><seg>bonjour</seg></tuv></tu></body></tmx>'
)
_CSV = b"source,target\nhello,bonjour\n"


_SEED_N = 0


def _seed():
    """One admin/manager/linguist/translator/viewer user (each with a docs key), a
    project + document (with fr-FR segments) + a workflow + a language set, all
    created directly so the fixtures exist regardless of role gating. Returns a
    dict of clients/keys/ids.

    Each call uses a fresh unique suffix so re-seeding in the same process/DB (the
    standalone runner drives all tests in ONE process; init_db does NOT wipe) never
    collides on a duplicate email / row."""
    global _SEED_N
    _SEED_N += 1
    n = _SEED_N
    init_db()
    keys = {}
    with Session(engine) as s:
        # A project + a document with a couple of fr-FR segments (for workflow run).
        proj = content.project_create(
            s,
            "RBAC Project",
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by="admin@x.com",
        )
        pid = proj.id
        doc = Document(
            project_id=pid, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        s.add(
            Segment(
                document_id=did,
                source_text="hello",
                target_text="",
                target_locale="fr-FR",
                order_index=0,
            )
        )
        s.add(
            Segment(
                document_id=did,
                source_text="world",
                target_text="",
                target_locale="fr-FR",
                order_index=1,
            )
        )
        # A single-step MT workflow so workflow run has something to resolve.
        wf = Workflow(name="RBAC WF", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
        s.add(
            WorkflowStep(
                workflow_id=wid,
                order_index=0,
                name="mt",
                step_type="mt",
                provider="mock",
                model="mock",
            )
        )
        s.commit()

    # A language set (via the service so the id exists).
    ls = languages.create(f"RBAC Set {n}", "en-US", "fr-FR", actor="admin@x.com")
    sid = ls.id
    with Session(engine) as s:
        run = WorkflowRun(workflow_id=wid, document_id=did, status="completed")
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id

    # A customer for the customer-configuration writes (settings/defaults/pairs).
    with Session(engine) as s:
        custid = content.customer_create(s, f"RBAC Cust {n}").id

    users = {}
    for role in (
        Role.ADMIN,
        Role.MANAGER,
        Role.LINGUIST,
        Role.REVIEWER,
        Role.TRANSLATOR,
        Role.VIEWER,
    ):
        u = security.create_user(f"{role}{n}@x.com", role.title(), role, f"{role}-password-123")
        keys[role] = _key_for(u)
        users[role] = u
        # Test role permissions on a visible project; membership has its own refusal suite.
        with Session(engine) as s:
            assert access.add_member(s, pid, u.email)[0]

    # A minimal connector connection (api.py connector endpoints under test).
    from app.services.connectors import store as conn_store

    conn = conn_store.create("airtable", f"rbac-conn-{n}", {}, {"api_key": "x"})
    cid = conn.id

    return {
        "pid": pid,
        "did": did,
        "wid": wid,
        "rid": rid,
        "sid": sid,
        "cid": cid,
        "custid": custid,
        "keys": keys,
        "users": users,
    }


def _forbidden(r) -> bool:
    return r.status_code == 403 and r.json().get("ok") is False


def _not_forbidden(r) -> bool:
    """The role gate passed: anything but a 403 (a downstream 200/400/404 all mean
    the request cleared the RBAC wall and reached the business logic)."""
    return r.status_code != 403


# The write endpoints under test, as callables (client-header -> Response), tagged
# with the predicate class. Each closure hits ONE /api write endpoint.
def _writes(api: TestClient, d):
    pid, did, wid, sid = d["pid"], d["did"], d["wid"], d["sid"]
    custid = d["custid"]
    return [
        (
            "can_write",
            "POST /api/projects",
            lambda h: api.post("/api/projects", headers=h, json={"name": "P", "targets": "fr-FR"}),
        ),
        (
            "can_write",
            "POST /api/projects/{id}/add-locale",
            lambda h: api.post(
                f"/api/projects/{pid}/add-locale", headers=h, json={"locale": "de-DE"}
            ),
        ),
        (
            "can_write",
            "POST /api/projects/{id}/apply-langset",
            lambda h: api.post(
                f"/api/projects/{pid}/apply-langset", headers=h, json={"langset_id": sid}
            ),
        ),
        (
            "can_write",
            "POST /api/projects/{id}/tm/import",
            lambda h: api.post(
                f"/api/projects/{pid}/tm/import",
                headers=h,
                json={"source": "en-US", "target": "fr-FR", "tmx_b64": _b64(_TMX)},
            ),
        ),
        (
            "can_write",
            "POST /api/projects/{id}/glossary/import",
            lambda h: api.post(
                f"/api/projects/{pid}/glossary/import",
                headers=h,
                json={"source": "en-US", "target": "fr-FR", "csv_b64": _b64(_CSV)},
            ),
        ),
        (
            "can_write",
            "POST /api/projects/{id}/glossary",
            lambda h: api.post(
                f"/api/projects/{pid}/glossary",
                headers=h,
                json={"source_term": "hi", "target": "fr-FR"},
            ),
        ),
        (
            "can_write",
            "POST /api/workflows/{id}/run",
            lambda h: api.post(
                f"/api/workflows/{wid}/run", headers=h, json={"doc_id": did, "locale": "fr-FR"}
            ),
        ),
        (
            "can_write",
            "POST /api/runs/{id}/resume",
            lambda h: api.post(f"/api/runs/{d['rid']}/resume", headers=h, json={}),
        ),
        (
            "can_write",
            "POST /api/jobs/run",
            lambda h: api.post(
                "/api/jobs/run",
                headers=h,
                json={
                    "kind": "translate_document",
                    "payload": {"document_id": did, "locale": "fr-FR"},
                },
            ),
        ),
        (
            "can_manage",
            "POST /api/customers",
            lambda h: api.post("/api/customers", headers=h, json={"name": "Cust"}),
        ),
        (
            "can_tune_ai",
            "POST /api/langsets",
            lambda h: api.post(
                "/api/langsets",
                headers=h,
                json={"name": "Set2", "source": "en-US", "targets": "fr-FR"},
            ),
        ),
        (
            "can_tune_ai",
            "POST /api/langsets/{id}",
            lambda h: api.post(f"/api/langsets/{sid}", headers=h, json={"name": "Renamed"}),
        ),
        (
            "can_tune_ai",
            "POST /api/langsets/{id}/delete",
            lambda h: api.post(f"/api/langsets/{sid}/delete", headers=h, json={}),
        ),
        # Customer configuration (2026-09-26): admin, manager or linguist.
        (
            "can_configure_customer",
            "POST /api/context/profiles",
            lambda h: api.post(
                "/api/context/profiles",
                headers=h,
                json={"customer_id": custid, "context": {"notes": "rbac card"}},
            ),
        ),
        (
            "can_configure_customer",
            "POST /api/customers/{id}/settings",
            lambda h: api.post(
                f"/api/customers/{custid}/settings",
                headers=h,
                json={"settings": {"notes": "rbac"}},
            ),
        ),
        (
            "can_configure_customer",
            "POST /api/customers/{id}/defaults",
            lambda h: api.post(
                f"/api/customers/{custid}/defaults", headers=h, json={"provider": "gemini"}
            ),
        ),
        (
            "can_configure_customer",
            "POST /api/customers/{id}/pairs/add",
            lambda h: api.post(
                f"/api/customers/{custid}/pairs/add", headers=h, json={"pair": "en-US->fr-FR"}
            ),
        ),
        (
            "can_configure_customer",
            "POST /api/customers/{id}/pairs/remove",
            lambda h: api.post(
                f"/api/customers/{custid}/pairs/remove",
                headers=h,
                json={"pair": "en-US->fr-FR"},
            ),
        ),
        (
            "can_configure_customer",
            "POST /api/customers/{id}/pairs/import",
            lambda h: api.post(
                f"/api/customers/{custid}/pairs/import",
                headers=h,
                json={"csv_b64": _b64(b"source_locale,target_locale\nen-US,de-DE\n")},
            ),
        ),
    ]


# The api.py connector write endpoints under test (all _ai_role_block-gated,
# mirroring the web /connectors pull/push/sync/retry/jira/confluence routes'
# _ai_block). No "airtable/webhook" here on purpose -- see
# test_airtable_webhook_shared_token_is_a_noop below for its distinct coverage
# (the shared connector token must NOT be gated, only a direct per-user caller).
def _connector_writes(api: TestClient, d):
    cid = d["cid"]
    return [
        (
            "POST /api/connectors/{id}/pull",
            lambda h: api.post(f"/api/connectors/{cid}/pull", headers=h),
        ),
        (
            "POST /api/connectors/{id}/push",
            lambda h: api.post(f"/api/connectors/{cid}/push", headers=h),
        ),
        (
            "POST /api/connectors/{id}/sync",
            lambda h: api.post(f"/api/connectors/{cid}/sync", headers=h, json={}),
        ),
        (
            "POST /api/connectors/{id}/retry",
            lambda h: api.post(
                f"/api/connectors/{cid}/retry",
                headers=h,
                json={"failures": [{"record_id": "rec1"}]},
            ),
        ),
        (
            "POST /api/connectors/{id}/jira/translate",
            lambda h: api.post(
                f"/api/connectors/{cid}/jira/translate", headers=h, json={"issue_key": "ABC-1"}
            ),
        ),
        (
            "POST /api/connectors/{id}/confluence/translate",
            lambda h: api.post(
                f"/api/connectors/{cid}/confluence/translate", headers=h, json={"page_id": "1"}
            ),
        ),
    ]


def _reads(api: TestClient, d):
    pid = d["pid"]
    return [
        ("GET /api/projects", lambda h: api.get("/api/projects", headers=h)),
        ("GET /api/projects/{id}", lambda h: api.get(f"/api/projects/{pid}", headers=h)),
        ("GET /api/customers", lambda h: api.get("/api/customers", headers=h)),
        ("GET /api/langsets", lambda h: api.get("/api/langsets", headers=h)),
        ("GET /api/workflows", lambda h: api.get("/api/workflows", headers=h)),
        ("GET /api/jobs", lambda h: api.get("/api/jobs", headers=h)),
    ]


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_viewer_blocked_on_every_write() -> None:
    """A viewer's docs-scoped token is 403'd on EVERY write endpoint."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    hv = _h(d["keys"][Role.VIEWER])
    for _pred, name, call in _writes(api, d):
        r = call(hv)
        assert _forbidden(r), (
            f"viewer should be 403 on {name}, got {r.status_code} ({r.text[:120]})"
        )
    print("  viewer: 403 on all %d write endpoints" % len(_writes(api, d)))  # noqa: UP031


def test_viewer_can_still_read() -> None:
    """A viewer's token still READS within scope -- reads stay role-ungated."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    hv = _h(d["keys"][Role.VIEWER])
    for name, call in _reads(api, d):
        r = call(hv)
        assert r.status_code == 200 and r.json().get("ok") is True, (
            f"viewer read {name} should be 200/ok, got {r.status_code} ({r.text[:120]})"
        )
    print("  viewer: reads still work (%d read endpoints)" % len(_reads(api, d)))  # noqa: UP031


def test_translator_allowed_on_writes_blocked_on_manage_and_ai() -> None:
    """A translator (can_write) clears the can_write writes but is 403'd on the
    can_manage (customer create), can_tune_ai (language set) and
    can_configure_customer (customer settings/defaults/pairs) endpoints."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    ht = _h(d["keys"][Role.TRANSLATOR])
    for pred, name, call in _writes(api, d):
        r = call(ht)
        if pred == "can_write":
            assert _not_forbidden(r), (
                f"translator should clear can_write {name}, got 403 ({r.text[:120]})"
            )
        else:
            assert _forbidden(r), (
                f"translator should be 403 on {name} ({pred}), got {r.status_code}"
            )
    print("  translator: passes can_write, 403 on can_manage + can_tune_ai")


def test_linguist_allowed_on_langsets() -> None:
    """A linguist (can_tune_ai) clears the language-set writes AND can_write writes,
    but is still 403'd on can_manage (customer create -- linguist rank < manager)."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    hl = _h(d["keys"][Role.LINGUIST])
    for pred, name, call in _writes(api, d):
        r = call(hl)
        if pred == "can_manage":
            assert _forbidden(r), (
                f"linguist should be 403 on can_manage {name}, got {r.status_code}"
            )
        else:
            assert _not_forbidden(r), (
                f"linguist should clear {name} ({pred}), got 403 ({r.text[:120]})"
            )
    print("  linguist: clears can_write + can_tune_ai, 403 on can_manage")


def test_admin_allowed_everywhere() -> None:
    """An admin token clears every write class."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    ha = _h(d["keys"][Role.ADMIN])
    for _pred, name, call in _writes(api, d):
        r = call(ha)
        assert _not_forbidden(r), f"admin should clear {name}, got 403 ({r.text[:120]})"
    print("  admin: clears all %d write endpoints" % len(_writes(api, d)))  # noqa: UP031


def test_manager_blocked_on_langsets_allowed_on_customer() -> None:
    """A manager (can_manage) clears can_write + can_manage (customer create) AND the
    can_configure_customer writes (customer settings/defaults/pairs; 2026-09-26:
    managers keep customer configuration), but is 403'd on the can_tune_ai
    language-set writes (manager is not admin/linguist). This proves the language-set
    gate really is can_tune_ai, not the rank ladder."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    hm = _h(d["keys"][Role.MANAGER])
    for pred, name, call in _writes(api, d):
        r = call(hm)
        if pred == "can_tune_ai":
            assert _forbidden(r), (
                f"manager should be 403 on can_tune_ai {name}, got {r.status_code}"
            )
        elif pred == "can_configure_customer":
            assert r.status_code == 200 and r.json().get("ok") is True, (
                f"manager config failed: {name}: {r.status_code} {r.text[:120]}"
            )
        else:
            assert _not_forbidden(r), (
                f"manager should clear {name} ({pred}), got 403 ({r.text[:120]})"
            )
    print("  manager: clears can_write + can_manage + customer config, 403 on can_tune_ai")


def test_context_profile_persistence_and_ai_boundary() -> None:
    """A successful profile POST must store the card; unrelated AI doors stay closed."""
    from app.services import context_pkg

    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    for role in (
        Role.ADMIN,
        Role.MANAGER,
        Role.LINGUIST,
        Role.TRANSLATOR,
        Role.REVIEWER,
        Role.VIEWER,
    ):
        h = _h(d["keys"][role])
        with Session(engine) as s:
            card = context_pkg.upsert_profile(
                s, customer_id=d["custid"], context_raw={"notes": "before"}
            )
            card_id = card.id
        allowed = role in (Role.ADMIN, Role.MANAGER, Role.LINGUIST)
        r = api.post(
            "/api/context/profiles",
            headers=h,
            json={"customer_id": d["custid"], "context": {"notes": role}},
        )
        assert r.status_code == (200 if allowed else 403), (role, r.status_code)
        with Session(engine) as s:
            stored = context_pkg.profile_row(context_pkg.get_profile(s, card_id))
        assert stored["context"]["notes"] == (role if allowed else "before")
    hm = _h(d["keys"][Role.MANAGER])
    for path, body in (
        (f"/api/context/documents/{d['did']}", {"context": {"notes": "denied"}}),
        ("/api/context/ab", {"document": d["did"], "provider": "mock"}),
    ):
        assert _forbidden(api.post(path, headers=hm, json=body)), path
    _auth_off()
    print("  profile persistence: six roles; manager document override and A/B still 403")


def test_context_profile_resolved_scope_roles() -> None:
    """Org-default writes/deletes are admin-only; scoped role gates stay intact."""
    from app.models import ContextProfile, User
    from app.services import content_types, context_pkg

    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    with Session(engine) as s:
        ctid = content_types.create(s, "Scope role card type").id

    def snapshot():
        with Session(engine) as s:
            return [p.model_dump() for p in s.exec(select(ContextProfile)).all()]

    failures = []
    for scope in (
        {},
        {"project_id": d["pid"]},
        {"content_type_id": ctid},
        {"customer_id": d["custid"]},
    ):
        # Test both creation and overwriting a pre-existing card for each role.
        for role in (
            Role.MANAGER,
            Role.LINGUIST,
            Role.ADMIN,
            Role.TRANSLATOR,
            Role.REVIEWER,
            Role.VIEWER,
        ):
            client = _web_login(d, role)
            for existing in (False, True):
                with Session(engine) as s:
                    card = context_pkg.find_by_scope(s, **scope)
                    if card is not None:
                        s.delete(card)
                        s.commit()
                    if existing:
                        context_pkg.upsert_profile(s, **scope, context_raw={"notes": "before"})
                before = snapshot()
                notes = f"{role} existing={existing}"
                r = api.post(
                    "/api/context/profiles",
                    headers=_h(d["keys"][role]),
                    json={**scope, "context": {"notes": notes}},
                )
                allowed = role == Role.ADMIN or (
                    bool(scope)
                    and (role == Role.LINGUIST or (role == Role.MANAGER and "customer_id" in scope))
                )
                if r.status_code != (200 if allowed else 403):
                    failures.append(f"{role} {scope} existing={existing}: {r.status_code}")
                if not allowed:
                    if snapshot() != before:
                        failures.append(f"refused scope mutated: {scope} existing={existing}")
                else:
                    with Session(engine) as s:
                        card = context_pkg.find_by_scope(s, **scope)
                        assert card is not None
                        assert context_pkg.profile_row(card)["context"]["notes"] == notes
                if not scope:
                    # Rebuild the fixture independently for the web save door.
                    with Session(engine) as s:
                        card = context_pkg.find_by_scope(s)
                        if card is not None:
                            s.delete(card)
                            s.commit()
                        if existing:
                            context_pkg.upsert_profile(s, context_raw={"notes": "before"})
                    before = snapshot()
                    r = client.post(
                        "/settings/context-default",
                        data={"notes": notes},
                        follow_redirects=False,
                    )
                    if r.status_code != (303 if allowed else 403):
                        failures.append(f"web save {role} existing={existing}: {r.status_code}")
                    if not allowed:
                        if snapshot() != before:
                            failures.append(f"web save mutated: {role} existing={existing}")
                    else:
                        with Session(engine) as s:
                            card = context_pkg.find_by_scope(s)
                            assert context_pkg.profile_row(card)["context"]["notes"] == notes
            with Session(engine) as s:
                card_id = context_pkg.upsert_profile(s, **scope, context_raw={"notes": "keep"}).id
            before = snapshot()
            # Scoped web deletes retain can_configure_customer, including managers.
            delete_allowed = role == Role.ADMIN or (
                bool(scope) and role in (Role.MANAGER, Role.LINGUIST)
            )
            r = client.post(f"/context-cards/{card_id}/delete", follow_redirects=False)
            if r.status_code != (303 if delete_allowed else 403):
                failures.append(f"web delete {role} {scope}: {r.status_code}")
            if not delete_allowed:
                if snapshot() != before:
                    failures.append(f"web delete mutated: {role} {scope}")
            else:
                with Session(engine) as s:
                    assert context_pkg.get_profile(s, card_id) is None
    with Session(engine) as s:
        user = s.get(User, d["users"][Role.MANAGER].id)
        user.customer_id = d["custid"]
        s.add(user)
        s.commit()
    r = api.post(
        "/api/context/profiles",
        headers=_h(d["keys"][Role.MANAGER]),
        json={"context": {"notes": "bound manager"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["customer_id"] == d["custid"]
    with Session(engine) as s:
        card = context_pkg.find_by_scope(s, customer_id=d["custid"])
        assert context_pkg.profile_row(card)["context"]["notes"] == "bound manager"
    assert not failures, "; ".join(failures)
    _auth_off()
    print("  profile scopes: org-default admin-only; six-role scoped matrix; bound manager allowed")


def test_bound_context_card_web_scopes() -> None:
    """A card's indirect project/content-type scope is an authorization boundary."""
    from app.models import User
    from app.services import content_types, context_pkg

    d = _seed()
    _auth_on()
    own = d["custid"]
    with Session(engine) as s:
        other = content.customer_create(s, "Other card customer").id
        own_ct = content_types.create(s, "Own card type", customer_id=own).id
        other_ct = content_types.create(s, "Other card type", customer_id=other).id
        own_proj = content.project_create(s, "Own card project", customer_id=own).id
        other_proj = content.project_create(s, "Other card project", customer_id=other).id
        u = s.get(User, d["users"][Role.LINGUIST].id)
        u.customer_id = own
        s.add(u)
        s.commit()
    client = _web_login(d, Role.LINGUIST)
    failures = []
    for scope, allowed in (
        ({"customer_id": own}, True),
        ({"project_id": own_proj}, True),
        ({"content_type_id": own_ct}, True),
        ({"customer_id": other}, False),
        ({"project_id": other_proj}, False),
        ({"content_type_id": other_ct}, False),
        ({"customer_id": own, "project_id": other_proj}, False),
        ({}, False),
    ):
        with Session(engine) as s:
            card_id = context_pkg.upsert_profile(s, **scope, context_raw={"notes": "keep"}).id
        r = client.post(f"/context-cards/{card_id}/delete", follow_redirects=False)
        with Session(engine) as s:
            exists = context_pkg.get_profile(s, card_id) is not None
        if r.status_code != (303 if allowed else 404) or exists == allowed:
            failures.append(f"delete {scope}: status {r.status_code}, exists {exists}")
    r = client.post(
        f"/customers/{own}/context-cards",
        data={"content_type_id": other_ct, "notes": "foreign"},
        follow_redirects=False,
    )
    if r.status_code != 404:
        failures.append(f"save foreign content type: {r.status_code}")
    with Session(engine) as s:
        if context_pkg.find_by_scope(s, customer_id=own, content_type_id=other_ct):
            failures.append("foreign content-type card was planted")
    assert not failures, "; ".join(failures)
    _auth_off()
    print("  bound web cards: own scopes allowed, foreign/global scopes refused without mutation")


def test_customer_config_role_descriptions() -> None:
    """Published help and agent descriptions must name the customer-config gate."""
    from pathlib import Path

    help_text = Path("app/templates/help.html").read_text(encoding="utf-8")
    for path in (
        "/api/customers/{cid}/settings",
        "/api/customers/{cid}/defaults",
        "/api/customers/{cid}/pairs/add",
        "/api/customers/{cid}/pairs/remove",
        "/api/customers/{cid}/pairs/import",
    ):
        row = next(line for line in help_text.splitlines() if f"<code>{path}</code>" in line)
        assert "<td>admin, manager, linguist</td>" in row, path
    llm = Path("app/capabilities/llm.md").read_text(encoding="utf-8")
    assert "**can_configure_customer** - admin, manager or linguist" in llm
    assert "`/pairs/import` (can_configure_customer" in llm
    assert "writes use `can_configure_customer`" in llm
    assert "**Context cards (R1b, scope-dependent role)**" in llm
    cli = Path("docs/CLI.md").read_text(encoding="utf-8")
    assert "Remote context-card authoring (`context set-profile`) stays" not in cli
    profile_roles = (
        "org-default card: admin only; customer-scoped: admin, manager, linguist; "
        "project or content-type scoped: admin, linguist"
    )
    row = next(
        line for line in help_text.splitlines() if "<code>/api/context/profiles</code>" in line
    )
    assert f"<td>{profile_roles}</td>" in row
    for text in (llm, cli, Path("llms.txt").read_text(encoding="utf-8")):
        assert profile_roles in text


def test_reviewer_refused_on_customer_config() -> None:
    """A reviewer (rank 2, same as linguist) clears the can_write writes but is 403'd
    on can_manage, can_tune_ai AND the can_configure_customer writes: customer
    configuration is admin/manager/linguist by ROLE, not by rank (#284 stays fixed)."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    hr = _h(d["keys"][Role.REVIEWER])
    for pred, name, call in _writes(api, d):
        r = call(hr)
        if pred == "can_write":
            assert _not_forbidden(r), (
                f"reviewer should clear can_write {name}, got 403 ({r.text[:120]})"
            )
        else:
            assert _forbidden(r), f"reviewer should be 403 on {name} ({pred}), got {r.status_code}"
    print("  reviewer: passes can_write, 403 on can_manage + can_tune_ai + customer config")


def _web_login(d, role) -> TestClient:
    """A web client signed in as the seeded user of `role` (CSRF primed)."""
    from tests._csrf import csrf_for

    client = TestClient(app_client())
    csrf_for(client)
    client.post(
        "/auth/login",
        data={
            "email": d["users"][role].email,
            "password": f"{role}-password-123",  # pregate: allow-secret test fixture
        },
        follow_redirects=False,
    )
    csrf_for(client)
    return client


def test_manager_configures_customer_on_web() -> None:
    """2026-09-26 (follow-up to F10 #284): the WEB customer-config doors
    (settings, context card save/delete, pair add/remove/import) admit a MANAGER
    (303), while a translator, reviewer or viewer still gets 403. The Customer 360
    page shows a manager the config forms (can_edit) but NOT the content-type
    Add/Delete controls, whose routes stay can_tune_ai."""
    from app.services import content_types, context_pkg, customers

    d = _seed()
    _auth_on()
    custid = d["custid"]
    with Session(engine) as s:
        ctid = content_types.create(s, "RBAC content", customer_id=custid).id
    failures = []
    for role, allowed in (
        (Role.ADMIN, True),
        (Role.MANAGER, True),
        (Role.LINGUIST, True),
        (Role.TRANSLATOR, False),
        (Role.REVIEWER, False),
        (Role.VIEWER, False),
    ):
        with Session(engine) as s:
            card_id = context_pkg.upsert_profile(
                s, customer_id=custid, context_raw={"notes": "Original"}
            ).id
            customers.add_pair(s, custid, "en-US->fr-FR")
        client = _web_login(d, role)
        page = client.get(f"/customers/{custid}")
        assert page.status_code == 200, (role, page.status_code)
        for marker in (
            "Save preferences",
            "Save card",
            f'action="/context-cards/{card_id}/delete"',
            f'action="/customers/{custid}/pairs"',
            f'action="/customers/{custid}/pairs/remove"',
            f'action="/customers/{custid}/pairs/import"',
        ):
            assert (marker in page.text) == allowed, (role, marker)
        for marker in ("New content type", f'action="/content-types/{ctid}/delete?'):
            assert (marker in page.text) == (role in (Role.ADMIN, Role.LINGUIST)), (role, marker)
        want = 303 if allowed else 403
        for path, data in (
            (f"/customers/{custid}/settings", {"default_provider": "gemini"}),
            (f"/customers/{custid}/context-cards", {"notes": "Changed"}),
            (f"/customers/{custid}/pairs", {"pair": "en-US->fr-FR"}),
            (f"/customers/{custid}/pairs/remove", {"pair": "en-US->fr-FR"}),
            (f"/context-cards/{card_id}/delete", {}),
        ):
            r = client.post(path, data=data, follow_redirects=False)
            if r.status_code != want:
                failures.append(f"{role} {path}: {r.status_code} != {want}")
        r = client.post(
            f"/customers/{custid}/pairs/import",
            follow_redirects=False,
            files={"file": ("pairs.csv", b"source_locale,target_locale\nen-US,fr-FR")},
        )
        if r.status_code != want:
            failures.append(f"{role} pairs/import: {r.status_code} != {want}")
    assert not failures, "customer-config web doors: " + "; ".join(failures)
    _auth_off()
    print("  web customer config: six-role write and visible-control matrix")


def test_connector_writes_blocked_for_viewer_translator_manager() -> None:
    """The api.py connector write endpoints (pull/push/sync/retry/jira/confluence)
    require can_tune_ai (admin/linguist), same as their web equivalents -- a
    viewer, translator, OR manager token is 403'd on every one of them (manager
    included: can_tune_ai is NOT the rank ladder, a manager is still not admin/
    linguist)."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    for role in (Role.VIEWER, Role.TRANSLATOR, Role.MANAGER):
        h = _h(d["keys"][role])
        for name, call in _connector_writes(api, d):
            r = call(h)
            assert _forbidden(r), (
                f"{role} should be 403 on {name}, got {r.status_code} ({r.text[:120]})"
            )
    print("  connector writes: viewer/translator/manager all 403 (can_tune_ai gate)")


def test_connector_writes_pass_role_gate_for_linguist_and_admin() -> None:
    """A linguist or admin token clears the ROLE gate on every connector write
    endpoint. The connection is a bare unconfigured 'airtable' stub, so the
    underlying connector call itself may still fail (e.g. no base/table
    configured) -- what matters here is that the failure is NOT a 403, i.e. the
    request reached the business logic instead of being turned away at the
    role wall."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    for role in (Role.LINGUIST, Role.ADMIN):
        h = _h(d["keys"][role])
        for name, call in _connector_writes(api, d):
            r = call(h)
            assert _not_forbidden(r), (
                f"{role} should clear the role gate on {name}, got 403 ({r.text[:120]})"
            )
    print(
        "  connector writes: linguist + admin clear the role gate (may still "
        "400 on connector-level errors, never 403)"
    )


def test_airtable_webhook_shared_token_refused() -> None:
    """Automation requires a per-user identity with AI authority."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    from app.web import deps as _deps

    cid = d["cid"]
    shared = _h(_deps.connector_api_token())
    r = api.post(
        f"/api/connectors/{cid}/airtable/webhook", headers=shared, json={"record_id": "rec1"}
    )
    assert _forbidden(r), r.text
    hv = _h(d["keys"][Role.VIEWER])
    r = api.post(f"/api/connectors/{cid}/airtable/webhook", headers=hv, json={"record_id": "rec1"})
    assert _forbidden(r), (
        f"a direct per-user VIEWER token SHOULD be 403'd on the webhook, got {r.status_code}"
    )
    print("  airtable webhook: shared connector and per-user viewer tokens refused")


def _paragraph_writes(api: TestClient, d):
    """The three FULL_TEXT paragraph WRITE routes (issue #50 closure evidence).
    Each closure hits ONE route; they bypass the mutating-RBAC middleware because
    /api is in _ALLOW_PREFIX, so the seam-level _write_role_block is their only
    can_write gate."""
    did = d["did"]
    return [
        (
            "POST /api/documents/{id}/paragraphs/0/save",
            lambda h: api.post(
                f"/api/documents/{did}/paragraphs/0/save?locale=fr-FR",
                headers=h,
                json={"text": "HACKED BY VIEWER", "confirm": True},
            ),
        ),
        (
            "POST /api/documents/{id}/paragraphs/0/revert",
            lambda h: api.post(f"/api/documents/{did}/paragraphs/0/revert?locale=fr-FR", headers=h),
        ),
        (
            "POST /api/documents/{id}/paragraphs/revert-all-damaged",
            lambda h: api.post(
                f"/api/documents/{did}/paragraphs/revert-all-damaged?locale=fr-FR", headers=h
            ),
        ),
    ]


def _seg0_target(did: int) -> str:
    with Session(engine) as s:
        seg = s.exec(
            select(Segment)
            .where(Segment.document_id == did, Segment.order_index == 0)
            .where(Segment.target_locale == "fr-FR")
        ).first()
        return "" if seg is None else (seg.target_text or "")


def test_viewer_blocked_on_paragraph_writes() -> None:
    """Issue #50 regression: a viewer's docs-scoped token is 403'd on all three
    FULL_TEXT paragraph write routes (save / revert / revert-all-damaged) and the
    target text is left UNCHANGED; a translator (can_write) is NOT 403'd (the role
    gate lets it through -- it may still fail deeper on object authz)."""
    d = _seed()
    _auth_on()
    api = TestClient(app_client())
    did = d["did"]
    before = _seg0_target(did)
    hv = _h(d["keys"][Role.VIEWER])
    for name, call in _paragraph_writes(api, d):
        r = call(hv)
        assert _forbidden(r), (
            f"viewer should be 403 on {name}, got {r.status_code} ({r.text[:120]})"
        )
        assert _seg0_target(did) == before, f"viewer write via {name} changed target text"
    ht = _h(d["keys"][Role.TRANSLATOR])
    for name, call in _paragraph_writes(api, d):
        r = call(ht)
        assert _not_forbidden(r), (
            f"translator should clear the role gate on {name}, got 403 ({r.text[:120]})"
        )
    # LOW-5: the loop above proves only that the ROLE gate lets a translator through.
    # On the SEGMENTED seed doc the three routes then 404 downstream (the FULL_TEXT
    # per-paragraph view refuses a non-full_text doc, and object authz also 404s a
    # translator here since project_scoping is on in this suite and a translator is
    # not see-all), so it never proves a write LANDS. Prove that too: give a
    # translator a FULL_TEXT document in a project it OWNS (created_by -> object
    # authz passes) and confirm the paragraph save returns 200 AND the committed
    # target text actually changes.
    tr = d["users"][Role.TRANSLATOR]
    with Session(engine) as s:
        proj = content.project_create(
            s,
            "RBAC FT Project",
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by=tr.email,
        )
        ftdoc = Document(
            project_id=proj.id,
            filename="ft.txt",
            fmt="txt",
            mode=TransMode.FULL_TEXT,
            status="parsed",
            skeleton={"units": [{"text": "hello"}, {"text": "world"}]},
        )
        s.add(ftdoc)
        s.commit()
        s.refresh(ftdoc)
        ftdid = ftdoc.id
        s.add(
            Segment(
                document_id=ftdid,
                source_text="hello\n\nworld",
                target_text="bonjour\n\nmonde",
                target_locale="fr-FR",
                order_index=0,
            )
        )
        s.commit()
    r = api.post(
        f"/api/documents/{ftdid}/paragraphs/0/save?locale=fr-FR",
        headers=ht,
        json={"text": "salut", "confirm": True},
    )
    assert r.status_code == 200 and r.json().get("ok") is True, (
        f"translator save should LAND (200/ok), got {r.status_code} ({r.text[:160]})"
    )
    with Session(engine) as s:
        landed = s.exec(
            select(Segment)
            .where(Segment.document_id == ftdid, Segment.target_locale == "fr-FR")
            .where(Segment.order_index == 0)
        ).first()
    got = None if landed is None else landed.target_text
    assert got == "salut\n\nmonde", f"translator write did not land, target_text={got!r}"
    print(
        "  paragraph writes (issue #50): viewer 403 on save/revert/revert-all-damaged "
        "with target text unchanged; translator clears the role gate AND a translator "
        "with project access lands a real FULL_TEXT paragraph save (200, target changed)"
    )


def test_gate_off_is_a_noop() -> None:
    """With the login wall OFF (single-tenant dev default), the role gate is a
    strict no-op: a viewer's token writes exactly as before (byte-identical)."""
    d = _seed()
    _auth_off()
    api = TestClient(app_client())
    hv = _h(d["keys"][Role.VIEWER])
    # A viewer creates a project via the API -- allowed, because auth is off. The maintainer
    # 2026-07-14: a project requires a customer when one exists (prior tests created
    # some) -- make one and pass it; the gate-off no-op is what this asserts.
    with Session(engine) as s:
        _go_cust = content.customer_create(s, "GateOffCust %d" % _SEED_N).id  # noqa: UP031
    r = api.post(
        "/api/projects",
        headers=hv,
        json={"name": "GateOff P", "targets": "fr-FR", "customer_id": _go_cust},
    )
    assert r.status_code == 200 and r.json().get("ok") is True, (
        f"gate-off viewer create should 200/ok, got {r.status_code} ({r.text[:120]})"
    )
    # And a language-set create (can_tune_ai) is likewise unrestricted.
    r = api.post(
        "/api/langsets",
        headers=hv,
        json={"name": "GateOff Set", "source": "en-US", "targets": "fr-FR"},
    )
    assert r.status_code == 200 and r.json().get("ok") is True, (
        f"gate-off viewer langset create should 200/ok, got {r.status_code}"
    )
    print("  gate-off: viewer writes unrestricted (byte-identical to today)")
    _auth_off()


# The FastAPI app is imported lazily so the env is fully set before import.
def app_client():
    from app.main import app

    return app


def test_security_review_role_doors():
    """Regression probes for #229, #231, #261, #268, #284 and #285."""
    import inspect
    from unittest.mock import patch

    from app.models import Connection, Customer
    from app.services import access, context_pkg, customers
    from app.web.deps import connector_api_token
    from tests._csrf import csrf_for

    d = _seed()
    _auth_on()
    settings.set_many({"project_scoping": "1"})
    settings.invalidate_cache()
    api = TestClient(app_client())
    hv = _h(d["keys"][Role.VIEWER])
    ha = _h(d["keys"][Role.ADMIN])
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)

    with Session(engine) as s:
        access.add_member(s, d["pid"], d["users"][Role.VIEWER].email)
        cust = Customer(name="Role regression customer")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
        hidden = content.project_create(
            s,
            "Hidden role regression project",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cid),
        )
        card = context_pkg.upsert_profile(s, customer_id=cid, context_raw={"notes": "Original"})
        card_id = card.id
        hidden_id = hidden.id
        s.add(Document(project_id=hidden_id, filename="hidden.txt", fmt="txt"))
        s.add(
            Connection(
                kind="airtable",
                name="Hidden role connection",
                customer_id=cid,
                project_id=hidden_id,
            )
        )
        s.commit()
    r = api.post(
        "/api/documents",
        headers=hv,
        data={"project_id": d["pid"]},
        files={"file": ("role.txt", b"Hello", "text/plain")},
    )
    check("229 upload", r.status_code == 403)
    r = api.post(
        f"/api/documents/{d['did']}/translate",
        headers=hv,
        json={"targets": ["fr-FR"], "provider": "mock"},
    )
    check("229 translate", r.status_code == 403)
    # Advanced Workflows left the core: its write routes are simply absent (404), never a door.
    for path in (
        "/api/advanced/missions",
        "/api/advanced/runs",
        "/api/advanced/runs/999999/cancel",
    ):
        r = api.post(path, headers=hv, json={})
        check("229 " + path, r.status_code == 404)
    for scope in ("docs", "scim"):
        body = {"scopes": scope}
        if scope == "docs":
            body["user"] = d["users"][Role.TRANSLATOR].email
        r = api.post("/api/apikeys/issue", headers=ha, json=body)
        check("231 " + scope, r.status_code in (400, 403))
    r = api.post("/api/apikeys/issue", headers=ha, json={"scopes": "docs"})
    check("231 self issuance", r.status_code == 200)
    r = api.post(
        "/api/apikeys/issue",
        headers=ha,
        json={"user": "missing-owner@example.invalid", "scopes": "docs"},
    )
    check("231 unknown owner", r.status_code == 403)
    for path in ("actions", "actions/missing/summary", "runs", "runs/missing"):
        r = api.get("/api/prompt-tap/" + path, headers=hv)
        check("261 " + path, r.status_code == 403)
    with patch("app.web.api.conn_store.airtable_delete_records") as delete:
        delete.return_value = (True, "Deleted", {})
        for role in (Role.VIEWER, Role.TRANSLATOR, Role.LINGUIST, Role.MANAGER):
            r = api.post(
                f"/api/connectors/{d['cid']}/airtable/records/delete",
                headers=_h(d["keys"][role]),
                json={"record_ids": ["rec1"]},
            )
            check("268 " + role, r.status_code == 403)
        check("268 no external deletion", delete.call_count == 0)
        r = api.post(
            f"/api/connectors/{d['cid']}/airtable/records/delete",
            headers=_h(connector_api_token()),
            json={"record_ids": ["rec1"]},
        )
        check("268 shared credential", r.status_code == 403 and delete.call_count == 0)
        r = api.post(
            f"/api/connectors/{d['cid']}/airtable/records/delete",
            headers=ha,
            json={"record_ids": ["rec1"]},
        )
        check("268 admin", r.status_code == 200 and delete.call_count == 1)
    for role in (Role.TRANSLATOR, Role.MANAGER, Role.LINGUIST):
        client = TestClient(app_client())
        csrf_for(client)
        client.post(
            "/auth/login",
            data={
                "email": d["users"][role].email,
                "password": f"{role}-password-123",  # pregate: allow-secret test fixture
            },
            follow_redirects=False,
        )
        csrf_for(client)
        # 2026-09-26: customer configuration is admin, manager or linguist
        # (can_configure_customer); a translator is still refused (#284).
        allowed = role in (Role.MANAGER, Role.LINGUIST)
        # A fresh card per role: an allowed role's delete below removes it.
        with Session(engine) as s:
            card_id = context_pkg.upsert_profile(
                s, customer_id=cid, context_raw={"notes": "Original"}
            ).id
        for path, data in (
            (f"/customers/{cid}/settings", {"default_provider": "gemini"}),
            (f"/customers/{cid}/context-cards", {"notes": "Changed"}),
            (f"/customers/{cid}/pairs", {"pair": "en-US->fr-FR"}),
            (f"/customers/{cid}/pairs/remove", {"pair": "en-US->fr-FR"}),
            (f"/context-cards/{card_id}/delete", {}),
        ):
            r = client.post(path, data=data, follow_redirects=False)
            check("284 web " + role + path, r.status_code == (303 if allowed else 403))
        r = client.post(
            f"/customers/{cid}/pairs/import",
            follow_redirects=False,
            files={"file": ("pairs.csv", b"source_locale,target_locale\nen-US,fr-FR")},
        )
        check("284 web import " + role, r.status_code == (303 if allowed else 403))
        for path, body in (
            ("defaults", {"provider": "gemini"}),
            ("settings", {"settings": {"notes": "Changed"}}),
            ("pairs/add", {"pair": "en-US->fr-FR"}),
            ("pairs/remove", {"pair": "en-US->fr-FR"}),
            ("pairs/import", {"csv_b64": _b64(b"source_locale,target_locale\nen-US,fr-FR")}),
        ):
            r = api.post(f"/api/customers/{cid}/{path}", headers=_h(d["keys"][role]), json=body)
            check("284 api " + role + path, r.status_code == (200 if allowed else 403))
        if role == Role.TRANSLATOR:
            page = client.get(f"/customers/{cid}")
            check("285 hidden project", "Hidden role regression project" not in page.text)
            check("285 hidden connection", "Hidden role connection" not in page.text)
            check("284 readonly", "Save preferences" not in page.text)
            with Session(engine) as s:
                kwargs = {}
                if "visible_pids" in inspect.signature(customers.get_customer_360).parameters:
                    kwargs["visible_pids"] = access.visible_project_ids(s, d["users"][role])
                view = customers.get_customer_360(s, cid, **kwargs)
                check("285 aggregates", not view["projects"] and not view["derived_pairs"])
                check("285 connections", not view["connections"])
                access.add_member(s, hidden_id, d["users"][role].email)
            check(
                "285 member",
                "Hidden role regression project" in client.get(f"/customers/{cid}").text,
            )
    assert not failures, "Security regressions: " + ", ".join(failures)
    _auth_off()


def _run():
    tests = [
        test_security_review_role_doors,
        test_viewer_blocked_on_every_write,
        test_viewer_can_still_read,
        test_translator_allowed_on_writes_blocked_on_manage_and_ai,
        test_linguist_allowed_on_langsets,
        test_admin_allowed_everywhere,
        test_manager_blocked_on_langsets_allowed_on_customer,
        test_context_profile_persistence_and_ai_boundary,
        test_context_profile_resolved_scope_roles,
        test_bound_context_card_web_scopes,
        test_customer_config_role_descriptions,
        test_reviewer_refused_on_customer_config,
        test_manager_configures_customer_on_web,
        test_connector_writes_blocked_for_viewer_translator_manager,
        test_connector_writes_pass_role_gate_for_linguist_and_admin,
        test_viewer_blocked_on_paragraph_writes,
        test_airtable_webhook_shared_token_refused,
        test_gate_off_is_a_noop,
    ]
    for t in tests:
        t()
    print("verify_api_rbac OK")


if __name__ == "__main__":
    _run()
