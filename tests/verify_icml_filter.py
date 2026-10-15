"""ICML (Adobe InCopy Markup Language) and WCML filter: hermetic round-trip suite.

Hand-written ICML documents (no InCopy needed): one <Story> in a snippet <Document>
with the IDML story model. Untranslated documents come back byte-identical; a
translated document keeps every byte outside the translated story (declaration, aid
processing instructions, styles, hyperlinks, other stories). Nested styles, breaks,
tabs and special characters, footnotes, tables, hyperlinks, hidden conditions, the
target-language stamp, WCML multi-story articles, hostile input and caps.

Hermetic + offline. Final line: "ICML FILTER OK".
"""

# ruff: noqa: E501  (hand-written ICML fixtures are long XML literals)

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_icml_")

from lxml import etree

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.services.segment import find_placeholders

FILT = get_filter("icml")
BOLD = "CharacterStyle/Bold"
NORMAL = "CharacterStyle/$ID/[No character style]"

HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<?aid style="50" type="snippet" readerVersion="6.0" featureSet="513" product="20.0(73)" ?>\n'
    '<?aid SnippetType="InCopyInterchange"?>\n'
    '<Document DOMVersion="20.0" Self="d">\n'
    '\t<Color Self="Color/Black" Model="Process" Space="CMYK" ColorValue="0 0 0 100" Name="Black"/>\n'
    '\t<RootCharacterStyleGroup Self="rcsg"><CharacterStyle Self="CharacterStyle/Bold" Name="Bold" FontStyle="Bold"/></RootCharacterStyleGroup>\n'
)


def psr(inner: str, style: str = "ParagraphStyle/$ID/NormalParagraphStyle") -> str:
    return f'\t\t<ParagraphStyleRange AppliedParagraphStyle="{style}">\n{inner}\n\t\t</ParagraphStyleRange>'


def csr(inner: str, style: str = NORMAL, extra: str = "") -> str:
    return f'\t\t\t<CharacterStyleRange AppliedCharacterStyle="{style}"{extra}>{inner}</CharacterStyleRange>'


def story(sid: str, body: str) -> str:
    return (
        f'\t<Story Self="{sid}" AppliedTOCStyle="n" TrackChanges="false" StoryTitle="$ID/" AppliedNamedGrid="n">\n'
        '\t\t<StoryPreference OpticalMarginAlignment="false" FrameType="TextFrameType"/>\n'
        '\t\t<InCopyExportOption IncludeGraphicProxies="true" IncludeAllResources="false"/>\n'
        f"{body}\n\t</Story>\n"
    )


def icml(body: str, resources: str = "", tail: str = "") -> bytes:
    return (HEAD + resources + story("u1", body) + tail + "</Document>\n").encode("utf-8")


def units(data: bytes) -> list:
    return FILT.parse(data).units


def root_of(data: bytes):
    return etree.fromstring(data)


def visible_text(root) -> str:
    return "|".join("".join(c.itertext()) for c in root.iter("Content"))


def assert_refused(data: bytes, why: str):
    try:
        FILT.parse(data)
    except (ValueError, InputLimitError):
        return
    raise AssertionError(f"{why}: parse must refuse")


def outside(data: bytes, sid: str) -> bytes:
    """The bytes of a document with one story's element cut out."""
    start = data.index(f'<Story Self="{sid}"'.encode())
    end = data.index(b"</Story>", start) + len(b"</Story>")
    return data[:start] + data[end:]


# ---------------------------------------------------------------------- tests
def test_registered_everywhere():
    exts = supported_exts()
    assert "icml" in exts and "wcml" in exts, exts
    assert get_filter("wcml") is FILT
    from app.services.mime import mime_for_fmt
    from app.web.api_part2 import _FMT_LABELS

    assert mime_for_fmt("icml") == "application/xml"
    assert "InCopy" in _FMT_LABELS["icml"]
    for path, needle in (
        ("app/templates/project.html", ".icml,.wcml"),
        ("app/templates/help.html", "ICML"),
        ("llms.txt", ".icml"),
        ("app/capabilities/documents.md", "icml"),
        ("app/services/export.py", '"icml"'),
    ):
        with open(path, encoding="utf-8") as fh:
            assert needle in fh.read(), (path, needle)
    print(
        "  [surfaces] icml + wcml registered: ext, MIME, label, accept, help, llms.txt, export OK"
    )


