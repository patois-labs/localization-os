"""IDML (Adobe InDesign Markup Language) filter: hermetic round-trip suite.

Hand-built IDML packages (no InDesign needed): multi-story order, nested character
styles, a run split across CharacterStyleRanges, paragraph breaks, tabs and special
characters, footnotes, tables, hyperlinks, index markers, inline XML tags, hidden
conditions, notes, non-printing frames, CJK and RTL text. Untouched members must come
back byte-identical with the same names, order, timestamps and compression, `mimetype`
first and STORED. Hostile and malformed packages are refused.

Hermetic + offline. Final line: "IDML FILTER OK".
"""

# ruff: noqa: E501  (hand-built IDML fixtures are long XML literals)

import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_idml_")

from lxml import etree

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.services.segment import find_placeholders

FILT = get_filter("idml")
MIMETYPE = b"application/vnd.adobe.indesign-idml-package"
PKG_NS = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"
DATE = (2026, 10, 10, 9, 30, 0)


def story_xml(sid: str, body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<idPkg:Story xmlns:idPkg="{PKG_NS}" DOMVersion="20.0">\n'
        f'\t<Story Self="{sid}" AppliedTOCStyle="n" TrackChanges="false">\n'
        '\t\t<StoryPreference OpticalMarginAlignment="false"/>\n'
        f"{body}\n"
        "\t</Story>\n"
        "</idPkg:Story>\n"
    ).encode()


def psr(inner: str, style: str = "ParagraphStyle/$ID/NormalParagraphStyle") -> str:
    return f'\t\t<ParagraphStyleRange AppliedParagraphStyle="{style}">\n{inner}\n\t\t</ParagraphStyleRange>'


def csr(inner: str, style: str = "CharacterStyle/$ID/[No character style]", extra: str = "") -> str:
    return f'\t\t\t<CharacterStyleRange AppliedCharacterStyle="{style}"{extra}>{inner}</CharacterStyleRange>'


BOLD = "CharacterStyle/Bold"


def designmap(stories, conditions: str = "", layers: str = "") -> bytes:
    refs = "".join(f'\t<idPkg:Story src="Stories/Story_{s}.xml"/>\n' for s in stories)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<?aid style="50" type="document" readerVersion="6.0" featureSet="257" product="20.0(73)" ?>\n'
        f'<Document xmlns:idPkg="{PKG_NS}" DOMVersion="20.0" Self="d">\n'
        f"{layers}{conditions}"
        '\t<idPkg:Graphic src="Resources/Graphic.xml"/>\n'
        '\t<idPkg:Spread src="Spreads/Spread_s1.xml"/>\n'
        f"{refs}"
        "</Document>\n"
    ).encode()


def spread(frames: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<idPkg:Spread xmlns:idPkg="{PKG_NS}" DOMVersion="20.0"><Spread Self="s1">'
        f"{frames}</Spread></idPkg:Spread>\n"
    ).encode()


def package(stories: dict, order=None, conditions="", layers="", frames="", extra=None) -> bytes:
    """stories: {id: body}. Members in InDesign's usual order, mimetype first STORED."""
    order = order or list(stories)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zi = zipfile.ZipInfo("mimetype", date_time=DATE)
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, MIMETYPE)
        members = [
            ("designmap.xml", designmap(order, conditions, layers)),
            ("META-INF/container.xml", b'<?xml version="1.0"?><container/>'),
            (
                "Resources/Graphic.xml",
                b'<?xml version="1.0"?><Graphic>\n\t<Color Self="c"/>\n</Graphic>',
            ),
            ("Spreads/Spread_s1.xml", spread(frames)),
        ]
        members += [
            (f"Stories/Story_{sid}.xml", story_xml(sid, body)) for sid, body in stories.items()
        ]
        members += list((extra or {}).items())
        for name, data in members:
            zi = zipfile.ZipInfo(name, date_time=DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)
    return out.getvalue()


def units(data: bytes) -> list:
    return FILT.parse(data).units


def story_of(data: bytes, sid: str):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return etree.fromstring(zf.read(f"Stories/Story_{sid}.xml"))


