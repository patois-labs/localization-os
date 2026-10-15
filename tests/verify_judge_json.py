"""QA-2 regression: the LLM judge (LQA + QE) must not silently discard findings when
the model wraps valid JSON in prose / a ```json fence.

Root cause (verified, the planning notes QA-2): both
judge seams did a bare ``json.loads(res["text"])`` (lqa.py:178, qe.py:132). The
reference live local model ``meta-llama-3-8b-instruct`` reliably prefixes its JSON with
a prose preamble ("Here is the JSON output:\\n\\n{…}"), so the parse raised and the
``except`` degraded to ``[]`` / ``skipped=junk_json`` — the ENTIRE judge dimension AND
the B3 style-adherence gate became a silent no-op while still reporting
``auto_approved``.

Fix: one tolerant extractor (``app.services.llm_json.loads``) both seams import — strip
a ```json fence, return the first balanced (string-aware) ``{…}`` object that parses;
genuine junk still raises so callers soft-degrade unchanged; clean JSON is byte-identical.

Coverage (each a hermetic, offline check; NO network, NO DB):
  1. extractor unit: prose / fence / trailing-prose / nested-braces-in-strings /
     stray-prose-brace / clean byte-identity / junk-raises / array-preserved.
  2. lqa._llm_errors seam: prose-wrapped Style/major finding LANDS (len 1) and drives
     score_text's gate to needs_review; fenced + trailing-prose land; genuine junk -> []
     (soft-degrade UNCHANGED); clean JSON unchanged.
  3. qe.score_segment seam: prose-wrapped counts -> a REAL score (not junk_json);
     genuine junk -> skipped=junk_json (UNCHANGED); clean JSON unchanged.
  4. B3 style-adherence path: a style guide + a prose-wrapped "Style" major violation
     -> score_text gate FAILS (the exact silent-no-op the report flagged).
  5. FAIL-BEFORE via git HEAD reconstruction: HEAD's pre-fix lqa/qe modules (bare
     json.loads) return []/skipped on the SAME prose-wrapped inputs the fixed in-tree
     modules recover — the regression that reproduces the bug. Guarded to stay green
     post-commit (when HEAD already carries the fix) via the permanent bare-json.loads
     stand-in proof (which is exactly the operation the old code performed).

Final line: "JUDGE JSON OK".
"""

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_judgejson_")

from app.services import llm_json, lqa, qe
from app.services.providers import ProviderSpec


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _canned(text):
    """A run_completion result carrying `text` (the judge's raw output)."""
    return {
        "text": text,
        "provider_used": "mock",
        "model_used": "mock",
        "attempts": 1,
        "error": None,
        "usage": {},
    }


# The exact wrapper shapes meta-llama-3-8b emits around otherwise-valid JSON.
def _prose(body):
    return f"Here is the JSON output:\n\n{body}\n\nHope that helps!"


def _fenced(body):
    return f"```json\n{body}\n```"


_LQA_STYLE_MAJOR = (
    '{"errors":[{"dimension":"Style","severity":"major",'
    '"note":"register is too casual for the guide"}]}'
)
_QE_COUNTS = (
    '{"accuracy":3,"fluency":0,"terminology":0,"style":0,"rationale":"multiple mistranslations"}'
)


# ===========================================================================
# 1. Extractor unit
# ===========================================================================
def test_extractor_unit():
    # prose-wrapped object
    d = llm_json.loads(_prose(_LQA_STYLE_MAJOR))
    if d["errors"][0]["dimension"] != "Style":
        _fail(("prose-wrapped object not recovered", d))
    # ```json fence
    d = llm_json.loads(_fenced(_QE_COUNTS))
    if d["accuracy"] != 3:
        _fail(("fenced object not recovered", d))
    # trailing prose after a clean object
    if llm_json.loads('{"errors":[]}\n\nThat is the final answer.') != {"errors": []}:
        _fail("trailing prose not tolerated")
    # nested braces + escaped quotes inside a string value must not break the balance
    nb = (
        'Sure: {"errors":[{"dimension":"Style","severity":"minor",'
        '"note":"uses {curly} braces and a \\"quote\\" oddly"}]}'
    )
    note = llm_json.loads(nb)["errors"][0]["note"]
    if note != 'uses {curly} braces and a "quote" oddly':
        _fail(("nested-brace/escaped-quote balance wrong", repr(note)))
    # a stray non-JSON brace in the preamble is skipped; the real object wins
    stray = (
        "the format is like {k: v}; here it is: "
        '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"clean"}'
    )
    if llm_json.loads(stray)["rationale"] != "clean":
        _fail("stray prose brace not skipped")
    # clean JSON -> byte-identical to json.loads
    clean = '{"accuracy":1,"fluency":2,"terminology":0,"style":0,"rationale":"x"}'
    if llm_json.loads(clean) != json.loads(clean):
        _fail("clean JSON not byte-identical")
    # a top-level array (fast path) is preserved, not mangled into an object
    if llm_json.loads("[1, 2, 3]") != [1, 2, 3]:
        _fail("top-level array not preserved")
    # genuine junk raises JSONDecodeError (a ValueError subclass, so qe's narrow
    # except (ValueError, TypeError) keeps working)
    for junk in ("not json at all {{{", "", "   ", "no braces here"):
        try:
            llm_json.loads(junk)
            _fail(("junk did not raise", repr(junk)))
        except json.JSONDecodeError as e:
            if not isinstance(e, ValueError):
                _fail("JSONDecodeError must subclass ValueError")
    # None also raises (never returns a bogus value)
    try:
        llm_json.loads(None)
        _fail("None did not raise")
    except json.JSONDecodeError:
        pass
    print(
        "  1. extractor: prose/fence/trailing/nested-braces/stray-brace recovered; "
        "clean byte-identical; array preserved; junk+None raise OK"
    )


# ===========================================================================
# 2. lqa._llm_errors seam
# ===========================================================================
def _lqa_errors(reply_text, *, rulebook="", style_guide=""):
    """Call lqa._llm_errors with the judge forced live and run_completion returning
    `reply_text`, restoring the originals after. _llm_errors returns
    (findings, cost_usd, tokens_in, tokens_out, judge_status, judge_shape) since the
    workflow-spend feature + #56 + #96; this seam test cares about the FINDINGS, so
    unpack element 0."""
    orig_live, orig_rc = lqa.provider_is_live, lqa.run_completion
    lqa.provider_is_live = lambda name: True
    lqa.run_completion = lambda spec, prompt, **kw: _canned(reply_text)
    try:
        return lqa._llm_errors(
            "Buy the widget today.",
            "[es] Que onda!",
            "en-US",
            "es-ES",
            rulebook,
            "mock",
            style_guide=style_guide,
        )[0]
    finally:
        lqa.provider_is_live, lqa.run_completion = orig_live, orig_rc


def _lqa_card(reply_text, *, rulebook="", style_guide=""):
    orig_live, orig_rc = lqa.provider_is_live, lqa.run_completion
    lqa.provider_is_live = lambda name: True
    lqa.run_completion = lambda spec, prompt, **kw: _canned(reply_text)
    try:
        return lqa.score_text(
            "Buy the widget today.",
            "[es] Que onda!",
            "en-US",
            "es-ES",
            rulebook,
            "mock",
            style_guide=style_guide,
        )
    finally:
        lqa.provider_is_live, lqa.run_completion = orig_live, orig_rc


def test_lqa_seam():
    # prose-wrapped valid finding LANDS (the headline QA-2 case: pre-fix returned [])
    errs = _lqa_errors(_prose(_LQA_STYLE_MAJOR))
    if not (len(errs) == 1 and errs[0]["dimension"] == "Style" and errs[0]["severity"] == "major"):
        _fail(("prose-wrapped LQA finding did not land", errs))
    # ... and it drives the gate: a major Style error -> needs_review
    card = _lqa_card(_prose(_LQA_STYLE_MAJOR))
    if not (card["passed"] is False and card["gate"] == "needs_review"):
        _fail(("prose-wrapped major must fail the gate", card))
    style = [e for e in card["errors"] if e["dimension"] == "Style"]
    if not style:
        _fail(("the judge Style error must merge into card['errors']", card["errors"]))

    # fenced ```json block -> parsed + lands
    if len(_lqa_errors(_fenced(_LQA_STYLE_MAJOR))) != 1:
        _fail("fenced LQA finding did not land")
    # JSON with trailing prose -> parsed + lands
    if len(_lqa_errors(_LQA_STYLE_MAJOR + "\n\nThat's my assessment.")) != 1:
        _fail("trailing-prose LQA finding did not land")

    # genuine junk -> [] (soft-degrade UNCHANGED)
    if _lqa_errors("total nonsense, no json here") != []:
        _fail("genuine junk must soft-degrade to []")
    # a not-live provider still short-circuits to [] (unchanged posture)
    orig_live = lqa.provider_is_live
    lqa.provider_is_live = lambda name: False
    try:
        if lqa._llm_errors("s", "t", "en-US", "es-ES", "", "mock")[0] != []:
            _fail("not-live provider must return []")
    finally:
        lqa.provider_is_live = orig_live

    # clean JSON -> unchanged (finding lands, no wrapper involved)
    if len(_lqa_errors(_LQA_STYLE_MAJOR)) != 1:
        _fail("clean-JSON LQA finding did not land")
    # clean JSON with NO errors -> [] (a clean pass, unchanged)
    if _lqa_errors('{"errors":[]}') != []:
        _fail("clean empty-errors must yield []")
    print(
        "  2. lqa._llm_errors: prose/fenced/trailing findings LAND + drive gate to "
        "needs_review; junk/not-live -> [] unchanged; clean unchanged OK"
    )


# ===========================================================================
# 3. qe.score_segment seam
# ===========================================================================
def _qe_score(reply_text):
    orig_rc = qe.run_completion
    qe.run_completion = lambda spec, prompt, **kw: _canned(reply_text)
    try:
        return qe.score_segment(
            ProviderSpec(provider="mock", model="m"), "source", "target", "en-US", "es-ES"
        )
    finally:
        qe.run_completion = orig_rc


def test_qe_seam():
    # prose-wrapped counts -> a REAL score (pre-fix: skipped=junk_json). accuracy:3 ->
    # 100 - 3*20 = 40, and the rationale survives.
    res = _qe_score(_prose(_QE_COUNTS))
    if not (
        res["skipped"] is False
        and res["score"] == 40
        and res["rationale"] == "multiple mistranslations"
    ):
        _fail(("prose-wrapped QE did not produce a real score", res))
    # fenced -> real score
    if _qe_score(_fenced(_QE_COUNTS))["score"] != 40:
        _fail("fenced QE did not score")
    # trailing prose -> real score
    if _qe_score(_QE_COUNTS + "\n\nDone.")["score"] != 40:
        _fail("trailing-prose QE did not score")
    # genuine junk -> skipped=junk_json (UNCHANGED soft-degrade)
    junk = _qe_score("no json whatsoever")
    if not (junk["skipped"] is True and junk["reason"] == "junk_json"):
        _fail(("genuine junk must skip as junk_json", junk))
    # a non-dict JSON value (top-level array) -> still junk_json (unchanged)
    arr = _qe_score("[1,2,3]")
    if not (arr["skipped"] is True and arr["reason"] == "junk_json"):
        _fail(("non-object JSON must skip as junk_json", arr))
    # clean JSON -> real score (unchanged)
    clean = _qe_score('{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"clean"}')
    if not (clean["skipped"] is False and clean["score"] == 100):
        _fail(("clean-JSON QE score wrong", clean))
    print(
        "  3. qe.score_segment: prose/fenced/trailing -> real score; junk/non-object "
        "-> skipped=junk_json unchanged; clean unchanged OK"
    )


