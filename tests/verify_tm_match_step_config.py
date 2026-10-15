"""Batch 15 Train 1 -- the CONFIGURABLE tm_match step (SPEC-tm-match-step-config).

Verifies the six per-step WorkflowStep policy columns + the durable
WorkflowRun.used_tm_match marker + the engine changes (effective policy, below-threshold
fuzzy fill, the fuzzy lock-mode enum, real tm_match_pct, per-step TM pool selection) +
the four-surface authoring ride. DISTINCT from the pre-existing verify_tm_match_step.py
(which stays green; its test_add_step_parity_tm_match is UPDATED for the DE-alias in the
same diff).

Covers the LAWs: TM-1 (config on the step), TM-3 (byte-identical upgrade), TM-5 (fuzzy
fill threshold-gated), TM-6 (variable-content parity), TM-7 (fuzzy lock modes), TM-8
(snapshot seeding), TM-9 (pool selection narrow-only), TM-10 (dead on non-tm_match),
TM-12 (resume 4-tuple + short-3 back-compat), TM-13/N2/P2 (durable marker, both
branches), TM-19 (real pct), TM-20 (heal exactness).

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
        settings.set_many(
            {
                "tm_lock_ice": "1",
                "tm_hundreds_enter_lqa": "1",
                "tm_context_floor": "95",
                "tm_fill_threshold": "100",
                "tm_fuzzy_lock_mode": "soft",
            }
        )
        p = _mk_project(s, "PolicyResolve")
        # A NULL-override step (every column None) resolves the LIVE global exactly.
        null_step = WorkflowStep(workflow_id=1, order_index=0, step_type=StepType.TM_MATCH)
        pol = tm_lock.resolve_policy_for_step(s, p, null_step)
        assert (pol.fill_threshold, pol.fuzzy_lock_mode) == (100, "soft"), pol
        assert (pol.lock_ice, pol.hundreds_enter_lqa, pol.context_floor) == (True, True, 95), pol
        # A concrete per-step override wins over the global.
        over = WorkflowStep(
            workflow_id=1,
            order_index=0,
            step_type=StepType.TM_MATCH,
            tm_fill_threshold=85,
            tm_fuzzy_lock_mode="policy",
            tm_lock_ice=False,
            tm_hundreds_enter_lqa=False,
            tm_context_floor=80,
        )
        pol2 = tm_lock.resolve_policy_for_step(s, p, over)
        assert pol2.fill_threshold == 85 and pol2.fuzzy_lock_mode == "policy", pol2
        assert pol2.lock_ice is False and pol2.hundreds_enter_lqa is False, pol2
        assert pol2.context_floor == 80, pol2
        # Clamps: threshold clamps [50,100], floor [50,99]; a garbage fuzzy mode self-heals
        # to the GLOBAL default (soft here), like a garbage context_floor.
        junk = WorkflowStep(
            workflow_id=1,
            order_index=0,
            step_type=StepType.TM_MATCH,
            tm_fill_threshold=999,
            tm_context_floor=1,
            tm_fuzzy_lock_mode="banana",
        )
        pol3 = tm_lock.resolve_policy_for_step(s, p, junk)
        assert pol3.fill_threshold == 100, pol3.fill_threshold
        assert pol3.context_floor == 50, pol3.context_floor
        assert pol3.fuzzy_lock_mode == "soft", pol3.fuzzy_lock_mode
    print("  policy: NULL->global, override wins, clamps [50,100]/[50,99], mode self-heals: OK")


def test_seeding_snapshots_globals():
    with Session(engine) as s:
        settings.set_many(
            {
                "tm_fill_threshold": "90",
                "tm_fuzzy_lock_mode": "policy",
                "tm_hundreds_enter_lqa": "0",
            }
        )
        try:
            wf = Workflow(name="SeedWF")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            step = wf_svc.add_step(s, wf.id, name="TM", step_type=StepType.TM_MATCH)
            # The five scalars are seeded from the CURRENT globals (concrete snapshot);
            # the pool selection is NEVER snapshotted (S2 -> None = all readable).
            assert step.tm_fill_threshold == 90, step.tm_fill_threshold
            assert step.tm_fuzzy_lock_mode == "policy", step.tm_fuzzy_lock_mode
            assert step.tm_hundreds_enter_lqa is False, step.tm_hundreds_enter_lqa
            assert step.tm_pool_selection is None, step.tm_pool_selection
            # A later global change does NOT mutate the already-created step (LAW-TM-8).
            settings.set_many({"tm_fill_threshold": "100"})
            s.refresh(step)
            assert step.tm_fill_threshold == 90, "a snapshot must not track a later global change"
            # A NON-tm_match step never seeds -> all six stay None.
            llm = wf_svc.add_step(s, wf.id, name="MT", step_type=StepType.LLM_AGENT)
            assert llm.tm_fill_threshold is None and llm.tm_fuzzy_lock_mode is None, llm
        finally:
            settings.set_many(
                {
                    "tm_fill_threshold": "100",
                    "tm_fuzzy_lock_mode": "soft",
                    "tm_hundreds_enter_lqa": "1",
                }
            )
    print("  seeding: new tm_match step snapshots globals (frozen); non-tm_match stays NULL: OK")


# ============================================================================
# (3) fuzzy fill: threshold gate + real pct (LAW-TM-5 / TM-19) + parity (TM-6)
# ============================================================================
def test_threshold_100_fills_exact_only_byte_identical():
    with Session(engine) as s:
        p = _mk_project(s, "Thresh100")
        tm.write_back(s, p.id, "Save the file.", "Enregistrer le fichier.", "en-US", "fr-FR")
        # A near (not exact) source at threshold 100 (default) is NOT filled -- byte-identical
        # to pre-Batch-15 exact-only behaviour.
        doc = _mk_doc(s, p.id, "fr-FR", ["Save the files."])
        wf = _mk_tm_workflow(s, tm_config={"tm_fill_threshold": 100})
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # No TM fill: the segment carries NO lock band and NO tm_match_pct, and its target
        # is NOT the TM entry's target (a lone-tm_match no-op leaves the source in place,
        # which is unchanged pre-Batch-15 behaviour).
        assert seg.lock_band is None and seg.tm_match_pct is None, (seg.lock_band, seg.tm_match_pct)
        assert seg.target_text != "Enregistrer le fichier.", (
            "a fuzzy hit must NOT fill at threshold 100",
            seg.target_text,
        )
    print("  fuzzy: threshold 100 fills exact only (byte-identical), no fuzzy fill: OK")


def test_threshold_85_fills_fuzzy_with_real_pct():
    with Session(engine) as s:
        p = _mk_project(s, "Thresh85")
        # A fuzzy pair: a one-word difference scores well above 85 but below 100.
        tm.write_back(
            s,
            p.id,
            "Please save the current document now.",
            "Veuillez enregistrer.",
            "en-US",
            "fr-FR",
        )
        doc = _mk_doc(s, p.id, "fr-FR", ["Please save the current document today."])
        wf = _mk_tm_workflow(s, tm_config={"tm_fill_threshold": 85, "tm_fuzzy_lock_mode": "soft"})
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "Veuillez enregistrer.", (
            "an >=85% fuzzy hit must FILL at threshold 85",
            seg.target_text,
        )
        # LAW-TM-19: the fill stamps its REAL pct (< 100), NOT a hardcoded 100 -- which
        # keeps it OUT of the exact re-lock whitelist.
        assert seg.tm_match_pct is not None and seg.tm_match_pct < 100, (
            "a fuzzy fill must stamp its real match_pct, not 100",
            seg.tm_match_pct,
        )
        assert seg.tm_match_pct >= 85, seg.tm_match_pct
        # soft mode -> the fuzzy band, soft lock.
        assert seg.lock_band == "fuzzy" and seg.lock_hard is False, (seg.lock_band, seg.lock_hard)
    print("  fuzzy: threshold 85 fills an 85-99 hit + stamps the REAL pct (LAW-TM-19): OK")


def test_number_parity_blocks_poisoned_fuzzy_fill():
    # LAW-TM-6: a fuzzy hit whose NUMBER differs from the segment source is NOT filled
    # (fuzz.ratio("Pay 900 now","Pay 100 now") ~= 95 yet the FACT differs). Revert-prove:
    # without variable_content_parity this would fill the wrong number.
    with Session(engine) as s:
        p = _mk_project(s, "NumberParity")
        tm.write_back(
            s, p.id, "Pay 100 dollars now please.", "Payez 100 dollars.", "en-US", "fr-FR"
        )
        doc = _mk_doc(s, p.id, "fr-FR", ["Pay 900 dollars now please."])
        wf = _mk_tm_workflow(s, tm_config={"tm_fill_threshold": 80, "tm_fuzzy_lock_mode": "soft"})
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text != "Payez 100 dollars.", (
            "a number-poisoned fuzzy hit must NEVER fill (LAW-TM-6)",
            seg.target_text,
        )
        assert seg.lock_band != "fuzzy", seg.lock_band
    # And the helper itself, directly:
    assert tm_lock.variable_content_parity("Pay 100 now", "Pay 100 now") is True
    assert tm_lock.variable_content_parity("Pay 900 now", "Pay 100 now") is False
    assert tm_lock.variable_content_parity("Go to http://a.com", "Go to http://b.com") is False
    assert tm_lock.variable_content_parity("Hello", "hello") is False  # case-only
    # L5(c) DISCOVERABLE RESIDUAL (section 19-Q1): the parity check compares DIGIT tokens,
    # not numbers spelled as WORDS -- "Pay nine hundred" vs "Pay one hundred" have identical
    # digit/URL/var signatures, so a fuzzy fill between them is NOT blocked by this guard.
    # This is the known LAW-TM-6 residual (the maintainer relaxed the threshold, not the safety line
    # for words-as-numbers); flagged here so it is discoverable, not silently assumed covered.
    assert tm_lock.variable_content_parity("Pay nine hundred", "Pay one hundred") is True
    print("  fuzzy: variable-content parity blocks a number/URL/case-poisoned fill (LAW-TM-6): OK")


def test_heal_branch_unit_exact_only():
    # LAW-TM-20 (unit): plan_hit at min_fill=100 (the heal path) never fills a fuzzy hit.
    pol = tm_lock.TmPolicy(fill_threshold=80, fuzzy_lock_mode="soft")
    fuzzy = {"match_pct": 90, "source_text": "Pay 100 now", "target_text": "X", "id": 1}
    assert tm_lock.plan_hit(fuzzy, pol, min_fill=100, seg_source="Pay 100 now") is None, (
        "the heal path (min_fill=100) must never fill a fuzzy hit (LAW-TM-20)"
    )
    got = tm_lock.plan_hit(fuzzy, pol, min_fill=80, seg_source="Pay 100 now")
    assert got == ("fuzzy", False), got
    # L1: a "fuzzy"-band hit whose target byte-equals a NON-empty stored target is NOT a
    # re-lock (safety-by-construction, not caller-discipline).
    fz2 = {"match_pct": 90, "source_text": "Pay 100 now", "target_text": "Paie 100.", "id": 1}
    assert (
        tm_lock.fill_or_relock("Paie 100.", fz2, pol, min_fill=80, seg_source="Pay 100 now") is None
    ), "a fuzzy hit must NEVER re-lock a non-empty byte-equal target (L1)"
    print("  heal (unit): plan_hit min_fill=100 never fuzzy-fills; fuzzy never re-locks (L1): OK")


def test_heal_branch_orchestrator_two_floor():
    # H2 (non-vacuous, orchestrator-level): an EMPTY candidate and a NON-EMPTY status=MT
    # segment SHARE a source whose only TM entry is a ~92% FUZZY match; threshold 85; the
    # workflow is tm_match -> translate. The EMPTY one fuzzy-fills (band "fuzzy", soft-locked
    # -> excluded from the translate). The drifted-MT one must be left for the translate step
    # (heal is EXACT-100 only, LAW-TM-20) -> it is NOT healed/locked with the fuzzy target.
    # MUTATION-PROVEN: setting the orchestrator heal min_fill to the step threshold makes the
    # 92% hit HEAL+lock the drifted segment (band "fuzzy", the fuzzy TM target) and fails the
    # two assertions below; mutating the _relock lookup floor is masked by the merge (the
    # drifted segment never fills/heals from a fuzzy hit).
    with Session(engine) as s:
        p = _mk_project(s, "HealTwoFloor")
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
            [
                "Please save the current document today.",
                "Please save the current document today.",
            ],
        )
        seg1 = _seg(s, doc.id, 1)
        seg1.target_text = "DRIFTED MT TEXT"
        seg1.status = SegStatus.MT
        s.add(seg1)
        s.commit()
        wf = _mk_tm_workflow(
            s,
            tm_config={"tm_fill_threshold": 85, "tm_fuzzy_lock_mode": "soft"},
            downstream=[(StepType.LLM_AGENT, False)],
        )
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        empty_seg = _seg(s, doc.id, 0)
        drifted = _seg(s, doc.id, 1)
        # the empty candidate fuzzy-fills + soft-locks (excluded from the translate step).
        assert empty_seg.target_text == "Veuillez enregistrer maintenant.", empty_seg.target_text
        assert empty_seg.lock_band == "fuzzy", empty_seg.lock_band
        # the drifted-MT segment is NOT healed/locked by the fuzzy hit -- it carries NO lock
        # band and is NOT the fuzzy TM target (the translate step re-did it instead).
        assert drifted.lock_band is None, (
            "a 92% fuzzy hit must NOT heal+lock a drifted MT segment (LAW-TM-20)",
            drifted.lock_band,
        )
        assert drifted.target_text != "Veuillez enregistrer maintenant.", (
            "a 92% fuzzy hit must NOT overwrite a drifted MT segment via heal",
            drifted.target_text,
        )
    print("  heal (orchestrator): a fuzzy hit never heals/locks a drifted MT segment (H2): OK")


# ============================================================================
# (4) fuzzy lock modes (LAW-TM-7)
# ============================================================================
def _fuzzy_fill_workflow(s, name, mode, downstream=None):
    p = _mk_project(s, name)
    tm.write_back(
        s, p.id, "Please save the current document now.", "Veuillez enregistrer.", "en-US", "fr-FR"
    )
    doc = _mk_doc(s, p.id, "fr-FR", ["Please save the current document today."])
    wf = _mk_tm_workflow(
        s, tm_config={"tm_fill_threshold": 85, "tm_fuzzy_lock_mode": mode}, downstream=downstream
    )
    return p, doc, wf


def test_lock_mode_none_no_lock_overwritten_by_translate():
    with Session(engine) as s:
        # `none`: filled, NO lock, stays in pending_idx -> a downstream UNPROTECTED translate
        # step overwrites it (flipping tm_origin False -> normal MT, no lock band).
        _p, doc, wf = _fuzzy_fill_workflow(
            s, "ModeNone", "none", downstream=[(StepType.LLM_AGENT, False)]
        )
        entry = s.exec(select(SegmentTM).where(SegmentTM.project_id == _p.id)).one()
        assert (entry.use_count or 0) == 0, entry.use_count
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.lock_band is None, ("a 'none' fuzzy fill carries no lock band", seg.lock_band)
        # L4 (non-vacuous): the none-fill ACTUALLY happened (it bumped the TM entry's
        # use_count -- the only TM read this workflow makes), THEN a downstream unprotected
        # translate step overwrote it with the MockProvider MT output. This distinguishes
        # "filled then overwritten" from "never filled" (a never-filled segment would leave
        # use_count 0, since the order-1 translate step does no TM lookup).
        s.refresh(entry)
        assert (entry.use_count or 0) >= 1, (
            "a 'none' fuzzy fill must have happened (bumped TM use_count) before the "
            "downstream translate overwrote it",
            entry.use_count,
        )
        # the downstream translate step overwrote the fill (tm_origin flipped -> plain MT):
        # the target is the MockProvider MT output (locale-prefixed), NOT the raw TM fill,
        # and it carries NO lock band and IS fresh MT (mt_confidence set).
        assert seg.target_text.startswith("[fr-FR]"), (
            "an unprotected translate step must overwrite the 'none' fuzzy fill with MT",
            seg.target_text,
        )
        assert seg.mt_confidence is not None and seg.tm_match_pct != 92, (
            "the overwritten segment must be fresh MT, not a TM fill",
            (seg.mt_confidence, seg.tm_match_pct),
        )
    print("  lock mode none: filled (use_count bumped) then overwritten by translate (L4): OK")


def test_lock_mode_none_survives_tm_only():
    with Session(engine) as s:
        # `none` in a TM-only workflow (no downstream step) SURVIVES to delivery, with its
        # real pct and NO lock band.
        _p, doc, wf = _fuzzy_fill_workflow(s, "ModeNoneTMonly", "none", downstream=None)
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "Veuillez enregistrer.", seg.target_text
        assert seg.lock_band is None, seg.lock_band
        assert seg.tm_match_pct is not None and seg.tm_match_pct < 100, seg.tm_match_pct
    print("  lock mode none: survives a TM-only workflow (real pct, no lock band): OK")


def test_lock_mode_soft_and_policy():
    with Session(engine) as s:
        # `soft`: fuzzy band, soft lock.
        _p, doc, wf = _fuzzy_fill_workflow(s, "ModeSoft", "soft", downstream=None)
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.lock_band == "fuzzy" and seg.lock_hard is False, (seg.lock_band, seg.lock_hard)
    with Session(engine) as s:
        # `policy` with hundreds_enter_lqa OFF -> a HARD fuzzy lock (mirrors the 100-band
        # policy). The write seam still governs it (LAW-TM-4).
        settings.set_many({"tm_hundreds_enter_lqa": "0"})
        try:
            _p, doc, wf = _fuzzy_fill_workflow(s, "ModePolicy", "policy", downstream=None)
            orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            s.expire_all()
            seg = _seg(s, doc.id, 0)
            assert seg.lock_band == "fuzzy" and seg.lock_hard is True, (
                seg.lock_band,
                seg.lock_hard,
            )
        finally:
            settings.set_many({"tm_hundreds_enter_lqa": "1"})
    # lock_mode_for directly:
    pol_soft = tm_lock.TmPolicy(hundreds_enter_lqa=True, fuzzy_lock_mode="policy")
    pol_hard = tm_lock.TmPolicy(hundreds_enter_lqa=False, fuzzy_lock_mode="policy")
    assert tm_lock.lock_mode_for("fuzzy", tm_lock.TmPolicy(fuzzy_lock_mode="none")) == "none"
    assert tm_lock.lock_mode_for("fuzzy", tm_lock.TmPolicy(fuzzy_lock_mode="soft")) == "soft"
    assert tm_lock.lock_mode_for("fuzzy", pol_soft) == "soft"
    assert tm_lock.lock_mode_for("fuzzy", pol_hard) == "hard"
    print("  lock mode soft/policy: soft-locks / inherits the 100-band lock discipline: OK")


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
# (6) per-step TM pool selection (LAW-TM-9) + fuzzy provenance (finding 12)
# ============================================================================
def test_pool_selection_narrows_and_provenance():
    with Session(engine) as s:
        p = _mk_project(s, "PoolSel")
        # own-project exact entry, carrying an id used for bump_usage.
        tm.write_back(s, p.id, "Save.", "Enregistrer.", "en-US", "fr-FR")
        entry = s.exec(select(SegmentTM).where(SegmentTM.project_id == p.id)).one()
        # NULL selection = all readable -> the own-project hit is found (restrict None).
        hits_all = tm.lookup_batch(s, p.id, ["Save."], "en-US", "fr-FR", min_score=100)
        assert hits_all["Save."] and hits_all["Save."]["match_pct"] == 100, hits_all
        # A selection naming ONLY the own project still finds it (narrow to {own}).
        hits_own = tm.lookup_batch(
            s, p.id, ["Save."], "en-US", "fr-FR", min_score=100, restrict_project_ids={p.id}
        )
        assert hits_own["Save."] is not None, hits_own
        # A selection resolving to the EMPTY set (a foreign/unreadable id only) matches
        # NOTHING (fail-closed), distinct from None=all-readable.
        hits_empty = tm.lookup_batch(
            s, p.id, ["Save."], "en-US", "fr-FR", min_score=100, restrict_project_ids=set()
        )
        assert hits_empty["Save."] is None, ("an empty selection must match nothing", hits_empty)
        hits_foreign = tm.lookup_batch(
            s, p.id, ["Save."], "en-US", "fr-FR", min_score=100, restrict_project_ids={999999}
        )
        assert hits_foreign["Save."] is None, (
            "an unreadable-only selection matches nothing",
            hits_foreign,
        )
        # Fuzzy provenance (finding 12): a FUZZY lookup dict now carries the winning id.
        fz = tm.lookup_batch(s, p.id, ["Sav."], "en-US", "fr-FR", min_score=50)
        assert fz["Sav."] is not None and fz["Sav."].get("id") == entry.id, (
            "a fuzzy hit must carry the winning entry id (provenance + bump_usage)",
            fz.get("Sav."),
        )
    print("  pool selection: NULL=all, subset narrows, empty/foreign match nothing; fuzzy id: OK")


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


# ============================================================================
# (7) dead fields on non-tm_match types (LAW-TM-10) + type-switch clears
# ============================================================================
def test_dead_fields_refused_and_type_switch_clears():
    with Session(engine) as s:
        wf = Workflow(name="DeadWF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        # a config field on an llm_agent step is refused.
        for stype in (StepType.LLM_AGENT, StepType.LQA, StepType.HUMAN_REVIEW, StepType.EXTERNAL):
            try:
                wf_svc.add_step(s, wf.id, name="bad", step_type=stype, tm_fuzzy_lock_mode="none")
                _fail(f"tm_fuzzy_lock_mode on a {stype} step must be refused")
            except wf_svc.WorkflowError:
                pass
        # a tm_match step carrying config, switched to llm_agent, force-clears the six to
        # NULL (nullable -> no IntegrityError; SPEC section 8.A).
        step = wf_svc.add_step(
            s, wf.id, name="tm", step_type=StepType.TM_MATCH, tm_fill_threshold=85
        )
        assert step.tm_fill_threshold == 85
        wf_svc.update_step(s, step.id, step_type=StepType.LLM_AGENT)
        s.refresh(step)
        assert step.step_type == StepType.LLM_AGENT
        assert step.tm_fill_threshold is None and step.tm_fuzzy_lock_mode is None, (
            "a type switch away from tm_match must NULL the six config columns",
            (step.tm_fill_threshold, step.tm_fuzzy_lock_mode),
        )
    print("  dead fields: the six refused on non-tm_match; a type switch clears them to NULL: OK")


# ============================================================================
# (8) four-surface round-trip (LAW-TM-11) -- api show + mcp edit_workflow field set
# ============================================================================
_TM_CFG_KEYS = (
    "tm_fill_threshold",
    "tm_fuzzy_lock_mode",
    "tm_lock_ice",
    "tm_hundreds_enter_lqa",
    "tm_context_floor",
    "tm_pool_selection",
)


def test_cli_show_row_and_package_view_carry_config():
    # L3: assert the six keys land on the REAL CLI `workflow show --json` step row (not just
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
            tm_fill_threshold=85,
            tm_fuzzy_lock_mode="policy",
            tm_pool_selection="1,2",
        )
        assert step.tm_pool_selection == [1, 2], step.tm_pool_selection
        blob = str(pv._tm_match_view(step))
        assert "85" in blob and "policy" in blob, ("package view must render the config", blob)
        assert "1, 2" in blob, blob
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
    assert row["tm_fill_threshold"] == 85 and row["tm_fuzzy_lock_mode"] == "policy", row
    assert row["tm_pool_selection"] == [1, 2], row["tm_pool_selection"]
    print(
        "  surfaces: CLI show --json row carries the six config keys; package_view renders them: OK"
    )


def test_mcp_field_allowlist_carries_config():
    # SPEC section 8.E: the six fields are in BOTH MCP step-field allowlists (create + edit), so
    # create_workflow / edit_workflow forward them generically instead of refusing them as
    # unknown.
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    for f in (
        "tm_fill_threshold",
        "tm_fuzzy_lock_mode",
        "tm_lock_ice",
        "tm_hundreds_enter_lqa",
        "tm_context_floor",
        "tm_pool_selection",
    ):
        assert f in mcp_server._WORKFLOW_STEP_FIELDS, f"{f} missing from MCP add allowlist"
        assert f in mcp_server._WORKFLOW_EDIT_FIELDS, f"{f} missing from MCP edit allowlist"
    print(
        "  surfaces: the six config fields are in both MCP step-field allowlists (section 8.E): OK"
    )


# ============================================================================
# (9) resume: a fuzzy soft lock + its pct survive pause/resume (LAW-TM-12)
# ============================================================================
def test_fuzzy_soft_lock_survives_pause_resume():
    with Session(engine) as s:
        p = _mk_project(s, "FuzzyResume")
        tm.write_back(
            s,
            p.id,
            "Please save the current document now.",
            "Veuillez enregistrer.",
            "en-US",
            "fr-FR",
        )
        doc = _mk_doc(s, p.id, "fr-FR", ["Please save the current document today.", "Free line."])
        wf = _mk_tm_workflow(
            s,
            tm_config={"tm_fill_threshold": 85, "tm_fuzzy_lock_mode": "soft"},
            downstream=[(StepType.HUMAN_REVIEW, None)],
        )
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", summ["paused_text"])
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "Veuillez enregistrer.", seg.target_text
        assert seg.lock_band == "fuzzy", ("the fuzzy soft lock must survive resume", seg.lock_band)
        # LAW-TM-19/12: the REAL pct survives the pause (the 4-tuple / tm_fill_pct round-trip),
        # never a hardcoded 100.
        assert seg.tm_match_pct is not None and seg.tm_match_pct < 100, (
            "the real fuzzy pct must survive pause/resume",
            seg.tm_match_pct,
        )
    print("  resume: a fuzzy soft lock + its real pct survive pause/resume (LAW-TM-12/19): OK")


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


def _run_none_then_protected(name, tm_protect):
    # tm_match(none) -> a tm_protect=<tm_protect> llm_agent step that ALWAYS runs (NO
    # run_condition -- the P7 shape, so the step actually executes; the prior version gated
    # it on prev_lqa:failed which MockProvider's LQA never triggers, making it SKIPPED and
    # the test vacuous). A tm_protect=True override drops tm_origin indices from the step's
    # pending set (lqa_safety.tm_protect_for), so the 'none' fuzzy fill SURVIVES; False/None
    # let the step overwrite it.
    with Session(engine) as s:
        p = _mk_project(s, name)
        tm.write_back(
            s,
            p.id,
            "Please save the current document now.",
            "Veuillez enregistrer.",
            "en-US",
            "fr-FR",
        )
        doc = _mk_doc(s, p.id, "fr-FR", ["Please save the current document today."])
        wf = Workflow(name=name + "WF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="tm",
                step_type=StepType.TM_MATCH,
                tm_fill_threshold=85,
                tm_fuzzy_lock_mode="none",
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=1,
                name="fix",
                step_type=StepType.LLM_AGENT,
                tm_protect=tm_protect,
            )
        )
        s.commit()
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        return _seg(s, doc.id, 0).target_text


def test_none_mode_protected_from_tm_protect_correction():
    # M6(a) / section 14 T-4 (N1, de-vacuum'd to the P7 shape): a 'none' fuzzy fill stays in
    # pending_idx but is a tm_origin index, so a tm_protect=ON step must NOT overwrite it,
    # while tm_protect=False DOES. MUTATION-PROVEN (R11): forcing tm_protected_idx = [] at the
    # orchestrator drop site makes the tm_protect=True run overwrite the fill and fails the
    # first assertion below (checked: it goes RED).
    protected = _run_none_then_protected("NoneProtectTrue", True)
    assert protected == "Veuillez enregistrer.", (
        "a 'none' fuzzy fill must be PROTECTED from a tm_protect=ON step (T-4)",
        protected,
    )
    unprotected = _run_none_then_protected("NoneProtectFalse", False)
    assert unprotected.startswith("[fr-FR]"), (
        "a tm_protect=OFF step must OVERWRITE the 'none' fuzzy fill (proves the True run is "
        "not vacuously passing)",
        unprotected,
    )
    print("  lock mode none: tm_protect=ON protects the fill, OFF overwrites (M6a/T-4/N1): OK")


def test_hard_fuzzy_policy_refused_at_write_seam():
    # M6(c): a HARD fuzzy `policy` lock (fuzzy_lock_mode=policy + hundreds_enter_lqa off) is
    # refused at the sole machine write seam (quality.commit_mt_output) exactly like any hard
    # lock (LAW-TM-4) -- a later machine write to the segment is refused.
    from app.services import quality

    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "0"})
        try:
            p = _mk_project(s, "HardFuzzy")
            tm.write_back(
                s,
                p.id,
                "Please save the current document now.",
                "Veuillez enregistrer.",
                "en-US",
                "fr-FR",
            )
            doc = _mk_doc(s, p.id, "fr-FR", ["Please save the current document today."])
            wf = _mk_tm_workflow(
                s, tm_config={"tm_fill_threshold": 85, "tm_fuzzy_lock_mode": "policy"}
            )
            orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            s.expire_all()
            seg = _seg(s, doc.id, 0)
            assert seg.lock_band == "fuzzy" and seg.lock_hard is True, (
                seg.lock_band,
                seg.lock_hard,
            )
            # the write seam REFUSES a machine overwrite of the hard fuzzy lock.
            outcome = quality.commit_mt_output(s, seg, "REWRITTEN BY MACHINE", source="workflow")
            assert outcome.lock_refused is True and outcome.committed is False, outcome
            s.refresh(seg)
            assert seg.target_text == "Veuillez enregistrer.", (
                "a hard fuzzy policy lock must be refused at commit_mt_output (M6c/LAW-TM-4)",
                seg.target_text,
            )
        finally:
            settings.set_many({"tm_hundreds_enter_lqa": "1"})
    print("  lock mode policy: a HARD fuzzy lock is refused at the write seam (M6c/LAW-TM-4): OK")


def test_builder_forms_carry_every_tm_field():
    # M6(b) / section 8.B: BOTH the add-form and the per-step edit-form in the workflow
    # builder must carry every name="tm_*" config input (a field on one form but not the
    # other is an authoring hole). Read the template and assert both forms carry all six.
    tmpl = os.path.join(_ROOT, "app", "templates", "workflow_builder.html")
    with open(tmpl, encoding="utf-8") as fh:
        html = fh.read()
    # split at the ADD form action so we can count occurrences per form region.
    marker = '/steps" class="form"'
    assert marker in html, "could not locate the add-step form in workflow_builder.html"
    edit_region, add_region = html.split(marker, 1)
    names = [
        "tm_fill_threshold",
        "tm_fuzzy_lock_mode",
        "tm_lock_ice",
        "tm_hundreds_enter_lqa",
        "tm_context_floor",
        "tm_pool_selection",
    ]
    for nm in names:
        needle = f'name="{nm}"'
        assert needle in edit_region, f"the EDIT form is missing {nm}"
        assert needle in add_region, f"the ADD form is missing {nm}"
    print("  surfaces: both builder forms carry every name=tm_* config input (M6b/8.B): OK")


def test_web_roundtrip_config():
    # M6(b) web round-trip: a config set via the service reads back through update_step
    # (present-value / present-blank clears to inherit), the same contract the web edit form
    # POSTs. Confirms an override survives an edit and a blank clears it.
    with Session(engine) as s:
        wf = Workflow(name="WebRT")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        step = wf_svc.add_step(
            s, wf.id, name="tm", step_type=StepType.TM_MATCH, tm_fill_threshold=85
        )
        wf_svc.update_step(s, step.id, tm_fuzzy_lock_mode="none", tm_lock_ice="off")
        s.refresh(step)
        assert step.tm_fuzzy_lock_mode == "none" and step.tm_lock_ice is False, step
        # a blank/"auto" clears back to inherit (NULL).
        wf_svc.update_step(s, step.id, tm_fill_threshold="", tm_lock_ice="auto")
        s.refresh(step)
        assert step.tm_fill_threshold is None and step.tm_lock_ice is None, (
            "a blank / 'auto' edit must clear the override back to inherit",
            (step.tm_fill_threshold, step.tm_lock_ice),
        )
    print(
        "  surfaces: web/update_step round-trip -- override sets, blank/auto clears to inherit: OK"
    )


def test_fuzzy_band_priced_and_dispatched_honestly():
    # H1: the new "fuzzy" lock_band must NOT be priced/dispatched as an exact 100% match.
    # classify_band lets a "fuzzy" band fall through to the tm_match_pct ladder, so a fuzzy
    # fill is fuzzy_mid (billable), provenanced tm_fuzzy with a match_score, and NOT locked
    # on the wire. MUTATION-PROVEN: the old `else -> exact` shorted an 85% fill to exact.
    from app.services import vendor_cost

    # the gate's exact pin + the ladder.
    assert vendor_cost.classify_band("x", 85, "fuzzy", set()) == "fuzzy_mid", (
        "fuzzy 85 -> fuzzy_mid"
    )
    assert vendor_cost.classify_band("x", 92, "fuzzy", set()) == "fuzzy_mid", (
        "fuzzy 92 -> fuzzy_mid"
    )
    assert vendor_cost.classify_band("x", 97, "fuzzy", set()) == "fuzzy_high", (
        "fuzzy 97 -> fuzzy_high"
    )
    # a covered exact lock is still exact (byte-identical); ICE stays context.
    assert vendor_cost.classify_band("x", 100, "100", set()) == "exact"
    assert vendor_cost.classify_band("x", 100, "ice", set()) == "context"
    # H1 consequences, keyed on the CLASSIFIED band (vendor_dispatch reads locked =
    # band in COVERED_BANDS, provenance = PROVENANCE_BY_BAND[band], match_score for tm_fuzzy):
    #  * BILLABLE (fuzzy_mid not covered), * provenance tm_fuzzy, * NOT locked on the wire.
    assert "fuzzy_mid" not in vendor_cost.COVERED_BANDS, "an 85% fuzzy fill must be billable"
    assert vendor_cost.PROVENANCE_BY_BAND["fuzzy_mid"] == "tm_fuzzy"
    est = vendor_cost.estimate_from_segments(
        [{"source": "please save now", "tm_match_pct": 85, "lock_band": "fuzzy"}],
        {"currency": "USD", "bands": dict.fromkeys(vendor_cost.BANDS, 1.0)},
    )
    assert est["billable_words"] == 3, ("an 85% fuzzy fill must be billable", est["billable_words"])
    assert est["bands"]["fuzzy_mid"]["segments"] == 1 and est["bands"]["exact"]["segments"] == 0, (
        est
    )
    print(
        "  vendor: a fuzzy-band fill is priced fuzzy_mid, billable, tm_fuzzy, not covered (H1): OK"
    )


def test_key_files_fuzzy_lock_round_trips():
    # M3: a "fuzzy" lock survives the key transport round-trip (it maps to XLIFF fuzzy-match).
    from types import SimpleNamespace

    from app.services import key_files

    seg = SimpleNamespace(lock_band="fuzzy", lock_hard=False)
    lk = key_files._lock_transport(seg)
    assert lk == {"state": "fuzzy-match"}, lk
    back = key_files._lock_from_transport(lk)
    assert back == ("fuzzy", False), ("a fuzzy lock must round-trip, not be dropped", back)
    # a hard fuzzy lock carries translate=no and still round-trips.
    seg_h = SimpleNamespace(lock_band="fuzzy", lock_hard=True)
    lkh = key_files._lock_transport(seg_h)
    assert lkh == {"state": "fuzzy-match", "translate": "no"}, lkh
    assert key_files._lock_from_transport(lkh) == ("fuzzy", True), key_files._lock_from_transport(
        lkh
    )
    print("  key transport: a fuzzy lock round-trips (XLIFF fuzzy-match), not dropped (M3): OK")


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


def test_junk_scalar_refused_clear_works():
    # N2: dropping argparse type=int from the scalar CLI flags must NOT let junk silently
    # clear the override. The seam (_require_valid_tm_scalar, used by add_step/update_step)
    # REFUSES a non-empty value that is neither an inherit token nor a valid int, on every
    # surface; the clear path (auto/inherit/blank) still works.
    assert wf_svc._require_valid_tm_scalar("85", "x") == 85
    assert wf_svc._require_valid_tm_scalar("auto", "x") is None
    assert wf_svc._require_valid_tm_scalar("", "x") is None
    assert wf_svc._require_valid_tm_scalar(None, "x") is None
    for bad in ("85%", "abc", "8O", "1.5"):
        try:
            wf_svc._require_valid_tm_scalar(bad, "TM fill threshold")
            _fail(f"a junk scalar {bad!r} must be REFUSED (N2)")
        except wf_svc.WorkflowError:
            pass
    with Session(engine) as s:
        wf = Workflow(name="JunkScalar")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        # junk on add_step is refused (not silently seeded/cleared).
        try:
            wf_svc.add_step(
                s, wf.id, name="tm", step_type=StepType.TM_MATCH, tm_fill_threshold="85%"
            )
            _fail("add_step must refuse a junk tm_fill_threshold (N2)")
        except wf_svc.WorkflowError:
            pass
        step = wf_svc.add_step(
            s, wf.id, name="tm2", step_type=StepType.TM_MATCH, tm_fill_threshold=85
        )
        # a junk EDIT is refused (the override is NOT silently cleared to inherit).
        try:
            wf_svc.update_step(s, step.id, tm_fill_threshold="abc")
            _fail("update_step must refuse a junk tm_fill_threshold (N2)")
        except wf_svc.WorkflowError:
            pass
        s.refresh(step)
        assert step.tm_fill_threshold == 85, ("a refused edit must not change the value", step)
        # the clear path still works: "auto" clears to inherit (NULL).
        wf_svc.update_step(s, step.id, tm_fill_threshold="auto")
        s.refresh(step)
        assert step.tm_fill_threshold is None, ("'auto' must clear to inherit", step)
    print("  scalar overrides: junk REFUSED on add/edit, auto/blank clears to inherit (N2): OK")


def test_badge_templates_render_real_pct_for_fuzzy():
    # N5 (de-vacuum'd): pin the M2 badge fix by asserting the EXACT badge EXPRESSION that
    # renders the real pct for a fuzzy soft lock -- not loose substrings like "fuzzy" or
    # "tm_match_pct" that also match unrelated qa-flag spans / title= attrs / the {% set %}
    # line (so reverting a badge to unconditional "TM 100%" would leave those green).
    # Reverting either template's badge label to unconditional "TM 100%" DELETES the exact
    # expression below -> RED (mutations R18/R19, verified).
    _tpl = os.path.join(_ROOT, "app", "templates")
    with open(os.path.join(_tpl, "editor.html"), encoding="utf-8") as _fh:
        editor = _fh.read()
    with open(os.path.join(_tpl, "project_keys.html"), encoding="utf-8") as _fh:
        pkeys = _fh.read()
    assert "TM {{ r.seg.tm_match_pct }}%" in editor, (
        "editor.html must render the badge label as 'TM {{ r.seg.tm_match_pct }}%' for a "
        "fuzzy soft lock (a fuzzy fill is NOT 'TM 100%')"
    )
    # BOTH badge sites (the editable-value row AND the plural-line row) must carry the
    # real-pct expression -- a per-file `in` check lets one badge silently revert to
    # "TM 100%" while the other keeps the pin green (post-merge gate R19 boundary probe).
    assert pkeys.count("TM {{ v.tm_match_pct }}%") == 2, (
        "project_keys.html must render the badge label as 'TM {{ v.tm_match_pct }}%' at "
        "BOTH badge sites for a fuzzy soft lock (a fuzzy fill is NOT 'TM 100%')"
    )
    print("  badge: both templates render 'TM <real pct>%' for a fuzzy lock (N5): OK")


def main():
    test_used_tm_match_marker_default_and_backfill()
    test_used_tm_match_stamped_segmented_and_pure_llm()
    test_effective_policy_resolution()
    test_seeding_snapshots_globals()
    test_threshold_100_fills_exact_only_byte_identical()
    test_threshold_85_fills_fuzzy_with_real_pct()
    test_number_parity_blocks_poisoned_fuzzy_fill()
    test_heal_branch_unit_exact_only()
    test_heal_branch_orchestrator_two_floor()
    test_lock_mode_none_no_lock_overwritten_by_translate()
    test_lock_mode_none_survives_tm_only()
    test_none_mode_protected_from_tm_protect_correction()
    test_lock_mode_soft_and_policy()
    test_hard_fuzzy_policy_refused_at_write_seam()
    test_tm_locks_disabled_whole_noop()
    test_pool_selection_narrows_and_provenance()
    test_pool_selection_run_drops_unreadable_and_notes()
    test_empty_pool_selection_roundtrip_and_note()
    test_junk_scalar_refused_clear_works()
    test_dead_fields_refused_and_type_switch_clears()
    test_cli_show_row_and_package_view_carry_config()
    test_mcp_field_allowlist_carries_config()
    test_builder_forms_carry_every_tm_field()
    test_web_roundtrip_config()
    test_badge_templates_render_real_pct_for_fuzzy()
    test_fuzzy_band_priced_and_dispatched_honestly()
    test_key_files_fuzzy_lock_round_trips()
    test_short3_restore_backcompat()
    test_fuzzy_soft_lock_survives_pause_resume()
    print("TM MATCH STEP CONFIG OK")


if __name__ == "__main__":
    main()
