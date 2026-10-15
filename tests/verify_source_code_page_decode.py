"""Batch B-encoding (the maintainer's decision 22, 2026-10-11).

A text file with no declared encoding (subtitles, plain text, CSV/TSV, INI and the other text
formats on the shared #509 decode) is read: BOM / UTF-8 first, then a charset the file declares,
then the Windows code page of the project's SOURCE language (Greek -> Windows-1253, Russian ->
Windows-1251, Czech -> Windows-1250, Japanese -> Shift_JIS, ...; Western -> Windows-1252).
The upload WARNS (upload JSON `file_warnings`, document page, CLI `doc upload`, MCP
`submit_translation`) when the decoded letters look wrong. Export reads the stored original
in the code page the import used (the skeleton's `text_code_page`); a document imported
before the rule (no stamp) is read exactly as before.

Cases: a Greek Windows-1253 SRT, a Czech Windows-1250 CSV, a Russian Windows-1251 INI and a
Japanese Shift_JIS text file in projects of those source languages decode right with no
warning and export right; a Western (Windows-1252) file in a Greek-source project still
decodes right, with a warning; a Greek file in an English-source project warns; a pre-rule
document is unaffected; repair keeps the code page; HTML Help without a .hhp uses the
source language; a Russian .properties (ISO-8859-1 by spec) warns.

Hermetic and offline: throwaway LOC_TMS_DATA_DIR, mock provider, ASCII-only prints.
Final line: "SOURCE CODE PAGE DECODE OK".
"""

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bencoding_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Document, Project, Segment, TargetLocale

init_db()

PASSWORD_FIXTURE = "bencoding-fixture-pw-12345"  # pregate: allow-secret test fixture user
KIND = "text_encoding"

EL_LINES = ["Καλημέρα σε όλους.", "Η ταινία αρχίζει τώρα."]
SRT_EL = (
    f"1\n00:00:01,000 --> 00:00:03,000\n{EL_LINES[0]}\n\n"
    f"2\n00:00:04,000 --> 00:00:06,000\n{EL_LINES[1]}\n"
).encode("cp1253")
CS_TEXT = "Příliš žluťoučký kůň úpěl ďábelské ódy"
CSV_CS = f"id,text\n1,{CS_TEXT}\n2,Ťukání na dveře\n".encode("cp1250")
RU_TEXT = "Добро пожаловать"
INI_RU = f"[strings]\ntitle={RU_TEXT}\nbutton=Сохранить\n".encode("cp1251")
JA_TEXT = "こんにちは世界。"
TXT_JA = f"{JA_TEXT}\n\nテストのファイルです。\n".encode("cp932")
WEST_TEXT = "Café crème brûlée, déjà vu."
TXT_WEST = f"{WEST_TEXT}\n\nNaïve résumé.\n".encode("cp1252")

_N = [0]