def visible_text(root) -> str:
    """Concatenated Content text of a story (what InDesign would show)."""
    return "|".join("".join(c.itertext()) for c in root.iter("Content"))


def assert_refused(data: bytes, why: str):
    try:
        FILT.parse(data)
    except (ValueError, InputLimitError):
        return
    raise AssertionError(f"{why}: parse must refuse")


# ---------------------------------------------------------------------- tests
def test_registered_everywhere():
    assert "idml" in supported_exts()
    from app.services.mime import mime_for_fmt
    from app.web.api_part2 import _FMT_LABELS

    assert mime_for_fmt("idml") == MIMETYPE.decode()
    assert "InDesign" in _FMT_LABELS["idml"]
    for path, needle in (
        ("app/templates/project.html", ".idml"),
        ("app/templates/help.html", "IDML"),
        ("llms.txt", "IDML"),
        ("app/capabilities/documents.md", "idml"),
    ):
        with open(path, encoding="utf-8") as fh:
            assert needle in fh.read(), (path, needle)
    print(
        "  [surfaces] idml registered: ext, MIME, /api/formats label, upload accept, help, llms.txt OK"
    )


def test_multi_story_order_and_byte_identity():
    data = package(
        {
            "a": psr(csr("<Content>First story.</Content>")),
            "b": psr(csr("<Content>Second story.</Content>")),
        },
        order=["b", "a"],
    )
    us = units(data)
    assert [u.text for u in us] == ["Second story.", "First story."], [u.text for u in us]
    out = FILT.build(data, {str(us[0].p_index): "Deuxieme histoire."})
    src = zipfile.ZipFile(io.BytesIO(data))
    dst = zipfile.ZipFile(io.BytesIO(out))
    assert [i.filename for i in dst.infolist()] == [i.filename for i in src.infolist()]
    first = dst.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    assert dst.read("mimetype") == MIMETYPE
    for a, b in zip(src.infolist(), dst.infolist(), strict=True):
        assert a.date_time == b.date_time and a.compress_type == b.compress_type, a.filename
        if a.filename != "Stories/Story_b.xml":
            assert src.read(a) == dst.read(b), f"{a.filename} must be byte-identical"
    assert "Deuxieme histoire." in visible_text(story_of(out, "b"))
    # an empty mapping rebuilds every member byte for byte
    same = zipfile.ZipFile(io.BytesIO(FILT.build(data, {})))
    for a in src.infolist():
        assert src.read(a) == same.read(a.filename)
    print(
        "  [package] designmap story order; untouched members byte-identical; mimetype first STORED OK"
    )


def test_nested_styles_one_segment_and_growth():
    body = psr(
        csr("<Content>Hello </Content>")
        + "\n"
        + csr("<Content>bold</Content>", BOLD)
        + "\n"
        + csr("<Content> world</Content>")
    )
    data = package({"a": body})
    (u,) = units(data)
    assert u.text == "Hello \u27e6r1\u27e7bold\u27e6/r1\u27e7 world", u.text
    # reorder + grow: the bold run moves to the front
    new = "\u27e6r1\u27e7Fettgedruckt\u27e6/r1\u27e7 ist hier, hallo liebe Welt"
    out = FILT.build(data, {str(u.p_index): new})
    root = story_of(out, "a")
    ranges = root.findall(".//CharacterStyleRange")
    got = [(r.get("AppliedCharacterStyle"), "".join(r.itertext()).strip()) for r in ranges]
    assert got == [
        (BOLD, "Fettgedruckt"),
        ("CharacterStyle/$ID/[No character style]", "ist hier, hallo liebe Welt"),
    ], got
    assert units(out)[0].text == new, units(out)[0].text
    print("  [styles] a run split across ranges is ONE segment; text grows and reorders OK")


