"""Design-a-workflow ASSISTANT step-type parity (BACKLOG 2026-08-31, the maintainer's #1).

THE BUG: the web "Design a workflow" assistant (draft -> review -> confirm) rejected the
Batch 15 `tm_match` step type with "Step 1: unknown step type 'tm_match'" while the CLI and
the manual builder accepted it. Root cause: a PRIVATE whitelist in setup_author
(`_WF_STEP_TYPES`) predating StepType.TM_MATCH / StepType.EXTERNAL -- so the drafter
silently DEFAULTED tm_match to llm_agent and the confirm path HARD-rejected it, and rejected
`external` the same way with the same misleading "unknown step type" message.

THE RATCHET (the load-bearing pin below): setup_author's ACCEPTED types
(`_WF_STEP_TYPES`) UNION its explicit EXCLUSIONS (`_ASSISTANT_UNSUPPORTED`) must equal
`app.models.StepType.ALL` EXACTLY. So the day someone adds a sixth StepType, THIS suite
fails in CI until the assistant surface makes a deliberate accept-or-exclude choice for it
-- it can never again be silently defaulted to llm_agent.

Also proves, through the REAL seams (in-memory fixture session + the web confirm route):
  * a tm_match step DRAFTS as tm_match (never rewritten to llm_agent);
  * `external` at draft/confirm gets the HONEST "add it in the builder" message that names
    the surface where it CAN be added -- NOT "unknown step type" (junk stays "unknown");
  * `apply_workflow_draft` with a tm_match step CREATES a workflow whose step is tm_match;
  * dead engine fields (provider/prompt/QE) POSTed on a tm_match step STRIP-AND-WARN
    (SPEC-lqa-step-honest-options) instead of exploding in add_step mid-apply;
  * the web /workflows/author/confirm route creates a tm_match step end-to-end, and its
    review dropdown offers tm_match but NOT external.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider). ASCII-only.
Final line: "WORKFLOW AUTHOR PARITY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_wfauthorparity_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Role, StepType, Workflow, WorkflowStep
from app.services import apikeys, security, setup_author
from app.services import workflows as wf_svc
from app.services.engines import create_profile, get_profile
from app.services.providers import OpenAICompatibleProvider, register_provider

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- deterministic authoring engine (the connector-test fixture pattern, mirrored from
# verify_promptable_workflow): a REAL OpenAICompatibleProvider whose transport returns a
# canned draft JSON, registered so the /api route resolves it. Lets the headless author
# route (LOW-3) draft a tm_match step deterministically, offline + free.
_ENGINE_SEQ = {"n": 0}


def _json_transport(responses, counter):
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


def _make_engine(responses, counter):
    _ENGINE_SEQ["n"] += 1
    p = create_profile(
        f"ParityWFEngine{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(p.id), transport=_json_transport(responses, counter)
        ),
    )
    return ref


def _steps_of(name):
    """Every WorkflowStep of the workflow named `name`, in order_index order."""
    with Session(engine) as s:
        wf = s.exec(select(Workflow).where(Workflow.name == name)).first()
        if wf is None:
            return None, []
        rows = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf.id)
            .order_by(WorkflowStep.order_index)
        ).all()
        return wf, rows


# ============================================================================
# 1. THE RATCHET: accepted UNION excluded == StepType.ALL (exactly), disjoint.
# ============================================================================
def test_invariant_accepted_union_excluded_is_all():
    accepted = set(setup_author._WF_STEP_TYPES)
    excluded = set(setup_author._ASSISTANT_UNSUPPORTED)
    allset = set(StepType.ALL)
    if accepted | excluded != allset:
        missing = allset - (accepted | excluded)
        extra = (accepted | excluded) - allset
        _fail(
            "assistant accept-set UNION exclude-set must equal StepType.ALL exactly "
            f"(unclassified new type(s): {sorted(missing)}; non-canonical: {sorted(extra)}) "
            "-- classify every StepType as draftable or explicitly excluded"
        )
    if accepted & excluded:
        _fail(f"a type is both accepted AND excluded: {sorted(accepted & excluded)}")
    if StepType.TM_MATCH not in accepted:
        _fail("tm_match must be an ACCEPTED (draftable) assistant step type")
    if StepType.EXTERNAL not in excluded:
        _fail("external must be an EXPLICITLY EXCLUDED assistant step type (honest message)")
    print(
        "  (1) ratchet: _WF_STEP_TYPES | _ASSISTANT_UNSUPPORTED == StepType.ALL, disjoint; "
        "tm_match accepted, external excluded: OK"
    )


# ============================================================================
# 2. DRAFT validation: tm_match survives as itself; external is honestly excluded;
#    genuine junk keeps the warn-and-default behavior.
# ============================================================================
def test_draft_tm_match_survives_as_itself():
    step, _warnings = setup_author._validate_step_draft(
        {"name": "TM prefill", "step_type": "tm_match"}, 0, [], []
    )
    if step["step_type"] != "tm_match":
        _fail(f"a drafted tm_match step must survive as tm_match, got {step['step_type']!r}")
    # Case-fold + hyphen normalization still resolves to the canonical value.
    step2, _w2 = setup_author._validate_step_draft(
        {"name": "x", "step_type": "TM-Match"}, 0, [], []
    )
    if step2["step_type"] != "tm_match":
        _fail(f"'TM-Match' must normalize to tm_match, got {step2['step_type']!r}")
    print("  (2a) draft: a tm_match step survives as tm_match (never rewritten to llm_agent): OK")


def test_draft_external_honest_not_junk():
    step, warnings = setup_author._validate_step_draft(
        {"name": "vendor leg", "step_type": "external"}, 0, [], []
    )
    # external is not draftable here: it falls back to a valid editable step, but the
    # warning is the HONEST "add it in the builder" message, NOT "not recognized".
    if step["step_type"] != "llm_agent":
        _fail(f"a drafted external step should fall back to llm_agent, got {step['step_type']!r}")
    joined = " ".join(warnings).lower()
    if "builder" not in joined:
        _fail(f"external's draft warning must name the builder path, got {warnings!r}")
    if "not recognized" in joined:
        _fail("external is RECOGNIZED (not junk) -- it must not say 'not recognized'")
    # MED-1: the warning must DISCLOSE the substitution (mirrors the junk path's
    # "defaulting to llm_agent"), so the human knows the step became an llm_agent.
    if "changed to llm_agent" not in joined:
        _fail(
            f"external's draft warning must disclose the llm_agent substitution, got {warnings!r}"
        )
    print(
        "  (2b) draft: external falls back with the HONEST 'add it in the builder' warning "
        "(not the junk 'not recognized'): OK"
    )


def test_draft_junk_defaults_with_unrecognized_warning():
    step, warnings = setup_author._validate_step_draft(
        {"name": "x", "step_type": "frobnicate"}, 0, [], []
    )
    if step["step_type"] != "llm_agent":
        _fail("a genuinely unknown junk type must still default to llm_agent")
    if "not recognized" not in " ".join(warnings).lower():
        _fail(f"an unknown junk type must warn 'not recognized', got {warnings!r}")
    print("  (2c) draft: a genuine junk type keeps the warn-and-default-to-llm_agent path: OK")


# ============================================================================
# 3. CONFIRM (apply_workflow_draft) through the real create seams.
# ============================================================================
def test_apply_creates_a_real_tm_match_step():
    payload = {
        "name": "Parity TM-first WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "tm_match", "name": "TM prefill"},
            {"step_type": "llm_agent", "prompt_text": "Translate {text} into {target_locale}."},
        ],
    }
    with Session(engine) as s:
        setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
    wf, rows = _steps_of("Parity TM-first WF")
    if wf is None:
        _fail("apply_workflow_draft must CREATE the workflow for a tm_match-first draft")
    if not rows or rows[0].step_type != StepType.TM_MATCH:
        _fail(f"step 1 must be a real tm_match step, got {[r.step_type for r in rows]}")
    if rows[1].step_type != StepType.LLM_AGENT:
        _fail("the following translate step must remain llm_agent")
    print("  (3a) confirm: a tm_match-first draft creates a workflow whose step 1 IS tm_match: OK")


def test_apply_tm_match_strips_dead_engine_fields_and_warns():
    # The review form ALWAYS posts provider/model/prompt/qe fields; on a tm_match step every
    # one of them is dead. They must STRIP-AND-WARN (SPEC-lqa-step-honest-options), never
    # hard-reject or explode in add_step mid-apply.
    payload = {
        "name": "Parity TM dead-fields WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        # prompt_text + qe_gate are BOTH dead on tm_match and need no provider-availability
        # lookup, so this asserts the strip machinery independent of the offered-engine set.
        "steps": [
            {
                "step_type": "tm_match",
                "name": "TM prefill",
                "prompt_text": "Do {text}",
                "qe_gate": True,
            },
        ],
    }
    strip_warnings: list = []
    with Session(engine) as s:
        setup_author.apply_workflow_draft(
            s, payload, org_id=None, actor="tester", warnings=strip_warnings
        )
    if not strip_warnings:
        _fail("dead engine fields on a tm_match step must produce a strip-and-warn message")
    if "step 1" not in strip_warnings[0].lower():
        _fail(f"the strip warning must name the offending step, got {strip_warnings!r}")
    wf, rows = _steps_of("Parity TM dead-fields WF")
    if wf is None or not rows:
        _fail("a strip-and-warn apply must SUCCEED (create the workflow), not reject it")
    st = rows[0]
    if st.step_type != StepType.TM_MATCH:
        _fail(f"the created step must be tm_match, got {st.step_type!r}")
    if (st.prompt_text or "").strip() or st.qe_gate:
        _fail(
            "the dead engine fields must land at their safe defaults "
            f"(prompt={st.prompt_text!r} qe_gate={st.qe_gate})"
        )
    print(
        "  (3b) confirm: a tm_match step's dead engine fields STRIP-AND-WARN to their safe "
        "defaults (workflow still created), never a mid-apply add_step explosion: OK"
    )


def test_apply_external_honest_exclusion_message():
    payload = {
        "name": "Parity external WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [{"step_type": "external", "name": "Vendor leg"}],
    }
    raised = False
    with Session(engine) as s:
        try:
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError as exc:
            raised = True
            joined = " ".join(exc.errors).lower()
            if "builder" not in joined:
                _fail(f"external confirm error must name the builder path, got {exc.errors!r}")
            if "unknown step type" in joined:
                _fail("external is RECOGNIZED -- it must not read as 'unknown step type'")
            # MED-1: the confirm error must also disclose the substitution risk, so the
            # re-render + a blind re-confirm can never silently create a stray llm_agent step.
            if "changed to llm_agent" not in joined:
                _fail(f"external confirm error must disclose the substitution: {exc.errors!r}")
    if not raised:
        _fail("an external step at confirm must be rejected (out of scope for the assistant)")
    wf, _rows = _steps_of("Parity external WF")
    if wf is not None:
        _fail("a rejected external draft must create NOTHING (atomic)")
    print(
        "  (3c) confirm: external is rejected with the HONEST builder-path message, NOT "
        "'unknown step type'; nothing is created: OK"
    )


def test_apply_unknown_junk_still_unknown_step_type():
    payload = {
        "name": "Parity junk WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [{"step_type": "frobnicate", "name": "?"}],
    }
    raised = False
    with Session(engine) as s:
        try:
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError as exc:
            raised = True
            if not any("unknown step type" in e.lower() for e in exc.errors):
                _fail(f"a junk type must still read as 'unknown step type', got {exc.errors!r}")
    if not raised:
        _fail("a genuinely unknown junk step type must be rejected")
    print("  (3d) confirm: a genuine junk type still rejects as 'unknown step type': OK")


# ============================================================================
# 4. WEB route: /workflows/author/confirm creates a tm_match step end-to-end,
#    and the review dropdown offers tm_match but NOT external.
# ============================================================================
def test_confirm_route_creates_tm_match_end_to_end():
    client = TestClient(app)
    data = {
        "name": "Web Parity TM WF",
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "step_count": "2",
        "s0_name": "TM prefill",
        "s0_step_type": "tm_match",
        "s1_name": "Translate",
        "s1_step_type": "llm_agent",
        "s1_prompt_text": "Translate {text} into {target_locale}.",
    }
    r = client.post("/workflows/author/confirm", data=data, follow_redirects=False)
    if r.status_code != 303:
        _fail(f"a valid tm_match confirm must redirect (303), got {r.status_code}: {r.text[:300]}")
    wf, rows = _steps_of("Web Parity TM WF")
    if wf is None or not rows or rows[0].step_type != StepType.TM_MATCH:
        _fail("the web confirm route must create a real tm_match step 1")
    print("  (4a) web confirm: a tm_match step is created end-to-end via the route (303): OK")


def test_review_dropdown_offers_tm_match_not_external():
    client = TestClient(app)
    # Force an error RE-RENDER (200) so we can inspect the review dropdown: an llm_agent
    # prompt without {text}/{prior} is a hard confirm rejection.
    data = {
        "name": "Web Parity Dropdown WF",
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "step_count": "1",
        "s0_name": "Translate",
        "s0_step_type": "llm_agent",
        "s0_prompt_text": "no placeholder here -- forces a rejection re-render",
    }
    r = client.post("/workflows/author/confirm", data=data, follow_redirects=False)
    if r.status_code != 200:
        _fail(f"a rejected confirm must re-render the review page (200), got {r.status_code}")
    body = r.text
    if 'value="tm_match"' not in body:
        _fail("the review step-type dropdown must OFFER tm_match")
    if 'value="external"' in body:
        _fail("the review step-type dropdown must NOT offer external (not draftable here)")
    print(
        "  (4b) web review dropdown: OFFERS tm_match, does NOT offer external "
        "(assistant surface == setup_author's accepted set): OK"
    )


def test_review_shows_posted_external_as_disabled_option():
    # MED-1: when external (or junk) is POSTED, the confirm-rejection re-render must show it
    # as a SELECTED + DISABLED option so the <select> never silently defaults to llm_agent
    # (a blind re-confirm would otherwise create a stray llm_agent step -- the BUG-A201 class).
    client = TestClient(app)
    data = {
        "name": "Web Parity External Render WF",
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "step_count": "1",
        "s0_name": "Vendor leg",
        "s0_step_type": "external",
    }
    r = client.post("/workflows/author/confirm", data=data, follow_redirects=False)
    if r.status_code != 200:
        _fail(f"an external confirm must re-render the review page (200), got {r.status_code}")
    body = r.text
    if 'value="external" selected disabled' not in body:
        _fail(
            "the re-render must show the posted external type as a selected+disabled option "
            "(so the <select> never silently defaults to llm_agent)"
        )
    print(
        "  (4c) web re-render: a posted external step shows as a SELECTED+DISABLED option "
        "(no silent llm_agent default on re-confirm): OK"
    )


def test_apply_tm_match_leftover_branch_drops_and_warns():
    # MED-2: a leftover branch on a tm_match step (which produces no gate) must be DROPPED
    # via strip-and-warn, NOT hard-reject the whole draft -- the review page hides the branch
    # controls for tm_match, so a refusal would be unfollowable. The workflow is still created.
    payload = {
        "name": "Parity TM branch-drop WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "tm_match", "name": "TM prefill", "branch": {"on_fail": "end"}},
            {"step_type": "llm_agent", "prompt_text": "Translate {text} into {target_locale}."},
        ],
    }
    warns: list = []
    with Session(engine) as s:
        setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester", warnings=warns)
    wf, rows = _steps_of("Parity TM branch-drop WF")
    if wf is None or not rows:
        _fail("a leftover branch on a tm_match step must NOT hard-reject the draft")
    if rows[0].step_type != StepType.TM_MATCH or rows[0].branch:
        _fail(f"the tm_match step must be created with its branch dropped: {rows[0].branch!r}")
    _dropped = [w for w in warns if "dropped the branch" in w.lower() and "step 1" in w.lower()]
    if not _dropped:
        _fail(f"the dropped branch must surface a per-step warning, got {warns!r}")
    print(
        "  (5) confirm: a leftover branch on a non-gate tm_match step DROPS via strip-and-warn "
        "(workflow still created), never a hard atomic reject: OK"
    )


def test_llm_agent_branch_still_hard_errors():
    # MED-B: a branch on a plain llm_agent step (qe_gate absent) is NOT dropped -- its On
    # pass/On fail controls AND the QE-gate checkbox are VISIBLE on the review page, so the
    # error is one-click-fixable and must stay a hard error (in the repair loop), not degrade
    # to a silent shape change. The whole draft is rejected; nothing is created.
    payload = {
        "name": "Parity llm_agent branch WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {
                "step_type": "llm_agent",
                "prompt_text": "Translate {text} into {target_locale}.",
                "branch": {"on_fail": "end"},
            }
        ],
    }
    warns: list = []
    raised = False
    with Session(engine) as s:
        try:
            setup_author.apply_workflow_draft(
                s, payload, org_id=None, actor="tester", warnings=warns
            )
        except setup_author.WorkflowApplyError:
            raised = True
    if not raised:
        _fail("a branch on a plain llm_agent step must HARD-error (its controls are visible)")
    if any("dropped the branch" in w.lower() for w in warns):
        _fail("an llm_agent branch must NOT be silently dropped -- it is a hard, fixable error")
    wf, _rows = _steps_of("Parity llm_agent branch WF")
    if wf is not None:
        _fail("a hard branch error must create nothing (atomic)")
    print(
        "  (5b) MED-B: a branch on a plain llm_agent step stays a HARD error (visible controls, "
        "repair loop), never a silent drop: OK"
    )


def test_human_review_branch_drops_with_warning():
    # MED-B: human_review is a branch-hidden type, so a leftover branch DROPS with a warning
    # (like tm_match), workflow still created.
    payload = {
        "name": "Parity HR branch-drop WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "human_review", "name": "Sign-off", "branch": {"on_fail": "end"}},
            {"step_type": "llm_agent", "prompt_text": "Translate {text} into {target_locale}."},
        ],
    }
    warns: list = []
    with Session(engine) as s:
        setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester", warnings=warns)
    wf, rows = _steps_of("Parity HR branch-drop WF")
    if wf is None or not rows or rows[0].step_type != StepType.HUMAN_REVIEW or rows[0].branch:
        _fail("a human_review leftover branch must DROP (workflow created, branch cleared)")
    if not any("dropped the branch" in w.lower() and "step 1" in w.lower() for w in warns):
        _fail(f"the human_review dropped branch must surface a per-step warning, got {warns!r}")
    print(
        "  (5c) MED-B: a leftover branch on a branch-hidden human_review step DROPS with a "
        "warning (workflow still created): OK"
    )


def test_branch_drop_hard_errors_without_warnings_sink():
    # LOW-B1: with warnings=None there is NOWHERE to surface a drop, so the branch drop must
    # NOT be silent -- a no-warnings caller keeps the OLD hard refusal (validate_branch),
    # never a silent shape change.
    payload = {
        "name": "Parity no-sink branch WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "tm_match", "name": "TM prefill", "branch": {"on_fail": "end"}},
            {"step_type": "llm_agent", "prompt_text": "Translate {text} into {target_locale}."},
        ],
    }
    raised = False
    with Session(engine) as s:
        try:
            # warnings omitted == None: no sink, so the drop must hard-error instead.
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError:
            raised = True
    if not raised:
        _fail("with warnings=None a leftover branch must HARD-error, not be silently dropped")
    wf, _rows = _steps_of("Parity no-sink branch WF")
    if wf is not None:
        _fail("the warnings=None hard refusal must create nothing (atomic)")
    print(
        "  (5d) LOW-B1: with no warnings sink (warnings=None), a leftover branch keeps the OLD "
        "hard refusal rather than a silent drop: OK"
    )


def test_med_a_blank_repost_stays_honest_and_terminates():
    # MED-A: a disabled blocked <option> submits NOTHING on re-POST. The hidden mirror keeps
    # the confirm error HONEST ("external ..."), never degraded to "(blank)", and the disabled
    # option persists so the <select> can never fall back to a submittable llm_agent -- the
    # loop terminates on a conscious pick, never a silent stray step.
    client = TestClient(app)
    # Click 1: post external -> honest error + disabled option + hidden mirror preserved.
    d1 = {
        "name": "MEDA Roundtrip WF",
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "step_count": "1",
        "s0_name": "Vendor",
        "s0_step_type": "external",
    }
    r1 = client.post("/workflows/author/confirm", data=d1, follow_redirects=False)
    if r1.status_code != 200:
        _fail(f"click 1 (external) must re-render (200), got {r1.status_code}")
    if 'name="s0_posted_step_type" value="external"' not in r1.text:
        _fail("the hidden posted-step-type mirror must be rendered so the type survives re-POST")
    # Click 2: the disabled <option> submits nothing -- only the hidden mirror is posted.
    d2 = {
        "name": "MEDA Roundtrip WF",
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "step_count": "1",
        "s0_name": "Vendor",
        "s0_posted_step_type": "external",  # NO s0_step_type (disabled option submits nothing)
    }
    r2 = client.post("/workflows/author/confirm", data=d2, follow_redirects=False)
    if r2.status_code != 200:
        _fail(f"click 2 (blank select + mirror) must re-render (200), got {r2.status_code}")
    b2 = r2.text.lower()
    if "unknown step type" in b2:
        _fail("the re-POST message must stay the HONEST external message, never '(blank)'")
    if "builder" not in b2:
        _fail("the honest external message must survive the re-POST via the hidden mirror")
    if 'value="external" selected disabled' not in r2.text:
        _fail("the disabled external option must persist so the loop terminates on a real pick")
    wf, _rows = _steps_of("MEDA Roundtrip WF")
    if wf is not None:
        _fail("no stray step may be created across the external re-POSTs")
    print(
        "  (4d) MED-A: the external re-POST stays honest (hidden mirror, no '(blank)') and the "
        "disabled option persists -- no silent llm_agent, loop terminates: OK"
    )


def test_med_a_truly_blank_shows_no_type_option():
    # MED-A: even a truly blank step_type (no mirror at all) yields a selected+disabled
    # "(no type selected)" option, so the <select> can never fall back to a submittable
    # llm_agent -- the human must consciously pick a type.
    client = TestClient(app)
    d = {
        "name": "MEDA Blank WF",
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "step_count": "1",
        "s0_name": "X",  # no s0_step_type, no s0_posted_step_type
    }
    r = client.post("/workflows/author/confirm", data=d, follow_redirects=False)
    if r.status_code != 200:
        _fail(f"a blank-type confirm must re-render (200), got {r.status_code}")
    if "(no type selected)" not in r.text:
        _fail("a blank step_type must render a selected+disabled '(no type selected)' option")
    wf, _rows = _steps_of("MEDA Blank WF")
    if wf is not None:
        _fail("a blank-type draft must create nothing")
    print(
        "  (4e) MED-A: a truly blank step_type shows a selected+disabled '(no type selected)' "
        "option (no silent llm_agent fallback): OK"
    )


def test_strip_candidates_cover_every_carried_dead_field():
    # LOW-2: the strip _candidates dict in apply_workflow_draft must cover every dead field
    # that clean_steps CARRIES into add_step (else it reaches add_step un-stripped and
    # refuse_dead_fields raises mid-write -- an opaque internal error at the bare except).
    # CARRIED is derived from a real add_step call (the clean_steps dict keys, minus the
    # popped needs_review) so a future column wired into clean_steps is caught automatically.
    captured: set = set()
    real_add = wf_svc.add_step

    def _spy(session, wid, **kw):
        captured.update(kw.keys())
        return real_add(session, wid, **kw)

    payload = {
        "name": "Parity carried-keys WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "llm_agent", "prompt_text": "Translate {text} into {target_locale}."}
        ],
    }
    set_pieces("app.services.workflows", "add_step", _spy)
    try:
        with Session(engine) as s:
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
    finally:
        set_pieces("app.services.workflows", "add_step", real_add)
    if not captured:
        _fail("the add_step spy captured nothing -- the apply never reached the write phase")
    # LOW-C: pin against the SINGLE source of truth the strip is BUILT from (not a hand-copied
    # literal that could silently drift), so REMOVING a field from the strip fails this test.
    # tm_examples is EXEMPT: _dead_field_is_meaningful can never flag it (checkbox-omission
    # ambiguity), so it is safe to carry un-stripped.
    strip_candidates = set(setup_author._STRIP_CANDIDATE_FIELDS)
    for t in StepType.ALL:
        dead = set(wf_svc.DEAD_FIELDS_BY_TYPE.get(t, ()))
        uncovered = (dead & captured) - strip_candidates - {"tm_examples"}
        if uncovered:
            _fail(
                f"step type {t!r}: dead field(s) {sorted(uncovered)} are CARRIED into add_step "
                "but not in the strip _candidates -- they would reach add_step un-stripped and "
                "raise mid-apply (add the field(s) to the strip machinery)"
            )
    print(
        "  (6) LOW-2 pin: every dead field clean_steps carries into add_step is covered by the "
        "strip _candidates (modulo tm_examples) for all five step types: OK"
    )


def test_api_author_route_creates_tm_match_end_to_end():
    # LOW-3: the headless twin -- POST /api/workflows/author (create:true) drafts + applies a
    # tm_match-first workflow through the SAME apply seam the web confirm route uses. The mock
    # authoring engine returns a canned tm_match draft (deterministic, offline, free).
    from app.services import settings as _settings

    draft_json = (
        '{"name": "API Parity TM WF", "customer": null, "source_locale": "en-US", '
        '"target_locale": "fr-FR", "steps": ['
        '{"name": "TM prefill", "step_type": "tm_match"}, '
        '{"name": "Translate", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} into {target_locale}."}'
        '], "notes_for_reviewer": ""}'
    )
    ref = _make_engine([draft_json], {"n": 0})
    u = security.create_user("apiparityauthor@x.com", "A", Role.ADMIN, "parity-password-123456")
    _row, key = apikeys.create_key(u.id, "parity-author", scopes="api docs")
    _settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    _settings.invalidate_cache()
    try:
        client = TestClient(app)
        r = client.post(
            "/api/workflows/author",
            headers={"X-Loc-Token": key},
            json={"description": "TM first then translate", "provider": ref, "create": True},
        )
        if r.status_code != 200:
            _fail(f"/api/workflows/author create must be 200, got {r.status_code}: {r.text[:120]}")
        body = r.json()
        if not (body.get("ok") and body.get("created")):
            _fail(f"the API author route must report created=True, got {body!r}")
    finally:
        _settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        _settings.invalidate_cache()
    wf, rows = _steps_of("API Parity TM WF")
    if wf is None or not rows or rows[0].step_type != StepType.TM_MATCH:
        got = [r.step_type for r in rows]
        _fail(f"the API author route must create a real tm_match step 1, got {got}")
    print(
        "  (7) LOW-3 headless twin: POST /api/workflows/author (create) drafts + applies a "
        "tm_match-first workflow end-to-end: OK"
    )


def test_branch_rules_teach_the_correction_loop_convention():
    """The drafting instructions must say that on_pass 'end' is legal on any gate step.

    Measured live against the real drafter (2026-09-16): without this, the model
    leaves the FIRST check unbranched every time. Its reasoning is sound given what the
    rules used to say -- they described a branch only as a jump, and "on fail, go on to
    the fix step" cannot be a jump, because forward jumps are unsupported. So it reached
    for run_condition and emitted no branch at all, and a PASSING translation fell
    through the fix step into the re-check that was only ever meant for a failure.

    `validate_branch` has always accepted 'end' on any gate-producing step -- it is not a
    target lookup, so no forward-jump rule applies to it. The rules simply never said so.
    This asserts the fix stays said, and that the fix step's two halves (the
    prev_lqa:failed guard and the {lqa_findings} placeholder) are named alongside it.
    """
    from app.services import setup_author_helpers

    rules = setup_author_helpers._WF_BRANCH_RULES
    assert "on_pass 'end'" in rules, (
        "the drafting rules must name on_pass 'end' -- without it the drafter leaves a "
        "passing check to fall through into the next step"
    )
    assert "not a jump" in rules.lower() or "no forward jumps" in rules, (
        "the rules must say WHY 'end' is always allowed, or the drafter keeps applying "
        "the forward-jump rule to it"
    )
    assert "{lqa_findings}" in rules, "the fix step's prompt placeholder must be named"
    assert "prev_lqa:failed" in rules, "the fix step's run_condition must be named"

    # And the convention must describe something validate_branch actually accepts: an
    # on_pass 'end' / on_fail 'next' branch on an LQA step, with no prior keys at all.
    clean = wf_svc.validate_branch(
        {"on_pass": "end", "on_fail": "next"}, StepType.LQA, False, [], self_key="lqa1"
    )
    assert clean == {"on_pass": "end", "on_fail": "next"}, clean
    print("  branch rules teach on_pass 'end' + the fix step's guard and placeholder OK")


def main() -> None:
    test_invariant_accepted_union_excluded_is_all()
    test_draft_tm_match_survives_as_itself()
    test_draft_external_honest_not_junk()
    test_draft_junk_defaults_with_unrecognized_warning()
    test_apply_creates_a_real_tm_match_step()
    test_apply_tm_match_strips_dead_engine_fields_and_warns()
    test_apply_external_honest_exclusion_message()
    test_apply_unknown_junk_still_unknown_step_type()
    test_confirm_route_creates_tm_match_end_to_end()
    test_review_dropdown_offers_tm_match_not_external()
    test_review_shows_posted_external_as_disabled_option()
    test_med_a_blank_repost_stays_honest_and_terminates()
    test_med_a_truly_blank_shows_no_type_option()
    test_apply_tm_match_leftover_branch_drops_and_warns()
    test_llm_agent_branch_still_hard_errors()
    test_human_review_branch_drops_with_warning()
    test_branch_drop_hard_errors_without_warnings_sink()
    test_strip_candidates_cover_every_carried_dead_field()
    test_api_author_route_creates_tm_match_end_to_end()
    test_branch_rules_teach_the_correction_loop_convention()
    print("WORKFLOW AUTHOR PARITY OK")


if __name__ == "__main__":
    main()
