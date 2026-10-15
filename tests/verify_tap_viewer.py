"""Prompt Tap RUN-ENVELOPE VIEWER (proposal §9) -- web + CLI parity suite.

Proves the grouped viewer contracts on top of the corrected grain:

  PAGE  A finalized workflow/batch run appears on /prompt-tap as a GROUPED entry: the
        action (project / document / locales / status) with its language-pair RUNS nested,
        and each run's captured CALLS nested inside those -- not a flat stream. A 2-locale
        batch is ONE action row with TWO run rows.
  EMPTY An install with no runs shows the teaching empty state, not a blank page.
  FILT  The action filters (project / status / has-failures) narrow the grouped list.
  FLAT  The legacy/flat "All calls" section is still reachable (union read + Clear
        scoping from Wave A must not regress); eval/ad-hoc/legacy records still show.
  CLI   `prompt-tap actions` and `prompt-tap show-action <id>` return the same tree
        (web+CLI parity law).
  ADMIN The page stays admin-only (unchanged access, D8).

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared keys; TestClient for the web arm; a real
subprocess for the CLI arm (true parity). ASCII-only prints.
"""

import json
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tapviewer_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Organization,
    Project,
    Segment,
    SegStatus,
    StepType,
    TapAction,
    TapActionKind,
    TapRun,
    TapStatus,
    TransMode,
    User,
    Workflow,
    WorkflowStep,
)
from app.services import (
    batch_runs,
    prompt_tap,
    security,
    settings,
    tap_envelope,
    tap_view,
)
from tests._csrf import csrf_for

_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _reset():
    prompt_tap.clear()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    with Session(engine) as s:
        from app.models import TapRun

        for row in s.exec(select(TapRun)).all():
            s.delete(row)
        for row in s.exec(select(TapAction)).all():
            s.delete(row)
        s.commit()
    settings.set_many({"prompt_tap_enabled": "1", "prompt_tap_envelopes_enabled": "1"})


def _login(c):
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": "admin", "password": "admin", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, ("admin login", r.status_code)
    csrf_for(c)
    return r


def _mk_project(s, name=None):
    p = Project(name=name or ("view-%d" % _seq()), source_locale="en-US", default_provider="mock")  # noqa: UP031
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _mk_multiloc_doc(s, project, sources, locales):
    doc = Document(
        project_id=project.id,
        filename="doc%d.txt" % _seq(),  # noqa: UP031
        fmt="txt",
        mode=TransMode.SEGMENTED,  # noqa: RUF100, UP031
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, txt in enumerate(sources):
        for loc in locales:
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=loc,
                    order_index=i,
                    source_text=txt,
                    status=SegStatus.NEW,
                )
            )
    s.commit()
    return doc


def _mk_workflow(s):
    wf = Workflow(name="wf-%d" % _seq(), scope="global")  # noqa: UP031
    s.add(wf)
    s.commit()
    s.refresh(wf)
    s.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            provider="mock",
            step_type=StepType.LLM_AGENT,
            prompt_text="Translate: {text}",
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
            key="translate",
        )
    )
    s.commit()
    return wf


def _run_batch(project_name="Acme Docs", locales=("es-ES", "fr-FR")):
    with Session(engine) as s:
        proj = _mk_project(s, name=project_name)
        doc = _mk_multiloc_doc(s, proj, ["Open Settings now.", "Second line."], list(locales))
        wf = _mk_workflow(s)
        wf_id, doc_id, pid = wf.id, doc.id, proj.id
    with Session(engine) as s:
        batch_runs.start_batch(
            s, workflow_id=wf_id, doc_id=doc_id, locales=list(locales), synchronous=True
        )
    return pid, doc_id


