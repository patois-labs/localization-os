"""AI Tagging 1.5c - the LLM tag-repair rung (smart post-translation tag re-alignment).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network,
MockProvider forced). NO real provider call is ever made: every LLM turn is a scripted
stub installed over `tag_llm_repair.run_completion`, so the suite proves the CONTRACT
(what the rung accepts, rejects, and writes) rather than any model's behavior.

What is proven:

  1. describe_damage: names each affected token individually and quotes the SOURCE span
     a dropped paired token wraps (the "explicit instructions" the design promises).
  2. LOCK 2 (body preservation): token-only edits pass; a retranslation, a dropped
     clause, an appended commentary, and a re-punctuation all FAIL -> candidate binned.
  3. LOCK 1 (structural): a candidate with the wrong token multiset/order/nesting or a
     residual sentinel is binned even when its body is preserved.
  4. LOCK 3 (placement verification): agrees -> clean; disagrees -> repaired-but-flagged;
     UNAVAILABLE (dead engine / junk answer / error) -> FAILS CLOSED to flagged, never
     silently clean.
  5. repair() self-disables: kill switch off, verify switch off, offline provider,
     oversized segment, already-clean target, tagless source.
  6. commit_mt_output wiring: no repairer injected -> byte-identical quarantine (the
     pre-1.5c behavior); verified -> committed CLEAN + TM-eligible + no flag; unverified
     -> committed + tag_integrity + NEEDS_REVIEW + TM-INELIGIBLE; a candidate that fails
     the gate is DISCARDED (never written); a raising repairer cannot break the write
     path; the deterministic rung still wins first and the LLM rung is NOT consulted.
  7. repair_stored_segments: dry-run writes NOTHING, --apply writes, a verified repair
     clears the tag flag (and only returns MT when no other flag remains), an unverified
     one keeps the segment in review, and --limit truncates + reports.
  8. CLI `tagging repair` is registered, dry-run-by-default, and honors the kill switch.

Final line: "TAG LLM REPAIR OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
# Never die on a non-ASCII glyph under a cp1252 pipe (the tokens below are non-ASCII;
# every print in this file is pure ASCII, this belt covers assertion messages).
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

init_env("loctms_tagllm_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    ProjectType,
    Segment,
    SegStatus,
)
from app.services import content, keys, quality, settings, tag_llm_repair

init_db()
# The SHIPPED default for `tag_llm_repair_enabled` is "0" (the rung ships DARK pending the
# 2026-07-25 review's CRITICAL/HIGH fixes).  This suite exercises
# the rung itself, so it turns it ON explicitly; test 5 still proves the OFF path.
settings.set_many({"tag_llm_repair_enabled": "1"})

_N = [0]


def _seq():
    _N[0] += 1
    return _N[0]


# --- scripted provider stubs -------------------------------------------------
# `tag_llm_repair` imports run_completion/provider_is_live INTO its own namespace, so
# patching the module attributes intercepts every call the rung makes. Nothing else in
# the app is affected and no socket is ever opened.
_REAL_RUN = tag_llm_repair.run_completion
_REAL_LIVE = tag_llm_repair.provider_is_live


def _script(*turns):
    """Install a stub that returns `turns` in order (each a dict merged over a success
    envelope, or an Exception instance to raise). Returns the recorded call list."""
    calls = []
    seq = list(turns)

    def _fake(spec, prompt, system=None, task=None, payload=None, tap_meta=None, max_tokens=None):
        calls.append({"task": task, "prompt": prompt, "spec": spec})
        turn = seq.pop(0) if seq else {"text": ""}
        if isinstance(turn, Exception):
            raise turn
        env = {
            "text": "",
            "error": None,
            "usage": {},
            "cost_usd": 0.0,
            "provider_used": "stub",
            "model_used": "stub",
            "attempts": 1,
            "truncated": False,
            "failover": False,
        }
        env.update(turn)
        return env

    tag_llm_repair.run_completion = _fake
    tag_llm_repair.provider_is_live = lambda *_a, **_k: True
    return calls


def _unscript():
    tag_llm_repair.run_completion = _REAL_RUN
    tag_llm_repair.provider_is_live = _REAL_LIVE


def _ok_verdict():
    return {"text": '{"correct": true}'}


def _bad_verdict(note="wraps the wrong phrase"):
    return {"text": f'{{"correct": false, "note": "{note}"}}'}


# --- fixtures ----------------------------------------------------------------
SRC = "Please read ⟦r1⟧the quarterly report⟦/r1⟧ before Friday."
# A target that DROPPED the pair entirely -- the exact case Phase 1 could only quarantine.
TGT_DROPPED = "Lea el informe trimestral antes del viernes."
TGT_FIXED = "Lea ⟦r1⟧el informe trimestral⟦/r1⟧ antes del viernes."


def _mk_seg(
    session, *, source, target, status=SegStatus.NEEDS_REVIEW, qa_flags=None, locale="es-ES"
):
    p = Project(name="tagllm-%d" % _seq(), org_id=1, source_locale="en-US", default_provider="mock")  # noqa: UP031
    session.add(p)
    session.commit()
    session.refresh(p)
    d = Document(project_id=p.id, filename="d.docx")
    session.add(d)
    session.commit()
    session.refresh(d)
    seg = Segment(
        document_id=d.id,
        target_locale=locale,
        order_index=0,
        source_text=source,
        target_text=target,
        status=status,
        qa_flags=qa_flags,
    )
    session.add(seg)
    session.commit()
    session.refresh(seg)
    return seg


# --- 1. describe_damage ------------------------------------------------------
def test_describe_damage():
    brief = tag_llm_repair.describe_damage(SRC, TGT_DROPPED)
    assert "⟦r1⟧" in brief and "⟦/r1⟧" in brief, brief
    # The source span must be quoted so the model knows WHAT to wrap -- this is the
    # whole difference between "a tag is missing" and an actionable instruction.
    assert "the quarterly report" in brief, brief
    assert "MISSING" in brief, brief

    # A surplus token the source never had is described as an ADD (remove it), and a
    # genuine duplicate is described with both counts.
    added = tag_llm_repair.describe_damage("a ⟦x1⟧ b", "a ⟦x1⟧ b ⟦x9⟧")
    assert "⟦x9⟧" in added and "NOT in the source" in added, added
    dup = tag_llm_repair.describe_damage("a ⟦x1⟧ b", "a ⟦x1⟧ b ⟦x1⟧")
    assert "2 times" in dup and "1 time" in dup, dup

    # Pure reorder names the source order explicitly.
    ro = tag_llm_repair.describe_damage("⟦x1⟧a⟦x2⟧", "⟦x2⟧a⟦x1⟧")
    assert "WRONG" in ro and "ORDER" in ro, ro

    # A point token is described as standalone, never as a wrapper.
    pt = tag_llm_repair.describe_damage("a ⟦x1⟧ b", "a b")
    assert "standalone" in pt, pt
    print("  [1] describe_damage names every token + quotes the source span")


# --- 2. LOCK 2: body preservation -------------------------------------------
def test_body_preservation_lock():
    bp = tag_llm_repair.body_preserved
    # Inserting tags changes no words -> passes.
    assert bp(TGT_DROPPED, TGT_FIXED)
    # Whitespace at the insertion seam is normalized, not penalized.
    assert bp("Lea el informe.", "Lea  ⟦x1⟧ el informe.")
    # A retranslation / reword FAILS -- this is the lock that keeps an unattended LLM
    # edit from silently rewriting translated content.
    assert not bp(TGT_DROPPED, "Lea ⟦r1⟧el reporte trimestral⟦/r1⟧ antes del viernes.")
    # A dropped clause FAILS.
    assert not bp(TGT_DROPPED, "Lea ⟦r1⟧el informe trimestral⟦/r1⟧.")
    # Appended commentary FAILS.
    assert not bp(TGT_DROPPED, TGT_FIXED + " (I fixed the tags for you!)")
    # Re-punctuation FAILS (a word-level change is a word-level change).
    assert not bp(TGT_DROPPED, TGT_FIXED.replace("viernes.", "viernes!"))
    print("  [2] LOCK 2 body preservation: tags-only passes; reword/drop/append/repunct fail")


# --- 3. LOCK 1: structural ---------------------------------------------------
def test_structural_lock_binds():
    # Body preserved but the model renumbered the tokens -> structurally wrong -> None.
    calls = _script(
        {"text": "Lea ⟦r2⟧el informe trimestral⟦/r2⟧ antes del viernes."}, _ok_verdict()
    )
    try:
        assert tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec()) is None
        # Only the REPAIR call was made -- a candidate binned by lock 1 must never reach
        # the (paid) verification call.
        assert [c["task"] for c in calls] == ["tag_repair"], calls
    finally:
        _unscript()

    # Body preserved, tokens right, but nesting crossed -> binned.
    src2 = "⟦g1⟧a ⟦g2⟧b⟦/g2⟧⟦/g1⟧"
    calls = _script({"text": "⟦g1⟧a ⟦g2⟧b⟦/g1⟧⟦/g2⟧"}, _ok_verdict())
    try:
        assert tag_llm_repair.repair(src2, "a b", spec=_spec()) is None
    finally:
        _unscript()

    # A residual masking sentinel in the candidate -> binned (never ship sentinel junk).
    calls = _script(
        {"text": "Lea «TAGdeadbe_1»el informe trimestral⟦/r1⟧ antes del viernes."}, _ok_verdict()
    )
    try:
        assert tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec()) is None
    finally:
        _unscript()

    # And LOCK 2: a structurally PERFECT candidate that retranslated is still binned.
    calls = _script(
        {"text": "Lea ⟦r1⟧el reporte trimestral⟦/r1⟧ antes del viernes."}, _ok_verdict()
    )
    try:
        assert tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec()) is None
        assert [c["task"] for c in calls] == ["tag_repair"], calls
    finally:
        _unscript()
    print(
        "  [3] LOCK 1 binds renumber/crossed-nesting/residual; LOCK 2 binds a perfect retranslation"
    )


def _spec():
    from app.services.providers import ProviderSpec

    return ProviderSpec(provider="stub", model="stub-model")


# --- 4. LOCK 3: placement verification --------------------------------------
def test_placement_verification():
    # Agrees -> clean.
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        res = tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec())
        assert res is not None and res.clean and res.verified, res
        assert res.text == TGT_FIXED
    finally:
        _unscript()

    # Disagrees -> repaired but NOT clean, and the reason is carried through.
    _script({"text": TGT_FIXED}, _bad_verdict("wraps the verb, not the noun phrase"))
    try:
        res = tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec())
        assert res is not None and not res.clean and not res.verified, res
        assert res.text == TGT_FIXED
        assert "wraps the verb" in res.note, res.note
    finally:
        _unscript()

    # FAILS CLOSED: verifier junk / error / dead engine must never read as approval.
    from app.services.providers import DeadEngineError

    for turn in (
        {"text": "sure looks fine to me"},
        {"text": "", "error": "boom"},
        DeadEngineError("engine gone"),
        # A JSON ARRAY verdict (or a prose-wrapped one) parses to a non-dict; llm_json
        # now preserves that shape, so `_verify_once` must guard it. Pre-guard the bare
        # `data.get("correct")` raised AttributeError OUTSIDE the try -> an uncaught
        # crash; a non-object verdict is an unusable answer -> NOT clean (fail closed).
        {"text": '[{"correct": true}]'},
        {"text": 'The verdict is:\n[{"correct": true}]'},
    ):
        _script({"text": TGT_FIXED}, turn)
        try:
            res = tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec())
            assert res is not None and not res.clean, (turn, res)
        finally:
            _unscript()

    # The verifier prompt must NOT mention that a repair happened (it is an independent
    # reader, not a rubber stamp on the previous turn).
    calls = _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec())
        vp = [c for c in calls if c["task"] == "tag_verify"][0]["prompt"]  # noqa: RUF015
        for leak in ("repair", "fixed", "MISSING", "was broken"):
            assert leak not in vp, (leak, vp[:200])
    finally:
        _unscript()
    # UNANIMITY (live-QA hardening): the default is 2 agreeing verdicts. One dissent
    # among them means NOT verified -- and the rung must SHORT-CIRCUIT so a rejection
    # never pays for the remaining calls.
    calls = _script({"text": TGT_FIXED}, _ok_verdict(), _bad_verdict("second reader disagrees"))
    try:
        res = tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec())
        assert res is not None and not res.clean, res
        assert "disagrees" in res.note, res.note
        assert len([c for c in calls if c["task"] == "tag_verify"]) == 2, calls
    finally:
        _unscript()
    calls = _script({"text": TGT_FIXED}, _bad_verdict(), _ok_verdict())
    try:
        tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec())
        assert len([c for c in calls if c["task"] == "tag_verify"]) == 1, (
            "a rejection must short-circuit the remaining votes"
        )
    finally:
        _unscript()

    # The VERIFY engine is separately addressable, because verdict quality is
    # engine-bound (measured: an 8B local model approved misplaced tags 2/3 of the time
    # while claude-haiku was 4/4). verify_spec must be used for tag_verify calls and
    # NEVER for the repair call.
    from app.services.providers import ProviderSpec

    vspec = ProviderSpec(provider="judge", model="judge-model")
    calls = _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec(), verify_spec=vspec)
        by_task = {c["task"]: c["spec"] for c in calls}
        assert by_task["tag_repair"].provider == "stub", by_task
        assert by_task["tag_verify"].provider == "judge", by_task
    finally:
        _unscript()
    print("  [4] LOCK 3 agrees->clean, disagrees->flagged, unavailable->FAILS CLOSED,")
    print("      prompt independent, unanimity + short-circuit, verify_spec honored")


# --- 5. self-disabling -------------------------------------------------------
def test_rung_self_disables():
    # Kill switch off -> no call at all.
    settings.set_many({"tag_llm_repair_enabled": "0"})
    calls = _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        assert tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec()) is None
        assert calls == []
    finally:
        _unscript()
        settings.set_many({"tag_llm_repair_enabled": "1"})

    # Verify switch off -> repair happens, placement NEVER certified -> flagged.
    settings.set_many({"tag_llm_repair_verify_enabled": "0"})
    calls = _script({"text": TGT_FIXED})
    try:
        res = tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec())
        assert res is not None and not res.clean and not res.verified
        assert [c["task"] for c in calls] == ["tag_repair"], calls
    finally:
        _unscript()
        settings.set_many({"tag_llm_repair_verify_enabled": "1"})

    # Offline (no live provider) -> never spends, returns None. This is what keeps the
    # whole offline/MockProvider posture byte-identical.
    tag_llm_repair.run_completion = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call a provider when offline")
    )
    tag_llm_repair.provider_is_live = lambda *_a, **_k: False
    try:
        assert tag_llm_repair.repair(SRC, TGT_DROPPED, spec=_spec()) is None
    finally:
        _unscript()

    # Oversized, already-clean, and tagless-source all short-circuit BEFORE any call.
    calls = _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        big = "x" * (tag_llm_repair.MAX_SEGMENT_CHARS + 1)
        assert tag_llm_repair.repair(SRC, big, spec=_spec()) is None
        assert tag_llm_repair.repair(SRC, TGT_FIXED, spec=_spec()) is None  # clean
        assert tag_llm_repair.repair("no tags here", "sin etiquetas", spec=_spec()) is None
        assert calls == [], calls
    finally:
        _unscript()
    print(
        "  [5] rung self-disables: kill switch, verify switch, offline, oversized, clean, tagless"
    )


# --- 6. commit_mt_output wiring ---------------------------------------------
def _commit(seg_target, *, repairer, source=SRC):
    with Session(engine) as s:
        seg = _mk_seg(s, source=source, target=None, status=SegStatus.NEW)
        out = quality.commit_mt_output(s, seg, seg_target, source="mt", llm_tag_repair=repairer)
        s.commit()
        s.refresh(seg)
        return out, seg


def test_commit_wiring():
    # (a) NO repairer injected -> byte-identical pre-1.5c quarantine.
    out, seg = _commit(TGT_DROPPED, repairer=None)
    assert out.tag_violation and not out.tm_eligible
    assert seg.status == SegStatus.NEEDS_REVIEW
    assert not out.tag_llm_repaired and out.tag_repair_cost_usd == 0.0

    # (b) VERIFIED repair -> committed CLEAN: no flag, TM-eligible, not in review.
    def _verified(src, tgt):
        return tag_llm_repair.RepairResult(
            text=TGT_FIXED,
            clean=True,
            verified=True,
            note="",
            cost_usd=0.002,
            tokens_in=10,
            tokens_out=20,
        )

    out, seg = _commit(TGT_DROPPED, repairer=_verified)
    assert seg.target_text == TGT_FIXED
    assert out.tag_violation == "" and out.tm_eligible
    assert out.tag_llm_repaired and out.tag_llm_verified
    assert out.tag_repair_cost_usd == 0.002 and out.tag_repair_tokens_in == 10
    assert seg.status != SegStatus.NEEDS_REVIEW
    assert not [f for f in (seg.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE]

    # (c) UNVERIFIED repair -> text improved BUT still flagged + review + TM-INELIGIBLE.
    def _unverified(src, tgt):
        return tag_llm_repair.RepairResult(
            text=TGT_FIXED, clean=False, verified=False, note="placement unconfirmed (x)"
        )

    out, seg = _commit(TGT_DROPPED, repairer=_unverified)
    assert seg.target_text == TGT_FIXED  # tags-back beats tags-missing
    assert out.tag_violation and not out.tm_eligible  # ...but honesty is preserved
    assert out.tag_llm_repaired and not out.tag_llm_verified
    assert seg.status == SegStatus.NEEDS_REVIEW
    assert [f for f in (seg.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE]

    # (d) A candidate the GATE rejects is DISCARDED -- the seam never trusts the rung.
    def _liar(src, tgt):
        return tag_llm_repair.RepairResult(
            text="totally unrelated ⟦r9⟧text⟦/r9⟧", clean=True, verified=True, note=""
        )

    out, seg = _commit(TGT_DROPPED, repairer=_liar)
    assert seg.target_text == TGT_DROPPED  # original kept, candidate binned
    assert out.tag_violation and not out.tm_eligible
    assert not out.tag_llm_repaired

    # (e) A RAISING repairer can never break the write path.
    def _boom(src, tgt):
        raise RuntimeError("provider exploded")

    out, seg = _commit(TGT_DROPPED, repairer=_boom)
    assert seg.target_text == TGT_DROPPED
    assert out.tag_violation and not out.tag_llm_repaired

    # (f) The DETERMINISTIC rung still wins first: a pure duplicate is de-duped clean and
    # the (paid) LLM rung is never consulted.
    seen = []

    def _spy(src, tgt):
        seen.append((src, tgt))
        return tag_llm_repair.RepairResult(text=tgt, clean=True, verified=True, note="")

    dup_src = "a ⟦x1⟧ b"
    out, seg = _commit("a ⟦x1⟧ b ⟦x1⟧", repairer=_spy, source=dup_src)
    assert out.tag_violation == "" and out.tag_repaired and not out.tag_llm_repaired
    assert seen == [], seen

    # (g) An intact target is byte-identical: no flag, no repairer call.
    seen.clear()
    out, seg = _commit(TGT_FIXED, repairer=_spy)
    assert out.tag_violation == "" and not out.tag_repaired and not out.tag_llm_repaired
    assert seen == [], seen
    print("  [6] commit wiring: none/verified/unverified/liar/raiser/deterministic-first/intact")


# --- 7. repair_stored_segments (the human-review half) ----------------------
def test_batch_repair():
    with Session(engine) as s:
        seg = _mk_seg(
            s, source=SRC, target=TGT_DROPPED, qa_flags=[quality.tag_flag("inline tag dropped")]
        )
        seg_id, doc_id = seg.id, seg.document_id

    # DRY RUN: calls happen (that is how we know a repair is possible) but NOTHING is
    # written -- safe to run against the real DB.
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=doc_id, apply=False)
        assert rep["scanned"] == 1 and rep["repaired"] == 1 and rep["verified"] == 1
        assert rep["applied"] is False
    finally:
        _unscript()
    with Session(engine) as s:
        assert s.get(Segment, seg_id).target_text == TGT_DROPPED  # untouched

    # APPLY + VERIFIED: text written, tag flag cleared, and (no other flags) back to MT.
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=doc_id, apply=True)
        assert rep["repaired"] == 1 and rep["verified"] == 1 and rep["applied"] is True
    finally:
        _unscript()
    with Session(engine) as s:
        got = s.get(Segment, seg_id)
        assert got.target_text == TGT_FIXED
        assert not [f for f in (got.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE]
        assert got.status == SegStatus.MT

    # APPLY + UNVERIFIED: text improved, but the segment STAYS in review with the flag.
    with Session(engine) as s:
        seg2 = _mk_seg(s, source=SRC, target=TGT_DROPPED)
        seg2_id, doc2_id = seg2.id, seg2.document_id
    _script({"text": TGT_FIXED}, _bad_verdict())
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=doc2_id, apply=True)
        assert rep["repaired"] == 1 and rep["verified"] == 0
    finally:
        _unscript()
    with Session(engine) as s:
        got = s.get(Segment, seg2_id)
        assert got.target_text == TGT_FIXED
        assert got.status == SegStatus.NEEDS_REVIEW
        assert [f for f in (got.qa_flags or []) if f.get("type") == quality.TAG_FLAG_TYPE]

    # A segment in review for ANOTHER reason keeps its review status even when the tag
    # repair verifies -- a tag fix has no business clearing a refusal flag.
    with Session(engine) as s:
        seg3 = _mk_seg(
            s,
            source=SRC,
            target=TGT_DROPPED,
            qa_flags=[
                quality.tag_flag("dropped"),
                {"type": "refusal", "severity": "major", "message": "hedge"},
            ],
        )
        seg3_id, doc3_id = seg3.id, seg3.document_id
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        with Session(engine) as s:
            tag_llm_repair.repair_stored_segments(s, doc_id=doc3_id, apply=True)
    finally:
        _unscript()
    with Session(engine) as s:
        got = s.get(Segment, seg3_id)
        assert got.status == SegStatus.NEEDS_REVIEW, got.status
        assert [f for f in (got.qa_flags or []) if f.get("type") == "refusal"]

    # --limit truncates AND says so (no silent cap).
    with Session(engine) as s:
        p = Project(name="tagllm-lim-%d" % _seq(), org_id=1, source_locale="en-US")  # noqa: UP031
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="m.docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        for i in range(3):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=SRC,
                    target_text=TGT_DROPPED,
                    status=SegStatus.NEEDS_REVIEW,
                )
            )
        s.commit()
        lim_doc = d.id
    _script(
        {"text": TGT_FIXED},
        _ok_verdict(),
        _ok_verdict(),
        {"text": TGT_FIXED},
        _ok_verdict(),
        _ok_verdict(),
    )
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=lim_doc, apply=False, limit=2)
        assert rep["repaired"] == 2 and rep["truncated"] is True, rep
    finally:
        _unscript()

    # --limit bounds ATTEMPTS, not successes: a sweep over segments the rung CANNOT fix
    # must still stop at the cap (otherwise an unfixable backlog spends without bound).
    calls = _script(*[{"text": "unrepairable"}] * 10)
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=lim_doc, apply=False, limit=2)
        assert rep["repaired"] == 0 and rep["attempted"] == 2, rep
        assert rep["truncated"] is True, rep
        assert len(calls) == 2, calls  # two repair calls, no verify calls, and no more
    finally:
        _unscript()
    # Issue #546: a rejected attempt still spent provider money; the sweep must report it.
    calls = _script(*[{"text": "unrepairable", "cost_usd": 0.01}] * 10)
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=lim_doc, apply=False, limit=2)
        assert rep["repaired"] == 0 and len(calls) == 2, rep
        assert abs(rep["cost_usd"] - 0.02) < 1e-9, ("rejected attempts must count", rep)
    finally:
        _unscript()
    # ...and an accepted repair is counted once, with its verification calls.
    _script(
        {"text": TGT_FIXED, "cost_usd": 0.01},
        {**_ok_verdict(), "cost_usd": 0.004},
        {**_ok_verdict(), "cost_usd": 0.004},
    )
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=lim_doc, apply=False, limit=1)
        assert rep["repaired"] == 1 and abs(rep["cost_usd"] - 0.018) < 1e-9, rep
    finally:
        _unscript()
    print("  [7] batch: dry-run writes nothing; apply+verified clears flag; unverified stays;")
    print("      other flags survive; --limit truncates and reports")


# --- 7b. char_limit re-judge on the repair write (fix-round F6) --------------
def _mk_key_value_seg(
    session, *, char_limit, source, target, tag_flag_msg="inline tag dropped", extra_flags=()
):
    """A KEY value segment (key_id set -> a real char_limit governs it) sitting in its
    project's internal document, damaged and in review, so repair_stored_segments will
    attempt it. This is the one segment shape where the F6 write path can lengthen a value
    across its character limit."""
    p = content.project_create(
        session,
        "tagllm-k-%d" % _seq(),  # noqa: UP031
        source_locale="en-US",
        target_locales="es-ES",
        default_provider="mock",
        project_type=ProjectType.KEY_BASED,
    )
    k = keys.create_key(session, p, "nav::k%d" % _seq(), char_limit=char_limit, base_value=source)  # noqa: UP031
    seg = keys.values_for(session, k, "es-ES")[0]
    seg.source_text = source
    seg.target_text = target
    seg.status = SegStatus.NEEDS_REVIEW
    seg.qa_flags = [quality.tag_flag(tag_flag_msg), *extra_flags]
    session.add(seg)
    session.commit()
    session.refresh(seg)
    return seg


def test_char_limit_rejudged_on_repair_write():
    """F6: repair_stored_segments' sanctioned target_text write can LENGTHEN a value by
    restoring tags. On a key value with a char_limit, a VERIFIED repair whose restored text
    is over the limit must be FLAGGED and STAY in review. Without the re-judge the
    char_limit family is bypassed AND the empty-qa_flags release fires -> the over-limit
    value silently returns to MT. (TGT_DROPPED 44 chars fits 45; TGT_FIXED 53 chars does
    not.)"""
    assert quality.char_count(TGT_DROPPED) == 44, "fixture: dropped target is 44 chars"
    assert quality.char_count(TGT_FIXED) == 53, "fixture: restored target is 53 chars"
    with Session(engine) as s:
        seg = _mk_key_value_seg(s, char_limit=45, source=SRC, target=TGT_DROPPED)
        seg_id, doc_id = seg.id, seg.document_id
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=doc_id, apply=True)
        assert rep["repaired"] == 1 and rep["verified"] == 1, rep
    finally:
        _unscript()
    with Session(engine) as s:
        got = s.get(Segment, seg_id)
        assert got.target_text == TGT_FIXED, "the restored (longer) text is written"
        cl = [f for f in (got.qa_flags or []) if f.get("type") == quality.CHAR_LIMIT_FLAG_TYPE]
        assert cl, (
            f"a verified repair that overflows the char_limit must be FLAGGED (F6): {got.qa_flags!r}"  # noqa: E501
        )
        assert cl[0].get("limit") == 45, "the char_limit flag records the limit"
        assert got.status == SegStatus.NEEDS_REVIEW, (
            "the over-limit value must NOT release to MT -- it stays in review"
        )
    print("  [7b-1] F6: over-limit verified repair is char_limit-flagged and kept in review")


def test_char_limit_stale_flag_cleared_on_repair_write():
    """F6 mirror: a STALE char_limit flag (from the pre-repair length) must be CLEARED when
    the restored text fits, or the lingering flag accidentally blocks the verified MT
    release. (TGT_FIXED 53 chars fits a limit of 60.)"""
    with Session(engine) as s:
        seg = _mk_key_value_seg(
            s,
            char_limit=60,
            source=SRC,
            target=TGT_DROPPED,
            extra_flags=[quality.char_limit_flag("99 characters, limit 60", 99, 60)],
        )
        seg_id, doc_id = seg.id, seg.document_id
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        with Session(engine) as s:
            tag_llm_repair.repair_stored_segments(s, doc_id=doc_id, apply=True)
    finally:
        _unscript()
    with Session(engine) as s:
        got = s.get(Segment, seg_id)
        assert not [
            f for f in (got.qa_flags or []) if f.get("type") == quality.CHAR_LIMIT_FLAG_TYPE
        ], f"a stale char_limit flag must be cleared when the repaired text fits: {got.qa_flags!r}"
        assert got.status == SegStatus.MT, (
            "with the tag AND stale char_limit flags gone, the verified repair releases to MT"
        )
    print("  [7b-2] F6: stale char_limit flag cleared on a fitting repair (release unblocked)")


# --- 8. CLI ------------------------------------------------------------------
def test_cli_registered():
    from app.cli.__main__ import build_parser

    p = build_parser()
    ns = p.parse_args(["tagging", "repair", "--doc", "1"])
    assert ns.tagging_cmd == "repair"
    assert ns.apply is False, "tagging repair must be DRY-RUN by default"
    assert ns.limit == 200
    ns2 = p.parse_args(["tagging", "repair", "--segment", "5", "--apply"])
    assert ns2.segment_id == 5 and ns2.apply is True
    print("  [8] CLI `tagging repair` registered, dry-run by default")


# --- 9. LIVE-QA REGRESSIONS (2026-07-25) ------------------------------------
def test_live_qa_regressions():
    """Three defects found by a LIVE run (llama-3-8b translate + claude LQA) over a
    multi-page, dozens-of-tag-shapes document. Each is pinned here with the real shape
    the model actually produced."""
    # (A) MASK-SENTINEL DEBRIS must not defeat body preservation. Live, the model
    # mangled `«TAG…_1»` into these forms; a repair that CLEANS the debris was being
    # discarded as a "content change", making residual-sentinel damage unrepairable.
    from app.services import ai_tagging

    clean = "La valeur ⟦r1⟧obligatoire⟦/r1⟧ doit etre revue."
    for debris in (
        "La valeur «TAG41d35d19_1» obligatoire doit etre revue.",
        "La valeur «TAG41d35d19_1⟩ obligatoire doit etre revue.",
        'La valeur "TAG41d35d19_1" obligatoire doit etre revue.',
        "La valeur «⟦/r1⟧TAG41d35d19_1⟦/r1⟧» obligatoire doit etre revue.",
    ):
        assert tag_llm_repair.body_preserved(debris, clean), debris
    # ...but a REAL word change is still caught with debris present on both sides.
    assert not tag_llm_repair.body_preserved(
        "La valeur «TAG41d35d19_1» obligatoire doit etre revue.",
        "La valeur ⟦r1⟧facultative⟦/r1⟧ doit etre revue.",
    )
    # The loose debris strip must NEVER weaken residual DETECTION.
    assert ai_tagging.has_residual_sentinel("x «TAGdeadbeef_1 y")

    # (B) A residual sentinel with an OTHERWISE-INTACT token set produced an EMPTY brief,
    # so repair() bailed before calling anything -- a whole damage class was unreachable.
    src = "Read ⟦g1⟧the report⟦/g1⟧ today."
    tgt = "Lisez ⟦g1⟧le rapport⟦/g1⟧ «TAGdeadbeef_3» aujourd'hui."
    assert quality.tag_damage(src, tgt), "fixture must be damaged"
    brief = tag_llm_repair.describe_damage(src, tgt)
    assert brief, "residual-sentinel damage must produce a brief"
    assert "DELETE" in brief and "TAG" in brief, brief
    calls = _script(
        {"text": "Lisez ⟦g1⟧le rapport⟦/g1⟧ aujourd'hui."}, _ok_verdict(), _ok_verdict()
    )
    try:
        res = tag_llm_repair.repair(src, tgt, spec=_spec())
        assert res is not None and res.clean, res  # now reachable AND repairable
        assert not ai_tagging.has_residual_sentinel(res.text)
        assert [c["task"] for c in calls][0] == "tag_repair", calls  # noqa: RUF015
    finally:
        _unscript()

    # (C) MASK-LAYER HOLE: auxiliary prompt material (the auto-injected LQA findings)
    # quoted RAW tokens beside a MASKED {text}. Live, 9 of 34 prompts showed the model
    # both notations and it began emitting raw tokens into masked output. mask_like
    # rewrites them into the SAME sentinels; an unknown token degrades to a neutral
    # phrase, never a copyable literal.
    _masked, mapping = ai_tagging.mask("Read ⟦g1⟧the report⟦/g1⟧ today.")
    sent = list(mapping)
    findings = "1. [Design/major] Inline placeholder dropped ⟦g1⟧, ⟦/g1⟧ and ⟦x7⟧"
    out = ai_tagging.mask_like(findings, mapping)
    assert "⟦" not in out, out  # no raw token survives
    assert sent[0] in out and sent[1] in out, out  # mapped to THIS call's sentinels
    assert "(a formatting tag)" in out, out  # ⟦x7⟧ isn't in this segment
    # A segment with NO tokens of its own still gets an EMPTY mapping -- but the findings
    # may quote tokens from elsewhere in the document. Live QA caught 9 such prompts
    # still handing the model raw literals to imitate, so an empty mapping must still
    # NEUTRALIZE (masking-off byte-identity is preserved at the CALL SITE, which only
    # invokes this when ai_tagging.enabled()).
    assert "⟦" not in ai_tagging.mask_like(findings, {}), "tagless segment must neutralize"
    assert ai_tagging.mask_like("no tokens here", mapping) == "no tokens here"
    # (D) THE SYSTEM PROMPT ITSELF taught the ⟦⟧ notation on every call. A MASKED call
    # was told "the text may contain markers of the form ⟦r1⟧...⟦/r1⟧" while the text it
    # received had none -- so the model INVENTED them (13 of 95 segments in one live run
    # came back with ⟦r1⟧/⟦/r1⟧ that the HTML filter cannot emit at all). The clause must
    # describe ONLY the notation actually present.
    from app.services import orchestrator

    masked_sys = orchestrator._system_for({"«TAGdeadbeef_1»": "⟦g1⟧"})
    raw_sys = orchestrator._system_for({})
    assert "⟦" not in masked_sys, masked_sys  # no literal to imitate when masked
    assert "TAG" in masked_sys, masked_sys
    assert "⟦r1⟧" in raw_sys, raw_sys  # mask-off path keeps the real grammar
    assert "TAG" not in raw_sys, raw_sys
    for sys_msg in (masked_sys, raw_sys):
        assert "never invent" in sys_msg, sys_msg
    # (E) THE SHIPPED DEFAULT IS DARK. Two independent reviews returned FIX-FIRST on the
    # rung (3 CRITICAL). It must ship OFF until those close, and the SEAM itself -- not
    # just the two surfaces -- must honour the switch, so no future caller can bypass it.
    assert settings.DEFAULTS["tag_llm_repair_enabled"] == "0", (
        "the rung must ship DARK until the 2026-07-25 review findings are closed"
    )
    settings.set_many({"tag_llm_repair_enabled": "0"})
    try:
        with Session(engine) as s:
            seg = _mk_seg(s, source=SRC, target=TGT_DROPPED)
            doc_id = seg.document_id
        calls = _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
        try:
            with Session(engine) as s:
                rep = tag_llm_repair.repair_stored_segments(s, doc_id=doc_id, apply=True)
            assert rep.get("disabled") is True and rep["segments"] == [], rep
            assert calls == [], "the seam must not call a provider when disabled"
        finally:
            _unscript()
        with Session(engine) as s:
            got = s.exec(select(Segment).where(Segment.document_id == doc_id)).first()
            assert got.target_text == TGT_DROPPED, "disabled seam must not write"
    finally:
        settings.set_many({"tag_llm_repair_enabled": "1"})
    print("  [9] live-QA regressions: sentinel debris, empty-brief blind spot,")
    print("      findings mask-hole (mask_like), notation-specific system prompt,")
    print("      rung ships DARK + seam honours the kill switch")


def test_workflow_run_counts_repair_spend():
    """#596: a workflow RUN must count every billed tag-repair call. The rung runs inside
    the write-back, after the StepRuns were persisted, so the run cost (a sum of
    StepRun.cost_usd, which budgets and reports read) missed the repair calls: all of them
    for a rejected candidate, and the accepted repair's too. Each case below runs a real
    workflow whose translate step drops the tags and whose repair/verify turns are the
    scripted stub, and compares the run's StepRun spend to the stubbed per-call costs."""
    import json
    import re
    from unittest.mock import patch

    from app.models import (
        RunStatus,
        StepRun,
        StepType,
        TargetLocale,
        TransMode,
        Workflow,
        WorkflowRun,
        WorkflowStep,
    )
    from app.services import ingest, orchestrator
    from app.services.providers import register_provider
    from app.services.providers_common import Provider

    class _Dropper(Provider):
        name = "repairspend"

        def complete(self, prompt, system=None, task=None, payload=None, **kwargs):
            # strips the masked tag sentinels, i.e. the damage the rung exists to repair
            return re.sub(r"«TAG[^»]+»", "", (payload or {}).get("text", ""))

    register_provider("repairspend", _Dropper())
    REPAIR_COST, VERIFY_COST = 0.0125, 0.004

    def run_case(mode, accept, interfere=None):
        calls = []

        def _fake(spec, prompt, system=None, task=None, payload=None, tap_meta=None, **_kw):
            calls.append(task)
            usage = {"input_tokens": 100, "output_tokens": 20}
            if task == "tag_repair":
                src = (payload or {}).get("source") or ""
                # accepted: the source back (tokens restored, body unchanged); rejected: a
                # retranslation that fails lock 2 (body preservation).
                text = src if accept else "Completely different wording here."
                return {"text": text, "error": None, "usage": usage, "cost_usd": REPAIR_COST}
            return {
                "text": json.dumps({"correct": True}),
                "error": None,
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "cost_usd": VERIFY_COST,
            }

        with Session(engine) as session:
            project = Project(name="repair-spend-%d" % _seq(), source_locale="en")  # noqa: UP031
            workflow = Workflow(name="repair-spend-%d" % _seq(), scope="global")  # noqa: UP031
            session.add_all([project, workflow])
            session.commit()
            session.add(TargetLocale(project_id=project.id, locale="fr"))
            session.commit()
            doc = ingest.ingest_document(
                session,
                project,
                "rs.html",
                b"<p>Contact <b>the team</b> today please.</p>",
                mode,
            )
            session.add(
                WorkflowStep(
                    workflow_id=workflow.id,
                    order_index=0,
                    step_type=StepType.LLM_AGENT,
                    provider="repairspend",
                    model="mock",
                    prompt_text="{text}",
                    chunk_policy="none",
                )
            )
            session.commit()
            real_terminal = orchestrator._commit_run_terminal

            def _terminal(sess, run, final_status):
                if interfere == "crash":
                    raise RuntimeError("boom after the repairs were paid")
                if interfere == "cancel":
                    # the staged write-back holds the writer lock; the cancel the CAS loses to
                    # is one that committed while this thread was finishing, so release it first
                    sess.rollback()
                    with Session(engine) as other:
                        other_run = other.get(WorkflowRun, run.id)
                        other_run.status = RunStatus.CANCELLED
                        other.add(other_run)
                        other.commit()
                return real_terminal(sess, run, final_status)

            events = []
            with (
                patch.object(tag_llm_repair, "run_completion", _fake),
                patch.object(tag_llm_repair, "provider_is_live", lambda *_a, **_k: True),
                patch.object(orchestrator, "_commit_run_terminal", _terminal),
            ):
                try:
                    for ev in orchestrator.run_workflow_stream(session, workflow.id, doc.id, "fr"):
                        events.append(ev)
                except RuntimeError:
                    assert interfere == "crash", "only the crash case may raise"
            if interfere is None:
                assert not [e for e in events if e.get("type") == "error"], events
            run_id = next(e["run_id"] for e in events if e.get("run_id"))
            session.expire_all()
            rows = session.exec(select(StepRun).where(StepRun.workflow_run_id == run_id)).all()
        n_repair = calls.count("tag_repair")
        n_verify = calls.count("tag_verify")
        assert n_repair >= 1, (mode, calls, events)
        cost = sum(r.cost_usd or 0.0 for r in rows)
        tin = sum(r.tokens_in or 0 for r in rows)
        tout = sum(r.tokens_out or 0 for r in rows)
        want = n_repair * REPAIR_COST + n_verify * VERIFY_COST
        assert abs(cost - want) < 1e-9, (mode, accept, cost, want, calls)
        # the translate step's own tokens are in the sum too, so the repair calls are a floor
        assert tin >= n_repair * 100 + n_verify * 10, (mode, accept, tin, calls)
        assert tout >= n_repair * 20 + n_verify * 2, (mode, accept, tout, calls)
        return n_repair, n_verify

    settings.set_many({"tag_llm_repair_enabled": "1"})
    for mode in (TransMode.SEGMENTED, TransMode.FULL_TEXT):
        _n_rep, n_ver = run_case(mode, accept=False)
        assert n_ver == 0, "a rejected candidate never reaches the verifier"
        _n_rep, n_ver = run_case(mode, accept=True)
        assert n_ver >= 1, "an accepted candidate is verified (counted once, not twice)"
    # A run that FAILS (crash before the terminal commit) or is CANCELLED while finishing, after
    # it already paid for repairs, must still count them (FT rolls its write-back back on a lost
    # CAS; the wrapper's finalizer stamps the unsettled spend), and exactly once.
    for mode in (TransMode.SEGMENTED, TransMode.FULL_TEXT):
        for how in ("crash", "cancel"):
            run_case(mode, accept=False, interfere=how)
            run_case(mode, accept=True, interfere=how)
    print("  [10] workflow run counts rejected AND accepted tag-repair spend exactly once")


