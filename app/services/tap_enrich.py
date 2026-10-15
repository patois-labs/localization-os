"""Prompt Tap RUN ENVELOPES -- Wave B enrichment (PROPOSAL-prompt-tap-run-envelopes §5).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _hash, _iso, _metric_row, _pricing, _call_rows,
_rollup_calls, _engine_mix, _step_detail, _run_segments, _shown_matched_from_capture, _entry_hash,
_glossary_manifest, _style_guide_manifest, _tm_manifest, _llmemory_manifest, _vendor_manifest from
app.services.tap_enrich_part1.

Wave A gave every translate run a TapAction + TapRun envelope and stamped `action_id` /
`run_id` into each captured provider call. Wave B POPULATES the reserved JSON columns
those rows left NULL:

  * TapAction.manifest -- the ASSET MANIFEST (D5): the TM sources, glossaries, style
    guide, and LLMemory context in play for the run, recorded as IDENTITY + CONTENT-HASH
    only (ids, names, version hashes) -- NEVER a full-content snapshot.
  * TapRun.detail -- per-leg enrichment: the raw per-call metric rows (tokens/spend, NO
    prompt/response text), the engine/failover mix, the glossary terms that matched, and
    -- the differentiated part (D6) -- glossary ADHERENCE: for each matched term whether
    the run's LQA pass honored it, violated it, or did not check it.
  * TapAction.totals -- honest roll-ups (segments, calls, tokens, spend, failures) summed
    UPWARD from the raw children, storing the parts they summed so a reader can re-verify.

THE OBSERVER CONTRACT (identical to tap_envelope / prompt_tap.capture, and load-bearing):
  * BEST-EFFORT, NEVER RAISES. enrich() is called from the SAME finalize seams Wave A
    already runs (orchestrator._finalize_run_envelope, translate's direct-MT finally). A
    failure to enrich degrades to "no enrichment" (the reserved columns stay NULL), never
    to a broken translation. Every sub-part is independently guarded: a failure resolving
    the TM manifest never loses the glossary manifest or the totals.
  * VISIBLE FAILURE. A total enrich failure WARNs once per process (the 2026-08-06 outage
    lesson -- a swallowed failure that hides for days is worse than a loud one). It does
    not debug-swallow.
  * ISOLATED SESSION. Every read/write opens its OWN short Session and commits once, so
    enrichment can never move the translation's transaction boundary. enrich() runs AFTER
    the run's own session has committed and AFTER Wave A finalized the envelope (lock
    free).
  * READ-ONLY ON BUSINESS STATE, BOUNDED WORK ON THE EXIT PATH. Enrichment never writes
    segment/TM/glossary/LQA state, and it adds NO work to the per-segment translate HOT
    path. It is NOT free, though: it runs SYNCHRONOUSLY at the run's finalize seam
    (orchestrator._finalize_run_envelope, translate's direct-MT finally -- including under
    GeneratorExit), so it DOES sit on the run's EXIT path. It re-derives the manifest and
    matched terms by REPLAYING the run's own deterministic resolvers (build_glossary_plan /
    match_terms_merged / resolve_style_guide) over the persisted segments, and reads the
    per-action call FILE + the run's StepRun rows. That replay is BOUNDED (M6): the plan is
    built once, match_terms_merged is memoized on source text, and at most
    ENRICH_MATCH_SEGMENT_CAP segments are replayed (beyond that detail.enrichment_truncated
    is set) -- so the exit-path cost is capped to a fraction of a second regardless of
    document size. (Moving enrichment fully async is a future design change, out of scope
    here.)

UNENFORCED-REFERENCE SAFETY (the Wave A review meta-finding, carried forward). Wave A's
defects all traced to ONE root: moving from ENFORCED references to UNENFORCED ones (bare
provenance ints) without adding VALIDATION ON READ. Wave B records MORE unenforced
references -- asset ids + hashes on the manifest. So every id here is recorded as INERT
DATA: nothing in a read path ever joins a manifest id back to a live row, and every
resolver tolerates a dangling / deleted / renamed asset by degrading to fewer facts (the
safe direction), never a crash and never a wrong join. Enrichment runs at finalize over a
snapshot; a later asset delete cannot corrupt an already-written manifest, and re-reading
one never dereferences its ids.

Documented in: docs/architecture/connectors.md
"""

