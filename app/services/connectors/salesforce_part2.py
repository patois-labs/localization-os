"""salesforce: part 2 (layered pure move)

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging

from app.services.connectors import flow
from app.services.connectors.salesforce_common import (
    _sf_build_payload,
    _sf_build_result,
    _sf_compute_ok,
    _sf_is_suppressed,
    _sf_partition,
    _sf_preflight,
    _sf_prepare,
)

log = logging.getLogger("loc_tms.connectors")


def _sf_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} translation version(s) "
        f"across {len(ctx.usable)} language(s) from {len(ctx.articles)} "
        f"article(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) language(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} empty article(s) (no translatable content)."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not active as a "
            f"Salesforce Knowledge language: {', '.join(ctx.rejected)}."
        )
    if ctx.errors:
        parts.append("Errors: " + "; ".join(ctx.errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("article write(s)").strip())  # Cluster A: loud, per-run
        log.warning(
            "salesforce_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def salesforce_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Salesforce Knowledge articles into one or more languages, each
    written back as the article's own translation VERSION via the T2
    SubResourceStrategy (submit-for-translation -> PATCH Draft -> set-complete ->
    publish, correlated by KnowledgeArticleId).

    Thin wrapper over flow.run_translate_flow. Salesforce-specific behavior via the
    spec hooks: the PER-RECORD-TYPE field-mapping generalizer (fieldmap.py) for
    extraction + write-payload reassembly, active-Knowledge-language rejection
    (all-rejected = FAIL) with many-to-one locale-fold dedup (_sf_partition,
    FINDING 1), empty-article skip, content-hash suppression that respects the
    managed publish lifecycle (a Draft never suppresses in publish mode until it
    reaches Online, FINDING 2; a missing language is always written), and the
    publish toggle. Poll-first, so NO watermark.
    only_pairs restricts the write loop to those (article_id, locale) pairs.
    """
    spec = flow.TranslateFlowSpec(
        kind="salesforce",
        no_conn_msg="No such Salesforce connection.",
        hash_key="sf_hashes",
        probe_label="Salesforce",
        preflight=_sf_preflight,
        prepare=_sf_prepare,
        is_suppressed=_sf_is_suppressed,
        build_payload=_sf_build_payload,
        compute_draft=lambda cfg: str(cfg.get("publish", "0")).strip() not in ("1", "true", "yes"),
        compute_ok=_sf_compute_ok,
        build_message=_sf_build_message,
        build_result=_sf_build_result,
        extra_counters=("empty_skipped",),
        partition=_sf_partition,
    )
    return flow.run_translate_flow(
        spec,
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
