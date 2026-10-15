"""Author-path output-budget verification (fix/author-path-run-completion).

The maintainer hit, LIVE in prod, a "Design a workflow" 502: "The model ran out of output
space before finishing the setup draft." Root cause: setup_author._author_json AND
orchestrator.author_prompt called prov.complete() DIRECTLY with a fixed/heuristic
budget, bypassing run_completion. A multi-step draft JSON (or a full step prompt)
easily exceeds that budget, and a direct call gets NO escalate-on-truncation retry,
NO Prompt-Tap capture, and an imprecise (model=None) ledger stamp.

The fix routes BOTH author paths through run_completion with a generous STARTING
output budget (min(model ceiling, 16384)); run_completion escalates toward the model
ceiling on truncation, captures the call, and books each billed attempt with the resolved
model. This suite proves it, all OFFLINE (throwaway DATA_DIR, blank keys):

  1. A first attempt that truncates now ESCALATES (16384 -> 32768) and SUCCEEDS -- for
     BOTH the setup-draft path (author_project_setup) and the step-prompt path
     (author_prompt). Pre-fix (fixed 4096, no escalation) both raise -> this FAILS.
  2. A drafting call is CAPTURED by the Prompt Tap (model/budget/truncation visible),
     tagged with its task_origin -- the debugging tool was blind on the old direct path.
  3. The ledger books BOTH the truncated first attempt and the successful retry,
     each exactly once, with no double-count from any leftover accounting.
  4. A genuine AT-CEILING truncation still fails LOUD with the NEW message on all three
     surfaces: SetupAuthorError(502) at the service + web /projects/author.json 502;
     author_prompt re-raises ProviderTruncated -> web /workflows/{id}/author.json 400 +
     CLI `workflow author-prompt` non-zero exit. All carry the NEW "expanded to the
     model's ceiling / pick a larger-output model" wording (no misleading "shorter
     description" as the primary advice).
  5. DeadEngineError (dead engine:<id> ref) and spend-cap-block behavior are UNCHANGED:
     a dead ref -> 400 (setup) / DeadEngineError (author_prompt), ZERO provider calls;
     an over-cap real engine -> 502 (setup, zero calls) / block-message-as-draft
     (author_prompt, zero calls).

Hermetic like the rest of tests/verify_*.py. ASCII-only output for Windows consoles.
"""

import contextlib
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_authorbudget_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine as db_engine
from app.db import init_db
from app.main import app
from app.models import Workflow
from app.services import (
    engines,
    orchestrator,
    pricing,
    prompt_tap,
    providers,
    setup_author,
)
from app.services import settings as settings_svc
from app.services import (
    spend as spend_svc,
)
from app.services.providers import (
    DeadEngineError,
    OpenAICompatibleProvider,
    Provider,
    ProviderTruncated,
    register_provider,
)

init_db()

# The NEW at-ceiling wording all three surfaces must now use (and the OLD misleading
# advice they must NOT lead with).
_NEW_MARK = "expanding the output budget to this model's ceiling"
_NEW_ADVICE = "larger output token limit"
_OLD_MISLEAD = "Try a shorter"


