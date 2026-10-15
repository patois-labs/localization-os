"""`prompt-tap` capture controls and root parser registration.

This module retains on/off/status/show/export/clear and the local content gate.
Action/run inspection lives in prompt_tap_verify; shared helpers live in
prompt_tap_common. The command-group contract follows.

The CLI parity surface for the /prompt-tap web page. Both surfaces call the SAME
service seam (app.services.prompt_tap), so a toggle / clear / read is identical
either way.

  * `on` / `off`   — flip capturing (Setting `prompt_tap_enabled`), audited like a
    Setup-AI setting change.
  * `status`       — report on/off + the stored record count, split into legacy/
    clearable vs run-capture (what `clear` drops vs what it leaves).
  * `show [--tail N]` — print captured records newest-first (default 20). `--json`
    (the global flag) emits the full records; the human view is a compact table.
  * `export`       — write captures to a FILE (or stdout) for analysis outside the
    tool: the same filters as `show`, through the same `prompt_tap.export_bundle`
    seam the web download uses, so both surfaces hand over identical bytes.
  * `runs` / `run <id>` (ROADMAP #6) — the RUN-first lens: `runs` lists translation runs
    newest-first (one row per run, grouped from each call's own run id in tap_meta), and
    `run <id>` drills into ONE run's calls grouped by step then unit. Both are BODIES-
    WITHHELD (lengths + sha256 + a 24-char head only for a body over 48 chars), so they
    work over `--remote` (GET
    /api/prompt-tap/runs[...]); `run <id> --bodies` adds the full payloads and is HOST-ONLY.
  * `clear`        — clear the LEGACY global capture file only. Per-action run-capture
    files and their envelopes are NOT cleared here (proposal section 8: run capture has
    no one-click clear; it falls only to the guarded Wave C purge).

REMOTE POSTURE (documented): LOCAL ARM ONLY (require_local) for everything that touches
raw call BODIES - `show`, `export`, `clear`, the toggles `on`/`off`, and `show-action
--calls` - matching the closest sibling verbs (`config` set/get/list and `providers key`).
The capture file lives in the SERVER's DATA_DIR and its bodies carry customer content, so
they stay host-only: a remote operator inspects them through the web viewer.

EXCEPTION (run-190 Item C, a DELIBERATE reversal of the former "no --remote arm at all"):
`actions --summary` and `show-action --summary` DO work over `--remote`, via the new GET
/api/prompt-tap/actions and /api/prompt-tap/actions/{id}/summary routes. Those return a
BODIES-WITHHELD summary ONLY - counts, statuses, error classes, finish reasons, spend,
timings, and the per-call failure shape - never a prompt or response body, so the egress
posture (raw bodies host-only) is preserved. The summary is org-scoped by the caller's
token server-side.

MULTI-ORG (`_tap_gate`): EVERY verb that touches captured content — `show`, `export`,
`clear` — and the capture switches `on`/`off` run under `local_op` (= require_local +
the SaaS in-process operator gate). A capture carries NO org attribution, so `--org`
cannot scope any of them and is refused; `--operator` is the sanctioned unscoped path.
Gating `export` ALONE was useless: `show --json` prints the same full records from the
same trust level (and is not even audited), and `clear` destroys every tenant's
captures — re-review found exactly that hole one flag away. `status` is deliberately
outside the gate: it reveals a count, not content. Strict no-op single-tenant.

ASCII-safe output throughout.

Transitional compatibility shim for one release (added 2026-09-14, remove after the
next tagged release): re-exported names are
    _strip_record_keys,
    _tap_gate,
    _RUNS_HEADERS,
    _SUMMARY_HEADERS,
    _action_flags,
    _actions,
    _emit_runs_page,
    _norm_run_date,
    _register_actions,
    _register_run,
    _register_runs,
    _register_show_action,
    _remote_actions,
    _remote_run,
    _remote_runs,
    _remote_show_action,
    _render_action_summary,
    _render_run_detail,
    _run,
    _runs,
    _runs_rows,
    _show_action,
    _summary_action_rows,
    register_prompt_tap_verify_commands.

Documented in: docs/architecture/connectors.md
"""

