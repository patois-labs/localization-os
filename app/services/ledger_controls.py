"""Guarded envelope administration. Documented in: docs/architecture/connectors.md.

Web and CLI share these ceremonies; MCP exposes status only. Each applied batch
commits its chained audit row with its database changes. Raw files are staged by
rename and restored on transaction failure; successful commits remove staged files.

A HARD KILL between the rename and the commit can leave a `<stem>.purge-<hex>-<epoch>`
file on disk. `reconcile_staged_purges()` (run at boot and on the scheduler housekeeping
pass, independent of the raw-capture switch) settles that residue against the
TapAction row: the row's absence means the purge committed (delete the file); its
presence means the purge did not commit (restore the `<stem>.jsonl` file).

Reconciliation must NOT race a purge that is still running (gate round-2 HIGH-1):
between a purge's `path.rename(staged)` and its `session.commit()` the row is still
visible, so a naive "does the row exist?" reconciliation would rename the staged file
BACK while the purge is about to commit its deletion, resurrecting a raw payload the
ceremony already reported and audited as gone. Two guards close that window: a purge
holds `_staging_lock` across its whole rename/commit/cleanup window for each batch and
reconciliation takes the SAME lock non-blocking (so it can never observe an in-flight
purge in THIS process), and reconciliation leaves alone any staged file younger than a
setting-backed grace window (default 10 minutes), which covers a live purge in ANOTHER
process (boot vs a running server) that an in-process lock cannot see.

The grace window ages from the STAGING moment, not the raw capture (gate round-3
HIGH-1): `path.rename` does NOT touch mtime, so a staged file's mtime is the moment the
envelope was last appended -- always days old by the time a purge (which only selects
terminal actions finished before a past-UTC cutoff) can stage it -- and an mtime grace
would therefore NEVER protect a real staged file. The staging epoch is carried IN the
staged file name (`<stem>.purge-<hex>-<epoch>`, an integer second) and reconciliation
reads it from the name; a legacy name with no epoch falls back to mtime and is treated
as old residue. The purge's post-commit cleanup is missing-file tolerant and never
raises after the commit.
"""

import contextlib
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, text
from sqlmodel import Session, select

from app.db import engine
from app.models import AuditLog, Setting, TapAction, TapRun, TapStatus
from app.services import audit, audit_state, prompt_tap, settings, tap_envelope

log = logging.getLogger("loc_tms.ledger_controls")

DISABLE_PHRASE = "disable envelope recording"
BATCH_SIZE = 100

# The retained-file directory scan is O(files) and was being run on every Ledger page and
# API list render (gate round-1 MED-1). Cache one scan behind the SAME hourly TTL the TTL
# sweep self-throttles to, so N renders in an hour do at most one directory scan. Fresh
# reads (CLI `ledger status`, tests) bypass the cache; render call sites pass use_cache=True.
_scan_lock = threading.Lock()
_scan_cache = {"at": 0.0, "scan": None}

# Purge STAGING lock (gate round-2 HIGH-1). A guarded purge holds this across its whole
# rename -> commit -> cleanup window for each batch; reconcile_staged_purges() takes it
# NON-BLOCKING, so a reconciliation can never observe an in-flight purge's staged file in THIS
# process (which would resurrect a file the purge is deleting). It is a plain (non-reentrant)
# lock on purpose: reconciliation and purge never nest on one thread, and a non-reentrant lock
# makes the "a purge is in flight -> skip" test deterministic. Orphaned residue from a DIFFERENT
# process (a hard kill; boot vs a running server) is handled by the staging-epoch grace window.
_staging_lock = threading.Lock()

# reconcile listdir throttle (gate round-2 LOW-1). The scheduler tick runs reconcile every ~30s
# ABOVE the TTL sweep's own hourly throttle (so residue settles even while raw capture is OFF),
# so reconcile needs its OWN throttle or it lists the raw dir ~120x/hour. `force=True` (boot,
# tests) bypasses it.
_reconcile_lock = threading.Lock()
_reconcile_at = 0.0
_RECONCILE_GRACE_SETTING = "ledger_purge_reconcile_grace_seconds"
_RECONCILE_GRACE_DEFAULT_S = 600  # 10 min; a real purge batch's staging window is milliseconds
# A staging epoch beyond now + this skew cannot be real: `_staged_path` stamps int(time.time())
# from the wall clock, so a FUTURE epoch means the clock was ahead at staging (a VM snapshot
# restore, a container booted before NTP sync, a wrong RTC) and was later stepped back. Such a
# file must be treated as OLD residue, never young-forever, or the reconciler is permanently
# disabled for it and the purged raw payload lingers under a `.purge-` name (gate round-4 MED-1).
# The mtime fallback for a legacy or malformed name gets the same upper bound.
_STAGE_EPOCH_SKEW_S = 300