import hashlib
import logging

from app.services.tap_enrich_part1 import (
    _call_rows,
    _engine_mix,
    _entry_hash,
    _formality_manifest,
    _glossary_manifest,
    _hash,
    _iso,
    _llmemory_manifest,
    _metric_row,
    _pricing,
    _rollup_calls,
    _run_segments,
    _shown_matched_from_capture,
    _step_detail,
    _style_guide_manifest,
    _tm_manifest,
    _vendor_manifest,
)

__all__ = [
    "_call_rows",
    "_engine_mix",
    "_entry_hash",
    "_formality_manifest",
    "_glossary_manifest",
    "_hash",
    "_iso",
    "_llmemory_manifest",
    "_metric_row",
    "_pricing",
    "_rollup_calls",
    "_run_segments",
    "_shown_matched_from_capture",
    "_step_detail",
    "_style_guide_manifest",
    "_tm_manifest",
    "_vendor_manifest",
    "hashlib",
]

log = logging.getLogger("loc_tms.tap_enrich")

_WARNED = False

# Enrichment payload shape version. Bumped only on a breaking shape change so a reader /
# exporter can branch. Independent of models.TAP_SCHEMA_VERSION (the row schema).
ENRICH_SCHEMA_VERSION = 1

# Hard bound on the glossary-adherence replay (M6). enrich() runs SYNCHRONOUSLY on the
# run's finalize/exit path; the per-segment match replay is linear in segment count
# (~0.4ms/segment measured), so a huge document would add unbounded latency to that exit.
# Beyond this many segments the replay stops and marks detail.enrichment_truncated rather
# than doing silent unbounded work. Sized to cover ordinary documents in full while
# capping the worst case to a fraction of a second.
ENRICH_MATCH_SEGMENT_CAP = 2000


def _warn_once(msg: str) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        log.warning(msg, exc_info=True)


def _session():
    """A fresh isolated Session on the app engine (mirrors tap_envelope._session)."""
    from sqlmodel import Session

    from app.db import engine

    return Session(engine)


def _call_rows_by_run(action_id):
    """{run_id: [metric_row, ...]} for ALL runs, from ONE pass over the per-action file
    (N5). Under the D1 grain N locales' calls share one file, so re-parsing the whole file
    per run (the round-1 rollup_action_totals defect -- O(N*file), ~20s at 17x5000 calls)
    is wrong; this parses the file ONCE and groups by run_id. Best-effort: {} on failure."""
    from app.services import prompt_tap

    pricing = _pricing()
    out = {}
    seqs = {}
    try:
        path = prompt_tap._action_path(action_id)
        if path is None:
            return out
        for rec in prompt_tap._iter_file(path):
            rid = (rec.get("meta") or {}).get("run_id")
            seq = seqs.get(rid, 0)
            out.setdefault(rid, []).append(_metric_row(rec, seq, pricing))
            seqs[rid] = seq + 1
    except Exception:
        return out
    return out


