"""Promptable WORKFLOW setup verification (SPEC-promptable-setup, Branch B),
offline + hermetic.

The LLM PROPOSES a whole workflow (name + customer + language pair(s) + ordered
steps); the server VALIDATES every reference against the caller's real, visibility-
scoped catalogs; the review surface holds it in EDITABLE form fields; confirm re-
validates ALL of it as UNTRUSTED input and creates ATOMICALLY through the SAME
builder seams (wf_service.create_workflow + add_step, prompts.validate_step_prompt,
glossary_module.options_for_step). Draft time persists NOTHING.

Covers the B4 list:
  1. 3-step the maintainer-style draft (translate -> LQA -> fix step) lands with glossary
     pinning on step 1 and needs_review semantics on step 3. A drafted qe_gate/
     qe_threshold on the LQA step itself is DEAD (QE never runs on an lqa step;
     SPEC-lqa-step-honest-options) and is refused at confirm -- see (1b).
  2. prompt_text missing {text}/{prior} -> a DRAFT warning AND a CONFIRM rejection.
  3. confirm atomicity: a poisoned LATER step -> zero rows created (proven by count).
  4. confirm payload tampering (glossary outside the allowed set, foreign customer,
     provider not offered) -> rejected, nothing created.
  5. multi-pair draft routes through the EXISTING copy-to-pairs/group seam.
  6. CLI parity: `workflow author` prints the draft; --create creates it; --create
     REFUSES on a remaining step-level validation error (warnings do not block).
  7. RBAC: a viewer/translator (no can_tune_ai) gets 403 on all three routes.
  8. shared-core repair-retry (exactly one) + spend-block (zero calls), re-pinned once
     at workflow level.

Provider wiring goes through the REAL OpenAICompatibleProvider with an injected
httpx.MockTransport (the connector-test fixture pattern), registered via
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
from tests._piece_patch import set_pieces

init_env("loctms_promptwf_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.cli.__main__ import main as cli_main
from app.db import engine as db_engine
from app.db import init_db
from app.models import (
    SpendLedger,
    Workflow,
    WorkflowStep,
)
from app.services import (
    content,
    settings,
    setup_author,
    spend,
)
from app.services import (
    glossary_module as gm,
)
from app.services.engines import create_profile, get_profile
from app.services.providers import (
    OpenAICompatibleProvider,
    register_provider,
)

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
        f"LocalWF{_ENGINE_SEQ['n']}",
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


def _approved_glossary(name, pairs, org_id=None, customer_id=None):
    with _session() as s:
        g = gm.create(s, name, org_id=org_id, customer_id=customer_id)
        gm.set_pairs(s, g.id, pairs)
        gm.approve(s, g.id)
        return g.id


def _counts():
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


# --- 1. 3-step the maintainer-style draft ---------------------------------------------
def check_three_step_draft():
    with _session() as s:
        cust = content.customer_create(s, "AcmeWf")
        cust_id = cust.id
    gid = _approved_glossary("AcmeTerms", [("en-US", "es-ES")])
    # QA-7 (#37): the steps are drafted with a BLANK provider (inherit the run default). On
    # this hermetic env config.DEFAULT_PROVIDER is keyless 'claude' (non-live), so a blank
    # executable step would silently run on MockProvider and is bound to the selected engine
    # with ONE "providers bound" warning (QA-7 review follow-up) -- orthogonal to the draft
    # SHAPE this check exercises (glossary/tm/qe/needs_review), isolated by the filter below.
    # Full engine-binding behavior is proven in verify_qa_7_8_10_authoring.py.
    draft_json = (
        '{"name": "Acme EN-ES 3-step", "customer": "AcmeWf", '
        '"source_locale": "en-US", "target_locale": "es-ES", "steps": ['
        '{"name": "Translate", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to {target_locale}. Honor {glossary}.", '
        '"glossary": "AcmeTerms", "tm_examples": true}, '
        '{"name": "LQA @90", "step_type": "lqa", "qe_gate": true, "qe_threshold": 90}, '
        # Names its target language: a locale-less llm_agent prompt draws its own
        # authoring warning (GitHub #43) and this check asserts the draft SHAPE is
        # warning-clean apart from the orthogonal provider-binding one.
        '{"name": "Fix errors", "step_type": "llm_agent", '
        '"prompt_text": "Using the LQA feedback, fix errors in {prior} '
        '({target_locale}).", '
        '"needs_review": true}], "notes_for_reviewer": "3-step the maintainer flow"}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s,
            description="EN to ES, translate then LQA at 90 then fix",
            provider=ref,
            org_id=None,
            actor="tester",
        )
    f, w = out["fields"], out["warnings"]
    # The draft SHAPE is fully resolvable -> no shape/prompt warning. TWO orthogonal
    # PROVIDER warnings are expected and filtered out here: the QA-7 binding one (blank steps
    # + a non-live run default), and its direct consequence, the #148 self-judging one --
    # binding all three steps to the ONE selected engine necessarily puts the LQA step on the
    # same engine as the Translate step it grades, which is a true positive about engine
    # choice, not about the shape of the graph.
    shape_w = [x for x in w if "bound to" not in x and "judging the engine" not in x]
    assert shape_w == [], ("a fully-resolvable draft must have no shape warnings", w)
    assert f["name"] == "Acme EN-ES 3-step"
    assert f["customer_id"] == cust_id, (f["customer_id"], cust_id)
    # A drafted workflow carries NO language pair (SPEC-language-agnostic-workflows T1):
    # the draft contract no longer asks for one, so the fields dict never carries them.
    assert "source_locale" not in f and "target_locales" not in f and "multi_pair" not in f, f
    assert len(f["steps"]) == 3, f["steps"]
    s0, s1, s2 = f["steps"]
    # QA-7: the blank Translate provider is bound to the selected engine (non-live run default)
    # with its (absent) model cleared to the engine default; glossary/tm stay intact.
    assert s0["step_type"] == "llm_agent" and s0["provider"] == ref, s0["provider"]
    assert s0["model"] == "", ("the bound step uses the engine default model", s0["model"])
    assert s0["glossary_id"] == gid and s0["prompt_warning"] is None
    # DRAFT time doesn't strip anything (only apply_workflow_draft does) -- the
    # proposed qe_gate/qe_threshold ride through to the editable review screen as-is.
    assert s1["step_type"] == "lqa" and s1["qe_gate"] is True and s1["qe_threshold"] == 90
    assert s2["needs_review"] is True and s2["prompt_warning"] is None
    assert counter["n"] == 1, ("the draft is a single provider call", counter["n"])

    # Apply it (service seam) -> a real workflow with the 3 steps + semantics.
    # SPEC-lqa-step-honest-options (adversarial-review finding, 1b): qe_gate/
    # qe_threshold are DEAD on an lqa step (QE never runs there) -- this is
    # STRIP-AND-WARN, not a rejection: the apply still SUCCEEDS, the dead fields
    # are reset to their safe defaults, and a visible per-step warning says so.
    strip_warnings: list = []
    with _session() as s:
        wf = setup_author.apply_workflow_draft(
            s, f, org_id=None, actor="tester", warnings=strip_warnings
        )
        wf_id = wf.id
    assert len(strip_warnings) == 1, ("exactly one step had a dead field stripped", strip_warnings)
    assert "step 2" in strip_warnings[0].lower(), strip_warnings
    assert "qe routing gate" in strip_warnings[0].lower(), strip_warnings
    with _session() as s:
        steps = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf_id)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert len(steps) == 3, steps
        assert steps[1].step_type == "lqa", steps[1].step_type
        assert steps[1].qe_gate is False and steps[1].qe_threshold == 70, (
            "the drafted qe_gate/qe_threshold must be STRIPPED to their safe "
            "defaults, not silently kept",
            steps[1].qe_gate,
            steps[1].qe_threshold,
        )
        assert steps[2].needs_review is True, "the fix step must be flagged needs_review"
        assert steps[0].glossary_id == gid
        wf = s.get(Workflow, wf_id)
        assert wf.customer_id == cust_id
        # No language pair is stamped on a drafted workflow (T1); it takes its languages
        # from the project it runs on. group_id stays NULL (a draft never fans out).
        assert wf.source_locale is None and wf.target_locale is None, (
            wf.source_locale,
            wf.target_locale,
        )
        assert wf.group_id is None, "a drafted workflow is never in a group"
    print(
        "  1. 3-step the maintainer draft: LQA step 2's drafted qe_gate@90 is STRIPPED "
        "(not refused) with a visible warning; needs_review on step 3, glossary "
        "pinned; applies to a real 3-step workflow"
    )


# --- 2. prompt missing {text}/{prior} -> draft warning AND confirm rejection --
def check_prompt_placeholder_warn_then_reject():
    draft_json = (
        '{"name": "Bad prompt wf", "source_locale": "en-US", '
        '"target_locale": "fr-FR", "steps": [{"name": "T", "step_type": "llm_agent", '
        '"prompt_text": "Translate everything to French."}]}'
    )
    counter = {"n": 0}
    ref = make_engine([draft_json], counter)
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="x", provider=ref, org_id=None, actor="tester"
        )
    f, w = out["fields"], out["warnings"]
    assert any("{text}" in x for x in w), ("draft must WARN a prompt missing {text}", w)
    assert f["steps"][0]["prompt_warning"] and "{text}" in f["steps"][0]["prompt_warning"]

    # At CONFIRM the SAME prompt is a REJECTION -> WorkflowApplyError, nothing created.
    wf_before, st_before = _counts()
    raised = False
    with _session() as s:
        try:
            setup_author.apply_workflow_draft(s, f, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError as exc:
            raised = True
            assert any("{text}" in e for e in exc.errors), exc.errors
    assert raised, "confirm must REJECT a prompt missing {text}"
    assert _counts() == (wf_before, st_before), "a rejected confirm must create nothing"
    print("  2. prompt missing {text} -> draft WARNING + confirm REJECTION (nothing created)")


# --- 3. confirm atomicity: a poisoned LATER step -> zero rows -----------------
def check_confirm_atomicity():
    # Steps 1-2 are perfectly valid; step 3 is poisoned (prompt missing {text}). The
    # whole confirm must roll back to ZERO rows -- no half-created workflow.
    payload = {
        "name": "Atomic wf",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["de-DE"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "llm_agent", "prompt_text": "Translate {text}.", "tm_examples": True},
            # qe_gate/qe_threshold are dead on an lqa step and would ALSO be
            # refused (SPEC-lqa-step-honest-options) -- omitted here so this step
            # is genuinely valid and step 3 remains the SOLE poisoned one.
            {"step_type": "lqa"},
            {"step_type": "llm_agent", "prompt_text": "Fix all errors now."},  # poison
        ],
    }
    wf_before, st_before = _counts()
    raised = False
    with _session() as s:
        try:
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError:
            raised = True
    assert raised, "a poisoned step must raise WorkflowApplyError"
    wf_after, st_after = _counts()
    assert (wf_after, st_after) == (wf_before, st_before), (
        "ATOMIC: a poisoned later step must create ZERO workflow/step rows",
        (wf_before, st_before),
        (wf_after, st_after),
    )
    print("  3. confirm atomicity: one poisoned later step -> zero rows created (proven by count)")


# --- 4. confirm payload tampering rejected -----------------------------------
def check_confirm_tampering_rejected():
    # A DRAFT (unapproved) glossary id is never in the allowed set -> a tampered pin.
    with _session() as s:
        draft_g = gm.create(s, "UnapprovedTerms")
        draft_gid = draft_g.id
    with _session() as s:
        cust_a = content.customer_create(s, "RingA")
        cust_b = content.customer_create(s, "RingB")
        a_id, b_id = cust_a.id, cust_b.id

    def _reject(payload, needle, **kw):
        before = _counts()
        raised = False
        with _session() as s:
            try:
                setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester", **kw)
            except setup_author.WorkflowApplyError as exc:
                raised = True
                assert any(needle in e.lower() for e in exc.errors), (needle, exc.errors)
        assert raised, ("tampering must be rejected", needle)
        assert _counts() == before, ("a rejected confirm creates nothing", needle)

    base = {
        "name": "Tamper wf",
        "source_locale": "en-US",
        "target_locales": ["es-ES"],
        "notes_for_reviewer": "",
        "customer_id": "",
    }
    # (a) glossary outside the allowed set (a draft glossary).
    _reject(
        {
            **base,
            "steps": [
                {
                    "step_type": "llm_agent",
                    "prompt_text": "Translate {text}.",
                    "glossary_id": str(draft_gid),
                }
            ],
        },
        "glossary",
    )
    # (b) provider not offered.
    _reject(
        {
            **base,
            "steps": [
                {
                    "step_type": "llm_agent",
                    "prompt_text": "Translate {text}.",
                    "provider": "totally-bogus",
                }
            ],
        },
        "provider",
    )
    # (c) a bound caller naming a FOREIGN customer.
    _reject(
        {
            **base,
            "customer_id": str(b_id),
            "steps": [{"step_type": "llm_agent", "prompt_text": "Translate {text}."}],
        },
        "your own customer",
        bound_customer_id=a_id,
    )
    print(
        "  4. confirm tampering (glossary outside set / un-offered provider / foreign "
        "customer) -> rejected, nothing created"
    )


# --- 5. a draft NEVER fans out into a group (SPEC-language-agnostic-workflows T1) ----
def check_draft_never_fans_out():
    # Several target_locales in the payload must NOT fan out: exactly ONE ungrouped
    # workflow, no pair, the base step config intact. The old multi-pair/group seam is
    # gone from the draft path (groups stay via verify_workflow_groups / copy_to_pairs).
    payload = {
        "name": "One wf only",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["es-ES", "fr-FR", "de-DE"],
        "notes_for_reviewer": "",
        "steps": [{"step_type": "llm_agent", "prompt_text": "Translate {text}."}],
    }
    wf_before, _ = _counts()
    with _session() as s:
        wf = setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        wf_id, group_id = wf.id, wf.group_id
    wf_after, _ = _counts()
    assert wf_after == wf_before + 1, "exactly ONE workflow row is created"
    assert group_id is None, "a draft never creates a group"
    with _session() as s:
        wf = s.get(Workflow, wf_id)
        assert wf.source_locale is None and wf.target_locale is None, (
            "no pair stamped even though the payload named several targets",
            wf.source_locale,
            wf.target_locale,
        )
        assert wf.group_id is None
        # the name carries no [src->tgt] suffix -- there is no family to disambiguate
        assert wf.name == "One wf only", wf.name
        steps = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).all()
        assert len(steps) == 1, "the single workflow carries the drafted step"
    print("  5. multi-target draft -> exactly ONE ungrouped workflow, no pair, no fan-out")


# --- 6. CLI parity: print, --create, refuse on remaining validation error -----
def check_cli_parity():
    good_json = (
        '{"name": "CLI WF Made", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"name": "T", "step_type": "llm_agent", "provider": "claude", '
        '"prompt_text": "Translate {text} to {target_locale}."}, '
        '{"name": "QA", "step_type": "lqa", "qe_gate": true, "qe_threshold": 85}]}'
    )

    # Default: prints the draft (no workflow created).
    ref = make_engine([good_json], {"n": 0})
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["workflow", "author", "en to es 2-step", "--provider", ref])
    out = buf.getvalue()
    assert rc == 0, ("printing the draft exits 0", rc)
    assert "Drafted workflow setup" in out and "CLI WF Made" in out, out
    with _session() as s:
        assert s.exec(select(Workflow).where(Workflow.name == "CLI WF Made")).first() is None, (
            "the default (print) mode must NOT create a workflow"
        )

    # --create: applies via apply_workflow_draft; the workflow + steps exist. The
    # drafted qe_gate/qe_threshold on the lqa step is STRIPPED (not refused) --
    # --create still exits 0 and a strip-warning prints (SPEC-lqa-step-honest-
    # options, adversarial-review finding: the draft path never hard-rejects a
    # dead-for-type field, only direct human authoring does).
    ref2 = make_engine([good_json], {"n": 0})
    buf2, err2 = io.StringIO(), io.StringIO()
    with redirect_stdout(buf2), redirect_stderr(err2):
        rc = cli_main(["workflow", "author", "en to es 2-step", "--provider", ref2, "--create"])
    out2 = buf2.getvalue() + err2.getvalue()  # emit.warn() prints to stderr
    assert rc == 0, ("--create exits 0 even with a dead-field draft", rc, out2)
    assert "qe routing gate" in out2.lower(), ("the CLI must print the strip warning", out2)
    with _session() as s:
        wf = s.exec(select(Workflow).where(Workflow.name == "CLI WF Made")).first()
        assert wf is not None, "--create must create the workflow"
        steps = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf.id)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert len(steps) == 2 and steps[1].step_type == "lqa"
        assert steps[1].qe_gate is False and steps[1].qe_threshold == 70, (
            "drafted qe_gate/qe_threshold must be stripped to their safe defaults",
            steps[1].qe_gate,
            steps[1].qe_threshold,
        )

    # --create REFUSES when a step-level validation error remains (prompt missing
    # {text}); warnings print but the create is refused with a non-zero exit.
    bad_json = (
        '{"name": "CLI WF Bad", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"step_type": "llm_agent", "prompt_text": "Translate to Spanish."}]}'
    )
    ref3 = make_engine([bad_json], {"n": 0})
    rc = cli_main(["workflow", "author", "bad", "--provider", ref3, "--create"])
    assert rc != 0, ("a remaining validation error under --create must exit non-zero", rc)
    with _session() as s:
        assert s.exec(select(Workflow).where(Workflow.name == "CLI WF Bad")).first() is None, (
            "a refused --create must create NOTHING"
        )
    print(
        "  6. CLI: `workflow author` prints the draft, --create creates it, --create "
        "refuses (non-zero) on a remaining prompt validation error"
    )


# --- 7. web routes: author renders review; confirm creates; RBAC 403 ----------
def check_web_author_confirm_and_rbac():
    # (a) author route renders the editable review page (auth off -> _ai_block passes).
    good_json = (
        '{"name": "Web WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": [{"name": "T", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to {target_locale}."}]}'
    )
    ref = make_engine([good_json], {"n": 0})
    client = TestClient(app_ref())
    r = client.post("/workflows/author", data={"description": "en to es", "provider": ref})
    assert r.status_code == 200, r.status_code
    assert "Review the drafted workflow" in r.text and "Web WF" in r.text
    assert 'name="s0_prompt_text"' in r.text, "review page must render editable steps"
    # The help text must show the literal placeholder {target_locale}, not the raw Jinja
    # literal-brace spelling {'{target_locale}'} (T1 F2: that leaked onto the page).
    assert "{target_locale}" in r.text, "review page must show the {target_locale} placeholder"
    assert "{'{" not in r.text, "review page leaked a raw Jinja literal-brace token"

    # (b) confirm creates the workflow atomically + redirects to its builder.
    r = client.post(
        "/workflows/author/confirm",
        data={
            "step_count": "1",
            "name": "Web WF Confirmed",
            "customer_id": "",
            "source_locale": "en-US",
            "target_locales": ["es-ES"],
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate {text} to Spanish.",
            "s0_qe_threshold": "70",
            "s0_tm_examples_window": "3",
            "s0_tm_examples": "1",
            "notes_for_reviewer": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/workflows/" in r.headers["location"], r.status_code
    with _session() as s:
        wf = s.exec(select(Workflow).where(Workflow.name == "Web WF Confirmed")).first()
        assert (
            wf is not None
            and len(s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf.id)).all())
            == 1
        )

    # (c) confirm with a tampered payload re-renders the review page with errors +
    # creates nothing (a prompt missing {text}).
    before = _counts()
    r = client.post(
        "/workflows/author/confirm",
        data={
            "step_count": "1",
            "name": "Web WF Reject",
            "source_locale": "en-US",
            "target_locales": ["es-ES"],
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate to Spanish.",
            "notes_for_reviewer": "",
        },
    )
    assert r.status_code == 200 and "could not be created" in r.text, r.status_code
    assert _counts() == before, "a rejected web confirm must create nothing"

    # (d) RBAC: a translator (can_write, NOT can_tune_ai) is 403 on all three routes.
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
    for path, data in (
        ("/workflows/author", {"description": "x", "provider": ""}),
        ("/workflows/author/confirm", {"step_count": "0", "name": "x", "source_locale": "en-US"}),
        ("/workflows/models", {"provider": ""}),
    ):
        r = client.post(path, data=data)
        assert r.status_code == 403, (path, r.status_code)
    print(
        "  7. web: author renders the review page, confirm creates + redirects, a "
        "tampered confirm re-renders with errors; translator 403 on all three routes"
    )


# --- 8. shared core: repair-retry (exactly one) + spend-block (zero calls) -----
def check_repair_retry_and_spend():
    # garbage JSON -> exactly ONE repair retry (2 calls) then a clean 502.
    counter = {"n": 0}
    ref = make_engine(["not json at all", "still not { json"], counter)
    raised = False
    with _session() as s:
        try:
            setup_author.author_workflow_setup(
                s, description="x", provider=ref, org_id=None, actor="tester"
            )
        except setup_author.SetupAuthorError as exc:
            raised = True
            assert exc.status == 502, exc.status
    assert raised, "unparseable JSON after a retry must raise SetupAuthorError"
    assert counter["n"] == 2, ("EXACTLY one repair retry: 2 provider calls", counter["n"])

    # Web: the author route surfaces a clean 502 error page, never a 500.
    counter2 = {"n": 0}
    ref2 = make_engine(["nope", "nope again"], counter2)
    client = TestClient(app_ref())
    r = client.post("/workflows/author", data={"description": "x", "provider": ref2})
    assert r.status_code == 502, ("web author must surface a clean 502", r.status_code)
    assert counter2["n"] == 2, ("web path also does exactly one repair retry", counter2["n"])

    # spend-cap blocked -> clean message with ZERO provider calls.
    period = spend.period_key()
    settings.set_many({"spend_cap_usd": "1.0"})
    with _session() as s:
        row = s.get(SpendLedger, period) or SpendLedger(period=period, accumulated_usd=0.0)
        row.accumulated_usd = 50.0
        s.add(row)
        s.commit()
    try:
        counter3 = {"n": 0}
        ref3 = make_engine(['{"name": "x", "steps": []}'], counter3)
        raised = False
        with _session() as s:
            try:
                setup_author.author_workflow_setup(
                    s, description="x", provider=ref3, org_id=None, actor="tester"
                )
            except setup_author.SetupAuthorError as exc:
                raised = True
                assert "cap" in str(exc).lower(), str(exc)
        assert raised, "an over-cap real provider must be refused before calling"
        assert counter3["n"] == 0, ("a spend-blocked request makes ZERO calls", counter3["n"])
    finally:
        settings.set_many({"spend_cap_usd": "0"})
        with _session() as s:
            row = s.get(SpendLedger, period)
            if row is not None:
                row.accumulated_usd = 0.0
                s.add(row)
                s.commit()
    print(
        "  8. shared core: garbage JSON -> exactly ONE repair retry then clean 502; "
        "spend-cap blocked -> clean message, ZERO provider calls"
    )


# --- REVIEW FIX 1 (HIGH, converted for T1): a draft creates ONE ungrouped workflow
# whose step config (glossary_id, tm_examples/window, needs_review, assignee_email) is
# fully applied. The old multi-pair "every variant preserves config" probe becomes, on
# the language-free draft path, "the single workflow's step config survives intact".
def check_draft_step_config_fidelity():
    gid = _approved_glossary("MPFidTerms", [("en-US", "es-ES")])
    payload = {
        "name": "Step fidelity wf",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["es-ES", "fr-FR", "de-DE"],
        "notes_for_reviewer": "",
        "steps": [
            {
                "name": "Translate",
                "step_type": "llm_agent",
                "prompt_text": "Translate {text} to {target_locale}.",
                "glossary_id": str(gid),
                "tm_examples": False,
                "tm_examples_window": "0",
            },
            {
                "name": "Human check",
                "step_type": "human_review",
                "needs_review": True,
                "assignee_email": "linguist@firm.com",
            },
        ],
    }
    with _session() as s:
        wf = setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        base_id, group_id = wf.id, wf.group_id
    assert group_id is None, "a draft never lands in a group (no fan-out)"

    with _session() as s:
        steps = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == base_id)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert len(steps) == 2, len(steps)
        s0, s1 = steps
        assert s0.glossary_id == gid, ("glossary_id lost", s0.glossary_id)
        assert s0.tm_examples is False, ("tm_examples reset", s0.tm_examples)
        assert s0.tm_examples_window == 0, ("tm window reset", s0.tm_examples_window)
        assert s1.needs_review is True, ("needs_review lost", s1.needs_review)
        assert s1.assignee_email == "linguist@firm.com", (
            "assignee_email lost",
            s1.assignee_email,
        )
    print(
        "  FIX-1 (HIGH): draft -> ONE ungrouped workflow; glossary_id, tm_examples/window, "
        "needs_review + assignee all applied intact"
    )


# --- REVIEW FIX 2 (MED): a confirm-rejection re-render keeps the customer + per-step
# glossary <option selected>. The defect: the form parse left customer_id/glossary_id
# as strings, so the template's int-typed `== c.id` / `== g.id` compares failed and
# the selections silently reset. Reviewer's probe_rerender_editloss as a pinned check.
def check_confirm_rerender_preserves_selection():
    with _session() as s:
        cust = content.customer_create(s, "AcmeRerender")
        cust_id = cust.id
    gid = _approved_glossary("RerenderTerms", [("en-US", "es-ES")])
    client = TestClient(app_ref())
    # A valid customer + a step0 with a valid pinned glossary, but step1 is POISONED
    # (prompt missing {text}) so the whole confirm is rejected + re-rendered.
    r = client.post(
        "/workflows/author/confirm",
        data={
            "step_count": "2",
            "name": "Rerender wf",
            "customer_id": str(cust_id),
            "source_locale": "en-US",
            "target_locales": ["es-ES"],
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate {text} to Spanish.",
            "s0_glossary_id": str(gid),
            "s0_qe_threshold": "70",
            "s0_tm_examples_window": "3",
            "s1_step_type": "llm_agent",
            "s1_prompt_text": "Fix all errors.",  # no {text}
            "notes_for_reviewer": "",
        },
    )
    assert r.status_code == 200 and "could not be created" in r.text, r.status_code
    html = r.text
    assert f'value="{cust_id}" selected' in html, (
        "the chosen customer must stay selected on the error re-render"
    )
    assert f'value="{gid}" selected' in html, (
        "the step-0 pinned glossary must stay selected on the error re-render"
    )
    print(
        "  FIX-2 (MED): confirm-rejection re-render keeps the customer + step-0 "
        "glossary <option selected> (int/str normalization in _parse_review_form)"
    )


# --- REVIEW FIX 3 (LOW): a forged step_count is clamped so the parse cannot iterate
# unboundedly. A huge count returns a clean bounded parse, not a hang.
def check_step_count_clamped():
    from app.web.workflows import _MAX_REVIEW_STEPS, _parse_review_form

    class _Form:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

        def getlist(self, k):
            v = self._d.get(k)
            return [v] if v else []

    payload, _desc = _parse_review_form(
        _Form({"step_count": "100000000", "name": "x", "source_locale": "en-US"})
    )
    assert len(payload["steps"]) <= _MAX_REVIEW_STEPS, (
        "a forged step_count must be clamped, not iterated",
        len(payload["steps"]),
    )
    # A negative count floors at 0 (no steps, no crash).
    payload2, _ = _parse_review_form(_Form({"step_count": "-5", "name": "x"}))
    assert payload2["steps"] == [], ("a negative step_count floors to 0", payload2["steps"])
    print(
        f"  FIX-3 (LOW): step_count clamped to <= {_MAX_REVIEW_STEPS} (forged huge "
        "count is a bounded parse; negative floors to 0)"
    )


# --- REVIEW FIX 4 (cross-branch): the foreign-org engine guard now covers WORKFLOW
# authoring. author_workflow_setup must thread org_id into _author_json so a
# foreign-org engine:<id> is refused as not-live (clean 400) BEFORE any provider
# call. Mirrors branch A's check_authoring_provider_org_guard for the workflow seam.
def check_workflow_authoring_org_guard():
    from app.models import Organization
    from app.services.providers import (
        OpenAICompatibleProvider,
        register_provider,
    )

    with _session() as s:
        oa, ob = Organization(name="WfAuthOrgA"), Organization(name="WfAuthOrgB")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        ORG_A, ORG_B = oa.id, ob.id

    draft_json = (
        '{"name": "Ok", "source_locale": "en-US", '
        '"target_locale": "fr-FR", "steps": [{"name": "T", '
        '"step_type": "llm_agent", "prompt_text": "Translate {text}."}]}'
    )
    counter = {"n": 0}
    _ENGINE_SEQ["n"] += 1
    prof = create_profile(
        f"WfOrgLLM{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
        org_id=ORG_B,
    )
    ref = f"engine:{prof.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(prof.id), transport=json_transport([draft_json], counter)
        ),
    )

    # (a) a foreign-org (ORG_A) caller using ORG_B's engine -> clean 400, ZERO calls.
    raised = False
    with _session() as s:
        try:
            setup_author.author_workflow_setup(
                s, description="x", provider=ref, org_id=ORG_A, actor="t"
            )
        except setup_author.SetupAuthorError as exc:
            raised = True
            assert exc.status == 400, ("a foreign engine is a clean 400", exc.status)
    assert raised, "workflow authoring must refuse a foreign-org engine"
    assert counter["n"] == 0, ("the guard fires BEFORE any provider call", counter["n"])

    # (b) same-org authoring still works (guard does not over-block).
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="x", provider=ref, org_id=ORG_B, actor="t"
        )
        assert out["fields"]["name"] == "Ok", out["fields"]
    assert counter["n"] == 1, ("the same-org path makes exactly one call", counter["n"])
    print(
        "  FIX-4 (cross-branch): a foreign-org engine ref is refused as not-live at "
        "the WORKFLOW authoring seam (org_id now threaded to _author_json)"
    )


# --- REVIEW FIX 5 (atomicity): if a post-base write faults mid-way (e.g. add_step
# raises), apply_workflow_draft must delete the created workflow + any partial steps
# (fail-closed) and re-raise WorkflowApplyError -- never leave a partial workflow behind.
def check_apply_atomic_on_step_fault():
    from app.services import workflows as wf_service

    wf_before, st_before = _counts()
    real_add = wf_service.add_step

    def _boom(session, workflow_id, *args, **kwargs):
        raise RuntimeError("simulated mid-write add_step fault")

    payload = {
        "name": "Atomic fault wf",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["es-ES", "fr-FR", "de-DE"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "llm_agent", "prompt_text": "Translate {text}."},
            {"step_type": "lqa"},
        ],
    }
    set_pieces("app.services.workflows", "add_step", _boom)
    try:
        raised = False
        with _session() as s:
            try:
                setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
            except setup_author.WorkflowApplyError as exc:
                raised = True
                assert exc.errors, "the fault must carry a clean message"
        assert raised, "a mid-write step fault must raise WorkflowApplyError"
    finally:
        set_pieces("app.services.workflows", "add_step", real_add)

    assert _counts() == (wf_before, st_before), (
        "a mid-write fault must leave ZERO workflow/step rows (fail-closed cleanup)",
        _counts(),
        (wf_before, st_before),
    )
    print(
        "  FIX-5 (atomicity): a mid-write add_step fault -> the partial workflow "
        "is deleted, clean WorkflowApplyError, zero rows"
    )


# ============================================================================
# SPEC-language-agnostic-workflows T1 -- the draft path has NO languages and NO fan-out.
#
# The old Phase-2 ADAPT toggle fanned several targets out through copy_to_pairs(adapt=...)
# and stamped a pair on every draft. T1 removes that from the draft path: a draft makes
# exactly ONE ungrouped workflow with no pair, and `--adapt` / the old `adapt` field are
# ignored no-ops. copy_to_pairs + adapt behaviour still lives on the GROUP seam and is
# covered by verify_workflow_groups + verify_group_pair_ux.
# ============================================================================


# --- T1: `adapt=True` on a draft is a clean no-op (no variants, no pair) ------------
def check_adapt_flag_is_noop():
    payload = {
        "name": "Adapt noop wf",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["es-ES", "fr-FR", "de-DE"],
        "notes_for_reviewer": "",
        "steps": [
            {
                "name": "Translate",
                "step_type": "llm_agent",
                "prompt_text": "Translate {text} to {target_locale}.",
            }
        ],
    }
    wf_before, _ = _counts()
    with _session() as s:
        wf = setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester", adapt=True)
        wf_id, group_id = wf.id, wf.group_id
    wf_after, _ = _counts()
    assert wf_after == wf_before + 1, "adapt=True creates exactly ONE workflow"
    assert group_id is None, "adapt=True never creates a group"
    with _session() as s:
        wf = s.get(Workflow, wf_id)
        assert wf.source_locale is None and wf.target_locale is None, (
            "no pair stamped; adapt is a no-op on this path",
        )
    print("  T1: draft adapt=True -> one ungrouped workflow, no pair (no-op)")


# --- T1: CLI `--create --adapt` prints the ignored line and makes ONE workflow --------
def check_cli_adapt_ignored():
    draft_json = (
        '{"name": "CLI Adapt WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"pairs": ["fr-FR"], "steps": [{"name": "T", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to {target_locale}."}]}'
    )
    ref = make_engine([draft_json], {"n": 0})
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(
            ["workflow", "author", "en to es+fr", "--provider", ref, "--create", "--adapt"]
        )
    assert rc == 0, ("--create --adapt exits 0", rc)
    assert "--adapt is ignored" in err.getvalue(), (
        "--adapt must print the ignored line",
        err.getvalue(),
    )
    with _session() as s:
        wf = s.exec(select(Workflow).where(Workflow.name == "CLI Adapt WF")).first()
        assert wf is not None, "--create must create the (single) workflow"
        assert wf.group_id is None, "a drafted workflow is never grouped"
        assert wf.source_locale is None and wf.target_locale is None, ("no pair stamped",)
    print("  T1: CLI --create --adapt -> ignored line + ONE ungrouped workflow, no pair")


# --- T1: the review screen shows NO language pair(s) fieldset / fan-out ----------------
def check_web_review_no_language_fieldset():
    good = (
        '{"name": "No Pair Review", "source_locale": "en-US", "target_locale": "es-ES", '
        '"pairs": ["fr-FR", "de-DE"], "steps": [{"name": "T", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} to {target_locale}."}]}'
    )
    ref = make_engine([good], {"n": 0})
    client = TestClient(app_ref())
    r = client.post("/workflows/author", data={"description": "es+fr+de", "provider": ref})
    assert r.status_code == 200, r.status_code
    t = r.text
    # The removed review-page fieldset/fan-out used these SPECIFIC markers. A bare "language
    # pair" substring or name="source_locale"/name="target_locales" inputs still legitimately
    # appear elsewhere in the page (glossary help prose; the global New-project quick-create
    # dialog in base.html), so assert only markers unique to the old fan-out/fieldset block.
    assert "Language pair(s)" not in t, "no language-pair fieldset legend on the review page"
    assert "Multilingual fan-out" not in t, "no fan-out card on the review page"
    assert "data-wf-fanout" not in t and "data-wf-ml-rows" not in t, (
        "no multilingual fan-out markup"
    )
    assert ">Create workflow</button>" in t and "Create 1 workflow +" not in t, (
        "the button is a plain 'Create workflow'"
    )
    print("  T1: web review renders no language-pair fieldset, no fan-out, plain Create")


def main() -> None:
    check_three_step_draft()
    check_prompt_placeholder_warn_then_reject()
    check_confirm_atomicity()
    check_confirm_tampering_rejected()
    check_draft_never_fans_out()
    check_cli_parity()
    check_repair_retry_and_spend()
    # Review-finding regression checks (run BEFORE the auth-flip test below).
    check_draft_step_config_fidelity()
    check_confirm_rerender_preserves_selection()
    check_step_count_clamped()
    check_workflow_authoring_org_guard()
    check_apply_atomic_on_step_fault()
    # SPEC-language-agnostic-workflows T1 -- the draft path has no languages / fan-out
    # (run BEFORE the auth flip; these hit /workflows/author with auth off).
    check_adapt_flag_is_noop()
    check_cli_adapt_ignored()
    check_web_review_no_language_fieldset()
    check_web_author_confirm_and_rbac()  # LAST: flips auth ON globally
    print("PROMPTABLE WORKFLOW OK")


if __name__ == "__main__":
    main()
