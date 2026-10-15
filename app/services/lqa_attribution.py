"""Vendor attribution for a scored LQA card (SPEC-wave2-qa-pe-reporting §3.1.7).

A pure helper: given the run + locale + time a card was scored, which vendor dispatch leg
(if any) produced the content that was scored? Reached only service->service (score_document
and the two orchestrator LQA persist sites), so it is NOT part of the kernel facade surface.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from sqlmodel import Session, select

from app.models import VendorDispatch, VendorDispatchStatus


def vendor_for_scored_content(
    session: Session, *, workflow_run_id, target_locale, scored_at
) -> tuple[int | None, int | None]:
    """(vendor_id, dispatch_id) of the leg whose RETURNED content this card scored, else
    (None, None).

    Rule: the MOST RECENT VendorDispatch leg for (workflow_run_id, target_locale) with
    `status == RETURNED` and `returned_at <= scored_at`. Nothing is invented:

      * a leg in any non-RETURNED status never attributes — matching Wave 1's money rule
        (reports.py:899-907), which sums only RETURNED legs;
      * score_document's ad-hoc path has no workflow_run_id, so it always yields
        (None, None) — no run means no leg to attribute to;
      * a two-leg rework attributes to the leg whose returned_at is the latest AT-OR-BEFORE
        scored_at;
      * a leg that returns AFTER the card was written does not appear on that card's rows —
        they are already written and are never updated (§3.1.6).

    KNOWN over-attribution, disclosed not hidden (§4.6): a workflow LQA finding is
    document-level (segment_id=None) while a leg's contract_ids may cover only a SUBSET of
    the document, and this time bound cannot see intervening mutations (a fix step or human
    edit between returned_at and scored_at). Both are stated in _vendor_caveats + the docs.
    """
    if workflow_run_id is None or target_locale is None or scored_at is None:
        return (None, None)
    legs = session.exec(
        select(VendorDispatch).where(
            VendorDispatch.workflow_run_id == workflow_run_id,
            VendorDispatch.target_locale == target_locale,
            VendorDispatch.status == VendorDispatchStatus.RETURNED,
        )
    ).all()
    best = None
    for leg in legs:
        if leg.returned_at is None or leg.returned_at > scored_at:
            continue
        if best is None or leg.returned_at > best.returned_at:
            best = leg
    if best is None:
        return (None, None)
    return (best.vendor_id, best.id)
