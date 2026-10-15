"""HTML inline-tag fidelity (bug T3-01).

The pre-fix HTML filter split text at EVERY tag boundary, so an inline-formatted
sentence like ``<p>Hello <b>world</b>. This is <i>bold</i> text.</p>`` shredded
into SIX segments ("Hello","world",".","This is","bold","text.") — a lone "." sent
to the provider as its own context-free unit, inline_tags NULL. This suite proves
the reworked filter (mirroring docx_filter.py / xliff_filter.py) makes each
BLOCK-level element ONE unit with paired placeholder tokens, so:

  * the inline sentence is TWO sentence segments (not six), no lone "." segment,
    inline_tags populated;
  * translate (mock) + export re-materializes the EXACT original <b>/<i> tags
    around the translated words, with NO per-fragment "[es-ES]" artifacts;
  * nested / adjacent inline tags and self-closing <br>/<img> round-trip;
  * a plain no-inline <p> still splits into sentence segments with NO tokens,
    byte-identical to the pre-fix build.

All ASCII output (Windows consoles); the non-ASCII placeholder tokens are built
via the segment helpers, never printed.
"""

import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_htmlfid_")

from fastapi.testclient import TestClient

from app.db import engine, init_db
from app.filters import get_filter
from app.main import app
from app.models import Segment
from app.services.segment import (
    find_placeholders,
    ph_close,
    ph_open,
    ph_point,
    split_sentences,
)

INLINE_SAMPLE = "<p>Hello <b>world</b>. This is <i>bold</i> text.</p>"


def _unit_segments(unit_text: str) -> list[str]:
    """Segment a unit exactly as ingest does (legacy splitter, SRX gate off)."""
    return split_sentences(unit_text)


# --------------------------------------------------------------------------- #
# 1. One block-level unit, two sentence segments, tokens present
# --------------------------------------------------------------------------- #
def test_one_unit_two_sentences():
    filt = get_filter("html")
    units = filt.parse(INLINE_SAMPLE.encode("utf-8")).units
    assert len(units) == 1, f"inline sentence must be ONE block unit, got {len(units)}"

    g1o, g1c = ph_open("g", 1), ph_close("g", 1)
    g2o, g2c = ph_open("g", 2), ph_close("g", 2)
    assert units[0].text == f"Hello {g1o}world{g1c}. This is {g2o}bold{g2c} text.", repr(
        units[0].text
    )

    segs = _unit_segments(units[0].text)
    assert len(segs) == 2, f"must be 2 sentence segments (not 6), got {len(segs)}: {segs!r}"
    assert segs[0] == f"Hello {g1o}world{g1c}.", repr(segs[0])
    assert segs[1] == f"This is {g2o}bold{g2c} text.", repr(segs[1])
    # the pre-fix bug produced a lone "." segment; it must be gone
    assert "." not in segs, f"a lone '.' segment leaked: {segs!r}"
    # the paired token spans survive intact in each segment (no orphaned half)
    assert find_placeholders(segs[0]) == [g1o, g1c], find_placeholders(segs[0])
    assert find_placeholders(segs[1]) == [g2o, g2c], find_placeholders(segs[1])
    # the map keys the tokens back to the ORIGINAL tags for build()
    assert units[0].inline_map["1"]["open"] == "<b>", units[0].inline_map
    assert units[0].inline_map["1"]["close"] == "</b>", units[0].inline_map
    assert units[0].inline_map["2"]["open"] == "<i>", units[0].inline_map
    print("  1) inline sentence -> ONE unit, TWO segments, no lone '.', tokens mapped")


