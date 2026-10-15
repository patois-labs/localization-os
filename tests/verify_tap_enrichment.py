"""Prompt Tap RUN ENVELOPES -- Wave B suite (PROPOSAL-prompt-tap-run-envelopes §5).

Proves the Wave B "enrichment" contracts -- the reserved JSON columns Wave A left NULL are
now populated at the finalize seam, as a pure OBSERVER over the run's own persisted state:

  MAN   MANIFEST POPULATED: a real workflow run and a real direct-MT pass both write
        TapAction.manifest with the TM sources, glossaries, and style guide in play --
        IDENTITY + CONTENT-HASH only (ids/names/hashes), never a full-content snapshot
        (D5). The delivered term/style text never appears in the manifest.
  ROLL  ROLL-UP HONESTY: TapAction.totals sums UPWARD from the runs and TapRun.detail sums
        from the raw call rows; total == sum(parts) at every level (the envelope cannot
        lie, §4). Attacked: re-derive the sums from the stored parts and compare. Mock
        spend is $0 (the ledger value, not an over-priced recompute).
  ADH   GLOSSARY ADHERENCE (D6): all three verdicts proven -- `honored` (LQA ran, term
        applied), `violated` (LQA ran, required target missing), `not_checked` (no LQA
        step ran). The checked/not-checked gate reads the run's own LQA StepRun (a
        RECORDED run id, §11.4), not a time window; the violation reads the SAME
        deterministic glossary check LQA's floor uses -- no new judging.
  DANG  DANGLING-REFERENCE SAFETY: after a run, deleting the referenced glossary/project
        leaves the already-written manifest readable and a re-enrich degrades to fewer
        facts (the safe direction), never a crash or a wrong join. (The Wave A review
        meta-finding: every new unenforced reference is safe to read back.)
  PURE  OBSERVER PURITY: an induced enrichment failure never fails (or perturbs) a
        translation, leaves the Wave A envelope intact (detail NULL), and WARNs once (the
        outage lesson) rather than debug-swallowing.
  TERM  NON-TERMINAL leg is not enriched: a paused run's detail stays NULL until the
        resume that finishes it.

Fixtures drive the REAL orchestrator / translate paths. Hermetic: throwaway
LOC_TMS_DATA_DIR + cleared provider keys; ASCII-only prints.
"""

import json
import logging
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_tapenrich_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    GlossaryEntry,
    Project,
    Segment,
    SegStatus,
    StepType,
    StyleGuide,
    StyleGuideStatus,
    TapAction,
    TapRun,
    TapStatus,
    TransMode,
    Workflow,
    WorkflowBatch,
    WorkflowStep,
)
from app.services import (
    batch_runs,
    orchestrator,
    prompt_tap,
    settings,
    tap_enrich,
    tap_envelope,
    translate,
)

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
        for row in s.exec(select(TapRun)).all():
            s.delete(row)
        for row in s.exec(select(TapAction)).all():
            s.delete(row)
        s.commit()
    settings.set_many(
        {
            "prompt_tap_enabled": "1",
            "prompt_tap_envelopes_enabled": "1",
            "lqa_glossary_adjudication_enabled": "1",
        }
    )
    tap_enrich._WARNED = False
    import app.services.tap_envelope as te

    te._WARNED = False


def _mk_project(s, legacy_term=None, style_body=None):
    p = Project(name="enr-%d" % _seq(), source_locale="en-US", default_provider="mock")  # noqa: UP031
    s.add(p)
    s.commit()
    s.refresh(p)
    if legacy_term is not None:
        src_t, tgt_t = legacy_term
        s.add(
            GlossaryEntry(
                project_id=p.id,
                source_term=src_t,
                target_term=tgt_t,
                source_locale="en-US",
                target_locale="es-ES",
            )
        )
        s.commit()
    if style_body is not None:
        s.add(
            StyleGuide(
                name="sg-%d" % _seq(),  # noqa: UP031
                status=StyleGuideStatus.APPROVED,
                org_id=p.org_id,
                customer_id=p.customer_id,
                body=style_body,
                source_locale="en-US",
                target_locale="es-ES",
            )
        )
        s.commit()
    return p


def _mk_doc(s, project, sources):
    doc = Document(project_id=project.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, txt in enumerate(sources):
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=i,
                source_text=txt,
                status=SegStatus.NEW,
            )
        )
    s.commit()
    return doc


def _mk_multiloc_doc(s, project, sources, locales):
    doc = Document(project_id=project.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
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


def _mk_workflow(s, with_lqa=False):
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
            prompt_text="Translate: {text} {glossary}",
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
            key="translate",
        )
    )
    if with_lqa:
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=1,
                name="LQA",
                provider="mock",
                step_type=StepType.LQA,
                prompt_text="{text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
                key="lqa",
            )
        )
    s.commit()
    return wf


def _one_action_run():
    with Session(engine) as s:
        a = s.exec(select(TapAction)).all()[-1]
        r = s.exec(select(TapRun).where(TapRun.action_id == a.id)).all()[-1]
        return a, r


# --- MAN: manifest populated (identity+hash, not snapshot) -------------------
def test_manifest_populated_and_hashed():
    _reset()
    style_body = "SECRETSTYLEBODY-always-use-formal-register"
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"), style_body=style_body)
        doc = _mk_doc(s, proj, ["Open Settings now.", "Another line."])
        wf = _mk_workflow(s, with_lqa=True)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "completed", summ

    a, r = _one_action_run()
    # Per-locale manifest (M5): the action carries a by_locale map; the leg's own manifest
    # is also in its run.detail, tagged with its target_locale.
    assert a.manifest is not None, "manifest must be populated after a terminal run"
    assert a.manifest["source_locale"] == "en-US", a.manifest
    assert "es-ES" in a.manifest["by_locale"], a.manifest
    man = a.manifest["by_locale"]["es-ES"]
    assert r.detail["manifest"]["target_locale"] == "es-ES", r.detail["manifest"]
    # TM: own project source with a content-version hash.
    assert man["tm"] and man["tm"]["sources"], man["tm"]
    own = [x for x in man["tm"]["sources"] if x["own"]]
    assert own and own[0]["content_hash"], own
    # Glossary: the legacy per-project termbase recorded by identity + hash.
    gl = man["glossaries"]
    assert gl and any(x["legacy"] for x in gl), gl
    leg = [x for x in gl if x["legacy"]][0]  # noqa: RUF015
    assert leg["content_hash"] and leg["term_count"] == 1, leg
    # Style guide: id/name/hash/version, NOT the body.
    assert man["style_guide"] and man["style_guide"]["content_hash"], man["style_guide"]
    # IDENTITY + HASH ONLY (D5): no full content snapshot anywhere in the manifest.
    blob = json.dumps(a.manifest)
    assert style_body not in blob, "style guide BODY must not be snapshotted into manifest"
    assert "Ajustes" not in blob and "Settings" not in blob, (
        "glossary term text must not be snapshotted into manifest (hash only)"
    )
    print("  (MAN) manifest: TM/glossary/style recorded by id+hash, no content snapshot OK")