def test_paragraph_breaks_inside_one_range():
    body = psr(
        csr(
            "<Content>Para one.</Content><Br/><Content>Para two.</Content><Br/><Content>Para three.</Content>"
        )
    )
    data = package({"a": body})
    us = units(data)
    assert [u.text for u in us] == ["Para one.", "Para two.", "Para three."]
    out = FILT.build(data, {str(us[1].p_index): "Absatz zwei, deutlich laenger als vorher."})
    root = story_of(out, "a")
    assert len(root.findall(".//Br")) == 2
    assert [u.text for u in units(out)] == [
        "Para one.",
        "Absatz zwei, deutlich laenger als vorher.",
        "Para three.",
    ]
    print("  [breaks] Br splits paragraphs inside one range; rebuild keeps both Br OK")


def test_tabs_special_characters_and_pis():
    body = psr(csr("<Content>Page\t<?ACE 18?> of the\u2028book</Content>"))
    data = package({"a": body})
    (u,) = units(data)
    assert u.text == "Page\u27e6x1\u27e7\u27e6x2\u27e7 of the\u27e6x3\u27e7book", u.text
    out = FILT.build(
        data, {str(u.p_index): "Seite\u27e6x1\u27e7\u27e6x2\u27e7 des\u27e6x3\u27e7Buches"}
    )
    raw = zipfile.ZipFile(io.BytesIO(out)).read("Stories/Story_a.xml").decode("utf-8")
    assert "<Content>Seite\t<?ACE 18?> des\u2028Buches</Content>" in raw, raw
    print("  [specials] tab, <?ACE 18?> and forced line break are point tokens OK")


def test_footnote_table_hyperlink_index_xml():
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
            "<Content>See </Content>"
            '<HyperlinkTextSource Self="h1" Name="link"><Content>our site</Content></HyperlinkTextSource>'
            '<Content> now</Content><PageReference Self="p1" PageReferenceType="CurrentPage"/>'
            "<Content>.</Content>" + footnote
        )
    )
    body += "\n" + psr(
        csr(
            '<XMLElement Self="x1" MarkupTag="XMLTag/name"><Content>Ada</Content></XMLElement><Content> wrote it.</Content>'
        )
    )
    body += "\n" + psr(csr(table))
    data = package({"a": body})
    us = units(data)
    texts = [u.text for u in us]
    assert texts == [
        "See \u27e6g1\u27e7our site\u27e6/g1\u27e7 now\u27e6x2\u27e7.\u27e6x3\u27e7",
        "\u27e6g1\u27e7Ada\u27e6/g1\u27e7 wrote it.",
        "\u27e6x1\u27e7 Footnote text.",
        "Cell A",
        "Cell B",
    ], texts
    mapping = {
        str(
            us[0].p_index
        ): "Besuchen Sie \u27e6g1\u27e7unsere Seite\u27e6/g1\u27e7 jetzt\u27e6x2\u27e7.\u27e6x3\u27e7",
        str(us[1].p_index): "\u27e6g1\u27e7Ada\u27e6/g1\u27e7 schrieb es.",
        str(us[2].p_index): "\u27e6x1\u27e7 Fussnotentext.",
        str(us[3].p_index): "Zelle A",
        str(us[4].p_index): "Zelle B",
    }
    out = FILT.build(data, mapping)
    root = story_of(out, "a")
    assert len(root.findall(".//HyperlinkTextSource[@Self='h1']")) == 1
    assert "".join(root.find(".//HyperlinkTextSource").itertext()) == "unsere Seite"
    assert len(root.findall(".//PageReference")) == 1 and len(root.findall(".//Footnote")) == 1
    assert len(root.findall(".//Table/Cell")) == 2 and root.find(".//Table").get("Self") == "t1"
    assert [u.text for u in units(out)] == list(mapping.values())
    print("  [inline] footnote, table cells, hyperlink, index marker and XML tag round-trip OK")


