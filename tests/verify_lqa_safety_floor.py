"""LQA safety floor — Phase 1 of PROPOSAL-lqa-glossary-adjudication.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network,
every provider call replaced by a deterministic in-test fake). Proves the two
deterministic guards the proposal calls "the safety floor":

  (C) FIXER POST-CHECK WITH REVERT — a step edit that INTRODUCES a glossary/DNT
      violation absent from the text it was editing is DISCARDED (the pre-fix text is
      kept byte-for-byte), flagged `fix_reverted`, and routed to NEEDS_REVIEW. A DELTA,
      never an absolute: a pre-existing violation the edit leaves alone is not reverted,
      and an edit that FIXES a violation is kept. FULL_TEXT flags but never reverts.
  (D1) TM PROVENANCE GUARD — segments filled by a 100% TM match are excluded from a
      CORRECTION-LOOP step's pending set, so a fix step can no longer rewrite an
      approved byte-exact match and have the run report it as fresh MT. Tristate
      `WorkflowStep.tm_protect` overrides in both directions.

Groups:
  1. lqa_safety unit: introduced_violations delta semantics (glossary + DNT, both
     directions), tm_protect_for tristate, merge_flags family preservation, kill
     switches.
  2. (C) segmented: introduced glossary miss -> reverted; introduced DNT breach ->
     reverted; a fix that REPAIRS a violation -> accepted; a pre-existing violation the
     fixer ignores -> NOT reverted; the FIRST translating step is never reverted back to
     the source; kill switch off -> pre-Phase-1 behaviour.
  3. (D1) segmented: a real prev_lqa:failed correction loop over a 100% TM doc leaves
     the TM segment byte-identical AND tm_match_pct == 100 while the non-TM segment IS
     fixed; tm_protect=False unprotects; tm_protect=True protects a non-loop step; the
     default leaves an ordinary polish step's semantics unchanged; kill switch off ->
     pre-Phase-1 behaviour.
  4. FULL_TEXT: a term-breaking second step is NOT reverted but IS attributed
     (`fix_term_regression` + NEEDS_REVIEW); a single translating step never flags.
  5. Regression: a workflow with no LQA and no glossary is behaviourally unchanged —
     no `lqa_safety` key on any event, no new qa_flags, targets = the last step's text.
  6. Parity: the tristate round-trips through the workflows service, `workflow show`
     (--json + the human opts column), the /api workflow payload, and the web builder
     template; it is refused as a dead field on lqa/human_review; the new qa_flag
     families are surfaced by the editor grid AND the headless review summary.
  7. Mutation probe (fail-before proof): a file-copy mutation of lqa_safety.py that
     neuters both guards makes the group-2 and group-3 assertions FAIL; the original
     bytes are restored and byte-compared.

Final line: "LQA SAFETY FLOOR OK".
"""

import glob
import importlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_lqasafety_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    GlossaryEntry,
    Project,
    Segment,
    SegmentTM,
    SegStatus,
    StepType,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import lqa, lqa_safety, orchestrator, settings, tm, workflows

init_db()

SRC, TGT = "en-US", "es-ES"
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


# --- builders ---------------------------------------------------------------
def _gloss(source_term, target_term, *, dnt=False, case_sensitive=False):
    """One match_terms-shaped constraint dict (the shape the guard consumes)."""
    return {
        "source_term": source_term,
        "target_term": target_term,
        "dnt": dnt,
        "sense_label": None,
        "case_sensitive": case_sensitive,
    }


def _mk_project(session):
    p = Project(name="lqasafe-%d" % _seq(), org_id=1, source_locale=SRC, default_provider="claude")  # noqa: UP031
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_doc(session, project, *, sources, mode=TransMode.SEGMENTED):
    # A FULL_TEXT run reads its source from the skeleton units (orchestrator._source_text),
    # not from the Segment rows; a SEGMENTED run reads the Segment rows. Populate both so
    # one builder serves either mode.
    doc = Document(
        project_id=project.id,
        filename="d.txt",
        fmt="txt",
        mode=mode,
        skeleton={"units": [{"text": t} for t in sources]},
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    for i, text in enumerate(sources):
        session.add(Segment(document_id=doc.id, target_locale=TGT, order_index=i, source_text=text))
    session.commit()
    return doc


def _add_term(session, project_id, source_term, target_term, *, dnt=False):
    session.add(
        GlossaryEntry(
            project_id=project_id,
            source_term=source_term,
            target_term=target_term,
            source_locale=SRC,
            target_locale=TGT,
            dnt=dnt,
        )
    )
    session.commit()


def _seed_tm(session, project_id, source, target):
    row = SegmentTM(
        project_id=project_id,
        source_locale=SRC,
        target_locale=TGT,
        source_text=source,
        target_text=target,
        source_hash=tm.hash_text(source, SRC, TGT),
    )
    session.add(row)
    session.commit()


def _step(
    session,
    wf_id,
    order,
    *,
    marker,
    name=None,
    step_type=StepType.LLM_AGENT,
    run_condition=None,
    tm_protect=None,
    key=None,
):
    st = WorkflowStep(
        workflow_id=wf_id,
        order_index=order,
        name=name or ("s%d" % order),  # noqa: UP031
        step_type=step_type,
        provider="claude",
        prompt_text=(f"{marker} {{text}}") if step_type == StepType.LLM_AGENT else "",
        chunk_policy="none",
        retry_max=1,
        backoff_base=0.0,
        run_condition=run_condition,
        tm_protect=tm_protect,
        key=key,
    )
    session.add(st)
    session.commit()
    return st


def _mk_wf(session, name="wf"):
    wf = Workflow(
        name="%s-%d" % (name, _seq()),  # noqa: UP031
        scope="global",
        source_locale=SRC,
        target_locale=TGT,  # noqa: RUF100, UP031
    )
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return wf


def _segs(session, doc_id):
    return sorted(
        session.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == TGT)
        ).all(),
        key=lambda s: s.order_index,
    )