import json as _json
import sys

from app import branding

from ._common import (
    EXIT_USAGE,
    CliError,
    _write_ascii_safe,
    remote_client,
    require_local,
)
from .prompt_tap_common import (
    _strip_record_keys,
    _tap_gate,
)
from .prompt_tap_verify import (
    _RUNS_HEADERS,
    _SUMMARY_HEADERS,
    _action_flags,
    _actions,
    _emit_runs_page,
    _norm_run_date,
    _register_actions,
    _register_run,
    _register_runs,
    _register_show_action,
    _remote_actions,
    _remote_run,
    _remote_runs,
    _remote_show_action,
    _render_action_summary,
    _render_run_detail,
    _run,
    _runs,
    _runs_rows,
    _show_action,
    _summary_action_rows,
    register_prompt_tap_verify_commands,
)


# --- registration ------------------------------------------------------------
def _register_on(sub) -> None:
    on = sub.add_parser("on", help="Turn request capturing ON (opt-in; off by default).")
    on.set_defaults(func=_on)


def _register_off(sub) -> None:
    off = sub.add_parser(
        "off",
        help="Stop NEW raw payload capture; envelopes continue; existing raw files retained, "
        "age-out FROZEN while off.",
    )
    off.set_defaults(func=_off)


def _register_status(sub) -> None:
    st = sub.add_parser(
        "status",
        help="Report whether capturing is on + how many "
        "records are stored (legacy/clearable vs run-capture).",
    )
    st.set_defaults(func=_status)


def _register_show(sub) -> None:
    sh = sub.add_parser("show", help="Print captured records, newest first.")
    sh.add_argument(
        "--tail", type=int, default=20, metavar="N", help="show the newest N records (default 20)."
    )
    sh.add_argument(
        "--provider", default=None, metavar="REF", help="filter by provider ref or resolved kind."
    )
    sh.add_argument(
        "--workflow", type=int, default=None, metavar="ID", help="filter by correlated workflow id."
    )
    sh.add_argument(
        "--run", type=int, default=None, metavar="ID", help="filter by correlated workflow-run id."
    )
    sh.add_argument(
        "--document", type=int, default=None, metavar="ID", help="filter by correlated document id."
    )
    sh.set_defaults(func=_show)


def _register_export(sub) -> None:
    ex = sub.add_parser(
        "export",
        help="Write captured records to a file (or stdout) "
        "for analysis outside the tool (GET /prompt-tap/download parity).",
    )
    ex.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="write to FILE (default: stdout, so it can be redirected).",
    )
    ex.add_argument(
        "--format",
        dest="fmt",
        default="jsonl",
        choices=("jsonl", "json"),
        help="jsonl = one record per line, the capture file's own format "
        "(default); json = one document with a manifest head.",
    )
    ex.add_argument(
        "--tail",
        type=int,
        default=None,
        metavar="N",
        help="export the newest N matching records (must be positive; "
        "default: all, capped at the stored maximum).",
    )
    ex.add_argument(
        "--record",
        default=None,
        metavar="KEY",
        help="export ONE record by its 16-hex key (shown on the web "
        "viewer's Download link); implies pretty-printed json.",
    )
    ex.add_argument(
        "--provider", default=None, metavar="REF", help="filter by provider ref or resolved kind."
    )
    ex.add_argument(
        "--workflow", type=int, default=None, metavar="ID", help="filter by correlated workflow id."
    )
    ex.add_argument(
        "--run", type=int, default=None, metavar="ID", help="filter by correlated workflow-run id."
    )
    ex.add_argument(
        "--document", type=int, default=None, metavar="ID", help="filter by correlated document id."
    )
    ex.set_defaults(func=_export)


def _register_clear(sub) -> None:
    cl = sub.add_parser(
        "clear",
        help="Clear LEGACY capture (the raw call log). Does "
        "NOT clear per-action run-capture files or envelopes.",
    )
    cl.set_defaults(func=_clear)


