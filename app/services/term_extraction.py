"""Term extraction — the upstream half of the maintainer's term-extraction idea (research
Option C, TERM-EXTRACTION-RESEARCH-2026-07-21.md).

Extract candidate terminology from a document, propose translations, and land the
candidates PENDING in a per-glossary review grid — where a reviewer accepts each into
the customer-bound glossary (a real GlossaryEntry) or drops it. NO silent auto-add
anywhere: this service is the ONE place both the standalone "Extract from document"
flow and the (rewired) paste-text builder land candidates.

Two-stage extraction (the L3X recall-then-precision pattern, research §2 + §4):

  stage 1  RECALL   — the EXISTING spend-capped, mock-exempt LLM extractor
                      ``glossary.build_from_text`` over-proposes candidates (reused,
                      not forked).
  stage 2  PRECISION — a DISTINCT LLM precision-prune pass (task ``prune_terms``: keep
                      genuine domain terminology, drop generic words) PLUS deterministic
                      sanity filters:
                        * minimum document frequency (the frequency lens),
                        * dedupe against terms ALREADY in this glossary (an
                          already-covered term never becomes a candidate — the
                          match-terms/existing-entry idiom),
                        * dedupe against this glossary's DROPPED/ACCEPTED history so
                          re-extraction NEVER re-proposes a decided term (the memoQ
                          self-teaching-stopword noise-control backbone),
                        * dedupe against still-PENDING candidates (no duplicate rows).

Proposed translations (task ``translate_terms``) fill ONE target locale per run (the
locale picked at launch — research's one-review-pass-per-locale). Every provider call
is spend-capped + mock-exempt (MockProvider answers all three tasks deterministically),
so the whole loop is hermetic-testable and demo-safe. A spend-blocked run refuses
cleanly with ZERO candidates written (never a partial batch).

A TermCandidate is DATA about a proposed term; like GlossaryEntry.meta it NEVER reaches
a prompt. Accepting a candidate populates TBX meta (usage_example) ONLY from the honest
in-context snippet — never fabricated.

Documented in: docs/architecture/terminology.md
"""

from __future__ import annotations

import json
import re

from sqlmodel import Session, select

from app.models import (
    Document,
    GlossaryEntry,
    Project,
    TermCandidate,
    TermCandidateStatus,
)
from app.services import glossary as gloss_engine
from app.services import glossary_module as gm
from app.services import spend as _spend
from app.services.providers import DeadEngineError, get_provider

# The extractor reads at most this much source text (mirrors build_from_text's own
# 6000-char cap); frequency is still counted over the same window so the two agree.
_EXTRACT_TEXT_CAP = 6000
# Cap on candidates a single run PROPOSES (after prune, highest-frequency first) so a
# huge document can never dump an unreviewable wall on the reviewer (the MultiTerm
# reviewer-fatigue lesson). A generous default; the deterministic filters usually cut
# well below it.
MAX_CANDIDATES = 40
# A candidate must occur at least this many times in the source document to survive the
# frequency lens (1 = "appears at all"). Kept conservative — the LLM prune is the main
# precision gate; this only drops terms that don't actually occur in the text window.
MIN_FREQUENCY = 1
_SNIPPET_WIDTH = 90


class TermExtractionError(ValueError):
    """A term-extraction operation was invalid (unknown/foreign document, blank source
    text, a candidate not owned by the glossary). Surfaced as a clean error by the
    web route / CLI / API."""


class ExtractionEngineUnavailable(TermExtractionError):
    """The default AI engine cannot run (no API key, or a dead engine reference). Extraction
    is an LLM task, so there is nothing to do until a key is added; every surface answers with
    this message (a 4xx on the API and MCP, a clean error on the CLI, a notice on the page)
    instead of a 500 (#761)."""


# --- source text + document scoping ------------------------------------------
#: Pass as ``user`` for a caller with NO end user (the host CLI, which is the operator surface
#: and has no RBAC). Web and /api callers pass the signed-in user (``None`` is the auth-off
#: anonymous user, which ``access.visible_project_ids`` treats as unrestricted).
OPERATOR = object()


