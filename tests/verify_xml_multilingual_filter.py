"""Multilingual XML (app/filters/xml_multilingual.py, reached through the generic .xml filter).

  identity   no translations (and source texts) rebuild byte-identical, localized or not
  attr       xml:lang siblings: an empty <text xml:lang="fr"/> is opened and filled, a missing
             fr sibling is created after the group with the source indentation (id dropped),
             de untouched; fr-FR asked writes into the file's bare fr
  element    <en>/<fr> children: an existing fr replaced, a missing one created; prefixed names
  xpath      <?loc-multilingual?> PI with {lang} XPath, en_US style kept (fr_FR), ja untouched
  in place   build() without a target replaces the source nodes (the classic contract)
  tokens     inline <b> is a paired token; damaged token streams stay well-formed
  config     constructor dict / JSON, explicit source, English-only file with create, create=no,
             enabled=no falls back to ITS; every invalid config refused with a plain message
  detection  a single group, a record of <to>/<id>/<body>, and ordinary XML stay ITS
  encodings  UTF-8 BOM kept, UTF-16 converted, CRLF kept
  scale      20 000 created targets: one linear splice, unit order kept (gate)
  hostile    DTD internal subset refused, pre-parse budget and segment cap, XPath cost checks,
             target language code never reaches an XPath unvalidated, xpath target budget

Fixtures (hand-written): tests/fixtures/formats/mulixml/catalog-attr.xml, items-element.xml,
xpath-pi.xml. Hermetic + offline. Final line: "XML MULTILINGUAL FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mulixml_")

from lxml import etree

from app import config
from app.filters import get_filter
from app.filters.base import InputLimitError
from app.filters.xml_its import ItsXmlFilter, XmlFilterError
from app.filters.xml_multilingual import build_multilingual, load_config, plan_multilingual

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "mulixml")
FIXTURES = ("catalog-attr.xml", "items-element.xml", "xpath-pi.xml")


def _read(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


def _xx(units) -> dict:
    return {str(u.p_index): "XX " + u.text for u in units}


def _wellformed(out: bytes):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    return etree.fromstring(out, parser=parser)


def _refused(fn, needle: str, exc=XmlFilterError):
    try:
        fn()
    except exc as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError(f"not refused: {needle}")


def test_identity():
    filt = get_filter("xml")
    for name in FIXTURES:
        data = _read(name)
        parsed = filt.parse(data)
        assert parsed.skeleton["dialect"] == "multilingual", name
        same = {str(u.p_index): u.text for u in parsed.units}
        assert filt.build(data, {}) == data, name
        assert filt.build(data, same) == data, name
        assert filt.build_localized(data, {}, "fr-FR") == data, name
    print("  [identity] three fixtures byte-identical with no / source translations OK")


CATALOG_FR = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Product strings, one entry per message, every language as a sibling -->
<catalog xml:lang="en" version='2'>
  <entry id="welcome">
    <text xml:lang="en" id="t1">Welcome to <b>Northwind</b>, {0}!</text>
    <text xml:lang="de" id="t2">Willkommen bei <b>Northwind</b>, {0}!</text>
    <text xml:lang="fr">Bienvenue chez <b>Northwind</b>, {0} !</text>
  </entry>
  <entry id="save">
    <text xml:lang="en">Save &amp; close</text>
    <text xml:lang="de">Speichern &amp; schliessen</text>
    <text xml:lang="fr">Enregistrer &amp; fermer</text>
  </entry>
  <entry id="count">
    <text xml:lang="en">You have %1$s new messages</text>
  </entry>
  <entry id="skip">
    <text xml:lang="en">   </text>
    <text xml:lang="de">   </text>
  </entry>
</catalog>
"""


