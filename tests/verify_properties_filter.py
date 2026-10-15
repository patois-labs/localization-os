"""Java .properties document filter (file-format Batch 1).

Spec checked: java.util.Properties.load(Reader) and store(), Java SE 21; PropertyResourceBundle
(UTF-8 first, ISO-8859-1 fallback); java.text.MessageFormat quoting. Fixtures are hand-written.

  grammar    `=`, `:` and blank separators, `#`/`!` comments, `\\` continuation, escaped
             key blanks, `\\:` in a value, an empty value, a duplicate key
  encodings  all-ASCII (native2ascii) stays ASCII with \\uXXXX; ISO-8859-1 stays ISO-8859-1;
             UTF-8 stays UTF-8; CRLF, the BOM and the trailing newline survive
  apostrophe a MessageFormat value with all apostrophes doubled shows single ones and is
             doubled again on build; a value with a lone quote is left exactly as written
  hostile    malformed \\uXXXX, binary bytes, UTF-16, the segment cap
  records()  key / value / comment / duplicate / unit_index

Hermetic + offline. Final line: "PROPERTIES FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_props_")

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

ASCII = (
    b"# Greeting shown on the home page\r\n"
    b"! second comment style\r\n"
    b"app.title = Welcome to Northwind\r\n"
    b"app.colon:Sign in\r\n"
    b"app.space Sign out\r\n"
    b"long.text = First part, \\\r\n"
    b"    second part\r\n"
    b"latin = Caf\\u00e9 au lait\r\n"
    b"empty =\r\n"
    b"dup = First\r\n"
    b"dup = Second\r\n"
    b"mf.args = Hello {0}, you have {1} messages\r\n"
    b"mf.apos = Don''t delete {0}\r\n"
    b"mf.lonequote = Use '{0}' literally\r\n"
    b"plain.apos = Don't panic\r\n"
    b"id.only = {0}\r\n"
    b"pad = \\ leading space\r\n"
    b"key\\ with\\ space = Spaced key value\r\n"
    b"url = https\\://example.com\r\n"
)

ASCII_UNITS = [
    "Welcome to Northwind",
    "Sign in",
    "Sign out",
    "First part, second part",
    "Caf\u00e9 au lait",
    "First",
    "Second",
    "Hello {0}, you have {1} messages",
    "Don't delete {0}",
    "Use '{0}' literally",
    "Don't panic",
    "leading space",
    "Spaced key value",
    "https://example.com",
]


def test_grammar_and_units():
    assert units("properties", ASCII) == ASCII_UNITS, units("properties", ASCII)
    assert_identity("properties", ASCII)
    print("  [grammar] separators, comments, continuation, escapes; echo build byte-identical OK")


def test_ascii_round_trip_escapes_non_ascii():
    mapping = {
        "0": "Willkommen bei Northwind",
        "3": "Erster Teil,\nzweiter Teil",
        "4": "Caf\u00e9 \u2014 \u65e5\u672c \U0001f600",
        "8": "N'effacez pas {0}",
        "10": "Pas de panique, c'est {fini}",
        "11": "espace",
        "12": "a\\b",
    }
    expected = (
        ASCII.replace(b"Welcome to Northwind", b"Willkommen bei Northwind")
        .replace(b"First part, \\\r\n    second part", b"Erster Teil,\\nzweiter Teil")
        .replace(b"Caf\\u00e9 au lait", b"Caf\\u00E9 \\u2014 \\u65E5\\u672C \\uD83D\\uDE00")
        .replace(b"Don''t delete {0}", b"N''effacez pas {0}")
        .replace(b"Don't panic", b"Pas de panique, c'est {fini}")
        .replace(b"\\ leading space", b"\\ espace")
        .replace(b"Spaced key value", b"a\\\\b")
    )
    assert_round_trip("properties", ASCII, mapping, expected)
    print("  [ascii] non-ASCII written as \\uXXXX (surrogate pairs too), newline as \\n OK")


def test_messageformat_apostrophes():
    src = b"a = It''s {0}\nb = Quote '{0}' here\nc = It's plain\n"
    assert units("properties", src) == ["It's {0}", "Quote '{0}' here", "It's plain"]
    out = get_filter("properties").build(
        src, {"0": "C'est {0}", "1": "Zitat '{0}' hier", "2": "C'est simple"}
    )
    assert out == b"a = C''est {0}\nb = Zitat '{0}' hier\nc = C'est simple\n", out
    # a translator who already typed '' does not get it quadrupled
    out2 = get_filter("properties").build(src, {"0": "C''est {0}"})
    assert out2 == b"a = C''est {0}\nb = Quote '{0}' here\nc = It's plain\n", out2
    # #697: a value with {n} arguments and no apostrophe is a MessageFormat pattern too;
    # a French translation's apostrophes are doubled (single, MessageFormat would quote the
    # rest: "Ouvrez lapplication {0}"). Without arguments nothing changes.
    src3 = (
        b"open = Open the application {0} now\nsave = Saved {1,number} of {0}\nhelp = Open help\n"
    )
    out3 = get_filter("properties").build(
        src3,
        {
            "0": "Ouvrez l'application {0} maintenant",
            "1": "{1,number} sur {0} enregistrés d'abord",
            "2": "Ouvrez l'aide",
        },
    )
    assert out3 == (
        b"open = Ouvrez l''application {0} maintenant\n"
        b"save = {1,number} sur {0} enregistr\\u00E9s d''abord\nhelp = Ouvrez l'aide\n"
    ), out3
    # #708: a source that quotes on purpose ('{' ... '}') is shown as written, but an elision
    # in the translation (live fr-FR "pour l'insérer dans {0}") never quotes: written ''.
    # Quoting apostrophes next to braces/spaces and an already doubled one stay as written.
    src4 = b"hint = Wrap a variable in '{' and '}' to insert it into {0}.\n"
    assert units("properties", src4) == ["Wrap a variable in '{' and '}' to insert it into {0}."]
    out4 = get_filter("properties").build(
        src4, {"0": "Envelopper une variable entre '{' et '}' pour l'insérer dans {0}."}
    )
    assert out4 == (
        b"hint = Envelopper une variable entre '{' et '}' pour l''ins\\u00E9rer dans {0}.\n"
    ), out4
    out5 = get_filter("properties").build(
        src4, {"0": "Racchiudi '{' e '}' per l''inserirla in {0}."}
    )
    assert out5 == b"hint = Racchiudi '{' e '}' per l''inserirla in {0}.\n", out5
    # live fr-FR: the engine unbalanced the quotes ('{'"id": 42"}' opens one at the end), and
    # the elision CLOSES it, which keeps {0} working: left single (doubling would quote {0})
    src6 = b"""json = Type '{'"id": 42'}' to select the record in {0}.\n"""
    tr6 = """Tapez '{'"id": 42"}' pour sélectionner l'enregistrement dans {0}."""
    out6 = get_filter("properties").build(src6, {"0": tr6})
    assert b"l'enregistrement dans {0}." in out6, out6
    print("  [messageformat] '' collapsed for translators, doubled on build with {n} args OK")


