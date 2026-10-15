"""Batch B-intake1 (the maintainer's decisions 2026-10-11, numbers 6, 9 and 21).

#687 (decision 6): a bilingual vendor file (SDLXLIFF, memoQ/Phrase XLIFF, Wordfast TXLF and
TXML, Trados TTX) keeps its own target language on export, so it is accepted with a warning
and seeded, translated and exported ONLY into the project target it declares; when no project
target is its language the upload is refused with a message naming both. Add-a-language and
the translate door never seed another language onto it; export refuses one (documents from
before the rule included); a project bundle skips and reports the pair. Web (document page
notice, upload refusal), /api (upload JSON `file_warnings`, 422) and CLI (`doc upload` text
and --json) all show it.

PO (decision 9): a catalog with no header entry gets one on a localized export (UTF-8 charset,
target Language, Plural-Forms per #739); every header declares the UTF-8 charset the export is
written in (a missing Content-Type, or the template placeholder CHARSET).

HTML Help (decision 21): a .hhc/.hhk with no declared code page is read in the code page of the
.hhp project's Language= when the .hhp is in the same upload (ZIP batch) or already in the
project, and the export reads it the same way; without one the #509 rule still applies.

Hermetic and offline: throwaway LOC_TMS_DATA_DIR, mock provider, ASCII-only prints.
Final line: "INTAKE TARGET PO HHP OK".
"""

import io
import json
import os
import subprocess
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_intake1_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.filters import get_filter
from app.filters.base import InputLimitError
from app.models import Document, Project, Segment, TargetLocale

init_db()

FX = os.path.join(_ROOT, "tests", "fixtures", "formats")
PASSWORD_FIXTURE = "intake1-fixture-pw-12345"  # pregate: allow-secret test fixture user


