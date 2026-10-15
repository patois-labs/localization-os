"""gettext MO filter (formats Batch 11, gettextmo): compiled catalogs in and out.

  units     an MO upload yields exactly the units a PO of the same catalog yields
            (contexts, msgid_plural as its own unit, header skipped).
  identity  an untouched build returns the original bytes (little- and big-endian, with a
            hash table) - never an equivalent recompile.
  build     a translated build is a valid MO: CPython's gettext and babel read it;
            contexts, plurals, placeholders survive; untranslated entries are left out.
  localized Language and Plural-Forms are switched to the target (ru: nplurals=3), and a
            header with no Language field gets one.
  PO export build_po / as_fmt=po on the API download and the web export give a .po file.
  encoding  a declared ISO-8859-1 catalog is decoded with its charset and exported UTF-8.
  hostile   bad magic, truncation, out-of-file tables and strings, a missing NUL at EOF,
            revision 2, system-dependent strings, overlapping-string amplification, stray
            NULs, invalid UTF-8 and the segment cap are refused with a plain message.

Fixtures are packed by hand here (independent of the filter's own writer), straight from
the GNU gettext manual layout. Hermetic + offline. Final line: "MO FILTER OK".
"""

import gettext
import io
import os
import struct
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mofilter_")

from babel.messages.mofile import read_mo
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.filters.mo_filter import InvalidMoFile, build_po, mo_to_po
from app.main import app
from app.models import Customer, Project, Role, Segment, SegStatus, TargetLocale, TransMode
from app.services import apikeys, security
from app.services.ingest import ingest_document

HEADER_FR = (
    "Project-Id-Version: demo 1.0\n"
    "Language: fr\n"
    "MIME-Version: 1.0\n"
    "Content-Type: text/plain; charset=UTF-8\n"
    "Content-Transfer-Encoding: 8bit\n"
    "Plural-Forms: nplurals=2; plural=(n > 1);\n"
)
CATALOG = [
    ("", HEADER_FR),
    ("%d file\x00%d files", "%d fichier\x00%d fichiers"),
    ("Hello %(name)s, you have {0} messages", "Bonjour %(name)s, vous avez {0} messages"),
    ("menu\x04Open", "Ouvrir"),
    ("state\x04Open", "Ouvert"),
]
PO_EQUIVALENT = (
    'msgid ""\n'
    'msgstr ""\n' + "".join(f'"{line}\\n"\n' for line in HEADER_FR.splitlines()) + "\n"
    'msgid "%d file"\nmsgid_plural "%d files"\nmsgstr[0] "%d fichier"\nmsgstr[1] "%d fichiers"\n\n'
    'msgid "Hello %(name)s, you have {0} messages"\n'
    'msgstr "Bonjour %(name)s, vous avez {0} messages"\n\n'
    'msgctxt "menu"\nmsgid "Open"\nmsgstr "Ouvrir"\n\n'
    'msgctxt "state"\nmsgid "Open"\nmsgstr "Ouvert"\n'
).encode()


def pack_mo(pairs, *, endian="<", hash_size=0, revision=0, encoding="utf-8", sysdep=None):
    """Pack (msgid, msgstr) pairs per the GNU manual. Strings are written in table order
    (callers pass them sorted), NUL terminated; an optional zero-filled hash table sits
    between the tables and the strings."""
    keys = [k.encode(encoding) if isinstance(k, str) else k for k, _v in pairs]
    vals = [v.encode(encoding) if isinstance(v, str) else v for _k, v in pairs]
    n = len(pairs)
    head_words = 7 if sysdep is None else 12
    o = head_words * 4
    t = o + 8 * n
    h = t + 8 * n
    pos = h + 4 * hash_size
    otab, ttab, blob = b"", b"", b""
    for k in keys:
        otab += struct.pack(endian + "II", len(k), pos)
        blob += k + b"\x00"
        pos += len(k) + 1
    for v in vals:
        ttab += struct.pack(endian + "II", len(v), pos)
        blob += v + b"\x00"
        pos += len(v) + 1
    words = [0x950412DE, revision, n, o, t, hash_size, h]
    if sysdep is not None:
        words += list(sysdep)
    head = struct.pack(endian + "%dI" % len(words), *words)  # noqa: UP031
    return head + otab + ttab + b"\x00" * (4 * hash_size) + blob


