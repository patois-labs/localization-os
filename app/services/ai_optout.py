"""AI opt-out: keep a project's content away from every AI engine or model (HECVAT DPAI-08).

LEVELS
  * Project.ai_opt_out  -- this project's content never goes to AI.
  * Customer.ai_opt_out -- the same for EVERY project of the customer, current and
    future. A project cannot undo a customer opt-out. Effective = project OR customer.

Per-USER opt-out is deliberately NOT offered. A project's content is shared work: many
people upload, edit, approve and review the same segments, TM and glossary rows are
written by several hands, runs are launched by whoever is on shift, and connectors
ingest content with no person attached at all. A "do not send my content to AI" flag
on one user could only be honoured for a sliver of that content and would read as a
promise the product cannot keep. The honest, enforceable granularity is the project
(and the customer as a default): put the content that must stay human-only in an
opted-out project.

ENFORCEMENT -- ONE CHOKE POINT PLUS CLEAR REFUSALS AT THE DOORS
  1. The choke point is the engine call boundary. `guard()` runs:
       * inside EVERY Provider.complete (providers_common.Provider.__init_subclass__
         wraps each subclass's complete, so a provider added tomorrow is covered
         without anyone remembering to call anything),
       * inside EVERY Embedder.embed (same wrapping in embeddings.py),
       * at the top of providers.run_completion,
       * at the non-Provider egress seams: ocr/image vision calls, the Jev QE HTTP
         post, and providers.local_complete.
     `guard()` reads the AI SCOPE (a ContextVar) and refuses when it resolves to an
     opted-out project. No content has left the process when it raises.
  2. The scope is set WITHOUT any caller cooperation at the outer layers:
       * web + API: a pure ASGI middleware (`AIScopeMiddleware`) records the project /
         document / run / segment ids found in the URL. It reads no database; the ids
         are resolved lazily, only when an AI call is actually about to happen.
       * the workflow run seam (orchestrator.run_workflow_stream) scopes each advance
         to the run's document, so every launcher (web, CLI, batch, resume) is covered.
       * the job worker scopes each job to its document / project.
       * thread pools that fan out AI calls submit through `submit()` so the scope
         travels to the worker thread (a ThreadPoolExecutor does not copy context).
  3. The doors refuse early and plainly (`refuse()`): a workflow with an AI step
     (llm_agent / lqa) is refused at launch and at the run seam; the MT job, the
     Editor's MT / alternatives / AI tag repair, LQA and QE runs, context/style A/B,
     glossary suggestion, image OCR, and advanced runs refuse before any work. TM and
     glossary LOOKUPS are pure database reads and keep working; human-only workflows
     (human_review, tm_match, external vendor steps) keep working.

`AIOptOutRefused` subclasses ValueError, so every launch surface that already turns a
ValueError into a clean 400 / usage error does so here too; main.py registers a handler
that answers 409 (JSON on /api, a plain page on the web) for any refusal that reaches a
route from deeper down.

Documented in: docs/architecture/content-and-projects.md
"""

from __future__ import annotations

import contextvars
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

log = logging.getLogger("loc_tms.ai_optout")

#: Workflow step types that call an AI engine. human_review, tm_match and external
#: (vendor) steps never do, so a workflow made only of those is "human-only".
AI_STEP_TYPES = ("llm_agent", "lqa")


class AIOptOutRefused(ValueError):
    """An AI call or AI feature was refused because the project opted out of AI."""

    status_code = 409

    def __init__(self, feature: str, project_id=None, project_name: str = "", via: str = ""):
        self.feature = feature or "This AI feature"
        self.project_id = project_id
        self.project_name = project_name or ""
        self.via = via or "project"
        super().__init__(self._text())

    def _text(self) -> str:
        if self.via == "unknown":
            return (
                f"Could not confirm that AI is allowed for the project this content came "
                f"from, so {self.feature} was not run. No content was sent to any AI engine."
            )
        who = f"project {self.project_id}" if self.project_id is not None else "this project"
        if self.project_name:
            who += f" ({self.project_name})"
        where = "for its customer" if self.via == "customer" else "for this project"
        return (
            f"AI is switched off {where}, so {self.feature} was not run for {who}. "
            "No content was sent to any AI engine. Use a human-only workflow (human "
            "review, TM match or an external vendor step), or ask an admin or manager "
            "to switch AI back on."
        )