# --- a fake provider that truncates below a budget threshold, mirroring the real
# guard (attaches budget + ceiling so run_completion can escalate). Registered under a
# REAL, PRICED provider name ("claude") so the ledger test sees a non-zero cost, and it
# is NOT MockProvider/LocalProvider so spend.is_exempt is False (it accrues). --------
class _EscalatingProvider(Provider):
    name = "claude"

    def __init__(self, content, truncate_until=None):
        # truncate while the RESOLVED budget < truncate_until; succeed once >=. None =>
        # never truncate. `content` is what a success returns (JSON for setup, a plain
        # prompt for author_prompt).
        self.content = content
        self.truncate_until = truncate_until
        self.budgets = []  # every resolved max_tokens, in call order
        self.usages = []
        self.last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        model = kw.get("model")
        budget = providers._resolve_max_tokens(prompt, system, model, kw)
        self.budgets.append(budget)
        if self.truncate_until is not None and budget < self.truncate_until:
            self.last_usage = {"input_tokens": 100, "output_tokens": budget}
            self.usages.append(self.last_usage.copy())
            e = ProviderTruncated(f"claude hit max_tokens ({budget}) -- truncated")
            e.budget = budget
            e.ceiling = config.model_output_ceiling(model)
            raise e
        self.last_usage = {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        self.usages.append(self.last_usage.copy())
        return self.content


@contextlib.contextmanager
def _registered(ref, prov):
    register_provider(ref, prov)
    try:
        yield prov
    finally:
        providers._OVERRIDES.pop(ref, None)


def _truncating_engine(name, *, finish, api_key="sk-x"):
    """A keyed openai_compatible engine whose transport always returns `finish`
    (use 'length' to always truncate). Returns the engine:<id> ref."""

    def h(req):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "partial..."}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            },
        )

    p = engines.create_profile(
        name,
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key=api_key,
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=engines.get_profile(p.id), transport=httpx.MockTransport(h)
        ),
    )
    return ref


def _new_workflow(name):
    wf = Workflow(name=name, scope="global")
    with Session(db_engine) as s:
        s.add(wf)
        s.commit()
        s.refresh(wf)
        return wf.id


# --- 1. truncated first attempt ESCALATES then SUCCEEDS (both author paths) ----
def check_escalation_succeeds_both_paths():
    # (a) setup-draft path. ceiling(claude-sonnet-4)=64000; seed=16384; truncate_until
    # 30000 => attempt0 (16384) truncates, attempt1 (32768) succeeds. Pre-fix (fixed
    # 4096, no escalation) truncates forever -> SetupAuthorError -> this FAILS.
    prov = _EscalatingProvider('{"name": "Draft Project"}', truncate_until=30000)
    with _registered("claude", prov), Session(db_engine) as session:
        draft = setup_author.author_project_setup(
            session,
            description="a big multi-step setup",
            provider="claude",
            org_id=None,
            actor="t",
            model="claude-sonnet-4",
        )
    assert draft["fields"]["name"] == "Draft Project", draft
    assert prov.budgets == [16384, 32768], (
        "setup draft must escalate 16384->32768 then succeed",
        prov.budgets,
    )

    # (b) step-prompt path (orchestrator.author_prompt) escalates identically.
    prov2 = _EscalatingProvider("Translate {text} faithfully.", truncate_until=30000)
    with _registered("claude", prov2):
        out = orchestrator.author_prompt("a faithful translator", "claude", model="claude-sonnet-4")
    assert out == "Translate {text} faithfully.", out
    assert prov2.budgets == [16384, 32768], (
        "author_prompt must escalate 16384->32768 then succeed",
        prov2.budgets,
    )
    print(
        "  1. truncated first attempt ESCALATES (16384->32768) then SUCCEEDS on BOTH "
        "the setup-draft and step-prompt paths"
    )


# --- 2. a drafting call is captured by the Prompt Tap -------------------------
def check_tap_capture_present():
    prompt_tap.clear()
    prompt_tap.set_enabled(True)
    try:
        # setup path (fake claude) -> task 'setup_author', meta.task_origin present.
        prov = _EscalatingProvider('{"name": "Tapped"}')
        with _registered("claude", prov), Session(db_engine) as session:
            setup_author.author_project_setup(
                session,
                description="tap me",
                provider="claude",
                org_id=None,
                actor="t",
                model="claude-sonnet-4",
            )
        # step-prompt path via the default offline MockProvider -> task 'author_prompt'.
        orchestrator.author_prompt("a translator")
        recs = prompt_tap.read_records(limit=50)
    finally:
        prompt_tap.set_enabled(False)
    tasks = {r.get("task") for r in recs}
    assert "setup_author" in tasks, ("setup drafting call must be captured", tasks)
    assert "author_prompt" in tasks, ("prompt drafting call must be captured", tasks)
    setup_rec = next(r for r in recs if r.get("task") == "setup_author")
    assert (setup_rec.get("meta") or {}).get("task_origin") == "setup_author", setup_rec
    assert setup_rec.get("outcome") == "ok", ("captured outcome", setup_rec)
    assert (setup_rec.get("request") or {}).get("max_tokens"), (
        "the capture must carry the call's output budget",
        setup_rec,
    )
    print(
        "  2. drafting calls are Prompt-Tap captured (task + task_origin + budget) on "
        "both paths -- the tool is no longer blind on the author seam"
    )


