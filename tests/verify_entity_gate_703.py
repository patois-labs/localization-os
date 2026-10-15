"""An engine that unescapes or changes an HTML character reference goes to review (#703).

Format soak round 7, local engine, i18n JSON strings meant for innerHTML, into de-DE, fr-FR,
ja-JP, zh-CN and ru-RU: `Press &lt;Enter&gt; to send` came back as `<Enter>` (5 of 5: an
unknown tag, the word vanishes from the page), `2&#162;` (cents) as `2 $` / `2ドル` (5 of 5:
a price 100x too high), `Smith &amp; Jones` as a raw `&` (3 of 5). A real Okapi TXML unit
whose text is `&#162;` was stored as `§` (ko-KR). Every one was saved as clean MT.

  comparator  quality.placeholder_violation now also names (a) a markup escape (&lt; &gt;
              &amp;, any spelling) the target writes as the raw character and (b) a symbol
              reference (Unicode Sc / So: currency, ©, ®, ™, °) whose character is in neither
              form in the target. Silent: typographic references localised or dropped
              (&nbsp; &quot; &hellip; &laquo;), a symbol written as its character
              (&euro;9 -> 9 €), an unknown name, ordinary placeholders unchanged.
  seam        quality.commit_mt_output routes the live shapes to NEEDS_REVIEW under the
              placeholder_integrity flag (soft, TM-ineligible); a correct target is clean.
  html        (gate) a filter that escapes raw <, > and & on export (HTML family, MDX: class
              attribute `escapes_raw_text`) writes a raw-character target back as the
              reference, so on its documents the ESCAPE rule is off (seam, approve and
              human-save callers pass `segment=`); the SYMBOL rule still holds. Export proof
              included; json / md / yaml / properties / po keep the rule.

Hermetic (throwaway LOC_TMS_DATA_DIR, no engine). ASCII output; final line
"ENTITY GATE 703 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_entity703_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus
from app.services import quality

FLAGGED = [
    # live engine outputs (soak triage/entities-r7)
    ("Press &lt;Enter&gt; to send the message.", "Drücken Sie <Enter>, um sie zu senden."),
    ("Press &lt;Enter&gt; to send the message.", "按 <Enter> 键发送消息。"),
    ("Every message costs 2&#162; after the free quota.", "Jede Nachricht kostet 2 $ danach."),
    ("Every message costs 2&#162; after the free quota.", "1通につき2ドルかかります。"),
    ("Built by Smith &amp; Jones for teams.", "由 Smith & Jones 为团队打造。"),
    ("&#162;", "§"),  # real Okapi Test03.mif.txml, ko-KR
    ("Text with &lt;i>HTML&lt;/i> codes.", "Testo con <i>HTML</i> codici."),
    # other spellings
    ("Use &#60;br&#62; here.", "Utilisez <br> ici."),
    ("Use &#x3C;br&#x3E; here.", "Utilisez <br> ici."),
    ("&copy; 2026 Kestrel Labs", "2026 Kestrel Labs"),
    ("Only &euro;9 a month", "Nur 9 $ im Monat"),
]
SILENT = [
    ("&copy; 2026 Kestrel Labs. All rights reserved.", "&copy; 2026 Kestrel Labs. Tous droits."),
    ("&copy; 2026 Kestrel Labs.", "© 2026 Kestrel Labs."),
    ("Only&nbsp;&euro;9 per month.", "Nur 9&nbsp;€ pro Monat."),
    ("Only&nbsp;&euro;9 per month.", "Всего 9&nbsp;€ в месяц."),
    ("Terms&nbsp;of&nbsp;Service", "Nutzungsbedingungen"),
    ("Click &quot;Save&quot; to keep it.", "Klicken Sie auf „Speichern“."),
    ("Loading&hellip;", "Chargement…"),
    ("&laquo; Back", "« Retour"),
    ("Press &lt;Enter&gt; to send.", "Appuyez sur &lt;Entrée&gt; pour envoyer."),
    ("Built by Smith &amp; Jones.", "Construit par Smith &amp; Jones."),
    ("Built by Smith &amp; Jones.", "Hergestellt von Smith und Jones."),  # dropped, no raw &
    ("Tom & Jerry <b>now</b>", "Tom & Jerry <b>jetzt</b>"),  # no references at all
    ("Save 20% &bogus; today", "Sparen Sie heute 20 %"),  # unknown name
    ("You have %s items", "Tienes %s elementos"),
]


def _fail(msg):
    print("FAIL:", str(msg).encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


def test_comparator():
    pv = quality.placeholder_violation
    for src, tgt in FLAGGED:
        r = pv(src, tgt)
        if "character reference" not in r:
            _fail(f"[comparator] must flag {src!r} -> {tgt!r}: {r!r}")
    for src, tgt in SILENT:
        r = pv(src, tgt)
        if r:
            _fail(f"[comparator] must stay silent on {src!r} -> {tgt!r}: {r!r}")
    r = pv("Save %s with &lt;b&gt;", "Speichern mit <b>")
    if "dropped %s" not in r or "unescaped &lt;" not in r:
        _fail(f"[comparator] both reasons must be named together: {r!r}")
    if pv("&#162;", "   "):
        _fail("[comparator] an empty target abstains")
    print(f"  [comparator] {len(FLAGGED)} flagged, {len(SILENT)} silent OK")


def test_seam():
    init_db()
    with Session(engine) as session:
        p = Project(
            name="E703", source_locale="en-US", target_locales=["de-DE"], default_provider="mock"
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="e.json", fmt="json", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)
        for i, (src, bad, good) in enumerate(
            [
                (FLAGGED[0][0], FLAGGED[0][1], "Drücken Sie &lt;Enter&gt;, um sie zu senden."),
                (FLAGGED[2][0], FLAGGED[2][1], "Jede Nachricht kostet 2&#162; danach."),
            ]
        ):
            seg = Segment(
                document_id=d.id,
                target_locale="de-DE",
                order_index=i,
                source_text=src,
                status=SegStatus.NEW,
            )
            session.add(seg)
            session.commit()
            session.refresh(seg)
            out = quality.commit_mt_output(session, seg, bad, source="mt")
            session.commit()
            session.refresh(seg)
            if seg.status != SegStatus.NEEDS_REVIEW or out.tm_eligible:
                _fail(f"[seam] {bad!r} must go to review, TM-ineligible: {seg.status}")
            flags = [
                f for f in (seg.qa_flags or []) if f.get("type") == quality.PLACEHOLDER_FLAG_TYPE
            ]
            if len(flags) != 1 or "character reference" not in flags[0].get("reason", ""):
                _fail(f"[seam] expected one placeholder_integrity flag: {seg.qa_flags!r}")
            out = quality.commit_mt_output(session, seg, good, source="mt")
            session.commit()
            session.refresh(seg)
            if seg.status != SegStatus.MT or out.placeholder_violation:
                _fail(f"[seam] the corrected target must be clean: {seg.status} {seg.qa_flags!r}")
    print("  [seam] live shapes held for review; corrected targets clean OK")


def test_html_family():
    from app.filters.base import get_filter, supported_formats
    from app.filters.html_filter import HtmlFilter
    from app.services import quality_part2

    rebuilt = quality_part2._fmt_rebuilds_escapes
    html_family = [f for f in supported_formats() if isinstance(get_filter(f), HtmlFilter)]
    for fmt in [*html_family, "epub", "eml", "flprjzip", "mdx"]:
        if not rebuilt(fmt):
            _fail(f"[html] {fmt} escapes raw text on export and must be marked")
    for fmt in ("json", "md", "yaml", "properties", "po", "txt", "strings", "arb", None, ""):
        if rebuilt(fmt):
            _fail(f"[html] {fmt!r} writes text verbatim and must keep the escape rule")
    # The claim the exemption rests on: the builder re-escapes a raw target.
    out = get_filter("html").build(
        b"<html><body><p>Tom &amp; Jerry &lt;Enter&gt; ok</p></body></html>",
        {"0": "Tom & Jerry <Enter> ok"},
    )
    if b"Tom &amp; Jerry &lt;Enter&gt; ok" not in out:
        _fail(f"[html] the HTML builder must re-escape a raw target: {out!r}")
    out = get_filter("json").build(b'{"k": "Tom &amp; Jerry"}', {"0": "Tom & Jerry <b>"})
    if b"Tom & Jerry <b>" not in out:
        _fail(f"[html] JSON writes the target verbatim (why the rule stays on): {out!r}")

    with Session(engine) as session:
        p = Project(
            name="E703h", source_locale="en-US", target_locales=["de-DE"], default_provider="mock"
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        docs = {}
        for fmt in ("html", "json"):
            d = Document(project_id=p.id, filename=f"e.{fmt}", fmt=fmt, mode="segmented")
            session.add(d)
            session.commit()
            session.refresh(d)
            docs[fmt] = d
        cases = [
            ("Press &lt;Enter&gt; to send.", "Drücken Sie <Enter> zum Senden.", False),
            ("Built by Smith &amp; Jones.", "由 Smith & Jones 打造。", False),
            ("Costs 2&#162; each.", "Kostet 2 $ pro Stück.", True),  # SYMBOL still holds
        ]
        for i, (src, tgt, flag_on_html) in enumerate(cases):
            for fmt, d in docs.items():
                seg = Segment(
                    document_id=d.id,
                    target_locale="de-DE",
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                )
                session.add(seg)
                session.commit()
                session.refresh(seg)
                want = flag_on_html if fmt == "html" else True
                r = quality.placeholder_violation(src, tgt, segment=seg)
                if bool(r) != want:
                    _fail(f"[html] {fmt} {src!r} -> {tgt!r}: want flagged={want}, got {r!r}")
                quality.commit_mt_output(session, seg, tgt, source="mt")
                session.commit()
                session.refresh(seg)
                st = SegStatus.NEEDS_REVIEW if want else SegStatus.MT
                if seg.status != st:
                    _fail(f"[html] seam {fmt} {src!r}: want {st}, got {seg.status}")
                # a detached segment (no session) resolves its format on its own
                session.expunge(seg)
                if bool(quality.placeholder_violation(src, tgt, segment=seg)) != want:
                    _fail(f"[html] detached {fmt} segment must judge the same: {src!r}")
                session.add(seg)
    # a key segment (no document) is written verbatim: the rule stays on
    key_seg = Segment(document_id=None, target_locale="de-DE", source_text="x")
    if not quality.placeholder_violation(cases[0][0], cases[0][1], segment=key_seg):
        _fail("[html] a segment without a document keeps the escape rule")
    print(f"  [html] {len(html_family) + 4} escaping formats exempt from the ESCAPE rule OK")


if __name__ == "__main__":
    test_comparator()
    test_seam()
    test_html_family()
    print("ENTITY GATE 703 OK")
