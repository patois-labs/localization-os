"""HUMAN-WORK PROTECTION at the sole MT write seam (data-integrity fix).

A machine pass (MT / import / vendor return / TM apply) must NEVER silently overwrite a
segment a HUMAN has APPROVED or EDITED. Before this fix a prior guard protected only the
byte-exact-TM-match subset inside the tm_match relock branch; the broad case -- an
approved/edited segment with no TM hard-lock and no byte-exact TM hit -- was overwritten
by any later MT pass and its status reset to `mt`.

The fix has two layers, both exercised here:
  * Layer 1 -- `quality.commit_mt_output` refuses when `status in (APPROVED, EDITED)`,
    leaving the row byte-untouched and returning `CommitOutcome.status_protected` (a
    DISTINCT marker, not overloaded onto lock_refused). NOT gated by TM_LOCKS_ENABLED or
    REFUSAL_GUARD_ENABLED (core, default-on). NEEDS_REVIEW is NOT protected.
  * Layer 2 -- the segmented orchestrator excludes approved/edited segments from every
    provider dispatch (working[] preserved, write-back skipped) and surfaces the count in
    the run summary; the FULL_TEXT write-back handles the refusal gracefully.

Callers audited: orchestrator segmented + FULL_TEXT, vendor return intake, key import.

HERMETIC: throwaway LOC_TMS_DATA_DIR; MockProvider (keyless) echoes "[locale] source";
ASCII-only. Final line: "STATUS PROTECTION OK".
"""

import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_statusprot_")

from sqlalchemy import text as sa_text
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    ProjectType,
    Segment,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    content,
    keys,
    orchestrator,
    quality,
    tm,
    vendor_return,
)

init_db()


def _mk_project(name, src="en-US", customer_id=None):
    with Session(engine) as s:
        p = Project(name=name, source_locale=src, default_provider="mock", customer_id=customer_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(pid, locale, segs, mode=TransMode.SEGMENTED):
    """segs = list of (source, target, status)."""
    with Session(engine) as s:
        kw = {}
        if mode == TransMode.FULL_TEXT:
            kw = {
                "fmt": "docx",
                "skeleton": {"units": [{"text": segs[0][0]}], "seg_locale": "en-US"},
            }
        doc = Document(project_id=pid, filename="d.txt", mode=mode, status="new", **kw)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, (src, tgt, st) in enumerate(segs):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=locale,
                    order_index=i,
                    source_text=src,
                    target_text=tgt,
                    status=st,
                )
            )
        s.add(TargetLocale(project_id=pid, locale=locale))
        s.commit()
        return doc.id


def _mk_mt_workflow():
    with Session(engine) as s:
        wf = Workflow(name="MTWF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(workflow_id=wf.id, order_index=0, name="MT", step_type=StepType.LLM_AGENT)
        )
        s.commit()
        return wf.id


def _segs(s, doc_id):
    return sorted(
        s.exec(select(Segment).where(Segment.document_id == doc_id)).all(),
        key=lambda x: x.order_index,
    )


def _full_row(seg):
    """The FULL persisted row a refusal must leave byte-untouched (F11): not just target +
    status, but qa_flags, mt_confidence, tm_match_pct, and the lock columns -- so a refusal
    that quietly re-flagged / re-scored / unlocked the row would be caught."""
    return (
        seg.target_text,
        seg.status,
        tuple(sorted(str(f) for f in (seg.qa_flags or []))),
        seg.mt_confidence,
        seg.tm_match_pct,
        getattr(seg, "lock_band", None),
        getattr(seg, "lock_hard", None),
        str(seg.meta or {}),
    )


# --- (1) an APPROVED segment survives a full workflow MT run untouched -------------
def test_approved_segment_preserved():
    pid = _mk_project("Approved1")
    doc_id = _mk_doc(
        pid,
        "es-ES",
        [
            ("Save changes.", "GUARDADO HUMANO", SegStatus.APPROVED),
            ("Fresh line.", "", SegStatus.NEW),
        ],
    )
    wf_id = _mk_mt_workflow()
    with Session(engine) as s:
        approved0, _ = _segs(s, doc_id)
        _before = _full_row(approved0)
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    # the run reports the protection, never silent
    assert summary.get("lqa_safety", {}).get("status_protected") == 1, summary
    with Session(engine) as s:
        approved, fresh = _segs(s, doc_id)
        # BYTE-IDENTICAL: the WHOLE row (target, status, qa_flags, mt_confidence, tm_match_pct,
        # lock columns, meta) is unchanged (the mock echo would be "[es-ES] ...")
        assert _full_row(approved) == _before, (_full_row(approved), _before)
        # the NEW sibling WAS translated -> proves the protection is not over-broad
        assert "Fresh line." in fresh.target_text and fresh.status == SegStatus.MT, fresh
    print("  (1) APPROVED segment preserved byte-identical (full row); NEW sibling translated: OK")


# --- (2) an EDITED segment survives the same run untouched -------------------------
def test_edited_segment_preserved():
    pid = _mk_project("Edited2")
    doc_id = _mk_doc(pid, "es-ES", [("Delete all.", "EDITADO HUMANO", SegStatus.EDITED)])
    wf_id = _mk_mt_workflow()
    with Session(engine) as s:
        (edited0,) = _segs(s, doc_id)
        _before = _full_row(edited0)
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    assert summary.get("lqa_safety", {}).get("status_protected") == 1, summary
    with Session(engine) as s:
        (edited,) = _segs(s, doc_id)
        assert _full_row(edited) == _before, (_full_row(edited), _before)
    print("  (2) EDITED segment preserved byte-identical (full row): OK")


# --- (3) a NEEDS_REVIEW (non-byte-exact) segment is STILL retranslated -------------
def test_needs_review_not_protected():
    """Scope boundary: NEEDS_REVIEW is machine routing, not human work -- a re-run is
    SUPPOSED to be able to re-translate it. Only the byte-exact-TM probe-5 protection of a
    NEEDS_REVIEW sentinel (in the orchestrator, not the seam) survives, and there is no TM
    entry here so that path does not apply."""
    pid = _mk_project("Review3")
    doc_id = _mk_doc(
        pid, "es-ES", [("Sign in now.", "texto viejo en revision", SegStatus.NEEDS_REVIEW)]
    )
    wf_id = _mk_mt_workflow()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    # NEEDS_REVIEW must NOT be counted as protected
    assert summary.get("lqa_safety", {}).get("status_protected") is None, summary
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        # retranslated by the mock (echo carries source + locale), status -> MT
        assert "Sign in now." in seg.target_text and "[es-ES]" in seg.target_text, seg
        assert seg.status == SegStatus.MT, seg.status
    print("  (3) NEEDS_REVIEW (non-byte-exact) is re-translated -- scope holds: OK")