def _project(s, source, targets=("fr-FR",)):
    _N[0] += 1
    p = Project(
        name=f"bencoding-{_N[0]}-{source}",
        org_id=1,
        source_locale=source,
        default_provider="mock",
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    for t in targets:
        s.add(TargetLocale(project_id=p.id, locale=t))
    s.commit()
    return p


def _sources(s, doc_id):
    rows = s.exec(select(Segment.source_text).where(Segment.document_id == doc_id)).all()
    return " ".join(rows)


def _warnings(doc):
    return [w for w in (doc.skeleton or {}).get("file_warnings") or [] if w.get("kind") == KIND]


def _translate_all(s, doc_id, prefix="X "):
    for g in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
        g.target_text = prefix + g.source_text
        g.status = "translated"
        s.add(g)
    s.commit()


def _decode_export(out: bytes, legacy: str) -> str:
    """The export is UTF-8 today; once byte-faithful exports (B-bytes) land it is the
    original code page when the text fits. Either way it must read back right."""
    try:
        return out.decode("utf-8-sig")
    except UnicodeDecodeError:
        return out.decode(legacy)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _ingest_export(s, source, name, data, legacy, expect):
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    p = _project(s, source)
    doc = ingest_document(s, p, name, data, "segmented")
    got = _sources(s, doc.id)
    for want in expect:
        assert want in got, (name, ascii(got[:200]))
    assert not _warnings(doc), (name, _warnings(doc))
    assert doc.skeleton.get("text_code_page") == legacy, (name, doc.skeleton.get("text_code_page"))
    _translate_all(s, doc.id)
    out = build_translated_file(s, doc, "fr-FR")
    text = _decode_export(out, legacy)
    import app.filters.base as fbase

    if hasattr(fbase, "encode_like"):
        # With byte-faithful exports (B-bytes) the source-language code page IS the file's
        # original encoding: a translation that fits comes back in it, not as UTF-8.
        assert out.decode(legacy) and not _is_utf8(out), (name, out[:120])
    for want in expect:
        assert "X " + want in text or want in text, (name, ascii(text[:300]))
    assert "X " in text, (name, ascii(text[:300]))
    return doc


# --------------------------------------------------------------------------- the four languages
def test_greek_srt_cp1253():
    with Session(engine) as s:
        _ingest_export(s, "el-GR", "film.srt", SRT_EL, "cp1253", EL_LINES)
    print("  [el] Windows-1253 SRT in a Greek project: decoded, no warning, export OK")


def test_czech_csv_cp1250():
    with Session(engine) as s:
        _ingest_export(s, "cs-CZ", "strings.csv", CSV_CS, "cp1250", [CS_TEXT, "Ťukání"])
    print("  [cs] Windows-1250 CSV in a Czech project: decoded, no warning, export OK")


def test_russian_ini_cp1251():
    with Session(engine) as s:
        _ingest_export(s, "ru-RU", "app.ini", INI_RU, "cp1251", [RU_TEXT, "Сохранить"])
    print("  [ru] Windows-1251 INI in a Russian project: decoded, no warning, export OK")


def test_japanese_txt_shift_jis():
    with Session(engine) as s:
        _ingest_export(s, "ja-JP", "notes.txt", TXT_JA, "cp932", [JA_TEXT, "テストのファイル"])
    print("  [ja] Shift_JIS text in a Japanese project: decoded, no warning, export OK")


def test_western_file_in_greek_project():
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    with Session(engine) as s:
        p = _project(s, "el-GR")
        doc = ingest_document(s, p, "west.txt", TXT_WEST, "segmented")
        got = _sources(s, doc.id)
        assert WEST_TEXT in got and "Naïve résumé." in got, ascii(got)
        warns = _warnings(doc)
        assert len(warns) == 1, warns
        msg = warns[0]["message"]
        assert "Greek (Windows-1253)" in msg and "Western European (Windows-1252)" in msg, msg
        assert "text_code_page" not in doc.skeleton, doc.skeleton.get("text_code_page")
        _translate_all(s, doc.id)
        text = _decode_export(build_translated_file(s, doc, "fr-FR"), "cp1252")
        assert "X " + WEST_TEXT in text, ascii(text)
    print("  [el+west] Windows-1252 file in a Greek project: decoded right, with a warning OK")


def test_greek_file_in_english_project_warns():
    from app.services.ingest import ingest_document

    with Session(engine) as s:
        doc = ingest_document(s, _project(s, "en-US"), "film.srt", SRT_EL, "segmented")
        warns = _warnings(doc)
        assert len(warns) == 1 and "look wrong" in warns[0]["message"], warns
        assert warns[0]["part"] == "text encoding", warns
        # a real Western file in a Western project: no warning
        doc = ingest_document(s, _project(s, "fr-FR", ("de-DE",)), "w.txt", TXT_WEST, "segmented")
        assert not _warnings(doc), _warnings(doc)
        # UTF-8 is never second-guessed
        utf = ingest_document(
            s, _project(s, "el-GR"), "u.srt", SRT_EL.decode("cp1253").encode(), "segmented"
        )
        assert EL_LINES[0] in _sources(s, utf.id) and not _warnings(utf)
        assert "text_code_page" not in utf.skeleton
    print("  [en+el] Greek file in an English project warns; Western and UTF-8 do not OK")


def test_existing_documents_unaffected():
    """A document imported before the rule has no stamp: export and repair read it with
    the #509 rule (Windows-1252), so its stored segments still line up."""
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document, reingest_document

    with Session(engine) as s:
        p = _project(s, "en-US")
        doc = ingest_document(s, p, "old.srt", SRT_EL, "segmented")
        mojibake = EL_LINES[0].encode("cp1253").decode("cp1252")
        assert mojibake in _sources(s, doc.id)
        # the project moves to Greek later: the old document keeps reading as it was
        p.source_locale = "el-GR"
        s.add(p)
        s.commit()
        out = _decode_export(build_translated_file(s, doc, "fr-FR"), "cp1252")
        assert mojibake in out, ascii(out)
        doc_id = doc.id
    assert reingest_document(doc_id) is True
    with Session(engine) as s:
        assert mojibake in _sources(s, doc_id)
        assert "text_code_page" not in s.get(Document, doc_id).skeleton

        # a document imported under the rule keeps its code page through a repair
        g = ingest_document(s, _project(s, "el-GR"), "new.srt", SRT_EL, "segmented")
        gid = g.id
    assert reingest_document(gid) is True
    with Session(engine) as s:
        assert EL_LINES[0] in _sources(s, gid), ascii(_sources(s, gid))
        assert s.get(Document, gid).skeleton.get("text_code_page") == "cp1253"
    print("  [legacy] pre-rule document exports and repairs as before; repair keeps cp OK")


def test_htmlhelp_and_properties():
    from app.services.ingest import ingest_document

    hhc = (
        "<HTML><HEAD></HEAD><BODY>\n<UL>\n"
        '<LI> <OBJECT type="text/sitemap">\n'
        f'<param name="Name" value="{EL_LINES[0]}">\n'
        '<param name="Local" value="intro.htm">\n'
        "</OBJECT>\n</UL>\n</BODY></HTML>\n"
    ).encode("cp1253")
    props = f"title={RU_TEXT}\nsave=Сохранить\n".encode("cp1251")
    with Session(engine) as s:
        d = ingest_document(s, _project(s, "el-GR"), "toc.hhc", hhc, "segmented")
        assert d.skeleton["units"][0]["text"] == EL_LINES[0], d.skeleton["units"]
        assert d.skeleton.get("hhp_code_page") == 1253, d.skeleton
        # Java reads .properties as ISO-8859-1 (kept); the upload warns
        d = ingest_document(s, _project(s, "ru-RU"), "app.properties", props, "segmented")
        warns = _warnings(d)
        assert warns and "ISO-8859-1" in warns[0]["message"], warns
    print("  [hhc/properties] HTML Help uses the source language; .properties warns OK")


# --------------------------------------------------------------------------- four surfaces
def _api_user(email, name):
    from app.services import apikeys, security

    u = security.create_user(email, name, "admin", PASSWORD_FIXTURE)
    _row, raw = apikeys.create_key(u.id, name + "-test", scopes="api docs")
    return {"X-Loc-Token": raw}


def test_web_and_api():
    from fastapi.testclient import TestClient

    from app.main import app

    tok = _api_user("bencoding@example.test", "bencoding")
    with Session(engine) as s:
        en = _project(s, "en-US").id
        el = _project(s, "el-GR").id
    with TestClient(app) as c:
        r = c.post(
            "/api/documents",
            data={"project_id": str(en)},
            files={"file": ("film.srt", SRT_EL, "application/octet-stream")},
            headers=tok,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        warns = [w for w in body.get("file_warnings") or [] if w.get("kind") == KIND]
        assert warns and "look wrong" in warns[0]["message"], body
        page = c.get(f"/documents/{body['document_id']}")
        assert page.status_code == 200, page.status_code
        assert "its letters look wrong" in page.text, "document page notice"

        r = c.post(
            "/api/documents",
            data={"project_id": str(el)},
            files={"file": ("film.srt", SRT_EL, "application/octet-stream")},
            headers=tok,
        )
        assert r.status_code == 200, r.text[:300]
        assert not [w for w in r.json().get("file_warnings") or [] if w.get("kind") == KIND]
        el_doc = r.json()["document_id"]
    with Session(engine) as s:
        assert EL_LINES[0] in _sources(s, el_doc), ascii(_sources(s, el_doc))
    print("  [web/api] upload JSON warning + document page notice; Greek decoded OK")


def test_cli():
    tmp = os.environ["LOC_TMS_DATA_DIR"]
    path = os.path.join(tmp, "film.srt")
    with open(path, "wb") as fh:
        fh.write(SRT_EL)
    with Session(engine) as s:
        en = _project(s, "en-US").id

    def cli(*argv):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, "-m", "app.cli", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_ROOT,
            env=env,
            timeout=300,
        )
        return r.returncode, r.stdout, r.stderr

    rc, out, err = cli("doc", "upload", str(en), path)
    assert rc == 0, (out, err)
    assert "its letters look wrong" in out, out
    rc, out, err = cli("--json", "doc", "upload", str(en), path)
    assert rc == 0, (out, err)
    res = json.loads(out)
    assert any(w.get("kind") == KIND for w in res.get("file_warnings", [])), res
    print("  [cli] doc upload warning (text + json) OK")


def test_mcp():
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    tok = _api_user("bencoding-mcp@example.test", "bencodingmcp")
    with Session(engine) as s:
        en = _project(s, "en-US").id
    path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "mcp-film.srt")
    with open(path, "wb") as fh:
        fh.write(SRT_EL)
    with TestClient(app) as c:

        async def fake_call(_client, method, url, *, data=None, files=None, json_body=None, **_kw):
            r = c.request(method, url, data=data, files=files, json=json_body, headers=tok)
            assert r.status_code == 200, (url, r.status_code, r.text[:300])
            return r

        real = mcp_server._call
        mcp_server._call = fake_call
        # #755: stdio file_path reads are confined to the allowed input folders, so the
        # fixture's folder must be one of them (same as verify_sdltm_import).
        mcp_server._allow_local_file_reads(True, roots=[os.path.dirname(path)])
        try:
            out = json.loads(
                asyncio.run(mcp_server.submit_translation(path, [], project_id=en, confirm=True))
            )
        finally:
            mcp_server._call = real
            mcp_server._allow_local_file_reads(False)
    warns = out.get("file_warnings") or []
    assert any(w.get("kind") == KIND for w in warns), out
    print("  [mcp] submit_translation relays the encoding warning OK")


