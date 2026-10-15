"""SRT ASS/SSA override blocks are protected tokens (#738, app/filters/srt_filter.py).

  units       every `{\\...}` override block (an8, i1/i0, b1, colour, several in a row) is a
              point token; leading/trailing blocks are kept out of the unit.
  plain       `{...}` without a backslash is text; SRT `<i>` / `<font>` stay unit text.
  restore     build writes every block back verbatim; the rebuilt file re-parses the same.
  gate        the live ja-JP drop of `{\\i0}` and an added token are reported by tag_damage.
  markers     an unknown / repeated marker is dropped; #713 blank lines with tokens.
  identity    no-op build and a build with the unit texts unchanged are byte-identical.
  legacy      a pre-#738 translation holding raw blocks is written once, as before; an
              untouched legacy unit exports as the source cue.
  html        #719 / #725 HTML tag checks still fire on SRT cue text.
  vtt         a WebVTT cue keeps `{\\...}` as text (#737 behaviour unchanged).
  pipeline    API upload -> CLI translate (mock) -> API download keeps every block.

Hermetic + offline. Final line: "SRT OVERRIDES 738 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_srt738_")

from app.filters import get_filter

# The round-11 soak repro (soak/triage/r11/subs/pos.srt) plus the other override kinds.
SRT = (
    b"1\n00:00:01,000 --> 00:00:03,000\n{\\an8}Welcome to the trail.\n"
    b"\n"
    b"2\n00:00:04,000 --> 00:00:06,000\n{\\i1}Watch{\\i0} your step on the rocks.\n"
    b"\n"
    b"3\n00:00:07,000 --> 00:00:09,000\n{\\an8}{\\c&H00FFFF&}See you at the top.\n"
    b"\n"
    b"4\n00:00:10,000 --> 00:00:12,000\nThe {\\b1}orange{\\b0} markers {\\i1}point up{\\i0}.\n"
    b"\n"
    b"5\n00:00:13,000 --> 00:00:15,000\nKeep {left} and {\\fs20}{\\c&HFF&}stay{\\r} close\n"
    b"{\\i1}on the ridge.{\\i0}\n"
    b"\n"
    b"6\n00:00:16,000 --> 00:00:18,000\n<i>No override here,</i> {just braces}.\n"
)

UNITS = [
    "Welcome to the trail.",
    # gate: an edge style toggle whose counterpart is inside the unit stays in the unit
    "⟦x1⟧Watch⟦x2⟧ your step on the rocks.",
    "See you at the top.",
    "The ⟦x1⟧orange⟦x2⟧ markers ⟦x3⟧point up⟦x4⟧.",
    "Keep {left} and ⟦x1⟧⟦x2⟧stay⟦x3⟧ close\n⟦x4⟧on the ridge.⟦x5⟧",
    "<i>No override here,</i> {just braces}.",
]


def _f():
    return get_filter("srt")


def _units(data: bytes) -> list[str]:
    return [u.text for u in _f().parse(data).units]


def _cue(out: bytes, i: int) -> str:
    return out.decode("utf-8").split("\n\n")[i].split("\n", 2)[2].rstrip("\n")


def test_units_tokenized():
    got = _units(SRT)
    assert got == UNITS, got
    joined = "\n".join(got)
    for raw in ("{\\", "an8", "i1}", "&H00FFFF&", "fs20"):
        assert raw not in joined, (raw, joined)
    # a cue of blocks only keeps them in the unit (unit membership unchanged)
    only = b"1\n00:00:01,000 --> 00:00:02,000\n{\\an8}\n"
    assert _units(only) == ["⟦x1⟧"], _units(only)
    assert _f().build(only, {"0": "⟦x1⟧"}) == only
    print("  [units] an8 / i1 i0 / b1 / colour / fs / r are point tokens, edges hoisted OK")


def test_plain_braces_and_html_untouched():
    src = b'1\n00:00:01,000 --> 00:00:02,000\n{y:i}Hi {there} <font color="red">you</font>\n'
    assert _units(src) == ['{y:i}Hi {there} <font color="red">you</font>'], _units(src)
    out = _f().build(src, {"0": '{y:i}Salut {toi} <font color="red">vous</font>'})
    assert out.endswith(b'{y:i}Salut {toi} <font color="red">vous</font>\n'), out
    print("  [plain] braces without a backslash and SRT HTML tags stay text OK")


def test_identity():
    f = _f()
    assert f.build(SRT, {}) == SRT
    same = {str(i): t for i, t in enumerate(UNITS)}
    assert f.build(SRT, same) == SRT
    assert f.build_localized(SRT, same, "ja-JP") == SRT
    print("  [identity] no-op build and unchanged units byte-identical OK")


def test_restore_verbatim():
    f = _f()
    tr = {str(i): "[ja] " + t for i, t in enumerate(UNITS)}
    out = f.build(SRT, tr)
    want = [
        "{\\an8}[ja] Welcome to the trail.",
        "[ja] {\\i1}Watch{\\i0} your step on the rocks.",
        "{\\an8}{\\c&H00FFFF&}[ja] See you at the top.",
        "[ja] The {\\b1}orange{\\b0} markers {\\i1}point up{\\i0}.",
        "[ja] Keep {left} and {\\fs20}{\\c&HFF&}stay{\\r} close\n{\\i1}on the ridge.{\\i0}",
        "[ja] <i>No override here,</i> {just braces}.",
    ]
    for i, w in enumerate(want):
        assert _cue(out, i) == w, (i, _cue(out, i), w)
    assert "⟦" not in out.decode("utf-8") and "⟧" not in out.decode("utf-8"), out
    assert _units(out) == ["[ja] " + t for t in UNITS], _units(out)
    print("  [restore] every override block written back verbatim OK")


def test_gate_sees_dropped_and_added():
    from app.services import quality

    # live ja-JP (round 11): the engine dropped {\i1} and {\i0}
    reason = quality.tag_damage(UNITS[1], "岩場では足元に注意してください。")
    assert "dropped" in reason and "⟦x1⟧" in reason and "⟦x2⟧" in reason, reason
    assert quality.tag_damage(UNITS[1], "⟦x1⟧岩場⟦x2⟧では足元に注意してください。") == ""
    reason = quality.tag_damage(UNITS[3], "Die ⟦x1⟧orangen⟦x2⟧ Markierungen zeigen nach oben")
    assert "dropped" in reason and "⟦x3⟧" in reason, reason
    reason = quality.tag_damage(UNITS[0], "⟦x1⟧Willkommen auf dem Weg.")
    assert "added" in reason, reason
    # gate: the dropped toggles are not invented; before, the hoisted {\i1} came back alone
    # and the whole cue turned italic
    out = _f().build(SRT, {"1": "岩場では足元に注意してください。"})
    assert _cue(out, 1) == "岩場では足元に注意してください。", _cue(out, 1)
    # a non-toggle edge block (position, colour) is still hoisted with toggles inside
    edge = b"1\n00:00:01,000 --> 00:00:02,000\n{\\an8}{\\i1}Watch{\\i0} out{\\i1}\n"
    assert _units(edge) == ["⟦x1⟧Watch⟦x2⟧ out⟦x3⟧"], _units(edge)
    assert _cue(_f().build(edge, {"0": "⟦x1⟧Gib⟦x2⟧ acht⟦x3⟧"}), 0) == (
        "{\\an8}{\\i1}Gib{\\i0} acht{\\i1}"
    )
    print("  [gate] dropped {\\i0} and an added token reported by tag_damage OK")


def test_markers():
    f = _f()
    # unknown, paired and repeated markers are dropped
    out = f.build(SRT, {"3": "a⟦g1⟧b⟦/g1⟧c⟦x9⟧d⟦x1⟧e⟦x1⟧f⟦x2⟧g⟦x3⟧h"})
    assert _cue(out, 3) == "abcd{\\b1}ef{\\b0}g{\\i1}h", _cue(out, 3)
    # #713: a blank line never splits the cue; tokens survive
    out = f.build(SRT, {"4": "Bleib ⟦x1⟧⟦x2⟧nah⟦x3⟧\n\n\n⟦x4⟧am Grat.⟦x5⟧"})
    assert _cue(out, 4) == "Bleib {\\fs20}{\\c&HFF&}nah{\\r}\n{\\i1}am Grat.{\\i0}", _cue(out, 4)
    print("  [markers] unknown / repeated markers dropped; #713 blank lines OK")


def test_legacy_translation():
    """A document imported before #738 stored raw blocks in its units; its translations
    hold raw blocks. They export as before: once, never wrapped in the hoisted blocks."""
    f = _f()
    legacy_units = [
        "{\\an8}Welcome to the trail.",
        "{\\i1}Watch{\\i0} your step on the rocks.",
        "{\\an8}{\\c&H00FFFF&}See you at the top.",
    ]
    tr = {
        "0": "{\\an8}Willkommen auf dem Weg.",
        "1": "{\\i1}Achtung{\\i0} auf den Felsen.",
        "2": "{\\an8}{\\c&H00FFFF&}Wir sehen uns oben.",
    }
    out = f.build(SRT, tr)
    assert _cue(out, 0) == "{\\an8}Willkommen auf dem Weg.", _cue(out, 0)
    assert _cue(out, 1) == "{\\i1}Achtung{\\i0} auf den Felsen.", _cue(out, 1)
    assert _cue(out, 2) == "{\\an8}{\\c&H00FFFF&}Wir sehen uns oben.", _cue(out, 2)
    # an untouched legacy unit (its stored text as the mapping) is the source cue
    out = f.build(SRT, {str(i): t for i, t in enumerate(legacy_units)})
    assert out == SRT, out
    print("  [legacy] pre-#738 raw-block translations exported once, unchanged OK")


def test_html_checks_still_fire():
    from app.services.quality_part2 import html_tag_violation

    src = (
        b"1\n00:00:01,000 --> 00:00:02,000\n{\\an8}<i>This line is in italics,</i>\n"
        b"and this one <b>is bold</b>.\n"
    )
    unit = _units(src)[0]
    assert unit == "<i>This line is in italics,</i>\nand this one <b>is bold</b>.", unit
    assert "stray" in html_tag_violation(unit, "<i>Kursiv,</i>\nund <b>fett</b>>")
    assert "dropped" in html_tag_violation(unit, "Kursiv,\nund <b>fett</b>.")
    out = _f().build(src, {"0": "<i>Kursiv,</i>\nund <b>fett</b> & <3"})
    assert out.endswith(b"{\\an8}<i>Kursiv,</i>\nund <b>fett</b> & <3\n"), out
    print("  [html] #719 / #725 HTML checks still fire on SRT cue text OK")


def test_vtt_keeps_braces():
    vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{\\an8}Hi <i>there</i>\n"
    units = [u.text for u in get_filter("vtt").parse(vtt).units]
    assert units == ["{\\an8}Hi ⟦g1⟧there⟦/g1⟧"], units
    print("  [vtt] WebVTT cue text keeps {\\...} as text (#737 unchanged) OK")


LOC = "ja-JP"


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
        p = Project(name="srt738", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user("srt738@x.test", "srt738", Role.ADMIN, "srt738-pw-123456")
    _row, raw = apikeys.create_key(user.id, "srt738", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("trail.srt", SRT, "application/x-subrip")},
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
    assert "⟦x1⟧" in joined and "{\\" not in joined, srcs
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text)
    text = r.content.decode("utf-8")
    for block in ("{\\an8}", "{\\i1}", "{\\i0}", "{\\c&H00FFFF&}", "{\\b1}", "{\\fs20}", "{\\r}"):
        assert text.count(block) == SRT.decode("utf-8").count(block), (block, text)
    assert "⟦" not in text and "⟧" not in text, text
    assert f"[{LOC}]" in text, text
    assert "00:00:04,000 --> 00:00:06,000\n" in text, text
    print("  [pipeline] API upload, CLI mock translate, API download keep every block OK")


def main():
    test_units_tokenized()
    test_plain_braces_and_html_untouched()
    test_identity()
    test_restore_verbatim()
    test_gate_sees_dropped_and_added()
    test_markers()
    test_legacy_translation()
    test_html_checks_still_fire()
    test_vtt_keeps_braces()
    test_pipeline_api_cli()
    print("SRT OVERRIDES 738 OK")


if __name__ == "__main__":
    main()