def _flags(seg, kind):
    return [f for f in (seg.qa_flags or []) if f.get("type") == kind]


class _FakeCalls:
    """Replace orchestrator.run_completion with a marker-dispatching fake.

    Each step's prompt carries a unique MARKER (its prompt_text prefix), and the
    segment's current working text is substituted into `{text}` — so a rule can key on
    both "which step is calling" and "which segment is it working on"."""

    def __init__(self, rules):
        # rules: list of (marker, source_or_working_substring_or_None, output_text)
        self.rules = rules
        self._orig = None
        self.prompts = []

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            self.prompts.append(prompt)
            for marker, needle, out in self.rules:
                if marker in prompt and (needle is None or needle in prompt):
                    return {
                        "text": out,
                        "provider_used": "mock",
                        "model_used": "mock",
                        "attempts": 1,
                        "error": None,
                        "usage": {},
                    }
            raise AssertionError(f"no fake rule matched prompt: {prompt[:200]!r}")

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


class _FailingLQA:
    """Force the LQA step to FAIL so a `prev_lqa:failed` fix step actually runs (the
    correction-loop shape D1 protects). Patches the module attribute the orchestrator
    resolves at call time."""

    def __init__(self, passed=False):
        self.passed = passed
        self._orig = None

    def __enter__(self):
        self._orig = lqa.score_text

        def fake(*a, **kw):
            return {
                "score": 95.0 if self.passed else 40.0,
                "passed": self.passed,
                "errors": [{"dimension": "Terminology", "severity": "major", "note": "term issue"}],
                "gate": "auto_approved" if self.passed else "needs_review",
                "cost_usd": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
            }

        lqa.score_text = fake
        return self

    def __exit__(self, *exc):
        lqa.score_text = self._orig
        return False


def _run(wf_id, doc_id):
    """Run a workflow to completion and return (events, summary-ish dict)."""
    events = []
    with Session(engine) as s:
        for ev in orchestrator.run_workflow_stream(s, wf_id, doc_id, TGT):
            events.append(ev)
    return events


def _done(events):
    for ev in events:
        if ev["type"] == "done":
            return ev
    return None


