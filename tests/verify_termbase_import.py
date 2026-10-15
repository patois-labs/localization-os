"""Termbase import: MultiTerm XML + TBX v2 (2002 default dialect) + TBX-Basic + TBX v3 -- hermetic.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys (MockProvider). ZERO network.

Route C of the connector parity program (RESEARCH-G3, MultiTerm and Quickterm sections):
Trados-heavy LSPs hand out termbases as MultiTerm XML (<mtf><conceptGrp>) or as
MultiTerm's TBX export, which is the TBX 2002 DEFAULT dialect (martif type="TBX"), not
TBX-Basic. Format references:
  - MultiTerm XML schema (RWS):
    https://developers.rws.com/multiterm-api-docs/apiconcepts/MTF/MTFElements.html
  - TBX dialects (TBX-Core / TBX-Basic / TBX-Min, ISO 30042):
    https://www.tbxinfo.net/tbx-dialects/
  - MultiTerm exports TBX 2002 default dialect, not TBX-Basic (RWS community thread):
    https://community.rws.com/product-groups/trados-portfolio/trados-studio/f/multiterm/42438/how-do-i-export-or-convert-a-multiterm-termbase-into-tbx-basic

Fixtures are HANDCRAFTED miniatures with the real-world quirks: UTF-16 LE with a BOM and
an encoding="UTF-16" declaration, upper/mixed-case language codes (EN-US, De-de), a
<language> with only a NAME (no lang code), several terms per language, forbidden and
deprecated terms in every status vocabulary (administrativeStatus, normativeAuthorization,
a MultiTerm "Status" picklist field), nested descripGrps and transacGrps.

Covers:
  - each dialect maps definition / part of speech / subject / context / notes
  - a forbidden or deprecated term is NEVER the target and never a source row; it is kept
    (notes on a per-project glossary; a forbidden/deprecated GlossaryTerm on the concept of
    a module glossary)
  - per-entry report: imported / merged / skipped with reason; a re-import is all merged
  - hostile XML: entity bomb (UTF-8 and UTF-16), external entity, deep nesting, oversize,
    too many entries -> clean TbxError, never a traceback
  - surfaces: web (both import routes + the report on the page), CLI (local), /api (JSON
    report), format detection for .xml / .mtf

Final line: "TERMBASE IMPORT OK".
"""

import base64
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_termbase_")

from sqlmodel import Session, select

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import Customer, GlossaryEntry, GlossaryTerm, Project
from app.services import apikeys, glossary, glossary_tbx, security
from app.services import glossary_module as gm

SRC, TGT = "en-US", "de-DE"