def _scan_raw_dir():
    """ONE O(files) pass over the per-action raw dir (plus the legacy file). Returns
    {"entries": [(stem_or_None, mtime), ...], "staged": <count of *.purge-* residue>}.
    `stem is None` marks the legacy global file. This is the patchable filesystem seam the
    throttle test counts."""
    entries = []
    staged = 0
    total_bytes = 0
    ad = prompt_tap._actions_dir()
    try:
        names = os.listdir(ad) if ad.exists() else []
    except OSError:
        names = []
    for name in names:
        if ".purge-" in name:
            staged += 1
            continue
        if not name.endswith(".jsonl"):
            continue
        path = ad / name
        try:
            if path.is_file() and not path.is_symlink():
                st = path.stat()
                entries.append((name[: -len(".jsonl")], st.st_mtime))
                total_bytes += st.st_size
        except OSError:
            continue
    legacy = prompt_tap._path()
    try:
        if legacy.is_file() and not legacy.is_symlink() and legacy.stat().st_size:
            entries.append((None, legacy.stat().st_mtime))
            total_bytes += legacy.stat().st_size
    except OSError:
        pass
    return {"entries": entries, "staged": staged, "bytes": total_bytes}


def _retained_scan(use_cache):
    if not use_cache:
        return _scan_raw_dir()
    now = time.monotonic()
    with _scan_lock:
        cached = _scan_cache["scan"]
        if cached is not None and (now - _scan_cache["at"]) < prompt_tap._SWEEP_MIN_INTERVAL_S:
            return cached
        scan = _scan_raw_dir()
        _scan_cache["scan"] = scan
        _scan_cache["at"] = now
        return scan


def invalidate_scan_cache():
    """Drop the cached directory scan so the next cached read re-scans (called after a purge
    apply, after reconciliation move/remove files, and after the TTL sweep deletes files)."""
    with _scan_lock:
        _scan_cache["scan"] = None
        _scan_cache["at"] = 0.0


def _dir_listing(ad):
    """The reconcile directory listing, isolated as the patchable seam the throttle test
    counts (gate round-2 LOW-1). Never raises."""
    try:
        return os.listdir(ad) if ad.exists() else []
    except OSError:
        return []


def _reconcile_grace_s() -> float:
    """Staged-file grace window in seconds (setting-backed, default 10 min). A staged file
    younger than this may belong to a live purge in ANOTHER process, so reconciliation leaves
    it alone; anything older cannot (a batch's staging window is milliseconds). Fails safe to
    the default; a negative value is treated as the default, never as "no grace"."""
    try:
        raw = settings.get(_RECONCILE_GRACE_SETTING, _RECONCILE_GRACE_DEFAULT_S)
        val = float(str(raw if raw is not None else _RECONCILE_GRACE_DEFAULT_S).strip())
    except Exception:
        return float(_RECONCILE_GRACE_DEFAULT_S)
    return val if val >= 0 else float(_RECONCILE_GRACE_DEFAULT_S)


def _staged_path(path):
    """The staged name a purge renames a raw file to: `<stem>.purge-<hex>-<epoch>`, where
    `<epoch>` is the integer STAGING second (gate round-3 HIGH-1). The epoch is carried in the
    name -- not read back from mtime -- because `path.rename` preserves the raw file's capture
    mtime (days old for any purgeable action), so the grace window must age from staging, not
    capture. purge() and the falsifying test both stage through this ONE helper so the test
    binds to production's naming, not a hand-rolled copy of it."""
    return path.with_suffix(f".purge-{uuid4().hex}-{int(time.time())}")