def _matched_terms(s, project, segments, src, tgt):
    """The glossary terms that matched this leg's segments, and the segment ids each
    matched -- by REPLAYING the run's own resolver (build_glossary_plan +
    match_terms_merged) over the persisted segments. Deterministic, so it recovers the
    run's matches without having captured them on the hot path (§5 'no capture logic in
    business code'). Returns (terms, plan, truncated): terms keyed by (source_term,
    target_term); the plan carries the module glossary_ids AND the include_legacy flag the
    manifest needs; `truncated` is True when the segment cap bit. ([], None, False) when no
    glossary is in scope or it was deleted (dangling-safe).

    BOUNDED + SINGLE-PASS (M6). enrich() runs SYNCHRONOUSLY on the run's finalize/exit
    path, so this replay must not grow unbounded with the document:
      * the plan is built ONCE (not per segment);
      * match_terms_merged is memoized on the segment's source TEXT, so identical source
        strings (common in real docs -- repeated labels, boilerplate) are matched once,
        not once per occurrence;
      * at most ENRICH_MATCH_SEGMENT_CAP segments are replayed; beyond that the replay
        STOPS and `truncated` is returned True (surfaced as detail.enrichment_truncated)
        rather than doing silent unbounded work. Adherence over the capped subset stays
        honest; the marker says the sample is partial."""
    if project is None or not segments:
        return [], None, False
    try:
        from app.services import glossary_module as gm

        plan = gm.build_glossary_plan(s, project, src, tgt)
        by_key = {}
        cache = {}
        truncated = len(segments) > ENRICH_MATCH_SEGMENT_CAP
        for seg in segments[:ENRICH_MATCH_SEGMENT_CAP]:
            text = seg.source_text or ""
            terms = cache.get(text)
            if terms is None:
                terms = gm.match_terms_merged(
                    s, project.id, text, src, tgt, plan, conflict_state=set()
                )
                cache[text] = terms
            for t in terms or []:
                key = (t.get("source_term") or "", t.get("target_term") or "")
                if not key[0]:
                    continue
                rec = by_key.setdefault(
                    key,
                    {
                        "source": key[0],
                        "expected_target": key[1],
                        "dnt": bool(t.get("dnt")),
                        "sense_label": t.get("sense_label"),
                        "segment_ids": [],
                    },
                )
                if seg.id not in rec["segment_ids"]:
                    rec["segment_ids"].append(seg.id)
        return list(by_key.values()), plan, truncated
    except Exception:
        return [], None, False


def _adherence(segments, matched, lqa_step_ran):
    """D6 adherence: per matched term, honored / violated / not_checked.

    THE JOIN (proposal §11.4 -- on a RECORDED run id, not a time window): whether the run
    checked its glossary is decided by `lqa_step_ran`, read from this run's own StepRun
    rows (workflow_run_id). No LQA step in the run -> every matched term is `not_checked`
    (honest: nothing judged it). An LQA step ran -> each term is `violated` when the SAME
    deterministic structural check the LQA adjudicator itself uses
    (quality.glossary_term_violations / dnt_violations, read-only) reports the required
    target missing on any of the term's delivered segments, else `honored`. NO new LLM
    judging -- this reads what LQA's own floor produces.

    Dangling-safe: driven entirely by the run's own segments + the matched terms; a
    deleted glossary yields no matched terms -> empty adherence, never a crash."""
    out = {
        "schema_version": ENRICH_SCHEMA_VERSION,
        "lqa_step_ran": bool(lqa_step_ran),
        "terms": [],
        "honored": 0,
        "violated": 0,
        "not_checked": 0,
        "method": "steprun_lqa_gate+deterministic_glossary_check",
    }
    if not matched:
        return out
    try:
        from app.services import quality
    except Exception:
        quality = None
    seg_by_id = {seg.id: seg for seg in segments}
    for term in matched:
        entry = {
            "source": term["source"],
            "expected_target": term["expected_target"],
            "dnt": term["dnt"],
            "segment_ids": list(term["segment_ids"]),
            "violated_segment_ids": [],
        }
        if not lqa_step_ran or quality is None:
            entry["status"] = "not_checked"
            out["not_checked"] += 1
            out["terms"].append(entry)
            continue
        term_dict = {
            "source_term": term["source"],
            "target_term": term["expected_target"],
            "dnt": term["dnt"],
            "sense_label": term.get("sense_label"),
            "case_sensitive": False,
        }
        violated_ids = []
        for sid in term["segment_ids"]:
            seg = seg_by_id.get(sid)
            if seg is None:
                continue
            srctext = seg.source_text or ""
            tgttext = seg.target_text or ""
            try:
                if term["dnt"]:
                    bad = quality.dnt_violations(srctext, tgttext, [term_dict])
                else:
                    bad = quality.glossary_term_violations(
                        srctext,
                        tgttext,
                        [term_dict],
                        target_locale=getattr(seg, "target_locale", None),
                    )
            except Exception:
                bad = []
            if bad:
                violated_ids.append(sid)
        if violated_ids:
            entry["status"] = "violated"
            entry["violated_segment_ids"] = violated_ids
            out["violated"] += 1
        else:
            entry["status"] = "honored"
            out["honored"] += 1
        out["terms"].append(entry)
    return out


