"""vendor_return: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session

from app.models import (
    Document,
    Project,
    TransMode,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
)
from app.services import egress, postedit, quality, vendor_dispatch
from app.services.vendor_return_common import (
    CLAIM_HEADER,
    SIGNATURE_HEADER,
    ReturnAuthError,
    ReturnContractError,
    ReturnStateError,
    _aligned_slots,
    _ct_eq,
    _finite_cost,
    _glossary_terms,
    _ledger,
    _norm_id,
    _notify_query_posts,
    _provenance,
    _resume_leg,
    _segment_index,
    assert_leg_admitting,
    dispatched_ids,
    is_source_echo,
    leg_for_claim,
    omitted_ids,
    outstanding_ids,
    parse_payload,
    returned_ids,
    signing_secret_for,
    verify_signature,
)

log = logging.getLogger("loc_tms.vendor_return")


GENERIC_AUTH_MESSAGE = "invalid claim token or signature."


_COMMENTS_PER_SEGMENT_MAX = 10


_COMMENT_BODY_MAX = 4000


_COMMENT_LABEL_MAX = 120


_COMMENT_REF_MAX = 64


#: The one message the public webhook is allowed to say about an auth failure.
# --- Wave 3 Train E: vendor comment admission (§7.3) --------------------------
#: caps (§3.4/§7.3). A per-segment `comments` block over this many entries, or a malformed
#: one, is a BROKEN CONTRACT -> the WHOLE return is refused in check_contract (a validator,
#: not a filter). The BUSINESS refusals (an over-full thread, an invalid `reply_to`) are
#: handled per-comment at admission and reported in `comments_rejected` -- the return still
#: admits (MED-5). These two are different failures and are answered differently.
def _queries_enabled() -> bool:
    """The `queries_enabled` kill switch (§5.2). When OFF the inbound admission is SKIPPED
    SILENTLY -- the return itself succeeds and every other field admits normally (a vendor
    must never have a delivery refused because the buyer turned an internal feature off).
    An unknown slug fail-opens to True; Train B ships the `queries` Module."""
    from app import features

    return features.is_enabled("queries")


def _validate_comments(seg: dict, i: int) -> list:
    """Validate + normalize a per-segment `comments` block (§7.3). A validator, not a
    filter: absent is fine; anything malformed or over-cap raises ReturnContractError and
    fails the WHOLE return with the leg untouched (matching every other check_contract arm).
    Returns the list of normalized comment dicts (never None)."""
    raw = seg.get("comments")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ReturnContractError(
            f"segments[{i}] `comments` must be a list; the WHOLE return is refused."
        )
    if len(raw) > _COMMENTS_PER_SEGMENT_MAX:
        raise ReturnContractError(
            f"segments[{i}] carries {len(raw)} comments (>{_COMMENTS_PER_SEGMENT_MAX} per "
            "segment); the WHOLE return is refused."
        )
    out = []
    for j, c in enumerate(raw):
        if not isinstance(c, dict):
            raise ReturnContractError(
                f"segments[{i}].comments[{j}] must be an object; the WHOLE return is refused."
            )
        body = c.get("body")
        if not isinstance(body, str) or not (1 <= len(body) <= _COMMENT_BODY_MAX):
            raise ReturnContractError(
                f"segments[{i}].comments[{j}] `body` must be a string of 1..{_COMMENT_BODY_MAX} "
                "chars; the WHOLE return is refused."
            )
        label = c.get("author_label", "")
        if label is None:
            label = ""
        if not isinstance(label, str) or len(label) > _COMMENT_LABEL_MAX:
            raise ReturnContractError(
                f"segments[{i}].comments[{j}] `author_label` must be a string "
                f"<={_COMMENT_LABEL_MAX} chars; the WHOLE return is refused."
            )
        ref = c.get("ref")
        if ref is not None and (not isinstance(ref, str) or len(ref) > _COMMENT_REF_MAX):
            raise ReturnContractError(
                f"segments[{i}].comments[{j}] `ref` must be a string <={_COMMENT_REF_MAX} "
                "chars; the WHOLE return is refused."
            )
        reply_to = c.get("reply_to")
        # int-or-null ONLY (a bool is not a thread id). A valid-shaped but nonexistent /
        # cross-tenant reply_to is NOT refused here -- it is an INDIVIDUAL business refusal
        # at admission (comments_rejected, MED-5), indistinguishable from a nonexistent id
        # so it is never an existence oracle (§7.3/HIGH).
        if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)):
            raise ReturnContractError(
                f"segments[{i}].comments[{j}] `reply_to` must be an integer or null; "
                "the WHOLE return is refused."
            )
        out.append({"body": body, "author_label": label, "ref": ref, "reply_to": reply_to})
    return out


def authenticate_callback(session: Session, headers: dict, raw_body: bytes):
    """Gate 0 for the SIGNED WEBHOOK. Returns the VendorDispatch on success; raises
    ReturnAuthError (always with the SAME generic message) otherwise.

    ORDERING IS THE SECURITY PROPERTY. The claim token is read from the HEADER, so the
    leg -- and therefore the signing key -- is selected while `raw_body` is still an
    opaque byte string. The HMAC is then verified over exactly those bytes. NOTHING is
    deserialized until both have passed, so a hostile caller cannot reach the JSON parser,
    and a malformed body with a bad signature fails as a signature error."""
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    sig_hdr = lower.get(SIGNATURE_HEADER) or ""
    leg = leg_for_claim(session, lower.get(CLAIM_HEADER) or "")
    if leg is None:
        # F-LOW-2: the unknown-token path does a THROWAWAY constant-time HMAC so it takes
        # the same work as a known token -- otherwise it returns measurably faster, a
        # (non-exploitable, 256-bit tokens) timing oracle. Defense in depth; keeps the
        # docstring's constant-time claim literally true.
        verify_signature("locstudio-unknown-token-hmac-pad", raw_body, sig_hdr)
        raise ReturnAuthError(GENERIC_AUTH_MESSAGE)
    vendor = session.get(Vendor, leg.vendor_id)
    if vendor is None:
        verify_signature("locstudio-unknown-token-hmac-pad", raw_body, sig_hdr)
        raise ReturnAuthError(GENERIC_AUTH_MESSAGE)
    if not verify_signature(signing_secret_for(vendor), raw_body, sig_hdr):
        raise ReturnAuthError(GENERIC_AUTH_MESSAGE)
    return leg


# --- gate 2: contract id accounting ------------------------------------------
def check_contract(leg: VendorDispatch, payload: dict) -> list[dict]:
    """Gate 2 -- THE FIRST CONTENT GATE. Validate the payload's shape, locale and ids
    against the leg's ledger and return the normalized per-segment units.

    Every failure raises ReturnContractError and therefore fails the WHOLE return: an
    unknown id, a duplicate WITHIN this payload, a duplicate of an ALREADY-ADMITTED id,
    or a wrong target locale poisons the entire payload, because a payload that names an
    id we never sent is not a payload with one bad row -- it is a payload we cannot
    trust the provenance of at all. Partial coverage is fine (that is the point of
    partial returns); partial CORRECTNESS is not."""
    want = dispatched_ids(leg)
    if not want:
        raise ReturnContractError(
            f"dispatch {leg.id} has no recorded contract id set (it predates the return "
            "ledger), so a return cannot be accounted against it. Use the manual "
            "re-upload path on a freshly dispatched leg."
        )
    loc = payload.get("target_locale")
    if loc is not None and str(loc).strip() and str(loc).strip() != (leg.target_locale or ""):
        raise ReturnContractError(
            f"return target_locale {str(loc).strip()!r} does not match dispatch "
            f"{leg.id}'s locale {leg.target_locale!r}; the WHOLE return is refused."
        )
    # F-MED-3: the leg is chosen by the HEADER claim token (webhook) or the arg/body token
    # (manual); the signature only proves the VENDOR's secret, not WHICH leg. The embedded
    # claim_token is the per-leg binding, so it is MANDATORY and must equal the leg it was
    # authenticated against -- reject when ABSENT or MISMATCHED. Making it conditional on
    # presence let a body signed for leg A be replayed into a distinct leg B of the SAME
    # vendor simply by omitting the field (intra-vendor cross-leg replay). The manual path
    # injects the operator's resolved token when a bare contract omits it (see admit_manual),
    # so an operator paste is unaffected while the signed webhook is bound to exactly one leg.
    claim = payload.get("claim_token")
    if claim is None or not str(claim).strip():
        raise ReturnContractError(
            "the return payload must carry a `claim_token` binding it to the dispatch leg; "
            "the WHOLE return is refused (a signed body must name the leg it belongs to)."
        )
    if not _ct_eq(str(claim).strip(), leg.claim_token):
        raise ReturnContractError(
            "the claim token in the return body does not match the leg it was "
            "authenticated against; the WHOLE return is refused."
        )
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ReturnContractError("return payload must carry a non-empty `segments` array.")
    want_set, already = set(want), set(returned_ids(leg))
    seen: set = set()
    units: list[dict] = []
    for i, seg in enumerate(raw_segments):
        if not isinstance(seg, dict):
            raise ReturnContractError(f"segments[{i}] must be an object with `id` and `target`.")
        if "id" not in seg:
            raise ReturnContractError(f"segments[{i}] has no `id`.")
        sid = _norm_id(seg.get("id"))
        if sid not in want_set:
            raise ReturnContractError(
                f"segments[{i}] names id {sid!r}, which dispatch {leg.id} never sent; "
                "the WHOLE return is refused (no partial admit of an unknown id)."
            )
        if sid in seen:
            raise ReturnContractError(
                f"segments[{i}] repeats id {sid!r} within this return; every dispatched "
                "id must come back exactly ONCE. The WHOLE return is refused."
            )
        if sid in already:
            raise ReturnContractError(
                f"segments[{i}] returns id {sid!r} a SECOND time (an earlier partial "
                "return already admitted it); the WHOLE return is refused."
            )
        seen.add(sid)
        tgt = seg.get("target")
        if tgt is not None and not isinstance(tgt, str):
            raise ReturnContractError(
                f"segments[{i}] `target` must be a string (got {type(tgt).__name__})."
            )
        # Per-segment locale, when the vendor bothers to send one, is held to the leg's.
        sloc = seg.get("locale") or seg.get("target_locale")
        if (
            sloc is not None
            and str(sloc).strip()
            and str(sloc).strip() != (leg.target_locale or "")
        ):
            raise ReturnContractError(
                f"segments[{i}] carries locale {str(sloc).strip()!r} but dispatch "
                f"{leg.id} is {leg.target_locale!r}; the WHOLE return is refused."
            )
        # Wave 3 Train E (§7.3): admit per-segment vendor `comments` as validated blocks.
        # This is a VALIDATOR, not a filter -- a malformed/over-cap block already raised
        # above, failing the whole return. The per-comment BUSINESS refusals happen later,
        # at the write point in _admit_units (comments_rejected).
        # M6: the vendor may DECLARE that consented comments were omitted upstream (over the
        # per-segment cap, or a malformed reply). Carry the count so the return report names it
        # -- the persona consented, the buyer must see it. A non-int degrades to 0; an
        # oversized value clamps to the per-segment comment cap (display-only figure, but a
        # hostile vendor must not write an absurd number into the buyer's audit log).
        try:
            _up_omitted = min(
                max(0, int(seg.get("comments_omitted") or 0)), _COMMENTS_PER_SEGMENT_MAX
            )
        except (TypeError, ValueError):
            _up_omitted = 0
        units.append(
            {
                "id": sid,
                "target": tgt or "",
                "comments": _validate_comments(seg, i),
                "comments_omitted_upstream": _up_omitted,
            }
        )
    return units


def admit_return(
    session: Session,
    leg: VendorDispatch,
    payload,
    *,
    actor: str = "system",
    transport: str = "manual",
    resume: str = "async",
) -> dict:
    """Run the whole pipeline for ONE return against ONE leg and report what happened.

    `resume` selects how a COMPLETING return continues the run: "async" (a background
    thread, what the web + webhook surfaces use so an intake request never executes a
    workflow inline), "sync" (the headless CLI / api_cli convention -- the operator waits
    and sees the outcome) or "none" (admit + close the leg, leave the run parked; used by
    a caller that wants to drive the resume itself).

    Returns a summary dict; raises ReturnStateError / ReturnContractError. NOTHING is
    written unless every gate passed, because every gate runs before the first
    `commit_mt_output` call."""
    data = parse_payload(payload)
    run = assert_leg_admitting(session, leg)  # gate 1
    units = check_contract(leg, data)  # gate 2
    cost = _finite_cost(data.get("actual_cost_usd"))

    doc = session.get(Document, leg.document_id) if leg.document_id else None
    if doc is None:
        raise ReturnContractError(
            f"dispatch {leg.id} names document {leg.document_id}, which no longer "
            "exists; the return is refused."
        )
    project = session.get(Project, doc.project_id) if doc.project_id else None
    # TENANT ISOLATION, re-asserted at the write moment rather than trusted from dispatch
    # time: the document this leg names must still belong to the customer the leg was
    # scoped to. A re-homed document (customer move) must not silently keep admitting a
    # previous customer's vendor content.
    if leg.customer_id is not None and getattr(project, "customer_id", None) != leg.customer_id:
        # L2: do NOT name either customer id in the refusal -- this message can reach the
        # (authenticated but untrusted) vendor over the webhook, and the owning tenant id is
        # not theirs to learn. The detail is in the server log / audit, not the response.
        log.warning(
            "vendor return: dispatch %s dropped -- document re-homed from customer %s to %s",
            leg.id,
            leg.customer_id,
            getattr(project, "customer_id", None),
        )
        raise ReturnStateError(
            f"dispatch {leg.id}: its document is no longer in the tenant this leg was "
            "scoped to; the return is DROPPED (a claim token never crosses a tenant "
            "boundary)."
        )

    from app.services import orchestrator

    segments = orchestrator._load_segments(session, doc.id, leg.target_locale)
    slots = _aligned_slots(session, leg, run, segments)  # alignment precondition
    by_id = _segment_index(segments, leg)
    missing = [u["id"] for u in units if u["id"] not in by_id]
    if missing:
        raise ReturnContractError(
            f"dispatch {leg.id}: returned id(s) {', '.join(missing)} no longer resolve to "
            f"a segment of document {doc.id} in {leg.target_locale}; the WHOLE return is "
            "refused."
        )

    src_locale = getattr(project, "source_locale", "") or ""
    prov = _provenance(leg)
    omitted = set(omitted_ids(leg))
    admitted, flagged, no_op, locked, omitted_noop = [], [], [], [], []
    # F10 (adversarial review): a return over a human-APPROVED/EDITED segment is refused by the
    # seam for the SAME reason a hard lock is (the latest human decision outranks a vendor
    # return), but it is a DISTINCT disposition -- keep it in its own bucket rather than
    # collapsing it into `lock_refused`, so the report and the audit log name it accurately.
    status_protected: list = []
    # Wave 3 Train E (§7.3): vendor comments rejected per-comment (over-full thread /
    # invalid reply_to) land here as (segment id, ref, reason) and surface on every return
    # surface; `affected_threads` collects the threads that gained a post so exactly one
    # notification fires per thread after the commit.
    comments_rejected: list = []
    affected_threads: dict = {}  # thread id -> representative post id (M5 notification)
    # M3 (spec Errata E5): when the queries feature is OFF, inbound comment admission is
    # skipped -- but the no-silent-caps doctrine (§5.2 ¶1) requires the drop be NAMED, not
    # silent. If this return carried any comments while the feature is off, the report carries
    # an explicit note on every surface.
    comments_skipped = ""
    if not _queries_enabled() and any(u.get("comments") for u in units):
        comments_skipped = "queries feature disabled"
    # M6: consented comments the vendor declared it could not ship (upstream cap / malformed).
    comments_omitted_upstream = sum(int(u.get("comments_omitted_upstream") or 0) for u in units)
    try:
        _admit_units(
            session,
            units,
            by_id,
            slots,
            leg,
            project,
            src_locale,
            prov,
            admitted,
            flagged,
            no_op,
            locked,
            omitted,
            omitted_noop,
            status_protected=status_protected,
            # reviewer H1: a FULL_TEXT return is one whole-body unit committed
            # verbatim -- exclude it from the whitespace seam gate (else a
            # trailing newline routes the whole document to review).
            full_text=(doc.mode == TransMode.FULL_TEXT),
            comments_rejected=comments_rejected,
            affected_threads=affected_threads,
        )
    except Exception:
        # Every GATE ran before this loop, so an exception here is an internal failure,
        # not a bad payload -- discard the half-applied batch rather than committing it.
        session.rollback()
        raise

    # F-CRIT: the COMPLETING return's resume must NEVER blindly re-commit the parked
    # working blob for a slot THIS return did not admit. For a source-echo no-op, a withheld
    # refusal, a hard-lock, or a slot an EARLIER partial return already admitted, that blob
    # is still the PRE-DISPATCH SOURCE (external->publish, or an echo/Mock predecessor) --
    # re-committing it would ship SOURCE as clean MT and ERASE the guard verdict the
    # admission just set (the K1 data-loss class). Two coupled defenses:
    #   (a) RESEED every such slot from its POST-admission stored target, so the resume
    #       seed, the run's terminal `done` text, and any downstream step input can never
    #       carry the parked SOURCE; and
    #   (b) hand the resume write-back a SKIP set (below, via _resume_leg) so it leaves those
    #       slots' target_text + status + needs_review disposition exactly as admitted.
    # `admitted_positions` are the slots THIS return committed vendor text into -- carried so
    # their resume re-commit stays TM-ineligible (F-LOW-1: vendor/external content intent).
    admitted_positions = {by_id[uid][0] for uid in admitted if uid in by_id}
    omitted_noop_set = set(omitted_noop)
    skip_positions = set()
    for _sid in dispatched_ids(leg):
        _p = by_id.get(_sid, (None,))[0]
        if _p is None or _p in admitted_positions:
            continue
        # VP-0 omit_locked_targets: an omitted locked segment the vendor returned BLANK is a
        # no-op -- it must resume EXACTLY as if the external step never touched it. Two cases,
        # split on whether its content is already persisted (commit_mt_output would DEMOTE an
        # approved/leveraged status to MT on a re-commit, so a persisted one must be SKIPPED):
        #   * ALREADY PERSISTED (leveraged/ICE/approved target on the row) -> SKIP + reseed
        #     from that stored target: status + target_text left exactly as they were.
        #   * NOT YET PERSISTED (an in-run translation still only in the parked working blob)
        #     -> NOT skipped and NOT reseeded, so its parked working text survives and the
        #     resume write-back persists it (the translated-not-yet-persisted DROP fix).
        if _sid in omitted_noop_set:
            if (segments[_p].target_text or "").strip():
                skip_positions.add(_p)
            continue
        skip_positions.add(_p)
    for _p in skip_positions:
        if 0 <= _p < len(segments):
            slots[_p] = segments[_p].target_text or ""

    # Ledger: every id in this return is accounted for, however it was judged. A
    # quarantined or no-op segment IS accounted -- it came back, it just did not land --
    # so the leg can complete instead of waiting forever for a re-send of a string the
    # vendor already answered.
    ledger = _ledger(leg)
    ledger["dispatched"] = dispatched_ids(leg)
    ledger["returned"] = returned_ids(leg) + [u["id"] for u in units]
    if cost is not None:
        leg.actual_cost_usd = cost  # decision 5c reconciliation
        # MEDIUM-3: the vendor's reported currency labels the ACTUAL cost -- it must NEVER
        # overwrite estimate_currency (the OPERATOR's rate-card label, the reconciliation
        # baseline; letting the vendor set it falsifies decision-5c and stored an unbounded
        # attacker string). Validate ISO-4217 shape (3 ASCII letters) and record it in the
        # ledger's OWN `actual_currency` key; a bogus/overlong value is dropped, never stored.
        _cur = data.get("actual_currency") or data.get("currency")
        _cur = str(_cur).strip().upper() if _cur is not None else ""
        if len(_cur) == 3 and _cur.isascii() and _cur.isalpha():
            ledger["actual_currency"] = _cur
    leg.contract_ids = ledger  # assign AFTER all ledger mutation
    complete = not outstanding_ids(leg)
    if complete:
        # F-HIGH: the DISPATCHED->RETURNED transition is EXACTLY-ONCE, enforced by a
        # conditional compare-and-set rather than the read-then-write of gate 1 (which is a
        # TOCTOU -- a webhook retry racing the poll sweep can pass gate 1 in two sessions at
        # once). Only the row still in DISPATCHED flips; the loser matches 0 rows, rolls its
        # whole batch back, and is DROPPED, so exactly one return completes + resumes.
        from sqlalchemy import update as _sa_update

        _now = datetime.utcnow()
        res = session.execute(
            _sa_update(VendorDispatch)
            .where(
                VendorDispatch.id == leg.id,
                VendorDispatch.status == VendorDispatchStatus.DISPATCHED,
            )
            .values(status=VendorDispatchStatus.RETURNED, returned_at=_now)
        )
        if res.rowcount != 1:
            session.rollback()
            raise ReturnStateError(
                f"dispatch {leg.id} was completed by a concurrent return; this one is "
                "DROPPED (the DISPATCHED->RETURNED transition is exactly-once)."
            )
        leg.status = VendorDispatchStatus.RETURNED
        leg.returned_at = _now
        # A COMPLETING return has no further delivery pending -> clear any held package
        # (a WIRE-LESS leg's frozen operator-delivery package; a native leg holds none). Only
        # the completing return reaches here; a PARTIAL return leaves the leg DISPATCHED above,
        # so the package is KEPT (the operator can still download the correct file). Single-
        # sourced lifecycle (vendor_dispatch._clear_or_keep_frozen).
        vendor_dispatch._clear_or_keep_frozen(leg, VendorDispatchStatus.RETURNED)
    session.add(leg)
    session.commit()

    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.return_admitted",
        target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
        detail=(
            f"{transport}: {len(admitted)} admitted, {len(flagged)} to review, "
            f"{len(no_op)} no-op, {len(locked)} locked, "
            f"{len(status_protected)} human-protected, "
            f"{len(omitted_noop)} omitted-noop, "
            f"{len(affected_threads)} comment(s) admitted, "
            f"{len(comments_rejected)} comment(s) rejected"
            + (f", comments SKIPPED ({comments_skipped})" if comments_skipped else "")
            + "; "
            f"{'complete' if complete else 'partial'}"
        ),
        customer_id=leg.customer_id,
    )

    # Wave 3 Train E (§7.3): one guarded mail per thread that gained a vendor post, AFTER
    # the commit (never one per post). Skipped entirely when queries_enabled=0. The hook is
    # itself internally guarded; this outer wrapper catches even an import/attribute error
    # so a mail side effect can never disturb the (already-committed) return.
    if affected_threads and _queries_enabled():
        _notify_query_posts(session, affected_threads)

    resumed = False
    if complete and resume != "none":
        resumed = _resume_leg(
            session,
            leg,
            run,
            slots,
            actor=actor,
            mode=resume,
            skip_positions=skip_positions,
            tm_ineligible_positions=admitted_positions,
        )
    return {
        "ok": True,
        "dispatch_id": leg.id,
        "run_id": leg.workflow_run_id,
        "transport": transport,
        "admitted": admitted,
        "needs_review": flagged,
        "no_op": no_op,
        "lock_refused": locked,
        "status_protected": status_protected,
        "omitted_noop": omitted_noop,
        "comments_admitted": len(affected_threads),
        "comments_rejected": comments_rejected,
        "comments_skipped": comments_skipped,
        "comments_omitted_upstream": comments_omitted_upstream,
        "complete": complete,
        "resumed": resumed,
        "outstanding": outstanding_ids(leg),
        "actual_cost_usd": leg.actual_cost_usd,
        "estimate_cost_usd": leg.estimate_cost_usd,
    }


def _admit_unit_comments(
    session, leg, segment_id, comments, comments_rejected, affected_threads
) -> None:
    """Admit one unit's vendor `comments` as QueryPost rows (§7.3), the ONE inbound vendor
    path into the query tables. Placed BEFORE the content branches (MED-2): a comment is
    not a content write and must not inherit a content write's refusals -- a comment on a
    hard-locked / status-protected / refusal-withheld segment is still recorded.

    THE N4 TRANSACTION LAW. Each write runs inside `session.begin_nested()` with a SINGLE
    retry on the `(thread_id, external_id)` uniqueness race, and ANY failure degrades to a
    WARN -- never a raise, which would poison the whole vendor-return transaction and fail
    the outer commit. A per-comment BUSINESS refusal (an over-full thread -> QueryConflict,
    an invalid/cross-tenant reply_to -> QueryNotFound, indistinguishable from a nonexistent
    id) lands in `comments_rejected` as (segment id, ref, reason); the return still admits."""
    if not comments or not _queries_enabled():
        return
    from sqlalchemy.exc import IntegrityError

    # M4 / spec §5.1 (NEW-1): this INBOUND WRITE imports the query_threads SERVICE, so a
    # cross_reach_allow row (app/services/vendor_return.py -> queries) lands with Train B's
    # `queries` Module row at B/E MERGE RECONCILIATION -- that Module does not exist on Train
    # E's base (A), so the row cannot be added here without going red in this tree.
    from app.services import query_threads

    for c in comments:
        ref = c.get("ref")
        for attempt in (1, 2):
            sp = session.begin_nested()
            try:
                post = query_threads.record_vendor_post(
                    session,
                    leg=leg,
                    segment_id=segment_id,
                    body=c["body"],
                    author_label=c.get("author_label") or "",
                    external_id=ref,
                    reply_to=c.get("reply_to"),
                )
                sp.commit()
                if affected_threads is not None and post is not None:
                    # thread id -> newest admitted post id (the representative for the §4.6
                    # one-mail-per-thread notification, which needs the triggering post).
                    affected_threads[post.thread_id] = post.id
                break
            except (query_threads.QueryConflict, query_threads.QueryNotFound) as exc:
                # A BUSINESS refusal (MED-5): reject THIS comment, admit the return, name it.
                sp.rollback()
                if comments_rejected is not None:
                    comments_rejected.append(
                        {"segment_id": segment_id, "ref": ref, "reason": str(exc)}
                    )
                break
            except IntegrityError:
                # The (thread_id, external_id) uniqueness race: roll back and retry ONCE --
                # the second pass's look-up-before-create finds the winner's row and dedups.
                sp.rollback()
                if attempt == 2:
                    log.warning(
                        "vendor comment on segment %s: uniqueness race did not "
                        "resolve after retry; dropped (return still admits)",
                        segment_id,
                    )
                continue
            except Exception:
                sp.rollback()
                log.warning(
                    "vendor comment on segment %s failed; dropped (return still admits)",
                    segment_id,
                    exc_info=True,
                )
                break


def _admit_units(
    session,
    units,
    by_id,
    slots,
    leg,
    project,
    src_locale,
    prov,
    admitted,
    flagged,
    no_op,
    locked,
    omitted=None,
    omitted_noop=None,
    status_protected=None,
    full_text=False,
    comments_rejected=None,
    affected_threads=None,
) -> None:
    """Gates 3+4 for every unit: source-echo no-op, else the ONE sanctioned write seam.

    Splits out of `admit_return` only so the caller can wrap it in one rollback boundary
    -- the ordering (echo check, then `commit_mt_output` and nothing else) is the whole
    contract and lives here in one readable block.

    VP-0 (`omit_locked_targets`): a BLANK return for an id in `omitted` (a locked segment the
    vendor was dispatched with no target) is a PURE no-op -- STATUS UNTOUCHED, stored target
    UNTOUCHED (it is not "untranslated"; we deliberately withheld the leveraged target). It is
    recorded in `omitted_noop` so the caller resumes it as an untouched segment. A NON-blank
    return for an omitted id means the vendor actually typed something, so it falls through to
    the normal guarded lanes below."""
    omitted = omitted or set()
    for unit in units:
        pos, seg = by_id[unit["id"]]
        # Wave 3 Train E (§7.3): admit vendor comments FIRST, before EVERY content branch
        # below (the omitted-noop, source-echo, lock_refused, status_protected and
        # not-committed continues). A comment is conversation, not content -- it is recorded
        # regardless of what happens to the unit's TEXT, including when the text is rejected
        # outright. Runs inside its own savepoints (N4); a failure never touches the outer
        # transaction. The post-edit capture stays where it is (it describes a committed edit).
        _admit_unit_comments(
            session, leg, seg.id, unit.get("comments"), comments_rejected, affected_threads
        )
        text = unit["target"]
        if unit["id"] in omitted and not (text or "").strip():
            # withheld locked target, blank back: touch NOTHING (status + stored target).
            if omitted_noop is not None:
                omitted_noop.append(unit["id"])
            continue
        if is_source_echo(seg.source_text or "", text, leg.target_locale):
            # Decision 4: NOT an overwrite. The stored target is left exactly as it is;
            # the segment is routed to review so a human sees the vendor returned nothing
            # usable for it. Post-edit capture (seam 2) is SKIPPED here (§3.2.4): a vendor
            # that returned the source unchanged did no post-editing, and recording a
            # distance for it would be a lie.
            seg.status = quality.SegStatus.NEEDS_REVIEW
            session.add(seg)
            no_op.append(unit["id"])
            continue
        # Snapshot the pre-write state for post-edit capture (seam 2) BEFORE the write
        # seam overwrites the target / clears a lock — `classify_baseline` needs the
        # target's ORIGIN.
        #
        # BASELINE ON slots[pos], NOT seg.target_text (gate H1). In the NATIVE dispatch
        # flow the MT the vendor started from lives in the PARKED StepRun blob
        # (`_aligned_slots:474-476` — "the text the vendor was actually sent"), and the
        # stored `seg.target_text` is empty at dispatch time. Baselining on the stored
        # target would classify every native-flow episode 'none' (empty pre -> NULL
        # distances) and empty the whole vendor edit-distance deliverable (VR-5/AI-8/QA-4).
        # `slots[pos]` is the dispatch-time text by construction; when the stored target is
        # genuinely newer the two coincide, so slots[pos] is never worse.
        _pe_pre = slots[pos] or ""
        _pe_pre_status = seg.status
        _pe_pre_tm_pct = seg.tm_match_pct
        _pe_pre_lock_band = seg.lock_band
        outcome = quality.commit_mt_output(
            session,
            seg,
            text,
            source=prov,
            tm_eligible=False,
            pair=(src_locale, leg.target_locale),
            glossary_terms=_glossary_terms(
                session, project, seg.source_text or "", src_locale, leg.target_locale
            ),
            lang_guard=True,
            full_text=full_text,
        )
        if getattr(outcome, "lock_refused", False):
            # A hard TM lock outranks a vendor return exactly as it outranks MT.
            locked.append(unit["id"])
            continue
        if getattr(outcome, "status_protected", False):
            # STATUS PROTECTION: the segment carries a human APPROVAL/EDIT, and the latest
            # human decision outranks a vendor return exactly as a hard lock does. The seam
            # left the row byte-untouched; reconcile it in its OWN bucket (F10) -- not
            # `flagged`/missing/failed, and not collapsed into `locked` -- so the accounting
            # names the human-protection disposition distinctly. Falls back to `locked` only
            # for a legacy caller that passes no status_protected bucket.
            (status_protected if status_protected is not None else locked).append(unit["id"])
            continue
        if not outcome.committed:
            # The refusal guard withheld the text (nothing written, needs_review + a
            # refusal qa_flag). The parked slot keeps its pre-dispatch value.
            flagged.append(unit["id"])
            continue
        admitted.append(unit["id"])
        # Post-edit capture seam 2 (SPEC-wave2 §3.2.4) — a vendor's admitted post-edit.
        # `leg` is in scope so no new plumbing is needed. `post` is seg.target_text (the
        # guard may have rewritten `text` — a certified tag repair — so read what was
        # actually stored). MID-TRANSACTION (this loop's commit is at admit_return's
        # :849), so `capture_guarded`'s SAVEPOINT is load-bearing: a capture failure rolls
        # back only the savepoint and the admission still commits (N4). No commit here.
        postedit.capture_guarded(
            session,
            segment=seg,
            pre=_pe_pre,
            post=seg.target_text or "",
            actor_kind="vendor",
            actor="",
            vendor_id=leg.vendor_id,
            dispatch_id=leg.id,
            unit_kind=("full_text" if full_text else "segment"),
            target_locale=leg.target_locale,
            pre_status=_pe_pre_status,
            tm_match_pct=_pe_pre_tm_pct,
            lock_band=_pe_pre_lock_band,
        )
        # Splice the ADMITTED text into the parked working blob. `seg.target_text` (not
        # `text`) because the guard may have REWRITTEN it -- a certified tag repair -- and
        # the run must carry forward what was actually stored.
        slots[pos] = seg.target_text or ""
        if (
            outcome.flagged
            or outcome.dnt_violations
            or outcome.glossary_violations
            or seg.status == quality.SegStatus.NEEDS_REVIEW
        ):
            flagged.append(unit["id"])


# --- transport: manual re-upload ----------------------------------------------
def admit_manual(
    session: Session,
    payload,
    *,
    claim_token: str = "",
    org_id=None,
    actor: str = "system",
    resume: str = "sync",
    dispatch_id=None,
) -> dict:
    """The manual re-upload transport: an OPERATOR hands us the return contract JSON.

    Authentication is the operator's own session/API identity (the callers are all
    admin-gated), so no signature is required -- but the VALIDATION is byte-identical to
    the signed path, because the transport never decides how content is judged. The leg is
    matched by `claim_token` (from the argument or the payload); `dispatch_id`, when given,
    must also match, so a paste of the wrong file against a chosen leg is refused rather
    than applied. `org_id`, when given, scopes the leg to the caller's org."""
    # L5: apply the SAME body cap the signed webhook enforces, so a large legitimate return
    # cannot 413 on the signed path yet flow unbounded through the manual/upload one. The
    # cap is on the raw payload (str/bytes); an already-parsed dict came through a caller
    # that read the request itself and is out of scope here.
    from app import config as _config

    _cap = _config.WEBHOOK_MAX_BODY_BYTES
    if isinstance(payload, (bytes, bytearray)) and len(payload) > _cap:
        raise ReturnContractError(f"return payload exceeds the {_cap}-byte cap.")
    if isinstance(payload, str) and len(payload.encode("utf-8", "ignore")) > _cap:
        raise ReturnContractError(f"return payload exceeds the {_cap}-byte cap.")
    data = parse_payload(payload)
    tok = (claim_token or data.get("claim_token") or "").strip()
    if not tok:
        raise ReturnContractError(
            "the return payload carries no `claim_token`; a manual re-upload must name "
            "the dispatch leg it belongs to."
        )
    leg = leg_for_claim(session, tok)
    if leg is None:
        raise ReturnAuthError("no open dispatch matches that claim token.")
    if org_id is not None and getattr(leg, "org_id", None) != org_id:
        # Same message as "not found": a foreign-org leg must not be enumerable.
        raise ReturnAuthError("no open dispatch matches that claim token.")
    if dispatch_id is not None:
        try:
            pinned = int(dispatch_id)
        except (TypeError, ValueError) as e:
            raise ReturnContractError(
                f"dispatch_id must be an integer (got {dispatch_id!r})."
            ) from e
        if pinned != int(leg.id):
            raise ReturnContractError(
                f"that return belongs to dispatch {leg.id}, not dispatch {pinned}; "
                "nothing was applied."
            )
    # F-MED-3: the leg-binding claim_token is MANDATORY in the contract (check_contract).
    # An operator who selected the leg via the `claim_token` ARGUMENT (a bare contract file
    # that omits the field) is honoured by folding that resolved token into the payload --
    # a DIFFERENT body token still mismatches and is refused, so this never loosens the
    # webhook's cross-leg binding, it only preserves the manual arg-selection convenience.
    if isinstance(data, dict) and not str(data.get("claim_token") or "").strip():
        data["claim_token"] = tok
    return admit_return(session, leg, data, actor=actor, transport="manual", resume=resume)


