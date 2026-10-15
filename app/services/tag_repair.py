"""Cheap DETERMINISTIC tag repair — the rung that runs BEFORE quarantine.

AI Tagging 1.5b. When the write gate (quality.commit_mt_output) finds a target
whose `⟦⟧` tokens don't match the source, it tries this rung FIRST. Without it a
small model would either swamp the NEEDS_REVIEW queue or fail LQA straight back
into the obliterator (the fix loop). With it, the COMMON recoverable cases are
fixed with ZERO dependency and ZERO ML, and everything else falls to review.

CONTRACT (the whole reason this is safe):
  * Every candidate this produces is RE-VERIFIED by the SAME gate that called it
    (`quality._tag_damage`). `repair()` itself also re-checks with `_ok`, and only
    RETURNS a candidate that verifies clean — a repair that doesn't certify is
    discarded (returns None → the caller quarantines). Repair NEVER lowers the
    guarantee; it can only turn a would-be quarantine into a clean commit.
  * SCOPE IS DELIBERATELY NARROW (the harder paired-span / min-max projection is
    Phase 2; alignment is Phase 3):
      1. DE-DUPLICATION — a token the model emitted more times than the source has
         it, where dropping the surplus copies (keeping the earliest) restores the
         exact source multiset AND order. Safe: removing a redundant duplicate can
         never mis-place the tokens that remain, so it is CERTIFIED CLEAN. The
         doubled whitespace a removed surplus token leaves behind is collapsed
         (LOW-1) so the output reads naturally.
      2. POINT-TOKEN ORDER-RESTORE — the multiset already matches the source but
         POINT tokens (`⟦xN⟧`, self-closing: line breaks, images) are in the wrong
         order. Point tokens wrap nothing, so re-sequencing them to source order
         produces a well-formed result — BUT a legitimate target-language reorder of
         position-sensitive point tokens (an image that moves, a break that shifts)
         must NOT be silently undone. So this rung is BEST-GUESS, NOT clean: the
         reordered text is committed but the gate STILL flags tag_integrity +
         NEEDS_REVIEW so a human confirms the placement (MEDIUM, reviewer #2). PAIRED
         tokens are NEVER reordered here (moving a paired token's identity could
         re-wrap the wrong span), so a paired-token reorder falls through to quarantine.
  * Placement of a genuinely DROPPED paired span is NOT attempted (no anchor model
    in Phase 1) — that is exactly what quarantine + human review is for.

`repair()` returns ``(text, clean)`` — `clean=True` for a certified de-dup (commit
clean), `clean=False` for a best-guess point-order restore (commit the text but flag
for review) — or ``None`` when nothing recoverable applies (quarantine).

Documented in: docs/architecture/inline-tags.md
"""

from __future__ import annotations

from collections import Counter

from app.services import ai_tagging, segment


def _ok(source: str, candidate: str) -> bool:
    """The repair's own re-verification: `candidate` carries the SAME `⟦⟧` multiset
    AND order as `source`, is well-formed, and has no residual sentinel. Mirrors the
    write gate's `tag_damage` so a returned candidate is guaranteed to pass it."""
    src = segment.find_placeholders(source)
    cand = segment.find_placeholders(candidate)
    return (
        segment.same_token_order(src, cand)
        and segment.placeholders_well_formed(candidate)
        and not ai_tagging.has_residual_sentinel(candidate, source)
    )


def _is_point(tok: str) -> bool:
    """True for a self-closing point token (`⟦xN⟧`) — kind letter 'x', not a paired
    open/close."""
    inner = tok[1:-1]
    return not inner.startswith("/") and segment.is_point_kind(inner[:1])


