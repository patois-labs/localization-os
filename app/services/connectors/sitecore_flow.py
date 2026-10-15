"""sitecore: translate-flow hooks over the shared engine (flow.run_translate_flow).

Poll-only (no watermark): the content-hash gate is the change detector. Everything vendor
specific (Authoring GraphQL, language versions, shared-field guard) lives in
`sitecore.SitecoreConnector`; this module is only the thin hook layer plus the held-back
bookkeeping, kept apart so neither file grows past the repo's size habit.

HELD-BACK RULES (defect taxonomy B/F, #640): a unit that is NEEDS_REVIEW or has no target text
is OMITTED from the write payload. A Rich Text field is all-or-nothing: if any one of its
sub-units is held, the whole field is left out (a half-translated HTML body would be written
over a stored translation with source sentences in it). The title is never source-seeded.
The shared flow refuses to CREATE a language version while a unit is held and records no
content hash for a pair that was written with a unit held.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import re

from app.services.connectors import flow

# A field name travels to the strategy under this prefix so a Sitecore field literally named
# "draft" can never collide with the strategy-injected `draft` flag (Strapi review M2).
FIELD_PREFIX = "\x00field:"

HELD_BACK_PREFIX = "held-back:"
HELD_BACK = (
    HELD_BACK_PREFIX + " every translatable field is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing language version (if any) kept"
)

# Item ids are Sitecore GUIDs. Anything else is refused before a URL or query is built
# (taxonomy A). Canonical form: 32 upper-case hex digits, no hyphens or braces, which is how
# the Authoring API itself returns an item id.
_GUID_RE = re.compile(
    r"\{?([0-9A-Fa-f]{8})-?([0-9A-Fa-f]{4})-?([0-9A-Fa-f]{4})-?([0-9A-Fa-f]{4})-?"
    r"([0-9A-Fa-f]{12})\}?"
)


def canonical_guid(raw) -> str | None:
    """The canonical item GUID for `raw`, or None when it is not exactly one GUID.
    fullmatch on the untouched string: no strip, so a trailing space, newline, NUL, '?',
    '#', '..', a path segment or a second id all fail."""
    if not isinstance(raw, str):
        return None
    m = _GUID_RE.fullmatch(raw)
    if not m or raw.startswith("{") != raw.endswith("}"):
        return None  # braces must come as a pair
    return "".join(m.groups()).upper()


def parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------
def _preflight(ctx):
    c = ctx.connector
    enabled = set(c.enabled_locales())
    c.root()  # resolve + scope the content root here so a bad root is one clean failure
    return enabled, {"source": c.source_locale().lower()}


def _partition(locales, enabled, extra):
    """usable = enabled targets minus the source language; rejected = not a Sitecore
    language. The source language is DROPPED (never written as a target) and reported."""
    src = (extra or {}).get("source", "")
    rejected = [loc for loc in locales if loc not in enabled]
    usable = [loc for loc in locales if loc in enabled and loc != src]
    return rejected, usable


def _prepare(ctx, art):
    ex = ctx.connector.extract(art)
    if ex.is_empty:
        return flow.SkipArticle("empty_skipped")
    aid = art["id"]
    disp = ex.title_text.strip() or str(art.get("name") or "")
    return flow.ArticlePrep(
        aid=aid,
        chash=ex.content_hash,
        title_text=ex.title_text,
        body_units=ex.body_units,
        extra_skeleton={"article_id": aid, "title": disp or None, "path": art.get("path")},
        doc_title=f"{(disp or 'item')[:50]} ({aid})",
        extra={"ex": ex},
    )


def _is_suppressed(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _build_payload(prep, loc, seg0_target, mapping):
    """The write payload for one language. Only translated fields are emitted, so a held or
    empty one keeps whatever the language version already stores."""
    ex = prep.extra["ex"]
    out: dict = {}
    if ex.title_field is not None and seg0_target is not None:
        out[FIELD_PREFIX + ex.title_field] = seg0_target
    for name, st in ex._fields.items():
        if st["type"] == "html":
            subs = st["subs"]
            if not subs or not all(pidx in mapping for pidx, _orig in subs):
                continue  # all-or-nothing: a partly held Rich Text keeps its stored value
            sub = {str(orig): mapping[pidx] for pidx, orig in subs}
            out[FIELD_PREFIX + name] = st["filter"].build(st["bytes"], sub).decode("utf-8")
        elif st["pindex"] in mapping:
            out[FIELD_PREFIX + name] = mapping[st["pindex"]]
    return out


def _dropped_as_source(ctx):
    return [loc for loc in ctx.locales if loc not in ctx.usable and loc not in ctx.rejected]


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(HELD_BACK_PREFIX)


def held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]


def _compute_ok(ctx):
    if not ctx.usable:
        return False
    real = real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0)
    )


def _build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} language version(s) across "
        f"{len(ctx.usable)} language(s) from {len(ctx.articles)} item(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) language(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} item(s) with no translatable fields."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} language(s) not found in Sitecore: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source language "
            f"({', '.join(dropped)}); configure different target languages."
        )
    held = held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (item, language) pair(s) awaiting review "
            f"(NEEDS_REVIEW): a new language version is not created while any of its fields "
            f"is held, and an existing one keeps the held field's stored value. "
            f"They will be re-attempted next run."
        )
    sel = ctx.connector.selection_summary()
    if sel:
        parts.append(sel)
    real = real_failures(ctx)
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("item write(s)").strip())
    return " ".join(parts)


def _build_result(ctx):
    c = ctx.connector
    return {
        "items": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(held_back(ctx)),
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "fields_selected": c.sel.selected_sorted(),
        "fields_skipped": c.sel.skipped_dict(),
        "failures": real_failures(ctx),
    }


def sitecore_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Sitecore XM Cloud items into one or more languages, each written back as a
    language version of the same item through the T2 SubResourceStrategy (addItemVersion
    then updateItem). Thin wrapper over flow.run_translate_flow; poll-only with content-hash
    suppression. only_pairs restricts the write loop to exactly those (item id, language)
    pairs."""
    spec = flow.TranslateFlowSpec(
        kind="sitecore",
        no_conn_msg="No such Sitecore connection.",
        hash_key="sitecore_hashes",
        probe_label="Sitecore",
        preflight=_preflight,
        prepare=_prepare,
        is_suppressed=_is_suppressed,
        build_payload=_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_compute_ok,
        build_message=_build_message,
        build_result=_build_result,
        extra_counters=("empty_skipped",),
        partition=_partition,
        held_prefix=HELD_BACK_PREFIX,
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