def _staged_epoch(name):
    """The staging epoch (integer seconds) carried in a `<stem>.purge-<hex>-<epoch>` staged
    name, or None for a legacy `<stem>.purge-<hex>` name written before the epoch was carried.
    A None result tells reconciliation to fall back to the file's mtime, which for such legacy
    residue is the capture mtime = already days old = genuine residue."""
    marker = name.rsplit(".purge-", 1)
    if len(marker) != 2:
        return None
    tail = marker[1].rsplit("-", 1)
    if len(tail) != 2:
        return None
    try:
        return int(tail[1])
    except ValueError:
        return None


def _reset_reconcile_throttle():
    """Test seam: clear the reconcile listdir throttle so the next non-forced call scans."""
    global _reconcile_at
    with _reconcile_lock:
        _reconcile_at = 0.0


def status(*, org_id=None, customer_id=None, scoped=False, use_cache=False):
    scan = _retained_scan(use_cache)
    entries = scan["entries"]
    if scoped or customer_id is not None:
        stems = [stem for (stem, _mt) in entries if stem is not None]
        if not stems:
            allowed = set()
        else:
            with Session(engine) as session:
                # BOUNDED by the on-disk stems (the retained-file set), never the whole id
                # space (gate round-1 MED-1): this joins against the files already listed
                # instead of loading every TapAction.id into a Python set.
                query = select(TapAction.id).where(TapAction.id.in_(stems))
                if scoped:
                    query = (
                        query.where(TapAction.org_id == org_id)
                        if org_id is not None
                        else query.where(False)
                    )
                if customer_id is not None:
                    query = query.where(TapAction.customer_id == customer_id)
                allowed = set(session.exec(query).all())
        timestamps = [mt for (stem, mt) in entries if stem is not None and stem in allowed]
    else:
        timestamps = [mt for (_stem, mt) in entries]
    raw_enabled = prompt_tap.enabled()
    # Review (#616): the raw byte total and the last prune count EVERY org's files, so a
    # tenant- or customer-scoped caller gets None for them (no cross-tenant volume signal);
    # the caps are install settings, not tenant data.
    install_wide = not (scoped or customer_id is not None)
    return {
        "envelopes_enabled": tap_envelope.envelopes_enabled(),
        "raw_capture_enabled": raw_enabled,
        "retained_raw_files": len(timestamps),
        "oldest_raw_date": datetime.fromtimestamp(min(timestamps), timezone.utc).date().isoformat()
        if timestamps
        else None,
        "raw_age_out_frozen": not raw_enabled,
        # Staged purge residue a hard kill left behind (install-wide, reconciled on the next
        # housekeeping pass); surfaced so the operator can see the debt (gate round-1 MED-5).
        "staged_purge_files": scan["staged"],
        # Retention visibility (#616): how big the raw capture is, the caps that bound it, and
        # what the last prune did. Bytes and the last prune are install-wide, so they are
        # withheld (None) from a scoped caller; the caps are shown to everyone.
        "raw_bytes": scan.get("bytes", 0) if install_wide else None,
        "retention_ttl_days": prompt_tap.retention_ttl_days(),
        "retention_max_bytes": prompt_tap.retention_max_bytes(),
        "last_prune": prompt_tap.last_prune_state() if install_wide else None,
    }


