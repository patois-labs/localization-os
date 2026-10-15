"""Chunk 6 verification — post-editing CAT editor, offline.

- editor page renders editable targets
- save: stores target + accumulates Time-to-Edit, returns QA flags
- approve: marks APPROVED and writes the pair back to the segment TM
          (a later TM lookup then returns it at 100%)
- alternates: returns multiple candidate translations
- concordance: substring search over the TM
- QA flags: untranslated + missing-placeholder detection
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Segment
from app.services import tm

TMX = """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4"><header srclang="en-US"/><body>
  <tu><tuv xml:lang="en-US"><seg>Adventure awaits.</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>L'aventure vous attend.</seg></tuv></tu>
</body></tmx>"""


def docx_from(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def main() -> None:
    init_db()
    client = TestClient(app)

    r = client.post(
        "/projects",
        data={
            "name": "Editor Test",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    client.post(
        f"/projects/{pid}/tm/import",
        data={"source_locale": "en-US", "target_locale": "fr-FR"},
        files={"file": ("m.tmx", TMX.encode(), "application/xml")},
    )
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_from(["Visit {LINK} now.", "Hello world.", "Adventure awaits."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])

    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        link_seg, hello_seg, adv_seg = segs[0].id, segs[1].id, segs[2].id

    # editor renders
    page = client.get(f"/documents/{doc_id}/editor?locale=fr-FR").text
    assert "Hello world." in page and "tgt-edit" in page, "editor grid not rendered"
    print("  editor page renders editable targets")

    # save + Time-to-Edit
    r = client.post(
        f"/segments/{hello_seg}/save", data={"target_text": "Bonjour le monde.", "tte_ms": "1500"}
    )
    d = r.json()
    assert d["ok"] and d["status"] == "edited", d
    with Session(engine) as s:
        assert s.get(Segment, hello_seg).tte_ms == 1500
    print("  save stores target + Time-to-Edit (1500ms)")

    # approve -> TM write-back
    r = client.post(f"/segments/{hello_seg}/approve")
    d = r.json()
    assert d["status"] == "approved" and d["tm_written"], d
    with Session(engine) as s:
        m = tm.lookup(s, pid, "Hello world.", "en-US", "fr-FR")
    assert m and m["match_pct"] == 100 and m["target_text"] == "Bonjour le monde.", m
    print("  approve writes back to TM (lookup now 100%)")

    # alternates
    r = client.post(f"/segments/{adv_seg}/alternates")
    alts = r.json()["alternates"]
    assert isinstance(alts, list) and len(alts) == 3, alts
    print(f"  alternates returned {len(alts)} candidates")

    # concordance (matches the imported TMX target)
    r = client.get(f"/concordance?project_id={pid}&q=aventure&source=en-US&target=fr-FR")
    res = r.json()["results"]
    assert res and "aventure" in res[0]["target"].lower(), res
    print(f"  concordance found {len(res)} TM match(es)")

    # QA: untranslated
    r = client.post(
        f"/segments/{adv_seg}/save", data={"target_text": "Adventure awaits.", "tte_ms": "0"}
    )
    types = {f["type"] for f in r.json()["qa"]}
    assert "untranslated" in types, types
    # QA: missing placeholder
    r = client.post(
        f"/segments/{link_seg}/save", data={"target_text": "Visitez maintenant.", "tte_ms": "0"}
    )
    types = {f["type"] for f in r.json()["qa"]}
    # #562: the save response now uses the SAME builder as the reloaded grid, which drops the
    # broad live "tags" badge when the persisted placeholder_integrity badge names the same loss.
    assert types & {"tags", "placeholder_integrity"}, types
    print("  QA flags: untranslated + missing-placeholder detected")

    # QA: inline-fidelity placeholder integrity (Phase 5 chunk 6). A target that
    # dropped one of the ⟦rN⟧ run tokens must flag "placeholder" (major), and it
    # must NOT double-flag as "tags".
    from app.services import qa

    src = "⟦r1⟧Please click ⟦/r1⟧⟦r2⟧Submit⟦/r2⟧"
    good = "⟦r1⟧Veuillez cliquer ⟦/r1⟧⟦r2⟧Envoyer⟦/r2⟧"
    dropped = "⟦r1⟧Veuillez cliquer Envoyer⟦/r1⟧"  # r2 tokens gone
    assert not any(f["type"] == "placeholder" for f in qa.check_segment(src, good)), (
        "intact tokens should not flag"
    )
    flags = qa.check_segment(src, dropped)
    ph = [f for f in flags if f["type"] == "placeholder"]
    assert ph and ph[0]["severity"] == "major", flags
    assert not any(f["type"] == "tags" for f in flags), "should not double-flag as tags"
    # the message must NAME the dropped token(s), never be an empty "Inline placeholder "
    assert ph[0]["message"].strip() != "Inline placeholder", ph[0]["message"]
    assert "dropped" in ph[0]["message"] and "r2" in ph[0]["message"], ph[0]["message"]
    print("  QA flags: inline-placeholder drop detected (no double-flag)")

    # A duplicated token (same id emitted twice) used to yield an EMPTY message
    # ("Inline placeholder "); it must now name the duplicated token.
    dup = qa.check_segment("⟦r1⟧A⟦/r1⟧", "⟦r1⟧A⟦/r1⟧⟦r1⟧B⟦/r1⟧")
    dph = [f for f in dup if f["type"] == "placeholder"]
    assert dph and dph[0]["message"].strip() != "Inline placeholder", dph
    assert "duplicated" in dph[0]["message"] and "r1" in dph[0]["message"], dph[0]["message"]
    print("  QA flags: duplicated-token message is specific (never empty)")

    # QA runs even on a 100% TM short-circuit. Upload a FRESH doc whose only
    # segment ("Adventure awaits.") exact-matches the imported TM but has no target
    # yet; pretranslate_stream fills it from TM and must still emit QA flags in that
    # segment event (the 100% short-circuit must not bypass QA).
    from app.services import translate

    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "tmhit.docx",
                docx_from(["Adventure awaits."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    tm_doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        events = list(translate.pretranslate_stream(s, tm_doc_id, "fr-FR"))
    tm_ev = next((e for e in events if e.get("source") == "tm" and e.get("pct") == 100), None)
    assert tm_ev is not None, f"expected a 100% TM-hit segment event, got {events}"
    assert "qa" in tm_ev, "TM-hit event must carry QA flags (QA not bypassed)"
    print("  QA runs on 100% TM-hit path (short-circuit does not bypass QA)")

    # --- BUG-1: tagchips display filter (read-only surfaces de-noise tokens) ---
    from markupsafe import Markup

    from app.web.deps import tagchips

    tokenized = "⟦r1⟧Contact ⟦/r1⟧⟦r2⟧sales⟦/r2⟧⟦x3⟧ <b>done</b>"
    out = tagchips(tokenized)
    assert isinstance(out, Markup), "tagchips must return markupsafe.Markup"
    out = str(out)
    # never emit a raw ⟦/⟧ into rendered read-only HTML
    assert "⟦" not in out and "⟧" not in out, f"raw token leaked: {out}"
    # placeholders become tag chips (paired open/close + a point token)
    assert 'class="tag-chip open"' in out and 'class="tag-chip close"' in out, out
    assert 'class="tag-chip point"' in out, out
    # surrounding HTML is escaped (the literal <b> in source is NOT live markup)
    assert "&lt;b&gt;done&lt;/b&gt;" in out and "<b>done</b>" not in out, out
    # stored segment text is untouched by the display transform
    with Session(engine) as s:
        before = s.get(Segment, hello_seg).source_text
    _ = tagchips(before)
    with Session(engine) as s:
        assert s.get(Segment, hello_seg).source_text == before, "tagchips mutated stored text"
    # a token-free string round-trips as plain escaped text (no chips)
    assert str(tagchips("plain & simple")) == "plain &amp; simple", tagchips("plain & simple")
    # the editor renders the SOURCE via chips (no raw ⟦) and shows the tag hint;
    # the target textarea keeps the LITERAL tokens so edits round-trip.
    with Session(engine) as s:
        seg = s.get(Segment, hello_seg)
        seg.source_text = tokenized
        seg.target_text = "⟦r1⟧Contactez ⟦/r1⟧⟦r2⟧ventes⟦/r2⟧⟦x3⟧"
        s.add(seg)
        s.commit()
    epage = client.get(f"/documents/{doc_id}/editor?locale=fr-FR").text
    assert 'class="tag-chip' in epage, "editor source cell should render tag chips"
    assert "contains formatting tags" in epage, "editor should show the tag hint"
    # the editable textarea must still contain the literal tokens for round-trip
    assert "⟦r1⟧Contactez" in epage, "editor textarea must keep literal target tokens"
    print(
        "  BUG-1: tagchips filter de-noises read-only surfaces (no raw tokens, HTML escaped, tokens kept in editor)"  # noqa: E501
    )

    print("CHUNK 6 OK")


if __name__ == "__main__":
    main()
