"""Document-upload filter encoding + XML hardening (Part-2 QA P-B2/P-B3/P-B4).

The document-upload filters are fed OPERATOR-supplied bytes, exactly as untrusted as
a vendor return, yet three of them drifted from the strict posture the keys-import and
vendor-XLIFF paths already hold:

  P-B2  the JSON filter decoded with `.decode("utf-8", "replace")`, so a latin-1 `café`
        was ACCEPTED and stored as `caf<U+FFFD>` -- permanent, silent corruption. It must
        REFUSE a non-UTF-8 file honestly (a ValueError the upload route turns into a
        polite "parse" refusal), never store replacement-char mojibake. The keys-import
        path (services/key_files.parse_json_keys) already rejects the same bytes.
  P-B3  the JSON filter decoded with plain `utf-8`, so a UTF-8 BOM (common from Windows
        editors) was refused. It must decode with `utf-8-sig`, stripping a leading BOM so
        the file parses -- composed with P-B2 (utf-8-sig STRICT, no "replace").
  P-B4  the XLIFF filter parsed with a bare `etree.fromstring`, inconsistent with the
        fully-hardened services/vendor_xliff. It must reuse the same hardened posture
        (DOCTYPE refusal + resolve_entities=False / no_network / load_dtd=False /
        no huge_tree) so a DOCTYPE / XXE-bearing .xlf upload is refused.

Hermetic + offline. Final line: "DOC FILTER ENCODING OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_docfilter_")

from app.filters import get_filter


# ============================================================================
# P-B3 -- a UTF-8 BOM is accepted (utf-8-sig)
# ============================================================================
def test_json_bom_is_accepted():
    filt = get_filter("json")
    body = b'{"greeting": "Hello world", "id": "42"}'
    bom = b"\xef\xbb\xbf" + body
    parsed = filt.parse(bom)
    texts = [u["text"] for u in parsed.skeleton["units"]]
    assert "Hello world" in texts, (
        "P-B3: a UTF-8-with-BOM JSON must parse (utf-8-sig strips the BOM)",
        texts,
    )
    # and a build round-trip over the BOM'd original still works (build re-decodes it).
    out = filt.build(bom, {"0": "Hallo Welt"})
    assert b"Hallo Welt" in out, out
    print("  [P-B3] a UTF-8-with-BOM JSON uploads, parses and rebuilds OK")


# ============================================================================
# P-B2 -- a non-UTF-8 file is refused honestly, never mojibaked
# ============================================================================
def test_json_non_utf8_is_refused_not_mojibaked():
    filt = get_filter("json")
    # 'café' encoded latin-1: the 0xe9 byte is not valid UTF-8. The OLD
    # .decode("utf-8","replace") turned it into U+FFFD and stored "caf<U+FFFD>".
    raw = '{"greeting": "café"}'.encode("latin-1")
    assert "�" in raw.decode("utf-8", "replace"), (
        "fixture must be genuinely non-UTF-8 (the mojibake the fix prevents)"
    )
    refused = False
    try:
        filt.parse(raw)
    except ValueError:
        refused = True  # UnicodeDecodeError/ValueError -> upload route answers "parse"
    assert refused, "P-B2: a non-UTF-8 JSON must be REFUSED, never decoded with U+FFFD mojibake"
    # build() is strict too: a corrupt stored original is refused, not silently mangled.
    build_refused = False
    try:
        filt.build(raw, {"0": "x"})
    except ValueError:
        build_refused = True
    assert build_refused, "P-B2: build() must also refuse non-UTF-8, never mojibake"
    print("  [P-B2] a non-UTF-8 JSON is refused honestly, never stored as mojibake OK")


# ============================================================================
# P-B4 -- the XLIFF filter is hardened like services/vendor_xliff
# ============================================================================
def test_xliff_doctype_xxe_is_refused():
    filt = get_filter("xliff")
    xxe = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>\n'
        b'<xliff version="1.2"><file source-language="en" target-language="de">'
        b'<body><trans-unit id="1"><source>Hello &xxe;</source></trans-unit>'
        b"</body></file></xliff>"
    )
    refused = False
    try:
        filt.parse(xxe)
    except ValueError:
        refused = True  # vendor_xliff.XliffParseError is a ValueError
    assert refused, "P-B4: a DOCTYPE/XXE-bearing .xlf must be refused by the hardened parser"
    print("  [P-B4] a DOCTYPE/XXE-bearing .xlf document is refused OK")


def test_xliff_hardening_refuses_independently_of_the_doctype_prescan():
    """F4: the DOCTYPE-bearing payload above is refused by the ASCII byte prescan
    (_reject_doctype) BEFORE the parser flags matter, so it does not on its own prove the
    hardened parser. These two payloads carry NO ASCII `<!DOCTYPE`, so the prescan is a
    no-op and the refusal must come from the parser itself:

      (a) a DTD-less `&custom;` entity -- the hardened parser refuses it (an undefined /
          non-predefined entity is not resolved), surfaced as a clean XliffParseError
          rather than a raw lxml XMLSyntaxError.
      (b) a UTF-16-encoded DOCTYPE -- invisible to the ASCII prescan, caught by the
          charset-independent post-parse authority (_assert_no_dtd)."""
    from app.services.vendor_xliff import XliffParseError

    filt = get_filter("xliff")

    # (a) DTD-less custom entity, no `<!DOCTYPE` bytes at all -> parser refuses cleanly.
    ent = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<xliff version="1.2"><file source-language="en" target-language="de">'
        b'<body><trans-unit id="1"><source>Hello &custom;</source></trans-unit>'
        b"</body></file></xliff>"
    )
    assert b"<!DOCTYPE" not in ent, "fixture must carry no ASCII DOCTYPE (prescan no-op)"
    refused_a = False
    try:
        filt.parse(ent)
    except XliffParseError:
        refused_a = True  # a CLEAN refusal, not a raw XMLSyntaxError
    assert refused_a, (
        "F4: a DTD-less entity must be refused by the hardened parser as a clean "
        "XliffParseError, independent of the DOCTYPE prescan"
    )

    # (b) UTF-16 DOCTYPE: the ASCII prescan cannot see it; _assert_no_dtd must.
    doc16 = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE xliff [ <!ENTITY x "y"> ]>'
        '<xliff version="1.2"><file source-language="en" target-language="de">'
        '<body><trans-unit id="1"><source>Hi</source></trans-unit>'
        "</body></file></xliff>"
    ).encode("utf-16")
    assert b"<!DOCTYPE" not in doc16, "utf-16 fixture must be invisible to the ASCII prescan"
    refused_b = False
    try:
        filt.parse(doc16)
    except XliffParseError:
        refused_b = True
    assert refused_b, (
        "F4: a UTF-16 DOCTYPE must be caught by the charset-independent _assert_no_dtd"
    )
    print(
        "  [F4] the hardened .xlf parser refuses independently of the ASCII DOCTYPE "
        "prescan (DTD-less entity + UTF-16 DOCTYPE) OK"
    )


def test_xliff_plain_still_parses_after_hardening():
    filt = get_filter("xliff")
    ok = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<xliff version="1.2"><file source-language="en" target-language="de">'
        b'<body><trans-unit id="1"><source>Hello world</source></trans-unit>'
        b"</body></file></xliff>"
    )
    parsed = filt.parse(ok)
    texts = [u["text"] for u in parsed.skeleton["units"]]
    assert texts == ["Hello world"], ("P-B4: a clean .xlf must still parse after hardening", texts)
    out = filt.build(ok, {"0": "Hallo Welt"})
    assert b"Hallo Welt" in out, out
    print("  [P-B4] a clean .xlf still parses and rebuilds after hardening OK")


def main():
    test_json_bom_is_accepted()
    test_json_non_utf8_is_refused_not_mojibaked()
    test_xliff_doctype_xxe_is_refused()
    test_xliff_hardening_refuses_independently_of_the_doctype_prescan()
    test_xliff_plain_still_parses_after_hardening()
    print("DOC FILTER ENCODING OK")


if __name__ == "__main__":
    main()