def test_gate_cases():
    """Review gate additions: GB18030 beyond GBK, CJK legacy that is valid UTF-8 by accident,
    a Western letter read as a Hebrew vowel point, and one code page per parse."""
    from app.filters.base import (
        UnsupportedEncodingError,
        _read_text_upload,
        source_code_page,
    )
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    # GB18030 four-byte characters (GBK cannot read them): decoded, stamped, exported
    zh = "中文标题 𠀀 €"
    ini = f"[s]\ntitle={zh}\n".encode("gb18030")
    with Session(engine) as s:
        doc = ingest_document(s, _project(s, "zh-CN"), "app.ini", ini, "segmented")
        assert zh in _sources(s, doc.id) and not _warnings(doc), ascii(_sources(s, doc.id))
        assert doc.skeleton.get("text_code_page") == "gb18030", doc.skeleton.get("text_code_page")
        _translate_all(s, doc.id)
        out = build_translated_file(s, doc, "fr-FR")
        assert "X " + zh in _decode_export(out, "gb18030"), out[:200]

    # a short GBK value that is valid UTF-8 by accident: stays UTF-8 (the #509 order), warns
    short = "title=说说\n".encode("gbk")
    assert short.decode("utf-8")
    with source_code_page("zh-CN") as h:
        _text, enc = _read_text_upload(short)
    assert enc == "utf-8" and h.warnings and "look wrong" in h.warnings[0]["message"], h.warnings
    # ... while real UTF-8 (Chinese, Western, curly quotes) in a Chinese project stays quiet
    for real in ("标题=说明\n", "Café crème, naïve škoda\n", "don’t\n"):
        with source_code_page("zh-CN") as h:
            _read_text_upload(real.encode())
        assert not h.warnings, (ascii(real), h.warnings)

    # Western "Ça" in a Hebrew project: Windows-1255 gives a vowel point glued to "a"
    west = "This is strong text.\tÇa c'est la traduction.\n".encode("cp1252")
    with source_code_page("he-IL") as h:
        text, enc = _read_text_upload(west)
    assert enc == "cp1252" and "Ça c'est" in text and h.warnings, (enc, ascii(text))

    # one parse, one code page: after a part fell back to Windows-1252, a later part is read
    # the same way (the single stamp must re-read every part as the import did) ...
    with source_code_page("el-GR") as h:
        _read_text_upload(TXT_WEST)
        text, enc = _read_text_upload(SRT_EL)
    assert h.used == "cp1252" and enc == "cp1252", (h.used, enc)
    # ... and after a part used Windows-1253, a part it cannot read is refused, not mixed in
    with source_code_page("el-GR") as h:
        _read_text_upload(SRT_EL)
        try:
            _read_text_upload(b"caf\xe9 \xaa")  # 0xAA is undefined in Windows-1253
            raise AssertionError("mixed code pages in one parse were not refused")
        except UnsupportedEncodingError:
            pass
    print("  [gate] GB18030, accidental UTF-8 warns, Hebrew point, one code page per parse OK")