def _build_manifest(s, project, doc, src, tgt, plan, workflow_run_id=None):
    """Assemble the asset manifest for the action. Each asset class is independently
    guarded so one resolver failing never loses the others. `workflow_run_id` (Wave C)
    adds the vendor leg class -- the human-vendor dispatches this run made."""
    manifest = {
        "schema_version": ENRICH_SCHEMA_VERSION,
        "source_locale": src,
        "tm": _tm_manifest(s, project, src, tgt),
        "glossaries": _glossary_manifest(s, project, plan, src, tgt),
        "style_guide": _style_guide_manifest(s, project, src, tgt),
        "llmemory": _llmemory_manifest(s, project, src, tgt),
        "vendor": _vendor_manifest(s, workflow_run_id),
    }
    # #617 item 7: the MT formality register that applied (key present only when one resolved,
    # so a manifest from a run without formality is unchanged).
    _fm = _formality_manifest(s, project, doc, tgt)
    if _fm is not None:
        manifest["formality"] = _fm
    return manifest


# --------------------------------------------------------------------------- #
# Roll-up totals (TapAction.totals) -- summed upward from the runs, parts stored
# --------------------------------------------------------------------------- #
def rollup_action_totals(s, action_id):
    """Sum every TapRun leg of this action into the action totals, STORING the per-run
    parts (by_run) so total == sum(by_run) is verifiable -- no aggregate without its raw
    parts (§4).

    H1: the per-run parts are re-summed from the per-action CALL FILE (via _rollup_calls)
    and the persisted Segment rows -- NOT from a sibling TapRun's `detail` (which a
    concurrent leg may not have written yet: last-writer-wins stale under-count). N5: the
    file is parsed ONCE (`_call_rows_by_run`) and grouped by run_id, not re-parsed per run.
    Called with the CALLER'S session so totals are stamped in the same transaction as the
    action's status.

    HONESTY MARKERS (N8): `spend_source` ("ledger_per_call" iff every counted call carried
    its exact recorded cost, else "estimated_from_usage"); `capture_withheld` (True when the
    disk-budget backstop withheld per-call capture -- the file-derived totals then
    UNDER-COUNT, and this says so); `enrichment_truncated` (True when any leg's adherence
    replay hit the segment cap)."""
    from sqlmodel import select

    from app.models import TapAction, TapRun

    action = s.get(TapAction, action_id)
    # RETENTION HONESTY (soak S1 fix, finding 4b): the per-action CALL FILE can be aged out by
    # prompt_tap's TTL sweep AFTER this action finalized. A later re-roll (a revival/reap re-
    # derive) must NOT recompute file-derived ZEROS over the honest totals captured while the
    # file existed -- that would make a real spend masquerade as exact-ledger-zero (the §4 "an
    # envelope cannot lie" law). If the file is GONE and we already stored real totals
    # (calls>0), preserve them verbatim and add `capture_swept` (distinct from capture_withheld,
    # the disk-budget backstop). The sweep protects NON-terminal envelopes' files, so this only
    # ever fires for a legitimately-aged terminal action; the marker is the honest tell that its
    # raw per-call detail is gone. Best-effort; never raises.
    try:
        from app.services import prompt_tap

        _swept = (
            action is not None
            and prompt_tap.is_action_id(action_id)
            and not prompt_tap._action_path(action_id).exists()
            and isinstance(action.totals, dict)
            and int((action.totals or {}).get("calls", 0) or 0) > 0
        )
    except Exception:
        _swept = False
    if _swept:
        preserved = dict(action.totals)
        preserved["capture_swept"] = True
        return preserved
    doc_id = getattr(action, "document_id", None) if action is not None else None
    rows_by_run = _call_rows_by_run(action_id)  # ONE pass over the file (N5)
    by_run = []
    any_est = False
    any_trunc = False
    runs = s.exec(select(TapRun).where(TapRun.action_id == action_id)).all()
    # Wave C (decision 5): HUMAN spend per leg, keyed by the TapRun's workflow_run_id (a
    # TapRun maps 1:1 to a WorkflowRun, which owns the vendor dispatch legs). AI spend is
    # the call-file rollup (spend_usd); this is the disjoint vendor-post-edit track.
    from app.services import vendor_spend

    _wf_run_ids = [r.workflow_run_id for r in runs if r.workflow_run_id is not None]
    human_by_run = vendor_spend.spend_by_run(s, _wf_run_ids) if _wf_run_ids else {}
    _human_agg = vendor_spend.spend_summary(s, _wf_run_ids) if _wf_run_ids else None
    for r in runs:
        rows = rows_by_run.get(r.id, [])
        roll = _rollup_calls(rows)
        # segments: prefer the leg's own recorded count (its committed detail), else count
        # the persisted Segment rows for (doc, locale) -- the same computation detail uses,
        # so a finishing leg whose detail is not yet written still totals correctly.
        d = r.detail or {}
        if "segments" in d:
            seg_n = int(d.get("segments") or 0)
        else:
            seg_n = len(_run_segments(s, doc_id, r.target_locale)) if doc_id else 0
        if roll.get("spend_source") == "estimated_from_usage":
            any_est = True
        if d.get("enrichment_truncated"):
            any_trunc = True
        _hr = human_by_run.get(r.workflow_run_id, {}) if r.workflow_run_id else {}
        by_run.append(
            {
                "run_id": r.id,
                "target_locale": r.target_locale,
                "calls": int(roll.get("calls", 0) or 0),
                "failures": int(roll.get("failures", 0) or 0),
                "tokens_in": int(roll.get("tokens_in", 0) or 0),
                "tokens_out": int(roll.get("tokens_out", 0) or 0),
                "chars": int(roll.get("chars", 0) or 0),
                "spend_usd": round(float(roll.get("spend_usd", 0.0) or 0.0), 8),
                "spend_source": roll.get("spend_source"),
                "human_spend_usd": round(float(_hr.get("human_spend_usd", 0.0) or 0.0), 6),
                "human_spend_source": _hr.get("human_spend_source", "none"),
                "segments": int(seg_n),
            }
        )
    withheld = bool(getattr(action, "capture_withheld", False)) if action is not None else False
    totals = {
        "schema_version": ENRICH_SCHEMA_VERSION,
        "runs": len(by_run),
        "by_run": by_run,
        "segments": sum(b["segments"] for b in by_run),
        "calls": sum(b["calls"] for b in by_run),
        "failures": sum(b["failures"] for b in by_run),
        "tokens_in": sum(b["tokens_in"] for b in by_run),
        "tokens_out": sum(b["tokens_out"] for b in by_run),
        "chars": sum(b["chars"] for b in by_run),
        "spend_usd": round(sum(b["spend_usd"] for b in by_run), 8),
        # Wave C (decision 5): the disjoint HUMAN (vendor) spend track alongside the AI
        # spend_usd, plus its provenance label (actual/estimate/mixed/none). Aggregated
        # from the same per-run legs summed into by_run above.
        "human_spend_usd": round((_human_agg or {}).get("human_spend_usd", 0.0), 6),
        "human_spend_source": (_human_agg or {}).get("human_spend_source", "none"),
        # Honesty markers (N8/N9 + the open honesty item): a reader must be able to tell
        # whether the spend is exact, and whether the file-derived totals under-count
        # because capture was withheld or the enrichment replay was truncated.
        "spend_source": "estimated_from_usage" if any_est else "ledger_per_call",
        "capture_withheld": withheld,
        "enrichment_truncated": any_trunc,
    }
    return totals


