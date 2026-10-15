"""B0 PACKAGE / VERIFY view verification (SPEC-workflows-redesign Track B, B0).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
ZERO live calls. Proves the "package view" — what each step packages for the model and
what is verified on the return — is legible and HONEST across all three surfaces from ONE
source (app/services/package_view.py):

  service facts (package_view.workflow_package_view / step_facts)
    - a PINNED glossary shows "pinned: <name>" (definition-certain, no scope tag);
    - an UNPINNED glossary + context + TM few-shot render as resolved PER DOCUMENT;
    - a QE gate surfaces its threshold; a correction loop's branch + max_visits show;
    - a style-guide-resolvable customer -> a per-document style-guide PACKAGE fact on
      translate steps AND a per-document style-adherence VERIFY fact on check steps;
    - OMISSION: with no guide in scope, no style facts; TM/PII/adaptive off -> absent.

  honesty contract
    - every per-document / per-language-pair fact carries its scope tag; nothing that is
      OFF is ever rendered as "no ...".

  CLI --json object shape + human block (workflow show)
    - each step row carries `package` + `verify` fact lists ({key,label,value,scope});
    - the human block prints the honest "per document" / "per language pair" wording.

  /api mirrors the CLI --json step-row shape EXACTLY (row-shape parity).

  web builder panel renders the facts (pinned name, per-document wording).

  graph byte-equivalence: the wf_graph SVG is UNCHANGED by B0 — a style guide being
    available in scope alters the PANEL but never the graph (B2's per-document decision:
    no style chip on nodes), so the rendered <svg> is identical with/without a guide.

Final line: "PACKAGE VIEW OK".
"""

import io
import json as _json
import os
import re
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pkgview_")

from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Customer,
    Glossary,
    GlossaryStatus,
    Organization,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.services import apikeys, security, settings
from app.services import package_view as pv
from app.services import style_guides as sg

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _facts_by_key(facts):
    return {f["key"]: f for f in facts}


# ---------------------------------------------------------------------------
# 1. Service facts — the fixture workflow (glossary pin + qe_gate + correction
#    loop + a style-guide-resolvable customer).
# ---------------------------------------------------------------------------
def _seed_workflow(*, with_guide, with_context=False):
    """A customer-scoped correction-loop workflow: Translate(pinned glossary, QE gate)
    -> Fix -> LQA(loops back to Fix). When `with_guide`, an APPROVED customer style guide
    is created so the customer scope resolves one. When `with_context` (QA-8 #38), a
    customer-scoped ContextProfile is created so the customer scope resolves a context (the
    Context PACKAGE fact is now gated on scope availability, exactly like the style-guide
    fact); without it, a bare install resolves no context and the fact is OMITTED."""
    with Session(engine) as s:
        if s.get(Organization, 1) is None:
            s.add(Organization(id=1, name="PV Org"))
            s.commit()
        cust = Customer(name="PV Cust", org_id=1)
        s.add(cust)
        s.commit()
        s.refresh(cust)
        g = Glossary(
            name="Legal Terms", status=GlossaryStatus.APPROVED, org_id=1, customer_id=cust.id
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        wf = Workflow(name="PV WF", scope="customer", customer_id=cust.id, org_id=1)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="Translate",
                step_type=StepType.LLM_AGENT,
                provider="openai",
                model="gpt-4o",
                fallback_provider="anthropic",
                glossary_id=g.id,
                qe_gate=True,
                qe_threshold=85,
                redact_pii=True,
                adaptive_feedback=True,
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=1,
                name="Fix",
                step_type=StepType.LLM_AGENT,
                key="fix",
                tm_examples=False,
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=2,
                name="LQA",
                step_type=StepType.LQA,
                key="lqa",
                branch={"on_pass": "end", "on_fail": {"step": "fix"}},
                max_visits=3,
            )
        )
        s.commit()
        if with_guide:
            guide = sg.create(
                s,
                "Formal Voice",
                org_id=1,
                customer_id=cust.id,
                body="Use the formal register throughout.",
            )
            sg.approve(s, guide.id, "pv@x.com")
        if with_context:
            from app.services import context_pkg

            context_pkg.upsert_profile(
                s,
                org_id=1,
                customer_id=cust.id,
                name="Brand Tone",
                context_raw={"formality": "formal", "tone": ["professional"]},
                actor="pv@x.com",
            )
        return wf.id