def test_tm_lock_interplay():
    """PROPOSAL-tm-matching (B2 + HIGH-4): the on-demand repair sweep is a sanctioned
    OUTSIDE-the-seam writer, so it must honour TM locks.
      - a HARD-locked segment is SKIPPED and reported (never rewritten);
      - a SOFT-locked segment IS repaired (D3) but its now-superseded soft lock is CLEARED
        so it never stands over text the rung just rewrote."""
    from app.services import tm_lock

    # HARD lock -> skipped
    with Session(engine) as s:
        seg = _mk_seg(s, source=SRC, target=TGT_DROPPED, qa_flags=[quality.tag_flag("dropped")])
        tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        hard_id, hdoc = seg.id, seg.document_id
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=hdoc, apply=True)
        assert rep["repaired"] == 0, ("a hard-locked segment must not be repaired", rep)
        assert any("hard-locked" in (r.get("note") or "") for r in rep["segments"]), rep
    finally:
        _unscript()
    with Session(engine) as s:
        got = s.get(Segment, hard_id)
        assert got.target_text == TGT_DROPPED and got.lock_hard is True, got

    # SOFT lock -> repaired + lock cleared
    with Session(engine) as s:
        seg2 = _mk_seg(s, source=SRC, target=TGT_DROPPED, qa_flags=[quality.tag_flag("dropped")])
        tm_lock._stamp_lock(seg2, band="100", hard=False, tm_id=2, entry_id=2)
        s.add(seg2)
        s.commit()
        soft_id, sdoc = seg2.id, seg2.document_id
    _script({"text": TGT_FIXED}, _ok_verdict(), _ok_verdict())
    try:
        with Session(engine) as s:
            rep = tag_llm_repair.repair_stored_segments(s, doc_id=sdoc, apply=True)
        assert rep["repaired"] == 1, ("a soft-locked segment IS repaired (D3)", rep)
    finally:
        _unscript()
    with Session(engine) as s:
        got = s.get(Segment, soft_id)
        assert got.target_text == TGT_FIXED, got.target_text
        assert not got.lock_band and not (got.meta or {}).get("tm_lock"), (
            "the superseded soft lock must be cleared",
            got.lock_band,
        )
    print("  tm-lock interplay: hard lock skipped+reported; soft lock repaired+cleared: OK")


if __name__ == "__main__":
    print("AI Tagging 1.5c - LLM tag-repair rung")
    test_describe_damage()
    test_body_preservation_lock()
    test_structural_lock_binds()
    test_placement_verification()
    test_rung_self_disables()
    test_commit_wiring()
    test_batch_repair()
    test_char_limit_rejudged_on_repair_write()
    test_char_limit_stale_flag_cleared_on_repair_write()
    test_tm_lock_interplay()
    test_cli_registered()
    test_live_qa_regressions()
    test_workflow_run_counts_repair_spend()
    print("TAG LLM REPAIR OK")
