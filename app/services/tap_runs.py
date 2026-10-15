"""Prompt Tap grouped by TRANSLATION RUN (ROADMAP #6) -- the RUN-first read model.

The Prompt Tap has two grouped lenses now. `tap_view` reads the DB run-ENVELOPE
hierarchy (TapAction click -> TapRun leg -> calls). THIS module is the flatter,
capture-driven lens the maintainer asked for: "from the point I click translate, how does
everything run as a whole" -- one row per TRANSLATION RUN, drilling into its calls
grouped by step then unit. It reads the SAME capture files, keyed on the run identity
each call already carries in `tap_meta` (no new capture, no writes).

TWO RUN NAMESPACES, one lens:
  * CLASSIC translate calls carry `meta.run_id` == the TapRun envelope id (a 26-char
    ULID). Grouped by `step_key` then `segment_index`.
  * ADVANCED (LLM harness) calls carry `meta.advanced_run_id` (the AdvancedRun id) and
    `meta.run_id` == the correlation string `advrun-<id>`. Grouped by `unit_key` then
    `committee_role` -- "advanced runs group by unit_key" (the task's rule).
The PUBLIC run id is the classic ULID or `advrun-<int>`; both are URL/filename safe.

RING-OWNED (the standing multi-org lens). A run belongs to a ring: a classic run via
TapRun -> TapAction.org_id/customer_id; an advanced run via AdvancedRun.org_id/customer_id.
Under active org (or a within-org customer binding) a foreign run is HIDDEN from the list
and is a NON-ENUMERABLE 404 in the detail route -- and a run whose backing row is gone
(purged/deleted) resolves to no ring, which fails CLOSED under scoping. Single-tenant
(org_id None, customer_id None) applies no predicate -- byte-identical to before.

BODIES WITHHELD BY DEFAULT. Every call row is a metadata digest (prompt/response/system
lengths + full-text sha256 + a 24-char head ONLY for a body longer than 2*24 chars, so a
short body -- a translated UI string -- never ships its whole self as a head) -- safe to
serve over `/api` and MCP. Full
text is returned ONLY with `bodies=True`, which the surfaces gate exactly like the existing
raw tap view: host-only on the CLI (`require_local`), admin-only on the web page, and NEVER
over the token `/api` or MCP (the raw call bodies stay host-only, the standing egress law).

READ-ONLY + BEST-EFFORT. Enumeration is driven by `prompt_tap._iter_records()` (the capture
files), never by the DB rows, so a TTL-swept run vanishes for free. Every function degrades
to []/None on any failure -- a viewer must always render, never 500 (the tap_view contract).

Documented in: docs/architecture/connectors.md
"""

import hashlib
import json
import logging
import os
import re

from app.services import access

log = logging.getLogger("loc_tms.tap_runs")

# Cap the calls materialized per unit so one huge run cannot blow up the page DOM / CLI
# output; the true count is still reported and the row says "showing N of M".
CALLS_PER_UNIT_CAP = 250
DEFAULT_RUN_LIMIT = 50
MAX_RUN_LIMIT = 500  # M4: the list page ceiling (paginate past it with `cursor`)
HEAD_CHARS = 24  # the bodies-withheld head preview length (task: "head-24")
# L4: the run-list sort key AND the pagination cursor join (last_ts, run_id) with this
# separator. It is below every character an ISO-8601 ts or a run id can hold, so the flat
# string orders exactly like the (last_ts, run_id) tuple -- a ts-less run sorts LAST, never
# stranded off later pages the way the old '|' separator (0x7C, above every ts char) stranded it.
_CURSOR_SEP = "\x1f"

# M4 (gate round 1): BOUND THE LIST WALK. A run summaries INDEX cached on disk beside the
# captures (prompt_tap/runs.index.json), rebuilt by ONE lock-free full walk only when the
# store fingerprint (legacy file + per-action files, by mtime+size) changed, or the index is
# missing/corrupt. list_runs then reads the index, never the whole store. An in-process cache
# holds the built index for _INDEX_TTL_S so an agent looping the read-tier `ledger_runs` MCP
# tool (or the remote GET) cannot force back-to-back full walks even during active capture --
# it serves the (at most _INDEX_TTL_S-old) cached index instead. The index is a diagnostic
# accelerator, NEVER authoritative: run_detail always reads the real file, and the ring
# confine is a FRESH batched DB read on every list (a purged run must fail closed at once, so
# ownership is never cached). The capture hot path is UNTOUCHED (the fingerprint catches every
# append/rotation/sweep on the READ side); see the errata for why no per-capture index write.
_INDEX_NAME = "runs.index.json"
_INDEX_VERSION = 1
_INDEX_TTL_S = 2.0
# In-process cache: the last-built index, its fingerprint, the monotonic time the last build
# FINISHED (`at`), and how long that build took (`dur`). M1: the append-throttle window is
# measured from `at` (build END) and is clamped to at least `dur`, so a store whose rebuild
# costs more than _INDEX_TTL_S self-throttles instead of expiring its own window mid-build.
_index_cache: dict = {"idx": None, "fp": None, "at": 0.0, "dur": 0.0}

