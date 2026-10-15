"""LaTeX document filter (.tex, .latex, .ltx).

Spec checked: the LaTeX2e unofficial reference manual (https://latexref.xyz/: special
characters, sectioning, environments, verbatim, math, \\input/\\include) and "LaTeX2e
for authors" (https://www.latex-project.org/help/documentation/usrguide.pdf). Fixtures
are hand-written.

  identity   empty build and echo build are byte-identical (LF, CRLF, BOM)
  body       paragraphs, sections (+ short title), captions, \\item labels, footnotes,
             tabular cells; preamble only \\title
  skipped    comments, inline and display math, equation/align, verbatim, lstlisting,
             \\verb, \\label/\\ref/\\cite keys, \\url with %, \\input/\\include not followed
  escaping   \\& \\% \\$ \\# \\_ \\{ \\} read as plain characters, ~ as no-break space;
             every LaTeX special character in a translation escaped on build
  config     LATEX_TEXT_COMMANDS adds an inline text command
  encoding   Windows-1252 converted, inputenc latin1 rewritten to utf8
  hostile    binary bytes, segment cap, unbalanced braces, deep nesting

Hermetic + offline. Final line: "LATEX FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_latex_")

from app.filters import get_filter
from app.filters.base import UnsupportedEncodingError
from tests._kvres_common import assert_identity, assert_segment_cap

F = get_filter("tex")


def units(data: bytes) -> list[str]:
    return [u.text for u in F.parse(data).units]


DOC = r"""\documentclass{article}
\usepackage[utf8]{inputenc}
% preamble comment
\newcommand{\pkg}[1]{\texttt{#1}}
\title{A Short \emph{Guide}}
\author{Jane Doe}
\begin{document}
\maketitle

\section[Intro]{Introduction}\label{sec:intro}
This is the first paragraph with \emph{emphasis}, a reference to
Section~\ref{sec:intro} and a citation \cite[p.~3]{knuth}. Costs are 10\% of R\&D.
Math stays $E = mc^2$ inline. % a trailing comment

\subsection*{Details}
\begin{itemize}
  \item First item with \textbf{bold}.
  \item[Label] Second item\footnote{A footnote text.}.
\end{itemize}

\begin{equation}
  a^2 + b^2 = c^2 \text{ never translated}
\end{equation}
\[ x = y \]

\begin{verbatim}
Do not translate this.
\end{verbatim}
\begin{lstlisting}[language=Python]
print("no")
\end{lstlisting}

\begin{figure}[htbp]
  \centering
  \includegraphics[width=0.5\textwidth]{img.png}
  \caption{A nice picture.}
  \label{fig:pic}
\end{figure}

\begin{tabular}{|l|r|}
  \hline
  Name & Value \\
  Apple & Red fruit \\ \hline
\end{tabular}

See \href{https://example.com}{the website} and \url{https://x.org/a%20b}.
Use \verb|code_here| in text.
\input{chapter1}
\end{document}
Trailing text after the document.
"""


def test_units_and_identity():
    data = DOC.encode()
    got = units(data)
    assert got == [
        "A Short ⟦g1⟧Guide⟦/g1⟧",
        "Intro",
        "Introduction",
        "This is the first paragraph with ⟦g1⟧emphasis⟦/g1⟧, a reference to Section\u00a0⟦x2⟧"
        " and a citation ⟦x3⟧. Costs are 10% of R&D. Math stays ⟦x4⟧ inline.",
        "Details",
        "First item with ⟦g1⟧bold⟦/g1⟧.",
        "Label",
        "Second item⟦g1⟧A footnote text.⟦/g1⟧.",
        "A nice picture.",
        "Name",
        "Value",
        "Apple",
        "Red fruit",
        "See ⟦g1⟧the website⟦/g1⟧ and ⟦x2⟧. Use ⟦x3⟧ in text.",
    ], got
    for body in (data, data.replace(b"\n", b"\r\n"), b"\xef\xbb\xbf" + data):
        assert_identity("tex", body)
    print(
        "  [body] paragraphs, sections, captions, items, cells; math/verbatim/preamble skipped OK"
    )


def test_translated_round_trip():
    data = DOC.encode()
    src = units(data)
    tr = {str(i): "DE " + t for i, t in enumerate(src)}
    out = F.build(data, tr).decode()
    assert r"\title{DE A Short \emph{Guide}}" in out, out
    assert r"\section[DE Intro]{DE Introduction}\label{sec:intro}" in out, out
    assert (
        r"DE This is the first paragraph with \emph{emphasis}, a reference to "
        r"Section~\ref{sec:intro}"
        r" and a citation \cite[p.~3]{knuth}. Costs are 10\% of R\&D. Math stays $E = mc^2$ inline."
        " % a trailing comment\n" in out
    ), out
    assert r"\item[DE Label] DE Second item\footnote{A footnote text.}." in out, out
    assert "  DE Name & DE Value \\\\\n  DE Apple & DE Red fruit \\\\ \\hline\n" in out, out
    assert r"\text{ never translated}" in out and "Do not translate this." in out
    assert r"DE See \href{https://example.com}{the website} and \url{https://x.org/a%20b}." in out
    assert "\\input{chapter1}\n\\end{document}\nTrailing text after the document.\n" in out
    assert units(out.encode()) == [tr[str(i)] for i in range(len(src))]
    print("  [round trip] translations spliced in; commands, keys and math unchanged OK")


def test_escaping():
    data = b"\\begin{document}\nPlain text here.\n\\end{document}\n"
    out = F.build(data, {"0": "a & b % c $ d # e _ f { g } h ~ i ^ j \\ k\u00a0l"})
    assert out == (
        b"\\begin{document}\n"
        b"a \\& b \\% c \\$ d \\# e \\_ f \\{ g \\} h \\textasciitilde{} i \\textasciicircum{} j"
        b" \\textbackslash{} k~l\n\\end{document}\n"
    ), out
    out = F.build(data, {"0": "zwei\n\nAbsätze"})
    assert out == "\\begin{document}\nzwei Absätze\n\\end{document}\n".encode(), out
    # a translation that drops or invents tokens keeps the markup balanced
    data = b"\\begin{document}\nA \\textbf{bold} word.\n\\end{document}\n"
    out = F.build(data, {"0": "Ein ⟦g1⟧fettes⟦/g1⟧ Wort ⟦x7⟧."})
    assert b"Ein \\textbf{fettes} Wort ." in out, out
    # gate: a dropped, missing or out-of-order close never unbalances the braces
    data = b"\\begin{document}\nA \\textbf{bold \\emph{very} word} end.\n\\end{document}\n"
    assert units(data) == ["A ⟦g1⟧bold ⟦g2⟧very⟦/g2⟧ word⟦/g1⟧ end."], units(data)
    for tr, want in (
        ("Ein ⟦g1⟧fett Ende.", b"Ein \\textbf{fett Ende.}"),
        ("Ein fett⟦/g1⟧ Ende.", b"Ein fett Ende."),
        ("⟦g1⟧a ⟦g2⟧b⟦/g1⟧ c⟦/g2⟧ d", b"\\textbf{a \\emph{b}} c d"),
    ):
        out = F.build(data, {"0": tr})
        assert want in out, (tr, out)
        body = out.split(b"\\begin{document}")[1]
        assert body.count(b"{") == body.count(b"}"), out
    print("  [escaping] specials escaped, no-break space as ~, line breaks folded OK")


def test_config_and_encoding():
    data = b"\\begin{document}\nA \\keyword{term} here.\n\\end{document}\n"
    assert units(data) == ["A ⟦x1⟧ here."]
    os.environ["LATEX_TEXT_COMMANDS"] = "keyword"
    try:
        assert units(data) == ["A ⟦g1⟧term⟦/g1⟧ here."]
    finally:
        del os.environ["LATEX_TEXT_COMMANDS"]
    latin = (
        "\\usepackage[latin1]{inputenc}\n"
        "\\begin{document}\nCaf\u00e9 cr\u00e8me.\n\\end{document}\n"
    )
    data = latin.encode("cp1252")
    assert units(data) == ["Caf\u00e9 cr\u00e8me."]
    out = F.build(data, {"0": "Kaffee \u2013 Sahne"}).decode("utf-8")
    assert out.startswith("\\usepackage[utf8]{inputenc}\n"), out
    assert "Kaffee \u2013 Sahne" in out
    print("  [config/encoding] extra text command; cp1252 exported as UTF-8 with utf8 inputenc OK")


def test_hostile():
    try:
        F.parse(b"\x89PNG\r\n\x1a\n\x00\x00\x81\x8d\x8f\x90binary")
    except UnsupportedEncodingError:
        pass
    else:
        raise AssertionError("binary must be refused")
    assert_segment_cap("tex", lambda i: b"Paragraph %d.\n\n" % i)
    for bad in (
        b"\\begin{document}\nOpen { brace and \\emph{unclosed\n\nNext para }}} here.\n",
        b"\\begin{verbatim}\nnever closed",
        b"$ unclosed math\n\nText \\[ unclosed display",
        b"\\section{" + b"{" * 5000 + b"deep" + b"}" * 5000 + b"}\n",
        b"\\verb",
        b"\\",
        b"%" * 10000,
        b"\\begin{tabular}{" * 2000,
        b"\\x{a " * 40000,  # unclosed groups in one paragraph: bounded scans, not quadratic
        b"$a " * 60000,
        b"\\item[ " * 40000,
    ):
        r = F.parse(bad)
        assert F.build(bad, {}) == bad
        F.build(bad, {str(u.p_index): "X " + u.text for u in r.units})
    print("  [hostile] binary, cap, unbalanced and unclosed input, deep nesting OK")


def test_control_word_glued_to_letters():
    r"""#696: a point token ending in a control word (\TeX, \LaTeX, \ldots) glued to
    letters by a Japanese/Chinese translation was written as `\TeXを`, one undefined
    control sequence under XeLaTeX/LuaLaTeX/upLaTeX. The build now ends it with `{}`;
    punctuation, spaces, digits, a `{}` or `\ ` already there need nothing."""
    data = b"\\begin{document}\nWrite with \\LaTeX\\ and plain \\TeX. See \\S 2.\n\\end{document}\n"
    assert units(data) == ["Write with ⟦x1⟧⟦x2⟧and plain ⟦x3⟧. See ⟦x4⟧2."], units(data)
    ja = "⟦x1⟧⟦x2⟧とプレーンな⟦x3⟧を使う。⟦x4⟧2を参照。"
    out = F.build(data, {"0": ja}).decode()
    assert r"\LaTeX\ とプレーンな\TeX{}を使う。" in out, out
    assert r"\S 2" in out, out  # the token's own trailing space already separates it
    # an ASCII letter too (`\TeXs`), and never a second group or a group before punctuation
    out = F.build(data, {"0": "Mit ⟦x1⟧⟦x2⟧und ⟦x3⟧s. ⟦x4⟧2."}).decode()
    assert r"Mit \LaTeX\ und \TeX{}s. \S 2." in out, out
    out = F.build(data, {"0": "Mit ⟦x1⟧⟦x2⟧und ⟦x3⟧. ⟦x4⟧2."}).decode()
    assert r"und \TeX. " in out, out
    print("  [control words] a control word glued to letters ends with {} (#696) OK")


def test_control_word_before_blank():
    r"""#742: TeX skips every blank after a control word. A source with the control word
    at the end (`Installing \LaTeX`, `with \LaTeX.`) gives a token without a trailing
    blank; a translation that moves it before a word (German verb-final order) was written
    `mit \LaTeX setzt`, which renders "LaTeXsetzt". The build now writes `\LaTeX{} setzt`.
    A token that already carried its blank, a control symbol and punctuation need nothing."""
    data = (
        b"\\begin{document}\n\\section{Installing \\LaTeX}\n"
        b"This chapter explains how to typeset with \\LaTeX.\n\n"
        b"Last updated \\today\n\nSee \\S 2 and plain \\TeX.\n\\end{document}\n"
    )
    us = units(data)
    assert us == [
        "Installing ⟦x1⟧",
        "This chapter explains how to typeset with ⟦x1⟧.",
        "Last updated ⟦x1⟧",
        "See ⟦x1⟧2 and plain ⟦x2⟧.",
    ], us
    de = {
        "0": "⟦x1⟧ installieren",
        "1": "Dieses Kapitel erklärt, wie man mit ⟦x1⟧ setzt.",
        "2": "Zuletzt aktualisiert\t⟦x1⟧",
        "3": "Siehe ⟦x1⟧2 und reines ⟦x2⟧ (mit ⟦x2⟧\tTab).",
    }
    out = F.build(data, de).decode()
    assert r"\section{\LaTeX{} installieren}" in out, out
    assert r"mit \LaTeX{} setzt." in out, out
    assert "Zuletzt aktualisiert\t\\today\n" in out, out  # nothing follows: no group
    assert r"Siehe \S 2 und reines \TeX{} (mit \TeX{}" + "\tTab)." in out, out
    # the export re-parses with the words where the translation put them
    again = units(out.encode())
    assert again[1] == "Dieses Kapitel erklärt, wie man mit ⟦x1⟧ setzt.", again
    # punctuation, end of unit and a no-op build are unchanged
    out = F.build(data, {"1": "Mit ⟦x1⟧."}).decode()
    assert "Mit \\LaTeX.\n" in out, out
    assert F.build(data, {}) == data
    print("  [control words] a blank after a control word survives TeX (#742) OK")


def main():
    test_units_and_identity()
    test_translated_round_trip()
    test_escaping()
    test_control_word_glued_to_letters()
    test_control_word_before_blank()
    test_config_and_encoding()
    test_hostile()
    print("LATEX FILTER OK")


if __name__ == "__main__":
    main()
