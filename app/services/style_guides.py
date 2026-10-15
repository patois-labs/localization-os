"""The Style Guide service seam (SPEC-workflows-redesign Track B, B2).

ONE module — called by the /style-guides web tab, the `style-guide` CLI group, and the
/api endpoints alike (the dual-surface parity rule) — that owns:

  * StyleGuide CRUD + the DRAFT/APPROVED approval lifecycle (mirroring
    glossary_module: a body/scope edit reverts an approved guide to draft; a rename
    does not);
  * scope RESOLUTION at the customer x content_type "workhorse" grain, optionally
    per-pair (`resolve_style_guide`), most-specific-wins in the context_pkg spirit;
  * safe RENDER of the one fenced, sanitized, budget-capped `<style_guide>` prompt
    block (`render_style_guide`) — the same layered-defense idioms context_pkg uses.

WHY BUILD ON THE CONTEXT CHASSIS (context_pkg): style guides are a scoped, layered
"standing asset" injected as a capped, injection-fenced prompt block — exactly what
context_pkg already does for TranslationContext. This module reuses its idioms rather
than reinventing them: a `<style_guide source="..." trust="data-only">` fence (like
`render_block`'s `<translation_context ...>`), values made inert at RENDER, an OWN
budget cap, and "" when absent so a run with no guide is BYTE-IDENTICAL to today.

THE ASSET-BODY CONTRAST (documented invariant, mirrored from models.StyleGuide): the
glossary module deliberately keeps term METADATA out of the prompt (it is for humans).
A style guide is the opposite — its `body` IS the asset and MUST reach the model. So
the body is rendered in, but sanitized (`_san_body`: control chars flattened, angle
brackets neutralized) so a hostile body can never forge the closing `</style_guide>`
tag and break out of the data-only fence. Reaching the prompt is the point; forging
prompt STRUCTURE is what is prevented.

Only APPROVED guides ever resolve. The archived-customer refusal lives at THIS service
seam (create/assign) — the glossary lesson: a forged /api POST or a stray CLI id is
refused here, not merely omitted from a web picker.

Documented in: docs/architecture/style-guides.md
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.models import Customer, StyleGuide, StyleGuideStatus
from app.services import context_pkg as _cp
from app.services import ids, languages_catalog, scaffold


def _utcnow() -> datetime:
    return datetime.utcnow()


class StyleGuideError(ValueError):
    """A style-guide operation was invalid (blank name, unknown guide, an archived
    customer, a bad locale/content-type). Surfaced by the web route / CLI / API as a
    clean error, never a 500 (mirrors GlossaryModuleError / ContentTypeError)."""


# --- render caps + fencing ---------------------------------------------------
# A guide is LONG-FORM, so it gets its OWN, larger budget than the context BLOCK_CAP
# (600) — a real guide would never fit that. Head-first truncation with a visible
# marker keeps the most important guidance (authors put the headline rules first) and
# never silently drops the whole thing.
BODY_CAP = 2000  # rendered <style_guide> body ceiling (chars)
_TRUNC_MARK = "\n[... style guide truncated at %d chars ...]"

# Built from the SHARED scaffold tokens (app.services.scaffold) so the fence and the
# scaffold-echo gate that detects it can never drift. Byte-identical to the prior literal
# `<style_guide source="%s" trust="data-only">` / `</style_guide>`.
_HEAD = f'<{scaffold.STYLE_GUIDE_TAG} source="%s" {scaffold.TRUST_ATTR}>'
_TAIL = f"</{scaffold.STYLE_GUIDE_TAG}>"


def _san_body(text, cap: int = BODY_CAP) -> str:
    """Sanitize a long-form guide body for injection — the SAME layered defense the
    context chassis uses (`context_pkg._san`), but PRESERVING newlines/tabs because a
    style guide is genuinely multi-line prose (context fields are short, so `_san`
    flattens their newlines; a guide must not be flattened into one line).

    THE FENCE BREAK-OUT DEFENSE (mirrors context_pkg._san's HIGH-1 rationale): the body
    is dropped into a `<style_guide>...</style_guide>` fence as readable text (NOT
    json.dumps'd — that would escape every newline and make a multi-line guide
    unreadable to the model). So the load-bearing defense is neutralizing `<`/`>` to
    `&lt;`/`&gt;`: with no raw angle bracket surviving, a body can never forge the
    literal `</style_guide>` closing tag and let text escape the data-only fence.
    Additionally every C0 control char EXCEPT newline/tab is stripped (so no NUL / ESC /
    backspace trickery), and CRLF is normalized to LF. Head-first cap with a visible
    truncation marker (guides put headline rules first)."""
    s = str(text if text is not None else "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Strip C0 control chars except \n (0x0a) and \t (0x09); keep printable + newlines.
    s = "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 0x20)
    s = s.replace("<", "&lt;").replace(">", "&gt;").strip()
    if len(s) > cap:
        mark = _TRUNC_MARK % cap
        s = s[: max(0, cap - len(mark))].rstrip() + mark
    return s


def render_style_guide(guide, formality=None) -> str:
    """Render ONE fenced, sanitized, budget-capped `<style_guide>` block for a resolved
    guide, or "" when the guide is None / has a blank body AND no formality applies.

    Mirrors context_pkg.render_block's idioms: an XML-ish `trust="data-only"` fence with
    a short provenance attribute, the value made inert (here via `_san_body`), and ""
    when there is nothing to render — so a run WITHOUT a resolvable guide produces a
    BYTE-IDENTICAL prompt to one built before style guides existed (the sacred gate).

    The `source="..."` attribute carries the guide's own name (sanitized to a clean bare
    token by `context_pkg._attr_safe`) so the model sees WHICH guide it was handed;
    falls back to "style-guide" when the name sanitizes to nothing.

    `formality` (#617 item 7, ADR 0062): the resolved `formality.Resolved` register for the
    run's target language, or None. When it carries an instruction, ONE sentence is appended
    INSIDE the fence after the body (so the body cap never truncates it); with no guide, a
    block with `source="formality"` holds just that sentence. The sentence is code text, never
    user input. None / `auto` -> not a byte changes."""
    note = ""
    if formality is not None:
        note = (getattr(formality, "instruction", "") or "").strip()
    body = _san_body(getattr(guide, "body", "") or "") if guide is not None else ""
    if not body and not note:
        return ""
    if not body:
        return (_HEAD % "formality") + "\n" + note + "\n" + _TAIL
    attr = _cp._attr_safe(getattr(guide, "name", "") or "") or "style-guide"
    if note:
        body = body + "\n\n" + note
    return (_HEAD % attr) + "\n" + body + "\n" + _TAIL


def style_guide_prefix(guide, formality=None) -> str:
    """The `<style_guide>` block as a PROMPT PREFIX: the rendered block plus the "\\n\\n"
    separator that seats it near the top of a step prompt (next to {context}), or ""
    when absent. Centralizes the separator convention so the `{style_guide}` substitution
    renders `<block>\\n\\n` when present and NOTHING (no stray blank line) when absent —
    the byte-stability contract for guide-less runs, exactly mirroring
    context_pkg.render_prefix."""
    block = render_style_guide(guide, formality)
    return block + "\n\n" if block else ""


# --- locale canonicalization (mirrors glossary_module._canon/_norm) ----------
def _norm(loc) -> str:
    return languages_catalog.normalize(loc) or ""


def _canon(loc) -> str:
    """Catalog-canonical display form (es_ES -> es-ES) for STORING a locale, or ""
    for a blank/unset side (a universal guide). A NON-blank unknown code RAISES (never
    coerced to a plausible fake), mirroring glossary_module._canon."""
    s = (loc or "").strip()
    if not s:
        return ""
    valid, _ = languages_catalog.validate_codes([s])
    if valid:
        return valid[0]
    raise StyleGuideError(f"{s!r} is not a known locale code (see the language catalog).")


def _pair_of(source_locale, target_locale) -> tuple[str, str]:
    """Canonicalize a submitted (source, target). BOTH-or-NEITHER: a half-pair is
    meaningless for resolution, so if exactly one side is given the other is required.
    Returns ("", "") for a fully-universal guide."""
    src, tgt = _canon(source_locale), _canon(target_locale)
    if bool(src) != bool(tgt):
        raise StyleGuideError(
            "a per-pair style guide needs BOTH a source and a target locale "
            "(leave both blank for a universal guide)."
        )
    return src, tgt


def _reject_archived_customer(session: Session, customer_id) -> None:
    """A style guide can never be created for / assigned to an ARCHIVED customer
    (SPEC-customer-archive). Checked at the SERVICE seam — mirroring
    glossary_module._reject_archived_customer — so a forged /api POST or a stray CLI id
    is refused, not merely omitted from the web picker. No-op when blank/None; a
    nonexistent id falls through to the caller's own scope check / the FK path."""
    cid = ids.coerce_id(customer_id)
    if cid is None:
        return
    cust = session.get(Customer, cid)
    if cust is not None and getattr(cust, "archived_at", None) is not None:
        raise StyleGuideError(
            f"customer {cid} is archived and cannot own a style guide "
            "(unarchive it first, or pick an active customer)."
        )


def _resolve_content_type(session, content_type_id, org_id, customer_id):
    """Validate a submitted content_type_id against an (org, customer) scope, or None.

    Blank/None -> None (applies to every content type). A non-blank id must be
    ATTACHABLE to a guide of this org+customer (GLOBAL or the guide's own customer) —
    else StyleGuideError. The single validation seam shared by create() +
    set_scope(), mirroring glossary_module._resolve_content_type."""
    if content_type_id is None or str(content_type_id).strip() == "":
        return None
    from app.services import content_types as _ct

    ct = _ct.attachable(session, content_type_id, org_id=org_id, customer_id=customer_id)
    if ct is None:
        raise StyleGuideError(
            f"content type {content_type_id} is not available for this style guide "
            "(unknown, or scoped to a different customer/org)."
        )
    return ct.id


# --- CRUD --------------------------------------------------------------------
def create(
    session: Session,
    name: str,
    *,
    org_id=None,
    customer_id=None,
    content_type_id=None,
    source_locale="",
    target_locale="",
    body="",
    created_by: str = "",
) -> StyleGuide:
    """Create a DRAFT style guide. Raises on a blank name.

    customer_id blank/None = an ORG-GLOBAL guide (allowed — unlike the customer-first
    glossary policy; a style guide legitimately applies org-wide). An ARCHIVED customer
    is refused at this seam. content_type_id / (source_locale, target_locale) are the
    optional scope narrowing (both-or-neither for the pair)."""
    name = (name or "").strip()
    if not name:
        raise StyleGuideError("style guide name is required.")
    cust_id = ids.coerce_id(customer_id)
    _reject_archived_customer(session, cust_id)
    # #73: attachable now fails closed on a bare None org_id under an active gate, so the
    # CLI --operator create door threads access.UNSCOPED (op_org_scope) here -- resolution
    # keeps the raw scope (UNSCOPED sweeps), the row is STAMPED via org_scope_value (which
    # collapses UNSCOPED->None, identity on a real id / -1 / None) so the persisted org is
    # byte-identical.
    from app.services import access

    ct_id = _resolve_content_type(session, content_type_id, org_id, cust_id)
    src, tgt = _pair_of(source_locale, target_locale)
    now = _utcnow()
    g = StyleGuide(
        name=name,
        status=StyleGuideStatus.DRAFT,
        org_id=access.org_scope_value(org_id),
        customer_id=cust_id,
        content_type_id=ct_id,
        source_locale=(src or None),
        target_locale=(tgt or None),
        body=body or "",
        created_by=created_by or "",
        created_at=now,
        updated_at=now,
    )
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def get(session: Session, guide_id: int) -> StyleGuide | None:
    return session.get(StyleGuide, guide_id)


def _require(session: Session, guide_id: int) -> StyleGuide:
    g = session.get(StyleGuide, guide_id)
    if g is None:
        raise StyleGuideError(f"style guide {guide_id} not found.")
    return g


def visible(session: Session, guide_id: int, *, org_id=None, customer_id=None) -> StyleGuide | None:
    """Fetch a guide only if visible to the caller's org + customer scope (else None ->
    the route 404s). The single read-authz seam. A customer-bound caller sees its own
    customer's guides AND org-global (customer_id NULL) ones — an org-global guide
    legitimately applies to that customer, mirroring options_for_step's confinement.

    #73: a bare None org_id under an ACTIVE gate resolves NOTHING (returns None), not
    any org's guide by id; access.UNSCOPED (the CLI --operator door) keeps the
    cross-org read; gate off is byte-identical."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    g = session.get(StyleGuide, guide_id)
    if g is None:
        return None
    if org_id is not None and getattr(g, "org_id", None) != org_id:
        return None
    if (
        customer_id is not None
        and getattr(g, "customer_id", None) is not None
        and getattr(g, "customer_id", None) != customer_id
    ):
        return None
    return g


def list_guides(session: Session, *, org_id=None, customer_id=None) -> list[StyleGuide]:
    """Style guides, newest first, org-scoped (+ R2-3 customer-confined for a bound
    caller: its own customer's guides plus org-global ones).

    #73 (fail-closed, mirrors the module read family): a bare None org_id under an
    ACTIVE gate lists NOTHING; access.UNSCOPED (the CLI --operator door) still sweeps;
    gate off byte-identical (None = unrestricted)."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(StyleGuide)
    if org_id is not None:
        stmt = stmt.where(StyleGuide.org_id == org_id)
    if customer_id is not None:
        stmt = stmt.where(
            (StyleGuide.customer_id == customer_id) | (StyleGuide.customer_id.is_(None))
        )
    return list(
        session.exec(stmt.order_by(StyleGuide.created_at.desc(), StyleGuide.id.desc())).all()
    )


def rename(session: Session, guide_id: int, name: str) -> StyleGuide:
    """Rename a guide (metadata; does NOT revert approval — mirrors glossary rename)."""
    g = _require(session, guide_id)
    name = (name or "").strip()
    if not name:
        raise StyleGuideError("style guide name is required.")
    g.name = name
    g.updated_at = _utcnow()
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def set_body(session: Session, guide_id: int, body: str) -> StyleGuide:
    """Replace a guide's long-form body. A CONTENT edit -> reverts approval to draft
    (an approved guide that changes must be re-blessed before it resolves again — the
    same safest-state semantics as a glossary term edit)."""
    g = _require(session, guide_id)
    g.body = body or ""
    g.updated_at = _utcnow()
    _to_draft(g)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def set_scope(
    session: Session,
    guide_id: int,
    *,
    customer_id=None,
    content_type_id=None,
    source_locale=None,
    target_locale=None,
    change_customer=False,
    change_content_type=False,
    change_pair=False,
) -> StyleGuide:
    """Edit a guide's scope (customer / content type / language pair). A SCOPE edit ->
    reverts approval to draft (the resolved audience changed, so the sign-off no longer
    applies — stricter than the glossary's set_content_type, which treats scope as
    metadata; a style guide's scope decides WHO gets a prose asset that verification
    (B3) then holds the output to, so a re-bless is the safe default).

    Only the flags set to True are changed (so a caller can edit one axis without
    clobbering the others). customer_id, when changed, is validated (archived refused,
    content type re-validated against the new owner)."""
    g = _require(session, guide_id)
    if change_customer:
        cid = ids.coerce_id(customer_id)
        _reject_archived_customer(session, cid)
        g.customer_id = cid
        # a customer change can invalidate the current content type's attachability
        g.content_type_id = _resolve_content_type(session, g.content_type_id, g.org_id, cid)
    if change_content_type:
        g.content_type_id = _resolve_content_type(session, content_type_id, g.org_id, g.customer_id)
    if change_pair:
        src, tgt = _pair_of(source_locale, target_locale)
        g.source_locale, g.target_locale = (src or None), (tgt or None)
    g.updated_at = _utcnow()
    _to_draft(g)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def delete(session: Session, guide_id: int) -> None:
    """Delete a guide. Nothing references a guide BY ID at run time (resolution is by
    SCOPE, not a stored FK — a workflow step never pins a guide), so there is no
    in-use guard / fail-closed check: deleting a guide simply removes that scope's
    guidance from future resolutions. (This is the deliberate contrast with
    content_types.delete, which fails closed while a project/default references it.)"""
    g = _require(session, guide_id)
    session.delete(g)
    session.commit()


# --- approval lifecycle ------------------------------------------------------
def _to_draft(g: StyleGuide) -> None:
    """Revert an in-memory guide to DRAFT + clear its sign-off (no commit — the caller
    commits). No-op if already draft."""
    if g.status == StyleGuideStatus.APPROVED:
        g.status = StyleGuideStatus.DRAFT
        g.approved_by = ""
        g.approved_at = None


def approve(session: Session, guide_id: int, approver_email: str = "") -> StyleGuide:
    """Mark a guide APPROVED (records approver + timestamp). Only approved guides
    resolve. Idempotent."""
    g = _require(session, guide_id)
    g.status = StyleGuideStatus.APPROVED
    g.approved_by = approver_email or ""
    g.approved_at = _utcnow()
    g.updated_at = _utcnow()
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def unapprove(session: Session, guide_id: int) -> StyleGuide:
    """Revert a guide to DRAFT (clears approver + timestamp)."""
    g = _require(session, guide_id)
    g.status = StyleGuideStatus.DRAFT
    g.approved_by = ""
    g.approved_at = None
    g.updated_at = _utcnow()
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def set_status(session: Session, guide_id: int, status: str) -> StyleGuide:
    """Set a guide's status to a StyleGuideStatus value (the CLI/api single seam).
    'approved' records the approver; 'draft' clears the sign-off. A bad value raises."""
    st = (status or "").strip().lower()
    if st not in StyleGuideStatus.ALL:
        raise StyleGuideError("status must be one of: {}".format(", ".join(StyleGuideStatus.ALL)))
    return (
        approve(session, guide_id)
        if st == StyleGuideStatus.APPROVED
        else unapprove(session, guide_id)
    )


# --- resolution (the layered scope chain — most-specific wins) ---------------
# Scope rungs tried in order, MOST-SPECIFIC first (per the spec: "customer x
# content_type beats customer beats content_type beats org-global"). Each is a
# (customer_id-predicate, content_type_id-predicate) pair; within a matched rung,
# pair-specific beats universal, then most-recently-updated wins.
def resolve_style_guide(
    session: Session,
    org_id=None,
    customer_id=None,
    content_type_id=None,
    source_locale=None,
    target_locale=None,
) -> StyleGuide | None:
    """Resolve the single effective APPROVED StyleGuide for a translation's scope, or
    None. Most-specific-wins, in the context_pkg spirit:

        customer x content_type  >  customer  >  content_type  >  org-global

    and WITHIN each of those scope rungs a PAIR-SPECIFIC guide (matching source+target)
    beats a UNIVERSAL (pair-agnostic) one; ties among equally-specific guides break by
    most-recently-updated (updated_at desc, id desc) so several drafts-then-approved
    revisions resolve to the freshest blessed one WITHOUT a unique constraint blocking
    the authoring flow (the documented non-unique-index decision).

    ONLY APPROVED guides are eligible (a draft never reaches a prompt). `org_id`
    (multi-org) scopes every read so a foreign org's guide is never applied (no-op
    single-tenant, byte-identical).

    #73 (fail-closed BY CONSTRUCTION, mirrors visible/list_guides): a bare None org_id
    under an ACTIVE multi-org gate resolves NOTHING (returns None), not any org's guide;
    access.UNSCOPED (the CLI --operator door) keeps the cross-org read; gate off is
    byte-identical. Every current caller feeds the project's own (row) org, so this is
    construction-level closure, not a today-live leak."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    cid = customer_id
    ct = content_type_id
    src, tgt = _norm(source_locale), _norm(target_locale)

    # The scope rungs, most-specific first. `None` in a slot means "that column IS
    # NULL" (a broader guide); a concrete id means "== that id".
    rungs: list[tuple] = []
    if cid is not None and ct is not None:
        rungs.append((cid, ct))
    if cid is not None:
        rungs.append((cid, None))
    if ct is not None:
        rungs.append((None, ct))
    rungs.append((None, None))  # org-global universal-scope guide

    for want_cust, want_ct in rungs:
        stmt = select(StyleGuide).where(StyleGuide.status == StyleGuideStatus.APPROVED)
        if org_id is not None:
            stmt = stmt.where(StyleGuide.org_id == org_id)
        stmt = stmt.where(
            (StyleGuide.customer_id == want_cust)
            if want_cust is not None
            else StyleGuide.customer_id.is_(None)
        )
        stmt = stmt.where(
            (StyleGuide.content_type_id == want_ct)
            if want_ct is not None
            else StyleGuide.content_type_id.is_(None)
        )
        candidates = list(session.exec(stmt).all())
        if not candidates:
            continue
        best = _pick(candidates, src, tgt)
        if best is not None:
            return best
    return None


def _pair_matches(g: StyleGuide, src: str, tgt: str) -> bool:
    """A guide's stored pair matches the run's (src, tgt), normalized. A guide with NO
    stored pair (universal) matches anything. A stored pair matches only its exact
    normalized source+target."""
    gs, gt = _norm(g.source_locale), _norm(g.target_locale)
    if not gs and not gt:
        return True  # universal guide
    return gs == src and gt == tgt


def _pick(candidates: list, src: str, tgt: str) -> StyleGuide | None:
    """Among same-scope approved candidates, choose: pair-specific over universal
    (only pair-specific guides that MATCH the run's pair are eligible; a NON-matching
    stored pair is skipped entirely), then most-recently-updated (updated_at desc, id
    desc). Returns None when no candidate is eligible (all had a non-matching pair)."""
    eligible = [g for g in candidates if _pair_matches(g, src, tgt)]
    if not eligible:
        return None

    def _key(g: StyleGuide):
        gs, gt = _norm(g.source_locale), _norm(g.target_locale)
        pair_specific = 1 if (gs or gt) else 0
        return (pair_specific, g.updated_at or datetime.min, g.id or 0)

    return max(eligible, key=_key)


def has_resolvable_guide(session: Session, *, org_id=None, customer_id=None) -> bool:
    """True when at least one APPROVED guide is visible to a (org, customer) scope —
    the guide's customer is the caller's OR org-global (NULL). Used by the workflow
    builder's honest "a style guide would resolve per document" note (it cannot know
    the content type or pair at build time, so this is a scope-level availability
    check, NOT a promise a specific guide resolves)."""
    stmt = select(StyleGuide.id).where(StyleGuide.status == StyleGuideStatus.APPROVED)
    if org_id is not None:
        stmt = stmt.where(StyleGuide.org_id == org_id)
    if customer_id is not None:
        stmt = stmt.where(
            (StyleGuide.customer_id == customer_id) | (StyleGuide.customer_id.is_(None))
        )
    else:
        # a staff/global caller: any approved guide in the org counts
        pass
    return session.exec(stmt.limit(1)).first() is not None


# --- echo helpers (CLI --json / web / api) -----------------------------------
def scope_label(g: StyleGuide) -> str:
    """Human-readable scope for a guide row (CLI/web echo)."""
    parts = []
    parts.append(f"customer #{g.customer_id}" if g.customer_id is not None else "org-global")
    if g.content_type_id is not None:
        parts.append(f"content-type #{g.content_type_id}")
    if g.source_locale or g.target_locale:
        parts.append("{}->{}".format(g.source_locale or "*", g.target_locale or "*"))
    else:
        parts.append("any pair")
    return " x ".join(parts)


def to_dict(session: Session, g: StyleGuide, *, include_body: bool = True) -> dict:
    """A JSON-serializable summary (CLI --json, /api, web) of a guide. `include_body`
    off for LIST views (the body can be long); on for SHOW/detail."""
    out = {
        "id": g.id,
        "name": g.name,
        "status": g.status,
        "org_id": g.org_id,
        "customer_id": g.customer_id,
        "content_type_id": g.content_type_id,
        "source_locale": g.source_locale,
        "target_locale": g.target_locale,
        "scope": scope_label(g),
        "approved_by": g.approved_by,
        "approved_at": g.approved_at.isoformat() if g.approved_at else None,
        "created_by": g.created_by,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
        "body_chars": len(g.body or ""),
    }
    if include_body:
        out["body"] = g.body or ""
    return out