# An advanced run's public id: `advrun-<AdvancedRun.id>` (the correlation string the harness
# stamps into every advanced tap_meta as `run_id`). \Z (not $) so a trailing newline cannot
# sneak past the anchor.
_ADV_RE = re.compile(r"^advrun-([0-9]{1,18})\Z")


def _session():
    from sqlmodel import Session

    from app.db import engine

    return Session(engine)


def is_run_key(value) -> bool:
    """True for a well-formed public run id -- a classic 26-char ULID or `advrun-<int>`.
    Surfaces validate BEFORE a lookup so junk is a clean 404, never a 500."""
    if not value:
        return False
    from app.services import prompt_tap

    v = str(value)
    return bool(prompt_tap.is_action_id(v)) or bool(_ADV_RE.match(v))


def _run_key_of(meta):
    """(run_key, kind) for a capture's meta, or (None, None) when it belongs to no run.
    ADVANCED-first: an advanced call carries BOTH advanced_run_id and a `advrun-` run_id,
    so the advanced namespace must win (a bare run_id check would misfile it as classic)."""
    if not isinstance(meta, dict):
        return None, None
    adv = meta.get("advanced_run_id")
    if adv is not None:
        rid = meta.get("run_id")
        # Prefer the stamped correlation string; synthesize it if a legacy record omitted it.
        key = str(rid) if rid and _ADV_RE.match(str(rid)) else f"advrun-{adv}"
        return key, "advanced"
    rid = meta.get("run_id")
    from app.services import prompt_tap

    if rid and prompt_tap.is_action_id(str(rid)):
        return str(rid), "classic"
    return None, None


def _adv_id_of(run_key):
    """The int AdvancedRun id inside an `advrun-<id>` key, or None."""
    m = _ADV_RE.match(str(run_key or ""))
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Ring resolution (batched DB reads; best-effort)
# --------------------------------------------------------------------------- #
def _rings_for(s, run_keys, user=access.UNSCOPED):
    """{run_key: (org_id, customer_id)} for the given keys, in two batched reads (classic
    via TapRun -> TapAction, advanced via AdvancedRun). A key whose backing row is gone is
    ABSENT from the map -- the caller treats that as no ring (fail closed under scoping).
    Best-effort: {} on any failure (the caller then hides everything under active scoping)."""
    out = {}
    allowed = None if user is access.UNSCOPED else access.visible_project_ids(s, user)
    if allowed is not None:
        allowed = set(allowed)
    keys = [k for k in run_keys if k]
    if not keys:
        return out
    classic = [k for k in keys if not _ADV_RE.match(k)]
    adv = {k: _adv_id_of(k) for k in keys if _ADV_RE.match(k)}
    try:
        from sqlmodel import select

        from app.models import AdvancedRun, TapAction, TapRun
        from app.services.tap_view import _action_project_visible

        if classic:
            runs = s.exec(select(TapRun).where(TapRun.id.in_(classic))).all()
            act_ids = {r.action_id for r in runs if r.action_id}
            actions = {}
            if act_ids:
                arows0 = s.exec(select(TapAction).where(TapAction.id.in_(act_ids))).all()
                actions = {a.id: a for a in arows0}
            for r in runs:
                a = actions.get(r.action_id)
                if a is not None and (
                    allowed is None
                    or a.project_id in allowed
                    or (a.project_id is None and _action_project_visible(s, user, a))
                ):
                    out[r.id] = (a.org_id, a.customer_id)
        if adv:
            ids = sorted({v for v in adv.values() if v is not None})
            if ids:
                arows1 = s.exec(select(AdvancedRun).where(AdvancedRun.id.in_(ids))).all()
                arows = {a.id: a for a in arows1}
                for k, aid in adv.items():
                    a = arows.get(aid)
                    if a is not None and (allowed is None or a.project_id in allowed):
                        out[k] = (a.org_id, a.customer_id)
    except Exception:
        log.warning("tap_runs._rings_for failed", exc_info=True)
        return {}
    return out


def _ring_visible(ring, org_id, customer_id) -> bool:
    """Whether a run with `ring` == (run_org, run_customer) is visible to a caller scoped to
    (org_id, customer_id). Single-tenant / unscoped (both None) -> always True. Under active
    scoping an UNKNOWN ring (row gone) fails CLOSED."""
    if org_id is None and customer_id is None:
        return True
    run_org, run_cust = ring if ring else (None, None)
    if org_id is not None and run_org != org_id:
        return False
    if customer_id is not None and run_cust != customer_id:  # noqa: SIM103
        return False
    return True