def test_identity_and_untouched_bytes():
    hyper = '\t<Hyperlink Self="hl1" Name="site" Source="h1" Visible="false"><Properties><Destination type="object">HyperlinkURLDestination/x</Destination></Properties></Hyperlink>\n'
    body = psr(csr("<Content>First paragraph.</Content><Br/><Content>Second paragraph.</Content>"))
    data = icml(body, tail=hyper)
    us = units(data)
    assert [u.text for u in us] == ["First paragraph.", "Second paragraph."], [u.text for u in us]
    assert FILT.build(data, {}) == data, "empty mapping must return the exact bytes"
    same = FILT.build(data, {str(u.p_index): u.text for u in us})
    assert [u.text for u in units(same)] == [u.text for u in us]
    out = FILT.build(data, {str(us[1].p_index): "Zweiter Absatz, viel laenger."})
    assert outside(out, "u1") == outside(data, "u1"), "bytes outside the story must not change"
    assert out.startswith(data[: data.index(b"<Document")]), "declaration + aid PIs kept"
    assert [u.text for u in units(out)] == ["First paragraph.", "Zweiter Absatz, viel laenger."]
    # BOM kept
    bom = b"\xef\xbb\xbf" + data
    out = FILT.build(bom, {str(us[0].p_index): "Erster."})
    assert out.startswith(b"\xef\xbb\xbf<?xml"), out[:20]
    assert units(out)[0].text == "Erster."
    print("  [bytes] identity on no change; only the translated story is rewritten; BOM kept OK")


def test_nested_styles_one_segment_and_growth():
    body = psr(
        csr("<Content>Hello </Content>")
        + "\n"
        + csr("<Content>bold</Content>", BOLD)
        + "\n"
        + csr("<Content> world</Content>")
    )
    data = icml(body)
    (u,) = units(data)
    assert u.text == "Hello ⟦r1⟧bold⟦/r1⟧ world", u.text
    new = "⟦r1⟧Fettgedruckt⟦/r1⟧ ist hier, hallo liebe Welt"
    out = FILT.build(data, {str(u.p_index): new})
    ranges = root_of(out).findall(".//Story//CharacterStyleRange")
    got = [(r.get("AppliedCharacterStyle"), "".join(r.itertext()).strip()) for r in ranges]
    assert got == [(BOLD, "Fettgedruckt"), (NORMAL, "ist hier, hallo liebe Welt")], got
    assert units(out)[0].text == new
    print("  [styles] a sentence across ranges is ONE segment; grows and reorders OK")


def test_specials_footnote_table_hyperlink():
    footnote = (
        "<Footnote>" + psr(csr("<Content><?ACE 4?> Footnote text.</Content>")) + "</Footnote>"
    )
    table = (
        '<Table Self="t1" BodyRowCount="1" ColumnCount="2"><Row Self="t1r0" Name="0"/>'
        '<Column Self="t1c0" Name="0"/><Column Self="t1c1" Name="1"/>'
        '<Cell Self="t1i0" Name="0:0">' + psr(csr("<Content>Cell A</Content>")) + "</Cell>"
        '<Cell Self="t1i1" Name="1:0">' + psr(csr("<Content>Cell B</Content>")) + "</Cell></Table>"
    )
    body = psr(
        csr(
            "<Content>Page\t<?ACE 18?> see </Content>"
            '<HyperlinkTextSource Self="h1" Name="link"><Content>our site</Content></HyperlinkTextSource>'
            '<Content> now</Content><PageReference Self="p1" PageReferenceType="CurrentPage"/>'
            "<Content>.</Content>" + footnote
        )
    )
    body += "\n" + psr(csr(table))
    data = icml(body)
    us = units(data)
    texts = [u.text for u in us]
    assert texts == [
        "Page⟦x1⟧⟦x2⟧ see ⟦g3⟧our site⟦/g3⟧ now⟦x4⟧.⟦x5⟧",
        "⟦x1⟧ Footnote text.",
        "Cell A",
        "Cell B",
    ], texts
    mapping = {
        str(us[0].p_index): "Seite⟦x1⟧⟦x2⟧ siehe ⟦g3⟧unsere Seite⟦/g3⟧ jetzt⟦x4⟧.⟦x5⟧",
        str(us[1].p_index): "⟦x1⟧ Fussnote.",
        str(us[2].p_index): "Zelle A",
        str(us[3].p_index): "Zelle B",
    }
    out = FILT.build(data, mapping)
    root = root_of(out)
    assert "<Content>Seite\t<?ACE 18?> siehe </Content>" in out.decode("utf-8"), out
    assert len(root.findall(".//HyperlinkTextSource[@Self='h1']")) == 1
    assert "".join(root.find(".//HyperlinkTextSource").itertext()) == "unsere Seite"
    assert len(root.findall(".//PageReference")) == 1 and len(root.findall(".//Footnote")) == 1
    assert len(root.findall(".//Table/Cell")) == 2
    assert [u.text for u in units(out)] == list(mapping.values())
    print("  [inline] tab, <?ACE?>, hyperlink, index marker, footnote, table cells round-trip OK")


