"""Hermetic refine-loop contracts. Run through test_verify_suites.py only."""

import contextlib
import copy
import io
import json
import os
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = Path(init_env("loctms_jev_loop_"))

from app import config
from app.db import init_db
from app.services import qe, qe_jev, scaffold, spend
from app.services import qe_jev_loop as loop
from app.services.providers import Provider, ProviderSpec, register_provider


def answer(**levels):
    return 200, {
        "answers": {
            **{
                c: {"type": "score", "score": levels.get(c, 0), "confidence": 0.9}
                for c in qe.QE_CATEGORIES
            },
            "meaning_changed": {"type": "noul", "noul": 0.1},
        },
        "usage": {"input_tokens": 1000, "output_tokens": 20},
    }


class Fixer(Provider):
    name = "jevfixer"

    def __init__(self):
        self.events = []
        self.replies = iter([])

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.events.append((task, prompt))
        self.last_usage = {"input_tokens": 100, "output_tokens": 10}
        if task == "qe":
            return '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"ok"}'
        return next(self.replies)


fixer = Fixer()
SPEC = ProviderSpec(provider="jevfixer")


def run(answers, replies=("edited one", "edited two"), **kwargs):
    fixer.events = []
    fixer.replies = iter(replies)
    scripted = iter(answers)

    def post(body):
        fixer.events.append(("jev", body["state"]["translation"]))
        return next(scripted)

    with patch.object(qe_jev, "_post", side_effect=post):
        return loop.refine("Source text", "initial", "en", "de", spec=SPEC, **kwargs)


