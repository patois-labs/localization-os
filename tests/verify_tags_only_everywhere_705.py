"""A segment with no translatable text is copied on EVERY path, never sent to an engine (#705).

Owner decision (2026-10-11): "Workflow runs that come across tag only segments should
copy the tag to the target and deliver. This should be an across the board thing."

#702 made the plain MT job copy a tags-only segment. Workflow runs still sent it to every step
(the soak's sdlxliff/3: 1,159 tags, ~20 minutes and ~40,000 tokens PER LLM step for the source
back). The ONE predicate is `app/services/tags_only.py::is_tags_only` (inline `⟦..⟧` tags and
placeholders such as `{name}`, `%s`, `%1$d`, `{{count}}` with only whitespace around them) and
the ONE action is `tags_only.commit_copy`.

  workflow   an SDLXLIFF with two tags-only units, a placeholder-only unit and a text unit
             through a TWO-step llm_agent workflow with a counting fake engine: the engine
             only ever sees the text unit (2 calls, not 8), the copies equal their sources
             (status MT, not held, no step_error / gate-hold badge, not TM), the run records
             `tags_only` = 3, both StepRuns complete and the step-0 output_chars counts the
             copies as delivered while step 1 does not
  gatehold   an LQA gate that FAILS with no correction loop holds the produced text but
             never the tags-only copy (it was not produced or judged)
  keys       a keys project (the `keys translate` launcher runs this same workflow kernel on
             the namespace document): a `{name}` value is copied; `Hello {name}` and the format
             string `%1$s %2$s` (two placeholders: their order can be the translation) reach
             the engine
  step       `orchestrator._run_step_on_segment`
             answers a tags-only segment with its input, zero spend, no call
  text       `translate.translate_text_once` (the /api/translate/text + MCP translate_text
             seam) returns a tags-only text unchanged without a call
  alternates the editor's alternates route returns the copy without a call
  qa         the LQA identical-to-source finding on a tags-only copy is advisory (like a
             numeral-only one, #345); on a text segment it still fails the gate
  helper     the predicate: tags / placeholders + whitespace only -> True; any word, digit,
             punctuation, raw markup, ICU plural or `%%` -> False; linear on hostile input

Every engine is a local fake (no network, no spend). Final line: "TAGS ONLY EVERYWHERE 705 OK".
"""

import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tagsonly705_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import (
    ProjectType,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    content,
    orchestrator,
    providers_hub,
    quality,
    tags_only,
    translate,
)
from app.services import keys as keys_svc
from app.services import providers as P
from app.services.providers import Provider, ProviderSpec, register_provider

init_db()
_client = TestClient(app)
_N = [0]
# What an engine text looks like once every tag, sentinel and placeholder is removed.
_STRIP_RE = re.compile(
    r"«TAG[0-9a-f]+_\d+»|«[A-Z]+_[^»\s]*»|⟦/?[a-z]?\d+⟧|\{[A-Za-z0-9_]+\}|%(?:\d+\$)?[sd]"
)