def reconcile_staged_purges(*, force=False):
    """Settle `<stem>.purge-<hex>` raw-capture files a HARD KILL left staged mid-purge
    (gate round-1 MED-5). The TapAction row is the source of truth: the row is GONE means
    the purge committed (delete the orphaned file); the row EXISTS means the purge did not
    commit (restore the `<stem>.jsonl` file). Bounded by the staged-file count. Best-effort,
    never raises. Returns {"restored": n, "removed": m, "scanned": k, "deferred": d,
    "skipped": reason|None}, where `deferred` counts staged files left alone this pass because
    they are inside the grace window (distinct from the terminal `skipped` reasons -- LOW-2).

    Concurrency (gate round-2 HIGH-1). Three guards keep reconciliation from racing an
    in-flight purge:
      * a listdir throttle (LOW-1): `force=False` (the scheduler tick) does at most one raw-dir
        listing per `_SWEEP_MIN_INTERVAL_S`; `force=True` (boot, tests) always scans. The
        throttle is consumed only by a tick that actually reaches the listing (not one turned
        away by the staging lock), so crash residue never waits an extra interval behind a
        busy purge (gate round-3 LOW-1).
      * the shared `_staging_lock`, taken NON-BLOCKING: if a purge in THIS process holds it we
        skip entirely (that purge cleans its own residue), so no reconciliation can observe a
        purge's rename -> commit window from inside the process.
      * a grace window (`_reconcile_grace_s`), aged from the STAGING epoch carried in the staged
        name (gate round-3 HIGH-1, NOT mtime -- a rename preserves the days-old capture mtime):
        a staged file younger than the window may belong to a live purge in ANOTHER process
        (the in-process lock cannot see it), so it is left alone; a real batch's staging window
        is milliseconds, so anything older is genuine residue. The window is bounded on BOTH
        sides (gate round-4 MED-1): an epoch (or mtime) beyond now + `_STAGE_EPOCH_SKEW_S` is a
        clock artifact, not a live purge, so it is clamped to OLD residue and settled, never
        deferred forever.
    """
    result = {"restored": 0, "removed": 0, "scanned": 0, "deferred": 0, "skipped": None}
    global _reconcile_at
    if not force:
        with _reconcile_lock:
            if (
                _reconcile_at
                and (time.monotonic() - _reconcile_at) < prompt_tap._SWEEP_MIN_INTERVAL_S
            ):
                result["skipped"] = "throttled"
                return result
    # A purge in this process owns the staging window under _staging_lock -> never observe it.
    if not _staging_lock.acquire(blocking=False):
        result["skipped"] = "purge_in_progress"
        return result
    try:
        # Consume the throttle only now that work is possible -- this tick reached the listing
        # (gate round-3 LOW-1). A tick turned away above by the staging lock never stamped it,
        # so residue does not sit an extra interval behind a busy purge.
        if not force:
            with _reconcile_lock:
                _reconcile_at = time.monotonic()
        ad = prompt_tap._actions_dir()
        names = _dir_listing(ad)
        # stem = the action id (a 26-char ULID, no dots) BEFORE the ".purge-" marker.
        stems = {name: name.split(".purge-")[0] for name in names if ".purge-" in name}
        if not stems:
            return result
        # Leave alone anything younger than the grace window (a live purge in another process).
        # Age from the STAGING epoch in the name, NOT mtime: a rename preserves the raw capture
        # mtime (days old for any purgeable action), so an mtime grace would never protect a
        # real staged file (gate round-3 HIGH-1). A legacy name with no epoch falls back to
        # mtime and reads as old residue.
        now = time.time()
        cutoff = now - _reconcile_grace_s()
        # Upper bound the staged age too (gate round-4 MED-1): a staging epoch (or a legacy/
        # malformed name's mtime) in the FUTURE is a clock artifact, not a live purge, so clamp it
        # to OLD residue rather than leaving it deferred forever.
        horizon = now + _STAGE_EPOCH_SKEW_S
        aged = {}
        for name, stem in stems.items():
            staged_at = _staged_epoch(name)
            if staged_at is None:
                try:
                    staged_at = (ad / name).stat().st_mtime
                except OSError:
                    continue
            if staged_at > horizon:
                # The clock was ahead at staging and has since been stepped back; settle the file
                # instead of deferring it forever, and WARN, since it is evidence of a clock jump.
                log.warning(
                    "reconcile: staged file %s carries a future epoch (%s > now + %ss); treating "
                    "as residue (the host clock was likely ahead at staging)",
                    name,
                    int(staged_at),
                    _STAGE_EPOCH_SKEW_S,
                )
                aged[name] = stem
            elif staged_at <= cutoff:
                aged[name] = stem
            else:
                result["deferred"] += 1
        if not aged:
            return result
        try:
            with Session(engine) as session:
                live = set(
                    session.exec(
                        select(TapAction.id).where(TapAction.id.in_(list(aged.values())))
                    ).all()
                )
        except Exception:
            return result  # cannot decide safely -> leave the residue for the next pass
        for name, stem in aged.items():
            result["scanned"] += 1
            src = ad / name
            try:
                if stem in live:
                    # Purge did NOT commit: the row survived -> put the file back under its
                    # name, unless fresh capture already recreated it (then drop the stale stage).
                    dest = ad / (stem + ".jsonl")
                    if dest.exists():
                        src.unlink()
                    else:
                        src.rename(dest)
                    result["restored"] += 1
                else:
                    # Purge COMMITTED: the row is gone -> the staged file is orphaned debt.
                    src.unlink()
                    result["removed"] += 1
            except OSError:
                continue
        if result["restored"] or result["removed"]:
            invalidate_scan_cache()
    finally:
        _staging_lock.release()
    return result


