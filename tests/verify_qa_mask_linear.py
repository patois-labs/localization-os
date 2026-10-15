"""#588: qa's non-prose masking runs in linear time with the SAME matches as before.

The old single alternation (`qa._MASK_RE` before #588) had a quadratic email alternative
(about 2.8 s on a 20,000 character token, cubic on an "@@@@" run) and a quadratic markup-tag
alternative ("<a<a<a" with no ">"), while a segment may be 100,000 characters (#571) and QA
runs on save and approve. This suite pins:

  * the legacy pattern, kept here verbatim as the reference, and the new scanner produce
    IDENTICAL spans and identical blanked text on a corpus: every string literal in the
    existing QA / Jev / number suites, typical and tricky emails, tags, URLs, numbers,
    unicode, and a seeded random fuzz over a hostile alphabet
  * a 100,000 character hostile token costs under 0.1 s for every degenerate shape
    (best of three, so a busy box does not fake a red)

Run only through tests/test_verify_suites.py. ASCII-only output.
"""

import ast
import os
import random
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qamask_")

from app.services import qa

# The pre-#588 alternation, verbatim (the reference the new scanner must match), with the
# three scheme-less URL carriers #581 added after `www.` (protocol-relative, host + path,
# root-relative).
LEGACY = re.compile(
    "|".join(
        (
            r"https?://\S+",
            r"www\.[^\s<>]+",
            r"(?<![\w:./-])//[^\s<>]+",
            r"(?<![\w./-])(?:[A-Za-z][A-Za-z0-9-]{0,62}\.)+[A-Za-z]{2,}(?::\d{2,5})?(?:/\S*)?",
            r"(?<![\w./-])(?:/[A-Za-z0-9._~%+-]+){2,}",
            r"[^\s<>()\[\]]+@[^\s<>()\[\]]+\.[A-Za-z]{2,}",
            r"⟦/?[a-z]?\d+⟧",
            r"\$\{[^{}]*\}",
            r"\{[^{}]*\}",
            r"%(?:\d+\$)?[0-9.\-]*[a-zA-Z]",
            r"\$[A-Za-z_][A-Za-z0-9_]*",
            r"</?[a-zA-Z][^>]*>",
            r"&#?[A-Za-z0-9]{1,8};",
            r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)",
            r"(?<!\d)\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}(?!\d)",
            r"(?<!\d)\d{1,2}[:h]\d{2}(?::\d{2})?(?!\d)",
            r"(?<![\w.])[vV]\.?\s?\d+(?:\.\d+)+(?!\d)",
        )
    ),
    re.UNICODE,
)


def spans_old(s):
    return [m.span() for m in LEGACY.finditer(s)]


def spans_new(s):
    return [m.span() for m in qa._MASK_RE.finditer(s)]


def check(s):
    a, b = spans_old(s), spans_new(s)
    assert a == b, f"mask spans differ for {s!r}: old {a} new {b}"
    blank = lambda m: " " * len(m.group(0))  # noqa: E731
    assert LEGACY.sub(blank, s) == qa._MASK_RE.sub(blank, s), s
    keep = lambda m: "<" + m[0] + ">"  # noqa: E731
    assert LEGACY.sub(keep, s) == qa._MASK_RE.sub(keep, s), s


