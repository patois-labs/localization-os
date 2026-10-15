# ruff: noqa: E501
"""XSL / XSLT filter (app/filters/xslt_filter.py): literal result text, inline tokens,
attribute and text value templates, byte-faithful round trip, hostile input.

Hand-written fixtures only. Hermetic + offline. Final line: "XSLT FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xslt_")

from lxml import etree

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import EmptyDocumentError, InputLimitError

F = get_filter("xslt")
_SAFE = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)

BASIC = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xsl:stylesheet [
  <!ENTITY nbsp "&#160;">
  <!-- copyright sign -->
  <!ENTITY copy '&#xA9;'>
]>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="html" indent="yes"/>
  <xsl:variable name="mode">compact</xsl:variable>
  <xsl:param name="title" select="'Report'"/>
  <xsl:key name="k" match="item" use="@id"/>
  <xsl:template match="/">
    <html lang="en">
      <body>
        <h1 title="Main {@id} heading">Welcome to the <b>shop</b>, <xsl:value-of select="user/name"/>!</h1>
        <p>Price:&nbsp;<xsl:value-of select="price"/> {0} %1$s {{x}}</p>
        <img src="{@src}" alt="Photo of {{the}} product"/>
        <xsl:if test="count(item) &gt; 0">
          You have <xsl:value-of select="count(item)"/> items.
        </xsl:if>
        <xsl:message terminate="no">Debug only</xsl:message>
        <xsl:comment>Generated file</xsl:comment>
        <p translate="no">ACME-42</p>
        <p><xsl:attribute name="title">Tooltip text</xsl:attribute><xsl:attribute name="class">big</xsl:attribute>Body</p>
        <table><tr><td>A cell</td><td>B cell</td></tr></table>
        <xsl:for-each select="x"><li><xsl:value-of select="."/></li></xsl:for-each>
        <xsl:choose>
          <xsl:when test="a">Yes branch</xsl:when>
          <xsl:otherwise><xsl:text>No branch</xsl:text></xsl:otherwise>
        </xsl:choose>
        <xsl:call-template name="footer">
          <xsl:with-param name="note">raw-code</xsl:with-param>
          <xsl:with-param name="html"><em>Thanks for reading</em></xsl:with-param>
        </xsl:call-template>
        <footer>&copy; 2026 Example</footer>
      </body>
    </html>
  </xsl:template>
</xsl:stylesheet>
"""

BASIC_UNITS = [
    "Welcome to the ⟦g1⟧shop⟦/g1⟧, ⟦x2⟧!",
    "Price:⟦x1⟧⟦x2⟧ {0} %1$s {{x}}",
    "You have ⟦x1⟧ items.",
    "Tooltip text",
    "Body",
    "A cell",
    "B cell",
    "Yes branch",
    "No branch",
    "Thanks for reading",
    "⟦x1⟧ 2026 Example",
    "Main ⟦x1⟧ heading",
    "Photo of {the} product",
]

XSLT3 = """<xsl:stylesheet version="3.0" expand-text="yes"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:map="http://www.w3.org/2005/xpath-functions/map">
  <xsl:template match="order">
    <p>Total: {sum(line/@price)} for {map{'a':1}?a} customer, {{literal}}</p>
    <p xsl:expand-text="no">Raw {braces} stay</p>
    <xsl:text>Line {position()} done</xsl:text>
    <span title="{concat('a}', @x)} items">x</span>
  </xsl:template>
</xsl:stylesheet>
"""

SIMPLIFIED = """<html xsl:version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xml:lang="en">
  <head><title>Expense Report Summary</title></head>
  <body>
    <p>Total Amount: <xsl:value-of select="expense-report/total"/></p>
    <p><img src="a.png" alt="Company logo"/> Inline image here</p>
  </body>
</html>
"""


def _texts(data: bytes):
    return [u.text for u in F.parse(data).units]


def _refused(data: bytes, exc=ValueError, needle: str = "") -> str:
    try:
        F.parse(data)
    except exc as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return str(e)
    raise AssertionError(f"expected refusal for {data[:80]!r}")


def _all(data: bytes, fn) -> dict:
    return {str(u.p_index): fn(u.text) for u in F.parse(data).units}