def test_service_facts_pinned_glossary_qe_loop_and_style_guide():
    wf_id = _seed_workflow(with_guide=True, with_context=True)
    with Session(engine) as s:
        from app.services import orchestrator

        wf = s.get(Workflow, wf_id)
        steps = orchestrator.get_steps(s, wf_id)
        views = pv.workflow_package_view(s, wf, steps)
    assert [v["role"] for v in views] == ["Translate", "Translate", "Check"], views

    tr = views[0]
    pkg = _facts_by_key(tr["package"])
    vf = _facts_by_key(tr["verify"])
    # model line: provider/model + fallback, definition-certain (no scope tag)
    assert pkg["model"]["value"] == "openai / gpt-4o; fallback: anthropic", pkg["model"]
    assert pkg["model"]["scope"] == "", pkg["model"]
    # PINNED glossary -> exact name, definition-certain
    assert pkg["glossary"]["value"] == "pinned: Legal Terms", pkg["glossary"]
    assert pkg["glossary"]["scope"] == "", pkg["glossary"]
    # per-document assets carry the honest scope tag (context resolves because a
    # ContextProfile was seeded in scope -- QA-8 #38 gates this fact on availability)
    assert pkg["context"]["scope"] == pv.PER_DOCUMENT
    assert pkg["tm"]["scope"] == pv.PER_DOCUMENT
    assert pkg["style_guide"]["scope"] == pv.PER_DOCUMENT, "guide resolves per document"
    assert "pii" in pkg and "adaptive_feedback" in pkg
    # VERIFY: refusal always; DNT + adherence per document; QE gate quotes threshold
    assert vf["refusal"]["scope"] == ""
    assert vf["dnt"]["scope"] == pv.PER_DOCUMENT
    assert vf["glossary_adherence"]["scope"] == pv.PER_DOCUMENT
    assert "85/100" in vf["qe_gate"]["value"] and "%" not in vf["qe_gate"]["value"], vf["qe_gate"]
    assert "judge openai" in vf["qe_gate"]["value"] and "auto-fix off" in vf["qe_gate"]["value"]

    # Fix step: UNPINNED glossary -> auto-attach per document; TM OFF -> absent
    fix = views[1]
    fpkg = _facts_by_key(fix["package"])
    assert fpkg["glossary"]["scope"] == pv.PER_DOCUMENT, "unpinned glossary is per-document"
    assert "auto-attach" in fpkg["glossary"]["value"], fpkg["glossary"]
    assert "tm" not in fpkg, "TM few-shot OFF must be omitted, not rendered as 'no TM'"
    assert "pii" not in fpkg and "adaptive_feedback" not in fpkg, "off toggles omitted"

    # LQA step: correction-loop routing facts + rulebook per-pair + style adherence
    lqa = views[2]
    lpkg = _facts_by_key(lqa["package"])
    lvf = _facts_by_key(lqa["verify"])
    assert lpkg["model"]["label"] == "Judge model", lpkg["model"]
    assert lvf["rulebook"]["scope"] == pv.PER_PAIR, "rulebook resolves per language pair"
    assert lvf["style_adherence"]["scope"] == pv.PER_DOCUMENT
    assert "branch" in lvf and "->" in lvf["branch"]["value"], lvf.get("branch")
    assert lvf["max_visits"]["value"].startswith("3"), lvf.get("max_visits")
    # glossary/TM/context/DNT do NOT apply to a check step
    assert "glossary" not in lpkg and "dnt" not in lvf and "context" not in lpkg
    print(
        "  (1) service facts: pinned glossary, per-doc context/TM/guide, QE 85/100, "
        "per-pair rulebook, loop routing, honest omission of off toggles"
    )