# --------------------------------------------------------------------------- #
# 2. Full ingest -> translate (mock) -> export round-trip through the app
# --------------------------------------------------------------------------- #
def test_ingest_export_roundtrip():
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "HTML Fidelity",
            "source_locale": "en-US",
            "target_locales": "es-ES",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("page.html", INLINE_SAMPLE.encode("utf-8"), "text/html")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/documents/" in r.headers["location"], r.text
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])

    from sqlmodel import Session, select

    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "es-ES"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        rows = [(x.id, x.source_text, x.inline_tags) for x in segs]

    # Exactly two source segments (NOT six), no lone "." unit, inline_tags populated.
    assert len(rows) == 2, f"ingest must create 2 segments, got {len(rows)}: {[r[1] for r in rows]}"
    assert all(t.strip() != "." for _i, t, _tg in rows), f"lone '.' segment created: {rows!r}"
    g1o, g1c = ph_open("g", 1), ph_close("g", 1)
    g2o, g2c = ph_open("g", 2), ph_close("g", 2)
    assert rows[0][2] == [g1o, g1c], f"seg0 inline_tags not populated: {rows[0][2]!r}"
    assert rows[1][2] == [g2o, g2c], f"seg1 inline_tags not populated: {rows[1][2]!r}"

    # Translate each segment (mock prefixes "[es-ES] " once per segment).
    for sid, src, _tg in rows:
        client.post(f"/segments/{sid}/save", data={"target_text": "[es-ES] " + src, "tte_ms": "0"})

    resp = client.get(f"/documents/{doc_id}/export?locale=es-ES")
    assert resp.status_code == 200, resp.text
    out = resp.content.decode("utf-8")

    # Original inline tags re-materialized around the SAME (translated) words.
    assert "<b>world</b>" in out, f"<b> not re-materialized around 'world': {out!r}"
    assert "<i>bold</i>" in out, f"<i> not re-materialized around 'bold': {out!r}"
    assert out.startswith("<p>") and out.endswith("</p>"), f"block structure lost: {out!r}"
    # No stray placeholder tokens leaked into the export.
    assert find_placeholders(out) == [], f"placeholder tokens leaked into export: {out!r}"
    # ONE "[es-ES]" per SENTENCE (2), not the pre-fix per-fragment five.
    assert out.count("[es-ES]") == 2, (
        f"per-fragment artifacts present ({out.count('[es-ES]')} tags): {out!r}"
    )
    # The pre-fix lone-"." artifact ("[es-ES] . ") must be absent.
    assert "[es-ES] ." not in out, f"lone-'.' fragment artifact present: {out!r}"
    for sid, src, _tg in rows:
        target = src + ' <img src=x onerror="alert(1)">'
        client.post(f"/segments/{sid}/save", data={"target_text": target, "tte_ms": "0"})
    escaped = client.get(f"/documents/{doc_id}/export?locale=es-ES")
    assert escaped.status_code == 200, escaped.text
    assert "<img" not in escaped.text and "&lt;img" in escaped.text
    print("  2) ingest->translate->export preserves inline tags and escapes target markup")


# --------------------------------------------------------------------------- #
# 3. Nested + adjacent inline tags round-trip at the filter level
# --------------------------------------------------------------------------- #
def _filter_roundtrip(html: str) -> str:
    filt = get_filter("html")
    units = filt.parse(html.encode("utf-8")).units
    mapping = {str(u.p_index): "[X] " + u.text for u in units}
    return filt.build(html.encode("utf-8"), mapping).decode("utf-8")


def test_nested_and_adjacent():
    # nested: <b><i>...</i></b>
    nested = "<p>a <b><i>bc</i></b> d</p>"
    units = get_filter("html").parse(nested.encode("utf-8")).units
    assert len(units) == 1, units
    g1o, g1c = ph_open("g", 1), ph_close("g", 1)
    g2o, g2c = ph_open("g", 2), ph_close("g", 2)
    assert units[0].text == f"a {g1o}{g2o}bc{g2c}{g1c} d", repr(units[0].text)
    out = _filter_roundtrip(nested)
    assert "<b><i>bc</i></b>" in out, f"nested tags lost: {out!r}"
    assert "[X] a" in out, out

    # adjacent: <b>x</b><i>y</i>
    adjacent = "<p><b>x</b><i>y</i></p>"
    out2 = _filter_roundtrip(adjacent)
    assert "<b>x</b>" in out2 and "<i>y</i>" in out2, f"adjacent tags lost: {out2!r}"
    assert find_placeholders(out2) == [], out2

    # a link with attributes round-trips verbatim
    link = '<p>See <a href="https://x.example/y?a=1&amp;b=2">the docs</a> now.</p>'
    out3 = _filter_roundtrip(link)
    assert '<a href="https://x.example/y?a=1&amp;b=2">the docs</a>' in out3, (
        f"anchor lost: {out3!r}"
    )
    print("  3) nested / adjacent / attributed inline tags round-trip verbatim")


