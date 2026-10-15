"""SRX 2.0 segmentation verification (#38), offline + hermetic.

Covers:
  1. Parsing a hand-written SRX doc: break / no-break / cascade / maprules.
  2. ICU -> Python re character-class translation (\\p{L} \\p{Lu} \\p{Ll} \\p{N}
     and negations) + rejection of unsupported ICU properties.
  3. The shipped default.srx reproduces the LEGACY regex splitter's output on a
     corpus of representative sentences (abbreviations, decimals, URLs, quotes,
     brackets, lowercase-after, CJK).
  4. srx_enabled=0 -> the legacy split path is byte-identical (the sacred gate).
  5. srx_enabled=1 -> segmentation goes through the SRX engine (default.srx).
  6. Invalid SRX upload is rejected with a clear message; a valid one is accepted.
  7. Per-language rule selection: ja text with the CJK terminator.
  8. Paired inline-fidelity token spans are never orphaned on the SRX path.
  9. Red-team audit 2026-10-03 finding 02: a catastrophic uploaded rule times out
     and falls back to the built-in default ruleset with a loud warning; an
     over-length rule pattern is rejected at upload.

Prints ASCII only (cp1252 console safe).
"""

import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

import re
import time

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, TransMode
from app.services import export, segment, settings, srx


def _seg(doc, text, locale):
    """Strip + drop-empty a raw SrxDocument.segment result (what segment_text does)."""
    return [p.strip() for p in doc.segment(text, locale) if p.strip()]


# The representative corpus. Each entry is exercised against BOTH the legacy
# splitter and the shipped-default SRX path; they MUST agree.
CORPUS = [
    "Adventure awaits. Save up to 50% this weekend only.",
    "Dr. Smith went home. He was tired.",  # version 2: no break after "Dr." (decision 15)
    "Max. 5 files can be uploaded. Then wait.",  # version 2: "Max." before a digit
    "We sell pears, etc. Next week we add plums.",  # "etc." still ends a sentence
    "The value is 3.14 today. Next.",
    "Visit https://example.com/a. Then leave.",
    'He said "Hello." She left.',  # no split (quote before ws)
    "e.g. this is fine. Another one.",
    "One two three.",
    "lowercase after. next is lower.",  # no split (lowercase follows)
    "Ends without terminator",
    "(Parenthetical start.) Next One here.",
    "A! B? C.",
    "No.5 is here. Done.",
    "Word.Word no space here.",
    "Tab\tseparated. Next.",
    "CJK test. Foo bar baz here.",
    "",  # empty -> []
    "   ",  # whitespace -> []
]


def test_icu_translation():
    assert re.match(srx.icu_to_python(r"\p{Lu}"), "A")
    assert not re.match(srx.icu_to_python(r"\p{Lu}"), "a")
    assert re.match(srx.icu_to_python(r"\p{Ll}"), "e")
    assert not re.match(srx.icu_to_python(r"\p{Ll}"), "E")
    assert re.match(srx.icu_to_python(r"\p{N}"), "7")
    assert re.match(srx.icu_to_python(r"\p{L}"), "Z")
    assert re.match(srx.icu_to_python(r"\p{L}"), "e")
    # negation
    assert re.match(srx.icu_to_python(r"\P{L}"), "5")
    assert not re.match(srx.icu_to_python(r"\P{L}"), "x")
    # inside a character class the property splices in
    pat = srx.icu_to_python(r"[\p{Lu}\p{N}]")
    assert re.match(pat, "A") and re.match(pat, "9") and not re.match(pat, "a")
    # long name + single-letter shorthand
    assert re.match(srx.icu_to_python(r"\p{Uppercase_Letter}"), "Q")
    assert re.match(srx.icu_to_python(r"\pL"), "k")
    # unsupported property is rejected
    try:
        srx.icu_to_python(r"\p{P}")
        raise AssertionError("expected SrxError for unsupported property")
    except srx.SrxError:
        pass
    # a non-property pattern passes through unchanged
    assert srx.icu_to_python(r"[.!?]\s+") == r"[.!?]\s+"
    print("  ICU translation: L/Lu/Ll/N + negation + class-splice + reject OK")


