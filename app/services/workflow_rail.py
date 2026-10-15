"""Workflow Studio aids-rail data assembly (Wave B).

The /workflows/new "What we already know" panel is a WEB-UX COMPOSITION of data
already exposed by the per-asset services — it introduces no new scope logic of its
own. Every list here is fetched through an EXISTING service seam that already applies
the org + customer-ring predicates (glossary_module / style_guides / context_pkg /
tm_module / customers), so a customer-bound linguist sees exactly their own customer's
assets and a staff author sees the customer they are building for. The route is
responsible for validating that the selected customer is in the caller's VISIBLE set
before calling in here (mirrors the /glossaries customer-first page): given that, the
customer_id passed to each list service is the authoritative confinement.

Honest by construction: an absent fact is an empty list (the panel omits that section),
never a fabricated value. Nothing is invented — glossary term/DNT counts and the term
lines come from the glossary's own entries; style guides are the APPROVED ones only;
pairs + TM size come from the customer-360 assembly the customer page already uses.

Documented in: docs/architecture/workflow-engine.md
"""

from sqlmodel import Session

from app.models import Customer, GlossaryStatus, StyleGuideStatus
from app.services import context_pkg, tm_module
from app.services import customers as customer_service
from app.services import glossary_module as gm
from app.services import style_guides as sg

# Cap the per-glossary term list rendered into the expandable insert menu. The counts
# (term/DNT) are always exact; only the click-to-insert LIST is bounded so a large
# glossary can't bloat the page. A truncated glossary carries `terms_truncated` +
# `terms_shown` so the template can note "showing first N of M" honestly.
_TERMS_CAP = 50


def _pair_insert(p: dict) -> str:
    """The single string a pair chip both SHOWS and INSERTS: base language name + code
    together, e.g. "English (en-US) → Spanish (es-ES)". The region already lives in the
    code, so we take the base name (name_for returns the verbose "English (United
    States)"; strip its parenthetical so the code isn't doubled). Falls back to the raw
    code(s) for a malformed / single-sided pair so it is never blank."""
    from app.services import languages_catalog

    def _base(code):
        return (languages_catalog.name_for(code) or code).split(" (")[0]

    src, tgt = p.get("src") or "", p.get("tgt") or ""
    if src and tgt:
        return f"{_base(src)} ({src}) → {_base(tgt)} ({tgt})"
    return p.get("label") or p.get("pair") or src or tgt


def _context_summary(ctx: dict) -> str:
    """A one-line domain/audience/formality/tone summary of a context profile's
    resolved fields (honest: only the fields that are actually set appear). '' when
    the profile carries none of them."""
    if not isinstance(ctx, dict):
        return ""
    bits = []
    dom = (ctx.get("domain") or "").strip()
    if dom:
        bits.append(dom)
    aud = (ctx.get("audience") or "").strip()
    if aud:
        bits.append(aud)
    fm = (ctx.get("formality") or "").strip()
    if fm:
        bits.append(f"{fm} formality")
    tone = ctx.get("tone")
    if isinstance(tone, list) and tone:
        bits.append("tone: " + ", ".join(str(t) for t in tone[:4]))
    elif isinstance(tone, str) and tone.strip():
        bits.append("tone: " + tone.strip())
    return " · ".join(bits)


def _glossary_facts(session: Session, org_id, customer_id: int) -> list:
    """The customer's glossaries with exact term + DNT counts and a bounded term list
    for click-to-insert. Uses list_for_customer (the customer-first scope seam) + the
    glossary's lean count/preview helpers — the counts are two COUNT(*) queries and the
    term list is a sense-free LIMIT fetch, so the cost is flat regardless of glossary
    size (never list_entries_for_glossary, which loads every row + per-entry senses)."""
    out = []
    for g in gm.list_for_customer(session, org_id, customer_id):
        term_count, dnt_count = gm.term_counts(session, g.id)
        terms = gm.preview_terms(session, g.id, limit=_TERMS_CAP)
        out.append(
            {
                "id": g.id,
                "name": g.name,
                "term_count": term_count,
                "dnt_count": dnt_count,
                "terms": terms,
                "terms_shown": len(terms),
                "terms_truncated": term_count > len(terms),
                "approved": g.status == GlossaryStatus.APPROVED,
            }
        )
    return out


def _style_guide_facts(session: Session, org_id, customer_id: int) -> list:
    """APPROVED style guides visible in the customer's scope (own + org-global), name +
    human scope label. Only approved guides ever resolve at run time, so the rail
    offers only those as reference lines."""
    return [
        {"id": g.id, "name": g.name, "scope": sg.scope_label(g)}
        for g in sg.list_guides(session, org_id=org_id, customer_id=customer_id)
        if g.status == StyleGuideStatus.APPROVED
    ]


def _context_facts(session: Session, org_id, customer_id: int) -> list:
    """The customer's context profiles with a domain/audience/formality/tone summary."""
    out = []
    for prof in context_pkg.list_profiles(session, org_id=org_id, customer_id=customer_id):
        row = context_pkg.profile_row(prof)
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "scope": row["scope"],
                "summary": _context_summary(row.get("context") or {}),
            }
        )
    return out


def customer_facts(session: Session, customer_id: int, *, org_id=None) -> dict:
    """Assemble the "What we already know" facts for ONE customer, scoped entirely to
    that customer through the existing per-asset services. The caller MUST have already
    confirmed `customer_id` is visible to the viewer (org + bound ring) — this composes
    the already-scoped list seams, it does not re-authorize.

    Returns a dict of honest, possibly-empty sections:
      pairs             — labelled active/derived language pairs (customer-360)
      tm_entries        — TM segment-entry count for the customer's projects
      glossaries        — [{name, term_count, dnt_count, terms:[{source,target,dnt}], ...}]
      style_guides      — APPROVED guides [{name, scope}]
      context_profiles  — [{name, scope, summary}]
    """
    facts = {
        "pairs": [],
        "tm_entries": 0,
        "glossaries": [],
        "style_guides": [],
        "context_profiles": [],
    }
    # Self-defending seam: get_customer_360 does a bare session.get with no org check,
    # so its `all_pairs` would otherwise honour the org_id argument only by the caller's
    # goodwill. Resolve the customer row here and, when an org is in play, refuse a
    # foreign-org id by returning the EMPTY-facts shape — byte-identical to no selection,
    # so it is never an existence oracle. The per-asset list services below are already
    # org+customer scoped; this closes the one field (pairs) that was not.
    cust = session.get(Customer, customer_id)
    if cust is None:
        return facts
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        return facts
    c360 = customer_service.get_customer_360(session, customer_id, org_id=org_id)
    if c360 is None:
        return facts
    # Pairs: reconcile the chip so its FACE and its click-to-insert payload carry the
    # SAME content — names + codes together ("English (en-US) → Spanish (es-ES)"), so a
    # zero-JS reader copies exactly what a click would insert.
    facts["pairs"] = [{**p, "insert": _pair_insert(p)} for p in c360.get("all_pairs", [])]
    # TM size via the tm_module scope seam (confined to the customer's own projects);
    # an empty project set -> in_([]) -> 0 (honest, never unrestricted).
    pids = tm_module.customer_project_ids(session, customer_id)
    facts["tm_entries"] = tm_module.count_entries(session, visible_pids=pids)
    facts["glossaries"] = _glossary_facts(session, org_id, customer_id)
    facts["style_guides"] = _style_guide_facts(session, org_id, customer_id)
    facts["context_profiles"] = _context_facts(session, org_id, customer_id)
    return facts