def register(subparsers) -> None:
    pt = subparsers.add_parser(
        "prompt-tap",
        help=f"Control + read the {branding.ledger_name()} request capture (the exact "
        "payload sent per completion call). CLI parity for the /prompt-tap page.",
    )
    sub = pt.add_subparsers(dest="prompt_tap_cmd", metavar="<subcommand>")
    sub.required = True

    _register_on(sub)
    _register_off(sub)
    _register_status(sub)
    _register_show(sub)

    _register_actions(sub)
    _register_show_action(sub)
    _register_runs(sub)
    _register_run(sub)

    _register_export(sub)
    _register_clear(sub)


# --- verbs -------------------------------------------------------------------
def _on(args, emit) -> int:
    _tap_gate(args, emit)
    from app.kernel import prompt_tap

    prompt_tap.set_enabled(True, actor=getattr(args, "actor", "cli"))
    emit.result(
        f"{branding.ledger_name()}: ON (capturing request payloads).",
        {"ok": True, "enabled": True},
    )
    return 0


def _off(args, emit) -> int:
    _tap_gate(args, emit)
    from app.kernel import prompt_tap

    prompt_tap.set_enabled(False, actor=getattr(args, "actor", "cli"))
    emit.result(
        f"{branding.ledger_name()}: OFF. New raw payload capture stopped; envelopes continue. "
        "Existing raw files retained; age-out FROZEN while off.",
        {"ok": True, "enabled": False},
    )
    return 0


def _retention_summary() -> dict:
    """Raw-capture size, the caps that bound it and the last prune (#616), for `status`."""
    from app.kernel import ledger_controls

    st = ledger_controls.status()
    return {
        "raw_bytes": st["raw_bytes"],
        "ttl_days": st["retention_ttl_days"],
        "max_bytes": st["retention_max_bytes"],
        "last_prune": st["last_prune"],
    }


def _status(args, emit) -> int:
    require_local(args)
    from app.kernel import prompt_tap

    on = prompt_tap.enabled()
    n = prompt_tap.count_records()
    legacy = prompt_tap.count_legacy_records()
    # `clear` drops only the LEGACY records; the per-action run-capture files are not
    # clearable here (proposal section 8). Report both so status and clear agree.
    run_cap = n - legacy
    ret = _retention_summary()
    lp = ret["last_prune"]
    days = f"{ret['ttl_days']} days" if ret["ttl_days"] else "forever"
    prune = f"{lp['at']}, {lp['deleted'] + lp['size_pruned']} files removed" if lp else "never"
    ret_line = (
        f" Raw capture {ret['raw_bytes'] / 1048576:.1f} MB (kept {days}, "
        f"cap {ret['max_bytes'] // 1048576} MB); last prune: {prune}."
    )
    emit.result(
        f"{branding.ledger_name()}: "
        + (  # keep the brand OUT of the % template so a brand containing % can't crash it
            "%s (%d record%s stored; %d legacy/clearable, %d run-capture)."  # noqa: UP031
            % ("ON" if on else "OFF", n, "" if n == 1 else "s", legacy, run_cap)
        )
        + ret_line,
        {
            "ok": True,
            "enabled": on,
            "records": n,
            "legacy_records": legacy,
            "run_capture_records": run_cap,
            "retention": ret,
        },
    )
    return 0


def _show(args, emit) -> int:
    # Same gate as `export`: --json here prints the FULL records, so leaving it
    # ungated made the export gate decorative.
    _tap_gate(args, emit)
    from app.kernel import prompt_tap

    recs = prompt_tap.read_records(
        limit=max(1, args.tail),
        provider=args.provider,
        workflow_id=args.workflow,
        workflow_run_id=args.run,
        document_id=args.document,
    )
    if emit.as_json:
        emit.json({"ok": True, "count": len(recs), "records": recs})
        return 0
    if not recs:
        emit.warn("no captures (turn it on with `prompt-tap on`, then run a translation).")
        return 0
    rows = []
    for r in recs:
        meta = r.get("meta") or {}
        corr = []
        if meta.get("workflow_run_id") is not None:
            corr.append("run{}".format(meta["workflow_run_id"]))
        if meta.get("document_id") is not None:
            corr.append("doc{}".format(meta["document_id"]))
        if meta.get("segment_index") is not None:
            corr.append("seg{}".format(meta["segment_index"]))
        rows.append(
            (
                (r.get("ts") or "")[:19],
                r.get("outcome") or "",
                (r.get("provider") or "") + (("/" + r["model"]) if r.get("model") else ""),
                " ".join(corr) or "-",
            )
        )
    emit.table(rows, headers=("ts", "outcome", "provider/model", "correlation"))
    emit.say("")
    emit.say("(use --json for the full request/response payloads)")
    return 0