# --------------------------------------------------------------------------- #
# 4. Self-closing / void inline tags (br, img) become standalone tokens
# --------------------------------------------------------------------------- #
def test_void_tags():
    br = "<p>line one<br>line two.</p>"
    units = get_filter("html").parse(br.encode("utf-8")).units
    assert len(units) == 1, units
    x1 = ph_point("x", 1)
    assert units[0].text == f"line one{x1}line two.", repr(units[0].text)
    out = _filter_roundtrip(br)
    assert "<br>" in out and find_placeholders(out) == [], f"<br> not restored: {out!r}"

    img = '<p>see <img src="a.png" alt="A"> here.</p>'
    out2 = _filter_roundtrip(img)
    # The <img> point token re-materializes as the original tag. Its alt text is now
    # its own translatable unit (output-a11y train, VPAT 504.2), so the round-trip's
    # "[X] " prefix lands in the alt value too; everything else is verbatim.
    assert '<img src="a.png" alt="[X] A">' in out2, f"<img> not restored verbatim: {out2!r}"

    # self-closing XHTML syntax also -> point token
    xbr = "<p>a<br/>b.</p>"
    out3 = _filter_roundtrip(xbr)
    assert "<br/>" in out3 and find_placeholders(out3) == [], f"<br/> not restored: {out3!r}"
    print("  4) void/self-closing <br>/<img> -> standalone token, restored verbatim")


# --------------------------------------------------------------------------- #
# 5. Plain no-inline paragraph: 2 sentence segments, NO tokens, byte-identical
# --------------------------------------------------------------------------- #
def test_plain_no_inline_unchanged():
    plain = "<p>Sentence one. Sentence two.</p>"
    units = get_filter("html").parse(plain.encode("utf-8")).units
    assert len(units) == 1, units
    assert units[0].text == "Sentence one. Sentence two.", repr(units[0].text)
    assert find_placeholders(units[0].text) == [], "plain block must carry NO tokens"
    assert units[0].inline_map == {}, units[0].inline_map

    segs = _unit_segments(units[0].text)
    assert segs == ["Sentence one.", "Sentence two."], (
        f"plain must split into 2 sentences: {segs!r}"
    )

    mapping = {"0": "[X] Sentence one. [X] Sentence two."}
    new_out = get_filter("html").build(plain.encode("utf-8"), mapping).decode("utf-8")
    assert new_out == "<p>[X] Sentence one. [X] Sentence two.</p>", repr(new_out)

    # Byte-identical to the PRE-FIX filter's build for the no-inline-tag case.
    old_filt = _load_prefix_filter()
    if old_filt is not None:
        old_out = old_filt.build(plain.encode("utf-8"), mapping).decode("utf-8")
        assert new_out == old_out, f"no-inline build regressed:\n new={new_out!r}\n old={old_out!r}"
        print("  5) plain <p>: 2 segments, no tokens, build byte-identical to pre-fix")
    else:
        print("  5) plain <p>: 2 segments, no tokens (pre-fix filter unavailable for byte-diff)")


# --------------------------------------------------------------------------- #
# 5b. #672: written text keeps ' and " (only &, < and > are escaped in text)
# --------------------------------------------------------------------------- #
def test_text_quotes_not_entities():
    f = get_filter("html")
    src = b'<html lang="en"><body><p>Don\'t stop.</p><p>Say "hi" <b>now</b>.</p></body></html>'
    units = f.parse(src).units
    same = {str(u.p_index): u.text for u in units}
    assert f.build(src, same) == src, "a no-change rebuild must be byte-identical"
    out = f.build(src, {"0": "N'arrêtez pas.", "1": units[1].text.replace("Say", "Dites")})
    out = out.decode("utf-8")
    assert "<p>N'arrêtez pas.</p>" in out and "&#x27;" not in out, out
    assert 'Dites "hi" <b>now</b>.' in out and "&quot;" not in out, out
    # Markup in a translation is still inert text; an existing entity is kept.
    out = f.build(b"<p>Hello</p>", {"0": "\"' &amp; & <i>x</i> a > b"}).decode()
    assert out == "<p>\"' &amp; &amp; &lt;i&gt;x&lt;/i&gt; a &gt; b</p>", out
    # Attribute values (alt text) still escape quotes: they are attribute context.
    img = b'<p><img alt="cat"> x</p>'
    (alt,) = [u for u in f.parse(img).units if u.style == "a11y:img-alt"]
    out = f.build(img, {str(alt.p_index): "a \"b\" 'c'"}).decode()
    assert 'alt="a &quot;b&quot; &#x27;c&#x27;"' in out, out
    print("  5b) #672: text keeps ' and \" (no-change rebuild byte-identical); alt still escaped")


