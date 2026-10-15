"""CAT editor service: per-segment edit/confirm, reviewer actions, comments, progress.

Save-per-segment on blur (PATCH per row) — never a batch form (avoids MateCat's
draft-lost-on-navigate trap). Locked rows are read-only and excluded from the progress
denominator and from the edited set.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from vstudio.models import (
    VComment,
    VProject,
    VProjectStatus,
    VQAIgnore,
    VSegment,
    VSegStatus,
)


class EditError(ValueError):
    """An illegal edit (a locked row, a cancelled project)."""


def _guard_writable(proj: VProject, seg: VSegment) -> None:
    if proj.status == VProjectStatus.CANCELLED:
        raise EditError("project is CANCELLED; editing is blocked.")
    if proj.status == VProjectStatus.DELIVERED:
        raise EditError("project is DELIVERED; editing is closed.")
    if seg.locked:
        raise EditError("segment is locked and read-only (returned as dispatched).")
    # #522: a segment already shipped in a partial delivery is final; later delivery skips
    # delivered ids, so an edit here could never reach the buyer.
    if seg.contract_id in set(proj.delivered_ids or []):
        raise EditError("segment was already delivered; it is closed to edits.")


def _guard_commentable(proj: VProject, seg: VSegment) -> None:
    """Comments are CONVERSATION, not content (Wave 3 Train E, §7.5 / HIGH-5). A locked
    segment is read-only for TEXT and is exactly the segment a translator most needs to ask
    about ('returned as dispatched -- why?'), so `locked` is NOT a bar here. A closed
    PROJECT still is: there is nobody left to answer, and a DELIVERED project's return has
    already been built."""
    if proj.status == VProjectStatus.CANCELLED:
        raise EditError("project is CANCELLED; commenting is blocked.")
    if proj.status == VProjectStatus.DELIVERED:
        raise EditError("project is DELIVERED; commenting is closed.")


def _same_text(a: str, b: str) -> bool:
    return (a or "").replace("\r\n", "\n") == (b or "").replace("\r\n", "\n")


def patch_target(
    session: Session,
    proj: VProject,
    seg: VSegment,
    target: str,
    actor: str = "",
    *,
    force: bool = False,
) -> VSegment:
    """Save one segment's target. Status becomes EDITED unless the text is unchanged from
    the prefill (then it stays PREFILLED/NEW). A previously CONFIRMED/APPROVED segment
    that is edited drops back to EDITED."""
    _guard_writable(proj, seg)
    if target is not None and not isinstance(target, str):
        raise EditError("target must be a string.")
    # #521: the browser saves on every blur. Unchanged text is a no-op, so it can never undo
    # a confirm/approve or clear a reviewer's reject tag. (`force` is for copy-source.)
    if not force and _same_text(target, seg.target):
        return seg
    seg.target = target or ""
    if (seg.target or "") == (seg.prefill or ""):
        seg.status = VSegStatus.PREFILLED if (seg.prefill or "").strip() else VSegStatus.NEW
    else:
        seg.status = VSegStatus.EDITED
    # A fresh edit clears a prior reviewer reject tag and confirmation.
    seg.issue_tag = ""
    seg.confirmed_by = ""
    seg.confirmed_at = None
    session.add(seg)
    # GATE-LAUNDERING FIX: a QA dismissal is scoped to the text it was granted for. Most
    # checks carry an empty detail_sig, so a persisted VQAIgnore would otherwise waive the
    # SAME check for ANY future text on this segment (dismiss a warning, then rewrite to
    # something worse, and the gate stays green). Clearing this segment's dismissals on
    # every edit forces the finding to be re-judged (and re-dismissed if still wanted).
    for row in session.exec(select(VQAIgnore).where(VQAIgnore.segment_id == seg.id)).all():
        session.delete(row)
    session.commit()
    session.refresh(seg)
    return seg


def copy_source(session: Session, proj: VProject, seg: VSegment, actor: str = "") -> VSegment:
    return patch_target(session, proj, seg, seg.source or "", actor=actor, force=True)


def confirm_segment(session: Session, proj: VProject, seg: VSegment, actor: str = "") -> VSegment:
    """Ctrl+Enter: confirm the segment (records who/when)."""
    _guard_writable(proj, seg)
    seg.status = VSegStatus.CONFIRMED
    seg.confirmed_by = actor
    seg.confirmed_at = datetime.utcnow()
    session.add(seg)
    session.commit()
    session.refresh(seg)
    return seg


def reviewer_approve(session: Session, proj: VProject, seg: VSegment, actor: str = "") -> VSegment:
    _guard_writable(proj, seg)  # M3: no approve on a locked seg / delivered/cancelled project
    seg.status = VSegStatus.APPROVED
    seg.issue_tag = ""
    session.add(seg)
    session.commit()
    session.refresh(seg)
    return seg


def reviewer_reject(
    session: Session, proj: VProject, seg: VSegment, issue_tag: str, actor: str = ""
) -> VSegment:
    """Reject → back to EDITED with an issue tag (translator picks it up)."""
    _guard_writable(proj, seg)  # M3: no reject on a locked seg / delivered/cancelled project
    seg.status = VSegStatus.EDITED
    seg.issue_tag = issue_tag or "issue"
    session.add(seg)
    session.commit()
    session.refresh(seg)
    return seg


def add_comment(
    session: Session,
    proj: VProject,
    seg: VSegment,
    *,
    author: str,
    role: str,
    body: str,
    share: bool = False,
    external_ref: str = "",
) -> VComment:
    """Add a persona comment (Wave 3 Train E, §7.5). Guarded by `_guard_commentable` (project
    status only -- a LOCKED segment stays commentable). `share` is the consent bit that lets
    the comment cross back on the return; `external_ref` (a buyer thread id) makes it a REPLY
    to that thread rather than a new one. A shared comment names BOTH audiences at the UI seam
    ('the buyer AND their end client will see this')."""
    _guard_commentable(proj, seg)
    c = VComment(
        segment_id=seg.id,
        author=author,
        role=role,
        body=body or "",
        direction="local",
        share=bool(share),
        external_ref=(str(external_ref).strip() or None),
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def comments_for(session: Session, seg: VSegment) -> list:
    return list(
        session.exec(
            select(VComment).where(VComment.segment_id == seg.id).order_by(VComment.created_at)
        ).all()
    )


def inbound_queries_for(session: Session, seg: VSegment) -> list:
    """Buyer queries carried in on the dispatch package (direction='in'), oldest first."""
    return list(
        session.exec(
            select(VComment)
            .where(VComment.segment_id == seg.id, VComment.direction == "in")
            .order_by(VComment.created_at)
        ).all()
    )


def local_comments_for(session: Session, seg: VSegment) -> list:
    """The persona's own comments (everything not carried in from the buyer)."""
    return list(
        session.exec(
            select(VComment)
            .where(VComment.segment_id == seg.id, VComment.direction != "in")
            .order_by(VComment.created_at)
        ).all()
    )


def progress(session: Session, proj: VProject) -> dict:
    """Progress = confirmed(+approved)/total non-locked segments."""
    segs = list(session.exec(select(VSegment).where(VSegment.project_id == proj.id)).all())
    non_locked = [s for s in segs if not s.locked]
    denom = len(non_locked)
    done = sum(1 for s in non_locked if s.status in (VSegStatus.CONFIRMED, VSegStatus.APPROVED))
    pct = int(round(100 * done / denom)) if denom else 100  # noqa: RUF046
    return {"done": done, "total": denom, "locked": len(segs) - denom, "pct": pct}
