"""salesforce: shared leaf helpers (layered pure move)

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re

from app.services.connectors import flow, harvest

log = logging.getLogger("loc_tms.connectors")


API_VERSION = "v65.0"


_LOGIN_SANDBOX = "https://test.salesforce.com"


_LOGIN_PROD = "https://login.salesforce.com"


_JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")


# pinned; assignments[] needs v43+, Knowledge__kav v39+ (research §7.9)
# Login hosts. Sandbox default for the FIRST build (spec guardrail) — a prod org
# uses login.salesforce.com or the org's My Domain.
# A Salesforce record Id is 15 or 18 chars, [a-zA-Z0-9]. Validated before it is
# interpolated into SOQL (the ids come from Salesforce's own responses, so this is
# defense-in-depth against a malformed value reaching a query, not the primary
# guard — string literals are also single-quote-escaped by _soql_lit).
# SOQL cursor from a query response: instance-relative data path only (no host, no
# traversal) so a hostile body cannot steer an authenticated follow-up call.
_NEXT_URL_RE = re.compile(r"^/services/data/v\d{2}\.\d/query/[A-Za-z0-9\-]+$")


def _err_code(r) -> str:
    """A short, safe error summary from a Salesforce error response: the errorCode
    values only (never the free-text message, which can echo query text)."""
    try:
        body = r.json()
    except Exception:
        return "error"
    rows = body if isinstance(body, list) else [body]
    codes = [
        str(x.get("errorCode") or x.get("error") or "")[:60] for x in rows if isinstance(x, dict)
    ]
    return ",".join(c for c in codes if c)[:120] or "error"


def _soql_lit(v: str) -> str:
    """Escape a value for a SOQL single-quoted string literal (backslash + single
    quote per the SOQL grammar)."""
    return str(v).replace("\\", "\\\\").replace("'", "\\'")


# ===========================================================================
# Translate flow moved here from store.py (Wave 3 store.py decomposition).
# The per-connector hook bundle + salesforce_translate wrapper live with the
# connector; store.py keeps a thin delegator so store.salesforce_translate stays
# importable/monkeypatchable (jobs/CLI/webhook dispatch + verify suites).
# ===========================================================================
# ------------------------- Salesforce Knowledge hooks ----------------------
# T2 (Knowledge__kav version per language, shared KnowledgeArticleId) — the
# FIELD-MAPPING GENERALIZER build (SPEC-salesforce-knowledge.md). Translatable
# fields VARY per record type and are discovered via Describe (fieldmap.py), so
# prepare/build_payload route through the article's Extraction rather than a fixed
# title+body. Locale rejection is the org's ACTIVE Knowledge languages (the
# MANDATORY explicit map — an inactive/unmapped locale is rejected, never guessed).
# Poll-first, so NO watermark (LastModifiedDate is metadata-tainted — the content
# hash is the gate).
def _sf_preflight(ctx):
    # enabled = the target locales that resolve to an ACTIVE Salesforce Knowledge
    # language (an unmapped/inactive locale is rejected by the default partition).
    # The connector is threaded through `extra` so _sf_partition can resolve each
    # usable locale's SF language code for the fold-dedup below (FINDING 1).
    enabled = {l for l in ctx.locales if ctx.connector.sf_lang_for(l)}  # noqa: E741
    return enabled, {"connector": ctx.connector}


def _sf_partition(locales, enabled, extra):
    # FINDING 1 (adversarial review, LOW): sf_lang_for's exact-then-base-strip
    # resolution folds MANY internal locales onto ONE Salesforce language code
    # (e.g. both fr-FR and fr -> 'fr'; both de-DE and de -> 'de'). Without a dedup
    # step, two 'usable' locales would write the SAME translation version
    # sequentially -- last-writer-wins clobber + a redundant paid MT run, while
    # BOTH record a success hash (the second write silently loses the first).
    #
    # First reproduce the default partition (reject anything not in `enabled`),
    # then fold usable locales that resolve to the SAME SF language code onto ONE
    # writer -- generalized from the Intercom _ic_partition D1 precedent (same
    # many-to-one alias-fold shape, different key space). Keep the locale whose
    # own canonical form IS the SF code (an exact match beats a base-stripped
    # locale); reject the collider with a clear reason that surfaces in the run
    # message like any other rejection.
    connector = extra.get("connector")
    rejected, usable = [], []
    for l in locales:  # noqa: E741
        if l in enabled:
            usable.append(l)
        else:
            rejected.append(l)

    def _sf_code(loc):
        return connector.sf_lang_for(loc) if connector is not None else None

    def _is_canonical(loc, sf_code):
        return str(loc).strip().replace("-", "_").lower() == str(sf_code).strip().lower()

    winner: dict = {}  # SF language code -> the winning locale from `usable`
    for l in usable:  # noqa: E741
        code = _sf_code(l)
        if not code:
            continue  # defense-in-depth: `enabled` already filtered these out
        cur = winner.get(code)
        if cur is None or (_is_canonical(l, code) and not _is_canonical(cur, code)):
            winner[code] = l
    deduped = []
    for l in usable:  # noqa: E741
        code = _sf_code(l)
        if not code or winner.get(code) is l:
            deduped.append(l)
        else:
            rejected.append(
                f"{l} collides with {winner[code]} on Salesforce language code '{code}'"
            )
    return rejected, deduped


def _sf_prepare(ctx, art):
    from app.filters import get_filter
    from app.services.connectors.fieldmap import extract_record

    aid = str(art.get("KnowledgeArticleId") or "")
    specs = ctx.connector._fields_for(art.get("RecordTypeId"))
    ex = extract_record(art, specs, get_filter)
    if ex.is_empty:
        # No translatable content at all -> skip without a write (never write an
        # empty/guessed translation version).
        return flow.SkipArticle("empty_skipped")
    raw_title = art.get("Title") or ""
    # Context harvest (Phase 3B): resolve RecordTypeId -> RecordType NAME -> content_kind,
    # gated on harvest_context so a disabled connection adds NO describe call. Cached on
    # the connector instance, so the describe happens ONCE per pull, never per-article.
    # Kept OFF extra_skeleton (persisted-shape parity); harvest.build_flow merges it in.
    content_kind = None
    if harvest.harvest_enabled(ctx.cfg):
        content_kind = ctx.connector.record_type_name(art.get("RecordTypeId"))
    return flow.ArticlePrep(
        aid=aid,
        chash=ex.content_hash,
        title_text=ex.title_text,
        body_units=ex.body_units,
        extra_skeleton={
            "article_id": aid,
            "record_type_id": str(art.get("RecordTypeId") or ""),
            "title": raw_title,
        },
        doc_title=f"{(raw_title or 'article')[:50]} ({aid})",
        extra={"extraction": ex},
        harvest={"content_kind": content_kind},
    )


def _sf_is_suppressed(connector, aid, loc):
    from app.services.connectors.strategy import ABSENT

    # No confirmed per-locale stale flag for Knowledge__kav: existence + unchanged
    # hash IS the gate (the freshdesk pattern). A MISSING language is always written.
    #
    # FINDING 2 (adversarial review, LOW-MED): a naive existence-only check leaves a
    # draft-then-publish run stuck forever. Run 1 (publish=0) creates a Draft and
    # records the content hash. If the operator later flips publish=1 and re-runs
    # with UNCHANGED source, existence + matching hash would suppress the pair
    # before the publish managed-action ever fires -- the article sits in Draft
    # forever (the only workaround being to edit the source or clear the hash).
    #
    # Fix: in PUBLISH mode, only suppress when the existing translation VERSION is
    # already 'Online'. A Draft (or any other non-Online status) with a matching
    # hash must NOT suppress -- the run falls through to stage 8/9, reuses the
    # Draft (SalesforceConnector._ensure_draft_version), and completes the
    # lifecycle (set-complete if needed, then publish). In DRAFT mode (publish=0)
    # existence alone is still the gate, unchanged from before.
    #
    # COST NOTE: un-suppressing the pair puts it back in stage-8's `pending` list,
    # and the shared engine (flow.py, frozen) always re-materializes a fresh
    # Document/Segments and re-runs pretranslate_document for every pending
    # (article, locale) -- there is no hash-aware MT-skip path reachable from here
    # without touching flow.py. So the publish-flip pays a ONE-TIME re-translation
    # cost even though the source text is unchanged (WriteBudget/spend ceilings
    # still bound it like any other write). This is accepted as correct, not
    # contorted around: reaching Online is the point of the fix, the hash still
    # prevents re-translation on every SUBSEQUENT unchanged run once the version is
    # Online, and the field PATCH itself (_lifecycle's step 2) is a single cheap
    # write regardless.
    existing = connector.get_translation(aid, loc)
    if existing is ABSENT:
        return False
    publish_mode = str((connector.config or {}).get("publish", "0")).strip() in ("1", "true", "yes")
    if publish_mode:
        return str(existing.get("PublishStatus") or "") == "Online"
    return True


def _sf_build_payload(prep, loc, seg0_target, mapping):
    # The Extraction rebuilds the per-FIELD translated payload {FieldName: value}
    # (title from seg0_target; html fields via the html filter; NEEDS_REVIEW/empty
    # plain fields omitted). The strategy adds the draft flag before write.
    #
    # Review gate 2026-10-10 (#640): a held unit must never put SOURCE text on the wire, and a
    # pair with a held unit must never be completed or published. omit_held drops a held
    # title / any html field with a held sub-unit (the field keeps its stored value), and
    # HELD_MARKER tells _lifecycle to stop at the field PATCH. An all-held pair yields no
    # fields at all, which _lifecycle refuses before any API call.
    ex = prep.extra["extraction"]
    payload = ex.build_payload(seg0_target, mapping, omit_held=True)
    held = (seg0_target is None and (ex.title_text or "").strip()) or any(
        str(u.p_index) not in mapping and (u.text or "").strip() for u in ex.body_units
    )
    if held:
        payload[HELD_MARKER] = True
    return payload


# Not a valid Salesforce field API name (they cannot start with "_"), so it can never
# collide with a real field; _lifecycle pops it before any request.
HELD_MARKER = "_held_back"


def _sf_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not ctx.failures)
        or (not ctx.failures and (ctx.created + ctx.updated + ctx.skipped) == 0 and ctx.usable)
    )


def _sf_build_result(ctx):
    return {
        "articles": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "failures": ctx.failures,
    }


def salesforce_rediscover_fields(
    cid: int, actor: str = "", transport=None
) -> tuple[bool, str, dict]:
    """Describe Knowledge__kav and persist the per-record-type translatable
    field map (the "rediscover fields" action — spec registration item 6/7). Stored
    under Connection.config['sf_field_map'] (patch_config, so it is refreshed on
    demand, NOT on every run). Web + CLI + /api all call this one seam."""
    from app.models import ConnStatus
    from app.services.connectors.store import _log, _set_status, build_fresh, get, patch_config

    conn = get(cid)
    if not conn or conn.kind != "salesforce":
        return False, "No such Salesforce connection.", {}
    try:
        connector = build_fresh(cid, transport)
        fmap = connector.discover_field_map()
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _set_status(cid, ConnStatus.ERROR, msg, tested=True)
        _log(cid, "rediscover", False, 0, msg, actor)
        return False, msg, {}
    patch_config(cid, {"sf_field_map": fmap})
    n_types = len([k for k in fmap if k])
    n_fields = len(fmap.get("") or [])
    msg = f"Discovered {n_fields} translatable field(s) across {n_types or 1} record type(s)."
    _set_status(cid, ConnStatus.CONNECTED, msg, tested=True)
    _log(cid, "rediscover", True, n_fields, msg, actor)
    return True, msg, {"record_types": n_types, "fields": n_fields, "field_map": fmap}
