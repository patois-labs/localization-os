"""vendor_dispatch: shared leaf helpers (layered pure move)

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import logging
import math
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.models import (
    RunStatus,
    StepRun,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
    WorkflowStep,
)

log = logging.getLogger("loc_tms.vendor_dispatch")


CONTRACT_VERSION = "1.0"


_PLACEHOLDER_RE = re.compile(r"⟦[^⟧]*⟧")


# The inline placeholder/tag markers our segmentation masks (⟦xN⟧ and friends). The
# contract reports each segment's placeholder MULTISET so the far side knows the exact
# tag inventory it must preserve (the §3.2 shape; the return-side integrity check is
# Wave B). Matches ⟦…⟧ non-greedily.
def _placeholders(text: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(text or "")


class VendorDispatchError(RuntimeError):
    """A TRANSIENT dispatch failure the backoff retry SHOULD re-attempt (M4 taxonomy): a
    transport-level error (ConnectError / timeout / reset) or a retryable response status
    (5xx / 408 / 429). Parks a RETRY_WAIT leg. May carry `retry_after` seconds (from a
    429/503 Retry-After header) which the retry honors, capped at the next backoff step."""

    def __init__(self, *args, retry_after: float | None = None):
        super().__init__(*args)
        self.retry_after = retry_after


class VendorPolicyRefusal(RuntimeError):
    """A NON-RETRYABLE dispatch response the vendor gave us (M4 taxonomy): a 3xx (delivered
    to nobody — redirects are not followed) or a non-retryable 4xx (401/403/404/413/…). A
    retry can never clear it, so it is NOT parked for retry; per decision 3 it does not
    silently hard-fail either — a person decides, so it escalates to a human_review pause
    (a DEAD_LETTER leg at first dispatch, or dead_letter_leg on the retry path). Kept
    DISTINCT from VendorDispatchError so the transient `except` never catches it."""


_RETRYABLE_STATUS = frozenset({408, 429})


# The response statuses the backoff retry re-attempts (M4): 408 Request Timeout, 429 Too
# Many Requests, and every 5xx. Any other non-2xx (3xx, other 4xx) is a policy refusal.
def _is_retryable_status(code: int) -> bool:
    return code in _RETRYABLE_STATUS or 500 <= int(code) < 600


def _parse_retry_after(value) -> float | None:
    """Parse a Retry-After header value -> seconds (>=0), or None. Supports the delta-seconds
    form and, best-effort, the HTTP-date form. A malformed/negative value -> None."""
    if not value:
        return None
    raw = str(value).strip()
    try:
        secs = float(raw)
        # #448: float("inf")/float("nan") parse cleanly, but a later timedelta(seconds=...)
        # on them raises OverflowError. A non-finite hint is not a usable wait, so treat it
        # as absent (None) rather than propagate it into the backoff arithmetic downstream.
        if not math.isfinite(secs):
            return None
        return secs if secs >= 0 else None
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        from datetime import timezone

        now = datetime.now(dt.tzinfo or timezone.utc)
        delta = (dt - now).total_seconds()
        return delta if delta >= 0 else 0.0
    except Exception:
        return None


# --- claim token -------------------------------------------------------------
def new_claim_token() -> str:
    """A high-entropy, URL-safe match token. Wave B matches a signed return to the exact
    VendorDispatch leg by it; it is echoed to the vendor inside the contract."""
    return secrets.token_urlsafe(32)


def _author_role(author_kind: str) -> str:
    """A COARSE label for a post's origin -- NEVER a User email (§7.2 privacy invariant).
    A vendor has no business receiving the buyer's staff directory."""
    return "vendor" if author_kind == "vendor" else "buyer"


def _as_segment_int(sid):
    """The internal Segment id an outbound-carry key maps to, or None. A segmented dispatch
    passes the int Segment id; the operator XLIFF rebuild (contract_for_leg) passes it as a
    normalized STRING; the FULL_TEXT unit id ("full:0") maps to no per-segment thread."""
    if isinstance(sid, bool):
        return None
    if isinstance(sid, int):
        return sid
    if isinstance(sid, str) and sid.strip().isdigit():
        return int(sid.strip())
    return None


_FORBIDDEN_CONFIG_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        "authorization",
        "expect",
    }
)