# --- ROLL: roll-up honesty (total == sum(parts), mock spend = ledger $0) ------
def test_rollup_honesty_totals_agree_with_children():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now.", "Two.", "Three."])
        wf = _mk_workflow(s, with_lqa=True)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")

    a, r = _one_action_run()
    d, t = r.detail, a.totals
    assert d and t, (d, t)
    # detail.rollup == sum over the stored raw call rows (attack the aggregate).
    rows = d["calls"]
    roll = d["rollup"]
    assert roll["calls"] == len(rows), (roll["calls"], len(rows))
    assert roll["spend_usd"] == round(sum(x["spend_usd"] for x in rows), 8), roll
    assert roll["tokens_out"] == sum(x["output_tokens"] for x in rows), roll
    # Mock is exempt -> spend is the LEDGER $0, not an over-priced usage recompute.
    assert roll["spend_usd"] == 0.0, ("mock spend must be ledger $0", roll["spend_usd"])
    assert roll["spend_source"] == "ledger_per_call", roll["spend_source"]
    # totals == sum over the action's runs (the parts stored in by_run).
    assert t["calls"] == sum(b["calls"] for b in t["by_run"]) == roll["calls"], t
    assert t["spend_usd"] == round(sum(b["spend_usd"] for b in t["by_run"]), 8), t
    assert t["tokens_out"] == sum(b["tokens_out"] for b in t["by_run"]), t
    assert t["segments"] == 3, t
    # A tampered child must make the stored aggregate DETECTABLY wrong (proves the
    # invariant is a real check, not a coincidence).
    rows[0]["spend_usd"] += 1.0
    assert roll["spend_usd"] != round(sum(x["spend_usd"] for x in rows), 8), (
        "the roll-up must be a verifiable sum of its parts"
    )
    print("  (ROLL) roll-up: totals == sum(parts) at run+action level, mock spend ledger $0 OK")


# --- ADH: adherence honored + not_checked (real runs) ------------------------
def test_adherence_honored_and_not_checked():
    # honored: an LQA step ran and mock applied the term.
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now."])
        wf = _mk_workflow(s, with_lqa=True)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    _, r = _one_action_run()
    adh = r.detail["adherence"]
    assert adh["lqa_step_ran"] is True, adh
    assert adh["honored"] == 1 and adh["violated"] == 0 and adh["not_checked"] == 0, adh
    assert adh["terms"][0]["status"] == "honored", adh

    # not_checked: the SAME term, but no LQA step in the workflow.
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now."])
        wf = _mk_workflow(s, with_lqa=False)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    _, r = _one_action_run()
    adh = r.detail["adherence"]
    assert adh["lqa_step_ran"] is False, adh
    assert adh["not_checked"] == 1 and adh["honored"] == 0, adh
    assert adh["terms"][0]["status"] == "not_checked", adh
    print("  (ADH) adherence: honored (LQA+applied) and not_checked (no LQA step) OK")


# --- ADH: adherence violated (real LQA StepRun + tampered delivery, re-enrich) -
def test_adherence_violated_via_reenrich():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now."])
        wf = _mk_workflow(s, with_lqa=True)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    wf_run_id = summ["run_id"]
    a, r = _one_action_run()
    assert r.detail["adherence"]["terms"][0]["status"] == "honored"

    # Tamper the DELIVERED target so the required term is now missing, then re-enrich:
    # the LQA StepRun still exists (checked), and the deterministic glossary check now
    # flags the term as violated on that segment.
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()[0]
        seg.target_text = "abrir configuracion ahora"  # no "Ajustes"
        s.add(seg)
        s.commit()
    tap_enrich.enrich(
        a.id, r.id, workflow_run_id=wf_run_id, document_id=doc_id, target_locale="es-ES"
    )
    with Session(engine) as s:
        r2 = s.get(TapRun, r.id)
    adh = r2.detail["adherence"]
    assert adh["lqa_step_ran"] is True, adh
    assert adh["violated"] == 1 and adh["honored"] == 0, adh
    t = adh["terms"][0]
    assert t["status"] == "violated" and t["violated_segment_ids"], t
    print("  (ADH) adherence: violated verdict on a missing required target (LQA ran) OK")


# --- ADH: the three-state join as a deterministic unit ------------------------
class _Seg:
    def __init__(self, sid, src, tgt):
        self.id, self.source_text, self.target_text, self.order_index = sid, src, tgt, sid


def test_adherence_unit_three_states():
    segs = [
        _Seg(1, "Open Settings.", "Abrir Ajustes."),
        _Seg(2, "More Settings here.", "Mas cosas aqui."),
    ]  # term missing in seg 2
    matched = [
        {
            "source": "Settings",
            "expected_target": "Ajustes",
            "dnt": False,
            "sense_label": None,
            "segment_ids": [1, 2],
        }
    ]
    # No LQA -> not_checked regardless of the text.
    a0 = tap_enrich._adherence(segs, matched, lqa_step_ran=False)
    assert a0["not_checked"] == 1 and a0["terms"][0]["status"] == "not_checked", a0
    # LQA ran -> honored on seg 1, violated on seg 2 -> overall violated (any miss).
    a1 = tap_enrich._adherence(segs, matched, lqa_step_ran=True)
    assert a1["violated"] == 1, a1
    assert a1["terms"][0]["violated_segment_ids"] == [2], a1
    # LQA ran + term present everywhere -> honored.
    segs_ok = [_Seg(1, "Open Settings.", "Abrir Ajustes.")]
    matched_ok = [
        {
            "source": "Settings",
            "expected_target": "Ajustes",
            "dnt": False,
            "sense_label": None,
            "segment_ids": [1],
        }
    ]
    a2 = tap_enrich._adherence(segs_ok, matched_ok, lqa_step_ran=True)
    assert a2["honored"] == 1 and a2["terms"][0]["status"] == "honored", a2
    print("  (ADH) adherence unit: honored/violated/not_checked deterministic OK")


# --- MAN: direct-MT pass gets a manifest + detail too ------------------------
def test_direct_mt_enrichment():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now.", "Two."])
        doc_id = doc.id
    with Session(engine) as s:
        for _ in translate.pretranslate_stream(s, doc_id, "es-ES"):
            pass
    a, r = _one_action_run()
    assert a.manifest and a.manifest["by_locale"]["es-ES"]["glossaries"], a.manifest
    assert a.totals and a.totals["calls"] == len(r.detail["calls"]), (a.totals, r.detail)
    # No LQA on the direct path -> matched terms are not_checked (honest).
    adh = r.detail["adherence"]
    assert adh["lqa_step_ran"] is False and adh["not_checked"] >= 1, adh
    print("  (MAN) direct MT: manifest + detail + not_checked adherence OK")