# ===========================================================================
# 1. lqa_safety unit
# ===========================================================================
def test_unit():
    T = [_gloss("Widget", "Aparato")]
    src = "Buy the Widget now."
    good = "Compra el Aparato ya."
    broken = "Compra el chisme ya."

    # INTRODUCED: pre had the term, post dropped it -> reported.
    dnt, gl = lqa_safety.introduced_violations(src, good, broken, T)
    assert dnt == [] and gl == [{"source_term": "Widget", "target_term": "Aparato"}], (dnt, gl)
    # FIXED: pre lacked it, post has it -> nothing (an improvement is never reverted).
    assert lqa_safety.introduced_violations(src, broken, good, T) == ([], [])
    # PRE-EXISTING: both lack it -> nothing (the write-back gate owns that case).
    assert lqa_safety.introduced_violations(src, broken, "Compra ya.", T) == ([], [])
    # Clean -> clean.
    assert lqa_safety.introduced_violations(src, good, "Compra el Aparato hoy.", T) == ([], [])
    print("  1a. introduced_violations: glossary delta (introduced/fixed/pre-existing) OK")

    # R7: the DELTA is locale-aware, and that changes the verdict. Required 'descarga'
    # (source 'download'); the PRE text 'Descargue el instalador.' carries the conjugation
    # 'Descargue', the POST text 'Instale el instalador.' drops it.
    #   * NO target_locale ('*' family: plural/append tolerance only, no conjugation stem):
    #     'Descargue' does NOT count as 'descarga' on the PRE leg, so the term is already a
    #     violation before the edit -> not INTRODUCED -> MASKED (empty). This is the
    #     append-only comparison the F2 note warns used to DISCARD good conjugations.
    #   * es-ES (the stem rung shares 'descarg'=7): 'Descargue' DOES satisfy 'descarga' on
    #     the PRE leg, so PRE is clean and the edit genuinely INTRODUCES the miss -> REPORTED.
    # The correct direction: the locale-aware delta reports a real break the generic family
    # hides. (Its sibling -- the pre-leg no longer masking a good conjugation -- is 1m-* in
    # verify_glossary_adherence.)
    DL = [_gloss("download", "descarga")]
    dl_src, dl_pre, dl_post = (
        "Download the installer.",
        "Descargue el instalador.",
        "Instale el instalador.",
    )
    assert lqa_safety.introduced_violations(dl_src, dl_pre, dl_post, DL, target_locale=None) == (
        [],
        [],
    ), "R7: no-locale masks the introduced glossary miss (append-only pre leg)"
    _dnt, _gl = lqa_safety.introduced_violations(dl_src, dl_pre, dl_post, DL, target_locale="es-ES")
    assert _dnt == [] and _gl == [{"source_term": "download", "target_term": "descarga"}], (
        "R7: es-ES reports the introduced miss (pre-leg conjugation recognised)",
        _dnt,
        _gl,
    )
    print(
        "  1a-R7: introduced_violations delta is locale-aware (no-locale masks, es-ES reports) OK"
    )

    D = [_gloss("BrandX", "BrandX", dnt=True)]
    dsrc = "Keep BrandX visible."
    dnt, gl = lqa_safety.introduced_violations(
        dsrc, "Mantener BrandX visible.", "Mantener MarcaX visible.", D
    )
    assert dnt == ["BrandX"] and gl == [], (dnt, gl)
    # DNT already gone before the edit -> not introduced.
    assert lqa_safety.introduced_violations(dsrc, "Mantener MarcaX.", "Mantener MarcaY.", D) == (
        [],
        [],
    )
    print("  1b. introduced_violations: DNT delta OK")

    # Byte-identical when the assets are absent.
    assert lqa_safety.introduced_violations(src, good, broken, []) == ([], [])
    assert lqa_safety.introduced_violations(src, good, broken, None) == ([], [])
    assert lqa_safety.introduced_violations("", good, broken, T) == ([], [])
    assert lqa_safety.introduced_violations(src, good, "", T) == ([], [])
    print("  1c. no glossary / empty text -> no-op OK")

    class _S:
        tm_protect = None

    s = _S()
    assert lqa_safety.tm_protect_for(s, True) is True
    assert lqa_safety.tm_protect_for(s, False) is False
    s.tm_protect = True
    assert lqa_safety.tm_protect_for(s, False) is True
    s.tm_protect = False
    assert lqa_safety.tm_protect_for(s, True) is False
    # A step object with no attribute at all (legacy row / duck type) -> the default.
    assert lqa_safety.tm_protect_for(object(), True) is True
    assert lqa_safety.tm_protect_label(None) == "auto"
    assert lqa_safety.tm_protect_label(True) == "on"
    assert lqa_safety.tm_protect_label(False) == "off"
    print("  1d. tm_protect_for tristate + labels OK")

    existing = [
        {"type": "qe", "score": 80},
        {"type": "dnt_violation", "term": "X"},
        {"type": lqa_safety.FIX_REVERTED_FLAG_TYPE, "message": "stale"},
    ]
    flag = lqa_safety.revert_flag(
        "Fixer", ["BrandX"], [{"source_term": "Widget", "target_term": "Aparato"}]
    )
    out = lqa_safety.merge_flags(existing, [flag])
    assert [f["type"] for f in out] == ["qe", "dnt_violation", lqa_safety.FIX_REVERTED_FLAG_TYPE], (
        out
    )
    assert out[-1]["severity"] == "major" and "BrandX" in out[-1]["message"]
    assert "Widget -> Aparato" in out[-1]["message"], out[-1]["message"]
    assert out is not existing
    # Clearing: merging an empty list drops the stale fix-guard entry, keeps the rest.
    assert [f["type"] for f in lqa_safety.merge_flags(existing, [])] == ["qe", "dnt_violation"]
    reg = lqa_safety.regression_flag(
        "Polisher", [], [{"source_term": "Widget", "target_term": "Aparato"}]
    )
    assert reg["type"] == lqa_safety.FIX_REGRESSION_FLAG_TYPE
    assert "not reverted" in reg["message"]
    # ASCII-safe (Windows console / CLI parity).
    for f in (out[-1], reg):
        f["message"].encode("ascii")
    print("  1e. merge_flags preserves other families; flags ASCII-safe OK")

    # --- FALSE-REVERT SUPPRESSOR (adversarial review 2026-07-28) -------------
    # quality.glossary_term_violations matches the SURFACE target_term and has no
    # lemmatizer; its docstring accepts that limitation because the consequence is "a
    # SOFT review flag ... never a block and NEVER A REWRITE". (C) is the first consumer
    # for which that is false, so a DETECTED delta must additionally prove the term
    # really DISAPPEARED before text is discarded. Each of these reverted a GOOD edit
    # before the suppressor (reproduced against the shipped matcher).
    P = [_gloss("widget", "aparato")]
    assert lqa_safety.introduced_violations(
        "Buy the widget now.", "Compra el aparato ahora.", "Compra los aparatos ahora.", P
    ) == ([], []), "ES/FR plural must not revert"
    D2 = [_gloss("database", "Datenbank")]
    assert lqa_safety.introduced_violations(
        "Open the database.", "Oeffne die Datenbank.", "Oeffne die Datenbankverbindung.", D2
    ) == ([], []), "DE compound must not revert"
    A2 = [_gloss("today's report", "le rapport d'aujourd'hui")]
    assert lqa_safety.introduced_violations(
        "Read today's report.",
        "Lisez le rapport d'aujourd'hui.",
        "Lisez le rapport d’aujourd’hui.",
        A2,
    ) == ([], []), "typographic-apostrophe normalisation must not revert"
    N2 = [_gloss("euro price", "prix 10 EUR")]
    assert lqa_safety.introduced_violations(
        "The euro price applies.", "Le prix 10 EUR s'applique.", "Le prix 10 EUR s'applique.", N2
    ) == ([], []), "NBSP/U+202F insertion must not revert"
    # ...and the suppressor is LEFT-ANCHORED, so it never hides a real removal:
    assert lqa_safety.introduced_violations(
        "Buy the widget now.", "Compra el aparato ahora.", "Compra el chisme ahora.", P
    )[1] == [{"source_term": "widget", "target_term": "aparato"}], (
        "a genuine term removal must STILL revert"
    )
    S2 = [_gloss("AI", "IA")]
    assert (
        lqa_safety.introduced_violations("Use AI today.", "Usa IA hoy.", "Usa correo hoy.", S2)[1]
        != []
    ), "a short term must not be 'survived' by an unrelated word"
    # DNT is deliberately NOT relaxed: verbatim survival is its definition.
    assert lqa_safety.introduced_violations(
        "Use BrandX today.",
        "Usa BrandX hoy.",
        "Usa BrandXs hoy.",
        [_gloss("BrandX", "BrandX", dnt=True)],
    )[0] == ["BrandX"], "DNT stays strict"
    print(
        "  1g. false-revert suppressor: inflection/compound/typography spared, "
        "genuine removal + DNT still reverted OK"
    )

    # --- F2: the LOCALE must be threaded so a good es CONJUGATION edit is not reverted --
    # The campaign fixture: pre-fix text carries the EXACT required surface "descarga";
    # the post-editor replaces it with the correct imperative conjugation "Descargue".
    # WITHOUT the locale the matcher falls back to the generic append-only family, cannot
    # recognise the conjugation, sees a freshly-introduced miss (the left-anchored
    # _survives_softly backstop does NOT cover "descarga"->"descargue"), and the guard
    # DISCARDS the good edit. WITH target_locale threaded, the es stem rung recognises the
    # shared lemma and the delta is empty -> the edit stands.
    CJ = [_gloss("download", "descarga")]
    _src = "Download the installer."
    _pre = "Descarga el instalador."  # exact required surface, clean
    _post = "Descargue el instalador."  # valid es conjugation, still correct
    assert lqa_safety.introduced_violations(_src, _pre, _post, CJ)[1] == [
        {"source_term": "download", "target_term": "descarga"}
    ], "pre-fix: without the locale the generic family reverts the good conjugation"
    assert lqa_safety.introduced_violations(_src, _pre, _post, CJ, target_locale="es-ES") == (
        [],
        [],
    ), "F2: with the locale threaded, a valid es conjugation is NOT reverted"
    # ...and a GENUINE break (a synonym sharing no stem) is still reported WITH the locale,
    # so threading the locale relaxes only real inflections, never a real removal.
    assert lqa_safety.introduced_violations(
        _src, _pre, "Instale el instalador.", CJ, target_locale="es-ES"
    )[1] == [{"source_term": "download", "target_term": "descarga"}], (
        "F2: a genuine term removal still reverts with the locale threaded"
    )
    # applied_terms + adjudicate accept the same kwarg (parity with the matcher).
    assert lqa_safety.applied_terms(_src, _post, CJ, target_locale="es-ES") == CJ, (
        "F2: applied_terms sees the conjugation as applied when the locale is threaded"
    )
    print("  1h. F2: target_locale threaded -> a good es conjugation edit is not reverted OK")

    assert lqa_safety.fix_guard_enabled() and lqa_safety.tm_protection_enabled()
    settings.set_many({"glossary_fix_guard_enabled": "0", "tm_fix_protection_enabled": "0"})
    assert not lqa_safety.fix_guard_enabled() and not lqa_safety.tm_protection_enabled()
    settings.set_many({"glossary_fix_guard_enabled": "1", "tm_fix_protection_enabled": "1"})
    print("  1f. kill switches read settings OK")