# M1: headers a vendor's `config.headers` may NEVER set -- hop-by-hop framing, Host
# (request smuggling / SSRF via Host override), the content-type/authorization we own, and
# our x-locstudio-* provenance headers (which are always set LAST regardless).
def _safe_config_headers(cfg: dict) -> dict:
    """The vendor's config-supplied extra headers, sanitized (M1): each key lowercased and
    dropped if it is hop-by-hop, Host, an auth/content header we own, or one of our
    x-locstudio-* headers. What survives is benign per-vendor metadata (e.g. an api
    account id header). Our own headers are layered on top afterwards, so nothing here can
    override them."""
    out: dict = {}
    for k, val in (cfg.get("headers") or {}).items():
        lk = str(k).strip().lower()
        if not lk or lk in _FORBIDDEN_CONFIG_HEADERS or lk.startswith("x-locstudio-"):
            continue
        # Strip CR/LF so a config value can't inject a second header line.
        out[lk] = str(val).replace("\r", " ").replace("\n", " ")
    return out


def _endpoint_host(url: str) -> str:
    """The host of an endpoint URL — the ONLY part of the target ever persisted / shown
    (never the full URL or secret)."""
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


RETRY_BACKOFF_SCHEDULE: tuple[int, ...] = (1, 5, 15, 60, 360)


RETRY_AFTER_FLOOR_SECONDS: int = 30


# --- dead-letter / backoff (interop R5, Wave C) ------------------------------
# The BOUNDED exponential-backoff window for retrying a TRANSIENT outbound-dispatch
# failure (network error / timeout / non-2xx from the vendor). Each entry is the wait (in
# MINUTES) before the next retry; after the last entry's retry fails the leg is terminal
# (DEAD_LETTER + escalate to a human). This "bounded retry window then dead-letter" is the
# industry convention (Lionbridge's ~24h shape, RWS's 8-attempt circuit breaker —
# PROPOSAL-vendor-portal-interop F6b/R5). 5 retries after the initial attempt ~= 7.7h,
# comfortably inside the default 168h dispatch wait, so an unreachable vendor dead-letters
# (a person decides, decision 3) long before the leg would expire.
# minutes; then terminal
# L-b: the minimum wait before ANY retry, so a vendor `Retry-After: 0` (or a tiny value)
# can never collapse the backoff into an immediate re-hammer.
def next_backoff(attempt_count: int) -> timedelta | None:
    """The wait before the retry that FOLLOWS `attempt_count` attempts, or None when the
    bounded window is exhausted (the caller then dead-letters). attempt_count==1 (the
    initial attempt failed) -> the 1st schedule entry; ==len(schedule) -> the last; beyond
    -> None."""
    n = int(attempt_count or 0)
    if 1 <= n <= len(RETRY_BACKOFF_SCHEDULE):
        return timedelta(minutes=RETRY_BACKOFF_SCHEDULE[n - 1])
    return None


_URL_RE = re.compile(r"https?://\S+")


_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")


_SECRETISH_RE = re.compile(
    r"(?i)(secret|token|signature|authorization|bearer|password|api[-_]?key)"
    r"[\s=:]+\S+"
)


_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:\\|\\\\)[^\s\"']+"
    r"|(?<![\w/])/(?:[\w.\-]+/)+[\w.\-]+"
)


# L2: separator is now ANY of `=`, `:`, or WHITESPACE, so `auth_token sk-...` and
# `x-locstudio-signature sha256=...` (space-separated) are scrubbed too, not only `k=v`.
# Over-redaction in an error string is always safe; under-redaction leaks a secret.
# L2 + L-a: filesystem paths that could leak a data dir / host layout: a Windows drive-letter
# path, a UNC path, OR (production is Linux) a POSIX absolute path. The POSIX arm is
# conservative -- it requires a leading slash NOT preceded by a word char and at least TWO
# segments (`/data/keys/...`), so it never scrubs `and/or`, a lone `/`, or a bare `/tmp`, and
# URLs are already reduced to `[url]` before this runs.
def sanitize_error(exc) -> str:
    """The SANITIZED transport-failure reason stored on a leg + rendered on the panels: no
    URLs (a poll/endpoint URL can carry a credential in its query), no `bearer <token>`,
    no secret/token/signature-looking values (whitespace- OR `=`/`:`-separated), and no
    drive-letter/UNC filesystem paths. Capped at 500 chars. The native errors already
    reduce the endpoint to host-only, but an underlying exception can still echo a full URL
    or a path, so this is the belt-and-braces scrub before anything is persisted or shown."""
    s = _URL_RE.sub("[url]", str(exc or ""))
    s = _PATH_RE.sub("[path]", s)
    s = _BEARER_RE.sub("bearer [redacted]", s)  # space-separated bearer token
    s = _SECRETISH_RE.sub(lambda m: f"{m.group(1)}=[redacted]", s)
    return s[:500]


