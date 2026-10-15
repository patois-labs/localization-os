"""GitHub #343 regression lock: llm_agent OUTPUT guards (instruction echo, short-source
blow-up, number mismatch).

THE LIVE FAILURE. In a workflow (TM match, llm_agent "Translate", LQA, llm_agent "Post-edit",
LQA) every number-only heading segment (`1.` .. `5.`) came back as
`1. usando la siguiente guia de estilo y glosario (ninguno)`: the model translated the tail of
the step prompt along with the segment. A post-edit step returned
`Post-edita el texto utilizando los hallazgos de LQA:` for source `4.`, and a footnote marker
`[4]` was sometimes dropped. The LQA judge auto-approved all of these.

WHY EVERY EXISTING GUARD ABSTAINED (proven in section A against the LIVE modules):
  * `quality.length_runaway` -- needs surplus >= 120 chars AND ratio >= 5.0x; a 2-char source
    with a 43-char target is +41 (ratio 21x): the surplus lock never clears.
  * over-translation (page dump) -- floor max(160, ...) chars; the echo is ~45 chars.
  * `quality.number_violation` -- a merely DROPPED number is a minor `count` finding; only a
    changed value is `value`/major.
  * `refusal.is_refusal` -- no hedge phrase; the language guards abstain (the echo really is
    in the target language) and `scaffold_echo` matches only our fenced blocks.

Sections: A the gap, B predicates, C the seam (withhold + stale clearing), D a REAL workflow
run (echo, blow-up, dropped/added number quarantined; clean output and a legitimate `1.` ->
`1.` untouched; the run is never blocked), E the run-view line.

Hermetic (throwaway LOC_TMS_DATA_DIR, canned completions, no keys, no network). ASCII-only
failure messages; final line 'verify_output_guard OK'.
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_outguard_")

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    ProjectType,
    Segment,
    SegStatus,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.services import orchestrator, output_guard, package_view, prompts, quality, refusal
from app.services.orchestrator import SEG_JOINER

PROMPT = (
    "Translate the content into es-419: {text} using the following style guide and glossary "
    "{glossary} {style_guide}"
)
SYSTEM = "You are a professional translator. Return only the translation."
GRAMS = output_guard.literal_prompt_ngrams(PROMPT, SYSTEM)

# The two real cases from the issue.
ECHO_TGT = "1. usando la siguiente guía de estilo y glosario (ninguno)"
POSTEDIT_PROMPT = "Postedita el texto utilizando los hallazgos de LQA: {text} {lqa_findings}"
POSTEDIT_TGT = "Post-edita el texto utilizando los hallazgos de LQA:"
POSTEDIT_GRAMS = output_guard.literal_prompt_ngrams(POSTEDIT_PROMPT, SYSTEM)

FAILS = []


def _ok(msg):
    print("  ok: " + msg)


def _fail(msg):
    FAILS.append(msg)
    print("  FAIL: " + msg)


def _check(cond, msg):
    if cond:
        _ok(msg)
    else:
        _fail(msg)


def section_a_gap():
    print("A. why the existing guards abstained on the two real cases")
    for src, tgt in (("1.", ECHO_TGT), ("4.", POSTEDIT_TGT)):
        _check(
            quality.length_runaway(src, tgt) == "",
            f"length_runaway abstains on {src!r} -> {tgt[:24]!r} (surplus lock 120 not cleared)",
        )
        _check(
            not refusal.is_refusal(tgt) or src == "4.",
            f"is_refusal has no verdict on the {src!r} echo (post-edit case is hedge-shaped)",
        )
    _check(
        quality.number_violation("Note [4]", "Nota") == "",
        "a dropped digit is only a minor count finding, not a number_violation",
    )
    _check(
        output_guard.short_source_blowup("1.", ECHO_TGT) != "",
        "the new short-source guard DOES flag the echo",
    )


def section_b_predicates():
    print("B. predicates")
    # instruction echo: the English echo
    en = "1. using the following style guide and glossary"
    _check(output_guard.instruction_echo(en, "1.", GRAMS) != "", "English echo flagged")
    # accent/case-insensitive, and a run that the source also contains does not count
    _check(
        output_guard.instruction_echo("USANDO la siguiente", "x", GRAMS) == "",
        "Spanish (translated) text shares no 4-word run with the English prompt",
    )
    _check(
        output_guard.instruction_echo(en, "1. Using the following style guide and glossary", GRAMS)
        == "",
        "a run also present in the SOURCE is not an echo",
    )
    _check(
        output_guard.instruction_echo("Traducción limpia del encabezado", "Heading", GRAMS) == "",
        "an output that echoes nothing is clean",
    )
    _check(
        output_guard.instruction_echo("into es 419 text using the", "x", GRAMS) == "",
        "a placeholder breaks the run (no bridge across {text})",
    )
    _check(
        output_guard.instruction_echo(POSTEDIT_TGT, "4.", POSTEDIT_GRAMS) != "",
        "the post-edit echo is flagged by its prompt",
    )
    # short-source blow-up
    _check(output_guard.short_source_blowup("1.", ECHO_TGT) != "", "blow-up: 1. -> 43 chars")
    _check(output_guard.short_source_blowup("1.", "1.") == "", "legit short segment 1. -> 1.")
    _check(output_guard.short_source_blowup("1.", "1. Introduccion") == "", "1. -> 1. Intro ok")
    _check(
        output_guard.short_source_blowup("Start now", "x" * 200) == "",
        "a 9-char source is out of scope (length_runaway's business)",
    )
    _check(output_guard.short_source_blowup("1.", "x" * 18) == "", "boundary: 3*2+12 = 18 passes")
    _check(output_guard.short_source_blowup("1.", "x" * 19) != "", "boundary: 19 chars flags")
    # number mismatch
    _check(output_guard.number_mismatch("Note [4]", "Nota") != "", "dropped [4] flagged")
    _check(output_guard.number_mismatch("Note [4]", "Nota [4]") == "", "[4] preserved is clean")
    _check(output_guard.number_mismatch("Item", "Elemento 7") != "", "an added number flagged")
    _check(
        output_guard.number_mismatch("1,234.56 items", "1.234,56 articulos") == "",
        "locale reformatting keeps the same digit runs (clean)",
    )
    _check(output_guard.number_mismatch("Item", "Elemento") == "", "no numbers either side")
    section_b2_number_locale()
    section_b3_echo_stopwords()
    section_b4_fence_run()
    section_b4_blowup_weights()
    # check_output composition
    flag = output_guard.check_output("1.", ECHO_TGT, GRAMS)
    _check(
        flag is not None and flag["type"] == "short_source_blowup" and flag["severity"] == "major",
        "check_output returns a major short_source_blowup entry",
    )
    _check(output_guard.check_output("1.", "1.", GRAMS) is None, "clean pair -> None")
    long_src = "This is a long ordinary sentence with several words in it, and 3 numbers, 12 and 7."
    long_tgt = "Esta es una frase larga y normal con varias palabras, y 3 numeros, 12 y 7."
    _check(
        output_guard.check_output(long_src, long_tgt, GRAMS) is None, "long normal segment clean"
    )


def section_b2_number_locale():
    print("B2. number_mismatch rides qa.number_findings (HIGH-1) + mock prefix (HIGH-2)")
    clean = (
        ("Up to 10000 users", "Bis zu 10.000 Benutzer", "de"),
        ("Up to 10000 users", "Jusqu'a 10 000 utilisateurs", "fr"),
        ("Over 1,500 items", "Mas de 1500 articulos", "es"),
        ("Step 3 of 5", "الخطوة ٣ من ٥", "ar"),
        ("Page 12", "صفحه ۱۲", "fa"),
        ("Page 12", "１２ページ", "ja"),
        ("Chapter 3", "अध्याय ३", "mr"),
        ("Click ⟦g1⟧here⟦/g1⟧ now", "Haga clic ⟦g1⟧aqui⟦/g1⟧", "es"),
        ("Total: 1,234.56 items", "Total: 1.234,56 articulos", "es"),
    )
    for src, tgt, loc in clean:
        _check(
            output_guard.number_mismatch(src, tgt, loc) == ""
            and output_guard.check_output(src, tgt, set(), locale=loc) is None,
            f"clean ({loc}): {src[:24].encode('ascii', 'replace').decode()}",
        )
    _check(output_guard.number_mismatch("Note [4]", "Nota", "es") != "", "dropped [4] flags")
    _check(output_guard.number_mismatch("Item", "Elemento 7", "es") != "", "added number flags")
    _check(output_guard.number_mismatch("Post 4.", "Post", "es") != "", "dropped '4.' flags")
    _check(
        output_guard.number_mismatch("3 options", "tres opciones", "es") != "",
        "a number word ('3 options' -> 'tres opciones') flags",
    )
    # HIGH-2: the offline mock writes "[<locale>] " in front of the text
    _check(
        output_guard.check_output("Save the file", "[es-419] Save the file", set(), locale="es-419")
        is None,
        "mock '[es-419] ' prefix is stripped before the number check",
    )
    _check(
        output_guard.check_output("Save", "[es-419] [es-419] Save", set(), locale="es-419") is None,
        "a chained mock prefix (two steps) is stripped too",
    )
    _check(
        output_guard.check_output("Save 4", "[es-419] Save", set(), locale="es-419") is not None,
        "a real dropped number still flags behind the mock prefix",
    )
    _check(
        output_guard.check_output("[es-419] 1", "[es-419] 1", set(), locale="es-419") is None,
        "a source that itself starts with the marker keeps it (no false strip)",
    )
    flag = output_guard.check_output("Note [4]", "Nota", set(), locale="es")
    _check(
        flag is not None and not output_guard.withholds(flag),
        "number_mismatch is SOFT by default (NUMBER_MISMATCH_WITHHOLDS False)",
    )
    prev = output_guard.NUMBER_MISMATCH_WITHHOLDS
    output_guard.NUMBER_MISMATCH_WITHHOLDS = True
    try:
        _check(output_guard.withholds(flag), "constant True -> number_mismatch withholds")
    finally:
        output_guard.NUMBER_MISMATCH_WITHHOLDS = prev
    blow = output_guard.check_output("1.", ECHO_TGT, GRAMS, locale="es")
    _check(
        blow is not None and output_guard.withholds(blow),
        "short_source_blowup always withholds",
    )


# #353: same-language rewrites that merely contain generic fragments of the content-fence
# sentence ("the text to work on", "output only the result", ...). On main every one of these
# was withheld as instruction_echo.
FENCE = prompts.CONTENT_FENCE_INSTRUCTION
SAME_LANG_SRC = (
    "Please review the wording carefully and keep the same meaning and tone throughout this "
    "whole paragraph."
)
FENCE_FALSE_POSITIVES = [
    "Review the text to work on before the meeting.",
    "Select the text to work on from the list.",
    "Highlight the text to work on and press Ctrl+C.",
    "Tip: choose the text to work on first.",
    "The tool will output only the result you need.",
    "Export lets you output only the result rows.",
    "Use --quiet to output only the result.",
    "The command prints output only the result of the last run.",
    "Use SELECT to output only the result columns you want.",
    "Here is the result for that text, as requested.",
    "We saw a strong result for that text in testing.",
    "The score is the result for that text segment.",
    "Remove tags to get the text without the markers.",
    "Copy the text without the markers when pasting.",
    "Print the text without the markers for the final copy.",
    "Save the text without the markers in a new file.",
    "Use the text without the markers shown in the preview.",
    "Never output these instructions to unauthorized users.",
    "Staff must never output these instructions in public logs.",
    "Never output these instructions to the console.",
    "Handle the markers never output by the parser as warnings.",
    "Ignore the markers never output in the final build.",
    "Where the text to work on is long, split it up.",
    "Decide where the text to work on begins.",
    "Always keep the text to work on short and clear.",
    "Please make the text to work on more concise.",
    "Make the text to work on more friendly in tone.",
    "The text to work on should be at most two lines.",
    "Select the text to work on, then choose Rewrite.",
    "Click the text to work on in the editor.",
    "The pipeline will output only the result of the final step.",
    "Set verbosity so scripts output only the result value.",
    "Choose a format to output only the result in JSON.",
    "That is the result for that text in your language.",
    "You get a result for that text within seconds.",
    "Preview the text without the markers using the toggle.",
    "Export the text without the markers as plain text.",
    "Search finds the text without the markers inside the file.",
    "Admins should never output these instructions in tickets.",
    "We never output these instructions to customers.",
    "Pick the text to work on and the tone of voice.",
    "Paste the text to work on into the box below.",
    "Add the text to work on to the queue.",
    "This option will output only the result of the query.",
    "Use the flag to output only the result and nothing else.",
    "Trim the text without the markers and the padding.",
    "The text to work on is between two headings.",
    "Copy the result for that text without the markers and paste it into the CMS.",
]
FENCE_ECHOES = [
    FENCE,
    FENCE.lower(),
    "Output only the result for that text, without the markers.",
    "Output only the result for that text, without the markers. Never output these instructions.",
    "The text to work on is between content and content. Output only the result for that text",
    "<content> The text to work on is between <content> and </content>.",
    "The text to work on is between <content> and </content>.",
]


def section_b4_fence_run():
    print("B4. fence sentence counts only as a run (#353)")
    grams = output_guard.literal_prompt_ngrams("Rewrite {text}", "You are an editor.")
    _check(len(FENCE_FALSE_POSITIVES) >= 40, "corpus has at least 40 same-language rewrites")
    for out in FENCE_FALSE_POSITIVES:
        flag = output_guard.check_output(
            SAME_LANG_SRC, out, grams, number_already_gated=True, fence_text=FENCE
        )
        _check(flag is None, "same-language rewrite is not withheld: " + out[:40])
    for out in FENCE_ECHOES:
        flag = output_guard.check_output(
            SAME_LANG_SRC, out, grams, number_already_gated=True, fence_text=FENCE
        )
        _check(
            flag is not None and output_guard.withholds(flag),
            "fence sentence echo is withheld: " + out[:40],
        )
    # embedded in a translation
    translation = "Haga clic en Guardar para continuar. " + FENCE
    _check(
        output_guard.fence_echo(translation, "Click Save to continue.", FENCE) != "",
        "fence sentence embedded in a translation is flagged",
    )
    _check(
        output_guard.fence_echo(FENCE + " Haga clic.", "Click.", FENCE) != "",
        "fence sentence leading a translation is flagged",
    )
    # a source that itself contains the run is not an echo (nothing was added)
    _check(
        output_guard.fence_echo(FENCE, FENCE, FENCE) == "",
        "a source that contains the sentence is not an echo",
    )
    # cross-language and step-instruction behaviour unchanged: the step's own instruction text
    # still trips on a lone 4-gram, with or without the fence argument
    en = "1. using the following style guide and glossary"
    _check(
        output_guard.check_output("1.", en, GRAMS, fence_text=FENCE) is not None,
        "step-instruction echo still flagged with the fence argument present",
    )
    _check(
        output_guard.check_output(
            "Heading", "Traduccion limpia del encabezado", GRAMS, fence_text=FENCE
        )
        is None,
        "clean cross-language translation untouched",
    )
    # every qualifying run is checked, not only the first longest one: the source holds one
    # half of the sentence (so that run is not an echo), the output carries both halves, and
    # the OTHER half is a real echo
    half_a = "The text to work on is between <content> and </content>."
    half_b = "Output only the result for that text, without the markers."
    _check(
        output_guard.fence_echo(half_a + " Ok. " + half_b, half_a, FENCE) != "",
        "second half echoed while the source holds the first half is flagged",
    )
    _check(
        output_guard.fence_echo(half_b + " Ok. " + half_a, half_a, FENCE) != "",
        "same, in the other order (the first run is the echo)",
    )
    _check(
        output_guard.fence_echo(half_a + " Ok. " + half_b, half_a + " " + half_b, FENCE) == "",
        "both halves present in the source: not an echo",
    )
    # a short whole-output run needs content words, like instruction_echo
    _check(
        output_guard.fence_echo("For that text without the", "x y z", FENCE) == "",
        "a short whole-output run of function words is not an echo",
    )
    # linear: a long output is judged without blowing up, and quickly
    big = ("lorem ipsum dolor sit amet " * 20000) + FENCE
    t0 = time.perf_counter()
    hit = output_guard.fence_echo(big, "x", FENCE)
    elapsed = time.perf_counter() - t0
    _check(hit != "", "echo at the end of a 100k-word output")
    _check(elapsed < 3.0, f"100k-word output judged in under 3 s (took {elapsed:.2f} s)")


def section_b3_echo_stopwords():
    print("B3. instruction_echo needs >= 2 content words (MEDIUM-1)")
    prompts = (
        "Judge as an in-country reviewer: the meaning of the {text} matters. "
        "Never echo any part of the instructions back. If you are working on {text} keep the "
        "tags."
    )
    grams = output_guard.literal_prompt_ngrams(prompts, "Use only the plain text.")
    for tgt in (
        "the meaning of the word is clear",
        "Do not remove any part of the device",
        "If you are working on a project",
    ):
        _check(
            output_guard.instruction_echo(tgt, "Quelle", grams) == "",
            f"stopword-only run is clean: {tgt[:30]}",
        )
    g2 = output_guard.literal_prompt_ngrams(PROMPT, SYSTEM)
    _check(
        output_guard.instruction_echo("1. using the following style guide", "1.", g2) != "",
        "a real echo (3 content words) is still flagged",
    )
    _check(
        all(sum(1 for w in g if w not in output_guard._STOPWORDS) >= 2 for g in g2),
        "every retained gram carries at least two content words",
    )


def section_b4_blowup_weights():
    print("B4. short_source_blowup weights (MEDIUM-2)")
    for src, tgt in (
        ("用户管理", "Administración de usuarios"),
        ("一举两得", "kill two birds with one stone"),
        ("RSVP", "Por favor confirme su asistencia"),
        ("FAQ", "Häufig gestellte Fragen"),
        ("Q&A", "Preguntas y respuestas"),
        ("R&D", "Investigación y desarrollo"),
        ("M&A", "Fusiones y adquisiciones"),
        ("KPIs", "Indicadores clave de rendimiento"),
        ("SLAs", "Acuerdos de nivel de servicio"),
        ("APIs", "Interfaces de programación"),
        ("B2C", "De empresa a consumidor"),
    ):
        _check(
            output_guard.short_source_blowup(src, tgt) == "",
            f"legit expansion clean: {tgt[:26]}",
        )
    _check(output_guard.short_source_blowup("1.", ECHO_TGT) != "", "'1.' echo still flags")
    _check(output_guard.short_source_blowup("4.", POSTEDIT_TGT) != "", "'4.' echo still flags")
    _check(
        output_guard.short_source_blowup("Q&A", ECHO_TGT + " " + ECHO_TGT) != "",
        "an ampersand-acronym source with a prompt-length echo still flags",
    )
    _check(
        output_guard.short_source_blowup("FAQ", ECHO_TGT + " " + ECHO_TGT) != "",
        "an acronym source with a prompt-length echo still flags",
    )
    _check(
        output_guard.short_source_blowup("用户管理", "x" * 60) != "",
        "a CJK source with an absurd target still flags",
    )


def _mk_seg(session, doc_id, text, order):
    s = Segment(
        document_id=doc_id,
        target_locale="es-419",
        order_index=order,
        source_text=text,
        status=SegStatus.NEW,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def section_c_seam(session, doc_id):
    print("C. the commit seam: withhold + own flag type + stale clearing")
    seg = _mk_seg(session, doc_id, "1.", 0)
    flag = output_guard.check_output("1.", ECHO_TGT, GRAMS)
    out = quality.commit_mt_output(
        session,
        seg,
        ECHO_TGT,
        source="workflow",
        already_refused=True,
        output_guard_flag=flag,
    )
    _check(out.flagged and not out.tm_eligible and not out.committed, "outcome flagged, no TM")
    _check(not seg.target_text, "target_text not written (withheld like a refusal)")
    _check(seg.status == SegStatus.NEEDS_REVIEW, "status NEEDS_REVIEW")
    _check(
        [f["type"] for f in seg.qa_flags] == ["short_source_blowup"],
        "one qa_flags entry with its own type",
    )
    # soft number_mismatch through the seam: text kept, flagged, review, no TM
    seg_soft = _mk_seg(session, doc_id, "Note [4]", 1)
    soft = output_guard.check_output("Note [4]", "Nota", set(), locale="es-419")
    out = quality.commit_mt_output(
        session, seg_soft, "Nota", source="workflow", output_guard_flag=soft
    )
    _check(out.committed and out.flagged is False, "soft flag: outcome committed")
    _check(not out.tm_eligible, "soft flag: TM-ineligible")
    _check(seg_soft.target_text == "Nota", "soft flag: target text KEPT")
    _check(seg_soft.status == SegStatus.NEEDS_REVIEW, "soft flag: NEEDS_REVIEW")
    _check([f["type"] for f in seg_soft.qa_flags] == ["number_mismatch"], "soft flag: own qa entry")
    # LOW-A: a soft entry never labels a withhold the seam makes for its own reason
    seg_ref = _mk_seg(session, doc_id, "Note [4]", 2)
    quality.commit_mt_output(
        session,
        seg_ref,
        "I'm sorry, but I can't translate this text.",
        source="workflow",
        output_guard_flag=soft,
    )
    _check(
        not seg_ref.target_text and [f["type"] for f in seg_ref.qa_flags] == ["refusal"],
        "seam refusal withhold carries the refusal flag, not the soft number_mismatch entry",
    )
    out = quality.commit_mt_output(session, seg_soft, "Nota [4]", source="workflow")
    _check(
        not any(f.get("type") == "number_mismatch" for f in seg_soft.qa_flags or []),
        "soft flag cleared by a clean re-commit",
    )
    out = quality.commit_mt_output(session, seg, "1.", source="workflow")
    _check(out.committed and seg.target_text == "1.", "a clean re-commit writes the text")
    _check(
        not any(f.get("type") in output_guard.OUTPUT_GUARD_FLAG_TYPES for f in seg.qa_flags or []),
        "the stale output-guard flag is cleared by the clean commit",
    )


class _Canned:
    """Swap `orchestrator.run_completion` for one keyed on the segment text in the prompt."""

    def __init__(self, table):
        self.table = table
        self.prompts = []
        self._orig = None

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            self.prompts.append(prompt)
            text = ""
            for key, val in self.table.items():
                if key in prompt:
                    text = val
            return {
                "text": text,
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": {},
            }

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


def section_d_workflow(session, pid):
    print("D. REAL workflow run")
    doc = Document(project_id=pid, filename="og.md", fmt="md", mode="segmented")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    srcs = {
        0: "AAA1 heading one",  # echo, English prompt tail
        1: "2.",  # translated echo (blow-up)
        2: "Note about ZZZ [4]",  # dropped footnote marker
        3: "Total ZZZ items",  # added number
        4: "3.",  # legit short -> unchanged
        5: "A long ordinary sentence QQQ with several words in it, 3 numbers, 12 and 7.",
        6: "Plain sentence RRR without numbers",  # echoes nothing
    }
    outs = {
        "AAA1": "AAA1 using the following style guide and glossary",
        "2.": "2. usando la siguiente guía de estilo y glosario (ninguno)",
        "Note about ZZZ": "Nota sobre ZZZ",
        "Total ZZZ": "Total de ZZZ 9 elementos",
        "3.": "3.",
        "QQQ": "Una frase larga y normal QQQ con varias palabras, 3 numeros, 12 y 7.",
        "RRR": "Frase simple RRR sin numeros",
    }
    segs = {i: _mk_seg(session, doc.id, t, i) for i, t in srcs.items()}
    wf = Workflow(name="og-wf-%d" % doc.id, scope="global")  # noqa: UP031
    session.add(wf)
    session.commit()
    session.refresh(wf)
    session.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text=PROMPT,
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
        )
    )
    session.commit()
    ids = {i: s.id for i, s in segs.items()}
    wf_id, doc_id = wf.id, doc.id
    events = []
    with _Canned(outs):
        for ev in orchestrator.run_workflow_stream(session, wf_id, doc_id, "es-419"):
            events.append(ev)
    session.expire_all()
    got = {i: session.get(Segment, sid) for i, sid in ids.items()}

    def types(i):
        return [f.get("type") for f in (got[i].qa_flags or [])]

    for i, kind in ((0, "instruction_echo"), (1, "short_source_blowup")):
        _check(
            got[i].status == SegStatus.NEEDS_REVIEW and kind in types(i),
            f"segment {i}: needs_review with a {kind} flag (got {got[i].status}, {types(i)})",
        )
        _check(not got[i].target_text, f"segment {i}: target not written (no TM seeding)")
    # OWNER DECISION default (NUMBER_MISMATCH_WITHHOLDS False): flag for review, KEEP the text
    for i, key in ((2, "Note about ZZZ"), (3, "Total ZZZ")):
        _check(
            got[i].status == SegStatus.NEEDS_REVIEW and "number_mismatch" in types(i),
            f"segment {i}: number mismatch flagged for review (got {got[i].status}, {types(i)})",
        )
        _check(
            got[i].target_text == outs[key],
            f"segment {i}: the translation is KEPT as the target (soft disposition)",
        )
    # the other disposition of the one named constant: withhold like the echo
    doc_w = Document(project_id=pid, filename="og-w.md", fmt="md", mode="segmented")
    session.add(doc_w)
    session.commit()
    session.refresh(doc_w)
    w_ids = {i: _mk_seg(session, doc_w.id, srcs[i], i).id for i in (2, 3)}
    output_guard.NUMBER_MISMATCH_WITHHOLDS = True
    try:
        with _Canned(outs):
            for _ in orchestrator.run_workflow_stream(session, wf_id, doc_w.id, "es-419"):
                pass
        session.expire_all()
        for i in (2, 3):
            s = session.get(Segment, w_ids[i])
            _check(
                s.status == SegStatus.NEEDS_REVIEW
                and not s.target_text
                and "number_mismatch" in [f.get("type") for f in (s.qa_flags or [])],
                f"segment {i}: NUMBER_MISMATCH_WITHHOLDS=True withholds the target",
            )
    finally:
        output_guard.NUMBER_MISMATCH_WITHHOLDS = False
    session.expire_all()
    got = {i: session.get(Segment, sid) for i, sid in ids.items()}
    for i in (4, 5, 6):
        _check(
            got[i].status == SegStatus.MT
            and got[i].target_text == outs[next(k for k in outs if k in srcs[i])]
            and not any(t in output_guard.OUTPUT_GUARD_FLAG_TYPES for t in types(i)),
            f"segment {i}: clean output committed as MT with no output-guard flag",
        )
    _check(
        any(getattr(ev, "get", lambda *_: None)("type") == "done" for ev in events) or bool(events),
        "the run completed (never blocked)",
    )
    # kill switch: same input, guard OFF -> committed unflagged as before
    prev = config.REFUSAL_GUARD_ENABLED
    config.REFUSAL_GUARD_ENABLED = False
    try:
        with _Canned({"2.": outs["2."]}):
            for _ in orchestrator.run_workflow_stream(session, wf_id, doc_id, "es-419"):
                pass
    finally:
        config.REFUSAL_GUARD_ENABLED = prev
    session.expire_all()
    s1 = session.get(Segment, ids[1])
    _check(
        s1.target_text == outs["2."]
        and "short_source_blowup" not in [f.get("type") for f in (s1.qa_flags or [])],
        "kill switch OFF: the blow-up commits and the stale flag is cleared",
    )


def section_d2_serial_adaptive(session, pid):
    print("D2. serial (adaptive_feedback) inline check: flagged output never seeds the buffer")
    doc = Document(project_id=pid, filename="og-serial.md", fmt="md", mode="segmented")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    srcs = [
        "Note about ZZZ [4]",  # soft number mismatch (dropped [4])
        "4.",  # blow-up echo (withheld)
        "Plain sentence RRR without numbers",
    ]
    outs = {
        "Note about ZZZ": "Nota sobre ZZZ",
        "4.": "4. usando la siguiente guía de estilo y glosario (ninguno)",
        "Plain sentence RRR": "Frase simple RRR sin numeros",
    }
    ids = [_mk_seg(session, doc.id, t, i).id for i, t in enumerate(srcs)]
    wf = Workflow(name="og-serial-%d" % doc.id, scope="global")  # noqa: UP031
    session.add(wf)
    session.commit()
    session.refresh(wf)
    session.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text=PROMPT,
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
            adaptive_feedback=True,
            adaptive_feedback_window=5,
        )
    )
    session.commit()
    wf_id, doc_id = wf.id, doc.id
    canned = _Canned(outs)
    with canned:
        for _ in orchestrator.run_workflow_stream(session, wf_id, doc_id, "es-419"):
            pass
    session.expire_all()
    last_prompt = next(p for p in canned.prompts if "RRR" in p and "Plain sentence" in p)
    _check("Nota sobre ZZZ" not in last_prompt, "soft-flagged output not in a later prompt")
    _check("usando la siguiente" not in last_prompt, "withheld output not in a later prompt")
    s0 = session.get(Segment, ids[0])
    _check(
        s0.target_text == "Nota sobre ZZZ"
        and s0.status == SegStatus.NEEDS_REVIEW
        and "number_mismatch" in [f.get("type") for f in (s0.qa_flags or [])],
        "serial path: soft mismatch keeps the text and flags the segment",
    )
    s1 = session.get(Segment, ids[1])
    _check(
        not s1.target_text and "short_source_blowup" in [f.get("type") for f in s1.qa_flags or []],
        "serial path: blow-up withheld and flagged",
    )
    # control: a CLEAN earlier output DOES seed the buffer (the block path is live)
    doc2 = Document(project_id=pid, filename="og-serial2.md", fmt="md", mode="segmented")
    session.add(doc2)
    session.commit()
    session.refresh(doc2)
    _mk_seg(session, doc2.id, "Plain sentence RRR without numbers", 0)
    _mk_seg(session, doc2.id, "Plain sentence SSS without numbers", 1)
    canned2 = _Canned({"Plain sentence RRR": "Frase simple RRR sin numeros", "SSS": "Frase SSS"})
    with canned2:
        for _ in orchestrator.run_workflow_stream(session, wf_id, doc2.id, "es-419"):
            pass
    second = next(p for p in canned2.prompts if "SSS" in p)
    _check("Frase simple RRR sin numeros" in second, "control: a clean output seeds the buffer")


def _paused_run(session, pid, name, srcs, outs):
    """Translate -> Human review, run to the pause. Returns (run_id, [segment ids])."""
    doc = Document(project_id=pid, filename=name + ".md", fmt="md", mode="segmented")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    ids = [_mk_seg(session, doc.id, t, i).id for i, t in enumerate(srcs)]
    wf = Workflow(name="og-pause-%s-%d" % (name, doc.id), scope="global")  # noqa: UP031
    session.add(wf)
    session.commit()
    session.refresh(wf)
    session.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text=PROMPT,
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
        )
    )
    session.add(
        WorkflowStep(
            workflow_id=wf.id, order_index=1, name="Review", step_type=StepType.HUMAN_REVIEW
        )
    )
    session.commit()
    with _Canned(outs):
        summary = orchestrator.run_workflow(session, wf.id, doc.id, "es-419", created_by="t")
    assert summary["status"] == "paused", summary
    return summary["run_id"], ids


def section_f_pause_resume(session, pid):
    print("F. verdicts survive a human_review pause/resume (HIGH-A)")
    srcs = ["Note about ZZZ [4]", "2.", "Plain sentence RRR without numbers"]
    outs = {
        "Note about ZZZ": "Nota sobre ZZZ",
        "2.": "2. usando la siguiente guía de estilo y glosario (ninguno)",
        "Plain sentence RRR": "Frase simple RRR sin numeros",
    }

    def types(seg):
        return [f.get("type") for f in (seg.qa_flags or []) if isinstance(f, dict)]

    # no edits (accept as-is)
    run_id, ids = _paused_run(session, pid, "f-noedit", srcs, outs)
    done = orchestrator.resume_workflow(session, run_id, "es-419", "")
    _check(done["status"] == "completed", "resume (no edits) completed")
    session.expire_all()
    s0, s1, s2 = (session.get(Segment, i) for i in ids)
    _check(
        s1.status == SegStatus.NEEDS_REVIEW
        and not s1.target_text
        and "short_source_blowup" in types(s1),
        f"echo still withheld after resume (got {s1.status}, {s1.target_text!r}, {types(s1)})",
    )
    _check(
        s0.status == SegStatus.NEEDS_REVIEW
        and s0.target_text == "Nota sobre ZZZ"
        and "number_mismatch" in types(s0),
        f"dropped [4] still soft-flagged, text kept (got {s0.status}, {types(s0)})",
    )
    _check(
        s2.status == SegStatus.MT
        and not any(t in output_guard.OUTPUT_GUARD_FLAG_TYPES for t in types(s2)),
        "a clean slot is untouched by the resume",
    )
    # operator edits the withheld slot during the pause: its record is dropped
    run_id, ids = _paused_run(session, pid, "f-edit", srcs, outs)
    edited = SEG_JOINER.join(
        ["Nota sobre ZZZ", "2. Titulo corregido", "Frase simple RRR sin numeros"]
    )
    done = orchestrator.resume_workflow(session, run_id, "es-419", edited)
    _check(done["status"] == "completed", "resume (operator edit) completed")
    session.expire_all()
    s0, s1, _ = (session.get(Segment, i) for i in ids)
    _check(
        s1.target_text == "2. Titulo corregido"
        and not any(t in output_guard.OUTPUT_GUARD_FLAG_TYPES for t in types(s1)),
        f"operator-edited slot: record dropped, edit lands clean (got {s1.status}, {types(s1)})",
    )
    _check(
        s0.status == SegStatus.NEEDS_REVIEW and "number_mismatch" in types(s0),
        "the unedited slot keeps its soft flag",
    )


def section_e_run_view():
    print("E. run view line")
    verify = package_view.step_facts({"step_type": StepType.LLM_AGENT})["verify"]
    labels = [f["label"] for f in verify]
    _check("Output guard" in labels, "llm_agent VERIFY list carries 'Output guard'")
    _check(
        labels.index("Output guard") == labels.index("Refusal guard") + 1,
        "it follows the Refusal guard line",
    )
    lqa = package_view.step_facts({"step_type": StepType.LQA})["verify"]
    _check("Output guard" not in [f["label"] for f in lqa], "an LQA step does not claim it")


def main():
    print("verify_output_guard -- GitHub #343")
    init_db()
    section_a_gap()
    section_b_predicates()
    with Session(engine) as session:
        p = Project(
            name="OG",
            source_locale="en-US",
            target_locales=["es-419"],
            default_provider="mock",
            project_type=ProjectType.DOCUMENT,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="seam.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)
        section_c_seam(session, d.id)
        section_d_workflow(session, p.id)
        section_d2_serial_adaptive(session, p.id)
        section_f_pause_resume(session, p.id)
    section_e_run_view()
    if FAILS:
        print("FAILED: %d" % len(FAILS))  # noqa: UP031
        for f in FAILS:
            print(" - " + f)
        sys.exit(1)
    print("verify_output_guard OK")


if __name__ == "__main__":
    main()
