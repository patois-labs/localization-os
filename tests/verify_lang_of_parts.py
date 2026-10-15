"""Language of parts (a11y train T5, WCAG 3.1.2) -- offline, hermetic.

Source and target text on the pages people translate on is marked with its own language
(`lang`, plus `dir="rtl"` for a right-to-left locale), so a screen reader switches voice
and a browser lays out Arabic or Hebrew right to left, while the English UI around it
stays English. Wrong `lang` is worse than none, so the suite proves both halves:

  [U] app/services/languages_catalog.html_lang / text_direction: legacy codes (es_ES,
      EN-us) normalize, script subtags decide direction (ku vs ku-Arab, pa-Arab, sd-Deva,
      ug-Latn), and blank / dash / star / junk with quotes or angle brackets give "" and
      never raise.
  [F] the Jinja filters langattrs / langtag / langdir (app/web/deps.py): escaped Markup,
      dir only for RTL, "" when unusable.
  [R] rendered pages with an Arabic (ar) target: Editor, Document, LQA, TM, Glossary
      detail, project keys and the Run view carry lang="ar" dir="rtl" on the target text
      nodes and lang="en-US" on the source; NO badge, chip, button, summary, label or
      table header sits under a target (or source) lang; an LQA step's output is untagged.
  [L] a legacy es_ES segment renders lang="es-ES"; a junk locale renders no lang and
      the page is still a 200.
  [S] the four surfaces of `direction`: GET /api/languages `directions`, the MCP
      list_languages pass-through, CLI `lang list --json`.

ASCII-only prints (the Windows console is cp1252).
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import quote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_langparts_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from markupsafe import Markup
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossaryTerm,
    ProjectType,
    RunStatus,
    Segment,
    SegmentTM,
    StepRun,
    StepType,
    TermCandidate,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import content, languages_catalog, qe_autofix, settings
from app.services import keys as keys_svc
from app.web.deps import langattrs, langdir, langtag

AR_TGT = "مغامرة في الانتظار"
AR_TERM = "مغامرة"
AR_KEY = "مرحبا"
AR_CONCEPT = "جواز السفر"
AR_ROUND = "مغامرة تنتظرك"

# (code, html_lang, text_direction)
UNIT_TABLE = [
    ("es_ES", "es-ES", "ltr"),
    ("EN-us", "en-US", "ltr"),
    ("en-US", "en-US", "ltr"),
    ("zh-Hant", "zh-Hant", "ltr"),
    ("zh_hant_tw", "zh-Hant-TW", "ltr"),
    ("sr-Latn", "sr-Latn", "ltr"),
    ("es-419", "es-419", "ltr"),
    ("ar", "ar", "rtl"),
    ("ar-SA", "ar-SA", "rtl"),
    ("he", "he", "rtl"),
    ("fa-IR", "fa-IR", "rtl"),
    ("ur-PK", "ur-PK", "rtl"),
    ("ckb", "ckb", "rtl"),
    ("ku", "ku", "ltr"),
    ("ku-Arab", "ku-Arab", "rtl"),
    ("pa", "pa", "ltr"),
    ("pa-Arab", "pa-Arab", "rtl"),
    ("sd", "sd", "rtl"),
    ("sd-Deva", "sd-Deva", "ltr"),
    ("ug", "ug", "rtl"),
    ("ug-Latn", "ug-Latn", "ltr"),
    ("az-Arab", "az-Arab", "rtl"),
    ("en-US-x-rtlish", "en-US-x-rtlish", "ltr"),
    # unusable: "" and never an exception
    ("", "", ""),
    (None, "", ""),
    ("   ", "", ""),
    ("-", "", ""),
    (chr(0x2014), "", ""),  # the dash placeholder some pages show for "no locale"
    ("*", "", ""),
    ('en" onmouseover="alert(1)', "", ""),
    ("<script>", "", ""),
    ("fr'FR", "", ""),
    ("en US", "", ""),
    ("en--US", "", ""),
    ("e", "", ""),
    ("x" * 200, "", ""),
    (12, "", ""),
    (["ar"], "", ""),
]


class _Unprintable:
    def __str__(self):
        raise RuntimeError("no string for you")


def test_unit_table() -> None:
    for code, want_lang, want_dir in UNIT_TABLE:
        got_lang = languages_catalog.html_lang(code)
        got_dir = languages_catalog.text_direction(code)
        assert got_lang == want_lang, (code, got_lang, want_lang)
        assert got_dir == want_dir, (code, got_dir, want_dir)
    assert languages_catalog.html_lang(_Unprintable()) == ""
    assert languages_catalog.text_direction(_Unprintable()) == ""
    print(f"  [U] html_lang / text_direction: {len(UNIT_TABLE) + 1} codes, junk never raises")


def test_filters() -> None:
    got = langattrs("ar")
    assert isinstance(got, Markup) and str(got) == ' lang="ar" dir="rtl"', got
    assert str(langattrs("es_ES")) == ' lang="es-ES"', langattrs("es_ES")
    for junk in ("", None, "*", chr(0x2014), 'x" onload="y', "<b>"):
        out = langattrs(junk)
        assert isinstance(out, Markup) and str(out) == "", (junk, out)
    assert langtag("EN-us") == "en-US" and langtag("*") == ""
    assert langdir("he-IL") == "rtl" and langdir("de") == "ltr" and langdir("") == ""
    print("  [F] langattrs = escaped Markup, dir only for RTL, empty when unusable")


# --------------------------------------------------------------------------- #
# Rendered-page inspection
# --------------------------------------------------------------------------- #
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"}
_CHROME_TAGS = {"button", "summary", "label", "th", "option", "select"}
_CHROME_CLASSES = {"badge", "chip", "btnlink", "mini", "status"}


class _LangParser(HTMLParser):
    """Records every element with its effective lang (the nearest lang BELOW <html>)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None, str | None]] = []
        self.elems: list[dict] = []
        self._open: list[dict] = []

    def _push(self, tag, attrs, void):
        a = {k: (v or "") for k, v in attrs}
        inherited = self.stack[-1][1] if self.stack else None
        own = a.get("lang") if tag != "html" else None
        eff = own if own is not None else inherited
        dir_inh = self.stack[-1][2] if self.stack else None
        eff_dir = a.get("dir", dir_inh) if tag != "html" else None
        parent = self._open[-1]["idx"] if self._open else -1
        el = {
            "tag": tag,
            "attrs": a,
            "lang": eff,
            "dir": eff_dir,
            "own_lang": own,
            "text": "",
            "own_text": "",
            "idx": len(self.elems),
            "parent": parent,
        }
        self.elems.append(el)
        if not void:
            self.stack.append((tag, eff, eff_dir))
            self._open.append(el)

    def handle_starttag(self, tag, attrs):
        self._push(tag, attrs, tag in _VOID)

    def handle_startendtag(self, tag, attrs):
        self._push(tag, attrs, True)

    def handle_endtag(self, tag):
        names = [s[0] for s in self.stack]
        if tag in names:
            while self.stack:
                self._open.pop()
                if self.stack.pop()[0] == tag:
                    break

    def handle_data(self, data):
        for el in self._open:
            el["text"] += data
        if self._open:
            self._open[-1]["own_text"] += data