def test_registration():
    exts = supported_exts()
    assert "xslt" in exts and "xsl" in exts
    assert get_filter("xsl").fmt == "xslt"
    from app.services import mime
    from app.services.connectors import msgraph

    assert msgraph.FORMATS.get("xsl") == "xslt" and msgraph.FORMATS.get("xslt") == "xslt"
    assert "xslt" in str(vars(mime))
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert ".xsl,.xslt," in html
    print("  [reg] xsl/xslt registered, upload accept list, mime and msgraph map OK")


def test_basic_units_and_identity():
    data = BASIC.encode("utf-8")
    pr = F.parse(data)
    assert [u.text for u in pr.units] == BASIC_UNITS, [u.text for u in pr.units]
    styles = [u.style for u in pr.units]
    assert styles[-2:] == ["attr:title", "attr:alt"] and set(styles[:-2]) == {""}, styles
    assert F.build(data, {}) == data
    assert F.build(data, _all(data, lambda t: t)) == data
    print("  [basic] literal text, inline tokens, skips, attributes; identity build OK")


def test_translated_round_trip():
    data = BASIC.encode("utf-8")
    out = F.build(data, _all(data, lambda t: "<" + t + "> & co"))
    root = etree.fromstring(out, _SAFE)
    text = out.decode("utf-8")
    assert 'title="&lt;Main {@id} heading> &amp; co"' in text, text
    assert (
        '&lt;Welcome to the <b>shop</b>, <xsl:value-of select="user/name"/>!&gt; &amp; co</h1>'
        in text
    )
    assert '&lt;Price:&nbsp;<xsl:value-of select="price"/> {0} %1$s {{x}}&gt; &amp; co' in text
    assert 'alt="&lt;Photo of {{the}} product> &amp; co"' in text, text
    # never touched: instructions, XPath, code text, messages, translate="no"
    for keep in (
        '<xsl:variable name="mode">compact</xsl:variable>',
        '<xsl:param name="title" select="\'Report\'"/>',
        '<xsl:if test="count(item) &gt; 0">',
        '<xsl:message terminate="no">Debug only</xsl:message>',
        "<xsl:comment>Generated file</xsl:comment>",
        '<p translate="no">ACME-42</p>',
        '<xsl:attribute name="class">big</xsl:attribute>',
        '<xsl:with-param name="note">raw-code</xsl:with-param>',
        '<img src="{@src}"',
        '<html lang="en">',
    ):
        assert keep in text, keep
    assert "<xsl:otherwise><xsl:text>&lt;No branch&gt; &amp; co</xsl:text></xsl:otherwise>" in text
    assert "<footer>&lt;&copy; 2026 Example&gt; &amp; co</footer>" in text
    assert root.tag.endswith("stylesheet")
    # the rebuilt file parses back to the same number of units
    assert len(F.parse(out).units) == len(BASIC_UNITS)
    print("  [build] translated splice: escaping, tokens restored, XSL untouched OK")


def test_value_templates():
    data = XSLT3.encode("utf-8")
    texts = _texts(data)
    assert texts == [
        "Total: ⟦x1⟧ for ⟦x2⟧ customer, {literal}",
        "Raw {braces} stay",
        "Line ⟦x1⟧ done",
        "x",
        "⟦x1⟧ items",
    ], texts
    assert F.build(data, _all(data, lambda t: t)) == data
    tr = {"0": "Gesamt {x}: ⟦x1⟧ / ⟦x2⟧", "1": "Roh {x}", "2": "Zeile ⟦x1⟧ {ok}", "4": "⟦x1⟧ {Stk}"}
    out = F.build(data, tr).decode("utf-8")
    assert "<p>Gesamt {{x}}: {sum(line/@price)} / {map{'a':1}?a}</p>" in out, out
    assert '<p xsl:expand-text="no">Roh {x}</p>' in out, out
    assert "<xsl:text>Zeile {position()} {{ok}}</xsl:text>" in out, out
    assert "title=\"{concat('a}', @x)} {{Stk}}\"" in out, out
    etree.fromstring(out.encode(), _SAFE)
    print("  [avt/tvt] AVT and expand-text expressions protected, braces doubled on build OK")