# --------------------------------------------------------------------------- #
# 6. Fail-pre-fix proof: the OLD algorithm shreds the sentence into 6 fragments
# --------------------------------------------------------------------------- #
def _load_prefix_filter():
    """Load the pre-fix HtmlFilter from git (main / origin/main), without polluting
    the live filter registry. Returns an instance, or None if unavailable / the ref
    already carries the fixed code."""
    for ref in ("main", "origin/main"):
        try:
            src = subprocess.run(
                ["git", "show", f"{ref}:app/filters/html_filter.py"],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except Exception:
            continue
        if not src or "_iter_units" in src:
            continue  # that ref already has the fixed filter
        import app.filters.base as _base

        real_register = _base.register
        _base.register = lambda cls: cls  # no-op so we don't overwrite the live filter
        try:
            ns = {"__name__": "prefix_html_filter"}
            exec(compile(src, "<prefix_html_filter>", "exec"), ns)
        finally:
            _base.register = real_register
        cls = ns.get("HtmlFilter")
        if cls is not None:
            return cls()
    return None


def _prefix_style_segments(html: str) -> list[str]:
    """Self-contained replica of the PRE-FIX pipeline: every text token between
    tags is its own unit, then each is sentence-split. This is the bug — it shreds
    an inline sentence into fragments including a lone '.'."""
    toks = re.split(r"(<[^>]+>)", html)
    skip = 0
    segs: list[str] = []
    for t in toks:
        if t.startswith("<") and t.endswith(">"):
            m = re.match(r"<\s*(/?)\s*([a-zA-Z0-9]+)", t)
            if m and m.group(2).lower() in ("script", "style"):
                if m.group(1):
                    skip = max(0, skip - 1)
                elif not t.rstrip(">").rstrip().endswith("/"):
                    skip += 1
        elif skip == 0 and any(c.isalnum() for c in t):
            segs.extend(split_sentences(t.strip()))
    return segs


def test_fail_pre_fix():
    # (a) self-contained replica of the pre-fix behavior -> 6 fragments incl. "."
    old_segs = _prefix_style_segments(INLINE_SAMPLE)
    assert len(old_segs) == 6, f"pre-fix replica should shred into 6, got {old_segs!r}"
    assert "." in old_segs, f"pre-fix replica should emit a lone '.': {old_segs!r}"

    # (b) the REAL pre-fix filter loaded from git, when available, does the same.
    old_filt = _load_prefix_filter()
    if old_filt is not None:
        pre_units = old_filt.parse(INLINE_SAMPLE.encode("utf-8")).units
        pre_segs: list[str] = []
        for u in pre_units:
            pre_segs.extend(split_sentences(u.text))
        assert len(pre_segs) == 6 and "." in pre_segs, (
            f"git pre-fix filter should shred into 6 incl. '.': {pre_segs!r}"
        )
        loaded = "git"
    else:
        loaded = "replica-only"

    # And the FIXED filter now yields 2, no lone "." — the same input, fixed.
    new_units = get_filter("html").parse(INLINE_SAMPLE.encode("utf-8")).units
    new_segs: list[str] = []
    for u in new_units:
        new_segs.extend(split_sentences(u.text))
    assert len(new_segs) == 2 and "." not in new_segs, f"post-fix should be 2: {new_segs!r}"
    print(f"  6) fail-pre-fix: pre-fix=6 frags (lone '.'), post-fix=2 segments [{loaded}]")


# --------------------------------------------------------------------------- #
# 7. #734: the HTML5 translate attribute (and class="notranslate") is honoured
# --------------------------------------------------------------------------- #
_PH_RE = re.compile("(⟦[^⟧]*⟧)")


def _shout(text: str) -> str:
    """A 'translation' that changes every word but leaves placeholder tokens alone."""
    return "".join(p if i % 2 else p.upper() for i, p in enumerate(_PH_RE.split(text)))


def _tno(fmt: str, html: str):
    filt = get_filter(fmt)
    data = html.encode("utf-8")
    units = filt.parse(data).units
    # A no-op build and re-submitting the source text both stay byte-identical.
    assert filt.build(data, {}) == data, f"{fmt}: no-op build changed bytes"
    same = {str(u.p_index): u.text for u in units}
    assert filt.build(data, same) == data, f"{fmt}: source resubmit changed bytes"
    out = filt.build(data, {str(u.p_index): _shout(u.text) for u in units}).decode("utf-8")
    return [u.text for u in units], out


def test_translate_no_734():
    x1 = ph_point("x", 1)
    # Inline span / b / code with translate="no": one point token, content verbatim.
    page = (
        '<html lang="en"><body>\n'
        '<p>Click <span translate="no">Save as draft</span> to keep your work.</p>\n'
        '<p>Our product <b translate="no">Open Trails</b> is free for schools.</p>\n'
        '<p translate="no">Terms of Service, version 3. Published in English only.</p>\n'
        '<p>The setting is called <code translate="no">Allow guests</code> here.</p>\n'
        "</body></html>"
    )
    texts, out = _tno("html", page)
    assert texts == [
        f"Click {x1} to keep your work.",
        f"Our product {x1} is free for schools.",
        f"The setting is called {x1} here.",
    ], texts
    assert '<p>CLICK <span translate="no">Save as draft</span> TO KEEP YOUR WORK.</p>' in out, out
    assert '<b translate="no">Open Trails</b> IS FREE' in out, out
    assert '<code translate="no">Allow guests</code> HERE.' in out, out
    # Block-level translate="no": no unit, its bytes untouched by the export.
    assert '<p translate="no">Terms of Service, version 3. Published in English only.</p>' in out, (
        out
    )

    # A whole block (div) and its descendants, including inline tags and an
    # unclosed <p>, are not offered; the next sibling block is.
    block = (
        '<div translate="no"><h2>Legal</h2><p>Line <i>one</i>.<p>Line two.</div>'
        "<p>After the block.</p>"
    )
    texts, out = _tno("html", block)
    assert texts == ["After the block."], texts
    assert out.startswith('<div translate="no"><h2>Legal</h2><p>Line <i>one</i>.<p>Line two.</div>')
    unclosed = '<p translate="no">Keep this.<p>Translate this.</p>'
    assert _tno("html", unclosed)[0] == ["Translate this."], "an implied </p> ends the scope"

    # translate="yes" re-enables a subtree inside a no subtree (block and inline).
    nested = (
        '<div translate="no"><p>Keep me.</p><p translate="yes">Translate me.</p></div>'
        '<p translate="no">Brand <span translate="yes">tagline here</span> end.</p>'
    )
    texts, out = _tno("html", nested)
    g2o, g2c, x3 = ph_open("g", 2), ph_close("g", 2), ph_point("x", 3)
    assert texts == ["Translate me.", f"{x1}{g2o}tagline here{g2c}{x3}"], texts
    assert "<p>Keep me.</p>" in out and '<p translate="yes">TRANSLATE ME.</p>' in out, out
    assert '<p translate="no">Brand <span translate="yes">TAGLINE HERE</span> end.</p>' in out, out

    # Attribute value / name case and quoting variants; empty = yes; invalid inherits;
    # Google's class="notranslate" counts as no unless translate says otherwise.
    for variant in (
        "TRANSLATE='NO'",
        "translate=No",
        'Translate = "nO"',
        'class="brand notranslate"',
        'translate="no" translate="yes"',  # the first occurrence wins
    ):
        texts, out = _tno("html", f"<p>Press <kbd {variant}>Ctrl</kbd> now.</p>")
        assert texts == [f"Press {x1} now."], (variant, texts)
        assert f"<kbd {variant}>Ctrl</kbd>" in out, (variant, out)
    for variant in ('translate=""', "translate", "translate=YES"):  # = yes, re-enables
        texts, _out = _tno("html", f'<div translate="no"><p {variant}>Words.</p></div>')
        assert texts == ["Words."], (variant, texts)
    for variant in ('translate="false"', 'translate="no "', 'class="notranslate-x"'):
        texts, _out = _tno("html", f"<p {variant}>Words.</p>")  # invalid: inherits yes
        assert texts == ["Words."], (variant, texts)
        texts, _out = _tno("html", f'<div translate="no"><p {variant}>Words.</p></div>')
        assert texts == [], (variant, texts)  # ... and inherits no
    texts, _out = _tno("html", '<p class="notranslate" translate="yes">Words.</p>')
    assert texts == ["Words."], texts
    # A translate= inside another attribute's value is not the attribute.
    texts, _out = _tno("html", '<p title="translate=no">Words.</p>')
    assert texts == ["Words."], texts

    # Accessibility attributes inside a no subtree are not offered either.
    a11y = (
        '<div translate="no"><img src="a.png" alt="Logo text">'
        '<a href="/x" title="Home page">Home</a></div>'
        '<p><img src="b.png" alt="A red cover"></p>'
    )
    texts, out = _tno("html", a11y)
    assert texts == ["A red cover"], texts
    assert 'alt="Logo text"' in out and 'title="Home page"' in out, out
    assert 'alt="A RED COVER"' in out, out

    # The same walk serves XHTML and server pages.
    xhtml = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p>Open <span translate="no">Settings</span> now.</p></body></html>'
    )
    texts, out = _tno("html", xhtml)
    assert texts == [f"Open {x1} now."], texts
    assert '<span translate="no">Settings</span> NOW.' in out, out
    jsp = (
        '<p>Hello <%= user.name %>, use <b translate="no">Open Trails</b> today.</p>\n'
        '<p translate="no">Do not touch <%= x %> this.</p>\n'
    )
    texts, out = _tno("jsp", jsp)
    assert len(texts) == 1 and "Open Trails" not in texts[0], texts
    assert '<b translate="no">Open Trails</b> TODAY.' in out, out
    assert '<p translate="no">Do not touch <%= x %> this.</p>' in out, out
    print("  7) #734 translate=no: inline -> verbatim point, block -> no unit, yes re-enables")