# --- PAGE: a batch run renders as one grouped action with nested runs + calls ----
def test_page_renders_grouped():
    _reset()
    _pid, _doc_id = _run_batch(project_name="Acme Docs", locales=("es-ES", "fr-FR"))
    c = TestClient(app)
    _login(c)
    page = c.get("/prompt-tap").text
    assert "Runs" in page and "grouped by translation action" in page, "grouped section header"
    assert "Acme Docs" in page, "the action shows its project name"
    assert "es-ES" in page and "fr-FR" in page, "both language-pair runs shown"
    # The grouped structure is present (action + run disclosures) and a nested call renders.
    assert "tap-action" in page and "tap-run" in page and "tap-call" in page, (
        "action -> run -> call hierarchy rendered"
    )
    # Exactly ONE action row for the 2-locale batch (grain), with two run locales.
    with Session(engine) as s:
        acts = s.exec(select(TapAction)).all()
        assert len(acts) == 1, ("one action for the batch click", len(acts))
    print("  (PAGE) 2-locale batch renders as ONE grouped action with 2 nested runs OK")


# --- EMPTY: teaching empty state -------------------------------------------------
def test_empty_state():
    _reset()
    c = TestClient(app)
    _login(c)
    page = c.get("/prompt-tap").text
    assert "No translation runs recorded yet" in page, "grouped empty state"
    print("  (EMPTY) no-runs empty state renders OK")


# --- FILT: action filters narrow the grouped list --------------------------------
def test_action_filters():
    _reset()
    pid_a, _ = _run_batch(project_name="Alpha Project", locales=("es-ES",))
    _pid_b, _ = _run_batch(project_name="Bravo Project", locales=("de-DE",))
    c = TestClient(app)
    _login(c)
    # Filter to project A only.
    page = c.get("/prompt-tap?action_project=%d" % pid_a).text  # noqa: UP031
    assert "Alpha Project" in page and "Bravo Project" not in page, (
        "project filter narrows to the requested project"
    )
    # has_failures: neither run failed (mock) -> no actions shown.
    page_f = c.get("/prompt-tap?action_failures=1").text
    assert "No translation runs recorded yet" in page_f, "has-failures filter hides clean runs"
    print("  (FILT) project + has-failures filters narrow the grouped list OK")


# --- FLAT: the legacy/flat section is still reachable ----------------------------
def test_flat_section_reachable():
    _reset()
    # A legacy record (no action) must still be reachable in the flat section.
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="claude",
            provider_kind="anthropic",
            model="m",
            task="translate",
            system="s",
            prompt="LEGACYPROBE",
            max_tokens=8,
            payload={"text": "x"},
            outcome="ok",
            response_text="y",
        )
    )
    c = TestClient(app)
    _login(c)
    page = c.get("/prompt-tap").text
    assert "All calls" in page, "the flat union section is present"
    assert "LEGACYPROBE" in page, "a legacy (no-action) record is still reachable on the page"
    assert "/prompt-tap/download" in page, "the flat section keeps its Download control"
    print("  (FLAT) legacy/flat 'All calls' section reachable; download control intact OK")


# --- ADMIN: the page stays admin-only -------------------------------------------
def test_admin_only():
    _reset()
    c = TestClient(app)  # not logged in
    r = c.get("/prompt-tap", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403), ("anon must not read the tap", r.status_code)
    print("  (ADMIN) /prompt-tap stays gated (anon denied) OK")


# --- CLI: actions + show-action parity (real subprocess) -------------------------
def _cli(*args):
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return out