def main():
    init_db()
    scoring_train_loop_case()
    register_provider("jevfixer", fixer)
    config.TYPESAFE_API_KEY = ""
    result = run([])
    assert result["stop_reason"] == "judge_unavailable_at_baseline"
    assert result["rounds"][0]["jev"]["reason"] == "not_configured"
    assert result["totals"]["llm_calls"] == 0 and not fixer.events
    unavailable_cross = run([], cross_check=True)
    assert unavailable_cross["totals"]["llm_calls"] == 0 and not fixer.events
    config.TYPESAFE_API_KEY = "fake"  # pregate: allow-secret patched HTTP
    before = spend.spent_this_period()
    result = run([answer(accuracy=2), answer(accuracy=1, style=3), answer()])
    assert result["stop_reason"] == "target_reached", result
    assert result["best"] == result["final"] and result["best"]["round"] == 2
    assert len(result["rounds"]) == 3
    assert [e[0] for e in fixer.events] == ["jev", "jev_refine", "jev", "jev_refine", "jev"]
    assert spend.spent_this_period() - before > 3 * 1000 * qe_jev.JEV_USD_PER_INPUT_TOKEN
    for key in ("cost_usd", "tokens_in", "tokens_out", "elapsed_ms"):
        assert result["totals"][key] == sum(r[key] for r in result["rounds"])
    assert result["totals"]["jev_calls"] == 3 and result["totals"]["llm_calls"] == 2
    assert all(r["llm"] is None for r in result["rounds"])
    regression = run(
        [answer(accuracy=2), answer(accuracy=1, style=1), answer(accuracy=1, terminology=1)],
        target_score=80,
        max_tries=3,
    )
    assert regression["stop_reason"] == "max_tries"
    assert regression["best"]["score"] == 75 and regression["best"]["round"] == 1
    assert regression["final"]["score"] == 65 and regression["final"]["round"] == 2
    tied = run([answer(accuracy=2)] * 3, max_tries=3)
    assert tied["best"]["round"] == 0 and tied["final"]["round"] == 2
    baseline_only = run([answer()], max_tries=1)
    assert baseline_only["stop_reason"] == "target_reached"
    assert baseline_only["totals"]["llm_calls"] == 0
    assert run([answer(accuracy=2)] * 2, ["initial"])["stop_reason"] == "no_progress"
    assert run([answer(accuracy=2), (400, {}), (400, {})])["stop_reason"] == "judge_skipped_twice"
    for reply in (
        "",
        "I cannot assist with this request.",
        "I'm sorry, but I can't help with that.",
        {"bad": "shape"},
        "<jev_refine>",
    ):
        with patch.object(loop, "run_completion", return_value={"text": reply}):
            failed = run([answer(accuracy=2)])
        assert failed["stop_reason"] == "fixer_failed", failed
        assert failed["rounds"][-1]["error"]
    with patch.object(loop, "run_completion", side_effect=RuntimeError("broken fixer")):
        assert run([answer(accuracy=2)])["rounds"][-1]["error"] == "broken fixer"
    with patch.object(loop, "run_completion", return_value={"error": "Spend cap reached"}):
        assert run([answer(accuracy=2)])["stop_reason"] == "spend_cap"
    cross = run([answer(accuracy=2), answer()], ["fixed"], cross_check=True)
    assert cross["rounds"][0]["llm"] is None
    assert all(r["llm"]["score"] == 100 for r in cross["rounds"][1:])
    assert cross["totals"]["llm_calls"] == 2
    cross_skips = run([answer(accuracy=2), (400, {}), answer()], cross_check=True)
    assert cross_skips["rounds"][0]["llm"] is None
    assert all(row["llm"] is not None for row in cross_skips["rounds"][1:])
    assert cross_skips["rounds"][1]["delta_vs_previous"]["score"] is None
    assert cross_skips["rounds"][2]["delta_vs_previous"]["accuracy"] is None
    for key in ("cost_usd", "tokens_in", "tokens_out", "elapsed_ms"):
        assert cross_skips["totals"][key] == sum(r[key] for r in cross_skips["rounds"])
    history = copy.deepcopy(result["rounds"])
    prompt = loop.build_prompt("SOURCE", "en", "de", history, 91)
    for section in loop.SECTION_NAMES:
        assert section in prompt
    assert loop.ROLE.format(src="en", tgt="de") in prompt
    assert loop.NUMBERS in prompt and loop.TARGET.format(target_score=91) in prompt
    assert loop.INSTRUCTION.format(version=3) in prompt
    assert "SOURCE" in prompt and "at least 91" in prompt
    assert prompt.index("Version 0") < prompt.index("Version 1") < prompt.index("Version 2")
    assert "accuracy: major -> minor (+)" in prompt and "style: none -> critical (-)" in prompt
    for category in qe.QE_CATEGORIES:
        assert qe_jev.INSTRUCTIONS[category] in prompt
        assert all(signal in prompt for signal in qe_jev._SIGNALS[category])
    assert scaffold.scaffold_echo(prompt)
    assert scaffold.scaffold_echo("</jev_refine>")
    history[0]["translation"] = "OLDEST" * 1000
    history[1]["translation"] = "NEWER" * 1000
    original = copy.deepcopy(history)
    full = loop.build_prompt("SOURCE", "en", "de", history, 91)
    with patch.object(config, "JEV_REFINE_PROMPT_CAP_CHARS", len(full) - 100):
        capped = loop.build_prompt("SOURCE", "en", "de", history, 91)
        assert len(capped) <= config.JEV_REFINE_PROMPT_CAP_CHARS
        assert "OLDEST" not in capped and "NEWER" in capped
    assert history == original
    with patch.object(config, "JEV_REFINE_PROMPT_CAP_CHARS", 5500):
        capped_run = run(
            [answer(accuracy=2), answer(accuracy=2), answer()],
            ["long translation " * 1000, "done"],
        )
    assert capped_run["rounds"][-1]["history_truncated"]
    with patch.object(config, "JEV_REFINE_PROMPT_CAP_CHARS", 1):
        too_large = run([answer(accuracy=2)])
    assert too_large["stop_reason"] == "fixer_failed"
    assert too_large["totals"]["llm_calls"] == 0
    assert "Prompt cap" in too_large["rounds"][-1]["error"]
    with patch.object(
        loop,
        "run_completion",
        return_value={
            "text": "",
            "cost_usd": 0.25,
            "usage": {"input_tokens": 20, "output_tokens": 2},
        },
    ):
        paid_failure = run([answer(accuracy=2)])
    assert paid_failure["rounds"][-1]["cost_usd"] == 0.25
    assert paid_failure["totals"]["tokens_out"] == 22
    gate_round_1_cases()
    cli_cases()
    print("verify_qe_jev_loop OK")