def _fail(msg):
    print("FAIL:", str(msg).encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


def _no_text(t: str) -> bool:
    """A NON-EMPTY engine text with nothing but tags / placeholders (an LQA judge call
    carries its document in the prompt, not the payload text, so "" is not a unit)."""
    return bool((t or "").strip()) and not _STRIP_RE.sub("", t or "").strip()


class _CountingEngine(Provider):
    """Records every text it is asked to work on; answers 'Bonjour ' + the text."""

    name = "countfake705"

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        text = (payload or {}).get("text", "")
        self.calls.append(text)
        self.last_usage = {"input_tokens": 10, "output_tokens": 10}
        return "Bonjour " + text


def _install(eng):
    names = sorted({"mock", (config.DEFAULT_PROVIDER or "mock").lower()})
    saved = {n: providers_hub._OVERRIDES.get(n) for n in names}
    for n in names:
        register_provider(n, eng)
    return saved


def _restore(saved):
    for n, prev in saved.items():
        if prev is None:
            providers_hub._OVERRIDES.pop(n, None)
        else:
            providers_hub._OVERRIDES[n] = prev


def _upload(name, data: bytes) -> int:
    _N[0] += 1
    r = _client.post(
        "/projects",
        data={
            "name": f"tagsonly705-{_N[0]}",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = _client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name, data, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def _xliff() -> bytes:
    short = '<x id="1"/>\n\t\t<x id="2"/>'
    long_ = "\n\t\t".join(f'<x id="{i}"/>' for i in range(3, 400))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff sdl:version="1.0" version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" '
        'xmlns:sdl="http://sdl.com/FileTypes/SdlXliff/1.0">\n'
        '<file original="t" source-language="en-US" target-language="fr-FR" '
        'datatype="x-sdlfilterframework2">'
        "<body>\n"
        f'<trans-unit id="a"><source>{short}</source></trans-unit>\n'
        '<trans-unit id="b"><source>Save the <x id="9000"/> file.</source></trans-unit>\n'
        f'<trans-unit id="c"><source>{long_}</source></trans-unit>\n'
        '<trans-unit id="d"><source>{count}</source></trans-unit>\n'
        "</body></file></xliff>\n"
    ).encode()


def _segs(doc_id, locale="fr-FR"):
    with Session(engine) as s:
        return s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == locale)
            .order_by(Segment.order_index)
        ).all()


def _workflow(steps):
    with Session(engine) as s:
        _N[0] += 1
        wf = Workflow(name=f"tagsonly705-wf-{_N[0]}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, spec in enumerate(steps):
            st = WorkflowStep(workflow_id=wf.id, order_index=i, **spec)
            s.add(st)
        s.commit()
        return wf.id


_LLM = {
    "step_type": StepType.LLM_AGENT,
    "provider": "mock",
    "prompt_text": "Translate into {target_locale}: {text}",
    "retry_max": 0,
    "backoff_base": 0.0,
}


def _last_run(doc_id):
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == doc_id)
            .order_by(WorkflowRun.id.desc())
        ).first()
        steps = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run.id).order_by(StepRun.id)
        ).all()
        return run, steps


def _has_flag(seg, ftype):
    return any(isinstance(f, dict) and f.get("type") == ftype for f in (seg.qa_flags or []))


def test_workflow():
    doc_id = _upload("tags705.sdlxliff", _xliff())
    segs = _segs(doc_id)
    copies = [g for g in segs if tags_only.is_tags_only(g.source_text)]
    texts = [g for g in segs if not tags_only.is_tags_only(g.source_text)]
    if len(copies) != 3 or len(texts) != 1:
        _fail(
            f"[workflow] want 3 no-text units + 1 text unit: {[g.source_text[:30] for g in segs]}"
        )
    # A stale step_error badge from an earlier run must not survive the copy.
    with Session(engine) as s:
        g = s.get(Segment, copies[0].id)
        g.qa_flags = quality.merge_step_error_flags(
            g.qa_flags, [quality.step_error_flag("Old step", "old failure")]
        )
        s.add(g)
        s.commit()
    wf_id = _workflow([{**_LLM, "name": "Translate"}, {**_LLM, "name": "Polish"}])
    eng = _CountingEngine()
    saved = _install(eng)
    try:
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf_id, doc_id, "fr-FR")
    finally:
        _restore(saved)
    for c in eng.calls:
        if _no_text(c):
            _fail(f"[workflow] an LLM step was sent a no-text segment ({len(c)} chars): {c[:60]!r}")
    if len(eng.calls) != 2:
        _fail(f"[workflow] only the text unit goes to each step: {[c[:40] for c in eng.calls]}")
    run, steps = _last_run(doc_id)
    if run.status != RunStatus.COMPLETED:
        _fail(f"[workflow] the run must complete: {run.status} {run.notes!r}")
    if (run.context or {}).get("tags_only") != 3:
        _fail(f"[workflow] the run must count 3 copies: {(run.context or {}).get('tags_only')!r}")
    if len(steps) != 2 or any(sr.status != RunStatus.COMPLETED for sr in steps):
        _fail(f"[workflow] both StepRuns complete: {[(sr.status, sr.error) for sr in steps]}")
    by_id = {g.id: g for g in _segs(doc_id)}
    copy_chars = sum(len(g.source_text) for g in copies)
    if (steps[0].output_chars or 0) < copy_chars:
        _fail(f"[workflow] step 0 delivers the copies: {steps[0].output_chars} < {copy_chars}")
    if (steps[1].output_chars or 0) >= copy_chars:
        _fail(f"[workflow] step 1 did not touch the copies: output_chars {steps[1].output_chars}")
    for g in copies:
        g = by_id[g.id]
        if g.target_text != g.source_text or g.status != SegStatus.MT:
            _fail(f"[workflow] copy as MT: {g.status} {g.target_text!r:.60}")
        if _has_flag(g, quality.STEP_ERROR_FLAG_TYPE) or quality.has_gate_hold_flag(g.qa_flags):
            _fail(f"[workflow] a copy carries no step/gate badge: {g.qa_flags!r}")
        if (g.meta or {}).get("mt_chunks"):
            _fail(f"[workflow] a copy is not chunked: {g.meta!r}")
    t = by_id[texts[0].id]
    if not (t.target_text or "").startswith("Bonjour Bonjour Save the"):
        _fail(f"[workflow] the text unit went through both steps: {t.target_text!r}")
    print("  [workflow] 2-step run: 3 no-text units copied without a call, text unit translated OK")


