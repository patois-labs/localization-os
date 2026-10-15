"""Native TBX (TermBase eXchange) glossary import/export verification -- hermetic.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys (MockProvider). ZERO network.

The glossary bulk seams were CSV-only; a termbase in the real world arrives as .tbx
(Microsoft Terminology Collection, IATE, every serious TMS). This suite covers the
format layer (app/services/glossary_tbx.py), the shared parse->apply seam it plugs
into (glossary._apply_rows / glossary.import_bytes), and EVERY surface that had a
CSV seam: legacy project + module glossary x web + CLI + /api x import + export.

Fixtures are HANDCRAFTED miniatures modelled on the structure of the two real files
on disk (corpus-data/glossaries/microsoft/SPANISH.tbx and
iate/extracted/IATE_export_26022019.tbx) -- same element nesting, same descrip /
termNote types, our own invented terms.

Covers:

  dialects
    - TBX 2.0/martif, Microsoft shape: ntig/termGrp, <descrip type="definition"> on
      the source langSet, <termNote type="partOfSpeech"> inside the term group
    - TBX 2.0/martif, IATE shape: tig, <descrip type="subjectField"> at the ENTRY
      level above the langSets, many sibling languages
    - TBX v3/TBX-Core: conceptEntry/langSec/termSec -- and the SAME file carrying the
      ISO default namespace parses identically (local-name matching)

  language-pair selection
    - exact xml:lang wins; base-language fallback (langSet "es" serves "es-ES", on
      BOTH sides); an entry missing either side is skipped and COUNTED; source and
      target collapsing onto one section is skipped, never a term mapped to itself

  metadata mapping + precedence
    - partOfSpeech/definition/subjectField land in GlossaryEntry.meta; unknown notes
      (geographicalUsage, grammaticalNumber, reliabilityCode) are ignored; narrowest
      level wins (term > source langSet > entry)

  shape rules
    - multiple source terms in the source langSet -> one row EACH; the target's FIRST
      term wins; dnt is always false and no senses are created

  refusals (clean errors, never a traceback)
    - malformed XML, a non-TBX XML document, empty bytes, an oversize file, and a
      document DECLARING XML entities (billion-laughs guard)

  CSV regression
    - the CSV arm through the new dispatcher is unchanged (senses, dnt, meta), and
      format detection follows extension-then-sniff

  export + round-trip
    - export_tbx emits TBX v3; import -> export -> re-import reproduces every term,
      target and mapped meta cell

  seams (parity)
    - web:  POST /projects/{id}/glossary/import + /glossaries/{id}/import with a .tbx
            upload; GET both exports with ?format=tbx
    - CLI:  `glossary import` / `glossary module import` on a .tbx path;
            `glossary export` / `glossary module export` to a .tbx path
    - /api: POST /api/projects/{pid}/glossary/import and /api/glossaries/{gid}/import
            carrying TBX bytes (with and WITHOUT the filename hint -> the sniff path
            a remote CLI depends on); GET /api/glossaries/{gid}/export?format=tbx;
            a malformed TBX -> 400 with a message, never a 500

Final line: "GLOSSARY TBX OK".
"""

import base64
import io
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_glosstbx_")

from sqlmodel import Session, select

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Customer,
    Glossary,
    GlossaryEntry,
    GlossarySense,
    Project,
)
from app.services import (
    apikeys,
    glossary,
    glossary_concepts,
    glossary_tbx,
    security,
)
from app.services import glossary_module as gm

SRC, TGT = "en-US", "es-ES"


# ===========================================================================
# Fixtures -- miniatures of the two real dialects.
# ===========================================================================
# TBX 2.0 / martif, MICROSOFT shape. 5 termEntry elements:
#   1 widget      -> definition (source langSet) + partOfSpeech (term group)
#   2 sign in     -> plain pair, no metadata
#   3 log on/logon-> TWO source terms in one langSet (one row each)
#   4 orphan      -> en-US only            -> skipped
#   5 huerfano    -> es + fr, no en-US     -> skipped
_MS_TBX = b"""<?xml version="1.0" encoding="utf-8"?>
<martif type="TBX" xml:lang="en-US"><martifHeader><fileDesc><titleStmt><title>Fixture Terminology Collection Export</title></titleStmt><sourceDesc><p>Handcrafted TBX 2.0 fixture</p></sourceDesc></fileDesc></martifHeader><text><body>
<termEntry id="1_11444_11446"><langSet xml:lang="en-US"><descripGrp><descrip type="definition">A small user-interface control.</descrip></descripGrp><ntig><termGrp><term id="11444">widget</term><termNote type="partOfSpeech">Noun</termNote><termNote type="grammaticalNumber">Singular</termNote></termGrp></ntig></langSet><langSet xml:lang="es"><ntig><termGrp><term id="11446">control</term><termNote type="geographicalUsage">MEX, ESP, ARG</termNote><termNote type="partOfSpeech">Noun</termNote><termNote type="grammaticalGender">Masculine</termNote></termGrp></ntig></langSet></termEntry>
<termEntry id="2_1_2"><langSet xml:lang="en-US"><ntig><termGrp><term id="21">sign in</term></termGrp></ntig></langSet><langSet xml:lang="es"><ntig><termGrp><term id="22">iniciar sesion</term></termGrp></ntig></langSet></termEntry>
<termEntry id="3_1_2"><langSet xml:lang="en-US"><ntig><termGrp><term id="31">log on</term><termNote type="partOfSpeech">Verb</termNote></termGrp></ntig><ntig><termGrp><term id="32">logon</term></termGrp></ntig></langSet><langSet xml:lang="es"><ntig><termGrp><term id="33">iniciar sesion</term></termGrp></ntig></langSet></termEntry>
<termEntry id="4_1"><langSet xml:lang="en-US"><ntig><termGrp><term id="41">orphan</term></termGrp></ntig></langSet></termEntry>
<termEntry id="5_1"><langSet xml:lang="es"><ntig><termGrp><term id="51">huerfano</term></termGrp></ntig></langSet><langSet xml:lang="fr"><ntig><termGrp><term id="52">orphelin</term></termGrp></ntig></langSet></termEntry>
</body></text></martif>
"""  # noqa: E501

# TBX 2.0 / martif, IATE shape: <tig>, entry-level <descrip type="subjectField">,
# many sibling languages, reliabilityCode noise. en/fr sections carry NO region, so
# importing with en-GB -> fr-FR exercises the base-language fallback on BOTH sides.
_IATE_TBX = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE martif SYSTEM "TBXcoreStructV02.dtd">
<martif type="TBX-Default" xml:lang="en">
  <martifHeader><fileDesc><sourceDesc><p>Handcrafted IATE-shaped fixture.</p></sourceDesc></fileDesc>
    <encodingDesc><p type="XCSURI">TBXXCS.xcs</p></encodingDesc></martifHeader>
  <text>
    <body>
      <termEntry id="FIX-1">
        <descripGrp><descrip type="subjectField">1011</descrip></descripGrp>
        <langSet xml:lang="bg"><tig><term>компетентност</term><termNote type="termType">fullForm</termNote></tig></langSet>
        <langSet xml:lang="en"><tig><term>competence of the Member States</term><termNote type="termType">fullForm</termNote><descrip type="reliabilityCode">3</descrip></tig></langSet>
        <langSet xml:lang="fr"><tig><term>competence des Etats membres</term><descrip type="reliabilityCode">3</descrip></tig></langSet>
      </termEntry>
      <termEntry id="FIX-2">
        <langSet xml:lang="de"><tig><term>Zustaendigkeit</term></tig></langSet>
        <langSet xml:lang="en"><tig><term>supply agency</term></tig></langSet>
        <langSet xml:lang="fr"><tig><term>agence d'approvisionnement</term></tig></langSet>
      </termEntry>
      <termEntry id="FIX-3">
        <descripGrp><descrip type="subjectField">0421</descrip></descripGrp>
        <langSet xml:lang="bg"><tig><term>само на български</term></tig></langSet>
        <langSet xml:lang="cs"><tig><term>jen cesky</term></tig></langSet>
      </termEntry>
      <termEntry id="FIX-4">
        <langSet xml:lang="en"><tig><term>english only</term></tig></langSet>
      </termEntry>
      <termEntry id="FIX-5">
        <langSet xml:lang="en"><tig><term>lorry</term></tig><tig><term>truck</term></tig></langSet>
        <langSet xml:lang="fr"><tig><term>camion</term></tig><tig><term>poids lourd</term></tig></langSet>
      </termEntry>
    </body>
  </text>
</martif>
""".encode()  # non-ASCII terms on purpose: a real termbase is not ASCII  # noqa: E501

# TBX v3 / TBX-Core. Also the shape our exporter emits.
_V3_BODY = """
      <conceptEntry id="c1">
        <descrip type="subjectField">software</descrip>
        <langSec xml:lang="en-US">
          <termSec>
            <term>dashboard</term>
            <termNote type="partOfSpeech">noun</termNote>
            <descrip type="definition">A single-screen summary view.</descrip>
          </termSec>
        </langSec>
        <langSec xml:lang="es-ES"><termSec><term>panel</term></termSec></langSec>
      </conceptEntry>
      <conceptEntry id="c2">
        <langSec xml:lang="en-US"><termSec><term>toolbar</term></termSec></langSec>
        <langSec xml:lang="es-ES"><termSec><term>barra de herramientas</term></termSec></langSec>
      </conceptEntry>
      <conceptEntry id="c3">
        <langSec xml:lang="de-DE"><termSec><term>Werkzeugleiste</term></termSec></langSec>
        <langSec xml:lang="es-ES"><termSec><term>solo espanol</term></termSec></langSec>
      </conceptEntry>
