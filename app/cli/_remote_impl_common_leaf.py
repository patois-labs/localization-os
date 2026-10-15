"""_remote_impl_common: shared leaf helpers (layered pure move)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import os
import time

from app.cli._common import (
    EXIT_USAGE,
    CliError,
)
from app.cli._common import TERM_META_FLAGS as _TERM_META_FLAGS  # noqa: F401 (re-exported)
from app.cli._common import term_meta_from_args as _term_meta_from_args  # noqa: F401


def _read_bytes(path: str) -> bytes:
    if not os.path.isfile(path):
        raise CliError(f"file not found: {path}", EXIT_USAGE)
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path: str, data: bytes, force: bool) -> None:
    if os.path.exists(path) and not force:
        raise CliError(f"destination exists: {path} (pass --force to overwrite).", EXIT_USAGE)
    with open(path, "wb") as fh:
        fh.write(data)


_WIN_DEVICE_NAMES = frozenset(
    [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    ]
)


def _safe_download_name(fname: str, fallback: str) -> str:
    """A default local filename from a SERVER-supplied Content-Disposition name (#530
    item 4). Keeps only the last path component (either separator), drops control
    characters, and refuses '', '.' and '..' (which basename() lets through and which
    crash or misbehave under --force), falling back to `fallback`."""
    name = (fname or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*').strip()
    # Windows silently drops trailing dots and spaces, and a bare device name (NUL, CON,
    # COM1 ...) with or without an extension opens the device, not a file.
    name = name.rstrip(". ")
    if name in ("", ".", "..") or name.split(".", 1)[0].upper() in _WIN_DEVICE_NAMES:
        return fallback
    return name


def _cd_filename(disp: str) -> str:
    """The filename from a Content-Disposition header, per RFC 6266 (R-MED-5).

    The /api download routes now emit the TWO-parameter form
    `attachment; filename="<ascii>"; filename*=UTF-8''<pct>` (content.
    content_disposition_attachment, session-3 CD hardening). PREFER the extended
    `filename*` ext-value (pct-decoded with its declared charset) and fall back to the
    quoted `filename=` fallback. The old `disp.split(";")` + bare `filename=` parse gave
    an extensionless file named `a` when the name held a `;`, and mangled non-ASCII."""
    import re
    from urllib.parse import unquote

    if not disp:
        return ""
    # filename*=<charset>'<lang>'<pct-encoded>  (RFC 5987 ext-value) wins.
    m = re.search(r"filename\*\s*=\s*([^;]+)", disp, re.IGNORECASE)
    if m:
        ext = m.group(1).strip()
        parts = ext.split("'", 2)
        if len(parts) == 3:
            charset, _lang, val = parts
            try:
                return unquote(val, encoding=(charset.strip() or "utf-8"))
            except (LookupError, ValueError):
                return unquote(val)
        return unquote(ext)
    # Fallback: a quoted filename= (its value may itself contain a ';', which the old
    # naive split mangled), then a bare filename=.
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', disp, re.IGNORECASE)
    if m:
        return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    m = re.search(r"filename\s*=\s*([^;]+)", disp, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


# ============================================================================
# style-guide (SPEC-workflows-redesign Track B, B2)
# ============================================================================
def _sg_body_arg(args, *, default=None):
    """Resolve the body text from --body-file (wins) or --body for a remote call."""
    bf = getattr(args, "body_file", None)
    if bf:
        try:
            with open(bf, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise CliError(f"cannot read {bf}: {exc}", EXIT_USAGE) from exc
    b = getattr(args, "body", None)
    return b if b is not None else default


# ============================================================================
# TM management module (browse / usage / lifecycle)
# ============================================================================
def _tm_query_params(args, include_query=True):
    """Assemble the shared TM filter query params for the /api/tm calls (drop Nones
    so the server sees only the filters the operator actually set)."""

    def _ctype(raw):
        raw = (raw or "").strip().lower()
        if not raw:
            return None
        if raw in ("general", "none", "untyped"):
            return "__none__"
        return raw if raw.isdigit() else None

    params = {
        # Customer-first browse: pass --customer through to /api/tm (same scoping).
        "customer": getattr(args, "tm_customer", None),
        "source_locale": getattr(args, "f_source", None),
        "target_locale": getattr(args, "f_target", None),
        "project_id": getattr(args, "f_project", None),
        "content_type_id": _ctype(getattr(args, "f_content_type", None)),
        "created_before": getattr(args, "f_created_before", None),
        "created_after": getattr(args, "f_created_after", None),
        "min_use": getattr(args, "f_min_use", None),
        "max_use": getattr(args, "f_max_use", None),
    }
    if getattr(args, "f_unused", False):
        params["unused"] = "1"
    if include_query and getattr(args, "f_query", None):
        params["q"] = args.f_query
    return {k: v for k, v in params.items() if v is not None}


def _tm_row(e: dict):
    ct = e.get("content_type_id")
    return (
        e["id"],
        (e["source_text"] or "")[:40],
        (e["target_text"] or "")[:40],
        f"{e['source_locale']}->{e['target_locale']}",
        ct if ct is not None else "general",
        e["use_count"],
        e["last_used_at"] or "-",
    )


# ============================================================================
# glossary module (DATA-SAFETY chip, QA-campaign 2026-07-28/29): the dedicated,
# first-class glossaries (customer-owned, approval lifecycle). Every verb below
# mirrors an existing /api/glossaries... route one-for-one -- these are the thin
# HTTP twins of cmd_project.py's `_gmod_*` in-process arms (which previously went
# straight to a local _session() with NO remote dispatch, so `--remote` was
# silently IGNORED and the command ran -- and reported success -- against the
# local DB. See the parser-tree guard test in tests/verify_cli_remote_dispatch_guard.py).
# ============================================================================
def _gmod_row(d: dict) -> tuple:
    return (
        d["id"],
        d["name"],
        d["status"],
        d.get("customer_id") or "-",
        ", ".join(f"{p['source_locale']}->{p['target_locale']}" for p in d.get("pairs") or [])
        or "-",
        d.get("entry_count", 0),
    )


def _parse_substitution_detail(detail: str) -> list:
    """Invert export.substitution_detail into the per-unit list the in-process
    `doc export --json` emits, so the --remote envelope carries the SAME `substituted`
    key (R-MED-6). Format: `substituted=N kind=K unit=U index=i,j ...` (one group per
    (kind,unit)). Each group's index list is capped at 20 server-side, so the count
    stays authoritative; this reconstructs the enumerable entries."""
    import re

    out: list = []
    for kind, unit, idxs in re.findall(r"kind=(\S+)\s+unit=(\S+)\s+index=(\S+)", detail or ""):
        for raw in idxs.split(","):
            raw = raw.strip()
            if not raw:
                continue
            idx = int(raw) if raw.lstrip("-").isdigit() else raw
            out.append({"unit": unit, "index": idx, "kind": kind})
    return out


# ============================================================================
# doc paragraphs / revert-paragraph / set-paragraph -- the #42 FULL_TEXT recovery
# verbs. Thin --remote mirrors of the /api routes; the local arm calls the service
# seam directly. Every refusal is classified by the SEAM and rendered by the
# transport; `_ascii_error` strips the `⟦⟧` brackets from a refusal message so repo
# law (ASCII-safe CLI output) holds without mangling the token into a backslash escape.
# ============================================================================
def _ascii_error(fn, *a, **kw):
    """Run a RemoteClient call, re-raising any CliError with the `⟦⟧` brackets stripped.

    The server's refusal messages quote real tokens; `_write_ascii_safe` keeps the CLI
    from crashing on a cp1252 console but renders them as backslash escapes, unreadable
    exactly when the operator most needs to read them. ONE formatter (`ascii_reason`),
    the same one `cmd_doc` uses locally."""
    from app.kernel.quality import ascii_reason

    try:
        return fn(*a, **kw)
    except CliError as exc:
        raise CliError(ascii_reason(str(exc)), exc.code) from None


# ============================================================================
# routing rules — remote arm of the `routing rule ...` group (cmd_routing.py, the
# feature-gated top-level twin, ffadmin Wave 4). The server answers the flag: when
# routing_enabled=0 on the host, every /api/routing/rules route 404s and the
# RemoteClient maps it to a clean "server: not found" CliError. `admin routing rule show`
# reuses routing_rule_show below (shared serializer).
# ============================================================================
def _routing_rule_line(emit, d, verb):
    """The shared human line + JSON echo for a create/toggle result (mirrors the local
    arm so `routing rule add` prints byte-identically local vs --remote)."""
    emit.result(
        f"{verb} rule #{d['id']} (prio {d['priority']}, "
        f"ext={d.get('match_ext') or '*'}, dept={d.get('department') or '*'}, "
        f"provider={d.get('provider') or '-'}).",
        {"ok": True, **d},
    )
    return 0


def report_kinds(args, emit) -> int:
    # `report kinds` is a static catalog; the /api has no dedicated route for it, so
    # serve the SAME catalog the service exposes (no server round-trip needed).
    from app.kernel import reports as reports_service

    catalog = reports_service.kinds_catalog()
    if emit.as_json:
        emit.json({"ok": True, "kinds": [{"kind": k, "columns": cols} for k, cols in catalog]})
    else:
        emit.table([(k, ", ".join(cols)) for k, cols in catalog], headers=("kind", "columns"))
    return 0


_POLL_INTERVAL_S = 2.0


_POLL_CEILING_S = 1800.0


_RUN_TERMINAL = {"completed", "failed", "cancelled", "rejected", "paused"}


# Launch-and-poll (AUDIT-core-qa-part1 Q2). The /api run + run-batch routes now LAUNCH
# the work ASYNC (they no longer execute the whole workflow inline on the server's event
# loop -- one long run used to DoS the whole instance, and a remote client that timed out
# at ~60s ABANDONED a run that kept executing unobserved). So the remote arm POSTs to get
# the run/batch id, then polls the read route to a terminal state and prints the SAME
# human line the in-process arm prints. On the ceiling it reports "still running, poll
# with <verb>" -- never abandons silently.
# 30 min: generous for real MT; each poll is a quick GET, not a
#                            held-open connection, so a long poll never blocks the server.
def _poll_run(c, run_id, interval=None, ceiling=None):
    """Poll GET /api/runs/{id} until the run is terminal (completed/failed/cancelled/
    rejected) or PAUSED (needs a human -- terminal for polling), or the ceiling is hit.
    Returns (body, timed_out). ?visits=1 so a completed body carries the executed-step
    count. interval/ceiling default to the module constants, read at call time."""
    interval = _POLL_INTERVAL_S if interval is None else interval
    deadline = time.time() + (_POLL_CEILING_S if ceiling is None else ceiling)
    while True:
        body = c.get(f"/api/runs/{run_id}", params={"visits": "1"})
        if str(body.get("status") or "").lower() in _RUN_TERMINAL:
            return body, False
        if time.time() >= deadline:
            return body, True
        time.sleep(interval)


def _add_step_body(args) -> dict:
    """The `workflow add-step` flag surface as the POST /api/workflows/{id}/steps body.
    Mirrors _wf_add_step's kwargs 1:1 so the route validates exactly what the in-process
    arm would."""
    body = {
        "name": args.name,
        "step_type": args.step_type,
        "provider": args.provider,
        "model": args.model,
        "fallback_provider": args.fallback_provider,
        "fallback_model": args.fallback_model,
        "prompt_text": args.prompt_text,
        "chunk_policy": args.chunk_policy,
        "redact_pii": bool(getattr(args, "redact_pii", False)),
        "adaptive_feedback": bool(getattr(args, "adaptive_feedback", False)),
        "adaptive_feedback_window": getattr(args, "adaptive_feedback_window", 5),
        "doc_context_prepass": bool(getattr(args, "doc_context_prepass", False)),
        "qe_gate": bool(getattr(args, "qe_gate", False)),
        "qe_threshold": getattr(args, "qe_threshold", 70),
        "qe_provider": getattr(args, "qe_provider", ""),
        # QE auto-fix (Train B): mirrors _wf_add_step (the route role-checks + clamps).
        "qe_autofix": bool(getattr(args, "qe_autofix", False)),
        "qe_autofix_provider": getattr(args, "qe_autofix_provider", ""),
        "qe_autofix_max_rounds": getattr(args, "qe_autofix_max_rounds", 3),
        "qe_autofix_cross_check": bool(getattr(args, "qe_autofix_cross_check", False)),
        "tm_examples": bool(getattr(args, "tm_examples", True)),
        "tm_examples_window": getattr(args, "tm_examples_window", 3),
        "tm_protect": getattr(args, "tm_protect", "auto"),
        # run-190 Item D / M5: the over-translation guard opt-out on the wire (add-step arm).
        "over_translation_guard": getattr(args, "over_translation_guard", "auto"),
        "memory_mode": getattr(args, "memory_mode", "auto"),
        # Batch 15 T1 (SPEC-tm-match-step-config §8.D): the six tm_match config overrides
        # on the wire (mirrors _wf_add_step so --remote validates exactly what the
        # in-process arm would). Defaults mean inherit -> seed on a new tm_match step.
        "tm_fill_threshold": getattr(args, "tm_fill_threshold", None),
        "tm_fuzzy_lock_mode": getattr(args, "tm_fuzzy_lock_mode", None),
        "tm_lock_ice": getattr(args, "tm_lock_ice", "auto"),
        "tm_hundreds_enter_lqa": getattr(args, "tm_hundreds_enter_lqa", "auto"),
        "tm_context_floor": getattr(args, "tm_context_floor", None),
        "tm_pool_selection": getattr(args, "tm_pool_selection", "") or None,
        "run_condition": getattr(args, "run_condition", "") or None,
        "key": getattr(args, "key", "") or None,
        "branch": getattr(args, "branch", []) or None,
        "max_visits": getattr(args, "max_visits", None),
        "parallel_group": getattr(args, "parallel_group", "") or None,
    }
    # feat/lqa-configurable-threshold: lqa_threshold rides here (only when the operator
    # passed --lqa-threshold) so an unset add sends no key -> the step keeps the built-in
    # default 90. prompt_template_id/glossary_id/vendor fields share the same optional rule.
    for f in (
        "prompt_template_id",
        "glossary_id",
        "vendor_id",
        "external_wait_hours",
        "lqa_threshold",
    ):
        v = getattr(args, f, None)
        if v is not None:
            body[f] = v
    return body
