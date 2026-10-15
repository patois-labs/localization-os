"""Language-agnostic DRAFT verification (SPEC-language-agnostic-workflows T1).

A workflow drafted through Design a workflow carries NO language pair of its own: it
takes its languages from the project it runs on. This hermetic suite proves the bar:

  1. A drafted workflow (mock authoring engine -> apply_workflow_draft) is created with
     source_locale / target_locale / group_id all NULL, and its step prompt keeps the
     {target_locale} / {source_locale} placeholders (no hard-coded language).
  2. The SAME workflow, run on two projects with different languages
     (en-US -> de-DE and fr-FR -> ja-JP), renders a prompt on each run that carries THAT
     project's languages -- captured at the provider seam.
  3. prompts.locale_warning warns when an llm_agent prompt names a language instead of
     {target_locale}, stays silent when it uses {target_locale}, and never raises.

Offline: throwaway LOC_TMS_DATA_DIR + cleared keys; the authoring call runs on a
registered OpenAI-compatible mock transport; the run's translate step uses a registered
recorder provider that captures the rendered prompt it is asked to run. ASCII-only.
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_langdraft_")

import httpx
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Role, StepType, Workflow, WorkflowStep
from app.services import apikeys, orchestrator, prompts, security, setup_author
from app.services.engines import create_profile, get_profile
from app.services.providers import (
    OpenAICompatibleProvider,
    Provider,
    register_provider,
)

init_db()

_API_TOKEN: str | None = None


def _api_token() -> str:
    """A docs-scoped admin API key for the /api authoring calls. The /api front door
    requires a user API key (docs scope) regardless of the login wall, so these calls
    carry one even though the rest of this suite runs auth-off."""
    global _API_TOKEN
    if _API_TOKEN is None:
        user = security.create_user(
            "langdraft-admin@x.com", "LangDraft Admin", Role.ADMIN, "langdraft-password-123"
        )
        _row, raw = apikeys.create_key(user.id, "langdraft", scopes="api docs")
        _API_TOKEN = raw
    return _API_TOKEN


class Recorder(Provider):
    """A live translate provider that records the rendered prompt it is asked to run."""

    name = "recorder"

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.calls.append(prompt or "")
        return "TRANSLATED"


RECORDER = Recorder()
register_provider("recorder", RECORDER)


def _json_transport(responses, counter):
    def h(req):
        i = counter["n"]
        counter["n"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": responses[min(i, len(responses) - 1)]},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 8},
            },
        )

    return httpx.MockTransport(h)


_ENGINE_SEQ = {"n": 0}


def _make_author_engine(draft_json):
    _ENGINE_SEQ["n"] += 1
    p = create_profile(
        f"LangDraftEng{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",  # pregate: allow-secret offline mock engine
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(p.id), transport=_json_transport([draft_json], {"n": 0})
        ),
    )
    return ref


def _session():
    return Session(engine)


def _new_project(client, name, source, target):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": source,
            "target_locales": target,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _docx(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def _upload(client, pid, paragraphs):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                _docx(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _draft_and_apply():
    """Design a workflow (mock engine) then confirm it; return the new workflow id.

    The confirmed step keeps its {target_locale}/{source_locale} placeholders; its
    provider is then repointed at the recorder so a later run's rendered prompt is
    captured at the provider seam."""
    draft_json = (
        '{"name": "Lang Agnostic Translate", "steps": ['
        '{"name": "Translate", "step_type": "llm_agent", '
        '"prompt_text": "Translate from {source_locale} into {target_locale}: {text}"}]}'
    )
    ref = _make_author_engine(draft_json)
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="translate content", provider=ref, org_id=None, actor="tester"
        )
        fields = out["fields"]
        wf = setup_author.apply_workflow_draft(s, fields, org_id=None, actor="tester")
        wf_id = wf.id
        # (1) the drafted workflow carries NO language pair and is NOT in a group.
        assert wf.source_locale is None and wf.target_locale is None, (
            wf.source_locale,
            wf.target_locale,
        )
        assert wf.group_id is None, "a drafted workflow is never in a group"
        step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).first()
        assert "{target_locale}" in step.prompt_text, step.prompt_text
        # Repoint the run at the recorder so the rendered prompt is captured.
        step.provider = "recorder"
        s.add(step)
        s.commit()
    return wf_id


def test_drafted_workflow_has_no_pair_and_keeps_placeholders():
    wf_id = _draft_and_apply()
    assert wf_id, "the drafted workflow must be created"
    print(
        "  (1) drafted workflow: source/target/group NULL, step prompt keeps "
        "{target_locale}/{source_locale}"
    )


def test_run_carries_each_projects_languages():
    client = TestClient(app)
    wf_id = _draft_and_apply()

    # Project A: en-US -> de-DE.
    pid_a = _new_project(client, "LangDraft A", "en-US", "de-DE")
    doc_a = _upload(client, pid_a, ["Hello there."])
    RECORDER.calls.clear()
    with Session(engine) as s:
        sm_a = orchestrator.run_workflow(s, wf_id, doc_a, "de-DE")
    assert sm_a["status"] == "completed", sm_a
    prompt_a = " ".join(RECORDER.calls)
    assert "en-US" in prompt_a and "de-DE" in prompt_a, prompt_a
    assert "ja-JP" not in prompt_a and "fr-FR" not in prompt_a, prompt_a

    # Project B: fr-FR -> ja-JP -- the same workflow, different project languages.
    pid_b = _new_project(client, "LangDraft B", "fr-FR", "ja-JP")
    doc_b = _upload(client, pid_b, ["Bonjour."])
    RECORDER.calls.clear()
    with Session(engine) as s:
        sm_b = orchestrator.run_workflow(s, wf_id, doc_b, "ja-JP")
    assert sm_b["status"] == "completed", sm_b
    prompt_b = " ".join(RECORDER.calls)
    assert "fr-FR" in prompt_b and "ja-JP" in prompt_b, prompt_b
    assert "de-DE" not in prompt_b and "en-US" not in prompt_b, prompt_b
    print(
        "  (2) one drafted workflow on two projects: each run's rendered prompt "
        "carries that project's languages"
    )


def test_locale_warning_never_blocks():
    # Names a language in prose with no placeholder -> warns, suggesting {target_locale}.
    w = prompts.locale_warning(StepType.LLM_AGENT, "Translate into Canadian French: {text}")
    assert w and "{target_locale}" in w, w
    # Uses the placeholder -> silent (naming a language alongside it is fine).
    assert (
        prompts.locale_warning(StepType.LLM_AGENT, "Translate into {target_locale}: {text}") == ""
    )
    # Never raises: blank, None prompt, a non-llm_agent step, or an unknown step type.
    for st, pt in (
        (StepType.LLM_AGENT, ""),
        (StepType.LLM_AGENT, None),
        (StepType.LQA, "Translate into Canadian French: {text}"),
        ("not-a-step-type", "Translate into Canadian French: {text}"),
        (None, None),
    ):
        assert isinstance(prompts.locale_warning(st, pt), str), (st, pt)
    print(
        "  (3) locale_warning: warns for a prose language (suggests {target_locale}), "
        "stays silent for {target_locale}, never raises"
    )


# --- shared detector (named_language) + its two surfaces ---------------------
# The T1 nudge fires only on a language-use phrase ("into French", "into es-419"), not on
# an ordinary catalog word ("Polish", "Turkish Delight", "Dutch courage"). These positives
# and negatives are checked on BOTH surfaces the detector drives: the llm_agent prompt
# warning (prompts.locale_warning) and the draft's description note
# (setup_author.author_workflow_setup).
_NAMED_POS = [
    "Translate into Canadian French: {text}",
    "Translate the following into French: {text}",
    "translate into es-419: {text}",
    "Translate into fr-CA: {text}",
]
_NAMED_NEG = [
    "Polish the text: {text}",
    "translate then polish the copy",
    "Turkish Delight product names: {text}",
    "Dutch courage",
    "Translate into {target_locale}: {text}",
    # N1: a hyphenated ENGLISH word after a language-use preposition reads as an xx-yy
    # locale code to the regex, but it is not a catalog locale -> not a named language.
    "Use facts from up-to-date sources: {text}",
    "Keep it in end-to-end tests",
    "Switch into re-do mode",
    "Mention in on-site support",
]
_LANG_NOTE = "Languages come from the project you run this workflow on"
_API_DRAFT_JSON = (
    '{"name": "API Lang Agnostic", "steps": ['
    '{"name": "Translate", "step_type": "llm_agent", '
    '"prompt_text": "Translate from {source_locale} into {target_locale}: {text}"}]}'
)
_NAMED_DRAFT_JSON = (
    '{"name": "API Names French", "steps": ['
    '{"name": "Translate", "step_type": "llm_agent", '
    '"prompt_text": "Translate into French: {text}"}]}'
)


def test_named_language_detector():
    for p in _NAMED_POS:
        assert prompts.named_language(p), p
        w = prompts.locale_warning(StepType.LLM_AGENT, p)
        assert "names a language" in w and "{target_locale}" in w, (p, w)
    for p in _NAMED_NEG:
        assert prompts.named_language(p) is None, p
        # The SHARP detector never claims a SPECIFIC language for these ordinary words, so
        # the warning must not quote a name ("names a language (French)"). F-B's broad
        # names_a_language fallback may still add the GENERIC "use {target_locale}" nudge
        # (no name), which is acceptable; what must never happen is a false specific name.
        assert "names a language (" not in prompts.locale_warning(StepType.LLM_AGENT, p), p
    # ENGLISH RULE: English after to/into is an explicit target (counts); English after
    # in/from is the source-is-English idiom (does not count).
    assert prompts.named_language("Translate into English: {text}") == "English"
    assert prompts.named_language("Written in English: {text}") is None
    print(
        "  (4) named_language: fires on a language-use phrase (prompt warning names it + "
        "suggests {target_locale}), silent on ordinary words and on {target_locale}"
    )


# N1 negatives: a hyphenated English word after in/to/from/into matches the xx-yy code
# shape, so it used to be read as a locale ("up-to", "end-to", "re-do", "on-site"). It is
# not a catalog locale, so it must read as NO language on BOTH surfaces (named_language and
# the draft's description note). N2: the warning quotes only the matched language, not a
# capitalised word that trails it ("Welsh Rarebit" -> "Welsh") while a real modifier the
# author attached stays ("Canadian French" is kept whole).
_N1_HYPHENATED = [
    "Use facts from up-to-date sources: {text}",
    "Keep it in end-to-end tests",
    "Switch into re-do mode",
    "Mention in on-site support",
]


def test_hyphenated_english_is_not_a_locale():
    for p in _N1_HYPHENATED:
        assert prompts.named_language(p) is None, p
        assert "names a language" not in prompts.locale_warning(StepType.LLM_AGENT, p), p
    # A later real code in the same text is still found once the fake code is skipped.
    assert prompts.named_language("Keep it in end-to-end and translate into fr-CA") == "fr-CA"
    # fr-CA positive (a real catalog locale): named_language returns the code and the
    # prompt warning names it.
    assert prompts.named_language("into fr-CA") == "fr-CA"
    w = prompts.locale_warning(StepType.LLM_AGENT, "Translate into fr-CA: {text}")
    assert "names a language" in w and "fr-CA" in w and "{target_locale}" in w, w
    # The description note must also stay silent for the hyphenated words and fire for fr-CA.
    ref = _make_author_engine(_API_DRAFT_JSON)
    with _session() as s:
        for d in _N1_HYPHENATED:
            out = setup_author.author_workflow_setup(
                s, description=d, provider=ref, org_id=None, actor="tester"
            )
            assert not any(_LANG_NOTE in w for w in out["warnings"]), (d, out["warnings"])
        out = setup_author.author_workflow_setup(
            s, description="Translate into fr-CA: {text}", provider=ref, org_id=None, actor="tester"
        )
        assert any(_LANG_NOTE in w for w in out["warnings"]), out["warnings"]
    print(
        "  (4b) N1: a hyphenated English word is not a locale (both surfaces silent); a "
        "real code (fr-CA) later in the same text is still found"
    )


def test_warning_quotes_only_the_language():
    # N2: name the matched language, not a trailing capitalised word. "Canadian French"
    # keeps its modifier (the matched name "French" is the run's last word, nothing trails
    # it), while "Welsh Rarebit" drops the trailing non-language "Rarebit".
    w = prompts.locale_warning(StepType.LLM_AGENT, "Translate into Canadian French please: {text}")
    assert "names a language (Canadian French)" in w and "{target_locale}" in w, w
    assert prompts.named_language("Translate into Canadian French please: {text}") == (
        "Canadian French"
    )
    w = prompts.locale_warning(StepType.LLM_AGENT, "Translate into Welsh Rarebit: {text}")
    assert "names a language (Welsh)" in w and "Welsh Rarebit" not in w, w
    assert prompts.named_language("Translate into Welsh Rarebit: {text}") == "Welsh"
    print(
        "  (4c) N2: the warning names only the language (Welsh, Canadian French), not a "
        "trailing word"
    )


def test_description_note_follows_detector():
    ref = _make_author_engine(_API_DRAFT_JSON)
    with _session() as s:
        for d in _NAMED_POS:
            out = setup_author.author_workflow_setup(
                s, description=d, provider=ref, org_id=None, actor="tester"
            )
            assert any(_LANG_NOTE in w for w in out["warnings"]), (d, out["warnings"])
        for d in _NAMED_NEG:
            out = setup_author.author_workflow_setup(
                s, description=d, provider=ref, org_id=None, actor="tester"
            )
            assert not any(_LANG_NOTE in w for w in out["warnings"]), (d, out["warnings"])
    print(
        "  (5) the draft's description note fires for a named language in the description, "
        "not for ordinary words"
    )


def test_api_author_old_client_gets_ignore_note_and_nulls():
    ref = _make_author_engine(_API_DRAFT_JSON)
    client = TestClient(app)
    with _session() as s:
        before = len(s.exec(select(Workflow)).all())
    r = client.post(
        "/api/workflows/author",
        headers={"X-Loc-Token": _api_token()},
        json={
            "description": "translate content",
            "provider": ref,
            "create": True,
            "source_locale": "en-US",
            "target_locales": ["de-DE", "fr-FR"],
            "adapt": True,
        },
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("created") is True, body
    joined = " | ".join(body.get("warnings") or [])
    assert "are ignored" in joined, body
    with _session() as s:
        assert len(s.exec(select(Workflow)).all()) == before + 1, "exactly one workflow"
        wf = s.get(Workflow, body["id"])
        assert wf.source_locale is None and wf.target_locale is None and wf.group_id is None, (
            wf.source_locale,
            wf.target_locale,
            wf.group_id,
        )
    print(
        "  (6) old API client (source_locale/target_locales/adapt): 200, ignore note, one "
        "workflow with source_locale/target_locale/group_id all NULL"
    )


def test_api_author_clean_client_gets_no_ignore_note():
    ref = _make_author_engine(_API_DRAFT_JSON)
    client = TestClient(app)
    r = client.post(
        "/api/workflows/author",
        headers={"X-Loc-Token": _api_token()},
        json={
            "description": "translate content",
            "provider": ref,
            "create": True,
            "adapt": False,
        },
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("created") is True, body
    joined = " | ".join(body.get("warnings") or [])
    assert "are ignored" not in joined, body
    print("  (7) clean API client (adapt:false, no locales): 200 with NO ignore note")


def test_api_author_named_language_step_warns_in_apply():
    ref = _make_author_engine(_NAMED_DRAFT_JSON)
    client = TestClient(app)
    r = client.post(
        "/api/workflows/author",
        headers={"X-Loc-Token": _api_token()},
        json={"description": "translate content", "provider": ref, "create": True},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("created") is True, body
    joined = " | ".join(body.get("warnings") or [])
    assert "names a language" in joined and "French" in joined and "{target_locale}" in joined, body
    print(
        "  (8) a drafted step prompt naming a language -> the apply warnings list carries "
        "the names-a-language warning"
    )


def test_multiword_language_names_never_raise():
    # F-A: three catalog names are MULTI-word ("Central Kurdish", "Norwegian Bokmål",
    # "Norwegian Nynorsk"). named_language used to crash (list.index over single words)
    # when one matched, taking down every step-prompt surface. Each must now be quoted
    # back whole, and the description note must not raise when a description names one.
    assert prompts.named_language("Translate into Norwegian Nynorsk: {text}") == (
        "Norwegian Nynorsk"
    )
    assert prompts.named_language("Translate into Central Kurdish: {text}") == "Central Kurdish"
    assert prompts.named_language("to Norwegian Bokmål") == "Norwegian Bokmål"
    # The warning surfaces the whole name, never a partial first word.
    w = prompts.locale_warning(StepType.LLM_AGENT, "Translate into Norwegian Nynorsk: {text}")
    assert "names a language (Norwegian Nynorsk)" in w and "{target_locale}" in w, w
    # The draft's description note (author_workflow_setup) names it and does not raise.
    ref = _make_author_engine(_API_DRAFT_JSON)
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s,
            description="Translate into Norwegian Nynorsk: {text}",
            provider=ref,
            org_id=None,
            actor="tester",
        )
        assert any(_LANG_NOTE in w for w in out["warnings"]), out["warnings"]
    print("  (9) F-A: a multi-word language name is quoted whole; the note does not raise")


def test_every_catalog_name_is_named_without_raising():
    # F-A regression that would have caught the crash: walk EVERY catalog name (multi-word
    # included) as a title-case "Translate into <Name>: {text}" prompt and prove
    # named_language never raises and returns a non-empty string for each one.
    names = prompts._language_name_set()
    assert names, "the catalog must be loaded for this check to mean anything"
    for name in sorted(names):
        text = f"Translate into {name.title()}: {{text}}"
        got = prompts.named_language(text)
        assert isinstance(got, str) and got.strip(), (name, text, got)
    print(f"  (10) F-A: all {len(names)} catalog names name without raising")


def test_builder_addstep_multiword_prompt_redirects():
    # F-A through the real builder route: an add-step POST whose prompt names a multi-word
    # language returned 500 (the ValueError escaped validate_step_prompt). It must now be a
    # plain 303 redirect, with the names-a-language notice (not an error).
    client = TestClient(app)
    with _session() as s:
        wf = Workflow(
            name="LangDraft-Nynorsk",
            scope="global",
            source_locale="en-US",
            target_locale="de-DE",
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
    r = client.post(
        f"/workflows/{wid}/steps",
        data={
            "step_type": StepType.LLM_AGENT,
            "name": "T",
            "provider": "mock",
            "prompt_text": "Translate into Norwegian Nynorsk: {text}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:400])
    print("  (11) F-A: builder add-step with a multi-word language name redirects 303, not 500")


def test_no_target_warning_only_when_no_language():
    # F-B + main parity: the old no-target warning fires ONLY when no catalog language is
    # named anywhere (exactly as on main). A prompt that names a language WITHOUT a
    # {target_locale} placeholder gets either the T1 nudge (a language-use phrase) or "" (an
    # ordinary word like "polish"; main returned "" too) -- never the no-target text, and
    # never a NEW generic warning on ordinary English.
    from app.services.prompts import NO_TARGET_LANGUAGE_WARNING

    # Names a language in a language-use phrase -> the T1 nudge, quoting the language.
    w = prompts.locale_warning(StepType.LLM_AGENT, "Translate into French: {text}")
    assert "names a language (French)" in w and "{target_locale}" in w, w
    # Names a catalog word but not in a language-use phrase -> "" exactly as on main.
    for p in (
        "translate into french: {text}",
        "Produce the French version of: {text}",
        "French translation of: {text}",
        "In Japanese, translate: {text}",
        "Polish the text: {text}",
        "translate then polish the copy: {text}",
        "Turkish Delight names: {text}",
        "Make it in English: {text}",
        "Translate into {target_locale}: {text}",
    ):
        got = prompts.locale_warning(StepType.LLM_AGENT, p)
        assert got == "", (p, got)
    # A prompt that names NO language at all keeps the exact no-target text from main.
    assert (
        prompts.locale_warning(StepType.LLM_AGENT, "Rewrite this: {text}")
        == NO_TARGET_LANGUAGE_WARNING
    )
    print(
        "  (12) F-B: no-target text only when no language is named; ordinary words "
        "('Polish the text') keep main's empty result"
    )


def main() -> None:
    test_drafted_workflow_has_no_pair_and_keeps_placeholders()
    test_run_carries_each_projects_languages()
    test_locale_warning_never_blocks()
    test_named_language_detector()
    test_hyphenated_english_is_not_a_locale()
    test_warning_quotes_only_the_language()
    test_description_note_follows_detector()
    test_multiword_language_names_never_raise()
    test_every_catalog_name_is_named_without_raising()
    test_builder_addstep_multiword_prompt_redirects()
    test_no_target_warning_only_when_no_language()
    test_api_author_old_client_gets_ignore_note_and_nulls()
    test_api_author_clean_client_gets_no_ignore_note()
    test_api_author_named_language_step_warns_in_apply()
    print("LANGUAGE-AGNOSTIC DRAFT OK")


if __name__ == "__main__":
    main()
