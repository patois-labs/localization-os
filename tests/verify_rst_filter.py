"""reStructuredText document filter (.rst, .rest).

Spec checked: the docutils reStructuredText Markup Specification
(https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html) and the directive
reference (https://docutils.sourceforge.io/docs/ref/rst/directives.html); title widths
follow docutils' column_width (East Asian wide/full-width = 2). Fixtures are hand-written.

  identity   empty build and echo build are byte-identical (LF, CRLF, BOM)
  blocks     titles (both styles), paragraphs, ``::`` literal blocks, lists, definition
             lists, admonitions, footnotes, line blocks, list-table, figure caption
  skipped    code-block, include (never followed), comments, targets, substitution
             definitions, directive options, field lists, doctest blocks
  inline     roles, literals, substitutions, references, footnote refs as tokens;
             emphasis/strong and `text <url>`_ as pairs; markup chars escaped on build
  titles     adornment resized to the translated title, CJK counted as 2 columns
  tables     grid and simple tables: re-wrap, row growth, refusal when a cell cannot fit,
             unsupported layouts kept verbatim and reported
  hostile    binary bytes, segment cap, deep nesting, a huge grid

Hermetic + offline. Final line: "RST FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rst_")

from app.filters import get_filter
from app.filters.base import InputLimitError, UnsupportedEncodingError
from app.filters.rst_filter import TableFitError, column_width
from tests._kvres_common import assert_identity, assert_segment_cap

F = get_filter("rst")


def units(data: bytes) -> list[str]:
    return [u.text for u in F.parse(data).units]


DOC = """\
=============
Project Guide
=============

Introduction
============

This is a *simple* paragraph with ``code`` and a :ref:`label <x>` role,
spanning two lines with |sub| and a link_ and `Docs <https://example.com>`_.

.. note:: Remember to save your work.

   A second note paragraph.

.. warning::
   :class: big

   Do not press the red button [1]_.

.. code-block:: python

   print("hello")

Example::

    literal text
    not translated

- First item
- Second item with **bold** text

  Nested paragraph in item.

1. Numbered one
#. Numbered two

Term
   Definition of the term.

.. _link: https://example.com
.. |sub| replace:: substituted
.. [1] The footnote text.
.. include:: other.rst
.. This is a comment
   spanning lines.

:Author: Jane Doe
:Version: 1.0

>>> print("doctest")
doctest

| Line block one
| Line block two

.. list-table:: Fruit table
   :header-rows: 1

   * - Name
     - Colour

.. figure:: img.png
   :alt: alt text

   The figure caption.

.. versionadded:: 2.1
   The new option.
