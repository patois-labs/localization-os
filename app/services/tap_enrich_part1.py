"""tap_enrich: part1

Documented in: docs/architecture/connectors.md
"""

import hashlib


def _hash(*parts) -> str:
    """A short, stable content-version hash (16 hex) over `parts`. Order-sensitive;
    callers pass a canonicalized, order-stable signature. A hash, NOT a snapshot (D5)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(b"\x1f")
        h.update(str(p).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def _iso(dt):
    return dt.isoformat() if dt is not None else None


# --------------------------------------------------------------------------- #
# The per-action call FILE -> raw per-call metric rows (the roll-up's raw parts)
# --------------------------------------------------------------------------- #
def _metric_row(rec, seq, pricing):
    """Build ONE raw per-call METRIC row from a capture record (tokens/spend/engine only,
    NEVER prompt/response text -- D5). Shared by the per-run reader and the single-pass
    grouped reader so both spell a row identically."""
    usage = rec.get("usage") or {}
    # HONEST SPEND: prefer the EXACT per-call cost the record carries (the ledger value
    # run_completion accrued -- $0 for a mock/exempt call). Fall back to a pricing recompute
    # only for a LEGACY record written before Wave B stamped cost_usd; that fallback can
    # overstate an exempt call, so `spend_estimated` records which path produced it.
    spend = 0.0
    spend_est = False
    if rec.get("cost_usd") is not None:
        try:
            spend = float(rec.get("cost_usd"))
        except Exception:
            spend = 0.0
    elif pricing is not None and usage:
        spend_est = True
        try:
            spend = float(
                pricing.cost_usd(
                    rec.get("provider_kind") or rec.get("provider"), rec.get("model"), usage
                )
            )
        except Exception:
            spend = 0.0
    return {
        "seq": seq,
        "provider": rec.get("provider") or "",
        "provider_kind": rec.get("provider_kind") or "",
        "model": rec.get("model") or "",
        "task": rec.get("task") or "",
        "outcome": rec.get("outcome") or "",
        "attempt": rec.get("attempt"),
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(usage.get("cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(usage.get("cache_write_tokens", 0) or 0),
        "chars": int(usage.get("chars", 0) or 0),
        "spend_usd": round(spend, 8),
        "spend_estimated": spend_est,
        # run-190 Item B/C: the per-call FAILURE SHAPE (metadata only) — carried through to
        # the run/API/summary surfaces so a reader sees WHY a call failed without the bodies.
        # None on a legacy record that predates build_record stamping them.
        "finish_reason": rec.get("finish_reason"),
        "error_class": rec.get("error_class"),
        "elapsed_ms": rec.get("elapsed_ms"),
        "output_chars": rec.get("output_chars"),
        "reasoning_tokens": rec.get("reasoning_tokens"),
        # #342: the sampling a LOCAL call sent (None for every other provider / legacy record).
        "temperature": rec.get("temperature"),
        "seed": rec.get("seed"),
    }


def _pricing():
    try:
        from app.services import pricing

        return pricing
    except Exception:
        return None


def _call_rows(action_id, run_id):
    """The raw per-call METRIC rows for ONE run, read from the per-action call file. Only
    records stamped for THIS run_id are included. Best-effort: [] on any failure."""
    from app.services import prompt_tap

    pricing = _pricing()
    rows = []
    try:
        path = prompt_tap._action_path(action_id)
        if path is None:
            return rows
        seq = 0
        for rec in prompt_tap._iter_file(path):
            meta = rec.get("meta") or {}
            if run_id is not None and meta.get("run_id") != run_id:
                continue
            rows.append(_metric_row(rec, seq, pricing))
            seq += 1
    except Exception:
        return rows
    return rows


def _rollup_calls(call_rows):
    """Sum the raw call rows into the per-run leg roll-up. The sum's PARTS are the rows
    themselves (stored in detail.calls), so total == sum(parts) is always verifiable --
    the envelope cannot lie about its children (§4, the Wave A review lens)."""
    calls = len(call_rows)
    failures = sum(1 for r in call_rows if r["outcome"] != "ok")
    tokens_in = sum(
        r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] for r in call_rows
    )
    tokens_out = sum(r["output_tokens"] for r in call_rows)
    chars = sum(r["chars"] for r in call_rows)
    spend = round(sum(r["spend_usd"] for r in call_rows), 8)
    # Honest provenance: "ledger" iff EVERY call carried its exact recorded cost; any
    # legacy call that fell back to a pricing recompute makes it "estimated".
    any_est = any(r.get("spend_estimated") for r in call_rows)
    return {
        "calls": calls,
        "failures": failures,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "chars": chars,
        "spend_usd": spend,
        "spend_source": "estimated_from_usage" if any_est else "ledger_per_call",
    }


def _engine_mix(call_rows):
    """The engines actually used this leg + a failover signal, from the raw call rows.
    `failover` is True when more than one distinct provider/model answered (Wave A / §5
    'failover chain summary'). `final` is the last call's engine."""
    by = {}
    order = []
    for r in call_rows:
        key = "{}/{}".format(r["provider_kind"] or r["provider"], r["model"])
        if key not in by:
            by[key] = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "spend_usd": 0.0}
            order.append(key)
        b = by[key]
        b["calls"] += 1
        b["tokens_in"] += r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"]
        b["tokens_out"] += r["output_tokens"]
        b["spend_usd"] = round(b["spend_usd"] + r["spend_usd"], 8)
    return {"by_engine": by, "failover": len(by) > 1, "final": order[-1] if order else None}


