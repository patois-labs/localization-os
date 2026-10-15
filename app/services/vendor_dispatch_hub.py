"""vendor_dispatch: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.models import (
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services import egress, vendor_cost, vendors
from app.services.vendor_dispatch_common import (
    CONTRACT_VERSION,
    VendorDispatchError,
    VendorPolicyRefusal,
    _as_segment_int,
    _author_role,
    _endpoint_host,
    _is_retryable_status,
    _manual_send,
    _parse_retry_after,
    _placeholders,
    _safe_config_headers,
)

log = logging.getLogger("loc_tms.vendor_dispatch")


GLOSSARY_TERM_CAP = 500


# VP-0: the sane upper bound on package-level glossary terms shipped in the contract's
# `assets.glossary` block. The terms are the doc-relevant matched constraints, usually a
# handful; the cap only bites on a pathological glossary and a count note rides alongside
# (`assets.glossary_note`) when it does, so the far side knows the list was truncated.
def _glossary_asset(glossary):
    """Normalize whatever the caller resolved as `glossary` into the vendor contract's
    package-level `assets.glossary` shape (tools/vendor_studio/vstudio/contract.py):
    a LIST of {term, target_term, dnt?, forbidden?} dicts, or a pre-rendered string passed
    through verbatim. Returns (asset, note): `asset` is the list/str (or None when empty),
    `note` is a human "showing N of M terms" string when the cap truncated, else "".

    The engine's matched-term dicts carry `source_term`/`target_term`/`dnt`; a hand-built
    or already-normalized list may carry `term`/`target`. Both are accepted. Deduped by
    term (first writer wins), bounded at GLOSSARY_TERM_CAP."""
    if not glossary:
        return None, ""
    if isinstance(glossary, str):
        return glossary, ""
    terms, seen = [], set()
    total = 0
    for t in glossary:
        if not isinstance(t, dict):
            continue
        term = str(t.get("term") or t.get("source_term") or "").strip()
        if not term or term in seen:
            continue
        seen.add(term)
        total += 1
        if len(terms) >= GLOSSARY_TERM_CAP:
            continue
        entry = {"term": term, "target_term": str(t.get("target_term") or t.get("target") or "")}
        if t.get("dnt"):
            entry["dnt"] = True
        if t.get("forbidden"):
            entry["forbidden"] = True
        terms.append(entry)
    if not terms:
        return None, ""
    note = f"showing {len(terms)} of {total} glossary terms (capped)" if total > len(terms) else ""
    return terms, note


_TEST_TRANSPORT: httpx.BaseTransport | None = None


# The injectable outbound transport (hermetic-test seam). Production leaves it None ->
# a real httpx.Client. A test sets it to an httpx.MockTransport so the whole dispatch
# path — including the orchestrator EXTERNAL branch — runs offline, exactly like the
# connector transport injection. `dispatch()` prefers an explicit `transport=` arg over
# this global.
def set_test_transport(transport: httpx.BaseTransport | None) -> None:
    """Install (or clear) the module-level test transport. Test-only."""
    global _TEST_TRANSPORT
    _TEST_TRANSPORT = transport


def _refuse_when_vendors_off() -> None:
    """ffadmin Wave 3 EGRESS KILL SWITCH: when the Vendors feature is switched off no NEW
    vendor work leaves this process (plan §7 -- "closes every surface ... never sends
    anything outward"). The module's two SEND seams (`dispatch` and `post_signed_message`)
    call this FIRST and refuse.

    Raises ConnectorError -- the module's OWN 'a person must fix a local config/policy state'
    refusal (`dispatch` already raises it for an unregistered protocol). It is deterministic
    (a retry can never clear a switched-off feature), so at the workflow seam
    `dispatch_external_step`'s `except ConnectorError` propagates it and the orchestrator FAILs
    the external step with the reason -- a run that hits it FAILS with a readable message
    naming the switch, never a silent skip and never a phantom parked leg. Reads the feature
    registry (`features.is_enabled`), the single-sourced predicate the middleware / route
    gates / scheduler share, so this can never split-brain against them."""
    from app import features

    if not features.is_enabled("vendors"):
        from app.services.connectors.base import ConnectorError

        raise ConnectorError(
            "vendors are disabled (setting vendors_enabled=0); refusing outbound vendor egress."
        )


def _effective_transport(transport):
    """The transport dispatch() will actually use: the explicit arg, else the module test
    transport, else None (a real httpx client). The egress guard keys on THIS."""
    return transport if transport is not None else _TEST_TRANSPORT


def assert_dispatch_egress_allowed(session, project, locale, transport) -> None:
    """H2: the mock-egress perimeter for vendor dispatch. A keyless / MockProvider install
    must NEVER POST deterministic offline placeholder text (e.g. '[es-ES] Hello world.') to
    a REAL vendor endpoint -- the same egress law every connector push obeys
    (connectors.egress_guard). Fires ONLY when the egress is REAL (transport is not a
    MockTransport / test double) AND the provider that produced this locale's content is
    NOT live, unless `allow_mock_egress` is set. Raises egress_guard.MockEgressBlocked
    BEFORE any bytes leave the process; the EXTERNAL branch turns that into a FAILED step."""
    from app.services.connectors import egress_guard

    if egress_guard.allow_mock_egress():
        return
    if not egress_guard.egress_is_real(transport):
        return
    from app.services import providers

    name = None
    if project is not None:
        try:
            from app.services import translate

            name = translate.spec_for_pair(session, project, locale).provider
        except Exception:
            name = getattr(project, "default_provider", None)
    from app import config as _config

    name = name or _config.DEFAULT_PROVIDER or "claude"
    if providers.provider_is_live(name):
        return
    raise egress_guard.MockEgressBlocked(
        f"refusing to dispatch to a REAL vendor endpoint: provider {name!r} is not live "
        f"(no API key / disabled engine), so the packaged content is deterministic offline "
        f"MockProvider placeholder text, not a real translation. Configure a live provider "
        f"key for this project, or set `allow_mock_egress` to permit mock output."
    )


# --- vendor resolution (fail-closed) -----------------------------------------
def resolve_vendor(session: Session, step: WorkflowStep, *, customer_id, org_id=None) -> Vendor:
    """The vendor an EXTERNAL step dispatches to, or raise VendorError. FAIL-CLOSED:
    the vendor must be configured on the step AND linked to the document's customer AND
    not archived — `vendors.vendor_for_customer` enforces all three. The customer LINK is
    the authorization: a cross-org-linked vendor resolves fine (linking is unrestricted
    across orgs, 2026-08-08), so the buyer's org and the vendor's org may differ. A
    document with no customer resolves NO vendor (an external step needs a customer-scoped
    vendor)."""
    vid = getattr(step, "vendor_id", None)
    label = step.name or getattr(step, "key", None) or "external"
    if not vid:
        raise vendors.VendorError(f"step '{label}': no vendor is configured on this external step.")
    v = vendors.vendor_for_customer(session, vid, customer_id, org_id=org_id)
    if v is None:
        raise vendors.VendorError(
            f"step '{label}': vendor {vid} is not connected to this customer "
            "(or is archived) -- dispatch refused (fail-closed)."
        )
    return v


_QUERIES_PER_SEGMENT = 5


_POSTS_PER_QUERY = 10


# --- the JSON contract -------------------------------------------------------
# --- Wave 3 Train E: the buyer query carry on the outbound package (§7.2) ------
# The `queries` block mirrors `feedback`'s shape but is a SEPARATE key (§7.1): folding it
# into the rework-scoped `feedback` would make a conversation vanish on the next rework.
#: caps (§3.4/§7.2): at most this many threads per segment, this many newest posts each,
#: carried on a dispatch package. A projection cannot REFUSE, so it DECLARES what it
#: dropped -- `queries_truncated` + `queries_omitted` (never a silent cut).
def _queries_enabled() -> bool:
    """The `queries_enabled` kill switch (§5.2). When OFF the whole `queries` key is
    OMITTED from the package (never an empty list -- that would leak that the feature
    exists). An unknown slug fail-opens to True (features.is_enabled) so this seam is
    live on any tree where the `queries` Module has landed; Train B ships that Module."""
    from app import features

    return features.is_enabled("queries")


def collect_outbound_queries(session: Session, segment_ids, target_locale: str) -> dict:
    """{segment_id: {"queries": [...], "queries_truncated": bool, "queries_omitted": int}}
    for the buyer threads that ride out with this leg (§7.2). The output is keyed by the SAME
    id VALUE passed in (int on the dispatch path, str on the XLIFF-rebuild path), so the
    build_contract lookup matches regardless of id spelling.

    Included: threads scoped to that segment whose `target_locale` matches the leg's, in
    status `open` or `answered` -- BOTH predicates pushed into the SQL (gate H1), so live
    threads are selected before any cap. A `closed` thread is never shipped and can never
    crowd a live one out of the page. Whole-thread visibility is ratified (§3.1), so a
    thread's whole post log ships, newest-capped; `queries_omitted` counts the TRUE total of
    hidden posts across ALL live threads (gate M1), never an under-count off a 50-row page.
    NO author email ever crosses the wire -- only the coarse `author_role`. Best-effort: any
    failure yields NOTHING for that segment (the caller degrades to no-queries, never a
    failed dispatch)."""
    # This OUTBOUND carry is a READ PROJECTION and deliberately reaches the query TABLES
    # through app.models (not app.services.query_threads) so the two-status + locale filter can
    # be pushed into SQL (gate H1). That mirrors the §5.2 "fourth seam" (content/retention/gdpr
    # reach the tables through app.models too) and keeps this module OFF the cross-feature-reach
    # scanner -- no cross_reach_allow row is needed for the dispatch side. The INBOUND WRITE
    # still goes through the service's record_vendor_post (transaction law + dedup + caps).
    from sqlalchemy import func as _func

    from app.models import QueryPost, QueryStatus, QueryThread

    out: dict = {}
    want_loc = (target_locale or "").lower()
    for sid in segment_ids:
        seg_int = _as_segment_int(sid)
        if seg_int is None:
            continue
        try:
            # Status + locale pushed into the QUERY (H1): live threads are the candidate set,
            # newest-first, BEFORE the 5-thread cap; closed/other-locale threads are excluded
            # in SQL and can never occupy a cap slot.
            eligible = list(
                session.exec(
                    select(QueryThread)
                    .where(
                        QueryThread.segment_id == seg_int,
                        QueryThread.status.in_((QueryStatus.OPEN, QueryStatus.ANSWERED)),
                        _func.lower(QueryThread.target_locale) == want_loc,
                    )
                    .order_by(QueryThread.updated_at.desc(), QueryThread.id.desc())
                ).all()
            )
        except Exception:
            log.warning("outbound queries: query failed for segment %s", seg_int, exc_info=True)
            continue
        if not eligible:
            continue
        shipped = eligible[:_QUERIES_PER_SEGMENT]
        # "comments not shown" = posts hidden by BOTH caps, counted over the TRUE eligible set
        # (M1): whole omitted live threads' posts + posts beyond the per-thread cap on shipped
        # threads. post_count is the denormalized authority (§3.1/L4) -- no extra query.
        omitted = sum(int(t.post_count or 0) for t in eligible[_QUERIES_PER_SEGMENT:])
        blocks = []
        for t in shipped:
            try:
                posts = list(
                    session.exec(
                        select(QueryPost)
                        .where(QueryPost.thread_id == t.id)
                        .order_by(QueryPost.created_at, QueryPost.id)
                    ).all()
                )
            except Exception:
                log.warning("outbound queries: thread %s posts load failed", t.id, exc_info=True)
                continue
            newest = posts[-_POSTS_PER_QUERY:] if len(posts) > _POSTS_PER_QUERY else posts
            omitted += max(0, len(posts) - _POSTS_PER_QUERY)
            blocks.append(
                {
                    "ref": str(t.id),
                    "subject": t.subject or "",
                    "status": t.status,
                    "posts": [
                        {
                            "body": p.body or "",
                            "author_role": _author_role(p.author_kind),
                            "at": (p.created_at.isoformat() if p.created_at else ""),
                        }
                        for p in newest
                    ],
                }
            )
        if not blocks:
            continue
        entry = {"queries": blocks}
        if omitted > 0:
            entry["queries_truncated"] = True
            entry["queries_omitted"] = int(omitted)
        out[sid] = entry
    return out


def build_contract(
    *,
    run: WorkflowRun,
    step: WorkflowStep,
    segments: list[dict],
    source_locale: str,
    target_locale: str,
    claim_token: str,
    glossary=None,
    style_guide: str = "",
    context: str = "",
    vendor: Vendor | None = None,
    rework: dict | None = None,
    queries_by_segment: dict | None = None,
) -> dict:
    """The versioned dispatch payload (§3.2 + VP-0 enrichment). `segments` is a list of
    per-segment dicts, each `{id, source, target?, placeholders?, tm_match_pct?,
    lock_band?}`; this normalizes them into the contract's stable shape and attaches the
    optional asset blocks the caller already resolved (glossary/style/context) so an
    external engine can be as informed as our own translator.

    VP-0 ENRICHMENT (PROPOSAL-vendor-platform §3, additive; the far side tolerates absence
    under contract_version 1.0). Per segment, derived PURELY from what Localization OS already
    knows — the vendor_cost TM-band classification (the SAME one that prices the estimate,
    incl. within-packaging repetition accounting) plus target presence:
      * `provenance`   -- tm_exact | tm_fuzzy | mt | new (covered bands -> tm_exact; the
                          three fuzzy tiers -> tm_fuzzy; `new` -> "mt" when the current
                          value came from MT, i.e. a target is present, else "new").
      * `match_score`  -- the TM match %, on tm_fuzzy segments only (else omitted).
      * `locked` / `locked_reason` -- True + a band-naming reason ("100% TM match",
                          "context match", "repetition") on COVERED-band segments (the
                          visible-but-locked research decision).
    `omit_locked_targets` (per-vendor config): when set, a locked segment keeps its source
    + locked marker but ships an EMPTY target -- the vendor never receives the leveraged
    text. The id still ships (and still returns), so the return-side id accounting is
    UNCHANGED. Locked-band cost math is unaffected: this touches the WIRE payload only, not
    the estimate the leg persists.

    CONTRACT LAW (§3.2, enforced on RETURN in Wave B): every dispatched segment id must
    return exactly once, right locale, no extras. The `claim_token` scopes the return to
    THIS leg. Secrets are never in the payload — only the claim token + content."""
    omit_locked = vendors.omit_locked_targets(vendor) if vendor is not None else False
    # Wave E R4: per-segment rework feedback (which guard lanes / LQA findings failed on the
    # prior delivery + any operator note), keyed by the normalized string id so it attaches to
    # the right segment regardless of int/str id shape on the wire.
    feedback_by_id: dict = {}
    if rework:
        for fb in rework.get("segments") or []:
            if isinstance(fb, dict) and fb.get("id") is not None:
                feedback_by_id[str(fb.get("id")).strip()] = fb
    seg_out = []
    seen: set = set()  # running set for the within-packaging repetition classification
    for i, seg in enumerate(segments or []):
        # LOW-2: read the CLASSIFICATION fields through the ONE definition of a segment's
        # fields (vendor_cost._seg_fields), so the contract and the estimate can never
        # diverge on what a segment's source / tm_match_pct / lock_band are.
        src, pcts, lock_band = vendor_cost._seg_fields(seg)
        sid = seg.get("id") if isinstance(seg, dict) else getattr(seg, "id", i)
        tgt = seg.get("target") if isinstance(seg, dict) else None
        ph = seg.get("placeholders") if isinstance(seg, dict) else None
        # Classify against the SAME band model the estimate uses (repetition accounting is
        # order-dependent on `seen`, added AFTER classification to match estimate_from_segments).
        band = vendor_cost.classify_band(src or "", pcts, lock_band, seen)
        locked = band in vendor_cost.COVERED_BANDS
        provenance = vendor_cost.PROVENANCE_BY_BAND.get(band, "new")
        # `new` band -> "mt" when the current working value came from MT (a target is present),
        # else genuinely "new" (nothing usable yet). Covered/fuzzy provenance is band-fixed.
        if provenance == "new" and (tgt or "").strip():
            provenance = "mt"
        # omit_locked_targets: blank the target on a locked segment (source + marker only).
        wire_target = "" if (locked and omit_locked) else (tgt or "")
        # LOW-1: a locked segment HAS content (it is leveraged, just withheld when omitted),
        # so its state is "needs-review-translation" even when the target is blanked -- the
        # far side reviews a locked row, it never re-translates it from scratch.
        has_content = bool((tgt or "").strip()) or locked
        entry = {
            "id": sid,
            "source": src or "",
            "target": wire_target,
            "state": "needs-review-translation" if has_content else "needs-translation",
        }
        if pcts is not None:
            entry["tm_match_pct"] = pcts
        # --- VP-0 enrichment (additive) ---
        entry["provenance"] = provenance
        if provenance == "tm_fuzzy" and pcts is not None:
            try:  # noqa: SIM105
                entry["match_score"] = int(pcts)
            except (TypeError, ValueError):
                pass
        if locked:
            entry["locked"] = True
            entry["locked_reason"] = vendor_cost.LOCKED_REASONS.get(band, "TM match")
        # The tag inventory the far side must preserve (caller-supplied, else extracted
        # from the source). Always present so the return-side integrity check (Wave B)
        # has a per-segment baseline.
        entry["placeholders"] = list(ph) if ph else _placeholders(src or "")
        # Wave E R4: on a rework dispatch, attach the per-segment feedback (the issues the
        # prior delivery failed + any note) so the vendor's CAT editor can show it in-line.
        fb = feedback_by_id.get(str(sid).strip())
        if fb:
            issues = [str(x) for x in (fb.get("issues") or []) if str(x).strip()]
            fb_entry = {}
            if issues:
                fb_entry["issues"] = issues
            if str(fb.get("note") or "").strip():
                fb_entry["note"] = str(fb.get("note")).strip()
            if fb_entry:
                entry["feedback"] = fb_entry
        # Wave 3 Train E (§7.2): the buyer query carry, keyed by the SAME segment id the
        # rest of the entry uses. Attached ONLY when non-empty (a segment with no open/
        # answered threads is byte-identical to the pre-Wave-3 package), and the whole
        # block is OMITTED when queries_enabled=0 (the caller passes None -> nothing here).
        q = (queries_by_segment or {}).get(sid)
        if q and q.get("queries"):
            entry["queries"] = q["queries"]
            if q.get("queries_truncated"):
                entry["queries_truncated"] = True
                entry["queries_omitted"] = int(q.get("queries_omitted") or 0)
        seg_out.append(entry)
        key = (src or "").strip()
        if key:
            seen.add(key)
    contract = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run.id,
        "step_key": getattr(step, "key", None) or "",
        "step_name": step.name or "",
        "claim_token": claim_token,
        "source_locale": source_locale,
        "target_locale": target_locale,
        "targets": [target_locale],
        "segments": seg_out,
        "assets": {},
    }
    # Wave E R3 (quote gate): a per-vendor `requires_quote_approval` vendor asks the far
    # side to SUBMIT A QUOTE before starting work. Additive top-level flag under the SAME
    # contract_version 1.0 (the VP-0 precedent: the far side tolerates unknown keys), present
    # ONLY when the flag is on -- so a non-quote-gated dispatch is BYTE-IDENTICAL. Vendor
    # Studio reads it and gates "Start work" behind "Submit quote".
    if vendor is not None and vendors.requires_quote_approval(vendor):
        contract["quote_requested"] = True
    # Wave E R4 (reject/rework): a top-level `rework` marker so the far side renders the job
    # clearly as REWORK, references the prior delivery, and shows the overall note. Per-segment
    # detail rides `segments[i].feedback` above. Present ONLY on a rework re-dispatch -> an
    # original dispatch is byte-identical.
    if rework:
        blk = {"round": int(rework.get("round") or 1), "is_rework": True}
        if str(rework.get("note") or "").strip():
            blk["note"] = str(rework.get("note")).strip()
        if rework.get("prior_run_id") is not None:
            blk["prior_run_id"] = rework.get("prior_run_id")
        if rework.get("prior_dispatch_id") is not None:
            blk["prior_dispatch_id"] = rework.get("prior_dispatch_id")
        contract["rework"] = blk
    gloss_asset, gloss_note = _glossary_asset(glossary)
    if gloss_asset is not None:
        contract["assets"]["glossary"] = gloss_asset
        if gloss_note:
            contract["assets"]["glossary_note"] = gloss_note
    if style_guide:
        contract["assets"]["style_guide"] = style_guide
    if context:
        contract["assets"]["context"] = context
    # Vendor Wave D (interop R2): an OPT-IN XLIFF 1.2 rendition of this contract, embedded
    # as `assets.xliff`, for a vendor system that wants a CAT-openable package on the wire.
    # Default OFF (per-vendor `include_xliff` config) so the wire stays byte-identical for
    # every existing vendor; the signed JSON contract remains canonical. A serialization of
    # the SAME segments already assembled above -- no new semantics.
    if vendor is not None and vendors.include_xliff(vendor):
        # HIGH-2: the XLIFF is a CONVENIENCE; the signed JSON contract is canonical, so a
        # rendering fault must NEVER break a dispatch. The writer already sanitizes XML-
        # invalid codepoints, but this belt-and-braces guard means a benign control char (or
        # any future writer bug) drops the assets.xliff and lets the dispatch proceed.
        try:
            from app.services import vendor_xliff

            contract["assets"]["xliff"] = vendor_xliff.render_contract_xliff(contract).decode(
                "utf-8"
            )
        except Exception:
            log.warning(
                "include_xliff: XLIFF rendition failed; dispatching JSON only", exc_info=True
            )
    return contract


NATIVE_PROTOCOL = "native"


MANUAL_PROTOCOL = "manual"


# --- transport protocol registry (interop R1, Wave C — seam only) ------------
# INVARIANT (PROPOSAL-vendor-portal-interop R1, non-negotiable): a protocol implementation
# is a TRANSPORT ONLY. It decides how bytes travel to / from the vendor system; it NEVER
# decides how a return is judged. Every inbound path — signed webhook callback, polling,
# manual re-upload — funnels through vendor_return.admit_return() -> quality.commit_mt_output
# with EVERY guard lane on. No protocol may ever gain its own admission path. Adding a
# per-LSP adapter (lionbridge / globallink / trados — LATER waves, blocked on real sandbox
# tenants) means implementing outbound send + inbound fetch that MAP our canonical contract
# onto the vendor's client API, then registering it here; the admission seam below is
# untouchable by construction.
# The FILE-EXCHANGE / manual tier (interop research F2 Tier-2 — the LSP majority: an SFTP
# drop, an email round trip, or a human running a desktop CAT tool like OmegaT). A `manual`
# vendor has NO system endpoint; "delivery" is the operator downloading the leg's XLIFF/JSON
# rendition (vendor_xliff.render_leg_xliff) and handing it to the vendor out-of-band. It is
# still a TRANSPORT under R1 — a manual leg's return funnels through the SAME
# vendor_return.admit_manual -> quality.commit_mt_output seam with every guard lane on.
class _NativeTransport:
    """The `native` protocol: our own signed JSON contract POSTed to the vendor's declared
    endpoint (outbound) and pulled from its `poll_url` (inbound poll, in vendor_return).
    This is today's exact behavior — byte-identical for every existing native vendor."""

    name = NATIVE_PROTOCOL
    #: F5: whether this transport moves bytes OFF the box. `wireless=False` -> native rides
    #: the wire (SSRF-guarded HTTPS POST), so the egress perimeter runs and the endpoint host
    #: is recorded. The egress-skip + host decisions key off `wireless`, resolved ONCE, so a
    #: future wired protocol never has to be re-listed at each decision site.
    wireless = False

    def send(
        self,
        vendor: Vendor,
        contract: dict,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        return _native_send(vendor, contract, transport=transport, timeout=timeout)


class _ManualTransport:
    """The `manual` (file-exchange) protocol: for a vendor with NO reachable system endpoint
    (F2 Tier-2 — the LSP majority: SFTP/email tiers, or a human with a desktop CAT tool). Its
    `send` performs NO network I/O at all — dispatch "delivery" is the operator later
    downloading the leg's XLIFF/JSON rendition and handing it to the vendor out-of-band; the
    signed JSON wire contract is simply never transmitted.

    The leg still lands EXACTLY like a delivered native dispatch (DISPATCHED, or QUOTED under
    the quote gate — that snapshot composes unchanged): claim token minted, dispatched_ids
    ledger frozen, estimate recorded, expiry set. In particular external_wait_hours still
    applies — a manual leg nobody returns escalates to a human on expiry, which is correct and
    desirable (a file handed to a vendor who never delivers must not park forever).

    R1 INVARIANT: this is a TRANSPORT ONLY. It admits nothing; a manual leg's return still
    funnels through vendor_return.admit_manual -> quality.commit_mt_output with every guard
    lane on, exactly like a native leg — that is the whole point of building `manual` as an
    OUTBOUND protocol rather than a special case."""

    name = MANUAL_PROTOCOL
    #: F5: `wireless=True` -> the file-exchange transport puts NOTHING on the wire, so the
    #: egress perimeter is SKIPPED (nothing to leak) and the leg records an EMPTY host. Every
    #: protocol-sensitive decision keys off this single attribute (resolved once per dispatch).
    wireless = True

    def send(
        self,
        vendor: Vendor,
        contract: dict,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        return _manual_send(vendor, contract, transport=transport, timeout=timeout)


_PROTOCOLS: dict[str, object] = {
    NATIVE_PROTOCOL: _NativeTransport(),
    MANUAL_PROTOCOL: _ManualTransport(),
}


# protocol name -> transport implementation. Ordered; `native` (the signed-JSON wire) and
# `manual` (file-exchange, no wire) are the registered entries.
def protocol_names() -> tuple[str, ...]:
    """Every registered transport protocol name (`native`, `manual`). The vocabulary the
    vendor-create/update validation (web/CLI/API) checks a submitted protocol against, and
    the choices the CLI/web protocol pickers offer — both read it, so a new protocol here
    appears on every surface automatically."""
    return tuple(_PROTOCOLS)


def is_known_protocol(name) -> bool:
    """True when `name` (a blank/None defaults to native) names a registered protocol.
    The validation gate: an unknown protocol is refused at vendor create/update time,
    never at dispatch time."""
    return (str(name).strip().lower() if name else NATIVE_PROTOCOL) in _PROTOCOLS


def vendor_protocol(vendor) -> str:
    """The effective transport protocol for a vendor: its `protocol` field, defaulting to
    `native` when NULL/'' (a legacy row) or — defensively — when it names a protocol no
    longer registered. This is the single dispatch-by-protocol read seam."""
    p = getattr(vendor, "protocol", None) or NATIVE_PROTOCOL
    p = str(p).strip().lower()
    return p if p in _PROTOCOLS else NATIVE_PROTOCOL


def leg_protocol(leg, vendor=None) -> str:
    """F2: the transport protocol a dispatch LEG was recorded under (its `protocol` snapshot),
    falling back to NATIVE when the snapshot is NULL/'' or (defensively) names a protocol no
    longer registered. This is the seam EVERY protocol-sensitive consumer keys off (retry re-
    dispatch, the poll refusal, pollable_legs, leg_to_dict.pollable, the rework re-dispatch), so
    a vendor flipped native<->manual while the leg is parked can never retroactively change how
    the already-dispatched leg is treated.

    F2-fix (BLOCKING re-review): the NULL-snapshot fallback is NATIVE_PROTOCOL, NOT the vendor's
    LIVE protocol. `manual` did not exist before migration d4e5f6a7b8c9, so EVERY pre-migration
    VendorDispatch row is native BY CONSTRUCTION -- it went over the wire (the only transport
    that existed). Falling back to `vendor_protocol(vendor)` here resurrected the exact phantom-
    delivery / poll-stop bugs the snapshot exists to prevent: after an operator flips the vendor
    to `manual`, a legacy native leg would read `manual` and (a) let a retry re-dispatch route
    through the manual no-op -> a PHANTOM delivery of a package that never left the process, and
    (b) drop out of the poll sweep though it was really delivered natively. The live-vendor
    fallback WAS the pre-F2 bug restated; a NULL snapshot is unambiguous history, so it resolves
    to `native`, never to whatever the vendor happens to be configured as now. Consequences of
    the fix on a legacy NULL leg: the retry protocol-mismatch guard now FIRES on a native->manual
    flip (dead-letters, mirroring the snapshotted case); the poll still continues (it was really
    delivered natively, so it stays pollable); and leg_to_dict.protocol / the template chip show
    `native`, the correct display (a legacy leg WAS native). `vendor` is retained for call-site
    signature parity but no longer decides the NULL case."""
    snap = str(getattr(leg, "protocol", None) or "").strip().lower()
    if snap in _PROTOCOLS:
        return snap
    return NATIVE_PROTOCOL


def transport_for_protocol(name: str):
    """The transport implementation for a protocol NAME (already normalized to a registered
    protocol by vendor_protocol / leg_protocol). Used to route a dispatch through a LEG's
    snapshotted protocol (retry / rework) rather than the vendor's current one."""
    return _PROTOCOLS[name if name in _PROTOCOLS else NATIVE_PROTOCOL]


def transport_for(vendor):
    """The transport implementation a vendor's dispatch/poll routes through."""
    return _PROTOCOLS[vendor_protocol(vendor)]


# --- outbound dispatch (SSRF-guarded, injectable transport) ------------------
def dispatch(
    vendor: Vendor,
    contract: dict,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
    impl=None,
) -> httpx.Response | None:
    """Send the contract to the vendor, through its transport PROTOCOL (interop R1 seam).

    F6: the return type is `httpx.Response | None` — the wired `native` transport returns the
    response; the `manual` (file-exchange) transport puts NOTHING on the wire and returns None.
    Callers only distinguish raise-vs-return, so None is fully tolerated.

    F5: `impl` lets a caller pin the transport implementation to a LEG's snapshotted protocol
    (retry / rework re-dispatch) rather than the vendor's CURRENT one — so a vendor flipped
    native<->manual while a leg is parked can never redirect an in-flight leg onto a different
    transport (e.g. phantom-"delivering" a native retry through the manual no-op). When `impl`
    is None the vendor's live transport is used, exactly as before.

    M6: the dispatch seam FAILS CLOSED, matching the poll seam, ALWAYS (even when `impl` is
    pinned): a NULL/'' vendor protocol normalizes to `native` (a legacy row), but a NON-EMPTY
    value that names no registered protocol is REFUSED here — an unregistered protocol is a
    LOCAL CONFIG error a person must fix, so it raises **ConnectorError** (first dispatch then
    FAILs the step; the retry path already dead-letters on ConnectorError). NOT a
    VendorPolicyRefusal (that class means a non-retryable VENDOR response and must not also be
    a ConnectorError, or 3xx/403/404 would wrongly reroute). `native` POSTs the signed JSON
    contract exactly as before. Raises ConnectorError (SSRF / bad scheme / non-https / unknown
    protocol), VendorDispatchError (transient), or VendorPolicyRefusal (non-retryable vendor
    response)."""
    _refuse_when_vendors_off()  # ffadmin Wave 3: no new vendor egress when off
    from app.services.connectors.base import ConnectorError

    raw = getattr(vendor, "protocol", None)
    if raw and str(raw).strip().lower() not in _PROTOCOLS:
        raise ConnectorError(
            f"vendor {getattr(vendor, 'id', '?')} has an unregistered transport protocol "
            f"{str(raw).strip().lower()!r}; refusing to dispatch (a person must fix it)."
        )
    if impl is None:
        impl = transport_for(vendor)
    return impl.send(vendor, contract, transport=transport, timeout=timeout)


def _native_send(
    vendor: Vendor,
    contract: dict,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """POST the contract to the vendor's endpoint, SSRF-guarded and HMAC-signed (the
    `native` transport implementation).

    The endpoint is validated by the SHARED SSRF guard (connectors.base._validate_target
    — private/loopback/link-local/CGNAT/metadata-IP + bad-scheme rejection). A hostile
    endpoint raises ConnectorError BEFORE any request leaves the process. The raw body is
    signed with the vendor's `signing_secret` (HMAC-SHA256, hex, `X-LocStudio-Signature:
    sha256=<hex>`) so Wave B's return callback can prove provenance; an optional
    `auth_token` rides as a bearer header. Uses the injected transport (explicit arg, else
    the module test transport, else a real client) so hermetic tests never touch the wire.

    Raises ConnectorError (SSRF/bad scheme/non-https), VendorDispatchError (non-2xx /
    transport error)."""
    from app.services.connectors.base import ConnectorError, _validate_target

    url = (vendor.endpoint_url or "").strip()
    # M2: HTTPS-ONLY scheme allowlist at dispatch (§5). The SSRF guard permits http+https
    # for offline connectors; a vendor dispatch carrying signed content + a bearer token
    # must never leave over cleartext http.
    if urlsplit(url).scheme.lower() != "https":
        raise ConnectorError(
            f"vendor endpoint must use https (got scheme {urlsplit(url).scheme or '(none)'!r})."
        )
    url = _validate_target(url, subsystem="vendor_dispatch")

    import json as _json

    body = _json.dumps(contract, ensure_ascii=False).encode("utf-8")
    cfg = dict(vendor.config or {})
    # M1: FORCE POST. A config-driven method (arbitrary GET/DELETE/... to the endpoint) is
    # not a Wave A feature; a method allowlist would be a deliberate future design.
    method = "POST"
    # M1: config headers are UNTRUSTED extras. Apply an allowlisted, sanitized set FIRST,
    # then set OUR headers LAST so config can never override Host (request smuggling), the
    # claim/signature/version (forge a return match), the content-type, or the bearer.
    headers = _safe_config_headers(cfg)
    headers["content-type"] = "application/json"
    headers["x-locstudio-claim"] = contract.get("claim_token", "")
    headers["x-locstudio-contract-version"] = CONTRACT_VERSION
    secret = vendors.secret_dict(vendor)
    signing = secret.get("signing_secret") or ""
    if signing:
        sig = hmac.new(signing.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["x-locstudio-signature"] = f"sha256={sig}"
    auth_token = secret.get("auth_token") or ""
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"

    tport = transport if transport is not None else _TEST_TRANSPORT
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            **egress.guarded_transport_kwargs(
                "vendor_dispatch", transport=tport, error_type=ConnectorError
            ),
        ) as client:
            resp = client.request(method, url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        # A transport-level error (ConnectError / timeout / reset) is always TRANSIENT.
        raise VendorDispatchError(
            f"dispatch to {_endpoint_host(url)} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if 200 <= resp.status_code < 300:
        return resp
    # M4 taxonomy: split the non-2xx response. A retryable status (5xx / 408 / 429) is a
    # TRANSIENT failure the backoff retries (honoring Retry-After when the vendor sent it);
    # anything else (3xx delivered-to-nobody since redirects are not followed, or a
    # non-retryable 4xx like 401/403/404/413) is a POLICY refusal a retry can never clear.
    host = _endpoint_host(url)
    if _is_retryable_status(resp.status_code):
        raise VendorDispatchError(
            f"dispatch to {host} returned HTTP {resp.status_code} "
            f"(retryable; will back off and retry).",
            retry_after=_parse_retry_after(resp.headers.get("retry-after")),
        )
    raise VendorPolicyRefusal(
        f"dispatch to {host} returned HTTP {resp.status_code} "
        f"(non-retryable response; a person must decide)."
    )


# --- signed control message (Wave E R3: quote-decision notice) ----------------
def post_signed_message(
    vendor: Vendor,
    body: dict,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    """POST a small SIGNED CONTROL MESSAGE (no translation content) to the vendor's endpoint,
    SSRF-guarded + HMAC-signed exactly like a dispatch. Used for the Wave E quote-decision
    notice (`{type: "quote_decision", ...}`) so a quote-gated vendor learns it may start work
    (approved) or stop (declined). It carries NO packaged content, so the mock-egress
    perimeter does not apply (like a poll); the SSRF guard + https allowlist very much do.
    Raises ConnectorError / VendorDispatchError / VendorPolicyRefusal like `dispatch()`; the
    caller (notify_quote_decision) treats every failure as best-effort."""
    _refuse_when_vendors_off()  # ffadmin Wave 3: no new vendor egress when off
    import json as _json

    from app.services.connectors.base import ConnectorError, _validate_target

    url = (vendor.endpoint_url or "").strip()
    if urlsplit(url).scheme.lower() != "https":
        raise ConnectorError(
            f"vendor endpoint must use https (got scheme {urlsplit(url).scheme or '(none)'!r})."
        )
    url = _validate_target(url, subsystem="vendor_dispatch")
    raw = _json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _safe_config_headers(dict(vendor.config or {}))
    headers["content-type"] = "application/json"
    headers["x-locstudio-claim"] = str(body.get("claim_token") or "")
    headers["x-locstudio-contract-version"] = CONTRACT_VERSION
    secret = vendors.secret_dict(vendor)
    if secret.get("signing_secret"):
        sig = hmac.new(secret["signing_secret"].encode("utf-8"), raw, hashlib.sha256).hexdigest()
        headers["x-locstudio-signature"] = f"sha256={sig}"
    if secret.get("auth_token"):
        headers["authorization"] = f"Bearer {secret['auth_token']}"
    tport = transport if transport is not None else _TEST_TRANSPORT
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            **egress.guarded_transport_kwargs(
                "vendor_dispatch", transport=tport, error_type=ConnectorError
            ),
        ) as client:
            resp = client.request("POST", url, content=raw, headers=headers)
    except httpx.HTTPError as exc:
        raise VendorDispatchError(
            f"quote-decision notice to {_endpoint_host(url)} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if 200 <= resp.status_code < 300:
        return resp
    if _is_retryable_status(resp.status_code):
        raise VendorDispatchError(
            f"quote-decision notice to {_endpoint_host(url)} returned HTTP "
            f"{resp.status_code} (retryable)."
        )
    raise VendorPolicyRefusal(
        f"quote-decision notice to {_endpoint_host(url)} returned HTTP "
        f"{resp.status_code} (non-retryable)."
    )


# --- record the dispatch -----------------------------------------------------
def record_dispatch(
    session: Session,
    *,
    vendor: Vendor,
    run: WorkflowRun,
    step: WorkflowStep,
    document_id,
    target_locale: str,
    claim_token: str,
    endpoint_host: str,
    estimate: dict,
    segment_count: int,
    word_count: int,
    expires_at: datetime | None,
    org_id=None,
    customer_id=None,
    dispatched_ids=None,
    omitted_ids=None,
    status: str = VendorDispatchStatus.DISPATCHED,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    last_error: str = "",
    retry_contract: dict | None = None,
    requires_quote: bool = False,
    rework_round: int = 0,
    rework_of=None,
    rework_feedback: dict | None = None,
    protocol: str | None = None,
) -> VendorDispatch:
    """Persist the VendorDispatch leg (claim token + the scoping estimate + host-only
    endpoint). The estimate's `total_cost` is the headline the vendor's returned actual
    reconciles against (decision 5c).

    `dispatched_ids` (Wave B) is the exact segment-id list the contract carried; it is
    frozen into the `contract_ids` ledger here because it is NOT recoverable later (the
    document's segment rows drift after dispatch). The return path's id accounting -- the
    FIRST gate, before any content guard -- is measured against it.

    Wave C (interop R5): `status` is DISPATCHED on a delivered dispatch, or RETRY_WAIT when
    a TRANSIENT transport failure parks the leg for a scheduled retry -- in which case
    `attempt_count` / `next_attempt_at` / `last_error` carry the backoff state and
    `retry_contract` holds the exact package to re-send.

    FROZEN PACKAGE (`retry_contract`, dual use): the column also holds the frozen dispatch-
    time contract on a DELIVERED WIRE-LESS (`manual`) leg -- the operator-delivered package
    the download renders verbatim (AUDIT-omegat-roundtrip-2026-08-10; see _frozen_package).
    A native delivered leg leaves it NULL (byte-identical). Only the RETRY_WAIT re-send path
    keys on status, so a manual DISPATCHED/QUOTED leg carrying it is inert in every retry.

    F2: `protocol` snapshots the transport this leg is dispatched under. When None (the
    default) it is captured from `vendor_protocol(vendor)` at THIS instant, so the leg
    remembers how it was sent even if the vendor config flips while it is parked. A caller may
    pass an explicit protocol to inherit a lineage's snapshot (a rework re-dispatch keys off
    the ORIGINAL leg's protocol, not the vendor's possibly-changed current one)."""
    est = estimate or {}
    ledger = (
        {"dispatched": [str(i) for i in dispatched_ids], "returned": []}
        if dispatched_ids is not None
        else None
    )
    # VP-0 (omit_locked_targets): the ids dispatched with a BLANKED target. A blank return
    # for one of these is a pure no-op on the return side (the withheld locked target is
    # not "untranslated"); a NON-blank return is judged normally. Stored on the ledger so
    # the frozen-package retry re-sends the same accounting.
    if ledger is not None and omitted_ids:
        ledger["omitted"] = [str(i) for i in omitted_ids]
    d = VendorDispatch(
        contract_ids=ledger,
        vendor_id=vendor.id,
        workflow_run_id=run.id,
        step_id=step.id,
        step_key=getattr(step, "key", None) or "",
        document_id=document_id,
        target_locale=target_locale or "",
        claim_token=claim_token,
        contract_version=CONTRACT_VERSION,
        endpoint_host=endpoint_host or "",
        status=status,
        segment_count=int(segment_count or 0),
        word_count=int(word_count or 0),
        estimate=est,
        estimate_cost_usd=float(est.get("total_cost") or 0.0),
        estimate_currency=str(est.get("currency") or "USD"),
        expires_at=expires_at,
        org_id=org_id,
        customer_id=customer_id,
        attempt_count=int(attempt_count or 0),
        next_attempt_at=next_attempt_at,
        last_error=last_error or "",
        retry_contract=retry_contract,
        requires_quote=bool(requires_quote),
        rework_round=int(rework_round or 0),
        rework_of=rework_of,
        rework_feedback=rework_feedback,
        # F2: snapshot the protocol on the leg (default: the vendor's protocol NOW).
        protocol=(protocol if protocol is not None else vendor_protocol(vendor)),
    )

    session.add(d)
    session.commit()
    session.refresh(d)
    return d


_FROZEN_PACKAGE_KEEP_STATUSES = frozenset(
    {
        VendorDispatchStatus.DISPATCHED,
        VendorDispatchStatus.QUOTED,
        VendorDispatchStatus.ESCALATED,
    }
)


#: The WORKABLE leg states through which a WIRE-LESS (`manual`) leg's frozen operator-delivery
#: package is KEPT: the operator must still be able to download the correct dispatch-time file
#: while the leg is DISPATCHED / QUOTED / ESCALATED (a return is REFUSED on ESCALATED, but the
#: delivery file must never silently regress to the empty mid-run rebuild). EVERY OTHER state
#: -- RETURNED / REJECTED / CANCELLED / DEAD_LETTER -- has NO pending delivery, so the package
#: is CLEARED there. Single source of truth for the frozen-package lifecycle; see
#: _clear_or_keep_frozen.
def _leg_is_wireless(leg) -> bool:
    """True when the leg was DISPATCHED under a WIRE-LESS (file-exchange, e.g. `manual`)
    transport, read from the leg's PROTOCOL SNAPSHOT (`leg_protocol`) -- NEVER the live vendor,
    so a vendor flipped native<->manual while the leg is parked can never retroactively change
    how the already-dispatched leg's frozen package is treated."""
    return bool(getattr(transport_for_protocol(leg_protocol(leg)), "wireless", False))


def leg_is_wireless(leg) -> bool:
    """Public seam: True when a dispatch LEG was recorded under a WIRE-LESS (file-exchange,
    e.g. `manual`) transport, keyed off the leg's PROTOCOL SNAPSHOT (never the live vendor).
    The web/run/document surfaces use this to decide whether to offer the operator the
    download-XLIFF + upload-return hand-off controls (a wire-less leg's return really DOES
    arrive by hand, so those pages are exactly where the operator should act)."""
    return _leg_is_wireless(leg)
