"""Batch 15 Train 1 -- the CONFIGURABLE tm_match step (SPEC-tm-match-step-config).

Verifies the per-step WorkflowStep policy columns (lock_ice / hundreds_enter_lqa / pool
selection) + the durable WorkflowRun.used_tm_match marker + the exact-only engine behaviour
(ADR 0102: TM fills exact matches only, a near match never fills, heal is exact) + the
four-surface authoring ride + the clear refusal of the REMOVED parameters
(tm_fill_threshold, tm_fuzzy_lock_mode, tm_context_floor). DISTINCT from the pre-existing
verify_tm_match_step.py.

Covers the LAWs: TM-1 (config on the step), TM-3 (byte-identical upgrade), TM-8
(snapshot seeding), TM-9 (pool selection narrow-only), TM-10 (dead on non-tm_match),
TM-12 (resume short-3 back-compat), TM-13/N2/P2 (durable marker, both branches), TM-20
(heal exactness). The fuzzy-fill LAWs (TM-5/6/7/19) were retired with fuzzy matching.

HERMETIC: throwaway LOC_TMS_DATA_DIR; MockProvider (keyless); ASCII-only.
Final line: "TM MATCH STEP CONFIG OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmcfg_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    SegmentTM,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, settings, tm, tm_lock
from app.services import workflows as wf_svc

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _mk_project(s, name, src="en-US"):
    p = Project(name=name, source_locale=src, default_provider="mock")
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _mk_doc(s, project_id, locale, sources):
    doc = Document(
        project_id=project_id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, src in enumerate(sources):
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=locale,
                order_index=i,
                source_text=src,
                target_text="",
                status=SegStatus.NEW,
            )
        )
    s.add(TargetLocale(project_id=project_id, locale=locale))
    s.commit()
    return doc


def _mk_tm_workflow(s, tm_config=None, downstream=None):
    """A workflow whose step 0 is a tm_match step carrying `tm_config` (a dict of the six
    columns), optionally followed by `downstream` steps [(step_type, tm_protect), ...]."""
    wf = Workflow(name="TMCfgWF")
    s.add(wf)
    s.commit()
    s.refresh(wf)
    step = WorkflowStep(
        workflow_id=wf.id, order_index=0, name="TM match", step_type=StepType.TM_MATCH
    )
    for k, v in (tm_config or {}).items():
        setattr(step, k, v)
    s.add(step)
    for i, (stype, tmp) in enumerate(downstream or [], start=1):
        d = WorkflowStep(workflow_id=wf.id, order_index=i, name=stype, step_type=stype)
        if tmp is not None:
            d.tm_protect = tmp
        s.add(d)
    s.commit()
    return wf


def _seg(s, doc_id, order):
    return s.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.order_index == order)
    ).one()


# ============================================================================
# (1) durable marker + backfill (LAW-TM-13 / N2 / P2)
# ============================================================================
def test_used_tm_match_marker_default_and_backfill():
    with Session(engine) as s:
        # A plain (no-tm_match) WorkflowRun row defaults used_tm_match False (the
        # server_default '0' / model default) -- so a legacy/pre-Batch-15 run is NOT
        # retroactively gated.
        wf = Workflow(name="Plain")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        doc = _mk_doc(s, _mk_project(s, "MarkerBackfill").id, "fr-FR", ["x"])
        run = WorkflowRun(workflow_id=wf.id, document_id=doc.id, target_locale="fr-FR")
        s.add(run)
        s.commit()
        s.refresh(run)
        if run.used_tm_match is not False:
            _fail(
                f"a fresh WorkflowRun must default used_tm_match False, got {run.used_tm_match!r}"
            )
    print("  marker: WorkflowRun.used_tm_match defaults False (legacy backfill safe): OK")


def test_used_tm_match_stamped_segmented_and_pure_llm():
    with Session(engine) as s:
        p = _mk_project(s, "MarkerSeg")
        tm.write_back(s, p.id, "Save.", "Enregistrer.", "en-US", "fr-FR")
        # A run that EXECUTES a tm_match step stamps the marker True (even a segmented run
        # that fills nothing does -- the presence of the step is the fact).
        doc = _mk_doc(s, p.id, "fr-FR", ["Save.", "Fresh."])
        wf = _mk_tm_workflow(s, downstream=[(StepType.LLM_AGENT, None)])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "completed", summ
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc.id)).one()
        if run.used_tm_match is not True:
            _fail("a segmented tm_match run must stamp used_tm_match True")
        # A PURE-LLM run (no tm_match step) leaves it False.
        p2 = _mk_project(s, "MarkerPureLLM")
        doc2 = _mk_doc(s, p2.id, "fr-FR", ["Only MT."])
        wf2 = Workflow(name="PureLLM")
        s.add(wf2)
        s.commit()
        s.refresh(wf2)
        s.add(
            WorkflowStep(workflow_id=wf2.id, order_index=0, name="MT", step_type=StepType.LLM_AGENT)
        )
        s.commit()
        orchestrator.run_workflow(s, wf2.id, doc2.id, "fr-FR")
        run2 = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc2.id)).one()
        if run2.used_tm_match is not False:
            _fail("a pure-LLM run must leave used_tm_match False")
    print("  marker: stamped by a segmented tm_match run; False for a pure-LLM run: OK")


# ============================================================================
# (2) effective policy resolution (LAW-TM-1 / TM-3) + seeding (LAW-TM-8)
# ============================================================================
def test_effective_policy_resolution():
    with Session(engine) as s:
        settings.set_many({"tm_lock_ice": "1", "tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "PolicyResolve")
        # A NULL-override step (every column None) resolves the LIVE global exactly.
        null_step = WorkflowStep(workflow_id=1, order_index=0, step_type=StepType.TM_MATCH)
        pol = tm_lock.resolve_policy_for_step(s, p, null_step)
        assert (pol.lock_ice, pol.hundreds_enter_lqa) == (True, True), pol
        # A concrete per-step override wins over the global.
        over = WorkflowStep(
            workflow_id=1,
            order_index=0,
            step_type=StepType.TM_MATCH,
            tm_lock_ice=False,
            tm_hundreds_enter_lqa=False,
        )
        pol2 = tm_lock.resolve_policy_for_step(s, p, over)
        assert pol2.lock_ice is False and pol2.hundreds_enter_lqa is False, pol2
        # ADR 0102: the removed per-step fields are gone from the model (the database columns
        # stay, unread), and the removed global settings are gone from the registry.
        for gone in ("tm_fill_threshold", "tm_fuzzy_lock_mode", "tm_context_floor"):
            assert not hasattr(WorkflowStep, gone), f"WorkflowStep.{gone} must be removed"
            assert gone not in settings.DEFAULTS, f"setting {gone} must be removed"
    print("  policy: NULL->global, override wins; removed fields/settings are gone (ADR 0102): OK")


def test_stored_removed_setting_is_inert():
    # ADR 0102: an install that still has appsetting rows for the removed keys must not
    # crash anything that reads settings (the rows are simply never read).
    with Session(engine) as s:
        settings.set_many({"tm_fill_threshold": "80", "tm_fuzzy_lock_mode": "none"})
        try:
            p = _mk_project(s, "InertSettings")
            tm.write_back(s, p.id, "Save.", "Enregistrer.", "en-US", "fr-FR")
            doc = _mk_doc(s, p.id, "fr-FR", ["Save.", "Save the files."])
            wf = _mk_tm_workflow(s)
            summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            assert summ["status"] == "completed", summ
            s.expire_all()
            near = _seg(s, doc.id, 1)
            assert near.lock_band is None and near.tm_match_pct is None, (
                "a stored tm_fill_threshold row must NOT re-enable fuzzy filling",
                (near.lock_band, near.tm_match_pct),
            )
        finally:
            settings.set_many({"tm_fill_threshold": "", "tm_fuzzy_lock_mode": ""})
    print("  settings: stored rows for the removed keys are inert (no crash, no fuzzy fill): OK")


def test_seeding_snapshots_globals():
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "0", "tm_lock_ice": "0"})
        try:
            wf = Workflow(name="SeedWF")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            step = wf_svc.add_step(s, wf.id, name="TM", step_type=StepType.TM_MATCH)
            # The lock scalars are seeded from the CURRENT globals (concrete snapshot);
            # the pool selection is NEVER snapshotted (S2 -> None = all readable).
            assert step.tm_hundreds_enter_lqa is False, step.tm_hundreds_enter_lqa
            assert step.tm_lock_ice is False, step.tm_lock_ice
            assert step.tm_pool_selection is None, step.tm_pool_selection
            # A later global change does NOT mutate the already-created step (LAW-TM-8).
            settings.set_many({"tm_hundreds_enter_lqa": "1"})
            s.refresh(step)
            assert step.tm_hundreds_enter_lqa is False, "a snapshot must not track a global change"
            # A NON-tm_match step never seeds -> stays None.
            llm = wf_svc.add_step(s, wf.id, name="MT", step_type=StepType.LLM_AGENT)
            assert llm.tm_lock_ice is None and llm.tm_hundreds_enter_lqa is None, llm
        finally:
            settings.set_many({"tm_hundreds_enter_lqa": "1", "tm_lock_ice": "1"})
    print("  seeding: new tm_match step snapshots globals (frozen); non-tm_match stays NULL: OK")


# ============================================================================
# (3) exact-only fill (ADR 0102): a near match NEVER fills
# ============================================================================
def test_near_match_never_fills():
    with Session(engine) as s:
        p = _mk_project(s, "ExactOnly")
        tm.write_back(s, p.id, "Save the file.", "Enregistrer le fichier.", "en-US", "fr-FR")
        tm.write_back(
            s,
            p.id,
            "Please save the current document now.",
            "Veuillez enregistrer.",
            "en-US",
            "fr-FR",
        )
        # Two near (not exact) sources: neither fills, whatever the step is configured with.
        doc = _mk_doc(
            s, p.id, "fr-FR", ["Save the files.", "Please save the current document today."]
        )
        wf = _mk_tm_workflow(s)
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        for i in (0, 1):
            seg = _seg(s, doc.id, i)
            assert seg.lock_band is None and seg.tm_match_pct is None, (
                i,
                seg.lock_band,
                seg.tm_match_pct,
            )
            assert seg.target_text not in ("Enregistrer le fichier.", "Veuillez enregistrer."), (
                "a near match must NOT fill (TM is exact-only, ADR 0102)",
                seg.target_text,
            )
    print("  exact-only: near matches (one word off) never fill, no pct, no lock: OK")


def test_exact_fills_and_stamps_100():
    with Session(engine) as s:
        p = _mk_project(s, "ExactFill")
        tm.write_back(s, p.id, "Save the file.", "Enregistrer le fichier.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save the file.", "Something else entirely."])
        wf = _mk_tm_workflow(s, downstream=[(StepType.LLM_AGENT, None)])
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        hit = _seg(s, doc.id, 0)
        assert hit.target_text == "Enregistrer le fichier.", hit.target_text
        assert hit.tm_match_pct is not None and hit.tm_match_pct >= 100, hit.tm_match_pct
        assert hit.lock_band in ("100", "ice"), hit.lock_band
        miss = _seg(s, doc.id, 1)
        assert miss.lock_band is None and miss.tm_match_pct is None, miss
    print("  exact-only: an exact hit fills, stamps >=100 and locks; a miss stays MT: OK")


def test_heal_leaves_drifted_mt_alone_without_exact_hit():
    # LAW-TM-20: the heal path is exact only. A drifted-MT segment whose only TM entry is a
    # NEAR match is left for the downstream translate step (it is not healed or locked).
    with Session(engine) as s:
        p = _mk_project(s, "HealNear")
        tm.write_back(
            s,
            p.id,
            "Please save the current document now.",
            "Veuillez enregistrer maintenant.",
            "en-US",
            "fr-FR",
        )
        doc = _mk_doc(
            s,
            p.id,
            "fr-FR",
            ["Please save the current document today.", "Please save the current document today."],
        )
        seg1 = _seg(s, doc.id, 1)
        seg1.target_text = "DRIFTED MT TEXT"
        seg1.status = SegStatus.MT
        s.add(seg1)
        s.commit()
        wf = _mk_tm_workflow(s, downstream=[(StepType.LLM_AGENT, False)])
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        for i in (0, 1):
            seg = _seg(s, doc.id, i)
            assert seg.lock_band is None, ("a near hit must not fill/heal/lock", i, seg.lock_band)
            assert seg.target_text != "Veuillez enregistrer maintenant.", (i, seg.target_text)
    print("  heal: a near hit never fills, heals or locks a segment (LAW-TM-20): OK")


# ============================================================================
# (5) TM_LOCKS_ENABLED off = whole no-op (finding 11)
# ============================================================================
def test_tm_locks_disabled_whole_noop():
    with Session(engine) as s:
        p = _mk_project(s, "LocksOff")
        tm.write_back(s, p.id, "Save.", "Enregistrer.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save."])
        wf = _mk_tm_workflow(s)
        _prev = config.TM_LOCKS_ENABLED
        config.TM_LOCKS_ENABLED = False
        try:
            orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        finally:
            config.TM_LOCKS_ENABLED = _prev
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # No TM fill / no lock (the source-in-place no-op is unchanged behaviour).
        assert seg.lock_band is None and seg.tm_match_pct is None, (seg.lock_band, seg.tm_match_pct)
        assert seg.target_text != "Enregistrer.", (
            "TM_LOCKS_ENABLED off: the tm_match step fills nothing",
            seg.target_text,
        )
    print("  TM_LOCKS_ENABLED off: the whole tm_match step is a no-op (fills nothing): OK")


# ============================================================================
# (6) per-step TM pool selection (LAW-TM-9)
# ============================================================================
def test_pool_selection_narrows_and_provenance():
    with Session(engine) as s:
        p = _mk_project(s, "PoolSel")
        # own-project exact entry, carrying an id used for bump_usage.
        tm.write_back(s, p.id, "Save.", "Enregistrer.", "en-US", "fr-FR")
        entry = s.exec(select(SegmentTM).where(SegmentTM.project_id == p.id)).one()
        # NULL selection = all readable -> the own-project hit is found (restrict None).
        hits_all = tm.lookup_batch(s, p.id, ["Save."], "en-US", "fr-FR")
        assert hits_all["Save."] and hits_all["Save."]["match_pct"] >= 100, hits_all
        assert hits_all["Save."].get("id") == entry.id, hits_all
        # A selection naming ONLY the own project still finds it (narrow to {own}).
        hits_own = tm.lookup_batch(
            s, p.id, ["Save."], "en-US", "fr-FR", restrict_project_ids={p.id}
        )
        assert hits_own["Save."] is not None, hits_own
        # A selection resolving to the EMPTY set (a foreign/unreadable id only) matches
        # NOTHING (fail-closed), distinct from None=all-readable.
        hits_empty = tm.lookup_batch(
            s, p.id, ["Save."], "en-US", "fr-FR", restrict_project_ids=set()
        )
        assert hits_empty["Save."] is None, ("an empty selection must match nothing", hits_empty)
        hits_foreign = tm.lookup_batch(
            s, p.id, ["Save."], "en-US", "fr-FR", restrict_project_ids={999999}
        )
        assert hits_foreign["Save."] is None, (
            "an unreadable-only selection matches nothing",
            hits_foreign,
        )
    print("  pool selection: NULL=all, subset narrows, empty/foreign match nothing; hit id: OK")


def test_pool_selection_run_drops_unreadable_and_notes():
    with Session(engine) as s:
        p = _mk_project(s, "PoolRun")
        tm.write_back(s, p.id, "Save.", "Enregistrer.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save."])
        # A selection naming an unreadable pool 999999 (dropped) + the own project (kept):
        wf = _mk_tm_workflow(s, tm_config={"tm_pool_selection": [p.id, 999999]})
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "Enregistrer.", (
            "the own-project pool is still read",
            seg.target_text,
        )
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc.id)).one()
        assert run.notes and "999999" in run.notes and "not readable" in run.notes, (
            "an unreadable pool id is dropped + surfaced as a run note",
            run.notes,
        )
    print("  pool selection: an unreadable id dropped + noted at run time; readable pool read: OK")


def test_empty_pool_selection_roundtrip_and_note():
    # M4 + L2: a NON-NULL empty selection [] is authorable, distinct from NULL, round-trips
    # via the web sentinel "(none)", renders as "nothing selected", and stamps a run note.
    from app.services import package_view as pv

    # the coercer: "(none)" -> [] (empty selection, matches nothing), blank/"auto" -> None.
    assert wf_svc._coerce_tm_pool_selection("(none)") == []
    assert wf_svc._coerce_tm_pool_selection("") is None
    assert wf_svc._coerce_tm_pool_selection([]) == []
    assert wf_svc._coerce_tm_pool_selection("auto") is None
    # N3: ids go through ids.coerce_id, so 0 / negative are DROPPED at authoring time.
    assert (
        wf_svc._coerce_tm_pool_selection("0") == [] and wf_svc._coerce_tm_pool_selection("-5") == []
    )
    assert wf_svc._coerce_tm_pool_selection("1,0,2") == [1, 2]
    # N4: the seam validator makes the "(none)" sentinel LOAD-BEARING -- an empty selection
    # is accepted ONLY via "(none)" (or an explicit []), while a non-empty input that yields
    # ZERO valid ids is REFUSED (a typo must not silently collapse all-readable -> nothing).
    assert wf_svc._require_valid_tm_pool_selection("(none)") == []
    assert wf_svc._require_valid_tm_pool_selection([]) == []
    assert wf_svc._require_valid_tm_pool_selection("1,2") == [1, 2]
    assert wf_svc._require_valid_tm_pool_selection(None) is None
    assert wf_svc._require_valid_tm_pool_selection("auto") is None
    for bad in ("garbage", "abc,def", "1|2", "0", "-5"):
        try:
            wf_svc._require_valid_tm_pool_selection(bad)
            _fail(f"an all-invalid pool selection {bad!r} must be REFUSED (N4)")
        except wf_svc.WorkflowError:
            pass
    with Session(engine) as s:
        wf = Workflow(name="EmptyPool")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        step = wf_svc.add_step(s, wf.id, name="tm", step_type=StepType.TM_MATCH)
        # set [] via the service (as /api/MCP would); package_view must NOT say "all readable".
        wf_svc.update_step(s, step.id, tm_pool_selection=[])
        s.refresh(step)
        assert step.tm_pool_selection == [], step.tm_pool_selection
        blob = str(pv._tm_match_view(step))
        assert "nothing selected" in blob, (
            "[] must render 'nothing selected', not all-readable",
            blob,
        )
        # L2: a run over an empty selection reads NO pool and stamps a note.
        p = _mk_project(s, "EmptyPoolRun")
        tm.write_back(s, p.id, "Save.", "Enregistrer.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save."])
        wf2 = _mk_tm_workflow(s, tm_config={"tm_pool_selection": []})
        orchestrator.run_workflow(s, wf2.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text != "Enregistrer.", (
            "an empty selection must read NO pool",
            seg.target_text,
        )
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc.id)).one()
        assert run.notes and "empty" in run.notes.lower(), (
            "an empty pool selection must be surfaced as a run note (L2)",
            run.notes,
        )
    print("  pool selection: [] distinct from NULL -- round-trips, renders, noted (M4/L2): OK")


# ============================================================================
# (7) dead fields on non-tm_match types (LAW-TM-10) + type-switch clears
# ============================================================================
def test_dead_fields_refused_and_type_switch_clears():
    with Session(engine) as s:
        wf = Workflow(name="DeadWF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        # a config field on a non-tm_match step is refused.
        for stype in (StepType.LLM_AGENT, StepType.LQA, StepType.HUMAN_REVIEW, StepType.EXTERNAL):
            try:
                wf_svc.add_step(s, wf.id, name="bad", step_type=stype, tm_lock_ice="off")
                _fail(f"tm_lock_ice on a {stype} step must be refused")
            except wf_svc.WorkflowError:
                pass
        # a tm_match step carrying config, switched to llm_agent, force-clears the config to
        # NULL (nullable -> no IntegrityError; SPEC section 8.A).
        step = wf_svc.add_step(
            s,
            wf.id,
            name="tm",
            step_type=StepType.TM_MATCH,
            tm_lock_ice="off",
            tm_pool_selection="1,2",
        )
        assert step.tm_lock_ice is False and step.tm_pool_selection == [1, 2], step
        wf_svc.update_step(s, step.id, step_type=StepType.LLM_AGENT)
        s.refresh(step)
        assert step.step_type == StepType.LLM_AGENT
        assert step.tm_lock_ice is None and step.tm_pool_selection is None, (
            "a type switch away from tm_match must NULL the tm config columns",
            (step.tm_lock_ice, step.tm_pool_selection),
        )
    print("  dead fields: refused on non-tm_match; a type switch clears them to NULL: OK")


# ============================================================================
# (7b) ADR 0102: the removed parameters are refused with a clear message
# ============================================================================
def test_removed_params_refused_with_clear_message():
    with Session(engine) as s:
        wf = Workflow(name="RemovedParams")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for kw, name in (
            ({"tm_fill_threshold": 85}, "tm_fill_threshold"),
            ({"tm_fill_threshold": "85%"}, "tm_fill_threshold"),
            ({"tm_fuzzy_lock_mode": "soft"}, "tm_fuzzy_lock_mode"),
            ({"tm_context_floor": 90}, "tm_context_floor"),
        ):
            try:
                wf_svc.add_step(s, wf.id, name="tm", step_type=StepType.TM_MATCH, **kw)
                _fail(f"add_step must refuse {kw!r} (ADR 0102)")
            except wf_svc.WorkflowError as exc:
                assert f"{name} was removed" in str(exc) and "ADR 0102" in str(exc), str(exc)
        # A threshold of 100 (and blank / None) is an accepted no-op: nothing is stored.
        ok = wf_svc.add_step(
            s, wf.id, name="tm100", step_type=StepType.TM_MATCH, tm_fill_threshold=100
        )
        assert not hasattr(ok, "tm_fill_threshold"), "the field is gone from the model"
        wf_svc.add_step(
            s,
            wf.id,
            name="tmblank",
            step_type=StepType.TM_MATCH,
            tm_fill_threshold="",
            tm_fuzzy_lock_mode=None,
            tm_context_floor="",
        )
        # update_step: the same rules, and a refused edit changes nothing.
        for kw, name in (
            ({"tm_fill_threshold": 85}, "tm_fill_threshold"),
            ({"tm_fuzzy_lock_mode": "none"}, "tm_fuzzy_lock_mode"),
            ({"tm_context_floor": 70}, "tm_context_floor"),
            ({"tm_fuzzy_lock_mode": "auto"}, "tm_fuzzy_lock_mode"),
        ):
            try:
                wf_svc.update_step(s, ok.id, **kw)
                _fail(f"update_step must refuse {kw!r} (ADR 0102)")
            except wf_svc.WorkflowError as exc:
                assert f"{name} was removed" in str(exc), str(exc)
        wf_svc.update_step(s, ok.id, tm_fill_threshold=100, tm_fuzzy_lock_mode=None)
        wf_svc.update_step(s, ok.id, tm_fill_threshold="", tm_lock_ice="off")
        s.refresh(ok)
        assert ok.tm_lock_ice is False, ok
    print("  removed params: refused with a clear ADR 0102 message; 100 / blank are no-ops: OK")


def test_removed_params_clear_error_on_api_and_cli():
    # The API answers 400 (the repo's standard {"ok": false, "error": ...} JSON); the CLI
    # exits non-zero with the same message. A hidden legacy flag must NOT crash argparse.
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    with Session(engine) as s:
        wf = Workflow(name="RemovedParamsSurfaces")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    u = security.create_user("tmcfg@x.com", "TM", "admin", "tmcfgpass123456")
    _row, tok = apikeys.create_key(u.id, "tmcfg", scopes="api docs")
    c = TestClient(app, headers={"X-Loc-Token": tok})
    r = c.post(
        f"/api/workflows/{wid}/steps",
        json={"name": "tm", "step_type": "tm_match", "tm_fuzzy_lock_mode": "soft"},
    )
    assert r.status_code == 400, (r.status_code, r.text)
    body = r.json()
    assert body.get("ok") is False and "tm_fuzzy_lock_mode was removed" in body["error"], body
    r = c.post(
        f"/api/workflows/{wid}/steps",
        json={"name": "tm", "step_type": "tm_match", "tm_fill_threshold": 100},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    sid = r.json()["id"]
    for field, val in (("tm_fill_threshold", 90), ("tm_context_floor", 80)):
        r = c.post(f"/api/workflows/{wid}/steps/{sid}/edit", json={field: val})
        assert r.status_code == 400 and f"{field} was removed" in r.json()["error"], (
            r.status_code,
            r.text,
        )
    r = c.post(f"/api/workflows/{wid}/steps/{sid}/edit", json={"tm_fill_threshold": None})
    assert r.status_code == 200, (r.status_code, r.text)
    settings.set_many({"security_auth_required": "0"})
    # CLI: the flags are hidden from --help but fail with the clear message, not "unrecognized".
    code, _out, err = run_cli(
        ["workflow", "add-step", str(wid), "--type", "tm_match", "--fill-threshold", "85"]
    )
    assert code != 0 and "tm_fill_threshold was removed" in (err + _out), (code, err, _out)
    code, _out, err = run_cli(
        ["workflow", "add-step", str(wid), "--type", "tm_match", "--fuzzy-lock-mode", "soft"]
    )
    assert code != 0 and "tm_fuzzy_lock_mode was removed" in (err + _out), (code, err, _out)
    code, _out, err = run_cli(
        ["workflow", "edit-step", str(wid), str(sid), "--context-floor", "70"]
    )
    assert code != 0 and "tm_context_floor was removed" in (err + _out), (code, err, _out)
    code, _out, err = run_cli(
        ["workflow", "add-step", str(wid), "--type", "tm_match", "--fill-threshold", "100"]
    )
    assert code == 0, (code, err, _out)
    code, hlp, _e = run_cli(["workflow", "add-step", "--help"])
    for gone in ("--fill-threshold", "--fuzzy-lock-mode", "--context-floor"):
        assert gone not in hlp, f"{gone} must not be advertised in --help"
    assert "--lock-ice" in hlp and "--tm-pools" in hlp, "the surviving TM flags stay"
    print(
        "  surfaces: API 400 + CLI non-zero with the ADR 0102 message; flags hidden from help: OK"
    )


# ============================================================================
# (8) four-surface round-trip (LAW-TM-11) -- api show + mcp edit_workflow field set
# ============================================================================
_TM_CFG_KEYS = ("tm_lock_ice", "tm_hundreds_enter_lqa", "tm_pool_selection")


def test_cli_show_row_and_package_view_carry_config():
    # L3: assert the config keys land on the REAL CLI `workflow show --json` step row (not just
    # _tm_match_view + the coercer) -- the CLI<->/api parity guards assert row-EQUALITY, so a
    # field missing from BOTH rows would pass them; this pins presence. package_view renders
    # the EFFECTIVE config too (section 8.F).
    import json

    from app.services import package_view as pv
    from tests._cli_runner import run_cli

    with Session(engine) as s:
        wf = Workflow(name="ShowWF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
        step = wf_svc.add_step(
            s,
            wid,
            name="tm",
            step_type=StepType.TM_MATCH,
            tm_lock_ice="off",
            tm_pool_selection="1,2",
        )
        assert step.tm_pool_selection == [1, 2], step.tm_pool_selection
        blob = str(pv._tm_match_view(step))
        assert "lock_ice False" in blob, ("package view must render the config", blob)
        assert "1, 2" in blob, blob
        assert "fuzzy" not in blob.lower() and "threshold" not in blob.lower(), blob
    code, out, _err = run_cli(["--json", "workflow", "show", str(wid)])
    assert code == 0, (code, _err)
    payload = json.loads(out)
    steps = payload.get("steps") or []  # the structured step rows (not "shape", a text sketch)
    assert steps and isinstance(steps[0], dict), (
        "workflow show --json returned no step rows",
        payload,
    )
    row = steps[0]
    for k in _TM_CFG_KEYS:
        assert k in row, f"CLI `workflow show --json` step row is missing {k!r}"
    for gone in ("tm_fill_threshold", "tm_fuzzy_lock_mode", "tm_context_floor"):
        assert gone not in row, f"CLI show row must not carry the removed {gone!r}"
    assert row["tm_lock_ice"] is False, row
    assert row["tm_pool_selection"] == [1, 2], row["tm_pool_selection"]
    print("  surfaces: CLI show --json row carries the config keys; package_view renders them: OK")


def test_mcp_field_allowlist_carries_config():
    # SPEC section 8.E: the live fields are in BOTH MCP step-field allowlists (create + edit), so
    # create_workflow / edit_workflow forward them generically instead of refusing them as
    # unknown. ADR 0102: the three removed names stay on the allowlists ONLY so the server
    # can answer them with the clear "was removed" error.
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    for f in _TM_CFG_KEYS:
        assert f in mcp_server._WORKFLOW_STEP_FIELDS, f"{f} missing from MCP add allowlist"
        assert f in mcp_server._WORKFLOW_EDIT_FIELDS, f"{f} missing from MCP edit allowlist"
    print("  surfaces: the live config fields are in both MCP step-field allowlists (8.E): OK")


def test_short3_restore_backcompat():
    # M1: a run PAUSED under pre-Batch-15 code serialized a 3-element tm_lock_plan and no
    # tm_fill_pct. The restore must treat a short (3-element) entry as match_pct=100 (band
    # "100") -- a cross-deploy resume must never IndexError or mis-stamp. MUTATION-PROVEN:
    # removing the `len(_pv) >= 4` guard crashes this resume.
    with Session(engine) as s:
        p = _mk_project(s, "Short3Restore")
        tm.write_back(s, p.id, "Save now.", "Enregistrer.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save now.", "Free line."])
        wf = _mk_tm_workflow(s, downstream=[(StepType.HUMAN_REVIEW, None)])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        run = s.get(WorkflowRun, summ["run_id"])
        ctx = dict(run.context or {})
        # DOWNGRADE the persisted context to the pre-upgrade shape: 3-element lock-plan
        # entries + NO tm_fill_pct key.
        plan = ctx.get("tm_lock_plan") or {}
        ctx["tm_lock_plan"] = {k: [v[0], v[1], v[2]] for k, v in plan.items()}
        ctx.pop("tm_fill_pct", None)
        run.context = ctx
        s.add(run)
        s.commit()
        # resume MUST NOT crash and MUST stamp pct 100 / band 100 for the exact fill.
        orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", summ["paused_text"])
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "Enregistrer.", seg.target_text
        assert seg.lock_band == "100" and seg.tm_match_pct == 100, (seg.lock_band, seg.tm_match_pct)
    print("  resume: a short (3-element) pre-upgrade lock-plan restores as pct 100 (M1): OK")


def test_builder_forms_carry_every_tm_field():
    # M6(b) / section 8.B: BOTH the add-form and the per-step edit-form in the workflow
    # builder must carry every name="tm_*" config input (a field on one form but not the
    # other is an authoring hole). ADR 0102: the removed inputs are on NEITHER form.
    tmpl = os.path.join(_ROOT, "app", "templates", "workflow_builder.html")
    with open(tmpl, encoding="utf-8") as fh:
        html = fh.read()
    # split at the ADD form action so we can count occurrences per form region.
    marker = '/steps" class="form"'
    assert marker in html, "could not locate the add-step form in workflow_builder.html"
    edit_region, add_region = html.split(marker, 1)
    for nm in _TM_CFG_KEYS:
        needle = f'name="{nm}"'
        assert needle in edit_region, f"the EDIT form is missing {nm}"
        assert needle in add_region, f"the ADD form is missing {nm}"
    for gone in ("tm_fill_threshold", "tm_fuzzy_lock_mode", "tm_context_floor"):
        assert gone not in html, f"workflow_builder.html must not carry the removed {gone}"
    print("  surfaces: both builder forms carry every live tm_* input, none of the removed: OK")


def test_web_roundtrip_config():
    # M6(b) web round-trip: a config set via the service reads back through update_step
    # (present-value / present-blank clears to inherit), the same contract the web edit form
    # POSTs. Confirms an override survives an edit and a blank clears it.
    with Session(engine) as s:
        wf = Workflow(name="WebRT")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        step = wf_svc.add_step(s, wf.id, name="tm", step_type=StepType.TM_MATCH)
        wf_svc.update_step(s, step.id, tm_hundreds_enter_lqa="off", tm_lock_ice="off")
        s.refresh(step)
        assert step.tm_hundreds_enter_lqa is False and step.tm_lock_ice is False, step
        # a blank/"auto" clears back to inherit (NULL).
        wf_svc.update_step(s, step.id, tm_hundreds_enter_lqa="", tm_lock_ice="auto")
        s.refresh(step)
        assert step.tm_hundreds_enter_lqa is None and step.tm_lock_ice is None, (
            "a blank / 'auto' edit must clear the override back to inherit",
            (step.tm_hundreds_enter_lqa, step.tm_lock_ice),
        )
    print("  surfaces: update_step round-trip -- override sets, blank/auto clears to inherit: OK")


def test_key_files_legacy_fuzzy_lock_round_trips():
    # M3: a STORED legacy "fuzzy" lock (written before ADR 0102 removed fuzzy matching) still
    # survives the key transport round-trip (it maps to XLIFF fuzzy-match), so old data
    # neither crashes nor is dropped on export / re-import.
    from types import SimpleNamespace

    from app.services import key_files

    seg = SimpleNamespace(lock_band="fuzzy", lock_hard=False)
    lk = key_files._lock_transport(seg)
    assert lk == {"state": "fuzzy-match"}, lk
    back = key_files._lock_from_transport(lk)
    assert back == ("fuzzy", False), ("a legacy fuzzy lock must round-trip, not be dropped", back)
    # a hard fuzzy lock carries translate=no and still round-trips.
    seg_h = SimpleNamespace(lock_band="fuzzy", lock_hard=True)
    lkh = key_files._lock_transport(seg_h)
    assert lkh == {"state": "fuzzy-match", "translate": "no"}, lkh
    assert key_files._lock_from_transport(lkh) == ("fuzzy", True), key_files._lock_from_transport(
        lkh
    )
    print("  key transport: a stored legacy fuzzy lock round-trips (XLIFF fuzzy-match): OK")


def test_junk_scalar_helper_still_validates():
    # N2: the generic whole-number seam helper stays (verify_numeric_input_bounds pins it);
    # the tm_match fields that used it are removed, so only the helper itself is checked.
    assert wf_svc._require_valid_tm_scalar("85", "x") == 85
    assert wf_svc._require_valid_tm_scalar("auto", "x") is None
    assert wf_svc._require_valid_tm_scalar("", "x") is None
    assert wf_svc._require_valid_tm_scalar(None, "x") is None
    for bad in ("85%", "abc", "8O", "1.5"):
        try:
            wf_svc._require_valid_tm_scalar(bad, "x")
            _fail(f"a junk scalar {bad!r} must be REFUSED (N2)")
        except wf_svc.WorkflowError:
            pass
    print("  scalar helper: junk REFUSED, auto/blank -> inherit (N2): OK")


def test_lock_badge_templates_exact_only():
    # ADR 0102 / A26-A27: the editor, document page and key page show exact (100 / 101) and
    # semantic badges only. A stored legacy lock_band "fuzzy" renders NO TM lock badge (the
    # segment reads as plain machine translation) and the Fuzzy hint is gone.
    _tpl = os.path.join(_ROOT, "app", "templates")
    with open(os.path.join(_tpl, "editor.html"), encoding="utf-8") as _fh:
        editor = _fh.read()
    with open(os.path.join(_tpl, "document.html"), encoding="utf-8") as _fh:
        document = _fh.read()
    with open(os.path.join(_tpl, "project_keys.html"), encoding="utf-8") as _fh:
        pkeys = _fh.read()
    assert "fuzzy-hint" not in editor and 'id="fz-' not in editor, "the Fuzzy hint badge is gone"
    assert "Fuzzy {{" not in editor and "fuzzy TM match" not in editor, editor.count("uzzy")
    assert "lock_band|string|lower != 'fuzzy'" in editor, (
        "editor.html must skip the TM lock badge for a stored legacy fuzzy band"
    )
    assert pkeys.count("lock_band|string|lower != 'fuzzy'") == 2, (
        "project_keys.html must skip the TM lock badge for a legacy fuzzy band at BOTH sites"
    )
    assert "fuzzy TM match" not in pkeys and "_vfuzzy" not in pkeys, pkeys.count("uzzy")
    for name, html in (("editor.html", editor), ("document.html", document)):
        assert 'class="badge {% if r.tm.match_pct == 100 %}exact{% else %}fuzzy' not in html, (
            f"{name} must not badge a below-100 TM match as fuzzy"
        )
        assert "~ Semantic" not in html, f"{name} has no semantic badge in the core"
    print("  badge: exact only; a stored legacy fuzzy lock renders no TM badge: OK")


def main():
    test_used_tm_match_marker_default_and_backfill()
    test_used_tm_match_stamped_segmented_and_pure_llm()
    test_effective_policy_resolution()
    test_stored_removed_setting_is_inert()
    test_seeding_snapshots_globals()
    test_near_match_never_fills()
    test_exact_fills_and_stamps_100()
    test_heal_leaves_drifted_mt_alone_without_exact_hit()
    test_tm_locks_disabled_whole_noop()
    test_pool_selection_narrows_and_provenance()
    test_pool_selection_run_drops_unreadable_and_notes()
    test_empty_pool_selection_roundtrip_and_note()
    test_junk_scalar_helper_still_validates()
    test_dead_fields_refused_and_type_switch_clears()
    test_removed_params_refused_with_clear_message()
    test_removed_params_clear_error_on_api_and_cli()
    test_cli_show_row_and_package_view_carry_config()
    test_mcp_field_allowlist_carries_config()
    test_builder_forms_carry_every_tm_field()
    test_web_roundtrip_config()
    test_lock_badge_templates_exact_only()
    test_key_files_legacy_fuzzy_lock_round_trips()
    test_short3_restore_backcompat()
    print("TM MATCH STEP CONFIG OK")


if __name__ == "__main__":
    main()
