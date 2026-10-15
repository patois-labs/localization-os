"""T4: customer-bound linguist probes on a single-org install, offline only."""

import os
import re
import sys
import traceback
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sec_scoping_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    ProjectType,
    Role,
    Segment,
    Workflow,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, lqa, security, settings, workflows
from tests._csrf import csrf_for
from tests._piece_patch import patch_pieces

_PASSWORD = "scoping-fixture-only-123"  # pregate: allow-secret offline fixture
_LOGIN_FIELD = "password"  # pregate: allow-secret login field name
_API_HEADER = "X-Loc-Token"  # pregate: allow-secret header name only


def put(row):
    with Session(engine) as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def login(email, role, customer_id=None):
    user = security.create_user(email, email, role, _PASSWORD, customer_id=customer_id)
    client = TestClient(app)
    csrf_for(client)
    result = client.post(
        "/auth/login",
        data={"email": email, _LOGIN_FIELD: _PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert result.status_code == 303, result.text
    csrf_for(client)
    return client, user.id


def setup():
    global mine, theirs, web, admin, viewer, api_headers, wf_mine, wf_theirs, doc, key_project
    init_db()
    settings.set_many({"security_auth_required": "1", "keys_enabled": "1"})
    settings.invalidate_cache()
    assert settings.get("multi_org_enabled") == "0"
    mine = put(Customer(name="scoping A"))
    theirs = put(Customer(name="scoping B"))
    web, uid = login("scoping-linguist@example.test", Role.LINGUIST, mine)
    admin, _ = login("scoping-admin@example.test", Role.ADMIN)
    viewer, _ = login("scoping-viewer@example.test", Role.VIEWER, mine)
    _, credential = apikeys.create_key(uid, "scoping", scopes="api docs")
    api_headers = {_API_HEADER: credential}
    wf_mine = put(Workflow(name="mine", customer_id=mine))
    wf_theirs = put(Workflow(name="theirs", customer_id=theirs))
    put(WorkflowStep(workflow_id=wf_theirs, prompt_text="Translate {text}", provider="mock"))
    project = put(Project(name="mine", customer_id=mine, source_locale="en-US"))
    doc = put(Document(project_id=project, filename="scoping.txt", fmt="txt"))
    put(Segment(document_id=doc, target_locale="fr-FR", source_text="Hello"))
    key_project = put(Project(name="keys", customer_id=mine, project_type=ProjectType.KEY_BASED))


def post(path, data, client=None):
    return (client or web).post(path, data=data, follow_redirects=False)


def masked(content: bytes) -> bytes:
    # Mask ONLY the per-request script CSP nonce (the #259 script CSP, 5e04b2be),
    # anchored to its attribute the way verify_account_lockout.py does. A themed
    # error page renders twice per request with two nonces; every other byte of a
    # no-disclosure pair must stay identical.
    return re.sub(rb'(\snonce=")[^"]+', rb"\1NONCE", content)


def test_delete_foreign_step():
    sid = put(WorkflowStep(workflow_id=wf_theirs))
    result = post(f"/workflows/{wf_mine}/steps/{sid}/delete", {})
    with Session(engine) as s:
        assert s.get(WorkflowStep, sid) is not None, "foreign step was deleted"
    assert result.status_code == 404, result.text
    own = put(WorkflowStep(workflow_id=wf_mine))
    assert post(f"/workflows/{wf_mine}/steps/{own}/delete", {}).status_code == 303
    with Session(engine) as s:
        assert s.get(WorkflowStep, own) is None


def test_delete_foreign_active_step_no_disclosure():
    wid = put(Workflow(name="private active workflow", customer_id=theirs))
    sid = put(WorkflowStep(workflow_id=wid))
    put(WorkflowRun(workflow_id=wid, document_id=doc, status="paused"))
    result = post(f"/workflows/{wf_mine}/steps/{sid}/delete", {})
    missing = post(f"/workflows/{wf_mine}/steps/99999999/delete", {})
    assert result.status_code == missing.status_code == 404, result.text
    assert masked(result.content) == masked(missing.content)
    assert "in-flight" not in result.text
    with Session(engine) as s:
        assert s.get(WorkflowStep, sid) is not None


def test_delete_service_requires_parent():
    sid = put(WorkflowStep(workflow_id=wf_theirs))
    with Session(engine) as s:
        assert not workflows.delete_step(s, sid, workflow_id=wf_mine)
        assert s.get(WorkflowStep, sid) is not None
        assert workflows.delete_step(s, sid, workflow_id=wf_theirs)


def test_rulebook_traversal():
    bad = [
        "..",
        "/fr-FR",
        "C:/outside",
        r"x\..\..\outside",
        "x%2f..%2foutside",
        "x/../../../../app/capabilities/workflows",
    ]
    accepted = []
    for value in bad:
        for pair in ((value, "fr-FR"), ("en-US", value)):
            try:
                lqa.rulebook_path(*pair)
            except ValueError:
                pass
            else:
                accepted.append(pair)
    assert not accepted, f"unsafe locales accepted: {accepted}"


def test_rulebook_roundtrip():
    lqa.save_rulebook("en-US", "zh-Hant-TW", "FORBID example")
    path = lqa.rulebook_path("en-US", "zh-Hant-TW")
    assert path.resolve().is_relative_to((config.DATA_DIR / "rulebooks").resolve())
    assert lqa.load_rulebook("en-US", "zh-Hant-TW") == "FORBID example"


def test_rulebook_locale_whitelist():
    path = lqa.rulebook_path("en-US", "de-DE")
    assert not path.exists()
    result = post(f"/documents/{doc}/rulebook", {"locale": "de-DE", "text": "bad"}, admin)
    assert not path.exists(), "locale outside document was written"
    assert result.status_code == 400, result.text


def test_rulebook_role_gate():
    for client in (web, viewer):
        with patch("app.web.documents_tm_only_delivery_empty_g.lqa.save_rulebook") as save:
            result = post(f"/documents/{doc}/rulebook", {"locale": "fr-FR", "text": "bad"}, client)
            save.assert_not_called()
            assert result.status_code == 403, result.text
    result = post(f"/documents/{doc}/rulebook", {"locale": "fr-FR", "text": "good"}, admin)
    assert result.status_code == 303
    assert lqa.load_rulebook("en-US", "fr-FR") == "good"


def test_group_default_binding():
    gid = put(WorkflowGroup(name="mine", customer_id=mine))
    foreign_gid = put(WorkflowGroup(name="theirs", customer_id=theirs))
    wid = put(Workflow(name="foreign default", customer_id=theirs, group_id=foreign_gid))
    result = post(f"/workflow-groups/{gid}/default", {"workflow_id": wid})
    with Session(engine) as s:
        assert not s.get(Workflow, wid).is_default, "foreign default changed"
    assert result.status_code == 404, result.text
    other = put(WorkflowGroup(name="other own group", customer_id=mine))
    own = put(Workflow(name="other own workflow", customer_id=mine, group_id=other))
    assert post(f"/workflow-groups/{gid}/default", {"workflow_id": own}).status_code == 400
    with Session(engine) as s:
        assert not s.get(Workflow, own).is_default
    member = put(Workflow(name="own member", customer_id=mine, group_id=gid))
    assert post(f"/workflow-groups/{gid}/default", {"workflow_id": member}).status_code == 303
    with Session(engine) as s:
        assert s.get(Workflow, member).is_default


def test_document_run_workflow_binding():
    with patch_pieces("app.services.orchestrator", "start_run", return_value=999) as launch:
        result = post(f"/documents/{doc}/run", {"workflow_id": wf_theirs, "locale": "fr-FR"})
        launch.assert_not_called()
        assert result.status_code == 404, result.text


def test_key_run_workflow_binding():
    with patch("app.web.keys_hub.keys_svc.launch_translation") as launch:
        launch.side_effect = AssertionError("foreign workflow reached key translation")
        result = post(
            f"/projects/{key_project}/keys/translate",
            {"workflow_id": wf_theirs, "locale": "fr-FR"},
        )
        launch.assert_not_called()
        assert result.status_code == 404, result.text


def test_customer_workflows_keys_on_customer_not_scope():
    """GATE R1 HIGH-2. `scope` and `customer_id` are INDEPENDENT columns, so a row marked
    scope=GLOBAL is not "owned by nobody". Promotion used to mint exactly that shape, so
    every promoted workflow was visible to, and selectable by, every OTHER customer, with
    no forgery at all. Six predicates read scope; they now read customer_id."""
    from app.models import Scope
    from app.services import customers as customer_service

    theirs_global = put(
        Workflow(name="theirs-marked-global", scope=Scope.GLOBAL, customer_id=theirs)
    )
    with Session(engine) as s:
        offered = [w.id for w in customer_service.customer_workflows(s, mine)]
    assert theirs_global not in offered, (
        "a workflow owned by another customer but marked scope=GLOBAL was offered"
    )
    with Session(engine) as s:
        assert theirs_global in [w.id for w in customer_service.customer_workflows(s, theirs)]


def test_customer_workflows_org_scope_fails_closed():
    """GATE R1 HIGH-2, org half. The customer predicate admits every GLOBAL workflow
    (customer_id is None), and those rows belong to an ORG, so this helper narrows on
    org and is therefore a #73 converged read. It must obey the same law as
    vendors.list_vendors: a bare None under an ACTIVE gate reads NOTHING rather than
    every org, the poison BLOCKED_ORG_ID reads nothing, access.UNSCOPED (the CLI
    --operator door) still sweeps, and with the gate OFF nothing changes at all.

    The verify_multiorg #73 ratchet catches a MISSING guard at build time. This pins the
    behaviour that guard stands for, so the two cannot drift apart."""
    from app.models import Scope
    from app.services import access
    from app.services import customers as customer_service

    org = put(Organization(name="Scoping Foreign Org"))
    foreign = put(Workflow(name="foreign-org-global", scope=Scope.GLOBAL, org_id=org))
    with Session(engine) as s:
        # Gate OFF: passing the new argument is byte-identical to omitting it.
        assert [w.id for w in customer_service.customer_workflows(s, mine)] == [
            w.id for w in customer_service.customer_workflows(s, mine, org_id=None)
        ]
        assert foreign in [w.id for w in customer_service.customer_workflows(s, mine)], (
            "gate off must stay see-all, or single-tenant installs lose rows"
        )
        with patch.object(access, "multi_org_active", return_value=True):
            assert customer_service.customer_workflows(s, mine, org_id=None) == [], (
                "a bare None under an active gate must FAIL CLOSED, not list every org"
            )
            assert customer_service.customer_workflows(s, mine, org_id=access.BLOCKED_ORG_ID) == []
            swept = [
                w.id for w in customer_service.customer_workflows(s, mine, org_id=access.UNSCOPED)
            ]
            assert foreign in swept and wf_mine in swept, "the --operator door must still sweep"
            scoped = [w.id for w in customer_service.customer_workflows(s, mine, org_id=org)]
            assert scoped == [foreign], f"an org scope must see only its own org: {scoped}"


def test_customer_workflows_scope_still_narrows():
    """GATE R2 MED-2. The round-1 fix SUBSTITUTED the ownership test for the scope test
    instead of LAYERING it, so the picker began offering rows the pre-train predicate had
    excluded: a PROJECT-scoped workflow, and a CUSTOMER-scoped one with no owner. Neither
    crosses a tenant, but a widened picker is still a defect and nothing pinned it.

    Pin the whole truth table against the predicate main shipped. The ONLY cell allowed to
    differ is the one this train exists to change."""
    from app.models import Scope
    from app.services import customers as customer_service

    def before(scope, owner):
        # The predicate on main @ 7b2e86d5, verbatim.
        return scope == Scope.GLOBAL or (scope == Scope.CUSTOMER and owner == mine)

    class Row:
        def __init__(self, scope, owner):
            self.scope, self.customer_id = scope, owner

    labels = {None: "unowned", mine: "mine", theirs: "theirs"}
    changed = []
    for scope in (Scope.GLOBAL, Scope.CUSTOMER, Scope.PROJECT):
        for owner in (None, mine, theirs):
            now = customer_service.workflow_offered_to_customer(Row(scope, owner), mine)
            if now != before(scope, owner):
                changed.append((scope, labels[owner], before(scope, owner), now))
    assert changed == [(Scope.GLOBAL, "theirs", True, False)], (
        f"the only intended change is refusing another customer's GLOBAL-marked row: {changed}"
    )


def test_set_customer_defaults_refuses_foreign_global():
    """GATE R2 MED-3. The WRITE half of round-1 HIGH-2 was pinned by nothing: the existing
    isolation case only plants a scope=CUSTOMER row, which main already rejected. A
    validator that disagrees with the picker it validates against is the original defect,
    so both now read one predicate."""
    from app.models import Scope
    from app.services import customers as customer_service

    foreign = put(Workflow(name="foreign-default", scope=Scope.GLOBAL, customer_id=theirs))
    with Session(engine) as s:
        try:
            customer_service.set_customer_defaults(s, mine, workflow_id=foreign)
        except ValueError:
            pass
        else:
            raise AssertionError("another customer's GLOBAL-marked workflow became a default")
        assert s.get(Customer, mine).default_workflow_id != foreign
        customer_service.set_customer_defaults(s, mine, workflow_id=wf_mine)
        assert s.get(Customer, mine).default_workflow_id == wf_mine


def test_get_customer_360_forwards_org_scope():
    """GATE R2 MED-3. The org narrow is only non-inert because get_customer_360 forwards
    org_id. The org case beside this one calls customer_workflows directly, so deleting
    that one forwarding line would leave it green."""
    from app.services import access
    from app.services import customers as customer_service

    with Session(engine) as s, patch.object(access, "multi_org_active", return_value=True):
        assert customer_service.get_customer_360(s, mine)["workflows"] == [], (
            "an un-wired caller must fail closed under an active gate"
        )
        assert customer_service.get_customer_360(s, mine, org_id=access.UNSCOPED)["workflows"]


def test_foreign_global_workflow_never_listed():
    """GATE R2 MED-1. Four predicates still keyed the customer ring on `scope`, so a bound
    linguist saw another customer's promoted workflow (its name, scope, step count and
    shape, plus a cross-link anchor on the building-blocks page) on three surfaces, while
    `_wf_or_404` returned 404 for the very same id. Drive the surfaces, do not reason."""
    from app.models import PromptTemplate, Scope

    name = "foreign-promoted-never-appears"
    wid = put(Workflow(name=name, scope=Scope.GLOBAL, customer_id=theirs))
    # GATE R3 MED-1: the FOURTH conversion is the `vwq` visible-workflow map behind the
    # building-blocks CROSS-LINKS, and the first cut of this case never reached it. A
    # cross-link name only renders when a prompt template is bound to a step of the foreign
    # workflow, and the step planted here carried no prompt_template_id, so reverting that
    # one line left the whole suite green. Bind an unowned template to the foreign
    # workflow's step, which is what makes its name a candidate for the anchor.
    shared = put(PromptTemplate(name="shared-template-for-crosslink", body="Translate"))
    put(
        WorkflowStep(
            workflow_id=wid,
            prompt_text="Translate {text}",
            provider="mock",
            prompt_template_id=shared,
        )
    )
    for path in ("/workflows", "/workflows/building-blocks"):
        page = web.get(path)
        assert page.status_code == 200, (path, page.status_code)
        assert name not in page.text, f"{path} leaked another customer's workflow"
    listed = web.get("/api/workflows", headers=api_headers)
    assert listed.status_code == 200, listed.text
    names = [w["name"] for w in listed.json()["workflows"]]
    assert name not in names, f"/api/workflows leaked it: {names}"
    assert web.get(f"/workflows/{wid}").status_code == 404, "the canonical ring must still 404"
    assert wf_mine in [w["id"] for w in listed.json()["workflows"]], "own workflows must remain"


def test_rulebook_edit_gate_matches_the_post():
    """GATE R2 MED-3. `can_edit_rulebook` was referenced by no test at all, so the round-1
    MED-1 fix (offer the Save form only to whoever the POST actually admits) could be
    reverted silently.

    MANAGER is the ONLY role that separates the two predicates: `can_manage` is True and
    `is_admin` is False. The first cut of this case used the LINGUIST, who is neither, and
    so stayed green against a reverted fix. A test that cannot fail is not coverage."""
    manager, _ = login("scoping-manager@example.test", Role.MANAGER)
    page = manager.get(f"/documents/{doc}/lqa")
    assert page.status_code == 200, page.status_code
    assert page.context["can_edit_rulebook"] is False, "a manager saw a Save button that 403s"
    assert admin.get(f"/documents/{doc}/lqa").context["can_edit_rulebook"] is True
    refused = post(f"/documents/{doc}/rulebook", {"locale": "fr-FR", "text": "x"}, manager)
    assert refused.status_code == 403, refused.status_code


def test_rulebook_read_never_raises():
    """GATE R1 MED-2. rulebook_path now refuses an invalid locale, and 5 of 6 call sites
    did not catch it: both /api routes 500ed, the CLI tracebacked, and the two orchestrator
    sites could abort an LQA step mid-run. A READ has nothing to tell the operator."""
    assert lqa.load_rulebook("en-US", "../../../etc/passwd") == ""
    assert lqa.load_rulebook("../..", "fr-FR") == ""
    try:
        lqa.save_rulebook("en-US", "../../../etc/passwd", "x")
    except ValueError:
        pass
    else:
        raise AssertionError("a WRITE must still refuse, so the operator is told")


if __name__ == "__main__":
    setup()
    failures = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}", flush=True)
            except Exception:
                failures.append(name)
                print(f"FAIL {name}", flush=True)
                traceback.print_exc()
    assert not failures, f"failed cases: {failures}"
    print("SEC SCOPING OK")