def _parse(html: str) -> _LangParser:
    p = _LangParser()
    p.feed(html)
    p.close()
    return p


def _untagged_arabic(html: str) -> list[str]:
    """Every piece of Arabic-script text on the page (an element's own text, or an
    input's value) whose effective lang is not an Arabic tag. Script bodies are data."""
    out = []
    for el in _parse(html).elems:
        if el["tag"] in ("script", "style"):
            continue
        parts = [el["own_text"]]
        if el["tag"] == "input":
            parts.append(el["attrs"].get("value", ""))
        for part in parts:
            arabic = any(0x0600 <= ord(ch) <= 0x06FF for ch in part)
            if arabic and not (el["lang"] or "").startswith("ar"):
                out.append(f"<{el['tag']}> lang={el['lang']!r}: {part.strip()[:60]!a}")
    return out


def _chrome_untagged(page: str, html: str) -> int:
    """No UI chrome (badges, chips, buttons, summaries, labels, headers) sits under a
    content lang: its effective lang must be the page's own (none below <html>)."""
    n = 0
    for el in _parse(html).elems:
        classes = set(el["attrs"].get("class", "").split())
        if el["tag"] in _CHROME_TAGS or classes & _CHROME_CLASSES:
            n += 1
            assert el["lang"] is None, (
                f"{page}: UI chrome <{el['tag']} class={el['attrs'].get('class', '')!r}> "
                f"inherits lang={el['lang']!r}: {el['text'][:80]!r}"
            )
    return n