def test_parse_break_nobreak_maprules():
    doc_xml = r"""<?xml version="1.0"?>
<srx xmlns="http://www.lisa.org/srx20" version="2.0">
  <header segmentsubflows="yes" cascade="no"/>
  <body>
    <languagerules>
      <languagerule languagerulename="English">
        <rule break="no"><beforebreak>\bMr\.</beforebreak><afterbreak>\s</afterbreak></rule>
        <rule break="yes"><beforebreak>[.!?]</beforebreak><afterbreak>\s+\p{Lu}</afterbreak></rule>
      </languagerule>
    </languagerules>
    <maprules>
      <languagemap languagepattern="[Ee][Nn].*" languagerulename="English"/>
    </maprules>
  </body>
</srx>"""
    d = srx.parse(doc_xml)
    assert d.cascade is False
    assert len(d.languagerules) == 1 and len(d.maprules) == 1
    # no-break rule (placed first) suppresses the split after "Mr."
    r = _seg(d, "Mr. Smith went home. He left.", "en-US")
    assert r == ["Mr. Smith went home.", "He left."], r
    # a locale not matched by any maprule -> no rules -> whole text is one segment
    r2 = _seg(d, "Mr. Smith went home. He left.", "fr-FR")
    assert r2 == ["Mr. Smith went home. He left."], r2
    print("  parse break/no-break + per-language maprule selection OK")


def test_cascade():
    casc = r"""<srx xmlns="http://www.lisa.org/srx20"><header cascade="yes"/><body>
      <languagerules>
        <languagerule languagerulename="A"><rule break="yes"><beforebreak>;</beforebreak><afterbreak></afterbreak></rule></languagerule>
        <languagerule languagerulename="B"><rule break="yes"><beforebreak>[.]</beforebreak><afterbreak>\s</afterbreak></rule></languagerule>
      </languagerules>
      <maprules>
        <languagemap languagepattern="en.*" languagerulename="A"/>
        <languagemap languagepattern=".*" languagerulename="B"/>
      </maprules></body></srx>"""  # noqa: E501
    dc = srx.parse(casc)
    assert dc.cascade is True
    # cascade: BOTH matching languagerules' rules apply (; from A, . from B)
    assert _seg(dc, "one;two. three", "en-US") == ["one;", "two.", "three"]
    # non-cascade: only the FIRST matching languagemap (A) applies
    dnc = srx.parse(casc.replace('cascade="yes"', 'cascade="no"'))
    assert _seg(dnc, "one;two. three", "en-US") == ["one;", "two. three"]
    print("  cascade=yes (concatenate) vs cascade=no (first-match) OK")


def test_validate_rejects_invalid():
    ok, _ = srx.validate(b"<notsrx/>")
    assert not ok
    ok, _ = srx.validate(b"this is not xml <<<")
    assert not ok
    # unsupported ICU property inside a rule regex is rejected at parse time
    bad = (
        '<srx xmlns="http://www.lisa.org/srx20"><body><languagerules>'
        '<languagerule languagerulename="X"><rule break="yes">'
        "<beforebreak>\\p{P}</beforebreak><afterbreak></afterbreak></rule>"
        "</languagerule></languagerules>"
        '<maprules><languagemap languagepattern=".*" languagerulename="X"/>'
        "</maprules></body></srx>"
    )
    ok, msg = srx.validate(bad.encode())
    assert not ok and "p{P}" in msg, msg
    # a languagemap pointing at a missing languagerule is rejected
    ok, _ = srx.validate(
        b'<srx xmlns="http://www.lisa.org/srx20"><body><languagerules>'
        b'<languagerule languagerulename="X"><rule break="yes">'
        b"<beforebreak>[.]</beforebreak><afterbreak></afterbreak></rule>"
        b"</languagerule></languagerules><maprules>"
        b'<languagemap languagepattern=".*" languagerulename="MISSING"/>'
        b"</maprules></body></srx>"
    )
    assert not ok
    # the shipped default is valid
    ok, msg = srx.validate(srx._DEFAULT_SRX_PATH.read_bytes())
    assert ok, msg
    print("  invalid SRX rejected (root/xml/ICU/map) + default.srx valid OK")