def test_latin1_and_utf8_encodings():
    latin = "title=Caf\u00e9\nother=Na\u00efve\n".encode("latin-1")
    filt = get_filter("properties")
    parsed = filt.parse(latin)
    assert [u.text for u in parsed.units] == ["Caf\u00e9", "Na\u00efve"]
    assert parsed.skeleton.get("source_encoding") == "iso8859-1", parsed.skeleton
    out = filt.build(latin, {"0": "Caf\u00e9 \u65e5\u672c"})
    assert out == b"title=Caf\xe9 \\u65E5\\u672C\nother=Na\xefve\n", out
    assert_identity("properties", latin)

    utf8 = "\ufefftitle=Caf\u00e9\n".encode()
    assert units("properties", utf8) == ["Caf\u00e9"]
    out = filt.build(utf8, {"0": "\u65e5\u672c\u8a9e"})
    assert out == "\ufefftitle=\u65e5\u672c\u8a9e\n".encode(), out
    assert_identity("properties", utf8)
    print("  [encoding] ISO-8859-1 stays ISO-8859-1, UTF-8 (BOM) stays UTF-8 OK")


def test_hostile_inputs():
    assert_refused("properties", b"k = bad \\u12G4\n", ValueError, "malformed")
    assert_refused(
        "properties", b"k = v\x00\x01\x02\x03\x04\x05" * 50, UnsupportedEncodingError, "binary"
    )
    assert_refused("properties", "k=v\n".encode("utf-16"), InputLimitError, "UTF-16")
    assert_segment_cap("properties", lambda i: b"key%d = Value number %d\n" % (i, i))
    # a key-only file and a file of comments only parse to nothing (ingest refuses empty)
    assert units("properties", b"# only a comment\nflag\n") == []
    # odd backslash at end of file, no next line: no crash
    assert units("properties", b"k = tail \\") == ["tail"]
    print("  [hostile] malformed escape, binary, UTF-16 and over-cap files refused OK")


