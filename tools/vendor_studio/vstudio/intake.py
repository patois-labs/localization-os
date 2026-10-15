"""Secure intake: turn a verified dispatch contract into a VProject + ordered VSegments.

The HMAC verification happens in the route (security.verify_signature over the RAW body)
BEFORE this module ever sees a dict. Here we only parse a trusted-shape payload.

GRACEFUL DEGRADATION (the VP-0 gap): the CURRENT dispatch contract carries
id/source/target/state/tm_match_pct/placeholders + optional assets.glossary. It does NOT
yet carry per-segment provenance/locked/locked_reason/char_limit or a package glossary in
the enriched shape. This module reads the enriched fields WHEN PRESENT and INFERS what it
can otherwise, so the model + UI light up for a VP-0 dispatch and degrade cleanly for the
current one.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime

from sqlmodel import Session, select

from vstudio import contract as C
from vstudio.models import (
    Provenance,
    VComment,
    VProject,
    VProjectStatus,
    VSegment,
    VSegStatus,
    WireLog,
)


class IntakeError(ValueError):
    """A dispatch payload that cannot be turned into a project (bad shape)."""


class IntakeConflict(ValueError):
    """L4: the SAME claim token re-arrived with a DIFFERENT payload."""


def fingerprint(payload: dict) -> str:
    """A stable fingerprint of the dispatched content: target locale + the ordered
    (id, source) pairs. Independent of asset ordering / cosmetic fields."""
    segs = payload.get("segments") or []
    parts = [str(payload.get("target_locale") or "")]
    for seg in segs:
        if isinstance(seg, dict):
            parts.append(C.norm_id(seg.get("id")) + "\x1f" + str(seg.get("source") or ""))
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def _infer_provenance(seg: dict) -> tuple[str, float | None]:
    """(provenance, match_score). Prefer explicit VP-0 fields; else infer from the
    current contract's tm_match_pct + state."""
    prov = str(seg.get("provenance") or "").strip().lower()
    score = seg.get("match_score")
    pct = seg.get("tm_match_pct")
    if prov in (Provenance.TM_EXACT, Provenance.TM_FUZZY, Provenance.MT, Provenance.NEW):
        try:
            return prov, (
                float(score) if score is not None else (float(pct) if pct is not None else None)
            )
        except (TypeError, ValueError):
            return prov, None
    # Inference from the current contract.
    if pct is not None:
        try:
            p = float(pct)
        except (TypeError, ValueError):
            p = None
        if p is not None:
            if p >= 100:
                return Provenance.TM_EXACT, p
            if p > 0:
                return Provenance.TM_FUZZY, p
    if (seg.get("target") or "").strip():
        # A prefilled target with no TM band = machine translation to post-edit.
        return Provenance.MT, None
    return Provenance.NEW, None


def _seg_status(prov: str, target: str) -> str:
    return VSegStatus.PREFILLED if (target or "").strip() else VSegStatus.NEW


def normalize_glossary(assets: dict) -> list:
    """The package glossary as a list of term dicts. Tolerates a list of dicts, or the
    rendered-string form vendor_dispatch may send (which we cannot structure -> [])."""
    gl = (assets or {}).get("glossary")
    if isinstance(gl, list):
        return [g for g in gl if isinstance(g, dict)]
    return []


def parse_contract(payload: dict) -> dict:
    """Validate the top-level shape and return a normalized intake dict. Raises
    IntakeError on a structurally invalid payload."""
    if not isinstance(payload, dict):
        raise IntakeError("dispatch payload must be a JSON object.")
    claim = str(payload.get("claim_token") or "").strip()
    if not claim:
        raise IntakeError("dispatch payload carries no claim_token.")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise IntakeError("dispatch payload must carry a non-empty segments array.")
    seen: set[str] = set()
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict) or "id" not in seg:
            raise IntakeError(f"segments[{i}] must be an object with an id.")
        # #522: 5 and "5" are the same id on the wire (norm_id), so they collide too.
        nid = C.norm_id(seg["id"])
        if nid in seen:
            raise IntakeError(f"segments[{i}] has a duplicate segment id: {nid}.")
        seen.add(nid)
    return payload


