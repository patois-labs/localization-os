"""CLI polish batch (#20) regressions - the code-behavior items (offline, in-process).

Covers the fixable-in-code nits from issue #20:
  (a) `workflow run` emits a per-step trace + a structured `step_summary` on --json
      (the `steps` key stays the int COUNT for back-compat) -- tested at the
      _run_step_summary/_run_step_line seam.
  (e) `connector airtable-webhook` with no leaf -> the SCOPED usage error (exit 2),
      not the top-level usage (nested subparser required=True).
  (h) `workflow add-step --branch on_fail=step:fix` -> a clean error that ECHOES the
      user's 'step:fix' and points at the bare-key syntax (not a slugified 'stepfix').
  (i) `context set-profile --tone` warns about words outside the curated vocabulary,
      and the vocabulary is documented in --tone --help.
  (j) `template show <missing>` exits 2 (the CLI-wide not-found convention), matching
      `template apply`.
  (k) `workflow prompt list` reports 'pinned_by' (how many steps pin the template),
      not the misleading 'steps' header/key.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys at import.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_polish_")

from app.db import init_db
from tests._cli_runner import run_cli as run


def run_json(argv):
    code, out, err = run(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def test_run_step_summary_seam():
    """(a) The per-step trace helpers turn orchestrator `step` events into a compact,
    ASCII, machine-readable summary; `steps` stays a count, `step_summary` is the list."""
    from app.cli import cmd_workflow

    events = [
        {
            "step_index": 0,
            "step_name": "Translator",
            "step_type": "llm_agent",
            "provider": "mock",
            "model": "m",
            "attempts": 1,
            "error": None,
        },
        {
            "step_index": 1,
            "step_name": "LQA",
            "step_type": "lqa",
            "provider": None,
            "model": None,
            "attempts": 0,
            "error": None,
            "lqa": {"score": 91, "gate": "pass", "errors": 0},
        },
    ]
    summ = cmd_workflow._run_step_summary(events)
    assert [s["index"] for s in summ] == [0, 1], summ
    assert summ[0]["provider"] == "mock" and "lqa" not in summ[0], summ[0]
    assert summ[1]["lqa"]["score"] == 91, summ[1]
    line0 = cmd_workflow._run_step_line(summ[0])
    line1 = cmd_workflow._run_step_line(summ[1])
    assert "[0]" in line0 and "provider=mock/m" in line0 and line0.endswith("ok"), line0
    assert "lqa=91 gate=pass" in line1, line1
    assert all(ord(c) < 128 for c in line0 + line1), "step line must be ASCII"
    # An errored step reports the error, not 'ok'.
    errline = cmd_workflow._run_step_line({"index": 2, "type": "llm_agent", "error": "boom"})
    assert "ERROR: boom" in errline, errline
    print("  (a) run step_summary/line seam: compact ASCII trace, count stays an int")


def test_airtable_webhook_requires_leaf():
    """(e) a bare `connector airtable-webhook` is a SCOPED usage error (exit 2)."""
    code, _out, err = run(["connector", "airtable-webhook"])
    assert code == 2, code
    assert "airtable-webhook" in err and "required" in err.lower(), err
    print("  (e) connector airtable-webhook (no leaf) -> scoped usage error, exit 2")


def test_branch_colon_error_echoes_input():
    """(h) --branch on_fail=step:fix echoes 'step:fix' + the bare-key hint (exit 2)."""
    code, pay, _ = run_json(["workflow", "create", "--name", "Branch Test"])
    assert code == 0, pay
    wf_id = pay["id"]
    code, _out, err = run(
        [
            "workflow",
            "add-step",
            str(wf_id),
            "--type",
            "llm_agent",
            "--prompt",
            "Translate: {text}",
            "--branch",
            "on_fail=step:fix",
        ]
    )
    assert code == 2, (code, err)
    assert "step:fix" in err, ("error must echo the user's exact input", err)
    assert "stepfix" not in err, ("error must NOT name the slugified key", err)
    assert "bare step key" in err, err
    print("  (h) --branch on_fail=step:fix -> clean error echoing 'step:fix', exit 2")


def test_prompt_list_pinned_by_header():
    """(k) `workflow prompt list` uses 'pinned_by', not the misleading 'steps'."""
    code, pay, err = run_json(
        [
            "workflow",
            "prompt",
            "create",
            "--name",
            "PT One",
            "--body",
            "Translate faithfully: {text}",
        ]
    )
    assert code == 0, (pay, err)
    code, pay, err = run_json(["workflow", "prompt", "list"])
    assert code == 0, (pay, err)
    rows = pay["prompt_templates"]
    assert rows and "pinned_by" in rows[0] and "steps" not in rows[0], rows[0]
    code, out, err = run(["workflow", "prompt", "list"])
    assert code == 0 and "pinned_by" in out and "steps" not in out.split("\n")[0], out
    print("  (k) workflow prompt list -> 'pinned_by' header + json key (not 'steps')")


def test_template_show_notfound_exit2():
    """(j) `template show <missing>` exits 2, matching the CLI-wide not-found convention."""
    code, _out, err = run(["template", "show", "999999"])
    assert code == 2, (code, err)
    assert "no such template" in err, err
    print("  (j) template show <missing> -> exit 2 (matches template apply / convention)")


def test_tone_vocabulary_warning_and_help():
    """(i) --tone warns about dropped words; the vocabulary is documented in --help."""
    # A mix of a valid and an invalid tone word: the invalid one is named in a warning.
    code, _out, err = run(
        ["context", "set-profile", "--name", "Tone Card", "--tone", "friendly, zzznotatone"]
    )
    assert code == 0, (code, err)
    assert "zzznotatone" in err and "vocabulary" in err.lower(), err
    assert "friendly" not in err.split("Allowed:")[0], ("valid words must not be flagged", err)
    # The vocabulary is discoverable in --help.
    code, out, _err = run(["context", "set-profile", "-h"])
    # argparse -h raises SystemExit(0); capture printed help.
    help_text = out
    assert "friendly" in help_text and "vocabulary" in help_text.lower(), help_text
    print("  (i) context set-profile --tone: warns on dropped words; vocab in --help")


def main_() -> None:
    init_db()
    test_run_step_summary_seam()
    test_airtable_webhook_requires_leaf()
    test_branch_colon_error_echoes_input()
    test_prompt_list_pinned_by_header()
    test_template_show_notfound_exit2()
    test_tone_vocabulary_warning_and_help()
    print("CLI POLISH OK")


if __name__ == "__main__":
    main_()