def test_records_view():
    recs = get_filter("properties").records(ASCII)
    first = recs[0]
    assert first.key == "app.title" and first.value == "Welcome to Northwind", first
    assert first.comment == "Greeting shown on the home page\nsecond comment style", first.comment
    by_key = {}
    for r in recs:
        by_key.setdefault(r.key, []).append(r)
    assert [r.duplicate for r in by_key["dup"]] == [False, True]
    assert by_key["empty"][0].unit_index is None and by_key["id.only"][0].unit_index is None
    assert by_key["key with space"][0].value == "Spaced key value"
    assert by_key["url"][0].unit_index == 13
    print("  [records] key, value, comment, duplicate and unit_index exposed OK")


def test_placeholders():
    assert_placeholder_dialect("Hello {0}, you have {1} messages", "Hallo {1} {0}", "Hallo {0}")
    assert_placeholder_dialect("Saved %s files", "%s Dateien", "Dateien")
    print("  [placeholders] MessageFormat {n} and printf %s seen by the placeholder gate OK")


def _java_load_line(line: str) -> tuple[str, str]:
    """Independent emulation of java.util.Properties.load for ONE natural line (Java SE 21
    source, load0 + loadConvert): the key ends at the first unescaped `=`, `:` or blank;
    then blanks are skipped and ONE `=`/`:` (only if no `=`/`:` ended the key) and more
    blanks. Backslash + char is that char (t n r f and uXXXX aside)."""
    ws = " \t\f"
    i = 0
    while i < len(line) and line[i] in ws:
        i += 1
    line = line[i:]
    n = len(line)
    key_len, value_start, has_sep, prec_bs = 0, n, False, False
    while key_len < n:
        c = line[key_len]
        if c in "=:" and not prec_bs:
            value_start, has_sep = key_len + 1, True
            break
        if c in ws and not prec_bs:
            value_start = key_len + 1
            break
        prec_bs = (not prec_bs) if c == "\\" else False
        key_len += 1
    while value_start < n:
        c = line[value_start]
        if c not in ws:
            if not has_sep and c in "=:":
                has_sep = True
            else:
                break
        value_start += 1

    def conv(s: str) -> str:
        out, j = [], 0
        while j < len(s):
            c = s[j]
            if c == "\\" and j + 1 < len(s):
                d = s[j + 1]
                if d == "u":
                    out.append(chr(int(s[j + 2 : j + 6], 16)))
                    j += 6
                    continue
                out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(d, d))
                j += 2
                continue
            out.append(c)
            j += 1
        return "".join(out)

    return conv(line[:key_len]), conv(line[value_start:])