def test_gate_hold_spares_copies():
    doc_id = _upload("tags705b.sdlxliff", _xliff())

    class _Echo(Provider):
        name = "echo705"

        def __init__(self):
            self.calls = []

        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            text = (payload or {}).get("text", "")
            self.calls.append(text)
            return text  # an untranslated echo: the offline LQA fails the gate

    wf_id = _workflow(
        [
            {**_LLM, "name": "Translate", "provider": "echo705"},
            {
                "name": "Check",
                "step_type": StepType.LQA,
                "key": "lqa1",
                "lqa_threshold": 90,
                "provider": "echo705",
                "branch": {"on_pass": "end", "on_fail": "end"},
            },
        ]
    )
    eng = _Echo()
    register_provider("echo705", eng)
    try:
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf_id, doc_id, "fr-FR")
    finally:
        providers_hub._OVERRIDES.pop("echo705", None)
    by = _segs(doc_id)
    text = next(g for g in by if not tags_only.is_tags_only(g.source_text))
    if not quality.has_gate_hold_flag(text.qa_flags):
        _fail(f"[gatehold] precondition: the echoed text unit must be gate-held: {text.qa_flags!r}")
    for g in by:
        if not tags_only.is_tags_only(g.source_text):
            continue
        if quality.has_gate_hold_flag(g.qa_flags) or g.status != SegStatus.MT:
            _fail(f"[gatehold] a copy is never held: {g.status} {g.qa_flags!r}")
        if g.target_text != g.source_text:
            _fail("[gatehold] the copy must be delivered")
    for c in eng.calls:
        if _no_text(c):
            _fail("[gatehold] the translate step was sent a no-text unit")
    print("  [gatehold] failed gate holds the text unit, never the tags-only copies OK")


