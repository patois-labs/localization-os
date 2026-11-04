"""Per-run read caches for the translate hot path (S4 query diet, ADR 0093).

A plain translate job used to re-read the same rows for every segment: the glossary entries of
the run's locale pair, the active LLMemory entries of the customer, the senses of a matched
term. Those reads return the same rows for the whole run, so the translate path now reads them
once per run into a plain dict the run owns (created in `_pretranslate_stream_impl`, dropped
with it) and hands that dict down as an optional `*_cache` argument. No cache argument (every
other caller) means exactly the old per-call reads.

What a cache holds is a SNAPSHOT: a transient copy of each row's column values, attached to no
session. The run commits after every segment and a commit expires every ORM object in the
session, so caching the live objects would re-load each of them on its next attribute read (one
SELECT per row per segment, worse than before). A snapshot is read-only data; nothing writes
through it (usage stamps go through ids, e.g. llmemory.mark_used_ids).

The trade, accepted in ADR 0093: an edit to a glossary or a memory made WHILE a run is
translating reaches that run's next job instead of its next segment. A run already took its
glossary plan, style guide, context package and TM examples once at the start; this makes the
glossary entries and memories behave the same way.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

from sqlalchemy import inspect as _sa_inspect


def snapshot(row):
    """A transient copy of `row` carrying every column value it has, attached to no session.

    Reading a column on the copy never touches the database. Relationship attributes are not
    copied (the translate path reads columns only)."""
    if row is None:
        return None
    cls = type(row)
    values = {attr.key: getattr(row, attr.key) for attr in _sa_inspect(cls).column_attrs}
    return cls(**values)


def snapshots(rows) -> list:
    return [snapshot(r) for r in rows]


# Keys shared between the translate run that fills a cache and the envelope enrich that
# reads it after the run (tap_enrich.enrich(run_cache=...)). One function per key so the
# producer and the consumer cannot drift apart.
def style_guide_key(org_id, customer_id, content_type_id, src, tgt):
    return ("style_guide", org_id, customer_id, content_type_id, src, tgt)


def glossary_plan_key(project_id, src, tgt):
    return ("glossary_plan", project_id, src, tgt)


def glossary_matches_key(project_id, src, tgt, text):
    return ("glossary_matches", project_id, src, tgt, text)


def glossary_pins_key(project_id):
    return ("glossary_pins", project_id)


def tm_source_ids_key(project_id):
    return ("tm_source_ids", project_id)


def load_document_chain(session, doc_id) -> tuple:
    """(document, project, customer) of `doc_id` in ONE joined read on `session`, any of
    them None when missing (S4 follow-up). The rows land in the session's identity map
    exactly as three session.get calls would leave them, so later gets and the AI opt-out
    lookups on that session are served from it while the caller holds the rows."""
    from sqlalchemy import bindparam
    from sqlmodel import select

    from app import db
    from app.models import Customer, Document, Project

    stmt = db.prebuilt(
        "run_cache.document_chain",
        lambda: (
            select(Document, Project, Customer)
            .outerjoin(Project, Project.id == Document.project_id)
            .outerjoin(Customer, Customer.id == Project.customer_id)
            .where(Document.id == bindparam("doc_id"))
        ),
    )
    row = session.exec(stmt, params={"doc_id": doc_id}).first()
    if row is None:
        return (None, None, None)
    return (row[0], row[1], row[2])