def test_default_srx_mirrors_legacy():
    """The heart of the no-regression guarantee: the shipped default.srx must
    reproduce the legacy splitter on the whole corpus."""
    mismatches = []
    for t in CORPUS:
        legacy = segment.split_sentences(t)  # srx_enabled defaults 0 here
        via_srx = srx.segment_text(t, "en-US")
        if legacy != via_srx:
            mismatches.append((t, legacy, via_srx))
    assert not mismatches, f"default.srx diverges from legacy: {mismatches!r}"
    print("  default.srx reproduces legacy splitter on %d cases OK" % len(CORPUS))  # noqa: UP031


def test_gate_off_is_legacy_and_on_uses_srx():
    # Gate OFF (default): split_sentences must be byte-identical to the pure
    # legacy path. We snapshot the legacy output while the setting is off.
    settings.set_many({"srx_enabled": "0", "srx_file": ""})
    off = {t: segment.split_sentences(t, "en-US") for t in CORPUS}

    # Gate ON: split_sentences routes through the SRX engine. With the shipped
    # default that equals the legacy output for every corpus case (proven above),
    # so the observable result is unchanged but the CODE PATH is the SRX one.
    settings.set_many({"srx_enabled": "1", "srx_file": ""})
    for t in CORPUS:
        assert segment.split_sentences(t, "en-US") == off[t], t

    # Prove the SRX path is actually taken: a custom ruleset that splits on ';'
    # changes the output only when the gate is on.
    semi = (
        '<srx xmlns="http://www.lisa.org/srx20"><body><languagerules>'
        '<languagerule languagerulename="X"><rule break="yes">'
        "<beforebreak>;</beforebreak><afterbreak></afterbreak></rule>"
        "</languagerule></languagerules><maprules>"
        '<languagemap languagepattern=".*" languagerulename="X"/>'
        "</maprules></body></srx>"
    )
    srx.custom_srx_path().write_bytes(semi.encode())
    settings.set_many({"srx_enabled": "1", "srx_file": "segmentation.srx"})
    assert segment.split_sentences("a;b;c", "en-US") == ["a;", "b;", "c"]
    # gate OFF with the same custom file present -> legacy path ignores it
    settings.set_many({"srx_enabled": "0"})
    assert segment.split_sentences("a;b;c", "en-US") == ["a;b;c"]
    # restore default state for any later test
    settings.set_many({"srx_enabled": "0", "srx_file": ""})
    print("  gate: OFF=legacy byte-identical, ON=SRX engine (custom rule fires) OK")


def test_per_language_japanese():
    # The shipped default has a Japanese maprule; CJK terminators always break.
    ja = "今日は良い天気です。明日も晴れます。"
    out = srx.segment_text(ja, "ja-JP")
    assert len(out) == 2, out
    assert out[0].endswith("。") and out[1].endswith("。"), out
    # matches the legacy CJK splitter too
    assert out == segment.split_sentences(ja), out
    print("  per-language: ja text splits on the CJK terminator (2 segments) OK")


def test_paired_token_not_orphaned():
    """A sentence boundary INSIDE a paired inline-fidelity token span must be
    suppressed on the SRX path, exactly as the legacy path does."""
    settings.set_many({"srx_enabled": "1", "srx_file": ""})
    paired = "⟦g1⟧First sentence. Second half⟦/g1⟧ Third one is here."
    segs = segment.split_sentences(paired, "en-US")
    for s in segs:
        phs = segment.find_placeholders(s)
        opens = [p for p in phs if not p.startswith("⟦/")]
        closes = [p for p in phs if p.startswith("⟦/")]
        assert len(opens) == len(closes), f"orphaned pair in {s!r}"
    settings.set_many({"srx_enabled": "0"})
    print("  paired inline-fidelity token span never orphaned on SRX path OK")


# A custom ruleset that splits on ';' — distinct enough from both the legacy
# splitter and default.srx that a mis-stamped export would visibly misalign.
_SEMI_SRX = (
    '<srx xmlns="http://www.lisa.org/srx20"><body><languagerules>'
    '<languagerule languagerulename="X"><rule break="yes">'
    "<beforebreak>;</beforebreak><afterbreak></afterbreak></rule>"
    "</languagerule></languagerules><maprules>"
    '<languagemap languagepattern=".*" languagerulename="X"/>'
    "</maprules></body></srx>"
)