def test_cli_actions_and_show_action():
    _reset()
    _pid, _doc_id = _run_batch(project_name="CLI Project", locales=("es-ES", "fr-FR"))
    # `--json prompt-tap actions` returns the grouped tree (--json is a GLOBAL flag).
    r = _cli("--json", "prompt-tap", "actions")
    assert r.returncode == 0, ("actions --json rc", r.returncode, r.stderr)
    data = json.loads(r.stdout)
    assert data["ok"] and data["count"] == 1, ("one action", data)
    action = data["actions"][0]
    assert sorted(action["requested_locales"]) == ["es-ES", "fr-FR"], action
    assert len(action["runs"]) == 2, ("two runs under the action", action["runs"])
    aid = action["id"]

    # Table form lists the action.
    r2 = _cli("prompt-tap", "actions")
    assert r2.returncode == 0 and "CLI Project" in r2.stdout, r2.stdout

    # `show-action <id>` returns the same action's runs.
    r3 = _cli("--json", "prompt-tap", "show-action", aid)
    assert r3.returncode == 0, (r3.returncode, r3.stderr)
    det = json.loads(r3.stdout)["action"]
    assert det["id"] == aid and len(det["runs"]) == 2, det
    assert sorted(rn["target_locale"] for rn in det["runs"]) == ["es-ES", "fr-FR"], det

    # An unknown id is a clean usage error, not a crash.
    r4 = _cli("prompt-tap", "show-action", "01BOGUSBOGUSBOGUSBOGUSBOGO")
    assert r4.returncode != 0 and "no such action" in (r4.stderr + r4.stdout).lower(), (
        r4.returncode,
        r4.stderr,
        r4.stdout,
    )
    print("  (CLI) prompt-tap actions + show-action return the grouped tree (parity) OK")


# --- H2: the grouped view is org-scoped under active multi-org --------------------
def test_h2_grouped_view_org_scoped():
    """H2 (HIGH): under active multi-org, the grouped-actions section must be confined to
    the caller's org (TapAction.org_id). Another org's project name / document filename /
    initiator must not appear. Single-tenant (org_id=None) still returns both -- byte-
    identical. The vulnerable seam is tap_view.list_actions + _name_maps, tested directly
    (the route is a thin caller passing _my_org_id())."""
    _reset()
    from app.services import access  # noqa: F401

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            oa, ob = Organization(name="OrgA"), Organization(name="OrgB")
            s.add(oa)
            s.add(ob)
            s.commit()
            s.refresh(oa)
            s.refresh(ob)
            org_a, org_b = oa.id, ob.id
            pa = Project(
                name="ALPHASECRETPROJ", source_locale="en-US", default_provider="mock", org_id=org_a
            )
            pb = Project(
                name="BRAVOSECRETPROJ", source_locale="en-US", default_provider="mock", org_id=org_b
            )
            s.add(pa)
            s.add(pb)
            s.commit()
            s.refresh(pa)
            s.refresh(pb)
            da = Document(
                project_id=pa.id, filename="alpha_secret.txt", fmt="txt", mode=TransMode.SEGMENTED
            )
            db_ = Document(
                project_id=pb.id, filename="bravo_secret.txt", fmt="txt", mode=TransMode.SEGMENTED
            )
            s.add(da)
            s.add(db_)
            s.commit()
            s.refresh(da)
            s.refresh(db_)
            s.add(
                TapAction(
                    id=tap_envelope.new_id(),
                    kind=TapActionKind.WORKFLOW,
                    org_id=org_a,
                    project_id=pa.id,
                    document_id=da.id,
                    initiator="alice@a.test",
                    requested_locales=["es-ES"],
                    status=TapStatus.COMPLETED,
                )
            )
            s.add(
                TapAction(
                    id=tap_envelope.new_id(),
                    kind=TapActionKind.WORKFLOW,
                    org_id=org_b,
                    project_id=pb.id,
                    document_id=db_.id,
                    initiator="bob@b.test",
                    requested_locales=["fr-FR"],
                    status=TapStatus.COMPLETED,
                )
            )
            advanced_id = tap_envelope.new_id()
            s.add(
                TapAction(
                    id=advanced_id,
                    kind=TapActionKind.ADVANCED,
                    org_id=org_b,
                    project_id=pb.id,
                    status=TapStatus.COMPLETED,
                )
            )
            s.commit()
        # Org A's scoped view: ONLY org A's action, project name, doc filename, initiator.
        a_view = tap_view.list_actions(org_id=org_a)
        assert advanced_id not in {a["id"] for a in tap_view.list_action_summaries(org_id=org_a)}
        assert tap_view.action_summary(advanced_id, org_id=org_a) is None
        assert tap_view.action_summary(advanced_id, org_id=org_b)["kind"] == "advanced"
        names = {v["project_name"] for v in a_view}
        docs = {v["document_name"] for v in a_view}
        inits = {v["initiator"] for v in a_view}
        assert "ALPHASECRETPROJ" in names and "BRAVOSECRETPROJ" not in names, (
            "org B project name leaked into org A's view",
            names,
        )
        assert "alpha_secret.txt" in docs and "bravo_secret.txt" not in docs, (
            "org B document filename leaked",
            docs,
        )
        assert "alice@a.test" in inits and "bob@b.test" not in inits, (
            "org B initiator email leaked",
            inits,
        )
        # Unscoped (single-tenant) returns both -- byte-identical to today.
        both = {v["project_name"] for v in tap_view.list_actions(org_id=None)}
        assert {"ALPHASECRETPROJ", "BRAVOSECRETPROJ"} <= both, both
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  (H2) grouped view scoped to caller org; other org's names/initiator hidden OK")