# ===========================================================================
# 2. (C) fixer post-check with revert — segmented
# ===========================================================================
def _two_step_doc(sources, terms):
    """A plain Translate -> Fix workflow (no LQA) over `sources`, with `terms` as
    (source_term, target_term, dnt) project glossary rows."""
    with Session(engine) as s:
        p = _mk_project(s)
        for st, tt, dnt in terms:
            _add_term(s, p.id, st, tt, dnt=dnt)
        doc = _mk_doc(s, p, sources=sources)
        wf = _mk_wf(s)
        _step(s, wf.id, 0, marker="STEP0:", name="Translator")
        _step(s, wf.id, 1, marker="FIX:", name="Fixer")
        return wf.id, doc.id


def test_c_reverts_introduced_glossary_miss():
    wf_id, doc_id = _two_step_doc(("Buy the Widget now.",), [("Widget", "Aparato", False)])
    with _FakeCalls(
        [("STEP0:", None, "Compra el Aparato ya."), ("FIX:", None, "Compra el chisme ya.")]
    ):
        events = _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert seg.target_text == "Compra el Aparato ya.", ("pre-fix text kept", seg.target_text)
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        fr = _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE)
        assert len(fr) == 1 and "Fixer" in fr[0]["message"], seg.qa_flags
        assert fr[0]["glossary_terms"] == [{"source_term": "Widget", "target_term": "Aparato"}], fr
    done = _done(events)
    # `degraded` (GitHub #45) rides this same SEGMENTED rollup; a revert is not a
    # degradation, so a run that only reverted reports zero of them.
    assert done["lqa_safety"] == {"reverted": 1, "tm_protected": 0, "degraded": 0}, done.get(
        "lqa_safety"
    )
    fix_ev = [e for e in events if e["type"] == "step" and e["step_name"] == "Fixer"][0]  # noqa: RUF015
    assert fix_ev["lqa_safety"]["reverted"] == 1, fix_ev
    assert "reverted" in (fix_ev["error"] or ""), fix_ev["error"]
    # The reverted text never reaches the persisted step blob either.
    assert "chisme" not in done["text"], done["text"]
    print("  2a. fixer introduced a glossary miss -> REVERTED + flagged + NEEDS_REVIEW OK")


def test_c_reverts_introduced_dnt_breach():
    wf_id, doc_id = _two_step_doc(("Keep BrandX visible.",), [("BrandX", "BrandX", True)])
    with _FakeCalls(
        [("STEP0:", None, "Mantener BrandX visible."), ("FIX:", None, "Mantener MarcaX visible.")]
    ):
        _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert seg.target_text == "Mantener BrandX visible.", seg.target_text
        assert seg.status == SegStatus.NEEDS_REVIEW
        fr = _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE)
        assert len(fr) == 1 and fr[0]["dnt_terms"] == ["BrandX"], seg.qa_flags
    print("  2b. fixer introduced a DNT breach -> REVERTED OK")


def test_c_accepts_a_real_fix():
    wf_id, doc_id = _two_step_doc(("Buy the Widget now.",), [("Widget", "Aparato", False)])
    with _FakeCalls(
        [("STEP0:", None, "Compra el chisme ya."), ("FIX:", None, "Compra el Aparato ya.")]
    ):
        events = _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert seg.target_text == "Compra el Aparato ya.", seg.target_text
        assert _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE) == []
        assert _flags(seg, "glossary_violation") == []
        assert seg.status == SegStatus.MT, seg.status
    assert "lqa_safety" not in (_done(events) or {}), "clean run must be byte-identical"
    print("  2c. a fix that REPAIRS the violation is accepted (no flag) OK")


