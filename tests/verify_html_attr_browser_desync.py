"""#676: a translated ATTRIBUTE value must never add an event handler, on any page.

The html filter family finds alt/title/aria-label values with its own scanner. On malformed
pages (an abrupt <!--> comment, <style> inside SVG, a stray </ x>, raw-text <xmp>, ...) a
browser's HTML5 tokenizer can be OUTSIDE a quoted value where the scanner sees one, so an
alt translated to `x" onfocus=alert(1) y="` (quotes escaped) became live attribute names in
Chrome. The fix (app/filters/html_guard.py): such values are written opaque to every HTML5
tag state, and every build is re-parsed with a browser-equivalent HTML5 parser (scripting on
and off, the 2025 and the legacy <select> parser) and refused
unless the browser finds only the source's elements, attributes and script/style bodies.

Covers html, xhtml, server pages (php/jsp/aspx/erb/shtml), EPUB chapters, Flare topics and
keyword terms, email HTML parts, HTML Help sitemaps and MDX attributes. The Chrome part
(real Chromium DOMParser through Playwright) runs where a browser launches and is skipped
otherwise, unless LOC_TMS_BROWSER_REQUIRED=1."""

import io
import os
import random
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests._bootstrap import init_env

init_env("loctms_html_attr_desync_")

from app.filters import get_filter
from app.filters.base import InputLimitError
from app.filters.html_filter import HtmlFilter

ATTACK = 'x" onfocus=alert(1) y="'
BREAKOUT = 'Z" onfocus=alert(1) q=\' onblur=alert(2) w="'
# The two reproductions in the issue, plus the raw-text cousin the gate probe used.
ISSUE_PAGES = (
    '<!--><a title="--> <p>Hi <img alt="cat"> there</p>',
    '<svg><style><a title="</style><img alt="cat"></svg>',
    '<xmp><a title="</xmp><img alt="cat">',
)
DESYNC_PAGES = (
    *ISSUE_PAGES,
    '<!---><a title="--> <p>Hi <img alt="cat"> there</p>',
    '<!-- a --!><a title="--> <p>Hi <img alt="cat"> there</p>',
    '<p></ x <a title="> <img alt="cat"></p>',
    '<textarea><a title="</textarea><img alt="cat">',
    '<noscript><a title="</noscript><img alt="cat">',
    '<a title= = "x><img alt="cat">',
    '<![CDATA[><a title="]]><img alt="cat">',
    '<?x ><a title="?><img alt="cat">',
    '<math><style><a title="</style><img alt="cat"></math>',
    '<select><xmp><a title="</xmp><img alt="cat"></select>',
)

_CHROME = []  # (src, out) pairs the Chrome step re-checks in a real browser


def _event_attrs(markup: str) -> set:
    """(tag, attribute) for every on* attribute an HTML5 browser parse finds, in both
    html_guard modes (scripting on / off)."""
    from app.filters.html_guard import browser_views

    return {
        (st.name, a)
        for view in browser_views(markup)
        for st in view.starts
        for a, _v in st.attrs
        if a.startswith("on")
    }


def _safe_or_refused(src: str, build) -> str | None:
    """Run `build()`; a refusal (an InputLimitError such as UnsafeMarkupRefused) is safe.
    Otherwise the output must hold no event handler the source did not have."""
    try:
        out = build()
    except InputLimitError as exc:
        assert "cannot be exported safely" in str(exc) or "cannot be translated" in str(exc), exc
        return None
    if isinstance(out, bytes):
        out = out.decode("utf-8")
    added = _event_attrs(out) - _event_attrs(src)
    assert not added, (src, out, added)
    _CHROME.append((src, out))
    return out


def _alt_mapping(filt, data: bytes, value: str) -> dict:
    return {
        str(u.p_index): (value if (u.style or "").startswith("a11y:") else u.text)
        for u in filt.parse(data).units
    }


