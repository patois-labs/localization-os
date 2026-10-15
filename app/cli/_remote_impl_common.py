"""_remote_impl: shared leaf helpers (layered pure move)

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _read_bytes, _write_bytes, _cd_filename,
_sg_body_arg, _tm_query_params, _tm_row, _TERM_META_FLAGS, _term_meta_from_args, _gmod_row,
_parse_substitution_detail, _ascii_error, _routing_rule_line, report_kinds,
_POLL_INTERVAL_S, _POLL_CEILING_S, _RUN_TERMINAL, _poll_run, _add_step_body from
app.cli._remote_impl_common_leaf.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import os
import time

from app.cli._common import (
    EXIT_USAGE,
    CliError,
    remote_client,
)
from app.cli._remote_impl_common_leaf import (
    _POLL_CEILING_S,
    _POLL_INTERVAL_S,
    _RUN_TERMINAL,
    _TERM_META_FLAGS,
    _add_step_body,
    _ascii_error,
    _cd_filename,
    _gmod_row,
    _parse_substitution_detail,
    _poll_run,
    _read_bytes,
    _routing_rule_line,
    _safe_download_name,
    _sg_body_arg,
    _term_meta_from_args,
    _tm_query_params,
    _tm_row,
    _write_bytes,
    report_kinds,
)

__all__ = [
    "_POLL_CEILING_S",
    "_POLL_INTERVAL_S",
    "_RUN_TERMINAL",
    "_TERM_META_FLAGS",
    "_add_step_body",
    "_ascii_error",
    "_cd_filename",
    "_gmod_row",
    "_parse_substitution_detail",
    "_poll_run",
    "_read_bytes",
    "_routing_rule_line",
    "_safe_download_name",
    "_sg_body_arg",
    "_term_meta_from_args",
    "_tm_query_params",
    "_tm_row",
    "_write_bytes",
    "os",
    "report_kinds",
    "time",
]


def _client(args):
    return remote_client(args)


# ============================================================================
# account (self-service). The token IS the user, so these act on the caller's OWN
# account; a non-empty --user was already refused by cmd_account._refuse_remote_user
# (exit 2) before dispatching here (mirrors the web self-service).
# ============================================================================
def account_show(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/account")
    if emit.as_json:
        emit.json({"ok": True, **{k: v for k, v in body.items() if k != "ok"}})
    else:
        emit.say(f"Account: {body.get('email')}  (role={body.get('role')})")
        emit.say(f"  name: {body.get('name') or '(none)'}")
        emit.say(f"  ui locale: {body.get('ui_locale') or '(default)'}")
        emit.say(f"  appearance: {body.get('ui_theme') or 'system'}")
        if isinstance(body.get("scopes"), list):
            # #751: the calling key's own grants (never another key's).
            emit.say(f"  key scopes: {' '.join(body['scopes']) or '(none)'}")
        ob = body.get("onboarding") or {}
        variant_note = f", variant={ob.get('variant')}" if ob.get("variant") else ""
        emit.say(
            f"  onboarding dismissed_at: {ob.get('dismissed_at') or '(none)'}"
            f"  (eligible={'yes' if ob.get('eligible') else 'no'}{variant_note})"
        )
    return 0


def account_set_theme(args, emit) -> int:
    # A bad value is rejected server-side (400 -> CliError exit 1), same as local.
    c = _client(args)
    theme = (args.theme or "").strip()
    body = c.post("/api/account/theme", json={"theme": theme})
    emit.result(f"Set your appearance -> {body.get('ui_theme', theme)}.", {"ok": True, **body})
    return 0


def account_reset_onboarding(args, emit) -> int:
    c = _client(args)
    body = c.post("/api/account/onboarding-reset", json={})
    emit.result("Reset your onboarding (tour re-armed).", {"ok": True, **body})
    return 0


# ============================================================================
# customer
# ============================================================================
def customer_create(args, emit) -> int:
    c = _client(args)
    body = c.post("/api/customers", json={"name": args.name})
    emit.result(
        f"Created customer #{body['id']}: {body['name']}",
        {"ok": True, "id": body["id"], "name": body["name"]},
    )
    return 0


def customer_pairs_import(args, emit) -> int:
    c = _client(args)
    data = _read_bytes(args.csv)
    body = c.post(
        f"/api/customers/{args.customer_id}/pairs/import",
        json={"csv_b64": base64.b64encode(data).decode()},
    )
    emit.result(
        f"Imported pairs on customer #{args.customer_id}: "
        f"{len(body['added'])} added, {len(body['skipped'])} already present "
        "(skipped).",
        {
            "ok": True,
            "customer_id": args.customer_id,
            "added": body["added"],
            "skipped": body["skipped"],
            "total_rows": body.get("total_rows", 0),
        },
    )
    return 0


def customer_list(args, emit) -> int:
    from .cmd_project import _note_truncated, _resolve_window

    c = _client(args)
    # SPEC-customer-archive: --archived (archived-only) / --all (both) mirror the local
    # list. M4: the archive mode is a SERVER-SIDE filter (?archived=only|all|active), so
    # the same result set comes back as local -- no client-side narrowing that would
    # diverge past the 1000 cap (e.g. archived customers beyond row 1000).
    show_archived = bool(getattr(args, "archived", False))
    show_all = bool(getattr(args, "all_customers", False))
    limit, offset = _resolve_window(args)
    params = {"limit": limit, "offset": offset}
    if show_all:
        params["archived"] = "all"
    elif show_archived:
        params["archived"] = "only"
    body = c.get("/api/customers", params=params)
    custs = body.get("customers", [])
    truncated = bool(body.get("truncated"))
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "customers": custs,
                "truncated": truncated,
                "limit": limit,
                "offset": offset,
            }
        )
    elif show_all:
        emit.table(
            [(x["id"], x["name"], "archived" if x.get("archived") else "active") for x in custs],
            headers=("id", "name", "status"),
        )
        _note_truncated(emit, truncated, offset, limit)
    else:
        emit.table([(x["id"], x["name"]) for x in custs], headers=("id", "name"))
        _note_truncated(emit, truncated, offset, limit)
    return 0


# ============================================================================
# project
# ============================================================================
def _parse_fields(pairs) -> dict | None:
    from .cmd_field import parse_field_args

    return parse_field_args(pairs) or None


def project_create(args, emit) -> int:
    c = _client(args)
    # Phase 2: forward the per-pair mode + mapping. --pair-provider implies
    # PER_PAIR; --per-pair alone opts into the mode with no overrides.
    pair_flags = list(getattr(args, "pair_provider", []) or [])
    per_pair = getattr(args, "per_pair", False) or bool(pair_flags)
    body = c.post(
        "/api/projects",
        json={
            "name": args.name,
            "source_locale": args.source,
            "targets": args.targets,
            "customer_id": args.customer_id,
            "provider": args.provider,
            "provider_mode": "per_pair" if per_pair else "uniform",
            "pair_providers": pair_flags,
            # Feature A: optional due date (blank = none).
            "due_date": getattr(args, "due", ""),
            # SPEC-content-types: optional content type (blank = General / customer default).
            "content_type_id": getattr(args, "content_type", ""),
            # SPEC-key-based-projects 2.1: blank = "document" server-side.
            "project_type": getattr(args, "project_type", ""),
            # Connected projects: --connected = intake "connected" (blank = upload).
            "intake": "connected" if getattr(args, "connected", False) else "",
            # #608: --all-targets silences the active-pairs advisory, same as in-process.
            "all_targets": bool(getattr(args, "all_targets", False)),
            # Custom project fields: --field KEY=VALUE (validated server-side, all or nothing).
            "custom_fields": _parse_fields(getattr(args, "field", [])),
        },
    )
    # --seg-profile: forward it (R-HIGH-1 — was silently dropped, so a remote project
    # got the inherit chain while a local one got the override). The route validates +
    # assigns the same segprof.assign_to_project seam. Unlike the in-process path (which
    # validates BEFORE the create commits), here the project row already exists when the
    # assign runs, so a bad/foreign profile is a clean 400 (-> CliError) that leaves the
    # created project on the inherit chain rather than rolling the create back.
    seg_profile = (getattr(args, "seg_profile", "") or "").strip()
    if seg_profile:
        c.post(
            "/api/segprofiles/assign", json={"project_id": body["id"], "profile_id": seg_profile}
        )
    # R-LOW: the in-process arm warns when a created target falls outside the customer's
    # configured active pairs (the maintainer's P2 confinement advisory). The /api create does not
    # evaluate it and the remote arm has no local view of the pairs, so say so rather than
    # letting the silence read as "all pairs are fine". To stderr, so --json is unaffected.
    if "targets_outside_pairs" in body:
        # #608: the server evaluated it (same rule and wording as the in-process arm).
        for _t in body["targets_outside_pairs"] or []:
            emit.warn(
                f"target {_t} is not in customer #{args.customer_id}'s "
                "active pairs (added anyway; configure pairs with "
                "`customer pairs add`, or pass --all-targets to silence)."
            )
    elif (getattr(args, "targets", "") or "").strip() and args.customer_id:
        # An older server that does not report the advisory: say so rather than let the
        # silence read as "all pairs are fine".
        emit.warn(
            "active-pairs confinement is not evaluated by this server: a target "
            "outside the customer's configured pairs is created without the "
            "in-process advisory (configure pairs with `customer pairs ...`)."
        )
    emit.result(
        f"Created project #{body['id']}: {body['name']} "
        f"({body['source_locale']} -> {', '.join(body['target_locales']) or 'no targets'}"
        + (", keys" if body.get("project_type") == "key_based" else "")
        + (", connected" if body.get("intake") == "connected" else "")
        + (f", seg-profile #{seg_profile}" if seg_profile else "")
        + ")",
        {"ok": True, **body, **({"segmentation_profile_id": seg_profile} if seg_profile else {})},
    )
    return 0


def project_list(args, emit) -> int:
    from app.models import ProjectStatus

    from .cmd_project import _note_truncated, _resolve_window

    c = _client(args)
    # --archived (active|all|only): forward as ?archived= (the route honours only/all;
    # anything else, incl. "active", is active-only). Parity with _project_list.
    arch = (getattr(args, "archived", "active") or "active").strip().lower()
    params = {}
    if arch in ("all", "only"):
        params["archived"] = arch
    limit, offset = _resolve_window(args)
    params["limit"] = limit
    params["offset"] = offset
    # M4: --status is a SERVER-SIDE filter now (?status=), so the returned page is the
    # SAME result set as the local arm -- no client-side narrowing that would diverge
    # past the cap. Validate locally too so a bad value exits 2 without a round-trip.
    status = (getattr(args, "status", "") or "").strip().lower()
    if status not in ("", "default", "all") and status not in ProjectStatus.ALL:
        raise CliError(
            f"unknown status {status!r} (choose one of: "
            f"{', '.join(ProjectStatus.ALL)}, all, default).",
            EXIT_USAGE,
        )
    if status:
        params["status"] = status
    connected = bool(getattr(args, "connected", False))
    if connected:
        params["intake"] = "connected"
    from .cmd_field import remote_list_params

    cf_cols = remote_list_params(args, params)
    body = c.get("/api/projects", params=params)
    projs = body.get("projects", [])
    truncated = bool(body.get("truncated"))
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "projects": projs,
                "truncated": truncated,
                "limit": limit,
                "offset": offset,
                "hidden": body.get("hidden", {}),
            }
        )
    else:
        from app.kernel import custom_fields as _cfs

        emit.table(
            [
                (
                    p["id"],
                    p["name"],
                    p["source_locale"],
                    p.get("customer", ""),
                    p.get("status", ""),
                    *((((p.get("source") or {}).get("label", ""),)) if connected else ()),
                    *(_cfs.flat(None, (p.get("custom_fields") or {}).get(k)) for k in cf_cols),
                )
                for p in projs
            ],
            headers=(
                "id",
                "name",
                "source",
                "customer",
                "status",
                *(("connected source",) if connected else ()),
                *cf_cols,
            ),
        )
        _note_truncated(emit, truncated, offset, limit)
        from .cmd_project_project import _note_hidden

        _note_hidden(emit, body.get("hidden"))
    return 0


def project_show(args, emit) -> int:
    c = _client(args)
    info = c.get(f"/api/projects/{args.project_id}")
    if emit.as_json:
        emit.json(info)
    else:
        emit.say(f"Project #{info['id']}: {info['name']}")
        emit.say(
            f"  source: {info['source_locale']}   provider: {info['default_provider']}"
            + (f"   customer: {info['customer']}" if info.get("customer") else "")
        )
        emit.say(f"  target locales: {', '.join(info['target_locales']) or '(none)'}")
        if info.get("stopped_locales"):
            emit.say(f"  stopped locales: {', '.join(info['stopped_locales'])}")
        # Phase 2: per-pair provider mode + overrides.
        if info.get("provider_mode"):
            emit.say(f"  provider mode: {info['provider_mode']}")
        for pr in info.get("pair_providers", []) or []:
            emit.say(f"    {pr['source_locale']} -> {pr['target_locale']}: {pr['provider']}")
        # Issue #19: mirror the local `project show` human output exactly (due
        # date, content type, segmentation profile) so --remote never shows less
        # than the in-process path.
        emit.say(f"  due date: {info.get('due_date') or '(none)'}")
        from app.kernel import custom_fields as _cfs

        for _k, _v in (info.get("custom_fields") or {}).items():
            emit.say(f"  field {_k}: {_cfs.flat(None, _v)}")
        emit.say(
            "  content type: "
            + (
                f"#{info['content_type_id']} '{info.get('content_type_name')}'"
                if info.get("content_type_id")
                else "(general)"
            )
        )
        seg_id = info.get("segmentation_profile_id")
        seg_rid = info.get("segmentation_profile_resolved_id")
        seg_rname = info.get("segmentation_profile_resolved_name")
        if seg_id:
            emit.say(
                f"  segmentation: override #{seg_id} "
                + (
                    f"('{seg_rname}')"
                    if seg_rname
                    else "(profile deleted; falls through to inherit chain)"
                )
            )
        else:
            emit.say(
                "  segmentation: inherit chain -> resolved to "
                + (f"#{seg_rid} '{seg_rname}'" if seg_rid else "the built-in default")
            )
        emit.say(
            f"  TM: {info['tm_count']} segment / {info['ft_tm_count']} full-text"
            f"   glossary: {info['glossary_count']}"
        )
        docs = info.get("documents", [])
        emit.say(f"  documents ({len(docs)}):")
        emit.table(
            [(d["id"], d["filename"], d["fmt"], d["mode"], d["status"]) for d in docs],
            headers=("id", "filename", "fmt", "mode", "status"),
        )
    return 0


def project_leverage(args, emit) -> int:
    """CV-1: GET /api/projects/{id}/leverage — the 7-band word-based TM leverage. Same
    human/JSON shape as the local `project leverage` arm."""
    c = _client(args)
    info = c.get(f"/api/projects/{args.project_id}/leverage")
    if emit.as_json:
        emit.json(info)
    else:
        emit.say(f"Project #{info['project_id']} - 7-band word leverage")
        emit.table(
            [
                (r["label"], r["words"], r["segments"], "covered" if r["covered"] else "billable")
                for r in info.get("bands", [])
            ],
            headers=("band", "words", "segments", "scope"),
        )
        emit.say(
            f"  total words: {info.get('total_words', 0)}   "
            f"covered: {info.get('covered_words', 0)}   "
            f"billable (post-edit): {info.get('billable_words', 0)}"
        )
        if info.get("note"):
            emit.say(f"  note: {info['note']}")
    return 0


def project_set_providers(args, emit) -> int:
    c = _client(args)
    pair_flags = list(getattr(args, "pair_provider", []) or [])
    per_pair = getattr(args, "per_pair", False) or bool(pair_flags)
    body = c.post(
        f"/api/projects/{args.project_id}/pair-providers",
        json={"provider_mode": "per_pair" if per_pair else "uniform", "pair_providers": pair_flags},
    )
    pairs = body.get("pair_providers", []) or []
    emit.result(
        f"Project #{args.project_id} provider mode: {body.get('provider_mode')} "
        f"({len(pairs)} pair override(s)).",
        {"ok": True, **body},
    )
    return 0


def project_add_locale(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/projects/{args.project_id}/add-locale", json={"locale": args.locale})
    emit.result(
        f"{'Added' if body['added'] else 'Already present:'} {args.locale} "
        f"on project #{args.project_id}.",
        {"ok": True, **body},
    )
    return 0


def _edit_step_body(args) -> dict:
    """The `workflow edit-step` PARTIAL flag surface as the POST .../edit body: ONLY the
    flags the operator actually passed (None sentinel = leave unchanged) go on the wire,
    so the route's partial contract is honoured across the transport, exactly as
    _wf_edit_step assembles its update_step kwargs."""
    body: dict = {}
    # Plain fields: value present (not None) = a real edit.
    for f in (
        "name",
        "step_type",
        "provider",
        "model",
        "fallback_provider",
        "fallback_model",
        "prompt_text",
        "chunk_policy",
        "assignee_email",
        "adaptive_feedback_window",
        "qe_threshold",
        "qe_provider",
        # QE auto-fix (Train B) on the wire (update arm).
        "qe_autofix_provider",
        "qe_autofix_max_rounds",
        "lqa_threshold",
        "tm_examples_window",
        "max_visits",
        "vendor_id",
        "external_wait_hours",
        "tm_protect",
        # run-190 Item D / M5: the over-translation guard opt-out on the wire (update arm).
        "over_translation_guard",
        "memory_mode",
        # Batch 15 T1 (SPEC-tm-match-step-config §8.D): the six tm_match config overrides,
        # partial (present-value / present-"auto"/"" clears / absent-unchanged).
        "tm_fill_threshold",
        "tm_fuzzy_lock_mode",
        "tm_lock_ice",
        "tm_hundreds_enter_lqa",
        "tm_context_floor",
        "tm_pool_selection",
        "glossary_id",
        "redact_pii",
        "adaptive_feedback",
        "doc_context_prepass",
        "qe_gate",
        "qe_autofix",
        "qe_autofix_cross_check",
        "tm_examples",
    ):
        v = getattr(args, f, None)
        if v is not None:
            body[f] = v
    # Clearable text fields: '' clears (send the empty string so the route clears).
    if getattr(args, "run_condition", None) is not None:
        body["run_condition"] = args.run_condition
    if getattr(args, "key", None) is not None:
        body["key"] = args.key
    if getattr(args, "prompt_template_id", None) is not None:
        body["prompt_template_id"] = args.prompt_template_id
    # branch / clear-branch (mutually exclusive at the parser).
    if getattr(args, "clear_branch", False):
        body["clear_branch"] = True
    elif getattr(args, "branch", None):
        body["branch"] = args.branch
    # parallel-group join / leave.
    if getattr(args, "no_parallel_group", False):
        body["no_parallel_group"] = True
    elif getattr(args, "parallel_group", None) is not None:
        body["parallel_group"] = args.parallel_group
    # deliberate pin / unpin intent.
    if getattr(args, "pin", False):
        body["pin"] = True
    if getattr(args, "unpin", False):
        body["unpin"] = True
    return body


def _body_arg(args) -> str:
    """--body-file (wins) or --body, mirroring cmd_memory._body_from_args -- the
    --remote arm reads the SAME local file the in-process arm would, since a file
    path is meaningless on the server."""
    bf = getattr(args, "body_file", "")
    if bf:
        try:
            with open(bf, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise CliError(f"cannot read {bf}: {exc}", EXIT_USAGE) from exc
    return getattr(args, "body", "") or ""


def _emit_export_lines(args, emit, lines, count, *, label: str, noun: str) -> int:
    """The shared tail of the memory / suggestions `export` verbs: write the rendered chunks
    to `--out` (or stdout, byte-exact), and report. One copy for both (#533)."""
    import sys

    if args.out:
        n_chars = 0
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                for chunk in lines:
                    fh.write(chunk)
                    n_chars += len(chunk)
        except OSError as exc:
            raise CliError(f"could not write {args.out}: {exc}") from exc
        emit.result(
            f"{label}: wrote {count} record(s) ({args.fmt}, {n_chars} bytes) to {args.out}.",
            {"ok": True, "count": count, "fmt": args.fmt, "out": args.out, "bytes": n_chars},
        )
        return 0

    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        sys.stdout.flush()
    for chunk in lines:
        if buf is not None:
            buf.write(chunk.encode("utf-8"))
        else:
            sys.stdout.write(chunk)
    if buf is not None:
        buf.flush()
    if not count:
        emit.warn(f"no {noun} match (the export is empty).")
    return 0


# ============================================================================
# keys (SPEC-key-based-projects section 7) -- the --remote twins of cmd_keys.py.
#
# Every renderer here is IMPORTED from cmd_keys rather than re-written, so the two
# arms cannot print different tables for the same data (the `glossary module show`
# meta-column divergence is the precedent: the same data rendered two ways because
# the remote arm re-implemented the formatting).
#
# The key-addressed routes take an ID, while the CLI addresses a key by its IDENTITY
# TUPLE (namespace, name, context) -- that is the whole point of a key. `_keys_find`
# does the one lookup that bridges them, and refuses a miss with the SAME message the
# local arm gives.
# ============================================================================
def _keys_lookup(c, args, name=None, include_deleted=False):
    """(key dict, source locale) for the key `args` names, or (None, source locale).

    `include_deleted=True` also finds SOFT-DELETED keys, which is what `keys restore`
    needs and what `keys add`'s duplicate pre-check must NOT use (a tombstoned key
    does not block a create -- the partial unique index is defined exactly so a name
    can be reused after a cleanup import)."""
    ns = getattr(args, "namespace", "") or ""
    ctx = getattr(args, "context", "") or ""
    want = name if name is not None else args.name
    cursor = 0
    source_locale = ""
    while True:
        params = {"q": want, "cursor": cursor, "limit": 200, "archived": 1}
        if include_deleted:
            params["include_deleted"] = 1
        body = c.get(f"/api/projects/{args.project_id}/keys", params=params)
        source_locale = body.get("source_locale") or source_locale
        rows = body.get("keys") or []
        for k in rows:
            if (
                k.get("name") == want
                and (k.get("namespace") or "") == ns
                and (k.get("context") or "") == ctx
            ):
                return k, source_locale
        if not body.get("has_more") or not rows:
            break
        cursor = body.get("next_cursor") or cursor
    return None, source_locale


# ============================================================================
# query (SPEC-wave3 §4.4) — the --remote arms hit /api/queries*, which is RBAC-enforced
# server-side by the token. They emit the SAME human/--json shape as the in-process arm
# (cmd_query), so a query verb is transport-independent.
# ============================================================================
def _q_row_line(r):
    return (
        r.get("id"),
        r.get("status"),
        r.get("project_id"),
        (r.get("subject") or "")[:40],
        r.get("assignee_email") or "-",
        r.get("post_count"),
        str(r.get("updated_at")),
    )


# --- reviewer LQA findings (SPEC-wave3 §6.5) — thin /api clients ---------------
def _lqa_row_line(f):
    return (
        f.get("id"),
        f.get("segment_id"),
        f.get("severity"),
        f.get("dimension"),
        (f.get("note") or "")[:40],
        f.get("actor") or "-",
    )


# ============================================================================
# data export (Batch 13, Train B): the /api/exports arms -- POST-start + poll,
# list, status, download. The remote arm and the in-process arm (cmd_export)
# render the BYTE-IDENTICAL artifact (LAW4, the prompt_tap parity law).
# ============================================================================
def _export_run_line(emit, d) -> int:
    ok = d.get("status") in ("ok", "partial")
    emit.result(
        "Export run {} ({}/{}): status={} ({} bytes).".format(
            d.get("run_uuid"),
            d.get("scope_kind"),
            d.get("history"),
            d.get("status"),
            d.get("artifact_bytes"),
        ),
        {"ok": ok, **{k: v for k, v in d.items() if k != "ok"}},
    )
    return 0 if ok else 1


def _remote_run_or_missing(c, run_id):
    """GET a run over /api, refusing a MISS with the SAME exit-2 "no such run: {id}" the in-process
    arm raises (round-1 M1: refusal parity on every id-addressed remote read -- a not-found must be
    a usage error, exit 2, not the generic server exit-1). A non-404 error still surfaces as the
    server's exit-1 refusal via the client's _decode."""
    body = c.get_optional(f"/api/advanced/runs/{run_id}")
    if body is None:
        raise CliError(f"no such run: {run_id}", EXIT_USAGE)
    return body.get("run") or {}
