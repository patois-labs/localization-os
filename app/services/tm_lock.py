"""TM matching -> fill -> LOCK, the engine behind the `tm_match` workflow step.

PROPOSAL-tm-matching-workflow-step (maintainer-approved, §2 locked). This module is the
ORCHESTRATION-ONLY core (D8: "it should never make changes, only hit play on what's
already set"): it looks up qualifying TM matches for a document's un-translated
segments, FILLS them through the sole write seam (quality.commit_mt_output — so a
poisoned entry is caught exactly as Phase 0 established), and LOCKS the filled
segments per the resolving TM policy so every later step (MT, LQA, fix rounds) skips
them. The lock is a persisted segment state (models.Segment.lock_band / lock_hard);
enforcement lives at the write seam (quality.is_hard_locked) and, for soft locks, in
the orchestrator's pending-set exclusion.

WHAT IS IN v1 HERE (matches §3 for the 100 band):
  * 100-band = source byte-exact (the existing tm.lookup_batch match_pct == 100).
  * fill + SOFT lock by default; HARD lock when policy.hundreds_enter_lqa is off.
  * multi-TM resolution rides tm.lookup_batch's own-row-first precedence; the winning
    entry's project supplies the policy (here: the reading project's global policy).

DELIBERATELY DEFERRED (documented in the proposal's own §14 wave split, and in the
builder report): the ICE / 101 context band (§4) needs a TM-entry `context` column +
capture-at-every-write-seam + backfill — "the largest and most invasive part of the
build" — so in this wave every fill is the 100 band and `lock_ice` is inert. An entry
with no context can only ever produce a 100, which is precisely the proposal's own
migration behaviour (§4). Backflow (§9) and spend-plan surfacing (§10) are later waves.

Documented in: docs/architecture/tm-and-glossary.md
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import Project, Segment, SegmentTM


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# --- per-TM policy (PROPOSAL §6) ---------------------------------------------
# POLICY GRAIN -- GLOBAL INTERIM, LABELLED (this fix round). §6 stores three
# controls "ON THE TM OBJECT" (per-TM, so you can duplicate a TM and A/B a toggle). This
# codebase has NO TM container object -- TM is a flat SegmentTM pool scoped by project --
# so there is nowhere per-TM to hang them yet. v1 resolves the policy from GLOBAL
# settings: the SAME three keys and defaults as §6 (D2/D3/D11), but applied install-wide
# rather than per-TM. This is a DELIBERATE, LABELLED INTERIM, not the §6 design: the
# per-TM grain is the target and lands with its own follow-up build (a first-class TM
# container object). Until then, the toggles below are global.
_SETTING_LOCK_ICE = "tm_lock_ice"  # default on  (D2)
_SETTING_HUNDREDS_ENTER_LQA = "tm_hundreds_enter_lqa"  # default on (D3)
_SETTING_CONTEXT_FLOOR = "tm_context_floor"  # default 95  (D11)
# Batch 15 T1 (SPEC-tm-match-step-config §11): the two NEW global default keys — the
# DEFAULTS a new tm_match step seeds from + the inherited value for a NULL-override step.
_SETTING_FILL_THRESHOLD = "tm_fill_threshold"  # default 100 (fill exactly the 100 band)
_SETTING_FUZZY_LOCK_MODE = "tm_fuzzy_lock_mode"  # default "soft" (a fuzzy fill enters LQA)

# The three fuzzy lock modes (D15-3 / §6.3). "none" = fill, no lock (a suggestion,
# overwritten by a downstream unprotected translate step, subject to the tm_protect
# guard); "soft" = fill + soft lock (enters LQA); "policy" = fill + the lock the 100-band
# policy would give (soft when hundreds_enter_lqa, else hard).
FUZZY_LOCK_MODES = ("none", "soft", "policy")


@dataclass(frozen=True)
class TmPolicy:
    """The resolved TM policy. `lock_ice` and `context_floor` are carried for
    completeness and forward-compatibility (ICE + the context band are deferred).

    Batch 15 T1 (SPEC-tm-match-step-config §6.1) adds `fill_threshold` (the min match %
    a tm_match step FILLS at; 100 = today's exact-only behaviour) and `fuzzy_lock_mode`
    (how a below-100 fuzzy fill locks: "none"|"soft"|"policy"). A step resolves its
    EFFECTIVE policy via resolve_policy_for_step (per-step override else the live
    global); resolve_policy keeps the global-only shape for seeding + non-step callers.
    """

    lock_ice: bool = True
    hundreds_enter_lqa: bool = True
    context_floor: int = 95
    fill_threshold: int = 100
    fuzzy_lock_mode: str = "soft"


def _as_bool(v, default: bool) -> bool:
    # Repo env-bool convention (config._get). None OR an empty/whitespace string means
    # "unset" -> take the DEFAULT direction (L1: an empty tm_hundreds_enter_lqa must NOT
    # read as "off" -> HARD; empty is the absence of a choice, so soft stays the default).
    if v is None:
        return default
    s = str(v).strip().lower()
    if s == "":
        return default
    return s not in ("0", "false", "no", "off")


def resolve_policy(session: Session, project: Project | None = None) -> TmPolicy:
    """Resolve the TM policy for a run. v1 = the GLOBAL settings interim (see the module
    header -- §6's per-TM grain is the target, not yet built); the `project` arg is
    accepted now so a per-project/per-TM override can land without a caller change.
    Settings are read at RUN time (§6: "settings changes take effect on the next matching
    run -- they never retroactively lock/unlock")."""
    from app.services import settings

    try:
        floor = int(settings.get(_SETTING_CONTEXT_FLOOR) or 95)
    except (TypeError, ValueError):
        floor = 95
    floor = max(50, min(99, floor))
    return TmPolicy(
        lock_ice=_as_bool(settings.get(_SETTING_LOCK_ICE), True),
        hundreds_enter_lqa=_as_bool(settings.get(_SETTING_HUNDREDS_ENTER_LQA), True),
        context_floor=floor,
        fill_threshold=_global_fill_threshold(settings),
        fuzzy_lock_mode=_global_fuzzy_lock_mode(settings),
    )


def _clamp_fill_threshold(v) -> int:
    """Bound a fill threshold to [50, 100] (mirrors the context_floor clamp). Junk/None
    -> 100 (exact-only, byte-identical to pre-Batch-15)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 100
    return max(50, min(100, n))


def _global_fill_threshold(settings) -> int:
    raw = settings.get(_SETTING_FILL_THRESHOLD)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return 100
    return _clamp_fill_threshold(raw)


def _norm_fuzzy_lock_mode(v, default: str = "soft") -> str:
    """Coerce a fuzzy-lock-mode value to one of FUZZY_LOCK_MODES; an unknown/blank value
    SELF-HEALS to `default` (like a garbage context_floor), never raises."""
    if v is None:
        return default
    s = str(v).strip().lower()
    return s if s in FUZZY_LOCK_MODES else default


def _global_fuzzy_lock_mode(settings) -> str:
    return _norm_fuzzy_lock_mode(settings.get(_SETTING_FUZZY_LOCK_MODE), "soft")


def resolve_policy_for_step(session: Session, project, step) -> TmPolicy:
    """The EFFECTIVE tm_match policy for ONE step (SPEC-tm-match-step-config §6.1,
    LAW-TM-1): per-step override column when non-NULL, else the live global. Removing an
    override (setting the column NULL) restores exact global-tracking behaviour. Reads
    each column via getattr so a partial/forward-compat step stub never raises. The five
    scalar knobs fall back to the global; `tm_pool_selection` is resolved separately by
    the orchestrator (§7)."""
    g = resolve_policy(session, project)

    def _col(name):
        return getattr(step, name, None)

    fill = _col("tm_fill_threshold")
    fill_threshold = _clamp_fill_threshold(fill) if fill is not None else g.fill_threshold

    mode = _col("tm_fuzzy_lock_mode")
    # An explicit per-step mode is validated (unknown -> the GLOBAL default, self-healing
    # like a garbage context_floor); NULL inherits the global.
    fuzzy_lock_mode = (
        _norm_fuzzy_lock_mode(mode, g.fuzzy_lock_mode) if mode is not None else g.fuzzy_lock_mode
    )

    lock_ice = _col("tm_lock_ice")
    lock_ice = _as_bool(lock_ice, g.lock_ice) if lock_ice is not None else g.lock_ice

    hundreds = _col("tm_hundreds_enter_lqa")
    hundreds_enter_lqa = (
        _as_bool(hundreds, g.hundreds_enter_lqa) if hundreds is not None else g.hundreds_enter_lqa
    )

    floor = _col("tm_context_floor")
    context_floor = max(50, min(99, int(floor))) if _is_intish(floor) else g.context_floor

    return TmPolicy(
        lock_ice=lock_ice,
        hundreds_enter_lqa=hundreds_enter_lqa,
        context_floor=context_floor,
        fill_threshold=fill_threshold,
        fuzzy_lock_mode=fuzzy_lock_mode,
    )


def _is_intish(v) -> bool:
    if v is None or isinstance(v, bool):
        return False
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


# --- lock provenance on the segment ------------------------------------------
def _stamp_lock(seg: Segment, *, band: str, hard: bool, tm_id, entry_id) -> None:
    """Set the persisted lock state + provenance on a segment. Provenance rides the
    per-unit `meta` JSON (a fresh dict so SQLAlchemy sees the JSON column change).

    L7: `matched_at` records when the lock was FIRST established -- so a re-lock of an
    already-locked segment with the SAME band+hard preserves the original timestamp
    (re-stamping would falsely claim the TM matched again). A genuine change (band/hard
    flips) refreshes it."""
    prev = (seg.meta or {}).get("tm_lock") or {}
    keep = (
        prev.get("matched_at")
        if prev.get("band") == band and bool(prev.get("hard")) == bool(hard)
        else None
    )
    seg.lock_band = band
    seg.lock_hard = hard
    meta = dict(seg.meta or {})
    meta["tm_lock"] = {
        "band": band,
        "hard": hard,
        "tm_id": tm_id,
        "entry_id": entry_id,
        "matched_at": keep or _utcnow_iso(),
    }
    seg.meta = meta


def clear_lock(seg: Segment) -> None:
    """Remove the lock state + provenance from a segment (the unlock write half)."""
    seg.lock_band = None
    seg.lock_hard = False
    if seg.meta and "tm_lock" in seg.meta:
        meta = dict(seg.meta)
        meta.pop("tm_lock", None)
        seg.meta = meta


def lock_summary(seg) -> dict | None:
    """A JSON/CLI-serialisable view of a segment's lock, or None when unlocked.
    getattr-guarded so a partial stub / forward-compat object without the lock columns
    never raises (this is a defensive serialization helper on a shared review path)."""
    band = getattr(seg, "lock_band", None)
    if not band:
        return None
    prov = (getattr(seg, "meta", None) or {}).get("tm_lock") or {}
    return {
        "band": band,
        "hard": bool(getattr(seg, "lock_hard", False)),
        "tm_id": prov.get("tm_id"),
        "entry_id": prov.get("entry_id"),
        "matched_at": prov.get("matched_at"),
    }


# --- TM drift (#617 item 5) ----------------------------------------------------
# A locked segment keeps `meta.tm_lock.tm_id`, the TM entry it was filled from. When that entry
# is edited afterwards the segment still holds the OLD target, silently. SegmentTM has no
# `updated_at`; its `activity_at` is bumped by every edit (tm_module.edit_entry, TMX import
# overwrite) but ALSO by every later use, so activity alone cannot say "edited". A segment is
# DRIFTED when its entry's activity is newer than the fill (`matched_at`) AND the entry's
# target no longer equals the segment's target. A pure usage bump leaves the targets equal, so
# it never shows the badge; a deleted entry is not reported (nothing to compare against).
def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def drifted(session: Session, segments) -> dict:
    """{segment.id: {"tm_id", "changed_at"}} for the locked `segments` whose TM entry changed
    since the fill. One bounded query for the whole list; segments without a lock or an integer
    `tm_id` are skipped."""
    wanted: dict = {}
    for seg in segments:
        if not getattr(seg, "lock_band", None):
            continue
        prov = (getattr(seg, "meta", None) or {}).get("tm_lock") or {}
        tm_id = prov.get("tm_id")
        if isinstance(tm_id, int) and not isinstance(tm_id, bool):
            wanted[seg.id] = (tm_id, prov.get("matched_at"), seg)
    if not wanted:
        return {}
    entries = {
        row.id: row
        for row in session.exec(
            select(SegmentTM).where(SegmentTM.id.in_({v[0] for v in wanted.values()}))
        ).all()
    }
    out: dict = {}
    for seg_id, (tm_id, matched_at, seg) in wanted.items():
        entry = entries.get(tm_id)
        if entry is None or entry.activity_at is None:
            continue
        filled = _parse_iso(matched_at)
        if filled is not None and entry.activity_at <= filled:
            continue
        if (entry.target_text or "").strip() == (seg.target_text or "").strip():
            continue
        out[seg_id] = {"tm_id": tm_id, "changed_at": entry.activity_at.isoformat()}
    return out


# --- the matching engine, as helpers the ONE caller (the orchestrator) uses ---
# There is a SINGLE tm_match engine: the orchestrator's segmented loop. It works on the
# run's in-memory `working[]`/`tm_origin[]` model and applies locks AFTER the single
# write-back, so it cannot be a straight-line "commit + lock" call. These helpers hold
# the SHARED decisions (candidacy + policy) so there is no second, divergent engine
# (the deleted `match_and_lock` was one, and it wrongly locked poisoned soft-gated hits
# because it gated on `outcome.flagged` instead of a clean status==MT commit).
def candidate_indices(segments, produced, already_locked) -> list[int]:
    """Segment indices a tm_match step may FILL: the STORED target is EMPTY (B4 -- an
    existing translation is never clobbered or locked), the slot was not already produced
    this run, and it is not already in this run's lock set. Reads the ORM segment's stored
    `target_text` (pre-run state during the loop, since the write-back is deferred).

    NOTE the honest edge (not an overclaim): "empty stored target" is the candidacy rule
    here; whether a NON-empty target is authoritative is not judged here. A segment an LQA
    round adjudicated to EMPTY (a withheld/quarantined value) is therefore a candidate --
    and it stays safe because the fill still goes through the write seam, so a poisoned
    100% hit is caught and NEVER locked (only a clean status==MT commit is locked). This
    function decides eligibility; the seam decides authenticity."""
    locked = set(already_locked or ())
    return [
        j
        for j in range(len(segments))
        if j not in produced
        and j not in locked
        and not ((getattr(segments[j], "target_text", "") or "").strip())
    ]


# --- LAW-TM-6 variable-content parity (SPEC-tm-match-step-config §6.2, finding 4) ----
# A below-100 fuzzy fill is allowed ONLY when the segment source and the hit source carry
# the IDENTICAL variable content -- the ordered multiset of numeric tokens, URLs, emails,
# and any surviving %s / {brace} variable literals -- and do NOT differ by case alone.
# The r0 "marker-multiset" claim was FALSE: tm.is_tm_eligible excludes every
# inline-token/placeholder-bearing source from TM entirely, so a ⟦xN⟧-marker string never
# gets a hit and the fillable universe is already token-free. The REAL hazard is NON-marker
# variable content in token-free text -- fuzz.ratio("Pay 900 now", "Pay 100 now") ~= 95 yet
# the FACT differs -- so this parity check (not the fuzzy score) is what stops a wrong
# number/URL from shipping. A divergence -> NOT fillable (the fuzzy fill drops to the
# context band, never a fill). Locale-pair byte-exactness (proposal D6) is preserved by the
# lookup's exact locale scoping; casing divergence is treated conservatively (a case-only
# difference is not a fill).
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
# printf ( %s %d %1$s ) + brace/named ( {count} {0} %(name)s ) variable literals that
# survived as literal text (token-bearing sources are already TM-excluded upstream).
_VAR_RE = re.compile(r"%\(\w+\)?[sdfgxac]|%\d*\$?[sdfgxac]|\{[^{}]*\}")


def _var_signature(text: str) -> tuple:
    """The ordered multiset (a tuple, order-preserving) of variable content in `text`:
    numbers, URLs, emails, and printf/brace variable literals. Two sources with the SAME
    signature carry the same facts; a divergence blocks a fuzzy fill (LAW-TM-6)."""
    t = text or ""
    return (
        tuple(_NUM_RE.findall(t)),
        tuple(_URL_RE.findall(t)),
        tuple(_EMAIL_RE.findall(t)),
        tuple(_VAR_RE.findall(t)),
    )


def variable_content_parity(seg_source: str, hit_source: str) -> bool:
    """True iff a below-100 fuzzy fill from `hit_source` onto `seg_source` is SAFE under
    LAW-TM-6: identical variable-content signatures AND not a case-only difference. A
    missing source (None) fails closed."""
    if seg_source is None or hit_source is None:
        return False
    if _var_signature(seg_source) != _var_signature(hit_source):
        return False
    # Case-only divergence (same letters, different case) is treated as non-fillable.
    return not (seg_source != hit_source and seg_source.lower() == hit_source.lower())


def plan_hit(
    hit, policy: TmPolicy, *, min_fill: int = 100, seg_source: str | None = None
) -> tuple[str, bool] | None:
    """(band, hard) for a fillable TM `hit` under `policy`, or None.

    `min_fill` (default 100 = today's exact-only behaviour) is the minimum match % this
    call fills at -- the empty-target FILL path passes the effective per-step threshold,
    while the HEAL / re-lock path passes 100 (EXACT ONLY, LAW-TM-20), so the below-100
    relaxation never reaches a non-empty target. A hit whose match_pct is below `min_fill`
    is not fillable.

      * match_pct >= 100 -> band "100" (ICE/context deferred); SOFT unless
        hundreds_enter_lqa is off (D3). Byte-identical to pre-Batch-15.
      * min_fill <= match_pct < 100 -> band "fuzzy" (below-100 fill, LAW-TM-5), GATED on
        the LAW-TM-6 variable-content parity of `seg_source` vs the hit source (a
        divergence -> None, fail-closed to the context band). `hard` follows the fuzzy
        lock mode: "policy" mirrors the 100-band hard bit, "none"/"soft" are never hard
        here (the orchestrator reads policy.fuzzy_lock_mode for the full none/soft/hard
        routing -- §6.3)."""
    if not hit:
        return None
    pct = hit.get("match_pct")
    if pct is None or pct < min_fill:
        return None
    if pct >= 100:
        return ("100", not policy.hundreds_enter_lqa)
    # below-100 fuzzy fill (only reachable when the caller lowered min_fill)
    if not variable_content_parity(seg_source, hit.get("source_text")):
        return None
    hard = (policy.fuzzy_lock_mode == "policy") and (not policy.hundreds_enter_lqa)
    return ("fuzzy", hard)


def lock_mode_for(band: str, policy: TmPolicy) -> str:
    """The lock disposition ("none" | "soft" | "hard") the orchestrator applies to a
    filled segment (§6.3). A 100/ICE band locks per hundreds_enter_lqa; a "fuzzy" band
    locks per the fuzzy lock mode."""
    if band == "fuzzy":
        mode = policy.fuzzy_lock_mode
        if mode == "none":
            return "none"
        if mode == "policy":
            return "hard" if not policy.hundreds_enter_lqa else "soft"
        return "soft"  # "soft" (the safe default)
    return "hard" if not policy.hundreds_enter_lqa else "soft"


def fill_or_relock(
    seg_target, hit, policy: TmPolicy, *, min_fill: int = 100, seg_source: str | None = None
) -> tuple[str, str, bool, str] | None:
    """Decide what a tm_match step does with ONE in-scope segment (not produced this run,
    not already locked) given its STORED target and its TM lookup `hit`. Returns
    ``(action, band, hard, lock_mode)`` or ``None``:

      * ("fill",   band, hard, lock_mode) -- STORED target EMPTY + a fillable hit
        (match_pct >= min_fill; a below-100 fuzzy fill also passes LAW-TM-6 parity): fill
        working[] with the hit target. `lock_mode` ("none"|"soft"|"hard") tells the
        orchestrator how to lock it (a fuzzy "none" fill is committed but NOT locked --
        §6.3). B4-safe (an empty slot is never an existing translation).
      * ("relock", band, hard, lock_mode) -- STORED target NON-EMPTY but already
        BYTE-EQUALS the hit's target (Q9): a prior TM-EXACT fill that lost its lock band.
        Re-lock is EXACT-ONLY BY CONSTRUCTION (L1): even if a caller passes a below-100
        min_fill, a "fuzzy"-band hit whose target coincidentally byte-equals a NON-empty
        stored target returns None here, never a re-lock -- re-lock exists only to restore a
        lost lock on a value the machine wrote as an EXACT fill, so a fuzzy coincidence is
        out of scope. (The orchestrator's re-lock scope passes min_fill=100 anyway, so this
        guard is belt-and-braces, not the only line.)
      * None -- below min_fill, no hit, a parity-failing fuzzy hit, OR a NON-EMPTY stored
        target that DIFFERS from the hit. B4: a different existing translation is NEVER
        clobbered and NEVER locked.

    F10: the equality is DELIBERATELY raw byte-equality, NOT Unicode-normalized (see the
    prior wave's note -- re-lock only restores a lost lock on a value the machine already
    wrote identically)."""
    plan = plan_hit(hit, policy, min_fill=min_fill, seg_source=seg_source)
    if plan is None:
        return None
    band, hard = plan
    mode = lock_mode_for(band, policy)
    stored = seg_target or ""
    if not stored.strip():
        return ("fill", band, hard, mode)
    if stored == hit.get("target_text"):
        # L1: re-lock is EXACT-ONLY by construction. A "fuzzy"-band hit whose target
        # coincidentally byte-equals a NON-empty stored target is NOT a re-lock (re-lock
        # restores a lost lock on a machine-written EXACT fill, never a fuzzy coincidence).
        if band == "fuzzy":
            return None
        return ("relock", band, hard, mode)
    return None


def clear_lock_on_human_edit(
    session: Session, seg: Segment, *, actor: str = "system", ip: str = ""
) -> bool:
    """A HUMAN edit landed on a locked segment (§5: "human edit wins, no stale lies").
    The CAT editor / key-grid saves are DIRECT writers (they do not pass through the
    machine seam, which would refuse a hard lock), so they call this to CLEAR the lock +
    provenance and audit it -- otherwise the segment would keep a band=100 / hard=True
    claim pointing at text a human just replaced. Idempotent: returns True iff a lock was
    actually cleared, False (no audit) when the segment was not locked. Does NOT commit
    (the save path owns the transaction)."""
    from app.services import audit

    if not seg.lock_band:
        return False
    prov = (seg.meta or {}).get("tm_lock") or {}
    clear_lock(seg)
    session.add(seg)
    audit.log(
        actor=actor or "system",
        action="tm.lock_cleared_by_edit",
        target=f"segment {seg.id}",
        detail=f"human edit cleared {prov.get('band')} lock (hard={prov.get('hard')})",
        ip=ip,
    )
    return True


def unlock_segment(session: Session, seg: Segment, *, actor: str = "system", ip: str = "") -> bool:
    """Unlock ONE segment (the reviewer+ audited action, §5). RBAC is the caller's job
    (route/CLI gate on reviewer+); this is the write + audit half. Returns True when a
    lock was actually cleared, False when the segment was not locked (no-op, no audit)."""
    from app.services import audit

    if not seg.lock_band:
        return False
    # #617 item 4 (gate): a run parked on human review holds this locale; refuse (409 on
    # web and /api, a CLI error) rather than change a segment the resume will write.
    from app.services import segment_review

    segment_review.refuse_if_awaiting_review(session, seg.document_id, seg.target_locale)
    prov = (seg.meta or {}).get("tm_lock") or {}
    clear_lock(seg)
    session.add(seg)
    # D-1b (§8): an EXPLICIT unlock is a pure-metadata transition (no text change), so it
    # would otherwise leave the key Delta feed silent -- emit a VALUE `updated` row. No-op
    # for a document segment.
    from app.services import keys as _keys

    _keys.log_value_lock_change(session, seg, actor=actor or "")
    session.commit()
    audit.log(
        actor=actor or "system",
        action="tm.unlock",
        target=f"segment {seg.id}",
        detail=f"cleared {prov.get('band')} lock (hard={prov.get('hard')})",
        ip=ip,
    )
    return True