def test_issue_reproductions():
    """The exact pages from #676: on the old code the alt translation gave <a> a live
    onfocus (html_guard's parser and Chrome agree). Now: refused, never exported."""
    filt = HtmlFilter()
    for page in DESYNC_PAGES:
        data = page.encode()
        mapping = _alt_mapping(filt, data, ATTACK)
        try:
            filt.build(data, mapping)
        except InputLimitError as exc:
            assert "cannot be exported safely" in str(exc), exc
            assert "line 1" in str(exc), exc
        else:
            _safe_or_refused(page, lambda d=data, m=mapping: filt.build(d, m))
    page = ISSUE_PAGES[0].encode()
    try:
        filt.build(page, _alt_mapping(filt, page, ATTACK))
        raise AssertionError("the #676 page must be refused")
    except InputLimitError as exc:
        assert "<a> tag" in str(exc) and "Correct that markup" in str(exc), exc
    print(f"  {len(DESYNC_PAGES)} desync pages: the alt breakout is refused or inert")


def test_ordinary_pages_unchanged():
    """Ordinary pages keep readable translated values, byte-identical no-change rebuilds
    and no refusal; a page with inline SVG still exports, its values made opaque."""
    filt = HtmlFilter()
    page = b'<html><body><p>Hello <b>there</b>.</p><img alt="A cat" src="c.png"></body></html>'
    units = filt.parse(page).units
    assert filt.build(page, {str(u.p_index): u.text for u in units}) == page
    out = filt.build(page, _alt_mapping(filt, page, "Un chat l'ami")).decode()
    assert 'alt="Un chat l&#x27;ami"' in out, out
    out = filt.build(page, _alt_mapping(filt, page, ATTACK)).decode()
    assert 'alt="x&quot; onfocus=alert(1) y=&quot;"' in out and not _event_attrs(out), out
    svg = b'<p>Logo <img alt="A cat"></p><svg><title>Chart</title><style>.a{fill:red}</style></svg>'
    out = filt.build(svg, _alt_mapping(filt, svg, "Un chat")).decode()
    assert 'alt="Un&#32;chat"' in out, out  # a browser reads "Un chat"
    from app.filters.html_guard import browser_view

    img = next(st for st in browser_view(out).starts if st.name == "img")
    import html

    assert html.unescape(dict(img.attrs)["alt"]) == "Un chat", img.attrs
    print("  ordinary pages: readable values, identity rebuild; SVG page values opaque")


def test_strict_value_is_opaque():
    """A strict value is one run in every HTML5 tag state: no whitespace, `/`, `=`, quote,
    `<`, `>` or backtick survives as itself."""
    from app.filters.html_guard import attr_value

    v = attr_value("a b\t/c=d`e\"f'g<h>i&j\nk", strict=True)
    assert not re.search(r"[\s/=`\"'<>]", v), v
    assert attr_value("a b", strict=False) == "a b"
    print("  strict values carry no tag-state character")