def cli_cases():
    from app.cli.__main__ import main as cli
    from app.ports import telemetry
    from app.services import prompt_tap

    with contextlib.redirect_stdout(io.StringIO()):
        assert cli(["prompt-tap", "on"]) == 0

    output = _TMP / "report.json"
    for args in (
        [
            "--source",
            "Source",
            "--target",
            "initial",
            "--source-locale",
            "en",
            "--target-locale",
            "de",
        ],
        ["--pairs", "tests/fixtures/qe_pairs_sample.jsonl"],
    ):
        fixer.replies = iter(["fixed"] * 100)
        if args[0] == "--pairs":
            fixer.replies = iter(
                "fixed 10 euros" if "10" in json.loads(line)["source"] else "fixed"
                for line in Path(args[1]).read_text(encoding="utf-8").splitlines()
            )
        capture = io.StringIO()
        with (
            patch.object(qe_jev, "_post", side_effect=[answer(accuracy=2), answer()] * 100),
            contextlib.redirect_stdout(capture),
            patch.object(telemetry, "enabled", return_value=True),
            patch.object(telemetry, "emit", side_effect=prompt_tap.capture) as emit,
        ):
            assert (
                cli(
                    [
                        "lqa",
                        "jev-refine",
                        "--yes",
                        *args,
                        "--provider",
                        "jevfixer",
                        "--show-prompts",
                        "--cross-check",
                        "--json",
                        str(output),
                    ]
                )
                == 0
            )
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["rows"] and "Version 0" in capture.getvalue()
        assert report["summary"]["reached_target"] == len(report["rows"])
        records = [call.args[0] for call in emit.call_args_list]
        assert len(records) == 4 * len(report["rows"])
        for number, _row in enumerate(report["rows"], 1):
            group = records[(number - 1) * 4 : number * 4]
            assert [r["provider"] for r in group] == [
                "jev",
                "jevfixer",
                "jev",
                "jevfixer",
            ]
            assert [r["meta"]["round"] for r in group] == [0, 1, 1, 1]
            for record in group:
                meta = record["meta"]
                assert meta["source"] == "cli" and meta["tool"] == "jev-refine"
                assert meta["pair"] == number and meta["segment_index"] == number
                assert all(
                    meta[k] is None for k in ("workflow_id", "workflow_run_id", "document_id")
                )
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            assert cli(["--json", "prompt-tap", "show", "--provider", "jev"]) == 0
        assert any(
            r.get("meta", {}).get("tool") == "jev-refine"
            for r in json.loads(shown.getvalue())["records"]
        )
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli(["prompt-tap", "off"]) == 0
    bad = _TMP / "bad.jsonl"
    bad.write_text("{}\nnot json\n", encoding="utf-8")
    for path in (bad, _TMP / "missing.jsonl"):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            assert cli(["lqa", "jev-refine", "--yes", "--pairs", str(path)]) == 0
        assert "error:" in capture.getvalue()


def case_1_target_rule():
    major = run([answer(terminology=2)], target_score=70, max_tries=1)
    assert major["final"]["score"] == 70
    assert major["stop_reason"] == "max_tries"
    minor = run([answer(accuracy=1, fluency=1)], target_score=70, max_tries=1)
    assert minor["final"]["score"] == 70
    assert minor["stop_reason"] == "target_reached"
    edited = run([answer(accuracy=2), answer(terminology=2)], max_tries=2)
    assert edited["stop_reason"] == "max_tries"


def case_no_veto_stops_and_best():
    unsure = answer(style=1)
    unsure[1]["answers"]["accuracy"]["confidence"] = 0.4
    result = run([unsure, answer()], max_tries=2)
    assert result["rounds"][0]["jev"]["score"] == 95
    assert len(result["rounds"]) == 2 and result["stop_reason"] == "target_reached"
    assert result["best"]["round"] == 1 and result["best"]["score"] == 100
    lower = copy.deepcopy(unsure)
    lower[1]["answers"]["style"]["score"] = 2
    result = run([lower, unsure, lower], max_tries=3)
    assert result["stop_reason"] == "unsure"
    assert result["best"]["round"] == 1 and result["best"]["score"] == 95
    assert result["best"]["unsure"] == ["accuracy"]
    for answers, best in (([unsure, answer(style=1)], 1), ([answer(style=1), unsure], 0)):
        result = run(answers, max_tries=2, target_score=100)
        assert result["best"]["round"] == best and result["best"]["unsure"] == []


