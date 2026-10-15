"""Vendor Studio's own XLIFF 1.2 writer/parser (Vendor Wave D).

VS ships an INDEPENDENT implementation (stdlib ElementTree, no `app/` import, no shared
code with Localization OS's lxml version). These tests prove it in isolation: writer shape +
provenance mapping, writer<->parser round trip (placeholders survive), foreign-file
tolerance, and the two HTTP download routes (`/package?format=xliff`, `/export?format=xliff`).
The Loc<->VS cross-validation lives in the main-app suite verify_vendor_xliff [G].
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from conftest import sample_contract
from vstudio import delivery, editor, intake, xliff

NS = "urn:oasis:names:tc:xliff:document:1.2"


def _store(session, **kw):
    payload = sample_contract(**kw)
    return intake.store_dispatch(session, payload, endpoint_host="loc.example.test")


# --- writer -------------------------------------------------------------------
def test_package_writer_shape_and_mappings(session):
    proj = _store(session, locked_last=True)
    segs = delivery.project_segments(session, proj)
    xb = xliff.render_package_xliff(proj, segs)
    root = ET.fromstring(xb)
    assert root.tag == f"{{{NS}}}xliff" and root.get("version") == "1.2"
    file_el = root.find(f"{{{NS}}}file")
    assert file_el.get("source-language") == "en-US"
    assert file_el.get("target-language") == "es-ES"
    tus = {tu.get("id"): tu for tu in root.iter(f"{{{NS}}}trans-unit")}
    assert set(tus) == {"1", "2", "3"}
    # seg 1 was a prefilled MT row -> mt-suggestion; seg 3 is locked tm_exact -> translate=no
    assert tus["1"].find(f"{{{NS}}}target").get("state-qualifier") == "mt-suggestion"
    assert tus["3"].get("translate") == "no"
    assert tus["3"].find(f"{{{NS}}}target").get("state-qualifier") == "exact-match"
    # only verified-core qualifiers are emitted
    quals = {t.get("state-qualifier") for t in root.iter(f"{{{NS}}}target")}
    quals.discard(None)
    assert quals <= {"exact-match", "fuzzy-match", "mt-suggestion"}, quals


def test_writer_parser_round_trip_placeholders(session):
    proj = _store(session)
    segs = delivery.project_segments(session, proj)
    xb = xliff.render_package_xliff(proj, segs)
    out = xliff.parse_xliff(xb)
    assert [u["id"] for u in out["units"]] == ["1", "2", "3"]
    by = {u["id"]: u for u in out["units"]}
    # the ⟦x1⟧ placeholder + source text survive byte-identically
    assert by["1"]["source"] == "Hello ⟦x1⟧ world."
    assert by["1"]["target"] == "Hola ⟦x1⟧ mundo (MT)."
    assert out["target_locale"] == "es-ES"


def test_return_writer_carries_delivered_targets(session):
    proj = _store(session)
    segs = {s.contract_id: s for s in delivery.project_segments(session, proj)}
    editor.patch_target(session, proj, segs["2"], "El gato se sento.", actor="tester")
    fresh = {s.contract_id: s for s in delivery.project_segments(session, proj)}
    units = [
        {"id": cid, "source": fresh[cid].source, "target": fresh[cid].target}
        for cid in ("1", "2", "3")
    ]
    xb = xliff.render_return_xliff(proj, units)
    out = xliff.parse_xliff(xb)
    assert {u["id"]: u["target"] for u in out["units"]}["2"] == "El gato se sento."
    # delivered targets are marked state="translated"
    root = ET.fromstring(xb)
    tgt2 = [  # noqa: RUF015
        tu.find(f"{{{NS}}}target") for tu in root.iter(f"{{{NS}}}trans-unit") if tu.get("id") == "2"
    ][0]
    assert tgt2.get("state") == "translated"


# --- foreign-file tolerance ---------------------------------------------------
def test_parser_tolerates_foreign_namespaces():
    doc = (
        '<?xml version="1.0"?>'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" '
        'xmlns:mq="MQXliff" xmlns:gs4tr="http://www.gs4tr.org/schema/xliff-ext">'
        '<file source-language="en-US" target-language="ja-JP"><body>'
        '<trans-unit id="1" mq:status="Confirmed" gs4tr:locked="true">'
        '<source>Hello.</source><target mq:locked="locked">Konnichiwa.</target></trans-unit>'
        "</body></file></xliff>"
    )
    out = xliff.parse_xliff(doc)
    assert out["units"] == [{"id": "1", "source": "Hello.", "target": "Konnichiwa."}]
    assert out["target_locale"] == "ja-JP"


def test_parser_refuses_malformed():
    import pytest

    with pytest.raises(xliff.XliffError):
        xliff.parse_xliff(b"not xml at all")
    with pytest.raises(xliff.XliffError):
        xliff.parse_xliff("<xliff><file><body></body></file></xliff>")  # no trans-units


# A GOLDEN file in exactly the shape Localization OS's INDEPENDENT (lxml) writer emits. VS is
# import-isolated from app/, so cross-validation is pinned by this wire sample rather than a
# cross-import: if either side drifts off the shared 1.2 profile this stops parsing. The
# reverse direction (Loc parsing a VS-shaped file) is pinned in tests/verify_vendor_xliff.py.
_LOC_GOLDEN = (
    "<?xml version='1.0' encoding='UTF-8'?>\n"
    '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">'
    '<file original="vendor" source-language="en-US" target-language="es-ES" '
    'datatype="plaintext"><body>'
    '<trans-unit id="1"><source xml:lang="en-US">Hello ⟦x1⟧ world.</source>'
    '<target xml:lang="es-ES" state="needs-review-translation" '
    'state-qualifier="mt-suggestion">Hola ⟦x1⟧ mundo.</target></trans-unit>'
    '<trans-unit id="2" translate="no"><source xml:lang="en-US">The cat sat.</source>'
    '<target xml:lang="es-ES" state="translated" state-qualifier="exact-match">El gato.'
    "</target></trans-unit></body></file></xliff>"
)


def test_vs_parser_reads_loc_shaped_file():
    out = xliff.parse_xliff(_LOC_GOLDEN)
    assert [u["id"] for u in out["units"]] == ["1", "2"]
    by = {u["id"]: u for u in out["units"]}
    assert by["1"]["source"] == "Hello ⟦x1⟧ world."
    assert by["1"]["target"] == "Hola ⟦x1⟧ mundo."
    assert out["target_locale"] == "es-ES"


# --- fix-round hardening (parity with Localization OS's parser) --------------------
def test_vs_parser_refuses_doctype_and_inline_and_oversized_id():
    import pytest

    doctype = (
        '<?xml version="1.0"?><!DOCTYPE xliff [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body><trans-unit id="1"><target>&x;</target>'
        "</trans-unit></body></file></xliff>"
    )
    with pytest.raises(xliff.XliffError):
        xliff.parse_xliff(doctype)
    inline = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body><trans-unit id="1">'
        '<target>Hola <x id="1"/> mundo.</target></trans-unit></body></file></xliff>'
    )
    with pytest.raises(xliff.XliffError):
        xliff.parse_xliff(inline)
    big = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body>'
        f'<trans-unit id="{"9" * 300}"><target>x</target></trans-unit>'
        "</body></file></xliff>"
    )
    with pytest.raises(xliff.XliffError):
        xliff.parse_xliff(big)


def test_vs_parser_ignores_nested_alt_trans_unit():
    doc = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body>'
        '<trans-unit id="1"><source>S</source><target>DELIVERED</target>'
        '<alt-trans match-quality="80%"><target>CANDIDATE</target>'
        '<trans-unit id="999"><target>NESTED</target></trans-unit></alt-trans>'
        "</trans-unit></body></file></xliff>"
    )
    out = xliff.parse_xliff(doc)
    assert [u["id"] for u in out["units"]] == ["1"]
    assert out["units"][0]["target"] == "DELIVERED"


def test_vs_parser_refuses_utf16_doctype():
    import pytest

    xml_str = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE xliff [<!ENTITY x "y">]>'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file target-language="es-ES"><body><trans-unit id="1"><target>t</target>'
        "</trans-unit></body></file></xliff>"
    )
    data16 = xml_str.encode("utf-16")
    assert b"<!DOCTYPE" not in data16  # ASCII prescan cannot see it; expat must
    with pytest.raises(xliff.XliffError):
        xliff.parse_xliff(data16)


def test_vs_writer_sanitizes_control_chars():
    proj = type(
        "P",
        (),
        {
            "source_locale": "en-US",
            "target_locale": "es-ES",
            "run_ref": "r",
            "glossary": None,
            "style_guide": "",
            "context": "",
        },
    )()
    seg = type(
        "S",
        (),
        {
            "contract_id": "1",
            "source": "a\x0c\x00b",
            "target": "t\x0bt",
            "provenance": "mt",
            "match_score": None,
            "locked": False,
        },
    )()
    xb = xliff.render_package_xliff(proj, [seg])
    ET.fromstring(xb)  # valid XML, no raise
    out = xliff.parse_xliff(xb)
    assert "\x0c" not in out["units"][0]["source"] and "�" in out["units"][0]["source"]


# --- HTTP routes --------------------------------------------------------------
def test_package_and_export_xliff_routes(client, session):
    proj = _store(session)
    r = client.get(f"/projects/{proj.id}/package?format=xliff")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-disposition"] == f'attachment; filename="package-{proj.id}.xlf"'
    parsed = xliff.parse_xliff(r.content)
    assert [u["id"] for u in parsed["units"]] == ["1", "2", "3"]

    r2 = client.get(f"/projects/{proj.id}/export?format=xliff")
    assert r2.status_code == 200, r2.text[:200]
    assert r2.headers["content-disposition"] == f'attachment; filename="return-{proj.id}.xlf"'
    ET.fromstring(r2.content)  # well-formed

    # the JSON forms still work unchanged (intake/export stays JSON-canonical)
    rj = client.get(f"/projects/{proj.id}/export")
    assert rj.status_code == 200
    assert rj.headers["content-type"].startswith("application/json")