# --------------------------------------------------------------------------- #
# Body digests + call rows
# --------------------------------------------------------------------------- #
def _digest(text, *, bodies):
    """The bodies-withheld digest of one text channel: {chars, sha256[, head]}. `sha256` is
    the FULL hex of the utf-8 text ("" for empty).

    `head` (the first HEAD_CHARS characters) is emitted ONLY when the body is LONGER than
    2*HEAD_CHARS -- the house rule from advanced/transcript.py:_digest_body (round-1 M2 there,
    M1 here). At or under 2*HEAD_CHARS a `head` would BE (or all but be) the whole body, and
    this digest crosses the `/api` + MCP egress boundary the module claims withholds bodies;
    a translated UI string ("Book" -> "Reservar") is exactly this short-body class. So a short
    body yields ONLY {chars, sha256} on the withheld view. `sha256` is the full hex of the full
    text -- a short target is dictionary-recoverable from it, a bounded residue documented in
    the errata (the reader is already entitled to that org's content; a content hash is what
    makes the digest useful for diffing).

    `text` (the full body) is attached ONLY when `bodies` -- the operator-gated host path."""
    s = text or ""
    d = {
        "chars": len(s),
        "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest() if s else "",
    }
    if len(s) > 2 * HEAD_CHARS:
        d["head"] = s[:HEAD_CHARS]
    if bodies:
        d["text"] = text
    return d


def _call_row(rec, *, bodies, include_key):
    """One call as a row: metadata always, body digests always, full text only with
    `bodies`. `include_key` attaches the content-address download key (web/host paths only;
    stripped on the token API / MCP, mirroring tap_view's _strip_record_keys discipline)."""
    from app.services import prompt_tap

    req = rec.get("request") or {}
    resp = rec.get("response") or {}
    usage = rec.get("usage") or {}
    row = {
        "ts": rec.get("ts"),
        "round": (rec.get("meta") or {}).get("round"),
        "purpose": _call_purpose(rec),
        "provider": rec.get("provider"),
        "provider_kind": rec.get("provider_kind"),
        "model": rec.get("model"),
        "task": rec.get("task"),
        "outcome": rec.get("outcome"),
        "attempt": rec.get("attempt"),
        "finish_reason": rec.get("finish_reason"),
        "error_class": rec.get("error_class"),
        "elapsed_ms": rec.get("elapsed_ms"),
        "output_chars": rec.get("output_chars"),
        "reasoning_tokens": rec.get("reasoning_tokens"),
        # #342: the sampling a LOCAL call sent (None for every other provider / legacy record).
        "temperature": rec.get("temperature"),
        "seed": rec.get("seed"),
        "cost_usd": rec.get("cost_usd"),
        "usage": dict(usage) if usage else None,
        "redacted": bool(rec.get("redacted")),
        "prompt": _digest(req.get("prompt"), bodies=bodies),
        "system": _digest(req.get("system"), bodies=bodies),
        "response": _digest(resp.get("text"), bodies=bodies),
    }
    if rec.get("error"):
        # The error channel is body-like (an upstream can echo content), so it follows the
        # same gate: a digest by default, the full string only with bodies.
        row["error"] = _digest(str(rec.get("error")), bodies=bodies)
    # L5 (gate round 1): segment_index is a PER-CALL fact -- it stays on the call row (NOT folded
    # into the run summary, where whichever segment captured first would masquerade as the run's).
    seg = (rec.get("meta") or {}).get("segment_index")
    if seg is not None:
        row["segment_index"] = seg
    if include_key:
        try:
            row["record_key"] = prompt_tap.record_key(rec)
        except Exception:
            row["record_key"] = ""
    return row


def _call_purpose(rec):
    meta = rec.get("meta") or {}
    purpose = meta.get("purpose")
    if purpose == "cross_check":
        return "cross-check"
    task = rec.get("task") or ""
    if task == "jev_refine":
        return "auto-fix post-editor"
    if task in ("qe", "qe_jev") or task.startswith("qe@"):
        return "qe judge"
    return purpose or ("translate" if task == "translate" else task or None)


def _engine_of(rec) -> str:
    """A compact `provider/model` engine label for the run's engine set."""
    prov = rec.get("provider") or rec.get("provider_kind") or ""
    model = rec.get("model") or ""
    return f"{prov}/{model}" if model else str(prov)


def _spend_of(rec):
    """The exact per-call USD the record carries, or None when it recorded none."""
    c = rec.get("cost_usd")
    if c is None:
        return None
    try:
        return float(c)
    except (TypeError, ValueError):
        return None