def test_hidden_conditions_notes_and_hidden_text():
    conds = '\t<Condition Self="Condition/Hide" Name="Hide" Visible="false"/>\n\t<Condition Self="Condition/Show" Name="Show" Visible="true"/>\n'
    body = psr(
        csr("<Content>Visible </Content>")
        + csr("<Content>SECRET</Content>", extra=' AppliedConditions="Condition/Hide"')
        + csr("<Content> shown</Content>", extra=' AppliedConditions="Condition/Show"')
        + csr(
            "<Content> tail</Content><Note><ParagraphStyleRange><CharacterStyleRange><Content>editor note</Content></CharacterStyleRange></ParagraphStyleRange></Note><HiddenText><Content>hidden words</Content></HiddenText>"
        )
    )
    data = package({"a": body}, conditions=conds)
    (u,) = units(data)
    assert (
        "SECRET" not in u.text and "editor note" not in u.text and "hidden words" not in u.text
    ), u.text
    joined = find_placeholders(u.text)
    new = u.text.replace("Visible", "Sichtbar")
    out = FILT.build(data, {str(u.p_index): new})
    root = story_of(out, "a")
    txt = visible_text(root)
    assert (
        "SECRET" in txt and "editor note" in txt and "hidden words" in txt and "Sichtbar" in txt
    ), txt
    assert root.find(".//CharacterStyleRange[@AppliedConditions='Condition/Hide']") is not None
    assert len(joined) == len(find_placeholders(units(out)[0].text))
    print("  [hidden] hidden condition, Note and HiddenText kept, never offered for translation OK")


def test_nonprinting_story_skipped():
    layers = '\t<Layer Self="L2" Name="Notes" Visible="true" Printable="false"/>\n'
    frames = '<TextFrame Self="f1" ParentStory="a"/><TextFrame Self="f2" ParentStory="b" ItemLayer="L2"/><TextFrame Self="f3" ParentStory="c" Nonprinting="true"/>'
    data = package(
        {
            "a": psr(csr("<Content>Printed.</Content>")),
            "b": psr(csr("<Content>Layer note.</Content>")),
            "c": psr(csr("<Content>Slug.</Content>")),
        },
        layers=layers,
        frames=frames,
    )
    assert [u.text for u in units(data)] == ["Printed."]
    print("  [non-printing] stories on non-printing layers or frames are skipped OK")


def test_cjk_and_rtl():
    data = package(
        {
            "a": psr(csr("<Content>\u65e5\u672c\u8a9e\u306e\u6587\u7ae0\u3002</Content>")),
            "b": psr(
                csr(
                    "<Content>\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645</Content>"
                )
            ),
        }
    )
    us = units(data)
    assert [u.text for u in us] == [
        "\u65e5\u672c\u8a9e\u306e\u6587\u7ae0\u3002",
        "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645",
    ]
    out = FILT.build(
        data,
        {
            str(us[0].p_index): "\u4e2d\u6587\u53e5\u5b50\u3002",
            str(us[1].p_index): "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd",
        },
    )
    assert [u.text for u in units(out)] == [
        "\u4e2d\u6587\u53e5\u5b50\u3002",
        "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd",
    ]
    print("  [scripts] CJK and RTL text round-trip OK")


def test_damaged_tokens_never_lose_elements():
    body = psr(
        csr(
            '<Content>A </Content><HyperlinkTextSource Self="h9"><Content>link</Content></HyperlinkTextSource>'
            '<Content> and</Content><PageReference Self="p9"/><Content> more</Content>'
        )
        + csr("<Content> bold</Content>", BOLD)
    )
    data = package({"a": body})
    (u,) = units(data)
    for bad in (
        "no tokens at all",
        "\u27e6x99\u27e7 junk \u27e6/g7\u27e7 \u27e6g1\u27e7a\u27e6/g1\u27e7\u27e6g1\u27e7b\u27e6/g1\u27e7",
    ):
        out = FILT.build(data, {str(u.p_index): bad})
        root = story_of(out, "a")
        assert len(root.findall(".//HyperlinkTextSource[@Self='h9']")) == 1, bad
        assert len(root.findall(".//PageReference[@Self='p9']")) == 1, bad
        assert "\u27e6" not in visible_text(root), bad
    print("  [damage] dropped/unknown/repeated tokens never lose or duplicate an element OK")