# ===========================================================================
# 3b. lqa._llm_errors TOP-LEVEL ARRAY (list-judge fix, HIGH, live-repro'd on
#     translategemma-12b-it): a judge that returns its findings as a bare
#     `[{...}, ...]` array (not wrapped in {"errors":...}) whenever it has >=1
#     finding. `llm_json.loads` preserves the top-level list, so pre-fix
#     `data.get("errors", [])` raised AttributeError -> the broad except swallowed
#     it -> the segments WITH findings degraded to JUDGE_UNAVAILABLE (auto-degrade
#     to needs_review, run halts before post-edit). Zero-finding segments returned a
#     dict and parsed fine, hiding the bug.
# ===========================================================================
_LQA_ARRAY_TWO = (
    '[{"dimension":"Accuracy","severity":"critical","note":"mistranslated the CTA"},'
    '{"dimension":"Style","severity":"major","note":"register too casual"}]'
)


def _lqa_full(reply_text, *, rulebook="", style_guide=""):
    """The first FIVE elements of the _llm_errors tuple (findings, cost, tin, tout,
    judge_status). Issue #96 appended a sixth (`judge_shape`, the judge call's
    engine/model/attempts/finish/error-class metadata); it is sliced off here so this
    suite's ~40 parse-behaviour assertions keep their 5-tuple shape -- the shape itself is
    proven in tests/verify_lqa_loop_progress.py."""
    orig_live, orig_rc = lqa.provider_is_live, lqa.run_completion
    lqa.provider_is_live = lambda name: True
    lqa.run_completion = lambda spec, prompt, **kw: _canned(reply_text)
    try:
        return lqa._llm_errors(
            "Buy the widget today.",
            "[es] Que onda!",
            "en-US",
            "es-ES",
            rulebook,
            "mock",
            style_guide=style_guide,
        )[:5]
    finally:
        lqa.provider_is_live, lqa.run_completion = orig_live, orig_rc


def test_lqa_toplevel_array():
    # (a) top-level array with 2 findings -> BOTH land, status OK (NOT unavailable),
    #     provenance stamped, and the judge keys survive per-item.
    findings, _cost, _tin, _tout, status = _lqa_full(_LQA_ARRAY_TWO)
    if status != lqa.JUDGE_OK:
        _fail(("top-level array must be JUDGE_OK, not degraded", status))
    if len(findings) != 2:
        _fail(("top-level array with 2 findings must yield 2, not degrade", findings))
    dims = sorted(e["dimension"] for e in findings)
    if dims != ["Accuracy", "Style"]:
        _fail(("both findings must land with their dimensions", dims))
    # our machine-authored provenance is stamped on each (same as the dict path)
    if not all(e.get("source") == "llm" and e.get("judge") for e in findings):
        _fail(("provenance must be stamped on array-recovered findings", findings))

    # (a2) ... and those findings DRIVE the gate: a critical error -> needs_review,
    #      and both merge into card['errors'] (the run would otherwise halt as a
    #      spurious JUDGE_UNAVAILABLE degrade — the live symptom).
    card = _lqa_card(_LQA_ARRAY_TWO)
    if not (card["passed"] is False and card["gate"] == "needs_review"):
        _fail(("array findings must flip the gate to needs_review", card))
    got = sorted(e["dimension"] for e in card["errors"] if e["dimension"] in ("Accuracy", "Style"))
    if got != ["Accuracy", "Style"]:
        _fail(("both array findings must merge into card['errors']", card["errors"]))

    # (b) the {"errors":[...]} DICT path is PINNED unchanged (byte-identical semantics).
    dict_form = (
        '{"errors":[{"dimension":"Accuracy","severity":"critical","note":"mistranslated the CTA"},'
        '{"dimension":"Style","severity":"major","note":"register too casual"}]}'
    )
    dfindings, _dc, _dti, _dto, dstatus = _lqa_full(dict_form)
    if dstatus != lqa.JUDGE_OK or len(dfindings) != 2:
        _fail(("dict path must remain JUDGE_OK with 2 findings", (dstatus, dfindings)))
    if sorted(e["dimension"] for e in dfindings) != ["Accuracy", "Style"]:
        _fail(("dict path findings changed", dfindings))

    # (c) {"errors":[]} (dict) -> zero findings, clean OK (unchanged).
    zfindings, _zc, _zti, _zto, zstatus = _lqa_full('{"errors":[]}')
    if not (zstatus == lqa.JUDGE_OK and zfindings == []):
        _fail(("empty-errors dict must be clean JUDGE_OK []", (zstatus, zfindings)))

    # (c2) an EMPTY top-level array [] -> zero findings, clean OK (symmetric with (c);
    #      a judge with no findings that still uses the array shape must NOT degrade).
    eafindings, _eac, _eati, _eato, eastatus = _lqa_full("[]")
    if not (eastatus == lqa.JUDGE_OK and eafindings == []):
        _fail(("empty top-level array must be clean JUDGE_OK []", (eastatus, eafindings)))

    # (d) garbage string -> JSONDecodeError inside loads -> clean JUDGE_UNAVAILABLE
    #     degrade (no crash), and a BARE INTEGER (a non-dict, non-list JSON value that
    #     PARSES) -> the EXPLICIT non-dict fail-closed arm -> JUDGE_UNAVAILABLE.
    gfindings, _gc, _gti, _gto, gstatus = _lqa_full("total nonsense, no json here")
    if not (gstatus == lqa.JUDGE_UNAVAILABLE and gfindings == []):
        _fail(("garbage string must degrade to JUDGE_UNAVAILABLE []", (gstatus, gfindings)))
    ifindings, _ic, _iti, _ito, istatus = _lqa_full("42")
    if not (istatus == lqa.JUDGE_UNAVAILABLE and ifindings == []):
        _fail(("bare integer must degrade to JUDGE_UNAVAILABLE []", (istatus, ifindings)))
    # a bare JSON string value likewise degrades cleanly (non-dict, non-list)
    sfindings, _sc, _sti, _sto, sstatus = _lqa_full('"just a string"')
    if not (sstatus == lqa.JUDGE_UNAVAILABLE and sfindings == []):
        _fail(("bare JSON string must degrade to JUDGE_UNAVAILABLE []", (sstatus, sfindings)))

    # (d2) an ALL-NON-DICT non-empty top-level array (e.g. bare strings, a plausible
    #      small-model emission) must fail CLOSED to JUDGE_UNAVAILABLE — NOT wrap-then-
    #      empty-filter to a false JUDGE_OK/100/auto_approve. Pre-fix this shape degraded
    #      to unavailable (held); the fix must preserve that safe posture.
    all_str_array = '["Accuracy: mistranslated the CTA", "Style: too casual"]'
    ndfindings, _ndc, _ndti, _ndto, ndstatus = _lqa_full(all_str_array)
    if not (ndstatus == lqa.JUDGE_UNAVAILABLE and ndfindings == []):
        _fail(("all-non-dict array must fail CLOSED to JUDGE_UNAVAILABLE []", ndstatus, ndfindings))

    # (d3) a MIXED top-level array (>=1 dict finding + interleaved non-dict junk) still
    #      lands its dict finding(s) as JUDGE_OK — the per-item isinstance filter drops the
    #      junk. This is a strict improvement over the pre-fix crash-to-unavailable.
    mixed = (
        '["a stray note", {"dimension":"Accuracy","severity":"critical","note":"mistranslated"}, 7]'
    )
    mxfindings, _mxc, _mxti, _mxto, mxstatus = _lqa_full(mixed)
    if not (mxstatus == lqa.JUDGE_OK and len(mxfindings) == 1):
        _fail(("mixed array must land its 1 dict finding as JUDGE_OK", (mxstatus, mxfindings)))
    if mxfindings[0]["dimension"] != "Accuracy":
        _fail(("mixed array's dict finding must survive", mxfindings))

    print(
        "  3b. lqa._llm_errors top-level array: 2 findings LAND + drive gate "
        "(JUDGE_OK, not degraded); dict path pinned; empty array/dict -> clean []; "
        "garbage/int/string -> JUDGE_UNAVAILABLE; all-non-dict array -> JUDGE_UNAVAILABLE "
        "(fail-closed); mixed array -> 1 finding/JUDGE_OK OK"
    )


def test_lqa_array_revert_proof():
    """REVERT-PROOF (git-independent, permanent): the pre-fix parse site did
    `data.get("errors", [])` DIRECTLY on `llm_json.loads(text)`. Reproduce that exact
    operation on a top-level array and prove it raises AttributeError (the bug), while
    the fixed in-tree `_llm_errors` recovers BOTH findings on the identical input.

    This is the permanent stand-in (like test 5a): the raw operation the old code
    performed is reconstructed here, so the proof holds even post-commit when HEAD
    carries the fix. The git-HEAD reconstruction below is the second, stronger proof."""
    parsed = llm_json.loads(_LQA_ARRAY_TWO)
    if not isinstance(parsed, list):
        _fail(("the extractor must hand back a list for a top-level array", type(parsed)))
    # the EXACT pre-fix operation: .get on the parsed judge output
    raised = False
    try:
        parsed.get("errors", [])  # this is the reproduction of the pre-fix bug
    except AttributeError:
        raised = True
    if not raised:
        _fail("pre-fix `.get('errors')` on a top-level array must raise AttributeError")
    # the fixed in-tree seam recovers both findings on the same input
    if len(_lqa_errors(_LQA_ARRAY_TWO)) != 2:
        _fail("fixed _llm_errors must recover both findings from the top-level array")
    print(
        "  3c. revert-proof (permanent): pre-fix `.get('errors')` on the parsed "
        "top-level array raises AttributeError; fixed seam recovers 2 findings OK"
    )

    # git HEAD reconstruction (stronger): load the COMMITTED lqa.py; if it lacks the
    # list-normalization guard, prove it degrades the array input to JUDGE_UNAVAILABLE
    # (the exact live symptom) while the in-tree fixed module recovers 2 findings.
    old_lqa_src = _git_show("app/services/lqa.py")
    if old_lqa_src and 'data = {"errors": data}' not in old_lqa_src:
        old_lqa = _load_module_from_source(old_lqa_src, "old_lqa_listbug")
        old_lqa.provider_is_live = lambda name: True
        old_lqa.run_completion = lambda spec, prompt, **kw: _canned(_LQA_ARRAY_TWO)
        old_tuple = old_lqa._llm_errors(
            "Buy the widget today.", "[es] Que onda!", "en-US", "es-ES", "", "mock", style_guide=""
        )
        old_findings, old_status = old_tuple[0], old_tuple[4]
        if not (old_findings == [] and old_status == old_lqa.JUDGE_UNAVAILABLE):
            _fail(
                (
                    "pre-fix lqa._llm_errors should degrade the top-level array to "
                    "JUDGE_UNAVAILABLE []",
                    old_findings,
                    old_status,
                )
            )
        if len(_lqa_errors(_LQA_ARRAY_TWO)) != 2:
            _fail("fixed in-tree lqa must recover both array findings")
        print(
            "  3d. git HEAD reconstruction: pre-fix lqa._llm_errors degrades the "
            "top-level array to JUDGE_UNAVAILABLE [], fixed module recovers 2 OK"
        )
    else:
        print(
            "  3d. HEAD already carries the list-normalization fix (post-commit) -> "
            "reconstruction skipped; the permanent proof (3c) stands in OK"
        )


