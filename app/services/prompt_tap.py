"""Prompt Tap — an opt-in, read-only OBSERVER of the exact request payload Loc
Studio sends on every completion call.

The maintainer tunes prompts against his own LM Studio and needs to see the EXACT final
request Localization OS hands the provider (LM Studio's own logs truncate the payload).
This service is the capture + storage half; the viewer (web `/prompt-tap` + the
`prompt-tap` CLI group) reads it back.

DESIGN CONTRACT (do not weaken):
  * OFF BY DEFAULT. `enabled()` reads the Setting `prompt_tap_enabled` (default
    "0"). When OFF there are ZERO writes and ZERO file/dir creation — the capture
    seam in providers.run_completion is skipped entirely, and capture() re-checks
    enabled() before it would ever touch the filesystem, so a disabled install is
    byte-for-byte identical to one without this module. A suite proves it.
  * READ-ONLY. capture() NEVER raises: the whole body is wrapped in a
    log-and-continue guard so a tap failure can never fail (or even perturb) a
    translation. The tap is an observer bolted onto run_completion AFTER the
    spend-cap / control-flow decisions are made; it cannot alter them.
  * SECRET-SAFE BY CONSTRUCTION ON THE REQUEST CHANNEL, REDACTED BEST-EFFORT ON
    THE RESPONSE/ERROR CHANNEL. run_completion hands us the prompt / system /
    model / params / task / deterministic-payload — the natural-language request.
    API keys, Authorization headers and Setting secrets live BELOW this seam
    (injected inside each provider's complete()), so they are never in a record.
    We additionally never serialize a `tap_meta` we did not build. What we do NOT
    control is what an UPSTREAM sends back: providers copies an error body verbatim
    and a model can echo anything, so `redact()` scrubs known key shapes from every
    serialized record before it is written. A suite greps the capture file for key
    material after a run with a fake key configured, including an upstream that
    echoes the key back.

STORAGE (SPLIT STORE, proposal section 6): captures live in TWO places by whether the
record belongs to a run envelope.
  * RUN CAPTURE — a record carrying meta.action_id goes to a per-action file
    DATA_DIR/prompt_tap/actions/<action_id>.jsonl. These are NOT rotated (the 500-record
    cap is retired for run capture, D4) so a run's children can never be evicted by
    unrelated traffic; they are bounded instead by the disk-budget backstop
    (ACTIONS_BUDGET_SETTING) and by an AGE-BASED TTL SWEEP (sweep_expired_captures /
    TTL_SETTING) that reclaims files not written in the retention window -- soak finding
    S1: without it these append-only files grow linearly forever (~11 MB/h measured under
    load) on a long-lived install.
  * LEGACY / eval / ad-hoc — a record with NO action_id goes to the single global file
    DATA_DIR/prompt_tap/captures.jsonl, newest appended last, kept bounded by ROTATION:
    a rewrite-on-threshold keeping the newest ~500 records AND ~10 MB, whichever bites
    first (the per-append cost is one stat()). Old records already in this file keep
    serving until they age out (the TTL sweep drops the whole file once it has not been
    written in the retention window -- coexistence).
Reads UNION both stores and order by each record's own `ts` (newest first), so nothing
that was visible before disappears.

EXPORT (the maintainer's queue #1, 2026-07-27): captures are hard to read IN the tool, so
`export_bundle()` hands the operator the same records for offline analysis. It is
ONE seam called by BOTH surfaces (`GET /prompt-tap/download` and `prompt-tap
export`), reusing the viewer's own selection so a download is exactly what the
operator was looking at. It adds NO new exposure: a record already renders in full
on the viewer page, and the secret-safety contract above is what keeps a download
key-free. Records are addressed by `record_key()` — a content digest, stable across
appends, rotation AND the per-action split (an index is not: every capture shifts
newest-first positions).

Documented in: docs/architecture/connectors.md
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import threading
import uuid
from datetime import datetime, timezone

from app import config
from app.services import settings

log = logging.getLogger("loc_tms.prompt_tap")

SETTING_KEY = "prompt_tap_enabled"
# Rotation bounds (see the module docstring). Kept as module constants so the
# rotation test can reference them rather than hard-coding magic numbers.
MAX_RECORDS = 500
MAX_BYTES = 10 * 1024 * 1024
# Response text is capped so one runaway generation can't bloat a record; the
# record carries an honest `truncated` flag + the true length when it bites.
RESPONSE_CAP = 4000
# Export (see the module docstring). MAX_RECORDS is the DEFAULT export limit — a sane,
# bounded page size, NOT a claim that the store is that small. Rotation bounds the legacy
# global file at MAX_RECORDS/MAX_BYTES, but per-action run capture is NOT rotated (D4),
# so the true store can be larger; an export past this default must raise --tail (the
# CLI/web report the honest "N of M matching" when the limit bites).
EXPORT_FORMATS = ("jsonl", "json")
EXPORT_DEFAULT_LIMIT = MAX_RECORDS
# Hex chars of the sha256 content digest used as a record's address. 64 bits: collision
# probability stays ~1e-9 even at ~1e5 records, comfortably covering the split store
# (legacy file bounded by rotation + unrotated per-action files) rather than only the
# old ~500-record single file.
KEY_LEN = 16
# A well-formed key, so a surface can reject junk BEFORE it means anything. Load-
# bearing: a blank/whitespace `record` param used to collapse to "no key given" and
# silently serve the WHOLE STORE where one record was asked for.
KEY_RE = re.compile(r"^[0-9a-f]{%d}$" % KEY_LEN)  # noqa: UP031
# Content-Disposition is a header; keep the whole filename far below any proxy's
# header budget regardless of what the filters contain.
FILENAME_MAX = 120

# DISK-BUDGET BACKSTOP for per-action run capture (proposal section 7, pulled forward
# from Wave C). Per-action files are NOT rotated (the 500-record cap is retired for run
# capture), so a runaway would eat the volume silently — the exact failure mode the
# 2026-08-06 outage taught us to make loud. When prompt_tap/actions/ exceeds this many
# bytes, NEW call capture is REFUSED (the file stops growing) while the envelope keeps
# recording, and the envelope's `capture_withheld` flag says so. A settings override
# (prompt_tap_actions_max_bytes) lets an operator raise it; the age-based TTL sweep
# (sweep_expired_captures) is what actually reclaims the space so the budget is a
# backstop, not the primary bound. Default 512 MB (the Fly volume is 3 GB).
ACTIONS_BUDGET_SETTING = "prompt_tap_actions_max_bytes"
ACTIONS_BUDGET_DEFAULT = 512 * 1024 * 1024

# AGE-BASED RETENTION for the run-capture files (soak finding S1 / PROPOSAL-prompt-tap-run-
# envelopes section 7, D4). Per-action files are NOT rotated, so without an age-out they
# grow linearly forever (~11 MB/h under load, ~260 MB/day projected). sweep_expired_captures
# (piggybacked on the scheduler tick) deletes a capture file whose LAST WRITE (mtime) is
# older than TTL_SETTING days -- so a file still being appended keeps a fresh mtime and is
# never swept, and the file currently being written is protected by construction.
#
# ON BY DEFAULT at 30 days (the maintainer's call, 2026-08-14). 30d is safe on first deploy: prod's
# captures only date to 2026-08-06, so nothing is old enough to be swept immediately, and the
# non-terminal-envelope protection (finding 4a) guards paused/running legs regardless of TTL.
# Raise it for long-retention installs (e.g. 180 on prod) -- a CONFIG change, not code. "0" is
# the ONLY disable value; a NEGATIVE or GARBAGE value is treated as invalid (falls back to the
# default + a re-arming WARN), never as "delete everything" nor as a silent disable.
TTL_SETTING = "prompt_tap_ttl_days"
TTL_DEFAULT_DAYS = 30
# Throttle: the sweep is O(files) (listdir + stat + a bounded delete pass), so it runs at
# most once per this interval even though the scheduler ticks every 30s (finding 10; the
# vendor_return per-vendor guard pattern). A `force=True` bypass exists for tests.
_SWEEP_MIN_INTERVAL_S = 3600.0
# RE-ARMING warn de-dup (finding 2): a once-per-PROCESS latch went silent forever after a
# single benign event, masking a later persistent failure. Instead each distinct failure
# SIGNATURE re-warns at most once per _WARN_REARM_S, so a transient blip is not spammed but a
# standing fault keeps surfacing (and a second, different fault is never masked by the first).
_WARN_REARM_S = 3600.0
_ttl_warn_at: dict = {}  # signature -> monotonic ts of that signature's last WARN
_last_sweep_at = 0.0  # monotonic ts of the last sweep that actually ran (throttle)
# The dir-size scan is O(files); cache it briefly so capture() does not stat the whole
# dir every call. Between refreshes each write increments the cached total, so the
# estimate tracks growth; the periodic rescan corrects for external changes (purge/TTL).
_BUDGET_TTL_S = 5.0

# One writer lock for the append + rotation rewrite (run_completion fans out across
# a thread pool for per-segment MT, so concurrent captures are the norm).
_LOCK = threading.Lock()

# CROSS-PROCESS writer lock for the legacy global file (GitHub #104, stress-0906).
# _LOCK is process-LOCAL, and one LOC_TMS_DATA_DIR is routinely shared by several
# processes at once (the server plus N headless `advanced run` CLIs). Those processes
# raced on ONE fixed captures.jsonl.tmp path: on Windows the race surfaced as three
# distinct errors ([WinError 5] / [WinError 32] on os.replace, [Errno 13] on the tmp
# open), each printing a raw traceback into the operator's run output and DROPPING that
# capture -- twice the dropped record was the ledger's only record of a FAILED provider
# call. On POSIX the identical race is SILENT: nothing raises, so one rotator publishes
# a file built from a stale read and the records another process appended in between
# vanish. So the append AND the rotation rewrite are serialized across processes by an
# advisory lock on a dedicated lock file next to the capture file.
#
# DEGRADE, NEVER DROP: if the lock cannot be taken within _XPROC_LOCK_TIMEOUT_S (a
# wedged holder, a filesystem with no locking support), capture still APPENDS and only
# the REWRITES are skipped for that call, with a re-arming WARN. A skipped rotation
# costs bounded extra bytes until the next capture; a skipped append would cost the
# record itself.
#
# THE DEGRADED APPEND IS STRICT (gate F round 1, M3). It is one short O_APPEND write and
# NOTHING else -- in particular NOT the self-heal, which is a read -> temp -> os.replace
# REWRITE of the whole file. Running that rewrite without the cross-process lock is
# exactly the #104 race this module is closing: on POSIX it silently publishes a file
# built from a stale read (records another process appended in between vanish) and on
# Windows it raises into capture()'s swallow. A writer-unique tmp name removed the tmp
# COLLISION, never the lost-update half.
#
# AND A PERSISTENT LOCK FAILURE LATCHES (gate F round 1, L2). _XPROC_LOCK_TIMEOUT_S is
# polled on the CALLER's (translation) thread with the process-local _LOCK held, so a
# filesystem with no advisory locking (ENOLCK, some network shares) would otherwise cost
# every capture a full 10 s poll loop with every other capture thread queued behind it,
# forever. After a failure the lock is treated as unavailable for _XPROC_DEGRADE_S -- the
# next capture pays nothing and still appends -- and the first success clears the latch.
#
# THE LATCH IS THE CAPTURE PATH'S ALONE (gate F round 2, M1). It is a cost optimisation for
# a hot loop on the translation thread, and applying it to every caller silently turned the
# operator's `clear()` (and the TTL sweep's legacy unlink) back into the unserialized
# truncate M3b had just fixed, for 30 s after ANY capture's lock failure -- a PRIVACY
# regression with no test able to see it, because a fresh test process starts unlatched.
# Those two callers pass `ignore_latch=True`: they are rare, they run on a request or
# maintenance thread, and losing the race costs correctness rather than latency.
_XPROC_LOCK_NAME = "captures.lock"
_XPROC_LOCK_TIMEOUT_S = 10.0
_XPROC_LOCK_POLL_S = 0.01
_XPROC_DEGRADE_S = 30.0
#: monotonic deadline until which _xproc_lock refuses immediately (0.0 = healthy).
_xproc_degraded_until = 0.0

# Once-per-process WARNING latches (see capture()). A wrong-owner capture file or a
# repeated write failure must be VISIBLE (not swallowed at debug like a translation
# never noticed for two days on prod), but identical failures on every subsequent
# call must not spam the log — so each surfaces exactly once per process.
_SELFHEAL_WARNED = False
_CAPTURE_WARNED = False
_BUDGET_WARNED = False

# Disk-budget state (guarded by _LOCK). `bytes` is the cached total of the actions dir;
# `at` its refresh time. `_withheld_flagged` remembers which action ids have already had
# their envelope flagged, so we flag each at most once (one DB write, not one per call).
_budget_cache = {"bytes": None, "at": 0.0}
_withheld_flagged = set()


def _dir():
    return config.DATA_DIR / "prompt_tap"


def _path():
    """The LEGACY global capture file. New captures that belong to a run envelope go
    to per-action files (see _action_path); this file keeps taking captures with NO
    action_id (eval traffic, ad-hoc run_completion calls) and keeps serving old records
    until TTL ages them out -- coexistence (proposal section 6)."""
    return _dir() / "captures.jsonl"


# Per-action call files (proposal section 6): a run's child calls live in
# prompt_tap/actions/<action_id>.jsonl, so unrelated traffic can never rotate them out
# from under their envelope. This is what RETIRES the 500-record cap for run capture
# (D4) -- these files are NOT rotated. A ULID action id is validated as a filename-safe
# token before it is ever turned into a path (defense-in-depth: the id is ours, but a
# file path built from meta must never accept anything but the exact ULID shape).
ACTIONS_SUBDIR = "actions"
# \Z (not $) so a trailing newline can never sneak past the anchor into a filename
# (`$` also matches just before a final \n) — a ULID is exactly 26 chars, nothing more.
_ACTION_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")


def _actions_dir():
    return _dir() / ACTIONS_SUBDIR


def is_action_id(value) -> bool:
    """True for a well-formed ULID action id (Crockford base32, 26 chars)."""
    return bool(value) and bool(_ACTION_ID_RE.match(str(value)))


def _action_path(action_id: str):
    return _actions_dir() / (action_id + ".jsonl")


def _actions_budget() -> int:
    """The max total bytes for prompt_tap/actions/ (settings override, default
    ACTIONS_BUDGET_DEFAULT). Fails safe to the default on a bad read."""
    try:
        raw = settings.get(ACTIONS_BUDGET_SETTING)
        return int(raw) if raw is not None and str(raw).strip() != "" else ACTIONS_BUDGET_DEFAULT
    except Exception:
        return ACTIONS_BUDGET_DEFAULT


def _ttl_days() -> int:
    """Retention window in DAYS for the run-capture files (settings override, default
    TTL_DEFAULT_DAYS == 0 == disabled). "0" is the ONLY disable value. Read ONCE per sweep
    pass -- a periodic housekeeping call, never the per-capture hot path -- so no cache/latch
    is needed here (settings.get is itself cached).

    FAILS SAFE, NEVER RAISES. The read is settings.get, a DB read that can SQLITE_BUSY under a
    write flood, so the except is BARE (like its sibling _actions_budget) -- catching only
    (TypeError, ValueError) would let an OperationalError propagate into the sweep (finding 1).
    A garbage value falls back to the default; a NEGATIVE value is INVALID (never a silent
    "delete everything" nor a silent disable) -> default + a re-arming WARN (finding 11)."""
    try:
        raw = settings.get(TTL_SETTING, TTL_DEFAULT_DAYS)
        val = int(str(raw if raw is not None else TTL_DEFAULT_DAYS).strip())
    except Exception:
        return TTL_DEFAULT_DAYS
    if val < 0:
        _warn_ttl_sweep(
            "bad_ttl",
            "prompt_tap_ttl_days is negative (%d); '0' is the only "  # noqa: UP031
            "disable value -- using the default (%d)" % (val, TTL_DEFAULT_DAYS),
        )
        return TTL_DEFAULT_DAYS
    return val


def _actions_dir_bytes_locked() -> int:
    """Cached total size of the actions dir (CALLER HOLDS _LOCK). Rescans at most once
    per _BUDGET_TTL_S; between rescans _budget_add_locked keeps it current."""
    import time as _time

    now = _time.time()
    if _budget_cache["bytes"] is None or (now - _budget_cache["at"]) > _BUDGET_TTL_S:
        total = 0
        ad = _actions_dir()
        try:
            for name in os.listdir(ad) if ad.exists() else []:
                try:  # noqa: SIM105
                    total += (ad / name).stat().st_size
                except OSError:
                    pass
        except OSError:
            pass
        _budget_cache["bytes"] = total
        _budget_cache["at"] = now
    return _budget_cache["bytes"]


def _budget_exceeded_locked() -> bool:
    """True when the actions dir is at/over budget (CALLER HOLDS _LOCK)."""
    return _actions_dir_bytes_locked() >= _actions_budget()


def _budget_add_locked(n: int) -> None:
    """Account for `n` bytes just written (CALLER HOLDS _LOCK)."""
    if _budget_cache["bytes"] is not None:
        _budget_cache["bytes"] += n


def _withhold_capture_locked(action_id) -> None:
    """Refuse a per-action write over budget (CALLER HOLDS _LOCK): warn once, and flag
    the envelope once. Best-effort; the envelope flag is a separate isolated-session
    write via tap_envelope and must never break the pipeline."""
    _warn_budget_once()
    if action_id not in _withheld_flagged:
        _withheld_flagged.add(action_id)
        try:
            from app.services import tap_envelope

            tap_envelope.mark_capture_withheld(action_id)
        except Exception:  # noqa: S110 -- envelope flag is observability; budget already warned
            pass


def _warn_budget_once() -> None:
    global _BUDGET_WARNED
    if not _BUDGET_WARNED:
        _BUDGET_WARNED = True
        log.warning(
            "prompt_tap: run-capture withheld — prompt_tap/actions/ hit the "
            "disk budget (%s bytes); envelopes keep recording, call files do "
            "not grow until space is reclaimed",
            _actions_budget(),
        )


def enabled() -> bool:
    """True iff the operator turned the tap ON (Setting `prompt_tap_enabled`=="1").
    Fails safe to False on any error so a broken read can never start capturing."""
    try:
        return settings.get(SETTING_KEY) == "1"
    except Exception:
        return False


def set_enabled(on: bool, *, actor: str = "system") -> None:
    """Flip the tap on/off (the web toggle + `prompt-tap on|off` CLI both call here)
    and audit it like the other Setup-AI setting changes. Audit is best-effort — a
    disabled/absent audit chain must not break the toggle."""
    settings.set_many({SETTING_KEY: "1" if on else "0"})
    try:
        from app.services import audit

        audit.log(actor=actor or "system", action="prompt_tap.toggle", detail="on" if on else "off")
    except Exception:
        log.debug("prompt_tap: audit of toggle failed", exc_info=True)


def cap_response(text):
    """Cap a response string to RESPONSE_CAP chars. Returns (capped, truncated,
    full_len). None/empty passes through as ("", False, 0)."""
    s = text or ""
    n = len(s)
    if n <= RESPONSE_CAP:
        return s, False, n
    return s[:RESPONSE_CAP], True, n


def build_record(
    *,
    provider,
    provider_kind,
    model,
    task,
    system,
    prompt,
    max_tokens,
    payload,
    outcome,
    tap_meta=None,
    response_text=None,
    usage=None,
    error=None,
    attempt=None,
    cost=None,
    finish_reason=None,
    error_class=None,
    elapsed_ms=None,
    output_chars=None,
    reasoning_tokens=None,
    temperature=None,
    seed=None,
) -> dict:
    """Assemble ONE capture record. Pure (no I/O) so run_completion can build it
    cheaply and tests can assert its exact shape. See the module docstring for the
    secret-safety contract — nothing key-bearing is ever passed in here.

    `request` is the natural-language package a real model reads, PLUS the
    OpenAI-style `messages` array a chat/local engine (e.g. LM Studio) actually
    receives, so the maintainer sees the exact wire shape. `payload` is the deterministic
    task channel the MockProvider answers from (glossary / source / target); it is
    kept for completeness but carries no secrets.

    run-190 Item B/C — the per-call FAILURE-SHAPE metadata (all OPTIONAL, all METADATA
    only, no prompt/response text): `finish_reason` (the wire finish reason: "length" on
    the thinking burn, "stop" on a clean call), `error_class` (the terminal exception TYPE
    NAME), `elapsed_ms` (wall time of this attempt), `output_chars` (translation chars
    produced — 0 on the empty thinking burn), and `reasoning_tokens` (when `usage` reported
    `completion_tokens_details.reasoning_tokens`). #342 adds `temperature` and `seed`,
    the sampling params a LOCAL engine call sent (seed absent when the install's
    local_seed is blank); both stay absent for every non-local provider. Each field is
    attached ONLY when not None, so a caller that passes none produces a byte-identical
    record; they thread through tap_enrich._metric_row -> the rollups -> tap_view so the
    run/API/summary surfaces can show the shape without the bodies."""
    resp_text, truncated, full_len = cap_response(response_text)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "provider_kind": provider_kind,
        "model": model,
        "task": task,
        "outcome": outcome,
        "request": {
            "system": system,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "messages": _messages(system, prompt),
            "payload": payload,
        },
    }
    if attempt is not None:
        rec["attempt"] = attempt
    if tap_meta:
        # Only a dict WE built is ever attached (run_completion passes a small,
        # fixed-shape meta assembled from workflow/run/step/doc ids, plus memory_ids and —
        # under the Terminology T3 renderer toggle, LAW-TERM-16 — shown_terms /
        # target_locale / segment_index=Segment.id, the leg's ACTUAL shown glossary set for
        # the adherence audit). Still a small, caller-built dict; never caller-supplied.
        rec["meta"] = dict(tap_meta)
    if outcome == "ok" or response_text is not None:
        rec["response"] = {"text": resp_text, "truncated": truncated, "chars": full_len}
    if usage:
        rec["usage"] = dict(usage)
    if cost is not None:
        # The EXACT USD this call added to the ledger (0.0 when exempt/mock or priced at
        # 0) -- the run-envelope roll-up's honest per-call spend PART (Wave B). Threaded
        # from run_completion's already-computed call_cost; NOT recomputed here, so the
        # envelope total can never disagree with the ledger (a mock/exempt call is $0
        # here, where a pricing-table recompute would overstate it).
        rec["cost_usd"] = float(cost)
    if error:
        rec["error"] = str(error)
    # run-190 Item B/C failure-shape metadata (attached only when supplied, so a caller
    # that passes none keeps a byte-identical record).
    if finish_reason is not None:
        rec["finish_reason"] = finish_reason
    if error_class is not None:
        rec["error_class"] = error_class
    if elapsed_ms is not None:
        rec["elapsed_ms"] = int(elapsed_ms)
    if output_chars is not None:
        rec["output_chars"] = int(output_chars)
    if reasoning_tokens is not None:
        rec["reasoning_tokens"] = int(reasoning_tokens)
    # #342: local sampling params (metadata only; absent for non-local providers).
    if temperature is not None:
        rec["temperature"] = float(temperature)
    if seed is not None:
        rec["seed"] = int(seed)
    return rec


def _messages(system, prompt):
    """The OpenAI-style chat messages array a chat/local engine receives — mirrors
    providers._chat_messages so the tap shows the real wire shape without importing
    (and coupling to) the provider internals."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