# --- (4) a vendor return targeting an APPROVED segment is refused + reconciled -----
def test_vendor_return_refused_on_approved():
    """The latest human decision outranks a vendor return exactly as a hard lock does: the
    seam refuses, the row is byte-untouched, and reconciliation counts it in the `locked`
    bucket (not flagged/missing/failed)."""
    with Session(engine) as s:
        cust = Customer(name="VC")
        s.add(cust)
        s.commit()
        s.refresh(cust)
    pid = _mk_project("Vendor4", customer_id=cust.id)
    doc_id = _mk_doc(pid, "es-ES", [("Hello vendor.", "APROBADO VENDOR", SegStatus.APPROVED)])
    with Session(engine) as s:
        project = s.get(Project, pid)
        (seg,) = _segs(s, doc_id)
        # direct seam proof: the ONE sanctioned writer refuses on an approved segment
        outcome = quality.commit_mt_output(s, seg, "texto del vendor", source="vendor")
        assert outcome.status_protected and not outcome.committed, outcome
        assert not outcome.lock_refused, "must be a DISTINCT marker, not lock_refused"
        assert seg.target_text == "APROBADO VENDOR" and seg.status == SegStatus.APPROVED
        # and the vendor-intake path buckets it in its OWN status_protected bucket (F10),
        # NOT collapsed into locked/flagged/no-op/admitted.
        admitted, flagged, no_op, locked, status_protected = [], [], [], [], []
        leg = SimpleNamespace(target_locale="es-ES")
        vendor_return._admit_units(
            s,
            [{"id": seg.id, "target": "Traduccion nueva del vendor"}],
            {seg.id: (0, seg)},
            ["placeholder"],
            leg,
            project,
            "en-US",
            "vendor",
            admitted,
            flagged,
            no_op,
            locked,
            status_protected=status_protected,
        )
        assert status_protected == [seg.id], (
            "approved -> its OWN status_protected bucket",
            status_protected,
            locked,
            flagged,
            no_op,
        )
        assert not admitted and not flagged and not no_op and not locked, (
            admitted,
            flagged,
            no_op,
            locked,
        )
        s.refresh(seg)
        assert seg.target_text == "APROBADO VENDOR" and seg.status == SegStatus.APPROVED
    print("  (4) vendor return over an APPROVED segment refused, own status_protected bucket: OK")


