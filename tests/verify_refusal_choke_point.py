"""Refusal CHOKE POINT + tripwire + tmx-import gate + historical scrub + the
flow-engine TITLE gap (SPEC-refusal-choke-point, arch-review Finding 2).

This suite verifies the five deliverables that convert the refusal CHECKLIST into a
SEAM:

  1. quality.commit_mt_output — the ONE sanctioned target_text writer: clean commit
     vs refusal quarantine, the provenance `source` on the qa_flag, the
     `already_refused` per-step authority, `set_status`, and QE/refusal coherence
     (no clobbered rationale).
  2. THE TRIPWIRE — a dumb grep + allowlist that FAILS if a `.target_text =`
     assignment (or a SegmentTM/FullTextTM construction) appears outside the
     sanctioned modules. This is the revert-catcher: a future connector author
     cannot silently bypass the seam. Positive controls prove the regex bites.
  3. tm.import_tmx gate — a hedged TARGET is skipped (skipped_refusal), never stored;
     position-anchored is_refusal holds (a mid-string marker is NOT skipped); kill
     switch OFF -> imported as before; surfaced on the /api import (web+CLI parity).
  4. tm_module.scrub_hedges — dry-run reports planted hedges in BOTH TM stores; a
     legit translation with a mid-string marker is NOT reported; --apply removes them
     + writes an audit row; a second dry-run is clean; Segment history untouched.
  5. flow.py stage-9 TITLE gap — a NEEDS_REVIEW title (a refusal hedge that retained
     text) must NOT reach the mock transport on UPDATE for any of the FIVE flows
     (zendesk / document360 / freshdesk / zoho / intercom). The four T2 flows
     (zendesk/document360/freshdesk/zoho) fall back to the SOURCE title (the same
     source-fallback the excluded BODY units get) -- full-object PUTs, no GET-merge.
     Intercom (T1, GET-merge-PUT of the whole translated_content map) instead OMITS
     the title key so the merge PRESERVES the existing target-locale title on update
     -- a source-title fallback there would reintroduce the D2 description-defect
     class (clobbering a previously-good target title).

Hermetic + FK enforcement ON; keyless (MockProvider). ASCII-only prints; final line
is 'verify_refusal_choke_point OK'.
"""

import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_chokepoint_")

import _flow_parity_fixtures as fx
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Document,
    FullTextTM,
    Project,
    Segment,
    SegmentTM,
    SegStatus,
)
from app.services import quality, refusal, tm, tm_module, translate
from app.services.connectors import store as cstore

# A canned conversational refusal (leads with the hedge -> is_refusal True).
HEDGE = (
    "I'm ready to translate. However, I don't see any source text. "
    "Please provide the actual source text you'd like me to translate."
)
assert refusal.is_refusal(HEDGE), "test fixture must itself be a detected refusal"


# =============================================================================
# helpers
# =============================================================================
def _mk_segment(session, *, source="Hello", status=SegStatus.NEW, qa_flags=None):
    """A committed Segment (with its owning Project+Document, for FK enforcement)."""
    p = Project(name="chokepoint")
    session.add(p)
    session.commit()
    d = Document(project_id=p.id, filename="d.docx")
    session.add(d)
    session.commit()
    seg = Segment(
        document_id=d.id,
        target_locale="es-MX",
        order_index=0,
        source_text=source,
        status=status,
        qa_flags=qa_flags,
    )
    session.add(seg)
    session.commit()
    return seg