# --------------------------------------------------------------------------- state
def _project_row(session, project_or_id):
    from app.models import Project

    if project_or_id is None:
        return None
    if isinstance(project_or_id, Project):
        return project_or_id
    try:
        return session.get(Project, int(project_or_id))
    except (TypeError, ValueError):
        return None


def opt_out_source(session, project_or_id) -> str | None:
    """'project' / 'customer' when AI is off for this project (and why), else None."""
    from app.models import Customer

    project = _project_row(session, project_or_id)
    if project is None:
        return None
    if bool(getattr(project, "ai_opt_out", False)):
        return "project"
    cid = getattr(project, "customer_id", None)
    if cid:
        cust = session.get(Customer, cid)
        if cust is not None and bool(getattr(cust, "ai_opt_out", False)):
            return "customer"
    return None


def is_opted_out(session, project_or_id) -> bool:
    return opt_out_source(session, project_or_id) is not None


def opted_out_project_ids(session) -> set:
    """Every project id whose AI is off, directly or through its customer."""
    from sqlmodel import select

    from app.models import Customer, Project

    ids = set(session.exec(select(Project.id).where(Project.ai_opt_out.is_(True))).all())
    cust_ids = set(session.exec(select(Customer.id).where(Customer.ai_opt_out.is_(True))).all())
    if cust_ids:
        ids |= set(session.exec(select(Project.id).where(Project.customer_id.in_(cust_ids))).all())
    return ids


def refuse(session, project_or_id, feature: str) -> None:
    """The entry-point refusal: raise AIOptOutRefused when the project opted out."""
    project = _project_row(session, project_or_id)
    if project is None:
        return
    via = opt_out_source(session, project)
    if via:
        raise AIOptOutRefused(feature, project.id, project.name, via)


def refuse_for_document(session, document_or_id, feature: str) -> None:
    from app.models import Document

    doc = document_or_id
    if doc is not None and not isinstance(doc, Document):
        try:
            doc = session.get(Document, int(doc))
        except (TypeError, ValueError):
            doc = None
    if doc is not None:
        refuse(session, getattr(doc, "project_id", None), feature)


def refuse_for_eval_dataset(session, dataset_id, feature: str = "an Evaluations Lab run"):
    """Refuse an engine run over a Lab dataset SNAPSHOTTED from an opted-out project.

    The snapshot records its source project and customer (EvalDataset.source_project_id /
    source_customer_id, stamped at snapshot time), so the answer does not depend on the
    source document still existing. FAILS CLOSED on anything it cannot attribute:
      * the stored source project or customer no longer exists (its opt-out state is
        unknowable), or was created AFTER the dataset (the id was reused by an unrelated
        row, so it says nothing about where the snapshot came from);
      * a legacy row (source_document_id but no stored project) whose document is gone, or
        whose document was created AFTER the dataset (the id was reused by an unrelated
        document, so it says nothing about where the snapshot came from).
    The dataset's own customer (ds.customer_id) opting out refuses too. A synthetic or
    uploaded dataset with no source document has no project to protect."""
    from app.models import Customer, Document, EvalDataset, Project

    try:
        ds = session.get(EvalDataset, int(dataset_id))
    except (TypeError, ValueError):
        ds = None
    if ds is None:
        return

    def _cust_off(cid) -> bool:
        cust = session.get(Customer, cid) if cid else None
        return cust is not None and bool(getattr(cust, "ai_opt_out", False))

    if _cust_off(getattr(ds, "customer_id", None)) or _cust_off(
        getattr(ds, "source_customer_id", None)
    ):
        raise AIOptOutRefused(feature, via="customer")
    created = getattr(ds, "created_at", None)

    def _newer(row) -> bool:
        # A row created AFTER the snapshot cannot be its source: the id was reused
        # (SQLite hands a deleted highest id to the next insert). All created_at columns
        # are naive UTC, so the comparison is safe.
        rc = getattr(row, "created_at", None)
        return created is not None and rc is not None and rc > created

    for cid in (ds.customer_id, ds.source_customer_id):
        if cid is not None:
            cust = session.get(Customer, cid)
            if cust is None or _newer(cust):
                raise AIOptOutRefused(feature, via="unknown")
    pid = getattr(ds, "source_project_id", None)
    src_doc = getattr(ds, "source_document_id", None)
    if pid is not None:
        proj = session.get(Project, pid)
        if proj is None or _newer(proj):
            raise AIOptOutRefused(feature, via="unknown")
        refuse(session, pid, feature)
        return
    if src_doc is None:
        return
    doc = session.get(Document, src_doc)
    if doc is None or _newer(doc):
        raise AIOptOutRefused(feature, via="unknown")
    proj = session.get(Project, doc.project_id)
    if proj is None or _newer(proj):
        raise AIOptOutRefused(feature, via="unknown")
    refuse(session, doc.project_id, feature)