# --- (5) a machine key import over an APPROVED key value is refused; human lands ----
def test_key_import_refused_on_approved():
    with Session(engine) as s:
        p = content.project_create(
            s,
            "KeyProj5",
            source_locale="en-US",
            target_locales="es-ES",
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        k = keys.create_key(s, p, "cta.save", base_value="Save")
        # a human APPROVES a value
        keys.set_value(
            s, k, "es-ES", "Guardar", origin=keys.ORIGIN_HUMAN, status=SegStatus.APPROVED
        )
        # a machine IMPORT of DIFFERENT text must be refused (row untouched)
        keys.set_value(s, k, "es-ES", "Salvar (maquina)", origin=keys.ORIGIN_IMPORT)
        seg = next(v for v in keys.values_for(s, k, "es-ES") if (v.plural_category or "") == "")
        assert seg.target_text == "Guardar", ("machine import must not overwrite", seg.target_text)
        assert seg.status == SegStatus.APPROVED, seg.status
        # the HUMAN escape hatch still lands (human writes are never gated)
        keys.set_value(s, k, "es-ES", "Guardar cambios", origin=keys.ORIGIN_HUMAN)
        seg2 = next(v for v in keys.values_for(s, k, "es-ES") if (v.plural_category or "") == "")
        assert seg2.target_text == "Guardar cambios", seg2.target_text
    print("  (5) machine key import refused over APPROVED; human write lands: OK")


# --- (6) mid-run approval race: the seam re-reads status (freshness) ---------------
def test_seam_freshness_reads_db_status():
    """The seam receives a possibly STALE segment loaded at run start. A human approval that
    lands mid-run must be honored. Staged deterministically: raw-UPDATE the DB row to APPROVED
    via the session connection (bypassing the identity map) so the in-memory object still reads
    its OLD status, then call the seam -- only the freshness DB re-read (a targeted scalar
    SELECT of the status column, F2) can catch it. This exercises exactly the seam-level read
    the cross-session race depends on; the genuine cross-session behaviour (another session's
    committed approval) is the same read against a committed row, and is correct by the same
    code path."""
    pid = _mk_project("Freshness6")
    doc_id = _mk_doc(pid, "es-ES", [("Publish now.", "", SegStatus.NEW)])
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        # flip the DB row to APPROVED WITHOUT touching the in-memory object's attribute
        s.connection().execute(
            sa_text("UPDATE segment SET status = 'approved' WHERE id = :i"), {"i": seg.id}
        )
        assert seg.status == SegStatus.NEW, (
            "in-memory status must be STALE for the test to prove the refresh",
            seg.status,
        )
        outcome = quality.commit_mt_output(s, seg, "[es-ES] Publish now.", source="mt")
        assert outcome.status_protected and not outcome.committed, outcome
        assert seg.target_text == "", (
            "the stale-object write must be refused via the DB re-read",
            seg.target_text,
        )
    print("  (6) seam re-reads status from the DB (mid-run approval honored): OK")


# --- (7) FULL_TEXT: an approved body survives a workflow run -----------------------
def test_fulltext_approved_body_preserved():
    pid = _mk_project("FullText7")
    doc_id = _mk_doc(
        pid,
        "es-ES",
        [("Hello world.", "MUNDO HUMANO APROBADO", SegStatus.APPROVED)],
        mode=TransMode.FULL_TEXT,
    )
    wf_id = _mk_mt_workflow()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    assert summary.get("lqa_safety", {}).get("status_protected") == 1, summary
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == "MUNDO HUMANO APROBADO", seg.target_text
        assert seg.status == SegStatus.APPROVED, seg.status
    print("  (7) FULL_TEXT approved body preserved: OK")


# --- (8) the guard is NOT gated by the refusal / TM kill switches ------------------
def test_protection_ungated_by_kill_switches():
    """Layer 1 is core, default-on: flipping REFUSAL_GUARD_ENABLED or TM_LOCKS_ENABLED off
    must not disable human-work protection."""
    from app import config

    pid = _mk_project("Ungated8")
    doc_id = _mk_doc(pid, "es-ES", [("Keep me.", "INTACTO HUMANO", SegStatus.APPROVED)])
    _rg, _tl = config.REFUSAL_GUARD_ENABLED, config.TM_LOCKS_ENABLED
    try:
        config.REFUSAL_GUARD_ENABLED = False
        config.TM_LOCKS_ENABLED = False
        with Session(engine) as s:
            (seg,) = _segs(s, doc_id)
            outcome = quality.commit_mt_output(s, seg, "[es-ES] Keep me.", source="mt")
            assert outcome.status_protected and not outcome.committed, outcome
            assert seg.target_text == "INTACTO HUMANO", seg.target_text
    finally:
        config.REFUSAL_GUARD_ENABLED, config.TM_LOCKS_ENABLED = _rg, _tl
    print("  (8) protection holds with both kill switches OFF: OK")


# --- (9) the human paragraph editor is NOT blocked by status protection -------------
def test_human_para_edit_bypasses_status_protection():
    """SCOPE BOUNDARY (the other direction): the status guard stops MACHINE overwrites of
    human work -- it must NEVER block a PERSON from editing their own content. The full-text
    paragraph editor (fulltext_paragraphs) is the one human writer that routes THROUGH the
    seam (for its tag-integrity gate), passing human_edit=True. Without the bypass, editing
    one paragraph (which sets the body EDITED) would refuse the NEXT edit, and an APPROVED
    full-text body could never be post-edited -- a regression the seam guard introduced if
    applied indiscriminately. Machine MT through the seam is still refused (proved inline)."""
    from app.models import Document, TransMode
    from app.services import fulltext_paragraphs as fp

    pid = _mk_project("HumanPara9")
    doc_id = _mk_doc(
        pid,
        "es-ES",
        [("Alpha.\n\nBeta.", "AlphaES.\n\nBetaES.", SegStatus.MT)],
        mode=TransMode.FULL_TEXT,
    )
    # the _mk_doc FULL_TEXT skeleton carries only one unit; give it two paragraphs so the
    # positional map has index 0 and index 1.
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        doc.skeleton = {"units": [{"text": "Alpha."}, {"text": "Beta."}], "seg_locale": "en-US"}
        s.add(doc)
        s.commit()
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        r1 = fp.save_paragraph(s, doc, "es-ES", 0, "Alpha EDIT1.", actor="t", ip="127.0.0.1")
        assert r1.get("ok") is True, ("first para edit must land", r1)
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.status == SegStatus.EDITED, seg.status  # body is now human-EDITED
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        # the SECOND edit, on a now-EDITED body, must ALSO land (the pre-fix regression
        # refused it as `write_refused`).
        r2 = fp.save_paragraph(s, doc, "es-ES", 1, "Beta EDIT2.", actor="t", ip="127.0.0.1")
        assert r2.get("ok") is True, ("second para edit on an EDITED body must land", r2)
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == "Alpha EDIT1.\n\nBeta EDIT2.", seg.target_text
        # a MACHINE write to the same EDITED body is STILL refused (the guard holds for
        # machine paths; only human_edit bypasses it).
        outcome = quality.commit_mt_output(s, seg, "[es-ES] machine.", source="mt")
        assert outcome.status_protected and not outcome.committed, outcome
        assert seg.target_text == "Alpha EDIT1.\n\nBeta EDIT2.", seg.target_text
    print("  (9) human paragraph editor bypasses protection; machine MT still refused: OK")


# --- (10) F1: an operator edit during a human_review PAUSE on a protected slot LANDS -------
def _pause_edit_lands(status, name):
    """F1 (adversarial review regression) + PART 1 (pause-edit protection, 2026-08-13): an
    operator edit made during a human_review pause on an APPROVED/EDITED segment must LAND, and
    now lands as SegStatus.EDITED -- human work, protected from every LATER run's machine passes
    by the status guard (it previously landed `mt`, UNPROTECTED, and the next run destroyed it:
    the flood's silent-loss strand). Pre-F1 the Layer-2 seed clobbered the operator's working
    text with the stored target on resume and the write-back skipped it (the edit was lost)."""
    pid = _mk_project(name)
    doc_id = _mk_doc(pid, "es-ES", [("Edit me during the pause.", "TARGET ORIGINAL", status)])
    with Session(engine) as s:
        wf = Workflow(name="RWF-" + name)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="Review", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        wf_id = wf.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    # the paused blob is the seeded stored target (the protection seeded working from it)
    assert summ.get("paused_text") == "TARGET ORIGINAL", summ.get("paused_text")
    edited = "TEXTO EDITADO POR EL OPERADOR"
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", edited)
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == edited, (
            "HUMAN EDIT WINS -- the pause edit must land, not be discarded for the stored target",
            seg.target_text,
        )
        # PART 1: lands as EDITED (protected), NOT mt -- the human's work survives the next run.
        assert seg.status == SegStatus.EDITED, seg.status


def test_pause_edit_lands_on_approved():
    _pause_edit_lands(SegStatus.APPROVED, "PauseApproved10")
    print("  (10) operator edit during a pause LANDS EDITED (protected) on an APPROVED slot: OK")


def test_pause_edit_lands_on_edited():
    _pause_edit_lands(SegStatus.EDITED, "PauseEdited10")
    print("  (10) operator edit during a pause LANDS EDITED (protected) on an EDITED slot: OK")


# --- (11) F3: a BLANK machine write must not DESTROY an approved value --------------
def test_blank_machine_write_refused_on_approved():
    """F3 (adversarial review): a blank (empty) machine write -- ORIGIN_API/ORIGIN_IMPORT --
    over an unlocked APPROVED value used to take the direct-clear branch and reset it to
    target='' status=new, DESTROYING the sign-off. It must now route through the seam and be
    refused, leaving the row byte-untouched. A blank write over a NON-final (mt) value still
    clears it (unchanged)."""
    with Session(engine) as s:
        p = content.project_create(
            s,
            "BlankF3",
            source_locale="en-US",
            target_locales="es-ES",
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        k = keys.create_key(s, p, "cta.ok", base_value="OK")
        keys.set_value(
            s, k, "es-ES", "Aceptar", origin=keys.ORIGIN_HUMAN, status=SegStatus.APPROVED
        )
        keys.set_value(s, k, "es-ES", "", origin=keys.ORIGIN_API)  # blank machine write
        seg = next(v for v in keys.values_for(s, k, "es-ES") if (v.plural_category or "") == "")
        assert seg.target_text == "Aceptar", (
            "a blank machine write must NOT clear an approved value",
            seg.target_text,
        )
        assert seg.status == SegStatus.APPROVED, seg.status
        # a blank machine write over a NON-final value still clears it (behavior preserved)
        k2 = keys.create_key(s, p, "cta.mt", base_value="Go")
        keys.set_value(s, k2, "es-ES", "Vamos", origin=keys.ORIGIN_HUMAN, status=SegStatus.MT)
        keys.set_value(s, k2, "es-ES", "", origin=keys.ORIGIN_API)
        seg2 = next(v for v in keys.values_for(s, k2, "es-ES") if (v.plural_category or "") == "")
        assert seg2.target_text == "" and seg2.status == SegStatus.NEW, (
            seg2.target_text,
            seg2.status,
        )
    print("  (11) blank machine write refused over APPROVED; still clears a non-final value: OK")


# --- (12) F4: a refused /api-style write is SURFACED, not silent ok:true ------------
def test_refusal_sink_surfaces_machine_refusal():
    """F4 (adversarial review): set_value used to return a segment and SWALLOW the seam's
    refusal, so PUT /api/keys returned ok:true for a write that never landed. The optional
    refusal_sink now collects the reason so the route can answer 409. A human-origin write
    still lands and appends nothing."""
    with Session(engine) as s:
        p = content.project_create(
            s,
            "SinkF4",
            source_locale="en-US",
            target_locales="es-ES",
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        k = keys.create_key(s, p, "cta.save", base_value="Save")
        keys.set_value(
            s, k, "es-ES", "Guardar", origin=keys.ORIGIN_HUMAN, status=SegStatus.APPROVED
        )
        sink: list = []
        keys.set_value(s, k, "es-ES", "Salvar (maquina)", origin=keys.ORIGIN_API, refusal_sink=sink)
        assert sink == ["status_protected"], ("a refused machine write must be surfaced", sink)
        seg = next(v for v in keys.values_for(s, k, "es-ES") if (v.plural_category or "") == "")
        assert seg.target_text == "Guardar" and seg.status == SegStatus.APPROVED, seg.target_text
        # a human-origin write lands and surfaces NO refusal
        sink2: list = []
        keys.set_value(
            s, k, "es-ES", "Guardar cambios", origin=keys.ORIGIN_HUMAN, refusal_sink=sink2
        )
        assert sink2 == [], ("a human write is never refused", sink2)
        seg2 = next(v for v in keys.values_for(s, k, "es-ES") if (v.plural_category or "") == "")
        assert seg2.target_text == "Guardar cambios", seg2.target_text
    print("  (12) a refused machine write is surfaced via refusal_sink; human write lands: OK")


# --- (13) F2: the freshness read honors in-memory status + runs under no_autoflush ---
def test_freshness_in_memory_and_no_autoflush():
    """F2 (adversarial review): the freshness check takes the MAX-PROTECTION of the in-memory
    status and the DB status (an unflushed staged approval is honored even before it hits the
    DB), and it reads the status under `no_autoflush` -- a SELECT of one column, never a
    session.refresh() (which would expire the row and AUTOFLUSH a sibling's unwritten change)."""
    # (a) in-memory APPROVED with NO persisted row: max-protection honors the staged status
    stub = Segment(
        document_id=0,
        target_locale="es-ES",
        order_index=0,
        source_text="y",
        target_text="HUM",
        status=SegStatus.APPROVED,
    )
    with Session(engine) as s:
        outcome = quality.commit_mt_output(s, stub, "[es-ES] y", source="mt")
        assert outcome.status_protected and not outcome.committed, outcome
        assert stub.target_text == "HUM", stub.target_text
    # (b) the freshness read runs under no_autoflush: a sibling's UNFLUSHED change is not
    # written as a side effect of a seam call on a DIFFERENT (approved) segment.
    pid = _mk_project("NoFlushF2")
    doc_id = _mk_doc(
        pid, "es-ES", [("Alpha.", "APROBADO", SegStatus.APPROVED), ("Beta.", "", SegStatus.NEW)]
    )
    with Session(engine) as s:
        a, b = _segs(s, doc_id)
        b.target_text = "UNFLUSHED SIBLING"
        s.add(b)
        assert b in s.dirty, "precondition: the sibling is staged but unflushed"
        outcome = quality.commit_mt_output(s, a, "[es-ES] Alpha.", source="mt")
        assert outcome.status_protected, (
            "a is approved -> the freshness read ran then refused",
            outcome,
        )
        assert b in s.dirty, (
            "the freshness read must run under no_autoflush -- the sibling "
            "must NOT have been flushed by the seam call"
        )
    print("  (13) freshness honors in-memory status + reads under no_autoflush: OK")


def _two_step_wf(name, first_type):
    """A workflow [<first_type>, human_review] (first_type is LLM_AGENT for a translate step)."""
    with Session(engine) as s:
        wf = Workflow(name=name)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, order_index=0, name="Step0", step_type=first_type))
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=1, name="Review", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        return wf.id


def _mk_review_only_workflow(name):
    """A single-step [human_review] workflow (review last -> a pause-edit is the final content)."""
    with Session(engine) as s:
        wf = Workflow(name=name)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="Review", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        return wf.id


# --- (14) NEW-ISSUE-1: a concurrent mid-pause approval survives an UNTOUCHED resume ----------
def test_concurrent_approval_survives_untouched_resume_segmented():
    """NEW-ISSUE-1 (adversarial re-review, HIGH): a segment NOT approved at run start gets MT'd
    (step 0) and the run pauses (human_review, step 1). DURING the pause a reviewer approves
    their OWN wording in the CAT editor -- the STORED target changes under the parked run.
    Resuming with the paused blob UNTOUCHED must NOT let the machine output overwrite that fresh
    approval. (A divergence-from-stored heuristic would; the parked-blob comparison does not --
    working == parked, so it reads as 'not edited by the operator' and the approval is protected.)"""  # noqa: E501
    pid = _mk_project("ConcApprSeg14")
    doc_id = _mk_doc(pid, "es-ES", [("Approve me mid pause.", "", SegStatus.NEW)])
    wf_id = _two_step_wf("CAWF-seg14", StepType.LLM_AGENT)
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    paused_blob = summ["paused_text"]  # the machine (MT) output, unedited
    # a reviewer approves their OWN wording mid-pause (a direct CAT-editor write, another session)
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        seg.target_text = "APROBACION DEL REVISOR"
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    # resume with the paused blob UNTOUCHED (no operator edit)
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", paused_blob)
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == "APROBACION DEL REVISOR", (
            "the mid-pause approval must SURVIVE an untouched resume",
            seg.target_text,
        )
        assert seg.status == SegStatus.APPROVED, seg.status
    print("  (14) segmented: a concurrent mid-pause approval survives an untouched resume: OK")


def test_concurrent_approval_survives_untouched_resume_fulltext():
    """NEW-ISSUE-1, FULL_TEXT arm: same shape. The set_status=False FT commit made this WORSE
    pre-fix -- the machine string was left LABELLED approved (a false sign-off that exports as
    reviewed). The parked-blob comparison protects it."""
    pid = _mk_project("ConcApprFt14")
    doc_id = _mk_doc(
        pid, "es-ES", [("Body to approve mid pause.", "", SegStatus.NEW)], mode=TransMode.FULL_TEXT
    )
    wf_id = _two_step_wf("CAWF-ft14", StepType.LLM_AGENT)
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    paused_blob = summ["paused_text"]
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        seg.target_text = "CUERPO APROBADO POR EL REVISOR"
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", paused_blob)
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == "CUERPO APROBADO POR EL REVISOR", seg.target_text
        assert seg.status == SegStatus.APPROVED, (
            "must stay approved, not be relabelled by a set_status=False machine commit",
            seg.status,
        )
    print("  (14) FULL_TEXT: a concurrent mid-pause approval survives an untouched resume: OK")


# --- (15) NEW-ISSUE-1(b): concurrent approval + an operator edit of a DIFFERENT slot ---------
def test_concurrent_approval_and_operator_edit_of_other_slot():
    """Both must be honored: the mid-pause approval of slot 0 SURVIVES, and the operator's edit
    of slot 1 (untouched by any concurrent write) LANDS."""
    pid = _mk_project("MixedSlots15")
    doc_id = _mk_doc(
        pid, "es-ES", [("First line.", "", SegStatus.NEW), ("Second line.", "", SegStatus.NEW)]
    )
    wf_id = _two_step_wf("MIX-seg15", StepType.LLM_AGENT)
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    parts = summ["paused_text"].split(orchestrator.SEG_JOINER)
    assert len(parts) == 2, parts
    # reviewer approves slot 0 mid-pause; operator edits ONLY slot 1 in the blob
    with Session(engine) as s:
        s0, s1 = _segs(s, doc_id)
        s0.target_text = "APROBADO SLOT 0"
        s0.status = SegStatus.APPROVED
        s.add(s0)
        s.commit()
    edited_blob = orchestrator.SEG_JOINER.join([parts[0], "EDITADO SLOT 1"])
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", edited_blob)
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        s0, s1 = _segs(s, doc_id)
        assert (s0.target_text, s0.status) == ("APROBADO SLOT 0", SegStatus.APPROVED), (
            "slot 0's concurrent approval must survive",
            s0.target_text,
            s0.status,
        )
        # PART 1: slot 1 was a MACHINE-status (NEW->mt) slot; the operator's pause-edit of it is
        # human work and lands EDITED (protected) -- the exact flood class (an operator replacing
        # a machine value during review), no longer landing an unprotected `mt`.
        assert s1.target_text == "EDITADO SLOT 1" and s1.status == SegStatus.EDITED, (
            "slot 1's operator edit must land EDITED",
            s1.target_text,
            s1.status,
        )
    print(
        "  (15) concurrent approval (slot 0) survives while an operator edit (slot 1) lands "
        "EDITED: OK"
    )


# --- (16) RESIDUAL-1: an operator typing the SOURCE into a pause slot LANDS as an edit -------
def test_operator_pause_text_to_source_lands():
    """RESIDUAL-1 (adversarial re-review): an operator who deliberately types the SOURCE text
    into a pause slot (a legitimate 'leave this in English' call) must have it LAND as an edit,
    not be silently discarded. Under the parked-blob comparison, source != parked -> operator edit."""  # noqa: E501
    pid = _mk_project("SrcEdit16")
    doc_id = _mk_doc(
        pid, "es-ES", [("Leave me in English.", "TARGET APROBADO", SegStatus.APPROVED)]
    )
    with Session(engine) as s:  # a single human_review step
        wf = Workflow(name="SRC-seg16")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="Review", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        wf_id = wf.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    assert summ["paused_text"] == "TARGET APROBADO", summ["paused_text"]
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", "Leave me in English.")
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == "Leave me in English.", (
            "the operator's source-text edit must LAND, not be discarded",
            seg.target_text,
        )
        # PART 1: a deliberate 'leave this in English' pause-edit is human work -> EDITED (protected).  # noqa: E501
        assert seg.status == SegStatus.EDITED, seg.status
    print("  (16) an operator setting a pause slot to the source lands EDITED (RESIDUAL-1): OK")


# --- (17) NEW-ISSUE-2: resume refuses a non-paused (completed) run ---------------------------
def test_resume_refuses_a_non_paused_run():
    """NEW-ISSUE-2 (adversarial re-review): resume_workflow must REFUSE a run that is not PAUSED,
    so a resume with arbitrary text cannot overwrite an already-finished protected body (the CLI
    sync path had no guard; the async path already did)."""
    pid = _mk_project("ResumeGuard17")
    doc_id = _mk_doc(
        pid, "es-ES", [("Body.", "APROBADO INTACTO", SegStatus.APPROVED)], mode=TransMode.FULL_TEXT
    )
    wf_id = _mk_mt_workflow()
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "completed", summ  # a COMPLETED (not paused) run
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", "ARBITRARY MACHINE TEXT")
    assert rsumm["status"] == "rejected", ("resuming a non-paused run must be refused", rsumm)
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert (seg.target_text, seg.status) == ("APROBADO INTACTO", SegStatus.APPROVED), (
            seg.target_text,
            seg.status,
        )
    print("  (17) resume of a non-paused run is refused; the finished body is untouched: OK")


# --- (18) NEW-ISSUE-3: a protected FULL_TEXT body still PAUSES for review + records steps ----
def test_ft_protected_body_still_pauses_and_records_steps():
    """NEW-ISSUE-3 (adversarial re-review): the FULL_TEXT status protection must skip only
    PROVIDER steps -- a human_review step must still PAUSE (the reviewer gets to look) and every
    step must still record a StepRun. Pre-fix the short-circuit skipped the whole loop, so the
    run completed instantly with zero StepRun rows and no pause."""
    from app.models import StepRun

    pid = _mk_project("FtReview18")
    doc_id = _mk_doc(
        pid, "es-ES", [("Hello.", "MUNDO APROBADO", SegStatus.APPROVED)], mode=TransMode.FULL_TEXT
    )
    wf_id = _two_step_wf("FTRV18", StepType.LLM_AGENT)  # [MT (provider), human_review]
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", ("a protected body must STILL pause for review", summ)
    # the reviewer sees the protected HUMAN target (the skipped MT step did not overwrite it)
    assert summ["paused_text"] == "MUNDO APROBADO", summ["paused_text"]
    with Session(engine) as s:
        rows = s.exec(select(StepRun).where(StepRun.workflow_run_id == summ["run_id"])).all()
        assert rows, "the skipped provider step + the pause must still record StepRun rows"
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", summ["paused_text"])
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert (seg.target_text, seg.status) == ("MUNDO APROBADO", SegStatus.APPROVED), (
            seg.target_text,
            seg.status,
        )
    print("  (18) FULL_TEXT protected body still pauses for review + records StepRuns: OK")


# --- (19) NEW-ISSUE-4: an operator edit is STICKY across a SECOND (untouched) pause -----------
def _sticky_two_pause(mode, name):
    """Edit the protected slot at pause A, then resume UNTOUCHED at pause B. The edit must
    SURVIVE: across parks the edit becomes the newly-parked baseline, so without a persisted +
    restored classification it is re-protected and RESET to the stored target (edit lost)."""
    doc_kwargs = {"mode": mode}
    pid = _mk_project(name)
    doc_id = _mk_doc(
        pid, "es-ES", [("Sticky body.", "TARGET APROBADO", SegStatus.APPROVED)], **doc_kwargs
    )
    with Session(engine) as s:
        wf = Workflow(name="STK-" + name)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="ReviewA", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=1, name="ReviewB", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        wf_id = wf.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ  # paused at review A
    edited = "EDICION DEL OPERADOR"
    with Session(engine) as s:
        r1 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", edited)
    assert r1["status"] == "paused", ("now paused at review B", r1)
    assert r1["paused_text"] == edited, r1["paused_text"]
    # untouched resume at pause B (the parked blob, verbatim -> no operator edit at B)
    with Session(engine) as s:
        r2 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", r1["paused_text"])
    assert r2["status"] == "completed", r2
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == edited, (
            "the pause-A edit must SURVIVE an untouched pause B, not revert to the stored target",
            seg.target_text,
        )
        # PART 1: an operator pause-edit that is still the final content lands EDITED (protected)
        # in BOTH arms. Segmented sets the status directly; FULL_TEXT (set_status=False commit)
        # now sets it explicitly too -- fixing the round-3 LOW-1 where an FT edit was left labelled
        # `approved`. The edit having SURVIVED pause B (asserted above) is the sticky-classification
        # guarantee; landing EDITED is the protection guarantee.
        assert seg.status == SegStatus.EDITED, seg.status


def test_operator_edit_sticky_across_two_pauses_segmented():
    _sticky_two_pause(TransMode.SEGMENTED, "Sticky19Seg")
    print("  (19) segmented: an operator edit survives a second untouched pause (sticky): OK")


def test_operator_edit_sticky_across_two_pauses_fulltext():
    _sticky_two_pause(TransMode.FULL_TEXT, "Sticky19Ft")
    print("  (19) FULL_TEXT: an operator edit survives a second untouched pause (sticky): OK")


# --- (20) NEW-ISSUE-4: [review, MT, review] -- the MT-of-the-edit lands, not the revert -------
def test_sticky_edit_then_mt_step_lands_segmented():
    """[review A, MT, review B]: the operator edits at A, the MT step re-translates that edit,
    and pause B is untouched. The FINAL row must be the MT step's output (derived from the edit),
    NOT the stored approval reverted onto it -- proving the sticky classification carries through
    an intervening provider step and its output is committed."""
    pid = _mk_project("Sticky20Seg")
    doc_id = _mk_doc(pid, "es-ES", [("Translate my edit.", "TARGET APROBADO", SegStatus.APPROVED)])
    with Session(engine) as s:
        wf = Workflow(name="STK-mt20")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="ReviewA", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.add(
            WorkflowStep(workflow_id=wf.id, order_index=1, name="MT", step_type=StepType.LLM_AGENT)
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=2, name="ReviewB", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        wf_id = wf.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    with Session(engine) as s:
        r1 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", "EDICION PARA MT")
    assert r1["status"] == "paused", ("the MT step ran, then paused at review B", r1)
    mt_of_edit = r1["paused_text"]  # the MT step's output over the edit
    assert mt_of_edit != "TARGET APROBADO", ("the MT step must have run over the edit", mt_of_edit)
    with Session(engine) as s:
        r2 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", r1["paused_text"])
    assert r2["status"] == "completed", r2
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == mt_of_edit, (
            "the MT-of-the-edit must LAND, not be reverted to the stored approval",
            seg.target_text,
            mt_of_edit,
        )
        assert seg.target_text != "TARGET APROBADO", seg.target_text
        assert seg.status == SegStatus.MT, seg.status
    print("  (20) [review, MT, review]: the MT-of-the-edit lands across the second pause: OK")


# --- (21) F1: clearing a FULL_TEXT body resets it to NEW -> the machine re-fills it ----------
def test_fulltext_clear_whole_body_hands_back_to_machine():
    """F1 (live prod QA 2026-08-13, AUDIT-humanwork-live-qa): the documented escape hatch
    -- clear the target so the machine re-translates it -- was BROKEN on the full-text
    paragraph surface. `save_paragraph` set EDITED unconditionally on a clean save, so
    clearing a single-paragraph full-text body left it empty + EDITED; the human-work guard
    excludes EDITED from every machine call, so no rerun could ever refill it and the export
    was HELD forever (live repro: doc 118 seg 3152). The fix mirrors the segmented editor:
    a whole-body-empty save resets status to NEW. This is the single-paragraph shape the
    live repro hit -- clearing paragraph 0 empties the ENTIRE body."""
    from app.models import Document
    from app.services import export
    from app.services import fulltext_paragraphs as fp

    pid = _mk_project("ClearHatch21")
    doc_id = _mk_doc(
        pid,
        "es-ES",
        [("Translate this line.", "[es-ES] Translate this line.", SegStatus.MT)],
        mode=TransMode.FULL_TEXT,
    )
    # (1) clear paragraph 0 -> the WHOLE body is empty -> status NEW, target empty.
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        r = fp.save_paragraph(s, doc, "es-ES", 0, "", actor="t", ip="127.0.0.1")
        assert r.get("ok") is True, ("clearing the paragraph must succeed", r)
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.status == SegStatus.NEW, (
            "a cleared full-text body must reset to NEW, not EDITED (which the guard would strand)",
            seg.status,
        )
        assert not (seg.target_text or "").strip(), ("target must be empty", seg.target_text)
        doc = s.get(Document, doc_id)
        # while empty, the locale is legitimately HELD (untranslated) -- the pre-fix trap is
        # that it stayed held after a rerun; the rerun below un-holds it.
        assert export.held_locale_reason(s, doc, "es-ES") is not None, "empty body must read held"
    # (2) a subsequent MT run RE-TRANSLATES it (NEW is not protected -> not refused).
    wf_id = _mk_mt_workflow()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    assert summary.get("lqa_safety", {}).get("status_protected") is None, (
        "a NEW body must NOT be counted protected",
        summary,
    )
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        # the machine re-filled the body it could not have touched had the clear left it
        # EDITED (the mid-test assertion above proved it was empty before this run).
        assert "Translate this line." in seg.target_text and "[es-ES]" in seg.target_text, (
            "the machine must have re-filled the cleared body",
            seg.target_text,
        )
        # FULL_TEXT write-back commits with set_status=False, so the status is left as-is
        # (NEW) -- the escape hatch does NOT hinge on a status flip, only on the refill +
        # the locale no longer being held. The PRE-fix trap was that the clear set EDITED,
        # which the guard then refused, so the body stayed empty here.
        assert seg.status == SegStatus.NEW, (
            "FT set_status=False leaves the re-translated body NEW",
            seg.status,
        )
        # (4) export is NO LONGER held after clear + retranslate (the strand is broken).
        doc = s.get(Document, doc_id)
        assert export.held_locale_reason(s, doc, "es-ES") is None, (
            "after clear+retranslate the locale must be deliverable, not held"
        )
    print("  (21) clearing a full-text body -> NEW -> machine re-fills it, no longer held: OK")


# --- (22) F1 boundary: clearing ONE paragraph of a MULTI-paragraph body stays EDITED ---------
def test_fulltext_clear_one_of_many_stays_edited():
    """F1 whole-body-vs-paragraph semantics: the SEGMENT (the whole body) is the unit of
    status, so ONLY an entirely-empty body goes NEW. Clearing ONE paragraph of a two-paragraph
    body leaves the body non-empty -> it stays EDITED (still human work) and is NOT stranded:
    export still delivers the non-empty body, and a person can retype the cleared paragraph
    (the human editor bypasses the guard). This also pins requirement (3): a NON-empty
    paragraph save sets EDITED and survives an MT rerun byte-identical."""
    from app.models import Document
    from app.services import export
    from app.services import fulltext_paragraphs as fp

    pid = _mk_project("ClearOne22")
    doc_id = _mk_doc(
        pid,
        "es-ES",
        [("First line.\n\nSecond line.", "PrimeraES.\n\nSegundaES.", SegStatus.MT)],
        mode=TransMode.FULL_TEXT,
    )
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        doc.skeleton = {
            "units": [{"text": "First line."}, {"text": "Second line."}],
            "seg_locale": "en-US",
        }
        s.add(doc)
        s.commit()
    # clear ONLY paragraph 0 -> body is still "\n\nSegundaES." (non-empty) -> EDITED, not NEW.
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        r = fp.save_paragraph(s, doc, "es-ES", 0, "", actor="t", ip="127.0.0.1")
        assert r.get("ok") is True, r
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.status == SegStatus.EDITED, (
            "one cleared paragraph of a multi-paragraph body must stay EDITED (body is not empty)",
            seg.status,
        )
        assert seg.target_text.split("\n\n") == ["", "SegundaES."], seg.target_text
        doc = s.get(Document, doc_id)
        # NOT stranded: the body still carries a delivered paragraph, so it is deliverable.
        assert export.held_locale_reason(s, doc, "es-ES") is None, (
            "a partially-cleared body must remain deliverable, not held"
        )
    # (3) the EDITED body survives an MT rerun byte-identical (the guard protects it).
    wf_id = _mk_mt_workflow()
    with Session(engine) as s:
        before = _full_row(_segs(s, doc_id)[0])
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    assert summary.get("lqa_safety", {}).get("status_protected") == 1, summary
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert _full_row(seg) == before, (
            "an EDITED body must survive a rerun byte-identical",
            _full_row(seg),
            before,
        )
    # the human can still retype the cleared paragraph (the editor bypasses the guard).
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        r = fp.save_paragraph(s, doc, "es-ES", 0, "Primera reescrita.", actor="t")
        assert r.get("ok") is True, ("the human refill must land, proving no strand", r)
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text.split("\n\n") == ["Primera reescrita.", "SegundaES."], (
            seg.target_text
        )
        assert seg.status == SegStatus.EDITED, seg.status
    print(
        "  (22) clearing ONE paragraph of a multi-paragraph body stays EDITED, not "
        "stranded; EDITED survives a rerun: OK"
    )


# --- (23) F1: a BLANKED pause-edit resets to NEW (empty), refillable -- never a protected strand -
def test_blanked_pause_edit_resets_to_new_segmented():
    """Review F1 (BLOCKING): clearing a slot in the pause box must reset it to NEW + empty (the
    documented clear-to-reset escape hatch), NOT land ('', EDITED) -- which the status guard would
    then protect FOREVER, so no machine pass could ever refill it (reopening the exact strand class
    ft-clear F1 closed). A subsequent MT run REFILLS the blanked slot; a non-blank sibling edit still
    lands EDITED (protected)."""  # noqa: E501
    pid = _mk_project("BlankPause23")
    # slot 0 carries an inline tag: blanking it would (pre-LOW-fix) leave a spurious
    # tag_integrity badge from the seam (dropped-tag vs the empty target) -- pinned clean below.
    doc_id = _mk_doc(
        pid, "es-ES", [("First ⟦x1⟧ line.", "", SegStatus.NEW), ("Second line.", "", SegStatus.NEW)]
    )
    wf_id = _two_step_wf("BLANK23", StepType.LLM_AGENT)  # [MT, human_review]
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    parts = summ["paused_text"].split(orchestrator.SEG_JOINER)
    assert len(parts) == 2, parts
    # blank slot 0 in the pause box; genuinely edit slot 1
    blob = orchestrator.SEG_JOINER.join(["", "EDITADO SLOT 1"])
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", blob)
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        s0, s1 = _segs(s, doc_id)
        assert (s0.target_text or "") == "" and s0.status == SegStatus.NEW, (
            "a blanked pause-edit must reset to NEW + empty, not ('', EDITED)",
            s0.target_text,
            s0.status,
        )
        # LOW: no spurious tag_integrity badge on the blanked (empty) slot -- the flag would
        # describe a dropped tag on text that no longer exists.
        assert not any(
            isinstance(f, dict) and f.get("type") == "tag_integrity" for f in (s0.qa_flags or [])
        ), ("a blanked slot must carry no residual tag_integrity badge", s0.qa_flags)
        assert s1.target_text == "EDITADO SLOT 1" and s1.status == SegStatus.EDITED, (
            "the non-blank sibling edit lands EDITED",
            s1.target_text,
            s1.status,
        )
    # a subsequent MT run REFILLS the blanked (NEW) slot -- proof it is not stranded/protected
    wf2 = _mk_mt_workflow()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf2, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        s0, s1 = _segs(s, doc_id)
        assert "⟦x1⟧" in (s0.target_text or "") and "[es-ES]" in (s0.target_text or ""), (
            "the machine must refill the blanked (NEW) slot",
            s0.target_text,
        )
        assert s1.target_text == "EDITADO SLOT 1" and s1.status == SegStatus.EDITED, (
            "the EDITED sibling stays protected across the rerun",
            s1.target_text,
            s1.status,
        )
    print("  (23) a blanked pause-edit resets to NEW (refillable); sibling edit stays EDITED: OK")


# --- (24) F2: a TAG-BREAKING pause-edit routes to NEEDS_REVIEW + badge (editor parity) ---------
def test_tag_breaking_pause_edit_routes_to_review():
    """Review F2: a pause-edit that DROPS a source inline tag must land NEEDS_REVIEW with its
    tag_integrity badge -- both direct editors route a tag-damaging human save to review, and
    because EDITED is protected, keeping it EDITED would freeze a structurally broken segment that
    even tag_llm_repair could no longer touch. A CLEAN pause-edit (tag preserved) still lands
    EDITED."""
    pid = _mk_project("TagPause24")
    doc_id = _mk_doc(
        pid,
        "es-ES",
        [("Hello ⟦x1⟧ world.", "", SegStatus.NEW), ("Bye ⟦x1⟧ now.", "", SegStatus.NEW)],
    )
    wf_id = _mk_review_only_workflow("TAG24")
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    # slot 0 DROPS the tag (damage); slot 1 KEEPS it (clean)
    blob = orchestrator.SEG_JOINER.join(["Hola mundo sin etiqueta.", "Adios ⟦x1⟧ ahora."])
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", blob)
    assert rsumm["status"] == "completed", rsumm
    with Session(engine) as s:
        s0, s1 = _segs(s, doc_id)
        assert s0.target_text == "Hola mundo sin etiqueta.", (
            "the edit still LANDS as content",
            s0.target_text,
        )
        assert s0.status == SegStatus.NEEDS_REVIEW, (
            "a tag-breaking pause-edit routes to review, not protected EDITED",
            s0.status,
        )
        assert any(
            isinstance(f, dict) and f.get("type") == "tag_integrity" for f in (s0.qa_flags or [])
        ), ("the tag badge must be present", s0.qa_flags)
        assert s1.target_text == "Adios ⟦x1⟧ ahora." and s1.status == SegStatus.EDITED, (
            "a CLEAN pause-edit (tag preserved) still lands EDITED",
            s1.target_text,
            s1.status,
        )
    print("  (24) a tag-breaking pause-edit -> NEEDS_REVIEW + badge; a clean edit -> EDITED: OK")


# --- (25) INFO-3: an operator SOURCE-text edit survives a post-pause tm_match HEAL -------------
def test_source_pause_edit_survives_in_run_tm_match_heal():
    """Review INFO-3: an operator who types the SOURCE at a pause (the RESIDUAL-1 'leave this in
    English' case) is NOT in `produced` (working == source), so a tm_match step AFTER the pause
    would -- via Part 2 healing of a drifted MACHINE row -- re-fill a 100% TM hit right over the
    operator's deliberate edit. The explicit `j not in operator_edited` heal exclusion prevents
    that: the edit SURVIVES, EDITED, and the heal does not fire on it."""
    pid = _mk_project("SrcHeal25")
    with Session(engine) as s:  # a 100% TM entry for the source
        tm.write_back(s, pid, "Keep this line.", "Garder cette ligne.", "en-US", "es-ES")
    # seed a DRIFTED machine (mt) stored target -> a heal candidate absent the exclusion
    doc_id = _mk_doc(pid, "es-ES", [("Keep this line.", "Un vieux scaffold.", SegStatus.MT)])
    with Session(engine) as s:  # [MT, human_review, tm_match]
        wf = Workflow(name="SRCHEAL25")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(workflow_id=wf.id, order_index=0, name="MT", step_type=StepType.LLM_AGENT)
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=1, name="Review", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=2, name="TM match", step_type=StepType.TM_MATCH
            )
        )
        s.commit()
        wf_id = wf.id
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    # the operator replaces the MT with the SOURCE (leave in English), then resumes
    with Session(engine) as s:
        rsumm = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", "Keep this line.")
    assert rsumm["status"] == "completed", rsumm
    # the post-pause tm_match step must NOT have healed over the operator's edit
    tm_ev = next((st.get("tm_match") for st in rsumm.get("steps", []) if st.get("tm_match")), {})
    assert not tm_ev.get("tm_healed"), ("the heal must NOT fire on an operator-edited slot", tm_ev)
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.target_text == "Keep this line.", (
            "the operator's source edit must SURVIVE the post-pause tm_match heal",
            seg.target_text,
        )
        assert seg.status == SegStatus.EDITED, ("and land EDITED (protected)", seg.status)
        assert not seg.lock_band, ("the heal never locked over the operator edit", seg.lock_band)
    print("  (25) INFO-3: an operator source-text edit survives a post-pause tm_match heal: OK")


