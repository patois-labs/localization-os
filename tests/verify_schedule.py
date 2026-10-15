"""Scheduled connector syncs verification (offline).

- POST /connectors/connection/{id}/schedule stores the interval; first run
  is one full interval out (never immediate)
- scheduler.tick() fires a due schedule exactly once, as a normal
  connector_translate job attributed to "scheduler", and audits the fire
- overlap guard: a queued/running sync defers the schedule instead of
  stacking a second run
- catch-up: a long-overdue schedule fires ONCE, not once per missed slot
- global kill switch sync_scheduler_enabled=0 pauses all fires
- the settings form no longer wipes non-spec config keys (language_set_id,
  sync_*) when saved -- proving the server-side config merge
- interval floor: a nonzero interval is stored at >= MIN_EVERY_MINUTES
- disable-resurrection guard: _save_state cannot re-add sync_next_at to a
  schedule that was turned off
- audit-failure safety: a raising audit.log leaves exactly one fire with the
  slot consumed, never a double-fire
- submit_unique dedup: an active connector_translate run makes both the
  scheduler and the manual sync route reuse the live job
- consume-slot-first: sync_next_at is advanced before the job is submitted
- reserved-email guard: security.create_user rejects system identities like a
  duplicate email
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Naive UTC now, matching how the app stores timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_sched_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Job, JobStatus, Role
from app.services import audit, jobs, scheduler, security, settings
from app.services.connectors import store as cstore


def wait_job(c, jid, timeout=30):
    deadline = time.time() + timeout
    d = {}
    while time.time() < deadline:
        d = c.get(f"/jobs/{jid}/status").json()
        if d.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED):
            return d
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish in {timeout}s: {d}")


def cfg_of(cid):
    return dict((cstore.get(cid).config) or {})


def main() -> None:
    init_db()
    c = TestClient(app)

    # scheduled fires run the real job handler -> stub out the live-system call
    real_translate_now = cstore.translate_now
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda cid, actor="", transport=None: (
            True,
            "ok: 3 items",
            {"translated": 3},
        ),
    )
    try:
        conn = cstore.create(
            "airtable", "Sched test", {"source_field": "Notes"}, {"api_key": "pat_x"}, actor="test"
        )
        cid = conn.id

        # --- enable via the route: first run is one interval OUT, never now ---
        r = c.post(
            f"/connectors/connection/{cid}/schedule",
            data={"every_minutes": "60"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        cfg = cfg_of(cid)
        assert cfg["sync_every_minutes"] == 60, cfg
        nxt = datetime.fromisoformat(cfg["sync_next_at"])
        delta_min = (nxt - utcnow()).total_seconds() / 60
        assert 58 <= delta_min <= 61, f"first run must be ~60 min out, got {delta_min:.1f}"
        assert scheduler.tick() == [], "not due yet -> nothing may fire"
        print("  enable: every_minutes=60 stored; first run in ~60 min; tick() no-op")

        # --- due -> exactly one fire, attributed + audited, state advanced ---
        scheduler._save_state(cid, next_at=utcnow() - timedelta(seconds=5))
        fired = scheduler.tick()
        assert len(fired) == 1, fired
        d = wait_job(c, fired[0])
        assert d["status"] == JobStatus.COMPLETED and d["result"]["translated"] == 3, d
        with Session(engine) as s:
            job = s.get(Job, fired[0])
        assert job.created_by == "scheduler", job.created_by
        assert (job.payload or {}).get("connection_id") == cid
        cfg = cfg_of(cid)
        assert cfg["sync_last_job_id"] == fired[0], cfg
        assert datetime.fromisoformat(cfg["sync_next_at"]) > utcnow(), cfg
        assert audit.count(action="connector.scheduled_sync") == 1
        assert scheduler.tick() == [], "just rescheduled -> must not fire again"
        print("  due fire: 1 job, created_by=scheduler, audited, next_at pushed forward")

        # --- overlap guard: active sync defers instead of stacking ---
        scheduler._save_state(cid, next_at=utcnow() - timedelta(seconds=5))
        with Session(engine) as s:
            stuck = Job(
                kind="connector_translate",
                status=JobStatus.QUEUED,
                payload={"connection_id": cid, "actor": "x"},
            )
            s.add(stuck)
            s.commit()
            s.refresh(stuck)
            stuck_id = stuck.id
        assert scheduler.tick() == [], "busy connection must be skipped"
        assert datetime.fromisoformat(cfg_of(cid)["sync_next_at"]) > utcnow(), (
            "skipped schedule must still be pushed forward"
        )
        with Session(engine) as s:
            j = s.get(Job, stuck_id)
            j.status = JobStatus.COMPLETED
            s.add(j)
            s.commit()
        print("  overlap guard: active sync -> skip + reschedule, no stacked run")

        # --- global kill switch ---
        scheduler._save_state(cid, next_at=utcnow() - timedelta(seconds=5))
        settings.set_many({"sync_scheduler_enabled": "0"})
        assert scheduler.tick() == [], "kill switch must pause all fires"
        settings.set_many({"sync_scheduler_enabled": "1"})
        fired = scheduler.tick()
        assert len(fired) == 1
        wait_job(c, fired[0])
        print("  kill switch: sync_scheduler_enabled=0 pauses; re-enable fires")

        # --- catch-up: 10h overdue on a 60-min schedule -> ONE fire, not ten ---
        scheduler._save_state(cid, next_at=utcnow() - timedelta(hours=10))
        fired = scheduler.tick()
        assert len(fired) == 1, "overdue schedule must fire exactly once"
        wait_job(c, fired[0])
        nxt = datetime.fromisoformat(cfg_of(cid)["sync_next_at"])
        delta_min = (nxt - utcnow()).total_seconds() / 60
        assert 58 <= delta_min <= 61, (
            f"catch-up must resume a full interval out, got {delta_min:.1f}"
        )
        assert scheduler.tick() == []
        print("  catch-up: long-overdue fires once; next slot a full interval out")

        # --- settings form must not wipe non-spec config keys (the old bug) ---
        c.post(
            f"/connectors/connection/{cid}/languages",
            data={"language_set_id": "", "target_locales": "es-MX, de-DE"},
            follow_redirects=False,
        )
        r = c.post(
            f"/connectors/connection/{cid}",
            data={"name": "Sched test", "source_field": "Notes"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        cfg = cfg_of(cid)
        assert cfg.get("sync_every_minutes") == 60, "settings save wiped the schedule"
        assert cfg.get("target_locales") == "es-MX, de-DE", "settings save wiped languages"
        assert cfg.get("source_field") == "Notes", cfg
        print("  settings save preserves sync_* and language keys (merge, not replace)")

        # --- disable + junk input ---
        c.post(
            f"/connectors/connection/{cid}/schedule",
            data={"every_minutes": "0"},
            follow_redirects=False,
        )
        cfg = cfg_of(cid)
        assert "sync_every_minutes" not in cfg and "sync_next_at" not in cfg, cfg
        assert scheduler.tick() == []
        c.post(
            f"/connectors/connection/{cid}/schedule",
            data={"every_minutes": "abc"},
            follow_redirects=False,
        )
        assert "sync_every_minutes" not in cfg_of(cid), "junk input must mean off"
        print("  disable: state keys removed; junk interval treated as off")

        # --- page renders the schedule card ---
        page = c.get(f"/connectors/connection/{cid}").text
        assert "Scheduled sync" in page and "every_minutes" in page, "schedule card missing"
        assert "minimum 15 minutes" in page, "schedule card missing the floor hint"
        print("  connection page renders the Scheduled sync card")

        # --- interval floor: nonzero minutes clamped up to MIN_EVERY_MINUTES ---
        c.post(
            f"/connectors/connection/{cid}/schedule",
            data={"every_minutes": "1"},
            follow_redirects=False,
        )
        assert cfg_of(cid)["sync_every_minutes"] == scheduler.MIN_EVERY_MINUTES, (
            "sub-floor interval must be clamped to the minimum"
        )
        assert scheduler.MIN_EVERY_MINUTES == 15
        c.post(
            f"/connectors/connection/{cid}/schedule",
            data={"every_minutes": "120"},
            follow_redirects=False,
        )
        assert cfg_of(cid)["sync_every_minutes"] == 120, "above-floor interval must pass through"
        print("  floor: every_minutes=1 -> 15; every_minutes=120 -> 120")

        # --- disable-resurrection guard: _save_state must not re-add next_at to
        #     a schedule that was turned off since the tick began ---
        scheduler.set_schedule(cid, 0)
        assert "sync_every_minutes" not in cfg_of(cid)
        scheduler._save_state(cid, next_at=utcnow() + timedelta(minutes=99))
        assert "sync_next_at" not in cfg_of(cid), (
            "disabled schedule must not regain sync_next_at (resurrection)"
        )
        print("  resurrection guard: _save_state cannot re-arm a disabled schedule")

        # --- consume-slot-first: next_at advances BEFORE the job is submitted.
        #     Stub submit_unique to snapshot config at submit time. ---
        c.post(
            f"/connectors/connection/{cid}/schedule",
            data={"every_minutes": "60"},
            follow_redirects=False,
        )
        scheduler._save_state(cid, next_at=utcnow() - timedelta(seconds=5))
        seen = {}
        real_submit_unique = jobs.submit_unique

        def spy_submit(kind, payload, actor="", dedupe_key="connection_id"):
            seen["next_at_at_submit"] = cfg_of(cid).get("sync_next_at")
            return real_submit_unique(kind, payload, actor=actor, dedupe_key=dedupe_key)

        jobs.submit_unique = spy_submit
        try:
            fired = scheduler.tick()
        finally:
            jobs.submit_unique = real_submit_unique
        assert len(fired) == 1, fired
        wait_job(c, fired[0])
        assert datetime.fromisoformat(seen["next_at_at_submit"]) > utcnow(), (
            "sync_next_at must already be advanced by the time the job is submitted"
        )
        assert scheduler.tick() == []
        print("  consume-slot-first: next_at advanced before submit")

        # --- audit-failure safety: a raising audit.log still leaves exactly one
        #     fire with the slot consumed; the next tick does not re-fire ---
        scheduler._save_state(cid, next_at=utcnow() - timedelta(seconds=5))
        real_audit_log = audit.log

        def boom(*a, **k):
            raise RuntimeError("audit sink down")

        audit.log = boom
        try:
            fired = scheduler.tick()
        finally:
            audit.log = real_audit_log
        assert len(fired) == 1, "audit failure must not prevent (or duplicate) the fire"
        wait_job(c, fired[0])
        assert datetime.fromisoformat(cfg_of(cid)["sync_next_at"]) > utcnow(), (
            "slot must be consumed even when audit.log raised"
        )
        assert scheduler.tick() == [], "already fired -> must not re-fire"
        print("  audit-failure safety: one fire, slot consumed, no re-fire")

        # --- submit_unique dedup: an active connector_translate run makes both
        #     the scheduler tick AND the manual sync route reuse the live job ---
        with Session(engine) as s:
            live = Job(
                kind="connector_translate",
                status=JobStatus.QUEUED,
                payload={"connection_id": cid, "actor": "x"},
            )
            s.add(live)
            s.commit()
            s.refresh(live)
            live_id = live.id
        jid, created = jobs.submit_unique(
            "connector_translate", {"connection_id": cid, "actor": "scheduler"}
        )
        assert (jid, created) == (live_id, False), (jid, created)
        # scheduler: due slot gets consumed but no new job stacks
        scheduler._save_state(cid, next_at=utcnow() - timedelta(seconds=5))
        assert scheduler.tick() == [], "active run -> tick must not stack a second job"
        assert datetime.fromisoformat(cfg_of(cid)["sync_next_at"]) > utcnow(), (
            "skipped-due-to-dup slot must still be consumed"
        )
        # manual route: redirect to the EXISTING job, not a new one (exact-id
        # regex — a substring check would let job=4 match job=41)
        r = c.post(f"/connectors/connection/{cid}/sync", follow_redirects=False)
        m = re.search(r"[?&]job=(\d+)", r.headers["location"])
        assert r.status_code == 303 and m and int(m.group(1)) == live_id, r.headers
        with Session(engine) as s:
            j = s.get(Job, live_id)
            j.status = JobStatus.COMPLETED
            s.add(j)
            s.commit()
        print("  submit_unique: dedup reuses live job; manual route lands on it")

        # --- reserved-email guard: create_user rejects a system identity the same
        #     way it rejects a duplicate email (ValueError) ---
        for reserved in ("scheduler", "system", "companion-app", "anonymous"):
            try:
                security.create_user(reserved, "X", Role.TRANSLATOR, "pw-abcdefghij")
                raise AssertionError(f"{reserved!r} should be rejected")
            except ValueError:
                pass
        security.create_user("realuser@x.com", "Real", Role.TRANSLATOR, "pw-abcdefghij")
        try:
            security.create_user("realuser@x.com", "Dup", Role.TRANSLATOR, "pw-abcdefghij")
            raise AssertionError("duplicate email should be rejected")
        except ValueError:
            pass
        print("  reserved-email: create_user rejects system identities like a duplicate")

        # --- registered add-on sweeps run every tick, ABOVE the connector-sync kill switch
        #     (DB-only housekeeping). The open core ships none, so tick() is a clean no-op there ---
        calls = {"n": 0}
        from app import extensions

        extensions.register_sweep(
            "tick_memory",
            lambda now=None: calls.__setitem__("n", calls["n"] + 1),
            "probe sweep",
            "probe",
        )
        try:
            settings.set_many({"sync_scheduler_enabled": "0"})  # sync OFF...
            scheduler.tick()
            assert calls["n"] == 1, ("a registered sweep must run with the sync switch off", calls)
            settings.set_many({"sync_scheduler_enabled": "1"})
            scheduler.tick()
            assert calls["n"] == 2, calls
        finally:
            extensions._sweeps.pop("tick_memory", None)
            settings.set_many({"sync_scheduler_enabled": "1"})
        print("  sweeps: registered sweeps run every tick, above the sync switch")
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real_translate_now)

    print("SCHEDULE OK")


if __name__ == "__main__":
    main()
