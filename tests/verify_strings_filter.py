"""Apple .strings document filter (file-format Batch 1).

Spec checked: Apple "String Resources" (Resource Programming Guide) and the old-style
property-list string syntax. Fixtures are hand-written.

  grammar    `"key" = "value";`, `/* */` and `//` comments, `\\"` `\\n` `\\\\` `\\U` escapes,
             unquoted words, a key identical to its value (never translated), `{ }` wrapper
  encodings  UTF-16 LE with BOM rebuilt as UTF-16 LE with BOM; UTF-8 stays UTF-8; BOM-less
             UTF-16 sniffed and kept; CRLF and the trailing newline survive
  placeholders  %@ and %1$@ are seen by the shared placeholder gate (dropped one flags)
  hostile    unterminated string/comment, binary plist, Windows-1252 bytes, stray text, cap
  records()  key / value / comment

Hermetic + offline. Final line: "STRINGS FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_strings_")

from app.filters import get_filter
from app.filters.base import InputLimitError, UnsupportedEncodingError
from tests._kvres_common import (
    assert_identity,
    assert_placeholder_dialect,
    assert_refused,
    assert_round_trip,
    assert_segment_cap,
    units,
)

SRC = (
    "/* Title of the main window */\r\n"
    '"Welcome" = "Welcome";\r\n'
    "// Button that saves\r\n"
    '"save.button" = "Save \\"all\\" files";\r\n'
    '"multi" = "Line one\\nLine two";\r\n'
    '"greeting" = "Hello %@, you have %1$@ items";\r\n'
    '"pct" = "%@";\r\n'
    '"unicode" = "Caf\\U00e9";\r\n'
    "plainKey = plainValue;\r\n"
    '"backslash" = "C:\\\\Temp";\r\n'
)
SRC_UNITS = [
    "Welcome",
    'Save "all" files',
    "Line one\nLine two",
    "Hello %@, you have %1$@ items",
    "Caf\u00e9",
    "plainValue",
    "C:\\Temp",
]


def _utf16le(s: str) -> bytes:
    return b"\xff\xfe" + s.encode("utf-16-le")


def test_grammar_utf8():
    data = SRC.encode()
    assert units("strings", data) == SRC_UNITS, units("strings", data)
    assert_identity("strings", data)
    mapping = {
        "0": "Willkommen",
        "1": 'Speichere "alle" Dateien',
        "2": "Zeile eins\nZeile zwei",
        "5": "Klartext",
        "6": "D:\\Temp",
    }
    expected = (
        SRC.replace('"Welcome" = "Welcome"', '"Welcome" = "Willkommen"')
        .replace('Save \\"all\\" files', 'Speichere \\"alle\\" Dateien')
        .replace("Line one\\nLine two", "Zeile eins\\nZeile zwei")
        .replace("plainKey = plainValue;", 'plainKey = "Klartext";')
        .replace("C:\\\\Temp", "D:\\\\Temp")
    ).encode()
    assert_round_trip("strings", data, mapping, expected)
    print("  [grammar] quoted/unquoted, comments, escapes; keys kept; byte-exact rebuild OK")


def test_utf16_with_bom_round_trip():
    data = _utf16le(SRC)
    filt = get_filter("strings")
    parsed = filt.parse(data)
    assert [u.text for u in parsed.units] == SRC_UNITS
    assert parsed.skeleton.get("source_encoding") == "utf-16", parsed.skeleton
    assert_identity("strings", data)
    out = filt.build(data, {"0": "\u3088\u3046\u3053\u305d"})
    assert out == _utf16le(SRC.replace('= "Welcome"', '= "\u3088\u3046\u3053\u305d"')), out[:80]
    # BOM-less UTF-16 LE (genstrings without a BOM) is sniffed and kept BOM-less
    bomless = SRC.encode("utf-16-le")
    assert units("strings", bomless) == SRC_UNITS
    assert_identity("strings", bomless)
    # UTF-16 BE with BOM
    be = b"\xfe\xff" + SRC.encode("utf-16-be")
    assert units("strings", be) == SRC_UNITS
    assert_identity("strings", be)
    print("  [encoding] UTF-16 LE/BE with BOM and BOM-less UTF-16 rebuilt in the same encoding OK")


def test_braces_wrapper_and_dictionary_form():
    data = b'{\n  "a" = "Alpha";\n  "b" = "Beta";\n}\n'
    assert units("strings", data) == ["Alpha", "Beta"]
    assert_identity("strings", data)
    print("  [grammar] a { } wrapped dictionary parses OK")


def test_placeholders():
    assert_placeholder_dialect(
        "Hello %@, you have %1$@ items", "%1$@ Artikel, hallo %@", "Hallo, Artikel %@"
    )
    assert_placeholder_dialect("Deleted %d files", "%d Dateien entfernt", "Dateien entfernt")
    print("  [placeholders] %@ / %1$@ / %d dropped in a translation are flagged OK")


def test_hostile_inputs():
    assert_refused("strings", b'"a" = "never closed;\n', ValueError, "never closed")
    assert_refused("strings", b'/* open comment\n"a" = "b";\n', ValueError, "never closed")
    assert_refused("strings", b"bplist00\x00\x01\x02", InputLimitError, "binary property list")
    assert_refused(
        "strings", '"a" = "Caf\u00e9";\n'.encode("cp1252"), UnsupportedEncodingError, "UTF-8"
    )
    assert_refused("strings", b'"a" = "b"\n"c" = "d";\n', ValueError, "missing ';'")
    assert_refused("strings", b'"a" = "b"; <script>\n', ValueError, "unexpected")
    assert_refused("strings", b'"a" = "b";\n"c" =', ValueError, "")
    assert_segment_cap("strings", lambda i: b'"k%d" = "Value number %d";\n' % (i, i))
    print("  [hostile] unterminated, binary plist, cp1252, malformed and over-cap files refused OK")


def test_records_view():
    recs = get_filter("strings").records(SRC.encode())
    assert recs[0].key == "Welcome" and recs[0].comment == "Title of the main window", recs[0]
    assert recs[1].key == "save.button" and recs[1].comment == "Button that saves", recs[1]
    assert recs[4].key == "pct" and recs[4].unit_index is None, recs[4]
    assert recs[6].key == "plainKey" and recs[6].value == "plainValue"
    print("  [records] key, value, comment and unit_index exposed OK")


def main():
    test_grammar_utf8()
    test_utf16_with_bom_round_trip()
    test_braces_wrapper_and_dictionary_form()
    test_placeholders()
    test_hostile_inputs()
    test_records_view()
    print("STRINGS FILTER OK")


if __name__ == "__main__":
    main()
