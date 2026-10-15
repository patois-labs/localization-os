"""Universal rename + delete — Branch 2 (projects surface) verification.

SPEC-rename-delete, Branch 2. Offline + hermetic (throwaway LOC_TMS_DATA_DIR,
provider keys blanked -> MockProvider; no network is ever touched). Covers:

  inbox re-key (the prereq)
    - a renamed department inbox is STILL found by routing.ensure_department_inbox
      (keyed on Project.inbox_key, not the name) instead of forking a duplicate;
    - FAIL-PRE-FIX PROOF: the pre-fix lookup was `Project.name == name`
      (git show HEAD:app/services/routing.py) — reproduced inline here, it does NOT
      find the renamed inbox, i.e. the old code WOULD fork a second inbox.
  project rename (service + web + /api + CLI) + audit old->new
  project delete
    - typed-confirmation enforcement (wrong name refused on web + /api);
    - blocker fail-closed (Connection.project_id AND RoutingRule.project_id);
    - cascade completeness (documents+blobs+segments+TM+embeddings+jobs+runs+
      locales+pair-providers+members+glossary gone; a sibling project UNTOUCHED);
    - the remaining project-FK families (steps 8e/8g): a project carrying a
      ProjectBudget and a MemorySuggestion deletes cleanly, un-scopes (never
      deletes) the suggestion, and never reaches a sibling project's rows;
    - RBAC: non-admin blocked on web AND /api; the /api dry-run preview.
  customer rename + fail-closed delete (itemized counts)
  document delete (reuses retention.delete_document, TM untouched) + rename
  glossary rename parity (CLI + /api; web pre-existing)
  language set delete fail-closed on BOTH ref kinds (RoutingRule + Connection config)
  foreign-customer caller gets 404 on the new {id} routes.

Final line: "RENAME DELETE PROJECTS OK".
"""

import base64
import io
import os
import re
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rendel_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AdvancedMission,
    AdvancedRun,
    AdvancedStagedTarget,
    AdvancedTranscriptEntry,
    Connection,
    Customer,
    Document,
    FullTextTM,
    Glossary,
    GlossaryEntry,
    Job,
    LanguageSet,
    LQAResult,
    MemorySuggestion,
    Organization,
    Project,
    ProjectBudget,
    ProjectMember,
    ProjectPairProvider,
    ProjectTemplate,
    ProjectType,
    QualityFinding,
    Role,
    RoutingRule,
    Segment,
    SegmentEmbedding,
    SegmentTM,
    StepRun,
    TargetLocale,
    User,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    apikeys,
    content,
    glossary_module,
    languages,
    routing,
    security,
    settings,
)
from app.services import (
    customers as customer_service,
)
from app.services import (
    keys as keys_service,
)
from tests._csrf import csrf_for

init_db()  # self-initializing (module-level), robust under pytest ordering


# --- helpers -----------------------------------------------------------------
def _auth_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _project(name, customer_id=None, targets="fr-FR, de-DE"):
    with Session(engine) as s:
        p = content.project_create(
            s, name, source_locale="en-US", target_locales=targets, customer_id=customer_id
        )
        return p.id


def _customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _user(email, *, role=Role.ADMIN, customer_id=None):
    with Session(engine) as s:
        u = User(email=email, name=email, role=role, active=True, customer_id=customer_id)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _docs_key(token):
    return {"X-Loc-Token": token}


def _cli_json(argv):
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", *argv])
    return rc, buf.getvalue()


def _last_audit(action):
    from app.models import AuditLog

    with Session(engine) as s:
        return s.exec(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())
        ).first()


# --- inbox re-key ------------------------------------------------------------
def test_inbox_rekey_survives_rename_and_fail_pre_fix():
    _auth_off()
    with Session(engine) as s:
        inbox = routing.ensure_department_inbox(s, "Sales", "actor@x.test")
        inbox_id, old_name = inbox.id, inbox.name
        assert inbox.inbox_key == "dept:sales", inbox.inbox_key

    # rename the inbox project (display name only)
    with Session(engine) as s:
        content.rename_project(s, inbox_id, "Renamed Sales Bucket")

    # POST-FIX: the re-keyed lookup still finds the SAME inbox (no fork).
    with Session(engine) as s:
        again = routing.ensure_department_inbox(s, "sales", "other@x.test")
        assert again.id == inbox_id, "re-keyed inbox lookup forked a duplicate!"
        # exactly ONE project carries this inbox key
        n = len(s.exec(select(Project).where(Project.inbox_key == "dept:sales")).all())
        assert n == 1, "expected 1 inbox for dept:sales, found %d" % n  # noqa: UP031

    # FAIL-PRE-FIX PROOF: the pre-fix code (git show HEAD:app/services/routing.py)
    # looked the inbox up by `Project.name == _inbox_name(...)`. Reproduced inline,
    # that query does NOT find the renamed inbox -> the old code would create a fresh
    # one (the duplicate fork this branch fixes).
    with Session(engine) as s:
        prefix_hit = s.exec(select(Project).where(Project.name == old_name)).first()
        assert prefix_hit is None, "pre-fix name lookup unexpectedly still matched — proof invalid"
    print(
        "  inbox re-key: renamed inbox still resolved by key; pre-fix name lookup "
        "would have forked OK"
    )


