"""Every deterministic Tier-1 QA check, as a pure function, plus the ADVISORY findings
summary. QA never blocks delivery (decided 2026-08-08 — see the decision record in
`vstudio/qa_engine.py`); these tests pin the advisory semantics."""

from __future__ import annotations

from conftest import sample_contract
from vstudio import delivery, intake, qa, qa_engine
from vstudio.models import VSegment


def test_empty_target():
    assert qa.check_empty_target("hi", "") and qa.check_empty_target("hi", "   ")
    assert not qa.check_empty_target("hi", "hola")


def test_target_equals_source_scoped_to_non_english():
    assert qa.check_target_equals_source("Colour", "Colour", "es-ES")
    assert not qa.check_target_equals_source("Colour", "Colour", "en-GB")
    assert not qa.check_target_equals_source("Colour", "Color", "es-ES")


def test_placeholder_mismatch_covers_loc_tags_curly_and_printf():
    # ⟦xN⟧ dropped.
    assert qa.check_placeholders("Hi ⟦x1⟧", "Hola")
    # {name} added.
    assert qa.check_placeholders("Hi", "Hola {name}")
    # %s preserved -> clean.
    assert not qa.check_placeholders("Hi %s", "Hola %s")
    # declared multiset wins over the source extraction.
    assert not qa.check_placeholders("whatever", "keep ⟦x1⟧", declared=["⟦x1⟧"])
    assert qa.check_placeholders("whatever", "drop it", declared=["⟦x1⟧"])


def test_number_mismatch():
    assert qa.check_numbers("You have 3 apples", "Tienes 4 manzanas")
    assert not qa.check_numbers("You have 3 apples", "Tienes 3 manzanas")
    # locale grouping normalizes.
    assert not qa.check_numbers("1,000 items", "1000 items")


def test_length_overflow():
    assert qa.check_length("x", "abcdef", 3)
    assert not qa.check_length("x", "abc", 3)
    assert not qa.check_length("x", "abcdef", None)


def test_spaces_and_repeats_and_brackets():
    assert qa.check_spaces("x", " hola")
    assert qa.check_spaces("x", "hola  mundo")
    assert not qa.check_spaces("x", "hola mundo")
    assert qa.check_repeated_word("x", "el el gato")
    assert not qa.check_repeated_word("x", "el gato")
    assert qa.check_unpaired("x", "hola (mundo")
    assert qa.check_unpaired("x", 'say "hi')
    assert not qa.check_unpaired("x", "hola (mundo)")


def test_glossary_required_forbidden_and_dnt():
    gl = [
        {"term": "cat", "target": "gato"},
        {"term": "damn", "forbidden": True},
        {"term": "Acme", "dnt": True},
    ]
    # required term missing.
    assert any(
        f["check"] == qa.GLOSSARY_MISSING for f in qa.check_glossary("the cat", "el felino", gl)
    )
    # required term present -> clean for that lane.
    assert not any(
        f["check"] == qa.GLOSSARY_MISSING for f in qa.check_glossary("the cat", "el gato", gl)
    )
    # forbidden term used in target.
    assert any(f["check"] == qa.FORBIDDEN_TERM for f in qa.check_glossary("x", "damn it", gl))
    # DNT term dropped from target.
    assert any(
        f["check"] == qa.FORBIDDEN_TERM
        for f in qa.check_glossary("Acme rocks", "Rocas geniales", gl)
    )


def test_unedited_mt_check_is_removed():
    """Decided 2026-08-08: the unedited-MT check is GONE (an unchanged MT means the MT was
    good). It must not exist in the module, the registry, or the default severities."""
    assert not hasattr(qa, "check_unedited_mt")
    assert not hasattr(qa, "UNEDITED_MT")
    assert "unedited_mt" not in qa.ALL_CHECKS
    assert "unedited_mt" not in qa.DEFAULT_SEVERITIES


def test_no_unignorable_registry():
    """Advisory-only: there is no unignorable-check concept any more."""
    assert not hasattr(qa, "UNIGNORABLE")


def test_job_consistency_same_source_different_target():
    segs = [
        {"source": "OK", "target": "Vale"},
        {"source": "OK", "target": "De acuerdo"},
        {"source": "Other", "target": "Otro"},
    ]
    out = qa.check_job_consistency(segs)
    assert set(out.keys()) == {0, 1}
    assert 2 not in out


def test_locked_segment_is_excluded_from_checks():
    seg = {"source": "Hi", "target": "", "locked": True}
    assert qa.check_segment(seg) == []


def test_locked_row_excluded_from_consistency_corpus():
    """A LOCKED row must not create a same-source->different-target conflict against an
    editable row (it is returned as-dispatched, not a translator's choice)."""
    segs = [
        {"source": "OK", "target": "Vale"},  # editable
        {"source": "OK", "target": "Distinto", "locked": True},
    ]  # locked, different
    # No conflict: the locked row is not in the corpus.
    assert qa.check_job_consistency(segs) == {}
    # Two editable rows with the same source but different targets DO conflict (control).
    segs2 = [{"source": "OK", "target": "Vale"}, {"source": "OK", "target": "Distinto"}]
    assert set(qa.check_job_consistency(segs2).keys()) == {0, 1}


