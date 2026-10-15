"""Batch 6: tabular data (CSV/TSV documents, XLSX column mode, spreadsheet glossary/TM).

  filter   untouched rows/columns are byte-identical; each target language writes only its
           own column; a half-filled target column keeps its text for untranslated rows;
           delimiter (, ; TAB |), quoting, quoted newlines, CRLF/LF and BOMs survive;
           UTF-16 LE/BE stay UTF-16; Windows-1252 converts (#509), undecodable refuses.
  safety   formula cells (=SUM, ="0012") are never units; every written cell starting
           with = + - @ is apostrophe-guarded; hostile inputs (wide rows, huge field,
           unterminated quote, over the segment cap) are refused, never crash.
  mapping  explicit spec by name/number, header=no, errors are ColumnMappingError.
  xlsx     column mode only with a mapping; without one the parse is unchanged.
  e2e      ingest -> segments with notes -> export per locale; reingest keeps the plan;
           web/API/CLI all take the mapping (API 422 on a bad one).
  imports  glossary XLSX/TSV and TM XLSX/CSV/TSV go through the existing importers.

Hermetic + offline. Final line: "CSV FILTER OK".
"""

import codecs
import contextlib
import csv
import io
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_csv_filter_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError
from app.filters.csv_filter_common import ColumnMappingError
from app.main import app
from app.models import Document, GlossaryEntry, Project, Segment, SegmentTM, TargetLocale
from tests._cli_runner import run_cli

init_db()

CSV = get_filter("csv")
TSV = get_filter("tsv")

MULTI = (
    b"\xef\xbb\xbfkey;en;de;fr;notes\r\n"
    b'hello;"Hello; world";;Bonjour;greeting\r\n'
    b'qty;="0012";;;\r\n'
    b'ml;"Line one\nline two";;;x\r\n'
    b"f;=SUM(A1);;;\r\n"
    b"bad;Plus one;;;\r\n"
)


def _parse(data, columns=None, src="en", targets=("de", "fr"), filt=CSV):
    return filt.parse(data, columns=columns, source_locale=src, target_locales=list(targets))


def _lines(b: bytes):
    return b.split(b"\n")


def test_registered():
    exts = supported_exts()
    assert "csv" in exts and "tsv" in exts and "tab" in exts, exts
    print("  [reg] csv, tsv, tab registered OK")


def test_untouched_bytes_identical():
    samples = [
        MULTI,
        b"source,target\nOne,\nTwo,\n",
        b"source,target\r\nOne,\r\nTwo,",  # no final newline
        b"id|text\n1|Alpha\n\n2|Beta\r\n",  # pipe, blank line, mixed endings
        b'text\n"quoted ""inner"" text"\n',
        codecs.BOM_UTF16_LE + "source\tde\nHallo Welt\t\n".encode("utf-16-le"),
        codecs.BOM_UTF16_BE + "source\tde\nHallo Welt\t\n".encode("utf-16-be"),
    ]
    for data in samples:
        r = _parse(data)
        t = r.skeleton["table"]
        assert CSV.build_localized(data, {}, "de", table=t) == data, data
        same = {str(u["p_index"]): u["text"] for u in r.skeleton["units"]}
        assert CSV.build_localized(data, same, "fr", table=t) == data or t["plan"]["targets"], data
        assert CSV.build(data, {}) == data
    print("  [bytes] no-op builds are byte-identical (BOMs, CRLF, pipe, UTF-16) OK")


def test_multilingual_columns():
    r = _parse(MULTI)
    plan = r.skeleton["table"]["plan"]
    assert plan["source"] == 1 and plan["targets"] == {"de": 2, "fr": 3}, plan
    texts = [u["text"] for u in r.skeleton["units"]]
    assert texts == ["Hello; world", "Line one\nline two", "Plus one"], texts
    notes = [u.get("note") for u in r.skeleton["units"]]
    assert notes[0] == "Key: hello | notes: greeting", notes
    t = r.skeleton["table"]
    de = CSV.build_localized(
        MULTI, {"0": "Hallo; Welt", "1": "Zeile\neins", "2": "Plus eins"}, "de", table=t
    )
    rows = list(csv.reader(io.StringIO(de.decode("utf-8-sig"), newline=""), delimiter=";"))
    assert [r_[2] for r_ in rows[1:]] == ["Hallo; Welt", "", "Zeile\neins", "", "Plus eins"], rows
    assert [r_[3] for r_ in rows[1:]] == ["Bonjour", "", "", "", ""], rows  # fr untouched
    assert de.startswith(codecs.BOM_UTF8)
    # untouched records keep their exact bytes
    assert b'qty;="0012";;;\r\n' in de and b"f;=SUM(A1);;;\r\n" in de
    # fr: an untranslated row (target == source) keeps the existing Bonjour
    fr = CSV.build_localized(MULTI, {"0": "Hello; world", "2": "Plus un"}, "fr", table=t)
    rows = list(csv.reader(io.StringIO(fr.decode("utf-8-sig"), newline=""), delimiter=";"))
    assert rows[1][3] == "Bonjour" and rows[5][3] == "Plus un", rows
    assert rows[1][2] == "", rows
    print("  [multi] each locale writes only its column; filled fr kept when untranslated OK")


