"""ConnectorTranslateFlow engine — the shared T2 connector translate pipeline.

FABLE-ARCH-REVIEW-2026-07-07 Finding 1: `store.py`'s four sibling T2 flows
(`zendesk_translate`, `document360_translate`, `freshdesk_translate`,
`zoho_translate`) were near-copies of ONE implicit pipeline, and the audit record
proved that duplication was the system's main defect generator — every
cross-cutting guard (WriteBudget ceiling, egress guard, refusal/NEEDS_REVIEW
exclusion, hash-merge concurrency discipline, the FINDING-1 watermark clamp) had
to be ported site-by-site, and some never made it to every site.

`run_translate_flow(spec, ...)` OWNS the invariant stages so they exist once, and
a per-connector `TranslateFlowSpec` (a dataclass of plain callables + config)
supplies only the vendor-specific hooks. The four flows become thin wrappers that
build their spec and call the engine; their public `(ok, msg, dict)` signatures,
message wording, result-dict keys, and persisted config state are byte-identical
to the hand-written flows they replace (see tests/verify_flow_engine_parity.py).

Stage map (numbers = SPEC-flow-engine.md's 11-stage checklist):
  1  connection resolve + kind check; build_fresh; effective_locales
  2  egress guard at job-start (assert_egress_allowed) w/ ERROR + SyncLog
  3  discovery (+ optional incremental/watermark union w/ the CARDINAL RULE gate)
  4  locale partition: enabled preflight, rejected/usable split, all-rejected FAIL
  5  _ensure_project + _add_missing_locales (canonical rows, wire-form segments)
  6  content-hash suppression w/ snapshot-read + delta-merge (patch_config_merge)
  7  WriteBudget trim w/ n_wanted bookkeeping; trimmed = incomplete, never settled
  8  materialize doc + segments; pretranslate; DeadEngineError aborts loud
  9  write-back per (record, locale): NEEDS_REVIEW excluded; broad-except -> per-pair
 10  watermark settle w/ FINDING-1 clamp (min(max_edited, oldest_incomplete - 1))
 11  ok-semantics, message, _set_status, _log w/ provenance-actor fold, result dict

NON-goals (per the spec): Confluence/Airtable/Jira/dynamic flows are NOT migrated;
no behavior/message changes; no store.py package split.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app import config
from app.db import engine
from app.filters.base import InputLimitError
from app.models import ConnStatus, Segment, SegStatus
from app.services import providers, seed_caps, translate
from app.services.connectors import egress_guard, harvest, run_ctx

log = logging.getLogger("loc_tms.connectors")


# --- per-article carrier the spec.prepare hook fills in ----------------------
@dataclass
class ArticlePrep:
    """What spec.prepare returns for an article that WILL be processed. The engine
    reads only the first six fields (uniform materialize + hash key); `extra` holds
    whatever flow-private state that connector's build_payload / on_write_ok needs
    (shielded body, merge-code tables, editor type, body_key, source bytes, ...)."""

    aid: str
    chash: str
    title_text: str  # Segment order-0 source text
    body_units: list  # units with .text and .p_index (order 1+)
    extra_skeleton: dict  # persisted Document skeleton extras
    doc_title: str  # Document.filename
    extra: dict = field(default_factory=dict)
    # Context-harvest extras (SPEC-context-metadata Phase 3A) a prepare hook wants
    # mapped onto Document.context but NOT persisted in the skeleton — e.g. Zendesk
    # label NAMES. Kept OFF extra_skeleton so the persisted skeleton shape stays
    # byte-identical to pre-harvest rows (flow-parity contract). Empty for a
    # connector whose only harvestable fields (title/editor) already live in
    # extra_skeleton; harvest.build_flow reads title/editor from there directly.
    harvest: dict = field(default_factory=dict)


@dataclass
class SkipArticle:
    """spec.prepare returns this to skip an article WITHOUT a write (empty body,
    unpublished master, ...). `counter` names a spec.extra_counters entry to bump;
    `error` (optional) is appended to the run's errors list verbatim."""

    counter: str
    error: str | None = None