def _make_doc_with_segments(paragraph, targets, seg_engine, seg_locale=""):
    """Build a segmented Document whose stored Segments are the per-sentence
    targets, stamped with ``seg_engine``. Returns (doc, locale). Mirrors what
    ingest writes so export.paragraph_texts_for can be exercised end to end."""
    skeleton = {
        "mode": TransMode.SEGMENTED,
        "seg_locale": seg_locale,
        "seg_engine": seg_engine,
        "units": [{"p_index": 0, "text": paragraph}],
    }
    with Session(engine) as s:
        # project_id is a real FK (document.project_id -> project.id): create a real
        # project and use its id instead of a hardcoded project_id=1 (which never
        # persisted on Postgres, where the FK is enforced).
        proj = Project(name="SRX doc project", source_locale="en-US")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(
            project_id=proj.id,
            filename="f.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            skeleton=skeleton,
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        loc = "de-DE"
        for k, tgt in enumerate(targets):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=loc,
                    order_index=k,
                    source_text=tgt,
                    target_text=tgt,
                )
            )
        s.commit()
        s.refresh(doc)
        return doc, loc


def test_f1_export_reproduces_ingest_segmentation():
    """F1: export must re-split with the segmentation IDENTITY stamped at ingest,
    NOT the live srx setting. Ingest a paragraph with SRX ON + a custom ';'
    ruleset, store one target per sentence, then flip srx OFF (and swap the
    ruleset) and export. The export re-split must match the INGEST-time split
    (3 pieces on ';'), so all 3 targets rejoin into the paragraph — not the
    live/legacy split (which would produce a different count and misalign)."""
    # 1) Ingest-time conditions: SRX on, custom ';' ruleset active.
    srx.custom_srx_path().write_bytes(_SEMI_SRX.encode())
    settings.set_many({"srx_enabled": "1", "srx_file": "segmentation.srx"})
    paragraph = "alpha;beta;gamma"
    ingest_pieces = segment.split_sentences(paragraph, "de-DE")  # engine=None (live)
    assert ingest_pieces == ["alpha;", "beta;", "gamma"], ingest_pieces
    seg_engine = srx.engine_identity(True)  # what ingest would stamp
    assert seg_engine.startswith("srx:"), seg_engine

    # Translate each stored segment distinctly so a misalignment is detectable.
    targets = ["A;", "B;", "C"]
    doc, loc = _make_doc_with_segments(paragraph, targets, seg_engine, "de-DE")

    # 2) Between ingest and export, the operator flips SRX OFF (the classic
    # corruption trigger). The custom ruleset FILE stays on disk so its hash still
    # resolves — export must reproduce the stamped SRX split, not the live legacy
    # one. (The file-swapped/removed case is the identity-lost path, covered by
    # test_f1_lost_ruleset_falls_back_to_legacy_with_warning.)
    settings.set_many({"srx_enabled": "0", "srx_file": ""})
    # Sanity: the LIVE split of the paragraph is now the legacy one (1 piece),
    # which would misalign 3 targets if export used the live setting.
    assert segment.split_sentences(paragraph, "de-DE") == ["alpha;beta;gamma"]

    # 3) Export re-splits using the STAMPED engine -> 3 pieces -> all targets used.
    with Session(engine) as s:
        d = s.get(Document, doc.id)
        mapping = export.paragraph_texts_for(s, d, loc)
    assert mapping["0"] == "A; B; C", (
        "export must reproduce ingest split (all 3 targets joined), got {!r}".format(mapping["0"])
    )
    print("  F1: export reproduces ingest-time SRX split via stamped engine (3/3 aligned)")


def test_f1_lost_ruleset_falls_back_to_legacy_with_warning():
    """F1: when the stamped custom ruleset is no longer available (hash mismatch),
    export must fall back to legacy re-segmentation (not silently mis-split with
    the default), so split_sentences with a gone-ruleset engine == legacy."""
    settings.set_many({"srx_enabled": "0", "srx_file": ""})
    gone_engine = "srx:deadbeef0000"  # a hash no available ruleset matches
    text = "One two three. Four five six."
    out = segment.split_sentences(text, "en-US", engine=gone_engine)
    assert out == segment.split_sentences(text, "en-US", engine="legacy"), out
    print("  F1: stamped-but-missing ruleset falls back to legacy (identity-lost path)")