def _audit_row(session, actor, action, detail, ip=""):
    """Write the guarded ceremony's audit row DIRECTLY into the caller's session (do NOT
    "simplify" this into `audit.log`). Two ratified reasons (gate round-2 MED-1):

    1. ATOMICITY. `audit.log()` opens its OWN session and commits independently, which would
       break the property test_audit_failure_is_atomic_and_chain_valid and test_purge_and_rollback
       pin: the ceremony's audit row must commit (or roll back) IN THE SAME TRANSACTION as the
       data change (the setting flip / the row+file deletion). Sharing the caller's session is
       the only way to get that -- a failure anywhere in the ceremony leaves NEITHER the change
       NOR its audit row.
    2. ALWAYS RECORD. `audit.log()` no-ops whenever `audit_enabled="0"`. The whole point of this
       guarded, always-audited control (proposal D9/D10) is that disabling/purging envelopes is
       recorded EVEN when general audit logging is turned off. Writing the row unconditionally
       here is deliberate, and is pinned by test_detail_banner_and_scoped_raw_counts (which flips
       audit_enabled off and still expects both ceremony rows). It keeps the hash chain intact by
       chaining off the last row and re-anchoring in the same transaction.
    """
    last = session.exec(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).first()
    row = AuditLog(
        actor=actor or "cli",
        action=action,
        detail=json.dumps(detail),
        ip=ip or "",
        prev_hash=last.row_hash if last else "",
    )
    row.row_hash = audit._compute_hash(
        row.prev_hash, row.ts.isoformat(), row.actor, row.action, row.target, row.detail, row.ip
    )
    session.add(row)
    session.flush()
    # Keep the anchor in this same transaction, including its first keyed-row boundary.
    anchor = session.get(Setting, audit._ANCHOR_KEY)
    keyed_from = row.id
    if anchor and anchor.value:
        keyed_from = int(anchor.value.split(":")[2])
    mac = audit._anchor_mac(row.id, row.row_hash, keyed_from)
    value = f"{row.id}:{row.row_hash}:{keyed_from}:{mac}"
    session.add(Setting(key=audit._ANCHOR_KEY, value=value) if anchor is None else anchor)
    if anchor is not None:
        anchor.value = value


def set_envelopes(enabled, *, confirm="", actor="cli", reason="", ip=""):
    if not enabled and confirm != DISABLE_PHRASE:
        raise ValueError(f'Type exactly "{DISABLE_PHRASE}".')
    with audit_state.process_lock(), audit._chain_lock, Session(engine) as session:
        session.exec(text("BEGIN IMMEDIATE"))
        row = session.get(Setting, tap_envelope.ENVELOPES_SETTING)
        if row is None:
            row = Setting(key=tap_envelope.ENVELOPES_SETTING, value="1")
        row.value = "1" if enabled else "0"
        session.add(row)
        settings._bump_version(session)
        _audit_row(
            session,
            actor,
            "ledger.envelopes.enable" if enabled else "ledger.envelopes.disable",
            {"reason": reason},
            ip=ip,
        )
        session.commit()
    settings.invalidate_cache()
    settings.apply_to_config()
    return {"envelopes_enabled": enabled}


def _eligible(cutoff):
    # Do not delete a parent while ANY child is in progress or newer than the cutoff.
    protected = select(TapRun.action_id).where(
        (TapRun.status.notin_(TapStatus.TERMINAL))
        | (TapRun.finished_at.is_(None))
        | (TapRun.finished_at >= cutoff)
    )
    return (
        select(TapAction)
        .where(
            TapAction.status.in_(TapStatus.TERMINAL),
            TapAction.finished_at < cutoff,
            TapAction.id.notin_(protected),
        )
        .order_by(TapAction.id)
    )


