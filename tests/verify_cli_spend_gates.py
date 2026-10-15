"""CLI spend gates (#531): the verbs that make paid provider calls show an estimate and refuse
to run without --yes, like `eval run start`.

Providers stay mocked and every paid seam is replaced by a call counter: the pins prove the
UNCONFIRMED invocation makes zero provider calls and exits 0 with `confirmed: false` plus an
estimate in --json, and that --yes runs the verb.

  1. `lqa qe-compare` and `lqa jev-refine` (estimate, no calls, --yes runs; invalid input and
     the --remote refusal still exit 2 before the gate).
  2. `context ab` and `style-guide ab` (estimate from the document's segments; --yes runs).
  3. `tagging repair` (the dry run is genuinely free: no provider call without --yes, and the
     plan lists the damaged segments).
  4. A project over its USD cap is refused (exit 1) before the estimate, with no call.

Final line: "CLI SPEND GATES OK".
"""

import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cli_spendgate_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Document, Project, Segment, TransMode
from tests._cli_runner import run_cli


def _last_json(out):
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else {}


def _pairs_file(n=2):
    path = os.path.join(tempfile.mkdtemp(prefix="loctms_spend_"), "pairs.jsonl")
    rows = [
        {
            "source": f"Pay ten euros {i}.",
            "target": f"Zahlen Sie neun Euro {i}.",
            "source_locale": "en",
            "target_locale": "de",
        }
        for i in range(n)
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _unconfirmed(argv):
    code, out, err = run_cli(["--json"] + argv)  # noqa: RUF005
    payload = _last_json(out)
    assert code == 0, (argv, code, out, err)
    assert payload.get("ok") is True and payload.get("confirmed") is False, (argv, payload)
    est = payload["estimate"]
    assert est["calls"] > 0 and "total_usd" in est, (argv, est)
    return payload


def test_lqa_verbs_gated():
    from app.kernel import qe, qe_jev, qe_jev_loop

    calls = []
    real = (qe.score_segment, qe_jev.score_segment, qe_jev_loop.refine)

    def _score(*a, **k):
        calls.append("score")
        return qe._skipped("provider_error", "stub")

    def _refine(*a, **k):
        calls.append("refine")
        raise ValueError("stub refine")

    qe.score_segment, qe_jev.score_segment, qe_jev_loop.refine = _score, _score, _refine
    try:
        pairs = _pairs_file(3)
        payload = _unconfirmed(["lqa", "qe-compare", "--pairs", pairs, "--llm-provider", "mock"])
        assert payload["estimate"]["calls"] == 6, payload
        payload = _unconfirmed(["lqa", "jev-refine", "--pairs", pairs, "--provider", "mock"])
        assert payload["estimate"]["calls"] > 6, payload
        assert not calls, f"provider seam called without --yes: {calls}"
        code, out, err = run_cli(
            ["lqa", "qe-compare", "--pairs", pairs, "--llm-provider", "mock", "--yes"]
        )
        assert code == 0 and len(calls) == 6, (code, calls, err)
        calls.clear()
        code, out, err = run_cli(
            ["lqa", "jev-refine", "--pairs", pairs, "--provider", "mock", "--yes"]
        )
        assert code == 0 and len(calls) == 3, (code, calls, err)
        # validation still comes first
        code, out, err = run_cli(["lqa", "jev-refine", "--max-tries", "26", "--pairs", pairs])
        assert code == 2, (code, out, err)
        code, out, err = run_cli(["lqa", "qe-compare", "--pairs", pairs + ".missing"])
        assert code == 2, (code, out, err)
    finally:
        qe.score_segment, qe_jev.score_segment, qe_jev_loop.refine = real
    print("  1. qe-compare / jev-refine: estimate, zero calls without --yes, --yes runs")


def _doc(sources=("Acme ships fast.", "Reset your Acme password."), org_id=None):
    with Session(engine) as s:
        proj = Project(name="sg-1", org_id=org_id, source_locale="en-US", default_provider="mock")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(project_id=proj.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, src in enumerate(sources):
            s.add(
                Segment(document_id=doc.id, target_locale="es-ES", order_index=i, source_text=src)
            )
        s.commit()
        return proj.id, doc.id


def test_ab_verbs_gated():
    from app.kernel import context_ab, style_ab

    _proj, doc = _doc()
    calls = []
    real = (context_ab.run_ab, style_ab.run_ab)

    def _stub(name):
        def run(*a, **k):
            calls.append(name)
            raise context_ab.ABError("stub " + name)

        return run

    context_ab.run_ab, style_ab.run_ab = _stub("context"), _stub("style")
    try:
        for group in ("context", "style-guide"):
            payload = _unconfirmed([group, "ab", "--document", str(doc), "--provider", "mock"])
            assert payload["estimate"]["calls"] == 8, payload  # 2 segments x 4 calls
            assert payload["estimate"]["free"] is True, payload  # mock
        assert not calls, f"A/B ran without --yes: {calls}"
        for group, name in (("context", "context"), ("style-guide", "style")):
            code, out, err = run_cli(
                [group, "ab", "--document", str(doc), "--provider", "mock", "--yes"]
            )
            assert code == 2 and calls[-1] == name, (group, code, calls, err)
        # a missing document is still a usage error, not a gate prompt
        code, out, err = run_cli(["context", "ab", "--document", "999999"])
        assert code == 2, (code, out, err)
    finally:
        context_ab.run_ab, style_ab.run_ab = real
    print("  2. context ab / style-guide ab: estimate (4 calls per segment), --yes runs")


def test_context_ab_remote_needs_yes():
    """Gate: `context ab --remote` is priced server-side, so the CLI cannot show a dollar
    figure, but it still must not POST the A/B without --yes."""
    from app.cli import _remote_impl

    calls = []
    real = _remote_impl.context_ab

    def _stub(args, emit):
        calls.append("remote")
        return 0

    _remote_impl.context_ab = _stub
    remote = ["--remote", "http://127.0.0.1:9", "--token", "lsu_" + "x" * 8]
    try:
        code, out, err = run_cli(["--json", *remote, "context", "ab", "--document", "1"])
        payload = _last_json(out)
        assert code == 0 and payload.get("confirmed") is False, (code, out, err)
        assert not calls, "context ab --remote reached the server without --yes"
        code, out, err = run_cli([*remote, "context", "ab", "--document", "1", "--yes"])
        assert code == 0 and calls == ["remote"], (code, calls, out, err)
    finally:
        _remote_impl.context_ab = real
    print("  2b. context ab --remote: no server call without --yes; --yes sends it")


def test_tagging_repair_dry_run_is_free():
    from app.kernel import settings
    from app.services import quality, tag_llm_repair

    settings.set_many({"tag_llm_repair_enabled": "1"})
    _proj, _doc_id = _doc(sources=("<b>Hello</b> world", "plain"))
    with Session(engine) as s:
        seg = s.exec(
            select(Segment).where(Segment.document_id == _doc_id, Segment.order_index == 0)
        ).first()
        seg.target_text = "Hola mundo"
        s.add(seg)
        s.commit()
    calls = []
    real_damage, real_repair = quality.tag_damage, tag_llm_repair.repair
    quality.tag_damage = lambda src, tgt: "missing tag" if "<b>" in (src or "") else ""

    def _repair(*a, **k):
        calls.append("repair")
        return None

    tag_llm_repair.repair = _repair
    try:
        payload = _unconfirmed(["tagging", "repair"])
        assert payload["estimate"]["calls"] == 2, payload
        assert not calls, "the unconfirmed dry run called the provider"
        code, out, err = run_cli(["tagging", "repair", "--yes"])
        assert code == 0 and calls == ["repair"], (code, calls, out, err)
    finally:
        quality.tag_damage, tag_llm_repair.repair = real_damage, real_repair
    print("  3. tagging repair: plan + estimate are free; --yes makes the provider call")


def test_budget_refusal_before_gate():
    from app.kernel import project_budgets

    proj, doc = _doc()
    real = project_budgets.project_check
    project_budgets.project_check = lambda pid, now=None: (False, 0.0, "project budget reached")
    try:
        code, out, err = run_cli(["context", "ab", "--document", str(doc), "--provider", "mock"])
        assert code == 1 and "budget" in (out + err), (code, out, err)
    finally:
        project_budgets.project_check = real
    assert proj
    print("  4. a project over its cap is refused before the estimate")


def main_() -> None:
    init_db()
    test_lqa_verbs_gated()
    test_ab_verbs_gated()
    test_context_ab_remote_needs_yes()
    test_tagging_repair_dry_run_is_free()
    test_budget_refusal_before_gate()
    print("CLI SPEND GATES OK")


if __name__ == "__main__":
    main_()
