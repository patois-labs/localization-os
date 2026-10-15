"""Contentful (headless CMS) connector — a T1 (field-level map) consumer of the
connector framework. Reference: bb-io/Contentful digest

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, API_VERSION, CMA_CONTENT_TYPE, _REGION_BASES,
_TRANSLATABLE_TYPES, _SYMBOL_MAX, _MAX_VERSION_RETRIES, _MAX_TOTAL_CONFLICT_WAIT, _NAME_SKIP_EXACT,
_NAME_ID_TOKENS, _WORD_RE, _CAMEL_ID_RE, _EMAIL_RE, _URL_RE, _DATETIME_RE, _TIME_RE, _name_words,
_name_looks_id, _compile_js_regex, _value_is_machine, _HELD_BACK_PREFIX, _HELD_BACK, _VBLOCK_PREFIX,
_Unit, _PutOutcome, _parse_names, _validate_segment, SPEC, ContentfulEntryStrategy, _ctf_preflight,
_ctf_partition, _ctf_prepare, _ctf_is_suppressed, _ctf_build_payload, _is_held_back, _is_vblock,
_dropped_as_source, _confirmed_incomplete, ContentfulConnector from
app.services.connectors.contentful_part1, app.services.connectors.contentful_part2.

TOPOLOGY — T1 (field-level map), re-derived from the digest rather than trusted.
A Contentful ENTRY is ONE record that carries ALL of its locales inline: every
field is a JSON object keyed `fields.{fieldId}.{locale}` (digest §3 — "Fields are
JObjects keyed fields[fieldId][locale]"). Localizing locale X = adding X's key
under each translatable field of that one entry. There is NO per-locale
sub-resource to 404-probe (that is T2) and NO separate document-per-locale we must
track ourselves (that is T3) — the source system owns the whole correlation on the
single entry. That is exactly T1. All access is the Content Management API (CMA),
space + environment scoped (`/spaces/{space}/environments/{env}/…`); the read-only
Delivery API is never touched (writes need the CMA anyway, and using ONE API for
read+write keeps the draft/published steady state consistent — see below).

WRITE MODEL — read-current → overlay ONLY this-run's translated (field, target-locale)
values → PUT the whole entry with `X-Contentful-Version`. This is the framework's
CARDINAL T1 rule (read-current → merge → write-full, design §2) specialised with
Contentful's optimistic-concurrency versioning (design §3.5). Two properties matter:
  * We overlay ONLY the fields we actually translated THIS run (never the whole
    target-locale block), so a held-back (NEEDS_REVIEW) field, every OTHER locale,
    and every non-translated field keep their stored values — the K1/H2 "guard for
    EVERY field on create AND update" law. This is the locale-scoped partial update
    bb-io calls `UpdateEntryForLocale`, expressed as our never-blind-PUT rule.
  * On a `409` version conflict we RE-FETCH the fresh entry, RE-APPLY only our
    translated delta onto it, and RE-PUT (bounded retries) — never re-sending a
    stale snapshot of the other locales. This mirrors bb-io's "retry re-runs the
    whole closure, which re-fetches the entry and picks up a fresh version" (digest
    §6). Retries exhausted → ABORT the pair (WriteResult ok=False), which records NO
    content-hash, so the next scheduled run retries (never a silent clobber).

WHY NOT the generic strategy.FieldMapStrategy (which Intercom uses): FieldMapStrategy
models Intercom's T1 SUB-SHAPE — a single locale-keyed `translated_content` map with
one block per locale, a versionless whole-map PUT, and a `put_record` that never
learns which locale it is writing. Contentful is the TRANSPOSED sub-shape (per-field
locale maps) with optimistic-concurrency versioning; correct 409 handling here MUST
re-apply only the translated delta, which FieldMapStrategy's block-merge / put_record
seam structurally cannot do (it only ever sees the merged container, not the delta or
the target locale). Design §3.5 calls out version-locking + bounded-retry as a
Contentful-specific need the shipped FieldMapStrategy does not provide. So Contentful
supplies a T1 strategy tailored to its concurrency model (ContentfulEntryStrategy
below), SUBCLASSING the shared LocaleMappingStrategy and REUSING the shared D1 record
lock (`strategy._record_write_lock_for`). Every OTHER shared primitive is reused
unchanged: the flow engine, WriteBudget, content-hash suppression, egress guard,
org/customer scoping (access.py, via the isolation sweep on the /api route), the
base._validate_target SSRF guard, and the harvest seam.

DRAFT / PUBLISHED STEADY STATE (defect taxonomy C). The CMA GET returns the entry's
current (latest, incl. unpublished) representation; the CMA PUT updates that same
representation and bumps `sys.version` — it does NOT publish. We NEVER publish
(publishing is the operator's explicit action). So read, existence-probe, and write
all target the SAME state within the SAME environment: after we write locale L, the
next run's read sees L present (exists → suppressed by the content hash) — no infinite
re-translate loop. (Runbook MANDATORY live-check: confirm the CMA GET returns the
draft/latest state with no status parameter, and that an entry update is a full
replace at the entry level so the read-merge-PUT is required.)

FIELD SELECTION — SCHEMA-DRIVEN + a NAME/VALUE heuristic (defect taxonomy D + review
H1 + design §3.3 localized-flag gate). Contentful's CMA token CAN read the content-type
schema, so the type/localized gate is exact: a field is a candidate iff (a)
`field.localized == true` AND (b) `field.type` is a type this connector handles
(`Symbol` / `Text`, and `RichText` since #648: see RICH TEXT below). But a LOCALIZED
Symbol is NOT automatically translatable
copy — Contentful users localize slugs, canonical URLs, codes and dates — so in AUTO
mode a candidate is still skipped when its NAME is an id/slug marker or its VALUE is an
unambiguous machine format (email / URL / ISO date-time), the same already-reviewed
skip layer Strapi uses. EVERYTHING skipped is REPORTED with a reason in the run message
+ result: Object/Location/Boolean/Integer/Number/Date/Link/Array, non-localized fields
(which physically cannot receive a target locale — Contentful 422s such a write), and
the id/slug/machine-value skips. An operator `fields` allowlist BYPASSES the name/value
heuristic (operator override) but still respects the localized + text-type gates (a
non-localized field named there is refused + reported, never written blind).

RICH TEXT (#648). A Rich Text field value is a Contentful document tree (nodeType /
data / content; text nodes carry value + marks). app/filters/contentful_rich_text.py
walks it exactly like the Jira ADF inline units: ONE segment per text block (paragraph,
heading; list items, quotes and table cells hold paragraphs), marked text and
hyperlink / entry-hyperlink / asset-hyperlink wrappers carried as paired placeholders
and embedded inline entries and code spans as point placeholders; embedded blocks,
unknown node types and every `data` payload are copied byte-identical from the source.
The flow engine's p_index for these units is "<field>#<n>". A field is ALL-OR-NOTHING:
if any block is held (NEEDS_REVIEW) or the translated placeholders do not round-trip
(structure guard), the field is left out of the payload, so the stored value stays and
no partial or mangled tree is written; the pair then follows the #640 rules (no new
locale created, no content hash, retried next run). `localized: false` Rich Text is
skipped like any other non-localized field.

WRITE-TIME VALIDATION (review H2). Before overlaying a translated value the write checks
it against the field's CMA validations (Symbol's hard 256 cap, plus the field's own
size/regexp/enum) so a length-expanding translation does NOT 422 the WHOLE entry PUT and
discard the entry's other good translations. An offending field is held back PER FIELD
(the delta overlay writes per field), the rest are written, and the held field is
surfaced as an operator-visible INCOMPLETE marker (see below).

INCOMPLETE PAIRS (review M1). When a write lands SOME fields but holds others back — a
NEEDS_REVIEW hedge or a validation failure — the flow still records the content hash
(suppression stands, so there is no MT re-spend loop) AND records the pair + held fields
as an operator-visible `incomplete` marker in the run message + result. retry_failed can
re-attempt these pairs: an explicit retry passes them as only_pairs, and the flow engine
BYPASSES content-hash suppression for exactly those pairs (flow.py stage 6), so they
re-materialize and re-attempt regardless of the recorded hash. Framework-wide adoption
of this marker for the other connectors is a documented follow-up.

SUPERSEDED for the hash (#665, 2026-10-10: nothing is marked done until every field
is written): a pair written INCOMPLETE, and a pair blocked by a CMA validation (nothing
written), record NO content hash. Because such a hold is deterministic for an unchanged
source, the pair is PARKED instead (`contentful_blocked` reason + `contentful_blocked_src`
source hash, flow spec hook `parked`): scheduled runs skip it without any MT, list it as
NOT DONE, and a source edit or an explicit retry re-attempts it. Review / placeholder holds
are not parked (they are retried every run, #640).

DEFERRED FROM v1 (studied in the digest, deliberately NOT half-built — see the report
+ runbook): recursive linked-entry embedding — each entry is translated INDEPENDENTLY by
id, no reference-graph walk. (Rich Text, formerly deferred, shipped in #648.)

CHANGE DETECTION — poll + per-(entry, locale) content-hash suppression, no watermark.
Contentful HAS webhooks (digest §5) but their intake signature scheme is not something
this build can verify offline against a real payload, so — following the Intercom/Zoho
precedent (never guess a scheme) — v1 ships POLL-FIRST; webhook intake is a documented
follow-up. `sys.updatedAt` bumps on any change (not a content-only signal), so it never
drives a watermark; the source-content hash is the sole re-translate gate.

PROVENANCE: endpoint shapes are reference-and-reimplemented from bb-io/Contentful
(© 2023 Blackbird, MIT — verified verbatim at build time). See
THIRD_PARTY_NOTICES.md.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.services.connectors import base, flow, harvest, webhooks
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    harvest_context_field,
    to_int,
)
from app.services.connectors.base import (
    content_hash as _content_hash,
)
from app.services.connectors.contentful_part1 import (
    _CAMEL_ID_RE,
    _DATETIME_RE,
    _EMAIL_RE,
    _HELD_BACK,
    _HELD_BACK_PREFIX,
    _MAX_TOTAL_CONFLICT_WAIT,
    _MAX_VERSION_RETRIES,
    _NAME_ID_TOKENS,
    _NAME_SKIP_EXACT,
    _REGION_BASES,
    _SYMBOL_MAX,
    _TIME_RE,
    _TRANSLATABLE_TYPES,
    _URL_RE,
    _VBLOCK_PREFIX,
    _WORD_RE,
    API_VERSION,
    CMA_CONTENT_TYPE,
    SPEC,
    ContentfulEntryStrategy,
    _compile_js_regex,
    _confirmed_incomplete,
    _ctf_build_payload,
    _ctf_extra_held,
    _ctf_is_suppressed,
    _ctf_parked,
    _ctf_partition,
    _ctf_preflight,
    _ctf_prepare,
    _ctf_write_incomplete,
    _dropped_as_source,
    _is_held_back,
    _is_vblock,
    _name_looks_id,
    _name_words,
    _parse_names,
    _PutOutcome,
    _Unit,
    _validate_segment,
    _value_is_machine,
    log,
)
from app.services.connectors.contentful_part2 import ContentfulConnector
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.strategy import (
    ABSENT,
    LocaleMappingStrategy,
    WriteResult,
    _record_write_lock_for,
)

__all__ = [
    "ABSENT",
    "API_VERSION",
    "CMA_CONTENT_TYPE",
    "SPEC",
    "_CAMEL_ID_RE",
    "_DATETIME_RE",
    "_EMAIL_RE",
    "_HELD_BACK",
    "_HELD_BACK_PREFIX",
    "_MAX_TOTAL_CONFLICT_WAIT",
    "_MAX_VERSION_RETRIES",
    "_NAME_ID_TOKENS",
    "_NAME_SKIP_EXACT",
    "_REGION_BASES",
    "_SYMBOL_MAX",
    "_TIME_RE",
    "_TRANSLATABLE_TYPES",
    "_URL_RE",
    "_VBLOCK_PREFIX",
    "_WORD_RE",
    "AuthKind",
    "Capability",
    "ChangeSignals",
    "Connector",
    "ConnectorError",
    "ConnectorPlugin",
    "ConnectorSpec",
    "ContentfulConnector",
    "ContentfulEntryStrategy",
    "FieldSpec",
    "LocaleMappingStrategy",
    "PulledItem",
    "RichText",
    "Topology",
    "WriteResult",
    "_PutOutcome",
    "_Unit",
    "_compile_js_regex",
    "_confirmed_incomplete",
    "_content_hash",
    "_ctf_build_payload",
    "_ctf_extra_held",
    "_ctf_is_suppressed",
    "_ctf_parked",
    "_ctf_partition",
    "_ctf_preflight",
    "_ctf_prepare",
    "_ctf_write_incomplete",
    "_dropped_as_source",
    "_is_held_back",
    "_is_vblock",
    "_name_looks_id",
    "_name_words",
    "_parse_names",
    "_record_write_lock_for",
    "_validate_segment",
    "_validate_target",
    "_value_is_machine",
    "base",
    "copy",
    "dataclass",
    "harvest",
    "harvest_context_field",
    "httpx",
    "log",
    "logging",
    "quote",
    "re",
    "to_int",
    "webhooks",
]


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _blocked(ctx):
    """Validation-blocked pairs this run (review F1a/F2) — reported, NOT counted as a
    real failure (so the connection is not permanently red) and NOT re-spent next run."""
    return [f for f in ctx.failures if _is_vblock(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f) and not _is_vblock(f)]


def _ctf_compute_ok(ctx):
    if not ctx.usable:
        return False  # nothing translatable (all rejected and/or all source-equal)
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or ((ctx.skipped + ctx.counters["parked_skipped"]) > 0 and not real)
        or (
            not real
            and (ctx.created + ctx.updated + ctx.skipped + ctx.counters["parked_skipped"]) == 0
        )
    )


def _ctf_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} locale value(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} entry(ies)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) locale(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} entry(ies) with no translatable fields."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not enabled in Contentful: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source locale "
            f"({', '.join(dropped)}) - configure different target locales."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (entry, locale) pair(s) awaiting review "
            f"(NEEDS_REVIEW) - nothing written; they will be re-attempted next run."
        )
    incomplete = _confirmed_incomplete(ctx)
    if incomplete:
        sample = "; ".join(
            f"{p['article_id']}/{p['locale']}: {', '.join(p['held_fields'])}"
            for p in incomplete[:3]
        )
        parts.append(
            f"Wrote {len(incomplete)} pair(s) INCOMPLETE - some field(s) held back "
            f"(NEEDS_REVIEW or failed CMA validation), the rest were written: "
            f"{sample}. Fix the source/field and re-run, or use retry."
        )
    if ctx.counters["parked_skipped"]:
        parts.append(
            f"{ctx.counters['parked_skipped']} pair(s) still NOT DONE: parked after a CMA "
            f"validation hold (no hash recorded, not re-translated while the source is "
            f"unchanged). Fix the field or source in Contentful, then use RETRY."
        )
    blocked = _blocked(ctx)
    if blocked:
        bsample = "; ".join(f"{f.get('article_id')}/{f.get('locale')}" for f in blocked[:3])
        parts.append(
            f"BLOCKED {len(blocked)} pair(s) on CMA validation (nothing written; "
            f"deterministic, so a plain re-run SKIPS them - the source hash is "
            f"unchanged). After fixing the field/source in Contentful, use RETRY "
            f"(the blocked list on the connection page, or `connector retry`) to "
            f"re-attempt them: {bsample}."
        )
    sel = ctx.connector._selection_summary()
    if sel:
        parts.append(sel)
    real = _real_failures(ctx)
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("entry write(s)").strip())
        log.warning(
            "contentful_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _blocked_pairs(ctx):
    """Validation-blocked pairs as operator-visible dicts (article_id, locale, error).
    Uses `error` (not `why`) so the shape matches failures/incomplete and survives
    _clean_failures on the retry path (review round 3 finding 8)."""
    return [
        {"article_id": f.get("article_id"), "locale": f.get("locale"), "error": f.get("error")}
        for f in _blocked(ctx)
    ]


def _persist_blocks(ctx) -> None:
    """DURABLE no-re-spend PARK marker (review F1a + round-3 findings 3/6, reworked for
    #665: 2026-10-10, nothing is marked done until every field is written). In ONE
    atomic config write:
      * for each validation-blocked pair (nothing written) AND each pair that was written
        with a field held back by a CMA validation, record the pair -> REASON string in
        `contentful_blocked` (rendered by the connection page and the CLI) and the pair ->
        source hash in `contentful_blocked_src`. A scheduled run then skips a parked pair
        WITHOUT materialize/pretranslate while the source is unchanged (flow `parked`
        hook), so the deterministic failure does not re-spend MT on every run. NO entry is
        added to `contentful_hashes`: the hash means "done", and a parked pair is not done.
        A source edit (hash change) or an explicit retry re-attempts it;
      * CLEAR the park for any pair that WROTE COMPLETELY this run (finding 3), so a later
        delete-in-Contentful self-heals. Both maps are bounded by CURRENTLY-parked pairs.
    One patch_config_nested call closes the two-write crash window (finding 6). Called
    from build_result (stage 11), AFTER the flow's own hash merge."""
    from app.services.connectors import store

    conn = ctx.connector
    chash_by = getattr(conn, "_chash_by_aid", {}) or {}
    block_add: dict = {}
    src_add: dict = {}

    def _park(aid, loc, reason: str) -> None:
        ch = chash_by.get(aid)
        if aid is None or loc is None or not ch:
            return
        block_add[f"{aid}:{loc}"] = reason[:300]
        src_add[f"{aid}:{loc}"] = ch

    reasons_by_pair = getattr(conn, "_blocked", {}) or {}
    for f in _blocked(ctx):
        aid, loc = f.get("article_id"), f.get("locale")
        reasons = reasons_by_pair.get((str(aid), str(loc))) or {}
        _park(
            aid,
            loc,
            "; ".join(f"{k}: {v}" for k, v in sorted(reasons.items()))
            or str(f.get("error") or "validation block"),
        )
    # A pair written with ONLY CMA-validation holds (#665): the held field fails again for
    # the same source, so park it too. A pair that also has a review / placeholder hold is
    # not parked (that hold clears on its own and is retried every run, #640).
    failed = {(str(f.get("article_id")), str(f.get("locale"))) for f in ctx.failures}
    incomplete = getattr(conn, "_incomplete", {}) or {}
    for (aid, loc), vheld in (getattr(conn, "_vheld", {}) or {}).items():
        if (aid, loc) in failed or not set(incomplete.get((aid, loc)) or ()) <= set(vheld):
            continue
        _park(aid, loc, "; ".join(f"{k}: {v}" for k, v in sorted(vheld.items())))
    # Pairs that wrote COMPLETELY this run (created/updated/unchanged all land a hash delta)
    # get their park cleared. Removing a never-parked pair is a harmless no-op pop.
    remove = [k for k in ctx.hash_delta if k not in block_add]
    changes: dict = {}
    if block_add or remove:
        changes["contentful_blocked"] = {"add": block_add, "remove": remove}
        changes["contentful_blocked_src"] = {"add": src_add, "remove": remove}
    # A pair written INCOMPLETE this run has its older stored hash dropped by the shared flow
    # (flow.py stage 9, #666), so a source revert to exactly that text is not taken as done.
    if changes:
        store.patch_config_nested(ctx.cid, changes)


def _ctf_build_result(ctx):
    _persist_blocks(ctx)
    return {
        "entries": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(_held_back(ctx)),
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "incomplete": _confirmed_incomplete(ctx),
        "blocked": _blocked_pairs(ctx),
        "parked": ctx.counters["parked_skipped"],
        "fields_selected": sorted(ctx.connector._sel_selected),
        "fields_skipped": dict(ctx.connector._sel_skipped),
        "failures": _real_failures(ctx),
    }


def contentful_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Contentful entries into one or more locales, each written back as a
    new locale key on the SAME entry (fields.{id}.{locale}) via the T1
    ContentfulEntryStrategy (version-locked read-merge-write; only the translated delta
    is applied, so the source locale, other locales and non-translated fields are never
    overwritten). Thin wrapper over flow.run_translate_flow; poll-only with
    content-hash suppression + empty-entry skip. only_pairs restricts the write loop to
    exactly those (entry_id, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="contentful",
        no_conn_msg="No such Contentful connection.",
        hash_key="contentful_hashes",
        probe_label="Contentful",
        preflight=_ctf_preflight,
        prepare=_ctf_prepare,
        is_suppressed=_ctf_is_suppressed,
        build_payload=_ctf_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_ctf_compute_ok,
        build_message=_ctf_build_message,
        build_result=_ctf_build_result,
        held_prefix=_HELD_BACK_PREFIX,
        report_held_updates=False,
        extra_held=_ctf_extra_held,
        write_incomplete=_ctf_write_incomplete,
        extra_counters=("empty_skipped", "parked_skipped"),
        parked=_ctf_parked,
        partition=_ctf_partition,
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
