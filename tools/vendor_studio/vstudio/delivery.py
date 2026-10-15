"""Delivery: build the signed return per the native contract and POST it to Localization OS.

CONTRACT LAWS honoured here (from vendor_return.check_contract):
  * every dispatched id returns EXACTLY ONCE, cumulatively across partial tranches;
  * the return carries the MANDATORY claim_token binding it to the leg;
  * locked / unedited ids are returned AS DISPATCHED (their target == prefill), never
    fabricated and never dropped.

The return builder NEVER invents an id: it only emits ids that were dispatched (the
VSegment.contract_id set). Autopilot's adversarial scenarios corrupt a COPY of a
correctly-built payload, so the builder itself stays honest.
"""

from __future__ import annotations

import json

import httpx
from sqlmodel import Session, select

from vstudio import contract as C
from vstudio.config import settings
from vstudio.intake import log_wire
from vstudio.models import VComment, VProject, VProjectStatus, VSegment, VSegStatus
from vstudio.security import compute_signature

#: The buyer's return-admission cap (vendor_return: 10 comments per segment). Ship at most
#: this many so a consented batch can never make the WHOLE return refuse for over-cap.
_SHARE_CAP = 10


def _shared_comments_for(session: Session, seg: VSegment) -> tuple[list, int]:
    """(comments, omitted) for a segment: the vendor-authored comments a persona ticked to
    share back (§7.5). Excludes buyer-inbound echoes (direction='in') and unshared internal
    notes. `omitted` counts consented comments NOT shipped -- so the drop is DISCLOSED, never
    silent (gate M6):
      * beyond the _SHARE_CAP the buyer would refuse the whole return over (capped here);
      * a malformed REPLY -- external_ref set but non-numeric -- which is DROPPED rather than
        silently re-filed as a NEW thread (gate L5: a failed reply must never become a new
        customer-visible thread). The UI only ever sets a numeric buyer thread id or an empty
        ref, so this guards data corruption."""
    rows = list(
        session.exec(
            select(VComment)
            .where(
                VComment.segment_id == seg.id,
                VComment.direction != "in",
                VComment.share == True,  # noqa: E712
            )
            .order_by(VComment.created_at)
        ).all()
    )
    out: list = []
    omitted = 0
    for c in rows:
        ext = (c.external_ref or "").strip()
        if ext and not ext.isdigit():
            # A malformed reply target: do NOT silently open a new thread with it (L5).
            omitted += 1
            continue
        if len(out) >= _SHARE_CAP:
            omitted += 1
            continue
        out.append(
            {
                "body": c.body or "",
                "author_label": (c.author or c.role or ""),
                "ref": str(c.id),
                "reply_to": (int(ext) if ext else None),
            }
        )
    return out, omitted


def project_segments(session: Session, proj: VProject) -> list:
    return list(
        session.exec(
            select(VSegment).where(VSegment.project_id == proj.id).order_by(VSegment.order_index)
        ).all()
    )


def outstanding_ids(session: Session, proj: VProject) -> list:
    """Dispatched contract ids not yet delivered in an earlier tranche, in order."""
    done = set(proj.delivered_ids or [])
    return [s.contract_id for s in project_segments(session, proj) if s.contract_id not in done]


def build_return_payload(
    session: Session,
    proj: VProject,
    ids: list,
    *,
    cost: float | None = None,
    currency: str | None = None,
    claim_override: str | None = None,
) -> dict:
    """Build the return contract for the given contract ids. Each segment's target is
    exactly its current stored target (== prefill for a locked/unedited row, so it is
    returned as dispatched)."""
    by_id = {s.contract_id: s for s in project_segments(session, proj)}
    segs = []
    for cid in ids:
        seg = by_id.get(cid)
        if seg is None:
            continue  # never emit an id we did not dispatch
        entry = {"id": seg.contract_id, "target": seg.target or ""}
        # Wave 3 Train E (§7.5): the vendor->buyer comment carry. ONLY comments a persona
        # explicitly ticked "share" cross back (default OFF: a vendor's internal notes never
        # ship), and NEVER a buyer-inbound echo (direction='in'). `ref` is the vendor's own
        # comment id (-> QueryPost.external_id); `reply_to` = the buyer thread id when the
        # comment replied to one (external_ref), else null (opens a new buyer thread).
        comments, comments_omitted = _shared_comments_for(session, seg)
        if comments:
            entry["comments"] = comments
        # M6: disclose consented comments that did NOT ship (over the buyer's per-segment cap,
        # or a malformed reply target) so the persona -- and the buyer's return report -- see
        # the count. Sibling keys on the segment entry; check_contract ignores unknown keys.
        if comments_omitted:
            entry["comments_truncated"] = True
            entry["comments_omitted"] = comments_omitted
        segs.append(entry)
    payload = {
        "contract_version": C.CONTRACT_VERSION,
        "claim_token": (claim_override if claim_override is not None else proj.claim_token),
        "target_locale": proj.target_locale,
        "segments": segs,
    }
    if cost is not None:
        payload["actual_cost_usd"] = float(cost)
        payload["actual_currency"] = currency or proj.actual_currency or "USD"
    return payload