# ===========================================================================
# 4. B3 style-adherence path: guide + prose-wrapped Style major -> gate FAILS
# ===========================================================================
def test_b3_style_path():
    from app.services import style_guides as sg

    class _G:
        def __init__(self, body, name="G"):
            self.body, self.name = body, name
            self.source_locale = self.target_locale = None

    block = sg.render_style_guide(_G("VOICE: use the FORMAL register. No slang."))
    # The judge, seeing the injected <style_guide> block, reports a MAJOR Style
    # violation wrapped in prose. Pre-fix this was discarded (gate auto_approved);
    # post-fix it lands and fails the gate.
    card = _lqa_card(_prose(_LQA_STYLE_MAJOR), style_guide=block)
    if not (card["passed"] is False and card["gate"] == "needs_review"):
        _fail(("B3: prose-wrapped Style major must fail the gate", card))
    if not any(e["dimension"] == "Style" for e in card["errors"]):
        _fail(("B3: the Style violation must be present in errors", card["errors"]))
    # control: the SAME guide + a prose-wrapped CLEAN judge reply -> gate passes (proves
    # it is the finding's content, recovered from prose, that flips the gate — not the
    # mere presence of a guide).
    ok = _lqa_card(_prose('{"errors":[]}'), style_guide=block)
    if not (ok["passed"] is True and ok["gate"] == "auto_approved"):
        _fail(("B3 control: prose-wrapped clean reply must pass", ok))
    print(
        "  4. B3 style path: guide + prose-wrapped Style major -> gate FAILS; "
        "prose-wrapped clean -> gate passes (content, recovered from prose, drives it) OK"
    )