def _with_text(html: str, text: str) -> list[dict]:
    """The innermost elements whose text contains `text` (no child element holds it)."""
    els = [e for e in _parse(html).elems if text in e["text"]]
    holders = {e["idx"] for e in els}
    has_child = {e["parent"] for e in els if e["parent"] in holders}
    return [e for e in els if e["idx"] not in has_child]


def _assert_lang(page: str, html: str, text: str, lang: str, rtl: bool) -> None:
    hits = _with_text(html, text)
    assert hits, f"{page}: text {text!r} not rendered"
    for el in hits:
        assert el["lang"] == lang, (page, text, el["tag"], el["lang"], lang)
        want_dir = "rtl" if rtl else None
        assert el["dir"] == want_dir, (page, text, el["tag"], el["dir"], want_dir)


def docx_from(paragraphs) -> bytes:
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def _setup_document(c: TestClient) -> tuple[int, int, int]:
    with Session(engine) as s:
        cust = content.customer_create(s, "LangCo")
        p = content.project_create(
            s, "LangParts", source_locale="en-US", target_locales="ar", customer_id=str(cust.id)
        )
        cid, pid = cust.id, p.id
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_locale="en-US",
                target_locale="ar",
                source_term="Adventure",
                target_term=AR_TERM,
            )
        )
        s.commit()
    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "t5.docx",
                docx_from(["Adventure awaits at the desk.", "Second sentence here."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "ar")
            ).all(),
            key=lambda x: x.order_index,
        )
        assert segs, "no ar segments"
        segs[0].target_text = AR_TGT
        # A QE auto-fix history: each round's candidate is ar target text (gate LOW 2).
        fix = qe_autofix.record(outcome="fixed", original_score=60, final_score=100)
        fix["rounds"] = [
            {"round": 0, "text": AR_TGT, "score": 60},
            {"round": 1, "text": AR_ROUND, "score": 100},
        ]
        segs[0].qa_flags = [fix]
        s.add(segs[0])
        # Legacy spellings: a grandfathered es_ES copy and a junk code, as stored.
        for loc, text in (("es_ES", "La aventura espera."), ('bad"<code>', "junk locale text")):
            data = segs[0].model_dump(exclude={"id"})
            data.update(target_locale=loc, target_text=text)
            s.add(Segment(**data))
        s.commit()
    return cid, pid, doc_id