def _visible_projects(session: Session, user):
    """Project ids ``user`` may see, or None for unrestricted (``OPERATOR`` included)."""
    if user is OPERATOR:
        return None
    from app.services import access

    visible = access.visible_project_ids(session, user)
    return None if visible is None else set(visible)


def document_source_text(document: Document) -> str:
    """The joined translatable units of a document (the same seam glossary_suggest
    uses). Empty string when the document has no skeleton units."""
    units = (document.skeleton or {}).get("units", []) if document else []
    return "\n".join(u.get("text", "") for u in units if isinstance(u, dict))


def documents_for_glossary(session: Session, glossary, org_id=None, user=OPERATOR) -> list[dict]:
    """Documents a reviewer may extract from for THIS glossary — the visible-scope
    picker. A glossary is customer-bound, so we offer documents from that customer's
    projects (org-scoped); an unassigned (customer_id NULL) glossary offers documents
    from customer-less projects. Returns [{id, filename, project_id, project_name}]
    newest-first. #541: when ``user`` is given, only projects that user can see are offered
    (the picker never lists a hidden project's document names)."""
    stmt = select(Project)
    if org_id is not None:
        stmt = stmt.where(Project.org_id == org_id)
    if glossary.customer_id is not None:
        stmt = stmt.where(Project.customer_id == glossary.customer_id)
    else:
        stmt = stmt.where(Project.customer_id.is_(None))
    visible = _visible_projects(session, user)
    projects = {p.id: p for p in session.exec(stmt).all() if visible is None or p.id in visible}
    if not projects:
        return []
    docs = session.exec(
        # SPEC-key-based-projects 2.5: internal key documents are not offerable
        # files -- their synthetic filenames would surface in the picker.
        select(Document)
        .where(Document.project_id.in_(list(projects.keys())), Document.fmt != "keys")
        .order_by(Document.created_at.desc())
    ).all()
    out = []
    for d in docs:
        p = projects.get(d.project_id)
        out.append(
            {
                "id": d.id,
                "filename": d.filename,
                "project_id": d.project_id,
                "project_name": p.name if p else "",
            }
        )
    return out


