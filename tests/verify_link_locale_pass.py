"""A link whose only change is its language/locale part passes QA; any other link change is held.

2026-10-13 (after #719 made QA hold every changed link address):
  PASS   /en/help -> /fr/help, /en-us/ -> /fr-fr/, en.example.com -> fr.example.com,
         ?lang=en -> ?lang=fr (HTML href, Markdown destination, bare URL on the live badge)
  HELD   another path, host, query key or value, fragment, another language than the target's,
         a dropped link, a source side that is not the source locale's code

Covers quality.placeholder_violation (write seam, editor save, Jev structural), the editor's
live "tags" badge (qa.check_segment) and the unit rules in services/link_locale.py.
Hermetic. ASCII-only. Final line "LINK LOCALE PASS OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_linkloc_")

from app.services import qa, quality
from app.services import quality_part4 as L


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _ph(src, tgt, sl="en-US", tl="fr-FR"):
    return quality.placeholder_violation(src, tgt, source_locale=sl, target_locale=tl)


PASS = [
    ("/en/help", "/fr/help"),
    ("/en-us/help", "/fr-fr/help"),
    ("/en_US/help", "/fr_FR/help"),
    ("https://en.example.com/a/b", "https://fr.example.com/a/b"),
    ("//en.example.com/a", "//fr.example.com/a"),
    ("en.example.com", "fr.example.com"),
    ("https://example.com/p?lang=en", "https://example.com/p?lang=fr"),
    ("https://example.com/p?x=1&hl=en-US", "https://example.com/p?x=1&hl=fr-FR"),
    ("https://example.com/en/a/?lang=en", "https://example.com/fr/a/?lang=fr"),
]
HELD = [
    ("/en/help", "/fr/aide"),  # the path changed
    ("/en/help", "/de/help"),  # not the target language
    ("/en/help", "/help"),  # language part dropped
    ("/en/help", "/en/help/"),
    ("https://a.com/en/x", "https://b.com/fr/x"),  # the domain changed
    ("https://a.com/en/x", "http://a.com/fr/x"),  # the scheme changed
    ("https://a.com/p?lang=en&id=1", "https://a.com/p?lang=fr&id=2"),
    ("https://a.com/p?page=en", "https://a.com/p?page=fr"),  # not a language query key
    ("/en/help#top", "/fr/help#bottom"),
    ("/de/help", "/fr/help"),  # the source side is not the source locale (en)
    ("/faq/x", "/fr/x"),  # "faq" is not a language code
]


def test_units():
    for a, b in PASS:
        if not L.url_locale_only(a, b, "fr-FR", "en-US"):
            _fail(f"should pass: {a} -> {b}")
    for a, b in HELD:
        if L.url_locale_only(a, b, "fr-FR", "en-US"):
            _fail(f"should be held: {a} -> {b}")
    if L.url_locale_only("/en/help", "/en/help", "fr", "en"):
        _fail("an unchanged link is not a locale change")
    if L.url_locale_only("/en/help", "/fr/help", None, "en"):
        _fail("with no target locale nothing is excused")
    if not L.url_locale_only("/de/help", "/fr/help", "fr", None):
        _fail("an unknown source locale accepts any language code")
    print("  units OK")


def _a(href):
    return '<a href="' + href + '">x</a>'


def test_html():
    for a, b in PASS:
        r = _ph(_a(a), _a(b))
        if r:
            _fail(f"html href should pass: {a} -> {b}: {r}")
    for a, b in HELD:
        if not _ph(_a(a), _a(b)):
            _fail(f"html href should be held: {a} -> {b}")
    if not _ph('<a href="/en/help">Help</a>', "<a>Aide</a>"):
        _fail("a dropped href is held")
    # two links, one localized and one changed: the changed one is still named
    r = _ph(
        '<a href="/en/a">A</a> <a href="/x">B</a>',
        '<a href="/fr/a">A</a> <a href="/y">B</a>',
    )
    if "/y" not in r or "/fr/a" in r:
        _fail(f"only the really changed link is named: {r}")
    # without a locale pair the old (strict) behaviour stands
    if not quality.placeholder_violation('<a href="/en/help">H</a>', '<a href="/fr/help">A</a>'):
        _fail("no locale pair given: the changed link is held as before")
    print("  html href OK")


def test_markdown():
    for a, b in PASS:
        if _ph(f"See [help]({a}) now.", f"Voir [aide]({b}) ici."):
            _fail(f"markdown destination should pass: {a} -> {b}")
    for a, b in HELD:
        if not _ph(f"See [help]({a}) now.", f"Voir [aide]({b}) ici."):
            _fail(f"markdown destination should be held: {a} -> {b}")
    if not _ph("See [help](/en/help).", "Voir aide."):
        _fail("a dropped markdown link is held")
    print("  markdown OK")


def test_live_badge():
    def tags(src, tgt):
        return [f for f in qa.check_segment(src, tgt, "fr-FR") if f["type"] == "tags"]

    if tags('<a href="/en/help">Help</a>', '<a href="/fr/help">Aide</a>'):
        _fail("live badge: localized href must pass")
    if tags("Visit https://en.example.com/x now", "Visitez https://fr.example.com/x"):
        _fail("live badge: localized bare URL must pass")
    if not tags('<a href="/en/help">Help</a>', '<a href="/fr/aide">Aide</a>'):
        _fail("live badge: a changed href is still flagged")
    if not tags("Visit https://a.com/x now", "Visitez https://b.com/x"):
        _fail("live badge: a changed bare URL is still flagged")
    print("  live badge OK")


def test_linear():
    big = "/en/" + "a/" * 20000
    t0 = time.perf_counter()
    L.url_locale_only(big, big.replace("/en/", "/fr/"), "fr", "en")
    src = " ".join(f'<a href="/en/p{i}">x</a>' for i in range(3000))
    tgt = " ".join(f'<a href="/fr/p{i}">x</a>' for i in range(3000))
    _ph(src, tgt)
    if time.perf_counter() - t0 > 5:
        _fail("hostile inputs must stay fast")
    print("  linear OK")


def main():
    test_units()
    test_html()
    test_markdown()
    test_live_badge()
    test_linear()
    print("LINK LOCALE PASS OK")


if __name__ == "__main__":
    main()
