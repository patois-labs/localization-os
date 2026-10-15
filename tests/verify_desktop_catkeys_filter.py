"""Freedesktop .desktop and Haiku .catkeys filters (app/filters/desktop_filter.py,
app/filters/catkeys_filter.py): units, escaping, byte-faithful round trip, localized
build (Key[ll_CC]= lines; catkeys header language), records(), hostile input.

Hand-written fixtures only. Hermetic + offline. Final line: "DESKTOP CATKEYS FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_desktopcat_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import EmptyDocumentError, InputLimitError, UnsupportedEncodingError
from app.filters.catkeys_filter import catkeys_fingerprint, language_name
from app.filters.desktop_filter import posix_locale

D = get_filter("desktop")
C = get_filter("catkeys")

DESKTOP = (
    "# Launcher for the example editor\n"
    "[Desktop Entry]\n"
    "Type=Application\n"
    "Version=1.5\n"
    "# Shown in menus\n"
    "Name=Text Editor\n"
    "Name[de]=Texteditor\n"
    "GenericName = Editor\n"
    "Comment=Edit\\stext files;\\nquickly\\\\safely\n"
    "Keywords=text;editor;plain\\;simple;\n"
    "Exec=example-editor %F\n"
    "Icon=accessories-text-editor\n"
    "Categories=Utility;TextEditor;\n"
    "Actions=new-window;\n"
    "\n"
    "[Desktop Action new-window]\n"
    "Name=New Window\n"
    "Exec=example-editor --new-window\n"
    "\n"
    "[X-Vendor Settings]\n"
    "Name=Not a launcher string\n"
)

CATKEYS = (
    "1\tenglish\tx-vnd.Example-App\t123456789\n"
    "Open\tMainWindow\t\tOpen\n"
    "Save %s?\tMainWindow\tfile name\tSave %s?\n"
    "(\tCalcView\t\t(\n"
    'Line one\\nLine "two"\tAbout\t\tLine one\\nLine "two"\n'
    "Quit\tMainWindow\t\tBeenden\n"
)


def _texts(f, data: bytes):
    return [u.text for u in f.parse(data).units]


def _refused(f, data: bytes, exc=ValueError, needle: str = "") -> str:
    try:
        f.parse(data)
    except exc as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return str(e)
    raise AssertionError(f"expected refusal for {data[:60]!r}")


def test_registration():
    for fmt in ("desktop", "catkeys"):
        assert fmt in supported_exts()
        assert get_filter(fmt).fmt == fmt
        from app.services.connectors import msgraph
        from app.services.mime import MIME_BY_FMT

        assert msgraph.FORMATS.get(fmt) == fmt
        assert fmt in MIME_BY_FMT
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert ".catkeys," in html and ".desktop," in html
    print("  [reg] desktop + catkeys registered, accept list, msgraph and MIME maps OK")


# ---------------------------------------------------------------- .desktop


def test_desktop_units_and_escapes():
    data = DESKTOP.encode("utf-8")
    texts = _texts(D, data)
    want = [
        "Text Editor",
        "Editor",
        "Edit text files;\nquickly\\safely",
        "text",
        "editor",
        "plain;simple",
        "New Window",
    ]
    assert texts == want, texts
    assert D.build(data, {}) == data
    assert D.build(data, {str(i): t for i, t in enumerate(texts)}) == data
    print("  [desktop] Name/GenericName/Comment/Keywords items, spec unescape, skips OK")


def test_desktop_plain_build():
    data = DESKTOP.encode("utf-8")
    out = D.build(data, {"2": " Texte;\tmodifier ", "5": "brut;simple"}).decode("utf-8")
    assert "Comment=\\sTexte;\\tmodifier\\s\n" in out, out
    assert "Keywords=text;editor;brut\\;simple;\n" in out, out
    assert "Name[de]=Texteditor\n" in out and "Exec=example-editor %F\n" in out
    assert _texts(D, out.encode("utf-8"))[2] == " Texte;\tmodifier "
    print("  [desktop] build() splices escaped values in place (\\s edges, \\; in lists) OK")


def test_desktop_localized_build():
    data = DESKTOP.encode("utf-8")
    tr = {
        "0": "Éditeur de texte",
        "1": "Éditeur",
        "2": "Modifier;\nvite",
        "3": "texte",
        "4": "éditeur",
        "6": "Nouvelle fenêtre",
    }
    out = D.build_localized(data, tr, "fr-FR").decode("utf-8")
    exp = (
        DESKTOP.replace("Name=Text Editor\n", "Name=Text Editor\nName[fr_FR]=Éditeur de texte\n")
        .replace("GenericName = Editor\n", "GenericName = Editor\nGenericName[fr_FR]=Éditeur\n")
        .replace("quickly\\\\safely\n", "quickly\\\\safely\nComment[fr_FR]=Modifier;\\nvite\n")
        .replace(
            "Keywords=text;editor;plain\\;simple;\n",
            "Keywords=text;editor;plain\\;simple;\nKeywords[fr_FR]=texte;éditeur;plain\\;simple;\n",
        )
        .replace("Name=New Window\n", "Name=New Window\nName[fr_FR]=Nouvelle fenêtre\n")
    )
    assert out == exp, out
    # the vendor group is untouched; source lines kept
    assert "Name=Not a launcher string\n" in out and "Name[fr_FR]=Not" not in out
    # an existing locale line is replaced in place, other locales stay
    out_de = D.build_localized(data, {"0": "Text-Editor"}, "de").decode("utf-8")
    assert out_de == DESKTOP.replace("Name[de]=Texteditor", "Name[de]=Text-Editor"), out_de
    # re-export over an already-localized file replaces, never duplicates
    again = D.build_localized(out.encode("utf-8"), {"0": "Éditeur"}, "fr-FR").decode("utf-8")
    assert again.count("\nName[fr_FR]=") == 2, again
    assert "Name=Text Editor\nName[fr_FR]=Éditeur\n" in again, again
    # localized lines are never units
    assert _texts(D, out.encode("utf-8")) == _texts(D, data)
    # no translations -> identical bytes
    assert D.build_localized(data, {}, "fr-FR") == data
    print("  [desktop] localized build adds/replaces Key[ll_CC]= after the source key OK")


def test_desktop_line_endings_and_bom():
    crlf = DESKTOP.replace("\n", "\r\n").encode("utf-8")
    assert _texts(D, crlf)[0] == "Text Editor"
    assert D.build(crlf, {}) == crlf
    out = D.build_localized(crlf, {"0": "Editor de texto"}, "es-419")
    assert b"Name=Text Editor\r\nName[es_419]=Editor de texto\r\n" in out, out
    # last line without a line break
    tail = b"[Desktop Entry]\nType=Application\nName=Clock"
    out = D.build_localized(tail, {"0": "Uhr"}, "de-DE")
    assert out == b"[Desktop Entry]\nType=Application\nName=Clock\nName[de_DE]=Uhr", out
    bom = b"\xef\xbb\xbf" + DESKTOP.encode("utf-8")
    assert D.build(bom, {}) == bom
    assert D.build(bom, {"0": "X"}).startswith(b"\xef\xbb\xbf# Launcher")
    print("  [desktop] CRLF, no final newline, BOM preserved OK")


def test_posix_locale():
    cases = {
        "fr-FR": "fr_FR",
        "de": "de",
        "sr-Latn": "sr@latin",
        "sr-Latn-RS": "sr_RS@latin",
        "sr-Cyrl": "sr@cyrillic",
        "zh-Hans": "zh_CN",
        "zh-Hant": "zh_TW",
        "zh-Hant-HK": "zh_HK",
        "pt_BR": "pt_BR",
        "ca-ES-valencia": "ca_ES@valencia",
        "es-419": "es_419",
    }
    for src, want in cases.items():
        assert posix_locale(src) == want, (src, posix_locale(src))
    print("  [desktop] BCP 47 -> POSIX locale keys OK")


def test_desktop_records():
    recs = D.records(DESKTOP.encode("utf-8"))
    from app.filters.kv_resource import KeyValueRecord  # gate: b1's type, no mirror

    assert recs and all(type(r) is KeyValueRecord for r in recs)
    keys = [r.key for r in recs]
    assert keys == [
        "Desktop Entry/Name",
        "Desktop Entry/GenericName",
        "Desktop Entry/Comment",
        "Desktop Entry/Keywords[0]",
        "Desktop Entry/Keywords[1]",
        "Desktop Entry/Keywords[2]",
        "Desktop Action new-window/Name",
    ], keys
    assert recs[0].comment == "Shown in menus" and recs[0].context == "Desktop Entry"
    assert [r.unit_index for r in recs] == list(range(7))
    assert recs[5].value == "plain;simple"
    print("  [desktop] records() view OK")


def test_desktop_hostile():
    _refused(D, b"Name=No group\n", ValueError, "Desktop Entry")
    _refused(D, b"[Desktop Entry]\nName=Caf\xe9\n", UnsupportedEncodingError, "UTF-8")
    _refused(D, b"[Desktop Entry]\nName=a\x00b\n", UnsupportedEncodingError)
    _refused(D, b"[Desktop Entry]\nType=Application\nExec=x\n", EmptyDocumentError)
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = ("[Desktop Entry]\nKeywords=" + ";".join(f"k{i}" for i in range(10)) + "\n").encode()
        _refused(D, many, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        _refused(D, b"[Desktop Entry]\n" + b"\n" * 100, InputLimitError, "lines")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    old = config.MAX_SEGMENT_CHARS
    try:
        config.MAX_SEGMENT_CHARS = 100
        _refused(D, b"[Desktop Entry]\nComment=" + b"w " * 200 + b"\n", InputLimitError)
    finally:
        config.MAX_SEGMENT_CHARS = old
    # a stray line without '=' and odd keys are kept byte for byte
    odd = b"[Desktop Entry]\nName=Ok\ngarbage line\nName[x y]=z\n"
    assert D.build(odd, {"0": "Gut"}) == odd.replace(b"Name=Ok", b"Name=Gut")
    print("  [desktop] hostile: no group, non-UTF-8, binary, empty, caps OK")


# ---------------------------------------------------------------- .catkeys


def test_catkeys_units_and_build():
    data = CATKEYS.encode("utf-8")
    texts = _texts(C, data)
    assert texts == ["Open", "Save %s?", 'Line one\nLine "two"', "Quit"], texts
    assert C.build(data, {}) == data
    # current translations given back -> identical
    same = {"0": "Open", "1": "Save %s?", "2": 'Line one\nLine "two"', "3": "Beenden"}
    assert C.build(data, same) == data
    tr = {"0": "Öffnen", "1": "%s speichern?", "2": 'Zeile eins\nZeile "zwei"\tTab', "3": "Beenden"}
    out = C.build(data, tr).decode("utf-8")
    assert "Open\tMainWindow\t\tÖffnen\n" in out
    assert "Save %s?\tMainWindow\tfile name\t%s speichern?\n" in out
    assert 'Line one\\nLine "two"\tAbout\t\tZeile eins\\nZeile \\"zwei\\"\\tTab\n' in out, out
    assert out.startswith("1\tenglish\tx-vnd.Example-App\t123456789\n")  # build(): header kept
    assert "(\tCalcView\t\t(\n" in out and "Quit\tMainWindow\t\tBeenden\n" in out
    assert _texts(C, out.encode("utf-8")) == texts  # source column never changes
    print("  [catkeys] source = unit, translation column filled, Haiku escaping OK")


def test_catkeys_localized_header():
    data = CATKEYS.encode("utf-8")
    out = C.build_localized(data, {"0": "Öffnen"}, "de-DE").decode("utf-8")
    assert out.startswith("1\tgerman (germany)\tx-vnd.Example-App\t123456789\n"), out[:80]
    out = C.build_localized(data, {}, "pt-BR").decode("utf-8")
    assert out.startswith("1\tportuguese (brazil)\t"), out[:60]
    assert language_name("de") == "german" and language_name("xx-!!") == "xx-!!"
    assert C.build_localized(data, {}, "en") == data  # already english: nothing to change
    print("  [catkeys] localized build sets the header language, keeps fingerprint OK")


def test_catkeys_fingerprint():
    # Worked example of Haiku HashFun (h = 5h + signed byte, then 5h + 1), chained over
    # source, context, comment: "A" -> 5*0+65=65 -> 326; context "" -> 5*326+1 = 1631;
    # comment "" -> 5*1631+1 = 8156.
    one = b"1\tenglish\tx-vnd.T\t8156\nA\t\t\tA\n"
    assert catkeys_fingerprint(one) == (8156, 8156)
    # a non-ASCII byte counts as a SIGNED char: "\xc3\xa9" (e acute) -> 5*0-61 = -61,
    # 5*(-61)-87 = -392, 5*(-392)+1 = -1959 -> (mod 2^32) then the two empty fields
    h = (-1959) & 0xFFFFFFFF
    h = (5 * h + 1) & 0xFFFFFFFF
    h = (5 * h + 1) & 0xFFFFFFFF
    two = ("1\tenglish\tx\t0\n\u00e9\t\t\tx\nA\t\t\tA\n").encode("utf-8")
    assert catkeys_fingerprint(two)[1] == (h + 8156) & 0xFFFFFFFF
    # escapes are hashed unescaped (as Haiku's catalog does): "\n" in the file is a newline
    esc = b"1\tenglish\tx\t0\nA\\n\t\t\t\n"
    raw_nl = (((65 * 5 + 10) * 5 + 1) * 5 + 1) * 5 + 1
    assert catkeys_fingerprint(esc)[1] == raw_nl
    print("  [catkeys] fingerprint (Haiku ComputeFingerprint) recomputed OK")


def test_catkeys_records():
    recs = C.records(CATKEYS.encode("utf-8"))
    from app.filters.kv_resource import KeyValueRecord  # gate: b1's type, no mirror

    assert recs and all(type(r) is KeyValueRecord for r in recs)
    assert [r.key for r in recs] == ["Open", "Save %s?", "(", 'Line one\nLine "two"', "Quit"]
    assert recs[1].comment == "file name" and recs[1].context == "MainWindow"
    assert recs[4].value == "Beenden"
    assert [r.unit_index for r in recs] == [0, 1, None, 2, 3]
    dup = (CATKEYS + "Open\tMainWindow\t\tAuf\nOpen\tOther\t\tAuf\n").encode("utf-8")
    flags = [r.duplicate for r in C.records(dup)]
    assert flags[-2:] == [True, False], flags
    print("  [catkeys] records() view (source, context, comment, translation) OK")


def test_catkeys_crlf_bom_and_hostile():
    crlf = CATKEYS.replace("\n", "\r\n").encode("utf-8")
    assert _texts(C, crlf)[0] == "Open"
    assert C.build(crlf, {}) == crlf
    out = C.build(crlf, {"0": "Ouvrir"})
    assert b"Open\tMainWindow\t\tOuvrir\r\n" in out, out
    bom = b"\xef\xbb\xbf" + CATKEYS.encode("utf-8")
    assert C.build(bom, {"0": "Ouvrir"}).startswith(b"\xef\xbb\xbf1\t")
    _refused(C, b"2\tenglish\tx\t1\nA\t\t\tA\n", ValueError, "first line")
    _refused(C, b"1\tenglish\tx\tabc\nA\t\t\tA\n", ValueError, "fingerprint")
    _refused(C, b"1\tenglish\tx\t1\nA\tonly two\n", ValueError, "four")
    _refused(C, b"1\tenglish\tx\t1\nCaf\xe9\t\t\t\n", UnsupportedEncodingError, "UTF-8")
    _refused(C, b"", ValueError, "empty")
    _refused(C, b"1\tenglish\tx\t1\n(\t\t\t(\n", EmptyDocumentError)
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = "1\tenglish\tx\t1\n" + "".join(f"S{i}\t\t\t\n" for i in range(10))
        _refused(C, many.encode(), InputLimitError, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [catkeys] CRLF, BOM, bad header, short line, non-UTF-8, caps refused OK")


def test_api_and_cli_round_trip():
    """Upload both through the API and the CLI, mock-translate, export."""
    import re
    import tempfile

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    loc = "de-DE"
    init_db()
    with Session(engine) as s:
        p = Project(name="desktopcat", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id
    user = security.create_user("dc@x.test", "dc", Role.ADMIN, "desktopcat-pw-123456")
    _row, raw_key = apikeys.create_key(user.id, "dc", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw_key}  # pregate: allow-secret throwaway test key

    def check_desktop(out: bytes, surface: str):
        text = out.decode("utf-8")
        assert f"Name=Text Editor\nName[de_DE]=[{loc}] Text Editor\n" in text, (surface, text)
        assert "Name[de]=Texteditor\n" in text and "Exec=example-editor %F\n" in text

    def check_catkeys(out: bytes, surface: str):
        text = out.decode("utf-8")
        assert text.startswith("1\tgerman (germany)\tx-vnd.Example-App\t123456789\n"), text
        assert f"Open\tMainWindow\t\t[{loc}] Open\n" in text, (surface, text)

    files = (
        ("org.example.Editor.desktop", DESKTOP.encode("utf-8"), check_desktop, "desktop"),
        ("en.catkeys", CATKEYS.encode("utf-8"), check_catkeys, "catkeys"),
    )
    for name, data, check, fmt in files:
        r = client.post(
            "/api/documents",
            headers=hdr,
            data={"project_id": str(pid)},
            files={"file": (name, data, "text/plain")},
        )
        assert r.status_code in (200, 201), (r.status_code, r.text)
        doc = r.json()["document_id"]
        code, out, err = run_cli(["doc", "translate", str(doc), "--locale", loc])
        assert code == 0, (code, out, err)
        r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": loc})
        assert r.status_code == 200, (r.status_code, r.text)
        check(r.content, "api")

        tmp = tempfile.mkdtemp(prefix="loctms_desktopcat_cli_")
        src = os.path.join(tmp, name)
        with open(src, "wb") as fh:
            fh.write(data)
        code, out, err = run_cli(["doc", "upload", str(pid), src])
        assert code == 0, (code, out, err)
        m = re.search(r"Uploaded #(\d+): " + re.escape(name) + r" \(" + fmt + ",", out)
        assert m, out
        cli_doc = int(m.group(1))
        code, out, err = run_cli(["doc", "translate", str(cli_doc), "--locale", loc])
        assert code == 0, (code, out, err)
        dest = os.path.join(tmp, "out." + fmt)
        code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", loc, "-o", dest])
        assert code == 0, (code, out, err)
        with open(dest, "rb") as fh:
            check(fh.read(), "cli")
    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    exts = {f["ext"] for f in r.json()["formats"]}
    assert {"desktop", "catkeys"} <= exts, r.text
    print("  [surfaces] API upload/download and CLI upload/export for both formats OK")


def main():
    test_registration()
    test_desktop_units_and_escapes()
    test_desktop_plain_build()
    test_desktop_localized_build()
    test_desktop_line_endings_and_bom()
    test_posix_locale()
    test_desktop_records()
    test_desktop_hostile()
    test_catkeys_units_and_build()
    test_catkeys_localized_header()
    test_catkeys_fingerprint()
    test_catkeys_records()
    test_catkeys_crlf_bom_and_hostile()
    test_api_and_cli_round_trip()
    print("DESKTOP CATKEYS FILTER OK")


if __name__ == "__main__":
    main()