def store_dispatch(session: Session, payload: dict, *, endpoint_host: str = "") -> VProject:
    """Create the VProject + VSegment rows for a verified dispatch. Idempotent on the
    claim token: a re-POST of the same dispatch returns the existing project rather than
    duplicating it (a vendor endpoint may legitimately be retried)."""
    parsed = parse_contract(payload)
    claim = str(parsed["claim_token"]).strip()
    fp = fingerprint(parsed)

    existing = session.exec(select(VProject).where(VProject.claim_token == claim)).first()
    if existing is not None:
        # L4: an identical re-POST is an idempotent retry; a DIFFERENT payload for the
        # same claim token is a conflict (never silently discarded as "accepted").
        if (existing.dispatch_fingerprint or "") and existing.dispatch_fingerprint != fp:
            raise IntakeConflict(
                "a dispatch with this claim token already exists with different content."
            )
        return existing

    assets = parsed.get("assets") or {}
    # Vendor Wave E — QUOTE GATE (R3): a quote-gated dispatch carries top-level
    # "quote_requested": true. Absent/false => not gated (backward-compatible default).
    quote_requested = bool(parsed.get("quote_requested"))
    # Vendor Wave E — REWORK (R4): a rework dispatch carries a top-level "rework" block
    # {round, is_rework, note, prior_run_id, prior_dispatch_id}. We store it verbatim (as a
    # dict) and flag the project. A rework arrives with a FRESH claim token, so it creates a
    # NEW project through the ordinary intake path (no idempotency conflict).
    rework_block = parsed.get("rework")
    rework_block = rework_block if isinstance(rework_block, dict) else None
    proj = VProject(
        contract_version=str(parsed.get("contract_version") or ""),
        run_ref=str(parsed.get("run_id") if parsed.get("run_id") is not None else ""),
        step_key=str(parsed.get("step_key") or ""),
        step_name=str(parsed.get("step_name") or ""),
        claim_token=claim,
        customer_label=str(parsed.get("customer_label") or parsed.get("customer") or "").strip(),
        source_locale=str(parsed.get("source_locale") or ""),
        target_locale=str(parsed.get("target_locale") or ""),
        status=VProjectStatus.NEW,
        estimate=(
            parsed.get("estimate")
            if isinstance(parsed.get("estimate"), dict)
            else (assets.get("estimate") if isinstance(assets.get("estimate"), dict) else {})
        ),
        glossary=normalize_glossary(assets),
        style_guide=str(assets.get("style_guide") or ""),
        context=str(assets.get("context") or ""),
        expiry_at=_parse_dt(parsed.get("expiry") or parsed.get("expires_at")),
        dispatch_fingerprint=fp,
        quote_requested=quote_requested,
        quote_status=("requested" if quote_requested else ""),
        is_rework=rework_block is not None,
        rework=rework_block,
    )
    session.add(proj)
    session.commit()
    session.refresh(proj)

    for i, seg in enumerate(parsed["segments"]):
        prov, score = _infer_provenance(seg)
        target = str(seg.get("target") or "")
        locked = bool(seg.get("locked"))
        # Vendor Wave E — REWORK (R4): per-segment buyer feedback {issues, note}. Only a dict
        # is stored (a malformed value degrades to None rather than corrupting the row).
        fb = seg.get("feedback")
        fb = fb if isinstance(fb, dict) else None
        vseg = VSegment(
            project_id=proj.id,
            contract_id=C.norm_id(seg.get("id")),
            order_index=i,
            source=str(seg.get("source") or ""),
            target=target,
            prefill=target,
            dispatch_state=str(seg.get("state") or ""),
            status=(VSegStatus.NEW if locked else _seg_status(prov, target)),
            provenance=prov,
            match_score=score,
            locked=locked,
            locked_reason=str(seg.get("locked_reason") or ""),
            placeholders=list(seg.get("placeholders") or []),
            char_limit=_int_or_none(seg.get("char_limit")),
            feedback=fb,
        )
        session.add(vseg)
    session.commit()
    _store_inbound_queries(session, proj, parsed["segments"])
    session.refresh(proj)
    return proj


