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

WHAT IT DOES (ADR 0102, exact-only TM):
  * 100-band = source byte-exact (tm.lookup_batch match_pct 100).
  * ICE / 101-band = byte-exact AND in context (match_pct 101: the stored unit's context key
    equals the segment's, see tm_context): fill + HARD lock when `tm_lock_ice` is on (the
    default); with it off an in-context hit behaves as a 100.
  * a 100 fills + SOFT locks by default; HARD lock when policy.hundreds_enter_lqa is off.
  * multi-TM resolution rides tm.lookup_batch's own-row-first precedence; the winning
    entry's project supplies the policy (here: the reading project's global policy).
  * There is no fuzzy band any more: stored segments with lock_band 'fuzzy' or a tm_match_pct
    of 75-99 (written before ADR 0102) are read as "not leveraged" and never crash anything.

Documented in: docs/architecture/tm-and-glossary.md
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models import Project, Segment, SegmentTM


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


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


@dataclass(frozen=True)
class TmPolicy:
    """The resolved TM policy. `lock_ice`: an in-context (101) fill hard-locks (off = it
    behaves as a 100). `hundreds_enter_lqa`: a 100 fill locks SOFT (enters LQA) when on, HARD
    when off. A step resolves its EFFECTIVE policy via resolve_policy_for_step (per-step
    override else the live global); resolve_policy keeps the global-only shape for seeding and
    non-step callers."""

    lock_ice: bool = True
    hundreds_enter_lqa: bool = True


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
    header -- section 6's per-TM grain is the target, not yet built); the `project` arg is
    accepted now so a per-project/per-TM override can land without a caller change.
    Settings are read at RUN time (section 6: "settings changes take effect on the next
    matching run -- they never retroactively lock/unlock")."""
    from app.services import settings

    return TmPolicy(
        lock_ice=_as_bool(settings.get(_SETTING_LOCK_ICE), True),
        hundreds_enter_lqa=_as_bool(settings.get(_SETTING_HUNDREDS_ENTER_LQA), True),
    )


def resolve_policy_for_step(session: Session, project, step) -> TmPolicy:
    """The EFFECTIVE tm_match policy for ONE step (SPEC-tm-match-step-config section 6.1,
    LAW-TM-1): per-step override column when non-NULL, else the live global. Removing an
    override (setting the column NULL) restores exact global-tracking behaviour. Reads each
    column via getattr so a partial/forward-compat step stub never raises. (The removed fuzzy
    columns tm_fill_threshold / tm_fuzzy_lock_mode / tm_context_floor are dropped by alembic
    revision d6b3f8a2c4e1; ADR 0102.)"""
    g = resolve_policy(session, project)

    def _col(name):
        return getattr(step, name, None)

    lock_ice = _col("tm_lock_ice")
    lock_ice = _as_bool(lock_ice, g.lock_ice) if lock_ice is not None else g.lock_ice

    hundreds = _col("tm_hundreds_enter_lqa")
    hundreds_enter_lqa = (
        _as_bool(hundreds, g.hundreds_enter_lqa) if hundreds is not None else g.hundreds_enter_lqa
    )
    return TmPolicy(lock_ice=lock_ice, hundreds_enter_lqa=hundreds_enter_lqa)


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


def plan_hit(hit, policy: TmPolicy) -> tuple[str, bool] | None:
    """(band, hard) for a fillable TM `hit` under `policy`, or None.

    * match_pct >= 101 (in context) with `lock_ice` on -> band "ice", HARD.
    * match_pct >= 100 otherwise (a plain 100, or a 101 with lock_ice off) -> band "100";
      SOFT unless hundreds_enter_lqa is off (D3).
    * anything else (None, or a stale sub-100 value) -> None: nothing below 100 fills."""
    if not hit:
        return None
    pct = hit.get("match_pct")
    if pct is None or pct < 100:
        return None
    if pct >= 101 and policy.lock_ice:
        return ("ice", True)
    return ("100", not policy.hundreds_enter_lqa)


def lock_mode_for(band: str, policy: TmPolicy) -> str:
    """The lock disposition ("soft" | "hard") the orchestrator applies to a filled segment
    (section 6.3): an ICE band always hard-locks (that is what lock_ice means); a 100 locks per
    hundreds_enter_lqa."""
    if band == "ice":
        return "hard"
    return "hard" if not policy.hundreds_enter_lqa else "soft"


def fill_or_relock(seg_target, hit, policy: TmPolicy) -> tuple[str, str, bool, str] | None:
    """Decide what a tm_match step does with ONE in-scope segment (not produced this run,
    not already locked) given its STORED target and its TM lookup `hit`. Returns
    ``(action, band, hard, lock_mode)`` or ``None``:

      * ("fill",   band, hard, lock_mode) -- STORED target EMPTY + a fillable hit (an exact
        100 or in-context 101): fill working[] with the hit target. B4-safe (an empty slot
        is never an existing translation).
      * ("relock", band, hard, lock_mode) -- STORED target NON-EMPTY but already
        BYTE-EQUALS the hit's target (Q9): a prior TM fill that lost its lock band.
      * None -- no hit, or a NON-EMPTY stored target that DIFFERS from the hit. B4: a
        different existing translation is NEVER clobbered and NEVER locked.

    F10: the equality is DELIBERATELY raw byte-equality, NOT Unicode-normalized (re-lock only
    restores a lost lock on a value the machine already wrote identically)."""
    plan = plan_hit(hit, policy)
    if plan is None:
        return None
    band, hard = plan
    mode = lock_mode_for(band, policy)
    stored = seg_target or ""
    if not stored.strip():
        return ("fill", band, hard, mode)
    if stored == hit.get("target_text"):
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