def assemble_batch_manifest(action_id):
    """(Re-)assemble a batch action's per-locale asset manifest from its runs' detail
    manifests -- IDEMPOTENTLY, race-tolerantly (M5 + N4).

    A multi-locale batch action's manifest must record EVERY locale's assets. A leg cannot
    merge its locale into the shared TapAction.manifest without racing its siblings
    (concurrent read-modify-write of one JSON column loses locales -- the M5 defect). So a
    batch leg writes ONLY its own per-locale manifest into its run.detail (its own row, no
    race); this collects them into TapAction.manifest.by_locale.

    NOT write-once (N4). The Wave-B/round-1 defect wrote the map once and only for legs
    that already had a manifest: a batch with a leg still PAUSED (or whose enrichment
    errored) froze by_locale to the completed locales forever, and a later resume never
    updated it. Instead this RE-ASSEMBLES the whole map from all runs on every call, and a
    leg with no manifest yet contributes a MARKED-ABSENT entry (status recorded) rather
    than being silently omitted -- so a reader sees the locale exists but its assets are
    not-yet/never resolved, and a resume that finishes the leg fills it in on the next
    (re-)assemble. Idempotent; called at every leg's enrich (for a revive/resume) AND at
    the dispatcher's authoritative post-join finalize (finalize_batch_action).

    Best-effort; never raises."""
    if not action_id:
        return
    try:
        from app.services import tap_envelope

        if not tap_envelope.envelopes_enabled():
            return
        from sqlmodel import select

        from app.models import TapAction, TapRun

        with _session() as s:
            action = s.get(TapAction, action_id)
            if action is None:
                return
            runs = s.exec(select(TapRun).where(TapRun.action_id == action_id)).all()
            if not runs:
                return
            by_locale = {}
            src = action.source_locale or ""
            for r in runs:
                man = (r.detail or {}).get("manifest")
                loc = r.target_locale or ""
                if man:
                    by_locale[loc] = man
                    src = src or man.get("source_locale") or ""
                else:
                    # a leg with no manifest yet (paused / not-terminal / enrich-errored):
                    # record its identity as ABSENT rather than dropping the locale (N4).
                    by_locale[loc] = {"absent": True, "target_locale": loc, "run_status": r.status}
            new_manifest = {
                "schema_version": ENRICH_SCHEMA_VERSION,
                "source_locale": src,
                "by_locale": by_locale,
            }
            if action.manifest == new_manifest:
                return

            def _stage():
                # Re-read fresh so a P-C5 retry (post-rollback) writes on a current
                # snapshot; re-check equality so a concurrent (re-)assemble is a no-op.
                a = s.get(TapAction, action_id)
                if a is None or a.manifest == new_manifest:
                    return
                a.manifest = new_manifest
                s.add(a)

            tap_envelope.commit_tap_write(
                s,
                _stage,
                what=f"assemble_batch_manifest {action_id}",
            )
    except Exception:
        _warn_once("tap_enrich: assemble_batch_manifest failed (ignored)")