def workflow_ai_steps(session, workflow_id) -> list[str]:
    """The AI step types a workflow contains (empty = a human-only workflow)."""
    from sqlmodel import select

    from app.models import WorkflowStep

    if workflow_id is None:
        return []
    rows = session.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id)).all()
    return sorted({r.step_type for r in rows if (r.step_type or "llm_agent") in AI_STEP_TYPES})


def refuse_workflow(session, workflow_id, doc) -> None:
    """Refuse a run of an AI workflow on an opted-out project's document. A human-only
    workflow is allowed through."""
    if doc is None:
        return
    refuse_workflow_for_project(session, workflow_id, getattr(doc, "project_id", None))


def refuse_workflow_for_project(session, workflow_id, project_or_id) -> None:
    project = _project_row(session, project_or_id)
    if project is None or not is_opted_out(session, project):
        return
    kinds = workflow_ai_steps(session, workflow_id)
    if kinds:
        refuse(session, project, f"a workflow with AI steps ({', '.join(kinds)})")


def _drop_embeddings(session, project_ids) -> None:
    """Delete the stored semantic-TM vectors of opted-out projects. A vector embedded while
    AI was still on would otherwise keep answering semantic lookups (including another
    project's, through an attached TM source). The TM rows themselves are untouched; the
    vectors are re-created by a backfill if AI is switched back on."""
    from sqlalchemy import delete

    from app.models import SegmentEmbedding

    ids = [int(i) for i in project_ids if i is not None]
    if ids:
        session.exec(delete(SegmentEmbedding).where(SegmentEmbedding.project_id.in_(ids)))


