"""Headless autopilot — scripts the VENDOR side of a scenario end-to-end, no UI.

Each scenario is the vendor-side BEHAVIOR ONLY. Localization OS's reaction (admit / refuse /
no-op / drop) is asserted by the roundtrip suite, not here — autopilot just reports what
it did and Localization OS's HTTP verdict.

Run over HTTP:   POST /autopilot/run  {"scenario": "...", "project_id": N}
Run headless:    python -m vstudio.autopilot <scenario> --project-id N
                 python -m vstudio.autopilot <scenario> --contract dispatch.json
"""

from __future__ import annotations

import copy
import json

from sqlmodel import Session, select

from vstudio import contract as C
from vstudio import delivery, editor, intake
from vstudio.models import VProject, VProjectStatus, VSegment

SCENARIOS = [
    "happy_path",
    "partial_2of3",
    "bad_id_missing",
    "bad_id_extra",
    "bad_id_foreign",
    "source_echo",
    "blank_targets",
    "silence",
    "late_after_cancel",
    "wrong_claim",
    "bad_signature",
    "replay",
]


class AutopilotError(RuntimeError):
    pass


def _segs(session: Session, proj: VProject) -> list:
    return list(
        session.exec(
            select(VSegment).where(VSegment.project_id == proj.id).order_by(VSegment.order_index)
        ).all()
    )


def _translate(seg: VSegment) -> str:
    """A deterministic, QA-clean pseudo-translation that preserves placeholders/numbers
    and is NOT a source echo."""
    return f"[{seg.source or ''}]"


def _fill_confirm(session: Session, proj: VProject, segs) -> None:
    """Edit + confirm each given non-locked segment with a clean pseudo-translation."""
    for seg in segs:
        if seg.locked:
            continue
        editor.patch_target(session, proj, seg, _translate(seg), actor="autopilot")
        editor.confirm_segment(session, proj, seg, actor="autopilot")


def _fill_clean(session: Session, proj: VProject) -> None:
    """Edit + confirm every non-locked segment with a clean pseudo-translation."""
    _fill_confirm(session, proj, _segs(session, proj))


def _resp(r) -> dict:
    try:
        body = r.json()
        body = body if isinstance(body, dict) else {"raw": str(body)[:200]}
    except (ValueError, TypeError):
        body = {"raw": (getattr(r, "text", "") or "")[:200]}
    return {"status_code": r.status_code, "body": body}