# --- (26)/(27) PART 2: the vendor-return sticky-flag guard (2026-08-13 security review) -------
# A human editing a segment while the run is parked at a VENDOR (EXTERNAL) leg resumes through the
# SAME generator + seed loop as a human_review pause -- BUT the vendor-return resume sets
# `_resume_kind == "vendor"` in run.context (vendor_return._resume_after_return), which the
# human_review resume does NOT. Tests (14)/(15) exercise the human_review resume and so never
# cover the vendor-return branch: on it, the RESTORED operator flag/set (persisted at an EARLIER
# pause-edit) must NOT let the VENDOR's returned body ride the operator human_edit channel. These
# two tests stage exactly that resume hermetically -- an earlier pause-edit persists the sticky
# flag, then run.context is stamped `_resume_kind = "vendor"` (the one marker the real vendor
# transport sets) and the run is resumed with the vendor's returned body. Staging the full vendor
# dispatch+return transport (a linked vendor, a signed contract, a claim token) would exercise the
# transport, not the guard; verify_vendor_return covers the transport.
def _review_review_wf(name):
    """A [human_review, human_review] workflow: review A = the operator's pause-edit; review B
    stands in for the run being parked at the vendor/external leg (the resume off it is stamped a
    vendor return)."""
    with Session(engine) as s:
        wf = Workflow(name=name)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="ReviewA", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=1, name="LegB", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        return wf.id