def test_c_leaves_pre_existing_violation_alone():
    wf_id, doc_id = _two_step_doc(("Buy the Widget now.",), [("Widget", "Aparato", False)])
    with _FakeCalls(
        [("STEP0:", None, "Compra el chisme ya."), ("FIX:", None, "Compra el chisme hoy.")]
    ):
        _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        # The edit was NOT reverted (it introduced nothing) -> the fixer's text stands.
        assert seg.target_text == "Compra el chisme hoy.", seg.target_text
        assert _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE) == []
        # ...and the ABSOLUTE write-back gate still flags the pre-existing miss.
        assert _flags(seg, "glossary_violation"), seg.qa_flags
        assert seg.status == SegStatus.NEEDS_REVIEW
    print("  2d. a pre-existing violation the fixer ignores -> NOT reverted OK")


def test_c_never_reverts_the_first_translation_to_source():
    """The guard is a DELTA. On the FIRST translating step working[] still holds the
    untranslated SOURCE, where a DNT term is present by definition — reverting there
    would replace a good translation with English."""
    with Session(engine) as s:
        p = _mk_project(s)
        _add_term(s, p.id, "BrandX", "BrandX", dnt=True)
        doc = _mk_doc(s, p, sources=("Keep BrandX visible.",))
        wf = _mk_wf(s)
        _step(s, wf.id, 0, marker="STEP0:", name="Translator")
        wf_id, doc_id = wf.id, doc.id
    with _FakeCalls([("STEP0:", None, "Mantener MarcaX visible.")]):
        _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert seg.target_text == "Mantener MarcaX visible.", seg.target_text
        assert _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE) == []
        # The ordinary DNT gate still routes it to review — detection is unchanged.
        assert _flags(seg, "dnt_violation") and seg.status == SegStatus.NEEDS_REVIEW
    print("  2e. first translating step is never reverted back to the source OK")


def test_c_nothing_vanishes_across_steps():
    """Two steps break the same term: BOTH reverts are recorded (the proposal's
    "nothing vanishes" invariant), and a later ACCEPTED edit never erases the record of
    an earlier discarded one."""
    with Session(engine) as s:
        p = _mk_project(s)
        _add_term(s, p.id, "Widget", "Aparato")
        doc = _mk_doc(s, p, sources=("Buy the Widget now.",))
        wf = _mk_wf(s)
        _step(s, wf.id, 0, marker="STEP0:", name="Translator")
        _step(s, wf.id, 1, marker="FIX1:", name="Fixer1")
        _step(s, wf.id, 2, marker="FIX2:", name="Fixer2")
        _step(s, wf.id, 3, marker="POLISH:", name="Polisher")
        wf_id, doc_id = wf.id, doc.id
    with _FakeCalls(
        [
            ("STEP0:", None, "Compra el Aparato ya."),
            ("FIX1:", None, "Compra el chisme ya."),
            ("FIX2:", None, "Compra la cosa ya."),
            ("POLISH:", None, "Compra el Aparato hoy."),
        ]
    ):
        events = _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert seg.target_text == "Compra el Aparato hoy.", seg.target_text
        fr = _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE)
        assert [f["step"] for f in fr] == ["Fixer1", "Fixer2"], fr
        assert seg.status == SegStatus.NEEDS_REVIEW
    assert _done(events)["lqa_safety"]["reverted"] == 2
    print(
        "  2g. two reverts on one segment are both recorded; a later accepted edit "
        "does not erase them OK"
    )


def test_c_kill_switch():
    wf_id, doc_id = _two_step_doc(("Buy the Widget now.",), [("Widget", "Aparato", False)])
    settings.set_many({"glossary_fix_guard_enabled": "0"})
    try:
        with _FakeCalls(
            [("STEP0:", None, "Compra el Aparato ya."), ("FIX:", None, "Compra el chisme ya.")]
        ):
            events = _run(wf_id, doc_id)
    finally:
        settings.set_many({"glossary_fix_guard_enabled": "1"})
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert seg.target_text == "Compra el chisme ya.", ("pre-Phase-1 behaviour", seg.target_text)
        assert _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE) == []
    assert "lqa_safety" not in (_done(events) or {})
    print("  2f. glossary_fix_guard_enabled=0 restores pre-Phase-1 behaviour OK")


# ===========================================================================
# 3. (D1) TM provenance guard — segmented correction loop
# ===========================================================================
TM_SRC = "Welcome to the Widget store."
TM_TGT = "Bienvenido a la tienda de Aparato."
OTHER_SRC = "Buy the Widget now."


def _loop_doc(*, tm_protect=None):
    """Translate -> LQA -> Fix(prev_lqa:failed) over two segments, one of which has a
    100% TM match seeded."""
    with Session(engine) as s:
        p = _mk_project(s)
        _add_term(s, p.id, "Widget", "Aparato")
        _seed_tm(s, p.id, TM_SRC, TM_TGT)
        doc = _mk_doc(s, p, sources=(TM_SRC, OTHER_SRC))
        wf = _mk_wf(s)
        _step(s, wf.id, 0, marker="STEP0:", name="Translator")
        _step(s, wf.id, 1, marker="", name="LQA", step_type=StepType.LQA)
        _step(
            s,
            wf.id,
            2,
            marker="FIX:",
            name="Fixer",
            run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
            tm_protect=tm_protect,
        )
        return wf.id, doc.id


def _run_loop(wf_id, doc_id, *, fix_text="Compra el Aparato hoy."):
    with (
        _FailingLQA(),
        _FakeCalls([("STEP0:", None, "Compra el Aparato ya."), ("FIX:", None, fix_text)]),
    ):
        return _run(wf_id, doc_id)