def test_check_build_rules():
    """The re-parse refuses new attributes, changed non-translatable values, new elements
    and new script bodies; translated alt/title/lang values and reordered inline tags pass."""
    from app.filters.html_guard import UnsafeMarkupRefused, check_build

    ok = (
        ('<a href="/x" title="A">t</a><img alt="B">', '<a href="/x" title="Z">u</a><img alt="Y">'),
        ("<b>x</b><i>y</i>", "<i>y</i><b>x</b>"),
        (
            '<html lang="en"><meta http-equiv="content-language" content="en">',
            '<html lang="fr"><meta http-equiv="content-language" content="fr">',
        ),
    )
    for before, after in ok:
        check_build(before, after)
    bad = (
        ('<img alt="A">', '<img alt="A" onerror="x">'),
        ('<a href="/x">t</a>', '<a href="javascript:x">t</a>'),
        ("<p>t</p>", "<p>t</p><script>x()</script>"),
        ("<p>t</p>", "<p><svg><script>x()</script></svg></p>"),
        (
            '<meta http-equiv="refresh" content="5">',
            '<meta http-equiv="refresh" content="0;url=javascript:x">',
        ),
        (
            '<?xml version="1.0"?><r><a t="1"/></r>',
            '<?xml version="1.0"?><r><a t="1"/><a u="2"/></r>',
        ),
    )
    for before, after in bad:
        try:
            check_build(before, after)
        except UnsafeMarkupRefused:
            continue
        raise AssertionError(f"not refused: {after}")
    # gate: <noscript> and <select> act independently, so a page holding both is read in
    # all four (scripting, select parser) combinations; today's DOMParser is scripting off
    # with the 2025 <select> parser, an older engine's page load the reverse
    from app.filters.html_guard import browser_views

    page = '<noscript></noscript><select><xmp><a title="</xmp><img alt="cat"></select>'
    spans = [sorted(v.spans) for v in browser_views(page)]
    assert len(spans) == 4 and len({tuple(s) for s in spans}) == 2, spans
    assert len(browser_views('<select><xmp><a title="</xmp></select>')) == 2
    assert len(browser_views("<p>plain</p>")) == 1
    print("  re-parse check: translated values pass, structural changes refused")


def test_server_pages():
    for fmt, page in (
        ("phtml", '<?php $x = 1; ?><!--><a title="--> <p>Hi <img alt="cat"> there</p>'),
        ("jsp", '<%@ page x="1" %><svg><style><a title="</style><img alt="cat"></svg>'),
        ("aspx", '<%@ Page Language="C#" %><xmp><a title="</xmp><img alt="cat">'),
        ("erb", '<% x = 1 %><!--><a title="--> <p>Hi <img alt="cat"> there</p>'),
        (
            "shtml",
            '<!--#include file="h.html" --><svg><style><a title="</style><img alt="cat"></svg>',
        ),
    ):
        filt = get_filter(fmt)
        data = page.encode()
        mapping = _alt_mapping(filt, data, ATTACK)
        assert any(v == ATTACK for v in mapping.values()), (fmt, mapping)
        _safe_or_refused(page, lambda f=filt, d=data, m=mapping: f.build(d, m))
    # html that holds PHP goes through the server-page filter (#672)
    page = '<?php echo 1; ?><svg><style><a title="</style><img alt="cat"></svg>'
    filt = HtmlFilter()
    _safe_or_refused(
        page, lambda: filt.build(page.encode(), _alt_mapping(filt, page.encode(), ATTACK))
    )
    print("  server pages (phtml, jsp, aspx, erb, shtml, html+php): breakout refused or inert")


EPUB_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:12345678-1234-1234-1234-123456789abc</dc:identifier>
    <dc:title>Book</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>