# =============================================================================
# 1. the seam: quality.commit_mt_output
# =============================================================================
def test_seam():
    with Session(engine) as s:
        # clean commit
        seg = _mk_segment(s)
        out = quality.commit_mt_output(s, seg, "[es] Hello", source="mt")
        s.commit()
        assert out.committed and not out.flagged and out.tm_eligible, out
        assert seg.target_text == "[es] Hello" and seg.status == SegStatus.MT, seg

        # refusal quarantine: no target_text, NEEDS_REVIEW, refusal flag w/ source key
        seg2 = _mk_segment(s)
        out = quality.commit_mt_output(s, seg2, HEDGE, source="mt")
        s.commit()
        assert (not out.committed) and out.flagged and not out.tm_eligible, out
        assert not (seg2.target_text or "").strip(), (
            "hedge must not be committed",
            seg2.target_text,
        )
        assert seg2.status == SegStatus.NEEDS_REVIEW, seg2.status
        rf = [f for f in (seg2.qa_flags or []) if f.get("type") == "refusal"]
        assert rf and rf[0].get("source") == "mt", ("provenance source recorded", seg2.qa_flags)
        assert refusal.REASON in rf[0]["message"], rf[0]
    print("  1a. commit_mt_output: clean commit vs refusal quarantine (+ source provenance) OK")

    with Session(engine) as s:
        # already_refused forces quarantine even for clean text (per-step C1 authority)
        seg = _mk_segment(s)
        out = quality.commit_mt_output(
            s, seg, "totally clean translation", source="workflow", already_refused=True
        )
        s.commit()
        assert out.flagged and not (seg.target_text or "").strip(), (
            "already_refused must quarantine regardless of text",
            seg.target_text,
        )

        # set_status=False writes target_text but leaves status untouched (FULL_TEXT path)
        seg2 = _mk_segment(s, status=SegStatus.NEW)
        out = quality.commit_mt_output(s, seg2, "body", source="workflow", set_status=False)
        s.commit()
        assert out.committed and seg2.target_text == "body" and seg2.status == SegStatus.NEW, (
            "set_status=False must not change status",
            seg2.status,
        )
    print("  1b. commit_mt_output: already_refused authority + set_status=False OK")

    # kill switch OFF -> refusal written through, no flag (byte-identical to pre-guard)
    config.REFUSAL_GUARD_ENABLED = False
    try:
        with Session(engine) as s:
            seg = _mk_segment(s)
            out = quality.commit_mt_output(s, seg, HEDGE, source="mt")
            s.commit()
            assert out.committed and seg.target_text == HEDGE and seg.status == SegStatus.MT, seg
            assert not [f for f in (seg.qa_flags or []) if f.get("type") == "refusal"], seg.qa_flags
    finally:
        config.REFUSAL_GUARD_ENABLED = True
    print("  1c. commit_mt_output: kill switch OFF -> hedge written through, no flag OK")

    # QE + refusal coherence: a pre-existing QE flag must SURVIVE a refusal quarantine
    # (no clobbered rationale — spec non-goal / adversarial lens).
    with Session(engine) as s:
        qe_flag = {"type": "qe", "severity": "minor", "score": 55, "rationale": "borderline"}
        seg = _mk_segment(s, qa_flags=[qe_flag])
        quality.commit_mt_output(s, seg, HEDGE, source="mt")
        s.commit()
        types = {f.get("type") for f in (seg.qa_flags or [])}
        assert "qe" in types and "refusal" in types, ("QE flag must not be clobbered", seg.qa_flags)
        kept = [f for f in seg.qa_flags if f.get("type") == "qe"][0]  # noqa: RUF015
        assert kept["rationale"] == "borderline", ("QE rationale intact", kept)
    print("  1d. QE + refusal coherence: refusal quarantine keeps the QE flag (no clobber) OK")


# =============================================================================
# 2. THE TRIPWIRE (revert-catcher)
# =============================================================================
# Statement-LEADING assignment to <obj>.target_text (single OR tuple-unpack),
# excluding `==` comparisons and keyword args (`text=x.target_text, ...`, whose
# `.target_text` is preceded by `=` and therefore never at line start). Applied
# per STATEMENT FRAGMENT (see _line_statements below), not per raw line, so a
# compound `x = 1; seg.target_text = y` or a suffixed `if ok: seg.target_text = y`
# is still caught without losing the line-start anchor that spares reads/kwargs.
_ASSIGN_RE = re.compile(r"""^\s*[\w.\[\]"']*\.target_text\b\s*(?:,\s*[\w.\[\]"']+\s*)*=(?!=)""")
_SETATTR_RE = re.compile(r"""setattr\(\s*[^,]+,\s*['"]target_text['"]""")
_TMROW_RE = re.compile(r"\b(?:SegmentTM|FullTextTM)\s*\(")
# SQLAlchemy Core bulk writer: `update(Model)....values(target_text=..., ...)` or
# `.values({Model.target_text: ...})` / `.values({"target_text": ...})` never goes
# through a Python attribute assignment at all (Finding 2, LOW) -- gdpr.py already
# has this exact `update(...).where(...).values(...)` shape for OTHER columns, so a
# future bulk-writer touching target_text is realistic. Dumb same-line scan (values()
# calls are single-line in this codebase; see the gdpr.py control in test_tripwire).
_VALUES_RE = re.compile(r"""\.values\(\s*[^)]*\btarget_text\b""")


