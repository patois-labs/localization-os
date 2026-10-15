"""TBX-Basic (DCA) strict-conformance verification -- hermetic, DB-light.

The T2 strict-conformance layer (SPEC-terminology-backend §6.5, LAW-TERM-10) runs two
engines against the VENDORED TBX-Basic v3 (DCA) schemas in ``app/resources/tbx/`` (the
core-struct RELAX NG and a faithful in-repo evaluator of the DCA Schematron -- see
``app/services/tbx_conformance.py``). This suite is that layer's own proof, distinct
from ``verify_glossary_tbx.py`` (which covers the format/dialect/seam layer). It asserts:

  1. COVERAGE RATCHET (E-T2-2): ``tbx_conformance.assert_count()`` equals the number of
     ``<assert>`` elements counted directly from the vendored ``TBX-Basic_DCA.sch`` -- so a
     future upstream rule addition the evaluator does not yet translate FAILS the build.
  2. The vendored upstream ``EXAMPLE_VALID_PATH`` (pure TBX-Basic) passes BOTH engines
     with ZERO findings.
  3. A normal module-glossary TBX export (default tbx-basic, los: on) has NO real
     conformance failure -- every finding is solely the DCT-guard flagging one of OUR
     ``los:`` private-namespace elements -- and the los: elements really appear.
  4. A ``strict_portable=True`` export drops los: entirely and validates with ZERO
     findings (a clean pure-TBX-Basic instance, §6.5).
  5. Every §6.1 picklist named in the spec is present in the parsed ``.sch`` (partOfSpeech
     / grammaticalGender / termType / administrativeStatus non-empty), grammaticalNumber
     has NO category (E-T2-4), and the picklists carry the values the spec pins.
  6. Hand-built INVALID samples are caught -- a bad picklist value by the Schematron
     evaluator, a wrong element by the RELAX NG -- proving BOTH engines bite.

Final line: "TBX BASIC CONFORMANCE OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tbxconf_")

from lxml import etree
from sqlmodel import Session

from app.db import engine, init_db
from app.services import glossary_module as gm
from app.services import settings as _settings
from app.services import tbx_conformance

SRC, TGT = "en-US", "es-ES"
_SCH_NS = "{http://purl.oclc.org/dsdl/schematron}assert"

# A concept-preserving TBX-Basic input that exercises: a normal term (valid POS), a DNT
# term, an OUT-OF-PICKLIST partOfSpeech ("properNoun"), and grammaticalNumber (a datcat
# with NO TBX-Basic category). All four ride the standard element shapes on the way IN;
# the export decides which have a standard home and which must fall back to los:.
# The concept store the export tests need. T2 defers the concept-preserving TBX IMPORT to T1b,
# so the store is seeded through the KEPT `.glossary.json` import (glossary_concepts.from_v1),
# not a TBX `--concepts` import. Content mirrors the old _IMPORT_TBX fixture: a plain pair, a DNT
# brand, and an out-of-picklist properNoun + grammaticalNumber (both ride los: on export).
_IMPORT_DOC = {
    "concepts": [
        {
            "external_id": "c1",
            "terms": [
                {"locale": "en-US", "term": "gateway", "part_of_speech": "noun"},
                {"locale": "es-ES", "term": "puerta de enlace"},
            ],
        },
        {
            "external_id": "c2",
            "terms": [
                {"locale": "en-US", "term": "Acme", "dnt": True},
                {"locale": "es-ES", "term": "Acme"},
            ],
        },
        {
            "external_id": "c3",
            "terms": [
                {
                    "locale": "en-US",
                    "term": "Anthropic",
                    "part_of_speech": "properNoun",
                    "grammatical_number": "singular",
                },
                {"locale": "es-ES", "term": "Anthropic"},
            ],
        },
    ],
}


def _new_glossary(name):
    """A fresh module glossary with the en-US:es-ES pair (satisfies the concept-import
    seed gate; the flag is on regardless)."""
    with Session(engine) as s:
        g = gm.create(s, name)
        gm.set_pairs(s, g.id, [f"{SRC}:{TGT}"])
        return g.id


def _seeded_export(name, **export_kw):
    """Seed a fresh glossary's concept store via the KEPT `.glossary.json` import (from_v1) and
    return the requested TBX export bytes. T2 defers the concept-preserving TBX IMPORT to T1b."""
    from app.services import glossary_concepts as gc

    gid = _new_glossary(name)
    doc = {"schema": gc.SCHEMA_V1, **_IMPORT_DOC}
    with Session(engine) as s:
        res = gm.import_bytes(s, gid, gc.dumps_v1(doc), SRC, TGT, filename="fixture.glossary.json")
        assert res["format"] == "json", res
        assert res["concepts"] == 3, res
        out = gm.export_bytes(s, gid, fmt="tbx", **export_kw)
    return out


# ===========================================================================
# 1. coverage ratchet
# ===========================================================================
def _schemas():
    return tbx_conformance.schemas_available()


def test_assert_count_matches_vendored_sch():
    """assert_count() == the number of <assert> elements physically in the vendored
    .sch. If upstream adds a rule the evaluator has not translated, the two diverge and
    THIS assertion fails the build (E-T2-2)."""
    if not _schemas():
        print("  coverage ratchet: SKIPPED (official .sch not installed)")
        return
    doc = etree.parse(str(tbx_conformance._require(tbx_conformance.SCH_NAME)))
    physical = len(doc.getroot().findall(f".//{_SCH_NS}"))
    assert physical > 0, "the vendored .sch must contain <assert> elements"
    assert tbx_conformance.assert_count() == physical, (
        tbx_conformance.assert_count(),
        physical,
        "the evaluator's known assert count must equal the vendored file's -- an "
        "upstream rule was added without an evaluator translation",
    )
    print(f"  coverage ratchet: assert_count() == {physical} <assert> in the vendored .sch OK")


# ===========================================================================
# 2. the vendored upstream valid example passes both engines cleanly
# ===========================================================================
_INHOUSE_VALID = """<?xml version="1.0" encoding="UTF-8"?>
<tbx xmlns="urn:iso:std:iso:30042:ed-2" type="TBX-Basic" style="dca" xml:lang="en">
  <tbxHeader>
    <fileDesc>
      <sourceDesc><p>In-house conformance fixture</p></sourceDesc>
    </fileDesc>
  </tbxHeader>
  <text>
    <body>
      <conceptEntry id="c1">
        <langSec xml:lang="en">
          <termSec id="t1">
            <term>planet</term>
            <termNote type="partOfSpeech">noun</termNote>
            <termNote type="administrativeStatus">preferredTerm-admn-sts</termNote>
          </termSec>
        </langSec>
        <langSec xml:lang="es">
          <termSec id="t2">
            <term>planeta</term>
            <termNote type="partOfSpeech">noun</termNote>
            <termNote type="grammaticalGender">masculine</termNote>
            <termNote type="administrativeStatus">preferredTerm-admn-sts</termNote>
          </termSec>
        </langSec>
      </conceptEntry>
    </body>
  </text>