def _export(args, emit) -> int:
    """Write captures out for offline analysis. Calls the SAME
    `prompt_tap.export_bundle` seam as `GET /prompt-tap/download`, so the file an
    operator gets here is byte-identical to the one the web control produces for the
    same selection.

    Output posture: `--out FILE` writes the file and reports (honoring `--json`);
    with no `--out` the export goes to STDOUT so it can be redirected, and the only
    thing on stderr is a truncation warning — never mixed into the data. Everything
    written is ASCII (the seam dumps with ensure_ascii)."""
    _tap_gate(args, emit)
    from app.kernel import audit, prompt_tap

    if args.tail is not None and args.tail <= 0:
        # `show` reads --tail 0 as "one row"; silently reading it here as "everything"
        # is the kind of trap that ships a whole-store file when 0 was meant.
        raise CliError(
            "--tail must be a positive number of records (omit it to export everything matching).",
            EXIT_USAGE,
        )
    limit = args.tail if (args.tail or 0) > 0 else prompt_tap.EXPORT_DEFAULT_LIMIT
    # .strip() to match the web arm exactly: a key pasted from the viewer link can
    # arrive padded, and the two surfaces must accept the same input.
    key = args.record.strip() if isinstance(args.record, str) else args.record
    try:
        bundle = prompt_tap.export_bundle(
            fmt=args.fmt,
            limit=limit,
            key=key,
            provider=args.provider,
            workflow_id=args.workflow,
            workflow_run_id=args.run,
            document_id=args.document,
        )
    except ValueError as exc:  # unknown --format / malformed --record key
        raise CliError(str(exc), EXIT_USAGE) from exc
    if args.record and not bundle["count"]:
        raise CliError(
            f"no capture with key {args.record} (it may have rotated out).",
            EXIT_USAGE,  # noqa: RUF100, UP032
        )
    if not bundle["count"]:
        emit.warn("no captures match (the export is empty).")

    if args.out:
        n_chars = 0
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                for chunk in bundle["lines"]:
                    fh.write(chunk)
                    n_chars += len(chunk)
        except OSError as exc:
            # A typo'd path is an operator error, not a crash — and nothing is audited
            # for a download that never happened.
            raise CliError(f"could not write {args.out}: {exc}") from exc
        _audit_export(args, emit, audit, bundle, args.out)
        emit.result(
            f"{branding.ledger_name()}: "
            + (  # brand kept OUT of the % template (a brand containing % must not crash)
                "wrote %d record%s (%s, %d bytes) to %s.%s"  # noqa: UP031
            )
            % (
                bundle["count"],
                "" if bundle["count"] == 1 else "s",
                bundle["fmt"],
                n_chars,
                args.out,
                ""
                if not bundle["truncated"]
                else " TRUNCATED: %d of %d matching -- raise --tail for more."  # noqa: UP031
                % (bundle["count"], bundle["total"]),
            ),
            dict(_export_payload(bundle, args.out), bytes=n_chars),
        )
    else:
        # STDOUT arm: data only, so `... > captures.jsonl` yields a clean file. The
        # truncation notice goes to stderr, where it cannot pollute the data.
        #
        # Windows text-mode stdout rewrites every \n to \r\n, so the DOCUMENTED
        # redirect produced a file that differed from both --out and the web download
        # — the byte-identity claim, broken on the platform the maintainer runs. Writing
        # through the BINARY buffer sidesteps the translation without mutating global
        # stdout state (an earlier fix used reconfigure(newline=""), which never gets
        # restored and silently changes every later print in the process). The export
        # is pure ASCII by construction, so the encode cannot fail.
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            sys.stdout.flush()
        for chunk in bundle["lines"]:
            if buf is not None:
                buf.write(chunk.encode("ascii", "backslashreplace"))
            else:  # a text sink with no buffer (the suite's StringIO)
                _write_ascii_safe(sys.stdout, chunk)
        if buf is not None:
            buf.flush()
        _audit_export(args, emit, audit, bundle, "stdout")
        if bundle["truncated"]:
            emit.warn(
                "exported the newest %d of %d matching records; raise --tail to "  # noqa: UP031
                "include more." % (bundle["count"], bundle["total"])
            )
        if emit.as_json:
            # The DATA is stdout's machine output here, so the --json summary goes to
            # stderr rather than corrupting the stream it describes.
            _write_ascii_safe(sys.stderr, _json.dumps(_export_payload(bundle, None)) + "\n")
    return 0