# ===========================================================================
# 5. FAIL-BEFORE via git HEAD reconstruction (guarded to survive post-commit)
# ===========================================================================
def _git_show(path):
    try:
        return subprocess.check_output(
            ["git", "show", f"HEAD:{path}"], text=True, cwd=_ROOT, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None


def _load_module_from_source(src, name):
    import importlib.util

    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    exec(compile(src, f"{name}.py", "exec"), mod.__dict__)
    return mod


def test_fail_before_reconstruction():
    # Permanent stand-in proof (git-independent): a bare json.loads — EXACTLY the
    # operation the pre-fix code performed at the parse site — raises on the same
    # prose-wrapped inputs the fixed extractor recovers. This is always a valid
    # demonstration of the bug and the fix.
    for wrapped in (_prose(_LQA_STYLE_MAJOR), _fenced(_QE_COUNTS)):
        try:
            json.loads(wrapped)
            _fail(("pre-fix bare json.loads should raise on wrapped output", wrapped))
        except json.JSONDecodeError:
            pass
        llm_json.loads(wrapped)  # fixed path recovers it (raises here would fail the test)
    print(
        "  5a. permanent proof: bare json.loads (the pre-fix op) raises on wrapped "
        "output; llm_json.loads recovers it OK"
    )

    # git HEAD reconstruction: load the committed pre-fix lqa/qe modules and prove they
    # exhibit the bug (return []/skipped) on the SAME inputs the in-tree fixed modules
    # recover. When HEAD already carries the fix (post-commit), the reconstruction can't
    # reproduce the bug — the permanent proof above stands in.
    old_lqa_src = _git_show("app/services/lqa.py")
    old_qe_src = _git_show("app/services/qe.py")

    reconstructed = False
    if old_lqa_src and "llm_json" not in old_lqa_src:
        old_lqa = _load_module_from_source(old_lqa_src, "old_lqa_qa2")
        old_lqa.provider_is_live = lambda name: True
        old_lqa.run_completion = lambda spec, prompt, **kw: _canned(_prose(_LQA_STYLE_MAJOR))
        old_errs = old_lqa._llm_errors(
            "Buy the widget today.", "[es] Que onda!", "en-US", "es-ES", "", "mock", style_guide=""
        )
        if old_errs != []:
            _fail(("pre-fix lqa._llm_errors should DROP the prose-wrapped finding", old_errs))
        # contrast: the fixed in-tree module recovers the very same input
        new_errs = _lqa_errors(_prose(_LQA_STYLE_MAJOR))
        if len(new_errs) != 1:
            _fail(("fixed lqa must recover the prose-wrapped finding", new_errs))
        reconstructed = True
        print(
            "  5b. git HEAD reconstruction: pre-fix lqa._llm_errors DROPS the prose "
            "finding ([]), fixed in-tree module recovers it (len 1) OK"
        )

    if old_qe_src and "llm_json" not in old_qe_src:
        old_qe = _load_module_from_source(old_qe_src, "old_qe_qa2")
        old_qe.run_completion = lambda spec, prompt, **kw: _canned(_prose(_QE_COUNTS))
        old_res = old_qe.score_segment(
            ProviderSpec(provider="mock", model="m"), "s", "t", "en-US", "es-ES"
        )
        if not (old_res["skipped"] is True and old_res["reason"] == "junk_json"):
            _fail(
                (
                    "pre-fix qe.score_segment should skip the prose-wrapped counts as junk_json",
                    old_res,
                )
            )
        new_res = _qe_score(_prose(_QE_COUNTS))
        if not (new_res["skipped"] is False and new_res["score"] == 40):
            _fail(("fixed qe must score the prose-wrapped counts", new_res))
        reconstructed = True
        print(
            "  5c. git HEAD reconstruction: pre-fix qe.score_segment SKIPS the prose "
            "counts (junk_json), fixed in-tree module scores 40 OK"
        )

    if not reconstructed:
        print(
            "  5b/c. HEAD already carries the fix (post-commit) -> reconstruction "
            "skipped; the permanent proof (5a) stands in OK"
        )


# ===========================================================================
# 6. UNIFIED tolerant path (fix/llm-json-array-parse): ONE left-to-right scan with a
#    kind-agnostic TRUNCATION GUARD + VERDICT PREFERENCE + empty-shape FALLBACK (no
#    first-structural-char classifier). A prose/fence-wrapped findings ARRAY survives;
#    a TRUNCATED payload fails closed (raise) regardless of stray braces/brackets in the
#    preamble; and an incidental bracketed aside ([], [3], [here], markdown "- [ ]") never
#    pre-empts the real object/array that follows it.
# ===========================================================================
_ARR = '[{"dimension":"Accuracy","severity":"critical","note":"a"},{"dimension":"Style","severity":"major","note":"b"}]'  # noqa: E501
_OBJ_CRIT = (
    '{"errors":[{"dimension":"Accuracy","severity":"critical","note":"mistranslated the CTA"}]}'
)
# Objects whose FIRST inner array ([]) is empty, used to prove an empty INNER array does
# not fool the verdict-preference (the outer object is still verdict-shaped -> wins).
_OBJ_EMPTY_INNER_A = (
    '{"tags":[],"errors":[{"dimension":"Accuracy","severity":"critical","note":"x"}]}'
)
_OBJ_EMPTY_INNER_C = '{"tags":[],"errors":[{"dimension":"Style","severity":"major","note":"x"}]}'


def test_array_shape_matrix():
    # --- arrays that must be RECOVERED WHOLE (list of 2), not reduced to a dict ---
    for label, text in (
        ("clean fast-path", _ARR),
        ("prose-wrapped", _prose(_ARR)),
        ("fenced (+prose outside)", "Sure thing:\n" + _fenced(_ARR) + "\nHope that helps"),
        ("trailing prose", _ARR + "\n\nThat is my assessment."),
        # DEFECT-2 pin: a '{' EARLIER than the array's '[' (an ICU {count} placeholder
        # quoted from the segment, or a schema-echo) must NOT route to an object collapse
        # under the unified scan — the full findings array still wins.
        ("brace preamble ({count}) + full array", "The placeholder {count} was dropped:\n" + _ARR),
        ("schema-echo preamble + full array", 'I use the {"errors": ...} shape:\n' + _ARR),
    ):
        r = llm_json.loads(text)
        if not (isinstance(r, list) and len(r) == 2 and r[0]["dimension"] == "Accuracy"):
            _fail(("array must be recovered whole, not collapsed to a dict", label, r))

    # --- TRUNCATED payloads must RAISE, never return a dict (the load-bearing case).
    #     The guard is KIND-AGNOSTIC: a '{'/'[' in the preamble cannot defeat it. ---
    for label, text in (
        ("bare truncation", '[{"dimension":"Accuracy","severity":"critical"},{"dimension":'),
        ("truncated mid-second-value", '[{"a":1},{"b":'),
        ("truncated after a COMPLETE member", '[{"dimension":"Accuracy","severity":"critical"},'),
        ("array opener never closes", "Here are findings: [{"),
        # DEFECT-1/2 pins: preamble braces before a truncated array must STILL raise.
        ("brace preamble + TRUNCATED array", 'The {count} dropped:\n[{"a":1},{"b":'),
        ("schema-echo + TRUNCATED array", 'The {"errors": ...} shape:\n[{"a":1},{"b":'),
    ):
        try:
            got = llm_json.loads(text)
            _fail(("truncated payload must RAISE, got a value", label, type(got).__name__, got))
        except json.JSONDecodeError:
            pass  # correct: fail closed

    # a truncated array that PARSES as a dict at its first member must STILL raise —
    # assert specifically that no dict ever leaks out of a truncated findings array.
    trunc = '[{"dimension":"Accuracy","severity":"critical","note":"real finding"},{"dim'
    try:
        leaked = llm_json.loads(trunc)
        _fail(("truncated findings array must not leak a dict", type(leaked).__name__, leaked))
    except json.JSONDecodeError:
        pass

    # --- INVERTED PIN (was: "array-junk-with-later-object must raise"). Under verdict
    #     preference, a leading '[not valid json]' aside BALANCES (its '[' closes) but
    #     does not parse, so it is skipped; the later valid OBJECT is the real verdict and
    #     WINS. There is no never-closing opener, so the truncation guard does not fire. ---
    r = llm_json.loads('[not valid json] but here is {"correct": true}')
    if not (isinstance(r, dict) and r["correct"] is True):
        _fail(("array-junk-with-later-object must now recover the trailing object", r))

    # --- empty array wrapped in prose -> [] (a no-findings verdict, recovered via the
    #     empty-shape fallback since [] is not verdict-shaped). ---
    if llm_json.loads("Result:\n\n[]\n\nnothing to report") != []:
        _fail("empty array in prose must recover as []")

    # --- brackets/braces INSIDE string values must not break array balance ---
    strbr = '["a ] and [ and } and { inside", {"n": "more ] ] ["}]'
    r = llm_json.loads(strbr)
    if not (isinstance(r, list) and r[0] == "a ] and [ and } and { inside"):
        _fail(("string-embedded brackets broke array balance", r))
    # nested arrays balance (a list of lists is not verdict-shaped -> recovered via fallback)
    if llm_json.loads("prefix [[1],[2,3]] suffix") != [[1], [2, 3]]:
        _fail("nested arrays not balanced through prose")

    # --- INVERTED PIN (was: incidental "Here [1] is: {obj}" -> [1]). Under verdict
    #     preference the leading [1] is a non-verdict aside; the real OBJECT wins. ---
    r = llm_json.loads('Here [1] is the object: {"correct": true}')
    if not (isinstance(r, dict) and r["correct"] is True):
        _fail(("incidental leading [1] must lose to the real trailing object", r))

    # --- OBJECT-shaped payloads stay recoverable (unchanged behavior) ---
    # prose-wrapped object -> the object (unchanged)
    if llm_json.loads('Here it is: {"correct": true} done')["correct"] is not True:
        _fail("object-shaped prose wrap changed")
    # object-shaped with a stray brace preamble -> real object wins (unchanged)
    if llm_json.loads('like {k: v}; {"ok": 1}')["ok"] != 1:
        _fail("object-shaped stray-brace behavior changed")
    # clean object / literal fast paths byte-identical
    if llm_json.loads('{"a":1}') != {"a": 1} or llm_json.loads("42") != 42:
        _fail("object/literal fast path changed")
    print(
        "  6. unified scan matrix: prose/fenced/trailing + brace-preamble arrays recovered "
        "WHOLE; truncated payloads (kind-agnostic guard) RAISE never-a-dict; verdict "
        "preference makes the real object/array win over [ ]/[1]/[not-json] asides; "
        "empty-array-in-prose -> []; string-embedded + nested brackets balance; "
        "object/literal paths unchanged OK"
    )


# ===========================================================================
# 6a. DEFECT REGRESSIONS (round-2 gate): the three confirmed holes the unified scan
#     closes, pinned at the EXTRACTOR level and (for the fail-open ones) the LQA level.
# ===========================================================================
def test_defect_regressions():
    # (a) DEFECT-2: an incidental EMPTY-bracket / markdown-task-list aside in the preamble
    #     must NOT be recovered instead of a real errors object that carries a CRITICAL
    #     finding. Extractor -> the object; lqa -> the finding LANDS, gate is NOT auto.
    for label, text in (
        ("markdown task list", "- [ ] fluency\n- [x] accuracy\n\n" + _OBJ_CRIT),
        ("empty-bracket aside", "Reviewed tags [] and found problems:\n" + _OBJ_CRIT),
        ("obj first inner array empty, [] in prose", "Checked [] items:\n" + _OBJ_EMPTY_INNER_A),
    ):
        r = llm_json.loads(text)
        errs = r.get("errors") if isinstance(r, dict) else None
        if not (errs and errs[0]["dimension"] == "Accuracy"):
            _fail(("(a) empty-bracket aside must not pre-empt the real errors object", label, r))
        card = _lqa_card(text)
        if card["gate"] == "auto_approved":
            _fail(("(a) empty-bracket aside must NOT auto-approve a critical finding", label))
        if not any(e["dimension"] == "Accuracy" for e in card["errors"]):
            _fail(("(a) the critical finding must land in the card", label, card["errors"]))

    # (b) DEFECT-1: a {count}-style brace preamble + a BARE findings array. Complete ->
    #     the full array; truncated -> raises (never a dict). The brace must not route
    #     the truncated case to a first-member collapse.
    complete = "The placeholder {count} was dropped. Findings:\n" + _ARR
    rb = llm_json.loads(complete)
    if not (isinstance(rb, list) and len(rb) == 2):
        _fail(("(b) brace-preamble + complete array must recover the full array", rb))
    truncated_b = 'The {count} dropped:\n[{"dimension":"Accuracy","severity":"critical"},{"dim'
    try:
        leaked = llm_json.loads(truncated_b)
        _fail(("(b) brace-preamble + truncated array must RAISE, not leak", type(leaked).__name__))
    except json.JSONDecodeError:
        pass

    # (c) DEFECT-3: a '[' aside that main recovered an object past must still yield the
    #     OBJECT under verdict preference (not the aside). Includes the object-with-empty-
    #     leading-inner-array + a preceding prose '[' -> the OBJECT, not the inner [].
    qe_cite = 'Per rule [3] the counts are:\n{"accuracy":3,"fluency":0,"style":0,"rationale":"bad"}'
    if llm_json.loads(qe_cite)["accuracy"] != 3:
        _fail("(c) citation-aside + QE object must recover the object")
    r = llm_json.loads("See [above]. " + _OBJ_EMPTY_INNER_C)
    if not (isinstance(r, dict) and r["errors"][0]["dimension"] == "Style"):
        _fail(("(c) object with empty leading inner array (+ prose [) must recover the OBJECT", r))
    print(
        "  6a. defect regressions: (a) empty-bracket/task-list aside -> real errors object "
        "(finding lands, no auto-approve); (b) brace-preamble array complete->full / "
        "truncated->raise; (c) '[' asides lose to the real object OK"
    )


# ===========================================================================
# 6a2. TRUNCATION FRONTIER VOIDS THE FALLBACK (round-3 gate, R2-1): a non-verdict
#      fallback (e.g. a "[]"/task-list aside) collected BEFORE the truncation
#      frontier must NOT be returned once the scan hits a cut — otherwise a
#      truncated findings array behind such an aside surfaces as [] -> lqa reads a
#      clean pass -> JUDGE_OK/100/auto_approved. The frontier break voids the
#      fallback, so these fail closed (raise -> JUDGE_UNAVAILABLE -> needs_review).
# ===========================================================================
_CRIT_TRUNC = '[{"dimension":"Accuracy","severity":"critical","note":"mistranslated"},{"dim'


def test_truncation_frontier_voids_fallback():
    # (a)+(b) an incidental aside that PARSES to a non-verdict, followed by a TRUNCATED
    #     findings array -> loads RAISES (never returns the aside as a list/dict), and at
    #     the lqa gate -> JUDGE_UNAVAILABLE, never auto_approved.
    for label, text in (
        ("(a) task-list aside + truncated array", "- [ ] fluency\n\n" + _CRIT_TRUNC),
        ("(b) '[]' aside + truncated array", "Reviewed tags [] then:\n" + _CRIT_TRUNC),
    ):
        try:
            got = llm_json.loads(text)
            _fail(("aside + truncated array must RAISE, not surface the aside", label, got))
        except json.JSONDecodeError:
            pass
        f, _c, _ti, _to, st = _lqa_full(text)
        if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
            _fail(("aside + truncated array must degrade to JUDGE_UNAVAILABLE []", label, st, f))
        card = _lqa_card(text)
        if card["gate"] == "auto_approved":
            _fail(("aside + truncated array must NEVER auto_approve", label, card))

    # (c) CONTROL: when NOTHING is cut, the empty-array fallback still survives -> [].
    if llm_json.loads("Result:\n\n[]\n\nnothing to report") != []:
        _fail("(c) uncut empty-array-in-prose fallback must still recover as []")

    # (d) the accepted exotic fail-safe flip: an empty "[]" verdict followed by an
    #     unclosed trailing '[' (with a brace inside) is now a cut, so the "[]" fallback
    #     is voided -> RAISE. Deliberate: fail-safe, and vanishingly rare in judge output.
    try:
        got = llm_json.loads("Result: []\nSee [ref {x} ok")
        _fail(("(d) exotic '[]'+unclosed-'[' must fail closed (raise), not return []", got))
    except json.JSONDecodeError:
        pass
    print(
        "  6a2. frontier voids fallback: (a) task-list + (b) '[]' aside before a truncated "
        "array -> RAISE / JUDGE_UNAVAILABLE / never auto_approve; (c) uncut '[]' -> [] "
        "(fallback survives); (d) exotic '[]'+unclosed '[' -> raise (deliberate fail-safe) OK"
    )


# ===========================================================================
# 6b. lqa._llm_errors under the ARRAY tolerant path: a PROSE-WRAPPED findings
#     array must LAND its findings (JUDGE_OK); a TRUNCATED array must degrade to
#     JUDGE_UNAVAILABLE (fail closed) — NOT to JUDGE_OK/[]/auto_approve.
# ===========================================================================
def test_lqa_prose_and_truncated_array():
    # prose-wrapped findings array -> BOTH findings land, JUDGE_OK (pre-fix: the array
    # fell to the object scan, returned the first finding dict which has no "errors"
    # key -> JUDGE_OK with ZERO findings -> a silent auto-approve of flagged content).
    findings, _c, _ti, _to, status = _lqa_full(_prose(_LQA_ARRAY_TWO))
    if not (status == lqa.JUDGE_OK and len(findings) == 2):
        _fail(("prose-wrapped findings array must land 2 findings JUDGE_OK", status, findings))
    if sorted(e["dimension"] for e in findings) != ["Accuracy", "Style"]:
        _fail(("prose-wrapped array findings wrong", findings))
    # ... and they drive the gate (a critical error -> needs_review, not auto_approved).
    card = _lqa_card(_prose(_LQA_ARRAY_TWO))
    if not (card["passed"] is False and card["gate"] == "needs_review"):
        _fail(("prose-wrapped array must flip the gate to needs_review", card))

    # TRUNCATED findings array (finish_reason=length) -> JUDGE_UNAVAILABLE, ZERO
    # findings, and CRUCIALLY the gate is NOT auto_approved. This is the exact silent
    # auto-approve the fix closes: a cut-off judgment must never read as a clean pass.
    truncated = '[{"dimension":"Accuracy","severity":"critical","note":"mistranslated"},{"dimen'
    tf, _tc, _tti, _tto, tstatus = _lqa_full(truncated)
    if not (tstatus == lqa.JUDGE_UNAVAILABLE and tf == []):
        _fail(("truncated array must degrade to JUDGE_UNAVAILABLE []", tstatus, tf))
    tcard = _lqa_card(truncated)
    if tcard["gate"] == "auto_approved":
        _fail(("truncated findings array must NOT auto_approve", tcard))
    print(
        "  6b. lqa array path: prose-wrapped findings array LANDS 2 findings (JUDGE_OK, "
        "gate->needs_review); TRUNCATED array -> JUDGE_UNAVAILABLE [] (never auto_approve) OK"
    )


# ===========================================================================
# 7. TEMPLATE-ECHO fix (fix/lqa-template-echo, R2-3). The LQA/QE prompts print a
#    VALID-JSON template; a model that echoes it back handed the parser a real dict
#    that verdict-preference returned AHEAD of the true verdict (lqa: the placeholder
#    severity "..." scored as a benign penalty-1 minor -> auto_approved; qe: the echo's
#    all-zero counts -> score 100). Layer 1: llm_json.loads(reject=...) skips an echo
#    candidate and keeps scanning. Layer 2 (lqa): strip echoes + fail closed on off-vocab
#    severity. Layer 2 (qe): strip echoes + skip a clean echo-only.
# ===========================================================================
_LQA_TEMPLATE = '{"errors":[{"dimension":"...","severity":"...","note":"..."}]}'
_LQA_REAL_CRIT = (
    '{"errors":[{"dimension":"Accuracy","severity":"critical","note":"mistranslated the CTA"}]}'
)
_QE_TEMPLATE = (
    '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"one short sentence"}'
)
_QE_REAL = (
    '{"accuracy":3,"fluency":0,"terminology":0,"style":0,"rationale":"multiple mistranslations"}'
)


def test_reject_predicate_unit():
    # reject=None is byte-identical to omitting it across the shape matrix (the previous
    # train's shapes, re-run both ways).
    shapes = [
        _LQA_REAL_CRIT,
        _LQA_TEMPLATE,
        _QE_TEMPLATE,
        "[1,2,3]",
        '{"a":1}',
        "42",
        'Here it is: {"correct": true} done',
        _prose(_LQA_ARRAY_TWO),
        "[]",
        "Result:\n\n[]\n\nnothing",
        "prefix [[1],[2,3]] suffix",
        '[not valid json] but here is {"correct": true}',
    ]
    for s in shapes:
        try:
            a, araised = llm_json.loads(s), False
        except json.JSONDecodeError:
            araised = True
        try:
            b, braised = llm_json.loads(s, reject=None), False
        except json.JSONDecodeError:
            braised = True
        if araised != braised or (not araised and a != b):
            _fail(("reject=None must be byte-identical to the default", s))

    # a reject predicate skips a matching candidate and recovers the NEXT one.
    def _is_echo(c):
        return isinstance(c, dict) and c.get("echo") is True

    if llm_json.loads('{"echo": true} then {"real": 1}', reject=_is_echo) != {"real": 1}:
        _fail("reject must skip the echo and return the following real dict")
    # the ONLY tolerant-path candidate rejected -> raises (never returns the echo). NOTE the
    # echo is PROSE-WRAPPED so it takes the tolerant path; a CLEAN lone echo returns via the
    # fast path un-screened by design (the caller's own schema/vocab backstop handles that).
    try:
        llm_json.loads('the form is {"echo": true}', reject=_is_echo)
        _fail("an all-rejected tolerant-path reply must raise, not return the echo")
    except json.JSONDecodeError:
        pass
    # a CLEAN lone echo is NOT screened by reject (fast path) -> returned as-is (documented).
    if llm_json.loads('{"echo": true}', reject=_is_echo) != {"echo": True}:
        _fail("a clean lone value must bypass reject via the fast path (documented)")
    # a rejected non-verdict aside does not become the rule-3 fallback either.
    try:
        llm_json.loads("aside [] only", reject=lambda c: isinstance(c, list))
        _fail("a rejected [] must not be returned as the fallback")
    except json.JSONDecodeError:
        pass
    # reject does NOT rescue a truncated payload: an echo before a truncated real verdict
    # still fails closed at the frontier (rule 1 wins over a pre-frontier rejected span).
    try:
        llm_json.loads('{"echo": true} then [{"a":1},{"b":', reject=_is_echo)
        _fail("reject before the truncation frontier must not rescue a cut payload")
    except json.JSONDecodeError:
        pass
    print(
        "  7. reject predicate: None byte-identical; echo skipped -> next verdict wins; "
        "all-rejected raises; rejected never a fallback; truncation frontier still wins OK"
    )


def test_lqa_template_echo():
    # (1) echo THEN real verdict -> the REAL critical finding lands (JUDGE_OK); the gate
    #     never auto_approves (pre-fix: the echo dict won, its errors were all placeholder
    #     -> after the isinstance filter the placeholder finding scored penalty-1 minor ->
    #     score 99 >= 90 and not major/critical -> auto_approved, hiding the real finding).
    reply = "Sure, the format is " + _LQA_TEMPLATE + " and here is my verdict:\n" + _LQA_REAL_CRIT
    findings, _c, _ti, _to, st = _lqa_full(reply)
    if not (st == lqa.JUDGE_OK and len(findings) == 1 and findings[0]["dimension"] == "Accuracy"):
        _fail(("echo+real: the real critical finding must land JUDGE_OK", st, findings))
    if _lqa_card(reply)["gate"] == "auto_approved":
        _fail("echo+real: a reply carrying a critical finding must NOT auto_approve")

    # (2) prose echo ONLY -> every candidate rejected -> loads raises -> JUDGE_UNAVAILABLE.
    econly = "The format is " + _LQA_TEMPLATE
    f, _c, _ti, _to, st = _lqa_full(econly)
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(("prose echo-only must degrade to JUDGE_UNAVAILABLE []", st, f))
    if _lqa_card(econly)["gate"] == "auto_approved":
        _fail("echo-only must NOT auto_approve")

    # (2b) CLEAN echo-only (the parser's fast path returns it BEFORE the reject belt) ->
    #      the severity fail-closed backstop catches the placeholder severity "..." ->
    #      JUDGE_UNAVAILABLE (this is the layer-2b path, not the reject belt).
    f, _c, _ti, _to, st = _lqa_full(_LQA_TEMPLATE)
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(("clean echo-only must degrade to JUDGE_UNAVAILABLE via the severity guard", st, f))

    # (3) off-vocabulary severity -> unreadable verdict -> JUDGE_UNAVAILABLE.
    offvocab = '{"errors":[{"dimension":"Accuracy","severity":"moderate","note":"x"}]}'
    f, _c, _ti, _to, st = _lqa_full(offvocab)
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(("off-vocab severity must degrade to JUDGE_UNAVAILABLE", st, f))
    # a MIXED verdict (one real + one off-vocab severity) is not honestly scorable -> closed.
    mixed = (
        '{"errors":[{"dimension":"Accuracy","severity":"critical","note":"a"},'
        '{"dimension":"Style","severity":"moderate","note":"b"}]}'
    )
    _f, _c, _ti, _to, st = _lqa_full(mixed)
    if st != lqa.JUDGE_UNAVAILABLE:
        _fail(("a verdict with ANY off-vocab severity must fail closed", st))

    # (4) controls UNCHANGED: {"errors":[]} -> JUDGE_OK zero findings; an in-vocab finding
    #     lands; a judge-emitted `neutral` stays JUDGE_OK (neutral is a PENALTY key, 0).
    zf, _c, _ti, _to, zst = _lqa_full('{"errors":[]}')
    if not (zst == lqa.JUDGE_OK and zf == []):
        _fail(("empty errors must stay JUDGE_OK zero findings", zst, zf))
    nf, _c, _ti, _to, nst = _lqa_full(
        '{"errors":[{"dimension":"Accuracy","severity":"neutral","note":"x"}]}'
    )
    if not (nst == lqa.JUDGE_OK and len(nf) == 1):
        _fail(("a neutral finding must stay JUDGE_OK (in-vocab, unchanged)", nst, nf))
    print(
        "  7b. lqa template echo: echo+real -> real finding lands (no auto_approve); "
        "prose + clean echo-only -> JUDGE_UNAVAILABLE; off-vocab/mixed severity -> "
        "JUDGE_UNAVAILABLE; empty/neutral controls unchanged OK"
    )


def test_qe_template_echo():
    # echo THEN real estimate -> the REAL estimate scores (pre-fix: the all-zero echo won
    # and scored 100, hiding the real low score).
    reply = "The form is " + _QE_TEMPLATE + " and my estimate:\n" + _QE_REAL
    r = _qe_score(reply)
    if not (r["skipped"] is False and r["score"] == 40):
        _fail(("qe echo+real must score the REAL estimate (40)", r))
    # prose echo-only -> rejected -> loads raises -> junk_json skip.
    r = _qe_score("The form is " + _QE_TEMPLATE)
    if not (r["skipped"] is True and r["reason"] == "junk_json"):
        _fail(("qe prose echo-only must skip as junk_json", r))
    # CLEAN echo-only -> the post-parse backstop skips it (never a free 100).
    r = _qe_score(_QE_TEMPLATE)
    if not (r["skipped"] is True and r["reason"] == "junk_json"):
        _fail(("qe clean echo-only must skip as junk_json, never score 100", r))
    # control: a REAL all-zero verdict with a real rationale is still a legit clean pass.
    r = _qe_score(
        '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"clean and accurate"}'
    )
    if not (r["skipped"] is False and r["score"] == 100):
        _fail(("a real clean verdict must still score 100", r))
    # L2: an echo that appends trailing sentence punctuation ("one short sentence.") must
    # STILL be recognized as the template (the rationale marker strips trailing .!… before
    # comparing) -> junk_json skip, not a free 100.
    r = _qe_score(
        '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"One short sentence."}'
    )
    if not (r["skipped"] is True and r["reason"] == "junk_json"):
        _fail(("qe trailing-punctuation echo must still skip as junk_json", r))
    print(
        "  7c. qe template echo: echo+real -> real score (40); prose/clean echo-only + "
        "trailing-punctuation echo -> junk_json (never a free 100); real verdict -> 100 OK"
    )


def test_caller_templates_non_parsing():
    # Every OTHER llm_json.loads caller prints a template that is NOT valid JSON, so an echo
    # of it cannot parse into a spurious verdict (survey: glossary/tag_llm_repair/
    # assist). Pin each: (a) the extractor RAISES on the literal, and (b) the literal still
    # lives in the module that builds the prompt -- so a future edit turning a template into
    # valid JSON (opening the echo hole) trips (a), and one that rewords/moves it trips (b).
    import inspect

    from app.services import glossary, tag_llm_repair

    def _must_raise(literal, where):
        try:
            got = llm_json.loads(literal)
            _fail(
                ("caller template must be non-parsing (an echo cannot form a verdict)", where, got)
            )
        except json.JSONDecodeError:
            pass

    checks = [
        ('{"index": <n>}', '{"index": <n>}', glossary, "glossary._choose_sense"),
        (
            '{"correct": true or false, "note": "<short reason when false>"}',
            '{"correct": true or false, "note": "<short reason when ',
            tag_llm_repair,
            "tag_llm_repair._verify_prompt",
        ),
    ]
    for parse_literal, source_literal, module, where in checks:
        _must_raise(parse_literal, where)
        # 2026-09-14 SRP splits: the caller may live in a sibling piece re-exported by `module`
        owner = getattr(module, where.split(".", 1)[1], None)
        owning = sys.modules[owner.__module__] if owner else module
        source = inspect.getsource(owning)
        if source_literal not in source:
            _fail(("caller template drifted -- re-verify it is still non-parsing", where))
    # assist prints two schema CONSTANTS (valid-looking but NOT valid JSON) -> non-parsing.
    print(
        "  7d. caller-template survey: glossary/tag_llm_repair templates are "
        "non-parsing (an echo cannot form a verdict) and still present in their prompts OK"
    )


def test_lqa_partial_placeholder_lands():
    # D1 (round-2, regression vs the round-1 commit): a REAL finding with ONE lazy placeholder
    # field (a "..." note beside a real dimension + severity) must NOT be classed as template
    # noise. Round 1 used any(field == "...") so it rejected such a finding; with any competing
    # parsing candidate later in the reply (a stray {} example, a {"errors":[]}, a markdown
    # "- [ ]"), the scan then promoted THAT weaker candidate -> {"errors":[]} -> JUDGE_OK
    # score 100 auto_approved, discarding the real critical finding. all() keeps the finding.
    lazy = '{"errors":[{"dimension":"Accuracy","severity":"critical","note":"..."}]}'
    for label, text in (
        ("stray {} example after", lazy + "\nExample: {}"),
        ("empty-errors candidate after", lazy + '\nIf none: {"errors":[]}'),
        ("markdown task-list [ ] before", "- [ ] fluency\n\n" + lazy),
        ("clean, no competitor (fast path)", lazy),
    ):
        findings, _c, _ti, _to, st = _lqa_full(text)
        if not (st == lqa.JUDGE_OK and len(findings) == 1):
            _fail(
                (
                    "D1: the lazy-note critical finding must LAND, not be rejected",
                    label,
                    st,
                    findings,
                )
            )
        if findings[0]["dimension"] != "Accuracy" or findings[0]["severity"] != "critical":
            _fail(("D1: the real finding's fields must survive", label, findings))
        card = _lqa_card(text)
        if not (card["gate"] == "needs_review" and card["score"] == 90):
            _fail(("D1: a landed critical finding must drive needs_review score 90", label, card))
    print(
        "  7e. D1 partial-placeholder: a real critical finding with a lazy '...' note LANDS "
        "(not rejected) even with a competing candidate -> needs_review score 90 OK"
    )


def test_lqa_severity_normalization():
    # D2 (round-2): _score reads the RAW severity value, so a merely-uncanonical severity
    # ("CRITICAL", "Major", " major ") would PASS the fail-closed guard (it is in PENALTY after
    # strip/lower) yet score as a benign penalty-1 NON-major minor -> score 99 auto_approved.
    # The guard now writes the canonical (stripped/lowered) key back onto the finding, so it
    # scores correctly AND persists lowercase.
    for label, raw, want_sev, want_score in (
        ("CRITICAL", "CRITICAL", "critical", 90),
        ("Major", "Major", "major", 95),
        ("' major '", " major ", "major", 95),
    ):
        text = '{"errors":[{"dimension":"Accuracy","severity":"' + raw + '","note":"x"}]}'
        findings, _c, _ti, _to, st = _lqa_full(text)
        if not (st == lqa.JUDGE_OK and len(findings) == 1 and findings[0]["severity"] == want_sev):
            _fail(
                ("D2: severity must normalize to the canonical lowercase key", label, st, findings)
            )
        card = _lqa_card(text)
        if not (card["gate"] == "needs_review" and card["score"] == want_score):
            _fail(
                ("D2: normalized severity must score correctly + drive needs_review", label, card)
            )
    # a genuinely off-vocabulary severity is NOT rescued by normalization -> still fail closed.
    f, _c, _ti, _to, st = _lqa_full(
        '{"errors":[{"dimension":"Accuracy","severity":"moderate","note":"x"}]}'
    )
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(("D2: a genuinely off-vocab severity must still fail closed", st, f))
    print(
        "  7f. D2 severity normalization: CRITICAL/Major/' major ' -> canonical lowercase key "
        "scored 90/95/95 needs_review; 'moderate' -> JUDGE_UNAVAILABLE OK"
    )


# ===========================================================================
# 8. STRAY NON-VERDICT DICT (fix/lqa-stray-dict, R2-3 sibling, families emptyobj/straydict).
#    llm_json's verdict-preference returns the FIRST dict (or list with >=1 dict). A stray
#    PARSING dict in the judge's preamble that is NOT a template echo -- `Example: {}`, a
#    quoted `{"a":1}` -- would therefore win AHEAD of the real verdict emitted later.
#      * lqa: the stray dict carries no "errors" key -> data.get("errors",[]) == [] -> zero
#        findings -> JUDGE_OK score 100 auto_approve, a SILENT pass of flagged content.
#      * qe: the stray dict carries no category counts -> _score_from_categories reads every
#        count as 0 -> a free score 100, hiding the real estimate later in the reply.
#    Layer 1 (tolerant path): the WIDENED reject belt (_reject_non_verdict /
#    _reject_qe_non_estimate) skips ANY non-verdict dict so the real verdict after it wins.
#    Layer 2 (clean fast path): a post-parse backstop fails closed on a lone stray dict, which
#    the parser's fast path returns BEFORE the belt runs (the belt does not screen the fast
#    path by design). The template-echo arm (section 7) and the array path (3b/6b) are the
#    same doctrine; these pin the stray-non-echo-dict families the reject widening now closes.
#
#    GATE ROUND 1 (findings 2/3/4/6): the value-shape bars, tightened from the round-1 bar.
#      * lqa: a dict is a verdict only with a LIST-valued "errors" -- {"errors":"none"},
#        {"errors":{"x":1}}, {"errors":5} are non-verdict (they iterate to a false clean pass
#        and mask a real verdict as a preamble).
#      * qe: an estimate needs at least one QE_CATEGORIES key whose value is a COUNT (_is_count)
#        -- {"style":"formal"}, {"accuracy":"lots"}, {"terminology":null}, {"accuracy":[]} are
#        non-estimate (they score a free 100 via _score_from_categories' `or 0` swallow).
#      * finding 6 (qe, documented): category-key matching is case-sensitive lowercase; an
#        uppercase-keyed reply fails closed to a soft skip (safe direction).
#      * finding 4 (both, PINNED as a residual, NOT a desired endpoint): a stray dict wrapped in
#        a LIST ([{"a":1}]) before a real verdict is returned by llm_json rule 2, so the dict
#        reject belt never sees it; it still fails CLOSED downstream (never a false pass), but
#        the real verdict after it does not land.
# ===========================================================================
_LQA_LAZY_NOTE = '{"errors":[{"dimension":"Accuracy","severity":"critical","note":"..."}]}'


def test_lqa_stray_dict():
    # (1) a stray dict ({} or a quoted {"a":1}) in the preamble BEFORE the real critical
    #     verdict -> the real finding LANDS (JUDGE_OK) and the gate is NOT auto_approved.
    #     Pre-fix the stray dict won: no "errors" key -> JUDGE_OK 0 findings -> score 100
    #     auto_approved, silently passing flagged content.
    for label, preamble in (
        ("empty {} example", "Example: {}\n"),
        ('quoted {"a":1} aside', 'Consider {"a":1} as an aside.\n'),
    ):
        text = preamble + _LQA_REAL_CRIT
        findings, _c, _ti, _to, st = _lqa_full(text)
        if not (
            st == lqa.JUDGE_OK and len(findings) == 1 and findings[0]["dimension"] == "Accuracy"
        ):
            _fail(
                ("(1) stray dict + real verdict: the real finding must land", label, st, findings)
            )
        card = _lqa_card(text)
        if not (card["gate"] == "needs_review" and card["score"] == 90):
            _fail(("(1) stray dict must NOT auto_approve a critical finding", label, card))

    # (2) a stray dict + a real TOP-LEVEL ARRAY verdict -> BOTH findings land (the list
    #     normalization is still reached after the stray dict is skipped).
    text = "Example: {}\n" + _LQA_ARRAY_TWO
    findings, _c, _ti, _to, st = _lqa_full(text)
    if not (st == lqa.JUDGE_OK and len(findings) == 2):
        _fail(("(2) stray dict + array verdict must land both findings", st, findings))
    if sorted(e["dimension"] for e in findings) != ["Accuracy", "Style"]:
        _fail(("(2) both array findings must survive past the stray dict", findings))
    if _lqa_card(text)["gate"] == "auto_approved":
        _fail("(2) stray dict + array verdict must NOT auto_approve")

    # (3) stray dict + template echo + the real verdict in ONE reply (real last) -> the real
    #     verdict wins: the widened belt rejects BOTH the stray {} and the pure-placeholder echo.
    text = "Aside: {}\nThe format is " + _LQA_TEMPLATE + "\nMy verdict:\n" + _LQA_REAL_CRIT
    findings, _c, _ti, _to, st = _lqa_full(text)
    if not (st == lqa.JUDGE_OK and len(findings) == 1 and findings[0]["dimension"] == "Accuracy"):
        _fail(("(3) stray + echo + real: the real verdict must win", st, findings))
    if _lqa_card(text)["gate"] == "auto_approved":
        _fail("(3) stray + echo + real must NOT auto_approve")

    # (4) prose stray-ONLY (no verdict) -> every candidate rejected -> loads raises ->
    #     JUDGE_UNAVAILABLE, never auto_approve.
    for label, text in (
        ("prose empty {}", "Example: {}"),
        ('prose quoted {"a":1}', 'Consider {"a":1} only.'),
    ):
        f, _c, _ti, _to, st = _lqa_full(text)
        if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
            _fail(("(4) prose stray-only must degrade to JUDGE_UNAVAILABLE []", label, st, f))
        if _lqa_card(text)["gate"] == "auto_approved":
            _fail(("(4) prose stray-only must NOT auto_approve", label))

    # (5) CLEAN stray-only reply (the parser's fast path returns it BEFORE the reject belt) ->
    #     the post-parse "errors" not in data backstop fails closed -> JUDGE_UNAVAILABLE.
    for label, text in (
        ("clean {}", "{}"),
        ('clean {"note":...}', '{"note":"looks fine"}'),
    ):
        f, _c, _ti, _to, st = _lqa_full(text)
        if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
            _fail(("(5) clean stray-only must degrade via the post-parse backstop", label, st, f))
        if _lqa_card(text)["gate"] == "auto_approved":
            _fail(("(5) clean stray-only must NOT auto_approve", label))

    # (6) controls UNCHANGED (the widening must not touch any legit shape).
    #   clean {"errors":[]} -> JUDGE_OK []
    zf, _c, _ti, _to, zst = _lqa_full('{"errors":[]}')
    if not (zst == lqa.JUDGE_OK and zf == []):
        _fail(("(6) clean empty-errors must stay JUDGE_OK []", zst, zf))
    #   prose-wrapped {"errors":[]} -> JUDGE_OK [] (the empty verdict is kept by the belt)
    pf, _c, _ti, _to, pst = _lqa_full(_prose('{"errors":[]}'))
    if not (pst == lqa.JUDGE_OK and pf == []):
        _fail(("(6) prose-wrapped empty-errors must stay JUDGE_OK []", pst, pf))
    #   clean top-level [] -> JUDGE_OK [] (normalized to {"errors": []} before the backstop)
    ef, _c, _ti, _to, est = _lqa_full("[]")
    if not (est == lqa.JUDGE_OK and ef == []):
        _fail(("(6) clean empty array must stay JUDGE_OK []", est, ef))
    #   an EXTRA-KEY verdict {"tags":[],"errors":[...]} still lands its finding (NOT rejected
    #   as a stray dict -- it has the "errors" key).
    xf, _c, _ti, _to, xst = _lqa_full(_OBJ_EMPTY_INNER_A)
    if not (xst == lqa.JUDGE_OK and len(xf) == 1 and xf[0]["dimension"] == "Accuracy"):
        _fail(("(6) extra-key {tags,errors} verdict must still land its finding", xst, xf))
    if _lqa_card(_OBJ_EMPTY_INNER_A)["gate"] != "needs_review":
        _fail("(6) the extra-key verdict's critical finding must drive needs_review")
    #   the D1 lazy-note finding (a "..." note beside a real dimension/severity) still LANDS,
    #   with a stray {} competitor BEFORE it (widened belt skips {}) and AFTER it (belt/scan
    #   returns the real finding first). Neither ordering may drop the real critical finding.
    for label, text in (
        ("stray {} before lazy note", "Example: {}\n" + _LQA_LAZY_NOTE),
        ("stray {} after lazy note", _LQA_LAZY_NOTE + "\nExample: {}"),
    ):
        f, _c, _ti, _to, st = _lqa_full(text)
        if not (st == lqa.JUDGE_OK and len(f) == 1 and f[0]["severity"] == "critical"):
            _fail(
                ("(6) D1 lazy-note finding must LAND despite a stray {} competitor", label, st, f)
            )
        if _lqa_card(text)["score"] != 90:
            _fail(("(6) the landed lazy-note critical must drive score 90", label))

    # (7) NON-LIST "errors" shapes (gate round 1, finding 2): {"errors":<non-list>} is not a
    #     verdict -- iterating it yields zero findings (a false clean pass), and as a preamble it
    #     MASKS the real verdict / can launder a truncated payload. Both layers reject it.
    #   preamble {"errors":"none"} + the real critical verdict -> the real finding LANDS.
    text = 'Note {"errors":"none"} first.\n' + _LQA_REAL_CRIT
    findings, _c, _ti, _to, st = _lqa_full(text)
    if not (st == lqa.JUDGE_OK and len(findings) == 1 and findings[0]["dimension"] == "Accuracy"):
        _fail(
            (
                '(7) {"errors":"none"} preamble + real verdict: the real finding must land',
                st,
                findings,
            )
        )
    if _lqa_card(text)["gate"] == "auto_approved":
        _fail('(7) {"errors":"none"} preamble must NOT auto_approve a critical finding')
    #   prose {"errors":"none"} alone -> rejected -> loads raises -> JUDGE_UNAVAILABLE.
    f, _c, _ti, _to, st = _lqa_full('The verdict: {"errors":"none"}')
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(('(7) prose {"errors":"none"}-only must degrade to JUDGE_UNAVAILABLE []', st, f))
    #   CLEAN non-list-errors shapes -> the errors-shape backstop fails closed (str/dict/int
    #   "errors" is not a list). {"errors":5} was pre-fix an INCIDENTAL TypeError via the broad
    #   except; the isinstance guard now makes it an EXPLICIT fail-closed at the backstop.
    for label, text in (
        ('clean {"errors":"none"}', '{"errors":"none"}'),
        ('clean {"errors":{"x":1}}', '{"errors":{"x":1}}'),
        ('clean {"errors":5}', '{"errors":5}'),
    ):
        f, _c, _ti, _to, st = _lqa_full(text)
        if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
            _fail(("(7) clean non-list-errors must fail closed via the backstop", label, st, f))
        if _lqa_card(text)["gate"] == "auto_approved":
            _fail(("(7) clean non-list-errors must NOT auto_approve", label))

    # (8) FINDING 4 residual PIN (documents CURRENT behavior, NOT a desired endpoint): a stray
    #     dict WRAPPED IN A LIST ([{"a":1}]) before a real verdict is returned by llm_json rule 2
    #     (a list with >=1 dict IS verdict-shaped), so the dict reject belt never sees it; it
    #     fails CLOSED downstream (the empty severity is off-vocab) -> JUDGE_UNAVAILABLE, never
    #     auto_approved. The real verdict after it does NOT land -- safe direction, out of this
    #     fix's dict-family scope.
    text = '[{"a":1}]\n' + _LQA_REAL_CRIT
    f, _c, _ti, _to, st = _lqa_full(text)
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(("(8) finding-4 list-wrapped stray must fail CLOSED to JUDGE_UNAVAILABLE []", st, f))
    if _lqa_card(text)["gate"] == "auto_approved":
        _fail("(8) finding-4 list-wrapped stray must NEVER auto_approve")

    # (9) ALL-NON-DICT errors list (gate round 2, FIX 1): {"errors":[<all strings/ints>]} has a
    #     LIST "errors" (passes the isinstance backstop) but ZERO dict findings -> the per-item
    #     filter keeps none -> pre-fix a false JUDGE_OK/100/auto_approve. The IDENTICAL content
    #     as a bare top-level array already failed closed (all-non-dict guard); the wrapped shape
    #     (what the prompt requests) must too. Both layers now fail closed.
    _WRAP_NONDICT = '{"errors":["Accuracy: mistranslated the CTA","Style: too casual"]}'
    #   clean -> the all-non-dict post-parse arm -> JUDGE_UNAVAILABLE.
    f, _c, _ti, _to, st = _lqa_full(_WRAP_NONDICT)
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(("(9) clean {errors:[all-non-dict]} must fail CLOSED to JUDGE_UNAVAILABLE []", st, f))
    if _lqa_card(_WRAP_NONDICT)["gate"] == "auto_approved":
        _fail("(9) clean {errors:[all-non-dict]} must NEVER auto_approve")
    #   as a preamble BEFORE the real verdict -> the reject belt skips it, the real finding LANDS.
    text = _WRAP_NONDICT + "\nMy verdict:\n" + _LQA_REAL_CRIT
    findings, _c, _ti, _to, st = _lqa_full(text)
    if not (st == lqa.JUDGE_OK and len(findings) == 1 and findings[0]["dimension"] == "Accuracy"):
        _fail(
            (
                "(9) {errors:[all-non-dict]} preamble + real verdict: the real finding must land",
                st,
                findings,
            )
        )
    if _lqa_card(text)["gate"] == "auto_approved":
        _fail("(9) {errors:[all-non-dict]} preamble must NOT auto_approve a critical finding")
    #   as a preamble BEFORE a TRUNCATED findings array -> no laundering: the rejected preamble
    #   is not a fallback and the cut voids it -> loads raises -> JUDGE_UNAVAILABLE.
    truncated = (
        _WRAP_NONDICT + '\n[{"dimension":"Accuracy","severity":"critical","note":"real"},{"dim'
    )
    f, _c, _ti, _to, st = _lqa_full(truncated)
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(
            ("(9) {errors:[all-non-dict]} before a truncated array must NOT launder the cut", st, f)
        )
    if _lqa_card(truncated)["gate"] == "auto_approved":
        _fail("(9) laundered-truncation shape must NEVER auto_approve")
    #   MIXED-LIST boundary control: {"errors":[{real dict},"junk string"]} still LANDS its dict
    #   finding (the per-item isinstance filter drops the junk) -> only the ZERO-dict list closes.
    mixed = (
        '{"errors":[{"dimension":"Accuracy","severity":"critical","note":"real"},"junk string"]}'
    )
    findings, _c, _ti, _to, st = _lqa_full(mixed)
    if not (st == lqa.JUDGE_OK and len(findings) == 1 and findings[0]["dimension"] == "Accuracy"):
        _fail(
            ("(9) mixed errors list (dict + junk) must still land its dict finding", st, findings)
        )
    if _lqa_card(mixed)["gate"] != "needs_review":
        _fail("(9) the mixed-list dict finding (critical) must drive needs_review")
    print(
        "  8a. lqa stray dict: stray {}/{'a':1} + real verdict (dict/array) -> real finding(s) "
        "LAND (no auto_approve); stray+echo+real -> real wins; prose/CLEAN stray-only -> "
        "JUDGE_UNAVAILABLE; NON-LIST errors (none/{}/5) preamble->real lands, clean->backstop "
        "fail-closed; ALL-NON-DICT errors list clean->UNAVAILABLE / preamble->real lands / "
        "before-truncated->no-launder, mixed-list dict finding LANDS; finding-4 list-wrapped "
        "stray -> JUDGE_UNAVAILABLE (residual pin); controls (empty-errors/[]/extra-key/D1) OK"
    )


def test_qe_stray_dict():
    # stray dict ({} or a quoted {"a":1}) in the preamble + the real estimate -> the REAL
    # estimate scores (40 for _QE_REAL). Pre-fix the stray dict won: no category counts ->
    # _score_from_categories reads all-zero -> a free 100, hiding the real low score.
    for label, preamble in (
        ("empty {} example", "Example: {}\n"),
        ('quoted {"a":1} aside', 'Consider {"a":1} first.\n'),
    ):
        r = _qe_score(preamble + _QE_REAL)
        if not (r["skipped"] is False and r["score"] == 40):
            _fail(("qe stray dict + real estimate must score the real estimate (40)", label, r))

    # prose stray-ONLY -> rejected -> loads raises -> junk_json (never a free 100).
    for label, text in (
        ("prose empty {}", "Example: {}"),
        ('prose quoted {"a":1}', 'Consider {"a":1} only.'),
    ):
        r = _qe_score(text)
        if not (r["skipped"] is True and r["reason"] == "junk_json"):
            _fail(("qe prose stray-only must skip as junk_json", label, r))

    # CLEAN stray-ONLY (the parser's fast path returns it BEFORE the reject belt) -> the
    # post-parse no-category backstop skips it as junk_json, never a free 100.
    for label, text in (
        ("clean {}", "{}"),
        ('clean {"note":...}', '{"note":"looks fine"}'),
    ):
        r = _qe_score(text)
        if not (r["skipped"] is True and r["reason"] == "junk_json" and r["score"] is None):
            _fail(("qe clean stray-only must skip as junk_json (never score 100)", label, r))

    # controls UNCHANGED:
    #  (a) the §6a(c) partial-category shape (missing "terminology") is a REAL estimate ->
    #      still scores (accuracy 3 -> 40): the bar is ">=1 category key", not all four.
    r = _qe_score('{"accuracy":3,"fluency":0,"style":0,"rationale":"bad"}')
    if not (r["skipped"] is False and r["score"] == 40):
        _fail(("control: partial-category verdict (missing terminology) must still score 40", r))
    #  (b) a real clean all-zero verdict WITH a real rationale is still a legit 100.
    r = _qe_score(
        '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"clean and accurate"}'
    )
    if not (r["skipped"] is False and r["score"] == 100):
        _fail(("control: a real clean verdict must still score 100", r))
    #  (c) the trailing-punctuation template echo still skips (the section-7 arm, unchanged).
    r = _qe_score(
        '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"One short sentence."}'
    )
    if not (r["skipped"] is True and r["reason"] == "junk_json"):
        _fail(("control: trailing-punctuation echo must still skip as junk_json", r))

    # NON-COUNT category values (gate round 1, finding 3): a category-NAMED key whose value is
    # not a count ({"style":"formal"}, {"accuracy":"lots"}, {"terminology":null}, {"accuracy":[]})
    # scores a free 100 via _score_from_categories' `or 0` swallow. The bar is a COUNT-valued key
    # (_is_count, strictly no `or 0`), so both layers reject these clean-fast-path shapes.
    for label, text in (
        ('clean {"style":"formal"}', '{"style":"formal"}'),
        ('clean {"accuracy":"lots"}', '{"accuracy":"lots"}'),
        ('clean {"terminology":null}', '{"terminology":null}'),
        ('clean {"accuracy":[]}', '{"accuracy":[]}'),
    ):
        r = _qe_score(text)
        if not (r["skipped"] is True and r["reason"] == "junk_json" and r["score"] is None):
            _fail(
                ("qe clean non-count category value must skip as junk_json (never 100)", label, r)
            )
    # {"style":"formal"} as a preamble MASKS a real estimate -> the reject belt now lets the real
    # one win -> score 40 (pre-round-1 the non-count key `c in cand` would have kept the stray).
    r = _qe_score('Aside {"style":"formal"} then:\n' + _QE_REAL)
    if not (r["skipped"] is False and r["score"] == 40):
        _fail(("qe non-count preamble + real estimate must score the real estimate (40)", r))

    # FINDING 6 asymmetry PIN (documented, deliberate): category-key matching is case-sensitive
    # lowercase; an uppercase-keyed reply has no lowercase count key -> fails closed to a soft qe
    # skip (junk_json), the SAFE direction (the parent gate gave this shape a bogus 100).
    r = _qe_score('{"Accuracy":3,"Fluency":0,"rationale":"bad"}')
    if not (r["skipped"] is True and r["reason"] == "junk_json"):
        _fail(("qe uppercase-keyed reply must fail closed to junk_json (finding-6 asymmetry)", r))

    # FINDING 4 residual PIN (qe analog, documents CURRENT behavior): a stray dict WRAPPED IN A
    # LIST ([{"a":1}]) before a real estimate is returned by llm_json rule 2, so the reject belt
    # never sees it; score_segment fails CLOSED on the non-dict (junk_json) and the real estimate
    # after it does NOT land. Safe direction, out of this fix's dict-family scope.
    r = _qe_score('[{"a":1}]\n' + _QE_REAL)
    if not (r["skipped"] is True and r["reason"] == "junk_json"):
        _fail(("qe finding-4 list-wrapped stray must fail closed to junk_json", r))

    # BOOLEAN category values (gate round 2, FIX 2): `bool` subclasses `int`, so a pre-fix
    # `int(False)`/`int(True)` coerced {"accuracy":false} to a free 100 and {"accuracy":true} to
    # 80. `_is_count` now excludes bools FIRST -> these are not estimates -> junk_json, never a
    # score. A JSON true/false is never an MQM error count.
    for label, text in (
        ('clean {"accuracy":false}', '{"accuracy":false}'),
        ('clean {"terminology":false}', '{"terminology":false}'),
        ('clean {"accuracy":true}', '{"accuracy":true}'),
    ):
        r = _qe_score(text)
        if not (r["skipped"] is True and r["reason"] == "junk_json" and r["score"] is None):
            _fail(
                ("qe clean boolean category value must skip as junk_json (never a score)", label, r)
            )
    # control: a REAL zero count ({"accuracy":0,...}) is a genuine estimate and still scores 100
    # (the bool guard must not catch an integer 0 -- 0 is False-y but not a bool).
    r = _qe_score('{"accuracy":0,"rationale":"clean"}')
    if not (r["skipped"] is False and r["score"] == 100):
        _fail(("control: a real zero-count verdict must still score 100 (not caught as a bool)", r))
    print(
        "  8b. qe stray dict: stray {}/{'a':1} + real estimate -> real score (40); prose + "
        "CLEAN stray-only -> junk_json (never a free 100); NON-COUNT category values "
        "(formal/lots/null/[]) + BOOLEAN values (false/true) -> junk_json, preamble->real lands; "
        "uppercase-key -> junk_json (finding-6 asymmetry); finding-4 list-wrapped stray -> "
        "junk_json (residual pin); controls (partial 40 / real clean 100 / zero-count 100 / "
        "trailing-punct echo skip) unchanged OK"
    )


# ===========================================================================
# 9. NO-POST-FRONTIER-SPAN TRUNCATION (fix/llm-json-nospan-frontier). The 6a2 guard
#    voided the rule-3 fallback only via a `cut` flag set INSIDE the span loop when a
#    BALANCED span began at/after the frontier. A payload truncated so that EVERY opener
#    after the frontier stays unclosed (e.g. "aside []\n{\"errors\":[{...crit") produces
#    NO post-frontier balanced span at all, so `cut` never flipped and a pre-frontier
#    aside ([]) was returned -> lqa read JUDGE_OK/100/auto_approved for a segment whose
#    findings were cut off mid-CRITICAL. The fix gates the fallback on `unclosed is None`
#    (any never-closing opener => truncation => fail closed), which strictly generalizes
#    the 6a2 pin. This section pins the no-span family closed AND the controls open.
# ===========================================================================
# Each ends in a findings object/array whose opener (and every opener after it) is cut
# off mid-value, so the ONLY balanced span is the leading non-verdict aside.
_NOSPAN_REPROS = (
    (
        "repro1: '[]' aside + truncated errors dict",
        'Reviewed tags [] and found problems:\n{"errors":[{"dimension":"Accuracy","severity":"crit',
    ),
    ("repro2: '[]' aside + truncated errors dict (short)", 'Result: []\n{"errors":[{"dim'),
    (
        "repro3: markdown task-list asides + truncated errors dict",
        '- [ ] fluency\n- [x] accuracy\n{"errors":[{"dimension":"Accuracy","severity":"crit',
    ),
)


def test_nospan_frontier_fallback_void():
    # (a) the three flagship repros -> loads RAISES (never returns the leading aside). Pre-fix
    #     each returned [] because no post-frontier balanced span ever set the `cut` flag.
    for label, text in _NOSPAN_REPROS:
        try:
            got = llm_json.loads(text)
            _fail(("(a) no-span truncation must RAISE, not surface the aside", label, got))
        except json.JSONDecodeError:
            pass

    # (b) the flagship repro at the LQA gate -> JUDGE_UNAVAILABLE, zero findings, and
    #     CRUCIALLY not auto_approved (the exact silent auto-approve the fix closes).
    _flag = _NOSPAN_REPROS[0][1]
    f, _c, _ti, _to, st = _lqa_full(_flag)
    if not (st == lqa.JUDGE_UNAVAILABLE and f == []):
        _fail(("(b) no-span truncation must degrade to JUDGE_UNAVAILABLE []", st, f))
    card = _lqa_card(_flag)
    if card["gate"] == "auto_approved":
        _fail(("(b) no-span truncation must NEVER auto_approve", card))

    # (c) the truncation families, all with NO post-frontier balanced span -> RAISE.
    families = (
        # no-span dict cut, openers all unclosed, no leading aside at all
        ("dict cut, no aside", '{"errors":[{"dimension":"Accuracy","severity":"crit'),
        # array cut behind a '[]' aside (bracket frontier)
        ("array cut behind '[]' aside", 'Result: []\n[{"dimension":"Accuracy","severity":"crit'),
        # mixed opener kinds: '[ ]' aside, then brace->bracket->brace all cut
        (
            "mixed kinds ('[ ]' aside + brace/bracket/brace cut)",
            'Checklist [ ] done\n{"verdict":"fail","items":[{"sev":"crit',
        ),
        # aside-before-cut: a task-list aside then a truncated top-level findings ARRAY
        (
            "task-list aside + truncated array",
            '- [ ] fluency\n\n[{"dimension":"Accuracy","severity":"crit',
        ),
    )
    for label, text in families:
        try:
            got = llm_json.loads(text)
            _fail(("(c) truncation family must RAISE", label, got))
        except json.JSONDecodeError:
            pass

    # (d) CONTROLS that must KEEP returning their value (unclosed is None => no truncation):
    #     an uncut '[]' in prose, a clean prose-wrapped '[]', and a real pre-frontier verdict
    #     dict followed by trailing truncated junk (rule 2 returns the dict before the cut).
    if llm_json.loads("Result: [] nothing to report") != []:
        _fail("(d) uncut 'Result: [] nothing to report' must still recover as []")
    if llm_json.loads(_prose("[]")) != []:
        _fail("(d) clean prose-wrapped '[]' must still recover as []")
    verdict_then_cut = llm_json.loads('{"errors":[]}\nAlso {"cut')
    if verdict_then_cut != {"errors": []}:
        _fail(
            ("(d) pre-frontier verdict dict + trailing cut must return the dict", verdict_then_cut)
        )
    print(
        "  9. no-post-frontier-span truncation: (a) 3 repros RAISE; (b) flagship -> "
        "JUDGE_UNAVAILABLE [] / never auto_approve; (c) dict/array/mixed/aside-before cut "
        "families RAISE; (d) uncut '[]', prose '[]', pre-frontier verdict+trailing-cut survive OK"
    )


def main():
    test_extractor_unit()
    test_lqa_seam()
    test_lqa_toplevel_array()
    test_lqa_array_revert_proof()
    test_qe_seam()
    test_b3_style_path()
    test_fail_before_reconstruction()
    test_array_shape_matrix()
    test_defect_regressions()
    test_truncation_frontier_voids_fallback()
    test_lqa_prose_and_truncated_array()
    test_reject_predicate_unit()
    test_lqa_template_echo()
    test_qe_template_echo()
    test_caller_templates_non_parsing()
    test_lqa_partial_placeholder_lands()
    test_lqa_severity_normalization()
    test_lqa_stray_dict()
    test_qe_stray_dict()
    test_nospan_frontier_fallback_void()
    print("JUDGE JSON OK")


if __name__ == "__main__":
    main()