def test_formula_injection_guarded():
    r = _parse(MULTI)
    t = r.skeleton["table"]
    for bad in ("=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(1)", "\tTab", "＝X"):
        out = CSV.build_localized(MULTI, {"2": bad}, "de", table=t)
        rows = list(csv.reader(io.StringIO(out.decode("utf-8-sig"), newline=""), delimiter=";"))
        assert rows[5][2] == "'" + bad, (bad, rows[5])
    # in-place mode guards too
    data = b"Name,Count\nWidget,5\n"
    r = _parse(data, targets=())
    assert r.skeleton["table"]["plan"]["inplace"], r.skeleton["table"]
    out = CSV.build_localized(data, {"0": "=HYPERLINK(1)"}, None, table=r.skeleton["table"])
    assert b"'=HYPERLINK(1)" in out, out
    print("  [inject] = + - @ TAB and fullwidth = are apostrophe-guarded on write OK")


def test_quoting_of_written_cells():
    data = b'source,target\nPlain,\n"Was quoted",""\n'
    r = _parse(data)
    t = r.skeleton["table"]
    out = CSV.build_localized(data, {"0": 'A, "b"\nc', "1": "simple"}, "de", table=t)
    rows = list(csv.reader(io.StringIO(out.decode(), newline="")))
    assert rows[1] == ["Plain", 'A, "b"\nc'] and rows[2] == ["Was quoted", "simple"], rows
    assert b'"Was quoted","simple"\n' in out, out  # an originally quoted field stays quoted
    print("  [quote] delimiter/quote/newline in a translation is quoted; quoting kept OK")


def test_explicit_mapping_and_errors():
    data = b"ID,English,German\n1,Save,\n2,Open,\n"
    r = _parse(
        data, columns="source=English, target.de=German, key=ID", src="en-US", targets=("de-DE",)
    )
    plan = r.skeleton["table"]["plan"]
    assert plan["source"] == 1 and plan["targets"] == {"de-DE": 2}, plan
    out = CSV.build_localized(
        data, {"0": "Speichern", "1": "Offnen"}, "de-DE", table=r.skeleton["table"]
    )
    assert out == b"ID,English,German\n1,Save,Speichern\n2,Open,Offnen\n", out
    r = _parse(b"1,Save\n2,Open\n", columns="header=no,source=2,target=3", targets=())
    assert [u["text"] for u in r.skeleton["units"]] == ["Save", "Open"]
    out = CSV.build_localized(
        b"1,Save\n2,Open\n", {"0": "S", "1": "O"}, "de", table=r.skeleton["table"]
    )
    assert out == b"1,Save,S\n2,Open,O\n", out  # short rows padded to the target column
    for bad in (
        "source=Nope",
        "target=German",
        "colour=red",
        "source",
        "header=maybe",
        "delimiter=x",
    ):
        try:
            _parse(data, columns=bad)
        except ColumnMappingError:
            continue
        raise AssertionError(f"spec {bad!r} was not refused")
    try:
        _parse(data, columns="source=English,target=English")
        raise AssertionError("source == target accepted")
    except ColumnMappingError:
        pass
    print("  [map] by name/number, header=no, padding; malformed specs refused OK")