def _dedup(source: str, target: str) -> str | None:
    """Rung 1 — drop surplus DUPLICATE token occurrences (keep the earliest of each)
    so the target multiset matches the source. Returns the edited target, or None
    when target has no such surplus (nothing to do)."""
    src_ct = Counter(segment.find_placeholders(source))
    tgt_ct = Counter(segment.find_placeholders(target))
    # A pure DUPLICATE is a token the target has MORE of than the source, where the
    # source has it at least once (a token wholly ABSENT from the source is an ADD,
    # not a duplicate — dropping it is not this rung's job; that stays a quarantine).
    surplus = {t: c for t, c in (tgt_ct - src_ct).items() if src_ct.get(t, 0) > 0}
    if not surplus:
        return None  # nothing is a pure duplicate of a source token
    # Walk the target token occurrences left→right, skipping the surplus copies of a
    # duplicated token (keep the first `src_ct[t]` occurrences of each).
    remaining = dict(surplus)  # how many copies still to drop, per token
    kept_seen: Counter = Counter()
    allowed = {t: src_ct[t] for t in surplus}
    out_parts: list[str] = []
    idx = 0
    for m in segment._PLACEHOLDER_RE.finditer(target):
        tok = m.group(0)
        out_parts.append(target[idx : m.start()])
        idx = m.end()
        if tok in remaining and remaining[tok] > 0:
            kept_seen[tok] += 1
            if kept_seen[tok] > allowed[tok]:
                remaining[tok] -= 1
                # LOW-1: this surplus token is being removed; if it was flanked by a
                # horizontal space on BOTH sides, drop the one that now follows so the
                # seam does not read as a doubled space. Targeted (only the removal
                # site) so unrelated whitespace elsewhere is untouched.
                prev = "".join(out_parts)
                if prev[-1:] in (" ", "\t") and idx < len(target) and target[idx] in (" ", "\t"):
                    idx += 1
                continue  # drop this surplus copy
        out_parts.append(tok)
    out_parts.append(target[idx:])
    return "".join(out_parts)


def _point_order_restore(source: str, target: str) -> str | None:
    """Rung 2 — when the multiset already matches but ONLY point tokens are out of
    order, rewrite the target's token slots so the token sequence follows source
    order. Returns the edited target, or None when the reorder would touch a paired
    token (never auto-reordered — see the module contract) or nothing needs fixing."""
    src = segment.find_placeholders(source)
    tgt = segment.find_placeholders(target)
    if Counter(src) != Counter(tgt) or segment.same_token_order(src, tgt):
        return None  # not a pure reorder (a free-order `⟦pN⟧` may move, #693)
    # Only safe when EVERY token involved is a point token: paired identities must
    # not be reshuffled (that could re-wrap the wrong span). If any paired token is
    # out of position, bail to quarantine.
    if any(not _is_point(t) for t in src):
        return None
    # Rewrite each token occurrence in the target, in order, to the source's k-th
    # token — positions (where the model put tokens) are preserved, identities follow
    # source order.
    # #693: a free-order token keeps the slot the model gave it; only the other tokens'
    # identities are put back in source order.
    it = iter(segment.order_significant(src))
    out_parts: list[str] = []
    idx = 0
    for m in segment._PLACEHOLDER_RE.finditer(target):
        out_parts.append(target[idx : m.start()])
        idx = m.end()
        tok = m.group(0)
        out_parts.append(tok if segment.is_free_token(tok) else next(it))
    out_parts.append(target[idx:])
    return "".join(out_parts)


def repair(source: str | None, target: str | None) -> tuple[str, bool] | None:
    """Try the cheap deterministic repairs on `target` against `source`, returning
    ``(repaired_text, clean)`` for the FIRST candidate that structurally verifies
    (`_ok`), or ``None`` when nothing recoverable applies (quarantine — the caller
    keeps the last structurally-valid version and routes to NEEDS_REVIEW).

    `clean` distinguishes the two rungs (MEDIUM, reviewer #2):
      * DE-DUP -> ``clean=True``: removing a redundant duplicate is provably safe, so
        the caller may commit it clean (no flag).
      * POINT-ORDER-RESTORE -> ``clean=False``: the reorder is a BEST-GUESS (a
        legitimate target-language point-token reorder must not be silently undone),
        so the caller commits the repaired text but STILL flags tag_integrity +
        NEEDS_REVIEW for a human to confirm the placement.

    Idempotent + side-effect-free (pure string transforms). A clean target returns
    None (no repair needed)."""
    source = source or ""
    target = target or ""
    if _ok(source, target):
        return None  # already clean — nothing to repair
    # Rung 1: de-dup (certified clean).
    cand = _dedup(source, target)
    if cand is not None and _ok(source, cand):
        return cand, True
    # dedup may fix the multiset but leave point tokens out of order; feed its output
    # to the point-order rung so the two compose (dedup THEN reorder) — best-guess.
    if cand is not None:
        cand2 = _point_order_restore(source, cand)
        if cand2 is not None and _ok(source, cand2):
            return cand2, False
    # Rung 2: point-order-restore alone (best-guess, NOT clean).
    cand = _point_order_restore(source, target)
    if cand is not None and _ok(source, cand):
        return cand, False
    return None
