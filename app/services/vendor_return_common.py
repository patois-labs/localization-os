"""vendor_return: shared leaf helpers (layered pure move)

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.models import (
    RunStatus,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
)
from app.services import vendor_dispatch, vendor_xliff, vendors

log = logging.getLogger("loc_tms.vendor_return")


SIGNATURE_HEADER = "x-locstudio-signature"


CLAIM_HEADER = "x-locstudio-claim"


PROVENANCE_VENDOR = "vendor"


PROVENANCE_EXTERNAL = "external"


POLL_MIN_INTERVAL_MINUTES = 15


POLL_MAX_PER_SWEEP = 25


POLL_TIMEOUT_SECONDS = 8.0


#: The signature header a vendor's callback must carry, same name + `sha256=<hex>` shape
#: Wave A's dispatch() sends outbound (one shared secret, both directions).
#: The claim-token header the callback must carry. Wave A already sends it outbound, so a
#: vendor echoing our own header back is the zero-surprise contract.
#: Provenance tags recorded on any qa_flag the guard writes, so a reviewer sees WHICH
#: path produced a quarantined string (§2: "only the provenance tag differs").
#: Polling floor, per VENDOR, PROCESS-LOCAL (see `_LAST_POLL`). Best-effort throttling only.
#: MEDIUM-5: hard cap on legs polled in ONE sweep, so a huge open-leg backlog can never
#: turn a single 30s tick into a request storm on the one scheduler thread.
#: MEDIUM-5: a SHORT per-poll timeout so a hung vendor cannot wedge the single scheduler
#: thread (which also drives connector syncs + the expiry/auto-close sweeps) for 30s x N.
# --- errors -------------------------------------------------------------------
class VendorReturnError(RuntimeError):
    """Base for every return-intake refusal. Carries a clean, surface-shared message."""


class ReturnAuthError(VendorReturnError):
    """The caller could not prove it holds the leg: unknown/blank claim token, missing or
    malformed signature, wrong signature, or a vendor with no signing secret configured.

    DELIBERATELY UNIFORM on the public webhook: every one of those returns the SAME
    generic message and status, so an unauthenticated prober cannot use the response to
    learn whether a claim token exists. The loud, specific errors below are only ever
    reachable AFTER the signature has been verified (or by an authenticated operator)."""


class ReturnStateError(VendorReturnError):
    """The leg cannot admit content any more: cancelled, escalated, already returned, or
    its run went terminal. This is the LOUD late-return refusal (§3.7)."""


class ReturnContractError(VendorReturnError):
    """The return violated the contract: unparseable body, wrong locale, an unknown /
    duplicate / missing id, or a leg with no recorded contract. Fails the WHOLE return."""


# --- small helpers ------------------------------------------------------------
def _norm_id(value) -> str:
    """One canonical string form for a contract segment id, on BOTH sides of every
    comparison. Segmented dispatch sends integer Segment ids, FULL_TEXT sends the string
    "full:0", and JSON round-trips (and hand-built vendor payloads) freely swap 12 for
    "12" -- so the accounting normalizes rather than trusting the wire's type."""
    return str(value).strip()


def _ct_eq(a, b) -> bool:
    """Constant-time equality for two caller-supplied token strings.

    Compares the UTF-8 BYTES, never the str objects: `hmac.compare_digest` raises
    TypeError on a non-ASCII str, and both sides of every claim-token comparison here are
    attacker-supplied (a header value, or a `claim_token` field inside a posted body). A
    500 on a non-ASCII token would be a free liveness probe on an unauthenticated route."""
    return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))


def _base_lang(locale: str | None) -> str:
    """The base language subtag, lowercased ("es-MX" -> "es"). Empty for a falsy locale."""
    if not locale:
        return ""
    return locale.replace("_", "-").split("-", 1)[0].strip().lower()