def post_return(
    payload: dict,
    claim: str,
    *,
    sign: bool = True,
    secret: str | None = None,
    base_url: str | None = None,
    timeout: float = 20.0,
) -> httpx.Response:
    """Sign (HMAC over the RAW body) and POST the return to Localization OS's webhook.

    `sign=False` (autopilot's bad_signature scenario) omits the signature header.
    The raw body signed is the EXACT bytes sent — never a re-serialization."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        C.CLAIM_HEADER: claim or "",
        C.CONTRACT_VERSION_HEADER: C.CONTRACT_VERSION,
    }
    if sign:
        headers[C.SIGNATURE_HEADER] = compute_signature(
            settings.signing_secret if secret is None else secret, raw
        )
    url = (base_url or settings.locstudio_url).rstrip("/") + settings.return_webhook_path
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        return client.post(url, content=raw, headers=headers)


def build_quote_payload(
    claim: str, quoted_cost_usd: float, *, currency: str = "USD", note: str = ""
) -> dict:
    """The quote-submission body (Vendor Wave E / R3). Localization OS's /webhooks/vendor-quotes
    binds it to the leg by the claim token; the cost is what the PM quotes before starting."""
    payload = {
        "type": "quote",
        "message_version": "1.0",
        "claim_token": claim or "",
        "quoted_cost_usd": float(quoted_cost_usd),
        "currency": (currency or "USD"),
    }
    if note:
        payload["note"] = str(note)
    return payload


def post_quote(
    payload: dict,
    claim: str,
    *,
    sign: bool = True,
    secret: str | None = None,
    base_url: str | None = None,
    timeout: float = 20.0,
) -> httpx.Response:
    """Sign (HMAC over the RAW body) and POST a quote to Localization OS's quote webhook.

    Mirrors post_return: the signing header + claim header are identical, only the URL path
    (quote_webhook_path) and body differ. The raw body signed is the EXACT bytes sent."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        C.CLAIM_HEADER: claim or "",
        C.CONTRACT_VERSION_HEADER: C.CONTRACT_VERSION,
    }
    if sign:
        headers[C.SIGNATURE_HEADER] = compute_signature(
            settings.signing_secret if secret is None else secret, raw
        )
    url = (base_url or settings.locstudio_url).rstrip("/") + settings.quote_webhook_path
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        return client.post(url, content=raw, headers=headers)


def submit_quote(
    session: Session,
    proj: VProject,
    quoted_cost_usd: float,
    *,
    currency: str = "USD",
    note: str = "",
    poster=None,
) -> dict:
    """Submit the PM's quote for a quote-gated project (Vendor Wave E / R3). Builds + signs
    the quote, POSTs it to Localization OS, and — on a 2xx — records quoted_cost_usd and flips
    quote_status to "submitted". `poster` (test seam) defaults to post_quote.

    Raises ValueError for a structural refusal (project not quote-gated / already approved)."""
    if not proj.quote_requested:
        raise ValueError("project is not quote-gated; no quote to submit.")
    if proj.quote_status == "approved":
        raise ValueError("quote already approved; resubmission is blocked.")
    poster = poster or post_quote
    payload = build_quote_payload(proj.claim_token, quoted_cost_usd, currency=currency, note=note)
    try:
        resp = poster(payload, proj.claim_token)
    except httpx.HTTPError as exc:
        log_wire(
            session,
            direction="out",
            kind="quote",
            verdict="error",
            reason=f"transport error: {type(exc).__name__}",
            project_id=proj.id,
        )
        raise
    ok = 200 <= resp.status_code < 300
    body = _safe_json(resp)
    verdict = "submitted" if ok else "rejected"
    reason = _sanitize_reason(body, resp.status_code)
    log_wire(
        session,
        direction="out",
        kind="quote",
        verdict=verdict,
        reason=reason,
        status_code=resp.status_code,
        project_id=proj.id,
    )
    if ok:
        proj.quoted_cost_usd = float(quoted_cost_usd)
        proj.quote_status = "submitted"
        session.add(proj)
        session.commit()
        session.refresh(proj)
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "response": body,
        "quote_status": proj.quote_status,
        "quoted_cost_usd": proj.quoted_cost_usd,
    }


def require_quote_cleared(proj: VProject) -> None:
    """The quote gate (#522): a quote-requested dispatch may not be claimed, delivered or
    completed until Localization OS has APPROVED the quote. Raises ValueError otherwise."""
    if proj.quote_requested and proj.quote_status != "approved":
        raise ValueError(
            f"quote is {proj.quote_status or 'requested'}; the quote must be approved first."
        )


def _confirmed_ids(session: Session, proj: VProject, done: set) -> list:
    """Contract ids of CONFIRMED/APPROVED, non-locked, not-yet-delivered segments —
    computed SERVER-SIDE (a caller may never choose the delivered set)."""
    return [
        s.contract_id
        for s in project_segments(session, proj)
        if s.status in (VSegStatus.CONFIRMED, VSegStatus.APPROVED)
        and not s.locked
        and s.contract_id not in done
    ]