def purge(*, before, confirm="", apply=False, actor="cli", batch_size=BATCH_SIZE, ip=""):
    try:
        parsed = date.fromisoformat(before)
        if parsed.isoformat() != before:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise ValueError("before must be YYYY-MM-DD") from exc
    # The cutoff must be in the PAST: a future (or today's) date with a matching typed phrase
    # would delete every terminal envelope in the install (gate round-1 LOW-2). UTC to match
    # the exclusive UTC cutoff semantics below.
    if parsed >= datetime.now(timezone.utc).date():
        raise ValueError("before must be a past UTC date (earlier than today).")
    if confirm != f"purge envelopes before {before}":
        raise ValueError(f'Type exactly "purge envelopes before {before}".')
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be 1..1000")
    cutoff = datetime.combine(parsed, datetime.min.time())
    result = {"cutoff": before, "actions": 0, "runs": 0, "raw_files": 0, "applied": apply}
    after = ""
    while True:
        staged = []
        committed = False
        removed = 0
        done = False
        # Hold _staging_lock across the WHOLE batch -- stage, commit AND the cleanup below -- so
        # reconciliation (which takes it non-blocking) can never observe this purge's staged
        # files mid-window and resurrect one (gate round-2 HIGH-1).
        with _staging_lock:
            try:
                with audit_state.process_lock(), audit._chain_lock, Session(engine) as session:
                    if apply:
                        session.exec(text("BEGIN IMMEDIATE"))
                    rows = session.exec(
                        _eligible(cutoff).where(TapAction.id > after).limit(batch_size)
                    ).all()
                    if not rows:
                        if apply and not result["actions"]:
                            _audit_row(session, actor, "ledger.envelopes.purge", result, ip=ip)
                            session.commit()
                        done = True
                    else:
                        ids = [row.id for row in rows]
                        after = ids[-1]
                        runs = session.exec(select(TapRun).where(TapRun.action_id.in_(ids))).all()
                        paths = [
                            prompt_tap._action_path(i) for i in ids if prompt_tap.is_action_id(i)
                        ]
                        paths = [p for p in paths if p.is_file() and not p.is_symlink()]
                        if apply:
                            for path in paths:
                                staged_path = _staged_path(path)
                                try:
                                    path.rename(staged_path)
                                except FileNotFoundError:
                                    continue  # vanished before staging -> nothing to remove
                                staged.append((path, staged_path))
                            session.exec(delete(TapRun).where(TapRun.action_id.in_(ids)))
                            session.exec(delete(TapAction).where(TapAction.id.in_(ids)))
                            # (d) the audit row records the files STAGED for removal (== the set
                            # the cleanup below removes under this lock); the returned result
                            # carries the count RE-COUNTED after the commit (see reply MED-1/(d)).
                            counts = {
                                "cutoff": before,
                                "actions": len(rows),
                                "runs": len(runs),
                                "raw_files": len(staged),
                                "applied": apply,
                            }
                            _audit_row(session, actor, "ledger.envelopes.purge", counts, ip=ip)
                            session.commit()
                            committed = True
                        else:
                            counts = {
                                "cutoff": before,
                                "actions": len(rows),
                                "runs": len(runs),
                                "raw_files": len(paths),
                                "applied": apply,
                            }
                        result["actions"] += counts["actions"]
                        result["runs"] += counts["runs"]
                        if not apply:
                            result["raw_files"] += counts["raw_files"]
            finally:
                # Post-commit cleanup is missing-file tolerant and NEVER raises after the commit
                # (gate round-2 HIGH-1 part c). `removed` is the count actually reclaimed from
                # disk, re-counted here after the commit.
                for original, temporary in staged:
                    if committed:
                        try:
                            temporary.unlink()
                            removed += 1
                        except FileNotFoundError:
                            pass  # a lost race already took it -> do not count, do not raise
                        except OSError:
                            # A Windows reader still holds the handle (the case the TTL sweep
                            # answers with a WARN). Never raise after the commit (gate round-2
                            # HIGH-1c), but do not go SILENT (gate round-3 LOW-3): the audit row
                            # says N removed, `removed` says N-1, and the leftover shows only as
                            # status()["staged_purge_files"] -- so name the staged path once.
                            log.warning(
                                "ledger_controls: post-commit unlink of staged purge file %s "
                                "failed; left for reconcile_staged_purges",
                                temporary,
                            )
                    else:
                        # already restored / gone -> degrade to a no-op
                        with contextlib.suppress(OSError):
                            temporary.rename(original)
            if apply:
                result["raw_files"] += removed
        if done:
            break
    if apply and result["actions"]:
        invalidate_scan_cache()
    return result