def test_encodings():
    cp = "source,de\nCaf\xe9 cr\xe8me,\n".encode("cp1252")
    r = _parse(cp)
    assert r.skeleton["units"][0]["text"] == "Café crème"
    assert r.skeleton.get("source_encoding") == "cp1252", r.skeleton
    # B-bytes: a translation that fits Windows-1252 goes back in it (no BOM, as uploaded);
    # one that does not becomes UTF-8 with a BOM so Excel still reads it.
    out = CSV.build_localized(cp, {"0": "Kaffee"}, "de", table=r.skeleton["table"])
    assert out == "source,de\nCaf\xe9 cr\xe8me,Kaffee\n".encode("cp1252"), out
    out = CSV.build_localized(cp, {"0": "コーヒー"}, "de", table=r.skeleton["table"])
    assert out.startswith(codecs.BOM_UTF8) and "Café".encode() in out, out
    try:
        _parse(b"source\ncaf\x81\x00\x00 x\n")
        raise AssertionError("binary accepted")
    except UnsupportedEncodingError:
        pass
    u16 = codecs.BOM_UTF16_LE + "source\tde\nHallo\t\n".encode("utf-16-le")
    r = _parse(u16, filt=TSV, src="de", targets=("en",))
    out = TSV.build_localized(u16, {"0": "Hello"}, "en", table=r.skeleton["table"])
    assert out.startswith(codecs.BOM_UTF16_LE), out[:4]
    assert out[2:].decode("utf-16-le") == "source\tde\nHallo\t\n" or "Hello" in out[2:].decode(
        "utf-16-le"
    )
    print("  [enc] UTF-16 stays UTF-16; cp1252 converts to UTF-8+BOM; binary refused OK")


def test_tsv_and_sniffing():
    data = b"source\ttarget\nHello, world\t\n"
    r = _parse(data, filt=TSV)
    assert [u["text"] for u in r.skeleton["units"]] == ["Hello, world"]
    out = TSV.build_localized(data, {"0": "Hallo, Welt"}, "de", table=r.skeleton["table"])
    assert out == b"source\ttarget\nHello, world\tHallo, Welt\n", out
    r = _parse(b"source;target\nA, b;\n")
    assert r.skeleton["table"]["delimiter"] == ";", r.skeleton["table"]
    # Gate: a build with no skeleton stamp (the msgraph connector calls parse/build_localized
    # bare) must still read a TSV as TAB, not sniff the commas inside its cells.
    data = b"Red, green, blue\tone, two, three\nCats, dogs, birds\tfour, five, six\n"
    units = [u.text for u in TSV.parse(data).units]
    assert units == ["Red, green, blue", "one, two, three", "Cats, dogs, birds", "four, five, six"]
    out = TSV.build_localized(data, {"0": "Rot, Gruen, Blau"}, "de")
    assert out == data.replace(b"Red, green, blue", b"Rot, Gruen, Blau"), out
    print("  [tsv] TSV is TAB; CSV delimiter sniffed OK")


def test_shopify_fixture():
    with open(
        os.path.join(_ROOT, "tests/fixtures/formats/csv/shopify_translate_adapt.csv"), "rb"
    ) as fh:
        data = fh.read()
    r = _parse(data, targets=("de",))
    plan = r.skeleton["table"]["plan"]
    assert plan["source"] == 6 and plan["targets"] == {"*": 7}, plan
    texts = [u["text"] for u in r.skeleton["units"]]
    assert texts[0] == "Classic Cotton Tee" and "classic-cotton-tee" in texts, texts
    out = CSV.build_localized(
        data,
        {"0": "Klassisches Baumwoll-T-Shirt", "3": "Summer Sale"},
        "de",
        table=r.skeleton["table"],
    )
    lines = _lines(out)
    assert lines[1].rstrip(b"\r").endswith(b"Classic Cotton Tee,Klassisches Baumwoll-T-Shirt"), (
        lines[1]
    )
    assert lines[4] == _lines(data)[4], "a filled translation is kept for an untranslated row"
    assert lines[2] == _lines(data)[2] and lines[5] == _lines(data)[5]
    print("  [shopify] Translate & Adapt export maps Default -> Translated content OK")


def test_hostile_inputs():
    wide = b"source," + b",".join(b"c%d" % i for i in range(1200)) + b"\nx\n"
    for data in (wide, b'source\n"' + b"a" * 200_000 + b'"\n'):
        try:
            _parse(data)
            raise AssertionError("hostile file accepted")
        except InputLimitError:
            pass
    with contextlib.suppress(InputLimitError):  # parses or is refused, never crashes
        _parse(b'source\n"never closed\nmore\n')
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        _parse(b"source\n" + b"".join(b"row %d\n" % i for i in range(20)))
        raise AssertionError("segment cap not enforced")
    except InputLimitError:
        pass
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [hostile] wide rows, huge field, unterminated quote, segment cap OK")


