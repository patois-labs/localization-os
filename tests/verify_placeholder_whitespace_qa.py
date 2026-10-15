"""QW-B regression lock: real-placeholder parity (LC-7) + whitespace/punctuation and
consistency-lite (QA-1), including every fix from the adversarial gate.

  LC-7 -- real placeholders ({0} {name} %s %1$d) were detected (the editor "tags" badge)
      but never persisted or gated at the sole write seam, so a dropped %s shipped as clean
      mt. quality.placeholder_violation: an exact MULTISET comparison at commit_mt_output,
      a mismatch -> NEEDS_REVIEW + TM-ineligible under a placeholder_integrity qa_flag.
      Conservative: reordering silent, printf position normalized (%1$s==%s), complex ICU
      not matched, %-glued-to-letter rejected, empty target ABSTAINS, URLs/HTML excluded.

  QA-1 whitespace -- interior double space + target-added leading/trailing whitespace,
      editor-live AND soft-gated at the seam behind real-MT lang_guard and NOT on full-text
      bodies. Own `whitespace` family, MINOR, tm-neutral. Excluded from LQA scoring.

  QA-1 terminal punctuation -- editor-only, ONE-DIRECTIONAL (drop only), script-guarded,
      quote/bracket-abstaining. Excluded from LQA scoring.

  QA-1 consistency-lite -- qa.inconsistent_segment_ids, one pass; informational.

Behavioral, hermetic (throwaway LOC_TMS_DATA_DIR, MockProvider, no keys, no network). The
comparators are asserted DIRECTLY (a pre-fix tree lacks these functions, so the suite errors
there -- the fail-before is intrinsic, no git-blob load / silent-skip). ASCII-only output;
final line 'verify_placeholder_whitespace_qa OK'.
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_qwb_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus
from app.services import glossary, lqa, qa, quality, segment


def _fail(msg):
    raise AssertionError(msg)


def _ok(label):
    print("  ok - " + label)


# ---------------------------------------------------------------------------
# A. LC-7 -- the placeholder comparator (quality.placeholder_violation).
# ---------------------------------------------------------------------------
def check_placeholder_comparator():
    print("A. quality.placeholder_violation (LC-7)")
    pv = quality.placeholder_violation

    if "%s" not in pv("You have %s items and {n}", "Tienes {n} elementos"):
        _fail("a dropped %s must be flagged and named")
    _ok("a dropped %s / {name} is flagged and named")

    for src, tgt, label in (
        ("{a} then {b}", "{b} luego {a}", "brace reorder"),
        ("%s and %s", "%2$s y %1$s", "printf positional reorder (legit technique)"),
        ("Value %d for %s", "%s: %d", "mixed reorder"),
    ):
        if pv(src, tgt):
            _fail(f"reordering must be silent ({label}): {src!r} -> {tgt!r}")
    _ok("reordering / printf positional variants are silent (multiset, position-normalized)")

    # the false-positive classes the posture note commits to keeping quiet
    clean = [
        ("50%off now", "50%de descuento", "'%' literal glued to a letter is not a specifier"),
        ("100%sure", "100%seguro", "'%sure' is not a printf %s"),
        ("20 % a year", "20 % al ano", "percent-space-letter is NOT a specifier (reviewer M6)"),
        (
            "{count, plural, one {# item} other {# items}}",
            "{count, plural, one {# articulo} other {# articulos}}",
            "ICU inner-text translated",
        ),
        ("Visit https://acme.test/en/docs", "Visita https://acme.test/es/docs", "localized URL"),
        ("Click <b>Save</b>", "Haz clic en <b>Guardar</b>", "HTML tag (out of the soft gate)"),
        ('<img alt="Cat">', '<img alt="Gato">', "translated alt attribute"),
        ("Hi {name}", "Hola {name}", "clean"),
        ("Has {name} and %s", "", "EMPTY target abstains (reviewer H2)"),
        ("Has {name}", "   ", "whitespace-only target abstains (reviewer H2)"),
        ("", "", "empty pair"),
    ]
    for src, tgt, label in clean:
        if pv(src, tgt):
            _fail("FALSE POSITIVE (" + label + f"): {src!r} -> {tgt!r}")
    _ok("%%-literals / percent-space / ICU / URLs / HTML / empty targets stay silent")

    if not pv("Welcome {name}", "Bienvenido {nombre}"):
        _fail("a translated placeholder NAME must be caught")
    if not pv("Value: %d", "Valor: %s"):
        _fail("a printf type change %d -> %s must be caught")
    if pv("Value: %d", "Valor: %1$d"):
        _fail("adding an explicit position to the same specifier must be silent")
    if pv("Set %05d and %-5d", "Poner %-5d y %05d"):
        _fail("printf width/flags (%05d %-5d) must round-trip cleanly (reviewer M5 classes)")
    _ok("translated name / printf type-change caught; positioning + width/flags clean")

    # #689: Python named placeholders are one token each (gettext python-format, Django).
    py_src = "Hello %(name)s, you have %(count)d messages (%(total).2f MB)"
    for tgt, label in (
        ("Bonjour, vous avez %(count)d messages (%(total).2f Mo)", "dropped %(name)s"),
        ("Bonjour %(nom)s, vous avez %(count)d messages (%(total).2f Mo)", "renamed name"),
        ("Bonjour %(name)s, vous avez %(count)s messages (%(total).2f Mo)", "type change"),
    ):
        if not pv(py_src, tgt):
            _fail(f"a Python named placeholder change must be flagged ({label}): {tgt!r}")
    if "%(name)s" not in pv(py_src, "Bonjour, vous avez %(count)d messages (%(total).2f Mo)"):
        _fail("the dropped %(name)s must be named in the reason")
    for tgt in (
        "Bonjour %(name)s, vous avez %(count)d messages (%(total).2f Mo)",
        "%(count)d messages pour %(name)s (%(total).2f Mo)",
    ):
        if pv(py_src, tgt):
            _fail(f"a kept / reordered %(name)s must be silent: {tgt!r}")
    if pv("Rate (approx)", "Taux (env.)") or pv("50%(approx)", "50%(env.)"):
        _fail("a parenthesis that is not %(name)<conversion> must stay silent")
    # gate: Python accepts (and ignores) an h/l/L length modifier: still one token
    if pv("Got %(n)ld rows", "%(n)ld Zeilen") or not pv("Got %(n)ld rows", "%(m)ld Zeilen"):
        _fail("%(n)ld (length modifier) must be one placeholder token")
    tags = segment.extract_inline_tags("Hi %(name)s and %(n)05d")
    if tags != ["%(name)s", "%(n)05d"]:
        _fail(f"the editor tags badge must list %(name)s whole: {tags!r}")
    if glossary.mask_protected_spans("Delete %(name)s now") != "Delete" + " " * 10 + "now":
        _fail("glossary matching must not look inside %(name)s")
    bomb = "%(a)" + "0" * 20000
    t0 = time.perf_counter()
    pv(bomb + "!", bomb + "!")
    if time.perf_counter() - t0 > 0.5:
        _fail("the %(name) placeholder regex backtracks quadratically")
    _ok("Python %(name)s: drop / rename / type change caught; kept or reordered silent")

    # #692: `%%` is an escaped percent; a placeholder escaped into literal text is a drop.
    live = "Uploaded %(size).1f MB of %(total).1f MB"
    for src, tgt, label in (
        (live, "%%(total).1f MB, %(size).1f MB", "live ja: %(total).1f -> %%(total).1f"),
        ("You have %d files", "Sie haben %%d Dateien", "printf %d -> %%d"),
        ("Hi %s", "Hallo %%s", "printf %s -> %%s"),
        # live fr-FR: the escape lost, `% d` now parses as a conversion (Python raises)
        (
            "%(percent)d%% of %(total)s files uploaded",
            "%(percent)d% des fichiers %(total)s",
            "live fr: %% -> %",
        ),
        ("Battery at %d%%", "Batterie a %d %", "printf %% -> %"),
    ):
        if not pv(src, tgt):
            _fail(f"an escaped placeholder must be flagged ({label}): {tgt!r}")
    if "%(total).1f" not in pv(live, "%%(total).1f MB, %(size).1f MB"):
        _fail("the escaped %(total).1f must be named in the reason")
    for src, tgt in (
        ("100%% of %(name)s", "100%% de %(name)s"),  # literal %% kept both sides
        ("%d%% done", "%d%% fait"),
        ("%%%d left", "%%%d restants"),  # literal % then a real %d
        ("50%", "50 %"),
    ):
        if pv(src, tgt):
            _fail(f"a literal %% must stay silent: {src!r} -> {tgt!r}")
    if quality._placeholder_multiset("100%% %d", extended=True) != quality._placeholder_multiset(
        "100%% %d"
    ):
        _fail("the extended (Jev) scan must read %% like the plain one")
    _ok("%% escapes: %d -> %%d, %(n)s -> %%(n)s, %% -> % caught; literal %% silent")

    # #707: .NET / Python `{{` brace escapes and i18next / Mustache `{{name}}`.
    for src, tgt, label in (
        (
            'Paste a JSON object such as {{"name": "{0}"}} into the box.',
            'Fugen Sie ein JSON-Objekt wie {"name": "{0}"} in das Feld ein.',
            "live de: {{ undoubled (string.Format throws)",
        ),
        ("The file {0} has {1} unmatched {{ characters.", "Fichier {0}: {1} { non apparies.", "{{"),
        ("You have {{count}} items", "Vous avez {count} elements", "i18next {{count}} -> {count}"),
        ("Hi {{name}}", "Hallo {{Name}}", "i18next name translated"),
        ("Hi {name}", "Hallo {{name}}", "a brace escape added"),
    ):
        if not pv(src, tgt):
            _fail(f"a brace escape / mustache change must be flagged ({label}): {tgt!r}")
    if "{{count}}" not in pv("You have {{count}} items", "Vous avez {count} elements"):
        _fail("the dropped {{count}} must be named in the reason")
    for src, tgt in (
        ('Paste {{"name": "{0}"}} here.', '{{ "name": "{0}" }} hier.'),
        ("Use {{FirstName}} for {0}.", "Verwenden Sie {{FirstName}} fur {0}."),
        ("Set {{a, b}} for {0}", "{0}: {{a, b}}"),
        ("{{{0}}}", "{{{0}}}"),
        # an ICU branch that opens with an argument is not a brace escape
        (
            "{n, plural, =1{One file} other{{n} files}}",
            "{n, plural, =1{Un fichier} other{{n} fichiers}}",
        ),
    ):
        if pv(src, tgt):
            _fail(f"kept brace escapes must stay silent: {src!r} -> {tgt!r}: {pv(src, tgt)!r}")
    _ok("{{ }} escapes and {{name}}: undoubled / renamed caught; kept or ICU silent (#707)")

    # #718: printf length modifiers (iOS / C / gettext `%ld`, `%lu`, `%zu`) and Qt / Windows
    # positional `%1` .. `%99` were invisible: a dropped one or a type change passed.
    for src, tgt, want in (
        ("%@ and %ld items", "%@ und Artikel", "%ld"),
        ("Uploaded %lu of %lu", "Hochgeladen %d von %d", "%lu"),
        ("Retry in %zu s (%lld)", "Erneut in %zu s", "%lld"),
        ("%2$lu photos by %1$@", "Fotos von %1$@", "%lu"),
        ("%1 of %2 files", "%1 von Dateien", "%2"),
        ("%L1 bytes free", "Bytes frei", "%L1"),
    ):
        if want not in pv(src, tgt):
            _fail(f"a dropped / changed {want} must be flagged: {tgt!r}: {pv(src, tgt)!r}")
    for src, tgt in (
        ("%1 of %2 files", "%2 Dateien von %1"),
        ("%2$lu photos by %1$@", "%1$@ hat %2$lu Fotos"),
        ("Battery at %ld%%", "Akku bei %ld%%"),
        ("%5d items, %10.2f EUR", "%5d Artikel, %10.2f EUR"),
        ("Version %1.%2", "Version %1.%2"),
        ("50%lower and a%20b", "50%niedriger und a%20b"),
        ("Save 20% today", "Heute 20 % sparen"),
    ):
        if pv(src, tgt):
            _fail(f"must stay silent: {src!r} -> {tgt!r}: {pv(src, tgt)!r}")
    _ok("printf length modifiers and Qt %1..%99 are placeholders (#718)")

    # #709: ICU structure (live de-DE Angular XLIFF: select -> wahlen, other -> andere).
    sel = "{role, select, admin {Site admin} editor {Page editor} other {Guest user} }"
    for tgt, want in (
        (
            "{role, wahlen, admin {Site Admin} editor {Seiten Editor} andere {Gast hier} }",
            "missing",
        ),
        ("{role, select, admin {Admin site} editor {Editeur page} autre {Invite ici} }", "autre"),
        ("{role, select, admin {Admin site} other {Invite ici} }", "dropped editor"),
    ):
        if want not in pv(sel, tgt):
            _fail(f"an ICU change must be flagged naming {want!r}: {tgt!r}: {pv(sel, tgt)!r}")
    plu = "{count, plural, =0 {No files at all} other {# files in the folder}}"
    if "dropped other" not in pv(plu, "{count, plural, =0 {Keine Dateien} many {# Dateien}}"):
        _fail("a plural that loses `other` must be flagged")
    for src, tgt in (
        (sel, "{role, select, admin {Admin site} editor {Editeur page} other {Invite ici} }"),
        # CLDR categories added or dropped per language: silent
        (plu, "{count, plural, =0 {Aucun fichier} one {# fichier} many {# fichiers} other {# f}}"),
        ("{n, plural, one {# post here} other {# posts here}}", "{n, plural, other {# Beitrage}}"),
        # offset and a nested argument
        (
            "{n, plural, offset:1 =0 {nobody yet} other {{name} and # others}}",
            "{n, plural, offset:1 =0 {personne ici} other {{name} et # autres}}",
        ),
        ("Plain, text with {0} and a comma", "Texte simple, avec {0}"),
    ):
        if pv(src, tgt):
            _fail(f"a kept ICU structure must stay silent: {tgt!r}: {pv(src, tgt)!r}")
    _ok("ICU header, select keys, plural other and =N changes caught; categories silent (#709)")
    # gate: the ICU walk is linear. The first version took 7 s on 24,000 characters of
    # repeated unclosed headers and grew quadratically (a long key run too).
    for label, icu_bomb in (
        ("unclosed headers", "{a, select, " * 16000),
        ("long key run", "{a, select, " + "x" * 190000 + " k{b} other{c}}"),
        ("deep nesting", "{a, select, k{" * 15000 + "}" * 15000),
    ):
        t0 = time.perf_counter()
        pv(icu_bomb + " {0}", icu_bomb + " {0}")
        dt = time.perf_counter() - t0
        if dt > 1.0:
            _fail(f"the ICU walk took {dt:.3f}s on {len(icu_bomb)} chars ({label}): not linear")
    _ok("ICU walk linear on unclosed headers, a long key run and deep nesting (#709 gate)")

    # #716: the engine wrote a fixed 1 for # in a plural `one` branch (live ru-RU
    # `one {Vybran(a) 1 fajl}`, in Cyrillic). Wrong where `one` also covers other counts (ru 21,
    # fr 0) that no `=N` branch takes first; German / Japanese `one` is exactly 1. Text is
    # transliterated to keep the file ASCII.
    from types import SimpleNamespace

    def _loc(locale):
        return SimpleNamespace(target_locale=locale, document_id=None)

    files = "{n, plural, =0 {No files} one {# file selected} other {# files selected}}"
    plain = "{n, plural, one {# file selected} other {# files selected}}"
    fixed = (
        ("ru-RU", files, "{n, plural, =0 {Net} one {Vybran 1 fajl} other {Vybrano # fajlov}}"),
        ("uk-UA", files, "{n, plural, =0 {Nemaye} one {Vybrano 1 fajl} other {Vybrano # fajliv}}"),
        ("fr-FR", plain, "{n, plural, one {1 fichier choisi} other {# fichiers choisis}}"),
        ("pt-BR", plain, "{n, plural, one {1 arquivo} other {# arquivos}}"),
    )
    for loc, src, tgt in fixed:
        got = pv(src, tgt, segment=_loc(loc))
        if "one branch has no #" not in got:
            _fail(f"a fixed 1 in `one` must be flagged for {loc}: {got!r}")
    invite = "{n, plural, one {an invitation} other {# invitations}}"
    for loc, src, tgt in (
        ("de-DE", files, "{n, plural, =0 {Keine} one {1 Datei} other {# Dateien}}"),
        ("ja-JP", files, "{n, plural, =0 {Nashi} one {1-tsu no fairu} other {#-ko no fairu}}"),
        ("ru-RU", files, "{n, plural, =0 {Net} one {Vybran # fajl} other {Vybrano # fajlov}}"),
        # French `one` is 0 and 1, and the =0 branch takes 0 first (live fr-FR): fine
        ("fr-FR", files, "{n, plural, =0 {Aucun} one {1 fichier} other {# fichiers}}"),
        # the source `one` shows no count: a fixed word is fine
        ("ru-RU", invite, "{n, plural, one {priglashenie} other {# priglashenij}}"),
    ):
        got = pv(src, tgt, segment=_loc(loc))
        if got:
            _fail(f"must stay silent for {loc}: {tgt!r}: {got!r}")
    if pv(files, fixed[0][2]):
        _fail("no segment / no locale: the #716 rule cannot judge and must stay silent")
    # #716 gate: the export's rung 3 (#711: held placeholder damage ships the SOURCE) does not
    # act on this wording finding: the held ru target is still the one exported.
    from app.models import SegStatus
    from app.services.export import segment_tag_damage

    held = SimpleNamespace(
        source_text=files,
        target_text=fixed[0][2],
        status=SegStatus.NEEDS_REVIEW,
        qa_flags=[],
        target_locale="ru-RU",
        document_id=None,
    )
    if segment_tag_damage(held):
        _fail(f"export must not swap a #716 wording finding for the source: {held!r}")
    held.target_text = "Vybrano {n} fajlov"  # the plural header is gone: #709
    if not segment_tag_damage(held):
        _fail("export rung 3 still acts on a broken ICU structure (#709/#711)")
    _ok("a fixed 1 for # in `one` flagged where `one` covers other counts (#716)")

    # #717: Java MessageFormat. Live ru-RU wrote one `}` too many after a choice argument
    # (MessageFormat throws "Unmatched braces"); fr-FR emptied the `0#` branch and moved its text
    # in front of the argument. Both were stored as clean MT. (Text transliterated: ASCII file.)
    files = "There {0,choice,0#are no files|1#is one file|1<are {0,number,integer} files} here."
    for tgt, want in (
        ("V papke {0,choice,0#net fajlov|1#odin fajl|1<est {0,number,integer} fajlov}}.", "braces"),
        (
            "Il n''y a aucun fichier{0,choice,0#|1#il y a un fichier|1<{0,number,integer} f} ici.",
            "branch 0# emptied",
        ),
        ("Es {0,choice,0#keine|1<{0,number,integer} Dateien} hier.", "limits changed"),
        ("Es gibt {0,number,integer} Dateien hier.", "{0,choice} missing"),
    ):
        if want not in pv(files, tgt):
            _fail(f"a MessageFormat change must be flagged naming {want!r}: {pv(files, tgt)!r}")
    for src, tgt in (
        (
            files,
            "Hay {0,choice,0#no archivos|1#un archivo|1<hay {0,number,integer} archivos} aqui.",
        ),
        # the engine's stray `>` inside a branch is text (cosmetic, live de-DE): silent
        (files, "Es {0,choice,0#gibt keine|1#gibt eine|1<gibt {0,number,integer} Dateien>} hier."),
        # a quoted MessageFormat brace leaves the source unbalanced: not judged
        ("It''s '{'literal'}' and '{'", "C''est '{'litteral'}' et"),
        # .NET doubled braces stay balanced (#707)
        ('{{"name": "{0}"}}', '{{"Name": "{0}"}}'),
        ("Plain text without braces", "Texte sans accolades }"),
    ):
        if pv(src, tgt):
            _fail(f"must stay silent: {tgt!r}: {pv(src, tgt)!r}")
    # #717 gate: the choice walk is linear. Each head walked its whole body, so 10,000 nested
    # choice arguments (170k chars) re-walked the inner text per level (minutes, synchronous).
    nested = "{a,choice,0#x|1#" * 10_000 + "y" + "}" * 10_000
    branchy = "{a,choice,0#" + "|" * 100_000 + "}"
    for label, bomb in (("nested", nested), ("branches", branchy)):
        t0 = time.perf_counter()
        pv(bomb, bomb)
        dt = time.perf_counter() - t0
        if dt > 2.0:
            _fail(f"the choice walk took {dt:.3f}s on {len(bomb)} chars ({label}): not linear")
    # a nested choice inside a branch is still judged at its own level
    inner_src = "{0,choice,0#none|1#{1,choice,0#a|1#b}}"
    if "emptied" not in pv(inner_src, "{0,choice,0#keine|1#{1,choice,0#|1#b}}"):
        _fail("an emptied branch of a nested choice must be flagged")
    _ok("MessageFormat brace balance and choice limits / emptied branches caught (#717)")
    # Unlike the #716 wording finding, these break the message at run time (MessageFormat
    # throws) or change what it says: a held target exports its SOURCE (#711 rung 3).
    from app.services import export

    held = Segment(
        source_text=files,
        target_text="V papke {0,choice,0#net fajlov|1#odin fajl|1<est {0,number,integer} fajlov}}.",
        target_locale="ru-RU",
        status=SegStatus.NEEDS_REVIEW,
    )
    if "braces" not in export.segment_tag_damage(held):
        _fail("a held unbalanced MessageFormat target must export its source (#711 rung 3)")
    _ok("a held #717 target exports its source (runtime damage, not wording)")

    # #719: raw HTML in a resource string (JSON / YAML / properties values). Live ar-SA dropped
    # the href of both links (`<a href="/terms">` -> `<a>`), stored as clean MT; live hi-IN
    # (xlsx cell text) doubled closing tags. (Text transliterated: ASCII file.)
    links = 'Agree to our <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.'
    for tgt, want in (
        ("Wafiq ala <a>al-shurut</a> wa <a>al-khususiya</a>.", 'dropped <a href="/privacy">'),
        ('Wafiq ala <a href="/terms">al-shurut</a> wa al-khususiya.', "dropped </a>"),
        ('Wafiq <a href="/terms">x</a></a> wa <a href="/privacy">y</a>.', "added </a>"),
        ('Wafiq <a href="/terms" class="x">x</a> wa <a href="/privacy">y</a>.', 'class="x"'),
    ):
        if want not in pv(links, tgt):
            _fail(f"an HTML tag change must be flagged naming {want!r}: {pv(links, tgt)!r}")
    for src, tgt in (
        (links, 'Wafiq <a href="/privacy">y</a> wa <a href="/terms">x</a>.'),  # moved: fine
        (links, "Wafiq <A HREF='/terms'>x</A> wa <a href=\"/privacy\">y</a>."),  # spelling
        # translatable attributes may change; <br> and <br/> are the same element
        ('<img src="a.png" alt="Logo"> Home<br>', '<img src="a.png" alt="Logo DE" /> Start<br/>'),
        # not HTML elements: CLI metavariables and plain comparisons are text
        ("Usage: git add <pathspec>", "Verwendung: git add <Pfadangabe>"),
        ("a < b and c > d", "a < b und c > d"),
        # the source's own markup is malformed: not judged
        ("<b>Bold text</> and more", "<b>Futu</b> und mehr"),
        # gate: a line break added or dropped is a layout choice, not damage (a held target
        # would ship its source under #711)
        ("First line<br>second line", "Erste Zeile zweite Zeile"),
        ("One long line here", "Eine lange<br/>Zeile<wbr>hier"),
    ):
        if pv(src, tgt):
            _fail(f"must stay silent: {tgt!r}: {pv(src, tgt)!r}")
    _ok("HTML tags in resource strings: dropped / added tags and changed href caught (#719)")

    # #725: every tag intact, one `>` too many after a closing tag (live th-TH SRT / WebVTT
    # cue, live ja-JP JSON string; text transliterated: ASCII file). #719's tag multiset was
    # silent and the file showed a literal `>`.
    cue = "<i>This line is in italics,</i>\nand this one <b>is bold</b>."
    plan = "You have used <b>{used}</b> of <b>{total}</b> projects on the <i>Starter</i> plan."
    for src, tgt in (
        (cue, "<i>banthat ni pen tua iang,</i>\nlae banthat ni <b>pen tua na</b>>"),
        (plan, "<i>Starter</i>>puran de, <b>{total}</b> no uchi <b>{used}</b> o shiyo shimashita."),
        (plan, "<i>Starter</i> <<b>{total}</b> no uchi <b>{used}</b>."),
        # gate: the escaped stray is the same visible character
        (cue, "<i>banthat ni pen tua iang,</i>\nlae banthat ni <b>pen tua na</b>&gt;"),
    ):
        if "stray" not in pv(src, tgt):
            _fail(f"a stray angle bracket next to a tag must be flagged: {tgt!r}: {pv(src, tgt)!r}")
    for src, tgt in (
        (cue, "<i>banthat ni pen tua iang,</i>\nlae banthat ni <b>pen tua na</b>"),
        # the source's own `>` kept, or replaced by an arrow: fine
        ("Open <b>File</b> > <b>Save</b>.", "Oeffnen Sie <b>Datei</b> > <b>Speichern</b>."),
        ("Open <b>File</b> > <b>Save</b>.", "Oeffnen Sie <b>Datei</b> → <b>Speichern</b>."),
        # no standard tags in the source: not judged (a translated metavariable)
        ("Press <now> to save", "Appuyez sur <maintenant>> pour enregistrer"),
    ):
        if pv(src, tgt):
            _fail(f"must stay silent: {tgt!r}: {pv(src, tgt)!r}")
    _ok("a stray '<' / '>' the engine adds next to intact HTML tags is caught (#725)")

    # #711 gate: a one-word ICU branch body is message text, not a {name} placeholder. It
    # held every correct translation of a one-word select branch (live fr `other {Lecteur}`),
    # and a held placeholder mismatch now exports the SOURCE.
    sel = "{VAR_SELECT, select, admin {Administrator} editor {Editor} other {Viewer} }"
    for src, tgt in (
        (sel, "{VAR_SELECT, select, admin {Administrateur} editor {Redacteur} other {Lecteur} }"),
        (
            "{g, select, male {He} female {She} other {They}} left",
            "{g, select, male {Er} female {Sie} other {Sie}} ist weg",
        ),
        ("{n, plural, =0 {None} other {Some}}", "{n, plural, =0 {Keine} other {Einige}}"),
    ):
        if pv(src, tgt):
            _fail(f"a translated one-word ICU branch must stay silent: {tgt!r}: {pv(src, tgt)!r}")
    for src, tgt, label in (
        (
            "{n, plural, one {# file} other {{n} files}}",
            "{n, plural, one {# Datei} other {Dateien}}",
            "a real {n} inside a branch",
        ),
        ("{n, plural, other {{n}}}", "{n, plural, other {n}}", "a branch that is only {n}"),
        ("Hello {Viewer}", "Hallo {Betrachter}", "a {name} outside any ICU argument"),
        (
            sel,
            "{VAR_SELECT, wahlen, admin {Administrator} editor {Editor} andere {Betrachter} }",
            "the live de keyword translation",
        ),
    ):
        if not pv(src, tgt):
            _fail(f"must still be flagged ({label}): {tgt!r}")
    _ok("one-word ICU branches are text, real arguments inside branches still counted (#711 gate)")

    # reviewer M5: no catastrophic backtracking on the sole synchronous write seam.
    bomb = "%" + "0" * 20000
    t0 = time.perf_counter()
    pv(bomb + "x", bomb + "x")
    dt = time.perf_counter() - t0
    if dt > 0.5:
        _fail(f"placeholder regex took {dt:.3f}s on 20k zeros -- quadratic backtracking (M5)")
    _ok("no quadratic backtracking: %d-char pathological input in %.4fs" % (len(bomb), dt))  # noqa: UP031


# ---------------------------------------------------------------------------
# B. QA-1 -- whitespace + punctuation comparators (editor-live).
# ---------------------------------------------------------------------------
def check_whitespace_comparator():
    print("B. qa.whitespace_findings + qa.punctuation_findings (QA-1)")
    wf = qa.whitespace_findings

    def kinds(src, tgt):
        return sorted(f["kind"] for f in wf(src, tgt))

    if kinds("Hello world", "Hola  mundo") != ["double_space"]:
        _fail("an interior double space must flag kind=double_space")
    if kinds("Hello", " Hola") != ["leading_trailing"]:
        _fail("target-added leading whitespace must flag")
    if kinds("Hello", "Hola ") != ["leading_trailing"]:
        _fail("target-added trailing whitespace must flag")
    # source-suppression: a source that itself uses the pattern licenses it in the target
    if wf("Hello  world", "Hola  mundo"):
        _fail("a source with a double space must suppress the double-space flag")
    if wf(" Hello", " Hola"):
        _fail("a source with a leading space must suppress the leading flag")
    for src, tgt in (("Hello", "Hola"), ("Hello world", "Hola mundo"), ("x", "")):
        if wf(src, tgt):
            _fail(f"clean/blank whitespace must be silent: {src!r} -> {tgt!r}")
    # non-vacuous severity assertion (reviewer L7): the finding must EXIST and be minor
    fs = wf("a", "b  c")
    if not fs or any(f["severity"] != "minor" for f in fs):
        _fail(f"whitespace findings must exist and be MINOR, got {fs!r}")
    _ok("double-space + leading/trailing detected; source-suppressed; clean silent; minor")

    pf = qa.punctuation_findings
    # ONE-DIRECTIONAL (reviewer M3): only a DROPPED terminal ender flags.
    if not pf("Hello world.", "Hola mundo"):
        _fail("source ends with a period, target does not -> must flag (drop)")
    for src, tgt, label in (
        ("Hola mundo", "Hello world.", "target ADDS a period (never flag)"),
        ("Ready", "Listo.", "'Ready' -> 'Listo.' adds punctuation"),
        ("(optional)", "(opcional).", "closing bracket + added period"),
        ('"hello."', "hola.", "source ends in a closing quote (real ender hidden)"),
        ("hello.", 'hola"', "target ends in a closing quote"),
        ("Hello world.", "Hola mundo.", "matching terminal punctuation"),
        ("Hello world.", "你好世界。", "CJK ideographic period matches"),
    ):
        if pf(src, tgt):
            _fail(f"punctuation FALSE POSITIVE ({label}): {src!r} -> {tgt!r}")
    # script guard: a Thai target abstains entirely
    thai = "สวัสดี"
    if pf("Hello world.", thai):
        _fail("a Thai target must abstain (script guard)")
    # locale script-guard (reviewer L2): wired through target_locale
    if pf("Hello world.", "xxxx", "th-TH"):
        _fail("a th-TH target_locale must abstain via the locale guard")
    if pf("Steps:", "Pasos"):
        _fail("colon is deliberately NOT a strong terminal ender (must abstain)")
    _ok("terminal-punct: drop-only, CJK judged, quotes/brackets/Thai/colon abstain")


# ---------------------------------------------------------------------------
# C. consistency-lite (qa.inconsistent_segment_ids).
# ---------------------------------------------------------------------------
def check_consistency():
    print("C. qa.inconsistent_segment_ids (QA-1 document scope)")
    rows = [
        (1, "Save", "Guardar"),
        (2, "Save", "Salvar"),
        (3, "Open", "Abrir"),
        (4, "save", "GUARDAR"),
        (5, "Cancel", ""),
    ]
    got = sorted(qa.inconsistent_segment_ids(rows))
    if got != [1, 2, 4]:
        _fail(f"expected segments 1,2,4 inconsistent, got {got!r}")
    _ok("same normalized source + differing target -> all members flagged (casing/ws-normalized)")
    if qa.inconsistent_segment_ids([(1, "Save", "Guardar"), (2, "Save", "Guardar")]):
        _fail("identical targets for the same source must be consistent")
    if qa.inconsistent_segment_ids([(1, "Save", ""), (2, "Save", "Guardar")]):
        _fail("an untranslated (blank-target) row must never create an inconsistency")
    _ok("identical targets / blank targets never flag")


# ---------------------------------------------------------------------------
# D. check_segment / lqa wiring (editor surfaces; LQA EXCLUSION).
# ---------------------------------------------------------------------------
def check_wiring():
    print("D. check_segment surfaces live; LQA scoring EXCLUDES them (reviewer M4)")
    flags = qa.check_segment("Hello world.", "Hola  mundo")
    types = {f["type"] for f in flags}
    if "whitespace" not in types or "punctuation" not in types:
        _fail(f"check_segment must surface whitespace + punctuation live, got {types!r}")
    _ok("check_segment surfaces the QA-1 findings live (editor path)")
    qa.check_segment("a", "b")  # 2-positional-arg legacy signature still works
    _ok("the 2-positional-arg check_segment signature is unchanged")

    # reviewer M4/L5: whitespace + punctuation must NOT reach the LQA structural sum.
    for t in ("whitespace", "punctuation"):
        if t not in lqa._LQA_STRUCTURAL_SKIP:
            _fail(f"{t!r} must be in lqa._LQA_STRUCTURAL_SKIP")
        if t in lqa._QA_TO_MQM:
            _fail(f"{t!r} must NOT be mapped in lqa._QA_TO_MQM (excluded, not scored)")
    errs = lqa._structural_errors("Hello world.", "Hola  mundo")
    dims = {e["dimension"] for e in errs}
    if any(d.startswith("Fluency/Whitespace") or d == "Fluency/Punctuation" for d in dims):
        _fail(f"whitespace/punctuation must not appear in LQA structural errors, got {dims!r}")
    _ok("whitespace + punctuation are excluded from LQA scoring (no pass/fail flip)")


# ---------------------------------------------------------------------------
# E. End to end at the sole write seam (quality.commit_mt_output).
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
    print("E. the write seam (quality.commit_mt_output)")
    init_db()
    with Session(engine) as session:
        p = Project(
            name="QW-B", source_locale="en-US", target_locales=["es-ES"], default_provider="mock"
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="qwb.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)
        from app.web import api_cli

        # (a) LC-7: a dropped %s -- committed, NEEDS_REVIEW, major flag, TM-ineligible.
        s1 = _seg(session, d.id, "You have %s items in your %s.", 0)
        out = quality.commit_mt_output(session, s1, "Tienes elementos.", source="mt")
        session.commit()
        session.refresh(s1)
        if not out.committed or out.flagged:
            _fail("the placeholder gate must be SOFT: text still committed")
        if s1.status != SegStatus.NEEDS_REVIEW:
            _fail(f"a placeholder drop must route to NEEDS_REVIEW, got {s1.status!r}")
        if out.tm_eligible:
            _fail("a placeholder-dropped target must be TM-ineligible")
        if not out.placeholder_violation:
            _fail("CommitOutcome.placeholder_violation must name the divergence")
        pf = [f for f in (s1.qa_flags or []) if f.get("type") == quality.PLACEHOLDER_FLAG_TYPE]
        if len(pf) != 1 or pf[0].get("severity") != "major":
            _fail(f"expected ONE major placeholder_integrity flag, got {s1.qa_flags!r}")
        row = api_cli._segment_detail_row(s1)
        if not any(f["type"] == quality.PLACEHOLDER_FLAG_TYPE for f in row["qa_flags"]):
            _fail("the placeholder flag must survive the headless surfaced-flag filter")
        _ok("LC-7 dropped %s: committed + NEEDS_REVIEW + major flag + TM-ineligible + surfaced")

        # (b) LC-7 stale-clear: a corrected re-commit clears the badge.
        out2 = quality.commit_mt_output(session, s1, "Tienes %s elementos en tu %s.", source="mt")
        session.commit()
        session.refresh(s1)
        if [f for f in (s1.qa_flags or []) if f.get("type") == quality.PLACEHOLDER_FLAG_TYPE]:
            _fail("a corrected commit must clear the placeholder_integrity badge")
        if out2.placeholder_violation or not out2.tm_eligible:
            _fail("the corrected commit must be clean + TM-eligible")
        _ok("placeholder_integrity clears once the placeholders are restored")

        # (c) LC-7 empty-target abstain (reviewer H2): source with placeholders, empty target.
        s2 = _seg(session, d.id, "You have %s items.", 1)
        out3 = quality.commit_mt_output(session, s2, "", source="mt")
        session.commit()
        session.refresh(s2)
        if out3.placeholder_violation or any(
            f.get("type") == quality.PLACEHOLDER_FLAG_TYPE for f in (s2.qa_flags or [])
        ):
            _fail(f"an EMPTY target must NOT get a placeholder flag (H2), got {s2.qa_flags!r}")
        _ok("LC-7 empty target abstains (no phantom 'dropped %s' badge)")

        # (d) LC-7 always-on + MockProvider-safe.
        s3 = _seg(session, d.id, "You have %s items.", 2)
        out4 = quality.commit_mt_output(session, s3, "[es-ES] You have %s items.", source="mt")
        session.commit()
        session.refresh(s3)
        if s3.status != SegStatus.MT or out4.placeholder_violation:
            _fail("the MockProvider echo preserves placeholders -> must be clean")
        _ok("the offline MockProvider echo never trips the placeholder gate (always-on safe)")

        # (e) QA-1 whitespace at the seam: ONLY under lang_guard (real MT).
        s4 = _seg(session, d.id, "Hello world", 3)
        out5 = quality.commit_mt_output(session, s4, "Hola  mundo", source="mt")  # no lang_guard
        session.commit()
        session.refresh(s4)
        if out5.whitespace_violation or any(
            f.get("type") == quality.WHITESPACE_FLAG_TYPE for f in (s4.qa_flags or [])
        ):
            _fail("without lang_guard the whitespace gate must NOT fire (mock safety)")
        _ok("whitespace gate is silent without lang_guard (MockProvider echo safe)")

        s5 = _seg(session, d.id, "Hello world", 4)
        out6 = quality.commit_mt_output(session, s5, "Hola  mundo", source="mt", lang_guard=True)
        session.commit()
        session.refresh(s5)
        if not out6.whitespace_violation:
            _fail("with lang_guard a double space must set whitespace_violation")
        if s5.status != SegStatus.NEEDS_REVIEW:
            _fail("a whitespace slip must route to NEEDS_REVIEW under lang_guard")
        if not out6.tm_eligible:
            _fail("whitespace is tm-NEUTRAL (soft, like char_limit): tm_eligible stays True")
        wsf = [f for f in (s5.qa_flags or []) if f.get("type") == quality.WHITESPACE_FLAG_TYPE]
        if len(wsf) != 1 or wsf[0].get("severity") != "minor":
            _fail(f"expected ONE minor whitespace flag, got {s5.qa_flags!r}")
        if not any(
            f["type"] == quality.WHITESPACE_FLAG_TYPE
            for f in api_cli._segment_detail_row(s5)["qa_flags"]
        ):
            _fail("the whitespace flag must be surfaced headlessly")
        _ok("whitespace under lang_guard: NEEDS_REVIEW + minor flag + TM-eligible + surfaced")

        # (f) L6: a later NON-guarded commit CLEARS a stale whitespace badge.
        quality.commit_mt_output(session, s5, "Hola  mundo otra vez", source="tm")
        session.commit()
        session.refresh(s5)
        if any(f.get("type") == quality.WHITESPACE_FLAG_TYPE for f in (s5.qa_flags or [])):
            _fail(
                "a non-guarded re-commit must CLEAR the stale whitespace badge (L6), got {!r}".format(  # noqa: E501, UP032
                    s5.qa_flags
                )
            )
        _ok("a non-guarded (TM/import) re-commit clears the stale whitespace badge (L6)")


# ---------------------------------------------------------------------------
# F. FULL_TEXT scoping (reviewer H1) + human-save clear semantics (M1).
# ---------------------------------------------------------------------------
def check_full_text_and_human_save():
    print("F. full_text whitespace exclusion (H1) + human-save clear (M1)")
    with Session(engine) as session:
        p = Project(
            name="QW-B FT", source_locale="en-US", target_locales=["es-ES"], default_provider="mock"
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="ft.md", fmt="md", mode="full_text")
        session.add(d)
        session.commit()
        session.refresh(d)

        # H1: a trailing-newline body under lang_guard flags WITHOUT full_text ...
        s1 = _seg(session, d.id, "Hello world", 0)
        out_seg = quality.commit_mt_output(
            session, s1, "Hola mundo\n", source="mt", lang_guard=True
        )
        if not out_seg.whitespace_violation:
            _fail("a trailing newline under lang_guard (non-full-text) must flag")
        # ... and does NOT flag WITH full_text=True (the every-full-text-delivery bug).
        s2 = _seg(session, d.id, "Hello world", 1)
        out_ft = quality.commit_mt_output(
            session,
            s2,
            "Hola mundo\n",
            source="workflow",
            lang_guard=True,
            set_status=False,
            full_text=True,
        )
        session.commit()
        if out_ft.whitespace_violation:
            _fail("full_text=True must EXCLUDE the whitespace seam gate (reviewer H1)")
        if any(f.get("type") == quality.WHITESPACE_FLAG_TYPE for f in (s2.qa_flags or [])):
            _fail("a full-text body must carry no whitespace flag")
        _ok("full_text=True excludes the whitespace seam gate; segmented still flags (H1)")

        # H2 at the seam on a full-text body: an empty body never gets a placeholder badge,
        # and placeholder still runs for a NON-empty full-text body with a real drop.
        s3 = _seg(session, d.id, "Has %s and {n}", 2)
        out_ftp = quality.commit_mt_output(
            session,
            s3,
            "Solo {n}.",
            source="workflow",
            lang_guard=True,
            set_status=False,
            full_text=True,
        )
        if not out_ftp.placeholder_violation:
            _fail(
                "placeholder gate still runs on a non-empty full-text body (only whitespace "
                "is excluded by full_text)"
            )
        _ok("placeholder gate still runs on full-text bodies (only whitespace is excluded)")

        # M1: the human-save clear/merge semantics documents.segment_save uses -- a machine
        # badge is cleared once the human's saved text no longer diverges, and re-merged when
        # it still does. This exercises the exact recompute-then-clear/merge the route calls.
        s4 = _seg(session, d.id, "Has %s items", 3)
        quality.commit_mt_output(session, s4, "Tiene elementos", source="mt")  # machine drop
        session.commit()
        session.refresh(s4)
        if not any(f.get("type") == quality.PLACEHOLDER_FLAG_TYPE for f in (s4.qa_flags or [])):
            _fail("setup: the machine commit must have set a placeholder_integrity badge")
        # human fixes the placeholder -> recompute clean -> clear (route logic)
        fixed = "Tiene %s elementos"
        r = quality.placeholder_violation(s4.source_text, fixed)
        s4.qa_flags = (
            quality.merge_placeholder_flags(s4.qa_flags, r)
            if r
            else quality.clear_placeholder_flags(s4.qa_flags)
        )
        if any(f.get("type") == quality.PLACEHOLDER_FLAG_TYPE for f in (s4.qa_flags or [])):
            _fail("the human-save clear must drop the placeholder badge once the fix lands")
        _ok("human-save recompute clears placeholder_integrity when the fix lands (M1)")

        # M1 (the maintainer-authorized extension): number_integrity gets the SAME human-save clear
        # route (GitHub #42 defect class). The seam sets it with the segment's target locale;
        # the human-save recompute must use that locale too (matching seam semantics).
        from app.web import api_cli

        s5 = _seg(session, d.id, "You have 25 items", 4)  # _seg locale is es-ES
        quality.commit_mt_output(session, s5, "Tienes 25.000 articulos", source="mt")
        session.commit()
        session.refresh(s5)
        if not any(f.get("type") == quality.NUMBER_FLAG_TYPE for f in (s5.qa_flags or [])):
            _fail("setup: the machine commit must set a number_integrity badge for es-ES")
        # STILL-BROKEN human save (locale-aware): the badge PERSISTS (merge).
        broken = "Tienes 25.000 articulos otra vez"
        rn = quality.number_violation(s5.source_text, broken, s5.target_locale)
        s5.qa_flags = (
            quality.merge_number_flags(s5.qa_flags, rn)
            if rn
            else quality.clear_number_flags(s5.qa_flags)
        )
        if not any(
            f["type"] == quality.NUMBER_FLAG_TYPE
            for f in api_cli._segment_detail_row(s5)["qa_flags"]
        ):
            _fail("a still-broken human save must KEEP number_integrity (detail API surface)")
        # FIXED human save: the badge CLEARS, and is gone from the headless detail API.
        fixed_num = "Tienes 25 articulos"
        rn2 = quality.number_violation(s5.source_text, fixed_num, s5.target_locale)
        s5.qa_flags = (
            quality.merge_number_flags(s5.qa_flags, rn2)
            if rn2
            else quality.clear_number_flags(s5.qa_flags)
        )
        if any(f.get("type") == quality.NUMBER_FLAG_TYPE for f in (s5.qa_flags or [])):
            _fail("a corrected human save must CLEAR number_integrity")
        if any(
            f["type"] == quality.NUMBER_FLAG_TYPE
            for f in api_cli._segment_detail_row(s5)["qa_flags"]
        ):
            _fail("the cleared number_integrity must be gone from the detail API surface")
        _ok("human-save recompute clears/keeps number_integrity locale-aware (M1 extension)")


# ---------------------------------------------------------------------------
# G. Surfacing invariants (the api_cli allowlist assert + summary parity).
# ---------------------------------------------------------------------------
def check_full_text_callers():
    """Reviewer round-2 CALLER-SIDE LOCK: every real-MT full-text caller must PASS
    full_text=True, so a trailing-newline-only body lands clean end-to-end -- not merely
    that the seam honours the flag. Exercises each caller function directly."""
    print("H. full-text callers pass full_text=True (trailing newline lands clean)")
    from app.models import VendorDispatch
    from app.services import fulltext_paragraphs, translate, vendor_return

    NL = "Hola mundo\n"  # provider output keeps its trailing newline
    with Session(engine) as session:
        p = Project(
            name="QW-B callers",
            source_locale="en-US",
            target_locales=["es-ES"],
            default_provider="mock",
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="ft2.md", fmt="md", mode="full_text")
        session.add(d)
        session.commit()
        session.refresh(d)

        # (a) translate._apply_result_ft (the plain `doc translate` full-text path). Force
        # lang_guard TRUE so the test is NON-VACUOUS: with a real provider the ONLY thing
        # keeping the trailing newline from flagging is the caller passing full_text=True.
        _orig = translate._lang_guard_for
        try:
            set_pieces("app.services.translate", "_lang_guard_for", lambda res: True)
            s1 = _seg(session, d.id, "Hello world", 0)
            res = {"text": NL, "error": None, "usage": {}, "provider_used": "mock"}
            out = translate._apply_result_ft(session, s1, res, translate._new_stats())
            session.commit()
            session.refresh(s1)
        finally:
            set_pieces("app.services.translate", "_lang_guard_for", _orig)
        if out.whitespace_violation or any(
            f.get("type") == quality.WHITESPACE_FLAG_TYPE for f in (s1.qa_flags or [])
        ):
            _fail("translate._apply_result_ft must pass full_text=True (real-MT full-text)")
        _ok("translate._apply_result_ft passes full_text=True (doc translate path)")

        # (b) vendor_return._admit_units (the `full:0` whole-body return, lang_guard hardcoded
        # True). full_text=True -> clean; full_text=False -> the same body flags: proves the
        # flag both threads through AND matters.
        leg = VendorDispatch(target_locale="es-ES")
        for ft, expect_flag in ((True, False), (False, True)):
            s = _seg(session, d.id, "Hello world", 10 + int(ft))
            units = [{"id": vendor_return._norm_id(s.id), "target": NL}]
            by_id = {vendor_return._norm_id(s.id): (0, s)}
            admitted, flagged, no_op, locked = [], [], [], []
            vendor_return._admit_units(
                session,
                units,
                by_id,
                [""],
                leg,
                p,
                "en-US",
                "vendor",
                admitted,
                flagged,
                no_op,
                locked,
                full_text=ft,
            )
            session.commit()
            session.refresh(s)
            has = any(f.get("type") == quality.WHITESPACE_FLAG_TYPE for f in (s.qa_flags or []))
            if has != expect_flag:
                _fail(
                    f"vendor _admit_units full_text={ft}: whitespace flag={has}, expected {expect_flag}"  # noqa: E501
                )
        _ok("vendor_return._admit_units threads full_text (True clean; False flags -> matters)")

        # (c) fulltext_paragraphs._commit_body (the paragraph-editor human save). lang_guard
        # is off here (human edit), so whitespace never fires -- full_text=True is belt-and-
        # suspenders; assert the full-text human save lands clean.
        s3 = _seg(session, d.id, "Hello world", 20)
        out3 = fulltext_paragraphs._commit_body(session, s3, [NL], provenance="workflow")
        session.commit()
        session.refresh(s3)
        if out3.whitespace_violation or any(
            f.get("type") == quality.WHITESPACE_FLAG_TYPE for f in (s3.qa_flags or [])
        ):
            _fail("_commit_body (full-text human save) must land clean of whitespace")
        _ok("fulltext_paragraphs._commit_body full-text human save is clean")


def check_surfacing_invariants():
    print("G. surfacing allowlists")
    from app.web import api_cli

    for t in (quality.PLACEHOLDER_FLAG_TYPE, quality.WHITESPACE_FLAG_TYPE):
        if t not in api_cli._SEGMENT_SURFACED:
            _fail(f"{t!r} missing from api_cli._SEGMENT_SURFACED")
    if {ft for ft, _ in api_cli._SUMMARY_FAMILIES} != set(api_cli._SEGMENT_SURFACED):
        _fail("_SUMMARY_FAMILIES must itemize every _SEGMENT_SURFACED family")
    # the deliberate asymmetry (whitespace headless-only, not in the editor _SURFACED)
    from app.web import documents as _docs  # noqa: F401

    _ok("placeholder_integrity + whitespace in the headless surfaced set + summary")


def main():
    print("verify_placeholder_whitespace_qa -- LC-7 placeholders + QA-1 whitespace/punct")
    check_placeholder_comparator()
    check_whitespace_comparator()
    check_consistency()
    check_wiring()
    check_choke_point()
    check_full_text_and_human_save()
    check_full_text_callers()
    check_surfacing_invariants()
    print("verify_placeholder_whitespace_qa OK")


if __name__ == "__main__":
    main()