def test_service_omits_style_facts_without_guide():
    wf_id = _seed_workflow(with_guide=False)
    with Session(engine) as s:
        from app.services import orchestrator

        wf = s.get(Workflow, wf_id)
        steps = orchestrator.get_steps(s, wf_id)
        assert pv.workflow_has_guide(s, wf) is False
        views = pv.workflow_package_view(s, wf, steps)
    tr_pkg = _facts_by_key(views[0]["package"])
    lqa_vf = _facts_by_key(views[2]["verify"])
    assert "style_guide" not in tr_pkg, "no approved guide in scope -> no style package fact"
    assert "style_adherence" not in lqa_vf, "no guide -> no style-adherence verify fact"
    print("  (2) no approved guide in scope -> style facts OMITTED on both step types")


def test_service_omits_context_without_profile():
    """QA-8 (#38): the Context PACKAGE fact is gated on scope availability the same way
    the style-guide fact is. A bare install (no ContextProfile) OMITS it (pre-fix it was
    emitted UNCONDITIONALLY, over-claiming a resolvable context on every install); an
    install with a ContextProfile in scope shows it, per document."""
    from app.services import orchestrator

    # (a) no ContextProfile in scope -> the translate step OMITS the context fact.
    bare_id = _seed_workflow(with_guide=False, with_context=False)
    with Session(engine) as s:
        wf = s.get(Workflow, bare_id)
        assert pv.workflow_has_context(s, wf) is False, "bare install resolves no context"
        views = pv.workflow_package_view(s, wf, orchestrator.get_steps(s, bare_id))
    tr_pkg = _facts_by_key(views[0]["package"])
    assert "context" not in tr_pkg, (
        "no ContextProfile in scope -> the Context fact must be OMITTED, not "
        "'resolved' (QA-8: the over-claim on a brand-new install)"
    )
    # (b) a ContextProfile in scope -> the fact shows, per document.
    ctx_id = _seed_workflow(with_guide=False, with_context=True)
    with Session(engine) as s:
        wf = s.get(Workflow, ctx_id)
        assert pv.workflow_has_context(s, wf) is True, "a seeded profile resolves in scope"
        views = pv.workflow_package_view(s, wf, orchestrator.get_steps(s, ctx_id))
    tr_pkg = _facts_by_key(views[0]["package"])
    assert "context" in tr_pkg and tr_pkg["context"]["scope"] == pv.PER_DOCUMENT, (
        "a resolvable ContextProfile -> the Context fact shows, per document",
        tr_pkg,
    )
    print("  (2b) QA-8: Context fact OMITTED with no profile, shown (per document) with one")


def test_human_review_and_defensive_dict_input():
    # human_review: no model call; assignee shown when set. Also proves the service
    # accepts plain draft dicts (not just ORM rows) and never raises.
    hr = {"step_type": StepType.HUMAN_REVIEW, "name": "Sign-off", "assignee_email": "rev@x.com"}
    f = pv.step_facts(hr)
    keys = {x["key"] for x in f["package"]}
    assert "review" in keys and "assignee" in keys, f["package"]
    assert f["verify"][0]["key"] == "human"
    # a bare dict missing most fields still classifies (llm_agent default) without raising
    minimal = pv.step_facts({"step_type": StepType.LLM_AGENT})
    assert _facts_by_key(minimal["package"])["model"]["value"] == "default"
    print("  (3) human_review facts + defensive dict input (never raises)")


# ---------------------------------------------------------------------------
# 2. CLI --json object shape + human block.
# ---------------------------------------------------------------------------
def _cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(list(argv))
    return rc, buf.getvalue()


_AGENT_STEP = {"step_type": "llm_agent", "name": "Translate"}