"""
_V3_TBX = (
    '<?xml version="1.0" encoding="utf-8"?>\n<tbx style="dca" type="TBX-Core" '
    'xml:lang="en">\n  <text>\n    <body>' + _V3_BODY + "    </body>\n  </text>\n</tbx>\n"
).encode("utf-8")
# Byte-identical content, ISO default namespace declared -> must parse the same.
_V3_TBX_NS = (
    '<?xml version="1.0" encoding="utf-8"?>\n<tbx xmlns="urn:iso:std:iso:30042:ed-2" '
    'style="dca" type="TBX-Core" xml:lang="en">\n  <text>\n    <body>'
    + _V3_BODY
    + "    </body>\n  </text>\n</tbx>\n"
).encode("utf-8")

_CSV = (
    b"source_term,target_term,dnt,sense_label,context_cue,part_of_speech\r\n"
    b"widget,artilugio,,,,noun\r\n"
    b"Acme,,true,,,\r\n"
    b"bank,banco,,finance,money savings account,\r\n"
    b"bank,orilla,,river,river water shore,\r\n"
)


# ===========================================================================
# helpers
# ===========================================================================
_N = 0


def _project(name="TBX Proj", source_locale=SRC):
    with Session(engine) as s:
        p = Project(name=name, source_locale=source_locale)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _customer():
    global _N
    _N += 1
    with Session(engine) as s:
        c = Customer(name=f"TBX Cust {_N}")
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _glossary(name="TBX GL", pairs=((SRC, TGT),)):
    global _N
    _N += 1
    with Session(engine) as s:
        g = gm.create(s, f"{name} {_N}", customer_id=_customer())
        gid = g.id
        if pairs:
            # T2: a MODULE TBX import is concept-preserving and needs >=1 language pair so
            # rematerialize projects the requested pair's compat entries (LAW-TERM-9/§6.3).
            gm.set_pairs(s, gid, [f"{a}:{b}" for a, b in pairs])
        return gid


def _terms(project_id=None, glossary_id=None):
    """{source_term: (target_term, dnt, meta)} for a project's or glossary's entries."""
    with Session(engine) as s:
        if glossary_id is not None:
            rows = s.exec(
                select(GlossaryEntry).where(GlossaryEntry.glossary_id == glossary_id)
            ).all()
        else:
            rows = s.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == project_id)).all()
        return {e.source_term: (e.target_term, e.dnt, e.meta or {}) for e in rows}


def _sense_count(project_id):
    with Session(engine) as s:
        ids = [
            e.id
            for e in s.exec(
                select(GlossaryEntry).where(GlossaryEntry.project_id == project_id)
            ).all()
        ]
        if not ids:
            return 0
        return len(
            s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(ids))).all()
        )


def _import(pid, data, src=SRC, tgt=TGT, filename=None):
    with Session(engine) as s:
        return glossary.import_bytes(s, pid, data, src, tgt, filename=filename)


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _user_key(role="admin"):
    global _N
    _N += 1
    u = security.create_user(f"tbx{role}{_N}@x.com", role.title(), role, "tbx-pw-12345")
    _row, raw = apikeys.create_key(u.id, "tbx-test", scopes="api docs")
    return raw


# ===========================================================================
# dialects
# ===========================================================================
def test_microsoft_martif_dialect():
    """The Microsoft Terminology shape: ntig/termGrp, definition on the source
    langSet, partOfSpeech in the term group, a base-language target ("es" serving
    "es-ES"), two source terms in one langSet, and two unusable entries counted."""
    pid = _project("MS TBX")
    res = _import(pid, _MS_TBX, filename="SPANISH.tbx")
    assert res["format"] == "tbx", res
    assert res["concepts"] == 5, res
    assert res["skipped"] == 2, ("orphan (en only) + huerfano (es/fr only)", res)
    # 4 rows -> 4 entries: widget, sign in, log on, logon.
    assert res["entries"] == 4, res
    assert res["senses"] == 0, "TBX has no sense model"

    terms = _terms(pid)
    assert set(terms) == {"widget", "sign in", "log on", "logon"}, terms
    # metadata: definition from the source langSet, partOfSpeech from the term group;
    # the TARGET side's own notes and every unknown note type are ignored.
    tgt, dnt, meta = terms["widget"]
    assert tgt == "control", terms["widget"]
    assert dnt is False, "TBX carries no DNT concept -> always false"
    assert meta == {"definition": "A small user-interface control.", "part_of_speech": "Noun"}, meta
    assert terms["sign in"] == ("iniciar sesion", False, {}), terms["sign in"]
    # both source terms of entry 3 landed, sharing the single target term.
    assert terms["log on"][0] == "iniciar sesion", terms["log on"]
    assert terms["logon"][0] == "iniciar sesion", terms["logon"]
    # per-TERM metadata, not shared across the langSet's terms.
    assert terms["log on"][2] == {"part_of_speech": "Verb"}, terms["log on"]
    assert terms["logon"][2] == {}, terms["logon"]
    # the skipped entries left NOTHING behind.
    assert "orphan" not in terms and "huerfano" not in terms
    print(
        "  MS martif dialect: ntig/termGrp, definition+partOfSpeech mapped, base-lang "
        "target, 2 source terms -> 2 rows, 2 entries skipped+counted OK"
    )


def test_iate_martif_dialect():
    """The IATE shape: <tig>, entry-level subjectField, many sibling languages, and a
    locale pair (en-GB -> fr-FR) that only matches by BASE language on both sides."""
    pid = _project("IATE TBX", source_locale="en-GB")
    res = _import(pid, _IATE_TBX, src="en-GB", tgt="fr-FR", filename="iate.TBX")
    assert res["format"] == "tbx", res
    assert res["concepts"] == 5, res
    assert res["skipped"] == 2, ("FIX-3 (bg/cs) + FIX-4 (en only)", res)
    assert res["entries"] == 4, res  # 2 singles + the 2-term FIX-5

    terms = _terms(pid)
    assert set(terms) == {"competence of the Member States", "supply agency", "lorry", "truck"}, (
        terms
    )
    # entry-level subjectField reaches the term; reliabilityCode/termType are ignored.
    tgt, _dnt, meta = terms["competence of the Member States"]
    assert tgt == "competence des Etats membres", terms["competence of the Member States"]
    assert meta == {"subject_field": "1011"}, meta
    assert terms["supply agency"] == ("agence d'approvisionnement", False, {}), terms[
        "supply agency"
    ]
    # multiple target terms: the FIRST wins for every source term of the entry.
    assert terms["lorry"][0] == "camion" and terms["truck"][0] == "camion", terms
    print(
        "  IATE martif dialect: tig, entry-level subjectField, base-language fallback "
        "on BOTH sides, first target term wins OK"
    )


def test_tbx_v3_dialect_and_namespace():
    """TBX v3 conceptEntry/langSec/termSec, with and without the ISO default
    namespace (local-name matching means the two parse identically)."""
    for label, data in (("plain", _V3_TBX), ("namespaced", _V3_TBX_NS)):
        pid = _project(f"V3 {label}")
        res = _import(pid, data, filename="terms.tbx")
        assert res["concepts"] == 3, (label, res)
        assert res["skipped"] == 1, (label, "c3 has no en-US side", res)
        assert res["entries"] == 2, (label, res)
        terms = _terms(pid)
        assert set(terms) == {"dashboard", "toolbar"}, (label, terms)
        assert terms["dashboard"][0] == "panel", (label, terms)
        assert terms["dashboard"][2] == {
            "subject_field": "software",
            "part_of_speech": "noun",
            "definition": "A single-screen summary view.",
        }, (label, terms["dashboard"])
        assert terms["toolbar"] == ("barra de herramientas", False, {}), (label, terms)
    print(
        "  TBX v3 dialect: conceptEntry/langSec/termSec, entry+term metadata, and a "
        "default-namespaced file parses identically OK"
    )


# ===========================================================================
# language-pair selection edge cases
# ===========================================================================
def test_exact_locale_beats_base_and_degenerate_pair_is_skipped():
    """An exact xml:lang match wins over a base-language sibling, and a pair whose
    two sides resolve to the SAME section is skipped (never a term mapped to itself)."""
    data = b"""<?xml version="1.0"?><tbx><text><body>
    <conceptEntry id="1">
      <langSec xml:lang="en"><termSec><term>color</term></termSec></langSec>
      <langSec xml:lang="pt"><termSec><term>cor generico</term></termSec></langSec>
      <langSec xml:lang="pt-BR"><termSec><term>cor brasileira</term></termSec></langSec>
    </conceptEntry>
    <conceptEntry id="2">
      <langSec xml:lang="en"><termSec><term>self</term></termSec></langSec>
    </conceptEntry>
    </body></text></tbx>"""
    pid = _project("Locale pick")
    res = _import(pid, data, src="en-US", tgt="pt-BR", filename="x.tbx")
    assert res["entries"] == 1 and res["skipped"] == 1, res
    assert _terms(pid)["color"][0] == "cor brasileira", "exact pt-BR must beat base pt"

    # source and target both resolving to the single "en" section: degenerate, skipped.
    pid2 = _project("Degenerate")
    res2 = _import(pid2, data, src="en-US", tgt="en-GB", filename="x.tbx")
    assert res2["entries"] == 0, res2
    assert res2["skipped"] == 2, ("both entries collapse onto one section", res2)
    assert _terms(pid2) == {}, "no term may be imported as its own translation"
    print(
        "  locale selection: exact xml:lang beats the base-language sibling; a "
        "source/target pair collapsing onto one section is skipped OK"
    )


# ===========================================================================
# refusals
# ===========================================================================
def test_malformed_and_guard_refusals():
    """Every unusable input raises TbxError with a user-facing message -- the web
    seams flash it, the CLI exits 1, /api answers 400. Never a traceback."""
    cases = {
        "truncated": b"<?xml version='1.0'?><tbx><text><body><conceptEntry>",
        "not xml at all": b"<<<< this is not xml >>>>",
        "empty": b"",
        "no entries": (
            b"<?xml version='1.0'?><tbx><text><body><somethingElse/></body></text></tbx>"
        ),
        # ElementTree EXPANDS internal entities, so a nested-entity document is a
        # memory-exhaustion vector; declaring entities at all is refused.
        "entity bomb": (
            b"<?xml version='1.0'?><!DOCTYPE tbx [<!ENTITY a 'aaaaaaaaaa'>"
            b"<!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;'>]>"
            b"<tbx><text><body><conceptEntry><langSec xml:lang='en'>"
            b"<termSec><term>&b;</term></termSec></langSec>"
            b"</conceptEntry></body></text></tbx>"
        ),
        # ...and the guard is not head-bounded: a DOCTYPE internal subset can be
        # padded with arbitrary comments, so a byte-window scan would be bypassable.
        "entity bomb behind padding": (
            b"<?xml version='1.0'?><!DOCTYPE tbx [<!--"
            + b"p" * 200000
            + b"--><!ENTITY a 'aaaaaaaaaa'>]>"
            b"<tbx><text><body><conceptEntry><langSec xml:lang='en'>"
            b"<termSec><term>&a;</term></termSec></langSec>"
            b"</conceptEntry></body></text></tbx>"
        ),
    }
    pid = _project("Bad TBX")
    for label, data in cases.items():
        try:
            _import(pid, data, filename="bad.tbx")
        except glossary_tbx.TbxError as exc:
            assert str(exc), (label, "the error message must be user-facing")
            assert "Traceback" not in str(exc), (label, exc)
        else:
            raise AssertionError(f"{label!r} must be refused")
    assert _terms(pid) == {}, "a refused import must write nothing"

    # size cap: enforced on the BYTES before a parse is attempted.
    big = (
        b'<?xml version="1.0"?><tbx><text><body>'
        + b"<!-- "
        + b"x" * (config.MAX_UPLOAD_BYTES + 10)
        + b" -->"
        + b"</body></text></tbx>"
    )
    try:
        _import(pid, big, filename="big.tbx")
    except glossary_tbx.TbxError as exc:
        assert "limit" in str(exc).lower(), exc
    else:
        raise AssertionError("an oversize TBX must be refused")
    print(
        "  refusals: truncated / non-XML / empty / entry-less / entity-declaring / "
        "oversize all raise a clean TbxError and write nothing OK"
    )


# ===========================================================================
# CSV regression + detection
# ===========================================================================
def test_csv_arm_unchanged_through_the_dispatcher():
    """The CSV path through import_bytes writes byte for byte what the old
    import_csv did: senses, DNT and the meta columns all still land. Its RETURN
    shape gained "updated"/"skipped_rows" counters (BUG-09 QA finding,
    2026-07-29 -- see glossary._apply_rows's docstring); import_csv's own direct
    entry point is what kept the historical narrow shape, pinned separately
    below."""
    pid = _project("CSV regression")
    res = _import(pid, _CSV, filename="terms.csv")
    assert res["format"] == "csv", res
    assert res["entries"] == 3 and res["senses"] == 2, res
    # "bank"'s SECOND row (the river sense) touches a term this same call already
    # created on its first ("bank"/finance) row -> counted as "updated", not a
    # 4th "entries" (see glossary._apply_rows's COUNTERS docstring section).
    assert res["updated"] == 1 and res["skipped_rows"] == 0, res
    # ...and the CSV arm still emits NO "skipped" key. That key is TBX-only and
    # every surface renders it as "entries with no matching language pair"
    # (project page, glossary page, both CLI import verbs), which is meaningless
    # for a CSV. Review round 1 caught the counter train folding blank CSV rows
    # into it: an Excel-exported CSV with trailing empty rows then told the user
    # N entries had no matching language pair. Pinned here AND in
    # verify_glossary_import_bounded_memory.py's counter test.
    assert "skipped" not in res, (
        "the CSV arm keeps its historical result shape -- 'skipped' is the TBX "
        "parse-level count and nothing else",
        res,
    )
    terms = _terms(pid)
    assert terms["widget"] == ("artilugio", False, {"part_of_speech": "noun"}), terms
    assert terms["Acme"][1] is True, "dnt column still honoured"
    assert _sense_count(pid) == 2, "sense rows still create senses"

    # identical result with NO filename (a CSV never sniffs as XML).
    pid2 = _project("CSV regression 2")
    assert _import(pid2, _CSV)["entries"] == 3
    assert _terms(pid2)["widget"][0] == "artilugio"

    # the old entry point is still exact.
    pid3 = _project("CSV regression 3")
    with Session(engine) as s:
        legacy = glossary.import_csv(s, pid3, _CSV, SRC, TGT)
    assert legacy == {"entries": 3, "senses": 2}, legacy
    print(
        "  CSV regression: senses + DNT + meta unchanged through import_bytes, and "
        "import_csv keeps its exact old return shape OK"
    )


def test_format_detection_rules():
    """Extension first (case-insensitive), then a content sniff -- the sniff is what
    makes the remote /api path work when no filename travels."""
    assert glossary.detect_format(_CSV, "g.csv") == "csv"
    assert glossary.detect_format(_MS_TBX, "g.tbx") == "tbx"
    assert glossary.detect_format(_MS_TBX, "G.TBX") == "tbx", "case-insensitive"
    assert glossary.detect_format(_MS_TBX, None) == "tbx", "sniff: XML prolog"
    assert glossary.detect_format(_V3_TBX, "") == "tbx"
    assert glossary.detect_format(_CSV, None) == "csv", "sniff: a CSV stays CSV"
    assert glossary.detect_format(b"\xef\xbb\xbf<?xml ?><tbx/>", None) == "tbx", "BOM"
    # a .csv NAME over XML bytes stays CSV (extension is the documented rule)...
    assert glossary.detect_format(_MS_TBX, "actually_tbx.csv") == "csv"
    # ...and a .tbx NAME over CSV bytes routes to the TBX parser, so the user gets a
    # TBX error rather than a silent zero-row import.
    assert glossary.detect_format(_CSV, "actually_csv.tbx") == "tbx"
    pid = _project("Mislabelled")
    try:
        _import(pid, _CSV, filename="actually_csv.tbx")
    except glossary_tbx.TbxError:
        pass
    else:
        raise AssertionError("a .tbx name over CSV bytes must fail loudly")
    assert glossary.export_format_for("out.tbx") == "tbx"
    assert glossary.export_format_for("out.TBX") == "tbx"
    assert glossary.export_format_for("out.csv") == "csv"
    assert glossary.export_format_for(None) == "csv"
    print(
        "  format detection: extension-then-sniff, case-insensitive, BOM-tolerant; a "
        "mislabelled .tbx fails loudly OK"
    )


# ===========================================================================
# export + round-trip
# ===========================================================================
def test_export_tbx_and_round_trip():
    """import TBX -> export TBX -> re-import reproduces every term, target and mapped
    meta cell. Also pins what TBX-Core cannot carry (DNT + senses)."""
    pid = _project("Export src")
    _import(pid, _MS_TBX, filename="SPANISH.tbx")
    with Session(engine) as s:
        data = glossary.export_bytes(s, pid, fmt="tbx")
    text = data.decode("utf-8")
    assert text.startswith("<?xml"), text[:40]
    assert "<conceptEntry" in text and "<langSec" in text and "<termSec" in text, text
    assert 'type="partOfSpeech"' in text and 'type="definition"' in text, text
    assert "<term>widget</term>" in text and "<term>control</term>" in text, text

    pid2 = _project("Export dst")
    res = _import(pid2, data, filename="round.tbx")
    assert res["format"] == "tbx" and res["skipped"] == 0, res
    before, after = _terms(pid), _terms(pid2)
    assert before == after, (before, after)

    # DNT round-trips via a private termNote extension (2026-07-29) -- but ONLY
    # when the term also carries a target: a target-less DNT term still exports
    # source-only (a valid single-language TBX concept, unrelated to DNT) and
    # still re-imports as a SKIPPED entry, because there is no target langSec to
    # resolve at all. The note is still emitted on that source-only export, even
    # though it can never be recovered on THIS shape. Senses remain CSV-only.
    pid3 = _project("Lossy")
    _import(pid3, _CSV, filename="terms.csv")
    with Session(engine) as s:
        lossy = glossary.export_bytes(s, pid3, fmt="tbx").decode("utf-8")
    assert "<term>Acme</term>" in lossy, "a DNT term still exports its source side"
    assert 'type="doNotTranslate"' in lossy, (
        "the DNT note is emitted even for a target-less DNT term"
    )
    assert "orilla" not in lossy, "senses have no TBX-Core home (CSV is the lossless one)"
    pid4 = _project("Lossy reimport")
    res4 = _import(pid4, lossy.encode("utf-8"), filename="lossy.tbx")
    assert res4["senses"] == 0, res4
    assert res4["skipped"] == 1, (
        "Acme has no target side -> still skipped (single-language concept), "
        "regardless of the DNT note",
        res4,
    )
    assert _terms(pid4)["widget"][0] == "artilugio"
    assert "Acme" not in _terms(pid4)

    # ...and the shape that NOW round-trips (the fix): a DNT term that DOES carry
    # a target (commonly the term itself) exports as an ordinary pair PLUS the
    # DNT note, and re-imports with dnt=TRUE -- documented-lossy before this.
    pid5 = _project("DNT with target")
    _import(pid5, b"source_term,target_term,dnt\r\nAcme,Acme,true\r\n", filename="t.csv")
    with Session(engine) as s:
        both = glossary.export_bytes(s, pid5, fmt="tbx")
    assert b'type="doNotTranslate"' in both, both
    pid6 = _project("DNT with target re")
    res6 = _import(pid6, both, filename="r.tbx")
    assert res6["skipped"] == 0, res6
    assert _terms(pid6)["Acme"] == ("Acme", True, {}), (
        "a DNT term with a target now round-trips through TBX with dnt=true "
        "(private termNote extension, 2026-07-29)",
        _terms(pid6),
    )

    # a foreign/plain TBX (no doNotTranslate note at all) is unaffected: dnt stays
    # False by default, exactly as every other dialect fixture in this suite pins.
    pid7 = _project("Foreign no DNT")
    _import(pid7, _V3_TBX, filename="terms.tbx")
    assert _terms(pid7)["dashboard"][1] is False, _terms(pid7)
    print(
        "  export: TBX v3 shape, import->export->re-import is term-for-term stable; "
        "DNT now round-trips via a private termNote when the term has a target "
        "(still lossy for a target-less DNT term and for senses/usage_example/notes); "
        "a foreign TBX without the note is unaffected OK"
    )


def test_module_dca_dnt_cli_round_trip():
    gid, gid2 = _glossary("DNT export"), _glossary("DNT import")
    with Session(engine) as s:
        gm.add_term(
            s, gid, "Acme", target_term="Acme", source_locale=SRC, target_locale=TGT, dnt=True
        )
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "dnt.tbx")
        rc, out = _run_cli(["glossary", "module", "export", str(gid), path])
        assert rc == 0, out
        with open(path, "rb") as fh:
            assert fh.read().count(b"<los:dnt>true</los:dnt>") == 1
        rc, out = _run_cli(
            ["glossary", "module", "import", str(gid2), path, "--source", SRC, "--target", TGT]
        )
        assert rc == 0, out
        assert _terms(glossary_id=gid2)["Acme"] == ("Acme", True, {}), (
            "DCA module export/import must preserve dnt=True with a target term"
        )
        rc, out = _run_cli(["glossary", "module", "export", str(gid2), path, "--force"])
        assert rc == 0, out
        with open(path, "rb") as fh:
            assert fh.read().count(b"<los:dnt>true</los:dnt>") == 1


def test_house_dnt_values_and_precedence():
    for value, expected in (
        ("true", "true"),
        ("TrUe", "true"),
        ("1", "true"),
        (" YeS ", "true"),
        ("false", ""),
        ("FALSE", ""),
        ("no", ""),
        ("0", ""),
        ("maybe", ""),
        ("", ""),
    ):
        for legacy in (None, "false", "true"):
            for reverse in (False, True):
                house = f'<alias:dnt xmlns:alias="{glossary_tbx.LOS_NS}">{value}</alias:dnt>'
                note = (
                    "" if legacy is None else f'<termNote type="doNotTranslate">{legacy}</termNote>'
                )
                markers = note + house if reverse else house + note
                data = _dnt_fixture(None).replace(
                    b"<term>Acme</term>", f"<term>Acme</term>{markers}".encode(), 1
                )
                rows, _ = glossary_tbx.parse_rows(data, SRC, TGT)
                row = next(iter(rows))
                want = "true" if legacy == "true" else expected or legacy or ""
                assert row["dnt"] == want, (value, legacy, reverse, row)
    for namespace in ("", ' xmlns="https://example.invalid/foreign"'):
        data = _dnt_fixture(None).replace(
            b"<term>Acme</term>", f"<term>Acme</term><dnt{namespace}>true</dnt>".encode(), 1
        )
        rows, _ = glossary_tbx.parse_rows(data, SRC, TGT)
        assert next(iter(rows))["dnt"] == "", "only the house namespace carries DNT"

    # A false house marker is additive: it cannot clear inherited legacy DNT.
    for legacy, expected in ((None, ""), ("true", "true")):
        data = _dnt_fixture(legacy, where="entry").replace(
            b"<term>Acme</term>",
            f'<term>Acme</term><los:dnt xmlns:los="{glossary_tbx.LOS_NS}">false</los:dnt>'.encode(),
            1,
        )
        rows, _ = glossary_tbx.parse_rows(data, SRC, TGT)
        assert next(iter(rows))["dnt"] == expected, (legacy, expected)

    # Duplicate legacy siblings are truthy-wins in either order.
    for first, second in (("true", "false"), ("false", "true")):
        data = _dnt_fixture(first).replace(
            b"</termSec>",
            f'<termNote type="doNotTranslate">{second}</termNote></termSec>'.encode(),
            1,
        )
        rows, _ = glossary_tbx.parse_rows(data, SRC, TGT)
        assert next(iter(rows))["dnt"] == "true", (first, second)

    # The same truthy-wins rule applies across all source metadata levels.
    for where in ("entry", "langsec"):
        data = _dnt_fixture("true", where=where).replace(
            b"</termSec>", b'<termNote type="doNotTranslate">false</termNote></termSec>', 1
        )
        rows, _ = glossary_tbx.parse_rows(data, SRC, TGT)
        assert next(iter(rows))["dnt"] == "true", where


def _dnt_fixture(note, where="term"):
    """A minimal en/es concept carrying a doNotTranslate termNote at `where`
    ("term" | "langsec" | "entry" | "target"), or none at all when `note` is None."""
    tag = "" if note is None else f'<termNote type="doNotTranslate">{note}</termNote>'
    at = {"term": "", "langsec": "", "entry": "", "target": ""}
    at[where] = tag
    return (
        '<?xml version="1.0"?><tbx xml:lang="en"><text><body>'
        f'<conceptEntry id="c0">{at["entry"]}'
        f'<langSec xml:lang="{SRC}">{at["langsec"]}<termSec><term>Acme</term>'
        f"{at['term']}</termSec></langSec>"
        f'<langSec xml:lang="{TGT}"><termSec><term>Acme</term>{at["target"]}'
        "</termSec></langSec></conceptEntry>"
        "</body></text></tbx>"
    ).encode()


def test_dnt_note_value_and_placement_tolerance():
    """Review round 1: a FOREIGN file can spell the private DNT note any way it
    likes, and the extension must not turn a stray note into an accidental
    do-not-translate. Only the documented truthy vocabulary
    ("1"/"true"/"yes"/"y", case-insensitive -- glossary._apply_rows's parser)
    sets the flag; "false"/"no"/"0"/garbage/empty and an absent note all read
    FALSE. Placement follows the same source-side precedence table the other
    three mapped notes use (entry / source langSet / source term); a note on the
    TARGET term is IGNORED, because target-side metadata is never collected."""
    for value, expected in (
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("yes", True),
        ("Y", True),
        ("1", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("maybe", False),
        ("", False),
        (None, False),
    ):
        pid = _project(f"DNT value {value!r}")
        _import(pid, _dnt_fixture(value), filename="d.tbx")
        got = _terms(pid)["Acme"]
        assert got[1] is expected, (value, got)
        # and it is a ROW key, never a glossary meta column: "dnt" must not leak
        # into GlossaryEntry.meta through the shared _TERMNOTE_MAP table.
        assert "dnt" not in got[2], ("dnt must never land in meta", value, got)
    for where, expected in (("term", True), ("langsec", True), ("entry", True), ("target", False)):
        pid = _project(f"DNT at {where}")
        _import(pid, _dnt_fixture("true", where=where), filename="d.tbx")
        assert _terms(pid)["Acme"][1] is expected, (
            f"a doNotTranslate note at the {where} level",
            _terms(pid),
        )
    print(
        "  DNT note tolerance: only true/yes/1/y (any case) set the flag -- "
        "false/no/0/garbage/empty/absent do not; entry, source-langSet and "
        "source-term placements all count, a TARGET-side note does not; the "
        "value never leaks into GlossaryEntry.meta OK"
    )


def test_reimport_tbx_without_marker_keeps_dnt():
    """Issue #497: a TBX carrying NO doNotTranslate marker must KEEP a stored
    dnt=True. A markerless row arrives as dnt="" -- the same blank/missing value
    the CSV contract keeps (last non-blank wins) -- so it must never un-flag the
    term, count the row as changed, or revert an APPROVED glossary."""
    from app.models import GlossaryStatus

    # project scope: seed dnt=True through CSV, re-import the SAME pair as a
    # markerless TBX -- the vendor-termbase shape from the issue.
    pid = _project("No marker re")
    _import(pid, b"source_term,target_term,dnt\r\nAcme,Acme,true\r\n", filename="t.csv")
    assert _terms(pid)["Acme"] == ("Acme", True, {})
    res = _import(pid, _dnt_fixture(None), filename="again.tbx")
    assert _terms(pid)["Acme"] == ("Acme", True, {}), (
        "a markerless TBX re-import must not clear the stored dnt flag",
        _terms(pid),
    )
    assert res["changed"] == 0 and res["unchanged"] == 1, (
        "the markerless row moved nothing, so it must not count as changed",
        res,
    )

    # module scope: an APPROVED glossary seeded WITH the marker stays APPROVED
    # (and dnt=True) through a markerless re-import of the same pair.
    gid = _glossary("No marker GL")
    with Session(engine) as s:
        gm.import_bytes(s, gid, _dnt_fixture("true"), SRC, TGT, filename="seed.tbx")
        gm.approve(s, gid, approver_email="x@y.com")
    with Session(engine) as s:
        res = gm.import_bytes(s, gid, _dnt_fixture(None), SRC, TGT, filename="again.tbx")
    assert _terms(glossary_id=gid)["Acme"] == ("Acme", True, {}), _terms(glossary_id=gid)
    assert res["changed"] == 0, res
    with Session(engine) as s:
        assert s.get(Glossary, gid).status == GlossaryStatus.APPROVED, (
            "a markerless re-import changed nothing, so approval must stand",
            res,
        )
    print(
        "  a TBX with no doNotTranslate marker keeps the stored dnt flag, counts "
        "no change and leaves an APPROVED glossary APPROVED OK"
    )


# ===========================================================================
# seams: web
# ===========================================================================
def test_web_seams():
    """Both web import routes accept a .tbx upload and both exports serve TBX with
    ?format=tbx; a malformed upload is a clean redirect, never a 500."""
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)

    for marker in ("house", "legacy"):
        data = _dnt_fixture("true")
        if marker == "house":
            data = data.replace(
                b'<termNote type="doNotTranslate">true</termNote>',
                f'<los:dnt xmlns:los="{glossary_tbx.LOS_NS}">true</los:dnt>'.encode(),
            )
        dnt_gid = _glossary(f"DNT web {marker}")
        r = c.post(
            f"/glossaries/{dnt_gid}/import",
            data={"source_locale": SRC, "target_locale": TGT},
            files={"file": ("dnt.tbx", data, "application/x-tbx+xml")},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert _terms(glossary_id=dnt_gid)["Acme"][1] is True, marker

    # --- legacy project glossary ---
    pid = _project("Web TBX")
    r = c.post(
        f"/projects/{pid}/glossary/import",
        data={"source_locale": SRC, "target_locale": TGT},
        files={"file": ("SPANISH.tbx", _MS_TBX, "application/x-tbx+xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "gl_entries=4" in loc and "gl_skipped=2" in loc, loc
    assert _terms(pid)["widget"][0] == "control"

    r = c.get(f"/projects/{pid}/glossary/export?format=tbx")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(glossary_tbx.TBX_MEDIA_TYPE), r.headers
    assert ".tbx" in r.headers["content-disposition"], r.headers
    assert b"<conceptEntry" in r.content
    # default (and an unknown value) stays CSV.
    for qs in ("", "?format=csv", "?format=bogus"):
        rc_ = c.get(f"/projects/{pid}/glossary/export{qs}")
        assert rc_.status_code == 200 and rc_.content.startswith(b"source_term,"), qs
        assert ".csv" in rc_.headers["content-disposition"], qs

    # a malformed TBX is a clean redirect with the error flag, not a 500.
    r = c.post(
        f"/projects/{pid}/glossary/import",
        data={"source_locale": SRC, "target_locale": TGT},
        files={"file": ("bad.tbx", b"<tbx><body", "application/x-tbx+xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=glossary" in r.headers["location"], r.headers

    # --- module glossary, flat TBX import (T2: the TBX import is the flat entry path only;
    # `concepts=1` is reserved and refused, pinned by test_concepts_flag_refused_on_all_surfaces).
    # A module TBX import still mirrors the concept store from those entries (bulk-import train),
    # so the concept-format export below is populated. ---
    gid = _glossary("Web GL")
    r = c.post(
        f"/glossaries/{gid}/import",
        data={"source_locale": SRC, "target_locale": TGT},
        files={"file": ("terms.tbx", _V3_TBX, "application/x-tbx+xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    # the flat entry path imports the en-US->es-ES pair: 2 entries ({dashboard, toolbar}); the
    # de/es concept has no en-US source, so it is skipped and counted (gl_skipped=1).
    assert "gl_entries=2" in loc and "gl_skipped=1" in loc, loc
    assert set(_terms(glossary_id=gid)) == {"dashboard", "toolbar"}

    # default TBX export is now TBX-Basic v3 (DCA): required <tbxHeader> first child + los:
    # private namespace (§6.4).
    r = c.get(f"/glossaries/{gid}/export?format=tbx")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(glossary_tbx.TBX_MEDIA_TYPE), r.headers
    assert b"<term>dashboard</term>" in r.content
    assert b'type="TBX-Basic"' in r.content and b"<tbxHeader>" in r.content, r.content[:400]
    r = c.get(f"/glossaries/{gid}/export")
    assert r.content.startswith(b"source_term,"), "default is still CSV"

    r = c.post(
        f"/glossaries/{gid}/import",
        data={"source_locale": SRC, "target_locale": TGT},
        files={"file": ("bad.tbx", b"nope", "application/x-tbx+xml")},
        follow_redirects=False,
    )
    # a malformed upload is a clean redirect with an error flag, never a 500. T2 surfaces the
    # TbxError's user-facing message (matching /api + CLI + the ConceptError path) rather than
    # the opaque "import" tag, so assert a clean err= redirect.
    assert r.status_code == 303 and "err=" in r.headers["location"], r.headers
    print(
        "  web seams: both import routes take a .tbx upload (skipped count surfaced), "
        "both exports serve ?format=tbx, malformed -> clean redirect OK"
    )


# ===========================================================================
# seams: CLI
# ===========================================================================
def test_cli_seams():
    """`glossary import` / `glossary module import` read a .tbx path, and the two
    exports write TBX when the OUTPUT path ends .tbx. ASCII-only output."""
    pid = _project("CLI TBX")
    gid = _glossary("CLI GL")
    with tempfile.TemporaryDirectory() as td:
        tbx_path = os.path.join(td, "SPANISH.tbx")
        with open(tbx_path, "wb") as fh:
            fh.write(_MS_TBX)
        v3_path = os.path.join(td, "terms.tbx")
        with open(v3_path, "wb") as fh:
            fh.write(_V3_TBX)

        rc, out = _run_cli(
            ["glossary", "import", str(pid), tbx_path, "--source", SRC, "--target", TGT]
        )
        assert rc == 0, out
        assert out.isascii(), out
        assert "Skipped 2 entries" in out, out
        assert _terms(pid)["widget"][0] == "control"

        # T2: the module TBX import is the flat entry path (concepts is reserved/refused). It
        # imports the en-US->es-ES pair (dashboard, toolbar) and skips the de/es concept.
        rc, out = _run_cli(
            ["glossary", "module", "import", str(gid), v3_path, "--source", SRC, "--target", TGT]
        )
        assert rc == 0, out
        assert out.isascii(), out
        assert "entr" in out and "Skipped 1" in out, out
        assert set(_terms(glossary_id=gid)) == {"dashboard", "toolbar"}
        # --concepts is refused before any write (concept-preserving TBX import deferred to T1b);
        # the exact message (on stderr) is asserted in test_concepts_flag_refused_on_all_surfaces.
        rc, out = _run_cli(
            [
                "glossary",
                "module",
                "import",
                str(gid),
                v3_path,
                "--source",
                SRC,
                "--target",
                TGT,
                "--concepts",
            ]
        )
        assert rc != 0, out

        # a malformed file is a clean non-zero exit, never a traceback.
        bad_path = os.path.join(td, "bad.tbx")
        with open(bad_path, "wb") as fh:
            fh.write(b"<tbx><text><body>")
        rc, out = _run_cli(
            ["glossary", "import", str(pid), bad_path, "--source", SRC, "--target", TGT]
        )
        assert rc != 0, out

        # exports: the output extension picks the format, on both verbs.
        out_tbx = os.path.join(td, "out.tbx")
        rc, out = _run_cli(["glossary", "export", str(pid), out_tbx])
        assert rc == 0, out
        assert "TBX" in out and out.isascii(), out
        with open(out_tbx, "rb") as fh:
            written = fh.read()
        assert written.startswith(b"<?xml") and b"<conceptEntry" in written
        with Session(engine) as s:
            assert written == glossary.export_bytes(s, pid, fmt="tbx"), (
                "CLI must write byte-identical TBX to the service"
            )

        out_csv = os.path.join(td, "out.csv")
        rc, out = _run_cli(["glossary", "export", str(pid), out_csv])
        assert rc == 0, out
        with open(out_csv, "rb") as fh:
            assert fh.read().startswith(b"source_term,"), "a .csv path still writes CSV"

        gout_tbx = os.path.join(td, "gout.tbx")
        rc, out = _run_cli(["glossary", "module", "export", str(gid), gout_tbx])
        assert rc == 0, out
        with open(gout_tbx, "rb") as fh:
            assert b"<term>dashboard</term>" in fh.read()
        gout_csv = os.path.join(td, "gout.csv")
        rc, out = _run_cli(["glossary", "module", "export", str(gid), gout_csv])
        assert rc == 0, out
        with open(gout_csv, "rb") as fh:
            assert fh.read().startswith(b"source_term,")
    print(
        "  CLI seams: both imports read a .tbx path (ASCII skip note), both exports "
        "write TBX for a .tbx path and CSV otherwise, malformed -> exit non-zero OK"
    )


# ===========================================================================
# seams: /api  (this is also the REMOTE CLI path)
# ===========================================================================
def test_api_seams():
    """The /api import handlers take TBX bytes in the historical csv_b64 field. WITH
    the filename hint (what the remote CLI now sends) and WITHOUT it (an older client
    -> the content sniff), so `--remote` works either way."""
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    hdr = {"X-Loc-Token": _user_key("admin")}

    for marker in ("house", "legacy"):
        data = _dnt_fixture("true")
        if marker == "house":
            data = data.replace(
                b'<termNote type="doNotTranslate">true</termNote>',
                f'<los:dnt xmlns:los="{glossary_tbx.LOS_NS}">true</los:dnt>'.encode(),
            )
        dnt_gid = _glossary(f"DNT API {marker}")
        r = c.post(
            f"/api/glossaries/{dnt_gid}/import",
            headers=hdr,
            json={
                "csv_b64": base64.b64encode(data).decode(),
                "filename": "dnt.tbx",
                "source": SRC,
                "target": TGT,
            },
        )
        assert r.status_code == 200, r.text
        assert _terms(glossary_id=dnt_gid)["Acme"][1] is True, marker

    pid = _project("API TBX")
    b64 = base64.b64encode(_MS_TBX).decode()

    r = c.post(
        f"/api/projects/{pid}/glossary/import",
        headers=hdr,
        json={"csv_b64": b64, "filename": "SPANISH.tbx", "source": SRC, "target": TGT},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "tbx" and body["entries"] == 4 and body["skipped"] == 2, body

    # NO filename -> the sniff path (an older remote CLI sends only the bytes).
    pid2 = _project("API TBX sniff")
    r = c.post(
        f"/api/projects/{pid2}/glossary/import",
        headers=hdr,
        json={"csv_b64": b64, "source": SRC, "target": TGT},
    )
    assert r.status_code == 200, r.text
    assert r.json()["format"] == "tbx", r.json()
    assert _terms(pid2)["widget"][0] == "control"

    # CSV over the same field is untouched.
    pid3 = _project("API CSV")
    r = c.post(
        f"/api/projects/{pid3}/glossary/import",
        headers=hdr,
        json={
            "csv_b64": base64.b64encode(_CSV).decode(),
            "filename": "terms.csv",
            "source": SRC,
            "target": TGT,
        },
    )
    assert r.status_code == 200 and r.json()["format"] == "csv", r.text
    assert r.json()["entries"] == 3 and r.json()["senses"] == 2, r.json()

    # malformed -> 400 with a message, never a 500/traceback.
    r = c.post(
        f"/api/projects/{pid}/glossary/import",
        headers=hdr,
        json={
            "csv_b64": base64.b64encode(b"<tbx><body").decode(),
            "filename": "bad.tbx",
            "source": SRC,
            "target": TGT,
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["ok"] is False and r.json()["error"], r.text

    # --- module import + export ---
    gid = _glossary("API GL")
    r = c.post(
        f"/api/glossaries/{gid}/import",
        headers=hdr,
        json={
            "csv_b64": base64.b64encode(_V3_TBX).decode(),
            "filename": "terms.tbx",
            "source": SRC,
            "target": TGT,
            # T2 scope decision: the TBX import is the flat entry path only; a module TBX import
            # still mirrors the concept store from those entries (bulk-import train), so the
            # concept-format export below is populated. `concepts=true` is refused (its own test).
        },
    )
    assert r.status_code == 200, r.text
    # the flat module TBX import reports the entry-path shape: 3 conceptEntries seen, the de/es
    # one skipped for the en-US->es-ES pair, 2 entries projected.
    jb = r.json()
    assert jb["format"] == "tbx" and jb["concepts"] == 3, jb
    assert jb["entries"] == 2 and jb["skipped"] == 1, jb

    r = c.get(f"/api/glossaries/{gid}/export?format=tbx", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "tbx" and body["dialect"] == "tbx-basic", body
    data = base64.b64decode(body["csv_b64"])
    assert b"<term>dashboard</term>" in data and b"<tbxHeader>" in data
    with Session(engine) as s:
        assert data == gm.export_bytes(s, gid, fmt="tbx"), "/api bytes must match"
    # default is still the CSV, with its historical response shape.
    r = c.get(f"/api/glossaries/{gid}/export", headers=hdr)
    assert r.json()["format"] == "csv"
    assert base64.b64decode(r.json()["csv_b64"]).startswith(b"source_term,")

    r = c.post(
        f"/api/glossaries/{gid}/import",
        headers=hdr,
        json={
            "csv_b64": base64.b64encode(b"nope").decode(),
            "filename": "bad.tbx",
            "source": SRC,
            "target": TGT,
        },
    )
    assert r.status_code == 400 and r.json()["error"], r.text
    print(
        "  /api seams: TBX through csv_b64 with AND without the filename hint (the "
        "remote-CLI sniff path), module import + ?format=tbx export, 400 on bad OK"
    )


def test_module_import_reverts_approval():
    """A TBX import is a term change, so it reverts an approved glossary to draft --
    the same contract the CSV import has."""
    from app.models import GlossaryStatus

    gid = _glossary("Approval GL")
    with Session(engine) as s:
        gm.approve(s, gid, approver_email="x@y.com")
        assert s.get(Glossary, gid).status == GlossaryStatus.APPROVED
        gm.import_bytes(s, gid, _V3_TBX, SRC, TGT, filename="t.tbx")
    with Session(engine) as s:
        assert s.get(Glossary, gid).status == GlossaryStatus.DRAFT, (
            "a TBX import must revert approval like a CSV import does"
        )
    print("  module TBX import reverts approval (same contract as CSV) OK")


# ===========================================================================
# Group R -- adversarial-review regressions (2026-07-27). Each test here pins a
# defect the builder's own fixtures missed; each FAILS against the pre-review
# glossary_tbx.py / glossary.detect_format.
# ===========================================================================
_BOMB = (
    "<?xml version='1.0' encoding='UTF-16'?>"
    "<!DOCTYPE tbx ["
    "<!ENTITY a 'aaaaaaaaaa'>"
    "<!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;'>"
    "<!ENTITY c '&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;'>"
    "<!ENTITY d '&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;'>"
    "]>"
    "<tbx><text><body><conceptEntry><langSec xml:lang='en-US'><termSec>"
    "<term>&d;</term></termSec></langSec>"
    "<langSec xml:lang='es-ES'><termSec><term>x</term></termSec></langSec>"
    "</conceptEntry></body></text></tbx>"
)


def test_utf16_entity_bomb_is_refused():
    """THE BUG: the entity-declaration guard scanned RAW bytes for "<!ENTITY", but
    expat auto-detects UTF-16/UTF-32 from the BOM and expands the declarations just
    the same -- and in those encodings the bytes read "<\\x00!\\x00E\\x00N...", so the
    literal scan found nothing. A UTF-16 billion-laughs parsed happily (reproduced:
    the 4-level fixture below expanded to a 10,000-char term). The guard now also
    scans a NUL-stripped view; NUL is not a legal XML character, so that view can
    never hide a legitimate file."""
    for label, data in (
        ("utf-16 (LE + BOM)", _BOMB.encode("utf-16")),
        ("utf-16-be", b"\xfe\xff" + _BOMB.encode("utf-16-be")),
    ):
        assert not glossary_tbx._ENTITY_DECL.search(data), (
            label,
            "fixture must NOT match the raw-byte scan, or it proves nothing",
        )
        try:
            glossary_tbx.parse_rows(data, SRC, TGT)
        except glossary_tbx.TbxError as exc:
            assert "entities" in str(exc), (label, exc)
        else:
            raise AssertionError(f"{label}: a UTF-16 entity bomb must be refused")
    print(
        "  UTF-16/UTF-16BE entity bombs are refused (the raw-byte scan alone missed "
        "them entirely) OK"
    )


def test_sniff_survives_utf16_and_long_leading_whitespace():
    """THE BUG: the content sniff looked at data[:512] after stripping only a UTF-8
    BOM, so (a) a UTF-16 TBX and (b) an XML file with >512 bytes of leading
    whitespace both sniffed as CSV. Over /api WITHOUT the filename hint -- the exact
    remote-CLI path the feature advertises -- that meant a silent zero-term import
    instead of a parse."""
    u16 = (
        "<?xml version='1.0' encoding='UTF-16'?><tbx><text><body><conceptEntry>"
        "<langSec xml:lang='en-US'><termSec><term>widget</term></termSec></langSec>"
        "<langSec xml:lang='es-ES'><termSec><term>control</term></termSec></langSec>"
        "</conceptEntry></body></text></tbx>"
    )
    for label, data in (
        ("utf-16 LE+BOM", u16.encode("utf-16")),
        ("utf-16-be", b"\xfe\xff" + u16.encode("utf-16-be")),
        # no prolog: an XML declaration may not be preceded by
        # anything, but a bare root element may.
        ("600 leading spaces", b"\n" * 600 + _V3_TBX.split(b"?>\n", 1)[1]),
    ):
        assert glossary.detect_format(data, None) == "tbx", label
        pid = _project(f"sniff {label}")
        res = _import(pid, data)  # no filename -> the sniff path
        assert res["format"] == "tbx" and res["entries"] >= 1, (label, res)
        assert "widget" in _terms(pid) or "dashboard" in _terms(pid), (label, _terms(pid))
    # a CSV is still a CSV under the widened sniff.
    assert glossary.detect_format(_CSV, None) == "csv"
    assert glossary.detect_format(b"", None) == "csv"
    assert glossary.detect_format(b"   \r\n  ", None) == "csv"
    print(
        "  sniff: UTF-16 (both endians) and >512B of leading whitespace now detect as "
        "TBX; a CSV/empty payload still detects as CSV OK"
    )


def test_deeply_nested_tbx_is_a_clean_error():
    """The per-entry walkers recurse once per level of XML nesting and nesting depth
    is attacker-controlled up to the size cap, so a deep file raised RecursionError
    -- NOT a TbxError. parse_rows now converts it."""
    deep = (
        b"<?xml version='1.0'?><tbx><text><body><conceptEntry>"
        + b"<w>" * 5000
        + b"</w>" * 5000
        + b"<langSec xml:lang='en-US'><termSec><term>a</term></termSec></langSec>"
        b"<langSec xml:lang='es-ES'><termSec><term>b</term></termSec></langSec>"
        b"</conceptEntry></body></text></tbx>"
    )
    try:
        # parse_rows now returns a GENERATOR eagerly (2026-07-29, bounded memory);
        # the RecursionError -> TbxError conversion happens while draining it, not
        # before parse_rows returns -- so the probe must drain to see it, exactly
        # as every real caller (_apply_rows's `for row in rows:`) does.
        rows, _stats = glossary_tbx.parse_rows(deep, SRC, TGT)
        list(rows)
    except glossary_tbx.TbxError as exc:
        assert "deep" in str(exc).lower(), exc
    else:
        raise AssertionError("a pathologically nested TBX must raise TbxError")
    print("  a pathologically nested TBX raises TbxError, not RecursionError OK")


def test_control_characters_do_not_break_the_export():
    """THE BUG: a CSV cell may legally contain a form feed / vertical tab, our CSV
    import stores it verbatim, and lxml then refuses to serialize it -- so a single
    such term turned EVERY TBX export of that glossary into a ValueError: HTTP 500 on
    both web export routes and an uncaught traceback out of `glossary export`
    (reproduced). The character is now dropped at serialization."""
    from fastapi.testclient import TestClient

    from app.main import app

    pid = _project("Ctrl chars")
    _import(
        pid,
        b"source_term,target_term,definition\r\nwid\x0cget,arti\x0bculo,a\x00b\r\n",
        filename="t.csv",
    )
    with Session(engine) as s:
        data = glossary.export_bytes(s, pid, fmt="tbx")
    assert b"<term>widget</term>" in data, data
    assert b"<term>articulo</term>" in data, data
    # the export still re-imports (i.e. the sanitized file is well-formed).
    pid2 = _project("Ctrl chars re")
    assert _import(pid2, data, filename="r.tbx")["entries"] == 1

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get(f"/projects/{pid}/glossary/export?format=tbx")
    assert r.status_code == 200, (r.status_code, r.text[:200])

    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "ctrl.tbx")
        rc, msg = _run_cli(["glossary", "export", str(pid), out])
        assert rc == 0, msg
    print(
        "  a term holding XML-illegal control characters exports cleanly on the "
        "service, web and CLI seams (was a 500 / traceback) OK"
    )


def test_export_is_conformant_tbx_v3():
    """The export is meant to be read by OTHER tools, so it must declare the ISO
    30042 default namespace and use valid XML IDs (a bare row id like "4" is not an
    NCName). Both were missing; our own local-name parser hid it."""
    from lxml import etree

    pid = _project("Conformance")
    _import(pid, _V3_TBX, filename="terms.tbx")
    with Session(engine) as s:
        data = glossary.export_bytes(s, pid, fmt="tbx")
    doc = etree.fromstring(data)
    assert doc.tag == "{urn:iso:std:iso:30042:ed-2}tbx", doc.tag
    ns = {"t": "urn:iso:std:iso:30042:ed-2"}
    ids = doc.xpath("//t:conceptEntry/@id", namespaces=ns)
    assert ids, etree.tostring(doc)[:400]
    assert all(not i[:1].isdigit() for i in ids), ids
    # every element is in the TBX namespace, and it still round-trips through US.
    assert doc.xpath("//t:term/text()", namespaces=ns), "namespaced <term> missing"
    pid2 = _project("Conformance re")
    res = _import(pid2, data, filename="r.tbx")
    assert res["entries"] == 2, res
    assert _terms(pid2)["dashboard"][0] == "panel", _terms(pid2)
    print(
        "  export declares the ISO 30042 namespace + NCName ids and still round-trips "
        "through our own parser OK"
    )


# ===========================================================================
# T2 -- TBX-Basic v3 (DCA) export + strict conformance + the RESERVED concept
# import (fix round 7: concept-preserving TBX IMPORT is deferred to T1b; the
# `--concepts` / `concepts=true` option is refused on every surface).
# ===========================================================================
def _concept_store(gid):
    """The glossary's glossary-concept/v1 document (concept/term store), for assertions
    that do not depend on the compat-entry projection (the coordinator's rule: do not pin
    projected entries for a bidirectional glossary)."""
    from app.services import glossary_concepts as gc

    with Session(engine) as s:
        return gc.to_v1(s, gid)


# A rich glossary-concept/v1 document exercising the §6.1 categories + the house fields that
# ride the los: private namespace on export. Seeds the concept store through the KEPT
# `.glossary.json` import path (glossary_concepts.from_v1) now that the concept-preserving TBX
# IMPORT arm is deferred to T1b -- the export/strict-portable/branding behaviours it feeds are
# unchanged, only how the store gets populated is.
def _rich_doc() -> dict:
    return {
        "schema": glossary_concepts.SCHEMA_V1,
        "concepts": [
            {
                "external_id": "k1",
                "domain": "finance",
                "definition": "A record of a transaction.",
                "subdomain": "ledgers",
                "terms": [
                    {
                        "locale": "en-US",
                        "term": "invoice",
                        "status": "preferred",
                        "part_of_speech": "noun",
                        "term_type": "fullForm",
                        "grammatical_gender": "masculine",
                        "geographical_usage": "US",
                        "grammatical_number": "singular",
                        "usage_note": "prefer over 'bill'",
                        "case_sensitive": True,
                        "exact": True,
                        "context_example": "Send the invoice today.",
                        "source": "acme-import",
                    },
                    {"locale": "en-US", "term": "bill", "status": "forbidden"},
                    {"locale": "es-ES", "term": "factura", "part_of_speech": "properNoun"},
                    {"locale": "fr-FR", "term": "facture"},
                ],
            }
        ],
    }


def _seed_concepts(gid: int, doc: dict) -> None:
    """Populate a module glossary's concept store via the KEPT concept import (from_v1), the
    same seam the `.glossary.json` import routes through."""
    with Session(engine) as s:
        glossary_concepts.from_v1(s, gid, doc)


def test_strict_refusal_both_engines():
    """LAW-TERM-10 / H13: strict=true refuses on the first vendored-dialect finding, from
    EITHER the Schematron evaluator (bad picklist value) or the RELAX NG (structural), and
    writes nothing. Re-verified on the FLAT (only) TBX import arm now `--concepts` is reserved
    -- glossary.import_bytes runs validate_strict on the flat arm before parse_rows."""
    bad_picklist = (
        b'<?xml version="1.0"?><tbx style="dca" type="TBX-Basic" xml:lang="en-US" '
        b'xmlns="urn:iso:std:iso:30042:ed-2"><tbxHeader><fileDesc><sourceDesc><p>x</p>'
        b"</sourceDesc></fileDesc></tbxHeader><text><body>"
        b'<conceptEntry id="b1"><langSec xml:lang="en-US"><termSec><term>x</term>'
        b'<termNote type="administrativeStatus">bogusValue</termNote></termSec></langSec>'
        b'<langSec xml:lang="es-ES"><termSec><term>y</term></termSec></langSec>'
        b"</conceptEntry></body></text></tbx>"
    )
    bad_struct = bad_picklist.replace(b"langSec", b"langSection")
    for label, data in (("bad picklist", bad_picklist), ("structural", bad_struct)):
        gid = _glossary("Strict GL", pairs=(("en-US", "es-ES"),))
        try:
            with Session(engine) as s:
                gm.import_bytes(s, gid, data, "en-US", "es-ES", filename="s.tbx", strict=True)
        except glossary_tbx.TbxError as exc:
            from app.services import tbx_conformance

            # Without the official (unshipped) schema files the refusal is the clear
            # "needs the official files" message; with them it is a conformance finding.
            ok = "strict TBX validation failed" in str(exc) or (
                not tbx_conformance.schemas_available()
                and "needs the official TBX-Basic files" in str(exc)
            )
            assert ok, (label, exc)
        else:
            raise AssertionError(f"{label!r} must be refused under strict")
        assert _concept_store(gid)["concepts"] == [], (label, "strict refusal wrote rows")
    # WARN-by-default: the same bad-picklist file imports (flat entries) when NOT strict.
    gid2 = _glossary("Warn GL", pairs=(("en-US", "es-ES"),))
    with Session(engine) as s:
        gm.import_bytes(s, gid2, bad_picklist, "en-US", "es-ES", filename="w.tbx", strict=False)
    assert _concept_store(gid2)["concepts"], "warn-by-default must still import"
    print("  strict: bad picklist + structural both refuse (both engines); warn imports OK")


def test_empty_glossary_export_refused_tbx_json_only():
    """M8/M14: a concept-format (TBX/JSON) export of an EMPTY glossary refuses; CSV of an
    empty glossary is unchanged (a valid header-only file)."""
    gid = _glossary("Empty GL", pairs=(("en-US", "es-ES"),))
    with Session(engine) as s:
        for fmt in ("tbx", "json"):
            try:
                gm.export_bytes(s, gid, fmt=fmt)
            except glossary_tbx.EmptyGlossaryError:
                pass
            else:
                raise AssertionError(f"empty {fmt} export must refuse")
        csv_bytes = gm.export_bytes(s, gid, fmt="csv")
    assert csv_bytes.startswith(b"source_term,"), "empty CSV is still a valid header-only file"
    print("  empty glossary: TBX + JSON export refuse; CSV header-only unchanged OK")


def test_branding_header_rename_roundtrip():
    """M3: the <sourceDesc> product string comes from branding.brand_name(), never a
    literal; renaming the brand renames the exported header. Seeded via the KEPT concept
    import (from_v1)."""
    from app import branding
    from app.services import settings as _settings

    gid = _glossary("Brand GL", pairs=(("en-US", "es-ES"),))
    _seed_concepts(gid, _rich_doc())
    with Session(engine) as s:
        default_out = gm.export_bytes(s, gid, fmt="tbx")
    assert branding.brand_name().encode() in default_out, "default brand name must be in the header"
    try:
        _settings.set_many({"brand_name": "Acme Terms"})
        _settings.invalidate_cache()
        with Session(engine) as s:
            renamed = gm.export_bytes(s, gid, fmt="tbx")
        assert b"Acme Terms" in renamed, "the rename must reach the exported <sourceDesc>"
    finally:
        _settings.set_many({"brand_name": ""})
        _settings.invalidate_cache()
    print("  branding: <sourceDesc> renders brand_name() and follows a rename (no literal) OK")


def test_strict_portable_names_its_drops():
    """round-1 M1: a --strict-portable / tbx-core export drops the los: private fields and
    NAMES every field type it drops (matching the CLI help / docs claim), instead of dropping
    DNT/audience/status/... silently. Seeded via the KEPT concept import (from_v1)."""
    gid = _glossary("M1 drops GL", pairs=(("en-US", "es-ES"),))
    _seed_concepts(gid, _rich_doc())
    with Session(engine) as s:
        drops: list[str] = []
        portable = gm.export_bytes(s, gid, fmt="tbx", strict_portable=True, drops_out=drops)
        full_drops: list[str] = []
        gm.export_bytes(s, gid, fmt="tbx", drops_out=full_drops)
    assert b"xmlns:los" not in portable and b"<los:" not in portable, portable[:200]
    assert drops, "strict-portable must name its drops, not drop silently"
    for expected in (
        "status",
        "case-sensitivity",
        "exact-match",
        "grammatical number",
        "usage note",
        "part of speech",
    ):
        assert expected in drops, (expected, drops)
    assert full_drops == [], ("a full tbx-basic export drops nothing", full_drops)
    print(f"  M1: strict-portable names its drops {sorted(drops)}; full export drops nothing OK")


def test_r2l1_guard_runs_before_strict_validation():
    """round-2 L1: on the flat TBX arm the entity-declaration refusal (`_guard`) must run
    BEFORE `validate_strict` hands the bytes to lxml, so a hostile entity-declaring TBX is met
    with the honest TbxError on every arm -- strict included -- rather than a raw lxml error."""
    xxe = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE tbx [<!ENTITY xx SYSTEM "file:///etc/passwd">]>\n'
        b'<tbx xml:lang="en"><text><body><conceptEntry id="c1">'
        b'<langSec xml:lang="en-US"><termSec><term>&xx;</term></termSec></langSec>'
        b"</conceptEntry></body></text></tbx>"
    )
    pid = _project("L1 strict proj")
    for strict in (True, False):
        try:
            with Session(engine) as s:
                glossary.import_bytes(
                    s, pid, xxe, SRC, TGT, glossary_id=None, filename="xxe.tbx", strict=strict
                )
        except glossary_tbx.TbxError as exc:
            assert "entit" in str(exc).lower(), (
                "an entity-declaring TBX must raise the honest 'declares XML entities' "
                "TbxError, not a raw lxml error",
                strict,
                str(exc),
            )
        else:
            raise AssertionError(
                f"an entity-declaring TBX must be refused (strict={strict}), not accepted"
            )
    print("  L1: _guard runs before strict validation; a hostile TBX raises a clean TbxError OK")


def test_default_module_tbx_import_takes_the_entry_path_and_concepts_is_refused():
    """T2 scope decision: a plain module TBX import takes the linear entry path (the bulk
    reconcile), and `concepts=True` is RESERVED -- refused before any parse or write, writing
    nothing (the concept-preserving TBX import is deferred to the terminology authority train)."""
    from app.services import glossary_concepts as _gc

    orig_bulk = _gc._bulk_sync_glossary_from_entries
    calls = {"bulk": 0}

    def _spy_bulk(*a, **k):
        calls["bulk"] += 1
        return orig_bulk(*a, **k)

    gid = _glossary("H1 default GL", pairs=(("en-US", "es-ES"),))
    set_pieces("app.services.glossary_concepts", "_bulk_sync_glossary_from_entries", _spy_bulk)
    try:
        with Session(engine) as s:
            res = gm.import_bytes(s, gid, _V3_TBX, SRC, TGT, filename="d.tbx")
        assert res["format"] == "tbx" and "entries" in res, res
        assert calls["bulk"] >= 1, ("default module TBX must use the bulk reconcile", calls)

        gid2 = _glossary("H1 concepts GL", pairs=(("en-US", "es-ES"),))
        try:
            with Session(engine) as s:
                gm.import_bytes(s, gid2, _V3_TBX, SRC, TGT, filename="c.tbx", concepts=True)
        except glossary_tbx.TbxError as exc:
            assert "terminology authority train" in str(exc) and "standard import" in str(exc), exc
        else:
            raise AssertionError("concepts=True must be refused (reserved for T1b)")
        assert _concept_store(gid2)["concepts"] == [], "a refused concepts import writes nothing"
    finally:
        set_pieces("app.services.glossary_concepts", "_bulk_sync_glossary_from_entries", orig_bulk)
    print("  default module TBX rides the bulk entry path; concepts=True is reserved/refused OK")


def test_from_v1_enforce_projection_fails_closed_at_zero():
    """H2 fail-closed (KEEP, re-verified directly on from_v1 now the TBX `--concepts` arm is
    deferred): from_v1(enforce_projection=True) refuses a document that writes terms but
    projects 0 entries for the glossary's configured pair(s), writing nothing; the
    `.glossary.json` arm (enforce_projection default OFF) accepts the same doc at 0. This is the
    substrate T1b's import_concepts_batched drives."""
    doc = {
        "schema": glossary_concepts.SCHEMA_V1,
        "concepts": [
            {
                "external_id": "m1",
                "terms": [
                    {"locale": "fr-FR", "term": "bonjour"},
                    {"locale": "de-DE", "term": "hallo"},
                ],
            }
        ],
    }
    gid = _glossary("H2 fail-closed GL", pairs=(("en-US", "es-ES"),))
    with Session(engine) as s:
        try:
            glossary_concepts.from_v1(s, gid, dict(doc), enforce_projection=True)
        except glossary_concepts.ConceptError as exc:
            assert "0 entries" in str(exc) or "project" in str(exc), exc
        else:
            raise AssertionError("enforce_projection=True must refuse a 0-projection import")
        s.rollback()
    assert _concept_store(gid)["concepts"] == [], "the refused import wrote nothing"

    gid2 = _glossary("H2 json-zero GL", pairs=(("en-US", "es-ES"),))
    with Session(engine) as s:
        counters = glossary_concepts.from_v1(s, gid2, dict(doc))
    assert counters.get("projected_entries") == 0, counters
    assert _concept_store(gid2)["concepts"], "JSON arm (enforce_projection OFF) accepts at 0"
    print("  H2: from_v1 enforce_projection fails closed at 0; JSON arm accepts at 0 OK")


def test_r4m2_cli_import_messages_use_the_corrected_projected_wording():
    """round-4 M2: the CLI import surfaces print the CORRECTED projected_entries wording -- what
    THIS import's terms enforce -- for a `.glossary.json` (concept) import, in parity with the
    web/api and the three descriptors. Driven live through the LOCAL CLI on the KEPT JSON arm."""
    import tempfile as _tf

    doc = {
        "schema": glossary_concepts.SCHEMA_V1,
        "concepts": [
            {
                "external_id": "m1",
                "terms": [
                    {"locale": "en-US", "term": "widget", "status": "preferred"},
                    {"locale": "es-ES", "term": "artilugio", "status": "preferred"},
                ],
            },
            {
                "external_id": "m2",
                "terms": [
                    {"locale": "en-US", "term": "gadget", "status": "preferred"},
                    {"locale": "es-ES", "term": "chisme", "status": "preferred"},
                ],
            },
        ],
    }
    gid = _glossary("R4M2 wording GL", pairs=(("en-US", "es-ES"),))
    with _tf.NamedTemporaryFile("wb", suffix=".glossary.json", delete=False) as fh:
        fh.write(glossary_concepts.dumps_v1(doc))
        path = fh.name
    argv = ["glossary", "module", "import", str(gid), path, "--source", SRC, "--target", TGT]
    rc, out = _run_cli(argv)
    os.unlink(path)
    assert rc == 0, (rc, out)
    assert "this import's terms enforce for the glossary's language pair(s)" in out, out
    assert "project for the glossary's language pair(s)" not in out, out

    import app.cli._remote_impl as ri
    import app.cli.cmd_project as cp
    from app.cli._common import projected_entries_sentence

    assert projected_entries_sentence(1) == (
        " 1 entry this import's terms enforce for the glossary's language pair(s)."
    )
    assert projected_entries_sentence(3) == (
        " 3 entries this import's terms enforce for the glossary's language pair(s)."
    )
    assert cp.projected_entries_sentence is projected_entries_sentence, "local CLI shared sentence"
    assert ri.projected_entries_sentence is projected_entries_sentence, "remote CLI shared sentence"
    print("  R4M2: both CLI import messages use the corrected projected_entries wording")


def test_concepts_flag_refused_on_all_surfaces():
    """T2 scope decision (fix round 7): the `--concepts` / `concepts=true` TBX import option is
    RESERVED and refused BEFORE any parse or write on every surface -- the service seam, the
    local CLI, the /api route (which the remote CLI and the MCP server both POST to), and the web
    checkbox -- with the shared message, writing nothing."""
    import base64 as _b64
    import tempfile as _tf

    from fastapi.testclient import TestClient

    from app.main import app

    msg = "Concept-preserving TBX import arrives with the terminology authority train"
    gid = _glossary("Refuse GL", pairs=(("en-US", "es-ES"),))

    # service seam
    with Session(engine) as s:
        try:
            gm.import_bytes(s, gid, _V3_TBX, SRC, TGT, filename="x.tbx", concepts=True)
        except glossary_tbx.TbxError as exc:
            assert msg in str(exc), exc
        else:
            raise AssertionError("service: concepts=True must be refused")

    # CLI (local)
    with _tf.NamedTemporaryFile("wb", suffix=".tbx", delete=False) as fh:
        fh.write(_V3_TBX)
        path = fh.name
    obuf, ebuf = io.StringIO(), io.StringIO()
    with redirect_stdout(obuf), redirect_stderr(ebuf):
        rc = cli_main(
            [
                "glossary",
                "module",
                "import",
                str(gid),
                path,
                "--source",
                SRC,
                "--target",
                TGT,
                "--concepts",
            ]
        )
    os.unlink(path)
    cli_out = obuf.getvalue() + ebuf.getvalue()
    assert rc != 0 and msg in cli_out, (rc, cli_out)

    # /api (the route the remote CLI and the MCP server both proxy to)
    c = TestClient(app)
    hdr = {"X-Loc-Token": _user_key("admin")}
    r = c.post(
        f"/api/glossaries/{gid}/import",
        headers=hdr,
        json={
            "csv_b64": _b64.b64encode(_V3_TBX).decode(),
            "source": SRC,
            "target": TGT,
            "filename": "x.tbx",
            "concepts": True,
        },
    )
    assert r.status_code == 400 and msg in (r.json().get("error") or ""), r.text

    # web checkbox
    r2 = c.post(
        f"/glossaries/{gid}/import",
        data={"source_locale": SRC, "target_locale": TGT, "concepts": "1"},
        files={"file": ("x.tbx", _V3_TBX, "application/octet-stream")},
        follow_redirects=False,
    )
    loc = r2.headers.get("location", "")
    assert "err=" in loc and "train" in loc, loc

    assert _concept_store(gid)["concepts"] == [], "a refused concepts import writes nothing"
    print(
        "  concepts=True refused on service, CLI, /api (remote + MCP proxy) and web; "
        "nothing written OK"
    )


def test_concepts_refusal_precedes_the_parser():
    """M2 (gate r8): the reserved `--concepts` refusal fires BEFORE any parse or write -- the
    load-bearing ordering the errata, docs/CLI.md, help.html, capabilities/glossary.md, the MCP
    `concepts` schema property and SPEC 6.3 all promise. Proven with MALFORMED bytes: WITH
    `concepts=True` the service seam and the /api wire surface return the RESERVED message (never
    an XML parse error) and write nothing, while the SAME bytes WITHOUT the flag reach the parser
    and raise the 'not readable XML' error -- so the file really would hit lxml if the refusal did
    not pre-empt it. Mutation-sensitive: moving the refusal below _guard/validate_strict/parse_rows
    makes these malformed bytes raise the parse error instead, reddening this test (gate mutation
    B); removing or rewording it also reddens it (mutations A/C)."""
    import base64 as _b64

    from fastapi.testclient import TestClient

    from app.main import app

    reserved = "Concept-preserving TBX import arrives with the terminology authority train"
    parse_sigs = ("readable XML", "well-formed")  # the two parse-error signatures in glossary_tbx
    malformed = (
        b"<?xml version='1.0'?><tbx style='dca' xmlns='urn:iso:std:iso:30042:ed-2'>"
        b"<text><body><conceptEntry>< not well formed"
    )
    gid = _glossary("Refuse pre-parse GL", pairs=(("en-US", "es-ES"),))

    # service seam, WITH the flag: refused with the reserved message, NOT a parse error.
    with Session(engine) as s:
        try:
            gm.import_bytes(s, gid, malformed, SRC, TGT, filename="bad.tbx", concepts=True)
        except glossary_tbx.TbxError as exc:
            assert reserved in str(exc), (
                "service concepts=True must give the reserved message",
                exc,
            )
            assert not any(p in str(exc) for p in parse_sigs), (
                "the refusal must pre-empt the parser (no XML parse error)",
                exc,
            )
        else:
            raise AssertionError("service: malformed + concepts=True must be refused")

    # CONTROL: the SAME malformed bytes WITHOUT the flag DO reach the parser (proves the fixture is
    # genuinely malformed, so the refusal above really is pre-empting a parse that would otherwise
    # fire).
    with Session(engine) as s:
        try:
            gm.import_bytes(s, gid, malformed, SRC, TGT, filename="bad.tbx")
        except glossary_tbx.TbxError as exc:
            assert any(p in str(exc) for p in parse_sigs), (
                "the control (no flag) must reach lxml and raise a parse error",
                exc,
            )
        else:
            raise AssertionError(
                "control: malformed bytes without the flag must raise a parse error"
            )

    # /api wire surface (the route the remote CLI and the MCP server both proxy to), WITH the flag.
    c = TestClient(app)
    hdr = {"X-Loc-Token": _user_key("admin")}
    r = c.post(
        f"/api/glossaries/{gid}/import",
        headers=hdr,
        json={
            "csv_b64": _b64.b64encode(malformed).decode(),
            "source": SRC,
            "target": TGT,
            "filename": "bad.tbx",
            "concepts": True,
        },
    )
    err = (r.json().get("error") or "") if r.status_code == 400 else r.text
    assert r.status_code == 400 and reserved in err, (r.status_code, err)
    assert not any(p in err for p in parse_sigs), ("/api must pre-empt the parser", err)

    assert _concept_store(gid)["concepts"] == [], "a refused pre-parse import writes nothing"
    print(
        "  M2: the --concepts refusal pre-empts the parser (malformed bytes) on service + /api; "
        "the same bytes without the flag reach lxml; nothing written OK"
    )


def test_json_arm_and_exact_locale_match_pin_the_kept_concept_behaviour():
    """L3 (gate r8): this pins the KEPT `.glossary.json` / exact-locale behaviour by asserting the
    expected ABSOLUTE concept/term/entry rows -- it is a regression guard, NOT a live diff against
    main (it never loads a main tree). The byte-for-byte-vs-main claim for the exact-locale path is
    the errata's, verified out of band in the gate (P2/P6); this test guards that the restored arm
    keeps producing those exact rows. T2 restores the `.glossary.json` import arm so glossary.py's
    JSON branch calls glossary_concepts.from_v1 directly again (provable via
    `git diff 1ae20a3a -- app/services/glossary.py`, which shows NO change in that branch): an
    exact-locale `.glossary.json` import stores the expected concept/term/entry rows, the flat
    exact-locale TBX arm projects the same entry, and a stable-id JSON round-trip is an idempotent
    fixed point (no duplicate concepts)."""
    doc = {
        "schema": glossary_concepts.SCHEMA_V1,
        "concepts": [
            {
                "external_id": "j1",
                "terms": [
                    {"locale": "en-US", "term": "widget", "status": "preferred"},
                    {"locale": "es-ES", "term": "artilugio", "status": "preferred"},
                ],
            },
            {
                "external_id": "j2",
                "terms": [
                    {"locale": "en-US", "term": "gadget", "status": "preferred"},
                    {"locale": "es-ES", "term": "chisme", "status": "preferred"},
                ],
            },
        ],
    }
    blob = glossary_concepts.dumps_v1(doc)
    gid = _glossary("JSON arm GL", pairs=(("en-US", "es-ES"),))
    with Session(engine) as s:
        res = gm.import_bytes(s, gid, blob, SRC, TGT, filename="seed.glossary.json")
    assert res["format"] == "json" and res["concepts"] == 2, res
    assert len(_concept_store(gid)["concepts"]) == 2, _concept_store(gid)
    t = _terms(glossary_id=gid)
    assert t.get("widget", (None,))[0] == "artilugio", t
    assert t.get("gadget", (None,))[0] == "chisme", t

    # stable-id JSON round-trip is idempotent (upsert by external_id; no duplicate concepts) --
    # main's fixed-point behaviour, unchanged by T2.
    with Session(engine) as s:
        exported = gm.export_bytes(s, gid, fmt="json")
        gm.import_bytes(s, gid, exported, SRC, TGT, filename="rt.glossary.json")
    assert len(_concept_store(gid)["concepts"]) == 2, "stable-id JSON re-import must not grow"

    # the flat EXACT-locale TBX arm projects the same entry (the match path is byte-identical to
    # main: exact xml:lang wins, no base fallback needed).
    pid = _project("JSON arm flat")
    tbx = (
        b'<?xml version="1.0"?><tbx style="dca" type="TBX-Core" xml:lang="en-US" '
        b'xmlns="urn:iso:std:iso:30042:ed-2"><text><body>'
        b'<conceptEntry id="f1"><langSec xml:lang="en-US"><termSec><term>widget</term>'
        b'</termSec></langSec><langSec xml:lang="es-ES"><termSec><term>artilugio</term>'
        b"</termSec></langSec></conceptEntry></body></text></tbx>"
    )
    res3 = _import(pid, tbx, filename="flat.tbx")
    assert res3["entries"] == 1 and _terms(pid)["widget"][0] == "artilugio", (res3, _terms(pid))
    print(
        "  JSON arm restored to main (from_v1 direct); exact-locale JSON + flat-TBX match, "
        "stable-id JSON round-trip idempotent OK"
    )


def main():
    init_db()
    # T2: the KEPT concept import (`.glossary.json` / from_v1) write seams are behind the
    # DEFAULT-OFF terminology_concept_editor flag (T1-lite). Enable it so the module-glossary
    # concept seams exercise the concept path; the flag-OFF empty-only refusal is pinned
    # separately (verify_glossary_concept_flag).
    from app.services import settings as _settings

    _settings.set_many({"terminology_concept_editor_enabled": "1"})
    _settings.invalidate_cache()
    test_microsoft_martif_dialect()
    test_iate_martif_dialect()
    test_tbx_v3_dialect_and_namespace()
    test_exact_locale_beats_base_and_degenerate_pair_is_skipped()
    test_malformed_and_guard_refusals()
    test_csv_arm_unchanged_through_the_dispatcher()
    test_format_detection_rules()
    test_export_tbx_and_round_trip()
    test_module_dca_dnt_cli_round_trip()
    test_house_dnt_values_and_precedence()
    test_dnt_note_value_and_placement_tolerance()
    test_reimport_tbx_without_marker_keeps_dnt()
    test_web_seams()
    test_cli_seams()
    test_api_seams()
    test_module_import_reverts_approval()
    # group R -- adversarial-review regressions
    test_utf16_entity_bomb_is_refused()
    test_sniff_survives_utf16_and_long_leading_whitespace()
    test_deeply_nested_tbx_is_a_clean_error()
    test_control_characters_do_not_break_the_export()
    test_export_is_conformant_tbx_v3()
    # T2 -- export + strict conformance (KEPT); the concept-preserving IMPORT arm is deferred to
    # T1b and its `--concepts` option is refused on every surface (fix round 7).
    test_strict_refusal_both_engines()
    test_empty_glossary_export_refused_tbx_json_only()
    test_branding_header_rename_roundtrip()
    test_strict_portable_names_its_drops()
    test_r2l1_guard_runs_before_strict_validation()
    test_default_module_tbx_import_takes_the_entry_path_and_concepts_is_refused()
    test_from_v1_enforce_projection_fails_closed_at_zero()
    test_r4m2_cli_import_messages_use_the_corrected_projected_wording()
    test_concepts_flag_refused_on_all_surfaces()
    test_concepts_refusal_precedes_the_parser()
    test_json_arm_and_exact_locale_match_pin_the_kept_concept_behaviour()
    print("GLOSSARY TBX OK")


if __name__ == "__main__":
    main()