MO_LE = pack_mo(CATALOG)
MO_BE_HASH = pack_mo(CATALOG, endian=">", hash_size=7)
MO = get_filter("mo")
PO = get_filter("po")


def _texts(result):
    return [u.text for u in result.units]


def test_registered():
    assert get_filter("mo") is MO and get_filter("gmo") is MO
    assert "mo" in supported_exts() and "gmo" in supported_exts()
    print("  [registry] .mo and .gmo resolve to the MO filter OK")


def test_units_match_po():
    want = _texts(PO.parse(PO_EQUIVALENT))
    for name, data in (("LE", MO_LE), ("BE+hash", MO_BE_HASH)):
        got = MO.parse(data)
        assert _texts(got) == want, (name, _texts(got), want)
        assert got.skeleton["format"] == "mo"
    assert want == [
        "%d file",
        "%d files",
        "Hello %(name)s, you have {0} messages",
        "Open",
        "Open",
    ], want
    print("  [units] MO units == PO units (plural pair, contexts, header skipped) OK")


def test_identity():
    for data in (MO_LE, MO_BE_HASH):
        assert MO.build(data, {}) == data
        # Supplying text IS a change (the msgstrs become that text), so it recompiles.
        same = {str(i): t for i, t in enumerate(_texts(MO.parse(data)))}
        assert MO.build(data, same) != data
    print("  [identity] untouched build returns the original bytes (LE, BE + hash) OK")


def _translated(data, locale=None):
    texts = {
        "0": "%d Datei",
        "1": "%d Dateien",
        "2": "Hallo %(name)s, Sie haben {0} Nachrichten",
        "3": "Oeffnen",
        "4": "Offen",
    }
    if locale:
        return MO.build_localized(data, texts, locale)
    return MO.build(data, texts)


def test_translated_build_is_valid_mo():
    for data in (MO_LE, MO_BE_HASH):
        out = _translated(data)
        t = gettext.GNUTranslations(io.BytesIO(out))
        assert t.gettext("Hello %(name)s, you have {0} messages") == (
            "Hallo %(name)s, Sie haben {0} Nachrichten"
        )
        assert t.pgettext("menu", "Open") == "Oeffnen"
        assert t.pgettext("state", "Open") == "Offen"
        assert t.ngettext("%d file", "%d files", 1) == "%d Datei"
        assert t.ngettext("%d file", "%d files", 5) == "%d Dateien"
        cat = read_mo(io.BytesIO(out))
        assert cat.get("Hello %(name)s, you have {0} messages").string.startswith("Hallo")
        # Re-parsing our own output gives the same units (parse/build walk one order).
        assert _texts(MO.parse(out)) == _texts(MO.parse(data))
    # An untranslated entry (empty msgstr) is left out, as msgfmt does.
    sparse = pack_mo([("", HEADER_FR), ("Keep", "Garder"), ("Skip", "")])
    out = MO.build(sparse, {"0": "Behalten"})
    t = gettext.GNUTranslations(io.BytesIO(out))
    assert t.gettext("Keep") == "Behalten" and t.gettext("Skip") == "Skip"
    assert b"Skip" not in out
    print("  [build] gettext + babel read the rebuilt MO; ctx, plurals, placeholders OK")


def test_localized_plural_forms():
    out = _translated(MO_LE, "ru-RU")
    t = gettext.GNUTranslations(io.BytesIO(out))
    info = t.info()
    assert info["language"] == "ru_RU", info
    assert info["plural-forms"].startswith("nplurals=3;"), info
    # #729 (decision 18): slot 2 (Russian "many") holds the plural translation; it used
    # to be empty and the program showed the English msgid_plural for 5, 11, 100 ...
    for n in (0, 5, 11, 100):
        assert t.ngettext("%d file", "%d files", n) == "%d Dateien", n
    assert t.ngettext("%d file", "%d files", 1) == "%d Datei"
    # A header with no Language field gets one for the target.
    bare = pack_mo(
        [
            (
                "",
                "Content-Type: text/plain; charset=UTF-8\n"
                "Plural-Forms: nplurals=2; plural=(n != 1);\n",
            ),
            ("A\x00As", "x\x00xs"),
        ]
    )
    out = MO.build_localized(bare, {"0": "Un", "1": "Uns"}, "pl-PL")
    info = gettext.GNUTranslations(io.BytesIO(out)).info()
    assert info["language"] == "pl_PL" and info["plural-forms"].startswith("nplurals=3;"), info
    print("  [localized] Language + Plural-Forms switched (ru nplurals=3; added when absent) OK")


