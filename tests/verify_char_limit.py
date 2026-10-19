"""K8 regression lock: `StringKey.char_limit` ENFORCED at the sole MT write seam.

Live QA campaign, the planning notes finding K8 + the maintainer's
call ("if there is a char_limit set for a key value pair then yes it should be enforced").
Spec: the SPEC-char-limit-enforcement spec. Before this wave `char_limit` was
stored, "validated" and displayed and checked on NO production path -- the only
implementation lived in `eval_metrics._qa_char_limit`, inside the Eval Lab, where it can
never reach a write.

WHAT THIS PINS (spec section 11):
  A  the UNCRASHABLE predicate: 0 / negative / None / a non-integer string / an absurd
     value all read as UNSET and never raise; chars-not-bytes; NFC code points (one
     verdict for precomposed vs decomposed); placeholders literal; whitespace verbatim;
     len == limit inclusive; the emoji-ZWJ code-point choice.
  B  the SEAM over a KEY value (keys.set_value machine path): over-limit -> text COMMITTED,
     char_limit flag, NEEDS_REVIEW, tm_eligible UNCHANGED (the deliberate deviation).
  C  the KILLER GOLDEN: a DOCUMENT segment (key_id None) is byte-identical AND issues ZERO
     StringKey queries; a dirty legacy limit (0 / -5 / "40" stored straight on the key)
     never raises and never flags a value that fits.
  D  plurals PER FORM: a ru key where only `many` is over -> exactly one of four flagged.
  E  the human/machine ASYMMETRY (spec 2a): a human over-limit write is flagged but STAYS
     `edited`; the machine write routes to review.
  F  a re-write within limit CLEARS the char_limit flag and leaves every other family.
  G  the REAL orchestrator over a key document: the char_limit flag is persisted by a live
     `run_workflow_stream` (the seam fires on the workflow path).
  H  the lifted Lab scorer delegates to `quality` (within / over / not-applicable / NFC).
  I  the EXPORT report: an over-limit value that ships gets a `char_limit` report entry,
     a notice line, and reaches export_headers/audit_detail; a document is unaffected.
  J  the READ-TIME derived `over_limit` (key_view) + the now-populated grid tooltip.
  K  ingestion HARDENING: create_key / apply_key_edit reject <=0 and non-integers and cap
     at CHAR_LIMIT_MAX (where the non-integers were entering).
  Z  FAIL-BEFORE via FILE COPY (never git stash -- shared stack):
     the pre-fix quality.py has no predicate and its seam does NOT flag an over-limit key
     value; the pre-fix create_key accepts a 0 char_limit. And a naive `len(text) > limit`
     TypeErrors on the reachable "40" string while the shipped predicate does not.

Hermetic (throwaway LOC_TMS_DATA_DIR, MockProvider, no keys, no network). ASCII-only
FAILURE MESSAGES (fixtures carry CJK / Devanagari / emoji and a Windows console would raise
UnicodeEncodeError on the report instead of showing the real failure); final line
'verify_char_limit OK'.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_k8_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Project,
    ProjectType,
    Segment,
    SegStatus,
    StepType,
    StringKey,
    Workflow,
)
from app.services import (
    content,
    eval_metrics,
    key_files,
    keys,
    orchestrator,
    quality,
)
from app.services import workflows as wf

init_db()

SRC = "en-US"
TGT = "de-DE"
#: The pre-fix commit (this branch's point off main) for the FILE-COPY fail-before.
PREFIX_COMMIT = "267815a"

_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _eq(a, b, msg):
    if a != b:
        _fail(f"{msg} -- got {a!r}, expected {b!r}")


# --- fixtures ----------------------------------------------------------------
def _key_project(target="de-DE"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            "K8-%d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=target,
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _make_key(project_id, name, *, char_limit=None, is_plural=False, base="Hi", locale="de-DE"):
    """Create a key and materialise its value row(s) for `locale`, returning the key id."""
    with Session(engine) as s:
        p = s.get(Project, project_id)
        k = keys.create_key(
            s,
            p,
            name,
            char_limit=char_limit,
            is_plural=is_plural,
            base_value=({"other": base} if is_plural else base),
        )
        return k.id


def _force_char_limit(key_id, raw):
    """Write a raw (possibly DIRTY) value straight onto StringKey.char_limit, bypassing
    the ingestion validators -- to reproduce the legacy rows section 0.5 makes reachable."""
    with Session(engine) as s:
        k = s.get(StringKey, key_id)
        k.char_limit = raw
        s.add(k)
        s.commit()


def _value_seg(key_id, locale=TGT, form=""):
    with Session(engine) as s:
        k = s.get(StringKey, key_id)
        segs = keys.values_for(s, k, locale)
        for seg in segs:
            if (seg.plural_category or "") == form:
                return seg.id
    return None


def _seg_snapshot(seg_id):
    with Session(engine) as s:
        seg = s.get(Segment, seg_id)
        return (seg.target_text, seg.status, list(seg.qa_flags or []))


def _has_flag(flags, ftype):
    return [f for f in (flags or []) if isinstance(f, dict) and f.get("type") == ftype]


# ============================================================================
# [A] THE UNCRASHABLE PREDICATE
# ============================================================================
def test_a_predicate():
    cv = quality.char_limit_violation
    # clean / over / inclusive boundary
    _eq(cv("hello", 10), "", "within limit must be clean")
    assert cv("hello world!!", 5), "over limit must produce a reason"
    _eq(cv("12345", 5), "", "len == limit is INCLUSIVE (matches _qa_char_limit)")
    assert cv("123456", 5), "len == limit+1 is over"
    # unset shapes -- NONE may raise
    for bad in (0, -5, None, "abc", "", 0.0):
        _eq(
            cv("hello world this is long", bad),
            "",
            f"a {bad!r} limit must read as UNSET, not flag, not raise",
        )
    # the reachable-today STRING defect: "40" is int-parseable, so it ENFORCES 40 (not a
    # crash), and a value within 40 is clean.
    _eq(cv("hello", "40"), "", "a numeric string limit coerces and this fits")
    assert cv("x" * 41, "40"), "a numeric string limit coerces and 41 > 40 fires"
    # chars NOT bytes: 10 CJK chars (30 UTF-8 bytes) pass a limit of 10.
    cjk = "中文测试字符串限制啊"  # 10 code points
    _eq(len(cjk), 10, "fixture must be 10 code points")
    _eq(cv(cjk, 10), "", "10 CJK chars vs limit 10 is chars, not the 30 bytes")
    assert cv(cjk + "!", 10), "11 CJK chars vs limit 10 fires"
    # NFC: precomposed e-acute and decomposed e+combining get the SAME verdict.
    pre, dec = "é", "é"
    _eq(quality.char_count(pre), quality.char_count(dec), "NFC must fold to one count")
    _eq(
        cv(pre, 1),
        cv(dec, 1),
        "precomposed and decomposed must get the SAME char_limit verdict (NFC)",
    )
    _eq(cv(pre, 1), "", "one grapheme under NFC fits a limit of 1")
    # Devanagari cluster counts as its code points (over-counts clusters -> soft gate).
    deva = "नि"  # ni: consonant + vowel sign = 2 code points, 1 cluster
    _eq(quality.char_count(deva), 2, "Devanagari na+vowel is 2 code points")
    _eq(cv(deva, 2), "", "Devanagari fits its code-point count")
    # placeholder counted LITERALLY ("{count} items" = 13 chars, not a rendered width).
    _eq(quality.char_count("{count} items"), 13, "placeholder counted as its literal text")
    _eq(cv("{count} items", 13), "", "{count} items is 13 chars, fits 13")
    assert cv("{count} items", 12), "{count} items over a limit of 12 fires"
    # whitespace verbatim -- no strip.
    assert cv("abcd  ", 5), "trailing whitespace pushing to 6 over a 5 limit fires"
    _eq(cv("abcde", 5), "", "control: 5 non-space chars fits 5")
    # emoji ZWJ family: 7 code points / 1 grapheme -> flagged vs limit 4 (pins code points).
    zwj = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"
    _eq(quality.char_count(zwj), 7, "the ZWJ family is 7 code points")
    assert cv(zwj, 4), "7 code points over a limit of 4 fires (pins the code-point choice)"
    print("  [A] predicate: uncrashable, chars-not-bytes, NFC, literal placeholders, code points")


# ============================================================================
# [B] THE SEAM over a KEY value (keys.set_value machine path)
# ============================================================================
def test_b_seam_flags_over_limit_key_value():
    pid = _key_project()
    kid = _make_key(pid, "nav::settings", char_limit=5, base="Settings")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Einstellungen und mehr", origin=keys.ORIGIN_IMPORT)
    text, status, flags = _seg_snapshot(_value_seg(kid))
    _eq(text, "Einstellungen und mehr", "the over-limit text must be COMMITTED, never dropped")
    _eq(status, SegStatus.NEEDS_REVIEW, "a machine over-limit write routes to review")
    cl = _has_flag(flags, quality.CHAR_LIMIT_FLAG_TYPE)
    assert cl, f"an over-limit machine write must carry a char_limit qa_flag: {flags!r}"
    _eq(cl[0].get("limit"), 5, "the flag records the limit")
    _eq(cl[0].get("length"), len("Einstellungen und mehr"), "the flag records the length")
    assert "Over character limit" in cl[0].get("message", ""), cl
    print("  [B1] machine over-limit key value: committed + char_limit flag + needs_review")


def test_b_tm_eligible_unchanged():
    """The DELIBERATE deviation (spec section 1): char_limit does NOT force TM-ineligibility
    -- an over-limit value is linguistically correct; the same sentence in a limit-free doc
    is a fine TM entry. Proven at the seam via the CommitOutcome."""
    pid = _key_project()
    kid = _make_key(pid, "nav::home", char_limit=3, base="Home")
    with Session(engine) as s:
        seg = s.get(Segment, _value_seg(kid))
        out = quality.commit_mt_output(s, seg, "Startseite", source="mt")
        assert out.char_limit_violation, "the outcome must carry the char_limit reason"
        assert out.tm_eligible, (
            "char_limit must leave tm_eligible TRUE -- the one deviation from the HARD gates"
        )
        assert out.committed, "the text is committed"
    print("  [B2] over-limit is TM-ELIGIBLE (correct text, wrong slot) -- the deviation")


def test_b_within_limit_is_clean():
    pid = _key_project()
    kid = _make_key(pid, "nav::ok", char_limit=20, base="OK")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Bestaetigen", origin=keys.ORIGIN_IMPORT)
    _text, status, flags = _seg_snapshot(_value_seg(kid))
    _eq(status, SegStatus.MT, "a within-limit machine write stays MT")
    assert not _has_flag(flags, quality.CHAR_LIMIT_FLAG_TYPE), (
        f"a within-limit value must carry NO char_limit flag: {flags!r}"
    )
    print("  [B3] within-limit machine value: clean, MT, no flag")


# ============================================================================
# [C] THE KILLER GOLDEN: document byte-identity + zero queries + dirty limits
# ============================================================================
def test_c_document_segment_is_byte_identical_and_issues_no_stringkey_query():
    """A DOCUMENT segment (key_id None) must be untouched by the gate AND must not even
    LOOK the key up. Proven by instrumenting Session.get to record StringKey lookups."""
    with Session(engine) as s:
        # A bare document-shaped segment: key_id is None, so the gate self-resolves to None.
        from app.models import Document, TransMode

        p = content.project_create(
            s,
            "K8doc-%d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=TGT,
            default_provider="mock",  # noqa: RUF100, UP031
        )
        doc = Document(
            project_id=p.id,
            filename="d.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hi"}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        seg = Segment(
            document_id=doc.id,
            target_locale=TGT,
            order_index=0,
            source_text="Hi",
            status=SegStatus.NEW,
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)

        lookups = []
        real_get = s.get

        def _watch_get(model, ident, *a, **kw):
            if model is StringKey:
                lookups.append(ident)
            return real_get(model, ident, *a, **kw)

        s.get = _watch_get
        try:
            out = quality.commit_mt_output(s, seg, "A perfectly ordinary target", source="mt")
        finally:
            s.get = real_get
        _eq(lookups, [], "a key_id-None segment must issue ZERO StringKey queries")
        _eq(out.char_limit_violation, "", "a document segment can never be over-limit")
        _eq(seg.status, SegStatus.MT, "a document segment commits clean MT")
        assert not _has_flag(seg.qa_flags, quality.CHAR_LIMIT_FLAG_TYPE), seg.qa_flags
    print("  [C1] document segment: byte-identical, ZERO StringKey queries (killer golden)")


def test_c_dirty_legacy_limits_never_crash_or_flag():
    for raw in (0, -5, "40", 10**9):
        pid = _key_project()
        kid = _make_key(pid, f"nav::dirty{_seq()}", base="Hi")
        _force_char_limit(kid, raw)
        with Session(engine) as s:
            k = s.get(StringKey, kid)
            # "kurz" is 4 chars: under a coerced 40 and 1e9, and a 0/-5 read as unset.
            keys.set_value(s, k, TGT, "kurz", origin=keys.ORIGIN_IMPORT)
        text, _status, flags = _seg_snapshot(_value_seg(kid))
        _eq(text, "kurz", f"the value must land unharmed under a {raw!r} limit")
        assert not _has_flag(flags, quality.CHAR_LIMIT_FLAG_TYPE), (
            f"a 4-char value must not flag under a {raw!r} limit: {flags!r}"
        )
    print('  [C2] dirty legacy limits (0, -5, "40", 1e9): no crash, no false flag')


# ============================================================================
# [D] PLURALS: per form
# ============================================================================
def test_d_plural_only_one_form_flagged():
    pid = _key_project(target="ru-RU")
    kid = _make_key(pid, "cart::items", char_limit=6, is_plural=True, base="items", locale="ru-RU")
    forms = keys.plural_categories_for("ru-RU")  # one, few, many, other
    assert "many" in forms and len(forms) == 4, forms
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        # only `many` is over 6; the other three fit.
        texts = {"one": "1", "few": "2-4", "many": "многотоваров", "other": "много"}
        for form, txt in texts.items():
            keys.set_value(s, k, "ru-RU", txt, origin=keys.ORIGIN_IMPORT, plural_category=form)
    flagged = []
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        for seg in keys.values_for(s, k, "ru-RU"):
            if _has_flag(seg.qa_flags, quality.CHAR_LIMIT_FLAG_TYPE):
                flagged.append(seg.plural_category)
    _eq(sorted(flagged), ["many"], "exactly ONE of four plural forms (many) must flag")
    print("  [D1] ru plural: only the over-limit `many` form flags (per form, not longest)")


# ============================================================================
# [E] HUMAN / MACHINE asymmetry (spec 2a)
# ============================================================================
def test_e_human_write_flags_but_stays_edited():
    pid = _key_project()
    kid = _make_key(pid, "nav::human", char_limit=4, base="Save")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Speichern", origin=keys.ORIGIN_HUMAN)
    text, status, flags = _seg_snapshot(_value_seg(kid))
    _eq(text, "Speichern", "human text is committed verbatim")
    _eq(status, SegStatus.EDITED, "a HUMAN over-limit write must STAY edited (spec 2a)")
    assert _has_flag(flags, quality.CHAR_LIMIT_FLAG_TYPE), (
        f"a human over-limit write must still carry the char_limit flag: {flags!r}"
    )
    print("  [E1] human over-limit write: flagged for the grid/export, status stays edited")


# ============================================================================
# [F] STALE-BADGE CLEARING
# ============================================================================
def test_f_rewrite_within_limit_clears_only_char_limit():
    pid = _key_project()
    kid = _make_key(pid, "nav::clear", char_limit=5, base="Home")
    seg_id = _value_seg(kid)
    with Session(engine) as s:
        # 1) machine over-limit -> char_limit flag + a co-resident family (source_changed)
        seg = s.get(Segment, seg_id)
        seg.qa_flags = keys.merge_source_changed_flag(seg.qa_flags)
        s.add(seg)
        s.commit()
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Startseite lang", origin=keys.ORIGIN_IMPORT)
    _, _, flags = _seg_snapshot(seg_id)
    assert _has_flag(flags, quality.CHAR_LIMIT_FLAG_TYPE), "precondition: over-limit flagged"
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "kurz", origin=keys.ORIGIN_IMPORT)
    _, _status, flags = _seg_snapshot(seg_id)
    assert not _has_flag(flags, quality.CHAR_LIMIT_FLAG_TYPE), (
        f"a re-write within limit must CLEAR the char_limit flag: {flags!r}"
    )
    print("  [F1] re-write within limit clears the char_limit flag (stale-badge rule)")


# ============================================================================
# [G] THE REAL ORCHESTRATOR over a key document
# ============================================================================
def test_g_real_workflow_run_persists_the_flag():
    """A live `run_workflow_stream` over a key document with a tiny limit: MockProvider's
    echo overflows it, so the char_limit flag is persisted AND the segment is routed to
    NEEDS_REVIEW. The status half is the orchestrator write-back re-assertion (reviewer H2):
    without it `--status needs_review` would DROP the over-limit value, the grid would show
    a char_limit badge beside an MT chip, and a connector gating on status would push it."""
    pid = _key_project()
    kid = _make_key(pid, "nav::wf", char_limit=3, base="Welcome to the dashboard.")
    with Session(engine) as s:
        doc = keys.internal_document(s, s.get(Project, pid), "", create=False)
        doc_id = doc.id
    with Session(engine) as s:
        w = Workflow(name="WF-%d" % _seq(), scope="global", source_locale=SRC, target_locale=TGT)  # noqa: UP031
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate to {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wid = w.id
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream(s, wid, doc_id, TGT))
    _, status, flags = _seg_snapshot(_value_seg(kid))
    assert _has_flag(flags, quality.CHAR_LIMIT_FLAG_TYPE), (
        f"a real workflow run must persist the char_limit flag on the key value: {flags!r}"
    )
    _eq(
        status,
        SegStatus.NEEDS_REVIEW,
        "the orchestrator write-back must re-assert NEEDS_REVIEW for the over-limit value",
    )
    print("  [G1] real run_workflow_stream: char_limit flag persisted AND status needs_review (H2)")


# ============================================================================
# [H1] ORDERING vs the tag-repair rungs (reviewer H1) -- the gate reads FINAL text
# ============================================================================
_TOKEN = "⟦x1⟧"  # the inline-fidelity token the tag gate tracks


def test_h1a_dedup_shorter_no_false_positive():
    """The deterministic dedup rung makes the committed text SHORTER. Judged BEFORE it, the
    gate false-positived on the pre-repair length (a flag naming text that no longer
    exists). The gate now runs AFTER repair, so a dedup that lands within the limit is
    clean."""
    pid = _key_project()
    kid = _make_key(pid, "nav::dedup", char_limit=25, base=f"Save {_TOKEN} now")
    # 40-char damaged target (6x the token); dedup -> 20 chars, within 25.
    tgt = "Speichern " + _TOKEN * 6 + " jetzt"
    with Session(engine) as s:
        seg = s.get(Segment, _value_seg(kid))
        out = quality.commit_mt_output(s, seg, tgt, source="mt")
        s.commit()
        s.refresh(seg)
        _eq(quality.char_count(seg.target_text), 20, "the committed text is the 20-char dedup")
        _eq(
            out.char_limit_violation,
            "",
            "the gate must judge the 20-char REPAIRED text, not the 40-char pre-repair one",
        )
        assert not _has_flag(seg.qa_flags, quality.CHAR_LIMIT_FLAG_TYPE), (
            f"a dedup within the limit must NOT carry a char_limit flag: {seg.qa_flags!r}"
        )
        _eq(seg.status, SegStatus.MT, "a clean dedup within the limit stays MT")
    print("  [H1a] dedup (shorter) is judged AFTER repair -> no false-positive flag")


def test_h1b_llm_rung_longer_is_flagged():
    """The LLM rung REASSIGNS the text to a LONGER verified string. Judged before it, the
    gate saw the short original and the limit was BYPASSED. The gate now runs after, so the
    longer committed text is flagged."""
    from app.services.tag_llm_repair import RepairResult

    longer = f"Los, wir legen jetzt richtig los {_TOKEN}!!"  # 39 chars, tags intact  # noqa: E501, RUF100, UP032

    def _repairer(_src, _text):
        return RepairResult(text=longer, clean=True, note="", verified=True)

    pid = _key_project()
    kid = _make_key(pid, "nav::llm", char_limit=10, base=f"Go {_TOKEN}")
    with Session(engine) as s:
        seg = s.get(Segment, _value_seg(kid))
        # "Los" is missing the token -> tag damage the deterministic rung cannot repair,
        # so the injected LLM rung runs and commits the 39-char verified text.
        out = quality.commit_mt_output(s, seg, "Los", source="mt", llm_tag_repair=_repairer)
        s.commit()
        s.refresh(seg)
        _eq(seg.target_text, longer, "the LLM-repaired 39-char text is committed")
        assert out.char_limit_violation, (
            "the gate must judge the 39-char REPAIRED text and flag it over the limit of 10"
        )
        assert _has_flag(seg.qa_flags, quality.CHAR_LIMIT_FLAG_TYPE), (
            f"the LLM-repaired over-limit text must carry a char_limit flag: {seg.qa_flags!r}"
        )
        _eq(seg.status, SegStatus.NEEDS_REVIEW, "and route to review")
    print("  [H1b] LLM rung (longer) is judged AFTER repair -> flagged, not bypassed")


# ============================================================================
# [M1] coerce never raises: OverflowError (float inf via JSON)
# ============================================================================
def test_m1_coerce_survives_overflow():
    for bad in (float("inf"), float("-inf"), float("nan")):
        _eq(
            quality.coerce_char_limit(bad),
            None,
            f"a {bad!r} limit must coerce to None, never raise (OverflowError/ValueError)",
        )
        _eq(
            quality.char_limit_violation("some text", bad),
            "",
            f"the predicate must not raise on a {bad!r} limit",
        )
    print("  [M1] coerce_char_limit(inf/-inf/nan) -> None, never raises")


# ============================================================================
# [M3] the Segment.meta per-unit override is DEFERRED (dropped from the resolver)
# ============================================================================
def test_m3_meta_override_is_ignored():
    """A document segment carrying meta['char_limit'] must resolve to None -- the override
    is phase-2 and reading it would compete with key.char_limit (reviewer M3). This also
    makes document byte-identity unconditional."""
    with Session(engine) as s:
        from app.models import Document, TransMode

        p = content.project_create(
            s,
            "K8meta-%d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=TGT,
            default_provider="mock",  # noqa: RUF100, UP031
        )
        doc = Document(
            project_id=p.id,
            filename="d.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hi"}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        seg = Segment(
            document_id=doc.id,
            target_locale=TGT,
            order_index=0,
            source_text="Hi",
            status=SegStatus.NEW,
            meta={"char_limit": 3},
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)
        _eq(
            quality.segment_char_limit(s, seg),
            None,
            "meta['char_limit'] must be IGNORED (phase-2 override deferred)",
        )
        out = quality.commit_mt_output(s, seg, "A long over-limit target", source="mt")
        _eq(out.char_limit_violation, "", "a meta-only limit must not gate")
    print("  [M3] Segment.meta['char_limit'] is ignored (override deferred to phase 2)")


# ============================================================================
# [L1] bool is not a limit (True -> 1)
# ============================================================================
def test_l1_bool_is_not_a_limit():
    _eq(quality.coerce_char_limit(True), None, "True must read as UNSET, not 1")
    _eq(quality.coerce_char_limit(False), None, "False must read as UNSET")
    _eq(
        quality.char_limit_violation("hello world", True),
        "",
        "a True limit must not flag every value as over 1",
    )
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        try:
            keys.create_key(s, p, "bool::key", char_limit=True)
            _fail("create_key accepted a bool char_limit")
        except keys.KeyServiceError:
            pass
    kid = _make_key(pid, "bool::edit", base="Hi")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        try:
            keys.apply_key_edit(s, k, char_limit=True)
            _fail("apply_key_edit accepted a bool char_limit")
        except keys.KeyServiceError:
            pass
    print("  [L1] bool char_limit rejected at ingestion and read as unset by the predicate")


# ============================================================================
# [H] THE LIFTED LAB SCORER delegates to quality
# ============================================================================
def test_h_lab_scorer_delegates():
    within = eval_metrics._qa_char_limit("s", "short", "ref", {"char_limit": 10})
    _eq(within.value, 1.0, "within-limit Lab score is 1.0")
    over = eval_metrics._qa_char_limit("s", "x" * 20, "ref", {"char_limit": 10})
    _eq(over.value, 0.0, "over-limit Lab score is 0.0")
    na = eval_metrics._qa_char_limit("s", "x", "ref", {"char_limit": 0})
    assert na.not_applicable, "char_limit 0 is NOT_APPLICABLE, never a free pass"
    na2 = eval_metrics._qa_char_limit("s", "x", "ref", {"char_limit": "abc"})
    assert na2.not_applicable, "a non-integer limit is NOT_APPLICABLE"
    # NFC parity between Lab and production: decomposed accent counts as ONE, so it fits 1.
    dec = eval_metrics._qa_char_limit("s", "é", "ref", {"char_limit": 1})
    _eq(dec.value, 1.0, "the lifted scorer counts NFC code points (decomposed e-acute is 1)")
    print("  [H1] Lab _qa_char_limit delegates to quality (within/over/NA/NFC)")


# ============================================================================
# [I] THE EXPORT report
# ============================================================================
def test_i_export_reports_over_limit_value():
    pid = _key_project()
    kid = _make_key(pid, "nav::exp", char_limit=4, base="Save")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Speichern", origin=keys.ORIGIN_IMPORT)
    rep = []
    with Session(engine) as s:
        key_files.export_locale(s, pid, TGT, fmt="json", report=rep)
    over = [e for e in rep if e.get("kind") == key_files.CHAR_OVER_LIMIT]
    assert over, f"an over-limit value that ships must produce a char_limit report entry: {rep!r}"
    # A whole-project (namespace=None) export qualifies names, so the default namespace is
    # spelled `__default__::nav::exp`; the point is that the offending key is named.
    assert over[0].get("index", "").endswith("nav::exp"), (
        f"the report must name the offending key: {over[0]!r}"
    )
    _eq(over[0].get("limit"), 4, "the report carries the limit")
    _eq(over[0].get("length"), len("Speichern"), "the report carries the length")
    names = key_files.over_char_limit_keys(rep)
    assert len(names) == 1 and names[0].endswith("nav::exp"), (
        f"over_char_limit_keys reads the report: {names!r}"
    )
    notices = key_files.export_notices(rep)
    assert any("character limit" in n for n in notices), (
        f"export_notices must announce the over-limit value: {notices!r}"
    )
    hdrs = key_files.export_headers(rep)
    _eq(hdrs.get(key_files.HDR_OVER_LIMIT), "1", "the download header carries the count")
    assert "over_char_limit" in key_files.audit_detail(rep), (
        "the audit detail must record the over-limit fact"
    )
    print("  [I1] export: report entry + notice + header + audit for an over-limit value")


def test_i_export_report_is_empty_for_within_limit():
    pid = _key_project()
    kid = _make_key(pid, "nav::fit", char_limit=40, base="Save")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Speichern", origin=keys.ORIGIN_IMPORT)
    rep = []
    with Session(engine) as s:
        key_files.export_locale(s, pid, TGT, fmt="json", report=rep)
    assert not [e for e in rep if e.get("kind") == key_files.CHAR_OVER_LIMIT], (
        f"a within-limit value must not appear in the export report: {rep!r}"
    )
    print("  [I2] export: a within-limit value produces no report entry")


# ============================================================================
# [J] READ-TIME derived over_limit + grid tooltip
# ============================================================================
def test_j_key_view_derives_over_limit_without_a_write():
    pid = _key_project()
    kid = _make_key(pid, "nav::derive", base="Hi")  # NO limit at create time
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Ein langer Wert", origin=keys.ORIGIN_HUMAN)
    # set a limit AFTER the value exists -- write-time gating never judged it.
    _force_char_limit(kid, 4)
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        view = keys.key_view(s, k)
    entry = view["values"][TGT][0]
    assert entry.get("over_limit"), (
        f"key_view must DERIVE over_limit for a pre-existing violation: {entry!r}"
    )
    _eq(entry["over_limit"]["limit"], 4, "the derived marker carries the limit")
    _eq(entry["over_limit"]["length"], len("Ein langer Wert"), "and the length")
    print("  [J1] key_view derives over_limit on read for an untouched historical value")


def test_j_flag_carries_a_message_for_the_grid_tooltip():
    """The grid tooltip read `f.get('detail')`, which NO family ever set -- every tooltip
    was empty. The char_limit flag (like every family) carries `message`; the template now
    reads it."""
    f = quality.char_limit_flag("63 characters, limit 40", 63, 40)
    assert f.get("message"), "the flag must carry a non-empty message for the tooltip"
    _eq(f.get("message"), "Over character limit: 63 characters, limit 40", "the message")
    print("  [J2] the char_limit flag carries a message (grid tooltip no longer empty)")


# ============================================================================
# [K] INGESTION hardening
# ============================================================================
def test_k_ingestion_rejects_bad_limits():
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        for bad in (0, -5, "abc"):
            try:
                keys.create_key(s, p, f"bad::{_seq()}", char_limit=bad)
                _fail(f"create_key accepted a {bad!r} char_limit")
            except keys.KeyServiceError:
                pass
        # a good value is capped at CHAR_LIMIT_MAX.
        k = keys.create_key(s, p, f"cap::{_seq()}", char_limit=10**9)
        from app.services.context_pkg import CHAR_LIMIT_MAX

        _eq(k.char_limit, CHAR_LIMIT_MAX, "an absurd limit is capped at CHAR_LIMIT_MAX")
    # apply_key_edit rejects the same shapes.
    kid = _make_key(pid, "edit::me", base="Hi")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        for bad in (0, -5, "abc"):
            try:
                keys.apply_key_edit(s, k, char_limit=bad)
                _fail(f"apply_key_edit accepted a {bad!r} char_limit")
            except keys.KeyServiceError:
                pass
    print("  [K1] create_key / apply_key_edit reject <=0 and non-integers, cap at MAX")


# ============================================================================
# [K2] update_key (the keys_hub3 seam, also reached by the API metadata PATCH) applies
#      the SAME char_limit rule. Before this it copied meta values straight in, so
#      0 / -5 / 'abc' / True landed on the column (Postgres DataError for non-ints).
# ============================================================================
def test_k2_update_key_rejects_bad_and_coerces_good():
    pid = _key_project()
    kid = _make_key(pid, "upd::meta", base="Hi")
    # every refusal leaves char_limit exactly where it was (nothing stored).
    for bad in (0, -5, "abc", True):
        with Session(engine) as s:
            k = s.get(StringKey, kid)
            before = k.char_limit
            try:
                keys.update_key(s, k, char_limit=bad)
                _fail(f"update_key accepted a {bad!r} char_limit")
            except keys.KeyServiceError:
                pass
        with Session(engine) as s:
            _eq(
                s.get(StringKey, kid).char_limit,
                before,
                f"a refused {bad!r} char_limit must store nothing",
            )
    # a valid int and its numeric string both land on 40.
    for good in (40, "40"):
        with Session(engine) as s:
            keys.update_key(s, s.get(StringKey, kid), char_limit=good)
        with Session(engine) as s:
            _eq(s.get(StringKey, kid).char_limit, 40, f"a {good!r} char_limit must store 40")
    # None clears the limit.
    with Session(engine) as s:
        keys.update_key(s, s.get(StringKey, kid), char_limit=None)
    with Session(engine) as s:
        _eq(s.get(StringKey, kid).char_limit, None, "a None char_limit clears the limit")
    # the keys.py edit path keeps refusing the same shapes.
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        for bad in (0, -5, "abc", True):
            try:
                keys.apply_key_edit(s, k, char_limit=bad)
                _fail(f"apply_key_edit accepted a {bad!r} char_limit")
            except keys.KeyServiceError:
                pass
    print("  [K2] update_key (keys_hub3) rejects 0/-5/'abc'/True, coerces 40/'40', clears None")


# ============================================================================
# [N] the CAT EDITOR save (documents.segment_save) mirrors the char_limit family
#     (fix-round F1). The grid Save routes through keys.set_value (sections B/E);
#     the editor Save is a SEPARATE seam that was NOT clearing/re-judging char_limit.
# ============================================================================
from starlette.requests import Request as _Req


def _bare_request():
    """A minimal POST Request. Auth is OFF in this hermetic suite, so `session_user`
    returns the SYSTEM admin WITHOUT touching request.session, and `_visibility_block`
    passes (single-tenant) -- exactly the posture the editor's TestClient save runs under."""
    return _Req(
        {
            "type": "http",
            "method": "POST",
            "headers": [],
            "query_string": b"",
            "path": "/segments/x/save",
        }
    )