def _fx(*parts) -> bytes:
    with open(os.path.join(FX, *parts), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")


# (filename, bytes, declared target as the file writes it)
FILES = [
    ("guide.sdlxliff", _fx("catxliff", "sample.sdlxliff"), "de-DE"),
    ("guide.mqxliff", _fx("catxliff", "sample.mqxliff"), "de-DE"),
    ("guide.mxliff", _fx("catxliff", "sample.mxliff"), "de"),
    ("guide.txlf", _fx("wordfast", "sample.txlf"), "de-DE"),
    ("guide.txml", _fx("wordfast", "sample.txml"), "de_DE"),
    ("guide.ttx", _fx("ttx", "sample.ttx"), "DE-DE"),
]

_N = [0]


def _project(s, targets, source="en-US"):
    _N[0] += 1
    p = Project(
        name=f"intake1-{_N[0]}-" + "-".join(targets),
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


def _locales(s, doc_id):
    return sorted(
        set(s.exec(select(Segment.target_locale).where(Segment.document_id == doc_id)).all())
    )


def _translate_all(s, doc_id, locale, prefix="X "):
    for g in s.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
    ).all():
        g.target_text = prefix + g.source_text
        g.status = "translated"
        s.add(g)
    s.commit()


# --------------------------------------------------------------------------- #687
def test_687_ingest():
    from app.services.ingest import ingest_document

    with Session(engine) as s:
        for name, data, declared in FILES:
            # several targets, one of them the file's: seeded for that one only, with a warning
            p = _project(s, ["fr-FR", "de-DE", "ja-JP"])
            doc = ingest_document(s, p, name, data, "segmented")
            assert _locales(s, doc.id) == ["de-DE"], (name, _locales(s, doc.id))
            warns = (doc.skeleton or {}).get("file_warnings") or []
            msgs = [w["message"] for w in warns if w.get("kind") == "file_target_language"]
            assert len(msgs) == 1, (name, warns)
            assert declared in msgs[0] and "fr-FR" in msgs[0] and "ja-JP" in msgs[0], msgs
            assert (doc.skeleton or {}).get("file_target_locales") == [declared], doc.skeleton

            # only the file's own language: no warning
            solo = _project(s, ["de-DE"])
            doc = ingest_document(s, solo, name, data, "segmented")
            assert _locales(s, doc.id) == ["de-DE"], name
            assert not [
                w
                for w in (doc.skeleton or {}).get("file_warnings") or []
                if w.get("kind") == "file_target_language"
            ], (name, doc.skeleton.get("file_warnings"))

            # none of the project's targets is the file's language: refused, nothing stored
            bad = _project(s, ["ja-JP"])
            try:
                ingest_document(s, bad, name, data, "segmented")
            except InputLimitError as exc:
                assert declared in str(exc) and "ja-JP" in str(exc), str(exc)
                assert "Nothing was imported" in str(exc), str(exc)
            else:
                raise AssertionError(f"{name}: a {declared} file into ja-JP must be refused")
            s.rollback()
            assert not s.exec(select(Document).where(Document.project_id == bad.id)).all(), name

        # a bare-language declaration (memoQ `ja`) matches a regional project target
        mq_ja = FILES[1][1].replace(b'target-language="de-DE"', b'target-language="ja"')
        p = _project(s, ["ja-JP", "de-DE"])
        doc = ingest_document(s, p, "ja.mqxliff", mq_ja, "segmented")
        assert _locales(s, doc.id) == ["ja-JP"], _locales(s, doc.id)
        # plain XLIFF is stamped to the project target: not restricted
        xlf = _fx("catxliff", "protected.xlf")
        doc = ingest_document(s, _project(s, ["de-DE", "ja-JP"]), "app.xlf", xlf, "segmented")
        assert _locales(s, doc.id) == ["de-DE", "ja-JP"], _locales(s, doc.id)
    print("  [687 ingest] 6 bilingual formats: matching target only + warning, mismatch refused OK")


def test_687_seed_and_export():
    from app.services import delivery, locale_seed
    from app.services.export import build_translated_file
    from app.services.export_gate import ExportUnreadableBlob
    from app.services.ingest import ingest_document

    sdl = FILES[0][1]
    with Session(engine) as s:
        p = _project(s, ["de-DE", "fr-FR"])
        doc = ingest_document(s, p, "guide.sdlxliff", sdl, "segmented")
        # the translate door refuses another language; add-a-language skips it with a note
        try:
            locale_seed.seed_locale(s, doc.id, "fr-FR", strict=True)
        except InputLimitError as exc:
            assert "de-DE" in str(exc) and "fr-FR" in str(exc), str(exc)
        else:
            raise AssertionError("seeding fr-FR onto a de-DE SDLXLIFF must be refused")
        assert locale_seed.seed_locale(s, doc.id, "it-IT", strict=False) == 0
        s.refresh(doc)
        assert _locales(s, doc.id) == ["de-DE"], _locales(s, doc.id)
        assert any("it-IT" in w["message"] for w in doc.skeleton.get("file_warnings") or [])

        _translate_all(s, doc.id, "de-DE", "DE ")
        out = build_translated_file(s, doc, "de-DE")
        assert b'target-language="de-DE"' in out and b"DE " in out, out[:400]
        try:
            build_translated_file(s, doc, "fr-FR")
        except ExportUnreadableBlob as exc:
            assert "fr-FR" in str(exc) and "de-DE" in str(exc), str(exc)
        else:
            raise AssertionError("exporting a de-DE SDLXLIFF as fr-FR must be refused")

        # a project bundle skips and reports the other language instead of failing
        data, _name, _ct, skipped = delivery.export_project(s, p.id, locales=["de-DE", "fr-FR"])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert any(n.startswith("de-DE/") for n in names), names
        assert not any(n.startswith("fr-FR/") for n in names), names
        assert any(x["locale"] == "fr-FR" and x["doc_id"] == doc.id for x in skipped), skipped

        # a document uploaded before the rule (no stamp, rows for the other language)
        legacy = _project(s, ["fr-FR"])
        old = Document(
            project_id=legacy.id,
            upload_blob_key="legacy.sdlxliff",
            filename="legacy.sdlxliff",
            fmt="sdlxliff",
            mode="segmented",
            status="new",
            skeleton={k: v for k, v in doc.skeleton.items() if k != "file_target_locales"},
        )
        s.add(old)
        s.commit()
        s.refresh(old)
        from app.ports import blobstore

        blobstore.get().put(blobstore.document_key(old), sdl)
        for g in s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "de-DE")
        ).all():
            s.add(
                Segment(
                    document_id=old.id,
                    target_locale="fr-FR",
                    order_index=g.order_index,
                    source_text=g.source_text,
                    target_text="FR " + g.source_text,
                    status="translated",
                )
            )
        s.commit()
        try:
            build_translated_file(s, old, "fr-FR")
        except ExportUnreadableBlob as exc:
            assert "de-DE" in str(exc), str(exc)
        else:
            raise AssertionError("a pre-rule de-DE SDLXLIFF must not export French under de-DE")
    print("  [687 seed/export] translate door, add-language, export, bundle, legacy doc OK")