def test_d1_protects_tm_in_correction_loop():
    wf_id, doc_id = _loop_doc()
    events = _run_loop(wf_id, doc_id)
    with Session(engine) as s:
        tm_seg, other = _segs(s, doc_id)
        assert tm_seg.target_text == TM_TGT, ("TM text byte-identical", tm_seg.target_text)
        assert tm_seg.tm_match_pct == 100, tm_seg.tm_match_pct
        # ERRATUM E2 (gate C round 2, M2 -- ratified 2026-09-06): this workflow is a
        # branch-less ALWAYS-FAILING check followed by a `prev_lqa:failed` fixer, so the
        # run ENDS with its check's standing verdict still FAILED and is therefore HELD:
        # every unit it produced lands NEEDS_REVIEW under the gate-hold badge, the TM fill
        # included, and the document export is refused until a reviewer looks. D1's
        # guarantee is about the TEXT (asserted above -- byte-identical TM target, 100%
        # band kept), not about the review status, and the run-level hold is orthogonal to
        # it. Pre-E2 this read `mt`.
        assert tm_seg.status == SegStatus.NEEDS_REVIEW, tm_seg.status
        assert other.target_text == "Compra el Aparato hoy.", other.target_text
    done = _done(events)
    assert done["lqa_safety"]["tm_protected"] == 1, done.get("lqa_safety")
    fix_ev = [e for e in events if e["type"] == "step" and e["step_name"] == "Fixer"][0]  # noqa: RUF015
    assert fix_ev["lqa_safety"]["tm_protected"] == 1
    assert "TM segment(s) protected" in (fix_ev["error"] or ""), fix_ev["error"]
    print(
        "  3a. correction loop over a 100% TM doc: TM segment untouched, "
        "tm_match_pct==100, non-TM segment fixed OK"
    )


def test_d1_tm_protect_off_unprotects():
    wf_id, doc_id = _loop_doc(tm_protect=False)
    _run_loop(wf_id, doc_id)
    with Session(engine) as s:
        tm_seg = _segs(s, doc_id)[0]
        assert tm_seg.target_text == "Compra el Aparato hoy.", tm_seg.target_text
        assert tm_seg.tm_match_pct != 100, tm_seg.tm_match_pct
    print("  3b. tm_protect=off -> the fixer rewrites the TM segment (opt-out) OK")


def test_d1_tm_protect_on_protects_a_plain_step():
    """A NON-correction-loop step: the default leaves it alone (existing polish
    workflows keep their semantics), tm_protect=on protects it."""

    def _build(tm_protect):
        with Session(engine) as s:
            p = _mk_project(s)
            _seed_tm(s, p.id, TM_SRC, TM_TGT)
            doc = _mk_doc(s, p, sources=(TM_SRC,))
            wf = _mk_wf(s)
            _step(s, wf.id, 0, marker="STEP0:", name="Translator")
            _step(s, wf.id, 1, marker="POLISH:", name="Polisher", tm_protect=tm_protect)
            return wf.id, doc.id

    wf_id, doc_id = _build(None)
    with _FakeCalls([("STEP0:", None, "x"), ("POLISH:", None, "Pulido.")]):
        _run(wf_id, doc_id)
    with Session(engine) as s:
        assert _segs(s, doc_id)[0].target_text == "Pulido.", "default must not change a polish step"

    wf_id, doc_id = _build(True)
    with _FakeCalls([("STEP0:", None, "x"), ("POLISH:", None, "Pulido.")]):
        _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert seg.target_text == TM_TGT and seg.tm_match_pct == 100, seg.target_text
    print("  3c. default leaves a polish step unchanged; tm_protect=on protects it OK")


def test_d1_kill_switch():
    wf_id, doc_id = _loop_doc()
    settings.set_many({"tm_fix_protection_enabled": "0"})
    try:
        _run_loop(wf_id, doc_id)
    finally:
        settings.set_many({"tm_fix_protection_enabled": "1"})
    with Session(engine) as s:
        tm_seg = _segs(s, doc_id)[0]
        assert tm_seg.target_text == "Compra el Aparato hoy.", tm_seg.target_text
        assert tm_seg.tm_match_pct != 100
    print("  3d. tm_fix_protection_enabled=0 restores pre-Phase-1 behaviour OK")


def test_d1_and_c_compose():
    """A fixer whose edit ALSO breaks a term on the non-TM segment: the TM segment is
    protected (never called) AND the non-TM edit is reverted."""
    wf_id, doc_id = _loop_doc()
    events = _run_loop(wf_id, doc_id, fix_text="Compra el chisme hoy.")
    with Session(engine) as s:
        tm_seg, other = _segs(s, doc_id)
        assert tm_seg.target_text == TM_TGT and tm_seg.tm_match_pct == 100
        assert _flags(tm_seg, lqa_safety.FIX_REVERTED_FLAG_TYPE) == []
        assert other.target_text == "Compra el Aparato ya.", other.target_text
        assert _flags(other, lqa_safety.FIX_REVERTED_FLAG_TYPE)
    done = _done(events)
    assert done["lqa_safety"] == {
        "reverted": 1,
        "tm_protected": 1,
        "degraded": 0,
        "lqa_tm_excluded": 1,
    }, done["lqa_safety"]
    print("  3e. C and D1 compose on one run (1 protected, 1 reverted) OK")