def _stamp_vendor_return(run_id):
    """Mark the parked run as a vendor/external RETURN completing-resume, exactly as
    vendor_return._resume_after_return does (the `_resume_kind = "vendor"` marker set
    UNCONDITIONALLY -- the primary discriminator, INFO-2)."""
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        ctx = dict(run.context or {})
        ctx["_resume_kind"] = "vendor"
        run.context = ctx
        s.add(run)
        s.commit()


def test_ft_vendor_return_does_not_ride_operator_bypass():
    """FULL_TEXT: a run pauses at review A, the operator edits the body (the sticky
    `ft_operator_edited` flag is persisted at review B), then the run resumes as a VENDOR RETURN.
    The restored flag must be CLEARED so the vendor's body commits by VENDOR rules, never via the
    operator human_edit bypass. Two shapes:
      (a) DEFENSE-IN-DEPTH: the DB body is APPROVED (a human sign-off) -> the vendor return must be
          REFUSED at the seam (status_protected), the sign-off byte-intact. WITHOUT the guard the
          restored flag sets human_edit=True and the vendor body OVERWRITES the sign-off.
      (b) REALISTIC: a machine-status body, vendor returns the body verbatim (a pass-through
          'no changes' return) -> it must NOT land EDITED (vendor content must never wear the
          human-protected status). WITHOUT the guard it lands EDITED."""
    # (a) approved body -- the vendor overwrite must be refused
    pid = _mk_project("FtVendorApproved26")
    doc_id = _mk_doc(
        pid,
        "es-ES",
        [("Hello world.", "APROBADO HUMANO", SegStatus.APPROVED)],
        mode=TransMode.FULL_TEXT,
    )
    wf_id = _review_review_wf("FTV26a")
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    with Session(engine) as s:  # operator edits at review A -> parks at B
        r1 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", "EDICION DEL OPERADOR")
    assert r1["status"] == "paused", r1
    with Session(engine) as s:  # the sticky flag is persisted
        run = s.get(WorkflowRun, summ["run_id"])
        assert (run.context or {}).get("ft_operator_edited") is True, run.context
    _stamp_vendor_return(summ["run_id"])
    with Session(engine) as s:  # resume AS A VENDOR RETURN with a new body
        r2 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", "CUERPO DEL VENDOR")
    assert r2["status"] == "completed", r2
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert (seg.target_text, seg.status) == ("APROBADO HUMANO", SegStatus.APPROVED), (
            "the vendor return must be REFUSED over an approved body, not ride the operator "
            "human_edit bypass and overwrite the sign-off",
            seg.target_text,
            seg.status,
        )
    assert r2.get("lqa_safety", {}).get("status_protected") == 1, r2

    # (b) machine-status body, vendor returns it verbatim -> must NOT land EDITED
    pid = _mk_project("FtVendorNoop26")
    doc_id = _mk_doc(
        pid, "es-ES", [("Hello world.", "MT ORIGINAL", SegStatus.MT)], mode=TransMode.FULL_TEXT
    )
    wf_id = _review_review_wf("FTV26b")
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    op_edit = "EDICION DEL OPERADOR B"
    with Session(engine) as s:
        r1 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", op_edit)
    assert r1["status"] == "paused", r1
    _stamp_vendor_return(summ["run_id"])
    with Session(engine) as s:  # the vendor returns the body verbatim (no-op)
        r2 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", op_edit)
    assert r2["status"] == "completed", r2
    with Session(engine) as s:
        (seg,) = _segs(s, doc_id)
        assert seg.status != SegStatus.EDITED, (
            "a vendor RETURN's body must never wear the operator's EDITED (human-protected) status "
            "-- the restored sticky flag rode the human_edit channel",
            seg.status,
        )
    print(
        "  (26) FULL_TEXT vendor return: the restored sticky flag does NOT ride the operator "
        "human_edit bypass (approved body refused; a no-op vendor body is not EDITED): OK"
    )