# --- the per-connector hook spec ---------------------------------------------
@dataclass
class TranslateFlowSpec:
    """Plain callables + config for ONE connector kind. No class hierarchy (spec's
    instruction): a flow that needs a hook no sibling needs adds a field here."""

    kind: str
    no_conn_msg: str
    hash_key: str
    probe_label: str  # "Zendesk" etc. (probe warn text)

    # stage 1/2/3/4
    preflight: Callable  # (ctx) -> (enabled:set, extra:dict)
    prepare: Callable  # (ctx, art) -> ArticlePrep | SkipArticle
    is_suppressed: Callable  # (connector, aid, loc) -> bool
    build_payload: Callable  # (prep, loc, seg0_target|None, mapping) -> dict
    compute_draft: Callable  # (cfg) -> bool
    # stage 11
    compute_ok: Callable  # (ctx) -> bool
    build_message: Callable  # (ctx) -> str
    build_result: Callable  # (ctx) -> dict (budget.summary merged by engine)

    # optional
    extra_counters: tuple = ()  # e.g. ("empty_skipped", "unpublished_skipped")
    partition: Callable | None = None  # (locales, enabled, extra) -> (rejected, usable)
    egress_raw_effective: bool = False  # freshdesk: egress on raw effective_locales
    watermark_key: str | None = None
    detect_change: Callable | None = None  # (connector, watermark) -> list[article]
    scope_ok: Callable | None = None  # (cfg, article) -> bool
    article_epoch: Callable | None = None  # (article) -> int
    on_write_ok: Callable | None = None  # (ctx, prep, loc) -> None (d360 publish)
    post_article: Callable | None = None  # (ctx, art) -> None (zendesk labels)
    # #640: set to the connector's held-back marker prefix (e.g. "held-back:") to make a
    # (record, locale) pair with ANY unit held back (NEEDS_REVIEW / empty target) refuse to
    # CREATE a new locale variant (a variant made with a field missing stays that way: the
    # hash would suppress every retry) and to report each held pair as a failure row that
    # starts with this prefix (the connector's own `_held_back(ctx)` counter reads it).
    # None = the connector's payload already seeds the held spot from the source, so a
    # create is safe and only the hash rule below applies.
    held_prefix: str | None = None
    # False for a connector that already reports a partial UPDATE itself (contentful,
    # shopify, hubspot, akeneo `incomplete`): the engine then only withholds the hash.
    report_held_updates: bool = True
    # (connector, aid, loc) -> bool: "does the locale variant exist", for the create gate
    # above when strategy.exists_target means something else (hubspot: tracked paths;
    # shopify: "every tracked key still covered").
    variant_exists: Callable | None = None
    # (connector, aid, loc) -> bool: the write succeeded but left something out that the
    # engine cannot see (a Contentful field held back by a CMA validation, #665). True
    # withholds the content hash so the next run retries the pair, like a held unit.
    write_incomplete: Callable | None = None
    # (prep, loc, mapping) -> list[str]: extra held unit names the engine cannot see from
    # the segment statuses alone (Contentful Rich Text whose placeholders did not round-trip
    # is kept as the stored value). They join `held_names`, so the #640 rules apply to them.
    extra_held: Callable | None = None
    # (connector, aid, loc, chash) -> bool: the pair is PARKED, a deterministic failure
    # (Contentful CMA validation) already recorded for exactly this source hash. A scheduled
    # run skips it without materializing or translating (no paid-MT loop), but it is NOT
    # done: no content hash is recorded for it, and a source edit or an explicit retry
    # re-attempts it (#665, 2026-10-10: nothing is marked done until every field is
    # written). Counted in ctx.counters["parked_skipped"] (declare it in extra_counters).
    parked: Callable | None = None
    # (connector, article_ids) -> list[article]: replaces the default
    # `connector.discover_articles(article_ids=...)` call when a flow needs run-level
    # narrowing the engine does not carry (Confluence: a caller space key or CQL, always
    # intersected with the connection's own scope inside the connector).
    discover: Callable | None = None
    # #666: True when the hash map may hold legacy keys that differ from "<aid>:<loc>" only
    # in case (Confluence lower-cases them into the pair on read); dropping a pair's stale
    # hash then drops those aliases too, or the next run would read the old hash back.
    hash_drop_casefold: bool = False


# --- run context (mutable accumulator threaded through the stages) -----------
@dataclass
class FlowCtx:
    cid: int
    conn: object
    cfg: dict
    connector: object
    actor: str
    source: str
    locales: list  # lowered effective locales
    enabled: set = field(default_factory=set)
    extra: dict = field(default_factory=dict)  # preflight extras (e.g. primary)
    rejected: list = field(default_factory=list)
    usable: list = field(default_factory=list)
    articles: list = field(default_factory=list)
    hashes: dict = field(default_factory=dict)
    hash_delta: dict = field(default_factory=dict)
    watermark: int = 0
    max_edited: int = 0
    oldest_incomplete: int | None = None
    draft: bool = True
    budget: object = None
    created: int = 0
    updated: int = 0
    skipped: int = 0
    counters: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    dropped_meta: list = field(default_factory=list)  # #579: scalar fields of dropped records
    # #640: (record, locale) pairs written while some unit was held back (NEEDS_REVIEW).
    # Their content hash is NOT recorded, so the next run retries them after approval.
    partial_held: list = field(default_factory=list)
    # #640: pairs NOT created because a unit was held (no held_prefix connectors; the others
    # report the same pairs as failure rows with their own prefix).
    held_create: list = field(default_factory=list)
    # #666: "<aid>:<loc>" keys written INCOMPLETELY this run (a unit held, or the write left
    # something out). Their OLDER stored hash describes content the target no longer holds
    # as a whole, so it is removed on persist: otherwise a later source revert to exactly
    # that older text would hash-match and treat the mixed target as done.
    hash_drop: set = field(default_factory=set)
    # Project automation: records another project owns on a shared connection. They are
    # skipped (never translated or written) and reported on the run (run_ctx.claim).
    claim_conflicts: list = field(default_factory=list)
    # True when the run could not look at every listed record (a record unreadable, refused
    # for size, dropped by the fetch, or skipped with an error / a halt). A project that holds
    # claims must then not read "not reached" as "left the scope" (project automation).
    listing_incomplete: bool = False