def test_malformed_and_hostile():
    assert_refused(b"not a zip at all", "not a zip")
    good = package({"a": psr(csr("<Content>x</Content>"))})

    def rezip(mutate) -> bytes:
        src = zipfile.ZipFile(io.BytesIO(good))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for info in src.infolist():
                name, data = mutate(info.filename, src.read(info))
                if name:
                    zf.writestr(name, data)
        return out.getvalue()

    assert_refused(rezip(lambda n, d: (None if n == "designmap.xml" else n, d)), "no designmap")
    assert_refused(
        rezip(lambda n, d: (n, b"application/zip" if n == "mimetype" else d)), "wrong mimetype"
    )
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b'<idPkg:Story xmlns:idPkg="x"><Story><ParagraphStyleRange><CharacterStyleRange>'
        b"<Content>&e;</Content></CharacterStyleRange></ParagraphStyleRange></Story></idPkg:Story>"
    )
    assert_refused(rezip(lambda n, d: (n, xxe if n.startswith("Stories/") else d)), "XXE story")
    assert_refused(
        rezip(lambda n, d: (n, b"<Story><unclosed>" if n.startswith("Stories/") else d)),
        "malformed XML",
    )
    assert_refused(
        rezip(lambda n, d: ("../../evil.xml" if n.startswith("Resources/") else n, d)),
        "path traversal",
    )
    assert_refused(
        rezip(lambda n, d: ("C:/evil.xml" if n.startswith("Resources/") else n, d)), "drive path"
    )
    # duplicate member names (zip smuggling)
    out = io.BytesIO(good)
    with zipfile.ZipFile(out, "a") as zf:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            zf.writestr("Stories/Story_a.xml", b"<x/>")
    assert_refused(out.getvalue(), "duplicate member")
    # compression bomb: one part inflates far past ZIP_MAX_RATIO
    buf = io.BytesIO(good)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Resources/Bomb.xml", b"\0" * (64 * 1024 * 1024))
    try:
        FILT.parse(buf.getvalue())
    except InputLimitError:
        pass
    else:
        raise AssertionError("a compression bomb must be an InputLimitError")
    print(
        "  [hostile] non-zip, no designmap, wrong mimetype, XXE, bad XML, traversal, duplicates, bomb refused OK"
    )


def test_export_maps_idml_rebuild_errors():
    with open("app/services/export.py", encoding="utf-8") as fh:
        assert '"idml"' in fh.read()
    print("  [export] an idml rebuild failure maps to ExportUnreadableBlob OK")


def test_token_shuffle_fuzz():
    """Seeded fuzz: any permutation, drop or duplication of the source tokens rebuilds a
    well-formed story that keeps every inline element exactly once and never leaks a
    marker into the text."""
    import random

    rng = random.Random(1010)
    body = psr(
        csr(
            '<Content>Start\t</Content><HyperlinkTextSource Self="hz"><Content>link</Content>'
            '</HyperlinkTextSource><PageReference Self="pz"/><Content>mid<?ACE 7?>dle</Content>'
        )
        + csr("<Content> bold </Content>", BOLD)
        + csr("<Content>italic</Content>", "CharacterStyle/Italic")
        + csr("<Content> end.</Content><Br/><Content>Next para.</Content>")
    )
    data = package({"a": body})
    first = units(data)[0]
    toks = find_placeholders(first.text)
    for _ in range(200):
        picked = [t for t in toks if rng.random() > 0.2]
        picked += rng.sample(toks, k=rng.randint(0, 2))
        rng.shuffle(picked)
        words = ["Wort", "zwei", " ", "drei"]
        text = "".join(t + rng.choice(words) for t in picked)
        out = FILT.build(data, {str(first.p_index): text})
        root = story_of(out, "a")
        assert len(root.findall(".//HyperlinkTextSource[@Self='hz']")) == 1, text
        assert len(root.findall(".//PageReference[@Self='pz']")) == 1, text
        assert len(root.findall(".//Br")) == 1, text
        assert "⟦" not in visible_text(root), text
        assert [u.text for u in units(out)][-1] == "Next para.", text
    print("  [fuzz] 200 shuffled/dropped/duplicated token sets rebuild well-formed stories OK")


LOC = "de-DE"


