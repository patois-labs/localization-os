"""Numbered placeholders move freely; Python %(name)s is a placeholder; kept names pass the
wrong-script check (#693, 2026-10-11 decisions 10 and 7a).

#693 (format soak, Wikipedia iOS stringsdict, ja-JP): positional Apple specifiers (`%2$d`,
`%3$@`) were masked as point tokens; the engine moved them for Japanese word order and the tag
gate put them back in SOURCE order (swapping the arguments) and held the string. A numbered
placeholder names its argument, so the target's order is safe.

  tokens     a filter marks a placeholder that names its argument with the free-order point
             kind `p` (`⟦p1⟧`): Apple `%1$@` / `%2$#@v@`, XLIFF `<x id>` / `<ph id>`, JS
             `{name}` `{{x}}` `%(name)s` `%1$s`, PHP `:name` `%1$s`, Windows `%1`. Unnumbered
             `%@` `%s`, tags and XLIFF bpt/ept keep `x` (their order is the argument order).
  gate       quality_common.tag_damage / qa.check_segment judge ORDER only over the other
             tokens; a dropped, added or duplicated `⟦pN⟧` is still damage. The seam stores the
             engine's order, clean MT; an unnumbered reorder is still restored and held.
  build      Apple detokenize, XLIFF rebuild, JS / PHP / RC render map `⟦pN⟧` by number (and
             still read the old `⟦xN⟧` of documents imported before this change).
  raw        raw `%1$s` / `{0}` (Android strings, .strings, JSON) already compare as a multiset.

7a: the wrong-script check ignores Python `%(name)s` placeholders, and does not count Latin
name / code tokens the target keeps verbatim from the source ("TruffleRuby (GraalVM)", a JSP
line) while plain words still count (a copied sentence, a Spanish target, a name the source
does not have stay held).

Hermetic (throwaway LOC_TMS_DATA_DIR, no engine). ASCII output; final line
"QA REORDER NAMES 693 OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_reorder693_")

from sqlmodel import Session

from app.db import engine, init_db
from app.filters import apple_filter, get_filter, js_module_filter, php_array_filter, rc_filter
from app.models import Document, Project, Segment, SegStatus
from app.services import qa, quality, refusal, tag_repair
from app.services.refusal_part2 import wrong_script

P = "⟦p{}⟧".format
X = "⟦x{}⟧".format


def _fail(msg):
    print("FAIL:", str(msg).encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


def test_apple_tokens():
    text, _toks = apple_filter.tokenize("You have reached the limit of %2$d articles for %3$@")
    if text != f"You have reached the limit of {P(1)} articles for {P(2)}":
        _fail(f"[apple] positional specifiers must be free-order tokens: {text!r}")
    text, _toks = apple_filter.tokenize("%1$#@v1@ since %2$@, and %@ of %d")
    if text != f"{P(1)} since {P(2)}, and {X(3)} of {X(4)}":
        _fail(f"[apple] unnumbered specifiers keep x tokens: {text!r}")
    value = "You have reached the limit of %2$d articles per reading list for %3$@"
    ja = f"{P(2)}の読書リストにおける{P(1)}記事の制限に達しました。"
    out = apple_filter.apply_translation(value, ja)
    if out != "%3$@の読書リストにおける%2$d記事の制限に達しました。":
        _fail(f"[apple] build must keep the engine's order with each specifier: {out!r}")
    # a target stored before this change (x tokens) still exports
    old = apple_filter.apply_translation(value, f"{X(2)} de {X(1)}")
    if old != "%3$@ de %2$d":
        _fail(f"[apple] old x-token targets must still build: {old!r}")
    filt = get_filter("stringsdict")
    with open(
        os.path.join(_ROOT, "tests", "fixtures", "formats", "apple", "Localizable.stringsdict"),
        "rb",
    ) as fh:
        data = fh.read()
    units = [u.text for u in filt.parse(data).units]
    want = f"You have {P(1)} in {P(2)} & more"
    if want not in units:
        _fail(f"[apple] stringsdict unit must carry free tokens: {units!r}")
    idx = str(units.index(want))
    built = filt.build(data, {idx: f"{P(2)} に {P(1)} があります"})
    if "<string>%2$#@folders@ に %1$#@messages@ があります</string>" not in built.decode("utf-8"):
        _fail(f"[apple] stringsdict build must follow the target order: {built[:400]!r}")
    print("  [apple] positional -> free tokens, unnumbered keep order, build follows target OK")


def test_gate_order():
    src = f"limit of {P(1)} articles for {P(2)}"
    if quality.tag_damage(src, f"{P(2)} no {P(1)} kiji"):
        _fail("[gate] a free-order reorder must be clean")
    for tgt, want in (
        (f"{P(2)} only", "dropped"),
        (f"{P(1)} {P(1)} {P(2)}", "duplicated"),
        (f"{P(1)} {P(2)} {P(3)}", "added"),
        (f"{X(1)} {P(2)}", "dropped"),
    ):
        r = quality.tag_damage(src, tgt)
        if want not in r:
            _fail(f"[gate] {tgt!r} must be damage naming {want!r}: {r!r}")
    if "reordered" not in quality.tag_damage(f"a {X(1)} b {X(2)}", f"{X(2)} a {X(1)}"):
        _fail("[gate] an unnumbered (x) reorder must still be damage")
    mixed = f"{X(1)} a {P(2)} b {X(3)}"
    if quality.tag_damage(mixed, f"{P(2)} {X(1)} c {X(3)}"):
        _fail("[gate] a free token moving around fixed ones in order must be clean")
    r = quality.tag_damage(mixed, f"{P(2)} {X(3)} c {X(1)}")
    if "reordered" not in r or "p2" in r:
        _fail(f"[gate] a fixed reorder is damage and names only the fixed tokens: {r!r}")
    # paired tokens still keep their nesting
    if not quality.tag_damage(f"⟦g1⟧a {P(2)}⟦/g1⟧", f"{P(2)}⟦/g1⟧a⟦g1⟧"):
        _fail("[gate] crossing a pair must still be damage")
    # editor badge
    fl = qa.check_segment(src, f"{P(2)} no {P(1)} kiji", "ja-JP")
    if any(f.get("type") == "placeholder" for f in fl):
        _fail(f"[gate] editor badge must accept the free reorder: {fl!r}")
    fl = qa.check_segment(f"a {X(1)} b {X(2)}", f"{X(2)} a {X(1)}", "ja-JP")
    if not any("reordered" in f.get("message", "") for f in fl):
        _fail(f"[gate] editor badge must still flag an x reorder: {fl!r}")
    # deterministic repair: a fixed reorder is restored, the free token keeps its slot
    rep = tag_repair.repair(mixed, f"{P(2)} {X(3)} c {X(1)}")
    if rep is None or rep[0] != f"{P(2)} {X(1)} c {X(3)}" or rep[1]:
        _fail(f"[gate] order restore must touch only fixed tokens: {rep!r}")
    # raw numbered placeholders (Android / .strings / JSON) compare as a multiset
    if quality.placeholder_violation(
        "Delete %1$s from %2$s?", "%2$s から %1$s を削除"
    ) or quality.placeholder_violation("{0} of {1}", "{1} の {0}"):
        _fail("[gate] raw numbered placeholders may move")
    print("  [gate] free reorder clean; drops/adds/dups and x reorders still damage OK")


def _seg(session, src, loc="ja-JP", fmt="stringsdict"):
    p = Project(
        name=f"R693-{time.time_ns()}",
        source_locale="en-US",
        target_locales=[loc],
        default_provider="mock",
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    d = Document(project_id=p.id, filename=f"x.{fmt}", fmt=fmt, mode="segmented")
    session.add(d)
    session.commit()
    session.refresh(d)
    seg = Segment(
        document_id=d.id, target_locale=loc, order_index=0, source_text=src, status=SegStatus.NEW
    )
    session.add(seg)
    session.commit()
    session.refresh(seg)
    return seg


def test_seam():
    src = f"You have reached the limit of {P(1)} articles per reading list for {P(2)}"
    ja = f"{P(2)}の読書リストにおける{P(1)}記事の制限に達しました。"
    with Session(engine) as s:
        seg = _seg(s, src)
        quality.commit_mt_output(s, seg, ja, source="mt", lang_guard=True)
        s.commit()
        s.refresh(seg)
        if seg.target_text != ja or seg.status != SegStatus.MT or seg.qa_flags:
            _fail(
                f"[seam] the free reorder must be stored as is, clean: {seg.target_text!r} "
                f"{seg.status} {seg.qa_flags!r}"
            )
        old = _seg(s, f"Read {X(1)} since {X(2)}!")
        quality.commit_mt_output(s, old, f"{X(2)} kara {X(1)} yomimashita", source="mt")
        s.commit()
        s.refresh(old)
        if (
            old.status != SegStatus.NEEDS_REVIEW
            or old.target_text != f"{X(1)} kara {X(2)} yomimashita"
        ):
            _fail(
                "[seam] an x reorder must still be restored and held: "
                f"{old.target_text!r} {old.status}"
            )
    print("  [seam] free reorder stored clean; x reorder restored and held OK")


XLF = b"""<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
 <file original="a" source-language="en" target-language="ja" datatype="plaintext"><body>
  <trans-unit id="u1"><source>Delete <x id="1" ctype="x-file"/> from <ph id="2">%2$s</ph> \
