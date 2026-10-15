"""INI document filter, generic and Joomla language files (file-format Batch 1).

Spec checked: the Joomla language-file rules (manual.joomla.org, multilingual/language-files:
UTF-8, `;` comments, values in double quotes, `\\"` for a quote, one line per value) and the
legacy `"_QQ_"` quote spelling. Fixtures are hand-written.

  grammar    sections, `;` and `#` comments, `key = value` and `KEY="value"`, a line
             without `=`, an empty value, a duplicate key in two sections
  dialect    `\\"` and `_QQ_` quotes read as `"` and written back in the source's dialect
  encodings  UTF-8 (BOM kept), UTF-16 LE with BOM kept, Windows-1252 converted to UTF-8
  newline    a line break in a translation becomes a space (INI values are one line)
  hostile    binary bytes, the segment cap, a long-line budget
  records()  key / value / comment / context (section)

Hermetic + offline. Final line: "INI FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ini_")

from app.filters import get_filter
from app.filters.base import UnsupportedEncodingError
from tests._kvres_common import (
    assert_identity,
    assert_placeholder_dialect,
    assert_refused,
    assert_round_trip,
    assert_segment_cap,
    units,
)

JOOMLA = (
    b"; Joomla! Project\n"
    b"; Note : All ini files need to be saved as UTF-8\n"
    b"\n"
    b"; Shown on the login button\n"
    b'COM_DEMO_LOGIN="Log in"\n'
    b'COM_DEMO_QUOTE="Click \\"Save\\" to continue"\n'
    b'COM_DEMO_LEGACY="Press "_QQ_"Go"_QQ_" now"\n'
    b'COM_DEMO_COUNT="%d items in %s"\n'
    b'COM_DEMO_EMPTY=""\n'
    b'COM_DEMO_HTML="<strong>Warning!</strong> Check it"\n'
)

GENERIC = (
    b"[General]\r\n"
    b"# Window caption\r\n"
    b"title = My Application\r\n"
    b"version=1.2.3\r\n"
    b"just a stray line\r\n"
    b"\r\n"
    b"[Messages]\r\n"
    b"title = Messages window\r\n"
    b"welcome =   Welcome back, friend   \r\n"
)


def test_joomla_dialects():
    assert units("ini", JOOMLA) == [
        "Log in",
        'Click "Save" to continue',
        'Press "Go" now',
        "%d items in %s",
        "<strong>Warning!</strong> Check it",
    ], units("ini", JOOMLA)
    assert_identity("ini", JOOMLA)
    mapping = {
        "0": "Anmelden",
        "1": 'Klicken Sie auf "Speichern"',
        "2": 'Drücken Sie "Los"',
        "3": "%d Elemente in %s",
        "4": "Zeile eins\nZeile zwei",
    }
    expected = (
        JOOMLA.replace(b'"Log in"', b'"Anmelden"')
        .replace(b'Click \\"Save\\" to continue', b'Klicken Sie auf \\"Speichern\\"')
        .replace(b'Press "_QQ_"Go"_QQ_" now', 'Drücken Sie "_QQ_"Los"_QQ_"'.encode())
        .replace(b"%d items in %s", b"%d Elemente in %s")
        .replace(b"<strong>Warning!</strong> Check it", b"Zeile eins Zeile zwei")
    )
    filt = get_filter("ini")
    out = filt.build(JOOMLA, mapping)
    assert out == expected, out
    back = units("ini", out)
    assert back[1] == 'Klicken Sie auf "Speichern"' and back[2] == 'Drücken Sie "Los"', back
    assert back[4] == "Zeile eins Zeile zwei", back
    print("  [joomla] quotes written back in the source dialect; newline -> space OK")


def test_generic_sections_and_layout():
    assert units("ini", GENERIC) == [
        "My Application",
        "1.2.3",
        "Messages window",
        "Welcome back, friend",
    ], units("ini", GENERIC)
    assert_identity("ini", GENERIC)
    mapping = {"0": "Meine Anwendung", "3": "Willkommen zurück"}
    expected = GENERIC.replace(b"= My Application", b"= Meine Anwendung").replace(
        b"Welcome back, friend", "Willkommen zurück".encode()
    )
    assert_round_trip("ini", GENERIC, mapping, expected)
    print("  [generic] sections, # comments, CRLF, padding around values preserved OK")


def test_encodings():
    filt = get_filter("ini")
    bom8 = b"\xef\xbb\xbf" + JOOMLA
    assert units("ini", bom8)[0] == "Log in"
    assert_identity("ini", bom8)
    assert filt.build(bom8, {"0": "Anmelden"}).startswith(b"\xef\xbb\xbf; Joomla")

    u16 = b"\xff\xfe" + GENERIC.decode().encode("utf-16-le")
    parsed = filt.parse(u16)
    assert parsed.units[0].text == "My Application"
    assert parsed.skeleton.get("source_encoding") == "utf-16", parsed.skeleton
    assert_identity("ini", u16)
    out = filt.build(u16, {"0": "日本"})
    assert out == b"\xff\xfe" + GENERIC.decode().replace("My Application", "日本").encode(
        "utf-16-le"
    )

    cp = "[a]\nname = Café\n".encode("cp1252")
    parsed = filt.parse(cp)
    assert parsed.units[0].text == "Café" and parsed.skeleton.get("source_encoding") == "cp1252"
    out = filt.build(cp, {"0": "Кафе"})
    assert out == "[a]\nname = Кафе\n".encode(), out
    print("  [encoding] UTF-8 BOM and UTF-16 kept; Windows-1252 converted to UTF-8 OK")


def test_hostile_inputs():
    assert_refused("ini", b"k=\x81\x00\x00\x00\x01\x02" * 40, UnsupportedEncodingError, "")
    assert_segment_cap("ini", lambda i: b'KEY_%d="Value number %d"\n' % (i, i))
    assert units("ini", b"=\n= =\n[]\n[unclosed\n;\n") == []
    print("  [hostile] binary and over-cap files refused; degenerate lines tolerated OK")


def test_records_and_placeholders():
    recs = get_filter("ini").records(GENERIC)
    assert [(r.context, r.key) for r in recs] == [
        ("General", "title"),
        ("General", "version"),
        ("Messages", "title"),
        ("Messages", "welcome"),
    ], recs
    assert recs[0].comment == "Window caption" and not recs[2].duplicate
    jrec = get_filter("ini").records(JOOMLA)
    assert jrec[0].comment == "Shown on the login button", jrec[0]
    assert jrec[4].key == "COM_DEMO_EMPTY" and jrec[4].unit_index is None
    assert_placeholder_dialect("%d items in %s", "%s: %d Elemente", "Elemente")
    print("  [records] key, value, comment, section context; printf placeholders gated OK")


def main():
    test_joomla_dialects()
    test_generic_sections_and_layout()
    test_encodings()
    test_hostile_inputs()
    test_records_and_placeholders()
    print("INI FILTER OK")


if __name__ == "__main__":
    main()
