"""Prompt-authoring assistant verification (fix/prompt-authoring), offline+hermetic.

Covers the two field-reported failure modes + the no-refresh JSON route:

  1. The authoring call must accept the SAME provider choices a workflow step
     accepts -- INCLUDING an engine:<id> EngineProfile (a locally-hosted
     openai_compatible model) -- resolved through the standard get_provider seam.
     (Field bug #2: "fails every time with a local LLM.")
  2. A WEAKER model's deviating response shape (markdown code fence + a
     conversational preamble) must still yield usable draft text -- stripped, never
     an error and never a blank box.
  3. A HARD provider failure on the direct call (HTTP 401 / unreachable server /
     malformed body / NMT engine with no prompt channel) must surface as a CLEAN
     message on BOTH surfaces -- web JSON {"ok": false, "error": ...} (not a 500)
     and a non-zero CLI exit (no traceback) -- via ProviderCallError.
  4. The JSON (fetch) route returns the draft and persists NOTHING server-side
     (stateless) -- so the no-refresh path can drop it into the still-unsaved
     Prompt box without touching the rest of the Add-a-step form.

Provider wiring is exercised through the REAL OpenAICompatibleProvider with an
injected httpx.MockTransport (the connector-test fixture pattern), registered via
register_provider so BOTH get_provider (service/CLI) and the web routes resolve it.
RBAC parity for /author.json is asserted in verify_roles.py (which has the auth
harness). ASCII-only output for Windows consoles.

Also covers (adversarial-review regression, FIX 1): _clean_draft's preamble strip
is FENCE-GATED -- a leading line is dropped ONLY when doing so leaves nothing but a
single fenced block, never on its own. This closes two review findings: (a) content
words (prompt/draft/response/output) must never trigger a strip since a real
instruction routinely starts with one of them and ends in ':', and (b) even a
genuine-sounding chat-opener line must NOT be stripped when no fence proves the
split is safe -- otherwise real content directly following the colon (no fence,
e.g. "Here is the context:\n{text}") would be silently eaten. Multiple top-level
fences are left alone too (no orphan-fence Frankenstein).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_authoring_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine as db_engine
from app.db import init_db
from app.main import app
from app.models import Workflow, WorkflowStep
from app.services import engines, orchestrator
from app.services.orchestrator import _clean_draft
from app.services.providers import (
    OpenAICompatibleProvider,
    ProviderCallError,
    get_provider,
    provider_is_live,
    register_provider,
)

init_db()


# --- transports (openai chat-completions wire shape) -------------------------
def chat_transport(content, captured=None, status=200, finish="stop"):
    def h(req):
        if captured is not None:
            captured["path"] = req.url.path
            captured["auth"] = req.headers.get("Authorization")
            import json as _json

            captured["body"] = _json.loads(req.content)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad key"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(h)


def _engine(name, transport, api_key="sk-local"):
    """Create a keyed openai_compatible EngineProfile and register a transport-
    injected provider override for its engine:<id> ref, so get_provider (service,
    CLI, AND web routes) all resolve to it. Returns the engine:<id> ref string."""
    p = engines.create_profile(
        name,
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key=api_key,
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref, OpenAICompatibleProvider(profile=engines.get_profile(p.id), transport=transport)
    )
    return ref


def _new_workflow(name):
    wf = Workflow(name=name, scope="global")
    with Session(db_engine) as s:
        s.add(wf)
        s.commit()
        s.refresh(wf)
        return wf.id


# --- 1. engine:<id> accepted + wired, returns drafted text -------------------
def check_engine_id_accepted():
    cap = {}
    clean_prompt = (
        "You are a strict in-country reviewer. Fix tone in {text} for "
        "{target_locale}. Honor {glossary}. Output only the result."
    )
    ref = _engine("LocalLLM", chat_transport(clean_prompt, cap))
    # The override resolves through the standard seam, exactly like a step provider.
    assert isinstance(get_provider(ref), OpenAICompatibleProvider)
    assert provider_is_live(ref) is True
    draft = orchestrator.author_prompt("a strict in-country reviewer for Quebec", ref)
    assert draft == clean_prompt, (
        "engine:<id> authoring must return the model's drafted prompt",
        draft,
    )
    # Proof it was actually wired to the engine (prompt-based), not a bare default:
    assert cap["path"] == "/v1/chat/completions" and cap["auth"] == "Bearer sk-local"
    roles = [m["role"] for m in cap["body"]["messages"]]
    assert roles[-1] == "user"
    assert "strict in-country reviewer for Quebec" in cap["body"]["messages"][-1]["content"], (
        "the step description must reach the engine's prompt"
    )
    print(
        "  1. author_prompt accepts engine:<id> (local openai_compatible), returns "
        "the drafted prompt via get_provider"
    )


# --- 2. deviating (fenced + preambled) response still usable ------------------
def check_deviating_shape_cleaned():
    body = (
        "You are a technical reviewer. Correct {text} from {source_locale} to "
        "{target_locale}. Preserve placeholders. Output only the result."
    )
    deviating = "Sure, here is the prompt:\n```text\n" + body + "\n```"
    ref = _engine("WeakLocal", chat_transport(deviating))
    draft = orchestrator.author_prompt("a technical reviewer", ref)
    assert "```" not in draft, (
        "markdown code fence must be stripped from a weak model's draft",
        draft,
    )
    assert not draft.lower().startswith("sure"), ("conversational preamble must be stripped", draft)
    assert draft == body, (
        "the inner prompt body (placeholders intact) must survive verbatim",
        draft,
    )
    # A draft is NEVER empty even if the whole thing looked like a preamble line.
    ref2 = _engine("Emptyish", chat_transport("Here is your prompt:"))
    d2 = orchestrator.author_prompt("x", ref2)
    assert d2.strip(), "cleaning must never blank the draft (raw fallback)"
    print(
        "  2. deviating response (fenced + preamble) is cleaned to usable text; "
        "never blanks the box"
    )


# --- 3. hard provider error surfaces clean on web + CLI ----------------------
def check_hard_error_surfaces_clean():
    ref = _engine("Dead401", chat_transport("", status=401))
    wf_id = _new_workflow("Author err wf")

    # service seam -> ProviderCallError (typed), not a bare RuntimeError.
    raised = False
    try:
        orchestrator.author_prompt("a reviewer", ref)
    except ProviderCallError:
        raised = True
    assert raised, "a hard provider failure must raise ProviderCallError, not escape raw"

    client = TestClient(app)
    # web JSON route: a clean 502 + JSON error body, never a 500/traceback.
    r = client.post(
        f"/workflows/{wf_id}/author.json", data={"description": "a reviewer", "provider": ref}
    )
    assert r.status_code == 502, (
        "JSON author route must surface a clean 5xx, not 500",
        r.status_code,
    )
    j = r.json()
    assert j["ok"] is False and j.get("error"), ("JSON error body required", j)
    # web full-page fallback: a clean error page, not a 500.
    r2 = client.post(
        f"/workflows/{wf_id}/author", data={"description": "a reviewer", "provider": ref}
    )
    assert r2.status_code == 502, (
        "HTML author route must surface a clean error, not 500",
        r2.status_code,
    )
    # CLI: non-zero exit, no traceback.
    rc = cli_main(["workflow", "author-prompt", str(wf_id), "a reviewer", "--provider", ref])
    assert rc != 0, ("CLI author-prompt with a failing engine must exit non-zero", rc)
    print(
        "  3. hard provider error (HTTP 401) surfaces clean: web JSON 502 + CLI "
        "non-zero exit, no 500/traceback"
    )


# --- 4. JSON route returns draft + is stateless ------------------------------
def check_json_route_stateless():
    wf_id = _new_workflow("Author json wf")
    client = TestClient(app)

    def step_count():
        with Session(db_engine) as s:
            return len(s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).all())

    before = step_count()
    # default provider (offline MockProvider) -> a real, placeholder-bearing draft.
    r = client.post(
        f"/workflows/{wf_id}/author.json", data={"description": "a faithful translator"}
    )
    assert r.status_code == 200, r.status_code
    j = r.json()
    assert j["ok"] is True and "{text}" in j["draft"], ("draft with placeholders", j)
    after = step_count()
    assert before == after == 0, (
        "author.json must persist NOTHING server-side (stateless)",
        before,
        after,
    )
    print("  4. author.json returns the draft and persists nothing (stateless)")


# --- 5. _clean_draft: fence-gated preamble strip, direct regex regression ----
def check_clean_draft_fence_gated_strip():
    """Adversarial-review regression (FIX 1). Two reviewers found the preamble
    strip could eat REAL content: (1) content words prompt/draft/response/output
    as line-starters false-positive on legitimate first lines that happen to end
    in ':'; (2) even with those words dropped, stripping any colon-terminated
    first line is unsafe when real content directly follows with NO fence to
    prove the split is safe. The fix: only strip when doing so leaves nothing but
    a SINGLE fenced block; otherwise leave the text completely untouched."""
    # --- (a) PRESERVED intact: no fence present -> untouched, whole-text, even
    # though the first line is colon-terminated and/or looks like a chat opener.
    no_fence_preserved = [
        "Output the translation of the text below:\n{text}",
        "Draft a faithful localization:\nTranslate {text} to {target_locale}.",
        "Response should preserve all tags:\nYou are a translator...",
        "Output only the translated text:\nTranslate faithfully.",
        "Response format:\nReturn the reviewed text only.",
        "Here is the context:\n{text}",
    ]
    for original in no_fence_preserved:
        cleaned = _clean_draft(original)
        assert cleaned == original, (
            "no-fence text must be PRESERVED byte-identical (nothing proves a "
            "strip is safe without a fence)",
            original,
            cleaned,
        )
    # --- (b) fence-gated strip STILL works: preamble line + a SINGLE fenced
    # block -> unwrapped to the inner body, preamble dropped along with it.
    fenced = "Sure! Here's a prompt:\n```\nTranslate {text}.\n```"
    assert _clean_draft(fenced) == "Translate {text}.", (
        "a genuine preamble + single fence must still unwrap to the fence body",
        _clean_draft(fenced),
    )
    fenced2 = "Certainly, here is the draft:\n```\nTranslate {text} faithfully.\n```"
    assert _clean_draft(fenced2) == "Translate {text} faithfully.", (
        "a second genuine preamble + single fence must also unwrap",
        _clean_draft(fenced2),
    )
    # --- (c) multiple top-level fences -> left ALONE (no orphan-fence
    # Frankenstein result from unwrapping only the first block).
    multi_fence = "```py\nprint(1)\n```\n```py\nprint(2)\n```"
    assert _clean_draft(multi_fence) == multi_fence, (
        "multiple fenced blocks must be left completely untouched",
        _clean_draft(multi_fence),
    )
    print(
        "  5. _clean_draft: 6 no-fence lines preserved byte-identical, 2 "
        "fence-gated preambles still strip to the fence body, multi-fence "
        "input left untouched"
    )


# --- 6. no-JS /author fallback renders the FULL builder page -----------------
def check_nojs_author_renders_full_page():
    """Adversarial-review regression (visual-refresh wave 2): the no-JS
    POST /workflows/{id}/author fallback rendered workflow_builder.html with a
    partial context (no locales/label_for/group/glossary_options). Jinja's
    default lenient Undefined iterates a missing name as EMPTY, so the page
    silently rendered with an empty copy-to-pairs language picker and empty
    source-locale select (~14KB of markup gone, no error anywhere) — a
    degraded page defeating the fallback's purpose. Page-wide markers can't
    catch this (the global new-project dialog renders its own langpicker with
    the same field name), so assert INSIDE the copy-to-pairs form."""
    wf_id = _new_workflow("Author nojs wf")
    client = TestClient(app)
    r = client.post(f"/workflows/{wf_id}/author", data={"description": "a faithful translator"})
    assert r.status_code == 200, ("no-JS author fallback must render, not 500", r.status_code)
    page = r.text
    assert "{text}" in page, "draft must be pre-filled in the rendered page"
    start = page.index("/copy-to-pairs")
    form = page[start : page.index("</form>", start)]
    assert 'value="fr-FR"' in form and "data-langpicker" in form, (
        "copy-to-pairs picker must contain real locale checkboxes: an empty "
        "picker means the author fallback rendered without locales/label_for "
        "context (lenient Undefined iterates empty — silent degradation)"
    )
    print(
        "  6. no-JS /author fallback renders the FULL builder page (200, draft "
        "pre-filled, copy-to-pairs picker populated)"
    )


def main() -> None:
    check_engine_id_accepted()
    check_deviating_shape_cleaned()
    check_hard_error_surfaces_clean()
    check_json_route_stateless()
    check_clean_draft_fence_gated_strip()
    check_nojs_author_renders_full_page()
    print("PROMPT AUTHORING OK")


if __name__ == "__main__":
    main()
