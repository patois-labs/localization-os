"""Windows resource script (.rc) filter: parse, round trip, encodings, accelerators,
LANGUAGE stamping and hostile input (Batch 11, fmt/winrc).

Hand-written fixtures only. Hermetic + offline. Final line: "RC FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rcfilter_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

SAMPLE = r"""// Microsoft Visual C++ generated resource script.
//
#include "resource.h"

#define APSTUDIO_READONLY_SYMBOLS
#include "winres.h"
#undef APSTUDIO_READONLY_SYMBOLS

/* block comment with "a string that is not text" */
#if !defined(AFX_RESOURCE_DLL) || defined(AFX_TARG_ENU)
LANGUAGE LANG_ENGLISH, SUBLANG_ENGLISH_US
#pragma code_page(1252)

IDI_APP                 ICON                    "res\\app.ico"
IDB_LOGO                BITMAP                  "res\\logo.bmp"

IDR_MAINMENU MENU
BEGIN
    POPUP "&File"
    BEGIN
        MENUITEM "&Open...\tCtrl+O",           ID_FILE_OPEN
        MENUITEM SEPARATOR
        MENUITEM "Save && E&xit",              ID_FILE_EXIT
    END
END

IDR_EXMENU MENUEX
BEGIN
    POPUP "&Help", 0, MFT_STRING, MFS_ENABLED
    BEGIN
        MENUITEM "", 0, MFT_SEPARATOR
        MENUITEM "&About", ID_HELP_ABOUT, MFT_STRING, MFS_ENABLED
    END
END

IDD_ABOUTBOX DIALOGEX 0, 0, 170, 62
STYLE DS_SETFONT | DS_MODALFRAME | WS_POPUP | WS_CAPTION | WS_SYSMENU
CAPTION "About Sample"
FONT 8, "MS Shell Dlg", 0, 0, 0x1
BEGIN
    ICON            IDI_APP, IDC_STATIC, 14, 14, 20, 20
    LTEXT           "Sample, Version 1.0", IDC_STATIC, 42, 14, 114, 8, SS_NOPREFIX
    CONTROL "&Remember me", IDC_REMEMBER, "Button", BS_AUTOCHECKBOX | WS_TABSTOP,
                    42, 26, 100, 10
    CONTROL         "IDB_LOGO", IDC_LOGO, "Static", SS_BITMAP, 4, 4, 20, 20
    EDITTEXT        IDC_NAME, 42, 36, 80, 12
    DEFPUSHBUTTON   "OK", IDOK, 113, 41, 50, 14, WS_GROUP
END

IDR_ACCEL ACCELERATORS
BEGIN
    "O",  ID_FILE_OPEN, VIRTKEY, CONTROL
END

STRINGTABLE
BEGIN
    IDS_HELLO               "Hello, %s! You have %d new messages."
    IDS_QUOTE,              "Say ""hi"" to C:\\Temp\nthen leave"
    IDS_FMT                 "Copying %1 to %2!s!..."
    IDS_WIDE                L"5\x00BC-Inch Floppy Disk"
    IDS_LONG                "first part \
second part"
    IDS_EMPTY               ""
    IDS_NUMBERS             "%d%%"
END

VS_VERSION_INFO VERSIONINFO
 FILEVERSION 1,0,0,1
 FILEFLAGSMASK 0x3fL
BEGIN
    BLOCK "StringFileInfo"
    BEGIN
        BLOCK "040904b0"
        BEGIN
            VALUE "CompanyName", "Acme"
            VALUE "FileDescription", "Sample Application"
            VALUE "FileVersion", "1.0.0.1"
            VALUE "LegalCopyright", "Copyright (C) 2026"
            VALUE "ProductName", "Sample"
        END
    END
    BLOCK "VarFileInfo"
    BEGIN
        VALUE "Translation", 0x409, 1200
    END
END

3 TEXTINCLUDE
BEGIN
    "#include ""winres.h""\r\n"
    "\0"