def _drain_fetch_notes(ctx) -> None:
    """#579: move the reasons the connector refused a record while FETCHING (a record over
    the per-record size limit, a page tree over its budget) onto the run, so the operator
    sees them in errors and failures like any other per-record refusal."""
    notes = getattr(ctx.connector, "fetch_notes", None)
    while notes:
        n = notes.pop(0)
        rid = str(n.get("id") or "")
        ctx.errors.append(f"{rid or 'record'}: {n['error']}")
        ctx.failures.append({"article_id": rid, "locale": "", "error": n["error"]})
        ctx.dropped_meta.append(n.get("meta") or {})


def run_translate_flow(
    spec: TranslateFlowSpec,
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Drive the invariant T2 translate pipeline for `spec`. Signature mirrors the
    four flows it replaces exactly (callers unchanged)."""
    # Lazy import: store imports THIS module, so importing store at file scope would
    # be circular. store is fully loaded before any flow runs, so this is safe.
    from app.services.connectors import store

    # -- stage 1: resolve + kind check + build_fresh + effective_locales ----------
    conn = store.get(cid)
    if not conn or conn.kind != spec.kind:
        return False, spec.no_conn_msg, {}
    # ffadmin Wave 5 -- the 9th ingest entrypoint. The 13 generic-article kinds (zendesk/
    # document360/freshdesk/zoho/intercom/salesforce/notion/helpscout/strapi/contentful/
    # shopify/hubspot/akeneo) route their <kind>_translate through HERE, not through their
    # own top-level guard, so the disabled-kind refusal must sit at THIS seam too -- byte-
    # parity with _refuse_archived_sync directly below. Without it, build_fresh() at the
    # next line raises an UNCAUGHT ConnectorError (no try here), which jobs.py logs as a
    # crash traceback + FAILs the job -- instead of the clean (False, "...disabled...")
    # contract the 4 flow-less kinds already return. No egress + no ERROR stamping either
    # way (build_fresh raises above _set_status), but this restores the readable-refusal
    # parity the archived gate establishes.
    _dis = store._refuse_disabled_sync(cid, actor=actor)
    if _dis is not None:
        return False, _dis, {}
    # Archive gate (residual sweep M6 / re-review R1): a DIRECT <kind>_translate call (CLI /
    # api) that bypassed translate_now still refuses to spend MT / write into an archived
    # project. Every flow connector funnels through here, so this one guard covers them all.
    _arch = store._refuse_archived_sync(cid, actor=actor)
    if _arch is not None:
        return True, _arch, {"created": 0, "updated": 0, "skipped_archived": True}
    cfg = conn.config or {}
    connector = store.build_fresh(cid, transport)
    raw_effective = store.effective_locales(conn, target_locales)
    locales = [l.strip().lower() for l in raw_effective]  # noqa: E741

    # -- stage 2: egress guard at job-start --------------------------------------
    egress_arg = raw_effective if spec.egress_raw_effective else locales
    try:
        egress_guard.assert_egress_allowed(conn, transport, egress_arg)
    except egress_guard.MockEgressBlocked as e:
        msg = str(e)
        store._set_status(cid, ConnStatus.ERROR, msg, tested=True)
        store._log(cid, "translate", False, 0, msg, actor)
        return False, msg, {}

    ctx = FlowCtx(
        cid=cid,
        conn=conn,
        cfg=cfg,
        connector=connector,
        actor=actor,
        source=source,
        locales=locales,
    )
    reset_budget = getattr(connector, "_reset_fetch_budget", None)
    if callable(reset_budget):
        reset_budget()  # #587: this run's own character count
    ctx.draft = spec.compute_draft(cfg)
    ctx.counters = dict.fromkeys(spec.extra_counters, 0)

    # -- stage 3: discovery (+ optional incremental/watermark union) -------------
    try:
        ctx.enabled, ctx.extra = spec.preflight(ctx)
        articles = (
            spec.discover(connector, article_ids)
            if spec.discover is not None
            else connector.discover_articles(article_ids=article_ids)
        )
        # Incremental-export backstop (detect_change layer 2): only for a flow that
        # DECLARES a watermark key. Others never enter this block, so the engine can
        # NOT accidentally introduce watermark/incremental state for them.
        if spec.watermark_key and article_ids is None:
            prior_watermark = int((conn.config or {}).get(spec.watermark_key) or 0)
            if prior_watermark > 0 and spec.detect_change is not None:
                inc_arts = spec.detect_change(connector, prior_watermark)
                have = {str(a.get("id")) for a in articles}
                for a in inc_arts:
                    if str(a.get("id")) in have:
                        continue
                    # CARDINAL RULE: gate each candidate on edited_at > watermark (the
                    # export endpoint fires on metadata/updated_at-only changes too,
                    # which must NOT re-enqueue).
                    if spec.article_epoch is not None and spec.article_epoch(a) <= prior_watermark:
                        continue
                    if spec.scope_ok is not None and not spec.scope_ok(cfg, a):
                        continue
                    articles.append(a)
                    have.add(str(a.get("id")))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        store._set_status(cid, ConnStatus.ERROR, msg, tested=True)
        store._log(cid, "translate", False, 0, msg, actor)
        return False, msg, {}
    # #579: the fetch itself is bounded (connectors.base.fetch_cap), so a list longer than
    # MAX_DOCUMENT_SEGMENTS proves the source is over the limit. Refuse the whole run here,
    # before any project row, Document or engine call, and say why on the connection and
    # in the run log. Records the fetch refused for size are reported on the run too.
    _drain_fetch_notes(ctx)
    if len(articles) > config.MAX_DOCUMENT_SEGMENTS:
        msg = (
            f"This run found more than {config.MAX_DOCUMENT_SEGMENTS} records, over the "
            "limit of MAX_DOCUMENT_SEGMENTS. Fetching stopped there. Nothing was stored and "
            "no translation was started. Lower the connection's record limit or narrow its "
            "scope."
        )
        store._set_status(cid, ConnStatus.ERROR, msg, tested=True)
        store._log(cid, "translate", False, 0, msg, actor)
        return False, msg, {}
    if not articles:
        if ctx.failures:
            msg = ctx.errors[0]
            store._set_status(cid, ConnStatus.ERROR, msg, tested=True)
            store._log(cid, "translate", False, 0, msg, actor)
            return False, msg, {"failures": ctx.failures}
        store._log(cid, "translate", True, 0, "No articles matched.", actor)
        return True, "No articles matched.", {"created": 0, "updated": 0}
    ctx.articles = articles

    # -- stage 4: locale partition (all-rejected = loud FAIL, checked in compute_ok) --
    if spec.partition is not None:
        ctx.rejected, ctx.usable = spec.partition(locales, ctx.enabled, ctx.extra)
    else:
        ctx.rejected = [l for l in locales if l not in ctx.enabled]  # noqa: E741
        ctx.usable = [l for l in locales if l in ctx.enabled]  # noqa: E741

    # -- stage 5: ensure project + register canonical TargetLocale rows ----------
    with Session(engine) as s:
        proj = store._ensure_project(s, conn)
        store._add_missing_locales(s, proj.id, [store._canonical_locale(l) for l in ctx.usable])  # noqa: E741
        s.commit()
    conn = store.get(cid)  # re-read: the commit expired the detached instance
    ctx.conn = conn

    # -- stage 6/7/10 state: snapshot hashes (+ watermark); this run's delta only --
    ctx.hashes = dict((conn.config or {}).get(spec.hash_key) or {})
    if spec.watermark_key:
        ctx.watermark = int((conn.config or {}).get(spec.watermark_key) or 0)
        ctx.max_edited = ctx.watermark
    ctx.budget = store.WriteBudget()
    claimer = run_ctx.claim()
    if claimer is None:
        from app.services import project_automation as _pa

        claimer = _pa.implicit_claimer(conn)

    def _mark_incomplete(epoch: int) -> None:
        ctx.oldest_incomplete = (
            epoch if ctx.oldest_incomplete is None else min(ctx.oldest_incomplete, epoch)
        )

    def _hold_dropped() -> None:
        # #579 review: a record the fetch dropped for size is held behind the watermark, so
        # the next run reports it again instead of the watermark moving past it for good.
        while ctx.dropped_meta:
            ctx.listing_incomplete = True
            meta = ctx.dropped_meta.pop(0)
            if spec.watermark_key and spec.article_epoch:
                try:
                    ep = spec.article_epoch(meta)
                except Exception:
                    ep = 0
                if ep:
                    _mark_incomplete(ep)

    _hold_dropped()

    # -- the per-article loop ----------------------------------------------------
    # Issue #459: an abort mid-run (DeadEngineError return, or any uncaught exception from
    # prepare / pretranslate / on_write_ok) used to skip the merge-persist below, losing
    # the hashes of articles ALREADY written to the external system: the next run then
    # re-materialized, re-machine-translated (paid) and re-wrote them. The finally block
    # saves this run's own delta (hashes only; the watermark is NOT advanced on an abort).
    loop_completed = False
    try:
        for art in articles:
            try:
                prep = spec.prepare(ctx, art)
                _drain_fetch_notes(ctx)
                _hold_dropped()
            except InputLimitError as limit_exc:
                # #576: a record whose body parse already tripped a #571 cap (the HTML
                # filter stops at the cap). Recorded on the run like any other refusal;
                # the rest of the run carries on and the record is held behind the
                # watermark so it is reported again next run, never silently dropped.
                guess = str(art.get("id", "")) if isinstance(art, dict) else ""
                ctx.errors.append(f"{guess or 'record'}: {limit_exc}")
                ctx.failures.append({"article_id": guess, "locale": "", "error": str(limit_exc)})
                ctx.listing_incomplete = True
                if spec.watermark_key and spec.article_epoch:
                    _mark_incomplete(spec.article_epoch(art))
                continue
            if isinstance(prep, SkipArticle):
                ctx.counters[prep.counter] += 1
                if prep.error:
                    ctx.errors.append(prep.error)
                if prep.error or prep.counter in ("halted_skipped", "unreadable_skipped"):
                    ctx.listing_incomplete = True
                continue
            aid = prep.aid
            chash = prep.chash
            edited_epoch = (
                spec.article_epoch(art) if (spec.watermark_key and spec.article_epoch) else 0
            )
            art_failed = False

            # A FULL pull walks a scope larger than the write ceiling over several runs: an
            # item finished earlier in this full cycle is passed over (project automation).
            if run_ctx.full_pull() and run_ctx.full_done_has(aid):
                ctx.skipped += 1
                continue

            # stage 6: which locales need work (content-hash suppression + only_pairs).
            # only_pairs is set ONLY by store.retry_failed — an EXPLICIT operator retry of
            # exactly these (article, locale) pairs. A recorded hash suppresses SCHEDULED /
            # full runs — including AFTER a PARTIAL/held write (the hash is recorded so there
            # is no MT re-spend loop) — but an operator retry is a direct instruction to
            # re-attempt, so those pairs re-materialize and re-attempt REGARDLESS of a
            # hash-match. This framework-level bypass is the ONE place the rule lives; it
            # replaces the per-connector stored-hash neutralisation store.retry_failed used
            # to run for contentful/shopify/hubspot. Scheduled/full runs are UNTOUCHED — no
            # suppression change outside an explicit retry.
            retry = only_pairs is not None
            pending = []
            for loc in ctx.usable:
                if retry and (aid, loc) not in only_pairs:
                    continue
                # Project automation (one connection feeding many projects): the first project
                # to reach a (record, locale) owns its write-back; a later project skips that
                # pair and the run reports it. Inert (no claim object) unless the connection
                # has claims, so a run of any other connection is untouched.
                if claimer is not None and not claimer.take(aid, loc):
                    ctx.claim_conflicts.append(f"{aid}:{loc}")
                    continue
                if not retry:
                    prior = ctx.hashes.get(f"{aid}:{loc}")
                    if spec.parked is not None and spec.parked(connector, aid, loc, chash):
                        ctx.counters["parked_skipped"] += 1
                        continue
                    # A FULL pull (project automation) ignores the stored hash so the pair is
                    # translated and written again; everything else (parking, the review
                    # gate, the write ceiling) still applies.
                    if prior == chash and not run_ctx.full_pull():
                        try:
                            if spec.is_suppressed(connector, aid, loc):
                                ctx.skipped += 1
                                continue
                        except Exception as probe_exc:
                            log.warning(
                                "%s existence probe failed for %s/%s (%s); writing anyway.",
                                spec.probe_label,
                                aid,
                                loc,
                                type(probe_exc).__name__,
                            )
                pending.append(loc)
            # stage 7: trim to the remaining write budget (build-time cap -> MT + write bounded)
            n_wanted = len(pending)
            if pending:
                pending = pending[: ctx.budget.trim(n_wanted)]
            if not pending:
                if spec.watermark_key:
                    if ctx.budget.ceiling_hit and n_wanted:
                        _mark_incomplete(edited_epoch)  # cut off before any locale built
                        continue
                    ctx.max_edited = max(ctx.max_edited, edited_epoch)  # settled (nothing to do)
                continue

            # stage 7b (#576): the #571 caps apply to a connector record too. A record
            # that would break MAX_DOCUMENT_SEGMENTS, MAX_DOCUMENT_ROWS or
            # MAX_SEGMENT_CHARS stops HERE, before any row is written and before any
            # engine call (no money is spent on rows we would refuse). The refusal is
            # recorded on the run and as a per-locale failure so an operator retry
            # reports it again; the rest of the run carries on.
            cap_err = seed_caps.seed_error(
                1 + len(prep.body_units),
                len(pending),
                what=f"Record {aid}",
                texts=[prep.title_text] + [u.text for u in prep.body_units],
            )
            if cap_err:
                art_failed = True
                ctx.errors.append(f"{aid}: {cap_err}")
                for loc in pending:
                    ctx.failures.append({"article_id": aid, "locale": loc, "error": cap_err})
                if spec.watermark_key:
                    _mark_incomplete(edited_epoch)
                continue

            # stage 8: materialize doc + segments, pretranslate per locale
            doc_id = _materialize(store, ctx, prep, pending)
            try:
                with Session(engine) as s:
                    for loc in pending:
                        translate.pretranslate_document(s, doc_id, loc)
            except providers.DeadEngineError as e:
                # A dead engine:<id> ref affects EVERY article/locale equally — abort the
                # whole run loud rather than writing mock output to the tenant.
                msg = str(e)
                store._set_status(cid, ConnStatus.ERROR, msg, synced=False)
                store._log(cid, "translate", False, 0, msg, actor)
                return False, msg, {"created": ctx.created, "updated": ctx.updated}
            with Session(engine) as s:
                seg_by = {
                    (sg.target_locale, sg.order_index): sg
                    for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
                }

            # stage 9: write-back per (record, locale)
            wrote_any = False
            for loc in pending:
                tseg = seg_by.get((loc, 0))
                # TITLE gap fix (SPEC-refusal-choke-point Deliverable 5, chip task_e782500f):
                # the title (segment order 0) must get the SAME NEEDS_REVIEW exclusion the
                # body units get below — without it a review-routed title (a refusal-flagged
                # hedge, or any NEEDS_REVIEW-with-text unit) reaches the wire while the body
                # is held back. Excluding it yields seg0_target=None, and EVERY connector's
                # build_payload already falls back to the SOURCE title on None (the same
                # source-fallback the excluded BODY units get via html.build's untranslated
                # skeleton), so a hedge is never posted; the source title (a real title) is.
                seg0_target = (
                    tseg.target_text
                    if (
                        tseg
                        and tseg.status != SegStatus.NEEDS_REVIEW
                        and (tseg.target_text or "").strip()
                    )
                    else None
                )
                mapping = {}
                for i, u in enumerate(prep.body_units):
                    sg = seg_by.get((loc, 1 + i))
                    # Defense-in-depth (QE gate, qe.py): NEEDS_REVIEW segments are excluded
                    # from write-back (refusal/QE perimeter — see push_connection's guard).
                    if (
                        sg
                        and sg.status != SegStatus.NEEDS_REVIEW
                        and (sg.target_text or "").strip()
                    ):
                        mapping[str(u.p_index)] = sg.target_text
                # #640: units of this pair that have no usable translation (NEEDS_REVIEW,
                # refusal-held or empty target). A source-empty unit is not "held".
                held_names = []
                if seg0_target is None and (prep.title_text or "").strip():
                    held_names.append("title")
                for u in prep.body_units:
                    if str(u.p_index) not in mapping and (u.text or "").strip():
                        held_names.append(str(u.p_index))
                if spec.extra_held is not None:
                    for name in spec.extra_held(prep, loc, mapping):
                        if name not in held_names:
                            held_names.append(name)
                # An ALL-held pair (nothing usable) goes on to the write: every connector
                # already turns that into a no-write held-back / per-pair failure of its own.
                has_usable = seg0_target is not None or bool(mapping)
                if held_names and has_usable:
                    # Never CREATE a locale variant while a unit is held: a field-mapped
                    # create leaves it empty, a doc-per-locale / article create fills it with
                    # the SOURCE text (a mixed-language page), and the hash would hide both
                    # for good. An existing variant is still updated (below). Fail closed
                    # when the existence probe itself fails.
                    try:
                        probe = spec.variant_exists or connector.strategy.exists_target
                        variant_exists = bool(probe(connector, aid, loc))
                    except Exception:
                        variant_exists = False
                    if not variant_exists:
                        art_failed = True
                        if spec.held_prefix:
                            ctx.failures.append(
                                {
                                    "article_id": aid,
                                    "locale": loc,
                                    "error": f"{spec.held_prefix} {len(held_names)} unit(s) "
                                    f"awaiting review (NEEDS_REVIEW): {', '.join(held_names)}; "
                                    f"the variant was not created and will be retried next run",
                                }
                            )
                        else:
                            ctx.held_create.append({"article_id": aid, "locale": loc})
                        continue
                payload = spec.build_payload(prep, loc, seg0_target, mapping)
                # Broad except ON PURPOSE: the strategy's exists probe / PUT can raise
                # ConnectorError or httpx.TransportError — this must land as a per-pair
                # failure (like a WriteResult failure), never crash the run (freshdesk
                # parity finding). Failures feed retry_failed.
                try:
                    res = connector.strategy.write_target(
                        connector, aid, loc, payload, draft=ctx.draft
                    )
                except Exception as write_exc:
                    msg = f"{type(write_exc).__name__}: {write_exc}"
                    art_failed = True
                    ctx.errors.append(f"{aid}/{loc}: {msg}")
                    ctx.failures.append({"article_id": aid, "locale": loc, "error": msg})
                    continue
                if res.ok:
                    wrote_any = True
                    if res.created:
                        ctx.created += 1
                    else:
                        ctx.updated += 1
                    write_held = bool(
                        spec.write_incomplete is not None
                        and spec.write_incomplete(connector, aid, loc)
                    )
                    if held_names or write_held:
                        # #640 / #665: written, but not complete. No hash, so the next run retries
                        # the pair once the held unit(s) are approved.
                        art_failed = True
                        ctx.partial_held.append({"article_id": aid, "locale": loc})
                        # #666: the pair's OLDER stored hash no longer describes the target
                        # (some fields are new, some old): drop it, or a later revert of the
                        # source to exactly that older text would hash-match as done.
                        # Review gate: a stored hash EQUAL to this source's hash is kept. It was
                        # recorded by a COMPLETE write of this same source (a partial write
                        # since would have dropped it), so the held fields still carry that
                        # write's text and the target as a whole matches. Dropping it would
                        # re-translate the whole pair at full MT cost on every scheduled run
                        # while the unit stays held (an explicit retry of an unchanged pair).
                        pkey = f"{aid}:{loc}"
                        if ctx.hashes.get(pkey) != chash:
                            ctx.hash_drop.add(pkey)
                            ctx.hashes.pop(pkey, None)
                            ctx.hash_delta.pop(pkey, None)
                        if spec.held_prefix and spec.report_held_updates:
                            ctx.failures.append(
                                {
                                    "article_id": aid,
                                    "locale": loc,
                                    "error": f"{spec.held_prefix} {len(held_names)} field(s) "
                                    f"awaiting review (NEEDS_REVIEW): {', '.join(held_names)}; "
                                    f"the other fields were written, the held ones kept their "
                                    f"stored value, and the pair will be retried next run",
                                }
                            )
                    else:
                        ctx.hashes[f"{aid}:{loc}"] = chash  # remember for suppression next run
                        ctx.hash_delta[f"{aid}:{loc}"] = chash  # this run's OWN delta
                    if spec.on_write_ok is not None:
                        spec.on_write_ok(ctx, prep, loc)
                else:
                    art_failed = True
                    ctx.errors.append(f"{aid}/{loc}: {res.message}")
                    ctx.failures.append({"article_id": aid, "locale": loc, "error": res.message})

            if not art_failed and not (ctx.budget.ceiling_hit and len(pending) < n_wanted):
                run_ctx.mark_done(aid)  # full-pull cursor: every pair of this item is written
            # stage 10: watermark settle accounting (only when a watermark is declared)
            if spec.watermark_key:
                # A failed OR partially-ceiling-trimmed article is NOT fully done, so it is
                # recorded as a watermark HOLE (FINDING 1) — a later fully-settled higher-
                # edited article's scalar max can't leapfrog it.
                partially_trimmed = ctx.budget.ceiling_hit and len(pending) < n_wanted
                if art_failed or partially_trimmed:
                    _mark_incomplete(edited_epoch)
                else:
                    ctx.max_edited = max(ctx.max_edited, edited_epoch)

            if wrote_any and spec.post_article is not None:
                spec.post_article(ctx, art)  # zendesk anti-loop labels
        loop_completed = True
    finally:
        if not loop_completed and (ctx.hash_delta or ctx.hash_drop):
            try:
                store.patch_config_merge(
                    cid,
                    spec.hash_key,
                    ctx.hash_delta,
                    hashes_remove=sorted(ctx.hash_drop),
                    remove_casefold=spec.hash_drop_casefold,
                )
            except Exception:
                log.warning(
                    "connection %s: could not persist hashes after an aborted run",
                    cid,
                    exc_info=True,
                )

    # -- stage 10 clamp + stage 6 merge-persist ----------------------------------
    if spec.watermark_key:
        # FINDING-1 clamp: the watermark may only advance to just BELOW the oldest
        # article that did NOT fully settle this run (edited_at > wm is the re-surface
        # gate, so wm = oldest_incomplete - 1 keeps that exact article eligible).
        if ctx.oldest_incomplete is not None:
            ctx.max_edited = min(ctx.max_edited, ctx.oldest_incomplete - 1)
        store.patch_config_merge(
            cid,
            spec.hash_key,
            ctx.hash_delta,
            watermark_key=spec.watermark_key,
            watermark_at_least=ctx.max_edited,
            hashes_remove=sorted(ctx.hash_drop),
            remove_casefold=spec.hash_drop_casefold,
        )
    else:
        store.patch_config_merge(
            cid,
            spec.hash_key,
            ctx.hash_delta,
            hashes_remove=sorted(ctx.hash_drop),
            remove_casefold=spec.hash_drop_casefold,
        )

    # -- stage 11: ok-semantics, message, status, SyncLog (provenance fold), result --
    ok = spec.compute_ok(ctx)
    msg = spec.build_message(ctx)
    if ctx.held_create:
        msg += (
            f" Held back {len(ctx.held_create)} (record, locale) pair(s) awaiting review "
            f"(NEEDS_REVIEW): a new locale variant is not created while a unit is held. "
            f"They will be re-attempted next run."
        )
    if ctx.partial_held and not (spec.held_prefix and spec.report_held_updates):
        msg += (
            f" {len(ctx.partial_held)} pair(s) written with unit(s) held for review "
            f"(NEEDS_REVIEW); they will be re-attempted next run."
        )
    store._set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, msg, synced=ok)
    # Provenance (source=manual/webhook/scheduled/...): folded into the SyncLog actor
    # rather than dropped — SyncLog has no dedicated source column. Manual (the
    # default) reads exactly as `actor` does today.
    log_actor = actor if source in ("manual", "", None) else f"{actor} (via {source})"
    store._log(cid, "translate", ok, ctx.created + ctx.updated, msg, log_actor)
    result = {**spec.build_result(ctx), **ctx.budget.summary()}
    if ctx.partial_held:
        result["partial_held"] = len(ctx.partial_held)
    if ctx.held_create:
        result["held_back"] = result.get("held_back", 0) + len(ctx.held_create)
    if ctx.claim_conflicts:
        result["claim_conflicts"] = len(ctx.claim_conflicts)
        result["claim_conflict_ids"] = ctx.claim_conflicts[:50]
    if claimer is not None and ctx.listing_incomplete:
        result["listing_incomplete"] = True
    return ok, msg, result


def _materialize(store, ctx: FlowCtx, prep: ArticlePrep, pending: list) -> int:
    """Create the connector Document + Segments (title order 0, body units 1+).
    Uniform across every T2 flow — TargetLocale rows in canonical form, Segment rows
    keyed on the connector's own wire-format locales (`pending`)."""
    canon = [store._canonical_locale(l) for l in pending]  # noqa: E741
    # SPEC-context-metadata Phase 3A: harvest this connector's in-hand source
    # metadata (title/labels/editor) into a sanitized Document.context — resolve()
    # injects it into every segment prompt with no per-connector translate change.
    # None (harvest off / nothing harvestable) leaves Document.context None ->
    # prompt byte-identical to pre-harvest.
    context = harvest.build_flow(prep, ctx.cfg, ctx.conn.kind)
    with Session(engine) as s:
        doc = store._new_connector_doc(
            s,
            ctx.conn,
            None,
            prep.doc_title,
            ctx.actor,
            locales=canon,
            extra_skeleton=prep.extra_skeleton,
            context=context,
        )
        doc_id = doc.id  # read once: a commit expires the instance

        def _rows():
            for loc in pending:
                yield Segment(
                    document_id=doc_id,
                    target_locale=loc,
                    order_index=0,
                    source_text=prep.title_text,
                    status=SegStatus.NEW,
                )
                for i, u in enumerate(prep.body_units):
                    yield Segment(
                        document_id=doc_id,
                        target_locale=loc,
                        order_index=1 + i,
                        source_text=u.text,
                        status=SegStatus.NEW,
                    )

        # #576: committed chunks (like #571 ingest), so the write lock is held briefly.
        seed_caps.add_in_chunks(s, _rows())
        return doc_id