def test_cli_json_object_shape_and_human_block():
    wf_id = _seed_workflow(with_guide=True)
    rc, out = _cli("--json", "workflow", "show", str(wf_id))
    assert rc == 0, out
    doc = _json.loads(out)
    steps = doc["steps"]
    assert len(steps) == 3, steps
    for st in steps:
        assert "package" in st and "verify" in st, ("B0 keys missing", st.keys())
        for fact in st["package"] + st["verify"]:
            assert set(fact.keys()) == {"key", "label", "value", "scope"}, fact
    tr = steps[0]
    tr_pkg = _facts_by_key(tr["package"])
    assert tr_pkg["glossary"]["value"] == "pinned: Legal Terms", tr_pkg["glossary"]

    # human block: honest per-document / per-language-pair wording present
    rc, human = _cli("workflow", "show", str(wf_id))
    assert rc == 0, human
    assert "Per-step package & verification:" in human, human
    assert "pinned: Legal Terms" in human, human
    assert "(per document)" in human, human
    assert "(per language pair)" in human, human
    assert "PACKAGE" in human and "VERIFY" in human, human
    # a '-' filler never appears inside the package block (omission, not "no X")
    block = human.split("Per-step package & verification:", 1)[1]
    assert not re.search(r"\bno TM\b|\bnone\b", block), block
    print("  (4) CLI --json per-step package/verify object shape + honest human block")


# ---------------------------------------------------------------------------
# 3. /api mirrors the CLI --json step-row shape EXACTLY (row-shape parity).
# ---------------------------------------------------------------------------
def test_api_mirrors_cli_json_rowshape():
    wf_id = _seed_workflow(with_guide=True)
    rc, out = _cli("--json", "workflow", "show", str(wf_id))
    assert rc == 0, out
    local = _json.loads(out)

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        u = security.create_user("pvapi@x.com", "PV", "admin", "pvpass123456")
        _row, tok = apikeys.create_key(u.id, "pv", scopes="api docs")
        c = TestClient(app)
        r = c.get(f"/api/workflows/{wf_id}", headers={"X-Loc-Token": tok})
        assert r.status_code == 200, r.text
        api = r.json()
    finally:
        settings.set_many({"security_auth_required": "0"})

    assert len(local["steps"]) == len(api["steps"])
    for ls, as_ in zip(local["steps"], api["steps"], strict=False):
        assert set(ls.keys()) == set(as_.keys()), (
            "CLI/api step row key mismatch",
            set(ls.keys()) ^ set(as_.keys()),
        )
        assert ls["package"] == as_["package"], "package facts must be identical"
        assert ls["verify"] == as_["verify"], "verify facts must be identical"
    print("  (5) /api workflow show mirrors CLI --json step-row shape + facts EXACTLY")


# ---------------------------------------------------------------------------
# 4. Web builder panel renders the facts.
# ---------------------------------------------------------------------------
def test_web_panel_renders_facts():
    wf_id = _seed_workflow(with_guide=True)
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.get(f"/workflows/{wf_id}")
    assert r.status_code == 200, r.status_code
    h = r.text
    for needle in (
        "Package &amp; verification",
        "wf-pkg-cols",
        "sent to the model",
        "checked on the return",
        "pinned: Legal Terms",
        "per document",
        "per language pair",
        "route below 85/100",
    ):
        assert needle in h, f"panel missing: {needle}"
    print("  (6) web builder panel renders package/verify facts (pinned name, per-doc)")


# ---------------------------------------------------------------------------
# 5. Graph byte-equivalence: B0 changes the PANEL, never the wf_graph SVG.
# ---------------------------------------------------------------------------
# SPEC-workflow-canvas Phase 1: the BUILDER's graph is now editable, so each node
# carries its OWN step id (the drag POSTs it to /steps/{id}/move) and each grip carries
# it again. This check compares two SEPARATELY SEEDED workflows, whose rows therefore
# hold different primary keys BY CONSTRUCTION -- a difference that has nothing to do with
# what this check is about. The ids are blanked before comparing and EVERYTHING else is
# still compared byte for byte; a style-guide leak would surface as a chip, a label or a
# geometry change, never as a primary key. The node COUNT is asserted equal separately,
# so a structural difference cannot hide behind the normalisation.
_STEP_ID_ATTR = re.compile(r'(data-step-id|data-grip)="\d+"')


