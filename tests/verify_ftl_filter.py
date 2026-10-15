"""Project Fluent (.ftl) filter (file-format Batch 11): app/filters/fluent_filter.py.

Round trip identity with no changes, translated round trip, select-expression variants
as separate (plural-aware) units, attributes, terms never translated unless configured,
placeables kept, brace / line-start escaping, multiline patterns, CRLF + BOM, refusals
(non-UTF-8, binary, nesting, caps). Fixtures are hand-written here.

Hermetic + offline. Final line: "FTL FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ftlfilter_")

from fluent.syntax import FluentParser, ast

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

SRC = b"""### Resource comment

# Simple message with a variable.
hello = Hello, { $name }!

## Group: brand
-brand-name = Firefox
    .gender = masculine

emails = { $unread ->
    [one] You have one unread email.
   *[other] You have { $unread } unread emails in { -brand-name }.
}

multi =
    First line of the text
    second line { "{" } brace

login-button = Log in
    .title = Click to log in
    .accesskey = L

only-attrs =
    .placeholder = Search the web

numbers = { NUMBER($n, minimumFractionDigits: 2) }
broken = { $x
ref = { hello }
"""


def _filt():
    f = get_filter("ftl")
    assert f is not None and f.fmt == "ftl"
    return f


def _valid(data: bytes) -> list:
    res = FluentParser().parse(data.decode("utf-8-sig"))
    return [type(e).__name__ for e in res.body]


def test_registration():
    assert "ftl" in supported_exts()
    assert get_filter("FTL").fmt == "ftl"
    print("  [ftl] registered as .ftl OK")


def test_units_and_identity():
    f = _filt()
    units = f.parse(SRC).units
    got = [(u.style, u.text) for u in units]
    assert got == [
        ("ftl:hello", "Hello, { $name }!"),
        ("ftl:emails[one]", "You have one unread email."),
        ("ftl:emails[other]", "You have { $unread } unread emails in { -brand-name }."),
        ("ftl:multi", "First line of the text\nsecond line { brace"),
        ("ftl:login-button", "Log in"),
        ("ftl:login-button.title", "Click to log in"),
        ("ftl:login-button.accesskey", "L"),
        ("ftl:only-attrs.placeholder", "Search the web"),
    ], got
    assert [u.p_index for u in units] == list(range(len(units)))
    # no changes: byte identical, also with every unit "translated" to itself
    assert f.build(SRC, {}) == SRC
    assert f.build(SRC, {str(u.p_index): u.text for u in units}) == SRC
    print("  [ftl] units (values, variants, attributes) + identity round trip OK")


def test_translated_round_trip():
    f = _filt()
    units = f.parse(SRC).units
    de = {
        "0": "Hallo, { $name }!",
        "1": "Sie haben eine ungelesene E-Mail.",
        "2": "Sie haben { $unread } ungelesene E-Mails in { -brand-name }.",
        "3": "Erste Zeile des Textes\nzweite Zeile { Klammer",
        "4": "Anmelden",
        "5": "Klicken, um sich anzumelden",
        "6": "A",
        "7": "Im Web suchen",
    }
    out = f.build(SRC, de)
    text = out.decode("utf-8")
    assert "hello = Hallo, { $name }!\n" in text, text
    assert "    [one] Sie haben eine ungelesene E-Mail.\n" in text, text
    assert "   *[other] Sie haben { $unread } ungelesene E-Mails in { -brand-name }.\n" in text
    assert 'multi =\n    Erste Zeile des Textes\n    zweite Zeile {"{"} Klammer\n' in text, text
    assert "    .title = Klicken, um sich anzumelden\n" in text
    assert "-brand-name = Firefox\n    .gender = masculine\n" in text  # term untouched
    assert "broken = { $x\n" in text  # Junk kept byte for byte
    assert "### Resource comment\n" in text and "## Group: brand\n" in text
    kinds = _valid(out)
    assert kinds.count("Junk") == 1, kinds  # only the source's own Junk
    again = f.parse(out).units
    assert [u.text for u in again] == [de[str(i)] for i in range(8)], [u.text for u in again]
    assert len(again) == len(units)
    print("  [ftl] translated round trip re-parses to the same structure OK")


def test_structure_cannot_be_broken():
    f = _filt()
    hostile = "Text { $evil } with } and {\n[one] fake variant\n*[other] x\n.attr = y\n  .z"
    out = f.build(SRC, {"0": hostile})
    kinds = _valid(out)
    assert kinds.count("Junk") == 1, kinds
    res = FluentParser().parse(out.decode())
    msgs = [e for e in res.body if isinstance(e, ast.Message)]
    assert [m.id.name for m in msgs][:2] == ["hello", "emails"], [m.id.name for m in msgs]
    hello = msgs[0]
    assert hello.attributes == [], "a translation must not create attributes"
    unit0 = f.parse(out).units[0].text
    assert unit0 == hostile, repr(unit0)  # Fluent keeps relative indentation
    # a placeable the source did not have is escaped, never live
    assert "{ $evil }" not in [
        out.decode()[p.span.start : p.span.end]
        for p in FluentParser(with_spans=True).parse(out.decode()).body[1].value.elements
        if isinstance(p, ast.Placeable)
    ]
    print("  [ftl] braces, fake variants and attributes in a translation are escaped OK")


def test_terms_only_when_configured():
    f = _filt()
    os.environ["FTL_TRANSLATE_TERMS"] = "1"
    try:
        units = f.parse(SRC).units
        styles = [u.style for u in units]
        assert "ftl:-brand-name" in styles, styles
        assert not any("gender" in s for s in styles), styles
        k = styles.index("ftl:-brand-name")
        out = f.build(SRC, {str(k): "Feuerfuchs"}).decode()
        assert "-brand-name = Feuerfuchs\n    .gender = masculine\n" in out, out
    finally:
        del os.environ["FTL_TRANSLATE_TERMS"]
    assert "ftl:-brand-name" not in [u.style for u in f.parse(SRC).units]
    print("  [ftl] terms untranslated by default, opt-in with FTL_TRANSLATE_TERMS OK")


def test_crlf_and_bom():
    f = _filt()
    src = b"\xef\xbb\xbf" + SRC.replace(b"\n", b"\r\n")
    units = f.parse(src).units
    assert units[3].text == "First line of the text\nsecond line { brace", repr(units[3].text)
    assert f.build(src, {}) == src
    out = f.build(src, {"3": "Eins\nZwei", "0": "Hallo"})
    assert out.startswith(b"\xef\xbb\xbf")
    assert b"multi =\r\n    Eins\r\n    Zwei\r\n" in out, out
    assert b"\r\n\r\n" in out and b"\n\n" not in out.replace(b"\r\n", b"")
    assert b"hello = Hallo\r\n" in out
    print("  [ftl] CRLF line endings and the UTF-8 BOM survive OK")


def test_non_translatable_skipped_but_aligned():
    f = _filt()
    src = b"a = { $only }\nb = Real text\nc = ---\nd = { -brand-name }\ne = More text\n"
    units = f.parse(src).units
    assert [u.text for u in units] == ["Real text", "More text"]
    out = f.build(src, {"0": "Echter Text", "1": "Mehr Text"}).decode()
    assert out == "a = { $only }\nb = Echter Text\nc = ---\nd = { -brand-name }\ne = Mehr Text\n"
    print("  [ftl] placeable-only values skipped, indices aligned OK")


def test_nested_select():
    f = _filt()
    src = (
        b"shared = { $gender ->\n"
        b"    [male] { $n ->\n"
        b"        [one] He shared one photo.\n"
        b"       *[other] He shared { $n } photos.\n"
        b"    }\n"
        b"   *[other] They shared photos.\n"
        b"}\n"
    )
    units = f.parse(src).units
    assert [u.style for u in units] == [
        "ftl:shared[male][one]",
        "ftl:shared[male][other]",
        "ftl:shared[other]",
    ], [u.style for u in units]
    out = f.build(src, {"1": "Er teilte { $n } Fotos.\nzweite Zeile"})
    assert "Junk" not in _valid(out)
    assert b"       *[other] Er teilte { $n } Fotos.\n           zweite Zeile\n" in out, out
    print("  [ftl] nested selects map to one unit per variant path OK")


def test_plural_variants_filled_729():
    """#729 (decision 13): a plural select exported into Polish / Arabic gets a variant
    for each CLDR category the target uses that the file lacks, holding the translated
    `[other]` variant (the shared rule, app/filters/plurals.py), placed before the
    default. A select on a non-plural key (`[male]`) and build() are untouched."""
    f = _filt()
    src = (
        b"files = { $count ->\n"
        b"    [one] { $count } file was deleted.\n"
        b"   *[other] { $count } files were deleted.\n"
        b"}\n"
        b"greet = { $gender ->\n"
        b"    [male] He said hi\n"
        b"   *[other] They said hi\n"
        b"}\n"
    )
    tr = {"0": "{ $count } S-pl", "1": "{ $count } P-pl\nline two", "3": "Oni"}
    out = f.build_localized(src, tr, "pl-PL")
    assert "Junk" not in _valid(out)
    text = out.decode()
    assert (
        "    [one] { $count } S-pl\n"
        "    [few] { $count } P-pl\n       line two\n"
        "    [many] { $count } P-pl\n       line two\n"
        "   *[other] { $count } P-pl\n       line two\n}\n"
    ) in text, text
    assert "[few]" not in text.split("greet")[1], text
    ar = f.build_localized(src, tr, "ar").decode()
    for cat in ("zero", "two", "few", "many"):
        assert f"    [{cat}] {{ $count }} P-pl\n" in ar, ar
    assert f.build_localized(src, tr, "ja") == f.build(src, tr)
    assert b"[few]" not in f.build(src, tr)
    print("  [#729] plural selects get the target's missing variants OK")
    # gate: an ordinal select follows the target's ORDINAL categories (Welsh ordinals
    # zero/one/two/few/many/other; Polish ordinals have only `other`, so nothing is added)
    ordsrc = (
        b'place = { NUMBER($n, type: "ordinal") ->\n    [one] { $n }st\n   *[other] { $n }th\n}\n'
    )
    otr = {"0": "{ $n }-O", "1": "{ $n }-X"}
    pl = f.build_localized(ordsrc, otr, "pl-PL")
    assert pl == f.build(ordsrc, otr), pl
    cy = f.build_localized(ordsrc, otr, "cy").decode()
    assert "Junk" not in _valid(cy.encode())
    for cat in ("zero", "two", "few", "many"):
        assert f"    [{cat}] {{ $n }}-X\n" in cy, cy
    print("  [#729 gate] ordinal selects use the ordinal categories OK")


def test_refusals():
    f = _filt()
    for bad, why in (
        ("hello = café\n".encode("latin-1"), "latin-1"),
        (b"\x00\x01\x02hello = x\x00", "binary"),
    ):
        try:
            f.parse(bad)
        except UnsupportedEncodingError:
            pass
        else:
            raise AssertionError(f"{why} input must be refused")
    deep = ("x = " + "{ " * 200 + "$a" + " }" * 200 + "\n").encode()
    try:
        f.parse(deep)
    except InputLimitError:
        pass
    else:
        raise AssertionError("deep nesting must be refused before the parser")
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 3
    try:
        many = "".join(f"m{i} = Message number {i}\n" for i in range(10)).encode()
        try:
            f.parse(many)
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment cap must apply")
        braces = ("x = " + "{ $a } " * 100 + "\n").encode()
        try:
            f.parse(braces)
        except InputLimitError:
            pass
        else:
            raise AssertionError("placeable budget must apply")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    old_chars = config.MAX_SEGMENT_CHARS
    config.MAX_SEGMENT_CHARS = 1000
    try:
        try:
            f.parse(("x = " + "word " * 400 + "\n").encode())
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment length cap must apply")
    finally:
        config.MAX_SEGMENT_CHARS = old_chars
    print("  [ftl] non-UTF-8, binary, nesting, segment and placeable caps refused OK")


def main():
    test_registration()
    test_units_and_identity()
    test_translated_round_trip()
    test_structure_cannot_be_broken()
    test_terms_only_when_configured()
    test_crlf_and_bom()
    test_non_translatable_skipped_but_aligned()
    test_nested_select()
    test_plural_variants_filled_729()
    test_refusals()
    print("FTL FILTER OK")


if __name__ == "__main__":
    main()
