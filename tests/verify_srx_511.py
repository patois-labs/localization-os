"""SRX segmentation: overlapping beforebreak matches and a per-paragraph time budget (#511).

  1. beforebreak `[A-Z]\\.[A-Z]\\.` over "See U.S.A." used to see only the non-overlapping
     match `U.S.`; `S.A.` (the one ending at the boundary) was never yielded, the no-break
     exception did not fire, and the text split after "U.S.A.".
  2. A custom ruleset that is slow in total (each match under the 0.2 s limit, the
     paragraph far over any sane budget) now raises SrxError from segment_spans past the
     budget, and spans_with_default_fallback re-segments with the built-in default rules.

Hermetic + offline. Final line: "SRX 511 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_srx511_")

from app.services import srx

_SRX = rb"""<?xml version="1.0" encoding="UTF-8"?>
<srx version="2.0" xmlns="http://www.lisa.org/srx20">
 <header segmentsubflows="yes" cascade="no"/>
 <body>
  <languagerules>
   <languagerule languagerulename="t">
    <rule break="no"><beforebreak>[A-Z]\.[A-Z]\.</beforebreak><afterbreak>\s</afterbreak></rule>
    <rule break="yes"><beforebreak>[.!?]</beforebreak><afterbreak>\s</afterbreak></rule>
   </languagerule>
  </languagerules>
  <maprules><languagemap languagepattern=".*" languagerulename="t"/></maprules>
 </body>
</srx>
"""


def test_overlapping_beforebreak_matches_are_seen():
    doc = srx.parse(_SRX)
    text = "See U.S.A. today is fine. Next one."
    pieces = [text[a:b] for a, b in doc.segment_spans(text, "en")]
    assert pieces == ["See U.S.A. today is fine.", " Next one."], pieces
    # the same abbreviation at the start must not depend on what precedes it
    text2 = "U.S.A. is large. Yes."
    pieces2 = [text2[a:b] for a, b in doc.segment_spans(text2, "en")]
    assert pieces2 == ["U.S.A. is large.", " Yes."], pieces2
    print("  [511-1] an overlapping beforebreak match ending at the boundary is found OK")


def test_total_budget_falls_back_to_default_rules():
    doc = srx.parse(_SRX)
    text = ("Alpha beta gamma. Delta epsilon zeta. " * 1500).strip()
    old = srx._SRX_PARAGRAPH_BUDGET_S
    srx._SRX_PARAGRAPH_BUDGET_S = 0.001
    try:
        try:
            doc.segment_spans(text, "en")
        except srx.SrxError as exc:
            assert "budget" in str(exc), exc
        else:
            raise AssertionError("a paragraph over the total budget must raise SrxError")
        spans = srx.spans_with_default_fallback(doc, text, "en")
    finally:
        srx._SRX_PARAGRAPH_BUDGET_S = old
    want = srx.default_document().segment_spans(text, "en", budget_s=0)
    assert spans == want and len(spans) > 1000, (len(spans), len(want))
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    # within budget the custom ruleset is used as before
    assert doc.segment_spans("One. Two.", "en") == [(0, 4), (4, 9)]
    print("  [511-2] a slow paragraph falls back to the default rules, no hang OK")


# Breaks at "; " (the built-in default rules never do), so custom and default differ.
_SRX_SEMI = rb"""<?xml version="1.0" encoding="UTF-8"?>
<srx version="2.0" xmlns="http://www.lisa.org/srx20">
 <header segmentsubflows="yes" cascade="no"/>
 <body>
  <languagerules>
   <languagerule languagerulename="t">
    <rule break="yes"><beforebreak>;</beforebreak><afterbreak>\s</afterbreak></rule>
   </languagerule>
  </languagerules>
  <maprules><languagemap languagepattern=".*" languagerulename="t"/></maprules>
 </body>