"""


def test_blocks_and_identity():
    data = DOC.encode()
    got = units(data)
    assert got == [
        "Project Guide",
        "Introduction",
        "This is a ⟦g1⟧simple⟦/g1⟧ paragraph with ⟦x2⟧ and a ⟦x3⟧ role, spanning two lines"
        " with ⟦x4⟧ and a ⟦x5⟧ and ⟦g6⟧Docs⟦/g6⟧.",
        "Remember to save your work.",
        "A second note paragraph.",
        "Do not press the red button ⟦x1⟧.",
        "Example",
        "First item",
        "Second item with ⟦g1⟧bold⟦/g1⟧ text",
        "Nested paragraph in item.",
        "Numbered one",
        "Numbered two",
        "Term",
        "Definition of the term.",
        "The footnote text.",
        "Line block one",
        "Line block two",
        "Fruit table",
        "Name",
        "Colour",
        "The figure caption.",
        "The new option.",
    ], got
    for body in (data, data.replace(b"\n", b"\r\n"), b"\xef\xbb\xbf" + data):
        assert_identity("rst", body)
    print("  [blocks] titles, paragraphs, lists, admonitions, footnotes, list-table; skips OK")


def test_translated_round_trip():
    data = DOC.encode()
    src = units(data)
    tr = {str(i): "DE " + t for i, t in enumerate(src)}
    out = F.build(data, tr).decode()
    assert "================\nDE Project Guide\n================\n" in out, out
    assert "DE Introduction\n===============\n" in out, out
    assert (
        "DE This is a *simple* paragraph with ``code`` and a :ref:`label <x>` role, spanning"
        " two lines with |sub| and a link_ and `Docs <https://example.com>`_.\n" in out
    ), out
    assert ".. note:: DE Remember to save your work.\n" in out
    assert "DE Example::\n\n    literal text\n    not translated\n" in out
    assert "- DE Second item with **bold** text\n\n  DE Nested paragraph in item.\n" in out
    assert '.. code-block:: python\n\n   print("hello")\n' in out
    assert ".. include:: other.rst\n" in out and ":Author: Jane Doe\n" in out
    assert "   * - DE Name\n     - DE Colour\n" in out, out
    assert ".. versionadded:: 2.1\n   DE The new option.\n" in out, out
    back = units(out.encode())
    assert back == [tr[str(i)] for i in range(len(src))], back
    print("  [round trip] translated units read back; markup and skipped blocks unchanged OK")


def test_inline_escaping_and_paragraph_start():
    data = b"Price is low.\n\nSecond para.\n"
    out = F.build(data, {"0": "Use *all* the_ items | now", "1": "- not a bullet"})
    assert out == b"Use \\*all\\* the\\_ items \\| now\n\n\\- not a bullet\n", out
    assert len(units(out)) == 2, units(out)  # still two paragraphs, no list item
    # a translation that drops a pair keeps the markup balanced; unknown tokens dropped
    data = b"A **bold** word.\n"
    out = F.build(data, {"0": "Ein ⟦g1⟧fettes⟦/g1⟧ Wort ⟦x9⟧."})
    assert out == b"Ein **fettes** Wort .\n", out
    # gate: a lone close is dropped, an open the translation never closes is closed
    assert F.build(data, {"0": "Ein ⟦g1⟧fettes Wort."}) == b"Ein **fettes Wort.**\n"
    assert F.build(data, {"0": "Ein fettes⟦/g1⟧ Wort."}) == b"Ein fettes Wort.\n"
    print("  [inline] markup characters escaped; pairs rebuilt; unknown tokens dropped OK")


def test_cjk_inline_boundaries():
    """#695 (same pattern as AsciiDoc): docutils recognises inline markup only at a
    boundary (whitespace or punctuation outside the start/end strings). A Japanese or
    Chinese translation glues the markup to letters, so the export showed literal
    asterisks and backticks. The build now puts an escaped space ("\\ ", removed by
    docutils) between a letter and the markup; punctuation and spaces need none."""
    import re

    data = b"Run ``make`` and read *this* and **that** and :ref:`intro` now.\n"
    src = F.parse(data).units[0].text
    toks = re.findall(r"⟦/?[a-z]?\d+⟧", src)
    assert len(toks) == 6, src
    ja = (
        "まず"
        + toks[0]
        + "を"
        + toks[1]
        + "これ"
        + toks[2]
        + "と"
        + toks[3]
        + "あれ"
        + toks[4]
        + "と"
        + toks[5]
        + "。"
    )
    out = F.build(data, {"0": ja}).decode()
    want = "まず\\ ``make``\\ を\\ *これ*\\ と\\ **あれ**\\ と\\ :ref:`intro`。\n"
    assert out == want, out
    # Every markup is still markup on re-parse (the escaped spaces are point tokens).
    again = F.parse(out.encode()).units[0].text
    assert "⟦g" in again and "``" not in again and "*" not in again, again
    # Punctuation is a boundary: no escape. A Latin translation keeps the source bytes.
    ja2 = "（" + toks[0] + "、" + toks[1] + "これ" + toks[2] + "。"
    assert F.build(data, {"0": ja2}).decode() == "（``make``、*これ*。\n"
    de = src.replace("Run", "Starte").replace(" now", " jetzt")
    assert F.build(data, {"0": de}) == data.replace(b"Run", b"Starte").replace(b" now", b" jetzt")
    print("  [cjk] markup glued to CJK letters gets escaped-space boundaries (#695) OK")


def test_title_widths():
    data = b"Title\n=====\n\nBody text.\n"
    out = F.build(data, {"0": "日本語の見出し"}).decode()
    assert out == "日本語の見出し\n==============\n\nBody text.\n", out
    assert column_width("日本語の見出し") == 14
    data = b"########\n Inset \n########\n\nText.\n"
    out = F.build(data, {"0": "Longer inset"}).decode()
    assert out == "###############\n Longer inset \n###############\n\nText.\n", out
    data = b"Long underline\n==============================\n"
    out = F.build(data, {"0": "Kurz"}).decode()
    assert out == "Kurz\n====================\n", out  # the extra 16 columns are kept
    assert units(out.encode()) == ["Kurz"]
    print("  [titles] adornments follow the translated title width (CJK = 2 columns) OK")


GRID = b"""\
+------------+------------+
| Header 1   | Header 2   |
+============+============+
| Cell one   | Cell two   |
+------------+------------+