# --- M3: a grouped per-call Download link carries a resolvable record key ---------
def test_m3_grouped_call_download_link_resolves():
    """M3 (MEDIUM): a per-call Download link in the RUNS section must carry a real record
    key that the download route resolves (pre-fix it emitted `record=` empty -> 404). The
    assertion is scoped to the grouped section so a flat-section key cannot satisfy it."""
    _reset()
    _run_batch(project_name="DL Project", locales=("es-ES",))
    c = TestClient(app)
    _login(c)
    page = c.get("/prompt-tap").text
    # Only the Runs (grouped) section: split on the flat section's heading id, not the words
    # "All calls" (the UX overhaul's jump nav above the Runs section also says "All calls").
    assert 'id="insight-all-calls"' in page, "the flat section heading anchor must render"
    grouped = page.split('id="insight-all-calls"')[0]
    keys = re.findall(r"/prompt-tap/download\?record=([0-9a-f]{16})", grouped)
    assert keys, "a real 16-hex record key must appear in a grouped per-call Download link"
    r = c.get(f"/prompt-tap/download?record={keys[0]}")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.content, "the downloaded record must have content"
    print("  (M3) grouped per-call Download link carries a resolvable record key (200) OK")


# --- L1: _calls_by_run streams under the cap, reports the true total --------------
def _cap_call(aid, rid, prompt):
    prompt_tap.capture(
        prompt_tap.build_record(
            provider="mock",
            provider_kind="mock",
            model="m",
            task="t",
            system="s",
            prompt=prompt,
            max_tokens=8,
            payload={"text": "x"},
            outcome="ok",
            response_text="y",
            cost=0.0,
            tap_meta=({"action_id": aid, "run_id": rid} if rid else {"action_id": aid}),
        )
    )


