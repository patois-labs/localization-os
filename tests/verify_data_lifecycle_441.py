"""Issue #441 decision (the maintainer): LLMemory learned from a deleted project STAYS on a plain
project
delete, but a privacy ERASURE of the project purges the entries, events and suggestions learned
from it (and customer-wide entries that rested only on its events).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII-only prints.
Final line: DATA LIFECYCLE 441 OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_data_lifecycle_441_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    MemoryEntry,
    MemoryEvent,
    MemoryEvidence,
    MemoryStatus,
    MemorySuggestion,
    MemorySuggestionEvidence,
    Project,
    Workflow,
)
from app.services import content

init_db()


def _seed(tag):
    """One customer, one project, with an entry, an event, a customer-wide entry resting only
    on that event, and a suggestion, all carrying a distinctive marker."""
    with Session(engine) as s:
        c = Customer(name="Cust441" + tag)
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(name="P441" + tag, customer_id=c.id, source_locale="en-US")
        wf = Workflow(name="wf441" + tag)
        s.add(p)
        s.add(wf)
        s.commit()
        s.refresh(p)
        s.refresh(wf)
        ev = MemoryEvent(
            customer_id=c.id,
            project_id=p.id,
            before_text="BEFORE-" + tag,
            after_text="AFTER-" + tag,
        )
        s.add(ev)
        s.commit()
        s.refresh(ev)
        scoped = MemoryEntry(
            kind="project_note",
            status=MemoryStatus.ACTIVE,
            customer_id=c.id,
            project_id=p.id,
            body="SCOPED-" + tag,
        )
        wide = MemoryEntry(
            kind="project_note",
            status=MemoryStatus.ACTIVE,
            customer_id=c.id,
            project_id=None,
            body="WIDE-" + tag,
        )
        s.add(scoped)
        s.add(wide)
        s.commit()
        s.refresh(scoped)
        s.refresh(wide)
        s.add(MemoryEvidence(memory_id=wide.id, event_id=ev.id))
        sug = MemorySuggestion(
            kind="x",
            customer_id=c.id,
            project_id=p.id,
            workflow_id=wf.id,
            rationale="SUGG-" + tag,
        )
        s.add(sug)
        s.commit()
        s.refresh(sug)
        s.add(MemorySuggestionEvidence(suggestion_id=sug.id, event_id=ev.id))
        s.commit()
        return {"pid": p.id, "ev": ev.id, "scoped": scoped.id, "wide": wide.id, "sug": sug.id}


def test_plain_delete_keeps_memory():
    f = _seed("keep")
    with Session(engine) as s:
        content.delete_project(s, f["pid"], actor="t")
    with Session(engine) as s:
        scoped = s.get(MemoryEntry, f["scoped"])
        assert scoped.body == "SCOPED-keep" and scoped.status == MemoryStatus.ACTIVE, (
            "a plain project delete keeps the entry"
        )
        assert scoped.project_id is None, "the kept entry is un-scoped to customer-wide"
        assert s.get(MemoryEntry, f["wide"]).body == "WIDE-keep"
        assert s.get(MemorySuggestion, f["sug"]).rationale == "SUGG-keep"
    print("  [1] a plain project delete keeps LLMemory (un-scoped) OK")


def test_erasure_purges_memory():
    f = _seed("erase")
    with Session(engine) as s:
        counts = content.delete_project(s, f["pid"], actor="t", privacy_erasure=True)
    assert counts is not None
    # Open core (no LLMemory add-on): the fallback deletes the project's events (with their
    # citation joins), the entries and suggestions scoped to it, and anything resting only on
    # those events, outright.
    with Session(engine) as s:
        assert s.get(MemoryEntry, f["scoped"]) is None, "scoped entry erased"
        assert s.get(MemoryEvent, f["ev"]) is None, "the project's event is deleted"
        assert s.get(MemorySuggestion, f["sug"]) is None, "the suggestion is erased"
        assert not s.exec(
            select(MemorySuggestionEvidence).where(
                MemorySuggestionEvidence.suggestion_id == f["sug"]
            )
        ).all()
        assert not s.exec(select(MemoryEvidence).where(MemoryEvidence.event_id == f["ev"])).all(), (
            "the citation joins to the event are gone"
        )
        # a customer-wide entry resting ONLY on the project's events goes with them
        assert s.get(MemoryEntry, f["wide"]) is None, (
            "a customer-wide entry resting only on the project's events is erased"
        )
    print("  [2] privacy erasure of a project erases its leftover LLMemory rows OK")


if __name__ == "__main__":
    failures = []
    for t in (test_plain_delete_keeps_memory, test_erasure_purges_memory):
        try:
            t()
        except Exception as exc:
            failures.append((t.__name__, repr(exc)))
    assert not failures, failures
    print("DATA LIFECYCLE 441 OK")