def test_one_form_target_uses_plural_682():
    """#682: a target with ONE plural form (ja, zh, ko, vi, th, tr: nplurals=1) uses that form
    for every count, so it must carry the msgid_plural translation (the general form), not
    the singular one. The old export kept the singular ("One file"), lost the %d and made
    `ngettext(...) % n` raise. Falls back to the singular when the plural is untranslated."""
    src = pack_mo(
        [
            (
                "",
                "Language: en\nContent-Type: text/plain; charset=UTF-8\n"
                "Plural-Forms: nplurals=2; plural=(n != 1);\n",
            ),
            ("One file\x00%d files", "\x00"),
            ("Hello", ""),
        ]
    )
    for locale in ("ja-JP", "tr-TR", "zh-CN", "ko-KR", "vi-VN", "th-TH"):
        texts = {"0": "T-one", "1": "T-many %d", "2": "T-hello"}
        out = MO.build_localized(src, texts, locale)
        t = gettext.GNUTranslations(io.BytesIO(out))
        assert t.info()["plural-forms"].startswith("nplurals=1;"), (locale, t.info())
        for n in (0, 1, 5):
            assert t.ngettext("One file", "%d files", n) % n == f"T-many {n}", (locale, n)
        assert t.gettext("Hello") == "T-hello"
        po = PO.build_localized(mo_to_po(src), texts, locale).decode()
        assert 'msgstr[0] "T-many %d"' in po and "msgstr[1]" not in po, (locale, po)
        # only the singular translated: it is all there is, keep it
        po = PO.build_localized(mo_to_po(src), {"0": "T-one"}, locale).decode()
        assert 'msgstr[0] "T-one"' in po, (locale, po)
        # gate: a plural unit the export fills with source text (fallback) is not a
        # translation and never replaces the translated singular
        fb = {"0": "T-one", "1": "%d files", "2": "T-hello"}
        po = PO.build_localized(mo_to_po(src), fb, locale, fallback_keys={"1"}).decode()
        assert 'msgstr[0] "T-one"' in po and '%d files"\nmsgstr' in po, (locale, po)
        out = MO.build_localized(src, fb, locale, fallback_keys={"1"})
        assert gettext.GNUTranslations(io.BytesIO(out)).ngettext("One file", "%d files", 5) == (
            "T-one"
        )
    # two-form targets are unchanged
    two = MO.build_localized(src, {"0": "Eine Datei", "1": "%d Dateien"}, "de-DE")
    t = gettext.GNUTranslations(io.BytesIO(two))
    assert t.ngettext("One file", "%d files", 1) == "Eine Datei"
    assert t.ngettext("One file", "%d files", 5) % 5 == "5 Dateien"
    print("  [#682] one-plural-form targets carry the plural translation in msgstr[0] OK")


def test_cldr_only_targets_739():
    """#739: targets missing from babel's legacy gettext table (id, ms, ta, sw ...) kept an
    empty Language and the source two-form rule; the MO goes through the PO path, so it
    did too. Now Language and the CLDR-derived rule are stamped and id (one form) runs
    the #682 mapping."""
    src = pack_mo(
        [
            (
                "",
                "Language: en\nContent-Type: text/plain; charset=UTF-8\n"
                "Plural-Forms: nplurals=2; plural=(n != 1);\n",
            ),
            ("One file\x00%d files", "\x00"),
        ]
    )
    texts = {"0": "Satu berkas", "1": "%d berkas"}
    t = gettext.GNUTranslations(io.BytesIO(MO.build_localized(src, texts, "id-ID")))
    assert t.info()["language"] == "id_ID", t.info()
    assert t.info()["plural-forms"] == "nplurals=1; plural=0;", t.info()
    for n in (0, 1, 5):
        assert t.ngettext("One file", "%d files", n) % n == f"{n} berkas", n
    texts = {"0": "Faili moja", "1": "Faili %d"}
    t = gettext.GNUTranslations(io.BytesIO(MO.build_localized(src, texts, "sw-KE")))
    assert t.info()["language"] == "sw_KE", t.info()
    assert t.ngettext("One file", "%d files", 1) == "Faili moja"
    assert t.ngettext("One file", "%d files", 5) % 5 == "Faili 5"
    po = build_po(src, {"0": "x", "1": "y"}, "ta-IN").decode()
    assert "Language: ta_IN\\n" in po and "nplurals=2; plural=(n != 1);" in po, po
    print("  [#739] CLDR-only targets (id, sw, ta) get Language + their own rule OK")


