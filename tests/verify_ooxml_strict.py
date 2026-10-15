"""#669: Strict Open XML workbooks, documents and presentations (format soak).

A file Office saved as "Strict Open XML" names its parts with http://purl.oclc.org/ooxml/
namespaces. The xlsx filter read such a workbook as zero units (upload: "no text") and its
rebuild crashed with openpyxl's IndexError; the docx filter crashed with a KeyError and
the pptx filter refused with a wrong reason ("no presentation part").

  [A] xlsx: a Strict workbook parses to its text (both sheets, shared, rich and inline
      strings; numbers and formulas skipped); build({}) and a translated build work and
      write the customer's Strict workbook back (#685: only the parts with new text are
      rewritten, with Strict names) that reads back with the translations.
  [B] xlsx refusals with a message, never a crash: a Strict namespace no edition defines;
      a workbook whose only sheets are hidden; a Strict workbook part behind Transitional
      root relationships (openpyxl reads zero sheets: the old IndexError path).
  [C] docx: a Strict document parses (body, runs, table cell); the rebuild stays Strict
      (purl names, w:conformance kept, no Transitional main namespace) and parses back
      to the translations; an unknown Strict namespace is refused.
  [D] pptx: a Strict presentation parses (slide text and alt text); build({}) keeps every
      member byte-identical; a translated rebuild rewrites only the slide, with Strict
      names; an unknown Strict namespace is refused.
  [E] Transitional packages are untouched by the Strict path (same bytes object back).
  [F] end to end: POST /api/documents takes a Strict .xlsx (5 segments) and the export
      carries the translation; an unknown-variant upload is refused 413 with the reason
      and stores nothing.
  [G] help.html says a Strict workbook comes back as Strict (#685).

Fixtures: tests/fixtures/formats/ooxml_strict/make_strict.py (hand-written Strict XML).
Hermetic + offline. Final line: "OOXML STRICT OK".
"""

import io
import os
import sys
import warnings
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ooxmlstrict_")

warnings.filterwarnings("ignore", module="openpyxl")

from app.filters import get_filter
from app.filters.base import InputLimitError
from tests.fixtures.formats.ooxml_strict import make_strict as M

PURL = b"http://purl.oclc.org/ooxml/"