def _svg_of(html):
    m = re.search(r"<svg\b.*?</svg>", html, re.DOTALL)
    return m.group(0) if m else None


def _svg_norm(svg):
    return _STEP_ID_ATTR.sub(r'\1="#"', svg)


def test_graph_unchanged_by_b0():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    wf_with = _seed_workflow(with_guide=True)
    wf_without = _seed_workflow(with_guide=False)
    svg_with = _svg_of(c.get(f"/workflows/{wf_with}").text)
    svg_without = _svg_of(c.get(f"/workflows/{wf_without}").text)
    assert svg_with and svg_without, "wf_graph SVG must render for both"
    # Same number of identity-bearing nodes -> the shapes really are comparable.
    n_with = len(_STEP_ID_ATTR.findall(svg_with))
    n_without = len(_STEP_ID_ATTR.findall(svg_without))
    assert n_with == n_without, (
        "the two graphs carry different numbers of step ids (%d != %d) -- the topologies "  # noqa: UP031
        "diverged, so the comparison below would be meaningless" % (n_with, n_without)
    )
    # identical topology (same steps) -> byte-identical SVG regardless of guide
    # availability: the guide is a PER-DOCUMENT panel fact, never a graph node chip.
    assert _svg_norm(svg_with) == _svg_norm(svg_without), (
        "a style guide in scope must NOT alter the graph"
    )
    # the package-panel detail never leaks INTO the graph (graph stays glanceable)
    assert "wf-pkg" not in svg_with and "per document" not in svg_with, svg_with[:200]
    print(
        "  (7) wf_graph SVG byte-identical with/without a guide (step ids aside; %d "  # noqa: UP031
        "nodes each); no panel leakage" % n_with
    )


def test_153_terminology_verify_only_promised_when_a_check_exists():
    """#153: the DNT / glossary-adherence VERIFY facts may only appear when the WORKFLOW
    contains an lqa step.

    `tap_enrich._adherence` is explicit -- "No LQA step in the run -> every matched term is
    `not_checked` (honest: nothing judged it)" -- so on a workflow with no check these two
    lines promised a protection that could not fire. Found on prod: a one-step workflow
    (#54) advertised both, and a glossary's required target term was then missed twice on a
    run that finished green with no warning (#58 / run #200).
    """
    from app.services.package_view import step_facts

    no_check = [f["label"] for f in step_facts(_AGENT_STEP, has_check=False)["verify"]]
    assert "DNT terms" not in no_check, (
        f"#153: DNT verification must not be advertised with no check in the workflow: {no_check}"
    )
    assert "Glossary adherence" not in no_check, (
        f"#153: glossary adherence must not be advertised with no check: {no_check}"
    )
    # Saying nothing would be a quieter lie: the panel exists to tell the operator what is
    # guarding their output, so the absence is stated.
    assert any("not verified" in lbl.lower() for lbl in no_check), (
        f"#153: the panel must SAY that terminology is unverified, not go silent: {no_check}"
    )

    with_check = [f["label"] for f in step_facts(_AGENT_STEP, has_check=True)["verify"]]
    assert "DNT terms" in with_check and "Glossary adherence" in with_check, (
        f"#153: with a check present both must still be promised: {with_check}"
    )
    # The default is permissive on purpose: a caller that has not been taught the flag
    # keeps today's output rather than silently dropping facts.
    assert [f["label"] for f in step_facts(_AGENT_STEP)["verify"]] == with_check
    print("  #153: terminology verification is promised only when a check can run it OK")


def main():
    test_service_facts_pinned_glossary_qe_loop_and_style_guide()
    test_service_omits_style_facts_without_guide()
    test_service_omits_context_without_profile()
    test_human_review_and_defensive_dict_input()
    test_cli_json_object_shape_and_human_block()
    test_api_mirrors_cli_json_rowshape()
    test_web_panel_renders_facts()
    test_graph_unchanged_by_b0()
    test_153_terminology_verify_only_promised_when_a_check_exists()
    print("PACKAGE VIEW OK")


if __name__ == "__main__":
    main()