# --- project rename (all surfaces + audit) -----------------------------------
def test_project_rename_all_surfaces_and_audit():
    _auth_off()
    # service
    pid = _project("svc-rename")
    with Session(engine) as s:
        content.rename_project(s, pid, "svc-renamed", actor="tester@x.test")
        assert s.get(Project, pid).name == "svc-renamed"
    a = _last_audit("project.rename")
    assert a and "svc-rename" in a.detail and "svc-renamed" in a.detail, a.detail

    # web (auth off -> system operator, no CSRF needed)
    c = TestClient(app)
    pid_w = _project("web-rename")
    r = c.post(f"/projects/{pid_w}/rename", data={"name": "web-renamed"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:150]
    with Session(engine) as s:
        assert s.get(Project, pid_w).name == "web-renamed"

    # /api
    uid = _user("api-rename-admin@x.test", role=Role.ADMIN)
    _row, token = apikeys.create_key(uid, "k", scopes="docs")
    pid_a = _project("api-rename")
    r = c.post(
        f"/api/projects/{pid_a}/rename", json={"name": "api-renamed"}, headers=_docs_key(token)
    )
    assert r.status_code == 200 and r.json()["name"] == "api-renamed", r.text[:150]

    # CLI
    pid_c = _project("cli-rename")
    rc, _out = _cli_json(["project", "rename", str(pid_c), "cli-renamed"])
    assert rc == 0
    with Session(engine) as s:
        assert s.get(Project, pid_c).name == "cli-renamed"

    # blank name refused (service)
    with Session(engine) as s:
        try:
            content.rename_project(s, pid, "   ")
            assert False, "blank rename should raise"
        except ValueError:
            pass
    print("  project rename: service/web/api/cli all rename + audit old->new OK")


# --- project delete: typed confirm + blockers + cascade ----------------------
def _seed_full_project(name):
    """A project wired with one of EVERY owned child kind, for the cascade assert."""
    pid = _project(name, targets="fr-FR")
    with Session(engine) as s:
        # per-pair provider
        s.add(
            ProjectPairProvider(
                project_id=pid, source_locale="en-US", target_locale="fr-FR", provider="claude"
            )
        )
        # a document + segment
        doc = Document(project_id=pid, filename="d.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        seg = Segment(document_id=doc.id, target_locale="fr-FR", source_text="hi")
        s.add(seg)
        s.commit()
        s.refresh(seg)
        # TM + full-text TM + embedding (embedding references the segmenttm row)
        stm = SegmentTM(
            project_id=pid,
            source_locale="en-US",
            target_locale="fr-FR",
            source_text="hi",
            target_text="salut",
            source_hash="h1",
        )
        s.add(stm)
        s.commit()
        s.refresh(stm)
        s.add(
            SegmentEmbedding(
                segment_tm_id=stm.id,
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                model="m",
                dim=0,
                embedding=b"",
            )
        )
        s.add(
            FullTextTM(
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_hash="h2",
                source_text="a",
                target_text="b",
            )
        )
        # legacy glossary entry
        s.add(
            GlossaryEntry(
                project_id=pid, source_locale="en-US", target_locale="fr-FR", source_term="hi"
            )
        )
        # member (needs a user)
        u = User(email=f"m-{name}@x.test", name="m", role=Role.TRANSLATOR)
        s.add(u)
        s.commit()
        s.refresh(u)
        s.add(ProjectMember(project_id=pid, user_id=u.id))
        # job referencing the project
        s.add(Job(kind="translate_document", payload={"project_id": pid}))
        # workflow run + step run on the document
        wf = Workflow(name=f"wf-{name}")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        step = WorkflowStep(workflow_id=wf.id, order_index=0)
        s.add(step)
        s.commit()
        s.refresh(step)
        run = WorkflowRun(workflow_id=wf.id, document_id=doc.id)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(StepRun(workflow_run_id=run.id, step_id=step.id))
        # LQA result on the document + segment
        s.add(LQAResult(document_id=doc.id, segment_id=seg.id, dimension="x"))
        s.commit()
    return pid


def _owned_counts(pid):
    with Session(engine) as s:
        doc_ids = list(s.exec(select(Document.id).where(Document.project_id == pid)).all())
        seg = (
            len(s.exec(select(Segment.id).where(Segment.document_id.in_(doc_ids))).all())
            if doc_ids
            else 0
        )
        run = (
            len(s.exec(select(WorkflowRun.id).where(WorkflowRun.document_id.in_(doc_ids))).all())
            if doc_ids
            else 0
        )
        return {
            "projects": 1 if s.get(Project, pid) else 0,
            "documents": len(doc_ids),
            "segments": seg,
            "locales": len(
                s.exec(select(TargetLocale.id).where(TargetLocale.project_id == pid)).all()
            ),
            "pair_providers": len(
                s.exec(
                    select(ProjectPairProvider.id).where(ProjectPairProvider.project_id == pid)
                ).all()
            ),
            "tm": len(s.exec(select(SegmentTM.id).where(SegmentTM.project_id == pid)).all()),
            "ft": len(s.exec(select(FullTextTM.id).where(FullTextTM.project_id == pid)).all()),
            "emb": len(
                s.exec(select(SegmentEmbedding.id).where(SegmentEmbedding.project_id == pid)).all()
            ),
            "members": len(
                s.exec(select(ProjectMember.id).where(ProjectMember.project_id == pid)).all()
            ),
            "glossary": len(
                s.exec(select(GlossaryEntry.id).where(GlossaryEntry.project_id == pid)).all()
            ),
            "jobs": len(content._project_job_ids(s, pid, doc_ids)),
            "runs": run,
        }


def test_project_delete_cascade_completeness():
    _auth_off()
    pid = _seed_full_project("victim")
    sibling = _seed_full_project("sibling")
    before = _owned_counts(pid)
    assert all(v > 0 for v in before.values()), f"seed incomplete: {before!r}"
    sib_before = _owned_counts(sibling)

    with Session(engine) as s:
        counts = content.delete_project(s, pid, actor="admin@x.test")
    assert counts["documents"] == 1 and counts["total"] > 0

    after = _owned_counts(pid)
    assert all(v == 0 for v in after.values()), f"cascade left rows: {after!r}"
    # sibling project entirely untouched
    assert _owned_counts(sibling) == sib_before, "cascade touched an unrelated project!"
    a = _last_audit("project.delete")
    assert a and "victim" in a.target
    print("  project delete cascade: every owned child removed; sibling untouched OK")


# --- project delete: the remaining project-FK families (steps 8e/8f/8g) ------
#
# REGRESSION (gate finding M5, SPEC-quality-lenses T3 round 1): `advancedrun.project_id` and
# `advancedmission.project_id` are hard FKs to project.id, and the cascade never cleared them, so
# `DELETE FROM project` raised sqlite3.IntegrityError on ANY project that had ever run one
# advanced mission -- AFTER steps 4-8 had already COMMITTED the documents, the keys and the whole
# project TM. The project was left as an unrecoverable husk and every retry failed the same way.
# Reproduced with AND without quality findings, which is why BOTH arms are run below.
#
# The round-1 gate on the fix then found TWO MORE tables of the identical class still unhandled
# in the same function (M1 `ProjectBudget.project_id`, M2 `MemorySuggestion.project_id`), each
# independently fatal in exactly the same way. They are seeded here too, on the victim AND the
# sibling, so this one test covers every project-FK family the cascade now knows about.
def _seed_advanced_project(name):
    """A KEY_BASED project with its own org/customer + one key, and one row of
    EACH remaining project-FK family the gate found still husking the delete: a ProjectBudget
    (M1, hard-deleted by step 8g) and a MemorySuggestion (M2, un-scoped by step 8e). Returns a
    dict so a caller can assert on the two ids afterwards."""
    with Session(engine) as s:
        org = Organization(name=f"advorg-{name}")
        s.add(org)
        s.commit()
        s.refresh(org)
        cust = Customer(name=f"advcust-{name}", org_id=org.id)
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = content.project_create(
            s,
            name,
            source_locale="en",
            target_locales="es",
            project_type=ProjectType.KEY_BASED,
            customer_id=cust.id,
            org_id=org.id,
        )
        keys_service.create_key(s, proj, "greeting", base_value={"other": "Book"}, actor="seed")
        s.commit()
        # M1: the per-project monthly USD cap (NOT NULL FK to project.id, UNIQUE per project).
        budget = ProjectBudget(project_id=proj.id, monthly_usd_limit=25.0, org_id=proj.org_id)
        s.add(budget)
        # M2: an LLMemory suggestion scoped to this project. Its workflow FK is NOT NULL, and the
        # workflow is left GLOBAL (project_id unset) on purpose -- step 3c deletes PROJECT-scoped
        # workflows, and this row must outlive the project to prove the un-scope, not a cascade.
        wf = Workflow(name=f"advwf-{name}")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        sugg = MemorySuggestion(
            customer_id=cust.id,
            org_id=proj.org_id,
            project_id=proj.id,
            workflow_id=wf.id,
            suggestion_key=f"k-{name}",
            rationale="seeded",
        )
        s.add(sugg)
        s.commit()
        s.refresh(budget)
        s.refresh(sugg)
        return {
            "org": proj.org_id,
            "cust": proj.customer_id,
            "pid": proj.id,
            "budget": budget.id,
            "suggestion": sugg.id,
        }


def test_project_delete_budget_and_suggestion_fk_clean():
    # The two project-FK families the CORE cascade owns (gate M1/M2): the per-project spend cap is
    # hard-deleted (step 8g) and an LLMemory suggestion is un-scoped (step 8e). The Advanced
    # missions/runs family (step 8f) is reaped by the paid add-on's registered reaper, so it is
    # not exercised in the open-core tree.
    _auth_off()
    v = _seed_advanced_project("adv-victim")
    org, cust, pid = v["org"], v["cust"], v["pid"]
    sib = _seed_advanced_project("adv-sibling")

    # the PREVIEW must already promise what the delete removes (same keys, same scope).
    with Session(engine) as s:
        prev = content.delete_project_preview(s, pid)["cascade"]
    assert prev["project_budgets"] == 1, prev
    assert prev["memory_suggestions"] == 1, prev
    assert prev["advanced_runs"] == 0 and prev["advanced_missions"] == 0, prev

    # THE REGRESSION: this raised sqlite3.IntegrityError (FOREIGN KEY constraint failed) at
    # step 9 on the budget and on the suggestion.
    with Session(engine) as s:
        counts = content.delete_project(s, pid, actor="admin@x.test", org_id=org)

    with Session(engine) as s:
        assert s.get(Project, pid) is None, "the project row survived the delete"
        # M1: the budget row is GONE (pure per-project config, nothing references it, and a
        # survivor would re-attach to whichever project reuses this rowid).
        assert s.get(ProjectBudget, v["budget"]) is None, "the project budget survived"
        # M2: the suggestion row SURVIVES, un-scoped. It is a bi-temporal tombstone the
        # CUSTOMER owns (customer_id NOT NULL); deleting it would destroy an audit record.
        kept = s.get(MemorySuggestion, v["suggestion"])
        assert kept is not None, "the memory suggestion must be un-scoped, never deleted"
        assert kept.project_id is None, f"suggestion still scoped: {kept.project_id!r}"
        assert kept.customer_id == cust, "un-scoping must not touch the customer partition"
    assert counts["project_budgets"] == 1 and counts["memory_suggestions"] == 1, counts
    assert counts["total"] == sum(n for k, n in counts.items() if k != "total"), counts
    # the SIBLING is untouched (budget, suggestion scope).
    with Session(engine) as s:
        assert s.get(Project, sib["pid"]) is not None
        assert s.get(ProjectBudget, sib["budget"]) is not None, "sibling budget destroyed"
        sib_sugg = s.get(MemorySuggestion, sib["suggestion"])
        assert sib_sugg.project_id == sib["pid"], "sibling suggestion was un-scoped"
    print("  project delete: budget deleted + suggestion un-scoped, sibling untouched OK")


def test_project_delete_reaps_leftover_advanced_rows_without_addon():
    """A database upgraded from the full edition still holds Advanced missions, runs, staged
    targets, transcript rows and findings, all with hard FKs to the project. With the add-on
    ABSENT no registered reaper runs, so content_projects reaps them in plain SQL."""
    from app import editions

    if editions.addon_installed("advanced_workflows"):
        print("  advanced add-on installed: leftover-row reap not applicable, skipped")
        return
    _auth_off()
    v = _seed_advanced_project("adv-left")
    sib = _seed_advanced_project("adv-left-sib")
    ids_by_pid = {}
    for seed in (v, sib):
        with Session(engine) as s:
            m = AdvancedMission(project_id=seed["pid"], name="left")
            s.add(m)
            s.commit()
            s.refresh(m)
            r = AdvancedRun(mission_id=m.id, project_id=seed["pid"])
            s.add(r)
            s.commit()
            s.refresh(r)
            st = AdvancedStagedTarget(run_id=r.id, unit_key="k")
            te = AdvancedTranscriptEntry(run_id=r.id, kind="note")
            s.add(st)
            s.add(te)
            s.commit()
            s.refresh(st)
            s.refresh(te)
            f_run = QualityFinding(run_id=r.id, unit_key="k")
            f_stg = QualityFinding(staged_target_id=st.id, unit_key="k")
            s.add(f_run)
            s.add(f_stg)
            s.commit()
            ids_by_pid[seed["pid"]] = (m.id, r.id, st.id, te.id, f_run.id, f_stg.id)
    pid, org = v["pid"], v["org"]
    with Session(engine) as s:
        prev = content.delete_project_preview(s, pid)["cascade"]
    assert prev["advanced_runs"] == 1 and prev["advanced_missions"] == 1, prev
    with Session(engine) as s:
        counts = content.delete_project(s, pid, actor="admin@x.test", org_id=org)
    m_id, r_id, st_id, te_id, f1, f2 = ids_by_pid[pid]
    with Session(engine) as s:
        assert s.get(Project, pid) is None, "the project survived the delete"
        assert s.get(AdvancedMission, m_id) is None
        assert s.get(AdvancedRun, r_id) is None
        assert s.get(AdvancedStagedTarget, st_id) is None
        assert s.get(AdvancedTranscriptEntry, te_id) is None
        assert s.get(QualityFinding, f1) is None and s.get(QualityFinding, f2) is None
        sm, sr, _sst, _ste, sf1, sf2 = ids_by_pid[sib["pid"]]
        assert s.get(AdvancedMission, sm) is not None and s.get(AdvancedRun, sr) is not None
        assert s.get(QualityFinding, sf1) is not None and s.get(QualityFinding, sf2) is not None
    assert counts["advanced_runs"] == 1 and counts["advanced_missions"] == 1, counts
    assert counts["advanced_staged_targets"] == 1, counts
    assert counts["advanced_transcript_entries"] == 1, counts
    assert counts["advanced_quality_findings"] == 2, counts
    print("  project delete: leftover Advanced rows reaped without the add-on, sibling kept OK")


def test_project_delete_blocker_fail_closed():
    _auth_off()
    pid = _project("blocked")
    with Session(engine) as s:
        s.add(Connection(kind="jira", name="c", project_id=pid))
        s.add(RoutingRule(project_id=pid))
        s.commit()
    with Session(engine) as s:
        try:
            content.delete_project(s, pid, actor="a@x.test")
            assert False, "delete should fail closed on connection/routing rule"
        except content.ProjectDeleteBlocked as exc:
            assert exc.blockers["connections"] == 1 and exc.blockers["routing_rules"] == 1
    with Session(engine) as s:
        assert s.get(Project, pid) is not None, "blocked delete must not remove the project"
    # remove the blockers -> now deletable
    with Session(engine) as s:
        for c in s.exec(select(Connection).where(Connection.project_id == pid)).all():
            s.delete(c)
        for r in s.exec(select(RoutingRule).where(RoutingRule.project_id == pid)).all():
            s.delete(r)
        s.commit()
    with Session(engine) as s:
        assert content.delete_project(s, pid, actor="a@x.test") is not None
        assert s.get(Project, pid) is None
    print("  project delete blockers: fail-closed on connection+routing rule OK")


def test_project_delete_workflow_suggestion_forecast_and_cli_guard():
    # LOW-2 + LOW-3. A LAW-2 MemorySuggestion targeting a PROJECT-SCOPED workflow is a permanent
    # audit tombstone (NOT NULL FK, never deleted), so delete_project fails closed at step 3c.
    #   LOW-2: delete_project_preview must FORECAST it (report the project as NOT deletable) so the
    #          /api dry-run + CLI preview do not say deletable and then 400. It is a SEPARATE
    #          blocker key, summed into total; the referrer short-circuits (connections/routing)
    #          are unaffected, so the tailored "archive instead" message still wins.
    #   LOW-3: the in-process CLI `project delete --force` must catch ProjectDeleteBlocked and exit
    #          EXIT_USAGE with that message, never a raw traceback.
    # (Latent today: no surface sets Workflow.project_id -- defensive, set directly here.)
    from app.cli.__main__ import main as cli_main

    _auth_off()
    cid = _customer("wf-sugg-cust")
    pid = _project("wf-sugg-proj", customer_id=cid)
    with Session(engine) as s:
        w = Workflow(name="proj-scoped-wf", project_id=pid, customer_id=cid)
        s.add(w)
        s.commit()
        s.refresh(w)
        wid = w.id
        s.add(MemorySuggestion(kind="tune_prompt", customer_id=cid, workflow_id=wid))
        s.commit()

    # LOW-2: the forecast is present, non-vacuous, and lifts total (so the preview is not
    # "deletable"). The referrer counts are still zero -- this project is blocked ONLY by the
    # tombstone, which is exactly the asymmetry LOW-2 closes.
    with Session(engine) as s:
        blk = content.delete_project_preview(s, pid)["blockers"]
    assert blk["workflow_memory_suggestions"] == 1, blk
    assert blk["connections"] == 0 and blk["routing_rules"] == 0, blk
    assert blk["total"] >= 1, blk

    # LOW-3: the CLI delete flows PAST the referrer preflight (0/0) into delete_project, whose
    # step-3c raise is caught and surfaced as a clean EXIT_USAGE + the tailored message.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", "project", "delete", str(pid), "--force"])
    import json as _json

    payload = _json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rc == 2, (rc, payload)  # EXIT_USAGE, not a traceback / not EXIT_OK
    assert payload["ok"] is False, payload
    err = payload["error"].lower()
    assert "archive" in err and "suggestion" in err, payload

    # nothing was deleted -- the delete rolled back whole (step-3c raises before any commit).
    with Session(engine) as s:
        assert s.get(Project, pid) is not None, "blocked delete removed the project"
        assert s.get(Workflow, wid) is not None, "blocked delete removed the workflow"
        assert (
            s.exec(select(MemorySuggestion).where(MemorySuggestion.workflow_id == wid)).first()
            is not None
        ), "blocked delete removed the tombstone"
    print("  project delete: forecasts the workflow-suggestion blocker + CLI exits clean OK")


def test_project_delete_typed_confirm_web_and_api():
    _auth_off()
    c = TestClient(app)
    # web: wrong typed name refused (redirects back to the confirm page with err)
    pid = _project("confirm-web")
    r = c.post(f"/projects/{pid}/delete", data={"confirm_name": "WRONG"}, follow_redirects=False)
    assert r.status_code == 303 and "/delete?err=" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(Project, pid) is not None, "wrong-name confirm must not delete"
    # correct name deletes
    r = c.post(
        f"/projects/{pid}/delete", data={"confirm_name": "confirm-web"}, follow_redirects=False
    )
    assert r.status_code == 303 and "deleted=1" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(Project, pid) is None

    # /api: admin key, dry-run preview + wrong confirm refused + correct confirm deletes
    uid = _user("del-admin@x.test", role=Role.ADMIN)
    _row, token = apikeys.create_key(uid, "k", scopes="docs")
    pid = _project("confirm-api")
    r = c.post(f"/api/projects/{pid}/delete", json={"dry_run": True}, headers=_docs_key(token))
    assert r.status_code == 200 and r.json()["dry_run"] and "cascade" in r.json()
    r = c.post(f"/api/projects/{pid}/delete", json={"confirm": "nope"}, headers=_docs_key(token))
    assert r.status_code == 400 and "confirm" in r.json()["error"]
    with Session(engine) as s:
        assert s.get(Project, pid) is not None
    r = c.post(
        f"/api/projects/{pid}/delete", json={"confirm": "confirm-api"}, headers=_docs_key(token)
    )
    assert r.status_code == 200 and r.json()["deleted"]
    with Session(engine) as s:
        assert s.get(Project, pid) is None
    print(
        "  project delete typed-confirm: wrong name refused, exact name deletes "
        "(web + /api dry-run) OK"
    )


def test_project_delete_rbac_non_admin_blocked():
    _auth_on()
    try:
        c = TestClient(app)
        pid = _project("rbac-target")
        # /api: a translator (can_write, NOT admin) is 403 on delete
        uid = _user("t@x.test", role=Role.TRANSLATOR)
        _row, token = apikeys.create_key(uid, "k", scopes="docs")
        r = c.post(
            f"/api/projects/{pid}/delete", json={"confirm": "rbac-target"}, headers=_docs_key(token)
        )
        # #219: a non-member gets the missing-id 404 (no existence oracle) ...
        assert r.status_code == 404, f"non-member /api delete must be 404 ({r.status_code})"
        from app.services import access

        with Session(engine) as s:
            assert access.add_member(s, pid, "t@x.test")[0]
        # ... and a member without the admin role is refused by the role check.
        r = c.post(
            f"/api/projects/{pid}/delete", json={"confirm": "rbac-target"}, headers=_docs_key(token)
        )
        assert r.status_code == 403, f"non-admin /api delete must be 403 ({r.status_code})"
        # web: a logged-in translator is 403 on the confirm page + POST
        from tests._csrf import csrf_for

        security.create_user("wt@x.test", "wt", Role.TRANSLATOR, "pw-123456789")
        wc = TestClient(app)
        csrf_for(wc)
        wc.post(
            "/auth/login",
            data={"email": "wt@x.test", "password": "pw-123456789", "next": "/"},
            follow_redirects=False,
        )
        csrf_for(wc)
        r = wc.get(f"/projects/{pid}/delete", follow_redirects=False)
        assert r.status_code == 403, f"non-admin web delete page must be 403 ({r.status_code})"
        r = wc.post(
            f"/projects/{pid}/delete", data={"confirm_name": "rbac-target"}, follow_redirects=False
        )
        assert r.status_code == 403
        with Session(engine) as s:
            assert s.get(Project, pid) is not None, "non-admin must not have deleted"
    finally:
        _auth_off()


# --- customer rename + fail-closed delete ------------------------------------
def test_customer_rename_and_delete_fail_closed():
    _auth_off()
    cid = _customer("Acme")
    with Session(engine) as s:
        customer_service.rename_customer(s, cid, "Acme Corp", actor="a@x.test")
        assert s.get(Customer, cid).name == "Acme Corp"
    a = _last_audit("customer.rename")
    assert a and "Acme" in a.detail and "Acme Corp" in a.detail

    # fail closed while a project references it
    pid = _project("cust-proj", customer_id=cid)
    with Session(engine) as s:
        try:
            customer_service.delete_customer(s, cid, actor="a@x.test")
            assert False, "customer delete should fail closed"
        except customer_service.CustomerDeleteBlocked as exc:
            assert exc.refs["projects"] == 1 and exc.refs["total"] >= 1
    # remove the project -> deletable (empty customer)
    with Session(engine) as s:
        content.delete_project(s, pid, actor="a@x.test")
    empty = _customer("Empty Co")
    with Session(engine) as s:
        assert customer_service.delete_customer(s, empty, actor="a@x.test") is not None
        assert s.get(Customer, empty) is None
    print(
        "  customer: rename + audit; delete fails closed while referenced, "
        "empty customer deletes OK"
    )


# --- document delete (reuse retention) + rename ------------------------------
def test_document_delete_and_rename():
    _auth_off()
    pid = _project("doc-proj", targets="fr-FR")
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="orig.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        s.add(Segment(document_id=did, target_locale="fr-FR", source_text="x"))
        # project TM must survive a single-document delete
        s.add(
            SegmentTM(
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="x",
                target_text="y",
                source_hash="dh",
            )
        )
        s.commit()
    # rename
    with Session(engine) as s:
        content.rename_document(s, did, "renamed.txt", actor="a@x.test")
        assert s.get(Document, did).filename == "renamed.txt"
    assert _last_audit("document.rename")
    # F1 (MEDIUM, adversarial review): a CR/LF-bearing filename must be refused at the
    # SERVICE layer, not just in one route -- the download route (documents.py) reflects
    # doc.filename verbatim into Content-Disposition unescaped, so a stored CRLF is a
    # live response-header injection. Both the web /documents/{id}/rename route and the
    # /api/documents/{id}/rename route call this same function, so this one guard
    # protects both surfaces. A normal name is untouched.
    with Session(engine) as s:
        try:
            content.rename_document(s, did, 'evil"\r\nX-Injected: 1.txt', actor="a@x.test")
            assert False, "CRLF rename should raise ValueError"
        except ValueError as exc:
            assert "control character" in str(exc), exc
        assert s.get(Document, did).filename == "renamed.txt", (
            "a refused rename must not mutate the stored filename"
        )
    print(
        "  document: rename refuses CR/LF/control-char filenames (F1) at the service "
        "layer, leaving the stored filename untouched"
    )
    # delete: doc + segments gone, project TM PRESERVED
    with Session(engine) as s:
        content.delete_document(s, did, actor="a@x.test")
        assert s.get(Document, did) is None
        assert not s.exec(select(Segment).where(Segment.document_id == did)).all()
        tm_left = s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
        assert len(tm_left) == 1, "single-doc delete must NOT wipe project TM"
    assert _last_audit("document.delete")
    print("  document: rename (blob id-keyed) + delete (segments gone, project TM kept) OK")


# --- glossary rename parity (CLI + /api) -------------------------------------
def test_glossary_rename_parity_cli_api():
    _auth_off()
    with Session(engine) as s:
        g = glossary_module.create(s, "Gloss A", created_by="a@x.test")
        gid = g.id
    # CLI rename
    rc, _out = _cli_json(["glossary", "module", "rename", str(gid), "Gloss B"])
    assert rc == 0
    with Session(engine) as s:
        assert s.get(Glossary, gid).name == "Gloss B"
    # /api rename (can_tune_ai — linguist)
    uid = _user("lg@x.test", role=Role.LINGUIST)
    _row, token = apikeys.create_key(uid, "k", scopes="docs")
    r = TestClient(app).post(
        f"/api/glossaries/{gid}/rename", json={"name": "Gloss C"}, headers=_docs_key(token)
    )
    assert r.status_code == 200 and r.json()["name"] == "Gloss C", r.text[:150]
    print("  glossary rename parity: CLI + /api both rename OK")


# --- language set delete fail-closed (both ref kinds) ------------------------
def test_langset_delete_fail_closed_both_refs():
    _auth_off()
    # (a) blocked by a RoutingRule.language_set_id
    ls_a = languages.create("LS-rule", "en-US", ["fr-FR"])
    with Session(engine) as s:
        s.add(RoutingRule(language_set_id=ls_a.id))
        s.commit()
    try:
        languages.delete(ls_a.id)
        assert False, "langset delete should fail closed on a routing rule"
    except languages.LanguageSetInUseError as exc:
        assert exc.refs["routing_rules"] == 1

    # (b) blocked by a Connection config JSON reference
    ls_b = languages.create("LS-conn", "en-US", ["de-DE"])
    with Session(engine) as s:
        s.add(Connection(kind="jira", name="lc", config={"language_set_id": ls_b.id}))
        s.commit()
    try:
        languages.delete(ls_b.id)
        assert False, "langset delete should fail closed on a connection config ref"
    except languages.LanguageSetInUseError as exc:
        assert exc.refs["connections"] == 1

    # (c) unreferenced set deletes cleanly
    ls_c = languages.create("LS-free", "en-US", ["it-IT"])
    languages.delete(ls_c.id)
    with Session(engine) as s:
        assert s.get(LanguageSet, ls_c.id) is None
    print("  langset delete: fails closed on routing-rule AND connection-config refs OK")


# --- foreign-customer caller -> 404 on the new {id} routes --------------------
def test_foreign_customer_caller_404():
    _auth_on()
    try:
        c = TestClient(app)
        home = _customer("Home")
        other = _customer("Other")
        # a project owned by 'other' customer
        pid = _project("foreign", customer_id=other)
        # a caller bound to 'home' with a can_write role (linguist)
        uid = _user("bound@x.test", role=Role.LINGUIST, customer_id=home)
        _row, token = apikeys.create_key(uid, "k", scopes="docs")
        # rename a foreign-customer project -> 404 (not enumerable)
        r = c.post(f"/api/projects/{pid}/rename", json={"name": "hax"}, headers=_docs_key(token))
        assert r.status_code == 404, f"foreign-customer rename must 404 ({r.status_code})"
        with Session(engine) as s:
            assert s.get(Project, pid).name == "foreign", "foreign project was mutated!"
    finally:
        _auth_off()


# --- FIX 1: template rename dup-check must be org-scoped, not a cross-tenant --
# name-existence oracle (adversarial-review finding). templates.get_by_name() is
# UNSCOPED by design (single-tenant callers rely on it); app/web/projects.py's
# _rename_template_compat() previously called it directly for the duplicate-name
# check, so an org-A manager renaming to a name that only exists in org B got a
# false "already exists" 403 (a template-name existence oracle across tenants),
# and org A could never legitimately reuse org B's name. The fix scopes the
# dup-check to the caller's own org INSIDE the compat function (see its comment).
def test_template_rename_dup_check_is_org_scoped():
    with Session(engine) as s:
        oa = Organization(name="Tpl Rename Org A")
        ob = Organization(name="Tpl Rename Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
        # a template already named X, living in org B
        tpl_b = ProjectTemplate(
            name="Shared Rename Name", source_locale="en-US", target_locales=["fr-FR"], org_id=ob_id
        )
        # two templates in org A: one we rename to X (cross-org), one that then
        # collides with it (same-org)
        tpl_a = ProjectTemplate(
            name="Org A Tpl", source_locale="en-US", target_locales=["fr-FR"], org_id=oa_id
        )
        tpl_a2 = ProjectTemplate(
            name="Org A Tpl 2", source_locale="en-US", target_locales=["fr-FR"], org_id=oa_id
        )
        s.add(tpl_b)
        s.add(tpl_a)
        s.add(tpl_a2)
        s.commit()
        s.refresh(tpl_b)
        s.refresh(tpl_a)
        s.refresh(tpl_a2)
        tpl_b_id, tpl_a_id, tpl_a2_id = tpl_b.id, tpl_a.id, tpl_a2.id
        mgr = User(
            email="tpl-rename-a-mgr@x.test",
            name="A Mgr",
            role=Role.MANAGER,
            org_id=oa_id,
            password_hash=security.hash_password("aa-mgr-pass-123456"),
        )
        s.add(mgr)
        s.commit()

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        c = TestClient(app, follow_redirects=False)
        csrf_for(c)
        r = c.post(
            "/auth/login",
            data={
                "email": "tpl-rename-a-mgr@x.test",
                "password": "aa-mgr-pass-123456",
                "next": "/",
            },
        )
        assert r.status_code == 303, f"login failed ({r.status_code})"
        csrf_for(c)

        # cross-org: org A renames its template to org B's existing name ->
        # SUCCEEDS (no cross-tenant existence oracle, no false collision).
        r = c.post(f"/templates/{tpl_a_id}/rename", data={"name": "Shared Rename Name"})
        assert r.status_code == 303, f"cross-org rename should succeed ({r.status_code})"
        with Session(engine) as s:
            assert s.get(ProjectTemplate, tpl_a_id).name == "Shared Rename Name"
            assert s.get(ProjectTemplate, tpl_b_id).name == "Shared Rename Name", (
                "org B's template must be untouched"
            )

        # same-org collision: org A's second template still can't steal a name
        # org A's first template just claimed.
        r = c.post(f"/templates/{tpl_a2_id}/rename", data={"name": "Shared Rename Name"})
        assert r.status_code == 403, f"same-org collision should still be refused ({r.status_code})"
        with Session(engine) as s:
            assert s.get(ProjectTemplate, tpl_a2_id).name == "Org A Tpl 2"

        # self-rename to its own current name still succeeds (no false self-collision).
        r = c.post(f"/templates/{tpl_a_id}/rename", data={"name": "Shared Rename Name"})
        assert r.status_code == 303, f"self-rename to current name should succeed ({r.status_code})"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print(
        "  template rename dup-check: cross-org name reuse ok, same-org collision "
        "refused, self-rename ok (no cross-tenant oracle) OK"
    )


# --- template rename is MANAGE-role on BOTH surfaces (RBAC-parity gap) --------
# The web twin POST /templates/{id}/rename requires can_manage ("Renaming templates
# requires a manager role"); the /api route POST /api/templates/{tid}/rename previously
# used _write_block, so a translator key renamed a template the same account is 403'd
# from renaming on the web (probe-proven by the lane-W audit). Now both are _manage_block
# / can_manage. This asserts the parity + that a refused rename leaves the name unchanged.
def test_template_rename_rbac_manager_parity():
    PW = "tpl-rbac-pass-123456"
    with Session(engine) as s:
        tpl = ProjectTemplate(
            name="RBAC Tpl", source_locale="en-US", target_locales=["fr-FR"]
        )  # global (org+customer None)
        s.add(tpl)
        s.commit()
        s.refresh(tpl)
        tid = tpl.id
    tr = security.create_user("tpl-rbac-tr@x.test", "TR", Role.TRANSLATOR, PW)
    mgr = security.create_user("tpl-rbac-mgr@x.test", "MGR", Role.MANAGER, PW)
    _r, tr_tok = apikeys.create_key(tr.id, "k", scopes="docs")
    _r, mgr_tok = apikeys.create_key(mgr.id, "k", scopes="docs")

    def _name():
        with Session(engine) as s:
            return s.get(ProjectTemplate, tid).name

    _auth_on()
    try:
        c = TestClient(app, follow_redirects=False)
        # --- /api: translator 403 (name unchanged), manager 200 ---
        r = c.post(
            f"/api/templates/{tid}/rename", headers=_docs_key(tr_tok), json={"name": "Hacked"}
        )
        assert r.status_code == 403, ("api translator", r.status_code, r.text[:200])
        assert _name() == "RBAC Tpl", "translator /api rename must not change the name"
        r = c.post(
            f"/api/templates/{tid}/rename", headers=_docs_key(mgr_tok), json={"name": "Api Renamed"}
        )
        assert r.status_code == 200, ("api manager", r.status_code, r.text[:200])
        assert _name() == "Api Renamed"

        # --- web: translator 403 (name unchanged), manager 303 ---
        def _weblogin(email):
            wc = TestClient(app, follow_redirects=False)
            csrf_for(wc)
            lr = wc.post("/auth/login", data={"email": email, "password": PW, "next": "/"})
            assert lr.status_code == 303, ("login", email, lr.status_code)
            csrf_for(wc)
            return wc

        wtr = _weblogin("tpl-rbac-tr@x.test")
        r = wtr.post(f"/templates/{tid}/rename", data={"name": "WebHacked"})
        assert r.status_code == 403, ("web translator", r.status_code)
        assert _name() == "Api Renamed", "web translator rename must not change the name"
        wmgr = _weblogin("tpl-rbac-mgr@x.test")
        r = wmgr.post(f"/templates/{tid}/rename", data={"name": "Web Renamed"})
        assert r.status_code == 303, ("web manager", r.status_code)
        assert _name() == "Web Renamed"
    finally:
        _auth_off()
    print(
        "  template rename RBAC: /api + web both refuse a translator (name unchanged) "
        "and allow a manager (can_manage parity) OK"
    )


# --- FIX 2: permanent self-discovering route sweep ----------------------------
# The by-id route sweep (adversarial-review process finding): every {id}-bearing
# route under the project/customer/document/template/glossary/langset families,
# DISCOVERED from the live FastAPI router (never hardcoded), must 404 a customer-
# bound caller's request for a FOREIGN customer's resource — so a new by-id route
# in this branch is covered automatically the moment it's registered. Mirrors the
# technique in tests/verify_connector_isolation_gaps.py (test_j/test_k on the
# sibling workflow-rename-delete branch), reimplemented standalone here.
_SWEEP_FAMILY_PREFIXES = (
    "/projects/",
    "/customers/",
    "/documents/",
    "/templates/",
    "/glossaries/",
    "/langsets/",
    "/language-sets/",
    "/api/projects/",
    "/api/customers/",
    "/api/documents/",
    "/api/templates/",
    "/api/glossaries/",
    "/api/langsets/",
    "/api/language-sets/",
    # SPEC-key-based-projects section 5: the key-addressed routes are a NEW by-id
    # family whose leading id is a StringKey, not a project, so the sweep needs its
    # own foreign row for them (below) or it would silently cover nothing here.
    "/api/keys/",
)

# HTTP methods a real caller can drive (excludes HEAD/OPTIONS auto-added by the router).
_SWEEP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# A route may escape the foreign-customer check ONLY by being named here, with a
# reason. Currently: LanguageSet (app/models.py) has no customer_id at all — it is
# org-scoped only (a reusable locale set shared across a whole org) — so there is
# no "foreign customer's language set" to probe; the org ring is this family's
# only isolation axis and is covered by _langset_in_org/list_sets(org_id=) tests
# elsewhere. Every other by-id route in these families IS customer-scoped and
# must pass the sweep unallowlisted.
_SWEEP_ALLOWLIST = {
    "/api/langsets/{sid}": "LanguageSet has no customer_id (org-scoped only)",
    "/api/langsets/{sid}/delete": "LanguageSet has no customer_id (org-scoped only)",
}


def _family_kind(path):
    """Which resource kind the route's LEADING {id} addresses, from its prefix."""
    if path.startswith("/api/keys/"):
        return "key"
    if "/customers/" in path:
        return "customer"
    if "/documents/" in path:
        return "document"
    if "/templates/" in path:
        return "template"
    if "/glossaries/" in path:
        return "glossary"
    if "/langsets/" in path or "/language-sets/" in path:
        return "langset"
    return "project"


def _sweep_routes():
    """Every registered route in the swept families whose path carries at least
    one path param, from the LIVE app router (never hardcoded) — so a NEW by-id
    route is covered automatically the moment it is registered."""
    from app.web._routes import walk_routes

    out = []
    for r in walk_routes(app):
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        if "{" not in path:
            continue
        if any(path.startswith(p) for p in _SWEEP_FAMILY_PREFIXES):
            out.append((path, frozenset(m for m in methods if m in _SWEEP_METHODS)))
    return out


def _fill_sweep_path(path, foreign_id):
    """The route's LEADING {..} -> the FOREIGN id; any further {..} -> a dummy.
    The ring resolves the leading (family) row FIRST, so a foreign leading id
    404s before any nested id is ever consulted."""
    first = re.sub(r"\{[^}]+\}", str(foreign_id), path, count=1)
    return re.sub(r"\{[^}]+\}", "999999", first)


# A generic payload covering every Form(...)/File(...) field name across the
# swept web routes' by-id POSTs, so a request survives FastAPI's pre-body
# dependency validation and REACHES the ring check — a 422 (pre-body, non-
# enumerable on its own) would otherwise mask the 404 this sweep asserts.
# Values are inert; a foreign-id request 404s at the ring before any of them are
# ever validated for business meaning.
_SWEEP_WEB_FORM = {
    "name": "x",
    "filename": "x",
    "locale": "fr-FR",
    "text": "x",
    "notes": "",
    "primary_contact": "",
    "technical_contact": "",
    "default_provider": "",
    "default_workflow_id": "",
    "default_segmentation_profile_id": "",
    "default_content_type_id": "",
    "pair": "",
    "status": "in_progress",
    "due_date": "",
    "content_type_id": "",
    "confirm_name": "",
    "scope": "global",
    "description": "",
    "segmentation_profile_id": "",
    "provider_mode": "uniform",
    "email": "sweep-nobody@x.test",
    "source_locale": "en-US",
    "target_locale": "fr-FR",
    "source_term": "x",
    "target_term": "",
    "dnt": "",
    "note": "",
    "mode": "segmented",
    "workflow_id": "1",
    "sense_label": "x",
    "context_cue": "",
    "language_set_id": "",
}
_SWEEP_FILES = {"file": ("sweep.txt", b"a\tb\n", "text/plain")}

# Same idea for /api by-id POSTs (JSON body — api_cli.py's _json() defaults a
# missing/unparseable body to {}, so most routes never NEED this; a few validate
# required fields BEFORE the ring, so a complete body is supplied to reach it).
_SWEEP_API_JSON = {
    "name": "x",
    "targets": ["fr-FR"],
    "target_locales": ["fr-FR"],
    "source": "en-US",
    "target": "fr-FR",
    "source_locale": "en-US",
    "target_locale": "fr-FR",
    "csv_b64": base64.b64encode(b"source,target\nhello,bonjour\n").decode(),
    "tmx_b64": base64.b64encode(b"<tmx/>").decode(),
    "confirm": "",
    "dry_run": False,
    "content_type_id": "",
    "langset_id": "1",
    "source_term": "x",
    "target_term": "",
    "customer_id": "",
}
# Query params for by-id GETs that require one (locale / source+target) — a
# missing REQUIRED query param is a pre-body 422 (FastAPI), which would mask
# the 404 this sweep asserts, same rationale as the form/JSON payloads above.
_SWEEP_QUERY = {"locale": "fr-FR", "source": "en-US", "target": "fr-FR", "q": "x"}


def test_sweep_by_id_routes_foreign_customer_404():
    _auth_on()
    try:
        home = _customer("Sweep Home")
        other = _customer("Sweep Other")
        own_pid = _project("sweep-own-project", customer_id=home)
        foreign_pid = _project("sweep-foreign-project", customer_id=other)
        with Session(engine) as s:
            foreign_doc = Document(project_id=foreign_pid, filename="foreign.txt", fmt="txt")
            s.add(foreign_doc)
            foreign_tpl = ProjectTemplate(
                name="Sweep Foreign Tpl",
                source_locale="en-US",
                target_locales=["fr-FR"],
                customer_id=other,
            )
            s.add(foreign_tpl)
            foreign_gloss = Glossary(name="Sweep Foreign Gloss", customer_id=other)
            s.add(foreign_gloss)
            s.commit()
            s.refresh(foreign_doc)
            s.refresh(foreign_tpl)
            s.refresh(foreign_gloss)
            foreign_doc_id, foreign_tpl_id, foreign_gloss_id = (
                foreign_doc.id,
                foreign_tpl.id,
                foreign_gloss.id,
            )

        # SPEC-key-based-projects: a real foreign STRING KEY, so the /api/keys/{id}
        # family is swept against something that actually exists. Against a
        # nonexistent id every route 404s trivially and the sweep would prove nothing
        # about the ring.
        from app.models import ProjectType as _PT
        from app.services import keys as _keys

        with Session(engine) as s:
            key_proj = content.project_create(
                s,
                "sweep-foreign-keys",
                source_locale="en-US",
                target_locales="fr-FR",
                customer_id=str(other),
                project_type=_PT.KEY_BASED,
            )
            foreign_key_id = _keys.create_key(
                s, key_proj, "sweep::foreign", base_value="Foreign", actor="sweep"
            ).id

        foreign_ids = {
            "project": foreign_pid,
            "customer": other,
            "document": foreign_doc_id,
            "template": foreign_tpl_id,
            "glossary": foreign_gloss_id,
            "key": foreign_key_id,
        }

        # Bound-caller identity: customer-bound to 'home' AND role=ADMIN, so it
        # clears EVERY route's role gate (can_write/can_manage/can_tune_ai/
        # is_admin) and the sweep exercises ONLY the ring, never a role denial —
        # a role-insufficient response would be a uniform 403 regardless of
        # foreign-ness (not an existence leak) and would falsely look like a
        # sweep failure. A real account can never be BOTH customer-bound and
        # admin (security.create_user enforces bound accounts are translator/
        # linguist/viewer only); constructed directly here, bypassing that
        # invariant, purely to isolate the ring check — same technique
        # tests/verify_templates.py::test_delete_cross_org uses for its
        # org-bound admin actor.
        with Session(engine) as s:
            web_user = User(
                email="sweep-web@x.test",
                name="Sweep Web",
                role=Role.ADMIN,
                active=True,
                customer_id=home,
                password_hash=security.hash_password("sweep-web-pass-123456"),
            )
            s.add(web_user)
            s.commit()
        web = TestClient(app, follow_redirects=False)
        csrf_for(web)
        r = web.post(
            "/auth/login",
            data={"email": "sweep-web@x.test", "password": "sweep-web-pass-123456", "next": "/"},
        )
        assert r.status_code == 303, f"web login failed ({r.status_code})"
        csrf_for(web)

        # /api: a per-user key carrying EVERY scope, same bound-admin shape. The
        # scopes are here for the same reason the ADMIN role above is: a scope denial,
        # like a role denial, is a uniform 403 regardless of foreign-ness — not an
        # existence leak, but it would stop the request before the ring and leave the
        # ring on those routes completely untested. `keys` (SPEC-key-based-projects
        # section 5) is deliberately NOT implied by `docs`, so it has to be listed.
        key_user = _user("sweep-api@x.test", role=Role.ADMIN, customer_id=home)
        _row, token = apikeys.create_key(key_user, "sweep-key", scopes="docs keys")
        api = TestClient(app, follow_redirects=False)
        h = _docs_key(token)

        routes = _sweep_routes()
        assert routes, "route enumeration returned NOTHING — discovery broke"
        discovered = {p for p, _m in routes}
        stale = set(_SWEEP_ALLOWLIST) - discovered
        assert not stale, f"allowlist names route(s) that no longer exist: {sorted(stale)!r}"

        leaks, covered, swept = [], 0, set()
        for path, methods in sorted(routes):
            if path in _SWEEP_ALLOWLIST:
                continue
            kind = _family_kind(path)
            is_api = path.startswith("/api/")
            client = api if is_api else web
            url = _fill_sweep_path(path, foreign_ids[kind])
            for method in sorted(methods):
                if is_api:
                    kwargs = {"headers": h}
                    if method == "GET":
                        kwargs["params"] = _SWEEP_QUERY
                    else:
                        kwargs["json"] = _SWEEP_API_JSON
                else:
                    kwargs = {}
                    if method == "GET":
                        kwargs["params"] = _SWEEP_QUERY
                    else:
                        kwargs["data"] = _SWEEP_WEB_FORM
                        kwargs["files"] = _SWEEP_FILES
                resp = client.request(method, url, follow_redirects=False, **kwargs)
                covered += 1
                swept.add(path)
                # A bound caller's FOREIGN customer resource must be indistinguishable
                # from missing: 404, BEFORE the route body / any mutation runs.
                if resp.status_code != 404:
                    leaks.append(
                        "{} {} -> {} loc={!r} body={!r}".format(
                            method,
                            path,
                            resp.status_code,
                            resp.headers.get("location", ""),
                            resp.text[:100],
                        )
                    )
        assert not leaks, (
            "SWEEP: by-id route(s) did NOT 404 a bound caller's FOREIGN customer "
            "resource (ring missing or bypassed):\n  " + "\n  ".join(leaks)
        )
        assert covered > 0, "swept 0 route/method pairs — enumeration/filter broke"

        # Sanity: the bound caller's OWN project is reachable PAST the ring, so
        # the ring isn't just blanket-denying everything.
        r = api.get(f"/api/projects/{own_pid}", headers=h)
        assert r.status_code == 200, f"own project must pass the ring (got {r.status_code})"

        print(
            "  sweep: %d by-id routes discovered, %d swept x methods = %d "  # noqa: UP031
            "route/method pairs -> foreign customer 404 on ALL; %d allowlisted "
            "(%s); own project reachable OK"
            % (
                len(discovered),
                len(swept),
                covered,
                len(_SWEEP_ALLOWLIST),
                ", ".join(sorted(_SWEEP_ALLOWLIST)) or "none",
            )
        )
    finally:
        _auth_off()


def test_pending_upload_erasure_surfaces():
    from app import ports
    from app.ports import blobstore
    from app.services.ingest import ingest_document

    class FailedDeletes:
        def put(self, key, data):
            pass

        def delete_status(self, key):
            return blobstore.DeleteStatus.FAILED

    _auth_off()
    ports.register("blobstore", FailedDeletes())
    try:
        client = TestClient(app)
        uid = _user("pending-admin@x.test", role=Role.ADMIN)
        _row, token = apikeys.create_key(uid, "pending", scopes="docs")
        for kind in ("document", "project"):
            for surface in ("web", "api", "cli"):
                name = f"pending-{kind}-{surface}"
                pid = _project(name)
                with Session(engine) as s:
                    doc = ingest_document(
                        s, s.get(Project, pid), "private.txt", b"private", "segmented"
                    )
                    did = doc.id
                    if kind == "project":
                        ingest_document(
                            s, s.get(Project, pid), "second.txt", b"private too", "segmented"
                        )
                expected = 2 if kind == "project" else 1
                resource_id = did if kind == "document" else pid
                if surface == "web":
                    url = f"/{kind}s/{resource_id}/delete"
                    response = client.post(url, data={"confirm_name": name}, follow_redirects=False)
                    assert response.status_code == 303, response.text
                    assert f"file_pending={expected}" in response.headers["location"], (
                        response.headers
                    )
                    page = client.get(response.headers["location"])
                    assert "Upload erasure is pending" in page.text
                elif surface == "api":
                    response = client.post(
                        f"/api/{kind}s/{resource_id}/delete",
                        json={"confirm": "private.txt" if kind == "document" else name},
                        headers=_docs_key(token),
                    )
                    assert response.status_code == 200, response.text
                    assert response.json()["cascade"]["file_pending"] == expected, response.text
                else:
                    from app.cli.__main__ import main as cli_main

                    buf = io.StringIO()
                    args = (
                        ["doc", "delete", str(did), "--yes"]
                        if kind == "document"
                        else ["project", "delete", str(pid), "--force"]
                    )
                    with redirect_stdout(buf):
                        rc = cli_main(args)
                    assert rc == 0, buf.getvalue()
                    assert "Upload erasure is pending" in buf.getvalue(), buf.getvalue()
                assert f"file_pending={expected}" in _last_audit(f"{kind}.delete").detail
    finally:
        ports.unregister("blobstore")
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from app.cli import _remote_impl_part2 as remote

    remote_client = Mock()
    remote_client.post.side_effect = [
        {"project_name": "pending-remote", "cascade": {"documents": 2}},
        {"cascade": {"file_pending": 2}},
    ]
    emit = Mock()
    with patch.object(remote, "_client", return_value=remote_client):
        assert remote.project_delete(SimpleNamespace(project_id=42, force=True), emit) == 0
    message, payload = emit.result.call_args.args
    assert "Upload erasure is pending" in message, message
    assert payload["cascade"]["file_pending"] == 2, payload
    print("  failed upload erasure: document/project web, API, CLI and audit report pending OK")


def main():
    test_pending_upload_erasure_surfaces()
    test_inbox_rekey_survives_rename_and_fail_pre_fix()
    test_project_rename_all_surfaces_and_audit()
    test_project_delete_cascade_completeness()
    test_project_delete_budget_and_suggestion_fk_clean()
    test_project_delete_reaps_leftover_advanced_rows_without_addon()
    test_project_delete_blocker_fail_closed()
    test_project_delete_workflow_suggestion_forecast_and_cli_guard()
    test_project_delete_typed_confirm_web_and_api()
    test_project_delete_rbac_non_admin_blocked()
    test_customer_rename_and_delete_fail_closed()
    test_document_delete_and_rename()
    test_glossary_rename_parity_cli_api()
    test_langset_delete_fail_closed_both_refs()
    test_foreign_customer_caller_404()
    test_template_rename_dup_check_is_org_scoped()
    test_template_rename_rbac_manager_parity()
    test_sweep_by_id_routes_foreign_customer_404()
    print("RENAME DELETE PROJECTS OK")


if __name__ == "__main__":
    main()