def _editor_save(seg_id, text, module=None):
    """Drive `documents.segment_save` (LIVE, or a pre-fix module) as the CAT editor's POST
    does. A fresh Session per call; the handler commits."""
    from app.web import documents as _live

    save = (module or _live).segment_save
    with Session(engine) as s:
        return save(
            seg_id, request=_bare_request(), target_text=text, tte_ms="0", confirm="", session=s
        )


def _editor_key_value_seg(char_limit, base="Hi"):
    """A KEY value segment: it carries BOTH a document_id (its project's internal doc) and a
    key_id, so it is editable through the editor save AND governed by a char_limit."""
    pid = _key_project()
    kid = _make_key(pid, "nav::editor%d" % _seq(), char_limit=char_limit, base=base)  # noqa: UP031
    return kid, _value_seg(kid)


def test_n_editor_save_clears_stale_machine_flag_on_human_fix():
    """CONSEQUENCE (a): a machine-set char_limit flag must not survive a human fix through
    the editor. If it does, project_keys.html suppresses the DERIVED over-limit marker
    (a stored flag 'already says the same thing') and renders the STALE badge over text
    that now fits."""
    kid, seg_id = _editor_key_value_seg(char_limit=5, base="Home")
    with Session(engine) as s:  # 1) machine over-limit -> flag + needs_review
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Startseite lang", origin=keys.ORIGIN_IMPORT)
    assert _has_flag(_seg_snapshot(seg_id)[2], "char_limit"), "precondition: machine flag set"
    _editor_save(seg_id, "kurz")  # 2) human fixes it to a within-limit value
    _, _, flags = _seg_snapshot(seg_id)
    assert not _has_flag(flags, "char_limit"), (
        f"the editor save must CLEAR the stale machine char_limit flag: {flags!r}"
    )
    print("  [N1] editor save clears a stale machine char_limit flag on a human fix")


