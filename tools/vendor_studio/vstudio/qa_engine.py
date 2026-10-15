"""QA service layer: run the pure checks over a project, apply editable severities, persist
per-finding dismissals (bookkeeping), and summarize the findings for the UI.

DECISION RECORD — QA IS ADVISORY-ONLY (decided 2026-08-08, from live testing; this OVERRIDES
the prior Phrase-style blocking-gate design that shipped in this file):

  1. "There's a QA warning if you do not change the translation at all that prevents you
     from submitting" -> the `unedited_mt` check is REMOVED entirely (gone from the check
     registry, from findings, and from the settings page). "It's actually good if no change
     was made because that means the MT is good."
  2. "QA warnings should never prevent you from submitting... don't let QA checks block
     delivery" -> the QA gate is ADVISORY-ONLY. Findings are still computed and rendered
     (per-row icons, findings pane, click-to-jump, and the ignore-with-reason flow kept for
     bookkeeping), but Complete and Deliver NEVER block on findings: no 409s, no unignorable
     check, no dismissal requirement. Delivery always proceeds for a claimed/in-progress
     project regardless of findings.

WHY THIS IS SAFE (architectural note): Localization OS's admission seam is the ENFORCING side.
Its tag-integrity / glossary / source-echo lanes flag `needs_review` on admit
(`app/services/quality.commit_mt_output`), so a placeholder-broken or glossary-violating
vendor return is accepted AND routed to a human on the BUYER side. Vendor-side QA is context
for the human; the buyer-side guards are the contract. `gate()` below therefore no longer
returns ok/blocking semantics — it returns an informational findings SUMMARY for the "N open
QA findings — deliver anyway?" banner, and `delivery.deliver()` records that summary as an
audit-trail snapshot on the delivery instead of refusing.
"""

from __future__ import annotations

from sqlmodel import Session, select

from vstudio import qa
from vstudio.models import (
    VCheckConfig,
    VProject,
    VQAIgnore,
    VSegment,
)


def severities(session: Session) -> dict:
    """check_key -> severity, from the editable config (falling back to defaults)."""
    out = dict(qa.DEFAULT_SEVERITIES)
    for row in session.exec(select(VCheckConfig)).all():
        out[row.check_key] = row.severity
    return out


def set_severity(session: Session, check_key: str, severity: str) -> None:
    if check_key not in qa.ALL_CHECKS:
        raise ValueError(f"unknown check {check_key!r}.")
    if severity not in ("warning", "error"):
        raise ValueError("severity must be 'warning' or 'error'.")
    row = session.exec(select(VCheckConfig).where(VCheckConfig.check_key == check_key)).first()
    if row is None:
        row = VCheckConfig(check_key=check_key, severity=severity)
    else:
        row.severity = severity
    session.add(row)
    session.commit()


def _ignored_set(session: Session, project_id: int) -> set:
    return {
        (r.segment_id, r.check_key, r.detail_sig)
        for r in session.exec(select(VQAIgnore).where(VQAIgnore.project_id == project_id)).all()
    }


def _seg_dict(seg: VSegment, locale: str) -> dict:
    return {
        "source": seg.source,
        "target": seg.target,
        "target_locale": locale,
        "placeholders": seg.placeholders,
        "char_limit": seg.char_limit,
        "provenance": seg.provenance,
        "prefill": seg.prefill,
        "locked": seg.locked,
    }


def run_findings(session: Session, proj: VProject, only_ids: set | None = None) -> dict:
    """segment_id -> list of finding dicts, each enriched with severity + ignored. Locked
    segments contribute nothing (excluded). `only_ids` scopes the run to a subset of segment
    ids (used to summarize a partial-delivery tranche)."""
    segs = list(
        session.exec(
            select(VSegment).where(VSegment.project_id == proj.id).order_by(VSegment.order_index)
        ).all()
    )
    if only_ids is not None:
        segs = [s for s in segs if s.id in only_ids]
    sev = severities(session)
    ignored = _ignored_set(session, proj.id)

    # Per-segment checks. LOCKED rows are excluded entirely (the industry pattern —
    # Xbench/memoQ exclude locked from QA scoping): they are read-only and returned
    # as-dispatched, so a finding would be unactionable, and under Localization OS's
    # omit_locked_targets a locked row arrives BLANK and must return blank — empty_target /
    # target==source must never fire on it and block a valid package. check_segment ALSO
    # guards this; skipping here makes the gate-layer exclusion explicit + refactor-proof.
    per_seg: dict = {s.id: [] for s in segs}
    for s in segs:
        if s.locked:
            continue
        for f in qa.check_segment(_seg_dict(s, proj.target_locale), proj.glossary):
            per_seg[s.id].append(_decorate(f, s.id, sev, ignored))

    # Job-level consistency check.
    job = qa.check_job_consistency(
        [{"source": s.source, "target": s.target, "locked": s.locked} for s in segs]
    )
    for idx, findings in job.items():
        sid = segs[idx].id
        for f in findings:
            per_seg[sid].append(_decorate(f, sid, sev, ignored))
    return per_seg


def _decorate(f: dict, segment_id, sev: dict, ignored: set) -> dict:
    check = f["check"]
    # QA is advisory-only: every finding is dismissable for bookkeeping (no unignorable
    # check). `ignored` records whether a human has waved this one through.
    is_ignored = (segment_id, check, f.get("detail_sig", "")) in ignored
    return {
        **f,
        "segment_id": segment_id,
        "severity": sev.get(check, "warning"),
        "ignored": is_ignored,
    }


def ignore_finding(
    session: Session, proj: VProject, segment_id: int, check_key: str, detail_sig: str, reason: str
) -> None:
    """Record a per-finding dismissal (ignore-with-reason). This is pure BOOKKEEPING now:
    QA is advisory, so a dismissal only annotates a finding as human-reviewed — it never
    unblocks anything (nothing was ever blocked). ANY finding may be dismissed (there is no
    unignorable check and no error-must-be-fixed rule)."""
    row = VQAIgnore(
        project_id=proj.id,
        segment_id=segment_id,
        check_key=check_key,
        detail_sig=detail_sig or "",
        reason=reason or "",
    )
    session.add(row)
    session.commit()


def clear_ignore(
    session: Session, proj: VProject, segment_id: int, check_key: str, detail_sig: str
) -> None:
    for r in session.exec(
        select(VQAIgnore).where(
            VQAIgnore.project_id == proj.id,
            VQAIgnore.segment_id == segment_id,
            VQAIgnore.check_key == check_key,
            VQAIgnore.detail_sig == (detail_sig or ""),
        )
    ).all():
        session.delete(r)
    session.commit()


def gate(session: Session, proj: VProject, only_ids: set | None = None) -> dict:
    """ADVISORY findings SUMMARY (decided 2026-08-08: QA never blocks delivery — see the
    decision record at the top of this module). This is deliberately NOT a gate: there is no
    ok/blocking verdict, and no route or delivery path refuses on its result. It returns the
    decorated findings plus counts for the UI banner ("N open QA findings — deliver anyway?")
    and for the audit-trail snapshot recorded on a delivery. `only_ids` scopes the summary to
    a tranche of segments (partial delivery)."""
    findings = run_findings(session, proj, only_ids=only_ids)
    flat = [f for flist in findings.values() for f in flist]
    open_findings = [f for f in flat if not f.get("ignored")]
    return {"total": len(flat), "open": len(open_findings), "findings": flat}