# ---------------------------------------------------------------------------
# MultiTerm XML. Entry 1: definition at concept level, POS + context per term, a
# FORBIDDEN German synonym listed FIRST (so "first target wins" would pick it), a
# transacGrp. Entry 2: language given by NAME only ("German"), two English synonyms,
# one DEPRECATED. Entry 3: English only -> skipped. Entry 4: every German term forbidden
# -> skipped with that reason. Entry 5: a note nested two descripGrps deep.
_MTF = """<?xml version="1.0" encoding="UTF-16"?>
<mtf>
  <conceptGrp>
    <concept>101</concept>
    <transacGrp><transac type="origination">alex</transac><date>2026-10-01T10:00:00</date></transacGrp>
    <descripGrp><descrip type="Definition">To start an authenticated session.</descrip></descripGrp>
    <descripGrp><descrip type="Subject">Software UI</descrip></descripGrp>
    <languageGrp>
      <language type="English" lang="EN-US"/>
      <termGrp>
        <term>log in</term>
        <descripGrp><descrip type="Part of Speech">verb</descrip></descripGrp>
        <descripGrp><descrip type="Context">Log in with your company account.</descrip></descripGrp>
        <transacGrp><transac type="modification">alex</transac><date>2026-10-02T10:00:00</date></transacGrp>
      </termGrp>
    </languageGrp>
    <languageGrp>
      <language type="German" lang="De-de"/>
      <termGrp>
        <term>einloggen</term>
        <descripGrp><descrip type="Status">Forbidden</descrip></descripGrp>
      </termGrp>
      <termGrp>
        <term>anmelden</term>
        <descripGrp><descrip type="Status">Preferred</descrip></descripGrp>
      </termGrp>
    </languageGrp>
  </conceptGrp>
  <conceptGrp>
    <concept>102</concept>
    <languageGrp>
      <language type="English" lang="EN"/>
      <termGrp><term>sign out</term></termGrp>
      <termGrp><term>logoff</term><descripGrp><descrip type="Usage Status">Deprecated</descrip></descripGrp></termGrp>
    </languageGrp>
    <languageGrp>
      <language type="German"/>
      <termGrp><term>abmelden</term></termGrp>
    </languageGrp>
  </conceptGrp>
  <conceptGrp>
    <concept>103</concept>
    <languageGrp><language type="English" lang="EN-US"/><termGrp><term>orphan</term></termGrp></languageGrp>
  </conceptGrp>
  <conceptGrp>
    <concept>104</concept>
    <languageGrp><language type="English" lang="EN-US"/><termGrp><term>hack</term></termGrp></languageGrp>
    <languageGrp><language type="German" lang="DE-DE"/>
      <termGrp><term>hacken</term><descripGrp><descrip type="Status">Do not use</descrip></descripGrp></termGrp>
    </languageGrp>
  </conceptGrp>
  <conceptGrp>
    <concept>105</concept>
    <languageGrp><language type="English" lang="EN-US"/>
      <termGrp><term>dashboard</term>
        <descripGrp><descrip type="Note">UI label</descrip><descripGrp><descrip type="Source">style guide</descrip></descripGrp></descripGrp>
      </termGrp>
    </languageGrp>
    <languageGrp><language type="German" lang="DE-DE"/><termGrp><term>Dashboard</term></termGrp></languageGrp>
  </conceptGrp>
</mtf>
"""  # noqa: E501
_MTF_UTF16 = _MTF.encode("utf-16")  # Python's utf-16 codec writes the BOM (LE on this host)

# TBX 2002 default dialect as MultiTerm exports it: martif type="TBX", xml:lang EN-US, field
# labels as descrip types, a tig and an ntig in one langSet, administrativeStatus and
# normativeAuthorization statuses, a <note>.
_TBX2_MULTITERM = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE martif SYSTEM "TBXcoreStructV02.dtd">
<martif type="TBX" xml:lang="EN-US">
<martifHeader><fileDesc><sourceDesc><p>MultiTerm export fixture</p></sourceDesc></fileDesc></martifHeader>
<text><body>
<termEntry id="c1">
  <descrip type="Definition">A visual control that toggles a setting.</descrip>
  <langSet xml:lang="EN-US">
    <tig><term>switch</term><termNote type="partOfSpeech">noun</termNote><descrip type="context">Turn the switch on.</descrip><note>Use for binary settings.</note></tig>
  </langSet>
  <langSet xml:lang="DE-DE">
    <tig><term>Knopf</term><termNote type="administrativeStatus">deprecatedTerm-admn-sts</termNote></tig>
    <ntig><termGrp><term>Schalter</term><termNote type="normativeAuthorization">preferredTerm</termNote></termGrp></ntig>
    <tig><term>Wippe</term><termNote type="normativeAuthorization">admittedTerm</termNote></tig>
  </langSet>
</termEntry>
<termEntry id="c2">
  <langSet xml:lang="en-us"><tig><term>toggle</term></tig></langSet>
  <langSet xml:lang="de"><tig><term>umschalten</term></tig></langSet>
</termEntry>
</body></text></martif>
"""  # noqa: E501

# TBX-Basic (v2, martif type="TBX-Basic") with a forbidden source synonym.
_TBX_BASIC = b"""<?xml version="1.0" encoding="UTF-8"?>
<martif type="TBX-Basic" xml:lang="en">
<text><body>
<termEntry id="b1">
  <descrip type="subjectField">Finance</descrip>
  <langSet xml:lang="en">
    <tig><term>invoice</term><termNote type="administrativeStatus">preferredTerm-admn-sts</termNote></tig>
    <tig><term>bill</term><termNote type="administrativeStatus">deprecatedTerm-admn-sts</termNote></tig>
  </langSet>
  <langSet xml:lang="de"><tig><term>Rechnung</term></tig></langSet>
