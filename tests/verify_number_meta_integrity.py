"""BUG-06 regression lock (live QA campaign, corpus-data/qa-campaign/CAMPAIGN.md).

Doc 23 / run 27 (workflow 4, local llama translate + Claude LQA, en-US -> es-ES) shipped
TWO defects with `status=mt` and `qa_flags: []`:

  (a) NUMBERS -- "25 items" -> "25.000 articulos": a value corrupted 1000x. A number
      check DID exist (`qa.check_segment`) but it was (1) never called from the sole
      write seam, so it could not persist a flag or route a status, and (2) a comparison
      of sorted numeral STRINGS with no notion of value or locale -- so it FALSE-POSITIVED
      on every correct locale re-rendering ("1,234.56" -> "1.234,56") while scoring the
      real corruption as a MINOR that `lqa._QA_TO_MQM` maps to 1 penalty point (99/100 --
      a PASS). Fix: `qa.number_findings` -- value-aware comparison with locale-aware
      parsing of the target side, non-prose numeric carriers masked first -- plus the
      `quality.number_violation` gate at `commit_mt_output`.

  (b) META-COMMENTARY -- the delivered file carried
      "*(Note: I corrected all issues identified in the feedback to produce a final,
      polished translation that complies with es-ES conventions and guidelines.)*".
      Every existing detector is ANCHORED TO THE START of the output (`is_refusal`'s
      80-char stem window / position-0 leading tier, `strip_meta_preamble`'s LEADING
      label + fence layers) and this note TRAILS a correct translation; the language
      heuristic correctly abstains because the segment really is Spanish. Fix:
      `quality.meta_commentary` -- span-bounded (parenthetical / bracket / note-led
      line) + first-person + meta-linguistic vocabulary, with a source-side keep-guard --
      wired at the same choke point onto the EXISTING `nontranslation` flag family.

FAIL-BEFORE is proven by FILE COPY, never `git stash` (the stash stack is shared across
worktrees): `git show <pre-fix-commit>:app/services/{qa,quality}.py` is
written to a temp dir and imported under a private module name, and the pre-fix code is
asserted to (1) lack the new seams entirely, (2) MISS the live number corruption's MAJOR
verdict, and (3) FALSE-POSITIVE on the correct es rendering. The commentary half is
proven against the LIVE, UNMODIFIED `refusal` module: all three of its detectors return
"no finding" for the exact shipped string.

Hermetic (throwaway LOC_TMS_DATA_DIR, MockProvider, no keys, no network). ASCII-only
output; final line 'verify_number_meta_integrity OK'.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bug06_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus
from app.services import lqa, qa, quality, refusal

# ---------------------------------------------------------------------------
# The EXACT live strings from the campaign report.
# ---------------------------------------------------------------------------
LIVE_NUM_SRC = "You have 25 items in your cart."
LIVE_NUM_TGT = "Tienes 25.000 articulos en tu carrito."

LIVE_NOTE_SRC = "Check your email to continue with the upload."
LIVE_NOTE_TGT = (
    "Revisa tu correo electronico para continuar con la subida.\n\n"
    "*(Note: I corrected all issues identified in the feedback to produce a final, "
    "polished translation that complies with es-ES conventions and guidelines.)*"
)

# The correct es-ES re-rendering of an en-US amount -- identical VALUE, different glyphs.
# The pre-fix string compare flagged this; the fix must be SILENT on it.
RERENDER_SRC = "The invoice total is 1,234.56 for 1,000 licenses."
RERENDER_TGT = "El total de la factura es 1.234,56 para 1.000 licencias."


def _fail(msg):
    raise AssertionError(msg)


def _ok(label):
    print("  ok - " + label)


# ---------------------------------------------------------------------------
# A. FAIL-BEFORE, by file copy of the pre-fix modules (never git stash).
# ---------------------------------------------------------------------------
# The BUG-06 fix landed in b474cc8, so "pre-fix" is the commit before it. Pinning
# (rather than HEAD:) avoids the proof inverting once the fix commit becomes HEAD --
# same technique as verify_export_tag_policy._BASE_COMMIT and the dispatch guard's
# _PRE_FIX_COMMIT.
_PRE_FIX_COMMIT = "a95a402"


def _load_pre_fix(rel_path, mod_name):
    """Import `git show <_PRE_FIX_COMMIT>:<rel_path>` under `mod_name` from a throwaway file."""
    blob = subprocess.run(
        ["git", "show", _PRE_FIX_COMMIT + ":" + rel_path], cwd=_ROOT, capture_output=True
    )
    if blob.returncode != 0 or not blob.stdout:
        return None
    tmpdir = tempfile.mkdtemp(prefix="loctms_bug06_pre_")
    path = os.path.join(tmpdir, mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(blob.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_fail_before():
    print("A. fail-before (pre-fix modules, loaded by file copy)")
    pre_qa = _load_pre_fix("app/services/qa.py", "_bug06_pre_qa")
    if pre_qa is None:
        print("  skip - no git HEAD available (shallow/detached checkout)")
        return
    if hasattr(pre_qa, "number_findings"):
        _fail("pre-fix qa.py already exposes number_findings -- HEAD is not pre-fix")
    _ok("pre-fix qa.py has no value-aware comparator (number_findings absent)")

    # (1) the live corruption was only ever a MINOR string-mismatch, never a value verdict
    pre = [f for f in pre_qa.check_segment(LIVE_NUM_SRC, LIVE_NUM_TGT) if f["type"] == "numbers"]
    if not pre or pre[0]["severity"] != "minor" or "kind" in pre[0]:
        _fail(f"expected the pre-fix number flag to be an undifferentiated MINOR, got {pre!r}")
    _ok("pre-fix verdict on the live corruption is MINOR with no value/format class")

    # (2) and it FALSE-POSITIVED on the correct es re-rendering
    pre_fp = [f for f in pre_qa.check_segment(RERENDER_SRC, RERENDER_TGT) if f["type"] == "numbers"]
    if not pre_fp:
        _fail("expected the pre-fix check to false-positive on the correct es rendering")
    _ok("pre-fix check false-positived on the correct es rendering (now silent)")

    # (3) the write seam carried neither gate
    pre_q = subprocess.run(
        ["git", "show", _PRE_FIX_COMMIT + ":app/services/quality.py"],
        cwd=_ROOT,
        capture_output=True,
    ).stdout.decode("utf-8", "replace")
    for missing in ("number_violation", "meta_commentary", "NUMBER_FLAG_TYPE"):
        if missing in pre_q:
            _fail(f"pre-fix quality.py already mentions {missing!r} -- HEAD is not pre-fix")
    _ok("pre-fix quality.py has neither the number gate nor the commentary guard")


def check_commentary_root_cause():
    """The commentary half's fail-before, proven against the LIVE refusal module (which
    this change does not touch): all three existing detectors miss the shipped string."""
    print("B. root cause of the commentary miss (live, unmodified refusal module)")
    if refusal.is_refusal(LIVE_NOTE_TGT):
        _fail("is_refusal unexpectedly catches the trailing note")
    _ok("refusal.is_refusal misses it (anchored to the first 80 chars / position 0)")
    if refusal.looks_untranslated(LIVE_NOTE_TGT, "es-ES"):
        _fail("looks_untranslated unexpectedly fires on real Spanish")
    _ok("refusal.looks_untranslated abstains (the segment really is Spanish)")
    cleaned, note = refusal.strip_meta_preamble(LIVE_NOTE_TGT, LIVE_NOTE_SRC)
    if note is not None or cleaned != LIVE_NOTE_TGT:
        _fail("strip_meta_preamble unexpectedly rewrote the trailing note")
    _ok("refusal.strip_meta_preamble leaves it (LEADING label / fence layers only)")


# ---------------------------------------------------------------------------
# C. The number comparator: value-aware + locale-aware + masked.
# ---------------------------------------------------------------------------
def check_number_findings():
    print("C. qa.number_findings")

    def kind(src, tgt, loc=None):
        f = qa.number_findings(src, tgt, loc)
        return f[0]["kind"] if f else None

    def sev(src, tgt, loc=None):
        f = qa.number_findings(src, tgt, loc)
        return f[0]["severity"] if f else None

    # THE headline regression: the live corruption, judged with the real target locale.
    if kind(LIVE_NUM_SRC, LIVE_NUM_TGT, "es-ES") != "value":
        _fail("live '25 items' -> '25.000 articulos' must be a VALUE change for es-ES")
    if sev(LIVE_NUM_SRC, LIVE_NUM_TGT, "es-ES") != "major":
        _fail("the live number corruption must be MAJOR")
    _ok("live 25 -> 25.000 for es-ES is MAJOR / kind=value")

    # Without a locale the two readings are genuinely undecidable -> the honest MINOR.
    # This is the LQA/editor path, and it preserves the pre-fix recall there.
    if kind(LIVE_NUM_SRC, LIVE_NUM_TGT) != "format":
        _fail("with no target locale the same pair must degrade to the ambiguous MINOR")
    _ok("same pair with no locale degrades to minor/format (recall preserved)")

    # A comma-decimal locale that is POINT-decimal by REGION must not misjudge.
    if kind(LIVE_NUM_SRC, LIVE_NUM_TGT, "es-MX") is not None:
        _fail("es-MX is point-decimal: '25.000' there really is 25, so it must be clean")
    _ok("es-MX (point-decimal region) reads 25.000 as 25 -> clean")

    # The false-positive class the old check had: a CORRECT locale re-rendering.
    for loc in (None, "es-ES", "fr-FR"):
        if kind(RERENDER_SRC, RERENDER_TGT, loc) is not None:
            _fail(f"correct es rendering of 1,234.56 / 1,000 must NOT flag (locale={loc!r})")
    _ok("1,234.56 -> 1.234,56 and 1,000 -> 1.000 are silent (was a false positive)")
    if kind("Total 1,234.56", "Total 1 234,56", "fr-FR") is not None:
        _fail("the fr plain-space grouping must not flag")
    _ok("fr '1 234,56' (plain space, per the campaign) is silent")

    # Conservatism: numbers inside non-prose carriers must never reach the comparator.
    masked = [
        ("Open https://acme.test/v1/25000/report", "Abre https://acme.test/v1/25000/report"),
        ("Uploaded {count} of %d files to $HOME", "Se subieron {count} de %d archivos a $HOME"),
        ("Released 2026-07-28 at 18:30", "Publicado el 28/07/2026 a las 18:30"),
        ("Requires build 10.0.26200 or v1.2.3", "Requiere la compilacion 10.0.26200 o v1.2.3"),
        ("Updated March 3, 2026", "Actualizado el 3 de marzo de 2026"),
        ("Click <b>Save</b> 2 times", "Haz clic en <b>Save</b> 2 veces"),
        ("Tag " + "⟦r1⟧" + "5 GB" + "⟦/r1⟧", "Etiqueta " + "⟦r1⟧" + "5 GB" + "⟦/r1⟧"),
    ]
    for src, tgt in masked:
        if kind(src, tgt, "es-ES") is not None:
            _fail(f"masked carrier must not produce a number flag: {src!r} -> {tgt!r}")
    _ok("URLs / placeholders / dates / times / versions / markup / tokens never flag")

    # Real value changes still caught; ambiguity never manufactures a MAJOR.
    if kind("We shipped 1,000 units.", "Enviamos 1.500 unidades.", "es-ES") != "value":
        _fail("1,000 -> 1.500 is a real value change")
    if kind("Only 5 left of 12.", "Solo quedan 5.", "es-ES") != "count":
        _fail("a dropped number is the minor count class")
    if kind("25 percent", "25 por ciento", "es-ES") is not None:
        _fail("an unchanged number must be silent")
    _ok("real value change MAJOR; dropped number minor/count; unchanged silent")

    # An empty / target-less segment must not explode or flag.
    for src, tgt in (("", ""), ("5 items", ""), (None, None)):
        qa.number_findings(src, tgt, "es-ES")
    _ok("empty / None inputs are safe")


def check_url_carrier_digits_never_leak():
    """#581: the number_mismatch message names ONLY the user's visible numbers, never
    digits harvested from a URL, a path or a placeholder token. Reproduced on the base
    code (see scratch/repro2.py): a scheme-less URL, a protocol-relative URL or a
    root-relative path leaked its path digits into the "missing/added ..." clause
    (the production lane saw "added 02, 73036, 1..."), because the mask covered only
    `https?://` and `www.` carriers. The fix extends the mask to scheme-less hosts,
    protocol-relative URLs and root-relative paths."""
    print("C2. leaked URL / path digits never reach the number message")

    def only_visible(src, tgt, loc, must_show, must_not, label):
        findings = qa.number_findings(src, tgt, loc)
        if not findings:
            _fail(f"{label}: expected a number finding")
        msg = findings[0]["message"]
        for d in must_show:
            if d not in msg:
                _fail(f"{label}: the user's number {d!r} must appear in {msg!r}")
        for leak in must_not:
            if leak in msg:
                _fail(f"{label}: internal digits {leak!r} leaked into {msg!r}")

    # A dropped user number (12) while both sides carry a scheme-less host path that must
    # never appear in the verdict. Before the fix this read as "value changed (73036 ->
    # 90001)"; now it is a plain "missing 12".
    only_visible(
        "Save 12 files at help.example.com/p/73036",
        "ファイルを example.com/p/90001 に保存",
        "ja-JP",
        must_show=["12"],
        must_not=["73036", "90001"],
        label="bare host path",
    )
    # A protocol-relative URL in the target that the source lacks: its digits must never
    # show up as "added". Before the fix this was "added 73036".
    only_visible(
        "Read 12 items now",
        "商品を見る //shop.test/p/73036/02",
        "ja-JP",
        must_show=["12"],
        must_not=["73036", "02"],
        label="protocol-relative URL",
    )
    # A root-relative path on the target side (the report's `/help/73036/02` shape).
    only_visible(
        "Read 12 items",
        "Read more at /help/73036/02",
        "en-GB",
        must_show=["12"],
        must_not=["73036", "02"],
        label="root-relative path",
    )
    # The masked-carrier property still holds when a correct rendering keeps the same URL:
    # no finding at all, and a real dropped value still separates from the noise.
    if qa.number_findings(
        "Open //shop.test/p/73036 for 5 items",
        "//shop.test/p/73036 へ 5 点",
        "ja-JP",
    ):
        _fail("a correct rendering sharing the same URL must be silent")
    # Review guard: list numbering and "N.word" are NOT hosts, so a changed number there
    # is still reported (the host mask needs a letter in every non-TLD label).
    for s_, t_ in (
        ("Press 5.ok", "Pulse 6.ok"),
        ("1.Introduction", "2.Introduccion"),
        ("Weight 10kg.The box", "Peso 12kg.The caja"),
    ):
        if not qa.number_findings(s_, t_, "es-ES", "en-US"):
            _fail(f"{s_!r} -> {t_!r}: a changed number next to a dot-word must still be flagged")
    _ok("number message shows only visible numbers; URL/path/placeholder digits never leak")


def check_check_segment_signature():
    """`target_locale` is ADDITIVE: every pre-existing 2-positional-argument caller
    (documents.py, content.py, translate.py, lqa._structural_errors) is unchanged."""
    print("D. check_segment signature compatibility")
    two_arg = qa.check_segment(LIVE_NUM_SRC, LIVE_NUM_TGT)
    three_arg = qa.check_segment(LIVE_NUM_SRC, LIVE_NUM_TGT, "es-ES")
    if not any(f["type"] == "numbers" and f["severity"] == "minor" for f in two_arg):
        _fail("the 2-arg call must still produce the conservative minor flag")
    if not any(f["type"] == "numbers" and f["severity"] == "major" for f in three_arg):
        _fail("the 3-arg call must produce the MAJOR value verdict")
    _ok("2-arg callers unchanged; the locale-aware 3-arg call is decisive")
    # The flag TYPE stays "numbers" so lqa._QA_TO_MQM keeps mapping it to
    # Accuracy/Number with NO edit to lqa.py (the intended seam).
    if "numbers" not in lqa._QA_TO_MQM:
        _fail("lqa._QA_TO_MQM lost its 'numbers' row")
    errs = lqa._structural_errors(LIVE_NUM_SRC, LIVE_NUM_TGT)
    if not any(e["dimension"] == "Accuracy/Number" for e in errs):
        _fail("the LQA structural layer must still raise an Accuracy/Number finding")
    _ok("lqa._structural_errors still maps it to Accuracy/Number (no lqa.py change)")


# ---------------------------------------------------------------------------
# E. The commentary detector: recall on the live string, precision elsewhere.
# ---------------------------------------------------------------------------
def check_meta_commentary():
    print("E. quality.meta_commentary")
    if not quality.meta_commentary(LIVE_NOTE_SRC, LIVE_NOTE_TGT):
        _fail("the exact shipped translator note must be detected")
    _ok("the live '*(Note: I corrected all issues ...)*' note is detected")

    flagged = [
        (
            "Nota: He corregido la traduccion segun las directrices del proyecto.",
            "es note line, first person + meta vocab",
        ),
        ("Le texte. (Remarque : j'ai traduit le contenu selon les consignes.)", "fr parenthetical"),
        (
            "Der Text. [Hinweis: Ich habe alle Anweisungen zur Uebersetzung befolgt.]",
            "de bracketed",
        ),
    ]
    for tgt, label in flagged:
        if not quality.meta_commentary("Some plain English source sentence.", tgt):
            _fail("expected a hit: " + label)
    _ok("es / fr / de translator-note shapes are detected")

    clean = [
        (
            "Reinicie el equipo. (Nota: la traduccion se actualiza automaticamente.)",
            "legit note marker + translation vocabulary, but NO first person",
        ),
        (
            "Guarde el archivo (he corregido el error de la pagina de inicio).",
            "first person, but about the PRODUCT, not the task",
        ),
        (
            "El cliente escribio: he traducido el documento segun la traduccion oficial.",
            "first person + vocab but NOT inside a note span",
        ),
        ("Consulte la guia de estilo y el glosario del proyecto.", "meta vocabulary alone"),
        ("Bienvenido a Acme Cloud (version 2.0).", "an ordinary parenthetical"),
        ("", "empty target"),
    ]
    for tgt, label in clean:
        if quality.meta_commentary("Some plain English source sentence.", tgt):
            _fail("FALSE POSITIVE (" + label + "): " + tgt)
    _ok("legitimate parentheticals / notes / first-person product copy stay clean")

    # Source-side keep-guard: a document genuinely ABOUT translator notes, and the
    # MockProvider echo (target == source), must both abstain.
    doc_src = "(Note: I corrected the translation per the feedback.)"
    if quality.meta_commentary(doc_src, doc_src):
        _fail("the source-side keep-guard must abstain when the SOURCE carries the note")
    if quality.meta_commentary(doc_src, "[es] " + doc_src):
        _fail("the offline MockProvider echo must never trip the guard")
    _ok("source-side keep-guard holds (real content + MockProvider echo)")


# ---------------------------------------------------------------------------
# F. End to end at the sole write seam (quality.commit_mt_output).
# ---------------------------------------------------------------------------
def _seg(session, doc_id, source_text, order):
    s = Segment(
        document_id=doc_id,
        target_locale="es-ES",
        order_index=order,
        source_text=source_text,
        status=SegStatus.NEW,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def check_choke_point():
    print("F. the write seam (quality.commit_mt_output)")
    init_db()
    with Session(engine) as session:
        p = Project(
            name="BUG-06", source_locale="en-US", target_locales=["es-ES"], default_provider="mock"
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="bug06.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)

        # (a) the live number corruption
        s1 = _seg(session, d.id, LIVE_NUM_SRC, 0)
        out = quality.commit_mt_output(session, s1, LIVE_NUM_TGT, source="workflow")
        session.commit()
        session.refresh(s1)
        if not out.committed or out.flagged:
            _fail("the number gate must be SOFT: the text is still committed")
        if s1.target_text != LIVE_NUM_TGT:
            _fail("the number gate must never rewrite or withhold the target")
        if s1.status != SegStatus.NEEDS_REVIEW:
            _fail(
                f"a number value change must route the segment to NEEDS_REVIEW, got {s1.status!r}"
            )
        if out.tm_eligible:
            _fail("a number-corrupted target must be TM-ineligible")
        if not out.number_violation:
            _fail("CommitOutcome.number_violation must name the divergence")
        types = [f.get("type") for f in (s1.qa_flags or [])]
        if quality.NUMBER_FLAG_TYPE not in types:
            _fail(f"expected a number_integrity qa_flag, got {s1.qa_flags!r}")
        sev = [f.get("severity") for f in s1.qa_flags if f.get("type") == quality.NUMBER_FLAG_TYPE]
        if sev != ["major"]:
            _fail(f"the number_integrity flag must be MAJOR, got {sev!r}")
        _ok("live number corruption: committed + NEEDS_REVIEW + major flag + TM-ineligible")

        # (b) the live trailing translator note
        s2 = _seg(session, d.id, LIVE_NOTE_SRC, 1)
        out2 = quality.commit_mt_output(session, s2, LIVE_NOTE_TGT, source="workflow")
        session.commit()
        session.refresh(s2)
        if not out2.committed or s2.target_text != LIVE_NOTE_TGT:
            _fail("the commentary guard must be SOFT: text committed, never rewritten")
        if s2.status != SegStatus.NEEDS_REVIEW:
            _fail("a surviving translator note must route to NEEDS_REVIEW")
        if out2.tm_eligible:
            _fail("a target carrying model commentary must be TM-ineligible")
        if not out2.meta_commentary:
            _fail("CommitOutcome.meta_commentary must name the note")
        notes = [
            f for f in (s2.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        ]
        if len(notes) != 1 or notes[0].get("severity") != "major":
            _fail(f"expected ONE major nontranslation-family flag, got {s2.qa_flags!r}")
        if quality.META_COMMENTARY_REASON not in (notes[0].get("reason") or ""):
            _fail(f"the flag must carry the distinct commentary reason, got {notes[0]!r}")
        if notes[0].get("reason") == refusal.NONTRANSLATION_REASON:
            _fail("the commentary verdict must be DISTINGUISHABLE from the language one")
        if notes[0].get("source") != "workflow":
            _fail("the flag must carry the caller's provenance tag")
        _ok("live translator note: NEEDS_REVIEW + nontranslation-family major flag")
        # Reusing the established family is what makes it visible headlessly with no
        # change to the editor's _SURFACED set or api_cli's _SEGMENT_SURFACED allowlist.
        from app.web import api_cli

        row = api_cli._segment_detail_row(s2)
        if not any(f["type"] == refusal.NONTRANSLATION_FLAG_TYPE for f in row["qa_flags"]):
            _fail("the commentary flag must survive the headless surfaced-flag filter")
        _ok("the commentary flag is surfaced by the /api + CLI detail row (parity)")

        # (c) a CLEAN pair: byte-identical to pre-fix behaviour.
        s3 = _seg(session, d.id, RERENDER_SRC, 2)
        out3 = quality.commit_mt_output(session, s3, RERENDER_TGT, source="mt")
        session.commit()
        session.refresh(s3)
        if s3.status != SegStatus.MT or s3.qa_flags or not out3.tm_eligible:
            _fail(
                "a correct locale re-rendering must commit clean, got status={!r} flags={!r}".format(  # noqa: E501, UP032
                    s3.status, s3.qa_flags
                )
            )
        if out3.number_violation or out3.meta_commentary:
            _fail("a clean commit must report no BUG-06 violation")
        _ok("the correct es re-rendering commits clean (status mt, no flags, TM-eligible)")

        # (d) the offline MockProvider echo path must stay untouched.
        s4 = _seg(session, d.id, "You have 25 items.", 3)
        out4 = quality.commit_mt_output(session, s4, "[es-ES] You have 25 items.", source="mt")
        session.commit()
        session.refresh(s4)
        if s4.status != SegStatus.MT or s4.qa_flags:
            _fail(
                f"the MockProvider echo must not trip either gate, got {s4.status!r} / {s4.qa_flags!r}"  # noqa: E501
            )
        if not out4.tm_eligible:
            _fail("the MockProvider echo must stay TM-eligible")
        _ok("the offline MockProvider echo is byte-identical (no flag, no downgrade)")

        # (e) kill switch: REFUSAL_GUARD_ENABLED off restores pre-guard behaviour for
        # the commentary half (the number gate is a pure deterministic honesty check and
        # stays on, exactly like the tag gate).
        from app import config

        prev = config.REFUSAL_GUARD_ENABLED
        try:
            config.REFUSAL_GUARD_ENABLED = False
            s5 = _seg(session, d.id, LIVE_NOTE_SRC, 4)
            out5 = quality.commit_mt_output(session, s5, LIVE_NOTE_TGT, source="mt")
            session.commit()
            session.refresh(s5)
            if out5.meta_commentary or s5.qa_flags:
                _fail("the kill switch must disable the commentary guard")
        finally:
            config.REFUSAL_GUARD_ENABLED = prev
        _ok("REFUSAL_GUARD_ENABLED=0 disables the commentary guard (kill switch honoured)")


# ---------------------------------------------------------------------------
# G. ADVERSARIAL-REVIEW REGRESSIONS (2026-07-28). Every case below was a REPRODUCED
#    defect in the first cut of the fix; each is pinned here so it cannot come back.
# ---------------------------------------------------------------------------
def check_review_number_regressions():
    print("G1. review: number false positives (MAJOR on correct delivered content)")

    def kind(src, tgt, loc=None):
        f = qa.number_findings(src, tgt, loc)
        return f[0]["kind"] if f else None

    # (1) LOCALE TABLE. Switzerland is POINT-decimal ("1'234.56") although de/fr/it are
    # comma-decimal in DE/FR/IT, and `es-419` names a region that spans BOTH conventions.
    # Both misreadings turned a CORRECT target into a MAJOR value change.
    for loc in ("de-CH", "de-LI", "fr-CH", "it-CH"):
        if qa.decimal_style(loc) != "point":
            _fail(f"{loc} must be point-decimal (Swiss apostrophe grouping)")
        if kind(LIVE_NUM_SRC, LIVE_NUM_TGT, loc) is not None:
            _fail(f"{loc} reads '25.000' as 25 -> must be clean, not a value change")
    _ok("de-CH / de-LI / fr-CH / it-CH are point-decimal (was a MAJOR false positive)")
    if qa.decimal_style("es-419") is not None:
        _fail("es-419 spans both conventions -> the parser must ABSTAIN, not guess comma")
    if kind(LIVE_NUM_SRC, LIVE_NUM_TGT, "es-419") != "format":
        _fail("es-419 must degrade to the ambiguous MINOR, never a MAJOR")
    _ok("es-419 abstains -> minor/format (was a MAJOR false positive)")
    # ...while the locales that ARE decisive stay decisive.
    for loc in ("es-ES", "de-DE", "fr-FR", "it-IT", "pt-BR", "tr-TR"):
        if kind(LIVE_NUM_SRC, LIVE_NUM_TGT, loc) != "value":
            _fail(f"{loc} is comma-decimal: the live corruption must stay MAJOR")
    _ok("es-ES / de-DE / fr-FR / it-IT / pt-BR / tr-TR keep the decisive MAJOR verdict")

    # (2) RE-GROUPING. A plain space is a legitimate thousands separator, so any run of
    # space-separated 3-digit groups fuses into ONE token; a target that re-punctuates it
    # splits it back apart, and the comparator called that a changed value.
    regrouped = [
        ("Sizes: 128 256 512 GB", "Tamanos: 128, 256, 512 GB", "space-grouped -> commas"),
        ("Sizes: 128, 256, 512 GB", "Tamanos: 128 256 512 GB", "commas -> space-grouped"),
        ("Call +1 555 123 4567", "Llame al +1 (555) 123-4567", "phone re-punctuated"),
        ("Call 555-123-4567", "Llame al 555 123 4567", "phone dashes -> spaces"),
    ]
    for src, tgt, label in regrouped:
        got = kind(src, tgt, "es-ES")
        if got == "value":
            _fail(f"MAJOR false positive on correct content ({label}): {src!r} -> {tgt!r}")
        if got != "format":
            _fail(f"expected the minor formatting class for {label}, got {got!r}")
    _ok("re-grouped / re-punctuated digit runs are minor/format (were MAJOR)")

    # ...and the narrowness of that demotion: the token COUNTS must differ. An
    # equal-count pair whose digits coincide is a real separator-position corruption.
    if kind("Pay 1,000 now", "Pague 10,00 ahora", "es-ES") != "value":
        _fail("'1,000' -> '10,00' is a real value change and must stay MAJOR")
    if kind("We shipped 1,000 units.", "Enviamos 1.500 unidades.", "es-ES") != "value":
        _fail("a genuine value change must stay MAJOR")
    _ok("equal-count digit coincidence ('1,000' -> '10,00') still MAJOR (guard is narrow)")

    # The LQA / editor path (two positional args, no locale) must see the same demotion.
    two_arg = [
        f
        for f in qa.check_segment("Sizes: 128 256 512 GB", "Tamanos: 128, 256, 512 GB")
        if f["type"] == "numbers"
    ]
    if any(f["severity"] == "major" for f in two_arg):
        _fail("the 2-arg LQA path must not raise a MAJOR on a re-grouping")
    _ok("the 2-arg LQA/editor path inherits the demotion")


def check_review_meta_regressions():
    print("G2. review: commentary recall gaps and asymmetry false positives")
    PLAIN = "Some plain English source sentence."

    # (1) RECALL. Each of these is a shape a real provider emits and the first cut missed.
    missed = [
        (
            "Verifiez votre courriel. (Remarque : j’ai traduit le contenu selon les consignes.)",
            PLAIN,
            "fr note with the CURLY apostrophe providers actually emit",
        ),
        (
            "Revisa tu correo. (Note: I’ve corrected the translation per the guidelines.)",
            PLAIN,
            "en contraction with a curly apostrophe",
        ),
        (
            "Pruefen Sie Ihre E-Mail. (Hinweis: Ich habe den Text gemaess den Richtlinien "
            "uebersetzt.)",
            PLAIN,
            "de perfect tense (participle at the END of the clause)",
        ),
        (
            "Controleer uw e-mail. (Opmerking: Ik heb de tekst volgens de richtlijnen vertaald.)",
            PLAIN,
            "nl perfect tense (participle at the end)",
        ),
        (
            "Revisa tu correo. (Note: I corrected all issues (see feedback) per the guidelines.)",
            PLAIN,
            "NESTED parens (invisible to the flat paren regex)",
        ),
        (
            "Texto traducido.\n<!-- Note: I translated this per the instructions -->",
            PLAIN,
            "note inside a comment delimiter (never starts its line)",
        ),
    ]
    # NOTE: the failure messages name the LABEL only, never the target -- these fixtures
    # deliberately carry non-ASCII (curly apostrophes, CJK) and a Windows console would
    # raise UnicodeEncodeError on the report instead of showing the real failure.
    for tgt, src, label in missed:
        if not quality.meta_commentary(src, tgt):
            _fail("commentary MISSED: " + label)
    _ok("curly apostrophes / de+nl word order / nested parens / comment notes detected")

    # (2) PRECISION. The source-side keep-guard is a PHRASE test, so it only protects
    # source languages the phrase lists cover. A genuine first-person parenthetical in an
    # UNCOVERED source language sailed past it while its English translation tripped
    # locks 1-3 -- correct content quarantined. The count-symmetry guard closes it: an
    # APPENDED note ADDS a span of its kind, a TRANSLATED one does not.
    asymmetric = [
        (
            "(Ich habe ueber 500 Dokumente uebersetzt.)",
            "(I have translated over 500 documents.)",
            "de source, en target",
        ),
        (
            "(J’ai traduit ce document en 2020.)",
            "(I have translated this document in 2020.)",
            "fr curly source, en target",
        ),
        (
            "(私はこの文書を翻訳しました。)",
            "(I have translated this document.)",
            "ja source (no phrase coverage at all)",
        ),
        (
            "(Note: I corrected the translation per the feedback.)",
            "(Nota: He corregido la traduccion segun los comentarios.)",
            "a document genuinely ABOUT translator notes",
        ),
    ]
    for src, tgt, label in asymmetric:
        if quality.meta_commentary(src, tgt):
            _fail("FALSE POSITIVE on a translated source span: " + label)
    _ok("a translated source parenthetical never flags, in any source language")

    # ...and the guard must not blunt the live case: an APPENDED note raises the count
    # even when the source already has a parenthetical of its own.
    if not quality.meta_commentary(
        "You have 25 items (in your cart).",
        "Tienes 25 articulos (en tu carrito).\n\n*(Note: I corrected all issues "
        "identified in the feedback.)*",
    ):
        _fail("an APPENDED note must still be caught when the source has its own paren")
    _ok("an appended note still flags when the source carries an unrelated parenthetical")

    # The precision locks from the original design are untouched by the broadening.
    still_clean = [
        (
            "Reinicie el equipo. (Nota: la traduccion se actualiza automaticamente.)",
            "note marker + translation vocabulary, but NO first person",
        ),
        (
            "Guarde el archivo (he corregido el error de la pagina de inicio).",
            "first person, but about the PRODUCT, not the task",
        ),
        (
            "El cliente escribio: he traducido el documento segun la traduccion oficial.",
            "first person + vocab but NOT inside a note span",
        ),
        (
            "Por favor, tenga en cuenta: la guia de estilo cambio.",
            "a mid-line note marker with neither lock 2 nor lock 3",
        ),
    ]
    for tgt, label in still_clean:
        if quality.meta_commentary(PLAIN, tgt):
            _fail("FALSE POSITIVE (" + label + "): " + tgt)
    _ok("locks 2 and 3 still hold after the marker rule was broadened to mid-line")


def check_stale_flag_clearing():
    """A flag that is only ever ADDED records a verdict about text that no longer exists.
    The export reviewer found this for `tag_integrity` (the exporter had to build an
    admissibility floor around the staleness); the same hole was in BOTH new BUG-06
    families. All three clear at the seam when the re-run check comes back clean."""
    print("G3. review: stale qa_flags are cleared when a later commit is clean")
    with Session(engine) as session:
        p = Project(
            name="BUG-06 stale",
            source_locale="en-US",
            target_locales=["es-ES"],
            default_provider="mock",
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="stale.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)

        # (a) number_integrity: damaged commit -> flag; clean re-commit -> gone.
        s = _seg(session, d.id, LIVE_NUM_SRC, 10)
        quality.commit_mt_output(session, s, LIVE_NUM_TGT, source="workflow")
        session.commit()
        session.refresh(s)
        if quality.NUMBER_FLAG_TYPE not in [f.get("type") for f in (s.qa_flags or [])]:
            _fail("setup: the corrupted commit must set a number_integrity flag")
        quality.commit_mt_output(
            session, s, "Tienes 25 articulos en tu carrito.", source="workflow"
        )
        session.commit()
        session.refresh(s)
        if quality.NUMBER_FLAG_TYPE in [f.get("type") for f in (s.qa_flags or [])]:
            _fail(f"a corrected figure must clear the number_integrity badge, got {s.qa_flags!r}")
        _ok("number_integrity clears once the figure is corrected")

        # (b) the commentary verdict clears -- WITHOUT touching the language verdict that
        # shares its flag family.
        s2 = _seg(session, d.id, LIVE_NOTE_SRC, 11)
        quality.commit_mt_output(session, s2, LIVE_NOTE_TGT, source="workflow")
        session.commit()
        session.refresh(s2)
        if not [
            f for f in (s2.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        ]:
            _fail("setup: the note commit must set a nontranslation-family flag")
        quality.commit_mt_output(
            session, s2, "Revisa tu correo electronico para continuar.", source="workflow"
        )
        session.commit()
        session.refresh(s2)
        if [f for f in (s2.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]:
            _fail(f"a note-free re-commit must clear the commentary badge, got {s2.qa_flags!r}")
        _ok("meta-commentary clears once the note is gone")

        # (c) tag_integrity (the export reviewer's finding) -- and the SCOPING proof:
        # a same-text re-check clears the three stale families while keeping its QE
        # verdict, a still-valid DNT finding, and the separate LANGUAGE verdict.
        s3 = _seg(session, d.id, "Hello world.", 12)
        s3.target_text = "Hola mundo."
        s3.qa_flags = [
            quality.tag_flag("stale: dropped run token"),
            quality.number_flag("stale: number value changed"),
            quality.meta_commentary_flag(quality.META_COMMENTARY_REASON + ": stale note"),
            refusal.nontranslation_flag("mt"),
            {"type": "dnt_violation", "severity": "major", "message": "keep me"},
            {"type": "qe", "severity": "minor", "message": "keep me too"},
        ]
        session.add(s3)
        session.commit()
        out = quality.commit_mt_output(
            session,
            s3,
            "Hola mundo.",
            source="mt",
        )
        session.commit()
        session.refresh(s3)
        left = [f.get("type") for f in (s3.qa_flags or [])]
        for gone in (quality.TAG_FLAG_TYPE, quality.NUMBER_FLAG_TYPE):
            if gone in left:
                _fail(f"a clean commit must clear the stale {gone!r} flag, got {s3.qa_flags!r}")
        reasons = [
            f.get("reason")
            for f in (s3.qa_flags or [])
            if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        ]
        if reasons != [refusal.NONTRANSLATION_REASON]:
            _fail(f"the LANGUAGE verdict must survive the commentary clear, got {s3.qa_flags!r}")
        if "dnt_violation" not in left or "qe" not in left:
            _fail(f"a clean commit must not touch other flag families, got {s3.qa_flags!r}")
        if not out.tm_eligible:
            _fail("a fully clean commit stays TM-eligible")
        _ok("tag_integrity + number_integrity cleared; dnt / qe / language verdict kept")

        # (d) byte-identity: a clean commit on a flag-less segment leaves the column
        # exactly as it was (None), never an empty list.
        s4 = _seg(session, d.id, "Hello world.", 13)
        quality.commit_mt_output(session, s4, "Hola mundo.", source="mt")
        session.commit()
        session.refresh(s4)
        if s4.qa_flags is not None:
            _fail(f"a clean commit must leave an unflagged column as None, got {s4.qa_flags!r}")
        _ok("an unflagged segment's qa_flags column is untouched (still None)")


def main():
    print("verify_number_meta_integrity -- BUG-06 (numbers + meta-commentary)")
    check_fail_before()
    check_commentary_root_cause()
    check_number_findings()
    check_url_carrier_digits_never_leak()
    check_check_segment_signature()
    check_meta_commentary()
    check_choke_point()
    check_review_number_regressions()
    check_review_meta_regressions()
    check_stale_flag_clearing()
    print("verify_number_meta_integrity OK")


if __name__ == "__main__":
    main()