def test_hidden_condition_in_same_file_and_notes():
    conds = '\t<Condition Self="Condition/Print Only" Name="Print Only" Visible="false"/>\n'
    body = psr(
        csr("<Content>Visible </Content>")
        + csr("<Content>SECRET</Content>", extra=' AppliedConditions="Condition/Print%20Only"')
        + csr(
            "<Content> tail</Content><Note><ParagraphStyleRange><CharacterStyleRange><Content>editor note</Content></CharacterStyleRange></ParagraphStyleRange></Note>"
        )
    )
    data = icml(body, resources=conds)
    (u,) = units(data)
    assert "SECRET" not in u.text and "editor note" not in u.text, u.text
    out = FILT.build(data, {str(u.p_index): u.text.replace("Visible", "Sichtbar")})
    txt = visible_text(root_of(out))
    assert "SECRET" in txt and "editor note" in txt and "Sichtbar" in txt, txt
    print("  [hidden] hidden condition (declared in the file) and notes kept, never offered OK")


def test_target_language_stamped():
    langs = (
        '\t<Language Self="Language/$ID/English%3a USA" Name="$ID/English: USA" Id="269"/>\n'
        '\t<Language Self="Language/$ID/de_DE_2006" Name="$ID/de_DE_2006" Id="275"/>\n'
    )
    body = psr(csr("<Content>Hello</Content>", extra=' AppliedLanguage="$ID/English: USA"'))
    data = icml(body, resources=langs)
    (u,) = units(data)
    mapping = {str(u.p_index): "Hallo"}
    plain = root_of(FILT.build(data, mapping)).find(".//Story//CharacterStyleRange")
    assert plain.get("AppliedLanguage") == "$ID/English: USA"
    de = root_of(FILT.build_localized(data, mapping, "de-DE")).find(".//Story//CharacterStyleRange")
    assert de.get("AppliedLanguage") == "$ID/de_DE_2006", de.get("AppliedLanguage")
    fr = root_of(FILT.build_localized(data, mapping, "fr-FR")).find(".//Story//CharacterStyleRange")
    assert fr.get("AppliedLanguage") == "$ID/English: USA"  # undeclared: never invented
    print("  [language] build_localized stamps only a language the file declares OK")


def test_wcml_multi_story_article():
    ns = "urn:SmartConnection_v3"
    data = (
        HEAD.replace("<Document DOMVersion", f'<Document xmlns:ea="{ns}" DOMVersion')
        + "\t<ea:Stories>\n"
        + '\t<ea:Story ea:GUID="g1"><ea:StoryInfo><ea:SI_EL>head</ea:SI_EL></ea:StoryInfo>\n'
        + story("s1", psr(csr("<Content>Headline here</Content>")))
        + "\t</ea:Story>\n"
        + '\t<ea:Story ea:GUID="g2"><ea:StoryInfo><ea:SI_EL>body</ea:SI_EL></ea:StoryInfo>\n'
        + story("s2", psr(csr("<Content>Body text.</Content>")))
        + "\t</ea:Story>\n\t</ea:Stories>\n</Document>\n"
    ).encode()
    us = units(data)
    assert [u.text for u in us] == ["Headline here", "Body text."], [u.text for u in us]
    out = FILT.build(data, {str(us[1].p_index): "Fliesstext."})
    assert outside(out, "s2") == outside(data, "s2"), "headline story and wrappers byte-identical"
    assert [u.text for u in units(out)] == ["Headline here", "Fliesstext."]
    print(
        "  [wcml] WoodWing multi-story article: stories in order, untouched story byte-identical OK"
    )


