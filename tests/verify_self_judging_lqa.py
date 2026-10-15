"""#148: a drafted quality check must not silently be judged by the engine that wrote the text.

An LQA step resolves its judge as `step.provider or config.DEFAULT_PROVIDER`
(`orchestrator_part7`), and the workflow drafter routinely emits BLANK providers on every
step. On a single-engine install that means the translate step and the check that grades it
are the SAME engine, so the model marks its own homework -- and a correction loop whose gate
cannot fail never reaches the fix step it exists to trigger.

Measured on PROD during campaign qa-0916w, which is why this suite exists: gemma scored its
own translation 100 and passed a 90 gate (run #196); claude scored the same text 85 and
failed it (run #197).

The draft-time response is a WARNING, not a rebind: choosing a different judge is a spend
decision (the alternative to a free local box is a paid cloud call) and the human makes it
on the review screen. These tests pin the warning's presence, its ABSENCE on the shapes that
are fine, and that it never rewrites the draft.

ASCII-only output. Prints "SELF-JUDGING LQA OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_selfjudge_")

from unittest.mock import patch

from app import config
from app.db import init_db
from app.services.setup_author import _warn_self_judging_lqa

init_db()


def _draft(steps):
    return {"fields": {"steps": steps}, "warnings": []}


def _agent(provider=None, model=None, name="Translate"):
    return {"step_type": "llm_agent", "provider": provider, "model": model, "name": name}


def _lqa(provider=None, model=None, name="LQA check"):
    return {"step_type": "lqa", "provider": provider, "model": model, "name": name}


def _warn(draft):
    return " ".join(draft["warnings"])


def test_blank_providers_on_one_engine_install_warn():
    """THE PROD SHAPE: every step blank, so every step inherits the one install default."""
    with patch.object(config, "DEFAULT_PROVIDER", "engine:1"):
        d = _draft([_agent(), _lqa()])
        before = [dict(s) for s in d["fields"]["steps"]]
        _warn_self_judging_lqa(d)
    assert d["warnings"], "#148: a blank judge inheriting the translate engine must warn"
    w = _warn(d)
    assert "judging the engine that produced the text" in w, w
    assert "step 2 (LQA check) judging step 1 (Translate)" in w, w
    assert "install default" in w, "the warning must say WHERE the judge got its engine"
    # It warns; it never rewrites.
    assert d["fields"]["steps"] == before, "#148: the warning must not mutate the draft"
    print("  blank providers on a one-engine install -> warned, draft untouched OK")


def test_same_explicit_engine_warns_even_when_family_is_unresolvable():
    """Identity beats family: two engine:<id> refs nobody can resolve are still one engine."""
    with patch.object(config, "DEFAULT_PROVIDER", "claude"):
        d = _draft([_agent(provider="engine:1"), _lqa(provider="engine:1")])
        _warn_self_judging_lqa(d)
    assert d["warnings"], "#148: the same explicit engine on both sides must warn"
    assert "the provider set on it" in _warn(d), _warn(d)
    # ...and a non-canonical spelling of the same engine is the same engine.
    with patch.object(config, "DEFAULT_PROVIDER", "claude"):
        d2 = _draft([_agent(provider="engine:1"), _lqa(provider="ENGINE: 01")])
        _warn_self_judging_lqa(d2)
    assert d2["warnings"], (
        "#148: a zero-padded/cased ref is the SAME engine, not an independent judge"
    )
    print("  same engine (incl. non-canonical spelling) -> warned OK")


def test_different_engines_do_not_warn():
    """The shape the maintainer hand-builds: translate on the local box, judge on a cloud model."""
    with patch.object(config, "DEFAULT_PROVIDER", "engine:1"):
        d = _draft([_agent(provider="engine:1"), _lqa(provider="claude")])
        _warn_self_judging_lqa(d)
    assert not d["warnings"], f"a genuinely independent judge must NOT warn: {d['warnings']}"
    # A blank TRANSLATE step inheriting the default, with an explicit different judge, is fine too.
    with patch.object(config, "DEFAULT_PROVIDER", "engine:1"):
        d2 = _draft([_agent(), _lqa(provider="claude")])
        _warn_self_judging_lqa(d2)
    assert not d2["warnings"], f"blank author + distinct judge must NOT warn: {d2['warnings']}"
    print("  independent judge (the hand-built shape) -> silent OK")


def test_same_family_different_ref_warns():
    """Two DIFFERENT refs in one model family are still not an independent judge."""
    with patch.object(config, "DEFAULT_PROVIDER", "engine:1"):
        d = _draft(
            [
                _agent(provider="claude", model="claude-sonnet-5"),
                _lqa(provider="claude", model="claude-opus-5"),
            ]
        )
        _warn_self_judging_lqa(d)
    assert d["warnings"], "#148: same family must warn even when the model ids differ"
    print("  same family, different model ids -> warned OK")


def test_no_judge_or_no_author_is_silent():
    """Nothing to collide: a straight-through draft, and a check with nothing to judge."""
    with patch.object(config, "DEFAULT_PROVIDER", "engine:1"):
        d = _draft([_agent(), _agent(name="Polish")])
        _warn_self_judging_lqa(d)
        assert not d["warnings"], d["warnings"]
        d2 = _draft([_lqa()])
        _warn_self_judging_lqa(d2)
        assert not d2["warnings"], d2["warnings"]
        d3 = _draft([])
        _warn_self_judging_lqa(d3)
        assert not d3["warnings"], d3["warnings"]
    print("  no judge / no author / empty draft -> silent OK")


def test_one_warning_per_judge_not_per_pair():
    """A loop has several translate-ish steps; the operator gets ONE readable sentence."""
    with patch.object(config, "DEFAULT_PROVIDER", "engine:1"):
        d = _draft([_agent(), _lqa(name="LQA 1"), _agent(name="Post-edit"), _lqa(name="LQA 2")])
        _warn_self_judging_lqa(d)
    assert len(d["warnings"]) == 1, f"one warning, not one per pair: {d['warnings']}"
    w = _warn(d)
    assert "LQA 1" in w and "LQA 2" in w, w
    # Each judge names the FIRST step it collides with, not every one of them.
    assert w.count("judging step") == 2, w
    print("  the real correction-loop shape -> one warning naming both checks OK")


if __name__ == "__main__":
    test_blank_providers_on_one_engine_install_warn()
    test_same_explicit_engine_warns_even_when_family_is_unresolvable()
    test_different_engines_do_not_warn()
    test_same_family_different_ref_warns()
    test_no_judge_or_no_author_is_silent()
    test_one_warning_per_judge_not_per_pair()
    print("SELF-JUDGING LQA OK")