def test_export_encoding_with_byte_faithful_exports():
    """With B-bytes on the tree: a Greek Windows-1253 SRT exports in Windows-1253 when the
    translation fits and as UTF-8 when it does not; an HTML page's bytes and charset label
    always agree."""
    import re as _re

    import app.filters.base as fbase
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    if not hasattr(fbase, "encode_like"):
        print("  [bytes] skipped: byte-faithful exports not on this tree")
        return
    with Session(engine) as s:
        p = _project(s, "el-GR", ("fr-FR",))
        doc = ingest_document(s, p, "film.srt", SRT_EL, "segmented")
        assert doc.skeleton.get("text_code_page") == "cp1253"
        _translate_all(s, doc.id, prefix="Γεια ")  # fits Windows-1253
        out = build_translated_file(s, doc, "fr-FR")
        assert not _is_utf8(out) and "Γεια " + EL_LINES[0] in out.decode("cp1253"), out[:120]
        _translate_all(s, doc.id, prefix="日本 ")  # does not fit: UTF-8, never "?"
        out = build_translated_file(s, doc, "fr-FR")
        assert _is_utf8(out) and "日本 " in out.decode("utf-8") and b"?" not in out, out[:120]

        # HTML: undeclared (no label) and declared ISO-8859-7 (#509 rewrites the label)
        body = f"<html><body><p>{EL_LINES[0]}</p></body></html>"
        for name, page in (
            ("plain.html", body),
            ("decl.html", body.replace("<body>", '<head><meta charset="iso-8859-7"></head><body>')),
        ):
            d = ingest_document(s, p, name, page.encode("cp1253"), "segmented")
            for prefix in ("Γεια ", "日本 "):
                _translate_all(s, d.id, prefix=prefix)
                out = build_translated_file(s, d, "fr-FR")
                labels = _re.findall(rb'charset="?([A-Za-z0-9_.:-]+)', out)
                if _is_utf8(out):
                    text = out.decode("utf-8")
                    assert all(lb.lower() == b"utf-8" for lb in labels), (name, labels)
                else:
                    # the bytes are in the codec every label names (undeclared: the stamp)
                    named = {fbase._codec_name(lb.decode()) for lb in labels} or {"cp1253"}
                    assert len(named) == 1 and None not in named, (name, labels)
                    text = out.decode(named.pop())
                assert prefix + EL_LINES[0] in text, (name, ascii(text[:200]))
    print("  [bytes] Greek SRT/HTML export: cp1253 when it fits, else UTF-8; labels agree OK")