def _api_user():
    from app.services import apikeys, security

    u = security.create_user("intake1@example.test", "intake1", "admin", PASSWORD_FIXTURE)
    _row, raw = apikeys.create_key(u.id, "intake1-test", scopes="api docs")
    return {"X-Loc-Token": raw}


def test_687_web_and_api():
    from fastapi.testclient import TestClient

    from app.main import app

    tok = _api_user()
    with Session(engine) as s:
        multi = _project(s, ["fr-FR", "de-DE"]).id
        wrong = _project(s, ["ja-JP"]).id
    ttx = FILES[5][1]
    with TestClient(app) as c:
        r = c.post(
            "/api/documents",
            data={"project_id": str(multi)},
            files={"file": ("guide.ttx", ttx, "application/octet-stream")},
            headers=tok,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        kinds = [w.get("kind") for w in body.get("file_warnings") or []]
        assert "file_target_language" in kinds, body
        assert body["target_locales"] == 1, body
        page = c.get(f"/documents/{body['document_id']}")
        assert page.status_code == 200, page.status_code
        assert "own target language is DE-DE" in page.text, "document page notice"

        r = c.post(
            "/api/documents",
            data={"project_id": str(wrong)},
            files={"file": ("guide.ttx", ttx, "application/octet-stream")},
            headers=tok,
        )
        assert r.status_code == 422, (r.status_code, r.text[:300])
        assert "DE-DE" in r.json()["error"] and "ja-JP" in r.json()["error"], r.json()
    print("  [687 web/api] upload warning JSON + document notice, mismatch 422 OK")


def test_687_cli():
    tmp = os.environ["LOC_TMS_DATA_DIR"]
    path = os.path.join(tmp, "guide.txlf")
    with open(path, "wb") as fh:
        fh.write(FILES[3][1])
    with Session(engine) as s:
        multi = _project(s, ["de-DE", "ja-JP"]).id
        wrong = _project(s, ["ja-JP"]).id

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

    rc, out, err = cli("doc", "upload", str(multi), path)
    assert rc == 0, (out, err)
    assert "translated and exported only into de-DE" in out, out
    rc, out, err = cli("--json", "doc", "upload", str(multi), path)
    assert rc == 0, (out, err)
    res = json.loads(out)
    assert any(w.get("kind") == "file_target_language" for w in res.get("file_warnings", [])), res
    rc, out, err = cli("doc", "upload", str(wrong), path)
    assert rc != 0, (rc, out, err)
    assert "de-DE" in (out + err) and "ja-JP" in (out + err), (out, err)
    print("  [687 cli] doc upload warning (text + json), mismatch refused OK")


# --------------------------------------------------------------------------- PO header
def _po_header(raw: bytes) -> str:
    from app.filters.po_filter import _is_header, _parse_entries

    heads = [e for e in _parse_entries(raw) if _is_header(e)]
    assert len(heads) == 1, raw[:300]
    return heads[0]["msgstr"]


def test_po_header():
    from babel.messages.pofile import read_po

    po = get_filter("po")
    nohdr = (
        b"# A catalog someone wrote by hand\n\n"
        b'#: app.py:1\nmsgid "Hello"\nmsgstr ""\n\n'
        b'msgid "One file"\nmsgid_plural "%d files"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    )
    units = [u.text for u in po.parse(nohdr).units]
    assert units == ["Hello", "One file", "%d files"], units
    out = po.build_localized(nohdr, {"0": "Hallo", "1": "Eine Datei", "2": "%d Dateien"}, "de-DE")
    hdr = _po_header(out)
    assert "charset=UTF-8" in hdr and "Language: de_DE" in hdr, hdr
    assert "Plural-Forms: nplurals=2; plural=(n != 1);" in hdr, hdr
    cat = read_po(io.BytesIO(out))
    assert cat.charset.lower() == "utf-8" and str(cat.locale) == "de_DE", (cat.charset, cat.locale)
    assert cat["Hello"].string == "Hallo", cat["Hello"].string
    assert out.index(b"# A catalog someone") < out.index(b'msgid ""'), out[:200]
    assert out.count(b'msgid ""') == 1, out

    # a header with no Language and no Content-Type (#688 added Language; now charset too)
    bare = b'msgid ""\nmsgstr "Project-Id-Version: demo 1.0\\n"\n\nmsgid "Hello"\nmsgstr ""\n'
    hdr = _po_header(po.build_localized(bare, {"0": "Bonjour"}, "fr-FR"))
    assert "Project-Id-Version: demo 1.0" in hdr and "Language: fr_FR" in hdr, hdr
    assert "Content-Type: text/plain; charset=UTF-8" in hdr, hdr
    # the template placeholder charset is set to the UTF-8 the export is written in
    tmpl = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=CHARSET\\n"\n\n'
        b'msgid "Hello"\nmsgstr ""\n'
    )
    hdr = _po_header(po.build_localized(tmpl, {"0": "Hola"}, "es-ES"))
    assert "charset=UTF-8" in hdr and "CHARSET" not in hdr and "Language: es" in hdr, hdr
    # an unknown locale still gets a header that names it
    hdr = _po_header(po.build_localized(nohdr, {"0": "x"}, "tlh"))
    assert "charset=UTF-8" in hdr and "Language: tlh" in hdr, hdr
    # build() (the byte-faithful rebuild) invents nothing
    assert b'msgid ""' not in po.build(nohdr, {"0": "Hallo"})
    print("  [po] header added (charset, Language, Plural-Forms), Content-Type fixed OK")


# --------------------------------------------------------------------------- HTML Help
RU_NAME = "Введение"  # Vvedenie (Introduction)
RU_NEW = "Обзор"  # Obzor (Overview)
HHC = (
    "<HTML><HEAD></HEAD><BODY>\n<UL>\n"
    '<LI> <OBJECT type="text/sitemap">\n'
    f'<param name="Name" value="{RU_NAME}">\n'
    '<param name="Local" value="intro.htm">\n'
    "</OBJECT>\n</UL>\n</BODY></HTML>\n"
).encode("cp1251")
HHP = (
    "[OPTIONS]\nCompiled file=help.chm\nContents file=toc.hhc\nLanguage=0x419 Russian\nTitle=Help\n"
).encode("cp1251")


def _zip(members) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def test_htmlhelp_code_page():
    from app.filters.htmlhelp_filter import hhp_code_page
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document, ingest_zip_batch

    assert hhp_code_page(HHP) == 1251, hhp_code_page(HHP)
    assert hhp_code_page(b"[OPTIONS]\nTitle=x\n") is None
    hhc = get_filter("hhc")
    # without a project file: the #509 rule (Windows-1252) as before
    assert hhc.parse(HHC).units[0].text == RU_NAME.encode("cp1251").decode("cp1252")
    parsed = hhc.parse(HHC, hhp_code_page=1251)
    assert parsed.units[0].text == RU_NAME, parsed.units[0].text
    assert parsed.skeleton.get("hhp_code_page") == 1251, parsed.skeleton
    # a declared charset still wins over the project's language
    declared = HHC.replace(
        b"<HEAD>",
        b'<HEAD><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">',
    )
    assert hhc.parse(declared, hhp_code_page=1251).units[0].text != RU_NAME

    with Session(engine) as s:
        # same upload: a ZIP batch with the .hhc before the .hhp
        p = _project(s, ["ru-RU"])
        res = ingest_zip_batch(
            s, p, "help.zip", _zip([("toc.hhc", HHC), ("help.hhp", HHP)]), "segmented"
        )
        assert not res["failed"], res
        toc = next(d for d in res["documents"] if d["fmt"] == "hhc")
        doc = s.get(Document, toc["id"])
        assert doc.skeleton["units"][0]["text"] == RU_NAME, doc.skeleton["units"]
        # export reads it the same way: untouched Cyrillic and the translation both fit cp1251
        for g in s.exec(select(Segment).where(Segment.document_id == doc.id)).all():
            g.target_text = RU_NEW
            g.status = "translated"
            s.add(g)
        s.commit()
        out = build_translated_file(s, doc, "ru-RU")
        assert RU_NEW in out.decode("cp1251"), out

        # same project: the .hhp uploaded first, then the .hhc on its own
        p2 = _project(s, ["ru-RU"])
        ingest_document(s, p2, "help.hhp", HHP, "segmented")
        d2 = ingest_document(s, p2, "toc.hhc", HHC, "segmented")
        assert d2.skeleton["units"][0]["text"] == RU_NAME, d2.skeleton["units"]
        # no project file anywhere: today's rule
        d3 = ingest_document(s, _project(s, ["ru-RU"]), "toc.hhc", HHC, "segmented")
        assert d3.skeleton["units"][0]["text"] != RU_NAME
        assert "hhp_code_page" not in d3.skeleton
    print("  [htmlhelp] .hhp Language= code page (zip + project), declared wins, export OK")


# --------------------------------------------------------------------------- Review gate
def test_gate_po_legacy_charsets():
    """Gate: a PO in a legacy code page (declared ISO-8859-1 / KOI8-R, or no header at all,
    read as Windows-1252) comes back as UTF-8 bytes under a UTF-8 header with every
    character intact: no header may name UTF-8 over legacy bytes (or the reverse)."""
    from babel.messages.pofile import read_po

    po = get_filter("po")
    bs = chr(92)

    def hdr_lines(*lines):
        return "".join('"' + ln + bs + 'n"\n' for ln in lines)

    cases = [
        (
            "latin1",
            (
                'msgid ""\nmsgstr ""\n'
                + hdr_lines("Content-Type: text/plain; charset=ISO-8859-1", "Language: fr")
                + '\nmsgid "café"\nmsgstr ""\n'
            ).encode("latin-1"),
            "café",
            "de-DE",
        ),
        (
            "koi8r",
            (
                'msgid ""\nmsgstr ""\n'
                + hdr_lines("Content-Type: text/plain; charset=KOI8-R")
                + '\nmsgid "привет"\nmsgstr ""\n'
            ).encode("koi8-r"),
            "привет",
            "uk-UA",
        ),
        (
            "noheader-cp1252",
            '# hand written\n\nmsgid "café – menü"\nmsgstr ""\n'.encode("cp1252"),
            "café – menü",
            "ru-RU",
        ),
    ]
    want = "éя€"  # e-acute, Cyrillic ya, euro: fits no single legacy page
    for name, raw, msgid, loc in cases:
        out = po.build_localized(raw, {"0": want}, loc)
        text = out.decode("utf-8")  # the bytes ARE UTF-8
        assert msgid in text and want in text, (name, text[:300])
        hdr = _po_header(out)
        assert "charset=UTF-8" in hdr and hdr.count("charset=") == 1, (name, hdr)
        cat = read_po(io.BytesIO(out))
        assert cat.charset.lower() == "utf-8", (name, cat.charset)
        assert cat[msgid].string == want, (name, cat[msgid].string)
    print("  [gate po] legacy code pages export as UTF-8 under a UTF-8 header, no mojibake OK")


def test_gate_687_legacy_bundle():
    """Gate: a document uploaded before the rule (no stamp, rows for another language) is
    skipped and reported by the project bundle, never delivered under its own language."""
    from app.ports import blobstore
    from app.services import delivery

    sdl = FILES[0][1]
    with Session(engine) as s:
        p = _project(s, ["fr-FR"])
        old = Document(
            project_id=p.id,
            upload_blob_key="legacy2.sdlxliff",
            filename="legacy2.sdlxliff",
            fmt="sdlxliff",
            mode="segmented",
            status="new",
            skeleton=get_filter("sdlxliff").parse(sdl).skeleton,
        )
        assert "file_target_locales" not in (old.skeleton or {})
        s.add(old)
        s.commit()
        s.refresh(old)
        blobstore.get().put(blobstore.document_key(old), sdl)
        for i, u in enumerate(get_filter("sdlxliff").parse(sdl).units):
            s.add(
                Segment(
                    document_id=old.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=u.text,
                    target_text="FR " + u.text,
                    status="translated",
                )
            )
        s.commit()
        data, _name, _ct, skipped = delivery.export_project(s, p.id, locales=["fr-FR"], zip=True)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert not any(n.endswith(".sdlxliff") for n in names), names
        assert any(x.get("doc_id") == old.id and "de-DE" in str(x) for x in skipped), skipped
    print("  [gate 687] pre-rule document skipped and reported by the project bundle OK")


def test_gate_687_mcp_warning():
    """Gate (four-surface rule): MCP submit_translation hands back the upload's file
    warnings, as the web page, /api JSON and CLI do."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    tok = _api_user_named("intake1-mcp@example.test", "intake1mcp")
    with Session(engine) as s:
        multi = _project(s, ["fr-FR", "de-DE"]).id
    path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "mcp-guide.ttx")
    with open(path, "wb") as fh:
        fh.write(FILES[5][1])
    with TestClient(app) as c:

        async def fake_call(_client, method, url, *, data=None, files=None, json_body=None, **_kw):
            r = c.request(method, url, data=data, files=files, json=json_body, headers=tok)
            assert r.status_code == 200, (url, r.status_code, r.text[:300])
            return r

        real = mcp_server._call
        mcp_server._call = fake_call
        # #755: stdio reads only inside the allowed input folders; this file lives in the data dir.
        mcp_server._allow_local_file_reads(True, roots=[os.environ["LOC_TMS_DATA_DIR"]])
        try:
            out = json.loads(
                asyncio.run(mcp_server.submit_translation(path, [], project_id=multi, confirm=True))
            )
        finally:
            mcp_server._call = real
    warns = out.get("file_warnings") or []
    assert any(
        w.get("kind") == "file_target_language" and "DE-DE" in w.get("message", "") for w in warns
    ), out
    print("  [gate 687 mcp] submit_translation relays the upload warning OK")


# --------------------------------------------------------------------------- #759
def _pkg(members) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def test_759_xliff_packages():
    """#759: the XLIFF members of a memoQ .mqxlz and a WorldServer .xlz / .wsxz package keep
    their own target-language on export (xliff_package.build_package), so the #687 rule
    applies to the package too."""
    from app.services import bilingual_target as bt
    from app.services.export import build_translated_file
    from app.services.export_gate import ExportUnreadableBlob
    from app.services.ingest import ingest_document

    mq = _fx("catxliff", "sample.mqxliff")
    sdl = _fx("catxliff", "sample.sdlxliff")
    pkgs = [
        ("guide.mqxlz", _pkg([("document.mqxliff", mq), ("skeleton.xml", b"<s/>")])),
        ("guide.wsxz", _pkg([("doc/guide.sdlxliff", sdl), ("tm/res.tmx", b"<tmx/>")])),
        ("guide.xlz", _pkg([("content.xlf", sdl), ("manifest.rkm", b"<m/>")])),
    ]
    with Session(engine) as s:
        for name, data in pkgs:
            ext = name.rsplit(".", 1)[1]  # a document's fmt is its upload extension
            assert get_filter(ext).fmt in ("mqxlz", "xlz"), (name, get_filter(ext).fmt)
            assert ext in bt.BILINGUAL_FMTS, (name, ext)
            got = bt.declared_targets(ext, data)
            assert got == ["de-DE"], (name, got)
            p = _project(s, ["fr-FR", "de-DE"])
            doc = ingest_document(s, p, name, data, "segmented")
            assert _locales(s, doc.id) == ["de-DE"], (name, _locales(s, doc.id))
            warns = [
                w
                for w in (doc.skeleton or {}).get("file_warnings") or []
                if w.get("kind") == "file_target_language"
            ]
            assert len(warns) == 1 and "fr-FR" in warns[0]["message"], (name, warns)
            _translate_all(s, doc.id, "de-DE", "DE ")
            out = build_translated_file(s, doc, "de-DE")
            inner = b"".join(
                zipfile.ZipFile(io.BytesIO(out)).read(n)
                for n in zipfile.ZipFile(io.BytesIO(out)).namelist()
            )
            assert b'target-language="de-DE"' in inner and b"DE " in inner, name
            try:
                build_translated_file(s, doc, "fr-FR")
            except ExportUnreadableBlob as exc:
                assert "de-DE" in str(exc) and "fr-FR" in str(exc), str(exc)
            else:
                raise AssertionError(f"{name}: exporting a de-DE package as fr-FR must be refused")
            bad = _project(s, ["ja-JP"])
            try:
                ingest_document(s, bad, name, data, "segmented")
            except InputLimitError as exc:
                assert "de-DE" in str(exc) and "ja-JP" in str(exc), str(exc)
            else:
                raise AssertionError(f"{name}: a de-DE package into ja-JP must be refused")
            s.rollback()
            assert not s.exec(select(Document).where(Document.project_id == bad.id)).all(), name
        # a package whose XLIFF names no target-language is stamped by the export: not restricted
        xlf = _fx("catxliff", "protected.xlf").replace(b' target-language="fr"', b"")
        bare = _pkg([("content.xlf", xlf)])
        doc = ingest_document(s, _project(s, ["de-DE", "ja-JP"]), "bare.xlz", bare, "segmented")
        assert _locales(s, doc.id) == ["de-DE", "ja-JP"], _locales(s, doc.id)
    print("  [759 packages] mqxlz / wsxz / xlz: declared target only, mismatch refused OK")


def _api_user_named(email, name):
    from app.services import apikeys, security

    u = security.create_user(email, name, "admin", PASSWORD_FIXTURE)
    _row, raw = apikeys.create_key(u.id, name + "-test", scopes="api docs")
    return {"X-Loc-Token": raw}


def main():
    test_687_ingest()
    test_687_seed_and_export()
    test_687_web_and_api()
    test_687_cli()
    test_po_header()
    test_htmlhelp_code_page()
    test_gate_po_legacy_charsets()
    test_gate_687_legacy_bundle()
    test_gate_687_mcp_warning()
    test_759_xliff_packages()
    print("INTAKE TARGET PO HHP OK")


if __name__ == "__main__":
    main()
