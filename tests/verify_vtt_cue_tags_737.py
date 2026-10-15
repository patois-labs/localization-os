"""WebVTT cue tags and Language header (#737, app/filters/srt_filter.py).

  units       every cue tag kind (c, i, b, u, v, lang, ruby, rt, karaoke timestamp,
              unknown) is a token; leading/trailing tags are hoisted out of the unit, so a
              voice name never reaches the engine.
  restore     build writes every tag back verbatim (classes, voice name, annotation).
  gate        a translation that drops / adds a token is reported by the tag-damage gate.
  balance     a stray or crossed close is dropped, an unclosed open closed at cue end.
  identity    no-op build and a build with the unit texts unchanged are byte-identical.
  language    the header `Language:` line is stamped with the target; none is added.
  entities    character references stay verbatim; a bare & / < / --> is escaped.
  legacy      a pre-#737 translation holding raw tags is written once, as before.
  srt         SRT cue text is unchanged (#719 / #725 HTML checks still judge it); #713.
  pipeline    API upload -> CLI translate (mock) -> API download keeps tags, stamps lang.

Hermetic + offline. Final line: "VTT CUE TAGS 737 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_vtt737_")

from app.filters import get_filter

VTT = (
    b"WEBVTT\n"
    b"Language: en\n"
    b"\n"
    b"00:00:01.000 --> 00:00:03.000\n"
    b"<v Roger>Welcome to the trail.</v>\n"
    b"\n"
    b"00:00:04.000 --> 00:00:06.000\n"
    b"<c.loud>Watch</c> your step on the <i>wet</i> rocks.\n"
    b"\n"
    b"00:00:07.000 --> 00:00:09.000\n"
    b"See you at <00:00:08.000>the top of the hill.\n"
    b"\n"
    b"00:00:10.000 --> 00:00:12.000\n"
    b"<v.first.loud Esme>It is getting dark.</v>\n"
    b"\n"
    b"00:00:13.000 --> 00:00:15.000\n"
    b"<ruby>Trail<rt>path</rt></ruby> markers are <b>orange</b>.\n"
    b"\n"
    b"00:00:16.000 --> 00:00:18.000\n"
    b"<lang fr>Bonjour</lang>, said <v Roger>Roger</v>, <u>loudly</u>.\n"
    b"\n"
    b"cue-7\n"
    b"00:00:19.000 --> 00:00:21.000\n"
    b"<v Esme>No close tag here\n"
    b"and a <i>second</i> line\n"
)

UNITS = [
    "Welcome to the trail.",
    "⟦g1⟧Watch⟦/g1⟧ your step on the ⟦g2⟧wet⟦/g2⟧ rocks.",
    "See you at ⟦x1⟧the top of the hill.",
    "It is getting dark.",
    "⟦g1⟧Trail⟦g2⟧path⟦/g2⟧⟦/g1⟧ markers are ⟦g3⟧orange⟦/g3⟧.",
    "⟦g1⟧Bonjour⟦/g1⟧, said ⟦g2⟧Roger⟦/g2⟧, ⟦g3⟧loudly⟦/g3⟧.",
    "No close tag here\nand a ⟦g1⟧second⟦/g1⟧ line",
]


def _f():
    return get_filter("vtt")


def _units(data: bytes) -> list[str]:
    return [u.text for u in _f().parse(data).units]


def test_units_tokenized():
    got = _units(VTT)
    assert got == UNITS, got
    joined = "\n".join(got)
    for raw in ("<", ">", "Roger>", "Esme", ".loud", "00:00:08"):
        assert raw not in joined, (raw, joined)
    print("  [units] c/i/b/u/v/lang/ruby/rt paired, timestamp point, edge tags hoisted OK")


def test_identity():
    f = _f()
    assert f.build(VTT, {}) == VTT
    same = {str(i): t for i, t in enumerate(UNITS)}
    assert f.build(VTT, same) == VTT
    assert f.build_localized(VTT, same, "es-ES") == VTT.replace(b"Language: en", b"Language: es-ES")
    print("  [identity] no-op build and unchanged units byte-identical OK")


def test_restore_verbatim():
    f = _f()
    tr = {str(i): "[ja] " + t for i, t in enumerate(UNITS)}
    out = f.build(VTT, tr).decode("utf-8")
    for tag in (
        "<v Roger>[ja] Welcome to the trail.</v>",
        "[ja] <c.loud>Watch</c> your step on the <i>wet</i> rocks.",
        "[ja] See you at <00:00:08.000>the top of the hill.",
        "<v.first.loud Esme>[ja] It is getting dark.</v>",
        "[ja] <ruby>Trail<rt>path</rt></ruby> markers are <b>orange</b>.",
        "[ja] <lang fr>Bonjour</lang>, said <v Roger>Roger</v>, <u>loudly</u>.",
        "<v Esme>[ja] No close tag here\nand a <i>second</i> line",
    ):
        assert tag in out, (tag, out)
    assert "⟦" not in out and "⟧" not in out, out
    # the rebuilt file parses back to the same tokens
    assert _units(out.encode("utf-8")) == ["[ja] " + t for t in UNITS]
    print("  [restore] every tag written back verbatim (classes, voice, annotation) OK")


def test_gate_sees_dropped_and_added_tags():
    from app.services import quality

    src = UNITS[2]
    # live ja-JP: the engine dropped the karaoke timestamp tag
    reason = quality.tag_damage(src, "丘の頂上で会いましょう。")
    assert "dropped" in reason and "⟦x1⟧" in reason, reason
    assert quality.tag_damage(src, "丘の⟦x1⟧頂上で会いましょう。") == ""
    reason = quality.tag_damage(UNITS[1], "⟦g1⟧Achtung⟦/g1⟧ auf nassen Steinen.")
    assert "dropped" in reason and "⟦g2⟧" in reason, reason
    reason = quality.tag_damage(UNITS[0], "⟦g1⟧Willkommen⟦/g1⟧ auf dem Weg.")
    assert "added" in reason, reason
    # a dropped tag is not written back from nowhere: the export shows what was kept
    out = _f().build(VTT, {"2": "丘の頂上で会いましょう。"}).decode("utf-8")
    assert "00:00:07.000 --> 00:00:09.000\n丘の頂上で会いましょう。\n" in out, out
    print("  [gate] dropped timestamp / span and an added tag reported by tag_damage OK")


def test_gate_stray_bracket_next_to_token():
    """Gate: #725's live th-TH WebVTT damage (`<b>...</b>>`) must still be held now that the
    cue tags are tokens (the #725 HTML check needs raw `<` tags in the source)."""
    from types import SimpleNamespace

    from app.models import SegStatus
    from app.services import export, quality

    src = "This line and this one ⟦g1⟧is bold⟦/g1⟧."
    for bad, ch in (
        ("บรรทัดนี้ ⟦g1⟧เป็นตัวหนา⟦/g1⟧>", "'>'"),
        ("บรรทัดนี้ ⟦g1⟧เป็นตัวหนา⟦/g1⟧&gt;", "'>'"),
        ("บรรทัดนี้ <⟦g1⟧เป็นตัวหนา⟦/g1⟧", "'<'"),
    ):
        reason = quality.placeholder_violation(src, bad)
        assert "stray" in reason and ch in reason, (bad, reason)
        held = SimpleNamespace(source_text=src, target_text=bad, status=SegStatus.NEEDS_REVIEW)
        assert export.segment_tag_damage(held), bad  # #711 rung 3: export writes the source
    for ok in (
        "บรรทัดนี้ ⟦g1⟧เป็นตัวหนา⟦/g1⟧",
        "a > b ⟦g1⟧x⟦/g1⟧ -> c",  # the translation's own brackets, away from a token
    ):
        assert quality.placeholder_violation(src, ok) == "", ok
    # the source already has the bracket there; untokenized sources are #725's business
    assert quality.placeholder_violation("⟦x1⟧> go", "⟦x1⟧> los") == ""
    from app.services.quality_part2 import token_stray_brackets

    assert token_stray_brackets("a > b", "a >> b") == ""
    print("  [gate] stray < / > against a token held (#725 shape, VTT) OK")


def test_gate_cue_end_smuggled_in_tag():
    """Gate: a translation `<b -->` / `<c.x-->` looked like a known tag to the escaper, so its
    `-->` reached the file and ended the cue."""
    src = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<i>run</i> now\n"
    for bad in ("⟦g1⟧cours⟦/g1⟧ <b --> x", "⟦g1⟧cours⟦/g1⟧ <c.x--> x"):
        out = _f().build(src, {"0": bad}).decode("utf-8")
        assert out.count("-->") == 1, out
        assert "--&gt;" in out, out
    print("  [gate] --> inside a tag-like translation text escaped OK")


def test_balance():
    f = _f()

    def cue(i, text):
        return f.build(VTT, {str(i): text}).decode("utf-8").split("\n\n")[i + 1].split("\n", 1)[1]

    # crossed close: dropped where it does not match, closed at the end
    got = cue(1, "⟦g1⟧A⟦g2⟧B⟦/g1⟧C⟦/g2⟧")
    assert got == "<c.loud>A<i>BC</i></c>", got
    # stray close, unknown marker and a duplicated point token are dropped
    got = cue(2, "a⟦/g9⟧b⟦r1⟧c⟦x1⟧d⟦x1⟧e")
    assert got == "abc<00:00:08.000>de", got
    # unclosed open closed at the cue end
    assert cue(4, "⟦g3⟧orange") == "<b>orange</b>"
    # #713: a blank line in the translation never splits the cue
    out = cue(1, "⟦g1⟧A⟦/g1⟧\n\n\nB ⟦g2⟧c⟦/g2⟧")
    assert out == "<c.loud>A</c>\nB <i>c</i>", out
    # </ruby> ends an open <rt> (the WebVTT parser rule): rt stays a point token
    src = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<ruby>Kan<rt>kan</ruby> ji\n"
    assert _units(src) == ["⟦g1⟧Kan⟦x2⟧kan⟦/g1⟧ ji"], _units(src)
    assert (
        _f().build(src, {"0": "⟦g1⟧Kan⟦x2⟧KAN⟦/g1⟧ JI"}).endswith(b"<ruby>Kan<rt>KAN</ruby> JI\n")
    )
    print("  [balance] crossed / stray / duplicate markers balanced; #713 blank lines OK")


def test_language_header():
    f = _f()
    out = f.build_localized(VTT, {}, "ja-JP")
    assert out.startswith(b"WEBVTT\nLanguage: ja-JP\n\n"), out[:40]
    assert f.build(VTT, {}).startswith(b"WEBVTT\nLanguage: en\n")
    # key is case-insensitive; spacing kept; other header lines untouched
    src = (
        b"WEBVTT - Trail\nKind: captions\nlanguage:  en-US \n\n00:00:01.000 --> 00:00:02.000\nHi.\n"
    )
    out = f.build_localized(src, {}, "ar-SA")
    assert out.startswith(b"WEBVTT - Trail\nKind: captions\nlanguage:  ar-SA \n\n"), out
    # no header: none is added; a Language: line in a NOTE or a cue is not a header
    src = (
        b"WEBVTT\n\nNOTE\nLanguage: en\n\n00:00:01.000 --> 00:00:02.000\nLanguage: en is spoken.\n"
    )
    out = f.build_localized(src, {}, "fr-FR")
    assert out == src, out
    assert f.build_localized(src, {"0": "On parle anglais."}, "fr-FR") == src.replace(
        b"Language: en is spoken.", b"On parle anglais."
    )
    # SRT has no header to stamp
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nLanguage: en\n"
    assert f.build_localized(srt, {}, "fr-FR") == srt
    print("  [language] Language: header stamped with the target; never added OK")


def test_entities():
    f = _f()
    src = (
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"
        b"Tom &amp; Jerry &lt;3 &gt; <i>&lrm;caf&eacute;&nbsp;now</i>\n"
    )
    assert _units(src) == ["Tom &amp; Jerry &lt;3 &gt; ⟦g1⟧&lrm;caf&eacute;&nbsp;now⟦/g1⟧"]
    assert f.build(src, {}) == src
    out = f.build(src, {"0": "Tom & Jerry <3 > ⟦g1⟧&lrm;café&nbsp;maintenant⟦/g1⟧ -->"})
    assert out.endswith(
        "Tom &amp; Jerry &lt;3 > <i>&lrm;café&nbsp;maintenant</i> --&gt;\n".encode()
    ), out
    out = f.build(src, {"0": "Tom &amp; Jerry &lt;3 ⟦g1⟧x⟦/g1⟧"})
    assert out.endswith(b"Tom &amp; Jerry &lt;3 <i>x</i>\n"), out
    print("  [entities] references verbatim, never double-escaped; bare & < --> escaped OK")


def test_legacy_translation():
    """A document imported before #737 stored raw tags in its units; its translations hold
    raw tags. They export as before: once, never wrapped in the hoisted tags again."""
    f = _f()
    legacy_units = [
        "<v Roger>Welcome to the trail.</v>",
        "<c.loud>Watch</c> your step on the <i>wet</i> rocks.",
    ]
    tr = {"0": "<v Roger>Willkommen.</v>", "1": "<c.loud>Achtung</c> auf <i>nassen</i> Steinen."}
    out = f.build(VTT, tr).decode("utf-8")
    assert "\n<v Roger>Willkommen.</v>\n" in out, out
    assert "\n<c.loud>Achtung</c> auf <i>nassen</i> Steinen.\n" in out, out
    # an untouched legacy unit (its stored text as the mapping) is the source cue
    out = f.build(VTT, {str(i): t for i, t in enumerate(legacy_units)})
    assert out == VTT, out
    print("  [legacy] pre-#737 raw-tag translations exported once, unchanged OK")


def test_srt_unchanged():
    from app.services.quality_part2 import html_tag_violation

    f = get_filter("srt")
    srt = (
        b"1\n00:00:01,000 --> 00:00:02,000\n<i>This line is in italics,</i>\n"
        b"and this one <b>is bold</b>.\n"
    )
    assert _units(srt) == ["<i>This line is in italics,</i>\nand this one <b>is bold</b>."]
    assert f.build(srt, {}) == srt
    out = f.build(srt, {"0": "<i>Kursiv,</i>\nund <b>fett</b> & <3"})
    assert out.endswith(b"<i>Kursiv,</i>\nund <b>fett</b> & <3\n"), out
    # #719 / #725 still judge SRT cue text
    src = _units(srt)[0]
    assert "stray" in html_tag_violation(src, "<i>Kursiv,</i>\nund <b>fett</b>>")
    assert "dropped" in html_tag_violation(src, "Kursiv,\nund <b>fett</b>.")
    print("  [srt] SRT cue text unchanged; #719 / #725 HTML checks still fire OK")


LOC = "fr-FR"


def test_pipeline_api_cli():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, Segment, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="vtt737", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user("vtt737@x.test", "vtt737", Role.ADMIN, "vtt737-pw-123456")
    _row, raw = apikeys.create_key(user.id, "vtt737", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("trail.vtt", VTT, "text/vtt")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    with Session(engine) as s:
        srcs = [
            x.source_text
            for x in s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == LOC)
                .order_by(Segment.order_index)
            ).all()
        ]
    joined = "\n".join(srcs)
    assert "⟦x1⟧" in joined and "⟦g1⟧" in joined, srcs
    assert "Roger>" not in joined and "<00:00:08.000>" not in joined, srcs
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text)
    text = r.content.decode("utf-8")
    assert text.startswith("WEBVTT\nLanguage: fr-FR\n\n"), text[:60]
    for tag in ("<v Roger>", "<c.loud>", "<00:00:08.000>", "<v.first.loud Esme>", "<lang fr>"):
        assert tag in text, (tag, text)
    assert "<ruby>" in text and "<rt>" in text and "</rt></ruby>" in text, text
    assert "⟦" not in text and "⟧" not in text, text
    assert f"[{LOC}]" in text, text
    print("  [pipeline] API upload, CLI mock translate, API download keep tags + stamp lang OK")


def main():
    test_units_tokenized()
    test_identity()
    test_restore_verbatim()
    test_gate_sees_dropped_and_added_tags()
    test_gate_stray_bracket_next_to_token()
    test_gate_cue_end_smuggled_in_tag()
    test_balance()
    test_language_header()
    test_entities()
    test_legacy_translation()
    test_srt_unchanged()
    test_pipeline_api_cli()
    print("VTT CUE TAGS 737 OK")


if __name__ == "__main__":
    main()