# ===========================================================================
# 4. FULL_TEXT — flags, never reverts
# ===========================================================================
def test_full_text_flags_without_reverting():
    with Session(engine) as s:
        p = _mk_project(s)
        _add_term(s, p.id, "Widget", "Aparato")
        doc = _mk_doc(s, p, sources=("Buy the Widget now.",), mode=TransMode.FULL_TEXT)
        wf = _mk_wf(s)
        _step(s, wf.id, 0, marker="STEP0:", name="Translator")
        _step(s, wf.id, 1, marker="FIX:", name="Fixer")
        wf_id, doc_id = wf.id, doc.id
    with _FakeCalls(
        [("STEP0:", None, "Compra el Aparato ya."), ("FIX:", None, "Compra el chisme ya.")]
    ):
        events = _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        # NOT reverted — the whole-document revert granularity is deliberately refused.
        assert seg.target_text == "Compra el chisme ya.", seg.target_text
        reg = _flags(seg, lqa_safety.FIX_REGRESSION_FLAG_TYPE)
        assert len(reg) == 1 and "Fixer" in reg[0]["message"], seg.qa_flags
        assert _flags(seg, lqa_safety.FIX_REVERTED_FLAG_TYPE) == [], "FULL_TEXT never reverts"
        assert seg.status == SegStatus.NEEDS_REVIEW
    done = _done(events)
    assert done["lqa_safety"] == {"reverted": 0, "term_regression": 1}, done.get("lqa_safety")
    print("  4a. FULL_TEXT: term-breaking step attributed, text NOT reverted OK")


def test_full_text_single_step_never_flags():
    with Session(engine) as s:
        p = _mk_project(s)
        _add_term(s, p.id, "Widget", "Aparato")
        doc = _mk_doc(s, p, sources=("Buy the Widget now.",), mode=TransMode.FULL_TEXT)
        wf = _mk_wf(s)
        _step(s, wf.id, 0, marker="STEP0:", name="Translator")
        wf_id, doc_id = wf.id, doc.id
    with _FakeCalls([("STEP0:", None, "Compra el chisme ya.")]):
        events = _run(wf_id, doc_id)
    with Session(engine) as s:
        seg = _segs(s, doc_id)[0]
        assert _flags(seg, lqa_safety.FIX_REGRESSION_FLAG_TYPE) == [], seg.qa_flags
        assert _flags(seg, "glossary_violation"), "the absolute gate still fires"
    assert "lqa_safety" not in (_done(events) or {})
    print("  4b. FULL_TEXT: the first translating step is never accused OK")


# ===========================================================================
# 5. Regression — a workflow with no LQA / no glossary is unchanged
# ===========================================================================
def test_no_lqa_no_glossary_is_unchanged():
    with Session(engine) as s:
        p = _mk_project(s)  # NO glossary entries
        doc = _mk_doc(s, p, sources=("Alpha one.", "Beta two."))
        wf = _mk_wf(s)
        _step(s, wf.id, 0, marker="STEP0:", name="Translator")
        _step(s, wf.id, 1, marker="POLISH:", name="Polisher")
        wf_id, doc_id = wf.id, doc.id
    with _FakeCalls([("STEP0:", None, "uno"), ("POLISH:", None, "final")]):
        events = _run(wf_id, doc_id)
    assert all("lqa_safety" not in ev for ev in events), (
        "no guard acted -> no event may carry the key"
    )
    with Session(engine) as s:
        for seg in _segs(s, doc_id):
            assert seg.target_text == "final", seg.target_text
            assert seg.status == SegStatus.MT, seg.status
            assert not [f for f in (seg.qa_flags or []) if f.get("type", "").startswith("fix_")], (
                seg.qa_flags
            )
    print("  5a. workflow with no LQA + no glossary: behaviourally unchanged OK")


# ===========================================================================
# 6. Parity — service / CLI / api / web / flag surfacing
# ===========================================================================
def test_parity():
    with Session(engine) as s:
        wf = _mk_wf(s, "parity")
        st = workflows.add_step(
            s, wf.id, name="T", step_type=StepType.LLM_AGENT, prompt_text="Translate {text}"
        )
        assert st.tm_protect is None, st.tm_protect
        st2 = workflows.add_step(
            s,
            wf.id,
            name="F",
            step_type=StepType.LLM_AGENT,
            prompt_text="Fix {text}",
            tm_protect="on",
        )
        assert st2.tm_protect is True, st2.tm_protect
        # tristate round-trip through the partial edit seam
        workflows.update_step(s, st2.id, tm_protect="off")
        assert s.get(WorkflowStep, st2.id).tm_protect is False
        workflows.update_step(s, st2.id, tm_protect="auto")
        assert s.get(WorkflowStep, st2.id).tm_protect is None
        # unrecognised -> inherit (never a guess)
        workflows.update_step(s, st2.id, tm_protect="banana")
        assert s.get(WorkflowStep, st2.id).tm_protect is None
        wf_id = wf.id
    print("  6a. workflows.add_step/update_step tristate round-trip OK")

    # Dead-field honesty: llm_agent-only, refused on lqa/human_review.
    with Session(engine) as s:
        wf2 = _mk_wf(s, "parity-dead")
        for bad_type in (StepType.LQA, StepType.HUMAN_REVIEW):
            try:
                workflows.add_step(s, wf2.id, step_type=bad_type, tm_protect="on")
            except workflows.WorkflowError as exc:
                assert "TM protection" in str(exc), exc
            else:
                raise AssertionError(f"tm_protect must be refused on {bad_type}")
        # ...but the inert default is accepted (an ordinary lqa step still saves).
        workflows.add_step(s, wf2.id, step_type=StepType.LQA, tm_protect="auto")
    print("  6b. tm_protect refused as a dead field on lqa/human_review OK")

    # CLI read surface (`workflow show --json`) + the human opts column.
    from app.cli._common import _opts_label

    _base = {"tm_examples": True, "tm_examples_window": 3}
    assert _opts_label({**_base, "tm_protect": True}) == "tm-protect:on"
    assert _opts_label({**_base, "tm_protect": False}) == "tm-protect:off"
    assert _opts_label({**_base, "tm_protect": None}) == "-"
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json", "workflow", "show", str(wf_id)],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env=dict(os.environ),
    )
    assert out.returncode == 0, out.stderr[-800:]
    import json

    payload = json.loads(out.stdout)
    assert all("tm_protect" in row for row in payload["steps"]), payload["steps"][0].keys()
    print("  6c. CLI `workflow show --json` carries tm_protect; opts label OK")

    # /api workflow payload mirrors the CLI row shape (the P6 row-shape guard).
    # 2026-09-14 SRP split: the /api routes live in api_cli.py plus sibling pieces
    src = "".join(
        open(f, encoding="utf-8").read()  # noqa: SIM115
        for f in sorted(glob.glob(os.path.join(_ROOT, "app", "web", "api_cli*.py")))
    )
    assert '"tm_protect": getattr(st, "tm_protect", None)' in src, "api payload missing tm_protect"
    # Web builder: the tristate select exists in BOTH the add and the edit form.
    tpl = open(  # noqa: SIM115
        os.path.join(_ROOT, "app", "templates", "workflow_builder.html"), encoding="utf-8"
    ).read()
    assert tpl.count('name="tm_protect"') == 2, tpl.count('name="tm_protect"')
    assert tpl.count('value="auto"') >= 2 and 'value="off"' in tpl
    print("  6d. /api payload + web builder tristate select present OK")

    # The new qa_flag families are surfaced on BOTH review surfaces.
    from app.web import api_cli as _api

    assert lqa_safety.FIX_REVERTED_FLAG_TYPE in _api._SEGMENT_SURFACED
    assert lqa_safety.FIX_REGRESSION_FLAG_TYPE in _api._SEGMENT_SURFACED
    line = _api.review_summary_line(
        [
            {"qa_flags": [{"type": lqa_safety.FIX_REVERTED_FLAG_TYPE}]},
            {"qa_flags": [{"type": lqa_safety.FIX_REGRESSION_FLAG_TYPE}]},
        ]
    )
    assert "fix-reverted 1" in line and "fix-regression 1" in line, line
    docs_src = open(os.path.join(_ROOT, "app", "web", "documents.py"), encoding="utf-8").read()  # noqa: SIM115
    assert "lqa_safety.FIX_REVERTED_FLAG_TYPE" in docs_src, "editor grid must surface it"
    print("  6e. fix_reverted / fix_term_regression surfaced in editor + headless summary OK")


