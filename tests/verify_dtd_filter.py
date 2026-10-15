"""Mozilla DTD entity file (.dtd) filter (file-format Batch 11): app/filters/dtd_filter.py.

Round trip identity, translated round trip, entity references kept, predefined and
character references resolved for the translator and escaped once on build, quote
style kept, `%` escaped, parameter / external entities and PE references untouched,
BOM + CRLF, hostile inputs (no expansion, no fetch), refusals and caps.

Hermetic + offline. Final line: "DTD FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_dtdfilter_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

SRC = b"""<!-- This Source Code Form is subject to the terms of the Mozilla Public
   - License, v. 2.0. -->

<!ENTITY % brandDTD SYSTEM "chrome://branding/locale/brand.dtd">
%brandDTD;

<!-- LOCALIZATION NOTE (window.title): &brandShortName; is the product name. -->
<!ENTITY window.title "&brandShortName; Settings">
<!ENTITY save.label 'Save &amp; close'>
<!ENTITY save.accesskey "S">
<!ENTITY quote.text "Say &quot;hi&quot; to &#169; 100&#037;">
<!ENTITY markup.text "Click <html:b>here</html:b> &lt;now&gt;">
<!ENTITY only.ref "&brandShortName;">
<!ENTITY   spaced.label
     "Spaced out"  >
<!ENTITY ext.file SYSTEM "http://example.invalid/evil.dtd">
"""


def _filt():
    f = get_filter("dtd")
    assert f is not None and f.fmt == "dtd"
    return f


def test_units_and_identity():
    assert "dtd" in supported_exts()
    f = _filt()
    units = f.parse(SRC).units
    got = [(u.style, u.text) for u in units]
    assert got == [
        ("dtd:window.title", "&brandShortName; Settings"),
        ("dtd:save.label", "Save & close"),
        ("dtd:save.accesskey", "S"),
        ("dtd:quote.text", 'Say "hi" to © 100%'),
        ("dtd:markup.text", "Click <html:b>here</html:b> &lt;now&gt;"),
        ("dtd:spaced.label", "Spaced out"),
    ], got
    assert f.build(SRC, {}) == SRC
    assert f.build(SRC, {str(u.p_index): u.text for u in units}) == SRC
    print("  [dtd] units + identity round trip (refs, PEs, externals untouched) OK")


def test_translated_round_trip():
    f = _filt()
    de = {
        "0": "&brandShortName;-Einstellungen",
        "1": "Speichern & schließen, 'sofort'",
        "2": "P",
        "3": 'Sag "hallo" zu © 100%',
        "4": "Klicke <html:b>hier</html:b> &lt;jetzt&gt;",
        "5": "Mit Abstand",
    }
    out = f.build(SRC, de).decode("utf-8")
    assert '<!ENTITY window.title "&brandShortName;-Einstellungen">' in out, out
    assert "<!ENTITY save.label 'Speichern &amp; schließen, &apos;sofort&apos;'>" in out, out
    assert '<!ENTITY quote.text "Sag &quot;hallo&quot; zu © 100&#037;">' in out, out
    assert '"Klicke <html:b>hier</html:b> &lt;jetzt&gt;"' in out, out
    assert '<!ENTITY   spaced.label\n     "Mit Abstand"  >' in out, out
    assert '<!ENTITY % brandDTD SYSTEM "chrome://branding/locale/brand.dtd">\n%brandDTD;' in out
    assert '<!ENTITY ext.file SYSTEM "http://example.invalid/evil.dtd">' in out
    again = [u.text for u in f.parse(out.encode()).units]
    assert again == [de[str(i)] for i in range(6)], again
    print("  [dtd] translated round trip escapes once and re-parses identically OK")


def test_xml_wellformed_use():
    """The rebuilt DTD still works as an internal subset (lxml, no network, no DTD load)."""
    from lxml import etree

    f = _filt()
    out = f.build(SRC, {"1": "A & B \"c\" 'd' 50%"}).decode()
    entities = "\n".join(
        line
        for line in out.splitlines()
        if line.startswith("<!ENTITY save") or line.startswith("<!ENTITY quote")
    )
    doc = f"<!DOCTYPE r [\n{entities}\n]><r>&save.label;|&quote.text;</r>"
    parser = etree.XMLParser(resolve_entities=True, no_network=True, load_dtd=False)
    root = etree.fromstring(doc.encode(), parser)
    assert root.text == 'A & B "c" \'d\' 50%|Say "hi" to © 100%', root.text
    print("  [dtd] rebuilt entities expand to the translated text in an XML parser OK")


def test_bom_crlf():
    f = _filt()
    src = b"\xef\xbb\xbf" + b'<!-- c -->\r\n<!ENTITY a "One">\r\n<!ENTITY b "Two">\r\n'
    assert [u.text for u in f.parse(src).units] == ["One", "Two"]
    assert f.build(src, {}) == src
    out = f.build(src, {"1": "Zwei"})
    assert out == b"\xef\xbb\xbf" + b'<!-- c -->\r\n<!ENTITY a "One">\r\n<!ENTITY b "Zwei">\r\n'
    print("  [dtd] BOM and CRLF survive OK")


def test_hostile_and_refusals():
    f = _filt()
    # billion laughs: nothing is ever expanded, the values are just text
    lol = b'<!ENTITY lol "lol">\n<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">\n'
    assert [u.text for u in f.parse(lol).units] == ["lol"]
    for bad, exc in (
        ('<!ENTITY a "café">'.encode("latin-1"), UnsupportedEncodingError),
        (b'\x00\x01<!ENTITY a "x">', UnsupportedEncodingError),
        (b'<!ENTITY a "unterminated>\n', ValueError),
        (b"<!-- never closed", ValueError),
        (b'<!ENTITY a "x" junk>', ValueError),
        (b"stray text", ValueError),
    ):
        try:
            f.parse(bad)
        except exc:
            pass
        else:
            raise AssertionError(f"must refuse {bad[:30]!r}")
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 3
    try:
        many = "".join(f'<!ENTITY e{i} "Value {i}">\n' for i in range(10)).encode()
        try:
            f.parse(many)
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment cap must apply")
        decls = ("<!-- x -->" * 20).encode()
        try:
            f.parse(decls)
        except InputLimitError:
            pass
        else:
            raise AssertionError("declaration budget must apply")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [dtd] no expansion, non-UTF-8 / binary / malformed refused, caps OK")


def test_other_declarations_kept():
    f = _filt()
    src = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!ELEMENT r (#PCDATA)>\n<!ATTLIST r a CDATA "x>y">\n'
        b'<!ENTITY a "Alpha">\n<!ENTITY pub PUBLIC "-//X//EN" "x.dtd">\n'
    )
    assert [u.text for u in f.parse(src).units] == ["Alpha"]
    out = f.build(src, {"0": "Alfa"})
    assert out == src.replace(b'"Alpha"', b'"Alfa"'), out
    print("  [dtd] PI, ELEMENT, ATTLIST and PUBLIC entities kept byte for byte OK")


def main():
    test_units_and_identity()
    test_translated_round_trip()
    test_xml_wellformed_use()
    test_bom_crlf()
    test_hostile_and_refusals()
    test_other_declarations_kept()
    print("DTD FILTER OK")


if __name__ == "__main__":
    main()