After.
"""


def test_grid_tables():
    assert units(GRID) == ["Header 1", "Header 2", "Cell one", "Cell two", "After."]
    assert_identity("rst", GRID)
    out = F.build(GRID, {"0": "Kopf", "3": "Zelle zwei ist lang"}).decode()
    assert out == (
        "+------------+------------+\n"
        "| Kopf       | Header 2   |\n"
        "+============+============+\n"
        "| Cell one   | Zelle zwei |\n"
        "|            | ist lang   |\n"
        "+------------+------------+\n"
        "\n"
        "After.\n"
    ), out
    assert units(out.encode())[:4] == ["Kopf", "Header 2", "Cell one", "Zelle zwei ist lang"]
    out = F.build(GRID, {"2": "表のセル"}).decode()
    assert "| 表のセル   |" in out, out
    try:
        F.build(GRID, {"2": "Donaudampfschifffahrt"})
    except TableFitError as e:
        assert "line 4" in str(e) and "Donaudampfschifffahrt" in str(e), str(e)
        assert isinstance(e, InputLimitError)
    else:
        raise AssertionError("an over-wide word must refuse the export")
    span = b"""\
+-------+-------+
| A     | B     |
+-------+       +
| C     |       |
+-------+-------+
"""
    assert units(span) == ["A", "B", "C"]
    out = F.build(span, {"0": "eins zwei"}).decode()
    assert out.splitlines()[1:3] == ["| eins  | B     |", "| zwei  |       |"], out
    assert units(out.encode()) == ["eins zwei", "B", "C"], out
    stairs = b"""\
+-------+-------+
| A     | B     |
|       +-------+
+-------+ C     |
| D     |       |
+-------+-------+
"""
    assert units(stairs) == ["A", "B", "C", "D"]
    try:
        F.build(stairs, {"0": "eins zwei drei"})
    except TableFitError as e:
        assert "cannot grow" in str(e), str(e)
    else:
        raise AssertionError("a row whose last line is another cell's border cannot grow")
    out = F.build(span, {"1": "bb cc dd"}).decode()
    assert out.splitlines()[1:4] == [
        "| A     | bb cc |",
        "+-------+ dd    +",
        "| C     |       |",
    ], out
    broken = b"+-----+-----+\n| a   | b\n+-----+-----+\n"
    r = F.parse(broken)
    assert [u.text for u in r.units] == [] and r.skeleton["rst_skipped_tables"][0]["line"] == 1
    print("  [grid] re-wrap, row growth, CJK cells, refusal, spans, malformed kept OK")


SIMPLE = b"""\
=====  ========
Name   Value
=====  ========
Alpha  First
Beta   Second
       line