def _members(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def _replace(data: bytes, name: str, old: bytes, new: bytes) -> bytes:
    m = _members(data)
    assert old in m[name], (name, old)
    m[name] = m[name].replace(old, new)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for k, v in m.items():
            zf.writestr(k, v)
    return out.getvalue()


def _refused(fn, *args) -> str:
    try:
        fn(*args)
    except InputLimitError as exc:
        return str(exc)
    raise AssertionError(f"expected a refusal from {fn}")


def _texts(fmt, data):
    return [u.text for u in get_filter(fmt).parse(data).units]


XLSX_TEXTS = ["Product", "Price", "Green tea", "Inline note", "Ship within two days"]


def test_a_xlsx_round_trip():
    f = get_filter("xlsx")
    data = M.make_xlsx()
    assert _texts("xlsx", data) == XLSX_TEXTS, _texts("xlsx", data)
    same = f.build(data, {})  # used to raise IndexError: At least one sheet must be visible
    assert _texts("xlsx", same) == XLSX_TEXTS
    tr = {str(i): "DE " + t for i, t in enumerate(XLSX_TEXTS)}
    out = f.build_localized(data, tr, "de-DE")
    assert _texts("xlsx", out) == ["DE " + t for t in XLSX_TEXTS]
    m = _members(out)
    # #685: the export patches the customer's own package, so Strict comes back Strict
    assert (
        PURL in m["xl/worksheets/sheet1.xml"]
        and m["xl/styles.xml"] == _members(data)["xl/styles.xml"]
    ), "the xlsx rebuild keeps the Strict workbook"
    from openpyxl import load_workbook

    from app.filters import ooxml_strict

    wb = load_workbook(io.BytesIO(ooxml_strict.to_transitional(out, "XLSX file")[0]))
    assert wb.sheetnames == ["Prices", "Notes"], wb.sheetnames
    ws = wb["Prices"]
    assert ws["A1"].value == "DE Product", ws["A1"].value
    assert ws["B2"].value == 4.5, ws["B2"].value
    assert ws["B3"].value == "=B2*2", ws["B3"].value
    assert wb.properties.language == "de-DE"
    print("  [A] Strict xlsx parses to 5 units; build and translated build OK (Strict out, #685)")


def test_b_xlsx_refusals():
    f = get_filter("xlsx")
    unknown = M.make_xlsx(unknown_ns=True)
    for fn in (f.parse, lambda d: f.build(d, {})):
        msg = _refused(fn, unknown)
        assert "Strict Open XML" in msg and "upload it again" in msg, msg
    hidden = _replace(
        M.make_xlsx(), "xl/workbook.xml", b'sheetId="1" ', b'sheetId="1" state="hidden" '
    )
    hidden = _replace(hidden, "xl/workbook.xml", b'sheetId="2" ', b'sheetId="2" state="hidden" ')
    # Transitional root relationships in front of a Strict workbook part: openpyxl finds the
    # workbook but reads zero sheets from it (the IndexError of #669)
    hybrid = _replace(
        M.make_xlsx(),
        "_rels/.rels",
        M.SRT.encode() + b"officeDocument",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    )
    for case in (hidden, hybrid):
        for fn in (f.parse, lambda d: f.build(d, {})):
            msg = _refused(fn, case)
            assert "no visible sheet" in msg, msg
    print("  [B] xlsx: unknown Strict namespace, all-hidden and hybrid workbooks refused")


DOCX_TEXTS = [
    "Welcome aboard",
    "⟦r1⟧Read the ⟦/r1⟧⟦r2⟧safety card⟦/r2⟧⟦r3⟧ before takeoff.⟦/r3⟧",
    "Exit row",
]


def test_c_docx():
    f = get_filter("docx")
    data = M.make_docx()
    assert _texts("docx", data) == DOCX_TEXTS, ascii(_texts("docx", data))
    units = f.parse(data).units
    tr = {str(u.p_index): u.text.replace("Welcome aboard", "Willkommen") for u in units}
    tr[str(units[2].p_index)] = "Notausgang"
    out = f.build_localized(data, tr, "de-DE")
    got = _texts("docx", out)
    assert got[0] == "Willkommen" and got[2] == "Notausgang", ascii(got)
    m, orig = _members(out), _members(data)
    doc = m["word/document.xml"]
    assert PURL + b"wordprocessingml/main" in doc and b'w:conformance="strict"' in doc, doc[:300]
    assert b"schemas.openxmlformats.org/wordprocessingml" not in doc
    assert M.SRT.encode() + b"officeDocument" in m["_rels/.rels"]
    for name, raw in m.items():
        assert b"schemas.openxmlformats.org/officeDocument/2006" not in raw, name
    assert m["docProps/app.xml"] == orig["docProps/app.xml"], "untouched member keeps its bytes"
    bad = _replace(
        data, "word/document.xml", b"<w:body>", b'<w:body xmlns:q="' + PURL + b'wordml/x">'
    )
    msg = _refused(f.parse, bad)
    assert "Strict Open XML" in msg, msg
    _refused(lambda d: f.build(d, {}), bad)
    print("  [C] Strict docx parses, rebuild stays Strict, unknown namespace refused")


def test_d_pptx():
    f = get_filter("pptx")
    data = M.make_pptx()
    assert _texts("pptx", data) == ["Spring launch", "Three new routes", "Route map"], ascii(
        _texts("pptx", data)
    )
    orig = _members(data)
    assert _members(f.build(data, {})) == orig, "an empty build keeps every member"
    out = f.build_localized(data, {"0": "Fruehjahrsstart", "2": "Streckenkarte"}, "de-DE")
    assert _texts("pptx", out) == ["Fruehjahrsstart", "Three new routes", "Streckenkarte"]
    m = _members(out)
    changed = sorted(k for k in m if m[k] != orig[k])
    assert changed == ["ppt/slides/slide1.xml"], changed
    slide = m["ppt/slides/slide1.xml"]
    assert PURL + b"presentationml/main" in slide and PURL + b"drawingml/main" in slide
    assert b"schemas.openxmlformats.org" not in slide, slide[:300]
    assert b'lang="de-DE"' in slide
    bad = _replace(
        data, "ppt/slides/slide1.xml", b"<p:cSld>", b'<p:cSld xmlns:q="' + PURL + b'pml/x">'
    )
    msg = _refused(f.parse, bad)
    assert "Strict Open XML" in msg, msg
    print("  [D] Strict pptx parses, untouched members identical, rebuild stays Strict")


def test_e_transitional_untouched():
    from app.filters import ooxml_strict
    from tests.fixtures.formats.pptx.make_pptx import make_pptx

    data = make_pptx("pptx")
    got, strict = ooxml_strict.to_transitional(data, "PowerPoint file")
    assert got is data and strict is False
    assert ooxml_strict.is_strict(M.make_xlsx()) and not ooxml_strict.is_strict(b"not a zip")
    print("  [E] Transitional packages pass through untouched")


def test_f_upload_end_to_end():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, func, select

    from app.db import engine, init_db
    from app.models import Document, Project, Segment, SegStatus, TargetLocale
    from app.services import apikeys, security, settings
    from app.services import export as export_svc

    init_db()
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    user = security.create_user("strict@x.test", "strict", "admin", "strict-password-123")
    _row, raw = apikeys.create_key(user.id, "strict-test", scopes="api docs")
    with Session(engine) as s:
        p = Project(name="strict", source_locale="en", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        pid = p.id
    from app.main import app

    client = TestClient(app)

    def post(name, body):
        return client.post(
            "/api/documents",
            headers={"X-Loc-Token": raw},
            files={"file": (name, body)},
            data={"project_id": str(pid)},
        )

    def count():
        with Session(engine) as s:
            return s.exec(select(func.count()).select_from(Document)).one()

    before = count()
    r = post("bad.xlsx", M.make_xlsx(unknown_ns=True))
    assert r.status_code == 413, (r.status_code, r.text[:300])
    assert "Strict Open XML" in r.json()["error"], r.text[:300]
    assert count() == before, "nothing stored for a refused upload"

    r = post("prices.xlsx", M.make_xlsx())
    assert r.status_code in (200, 201), (r.status_code, r.text[:300])
    with Session(engine) as s:
        doc = s.exec(select(Document).where(Document.project_id == pid)).one()
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "de-DE")
        ).all()
        assert sorted(x.source_text for x in segs) == sorted(XLSX_TEXTS), [
            x.source_text for x in segs
        ]
        for seg in segs:
            seg.target_text = "DE " + seg.source_text
            seg.status = SegStatus.MT
            s.add(seg)
        s.commit()
        out = export_svc.build_translated_file(s, doc, "de-DE")
    assert sorted(_texts("xlsx", out)) == sorted("DE " + t for t in XLSX_TEXTS)
    print("  [F] Strict xlsx upload ingests 5 segments and exports; unknown variant 413")


def test_g_help_says_so():
    # Gate (#669): the help page tells the user a Strict workbook comes back Transitional,
    # while Strict documents/presentations stay Strict.
    with open(os.path.join(_ROOT, "app", "templates", "help.html"), encoding="utf-8") as fh:
        html = " ".join(fh.read().split())
    assert "comes back as Strict Open XML (PowerPoint .pptx too)" in html
    assert "A workbook saved as Strict Open XML is read and comes back as Strict" in html
    print("  [G] help page states the Strict output rule for xlsx and docx/pptx")


if __name__ == "__main__":
    test_a_xlsx_round_trip()
    test_b_xlsx_refusals()
    test_c_docx()
    test_d_pptx()
    test_e_transitional_untouched()
    test_f_upload_end_to_end()
    test_g_help_says_so()
    print("OOXML STRICT OK")