"""
EPUB_CONTAINER = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf"
 media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _zip(members: dict, first: tuple | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if first:
            z.writestr(zipfile.ZipInfo(first[0]), first[1])
        for name, data in members.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def _member(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name).decode("utf-8")


def test_epub_chapter():
    chapter = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head><title>C</title></head><body><!--><a title="--><p>Hi <img alt="cat" src="c.png"/>'
        " there</p></body></html>"
    )
    epub = _zip(
        {
            "META-INF/container.xml": EPUB_CONTAINER,
            "OEBPS/content.opf": EPUB_OPF.encode(),
            "OEBPS/c1.xhtml": chapter.encode(),
        },
        first=("mimetype", b"application/epub+zip"),
    )
    filt = get_filter("epub")
    units = filt.parse(epub).units
    mapping = {str(u.p_index): (ATTACK if u.text == "cat" else u.text) for u in units}
    assert ATTACK in mapping.values(), units
    _safe_or_refused(chapter, lambda: _member(filt.build(epub, mapping), "OEBPS/c1.xhtml"))
    print("  EPUB chapter: breakout refused or inert")


def test_flare_topic_and_keyword():
    ns = 'xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd"'
    topic = (
        f'<?xml version="1.0" encoding="utf-8"?>\n<html {ns}><body><!--><a title="-->'
        '<p>Hi <img alt="cat" src="a.png" /> there</p><!--><b title="-->'
        '<MadCap:keyword term="cat" /></body></html>\n'
    )
    data = _zip(
        {"Content/T.htm": topic.encode(), "Help.flprj": b'<?xml version="1.0"?><CatapultProject/>'}
    )
    filt = get_filter("flprjzip")
    units = filt.parse(data).units
    mapping = {str(u.p_index): (ATTACK if u.text == "cat" else u.text) for u in units}
    assert sum(v == ATTACK for v in mapping.values()) == 2, units  # alt + keyword term
    _safe_or_refused(topic, lambda: _member(filt.build(data, mapping), "Content/T.htm"))
    # an ordinary topic still translates its alt text and keyword
    plain = topic.replace('<!--><a title="-->', "").replace('<!--><b title="-->', "")
    data = _zip(
        {"Content/T.htm": plain.encode(), "Help.flprj": b'<?xml version="1.0"?><CatapultProject/>'}
    )
    units = filt.parse(data).units
    out = _member(
        filt.build(data, {str(u.p_index): ("chat" if u.text == "cat" else u.text) for u in units}),
        "Content/T.htm",
    )
    assert 'alt="chat"' in out and 'term="chat"' in out, out
    print("  Flare topic: alt and keyword breakouts refused or inert; plain topic translated")


def test_email_html_part():
    html_part = '<!--><a title="--> <p>Hi <img alt="cat"> there</p>'
    eml = (
        "From: a@example.com\r\nTo: b@example.com\r\nSubject: Hi\r\nMIME-Version: 1.0\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n\r\n' + html_part + "\r\n"
    ).encode()
    filt = get_filter("eml")
    units = filt.parse(eml).units
    mapping = {str(u.p_index): (ATTACK if u.text == "cat" else u.text) for u in units}
    assert ATTACK in mapping.values(), units
    _safe_or_refused(html_part, lambda: filt.build(eml, mapping).decode().split("\r\n\r\n", 1)[1])
    print("  email HTML part: breakout refused or inert")


def test_htmlhelp_sitemap():
    hhc = (
        '<HTML><BODY><UL><LI><OBJECT type="text/sitemap"><!--><a title="-->'
        '<param name=Name value="cat"><param name="Local" value="a.htm">'
        "</OBJECT></UL></BODY></HTML>"
    )
    filt = get_filter("hhc")
    data = hhc.encode()
    units = filt.parse(data).units
    _safe_or_refused(hhc, lambda: filt.build(data, {str(u.p_index): ATTACK for u in units}))
    ok = (
        '<HTML><BODY><UL><LI><OBJECT type="text/sitemap"><param name="Name" value="cat">'
        "</OBJECT></UL></BODY></HTML>"
    )
    out = filt.build(ok.encode(), {"0": "le chat"}).decode()
    assert 'value="le chat"' in out, out
    print("  HTML Help sitemap: breakout refused or inert; ordinary value readable")


def test_mdx_attribute():
    from app.filters.mdx_filter import MdxFilter

    filt = MdxFilter()
    src = b'# Title\n\n<Figure alt="A cat" />\n'
    units = filt.parse(src).units
    k = next(u.p_index for u in units if u.text == "A cat")
    out = filt.build(src, {str(k): 'x" onClick={() => alert(1)} y="<b>'}).decode()
    assert "{" not in out and "<b>" not in out and '"x"' not in out, out
    assert (
        'alt="x&quot; onClick&#61;&#123;() &#61;&#62; alert(1)&#125; y&#61;&quot;&#60;b&#62;"'
        in out
    ), out
    assert filt.build(src, {str(k): "A cat"}) == src
    out = filt.build(src, {str(k): "Un chat l'ami"}).decode()
    assert 'alt="Un chat l\'ami"' in out, out
    print("  MDX attribute: quotes, braces, angle brackets and = written as references")


def test_fuzz_malformed_pages():
    """The #672 gate fuzz corpus (seeded): every unit, text and attribute, translated to a
    breakout payload. No output may carry an event handler (html_guard parse here; real
    Chrome in the browser step)."""
    frags = [
        "<p>",
        "</p>",
        '<a title="x>y">',
        "<a title='x>y'>",
        "<a title=x>",
        '<a title = "a>b" >',
        "<!-- c -->",
        "<!-- a --!> b -->",
        "<!-->",
        "<![CDATA[ x>y ]]>",
        '<!DOCTYPE html "a>b">',
        '<?x "a>b"?>',
        "</ x>",
        "</>",
        '<b "a>b">',
        '<a title=x"y>',
        '<a title= = "x>y">',
        '<a/title="x>y">',
        '<title>T "q" <b>i</b></title>',
        '<textarea><p title="a>b"></textarea>',
        '<script>var a="</p>";</script>',
        "<style>p{}</style>",
        '<xmp><a title="></xmp>',
        '<noscript><a title="x></noscript>',
        "Hello world. ",
        'It\'s "quoted". ',
        "&amp; &lt; ",
        '<img alt="a > b" src=x>',
        '<input value="v">',
        '<div\ntitle="a>b"\n>',
        "<br/>",
        '<p title="unclosed >',
        "<svg><![CDATA[<b>]]></svg>",
        "<math><mi>x</mi></math>",
        '<iframe srcdoc="<b>x</b>"></iframe>',
        "<a\ttitle='a\"b'>",
        '<p a=b=c "d>e">',
        "<plaintext>",
        "<% x %>",
        "<?php echo 1; ?>",
        "<b>bold</b>",
        '<i title="t">it</i>',
        '<svg><style><a title="</style>',
        "<!--->",
        '</ <a title=">',
        "<select>",
        "<svg><title>",
        '<img alt="cat">',
    ]
    rng = random.Random(676)
    filt = get_filter("html")
    built = refused = 0
    for _n in range(400):
        src = "".join(rng.choice(frags) for _ in range(rng.randint(3, 14)))
        try:
            units = filt.parse(src.encode()).units
        except InputLimitError:
            continue
        mapping = {
            str(i): BREAKOUT + "".join(re.findall(r"⟦[^⟧]*⟧", u.text)) for i, u in enumerate(units)
        }
        if _safe_or_refused(src, lambda s=src, m=mapping: filt.build(s.encode(), m)) is None:
            refused += 1
        else:
            built += 1
    assert built > 200, (built, refused)
    print(f"  fuzz: {built} malformed pages built without an event handler, {refused} refused")


def _chrome_corpus():
    """Kept for the browser step: a seeded corpus of malformed pages whose elements and
    attributes html_guard must read exactly as Chrome's DOMParser does."""
    frags = [
        "<p>",
        "</p>",
        '<a title="x>y">',
        "<!-->",
        "<!--->",
        "<!-- a --!> b -->",
        "</ x>",
        '<a title= = "x>y">',
        '<xmp><a title="></xmp>',
        '<noscript><a title="x></noscript>',
        '<textarea><p title="a>b"></textarea>',
        '<title>T "q" <b>i</b></title>',
        '<svg><style><a title="</style>',
        "<svg>",
        "</svg>",
        "<math>",
        "<mi>",
        "<mtext>",
        "<foreignObject>",
        "</foreignObject>",
        "<svg><title>",
        "<select>",
        "</select>",
        "<option>",
        "<table>",
        "<td>",
        '<annotation-xml encoding="text/html">',
        "<font color=red>",
        '<iframe><b title="</iframe>">',
        "<svg><![CDATA[<b>]]></svg>",
        '<script><!--<script>x</script>-->"</script>',
        '<span title="x" title="y">',
        '</p title=">">',
        '<img alt="a > b" src=x>',
        '<b "a>b">',
        '<p a=b=c "d>e">',
    ]
    rng = random.Random(1676)
    return ["".join(rng.choice(frags) for _ in range(rng.randint(3, 14))) for _ in range(600)]