</srx>
"""


def test_fallback_decision_is_replayed_not_reclocked():
    """Gate (MEDIUM a): the budget is wall-clock, so ingest and export must not each
    decide on their own clock. Ingest records the paragraphs that fell back; export
    (segments_per_unit) replays that decision whatever the clock says now."""
    from types import SimpleNamespace

    from app.services import segment

    doc = srx.parse(_SRX_SEMI)
    text = "Alpha one; beta two; gamma three."
    custom = doc.segment_spans(text, "en")
    default = srx.default_document().segment_spans(text, "en", budget_s=0)
    assert len(custom) == 3 and len(default) == 1, (custom, default)

    class _SlowClock:  # every clock read is one second later: a box far too slow
        def __init__(self):
            self.t = 0.0

        def monotonic(self):
            self.t += 1.0
            return self.t

    old = srx._SRX_PARAGRAPH_BUDGET_S
    real_time = srx.time
    srx._SRX_PARAGRAPH_BUDGET_S = 10.0
    try:
        # INGEST on a slow box: over budget, falls back, and the decision is recorded
        srx.time = _SlowClock()
        with srx.record_fallbacks() as slow_fb:
            assert srx.spans_with_default_fallback(doc, text, "en") == default
        assert slow_fb == {srx.fallback_key(text, "en")}, slow_fb
        # INGEST on a fast box: no fallback, nothing recorded
        srx.time = real_time
        with srx.record_fallbacks() as fast_fb:
            assert srx.spans_with_default_fallback(doc, text, "en") == custom
        assert fast_fb == set(), fast_fb

        # EXPORT on a fast box replays the slow ingest's fallback (1 segment, not 3)
        with srx.replay_fallbacks(slow_fb):
            assert srx.spans_with_default_fallback(doc, text, "en") == default
        # EXPORT on a slow box replays the fast ingest's custom split (3, not 1)
        srx.time = _SlowClock()
        with srx.replay_fallbacks(fast_fb):
            assert srx.spans_with_default_fallback(doc, text, "en") == custom

        # ...and through the export chokepoint itself (segments_per_unit), with the
        # stamped ruleset resolved as export resolves it.
        engine = srx.engine_identity_for_bytes(True, _SRX_SEMI)
        real_lookup = srx._document_for_hash
        srx._document_for_hash = lambda h: doc
        try:
            for recorded, want in ((sorted(slow_fb), 1), ([], 3)):
                skel = {"seg_locale": "en", "seg_engine": engine, "units": [{"text": text}]}
                if recorded:
                    skel["srx_fallback"] = recorded
                got = segment.segments_per_unit(SimpleNamespace(skeleton=skel))
                assert got == [want], (recorded, got)
        finally:
            srx._document_for_hash = real_lookup
    finally:
        srx.time = real_time
        srx._SRX_PARAGRAPH_BUDGET_S = old
    print("  [511-3] ingest's fallback decision is replayed at export, never re-clocked OK")


def test_ingest_records_the_fallback_on_the_skeleton():
    """The real ingest stores the fallback decision; export's re-split then matches the
    stored segment count on a box with a different clock."""
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Project, Segment, TargetLocale
    from app.services import segment, settings
    from app.services.ingest import ingest_document

    class _SlowClock:
        def __init__(self):
            self.t = 0.0

        def monotonic(self):
            self.t += 1.0
            return self.t

    init_db()
    srx.custom_srx_path().write_bytes(_SRX_SEMI)
    settings.set_many({"srx_enabled": "1", "srx_file": "segmentation.srx"})
    data = b"Alpha one; beta two; gamma three.\n"
    real_time = srx.time
    try:
        with Session(engine) as s:
            p = Project(name="srx511", org_id=1, source_locale="en-US", default_provider="mock")
            s.add(p)
            s.commit()
            s.refresh(p)
            s.add(TargetLocale(project_id=p.id, locale="de-DE"))
            s.commit()
            srx.time = _SlowClock()
            slow = ingest_document(s, p, "slow.txt", data, "segmented")
            srx.time = real_time
            fast = ingest_document(s, p, "fast.txt", data, "segmented")
            counts = {}
            for d in (slow, fast):
                s.refresh(d)
                counts[d.filename] = len(
                    s.exec(
                        select(Segment).where(
                            Segment.document_id == d.id, Segment.target_locale == "de-DE"
                        )
                    ).all()
                )
            assert counts == {"slow.txt": 1, "fast.txt": 3}, counts
            assert slow.skeleton.get("srx_fallback"), slow.skeleton
            assert "srx_fallback" not in fast.skeleton, fast.skeleton
            # export re-splits on the OTHER clock and still matches what was stored
            srx.time = real_time
            assert segment.segments_per_unit(slow) == [1]
            srx.time = _SlowClock()
            assert segment.segments_per_unit(fast) == [3]
    finally:
        srx.time = real_time
        settings.set_many({"srx_enabled": "0", "srx_file": ""})
        srx.custom_srx_path().unlink(missing_ok=True)
    print("  [511-4] ingest stores the fallback; export matches the stored count OK")


def main():
    test_overlapping_beforebreak_matches_are_seen()
    test_total_budget_falls_back_to_default_rules()
    test_fallback_decision_is_replayed_not_reclocked()
    test_ingest_records_the_fallback_on_the_skeleton()
    print("SRX 511 OK")


if __name__ == "__main__":
    main()
