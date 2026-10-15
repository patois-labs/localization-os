"""Draft-repair verification ("Fix this for me"), offline + hermetic.

The review screen's confirm can fail validation (a branch target that names no step,
a prompt missing {text}, ...). "Fix this for me" takes the CURRENT (possibly hand-
edited) review-form state + the exact confirm-path errors, calls the SAME drafting
engine with a tight repair prompt, re-validates the corrected draft with the SAME
confirm validation (nothing saved), and re-renders the review page with the corrected
values, a per-step 'repaired' badge, and any REMAINING errors (button kept -- one repair
PASS per click; a pass is up to 2 provider calls with one JSON-parse retry). CLI parity:
`workflow author --create --auto-fix [N]` runs the same seam up to N times.

Covers:
  1. service: repair fixes a prompt-{text} error in one call (diff marks the field).
  2. service: repair fixes the canonical branch-target error (add-and-key a step).
  3. service: a user's hand-edit that the repair leaves alone is PRESERVED (not dropped,
     not badged) -- the diff applies onto the CURRENT state.
  3c. service: a repair whose model OMITS lqa_threshold RESTORES the user's pass bar
      (#452: the bar was silently reset to 90); the serializer emits it so the model sees it.
  4. service: a repair that STILL fails validation returns honest REMAINING errors.
  5. service: tolerant JSON parse of a fenced/prose-wrapped repair response.
  6. service: a repaired draft that reintroduces a dead-for-type field is STRIPPED-and-
     WARNED, not errored (requirement 7).
  7. service: garbage repair output -> exactly ONE repair retry then a clean 502.
  8. service: spend-cap blocked -> clean message, ZERO provider calls.
  9. service: apply_workflow_draft(validate_only=True) saves NOTHING (valid + invalid).
  10. web: /workflows/author/repair fixes + re-renders with the badge; a confirm
      rejection renders the "Fix this for me" button wired to the repair route.
  11. web: a provider error during repair NEVER dead-ends -- the page re-renders with the
      current form state preserved + a clean message + the button kept (requirement 5).
  12. web: RBAC -- a translator (no can_tune_ai) is 403 on /workflows/author/repair.
  13. CLI: `--auto-fix` repairs then creates; `--auto-fix N` is bounded (refuses cleanly,
      creates nothing, exactly N repair calls) when unresolved.

Provider wiring goes through the REAL OpenAICompatibleProvider with an injected
httpx.MockTransport (the promptable-workflow fixture pattern), registered via
register_provider so the service, CLI, and web routes all resolve it. ASCII-only.
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_draftrepair_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.cli.__main__ import main as cli_main
from app.db import engine as db_engine
from app.db import init_db
from app.models import SpendLedger, Workflow, WorkflowStep
from app.services import settings, setup_author, spend
from app.services.engines import create_profile, get_profile
from app.services.providers import (
    OpenAICompatibleProvider,
    register_provider,
)
from app.services.setup_author_helpers import _serialize_workflow_draft

init_db()


# --- transports (openai chat-completions wire shape) -------------------------
def json_transport(responses, counter):
    def h(req):
        i = counter["n"]
        counter["n"] += 1
        content_str = responses[min(i, len(responses) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content_str}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(h)


_ENGINE_SEQ = {"n": 0}


def make_engine(responses, counter):
    _ENGINE_SEQ["n"] += 1
    p = create_profile(
        f"RepairLLM{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(p.id), transport=json_transport(responses, counter)
        ),
    )
    return ref


def _session():
    return Session(db_engine)


def _wf_step_count():
    with _session() as s:
        wf = s.exec(select(func.count()).select_from(Workflow)).one()
        st = s.exec(select(func.count()).select_from(WorkflowStep)).one()
        return wf, st


_APP = None


def app_ref():
    global _APP
    if _APP is None:
        from app.main import app

        _APP = app
    return _APP


# --- payload builders (the flat review-form shape apply/repair re-validate) ---
def _step(step_type="llm_agent", prompt_text="", **kw):
    base = {
        "name": "",
        "step_type": step_type,
        "provider": "",
        "model": "",
        "fallback_provider": "",
        "fallback_model": "",
        "prompt_text": prompt_text,
        "assignee_email": "",
        "glossary_id": None,
        "qe_threshold": "70",
        "qe_provider": "",
        "tm_examples_window": "3",
        "key": None,
        "max_visits": None,
        "needs_review": False,
        "qe_gate": False,
        "tm_examples": True,
        "run_condition": None,
        "branch": None,
    }
    base.update(kw)
    return base


def _payload(
    steps,
    name="Repair WF",
    customer_id=None,
    source="en-US",
    targets=("es-ES",),
    adapt=False,
    notes="",
):
    return {
        "name": name,
        "customer_id": customer_id,
        "source_locale": source,
        "target_locales": list(targets),
        "steps": steps,
        "notes_for_reviewer": notes,
        "adapt": adapt,
    }


def _repair(payload, errors, ref, **kw):
    with _session() as s:
        return setup_author.repair_workflow_draft(
            s, payload=payload, errors=errors, provider=ref, org_id=None, actor="tester", **kw
        )


def _current_errors(payload):
    """The authoritative confirm-path errors on `payload` (validate_only, nothing saved)."""
    with _session() as s:
        try:
            setup_author.apply_workflow_draft(
                s, payload, org_id=None, actor="tester", validate_only=True
            )
        except setup_author.WorkflowApplyError as exc:
            return list(exc.errors)
    return []


# --- 1. repair fixes a prompt-{text} error in one call -----------------------
def check_repair_fixes_prompt():
    payload = _payload([_step(prompt_text="Translate to Spanish.")])  # no {text}
    errors = _current_errors(payload)
    assert errors and any("{text}" in e for e in errors), errors
    fixed = (
        '{"name": "Repair WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"name": "", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to Spanish."}]}'
    )
    counter = {"n": 0}
    ref = make_engine([fixed], counter)
    result = _repair(payload, errors, ref)
    assert counter["n"] == 1, ("one repair click == one provider call", counter["n"])
    assert result["errors"] == [], ("the repair resolved every error", result["errors"])
    s0 = result["fields"]["steps"][0]
    assert "{text}" in s0["prompt_text"], s0["prompt_text"]
    assert "prompt_text" in s0["repaired_fields"], (
        "the changed field must be marked repaired",
        s0["repaired_fields"],
    )
    print("  1. repair fixes a prompt-{text} error in ONE call; prompt_text marked repaired")


# --- 2. repair fixes the canonical branch-target error -----------------------
def check_repair_fixes_branch_target():
    # Step 2 (lqa) branches on_fail to a step keyed 'fix' that does not exist -> the exact
    # error from the spec. The repair adds+keys a fix step (or retargets); we return a
    # draft that KEYS the (new) fix step so the branch resolves.
    payload = _payload(
        [
            _step(prompt_text="Translate {text} to Spanish."),
            _step(step_type="lqa", branch={"on_fail": {"step": "fix"}}),
        ]
    )
    errors = _current_errors(payload)
    assert errors and any("does not exist" in e for e in errors), errors
    # Corrected: reorder to translate -> lqa1(key lqa1) -> fix(key fix, guarded) so the
    # branch target 'fix' exists earlier than any loop-back. Simplest faithful fix here:
    # drop the dangling branch to 'next' (a minimal correction the model may choose).
    fixed = (
        '{"name": "Repair WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"step_type": "llm_agent", "prompt_text": "Translate {text}."}, '
        '{"step_type": "lqa", "branch": {"on_fail": "next"}}]}'
    )
    counter = {"n": 0}
    ref = make_engine([fixed], counter)
    result = _repair(payload, errors, ref)
    assert result["errors"] == [], ("the branch error must be resolved", result["errors"])
    s1 = result["fields"]["steps"][1]
    assert "branch" in s1["repaired_fields"], (
        "the branch change is marked repaired",
        s1["repaired_fields"],
    )
    print(
        "  2. repair resolves the canonical 'branch target does not exist' error; "
        "branch marked repaired"
    )


# --- 3. hand-edits survive a NON-COMPLIANT model that OMITS fields (F1 merge-back) --
def check_repair_preserves_user_edits():
    # step0 (llm_agent) + step1 (human_review) carry hand-edits; step2 is the SOLE error.
    payload = _payload(
        [
            _step(
                name="MyTranslator", prompt_text="Translate {text} to Spanish.", tm_examples=False
            ),  # a user-toggled-OFF bool (validation would default it ON)
            _step(step_type="human_review", name="MyReviewer", assignee_email="linguist@firm.com"),
            _step(prompt_text="Fix all errors."),  # no {text} -> the sole error
        ]
    )
    errors = _current_errors(payload)
    assert errors and len(errors) == 1, errors
    # A NON-COMPLIANT repair: it fixes step2's prompt but OMITS name/tm_examples on step0
    # and name/assignee_email on step1 (the exact data-loss shape F1 reproduced). Merge-back
    # must RESTORE those from the current state -- the guarantee is structural, not model-
    # dependent.
    non_compliant = (
        '{"name": "Repair WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"step_type": "llm_agent", "prompt_text": "Translate {text} to Spanish."}, '
        '{"step_type": "human_review"}, '
        '{"step_type": "llm_agent", "prompt_text": "Fix all errors in {prior}."}]}'
    )
    ref = make_engine([non_compliant], {"n": 0})
    result = _repair(payload, errors, ref)
    assert result["errors"] == [], result["errors"]
    s0, s1, s2 = result["fields"]["steps"]
    assert s0["name"] == "MyTranslator", (
        "an OMITTED name must be restored, not blanked",
        s0["name"],
    )
    assert s0["tm_examples"] is False, (
        "an OMITTED user-off bool must be restored (not defaulted back ON)",
        s0["tm_examples"],
    )
    assert s1["name"] == "MyReviewer", ("an OMITTED name on step 2 must be restored", s1["name"])
    assert s1["assignee_email"] == "linguist@firm.com", (
        "an OMITTED assignee_email must be restored, not dropped",
        s1["assignee_email"],
    )
    assert s0["repaired_fields"] == [] and s1["repaired_fields"] == [], (
        "restored (unchanged) steps must NOT be badged repaired",
        s0["repaired_fields"],
        s1["repaired_fields"],
    )
    assert "prompt_text" in s2["repaired_fields"], s2["repaired_fields"]
    print(
        "  3. F1 merge-back: a non-compliant model that OMITS name/assignee_email/bool "
        "cannot erase hand-edits -- they are RESTORED (not badged); only the fixed step "
        "is marked repaired"
    )


# --- 3c. lqa_threshold survives a repair whose model OMITS it (#452, F1 merge-back) --
def check_repair_preserves_lqa_threshold():
    # The user set a 94 pass bar on the lqa step. Confirm fails for an UNRELATED reason
    # (step0's prompt has no {text}); the repair model returns the corrected draft but
    # OMITS lqa_threshold on the lqa step. Merge-back must RESTORE 94 -- the edited pass
    # bar is preserved STRUCTURALLY, not model-dependently -- and it must NOT read as a
    # "repair" (the value never changed). FAILS before the fix: it comes back None (90).
    payload = _payload(
        [
            _step(prompt_text="Translate to Spanish."),  # no {text} -> the sole error
            _step(step_type="lqa", lqa_threshold=94),  # the user's custom pass bar
        ]
    )
    errors = _current_errors(payload)
    assert errors and any("{text}" in e for e in errors), errors
    # The lqa step comes back WITHOUT lqa_threshold -- the exact #452 shape: the serializer
    # never showed the model the 94, so a minimal repair drops the field entirely.
    non_compliant = (
        '{"name": "Repair WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"step_type": "llm_agent", "prompt_text": "Translate {text} to Spanish."}, '
        '{"step_type": "lqa"}]}'
    )
    ref = make_engine([non_compliant], {"n": 0})
    result = _repair(payload, errors, ref)
    assert result["errors"] == [], result["errors"]
    s1 = result["fields"]["steps"][1]
    assert s1["lqa_threshold"] == 94, (
        "an OMITTED lqa_threshold must be restored to the user's 94, not reset to "
        f"None (inherit 90): {s1['lqa_threshold']!r}",
    )
    assert "lqa_threshold" not in s1["repaired_fields"], (
        "a restored (unchanged) pass bar must NOT be badged repaired",
        s1["repaired_fields"],
    )
    print(
        "  3c. #452 F1 merge-back: a repair whose model OMITS lqa_threshold RESTORES the "
        "user's 94 (not None/90) and does not badge it repaired"
    )


# --- 3d. the serializer emits lqa_threshold so the model can see it (#452) ----------
def check_serialize_emits_lqa_threshold():
    # The repair prompt grounds on _serialize_workflow_draft's output; if the pass bar is
    # absent there the model never sees it and cannot return it. Emit it (None when unset).
    fields = {
        "name": "Ser WF",
        "customer_id": None,
        "steps": [
            {"step_type": "lqa", "name": "Quality", "lqa_threshold": 94},
            {"step_type": "llm_agent", "name": "Translate"},  # no bar set -> None
        ],
        "notes_for_reviewer": "",
    }
    out = _serialize_workflow_draft(fields, {}, {})
    assert "lqa_threshold" in out["steps"][0], (
        "the serialized contract must carry lqa_threshold so the repair model sees it",
        sorted(out["steps"][0]),
    )
    assert out["steps"][0]["lqa_threshold"] == 94, out["steps"][0]["lqa_threshold"]
    assert out["steps"][1]["lqa_threshold"] is None, (
        "a step with no custom bar serializes to None (inherit 90)",
        out["steps"][1]["lqa_threshold"],
    )
    print(
        "  3d. _serialize_workflow_draft emits lqa_threshold (94 kept; unset -> None) so "
        "the repair model sees the pass bar"
    )


# --- 3b. a repair that returns FEWER steps is surfaced honestly (F2) ----------
def check_repair_fewer_steps_surfaced():
    payload = _payload(
        [
            _step(name="Translate", prompt_text="Translate {text} to Spanish."),
            _step(name="Review", prompt_text="Fix all errors."),  # no {text} -> the error
        ]
    )
    errors = _current_errors(payload)
    # The repair DROPS the "Review" step entirely, returning only the valid "Translate".
    dropped = (
        '{"name": "Repair WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"name": "Translate", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to Spanish."}]}'
    )
    ref = make_engine([dropped], {"n": 0})
    result = _repair(payload, errors, ref)
    assert len(result["fields"]["steps"]) == 1, result["fields"]["steps"]
    assert result["removed_notes"], (
        "a repair that drops a step must surface a notice",
        result["removed_notes"],
    )
    assert any("Review" in n for n in result["removed_notes"]), result["removed_notes"]
    print(
        "  3b. F2: a repair that returns FEWER steps surfaces an honest 'removed step' "
        "notice naming the dropped step"
    )


# --- 4. a repair that STILL fails returns honest remaining errors -------------
def check_repair_still_fails():
    payload = _payload([_step(prompt_text="Translate to Spanish.")])  # no {text}
    errors = _current_errors(payload)
    # The engine returns a STILL-broken draft (prompt still omits {text}).
    still_bad = (
        '{"name": "Repair WF", "source_locale": "en-US", '
        '"target_locale": "es-ES", "steps": [{"step_type": "llm_agent", '
        '"prompt_text": "Translate to Spanish now."}]}'
    )
    counter = {"n": 0}
    ref = make_engine([still_bad], counter)
    result = _repair(payload, errors, ref)
    assert counter["n"] == 1, ("still exactly one call (no auto-loop in the seam)", counter["n"])
    assert result["errors"] and any("{text}" in e for e in result["errors"]), (
        "a still-invalid repair must return the REMAINING errors honestly",
        result["errors"],
    )
    print("  4. a repair that still fails returns honest remaining errors (no auto-loop)")


# --- 5. tolerant JSON parse of a fenced/prose-wrapped repair response ---------
def check_repair_tolerant_parse():
    payload = _payload([_step(prompt_text="Translate to Spanish.")])
    errors = _current_errors(payload)
    wrapped = (
        "Sure! Here is the corrected workflow draft:\n\n```json\n"
        '{"name": "Repair WF", "source_locale": "en-US", '
        '"target_locale": "es-ES", "steps": [{"step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to Spanish."}]}\n```\nHope that helps!'
    )
    counter = {"n": 0}
    ref = make_engine([wrapped], counter)
    result = _repair(payload, errors, ref)
    assert counter["n"] == 1, ("a wrapped-but-recoverable response needs NO retry", counter["n"])
    assert result["errors"] == [], result["errors"]
    assert "{text}" in result["fields"]["steps"][0]["prompt_text"]
    print("  5. a fenced + prose-wrapped repair response is parsed tolerantly (no retry)")


# --- 6. a repaired draft reintroducing a dead-for-type field is STRIP-and-WARN -
def check_repair_dead_field_stripped():
    # The current state: step0 poisoned (real error), step1 an lqa (fine). The repair
    # fixes step0 BUT turns qe_gate=true on the lqa step (a dead field for lqa). That is
    # STRIP-AND-WARN at re-validation, NOT an error -> result.errors == [], a strip warning.
    payload = _payload(
        [
            _step(prompt_text="Translate to Spanish."),  # no {text}
            _step(step_type="lqa"),
        ]
    )
    errors = _current_errors(payload)
    fixed = (
        '{"name": "Repair WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"step_type": "llm_agent", "prompt_text": "Translate {text}."}, '
        '{"step_type": "lqa", "qe_gate": true, "qe_threshold": 90}]}'
    )
    ref = make_engine([fixed], {"n": 0})
    result = _repair(payload, errors, ref)
    assert result["errors"] == [], (
        "a dead-for-type field on the repaired draft must be STRIPPED, not errored",
        result["errors"],
    )
    assert any("qe routing gate" in w.lower() for w in result["warnings"]), (
        "the dead-field strip must surface a visible warning",
        result["warnings"],
    )
    print(
        "  6. a repaired draft that reintroduces a dead-for-type field is STRIPPED + "
        "warned, not errored (requirement 7)"
    )


# --- 7. garbage repair output -> exactly ONE retry then a clean 502 ----------
def check_repair_retry_then_502():
    payload = _payload([_step(prompt_text="Translate to Spanish.")])
    errors = _current_errors(payload)
    counter = {"n": 0}
    ref = make_engine(["not json", "still not { json"], counter)
    raised = False
    try:
        _repair(payload, errors, ref)
    except setup_author.SetupAuthorError as exc:
        raised = True
        assert exc.status == 502, exc.status
    assert raised, "unparseable repair output after a retry must raise SetupAuthorError"
    assert counter["n"] == 2, ("EXACTLY one repair retry: 2 provider calls", counter["n"])
    print("  7. garbage repair output -> exactly ONE repair retry then a clean 502")


# --- 8. spend-cap blocked -> clean message, ZERO provider calls ---------------
def check_repair_spend_block():
    payload = _payload([_step(prompt_text="Translate to Spanish.")])
    errors = _current_errors(payload)
    period = spend.period_key()
    settings.set_many({"spend_cap_usd": "1.0"})
    with _session() as s:
        row = s.get(SpendLedger, period) or SpendLedger(period=period, accumulated_usd=0.0)
        row.accumulated_usd = 50.0
        s.add(row)
        s.commit()
    try:
        counter = {"n": 0}
        ref = make_engine(['{"name": "x", "steps": []}'], counter)
        raised = False
        try:
            _repair(payload, errors, ref)
        except setup_author.SetupAuthorError as exc:
            raised = True
            assert "cap" in str(exc).lower(), str(exc)
        assert raised, "an over-cap real provider must be refused before calling"
        assert counter["n"] == 0, ("a spend-blocked repair makes ZERO calls", counter["n"])
    finally:
        settings.set_many({"spend_cap_usd": "0"})
        with _session() as s:
            row = s.get(SpendLedger, period)
            if row is not None:
                row.accumulated_usd = 0.0
                s.add(row)
                s.commit()
    print("  8. spend-cap blocked repair -> clean message, ZERO provider calls")


# --- 9. apply_workflow_draft(validate_only=True) saves NOTHING ----------------
def check_validate_only_saves_nothing():
    before = _wf_step_count()
    # (a) a VALID payload validates but creates nothing (returns None).
    good = _payload([_step(prompt_text="Translate {text}.")], name="ValOnly Good")
    with _session() as s:
        out = setup_author.apply_workflow_draft(
            s, good, org_id=None, actor="tester", validate_only=True
        )
    assert out is None, ("validate_only returns None (no workflow row)", out)
    # (b) an INVALID payload raises WorkflowApplyError and creates nothing.
    bad = _payload([_step(prompt_text="no placeholder")], name="ValOnly Bad")
    raised = False
    with _session() as s:
        try:
            setup_author.apply_workflow_draft(
                s, bad, org_id=None, actor="tester", validate_only=True
            )
        except setup_author.WorkflowApplyError:
            raised = True
    assert raised, "validate_only still rejects an invalid draft"
    assert _wf_step_count() == before, (
        "validate_only creates ZERO rows either way",
        _wf_step_count(),
        before,
    )
    print(
        "  9. apply_workflow_draft(validate_only=True) validates but saves NOTHING "
        "(valid -> None; invalid -> raises), zero rows"
    )


# --- 10. web: /repair fixes + re-renders with the badge; confirm shows button --
def check_web_repair_and_button():
    # (a) a confirm rejection renders the "Fix this for me" button wired to /repair, with
    # the drafting engine relayed as a hidden input.
    client = TestClient(app_ref())
    r = client.post(
        "/workflows/author/confirm",
        data={
            "step_count": "1",
            "name": "Web Repair WF",
            "source_locale": "en-US",
            "target_locales": ["es-ES"],
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate to Spanish.",  # no {text}
            "provider": "engine:999",
            "notes_for_reviewer": "",
        },
    )
    assert r.status_code == 200 and "could not be created" in r.text, r.status_code
    assert 'formaction="/workflows/author/repair"' in r.text, (
        "the review page must render the Fix-this-for-me button wired to /repair"
    )
    assert 'name="provider"' in r.text, "the drafting engine must ride as a hidden input"

    # (b) POST /repair with a real engine -> the draft is fixed + re-rendered with the badge.
    fixed = (
        '{"name": "Web Repair WF", "source_locale": "en-US", '
        '"target_locale": "es-ES", "steps": [{"step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to Spanish."}]}'
    )
    counter = {"n": 0}
    ref = make_engine([fixed], counter)
    r = client.post(
        "/workflows/author/repair",
        data={
            "step_count": "1",
            "name": "Web Repair WF",
            "source_locale": "en-US",
            "target_locales": ["es-ES"],
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate to Spanish.",
            "provider": ref,
            "notes_for_reviewer": "",
        },
    )
    assert r.status_code == 200, r.status_code
    assert counter["n"] == 1, ("the web repair makes exactly one provider call", counter["n"])
    assert "Repaired." in r.text and "repaired-step" in r.text, (
        "the repaired page must show the repaired note + a badged step"
    )
    assert "Translate {text} to Spanish." in r.text, "the fixed prompt must be rendered"
    # Step-header hierarchy (the maintainer's P1: unmistakable "Step N" while scrolling a long
    # workflow) must coexist cleanly with the repaired badge on the SAME legend --
    # the badge is still attached, just inside the new bigger/bolder header.
    assert 'class="lp-fieldset wf-step-fieldset repaired-step"' in r.text, (
        "a repaired step's fieldset must carry BOTH the step-header class and "
        "the repaired-step class"
    )
    assert '<span class="wf-step-num">Step 1</span>' in r.text, (
        "the repaired step must still show its visible step-number label"
    )
    assert (
        '<span class="wf-step-num">Step 1</span><span class="chip '
        'wf-step-type-chip">llm_agent</span> <span class="badge semantic">'
        "repaired</span>"
    ) in r.text, (
        "the step number, the type chip, AND the repaired badge must all render "
        "together on the same legend"
    )
    print(
        "  10. web: confirm rejection shows the Fix-this-for-me button; POST /repair "
        "fixes + re-renders with the repaired badge (one call); the repaired badge "
        "coexists with the new step-number header + type chip"
    )


# --- 11. web: a repair provider error NEVER dead-ends (state preserved) -------
def check_web_repair_provider_error_preserves_state():
    # The engine returns garbage twice -> a clean 502-class SetupAuthorError inside repair.
    # The route must RE-RENDER the review page (200) with the current form state + a clean
    # message + the button kept -- never a dead-end error page (requirement 5).
    counter = {"n": 0}
    ref = make_engine(["nope", "nope again"], counter)
    client = TestClient(app_ref())
    r = client.post(
        "/workflows/author/repair",
        data={
            "step_count": "1",
            "name": "Web Repair Err",
            "source_locale": "en-US",
            "target_locales": ["es-ES"],
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate to Spanish.",
            "provider": ref,
            "notes_for_reviewer": "",
        },
    )
    assert r.status_code == 200, ("a repair provider error must re-render, not 502", r.status_code)
    assert "Automatic repair could not run" in r.text, "the clean repair-failure message"
    assert "Translate to Spanish." in r.text, (
        "the current form state must be preserved on a repair error"
    )
    assert 'formaction="/workflows/author/repair"' in r.text, (
        "the Fix-this-for-me button must remain so the user can retry"
    )
    print(
        "  11. web: a repair provider error re-renders with state preserved + a clean "
        "message + the button kept (no dead-end error page)"
    )


# --- 12. web: RBAC -- a translator is 403 on /workflows/author/repair ---------
def check_web_repair_rbac():
    from app.services import security
    from tests._csrf import csrf_for

    security.seed_initial()
    client = TestClient(app_ref())
    csrf_for(client)
    r = client.post(
        "/auth/login",
        data={"email": "user1", "password": "user1", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, ("user1 login", r.status_code)
    csrf_for(client)
    r = client.post(
        "/workflows/author/repair", data={"step_count": "0", "name": "x", "source_locale": "en-US"}
    )
    assert r.status_code == 403, ("a translator must be 403 on the repair route", r.status_code)
    print("  12. web: RBAC -- a translator (no can_tune_ai) is 403 on /workflows/author/repair")


# --- 13. CLI: --auto-fix repairs+creates; --auto-fix N is bounded ------------
def check_cli_auto_fix():
    # The draft is POISONED (prompt missing {text}); --auto-fix repairs it in one round
    # then --create succeeds. One engine serves BOTH the authoring draft and the repair.
    bad_draft = (
        '{"name": "CLI AutoFix WF", "source_locale": "en-US", '
        '"target_locale": "es-ES", "steps": [{"name": "T", '
        '"step_type": "llm_agent", "prompt_text": "Translate to Spanish."}]}'
    )
    fixed = (
        '{"name": "CLI AutoFix WF", "source_locale": "en-US", '
        '"target_locale": "es-ES", "steps": [{"name": "T", '
        '"step_type": "llm_agent", "prompt_text": "Translate {text} to Spanish."}]}'
    )
    counter = {"n": 0}
    ref = make_engine([bad_draft, fixed], counter)
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = cli_main(
            ["workflow", "author", "en to es", "--provider", ref, "--create", "--auto-fix"]
        )
    out = buf.getvalue() + err.getvalue()
    assert rc == 0, ("--auto-fix repairs then creates (exit 0)", rc, out)
    assert "auto-fix round 1" in out, ("each round is logged", out)
    assert "repaired:" in out, ("the round prints what changed", out)
    with _session() as s:
        wf = s.exec(select(Workflow).where(Workflow.name == "CLI AutoFix WF")).first()
        assert wf is not None, "--auto-fix must create the workflow once repaired"
        st = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf.id)).all()
        assert len(st) == 1 and "{text}" in (st[0].prompt_text or ""), (
            "the created step carries the repaired prompt",
            st[0].prompt_text,
        )
    # bounded: the repair keeps returning a STILL-broken draft; --auto-fix 1 refuses
    # cleanly (non-zero), creates nothing, exactly author(1) + repair(1) = 2 calls.
    counter2 = {"n": 0}
    ref2 = make_engine([bad_draft, bad_draft, bad_draft], counter2)
    before = _wf_step_count()
    buf2, err2 = io.StringIO(), io.StringIO()
    with redirect_stdout(buf2), redirect_stderr(err2):
        rc = cli_main(
            ["workflow", "author", "en to es", "--provider", ref2, "--create", "--auto-fix", "1"]
        )
    out2 = buf2.getvalue() + err2.getvalue()
    assert rc != 0, ("an unresolved --auto-fix must refuse cleanly (non-zero)", rc, out2)
    assert counter2["n"] == 2, ("--auto-fix 1 == 1 author + 1 repair call (bounded)", counter2["n"])
    assert _wf_step_count() == before, (
        "a refused --auto-fix creates nothing",
        _wf_step_count(),
        before,
    )
    print(
        "  13. CLI: --auto-fix repairs then creates; --auto-fix N is bounded (refuses "
        "cleanly, zero rows, exactly N repair calls)"
    )


def main() -> None:
    check_repair_fixes_prompt()
    check_repair_fixes_branch_target()
    check_repair_preserves_user_edits()
    check_repair_preserves_lqa_threshold()
    check_serialize_emits_lqa_threshold()
    check_repair_fewer_steps_surfaced()
    check_repair_still_fails()
    check_repair_tolerant_parse()
    check_repair_dead_field_stripped()
    check_repair_retry_then_502()
    check_repair_spend_block()
    check_validate_only_saves_nothing()
    check_web_repair_and_button()
    check_web_repair_provider_error_preserves_state()
    check_cli_auto_fix()
    check_web_repair_rbac()  # LAST: flips auth ON globally
    print("DRAFT REPAIR OK")


if __name__ == "__main__":
    main()