def test_attr_layout():
    filt = get_filter("xml")
    data = _read("catalog-attr.xml")
    parsed = filt.parse(data)
    ml = parsed.skeleton["multilingual"]
    assert ml["mode"] == "attr" and ml["source"] == "en", ml
    assert ml["lang_attr"] == "xml:lang" and ml["languages"] == ["en", "de", "fr"], ml
    texts = [u.text for u in parsed.units]
    assert texts == [
        "Welcome to ⟦g1⟧Northwind⟦/g1⟧, {0}!",
        "Save & close",
        "You have %1$s new messages",
    ], texts
    tx = {"0": "Bienvenue chez ⟦g1⟧Northwind⟦/g1⟧, {0} !", "1": "Enregistrer & fermer"}
    out = filt.build_localized(data, tx, "fr-FR")
    assert out.decode("utf-8") == CATALOG_FR, out.decode("utf-8")
    # unit 2 translated too: a missing fr is created after the group, indentation copied
    out = filt.build_localized(data, {**tx, "2": "Vous avez %1$s nouveaux messages"}, "fr-FR")
    assert (
        '    <text xml:lang="en">You have %1$s new messages</text>\n'
        '    <text xml:lang="fr">Vous avez %1$s nouveaux messages</text>\n  </entry>'
    ) in out.decode("utf-8")
    # a new language: created in every group, after the last sibling; id/xml:id dropped
    out = filt.build_localized(data, _xx(parsed.units), "it").decode("utf-8")
    assert (
        '<text xml:lang="de" id="t2">Willkommen bei <b>Northwind</b>, {0}!</text>\n'
        '    <text xml:lang="it">XX Welcome to <b>Northwind</b>, {0}!</text>'
    ) in out, out
    assert '<text xml:lang="fr"/>\n    <text xml:lang="it">XX Save &amp; close</text>' in out
    assert out.count('xml:lang="it"') == 3 and 'id="t1"' in out and out.count('id="t') == 2
    _wellformed(out.encode("utf-8"))
    print("  [attr] fill empty target, create missing sibling, fr-FR -> file's fr OK")


ITEMS_FR = """<?xml version="1.0" encoding="utf-8"?>
<items>
\t<item key="menu.file">
\t\t<en>File</en>
\t\t<fr>Fichier (nouveau)</fr>
\t</item>
\t<item key="menu.open">
\t\t<en>Open <![CDATA[recent]]> files</en>
\t\t<fr>Ouvrir les fichiers récents</fr>
\t</item>
\t<item key="menu.quit">
\t\t<en>Quit {{app}}</en>
\t\t<fr>Quitter {{app}}</fr>
\t\t<!-- fr missing on purpose -->
\t</item>
</items>
"""


def test_element_layout():
    filt = get_filter("xml")
    data = _read("items-element.xml")
    parsed = filt.parse(data)
    assert parsed.skeleton["multilingual"]["mode"] == "element"
    assert [u.text for u in parsed.units] == ["File", "Open recent files", "Quit {{app}}"]
    out = filt.build_localized(data, {"0": "Fichier (nouveau)", "2": "Quitter {{app}}"}, "fr")
    assert out.decode("utf-8") == ITEMS_FR, out.decode("utf-8")
    # namespace-prefixed language elements keep their prefix
    pre = (
        b'<r xmlns:l="urn:l"><g><l:en>One</l:en><l:de>Eins</l:de></g>'
        b"<g><l:en>Two</l:en><l:de>Zwei</l:de></g></r>"
    )
    out = filt.build_localized(pre, {"1": "Deux"}, "fr")
    assert out == (
        b'<r xmlns:l="urn:l"><g><l:en>One</l:en><l:de>Eins</l:de></g>'
        b"<g><l:en>Two</l:en><l:de>Zwei</l:de><l:fr>Deux</l:fr></g></r>"
    ), out
    print("  [element] replace existing, create missing (prefix kept) OK")


def test_xpath_pi():
    filt = get_filter("xml")
    data = _read("xpath-pi.xml")
    parsed = filt.parse(data)
    ml = parsed.skeleton["multilingual"]
    assert ml["mode"] == "xpath" and ml["source"] == "en_US", ml
    out = filt.build_localized(data, {"0": "Bonjour", "1": "Au revoir"}, "fr-FR").decode("utf-8")
    assert (
        '<msg name="greet"><v loc="en_US">Hello</v><v loc="fr_FR">Bonjour</v>'
        '<v loc="ja_JP">こんにちは</v></msg>'
    ) in out, out
    assert '<msg name="bye"><v loc="en_US">Goodbye</v><v loc="fr_FR">Au revoir</v></msg>' in out
    out = filt.build_localized(data, {"0": "Konnichiwa"}, "ja-JP").decode("utf-8")
    assert '<v loc="ja_JP">Konnichiwa</v></msg>' in out and "こんにちは" not in out
    assert out.count("<v ") == 3  # nothing created for the untranslated unit
    print("  [xpath] PI rule, en_US style kept, existing ja replaced OK")


def test_in_place_build():
    filt = get_filter("xml")
    data = _read("items-element.xml")
    out = filt.build(data, {"0": "Fichier"}).decode("utf-8")
    assert "<en>Fichier</en>\n\t\t<fr>Fichier</fr>" in out, out
    assert out.replace("<en>Fichier</en>", "<en>File</en>").encode("utf-8") == data
    print("  [in place] build() without a target rewrites the source nodes only OK")