def _assemble_query_body(q: dict) -> str:
    """One human-readable body for an inbound buyer thread: its subject + each post,
    author-role-tagged. NO buyer email is ever in the package (only coarse roles), so this
    text carries none either."""
    lines = []
    subject = str(q.get("subject") or "").strip()
    if subject:
        lines.append(f"Query: {subject}")
    for p in q.get("posts") or []:
        if not isinstance(p, dict):
            continue
        body = str(p.get("body") or "").strip()
        if not body:
            continue
        role = str(p.get("author_role") or "buyer").strip() or "buyer"
        lines.append(f"[{role}] {body}")
    return "\n".join(lines) if lines else (subject or "Query")


def _store_inbound_queries(session: Session, proj: VProject, segments: list) -> None:
    """Wave 3 Train E (§7.2): persist the buyer queries carried on the dispatch package as
    VComment rows with direction='in' and external_ref = the buyer's thread id, so the editor
    renders them above the local comment list marked as coming from the buyer. Best-effort +
    idempotent: a re-POST of the same leg (store_dispatch returns the existing project before
    this runs) never reaches here, but we still de-dup on (segment, external_ref) so a manual
    replay cannot double the rows."""
    # L4: genuinely best-effort. The VProject + VSegment rows are ALREADY committed by the
    # caller, so a failure to persist inbound queries must NEVER fail the intake (the dispatch
    # is stored and workable; the buyer queries are an enrichment). Any error rolls back only
    # this uncommitted batch and is swallowed.
    try:
        by_cid = {
            s.contract_id: s
            for s in session.exec(select(VSegment).where(VSegment.project_id == proj.id)).all()
        }
        added = False
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            queries = seg.get("queries")
            if not isinstance(queries, list) or not queries:
                continue
            vseg = by_cid.get(C.norm_id(seg.get("id")))
            if vseg is None:
                continue
            for q in queries:
                if not isinstance(q, dict):
                    continue
                ref = str(q.get("ref") or "").strip() or None
                if ref is not None:
                    dup = session.exec(
                        select(VComment).where(
                            VComment.segment_id == vseg.id,
                            VComment.direction == "in",
                            VComment.external_ref == ref,
                        )
                    ).first()
                    if dup is not None:
                        continue
                session.add(
                    VComment(
                        segment_id=vseg.id,
                        author="buyer",
                        role="buyer",
                        body=_assemble_query_body(q),
                        direction="in",
                        external_ref=ref,
                    )
                )
                added = True
        if added:
            session.commit()
    except Exception:
        session.rollback()


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --- status ladder -----------------------------------------------------------
def can_transition(current: str, target: str) -> bool:
    """Forward-only ladder + CANCELLED as a terminal off-ladder sink. A project may move
    forward one-or-more steps (e.g. NEW -> IN_PROGRESS on claim) but never backward, and
    never out of a terminal state."""
    if current in (VProjectStatus.DELIVERED, VProjectStatus.CANCELLED):
        return False
    if target == VProjectStatus.CANCELLED:
        return True
    ladder = VProjectStatus.LADDER
    if current not in ladder or target not in ladder:
        return False
    return ladder.index(target) > ladder.index(current)


def set_status(session: Session, proj: VProject, target: str) -> None:
    """Apply a status transition, raising ValueError if the ladder forbids it."""
    if proj.status == target:
        return
    if not can_transition(proj.status, target):
        raise ValueError(f"illegal status transition {proj.status} -> {target}.")
    proj.status = target
    session.add(proj)
    session.commit()