def test_f2_redos_pathological_rule_is_bounded():
    """F2: a malicious admin (a+)+ beforebreak against a long line must NOT hang.
    With the bounded context window the match work is capped, so segmentation
    returns quickly. Also verifies the paragraph-length cap takes the legacy
    fallback path for an oversized paragraph."""
    evil = (
        '<srx xmlns="http://www.lisa.org/srx20"><body><languagerules>'
        '<languagerule languagerulename="X"><rule break="yes">'
        "<beforebreak>(a+)+$</beforebreak><afterbreak></afterbreak></rule>"
        "</languagerule></languagerules><maprules>"
        '<languagemap languagepattern=".*" languagerulename="X"/>'
        "</maprules></body></srx>"
    )
    doc = srx.parse(evil)
    # A long run of 'a' with a trailing non-match is the classic catastrophic
    # input for (a+)+. Bounded window -> completes fast.
    line = "a" * 5000 + "!"
    t0 = time.monotonic()
    spans = doc.segment_spans(line, "en-US")
    dt = time.monotonic() - t0
    assert dt < 2.0, f"pathological (a+)+ rule took {dt:.2f}s (ReDoS guard failed)"
    assert spans and spans[0][0] == 0 and spans[-1][1] == len(line)

    # Paragraph cap: a paragraph over the cap short-circuits to a single span
    # (segment_spans) and to legacy via segment_text (returns the text unsplit).
    huge = "x. " * (srx._MAX_SRX_PARAGRAPH // 2)  # comfortably over the cap
    assert len(huge) > srx._MAX_SRX_PARAGRAPH
    assert doc.segment_spans(huge, "en-US") == [(0, len(huge))], "cap span fallback"
    assert srx.segment_text(huge, "en-US") == [huge.strip()], "cap legacy fallback"
    print("  F2: (a+)+ pathological rule bounded (< 2s); paragraph cap -> legacy fallback")


def test_f3_repeated_substring_offset_fidelity():
    """F3: a doc whose segments share a repeated substring AND contain an inline
    paired-token span must segment via EXACT offsets (not str.find on stripped
    pieces), so the protected-span merge cannot mis-locate a repeat and orphan a
    token pair or corrupt the merge."""
    settings.set_many({"srx_enabled": "1", "srx_file": ""})
    # "Go. Go. ⟦g1⟧Go. Go⟦/g1⟧ Go." — 'Go.' repeats; the paired span straddles a
    # sentence boundary that must be re-merged. find() on stripped 'Go.' pieces
    # could match the wrong occurrence; exact offsets cannot.
    text = "Go. Go. ⟦g1⟧Go. Go⟦/g1⟧ Go."
    segs = segment.split_sentences(text, "en-US")
    # Every paired token must be balanced within each segment (no orphan).
    for sg in segs:
        phs = segment.find_placeholders(sg)
        opens = [p for p in phs if not p.startswith("⟦/")]
        closes = [p for p in phs if p.startswith("⟦/")]
        assert len(opens) == len(closes), f"orphaned pair in {sg!r} (offset mis-merge)"
    # The paired span's two halves must live together in exactly one segment.
    joined = [sg for sg in segs if "⟦g1⟧" in sg]
    assert len(joined) == 1 and "⟦/g1⟧" in joined[0], f"paired span split across segments: {segs!r}"
    settings.set_many({"srx_enabled": "0", "srx_file": ""})
    print("  F3: repeated-substring + paired-token span merges via exact offsets OK")


def _srx_with_before(before: str) -> bytes:
    """A minimal one-rule SRX whose only rule carries `before` as beforebreak."""
    return (
        '<srx xmlns="http://www.lisa.org/srx20"><body><languagerules>'
        '<languagerule languagerulename="X"><rule break="yes">'
        f"<beforebreak>{before}</beforebreak><afterbreak></afterbreak></rule>"
        "</languagerule></languagerules><maprules>"
        '<languagemap languagepattern=".*" languagerulename="X"/>'
        "</maprules></body></srx>"
    ).encode()


def test_red_team_finding02_rule_timeout_and_length_cap():
    """Red-team audit 2026-10-03 finding 02. (a) An uploaded rule whose match is
    catastrophic ((a|aa)+b against a long 'a' run - exponential even inside the
    bounded 200-char window) must NOT hang: the match hits the per-match timeout,
    segmentation falls back to the built-in default rules for that document, and a
    warning names the offending rule. (b) An upload whose rule pattern exceeds the
    length cap is rejected with a clear message."""
    # (b) upload-time rejection: 1 char over the cap is refused, message is clear.
    over = "a" * (srx._SRX_PATTERN_MAX_LEN + 1)
    ok, msg = srx.validate(_srx_with_before(over))
    assert not ok, "an over-length rule pattern must be rejected at upload"
    assert str(len(over)) in msg and str(srx._SRX_PATTERN_MAX_LEN) in msg, msg
    # (a) match-time timeout -> default fallback. A legal-but-catastrophic rule
    # parses fine (the guard fires when it RUNS), so it uploads first.
    evil = _srx_with_before("(a|aa)+b")
    ok, msg = srx.validate(evil)
    assert ok, f"a slow-but-valid rule must parse: {msg}"
    text = "Alpha. " + "a" * 300 + ". Bravo. Charlie."
    # baseline: what the shipped default ruleset yields while it is the active doc
    settings.set_many({"srx_enabled": "1", "srx_file": ""})
    expected = srx.segment_text(text, "en-US")
    assert len(expected) > 1, f"expected the default rules to split, got {expected!r}"
    # swap in the evil custom ruleset and segment under a log capture
    srx.custom_srx_path().write_bytes(evil)
    settings.set_many({"srx_file": "segmentation.srx"})
    msgs: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            msgs.append(record.getMessage())

    cap = _Cap()
    logging.getLogger("loc_tms").addHandler(cap)
    try:
        t0 = time.monotonic()
        out = srx.segment_text(text, "en-US")
        dt = time.monotonic() - t0
        # Gate fix: the REAL ingest path (segment.split_sentences -> _split_via_srx)
        # and the stamped export path must take the same default-rule fallback with
        # a warning, not a silent drop to the legacy splitter.
        expected_spans = srx.default_document().segment_spans(text, "en-US")
        n_before = len(msgs)
        t1 = time.monotonic()
        ingest = segment.split_sentences(text, "en-US")
        dt_ingest = time.monotonic() - t1
        assert dt_ingest < 2.0, f"ingest path took {dt_ingest:.2f}s"
        assert ingest == [text[a:b].strip() for a, b in expected_spans], ingest
        assert any("fell back to the built-in default" in m for m in msgs[n_before:]), msgs
        stamp = srx.engine_identity(True)
        assert srx.segment_spans_for_engine(text, "en-US", stamp) == expected_spans
        assert segment.split_sentences(text, "en-US", engine=stamp) == ingest
    finally:
        logging.getLogger("loc_tms").removeHandler(cap)
        srx.custom_srx_path().unlink(missing_ok=True)
        settings.set_many({"srx_enabled": "0", "srx_file": ""})
    assert dt < 2.0, f"timed-out rule took {dt:.2f}s (ReDoS guard failed)"
    assert out == expected, f"expected default-rule segmentation, got {out!r}"
    w = [m for m in msgs if "fell back to the built-in default" in m]
    assert w and "(a|aa)+b" in w[0], f"expected a warning naming the rule, got {msgs!r}"
    print(
        "  finding 02: catastrophic rule times out (< 2s) -> default fallback + warning;"
        " over-length pattern rejected at upload"
    )


def main():
    init_db()
    print("Verifying SRX 2.0 segmentation...")
    test_icu_translation()
    test_parse_break_nobreak_maprules()
    test_cascade()
    test_validate_rejects_invalid()
    test_default_srx_mirrors_legacy()
    test_gate_off_is_legacy_and_on_uses_srx()
    test_per_language_japanese()
    test_paired_token_not_orphaned()
    test_f1_export_reproduces_ingest_segmentation()
    test_f1_lost_ruleset_falls_back_to_legacy_with_warning()
    test_f2_redos_pathological_rule_is_bounded()
    test_f3_repeated_substring_offset_fidelity()
    test_red_team_finding02_rule_timeout_and_length_cap()
    # restore default state
    settings.set_many({"srx_enabled": "0", "srx_file": ""})
    print("SRX OK")


if __name__ == "__main__":
    main()