def test_keys():
    with Session(engine) as s:
        p = content.project_create(
            s,
            "Keys705",
            source_locale="en-US",
            target_locales="de-DE",
            project_type=ProjectType.KEY_BASED,
        )
        pid = p.id
        ids = {}
        for name, base in (("greet", "Hello {name}"), ("ph", "{name}"), ("fmt", "%1$s %2$s")):
            k = keys_svc.create_key(s, s.get(type(p), pid), name, base_value=base, actor="fixture")
            keys_svc.ensure_value_rows(s, k)
            ids[name] = k.id
        s.commit()
        rows = {
            n: s.exec(
                select(Segment).where(Segment.key_id == kid, Segment.target_locale == "de-DE")
            ).first()
            for n, kid in ids.items()
        }
        doc_ids = {r.document_id for r in rows.values()}
    if len(doc_ids) != 1:
        _fail(f"[keys] expected one namespace document: {doc_ids}")
    doc_id = doc_ids.pop()
    wf_id = _workflow([{**_LLM, "name": "Translate"}])
    eng = _CountingEngine()
    saved = _install(eng)
    try:
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf_id, doc_id, "de-DE")
    finally:
        _restore(saved)
    if len(eng.calls) != 2 or not any("Hello" in c for c in eng.calls):
        _fail(
            f"[keys] the text value and the two-placeholder format reach the engine: {eng.calls!r}"
        )
    if any(c.strip() == "{name}" for c in eng.calls):
        _fail(f"[keys] a one-placeholder value is never sent: {eng.calls!r}")
    with Session(engine) as s:
        for n in ("ph",):
            g = s.get(Segment, rows[n].id)
            if g.target_text != g.source_text or g.status != SegStatus.MT:
                _fail(f"[keys] {n} must be copied as MT: {g.status} {g.target_text!r}")
        g = s.get(Segment, rows["greet"].id)
        if not (g.target_text or "").startswith("Bonjour"):
            _fail(f"[keys] the text value is translated: {g.target_text!r}")
    print("  [keys] placeholder-only values copied, the text value translated OK")


def test_step_engine_and_eval():
    eng = _CountingEngine()
    saved = _install(eng)
    try:
        step = WorkflowStep(workflow_id=0, order_index=0, name="T", **_LLM)
        res = orchestrator._run_step_on_segment(
            ProviderSpec(provider="mock"),
            _LLM["prompt_text"],
            {"source_locale": "en-US", "target_locale": "fr-FR", "context": "", "style_guide": ""},
            step,
            "⟦x1⟧ ⟦x2⟧",
            "⟦x1⟧ ⟦x2⟧",
            "fr-FR",
            [],
        )
        if eng.calls or res["output"] != "⟦x1⟧ ⟦x2⟧" or res["cost_usd"] or res["attempts"]:
            _fail(f"[step] a tags-only segment needs no call: {eng.calls!r} {res!r}")
        print("  [step] _run_step_on_segment answers a tags-only segment without a call OK")
        r = translate.translate_text_once("⟦x1⟧\n⟦x2⟧", "en-US", "fr-FR")
        if eng.calls or r.get("text") != "⟦x1⟧\n⟦x2⟧" or r.get("error"):
            _fail(f"[text] a tags-only text needs no call: {eng.calls!r} {r!r}")
        r = translate.translate_text_once("Hello", "en-US", "fr-FR")
        if len(eng.calls) != 1 or not (r.get("text") or "").startswith("Bonjour"):
            _fail(f"[text] a text still reaches the engine: {eng.calls!r} {r!r}")
        print("  [text] translate_text_once returns a tags-only text unchanged OK")
        eng.calls.clear()
        doc_id = _upload("tags705c.sdlxliff", _xliff())
        seg = next(g for g in _segs(doc_id) if tags_only.is_tags_only(g.source_text))
        r = _client.post(f"/segments/{seg.id}/alternates")
        if r.status_code != 200 or r.json().get("alternates") != [seg.source_text] or eng.calls:
            _fail(f"[alternates] copy only: {r.status_code} {r.text[:200]} {eng.calls!r}")
        print("  [alternates] tags-only segment answered with its copy, no call OK")
    finally:
        _restore(saved)


class _Rec(P.Provider):
    def __init__(self, name, out):
        self.name = name
        self._out = out
        self.calls: list[str] = []

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        self.last_usage = {"input_tokens": 8, "output_tokens": 8}
        self.calls.append(str((payload or {}).get("text") or prompt or ""))
        return self._out