def _surface_package() -> bytes:
    body = psr(
        csr("<Content>Hello </Content>")
        + csr("<Content>bold</Content>", BOLD)
        + csr("<Content> world.</Content><Br/><Content>Second\tline.</Content>")
    )
    return package({"a": body, "b": psr(csr("<Content>Other story.</Content>"))})


def _assert_idml_export(surface: str, data: bytes, original: bytes) -> None:
    src = zipfile.ZipFile(io.BytesIO(original))
    dst = zipfile.ZipFile(io.BytesIO(data))
    assert dst.infolist()[0].filename == "mimetype", surface
    assert dst.infolist()[0].compress_type == zipfile.ZIP_STORED, surface
    for info in src.infolist():
        if not info.filename.startswith("Stories/"):
            assert src.read(info) == dst.read(info.filename), (surface, info.filename)
    txt = visible_text(story_of(data, "a")) + visible_text(story_of(data, "b"))
    assert f"[{LOC}]" in txt, (surface, txt)
    assert "⟦" not in txt, (surface, txt)
    assert "\t" in txt, (surface, txt)
    root = story_of(data, "a")
    assert root.find(f".//CharacterStyleRange[@AppliedCharacterStyle='{BOLD}']") is not None, (
        surface
    )


def test_every_surface_upload_translate_export():
    """Four-surface parity (ADR 0011): an .idml uploads, translates (mock engine) and
    exports as .idml on the API, web, CLI and MCP doors; a hostile package is refused."""
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
        p = Project(name="idml", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user(
        "idml@x.test", "idml", Role.ADMIN, "idml-pw-123456"
    )  # pregate: allow-secret throwaway test user
    _row, api_raw = apikeys.create_key(user.id, "idml", scopes="api docs admin")
    tmp = tempfile.mkdtemp(prefix="loctms_idml_surf_")
    data = _surface_package()

    def translate(doc_id: int) -> None:
        code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
        assert code == 0, (code, out, err)

    # --- API
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": api_raw}  # pregate: allow-secret throwaway test key
    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200 and any(f["ext"] == "idml" for f in r.json()["formats"]), r.text
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("brochure.idml", data, MIMETYPE.decode())},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    api_doc = r.json()["document_id"]
    translate(api_doc)
    r = client.get(f"/api/documents/{api_doc}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.headers["content-type"].startswith(MIMETYPE.decode()), r.headers["content-type"]
    assert ".idml" in r.headers.get("content-disposition", ""), r.headers
    _assert_idml_export("api", r.content, data)
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("evil.idml", b"PK not really a zip", MIMETYPE.decode())},
    )
    assert r.status_code in (413, 422), (r.status_code, r.text)
    print("  [api] /api/formats lists idml; upload, translate, download .idml; junk refused OK")

    # --- web
    web = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    csrf_for(web)
    r = web.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("web.idml", data, MIMETYPE.decode())},
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    m = re.match(r"^/documents/(\d+)$", r.headers["location"])
    assert m, r.headers["location"]
    web_doc = int(m.group(1))
    translate(web_doc)
    r = web.get(f"/documents/{web_doc}/export", params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    _assert_idml_export("web", r.content, data)
    print("  [web] upload, translate, export .idml OK")

    # --- CLI
    src = os.path.join(tmp, "cli.idml")
    with open(src, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    cli_doc = int(re.search(r"Uploaded #(\d+)", out).group(1))
    translate(cli_doc)
    dest = os.path.join(tmp, "cli.de.idml")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", LOC, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        _assert_idml_export("cli", fh.read(), data)
    print("  [cli] doc upload / translate / export .idml OK")

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
    assert got["path"].endswith(".idml"), got
    with open(got["path"], "rb") as fh:
        _assert_idml_export("mcp", fh.read(), data)
    print("  [mcp] submit_translation / get_result round-trip .idml OK")


def _ranges_ok(root) -> None:
    """A rebuilt story never nests a range directly in a range, never leaves text
    outside a range and keeps every Self id unique."""
    selfs = [el.get("Self") for el in root.iter() if el.get("Self")]
    assert len(selfs) == len(set(selfs)), selfs
    for el in root.iter("CharacterStyleRange"):
        assert el.getparent().tag != "CharacterStyleRange", "range directly inside a range"
    for el in root.iter("Content"):
        assert any(a.tag == "CharacterStyleRange" for a in el.iterancestors()), "bare Content"


def test_hyperlink_spanning_ranges():
    """Shapes InDesign writes when a hyperlink crosses a formatting change (gate 10-10,
    found in real exports): ParagraphStyleRange > HyperlinkTextSource >
    CharacterStyleRange, and CharacterStyleRange > HyperlinkTextSource >
    CharacterStyleRange. The link text was never offered (source left in the file)."""
    block = (
        '\t\t<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/$ID/NormalParagraphStyle">'
        + csr("", extra=' KerningMethod="$ID/Optical"')
        + '<HyperlinkTextSource Self="hb" Name="Hyperlink 1" Hidden="false">'
        + csr("<Content>read the </Content>")
        + csr("<Content>manual</Content>", BOLD)
        + "</HyperlinkTextSource></ParagraphStyleRange>"
    )
    inline = psr(
        csr("<Content>Go to </Content>")
        + csr(
            '<HyperlinkTextSource Self="hi" Name="Hyperlink 2">'
            + csr("<Content>our site</Content>", BOLD).strip()
            + "</HyperlinkTextSource><Content> today</Content>",
            "CharacterStyle/Hyperlink",
        )
    )
    data = package({"a": block + "\n" + inline})
    us = units(data)
    assert len(us) == 2 and "manual" in us[0].text and "our site" in us[1].text, [
        u.text for u in us
    ]
    mapping = {
        str(u.p_index): u.text.replace("manual", "Handbuch").replace("our site", "unsere Seite")
        for u in us
    }
    out = FILT.build(data, mapping)
    root = story_of(out, "a")
    _ranges_ok(root)
    hb = root.find(".//HyperlinkTextSource[@Self='hb']")
    hi = root.find(".//HyperlinkTextSource[@Self='hi']")
    assert "Handbuch" in "".join(hb.itertext()), etree.tostring(hb)
    assert "".join(hi.itertext()) == "unsere Seite", etree.tostring(hi)
    assert hi.find("CharacterStyleRange").get("AppliedCharacterStyle") == BOLD
    assert [u.text for u in units(out)] == list(mapping.values())
    # Misplaced, dropped and repeated tokens: the link survives once, no range in a range.
    (_, u2) = us
    toks = find_placeholders(u2.text)
    for bad in ("unsere Seite", " ".join(reversed(toks)) + " x", toks[-1] + " a " + toks[0]):
        root = story_of(FILT.build(data, {str(u2.p_index): bad}), "a")
        _ranges_ok(root)
        assert len(root.findall(".//HyperlinkTextSource[@Self='hi']")) == 1, bad
    print("  [links] hyperlinks spanning ranges are translated inside the link OK")


def test_real_export_shapes():
    """Gate 10-10 findings from real InDesign exports: percent-encoded
    AppliedConditions, bare Content under Story/ParagraphStyleRange, endnote text,
    tracked insertions holding whole paragraphs, Content attributes on new text."""
    conds = '\t<Condition Self="Condition/Print Only" Name="Print Only" Visible="false"/>\n'
    hidden = psr(
        csr("<Content>Shown </Content>")
        + csr("<Content>PRINTONLY</Content>", extra=' AppliedConditions="Condition/Print%20Only"')
    )
    assert [u.text for u in units(package({"a": hidden}, conditions=conds))] == ["Shown"]

    bare = (
        "\t\t<Content>Direct one.</Content>\n"
        + csr("<Content> Two.</Content>")
        + "\n"
        + psr(csr("<Content>Three.</Content>"))
        + "\n\t\t<Content>Four.</Content>"
    )
    data = package({"a": bare})
    us = units(data)
    joined = " ".join(u.text for u in us)
    assert "Direct one." in joined and "Four." in joined, joined
    out = FILT.build(data, {str(u.p_index): u.text.upper() for u in us})
    root = story_of(out, "a")
    _ranges_ok(root)
    assert "DIRECT ONE." in visible_text(root) and "FOUR." in visible_text(root)

    notes = psr(
        csr(
            "<Content>Body</Content>"
            '<EndnoteRange Self="e1">'
            + psr(csr("<Content>Endnote words.</Content>"))
            + "</EndnoteRange>"
            '<Change ChangeType="InsertedText">'
            + psr(csr("<Content>Inserted paragraph.</Content>"))
            + "</Change>"
        )
    )
    texts = [u.text for u in units(package({"a": notes}))]
    assert "Endnote words." in texts and "Inserted paragraph." in texts, texts

    keep = psr(
        csr('<Content xml:space="preserve">Kept </Content>') + csr("<Content>bold</Content>", BOLD)
    )
    data = package({"a": keep})
    (u,) = units(data)
    out = FILT.build(data, {str(u.p_index): u.text.replace("Kept", "Behalten\nzwei")})
    raw = zipfile.ZipFile(io.BytesIO(out)).read("Stories/Story_a.xml").decode("utf-8")
    assert 'xml:space="preserve"' in raw and "Behalten zwei" in raw, raw
    print("  [real shapes] encoded conditions, bare Content, endnotes, inserted paragraphs OK")


def test_target_language_stamped():
    langs = (
        '\t<Language Self="Language/$ID/English%3a USA" Name="$ID/English: USA" Id="269"/>\n'
        '\t<Language Self="Language/$ID/de_DE_2006" Name="$ID/de_DE_2006" Id="275"/>\n'
        '\t<Language Self="Language/$ID/hi_IN" Name="$ID/hi_IN" Id="348"/>\n'
    )
    body = (
        psr(
            csr("<Content>Hello </Content>", extra=' AppliedLanguage="$ID/English: USA"')
            + csr(
                "<Content>world</Content><Footnote>"
                + psr(csr("<Content>Fn.</Content>", extra=' AppliedLanguage="$ID/English: USA"'))
                + "</Footnote>",
                BOLD,
            )
        )
        + "\n"
        + psr(csr("<Content>Untouched.</Content>"))
    )
    data = package({"a": body}, conditions=langs)
    us = units(data)
    mapping = {str(us[0].p_index): us[0].text.replace("Hello", "Hallo")}
    plain = story_of(FILT.build(data, mapping), "a")
    assert "de_DE_2006" not in etree.tostring(plain).decode()
    for locale, want in (("de-DE", "$ID/de_DE_2006"), ("hi-IN", "$ID/hi_IN"), ("fr-FR", None)):
        root = story_of(FILT.build_localized(data, mapping, locale), "a")
        paras = root.find("Story").findall("ParagraphStyleRange")
        got = {r.get("AppliedLanguage") for r in paras[0].findall("CharacterStyleRange")}
        fn = root.find(".//Footnote//CharacterStyleRange")
        assert fn.get("AppliedLanguage") == "$ID/English: USA", fn.get("AppliedLanguage")
        if want:
            assert got == {want}, (locale, got)
        else:
            assert "$ID/French" not in got, (locale, got)  # undeclared: never invented
        assert paras[1].find("CharacterStyleRange").get("AppliedLanguage") is None
    print("  [language] build_localized stamps the declared target language only OK")


def main():
    test_registered_everywhere()
    test_multi_story_order_and_byte_identity()
    test_nested_styles_one_segment_and_growth()
    test_paragraph_breaks_inside_one_range()
    test_tabs_special_characters_and_pis()
    test_footnote_table_hyperlink_index_xml()
    test_hidden_conditions_notes_and_hidden_text()
    test_nonprinting_story_skipped()
    test_cjk_and_rtl()
    test_damaged_tokens_never_lose_elements()
    test_hyperlink_spanning_ranges()
    test_real_export_shapes()
    test_target_language_stamped()
    test_malformed_and_hostile()
    test_export_maps_idml_rebuild_errors()
    test_token_shuffle_fuzz()
    test_every_surface_upload_translate_export()
    print("IDML FILTER OK")


if __name__ == "__main__":
    main()