# --------------------------------------------------------------------------- #
# StepRun rows (run-correlated by workflow_run_id) -> step outcomes + ledger spend
# --------------------------------------------------------------------------- #
def _step_detail(s, workflow_run_id):
    """(steps, lqa_step_ran, ledger_spend) for a workflow leg, from its StepRun rows.

    StepRun carries workflow_run_id -- a RECORDED run id (not a time window, §11.4) -- so
    these facts are unambiguously attributed to THIS run. `lqa_step_ran` gates adherence's
    checked/not-checked; `ledger_spend` is the ledger's own figure (the exact cost
    record_usage accrued), returned as an independent cross-check beside the
    priced-from-usage roll-up. Best-effort: ([], False, None) on any failure or a direct
    (non-workflow) leg."""
    if workflow_run_id is None:
        return [], False, None
    try:
        from sqlmodel import select

        from app.models import StepRun, StepType, WorkflowStep

        steps = []
        lqa_ran = False
        ledger = 0.0
        any_cost = False
        rows = s.exec(select(StepRun).where(StepRun.workflow_run_id == workflow_run_id)).all()
        for sr in rows:
            wstep = s.get(WorkflowStep, sr.step_id) if sr.step_id else None
            stype = getattr(wstep, "step_type", "") or ""
            if stype == StepType.LQA:
                lqa_ran = True
            if sr.cost_usd is not None:
                ledger += float(sr.cost_usd)
                any_cost = True
            steps.append(
                {
                    "step_id": sr.step_id,
                    "step_type": stype,
                    "status": sr.status,
                    "gate": sr.gate,
                    "score": sr.score,
                    "cost_usd": sr.cost_usd,
                    "tokens_in": sr.tokens_in,
                    "tokens_out": sr.tokens_out,
                    "outcome_index": sr.outcome_index,
                }
            )
        return steps, lqa_ran, (round(ledger, 8) if any_cost else None)
    except Exception:
        return [], False, None


# --------------------------------------------------------------------------- #
# Glossary matched terms + adherence (D6, the differentiated part)
# --------------------------------------------------------------------------- #
def _run_segments(s, document_id, target_locale):
    """This leg's segments (source + delivered target), in order. The run's OWN delivered
    text -- exactly what an LQA pass judged. [] when the document/segments are gone
    (dangling-safe: adherence degrades to empty, never crashes)."""
    if document_id is None:
        return []
    try:
        from sqlmodel import select

        from app.models import Segment

        segs = s.exec(
            select(Segment).where(
                Segment.document_id == document_id, Segment.target_locale == target_locale
            )
        ).all()
        return sorted(segs, key=lambda x: x.order_index if x.order_index is not None else 0)
    except Exception:
        return []


