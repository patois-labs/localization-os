"""An engine-translated ICU number / date / time argument type or style goes to review (#727).

Format soak round 10, local engine, a Flutter ARB file with 8 ICU messages into de-DE, fr-FR,
es-ES and ja-JP: fr-FR wrote `{progress, nombre, pourcentage} terminé` for
`{progress, number, percent} complete` (gen-l10n / FormatJS / java.text.MessageFormat refuse an
unknown format type, so the whole catalog stops building). #709's check reads only plural /
select / selectordinal headers, so it was saved as clean MT and exported.

  comparator  quality.placeholder_violation also names (a) a simple argument whose TYPE is a
              predefined ICU type (number, date, time, spellout, ordinal, duration) and whose
              type, or predefined STYLE keyword (integer, currency, percent, short, medium,
              long, full), the target changed or dropped, and (b) a plural `offset:N` the
              target lost. Silent: a custom pattern or skeleton style localised
              (`dd.MM.yyyy`, `::currency/EUR`), arguments reordered, spacing, plain `{name}`.
  seam        quality.commit_mt_output routes the live shape to NEEDS_REVIEW under the
              placeholder_integrity flag (soft, TM-ineligible); the correct target is clean.
  linear      hostile shapes (long space runs inside unclosed arguments) stay fast.

Hermetic (throwaway LOC_TMS_DATA_DIR, no engine). ASCII output; final line
"ICU FORMAT ARGS 727 OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_icufmt727_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus
from app.services import quality

LIVE_SRC = "{progress, number, percent} complete"
LIVE_BAD = "{progress, nombre, pourcentage} terminé"  # live fr-FR (soak triage/fmt-r10/icu)
LIVE_GOOD = "{progress, number, percent} terminé"

FLAGGED = [
    (LIVE_SRC, LIVE_BAD, "{progress, number, percent} changed"),
    ("Due {d, date, short}", "Fällig {d, datum, kurz}", "{d, date, short} changed"),
    ("Due {d, date, short}", "Fällig {d, date, kurz}", "{d, date, short} changed"),
    ("At {t, time}", "A {t, heure}", "{t, time} changed"),
    ("At {t, time, short}", "A {t}", "{t, time, short} changed"),
    ("Total: {amount, number, currency}", "Gesamt: {amount, Zahl, Währung}", "{amount, number"),
    ("{0,number,integer} files", "{0,Zahl,integer} Dateien", "{0, number, integer} changed"),
    (
        "{n, plural, offset:1 =0 {nobody} one {you} other {you and # others}}",
        "{n, plural, Versatz:1 =0 {niemand} one {du} other {du und # andere}}",
        "offset:1 lost",
    ),
]
SILENT = [
    (LIVE_SRC, LIVE_GOOD),
    ("Due {d, date, dd.MM.yyyy}", "Fällig {d, date, yyyy/MM/dd}"),  # a pattern may be localised
    ("Total {n, number, ::currency/EUR}", "Gesamt {n, number, ::currency/EUR}"),
    ("{d, date, short} and {d, date, long}", "{d, date, long} et {d, date, short}"),  # reordered
    ("{d, date,short}", "{d, date, short }"),  # spacing
    ("{0,number,integer} files", "{0, number, integer} Dateien"),
    (
        "{n, plural, offset:1 =0 {nobody} one {you} other {you and # others}}",
        "{n, plural, offset:1 =0 {niemand} one {du} other {du und # andere}}",
    ),
    (
        "Hello {name}, {count, plural, one {# item} other {# items}}",
        "Hallo {name}, {count, plural, one {# Element} other {# Elemente}}",
    ),
    ("Pick {a, select, x {X} other {Y}}", "Wähle {a, select, x {X} other {Y}}"),
    ("Status: {state}", "Statut : {etat}"),  # not a format argument: other checks judge names
]


def _fail(msg):
    print("FAIL:", msg.encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


def test_comparator():
    for src, tgt, want in FLAGGED:
        r = quality.placeholder_violation(src, tgt)
        if want not in r:
            _fail(f"[comparator] {tgt!r} must be flagged naming {want!r}, got {r!r}")
    for src, tgt in SILENT:
        r = quality.placeholder_violation(src, tgt)
        if "ICU" in r:
            _fail(f"[comparator] must stay silent: {tgt!r}: {r!r}")
    if quality.placeholder_violation(LIVE_SRC, "   "):
        _fail("[comparator] an empty target abstains")
    print(f"  [comparator] {len(FLAGGED)} flagged, {len(SILENT)} silent OK")


def test_seam():
    init_db()
    with Session(engine) as session:
        p = Project(
            name="F727", source_locale="en-US", target_locales=["fr-FR"], default_provider="mock"
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        d = Document(project_id=p.id, filename="app_en.arb", fmt="arb", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(d)
        seg = Segment(
            document_id=d.id,
            target_locale="fr-FR",
            order_index=0,
            source_text=LIVE_SRC,
            status=SegStatus.NEW,
        )
        session.add(seg)
        session.commit()
        session.refresh(seg)
        out = quality.commit_mt_output(session, seg, LIVE_BAD, source="mt")
        session.commit()
        session.refresh(seg)
        if seg.status != SegStatus.NEEDS_REVIEW or out.tm_eligible:
            _fail(f"[seam] the live target must go to review, TM-ineligible: {seg.status}")
        flags = [f for f in (seg.qa_flags or []) if f.get("type") == quality.PLACEHOLDER_FLAG_TYPE]
        if len(flags) != 1 or "number, percent" not in flags[0].get("reason", ""):
            _fail(
                f"[seam] want one placeholder_integrity flag naming the argument: {seg.qa_flags!r}"
            )
        quality.commit_mt_output(session, seg, LIVE_GOOD, source="mt")
        session.commit()
        session.refresh(seg)
        if seg.status != SegStatus.MT:
            _fail(f"[seam] the correct target must be clean MT: {seg.status} {seg.qa_flags!r}")
    print("  [seam] live fr-FR target held for review; the correct one is clean OK")


def test_linear():
    from app.services.quality_part2 import icu_format_violation

    t0 = time.perf_counter()
    for x in (
        ("{a,number," + " " * 100) * 1500,
        ("{a,number,short" + " " * 100) * 1500,
        "{a, b, " * 30000,
        "{" * 100000,
        "{0,number," + "x" * 150000,
        "{a, number, " + "{b, number, " * 20000,
    ):
        icu_format_violation(x, x + "z")
    # gate: every one of 7k distinct arguments changed (one target scan per changed argument
    # was quadratic: about 2 s); the reason stays short (it listed all of them)
    src = "".join(f"{{a{i}, number, integer}}" for i in range(7000))
    tgt = "".join(f"{{a{i}, nombre, entier}}" for i in range(7000))
    reason = icu_format_violation(src, tgt)
    if not reason or len(reason) > 400:
        _fail(f"[linear] many changed arguments: reason {len(reason)} chars")
    took = time.perf_counter() - t0
    if took > 3.0:
        _fail(f"[linear] hostile shapes took {took:.1f} s")
    print("  [linear] hostile shapes stay fast OK")


if __name__ == "__main__":
    test_comparator()
    test_seam()
    test_linear()
    print("ICU FORMAT ARGS 727 OK")