def test_leading_separator_chars_732():
    # #732: a translation that STARTS with `=` or `:` is written `\=` / `\:`; on a `key value`
    # line (blank separator) Java's load() took the raw `=` / `:` as the separator and the
    # value lost it. `key=value` / `key: value` lines get the same escape (harmless there).
    src = (
        b"key3 Text 3\n"
        b"k\\:\\= Text\n"
        b"key4=Text 4\n"
        b"key5: Text 5\n"
        b"key6 = Text 6\n"
        b"key7\tTab text\n"
        b"mf Don''t open {0}\n"
        b"mid Plain value\n"
    )
    mapping = {
        "0": "=1+2 Text 3",
        "1": ": optional",
        "2": "= Total",
        "3": ":colon",
        "4": "==double",
        "5": "=",
        "6": ": N'ouvrez pas {0}",
        "7": "a=b: c",
    }
    out = get_filter("properties").build(src, mapping)
    assert out == (
        b"key3 \\=1+2 Text 3\n"
        b"k\\:\\= \\: optional\n"
        b"key4=\\= Total\n"
        b"key5: \\:colon\n"
        b"key6 = \\==double\n"
        b"key7\t\\=\n"
        b"mf \\: N''ouvrez pas {0}\n"
        b"mid a=b: c\n"
    ), out
    want = {
        "key3": "=1+2 Text 3",
        "k:=": ": optional",
        "key4": "= Total",
        "key5": ":colon",
        "key6": "==double",
        "key7": "=",
        "mf": ": N''ouvrez pas {0}",  # the raw MessageFormat pattern Java hands to format()
        "mid": "a=b: c",
    }
    java = dict(_java_load_line(ln) for ln in out.decode("ascii").splitlines())
    assert java == want, java
    # our own parser reads it back the same way (MessageFormat apostrophes undone for display;
    # a bare "=" has no words, so it is not a unit on re-import)
    assert units("properties", out) == [
        mapping[str(i)] if i != 6 else ": N'ouvrez pas {0}" for i in range(8) if i != 5
    ], units("properties", out)
    assert [r.value for r in get_filter("properties").records(out)] == list(want.values())
    # the source file itself parses the same in both readers
    assert dict(_java_load_line(ln) for ln in src.decode("ascii").splitlines())["k:="] == "Text"
    # no-op: untouched and echoed entries (including an already escaped leading `\=`) stay
    # byte-identical; a value that does not start with `=` / `:` is never escaped.
    esc = b"eq \\=already\ncol \\:kept\nplain = x=y\n"
    assert units("properties", esc) == ["=already", ":kept", "x=y"]
    assert_identity("properties", esc)
    assert_identity("properties", src)
    print("  [#732] leading = / : escaped; Java load() and our parser read the translation OK")


def test_end_to_end_web_upload_and_export():
    """The real door: web upload of an ISO-8859-1 file, segment targets, export bytes."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import Document, Segment
    from app.services import export

    init_db()
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "props-e2e",
            "source_locale": "en-US",
            "target_locales": "de-DE",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    src = "# Title\r\ntitle = Caf\u00e9 menu\r\nid = {0}\r\nbye = Goodbye {0}\r\n".encode("latin-1")
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("messages.properties", src, "text/x-java-properties")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])
    with Session(engine) as s:
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
            .order_by(Segment.order_index)
        ).all()
        assert [g.source_text for g in segs] == ["Caf\u00e9 menu", "Goodbye {0}"], segs
        segs[0].target_text = "Caf\u00e9-Men\u00fc \u2615"
        segs[1].target_text = "Auf Wiedersehen {0}"
        for g in segs:
            g.status = "mt"
            s.add(g)
        s.commit()
        doc = s.get(Document, doc_id)
        out = export.build_translated_file(s, doc, "de-DE")
    assert out == (
        b"# Title\r\ntitle = Caf\xe9-Men\xfc \\u2615\r\nid = {0}\r\nbye = Auf Wiedersehen {0}\r\n"
    ), out
    print("  [e2e] web upload of ISO-8859-1 .properties exports ISO-8859-1 with \\u escapes OK")


def main():
    test_grammar_and_units()
    test_ascii_round_trip_escapes_non_ascii()
    test_messageformat_apostrophes()
    test_latin1_and_utf8_encodings()
    test_hostile_inputs()
    test_records_view()
    test_placeholders()
    test_leading_separator_chars_732()
    test_end_to_end_web_upload_and_export()
    print("PROPERTIES FILTER OK")


if __name__ == "__main__":
    main()