def _export_payload(bundle, out) -> dict:
    return {
        "ok": True,
        "out": out,
        "format": bundle["fmt"],
        "count": bundle["count"],
        "total": bundle["total"],
        "truncated": bundle["truncated"],
        "suggested_filename": bundle["filename"],
    }


def _audit_export(args, emit, audit, bundle, dest) -> None:
    """Record the export AFTER it succeeded, with the SELECTION. Logging intent up
    front left a permanent audit row claiming a successful export when the write had
    actually failed; a bare count made a one-document export and a whole-install dump
    read identically. BEST-EFFORT (like set_enabled's own audit): the file is already
    written by the time we get here, so a broken audit chain must not turn a completed
    export into a non-zero exit."""
    active = [f"{k}={v}" for k, v in sorted(bundle["filters"].items()) if v is not None]
    scope = "record-only" if bundle["key"] else (" ".join(active) if active else "none")
    try:
        audit.log(
            actor=getattr(args, "actor", "cli") or "cli",
            action="prompt_tap.download",
            detail="%s: %d of %d record%s -> %s%s, filters=%s"  # noqa: UP031
            % (
                bundle["fmt"],
                bundle["count"],
                bundle["total"],
                "" if bundle["total"] == 1 else "s",
                dest,
                " (record {})".format(bundle["key"]) if bundle["key"] else "",
                scope,
            ),
        )
    except Exception:
        emit.warn("the export completed but could not be audited.")


def _clear(args, emit) -> int:
    # Gated too: unscoped, this destroys EVERY tenant's captures.
    _tap_gate(args, emit)
    from app.kernel import audit, prompt_tap

    n = prompt_tap.clear()
    # Record what was ACTUALLY dropped (legacy records only). Per-action run-capture
    # files and envelopes are NOT cleared here (proposal section 8).
    audit.log(
        actor=getattr(args, "actor", "cli") or "cli",
        action="prompt_tap.clear",
        detail="cleared %d legacy records (run capture untouched)" % n,  # noqa: UP031
    )
    emit.result(
        f"{branding.ledger_name()}: "
        + (  # brand kept OUT of the % template (a brand containing % must not crash)
            "cleared %d legacy record%s (run-capture files are not cleared here)."  # noqa: UP031
            % (n, "" if n == 1 else "s")
        ),
        {"ok": True, "cleared": n},
    )
    return 0


__all__ = [
    "_RUNS_HEADERS",
    "_SUMMARY_HEADERS",
    "_action_flags",
    "_actions",
    "_audit_export",
    "_clear",
    "_emit_runs_page",
    "_export",
    "_export_payload",
    "_norm_run_date",
    "_off",
    "_on",
    "_register_actions",
    "_register_clear",
    "_register_export",
    "_register_off",
    "_register_on",
    "_register_run",
    "_register_runs",
    "_register_show",
    "_register_show_action",
    "_register_status",
    "_remote_actions",
    "_remote_run",
    "_remote_runs",
    "_remote_show_action",
    "_render_action_summary",
    "_render_run_detail",
    "_run",
    "_runs",
    "_runs_rows",
    "_show",
    "_show_action",
    "_status",
    "_strip_record_keys",
    "_summary_action_rows",
    "_tap_gate",
    "register",
    "register_prompt_tap_verify_commands",
    "remote_client",
]