_SYNTH = {
    "html",
    "head",
    "body",
    "tbody",
    "tr",
    "p",
    "colgroup",
    "a",
    "b",
    "i",
    "font",
    "span",
    "option",
}
_DROPPED = ("td", "th", "tr", "tbody", "caption", "col", "frame")


def test_chrome():
    """Real Chromium (DOMParser, scripting off; the html_guard mode of the same name):
    (1) no output built above carries an on* attribute, (2) html_guard reads every element
    and attribute of a malformed-page corpus exactly as Chrome does."""
    required = os.environ.get("LOC_TMS_BROWSER_REQUIRED") == "1"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        assert not required, exc
        print(f"  Chrome step skipped: playwright not installed ({exc})")
        return
    import html as _html

    from app.filters.html_guard import browser_view

    corpus = _chrome_corpus()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            assert not required, exc
            print(f"  Chrome step skipped: chromium will not launch ({str(exc)[:120]})")
            return
        page = browser.new_page()
        handlers = page.evaluate(
            """docs => docs.map(d => { const doc = new DOMParser().parseFromString(d, 'text/html');
              const bad = []; for (const el of doc.querySelectorAll('*'))
                for (const a of el.attributes) if (/^on/i.test(a.name))
                  bad.push(el.localName + '@' + a.name);
              return bad; })""",
            [out for _src, out in _CHROME],
        )
        src_handlers = page.evaluate(
            """docs => docs.map(d => { const doc = new DOMParser().parseFromString(d, 'text/html');
              const bad = []; for (const el of doc.querySelectorAll('*'))
                for (const a of el.attributes) if (/^on/i.test(a.name))
                  bad.push(el.localName + '@' + a.name);
              return bad; })""",
            [src for src, _out in _CHROME],
        )
        trees = page.evaluate(
            """docs => docs.map(d => { const doc = new DOMParser().parseFromString(d, 'text/html');
              return Array.from(doc.querySelectorAll('*')).map(el =>
                [el.localName.toLowerCase(),
                 Array.from(el.attributes).map(a => [a.name, a.value])]); })""",
            corpus,
        )
        version = browser.version
        browser.close()
    for (src, out), bad, had in zip(_CHROME, handlers, src_handlers, strict=True):
        assert not set(bad) - set(had), (src, out, bad)
    mismatches = []
    for doc, tree in zip(corpus, trees, strict=True):
        chrome = {(n, tuple(sorted((a, v) for a, v in at))) for n, at in tree}
        ours = {
            (st.name, tuple(sorted((a, _html.unescape(v or "")) for a, v in st.attrs)))
            for st in browser_view(doc, scripting=False, legacy_select=False).starts
        }
        missed = {x for x in chrome - ours if x[1] or x[0] not in _SYNTH}
        extra = {x for x in ours - chrome if x[1] and x[0] not in _DROPPED}
        if missed or extra:
            mismatches.append((doc, missed, extra))
    assert not mismatches, mismatches[:3]
    print(
        f"  Chrome {version}: {len(_CHROME)} built pages have no new on* attribute; "
        f"html_guard reads {len(corpus)} malformed pages exactly as Chrome"
    )


