"""Server-page template filters (format Batch 16): .phtml, .jsp/.jspf, .aspx/.ascx, .erb,
.shtml/.shtm/.stm.

Proves: untouched round trip is byte-identical for every dialect; output expressions are
point tokens inside their sentence, statement blocks split units; code inside attribute
values is kept and never extracted; a translation can neither drop, duplicate nor inject
code; unsafe islands refuse the file; closed code islands in a .html are protected the
same way (#672); ingest + export end to end.

All fixtures are hand-written. ASCII output only.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_serverpage_")

from fastapi.testclient import TestClient

from app import config
from app.db import engine, init_db
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.filters.server_page_filter import ServerPageRefused
from app.main import app
from app.models import Segment
from app.services.segment import find_placeholders, ph_close, ph_open, ph_point

X1, X2, X3 = ph_point("x", 1), ph_point("x", 2), ph_point("x", 3)

PHP = (
    '<!DOCTYPE html>\n<html lang="en">\n<head><title><?= $title ?></title></head>\n<body>\n'
    "<?php if ($user): ?>\n"
    "<p>Hello <?= htmlspecialchars($user) ?>, you have <b><?php echo $n; ?></b> new messages.</p>\n"
    "<?php else: ?>\n<p>Please sign in.</p>\n<?php endif; ?>\n"
    '<a href="<?php echo url("a\\"b>"); ?>" title="Open the inbox">Inbox</a>\n'
    '<img src="logo.png" alt="<?= $alt ?>">\n'
    "<?php\n$block = <<<EOT\n<p>Not text ?> here</p>\nEOT;\n// comment with ' quote\n?>\n"
    "<script>var n = <?= json_encode($n) ?>; if (a <b) {}</script>\n"
    "</body>\n</html>\n"
)
JSP = (
    '<%@ page contentType="text/html; charset=UTF-8" %>\n'
    "<%-- page comment with <p>markup</p> --%>\n"
    '<p class="${cls}">Welcome back, ${user.name}! Today is <%= date %>.</p>\n'
    "<% for (int i = 0; i < 3; i++) { %><li>Item</li><% } %>\n"
    '<jsp:scriptlet>String s = "<p>no</p>";</jsp:scriptlet>\n'
    "<p>Price: \\${not.el} only</p>\n"
)
ASPX = (
    '<%@ Page Language="C#" AutoEventWireup="true" %>\n'
    "<%-- comment --%>\n"
    '<h1>Hi <%: Model.Name %>, and <%# Eval("Title") %> is ready.</h1>\n'
    "<% if (show) { %><p>Shown only sometimes</p><% } %>\n"
    '<asp:Label runat="server" Text="Server label" />\n'
)
ERB = (
    "<h1><%= t('.title') %></h1>\n"
    "<% @items.each do |item| -%>\n"
    "<p>Only <%= item.price %> today</p>\n"
    "<% end -%>\n"
    "<%# a comment %>\n"
    "<p>Literal <%% marker and <%== raw %> end</p>\n"
)
SHTML = (
    '<!--#include virtual="/header.html" -->\n'
    '<!--#config timefmt="%Y" -->\n'
    '<p>Last updated <!--#echo var="LAST_MODIFIED" --> by the team.</p>\n'
)
SAMPLES = {"phtml": PHP, "jsp": JSP, "aspx": ASPX, "erb": ERB, "shtml": SHTML}


def _units(fmt, src):
    return [u.text for u in get_filter(fmt).parse(src.encode("utf-8")).units]


def _build(fmt, src, mapping):
    return get_filter(fmt).build(src.encode("utf-8"), mapping).decode("utf-8")


def test_registry():
    exts = set(supported_exts())
    for e in ("phtml", "jsp", "jspf", "aspx", "ascx", "erb", "shtml", "shtm", "stm"):
        assert e in exts, e
    assert get_filter("jspf").fmt == "jsp" and get_filter("ascx").fmt == "aspx"
    assert get_filter("stm").fmt == "shtml" and get_filter("shtm").fmt == "shtml"
    # .html/.htm/.xhtml stay on the plain HTML filter; .php is not claimed here.
    for e in ("html", "htm", "xhtml"):
        assert type(get_filter(e)).__name__ == "HtmlFilter", e
    # .php routes by content: a page template here, a language array to php_array_filter
    # (Batch 11) when that module is present, PHP code with no page text refused.
    php = get_filter("php")
    assert type(php).__name__ == "PhpFilter", type(php)
    tpl = "<p>Hello <?= $n ?></p>\n"
    assert [u.text for u in php.parse(tpl.encode()).units] == [f"Hello {X1}"]
    assert php.build(tpl.encode(), {"0": f"Salut {X1}"}) == b"<p>Salut <?= $n ?></p>\n"
    try:
        php.parse(b"<?php\nfunction f() { return 1; }\n")
        raise AssertionError("code-only .php accepted")
    except ServerPageRefused as exc:
        assert "language array" in str(exc)
    lang = b"<?php\nreturn ['welcome' => 'Welcome back'];\n"
    import importlib.util

    if importlib.util.find_spec("app.filters.php_array_filter") is not None:
        units = [u.text for u in php.parse(lang).units]
        assert units == ["Welcome back"], units
        out = php.build(lang, {"0": "Bon retour"})
        assert b"'welcome' => 'Bon retour'" in out, out
        # the keys bridge sees the array's key view through the router
        recs = php.records(lang)
        assert [(r["key"], r["value"]) for r in recs] == [("welcome", "Welcome back")], recs
        try:
            php.records(tpl.encode())
            raise AssertionError("template .php gave key records")
        except ServerPageRefused as exc:
            assert "page template" in str(exc)
    else:
        try:
            php.parse(lang)
            raise AssertionError("lang array parsed as a template")
        except ServerPageRefused:
            pass
        assert not hasattr(php, "records"), "key view advertised with no array filter"
    print("  1) registry: 10 extensions, html untouched, .php routed by content")


def test_identity_roundtrip():
    for fmt, src in SAMPLES.items():
        f = get_filter(fmt)
        assert f.build(src.encode("utf-8"), {}) == src.encode("utf-8"), fmt
        r = f.parse(src.encode("utf-8"))
        same = {str(u.p_index): u.text for u in r.units}
        assert f.build(src.encode("utf-8"), same) == src.encode("utf-8"), fmt
    print("  2) untouched and identity-translated round trips are byte-identical (5 dialects)")


def test_units():
    php = _units("phtml", PHP)
    assert f"Hello {X1}, you have {ph_open('g', 2)}{X3}{ph_close('g', 2)} new messages." in php, php
    assert "Please sign in." in php
    assert "Open the inbox" in php  # quoted title attribute without code
    assert not any("$" in u or "?>" in u or "EOT" in u for u in php), php
    assert not any("Not text" in u for u in php)
    jsp = _units("jsp", JSP)
    assert f"Welcome back, {X1}! Today is {X2}." in jsp, jsp
    assert "Item" in jsp and not any("String" in u or "markup" in u for u in jsp), jsp
    assert "Price: \\${not.el} only" in jsp, jsp  # escaped EL is literal text
    aspx = _units("aspx", ASPX)
    assert f"Hi {X1}, and {X2} is ready." in aspx, aspx
    assert "Shown only sometimes" in aspx
    erb = _units("erb", ERB)
    assert f"Only {X1} today" in erb and f"Literal {X1} marker and {X2} end" in erb, erb
    assert not any("comment" in u for u in erb)
    ssi = _units("shtml", SHTML)
    assert ssi == [f"Last updated {X1} by the team."], ssi
    print("  3) output expressions are point tokens; statements, directives, comments are not text")


def test_translated_roundtrip():
    src = PHP
    f = get_filter("phtml")
    r = f.parse(src.encode())
    mapping = {}
    for u in r.units:
        if u.text.startswith("Hello"):
            g2, g2c = ph_open("g", 2), ph_close("g", 2)
            mapping[str(u.p_index)] = f"Bonjour {X1}, vous avez {g2}{X3}{g2c} nouveaux messages."
        elif u.text == "Open the inbox":
            mapping[str(u.p_index)] = 'Ouvrir la boite "<b>"'
        else:
            mapping[str(u.p_index)] = "FR " + u.text
    out = _build("phtml", src, mapping)
    assert (
        "Bonjour <?= htmlspecialchars($user) ?>, vous avez <b><?php echo $n; ?></b> nouveaux" in out
    )
    assert (
        '<a href="<?php echo url("a\\"b>"); ?>" title="Ouvrir la boite &quot;&lt;b&gt;&quot;">'
        in out
    )
    assert '<img src="logo.png" alt="<?= $alt ?>">' in out
    assert "<?php if ($user): ?>" in out and "<?php endif; ?>" in out
    assert "$block = <<<EOT\n<p>Not text ?> here</p>\nEOT;" in out
    assert "<script>var n = <?= json_encode($n) ?>; if (a <b) {}</script>" in out
    assert find_placeholders(out) == [], out
    print("  4) translated round trip: code byte-identical, attribute escaped, no token leaks")


def test_tokens_cannot_lose_or_inject_code():
    src = "<p>Hello <?= $name ?>, bye.</p>"
    out = _build("phtml", src, {"0": "Bonjour, au revoir."})
    assert out.count("<?= $name ?>") == 1, out  # dropped token restored
    out = _build("phtml", src, {"0": f"Bonjour {X1} {X1}."})
    assert out.count("<?= $name ?>") == 1, out  # duplicate written once
    out = _build("phtml", src, {"0": "Hi ﷐0﷑ <?php system('id'); ?> " + X1})
    assert "system('id'); ?>" not in out and "&lt;?php" in out, out
    assert out.count("<?= $name ?>") == 1, out
    # JSP: translated text can never become EL; ERB/ASPX "<%" is escaped as markup.
    out = _build("jsp", "<p>Hi ${u}</p>", {"0": f"Salut {X1} ${{evil}} #{{evil}}"})
    assert "\\${evil}" in out and "\\#{evil}" in out and "Salut ${u}" in out, out
    out = _build("erb", "<p>Hi <%= u %></p>", {"0": f"Salut {X1} <%= `id` %>"})
    assert "&lt;%= `id` %&gt;" in out, out
    out = _build("shtml", "<p>Hi</p>", {"0": '<!--#exec cmd="id" -->'})
    assert "<!--#exec" not in out, out
    print("  5) dropped/duplicated tokens fixed; injected PHP, EL, ERB, SSI all inert")


def _refused(fmt, src, needle):
    try:
        get_filter(fmt).parse(src.encode("utf-8"))
    except ServerPageRefused as exc:
        assert needle in str(exc), (fmt, src, str(exc))
        assert isinstance(exc, InputLimitError)
        return
    raise AssertionError(f"not refused: {fmt} {src!r}")


def test_refusals():
    _refused("phtml", "<p>Hi</p>\n<<?= $tag ?> class='x'>Hello</<?= $tag ?>>", "tag name (line 2)")
    _refused("phtml", "<div <?php echo $attrs ?>>Hello</div>", "tag or attribute name")
    _refused("aspx", '<input <%= disabled %> value="x">', "tag or attribute name")
    _refused("phtml", '<?php echo "<div"; ?> class="x">Hello</div>', "start of an HTML tag")
    _refused("phtml", "<p>Hi <?php echo 'oops ?></p>", "string is never closed")
    _refused("phtml", "<p>Hi <?php /* never ?></p>", "comment is never closed")
    _refused("phtml", "<?php $x = <<<EOT\nno end\n", "heredoc is never closed")
    _refused("jsp", "<p>Hi <% int x = 1; </p>", "never closed")
    _refused("jsp", "<p>Hi ${user.name</p>", "never closed")
    _refused("jsp", "<jsp:scriptlet>int x;", "never closed")
    _refused("aspx", "<p>Hi <%-- never closed</p>", "never closed")
    _refused("shtml", "<p><!--#echo var='x' </p>", "never closed")
    _refused("erb", "<p>﷐ x</p>", "Remove those characters")
    print("  6) unsafe islands refuse the file with a line number (13 cases)")


def test_accepted_edge_cases():
    # A PHP file ending in PHP mode, ?> inside a string, ?> ending a // comment.
    for src in (
        "<p>Hi</p>\n<?php\nfoo();\n",
        '<p>Price <?php echo "?>"; ?> here</p>',
        "<p>A <?php // note ?> B</p>",
        "<p>A <?php # note ?> B <?php #[Attr] ?></p>",
        '<?xml version="1.0"?>\n<p>Text</p>',
    ):
        f = get_filter("phtml")
        assert f.build(src.encode(), {}) == src.encode(), src
        f.parse(src.encode())
    assert _units("phtml", '<p>Price <?php echo "?>"; ?> here</p>') == [f"Price {X1} here"]
    assert _units("phtml", "<p>A <?php // note ?> B</p>") == ["A", "B"]
    # ERB %%> inside a tag is literal; code inside quoted attribute values is fine.
    assert _units("erb", "<p>Use <%= a %%> b %> now</p>") == [f"Use {X1} now"]
    assert _units("jsp", '<a href="<%= u %>" title="${t}">Go home</a>')[0].endswith(
        "Go home" + ph_close("g", 1)
    )
    print("  7) legal edge cases accepted: EOF PHP, ?> in strings/comments, %%>, <?xml")


def test_language_stamp():
    f = get_filter("phtml")
    out = f.build_localized(b'<html lang="<?= $l ?>"><p>Hi</p></html>', {"0": "Salut"}, "fr-FR")
    assert out.decode() == '<html lang="<?= $l ?>"><p>Salut</p></html>', out
    out = f.build_localized(
        b'<html lang="en"><p>Hi <?= $n ?></p></html>', {"0": f"Marhaba {X1}"}, "ar"
    )
    assert out.decode() == '<html dir="rtl" lang="ar"><p>Marhaba <?= $n ?></p></html>', out
    print("  8) target language stamped; a dynamic lang attribute is left dynamic")


def test_encodings():
    f = get_filter("aspx")
    bom = b"\xef\xbb\xbf" + "<p>Café <%= x %> ouvert</p>".encode()
    assert f.build(bom, {}) == bom
    assert _units("aspx", "<p>Café <%= x %> ouvert</p>") == [f"Café {X1} ouvert"]
    cp = "<p>Café <%= x %></p>".encode("cp1252")
    r = f.parse(cp)
    assert r.skeleton.get("source_encoding"), r.skeleton
    print("  9) UTF-8 BOM byte-identical; Windows-1252 converted and recorded")


def test_caps():
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        many = "".join(f"<p>Line {i} <?= $v ?></p>" for i in range(10))
        try:
            get_filter("phtml").parse(many.encode())
            raise AssertionError("segment cap not enforced")
        except InputLimitError:
            pass
        islands = "<?php ?>" * 60
        try:
            get_filter("phtml").parse(("<p>x</p>" + islands).encode())
            raise AssertionError("island cap not enforced")
        except InputLimitError:
            pass
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # Hostile scale: long code and long strings are linear-time.
    import time

    t = time.monotonic()
    big = "<p>Hi</p><?php " + ("'" + "x" * 200000 + "' . ") * 20 + "1; ?><p>Bye</p>"
    assert _units("phtml", big) == ["Hi", "Bye"]
    big2 = "<p>a</p>" + "<% x %>" * 20000
    get_filter("jsp").parse(big2.encode())
    assert time.monotonic() - t < 20, time.monotonic() - t
    print("  10) segment and island caps enforced; large inputs stay linear")


def test_html_unchanged():
    # A plain .html with no CLOSED code island parses exactly as before (#672 only
    # delegates when an island opens and closes).
    f = get_filter("html")
    for src in ("<p>Save 5 <% today</p>", "<p>Price ${x} and a <? mark</p>", "<p>a %> b</p>"):
        units = [u.text for u in f.parse(src.encode()).units]
        assert len(units) == 1 and "server_code" not in f.parse(src.encode()).skeleton, units
        assert f.build(src.encode(), {}) == src.encode()
    print("  11) html filter unchanged for code-looking content with no closed island")


def test_html_server_code_protected():
    # #672: Okapi's Jericho page Test02.html opens with JSP directives although it is .html.
    # The directive used to be offered as page text and written back escaped (visible).
    f = get_filter("html")
    jsp = (
        '<?xml version="1.0" encoding="UTF-8" ?>\n<%@ page language="java" %>'
        '<%@ taglib uri="/WEB-INF/struts-i18n.tld" prefix="i18n" %>\n'
        "<html><body><p>Hello <%= user %>, welcome.</p><p>Cost ${price} today.</p></body></html>\n"
    )
    parsed = f.parse(jsp.encode())
    texts = [u.text for u in parsed.units]
    assert texts == [f"Hello {X1}, welcome.", f"Cost {X1} today."], texts
    assert not any("<%" in t or "page" in t for t in texts), texts
    assert parsed.skeleton["format"] == "html" and parsed.skeleton["server_code"] == "jsp"
    assert f.build(jsp.encode(), {}) == jsp.encode()
    out = f.build(jsp.encode(), {"0": f"Bonjour {X1}, bienvenue.", "1": f"Prix {X1} aujourd'hui."})
    out = out.decode()
    assert '<%@ page language="java" %>' in out and "&lt;%" not in out, out
    # Gate #672: server pages keep quotes escaped in text (an island can emit markup at
    # runtime, so a text run may sit inside an attribute the scanner never sees).
    assert "Bonjour <%= user %>, bienvenue." in out and "Prix ${price} aujourd&#x27;hui." in out
    assert "aujourd'hui" not in out, out
    # No JSP hint: generic <% %> (ASP.NET lexer); ${...} stays ordinary text.
    asp = "<% Response.Write(1) %>\n<p>Hi <%= name %> and ${x}.</p>\n"
    assert [u.text for u in f.parse(asp.encode()).units] == [f"Hi {X1} and ${{x}}."]
    assert f.parse(asp.encode()).skeleton["server_code"] == "aspx"
    # PHP in .html: statements split units, echoes are point tokens.
    php = "<?php if ($a): ?><p>Hi <?= $n ?>, bye.</p><?php endif; ?>\n"
    assert [u.text for u in f.parse(php.encode()).units] == [f"Hi {X1}, bye."]
    out = f.build(php.encode(), {"0": f"Salut {X1}, <?php system('id'); ?>"}).decode()
    assert out.count("<?php") == 2 and "&lt;?php system" in out, out
    assert f.build_localized(php.encode(), {}, "fr-FR").count(b"<?") == 3
    # Mixed PHP and <% %> in one .html: refused with the reason, never guessed at.
    _refused("html", "<p>Hi <?= $n ?> and <%= x %></p>", "mixes PHP code and <% %> code")
    # An island the server-page rules cannot keep safe refuses the .html as well.
    _refused("html", '<input <%= disabled %> value="x">', "tag or attribute name")
    print("  11b) #672: JSP/ASP/PHP code in .html protected, never page text; mixed refused")


def test_ingest_export():
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "Server pages",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("index.jsp", JSP.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/documents/" in r.headers["location"], r.text
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    from sqlmodel import Session, select

    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "fr-FR")
        ).all()
        rows = [(x.id, x.source_text) for x in segs]
    assert rows and not any("<%" in t or "String" in t for _i, t in rows), rows
    for sid, src in rows:
        client.post(f"/segments/{sid}/save", data={"target_text": "FR " + src, "tte_ms": "0"})
    resp = client.get(f"/documents/{doc_id}/export?locale=fr-FR")
    assert resp.status_code == 200, resp.text
    out = resp.content.decode("utf-8")
    for code in (
        '<%@ page contentType="text/html; charset=UTF-8" %>',
        "<%= date %>",
        "${user.name}",
        "<% for (int i = 0; i < 3; i++) { %>",
        '<jsp:scriptlet>String s = "<p>no</p>";</jsp:scriptlet>',
    ):
        assert code in out, (code, out)
    assert "FR " in out and find_placeholders(out) == [], out
    assert resp.headers["content-type"].startswith("text/plain"), resp.headers
    # A refused file creates nothing and says why.
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("bad.phtml", b"<div <?php echo $a ?>>x</div>", "text/plain")},
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    assert r.status_code == 303 and "/documents/" not in loc, loc
    assert "err=" in loc and "attribute%20name" in loc, loc
    from app.models import Document

    with Session(engine) as s:
        names = [d.filename for d in s.exec(select(Document)).all()]
    assert "bad.phtml" not in names, names
    print("  12) web upload + export of a .jsp: code kept, text translated, refusal stores nothing")


def main() -> None:
    init_db()
    test_registry()
    test_identity_roundtrip()
    test_units()
    test_translated_roundtrip()
    test_tokens_cannot_lose_or_inject_code()
    test_refusals()
    test_accepted_edge_cases()
    test_language_stamp()
    test_encodings()
    test_caps()
    test_html_unchanged()
    test_html_server_code_protected()
    test_ingest_export()
    print("SERVER PAGE FILTER OK")


if __name__ == "__main__":
    main()
