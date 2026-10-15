"""Generic XML (W3C ITS 2.0 rules), DITA and DocBook filter (format batch 7, xml_its.py).

  identity   build with no translations (or the source texts) returns the exact bytes
  DITA       topic + map: codeph/codeblock protected, conref / translate="no" / draft-comment
             skipped, indexterm and fn nested, navtitle / map title / image alt attributes
  DocBook    5 (namespace) and 4.5 (DOCTYPE): programlisting / screen / remark skipped,
             emphasis inline, footnote nested, xref a point token; lang stamping
  ITS        in-file rules: translateRule (element + attribute), withinTextRule, locNoteRule,
             local its:translate, xml:space="preserve"; a user rules file via ItsXmlFilter(rules)
  tokens     dropped / duplicated / unknown / stray tokens never break well-formedness; a
             dropped nested element (footnote) is kept
  encodings  UTF-8 BOM kept, UTF-16 and Windows-1252 converted to UTF-8, CRLF kept
  hostile    DTD internal subset (billion laughs, XXE) refused, external DTD never loaded,
             deep nesting refused, pre-parse budget and segment cap, unsafe XPath rules refused

Fixtures (hand-written): tests/fixtures/formats/xmlits/task.dita,
tests/fixtures/formats/xmlits/guide.ditamap, tests/fixtures/formats/xmlits/book5.dbk,
tests/fixtures/formats/xmlits/book45.xml, tests/fixtures/formats/xmlits/catalog.xml.

Hermetic + offline. Final line: "XML ITS FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xmlits_")

from lxml import etree

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.filters.xml_its import ItsXmlFilter, XmlFilterError, load_rules
from app.services.segment import _PLACEHOLDER_RE

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "xmlits")


def _read(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


def _texts(filt, data):
    return [u.text for u in filt.parse(data).units]


def _fr(text: str) -> str:
    """A fake translation that keeps every token in place: words get an FR: prefix."""
    out, pos = [], 0
    for m in _PLACEHOLDER_RE.finditer(text):
        chunk = text[pos : m.start()]
        out.append(chunk.replace("a", "à") if chunk.strip() else chunk)
        out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:].replace("a", "à"))
    return "FR:" + "".join(out)


def _translate_all(filt, data, locale=None):
    units = filt.parse(data).units
    mapping = {str(u.p_index): _fr(u.text) for u in units}
    if locale:
        return filt.build_localized(data, mapping, locale)
    return filt.build(data, mapping)


def _wellformed(out: bytes):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    return etree.fromstring(out, parser=parser)


def test_registration():
    exts = set(supported_exts())
    for ext in ("dita", "ditamap", "bookmap", "dbk", "docbook", "xml"):
        assert ext in exts, (ext, "not registered")
    assert get_filter("ditamap").fmt == "dita"
    assert get_filter("bookmap").fmt == "dita"
    assert get_filter("dbk").fmt == "docbook"
    print("  [registration] dita/ditamap/bookmap, dbk and xml resolve OK")


def test_identity_all_fixtures():
    for name, ext in (
        ("task.dita", "dita"),
        ("guide.ditamap", "ditamap"),
        ("book5.dbk", "dbk"),
        ("book45.xml", "xml"),
        ("catalog.xml", "xml"),
    ):
        data = _read(name)
        filt = get_filter(ext)
        units = filt.parse(data).units
        assert units, name
        assert filt.build(data, {}) == data, (name, "no-change build must be byte-identical")
        same = {str(u.p_index): u.text for u in units}
        assert filt.build(data, same) == data, (name, "source-text build must be byte-identical")
    print("  [identity] five fixtures rebuild byte-identical with no or source translations OK")


def test_dita_topic():
    data = _read("task.dita")
    filt = get_filter("dita")
    parsed = filt.parse(data)
    assert parsed.skeleton["dialect"] == "dita"
    texts = [u.text for u in parsed.units]
    assert texts == [
        "Printing a ⟦g1⟧report⟦/g1⟧",
        "Send the report to a printer.",
        "Use the ⟦x1⟧ command or see ⟦x2⟧.⟦x3⟧",
        "printing⟦x1⟧",
        "reports",
        "Press ⟦g1⟧Print⟦/g1⟧ & wait <5> seconds for ⟦x2⟧.",
        "Click ⟦g1⟧OK⟦/g1⟧.",
        "A laser printer",
    ], texts
    joined = " ".join(texts)
    for hidden in ("lpr", "Internal build", "Check with legal", "warning", "report.txt"):
        assert hidden not in joined, (hidden, "must not be extracted")
    assert parsed.units[-1].style == "a11y:img-alt"

    out = _translate_all(filt, data, "fr-FR")
    root = _wellformed(out)
    s = out.decode("utf-8")
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "fr-FR"
    assert '<!DOCTYPE task PUBLIC "-//OASIS//DTD DITA Task//EN" "task.dtd">' in s
    assert "<!-- authoring note: keep this comment -->" in s
    assert "<codeph>lpr -P {0}</codeph>" in s, "codeph content untouched"
    assert '<codeblock xml:space="preserve">lpr -P office\n  report.txt</codeblock>' in s
    assert '<p conref="shared.dita#shared/warning"/>' in s
    assert '<p translate="no">Internal build 42</p>' in s
    assert "<draft-comment>Check with legal</draft-comment>" in s
    assert "FR:Press <uicontrol>FR:Print</uicontrol>" not in s  # tokens are not prefixed
    assert "<uicontrol>Print</uicontrol> &amp; wàit &lt;5&gt; seconds for &product;." in s, s
    assert "<indexterm>FR:printing<indexterm>FR:reports</indexterm></indexterm>" in s, s
    assert 'alt="FR:A làser printer"' in s
    assert '<xref href="other.dita"/>' in s
    print("  [dita] topic: protected code, skipped conref/draft-comment, nested indexterm OK")


def test_dita_map():
    data = _read("guide.ditamap")
    filt = get_filter("ditamap")
    texts = _texts(filt, data)
    assert texts == [
        "User Guide",
        "User Guide",
        "Everything about printing.",
        "Printing a report",
        "Printing",
        "Setting up & testing",
    ], texts
    out = _translate_all(filt, data).decode("utf-8")
    _wellformed(out.encode("utf-8"))
    assert 'navtitle="FR:Printing à report"' in out
    assert "navtitle='FR:Setting up &amp; testing'" in out, "quote style kept, & escaped"
    assert 'href="task.dita"' in out and 'title="FR:User Guide"' in out
    print("  [dita] map: navtitle/title attributes and topicmeta translate OK")


def test_docbook():
    data = _read("book5.dbk")
    filt = get_filter("dbk")
    texts = _texts(filt, data)
    assert texts == [
        "Getting started",
        "Install",
        "Run the ⟦g1⟧installer⟦/g1⟧, then read ⟦x2⟧.⟦x3⟧",
        "Admin rights needed.",
        "Type ⟦x1⟧ when asked.",
    ], texts
    out = _translate_all(filt, data, "de-DE").decode("utf-8")
    root = _wellformed(out.encode("utf-8"))
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "de-DE"
    assert '<programlisting language="sh">./install.sh --prefix /opt</programlisting>' in out
    assert "<remark>TODO: screenshots</remark>" in out
    assert '<emphasis role="strong">instàller</emphasis>' in out
    assert "<footnote><para>FR:Admin rights needed.</para></footnote>" in out

    data45 = _read("book45.xml")
    generic = get_filter("xml")
    parsed = generic.parse(data45)
    assert parsed.skeleton["dialect"] == "docbook", "DocBook 4.5 sniffed from the DOCTYPE"
    assert [u.text for u in parsed.units][-1] == "Back up ⟦x1⟧ every ⟦g2⟧day⟦/g2⟧."
    out45 = _translate_all(generic, data45, "ja-JP").decode("utf-8")
    assert '<book lang="ja-JP">' in out45, "DocBook 4.x uses lang"
    assert "<screen>$ app backup --all</screen>" in out45
    assert "<filename>/etc/app.conf</filename>" in out45
    print("  [docbook] 5 and 4.5: code skipped, inline/nested tags, lang stamping OK")


def test_its_rules_in_document():
    data = _read("catalog.xml")
    filt = get_filter("xml")
    parsed = filt.parse(data)
    units = parsed.skeleton["units"]
    assert [u["text"] for u in units] == [
        "Desk lamp",
        "A ⟦g1⟧bright⟦/g1⟧ lamp⟦x2⟧for your desk.",
        "Line one\n    indented   line",
    ], units
    assert units[1]["note"] == "Max 80 characters."
    assert units[0]["style"] == "attr:label"
    out = _translate_all(filt, data).decode("utf-8")
    _wellformed(out.encode("utf-8"))
    assert "<sku>L-100-BLK</sku>" in out and '<legal its:translate="no">Made in EU</legal>' in out
    assert 'code="L-1"' in out and 'label="FR:Desk làmp"' in out
    assert "FR:Line one\n    indented   line" in out, "preserved whitespace untouched"
    assert "<its:locNote>Max 80 characters.</its:locNote>" in out
    print("  [its] in-file translateRule/withinTextRule/locNoteRule/preserve OK")


def test_user_rules_file():
    rules = (
        b'<its:rules xmlns:its="http://www.w3.org/2005/11/its" version="2.0">'
        b'<its:translateRule selector="//note" translate="no"/>'
        b'<its:translateRule selector="//item/@title" translate="yes"/>'
        b'<its:withinTextRule selector="//em" withinText="yes"/>'
        b"</its:rules>"
    )
    filt = ItsXmlFilter(rules=rules)
    data = b'<doc><item title="Hello"><p>A <em>big</em> day</p><note>skip me</note></item></doc>'
    assert _texts(filt, data) == ["Hello", "A ⟦g1⟧big⟦/g1⟧ day"]
    assert _texts(ItsXmlFilter(), data) == ["A", "big", "day", "skip me"]
    bad = [
        (b"<rules/>", "root"),
        (
            b'<its:rules xmlns:its="http://www.w3.org/2005/11/its" version="2.0">'
            b'<its:translateRule selector="//a[" translate="no"/></its:rules>',
            "invalid XPath",
        ),
        (
            b'<its:rules xmlns:its="http://www.w3.org/2005/11/its" version="2.0">'
            b'<its:translateRule selector="//a[//b]" translate="no"/></its:rules>',
            "not allowed inside a predicate",
        ),
        (
            b'<its:rules xmlns:its="http://www.w3.org/2005/11/its" version="2.0">'
            b'<its:translateRule selector="//a[b[c[d]]]" translate="no"/></its:rules>',
            "two deep",
        ),
        (
            b'<its:rules xmlns:its="http://www.w3.org/2005/11/its" version="2.0">'
            b'<its:translateRule selector="//a" translate="maybe"/></its:rules>',
            "yes or no",
        ),
        (
            b'<its:rules xmlns:its="http://www.w3.org/2005/11/its" version="2.0">'
            + b'<its:translateRule selector="//a" translate="no"/>' * 101
            + b"</its:rules>",
            "more than 100",
        ),
    ]
    # Gate: quadratic selector / pointer shapes (//*/following::* took 45 s at 4000 elements).
    its_open = b'<its:rules xmlns:its="http://www.w3.org/2005/11/its" version="2.0">'
    for sel, needle in [
        (b"//*/following::*", "following and preceding"),
        (b"//p/preceding-sibling::*", "following and preceding"),
        (b"//p[../q]", "not allowed inside a predicate"),
        (b"//p[parent::*/q]", "not allowed inside a predicate"),
        (b"//p[/doc/q = 'x']", "not allowed inside a predicate"),
        (b"//p[count(/doc/*) > 1]", "not allowed inside a predicate"),
    ]:
        bad.append(
            (
                its_open
                + b'<its:translateRule selector="'
                + sel
                + b'" translate="no"/></its:rules>',
                needle,
            )
        )
    for ptr in (b"//p", b"/doc/p", b"descendant::p", b"../following-sibling::p"):
        bad.append(
            (
                its_open + b'<its:locNoteRule selector="//p" locNotePointer="' + ptr + b'"/>'
                b"</its:rules>",
                "pointer",
            )
        )
    for raw, needle in bad:
        try:
            load_rules(raw)
        except XmlFilterError as exc:
            assert needle in str(exc), (needle, str(exc))
        else:
            raise AssertionError(f"rules accepted: {raw[:80]!r}")
    # A literal that merely contains an axis name, and a relative pointer, are fine.
    ok = load_rules(
        its_open + b'<its:translateRule selector="//p[@kind=\'following\']" translate="no"/>'
        b'<its:locNoteRule selector="//p" locNotePointer="../n"/></its:rules>'
    )
    assert len(ok) == 2
    doc = b"<d><g><p>Save</p><n>Button label</n></g></d>"
    filt = ItsXmlFilter(
        rules=its_open + b'<its:locNoteRule selector="//p" locNotePointer="../n"/></its:rules>'
    )
    notes = [u.get("note") for u in filt.parse(doc).skeleton["units"]]
    assert notes[0] == "Button label", notes
    # A pointer runs once per hit: hits x elements is bounded.
    from app import config
    from app.filters.base import InputLimitError

    many = b"<d>" + b"<g><p>t</p><n>x</n></g>" * 40 + b"</d>"
    saved = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 20  # budget 200 * 20 = 4000 < 40 hits x 121 elements
    try:
        filt.parse(many)
    except InputLimitError as exc:
        assert "locNotePointer" in str(exc), str(exc)
    else:
        raise AssertionError("pointer budget not enforced")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = saved
    print("  [its] user rules file applies; malformed or costly rules refused OK")


def test_placeholders_and_cdata():
    filt = get_filter("xml")
    data = (
        b"<r><m>Hello {0}, you have %1$s new {{count}} items</m><c><![CDATA[Use <b> here]]></c></r>"
    )
    texts = _texts(filt, data)
    assert texts == ["Hello {0}, you have %1$s new {{count}} items", "Use <b> here"], texts
    out = filt.build(
        data, {"0": "Hallo {0}, du hast %1$s neue {{count}}", "1": "Nimm <b> ]]> hier"}
    )
    s = out.decode("utf-8")
    assert "<m>Hallo {0}, du hast %1$s neue {{count}}</m>" in s
    assert "<c><![CDATA[Nimm <b> ]]]]><![CDATA[> hier]]></c>" in s, s
    root = _wellformed(out)
    assert root.find("c").text == "Nimm <b> ]]> hier"
    print("  [text] printf/ICU/mustache placeholders survive, CDATA stays CDATA OK")


def test_token_damage_stays_wellformed():
    data = _read("book5.dbk")
    filt = get_filter("docbook")
    k = 2  # "Run the <g1>installer</g1>, then read <x2>.<x3>"
    for bad in (
        "Lancez ⟦g1⟧l'installateur, puis lisez ⟦x2⟧.",  # unclosed g1, dropped x3 (footnote)
        "⟦/g1⟧Lancez ⟦g1⟧ ⟦g1⟧x⟦/g1⟧ ⟦x9⟧ ⟦x2⟧⟦x2⟧ fin",  # stray close, dup, unknown
        "Plain text with < & > only",
    ):
        out = filt.build(data, {str(k): bad})
        root = _wellformed(out)
        s = out.decode("utf-8")
        assert "⟦" not in s and "⟧" not in s, ("token leaked", s)
        assert s.count("<footnote>") == 1, "the footnote is kept exactly once"
        assert root is not None
    print("  [tokens] damaged token streams rebuild well-formed, footnote kept OK")


def test_encodings():
    filt = get_filter("xml")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n<r>\r\n  <p>café</p>\r\n  <q>two</q>\r\n</r>\r\n'
    )
    bom = b"\xef\xbb\xbf" + body.encode("utf-8")
    assert filt.build(bom, {}) == bom, "BOM and CRLF kept"
    out = filt.build(bom, {"1": "deux"})
    assert out.startswith(b"\xef\xbb\xbf") and b"<q>deux</q>\r\n</r>\r\n" in out

    utf16 = body.replace("UTF-8", "UTF-16").encode("utf-16")
    parsed = filt.parse(utf16)
    assert [u.text for u in parsed.units] == ["café", "two"]
    assert parsed.skeleton.get("source_encoding") == "utf-16"
    out16 = filt.build(utf16, {"0": "Kaffee"})
    assert out16.startswith(b'<?xml version="1.0" encoding="UTF-8"?>') and b"<p>Kaffee</p>" in out16

    latin = '<?xml version="1.0" encoding="windows-1252"?><r><p>naïve</p></r>'.encode("cp1252")
    parsed = filt.parse(latin)
    assert [u.text for u in parsed.units] == ["naïve"]
    assert filt.build(latin, {}) == latin  # B-bytes: untouched = as uploaded
    out1252 = filt.build(latin, {"0": "naïve!"})
    assert out1252.decode("utf-8") == '<?xml version="1.0" encoding="UTF-8"?><r><p>naïve!</p></r>'
    print("  [encodings] BOM/CRLF kept; UTF-16 and Windows-1252 exported as UTF-8 OK")


def test_hostile_inputs():
    filt = get_filter("xml")
    laughs = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]><lolz>&lol2;</lolz>'
    )
    xxe = b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r><p>&x;</p></r>'
    for raw in (laughs, xxe):
        try:
            filt.parse(raw)
        except XmlFilterError as exc:
            assert "internal subset" in str(exc), str(exc)
        else:
            raise AssertionError("internal subset accepted")

    ext = b'<!DOCTYPE r SYSTEM "http://127.0.0.1:9/evil.dtd"><r><p>Hi &ent; there</p></r>'
    parsed = filt.parse(ext)
    assert [u.text for u in parsed.units] == ["Hi ⟦x1⟧ there"], "entity kept as a token"
    out = filt.build(ext, {"0": "Salut ⟦x1⟧ toi"}).decode("utf-8")
    assert out == '<!DOCTYPE r SYSTEM "http://127.0.0.1:9/evil.dtd"><r><p>Salut &ent; toi</p></r>'

    for raw, needle in (
        (b"<r><p>unclosed</r>", "not well-formed"),
        (b"<r><p>&nbsp;</p></r>", "not well-formed"),
        (b"<a>" * 150 + b"x" + b"</a>" * 150, "deeper than"),
    ):
        try:
            filt.parse(raw)
        except XmlFilterError as exc:
            assert needle in str(exc), (needle, str(exc))
        else:
            raise AssertionError(f"accepted: {raw[:40]!r}")

    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 5
        many = b"<r>" + b"<p>x</p>" * 6 + b"</r>"
        try:
            filt.parse(many)
        except InputLimitError as exc:
            assert "MAX_DOCUMENT_SEGMENTS" in str(exc)
        else:
            raise AssertionError("segment cap not enforced")
        huge = b"<r>" + b"<b/>" * 300 + b"</r>"
        try:
            filt.parse(huge)
        except InputLimitError as exc:
            assert "far more elements" in str(exc), str(exc)
        else:
            raise AssertionError("pre-parse budget not enforced")
        costly = (
            b'<r xmlns:its="http://www.w3.org/2005/11/its"><its:rules version="2.0">'
            + b'<its:translateRule selector="//b" translate="no"/>' * 6
            + b"</its:rules>"
            + b"<b/>" * 185
            + b"</r>"
        )
        try:
            filt.parse(costly)
        except InputLimitError as exc:
            assert "number of ITS rules" in str(exc), str(exc)
        else:
            raise AssertionError("rules x elements budget not enforced")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [hostile] internal subset refused, external DTD never loaded, caps enforced OK")


def test_generic_localized_lang_only_when_present():
    filt = get_filter("xml")
    data = b"<r><p>Hello</p></r>"
    assert filt.build_localized(data, {"0": "Bonjour"}, "fr-FR") == b"<r><p>Bonjour</p></r>"
    data2 = b'<r xml:lang="en"><p>Hello</p></r>'
    assert filt.build_localized(data2, {}, "fr-FR") == b'<r xml:lang="fr-FR"><p>Hello</p></r>'
    print("  [lang] generic XML only updates an existing xml:lang OK")


if __name__ == "__main__":
    test_registration()
    test_identity_all_fixtures()
    test_dita_topic()
    test_dita_map()
    test_docbook()
    test_its_rules_in_document()
    test_user_rules_file()
    test_placeholders_and_cdata()
    test_token_damage_stays_wellformed()
    test_encodings()
    test_hostile_inputs()
    test_generic_localized_lang_only_when_present()
    print("XML ITS FILTER OK")
