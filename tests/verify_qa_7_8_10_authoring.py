"""QA-7 / QA-8 / QA-10 authoring-surface regressions (offline + hermetic).

Wave-2 fixes from the planning notes:

  QA-7 (#37, MED, the substantive one): Design-a-workflow drafted step providers ignored
    the SELECTED engine. The "Use provider" pick drove only the authoring call, so the
    drafted steps' provider values (the model's free text) left "use the local model" +
    a registered engine:<id> running on a keyless built-in / an unreachable 'local', with
    the selected engine referenced by NOTHING. Fix: author_workflow_setup binds every
    EXECUTABLE step whose drafted provider resolves to nothing runnable to the concrete,
    runnable selection (clearing the drafted model so no provider/model family mismatch),
    surfaces ONE reviewer-visible binding warning, and leaves already-runnable providers
    alone. "Resolves to nothing runnable" covers a CONCRETE non-live provider AND a
    BLANK/absent provider ("inherit the run default") WHEN that run default
    (config.DEFAULT_PROVIDER) is itself non-live (keyless install -> MockProvider): a blank
    step would otherwise silently run on the mock, not the selected engine, so it is bound
    too and named in the SAME warning (QA-7 review follow-up, MED). When the run default IS
    live a blank inherits a runnable default and is LEFT UNTOUCHED (byte-identical).
    Byte-identical when no concrete / non-live selection was made.

  QA-8 (#38, LOW): the "Context - resolved" PACKAGE fact rendered UNCONDITIONALLY on every
    translate step, over-claiming a resolvable context on a brand-new install. Fix: gate it
    on scope-level availability (workflow_has_context), exactly as the style-guide fact is
    gated on workflow_has_guide.

  QA-10 (#40, LOW): the multilingual review HEADLINE counted all N target locales ("3
    language variants") while the fan-out rows + confirm button counted base + N-1 ("2
    language variants"). Fix: the headline counts VARIANTS (n_locales - 1) too.

Each check FAILS on the pre-fix tree (see the per-check comments) and passes here. Provider
wiring reuses the connector-test fixture pattern (a REAL OpenAICompatibleProvider behind an
injected httpx.MockTransport, registered so the service resolves it). ASCII-only.

Final line: "QA 7/8/10 AUTHORING OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qa7810_")

import httpx
from sqlmodel import Session

from app.db import engine as db_engine
from app.db import init_db
from app.models import Customer, Organization, StepType, Workflow, WorkflowStep
from app.services import context_pkg, setup_author
from app.services import package_view as pv
from app.services import workflow_shapes as ws
from app.services.engines import create_profile, get_profile
from app.services.providers import (
    OpenAICompatibleProvider,
    provider_is_live,
    register_provider,
)

init_db()


# --- provider fixtures (openai chat-completions wire shape) ------------------
def _json_transport(responses, counter):
    def h(req):
        i = counter["n"]
        counter["n"] += 1
        body = responses[min(i, len(responses) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": body}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(h)


_SEQ = {"n": 0}


def _make_engine(responses, counter=None):
    """Register a LIVE, offered engine:<id> (openai_compatible, keyed + based). Returns its
    'engine:<id>' ref. Used both as the SELECTED authoring engine and as a live step
    provider that must be RESPECTED."""
    counter = counter if counter is not None else {"n": 0}
    _SEQ["n"] += 1
    p = create_profile(
        f"QA7Engine{_SEQ['n']}",
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


def _session():
    return Session(db_engine)


# --- QA-7: bind drafted dead/blank step providers to the selected engine ------
def check_qa7_binds_dead_step_providers_end_to_end():
    # Precondition: keyless built-ins are NON-runnable in the hermetic env, so a drafted
    # 'local'/'claude' step resolves to nothing runnable (the exact reported footgun).
    assert provider_is_live("claude") is False, "hermetic env must keep 'claude' keyless"
    assert provider_is_live("local") is False, "hermetic env must keep 'local' base-less"

    # config.DEFAULT_PROVIDER defaults to keyless 'claude' in this hermetic env (init_env
    # blanks the keys), so it is NON-LIVE -> a blank step silently resolves to MockProvider.
    from app import config

    assert provider_is_live(config.DEFAULT_PROVIDER) is False, (
        "hermetic env must keep the run default non-live",
        config.DEFAULT_PROVIDER,
    )
    live2 = _make_engine(["{}"])  # a live engine named explicitly in a step
    draft_json = (
        '{"name": "Bind WF", "source_locale": "en-US", "target_locale": "es-ES", '
        '"steps": ['
        '{"name": "Translate", "step_type": "llm_agent", "provider": "local", '
        '"model": "gpt-4o-mini", '  # a cloud model name pinned onto a dead engine
        '"prompt_text": "Translate {text} to {target_locale}."}, '
        '{"name": "LQA", "step_type": "lqa", "provider": "claude"}, '
        '{"name": "Fix", "step_type": "llm_agent", "prompt_text": "Fix {prior}."}, '
        '{"name": "Rewrite", "step_type": "llm_agent", "provider": "' + live2 + '", '
        '"prompt_text": "Rewrite {prior}."}, '
        '{"name": "Sign-off", "step_type": "human_review"}]}'
    )
    live1 = _make_engine([draft_json])  # the SELECTED (authoring) engine
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="use the local model", provider=live1, org_id=None, actor="tester"
        )
    f, w = out["fields"], out["warnings"]
    s0, s1, s2, s3, s4 = f["steps"]
    # A CONCRETE drafted provider that resolves to NOTHING RUNNABLE -> bound to the live
    # selection. PRE-FIX: s0 stays "local", s1 stays "claude" (the selected engine referenced
    # by nothing), so these asserts fail on the old tree.
    assert s0["provider"] == live1, ("dead 'local' must bind to the selection", s0["provider"])
    assert s1["provider"] == live1, ("dead 'claude' must bind to the selection", s1["provider"])
    # LOW: the drafted model is cleared on bind so the bound engine uses its OWN default
    # (a cloud model name has no meaning on the bound local engine).
    assert s0["model"] == "", ("the drafted model is cleared on bind", s0["model"])
    # A BLANK provider ("inherit the run default") whose run default is non-live here (keyless
    # 'claude' -> MockProvider) would silently run on the mock, NOT the selected engine -- the
    # same footgun spelled as a blank -- so it is BOUND too and its (absent) model cleared.
    # PRE-FIX: s2 stayed "" (unbound), so this assert fails on the old tree.
    assert s2["provider"] == live1, ("a blank + non-live default is bound", s2["provider"])
    assert s2["model"] == "", ("the bound blank step uses the engine default model", s2["model"])
    # A DIFFERENT provider that IS runnable is RESPECTED.
    assert s3["provider"] == live2, ("a runnable named provider is kept", s3["provider"])
    # human_review makes no model call -> untouched.
    assert s4["provider"] == "", ("a human-review step is not an executable step", s4)
    # ONE aggregate binding warning naming EVERY bound step (Translate, LQA, Fix) + the engine;
    # the live-named + human steps are not named. No second/inherit warning under the bind fix.
    binds = [x for x in w if "bound to" in x]
    assert len(binds) == 1, ("one aggregate binding warning", w)
    assert (
        live1 in binds[0] and "Translate" in binds[0] and "LQA" in binds[0] and "Fix" in binds[0]
    ), binds
    assert "Rewrite" not in binds[0] and "Sign-off" not in binds[0], (
        "a live-named + a human step are not bound",
        binds,
    )
    # The subject here is the BINDING warning: a blank step must be bound, not flagged with
    # a separate 'inherits the default' warning of its own. #148 added an unrelated warning
    # that happens to contain the word 'inherits' (a CHECK inheriting the translate step's
    # engine), so the old bare substring test matched the wrong sentence. Scope it to the
    # binding subject rather than to a word that two different warnings share.
    assert not [x for x in w if "inherit" in x.lower() and "judging the engine" not in x], (
        "the blank step is bound, not flagged with a separate inherit warning",
        w,
    )
    print(
        "  QA-7 (e2e): dead 'local'/'claude' AND the blank step (non-live run default) bound "
        "to the selected engine with models cleared + one aggregate binding warning; a live "
        "named provider is respected, human step untouched"
    )


def check_qa7_noop_without_a_live_selection():
    """The binding is a NO-OP (byte-identical) when there is no concrete selection, a
    blank selection, or a selection that is not itself runnable -- binding to a dead
    selection cannot make a step runnable. Driven through the helper directly so it is
    deterministic (no authoring call). PRE-FIX: _bind_drafted_step_providers does not
    exist, so this check errors on the old tree."""
    live = _make_engine(["{}"])
    offered = ["claude", "local", "openai", "gemini", live]

    def _draft():
        return {
            "fields": {
                "steps": [
                    {"step_type": "llm_agent", "provider": "local", "name": "T"},
                    {"step_type": "lqa", "provider": "claude", "name": "Q"},
                ]
            },
            "warnings": [],
        }

    for selection in (None, "", "claude"):  # no pick / blank / a NON-LIVE pick
        d = _draft()
        setup_author._bind_drafted_step_providers(d, selection, offered)
        assert [st["provider"] for st in d["fields"]["steps"]] == ["local", "claude"], (
            "no live selection -> providers byte-identical",
            selection,
            d["fields"]["steps"],
        )
        assert d["warnings"] == [], ("no live selection -> no warning", selection, d)

    # A live selection DOES bind both dead providers, with one warning.
    d = _draft()
    setup_author._bind_drafted_step_providers(d, live, offered)
    assert [st["provider"] for st in d["fields"]["steps"]] == [live, live], d["fields"]["steps"]
    assert len(d["warnings"]) == 1 and "bound to" in d["warnings"][0], d["warnings"]
    print(
        "  QA-7 (no-op): no selection / blank / non-live selection -> byte-identical; a "
        "live selection binds + warns"
    )


def check_qa7_blank_bound_on_dead_default_and_model_cleared():
    """QA-7 review follow-up (MED + LOW). A BLANK/absent executable-step provider inherits
    config.DEFAULT_PROVIDER at run time. Whether it is BOUND to the live selection depends on
    that default:
      * default NON-LIVE (keyless install) -> the blank step would silently resolve to
        MockProvider (NOT the selected engine), the exact QA-7 footgun spelled as a blank,
        so it is BOUND to the selection and named in the SAME aggregate "bound to" warning
        (MED: close the "neither bound nor mentioned" gap; the human can still change it on
        the review screen).
      * default LIVE -> the blank step inherits a runnable default -> LEFT ALONE and NOT
        flagged, byte-identical to today.
    On every bind the drafted `model` is CLEARED so the bound engine uses its own default
    (LOW: no provider/model family mismatch); a left-alone step keeps its model. Driven
    through the helper with a monkeypatched config.DEFAULT_PROVIDER so both branches are
    deterministic and hermetic. PRE-FIX (HEAD): a blank step was left alone even on a non-live
    default (no bind, no warning) and a bound step's model was left as drafted, so the bind +
    model-clear asserts fail on the pre-fix tree."""
    from app import config

    live = _make_engine(["{}"])
    offered = ["claude", "local", "openai", "gemini", live]

    def _draft():
        # one BLANK-provider executable step carrying a drafted model, + one concrete-DEAD
        # provider step carrying a drafted model (to pin the model-clear on both bind paths).
        return {
            "fields": {
                "steps": [
                    {
                        "step_type": "llm_agent",
                        "provider": "",
                        "name": "Blank",
                        "model": "drafted-model",
                    },
                    {
                        "step_type": "llm_agent",
                        "provider": "local",
                        "name": "Dead",
                        "model": "cloud-model",
                    },
                ]
            },
            "warnings": [],
        }

    orig = config.DEFAULT_PROVIDER
    try:
        # (a) run default NON-LIVE -> BOTH the blank (inherits a non-live default) and the
        # concrete-dead provider are bound to the selection with their models cleared, named
        # in ONE aggregate "bound to" warning (no separate/second warning).
        config.DEFAULT_PROVIDER = "claude"
        assert provider_is_live("claude") is False, "keyless 'claude' must be non-live"
        d = _draft()
        setup_author._bind_drafted_step_providers(d, live, offered)
        blank, dead = d["fields"]["steps"]
        assert blank["provider"] == live, ("a blank + non-live default is BOUND", blank)
        assert blank["model"] == "", ("the drafted model is cleared on bind", blank)
        assert dead["provider"] == live, ("a concrete dead provider is bound", dead)
        assert dead["model"] == "", ("the drafted model is cleared on bind", dead)
        binds = [x for x in d["warnings"] if "bound to" in x]
        assert len(binds) == 1 and "Blank" in binds[0] and "Dead" in binds[0], d["warnings"]
        assert len(d["warnings"]) == 1, ("one aggregate warning, no extras", d["warnings"])

        # (b) run default LIVE -> the blank step inherits a runnable default: LEFT ALONE
        # (provider + model untouched) and NOT flagged; only the concrete-dead provider is
        # bound + model cleared, one warning naming only Dead.
        config.DEFAULT_PROVIDER = live
        assert provider_is_live(live) is True, "the engine ref must be live"
        d = _draft()
        setup_author._bind_drafted_step_providers(d, live, offered)
        blank, dead = d["fields"]["steps"]
        assert blank["provider"] == "", ("blank inherits a LIVE default -- left alone", blank)
        assert blank["model"] == "drafted-model", ("a left-alone step keeps its model", blank)
        assert dead["provider"] == live and dead["model"] == "", dead
        assert (
            len(d["warnings"]) == 1
            and "bound to" in d["warnings"][0]
            and "Dead" in d["warnings"][0]
            and "Blank" not in d["warnings"][0]
        ), ("a blank step is not bound/flagged when the run default is live", d["warnings"])
    finally:
        config.DEFAULT_PROVIDER = orig
    print(
        "  QA-7 (blank bind): a blank step is bound to the selection (model cleared) only "
        "when the run default is non-live; with a live default it is left alone; a concrete "
        "dead provider is always bound with its model cleared"
    )


# --- QA-8: the Context PACKAGE fact is gated on scope availability -------------
def check_qa8_context_fact_gated():
    """Pure service check: the translate-step Context fact is OMITTED when no context is
    available and shown (per document) when it is -- mirroring the style-guide gate. The
    glossary auto-attach + TM few-shot facts are always-on per-document MECHANISMS and stay
    unconditional. PRE-FIX: the Context fact was appended unconditionally, so the first
    assert (default has_context=False -> omitted) fails on the old tree."""
    step = {"step_type": StepType.LLM_AGENT, "name": "T"}
    bare = {f["key"] for f in pv.step_facts(step)["package"]}  # default has_context=False
    assert "context" not in bare, ("QA-8: Context omitted when none resolves", bare)
    assert "glossary" in bare and "tm" in bare, ("mechanisms stay unconditional", bare)
    shown = {f["key"]: f for f in pv.step_facts(step, has_context=True)["package"]}
    assert "context" in shown and shown["context"]["scope"] == pv.PER_DOCUMENT, shown

    # End-to-end through workflow_has_context + workflow_package_view: a bare workflow
    # resolves no context (Context fact omitted); adding a customer-scoped ContextProfile
    # flips workflow_has_context True and the fact shows, per document.
    with _session() as s:
        if s.get(Organization, 1) is None:
            s.add(Organization(id=1, name="QA Org"))
            s.commit()
        cust = Customer(name="QA8 Cust", org_id=1)
        s.add(cust)
        s.commit()
        s.refresh(cust)
        wf = Workflow(name="QA8 WF", scope="customer", customer_id=cust.id, org_id=1)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="Translate", step_type=StepType.LLM_AGENT
            )
        )
        s.commit()

    with _session() as s:
        wf = s.exec(_select_wf("QA8 WF")).first()
        steps = list(s.exec(_select_steps(wf.id)).all())
        assert pv.workflow_has_context(s, wf) is False, "bare workflow resolves no context"
        pkg_bare = _pkg(pv.workflow_package_view(s, wf, steps)[0])
        assert "context" not in pkg_bare, ("no profile -> Context omitted", set(pkg_bare))
        context_pkg.upsert_profile(
            s,
            org_id=1,
            customer_id=wf.customer_id,
            name="Tone",
            context_raw={"formality": "formal"},
            actor="qa",
        )
        assert pv.workflow_has_context(s, wf) is True, "a seeded profile resolves in scope"
        pkg_ctx = _pkg(pv.workflow_package_view(s, wf, steps)[0])
        assert "context" in pkg_ctx and pkg_ctx["context"]["scope"] == pv.PER_DOCUMENT, pkg_ctx
    print(
        "  QA-8: Context fact gated on availability (omitted bare, shown per-document with "
        "a ContextProfile); glossary/TM mechanisms stay unconditional"
    )


# --- QA-10: the review headline counts VARIANTS (base + n-1), not raw locales --
def check_qa10_headline_counts_variants():
    """PRE-FIX: shape_headline(n_locales=3) said '3 language variants' while the review
    rows + confirm button said '2'; this asserts the headline now agrees (base + n-1)."""
    steps = [{"step_type": StepType.LLM_AGENT, "name": "Translate"}]
    h = ws.shape_headline(steps, n_locales=3)
    assert "2 language variants" in h, ("headline counts base + (n-1) variants", h)
    assert "3 language variants" not in h, ("never the raw locale count", h)
    # n_locales in (None, 1) -> no fan-out note at all (byte-identical single-target).
    assert "language variants" not in ws.shape_headline(steps)
    assert "language variants" not in ws.shape_headline(steps, n_locales=1)
    print(
        "  QA-10: headline fan-out note counts variants (base + n-1), consistent with the "
        "rows/button; single-target has no note"
    )


# --- small query helpers (avoid importing select at module top for clarity) ---
def _select_wf(name):
    from sqlmodel import select

    return select(Workflow).where(Workflow.name == name)


def _select_steps(wf_id):
    from sqlmodel import select

    return (
        select(WorkflowStep)
        .where(WorkflowStep.workflow_id == wf_id)
        .order_by(WorkflowStep.order_index)
    )


def _pkg(view):
    return {f["key"]: f for f in view["package"]}


def main():
    check_qa7_binds_dead_step_providers_end_to_end()
    check_qa7_noop_without_a_live_selection()
    check_qa7_blank_bound_on_dead_default_and_model_cleared()
    check_qa8_context_fact_gated()
    check_qa10_headline_counts_variants()
    print("QA 7/8/10 AUTHORING OK")


if __name__ == "__main__":
    main()
