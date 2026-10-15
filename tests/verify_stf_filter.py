"""Salesforce Translation Workbench .stf filter (app/filters/stf_filter.py): Source,
Outdated and untranslated and Bilingual exports, byte-faithful round trip, the import
shape Salesforce expects, escapes and placeholders, encodings, hostile input.

Hand-written fixtures only. Hermetic + offline. Final line: "STF FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_stf_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError
from app.filters.stf_filter import over_limit, records, salesforce_language_code

F = get_filter("stf")
T = "\t"

BILINGUAL = (
    "# Use the Bilingual file to review translations and add translations.\n"
    "# - The TRANSLATED section of the file contains text that has been translated.\n"
    "# - The UNTRANSLATED section of the file contains text that hasn't been translated.\n"
    "Language code: de\n"
    "Type: Bilingual\n"
    "Translation type: Metadata\n"
    "\n"
    "------------------TRANSLATED-------------------\n"
    "\n"
    f"# KEY{T}LABEL{T}TRANSLATION{T}OUT OF DATE\n"
    "\n"
    "# CustomField\n"
    f"CustomField.Account.Region.FieldLabel{T}Region{T}Region{T}-\n"
    f"CustomField.Account.Region.HelpText{T}Pick the sales region.\\nOne per account."
    f"{T}Verkaufsregion.{T}*\n"
    "\n"
    "------------------UNTRANSLATED-----------------\n"
    "\n"
    f"# KEY{T}LABEL\n"
    "\n"
    "# CustomLabel\n"
    f"CustomLabel.Welcome{T}Welcome back, {{0}}! See {{!$User.FirstName}}.\n"
    f"PicklistValue.Account.Industry.Agriculture{T}Agriculture\n"
    f"CustomLabel.Blank{T}\n"
    f"CustomLabel.Dots{T}\\n\n"
)

SOURCE = (
    "# Use the Source file to translate labels for the first time.\r\n"
    "Language code: en_US\r\n"
    "Type: Source\r\n"
    "Translation type: Metadata\r\n"
    "\r\n"
    f"# KEY{T}LABEL\r\n"
    "\r\n"
    f"ButtonOrLink.Account.Call{T}Call now\r\n"
    f"LayoutSection.Account.Account Layout.Address{T}Address Information\r\n"
)

OUTDATED = (
    "Language code: fr\n"
    "Type: Outdated and untranslated\n"
    "\n"
    "------------------OUTDATED AND UNTRANSLATED-----------------\n"
    "\n"
    f"# KEY{T}LABEL\n"
    f"CustomLabel.Save{T}Save\n"
)


def _texts(data: bytes) -> list[str]:
    return [u.text for u in F.parse(data).units]


def _all(data: bytes, fn) -> dict:
    return {str(u.p_index): fn(u.text) for u in F.parse(data).units}


def _pt(text: str) -> str:
    """Mock translation that keeps point tokens where they are."""
    return "[de] " + text


def test_registration():
    assert "stf" in supported_exts()
    assert get_filter("stf").fmt == "stf"
    from app.services.connectors import msgraph
    from app.services.mime import MIME_BY_FMT

    assert msgraph.FORMATS.get("stf") == "stf"
    assert MIME_BY_FMT["stf"].startswith("text/plain")
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        assert ".stf," in fh.read()
    for rel in ("llms.txt", "docs/CLI.md", "app/capabilities/documents.md"):
        with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
            body = fh.read()
        assert "Salesforce" in body and ".stf" in body, rel
    print("  [reg] stf registered, accept list, msgraph, mime and docs OK")


def test_bilingual_units_and_identity():
    data = BILINGUAL.encode("utf-8")
    texts = _texts(data)
    assert texts == [
        "Region",
        "Pick the sales region.⟦x1⟧One per account.",
        "Welcome back, ⟦x1⟧! See ⟦x2⟧.",
        "Agriculture",
    ], texts
    styles = [u.style for u in F.parse(data).units]
    assert styles == ["stf:translated"] * 2 + ["stf:untranslated"] * 2, styles
    # identity: nothing changed -> every byte the same (build and build_localized to same lang)
    assert F.build(data, {}) == data
    assert F.build(data, _all(data, lambda t: t)) == data
    assert F.build_localized(data, {}, "de") == data
    sk = F.parse(data).skeleton
    assert sk["type"] == "Bilingual" and sk["language_code"] == "de", sk
    assert sk["units"][0]["max_length"] == 40 and sk["units"][1]["max_length"] == 510
    assert sk["units"][2]["key"] == "CustomLabel.Welcome"
    print("  [bilingual] units, sections, identity round trip OK")


def test_bilingual_translated_import_shape():
    data = BILINGUAL.encode("utf-8")
    out = F.build_localized(data, _all(data, _pt), "de-DE").decode("utf-8")
    lines = out.split("\n")
    # TRANSLATED rows: KEY and LABEL kept, TRANSLATION replaced, OUT OF DATE kept
    assert f"CustomField.Account.Region.FieldLabel{T}Region{T}[de] Region{T}-" in lines, out
    assert (
        f"CustomField.Account.Region.HelpText{T}Pick the sales region.\\nOne per account."
        f"{T}[de] Pick the sales region.\\nOne per account.{T}*" in lines
    ), out
    # UNTRANSLATED rows: translation in place of LABEL, key untouched
    assert f"CustomLabel.Welcome{T}[de] Welcome back, {{0}}! See {{!$User.FirstName}}." in lines, (
        out
    )
    assert f"PicklistValue.Account.Industry.Agriculture{T}[de] Agriculture" in lines, out
    assert f"CustomLabel.Blank{T}" in lines and f"CustomLabel.Dots{T}\\n" in lines
    # Language code stamped in Salesforce form; headers and comments otherwise identical
    assert "Language code: de_DE" in lines, out
    assert "Type: Bilingual" in lines and f"# KEY{T}LABEL{T}TRANSLATION{T}OUT OF DATE" in lines

    # every key column is byte-identical, row count unchanged
    def keys(rows):
        return [ln.split(T)[0] for ln in rows if T in ln and not ln.startswith("#")]

    src_keys, out_keys = keys(BILINGUAL.split("\n")), keys(lines)
    assert src_keys == out_keys
    # re-parse of the output still yields the same units (stable unit walk)
    assert len(F.parse(out.encode("utf-8")).units) == 4
    print("  [bilingual] TRANSLATION column filled, LABEL replaced in UNTRANSLATED OK")


def test_partial_translation_keeps_reviewed():
    data = BILINGUAL.encode("utf-8")
    out = F.build(data, {"1": "Verkaufsregion auswählen.⟦x1⟧Eine pro Konto."}).decode("utf-8")
    # unit 0 not provided -> existing reviewed translation stays
    assert f"FieldLabel{T}Region{T}Region{T}-" in out
    assert f"{T}Verkaufsregion auswählen.\\nEine pro Konto.{T}*\n" in out, out
    print("  [partial] untouched units keep the existing translation OK")


def test_source_and_outdated_files():
    data = SOURCE.encode("utf-8")
    assert _texts(data) == ["Call now", "Address Information"]
    assert F.build(data, {}) == data
    out = F.build_localized(data, {"0": "Jetzt anrufen", "1": "Adressinformationen"}, "de")
    s = out.decode("utf-8")
    assert "Language code: de\r\n" in s, s
    assert f"ButtonOrLink.Account.Call{T}Jetzt anrufen\r\n" in s
    assert f"LayoutSection.Account.Account Layout.Address{T}Adressinformationen\r\n" in s
    assert "Type: Source\r\n" in s and s.count("\r\n") == SOURCE.count("\r\n")
    od = OUTDATED.encode("utf-8")
    assert [u.style for u in F.parse(od).units] == ["stf:outdated and untranslated"]
    o = F.build_localized(od, {"0": "Enregistrer"}, "fr-FR").decode("utf-8")
    assert f"CustomLabel.Save{T}Enregistrer\n" in o and "Language code: fr_FR\n" in o
    print("  [source/outdated] LABEL replaced, CRLF kept, Language code stamped OK")


def test_escaping_new_text_and_tokens():
    data = BILINGUAL.encode("utf-8")
    # A raw tab / newline in a translation must not split the row; unknown tokens dropped.
    out = F.build(data, {"3": "Land\twirtschaft\r\nneu⟦x9⟧", "2": "⟦x2⟧ hallo ⟦x1⟧"})
    s = out.decode("utf-8")
    assert f"{T}Agriculture\n" not in s
    assert f"PicklistValue.Account.Industry.Agriculture{T}Land\\twirtschaft\\nneu\n" in s, s
    assert f"CustomLabel.Welcome{T}{{!$User.FirstName}} hallo {{0}}\n" in s, s
    assert len(s.split("\n")) == len(BILINGUAL.split("\n"))
    print("  [escapes] raw tab/newline escaped, tokens reordered and restored OK")


def test_missing_translation_cell_and_limits():
    text = (
        "Language code: de\nType: Bilingual\n------------------TRANSLATED-------------------\n"
        f"# KEY{T}LABEL{T}TRANSLATION{T}OUT OF DATE\n"
        f"CustomField.Account.Tier.FieldLabel{T}Tier\n"
    )
    data = text.encode("utf-8")
    long = "Sehr langer Feldname der viel zu lang ist fuer Salesforce"
    out = F.build(data, {"0": long}).decode("utf-8")
    assert f"FieldLabel{T}Tier{T}{long}\n" in out, out
    rep = over_limit(data, {"0": long})
    assert rep and rep[0]["max_length"] == 40 and rep[0]["length"] == len(long), rep
    recs = records(BILINGUAL.encode("utf-8"))
    assert [r["key"] for r in recs][:2] == [
        "CustomField.Account.Region.FieldLabel",
        "CustomField.Account.Region.HelpText",
    ]
    assert recs[1]["translation"] == "Verkaufsregion." and recs[1]["out_of_date"] is True
    assert recs[0]["out_of_date"] is False and recs[3]["max_length"] == 255
    assert recs[2]["translation"] is None and len(recs) == 6
    print("  [limits] missing TRANSLATION cell padded, Salesforce limits reported OK")


def test_language_codes():
    cases = {
        "de": "de",
        "de-DE": "de_DE",
        "pt-BR": "pt_BR",
        "es-419": "es_419",
        "zh-Hans": "zh_CN",
        "zh-Hant-TW": "zh_TW",
        "zh-CN": "zh_CN",
        "nl": "nl_NL",
        "nl-BE": "nl_BE",
        "he": "iw",
        "id-ID": "in_ID",
        "nb": "no",
        "en-GB": "en_GB",
    }
    for bcp, sf in cases.items():
        assert salesforce_language_code(bcp) == sf, (bcp, salesforce_language_code(bcp))
    print("  [lang] BCP 47 -> Salesforce language codes OK")


def test_encodings():
    data = BILINGUAL.encode("utf-8")
    bom = b"\xef\xbb\xbf" + data
    assert _texts(bom) == _texts(data)
    assert F.build(bom, {}) == bom
    out = F.build(bom, {"0": "Gebiet"})
    assert out.startswith(b"\xef\xbb\xbf") and b"Gebiet" in out
    # Excel "Unicode Text" (UTF-16 LE with BOM) is read and exported as UTF-8
    u16 = BILINGUAL.encode("utf-16")
    assert _texts(u16) == _texts(data)
    sk = F.parse(u16).skeleton
    assert sk.get("source_encoding") == "utf-16"
    out16 = F.build(u16, {"0": "Gebiet"})
    assert out16.decode("utf-8").startswith("# Use the Bilingual") and "Gebiet" in out16.decode()
    # Windows-1252 file
    cp = "Language code: fr\n# KEY\tLABEL\nCustomLabel.Cafe\tCaf\xe9 cr\xe8me\n".encode("cp1252")
    assert _texts(cp) == ["Café crème"]
    assert F.build(cp, {"0": "Café"}).decode("utf-8").endswith("CustomLabel.Cafe\tCafé\n")
    print("  [enc] UTF-8 BOM kept, UTF-16 and cp1252 read and written as UTF-8 OK")


def test_hostile_inputs():
    # binary refused
    try:
        F.parse(b"\x00\x01\x02\x03" * 100)
        raise AssertionError("binary accepted")
    except UnsupportedEncodingError:
        pass
    # segment cap
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        rows = "".join(f"CustomLabel.K{i}\tText {i}\n" for i in range(10))
        try:
            F.parse(("Language code: de\n" + rows).encode())
            raise AssertionError("segment cap not enforced")
        except InputLimitError:
            pass
        # line budget (20 x cap) before any per-line work
        try:
            F.parse(("\n" * 200).encode())
            raise AssertionError("line budget not enforced")
        except InputLimitError:
            pass
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # long single cell
    old = config.MAX_SEGMENT_CHARS
    config.MAX_SEGMENT_CHARS = 50
    try:
        try:
            F.parse(("CustomLabel.X\t" + "a" * 100 + "\n").encode())
            raise AssertionError("segment length cap not enforced")
        except InputLimitError:
            pass
    finally:
        config.MAX_SEGMENT_CHARS = old
    # header-looking text after data is not a header; keyless rows and comments never units
    weird = (
        "Language code: de\n"
        f"\torphan label\n"
        f"CustomLabel.A{T}Alpha\n"
        "Language code: xx\n"
        f"#CustomLabel.B{T}Commented\n"
        "-----not a marker\n"
    ).encode()
    assert _texts(weird) == ["Alpha"]
    out = F.build_localized(weird, {"0": "A"}, "ja").decode()
    assert out.startswith("Language code: ja\n") and "Language code: xx\n" in out
    # a header value with a merge-field-like brace never runs or expands anything
    assert F.build(b"Language code: {!x}\n", {}) == b"Language code: {!x}\n"
    print("  [hostile] binary, caps, line budget, keyless rows, late headers OK")


def test_api_and_cli_round_trip():
    """Upload a .stf through the API and the CLI, mock-translate, export."""
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
        p = Project(name="stf", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id
    user = security.create_user("stf@x.test", "stf", Role.ADMIN, "stf-pw-123456")
    _row, raw_key = apikeys.create_key(user.id, "stf", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw_key}  # pregate: allow-secret throwaway test key
    data = BILINGUAL.encode("utf-8")

    def check(out: bytes, surface: str):
        text = out.decode("utf-8")
        assert "Language code: de_DE\n" in text, (surface, text)
        assert f"FieldLabel{T}Region{T}[{loc}] Region{T}-\n" in text, (surface, text)
        assert f"{T}Agriculture\n" not in text, (surface, text)
        assert "{0}" in text and "{!$User.FirstName}" in text, (surface, text)

    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("de.stf", data, "text/plain")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", loc])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": loc})
    assert r.status_code == 200, (r.status_code, r.text)
    check(r.content, "api")
    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    assert {"ext": "stf", "name": "Salesforce Translation Workbench"} in r.json()["formats"], r.text

    tmp = tempfile.mkdtemp(prefix="loctms_stf_cli_")
    src = os.path.join(tmp, "de.stf")
    with open(src, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    m = re.search(r"Uploaded #(\d+): de\.stf \(stf,", out)
    assert m, out
    cli_doc = int(m.group(1))
    code, out, err = run_cli(["doc", "translate", str(cli_doc), "--locale", loc])
    assert code == 0, (code, out, err)
    dest = os.path.join(tmp, "out.stf")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", loc, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        check(fh.read(), "cli")
    print("  [surfaces] API upload/download and CLI upload/export round trip OK")


def test_gate_row_and_header_edges():
    """Gate fixes: a label holding U+2028 / U+0085 / form feed stays ONE row (rows end at
    CR/LF only), and a `Language code:<TAB>de` header is a header, never a unit."""
    src = (
        f"Language code:{T}en_US\n"
        f"Type:{T}Source\n"
        f"# KEY{T}LABEL\n"
        f"CustomLabel.A{T}First second\x85third\x0cfourth\n"
        f"CustomLabel.B{T}Plain\n"
    ).encode()
    assert _texts(src) == ["First second\x85third\x0cfourth", "Plain"], _texts(src)
    assert F.build(src, {}) == src
    out = F.build_localized(src, {"0": "Erste zweite", "1": "Einfach"}, "de-DE").decode()
    assert out == (
        f"Language code:{T}de_DE\n"
        f"Type:{T}Source\n"
        f"# KEY{T}LABEL\n"
        f"CustomLabel.A{T}Erste zweite\n"
        f"CustomLabel.B{T}Einfach\n"
    ), out
    print("  [gate] U+2028 / NEL / FF labels stay one row; tab-separated header is a header OK")


def main():
    test_gate_row_and_header_edges()
    test_registration()
    test_bilingual_units_and_identity()
    test_bilingual_translated_import_shape()
    test_partial_translation_keeps_reviewed()
    test_source_and_outdated_files()
    test_escaping_new_text_and_tokens()
    test_missing_translation_cell_and_limits()
    test_language_codes()
    test_encodings()
    test_hostile_inputs()
    test_api_and_cli_round_trip()
    print("STF FILTER OK")


if __name__ == "__main__":
    main()