def test_heuristic_units():
    from app.filters.base import text_looks_garbled

    # right decodes are quiet
    for text, codec, loc in [
        (" ".join(EL_LINES), "cp1253", "el-GR"),
        (CS_TEXT, "cp1250", "cs-CZ"),
        (RU_TEXT, "cp1251", "ru-RU"),
        (JA_TEXT + "Windows版", "cp932", "ja-JP"),
        ("İstanbul'da güzel bir gün, şimdi çay içiyoruz", "cp1254", "tr-TR"),
        ("שלום עולם", "cp1255", "he-IL"),
        ("สวัสดีชาวโลก", "cp874", "th-TH"),
        (WEST_TEXT, "cp1252", "fr-FR"),
    ]:
        assert text_looks_garbled(text, codec, loc, text.encode(codec)) is None, (loc, codec)
    # wrong decodes speak up
    assert text_looks_garbled(WEST_TEXT.encode("cp1252").decode("cp1253"), "cp1253", "el-GR")
    ru = "Привет, мир! Как дела? Хорошо."
    assert text_looks_garbled(ru.encode("cp1251").decode("cp1252"), "cp1252", "en-US")
    mixed = ("Déjà vu, ça. " * 3).encode() + b"caf\xe9"
    assert text_looks_garbled(mixed.decode("cp1252"), "cp1252", "fr-FR", mixed)
    print("  [heuristic] right decodes quiet, wrong decodes and mixed UTF-8 flagged OK")


def main():
    # Every case runs and reports, so a failing tree shows each broken case (not just the first).
    failed = []
    for case in (
        test_greek_srt_cp1253,
        test_czech_csv_cp1250,
        test_russian_ini_cp1251,
        test_japanese_txt_shift_jis,
        test_western_file_in_greek_project,
        test_greek_file_in_english_project_warns,
        test_existing_documents_unaffected,
        test_htmlhelp_and_properties,
        test_web_and_api,
        test_cli,
        test_mcp,
        test_gate_cases,
        test_export_encoding_with_byte_faithful_exports,
        test_heuristic_units,
    ):
        try:
            case()
        except Exception as exc:  # report every case, then fail
            failed.append(case.__name__)
            print(f"  FAILED {case.__name__}: {type(exc).__name__}: {ascii(str(exc))[:300]}")
    if failed:
        raise SystemExit(f"{len(failed)} case(s) failed: {', '.join(failed)}")
    print("SOURCE CODE PAGE DECODE OK")


if __name__ == "__main__":
    main()