# --- 3. the ledger books every billed attempt EXACTLY ONCE -------------------
def check_ledger_stamped_exactly_once():
    before = spend_svc.spent_this_period()
    # The truncated first attempt (16384) and the successful retry both cost money.
    prov = _EscalatingProvider('{"name": "Billed"}', truncate_until=30000)
    with _registered("claude", prov), Session(db_engine) as session:
        setup_author.author_project_setup(
            session,
            description="bill me once",
            provider="claude",
            org_id=None,
            actor="t",
            model="claude-sonnet-4",
        )
    after = spend_svc.spent_this_period()
    assert len(prov.usages) == 2
    expected = sum(pricing.cost_usd("claude", "claude-sonnet-4", u) for u in prov.usages)
    assert expected > 0, ("test needs a priced provider/model", expected)
    delta = after - before
    assert abs(delta - expected) < 1e-9, (
        "ledger must book the truncated attempt and successful retry exactly once",
        delta,
        expected,
    )
    print("  3. ledger books truncated and successful attempts exactly once")


# --- 4. at-ceiling truncation fails LOUD with the NEW message, all surfaces ---
def check_at_ceiling_loud_new_message():
    # (a) service: setup path -> SetupAuthorError(502), NEW wording, no old advice.
    prov = _EscalatingProvider('{"name": "x"}', truncate_until=10**9)  # never satisfiable
    raised = None
    with _registered("claude", prov), Session(db_engine) as session:
        try:
            setup_author.author_project_setup(
                session,
                description="too big",
                provider="claude",
                org_id=None,
                actor="t",
                model="claude-3-5",
            )  # ceiling 8192 -> seed clamps to ceiling
        except setup_author.SetupAuthorError as exc:
            raised = exc
    assert raised is not None and raised.status == 502, raised
    assert _NEW_MARK in str(raised) and _NEW_ADVICE in str(raised), str(raised)
    assert _OLD_MISLEAD not in str(raised), ("old misleading advice must be gone", str(raised))

    client = TestClient(app)
    # (b) web setup route /projects/author.json -> clean 502 + NEW message.
    ref_len = _truncating_engine("SetupTrunc", finish="length")
    try:
        r = client.post(
            "/projects/author.json", data={"description": "too big", "provider": ref_len}
        )
    finally:
        providers._OVERRIDES.pop(ref_len, None)
    assert r.status_code == 502, ("setup author route must surface a clean 502", r.status_code)
    assert _NEW_ADVICE in r.json().get("error", ""), r.json()

    # (c) author_prompt path re-raises ProviderTruncated -> web 400 + CLI non-zero.
    ref_len2 = _truncating_engine("PromptTrunc", finish="length")
    wf_id = _new_workflow("At-ceiling wf")
    try:
        # service: re-raises the typed error (loud contract preserved).
        raised2 = False
        try:
            orchestrator.author_prompt("too big a prompt", ref_len2)
        except ProviderTruncated:
            raised2 = True
        assert raised2, "author_prompt must RE-RAISE ProviderTruncated at ceiling"
        # web JSON step-prompt route -> 400 + NEW message.
        r2 = client.post(
            f"/workflows/{wf_id}/author.json",
            data={"description": "too big a prompt", "provider": ref_len2},
        )
        assert r2.status_code == 400, ("prompt author route -> clean 400", r2.status_code)
        assert _NEW_ADVICE in r2.json().get("error", ""), r2.json()
        assert _OLD_MISLEAD not in r2.json().get("error", ""), r2.json()
        # CLI -> non-zero exit + NEW message on stderr, no traceback.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli_main(
                [
                    "workflow",
                    "author-prompt",
                    str(wf_id),
                    "too big a prompt",
                    "--provider",
                    ref_len2,
                ]
            )
        assert rc != 0, ("CLI author-prompt at ceiling must exit non-zero", rc)
        assert _NEW_ADVICE in err.getvalue(), ("CLI must print the NEW message", err.getvalue())
    finally:
        providers._OVERRIDES.pop(ref_len2, None)
    print(
        "  4. at-ceiling truncation fails LOUD with the NEW message on all surfaces: "
        "service 502, web 502 (setup) + 400 (prompt), CLI non-zero"
    )