def _snippet(sid: str, body: str, resources: str = "") -> str:
    """One InCopy snippet <Document> as Smart Connection v3 nests it in an <ea:Story>."""
    return (
        '\t<Document DOMVersion="8.0" Self="d">\n'
        '\t<Color Self="Color/Black" Model="Process" Space="CMYK" ColorValue="0 0 0 100" Name="Black"/>\n'
        + resources
        + story(sid, body)
        + "\t</Document>\n"
    )


def test_wcml_smart_connection_v3_root():
    # Issue #670: a real Smart Connection article has <ea:Stories> as its root and one
    # InCopy snippet <Document> inside each <ea:Story> (Okapi Test01.wcml layout).
    ns = "urn:SmartConnection_v3"
    hidden = '\t<Condition Self="Condition/Draft" Name="Draft" Visible="false"/>\n'
    shown = '\t<Condition Self="Condition/Draft" Name="Draft" Visible="true"/>\n'
    draft = ' AppliedConditions="Condition/Draft"'
    data = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
        f'<ea:Stories ea:WWVersion="2.0" xmlns:aic="http://ns.adobe.com/AdobeInCopy/2.0" xmlns:ea="{ns}">\n'
        '\t<ea:Story ea:GUID="g1" pi0="style=&quot;50&quot; type=&quot;snippet&quot;" '
        'pi1="SnippetType=&quot;InCopyInterchange&quot;">\n'
        "\t\t<ea:StoryInfo><ea:SI_EL>head</ea:SI_EL>"
        "<ea:SI_Snippet>Headline preview</ea:SI_Snippet></ea:StoryInfo>\n"
        + _snippet(
            "s1",
            psr(
                csr("<Content>Headline here</Content>")
                + csr("<Content> DRAFT</Content>", extra=draft)
            ),
            hidden,
        )
        + "\t</ea:Story>\n"
        '\t<ea:Story ea:GUID="g2" pi0="" pi1="">\n'
        "\t\t<ea:StoryInfo><ea:SI_EL>body</ea:SI_EL></ea:StoryInfo>\n"
        + _snippet("s2", psr(csr("<Content>Body text.</Content>", extra=draft)), shown)
        + "\t</ea:Story>\n"
        "\t<ea:wwsd_document><ea:wwsd_styles>\n"
        '\t<Document DOMVersion="8.0" Self="d"><Color Self="Color/Black" Name="Black"/></Document>\n'
        "\t</ea:wwsd_styles></ea:wwsd_document>\n"
        "</ea:Stories>\n"
    ).encode()
    assert get_filter("wcml").build(data, {}) == data, "no-change build is byte-identical"
    us = units(data)
    # The first snippet hides Draft; the second declares Draft visible: each story honours
    # only its own document's conditions. StoryInfo metadata is never offered.
    assert [u.text for u in us] == ["Headline here", "Body text."], [u.text for u in us]
    out = FILT.build(data, {str(us[1].p_index): "Fliesstext."})
    assert outside(out, "s2") == outside(data, "s2"), "wrapper and headline story byte-identical"
    assert out.startswith(data[: data.index(b'<Story Self="s2"')]), "ea:Story wrapper kept"
    assert [u.text for u in units(out)] == ["Headline here", "Fliesstext."]
    assert b"Headline preview" in out and b"DRAFT" in out
    loc = FILT.build_localized(data, {str(us[0].p_index): "Titel hier"}, "de-DE")
    assert [u.text for u in units(loc)] == ["Titel hier", "Body text."]
    # Only WoodWing's own namespace opens; another <Stories> root is still refused.
    assert_refused(data.replace(ns.encode(), b"urn:example"), "foreign Stories root")
    # A wrapper with no snippet story inside a <Document> holds nothing to translate.
    assert_refused(
        f'<ea:Stories xmlns:ea="{ns}"><ea:Story><Story Self="x"/></ea:Story></ea:Stories>'.encode(),
        "story outside a snippet document",
    )
    print(
        "  [wcml] Smart Connection v3 <ea:Stories> root: snippets walked, per-document conditions, "
        "wrapper bytes kept, foreign root refused OK"
    )