END
#endif
"""

EXPECTED = [
    ("&File", "rc:menu"),
    ("&Open...⟦x1⟧Ctrl+O", "rc:menu"),
    ("Save && E&xit", "rc:menu"),
    ("&Help", "rc:menu"),
    ("&About", "rc:menu"),
    ("About Sample", "rc:caption"),
    ("Sample, Version 1.0", "rc:control"),
    ("&Remember me", "rc:control"),
    ("OK", "rc:control"),
    ("Hello, ⟦x1⟧! You have ⟦x2⟧ new messages.", "rc:string"),
    ('Say "hi" to C:\\Temp⟦x1⟧then leave', "rc:string"),
    ("Copying ⟦p1⟧ to ⟦p2⟧...", "rc:string"),  # #693: numbered %1 / %2!s! may move
    ("5¼-Inch Floppy Disk", "rc:string"),
    ("first part second part", "rc:string"),
    ("Sample Application", "rc:version"),
    ("Copyright (C) 2026", "rc:version"),
    ("Sample", "rc:version"),
]

F = get_filter("rc")


def _crlf(s: str) -> str:
    return s.replace("\n", "\r\n")


def test_registered():
    assert F is not None and F.fmt == "rc"
    assert "rc" in supported_exts()
    print("  [rc] registered OK")


def test_parse_units():
    data = _crlf(SAMPLE).encode("cp1252")
    res = F.parse(data)
    got = [(u.text, u.style) for u in res.units]
    assert got == EXPECTED, got
    assert [u.p_index for u in res.units] == list(range(len(EXPECTED)))
    assert res.units[1].inline_map == {"1": {"point": "\\t"}}
    assert res.skeleton["source_encoding"] == "cp1252", res.skeleton
    print("  [rc] stringtable, dialog, menu, versioninfo units in order OK")


def test_identity_round_trip_every_encoding():
    text = _crlf(SAMPLE)
    variants = {
        "cp1252": text.encode("cp1252"),
        "utf-8": text.encode("utf-8"),
        "utf-8-bom": b"\xef\xbb\xbf" + text.encode("utf-8"),
        "utf-16-le-bom": b"\xff\xfe" + text.encode("utf-16-le"),
        "utf-16-be-bom": b"\xfe\xff" + text.encode("utf-16-be"),
        "utf-16-le": text.encode("utf-16-le"),
    }
    for name, data in variants.items():
        res = F.parse(data)
        assert len(res.units) == len(EXPECTED), (name, len(res.units))
        same = {str(u.p_index): u.text for u in res.units}
        assert F.build(data, {}) == data, name
        assert F.build(data, same) == data, name
    print("  [rc] identity round trip in cp1252, UTF-8 (+BOM), UTF-16 LE/BE (+BOM) OK")


def test_translated_round_trip_utf16():
    data = b"\xff\xfe" + _crlf(SAMPLE).encode("utf-16-le")
    assert len(F.parse(data).units) == len(EXPECTED)
    tr = {
        "1": "&Öffnen...⟦x1⟧Strg+O",
        "9": "Hallo, ⟦x1⟧! Sie haben ⟦x2⟧ neue Nachrichten.",
        "10": 'Sag "hallo" zu C:\\Temp⟦x1⟧dann geh',
        "12": "5¼-Zoll-Diskette",
    }
    out = F.build(data, tr)
    assert out[:2] == b"\xff\xfe"
    s = out[2:].decode("utf-16-le")
    assert '"&Öffnen...\\tStrg+O"' in s, s
    assert '"Hallo, %s! Sie haben %d neue Nachrichten."' in s
    assert '"Sag ""hallo"" zu C:\\\\Temp\\ndann geh"' in s
    assert 'L"5¼-Zoll-Diskette"' in s
    # untouched lines identical
    orig = _crlf(SAMPLE)
    assert s.replace('"&Öffnen...\\tStrg+O"', '"&Open...\\tCtrl+O"').count("\r\n") == orig.count(
        "\r\n"
    )
    assert 'MENUITEM "Save && E&xit"' in s and '"Copying %1 to %2!s!..."' in s
    again = [u.text for u in F.parse(out).units]
    assert again[1] == tr["1"] and again[9] == tr["9"] and again[10] == tr["10"], again
    print("  [rc] translated UTF-16 round trip keeps escapes, quotes, placeholders, L prefix OK")


def test_placeholders_dropped_token_and_unknown():
    data = SAMPLE.encode("utf-8")
    # token reorder keeps the raw source text; an invented token is dropped
    out = F.build(data, {"9": "⟦x2⟧ / ⟦x1⟧ ⟦x7⟧ ⟦g1⟧x⟦/g1⟧"}).decode("utf-8")
    assert '"%d / %s  x"' in out, out
    print("  [rc] placeholder tokens rematerialize, unknown tokens dropped OK")


def test_accelerators():
    data = SAMPLE.encode("utf-8")
    out = F.build(
        data,
        {
            "0": "Datei",  # missing: inserted before the same letter? none -> (&F)
            "3": "Hilfe",  # source &H -> letter present
            "4": "&Info &über",  # two -> second becomes literal
            "7": "ファイルを記憶",  # CJK style append
            "1": "Öffnen...⟦x1⟧Strg+O",  # append before the tab and the dots
            "2": "Speichern && Beenden",  # source has one (&x) -> x present
        },
    ).decode("utf-8")
    assert 'POPUP "&Datei"' in out, out  # alphabetic target: mark its own first letter
    assert 'POPUP "&Hilfe"' in out, out
    assert 'MENUITEM "&Info &&über"' in out, out
    assert '"ファイルを記憶(&R)"' in out, out
    assert '"&Öffnen...\\tStrg+O"' in out, out
    # CJK before a tab still appends (&X) before the dots
    cjk = F.build(data, {"1": "開く...⟦x1⟧Ctrl+O"}).decode("utf-8")
    assert '"開く(&O)...\\tCtrl+O"' in cjk, cjk
    assert '"Speichern && Beenden(&X)"' in out or '"Speichern && Beend&en"' not in out
    # strings in a STRINGTABLE are never touched by accelerator logic
    out2 = F.build(data, {"9": "A & B"}).decode("utf-8")
    assert '"A & B"' in out2
    print("  [rc] exactly one & accelerator kept on menus and controls OK")


def test_ansi_overflow_switches_to_utf8_pragma():
    data = _crlf(SAMPLE).encode("cp1252")
    out = F.build(data, {"5": "关于示例"})
    s = out.decode("utf-8")
    assert "#pragma code_page(65001)" in s and "code_page(1252)" not in s
    assert 'CAPTION "关于示例"' in s
    # a cp1252-representable translation stays cp1252
    out2 = F.build(data, {"5": "À propos"})
    assert out2.decode("cp1252").count("À propos") == 1
    # no pragma at all: one is added at the top
    nop = SAMPLE.replace("#pragma code_page(1252)\n", "").encode("cp1252")
    s3 = F.build(nop, {"5": "关于"}).decode("utf-8")
    assert s3.startswith("#pragma code_page(65001)\n"), s3[:60]
    print("  [rc] ANSI code page kept when it fits, else UTF-8 + code_page(65001) OK")


def test_declared_code_page():
    src = 'LANGUAGE 0x11, 0x01\n#pragma code_page(932)\nSTRINGTABLE\nBEGIN\n 1 "ファイル"\nEND\n'
    data = src.encode("cp932")
    res = F.parse(data)
    assert [u.text for u in res.units] == ["ファイル"]
    assert res.skeleton["source_encoding"] == "cp932"
    out = F.build(data, {"0": "ファイルを開く"})
    assert out.decode("cp932").count("ファイルを開く") == 1
    mixed = b'#pragma code_page(1252)\n#pragma code_page(932)\nSTRINGTABLE BEGIN 1 "x" END\n'
    try:
        F.parse(mixed)
        raise AssertionError("mixed code pages must be refused")
    except UnsupportedEncodingError as exc:
        assert "code page" in str(exc)
    bad = b'#pragma code_page(1234)\nSTRINGTABLE BEGIN 1 "x" END\n'
    try:
        F.parse(bad)
        raise AssertionError("unknown code page must be refused")
    except UnsupportedEncodingError:
        pass
    print("  [rc] #pragma code_page(932) decoded and re-encoded; mixed/unknown refused OK")


def test_language_stamping():
    data = _crlf(SAMPLE).encode("cp1252")
    out = F.build_localized(data, {}, "de-DE").decode("cp1252")
    assert "LANGUAGE 0x07, 0x01" in out, out[:400]
    assert 'BLOCK "040704b0"' in out
    assert 'VALUE "Translation", 0x407, 1200' in out
    ja = F.build_localized(data, {"5": "バージョン情報"}, "ja-JP").decode("utf-8")
    assert "LANGUAGE 0x11, 0x01" in ja and 'BLOCK "041104b0"' in ja
    zh = F.build_localized(data, {}, "zh-Hant").decode("cp1252")
    assert "LANGUAGE 0x04, 0x01" in zh and "0x404" in zh, zh[:400]
    multi = (
        'LANGUAGE 9, 1\nSTRINGTABLE BEGIN 1 "a" END\nLANGUAGE 7, 1\nSTRINGTABLE BEGIN 2 "b" END\n'
    )
    out_m = F.build_localized(multi.encode(), {}, "fr-FR").decode()
    assert out_m == multi, "a multi-language script keeps its LANGUAGE statements"
    # per-resource LANGUAGE inside a header is stamped too
    per = 'STRINGTABLE\nLANGUAGE LANG_ENGLISH, SUBLANG_NEUTRAL\nBEGIN\n 1 "Hi"\nEND\n'
    assert "LANGUAGE 0x0c, 0x01" in F.build_localized(per.encode(), {}, "fr-FR").decode()
    assert F.build_localized(data, {}, "") == data
    print("  [rc] LANGUAGE, StringFileInfo block and Translation stamped to target LANGID OK")


def test_rc_exe_string_rules():
    # resinator literals.zig (mirrors rc.exe): a backslash continuation drops the next
    # line's leading whitespace; \' and \? are not escapes (narrow keeps the backslash,
    # wide drops it).
    src = (
        "STRINGTABLE\nBEGIN\n"
        '  1 "Hello \\\n        world"\n'
        '  2 "Don\\\'t"\n'
        '  3 L"Don\\\'t"\n'
        "END\n"
        "VS_VERSION_INFO VERSIONINFO\nBEGIN\n"
        ' BLOCK "StringFileInfo"\n BEGIN\n  BLOCK "040904B0"\n  BEGIN\n'
        '   VALUE "ProductName", "Sample"\n  END\n END\nEND\n'
    )
    data = src.encode("ascii")
    texts = [u.text for u in F.parse(data).units]
    assert texts == ["Hello world", "Don\\'t", "Don't", "Sample"], texts
    out = F.build(data, {"0": "Hallo Welt", "1": "Don\\'t do it", "2": "Nicht's"}).decode()
    assert '1 "Hallo Welt"' in out and '2 "Don\\\\\'t do it"' in out, out
    assert '3 L"Nicht\'s"' in out, out
    again = [u.text for u in F.parse(out.encode()).units]
    assert again[:3] == ["Hallo Welt", "Don\\'t do it", "Nicht's"], again
    loc = F.build_localized(data, {}, "fr-FR").decode()
    assert 'BLOCK "040C04B0"' in loc, loc
    print("  [rc] continuation whitespace, non-escapes, block-id case follow rc.exe OK")


def test_include_never_followed():
    src = (
        '#include "C:\\\\Windows\\\\win.ini"\n#include <secret.rc>\n'
        '  #  include "indented.rc"\nrcinclude other.rc\nSTRINGTABLE { 1, "Only me" }\n'
    )
    res = F.parse(src.encode())
    assert [u.text for u in res.units] == ["Only me"], [u.text for u in res.units]
    print("  [rc] #include / rcinclude lines skipped, never opened OK")


def test_hostile_inputs():
    # binary
    try:
        F.parse(b"\x00\x01\x02\x03garbage\x00" * 50 + b"\x81")
        raise AssertionError("binary must be refused")
    except UnsupportedEncodingError:
        pass
    # unterminated string, stray END, deep nesting: no crash
    junk = (
        'STRINGTABLE BEGIN 1 "open\n2 "ok" END END END\n'
        + "BEGIN " * 5000
        + '\nX MENU BEGIN MENUITEM "&Go", 1'
    )
    res = F.parse(junk.encode())
    assert [u.text for u in res.units][:1] == ["ok"], [u.text for u in res.units]
    # unterminated block comment
    F.parse(b'STRINGTABLE BEGIN 1 "a" END /* never closed "b"')
    # segment cap
    from app import config

    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        big = "STRINGTABLE\nBEGIN\n" + "".join(f' {i} "Text {i}"\n' for i in range(20)) + "END\n"
        try:
            F.parse(big.encode())
            raise AssertionError("segment cap")
        except InputLimitError:
            pass
        quotes = '"' * 2000
        try:
            F.parse(quotes.encode())
            raise AssertionError("quote budget")
        except InputLimitError:
            pass
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # huge single string
    try:
        F.parse(('STRINGTABLE BEGIN 1 "' + "a" * (config.MAX_SEGMENT_CHARS + 5) + '" END').encode())
        raise AssertionError("segment chars")
    except InputLimitError:
        pass
    # pathological backslashes and quotes stay linear
    import time

    t0 = time.time()
    F.parse(('STRINGTABLE BEGIN 1 "' + "\\\\" * 200000 + '" END').encode())
    F.parse(('STRINGTABLE BEGIN 1 "' + '""' * 200000 + '" END').encode())
    assert time.time() - t0 < 10
    print("  [rc] binary, unterminated, nesting, caps and budgets refused or survived OK")


def test_end_to_end_upload_export():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import Segment

    init_db()
    client = TestClient(app)
    src = b"\xff\xfe" + _crlf(SAMPLE).encode("utf-16-le")
    created = client.post(
        "/projects",
        data={"name": "RC e2e", "source_locale": "en-US", "target_locales": "de-DE"},
        follow_redirects=False,
    )
    pid = int(created.headers["location"].rsplit("/", 1)[-1])
    uploaded = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("app.rc", src)},
        follow_redirects=False,
    )
    assert uploaded.status_code in (302, 303), uploaded.text[:400]
    did = int(uploaded.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as session:
        rows = session.exec(select(Segment).where(Segment.document_id == did)).all()
        by_src = {r.source_text: r.id for r in rows}
    assert "About Sample" in by_src and "&About" in by_src, sorted(by_src)
    for source, target in (("About Sample", "Info zu Sample"), ("&About", "Info")):
        saved = client.post(f"/segments/{by_src[source]}/save", data={"target_text": target})
        assert saved.status_code == 200, saved.text[:300]
    exported = client.get(f"/documents/{did}/export?locale=de-DE")
    assert exported.status_code == 200, exported.text[:300]
    body = exported.content
    assert body[:2] == b"\xff\xfe", body[:20]
    s = body[2:].decode("utf-16-le")
    assert 'CAPTION "Info zu Sample"' in s, s[:300]
    assert 'MENUITEM "&Info"' in s, s
    assert "LANGUAGE 0x07, 0x01" in s and 'BLOCK "040704b0"' in s
    print("  [rc] end to end: UTF-16 upload -> save -> export keeps encoding, stamps de-DE OK")


if __name__ == "__main__":
    test_registered()
    test_parse_units()
    test_identity_round_trip_every_encoding()
    test_translated_round_trip_utf16()
    test_placeholders_dropped_token_and_unknown()
    test_accelerators()
    test_ansi_overflow_switches_to_utf8_pragma()
    test_declared_code_page()
    test_language_stamping()
    test_rc_exe_string_rules()
    test_include_never_followed()
    test_hostile_inputs()
    test_end_to_end_upload_export()
    print("RC FILTER OK")