def test_build_po():
    po = build_po(MO_LE, {"2": "Hola %(name)s, tienes {0} mensajes"}, "es-ES").decode()
    assert 'msgctxt "menu"' in po and 'msgid_plural "%d files"' in po, po
    assert 'msgstr "Hola %(name)s, tienes {0} mensajes"' in po, po
    assert "Language: es_ES" in po, po
    assert _texts(PO.parse(po.encode())) == _texts(MO.parse(MO_LE))
    assert mo_to_po(MO_LE).decode().count('msgid ""') == 1
    print("  [po export] build_po gives a PO with the same units and the target header OK")


def test_declared_charset():
    hdr = "Content-Type: text/plain; charset=ISO-8859-1\nLanguage: fr\n"
    data = pack_mo([("", hdr), ("Coffee", "Café")], encoding="latin-1")
    res = MO.parse(data)
    assert _texts(res) == ["Coffee"], _texts(res)
    assert "Café" in mo_to_po(data).decode()
    assert "charset=UTF-8" in mo_to_po(data).decode()
    assert MO.build(data, {}) == data
    out = MO.build(data, {"0": "Café crème"})
    t = gettext.GNUTranslations(io.BytesIO(out))
    assert t.gettext("Coffee") == "Café crème" and t.charset().lower() == "utf-8"
    print("  [encoding] ISO-8859-1 catalog decoded by its charset, exported UTF-8 OK")


def _refused(data, cls=InputLimitError, needle=""):
    try:
        MO.parse(data)
    except cls as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return
    raise AssertionError(f"expected {cls.__name__} for {data[:32]!r}")


def test_hostile():
    _refused(b"", InvalidMoFile, "shorter")
    _refused(b"\x00" * 40, InvalidMoFile, "magic")
    _refused(b'msgid ""\nmsgstr ""\n', InvalidMoFile)
    _refused(MO_LE[:20], InvalidMoFile, "shorter")
    _refused(pack_mo(CATALOG, revision=2 << 16), InvalidMoFile, "revision")
    # Original table pointing past the end of the file.
    bad = bytearray(MO_LE)
    struct.pack_into("<I", bad, 12, len(MO_LE) + 100)
    _refused(bytes(bad), InvalidMoFile, "outside")
    # Hash table past the end.
    bad = bytearray(MO_LE)
    struct.pack_into("<II", bad, 20, 1000, len(MO_LE))
    _refused(bytes(bad), InvalidMoFile, "hash table")
    # A string offset outside the file.
    bad = bytearray(MO_LE)
    struct.pack_into("<II", bad, 28 + 8, 5, len(MO_LE) + 10)
    _refused(bytes(bad), InvalidMoFile, "string")
    # Last string with no NUL terminator (ends exactly at EOF).
    _refused(MO_LE[:-1], InvalidMoFile, "string")
    # N far over MAX_DOCUMENT_SEGMENTS is refused before any table read.
    bad = bytearray(MO_LE)
    struct.pack_into("<I", bad, 8, 0xFFFFFFFF)
    _refused(bytes(bad), InputLimitError, "MAX_DOCUMENT_SEGMENTS")
    # N inside the cap but tables longer than the file.
    bad = bytearray(MO_LE)
    struct.pack_into("<I", bad, 8, 40000)
    _refused(bytes(bad), InvalidMoFile, "outside")
    # System-dependent strings (revision 0.1 with a non-zero count).
    sysdep = pack_mo(CATALOG, revision=1, sysdep=(0, 0, 1, 0, 0))
    _refused(sysdep, InvalidMoFile, "system-dependent")
    # Revision 0.1 with NO sysdep strings reads fine.
    assert _texts(MO.parse(pack_mo(CATALOG, revision=1, sysdep=(0, 0, 0, 0, 0))))
    # Every entry pointing at one big string: decoded size far above the file size.
    n = 300
    base = 28 + 16 * n
    otab = struct.pack("<II", 1, base) * n
    ttab = struct.pack("<II", 5000, base + 2) * n
    strings = b"A\x00" + b"x" * 5000 + b"\x00"
    bad = struct.pack("<7I", 0x950412DE, 0, n, 28, 28 + 8 * n, 0, 0) + otab + ttab + strings
    _refused(bad, InputLimitError, "overlapping")
    # NULs where no plural is declared.
    _refused(pack_mo([("", HEADER_FR), ("One", "a\x00b")]), InvalidMoFile, "NUL")
    _refused(pack_mo([("", HEADER_FR), ("a\x00b\x00c", "x")]), InvalidMoFile, "NUL")
    # Bytes that are not the declared charset.
    _refused(pack_mo([("", HEADER_FR), (b"\xff\xfe", b"x")]), InvalidMoFile, "UTF-8")
    # The segment cap applies while units are built.
    saved = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 3
    try:
        _refused(MO_LE, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = saved
    print("  [hostile] header, offsets, NUL, revision, sysdep, amplification, caps refused OK")


PW = "mofilter-pw-123456"


def _project_with_mo(locale="de-DE"):
    with Session(engine) as s:
        c = Customer(name="MO Cust")
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(
            name="mo", org_id=1, source_locale="en-US", default_provider="mock", customer_id=c.id
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=locale))
        s.commit()
        d = ingest_document(s, p, "app.mo", MO_LE, TransMode.SEGMENTED)
        s.commit()
        s.refresh(d)
        assert d.fmt == "mo", d.fmt
        for seg in s.exec(select(Segment).where(Segment.document_id == d.id)).all():
            seg.target_text = "DE " + seg.source_text
            seg.status = SegStatus.MT
            s.add(seg)
        s.commit()
        return d.id