META_PAGE = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="robots" content="noindex, nofollow">\n'
    '<meta http-equiv="content-language" content="en">\n'
    '<meta name="Description" content="Learn how to set up &amp; run a project.">\n'
    "<meta name=keywords content='setup, onboarding'>\n"
    '<meta property="og:title" content="Getting started">\n'
    '<meta property="og:description" content="Set up your first project">\n'
    '<meta property="og:type" content="article">\n'
    '<meta property="og:url" content="https://example.com/start">\n'
    '<meta property="og:image" content="https://example.com/a.png">\n'
    '<meta name="twitter:card" content="summary">\n'
    '<meta name="twitter:title" content="Start here">\n'
    '<meta name="twitter:description" content="Your first project">\n'
    '<meta name="theme-color" content="#ffffff">\n'
    '<meta name="generator" content="Hugo 0.120">\n'
    '<meta name="description" http-equiv="refresh" content="5; url=next.html">\n'
    '<meta name="description" content="12345">\n'
    "<title>Getting started</title>\n</head>\n<body>\n"
    '<p>Open the dashboard and click <b>New project</b>.</p>\n<img alt="A cat" src="c.png">\n'
    "</body>\n</html>\n"
)
META_STYLES = [
    ("meta:description", "Learn how to set up & run a project."),
    ("meta:keywords", "setup, onboarding"),
    ("meta:og-title", "Getting started"),
    ("meta:og-description", "Set up your first project"),
    ("meta:twitter-title", "Start here"),
    ("meta:twitter-description", "Your first project"),
]
META_ATTACK = '"><script>alert(1)</script> onload="alert(2)" <b>x'