def run_scenario(
    session: Session, scenario: str, *, project_id: int | None = None, poster=None
) -> dict:
    """Run one scenario against a stored project. `poster` (test seam) defaults to
    delivery.post_return."""
    if scenario not in SCENARIOS:
        raise AutopilotError(f"unknown scenario {scenario!r}")
    poster = poster or delivery.post_return
    proj = _resolve_project(session, project_id)
    ids = [s.contract_id for s in _segs(session, proj)]

    if scenario == "happy_path":
        _fill_clean(session, proj)
        out = delivery.deliver(session, proj, mode="full", poster=poster, cost=12.5)
        return {"scenario": scenario, "delivered": True, "responses": [out]}

    if scenario == "partial_2of3":
        segs = _segs(session, proj)
        if len(segs) < 2:
            raise AutopilotError("partial_2of3 needs at least 2 segments.")
        # Confirm the first N-1, deliver that confirmed subset; then confirm the last and
        # deliver the completing tranche. The delivered set is server-chosen (confirmed).
        _fill_confirm(session, proj, segs[:-1])
        first = delivery.deliver(session, proj, mode="partial", poster=poster)
        _fill_confirm(session, proj, segs[-1:])
        second = delivery.deliver(session, proj, mode="partial", poster=poster, cost=20.0)
        return {"scenario": scenario, "delivered": True, "responses": [first, second]}

    if scenario == "bad_id_missing":
        # Confirm + deliver all but the last id: a valid PARTIAL that leaves the leg open
        # (not a refusal). The undelivered id is simply never confirmed.
        segs = _segs(session, proj)
        _fill_confirm(session, proj, segs[:-1])
        out = delivery.deliver(session, proj, mode="partial", poster=poster)
        return {"scenario": scenario, "delivered": True, "responses": [out]}

    if scenario in ("bad_id_extra", "bad_id_foreign"):
        _fill_clean(session, proj)
        payload = delivery.build_return_payload(session, proj, ids)
        bogus = "999999999" if scenario == "bad_id_extra" else "foreign:leg:0"
        payload["segments"].append({"id": bogus, "target": "intruder"})
        r = poster(payload, proj.claim_token)
        return {
            "scenario": scenario,
            "delivered": True,
            "corrupt_id": bogus,
            "responses": [_resp(r)],
        }

    if scenario == "source_echo":
        # Echo the SOURCE back for every segment (Localization OS no-ops non-en targets).
        payload = {
            "contract_version": C.CONTRACT_VERSION,
            "claim_token": proj.claim_token,
            "target_locale": proj.target_locale,
            "segments": [
                {"id": s.contract_id, "target": s.source or ""} for s in _segs(session, proj)
            ],
        }
        r = poster(payload, proj.claim_token)
        return {"scenario": scenario, "delivered": True, "responses": [_resp(r)]}

    if scenario == "blank_targets":
        payload = {
            "contract_version": C.CONTRACT_VERSION,
            "claim_token": proj.claim_token,
            "target_locale": proj.target_locale,
            "segments": [{"id": s.contract_id, "target": ""} for s in _segs(session, proj)],
        }
        r = poster(payload, proj.claim_token)
        return {"scenario": scenario, "delivered": True, "responses": [_resp(r)]}

    if scenario == "silence":
        # The vendor never returns anything.
        return {"scenario": scenario, "delivered": False, "responses": []}

    if scenario == "late_after_cancel":
        _fill_clean(session, proj)
        # Force the project CANCELLED locally, then POST anyway (bypassing deliver()'s UI
        # guard) so the roundtrip can prove Localization OS DROPS a late return on a closed leg.
        proj.status = VProjectStatus.CANCELLED
        session.add(proj)
        session.commit()
        payload = delivery.build_return_payload(session, proj, ids)
        r = poster(payload, proj.claim_token)
        return {"scenario": scenario, "delivered": True, "responses": [_resp(r)]}

    if scenario == "wrong_claim":
        _fill_clean(session, proj)
        payload = delivery.build_return_payload(
            session, proj, ids, claim_override="not-a-real-claim-token"
        )
        r = poster(payload, "not-a-real-claim-token")
        return {"scenario": scenario, "delivered": True, "responses": [_resp(r)]}

    if scenario == "bad_signature":
        _fill_clean(session, proj)
        payload = delivery.build_return_payload(session, proj, ids)
        r = poster(payload, proj.claim_token, sign=False)
        return {"scenario": scenario, "delivered": True, "responses": [_resp(r)]}

    if scenario == "replay":
        _fill_clean(session, proj)
        payload = delivery.build_return_payload(session, proj, ids, cost=5.0)
        first = poster(payload, proj.claim_token)
        second = poster(copy.deepcopy(payload), proj.claim_token)  # exact replay
        return {"scenario": scenario, "delivered": True, "responses": [_resp(first), _resp(second)]}

    raise AutopilotError(f"scenario {scenario!r} not implemented")  # pragma: no cover


def _resolve_project(session: Session, project_id: int | None) -> VProject:
    if project_id is not None:
        proj = session.get(VProject, project_id)
        if proj is None:
            raise AutopilotError(f"no project {project_id}")
        return proj
    # Default: the most recent still-open project.
    proj = session.exec(
        select(VProject)
        .where(VProject.status != VProjectStatus.DELIVERED)
        .order_by(VProject.created_at.desc())
    ).first()
    if proj is None:
        raise AutopilotError("no open project to run a scenario against.")
    return proj


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse

    from vstudio.db import get_session, init_db

    ap = argparse.ArgumentParser(
        prog="vstudio.autopilot", description="Run a Vendor Studio autopilot scenario."
    )
    ap.add_argument("scenario", choices=SCENARIOS)
    ap.add_argument("--project-id", type=int, default=None)
    ap.add_argument(
        "--contract",
        default="",
        help="a dispatch contract JSON file to intake first (offline seed).",
    )
    args = ap.parse_args(argv)

    init_db()
    with get_session() as s:
        pid = args.project_id
        if args.contract:
            with open(args.contract, encoding="utf-8") as fh:
                payload = json.load(fh)
            proj = intake.store_dispatch(s, payload)
            pid = proj.id
        try:
            out = run_scenario(s, args.scenario, project_id=pid)
        except AutopilotError as exc:
            print(f"autopilot error: {exc}")
            return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
