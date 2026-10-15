"""Status-ladder transitions + locked-row invariants + partial-tranche accounting +
the editor's edit/confirm/reviewer flow."""

from __future__ import annotations

import pytest
from conftest import sample_contract
from vstudio import delivery, editor, intake
from vstudio.models import VProjectStatus, VSegStatus


def test_ladder_forward_only_and_cancel_terminal(session):
    proj = intake.store_dispatch(session, sample_contract())
    assert proj.status == VProjectStatus.NEW
    intake.set_status(session, proj, VProjectStatus.IN_PROGRESS)
    assert proj.status == VProjectStatus.IN_PROGRESS
    # cannot go backward.
    with pytest.raises(ValueError):
        intake.set_status(session, proj, VProjectStatus.NEW)
    # can cancel from mid-ladder.
    intake.set_status(session, proj, VProjectStatus.CANCELLED)
    # cancelled is terminal.
    with pytest.raises(ValueError):
        intake.set_status(session, proj, VProjectStatus.IN_PROGRESS)


def test_can_transition_table():
    ct = intake.can_transition
    assert ct(VProjectStatus.NEW, VProjectStatus.CLAIMED)
    assert ct(VProjectStatus.NEW, VProjectStatus.IN_PROGRESS)  # skip forward ok
    assert not ct(VProjectStatus.IN_QA, VProjectStatus.CLAIMED)
    assert not ct(VProjectStatus.DELIVERED, VProjectStatus.CANCELLED)
    assert ct(VProjectStatus.IN_PROGRESS, VProjectStatus.CANCELLED)


def test_locked_segment_never_editable(session):
    proj = intake.store_dispatch(session, sample_contract(locked_last=True))
    locked = delivery.project_segments(session, proj)[-1]
    with pytest.raises(editor.EditError):
        editor.patch_target(session, proj, locked, "hacked", actor="x")
    with pytest.raises(editor.EditError):
        editor.confirm_segment(session, proj, locked, actor="x")
    # its target is unchanged (as dispatched).
    session.refresh(locked)
    assert locked.target == "Tienes 3 mensajes."


def test_progress_excludes_locked_from_denominator(session):
    proj = intake.store_dispatch(session, sample_contract(locked_last=True))
    segs = [s for s in delivery.project_segments(session, proj) if not s.locked]
    for s in segs:
        editor.patch_target(session, proj, s, f"[{s.source}]", actor="t")
        editor.confirm_segment(session, proj, s, actor="t")
    prog = editor.progress(session, proj)
    assert prog["total"] == 2 and prog["done"] == 2 and prog["pct"] == 100
    assert prog["locked"] == 1


def test_edit_sets_edited_then_confirm_records_actor(session):
    proj = intake.store_dispatch(session, sample_contract())
    seg = delivery.project_segments(session, proj)[1]
    editor.patch_target(session, proj, seg, "Hola", actor="translator-1")
    assert seg.status == VSegStatus.EDITED
    editor.confirm_segment(session, proj, seg, actor="translator-1")
    assert seg.status == VSegStatus.CONFIRMED and seg.confirmed_by == "translator-1"
    # editing a confirmed segment drops it back to EDITED.
    editor.patch_target(session, proj, seg, "Hola de nuevo", actor="translator-1")
    assert seg.status == VSegStatus.EDITED and seg.confirmed_by == ""


def test_reviewer_reject_routes_back_to_edited_with_tag(session):
    proj = intake.store_dispatch(session, sample_contract())
    seg = delivery.project_segments(session, proj)[1]
    editor.patch_target(session, proj, seg, "Hola", actor="t")
    editor.confirm_segment(session, proj, seg, actor="t")
    editor.reviewer_reject(session, proj, seg, "mistranslation", actor="rev")
    assert seg.status == VSegStatus.EDITED and seg.issue_tag == "mistranslation"
    editor.reviewer_approve(session, proj, seg, actor="rev")
    assert seg.status == VSegStatus.APPROVED and seg.issue_tag == ""


def test_partial_tranche_ledger_and_delivered_ids(session):
    proj = intake.store_dispatch(session, sample_contract())
    ids = [s.contract_id for s in delivery.project_segments(session, proj)]
    # Simulate a delivered first tranche.
    proj.delivered_ids = ids[:1]
    session.add(proj)
    session.commit()
    assert delivery.outstanding_ids(session, proj) == ids[1:]
    # A re-delivery of an already-delivered id is filtered out by deliver()'s tranche calc.
    # (deliver() itself needs a live Localization OS; the outstanding-set math is the invariant.)
    proj.delivered_ids = ids
    session.add(proj)
    session.commit()
    assert delivery.outstanding_ids(session, proj) == []