def _meta_mapping(units, value: str) -> dict:
    return {
        str(u.p_index): (value + u.text if (u.style or "").startswith("meta:") else u.text)
        for u in units
    }


def test_page_meta_733():
    """#733: the description / keywords / og / twitter title and description metas are
    offered, numbered after every body and a11y unit (so existing indexes never move);
    every other meta stays byte for byte; a hostile translation stays an inert value;
    the content-language stamp still works; concatenating filters (EPUB, email) do not
    add them; server pages do."""
    import html

    from app.filters import a11y
    from app.filters.html_guard import browser_view

    filt = HtmlFilter()
    data = META_PAGE.encode()
    units = filt.parse(data).units
    meta = [(u.style, u.text) for u in units if (u.style or "").startswith("meta:")]
    assert meta == META_STYLES, meta
    # body + a11y units first, in the order the page without these metas gives them
    stripped = re.sub(r"<meta (?:name|property)=[^>]*>\n", "", META_PAGE).encode()
    base = [(u.p_index, u.text, u.style) for u in filt.parse(stripped).units]
    assert [(u.p_index, u.text, u.style) for u in units[: len(base)]] == base, units
    assert all(u.p_index >= len(base) for u in units if u.style.startswith("meta:"))
    assert units[len(base)].style == "meta:description"
    # gate #733 (after #734): a meta in a translate="no" / notranslate scope is not offered;
    # an unquoted name= still is (read with #734's attribute reader)
    no_tr = META_PAGE.replace("<html", '<html translate="no"', 1).encode()
    assert not [u for u in filt.parse(no_tr).units if u.style.startswith("meta:")]
    own = b'<html><head><meta name="description" translate="no" content="Keep me">'
    own += b'<meta class="notranslate" name=keywords content="keep, me">'
    own += b"<meta name=description content='Offered text'></head><body><p>Hi.</p></body></html>"
    assert [(u.style, u.text) for u in filt.parse(own).units if u.style.startswith("meta:")] == [
        ("meta:description", "Offered text")
    ], filt.parse(own).units
    # no-op builds are byte-identical
    assert filt.build(data, {}) == data
    assert filt.build(data, {str(u.p_index): u.text for u in units}) == data
    # a hostile translation is one inert attribute value
    out = filt.build(data, _meta_mapping(units, META_ATTACK)).decode()
    assert not _event_attrs(out) and "<script>" not in out, out
    view = browser_view(out)
    assert [st.name for st in view.starts] == [st.name for st in browser_view(META_PAGE).starts]
    got = {}
    for st in view.starts:
        attrs = dict(st.attrs)
        if st.name == "meta" and "content" in attrs:
            key = attrs.get("property") or attrs.get("name") or attrs.get("http-equiv")
            got.setdefault(key.lower(), html.unescape(attrs["content"]))
    assert got["description"] == META_ATTACK + "Learn how to set up & run a project.", got
    assert got["og:title"] == META_ATTACK + "Getting started", got
    assert got["twitter:description"] == META_ATTACK + "Your first project", got
    for key, val in (
        ("viewport", "width=device-width, initial-scale=1"),
        ("robots", "noindex, nofollow"),
        ("og:type", "article"),
        ("og:url", "https://example.com/start"),
        ("og:image", "https://example.com/a.png"),
        ("twitter:card", "summary"),
        ("theme-color", "#ffffff"),
        ("generator", "Hugo 0.120"),
    ):
        assert got[key] == val, (key, got)
    offered = re.compile(r"Description|keywords|og:title|og:desc|twitter:title|twitter:desc")
    for line in META_PAGE.splitlines():
        if line.startswith("<meta") and (not offered.search(line) or "12345" in line):
            assert line in out, line  # untouched metas keep their original bytes
        elif line.startswith("<meta") and 'http-equiv="refresh"' not in line:
            assert line not in out, line
    assert 'content="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt; onload=&quot;' in out, out
    # the language stamp still rewrites content-language and only it
    loc = filt.build_localized(data, _meta_mapping(units, "FR "), "fr-FR").decode()
    assert '<meta http-equiv="content-language" content="fr-FR">' in loc, loc
    assert '<html lang="fr-FR">' in loc and 'content="FR Getting started"' in loc, loc
    assert '<meta property="og:type" content="article">' in loc, loc
    # a desync page: the meta value is written opaque, never live markup
    desync = b'<!--><a title="--> <meta name="description" content="Hello world">'
    dunits = filt.parse(desync).units
    assert any(u.style == "meta:description" for u in dunits), dunits
    _safe_or_refused(desync.decode(), lambda: filt.build(desync, _meta_mapping(dunits, ATTACK)))
    # full-text documents leave the appended units out, like the a11y ones
    assert a11y.is_appended_style("meta:og-title") and a11y.is_appended_style("a11y:img-alt")
    assert not a11y.is_appended_style("") and not a11y.is_a11y_style("meta:description")
    # server pages offer them too (after their a11y units); code values are skipped
    php = get_filter("phtml")
    page = (
        b'<?php $t = "x"; ?><html><head><meta name="description" content="About us">'
        b'<meta name="keywords" content="<?= $kw ?>"></head><body><p>Hi</p>'
        b'<img alt="A cat"></body></html>'
    )
    punits = php.parse(page).units
    assert [(u.style, u.text) for u in punits[-2:]] == [
        ("a11y:img-alt", "A cat"),
        ("meta:description", "About us"),
    ], punits
    pout = php.build(page, _meta_mapping(punits, ATTACK)).decode()
    assert 'content="x&quot; onfocus=alert(1) y=&quot;About us"' in pout, pout
    assert php.build(page, {}) == page
    # EPUB chapters and e-mail parts are concatenated: no per-page meta units there
    from app.filters import email_filter, epub_filter

    assert epub_filter._html().page_meta is False and email_filter._HTML.page_meta is False
    print("  #733 page metadata: 6 metas offered and appended, others untouched, values inert")


def main():
    for test in (
        test_page_meta_733,
        test_issue_reproductions,
        test_ordinary_pages_unchanged,
        test_strict_value_is_opaque,
        test_check_build_rules,
        test_server_pages,
        test_epub_chapter,
        test_flare_topic_and_keyword,
        test_email_html_part,
        test_htmlhelp_sitemap,
        test_mdx_attribute,
        test_fuzz_malformed_pages,
        test_chrome,
    ):
        test()
    print("verify_html_attr_browser_desync OK")


if __name__ == "__main__":
    main()