# --------------------------------------------------------------------------- #
# The single enrichment entry point (called from Wave A's finalize seams)
# --------------------------------------------------------------------------- #
def _write_run_detail(run_id, detail):
    """Write the run's per-leg DETAIL in its OWN short transaction (R1-F1). Rides out
    transient SQLite writer-lock contention with the bounded jittered P-C5 retry and
    DEGRADES (one WARN naming the taprun, the update dropped) rather than raising the lock
    traceback the volume flood surfaced. Idempotent: re-writes `detail` from the run's
    finalized state, so a duplicate terminal finalize writes byte-identical enrichment."""
    from app.models import TapRun
    from app.services import tap_envelope

    with _session() as s:

        def _stage():
            run = s.get(TapRun, run_id)
            if run is None:
                return
            # TODO(observability): `detail` was built from a PHASE-1 snapshot and is
            # blind-overwritten here -- a concurrent terminal re-finalize could land a
            # newer detail between phase 1 and this write, and this would clobber it back
            # to the older snapshot. Pre-existing (the single-session version had the same
            # last-writer-wins window); the read/write split widens it slightly. Benign:
            # detail is observability-only, idempotent from a terminal run's persisted
            # state, and both writers derive from the SAME finalized run, so the values
            # converge -- not worth a version guard for a MED fix. Revisit if detail ever
            # carries a non-idempotent field.
            run.detail = detail
            s.add(run)

        tap_envelope.commit_tap_write(
            s,
            _stage,
            what=f"run-detail enrichment for taprun {run_id}",
        )