def test_unlocatable_story_falls_back_to_full_serialize():
    # A comment that mentions a story tag defeats the byte splice; the build must still
    # be correct (whole-document serialization under the original declaration).
    data = icml(psr(csr("<Content>Hello.</Content>")), tail="\t<!-- old <Story> removed -->\n")
    (u,) = units(data)
    out = FILT.build(data, {str(u.p_index): "Hallo."})
    assert out.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'), out[:80]
    assert b'<?aid SnippetType="InCopyInterchange"?>' in out
    assert units(out)[0].text == "Hallo."
    print("  [fallback] unlocatable story span: full re-serialization keeps decl + aid PIs OK")


def test_damaged_tokens_fuzz():
    import random

    rng = random.Random(1010)
    body = psr(
        csr(
            '<Content>Start\t</Content><HyperlinkTextSource Self="hz"><Content>link</Content>'
            '</HyperlinkTextSource><PageReference Self="pz"/><Content>mid<?ACE 7?>dle</Content>'
        )
        + csr("<Content> bold </Content>", BOLD)
        + csr("<Content> end.</Content><Br/><Content>Next para.</Content>")
    )
    data = icml(body)
    first = units(data)[0]
    toks = find_placeholders(first.text)
    for _ in range(150):
        picked = [t for t in toks if rng.random() > 0.2]
        picked += rng.sample(toks, k=rng.randint(0, 2))
        rng.shuffle(picked)
        text = "".join(t + rng.choice(["Wort", "zwei", " ", "drei"]) for t in picked)
        root = root_of(FILT.build(data, {str(first.p_index): text}))
        assert len(root.findall(".//HyperlinkTextSource[@Self='hz']")) == 1, text
        assert len(root.findall(".//PageReference[@Self='pz']")) == 1, text
        assert len(root.findall(".//Br")) == 1, text
        assert "⟦" not in visible_text(root), text
        assert root.find(".//RootCharacterStyleGroup") is not None
    print("  [fuzz] 150 shuffled/dropped/duplicated token sets rebuild well-formed stories OK")


def test_malformed_and_hostile():
    from app import config

    assert_refused(b"not xml at all", "not xml")
    assert_refused(b"PK\x03\x04 a zip renamed", "zip")
    assert_refused(b'<?xml version="1.0"?><Root><Story/></Root>', "wrong root")
    assert_refused(b'<?xml version="1.0"?><Document Self="d"/>', "no story")
    assert_refused(
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b"<Document><Story><ParagraphStyleRange><CharacterStyleRange><Content>&e;</Content>"
        b"</CharacterStyleRange></ParagraphStyleRange></Story></Document>",
        "XXE",
    )
    assert_refused(
        b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><Document><Story/></Document>',
        "billion laughs",
    )
    assert_refused(b"<Document><Story><ParagraphStyleRange>", "truncated")
    utf16 = '<?xml version="1.0" encoding="UTF-16"?><Document><Story/></Document>'.encode("utf-16")
    assert_refused(utf16, "UTF-16")
    # markup budget: refused before a tree is built
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 2
    try:
        try:
            FILT.parse(icml(psr(csr("<Content>x</Content>" * 100))))
        except InputLimitError:
            pass
        else:
            raise AssertionError("markup budget must be an InputLimitError")
        # segment cap during the walk (budget 40 x 3 is enough markup for this file)
        many = icml(psr(csr("<Br/>".join(f"<Content>p{i}</Content>" for i in range(4)))))
        config.MAX_DOCUMENT_SEGMENTS = 3
        assert many.count(b"<") < 120, many.count(b"<")
        try:
            FILT.parse(many)
        except InputLimitError as exc:
            assert "segments" in str(exc).lower(), exc
        else:
            raise AssertionError("segment cap must be an InputLimitError")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print(
        "  [hostile] non-XML, zip, wrong root, no story, XXE, entities, truncated, UTF-16, caps refused OK"
    )


LOC = "de-DE"


def _assert_icml_export(surface: str, data: bytes, original: bytes) -> None:
    assert outside(data, "u1") == outside(original, "u1"), surface
    txt = visible_text(root_of(data))
    assert f"[{LOC}]" in txt and "⟦" not in txt and "\t" in txt, (surface, txt)
    assert (
        root_of(data).find(f".//CharacterStyleRange[@AppliedCharacterStyle='{BOLD}']") is not None
    )