def test_pages(c: TestClient) -> None:
    cid, pid, doc_id = _setup_document(c)
    counts = {}

    # Editor
    html = c.get(f"/documents/{doc_id}/editor?locale=ar").text
    _assert_lang("editor", html, "Adventure awaits at the desk.", "en-US", False)
    _assert_lang("editor", html, AR_TGT, "ar", True)
    assert '<span lang="en-US">Adventure</span>' in html, "editor source term span"
    assert f'→ <span lang="ar" dir="rtl">{AR_TERM}</span>' in html, "editor target term span"
    assert 'const TGT_LANG = "ar", TGT_DIR = "rtl";' in html, "editor JS constants"
    textareas = [
        e
        for e in _parse(html).elems
        if e["tag"] == "textarea" and "tgt-edit" in e["attrs"].get("class", "")
    ]
    assert textareas and all(e["own_lang"] == "ar" for e in textareas), "editor textareas"
    _assert_lang("editor qe auto-fix round", html, AR_ROUND, "ar", True)
    bad = _untagged_arabic(html)
    assert not bad, f"editor: untagged Arabic text {bad}"
    counts["editor"] = _chrome_untagged("editor", html)

    # Document
    html = c.get(f"/documents/{doc_id}?locale=ar").text
    _assert_lang("document", html, "Adventure awaits at the desk.", "en-US", False)
    _assert_lang("document", html, AR_TGT, "ar", True)
    counts["document"] = _chrome_untagged("document", html)

    # LQA
    html = c.get(f"/documents/{doc_id}/lqa?locale=ar").text
    _assert_lang("lqa", html, AR_TGT, "ar", True)
    tds = [
        e for e in _parse(html).elems if e["tag"] == "td" and "src" in e["attrs"].get("class", "")
    ]
    assert tds and all(e["own_lang"] == "en-US" for e in tds), "lqa source cells"
    counts["lqa"] = _chrome_untagged("lqa", html)

    # TM
    with Session(engine) as s:
        s.add(
            SegmentTM(
                project_id=pid,
                source_locale="en-US",
                target_locale="ar",
                source_text="Book the flight",
                target_text=AR_TGT,
                source_hash="t5|tm|1",
                use_count=2,
            )
        )
        s.commit()
    html = c.get(f"/tm?customer={cid}").text
    _assert_lang("tm", html, "Book the flight", "en-US", False)
    _assert_lang("tm", html, AR_TGT, "ar", True)
    counts["tm"] = _chrome_untagged("tm", html)

    # Glossary detail
    with Session(engine) as s:
        g = Glossary(name="T5 terms", customer_id=cid)
        s.add(g)
        s.commit()
        s.refresh(g)
        gid = g.id
        s.add(GlossaryPair(glossary_id=gid, source_locale="en-US", target_locale="ar"))
        s.add(
            GlossaryEntry(
                glossary_id=gid,
                source_locale="en-US",
                target_locale="ar",
                source_term="Checkout",
                target_term=AR_KEY,
            )
        )
        # A term candidate: the _macros.html grid (langattrs called as a global there).
        s.add(
            TermCandidate(
                glossary_id=gid,
                source_term="Boarding pass",
                proposed_target=AR_TERM,
                source_locale="en-US",
                target_locale="ar",
                context_snippet="Show your boarding pass at the gate.",
            )
        )
        # A concept with an en-US and an ar term: the Concepts card (gate round 1, MEDIUM 1).
        concept = GlossaryConcept(glossary_id=gid, definition="Travel document")
        s.add(concept)
        s.flush()
        s.add(GlossaryTerm(concept_id=concept.id, locale="en-US", term="Passport"))
        s.add(GlossaryTerm(concept_id=concept.id, locale="ar", term=AR_CONCEPT))
        s.commit()
    html = c.get(f"/glossaries/{gid}").text
    _assert_lang("glossary", html, "Checkout", "en-US", False)
    _assert_lang("glossary", html, AR_KEY, "ar", True)
    _assert_lang("glossary concepts", html, "Passport", "en-US", False)
    _assert_lang("glossary concepts", html, AR_CONCEPT, "ar", True)
    bad = _untagged_arabic(html)
    assert not bad, f"glossary: untagged Arabic text {bad}"
    # The concept editor's per-term edit input (feature flag on for this one render).
    settings.set_many({"terminology_concept_editor_enabled": "1"})
    try:
        edit_html = c.get(f"/glossaries/{gid}").text
    finally:
        settings.set_many({"terminology_concept_editor_enabled": "0"})
    term_in = [
        e
        for e in _parse(edit_html).elems
        if e["tag"] == "input" and e["attrs"].get("name") == "term"
    ]
    by_term = {e["attrs"].get("value"): e for e in term_in}
    assert by_term.get("Passport", {}).get("own_lang") == "en-US", "concept term edit input en"
    ar_edit = by_term.get(AR_CONCEPT, {})
    assert ar_edit.get("own_lang") == "ar" and ar_edit.get("attrs", {}).get("dir") == "rtl", (
        "concept term edit input ar"
    )
    bad = _untagged_arabic(edit_html)
    assert not bad, f"glossary (concept editor on): untagged Arabic text {bad}"
    _chrome_untagged("glossary (concept editor on)", edit_html)
    _assert_lang("glossary candidates", html, "Boarding pass", "en-US", False)
    _assert_lang(
        "glossary candidates", html, "Show your boarding pass at the gate.", "en-US", False
    )
    cand = [
        e for e in _parse(html).elems if e["tag"] == "input" and e["attrs"].get("value") == AR_TERM
    ]
    assert cand and all(e["own_lang"] == "ar" for e in cand), "candidate target input"
    counts["glossary"] = _chrome_untagged("glossary", html)

    # Project keys
    with Session(engine) as s:
        kp = content.project_create(
            s,
            "LangKeys",
            source_locale="en-US",
            target_locales="ar",
            customer_id=str(cid),
            project_type=ProjectType.KEY_BASED,
        )
        kpid = kp.id
        key = keys_svc.create_key(s, kp, "greet.hello", base_value="Hello there", actor="t5")
        keys_svc.set_value(s, key, "ar", AR_KEY, origin=keys_svc.ORIGIN_IMPORT)
    html = c.get(f"/projects/{kpid}/keys").text
    inputs = [
        e for e in _parse(html).elems if e["tag"] == "input" and e["attrs"].get("name") == "text"
    ]
    by_value = {e["attrs"].get("value"): e for e in inputs}
    assert by_value.get("Hello there", {}).get("own_lang") == "en-US", "keys base input"
    ar_in = by_value.get(AR_KEY, {})
    assert ar_in.get("own_lang") == "ar" and ar_in.get("attrs", {}).get("dir") == "rtl", (
        "keys ar input"
    )
    counts["keys"] = _chrome_untagged("keys", html)

    # Run view (static render of a finished run): the translate step's output is ar,
    # the LQA step's output is not tagged.
    with Session(engine) as s:
        wf = Workflow(name="T5 flow")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        st1 = WorkflowStep(
            workflow_id=wf.id, order_index=0, name="Translate", step_type=StepType.LLM_AGENT
        )
        st2 = WorkflowStep(workflow_id=wf.id, order_index=1, name="Check", step_type=StepType.LQA)
        s.add(st1)
        s.add(st2)
        s.commit()
        run = WorkflowRun(
            workflow_id=wf.id, document_id=doc_id, target_locale="ar", status=RunStatus.COMPLETED
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=st1.id,
                status=RunStatus.COMPLETED,
                output_text=AR_TGT,
            )
        )
        s.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=st2.id,
                status=RunStatus.COMPLETED,
                output_text="LQA judge note: fluent",
            )
        )
        s.commit()
        run_id, wf_id, st1_id = run.id, wf.id, st1.id
    html = c.get(f"/runs/{run_id}").text
    _assert_lang("run view", html, AR_TGT, "ar", True)
    lqa_pre = _with_text(html, "LQA judge note: fluent")
    assert lqa_pre and all(e["lang"] is None for e in lqa_pre), "an LQA step's output was tagged"
    assert 'data-lang="ar" data-dir="rtl"' in html, "run view #stepLog data attributes"
    assert 'data-text-steps="external human_review llm_agent tm_match"' in html, html[:200]
    counts["run view"] = _chrome_untagged("run view", html)

    # Legacy runs (target_locale == "", gate LOW 6): the page may only guess the language
    # when the document has exactly one target locale. doc_id holds ar, es_ES and a junk
    # code, so its legacy run tags nothing; a one-locale document's legacy run tags ar.
    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "t5-one.docx",
                docx_from(["One locale only."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    one_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    legacy = {}
    with Session(engine) as s:
        for label, d in (("multi", doc_id), ("one", one_doc)):
            lrun = WorkflowRun(
                workflow_id=wf_id, document_id=d, target_locale="", status=RunStatus.COMPLETED
            )
            s.add(lrun)
            s.commit()
            s.refresh(lrun)
            s.add(
                StepRun(
                    workflow_run_id=lrun.id,
                    step_id=st1_id,
                    status=RunStatus.COMPLETED,
                    output_text=f"{AR_ROUND} {label}",
                )
            )
            s.commit()
            legacy[label] = lrun.id
    html = c.get(f"/runs/{legacy['multi']}").text
    multi = _with_text(html, f"{AR_ROUND} multi")
    assert multi and all(e["lang"] is None for e in multi), "multi-locale legacy run was tagged"
    assert 'data-lang="" data-dir=""' in html, "multi-locale legacy #stepLog carries a lang"
    assert ' lang="' not in html.split("<body", 1)[1], "multi-locale legacy run tags a part"
    html = c.get(f"/runs/{legacy['one']}").text
    _assert_lang("legacy one-locale run", html, f"{AR_ROUND} one", "ar", True)
    assert 'data-lang="ar" data-dir="rtl"' in html, "one-locale legacy #stepLog"

    print(
        "  [R] ar target: lang+dir on target, en-US on source, no chrome tagged ("
        + ", ".join(f"{k} {v}" for k, v in counts.items())
        + " chrome elements checked)"
    )
    return doc_id


def test_legacy_codes(c: TestClient, doc_id: int) -> None:
    r = c.get(f"/documents/{doc_id}/editor?locale=es_ES")
    assert r.status_code == 200, r.status_code
    _assert_lang("editor es_ES", r.text, "La aventura espera.", "es-ES", False)
    junk = 'bad"<code>'
    for path in (f"/documents/{doc_id}/editor", f"/documents/{doc_id}", f"/documents/{doc_id}/lqa"):
        r = c.get(f"{path}?locale={quote(junk)}")
        assert r.status_code == 200, (path, r.status_code)
        hits = _with_text(r.text, "junk locale text")
        assert hits, (path, "junk-locale text not rendered")
        assert all(e["lang"] is None for e in hits), (path, [e["lang"] for e in hits])
        assert 'lang="bad' not in r.text, path
    print("  [L] legacy es_ES renders lang=es-ES; a junk locale renders no lang and a 200")


def _cli_json(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(["--json", *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    text = out.getvalue().strip()
    return code, (json.loads(text.splitlines()[-1]) if text else {}), err.getvalue()


def test_surfaces(c: TestClient) -> None:
    # CLI: lang list --json carries direction.
    code, payload, err = _cli_json(["lang", "list", "--all"])
    assert code == 0 and payload.get("ok"), (code, err)
    rows = {r["code"]: r for r in payload["languages"]}
    assert rows["ar"]["direction"] == "rtl" and rows["he"]["direction"] == "rtl", rows.get("ar")
    assert rows["en-US"]["direction"] == "ltr", rows.get("en-US")
    # CLI text table: `dir` is APPENDED after the existing columns (gate LOW 5), so a text
    # scraper reading code / name / enabled by position sees nothing move.
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(["lang", "list", "--all", "--search", "Arabic"])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert code == 0 and lines, (code, err.getvalue())
    assert lines[0].split() == ["code", "name", "enabled", "dir"], lines[0]
    ar_line = next(ln for ln in lines if ln.split()[:1] == ["ar"])
    assert ar_line.split()[-1] == "rtl", ar_line
    # docs/CLI.md documents the column and the --json field.
    with open(os.path.join(_ROOT, "docs", "CLI.md"), encoding="utf-8") as fh:
        cli_md = fh.read()
    row = next(ln for ln in cli_md.splitlines() if ln.startswith("| `lang list"))
    assert "`dir`" in row and "`direction`" in row, row
    # MCP: list_languages passes the API's directions through, and its registry summary
    # says so.
    with open(
        os.path.join(_ROOT, "integrations", "mcp-server", "server.py"), encoding="utf-8"
    ) as fh:
        src = fh.read()
    body = src.split("async def list_languages()", 1)[1].split("\nasync def ", 1)[0]
    assert '"directions": body.get("directions", {})' in body, body
    with open(
        os.path.join(_ROOT, "integrations", "mcp-server", "registry.py"), encoding="utf-8"
    ) as fh:
        reg = fh.read()
    reg_body = reg.split('name="list_languages"', 1)[1].split("Tool(", 1)[0]
    assert "directions" in reg_body, reg_body
    # API: GET /api/languages returns directions for every listed locale (a docs key).
    from app.models import Role
    from app.services import apikeys, languages, security

    fixture_pw = "t5-docs-pw-1234"  # a throwaway user in the hermetic test DB
    user = security.create_user("t5docs", "T5 Docs", Role.ADMIN, fixture_pw)
    _k, raw_key = apikeys.create_key(user.id, "t5 docs", scopes="api docs")
    languages.create("T5 set", "en-US", ["ar", "fa-IR", "de-DE"])
    hdr = {"X-Loc-Token": raw_key}  # pregate: allow-secret throwaway key
    r = c.get("/api/languages", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    got = r.json()
    assert set(got["directions"]) == set(got["locales"]), got
    assert got["directions"]["ar"] == "rtl" and got["directions"]["fa-IR"] == "rtl", got
    assert got["directions"]["en-US"] == "ltr" and got["directions"]["de-DE"] == "ltr", got
    print("  [S] direction on /api/languages (directions), MCP list_languages, CLI lang list")


def main() -> None:
    init_db()
    test_unit_table()
    test_filters()
    c = TestClient(app)
    doc_id = test_pages(c)
    test_legacy_codes(c, doc_id)
    test_surfaces(c)
    print("collected ids: U F R L S")
    print("LANG OF PARTS OK")


if __name__ == "__main__":
    main()