def _manual_send(
    vendor: Vendor,
    contract: dict,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> None:
    """The `manual` (file-exchange) transport: a DELIBERATE no-op. There is NO wire, so:
      * NO SSRF guard and NO https requirement run here (there is no target to validate);
      * NO body is built, signed, or transmitted — not a single byte leaves the process;
      * a manual vendor MAY carry endpoint_url (ignored) — we neither read nor validate it.
    Delivery is out-of-band: the operator downloads the leg's rendition
    (vendor_xliff.render_leg_xliff) and hands it to the vendor. Returns None — the honest
    signal that nothing was transported. dispatch_external_step's success path only
    distinguishes raise-vs-return and ignores the value, so None is fully tolerated.
    `transport`/`timeout` are accepted purely for signature parity with the native transport
    and are unused."""
    return None


# --- expiry policy -----------------------------------------------------------
def wait_hours_for(step: WorkflowStep) -> int:
    """The bounded-wait window (hours) for a parked external leg: the step's
    `external_wait_hours` override, else the install `vendor_dispatch_ttl_hours` setting.
    0 = no bounded wait (park indefinitely)."""
    v = getattr(step, "external_wait_hours", None)
    if v is not None:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            pass
    from app.services import settings

    try:
        return max(0, int(settings.get("vendor_dispatch_ttl_hours") or 168))
    except (TypeError, ValueError):
        return 168


# --- frozen operator-delivery package (wire-less legs) -----------------------
def _frozen_package(contract: dict) -> dict:
    """A copy of the just-built dispatch contract to FREEZE on a WIRE-LESS (`manual`) leg,
    so the operator "Download XLIFF" surface renders the DISPATCH-TIME package VERBATIM.

    WHY (AUDIT-omegat-roundtrip-2026-08-10): a run's MT output lives in the run's ROLLING
    TEXT until the run completes -- it is NOT committed to Segment rows while the run is
    parked at the external step. The EXTERNAL branch dispatches the in-flight per-segment
    dicts, so the dispatch-time contract carries the MT draft targets; but for a non-
    RETRY_WAIT leg `vendor_xliff.contract_for_leg` REBUILDS the contract from the live
    Segment rows, whose targets are still EMPTY mid-run. For a NATIVE leg the signed wire
    JSON is authoritative and the rebuilt download is a documented best-effort convenience;
    for a MANUAL leg the DOWNLOAD *is* the delivery, so a rebuilt-from-empty file would hand
    the vendor an untranslated package. Freezing the contract closes that gap.

    MECHANISM: stored in the SAME `VendorDispatch.retry_contract` JSON column that
    `contract_for_leg` ALREADY prefers verbatim for any leg carrying one, so the render path
    needs NO change. A DISPATCHED/QUOTED manual leg carrying it is inert in every retry path --
    `due_retry_legs` / `retry_leg` / `dead_letter_leg` all key on `status == RETRY_WAIT`, which
    a wire-less leg never reaches (the manual transport never touches the wire, so it never
    raises a transient failure to park RETRY_WAIT).

    LIFECYCLE (single-sourced in `_clear_or_keep_frozen`, which EVERY status transition routes
    through): the wire-less operator-delivery package is KEPT while the leg is WORKABLE
    (DISPATCHED / QUOTED / ESCALATED -- the operator must still be able to download the correct
    file after expiry; a return is refused on ESCALATED but the delivery file must never
    silently regress to the empty mid-run rebuild) and CLEARED only when the leg reaches a
    state with no pending delivery (RETURNED / REJECTED / CANCELLED / DEAD_LETTER).
    A DOCUMENT DELETE ERASES IT (2026-08-31): `retention.delete_document` clears
    `retry_contract` + severs `document_id` for every leg of the document, and then deletes
    outright any leg anchored to one of that document's runs (`workflow_run_id` is NOT NULL, so
    such a leg cannot outlive its run) -- which is the ordinary shape, since a real dispatch's
    run points at the document it dispatched. Only a leg whose run belongs to a DIFFERENT
    document survives, package cleared. (Until that fix the surrounding delete was FK-blocked
    for any document with a run, so the clear rolled back and nothing was erased.) This differs
    from the RETRY_WAIT re-send use of the column, which is cleared on EVERY exit -- a native
    leg is never wire-less, so the helper clears it unconditionally there.

    SIZE HYGIENE: strip `assets.xliff` from the FROZEN copy. When the vendor has
    `include_xliff` on, the contract embeds a full XLIFF STRING duplicating the segments; the
    download re-renders XLIFF from the contract anyway, so keeping the embedded copy is pure
    duplication. Every OTHER asset (glossary / style_guide / context / glossary_note) is kept
    intact so the rendered file's `<header>` notes survive.

    CLAIM TOKEN (F3): BLANKED in the frozen copy (`claim_token = ""`). The stored package is
    only ever consumed by `contract_for_leg` (the operator download), which blanks the token
    anyway (R1) -- and the RETRY_WAIT re-send path, the only consumer that re-signs from a
    stored token, never sees a _frozen_package output (a wire-less leg never reaches
    RETRY_WAIT). So nothing needs the real token here, and not storing it keeps the
    operator-delivery package free of the return-admission credential."""
    frozen = dict(contract or {})
    frozen["claim_token"] = ""
    assets = frozen.get("assets")
    if isinstance(assets, dict) and "xliff" in assets:
        stripped = dict(assets)
        stripped.pop("xliff", None)
        frozen["assets"] = stripped
    return frozen


_TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


#: Run statuses past which a dispatch leg must never be touched (H4). CANCELLED (the
#: run-cancel train) is terminal here too: cancelling a run PAUSED at an external vendor
#: step must CLOSE its open legs (close_open_legs_for_run) and DROP any late return
#: (assert_leg_admitting), exactly as completed/failed do — a cancelled run has moved on
#: and a leg must never drag it back to PAUSED.
def is_run_terminal(status) -> bool:
    """True when a run has finished (completed/failed) or been CANCELLED -- a leg for such
    a run is stale and must be CLOSED, never escalated (H4)."""
    return status in _TERMINAL_RUN_STATUSES


_BLOCKING_PAUSE_STATES = frozenset({"dispatched", "wedged", "retry_wait", "quoted"})


#: Vendor-pause states at which a HUMAN "Approve & resume" is REFUSED, because vendor
#: content is (or may be) in flight and a naive resume would re-commit the stale pre-dispatch
#: SOURCE seed over it. "quoted" (Wave E R3) blocks too: the vendor has not started work, so
#: advancing past the vendor with the stale seed would skip the vendor step entirely -- the
#: sanctioned exits from a QUOTED leg are `approve-quote` (work proceeds) or `decline-quote`
#: (escalate to a human). DEAD_LETTER / ESCALATED are NOT here: no vendor content ever landed
#: on them, they have already escalated to a human, and the human resume IS the sanctioned
#: exit (mirroring how an ESCALATED leg has always fallen through to the human flow).
def blocks_human_resume(state: str) -> bool:
    """True when a vendor-pause state must REFUSE a plain human resume (see
    _BLOCKING_PAUSE_STATES)."""
    return state in _BLOCKING_PAUSE_STATES


def vendor_pause_state(session: Session, run_id: int):
    """HIGH-1 + round-3 wedge guard (Wave C-extended): whether the run's CURRENT pause is a
    vendor pause, and how the surfaces should render/gate it. Returns (leg, state):

      * (leg, "dispatched") -- an OPEN leg at the current pause step, awaiting a return.
      * (leg, "retry_wait") -- a RETRY_WAIT leg at the current pause step: a transient
                               dispatch failure is being retried on a backoff. A human
                               "Approve & resume" here would advance PAST the vendor with the
                               stale seed while the package is still trying to go out -- so it
                               is BLOCKED, exactly like "dispatched" (BLOCKER fix).
      * (leg, "wedged")     -- a RETURNED leg at the current pause step whose run is STILL
                               paused there: the return admitted its content + closed the leg
                               but the follow-on resume threw (MEDIUM-4 wedge), so the run
                               never advanced. A human "Approve & resume" here would re-commit
                               the STALE pre-dispatch seed (SOURCE) over the already-committed
                               vendor targets -- the F-CRIT clobber. The sanctioned way out is
                               `vendor redrive`, NOT a human resume.
      * (leg, "dead_letter")-- a DEAD_LETTER leg at the current pause step: the dispatch
                               exhausted its retry window (or was refused non-retryably) and
                               ESCALATED to a human. NO vendor content ever landed, so the
                               human resume IS the sanctioned exit -- this is REPORTED (so the
                               surface can render "dead-lettered -- human review") but does
                               NOT block the resume (blocks_human_resume("dead_letter") is
                               False), mirroring how ESCALATED has always fallen through.
      * (None, "")          -- a genuine human_review pause, an ESCALATED leg, or a cleanly
                               resumed leg (run advanced past its step) -- resume normally.

    The blocking decision is `blocks_human_resume(state)`, NOT `leg is not None`, so callers
    that gate a resume must consult it (a DEAD_LETTER leg returns a non-None leg but must be
    resumable)."""
    sr = session.exec(
        select(StepRun)
        .where(StepRun.workflow_run_id == run_id, StepRun.status == RunStatus.PAUSED)
        .order_by(StepRun.id.desc())
    ).first()
    if sr is None:
        return None, ""
    leg = session.exec(
        select(VendorDispatch)
        .where(
            VendorDispatch.workflow_run_id == run_id,
            VendorDispatch.step_id == sr.step_id,
            VendorDispatch.status.in_(
                (
                    VendorDispatchStatus.DISPATCHED,
                    VendorDispatchStatus.RETRY_WAIT,
                    VendorDispatchStatus.QUOTED,
                    VendorDispatchStatus.RETURNED,
                    VendorDispatchStatus.REJECTED,
                    VendorDispatchStatus.DEAD_LETTER,
                    VendorDispatchStatus.ESCALATED,
                )
            ),
        )
        .order_by(VendorDispatch.id.desc())
    ).first()
    if leg is None:
        return None, ""
    # MED-2: ESCALATED + DEAD_LETTER are REPORTED (so the surface can render a state-only
    # context line) but stay NON-blocking (blocks_human_resume is False for both) -- the
    # human resume IS the sanctioned exit for them.
    _state = {
        VendorDispatchStatus.DISPATCHED: "dispatched",
        VendorDispatchStatus.RETRY_WAIT: "retry_wait",
        VendorDispatchStatus.QUOTED: "quoted",
        VendorDispatchStatus.RETURNED: "wedged",
        # Wave E R4 (finding #7): a REJECTED leg at the current pause is the rework-CAP
        # escalation -- the run re-parked into a human_review pause with no further vendor
        # round. REPORTED so the surface renders "rework escalated -- human review", but
        # NON-blocking (not in _BLOCKING_PAUSE_STATES): no vendor content is in flight, and
        # the human resume IS the sanctioned exit (mirroring ESCALATED / DEAD_LETTER).
        VendorDispatchStatus.REJECTED: "rework_escalated",
        VendorDispatchStatus.DEAD_LETTER: "dead_letter",
        VendorDispatchStatus.ESCALATED: "escalated",
    }[leg.status]
    return leg, _state


def due_retry_legs(session: Session, *, now: datetime | None = None) -> list[VendorDispatch]:
    """Every RETRY_WAIT leg whose backoff has elapsed (next_attempt_at in the past). A leg
    with a NULL next_attempt_at is never due (defensive — a RETRY_WAIT leg always carries
    one)."""
    now = now or datetime.utcnow()
    rows = session.exec(
        select(VendorDispatch).where(VendorDispatch.status == VendorDispatchStatus.RETRY_WAIT)
    ).all()
    return [d for d in rows if d.next_attempt_at is not None and d.next_attempt_at <= now]


def _run_paused_at_leg_step(session: Session, run: WorkflowRun, leg: VendorDispatch) -> bool:
    """HIGH-A: True only when the run is STILL PAUSED at THIS leg's step -- the same predicate
    vendor_pause_state uses. A run that was resumed/advanced (RUNNING, or PAUSED at a LATER
    step) is NOT here, so a stale RETRY_WAIT leg is never re-sent to a real vendor for a run
    that already moved on."""
    if run is None or run.status != RunStatus.PAUSED:
        return False
    sr = session.exec(
        select(StepRun)
        .where(StepRun.workflow_run_id == run.id, StepRun.status == RunStatus.PAUSED)
        .order_by(StepRun.id.desc())
    ).first()
    return sr is not None and sr.step_id == leg.step_id