def visible_document(
    session: Session, glossary, document_id, org_id=None, user=OPERATOR
) -> Document | None:
    """Fetch a document ONLY if it is in the glossary's extraction scope (its customer's
    projects, org-scoped), else None. The read-authz seam for the document picker — a
    forged document_id from another customer/org is refused. #541: when ``user`` is given,
    a document in a project that user cannot see is refused too (same answer as missing)."""
    doc = session.get(Document, document_id)
    if doc is None:
        return None
    project = session.get(Project, doc.project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    visible = _visible_projects(session, user)
    if visible is not None and project.id not in visible:
        return None
    if glossary.customer_id is not None:
        if project.customer_id != glossary.customer_id:
            return None
    else:
        if project.customer_id is not None:
            return None
    return doc


# --- deterministic lenses ----------------------------------------------------
def _term_frequency(term: str, text: str) -> int:
    """Count word-boundary occurrences of a term in text (case-insensitive), reusing the
    same ASCII-word-boundary vs substring rule as glossary matching so a CJK term is
    counted by substring."""
    if not term:
        return 0
    if re.fullmatch(r"[\w\s\-']+", term) and term.isascii():
        return len(re.findall(rf"\b{re.escape(term)}\b", text, re.IGNORECASE))
    lowered = text.lower()
    t = term.lower()
    return lowered.count(t) if t else 0


def _context_snippet(term: str, text: str, width: int = _SNIPPET_WIDTH) -> str:
    """A short in-context snippet around the term's first occurrence (the concordance
    lens) — whitespace-flattened, capped. Empty when the term isn't found."""
    flat = " ".join(text.split())
    if not term:
        return ""
    m = re.search(re.escape(term), flat, re.IGNORECASE)
    if m is None:
        return flat[:width]
    start = max(0, m.start() - width // 3)
    end = min(len(flat), m.end() + width // 3)
    snippet = flat[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(flat):
        snippet = snippet + "..."
    return snippet[: width + 8]


def _dedupe_key(term: str) -> str:
    """Normalized comparison key for dedupe: strip surrounding punctuation/whitespace +
    lower-case, so a near-dup like '.API' / 'API,' collapses onto 'api'. Used on BOTH
    sides (candidate + existing/history sets) so normalization is symmetric. The stored
    source_term keeps its original spelling; only the KEY is normalized."""
    return (term or "").strip().strip(".,;:!?\"'()[]{}").strip().lower()


def _existing_terms(session: Session, glossary_id: int) -> set[str]:
    """Normalized source terms ALREADY in this glossary — an already-covered term never
    becomes a candidate."""
    rows = session.exec(
        select(GlossaryEntry.source_term).where(GlossaryEntry.glossary_id == glossary_id)
    ).all()
    return {_dedupe_key(t) for t in rows if _dedupe_key(t)}


def _decided_or_pending_terms(session: Session, glossary_id: int) -> set[str]:
    """Normalized source terms already DECIDED (accepted/dropped) OR still PENDING for
    this glossary — the noise-control history: a decided term is never re-proposed, and
    a pending one is never duplicated."""
    rows = session.exec(
        select(TermCandidate.source_term).where(TermCandidate.glossary_id == glossary_id)
    ).all()
    return {_dedupe_key(t) for t in rows if _dedupe_key(t)}


# --- the pipeline ------------------------------------------------------------
def _prune(candidates: list[str], text: str, provider) -> tuple[list[str], bool]:
    """Stage-2 precision-prune (LLM judge, spend-capped). Returns (kept_subset,
    spend_degraded). On a spend-blocked REAL engine the prune is SKIPPED (degrade to
    frequency-only precision — candidates still land, LESS pruned) rather than failing
    the run, and spend_degraded is True so the caller can flag the run honestly. Mock is
    exempt (never degraded)."""
    if not candidates:
        return [], False
    prompt = (
        "You are pruning extracted glossary term candidates for PRECISION. Keep only "
        "genuine domain terminology (product names, technical/domain terms, "
        "do-not-translate items); drop generic words and common phrases. Reply as a "
        "JSON list of the source terms to KEEP.\n\nCandidates: "
        + json.dumps(candidates)
        + "\n\nDocument sample:\n"
        + text[:2000]
    )
    try:
        raw = _spend.complete_direct(
            provider,
            prompt,
            task="prune_terms",
            payload={"candidates": candidates, "text": text[:2000]},
        )
        kept = json.loads(raw)
        if not isinstance(kept, list):
            return list(candidates), False
        keep_set = {str(k).strip().lower() for k in kept}
        # Preserve the incoming order; only keep terms the judge returned.
        return [c for c in candidates if c.strip().lower() in keep_set], False
    except _spend.SpendCapReached:
        return list(candidates), True
    except Exception:
        return list(candidates), False  # a hiccup (not a spend block) is not "degraded"


def _propose_translations(
    terms: list[str], source_locale: str, target_locale: str, provider
) -> tuple[dict, bool]:
    """Propose a target-locale translation for each kept term (LLM, spend-capped, ONE
    batched call). Returns ({source_term: proposed_target}, spend_degraded). On a
    spend-blocked REAL engine returns ({}, True) — candidates land WITHOUT proposed
    translations (the reviewer supplies them; research's documented degrade), and the
    flag lets the caller say so. Mock is exempt (never degraded)."""
    if not terms:
        return {}, False
    prompt = (
        f"Translate each of these glossary terms from {source_locale or 'the source'} "
        f"to {target_locale or 'the target'} locale. Reply as a JSON object mapping "
        "each source term to its translation.\n\n" + json.dumps(terms)
    )
    try:
        raw = _spend.complete_direct(
            provider,
            prompt,
            task="translate_terms",
            payload={"terms": terms, "source": source_locale, "target": target_locale},
        )
        out = json.loads(raw)
        return (out if isinstance(out, dict) else {}), False
    except _spend.SpendCapReached:
        return {}, True
    except Exception:
        return {}, False  # a hiccup (not a spend block) is not "degraded"


def extract(
    session: Session,
    glossary,
    target_locale: str,
    *,
    document: Document | None = None,
    text: str | None = None,
    source_locale: str = "",
    provider=None,
) -> dict:
    """Run the two-stage extraction for ONE glossary + ONE target locale and land the
    surviving candidates PENDING in the review grid. Returns a summary dict:

      {ok, blocked, proposed, extracted, filtered_existing, filtered_history,
       filtered_frequency, pruned, source_document_id}

    `blocked=True` (a spend-capped REAL engine) is a CLEAN refusal: ZERO candidates are
    written (never a partial batch). Never auto-adds a GlossaryEntry — candidates await
    review."""
    if document is not None:
        # AI opt-out (DPAI-08): extraction is an LLM task; refuse for an opted-out project.
        from app.services import ai_optout

        ai_optout.refuse_for_document(session, document, "glossary term extraction")
    if provider is None:
        try:
            provider = get_provider()
        except DeadEngineError as exc:
            raise ExtractionEngineUnavailable(
                "Term extraction needs an AI engine, but the default engine cannot run: "
                f"{exc} Add the API key in Setup AI (CLI: `engine update <id> --secret "
                "api_key=-`), then try again."
            ) from exc
    # Resolve the source text + provenance.
    if document is not None:
        src_text = document_source_text(document)
        src_doc_id = document.id
        if not source_locale:
            project = session.get(Project, document.project_id)
            source_locale = getattr(project, "source_locale", "") or ""
    else:
        src_text = text or ""
        src_doc_id = None
    src_text = src_text[:_EXTRACT_TEXT_CAP]
    summary = {
        "ok": True,
        "blocked": False,
        "degraded": False,
        "degraded_steps": [],
        "proposed": 0,
        "extracted": 0,
        "filtered_existing": 0,
        "filtered_history": 0,
        "filtered_frequency": 0,
        "pruned": 0,
        "source_document_id": src_doc_id,
    }
    if not src_text.strip():
        return summary

    # Spend brake BEFORE any provider call: a blocked REAL engine refuses the whole run
    # with no partial candidates (mock is exempt -> proceeds).
    if _spend.blocked_for(provider):
        summary.update(ok=False, blocked=True)
        return summary

    # Stage 1 (recall): the existing extractor over-proposes. Records its own spend.
    raw = gloss_engine.build_from_text(src_text, provider)
    # Unique, non-blank source terms, first spelling wins (case-insensitive key).
    seen: set[str] = set()
    ordered: list[str] = []
    for c in raw:
        st = str((c or {}).get("source_term") or "").strip()
        if not st:
            continue
        key = _dedupe_key(st)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(st)
    summary["extracted"] = len(ordered)

    existing = _existing_terms(session, glossary.id)
    history = _decided_or_pending_terms(session, glossary.id)
    survivors: list[str] = []
    freqs: dict[str, int] = {}
    for st in ordered:
        key = _dedupe_key(st)
        if key in existing:
            summary["filtered_existing"] += 1
            continue
        if key in history:
            summary["filtered_history"] += 1
            continue
        freq = _term_frequency(st, src_text)
        if freq < MIN_FREQUENCY:
            summary["filtered_frequency"] += 1
            continue
        freqs[st] = freq
        survivors.append(st)

    # Stage 2 (precision): LLM prune, then cap by frequency (highest first). A
    # spend-block MID-pipeline (stage-1 recall succeeded, then the cap tripped) skips the
    # prune but still lands the survivors PENDING — flagged `degraded` so the surfaces can
    # say the precision pass was skipped (expect more noise, review accordingly).
    kept, prune_degraded = _prune(survivors, src_text, provider)
    summary["pruned"] = len(survivors) - len(kept)
    kept.sort(key=lambda t: (-freqs.get(t, 0), t.lower()))
    kept = kept[:MAX_CANDIDATES]

    # Proposed translations for the one target locale (one batched call). A mid-pipeline
    # spend-block here lands candidates WITHOUT proposed translations (also `degraded`).
    proposals, translate_degraded = _propose_translations(
        kept, source_locale, target_locale, provider
    )
    if prune_degraded:
        summary["degraded_steps"].append("prune")
    if translate_degraded:
        summary["degraded_steps"].append("translation")
    summary["degraded"] = bool(summary["degraded_steps"])

    for st in kept:
        proposed = proposals.get(st)
        cand = TermCandidate(
            glossary_id=glossary.id,
            source_term=st,
            proposed_target=(str(proposed).strip() if proposed else None),
            source_locale=source_locale or "",
            target_locale=target_locale or "",
            frequency=freqs.get(st, 0),
            context_snippet=_context_snippet(st, src_text),
            status=TermCandidateStatus.PENDING,
            source_document_id=src_doc_id,
        )
        session.add(cand)
        summary["proposed"] += 1
    session.commit()
    return summary


def estimate(
    session: Session, glossary, *, document: Document | None = None, text: str | None = None
) -> dict:
    """A pre-flight, NO-provider-call estimate for the launch form (spend honesty):
    source size + the three LLM passes a run makes. Extraction, prune and per-term
    translation are all real token spend on a live engine (free + deterministic on the
    offline mock)."""
    if document is not None:  # noqa: SIM108
        src = document_source_text(document)
    else:
        src = text or ""
    src = src[:_EXTRACT_TEXT_CAP]
    units = (document.skeleton or {}).get("units", []) if document is not None else []
    return {
        "chars": len(src),
        "units": len(units),
        "llm_passes": 3,  # extract (recall) + prune (precision) + translate proposals
        "note": "3 LLM passes: extract, prune, and propose translations. Free + "
        "deterministic offline (no key); real token spend on a live engine.",
    }


# --- candidate review CRUD (the shared accept/drop seams) --------------------
def list_candidates(
    session: Session, glossary_id: int, status: str = TermCandidateStatus.PENDING
) -> list[TermCandidate]:
    """Candidates for a glossary in a status (default PENDING — the review queue),
    highest-frequency first so real terms surface first (the memoQ status-sorted grid)."""
    stmt = select(TermCandidate).where(TermCandidate.glossary_id == glossary_id)
    if status is not None:
        stmt = stmt.where(TermCandidate.status == status)
    rows = list(session.exec(stmt).all())
    rows.sort(key=lambda c: (-(c.frequency or 0), c.source_term.lower()))
    return rows


def hidden_source_document_ids(session: Session, cands, user) -> set:
    """#478: the source document ids, among these candidates, whose project `user` may not
    see. A candidate's `context_snippet` is a sentence copied from that document, so every
    read surface blanks the snippet (and the document id) for these. Empty for an
    unrestricted user (staff see-all, auth off). A document that no longer exists counts
    as hidden for a restricted user."""
    doc_ids = {c.source_document_id for c in cands if getattr(c, "source_document_id", None)}
    if not doc_ids:
        return set()
    visible = _visible_projects(session, user)
    if visible is None:
        return set()
    owner = dict(
        session.exec(select(Document.id, Document.project_id).where(Document.id.in_(doc_ids))).all()
    )
    return {d for d in doc_ids if owner.get(d) not in visible}


def redact_hidden_snippets(session: Session, cands, user) -> list:
    """#478: `cands` for display to `user`: a candidate whose source document is in a
    project the user cannot see comes back as a plain dict with no snippet and no document
    id (never a mutated ORM row, so nothing can flush back); the rest are unchanged."""
    hidden = hidden_source_document_ids(session, cands, user)
    if not hidden:
        return list(cands)
    out = []
    for c in cands:
        if c.source_document_id in hidden:
            row = c.model_dump()
            row["context_snippet"] = ""
            row["source_document_id"] = None
            out.append(row)
        else:
            out.append(c)
    return out


def counts(session: Session, glossary_id: int) -> dict:
    """Per-status candidate counts for a glossary (for the grid header / CLI)."""
    rows = session.exec(
        select(TermCandidate.status).where(TermCandidate.glossary_id == glossary_id)
    ).all()
    out = dict.fromkeys(TermCandidateStatus.ALL, 0)
    for s in rows:
        out[s] = out.get(s, 0) + 1
    return out


def get_candidate(session: Session, glossary_id: int, candidate_id: int) -> TermCandidate | None:
    """Fetch a candidate ONLY if it belongs to this glossary (else None — a forged
    candidate_id can't touch another glossary's queue)."""
    c = session.get(TermCandidate, candidate_id)
    if c is None or c.glossary_id != glossary_id:
        return None
    return c


def accept(
    session: Session,
    glossary_id: int,
    candidate_id: int,
    decided_by: str = "",
    target_term: str | None = None,
    user=OPERATOR,
) -> GlossaryEntry | None:
    """Accept a PENDING candidate into THIS glossary as a real GlossaryEntry
    (customer-bound by construction via gm.add_term — which reverts an approved glossary
    to draft, the correct re-approval semantics). `target_term` overrides the proposed
    translation (the reviewer's edit); otherwise the stored proposal is used. TBX meta
    is populated ONLY from the honest in-context snippet (usage_example) — never
    fabricated; meta never reaches a prompt. #541: the snippet is a sentence copied from the
    source document, so when ``user`` cannot see that document's project the snippet is
    NOT copied into the entry (the term is still accepted). Returns the entry, or None if
    the candidate is unknown/foreign or already decided."""
    c = get_candidate(session, glossary_id, candidate_id)
    if c is None or c.status != TermCandidateStatus.PENDING:
        return None
    tgt = target_term if target_term is not None else (c.proposed_target or "")
    # Honest meta only: a real in-context example -> usage_example (allowlisted +
    # sanitized by the engine). Never invent definition/part-of-speech.
    meta = {"usage_example": c.context_snippet} if (c.context_snippet or "").strip() else None
    if meta and hidden_source_document_ids(session, [c], user):
        meta = None
    entry = gm.add_term(
        session,
        glossary_id,
        c.source_term,
        target_term=tgt,
        source_locale=c.source_locale,
        target_locale=c.target_locale,
        meta=meta,
    )
    c.status = TermCandidateStatus.ACCEPTED
    # Keep source_document_id + context_snippet on the durable decision: erasure
    # uses this provenance to scrub the sanitized example in every glossary store.
    c.decided_at = _now()
    c.decided_by = decided_by or ""
    session.add(c)
    session.commit()
    return entry


def drop(session: Session, glossary_id: int, candidate_id: int, decided_by: str = "") -> bool:
    """Drop a PENDING candidate. It PERSISTS as DROPPED so re-extraction never
    re-proposes it (the noise-control backbone). Returns True on success, False if the
    candidate is unknown/foreign or already decided."""
    c = get_candidate(session, glossary_id, candidate_id)
    if c is None or c.status != TermCandidateStatus.PENDING:
        return False
    c.status = TermCandidateStatus.DROPPED
    c.decided_at = _now()
    c.decided_by = decided_by or ""
    session.add(c)
    session.commit()
    return True


def batch(
    session: Session,
    glossary_id: int,
    candidate_ids,
    action: str,
    decided_by: str = "",
    user=OPERATOR,
) -> dict:
    """Batch accept/drop a set of candidate ids. Returns {accepted, dropped, skipped}.
    A candidate already decided or not owned by the glossary is counted as skipped."""
    accepted = dropped = skipped = 0
    for cid in candidate_ids or []:
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if action == "accept":
            entry = accept(session, glossary_id, cid, decided_by=decided_by, user=user)
            if entry is not None:
                accepted += 1
            else:
                skipped += 1
        elif action == "drop":
            if drop(session, glossary_id, cid, decided_by=decided_by):
                dropped += 1
            else:
                skipped += 1
        else:
            skipped += 1
    return {"accepted": accepted, "dropped": dropped, "skipped": skipped}


def _now():
    from datetime import datetime

    return datetime.utcnow()