def test_token_damage():
    filt = get_filter("xml")
    data = _read("catalog-attr.xml")
    for bad in (
        "⟦/g1⟧Bienvenue ⟦g1⟧chez",
        "Bienvenue ⟦g1⟧⟦g1⟧ ⟦g9⟧ ⟦x4⟧",
        "<b>raw</b> & ]]> stuff",
        "",
    ):
        out = filt.build_localized(data, {"0": bad}, "fr")
        root = _wellformed(out)
        frs = root.xpath("//entry[@id='welcome']/text[@xml:lang='fr']")
        assert len(frs) == 1, bad
    print("  [tokens] damaged token streams stay well-formed OK")


def test_config():
    data = _read("catalog-attr.xml")
    # explicit source: German becomes the source language
    filt = ItsXmlFilter(multilingual={"source": "de"})
    texts = [u.text for u in filt.parse(data).units]
    assert texts == ["Willkommen bei ⟦g1⟧Northwind⟦/g1⟧, {0}!", "Speichern & schliessen"], texts
    # JSON bytes, element mode on an English-only file (explicit: no detection threshold)
    only = b"<r><i><en>Yes</en></i></r>"
    filt = ItsXmlFilter(multilingual=b'{"mode": "element", "source": "en"}')
    assert [u.text for u in filt.parse(only).units] == ["Yes"]
    assert (
        filt.build_localized(only, {"0": "Oui"}, "fr") == b"<r><i><en>Yes</en><fr>Oui</fr></i></r>"
    )
    # create = no: a missing target is not created
    filt = ItsXmlFilter(multilingual={"mode": "element", "source": "en", "create": "no"})
    assert filt.build_localized(only, {"0": "Oui"}, "fr") == only
    # PI with enabled = no: back to plain ITS (every text is a unit)
    off = data.replace(b"<catalog", b'<?loc-multilingual enabled="no"?>\n<catalog', 1)
    units = get_filter("xml").parse(off)
    assert units.skeleton["dialect"] != "multilingual" and len(units.units) == 9
    # explicit config that finds nothing is a plain refusal
    _refused(lambda: ItsXmlFilter(multilingual={"source": "pt"}).parse(data), "no source-language")
    for cfg, needle in (
        ({"colour": "red"}, "unknown key"),
        ({"mode": "rows"}, "mode must be"),
        ({"source": "en'] | //*"}, "not a language code"),
        ({"lang_attr": "a b"}, "not an attribute name"),
        ({"create": "maybe"}, "create must be yes or no"),
        ({"mode": "xpath", "source": "en", "source_xpath": "//v"}, "{lang} placeholder"),
        (
            {
                "mode": "xpath",
                "source_xpath": "//v[@l='{lang}']",
                "target_xpath": "../v[@l='{lang}']",
            },
            "needs a source",
        ),
        (
            {
                "source": "en",
                "source_xpath": "//v[@l='{lang}']",
                "target_xpath": "//v[@l='{lang}']",
            },
            "pointer must be relative",
        ),
        (
            {
                "source": "en",
                "source_xpath": "//v[@l='{lang}']/following::v",
                "target_xpath": "../v[@l='{lang}']",
            },
            "following and preceding",
        ),
        (
            {
                "source": "en",
                "source_xpath": "//v[.//w[@l='{lang}']]",
                "target_xpath": "../v[@l='{lang}']",
            },
            "inside a predicate",
        ),
        (b"{not json", "not valid JSON"),
        (b"[1]", "must be an object"),
        ("x" * 30_000, "larger than 20 KB"),
        ({"source": 5}, "must be text"),
    ):
        _refused(lambda cfg=cfg: load_config(cfg), needle)
    # a bad PI is refused the same way at parse
    bad_pi = b'<?loc-multilingual mode="rows"?><r/>'
    _refused(lambda: get_filter("xml").parse(bad_pi), "mode must be")
    print("  [config] dict / JSON / PI configs, create=no, enabled=no, refusals OK")


def test_detection_negatives():
    filt = get_filter("xml")
    one_group = b'<r><g><t xml:lang="en">Hi</t><t xml:lang="de">Hallo</t></g></r>'
    assert filt.parse(one_group).skeleton["dialect"] != "multilingual"
    record = (
        b"<log><msg><to>Ann</to><id>Bob</id><body>Hello</body></msg>"
        b"<msg><to>Cy</to><id>Di</id><body>Bye</body></msg></log>"
    )
    assert filt.parse(record).skeleton["dialect"] != "multilingual"
    plain = b'<doc xml:lang="en"><p>One</p><p>Two</p></doc>'
    assert filt.parse(plain).skeleton["dialect"] != "multilingual"
    # two groups, two languages each: detected; the source is the language in most groups
    two = (
        b'<r><g><t lang="de">Hallo</t><t lang="en">Hi</t></g>'
        b'<g><t lang="en">Bye</t></g><g><t lang="en">Yes</t><t lang="de">Ja</t></g></r>'
    )
    sk = filt.parse(two).skeleton
    assert sk["dialect"] == "multilingual" and sk["multilingual"]["source"] == "en", sk
    assert sk["multilingual"]["lang_attr"] == "lang"
    print("  [detection] single group, records and plain XML stay ITS; lang attr detected OK")