def test_n_editor_save_flags_human_introduced_violation():
    """CONSEQUENCE (b): a human over-limit edit through the editor must be flagged exactly
    like the identical edit through keys.set_value's human branch -- and STAY edited (human
    text is content, never gated: spec 2a)."""
    _kid, seg_id = _editor_key_value_seg(char_limit=4, base="Save")
    _editor_save(seg_id, "Speichern")  # 9 chars over 4
    text, status, flags = _seg_snapshot(seg_id)
    _eq(text, "Speichern", "the human text is committed verbatim")
    _eq(status, SegStatus.EDITED, "a human over-limit editor save STAYS edited (spec 2a)")
    cl = _has_flag(flags, "char_limit")
    assert cl, f"a human over-limit editor save must carry a char_limit flag: {flags!r}"
    _eq(cl[0].get("limit"), 4, "the flag records the limit")
    _eq(cl[0].get("length"), len("Speichern"), "the flag records the length")
    print("  [N2] editor save flags a human-introduced over-limit value (stays edited)")


def test_n_editor_save_document_segment_is_byte_identical():
    """A DOCUMENT segment (key_id None) edited through the editor is untouched by the gate:
    no char_limit flag, no crash. Guards the unconditional byte-identity claim on the
    editor seam too."""
    with Session(engine) as s:
        from app.models import Document, TransMode

        p = content.project_create(
            s,
            "K8ed-%d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=TGT,
            default_provider="mock",  # noqa: RUF100, UP031
        )
        doc = Document(
            project_id=p.id,
            filename="d.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hi"}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        seg = Segment(
            document_id=doc.id,
            target_locale=TGT,
            order_index=0,
            source_text="Hi",
            status=SegStatus.NEW,
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)
        seg_id = seg.id
    _editor_save(seg_id, "A perfectly ordinary and rather long target string")
    _, _, flags = _seg_snapshot(seg_id)
    assert not _has_flag(flags, "char_limit"), (
        f"a document segment must never carry a char_limit flag from the editor: {flags!r}"
    )
    print("  [N3] editor save on a document segment: no char_limit flag (byte-identical)")