def _mk_bare_action_run(kind=None):
    with Session(engine) as s:
        a = TapAction(
            id=tap_envelope.new_id(),
            kind=kind or TapActionKind.DIRECT,
            requested_locales=["es-ES"],
            status=TapStatus.COMPLETED,
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        r = TapRun(
            id=tap_envelope.new_id(),
            action_id=a.id,
            target_locale="es-ES",
            status=TapStatus.COMPLETED,
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        return a.id, r.id


def test_l1_calls_capped_while_streaming():
    """L1 (LOW): _calls_by_run(cap=K) must MATERIALIZE at most K (the newest) records per
    run while still reporting the TRUE total -- the cap bites WHILE streaming the file, so a
    huge per-action file (N locales concentrated into one file) cannot blow memory."""
    _reset()
    aid, rid = _mk_bare_action_run()
    for i in range(7):
        _cap_call(aid, rid, "p%d" % i)  # noqa: UP031
    calls_by_run, counts = tap_view._calls_by_run(aid, cap=3)
    assert len(calls_by_run[rid]) == 3, (
        "cap must bound materialized calls",
        len(calls_by_run[rid]),
    )
    assert counts[rid] == 7, ("the true total must still be counted", counts[rid])
    assert calls_by_run[rid][0]["request"]["prompt"] == "p6", (
        "newest-first within the run",
        calls_by_run[rid][0]["request"]["prompt"],
    )
    print("  (L1) _calls_by_run streams under the cap, reports true total, newest-first OK")


# --- L2: capture_withheld is surfaced in the grouped view ------------------------
def test_l2_capture_withheld_renders():
    """L2 (LOW): TapAction.capture_withheld (the disk-budget backstop) must be SURFACED in
    the grouped section, not computed and dropped."""
    _reset()
    _run_batch(project_name="WH Project", locales=("es-ES",))
    with Session(engine) as s:
        a = s.exec(select(TapAction)).all()[-1]
        a.capture_withheld = True
        s.add(a)
        s.commit()
    c = TestClient(app)
    _login(c)
    page = c.get("/prompt-tap").text
    assert "capture withheld" in page, "the capture_withheld flag must render in the section"
    print("  (L2) capture_withheld surfaces in the grouped view OK")


# --- orphan_calls + calls_cap (fixture-realism gaps to close) --------------------
def test_orphan_calls_and_calls_cap():
    """Fixture-realism: a call stamped for an action but NO run (run_id absent) surfaces in
    the action's orphan bucket (nothing silently vanishes); and the per-run display cap
    reports `more` with the true call_count."""
    _reset()
    aid, rid = _mk_bare_action_run()
    for i in range(4):
        _cap_call(aid, rid, "p%d" % i)  # noqa: UP031
    _cap_call(aid, None, "ORPHANPROBE")  # action_id but no run_id -> orphan bucket
    orig = tap_view.CALLS_PER_RUN_CAP
    tap_view.CALLS_PER_RUN_CAP = 2
    try:
        views = tap_view.list_actions()
    finally:
        tap_view.CALLS_PER_RUN_CAP = orig
    v = [x for x in views if x["id"] == aid][0]  # noqa: RUF015
    rv = v["runs"][0]
    assert rv["call_count"] == 4 and len(rv["calls"]) == 2 and rv["more"] == 2, rv
    assert v["orphan_calls"], "an orphan (no-run) call must surface in the orphan bucket"
    assert any(c["request"]["prompt"] == "ORPHANPROBE" for c in v["orphan_calls"]), v[
        "orphan_calls"
    ]
    print("  (ORPHAN/CAP) orphan calls surface; per-run cap reports 'more' with true count OK")


# --- N3: the flat "All calls" section + record download are org-scoped ------------
def _flat_rec(prompt, action_id=None, run_id=None):
    meta = {}
    if action_id:
        meta["action_id"] = action_id
    if run_id:
        meta["run_id"] = run_id
    return prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="t",
        system="s",
        prompt=prompt,
        max_tokens=8,
        payload={"text": "x"},
        outcome="ok",
        response_text="y",
        cost=0.0,
        tap_meta=(meta or None),
    )


def test_n3_flat_section_and_download_org_scoped():
    """N3 (HIGH): the flat "All calls" section (page_snapshot walks legacy + EVERY per-action
    file) and /prompt-tap/download?record= must NOT expose other orgs' raw content under
    active multi-org. Own-org content shows + downloads; other-org and unattributable legacy
    content is hidden in the section and refused (403) by the record download."""
    _reset()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            admin = s.exec(select(User).where(User.email == "admin")).first()
            assert admin is not None, "seeded admin present for the web arm"
            # Deterministic setup: scope the admin to org A explicitly (no conditional
            # sidestep -- the F4 org-NULL case is asserted directly below).
            orgA = Organization(name="OrgA-flat")
            orgB = Organization(name="OrgB-flat")
            s.add(orgA)
            s.add(orgB)
            s.commit()
            s.refresh(orgA)
            s.refresh(orgB)
            admin.org_id = orgA.id
            s.add(admin)
            s.commit()
            org_a, org_b = orgA.id, orgB.id
            aidA, aidB = tap_envelope.new_id(), tap_envelope.new_id()
            ridA, ridB = tap_envelope.new_id(), tap_envelope.new_id()
            s.add(
                TapAction(
                    id=aidA,
                    kind=TapActionKind.WORKFLOW,
                    org_id=org_a,
                    requested_locales=["es-ES"],
                    status=TapStatus.COMPLETED,
                )
            )
            s.add(
                TapAction(
                    id=aidB,
                    kind=TapActionKind.WORKFLOW,
                    org_id=org_b,
                    requested_locales=["fr-FR"],
                    status=TapStatus.COMPLETED,
                )
            )
            s.add(
                TapRun(id=ridA, action_id=aidA, target_locale="es-ES", status=TapStatus.COMPLETED)
            )
            s.add(
                TapRun(id=ridB, action_id=aidB, target_locale="fr-FR", status=TapStatus.COMPLETED)
            )
            s.commit()
        recA = _flat_rec("ALPHAFLATSECRET", aidA, ridA)
        recB = _flat_rec("BRAVOFLATSECRET", aidB, ridB)
        recL = _flat_rec("LEGACYFLATSECRET")  # no action_id -> legacy/unattributable
        for rec in (recA, recB, recL):
            prompt_tap.capture(rec)
        keyA = prompt_tap.record_key(recA)
        keyB = prompt_tap.record_key(recB)
        keyL = prompt_tap.record_key(recL)
        c = TestClient(app)
        _login(c)
        page = c.get("/prompt-tap").text
        assert "ALPHAFLATSECRET" in page, "own-org content must show"
        assert "BRAVOFLATSECRET" not in page, (
            "another org's raw content must NOT render (flat or grouped) -- N3"
        )
        assert "LEGACYFLATSECRET" not in page, (
            "unattributable legacy content must be hidden (fail closed) -- N3"
        )
        assert c.get(f"/prompt-tap/download?record={keyA}").status_code == 200, (
            "own-org record downloads"
        )
        assert c.get(f"/prompt-tap/download?record={keyB}").status_code == 403, (
            "another org's record download must be refused (N3)"
        )
        assert c.get(f"/prompt-tap/download?record={keyL}").status_code == 403, (
            "legacy/unattributable record download must be refused (N3)"
        )
        # F4: an org-NULL caller under ACTIVE multi-org must FAIL CLOSED everywhere (never
        # the whole store), matching the fail-closed docstring. This state is unreachable
        # via a live login (it 401s an org-NULL admin), so force the resolved org to None at
        # the route seam (_my_org_id) and assert the refusal -- download 403, and the page's
        # flat AND grouped sections both hide all content.
        adm = admin_mod()
        _orig = adm._my_org_id
        adm._my_org_id = lambda req: None
        try:
            assert c.get(f"/prompt-tap/download?record={keyA}").status_code == 403, (
                "an org-NULL admin's record download must fail closed (F4)"
            )
            null_page = c.get("/prompt-tap").text
            assert "ALPHAFLATSECRET" not in null_page and "BRAVOFLATSECRET" not in null_page, (
                "an org-NULL admin's flat + grouped sections must fail closed to empty (F4)"
            )
        finally:
            adm._my_org_id = _orig
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  (N3) flat section + record download scoped to caller org; legacy hidden; "
        "org-NULL fails closed (F4) OK"
    )