_INF = float("inf")


def _finite(value):
    """Rewrite non-finite floats to their string spelling, recursively — in dict KEYS
    as well as values. json.dumps emits bare `NaN`/`Infinity` for them, which is
    INVALID JSON that every strict parser (and every tool a download is meant to feed)
    rejects. Missing the KEY case made the fallback below re-raise, and capture()'s
    blanket guard then DROPPED a record the pre-`_dumps` code stored."""
    if isinstance(value, float):
        return value if value == value and value not in (_INF, -_INF) else str(value)
    if isinstance(value, dict):
        return {_finite(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _dumps(rec, *, indent=None, sort_keys=False) -> str:
    """THE record serializer — capture, digest and every export arm go through it, so
    a downloaded line is spelled exactly like the stored one. ASCII (any console or
    pipe can carry it), `default=str` for values json cannot handle, and
    `allow_nan=False` so a non-finite float in a payload falls back to a quoted
    spelling instead of emitting unparseable output.

    NEVER raises: three callers depend on it, one of them mid-`StreamingResponse`
    where an exception would tear a half-written download. A record that defeats even
    the fallback is stored as its repr rather than lost."""
    try:
        return json.dumps(
            rec, ensure_ascii=True, default=str, allow_nan=False, indent=indent, sort_keys=sort_keys
        )
    except (ValueError, RecursionError, TypeError):
        pass
    try:  # a non-finite float somewhere in the record (value OR key)
        return json.dumps(
            _finite(rec),
            ensure_ascii=True,
            default=str,
            allow_nan=False,
            indent=indent,
            sort_keys=sort_keys,
        )
    except (ValueError, RecursionError, TypeError):
        return json.dumps(
            {"unserializable_record": repr(rec)[:4000]},
            ensure_ascii=True,
            indent=indent,
            sort_keys=sort_keys,
        )


# The string-secret scrubber moved to app/services/secrets_scrub.py so the Databricks
# analytical exporter can reuse it WITHOUT importing prompt_tap (a contract-forbidden
# providers->prompt_tap shaped edge) and WITHOUT prompt_tap.redact's wrong dict->dict
# signature (SPEC-databricks-exporter §5.4 / round-2 B3). Behavior is byte-identical:
# the constants + `_live_secrets` + the `_scrub` body live there verbatim.
#
# `_scrub` / `_live_secrets` / `REDACTION` are KEPT here as re-exported module-level
# aliases (option (i)) because verify_prompt_tap reaches them as `prompt_tap._scrub`
# / `prompt_tap._live_secrets` / `prompt_tap.REDACTION` at pinned sites — the aliases
# keep that suite green UNTOUCHED. `redact()` below still calls `_scrub(...)` (the
# alias), so the telemetry contract row (prompt_tap.redact stays in prompt_tap;
# providers->prompt_tap unchanged) is untouched.
from app.services import secrets_scrub  # noqa: E402

# MAINTENANCE NOTE: the scrubber's BEHAVIOR now lives in secrets_scrub — patch/extend the
# patterns THERE, not here. These three are pure re-export aliases; reassigning them here
# would silently fork the two consumers (prompt_tap + the Databricks exporter).
_scrub = secrets_scrub.scrub
_live_secrets = secrets_scrub._live_secrets
REDACTION = secrets_scrub.REDACTION


def redact(record: dict):
    """BEST-EFFORT secret scrub of the channels we DO NOT CONTROL. Returns the record
    to store (a copy when anything changed, the original object otherwise).

    WHY IT EXISTS (review, 2026-07-27): the contract above is true for the REQUEST
    channel — Localization OS never hands a key to build_record. It is NOT true for what
    comes BACK: `providers` copies an upstream error body verbatim (`HTTP 401:
    {"message":"Incorrect API key provided: sk-..."}` — what OpenAI-compatible
    gateways emit), and a model can echo anything into its response text. A capture
    used to live on an admin's screen; a download makes it a file that TRAVELS.

    WHY IT IS SCOPED TO `response.text` + `error` (re-review, same day): the first
    version scrubbed the whole serialized line, which mangled the very thing the tap
    exists to show — a prompt about OAuth ("Send the bearer token_from_the_identity
    _provider header") or a changelog slug (`sk-format-migration-notes-v2`) came back
    rewritten, so a tuning session would debug a string that was never sent. The
    request channel is OURS and is safe by construction, so it is left ALONE. Within
    the two scrubbed channels, unambiguous key PREFIXES apply to both; shapes that
    also occur in real text (a bare 32-hex hash) apply only to `error`, which is
    never delivered content.

    A scrub sets `redacted: true` on the record, so the viewer and any download say
    plainly that a value was rewritten rather than showing an altered payload as if
    it were verbatim. Pre-existing capture files are not rewritten — `prompt-tap
    clear` if an old one may hold a key."""
    changed = False
    out = record
    resp = record.get("response")
    if isinstance(resp, dict) and resp.get("text"):
        text, hit = _scrub(resp["text"], error_channel=False)
        if hit:
            out = dict(out)
            out["response"] = dict(resp, text=text)
            changed = True
    if record.get("error"):
        text, hit = _scrub(str(record["error"]), error_channel=True)
        if hit:
            out = dict(out) if out is record else out
            out["error"] = text
            changed = True
    if changed:
        out["redacted"] = True
    return out


def capture(record: dict) -> None:
    """Append one record as JSONL. NEVER raises — a tap failure must never fail a
    translation. Re-checks enabled() so no file/dir is ever created while OFF.

    SELF-HEALING (2026-08-06): the append can hit a capture file the process cannot
    write — the prod repro was a root in-process CLI run whose rotation rewrite
    recreated captures.jsonl owned root:root 644, after which every append by the
    `USER app` process raised PermissionError and was silently swallowed at debug for
    two days. The DIRECTORY stays app-owned, so the app can legally REPLACE the file
    even when it cannot write INTO it, and the 644 file is world-readable so its
    records are recoverable. On a PermissionError we therefore rebuild the file in
    place (read → temp → os.replace, now owned by this process) and append, surfacing
    one WARNING. Any failure still keeps the never-raise contract, but at WARNING (once
    per process) rather than debug so a wedged tap is finally visible."""
    try:
        if not enabled():
            return
        out = redact(record)
        line = _dumps(out)
        action_id = (out.get("meta") or {}).get("action_id")
        if is_action_id(action_id):
            # RUN CAPTURE: a per-action file, NOT rotated (the 500-record cap is retired
            # for run capture, D4). Same self-heal + WARNING visibility as the global
            # file below (proposal section 6, carrying commit dec7c1b forward).
            with _LOCK:
                if _budget_exceeded_locked():
                    # DISK-BUDGET BACKSTOP (section 7): refuse the write, flag the
                    # envelope (once), warn (once). The envelope keeps recording; only
                    # the per-call file stops growing.
                    _withhold_capture_locked(action_id)
                    return
                _actions_dir().mkdir(parents=True, exist_ok=True)
                _append_selfheal(_action_path(action_id), line)
                _budget_add_locked(len(line) + 1)
        else:
            # LEGACY / eval / ad-hoc call with no action id: the global file, rotated.
            _dir().mkdir(parents=True, exist_ok=True)
            with _LOCK:
                # #104: append + rotate are serialized across PROCESSES too -- several
                # processes routinely share one LOC_TMS_DATA_DIR.
                _append_and_rotate(line)
    except Exception:
        _warn_capture_once("prompt_tap: capture failed (ignored)")


class _XProcLockUnavailable(Exception):
    """The cross-process capture lock could not be taken within its timeout (or the
    platform/filesystem offers no advisory locking). Handled by `_append_and_rotate`,
    which degrades to append-without-rotation rather than dropping the record."""


def _xproc_acquire(fd: int) -> None:
    """Block (bounded by _XPROC_LOCK_TIMEOUT_S) until this process holds the exclusive
    advisory lock on `fd`. Windows uses msvcrt byte-range locking, POSIX uses flock;
    both are polled NON-blockingly so the wait can never exceed the timeout (a blocking
    flock on a wedged holder would pin a translation thread forever). Raises
    _XProcLockUnavailable when the deadline passes or locking is unsupported."""
    import time as _time

    deadline = _time.monotonic() + _XPROC_LOCK_TIMEOUT_S
    last: Exception | None = None
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except ImportError as exc:  # no locking primitive on this platform
            raise _XProcLockUnavailable(str(exc)) from exc
        except OSError as exc:
            last = exc
            if _time.monotonic() >= deadline:
                raise _XProcLockUnavailable(
                    f"timed out after {_XPROC_LOCK_TIMEOUT_S:.0f}s ({last})"
                ) from exc
            _time.sleep(_XPROC_LOCK_POLL_S)


def _xproc_release(fd: int) -> None:
    """Release the advisory lock taken by _xproc_acquire. Never raises: the fd is
    closed immediately after, which releases it anyway on both platforms."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        log.debug("prompt_tap: releasing the cross-process capture lock failed", exc_info=True)


@contextlib.contextmanager
def _xproc_lock(ignore_latch: bool = False):
    """EXCLUSIVE across PROCESSES for the duration of the block (#104). The lock lives
    in its OWN file (prompt_tap/captures.lock) rather than on captures.jsonl itself, so
    it is never invalidated by the rotation that REPLACES the capture file, and a reader
    is never blocked by a writer.

    `ignore_latch=True` SKIPS THE L2 DEGRADE LATCH (gate F round 2, M1). The latch exists
    for ONE reason -- a capture on a TRANSLATION thread must not pay a 10 s poll loop on
    every call once the filesystem has proved it offers no advisory locking -- and it must
    not silently disarm the two callers that are not captures. An OPERATOR's `clear()` and
    the TTL sweep's LEGACY unlink run on a request/maintenance thread, are rare, and both
    have a correctness (for `clear()`, a PRIVACY) consequence if they race a rotation in
    another process, so they always ATTEMPT the lock and pay the timeout if they must. A
    failure from those callers still ARMS the latch: the filesystem's verdict is the same
    whoever asked, and the capture path should learn it from any prover."""
    global _xproc_degraded_until
    import time as _time

    # L2: a standing failure (no advisory locking on this filesystem, a permanently
    # wedged holder) is remembered for _XPROC_DEGRADE_S so it stops costing every
    # capture a full poll loop on the translation thread.
    if not ignore_latch and _xproc_degraded_until and _time.monotonic() < _xproc_degraded_until:
        raise _XProcLockUnavailable(
            "the cross-process capture lock is latched unavailable after a recent failure"
        )
    path = _dir() / _XPROC_LOCK_NAME
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        _xproc_degraded_until = _time.monotonic() + _XPROC_DEGRADE_S
        raise _XProcLockUnavailable(f"cannot open {path} ({exc})") from exc
    try:
        _xproc_acquire(fd)
    except _XProcLockUnavailable:
        _xproc_degraded_until = _time.monotonic() + _XPROC_DEGRADE_S
        os.close(fd)
        raise
    except BaseException:
        os.close(fd)
        raise
    _xproc_degraded_until = 0.0  # a success clears the latch
    try:
        yield
    finally:
        try:
            _xproc_release(fd)
        finally:
            os.close(fd)


def _append_and_rotate(line: str) -> None:
    """Append `line` to the LEGACY global capture file and rotate it, serialized across
    PROCESSES as well as threads (#104). CALLER HOLDS _LOCK.

    DEGRADE, NEVER DROP: when the cross-process lock is unavailable the record is still
    appended and only the REWRITES are skipped, with a re-arming WARN so the condition is
    never silent on any OS. The next capture that can take the lock rotates.

    THE DEGRADED APPEND IS STRICT (gate F round 1, M3): `_append_strict` is one O_APPEND
    write and nothing else. It deliberately does NOT self-heal, because `_selfheal_file`
    is a read -> temp -> os.replace REWRITE of the whole file, and running that while the
    cross-process lock is NOT held is the very #104 lost-update race this function exists
    to close (silent on POSIX, an exception into capture()'s swallow on Windows). An
    unwritable file therefore stays unwritable until a capture can take the lock and heal
    it under it -- one bounded window, versus publishing a file built from a stale read."""
    try:
        with _xproc_lock():
            _append_selfheal(_path(), line)
            _rotate_locked()
            return
    except _XProcLockUnavailable as exc:
        _warn_ttl_sweep(
            "xproc_lock",
            f"could not take the cross-process capture lock ({exc}); the record was kept "
            "but rotation and self-heal were skipped for this call",
        )
    _append_strict(_path(), line)


def _append_strict(path, line: str) -> None:
    """Append `line + "\n"` to `path` and NOTHING else -- no read, no rewrite, no
    self-heal. The only file operation that is safe to perform on the legacy capture file
    without the cross-process lock (see `_append_and_rotate`). CALLER HOLDS _LOCK."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _append_selfheal(path, line: str) -> None:
    """Append `line + "\\n"` to `path`, self-healing an unwritable file. CALLER HOLDS
    _LOCK. The healthy fast path is byte-identical to the pre-self-heal code; on a
    PermissionError it rebuilds the file in place (see _selfheal_file)."""
    try:
        # HEALTHY FAST PATH — byte-identical to the pre-self-heal code.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except PermissionError:
        _selfheal_file(path, line)


def _selfheal_file(path, line: str) -> None:
    """Rebuild an unwritable capture file in place, then append `line`. CALLER HOLDS
    _LOCK. Reads the (world-readable) records back, writes them to a fresh temp file in
    the same app-owned dir, atomically replaces the target — the new file is owned by
    THIS process — then appends the new record through the now-writable file. Raises
    only if the rebuild itself fails, in which case capture()'s outer guard warns and
    swallows (never-raise contract). The self-heal WARNING fires once per process.

    Parameterized by `path` so it heals a per-action file exactly as it heals the legacy
    global file (proposal section 6: the dec7c1b self-heal + WARNING carry over)."""
    p = path
    try:
        with open(p, encoding="utf-8") as f:
            preserved = f.readlines()
    except OSError:
        preserved = []
    if preserved and not preserved[-1].endswith("\n"):
        preserved[-1] += "\n"
    # #104: a WRITER-UNIQUE tmp name. A fixed ".heal.tmp" is the same cross-process
    # collision the rotation carried (two processes sharing one data dir opening and
    # replacing one path), so the temp is per-writer and cleaned up on failure.
    tmp = _tmp_path(p, "heal")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(preserved)
        _replace_forcing(tmp, str(p))
    except BaseException:
        _discard_tmp(tmp)
        raise
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    _warn_selfheal_once(len(preserved))


def _tmp_path(p, kind: str) -> str:
    """A WRITER-UNIQUE temp path beside `p` (#104). The pre-fix code used one fixed
    `<file>.tmp` for every writer, so two processes sharing a data dir opened and
    replaced the SAME path: on Windows that raised (dropping the capture), on POSIX it
    silently published a mixture. pid + a random tag makes a collision impossible even
    if a writer somehow bypasses the cross-process lock (an older build mid-upgrade)."""
    return f"{p}.{kind}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"


def _discard_tmp(tmp: str) -> None:
    """Remove a temp file a failed rewrite left behind. Best-effort by construction --
    the caller is already handling the real error."""
    try:  # noqa: SIM105
        os.remove(tmp)
    except OSError:
        pass


def _replace_forcing(tmp: str, dst: str) -> None:
    """os.replace(tmp, dst), tolerating a destination whose read-only ATTRIBUTE is set
    on Windows (os.replace onto it raises PermissionError there; POSIX ignores the
    file's own perms for a replace inside a writable dir, so it never reaches the retry
    arm). Clearing the attribute lets the replace through."""
    try:
        os.replace(tmp, dst)
        return
    except OSError:
        os.chmod(dst, stat.S_IWRITE)
        os.replace(tmp, dst)


def _warn_selfheal_once(n: int) -> None:
    global _SELFHEAL_WARNED
    if not _SELFHEAL_WARNED:
        _SELFHEAL_WARNED = True
        log.warning(
            "prompt_tap: capture file was not writable (wrong owner?); "
            "replaced it in place, %d records preserved",
            n,
        )


def _warn_capture_once(msg: str) -> None:
    global _CAPTURE_WARNED
    if not _CAPTURE_WARNED:
        _CAPTURE_WARNED = True
        log.warning(msg, exc_info=True)


def _rotate_locked() -> None:
    """Rewrite-on-threshold rotation (caller holds _LOCK). Keeps the newest
    MAX_RECORDS records and caps total size at MAX_BYTES, whichever bites first."""
    p = _path()
    try:
        size = p.stat().st_size
    except OSError:
        return
    if size <= MAX_BYTES:  # noqa: SIM102
        # Cheap fast path: only count lines when the file is non-trivial.
        # A line-count read is O(file); skip it until the file is plausibly long.
        if size < MAX_RECORDS * 64:
            return
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_RECORDS and size <= MAX_BYTES:
        return
    kept = lines[-MAX_RECORDS:]
    # Enforce the byte cap too: drop oldest until under MAX_BYTES.
    while kept and sum(len(x.encode("utf-8", "ignore")) for x in kept) > MAX_BYTES:
        kept.pop(0)
    # #104: a WRITER-UNIQUE tmp (see _tmp_path) + cleanup, so a failed rewrite leaves
    # neither debris nor a path a second writer can collide on. The rewrite itself is
    # serialized across processes by _append_and_rotate's advisory lock.
    tmp = _tmp_path(p, "rot")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, p)
    except BaseException:
        _discard_tmp(tmp)
        raise


def _unlink_forcing(path) -> None:
    """os.remove(path), tolerating a destination whose read-only ATTRIBUTE is set on Windows
    (os.remove onto it raises PermissionError there). A vanished file is a no-op (the sweep's
    goal is already met).

    WINDOWS-ONLY chmod (finding 3). On POSIX an unlink depends on the DIRECTORY's perms, never
    the file's own mode, so chmod'ing the file cannot help -- and chmod(S_IWRITE) there is
    0o200, which STRIPS the read bit, leaving a file the readers (_iter_file) then silently
    can't read (records vanish from the viewer/export/enrich while appends keep succeeding --
    a fresh instance of the 2026-08-06 outage class). So the retry chmod is gated to nt; on
    POSIX the OSError re-raises to the caller, which WARNs + skips (self-heal next cycle)."""
    try:
        os.remove(path)
        return
    except FileNotFoundError:
        return
    except OSError:
        if os.name != "nt":
            raise  # POSIX: the file's own mode is irrelevant to unlink; never strip its perms
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def _warn_ttl_sweep(signature: str, msg: str, *, exc_info: bool = False) -> None:
    """RE-ARMING per-signature WARN (finding 2). A once-per-process latch went silent forever
    after a single benign event (e.g. one Windows reader-blocked unlink), masking a later
    persistent fault. Instead each distinct `signature` re-warns at most once per _WARN_REARM_S
    -- a transient blip is not spammed, a standing fault keeps surfacing, and a second DIFFERENT
    fault is never suppressed by the first."""
    import time as _time

    now = _time.monotonic()
    last = _ttl_warn_at.get(signature)
    if last is None or (now - last) >= _WARN_REARM_S:
        _ttl_warn_at[signature] = now
        log.warning("prompt_tap: %s", msg, exc_info=exc_info)


def _sweep_protected_action_ids(action_ids):
    """The subset of `action_ids` whose TapAction envelope is NON-TERMINAL (RUNNING/PAUSED) --
    their call files MUST NOT be swept regardless of mtime (finding 4a). The sharp case: a
    PAUSED vendor leg (vendor_dispatch_ttl_hours default 168h == exactly the 7d a TTL could be
    set to) is deliberately skipped by the orphan reaper as durable human state, so 'the reaper
    closes it before TTL' is FALSE for it; sweeping its file would make the envelope later
    report calls=0/$0.00 for a leg that spent money. FAILS SAFE: on any DB error every candidate
    is treated as protected (skip this cycle, retry next) -- deleting a possibly-live file is
    the dangerous direction. Never raises."""
    if not action_ids:
        return set()
    try:
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import TapAction, TapStatus

        ids = list(action_ids)
        protected = set()
        with Session(engine) as s:
            rows = s.exec(select(TapAction).where(TapAction.id.in_(ids))).all()
            for row in rows:
                if row.status not in TapStatus.TERMINAL:
                    protected.add(row.id)
        return protected
    except Exception:
        _warn_ttl_sweep(
            "envelope_probe",
            "TTL sweep could not read envelope status; "
            "protecting all per-action candidates this cycle",
            exc_info=True,
        )
        return set(action_ids)


PRUNE_TARGET_FRACTION = 0.8
PRUNE_STATE_FILE = "prune_state.json"
# Review hardening (#616): the size prune DELETES files, so it fails closed. A budget below this
# floor is a misconfiguration (e.g. "512" typed for 512 MB) and must never turn into "delete
# every capture"; a file written in the last PRUNE_MIN_AGE_S seconds is never size-pruned (it may
# belong to a live run with no envelope row, or be mid-append in another process).
PRUNE_MIN_BUDGET_BYTES = 1024 * 1024
PRUNE_MIN_AGE_S = 600.0


def retention_ttl_days() -> int:
    """The effective retention window in days (0 == age pruning disabled). Public read for
    the Ledger status; same fail-safe rules as the sweep."""
    return _ttl_days()


def retention_max_bytes() -> int:
    """The effective size cap for prompt_tap/actions/ in bytes (the disk budget)."""
    return _actions_budget()


def _prune_state_path():
    return _dir() / PRUNE_STATE_FILE


def _write_prune_state(result: dict) -> None:
    """Record the last sweep that actually ran (when, window, what it reclaimed) in
    prompt_tap/prune_state.json so the Ledger status can show it across restarts and
    processes. Atomic tmp+replace; best-effort, never raises; a failure only loses visibility."""
    try:
        state = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ttl_days": result.get("ttl_days", 0),
            "scanned": result.get("scanned", 0),
            "deleted": result.get("deleted", 0),
            "freed_bytes": result.get("freed_bytes", 0),
            "size_pruned": result.get("size_pruned", 0),
            "size_freed_bytes": result.get("size_freed_bytes", 0),
        }
        if not _dir().exists():
            return  # a sweep over a missing store creates nothing (suite G)
        path = _prune_state_path()
        tmp = path.with_name(PRUNE_STATE_FILE + "." + uuid.uuid4().hex[:8] + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        _warn_ttl_sweep("prune_state_write", "could not record the prune state", exc_info=True)


def last_prune_state():
    """The last recorded sweep (dict) or None when none has run on this install. Never raises.
    Gate (#616): the record is normalized to the known keys with int counters, so a truncated or
    hand-edited file can never crash the CLI status line or the Ledger template (both add the
    counters); an unreadable or malformed record reads as None ("never")."""
    try:
        data = json.loads(_prune_state_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("at"), str):
            return None
        out = {"at": data["at"][:32]}
        for key in (
            "ttl_days",
            "scanned",
            "deleted",
            "freed_bytes",
            "size_pruned",
            "size_freed_bytes",
        ):
            val = data.get(key, 0)
            out[key] = val if isinstance(val, int) and not isinstance(val, bool) else 0
        return out
    except Exception:
        return None


def _prune_to_budget(result: dict) -> int:
    """Delete the oldest unprotected per-action capture files until prompt_tap/actions/ is at
    or under PRUNE_TARGET_FRACTION of the byte budget. No-op while under the budget. Updates
    result["size_pruned"/"size_freed_bytes"]; returns the number deleted. Never raises.

    FAILS CLOSED (this deletes production files): only a REGULAR file (never a symlink) named
    exactly `<26-char ULID>.jsonl` directly inside prompt_tap/actions/ is a candidate, so tmp
    residue, staged purge files, the legacy captures.jsonl and everything outside actions/ are
    never touched; a non-terminal (RUNNING/PAUSED) envelope's file, a file written in the last
    PRUNE_MIN_AGE_S seconds and a file appended since the scan are kept; a budget below
    PRUNE_MIN_BUDGET_BYTES is treated as a misconfiguration and prunes nothing."""
    import time as _time

    deleted = 0
    try:
        budget = _actions_budget()
        if budget <= 0:
            return 0
        if budget < PRUNE_MIN_BUDGET_BYTES:
            _warn_ttl_sweep(
                "prune_budget_floor",
                "prompt_tap_actions_max_bytes is below the %d-byte prune floor; "  # noqa: UP031
                "size pruning skipped (check the setting)" % PRUNE_MIN_BUDGET_BYTES,
            )
            return 0
        ad = _actions_dir()
        files = []  # (mtime, size, path, action_id)
        total = 0
        for name in os.listdir(ad) if ad.exists() else []:
            p = ad / name
            try:
                st = os.lstat(p)
            except OSError:
                continue
            total += st.st_size
            if (
                name.endswith(".jsonl")
                and is_action_id(name[: -len(".jsonl")])
                and stat.S_ISREG(st.st_mode)
            ):
                files.append((st.st_mtime, st.st_size, p, name[: -len(".jsonl")]))
        if total <= budget:
            return 0
        target = int(budget * PRUNE_TARGET_FRACTION)
        files.sort(key=lambda f: f[0])
        protected = _sweep_protected_action_ids([f[3] for f in files])
        recent_cutoff = _time.time() - PRUNE_MIN_AGE_S
        for mtime, size, p, aid in files:
            if total <= target:
                break
            if aid in protected or mtime >= recent_cutoff:
                continue
            with _LOCK:
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime != mtime:
                    continue  # swapped or appended since the scan: keep it
                try:
                    _unlink_forcing(p)
                except OSError:
                    _warn_ttl_sweep(
                        "unlink_failed",
                        "size prune could not delete a capture file; skipped it",
                        exc_info=True,
                    )
                    continue
            total -= size
            deleted += 1
            result["size_pruned"] += 1
            result["size_freed_bytes"] += size
        if deleted:
            log.info(
                "prompt_tap: size prune (cap=%d bytes): deleted=%d freed=%d bytes",
                budget,
                deleted,
                result["size_freed_bytes"],
            )
    except Exception:
        _warn_ttl_sweep("size_prune_crashed", "size prune crashed (ignored)", exc_info=True)
    return deleted


def sweep_expired_captures(*, now_ts=None, ttl_days=None, force=False) -> dict:
    """Delete run-capture files (per-action files + tmp residue, plus a dormant legacy file)
    whose LAST WRITE is older than the retention window. Piggybacks the scheduler tick -- no
    new daemon -- and reclaims the space the disk-budget backstop only STOPS growing (soak
    finding S1 / proposal section 7, D4). Returns a dict with `deleted`, `freed_bytes`,
    `scanned`, `ttl_days`, and (when it did not run) a `skipped` reason.

    OPT-IN + TAP-GATED. Returns immediately (skipped) unless the operator both turned capturing
    ON (`prompt_tap_enabled`, finding 7 -- a disabled tap may be the 2026-08-06 evidence-
    preservation posture, so retention freezes with it: re-enable to resume BOTH capture and
    retention) and `prompt_tap_ttl_days` is a positive window (default 30 == ON, the maintainer's
    2026-08-14 call; "0" is the only disable value). THROTTLED to at most once per
    _SWEEP_MIN_INTERVAL_S (finding 10; `force` bypasses for tests).

    NEVER raises, NEVER fatal (the 2026-08-06 tap-outage lesson is law). A file it cannot delete
    (locked, wrong owner, vanished) is skipped with a RE-ARMING per-signature WARN -- the next
    cycle self-heals. A whole-sweep failure degrades to a WARN, never an exception into the
    scheduler. This is observability housekeeping: it may drop capture data, nothing else may
    break. It emits an INFO summary when it runs (retention window + reclaim counts -- the
    operator visibility findings 2/5 asked for) and a WARN when aged files were attempted but
    none could be reclaimed. Unlike purge() -- which destroys the PM-history ENVELOPE ROWS and
    so writes an AUDIT row -- this only drops raw observability files and logs; a full audit row
    is overkill for capture data (that reasoning is the resolved finding-5 policy call).

    AGE BY MTIME. "older than TTL" == not written in TTL days. mtime tracks the LAST append
    (~= the newest record's `ts`), so deleting a file whose mtime predates the cutoff never
    drops a young record, and the file currently being written (mtime == now) is protected by
    construction. Non-terminal ENVELOPES are protected regardless of mtime
    (_sweep_protected_action_ids, finding 4a). Envelope ROWS are never swept here.

    CONCURRENCY. The write path is append-REOPEN-per-write (open(path,"a") per call, no long-
    lived handle) under _LOCK, so: (1) the candidate scan is lock-free (stat only); (2) each
    delete takes _LOCK and RE-STATS under it, so no WRITER (append) handle is open when the
    unlink runs and a file that got a fresh append since the scan (mtime back inside the window)
    is left alone; (3) a later append to a just-deleted action recreates the file cleanly.
    NOTE (finding 6): _LOCK guards against WRITERS only. Concurrent READERS (tap_view /
    _iter_file / tap_enrich) hold unlocked handles, so on WINDOWS a reader can still block the
    unlink -- that delete is then skipped (re-arming WARN) and reclaimed next cycle; it is never
    fatal. On POSIX an open handle never blocks unlink. So the unlink is clean against APPENDS
    on both OSes, but not necessarily against a concurrent Windows READER.

    `now_ts` (epoch seconds), `ttl_days` and `force` are test overrides; production calls it
    argless. It compares filesystem mtimes against time.time() (both epoch, same domain) and
    never converts a naive-UTC datetime (the localtime trap), which is why the scheduler calls
    it WITHOUT the tick's `now` (that stamp is DB wall clock)."""
    global _last_sweep_at
    import time as _time

    result = {"deleted": 0, "freed_bytes": 0, "scanned": 0, "ttl_days": 0}
    # RECONCILE staged purge residue FIRST, before any gate/throttle below returns: a hard
    # kill mid-purge can leave `<stem>.purge-<hex>` files, and the capture-off early return
    # right below is exactly the state the purge ceremony teaches operators to expect, so the
    # reconciliation must run even while raw capture is OFF (gate round-1 MED-5). Best-effort,
    # never fatal; a lazy import keeps ledger_controls off this module's import graph.
    try:
        from app.services import ledger_controls

        ledger_controls.reconcile_staged_purges()
    except Exception:
        # Unreconciled purge residue is captured prompt data that should already be gone.
        log.warning("prompt tap: staged purge reconciliation failed", exc_info=True)
    try:
        # OPT-IN + TAP-GATED (finding 7): a disabled tap freezes retention with capture.
        if ttl_days is None and not enabled():
            result["skipped"] = "capture_disabled"
            return result
        days = ttl_days if ttl_days is not None else _ttl_days()
        result["ttl_days"] = days
        if days <= 0:
            result["skipped"] = "retention_disabled"
            return result  # "0" is the only disable value (default is ON at 30)
        # THROTTLE (finding 10): O(files) work runs at most hourly, not every 30s tick.
        if not force:
            mono = _time.monotonic()
            if _last_sweep_at and (mono - _last_sweep_at) < _SWEEP_MIN_INTERVAL_S:
                result["skipped"] = "throttled"
                return result
            _last_sweep_at = mono
        now = now_ts if now_ts is not None else _time.time()
        cutoff = now - days * 86400

        # (1) LOCK-FREE candidate scan. Per-action .jsonl files (envelope-protected), tmp
        # residue (.tmp / .heal.tmp -- abandoned by a crashed rotate/heal, counted by the disk
        # budget so it is permanent debt otherwise, finding 9), and a dormant legacy file.
        per_action = []  # (path, action_id)
        residue = []  # abandoned temp files (no envelope identity)
        ad = _actions_dir()
        try:
            names = os.listdir(ad) if ad.exists() else []
        except OSError:
            names = []
        for name in names:
            p = ad / name
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            result["scanned"] += 1
            if mtime >= cutoff:
                continue
            if name.endswith(".jsonl"):
                per_action.append((p, name[: -len(".jsonl")]))
            elif name.endswith(".tmp"):  # covers .heal.tmp and the rotation .tmp
                residue.append(p)
        # The legacy global file ages out as a WHOLE only when it has not been written in TTL
        # days (mtime < cutoff -> its newest record is older than the window, so no young
        # record is dropped; proposal section 6). It is already SIZE-bounded by rotation.
        legacy = _path()
        try:
            if legacy.exists():
                result["scanned"] += 1
                if legacy.stat().st_mtime < cutoff:
                    residue.append(legacy)  # no envelope identity -> treated like residue
        except OSError:
            pass

        # Protect files of NON-TERMINAL envelopes regardless of mtime (finding 4a).
        protected = _sweep_protected_action_ids(
            [aid for (_p, aid) in per_action if is_action_id(aid)]
        )
        to_delete = [p for (p, aid) in per_action if aid not in protected] + residue

        # (2) delete each survivor under _LOCK with a RE-STAT (concurrent-APPEND safe).
        attempted = 0
        deleted_any = False
        for p in to_delete:
            with _LOCK:
                # M3c (gate F round 1): the LEGACY global file is the one path a writer in
                # ANOTHER process rewrites (append + rotate), so its unlink is taken under
                # the cross-process lock too -- otherwise the sweep deletes the file a
                # concurrent rotation is about to os.replace into place (POSIX: the
                # rotation silently resurrects it; Windows: the rotation raises and drops
                # that capture). Per-action files and tmp residue are never rewritten
                # cross-process, so they keep the cheap _LOCK-only path.
                #
                # `ignore_latch=True` (gate F round 2, M1): like clear(), this is not the
                # capture hot loop the L2 latch was added for. The sweep runs once a cycle
                # on a maintenance thread, and skipping the lock because a translation
                # thread failed to take it 20 s ago would delete the file a rotation is
                # about to os.replace back into place.
                try:
                    with (
                        _xproc_lock(ignore_latch=True) if p == legacy else contextlib.nullcontext()
                    ):
                        try:
                            st = p.stat()
                        except OSError:
                            continue  # vanished between scan and now
                        if st.st_mtime >= cutoff:
                            continue  # a fresh append landed since the scan -> keep it
                        size = st.st_size
                        attempted += 1
                        try:
                            _unlink_forcing(p)
                        except OSError:
                            _warn_ttl_sweep(
                                "unlink_failed",
                                "TTL sweep could not delete a capture "
                                "file (locked / wrong owner / Windows reader?); skipped "
                                "it, will retry next cycle",
                                exc_info=True,
                            )
                            continue
                        result["deleted"] += 1
                        result["freed_bytes"] += size
                        deleted_any = True
                except _XProcLockUnavailable as exc:
                    _warn_ttl_sweep(
                        "xproc_lock_sweep",
                        f"could not take the cross-process capture lock ({exc}); the "
                        "dormant legacy capture file was left for the next sweep",
                    )
                    continue
        # (2b) SIZE CAP (#616): the age pass alone leaves a high-volume install growing for
        # the whole TTL window, and the disk budget only STOPS new capture. When the actions
        # dir is over the budget, delete the OLDEST unprotected per-action files until it is
        # back under PRUNE_TARGET_FRACTION of the budget. Only prompt_tap/actions/*.jsonl are
        # ever touched; non-terminal envelopes stay protected exactly as in the age pass.
        result["size_pruned"] = 0
        result["size_freed_bytes"] = 0
        size_deleted = _prune_to_budget(result)
        if size_deleted:
            deleted_any = True
        # (3) a delete makes the actions-dir byte cache stale (too high); force one rescan on
        # the next capture so the disk-budget backstop cannot spuriously withhold.
        if deleted_any:
            with _LOCK:
                _budget_cache["bytes"] = None
            # A delete also makes the Ledger status scan cache stale (too high): the render path
            # would keep reporting files this sweep just reclaimed, for up to the cache TTL (gate
            # round-2 LOW-2). purge() and reconcile invalidate it; the routine deleter must too.
            try:
                from app.services import ledger_controls

                ledger_controls.invalidate_scan_cache()
            except Exception:  # noqa: S110 -- stale status cache self-expires at its TTL
                pass

        # OPERATOR SIGNAL (findings 2 + 5): one INFO summary per (throttled ~hourly) run states
        # the window in effect AND what was reclaimed; a WARN fires when aged files were
        # attempted but NONE could be reclaimed (a standing fault, not a legit protection skip).
        log.info(
            "prompt_tap: TTL sweep (retention=%d days): scanned=%d deleted=%d freed=%d bytes",
            days,
            result["scanned"],
            result["deleted"],
            result["freed_bytes"],
        )
        _write_prune_state(result)
        if attempted > 0 and result["deleted"] == 0:
            _warn_ttl_sweep(
                "no_reclaim",
                "TTL sweep found %d aged capture file(s) but could "  # noqa: UP031
                "not reclaim any (locked / wrong owner / Windows reader?)" % attempted,
            )
    except Exception:
        _warn_ttl_sweep("sweep_crashed", "TTL sweep crashed (ignored)", exc_info=True)
    return result


def _iter_file(p):
    """Yield parsed records from ONE JSONL file in file order (oldest first), skipping
    any corrupt line. No lock: readers tolerate a concurrent append (a half-written
    trailing line is simply skipped)."""
    if not p.exists():
        return
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, TypeError):
                    continue
    except OSError:
        return


def _iter_records():
    """Yield parsed records across the legacy global file AND every per-action file
    (proposal section 6, coexistence). The legacy file (oldest legacy traffic) comes
    first, then the per-action files in sorted name order — action ids are ULIDs, so a
    sorted filename order is chronological — each file in append order. The selectors
    reverse this to newest-first. Corrupt lines are skipped."""
    yield from _iter_file(_path())
    ad = _actions_dir()
    try:
        names = sorted(os.listdir(ad)) if ad.exists() else []
    except OSError:
        names = []
    for name in names:
        if name.endswith(".jsonl"):
            yield from _iter_file(ad / name)


def _ts_key(rec):
    """A record's own capture timestamp, for newest-first ordering across the union of
    the legacy global file and per-action files (H3). build_record stamps `ts` as UTC
    ISO-8601, which sorts lexicographically. Ordering by the RECORD's time — not by
    file-concatenation order — is what keeps a fresh UNSTAMPED capture (eval/ad-hoc, in
    the global file) from sorting as OLDER than every per-action record and vanishing
    from a limited viewer page / export. Missing/blank ts sorts oldest."""
    return rec.get("ts") or ""


def _matches(rec, *, provider=None, workflow_run_id=None, document_id=None) -> bool:
    if (
        provider
        and (rec.get("provider") or "") != provider
        and (rec.get("provider_kind") or "") != provider
    ):
        return False
    meta = rec.get("meta") or {}
    if workflow_run_id is not None and meta.get("workflow_run_id") != workflow_run_id:
        return False
    if document_id is not None and meta.get("document_id") != document_id:  # noqa: SIM103
        return False
    return True


def _select(*, limit=100, provider=None, workflow_id=None, workflow_run_id=None, document_id=None):
    """ONE matching pass -> (records NEWEST-FIRST capped at `limit`, total matches).
    The total is what makes a truncated export honest: the caller can say "N of M"
    without a second pass that a concurrent append could disagree with."""
    out = []
    for rec in _iter_records():
        if workflow_id is not None and (rec.get("meta") or {}).get("workflow_id") != workflow_id:
            continue
        if _matches(
            rec, provider=provider, workflow_run_id=workflow_run_id, document_id=document_id
        ):
            out.append(rec)
    # NEWEST-FIRST by each record's OWN ts, not file order (H3). Stable sort keeps append
    # order among equal-ts records.
    out.sort(key=_ts_key, reverse=True)
    total = len(out)
    if limit and limit > 0:
        out = out[:limit]
    return out, total


def read_records(
    *, limit=100, provider=None, workflow_id=None, workflow_run_id=None, document_id=None
) -> list:
    """Return matching records NEWEST-FIRST, capped at `limit`. Filters are simple
    equality on provider (ref or resolved kind) + correlation meta. Shared by the
    web viewer and the `prompt-tap show` CLI so both render identical data."""
    return _select(
        limit=limit,
        provider=provider,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        document_id=document_id,
    )[0]


def read_records_with_total(
    *, limit=100, provider=None, workflow_id=None, workflow_run_id=None, document_id=None
):
    """`read_records` plus the count BEFORE the limit, from ONE pass. A caller that
    shows rows AND says how many match must use this: two passes can be separated by
    an append or a `clear()`, and the page would then display rows under a label that
    contradicts them (reproduced in review)."""
    recs, matching, _ = page_snapshot(
        limit=limit,
        provider=provider,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        document_id=document_id,
    )
    return recs, matching


def page_snapshot(
    *, limit=100, provider=None, workflow_id=None, workflow_run_id=None, document_id=None
):
    """(rows, matching, stored) — EVERY number the viewer prints, from ONE walk of the
    capture file. Splitting them across passes is what let the page say "0 records
    stored" above five rendered rows when a clear() landed in between; the first fix
    put the rows and the MATCHING count on one snapshot and left the STORE total on a
    second pass, which is the same defect one field over."""
    rows = []
    stored = 0
    for rec in _iter_records():
        stored += 1
        if workflow_id is not None and (rec.get("meta") or {}).get("workflow_id") != workflow_id:
            continue
        if _matches(
            rec, provider=provider, workflow_run_id=workflow_run_id, document_id=document_id
        ):
            rows.append(rec)
    rows.sort(key=_ts_key, reverse=True)  # newest first by record ts, not file order (H3)
    matching = len(rows)
    if limit and limit > 0:
        rows = rows[:limit]
    return rows, matching, stored


def count_records(*, workflow_id=None) -> int:
    """Total captures, or captures whose meta.workflow_id matches (the builder-link
    badge). Cheap enough for a page render (bounded by MAX_RECORDS)."""
    n = 0
    for rec in _iter_records():
        if workflow_id is not None and (rec.get("meta") or {}).get("workflow_id") != workflow_id:
            continue
        n += 1
    return n


# --- export (individual + batch download) -------------------------------------
def _canonical(rec) -> str:
    """The record as ONE canonical line of ASCII JSON. `sort_keys` makes it
    order-independent so the digest below is stable."""
    return _dumps(rec, sort_keys=True)


def record_key(rec) -> str:
    """A record's stable address: the first KEY_LEN hex of the sha256 of its
    canonical JSON. CONTENT-addressed on purpose — the capture file is append-only
    and rotates, so an index or line number names a DIFFERENT record a moment later,
    which would make "download this row" silently hand over the wrong payload."""
    return hashlib.sha256(_canonical(rec).encode("utf-8")).hexdigest()[:KEY_LEN]


def is_record_key(value) -> bool:
    """True for a well-formed record address. Surfaces validate BEFORE dispatching so
    a malformed or truncated per-row link is refused outright rather than falling
    through to "no key given" — which is a whole-store download."""
    return bool(value) and bool(KEY_RE.match(str(value)))


def find_record(key):
    """The one record whose record_key() matches, or None. Scans newest-last file
    order and returns the first hit; two records sharing a key are byte-identical by
    construction (up to the truncated-digest bound noted at KEY_LEN), so which one is
    returned cannot matter."""
    if not key:
        return None
    for rec in _iter_records():
        if record_key(rec) == key:
            return rec
    return None


_FNAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value) -> str:
    """Filename-safe, ASCII-only fragment (provider is operator-typed free text)."""
    return _FNAME_UNSAFE.sub("-", str(value)).strip("-")[:32]


def _export_filename(*, fmt, key, truncated, count, total, filters, stamp) -> str:
    """Name the file after WHAT IT CONTAINS: the active filters and, when the limit
    bit, the honest "N-of-M" marker (a jsonl stream can carry no manifest of its own,
    so the filename is where truncation has to be visible)."""
    if key:
        return f"prompt-tap-record-{_slug(key)}-{stamp}.json"
    parts = ["prompt-tap"]
    if filters.get("provider"):
        parts.append(_slug(filters["provider"]))
    for label, name in (("wf", "workflow_id"), ("run", "workflow_run_id"), ("doc", "document_id")):
        if filters.get(name) is not None:
            # _slug the INTEGER filters too: `int()` accepts thousands of digits, and
            # an unbounded fragment here becomes a multi-KB Content-Disposition that a
            # proxy in front of the app turns into a 502 (review, 2026-07-27).
            parts.append(_slug(f"{label}{filters[name]}"))
    parts.append(stamp)
    if truncated:
        parts.append("TRUNCATED-%dof%d" % (count, total))  # noqa: UP031
    name = "-".join(parts) + ("." + fmt if fmt == "json" else ".jsonl")
    return name if len(name) <= FILENAME_MAX else name[: FILENAME_MAX - 6] + name[-6:]


def export_bundle(
    *,
    fmt="jsonl",
    limit=EXPORT_DEFAULT_LIMIT,
    key=None,
    provider=None,
    workflow_id=None,
    workflow_run_id=None,
    document_id=None,
) -> dict:
    """THE export seam — `GET /prompt-tap/download` and `prompt-tap export` both call
    exactly this, so the two surfaces cannot render different payloads (parity law).

    Selection is the viewer's own `_select`, so a batch download is precisely the set
    the operator was filtered to, newest first. Pass `key` for the INDIVIDUAL case:
    one record, pretty-printed, addressed by record_key().

    Returns a dict:
      fmt / media_type / filename  — how to serve or name it
      count / total / truncated    — honest arithmetic (`total` is matches BEFORE the
                                     limit; `truncated` means the limit bit)
      filters                      — echoed back for the json manifest + audit
      lines                        — a GENERATOR of str chunks, one record per chunk
                                     for jsonl. Consume once. NOTE: the matching
                                     records are materialized (already-parsed dicts,
                                     bounded by rotation at MAX_RECORDS/MAX_BYTES)
                                     before the generator starts; only the
                                     SERIALIZATION is streamed. That is deliberate —
                                     it is why a concurrent capture or rotation cannot
                                     tear the output mid-stream.
    An unknown `fmt` raises ValueError. So does a malformed `key`: an empty or junk
    address must NOT degrade into "no key given", which is a whole-store download. A
    well-formed `key` that matches nothing yields count 0 and an empty stream — the
    caller reports "no such record" (web: 404)."""
    fmt = (fmt or "jsonl").lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(
            "unknown export format: {!r} (expected one of {})".format(
                fmt, ", ".join(EXPORT_FORMATS)
            )
        )
    if key is not None and not is_record_key(key):
        raise ValueError("malformed record key: %r (expected %d hex chars)" % (key, KEY_LEN))  # noqa: UP031
    filters = {
        "provider": provider or None,
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "document_id": document_id,
    }
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")

    if key:
        # INDIVIDUAL: exactly the record the viewer row showed, pretty-printed for
        # reading in an editor. No envelope — the file IS the record.
        rec = find_record(key)
        recs = [rec] if rec is not None else []
        fmt = "json"
        count = total = len(recs)
        truncated = False
        lines = _iter_pretty(recs)
    else:
        recs, total = _select(
            limit=limit,
            provider=provider,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            document_id=document_id,
        )
        count = len(recs)
        truncated = total > count
        if fmt == "jsonl":
            lines = _iter_jsonl(recs)
        else:
            lines = _iter_json_envelope(
                recs,
                filters=filters,
                limit=limit,
                count=count,
                total=total,
                truncated=truncated,
                exported_at=now.isoformat(),
            )
    return {
        "fmt": fmt,
        "media_type": "application/x-ndjson" if fmt == "jsonl" else "application/json",
        "filename": _export_filename(
            fmt=fmt,
            key=key,
            truncated=truncated,
            count=count,
            total=total,
            filters=filters,
            stamp=stamp,
        ),
        "count": count,
        "total": total,
        "truncated": truncated,
        "limit": limit,
        "filters": filters,
        "key": key or None,
        "lines": lines,
    }


def _iter_jsonl(recs):
    """One record per line, ASCII, compact — the capture file's OWN format, so
    external tooling can stream a download the same way it would the file."""
    for rec in recs:
        yield _dumps(rec) + "\n"


def _iter_pretty(recs):
    for rec in recs:
        yield _dumps(rec, indent=2) + "\n"


def _iter_json_envelope(recs, *, filters, limit, count, total, truncated, exported_at):
    """A single JSON document with a MANIFEST head — the format's advantage over
    jsonl is that truncation and the filter set travel INSIDE the file. Streamed
    piecewise so a 10 MB export never becomes a 10 MB string."""
    head = {
        "source": "loc-studio prompt-tap",
        "exported_at": exported_at,
        "filters": filters,
        "limit": limit,
        "count": count,
        "total_matching": total,
        "truncated": truncated,
    }
    yield "{\n"
    for k, v in head.items():
        yield f"  {_dumps(k)}: {_dumps(v)},\n"
    yield '  "records": [\n'
    for i, rec in enumerate(recs):
        yield "    " + _dumps(rec) + (",\n" if i < count - 1 else "\n")
    yield "  ]\n}\n"


def count_matching(
    *, provider=None, workflow_id=None, workflow_run_id=None, document_id=None
) -> int:
    """How many records match these filters, ignoring any display limit. NOTE: the
    viewer no longer calls this — it takes every number from `page_snapshot`'s single
    pass. Kept as the standalone count for callers that need it without the rows."""
    return _select(
        limit=0,
        provider=provider,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        document_id=document_id,
    )[1]


def count_legacy_records() -> int:
    """How many records live in the LEGACY global file — i.e. exactly what clear() would
    drop (proposal section 8). Surfaces show this beside the total so `status` and
    `clear` agree on what is clearable (the per-action run-capture files are not)."""
    return sum(1 for _ in _iter_file(_path()))


def clear() -> int:
    """Truncate the LEGACY global capture file, returning how many records were
    dropped. Best-effort; a missing file is a no-op (returns 0).

    SCOPED TO THE GLOBAL FILE ON PURPOSE (proposal section 8): `POST /prompt-tap/clear`
    clears only legacy/raw call capture. The per-action run-capture files are NOT
    cleared here and envelopes have no clear button at all — they fall only to the
    guarded, audited purge (Wave C). So the count reflects the global file alone, not
    the per-action files a broad count_records() would also see.

    LOCKED TRUNCATE (gate F round 1, M3b). The truncate is taken under the CROSS-PROCESS
    lock as well as _LOCK, because the writer it races is in another process: a rotation
    committing its `os.replace` between this count and this truncate puts the pre-clear
    record set back over a file an operator has just cleared (POSIX), or raises
    `[WinError 5]` in that writer and drops its capture (Windows). This is the one case
    in #104's family with a PRIVACY consequence -- the button exists to drop captured
    prompt BODIES -- so it must not be best-effort about the race.

    If the cross-process lock is unavailable the truncate still happens (an operator's
    clear may never be silently skipped) with a re-arming WARN naming the residual race.

    AND IT IGNORES THE L2 DEGRADE LATCH (gate F round 2, M1). The latch makes the CAPTURE
    path stop attempting a lock the filesystem has just refused, so a translation thread
    does not pay a 10 s poll loop per call. Consulting it here would mean that for 30 s
    after any capture's lock failure an operator's Clear did not even TRY to take the lock
    and fell straight to the racy truncate -- exactly what this docstring says it must not
    do. A Clear is rare and runs on a request thread, so it pays the timeout instead."""
    p = _path()
    n = sum(1 for _ in _iter_file(p))
    try:
        if p.exists():
            with _LOCK:
                try:
                    with _xproc_lock(ignore_latch=True):
                        _truncate_legacy(p)
                except _XProcLockUnavailable as exc:
                    _warn_ttl_sweep(
                        "xproc_lock_clear",
                        f"could not take the cross-process capture lock ({exc}); the "
                        "capture file was cleared anyway, so a rotation racing this "
                        "clear in another process could restore records",
                    )
                    _truncate_legacy(p)
    except OSError:
        log.debug("prompt_tap: clear failed", exc_info=True)
    return n


def _truncate_legacy(p) -> None:
    """Empty the legacy capture file in place. CALLER HOLDS _LOCK (and, when it could be
    taken, the cross-process lock)."""
    open(p, "w", encoding="utf-8").close()