def test_segmented_vendor_return_per_slot_sticky_guard():
    """SEGMENTED: the restored operator set must be honoured PER SLOT on a vendor return.
    * A slot the vendor did NOT change (working == the pre-dispatch parked blob) KEEPS its
      operator stickiness -> the earlier edit still lands EDITED (protected). WITHOUT the per-slot
      guard the wholesale `not _is_vendor_return` drop lands it `mt` (protection lost) -- or, for
      an APPROVED slot, DISCARDS the operator's edit back to the stored target.
    * A slot the vendor DID change (working != parked) is SUPERSEDED -> the operator flag drops
      and the vendor's content lands by vendor rules (never EDITED via the human bypass)."""
    # slot 0: operator edits it at review A; slot 1: the vendor changes it on return.
    pid = _mk_project("SegVendor27")
    doc_id = _mk_doc(
        pid, "es-ES", [("First line.", "", SegStatus.NEW), ("Second line.", "", SegStatus.NEW)]
    )
    wf_id = _review_review_wf("SGV27")
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    partsA = summ["paused_text"].split(orchestrator.SEG_JOINER)
    assert len(partsA) == 2, partsA
    op0 = "EDITADO POR OPERADOR SLOT 0"
    with Session(engine) as s:  # operator edits ONLY slot 0 at review A
        r1 = orchestrator.resume_workflow(
            s, summ["run_id"], "es-ES", orchestrator.SEG_JOINER.join([op0, partsA[1]])
        )
    assert r1["status"] == "paused", r1
    partsB = r1["paused_text"].split(orchestrator.SEG_JOINER)
    with Session(engine) as s:
        run = s.get(WorkflowRun, summ["run_id"])
        assert 0 in set((run.context or {}).get("operator_edited") or []), run.context
    _stamp_vendor_return(summ["run_id"])
    # the vendor return: slot 0 UNCHANGED (the vendor never touched the operator's slot), slot 1
    # replaced with the vendor's translation.
    vendor_blob = orchestrator.SEG_JOINER.join([partsB[0], "TRADUCCION DEL VENDOR SLOT 1"])
    with Session(engine) as s:
        r2 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", vendor_blob)
    assert r2["status"] == "completed", r2
    with Session(engine) as s:
        s0, s1 = _segs(s, doc_id)
        assert (s0.target_text, s0.status) == (op0, SegStatus.EDITED), (
            "the operator's edit to a slot the vendor did NOT touch must KEEP its EDITED protection "  # noqa: E501
            "across the vendor return",
            s0.target_text,
            s0.status,
        )
        assert s1.target_text == "TRADUCCION DEL VENDOR SLOT 1" and s1.status != SegStatus.EDITED, (
            "the vendor-returned slot lands by vendor rules, never EDITED",
            s1.target_text,
            s1.status,
        )

    # the DEFENSIVE direction: the vendor CHANGES the operator's slot -> vendor supersedes.
    pid = _mk_project("SegVendorSupersede27")
    doc_id = _mk_doc(
        pid, "es-ES", [("First line.", "", SegStatus.NEW), ("Second line.", "", SegStatus.NEW)]
    )
    wf_id = _review_review_wf("SGVS27")
    with Session(engine) as s:
        summ = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summ["status"] == "paused", summ
    partsA = summ["paused_text"].split(orchestrator.SEG_JOINER)
    with Session(engine) as s:
        r1 = orchestrator.resume_workflow(
            s, summ["run_id"], "es-ES", orchestrator.SEG_JOINER.join(["EDITADO SLOT 0", partsA[1]])
        )
    assert r1["status"] == "paused", r1
    partsB = r1["paused_text"].split(orchestrator.SEG_JOINER)
    _stamp_vendor_return(summ["run_id"])
    vendor_blob = orchestrator.SEG_JOINER.join(["EL VENDOR REEMPLAZO SLOT 0", partsB[1]])
    with Session(engine) as s:
        r2 = orchestrator.resume_workflow(s, summ["run_id"], "es-ES", vendor_blob)
    assert r2["status"] == "completed", r2
    with Session(engine) as s:
        s0, _s1 = _segs(s, doc_id)
        assert s0.target_text == "EL VENDOR REEMPLAZO SLOT 0" and s0.status != SegStatus.EDITED, (
            "when the vendor CHANGES the operator's slot it supersedes -> vendor content lands by "
            "vendor rules, the stale operator flag drops (never EDITED via the human bypass)",
            s0.target_text,
            s0.status,
        )
    print(
        "  (27) segmented vendor return: the restored operator set is honoured PER SLOT "
        "(untouched slot stays EDITED; a vendor-changed slot supersedes): OK"
    )