def _needs_history(sha):
    """Run a fail-before proof only where its pinned pre-fix commit exists. The public
    repository starts at the 1.0.0 commit, so the older history these proofs read with
    `git show` is absent there; the proof still runs in a clone that has the history."""

    def wrap(fn):
        def inner(*args, **kwargs):
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=_ROOT, capture_output=True
            )
            if probe.returncode != 0:
                print(f"  [skip] {fn.__name__}: pre-fix commit {sha} is not in this clone")
                return None
            return fn(*args, **kwargs)

        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner

    return wrap


@_needs_history("6e6b727")
def test_n_prefix_editor_save_reproduces_both_consequences():
    """FAIL-BEFORE via `git show 6e6b727:app/web/documents.py` (NEVER git stash -- shared
    stack). 6e6b727 is the last pre-F1 commit (permanently in main's history after the K8
    merge); its segment_save never touched the char_limit family, so BOTH consequences
    reproduce against it: (a) a machine flag SURVIVES a within-limit human fix, and (b) a
    human over-limit save lands with NO char_limit flag. (Was `HEAD` on the branch, which
    inverts once the fix itself is at HEAD -- a committed fail-before must pin the pre-fix
    commit, like PREFIX_COMMIT above.)"""
    _pre_commit = "6e6b727"
    presrc = subprocess.check_output(
        ["git", "show", f"{_pre_commit}:app/web/documents.py"],
        cwd=_ROOT,  # noqa: RUF100, UP032
    ).decode("utf-8", "replace")
    assert "clear_char_limit_flags" not in presrc, (
        "pre-fix documents.py must NOT clear char_limit -- the fail-before is not the bug"
    )
    pre = _load_prefix_module("app/web/documents.py", "_prefix_documents", commit=_pre_commit)
    # (a) machine flag survives a within-limit human fix
    kid, seg_id = _editor_key_value_seg(char_limit=5, base="Home")
    with Session(engine) as s:
        k = s.get(StringKey, kid)
        keys.set_value(s, k, TGT, "Startseite lang", origin=keys.ORIGIN_IMPORT)
    _editor_save(seg_id, "kurz", module=pre)
    assert _has_flag(_seg_snapshot(seg_id)[2], "char_limit"), (
        "PRE-FIX editor save must LEAVE the stale machine flag (consequence a reproduces)"
    )
    # (b) human over-limit save lands with no flag
    _kid2, seg_id2 = _editor_key_value_seg(char_limit=4, base="Save")
    _editor_save(seg_id2, "Speichern", module=pre)
    _eq(_seg_snapshot(seg_id2)[0], "Speichern", "pre-fix still commits the human text")
    assert not _has_flag(_seg_snapshot(seg_id2)[2], "char_limit"), (
        "PRE-FIX editor save must NOT flag a human over-limit value (consequence b reproduces)"
    )
    print(
        "  [N4] pre-fix editor save (HEAD): stale flag survives AND human violation "
        "unflagged -- both consequences"
    )