def set_project(session, project_id, value: bool, *, actor="system", ip="", org_id=None):
    """Switch AI off (True) or back on (False) for one project. Audited (forced: a
    privacy control change is never un-audited). Returns the Project or None when it
    does not exist / is outside `org_id`.

    #73 (fail closed): a bare None `org_id` under an ACTIVE multi-org gate is a broken
    principal, not "unscoped", so it finds nothing (None). The key-verified CLI
    `--operator` door passes access.UNSCOPED for a deliberate cross-org write. With the
    gate OFF, None keeps today's unscoped behaviour."""
    from app.services import access, audit

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    project = _project_row(session, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    before = bool(project.ai_opt_out)
    project.ai_opt_out = bool(value)
    session.add(project)
    if value:
        _drop_embeddings(session, [project.id])
    session.commit()
    session.refresh(project)
    audit.log(
        actor=actor or "system",
        action="project.ai_opt_out" if value else "project.ai_opt_in",
        target=f"project {project.id} ({project.name})",
        detail=f"ai_opt_out {str(before).lower()} -> {str(bool(value)).lower()}",
        ip=ip,
        force=True,
        customer_id=project.customer_id,
    )
    return project


def set_customer(session, customer_id, value: bool, *, actor="system", ip="", org_id=None):
    """Switch AI off (True) or back on (False) for every project of a customer. Audited
    (forced). Returns the Customer or None when missing / outside `org_id`.

    #73 (fail closed): same scope rules as set_project -- a bare None under an ACTIVE
    multi-org gate finds nothing; access.UNSCOPED is the authorized operator door."""
    from app.models import Customer
    from app.services import access, audit

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    try:
        cust = session.get(Customer, int(customer_id))
    except (TypeError, ValueError):
        cust = None
    if cust is None:
        return None
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        return None
    before = bool(cust.ai_opt_out)
    cust.ai_opt_out = bool(value)
    session.add(cust)
    if value:
        from sqlmodel import select

        from app.models import Project

        _drop_embeddings(
            session, list(session.exec(select(Project.id).where(Project.customer_id == cust.id)))
        )
    session.commit()
    session.refresh(cust)
    audit.log(
        actor=actor or "system",
        action="customer.ai_opt_out" if value else "customer.ai_opt_in",
        target=f"customer {cust.id} ({cust.name})",
        detail=f"ai_opt_out {str(before).lower()} -> {str(bool(value)).lower()}",
        ip=ip,
        force=True,
        customer_id=cust.id,
    )
    return cust


def status(session, project) -> dict:
    """The read-back every surface shows: {ai_opt_out, ai_opt_out_project,
    ai_opt_out_customer}. `ai_opt_out` is the EFFECTIVE value."""
    via = opt_out_source(session, project)
    return {
        "ai_opt_out": via is not None,
        "ai_opt_out_project": bool(getattr(project, "ai_opt_out", False)),
        "ai_opt_out_customer": via == "customer",
    }


def parse_switch(raw) -> bool | None:
    """'on'/'off'/true/false/1/0 -> bool (True = AI OFF for the project); None = bad."""
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if text in ("1", "true", "yes", "on", "off-ai", "opt-out", "optout"):
        return True
    if text in ("0", "false", "no", "off", "opt-in", "optin"):
        return False
    return None


# --------------------------------------------------------------------------- scope
@dataclass
class _Scope:
    project_id: int | None = None
    document_id: int | None = None
    run_id: int | None = None
    segment_id: int | None = None
    eval_dataset_id: int | None = None
    #: a cached REFUSAL (project_id, project_name, via). Only a refusal is cached: an
    #: "allowed" answer is re-checked on every guard(), so switching AI off mid-job,
    #: mid-request, mid-stream or mid-mission stops the very next engine call.
    resolved: tuple | None = field(default=None, repr=False)


_SCOPES: contextvars.ContextVar[tuple] = contextvars.ContextVar("loc_ai_scope", default=())


def current_scopes() -> tuple:
    return _SCOPES.get()


@contextmanager
def scope(*, project_id=None, document_id=None, run_id=None, segment_id=None, eval_dataset_id=None):
    """Mark the enclosed work as belonging to a project (or a document / run / segment
    of one). Scopes STACK: guard() refuses if ANY enclosing scope is opted out."""

    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    sc = _Scope(
        _int(project_id), _int(document_id), _int(run_id), _int(segment_id), _int(eval_dataset_id)
    )
    if all(
        v is None
        for v in (sc.project_id, sc.document_id, sc.run_id, sc.segment_id, sc.eval_dataset_id)
    ):
        yield
        return
    token = _SCOPES.set((*_SCOPES.get(), sc))
    try:
        yield
    finally:
        try:
            _SCOPES.reset(token)
        except ValueError:
            # A generator closed from another context: drop our entry explicitly.
            _SCOPES.set(tuple(s for s in _SCOPES.get() if s is not sc))


def _resolve(sc: _Scope) -> tuple:
    if sc.resolved is not None:
        return sc.resolved
    from sqlmodel import Session

    from app.db import engine
    from app.models import Document, EvalDataset, Project, Segment, WorkflowRun

    out: tuple = ()
    with Session(engine) as s:
        if sc.eval_dataset_id is not None:
            # Resolve durable snapshot provenance on a fresh connection at EVERY egress,
            # including judge retries and workflow steps. Never cache an allowed policy.
            if s.get(EvalDataset, sc.eval_dataset_id) is None:
                sc.resolved = (None, "", "unknown")
                return sc.resolved
            try:
                refuse_for_eval_dataset(s, sc.eval_dataset_id)
            except AIOptOutRefused as exc:
                sc.resolved = (exc.project_id, exc.project_name, exc.via)
                return sc.resolved
        pid = sc.project_id
        doc_id = sc.document_id
        if doc_id is None and sc.run_id is not None:
            run = s.get(WorkflowRun, sc.run_id)
            doc_id = run.document_id if run is not None else None
        if doc_id is None and sc.segment_id is not None:
            seg = s.get(Segment, sc.segment_id)
            doc_id = seg.document_id if seg is not None else None
        if pid is None and doc_id is not None:
            doc = s.get(Document, doc_id)
            pid = doc.project_id if doc is not None else None
        project = s.get(Project, pid) if pid is not None else None
        if project is not None:
            via = opt_out_source(s, project)
            if via:
                out = (project.id, project.name, via)
    if out:
        sc.resolved = out
    return out


def guard(feature: str = "an AI engine call") -> None:
    """THE CHOKE POINT. Raise AIOptOutRefused when the current scope belongs to an
    opted-out project. Called before any byte of content leaves for an AI engine."""
    scopes = _SCOPES.get()
    if not scopes:
        return
    for sc in scopes:
        try:
            hit = _resolve(sc)
        except AIOptOutRefused:
            raise
        except Exception:
            # Fail CLOSED: if we cannot tell whether AI is allowed, it is not.
            log.warning("AI opt-out scope unresolved; refusing the AI call", exc_info=True)
            raise AIOptOutRefused(feature, via="unknown") from None
        if hit:
            raise AIOptOutRefused(feature, hit[0], hit[1], hit[2])


def scoped_stream(stream, **ids):
    """Drive a generator with the AI scope set during each advance (and its close), but
    never across a yield, so the consumer's own context is left untouched."""
    try:
        while True:
            with scope(**ids):
                try:
                    event = next(stream)
                except StopIteration:
                    return
            yield event
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            with scope(**ids):
                close()


def submit(pool, fn, *args, **kwargs):
    """pool.submit that carries the AI scope to the worker thread (one context copy per
    task, since a Context cannot be entered by two threads at once)."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)


def wrap_callable(fn, feature: str):
    """Wrap an engine method so guard() runs before it (used by the Provider and
    Embedder base classes on every subclass)."""
    import functools

    if getattr(fn, "_ai_optout_guarded", False):
        return fn

    @functools.wraps(fn)
    def _guarded(self, *args, **kwargs):
        guard(feature)
        return fn(self, *args, **kwargs)

    _guarded._ai_optout_guarded = True  # type: ignore[attr-defined]
    return _guarded


# --------------------------------------------------------------------------- web scope
_PATH_KINDS = {
    "projects": "project_id",
    "documents": "document_id",
    "images": "document_id",
    "runs": "run_id",
    "segments": "segment_id",
}
_PATH_ID_RE = re.compile(r"/(projects|documents|images|runs|segments)/(\d+)(?=/|$)")


def ids_from_path(path: str) -> list[tuple[str, int]]:
    """EVERY (kind, id) the URL names, in order. Each becomes its own stacked scope, so a
    route under /projects/A that also names /documents/B is refused if EITHER opted out."""
    return [(_PATH_KINDS[k], int(v)) for k, v in _PATH_ID_RE.findall(path or "")]


class AIScopeMiddleware:
    """Pure ASGI: scope each request to the project / document / run / segment its URL
    names, so any AI call a route makes (today's or tomorrow's) passes the choke point
    with project context. Reads NO database (resolution is lazy, in guard())."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope_, receive, send):
        if scope_.get("type") != "http":
            return await self.app(scope_, receive, send)
        ids = ids_from_path(scope_.get("path") or "")
        if not ids:
            return await self.app(scope_, receive, send)
        from contextlib import ExitStack

        with ExitStack() as stack:
            for key, value in ids:
                stack.enter_context(scope(**{key: value}))
            return await self.app(scope_, receive, send)
