"""Read-only workflow spend disclosure, shared by all four surfaces.

Documented in: docs/architecture/workflow-engine.md
"""

from sqlmodel import select

from app.models import Project, Segment, StepType, Workflow
from app.services import engines, orchestrator, qe_autofix, workflows
from app.services.providers import ProviderSpec


def _engine(spec):
    ref = spec.provider.lower()
    kind = engines.ref_kind(ref) or ref
    pid = engines._parse_engine_ref(ref)
    profile = engines.get_profile(pid) if pid is not None else None
    return {
        "provider": ref,
        "model": spec.model or (profile.model if profile else None),
        "priced": kind not in {"mock", "local", "jev_local"},
        "pricing": "local/free" if kind in {"mock", "local", "jev_local"} else "priced",
    }


def plan(session, workflow_id, doc, locale):
    """Caller owns visibility checks. Reuse the dispatch resolver and runtime specs.

    No provider calls or writes. Pricing describes configured engines, not a promise
    that credentials are live. Branches, retries and failed segments affect actual spend.
    """
    present = session.exec(
        select(Segment.id).where(Segment.document_id == doc.id, Segment.target_locale == locale)
    ).first()
    if present is None:
        raise ValueError(f"locale {locale!r} not on document {doc.id}.")
    project = session.get(Project, doc.project_id)
    wid = workflows.resolve_variant(
        session,
        workflow_id,
        locale,
        project.source_locale,
        doc_customer_id=getattr(project, "customer_id", None),
    )
    wf = session.get(Workflow, wid)
    warnings = []
    try:
        orchestrator._refuse_archived(session, workflow_id, doc)
    except ValueError as exc:
        reason = str(exc).replace("\u2014", "-")
        warnings.append(f"Run would be refused: {reason}")
    rows = []
    for step in orchestrator.get_steps(session, wid):
        row = {
            "step": step.name,
            "type": step.step_type,
        }
        if step.step_type in {StepType.TM_MATCH, StepType.HUMAN_REVIEW, StepType.EXTERNAL}:
            row.update(provider=None, model=None, priced=False, pricing="no engine call")
            rows.append(row)
            continue
        spec = orchestrator._spec_for_step(step)
        row.update(_engine(spec))
        if spec.fallback_provider:
            row["fallback"] = {
                **_engine(ProviderSpec(provider=spec.fallback_provider, model=spec.fallback_model)),
                "note": "Used when the step engine fails.",
            }
        if step.qe_gate:
            row["qe_judge"] = _engine(orchestrator._qe_spec_for_step(step))
        if step.qe_autofix:
            spec, reason = qe_autofix.fixer_spec(step)
            if spec is None:
                spec = ProviderSpec(
                    provider=qe_autofix.effective_fixer_ref(step.qe_autofix_provider, step.provider)
                )
            row["auto_fix"] = {
                **_engine(spec),
                "max_rounds": qe_autofix.clamp_rounds(step.qe_autofix_max_rounds),
                "cross_check_adds_calls": bool(step.qe_autofix_cross_check),
                "note": "On eligible QE failures: each round calls the post-editor and judge; "
                "cross-check adds a post-editor scoring call after each accepted rewrite.",
                "warnings": qe_autofix.configuration_warnings(step) + ([reason] if reason else []),
            }
        rows.append(row)
    return {
        "requested_workflow_id": workflow_id,
        "workflow_id": wid,
        "workflow_name": wf.name,
        "document_id": doc.id,
        "locale": locale,
        "steps": rows,
        "warnings": warnings,
        "note": "Configured step engines, fallbacks, QE judges and auto-fix post-editors; "
        "fallbacks are used when the step engine fails. "
        "Conditions, retries and loop visits affect actual spend. "
        "The confirmed run resolves the current configuration again.",
    }