# --- DANG: a deleted asset is safe to read back / re-enrich ------------------
def test_dangling_asset_read_and_reenrich_safe():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"), style_body="BODY")
        doc = _mk_doc(s, proj, ["Open Settings now."])
        wf = _mk_workflow(s, with_lqa=True)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    wf_run_id = summ["run_id"]
    a, r = _one_action_run()
    assert a.manifest["by_locale"]["es-ES"]["glossaries"], a.manifest

    # Delete the referenced glossary + style guide (the assets the manifest names).
    with Session(engine) as s:
        for ge in s.exec(select(GlossaryEntry)).all():
            s.delete(ge)
        for sg in s.exec(select(StyleGuide)).all():
            s.delete(sg)
        s.commit()

    # The already-written manifest is still readable (stored ids/hashes, no live join).
    with Session(engine) as s:
        a2 = s.get(TapAction, a.id)
        assert a2.manifest is not None and json.dumps(a2.manifest), "manifest still readable"

    # Re-enrich after the delete degrades the per-leg DETAIL to fewer facts (the safe
    # direction) and never crashes. The action MANIFEST is write-once (a stable historical
    # record of what the run used), so it still names the now-deleted assets by their
    # stored id+hash -- which is exactly the dangling-safe property: reading a manifest
    # whose asset ids no longer resolve is safe (they are inert stored data, never joined).
    man_before = json.dumps(a2.manifest, sort_keys=True)
    tap_enrich.enrich(
        a.id, r.id, workflow_run_id=wf_run_id, document_id=doc_id, target_locale="es-ES"
    )
    with Session(engine) as s:
        r2 = s.get(TapRun, r.id)
        a3 = s.get(TapAction, a.id)
        assert r2.detail is not None, "re-enrich must still write detail"
        # No glossary in scope now -> matched terms empty, adherence empty (not a crash).
        assert r2.detail["glossary"]["count"] == 0, r2.detail["glossary"]
        # The stable manifest still records the deleted style guide's identity, readable.
        assert json.dumps(a3.manifest, sort_keys=True) == man_before, (
            "the write-once manifest is a stable historical record, unchanged by a delete"
        )
        assert a3.manifest["by_locale"]["es-ES"]["style_guide"] is not None, (
            "the deleted style guide's identity+hash stays readable in the manifest"
        )
    print("  (DANG) dangling asset: manifest stable+readable, detail degrades safely OK")


# --- PURE: induced enrichment failure never perturbs the translation ---------
class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_observer_purity_enrich_failure():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now.", "Two."])
        wf = _mk_workflow(s, with_lqa=True)
        wf_id, doc_id = wf.id, doc.id

    orig = tap_enrich._session
    tap_enrich._session = lambda: (_ for _ in ()).throw(RuntimeError("induced enrich fail"))
    logger = logging.getLogger("loc_tms.tap_enrich")
    handler = _ListHandler()
    logger.addHandler(handler)
    try:
        with Session(engine) as s:
            summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        # The translation SUCCEEDED despite every enrich write failing.
        assert summ["status"] == "completed", summ
        with Session(engine) as s:
            done = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
            assert all(x.target_text for x in done), "all segments translated"
        # Wave A envelope intact + terminal; enrich's OWN outputs (per-leg detail + the
        # action manifest) stayed NULL. Action TOTALS, by contrast, are rolled up at the
        # finalize seam over the call file on the CALLER's session (H1) -- decoupled from
        # enrich -- so they survive an enrich failure (the robust direction: a reader still
        # gets honest calls/spend even when per-leg enrichment could not be written).
        a, r = _one_action_run()
        assert a.status == TapStatus.COMPLETED and r.detail is None, (a.status, r.detail)
        assert a.manifest is None, ("enrich's manifest write stayed NULL", a.manifest)
        assert a.totals is not None and a.totals["calls"] >= 1, (
            "totals are rolled up independently of enrich (H1)",
            a.totals,
        )
        # The failure was VISIBLE at WARNING, not debug-swallowed (the outage lesson).
        assert any(rec.levelno == logging.WARNING for rec in handler.records), (
            "an enrichment failure must WARN, not be silent"
        )
    finally:
        tap_enrich._session = orig
        logger.removeHandler(handler)
    print("  (PURE) observer purity: enrich failure -> run OK, detail NULL, one WARNING OK")


# --- TERM: a non-terminal (paused) leg is not enriched -----------------------
def _mk_pause_workflow(s):
    wf = Workflow(name="wfp-%d" % _seq(), scope="global")  # noqa: UP031
    s.add(wf)
    s.commit()
    s.refresh(wf)
    plan = [(StepType.LLM_AGENT, "t1"), (StepType.HUMAN_REVIEW, "r1"), (StepType.LLM_AGENT, "t2")]
    for i, (kind, key) in enumerate(plan):
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=i,
                name=key,
                provider="mock",
                step_type=kind,
                prompt_text="Translate: {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
                key=key,
            )
        )
    s.commit()
    return wf


def test_non_terminal_leg_not_enriched():
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now."])
        wf = _mk_pause_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-ES"))
    # It paused at the human_review step -> the leg is non-terminal.
    a, r = _one_action_run()
    assert r.status == TapStatus.PAUSED, ("run should pause", r.status)
    assert r.detail is None, ("a paused leg must NOT be enriched yet", r.detail)
    # Resume to completion -> now terminal -> enriched.
    with Session(engine) as s:
        orchestrator.resume_workflow(s, r.workflow_run_id, "es-ES", "keep")
    with Session(engine) as s:
        r2 = s.get(TapRun, r.id)
        a2 = s.get(TapAction, a.id)
        assert r2.status == TapStatus.COMPLETED, r2.status
        assert r2.detail is not None and a2.totals is not None, (
            "the resume that finishes the run must enrich it"
        )
    print("  (TERM) non-terminal leg not enriched; the finishing resume enriches it OK")


# --- GRAIN (proposal D1): one translate CLICK = one TapAction, one TapRun per pair ---
def test_batch_one_action_n_runs():
    """A 2-locale batch (ONE click) yields exactly ONE TapAction with TWO TapRuns -- not
    two actions (the Wave A deviation this fix corrects). The action carries ALL requested
    locales, finalizes terminal only after every leg is done, and its totals roll up
    across BOTH legs. All the legs' calls live in the ONE per-action file (§6)."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_multiloc_doc(s, proj, ["Open Settings now.", "Second line."], ["es-ES", "fr-FR"])
        wf = _mk_workflow(s, with_lqa=True)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf_id, doc_id=doc_id, locales=["es-ES", "fr-FR"], synchronous=True
        )
    assert batch.status == "completed", batch.status

    with Session(engine) as s:
        actions = s.exec(select(TapAction)).all()
        assert len(actions) == 1, (
            "ONE action per click (D1), not one per locale",
            [(a.id, a.batch_id, a.requested_locales) for a in actions],
        )
        a = actions[0]
        assert a.batch_id is not None, a
        assert sorted(a.requested_locales) == ["es-ES", "fr-FR"], (
            "the action lists ALL requested locales",
            a.requested_locales,
        )
        assert a.status == TapStatus.COMPLETED and a.finished_at is not None, a
        runs = s.exec(select(TapRun).where(TapRun.action_id == a.id)).all()
        assert len(runs) == 2, ("one TapRun per language pair", len(runs))
        assert sorted(r.target_locale for r in runs) == ["es-ES", "fr-FR"], runs
        assert all(r.status == TapStatus.COMPLETED for r in runs), [r.status for r in runs]
        assert all(r.detail is not None for r in runs), "each leg enriched"
        # Totals roll up across BOTH legs and agree with the stored per-run parts.
        t = a.totals
        assert t["runs"] == 2, t
        assert t["calls"] == sum(len(r.detail["calls"]) for r in runs), t
        assert t["calls"] == sum(b["calls"] for b in t["by_run"]), t
        assert t["segments"] == sum(r.detail["segments"] for r in runs), t
        # ONE per-action file holds BOTH legs' calls, each stamped with its own run_id.
        recs = list(prompt_tap._iter_file(prompt_tap._action_path(a.id)))
        run_ids = {(rec.get("meta") or {}).get("run_id") for rec in recs}
        assert run_ids == {r.id for r in runs}, (
            "both legs' calls share the one per-action file, tagged per run",
            run_ids,
        )
    print("  (GRAIN) 2-locale batch: 1 TapAction + 2 TapRuns, totals across both, 1 file OK")


def test_single_locale_run_is_one_plus_one():
    """A single-locale, non-batch run stays 1 action + 1 run (unchanged), and a
    single-locale BATCH is likewise 1 action + 1 run -- the grain fix does not inflate the
    simple case."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_doc(s, proj, ["One."])
        wf = _mk_workflow(s, with_lqa=False)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    with Session(engine) as s:
        acts = s.exec(select(TapAction)).all()
        assert len(acts) == 1 and acts[0].batch_id is None, acts
        assert len(s.exec(select(TapRun)).all()) == 1
        assert acts[0].requested_locales == ["es-ES"], acts[0].requested_locales
    print("  (GRAIN) single-locale run stays 1 action + 1 run OK")