_TEST_RESOLVER = None


#: Injectable poll resolver; address policy belongs exclusively to egress.py.
def set_test_resolver(fn) -> None:
    """Override the poll-path DNS resolver (tests only). `fn` is `host -> iterable[str]`."""
    global _TEST_RESOLVER
    _TEST_RESOLVER = fn


def _resolve_host_ips(host: str) -> list[str]:
    """Every A/AAAA address `host` resolves to (deduped). Overridable via set_test_resolver
    so the hermetic suites never hit the wire."""
    if _TEST_RESOLVER is not None:
        return [str(ip) for ip in _TEST_RESOLVER(host)]
    import socket

    return list({str(ai[4][0]) for ai in socket.getaddrinfo(host, None)})


def _guard_poll_target(raw_url: str) -> str:
    """HTTPS-only poll URLs use the shared private-egress policy and resolver seam.
    Redirects remain disabled; the client also guards every sent request.
    """
    from urllib.parse import urlsplit

    from app.services.connectors.base import ConnectorError

    parts = urlsplit((raw_url or "").strip())
    if (parts.scheme or "").lower() != "https":
        raise ConnectorError(
            f"vendor poll_url must use https (got scheme {parts.scheme or '(none)'!r})."
        )
    from app.services.credential_origin import origin_unreadable

    if origin_unreadable(raw_url):
        # Gate round 2 MEDIUM-1: an origin the credential pin cannot read is refused.
        raise ConnectorError(
            "vendor poll_url must look like https://host[:port]/path with a plain numeric "
            "port; this one could not be read."
        )
    try:
        url = egress.validate_target(raw_url, subsystem="vendor_return", resolver=_resolve_host_ips)
    except egress.EgressBlocked as exc:
        raise ConnectorError(str(exc)) from exc
    return url