</termEntry>
</body></text></martif>
"""  # noqa: E501

# TBX v3 (ISO 30042:2019) with the default namespace and a superseded target.
_TBX_V3 = b"""<?xml version="1.0" encoding="UTF-8"?>
<tbx type="TBX-Basic" style="dca" xml:lang="en" xmlns="urn:iso:std:iso:30042:ed-2">
<text><body>
<conceptEntry id="v1">
  <langSec xml:lang="en-US"><termSec><term>folder</term><termNote type="partOfSpeech">noun</termNote></termSec></langSec>
  <langSec xml:lang="de-DE">
    <termSec><term>Mappe</term><termNote type="administrativeStatus">supersededTerm-admn-sts</termNote></termSec>
    <termSec><term>Ordner</term><termNote type="administrativeStatus">preferredTerm-admn-sts</termNote></termSec>
  </langSec>
</conceptEntry>
</body></text></tbx>
"""  # noqa: E501

_N = 0


def _project():
    with Session(engine) as s:
        p = Project(name="Termbase Proj", source_locale=SRC)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _glossary(name="Termbase GL"):
    global _N
    _N += 1
    with Session(engine) as s:
        c = Customer(name=f"Termbase Cust {_N}")
        s.add(c)
        s.commit()
        s.refresh(c)
        g = gm.create(s, f"{name} {_N}", customer_id=c.id)
        gid = g.id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
        return gid


def _terms(project_id=None, glossary_id=None):
    with Session(engine) as s:
        q = select(GlossaryEntry)
        q = (
            q.where(GlossaryEntry.glossary_id == glossary_id)
            if glossary_id is not None
            else q.where(GlossaryEntry.project_id == project_id)
        )
        return {e.source_term: (e.target_term, e.dnt, e.meta or {}) for e in s.exec(q).all()}


def _import(pid, data, filename=None, src=SRC, tgt=TGT):
    with Session(engine) as s:
        return glossary.import_bytes(s, pid, data, src, tgt, filename=filename)


def _gimport(gid, data, filename=None):
    with Session(engine) as s:
        return gm.import_bytes(s, gid, data, SRC, TGT, filename=filename)


def _expect_tbx_error(data, needle):
    try:
        rows, _stats = glossary_tbx.parse_rows(data, SRC, TGT)
        list(rows)
    except glossary_tbx.TbxError as exc:
        assert needle in str(exc), (needle, str(exc))
        return
    raise AssertionError(f"expected a TbxError containing {needle!r}")


# ===========================================================================
def test_language_codes():
    n = glossary_tbx.normalize_lang
    assert n("EN-US") == "en-US" and n("en_us") == "en-US" and n("De-de") == "de-DE"
    assert n("ZH-HANS-CN") == "zh-Hans-CN" and n("es-419") == "es-419" and n("") == ""
    assert n("und-x-ANYSRC") == "und-x-anysrc"
    import xml.etree.ElementTree as ET

    def lang(**attrs):
        return glossary_tbx.mtf_lang(ET.Element("language", attrs))

    assert lang(type="German", lang="DE-CH") == "de-CH"
    assert lang(type="German") == "de"
    assert lang(type="German (Switzerland)") == "de-CH"
    assert lang(type="Spanish (Latin America)") == "es-419"
    assert lang(type="Klingon") == ""
    s = glossary_tbx.status_of
    assert s("Forbidden") == "forbidden" and s("Not allowed") == "forbidden"
    assert s("deprecatedTerm-admn-sts") == "deprecated" and s("supersededTerm") == "deprecated"
    assert s("admittedTerm-admn-sts") == "admitted" and s("preferredTerm") == "preferred"
    assert s("Approved") == "preferred" and s("whatever") is None
    print("  language codes + status vocabularies OK")


def test_multiterm_xml_utf16_legacy_project():
    assert _MTF_UTF16[:2] in (b"\xff\xfe", b"\xfe\xff"), "fixture must carry a BOM"
    pid = _project()
    res = _import(pid, _MTF_UTF16, filename="termbase.xml")
    assert res["format"] == "tbx" and res["dialect"] == "multiterm-xml", res
    rep = res["report"]
    assert rep["entries"] == 5 and rep["imported"] == 3 and rep["skipped"] == 2, rep
    assert rep["merged"] == 0, rep
    reasons = rep["skip_reasons"]
    assert reasons.get(f"no {TGT} term") == 1, reasons
    assert reasons.get(f"every {TGT} term is forbidden or deprecated") == 1, reasons
    by_id = {d["id"]: d for d in rep["entries_detail"]}
    assert by_id["103"]["action"] == "skipped" and by_id["101"]["action"] == "imported", by_id
    t = _terms(project_id=pid)
    # forbidden "einloggen" is listed FIRST but the preferred "anmelden" is the target
    assert t["log in"][0] == "anmelden", t
    meta = t["log in"][2]
    assert meta["definition"] == "To start an authenticated session.", meta
    assert meta["part_of_speech"] == "verb" and meta["subject_field"] == "Software UI", meta
    assert meta["usage_example"] == "Log in with your company account.", meta
    assert "einloggen" in meta["notes"] and "forbidden" in meta["notes"], meta
    # name-only <language type="German"> mapped; the DEPRECATED source synonym makes no row
    assert t["sign out"][0] == "abmelden" and "logoff" not in t, t
    assert "logoff" in t["sign out"][2]["notes"], t["sign out"]
    assert "hack" not in t and "orphan" not in t
    assert t["dashboard"][2]["notes"] == "UI label", t["dashboard"]
    # re-import: every imported entry now MERGES into the existing terms
    res2 = _import(pid, _MTF_UTF16, filename="termbase.xml")
    assert res2["report"]["imported"] == 0 and res2["report"]["merged"] == 3, res2["report"]
    assert res2["changed"] == 0, res2
    print("  MultiTerm XML (UTF-16 BOM, name-only language, forbidden first) OK")


def test_tbx2_multiterm_default_dialect():
    pid = _project()
    res = _import(pid, _TBX2_MULTITERM, filename="export.tbx")
    assert res["dialect"] == "tbx-v2", res
    t = _terms(project_id=pid)
    assert t["switch"][0] == "Schalter", t  # deprecated Knopf skipped, preferred wins over Wippe
    m = t["switch"][2]
    assert m["definition"].startswith("A visual control") and m["part_of_speech"] == "noun", m
    assert m["usage_example"] == "Turn the switch on.", m
    assert "Use for binary settings." in m["notes"] and "Knopf" in m["notes"], m
    assert t["toggle"][0] == "umschalten", t  # lower-case en-us + base "de" both resolve
    assert res["report"]["status_terms"] == 1, res["report"]
    print("  TBX 2002 default dialect (MultiTerm export) OK")


def test_tbx_basic_and_v3_module_glossary_keeps_status():
    gid = _glossary()
    res = _gimport(gid, _TBX_BASIC, filename="basic.tbx")
    assert res["report"]["imported"] == 1, res["report"]
    t = _terms(glossary_id=gid)
    assert set(t) == {"invoice"} and t["invoice"][0] == "Rechnung", t
    assert t["invoice"][2]["subject_field"] == "Finance", t
    res = _gimport(gid, _TBX_V3, filename="v3.tbx")
    assert res["dialect"] == "tbx-v3" and res["report"]["status_terms_kept"] == 1, res
    t = _terms(glossary_id=gid)
    assert t["folder"][0] == "Ordner", t
    with Session(engine) as s:
        terms = {(x.locale, x.term): x.status for x in s.exec(select(GlossaryTerm)).all()}
    assert terms.get((SRC, "bill")) == "deprecated", terms
    assert terms.get((TGT, "Mappe")) == "deprecated", terms
    assert terms.get((TGT, "Ordner")) in ("preferred", "admitted"), terms
    # MultiTerm into a module glossary: the forbidden target is a FORBIDDEN concept term
    res = _gimport(gid, _MTF_UTF16, filename="termbase.mtf")
    assert res["report"]["status_terms_kept"] >= 2, res["report"]
    with Session(engine) as s:
        terms = {(x.locale, x.term): x.status for x in s.exec(select(GlossaryTerm)).all()}
    assert terms.get((TGT, "einloggen")) == "forbidden", terms
    assert terms.get((SRC, "logoff")) == "deprecated", terms
    assert _terms(glossary_id=gid)["log in"][0] == "anmelden"
    # idempotent: the same file again changes nothing and keeps the statuses
    res = _gimport(gid, _MTF_UTF16, filename="termbase.mtf")
    assert res["changed"] == 0 and res["report"]["merged"] == 3, res
    print("  TBX-Basic + TBX v3 + MultiTerm into a module glossary keep forbidden/deprecated OK")


def test_hostile_xml():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE mtf [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b"<mtf><conceptGrp><concept>1</concept><languageGrp><language type='English' lang='EN'/>"
        b"<termGrp><term>&b;</term></termGrp></languageGrp></conceptGrp></mtf>"
    )
    _expect_tbx_error(bomb, "declares XML entities")
    _expect_tbx_error(bomb.decode().encode("utf-16"), "declares XML entities")
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE mtf [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<mtf><conceptGrp><languageGrp><language type='English' lang='EN'/>"
        b"<termGrp><term>&x;</term></termGrp></languageGrp></conceptGrp></mtf>"
    )
    _expect_tbx_error(xxe, "declares XML entities")
    deep = (
        b"<mtf><conceptGrp><descripGrp>"
        + b"<descripGrp>" * 20000
        + b"</descripGrp>" * 20000
        + b"</descripGrp></conceptGrp></mtf>"
    )
    try:
        rows, _ = glossary_tbx.parse_rows(deep, SRC, TGT)
        list(rows)
    except glossary_tbx.TbxError:
        pass  # nested too deeply / unreadable: a clean error either way
    _expect_tbx_error(b"<mtf><conceptGrp><concept>1", "not readable XML")
    _expect_tbx_error(b"<mtf></mtf>", "no TBX entries found")
    old = config.MAX_UPLOAD_BYTES
    config.MAX_UPLOAD_BYTES = 100
    try:
        _expect_tbx_error(_TBX_BASIC, "upload limit")
    finally:
        config.MAX_UPLOAD_BYTES = old
    old_rows = config.MAX_DOCUMENT_ROWS
    config.MAX_DOCUMENT_ROWS = 2
    try:
        many = b"<mtf>" + b"<conceptGrp><concept>1</concept></conceptGrp>" * 5 + b"</mtf>"
        _expect_tbx_error(many, "MAX_DOCUMENT_ROWS")
    finally:
        config.MAX_DOCUMENT_ROWS = old_rows
    print("  hostile XML (entity bombs, XXE, deep nesting, size/entry caps) refused OK")


def test_format_detection():
    assert glossary.detect_format(b"x", "termbase.xml") == glossary.TBX_FORMAT
    assert glossary.detect_format(b"x", "termbase.MTF") == glossary.TBX_FORMAT
    assert glossary.detect_format(_MTF_UTF16, None) == glossary.TBX_FORMAT
    assert glossary.detect_format(b"source_term,target_term\n", "a.csv") == glossary.CSV_FORMAT
    print("  format detection (.xml / .mtf / UTF-16 sniff) OK")


def test_surfaces():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    # web: per-project route + module route, report on the redirected page
    pid = _project()
    r = c.post(
        f"/projects/{pid}/glossary/import",
        data={"source_locale": SRC, "target_locale": TGT},
        files={"file": ("termbase.xml", _MTF_UTF16, "application/xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "gl_dialect=multiterm-xml" in loc and "gl_imported=3" in loc and "gl_status=" in loc, loc
    page = c.get(loc)
    assert page.status_code == 200 and "Termbase (multiterm-xml)" in page.text, page.text[:300]
    gid = _glossary("Web")
    r = c.post(
        f"/glossaries/{gid}/import",
        data={"source_locale": SRC, "target_locale": TGT},
        files={"file": ("export.tbx", _TBX2_MULTITERM, "application/x-tbx+xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "gl_dialect=tbx-v2" in r.headers["location"], r.headers
    page = c.get(r.headers["location"])
    assert "Termbase (tbx-v2)" in page.text and "do-not-use" in page.text, page.text[:300]

    # CLI (local): report lines, ASCII only
    pid2 = _project()
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "termbase.xml")
        with open(path, "wb") as fh:
            fh.write(_MTF_UTF16)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["glossary", "import", str(pid2), path, "--source", SRC, "--target", TGT])
        out = buf.getvalue()
    assert rc == 0, out
    assert out.isascii(), out
    assert "3 imported, 0 merged into existing terms, 2 skipped" in out, out
    assert "entry 103 'orphan' skipped: no de-DE term" in out, out
    assert "forbidden/deprecated" in out, out

    # /api: the JSON carries the full report; filename-less bytes are sniffed
    u = security.create_user("termbase-admin@x.com", "Admin", "admin", "tb-pw-123456")
    _row, raw = apikeys.create_key(u.id, "termbase", scopes="api docs")
    pid3 = _project()
    r = c.post(
        f"/api/projects/{pid3}/glossary/import",
        headers={"X-Loc-Token": raw},
        json={"csv_b64": base64.b64encode(_MTF_UTF16).decode(), "source": SRC, "target": TGT},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dialect"] == "multiterm-xml" and body["report"]["imported"] == 3, body
    assert any(d["action"] == "skipped" for d in body["report"]["entries_detail"]), body
    r = c.post(
        f"/api/projects/{pid3}/glossary/import",
        headers={"X-Loc-Token": raw},
        json={
            "csv_b64": base64.b64encode(b"<mtf><conceptGrp>").decode(),
            "source": SRC,
            "target": TGT,
        },
    )
    assert r.status_code == 400, r.text
    print("  surfaces: web (both routes + report), CLI, /api OK")


def _tbx_one(de_terms, note=""):
    """A one-entry TBX: en-US 'log in' and the given [(de term, status or '')]."""
    tigs = "".join(
        f"<tig><term>{t}</term>"
        + (f'<termNote type="administrativeStatus">{st}</termNote>' if st else "")
        + "</tig>"
        for t, st in de_terms
    )
    note_xml = f"<note>{note}</note>" if note else ""
    return (
        '<martif type="TBX"><text><body><termEntry id="c1">'
        '<langSet xml:lang="en-US"><tig><term>log in</term></tig></langSet>'
        f'<langSet xml:lang="de-DE">{tigs}</langSet>{note_xml}</termEntry></body></text></martif>'
    ).encode()


def test_gate_findings():
    """Review gate 2026-10-10: negated status words, curated notes never clobbered by the
    generated do-not-use line, no demotion of a term a binding still enforces, report
    field caps, BCP 47 extension casing."""
    s = glossary_tbx.status_of
    assert s("Disallowed") == "forbidden" and s("Not permitted") == "forbidden", s("Disallowed")
    assert s("Unacceptable") == "forbidden" and s("Illegal") == "forbidden"
    assert s("Not recommended") == "deprecated" and s("Not preferred") == "deprecated"
    assert s("Not approved") is None and s("Unapproved") is None and s("Non-standard") is None
    assert s("Legal") == "preferred" and s("Approved") == "preferred"
    assert glossary_tbx.normalize_lang("EN-u-CA-Gregory") == "en-u-ca-gregory"
    # a "Disallowed" term listed first is never the target; "Not approved" never outranks
    # an admitted term
    rows, _ = glossary_tbx.parse_rows(
        _tbx_one([("einloggen", "Disallowed"), ("anmelden", "")]), SRC, TGT
    )
    assert [r["target_term"] for r in rows] == ["anmelden"], rows
    rows, _ = glossary_tbx.parse_rows(
        _tbx_one([("einloggen", "Not approved"), ("anmelden", "admittedTerm-admn-sts")]), SRC, TGT
    )
    rows = list(rows)
    assert rows[0]["target_term"] == "einloggen", rows  # unranked == first usable, as before
    # report fields are capped (a multi-MB id attribute must not ride in the API reply)
    big = _tbx_one([("anmelden", "")]).replace(b'id="c1"', b'id="' + b"A" * 50_000 + b'"')
    rows, st = glossary_tbx.parse_rows(big, SRC, TGT)
    list(rows)
    assert len(st["report"][0]["id"]) == glossary_tbx.REPORT_FIELD_CAP, len(st["report"][0]["id"])
    # per-project: a curated note survives a re-import whose entry has a forbidden term
    # but no <note>; a file note replaces it (file wins) with the do-not-use line appended
    pid = _project()
    with Session(engine) as s2:
        glossary.add_term(s2, pid, "log in", "einloggen", SRC, TGT, meta={"notes": "Curated"})
        s2.commit()
    _import(pid, _tbx_one([("einloggen", "forbidden"), ("anmelden", "")]), "a.tbx")
    t = _terms(project_id=pid)
    assert t["log in"][0] == "anmelden" and t["log in"][2]["notes"] == "Curated", t
    _import(
        pid, _tbx_one([("einloggen", "forbidden"), ("anmelden", "")], note="From file"), "a.tbx"
    )
    notes = _terms(project_id=pid)["log in"][2]["notes"]
    assert notes.startswith("From file; do not use (de-DE, forbidden): einloggen"), notes
    # module: the generated line never lands in notes; the forbidden term is on the concept
    gid = _glossary("Gate GL")
    with Session(engine) as s2:
        gm.import_bytes(
            s2,
            gid,
            b"source_term,target_term,notes\nlog in,einloggen,Curated\n",
            SRC,
            TGT,
            filename="a.csv",
        )
    res = _gimport(gid, _tbx_one([("einloggen", "Disallowed"), ("anmelden", "")]), "a.tbx")
    t = _terms(glossary_id=gid)
    assert t["log in"][0] == "anmelden" and t["log in"][2].get("notes") == "Curated", t
    assert res["report"]["status_terms_kept"] == 1, res["report"]
    with Session(engine) as s2:
        st = {
            x.term: x.status
            for x in s2.exec(select(GlossaryTerm).where(GlossaryTerm.locale == TGT)).all()
        }
    assert st.get("einloggen") == "forbidden" and st.get("anmelden") == "preferred", st
    # a surface some binding still ENFORCES is never demoted in place. de-CH is bound to
    # the de-DE term "speichern" by base fallback; a de-DE import that prefers "sichern"
    # and marks "speichern" forbidden must not leave the de-CH binding enforcing a
    # forbidden term (record_status_terms does not re-project).
    gid2 = _glossary("Gate GL2")
    with Session(engine) as s2:
        gm.import_bytes(
            s2, gid2, b"source_term,target_term\nsave,speichern\n", SRC, TGT, filename="a.csv"
        )
    with Session(engine) as s2:
        gm.set_pairs(s2, gid2, [f"{SRC}:{TGT}", f"{SRC}:de-CH"])  # widening binds de-CH
    with Session(engine) as s2:
        ch = s2.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid2, GlossaryEntry.target_locale == "de-CH"
            )
        ).first()
        ch_tid = ch.target_term_id if ch else None
    assert ch_tid is not None, "fixture: de-CH binding must exist"
    data = (
        b'<martif><text><body><termEntry><langSet xml:lang="en-US"><tig><term>save</term></tig>'
        b'</langSet><langSet xml:lang="de-DE"><tig><term>speichern</term>'
        b'<termNote type="administrativeStatus">forbidden</termNote></tig>'
        b"<tig><term>sichern</term></tig></langSet></termEntry></body></text></martif>"
    )
    _gimport(gid2, data, "b.tbx")
    with Session(engine) as s2:
        ch = s2.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid2, GlossaryEntry.target_locale == "de-CH"
            )
        ).first()
        bound = s2.get(GlossaryTerm, ch.target_term_id) if ch.target_term_id else None
    assert bound is None or bound.status in ("preferred", "admitted"), (
        bound.term,
        bound.status,
        ch.target_term,
    )
    print("  gate findings (negation, curated notes, live bindings, caps) OK")


def main():
    init_db()
    test_gate_findings()
    test_language_codes()
    test_multiterm_xml_utf16_legacy_project()
    test_tbx2_multiterm_default_dialect()
    test_tbx_basic_and_v3_module_glossary_keeps_status()
    test_hostile_xml()
    test_format_detection()
    test_surfaces()
    print("TERMBASE IMPORT OK")


if __name__ == "__main__":
    main()