def _write_action_manifest(action_id, src, tgt, run_manifest):
    """Write a NON-batch action's per-locale MANIFEST ONCE, in its own short transaction.
    Re-checks manifest-is-None INSIDE the stage so a concurrent finalize cannot double-
    write (write-once = a stable historical record) -- also the correct behaviour after a
    P-C5 retry's rollback. Degrades cleanly on sustained contention."""
    from app.models import TapAction
    from app.services import tap_envelope

    with _session() as s:

        def _stage():
            action = s.get(TapAction, action_id)
            if action is None or action.manifest is not None:
                return
            action.manifest = {
                "schema_version": ENRICH_SCHEMA_VERSION,
                "source_locale": src,
                "by_locale": {tgt: run_manifest},
            }
            s.add(action)

        tap_envelope.commit_tap_write(s, _stage, what=f"action manifest for {action_id}")


def enrich(
    action_id, run_id, *, workflow_run_id=None, document_id=None, target_locale="", source_locale=""
):
    """Populate the run's per-leg DETAIL and (for a non-batch action) its per-locale
    MANIFEST for a FINALIZED run envelope. Best-effort, never raises. Safe to call from
    every finalize exit: it no-ops unless the run has reached a TERMINAL status (a paused
    leg is not enriched; the resume that finishes it will be).

    Action TOTALS are NOT written here (they are rolled up once at
    tap_envelope.finalize_action_if_all_runs_done, H1); a BATCH action's manifest is
    assembled once at tap_envelope.finalize_batch_action (M5). This function writes only
    the run's OWN row (detail) plus, for a single-leg non-batch action, that action's
    manifest -- no cross-leg shared-column write, so parallel legs never race.

    Idempotent by construction: detail is (re)written from the run's final persisted
    state, so a duplicate terminal finalize writes byte-identical enrichment; the action
    manifest is write-once (a stable historical record)."""
    if not action_id or not run_id:
        return
    try:
        from app.services import tap_envelope

        if not tap_envelope.envelopes_enabled():
            return
        from app.models import Document, Project, TapAction, TapRun, TapStatus

        # --- PHASE 1: ENRICH OUTSIDE THE WRITE TRANSACTION (R1-F1) --------------
        # ALL the replay/read work runs in a READ-ONLY session that is CLOSED before any
        # write. Previously the reads AND the detail write shared ONE session, so a single
        # transaction stayed open across the whole (bounded-but-not-free) enrichment: under
        # a write flood the connection sat on the pool for the full replay, and the final
        # commit tried to upgrade a STALE read snapshot to a write -- which SQLite rejects
        # with SQLITE_BUSY ("database is locked"), a condition busy_timeout CANNOT wait out.
        # That is the R1-F1 flood traceback (UPDATE taprun SET detail=?) and the correlated
        # /ready stall (a pooled connection + the writer path held for ~seconds). Building
        # `detail` first and writing it from a FRESH short session holds the writer lock for
        # milliseconds, off a stale snapshot.
        with _session() as s:
            run = s.get(TapRun, run_id)
            if run is None or run.status not in TapStatus.TERMINAL:
                return
            action = s.get(TapAction, action_id)
            if action is None:
                return

            doc = s.get(Document, document_id) if document_id else None
            project = None
            if doc is not None and doc.project_id:
                project = s.get(Project, doc.project_id)
            src = (
                source_locale or run.source_locale or (getattr(project, "source_locale", "") or "")
            )
            tgt = target_locale or run.target_locale or ""

            # --- per-run detail ---------------------------------------------
            call_rows = _call_rows(action_id, run_id)
            rollup = _rollup_calls(call_rows)
            engines = _engine_mix(call_rows)
            steps, lqa_ran, ledger_spend = _step_detail(s, workflow_run_id)
            if ledger_spend is not None:
                rollup["ledger_spend_usd"] = ledger_spend
            segments = _run_segments(s, document_id, tgt)
            matched, plan, truncated = _matched_terms(s, project, segments, src, tgt)
            # Terminology T3 (LAW-TERM-16): when a toggle-ON step stamped its shown set onto
            # the capture records, the ADHERENCE view measures against that ACTUAL shown set
            # (the enforced set) instead of the divergent replay above — so the audit never
            # reports a capped-out/human-only term as "missed" that the model was never
            # shown. The replay's `plan` is still used for the manifest below; only `matched`
            # (and its adherence) switches. No stamp (toggle OFF, or Tap wrote none) -> the
            # replay stands, unchanged from today.
            _shown_matched, _shown_present = _shown_matched_from_capture(action_id, run_id, tgt)
            if _shown_present:
                matched = _shown_matched
                truncated = False
            adherence = _adherence(segments, matched, lqa_ran)
            # --- per-run asset manifest (per TARGET LOCALE) -----------------
            # Each leg records the assets IN PLAY FOR ITS OWN LOCALE, in its OWN run row
            # (M5). A multi-locale action's legs never merge into one shared manifest
            # column (that races -- concurrent read-modify-write loses locales), so a
            # 17-locale action records all 17 locales' TM/glossary/style/LLMemory here,
            # each tagged with its target_locale, race-free.
            run_manifest = _build_manifest(
                s, project, doc, src, tgt, plan, workflow_run_id=workflow_run_id
            )
            run_manifest["target_locale"] = tgt
            detail = {
                "schema_version": ENRICH_SCHEMA_VERSION,
                "target_locale": tgt,
                "source_locale": src,
                "segments": len(segments),
                "calls": call_rows,
                "rollup": rollup,
                "engines": engines,
                "steps": steps,
                "glossary": {"matched_terms": matched, "count": len(matched)},
                "adherence": adherence,
                "manifest": run_manifest,
                "enrichment_truncated": bool(truncated),
                "timing": {
                    "started_at": _iso(run.started_at),
                    "finished_at": _iso(run.finished_at),
                    "duration_ms": (
                        int((run.finished_at - run.started_at).total_seconds() * 1000)
                        if run.finished_at and run.started_at
                        else None
                    ),
                },
            }
            is_batch = action.batch_id is not None
            # F2: only the leg that makes the ACTION terminal (re)assembles the batch
            # manifest -- assembling at every leg's enrich deserialized every sibling's
            # detail (O(N^2)) during the run. The action's status here reflects the
            # finalize_pair that ran just before enrich.
            action_terminal = action.status in TapStatus.TERMINAL
            # NON-BATCH: the single leg owns the action (no cross-leg race), so it writes
            # the action's per-locale manifest ONCE (write-once = a stable historical
            # record of what the run used). Decided here (snapshot read), written in phase 2
            # with a race-safe re-check inside its stage.
            write_action_manifest = not is_batch and action.manifest is None
        # read session CLOSED here -> the read snapshot is released BEFORE any write.

        # --- PHASE 2: SHORT WRITE TRANSACTIONS (P-C5 retry + degrade) -----------
        _write_run_detail(run_id, detail)
        if write_action_manifest:
            _write_action_manifest(action_id, src, tgt, run_manifest)
        # NOTE: action TOTALS are NOT rolled up here. They are computed over the committed
        # children at tap_envelope.finalize_action_if_all_runs_done (H1).

        # BATCH: (re-)assemble the action's per-locale manifest ONCE the action is terminal
        # (F2) -- the terminal leg during a normal batch, or the resume that completes a
        # partially-paused batch. Idempotent + marks not-yet-resolved legs absent, so a
        # resume that finishes the batch fills every locale (the round-1 write-once froze the
        # partial map). The authoritative post-join re-assembly still runs in
        # finalize_batch_action for the paused/partial case.
        if is_batch and action_terminal:
            assemble_batch_manifest(action_id)
    except Exception:
        _warn_once("tap_enrich: enrich failed (ignored)")