TYPICAL = [
    "",
    "Mail me at jane.doe@example.com today",
    "jane.doe+tag@sub.example.co.uk, bob@x.io; (carol@y.org) [dave@z.net] <erin@w.de>",
    "no email here, just words and 12 numbers 3.5 and 1,234.56",
    "a@b.c",
    "a@b.cc",
    "a@@b.cc",
    "@b.cc",
    "a@.cc",
    "a@b..cc",
    "a@b.c1",
    "a@b.cc9",
    "a@b.cc9d@e.ff",
    "x@y.com.",
    "x@y.com,more",
    "x@y.com1",
    "x@y.12",
    "x@y.c.d.ef",
    "first@second@third.org",
    "a.b@c.d.e.fgh and a@b",
    "name@domain.comMore",
    "user@host.com?subject=Hi&body=x",
    "http://user@host.com/path",
    "https://example.com/a?b=c&d=e#frag and www.example.com/x",
    "www.x.com,www.y.org",
    "üser@dömain.de und 日本@例.jp mail@例.com",
    "café@exemple.fr x@y.éé x@y.abé",
    "tabs\tand\nnewlines a@b.co\nc@d.ee",
    "nbsp a@b.co",
    '<b>bold</b> <a href="x@y.com">link</a> <br/> </p> <1> < a> <a',
    "<a<a<a<a>",
    "<//a> <a/> </ a> <i>",
    "{0} {name} ${VAR} $VAR %s %1$d %.2f &nbsp; &#8212; {unclosed {a} x}",
    "2026-03-07 3/7/2026 3.7.2026 18:30 18h30 18:30:05 v1.2.3 V 2.0 v1",
    "⟦r1⟧ ⟦/g2⟧ ⟦ ⟧ ⟦x⟧",
    "Version 10.0.26200 costs 1.234.567,89 EUR on 12:30 at a@b.cc",
    "{a b}user@example.com <x y>u@v.co",
    "{a b}foo@x.com%dbar@y.org",
    "a" * 50,
    "@" * 50,
    "a@" * 25,
    "a@b" * 25,
    "<a" * 25,
    "a@a." * 12,
    "http://" * 8,
]

# Every string literal in the existing QA, number and Jev suites (a real-fixture corpus).
FIXTURE_FILES = [
    "tests/verify_qa_7_8_10_authoring.py",
    "tests/verify_qe_jev.py",
    "tests/verify_qe_jev_loop.py",
    "tests/verify_number_meta_integrity.py",
]


def fixture_strings():
    out = []
    for rel in FIXTURE_FILES:
        path = os.path.join(_ROOT, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and 0 < len(node.value) <= 2000
            ):
                out.append(node.value)
    return out


ATOMS = [
    "a", "b", "Z", "1", "2", ".", "@", "@", "-", "_", " ", "<", ">", "(", ")", "[", "]",
    "/", "{", "}", "%", "$", "&", ";", "#", ":", "h", "v", "V", "http://", "https://",
    "www.", "⟦r1⟧", "⟦", "⟧", "\n", "é", "日", "com", "org",
    "x@y.co", "${", "<a", "</b", "<b>", "&amp;", "3.7.2026", "12:30", "1.2.3", "%s",
    "%1$d", " ", "'", ",",
]  # fmt: skip


REAL_FRAGMENTS = [
    "{count, plural, one {# file} other {# files}}",
    "{0}",
    "{name}",
    "{{handlebars}}",
    "${user.name}",
    "%s",
    "%d items",
    "%1$s of %2$s",
    "%.2f%%",
    "%-10s|",
    "%(name)s",
    "$HOME",
    "$1",
    "100%",
    "50 %",
    "<b>",
    "</b>",
    '<a href="https://x.com/a?b=1&c=2">',
    "</a>",
    "<br/>",
    "<br />",
    '<ph id="1"/>',
    "<x id='2'>text</x>",
    "<![CDATA[x < y]]>",
    "<!-- c -->",
    "a < b and c > d",
    "x<y",
    "1 < 2",
    "https://example.com/path?q=a@b.com",
    "http://user:pw@host.io:8080/x",
    "mailto:jane@example.com",
    "mailto:a.b+c@sub.example.co.uk?subject=Hi",
    "www.example.com",
    "www.example.com/a_b-c.",
    "jane.doe@example.com",
    "support@patoislabs.com.",
    "(jane@example.com)",
    "<jane@example.com>",
    "@handle",
    "user@localhost",
    "me@x.y",
    "a@b.cd9",
    "üser@dömain.de",
    "e@例.jp",
    "&nbsp;",
    "&amp;",
    "&#8212;",
    "&#x1F600;",
    "&bogus",
    "AT&T",
    "Q&A;",
    "2026-10-07",
    "07/10/2026",
    "7.10.2026",
    "07-10-26",
    "12:30",
    "9h05",
    "18:30:05",
    "1:05 PM",
    "12.30 Uhr",
    "v1.2.3",
    "V 2.0",
    "version 3.4.5.6",
    "1.234,56",
    "1,234.56",
    "3.14",
    "42",
    "2026",
    "100.000",
    "😀",
    "👍🏽",
    "❤️",
    "שלום",
    "مرحبا",
    "‏",
    "‫",
    "你好",
    "こんにちは",
    "한국어",
    "　",
    " ",
    "⟦r1⟧",
    "⟦/r1⟧",
    "⟦g12⟧",
    "⟦0⟧",
    "Hello",
    "world",
    "the",
    "of",
    " ",
    " ",
    " ",
    ", ",
    ". ",
    "; ",
    ": ",
    "!",
    "?",
    "\n",
    "\t",
    "-",
    "/",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    "<",
    ">",
    "@",
    ".",
    "com",
    "co.uk",
    "x@y.com",
    "a@b",
    ".com",
    "9",
]
FRAG_SEPS = ["", " ", " ", "", ", ", "\n"]