# --- advisory summary + severities + ignores ---------------------------------
def test_gate_is_advisory_summary_never_a_verdict(session):
    """`gate()` is an advisory SUMMARY, not a blocking verdict: it carries findings + counts
    and has NO ok/blocking keys, regardless of how many findings exist."""
    proj = intake.store_dispatch(session, sample_contract())
    segs = session.query(VSegment).order_by(VSegment.order_index).all()
    # seg 2/3 empty -> empty_target findings; still no ok/blocking semantics.
    g = qa_engine.gate(session, proj)
    assert "ok" not in g and "blocking" not in g, ("gate must be advisory-only", g)
    assert g["open"] >= 2 and g["total"] == g["open"] and g["findings"]
    # Fill everything cleanly -> zero open findings.
    for s in segs:
        s.target = f"[{s.source}]"  # preserves ⟦x1⟧/numbers, not a source echo
        session.add(s)
    session.commit()
    g2 = qa_engine.gate(session, proj)
    assert g2["open"] == 0, g2["findings"]


def test_locked_blank_segment_no_findings_and_absent_from_summary(session):
    """(a) Localization OS's omit_locked_targets sends a locked row with a BLANK target. It must
    produce NO findings (no empty_target/target==source) and add nothing to the summary."""
    from vstudio import editor

    proj = intake.store_dispatch(session, sample_contract(locked_blank=True))
    segs = delivery.project_segments(session, proj)
    locked = segs[-1]
    assert locked.locked and locked.target == "", "the locked row arrived blank"
    # Fill + confirm the two editable rows cleanly.
    for s in segs[:-1]:
        editor.patch_target(session, proj, s, f"[{s.source}]", actor="t")
        editor.confirm_segment(session, proj, s, actor="t")
    findings = qa_engine.run_findings(session, proj)
    assert findings.get(locked.id, []) == [], "a locked blank row must have no findings"
    g = qa_engine.gate(session, proj)
    assert g["open"] == 0, ("a locked blank row must add no open finding", g["findings"])


def test_delivery_of_blank_locked_package_returns_ids_blank_as_dispatched(session):
    """(b) A package whose locked rows are blank returns those ids blank-as-dispatched — the
    builder never invents a target for a locked row (a contract guard, independent of QA)."""
    from vstudio import editor

    proj = intake.store_dispatch(session, sample_contract(locked_blank=True))
    segs = delivery.project_segments(session, proj)
    for s in segs[:-1]:
        editor.patch_target(session, proj, s, f"[{s.source}]", actor="t")
        editor.confirm_segment(session, proj, s, actor="t")
    ids = [s.contract_id for s in segs]
    seg_ids = {s.id for s in segs}
    # The advisory summary over the full tranche has no open finding (clean editable rows).
    assert qa_engine.gate(session, proj, only_ids=seg_ids)["open"] == 0
    payload = delivery.build_return_payload(session, proj, ids)
    got = {p["id"]: p["target"] for p in payload["segments"]}
    assert got[segs[-1].contract_id] == "", "the locked row must return blank-as-dispatched"
    # ...and every dispatched id is present exactly once.
    assert sorted(got.keys()) == sorted(ids)


def test_placeholder_mismatch_is_advisory_and_dismissable(session):
    """The placeholder-mismatch check STAYS as a finding, but it is advisory: it is no longer
    unignorable (it can be dismissed for bookkeeping) and it never yields a blocking verdict."""
    proj = intake.store_dispatch(session, sample_contract())
    seg = session.query(VSegment).order_by(VSegment.order_index).first()
    seg.target = "Hola mundo sin etiqueta"  # dropped ⟦x1⟧
    session.add(seg)
    session.commit()
    f = next(
        x
        for x in qa_engine.run_findings(session, proj)[seg.id]
        if x["check"] == qa.PLACEHOLDER_MISMATCH
    )
    assert f["ignored"] is False
    # It CAN now be dismissed for bookkeeping (no unignorable rule any more).
    qa_engine.ignore_finding(
        session,
        proj,
        seg.id,
        qa.PLACEHOLDER_MISMATCH,
        f.get("detail_sig", ""),
        "test payload, waved through",
    )
    after = next(
        x
        for x in qa_engine.run_findings(session, proj)[seg.id]
        if x["check"] == qa.PLACEHOLDER_MISMATCH
    )
    assert after["ignored"] is True
    # And the summary is never a blocking verdict.
    g = qa_engine.gate(session, proj)
    assert "ok" not in g and "blocking" not in g


def test_any_finding_is_dismissable_for_bookkeeping(session):
    """Advisory-only: ANY finding can be dismissed — including an error-severity one — since
    a dismissal is pure bookkeeping and unblocks nothing."""
    proj = intake.store_dispatch(session, sample_contract())
    seg = session.query(VSegment).order_by(VSegment.order_index).all()[1]
    seg.target = "hola  mundo"  # double space (warning) + fine otherwise
    session.add(seg)
    session.commit()
    findings = qa_engine.run_findings(session, proj)[seg.id]
    ds = next(f for f in findings if f["check"] == qa.DOUBLE_SPACE)
    qa_engine.ignore_finding(session, proj, seg.id, qa.DOUBLE_SPACE, ds["detail_sig"], "cosmetic")
    after = [  # noqa: RUF015
        f for f in qa_engine.run_findings(session, proj)[seg.id] if f["check"] == qa.DOUBLE_SPACE
    ][0]
    assert after["ignored"] is True
    # Even after promoting the check to ERROR severity, a dismissal is accepted (no raise).
    qa_engine.set_severity(session, qa.DOUBLE_SPACE, "error")
    qa_engine.ignore_finding(session, proj, seg.id, qa.DOUBLE_SPACE, "", "still fine")
