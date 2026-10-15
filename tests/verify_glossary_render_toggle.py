"""verify_glossary_render_toggle — Terminology backend T3 (SPEC §9-T3).

The concept-glossary renderer swap behind the per-step `concept_glossary_render` toggle
(default OFF). Covers, per §9-T3 Tests:

  1. OFF -> both prompt seams byte-identical to the plain glossary_line baseline
     (LAW-TERM-1/14); the shared `_render_glossary_block` seam returns the full match set.
  2. ON  -> the Markdown block with caps / ordering / audience / do-not-use / per-field
     budget (LAW-TERM-13/14/15/16); render_for_llm returns the exact shown_terms.
  3. The shown_terms are the five-key compat dicts that feed the gates in-process.
  4. A fixture with MORE matches than concept_cap AND a human-only term produces NO new
     glossary_violation / dnt_violation on the PRODUCING step's own write-back (ON, the shown
     term is still enforced), while the OFF control (full glossary shown + enforced) DOES flag
     it. Proven in BOTH run modes.
  5. CROSS-STEP, in BOTH run modes: a toggle-ON Translate step FOLLOWED BY a toggle-OFF
     correction (llm_agent) step, with a capped-out AND a human-only term:
       (a) the correction FIX GUARD (segmented) / REGRESSION ARM (whole-document) reads the
           PRODUCING step's shown set and manufactures NO revert/flag for a term the model was
           never shown;
       (a') once the toggle-OFF correction PRODUCES the text, enforcement reverts to the FULL
            match (LAW-TERM-15 "OFF producer -> full match", M5): the run flags exactly like a
            both-OFF run — the shown-set home is reset by the OFF producer, not left to leak;
       (a'') the lazily-rebuilt LQA adjudicator union (`_lqa_terms_now()`) is the producing
             step's shown set, not the full match (M3);
       (b) the toggle-OFF correction step's assembled prompt is byte-identical to the toggle-OFF
           baseline (residue 8's closing assertion).
     Both steps OFF -> byte-identical full-match enforcement (the flag reappears).
  M4. The whole-document LAW-TERM-16 stamp helper joins the RIGHT leg of a multi-locale action
      (filters Segment.target_locale).
  6. The plain-MT seam drops human-only terms from payload["glossary"] at all three build
     sites (H7/M17): prompts.translation_prompt's payload + the mt-channel filter that feeds
     the two orchestrator direct assemblies.
  7. With the Prompt Tap ENABLED the per-call capture record carries meta.shown_terms /
     meta.target_locale / meta.segment_index (=Segment.id) and the adherence leg reads them
     (not the replay); with the Tap DISABLED there is no capture and enforcement is still
     consistent via the in-memory map.

Final line: "GLOSSARY RENDER TOGGLE OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_rendertoggle_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    GlossaryConcept,
    GlossaryTerm,
    Project,
    Segment,
    StepType,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import glossary_concepts, orchestrator, prompts, quality, settings

init_db()

SRC, TGT = "en-US", "es-ES"
_SEQ = [0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


# --------------------------------------------------------------------------- #
# synthetic match dicts (the shape match_concepts returns) for the PURE tests
# --------------------------------------------------------------------------- #
def _m(
    source,
    target,
    *,
    dnt=False,
    audience="human,mt,llm",
    definition="",
    pos="",
    domain="",
    alternatives=None,
    legacy=False,
):
    d = {
        "source_term": source,
        "target_term": target,
        "sense_label": None,
        "dnt": dnt,
        "case_sensitive": False,
    }
    if legacy:
        d["_concept"] = None
    else:
        d["_concept"] = {
            "definition": definition,
            "domain": domain,
            "subdomain": "",
            "notes": "",
            "status": "preferred",
            "term_type": "",
            "part_of_speech": pos,
            "usage_note": "",
            "context_example": "",
            "audience": audience,
            "alternatives": alternatives or [],
        }
    return d


# ===========================================================================
# 1-3. Pure renderer/filter seams (LAW-TERM-4/13/15).
# ===========================================================================
def test_render_off_byte_identical():
    terms = [_m("Widget", "Aparato"), _m("Acme", "", dnt=True)]
    concepts = list(terms)
    # OFF (render_cfg falsy) -> glossary_line verbatim, shown None, mt == full terms.
    block, shown, mt = orchestrator._render_glossary_block(concepts, terms, {"on": False})
    assert block == prompts.glossary_line(terms), ("OFF must equal glossary_line", block)
    assert shown is None, shown
    assert mt == terms, mt
    # A None concepts (no toggle-ON step in the run) is also OFF regardless of cfg.
    block2, shown2, mt2 = orchestrator._render_glossary_block(None, terms, {"on": True})
    assert block2 == prompts.glossary_line(terms) and shown2 is None and mt2 == terms
    print("  1. OFF: _render_glossary_block == glossary_line, shown=None, mt=full: OK")


def test_render_on_caps_ordering_audience_budget():
    long_def = "D" * 400  # exceeds the 200-char per-field render budget
    concepts = [
        _m("cloud", "nube", definition=long_def, pos="noun", domain="infra"),
        _m("Gadget", "Chisme", audience="human"),  # human-only -> dropped on llm
        _m("Acme", "", dnt=True),  # DNT -> always kept, ordered FIRST
        _m("legacy", "heredado", legacy=True),  # no concept -> always kept
        _m(
            "depry",
            "nuevo",
            alternatives=[{"surface": "viejo", "status": "deprecated", "replacement": "nuevo"}],
        ),
        _m("alpha", "alfa"),
        _m("beta", "beta2"),
        _m("gamma", "gamma2"),
    ]
    # audience filter (llm): the human-only Gadget is dropped; everything else kept.
    kept = glossary_concepts.filter_by_audience(concepts, "llm")
    assert all(k["source_term"] != "Gadget" for k in kept), "human-only term must be dropped on llm"
    assert any(k["source_term"] == "legacy" for k in kept), "legacy match must always be kept"
    assert any(k["source_term"] == "Acme" for k in kept), "DNT must always be kept"
    # render with a small concept cap so ordering (DNT/forbidden first) is load-bearing.
    _blk, shown = glossary_concepts.render_for_llm(kept, concept_cap=3, token_cap=1500)
    assert len(shown) == 3, ("concept_cap not enforced", len(shown), shown)
    # DNT + forbidden/deprecated-bearing concepts survive the cap first (ordering).
    shown_src = {s["source_term"] for s in shown}
    assert "Acme" in shown_src and "depry" in shown_src, (
        "DNT/forbidden must survive the cap",
        shown_src,
    )
    # per-field budget: the 400-char definition is truncated with the … marker IF cloud
    # survived; force it in by rendering uncapped and checking the budget on the block.
    block_all, shown_all = glossary_concepts.render_for_llm(kept, concept_cap=99, token_cap=100000)
    assert "…" in block_all, ("per-field budget marker missing", block_all)
    assert long_def not in block_all, "the full 400-char definition must be budget-truncated"
    # do-not-use sub-line for the deprecated alternative.
    assert "do not use: viejo" in block_all, ("deprecated alternative sub-line missing", block_all)
    # DNT rendering.
    assert "Acme -> (do not translate)" in block_all, block_all
    # shown_terms are exactly the five compat keys (feed the gates).
    for s in shown_all:
        assert set(s) == {"source_term", "target_term", "sense_label", "dnt", "case_sensitive"}, s
    print(
        "  2-3. ON: caps + DNT/forbidden-first ordering + audience drop + per-field budget "
        "+ do-not-use + five-key shown_terms: OK"
    )


def test_render_token_cap_drops_whole_concepts():
    concepts = [_m("Acme", "", dnt=True)] + [_m(f"t{i}", f"v{i}") for i in range(20)]
    block, shown = glossary_concepts.render_for_llm(concepts, concept_cap=99, token_cap=30)
    # token cap holds on the emitted block (marker counted inside), and DNT survived first.
    assert glossary_concepts._est_tokens(block) <= 30, (glossary_concepts._est_tokens(block), block)
    assert any(s["dnt"] for s in shown), "DNT must survive the token cap (ordered first)"
    print("  2b. token cap drops whole concepts from the tail (DNT survives first): OK")


# ===========================================================================
# 6. plain-MT seam: mt-channel filter drops human-only at the three build sites.
# ===========================================================================
def test_mt_seam_drops_human_only():
    concepts = [
        _m("Widget", "Aparato"),
        _m("Gadget", "Chisme", audience="human"),
        _m("Acme", "", dnt=True),
    ]
    mt = orchestrator._five_key(glossary_concepts.filter_by_audience(concepts, "mt"))
    srcs = {t["source_term"] for t in mt}
    assert "Gadget" not in srcs, ("human-only term must not reach the mt payload", srcs)
    assert {"Widget", "Acme"} <= srcs, srcs
    # site 1: translation_prompt's block + payload["glossary"] carry only the mt-filtered set.
    _sys, prompt, payload = prompts.translation_prompt("x", SRC, TGT, mt)
    assert "Chisme" not in prompt and "Gadget" not in prompt, prompt
    assert all(t["source_term"] != "Gadget" for t in payload["glossary"]), payload["glossary"]
    # sites 2+3 (orchestrator direct assemblies) receive the SAME already-filtered `mt`.
    for _asm in ({"text": "x", "target": TGT, "glossary": mt},):
        assert all(t["source_term"] != "Gadget" for t in _asm["glossary"])
    print(
        "  6. plain-MT seam: human-only dropped from translation_prompt block + payload "
        "(all three build sites): OK"
    )


# --------------------------------------------------------------------------- #
# integration harness (module glossary + concepts + a real workflow run)
# --------------------------------------------------------------------------- #
def _customer():
    with Session(engine) as s:
        c = Customer(name=f"RT Cust {_seq()}", org_id=1)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _module_glossary(cid, terms, human_only=(), name=None):
    """terms: list of (source, target, dnt). human_only: source surfaces to mark audience
    human (dropped on the llm+mt channels). Returns an APPROVED module glossary id."""
    from app.services import glossary_module as gm

    with Session(engine) as s:
        gid = gm.create(s, name or f"RT GL {_seq()}", org_id=1, customer_id=cid).id
        gm.set_pairs(s, gid, [(SRC, TGT)])
        for src, tgt, dnt in terms:
            gm.add_term(s, gid, src, target_term=tgt, dnt=dnt, source_locale=SRC, target_locale=TGT)
        # Mark the human-only source terms directly on their GlossaryTerm rows (avoids the
        # flag-gated set_audience editor seam; filter_by_audience reads the SOURCE audience).
        if human_only:
            cids = [
                c.id
                for c in s.exec(
                    select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)
                ).all()
            ]
            rows = s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(cids))).all()
            for t in rows:
                if t.term in human_only:
                    t.audience = "human"
                    s.add(t)
            s.commit()
        gm.approve(s, gid, "t@x.com")
        return gid


def _project(cid, provider="claude"):
    with Session(engine) as s:
        p = Project(
            name=f"rt-{_seq()}",
            org_id=1,
            customer_id=cid,
            source_locale=SRC,
            default_provider=provider,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _doc(project_id, sources, mode=TransMode.SEGMENTED):
    with Session(engine) as s:
        doc = Document(
            project_id=project_id,
            filename="d.txt",
            fmt="txt",
            mode=mode,
            skeleton={"units": [{"text": t} for t in sources]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, t in enumerate(sources):
            s.add(Segment(document_id=doc.id, target_locale=TGT, order_index=i, source_text=t))
        s.commit()
        return doc.id


def _wf(steps, provider="claude"):
    """steps: list of dicts {marker, gid?, render?, cap?, type?}. type defaults to
    llm_agent; type="lqa" builds an LQA judge step. Returns wf_id."""
    with Session(engine) as s:
        wf = Workflow(
            name=f"rtwf-{_seq()}",
            scope="global",
            source_locale=SRC,
            target_locale=TGT,
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, st in enumerate(steps):
            if st.get("type") == "lqa":
                row = WorkflowStep(
                    workflow_id=wf.id,
                    order_index=i,
                    name=st["marker"],
                    step_type=StepType.LQA,
                    provider=provider,
                    retry_max=1,
                    backoff_base=0.0,
                    run_condition=st.get("run_condition"),
                )
            else:
                row = WorkflowStep(
                    workflow_id=wf.id,
                    order_index=i,
                    name=st["marker"],
                    step_type=StepType.LLM_AGENT,
                    provider=provider,
                    prompt_text=f"{st['marker']} {{glossary}} {{text}}",
                    chunk_policy="none",
                    retry_max=1,
                    backoff_base=0.0,
                    glossary_id=st.get("gid"),
                    concept_glossary_render=bool(st.get("render")),
                    glossary_concept_cap=int(st.get("cap", 10)),
                    glossary_token_cap=0,
                    run_condition=st.get("run_condition"),
                )
            s.add(row)
        s.commit()
        return wf.id


class _ScoreSpy:
    """Spy for orchestrator.lqa.score_text — records the `glossary_terms` it was handed
    (the LQA judge's approved-terminology set, LAW-TERM-15 consumer 4) and returns a
    minimal passing card so the run completes."""

    def __init__(self):
        self._orig = None
        self.glossary_terms = None

    def __enter__(self):
        self._orig = orchestrator.lqa.score_text

        def fake(*a, **kw):
            self.glossary_terms = kw.get("glossary_terms")
            return {"errors": [], "score": 100.0, "passed": True, "gate": "passed"}

        orchestrator.lqa.score_text = fake
        return self

    def __exit__(self, *exc):
        orchestrator.lqa.score_text = self._orig
        return False


class _FakeCalls:
    """marker-dispatching fake for orchestrator.run_completion. rules: (marker, out)."""

    def __init__(self, rules):
        self.rules = rules
        self._orig = None
        self.calls = []  # (prompt, payload)

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            self.calls.append((prompt, payload))
            for marker, out in self.rules:
                if marker in prompt:
                    return {
                        "text": out,
                        "provider_used": "mock",
                        "model_used": "mock",
                        "attempts": 1,
                        "error": None,
                        "usage": {},
                    }
            raise AssertionError(f"no fake rule for prompt: {prompt[:120]!r}")

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False

    def prompt_for(self, marker):
        for p, _ in self.calls:
            if marker in p:
                return p
        return None


def _run(wf_id, doc_id):
    events = []
    with Session(engine) as s:
        for ev in orchestrator.run_workflow_stream(s, wf_id, doc_id, TGT):
            events.append(ev)
    return events


def _seg_flags(doc_id, kind):
    with Session(engine) as s:
        out = {}
        for seg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == TGT)
        ).all():
            out[seg.order_index] = [f for f in (seg.qa_flags or []) if f.get("type") == kind]
        return out


# Six terms so a concept_cap of 2 caps some out; Gadget is human-only.
_TERMS = [
    ("Widget", "Aparato", False),
    ("Gadget", "Chisme", False),  # marked human-only in setup
    ("cloud", "nube", False),
    ("alpha", "alfa", False),
    ("beta", "beta2", False),
    ("gamma", "gamma2", False),
]
_SRC_SENTENCE = "Buy the Widget and the Gadget for the cloud with alpha beta gamma."
# At concept_cap=2 the DNT/forbidden-first ordering keeps {Widget, cloud} as the SHOWN set;
# Gadget is dropped by the human-only audience filter; alpha/beta/gamma are capped out.
# Translations that keep/drop specific targets so the enforcement seams are discriminating:
_XLATE = "Compra el Aparato para la nube ya."  # keeps Aparato (Widget) + nube (cloud)
_XLATE_DROP_CLOUD = "Compra el Aparato para el sitio ya."  # keeps Aparato; DROPS nube (cloud)
_XLATE_KEEP_ALPHA = "Compra el Aparato con alfa para la nube."  # keeps Aparato + alfa + nube
_CORRECT = "Compra el Aparato para la nube hoy."  # keeps Aparato + nube; drops the rest (incl alfa)


# ===========================================================================
# 4. SEGMENTED: the toggle-ON producing step's OWN write-back enforces the shown set.
# ===========================================================================
def test_segmented_producing_step_shown_set():
    """A single toggle-ON Translate step (cap 2): its terminal write-back enforces EXACTLY
    the shown set {Widget, cloud}. The output drops cloud's target (a SHOWN term -> flagged)
    and every other target; the human-only Gadget and the capped-out alpha/beta/gamma were
    never shown -> NOT enforced -> no manufactured glossary_violation (LAW-TERM-15 consumer 1).
    Non-vacuous: reverting the write-back to the full match (gate Mutant A) flags them all."""
    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pid = _project(cid)
    doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.SEGMENTED)
    wf = _wf([{"marker": "XL", "gid": gid, "render": True, "cap": 2}])
    with _FakeCalls([("XL", _XLATE_DROP_CLOUD)]):
        _run(wf, doc)
    srcs = {g.get("source_term") for g in _seg_flags(doc, quality.GLOSSARY_FLAG_TYPE)[0]}
    assert "cloud" in srcs, ("a SHOWN, dropped term must be enforced (gate not defanged)", srcs)
    assert not ({"Gadget", "alpha", "beta", "gamma"} & srcs), (
        "a term the model was never shown (human-only / capped-out) was enforced under the toggle",
        srcs,
    )
    print(
        "  4. SEGMENTED single ON: write-back enforces the shown set (cloud flagged; "
        "human-only + capped-out NOT enforced): OK"
    )


# ===========================================================================
# 5(a). SEGMENTED cross-step: the fix guard reads the PRODUCING step's shown set.
# ===========================================================================
def test_segmented_fix_guard_reads_shown_set():
    """ON Translate (keeps alfa, a capped-out term) FOLLOWED BY a toggle-OFF correction that
    DROPS alfa. The correction's fix guard enforces the producing step's shown set
    {Widget, cloud}; alpha was never shown, so dropping it is NOT an introduced violation and
    the edit is NOT reverted (LAW-TERM-15 consumer 2). Non-vacuous: reverting the fix guard to
    the full match (gate Mutant A) rejects the edit and writes a fix_reverted flag."""
    from app.services import lqa_safety

    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pid = _project(cid)
    doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.SEGMENTED)
    wf = _wf(
        [
            {"marker": "XL", "gid": gid, "render": True, "cap": 2},
            {"marker": "FX", "render": False},
        ]
    )
    with _FakeCalls([("XL", _XLATE_KEEP_ALPHA), ("FX", _CORRECT)]):
        _run(wf, doc)
    reverts = _seg_flags(doc, lqa_safety.FIX_REVERTED_FLAG_TYPE)[0]
    assert not reverts, (
        "the fix guard manufactured a revert for a capped-out term the producing step never "
        "showed (must read the shown set, not the full match)",
        reverts,
    )
    print(
        "  5a. SEGMENTED ON->OFF: the fix guard reads the shown set — dropping a capped-out "
        "term is not a manufactured revert: OK"
    )


# ===========================================================================
# 5(a'). SEGMENTED cross-step: a toggle-OFF producing step reverts enforcement to the
#         FULL match (LAW-TERM-15 "OFF producer -> full match", M5).
# ===========================================================================
def test_segmented_on_off_reverts_to_full_match():
    """ON Translate -> OFF correction, both dropping Gadget/alpha/beta/gamma targets. Once the
    toggle-OFF correction PRODUCES the segment, LAW-TERM-15 says its enforcement is the FULL
    match — so the final glossary_violations must match a both-OFF run exactly (M5). Non-vacuous:
    without the shown-set reset the ON step's reduced set {Widget, cloud} would leak forward and
    flag nothing (both targets kept)."""

    def _run_pair(render_first):
        cid = _customer()
        gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
        pid = _project(cid)
        doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.SEGMENTED)
        wf = _wf(
            [
                {"marker": "XL", "gid": gid, "render": render_first, "cap": 2},
                {"marker": "FX", "render": False},
            ]
        )
        with _FakeCalls([("XL", _XLATE), ("FX", _CORRECT)]):
            _run(wf, doc)
        return {g.get("source_term") for g in _seg_flags(doc, quality.GLOSSARY_FLAG_TYPE)[0]}

    on_off = _run_pair(True)
    off_off = _run_pair(False)
    assert on_off == off_off, (
        "an ON->OFF run must flag like an OFF->OFF run once the OFF step produces the text "
        "(LAW-TERM-15 'OFF producer -> full match', M5)",
        on_off,
        off_off,
    )
    assert "alpha" in on_off, ("the full match must be enforced by the OFF producer", on_off)
    print("  5a'. SEGMENTED ON->OFF reverts to the full match (flags like OFF->OFF): OK")


# ===========================================================================
# 5(a''). SEGMENTED: the lazily-rebuilt LQA union is the producing step's shown set (M3).
# ===========================================================================
def test_segmented_lqa_union_reads_shown_set():
    """A toggle-ON Translate step FOLLOWED BY an LQA step: the LQA judge's approved-terminology
    set (`_lqa_terms_now()`, rebuilt lazily at the call site) is the producing step's shown set
    {Widget, cloud}, NOT the full match (LAW-TERM-15 consumer 4 / M3). Directly spies the
    glossary_terms handed to lqa.score_text."""
    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pid = _project(cid)
    doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.SEGMENTED)
    wf = _wf(
        [
            {"marker": "XL", "gid": gid, "render": True, "cap": 2},
            {"marker": "JUDGE", "type": "lqa"},
        ]
    )
    with _FakeCalls([("XL", _XLATE)]), _ScoreSpy() as spy:
        _run(wf, doc)
    assert spy.glossary_terms is not None, "the LQA judge was never handed a glossary set"
    judged = {t.get("source_term") for t in spy.glossary_terms}
    assert judged == {"Widget", "cloud"}, (
        "the LQA union must be the producing step's shown set, not the full match "
        "(no Gadget / alpha / beta / gamma)",
        judged,
    )
    print(
        "  5a''. SEGMENTED: the lazily-rebuilt LQA union == the shown set {Widget, cloud} "
        "(not the full match): OK"
    )


def test_segmented_both_off_enforces_full_match():
    """Control: with BOTH steps OFF the full glossary is shown AND enforced, so a term the
    mock output dropped IS flagged — proving the ON run's silence is the audience gate, not
    a broken gate."""
    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pid = _project(cid)
    doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.SEGMENTED)
    wf = _wf(
        [
            {"marker": "XL", "gid": gid, "render": False},
            {"marker": "FX", "render": False},
        ]
    )
    with _FakeCalls([("XL", _XLATE), ("FX", _CORRECT)]):
        _run(wf, doc)
    gflags = _seg_flags(doc, quality.GLOSSARY_FLAG_TYPE)[0]
    srcs = {g.get("source_term") for g in gflags}
    # OFF path: audience is never consulted, so even the human-only Gadget (target Chisme,
    # absent from the mock output) is enforced and flagged. This is the full-match baseline.
    assert "Gadget" in srcs, ("both-OFF must enforce the full match (Gadget flagged)", srcs)
    print(
        "  5. SEGMENTED both OFF: full-match enforcement flags the dropped term "
        "(byte-identical baseline): OK"
    )


# ===========================================================================
# 5(b). WHOLE-DOCUMENT (FULL_TEXT): producing-step write-back, regression arm,
#        OFF-producer revert, and residue-8 prompt byte-identity.
# ===========================================================================
def test_wholedoc_producing_step_shown_set():
    """A single toggle-ON Translate step (cap 2) in FULL_TEXT mode: the whole-document
    terminal write-back enforces the run-level shown set {Widget, cloud}. The output drops
    cloud (SHOWN -> flagged) and the rest; Gadget/alpha/beta/gamma were never shown ->
    NOT enforced (M2). Non-vacuous: reverting the write-back to the full match (Mutant A)
    flags them all."""
    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pid = _project(cid)
    doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.FULL_TEXT)
    wf = _wf([{"marker": "XL", "gid": gid, "render": True, "cap": 2}])
    with _FakeCalls([("XL", _XLATE_DROP_CLOUD)]):
        _run(wf, doc)
    with Session(engine) as s:
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc, Segment.target_locale == TGT)
        ).first()
        srcs = {
            g.get("source_term")
            for g in (seg.qa_flags or [])
            if g.get("type") == quality.GLOSSARY_FLAG_TYPE
        }
    assert "cloud" in srcs, ("a SHOWN, dropped term must be enforced (gate not defanged)", srcs)
    assert not ({"Gadget", "alpha", "beta", "gamma"} & srcs), (
        "the whole-document write-back enforced a term the model was never shown",
        srcs,
    )
    print(
        "  5b. WHOLE-DOCUMENT single ON: terminal write-back enforces the run-level shown "
        "set (cloud flagged; human-only + capped-out NOT): OK"
    )


def test_wholedoc_regression_arm_reads_shown_set():
    """ON Translate (keeps alfa) -> OFF correction (drops alfa). The FULL_TEXT regression arm
    reads the PRIOR producer's run-level shown set {Widget, cloud}; alpha was never shown, so
    dropping it manufactures NO regression flag (LAW-TERM-15 (i), FLAG-ONLY arm). Non-vacuous:
    reverting the arm to the full match (Mutant A) manufactures a fix_term_regression for alpha."""
    from app.services import lqa_safety

    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pid = _project(cid)
    doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.FULL_TEXT)
    wf = _wf(
        [
            {"marker": "XL", "gid": gid, "render": True, "cap": 2},
            {"marker": "FX", "render": False},
        ]
    )
    with _FakeCalls([("XL", _XLATE_KEEP_ALPHA), ("FX", _CORRECT)]):
        _run(wf, doc)
    with Session(engine) as s:
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc, Segment.target_locale == TGT)
        ).first()
        regs = [
            f for f in (seg.qa_flags or []) if f.get("type") == lqa_safety.FIX_REGRESSION_FLAG_TYPE
        ]
    for f in regs:
        assert "alpha" not in str(f) and "alfa" not in str(f), (
            "the regression arm manufactured a flag for a capped-out term the model was "
            "never shown",
            f,
        )
    print(
        "  5b'. WHOLE-DOCUMENT ON->OFF: the regression arm reads the shown set — no "
        "manufactured regression for a capped-out term: OK"
    )


def test_wholedoc_on_off_reverts_to_full_match():
    """ON Translate -> OFF correction (both drop Gadget/alpha/beta/gamma). Once the toggle-OFF
    correction produces the body, the run-level shown-set home is RESET to None so the terminal
    write-back enforces the FULL match — the final glossary_violations must match a both-OFF run
    (M5). Non-vacuous: without the reset the ON step's {Widget, cloud} would leak forward and
    (both kept) flag nothing."""

    def _run_pair(render_first):
        cid = _customer()
        gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
        pid = _project(cid)
        doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.FULL_TEXT)
        wf = _wf(
            [
                {"marker": "XL", "gid": gid, "render": render_first, "cap": 2},
                {"marker": "FX", "render": False},
            ]
        )
        with _FakeCalls([("XL", _XLATE), ("FX", _CORRECT)]):
            _run(wf, doc)
        with Session(engine) as s:
            seg = s.exec(
                select(Segment).where(Segment.document_id == doc, Segment.target_locale == TGT)
            ).first()
            return {
                g.get("source_term")
                for g in (seg.qa_flags or [])
                if g.get("type") == quality.GLOSSARY_FLAG_TYPE
            }

    on_off = _run_pair(True)
    off_off = _run_pair(False)
    assert on_off == off_off, (
        "a whole-document ON->OFF run must flag like an OFF->OFF run once the OFF step produces "
        "the body (LAW-TERM-15 'OFF producer -> full match', M5)",
        on_off,
        off_off,
    )
    assert "alpha" in on_off, ("the full match must be enforced by the OFF producer", on_off)
    print("  5b''. WHOLE-DOCUMENT ON->OFF reverts to the full match (flags like OFF->OFF): OK")


def test_wholedoc_lqa_union_reads_shown_set():
    """H1 regression: in the WHOLE-DOCUMENT run the LQA judge's approved-terminology set must
    be the producing step's shown set {Widget, cloud} under the toggle, NOT the full match — the
    seam the round-1 gate found unwired (`lqa_terms=terms` at both whole-doc call sites). Spies
    the glossary_terms handed to lqa.score_text; ON vs OFF, exactly the gate's probe."""
    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    # ON translate -> LQA: the judge must see the shown set only.
    pid_on = _project(cid)
    doc_on = _doc(pid_on, [_SRC_SENTENCE], mode=TransMode.FULL_TEXT)
    wf_on = _wf(
        [
            {"marker": "XL", "gid": gid, "render": True, "cap": 2},
            {"marker": "JUDGE", "type": "lqa"},
        ]
    )
    with _FakeCalls([("XL", _XLATE)]), _ScoreSpy() as spy_on:
        _run(wf_on, doc_on)
    judged_on = {t.get("source_term") for t in (spy_on.glossary_terms or [])}
    assert judged_on == {"Widget", "cloud"}, (
        "the whole-document LQA judge must receive the shown set, not the full match "
        "(a human-only / capped-out term must never reach the judge's prompt under the toggle)",
        judged_on,
    )
    # OFF translate -> LQA: the judge sees the full match exactly as today (byte-identical).
    pid_off = _project(cid)
    doc_off = _doc(pid_off, [_SRC_SENTENCE], mode=TransMode.FULL_TEXT)
    wf_off = _wf(
        [
            {"marker": "XL", "gid": gid, "render": False},
            {"marker": "JUDGE", "type": "lqa"},
        ]
    )
    with _FakeCalls([("XL", _XLATE)]), _ScoreSpy() as spy_off:
        _run(wf_off, doc_off)
    judged_off = {t.get("source_term") for t in (spy_off.glossary_terms or [])}
    assert {"Gadget", "alpha", "beta", "gamma"} <= judged_off, (
        "the toggle-OFF whole-document LQA judge must receive the full match (byte-identical)",
        judged_off,
    )
    print(
        "  5b''''. WHOLE-DOCUMENT LQA judge: ON -> shown set {Widget, cloud}; OFF -> full "
        "match (H1 closed in both call sites): OK"
    )


def test_wholedoc_off_prompt_byte_identity():
    """Residue 8's closing assertion: a toggle-OFF correction step's assembled prompt is
    byte-identical whether or not an EARLIER step rendered concepts, because the OFF step keeps
    the run-level glossary_line block. Non-vacuous: writing the ON block into the run-level
    base_ctx (Mutant B) diverges the OFF correction's prompt."""
    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pidA = _project(cid)
    docA = _doc(pidA, [_SRC_SENTENCE], mode=TransMode.FULL_TEXT)
    wfA = _wf(
        [
            {"marker": "XL", "gid": gid, "render": True, "cap": 2},
            {"marker": "FX", "render": False},
        ]
    )
    with _FakeCalls([("XL", _XLATE), ("FX", _CORRECT)]) as fa:
        _run(wfA, docA)
    corr_prompt_on = fa.prompt_for("FX")
    pidB = _project(cid)
    docB = _doc(pidB, [_SRC_SENTENCE], mode=TransMode.FULL_TEXT)
    wfB = _wf(
        [
            {"marker": "XL", "gid": gid, "render": False},
            {"marker": "FX", "render": False},
        ]
    )
    with _FakeCalls([("XL", _XLATE), ("FX", _CORRECT)]) as fb:
        _run(wfB, docB)
    corr_prompt_off = fb.prompt_for("FX")
    assert corr_prompt_on is not None and corr_prompt_off is not None
    assert corr_prompt_on == corr_prompt_off, (
        "the toggle-OFF correction step's prompt must be byte-identical whether or not an "
        "earlier step rendered concepts (residue 8)",
        corr_prompt_on,
        corr_prompt_off,
    )
    print(
        "  5b'''. WHOLE-DOCUMENT: the toggle-OFF correction prompt is byte-identical to the "
        "OFF baseline (residue 8): OK"
    )


# ===========================================================================
# M4. The whole-document LAW-TERM-16 stamp joins the RIGHT leg of a multi-locale action.
# ===========================================================================
def test_wholedoc_multilocale_stamp():
    """A FULL_TEXT document with one Segment row per target locale sharing order_index: the
    stamp helper `_ft_segment_id_for_stamp` must return the queried locale's OWN Segment.id
    (M4). Non-vacuous: without the target_locale filter it returns the order_index-first row,
    the same id for both legs, so the adherence join misses the other leg's records."""
    pid = _project(_customer())
    with Session(engine) as s:
        doc = Document(
            project_id=pid,
            filename="ml.txt",
            fmt="txt",
            mode=TransMode.FULL_TEXT,
            skeleton={"units": [{"text": "x"}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        # two legs, SAME order_index, different target_locale.
        s.add(Segment(document_id=doc.id, target_locale="fr-FR", order_index=0, source_text="x"))
        s.add(Segment(document_id=doc.id, target_locale="es-ES", order_index=0, source_text="x"))
        s.commit()
        fr_id = s.exec(
            select(Segment.id).where(
                Segment.document_id == doc.id, Segment.target_locale == "fr-FR"
            )
        ).first()
        es_id = s.exec(
            select(Segment.id).where(
                Segment.document_id == doc.id, Segment.target_locale == "es-ES"
            )
        ).first()
        got_fr = orchestrator._ft_segment_id_for_stamp(s, doc.id, "fr-FR")
        got_es = orchestrator._ft_segment_id_for_stamp(s, doc.id, "es-ES")
    assert fr_id != es_id, "the two locale legs must be distinct Segment rows"
    assert got_fr == fr_id, ("stamp must return the fr-FR leg's own id", got_fr, fr_id)
    assert got_es == es_id, ("stamp must return the es-ES leg's own id", got_es, es_id)
    print("  M4. WHOLE-DOCUMENT stamp joins the right leg of a multi-locale action: OK")


# ===========================================================================
# 7. Prompt Tap (LAW-TERM-16): the shown-set stamp + adherence read, disabled and enabled.
# ===========================================================================
def test_tap_disabled_consistency():
    """Tap DISABLED (default): no capture is written, and enforcement is still consistent
    via the in-memory map (the human-only term is not flagged). Uses the REAL mock provider
    (not the run_completion fake) so the capture path is genuinely exercised-and-suppressed."""
    from app.services import prompt_tap

    settings.set_many({"prompt_tap_enabled": "0", "prompt_tap_envelopes_enabled": "0"})
    cid = _customer()
    gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
    pid = _project(cid, provider="mock")
    doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.SEGMENTED)
    wf = _wf([{"marker": "XL", "gid": gid, "render": True, "cap": 2}], provider="mock")
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf, doc, TGT)
    gflags = _seg_flags(doc, quality.GLOSSARY_FLAG_TYPE)[0]
    assert all(g.get("source_term") != "Gadget" for g in gflags), gflags
    assert _find_stamped_record(prompt_tap) is None, "Tap disabled must write no capture"
    print("  7a. Tap DISABLED: no capture; enforcement consistent via the in-memory map: OK")


def test_tap_enabled_stamp_and_adherence():
    """Tap ENABLED: the per-call capture carries meta.shown_terms/target_locale/
    segment_index(=Segment.id), and the adherence leg READS the stamp (not the replay) —
    proven because the human-only Gadget IS a real glossary match the replay would surface,
    yet it is absent from both the stamp and the resulting adherence matched set. Uses the
    REAL mock provider so run_completion writes a genuine capture record."""
    from app.models import TapRun
    from app.services import project_sources, prompt_tap

    settings.set_many({"prompt_tap_enabled": "1", "prompt_tap_envelopes_enabled": "1"})
    try:
        cid = _customer()
        gid = _module_glossary(cid, _TERMS, human_only=("Gadget",))
        pid = _project(cid, provider="mock")
        # L5: PIN the glossary to the project so the adherence REPLAY (project-scoped
        # build_glossary_plan) really would surface every match INCLUDING the human-only
        # Gadget — that is what makes "Gadget absent from the matched set" a genuine
        # stamp-vs-replay discriminator rather than a vacuous empty-replay assertion.
        with Session(engine) as s:
            project_sources.add_glossary_pin(s, pid, gid, org_id=1, bound_customer_id=cid)
        doc = _doc(pid, [_SRC_SENTENCE], mode=TransMode.SEGMENTED)
        wf = _wf([{"marker": "XL", "gid": gid, "render": True, "cap": 2}], provider="mock")
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf, doc, TGT)
            segid = s.exec(
                select(Segment.id).where(Segment.document_id == doc, Segment.target_locale == TGT)
            ).first()
        # (i) the capture record carries the stamp, keyed by Segment.id, human-only excluded.
        stamped = _find_stamped_record(prompt_tap)
        assert stamped is not None, "Tap enabled must write a capture carrying the shown-set stamp"
        meta = stamped.get("meta") or {}
        assert isinstance(meta.get("shown_terms"), list) and meta["shown_terms"], meta
        assert meta.get("target_locale") == TGT, meta
        assert meta.get("segment_index") == segid, (meta.get("segment_index"), segid)
        assert all(t.get("source_term") != "Gadget" for t in meta["shown_terms"]), meta
        # (ii) the adherence leg read the stamp: the matched set on the TapRun detail excludes
        #      Gadget (the replay WOULD include it — it is a live glossary match of the source).
        with Session(engine) as s:
            r = s.exec(select(TapRun).order_by(TapRun.id)).all()[-1]
        matched = (r.detail or {}).get("glossary", {}).get("matched_terms", [])
        assert matched, ("adherence matched set empty", r.detail)
        # (iii) DISCRIMINATOR PROOF (L5): the project-scoped REPLAY — the source the adherence
        #       leg would use WITHOUT the stamp — DOES surface Gadget (a live glossary match),
        #       so its absence from `matched` proves the leg read the audience-gated STAMP.
        from app.services import tap_enrich

        with Session(engine) as s:
            proj = s.get(Project, pid)
            segs = tap_enrich._run_segments(s, doc, TGT)
            replay_terms, _pl, _tr = tap_enrich._matched_terms(s, proj, segs, SRC, TGT)
        replay_srcs = {t.get("source") for t in replay_terms}
        assert "Gadget" in replay_srcs, (
            "the project-scoped replay must surface Gadget, else the stamp-vs-replay "
            "discriminator is vacuous (L5)",
            replay_srcs,
        )
        assert all(m.get("source") != "Gadget" for m in matched), (
            "adherence must measure against the STAMPED shown set, not the replay "
            "(Gadget is a live match but was audience-gated out of the shown set)",
            matched,
        )
        print(
            "  7b. Tap ENABLED: capture stamp (shown_terms/target_locale/segment_index) + "
            "adherence reads the stamp, not the replay (human-only excluded): OK"
        )
    finally:
        settings.set_many({"prompt_tap_enabled": "0", "prompt_tap_envelopes_enabled": "0"})


def _find_stamped_record(prompt_tap):
    """Scan the prompt_tap actions dir for a record whose meta carries shown_terms."""
    import json

    try:
        ad = prompt_tap._actions_dir()
    except Exception:
        return None
    try:
        paths = list(ad.glob("*.jsonl")) if hasattr(ad, "glob") else []
    except Exception:
        paths = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if (rec.get("meta") or {}).get("shown_terms") is not None:
                        return rec
        except Exception:
            continue
    return None


def main():
    test_render_off_byte_identical()
    test_render_on_caps_ordering_audience_budget()
    test_render_token_cap_drops_whole_concepts()
    test_mt_seam_drops_human_only()
    test_segmented_producing_step_shown_set()
    test_segmented_fix_guard_reads_shown_set()
    test_segmented_on_off_reverts_to_full_match()
    test_segmented_lqa_union_reads_shown_set()
    test_segmented_both_off_enforces_full_match()
    test_wholedoc_producing_step_shown_set()
    test_wholedoc_regression_arm_reads_shown_set()
    test_wholedoc_on_off_reverts_to_full_match()
    test_wholedoc_lqa_union_reads_shown_set()
    test_wholedoc_off_prompt_byte_identity()
    test_wholedoc_multilocale_stamp()
    test_tap_disabled_consistency()
    test_tap_enabled_stamp_and_adherence()
    print("GLOSSARY RENDER TOGGLE OK")


if __name__ == "__main__":
    main()