def test_encodings():
    filt = get_filter("xml")
    data = _read("items-element.xml")
    bom = b"\xef\xbb\xbf" + data
    assert filt.build(bom, {}) == bom
    out = filt.build_localized(bom, {"2": "Quitter"}, "fr")
    assert out.startswith(b"\xef\xbb\xbf") and b"<fr>Quitter</fr>" in out
    crlf = data.replace(b"\n", b"\r\n")
    out = filt.build_localized(crlf, {"2": "Quitter"}, "fr")
    assert b"<en>Quit {{app}}</en>\r\n\t\t<fr>Quitter</fr>\r\n" in out, out
    assert out.replace(b"\r\n\t\t<fr>Quitter</fr>", b"") == crlf
    u16 = data.decode("utf-8").replace('encoding="utf-8"', 'encoding="UTF-16"').encode("utf-16")
    parsed = filt.parse(u16)
    assert parsed.skeleton.get("source_encoding"), parsed.skeleton
    out = filt.build_localized(u16, {"2": "Quitter"}, "fr")
    assert out.startswith(b'<?xml version="1.0" encoding="UTF-8"?>') and b"<fr>Quitter</fr>" in out
    print("  [encodings] BOM kept, CRLF kept, UTF-16 converted OK")


def test_hostile():
    filt = get_filter("xml")
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b'<r><g><t xml:lang="en">&b;</t><t xml:lang="de">x</t></g>'
        b'<g><t xml:lang="en">y</t><t xml:lang="de">z</t></g></r>'
    )
    _refused(lambda: filt.parse(bomb), "internal subset")
    xxe = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r/>'
    _refused(lambda: filt.parse(xxe), "internal subset")
    # an unvalidated language code never reaches the XPath / the output
    mp = plan_multilingual(_read("xpath-pi.xml"))
    _refused(lambda: build_multilingual(mp, {"0": "x"}, "fr'] | //*[@a='"), "not a language code")
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 2
        _refused(lambda: filt.parse(_read("catalog-attr.xml")), "", InputLimitError)
        many = b"<r>" + b'<g><t xml:lang="en">a</t><t xml:lang="de">b</t></g>' * 30 + b"</r>"
        _refused(lambda: filt.parse(many), "MAX_DOCUMENT_SEGMENTS", InputLimitError)
        config.MAX_DOCUMENT_SEGMENTS = 20
        # xpath mode: sources x siblings bounded before the per-source target rule runs
        wide = (
            b'<?loc-multilingual source="en" source-xpath="/r/v[@l=\'{lang}\']" '
            b"target-xpath=\"../v[@l='{lang}']\"?><r>" + b'<v l="en">a</v>' * 70 + b"</r>"
        )
        _refused(lambda: filt.parse(wide), "too many nodes", InputLimitError)
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [hostile] internal subset, code injection, caps, xpath budget refused OK")


def test_scale_linear_build():
    """Gate: 20 000 created targets build in one linear splice (a per-edit string rebuild
    was quadratic: minutes on a file this size), in unit order, every target present."""
    import time

    n = 20000
    row = b'  <g><t xml:lang="en">Text %d</t><t xml:lang="de">T %d</t></g>\n'
    data = b"<r>\n" + b"".join(row % (i, i) for i in range(n)) + b"</r>\n"
    filt = get_filter("xml")
    units = filt.parse(data).units
    assert len(units) == n, len(units)
    t0 = time.monotonic()
    out = filt.build_localized(data, {str(u.p_index): "F" + u.text for u in units}, "fr")
    took = time.monotonic() - t0
    assert took < 20, f"build took {took:.1f} s"
    root = _wellformed(out)
    lang = "{http://www.w3.org/XML/1998/namespace}lang"
    frs = [e.text for e in root.iter("t") if e.get(lang) == "fr"]
    assert frs == [f"FText {i}" for i in range(n)], frs[:3]
    print(f"  [scale] {n} created targets in one linear splice ({took:.1f} s) OK")


if __name__ == "__main__":
    test_identity()
    test_attr_layout()
    test_element_layout()
    test_xpath_pi()
    test_in_place_build()
    test_token_damage()
    test_config()
    test_detection_negatives()
    test_encodings()
    test_hostile()
    test_scale_linear_build()
    print("XML MULTILINGUAL FILTER OK")