# ============================================================================
# [Z] FAIL-BEFORE via file copy (NEVER git stash)
# ============================================================================
def _load_prefix_module(rel_path, mod_name, commit=PREFIX_COMMIT):
    """`git show <commit>:rel_path` -> a temp file imported under a private name, so the
    pre-fix code runs against the LIVE app.models/app.services it imports. `commit` defaults
    to the branch point off main; the editor fix-round (F1) passes HEAD, whose committed
    documents.py predates the char_limit clearing added to the working tree."""
    src = subprocess.check_output(["git", "show", f"{commit}:{rel_path}"], cwd=_ROOT)
    # The old commit imports the LLMemory add-on, which the open core does not ship; its
    # capture call sits in a try/except, so dropping the import keeps the fail-before honest.
    src = src.replace(b" llmemory,", b"")
    fd, path = tempfile.mkstemp(prefix=mod_name + "_", suffix=".py")
    os.write(fd, src)
    os.close(fd)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@_needs_history(PREFIX_COMMIT)
def test_z_prefix_quality_has_no_predicate_and_does_not_flag():
    pre = _load_prefix_module("app/services/quality.py", "_prefix_quality")
    assert not hasattr(pre, "char_limit_violation"), (
        "pre-fix quality.py must have NO char_limit predicate (feature absent)"
    )
    # Drive an over-limit KEY value through the PRE-FIX seam: it must NOT flag (the bug).
    pid = _key_project()
    kid = _make_key(pid, "nav::prefix", char_limit=3, base="Home")
    with Session(engine) as s:
        seg = s.get(Segment, _value_seg(kid))
        pre.commit_mt_output(s, seg, "Startseite ist zu lang", source="mt")
        s.commit()
        s.refresh(seg)
        assert not _has_flag(seg.qa_flags, "char_limit"), (
            "PRE-FIX seam flagged char_limit -- the fail-before is not reproducing the "
            f"shipped bug (enforced NOWHERE): {seg.qa_flags!r}"
        )
    print("  [Z1] pre-fix quality.py: no predicate, seam does NOT flag an over-limit value")