def deliver(
    session: Session,
    proj: VProject,
    *,
    mode: str = "full",
    cost: float | None = None,
    currency: str | None = None,
    poster=None,
) -> dict:
    """Deliver a tranche to Localization OS. `mode`:
      * "full"    — every still-outstanding dispatched id (the completing delivery).
      * "partial" — only the CONFIRMED/APPROVED subset, computed server-side.

    QA is ADVISORY-ONLY (decided 2026-08-08 — see the decision record in `qa_engine.py`):
    delivery NEVER refuses on QA findings. Instead we compute a findings SNAPSHOT over exactly
    the delivered segments and record it on the delivery for the audit trail (returned as
    `qa_snapshot` and noted in the outbound WireLog reason). Enforcement lives on Localization OS's
    buyer-side admission seam, which flags a placeholder-broken / glossary-violating return
    needs_review on admit. Updates the cumulative delivered-id ledger, records the wire
    verdict, and flips the project DELIVERED when the completing tranche is accepted. `poster`
    (test seam) defaults to post_return.

    Raises ValueError only for a structural refusal (nothing to deliver / cancelled)."""
    if proj.status == VProjectStatus.CANCELLED:
        raise ValueError("project is CANCELLED; delivery is blocked.")
    require_quote_cleared(proj)
    poster = poster or post_return
    valid = {s.contract_id for s in project_segments(session, proj)}
    done = set(proj.delivered_ids or [])
    if mode == "partial":
        tranche = [cid for cid in _confirmed_ids(session, proj, done)]  # noqa: C416
        if not tranche:
            raise ValueError("no confirmed segments to deliver.")
    else:
        tranche = [cid for cid in outstanding_ids(session, proj)]  # noqa: C416
        if not tranche:
            raise ValueError("no outstanding ids to deliver.")
    tranche = [cid for cid in tranche if cid in valid and cid not in done]
    if not tranche:
        raise ValueError("no outstanding ids to deliver.")

    # ADVISORY QA SNAPSHOT over exactly the segments being delivered (a partial tranche is
    # judged on its own segments, not the whole job). Locked/unedited-as-dispatched rows are
    # excluded by the checks themselves. This NEVER refuses — it is context recorded on the
    # delivery for the audit trail (the enforcing side is Localization OS's admission seam).
    from vstudio import qa_engine

    tranche_set = set(tranche)
    seg_ids = {s.id for s in project_segments(session, proj) if s.contract_id in tranche_set}
    qa_snapshot = qa_engine.gate(session, proj, only_ids=seg_ids)

    payload = build_return_payload(session, proj, tranche, cost=cost, currency=currency)
    try:
        resp = poster(payload, proj.claim_token)
    except httpx.HTTPError as exc:
        log_wire(
            session,
            direction="out",
            kind="return",
            verdict="error",
            reason=f"transport error: {type(exc).__name__}",
            project_id=proj.id,
        )
        raise
    ok = 200 <= resp.status_code < 300
    body = _safe_json(resp)
    verdict = "accepted" if ok else "rejected"
    reason = _sanitize_reason(body, resp.status_code)
    if qa_snapshot["open"]:
        # Record the advisory findings count on the delivery's audit row (never a secret).
        reason = f"{reason}; QA advisory: {qa_snapshot['open']} open finding(s) at delivery"
    log_wire(
        session,
        direction="out",
        kind="return",
        verdict=verdict,
        reason=reason,
        status_code=resp.status_code,
        project_id=proj.id,
    )
    if ok:
        proj.delivered_ids = list(done | set(tranche))
        # DELIVERED only once every dispatched id has been accepted cumulatively.
        remaining = [cid for cid in valid if cid not in set(proj.delivered_ids)]
        if cost is not None:
            proj.actual_cost_usd = float(cost)
            if currency:
                proj.actual_currency = currency
        if not remaining:
            from datetime import datetime

            proj.status = VProjectStatus.DELIVERED
            proj.delivered_at = datetime.utcnow()
        session.add(proj)
        session.commit()
        session.refresh(proj)
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "response": body,
        "delivered": tranche,
        "complete": bool(body.get("complete")) if ok else False,
        "project_status": proj.status,
        "qa_snapshot": qa_snapshot,
    }


def export_return_file(session: Session, proj: VProject, *, ids: list | None = None) -> bytes:
    """The return contract as JSON bytes for the manual re-upload path (download)."""
    tranche = ids if ids is not None else outstanding_ids(session, proj)
    payload = build_return_payload(session, proj, tranche, cost=proj.actual_cost_usd)
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _safe_json(resp: httpx.Response) -> dict:
    try:
        out = resp.json()
        return out if isinstance(out, dict) else {"raw": str(out)[:200]}
    except (ValueError, TypeError):
        return {"raw": (resp.text or "")[:200]}


def _sanitize_reason(body: dict, status: int) -> str:
    """A short, secret-free reason string for the WireLog. Localization OS's own responses are
    already generic, but we keep this defensive."""
    msg = str(body.get("error") or body.get("raw") or "")[:200]
    return f"HTTP {status}: {msg}" if msg else f"HTTP {status}"