def test_simplified_and_inline_attr_inside_text_unit():
    data = SIMPLIFIED.encode("utf-8")
    pr = F.parse(data)
    texts = [u.text for u in pr.units]
    assert texts == [
        "Expense Report Summary",
        "Total Amount: ⟦x1⟧",
        "⟦x1⟧ Inline image here",
        "Company logo",
    ], texts
    out = F.build_localized(data, _all(data, lambda t: "[fr] " + t), "fr-FR").decode("utf-8")
    assert '<p>[fr] <img src="a.png" alt="[fr] Company logo"/> Inline image here</p>' in out, out
    assert 'xml:lang="fr-FR"' in out or 'xml:lang="fr"' in out, out
    assert 'xsl:version="1.0"' in out
    print(
        "  [simplified] literal-result-element stylesheet; nested attr edit inside a token; lang stamp OK"
    )


def test_token_damage():
    data = BASIC.encode("utf-8")
    # dropped point token is appended; stray / duplicate / unknown tokens dropped; unclosed pair closed
    tr = {"0": "Hallo ⟦g1⟧Laden ⟦x9⟧⟦/g3⟧", "1": "⟦x2⟧⟦x2⟧ Preis"}
    out = F.build(data, tr).decode("utf-8")
    assert (
        '<h1 title="Main {@id} heading">Hallo <b>Laden </b><xsl:value-of select="user/name"/></h1>'
        in out
    ), out
    assert '<p><xsl:value-of select="price"/> Preis&nbsp;</p>' in out, out
    etree.fromstring(out.encode(), _SAFE)
    print("  [tokens] damaged token streams rebuild well-formed, protected tokens never lost OK")


def test_whitespace_and_crlf():
    src = (
        '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">\r\n'
        '  <xsl:template match="/">\r\n'
        "    <p>First line\r\n       second line</p>\r\n"
        '    <pre xml:space="preserve">keep   this\r\n  layout</pre>\r\n'
        "    <xsl:text>  padded  </xsl:text>\r\n"
        "  </xsl:template>\r\n"
        "</xsl:stylesheet>\r\n"
    )
    data = src.encode("utf-8")
    assert _texts(data) == ["First line second line", "keep   this\n  layout", "padded"], _texts(
        data
    )
    assert F.build(data, _all(data, lambda t: t)) == data
    out = F.build(data, {"1": "a\nb"}).decode("utf-8")
    assert '<pre xml:space="preserve">a\r\nb</pre>' in out
    out = F.build(data, {"2": "voll"}).decode("utf-8")
    assert "<xsl:text>  voll  </xsl:text>" in out
    print("  [ws] whitespace collapsed for display only, xsl:text/xml:space kept, CRLF OK")


def test_encodings_and_bom():
    body = BASIC.replace('encoding="UTF-8"', 'encoding="UTF-16"')
    for raw in (b"\xef\xbb\xbf" + BASIC.encode("utf-8"), b"\xff\xfe" + body.encode("utf-16-le")):
        assert _texts(raw) == BASIC_UNITS
        assert F.build(raw, _all(raw, lambda t: t)) == raw
        out = F.build(raw, {"5": "Zelle ä"})
        assert out[:2] == raw[:2]
    cp = BASIC.replace('encoding="UTF-8"', 'encoding="windows-1252"').replace("A cell", "Café cell")
    raw = cp.encode("cp1252")
    pr = F.parse(raw)
    assert pr.units[5].text == "Café cell" and pr.skeleton.get("source_encoding") == "cp1252"
    out = F.build(raw, {"5": "中文 é"})
    assert b"<td>&#20013;&#25991; \xe9</td>" in out, out
    print("  [enc] UTF-8 BOM, UTF-16 LE, declared windows-1252 round trips OK")