if __name__ == "__main__":
    test_approved_segment_preserved()
    test_edited_segment_preserved()
    test_needs_review_not_protected()
    test_vendor_return_refused_on_approved()
    test_key_import_refused_on_approved()
    test_seam_freshness_reads_db_status()
    test_fulltext_approved_body_preserved()
    test_protection_ungated_by_kill_switches()
    test_human_para_edit_bypasses_status_protection()
    test_pause_edit_lands_on_approved()
    test_pause_edit_lands_on_edited()
    test_blank_machine_write_refused_on_approved()
    test_refusal_sink_surfaces_machine_refusal()
    test_freshness_in_memory_and_no_autoflush()
    test_concurrent_approval_survives_untouched_resume_segmented()
    test_concurrent_approval_survives_untouched_resume_fulltext()
    test_concurrent_approval_and_operator_edit_of_other_slot()
    test_operator_pause_text_to_source_lands()
    test_resume_refuses_a_non_paused_run()
    test_ft_protected_body_still_pauses_and_records_steps()
    test_operator_edit_sticky_across_two_pauses_segmented()
    test_operator_edit_sticky_across_two_pauses_fulltext()
    test_sticky_edit_then_mt_step_lands_segmented()
    test_fulltext_clear_whole_body_hands_back_to_machine()
    test_fulltext_clear_one_of_many_stays_edited()
    test_blanked_pause_edit_resets_to_new_segmented()
    test_tag_breaking_pause_edit_routes_to_review()
    test_source_pause_edit_survives_in_run_tm_match_heal()
    test_ft_vendor_return_does_not_ride_operator_bypass()
    test_segmented_vendor_return_per_slot_sticky_guard()
    print("STATUS PROTECTION OK")