def test_export_surfaces():
    user = security.create_user("mo@x.com", "mo@x.com", Role.ADMIN, PW)
    _row, tok = apikeys.create_key(user.id, "mo", scopes="api docs keys admin")
    doc_id = _project_with_mo()
    client = TestClient(app, raise_server_exceptions=False)
    hdr = {"X-Loc-Token": tok}
    url = "/api/documents/%d/download" % doc_id  # noqa: UP031
    r = client.get(url, params={"locale": "de-DE"}, headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    t = gettext.GNUTranslations(io.BytesIO(r.content))
    assert t.pgettext("menu", "Open") == "DE Open", t.pgettext("menu", "Open")
    assert t.info()["language"] == "de_DE"
    assert ".de-DE.mo" in r.headers["content-disposition"], r.headers["content-disposition"]
    r = client.get(url, params={"locale": "de-DE", "as_fmt": "po"}, headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert 'msgctxt "menu"' in r.text and 'msgstr "DE Open"' in r.text, r.text
    assert ".de-DE.po" in r.headers["content-disposition"], r.headers["content-disposition"]
    assert r.headers["content-type"].startswith("text/x-gettext-translation")
    r = client.get(url, params={"locale": "de-DE", "as_fmt": "xliff"}, headers=hdr)
    assert r.status_code == 400, (r.status_code, r.text[:300])
    print("  [surfaces] API download as .mo, as_fmt=po as .po, other as_fmt 400 OK")
    out_dir = os.environ["LOC_TMS_DATA_DIR"]
    po_path = os.path.join(out_dir, "cli_out.po")
    rc, out = _run_cli(
        ["doc", "export", str(doc_id), "--locale", "de-DE", "--as-fmt", "po", "-o", po_path]
    )
    assert rc == 0, out
    with open(po_path, encoding="utf-8") as fh:
        assert 'msgstr "DE Open"' in fh.read()
    rc, out = _run_cli(["doc", "export", str(doc_id), "--locale", "de-DE", "--as-fmt", "xliff"])
    assert rc != 0, out
    print("  [surfaces] CLI doc export --as-fmt po writes the .po; an unknown value is refused OK")


def _run_cli(argv):
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue() + err.getvalue()


def main():
    init_db()
    test_registered()
    test_units_match_po()
    test_identity()
    test_translated_build_is_valid_mo()
    test_localized_plural_forms()
    test_one_form_target_uses_plural_682()
    test_cldr_only_targets_739()
    test_build_po()
    test_declared_charset()
    test_hostile()
    test_export_surfaces()
    print("MO FILTER OK")


if __name__ == "__main__":
    main()