def _line_statements(line):
    """Dumb statement-wise split of a raw source line for tripwire scanning: no real
    parser, just split on `;` (compound statements: `x = 1; seg.target_text = y`)
    then on `: ` (colon-space -- peels off an `if ok:` / `else:` / `while ...:`
    prefix so a suffixed `if ok: seg.target_text = y` still lands as its own
    fragment). A stray split inside e.g. a dict literal's `"key": value,` is
    harmless: the still line/fragment-start-ANCHORED _ASSIGN_RE/_SETATTR_RE only
    fire on a genuine assignment fragment (proven by the negative controls below,
    which exercise exactly that dict-literal / kwarg / comparison shape)."""
    for part in line.split(";"):
        for frag in part.split(": "):  # noqa: UP028
            yield frag


def _line_has_target_text_writer(line):
    """True if ANY statement fragment on this raw line is a sanctioned-seam-
    bypassing target_text writer: a direct/tuple assignment, a setattr, or a
    SQLAlchemy Core `.values(...)` call naming target_text."""
    if _VALUES_RE.search(line):
        return True
    for frag in _line_statements(line):
        if _ASSIGN_RE.search(frag) or _SETATTR_RE.search(frag):
            return True
    return False


# Files where a `.target_text` assignment is SANCTIONED (each justified):
_TARGET_TEXT_ALLOW = {
    # THE choke point — the only fresh-MT / imported writer.
    os.path.join("app", "services", "quality.py"),
    # CAT editor HUMAN save (segment_save) — human text is content, never gated.
    os.path.join("app", "web", "documents.py"),
    os.path.join("app", "web", "documents_part3.py"),  # SRP split 2026-09-14: segment_save moved
    # Images (Beta) review-page region save (image_region_save) — the SAME human-
    # edit semantics as segment_save (target_text + EDITED + QE-flag clear), on the
    # image module's own surface. Human text is content, never gated; fresh MT for
    # image segments still flows through pretranslate_stream -> commit_mt_output.
    os.path.join("app", "web", "images.py"),
    # TM STORE module — writes SegmentTM/FullTextTM ROW.target_text (import_tmx /
    # ft_store / write_back). The refusal gate for TM text is at write time
    # (import gate + scrub + approve's C3), not on the row assignment itself.
    os.path.join("app", "services", "tm.py"),
    os.path.join("app", "services", "tm_part2.py"),  # SRP split 2026-09-15: write_back piece
    # NOTE (Phase-0 seam fix): content.py (apply_tm_to_document) and translate.py (the
    # segment + full-text TM 100%-match short-circuits) USED to write target_text
    # directly and were exempted here. They now ALL route their TM applies through
    # quality.commit_mt_output (source="tm"), so they carry NO direct target_text
    # assignment and are DELIBERATELY NOT in this allowlist — the tripwire now correctly
    # guards them, catching any future direct-write regression (the exact defect Phase 0
    # closed).
    # TM MANAGEMENT edit (tm_module.edit_entry) — an in-place edit of a SegmentTM
    # ROW.target_text. It joins the refusal perimeter EXPLICITLY: the new target runs
    # through quality.is_refusal_check (the SAME gate commit_mt_output / import_tmx
    # enforce) and a hedge raises TMEditRefused BEFORE the assignment, so an edit can
    # never be a bypass seam for text the write path would refuse (verify_tm_edit's
    # refusal test proves it). The gate is at edit-write time, like tm.py's own writers.
    os.path.join("app", "services", "tm_module.py"),
    # AI Tagging 1.5c ON-DEMAND repair (tag_llm_repair.repair_stored_segments) — an
    # in-place edit of a segment's EXISTING target_text, not fresh MT.
    #
    # Why this is not a bypass seam, and why it is the ONE case where re-running the
    # choke point would prove nothing: the rung's LOCK 2 (body_preserved) requires the
    # candidate to be token-for-word IDENTICAL to the text already in target_text once
    # `⟦⟧` tokens are stripped and whitespace is normalized. A repair therefore CANNOT
    # change, add, or remove a single word — so every WORD-level verdict the choke point
    # computes (is_refusal, looks_untranslated, DNT, glossary) is provably invariant
    # across the edit: it was already decided when this text was originally committed
    # through commit_mt_output, and no repair can change it. The only verdict a repair
    # CAN move is the tag one, and that is re-run explicitly with
    # `quality.tag_damage(seg.source_text, res.text)` immediately before the assignment
    # — a candidate that fails it is discarded and the segment is left untouched.
    # (The INLINE rung needs no exemption at all: it runs INSIDE commit_mt_output.)
    os.path.join("app", "services", "tag_llm_repair.py"),
    # KEY-BASED PROJECTS (SPEC-key-based-projects 3): keys.set_value is the one write
    # seam for a key's value, and it SPLITS on provenance rather than claiming a blanket
    # exemption:
    #   * origin="human" (the key grid, the CAT editor, `keys set`) assigns directly --
    #     the same human-save semantics as web/documents.py's segment_save above, which
    #     is the writer this one is modelled on. Human text is content, never gated.
    #   * EVERY other origin (a file import, an /api write, machine output) is handed to
    #     quality.commit_mt_output, so an imported .po whose msgstr is an LLM hedge is
    #     quarantined on the way in exactly as the same text on a document would be.
    # A blank value takes the human branch too, because clearing a value is not machine
    # output and there is nothing to gate -- but a blank NON-human write no longer counts
    # as the human confirmation that clears the `source_changed` flag (review A7).
    # THIS ENTRY IS WHOLE-FILE, which is the honest limit of this allowlist's shape: it
    # exempts every target_text assignment in a growing module, not just the two above.
    # The compensating control lives in the code -- `origin` is KEYWORD-REQUIRED and
    # validated against keys.ORIGINS, so a future writer that forgets to say where its
    # text came from is a TypeError at the call site rather than a silent slide into the
    # ungated branch (verify_keys_core [F7] pins that). tests/verify_keys_pipeline.py's
    # [D] group asserts the gated branch produces byte-identical outcomes to a document
    # segment, and [D2] is the real enforcement: it goes RED under a bypass mutation.
    os.path.join("app", "services", "keys.py"),
    os.path.join("app", "services", "keys_hub3.py"),  # SRP split 2026-09-15: key edit writer piece
}
# Files where constructing a TM ROW is sanctioned.
_TMROW_ALLOW = {
    os.path.join("app", "services", "tm.py"),  # the store constructors
    os.path.join("app", "services", "tm_part2.py"),  # SRP split 2026-09-15: ft_store piece
    os.path.join("app", "models.py"),  # the class definitions
}