def test_qa():
    from app.services import qa

    # gate: identical-to-source is the CORRECT target of a tags-only segment, so QA (editor,
    # LQA structural errors, the quality lens' opt-in block) never calls the copy untranslated.
    for s in ("⟦x1⟧ ⟦x2⟧", "{count}", "⟦x1⟧ {n}"):
        if any(f["type"] == "untranslated" for f in qa.check_segment(s, s)):
            _fail(f"[qa] a tags-only copy is not 'untranslated': {s!r}")
    for s in ("Save", "{first} {last}"):
        if not any(f["type"] == "untranslated" for f in qa.check_segment(s, s)):
            _fail(f"[qa] an identical non-tags-only target is still 'untranslated': {s!r}")
    from app.services.lqa_downstream_findings_bloc import downgrade_numeral_identical

    errs = [{"dimension": "Accuracy/Untranslated", "severity": "major", "note": "untranslated"}]
    downgrade_numeral_identical(errs, "⟦x1⟧ ⟦x2⟧", "⟦x1⟧ ⟦x2⟧")
    if errs[0]["severity"] != "neutral":
        _fail(f"[qa] the identical finding on a tags-only copy is advisory: {errs!r}")
    errs = [{"dimension": "Accuracy/Untranslated", "severity": "major", "note": "untranslated"}]
    downgrade_numeral_identical(errs, "Save", "Save")
    if errs[0]["severity"] != "major":
        _fail("[qa] the identical finding on a text segment still fails the gate")
    print("  [qa] no 'untranslated' flag and an advisory LQA finding on a tags-only copy OK")


def test_helper():
    t = tags_only.is_tags_only
    yes = ["⟦x1⟧", "⟦x1⟧\n\t\t⟦x2⟧ ", "⟦g1⟧⟦/g1⟧", "{0}", "{name}", " {{count}} ", "%s",
           "%05d", "%{name}", "%(count)d", "${user}", "⟦x1⟧{0}"]  # fmt: skip
    no = ["", "   ", "Save", "⟦x1⟧ Save ⟦x2⟧", "⟦x1⟧ 2024 ⟦x2⟧", "⟦x1⟧?⟦x2⟧", "{0} items",
          "50%", "%%", "%d%%", "<b></b>", "⟦x1⟧ ⟦not a tag⟧", "{count, plural, one {#} other {#}}",
          "{ name }",
          # gate: two placeholders are a format string whose ORDER can be the translation
          "%1$s %2$s", "{first} {last}", "{a}{b}", "⟦x1⟧{0} {1}"]  # fmt: skip
    for s in yes:
        if not t(s):
            _fail(f"[helper] expected tags-only: {s!r}")
    for s in no:
        if t(s):
            _fail(f"[helper] expected text: {s!r}")
    # #599: request text must stay linear on hostile shapes.
    for hostile in ("%" + "0" * 200_000 + "!", "%1" * 100_000, "{" + "a" * 200_000, "%(" * 100_000):
        t0 = time.time()
        t(hostile)
        if time.time() - t0 > 2.0:
            _fail(f"[helper] predicate is not linear on {hostile[:12]!r}...")
    # gate: the context / style A/B experiments never sample a tags-only unit (two arm calls
    # and a judge call on identical output, and a diluted delta).
    from app.services import context_ab, style_ab

    for mod in (context_ab, style_ab):
        if mod._has_text("⟦x1⟧ ⟦x2⟧") or mod._has_text("{count}") or mod._has_text("  "):
            _fail(f"[helper] {mod.__name__} must not sample a tags-only / empty unit")
        if not (mod._has_text("Save the file") and mod._has_text("{first} {last}")):
            _fail(f"[helper] {mod.__name__} still samples text units")
    print("  [helper] predicate cases + linear on hostile input + A/B sampling OK")


if __name__ == "__main__":
    test_helper()
    test_qa()
    test_step_engine_and_eval()
    test_workflow()
    test_gate_hold_spares_copies()
    test_keys()
    print("TAGS ONLY EVERYWHERE 705 OK")
