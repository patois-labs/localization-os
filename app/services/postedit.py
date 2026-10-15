"""Post-edit capture — episode-scoped edit-distance rows (SPEC-wave2-qa-pe-reporting §3.2).

Pure capture layer for the `postedit` report kind and the `segment_post_edits` lakehouse
stream. It is deliberately un-flag-gated (§2.5's whole argument is that a report must not
empty when an unrelated switch flips) and deliberately CHEAP: one row upsert + two
Levenshtein calls on ONE segment per edit.

Two public entry points:
  * `distances(baseline, final)` — the reused metric primitive (§3.2.5).
  * `capture_guarded(session, ...)` — the ONLY thing a seam calls. It wraps `capture`
    in a SAVEPOINT (`session.begin_nested`, N4) so a capture failure rolls back ONLY the
    capture and never poisons the caller's transaction, with a single race-retry (N9) for
    the (segment_id, episode_seq) unique index. It NEVER raises and never commits — the
    seam owns the outer commit.

The metric is REUSED from `eval_metrics`, never forked (§3.2.5, the sacreBLEU lesson): a
distance computed with a different tokenizer is a different number, and the Evaluations Lab
and this report must never disagree about what "edit distance" means. `metric_version` is
stamped on write and never mutated.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import hashlib
import logging
import re
from datetime import datetime

import rapidfuzz
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import SegmentPostEdit, SegStatus
from app.services import eval_metrics

log = logging.getLogger("loc_tms.postedit")

PE_METRIC_VERSION = "pe/1"
# Documentation constant (not stored — only PE_METRIC_VERSION is persisted). The
# spec-§3.2.5 signature: word tokenizer + rapidfuzz build, so a distance's provenance is
# self-describing exactly like eval_metrics' own signatures.
PE_METRIC_SIGNATURE = (
    f"levenshtein|char+word|tok:{eval_metrics.WORD_TOKENIZER}|{rapidfuzz.__version__}"
)

# Whitespace-delimited token rule — IDENTICAL to reports._derived_words (`\S+`), inlined
# so the capture hot path takes no reports import (L6). `words_derived` in the report
# therefore sums the same number this stamps.
_WS_RE = re.compile(r"\S+")

# Pre-write statuses that mean the target came from a human (§3.2.4 classify). Everything
# else non-empty is treated as a machine/import origin ('mt'); a TM signal wins first.
_HUMAN_STATUSES = frozenset({SegStatus.EDITED, SegStatus.APPROVED})


# #599: Levenshtein is O(len(a) * len(b)) and rapidfuzz holds the GIL for the whole call, so
# one editor save of a 100k-character target (MAX_SEGMENT_CHARS) froze EVERY thread, the event
# loop and /health included, for about 8 s. Up to this many cells (20k x 20k characters, about
# 0.05 s) the computation is the full one, unchanged; past it, the banded search below.
PE_EXACT_CELLS = 400_000_000


def _bounded_distance(a, b) -> tuple[int, bool]:
    """(distance, exact) for two strings or token lists, in bounded time (#599).

    Within PE_EXACT_CELLS: the plain full computation. Past it: rapidfuzz's banded search
    (`score_cutoff`), whose cost is length x band, with the band widened 4x per round up to
    the budget. It is EXACT whenever the true distance fits the band, which is the normal
    post-edit case (a long segment with a small edit). Only a near-total rewrite of a huge
    segment exceeds it; that returns the proven lower bound max(band + 1, |len(a) - len(b)|)
    with exact=False, never the full quadratic pass."""
    return eval_metrics.bounded_distance(a, b, PE_EXACT_CELLS)


def distances(baseline: str, final: str) -> tuple[int, int, float]:
    """(char, word, pct) edit distance, byte-for-byte the same numbers eval_metrics emits.

    char = Levenshtein.distance over raw characters       (eval_metrics._edit_char:473)
    word = Levenshtein.distance over eval_metrics.word_tokens (…_edit_word:462)
    pct  = Levenshtein.normalized_distance * 100, 4dp         (…_edit_pct:482)

    Bounded time (#599): past PE_EXACT_CELLS see _bounded_distance (exact for a small edit
    of a huge segment; a lower bound only for a near-total rewrite of one).
    """
    from rapidfuzz.distance import Levenshtein

    a = baseline or ""
    b = final or ""
    if len(a) * len(b) <= PE_EXACT_CELLS:
        char = Levenshtein.distance(a, b)
        word = Levenshtein.distance(eval_metrics.word_tokens(a), eval_metrics.word_tokens(b))
        pct = round(Levenshtein.normalized_distance(a, b) * 100.0, 4)
        return char, word, pct
    char, exact = _bounded_distance(a, b)
    word, _ = _bounded_distance(eval_metrics.word_tokens(a), eval_metrics.word_tokens(b))
    if not exact:
        log.warning(
            "post-edit distance over %d x %d characters exceeded the bounded band; "
            "recorded the lower bound %d",
            len(a),
            len(b),
            char,
        )
    pct = round(char / max(len(a), len(b)) * 100.0, 4)
    return char, word, pct


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _derived_words(source_text: str) -> int:
    """Whitespace-delimited token count — the SAME rule as reports._derived_words (M4),
    inlined (L6) so capture takes no reports import."""
    return len(_WS_RE.findall(source_text or ""))


def classify_baseline(pre: str, *, pre_status, tm_match_pct, lock_band) -> str:
    """'mt' | 'tm' | 'human' | 'none' for the START-of-episode target (§3.2.4).

    Order is load-bearing: an empty pre is 'none' (a human clearing an empty target and
    typing is NOT "100% post-edited", invariant §3.2.4/4); a TM signal (a carried
    tm_match_pct or lock_band) is 'tm'; a human-owned status is 'human'; everything else
    non-empty in an MT-first product is machine origin, 'mt'."""
    if not (pre or "").strip():
        return "none"
    if tm_match_pct or lock_band:
        return "tm"
    if pre_status in _HUMAN_STATUSES:
        return "human"
    return "mt"


def _identity(row: SegmentPostEdit) -> tuple:
    return (row.actor_kind or "", row.actor or "", row.vendor_id, row.dispatch_id)


def capture(
    session: Session,
    *,
    segment,
    pre: str,
    post: str,
    actor_kind: str,
    actor: str = "",
    vendor_id=None,
    dispatch_id=None,
    unit_kind: str = "segment",
    target_locale=None,
    pre_status=None,
    tm_match_pct=None,
    lock_band=None,
    now: datetime | None = None,
) -> SegmentPostEdit | None:
    """Record ONE edit into its episode row (§3.2.4). Returns the row, or None on a no-op.

    Continues the last episode iff the actor identity is unchanged AND the last episode's
    `final_sha` still describes the pre-write text (N1: an intervening machine re-commit
    breaks the lineage and opens a fresh episode). Otherwise opens a new episode baselined
    on `pre`. Does NOT commit — the seam owns the outer transaction (N4).
    """
    pre = pre or ""
    post = post or ""
    if pre == post:
        return None  # invariant §3.2.4/5 — a no-op edit records nothing
    now = now or datetime.utcnow()
    tl = target_locale if target_locale is not None else (segment.target_locale or "")
    if pre_status is None:
        pre_status = getattr(segment, "status", "")
    if tm_match_pct is None:
        tm_match_pct = getattr(segment, "tm_match_pct", None)
    if lock_band is None:
        lock_band = getattr(segment, "lock_band", None)

    ident = (actor_kind or "", actor or "", vendor_id, dispatch_id)
    # L1: load ONLY the two rows the algorithm needs — the LAST episode (identity +
    # final_sha lineage check) and the FIRST episode (cum_* origin baseline) — never every
    # full-body row of the segment's history. `baseline_text` can be a whole document on a
    # full_text unit, so an ORDER-BY-all load is the F1-shaped cost this avoids.
    last = session.exec(
        select(SegmentPostEdit)
        .where(SegmentPostEdit.segment_id == segment.id)
        .order_by(SegmentPostEdit.episode_seq.desc())
        .limit(1)
    ).first()

    if last is not None and _identity(last) == ident and last.final_sha == _sha(pre):
        ep = last  # same actor, unbroken lineage (N1)
    else:
        ep = SegmentPostEdit(
            segment_id=segment.id,
            document_id=segment.document_id,
            target_locale=tl,
            episode_seq=(last.episode_seq + 1) if last else 1,
            unit_kind=unit_kind,
            actor=actor or "",
            actor_kind=actor_kind or "",
            vendor_id=vendor_id,
            dispatch_id=dispatch_id,
            baseline_text=pre,
            baseline_kind=classify_baseline(
                pre, pre_status=pre_status, tm_match_pct=tm_match_pct, lock_band=lock_band
            ),
            baseline_chars=len(pre),
            source_words=_derived_words(getattr(segment, "source_text", "") or ""),
            metric_version=PE_METRIC_VERSION,
            edit_count=0,
            first_edited_at=now,
            last_edited_at=now,
        )
        session.add(ep)

    ep.edit_count += 1
    ep.last_edited_at = now
    ep.final_chars = len(post)
    ep.final_sha = _sha(post)  # N1: the lineage marker

    if ep.baseline_kind == "none":
        # invariant §3.2.4/4 — no machine/TM/human origin, so no distance is meaningful.
        ep.distance_char = ep.distance_word = ep.distance_pct = None
    else:
        ep.distance_char, ep.distance_word, ep.distance_pct = distances(ep.baseline_text, post)

    # cum_* origin = episode 1's baseline. When this IS episode 1 (no prior rows) the origin
    # is `ep` itself; otherwise fetch the min-episode_seq row (one row, not the whole tail).
    if last is None:
        origin = ep
    else:
        origin = session.exec(
            select(SegmentPostEdit)
            .where(SegmentPostEdit.segment_id == segment.id)
            .order_by(SegmentPostEdit.episode_seq.asc())
            .limit(1)
        ).first()
    if origin.baseline_kind in ("mt", "tm"):
        ep.cum_distance_char, ep.cum_distance_word, ep.cum_distance_pct = distances(
            origin.baseline_text, post
        )
    else:
        # invariant §3.2.4/3 — no machine origin ⇒ cum_* is NULL, never 0, never fabricated.
        ep.cum_distance_char = ep.cum_distance_word = ep.cum_distance_pct = None

    session.flush()
    return ep


def capture_guarded(session: Session, **kwargs) -> SegmentPostEdit | None:
    """The ONLY thing a seam calls. Savepoint-wrapped capture (N4) + one race-retry (N9).

    Never raises and never commits. A capture failure — any exception, or the
    (segment_id, episode_seq) unique-index race — rolls back ONLY the SAVEPOINT, leaving
    the caller's outer transaction usable (so a vendor-return admission, a segment save or
    a paragraph write still commits). The retry re-reads `max(episode_seq)` implicitly by
    re-running `capture` in a fresh savepoint.
    """
    try:
        with session.begin_nested():
            return capture(session, **kwargs)
    except IntegrityError:
        # N9: two concurrent saves raced the unique index. Retry ONCE in a fresh
        # savepoint (capture re-reads the episode rows, so it recomputes episode_seq).
        try:
            with session.begin_nested():
                return capture(session, **kwargs)
        except Exception:
            log.warning("postedit: capture failed after one retry", exc_info=True)
    except Exception:
        log.warning("postedit: capture failed", exc_info=True)
    return None