def _iter_app_py():
    app_dir = os.path.join(_ROOT, "app")
    for root, _dirs, files in os.walk(app_dir):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(root, f)
                yield full, os.path.relpath(full, _ROOT)


def test_tripwire():
    # positive controls: the regex MUST bite known bypass forms (not vacuous).
    assert _ASSIGN_RE.search("    seg.target_text = hedge"), "single-assign control"
    assert _ASSIGN_RE.search("    seg.target_text, seg.status = a, b"), "tuple-assign control"
    assert _SETATTR_RE.search("setattr(seg, 'target_text', hedge)"), "setattr control"
    # negatives: a read / kwarg / comparison must NOT match.
    assert not _ASSIGN_RE.search('    "target": seg.target_text,'), "dict-value read"
    assert not _ASSIGN_RE.search("        text=sg.target_text, title=x"), "kwarg not an assign"
    assert not _ASSIGN_RE.search("    if seg.target_text == hedge:"), "comparison not an assign"
    print("  2a. tripwire regex: bites bypass forms, spares reads/kwargs/comparisons OK")

    # Finding 2 (LOW, hardening): the reviewer's four bypass forms the ORIGINAL
    # line-anchored-only scan missed. Positive controls run through the FULL
    # detector (_line_has_target_text_writer), which statement-splits + adds the
    # SQLAlchemy Core .values() pattern.
    assert _line_has_target_text_writer("    x = 1; seg.target_text = hedge"), (
        "compound-semicolon bypass control"
    )
    assert _line_has_target_text_writer("    if ok: seg.target_text = hedge"), (
        "if-suffixed bypass control"
    )
    assert _line_has_target_text_writer(
        "        s.exec(update(Segment).where(Segment.id == sid).values(target_text=hedge))"
    ), "SQLAlchemy Core update(...).values(target_text=...) bypass control"
    assert _line_has_target_text_writer(
        '        s.exec(update(Segment).where(Segment.id == sid).values({"target_text": hedge}))'
    ), "SQLAlchemy Core update(...).values({'target_text': ...}) dict-form control"
    # negative controls MUST still pass through the full detector (no new false
    # positives from the statement split or the .values() pattern).
    assert not _line_has_target_text_writer('    "target": seg.target_text,'), (
        "dict-value read (post-split) must not trigger"
    )
    assert not _line_has_target_text_writer("        text=sg.target_text, title=x"), (
        "kwarg (post-split) must not trigger"
    )
    assert not _line_has_target_text_writer("    if seg.target_text == hedge:"), (
        "comparison (post-split) must not trigger"
    )
    # the EXACT gdpr.py update(...).values(...) shape on OTHER columns must NOT
    # trigger the new .values() pattern — this is the realism control the finding
    # asked for (a future bulk-writer touching target_text is realistic BECAUSE this
    # exact shape already ships for other columns).
    assert not _line_has_target_text_writer(
        "            s.exec(update(model).where(column == email).values({column.key: tomb}))"
    ), "gdpr.py update(...).values(...) on a DYNAMIC other-column key must not trigger"
    assert not _line_has_target_text_writer("               .values(added_by=tomb))"), (
        "gdpr.py update(...).values(added_by=...) on another column must not trigger"
    )
    print(
        "  2a2. tripwire hardening: compound-semicolon / if-suffixed / SQLAlchemy "
        ".values() bypass forms caught; gdpr.py's other-column .values(...) spared OK"
    )

    tt_viol, row_viol = [], []
    for full, rel in _iter_app_py():
        with open(full, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if _line_has_target_text_writer(line):  # noqa: SIM102
                    if rel not in _TARGET_TEXT_ALLOW:
                        tt_viol.append(f"{rel}:{i}: {line.strip()}")
                if _TMROW_RE.search(line) and rel not in _TMROW_ALLOW:
                    row_viol.append(f"{rel}:{i}: {line.strip()}")
    assert not tt_viol, (
        "UNSANCTIONED target_text writer(s) — join the choke point "
        "(quality.commit_mt_output) or justify in the allowlist:\n" + "\n".join(tt_viol)
    )
    assert not row_viol, "TM row constructed outside tm.py/models.py:\n" + "\n".join(row_viol)
    print(
        "  2b. tripwire: no target_text writer / TM-row builder outside the allowlist "
        "(scanned app/, including gdpr.py's own update(...).values(...) calls) OK"
    )

    # prove the tripwire would CATCH a reverted seam: inject a synthetic bypass line
    # into an app module's text and re-scan (in memory — no file write).
    bypass = "    seg.target_text = res['text']  # bypassing the seam"
    caught = any(_line_has_target_text_writer(l) for l in ["def f():", bypass, "    return seg"])  # noqa: E741
    assert caught, "tripwire failed to catch a synthetic bypass writer"
    print("  2c. tripwire revert-proof: a synthetic bypass writer IS caught by the scan OK")


# =============================================================================
# 3. tm.import_tmx refusal gate
# =============================================================================
def _tmx_bytes(pairs):
    tus = []
    for src, tgt in pairs:
        tus.append(
            f'<tu><tuv xml:lang="en-US"><seg>{src}</seg></tuv>'
            f'<tuv xml:lang="es-MX"><seg>{tgt}</seg></tuv></tu>'
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<tmx version="1.4"><header srclang="en-US"/><body>' + "".join(tus) + "</body></tmx>"
    )
    return xml.encode("utf-8")


def test_tmx_gate():
    mid = "Una traduccion legitima que menciona there is no text a mitad de frase."
    assert not refusal.is_refusal(mid), "position-anchored: a mid-string marker is NOT a refusal"
    data = _tmx_bytes(
        [
            ("Hello world", "Hola mundo"),
            ("[Example] label", HEDGE),  # hedged target -> skipped
            ("Mid marker", mid),
        ]
    )  # legit mid-string -> imported
    with Session(engine) as s:
        p = Project(name="tmxproj")
        s.add(p)
        s.commit()
        pid = p.id
        res = tm.import_tmx(s, pid, data, "en-US", "es-MX")
        assert res["imported"] == 2 and res["skipped_refusal"] == 1, res
        # the hedge is NOT stored (so it can never re-serve as a guard-bypassing 100%)
        assert tm.lookup(s, pid, "[Example] label", "en-US", "es-MX") is None, (
            "hedge must not be stored"
        )
        # the clean + mid-string units ARE stored
        assert tm.lookup(s, pid, "Hello world", "en-US", "es-MX")["match_pct"] == 100
        assert tm.lookup(s, pid, "Mid marker", "en-US", "es-MX")["match_pct"] == 100
    print("  3a. import_tmx: hedged target skipped (skipped_refusal), mid-string FP imported OK")

    # kill switch OFF -> the hedge imports exactly as before this gate existed.
    config.REFUSAL_GUARD_ENABLED = False
    try:
        with Session(engine) as s:
            p = Project(name="tmxkill")
            s.add(p)
            s.commit()
            res = tm.import_tmx(s, p.id, data, "en-US", "es-MX")
            assert res["skipped_refusal"] == 0 and res["imported"] == 3, res
            assert tm.lookup(s, p.id, "[Example] label", "en-US", "es-MX") is not None
    finally:
        config.REFUSAL_GUARD_ENABLED = True
    print("  3b. import_tmx: kill switch OFF -> hedge imported (byte-identical to pre-gate) OK")

    # web PARITY: the web import form (the UI surface; the CLI uses the same service)
    # surfaces skipped_refusal on its redirect back to the project page.
    client = TestClient(app)
    with Session(engine) as s:
        p = Project(name="tmxweb")
        s.add(p)
        s.commit()
        pid = p.id
    r = client.post(
        f"/projects/{pid}/tm/import",
        data={"source_locale": "en-US", "target_locale": "es-MX"},
        files={"file": ("x.tmx", data, "application/xml")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "tm_skipped_refusal=1" in loc and "tm_imported=2" in loc, loc
    print(
        "  3c. import_tmx: web import form surfaces skipped_refusal on redirect (web+CLI parity) OK"
    )


# =============================================================================
# 4. tm_module.scrub_hedges (historical remediation)
# =============================================================================
def test_scrub():
    mid = "Legit target con there is no text a mitad, no debe borrarse."
    with Session(engine) as s:
        p = Project(name="scrubproj")
        s.add(p)
        s.commit()
        pid = p.id
        # plant: 1 hedged SegmentTM, 1 hedged FullTextTM, 1 legit SegmentTM (FP guard)
        tm.write_back(s, pid, "seg hedge src", HEDGE, "en-US", "es-MX")
        tm.write_back(s, pid, "legit src", mid, "en-US", "es-MX")
        tm.ft_store(s, pid, "ft hedge src", HEDGE, "en-US", "es-MX")

        def _seg_rows():
            return s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()

        def _ft_rows():
            return s.exec(select(FullTextTM).where(FullTextTM.project_id == pid)).all()

        assert len(_seg_rows()) == 2 and len(_ft_rows()) == 1, (len(_seg_rows()), len(_ft_rows()))

        # scoped to this project so earlier sections' TM rows don't leak in.
        dry = tm_module.scrub_hedges(s, apply=False, project_id=pid)
        assert dry["matched"] == 2 and dry["deleted"] == 0, dry
        kinds = {h["kind"] for h in dry["segment"] + dry["full_text"]}
        assert kinds == {"segment", "full_text"}, kinds
        # the legit mid-string row is NOT among the matches
        assert all(mid[:20] not in h["sample"] for h in dry["segment"]), (
            "legit row not matched",
            dry,
        )
        # nothing deleted on a dry run
        assert len(_seg_rows()) == 2, "dry run must not delete"

        applied = tm_module.scrub_hedges(s, apply=True, actor="tester", project_id=pid)
        assert applied["deleted"] == 2, applied
        # only the legit row survives; the FT hedge is gone
        seg_left = _seg_rows()
        assert len(seg_left) == 1 and seg_left[0].target_text == mid, [
            x.target_text for x in seg_left
        ]
        assert _ft_rows() == [], "FT hedge must be deleted"

        # a second dry run is clean
        assert tm_module.scrub_hedges(s, apply=False, project_id=pid)["matched"] == 0, (
            "second dry run must be clean"
        )

        # audit row written for the apply
        rows = s.exec(select(AuditLog).where(AuditLog.action == "tm.scrub_hedges")).all()
        assert rows and rows[-1].actor == "tester", ("scrub apply must be audit-logged", rows)
    print(
        "  4. scrub_hedges: dry-run reports both stores, mid-string FP spared, --apply removes "
        "+ audit, second dry-run clean OK"
    )


# =============================================================================
# 5. flow.py stage-9 TITLE gap (all 5 flows)
# =============================================================================
class FakeIntercom:
    """Minimal Intercom Help Center tenant (T1 FieldMapStrategy); records PUTs."""

    def __init__(self, articles=None):
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        self.puts = []

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, req):
        path, method = req.url.path, req.method
        if method == "GET" and path == "/articles":
            data = list(self.articles.values())
            return httpx.Response(
                200,
                json={
                    "type": "list",
                    "total_count": len(data),
                    "pages": {"type": "pages", "next": None},
                    "data": data,
                },
            )
        if path.startswith("/articles/") and path.count("/") == 2:
            aid = path.rsplit("/", 1)[-1]
            if method == "GET":
                art = self.articles.get(aid)
                return httpx.Response(200, json=art) if art else httpx.Response(404, json={})
            if method == "PUT":
                tc = json.loads(req.content).get("translated_content") or {}
                self.puts.append((aid, tc))
                if aid in self.articles:
                    self.articles[aid]["translated_content"] = dict(tc)
                return httpx.Response(200, json=self.articles.get(aid, {"id": aid}))
        if method == "GET" and path == "/help_center/collections":
            return httpx.Response(200, json={"type": "list", "data": [], "pages": {"next": None}})
        return httpx.Response(404, json={"errors": [{"code": "not_found", "path": path}]})


def _ic_article(aid, title="Reset password"):
    return {
        "id": str(aid),
        "type": "article",
        "title": title,
        "description": "How to reset",
        "body": "<h1>Reset</h1><p>Click the link.</p>",
        "author_id": "55",
        "state": "published",
        "default_locale": "en",
        "updated_at": 1_700_000_000,
        "help_center_id": None,
        "translated_content": {
            "type": "article_translated_content",
            "de": {"title": "old de", "body": "<p>x</p>", "author_id": "55"},
            "fr": {"title": "old fr", "body": "<p>x</p>", "author_id": "55"},
        },
    }


def _hedge_title_stub(session, doc_id, loc, *a, **k):
    """Stand-in for translate.pretranslate_document: fill the TITLE (order 0) as a
    NEEDS_REVIEW hedge that RETAINED text (the defensive case the fix protects — a
    refusal withholds text, but a NEEDS_REVIEW-with-text unit is exactly what the
    title read-path must still exclude), and every BODY unit as a clean MT target."""
    segs = session.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == loc)
    ).all()
    for sg in segs:
        if sg.order_index == 0:
            sg.target_text = HEDGE
            sg.status = SegStatus.NEEDS_REVIEW
            sg.qa_flags = refusal.merge_flag(sg.qa_flags, refusal.refusal_flag("mt"))
        else:
            sg.target_text = f"[{loc}] {sg.source_text}"
            sg.status = SegStatus.MT
        session.add(sg)
    session.commit()
    return {}