def _finite_cost(value):
    """A vendor-reported cost coerced to a sane float, or None. Refuses NaN/Infinity and
    negatives the same way the rate-card validator does (a stored NaN permanently 500s
    every JSON surface that renders the leg, and a negative invoice is not a thing)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError) as e:
        raise ReturnContractError(f"actual_cost_usd must be a number (got {value!r}).") from e
    if not math.isfinite(f) or f < 0:
        raise ReturnContractError(f"actual_cost_usd must be a finite number >= 0 (got {value!r}).")
    return f


# --- claim-token lookup + signature -------------------------------------------
def leg_for_claim(session: Session, claim_token: str) -> VendorDispatch | None:
    """The VendorDispatch a claim token names, or None. The token is a 32-byte urlsafe
    secret, so the indexed lookup is effectively unique; the compare_digest re-check
    exists so an exotic collation (or a future case-insensitive column) can never turn a
    near-miss into a match."""
    tok = (claim_token or "").strip()
    if not tok:
        return None
    row = session.exec(select(VendorDispatch).where(VendorDispatch.claim_token == tok)).first()
    if row is None:
        return None
    if not _ct_eq(row.claim_token, tok):
        return None
    return row


def signing_secret_for(vendor: Vendor) -> str:
    """The vendor's shared HMAC secret (the SAME one Wave A signs outbound dispatches
    with). "" when unset -- which makes a signed intake impossible, and is treated as an
    auth failure rather than as "no signature required"."""
    return (vendors.secret_dict(vendor) or {}).get("signing_secret") or ""


def verify_signature(secret: str, raw_body: bytes, header_value: str) -> bool:
    """Constant-time HMAC-SHA256 verification over the RAW request body.

    Accepts the `sha256=<hex>` form Wave A emits (and a bare hex digest, since some
    vendor stacks strip the prefix). Every failure mode -- no secret, no header, wrong
    algorithm prefix, non-hex, WRONG LENGTH, wrong value -- returns False through the
    same path; the digest comparison itself is `hmac.compare_digest` over the raw BYTES
    (not the hex text), so a length mismatch cannot be timed either."""
    if not secret or not header_value:
        return False
    sent = str(header_value).strip()
    if sent.lower().startswith("sha256="):
        sent = sent[len("sha256=") :].strip()
    if not sent:
        return False
    try:
        sent_bytes = bytes.fromhex(sent)
    except ValueError:
        return False
    expect = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).digest()
    return hmac.compare_digest(sent_bytes, expect)


def parse_payload(raw) -> dict:
    """The return contract as a dict. Only ever called AFTER authentication.

    Vendor Wave D: a return body may arrive as XLIFF 1.2 instead of our JSON contract. An
    XLIFF body is a TRANSPORT DECODE ONLY -- it is parsed into the SAME {id, target}
    contract dict every other transport yields, then judged by the identical gates
    (check_contract onward). It is NEVER a new admission path: no claim_token is produced
    from the XLIFF (it has no native slot); the caller injects the AUTHENTICATED leg's
    token -- the header-selected leg on the signed webhook (decode_return_body), or the
    operator's --claim/resolved token on the manual path (admit_manual) -- exactly as a
    bare JSON contract that omits the field is handled."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray, str)) and vendor_xliff._looks_like_xliff(raw):
        try:
            return vendor_xliff.parse_return_xliff(raw)
        except vendor_xliff.XliffParseError as exc:
            raise ReturnContractError(f"return XLIFF could not be parsed: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReturnContractError(f"return payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReturnContractError("return payload must be a JSON object.")
    return data


def decode_return_body(raw, *, leg: VendorDispatch | None = None):
    """Vendor Wave D transport decode for the SIGNED WEBHOOK: turn a raw return body into
    the payload `admit_return` consumes.

    A JSON body is passed through UNTOUCHED (returned as-is, so admit_return's parse_payload
    json-decodes it and the F-MED-3 body-claim binding is preserved for JSON). An XLIFF body
    is decoded HERE and stamped with the AUTHENTICATED leg's claim token: XLIFF has no native
    token slot, and the `X-LocStudio-Claim` header already selected + the HMAC already
    authenticated the leg, so binding the decoded payload to THAT leg is the transport's job
    -- mirroring how `admit_manual` folds the operator's resolved token into a tokenless body.
    This stays a TRANSPORT DECODE: every gate (id accounting, guard lanes, source-echo,
    omitted no-op) runs afterward in admit_return, unchanged."""
    if leg is not None and vendor_xliff._looks_like_xliff(raw):
        data = parse_payload(raw)  # -> dict via the XLIFF branch (or raises 400)
        # MED-1: if the file still carries OUR claim note, KEEP it -- check_contract then
        # holds it to this authenticated leg, so a cross-leg replay of a token-bearing file
        # is a 400. Only a tokenless file (foreign tool stripped the note) is bound to the
        # authenticated leg by stamping. Residual replay (accepted, see module docstring)
        # needs the target leg's claim header + identical id sets + the vendor's signing key.
        if not str(data.get("claim_token") or "").strip():
            data["claim_token"] = leg.claim_token
        return data
    return raw


# --- the contract ledger ------------------------------------------------------
def _ledger(leg: VendorDispatch) -> dict:
    return dict(leg.contract_ids or {})


def dispatched_ids(leg: VendorDispatch) -> list[str]:
    """The id set the outbound contract carried (normalized). Empty when the leg predates
    the ledger column -- which the accounting treats as un-accountable, never as "any id
    is fine"."""
    return [_norm_id(i) for i in (_ledger(leg).get("dispatched") or [])]


def returned_ids(leg: VendorDispatch) -> list[str]:
    """The ids ALREADY admitted by earlier partial returns (normalized)."""
    return [_norm_id(i) for i in (_ledger(leg).get("returned") or [])]


def outstanding_ids(leg: VendorDispatch) -> list[str]:
    """The dispatched ids still awaited, in dispatch order."""
    done = set(returned_ids(leg))
    return [i for i in dispatched_ids(leg) if i not in done]


def omitted_ids(leg: VendorDispatch) -> list[str]:
    """VP-0: the dispatched ids whose target was BLANKED by the vendor's
    `omit_locked_targets` option (the locked/leveraged segments the vendor never received a
    target for). Empty on a leg dispatched without the flag. A BLANK return for one of these
    is a pure no-op (the withheld locked target is not "untranslated"); a NON-blank return
    is judged through the normal guarded lanes (the vendor typed something)."""
    return [_norm_id(i) for i in (_ledger(leg).get("omitted") or [])]


# --- gate 1: leg state --------------------------------------------------------
def assert_leg_admitting(session: Session, leg: VendorDispatch) -> WorkflowRun:
    """Gate 1 (§3.7, matched-token-or-DROP). Raise ReturnStateError -- LOUDLY, naming the
    real reason, because this is only reachable by an already-authenticated caller -- when
    the leg has stopped being able to admit content. Returns the live WorkflowRun."""
    status = leg.status or ""
    if status == VendorDispatchStatus.RETURNED:
        raise ReturnStateError(
            f"dispatch {leg.id} already returned at "
            f"{leg.returned_at.isoformat() if leg.returned_at else 'an earlier time'}; "
            "a second return is DROPPED (the leg is closed)."
        )
    if status == VendorDispatchStatus.CANCELLED:
        raise ReturnStateError(
            f"dispatch {leg.id} was CANCELLED (its run was aborted, superseded or has "
            "finished); this late return is DROPPED and nothing was written."
        )
    if status == VendorDispatchStatus.ESCALATED:
        raise ReturnStateError(
            f"dispatch {leg.id} EXPIRED and was escalated to human review; this late "
            "return is DROPPED. A person now owns the parked leg."
        )
    if status != VendorDispatchStatus.DISPATCHED:
        raise ReturnStateError(
            f"dispatch {leg.id} is in state {status!r} and cannot admit a return."
        )
    run = session.get(WorkflowRun, leg.workflow_run_id)
    if run is None:
        raise ReturnStateError(
            f"dispatch {leg.id} names run {leg.workflow_run_id}, which no longer exists; "
            "the return is DROPPED."
        )
    if vendor_dispatch.is_run_terminal(run.status):
        raise ReturnStateError(
            f"run {run.id} is already {run.status}; a return for a finished run is "
            "DROPPED, never applied (it would resurrect a run that moved on)."
        )
    if run.status != RunStatus.PAUSED:
        raise ReturnStateError(
            f"run {run.id} is {run.status}, not paused at the external step; the return "
            "is DROPPED (only a parked leg may admit one)."
        )
    # HIGH-1 (STEP ISOLATION): a PAUSED run is not necessarily parked at THIS leg's step.
    # If the run advanced PAST the external step to a LATER pause (e.g. a human_review step),
    # the newest paused StepRun -- the one `_aligned_slots`/`_resume_leg` splice into and
    # resume -- belongs to THAT step. Admitting here would write vendor content into the
    # wrong step's working blob and drive the foreign pause to completion (the human's review
    # consumed unseen). Refuse unless the step CURRENTLY parked is this leg's step.
    from app.services import orchestrator

    paused_sr = orchestrator._latest_paused_steprun(session, run.id)
    if paused_sr is None or (leg.step_id is not None and paused_sr.step_id != leg.step_id):
        raise ReturnStateError(
            f"dispatch {leg.id}: run {run.id} is no longer parked at this leg's step "
            "(it advanced to a different pause); the return is DROPPED."
        )
    return run


# --- the working-blob alignment (checked BEFORE anything is written) ----------
def _aligned_slots(
    session: Session, leg: VendorDispatch, run: WorkflowRun, segments: list
) -> list[str]:
    """The parked leg's working text, one slot per dispatched unit.

    The PAUSED StepRun's blob is the text the vendor was actually sent. An admitted
    return REPLACES only its own slot; every other slot -- including one whose return was
    quarantined or was a source-echo no-op -- keeps the pre-dispatch text. That is what
    makes the resume incapable of losing work: the seed handed back to the run loop is
    "what we had, with the vendor's accepted improvements spliced in", never "whatever is
    in the DB now".

    Raises ReturnContractError when the blob no longer aligns to the segment set (which
    should be impossible -- the same seam wrote both -- but a misaligned splice would
    write targets onto the WRONG segments, so it fails closed BEFORE any admission)."""
    from app.services import orchestrator

    blob = orchestrator._paused_blob(session, leg.workflow_run_id)
    if blob is None:
        raise ReturnContractError(
            f"dispatch {leg.id}: run {run.id} has no paused step record to resume from; "
            "the return is refused rather than guessing the parked text."
        )
    slots = blob.split(orchestrator.SEG_JOINER)
    expected = len(segments) if segments else 1
    if len(slots) != expected:
        raise ReturnContractError(
            f"dispatch {leg.id}: the parked working text has {len(slots)} slot(s) but "
            f"the document now has {expected} segment(s); the return is refused (a "
            "misaligned splice would write targets onto the wrong segments)."
        )
    return slots


# --- the admission itself -----------------------------------------------------
def _provenance(leg: VendorDispatch) -> str:
    """ "vendor" when the leg has a vendor attached, "external" otherwise (§2: the ONE
    honest difference between the two profiles of this substrate is the tag)."""
    return PROVENANCE_VENDOR if getattr(leg, "vendor_id", None) else PROVENANCE_EXTERNAL


def is_source_echo(source: str, target: str, target_locale: str) -> bool:
    """Decision 4 / the K1 law: a returned target that is just the SOURCE, for a
    NON-English target, is "not translated" -- never an overwrite of a good stored
    target. A blank target is the same thing said with fewer characters.

    Scoped to non-English targets on purpose: en-US -> en-GB legitimately leaves many
    strings byte-identical, and treating those as failures would flood review.

    HIGH-1: both sides are line-ending-normalized (\\r\\n / \\r -> \\n) before comparison,
    so a CRLF source echoed back through XML (which mandates \\r\\n -> \\n on parse, or via
    a mixed-line-ending JSON return) is still recognized as an echo, never admitted as a
    spuriously "edited" clean MT."""
    t = vendor_xliff._normalize_newlines(target or "")
    if not t.strip():
        return True
    lang = _base_lang(target_locale)
    if not lang or lang == "en":
        return False
    return t.strip() == vendor_xliff._normalize_newlines(source or "").strip()


def _glossary_terms(
    session: Session, project, source_text: str, src_locale: str, tgt_locale: str
) -> list:
    """The matched glossary constraints for one segment, so the DNT + required-term lanes
    inside `commit_mt_output` actually have something to check. Fully guarded: a glossary
    resolution failure degrades to "no terms" (the other guard lanes still run) and never
    breaks an intake."""
    if project is None:
        return []
    try:
        from app.services import glossary_module as _gm

        plan = _gm.build_glossary_plan(session, project, src_locale, tgt_locale)
        return _gm.match_terms_merged(
            session, project.id, source_text, src_locale, tgt_locale, plan, conflict_state=set()
        )
    except Exception:
        log.warning("vendor return: glossary resolution failed", exc_info=True)
        return []


def _segment_index(segments: list, leg: VendorDispatch) -> dict:
    """id -> (position, Segment) for the units a return may name. FULL_TEXT dispatches one
    whole-body unit under the STRING id "full:0" (Wave A, L10) which maps to the
    document's single segment for the locale; SEGMENTED units are Segment ids."""
    out = {}
    for pos, seg in enumerate(segments):
        out[_norm_id(seg.id)] = (pos, seg)
    if segments:
        out.setdefault("full:0", (0, segments[0]))
    return out


def _notify_query_posts(session, thread_to_post) -> None:
    """Fire the guarded query-post notification for each affected thread's assignee (§7.3):
    ONE mail per thread, never one per post. `thread_to_post` maps thread id -> the id of the
    representative (newest admitted) vendor post on it.

    ** THE HOOK CONTRACT (Train B's surface; B/E MERGE RECONCILIATION). Per §4.6 the "new
    post" event notifies the thread author + assignee_email MINUS THE ACTOR, so the hook needs
    both the thread AND the triggering post (whose author identifies the actor to subtract):

        notifications.on_query_post(thread_id: int, post_id: int, actor_email: str) -> int

    For a vendor post the actor has no platform email (author_kind='vendor', author=""), so the
    subtraction is a no-op and both the buyer author and the assignee are notified. The hook is
    getattr-guarded here so this call is INERT on a tree where B has not landed and composes
    once it has. When the trains merge, ASSERT this call matches B's signature -- a mismatch
    degrades to a logged no-op here, never a raise. Wrapped so a mail side effect can never
    disturb the already-committed return (mirrors orchestrator._notify_human_review_paused)."""
    try:
        from app.services import notifications

        hook = getattr(notifications, "on_query_post", None)
        if hook is None:
            return
        from app.models import QueryPost, QueryThread

        for tid, pid in thread_to_post.items():
            try:
                thread = session.get(QueryThread, tid)
                post = session.get(QueryPost, pid) if pid is not None else None
                # Reconciliation (B/E merge): B's on_query_post takes ids + the actor email,
                # not objects. A vendor post has no platform-user actor, so pass "" -- the
                # buyer's author and assignee are both notified, nobody subtracted.
                if thread is not None and post is not None:
                    hook(thread.id, post.id, "")
            except Exception:
                log.warning("query-post notification failed for thread %s", tid, exc_info=True)
    except Exception:
        log.warning("query-post notification hook failed", exc_info=True)


def _resume_leg(
    session: Session,
    leg: VendorDispatch,
    run: WorkflowRun,
    slots: list,
    *,
    actor: str,
    mode: str,
    skip_positions=None,
    tm_ineligible_positions=None,
) -> bool:
    """Continue the parked run through the EXISTING human_review resume machinery, once.

    EXACTLY-ONCE is structural: this is only reached on the return that made the guarded
    DISPATCHED -> RETURNED transition (already committed above), and the run must still be
    PAUSED. A second return cannot get here because gate 1 refuses a RETURNED leg.

    F-CRIT / F-LOW-1: the write-back policy for this resume is handed to the orchestrator
    through the resume-safe `run.context` scratchpad (a ONE-SHOT `_vendor_return_writeback`
    key the resumed execution consumes + clears at its top, so it never leaks into a later
    human_review resume of the same run). It names the slots the write-back must NOT
    re-commit (their vendor verdict is authoritative) and the admitted-vendor slots whose
    re-commit must stay TM-ineligible."""
    from app.services import orchestrator

    fresh = session.get(WorkflowRun, run.id)
    if fresh is None or fresh.status != RunStatus.PAUSED:
        return False
    seed = orchestrator.SEG_JOINER.join(slots)
    skip = sorted(int(p) for p in (skip_positions or set()))
    tm_inelig = sorted(int(p) for p in (tm_ineligible_positions or set()))
    # `_resume_kind: "vendor"` is set UNCONDITIONALLY (INFO-2): it is the EXPLICIT signal that
    # this resume is a vendor/external RETURN completing-resume, not a human pause-edit -- so the
    # orchestrator's operator-edit classification (which re-reads the parked blob) never mistakes
    # the vendor's returned text for an operator edit. It does NOT depend on the `skip`/`tm_inelig`
    # side effect below (which is only present when there are authoritative/ineligible slots): an
    # all-flagged return sets no writeback dict but is still a vendor resume. Both keys are
    # one-shot -- the resumed execution consumes + clears them at its top.
    _ctx = {**(fresh.context or {}), "_resume_kind": "vendor"}
    if skip or tm_inelig:
        _ctx["_vendor_return_writeback"] = {"skip": skip, "tm_ineligible": tm_inelig}
    fresh.context = _ctx
    session.add(fresh)
    session.commit()
    try:
        if mode == "sync":
            orchestrator.resume_workflow(session, fresh.id, fresh.target_locale, seed)
        else:
            orchestrator.resume_run_async(
                session, fresh.id, fresh.target_locale, seed, actor=actor or "vendor-return"
            )
    except Exception:
        log.exception("vendor return: resume of run %s failed", fresh.id)
        # MEDIUM-4: the leg is RETURNED (committed above) but the run never resumed. Gate 1
        # now refuses every retry, so without a LOUD signal + a re-drive path the run is
        # wedged PAUSED forever, silently. Record it durably AND notify the owner; the leg
        # is re-drivable via `redrive_returned_leg` / the CLI `vendor redrive` verb. Both
        # side effects are best-effort -- neither may mask the underlying resume failure.
        try:
            from app.services import audit

            audit.log(
                actor=actor or "system",
                action="vendor.return_resume_failed",
                target=f"dispatch {leg.id} (run {fresh.id})",
                detail=(
                    "return admitted + leg RETURNED, but the resume threw; the "
                    "run is PAUSED and needs a re-drive (vendor redrive)."
                ),
                customer_id=getattr(leg, "customer_id", None),
            )
        except Exception:
            log.exception("vendor return: could not audit wedged resume of run %s", fresh.id)
        try:
            from app.services import notifications

            notifications.on_vendor_return_resume_failed(fresh, leg)
        except Exception:
            log.exception("vendor return: could not notify wedged resume of run %s", fresh.id)
        return False
    return True


_LAST_POLL: dict = {}
#: leg_id -> last instant THAT leg was polled (process-local, like `_LAST_POLL`). #447: the sweep
#: polls one leg per vendor, and this makes it rotate through the vendor's open legs (least
#: recently polled first) instead of always taking the newest. Pruned to the open legs.
_LAST_POLL_LEG: dict = {}


# --- transport: polling -------------------------------------------------------
#: vendor_id -> last poll instant. PROCESS-LOCAL best-effort throttling: it resets on
#: restart and is invisible to a second process, which is fine -- the floor exists to
#: stop a 30s scheduler tick hammering a vendor, not to enforce a contract. MEDIUM-5: keyed
#: by VENDOR (was per-leg), so a vendor with N open legs is polled at most ONCE per window
#: (one GET per vendor per sweep) instead of N. Bounded by vendor count, not leg count.
class VendorPollError(VendorReturnError):
    """A poll could not be performed (no poll_url configured, SSRF/scheme refusal, a
    transport error, or a non-2xx that is not the documented "nothing ready" answer)."""


def poll_url_for(vendor: Vendor) -> str:
    """The vendor's declared poll endpoint, from its NON-SECRET `config.poll_url`.

    Deliberately NOT derived from `endpoint_url`: polling must be an explicit per-vendor
    opt-in, so an install that never configured one is never swept, and no vendor is
    GET-probed on an intake URL it only ever agreed to receive POSTs on."""
    return str((vendor.config or {}).get("poll_url") or "").strip() if vendor else ""


def open_legs(session: Session, *, vendor_id=None, org_id=None) -> list[VendorDispatch]:
    """Every still-OPEN leg (DISPATCHED or RETRY_WAIT), newest first, optionally scoped to
    one vendor / org. M1: RETRY_WAIT is included so the vendor detail page / `/api/vendors/
    dispatches` default / `vendor dispatches` default show a leg that is transiently
    retrying rather than hiding it. Polling still targets DISPATCHED only (pollable_legs
    filters), since a RETRY_WAIT leg has delivered nothing to poll for."""
    q = select(VendorDispatch).where(
        VendorDispatch.status.in_(tuple(vendor_dispatch._OPEN_LEG_STATUSES))
    )
    if vendor_id is not None:
        q = q.where(VendorDispatch.vendor_id == vendor_id)
    if org_id is not None:
        q = q.where(VendorDispatch.org_id == org_id)
    return list(session.exec(q.order_by(VendorDispatch.id.desc())).all())


def pollable_legs(
    session: Session,
    *,
    vendor_id=None,
    org_id=None,
    now: datetime | None = None,
    throttle: bool = True,
) -> list:
    """The open legs due for a poll: at most ONE per vendor (MEDIUM-5), whose vendor declared
    a `poll_url` and whose per-VENDOR throttle window has elapsed, capped at
    POLL_MAX_PER_SWEEP total. An install with no polling vendor gets an empty list, so the
    scheduler hook is a pure no-op for it.

    The single leg chosen per vendor is its LEAST RECENTLY POLLED open leg (#447), so over
    successive sweeps every open leg gets a turn; a completing return closes the leg
    regardless of transport, and the manual/webhook paths are always available."""
    now = now or datetime.utcnow()
    floor = timedelta(minutes=POLL_MIN_INTERVAL_MINUTES)
    legs = open_legs(session, vendor_id=vendor_id, org_id=org_id)
    if vendor_id is None and org_id is None:
        # bound the per-leg map to legs that are still open
        live = {leg.id for leg in legs}
        for stale in [k for k in _LAST_POLL_LEG if k not in live]:
            _LAST_POLL_LEG.pop(stale, None)
    # vendor_id -> its pollable legs (open_legs is newest-first; the dict keeps the vendors in
    # that order, so the cross-vendor order is unchanged)
    by_vendor: dict = {}
    for leg in legs:
        # M1: open_legs now also returns RETRY_WAIT legs, but a leg that has not been
        # delivered yet has nothing to poll for -- only a truly DISPATCHED leg is pollable.
        if leg.status != VendorDispatchStatus.DISPATCHED:
            continue
        vendor = session.get(Vendor, leg.vendor_id)
        if not poll_url_for(vendor):
            continue
        # A non-native transport (e.g. `manual` file-exchange) has NO poll wire: poll_leg
        # refuses it, and a manual vendor normally has no poll_url anyway. Skip it here so the
        # sweep never picks a leg it cannot poll -- defence-in-depth for a manual vendor that
        # (harmlessly) carries a poll_url in its config. F2: key off the LEG's protocol SNAPSHOT
        # (not the vendor's live protocol), so a manual-dispatched leg is never picked up for a
        # poll even if the vendor was flipped to native while the leg was parked.
        if vendor_dispatch.leg_protocol(leg, vendor) != vendor_dispatch.NATIVE_PROTOCOL:
            continue
        by_vendor.setdefault(leg.vendor_id, []).append(leg)
    out = []
    for vid, vlegs in by_vendor.items():
        # MEDIUM-5: one decision per vendor per sweep (was per-leg, so N legs = N GETs per
        # tick). #447: the leg chosen is the LEAST RECENTLY polled (never-polled first, then
        # the oldest leg), so a newer leg that keeps answering "not ready" cannot starve an
        # older leg whose return is waiting.
        leg = min(vlegs, key=lambda lg: (_LAST_POLL_LEG.get(lg.id) or datetime.min, lg.id))
        last = _LAST_POLL.get(vid)
        if throttle and last is not None and (now - last) < floor:
            continue
        out.append(leg)
        if len(out) >= POLL_MAX_PER_SWEEP:
            break
    return out