def _shown_matched_from_capture(action_id, run_id, target_locale):
    """Terminology T3 (LAW-TERM-16): recover this leg's ACTUAL shown glossary set from the
    per-call capture records the toggle-ON renderer stamped (`meta.shown_terms`), instead of
    replaying `match_terms_merged`. The replay caps at ENRICH_MATCH_SEGMENT_CAP and drops the
    per-step pin, so under the toggle it diverges from what the model was actually shown; the
    stamp is exact. Returns `(matched, present)` in the SAME shape `_matched_terms` returns
    (`matched`); `present` is True iff ANY record for this leg carried a `shown_terms` stamp,
    which is the caller's signal to use this in place of the replay. The stamp pins
    `meta.segment_index` to the persisted `Segment.id` (M1), so the resulting `segment_ids`
    join the same `seg.id` map `_adherence` uses. Best-effort: ([], False) on any failure."""
    from app.services import prompt_tap

    by_key = {}
    present = False
    try:
        path = prompt_tap._action_path(action_id)
        if path is None:
            return [], False
        for rec in prompt_tap._iter_file(path):
            meta = rec.get("meta") or {}
            if run_id is not None and meta.get("run_id") != run_id:
                continue
            if "shown_terms" not in meta:
                continue
            # One per-action file carries ALL legs of a multi-locale batch; select this
            # leg by its target_locale (the M1 leg identity) when the stamp carries one.
            leg_loc = meta.get("target_locale")
            if target_locale and leg_loc is not None and leg_loc != target_locale:
                continue
            present = True
            seg_id = meta.get("segment_index")
            for t in meta.get("shown_terms") or []:
                key = (t.get("source_term") or "", t.get("target_term") or "")
                if not key[0]:
                    continue
                rec2 = by_key.setdefault(
                    key,
                    {
                        "source": key[0],
                        "expected_target": key[1],
                        "dnt": bool(t.get("dnt")),
                        "sense_label": t.get("sense_label"),
                        "segment_ids": [],
                    },
                )
                if seg_id is not None and seg_id not in rec2["segment_ids"]:
                    rec2["segment_ids"].append(seg_id)
        return list(by_key.values()), present
    except Exception:
        return [], False


# --------------------------------------------------------------------------- #
# The asset manifest (TapAction.manifest) -- identity + hash, never a snapshot
# --------------------------------------------------------------------------- #
def _entry_hash(entries):
    """A content-version hash over a set of glossary entries (sorted term tuples). A
    version signature that changes when the terms change -- NOT the terms (D5)."""
    sig = sorted(
        "{}|{}|{}|{}".format(e.source_term, e.target_term, e.dnt, getattr(e, "source_locale", ""))
        for e in entries
    )
    return _hash("glossary", len(sig), *sig)


def _glossary_manifest(s, project, plan, src, tgt):
    """Glossaries in play, in precedence order: id, name, status, pin state, content
    hash. Lists the module glossaries the plan resolved AND -- when the plan merges the
    project's LEGACY per-project entries (include_legacy) -- a synthetic `legacy` source
    so the manifest is complete (a legacy-only project still records its termbase). The
    hash is a version signature over the entry tuples, NOT the terms themselves (D5).
    Dangling-safe: a glossary id that no longer resolves is simply skipped."""
    out = []
    if plan is None or project is None:
        return out
    try:
        from sqlmodel import select

        from app.models import Glossary, GlossaryEntry

        pinned = set()
        try:
            from app.services import project_sources

            pinned = set(project_sources.pinned_glossary_ids(s, project) or ())
        except Exception:
            pinned = set()
        for gid in getattr(plan, "glossary_ids", ()) or ():
            g = s.get(Glossary, gid)
            if g is None:
                continue
            try:
                entries = s.exec(
                    select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)
                ).all()
                ghash = _entry_hash(entries)
            except Exception:
                ghash = None
            out.append(
                {
                    "id": gid,
                    "name": g.name,
                    "status": g.status,
                    "pinned": gid in pinned,
                    "content_hash": ghash,
                    "legacy": False,
                }
            )
        if getattr(plan, "include_legacy", False):
            try:
                legacy = s.exec(
                    select(GlossaryEntry).where(
                        GlossaryEntry.project_id == project.id, GlossaryEntry.glossary_id.is_(None)
                    )
                ).all()
            except Exception:
                legacy = []
            if legacy:
                out.append(
                    {
                        "id": None,
                        "name": "(legacy per-project terms)",
                        "status": None,
                        "pinned": False,
                        "content_hash": _entry_hash(legacy),
                        "legacy": True,
                        "term_count": len(legacy),
                    }
                )
    except Exception:
        return out
    return out


def _style_guide_manifest(s, project, src, tgt):
    """The resolved style guide: id, name, status, content hash (over its body), version
    (updated_at). None when none resolves. Replays the run's own resolver."""
    try:
        from app.services import style_guides

        g = style_guides.resolve_style_guide(
            s,
            org_id=getattr(project, "org_id", None),
            customer_id=getattr(project, "customer_id", None),
            content_type_id=getattr(project, "content_type_id", None),
            source_locale=src,
            target_locale=tgt,
        )
        if g is None:
            return None
        return {
            "id": g.id,
            "name": g.name,
            "status": g.status,
            "content_hash": _hash("style_guide", g.body or ""),
            "version": _iso(getattr(g, "updated_at", None)),
        }
    except Exception:
        return None


def _formality_manifest(s, project, doc, tgt):
    """The MT formality register that applied to this leg (#617 item 7, ADR 0062): register,
    source level (document / project / customer), target locale. None when nothing resolves.
    Replays the run's own resolver."""
    try:
        from app.services import formality

        return formality.record_for(s, project=project, document=doc, target_locale=tgt)
    except Exception:
        return None