def case_no_veto_deltas():
    unsure = answer(style=1)
    unsure[1]["answers"]["accuracy"]["confidence"] = 0.4
    for answers, expected in (
        ([unsure, answer(accuracy=2)], "accuracy: unsure -> major"),
        ([answer(style=1), unsure], "accuracy: none -> unsure"),
    ):
        result = run(answers, max_tries=2, target_score=100)
        previous, current = [r["jev"] for r in result["rounds"]]
        assert result["rounds"][1]["delta_vs_previous"]["accuracy"] is None
        tail = ", fluency: none -> none (0), terminology: none -> none (0), " + (
            "style: minor -> none (+); score: -35"
            if current["score"] == 60
            else "style: minor -> minor (0); score: +0"
        )
        assert loop._delta_line(previous, current) == "Delta versus previous: " + expected + tail


def case_2_max_tries_ceiling():
    from app.cli.__main__ import main as cli

    # Supply only 26 rounds: a missing ceiling fails quickly, never doing 100000 calls.
    result = run(
        [answer(accuracy=2)] * 26,
        [f"edit {i}" for i in range(25)],
        max_tries=100000,
    )
    assert len(result["rounds"]) == 25 and result["stop_reason"] == "max_tries"
    assert loop.MAX_TRIES_CEILING == 25
    assert result["max_tries_clamped"] is True
    assert result["totals"]["llm_calls"] == 24
    for budget in (1, 25):
        assert not run([answer()], max_tries=budget).get("max_tries_clamped", False)
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
        try:
            code = cli(["lqa", "jev-refine", "--yes", "--max-tries", "26"])
        except SystemExit as exc:
            code = exc.code
    assert code == 2
    assert "--max-tries must be from 1 to 25" in capture.getvalue()
    assert len(capture.getvalue().strip().splitlines()) == 1
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture), contextlib.suppress(SystemExit):
        cli(["lqa", "jev-refine", "--yes", "--help"])
    assert "range 1-25" in " ".join(capture.getvalue().split())
    doc = Path("docs/CLI.md").read_text(encoding="utf-8").split("### Jev-guided refine loop")[1]
    assert "whole numbers 1-25" in doc and "max_tries_clamped" in doc


def case_3_reply_json():
    for reply, rejected in (
        ('{"accuracy": 0}', True),
        ("[]", True),
        ('```json\n{"accuracy": 0}\n```', True),
        ("```\n[]\n```", True),
        ("```json\n42\n```", True),
        ("{Name} hat sich angemeldet", False),
        ('{"Name"} hat sich angemeldet', False),
        ("[Name] hat sich angemeldet", False),
        ("```de\nhallo\n```", False),
        ("```json\n{}\n```\nNachwort", False),
        ("```de\nhallo", False),
    ):
        result = run([answer(accuracy=2), answer()], [reply], max_tries=2)
        assert (result["stop_reason"] == "fixer_failed") == rejected, repr(reply)
        assert result["final"]["translation"] == ("initial" if rejected else reply)


def case_4_scaffold_trust():
    expected = '<jev_refine trust="data-only">\nsection\n</jev_refine>'
    assert scaffold.fence(scaffold.JEV_REFINE_TAG, "section") == expected
    assert scaffold.scaffold_echo(expected)
    assert not scaffold.scaffold_echo(expected, expected)
    prompt = loop.build_prompt("Source", "en", "de", [], 70)
    assert prompt.count('<jev_refine trust="data-only">') == 6


def case_5_documentation():
    from app.cli.__main__ import main as cli

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            cli(["lqa", "jev-refine", "--yes", "--max-tries", "abc"])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("argparse must reject a non-integer budget")
    cli_cases()  # Missing files and malformed rows still print errors and return zero.
    result = run([answer(accuracy=2), (400, {}), (400, {})], ["edited", "edited"])
    assert result["stop_reason"] == "no_progress"
    for path in ("docs/CLI.md", "docs/architecture/quality-lqa-evaluations.md"):
        section = Path(path).read_text(encoding="utf-8").split("Jev-guided refine loop (POC)")[-1]
        section = section.split("\n## ")[0]
        normalized = " ".join(section.split())
        assert "no_progress wins over judge_skipped_twice when both hold" in normalized
        assert "\u2014" not in section
        if path == "docs/CLI.md":
            assert "Argparse value errors exit 2" in normalized
            assert "missing files and malformed rows print messages and exit 0" in normalized