# --- MODEL: an engine-profile call stamps the RESOLVED model, not null ------
def test_engine_profile_call_stamps_resolved_model():
    """An `engine:<id>` (openai_compatible) call has a BLANK spec model; the resolved
    model lives on the provider's EngineProfile. The tap record must stamp that resolved
    model, not null -- so a reader can tell WHICH model ran (§1 provenance-complete)."""
    _reset()
    from app.services import providers

    class _FakeProfile:
        model = "deepseek/deepseek-chat"

    class _FakeEngineProv:
        name = "openai_compatible"
        profile = _FakeProfile()
        last_usage = None

        def complete(self, prompt, system=None, task=None, payload=None, model=None, **kw):
            self.last_usage = {"input_tokens": 4, "output_tokens": 3}
            return "traducido"

    orig = providers.get_provider
    set_pieces("app.services.providers", "get_provider", lambda name=None: _FakeEngineProv())
    try:
        spec = providers.ProviderSpec(provider="engine:1", model="", retry_max=0)
        res = providers.run_completion(
            spec,
            "Hola",
            system="s",
            task="translate",
            payload={"text": "Hola"},
            tap_meta={"document_id": 1},
        )
        assert res["text"] == "traducido", res
    finally:
        set_pieces("app.services.providers", "get_provider", orig)
    recs = _global_records()
    assert recs, "the engine-profile call must have captured a record"
    assert any(r.get("model") == "deepseek/deepseek-chat" for r in recs), (
        "the resolved model must be stamped, not null",
        [r.get("model") for r in recs],
    )
    print("  (MODEL) engine-profile call stamps the resolved model id, not null OK")


def _global_records():
    return list(prompt_tap._iter_file(prompt_tap._path()))


def test_batch_resume_does_not_fork_action():
    """A batch leg's RESUME reuses its existing (action, run) rather than forking a second
    action -- the grain must survive a pause/resume. Proven at the envelope seam over REAL
    ids: a batch action created eagerly for a real project/document (the shape start_batch
    actually writes -- not document_id=None + fabricated ids), a leg's fresh run attached,
    then a resume returns the SAME pair and adds no action/run. The workflow_run_ids are
    real WorkflowRun rows so the resume lookup validates on genuine (document, locale)
    identity (B1)."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s)
        doc = _mk_multiloc_doc(s, proj, ["One.", "Two."], ["es-ES", "fr-FR"])
        wf = _mk_workflow(s)
        pid, doc_id, wf_id, org_id = proj.id, doc.id, wf.id, proj.org_id
        # Real WorkflowRun rows so the leg's workflow_run_id is a genuine id, not fabricated.
        from app.models import RunStatus, WorkflowRun

        wr_es = WorkflowRun(
            workflow_id=wf_id, document_id=doc_id, target_locale="es-ES", status=RunStatus.RUNNING
        )
        wr_fr = WorkflowRun(
            workflow_id=wf_id, document_id=doc_id, target_locale="fr-FR", status=RunStatus.RUNNING
        )
        s.add(wr_es)
        s.add(wr_fr)
        s.commit()
        s.refresh(wr_es)
        s.refresh(wr_fr)
        es_run, fr_run = wr_es.id, wr_fr.id
    ident = dict(project_id=pid, document_id=doc_id, org_id=org_id)  # noqa: C408
    aid = tap_envelope.begin_batch_action(
        batch_id=777, requested_locales=["es-ES", "fr-FR"], **ident
    )
    assert aid, aid
    # A fresh es-ES leg attaches its run to the batch action.
    a1, r1 = tap_envelope.begin_workflow_envelope(
        workflow_run_id=es_run, is_resume=False, batch_id=777, target_locale="es-ES", **ident
    )
    assert a1 == aid, (a1, aid)
    # A second locale leg -> SAME action, a NEW run under it.
    a2, r2 = tap_envelope.begin_workflow_envelope(
        workflow_run_id=fr_run, is_resume=False, batch_id=777, target_locale="fr-FR", **ident
    )
    assert a2 == aid and r2 != r1, (a2, r2, r1)
    # RESUME of the es-ES leg reuses (a1, r1); no fork.
    a3, r3 = tap_envelope.begin_workflow_envelope(
        workflow_run_id=es_run, is_resume=True, batch_id=777, target_locale="es-ES", **ident
    )
    assert (a3, r3) == (a1, r1), ((a3, r3), (a1, r1))
    with Session(engine) as s:
        assert len(s.exec(select(TapAction).where(TapAction.batch_id == 777)).all()) == 1
        assert len(s.exec(select(TapRun).where(TapRun.action_id == aid)).all()) == 2
    print("  (GRAIN) batch resume reuses the leg's (action,run); no fork OK")


# --- B1: rowid-reused batch_id must not hijack another tenant's action --------
def test_b1_cross_tenant_batch_action_no_hijack():
    """B1 (BLOCKER): WorkflowBatch.id is a SQLite rowid alias -> REUSED after a batch is
    deleted. Tenant B's new batch that inherits tenant A's deleted batch_id must NOT attach
    its TapRun to (or otherwise hijack) tenant A's TapAction -- which would append B's
    captures to A's per-action file and render them under A's project/doc/customer.

    Modeled at the envelope seam: tenant A owns a batch action for a reused batch_id;
    tenant B's leg calls begin_workflow_envelope with the SAME batch_id but B's OWN tenant
    identity -> it must mint a FRESH action, and A's action must gain no run."""
    _reset()
    REUSE = 424242
    a_ident = dict(project_id=100, document_id=200, customer_id=1, org_id=1)  # noqa: C408
    b_ident = dict(project_id=300, document_id=400, customer_id=2, org_id=2)  # noqa: C408
    aid_a = tap_envelope.begin_batch_action(batch_id=REUSE, requested_locales=["es-ES"], **a_ident)
    assert aid_a, aid_a
    # Tenant B (rowid reuse): a leg with the SAME batch_id but B's identity.
    a_b, _r_b = tap_envelope.begin_workflow_envelope(
        workflow_run_id=9001, is_resume=False, batch_id=REUSE, target_locale="de-DE", **b_ident
    )
    assert a_b and a_b != aid_a, ("tenant B must NOT adopt tenant A's action", a_b, aid_a)
    with Session(engine) as s:
        a_runs = s.exec(select(TapRun).where(TapRun.action_id == aid_a)).all()
        assert len(a_runs) == 0, ("tenant A's action must gain no run from B", a_runs)
        act_b = s.get(TapAction, a_b)
        assert act_b.org_id == 2 and act_b.project_id == 300, act_b
    # Identity-validated lookup returns each tenant its OWN action, never the other's.
    assert tap_envelope.find_batch_action(REUSE, **a_ident) == aid_a
    assert tap_envelope.find_batch_action(REUSE, **b_ident) == a_b
    print("  (B1) rowid-reused batch_id: tenant B mints its own action; A not hijacked OK")