def test_hostile_inputs():
    _refused(b"<xsl:stylesheet", ValueError, "")
    _refused(b"\x00\x01\x02binary", ValueError, "")
    lol = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        '<xsl:template match="/"><p>&b;</p></xsl:template></xsl:stylesheet>'
    )
    _refused(lol.encode(), ValueError, "DOCTYPE")
    xxe = lol.replace(
        '<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;">', '<!ENTITY b SYSTEM "file:///etc/passwd">'
    )
    _refused(xxe.encode(), ValueError, "DOCTYPE")
    pe = lol.replace(
        '<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;">', '<!ENTITY % p SYSTEM "http://x/">%p;'
    )
    _refused(pe.encode(), ValueError, "DOCTYPE")
    # gate (train-16): a comment that only MENTIONS a DOCTYPE is not a declaration; the
    # prolog-only DOCTYPE check must not refuse it, while a hostile prolog DOCTYPE behind a
    # leading comment is still refused.
    mention = (
        b'<?xml version="1.0"?><xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        b'<!-- emit <!DOCTYPE html [ <!ENTITY a SYSTEM "f"> ]> first -->'
        b'<xsl:template match="/"><p>Hello</p></xsl:template></xsl:stylesheet>'
    )
    assert _texts(mention) == ["Hello"], _texts(mention)
    assert F.build(mention, {"0": "Hello"}) == mention
    hidden = xxe.replace("<!DOCTYPE", "<!-- note --><?pi x?>\n<!DOCTYPE", 1)
    _refused(hidden.encode(), ValueError, "DOCTYPE")
    undefined = (
        '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        '<xsl:template match="/"><p>&nbsp;x</p></xsl:template></xsl:stylesheet>'
    )
    _refused(undefined.encode(), ValueError, "well-formed")
    deep = (
        '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:template match="/">'
        + "<div>" * 260
        + "x"
        + "</div>" * 260
        + "</xsl:template></xsl:stylesheet>"
    )
    _refused(deep.encode(), ValueError, "")
    _refused(
        b'<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        b'<xsl:template match="/"><xsl:value-of select="."/></xsl:template></xsl:stylesheet>',
        EmptyDocumentError,
        "no literal output text",
    )
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = (
            '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:template match="/">'
            + "".join(f"<p>Line {i}</p>" for i in range(6))
            + "</xsl:template></xsl:stylesheet>"
        )
        _refused(many.encode(), InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        _refused(("<p>" * 200).encode(), InputLimitError, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    old = config.MAX_SEGMENT_CHARS
    try:
        config.MAX_SEGMENT_CHARS = 1000
        big = (
            '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
            '<xsl:template match="/"><p>' + "word " * 400 + "</p></xsl:template></xsl:stylesheet>"
        )
        _refused(big.encode(), InputLimitError, "MAX_SEGMENT_CHARS")
    finally:
        config.MAX_SEGMENT_CHARS = old
    _refused(
        BASIC.replace('encoding="UTF-8"', 'encoding="ebcdic-cp-us"').encode(),
        ValueError,
        "encoding",
    )
    print(
        "  [hostile] billion laughs, XXE, parameter entities, undefined entity, depth, caps refused OK"
    )


def test_illegal_xml_chars_in_translation():
    data = BASIC.encode("utf-8")
    out = F.build(data, {"5": "bad\x01char\x0bnext"})
    assert b"<td>badchar\nnext</td>" in out, out
    etree.fromstring(out, _SAFE)
    print("  [build] XML-illegal characters stripped from targets OK")


def test_prefix_doe_and_inline_runs():
    """Any prefix bound to the XSLT namespace; disable-output-escaping text is never text;
    whitespace-only text between instructions separates units; xsl:variable LREs translate."""
    src = (
        '<t:stylesheet version="2.0" xmlns:t="http://www.w3.org/1999/XSL/Transform"\n'
        '    xmlns:xsl="urn:not-xslt">\n'
        '  <t:template match="/">\n'
        '    <t:text disable-output-escaping="yes">&lt;br/&gt;</t:text>\n'
        "    <b>Bold</b> <i>Italic</i>\n"
        '    <p>Read <a href="{@u}" title="Open the guide">the guide</a> now.</p>\n'
        '    <t:variable name="label"><span>Saved label</span></t:variable>\n'
        "    <xsl:note>Not an instruction</xsl:note>\n"
        "  </t:template>\n"
        "</t:stylesheet>\n"
    )
    data = src.encode("utf-8")
    texts = _texts(data)
    assert texts == [
        "Bold",
        "Italic",
        "Read ⟦g1⟧the guide⟦/g1⟧ now.",
        "Saved label",
        "Not an instruction",
        "Open the guide",
    ], texts
    out = F.build(data, {"2": "Lies ⟦g1⟧die Anleitung⟦/g1⟧.", "5": "Anleitung öffnen"})
    out = out.decode("utf-8")
    assert '<p>Lies <a href="{@u}" title="Anleitung öffnen">die Anleitung</a>.</p>' in out, out
    assert '<t:text disable-output-escaping="yes">&lt;br/&gt;</t:text>' in out
    print("  [ns] other XSLT prefixes, disable-output-escaping, run breaks, variable LREs OK")


def test_api_and_cli_round_trip():
    """Upload an .xsl through the API and the CLI, mock-translate, export: the file comes
    back well-formed with only literal text changed."""
    import re
    import tempfile

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    loc = "de-DE"
    init_db()
    with Session(engine) as s:
        p = Project(name="xslt", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id
    user = security.create_user("xslt@x.test", "xslt", Role.ADMIN, "xslt-pw-123456")
    _row, raw_key = apikeys.create_key(user.id, "xslt", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw_key}  # pregate: allow-secret throwaway test key
    data = BASIC.encode("utf-8")

    def check(out: bytes, surface: str):
        etree.fromstring(out, _SAFE)
        text = out.decode("utf-8")
        assert f"<td>[{loc}] A cell</td>" in text, (surface, text)
        assert '<xsl:value-of select="user/name"/>' in text, (surface, text)
        assert '<xsl:variable name="mode">compact</xsl:variable>' in text, (surface, text)
        assert '<html lang="de-DE">' in text or '<html lang="de">' in text, (surface, text)

    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("invoice.xsl", data, "application/xslt+xml")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", loc])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": loc})
    assert r.status_code == 200, (r.status_code, r.text)
    check(r.content, "api")
    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    assert {"ext": "xslt", "name": "XSLT stylesheet"} in r.json()["formats"], r.text

    tmp = tempfile.mkdtemp(prefix="loctms_xslt_cli_")
    src = os.path.join(tmp, "report.xslt")
    with open(src, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    m = re.search(r"Uploaded #(\d+): report\.xslt \(xslt,", out)
    assert m, out
    cli_doc = int(m.group(1))
    code, out, err = run_cli(["doc", "translate", str(cli_doc), "--locale", loc])
    assert code == 0, (code, out, err)
    dest = os.path.join(tmp, "out.xslt")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", loc, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        check(fh.read(), "cli")
    print("  [surfaces] API upload/download and CLI upload/export round trip OK")


def test_script_and_style_are_code():
    """#694: literal result <script>/<style> content is code, never a unit (real Okapi
    helpproject.xsl shape: CSS rules and a script with xsl:value-of inside)."""
    xsl = (
        b'<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        b'<xsl:template match="/"><html><head><title>Help</title>\n'
        b"<style type=\"text/css\">body, p {font-family: 'Arial'; color: #000}</style>\n"
        b'<SCRIPT language="javascript">var s1 = \'<xsl:value-of select="$p"/>\';'
        b' function f(x) { return x + "Baggage"; }</SCRIPT>\n'
        b'<script xmlns="http://www.w3.org/1999/xhtml">alert("Hi")</script>'
        b"</head><body><p>Topic Title:</p></body></html></xsl:template></xsl:stylesheet>"
    )
    texts = [u.text for u in F.parse(xsl).units]
    assert texts == ["Help", "Topic Title:"], texts
    assert F.build(xsl, {}) == xsl
    out = F.build(xsl, {"0": "Hilfe", "1": "Thema:"})
    assert b"<title>Hilfe</title>" in out and b"<p>Thema:</p>" in out, out
    assert b"color: #000}</style>" in out and b'return x + "Baggage"; }</SCRIPT>' in out, out
    # gate: <style> in another vocabulary's namespace is document text, still a unit
    cat = (
        b'<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"'
        b' xmlns:c="urn:example:catalog"><xsl:template match="/">'
        b"<c:item><c:style>Classic leather</c:style></c:item>"
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>.a{fill:red}</style></svg>'
        b"</xsl:template></xsl:stylesheet>"
    )
    assert [u.text for u in F.parse(cat).units] == ["Classic leather"], F.parse(cat).units
    print("  [code] script/style result elements never units; bytes kept")


def main():
    test_registration()
    test_script_and_style_are_code()
    test_basic_units_and_identity()
    test_translated_round_trip()
    test_value_templates()
    test_simplified_and_inline_attr_inside_text_unit()
    test_token_damage()
    test_whitespace_and_crlf()
    test_encodings_and_bom()
    test_hostile_inputs()
    test_illegal_xml_chars_in_translation()
    test_prefix_doe_and_inline_runs()
    test_api_and_cli_round_trip()
    print("XSLT FILTER OK")


if __name__ == "__main__":
    main()