def scoring_train_loop_case():
    from app.services import qe_autofix

    baseline = {
        "skipped": False,
        "score": 60,
        "categories": {"accuracy": 2, "style": 0},
        "unsure": ["style"],
        "rationale": "accuracy=major style=unsure",
    }
    assert (
        qe_autofix.skip_reason(
            baseline, judge_is_jev=True, redact=False, cap_tripped=False, refused=False
        )
        is None
    ), "#201 confident major accuracy must run despite unsure minor style"
    with patch.object(loop, "run_completion", return_value={"text": "Payez neuf euros."}) as fix:
        result = loop.refine(
            "Pay nine euros.",
            "Payez dix euros.",
            "en",
            "fr",
            spec=SPEC,
            baseline_card=baseline,
            max_tries=2,
            stop_on_unsure=True,
            scorer=lambda *a, **kw: {
                **baseline,
                "score": 100,
                "categories": {"accuracy": 0, "style": 0},
            },
        )
    assert fix.call_count == 1
    assert "Leave unsure dimensions unchanged: style" in fix.call_args.args[1]
    winner, fixed = qe_autofix.pick_winner(result, 60, 70)
    assert winner and winner["translation"] == "Payez neuf euros." and not fixed
    assert winner["jev"]["unsure"] == ["style"]
    # Once accuracy is clean, an unchanged unsure style can produce a low-confidence
    # card. Its confident accuracy result still permits keeping the repair for review.
    clean = answer()
    clean[1]["answers"]["style"]["confidence"] = 0.2
    with (
        patch.object(config, "TYPESAFE_API_KEY", "fake"),
        patch.object(qe_jev, "_post", return_value=clean),
        patch.object(loop, "run_completion", return_value={"text": "Payez neuf euros."}),
    ):
        result = loop.refine(
            "Pay nine euros.",
            "Payez dix euros.",
            "en",
            "fr",
            spec=SPEC,
            baseline_card=baseline,
            max_tries=2,
            stop_on_unsure=True,
        )
    winner, fixed = qe_autofix.pick_winner(result, 60, 70)
    assert winner and not fixed and winner["jev"]["unsure"] == ["style"]
    assert winner["jev"]["score"] is None
    assert "judge did not score" in winner["jev"]["rationale"]
    assert qe_autofix.gate_decision(winner["jev"], 70) == "undecided"
    structural = {
        **baseline,
        "score": 100,
        "unsure": [],
        "categories": {},
        "structural_blocked": True,
        "structural_findings": [
            {"dimension": "untranslated", "severity": "major", "reason": "copy"}
        ],
    }
    with patch.object(loop, "run_completion", return_value={"text": "Payez neuf euros."}):
        result = loop.refine(
            "Pay nine euros.",
            "Pay nine euros.",
            "en",
            "fr",
            spec=SPEC,
            baseline_card=structural,
            max_tries=2,
            target_score=0,
            scorer=lambda *a, **kw: {**baseline, "score": 100, "unsure": [], "categories": {}},
        )
    assert result["stop_reason"] == "target_reached" and result["best"]["round"] == 1


def gate_round_1_cases():
    failures = []
    for number, case in enumerate(
        (
            case_1_target_rule,
            case_2_max_tries_ceiling,
            case_3_reply_json,
            case_4_scaffold_trust,
            case_5_documentation,
            case_no_veto_stops_and_best,
            case_no_veto_deltas,
        ),
        1,
    ):
        try:
            case()
        except AssertionError:
            failures.append(number)
            print(f"FAIL round-1 case {number}: {case.__name__}")
            traceback.print_exc(file=sys.stdout)
        else:
            print(f"PASS round-1 case {number}: {case.__name__}")
    assert not failures, f"round-1 cases failed: {failures}"


if __name__ == "__main__":
    main()