def realistic_fuzz(rnd, count):
    for _ in range(count):
        n = rnd.randint(1, 14)
        yield "".join(rnd.choice(REAL_FRAGMENTS) + rnd.choice(FRAG_SEPS) for _ in range(n))


def hostile(n):
    return {
        "letters": "a" * n,
        "ats": "@" * n,
        "a-at pairs": "a@" * (n // 2),
        "at-dot runs": "a@a." * (n // 4),
        "dots": "a." * (n // 2),
        "unclosed tags": "<a" * (n // 2),
        "unclosed braces": "{a" * (n // 2),
        "digits": "1" * n,
        "grouped digits": "1" + " 111" * (n // 4) + "1a",
        "urls": "http://" * (n // 7),
        "email then junk": "x@y.com" + "9" * n,
        "tag then email run": "{a b}" + "x@y." * (n // 4),
        "percent runs": "%1" * (n // 2),
        "ampersands": "&a" * (n // 2),
        "spaces": "1" + " " * n + "x",
    }


def main():
    # --- 1. identical matches on the corpus -----------------------------------------
    corpus = list(TYPICAL)
    fx = fixture_strings()
    corpus += fx
    for s in corpus:
        check(s)
    print(f"  identical matches on {len(TYPICAL)} hand cases + {len(fx)} existing fixture strings")

    rnd = random.Random(588)
    cases = 0
    for _ in range(40000):
        s = "".join(rnd.choice(ATOMS) for _ in range(rnd.randint(0, 40)))
        check(s)
        cases += 1
    print(f"  identical matches on {cases} seeded random strings over a hostile alphabet")

    rcases = 0
    for s in realistic_fuzz(random.Random(5882), 60000):
        check(s)
        rcases += 1
    print(
        f"  identical matches on {rcases} realistic localization strings"
        " (ICU, printf, tags, URLs, emails, RTL, CJK, emoji)"
    )

    # Old and new agree on a mid-size hostile token too (the old one is still quick at 400).
    for s in hostile(400).values():
        check(s)
    print("  identical matches on the degenerate shapes at 400 characters")

    # --- 2. linear time: 100,000 characters, under 0.1 s each ------------------------
    for name, s in hostile(100_000).items():
        best = 9.0
        for _ in range(3):
            t0 = time.perf_counter()
            qa.mask_non_prose(s)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.1, f"mask_non_prose on a 100k {name!r} token took {best:.3f}s (want < 0.1)"
    print("  100,000 char hostile tokens: all 15 shapes mask in under 0.1 s")

    # The reported case: a 20,000 char token used to take about 2.8 s.
    t0 = time.perf_counter()
    qa.mask_non_prose("a" * 20_000)
    assert time.perf_counter() - t0 < 0.1
    # and the number check built on it stays linear end to end
    t0 = time.perf_counter()
    qa.number_findings("x@" * 50_000 + "1", "y@" * 50_000 + "1", "en-US", "de-DE")
    assert time.perf_counter() - t0 < 1.0
    print("  number_findings on a 100k hostile segment stays fast")
    print("verify_qa_mask_linear OK")


if __name__ == "__main__":
    main()