# ===========================================================================
# 7. Mutation probe — the guards are load-bearing (fail-before proof)
# ===========================================================================
_NEUTERED = """

# --- MUTATION PROBE (test-only, restored immediately) -----------------------
def introduced_violations(source_text, pre_text, post_text, glossary_terms,
                          *, target_locale=None):
    return [], []


def tm_protect_for(step, is_correction_loop):
    return False
"""


def test_mutation_probe():
    path = os.path.join(_ROOT, "app", "services", "lqa_safety.py")
    with open(path, "rb") as fh:
        original = fh.read()
    try:
        with open(path, "wb") as fh:
            fh.write(original + _NEUTERED.encode("utf-8"))
        importlib.reload(lqa_safety)

        # (C) probe: with the delta neutered, the fixer's term-breaking edit COMMITS.
        wf_id, doc_id = _two_step_doc(("Buy the Widget now.",), [("Widget", "Aparato", False)])
        with _FakeCalls(
            [("STEP0:", None, "Compra el Aparato ya."), ("FIX:", None, "Compra el chisme ya.")]
        ):
            _run(wf_id, doc_id)
        with Session(engine) as s:
            seg = _segs(s, doc_id)[0]
            assert seg.target_text == "Compra el chisme ya.", (
                "MUTATION PROBE FAILED: the C assertions would pass without the guard"
            )
            assert _flags(seg, "fix_reverted") == []

        # (D1) probe: with protection neutered, the fixer rewrites the 100% TM segment.
        wf_id, doc_id = _loop_doc()
        _run_loop(wf_id, doc_id)
        with Session(engine) as s:
            tm_seg = _segs(s, doc_id)[0]
            assert tm_seg.target_text == "Compra el Aparato hoy.", (
                "MUTATION PROBE FAILED: the D1 assertions would pass without the guard"
            )
            assert tm_seg.tm_match_pct != 100
        print(
            "  7a. mutation probe: both guards neutered -> the group-2/3 properties "
            "DISAPPEAR (the tests are load-bearing) OK"
        )
    finally:
        with open(path, "wb") as fh:
            fh.write(original)
        importlib.reload(lqa_safety)
    with open(path, "rb") as fh:
        assert fh.read() == original, "restore must be byte-identical"
    # ...and the restored module behaves again.
    assert lqa_safety.introduced_violations(
        "Buy the Widget now.",
        "Compra el Aparato.",
        "Compra el chisme.",
        [_gloss("Widget", "Aparato")],
    ) == ([], [{"source_term": "Widget", "target_term": "Aparato"}])
    print("  7b. restore byte-identical + behaviour back OK")


if __name__ == "__main__":
    print("LQA safety floor (PROPOSAL-lqa-glossary-adjudication Phase 1)")
    print("1. lqa_safety unit")
    test_unit()
    print("2. (C) fixer post-check with revert")
    test_c_reverts_introduced_glossary_miss()
    test_c_reverts_introduced_dnt_breach()
    test_c_accepts_a_real_fix()
    test_c_leaves_pre_existing_violation_alone()
    test_c_never_reverts_the_first_translation_to_source()
    test_c_nothing_vanishes_across_steps()
    test_c_kill_switch()
    print("3. (D1) TM provenance guard")
    test_d1_protects_tm_in_correction_loop()
    test_d1_tm_protect_off_unprotects()
    test_d1_tm_protect_on_protects_a_plain_step()
    test_d1_kill_switch()
    test_d1_and_c_compose()
    print("4. FULL_TEXT asymmetry")
    test_full_text_flags_without_reverting()
    test_full_text_single_step_never_flags()
    print("5. regression")
    test_no_lqa_no_glossary_is_unchanged()
    print("6. parity")
    test_parity()
    print("7. mutation probe")
    test_mutation_probe()
    print("LQA SAFETY FLOOR OK")