def test_every_surface_upload_translate_export():
    """Four-surface parity (ADR 0011): an .icml uploads, translates (mock engine) and
    exports as .icml on the API, web, CLI and MCP doors; junk is refused."""
    import asyncio
    import json
    import pathlib
    import re
    import tempfile

    import httpx
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli
    from tests._csrf import csrf_for

    init_db()
    with Session(engine) as s:
        p = Project(name="icml", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user(
        "icml@x.test", "icml", Role.ADMIN, "icml-pw-123456"
    )  # pregate: allow-secret throwaway test user
    _row, api_raw = apikeys.create_key(user.id, "icml", scopes="api docs admin")
    tmp = tempfile.mkdtemp(prefix="loctms_icml_surf_")
    data = icml(
        psr(
            csr("<Content>Hello </Content>")
            + csr("<Content>bold</Content>", BOLD)
            + csr("<Content> world.</Content><Br/><Content>Second\tline.</Content>")
        )
    )

    def translate(doc_id: int) -> None:
        code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
        assert code == 0, (code, out, err)

    # --- API
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": api_raw}  # pregate: allow-secret throwaway test key
    r = client.get("/api/formats", headers=hdr)
    exts = {f["ext"] for f in r.json()["formats"]}
    assert r.status_code == 200 and "icml" in exts and "wcml" in exts, r.text
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("story.icml", data, "application/xml")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    api_doc = r.json()["document_id"]
    translate(api_doc)
    r = client.get(f"/api/documents/{api_doc}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert ".icml" in r.headers.get("content-disposition", ""), r.headers
    _assert_icml_export("api", r.content, data)
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("evil.icml", b"<Document><Story>", "application/xml")},
    )
    assert r.status_code in (413, 422), (r.status_code, r.text)
    # a .wcml upload goes through the same filter
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("article.wcml", data, "application/xml")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    print(
        "  [api] /api/formats lists icml+wcml; upload, translate, download .icml; junk refused OK"
    )

    # --- web
    web = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    csrf_for(web)
    r = web.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("web.icml", data, "application/xml")},
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    m = re.match(r"^/documents/(\d+)$", r.headers["location"])
    assert m, r.headers["location"]
    web_doc = int(m.group(1))
    translate(web_doc)
    r = web.get(f"/documents/{web_doc}/export", params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    _assert_icml_export("web", r.content, data)
    print("  [web] upload, translate, export .icml OK")

    # --- CLI
    src = os.path.join(tmp, "cli.icml")
    with open(src, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    cli_doc = int(re.search(r"Uploaded #(\d+)", out).group(1))
    translate(cli_doc)
    dest = os.path.join(tmp, "cli.de.icml")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", LOC, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        _assert_icml_export("cli", fh.read(), data)
    print("  [cli] doc upload / translate / export .icml OK")

    # --- MCP
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    from tests._mcp_plan import aconfirmed

    mcp_server._allow_local_file_reads(True)
    mcp_server.BASE_URL = "http://testserver"
    mcp_server.TOKEN = api_raw  # pregate: allow-secret throwaway test key
    mcp_server.OUTPUT_DIR = pathlib.Path(tmp)
    mcp_server._client = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), timeout=60
    )
    up = json.loads(
        asyncio.run(
            aconfirmed(  # #757: plan first, then confirm with its plan_id
                mcp_server,
                "submit_translation",
                {"file_path": src, "targets": [], "project_id": pid, "confirm": True},
            )
        )
    )
    mcp_doc = int(up["document_id"])
    translate(mcp_doc)
    got = json.loads(
        asyncio.run(
            mcp_server.call_tool_unvalidated("get_result", {"document_id": mcp_doc, "locale": LOC})
        )
    )
    assert got["path"].endswith(".icml"), got
    with open(got["path"], "rb") as fh:
        _assert_icml_export("mcp", fh.read(), data)
    print("  [mcp] submit_translation / get_result round-trip .icml OK")


def main():
    test_registered_everywhere()
    test_identity_and_untouched_bytes()
    test_nested_styles_one_segment_and_growth()
    test_specials_footnote_table_hyperlink()
    test_hidden_condition_in_same_file_and_notes()
    test_target_language_stamped()
    test_wcml_multi_story_article()
    test_wcml_smart_connection_v3_root()
    test_unlocatable_story_falls_back_to_full_serialize()
    test_damaged_tokens_fuzz()
    test_malformed_and_hostile()
    test_every_surface_upload_translate_export()
    print("ICML FILTER OK")


if __name__ == "__main__":
    main()