# --- 5. DeadEngineError + spend-block behavior UNCHANGED ----------------------
def check_dead_engine_and_spend_block_unchanged():
    dead_ref = "engine:999999"  # no such profile -> a dead ref

    # (a) dead ref: author_prompt raises DeadEngineError (before run_completion).
    raised_dead = False
    try:
        orchestrator.author_prompt("x", dead_ref)
    except DeadEngineError:
        raised_dead = True
    assert raised_dead, "author_prompt on a dead engine:<id> must raise DeadEngineError"

    # (b) dead ref: setup path -> clean SetupAuthorError(400), zero provider calls.
    with Session(db_engine) as session:
        try:
            setup_author.author_project_setup(
                session, description="x", provider=dead_ref, org_id=None, actor="t"
            )
            assert False, "a dead ref must raise"
        except setup_author.SetupAuthorError as exc:
            assert exc.status == 400, ("dead ref -> 400", exc.status)

    # (c) spend cap tripped: a REAL over-cap engine makes ZERO calls on both paths.
    spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 2_000_000})  # ~$10
    settings_svc.set_many({"spend_cap_usd": "5"})
    try:
        assert spend_svc.would_block()[0] is True, "cap must be tripped for this check"
        prov = _EscalatingProvider('{"name": "never"}')
        with _registered("claude", prov):
            # setup path -> SetupAuthorError(502), zero calls.
            with Session(db_engine) as session:
                try:
                    setup_author.author_project_setup(
                        session,
                        description="blocked",
                        provider="claude",
                        org_id=None,
                        actor="t",
                        model="claude-sonnet-4",
                    )
                    assert False, "over-cap setup must refuse"
                except setup_author.SetupAuthorError as exc:
                    assert exc.status == 502, exc.status
            assert prov.budgets == [], ("blocked setup must make ZERO provider calls", prov.budgets)
            # author_prompt path -> returns the block message AS THE DRAFT, zero calls.
            draft = orchestrator.author_prompt("blocked", "claude", model="claude-sonnet-4")
            assert "cap" in draft.lower(), (
                "author_prompt must return the spend-cap message as the draft",
                draft,
            )
            assert prov.budgets == [], ("blocked author_prompt must make ZERO calls", prov.budgets)
    finally:
        settings_svc.set_many({"spend_cap_usd": ""})  # reset the cap for other suites
    print(
        "  5. dead engine:<id> (400 / DeadEngineError) and spend-cap block (502 / "
        "block-as-draft, zero calls) behavior UNCHANGED"
    )


def main() -> None:
    check_escalation_succeeds_both_paths()
    check_tap_capture_present()
    check_ledger_stamped_exactly_once()
    check_at_ceiling_loud_new_message()
    check_dead_engine_and_spend_block_unchanged()
    print("AUTHOR BUDGET OK")


if __name__ == "__main__":
    main()