def _tokens_of(rec):
    """(tokens_in, tokens_out) folding input + cache_read + cache_write on the in side, so
    the run total agrees with the envelope rollup (tap_enrich._rollup_calls)."""
    u = rec.get("usage") or {}
    try:
        ti = (
            int(u.get("input_tokens", 0) or 0)
            + int(u.get("cache_read_tokens", 0) or 0)
            + int(u.get("cache_write_tokens", 0) or 0)
        )
        to = int(u.get("output_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0
    return ti, to


# --------------------------------------------------------------------------- #
# The aggregate walk: one pass over the capture files -> per-run accumulators
# --------------------------------------------------------------------------- #
class _Acc:
    __slots__ = (
        "calls",
        "engines",
        "failures",
        "file",
        "first_ts",
        "ident",
        "kind",
        "last_ts",
        "run_key",
        "spend",
        "spend_present",
        "tokens_in",
        "tokens_out",
    )

    def __init__(self, run_key, kind):
        self.run_key = run_key
        self.kind = kind
        self.calls = 0
        self.failures = 0
        self.first_ts = None
        self.last_ts = None
        self.engines = set()
        self.spend = 0.0
        self.spend_present = False
        self.tokens_in = 0
        self.tokens_out = 0
        self.ident = {}
        # M4: the ONE capture file this run's records live in (a classic run -> its per-action
        # file; an advanced run -> the legacy global file). run_detail reads only this file.
        self.file = None

    def add(self, rec):
        self.calls += 1
        if (rec.get("outcome") or "") != "ok":
            self.failures += 1
        ts = rec.get("ts") or ""
        if ts:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
        eng = _engine_of(rec)
        if eng:
            self.engines.add(eng)
        sp = _spend_of(rec)
        if sp is not None:
            self.spend_present = True
            self.spend += sp
        ti, to = _tokens_of(rec)
        self.tokens_in += ti
        self.tokens_out += to
        meta = rec.get("meta") or {}
        # L6: latch the first NON-EMPTY capture file, not merely the first. _file_of returns ""
        # for a classic record with no usable action_id; if that were the first record the run
        # would store file "" and later, if its TapRun row is gone, run_detail would resolve no
        # file and 404 while the list still shows the run. Keep looking until a real file appears.
        if not self.file:
            f = _file_of(meta, self.kind)
            if f:
                self.file = f
        # Identity hints -- recorded once, from whichever call carries them (they are
        # constant across a run). Never joined back to a live row (unenforced-reference safe).
        # L5 (gate round 1): segment_index is a PER-CALL fact (a classic run spans many
        # segments), NOT run-constant, so it is NOT a run-level ident -- it stays on each call
        # row instead (whichever segment captured first must not masquerade as "the run's").
        if self.kind == "advanced":
            for k in ("advanced_run_id", "advanced_mission_id"):
                if meta.get(k) is not None and k not in self.ident:
                    self.ident[k] = meta.get(k)
        else:
            for k in ("workflow_run_id", "workflow_id", "document_id"):
                if meta.get(k) is not None and self.ident.get(k) is None:
                    self.ident[k] = meta.get(k)

    def summary(self):
        out = {
            "run_id": self.run_key,
            "kind": self.kind,
            "calls": self.calls,
            "captured_calls": self.calls,  # what the capture fold actually holds (see M5 marks)
            "failures": self.failures,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "engines": sorted(self.engines),
            "spend_usd": round(self.spend, 8) if self.spend_present else None,
            "spend_present": self.spend_present,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "has_failures": bool(self.failures),
            "bodies_withheld": True,
        }
        out.update(self.ident)
        return out


def _file_of(meta, kind) -> str:
    """The relative capture-file name a run's records live in. CLASSIC calls carry a ULID
    `action_id` -> the per-action file `actions/<action_id>.jsonl` (NOT rotated). ADVANCED
    calls have a uuid4 action_id (not a ULID) -> the LEGACY global file (rotated). Empty when
    a classic record somehow lacks a usable action_id (falls back to a legacy read)."""
    if kind != "advanced":
        from app.services import prompt_tap

        aid = meta.get("action_id")
        if aid and prompt_tap.is_action_id(str(aid)):
            return f"{prompt_tap.ACTIONS_SUBDIR}/{aid}.jsonl"
        return ""
    return "captures.jsonl"


# --------------------------------------------------------------------------- #
# M5: per-run honesty marks (the fold vs the authoritative envelope)
# --------------------------------------------------------------------------- #
def _envelope_marks(s, fold_calls):
    """{run_key: mark} where a run's CAPTURE fold under-reports vs its authoritative envelope
    (M5 -- silent truncation is banned by the project doctrine). `fold_calls` is
    {run_key: captured_call_count}. Best-effort: {} on failure.

    M3 (gate round 2): these marks are DB-derived, so they are computed FRESH per read (list and
    detail alike) rather than folded into the on-disk index -- a `capture_withheld`/`partial`
    flag set with no intervening capture write must still surface on the LIST, and a cached
    index would freeze the (unchanged) fingerprint and hide it. The read is one batched query
    by id per run kind, cheap next to the walk the index already saved.

    CLASSIC: the per-action file is NOT rotated, so a classic run only loses calls to the
    disk-budget backstop, which flags the ENVELOPE (TapAction.capture_withheld) while it keeps
    recording. Surface that as `capture_withheld: true` on the run.

    ADVANCED: the legacy file IS rotated, so an advanced run's oldest calls can be evicted by
    unrelated traffic. AdvancedRun carries the LIVE actuals (provider_calls / spent_usd /
    tokens_*), an independent count the fold can be checked against: when the envelope counted
    more provider calls than the fold holds, mark `partial: true` and carry the envelope totals
    so calls / spend_usd / tokens never silently under-report."""
    out = {}
    keys = list(fold_calls.keys())
    if not keys:
        return out
    classic = [k for k in keys if not _ADV_RE.match(k)]
    adv = {k: _adv_id_of(k) for k in keys if _ADV_RE.match(k)}
    try:
        from sqlmodel import select

        from app.models import AdvancedRun, TapAction, TapRun

        if classic:
            runs = s.exec(select(TapRun).where(TapRun.id.in_(classic))).all()
            act_ids = {r.action_id for r in runs if r.action_id}
            actions = {}
            if act_ids:
                for a in s.exec(select(TapAction).where(TapAction.id.in_(act_ids))).all():
                    actions[a.id] = a
            for r in runs:
                a = actions.get(r.action_id)
                if a is not None and getattr(a, "capture_withheld", False):
                    out[r.id] = {"capture_withheld": True}
        if adv:
            ids = sorted({v for v in adv.values() if v is not None})
            rows = {}
            if ids:
                for a in s.exec(select(AdvancedRun).where(AdvancedRun.id.in_(ids))).all():
                    rows[a.id] = a
            for k, aid in adv.items():
                a = rows.get(aid)
                if a is None:
                    continue
                env_calls = int(getattr(a, "provider_calls", 0) or 0)
                if env_calls > int(fold_calls.get(k) or 0):
                    out[k] = {
                        "partial": True,
                        "env_calls": env_calls,
                        "env_spend": float(getattr(a, "spent_usd", 0.0) or 0.0),
                        "env_tokens_in": int(getattr(a, "tokens_in", 0) or 0),
                        "env_tokens_out": int(getattr(a, "tokens_out", 0) or 0),
                    }
    except Exception:
        log.warning("tap_runs._envelope_marks failed", exc_info=True)
        return {}
    return out


def _apply_marks(summ, mk):
    """Fold an M5 mark into a run summary IN PLACE. A partial advanced run reports the
    envelope's authoritative totals (never LESS than the fold) beside `captured_calls` (what
    the fold holds) and `partial: true`; a withheld classic run carries `capture_withheld`."""
    if not mk:
        return summ
    if mk.get("capture_withheld"):
        summ["capture_withheld"] = True
    if mk.get("partial"):
        summ["partial"] = True
        summ["calls"] = max(int(summ.get("calls") or 0), int(mk.get("env_calls") or 0))
        env_spend = mk.get("env_spend")
        if env_spend is not None and (
            summ.get("spend_usd") is None or env_spend > summ["spend_usd"]
        ):
            summ["spend_usd"] = round(float(env_spend), 8)
            summ["spend_present"] = True
        summ["tokens_in"] = max(int(summ.get("tokens_in") or 0), int(mk.get("env_tokens_in") or 0))
        summ["tokens_out"] = max(
            int(summ.get("tokens_out") or 0), int(mk.get("env_tokens_out") or 0)
        )
    return summ


# --------------------------------------------------------------------------- #
# M4: the on-disk run-summaries index (bounds the list walk)
# --------------------------------------------------------------------------- #
def _index_path():
    from app.services import prompt_tap

    return prompt_tap._dir() / _INDEX_NAME


def _store_fingerprint():
    """A cheap fingerprint of the capture store: (mtime, size) of the legacy file plus each
    per-action file. Any append, rotation, TTL sweep or clear changes it, so the index rebuilds
    lazily on the READ side without the capture path ever writing the index. O(files) stat only."""
    from app.services import prompt_tap

    fp = {"legacy": None, "actions": {}}
    try:
        st = prompt_tap._path().stat()
        fp["legacy"] = [st.st_mtime, st.st_size]
    except OSError:
        pass
    ad = prompt_tap._actions_dir()
    try:
        names = os.listdir(ad) if ad.exists() else []
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        try:
            st = (ad / name).stat()
            fp["actions"][name] = [st.st_mtime, st.st_size]
        except OSError:
            pass
    return fp


def _load_index():
    """The persisted index dict, or None (missing / corrupt / wrong version)."""
    try:
        with open(_index_path(), encoding="utf-8") as f:
            idx = json.load(f)
        if (
            isinstance(idx, dict)
            and idx.get("v") == _INDEX_VERSION
            and isinstance(idx.get("runs"), dict)
        ):
            return idx
    except (OSError, ValueError, TypeError):
        pass
    return None


def _store_index(idx):
    """Write the index atomically (temp + os.replace). Best-effort: a failure just means the
    next read rebuilds -- never raises into a caller. L5: NEVER create the capture directory
    just to persist an index -- a read path must not write on a tap-disabled install that has
    never captured. If the store dir does not exist there is nothing to index (the index would
    be empty), so skip; the capture path creates the dir when it first records and the index
    rejoins it then."""
    try:
        p = _index_path()
        if not p.parent.exists():
            return
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=True)
        os.replace(tmp, str(p))
    except OSError:
        log.debug("tap_runs: index write failed (will rebuild on next read)", exc_info=True)


def _build_index(fp):
    """ONE lock-free full walk of the capture store -> {run_key: summary(+file)}. Called only
    when the fingerprint changed or the index is missing. NO DB reads here: M3 keeps the
    DB-derived M5 marks (`partial`/`capture_withheld`) OUT of the stored index -- they are
    re-derived FRESH per read (list_runs_page / run_detail), so a DB-only flag surfaces even
    when the capture store (and thus the fingerprint) has not moved. The index carries only the
    capture-fold summary (calls, captured_calls, spend, tokens) plus the `file` hint."""
    from app.services import prompt_tap

    accs = {}
    for rec in prompt_tap._iter_records():
        key, kind = _run_key_of(rec.get("meta") or {})
        if key is None:
            continue
        acc = accs.get(key)
        if acc is None:
            acc = accs[key] = _Acc(key, kind)
        acc.add(rec)
    runs = {}
    for key, acc in accs.items():
        summ = acc.summary()
        summ["file"] = acc.file or ""
        runs[key] = summ
    from datetime import datetime, timezone

    built_at = datetime.now(timezone.utc).isoformat()
    return {"v": _INDEX_VERSION, "fp": fp, "runs": runs, "built_at": built_at}


def _fp_is_pure_growth(old, new) -> bool:
    """True when `new` differs from `old` ONLY by APPENDS -- the legacy file did not shrink or
    vanish and no per-action file shrank or vanished (new files may appear). That is the active-
    capture signature. An EVICTION (rotation / TTL sweep / clear shrinks or removes a file) is
    NOT pure growth, so it is never throttled and always rebuilds at once (a swept run must
    vanish immediately, in the tests and in production)."""
    if not old or not new:
        return False
    o_leg, n_leg = old.get("legacy"), new.get("legacy")
    if o_leg is not None and (n_leg is None or n_leg[1] < o_leg[1]):
        return False
    o_act, n_act = old.get("actions") or {}, new.get("actions") or {}
    for name, meta in o_act.items():
        nm = n_act.get(name)
        if nm is None or nm[1] < meta[1]:
            return False
    return True


def _get_index():
    """The run-summaries index, rebuilt by a full walk ONLY when the store fingerprint changed
    (or the index is missing/corrupt). The fingerprint is a cheap stat, so the warm path (store
    unchanged) never walks; an EVICTION always rebuilds at once; and a pure-APPEND change (active
    capture) is throttled to at most one rebuild per _INDEX_TTL_S -- so an agent looping the
    read-tier list cannot force back-to-back full walks during a live batch (it sees an index at
    most _INDEX_TTL_S stale, a run that just started may lag by that window). Best-effort: an
    index with an empty `runs` on any failure -- the lens degrades to 'no runs', never 500s."""
    import time as _t

    try:
        c = _index_cache
        now = _t.monotonic()
        fp = _store_fingerprint()
        if c["idx"] is not None:
            if c["fp"] == fp:
                return c["idx"]  # store unchanged -> serve the cache (no walk)
            # M1: the append-throttle window runs from the END of the last build (c["at"]) and is
            # at least that build's own duration, so a rebuild slower than _INDEX_TTL_S cannot
            # expire its window before it returns (which put list_runs back to a full walk per
            # call under active capture). Evictions are never pure-growth and rebuild at once.
            ttl = c["dur"] if c["dur"] > _INDEX_TTL_S else _INDEX_TTL_S
            if _fp_is_pure_growth(c["fp"], fp) and (now - c["at"]) < ttl:
                return c["idx"]  # appends only + throttled -> serve a slightly-stale index
        disk = _load_index()
        if disk is not None and disk.get("fp") == fp:
            c.update(idx=disk, fp=fp, at=_t.monotonic(), dur=0.0)
            return disk
        t0 = _t.monotonic()
        idx = _build_index(fp)
        _store_index(idx)
        end = _t.monotonic()
        c.update(idx=idx, fp=fp, at=end, dur=end - t0)
        return idx
    except Exception:
        log.warning("tap_runs._get_index failed (empty index)", exc_info=True)
        return {"v": _INDEX_VERSION, "fp": None, "runs": {}, "built_at": None}


def invalidate_index() -> None:
    """Drop the in-process index cache (the next read re-checks the fingerprint and rebuilds if
    needed). A cheap, always-safe poke -- used by tests after a forced sweep, and available to any
    caller that wants the very next list to reflect a mutation it just made without the throttle."""
    _index_cache.update(idx=None, fp=None, at=0.0, dur=0.0)


def _clamp_limit(limit) -> int:
    """A run-list page size in [1, MAX_RUN_LIMIT], defaulting to DEFAULT_RUN_LIMIT."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_RUN_LIMIT
    if n <= 0:
        return DEFAULT_RUN_LIMIT
    return min(n, MAX_RUN_LIMIT)


def _cursor_key(summ) -> str:
    """The opaque pagination cursor for a run row AND the list sort key -- ONE relation, so
    paging can never diverge from the sort (L4). The row is keyed on (last_ts, run_id) joined by
    _CURSOR_SEP; the separator sorts BELOW every character an ISO-8601 `ts` or a run id can carry
    ('+' 0x2B is the lowest such char), so the flat string orders exactly like the (last_ts,
    run_id) tuple -- including a run whose captures have no `ts` (last_ts ""), which then sorts
    LAST here just as the tuple would, instead of the old '|' (0x7C) separator that sorted such a
    run ABOVE every timestamp and stranded it off every page but the first. The list is
    newest-first (DESC), so the NEXT page is the rows whose key is strictly LESS than this."""
    return (summ.get("last_ts") or "") + _CURSOR_SEP + (summ.get("run_id") or "")


def _summary_public(summ) -> dict:
    """A copy of a stored index summary with the INTERNAL `file` key dropped (it is the capture
    file location, a host detail; never part of the served payload)."""
    return {k: v for k, v in summ.items() if k != "file"}


def list_runs_page(
    *,
    limit=DEFAULT_RUN_LIMIT,
    cursor=None,
    org_id=None,
    customer_id=None,
    since=None,
    until=None,
    has_failures=False,
    user=access.UNSCOPED,
):
    """{"runs":[...], "truncated":bool, "next_cursor":str|None} -- the paginated run list.
    Reads the M4 index (no full store walk on the warm path), applies a FRESH ring confine (a
    batched DB read; ownership is never cached, so a purged run fails closed at once), the
    since/until/has_failures filters, sorts newest-first, then pages by `limit` (<= MAX_RUN_LIMIT)
    and an opaque `cursor`. Best-effort: an empty page on any failure."""
    try:
        from app.services import access

        # #73: a NULL principal under active multi-org is BLOCKED (fail closed), exactly like
        # vendors.list_vendors; then collapse the authorized see-all sentinel to a concrete None
        # before _ring_visible (the raw UNSCOPED sentinel matches no run's org).
        if access.org_scope_blocked(org_id):
            return {"runs": [], "truncated": False, "next_cursor": None}
        org_id = access.org_scope_value(org_id)

        runs_map = _get_index().get("runs") or {}
        if not runs_map:
            return {"runs": [], "truncated": False, "next_cursor": None}
        with _session() as s:
            rings = _rings_for(s, list(runs_map.keys()), user)
        needs_membership = user is not access.UNSCOPED and not access.can_see_all(user)
        since_s = _date_lb(since)
        until_s = _date_ub(until)
        out = []
        for key, summ in runs_map.items():
            if needs_membership and key not in rings:
                continue
            if not _ring_visible(rings.get(key), org_id, customer_id):
                continue
            if has_failures and not summ.get("failures"):
                continue
            ft = summ.get("first_ts") or ""
            if since_s is not None and ft < since_s:
                continue
            if until_s is not None and ft > until_s:
                continue
            out.append(summ)
        out.sort(key=_cursor_key, reverse=True)
        if cursor:
            cur = str(cursor)
            out = [r for r in out if _cursor_key(r) < cur]
        lim = _clamp_limit(limit)
        page = [_summary_public(r) for r in out[:lim]]
        # M3: re-derive the DB-derived M5 marks FRESH for the paged runs (bounded batched read)
        # and apply them to the PUBLIC copies -- so `partial`/`capture_withheld` surface on the
        # LIST even when set with no intervening capture write, and the cached index is never
        # mutated. The fold count comes from the summary's `captured_calls`.
        try:
            fold = {r["run_id"]: int(r.get("captured_calls") or 0) for r in page if r.get("run_id")}
            if fold:
                with _session() as s:
                    marks = _envelope_marks(s, fold)
                for r in page:
                    _apply_marks(r, marks.get(r.get("run_id")))
        except Exception:
            log.warning("tap_runs.list_runs_page marks failed", exc_info=True)
        truncated = len(out) > lim
        next_cursor = _cursor_key(out[lim - 1]) if truncated and page else None
        return {"runs": page, "truncated": truncated, "next_cursor": next_cursor}
    except Exception:
        log.warning("tap_runs.list_runs_page failed (returning empty)", exc_info=True)
        return {"runs": [], "truncated": False, "next_cursor": None}


def list_runs(**kwargs):
    """The run list as a bare list (the web + human CLI render path). See list_runs_page for the
    filters + pagination; this drops the truncated/next_cursor envelope."""
    return list_runs_page(**kwargs)["runs"]


def _date_lb(value):
    """A yyyy-mm-dd lower bound as the start-of-day ISO prefix, or None. Compared against a
    UTC ISO `ts` string lexically (both ISO-8601), so a bare date sorts before any time."""
    v = (value or "").strip()
    return v[:10] if v else None


def _date_ub(value):
    """A yyyy-mm-dd upper bound. `<= until` must include the whole day, so pad the date with
    the max time so any `ts` on that day passes; a full ISO value is used verbatim."""
    v = (value or "").strip()
    if not v:
        return None
    return (v[:10] + "T99") if len(v) <= 10 else v


def _step_unit(meta, kind):
    """(step_label, unit_label) for a call, by run kind. Classic groups by step_key then
    segment_index; advanced groups by unit_key then committee_role (the task's rule:
    'advanced runs group by unit_key' -- unit_key is the top level)."""
    if kind == "advanced":
        step = meta.get("unit_key")
        unit = meta.get("committee_role")
    else:
        step = meta.get("step_key") or meta.get("step_name")
        unit = meta.get("segment_index")
    step_s = str(step) if step is not None and str(step) != "" else None
    return step_s, unit


def _files_for(s, run_key, kind):
    """The capture file(s) a run's records live in -- so run_detail reads ONE file, not the
    whole store (M4). ADVANCED: the legacy global file (rotated, bounded). CLASSIC: the run's
    own per-action file, resolved TapRun -> action_id (a fresh one-row DB read), or, if that row
    is gone, the file the M4 index recorded for it. [] when it cannot be located (-> a 404)."""
    from app.services import prompt_tap

    if kind == "advanced":
        return [prompt_tap._path()]
    try:
        from sqlmodel import select

        from app.models import TapRun

        row = s.exec(select(TapRun).where(TapRun.id == run_key)).first()
        if row is not None and row.action_id and prompt_tap.is_action_id(str(row.action_id)):
            return [prompt_tap._action_path(row.action_id)]
    except Exception:
        log.warning("tap_runs._files_for failed", exc_info=True)
    ent = (_get_index().get("runs") or {}).get(run_key)
    rel = (ent or {}).get("file")
    if rel:
        return [prompt_tap._dir() / rel]
    return []


def run_detail(
    run_key,
    *,
    bodies=False,
    include_keys=False,
    org_id=None,
    customer_id=None,
    user=access.UNSCOPED,
):
    """One run's calls grouped by step then unit, or None (unknown / foreign ring / malformed
    -- all a non-enumerable 404 to the caller). Serves `GET /api/prompt-tap/runs/{id}`, the web
    drill-in, and `prompt-tap run <id>`.

    M4: reads only THIS run's own capture file (a classic run's per-action file, or the rotated
    legacy file for an advanced run) -- never the whole store. `bodies` returns the full
    prompt/response/system/error text (the operator-gated path -- the surfaces gate it: host-only
    CLI, admin web, NEVER the token API / MCP). Default is the bodies-withheld digest (lengths +
    sha256 + a head only past 2*HEAD_CHARS). `include_keys` adds the per-call download address
    (web/host only). Best-effort: None on any failure."""
    try:
        run_key = (str(run_key or "")).strip()
        if not is_run_key(run_key):
            return None
        from app.services import access, prompt_tap

        # #73: a BLOCKED principal (NULL org under active multi-org) reads nothing.
        if access.org_scope_blocked(org_id):
            return None
        # Collapse the authorized see-all sentinel to a concrete None before _ring_visible
        # (same reason as list_runs -- the raw UNSCOPED sentinel matches no run's org).
        org_id = access.org_scope_value(org_id)

        kind = "advanced" if _ADV_RE.match(run_key) else "classic"
        # Ring confine + targeted file resolve in one DB read window; confine BEFORE any read.
        with _session() as s:
            rings = _rings_for(s, [run_key], user)
            if (
                user is not access.UNSCOPED
                and not access.can_see_all(user)
                and run_key not in rings
            ):
                return None
            files = _files_for(s, run_key, kind)
        if not _ring_visible(rings.get(run_key), org_id, customer_id):
            return None  # foreign / unknown ring -> non-enumerable 404
        recs = []
        for p in files:
            for rec in prompt_tap._iter_file(p):
                key, _k = _run_key_of(rec.get("meta") or {})
                if key == run_key:
                    recs.append(rec)
        if not recs:
            return None  # no captures for this run (unknown / TTL-swept) -> 404
        recs.sort(key=lambda r: r.get("ts") or "")  # oldest-first within the run
        acc = _Acc(run_key, kind)
        # step_label -> unit_label -> list of (rec)
        steps = {}
        step_order = []
        for rec in recs:
            acc.add(rec)
            step_label, unit_label = _step_unit(rec.get("meta") or {}, kind)
            sk = step_label if step_label is not None else "-"
            if sk not in steps:
                steps[sk] = {"order": [], "units": {}}
                step_order.append(sk)
            uk = unit_label if unit_label is not None else "-"
            bucket = steps[sk]
            if uk not in bucket["units"]:
                bucket["units"][uk] = []
                bucket["order"].append(uk)
            bucket["units"][uk].append(rec)
        step_views = []
        for sk in step_order:
            bucket = steps[sk]
            unit_views = []
            for uk in bucket["order"]:
                unit_recs = bucket["units"][uk]
                total = len(unit_recs)
                shown = unit_recs[:CALLS_PER_UNIT_CAP]
                unit_views.append(
                    {
                        "unit": None if uk == "-" else uk,
                        "call_count": total,
                        "more": max(0, total - len(shown)),
                        "calls": [
                            _call_row(r, bodies=bodies, include_key=include_keys) for r in shown
                        ],
                    }
                )
            step_views.append(
                {
                    "step": None if sk == "-" else sk,
                    "units": unit_views,
                    "call_count": sum(u["call_count"] for u in unit_views),
                }
            )
        detail = acc.summary()
        detail["bodies_withheld"] = not bodies
        detail["steps"] = step_views
        # M5: mark the detail if the fold under-reports vs the authoritative envelope. Derived
        # FRESH here (M3), not read from the index -- a DB-only flag must still surface.
        try:
            with _session() as s:
                marks = _envelope_marks(s, {run_key: acc.calls})
            _apply_marks(detail, marks.get(run_key))
        except Exception:
            log.warning("tap_runs.run_detail marks failed", exc_info=True)
        return detail
    except Exception:
        log.warning("tap_runs.run_detail failed", exc_info=True)
        return None
