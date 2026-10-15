"""Windows .resjson document filter (file-format Batch 1).

Spec checked: Microsoft "Quickstart: Translating UI resources (HTML)" (Windows 8.x archive):
a JSON object of id -> string with `_id.comment` translator notes. Fixtures are hand-written.

  grammar    `_key.comment` (and any `_key.attr`) metadata is never translated and becomes
             the record comment; `//` and `/* */` comments, key order, spacing and the
             BOM survive; nested objects are walked with a path context
  escapes    JSON escapes decode once; a translation is JSON-escaped once; untouched
             strings keep their original `\\u` spelling
  hostile    unterminated string/comment, trailing junk, bad literal, deep nesting, cap
  records()  key / value / comment / unit_index

Hermetic + offline. Final line: "RESJSON FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_resjson_")

from app.filters import get_filter
from app.filters.base import InputLimitError
from tests._kvres_common import (
    assert_identity,
    assert_placeholder_dialect,
    assert_refused,
    assert_round_trip,
    assert_segment_cap,
    units,
)

SRC = (
    b"\xef\xbb\xbf{\r\n"
    b"    // Shown on the start page\r\n"
    b'    "greeting"              : "Hello",\r\n'
    b'    "_greeting.comment"     : "A welcome greeting.",\r\n'
    b"\r\n"
    b'    "farewell"              : "Goodbye",\r\n'
    b'    "_farewell.comment"     : "A goodbye.",\r\n'
    b'    "quote"                 : "Say \\"cheese\\" \\u00e9",\r\n'
    b'    "count"                 : "{0} new messages",\r\n'
    b'    "id"                    : "{0}",\r\n'
    b'    "_count.maxLength"      : 40,\r\n'
    b"    /* grouped strings */\r\n"
    b'    "menu"                  : { "open": "Open file", "flag": true }\r\n'
    b"}\r\n"
)


def test_units_and_identity():
    assert units("resjson", SRC) == [
        "Hello",
        "Goodbye",
        'Say "cheese" \u00e9',
        "{0} new messages",
        "Open file",
    ], units("resjson", SRC)
    assert_identity("resjson", SRC)
    print(
        "  [grammar] comments, _key.comment metadata, nested object, BOM; echo is byte-identical OK"
    )


def test_round_trip():
    mapping = {"0": "Hallo", "2": 'Sag "K\u00e4se"\n', "4": "Datei \u00f6ffnen"}
    expected = (
        SRC.replace(b'"Hello"', b'"Hallo"')
        .replace(b'"Say \\"cheese\\" \\u00e9"', '"Sag \\"K\u00e4se\\"\\n"'.encode())
        .replace(b'"Open file"', '"Datei \u00f6ffnen"'.encode())
    )
    assert_round_trip("resjson", SRC, mapping, expected)
    print("  [round trip] translations JSON-escaped once, comments and layout kept OK")


def test_hostile_inputs():
    assert_refused("resjson", b'{"a": "never closed}', ValueError, "never closed")
    assert_refused("resjson", b'{"a": "b" /* open', ValueError, "never closed")
    assert_refused("resjson", b'{"a": "b"} {"c": "d"}', ValueError, "after the top-level")
    assert_refused("resjson", b'{"a": nope}', ValueError, "expected a value")
    assert_refused("resjson", b'{"a" "b"}', ValueError, "expected ':'")
    assert_refused("resjson", b'["a"]', ValueError, "one JSON object")
    assert_refused("resjson", b'{"a": "bad \\x escape"}', ValueError, "escape")
    assert_refused("resjson", b'{"a":' + b"[" * 200 + b"]" * 200 + b"}", InputLimitError, "nested")
    assert_refused("resjson", b'{"a": "caf\xe9"}', ValueError, "UTF-8")
    assert_segment_cap(
        "resjson",
        lambda i: (
            (b"{" if i == 0 else b"")
            + b'"k%d": "Value number %d"' % (i, i)
            + (b"}" if i == 29 else b",")
        ),
    )
    print(
        "  [hostile] unterminated, trailing junk, bad literal/escape, deep nesting, cap refused OK"
    )


def test_records_and_placeholders():
    recs = get_filter("resjson").records(SRC)
    by = {(r.context, r.key): r for r in recs}
    assert by[("", "greeting")].comment == "A welcome greeting."
    assert by[("", "farewell")].comment == "A goodbye."
    assert by[("", "_greeting.comment")].unit_index is None
    assert by[("", "id")].unit_index is None
    assert by[("menu", "open")].unit_index == 4 and by[("menu", "open")].value == "Open file"
    assert_placeholder_dialect("{0} new messages", "{0} neue Nachrichten", "neue Nachrichten")
    print("  [records] comments joined from _key.comment, nested path context; {0} gated OK")


def main():
    test_units_and_identity()
    test_round_trip()
    test_hostile_inputs()
    test_records_and_placeholders()
    print("RESJSON FILTER OK")


if __name__ == "__main__":
    main()