@_needs_history(PREFIX_COMMIT)
def test_z_prefix_create_key_accepts_a_zero_limit():
    pre = _load_prefix_module("app/services/keys.py", "_prefix_keys")
    assert not hasattr(pre, "_coerce_char_limit"), (
        "pre-fix keys.py must have NO _coerce_char_limit (ingestion was unguarded)"
    )
    pid = _key_project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        k = pre.create_key(s, p, "nav::prefixzero", char_limit=0)
        _eq(k.char_limit, 0, "pre-fix create_key stored a 0 char_limit straight through")
    print("  [Z2] pre-fix create_key accepted a 0 char_limit (ingestion was unguarded)")


def test_z_naive_comparison_would_crash_on_a_string_limit():
    """The reachable-today string defect: a `\"40\"` limit reaching a NAIVE `len(text) >
    limit` raises TypeError inside the write seam. The shipped predicate coerces it."""
    crashed = False
    try:
        _ = len("hello world") > "40"  # int > str -> TypeError in Py3
    except TypeError:
        crashed = True
    assert crashed, "the naive comparison must TypeError on a string limit"
    # the shipped predicate does not:
    _eq(
        quality.char_limit_violation("hello world", "40"),
        "",
        'the shipped predicate coerces "40" and does not raise',
    )
    print('  [Z3] a naive len()>limit crashes on "40"; the shipped predicate coerces it')