def _tm_manifest(s, project, src, tgt):
    """TM sources in play: the run's OWN project plus any ATTACHED source projects, in
    precedence order (own first), each with a content-version hash. The hash is a cheap
    VERSION SIGNATURE (row count + max row id per project) -- it changes when the TM
    grows/changes without hashing every unit (D5: a version hash, not a snapshot).
    Dangling-safe: a source id that no longer resolves contributes nothing."""
    if project is None:
        return None
    try:
        from sqlmodel import func, select

        from app.models import FullTextTM, Project, SegmentTM
        from app.services import tm as tm_svc

        def _sig(pid):
            try:
                n_seg = s.exec(
                    select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == pid)
                ).one()
                n_ft = s.exec(
                    select(func.count()).select_from(FullTextTM).where(FullTextTM.project_id == pid)
                ).one()
                n_seg = n_seg[0] if isinstance(n_seg, (list, tuple)) else n_seg
                n_ft = n_ft[0] if isinstance(n_ft, (list, tuple)) else n_ft
                return _hash("tm", pid, int(n_seg or 0), int(n_ft or 0))
            except Exception:
                return None

        precedence = [project.id]
        try:
            attached = list(tm_svc._tm_source_project_ids(s, project.id) or ())
        except Exception:
            attached = []
        for pid in attached:
            if pid not in precedence:
                precedence.append(pid)
        sources = []
        for pid in precedence:
            p = s.get(Project, pid)
            if p is None and pid != project.id:
                continue
            sources.append(
                {
                    "project_id": pid,
                    "name": getattr(p, "name", None),
                    "own": pid == project.id,
                    "content_hash": _sig(pid),
                }
            )
        return {"precedence": precedence, "sources": sources}
    except Exception:
        return None


def _llmemory_manifest(s, project, src, tgt):
    """The LLMemory context eligible for this run: the ACTIVE, in-scope recall entries
    (ids + a content hash of each belief's value), consistent with the id+hash rule (D5).
    Best-effort -- recall picks a tiny-k subset per segment at run time; here we record
    the eligible set's identity, which is the reproducible provenance an ML reader wants.
    None when LLMemory is unavailable / out of scope."""
    if project is None:
        return None
    try:
        from app import extensions
        from app.models import MemoryKind

        if not extensions.memory.installed:
            return None
        entries = extensions.memory._active_entries(
            s,
            MemoryKind.ALL,
            customer_id=getattr(project, "customer_id", None),
            project_id=project.id,
            src=src,
            tgt=tgt,
            org_id=getattr(project, "org_id", None),
        )
        out = []
        for e in entries or []:
            out.append(
                {
                    "id": e.id,
                    "kind": getattr(e, "kind", ""),
                    "content_hash": _hash(
                        "mem",
                        getattr(e, "source_term_norm", "") or getattr(e, "source_phrase", "") or "",
                        getattr(e, "value", "") or "",
                    ),
                }
            )
        if not out:
            return None
        return {"count": len(out), "entries": out}
    except Exception:
        return None


def _vendor_manifest(s, workflow_run_id):
    """The VENDOR asset/leg class for the run's envelope (Wave C, interop R5/R6): the
    dispatch legs this workflow run sent to human vendors. Summarized (never the claim
    token / secret / full endpoint URL -- host only, leg_to_dict's discipline): vendor,
    status, host, protocol, counts, estimate-vs-actual cost, and partial-return progress.
    None when the run has no legs. Independently guarded like every other asset resolver."""
    if not workflow_run_id:
        return None
    try:
        from sqlmodel import select

        from app.models import VendorDispatch
        from app.services import vendor_return

        legs = s.exec(
            select(VendorDispatch)
            .where(VendorDispatch.workflow_run_id == workflow_run_id)
            .order_by(VendorDispatch.id.asc())
        ).all()
        if not legs:
            return None
        out = []
        for leg in legs:
            d = vendor_return.leg_to_dict(s, leg)
            out.append(
                {
                    "id": d["id"],
                    "vendor": d["vendor_name"] or d["vendor_id"],
                    "status": d["status"],
                    "endpoint_host": d["endpoint_host"],
                    "protocol": d.get("protocol", "native"),
                    "target_locale": d["target_locale"],
                    "segments": d["segment_count"],
                    "words": d["word_count"],
                    "estimate_cost_usd": d["estimate_cost_usd"],
                    "actual_cost_usd": d["actual_cost_usd"],
                    "returned_ids": d["returned_ids"],
                    "dispatched_ids": d["dispatched_ids"],
                }
            )
        return {"count": len(out), "legs": out}
    except Exception:
        return None
