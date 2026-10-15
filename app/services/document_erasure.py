"""Source-derived copies reached by the shared document deletion transaction.

Documented in: docs/architecture/data-and-migrations.md
"""

from datetime import datetime

from sqlalchemy import delete, or_
from sqlmodel import Session, select

from app.models import (
    Document,
    EvalDataset,
    EvalDatasetItem,
    EvalRun,
    EvalRunItem,
    EvalScore,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryTerm,
    QualityGoldItem,
    QualityGoldLabel,
    TermCandidate,
)


def scrub_term_examples(session: Session, document_id: int) -> int:
    """Scrub before severing candidate provenance, including undecided candidates.

    The candidate retains the original snippet and glossary even after acceptance.
    That pair identifies copies in all three glossary stores, including legacy
    accepted examples and renamed terms. Matching the sanitized value also reaches
    pinned overrides while leaving nonmatching examples alone. Keep the
    accept/drop decision and terminology; only the document context is erased.
    """
    from app.services.glossary_common import _sanitize_meta_value

    candidates = session.exec(
        select(TermCandidate).where(TermCandidate.source_document_id == document_id)
    ).all()
    examples = {}
    for candidate in candidates:
        snippet = _sanitize_meta_value(candidate.context_snippet)
        if snippet:
            examples.setdefault(candidate.glossary_id, set()).add(snippet)
    for glossary_id, snippets in examples.items():
        concepts = select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == glossary_id)
        for term in session.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(concepts))):
            if term.context_example in snippets:
                term.context_example = ""
                session.add(term)
        for entry in session.exec(
            select(GlossaryEntry).where(GlossaryEntry.glossary_id == glossary_id)
        ):
            for field in ("meta", "meta_overrides"):
                values = dict(getattr(entry, field) or {})
                if values.get("usage_example") in snippets:
                    # A tombstone prevents a later projection restoring the example.
                    if field == "meta_overrides":
                        values["usage_example"] = None
                    else:
                        values.pop("usage_example")
                    setattr(entry, field, values or None)
                    session.add(entry)
    for candidate in candidates:
        candidate.context_snippet = ""
        candidate.source_document_id = None
        session.add(candidate)
    session.flush()
    return len(candidates)


def erase_lab_snapshots(
    session: Session,
    document_id: int,
    *,
    erase: bool,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> dict:
    """Privacy erasure overrides snapshot independence, even with Lab disabled.

    Use subqueries instead of unbounded IN lists. Delete deepest children first;
    FK enforcement prevents a concurrent worker from restoring erased run results.
    No commit: failure rolls back with the source document's erasure.
    Plain provenance survives an ordinary deletion, so a later erasure can still
    find snapshots even when the source document no longer exists.
    """
    datasets = select(EvalDataset.id).where(EvalDataset.source_document_id == document_id)
    if created_after is not None:
        # IDs can be reused, but a document may legitimately move projects.
        datasets = datasets.where(EvalDataset.created_at >= created_after)
    if created_before is not None:
        datasets = datasets.where(EvalDataset.created_at < created_before)
    count = len(session.exec(datasets).all())
    counts = {"lab_datasets_erased": 0, "lab_datasets_preserved": count}
    if not erase or not count:
        return counts
    items = select(EvalDatasetItem.id).where(EvalDatasetItem.dataset_id.in_(datasets))
    runs = select(EvalRun.id).where(EvalRun.dataset_id.in_(datasets))
    run_items = select(EvalRunItem.id).where(
        or_(EvalRunItem.run_id.in_(runs), EvalRunItem.dataset_item_id.in_(items))
    )
    gold = select(QualityGoldItem.id).where(QualityGoldItem.dataset_item_id.in_(items))
    for model, condition in (
        (QualityGoldLabel, QualityGoldLabel.gold_item_id.in_(gold)),
        (QualityGoldItem, QualityGoldItem.dataset_item_id.in_(items)),
        (EvalScore, or_(EvalScore.run_id.in_(runs), EvalScore.run_item_id.in_(run_items))),
        (EvalRunItem, EvalRunItem.id.in_(run_items)),
        (EvalRun, EvalRun.id.in_(runs)),
        (EvalDatasetItem, EvalDatasetItem.id.in_(items)),
        (EvalDataset, EvalDataset.id.in_(datasets)),
    ):
        session.exec(delete(model).where(condition).execution_options(synchronize_session=False))
    session.flush()
    counts.update(lab_datasets_erased=count, lab_datasets_preserved=0)
    return counts


def erase_aged_orphan_snapshots(session: Session, cutoff: datetime) -> int:
    """Retention backstop for ordinary deletions, including reused document IDs.

    A snapshot predating the current row with its source ID belongs to a deleted
    incarnation. Only aged snapshots are removed; uploaded datasets have no source.
    The caller owns the transaction.
    """
    sources = session.exec(
        select(EvalDataset.source_document_id)
        .where(EvalDataset.source_document_id.is_not(None), EvalDataset.created_at < cutoff)
        .distinct()
    ).all()
    count = 0
    for source_id in sources:
        doc = session.get(Document, source_id)
        before = min(cutoff, doc.created_at) if doc else cutoff
        count += erase_lab_snapshots(session, source_id, erase=True, created_before=before)[
            "lab_datasets_erased"
        ]
    return count