def _assert_no_hedge_title(label, titles, source_title="Reset password", stored_title=None):
    """stored_title: a connector that keeps the STORED translation for a held title on an
    update (freshdesk, rebuilt 2026-10-10: taxonomy B, #640) never falls back to the source."""
    assert titles, (label, "no titles were written to the wire")
    for t in titles:
        assert HEDGE not in (t or ""), (label, "a refusal-flagged TITLE reached the wire", t)
        if stored_title is not None:
            assert t == stored_title, (label, "title must keep the STORED translation", t)
        else:
            assert t == source_title, (label, "title must fall back to the SOURCE title", t)


def test_title_gap():
    orig = translate.pretranslate_document
    set_pieces("app.services.translate", "pretranslate_document", _hedge_title_stub)
    try:
        # --- zendesk (UPDATE: pre-existing translation) ---
        fz = fx.FakeZendesk(articles=[fx._zd_article("100")])
        fz.add_translation("100", "de", title="Gespeichert")
        fz.add_translation("100", "fr", title="Enregistre")
        c = fx._zd_conn("ZD title")
        cstore.zendesk_translate(c.id, actor="auto", transport=fz.transport())
        # Zendesk re-sends a held title with its STORED value (never the refusal or the
        # source), which is stronger than the source-title fallback the other connectors use
        # and safe whether the translation PUT merges or replaces.
        assert fz.updates and not fz.creates, (fz.updates, fz.creates)
        for _a, loc, b in fz.updates:
            stored_title = {"de": "Gespeichert", "fr": "Enregistre"}[loc]
            assert b.get("title") == stored_title and HEDGE not in str(b), ("zendesk", loc, b)

        # --- document360 (UPDATE: pre-existing versions) ---
        fd = fx.FakeD360(articles=[fx._d360_md_article("100")])
        fd.add_version("100", "de", title="old", content="x")
        fd.add_version("100", "fr", title="old", content="x")
        c = fx._d360_conn("D360 title")
        cstore.document360_translate(c.id, actor="auto", transport=fd.transport())
        # A held (hedged) title is OMITTED from the partial PUT, so the stored title survives
        # (stronger than the source-title fallback the other T2 connectors use).
        assert fd.writes and all(
            "title" not in b and HEDGE not in str(b) for _a, _l, b in fd.writes
        ), fd.writes
        assert fd.latest("100", "de")["title"] == "old", "stored title kept, not the source"

        # --- freshdesk (UPDATE) ---
        ff = fx.FakeFreshdesk(articles=[fx._fd_article("100")])
        ff.add_translation("100", "fr")
        ff.add_translation("100", "de")
        c = fx._fd_conn("FD title")
        cstore.freshdesk_translate(c.id, actor="auto", transport=ff.transport())
        _assert_no_hedge_title(
            "freshdesk", [b["title"] for _a, _l, _m, b in ff.writes], stored_title="t"
        )

        # --- zoho (UPDATE) ---
        fzo = fx.FakeZoho(articles=[fx._zo_article("100")])
        fzo.add_translation("100", "de")
        fzo.add_translation("100", "fr")
        c = fx._zo_conn("ZO title")
        cstore.zoho_translate(c.id, actor="auto", transport=fzo.transport())
        # Zoho PATCHes only what is translated: a refusal-held title is OMITTED, so the stored
        # translation title stays (rebuilt 2026-10-10: taxonomy B, #640).
        assert fzo.writes, "zoho wrote nothing"
        for _a, _l, _m, b in fzo.writes:
            assert "title" not in b and HEDGE not in str(b), ("zoho", b)

        # --- intercom (UPDATE: pre-seeded translated_content keys) ---
        fi = FakeIntercom(articles=[_ic_article("100")])
        c = cstore.create(
            "intercom",
            "IC title",
            {"region": "US", "source_locale": "en", "target_locales": "de, fr", "author_id": "55"},
            {"access_token": "tok-123"},
            actor="admin",
        )
        cstore.intercom_translate(c.id, actor="auto", transport=fi.transport())
        # intercom GET-merge-PUTs the WHOLE translated_content map, so an intermediate
        # PUT snapshot legitimately still carries the OTHER locale's not-yet-updated
        # title — assert on the FINAL map (each written locale) + hedge-absence across
        # every PUT.
        assert fi.puts, ("intercom", "no PUT written")
        for _aid, tc in fi.puts:
            for blk in tc.values():
                if isinstance(blk, dict):
                    assert HEDGE not in (blk.get("title") or ""), (
                        "intercom",
                        "a refusal-flagged TITLE reached the wire",
                        blk,
                    )
        # intercom is T1 (per-key `block.update(fields)` merge onto the EXISTING
        # block, strategy.FieldMapStrategy.write_target) — a NEEDS_REVIEW title must
        # PRESERVE the pre-existing "old de"/"old fr" target title, NOT be clobbered
        # by the source title (that would be the D2 description-defect class,
        # reintroduced for title). This is the opposite assertion from the four T2
        # flows above, which correctly source-fallback because they full-object PUT.
        final_tc = fi.articles["100"]["translated_content"]
        assert final_tc["de"]["title"] == "old de", (
            "intercom",
            "NEEDS_REVIEW title must PRESERVE the existing target title "
            "on update, not be clobbered by the source title",
            final_tc["de"],
        )
        assert final_tc["fr"]["title"] == "old fr", (
            "intercom",
            "NEEDS_REVIEW title must PRESERVE the existing target title "
            "on update, not be clobbered by the source title",
            final_tc["fr"],
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    print(
        "  5. flow stage-9 TITLE gap CLOSED for all 5 flows: the hedge never reaches the wire "
        "(update) -- T2 (zendesk/document360/freshdesk/zoho) falls back to the SOURCE title, "
        "T1 (intercom) PRESERVES the existing target title via GET-merge-PUT OK"
    )


def main():
    init_db()
    test_seam()
    test_tripwire()
    test_tmx_gate()
    test_scrub()
    test_title_gap()
    print("verify_refusal_choke_point OK")


if __name__ == "__main__":
    main()
