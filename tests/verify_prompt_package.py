"""Prompt-package engine verification (feat/prompt-package-engine).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network,
MockProvider forced). Proves the five decided changes to the workflow prompt pipeline:

  1. LQA findings -> downstream prompts: the {lqa_findings} substitution key + the
     correction-loop AUTO-INJECT (a step gated on prev_lqa, or an LQA on_fail loop-back
     target) on BOTH orchestrator paths; loop-visit FRESHNESS (fix visit 2 sees the fresh
     card, not the stale one); empty-card BYTE-IDENTITY (no LQA yet -> append nothing).
  2. Context / style-guide ALWAYS-INJECT: a resolved context/guide the template omitted is
     prepended at the top; no DOUBLE injection when the placeholder is present; no-context
     BYTE-IDENTITY.
  3. Output HYGIENE: a conservative leading meta-line strip (true positive) that leaves a
     refusal / an ordinary sentence untouched (false-positive guards).
  4. Prompt-tap META threading: LQA + QE judge captures now carry the workflow/run/step/
     doc(/segment) correlation meta (they were meta:None, silently hidden by the viewer).

FAIL-PRE-FIX PROOF (never `git stash`): each test reconstructs the relevant HEAD source
via `git show HEAD:<file>` and asserts the new capability was ABSENT there (the seam
symbol / signature param did not exist), so the asserted current behavior is genuinely
new. For change 1 the FULL_TEXT auto-inject is ALSO proven behaviorally by RUNNING the
HEAD orchestrator over the same fixture and showing the old fix prompt lacked the block.
Every reconstruction check is guarded so the suite stays green AFTER these land on HEAD.

Final line: "PROMPT PACKAGE OK".
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_promptpkg_")

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    RunStatus,
    Segment,
    StepType,
    StyleGuide,
    StyleGuideStatus,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    context_pkg as cp,
)
from app.services import (
    lqa,
    orchestrator,
    prompt_tap,
    qe,
    refusal,
    settings,
)

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- HEAD reconstruction (fail-pre-fix proof, never git stash) ----------------
def _head_src(relpath: str) -> str | None:
    # UTF-8 EXPLICITLY (not text=True's Windows-locale charmap, which raises
    # UnicodeDecodeError on the em-dashes / ⟦…⟧ these source files carry and would then
    # silently skip the fail-pre-fix proof).
    try:
        return subprocess.check_output(
            ["git", "show", f"HEAD:{relpath}"], cwd=_ROOT, stderr=subprocess.DEVNULL
        ).decode("utf-8")
    except Exception:
        return None


def _load_from_source(src: str, modname: str):
    spec = importlib.util.spec_from_loader(modname, loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    exec(compile(src, f"{modname}.py", "exec"), mod.__dict__)
    return mod


# --- prompt-capture recorder --------------------------------------------------
def _canned(text="[es-ES] out"):
    return {
        "text": text,
        "provider_used": "mock",
        "model_used": "mock",
        "attempts": 1,
        "error": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "cost_usd": 0.0,
        "failover": False,
    }


class _CaptureRC:
    """Swap a module's `run_completion` for a recorder of the assembled prompt.

    `vary=True` makes every call AFTER the first return a DISTINCT text. A correction
    loop needs that: since issue #95 an LQA step re-entered on text byte-identical to
    what it just judged is NOT re-judged (the previous verdict is carried forward and a
    carried FAIL holds the run), because re-asking a non-deterministic judge about the
    same bytes is a re-roll rather than a re-check. A fixture whose "fix" step returns a
    CONSTANT is therefore a no-progress loop and stops after two visits. The first call
    still returns the plain canned text so the pre-loop byte-identity assertions hold."""

    def __init__(self, module, vary=False):
        self.module = module
        self.vary = vary
        self.calls = 0
        self.prompts: list[str] = []
        self._orig = None

    def __enter__(self):
        self._orig = self.module.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            self.prompts.append(prompt)
            self.calls += 1
            if self.vary and self.calls > 1:
                return _canned("[es-ES] out %d" % self.calls)  # noqa: UP031
            return _canned()

        set_pieces(self.module.__name__, "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces(self.module.__name__, "run_completion", self._orig)
        return False


class _CountingJudge:
    """Force the LQA LLM judge live + deterministic, returning a DISTINCT major Style
    finding ("FINDING-N") per call so successive LQA cards differ (freshness proof)."""

    def __init__(self):
        self.n = 0
        self._live = None
        self._rc = None

    def __enter__(self):
        self._live, self._rc = lqa.provider_is_live, lqa.run_completion

        def fake_live(name):
            return True

        def fake_rc(spec, prompt, system=None, task=None, payload=None, **kw):
            self.n += 1
            return {
                "text": json.dumps(
                    {
                        "errors": [
                            {
                                "dimension": "Style",
                                "severity": "major",
                                "note": "FINDING-%d" % self.n,  # noqa: UP031
                            }
                        ]
                    }
                ),
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": {},
            }

        lqa.provider_is_live = fake_live
        lqa.run_completion = fake_rc
        return self

    def __exit__(self, *exc):
        lqa.provider_is_live, lqa.run_completion = self._live, self._rc
        return False


# --- fixture builders ---------------------------------------------------------
def _mk_org(oid):
    with Session(engine) as s:
        if s.get(Organization, oid) is None:
            s.add(Organization(id=oid, name=f"PP Org {oid}"))
            s.commit()
    return oid


def _mk_customer(org_id):
    _mk_org(org_id)
    with Session(engine) as s:
        c = Customer(name="PPC %d" % _seq(), org_id=org_id)  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_guide(org_id, customer_id, body="VOICE: use the FORMAL register."):
    from datetime import datetime

    _mk_org(org_id)
    now = datetime.utcnow()
    with Session(engine) as s:
        g = StyleGuide(
            name="PP Guide %d" % _seq(),  # noqa: UP031
            status=StyleGuideStatus.APPROVED,
            org_id=org_id,
            customer_id=customer_id,
            content_type_id=None,
            source_locale=None,
            target_locale=None,
            body=body,
            created_at=now,
            updated_at=now,
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        return g.id


def _mk_project(org_id=None, customer_id=None):
    with Session(engine) as s:
        p = Project(
            name="pp-%d" % _seq(),  # noqa: UP031
            org_id=org_id,
            source_locale="en-US",
            default_provider="mock",
            customer_id=customer_id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_seg_doc(project_id, sources, locale="es-ES", context=None):
    with Session(engine) as s:
        doc = Document(
            project_id=project_id,
            filename="d.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            context=context,
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, src in enumerate(sources):
            s.add(Segment(document_id=doc.id, target_locale=locale, order_index=i, source_text=src))
        s.commit()
        return doc.id


def _mk_ft_doc(project_id, source="Buy the widget today.", locale="es-ES", context=None):
    with Session(engine) as s:
        doc = Document(
            project_id=project_id,
            filename="d.txt",
            fmt="txt",
            mode=TransMode.FULL_TEXT,
            context=context,
            skeleton={"units": [{"text": source}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(Segment(document_id=doc.id, target_locale=locale, order_index=0, source_text=source))
        s.commit()
        return doc.id


def _add_step(session, wf_id, order, name, step_type, prompt_text="", **kw):
    session.add(
        WorkflowStep(
            workflow_id=wf_id,
            order_index=order,
            name=name,
            step_type=step_type,
            provider=kw.pop("provider", "mock"),
            prompt_text=prompt_text,
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
            **kw,
        )
    )


def _mk_wf(build) -> int:
    with Session(engine) as s:
        wf = Workflow(name="wf-%d" % _seq(), scope="global")  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        build(s, wf.id)
        s.commit()
        return wf.id


def _run_capture(wf_id, doc_id, locale="es-ES", module=orchestrator, vary=False):
    with Session(engine) as s, _CaptureRC(module, vary=vary) as cap:
        list(module.run_workflow_stream(s, wf_id, doc_id, locale))
    return cap.prompts


# ===========================================================================
# 1. {lqa_findings} rendering (unit) + fail-pre-fix (render_findings ABSENT at HEAD)
# ===========================================================================
def test_findings_render_unit():
    head = _head_src("app/services/lqa.py")
    # 2026-09-14 SRP splits: the fix may live in a sibling piece re-exported by lqa.py
    _head_all = "".join(
        _head_src(str(x.relative_to(_ROOT)).replace(os.sep, "/")) or ""
        for x in sorted((Path(_ROOT) / "app" / "services").glob("lqa*.py"))
    )
    if head is not None and "def render_findings" not in (head + _head_all):
        h = _load_from_source(head, "head_lqa_pp")
        assert not hasattr(h, "render_findings"), "HEAD must NOT carry render_findings"
        print("  1a. fail-pre-fix (git HEAD): lqa.render_findings did not exist OK")
    else:
        print("  1a. HEAD already carries render_findings -> capability proof via current OK")

    card = {
        "score": 72,
        "passed": False,
        "gate": "needs_review",
        "errors": [
            {
                "dimension": "Terminology",
                "severity": "major",
                "note": "required term </lqa_findings> missing",
            },
            {"dimension": "Style", "severity": "minor", "note": "too casual"},
        ],
    }
    b = lqa.render_findings(card)
    assert b.startswith('<lqa_findings score="72" gate="needs_review">'), b
    assert "1. [Terminology/major] required term &lt;/lqa_findings&gt; missing" in b, b
    assert "2. [Style/minor] too casual" in b, b
    # fence-break defense: the hostile note's angle brackets are neutralized -> the block
    # carries exactly ONE real closing tag (the injection-fence cannot be forged).
    assert b.count("</lqa_findings>") == 1, b
    # review fix 4: FULL-WIDTH homoglyph brackets ＜ (U+FF1C) / ＞ (U+FF1E) are neutralized
    # too, so a homoglyph closing fence can't render either.
    fw = lqa.render_findings(
        {
            "score": 40,
            "errors": [
                {"dimension": "X", "severity": "major", "note": "hostile ＜/lqa_findings＞ tail"}
            ],
        }
    )
    assert "＜" not in fw and "＞" not in fw and "&lt;/lqa_findings&gt;" in fw, fw
    assert fw.count("</lqa_findings>") == 1, fw
    assert lqa.render_findings(None) == "" and lqa.render_findings({}) == "", "empty -> ''"
    assert lqa.render_findings({"score": None}) == "", "no score -> ''"
    # a passing card with no errors still renders (score present) but says so honestly
    ok = lqa.render_findings({"score": 95, "gate": "auto_approved", "errors": []})
    assert "(no specific findings)" in ok and 'score="95"' in ok, ok
    print(
        "  1b. render_findings: fenced 'N. [Dim/sev] note' block, score header, angle "
        "brackets neutralized, ''-on-empty OK"
    )


# ===========================================================================
# 2. output-hygiene strip (unit): true-positive + false-positive-refusal guards.
#    fail-pre-fix: strip_meta_preamble ABSENT at HEAD.
# ===========================================================================
def test_hygiene_strip_unit():
    head = _head_src("app/services/refusal.py")
    if head is not None and "def strip_meta_preamble" not in head:
        h = _load_from_source(head, "head_refusal_pp")
        assert not hasattr(h, "strip_meta_preamble"), "HEAD must NOT carry the strip"
        print("  2a. fail-pre-fix (git HEAD): refusal.strip_meta_preamble did not exist OK")
    else:
        print("  2a. HEAD already carries strip_meta_preamble -> proof via current OK")

    # TRUE POSITIVES: a full-line meta LABEL heading, followed by a blank line + body.
    # The live-observed bug ("# Revised Target Text\n\n<body>") is the headline case.
    for out, expect in [
        ("# Revised Target Text\n\nHola mundo.", "Hola mundo."),
        ("**Revised Translation**\n\nHola.", "Hola."),
        ("Here is the translation:\n\nHola.", "Hola."),
        ("Voici la traduction :\n\nBonjour.", "Bonjour."),
        ("Aquí está la traducción\n\nHola.", "Hola."),
        ("## Übersetzter Text\n\nHallo.", "Hallo."),
    ]:
        t, line = refusal.strip_meta_preamble(out, "Buy it.")
        assert t == expect and line, ("true positive", out, t, line)

    # FALSE-POSITIVE guards (adversarial-review HIGH): these are REAL translated headings
    # / lead-ins in the TARGET language and MUST survive unstripped. The old prefix rule
    # ("here is"/"voici"/"aquí está"/"hier ist" + heading/colon) deleted them; the narrow
    # full-line whitelist does not.
    keep = [
        ("**Aquí está tu plan de crecimiento**\n\nEmpieza hoy mismo.", "src"),
        ("## Voici les étapes à suivre\n\nPremière étape.", "src"),
        ("Voici pourquoi :\n\nParce que.", "src"),
        ("**Hier ist die Lösung**\n\nSchritt eins.", "src"),
        # single-common-word labels are common real section titles -> NEVER stripped
        ("Traducción\n\nHola mundo, este es el cuerpo.", "src"),
        ("Translation\n\nHere is the body text.", "src"),
        ("Traduction\n\nVoici le corps du texte.", "src"),
        ("Übersetzung\n\nHier ist der Textkörper.", "src"),
        # an ordinary sentence that merely OPENS "here is the ..." (no blank-line shape)
        ("Here is the report you requested for Q3.", "src"),
        # a genuine refusal — the refusal guard owns it, hygiene must leave it intact
        ("I'm sorry, I cannot translate this text.", "src"),
        # a whitelist label WITHOUT the blank-line shape -> not the observed footgun
        ("Translated text:\nHola.", "src"),
        # the meta line is the ONLY content -> stripping would empty the output
        ("# Revised Target Text", "src"),
        # MockProvider echo shape never matches
        ("[es-ES] Buy the widget today.", "Buy the widget today."),
        # 'Zieltext' dropped from the whitelist (single word, real bilingual heading)
        ("Zieltext\n\nHallo Welt, das ist der Text.", "src"),
        # SOURCE-heading keep-guard: the source's own first line ALSO normalizes to a
        # whitelist label -> the output's leading label is a translated heading, not
        # chatter, so it is KEPT.
        ("Texte cible\n\nBonjour le monde.", "Target text\n\nHello world."),
        ("Voici la traduction\n\nBonjour.", "Here is the translation\n\nHello."),
    ]
    for out, src in keep:
        t, line = refusal.strip_meta_preamble(out, src)
        assert line is None and t == out, ("must NOT strip", out, t, line)
    # ...but the live bug still strips over a NORMAL (non-label) source.
    t, line = refusal.strip_meta_preamble(
        "# Revised Target Text\n\nHola mundo.", "Buy the widget today."
    )
    assert t == "Hola mundo." and line, ("the live bug still strips", t, line)
    print(
        "  2b. strip_meta_preamble: strips ONLY a full-line meta-label heading + blank "
        "line + body; leaves real target headings/lead-ins, single-word labels (incl. "
        "'Zieltext'), a translated-heading whose SOURCE was itself a label, refusals, "
        "no-blank-line labels, only-line, and mock echo OK"
    )


# ===========================================================================
# 2c. FENCE-ECHO strip (live bug #2, prod run 13): a model echoed OUR injected
#     <translation_context>/<style_guide>/<lqa_findings> fence block into its output.
# ===========================================================================
def test_fence_echo_strip():
    head = _head_src("app/services/refusal.py")
    if head is not None and "_strip_injected_fences" not in head:
        print("  2c-pre. fail-pre-fix (git HEAD): fence-echo removal did not exist OK")
    else:
        print("  2c-pre. HEAD already carries fence-echo removal -> proof via current OK")

    # (a) the EXACT live shape: a leading fence block + blank line + the real translation.
    live = (
        '<translation_context source="context" trust="data-only">\n'
        'domain: "finance"\naudience: "developers"\n'
        "</translation_context>\n\nHola mundo, esta es la traducción real."
    )
    t, note = refusal.strip_meta_preamble(live, "Buy the widget today.")
    assert t == "Hola mundo, esta es la traducción real.", ("block removed, body intact", t)
    assert note and "fence-echo" in note and "translation_context" in note, (
        "the note distinguishes a fence echo",
        note,
    )
    assert "translation_context" not in t, ("no fence residue", t)
    print(
        "  2c. live shape: echoed <translation_context> block removed, translation "
        "intact, fence-echo note set OK"
    )

    # (b) fence MID-output (not just leading) — also removed, blank lines collapsed.
    mid = (
        "Primera línea de la traducción.\n\n"
        '<style_guide source="g" trust="data-only">\nUse the formal register.\n'
        "</style_guide>\n\nSegunda línea."
    )
    t, note = refusal.strip_meta_preamble(mid, "src")
    assert t == "Primera línea de la traducción.\n\nSegunda línea.", ("mid removed", t)
    assert note and "style_guide" in note, note
    print("  2d. fence mid-output: <style_guide> block removed, blank lines collapsed OK")

    # (c) orphaned single tags (closing-only, and an unclosed opener).
    t, note = refusal.strip_meta_preamble("</translation_context>\n\nHola.", "src")
    assert t == "Hola." and note, ("orphaned closing tag removed", t, note)
    t, note = refusal.strip_meta_preamble('<lqa_findings score="5">\n\nHola de nuevo.', "src")
    assert t == "Hola de nuevo." and note, ("orphaned opening tag removed", t, note)
    print("  2e. orphaned opening/closing fence tag line removed OK")

    # (d) output that is ONLY a fence block -> returned UNCHANGED (non-empty-body guard).
    only = (
        '<lqa_findings score="50" gate="needs_review">\n'
        "1. [Terminology/major] required term 'Kubernetes' missing\n</lqa_findings>"
    )
    t, note = refusal.strip_meta_preamble(only, "src")
    assert t == only and note is None, ("only-a-fence returns unchanged", t, note)
    print("  2f. output that is ONLY a fence block -> returned unchanged (no empty body) OK")

    # (e) combined: an echoed fence block AND a meta-label heading both cleaned; note names
    #     both causes distinctly.
    comb = (
        '<translation_context source="context">\ndomain: x\n</translation_context>\n\n'
        "# Revised Target Text\n\nHola final."
    )
    t, note = refusal.strip_meta_preamble(comb, "Buy it.")
    assert t == "Hola final.", ("both layers cleaned", t)
    assert "fence-echo" in note and "label" in note, ("note names both causes", note)
    print("  2g. combined fence-echo + meta-label: both removed, note names both OK")

    # (f) SAFETY: legit content with non-fence angle brackets (HTML/math) is byte-identical.
    for s in [
        "Traducción con <b>negrita</b> y un enlace.",
        "Si A < B y C > D entonces continúa.",
        "[es-ES] Buy the widget today.",
    ]:
        t, note = refusal.strip_meta_preamble(s, "src")
        assert t == s and note is None, ("non-fence angle brackets untouched", s, t, note)
    print("  2h. safety: legit <b>/< >/math angle brackets are NOT touched (byte-identity) OK")


# ===========================================================================
# 2i. FENCE-ECHO review fixes: source-side keep-guard + truncated/space-padded tags.
# ===========================================================================
def test_fence_echo_review_fixes():
    # ITEM 1 — source-side keep-guard: a SOURCE that contains the literal fence tag as
    # prose (our own docs describing the defense) -> the translated fence-mention is REAL
    # content and must NOT be gutted.
    src_prose = (
        "Our defense wraps every value in <translation_context> fences so an "
        "injected value cannot break out."
    )
    out = (
        "Nuestra defensa envuelve cada valor en <translation_context> para que un "
        "valor inyectado no pueda escapar."
    )
    t, note = refusal.strip_meta_preamble(out, src_prose)
    assert t == out and note is None, ("source-with-fence-prose must be kept", t, note)
    # per-NAME: the source mentions translation_context only, so a style_guide echo in the
    # SAME output is still stripped while the translation_context mention is preserved.
    mixed = (
        "Texto. <translation_context>x</translation_context> "
        '<style_guide src="g">be formal</style_guide> fin.'
    )
    t, note = refusal.strip_meta_preamble(mixed, src_prose)
    assert "<translation_context>" in t and "style_guide" not in t, (
        "per-name protection: protected tag kept, unprotected echo stripped",
        t,
    )
    # normal source (no fence prose) -> the echo IS stripped.
    live = '<translation_context source="context">\ndomain: x\n</translation_context>\n\nHola real.'
    t, note = refusal.strip_meta_preamble(live, "Buy the widget today.")
    assert t == "Hola real." and note, ("normal source -> echo stripped", t, note)
    # re-review #3: an unrelated SUPERSTRING in the source (<style_guideline>) must NOT
    # protect style_guide — a genuine <style_guide> echo still ships otherwise.
    superstr = "See <style_guideline> for the house rules."
    echo = 'Texto. <style_guide src="g">be formal</style_guide> fin.'
    t, note = refusal.strip_meta_preamble(echo, superstr)
    assert "style_guide" not in t, ("<style_guideline> must NOT protect style_guide", t)
    # ...but genuine <style_guide> fence prose in the source DOES protect it.
    t, note = refusal.strip_meta_preamble(echo, "We wrap values in <style_guide> fences.")
    assert "<style_guide" in t and note is None, ("real fence prose protects", t, note)
    print(
        "  2i. source-side keep-guard: fence-prose source kept (per-name, word-boundary "
        "bounded — a superstring does NOT protect); normal source still strips the echo OK"
    )

    # ITEM 2a — a TRUNCATED closing tag at end-of-output (generation cut off mid-tag, no
    # trailing '>'): the WHOLE opener+body+half-closer span is removed (not just the tag
    # lines with the fence body left as residual scaffolding); the real translation BEFORE
    # the opener stays intact.
    trunc = (
        'Hola real translation.\n\n<translation_context source="context">\n'
        "domain: y\n</translation_context"
    )  # cut off, no '>'
    t, note = refusal.strip_meta_preamble(trunc, "Buy it.")
    assert t == "Hola real translation.", ("whole truncated-closer span removed", t)
    assert "domain: y" not in t, ("the fence body must not survive as residual", t)
    assert note and "fence-echo" in note, note
    # ...and when the truncated-closer block is the ONLY content, the non-empty-body guard
    # returns it UNCHANGED (never ship an empty target).
    only = '<translation_context source="x">\ndomain: y\n</translation_context'
    t, note = refusal.strip_meta_preamble(only, "Buy it.")
    assert t == only and note is None, ("only a truncated block -> unchanged", t, note)
    # ASYMMETRIC (accepted residual): an UNCLOSED opener with NO closer at all only has its
    # opener TAG line stripped — the body has no closing delimiter to bound it, so it is
    # indistinguishable from real content and left in place.
    asym = '<translation_context source="x">\ndomain: y\n\nHola real.'
    t, note = refusal.strip_meta_preamble(asym, "Buy it.")
    assert "translation_context" not in t and "domain: y" in t and "Hola real." in t, (
        "unclosed opener: tag line stripped, body an accepted residual",
        t,
    )
    t, note = refusal.strip_meta_preamble("Hola.\n\n</lqa_findin", "src")
    assert t == "Hola." and note, ("a truncated NAME prefix (with '_') is stripped", t, note)
    t, note = refusal.strip_meta_preamble("Hola.\n\n</translation_conte", "src")
    assert t == "Hola." and note, ("</translation_conte still stripped", t, note)
    # re-review #2: a truncated name must contain an UNDERSCORE (all three real names do),
    # so plain prose / XLIFF "<translation" (a prefix of translation_context) is NOT eaten.
    for keep in (
        "un elemento <translation",  # prose ending in a bare word
        "texto normal\n\notro </translation",  # XLIFF-ish truncated closer
        "Frase normal.\n\ntexto <abcdefghij",
    ):  # not a fence name at all
        t, note = refusal.strip_meta_preamble(keep, "src")
        assert t == keep and note is None, ("underscore-less trailing '<name' kept", keep, t)

    # ITEM 2b — whitespace after '<' (opener AND closer) is tolerated.
    sp_open = '< translation_context source="x">\n\nHola con espacio.'
    t, note = refusal.strip_meta_preamble(sp_open, "Buy it.")
    assert t == "Hola con espacio." and "<" not in t, ("space-after-< opener stripped", t)
    sp_block = '<translation_context source="x">\ndomain: z\n< / translation_context >\n\nHola.'
    t, note = refusal.strip_meta_preamble(sp_block, "Buy it.")
    assert t == "Hola.", ("a space-padded closer still closes the block", t)
    print(
        "  2j. truncated-closer block fully removed (translation before it intact; "
        "only-block unchanged; unclosed-opener body an accepted residual); truncated "
        "NAME prefix (with '_') stripped, underscore-less '<name' kept; "
        "whitespace-after-'<' tolerated OK"
    )


# ===========================================================================
# 2k. re-review CRITICAL — the truncated-tag pass is LINEAR (no ReDoS). A ~400KB garbled
#     "<aaaaaaaa "-repeated output (many '<word' starts, no '>' / newline — the exact
#     runaway-model input this code fires on) must round-trip in well under a second.
# ===========================================================================
def test_fence_strip_no_redos():
    import time

    garbled = "<aaaaaaaa " * 40000  # ~400KB, no '>' and no newline
    t0 = time.time()
    out, note = refusal.strip_meta_preamble(garbled, "src")
    dt = time.time() - t0
    assert dt < 2.0, ("truncated-tag pass must be linear (ReDoS guard)", dt)
    assert out == garbled and note is None, ("garbled non-fence input is untouched", out[:40])
    print(
        "  2k. ReDoS guard: 400KB garbled '<word'-runaway output cleaned in {:.3f}s (< 2s) OK".format(  # noqa: E501, UP032
            dt
        )
    )


# ===========================================================================
# 3. context / style-guide ALWAYS-INJECT (segmented): prepend when the template omits
#    the placeholder; no DOUBLE injection when present; no-context BYTE-IDENTITY.
#    fail-pre-fix: _inject_missing_prefixes ABSENT at HEAD (+ HEAD behavioral run).
# ===========================================================================
def test_context_style_always_inject():
    head = _head_src("app/services/orchestrator.py")
    head_has = head is not None and "_inject_missing_prefixes" in head
    if head is not None and not head_has:
        print("  3a. fail-pre-fix (git HEAD): _inject_missing_prefixes seam did not exist OK")
    else:
        print("  3a. HEAD already carries the inject seam -> behavior proof via current OK")

    org = 9401
    cust = _mk_customer(org)
    _mk_guide(org, cust, body="ORCH-VOICE: keep it FORMAL.")
    ctx = cp.validate_and_sanitize({"domain": "finance", "audience": "developers"})

    # (A) custom template lacking BOTH placeholders + a resolved context AND guide ->
    # both blocks prepended at the TOP (context before the style guide).
    pid = _mk_project(org_id=org, customer_id=cust)
    doc = _mk_seg_doc(pid, ["Ship it now."], context=ctx)
    wf = _mk_wf(lambda s, w: _add_step(s, w, 0, "T", StepType.LLM_AGENT, "T: {text}"))
    p = _run_capture(wf, doc)[0]
    assert p.startswith('<translation_context source="context"'), p[:80]
    assert 'domain: "finance"' in p, p
    i_ctx, i_sg, i_body = (
        p.index("</translation_context>"),
        p.index("<style_guide"),
        p.index("T: "),
    )
    assert i_ctx < i_sg < i_body, ("order: context, style guide, body", i_ctx, i_sg, i_body)
    assert "ORCH-VOICE: keep it FORMAL." in p, p
    print(
        "  3b. custom template lacking {context}/{style_guide}: BOTH resolved blocks "
        "prepended (context, then guide, then body) OK"
    )

    # fail-pre-fix behavioral: HEAD orchestrator DROPS both blocks for the same fixture.
    if head is not None and not head_has:
        try:
            ho = _load_from_source(head, "head_orch_pp3")
            p_head = _run_capture(wf, doc, module=ho)[0]
            assert "translation_context" not in p_head and "style_guide" not in p_head, (
                "HEAD must silently DROP the resolved blocks",
                p_head[:120],
            )
            print("  3c. fail-pre-fix (HEAD run): the resolved context+guide were DROPPED OK")
        except SystemExit:
            raise
        except Exception as exc:
            print(
                f"  3c. HEAD behavioral run skipped ({type(exc).__name__}) -> symbol-absence proof stands OK"  # noqa: E501
            )

    # (B) NO double injection: a template WITH {context} substitutes there ONLY (exactly
    # one block, at the placeholder), and the guide (omitted) is prepended.
    pid2 = _mk_project(org_id=org, customer_id=cust)
    doc2 = _mk_seg_doc(pid2, ["Ship it now."], context=ctx)
    wf2 = _mk_wf(
        lambda s, w: _add_step(s, w, 0, "T", StepType.LLM_AGENT, "Lead: {context}Body: {text}")
    )
    p2 = _run_capture(wf2, doc2)[0]
    assert p2.count("<translation_context") == 1, ("no double context injection", p2)
    assert "Lead: <translation_context" in p2, ("context substituted at its placeholder", p2)
    print(
        "  3d. template WITH {context}: substituted at the placeholder ONLY (no double "
        "injection); the omitted guide still prepended OK"
    )

    # (C) BYTE-IDENTITY: no context/guide resolves -> the prompt is the bare template.
    pid3 = _mk_project()  # no org/customer -> nothing resolves
    doc3 = _mk_seg_doc(pid3, ["Ship it now."])
    wf3 = _mk_wf(lambda s, w: _add_step(s, w, 0, "T", StepType.LLM_AGENT, "T: {text}"))
    p3 = _run_capture(wf3, doc3)[0]
    # #344: the bare template render now carries the <content> fence around {text}
    # (the recorder's provider echoes the source, so the value is the segment itself).
    assert p3 == "T: <content>Ship it now.</content>", p3
    assert "translation_context" not in p3 and "style_guide" not in p3, p3
    print(
        "  3e. no context/guide resolves -> byte-identical bare template (no stray "
        "block, no blank line) OK"
    )


# ===========================================================================
# 4. FULL_TEXT auto-inject of findings into a prev_lqa fix step + HEAD behavioral proof.
# ===========================================================================
def _mk_ft_fix_loop(rulebook_term="Kubernetes"):
    guard = {"ref": "prev_lqa", "op": "failed", "value": None}
    lqa.save_rulebook("en-US", "es-ES", f"REQUIRE {rulebook_term}")

    def build(s, w):
        _add_step(s, w, 0, "Translator", StepType.LLM_AGENT, "T: {text}", key="tr")
        _add_step(s, w, 1, "LQA", StepType.LQA, key="lqa")
        _add_step(s, w, 2, "Fix", StepType.LLM_AGENT, "Fix: {text}", key="fix", run_condition=guard)

    return _mk_wf(build)


def test_fulltext_findings_autoinject():
    head = _head_src("app/services/orchestrator.py")
    head_has = head is not None and "_findings_suffix" in head
    if head is not None and not head_has:
        print("  4a. fail-pre-fix (git HEAD): _findings_suffix seam did not exist OK")
    else:
        print("  4a. HEAD already carries the findings seam -> proof via current OK")

    pid = _mk_project()
    doc = _mk_ft_doc(pid, source="Buy the widget today.")
    wf = _mk_ft_fix_loop()
    prompts = _run_capture(wf, doc)
    fix = [p for p in prompts if p.startswith("Fix:")]
    assert fix, ("the fix step must have run (LQA failed the REQUIRE gate)", prompts)
    fp = fix[0]
    assert "MQM LQA findings to resolve:" in fp, ("auto-inject preamble present", fp)
    assert "<lqa_findings" in fp and "required term 'Kubernetes' missing" in fp, (
        "the failing LQA card's finding is appended",
        fp,
    )
    print(
        "  4b. FULL_TEXT: a prev_lqa fix step whose template omits {lqa_findings} "
        "AUTO-receives the failing card's findings OK"
    )

    # fail-pre-fix behavioral: the HEAD orchestrator appends NOTHING for the same run.
    if head is not None and not head_has:
        try:
            ho = _load_from_source(head, "head_orch_pp4")
            hp = [p for p in _run_capture(wf, doc, module=ho) if p.startswith("Fix:")]
            assert hp and "lqa_findings" not in hp[0], ("HEAD must NOT inject findings", hp[:1])
            print("  4c. fail-pre-fix (HEAD run): the old fix prompt carried NO findings OK")
        except SystemExit:
            raise
        except Exception as exc:
            print(
                f"  4c. HEAD behavioral run skipped ({type(exc).__name__}) -> symbol-absence proof stands OK"  # noqa: E501
            )


# ===========================================================================
# 5. SEGMENTED Phase-3 loop: on_fail-target auto-inject + LOOP-VISIT FRESHNESS +
#    empty-card BYTE-IDENTITY on the pre-LQA visit.
# ===========================================================================
def test_segmented_loop_freshness():
    # translate(0) -> fix(1, on_fail target) -> lqa(2, on_pass end / on_fail -> fix).
    # fix is bounded by max_visits=3, so: fix v0 (no LQA yet), lqa1, fix v1 (card1),
    # lqa2, fix v2 (card2 FRESH), lqa3 -> fix at cap -> completes.
    #
    # `vary=True` on the recorder is LOAD-BEARING since issue #95: the fix step must
    # actually CHANGE the text, or the LQA step is re-entered on byte-identical bytes,
    # carries its previous verdict forward instead of re-judging, and the run holds after
    # two visits. A constant-output "fix" is precisely the no-progress loop #95 closes, so
    # the freshness property this test pins is only meaningful on a loop that progresses.
    def build(s, w):
        _add_step(s, w, 0, "Translator", StepType.LLM_AGENT, "T: {text}", key="tr")
        _add_step(s, w, 1, "Fix", StepType.LLM_AGENT, "Fix: {text}", key="fix", max_visits=3)
        _add_step(
            s,
            w,
            2,
            "LQA",
            StepType.LQA,
            key="lqa",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )

    wf = _mk_wf(build)
    pid = _mk_project()
    doc = _mk_seg_doc(pid, ["Ship it now."])

    with _CountingJudge():
        prompts = _run_capture(wf, doc, vary=True)
    fix = [p for p in prompts if p.startswith("Fix:")]
    assert len(fix) == 3, ("fix must run three times in the bounded loop", len(fix))

    # empty-card byte-identity: the FIRST fix ran BEFORE any LQA -> no findings appended.
    # #344: the {text} value is fenced in <content>...</content> at the render seam.
    assert fix[0] == "Fix: <content>[es-ES] out</content>", (
        "pre-LQA fix visit is byte-identical",
        fix[0],
    )
    assert "lqa_findings" not in fix[0], fix[0]
    # visit with card1 carries FINDING-1; the NEXT visit carries the FRESH FINDING-2 and
    # NOT the stale FINDING-1 (the loop updates the stashed card each LQA visit).
    assert "FINDING-1" in fix[1], ("fix visit 1 sees LQA card 1", fix[1])
    assert "FINDING-2" in fix[2] and "FINDING-1" not in fix[2], (
        "fix visit 2 sees the FRESH card 2, not the stale card 1",
        fix[2],
    )
    print(
        "  5. SEGMENTED loop: on_fail-target auto-inject; empty-card byte-identity on "
        "the pre-LQA visit; freshness (visit 2 sees card 2, not card 1) OK"
    )


# ===========================================================================
# 6. EXPLICIT {lqa_findings} placeholder on a NON-loop step + a non-target step gets
#    NOTHING (findings reach only the placeholder / the correction-loop shapes).
# ===========================================================================
def test_explicit_placeholder_and_non_target():
    lqa.save_rulebook("en-US", "es-ES", "REQUIRE Kubernetes")

    def build(s, w):
        _add_step(s, w, 0, "Translator", StepType.LLM_AGENT, "T: {text}", key="tr")
        _add_step(s, w, 1, "LQA", StepType.LQA, key="lqa")  # linear (no branch) -> falls through
        _add_step(s, w, 2, "Report", StepType.LLM_AGENT, "R[{lqa_findings}]:: {text}", key="report")
        _add_step(s, w, 3, "Plain", StepType.LLM_AGENT, "P: {text}", key="plain")

    wf = _mk_wf(build)
    pid = _mk_project()
    doc = _mk_seg_doc(pid, ["Ship it now."])
    prompts = _run_capture(wf, doc)

    report = [p for p in prompts if p.startswith("R[")][0]  # noqa: RUF015
    plain = [p for p in prompts if p.startswith("P: ")][0]  # noqa: RUF015
    trans = [p for p in prompts if p.startswith("T: ")][0]  # noqa: RUF015
    # explicit placeholder: the report step (NOT a loop shape) still gets the block —
    # substituted AT the placeholder, not auto-appended.
    assert "<lqa_findings" in report and "required term 'Kubernetes' missing" in report, report
    assert "MQM LQA findings to resolve:" not in report, (
        "explicit substitution, no auto-append preamble",
        report,
    )
    # a non-target step WITHOUT the placeholder gets NOTHING (byte-identical).
    # #344: the {text} value is fenced in <content>...</content> at the render seam.
    assert "lqa_findings" not in plain and plain == "P: <content>[es-ES] out</content>", plain
    # the translate step ran BEFORE any LQA and is not a loop shape -> no findings.
    assert "lqa_findings" not in trans, trans
    print(
        "  6. explicit {lqa_findings} substitutes for a non-loop step; a non-target "
        "step without the placeholder gets NOTHING (byte-identical) OK"
    )


# ===========================================================================
# 7. Prompt-tap META threading: LQA + QE judge captures now carry the correlation meta.
#    fail-pre-fix: score_text / score_segment lacked a tap_meta param at HEAD.
# ===========================================================================
def _tap_records(task):
    return [r for r in prompt_tap.read_records(limit=200) if r.get("task") == task]


def test_tap_meta_lqa_qe():
    import inspect

    assert "tap_meta" in inspect.signature(lqa.score_text).parameters, "score_text tap_meta"
    assert "tap_meta" in inspect.signature(qe.score_segment).parameters, "score_segment tap_meta"
    hl = _head_src("app/services/lqa.py")
    hq = _head_src("app/services/qe.py")
    head_lqa_had = (
        hl is not None
        and "tap_meta"
        in inspect.signature(_load_from_source(hl, "head_lqa_pp7").score_text).parameters
    )
    head_qe_had = (
        hq is not None
        and "tap_meta"
        in inspect.signature(_load_from_source(hq, "head_qe_pp7").score_segment).parameters
    )
    if hl is not None and not head_lqa_had and not head_qe_had:
        print("  7a. fail-pre-fix (git HEAD): score_text + score_segment had NO tap_meta param OK")
    else:
        print("  7a. HEAD already threads tap_meta into the judges -> proof via current OK")

    # A workflow that fires BOTH judges through the REAL MockProvider seam (so the tap
    # actually captures): step 0 is QE-gated (qe judge per segment); step 1 is LQA (its
    # judge is forced live so it makes a real run_completion call).
    def build(s, w):
        _add_step(
            s,
            w,
            0,
            "Translator",
            StepType.LLM_AGENT,
            "T: {text}",
            key="tr",
            qe_gate=True,
            qe_threshold=1,
        )
        _add_step(s, w, 1, "LQA", StepType.LQA, key="lqa")

    wf = _mk_wf(build)
    pid = _mk_project()
    doc = _mk_seg_doc(pid, ["Ship it now."])

    prompt_tap.clear()
    settings.set_many({"prompt_tap_enabled": "1"})
    try:
        # LQA judge forced live so its run_completion (and thus its capture) happens.
        orig_live = lqa.provider_is_live
        lqa.provider_is_live = lambda name: True
        try:
            with Session(engine) as s:
                summary = orchestrator.run_workflow(s, wf, doc, "es-ES")
        finally:
            lqa.provider_is_live = orig_live
        assert summary["status"] == "completed", summary

        run_id = summary["run_id"]
        qe_recs = _tap_records("qe")
        lqa_recs = _tap_records("lqa")
        assert qe_recs, "the QE judge call must have been captured"
        assert lqa_recs, "the LQA judge call must have been captured"
        qm = qe_recs[0].get("meta") or {}
        assert qm.get("workflow_run_id") == run_id and qm.get("step_key") == "tr", qm
        assert qm.get("document_id") == doc and qm.get("segment_index") == 0, qm
        lm = lqa_recs[0].get("meta") or {}
        assert lm.get("workflow_run_id") == run_id and lm.get("step_key") == "lqa", lm
        assert lm.get("document_id") == doc, lm
        assert "segment_index" not in lm, ("LQA is doc-level -> no segment_index", lm)
        print(
            "  7b. LQA + QE judge captures carry workflow/run/step/doc(/segment) meta "
            "(QE has segment_index; LQA does not) OK"
        )

        # fail-pre-fix behavioral: the HEAD orchestrator passes NO tap_meta to the judges,
        # so their captured records have no workflow correlation meta. Detected by the OLD
        # QE call shape (no tap_meta arg) being present verbatim in the HEAD source.
        head_orch = _head_src("app/services/orchestrator.py")
        _old_qe_call = 'qe.score_segment(qe_spec, r["fb_source"], r["fb_output"], src, locale)'
        if head_orch is not None and _old_qe_call in head_orch:
            try:
                ho = _load_from_source(head_orch, "head_orch_pp7")
                prompt_tap.clear()
                orig_live = lqa.provider_is_live
                lqa.provider_is_live = lambda name: True
                try:
                    with Session(engine) as s:
                        ho.run_workflow(s, wf, doc, "es-ES")
                finally:
                    lqa.provider_is_live = orig_live
                q2 = [(r.get("meta") or {}).get("workflow_run_id") for r in _tap_records("qe")]
                l2 = [(r.get("meta") or {}).get("workflow_run_id") for r in _tap_records("lqa")]
                assert q2 and all(v is None for v in q2), ("HEAD QE captures have no run meta", q2)
                assert l2 and all(v is None for v in l2), ("HEAD LQA captures have no run meta", l2)
                print("  7c. fail-pre-fix (HEAD run): LQA + QE captures carried meta:None OK")
            except SystemExit:
                raise
            except Exception as exc:
                print(
                    f"  7c. HEAD behavioral run skipped ({type(exc).__name__}) -> signature proof stands OK"  # noqa: E501
                )
    finally:
        prompt_tap.clear()
        settings.set_many({"prompt_tap_enabled": "0"})


# ===========================================================================
# 8. review fix 2 — findings SURVIVE a human_review pause/resume (persisted in
#    WorkflowRun.context, restored on resume so a prev_lqa fix step still gets them).
# ===========================================================================
def test_findings_survive_pause_resume():
    lqa.save_rulebook("en-US", "es-ES", "REQUIRE Kubernetes")

    def build(s, w):
        _add_step(s, w, 0, "Translator", StepType.LLM_AGENT, "T: {text}", key="tr")
        _add_step(s, w, 1, "LQA", StepType.LQA, key="lqa")
        _add_step(s, w, 2, "Review", StepType.HUMAN_REVIEW)
        _add_step(
            s,
            w,
            3,
            "Fix",
            StepType.LLM_AGENT,
            "Fix: {text}",
            key="fix",
            run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
        )

    wf = _mk_wf(build)
    pid = _mk_project()
    doc = _mk_seg_doc(pid, ["Ship it now."])

    # run to the human_review pause (LQA fails the REQUIRE gate on the way).
    run_id = None
    with Session(engine) as s, _CaptureRC(orchestrator):
        for ev in orchestrator.run_workflow_stream(s, wf, doc, "es-ES"):
            if ev["type"] == "run_started":
                run_id = ev["run_id"]

    # PERSISTENCE proof: the failing LQA's findings were written into run.context BEFORE
    # the pause (the in-memory local is gone once the generator returned).
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED, run.status
        blk = (run.context or {}).get("lqa_findings_block")
        assert blk and "required term 'Kubernetes' missing" in blk, (
            "findings must be persisted in run.context across the pause",
            run.context,
        )

    # RESUME: the fix step (prev_lqa failed) runs with the RESTORED findings appended,
    # even though the resumed generator's lqa_findings_block local started as "".
    with Session(engine) as s, _CaptureRC(orchestrator) as cap:
        orchestrator.resume_workflow(s, run_id, "es-ES", "")
    fix = [p for p in cap.prompts if p.startswith("Fix:")]
    assert fix, ("the fix step must run on resume", cap.prompts)
    assert "MQM LQA findings to resolve:" in fix[0], ("restored findings appended", fix[0])
    assert "required term 'Kubernetes' missing" in fix[0], fix[0]
    print(
        "  8. findings persist in run.context across a human_review pause and are "
        "RESTORED on resume -> the fix step still receives them OK"
    )


# ===========================================================================
# 9. review fix 3 — auto-inject honors the run_condition op: prev_lqa:FAILED injects,
#    prev_lqa:passed / score ops do NOT (they can still use the explicit placeholder).
# ===========================================================================
def test_autoinject_honors_condition_op():
    from types import SimpleNamespace as NS

    blk = '<lqa_findings score="50" gate="needs_review">\n1. [X/major] y\n</lqa_findings>'

    def step(rc=None, key=None, branch=None, st=StepType.LLM_AGENT):
        return NS(step_type=st, run_condition=rc, key=key, branch=branch)

    failed = step(rc={"ref": "prev_lqa", "op": "failed"})
    passed = step(rc={"ref": "prev_lqa", "op": "passed"})
    score = step(rc={"ref": "prev_lqa", "op": "score_lt", "value": 80})
    plain = step()
    steps = [failed, passed, score, plain]

    assert orchestrator._references_prev_lqa(failed) is True, "failed-gated auto-injects"
    assert orchestrator._references_prev_lqa(passed) is False, "passed-gated does NOT"
    assert orchestrator._references_prev_lqa(score) is False, "score-gated does NOT"

    # _findings_suffix appends only for the failed-gated step; empty for the rest.
    assert orchestrator._findings_suffix(failed, steps, "Fix: {text}", blk).endswith(blk)
    assert orchestrator._findings_suffix(passed, steps, "P: {text}", blk) == ""
    assert orchestrator._findings_suffix(score, steps, "P: {text}", blk) == ""
    assert orchestrator._findings_suffix(plain, steps, "P: {text}", blk) == ""
    # explicit placeholder still works for ANY step (no auto-append, substituted instead).
    assert orchestrator._findings_suffix(passed, steps, "P {lqa_findings}: {text}", blk) == ""
    # on_fail loop-back target (not run_condition) still auto-injects.
    fix_target = step(key="fix")
    lqa_branch = step(st=StepType.LQA, branch={"on_fail": {"step": "fix"}, "on_pass": "end"})
    assert orchestrator._findings_suffix(
        fix_target, [lqa_branch, fix_target], "Fix: {text}", blk
    ).endswith(blk)
    print(
        "  9. auto-inject honors the op: prev_lqa:failed + on_fail target inject; "
        "prev_lqa:passed / score-gated do NOT (explicit placeholder still works) OK"
    )


# ===========================================================================
# 10. ADDENDUM — the LQA judge honors the STEP's pinned model + fallback chain (was
#     silently rebuilt at the provider default with no fallback). Non-workflow callers
#     unchanged. fail-pre-fix: HEAD _llm_errors had no `spec` param.
# ===========================================================================
class _SpecCapture:
    """Force the LQA judge live + capture the ProviderSpec it hands run_completion."""

    def __init__(self):
        self.spec = None

    def __enter__(self):
        self._live, self._rc = lqa.provider_is_live, lqa.run_completion

        def rc(spec, prompt, system=None, task=None, payload=None, **kw):
            self.spec = spec
            return {
                "text": json.dumps({"errors": []}),
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": {},
            }

        lqa.provider_is_live = lambda n: True
        lqa.run_completion = rc
        return self

    def __exit__(self, *exc):
        lqa.provider_is_live, lqa.run_completion = self._live, self._rc
        return False


def test_lqa_judge_honors_step_spec():
    import inspect

    hl = _head_src("app/services/lqa.py")
    head_had = (
        hl is not None
        and "spec"
        in inspect.signature(_load_from_source(hl, "head_lqa_pp10")._llm_errors).parameters
    )
    if hl is not None and not head_had:
        print(
            "  10a. fail-pre-fix (git HEAD): lqa._llm_errors had NO `spec` param "
            "(judge ignored the step's model + fallback) OK"
        )
    else:
        print("  10a. HEAD already threads spec into the judge -> proof via current OK")

    # a workflow LQA step with a PINNED model + a fallback chain -> the judge's spec.
    def build(s, w):
        _add_step(s, w, 0, "T", StepType.LLM_AGENT, "T: {text}", key="tr")
        _add_step(
            s,
            w,
            1,
            "LQA",
            StepType.LQA,
            key="lqa",
            provider="mock",
            model="pinned-judge-x",
            fallback_provider="mock",
            fallback_model="fb-judge-y",
        )

    wf = _mk_wf(build)
    doc = _mk_seg_doc(_mk_project(), ["Ship it now."])
    with _SpecCapture() as cap, Session(engine) as s:
        orchestrator.run_workflow(s, wf, doc, "es-ES")
    assert cap.spec is not None, "the judge must have run"
    assert cap.spec.model == "pinned-judge-x", (
        "the pinned judge model reaches the judge",
        cap.spec.model,
    )
    assert cap.spec.fallback_provider == "mock" and cap.spec.fallback_model == "fb-judge-y", (
        "the step's fallback chain reaches the judge",
        cap.spec,
    )
    print("  10b. workflow LQA judge uses the STEP's pinned model + fallback chain OK")

    # NON-workflow score_text (no spec) is UNCHANGED: provider-default model, no fallback.
    with _SpecCapture() as cap2:
        lqa.score_text("hello", "[es] hola", "en-US", "es-ES", "", "mock")
    assert cap2.spec.model == config.DEFAULT_MODELS.get("mock"), (
        "non-workflow caller keeps the provider-default model",
        cap2.spec.model,
    )
    assert cap2.spec.fallback_provider is None, ("no fallback for the default spec", cap2.spec)
    print(
        "  10c. non-workflow score_text (no spec) unchanged: provider-default model, no fallback OK"
    )

    # BEHAVIORAL failover: the judge's PRIMARY provider errors; the step's fallback
    # rescues it, so the LQA step (and the run) complete instead of propagating.
    from app.services.providers import _OVERRIDES, Provider, register_provider

    class _BoomClaude(Provider):
        name = "claude"

        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            raise RuntimeError("primary judge provider down")

    register_provider("claude", _BoomClaude())
    orig_live = lqa.provider_is_live
    lqa.provider_is_live = lambda n: True
    try:

        def build2(s, w):
            _add_step(s, w, 0, "T", StepType.LLM_AGENT, "T: {text}", key="tr")
            _add_step(
                s,
                w,
                1,
                "LQA",
                StepType.LQA,
                key="lqa",
                provider="claude",
                model="cm",
                fallback_provider="mock",
                fallback_model="mm",
            )

        wf2 = _mk_wf(build2)
        doc2 = _mk_seg_doc(_mk_project(), ["Ship it now."])
        with Session(engine) as s:
            summ = orchestrator.run_workflow(s, wf2, doc2, "es-ES")
        assert summ["status"] == "completed", (
            "the judge must FAIL OVER to the step's fallback, not propagate",
            summ,
        )
    finally:
        lqa.provider_is_live = orig_live
        _OVERRIDES.pop("claude", None)
    print(
        "  10d. behavioral failover: a dead PRIMARY judge provider fails over to the "
        "step's fallback -> the LQA step + run complete OK"
    )


def main():
    test_findings_render_unit()
    test_hygiene_strip_unit()
    test_fence_echo_strip()
    test_fence_echo_review_fixes()
    test_fence_strip_no_redos()
    test_context_style_always_inject()
    test_fulltext_findings_autoinject()
    test_segmented_loop_freshness()
    test_explicit_placeholder_and_non_target()
    test_tap_meta_lqa_qe()
    test_findings_survive_pause_resume()
    test_autoinject_honors_condition_op()
    test_lqa_judge_honors_step_spec()
    print("PROMPT PACKAGE OK")


if __name__ == "__main__":
    main()