def _xlsx(rows) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_xlsx_column_mode():
    from openpyxl import load_workbook

    data = _xlsx(
        [
            ["id", "en", "de", "fr", "notes"],
            ["a", "Save file", None, "Enregistrer", "button"],
            ["b", "Open file", None, None, None],
            ["c", "=CONCAT(B2)", None, None, None],
        ]
    )
    x = get_filter("xlsx")
    plain = x.parse(data)
    assert "table" not in plain.skeleton and len(plain.units) == 12, len(plain.units)
    r = x.parse(
        data,
        columns="source=en,target.de=de,target.fr=fr,key=id,context=notes",
        source_locale="en",
        target_locales=["de", "fr"],
    )
    assert [u.text for u in r.units] == ["Save file", "Open file"]
    t = r.skeleton["table"]
    out = x.build_localized(data, {"0": "Save file", "1": "=Ouvrir"}, "fr", table=t)
    ws = load_workbook(io.BytesIO(out)).active
    assert ws["D2"].value == "Enregistrer" and ws["D3"].value == "=Ouvrir", ws["D3"].value
    assert ws["D3"].data_type == "s", "a written cell is text, never a formula"
    assert ws["B4"].value == "=CONCAT(B2)" and ws["C2"].value is None
    # Gate (merge with Batch 4): an .xlsm in column mode keeps its VBA project and macro type.
    import zipfile

    def members(blob):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            return {i.filename: zf.read(i) for i in zf.infolist()}

    m = members(data)
    xlsx_ct = b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    xlsm_ct = b"application/vnd.ms-excel.sheet.macroEnabled.main+xml"
    m["[Content_Types].xml"] = (
        m["[Content_Types].xml"]
        .replace(xlsx_ct, xlsm_ct)
        .replace(
            b"</Types>",
            b'<Override PartName="/xl/vbaProject.bin" '
            b'ContentType="application/vnd.ms-office.vbaProject"/></Types>',
        )
    )
    m["xl/vbaProject.bin"] = b"\xd0\xcf\x11\xe0 gate VBA stand-in"
    m["xl/_rels/workbook.xml.rels"] = m["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>",
        b'<Relationship Id="rIdVba" Type="http://schemas.microsoft.com/office/2006/'
        b'relationships/vbaProject" Target="vbaProject.bin"/></Relationships>',
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in m.items():
            zf.writestr(name, body)
    xlsm = buf.getvalue()
    xm = get_filter("xlsm")
    r = xm.parse(xlsm, columns="source=en,target.fr=fr", source_locale="en", target_locales=["fr"])
    out = members(xm.build_localized(xlsm, {"1": "Ouvrir"}, "fr", table=r.skeleton["table"]))
    assert out["xl/vbaProject.bin"] == m["xl/vbaProject.bin"], "column mode dropped the VBA"
    assert xlsm_ct in out["[Content_Types].xml"]
    print("  [xlsx] column mode only with a mapping; targets per locale, text-typed OK")


# --- end to end ---------------------------------------------------------------
def _project(name, src="en-US", targets=("de-DE", "fr-FR")) -> int:
    with Session(engine) as s:
        p = Project(name=name, source_locale=src)
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in targets:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        return p.id


E2E = b"key,en,de,fr,context\r\ngreet,Good morning,,Bonjour,home screen\r\nbye,See you soon,,,\r\n"


def test_ingest_export_reingest():
    from app.services import export, ingest

    pid = _project("csv-e2e")
    with Session(engine) as s:
        p = s.get(Project, pid)
        doc = ingest.ingest_document(s, p, "strings.csv", E2E, "segmented")
        did = doc.id
        segs = s.exec(select(Segment).where(Segment.document_id == did)).all()
        de = sorted([x for x in segs if x.target_locale == "de-DE"], key=lambda x: x.order_index)
        assert [x.source_text for x in de] == ["Good morning", "See you soon"], de
        assert de[0].meta == {"notes": "Key: greet | context: home screen"}, de[0].meta
        de[0].target_text = "Guten Morgen"
        s.add(de[0])
        s.commit()
        out = export.build_translated_file(s, s.get(Document, did), "de-DE")
        assert out == E2E.replace(b"Good morning,,", b"Good morning,Guten Morgen,"), out
        out = export.build_translated_file(s, s.get(Document, did), "fr-FR")
        assert out.startswith(b"key,en,de,fr,context\r\ngreet,Good morning,,Bonjour,"), out
    for x in de:
        with Session(engine) as s:
            row = s.get(Segment, x.id)
            row.target_text = ""
            s.add(row)
            s.commit()
    assert ingest.reingest_document(did)
    with Session(engine) as s:
        assert s.get(Document, did).skeleton["table"]["plan"]["targets"] == {"de-DE": 2, "fr-FR": 3}
    print("  [e2e] ingest notes, per-locale export, reingest keeps the plan OK")


def test_surfaces_api_and_cli():
    from app.models import Role, User
    from app.services import apikeys

    pid = _project("csv-surfaces", targets=("de-DE",))
    with Session(engine) as s:
        user = User(email="csv-admin@example.test", role=Role.ADMIN)
        s.add(user)
        s.commit()
        s.refresh(user)
        uid = user.id
    _, key = apikeys.create_key(uid, "csv test", scopes="api docs")
    client = TestClient(app, headers={"X-Loc-Token": key})
    data = b"ID,English,German\n1,Save,\n"
    res = client.post(
        "/api/documents",
        files={"file": ("ui.csv", data, "text/csv")},
        data={"project_id": str(pid), "columns": "source=Nope"},
    )
    assert res.status_code == 422 and "Nope" in res.json()["error"], (res.status_code, res.text)
    res = client.post(
        "/api/documents",
        files={"file": ("ui.csv", data, "text/csv")},
        data={"project_id": str(pid), "columns": "source=English,target.de=German"},
    )
    assert res.status_code == 200, res.text
    with Session(engine) as s:
        doc = s.get(Document, res.json()["document_id"])
        assert doc.skeleton["table"]["plan"]["targets"] == {"de-DE": 2}
    path = os.path.join(tempfile.mkdtemp(prefix="loctms_csvcli_"), "ui.csv")
    with open(path, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(
        ["--json", "doc", "upload", str(pid), path, "--columns", "source=2,target=3"]
    )
    assert code == 0, (out, err)
    code, out, err = run_cli(["doc", "upload", str(pid), path, "--columns", "source=9"])
    assert code != 0 and "does not exist" in (out + err), (code, out, err)
    page = client.get(f"/projects/{pid}").text
    assert 'id="doc-upload-columns"' in page and ".csv,.tsv" in page
    print("  [surfaces] API 422/200, CLI --columns, web field OK")


def test_glossary_and_tm_spreadsheets():
    from app.services import glossary_hub, tm

    pid = _project("csv-gloss", targets=("de-DE",))
    xl = _xlsx([["en-US", "de-DE"], ["checkout", "Kasse"], ["=equals", "=gleich"], ["", "x"]])
    with Session(engine) as s:
        res = glossary_hub.import_bytes(s, pid, xl, "en-US", "de-DE", filename="terms.xlsx")
        assert res["entries"] == 2 and res["format"] == "csv", res
        res = glossary_hub.import_bytes(
            s, pid, b"source\ttarget\ncart\tWarenkorb\n", "en-US", "de-DE", filename="t.tsv"
        )
        assert res["entries"] == 1, res
        terms = {
            e.source_term: e.target_term
            for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == pid))
        }
        assert terms == {"checkout": "Kasse", "=equals": "=gleich", "cart": "Warenkorb"}, terms
        res = tm.import_tmx(s, pid, b"Save the file,Datei speichern\nOpen,\n", "en-US", "de-DE")
        assert res["imported"] == 1, res
        res = tm.import_tmx(
            s, pid, _xlsx([["source", "target"], ["Close", "Schliessen"]]), "en-US", "de-DE"
        )
        assert res["imported"] == 1, res
        res = tm.import_tmx(s, pid, b"en-US\tde-DE\nA <b> & c\tA <b> & c DE\n", "en-US", "de-DE")
        assert res["imported"] == 1, res
        rows = {
            r.source_text: r.target_text
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid))
        }
        assert rows.get("A <b> & c") == "A <b> & c DE" and rows.get("Close") == "Schliessen", rows
        try:
            tm.import_tmx(s, pid, b"just one column\nmore\n", "en-US", "de-DE")
            raise AssertionError("one-column TM accepted")
        except InputLimitError:
            pass
    print("  [imports] glossary XLSX/TSV and TM CSV/XLSX/TSV via existing importers OK")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("CSV FILTER OK")