# ============================================================================
def main():
    test_a_predicate()
    test_b_seam_flags_over_limit_key_value()
    test_b_tm_eligible_unchanged()
    test_b_within_limit_is_clean()
    test_c_document_segment_is_byte_identical_and_issues_no_stringkey_query()
    test_c_dirty_legacy_limits_never_crash_or_flag()
    test_d_plural_only_one_form_flagged()
    test_e_human_write_flags_but_stays_edited()
    test_f_rewrite_within_limit_clears_only_char_limit()
    test_g_real_workflow_run_persists_the_flag()
    test_h1a_dedup_shorter_no_false_positive()
    test_h1b_llm_rung_longer_is_flagged()
    test_m1_coerce_survives_overflow()
    test_m3_meta_override_is_ignored()
    test_l1_bool_is_not_a_limit()
    test_h_lab_scorer_delegates()
    test_i_export_reports_over_limit_value()
    test_i_export_report_is_empty_for_within_limit()
    test_j_key_view_derives_over_limit_without_a_write()
    test_j_flag_carries_a_message_for_the_grid_tooltip()
    test_k_ingestion_rejects_bad_limits()
    test_k2_update_key_rejects_bad_and_coerces_good()
    test_n_editor_save_clears_stale_machine_flag_on_human_fix()
    test_n_editor_save_flags_human_introduced_violation()
    test_n_editor_save_document_segment_is_byte_identical()
    test_n_prefix_editor_save_reproduces_both_consequences()
    test_z_prefix_quality_has_no_predicate_and_does_not_flag()
    test_z_prefix_create_key_accepts_a_zero_limit()
    test_z_naive_comparison_would_crash_on_a_string_limit()
    print("verify_char_limit OK")


if __name__ == "__main__":
    main()
