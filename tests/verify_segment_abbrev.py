"""The default sentence splitter does not break after abbreviations (decision 15, 2026-10-11).

Soak finding: the built-in splitter broke after every "." followed by a space and a capital
or digit, so "Max. 5 files", "Call us, ext. 42." and "incl. VAT" reached the engine as
fragments (Japanese came back with a full stop mid-sentence). Version 2 of the splitter
(stamp "legacy:2") holds those breaks; documents imported before keep version 1 (stamp
"legacy", or no stamp at all) so their stored segments still line up on export.

  rules     version 2 holds after e.g. / incl. / ext. / Max. 5 / Mr. / initials /
            z. B. / p. ex. / Sra.; still breaks after a real sentence end ("etc. Next",
            "I met Max. He", "So did I. Then", "EE. UU. Desde")
  v1        the "legacy" stamp still splits exactly as before (fragments and all)
  srx       the shipped default.srx carries the same rules (parity on the corpus and
            rule-for-rule with segment._ABBREV_RULES); default_v1.srx is unchanged
  ingest    a new upload is stamped "legacy:2" and stored abbreviation-aware
  old docs  a document imported under version 1 ("legacy" stamp, a pre-stamp document,
            and an "srx:<v1 default hash>" document) re-exports with its own split:
            every stored target lands in its own place
  custom    a custom SRX ruleset still overrides the built-in rules

Hermetic + offline. Final line: "SEGMENT ABBREV OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_abbrev_")

import contextlib
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Document, Segment
from app.services import export, ingest, segment, settings, srx

init_db()
_client = TestClient(app)
_N = [0]

V2 = "legacy:2"

# (text, locale, version 2 split). Each case also asserts the version 1 split differs
# where the old splitter broke inside an abbreviation.
HOLDS = [
    (
        "Max. 5 files can be uploaded. Then wait.",
        "en-US",
        ["Max. 5 files can be uploaded.", "Then wait."],
    ),
    (
        "Call our office, ext. 42. We answer fast.",
        "en-US",
        ["Call our office, ext. 42.", "We answer fast."],
    ),
    (
        "All prices incl. VAT. Shipping is extra.",
        "en-US",
        ["All prices incl. VAT.", "Shipping is extra."],
    ),
    ("Use a tool, e.g. Python. It works.", "en-US", ["Use a tool, e.g. Python.", "It works."]),
    ("Pick one, i.e. The first. Done.", "en-US", ["Pick one, i.e. The first.", "Done."]),
    (
        "Mr. Smith met Dr. Jones. They talked.",
        "en-US",
        ["Mr. Smith met Dr. Jones.", "They talked."],
    ),
    ("Apples vs. Oranges. Done.", "en-US", ["Apples vs. Oranges.", "Done."]),
    ("See No. 7 and p. 12. Fine.", "en-US", ["See No. 7 and p. 12.", "Fine."]),
    ("It costs approx. 40 USD. Pay now.", "en-US", ["It costs approx. 40 USD.", "Pay now."]),
    ("J. R. R. Tolkien wrote it. Read it.", "en-US", ["J. R. R. Tolkien wrote it.", "Read it."]),
    (
        "John F. Kennedy spoke. J. A. Smith too.",
        "en-US",
        ["John F. Kennedy spoke.", "J. A. Smith too."],
    ),
    ("Ask Mr. Smith. Done.", "en-US", ["Ask Mr. Smith.", "Done."]),
    ("Das gilt z. B. Montags. Danke.", "de-DE", ["Das gilt z. B. Montags.", "Danke."]),
    ("Rot bzw. Blau. Danke.", "de-DE", ["Rot bzw. Blau.", "Danke."]),
    ("Bitte S. 5 lesen. Danke.", "de-DE", ["Bitte S. 5 lesen.", "Danke."]),
    ("Il vient, p. ex. Lundi. Merci.", "fr-FR", ["Il vient, p. ex. Lundi.", "Merci."]),
    ("M. Dupont arrive. Merci.", "fr-FR", ["M. Dupont arrive.", "Merci."]),
    ("La Sra. Lopez llega. Gracias.", "es-ES", ["La Sra. Lopez llega.", "Gracias."]),
    ("Vive en EE. UU. desde 2020.", "es-ES", ["Vive en EE. UU. desde 2020."]),
]

# Real sentence ends that must STILL split under version 2 (no false merge).
BREAKS = [
    (
        "We sell apples, pears, etc. Next week we add plums.",
        "en-US",
        ["We sell apples, pears, etc.", "Next week we add plums."],
    ),
    ("I met Max. He left.", "en-US", ["I met Max.", "He left."]),
    ("So did I. Then I left.", "en-US", ["So did I.", "Then I left."]),
    ("The answer is No. Then we stop.", "en-US", ["The answer is No.", "Then we stop."]),
    ("Vive en EE. UU. Desde 2020 trabaja.", "es-ES", ["Vive en EE. UU.", "Desde 2020 trabaja."]),
    ("Ask Dr. Smith. Everyone does.", "en-US", ["Ask Dr. Smith.", "Everyone does."]),
    # German-only rules stay German: "bzw." is not an English abbreviation, so en-US
    # keeps the break (an unknown word followed by a capital is a sentence end).
    ("Order bzw. Then go.", "en-US", ["Order bzw.", "Then go."]),
    # Gate fixes: titles are case-sensitive (units and codes that look like them end
    # sentences), and an initial followed by a sentence opener is a real end.
    ("The timeout is 500 ms. Then retry.", "en-US", ["The timeout is 500 ms.", "Then retry."]),
    ("I live in CA. Then I moved.", "en-US", ["I live in CA.", "Then I moved."]),
    ("It ships next-gen. The rest waits.", "en-US", ["It ships next-gen.", "The rest waits."]),
    ("Il mesure 5 mm. Ensuite on coupe.", "fr-FR", ["Il mesure 5 mm.", "Ensuite on coupe."]),
    ("Plan B. Then we go.", "en-US", ["Plan B.", "Then we go."]),
    ("Take vitamin C. The rest is fine.", "en-US", ["Take vitamin C.", "The rest is fine."]),
    ("Use option A. A new window opens.", "en-US", ["Use option A.", "A new window opens."]),
    ("Gehe zu Punkt B. Dann weiter.", "de-DE", ["Gehe zu Punkt B.", "Dann weiter."]),
]


def test_rules():
    for text, loc, want in HOLDS + BREAKS:
        got = segment.split_sentences(text, loc, engine=V2)
        assert got == want, (text, loc, got)
        # the live path (gate off) is version 2 too
        assert segment.split_sentences(text, loc) == want, (text, loc)
    # the soak sentences really did break under version 1
    v1 = segment.split_sentences(HOLDS[0][0], "en-US", engine="legacy")
    assert v1 == ["Max.", "5 files can be uploaded.", "Then wait."], v1
    print(
        f"  [rules] version 2 holds after {len(HOLDS)} abbreviations, "
        f"{len(BREAKS)} real ends still split OK"
    )


def test_v1_stamp_unchanged():
    cases = {
        "Max. 5 files can be uploaded. Then wait.": [
            "Max.",
            "5 files can be uploaded.",
            "Then wait.",
        ],
        "Call our office, ext. 42. We answer fast.": [
            "Call our office, ext.",
            "42.",
            "We answer fast.",
        ],
        "All prices incl. VAT. Shipping is extra.": [
            "All prices incl.",
            "VAT.",
            "Shipping is extra.",
        ],
        "Dr. Smith went home. He was tired.": ["Dr.", "Smith went home.", "He was tired."],
    }
    for text, want in cases.items():
        got = segment.split_sentences(text, "en-US", engine="legacy")
        assert got == want, (text, got)
    print("  [v1] the 'legacy' stamp splits exactly as before OK")


def _srx_rules(doc_bytes: bytes) -> dict[str, list[tuple[str, str, bool]]]:
    root = ET.fromstring(doc_bytes)
    ns = {"s": "http://www.lisa.org/srx20"}
    out = {}
    for lr in root.iter("{http://www.lisa.org/srx20}languagerule"):
        rules = []
        for r in lr.findall("s:rule", ns):
            b = r.find("s:beforebreak", ns)
            a = r.find("s:afterbreak", ns)
            rules.append(
                (
                    (b.text or "") if b is not None else "",
                    (a.text or "") if a is not None else "",
                    r.get("break") == "yes",
                )
            )
        out[lr.get("languagerulename")] = rules
    return out


def test_srx_parity():
    rules = _srx_rules(srx.default_ruleset_bytes())
    lang_of = {
        "German": "de",
        "French": "fr",
        "Spanish": "es",
        "Portuguese": "pt",
        "Italian": "it",
        "Dutch": "nl",
    }
    want_default = [(b, a) for b, a, locs in segment._ABBREV_RULES if locs is None]
    got_default = [(b, a) for b, a, brk in rules["Default"] if not brk]
    assert got_default == want_default, got_default
    for name, code in lang_of.items():
        want = [(b, a) for b, a, locs in segment._ABBREV_RULES if locs and code in locs]
        got = [(b, a) for b, a, brk in rules[name] if not brk]
        assert got == want, (name, got)
    # every locale-specific rule in segment.py is carried by some SRX languagerule
    langs = {c for _, _, locs in segment._ABBREV_RULES if locs for c in locs}
    assert langs == set(lang_of.values()), langs
    # behaviour parity: the shipped SRX default splits like version 2
    doc = srx.default_document()
    for text, loc, want in HOLDS + BREAKS:
        got = [p.strip() for p in doc.segment(text, loc) if p.strip()]
        assert got == want, ("srx", text, loc, got)
    # the version 1 ruleset still ships, unchanged (no abbreviation rules)
    v1 = srx._DEFAULT_V1_SRX_PATH.read_bytes()
    assert b'break="no"' not in v1
    v1_doc = srx.parse(v1)
    got = [p.strip() for p in v1_doc.segment("Dr. Smith went home. He was tired.", "en-US")]
    assert got == ["Dr.", "Smith went home.", "He was tired."], got
    print("  [srx] default.srx = segment._ABBREV_RULES rule for rule; v1 ruleset kept OK")


def _project(src: str, tgt: str) -> int:
    _N[0] += 1
    r = _client.post(
        "/projects",
        data={
            "name": f"abbrev-{_N[0]}",
            "source_locale": src,
            "target_locales": tgt,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _upload(src: str, tgt: str, name: str, data: bytes) -> int:
    pid = _project(src, tgt)
    r = _client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name, data, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def _sources(doc_id: int, tgt: str) -> list[str]:
    with Session(engine) as s:
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == tgt)
            .order_by(Segment.order_index)
        ).all()
        return [g.source_text for g in segs]


def _export_bracketed(doc_id: int, tgt: str) -> bytes:
    """Translate every stored segment to "[<its source>]" and export."""
    with Session(engine) as s:
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == tgt)
            .order_by(Segment.order_index)
        ).all()
        for g in segs:
            g.target_text = "[" + g.source_text + "]"
            g.status = "mt"
            s.add(g)
        s.commit()
        return export.build_translated_file(s, s.get(Document, doc_id), tgt)


TXT = (
    "Max. 5 files can be uploaded. Then wait.\n\n"
    "Call our office, ext. 42. We answer fast.\n\n"
    "All prices incl. VAT. Shipping is extra.\n"
)


def test_ingest_new_document():
    doc_id = _upload("en-US", "ja-JP", "new.txt", TXT.encode())
    with Session(engine) as s:
        assert (s.get(Document, doc_id).skeleton or {}).get("seg_engine") == V2
    got = _sources(doc_id, "ja-JP")
    assert got == [
        "Max. 5 files can be uploaded.",
        "Then wait.",
        "Call our office, ext. 42.",
        "We answer fast.",
        "All prices incl. VAT.",
        "Shipping is extra.",
    ], got
    out = _export_bracketed(doc_id, "ja-JP").decode("utf-8")
    assert "[Max. 5 files can be uploaded.]" in out and "[Call our office, ext. 42.]" in out, out
    print("  [ingest] a new upload is stamped legacy:2 and split abbreviation-aware OK")


@contextlib.contextmanager
def _as_version_1():
    """Ingest the way the app did before decision 15: version 1 split, "legacy" stamp, and
    the version 1 shipped SRX ruleset."""
    saved = (ingest.LEGACY_ENGINE_V2, srx.engine_identity, srx._DEFAULT_SRX_PATH)
    ingest.LEGACY_ENGINE_V2 = "legacy"
    srx.engine_identity = lambda on: "legacy" if not on else saved[1](on)
    srx._DEFAULT_SRX_PATH = srx._DEFAULT_V1_SRX_PATH
    try:
        yield
    finally:
        ingest.LEGACY_ENGINE_V2, srx.engine_identity, srx._DEFAULT_SRX_PATH = saved


_V1_SOURCES = [
    "Max.",
    "5 files can be uploaded.",
    "Then wait.",
    "Call our office, ext.",
    "42.",
    "We answer fast.",
    "All prices incl.",
    "VAT.",
    "Shipping is extra.",
]
_V1_EXPORT = (
    "[Max.] [5 files can be uploaded.] [Then wait.]\n\n"
    "[Call our office, ext.] [42.] [We answer fast.]\n\n"
    "[All prices incl.] [VAT.] [Shipping is extra.]\n"
)


def test_old_documents_keep_their_split():
    # (a) "legacy" stamp: imported under version 1 with the SRX gate off
    with _as_version_1():
        doc_a = _upload("en-US", "ja-JP", "old.txt", TXT.encode())
    with Session(engine) as s:
        assert (s.get(Document, doc_a).skeleton or {}).get("seg_engine") == "legacy"
    assert _sources(doc_a, "ja-JP") == _V1_SOURCES, _sources(doc_a, "ja-JP")
    out = _export_bracketed(doc_a, "ja-JP").decode("utf-8")
    assert out == _V1_EXPORT, out

    # (b) a document from before the seg_engine stamp existed (no key at all)
    with _as_version_1():
        doc_b = _upload("en-US", "ja-JP", "older.txt", TXT.encode())
    with Session(engine) as s:
        d = s.get(Document, doc_b)
        skel = dict(d.skeleton or {})
        skel.pop("seg_engine", None)
        d.skeleton = skel
        s.add(d)
        s.commit()
    out = _export_bracketed(doc_b, "ja-JP").decode("utf-8")
    assert out == _V1_EXPORT, out

    # (c) imported with the SRX gate on and the version 1 shipped ruleset
    settings.set_many({"srx_enabled": "1", "srx_file": ""})
    try:
        with _as_version_1():
            doc_c = _upload("en-US", "ja-JP", "oldsrx.txt", TXT.encode())
        with Session(engine) as s:
            stamp = (s.get(Document, doc_c).skeleton or {}).get("seg_engine")
        v1_hash = srx._ruleset_hash(srx._DEFAULT_V1_SRX_PATH.read_bytes())
        assert stamp == "srx:" + v1_hash, stamp
        assert _sources(doc_c, "ja-JP") == _V1_SOURCES, _sources(doc_c, "ja-JP")
        out = _export_bracketed(doc_c, "ja-JP").decode("utf-8")
        assert out == _V1_EXPORT, out
        # ... and the same v1 stamp written with the other line-ending form resolves too
        raw = srx._DEFAULT_V1_SRX_PATH.read_bytes()
        other = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw.replace(b"\n", b"\r\n")
        assert srx._document_for_hash(srx._ruleset_hash(other)) is not None
        # a NEW upload with the gate on uses the version 2 default ruleset
        doc_d = _upload("en-US", "ja-JP", "newsrx.txt", TXT.encode())
        assert _sources(doc_d, "ja-JP")[:2] == ["Max. 5 files can be uploaded.", "Then wait."]
    finally:
        settings.set_many({"srx_enabled": "0", "srx_file": ""})
    print("  [old docs] legacy / unstamped / srx-v1 documents re-export with their own split OK")


def test_custom_ruleset_overrides():
    custom = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<srx xmlns="http://www.lisa.org/srx20" version="2.0">'
        b'<header segmentsubflows="yes" cascade="yes"/><body><languagerules>'
        b'<languagerule languagerulename="All"><rule break="yes">'
        b"<beforebreak>\\.</beforebreak><afterbreak>\\s</afterbreak></rule></languagerule>"
        b'</languagerules><maprules><languagemap languagepattern=".*" languagerulename="All"/>'
        b"</maprules></body></srx>"
    )
    srx.write_custom_ruleset(custom)
    settings.set_many({"srx_enabled": "1", "srx_file": "segmentation.srx"})
    try:
        got = segment.split_sentences("Max. 5 files. Then wait.", "en-US")
        assert got == ["Max.", "5 files.", "Then wait."], got
    finally:
        settings.set_many({"srx_enabled": "0", "srx_file": ""})
    print("  [custom] a custom ruleset still decides the split OK")


def main():
    test_rules()
    test_v1_stamp_unchanged()
    test_srx_parity()
    test_ingest_new_document()
    test_old_documents_keep_their_split()
    test_custom_ruleset_overrides()
    print("SEGMENT ABBREV OK")


if __name__ == "__main__":
    main()