# --- TM-band breakdown (always derivable from the segments) ------------------
def band_breakdown(segments: list) -> list:
    """A human-readable TM-band table derived from the segments' provenance. Each row:
    {band, segments, words, covered, cost}. `cost` is None unless the dispatch carried
    an estimate (the current contract does not), so the UI shows counts always and cost
    only when present — the "light up when present" contract."""
    bands = {
        Provenance.TM_EXACT: {"band": "TM 100%", "covered": True},
        Provenance.TM_FUZZY: {"band": "TM fuzzy", "covered": False},
        Provenance.MT: {"band": "MT", "covered": False},
        Provenance.NEW: {"band": "New", "covered": False},
    }
    rows = {
        k: {"band": v["band"], "segments": 0, "words": 0, "covered": v["covered"], "cost": None}
        for k, v in bands.items()
    }
    for seg in segments:
        prov = getattr(seg, "provenance", "") or Provenance.NEW
        if prov not in rows:
            prov = Provenance.NEW
        rows[prov]["segments"] += 1
        rows[prov]["words"] += len((getattr(seg, "source", "") or "").split())
        if getattr(seg, "locked", False):
            rows[prov]["covered"] = True
    return [r for r in rows.values() if r["segments"] > 0]


# --- wire log ----------------------------------------------------------------
#: H2: a hard row cap on the WireLog table (a 512MB volume must not fill with rows an
#: unauthenticated caller can generate). When exceeded, the oldest rows are pruned.
WIRELOG_ROW_CAP = 50_000
#: H2: per-source coalescing for UNAUTHENTICATED rejected intakes — at most this many rows
#: per source per minute; the rest are dropped (a bad-signature flood cannot write a row
#: per request). Process-local best-effort — correct only because Vendor Studio runs as a
#: SINGLE process (one replica, no autoscale); a multi-machine deploy would need a
#: shared counter. The single-process constraint is load-bearing here.
UNAUTH_MAX_PER_MINUTE = 20
_UNAUTH_BUCKET: dict = {}


def _rate_ok(source: str) -> bool:
    """True if an unauthenticated rejected-intake row may be written for `source` this
    minute (H2). Coalesces the flood to UNAUTH_MAX_PER_MINUTE per source per minute."""
    minute = int(time.time() // 60)
    key = (source or "?", minute)
    # Drop stale buckets so the dict cannot grow unbounded either.
    for k in [k for k in _UNAUTH_BUCKET if k[1] < minute - 1]:
        _UNAUTH_BUCKET.pop(k, None)
    count = _UNAUTH_BUCKET.get(key, 0)
    if count >= UNAUTH_MAX_PER_MINUTE:
        return False
    _UNAUTH_BUCKET[key] = count + 1
    return True


def log_wire(
    session: Session,
    *,
    direction: str,
    kind: str,
    verdict: str,
    reason: str = "",
    status_code: int | None = None,
    project_id: int | None = None,
    rate_source: str | None = None,
) -> None:
    """Record a wire event with a SANITIZED reason (callers must never pass a secret or
    claim token). Best-effort; a logging failure never breaks the request.

    `rate_source` (H2): when set, the row is an UNAUTHENTICATED reject and is coalesced to
    UNAUTH_MAX_PER_MINUTE per source per minute so a signature-flood cannot bloat the table.
    Every write also prunes the table back under WIRELOG_ROW_CAP (oldest first)."""
    if rate_source is not None and not _rate_ok(rate_source):
        return
    try:
        session.add(
            WireLog(
                direction=direction,
                kind=kind,
                verdict=verdict,
                reason=reason[:500],
                status_code=status_code,
                project_id=project_id,
            )
        )
        session.commit()
        _prune(session)
    except Exception:
        session.rollback()


def _prune(session: Session) -> None:
    """Keep the WireLog table under the row cap (oldest first). Cheap: only acts when the
    count actually exceeds the cap."""
    from sqlalchemy import func

    try:
        total = session.exec(select(func.count()).select_from(WireLog)).one()
        total = total[0] if isinstance(total, tuple) else total
        if total <= WIRELOG_ROW_CAP:
            return
        overflow = total - WIRELOG_ROW_CAP
        oldest = session.exec(select(WireLog).order_by(WireLog.id).limit(overflow)).all()
        for row in oldest:
            session.delete(row)
        session.commit()
    except Exception:
        session.rollback()