now <bpt id="3">&lt;b&gt;</bpt>bold<ept id="3">&lt;/b&gt;</ept></source></trans-unit>
 </body></file></xliff>
"""


def test_xliff():
    filt = get_filter("xliff")
    units = [u.text for u in filt.parse(XLF).units]
    if units != [f"Delete {P(1)} from {P(2)} now {X(3)}bold{X(4)}"]:
        _fail(f"[xliff] <x id>/<ph id> free, bpt/ept fixed: {units!r}")
    out = filt.build(XLF, {"0": f"{P(2)} kara {P(1)} wo {X(3)}futoji{X(4)}"}).decode("utf-8")
    if '<target><ph id="2">%2$s</ph> kara <x id="1" ctype="x-file"/> wo <bpt id="3">' not in out:
        _fail(f"[xliff] rebuild must follow the target order: {out!r}")
    if quality.tag_damage(units[0], f"{P(2)} kara {P(1)} wo {X(3)}futoji{X(4)}"):
        _fail("[xliff] the reorder of x/ph codes must be clean")
    if not quality.tag_damage(units[0], f"{P(1)} {P(2)} {X(4)}futoji{X(3)}"):
        _fail("[xliff] an ept before its bpt must still be damage")
    # an <x>/<ph> that is one side of a formatting pair keeps its order (Angular, equiv-text)
    with open(
        os.path.join(_ROOT, "tests", "fixtures", "formats", "angular", "messages.xlf"), "rb"
    ) as fh:
        ang = [u.text for u in filt.parse(fh.read()).units]
    if (
        ang[1] != f"Hello, {P(1)}! You have {P(2)} new messages."
        or ang[2] != f"The {X(1)}sky{X(2)} is blue"
    ):
        _fail(f"[xliff] Angular INTERPOLATION free, START_/CLOSE_ fixed: {ang!r}")
    for attrs in (
        'id="1" ctype="bold"',
        'id="1" equiv-text="&lt;i&gt;"',
        'id="1" disp="&lt;i&gt;"',
    ):
        one = XLF.replace(b'<x id="1" ctype="x-file"/>', f"<x {attrs}/>".encode())
        if not filt.parse(one).units[0].text.startswith(f"Delete {X(1)}"):
            _fail(f"[xliff] a markup-side <x {attrs}> must keep x")
    # gate: a <ph> whose native code is an UNNUMBERED argument (`%s`, `%d`, `%@`, `{}`) or a
    # markup side (`<b>` as ph content) keeps its order: moving it swaps the values
    for code in ("%s", "%d", "%@", "{}", "&lt;b&gt;", "&lt;/a&gt;", " %-5.2f "):
        one = XLF.replace(b"%2$s", code.encode())
        if filt.parse(one).units[0].text != f"Delete {P(1)} from {X(2)} now {X(3)}bold{X(4)}":
            _fail(f"[xliff] <ph>{code}</ph> must keep x: {filt.parse(one).units[0].text!r}")
    # a self-closing element (`<br/>`) is a point, not a markup side: it may move
    for code in ("&lt;br/&gt;", "{0}", "%1$s"):
        one = XLF.replace(b"%2$s", code.encode())
        if filt.parse(one).units[0].text != f"Delete {P(1)} from {P(2)} now {X(3)}bold{X(4)}":
            _fail(f"[xliff] <ph>{code}</ph> may move: {filt.parse(one).units[0].text!r}")
    one = XLF.replace(b'<x id="1" ctype="x-file"/>', b'<x id="1" equiv-text="%s"/>')
    if not filt.parse(one).units[0].text.startswith(f"Delete {X(1)}"):
        _fail("[xliff] equiv-text %s must keep x")
    # XLIFF 2.0: the code behind dataRef decides; {0} / %1$s move, %s / <b> keep order
    x2 = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" version="2.0" srcLang="en"'
        b' trgLang="ja"><file id="f"><unit id="u"><originalData>'
        b'<data id="d1">{0}</data><data id="d2">%s</data><data id="d3">&lt;b&gt;</data>'
        b'<data id="d4">%1$s</data></originalData><segment><source>A <ph id="1" dataRef="d1"/>'
        b' B <ph id="2" dataRef="d2"/> C <ph id="3" dataRef="d3"/> D <ph id="4" dataRef="d4"/>'
        b"</source></segment></unit></file></xliff>\n"
    )
    got = [u.text for u in filt.parse(x2).units]
    if got != [f"A {P(1)} B {X(2)} C {X(3)} D {P(4)}"]:
        _fail(f"[xliff] 2.0 dataRef codes decide the kind: {got!r}")
    # linear: one <originalData> lookup table per unit (4000 dataRefs: was ~90 s quadratic)
    n = 4000
    big = x2.decode().replace(
        '<data id="d1">{0}</data>', "".join(f'<data id="d{i}">%s</data>' for i in range(n))
    )
    big = big.replace(
        '<source>A <ph id="1" dataRef="d1"/>',
        "<source>" + "".join(f' w <ph id="{i}" dataRef="d{i}"/>' for i in range(5, n)),
    ).encode()
    t0 = time.perf_counter()
    filt.parse(big)
    if time.perf_counter() - t0 > 5.0:
        _fail(f"[xliff] dataRef lookup must stay linear: {time.perf_counter() - t0:.1f} s")
    print("  [xliff] x/ph codes with ids move freely, bpt/ept and markup sides keep order OK")


def test_script_filters():
    t, imap = js_module_filter._unit_text("Hi {name}, {{count}} of %1$s and %s <b>x</b> %(user)s")
    if t != f"Hi {P(1)}, {P(2)} of {P(3)} and {X(4)} {X(5)}x{X(6)} {P(7)}":
        _fail(f"[js] named / numbered free, %s and tags fixed: {t!r}")
    r = js_module_filter._render(f"{P(7)} {P(1)} {X(4)}", imap, '"', False)
    if r != '"%(user)s {name} %s"':
        _fail(f"[js] render must map free tokens by number: {r!r}")
    t, imap = php_array_filter._unit_text("Hello :name, you have %1$s and %s")
    if t != f"Hello {P(1)}, you have {P(2)} and {X(3)}":
        _fail(f"[php] :name and %1$s free, %s fixed: {t!r}")
    if php_array_filter._render(f"{P(2)} {P(1)} {X(3)}", imap, "'") != "'%1$s :name %s'":
        _fail("[php] render must map free tokens by number")
    pieces, wide = rc_filter._literal_pieces('"Copy %1 to %2!s! (%s)"')
    t, imap, _ = rc_filter._unit_text(pieces)
    if t != f"Copy {P(1)} to {P(2)} ({X(3)})":
        _fail(f"[rc] %1 / %2!s! free, %s fixed: {t!r}")
    if rc_filter._render_literal(f"{P(2)} {P(1)} {X(3)}", imap, wide) != '"%2!s! %1 %s"':
        _fail("[rc] render must map free tokens by number")
    print("  [filters] js / php / rc mark named and numbered placeholders free OK")


NAMES_PASS = [  # (source, kept target, locale): names / code the engine kept verbatim
    ("TruffleRuby (GraalVM)", "TruffleRuby (GraalVM)", "ru-RU"),
    ("TruffleRuby (GraalVM)", "TruffleRuby (GraalVM)", "zh-CN"),
    ('<%= request.getParameter("userName") %>', '<%= request.getParameter("userName") %>', "ru-RU"),
    (
        '<%@ page contentType="text/html; charset=UTF-8" %>',
        '<%@ page contentType="text/html; charset=UTF-8" %>',
        "ja-JP",
    ),
    (
        "JRuby 9.4.5.0 (OpenJDK 64-Bit Server VM 21.0.2)",
        "JRuby 9.4.5.0 (OpenJDK 64-Bit Server VM 21.0.2)",
        "ru-RU",
    ),
    ("Runs on TruffleRuby and GraalVM", "TruffleRuby GraalVM", "zh-CN"),
    ("%(first_name)s %(last_name)s", "%(first_name)s %(last_name)s", "ja-JP"),
    ("Hello %(first_name)s %(last_name)s", "%(first_name)s %(last_name)s", "zh-CN"),
]
NAMES_HELD = [  # real non-translations stay held
    ("Save the file before closing", "Save the file before closing", "ru-RU"),
    ("Install TruffleRuby on GraalVM today", "Install TruffleRuby on GraalVM today", "ru-RU"),
    ("Save the file before closing", "Guardar el archivo antes de cerrar", "ru-RU"),
    ("TruffleRuby (GraalVM)", "HelloWorld FooBarBazQuxCorge", "ru-RU"),  # not in the source
    ("TruffleRuby (GraalVM)", "truffleruby graalvm runtime", "ru-RU"),  # not verbatim, plain
    ("Open the TruffleRuby settings page now", "Open the TruffleRuby settings page now", "ja-JP"),
    ("Run GraalVM", "Run GraalVM and then start the server please", "zh-CN"),
    # gate: an all-caps source has no acronyms to excuse (every word is 2+ capitals)
    ("SAVE YOUR CHANGES BEFORE EXIT", "SAVE YOUR CHANGES BEFORE EXIT", "ru-RU"),
    ("PLEASE RESTART THE APPLICATION", "PLEASE RESTART THE APPLICATION", "ar-SA"),
    # gate: an abbreviation is a word, not code ("e.g." does not excuse the sentence)
    ("e.g. see docs.example.com for more", "e.g. see docs.example.com for more", "ja-JP"),
]
NAMES_PASS_GATE = [  # gate: realistic UI strings that keep product names / acronyms
    ("JSON API v2 (REST)", "JSON API v2 (REST)", "ru-RU"),
    ("PostgreSQL, MySQL, SQLite", "PostgreSQL, MySQL, SQLite", "zh-CN"),
    ("Export to CSV, XLSX or JSON", "CSV, XLSX JSON", "ja-JP"),
]


def test_wrong_script():
    for src, tgt, loc in NAMES_PASS + NAMES_PASS_GATE:
        if wrong_script(tgt, loc, source=src):
            _fail(f"[script] kept names / code must pass for {loc}: {tgt!r}")
    for src, tgt, loc in NAMES_HELD:
        if not wrong_script(tgt, loc, source=src):
            _fail(f"[script] a real non-translation must stay held for {loc}: {tgt!r}")
    # without a source the check is unchanged (except placeholders, which are never content)
    if not wrong_script("TruffleRuby (GraalVM)", "ru-RU"):
        _fail("[script] without a source a long Latin run is still judged")
    if wrong_script("%(first_name)s %(last_name)s", "ja-JP"):
        _fail("[script] Python %(name)s letters are not content")
    if refusal.wrong_script is not wrong_script:
        _fail("[script] the facade must re-export the same function")
    with Session(engine) as s:
        seg = _seg(s, "TruffleRuby (GraalVM)", loc="ru-RU", fmt="md")
        quality.commit_mt_output(s, seg, "TruffleRuby (GraalVM)", source="mt", lang_guard=True)
        s.commit()
        s.refresh(seg)
        if seg.status != SegStatus.MT or any(
            f.get("reason") == refusal.WRONG_SCRIPT_REASON for f in seg.qa_flags or []
        ):
            _fail(f"[script] the seam must keep the kept name clean: {seg.status} {seg.qa_flags!r}")
        bad = _seg(s, "Save the file before closing", loc="ru-RU", fmt="md")
        quality.commit_mt_output(
            s, bad, "Guardar el archivo antes de cerrar", source="mt", lang_guard=True
        )
        s.commit()
        s.refresh(bad)
        if bad.status != SegStatus.NEEDS_REVIEW:
            _fail(f"[script] the seam must still hold a Spanish target for ru: {bad.status}")
    print(
        f"  [script] {len(NAMES_PASS)} kept names pass, {len(NAMES_HELD)} non-translations held OK"
    )


def test_linear():
    hostile = [
        "%(" + "a" * 50000,
        "%(a)" + "0" * 50000,
        "Aa" * 30000,
        "(" * 50000 + "GraalVM",
        "x." * 30000,
    ]
    for h in hostile:
        t0 = time.perf_counter()
        wrong_script(h, "ru-RU", source=h)
        quality.tag_damage(h, h)
        js_module_filter._unit_text(h[:20000])
        if time.perf_counter() - t0 > 1.0:
            _fail(f"[linear] slow on {h[:12]!r}")
    print("  [linear] hostile inputs stay fast OK")


def main():
    init_db()
    test_apple_tokens()
    test_gate_order()
    test_seam()
    test_xliff()
    test_script_filters()
    test_wrong_script()
    test_linear()
    print("QA REORDER NAMES 693 OK")


if __name__ == "__main__":
    main()