# --- F3: the flat org filter runs BEFORE the page cap (a loud sibling can't blank us) --
def test_f3_flat_filter_before_page_cap():
    """F3 (MED): the org filter must be applied BEFORE the 200-row page cap. Round 2 filtered
    AFTER the cap, so a loud sibling org's newer records consumed the cap and a quiet org saw
    0 rows + a false "0 records stored". Here a quiet org (5 records) must see its 5 records
    and honest counts while a loud org holds many more, newer records."""
    _reset()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            admin = s.exec(select(User).where(User.email == "admin")).first()
            orgQuiet = Organization(name="OrgQuiet")
            orgLoud = Organization(name="OrgLoud")
            s.add(orgQuiet)
            s.add(orgLoud)
            s.commit()
            s.refresh(orgQuiet)
            s.refresh(orgLoud)
            admin.org_id = orgQuiet.id  # the caller is the QUIET org
            s.add(admin)
            s.commit()
            qid, lid = orgQuiet.id, orgLoud.id
            aQ, aL = tap_envelope.new_id(), tap_envelope.new_id()
            rQ, rL = tap_envelope.new_id(), tap_envelope.new_id()
            s.add(
                TapAction(
                    id=aQ,
                    kind=TapActionKind.WORKFLOW,
                    org_id=qid,
                    requested_locales=["es-ES"],
                    status=TapStatus.COMPLETED,
                )
            )
            s.add(
                TapAction(
                    id=aL,
                    kind=TapActionKind.WORKFLOW,
                    org_id=lid,
                    requested_locales=["fr-FR"],
                    status=TapStatus.COMPLETED,
                )
            )
            s.add(TapRun(id=rQ, action_id=aQ, target_locale="es-ES", status=TapStatus.COMPLETED))
            s.add(TapRun(id=rL, action_id=aL, target_locale="fr-FR", status=TapStatus.COMPLETED))
            s.commit()
        # The QUIET org's 5 records go in FIRST (older); the LOUD org then floods 250 NEWER
        # records, which pre-fix consumed the whole 200-row page cap before the org filter.
        for i in range(5):
            prompt_tap.capture(_flat_rec("QUIETREC%d" % i, aQ, rQ))  # noqa: UP031
        loud_cap = admin_mod().PROMPT_TAP_PAGE_ROWS + 50
        for i in range(loud_cap):
            prompt_tap.capture(_flat_rec("LOUDREC%d" % i, aL, rL))  # noqa: UP031
        c = TestClient(app)
        _login(c)
        page = c.get("/prompt-tap").text
        assert "QUIETREC0" in page and "QUIETREC4" in page, (
            "the quiet org must see ALL its records despite the loud sibling (F3)"
        )
        assert "LOUDREC0" not in page, "the loud org's content must not render for the quiet org"
        # Honest counts: the Capture card must read "5 records stored", not a false "0".
        assert "5 records stored" in page, (
            "the quiet org's count must be honest (5), not the pre-cap-consumed 0 (F3)"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  (F3) flat org filter runs pre-cap; quiet org sees its records + honest counts OK")


def admin_mod():
    from app.web import admin as _a

    return _a


# --- N6: record keys are computed only for post-cap survivors ---------------------
def test_n6_record_keys_only_for_survivors():
    """N6 (MEDIUM): _record_key must be hashed only for the SURVIVING (post-cap) records,
    not every record the streaming loop sees and the deque discards (round 1 hashed all)."""
    _reset()
    aid, rid = _mk_bare_action_run()
    for i in range(7):
        _cap_call(aid, rid, "p%d" % i)  # noqa: UP031
    from app.services import prompt_tap as pt

    calls = {"n": 0}
    orig = pt.record_key
    pt.record_key = lambda rec: calls.__setitem__("n", calls["n"] + 1) or orig(rec)
    try:
        out, counts = tap_view._calls_by_run(aid, cap=3)
    finally:
        pt.record_key = orig
    assert len(out[rid]) == 3 and counts[rid] == 7, (len(out[rid]), counts[rid])
    assert calls["n"] == 3, (
        "record_key must hash only the 3 survivors, not all 7 (N6)",
        calls["n"],
    )
    assert all(pt.is_record_key(r["_record_key"]) for r in out[rid]), out[rid]
    print("  (N6) record keys computed only for post-cap survivors OK")


def main():
    init_db()
    security.seed_initial()  # admin/admin for the TestClient web arm
    test_page_renders_grouped()
    test_empty_state()
    test_action_filters()
    test_flat_section_reachable()
    test_admin_only()
    test_cli_actions_and_show_action()
    test_h2_grouped_view_org_scoped()
    test_m3_grouped_call_download_link_resolves()
    test_l1_calls_capped_while_streaming()
    test_l2_capture_withheld_renders()
    test_orphan_calls_and_calls_cap()
    test_n3_flat_section_and_download_org_scoped()
    test_f3_flat_filter_before_page_cap()
    test_n6_record_keys_only_for_survivors()
    print("TAP VIEWER OK")


if __name__ == "__main__":
    main()