</tbx>
"""


def test_vendored_valid_example_is_clean():
    """The vendored upstream Example_Astronomy_DCA_VALID.tbx is pure TBX-Basic, so BOTH
    engines and the combined validate() must return ZERO findings."""
    if not _schemas():
        print("  in-house valid example: SKIPPED (official schema files not installed)")
        return
    data = _INHOUSE_VALID.encode("utf-8")
    assert tbx_conformance.relaxng_findings(data) == [], (
        "RELAX NG must accept the vendored valid file"
    )
    assert tbx_conformance.non_los_findings(data) == [], "no non-los conformance findings expected"
    findings = tbx_conformance.validate(data)
    assert findings == [], [repr(f) for f in findings][:5]
    print("  vendored valid example: RELAX NG + Schematron both clean (0 findings) OK")


# ===========================================================================
# 3. a normal export carries ONLY los: findings
# ===========================================================================
def test_normal_export_only_los_findings():
    """A default tbx-basic export (los: on) of a glossary carrying a DNT term, an
    out-of-picklist partOfSpeech and a grammaticalNumber has NO real conformance
    failure: every finding is solely the DCT-guard flagging one of our los: elements,
    and those los: elements actually appear in the bytes."""
    if not _schemas():
        print("  normal export: SKIPPED (official schema files not installed)")
        return
    out = _seeded_export("Conf normal")
    # only los: findings -- no real conformance failure.
    assert tbx_conformance.non_los_findings(out) == [], [
        repr(f) for f in tbx_conformance.non_los_findings(out)
    ][:5]
    findings = tbx_conformance.validate(out)
    assert findings, "a los-bearing export MUST produce the DCT-guard los: findings"
    assert all(tbx_conformance.is_los_finding(f) for f in findings), [
        repr(f) for f in findings if not tbx_conformance.is_los_finding(f)
    ][:5]
    # the los: elements really rode along (DNT + the out-of-picklist fallbacks).
    assert b"xmlns:los" in out, out[:200]
    assert b"<los:dnt" in out, "the DNT term must export its los:dnt extension"
    assert b"<los:partOfSpeech" in out, "the out-of-picklist POS must fall back to los:"
    assert b"<los:grammaticalNumber" in out, "grammaticalNumber has no TBX-Basic home -> los:"
    print(
        "  normal export: no non-los findings, every finding is a los: DCT-guard hit, "
        "los:dnt/partOfSpeech/grammaticalNumber all present OK"
    )


# ===========================================================================
# 4. a strict-portable export is clean pure TBX-Basic
# ===========================================================================
def test_strict_portable_export_is_zero_findings():
    """strict_portable=True drops los: entirely (and its xmlns declaration) and validates
    with ZERO findings from either engine -- a clean, fully portable TBX-Basic instance."""
    if not _schemas():
        print("  strict-portable export: SKIPPED (official schema files not installed)")
        return
    out = _seeded_export("Conf portable", strict_portable=True)
    findings = tbx_conformance.validate(out)
    assert findings == [], [repr(f) for f in findings][:5]
    assert b"xmlns:los" not in out, "portable must not declare the los: namespace"
    assert b"<los:" not in out, "portable must emit no los: elements"
    print("  strict-portable export: 0 findings (both engines), no los: namespace or elements OK")


# ===========================================================================
# 5. the vendored picklists match the spec
# ===========================================================================
def test_picklists_from_vendored_sch():
    """Every §6.1 picklist is present in the parsed .sch; grammaticalNumber has no
    category (E-T2-4); the picklists carry the spec-pinned values."""
    for datcat in ("partOfSpeech", "grammaticalGender", "termType", "administrativeStatus"):
        vals = tbx_conformance.permitted_values(datcat)
        assert vals, (datcat, "must have a non-empty picklist")
        # the built-in list (used when the official files are absent) must equal the file's
        assert vals == tbx_conformance._BUILTIN_VALUES[datcat], (datcat, vals)
    for elem in ("termNote", "descrip", "admin"):
        assert tbx_conformance._permitted_types(elem) == tbx_conformance._BUILTIN_TYPES[elem], elem
    assert tbx_conformance.permitted_values("grammaticalNumber") == [], (
        "grammaticalNumber has no TBX-Basic data category (E-T2-4)"
    )
    assert "phrase" in tbx_conformance.permitted_values("termType"), (
        tbx_conformance.permitted_values("termType")
    )
    assert "properNoun" not in tbx_conformance.permitted_values("partOfSpeech"), (
        "partOfSpeech is a closed picklist (adjective/noun/other/verb/adverb) -- properNoun "
        "is exactly the value that must fall back to los:"
    )
    # the permitted-termnote-types surface enumerates the datcats the DCA dialect allows.
    tn_types = tbx_conformance.permitted_termnote_types()
    assert "partOfSpeech" in tn_types and "administrativeStatus" in tn_types, tn_types
    print(
        "  picklists: partOfSpeech/grammaticalGender/termType/administrativeStatus non-empty, "
        "grammaticalNumber absent, 'phrase' in termType, 'properNoun' not in partOfSpeech OK"
    )


# ===========================================================================
# 6. hand-built INVALID samples are caught by each engine
# ===========================================================================
def test_invalid_samples_are_refused():
    """From a known-valid portable export (0 findings), two targeted mutations prove each
    engine independently bites: a bad administrativeStatus picklist value -> a non-los
    Schematron finding; a wrong element name (langSec -> langSection) -> a RELAX NG
    finding. Also proves glossary_tbx.validate_strict refuses the picklist case."""
    from app.services import glossary_tbx

    if not _schemas():
        print("  invalid samples: SKIPPED (official schema files not installed)")
        return
    base = _seeded_export("Conf invalid base", strict_portable=True)
    assert tbx_conformance.validate(base) == [], "the mutation base must start clean"
    assert b"administrativeStatus" in base and b"preferredTerm-admn-sts" in base, base[:400]

    # --- (a) bad picklist value -> Schematron ---
    bad_pick = base.replace(b"preferredTerm-admn-sts", b"bogusValue", 1)
    assert bad_pick != base, "the mutation must have changed the bytes"
    sch = [f for f in tbx_conformance.evaluate(bad_pick) if f.engine == "schematron"]
    assert sch, "a bad administrativeStatus value must raise a Schematron finding"
    assert not tbx_conformance.is_los_finding(sch[0]), (
        "a real picklist failure is NOT a los: finding"
    )
    nonlos = tbx_conformance.non_los_findings(bad_pick)
    assert nonlos and any(f.engine == "schematron" for f in nonlos), [repr(f) for f in nonlos][:5]
    # this is a Schematron catch, not a structural one: the RELAX NG still accepts it.
    assert tbx_conformance.relaxng_findings(bad_pick) == [], (
        "a bad picklist VALUE is a Schematron concern, the structure is still valid"
    )
    # and strict validation refuses it, writing nothing.
    try:
        glossary_tbx.validate_strict(bad_pick)
    except glossary_tbx.TbxError as exc:
        assert "strict TBX validation failed" in str(exc), exc
    else:
        raise AssertionError("validate_strict must refuse a bad picklist value")

    # --- (b) wrong element -> RELAX NG ---
    bad_struct = base.replace(b"<langSec ", b"<langSection ").replace(
        b"</langSec>", b"</langSection>"
    )
    assert bad_struct != base, "the mutation must have changed the bytes"
    rng = tbx_conformance.relaxng_findings(bad_struct)
    assert rng, "an unknown element (langSection) must raise a RELAX NG finding"
    assert any(f.engine == "relaxng" for f in tbx_conformance.non_los_findings(bad_struct)), (
        "the structural failure must surface as a relaxng finding in non_los_findings"
    )
    print(
        "  invalid samples: bad picklist -> Schematron (RNG clean) + validate_strict refuses; "
        "wrong element -> RELAX NG; both engines bite OK"
    )


def test_official_files_are_not_shipped():
    """The upstream TBX-Basic files carry no licence, so the repo must not contain them:
    only the fetch script and the pinned sources ship. THIRD_PARTY_NOTICES.md says so and
    PROVENANCE.md records the sha256 each downloaded file is checked against."""
    tracked_dir = os.path.join(_ROOT, "app", "resources", "tbx")
    committed = subprocess_ls_files(tracked_dir)
    for name in ("TBXcoreStructV03.rng", "TBX-Basic_DCA.sch", "Example_Astronomy_DCA_VALID.tbx"):
        assert name not in committed, f"{name} must not be committed (no upstream licence)"
    with open(os.path.join(_ROOT, "THIRD_PARTY_NOTICES.md"), encoding="utf-8") as fh:
        text = fh.read()
    assert "not shipped" in text and "fetch_tbx_schemas.py" in text
    with open(os.path.join(tracked_dir, "PROVENANCE.md"), encoding="utf-8") as fh:
        prov = fh.read()
    for name, (_url, _size, sha) in tbx_conformance.SCHEMA_SOURCES.items():
        assert name in prov and sha in prov, f"PROVENANCE.md must pin {name}"
    print("  official TBX files are not committed; notices + provenance pin them OK")


def subprocess_ls_files(path):
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "--", path], cwd=_ROOT, capture_output=True, text=True
    ).stdout
    return {os.path.basename(line) for line in out.splitlines() if line.strip()}


def test_missing_files_are_reported_clearly():
    """With no official files installed, strict validation refuses with the fetch instructions
    and export keeps working from the built-in picklists."""
    import tempfile

    from app.services import glossary_tbx

    saved = (tbx_conformance.schema_dirs, tbx_conformance._rng_cache, tbx_conformance._sch_cache)
    empty = tempfile.mkdtemp(prefix="tbx_empty_")
    try:
        tbx_conformance.schema_dirs = lambda: [__import__("pathlib").Path(empty)]
        tbx_conformance._rng_cache = None
        tbx_conformance._sch_cache = None
        assert not tbx_conformance.schemas_available()
        try:
            glossary_tbx.validate_strict(_INHOUSE_VALID.encode("utf-8"))
        except glossary_tbx.TbxError as exc:
            msg = str(exc)
            assert "fetch_tbx_schemas.py" in msg and "TBXcoreStructV03.rng" in msg, msg
        else:
            raise AssertionError("validate_strict must refuse when the official files are absent")
        assert tbx_conformance.permitted_values("partOfSpeech") == [
            "adjective",
            "noun",
            "other",
            "verb",
            "adverb",
        ]
        out = _seeded_export("Conf nofiles", strict_portable=True)
        assert b"<conceptEntry" in out, out[:200]
    finally:
        tbx_conformance.schema_dirs, tbx_conformance._rng_cache, tbx_conformance._sch_cache = saved
    print("  missing files: strict refuses with fetch instructions; export still works OK")


def main():
    init_db()
    # The concept editor (concept-preserving TBX import/export write path) is DEFAULT-OFF;
    # turn it on (after init_db so the setting table exists) so the concept import/export
    # is unconditional, not just the empty-seed path.
    _settings.set_many({"terminology_concept_editor_enabled": "1"})
    _settings.invalidate_cache()
    test_assert_count_matches_vendored_sch()
    test_vendored_valid_example_is_clean()
    test_normal_export_only_los_findings()
    test_strict_portable_export_is_zero_findings()
    test_picklists_from_vendored_sch()
    test_invalid_samples_are_refused()
    test_official_files_are_not_shipped()
    test_missing_files_are_reported_clearly()
    print("TBX BASIC CONFORMANCE OK")


if __name__ == "__main__":
    main()