# --- H1: action totals summed from the CALL FILE, not stale sibling detail ----
def _cap(aid, rid, n, outcome="ok"):
    for i in range(n):
        prompt_tap.capture(
            prompt_tap.build_record(
                provider="mock",
                provider_kind="mock",
                model="m",
                task="translate",
                system="s",
                prompt="p%d" % i,  # noqa: UP031
                max_tokens=8,
                payload={"text": "x"},
                outcome=outcome,
                response_text="y",
                cost=0.0,
                tap_meta={"action_id": aid, "run_id": rid},
            )
        )


def test_h1_totals_from_callfile_not_stale_sibling_detail():
    """H1 (HIGH): action totals must sum from the per-action CALL FILE (complete for every
    terminal run) -- NOT from a sibling TapRun.detail a concurrent leg may not have written
    yet (the Wave B last-writer-wins under-count). Deterministic repro: two terminal runs,
    calls in the file for BOTH, but detail written for only ONE.
    finalize_action_if_all_runs_done must still count BOTH legs' calls."""
    _reset()
    from app.models import TapAction, TapActionKind

    with Session(engine) as s:
        a = TapAction(
            id=tap_envelope.new_id(),
            kind=TapActionKind.WORKFLOW,
            batch_id=555,
            requested_locales=["es-ES", "fr-FR"],
            status=TapStatus.RUNNING,
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
        r1 = TapRun(
            id=tap_envelope.new_id(),
            action_id=aid,
            target_locale="es-ES",
            status=TapStatus.COMPLETED,
        )
        r2 = TapRun(
            id=tap_envelope.new_id(),
            action_id=aid,
            target_locale="fr-FR",
            status=TapStatus.COMPLETED,
        )
        s.add(r1)
        s.add(r2)
        s.commit()
        s.refresh(r1)
        s.refresh(r2)
        rid1, rid2 = r1.id, r2.id
    _cap(aid, rid1, 2)  # es-ES: 2 calls in the file
    _cap(aid, rid2, 3)  # fr-FR: 3 calls in the file
    # Only run1 has detail written (run2's enrichment "hasn't landed" yet).
    with Session(engine) as s:
        r1 = s.get(TapRun, rid1)
        r1.detail = {
            "rollup": {
                "calls": 2,
                "failures": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "chars": 0,
                "spend_usd": 0.0,
            },
            "segments": 2,
        }
        s.add(r1)
        s.commit()
    tap_envelope.finalize_action_if_all_runs_done(aid)
    with Session(engine) as s:
        a = s.get(TapAction, aid)
        assert a.status == TapStatus.COMPLETED and a.finished_at is not None, a.status
        t = a.totals
        assert t is not None, "totals must be rolled up at the finalize seam (H1)"
        assert t["runs"] == 2, t
        assert t["calls"] == 5, ("totals must count BOTH legs' calls from the file", t)
        by = {b["run_id"]: b["calls"] for b in t["by_run"]}
        assert by[rid1] == 2 and by[rid2] == 3, by
    print(
        "  (H1) action totals summed from the call file across ALL legs (no stale sibling read) OK"
    )


# --- M1: a zero-leg batch action is closed ABANDONED, not stuck running -------
def test_m1_zero_leg_batch_action_abandoned():
    """M1 (MEDIUM): a batch that launches NO member run (spend gate / fail-fast / all
    already-running) still created its TapAction eagerly. finalize_batch_action (the
    dispatcher's post-join hook) must close that zero-run action as ABANDONED rather than
    leaving it 'running' forever."""
    _reset()
    from app.models import TapAction

    ident = dict(project_id=101, document_id=202, org_id=1)  # noqa: C408
    aid = tap_envelope.begin_batch_action(
        batch_id=888, requested_locales=["es-ES", "fr-FR"], **ident
    )
    assert aid, aid
    with Session(engine) as s:
        assert s.get(TapAction, aid).status == TapStatus.RUNNING
        assert not s.exec(select(TapRun).where(TapRun.action_id == aid)).all()
    tap_envelope.finalize_batch_action(888, **ident)
    with Session(engine) as s:
        a = s.get(TapAction, aid)
        assert a.status == TapStatus.ABANDONED and a.finished_at is not None, a.status
    print("  (M1) zero-leg batch action closed ABANDONED (not stuck running) OK")


# --- M2: a paused leg surfaces the action as paused; resume moves it onward ---
def test_m2_paused_leg_surfaces_paused_action():
    """M2 (MEDIUM): a PAUSED leg must map the action to PAUSED (so the viewer's `paused`
    filter can match it), not leave it stuck RUNNING. A resume flips it running -> terminal
    as the leg proceeds."""
    _reset()
    from app.models import TapAction, TapActionKind

    with Session(engine) as s:
        a = TapAction(
            id=tap_envelope.new_id(),
            kind=TapActionKind.WORKFLOW,
            requested_locales=["es-ES"],
            status=TapStatus.RUNNING,
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
        r = TapRun(
            id=tap_envelope.new_id(), action_id=aid, target_locale="es-ES", status=TapStatus.PAUSED
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        rid = r.id
    tap_envelope.finalize_action_if_all_runs_done(aid)
    with Session(engine) as s:
        assert s.get(TapAction, aid).status == TapStatus.PAUSED, (
            "a paused leg must surface a PAUSED action (M2)"
        )
    # The action tracks its leg's status idempotently as it changes: a leg reaching a
    # terminal state rolls the action up. (The REAL paused->running RESUME transition is
    # covered end-to-end by test_n7 via the actual resume seam, not a hand-set status.)
    with Session(engine) as s:
        r = s.get(TapRun, rid)
        r.status = TapStatus.COMPLETED
        s.add(r)
        s.commit()
    tap_envelope.finalize_action_if_all_runs_done(aid)
    with Session(engine) as s:
        a = s.get(TapAction, aid)
        assert a.status == TapStatus.COMPLETED and a.finished_at is not None, a.status
    print("  (M2) paused leg -> paused action; leg finishing -> completed action OK")


# --- M4: the model grain docs match the shipped one-action-per-batch grain ----
def test_m4_models_doc_matches_shipped_grain():
    """M4 (MEDIUM): the TapAction/TapRun model docs must describe the SHIPPED grain (ONE
    action per batch CLICK, N runs) -- not the inverted 'per-locale actions grouped by
    batch_id' that find_batch_action's invariant contradicts."""
    import app.models as _m

    with open(_m.__file__, encoding="utf-8") as fh:
        src = fh.read()
    for stale in (
        "groups a batch's several actions by",
        "spawned several actions",
        "one TapAction with its single TapRun",
    ):
        assert stale not in src, f"stale grain doc still present: {stale!r}"
    print("  (M4) models grain docs match the shipped one-action-per-batch grain OK")


# --- M5: a multi-locale action records per-locale manifests for EVERY locale --
def test_m5_per_locale_manifest_records_every_locale():
    """M5 (MEDIUM): a multi-locale batch action must record EVERY locale's assets, not one
    arbitrary leg's. A 2-locale batch: action.manifest.by_locale carries BOTH locales, and
    each run.detail.manifest carries its own target_locale (the per-run, race-free store)."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_multiloc_doc(s, proj, ["Open Settings now."], ["es-ES", "fr-FR"])
        wf_id, doc_id = _mk_workflow(s, with_lqa=True).id, doc.id
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf_id, doc_id=doc_id, locales=["es-ES", "fr-FR"], synchronous=True
        )
    assert batch.status == "completed", batch.status
    with Session(engine) as s:
        a = s.exec(select(TapAction)).all()[-1]
        assert set(a.manifest["by_locale"].keys()) == {"es-ES", "fr-FR"}, (
            "both locales' assets recorded at the action level",
            a.manifest["by_locale"],
        )
        runs = s.exec(select(TapRun).where(TapRun.action_id == a.id)).all()
        locs = {r.detail["manifest"]["target_locale"] for r in runs}
        assert locs == {"es-ES", "fr-FR"}, ("each leg records its own locale's manifest", locs)
    print("  (M5) multi-locale action records per-locale manifests for every locale OK")


# --- M6: the glossary-adherence replay is bounded + marks truncation ----------
def test_m6_enrich_replay_is_bounded():
    """M6 (MEDIUM): the synchronous adherence replay must be BOUNDED. With more segments
    than the cap, enrich matches only the capped subset and marks detail.enrichment_truncated
    (no silent unbounded work); below the cap it is not truncated."""
    _reset()
    orig_cap = tap_enrich.ENRICH_MATCH_SEGMENT_CAP
    tap_enrich.ENRICH_MATCH_SEGMENT_CAP = 3
    try:
        with Session(engine) as s:
            proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
            doc = _mk_doc(
                s,
                proj,
                [
                    "Open Settings now.",
                    "Settings two.",
                    "More here.",
                    "And Settings four.",
                    "Five.",
                ],
            )
            wf_id, doc_id = _mk_workflow(s, with_lqa=False).id, doc.id
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
        _, r = _one_action_run()
        assert r.detail["enrichment_truncated"] is True, (
            "over-cap replay must mark truncation",
            r.detail.get("enrichment_truncated"),
        )
    finally:
        tap_enrich.ENRICH_MATCH_SEGMENT_CAP = orig_cap
    # Below the cap: not truncated.
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now."])
        wf_id, doc_id = _mk_workflow(s, with_lqa=False).id, doc.id
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    _, r = _one_action_run()
    assert r.detail["enrichment_truncated"] is False, r.detail
    print("  (M6) enrich glossary replay bounded by the segment cap + truncation marker OK")


# --- N1: a re-run into the SAME batch re-rolls the action totals -------------------
def test_n1_rerun_rerolls_action_totals():
    """N1 (HIGH regression): a re-run dispatches FRESH member runs INTO THE SAME batch_id
    (rerun_batch), so a new leg attaches to the already-terminal batch action. Round 1 made
    finalize sticky + rolled totals only at the first terminal transition, so the re-run's
    spend stayed permanently invisible and total != sum(parts). Idempotent revival-aware
    finalize must re-roll. Driven through the REAL rerun_batch path."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_multiloc_doc(s, proj, ["Open Settings now.", "Second."], ["es-ES", "fr-FR"])
        wf_id, doc_id = _mk_workflow(s, with_lqa=True).id, doc.id
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf_id, doc_id=doc_id, locales=["es-ES", "fr-FR"], synchronous=True
        )
        batch_id = batch.id
    with Session(engine) as s:
        a = s.exec(select(TapAction).where(TapAction.batch_id == batch_id)).all()[0]
        aid = a.id
        assert a.status == TapStatus.COMPLETED, a.status
        calls0 = a.totals["calls"]
        assert a.totals["runs"] == 2, a.totals
    # Re-run es-ES INTO the same batch (a 3rd fresh leg attaches to the same action).
    with Session(engine) as s:
        batch = s.get(WorkflowBatch, batch_id)
        batch_runs.rerun_batch(s, batch=batch, locales=["es-ES"], synchronous=True)
    with Session(engine) as s:
        a = s.get(TapAction, aid)
        assert a.status == TapStatus.COMPLETED and a.finished_at is not None, a.status
        t = a.totals
        assert t["runs"] == 3, ("the re-run's leg must be counted", t["runs"])
        assert t["calls"] > calls0, ("re-run calls must become visible (N1)", t["calls"], calls0)
        assert t["calls"] == sum(b["calls"] for b in t["by_run"]), (
            "total == sum(parts) after the re-run",
            t,
        )
    print("  (N1) re-run into same batch re-rolls totals (total==sum(parts)) OK")


# --- N2: an ABANDONED zero-leg batch is revived + completed by a re-run ------------
def test_n2_abandoned_zero_leg_revived_by_rerun():
    """N2 (HIGH regression): a zero-leg batch closed ABANDONED (spend gate) must be
    RE-OPENED by a successful re-run, not stay abandoned with totals=None forever (round 1
    was strictly worse than pre-fix here). Driven through the REAL spend gate + rerun_batch."""
    _reset()
    from app.services import spend as _spend

    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_multiloc_doc(s, proj, ["Open Settings now."], ["es-ES"])
        wf_id, doc_id = _mk_workflow(s, with_lqa=False).id, doc.id
    # Trip the spend gate so the batch launches ZERO legs -> action ABANDONED (M1).
    orig = _spend.would_block
    _spend.would_block = lambda now=None: (True, 5.0, 1.0)
    try:
        with Session(engine) as s:
            batch = batch_runs.start_batch(
                s, workflow_id=wf_id, doc_id=doc_id, locales=["es-ES"], synchronous=True
            )
            batch_id = batch.id
    finally:
        _spend.would_block = orig
    with Session(engine) as s:
        a = s.exec(select(TapAction).where(TapAction.batch_id == batch_id)).all()[0]
        aid = a.id
        assert a.status == TapStatus.ABANDONED, ("zero-leg -> abandoned", a.status)
        assert not s.exec(select(TapRun).where(TapRun.action_id == aid)).all()
    # Cap raised, operator re-runs: the abandoned action must revive + complete.
    with Session(engine) as s:
        batch = s.get(WorkflowBatch, batch_id)
        batch_runs.rerun_batch(s, batch=batch, locales=["es-ES"], synchronous=True)
    with Session(engine) as s:
        a = s.get(TapAction, aid)
        assert a.status == TapStatus.COMPLETED, (
            "re-run must revive the abandoned action (N2)",
            a.status,
        )
        assert a.totals is not None and a.totals["runs"] == 1 and a.totals["calls"] >= 1, a.totals
        runs = s.exec(select(TapRun).where(TapRun.action_id == aid)).all()
        assert len(runs) == 1 and runs[0].status == TapStatus.COMPLETED, runs
    print("  (N2) abandoned zero-leg batch revived + completed by a re-run OK")


# --- N4: batch manifest marks not-yet-resolved legs absent; resume fills them ------
def test_n4_batch_manifest_marks_absent_then_filled_on_resume():
    """N4 (MEDIUM): a batch with a still-PAUSED (or enrich-errored) leg must record that
    locale as MARKED-ABSENT in by_locale, not freeze the map to the resolved locales (round
    1's write-once + skip-empty froze partials). Resuming the batch to completion fills every
    locale (F2: the action-level manifest is (re-)assembled when the action reaches terminal
    -- the terminal leg / the resume that completes the batch)."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_multiloc_doc(s, proj, ["Open Settings now."], ["es-ES", "fr-FR"])
        wf = _mk_pause_workflow(s)  # HUMAN_REVIEW -> every leg pauses
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf_id, doc_id=doc_id, locales=["es-ES", "fr-FR"], synchronous=True
        )
        batch_id = batch.id
    with Session(engine) as s:
        a = s.exec(select(TapAction).where(TapAction.batch_id == batch_id)).all()[0]
        aid = a.id
        by = a.manifest["by_locale"]
        assert set(by.keys()) == {"es-ES", "fr-FR"}, (
            "both locales recorded, not frozen",
            by.keys(),
        )
        assert by["es-ES"].get("absent") and by["fr-FR"].get("absent"), (
            "a paused leg is MARKED absent, not omitted",
            by,
        )
        run_ids = {
            r.target_locale: r.workflow_run_id
            for r in s.exec(select(TapRun).where(TapRun.action_id == aid)).all()
        }
    # Resume BOTH legs to completion (individual resumes, OUTSIDE the batch dispatch); the
    # resume that makes the action terminal (re-)assembles the manifest with every locale.
    for loc in ("es-ES", "fr-FR"):
        with Session(engine) as s:
            orchestrator.resume_workflow(s, run_ids[loc], loc, "keep")
    with Session(engine) as s:
        a = s.get(TapAction, aid)
        assert a.status == TapStatus.COMPLETED, a.status
        by = a.manifest["by_locale"]
        for loc in ("es-ES", "fr-FR"):
            assert not by[loc].get("absent") and by[loc].get("tm") is not None, (
                f"resume-to-completion fills the {loc} manifest (N4)",
                by[loc],
            )
    print("  (N4) batch manifest marks paused legs absent; completion fills every locale OK")


# --- N5: the call-file roll-up parses the file ONCE, grouped by run ----------------
def test_n5_call_rows_grouped_single_pass():
    """N5 (MEDIUM): rollup must not re-parse the whole per-action file per run (O(N*file)
    under the D1 grain). _call_rows_by_run groups all runs from ONE pass."""
    _reset()
    aid = tap_envelope.new_id()
    r1, r2 = tap_envelope.new_id(), tap_envelope.new_id()
    for rid, n in ((r1, 2), (r2, 3)):
        for i in range(n):
            prompt_tap.capture(
                prompt_tap.build_record(
                    provider="mock",
                    provider_kind="mock",
                    model="m",
                    task="t",
                    system="s",
                    prompt="p%d" % i,  # noqa: UP031
                    max_tokens=8,
                    payload={"text": "x"},
                    outcome="ok",
                    response_text="y",
                    cost=0.0,
                    tap_meta={"action_id": aid, "run_id": rid},
                )
            )
    grouped = tap_enrich._call_rows_by_run(aid)
    assert len(grouped[r1]) == 2 and len(grouped[r2]) == 3, {k: len(v) for k, v in grouped.items()}
    assert [x["seq"] for x in grouped[r1]] == [0, 1], grouped[r1]
    print("  (N5) _call_rows_by_run groups all runs in a single file pass OK")


# --- N7: the REAL resume seam flips a paused leg + its action back to running ------
def test_n7_resume_flips_paused_run_to_running():
    """N7 (finding): round 1 CLAIMED resume flipped a paused leg to running but nothing did
    (begin_workflow_envelope's is_resume branch returned the pair untouched); the test
    hand-set the status. Drive the REAL envelope resume seam and assert it flips the leg AND
    re-derives the action to RUNNING."""
    _reset()
    with Session(engine) as s:
        proj = _mk_project(s, legacy_term=("Settings", "Ajustes"))
        doc = _mk_doc(s, proj, ["Open Settings now."])
        wf = _mk_pause_workflow(s)
        wf_id, doc_id = wf.id, doc.id
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream(s, wf_id, doc_id, "es-ES"))
    a, r = _one_action_run()
    assert r.status == TapStatus.PAUSED and a.status == TapStatus.PAUSED, (r.status, a.status)
    wf_run_id = r.workflow_run_id
    # The exact seam orchestrator uses on resume (is_resume=True).
    aid, rid = tap_envelope.begin_workflow_envelope(
        workflow_run_id=wf_run_id,
        is_resume=True,
        document_id=doc_id,
        target_locale="es-ES",
        batch_id=None,
    )
    assert (aid, rid) == (a.id, r.id), ((aid, rid), (a.id, r.id))
    with Session(engine) as s:
        r2 = s.get(TapRun, r.id)
        a2 = s.get(TapAction, a.id)
        assert r2.status == TapStatus.RUNNING, (
            "resume flips the paused leg to running (N7)",
            r2.status,
        )
        assert a2.status == TapStatus.RUNNING, ("the action follows the resumed leg", a2.status)
    print("  (N7) resume flips the paused leg + action back to RUNNING (real seam) OK")


# --- F1: revive preserves finished_at (a historical fact) + pins the revive wiring (F6) --
def test_f1_revive_preserves_finished_at():
    """F1/F6: _revive_action_if_terminal must re-open a terminal/abandoned action to RUNNING
    WITHOUT clearing finished_at (destroying a historical fact); the re-completion re-anchors
    it. Idempotent no-op on a non-terminal action. Direct unit test pins the revive mechanism
    independently of the sticky-guard removal (so neither can be silently restored)."""
    _reset()
    from datetime import datetime as _dt

    fin = _dt(2026, 8, 1, 12, 0, 0)
    with Session(engine) as s:
        from app.models import TapActionKind

        comp = TapAction(
            id=tap_envelope.new_id(),
            kind=TapActionKind.WORKFLOW,
            status=TapStatus.COMPLETED,
            finished_at=fin,
        )
        aband = TapAction(
            id=tap_envelope.new_id(),
            kind=TapActionKind.WORKFLOW,
            status=TapStatus.ABANDONED,
            finished_at=fin,
        )
        run = TapAction(
            id=tap_envelope.new_id(),
            kind=TapActionKind.WORKFLOW,
            status=TapStatus.RUNNING,
            finished_at=None,
        )
        for x in (comp, aband, run):
            s.add(x)
        s.commit()
        cid, abid, rid = comp.id, aband.id, run.id
    tap_envelope._revive_action_if_terminal(cid)
    tap_envelope._revive_action_if_terminal(abid)
    tap_envelope._revive_action_if_terminal(rid)
    with Session(engine) as s:
        c, ab, r = s.get(TapAction, cid), s.get(TapAction, abid), s.get(TapAction, rid)
        assert c.status == TapStatus.RUNNING and c.finished_at == fin, (
            "completed -> running, finished_at PRESERVED (F1)",
            c.status,
            c.finished_at,
        )
        assert ab.status == TapStatus.RUNNING and ab.finished_at == fin, (
            "abandoned -> running, finished_at preserved",
            ab.status,
            ab.finished_at,
        )
        assert r.status == TapStatus.RUNNING and r.finished_at is None, (
            "a non-terminal action is a no-op",
            r.status,
            r.finished_at,
        )
    print("  (F1) revive re-opens terminal/abandoned; preserves finished_at; no-op running OK")


# --- F2: totals roll-up (the file parse) happens ONLY when the action is terminal --------
def test_f2_totals_rollup_only_when_terminal():
    """F2: rollup_action_totals (the whole-file parse) must run ONLY when finalize derives a
    TERMINAL status -- NOT on every leg finalize (round 2 re-parsed the file per leg, O(N)
    passes on the hot path). Counted via a monkeypatch on the single-pass reader."""
    _reset()
    from app.models import TapActionKind

    with Session(engine) as s:
        a = TapAction(
            id=tap_envelope.new_id(), kind=TapActionKind.WORKFLOW, status=TapStatus.RUNNING
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
        r1 = TapRun(
            id=tap_envelope.new_id(),
            action_id=aid,
            target_locale="es-ES",
            status=TapStatus.COMPLETED,
        )
        r2 = TapRun(
            id=tap_envelope.new_id(), action_id=aid, target_locale="fr-FR", status=TapStatus.RUNNING
        )  # one leg still in flight -> action running
        s.add(r1)
        s.add(r2)
        s.commit()
        r2id = r2.id
    passes = {"n": 0}
    orig = tap_enrich._call_rows_by_run
    tap_enrich._call_rows_by_run = lambda aid_: (
        passes.__setitem__("n", passes["n"] + 1) or orig(aid_)
    )
    try:
        # Non-terminal (one leg running): NO file parse.
        tap_envelope.finalize_action_if_all_runs_done(aid)
        assert passes["n"] == 0, (
            "no file parse while the action is non-terminal (F2)",
            passes["n"],
        )
        # Flip the last leg terminal -> exactly ONE file parse for the roll-up.
        with Session(engine) as s:
            r2 = s.get(TapRun, r2id)
            r2.status = TapStatus.COMPLETED
            s.add(r2)
            s.commit()
        tap_envelope.finalize_action_if_all_runs_done(aid)
        assert passes["n"] == 1, ("exactly one file parse at terminal, not per-leg", passes["n"])
    finally:
        tap_enrich._call_rows_by_run = orig
    with Session(engine) as s:
        a = s.get(TapAction, aid)
        assert a.status == TapStatus.COMPLETED and a.totals is not None, (a.status, a.totals)
    print("  (F2) totals file-parse runs only at terminal (not per leg finalize) OK")


# --- F5: an all-None-identity caller cannot REVIVE a foreign terminal action ------------
def test_f5_all_none_identity_no_revive():
    """F5: find_batch_action skips every None predicate, so an all-None-identity caller
    matches a batch action regardless of tenant. Attaching a leg must NOT revive that foreign
    terminal action (the revive is gated on the leg carrying >=1 identity predicate)."""
    _reset()
    ident = dict(project_id=100, document_id=200, org_id=1)  # noqa: C408
    aid = tap_envelope.begin_batch_action(batch_id=707, requested_locales=["es-ES"], **ident)
    assert aid
    # Drive it terminal.
    _, r = tap_envelope.begin_workflow_envelope(
        workflow_run_id=6001, is_resume=False, batch_id=707, target_locale="es-ES", **ident
    )
    tap_envelope.finalize_pair(aid, r, TapStatus.COMPLETED)
    with Session(engine) as s:
        assert s.get(TapAction, aid).status == TapStatus.COMPLETED
    # An all-None-identity leg attaches (find_batch_action matches by batch_id alone) -> it
    # must NOT revive the foreign action.
    tap_envelope.begin_workflow_envelope(
        workflow_run_id=6002, is_resume=False, batch_id=707, target_locale="de-DE"
    )
    with Session(engine) as s:
        assert s.get(TapAction, aid).status == TapStatus.COMPLETED, (
            "an all-None caller must not revive a foreign terminal action (F5)",
            s.get(TapAction, aid).status,
        )
    print("  (F5) all-None-identity caller does not revive a foreign terminal action OK")


def main():
    init_db()
    test_batch_one_action_n_runs()
    test_single_locale_run_is_one_plus_one()
    test_batch_resume_does_not_fork_action()
    test_b1_cross_tenant_batch_action_no_hijack()
    test_h1_totals_from_callfile_not_stale_sibling_detail()
    test_m1_zero_leg_batch_action_abandoned()
    test_m2_paused_leg_surfaces_paused_action()
    test_m4_models_doc_matches_shipped_grain()
    test_m5_per_locale_manifest_records_every_locale()
    test_m6_enrich_replay_is_bounded()
    test_n1_rerun_rerolls_action_totals()
    test_n2_abandoned_zero_leg_revived_by_rerun()
    test_n4_batch_manifest_marks_absent_then_filled_on_resume()
    test_n5_call_rows_grouped_single_pass()
    test_n7_resume_flips_paused_run_to_running()
    test_f1_revive_preserves_finished_at()
    test_f2_totals_rollup_only_when_terminal()
    test_f5_all_none_identity_no_revive()
    test_engine_profile_call_stamps_resolved_model()
    test_manifest_populated_and_hashed()
    test_rollup_honesty_totals_agree_with_children()
    test_adherence_honored_and_not_checked()
    test_adherence_violated_via_reenrich()
    test_adherence_unit_three_states()
    test_direct_mt_enrichment()
    test_dangling_asset_read_and_reenrich_safe()
    test_observer_purity_enrich_failure()
    test_non_terminal_leg_not_enriched()
    print("TAP ENRICHMENT OK")


if __name__ == "__main__":
    main()