=====  ========
"""


def test_simple_tables():
    assert units(SIMPLE) == ["Name", "Value", "Alpha", "First", "Beta", "Second line"]
    assert_identity("rst", SIMPLE)
    out = F.build(SIMPLE, {"1": "Wert", "5": "Zweite", "3": "Erste Zeile hier"}).decode()
    assert out == (
        "=====  ========\n"
        "Name   Wert\n"
        "=====  ========\n"
        "Alpha  Erste Zeile hier\n"
        "Beta   Zweite\n"
        "=====  ========\n"
    ), out
    try:
        F.build(SIMPLE, {"2": "Alphabet"})
    except TableFitError as e:
        assert "first column" in str(e), str(e)
    else:
        raise AssertionError("the first simple-table column cannot wrap")
    three = (
        b"=====  =====  ===\nA      B      C\n"
        b"=====  =====  ===\nx      long   z\n=====  =====  ===\n"
    )
    out = F.build(three, {"4": "wide words"}).decode()
    assert out.splitlines()[3:5] == ["x      wide   z", "       words"], out
    gap = b"=====  =====\nA      B\nspanning text\n=====  =====\n"
    r = F.parse(gap)
    assert "gap" in r.skeleton["rst_skipped_tables"][0]["reason"]
    print("  [simple] wrap, continuation lines, first-column refusal, gaps kept OK")


def test_hostile():
    try:
        F.parse(b"\x89PNG\r\n\x1a\n\x00\x00\x81\x8d\x8f\x90binary")
    except UnsupportedEncodingError:
        pass
    else:
        raise AssertionError("binary must be refused")
    assert_segment_cap("rst", lambda i: b"Paragraph %d.\n\n" % i)
    deep = b"".join(b"  " * k + b"- item\n\n" for k in range(200))
    F.parse(deep)
    big = (
        b"+"
        + b"-+" * 1500
        + b"\n"
        + (b"|" + b" |" * 1500 + b"\n") * 300
        + b"+"
        + b"-+" * 1500
        + b"\n"
    )
    r = F.parse(big)
    assert r.skeleton.get("rst_skipped_tables"), "an oversized grid is kept verbatim"
    patho = b"*" * 20000 + b"\n\n" + b"`" * 20000 + b"\n\n" + b"|a " * 5000 + b"\n"
    F.parse(patho)
    F.parse(b"=====  =====\nx\n\n" * 30000)  # unclosed simple tables: linear end search
    print("  [hostile] binary, cap, deep nesting, huge grid, pathological inline OK")


META_DOC = """\
:orphan:

.. meta::
   :description: Learn how to set up |product| in five minutes.
   :keywords: setup, onboarding
   :robots: noindex
   :description lang=fr: Deja traduit
   :http-equiv=Content-Type: text/html; charset=UTF-8

Getting started
===============

:Author: Jane Doe

Open the dashboard and click **New project**.

.. meta::
   :Keywords:
      wrapped, over
      two lines
   :description: 12345
"""


def test_meta_directive_733():
    """#733: `.. meta::` description / keywords values are units appended after every
    body unit (body indexes unchanged); other meta fields stay; written back on one
    line with rST escaping; no-op builds identical."""
    data = META_DOC.encode()
    us = F.parse(data).units
    body = [(u.p_index, u.text) for u in us if not u.style]
    plain = META_DOC.replace(":description: Learn", ":x-a: Learn").replace(
        ":keywords: setup", ":x-b: setup"
    )
    plain = plain.replace(":Keywords:", ":x-c:")
    assert body == [(u.p_index, u.text) for u in F.parse(plain.encode()).units], body
    n = len(body)
    meta = [(u.p_index, u.text, u.style) for u in us if u.style]
    assert meta == [
        (n, "Learn how to set up ⟦x1⟧ in five minutes.", "meta:description"),
        (n + 1, "setup, onboarding", "meta:keywords"),
        (n + 2, "wrapped, over two lines", "meta:keywords"),
    ], meta
    assert_identity("rst", data)
    assert F.build(data, {str(u.p_index): u.text for u in us}) == data
    out = F.build(
        data,
        {
            str(n): "- Apprenez *vite* ⟦x1⟧\nen cinq minutes",
            str(n + 1): "installation, prise en main",
            str(n + 2): "enveloppe, sur deux lignes",
        },
    ).decode()
    assert "   :description: \\- Apprenez \\*vite\\* |product| en cinq minutes\n" in out, out
    assert "   :keywords: installation, prise en main\n   :robots: noindex\n" in out, out
    assert "   :Keywords:\n      enveloppe, sur deux lignes\n   :description: 12345\n" in out
    for kept in (":description lang=fr: Deja traduit", ":http-equiv=Content-Type: text/html"):
        assert kept in out, kept
    assert len(F.parse(out.encode()).units) == len(us)
    print("  [meta] .. meta:: description / keywords appended, one line, others kept OK")


def main():
    test_meta_directive_733()
    test_blocks_and_identity()
    test_translated_round_trip()
    test_inline_escaping_and_paragraph_start()
    test_cjk_inline_boundaries()
    test_title_widths()
    test_grid_tables()
    test_simple_tables()
    test_hostile()
    print("RST FILTER OK")


if __name__ == "__main__":
    main()