def test_moved_walk_guard_734():
    """#734 gate: a jsp / php / epub ... document imported BEFORE #734 that carries
    translate="no" has stored unit indexes the new walk no longer produces. fmt html already
    refused such a document on export; these formats would have written translations into the
    wrong units. Now refused with a re-import message; a current import still exports."""
    import io
    import zipfile
    from types import SimpleNamespace

    from app.services.export import (
        ExportUnreadableBlob,
        _may_carry_translate_attr,
        _refuse_moved_html_walk,
    )

    jsp = (
        b'<p translate="no">Legal text.</p>\n<p>Hello <%= user.name %>, welcome.</p>\n'
        b"<p>Second paragraph.</p>\n"
    )
    filt = get_filter("jsp")
    fresh = [{"p_index": u.p_index, "text": u.text} for u in filt.parse(jsp).units]
    # pre-#734 shape: the translate=no paragraph was unit 0 and every index after it shifted
    old = [{"p_index": 0, "text": "Legal text."}] + [
        {"p_index": u["p_index"] + 1, "text": u["text"]} for u in fresh
    ]
    assert _may_carry_translate_attr("jsp", jsp)
    assert not _may_carry_translate_attr("jsp", b"<p>Plain page.</p>")
    try:
        _refuse_moved_html_walk(
            filt, jsp, SimpleNamespace(id=1, fmt="jsp", skeleton={"units": old})
        )
    except ExportUnreadableBlob as exc:
        assert "Re-import" in str(exc), exc
    else:
        raise AssertionError("a pre-#734 jsp document must be refused, not misaddressed")
    # a document imported with the current walk exports
    _refuse_moved_html_walk(filt, jsp, SimpleNamespace(id=1, fmt="jsp", skeleton={"units": fresh}))
    # zip formats: the marker is found inside an (X)HTML member
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OEBPS/ch1.xhtml", '<p class="notranslate">x</p>')
    assert _may_carry_translate_attr("epub", buf.getvalue())
    assert not _may_carry_translate_attr("epub", b"not a zip")
    print("  8) #734 gate: a pre-#734 server page / epub / flare document is refused on export")


def main() -> None:
    init_db()
    test_one_unit_two_sentences()
    test_ingest_export_roundtrip()
    test_nested_and_adjacent()
    test_void_tags()
    test_plain_no_inline_unchanged()
    test_text_quotes_not_entities()
    test_fail_pre_fix()
    test_translate_no_734()
    test_moved_walk_guard_734()
    print("HTML INLINE FIDELITY OK")


if __name__ == "__main__":
    main()
