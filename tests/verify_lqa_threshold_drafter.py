"""AI workflow drafter emits + persists lqa_threshold (chip task_8face0e6) -- hermetic.

The maintainer's headline example ("an LQA step that needs to pass with a 94") is now expressible
in the engine (feat/lqa-configurable-threshold), but the AI WORKFLOW DRAFTER dropped it:
the draft JSON schema, the authoring prompt, the draft validator and the confirm/apply
pipeline all ignored `lqa_threshold`, so a "pass with a 94" description produced a
DEFAULT-90 gate. This teaches the drafter to carry it end-to-end and pins the [0,100]
reject rule the add/update seam uses.

Pinned (the LLM is a deterministic MockTransport returning a fixed draft JSON):
  1. a drafted lqa step with lqa_threshold=94 survives author_workflow_setup (draft) AND
     apply_workflow_draft (confirm) -> the stored WorkflowStep.lqa_threshold == 94.
  2. an out-of-band lqa_threshold (150) is WARN+DROPPED at draft time (editable on the
     review screen) and HARD-REJECTED at confirm (WorkflowApplyError, nothing saved) --
     the same [0,100] reject rule, never a silent clamp.
  3. an lqa_threshold on a NON-lqa (llm_agent) step is STRIP-AND-WARNED at confirm
     (dead-for-type), never a hard reject over a field the review page hides.

Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII-only.
Final line: "LQA THRESHOLD DRAFTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lqathrdraft_")

import httpx
from sqlmodel import Session, select

from app.db import engine as db_engine
from app.db import init_db
from app.models import WorkflowStep
from app.services import content, setup_author
from app.services.engines import create_profile, get_profile
from app.services.providers import OpenAICompatibleProvider, register_provider

init_db()
_ENGINE_SEQ = {"n": 0}


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _transport(responses, counter):
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


def _engine(responses, counter):
    _ENGINE_SEQ["n"] += 1
    p = create_profile(
        f"LqaDraftEng{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(p.id), transport=_transport(responses, counter)
        ),
    )
    return ref


def _draft(lqa_step_extra, translate_extra=""):
    return (
        '{"name": "Acme 94 flow", "source_locale": "en-US", '
        '"target_locale": "es-419", "steps": ['
        '{"name": "Translate", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} into {target_locale}."' + translate_extra + "}, "
        '{"name": "LQA gate", "step_type": "lqa"' + lqa_step_extra + "}], "
        '"notes_for_reviewer": "pass with a 94"}'
    )


def _author(desc, draft_json):
    counter = {"n": 0}
    ref = _engine([draft_json], counter)
    with Session(db_engine) as s:
        out = setup_author.author_workflow_setup(
            s, description=desc, provider=ref, org_id=None, actor="tester"
        )
    return out


# ============================================================================ #
# 1. "pass with a 94" -> lqa_threshold=94 survives draft + apply
# ============================================================================ #
def test_drafter_emits_and_persists_94():
    out = _author(
        "Translate then an LQA gate that must pass with a 94", _draft(', "lqa_threshold": 94')
    )
    f = out["fields"]
    steps = f["steps"]
    if len(steps) != 2:
        _fail(f"expected 2 drafted steps, got {len(steps)}")
    if steps[1].get("lqa_threshold") != 94:
        _fail(
            f"the drafted lqa step must carry lqa_threshold=94, got {steps[1].get('lqa_threshold')}"
        )
    # apply (confirm seam) persists it on the real WorkflowStep
    with Session(db_engine) as s:
        wf = setup_author.apply_workflow_draft(s, f, org_id=None, actor="tester")
        wf_id = wf.id
    with Session(db_engine) as s:
        stored = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf_id)
            .order_by(WorkflowStep.order_index)
        ).all()
        if stored[1].step_type != "lqa" or stored[1].lqa_threshold != 94:
            _fail(
                f"apply must persist lqa_threshold=94, got type="
                f"{stored[1].step_type} thr={stored[1].lqa_threshold}"
            )
    print("  1. 'pass with a 94' -> drafted + persisted lqa_threshold=94: OK")


# ============================================================================ #
# 2. out-of-band -> warn+drop at draft, hard reject at confirm
# ============================================================================ #
def test_out_of_band_warned_then_rejected():
    out = _author("LQA gate at 150", _draft(', "lqa_threshold": 150'))
    f = out["fields"]
    # draft time: dropped to None + a visible warning (not a hard failure)
    if f["steps"][1].get("lqa_threshold") is not None:
        _fail("an out-of-band drafted lqa_threshold must be dropped to None at draft time")
    if not any("lqa pass threshold" in w.lower() for w in out["warnings"]):
        _fail(f"draft must warn about the dropped threshold: {out['warnings']}")

    # confirm time: an untrusted payload carrying 150 is HARD-rejected (nothing saved)
    payload = {
        "name": "reject-150",
        "source_locale": "en-US",
        "target_locale": "es-419",
        "steps": [
            {
                "name": "Translate",
                "step_type": "llm_agent",
                "prompt_text": "Translate {text} into {target_locale}.",
            },
            {"name": "LQA", "step_type": "lqa", "lqa_threshold": 150},
        ],
    }
    with Session(db_engine) as s:
        try:
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError as exc:
            if (
                "0 to 100" not in str(exc).lower()
                and "0 to 100" not in "; ".join(exc.errors).lower()
            ):
                _fail(f"the confirm rejection must name the [0,100] band: {exc.errors}")
        else:
            _fail("apply_workflow_draft must HARD-reject an out-of-band lqa_threshold")
    print("  2. out-of-band 150: warn+drop at draft, hard reject at confirm: OK")


# ============================================================================ #
# 3. lqa_threshold on a NON-lqa step -> strip-and-warn at confirm (not a reject)
# ============================================================================ #
def test_dead_for_type_stripped_not_rejected():
    payload = {
        "name": "strip-off-type",
        "source_locale": "en-US",
        "target_locale": "es-419",
        "steps": [
            {
                "name": "Translate",
                "step_type": "llm_agent",
                "prompt_text": "Translate {text} into {target_locale}.",
                "lqa_threshold": 94,
            },
        ],
    }
    warnings: list = []
    with Session(db_engine) as s:
        wf = setup_author.apply_workflow_draft(
            s, payload, org_id=None, actor="tester", warnings=warnings
        )
        wf_id = wf.id
    if not any("lqa pass threshold" in w.lower() for w in warnings):
        _fail(f"a dead lqa_threshold on an llm_agent step must be strip-warned: {warnings}")
    with Session(db_engine) as s:
        stored = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).all()
        if stored[0].lqa_threshold is not None:
            _fail("the stripped lqa_threshold must persist as None on the llm_agent step")
    print("  3. lqa_threshold on a non-lqa step -> stripped+warned at confirm (not rejected): OK")


# ============================================================================ #
# 4. review H3: the WEB review screen carries lqa_threshold through Confirm
# ============================================================================ #
def test_web_review_confirm_roundtrip():
    """The primary surface for the maintainer's example is the web "Design a workflow" review
    screen. Its template had NO s{i}_lqa_threshold input, so a drafted "pass with a 94"
    reached the review page, was parsed as "" on Confirm, and silently fell back to 90.
    Prove (a) the input now renders with the drafted value, and (b) a Confirm carrying
    it persists 94 while a Confirm WITHOUT it falls back to None (so the field is what
    carries the fact)."""
    from fastapi.testclient import TestClient
    from sqlmodel import select as _select

    from app.main import app

    client = TestClient(app)  # auth OFF in this suite -> _ai_block + CSRF are no-ops
    counter = {"n": 0}
    ref = _engine([_draft(', "lqa_threshold": 94')], counter)
    # (a) author -> the review page renders the lqa_threshold input WITH the value
    r = client.post(
        "/workflows/author", data={"description": "LQA gate must pass with a 94", "provider": ref}
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    html = r.text
    if 'name="s1_lqa_threshold"' not in html:
        _fail("the review page must render an s1_lqa_threshold input for the lqa step")
    if 'value="94"' not in html:
        _fail("the review page must pre-fill the drafted lqa_threshold=94")

    # (b) Confirm carrying s1_lqa_threshold=94 -> persisted 94
    def _confirm(thr_field):
        form = {
            "step_count": "2",
            "name": "Web 94 flow",
            "customer_id": "",
            "source_locale": "en-US",
            "target_locales": "es-419",
            "s0_name": "Translate",
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate {text} into {target_locale}.",
            "s1_name": "LQA gate",
            "s1_step_type": "lqa",
        }
        form.update(thr_field)
        resp = client.post("/workflows/author/confirm", data=form, follow_redirects=False)
        assert resp.status_code == 303, (resp.status_code, resp.text[:300])
        wid = int(resp.headers["location"].rsplit("/", 1)[-1])
        with Session(db_engine) as s:
            steps = s.exec(
                _select(WorkflowStep)
                .where(WorkflowStep.workflow_id == wid)
                .order_by(WorkflowStep.order_index)
            ).all()
        return steps[1].lqa_threshold

    got = _confirm({"s1_lqa_threshold": "94"})
    if got != 94:
        _fail(f"a Confirm carrying s1_lqa_threshold=94 must persist 94, got {got}")
    # control: no field -> falls back to None (default 90), proving the field carries it
    got_none = _confirm({})
    if got_none is not None:
        _fail(f"a Confirm WITHOUT the field must persist None (default), got {got_none}")
    print(
        "  4. web review screen renders + round-trips lqa_threshold through Confirm "
        "(field absent -> default None): OK"
    )


def main():
    # a customer catalog is not required for these drafts (no customer named)
    with Session(db_engine) as s:
        content.customer_create(s, "AnyCust")
    test_drafter_emits_and_persists_94()
    test_out_of_band_warned_then_rejected()
    test_dead_for_type_stripped_not_rejected()
    test_web_review_confirm_roundtrip()
    print("LQA THRESHOLD DRAFTER OK")


if __name__ == "__main__":
    main()
