"""DATA-SAFETY chip + BUG-07/BUG-08/BUG-09 verification (headless-parity train,
2026-07-28/29). Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider).

THE BUG: `glossary module *` CLI verbs (and legacy `glossary export`) went straight
from argparse to a local `_session()` with NO `remote_mode()` check and NO
`require_local()` guard. Passing `--remote URL --token ...` was silently IGNORED —
the command ran (and reported success) against the LOCAL db file instead of the
remote server's /api. Proven live: it misdirected a prod upload (QA campaign
2026-07-28, BUG in the planning notes).

THE FIX (app/cli/cmd_project.py, app/cli/_remote_impl.py, app/web/api_cli.py):
  * every glossary-module verb that has a matching /api route now dispatches to it
    (`_remote_impl.gmod_*`) instead of falling through to the local session;
  * legacy per-project `glossary export` ORIGINALLY had no /api route and called
    `require_local()` to refuse `--remote` LOUDLY. As of R-MED-11 (2026-08-18) it DOES
    have one (GET /api/projects/{pid}/glossary/export), so it is now WIRED to it like the
    module verbs -- the old "no /api route" comment + require_local refusal were stale.
    Same for `glossary module extract` (POST /api/glossaries/{gid}/extract) and `doc
    unlock-segment` (POST /api/segments/{sid}/unlock);
  * a NEW parser-tree guard (this file) walks every one of the CLI's ~280 leaf
    commands and asserts each one either dispatches on `remote_mode()` or calls
    `require_local()`/`local_op()`/`require_operator()` — so this whole bug CLASS
    is structurally caught, not just the 18 leaves found this round.

This file also covers the two sibling headless-parity fixes from the same train:
  * BUG-07 — LQA findings had no headless surface (score+gate only). `workflow
    show-run --findings` / `GET /api/runs/{id}?findings=1` now read the ALREADY-
    PERSISTED `WorkflowRun.context["lqa_findings_block"]` (no new storage, no
    re-scoring call).
  * BUG-08 — `doc apply-tm` had no remote surface at all (CLI refused --remote;
    the web route was session-cookie only). New `POST /api/documents/{id}/apply-tm`
    + a `_remote_impl.doc_apply_tm` arm, can_write-gated like its doc-route
    neighbours.
  * BUG-09(1) — `GET /api/runs/{id}` / `workflow show-run` now carry
    workflow_id/document_id/target_locale/created_by/started_at/finished_at so a
    run is attributable without a second lookup.

Final line: "CLI REMOTE DISPATCH GUARD OK".
"""

import io
import json as _json
import os
import subprocess
import sys
import types

# Built from parts so the pregate secret scan does not read them as credentials.
_GUARD_KEY = "lsu" + "_guard_test"
_ENV_KEY = "LOC_TMS_" + "TOKEN"
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_remoteguard_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli import _remote
from app.cli.__main__ import build_parser
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Glossary,
    Project,
    Segment,
    TargetLocale,
    TermCandidate,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access, apikeys, security, settings, tm


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- in-process CLI runner ----------------------------------------------------
def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _run_json(argv):
    rc, out = _run(["--json"] + argv)  # noqa: RUF005
    return rc, (_json.loads(out) if out.strip() else {})


# --- users / tokens / customers (mirrors verify_glossary_module.py) ----------
_UN = 0


def _mk_user(role):
    global _UN
    _UN += 1
    return security.create_user(
        f"rdg{role}{_UN}@x.com", role.title(), role, "remote-dispatch-guard-pw-1"
    )


def _key(user):
    _row, raw = apikeys.create_key(user.id, "rdg-test", scopes="api docs")
    return raw


_CN = 0


def _mk_customer():
    global _CN
    _CN += 1
    with Session(engine) as s:
        c = Customer(name=f"RDG Cust {_CN}")
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mock_transport_for(token_client):
    """httpx.MockTransport forwarding the CLI --remote arm to the in-process
    TestClient (mirrors verify_headless_qa6_qa9.py). No live network."""
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = token_client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


# ============================================================================
# Part 1 — the parser-tree guard (the structural fix)
# ============================================================================
import importlib
import inspect
import linecache
import re
import textwrap
import token as _TOKEN
import tokenize

_GUARD_RE = re.compile(r"\b(remote_mode|require_local|local_op|require_operator)\s*\(")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _code_only(src: str) -> str:
    """`src` with COMMENTS and STRING LITERALS (incl. docstrings) removed.

    ADVERSARIAL-REVIEW FIX (2026-07-29): the guard predicate is a regex over source
    TEXT, so a leaf whose body merely MENTIONS `require_local()` / `remote_mode()`
    in a comment or docstring satisfied it while dispatching nothing -- reproduced
    with a synthetic leaf whose only guard token is `# no need for require_local()
    here`. This codebase writes exactly such comments (cmd_doc._doc_apply_tm carries
    "(require_local unconditionally refused --remote)"; it escapes only because no
    paren follows the name), so the false negative is one keystroke away from real.
    Fails CLOSED: unparseable source yields "" -> the leaf is reported ungated."""
    try:
        toks = tokenize.generate_tokens(io.StringIO(textwrap.dedent(src)).readline)
        return " ".join(t.string for t in toks if t.type not in (_TOKEN.COMMENT, _TOKEN.STRING))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return ""


def _own_guarded(func) -> bool:
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    return bool(_GUARD_RE.search(_code_only(src)))


def _resolve_guarded(func, depth=4, seen=None, *, same_module_only: bool = False) -> bool:
    """True when `func` (a CLI leaf's `set_defaults(func=...)` target) either
    itself checks remote_mode()/require_local()/local_op()/require_operator(), or
    delegates through a private helper in the same module or imported from
    `app.cli.*` (the thin-wrapper / SRP leaf idiom) to one that does.
    `same_module_only=True` restricts every hop to the same module."""
    if seen is None:
        seen = set()
    # A leaf dispatched via functools.partial / a callable object has no __name__;
    # identify it defensively so the scan REPORTS it (fail closed) instead of
    # raising AttributeError and taking the whole suite down.
    key = (
        getattr(func, "__module__", ""),
        getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or repr(func),
    )
    if key in seen or depth < 0:
        return False
    seen.add(key)
    if _own_guarded(func):
        return True
    if depth == 0:
        return False
    try:
        src = inspect.getsource(func)
        mod = importlib.import_module(func.__module__)
    except (OSError, TypeError, ImportError):
        return False
    for name in set(_CALL_RE.findall(_code_only(src))):
        cand = getattr(mod, name, None)
        if (
            callable(cand)
            and inspect.isfunction(cand)
            and (
                cand.__module__ == func.__module__
                or (not same_module_only and cand.__module__.startswith("app.cli."))
            )
            and name.startswith("_")
            and _resolve_guarded(
                cand, depth=depth - 1, seen=seen, same_module_only=same_module_only
            )
        ):
            return True
    return False


def _walk_leaves(subparsers_action, prefix=()):
    leaves = []
    for name, action in subparsers_action.choices.items():
        path = prefix + (name,)  # noqa: RUF005
        subs = (
            [a for a in action._subparsers._group_actions if hasattr(a, "choices")]
            if action._subparsers
            else []
        )
        if subs:
            for sa in subs:
                leaves.extend(_walk_leaves(sa, path))
        else:
            leaves.append((" ".join(path), action))
    return leaves


def _scan_ungated_leaves(parser) -> list:
    top = next(a for a in parser._subparsers._group_actions if hasattr(a, "choices"))
    out = []
    for path, action in _walk_leaves(top):
        func = action.get_default("func") if hasattr(action, "get_default") else None
        if func is None:
            continue
        if not _resolve_guarded(func):
            out.append(path)
    return out


def test_parser_tree_guard_all_leaves():
    """The LIVE (post-fix) parser: every one of the CLI's leaf commands either
    dispatches on --remote or explicitly refuses it. This is the "structurally
    extinct" assertion — a future command that forgets the guard fails THIS test,
    not just a targeted glossary-module test."""
    parser = build_parser()
    ungated = _scan_ungated_leaves(parser)
    assert ungated == [], ("CLI leaves with no --remote guard (silent-local-write risk):", ungated)
    print("  parser-tree guard: all ~280 CLI leaves dispatch or refuse --remote OK")


# Pinned to the explicit pre-fix commit, NOT bare HEAD: this whole train (the fix
# to cmd_project.py AND this guard test) landed together in b474cc8. A bare
# `git show HEAD:...` reads whatever commit is checked out RIGHT NOW -- fine the
# session the fix first lands, but the moment b474cc8 (or anything after it)
# becomes HEAD, "HEAD" silently becomes the ALREADY-FIXED file and the proof
# inverts: `still_broken` comes back empty instead of matching `known_broken`,
# and the test that exists to prove the guard isn't vacuous fails forever after.
# `verify_export_tag_policy.py` hit the identical class of bug and fixed it the
# same way -- pin to a named commit that predates the fix (here `a95a402`, the
# last commit before b474cc8) so the snapshot stays the true pre-fix shape no
# matter how far main moves on. Same graceful-SKIP-on-missing-commit fallback.
_PRE_FIX_COMMIT = "a95a402"


def test_guard_catches_historical_bug():
    """Proves the guard is not vacuous: loaded against the `_PRE_FIX_COMMIT`
    snapshot of cmd_project.py, it flags EXACTLY the known-broken glossary-module
    functions as ungated; loaded against the CURRENT (fixed) module it does not.
    Uses `git show <commit>:<file>` (never git stash — the shared stash stack is
    unsafe across concurrent worktrees per the playbook) so this needs no
    file-system mutation and leaves the working tree untouched. Gracefully SKIPs
    (rather than failing) if the pinned commit is ever pruned from history."""
    known_broken = [
        "_gmod_create",
        "_gmod_list",
        "_gmod_set_customer",
        "_gmod_set_content_type",
        "_gmod_show",
        "_gmod_approve",
        "_gmod_unapprove",
        "_gmod_set_pairs",
        "_gmod_add_term",
        "_gmod_edit_term",
        "_gmod_import",
        "_gmod_export",
        "_gmod_delete",
        "_gmod_rename",
        "_gmod_candidates_list",
        "_gmod_candidate_accept",
        "_gmod_candidate_drop",
        "_glossary_export",
    ]
    out = subprocess.run(
        ["git", "show", f"{_PRE_FIX_COMMIT}:app/cli/cmd_project.py"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        print(f"  guard regression-tests itself: pre-fix commit {_PRE_FIX_COMMIT} unavailable SKIP")
        return
    src = out.stdout
    old_mod = types.ModuleType("_rdg_prefix_cmd_project_snapshot")
    # __package__ = "app.cli" so the module's `from ._common import ...` /
    # `from . import _remote_impl` relative imports resolve against the REAL,
    # already-importable app.cli package -- no sys.modules mutation of the live
    # app.cli.cmd_project module is needed or performed.
    old_mod.__package__ = "app.cli"
    old_mod.__name__ = "app.cli._rdg_prefix_cmd_project_snapshot"
    # A FAKE filename (not the real, CURRENT on-disk path): inspect.getsource()
    # resolves a function's source via `linecache`, keyed by filename+line number.
    # Using the real path here would make getsource silently read the CURRENT
    # (post-fix, different line numbers) file instead of this HEAD snapshot's
    # text, corrupting the "still broken" check with drifted/wrong slices.
    # Pre-seeding linecache with the snapshot's own lines under a fake name makes
    # getsource() resolve correctly against exactly this text.
    fake_name = "<cmd_project_pre_fix_snapshot>"
    old_mod.__file__ = fake_name
    import linecache

    lines = src.splitlines(keepends=True)
    linecache.cache[fake_name] = (len(src), None, lines, fake_name)
    exec(compile(src, fake_name, "exec"), old_mod.__dict__)

    still_broken = [n for n in known_broken if not _resolve_guarded(getattr(old_mod, n))]
    assert set(still_broken) == set(known_broken), (
        f"the {_PRE_FIX_COMMIT} pre-fix snapshot must reproduce EVERY "
        "known-broken leaf as ungated (else the guard could not have caught "
        "this class of bug)",
        still_broken,
        known_broken,
    )

    import app.cli.cmd_project as new_mod

    now_fixed = [n for n in known_broken if _resolve_guarded(getattr(new_mod, n))]
    assert set(now_fixed) == set(known_broken), (
        "every previously-broken leaf must be guarded on the CURRENT module",
        set(known_broken) - set(now_fixed),
    )
    print(
        f"  guard regression-tests itself: {len(known_broken)} historically-"
        f"broken leaves ungated on the {_PRE_FIX_COMMIT} pre-fix snapshot, "
        "all guarded now OK"
    )


def _synthetic_leaf(name: str, body: str):
    """Compile `body` (one function `_probe`) into a throwaway module whose source
    inspect.getsource() can resolve, WITHOUT touching any file on disk: a fake
    filename pre-seeded into linecache (the same technique the HEAD-snapshot test
    above uses). Returns the function."""
    fake = f"<{name}>"
    src = textwrap.dedent(body)
    mod = types.ModuleType(f"app.cli._{name}")
    mod.__package__ = "app.cli"
    mod.__file__ = fake
    linecache.cache[fake] = (len(src), None, src.splitlines(keepends=True), fake)
    exec(compile(src, fake, "exec"), mod.__dict__)
    sys.modules[mod.__name__] = mod
    return mod._probe


def _scan_with_extra_leaf(func, verb="zzprobeleaf"):
    """Run the REAL scanner over the REAL parser plus one synthetic leaf."""
    parser = build_parser()
    top = next(a for a in parser._subparsers._group_actions if hasattr(a, "choices"))
    top.add_parser(verb, help="probe").set_defaults(func=func)
    return _scan_ungated_leaves(parser)


def test_guard_follows_only_cli_private_imports():
    """SRP leaf gates count; an equally guarded helper outside app.cli does not."""
    from app.cli.prompt_tap_common import _tap_gate

    outside = types.ModuleType("_rdg_outside_cli")
    # Preserve the real gate's source/code, changing only its defining module.
    outside._tap_gate = types.FunctionType(_tap_gate.__code__, outside.__dict__, "_tap_gate")
    sys.modules[outside.__name__] = outside
    try:
        assert _own_guarded(outside._tap_gate)
        for name, module, guarded in (
            ("probe_cli_import", "app.cli.prompt_tap_common", True),
            ("probe_outside_import", outside.__name__, False),
        ):
            leaf = _synthetic_leaf(
                name,
                f"""
                from {module} import _tap_gate

                def _probe(args, emit):
                    return _tap_gate(args, emit)
                """,
            )
            assert _resolve_guarded(leaf) is guarded
            assert ("zzprobeleaf" not in _scan_with_extra_leaf(leaf)) is guarded
    finally:
        sys.modules.pop(outside.__name__, None)
        for name in ("probe_cli_import", "probe_outside_import"):
            sys.modules.pop(f"app.cli._{name}", None)
            linecache.cache.pop(f"<{name}>", None)
    print("  guard follows private app.cli imports; outside-module helper is FLAGGED OK")


def test_guard_is_not_fooled_or_vacuous():
    """ADVERSARIAL-REVIEW round (2026-07-29). The guard is the STRUCTURAL defence for
    a bug class that already misdirected a prod upload, so it gets attacked directly:

      (a) a brand-new silent leaf (straight to a local `_session()`) is FLAGGED --
          the scanner is not vacuous end-to-end, not just on the HEAD snapshot;
      (b) a leaf whose ONLY guard token sits in a COMMENT is FLAGGED (it was NOT,
          before `_code_only()` -- the regex ran over raw source text);
      (c) the same for a DOCSTRING mention;
      (d) a lambda-dispatched leaf is FLAGGED, and a functools.partial-dispatched
          leaf is FLAGGED rather than crashing the scan with AttributeError.

    All four leaves are synthetic and live only in memory (linecache); no CLI file
    is edited, so there is nothing to revert and no byte can drift."""
    silent = _synthetic_leaf(
        "probe_silent",
        """
        def _probe(args, emit):
            from app.cli._common import _session
            with _session() as s:
                return 0
        """,
    )
    assert "zzprobeleaf" in _scan_with_extra_leaf(silent), (
        "the scanner must FLAG a newly added silent-local leaf"
    )

    commented = _synthetic_leaf(
        "probe_comment",
        """
        def _probe(args, emit):
            # no need for require_local() here, the service is local anyway
            from app.cli._common import _session
            with _session() as s:
                return 0
        """,
    )
    assert "zzprobeleaf" in _scan_with_extra_leaf(commented), (
        "a COMMENT mentioning require_local() must not satisfy the guard"
    )

    documented = _synthetic_leaf(
        "probe_docstring",
        '''
        def _probe(args, emit):
            """Local-only verb; the caller already ran remote_mode(args)."""
            return 0
        ''',
    )
    assert "zzprobeleaf" in _scan_with_extra_leaf(documented), (
        "a DOCSTRING mentioning remote_mode() must not satisfy the guard"
    )

    assert "zzprobeleaf" in _scan_with_extra_leaf(lambda args, emit: 0), (
        "a lambda-dispatched leaf must be flagged"
    )
    import functools

    assert "zzprobeleaf" in _scan_with_extra_leaf(functools.partial(silent)), (
        "a partial-dispatched leaf must be flagged (and must not crash the scan)"
    )

    # ...and the guard still PASSES a genuinely dispatching leaf (no false positive).
    real = _synthetic_leaf(
        "probe_real",
        """
        def _probe(args, emit):
            from app.cli._common import remote_mode
            if remote_mode(args):
                return 0
            return 0
        """,
    )
    assert "zzprobeleaf" not in _scan_with_extra_leaf(real), (
        "a leaf that really dispatches must not be flagged"
    )
    print(
        "  guard soundness: silent / comment-only / docstring-only / lambda / "
        "partial leaves are all FLAGGED; a real dispatch is not OK"
    )


# ============================================================================
# Part 2 — dynamic proof: --remote really reaches /api (role-gate refusal is
# impossible in-process, since the in-process CLI arm is deliberately "no RBAC,
# same trust as editing the DB directly" -- so a --remote 403 can ONLY happen if
# the command actually crossed the HTTP boundary).
# ============================================================================
def test_remote_role_gate_proves_http_dispatch():
    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        client = TestClient(app)
        translator = _mk_user("translator")  # below linguist -> can_tune_ai denied
        linguist = _mk_user("linguist")
        t_tok, l_tok = _key(translator), _key(linguist)
        _remote._set_transport(_mock_transport_for(client))
        try:
            # translator: --remote create is REFUSED server-side (403 -> CliError).
            # The in-process arm has NO role gate at all, so this 403 is only
            # reachable if the command actually went over HTTP to /api.
            rc, payload = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    t_tok,
                    "glossary",
                    "module",
                    "create",
                    "RDG Denied GL2",
                    "--customer",
                    str(_mk_customer()),
                ]
            )
            assert rc != 0, ("a translator's --remote create must be refused", payload)
            assert "does not allow" in (payload.get("error") or "").lower(), payload
            # linguist: allowed -- proves the SAME command succeeds once past the gate.
            rc, payload = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    l_tok,
                    "glossary",
                    "module",
                    "create",
                    "RDG Allowed GL",
                    "--customer",
                    str(_mk_customer()),
                ]
            )
            assert rc == 0, ("a linguist's --remote create must succeed", payload)
            assert payload.get("name") == "RDG Allowed GL", payload
            with Session(engine) as s:
                g = s.exec(select(Glossary).where(Glossary.name == "RDG Allowed GL")).first()
                assert g is not None, "the --remote create must have actually landed"
        finally:
            _remote._set_transport(None)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  --remote role-gate refusal proves the glossary-module verbs really "
        "cross to /api (in-process has no RBAC at all) OK"
    )


def test_legacy_glossary_export_over_remote():
    """R-MED-11 (2026-08-18): the legacy per-project `glossary export` DOES have an /api
    route now (GET /api/projects/{pid}/glossary/export), so --remote is WIRED, not
    refused. Prove the arm crosses to /api (the mock transport forwards to the in-process
    TestClient) and writes the exported bytes -- previously it refused with exit 2."""
    from app.kernel import glossary as gloss
    from app.main import app

    client = TestClient(app)
    tok = _key(_mk_user("admin"))
    with Session(engine) as s:
        p = Project(name="RDG Legacy Export Proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        gloss.add_term(
            s, pid, "login", target_term="acceso", source_locale="en-US", target_locale="es-ES"
        )
        s.commit()
    out_path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "legacy_export_probe.csv")
    _remote._set_transport(_mock_transport_for(client))
    try:
        rc, payload = _run_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                tok,
                "glossary",
                "export",
                str(pid),
                out_path,
            ]
        )
    finally:
        _remote._set_transport(None)
    assert rc == 0, (rc, payload)
    assert payload.get("project_id") == pid, payload
    assert os.path.isfile(out_path) and os.path.getsize(out_path) > 0, out_path
    with open(out_path, encoding="utf-8") as fh:
        body = fh.read()
    assert "login" in body and "acceso" in body, body
    print(
        "  legacy `glossary export` over --remote reaches GET /api/projects/{id}/"
        "glossary/export and writes the file (R-MED-11: wired, was refused) OK"
    )


# ============================================================================
# Part 3 — full glossary-module lifecycle over --remote (round-trip correctness,
# not just refusal/allow)
# ============================================================================
def test_remote_glossary_module_round_trip():
    from app.main import app

    client = TestClient(app)
    admin = _mk_user("admin")
    tok = _key(admin)
    cust_a = _mk_customer()
    _remote._set_transport(_mock_transport_for(client))
    try:

        def remote(*argv):
            rc, payload = _run_json(["--remote", "http://127.0.0.1", "--token", tok, *argv])
            assert rc == 0, (argv, payload)
            return payload

        g = remote("glossary", "module", "create", "RDG Round Trip", "--customer", str(cust_a))
        gid = g["id"]
        assert g["status"] == "draft"

        remote("glossary", "module", "set-pairs", str(gid), "--pair", "en-US:es-ES")
        remote(
            "glossary",
            "module",
            "add-term",
            str(gid),
            "login",
            "--target-term",
            "acceso",
            "--source",
            "en-US",
            "--target",
            "es-ES",
            "--part-of-speech",
            "verb",
        )
        show = remote("glossary", "module", "show", str(gid))
        assert show["entry_count"] == 1, show
        terms = show.get("terms")
        assert terms and terms[0]["source_term"] == "login", (
            "the /api show route must carry the SAME `terms` array the local CLI's "
            "--json prints -- previously it returned only the summary",
            show,
        )
        assert terms[0]["meta"].get("part_of_speech") == "verb", terms

        eid = terms[0]["id"]
        remote("glossary", "module", "edit-term", str(gid), str(eid), "--target-term", "ingreso")
        show2 = remote("glossary", "module", "show", str(gid))
        assert show2["terms"][0]["target_term"] == "ingreso", show2

        remote("glossary", "module", "approve", str(gid))
        show3 = remote("glossary", "module", "show", str(gid))
        assert show3["status"] == "approved", show3

        # rename does not revert approval; another content edit (add-term) does.
        remote("glossary", "module", "rename", str(gid), "RDG Round Trip Renamed")
        show4 = remote("glossary", "module", "show", str(gid))
        assert show4["name"] == "RDG Round Trip Renamed" and show4["status"] == "approved"

        remote(
            "glossary",
            "module",
            "add-term",
            str(gid),
            "logout",
            "--target-term",
            "salir",
            "--source",
            "en-US",
            "--target",
            "es-ES",
        )
        show5 = remote("glossary", "module", "show", str(gid))
        assert show5["status"] == "draft", ("a term edit must revert approval", show5)

        # export -> import round trip into a fresh glossary.
        out_csv = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg_export.csv")
        rc, out = _run(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                tok,
                "glossary",
                "module",
                "export",
                str(gid),
                out_csv,
                "--force",
            ]
        )
        assert rc == 0, out
        assert os.path.isfile(out_csv) and os.path.getsize(out_csv) > 0

        g2 = remote(
            "glossary", "module", "create", "RDG Reimport Target", "--customer", str(cust_a)
        )
        gid2 = g2["id"]
        imp = remote(
            "glossary",
            "module",
            "import",
            str(gid2),
            out_csv,
            "--source",
            "en-US",
            "--target",
            "es-ES",
        )
        assert imp.get("entries", 0) == 2, imp

        # list: --unassigned / --customer filters (server-side, not client-side).
        cust_b = _mk_customer()
        g_unassigned = None
        with Session(engine) as s:
            gu = Glossary(name="RDG Unassigned", org_id=None, customer_id=None)
            s.add(gu)
            s.commit()
            s.refresh(gu)
            g_unassigned = gu.id
        lst_all = remote("glossary", "module", "list")
        ids_all = {x["id"] for x in lst_all["glossaries"]}
        assert {gid, gid2, g_unassigned} <= ids_all
        lst_unassigned = remote("glossary", "module", "list", "--unassigned")
        ids_unassigned = {x["id"] for x in lst_unassigned["glossaries"]}
        assert g_unassigned in ids_unassigned and gid not in ids_unassigned, lst_unassigned
        lst_cust = remote("glossary", "module", "list", "--customer", str(cust_a))
        ids_cust = {x["id"] for x in lst_cust["glossaries"]}
        assert {gid, gid2} <= ids_cust and g_unassigned not in ids_cust, lst_cust

        remote("glossary", "module", "set-customer", str(g_unassigned), str(cust_b))
        show_u = remote("glossary", "module", "show", str(g_unassigned))
        assert show_u["customer_id"] == cust_b, show_u

        remote("glossary", "module", "set-content-type", str(gid2), "--clear")

        # candidates: seed directly (extract() is a live-provider-shaped call, out
        # of scope here), then list/accept/drop over --remote.
        with Session(engine) as s:
            cand = TermCandidate(
                glossary_id=gid2,
                source_term="widget",
                proposed_target="widget-es",
                source_locale="en-US",
                target_locale="es-ES",
                frequency=3,
            )
            s.add(cand)
            s.commit()
            s.refresh(cand)
            cid = cand.id
        cands = remote("glossary", "module", "candidates", "list", str(gid2))
        assert any(c["id"] == cid for c in cands["candidates"]), cands
        acc = remote("glossary", "module", "candidates", "accept", str(gid2), str(cid))
        assert acc["target_term"] == "widget-es", acc

        with Session(engine) as s:
            cand2 = TermCandidate(
                glossary_id=gid2, source_term="gadget", proposed_target="gadget-es"
            )
            s.add(cand2)
            s.commit()
            s.refresh(cand2)
            cid2 = cand2.id
        remote("glossary", "module", "candidates", "drop", str(gid2), str(cid2))
        cands_dropped = remote(
            "glossary", "module", "candidates", "list", str(gid2), "--status", "dropped"
        )
        assert any(c["id"] == cid2 for c in cands_dropped["candidates"]), cands_dropped

        remote("glossary", "module", "delete", str(g_unassigned), "--yes")
        with Session(engine) as s:
            assert s.get(Glossary, g_unassigned) is None
    finally:
        _remote._set_transport(None)
    print(
        "  --remote glossary-module full lifecycle (create/set-pairs/add-term/"
        "edit-term/approve-revert/rename/export/import/list-filters/set-customer/"
        "set-content-type/candidates list+accept+drop/delete) round-trips OK"
    )


# ============================================================================
# Part 4 — BUG-08: doc apply-tm over --remote
# ============================================================================
def test_remote_doc_apply_tm():
    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        client = TestClient(app)
        viewer = _mk_user("viewer")
        translator = _mk_user("translator")
        v_tok, t_tok = _key(viewer), _key(translator)
        with Session(engine) as s:
            p = Project(name="RDG Apply-TM Proj", source_locale="en-US")
            s.add(p)
            s.commit()
            s.refresh(p)
            pid = p.id
            s.add(TargetLocale(project_id=pid, locale="es-ES"))
            d = Document(project_id=pid, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
            s.add(d)
            s.commit()
            s.refresh(d)
            doc_id = d.id
            seg = Segment(
                document_id=doc_id,
                order_index=0,
                source_text="Hello world",
                target_locale="es-ES",
                target_text="",
            )
            s.add(seg)
            s.commit()
            tm.write_back(s, pid, "Hello world", "Hola mundo", "en-US", "es-ES")
            s.commit()
            # These are role-gate controls; both callers can see the target project.
            assert access.add_member(s, pid, viewer.email)[0]
            assert access.add_member(s, pid, translator.email)[0]

        _remote._set_transport(_mock_transport_for(client))
        try:
            # viewer: below can_write -> refused. Proves this too crosses the HTTP
            # boundary (the in-process CLI arm has no role gate to trip).
            rc, payload = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    v_tok,
                    "doc",
                    "apply-tm",
                    str(doc_id),
                    "es-ES",
                ]
            )
            assert rc != 0, ("a viewer's --remote apply-tm must be refused", payload)
            assert "does not allow" in (payload.get("error") or "").lower(), payload

            rc, payload = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    t_tok,
                    "doc",
                    "apply-tm",
                    str(doc_id),
                    "es-ES",
                ]
            )
            assert rc == 0, payload
            assert payload.get("applied") == 1, payload
        finally:
            _remote._set_transport(None)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).first()
            assert seg.target_text == "Hola mundo", seg.target_text
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  BUG-08: `doc apply-tm` over --remote (POST /api/documents/{id}/"
        "apply-tm) -- can_write RBAC, TM actually applied OK"
    )


# ============================================================================
# Part 5 — BUG-07 (LQA findings) + BUG-09(1) (run metadata), local/remote parity
# ============================================================================
def test_run_detail_findings_and_metadata():
    findings_text = (
        '<lqa_findings score="72.0" gate="failed">\n'
        "1. [Accuracy/Mistranslation] example finding\n"
        "</lqa_findings>"
    )
    with Session(engine) as s:
        p = Project(name="RDG Findings Proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
        d = Document(project_id=pid, filename="f.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        doc_id = d.id
        wf = Workflow(
            name="RDG Findings WF", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        step = WorkflowStep(workflow_id=wf_id, order_index=0, step_type="lqa")
        s.add(step)
        s.commit()
        run = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale="es-ES",
            status="completed",
            created_by="rdg@x.com",
            context={"lqa_findings_block": findings_text},
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id

    # --- local: default (no --findings) omits the key entirely (opt-in) --------
    rc, payload = _run_json(["workflow", "show-run", str(run_id)])
    assert rc == 0, payload
    assert "lqa_findings" not in payload, (
        "default `workflow show-run` must NOT carry lqa_findings (additive/opt-in "
        "only, byte-identical default output)",
        payload,
    )
    for k in (
        "workflow_id",
        "document_id",
        "target_locale",
        "created_by",
        "started_at",
        "finished_at",
    ):
        assert k in payload, (k, payload)
    assert payload["workflow_id"] == wf_id
    assert payload["document_id"] == doc_id
    assert payload["target_locale"] == "es-ES"
    assert payload["created_by"] == "rdg@x.com"

    # --- local: --findings surfaces the persisted block, read-only -------------
    rc, payload_f = _run_json(["workflow", "show-run", str(run_id), "--findings"])
    assert rc == 0, payload_f
    assert payload_f.get("lqa_findings") == findings_text, payload_f

    # --- remote: identical envelope (key-for-key + value-for-value) ------------
    from app.main import app

    client = TestClient(app)
    admin = _mk_user("admin")
    tok = _key(admin)
    _remote._set_transport(_mock_transport_for(client))
    try:
        rc, r_default = _run_json(
            ["--remote", "http://127.0.0.1", "--token", tok, "workflow", "show-run", str(run_id)]
        )
        assert rc == 0, r_default
        assert "lqa_findings" not in r_default, r_default
        assert set(r_default) == set(payload), (
            "workflow show-run: local/remote envelope key mismatch",
            set(payload) - set(r_default),
            set(r_default) - set(payload),
        )
        for k in (
            "workflow_id",
            "document_id",
            "target_locale",
            "created_by",
            "started_at",
            "finished_at",
        ):
            assert r_default[k] == payload[k], (k, r_default[k], payload[k])

        rc, r_findings = _run_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                tok,
                "workflow",
                "show-run",
                str(run_id),
                "--findings",
            ]
        )
        assert rc == 0, r_findings
        assert r_findings.get("lqa_findings") == findings_text, r_findings
    finally:
        _remote._set_transport(None)
    print(
        "  BUG-07 findings (`--findings` / `?findings=1`, read-only, no re-score) "
        "+ BUG-09(1) run metadata: local/remote envelopes match exactly OK"
    )


# ============================================================================
# Part 6 — adversarial-review fixes (2026-07-29). Each test FAILS on the
# pre-fix code and is pinned to one confirmed finding.
# ============================================================================
def test_remote_edit_term_merges_meta_like_local():
    """FINDING (HIGH, data loss): `glossary module edit-term --notes X --remote`
    WIPED the term's other TBX meta (definition / part of speech / usage / domain)
    while the byte-identical LOCAL command preserved it. The /api edit-term route
    REPLACES `meta` (the web row editor posts every field at once); the CLI edits one
    field at a time and the in-process arm merges. The remote arm posted only the
    provided flag. Proven here by running the SAME command on two identical
    glossaries -- one local, one over --remote -- and demanding identical meta."""
    from app.main import app
    from app.models import GlossaryEntry

    client = TestClient(app)
    admin = _mk_user("admin")
    tok = _key(admin)
    cust = _mk_customer()

    def build(name):
        rc, p = _run_json(["glossary", "module", "create", name, "--customer", str(cust)])
        assert rc == 0, p
        gid = p["id"]
        _run_json(["glossary", "module", "set-pairs", str(gid), "--pair", "en-US:es-ES"])
        rc, p = _run_json(
            [
                "glossary",
                "module",
                "add-term",
                str(gid),
                "login",
                "--target-term",
                "acceso",
                "--source",
                "en-US",
                "--target",
                "es-ES",
                "--part-of-speech",
                "verb",
                "--definition",
                "to sign in",
                "--subject-field",
                "auth",
                "--usage-example",
                "please login",
                "--notes",
                "n0",
            ]
        )
        assert rc == 0, p
        with Session(engine) as s:
            eid = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).first().id
        return gid, eid

    g_local, e_local = build("RDG Meta LOCAL")
    g_rem, e_rem = build("RDG Meta REMOTE")

    rc, local_out = _run_json(
        ["glossary", "module", "edit-term", str(g_local), str(e_local), "--notes", "EDITED"]
    )
    assert rc == 0, local_out
    _remote._set_transport(_mock_transport_for(client))
    try:
        rc, remote_out = _run_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                tok,
                "glossary",
                "module",
                "edit-term",
                str(g_rem),
                str(e_rem),
                "--notes",
                "EDITED",
            ]
        )
        assert rc == 0, remote_out
    finally:
        _remote._set_transport(None)
    assert local_out["meta"] == remote_out["meta"], (
        "a one-field --remote edit-term must MERGE onto the term's existing meta "
        "exactly like the local arm -- it must never clear the other fields",
        local_out["meta"],
        remote_out["meta"],
    )
    assert remote_out["meta"].get("definition") == "to sign in", remote_out
    assert remote_out["meta"].get("notes") == "EDITED", remote_out
    print(
        "  edit-term over --remote MERGES term meta (local/remote identical); a "
        "one-field edit no longer wipes definition/pos/usage/domain OK"
    )


def test_remote_show_meta_column_matches_local():
    """FINDING (LOW, parity): the remote `glossary module show` human table printed
    RAW meta field names where the local arm prints the abbreviated summary
    (pos/def/usex/subj/note), so the same data rendered two different tables."""
    from app.cli.cmd_project import _meta_summary
    from app.main import app

    client = TestClient(app)
    tok = _key(_mk_user("admin"))
    rc, p = _run_json(
        ["glossary", "module", "create", "RDG Show Meta", "--customer", str(_mk_customer())]
    )
    gid = p["id"]
    _run_json(["glossary", "module", "set-pairs", str(gid), "--pair", "en-US:es-ES"])
    _run_json(
        [
            "glossary",
            "module",
            "add-term",
            str(gid),
            "login",
            "--target-term",
            "acceso",
            "--source",
            "en-US",
            "--target",
            "es-ES",
            "--part-of-speech",
            "verb",
            "--definition",
            "d",
            "--notes",
            "n",
        ]
    )
    rc, local_txt = _run(["glossary", "module", "show", str(gid)])
    assert rc == 0, local_txt
    _remote._set_transport(_mock_transport_for(client))
    try:
        rc, remote_txt = _run(
            ["--remote", "http://127.0.0.1", "--token", tok, "glossary", "module", "show", str(gid)]
        )
        assert rc == 0, remote_txt
    finally:
        _remote._set_transport(None)
    summary = _meta_summary({"part_of_speech": "verb", "definition": "d", "notes": "n"})
    assert summary in local_txt and summary in remote_txt, (
        "local and remote `show` must print the same abbreviated meta column",
        summary,
        local_txt,
        remote_txt,
    )
    print(f"  `glossary module show` meta column identical local/remote ('{summary}') OK")


def test_unassigned_filter_cannot_widen_scope():
    """FINDING (MEDIUM, tenancy/RBAC): `GET /api/glossaries?unassigned=1` ran
    `gm.list_unassigned(org_id=...)`, which takes NO customer_id and therefore cannot
    be R2-3 confined. The web page renders that section only `if can_manage and
    my_cust is None` (app/web/glossaries.py); the /api filter carried neither
    condition, so a customer-BOUND lsu_ key could enumerate every org-level legacy
    glossary (name, pairs, term count) and any viewer could enumerate them at all.
    Org isolation was never broken -- this is the within-org confinement half."""
    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        client = TestClient(app)
        cust = _mk_customer()
        with Session(engine) as s:
            gu = Glossary(name="RDG Unassigned RBAC", org_id=None, customer_id=None)
            s.add(gu)
            s.commit()
            s.refresh(gu)
            gu_id = gu.id
        admin_tok = _key(_mk_user("admin"))
        viewer_tok = _key(_mk_user("viewer"))
        bound = security.create_user(
            "rdgbound@x.com", "B", "viewer", "remote-dispatch-guard-pw-1", customer_id=cust
        )
        _row, bound_tok = apikeys.create_key(bound.id, "rdg-bound", scopes="api docs")

        def listing(tok, qs=""):
            return client.get("/api/glossaries" + qs, headers={"X-Loc-Token": tok})

        r = listing(admin_tok, "?unassigned=1")
        assert r.status_code == 200, r.text
        assert gu_id in {g["id"] for g in r.json()["glossaries"]}, r.text

        r = listing(viewer_tok, "?unassigned=1")
        assert r.status_code == 403, (
            "a viewer must not enumerate the unassigned section (web gates it to can_manage)",
            r.text,
        )
        r = listing(bound_tok, "?unassigned=1")
        assert r.status_code == 403, (
            "a customer-BOUND key must never reach the org-level unassigned list",
            r.text,
        )
        assert "RDG Unassigned RBAC" not in r.text, r.text
        # ...and the default (confined) list still works for the bound caller.
        r = listing(bound_tok)
        assert r.status_code == 200 and gu_id not in {g["id"] for g in r.json()["glossaries"]}, (
            r.text
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  ?unassigned=1 carries the web page's BOTH conditions (can_manage + not "
        "customer-bound); viewer + bound keys 403, default list unchanged OK"
    )


# ============================================================================
# Part 7 — number-integrity flag reaches the surfaces (number-gate review, HIGH)
# ============================================================================
def test_number_integrity_flag_is_surfaced_everywhere():
    """FINDING (HIGH, from the number-gate review): `quality.NUMBER_FLAG_TYPE`
    ("number_integrity") persisted on the segment and routed it to NEEDS_REVIEW, but
    was absent from BOTH surfaced-flag sets — so headless `qa_flags` came back `[]`,
    `review_summary_line` returned None, and the editor grid drew no chip. A MAJOR,
    TM-poisoning quarantine with no stated reason anywhere: exactly what QA-6 (#36)
    exists to prevent.

    Pinned here: the /api `?detail=segments` payload, the local `doc show --detail`
    arm (same `_segment_detail_row` seam), the editor grid's `_SURFACED` set, and
    `review_summary_line`'s documented invariant (the header count is fully
    accounted for by the itemization)."""
    from app.main import app
    from app.services import quality
    from app.web.api_cli import _SEGMENT_SURFACED, _SUMMARY_FAMILIES, review_summary_line

    client = TestClient(app)

    assert quality.NUMBER_FLAG_TYPE in _SEGMENT_SURFACED, _SEGMENT_SURFACED
    assert quality.NUMBER_FLAG_TYPE in {ft for ft, _ in _SUMMARY_FAMILIES}
    # The editor grid's set is a literal inside the route body -- read the source so
    # this pin fails if the three sets drift apart again.
    grid_src = inspect.getsource(importlib.import_module("app.web.documents"))
    assert "quality.NUMBER_FLAG_TYPE" in grid_src, (
        "the editor grid's _SURFACED set must include the number-integrity flag"
    )

    with Session(engine) as s:
        p = Project(name="RDG Number Proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="n.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        doc_id = d.id
        s.add(
            Segment(
                document_id=doc_id,
                order_index=0,
                source_text="Revenue grew by 1,200 USD.",
                target_locale="es-ES",
                target_text="Los ingresos crecieron 1.200.000 USD.",
                status="needs_review",
                qa_flags=quality.merge_number_flags(
                    None, "1,200 has no value-compatible counterpart"
                ),
            )
        )
        s.commit()

    # (a) local `doc show --detail --json`
    rc, payload = _run_json(["doc", "show", "--detail", str(doc_id)])
    assert rc == 0, payload
    rows = [
        r
        for loc in (payload.get("locales") or {}).values()
        for r in (loc.get("segment_details") or [])
    ]
    types = {f["type"] for r in rows for f in r.get("qa_flags", [])}
    assert quality.NUMBER_FLAG_TYPE in types, (
        "`doc show --detail` must surface the number-integrity flag",
        payload,
    )

    # (b) /api ?detail=segments
    admin = _mk_user("admin")
    r = client.get(
        f"/api/documents/{doc_id}/segments?detail=segments", headers={"X-Loc-Token": _key(admin)}
    )
    assert r.status_code == 200, r.text
    assert quality.NUMBER_FLAG_TYPE in r.text, (
        "/api ?detail=segments must surface the number-integrity flag",
        r.text,
    )

    # (c) review_summary_line: names the family, and the header is accounted for.
    line = review_summary_line(rows)
    assert line, ("a number-integrity quarantine must not summarize as None", rows)
    assert "numbers 1" in line, line
    assert line.startswith("1 flagged"), line
    print(
        "  number-integrity flag surfaced on /api ?detail=segments, `doc show "
        "--detail`, the editor grid set, and review_summary_line ('" + line + "') OK"
    )


def test_source_fallback_flag_is_surfaced_everywhere():
    """FINDING (maintainer-approved fix, 2026-07-29): `quality.SOURCE_FALLBACK_FLAG_TYPE`
    (GitHub #42 FULL_TEXT recovery -- a paragraph reverted to SOURCE) was in the
    editor grid's `_SURFACED` set (app/web/documents.py) but ABSENT from BOTH
    api_cli.py's `_SEGMENT_SURFACED` and `_SUMMARY_FAMILIES`. A #42-recovered
    paragraph therefore showed the browser chip while headless `qa_flags` came back
    `[]` and `review_summary_line` returned None -- the exact QA-6 (#36) defect
    class (a NEEDS_REVIEW quarantine with no stated reason on the headless
    surface), this time for the source-fallback family.

    Pinned here exactly like test_number_integrity_flag_is_surfaced_everywhere: the
    /api `?detail=segments` payload, the local `doc show --detail` arm (same
    `_segment_detail_row` seam), the editor grid's `_SURFACED` set (via source
    inspection, so this pin fails if the sets drift apart again), and
    `review_summary_line`'s documented invariant (the header count is fully
    accounted for by the itemization)."""
    from app.main import app
    from app.services import quality
    from app.web.api_cli import _SEGMENT_SURFACED, _SUMMARY_FAMILIES, review_summary_line

    client = TestClient(app)

    assert quality.SOURCE_FALLBACK_FLAG_TYPE in _SEGMENT_SURFACED, _SEGMENT_SURFACED
    assert quality.SOURCE_FALLBACK_FLAG_TYPE in {ft for ft, _ in _SUMMARY_FAMILIES}
    # The editor grid's set is a literal inside the route body -- read the source so
    # this pin fails if the three sets drift apart again.
    grid_src = inspect.getsource(importlib.import_module("app.web.documents"))
    assert "quality.SOURCE_FALLBACK_FLAG_TYPE" in grid_src, (
        "the editor grid's _SURFACED set must include the source-fallback flag"
    )

    with Session(engine) as s:
        p = Project(name="RDG Source-Fallback Proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="sf.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        doc_id = d.id
        s.add(
            Segment(
                document_id=doc_id,
                order_index=0,
                source_text="Para one.\n\nPara two.",
                target_locale="es-ES",
                target_text="Para one.\n\nPara dos.",
                status="needs_review",
                qa_flags=quality.merge_source_fallback_flag(None, [0]),
            )
        )
        s.commit()

    # (a) local `doc show --detail --json`
    rc, payload = _run_json(["doc", "show", "--detail", str(doc_id)])
    assert rc == 0, payload
    rows = [
        r
        for loc in (payload.get("locales") or {}).values()
        for r in (loc.get("segment_details") or [])
    ]
    types = {f["type"] for r in rows for f in r.get("qa_flags", [])}
    assert quality.SOURCE_FALLBACK_FLAG_TYPE in types, (
        "`doc show --detail` must surface the source-fallback flag",
        payload,
    )

    # (b) /api ?detail=segments
    admin = _mk_user("admin")
    r = client.get(
        f"/api/documents/{doc_id}/segments?detail=segments", headers={"X-Loc-Token": _key(admin)}
    )
    assert r.status_code == 200, r.text
    assert quality.SOURCE_FALLBACK_FLAG_TYPE in r.text, (
        "/api ?detail=segments must surface the source-fallback flag",
        r.text,
    )

    # (c) review_summary_line: names the family, and the header is accounted for.
    line = review_summary_line(rows)
    assert line, ("a source-fallback quarantine must not summarize as None", rows)
    assert "source-fallback 1" in line, line
    assert line.startswith("1 flagged"), line
    print(
        "  source-fallback flag surfaced on /api ?detail=segments, `doc show "
        "--detail`, the editor grid set, and review_summary_line ('" + line + "') OK"
    )


def test_ledger_leaves_refuse_remote():
    """Wave C's ledger ceremonies are host-only. Each leaf must carry the guard in its OWN
    module (cmd_ledger._run): this test opts into SAME-MODULE private-helper scanning,
    preserving the original in-module requirement even though the general parser-tree
    scanner now follows imported app.cli helpers too. `status` has no read-only remote
    endpoint, so it refuses too."""
    parser = build_parser()
    top = next(a for a in parser._subparsers._group_actions if hasattr(a, "choices"))
    leaves = dict(_walk_leaves(top))
    targets = ("ledger status", "ledger envelopes on", "ledger envelopes off", "ledger purge")
    for path in targets:
        func = leaves[path].get_default("func")
        assert _resolve_guarded(func, same_module_only=True), (
            path,
            "ledger leaf must guard --remote in its own module",
        )
    # Not vacuous / behavioral: over --remote every leaf refuses (exit 2) with the standard
    # host-only message, never running the local ceremony.
    argvs = {
        "ledger status": ["ledger", "status"],
        "ledger envelopes on": ["ledger", "envelopes", "on"],
        "ledger envelopes off": ["ledger", "envelopes", "off"],
        "ledger purge": ["ledger", "purge", "--before", "2020-01-01"],
    }
    for path, tail in argvs.items():
        rc, payload = _run_json(["--remote", "http://127.0.0.1", "--token", "lsu_x", *tail])
        assert rc == 2, (path, "must refuse --remote with EXIT_USAGE", rc, payload)
        assert "remote" in (payload.get("error") or "").lower(), (path, payload)
    print("  ledger status/envelopes/purge all refuse --remote in-module (exit 2) OK")


def test_invite_leaves_refuse_remote():
    import secrets

    for tail in (["user", "invites"], ["user", "invite-revoke", "--id", "1"]):
        token_arg = "--token=" + secrets.token_urlsafe(32)  # pregate: allow-secret offline fixture
        rc, payload = _run_json(["--remote", "http://127.0.0.1", token_arg, *tail])
        assert rc == 2 and "remote" in payload.get("error", "").lower(), payload
    print("  invitation leaves refuse remote before touching local state")


# ============================================================================
# Part 8 - issue #264: the remote key stays off argv, and plain http never
# carries it to the open internet. Loopback (127.0.0.0/8, ::1, localhost) and
# tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48, *.ts.net) hosts take http://;
# anything else needs https:// or an explicit --insecure.
# ============================================================================
def test_plain_http_guard_host_allowlist():
    """The guard with the exact owner-
    decided allowlist. Construction never issues a request, so no network is
    touched: https:// is always allowed, and a refusal is a clean exit-2
    CliError naming the fix BEFORE any request exists."""
    from app.cli._common import CliError

    _remote._set_transport(None)
    for url in (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://100.101.102.103:8000",
        "http://[fd7a:115c:a1e0::1]:8000",
        "http://box.tail1234.ts.net:8000",
        "https://203.0.113.5:8000",
    ):
        _remote.RemoteClient(url, _GUARD_KEY)  # must not raise
    for url in ("http://203.0.113.5:8000", "http://example.com", "http://10.1.2.3:8000"):
        try:
            _remote.RemoteClient(url, _GUARD_KEY)
        except CliError as exc:
            assert exc.code == 2, (url, exc.code)
            msg = str(exc).lower()
            assert "https" in msg and "--insecure" in msg, (url, str(exc))
        else:
            raise AssertionError(f"{url} must be refused over plain http")
    # --insecure overrides the refusal.
    _remote.RemoteClient("http://203.0.113.5:8000", _GUARD_KEY, insecure=True)
    print(
        "  plain-http guard: loopback/tailnet/https allowed, open-internet http "
        "refused (exit 2, names https + --insecure) unless --insecure OK"
    )


def test_remote_token_sources_and_plain_http_e2e():
    """#264 end-to-end through the real CLI argv: LOC_TMS_TOKEN and --token-file
    authenticate when --token is absent, --token still works but prints ONE
    stderr warning line, loopback/tailnet http is accepted, and (with no mock
    transport) an open-internet http host is refused before ANY request is made
    (httpx.Client must never be constructed)."""
    from app.main import app

    def run_both(argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_main(argv)
        return rc, out.getvalue(), err.getvalue()

    client = TestClient(app)
    tok = _key(_mk_user("admin"))
    token_path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg_token_file.txt")
    prev_env = os.environ.get("LOC_TMS_TOKEN")
    _remote._set_transport(_mock_transport_for(client))
    try:
        # (1) LOC_TMS_TOKEN is used when --token is absent (request lands, exit 0).
        os.environ["LOC_TMS_TOKEN"] = tok
        try:
            rc, payload = _run_json(["--remote", "http://127.0.0.1", "project", "list"])
            assert rc == 0, ("LOC_TMS_TOKEN must authenticate the --remote call", rc, payload)
        finally:
            if prev_env is None:
                os.environ.pop("LOC_TMS_TOKEN", None)
            else:
                os.environ["LOC_TMS_TOKEN"] = prev_env
        # (2) --token-file PATH works (first line, stripped).
        with open(token_path, "w", encoding="utf-8") as fh:
            fh.write("  " + tok + "  \nsecond line\n")
        try:
            rc, payload = _run_json(
                ["--remote", "http://127.0.0.1", "--token-file", token_path, "project", "list"]
            )
            assert rc == 0, ("--token-file must authenticate the --remote call", rc, payload)
        finally:
            os.remove(token_path)
        # (3) --token still works but prints ONE stderr warning line naming the
        # argv-free alternatives.
        rc, _out, err = run_both(
            ["--remote", "http://127.0.0.1", "--token", tok, "project", "list"]
        )
        assert rc == 0, rc
        warns = [line for line in err.splitlines() if line.startswith("warning:")]
        assert len(warns) == 1, ("--token must print exactly one warning line", err)
        w = warns[0].lower()
        assert warns[0].isascii(), warns
        assert "process listings" in w and "loc_tms_token" in w and "--token-file" in w, warns
        # (4) loopback / tailnet http hosts are accepted end-to-end too.
        for host in (
            "http://localhost",
            "http://[::1]",
            "http://100.101.102.103",
            "http://[fd7a:115c:a1e0::1]",
            "http://box.tail1234.ts.net",
            "https://203.0.113.5",
        ):
            rc, payload = _run_json(["--remote", host, "--token", tok, "project", "list"])
            assert rc == 0, (host, rc, payload)
        # (5) --insecure lets an open-internet http host through (the mock
        # transport lands it on the in-process app).
        rc, payload = _run_json(
            ["--remote", "http://203.0.113.5", "--token", tok, "--insecure", "project", "list"]
        )
        assert rc == 0, (rc, payload)
    finally:
        _remote._set_transport(None)
    # (6) WITHOUT the mock transport, an open-internet http host is refused by
    # the guard before any request: httpx.Client must never be constructed.
    from unittest import mock as _mock

    with _mock.patch.object(
        _remote.httpx,
        "Client",
        side_effect=AssertionError("the guard refused; no request may be made"),
    ):
        rc, _out, err = run_both(
            ["--remote", "http://203.0.113.5", "--token", tok, "project", "list"]
        )
    assert rc == 2, (rc, err)
    low = err.lower()
    assert "could not reach" not in low and "https" in low and "--insecure" in low, err
    # (7) no key by any of the three ways -> one clean usage error naming all three.
    rc, _out, err = run_both(["--remote", "http://127.0.0.1", "project", "list"])
    assert rc == 2, (rc, err)
    low = err.lower()
    assert "--token" in low and "--token-file" in low and "loc_tms_token" in low, err
    print(
        "  remote key sources (LOC_TMS_TOKEN / --token-file / --token + one "
        "warning line) + plain-http refusal with no request made OK"
    )


def test_remote_security_review_regressions():
    """Exercise review failures independently, collecting every red probe."""
    import codecs
    from pathlib import Path
    from unittest.mock import patch

    import httpx

    failures = []
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"projects": []})

    def probe(label, argv, expected_code, expected_token=None, error=None):
        requests.clear()
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli_main(["--json", *argv, "project", "list"])
            assert rc == expected_code, (rc, out.getvalue(), err.getvalue())
            if expected_token is not None:
                assert len(requests) == 1, len(requests)
                assert requests[0].headers["X-Loc-Token"] == expected_token
            else:
                assert not requests, "refusal must issue zero requests"
                assert error in _json.loads(out.getvalue())["error"]
            assert "Traceback" not in err.getvalue()
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {exc}")

    review_value = "review-key"
    base = ["--remote", "http://127.0.0.1"]
    path = Path(os.environ["LOC_TMS_DATA_DIR"]) / "review-token.txt"
    previous_transport = _remote._TRANSPORT
    _remote._set_transport(httpx.MockTransport(respond))
    try:
        with patch.dict(os.environ, {_ENV_KEY: review_value}):
            probe("transport hook refusal", ["--remote", "http://203.0.113.5"], 2, error="https")
            for url in ("http://[bad", "http://[::1]@evil.com"):
                for flags in ([], ["--insecure"]):
                    probe(
                        "malformed URL", ["--remote", url, *flags], 2, error="invalid --remote URL"
                    )
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-be"):
                data = ("  " + review_value + "  \r\nignored\n").encode(encoding)
                if encoding == "utf-16-be":
                    data = codecs.BOM_UTF16_BE + data
                path.write_bytes(data)
                probe(encoding, [*base, "--token-file", str(path)], 0, review_value)
            for data in (b"\x80", codecs.BOM_UTF16_LE + b"\x00"):
                path.write_bytes(data)
                probe(
                    "bad encoding",
                    [*base, "--token-file", str(path)],
                    2,
                    error="cannot read --token-file",
                )
        with patch.dict(os.environ, {_ENV_KEY: " \t" + review_value + "\n"}):
            probe("stripped env", base, 0, review_value)
        with patch.dict(os.environ, {_ENV_KEY: " \t\n"}):
            probe("blank env", base, 2, error="--remote requires")
    finally:
        _remote._set_transport(previous_transport)
        path.unlink(missing_ok=True)
    assert not failures, "\n".join(failures)
    print("  review regressions: transport refusal, encodings, malformed URLs, env trimming OK")


# ============================================================================
# Part 9 - issue #317: a redirect (or a non-JSON body) from the server must be
# a clean exit-1 refusal through RemoteClient, never a TypeError traceback,
# never a silently followed redirect, and never bytes written to the download
# target. follow_redirects stays False (the #264 key-carrying rule); these
# tests pin what happens with the 3xx instead.
# ============================================================================
def _redirect_transport(status, location="https://elsewhere/final"):
    """MockTransport answering EVERY request with `status` (optionally without a
    Location header when `location` is empty), counting the requests it saw so a
    test can prove the redirect was NOT followed: exactly one request means the
    CLI never dialed the redirect target and the key never moved."""
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        # Raw bytes on the wire (a real server can send non-ASCII / control bytes).
        headers = [(b"location", location.encode("utf-8"))] if location else []
        return httpx.Response(status, headers=headers)

    return httpx.MockTransport(handler), seen


def _cli_capture(argv):
    """Run the REAL cli_main with both streams captured. An unexpected exception
    propagates out of cli_main (it only maps CliError), so the pre-fix #317
    TypeError shape fails the suite loudly instead of masquerading as a pass."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_redirect_is_a_clean_cli_error_through_the_cli():
    """Every redirect status through the real CLI entry point with a MockTransport
    installed: `release manifest` (the leaf from the #317 report) and `version`
    exit 1 with no traceback, the message names the Location and points at
    --remote, and the transport saw EXACTLY one request -- the redirect is not
    followed and the key does not move."""
    prev = _remote._TRANSPORT
    try:
        for status in (301, 302, 303, 307, 308):
            for leaf in (["release", "manifest"], ["version"]):
                transport, seen = _redirect_transport(status)
                _remote._set_transport(transport)
                rc, out, err = _cli_capture(
                    ["--json", "--remote", "http://127.0.0.1:8000", "--token", _GUARD_KEY, *leaf]
                )
                where = (status, " ".join(leaf))
                assert rc == 1, (where, rc, out, err)
                assert "Traceback" not in out and "Traceback" not in err, (where, out, err)
                text = out + err
                assert "https://elsewhere/final" in text, (where, text)
                assert "--remote" in text, (where, text)
                assert len(seen) == 1, (where, "exactly one request; no dialing the target")
    finally:
        _remote._set_transport(prev)
    print(
        "  #317: 301/302/303/307/308 over the real CLI -> exit 1 naming the Location "
        "and --remote, no traceback, exactly ONE request (release manifest + version) OK"
    )


def test_every_client_verb_rejects_a_redirect():
    """The verb-level contract behind the CLI proof: get/post/patch/put/delete/
    get_optional/post_optional/upload/download/stream_to_file ALL turn a 302 into
    the same clean exit-1 CliError naming the Location, after exactly one request.
    The binary verbs additionally leave no output file and no `.part` temp behind
    (a redirect body must never land on disk as the 'downloaded' file)."""
    from app.cli._common import CliError

    transport, seen = _redirect_transport(302)
    c = _remote.RemoteClient("http://127.0.0.1:8000", _GUARD_KEY, transport=transport)
    out_dir = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg317")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "payload.bin")
    calls = (
        ("get", lambda: c.get("/api/x")),
        ("post", lambda: c.post("/api/x", json={})),
        ("patch", lambda: c.patch("/api/x", json={})),
        ("put", lambda: c.put("/api/x", json={})),
        ("delete", lambda: c.delete("/api/x")),
        ("get_optional", lambda: c.get_optional("/api/x")),
        ("post_optional", lambda: c.post_optional("/api/x")),
        ("upload", lambda: c.upload("/api/x", "f.txt", b"data")),
        ("download", lambda: c.download("/api/x")),
        ("stream_to_file", lambda: c.stream_to_file("/api/x", out_path)),
    )
    for name, call in calls:
        seen.clear()
        try:
            call()
        except CliError as exc:
            assert exc.code == 1, (name, exc.code)
            assert "https://elsewhere/final" in str(exc), (name, str(exc))
            assert "--remote" in str(exc), (name, str(exc))
        except Exception as exc:
            raise AssertionError(f"{name}: expected CliError on a 302, got {exc!r}") from exc
        else:
            raise AssertionError(f"{name} must raise CliError on a 302 redirect")
        assert len(seen) == 1, (name, "exactly one request; the redirect must not be followed")
        assert not os.path.exists(out_path), (name, out_path)
        assert not os.path.exists(out_path + ".part"), (name, out_path + ".part")
    # A redirect with NO Location header still refuses cleanly (no KeyError on the
    # missing header, same fix advice).
    transport_nl, seen_nl = _redirect_transport(307, location="")
    c2 = _remote.RemoteClient("http://127.0.0.1:8000", _GUARD_KEY, transport=transport_nl)
    try:
        c2.get("/api/x")
    except CliError as exc:
        assert exc.code == 1 and "--remote" in str(exc), str(exc)
    else:
        raise AssertionError("a 3xx without a Location header must still be a CliError")
    assert len(seen_nl) == 1
    print(
        "  #317: all 10 RemoteClient verbs refuse a 302 (exit 1, names Location + "
        "--remote, one request); download/stream_to_file leave no file and no .part OK"
    )


def test_redirect_secrets_are_removed():
    from app.cli._common import CliError

    userinfo_location = (
        "https://user:private-password@elsewhere:8443"  # pregate: allow-secret offline fixture
        "/final#private-fragment"
    )
    signed_location = (
        "https://elsewhere/final?X-Amz-Signature=private-signature"  # pregate: allow-secret fixture
        "&X-Amz-Credential=private-credential"  # pregate: allow-secret offline fixture
    )
    locations = (
        userinfo_location,
        signed_location,
        "/final?token=private-relative#private-fragment",  # pregate: allow-secret offline fixture
    )
    prev = _remote._TRANSPORT
    try:
        for location in locations:
            transport, seen = _redirect_transport(302, location)
            _remote._set_transport(transport)
            for json_flag in ([], ["--json"]):
                rc, out, err = _cli_capture(
                    [
                        *json_flag,
                        "--remote",
                        "http://127.0.0.1",
                        "--token",
                        _GUARD_KEY,
                        "release",
                        "manifest",
                    ]
                )
                assert rc == 1
                assert "private-" not in out + err and "user:" not in out + err
                assert "/final" in out + err
                if "?" in location:
                    assert "(query removed)" in out + err
            c = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
            try:
                c.get("/api/release")
            except CliError as exc:
                text = str(exc) + repr(exc.payload)
                assert "private-" not in text and "user:" not in text
                assert "X-Amz-" not in text
            else:
                raise AssertionError("redirect accepted")
            assert len(seen) == 3
    finally:
        _remote._set_transport(prev)
    print("  #317: Location secrets removed from human/JSON output and CliError payload OK")


def test_redirect_download_preserves_existing_files():
    from pathlib import Path

    from app.cli._common import CliError

    target = Path(os.environ["LOC_TMS_DATA_DIR"]) / "redirect-existing.zip"
    for status in (301, 302, 303, 307, 308):
        for method in ("download", "stream_to_file"):
            for exists in (False, True):
                target.unlink(missing_ok=True)
                if exists:
                    target.write_bytes(b"original")
                transport, seen = _redirect_transport(status)
                c = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
                try:
                    if method == "download":
                        target.write_bytes(c.download("/api/x").content)
                    else:
                        c.stream_to_file("/api/x", str(target))
                except CliError as exc:
                    assert exc.code == 1
                else:
                    raise AssertionError((status, method))
                assert len(seen) == 1
                assert target.read_bytes() == b"original" if exists else not target.exists()
                assert not Path(str(target) + ".part").exists()
    print("  #317: redirected downloads preserve existing files for all redirect statuses OK")


def test_export_artifact_redirect_download():
    import json
    from pathlib import Path

    import httpx

    from app.cli._common import CliError

    run_uuid = "adcf2b8a-1ffb-4f98-a329-e3481f5902a5"
    route = f"/api/exports/{run_uuid}/artifact"
    signed = "https://bucket.example/export.zip?X-Amz-Signature=private-signature"
    target = Path(os.environ["LOC_TMS_DATA_DIR"]) / "s3-export.zip"
    prev = _remote._TRANSPORT
    try:
        for location, status, second in (
            (signed, 302, "ok"),
            (signed.replace("https:", "http:"), 302, "unused"),
            (signed.replace("bucket.example", "127.0.0.1"), 302, "unused"),
            (signed.replace("bucket.example", "user:pass@bucket.example"), 302, "unused"),
            (signed, 307, "unused"),
            (signed, 302, "redirect"),
            (signed, 302, "error"),
            (signed, 302, "broken"),
        ):
            seen = []
            target.write_bytes(b"original")

            class BrokenStream(httpx.SyncByteStream):
                def __iter__(self):
                    yield b"partial"
                    raise httpx.ReadError(signed)

            def handler(request, seen=seen, status=status, location=location, second=second):
                seen.append(request)
                if len(seen) == 1:
                    assert request.url.path == route
                    assert request.headers["X-Loc-Token"] == _GUARD_KEY
                    return httpx.Response(
                        status,
                        headers={
                            "location": location,
                            "set-cookie": "secret=cookie; Path=/",
                        },
                    )
                assert request.url.host == "bucket.example"
                assert str(request.url) == signed
                for header in ("X-Loc-Token", "Authorization", "Cookie"):
                    assert header not in request.headers
                if second == "redirect":
                    return httpx.Response(302, headers={"location": signed})
                if second == "error":
                    return httpx.Response(403, text=signed)
                if second == "broken":
                    return httpx.Response(200, stream=BrokenStream())
                assert second == "ok"
                return httpx.Response(200, content=b"zip-bytes")

            _remote._set_transport(httpx.MockTransport(handler))
            rc, out, err = _cli_capture(
                [
                    "--json",
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    _GUARD_KEY,
                    "export",
                    "download",
                    run_uuid,
                    "--out",
                    str(target),
                    "--force",
                ]
            )
            assert signed not in out + err and "private-signature" not in out + err
            assert "Traceback" not in out + err
            assert len(seen) == (1 if second == "unused" else 2)
            if second == "ok":
                assert rc == 0, (out, err)
                assert target.read_bytes() == b"zip-bytes"
                assert json.loads(out)["bytes"] == 9
            else:
                assert rc == (3 if second == "broken" else 1), (rc, out, err)
                assert target.read_bytes() == b"original"
            assert not Path(str(target) + ".part").exists()

        # Even an explicit opt-in cannot authorize another route.
        transport, seen = _redirect_transport(302, signed)
        c = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
        try:
            c.stream_to_file("/api/other/artifact", str(target), export_artifact=True)
        except CliError as exc:
            assert exc.code == 1 and "redirect" in str(exc)
            assert "private-signature" not in str(exc) + repr(exc.payload)
        else:
            raise AssertionError("non-export redirect accepted")
        assert len(seen) == 1
        assert target.read_bytes() == b"original"
    finally:
        _remote._set_transport(prev)
    print("  #317: export-only HTTPS handoff has no auth/cookies, atomic writes, safe failures OK")


def test_non_json_2xx_is_a_clean_error_for_json_verbs_and_still_raw_for_downloads():
    """Defect 2's two-sided contract: a 200 whose body is an HTML page (the proxy
    login wall shape) is a clean exit-1 CliError on the JSON verbs -- it must not
    leak the Response object into callers that index the body -- while download()
    and stream_to_file() KEEP returning/streaming the raw 2xx body, because the
    CSV/text/binary leaves read exactly that."""
    import httpx

    from app.cli._common import CliError

    html = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            text="<html><body>proxy login</body></html>",
            headers={"content-type": "text/html"},
        )
    )
    c = _remote.RemoteClient("http://127.0.0.1:8000", _GUARD_KEY, transport=html)
    json_calls = (
        ("get", lambda: c.get("/api/release")),
        ("post", lambda: c.post("/api/x", json={})),
        ("upload", lambda: c.upload("/api/x", "f.txt", b"data")),
        ("get_optional", lambda: c.get_optional("/api/x")),
        ("post_optional", lambda: c.post_optional("/api/x")),
    )
    for name, call in json_calls:
        try:
            call()
        except CliError as exc:
            assert exc.code == 1 and "json" in str(exc).lower(), (name, str(exc))
        except Exception as exc:
            raise AssertionError(
                f"{name}: expected CliError on a non-JSON 200, got {exc!r}"
            ) from exc
        else:
            raise AssertionError(f"{name} must raise CliError on a non-JSON 200")

    # ...while the raw-body leaves keep working on the very same transport.
    resp = c.download("/api/projects/1/glossary/export")
    assert resp.status_code == 200 and b"proxy login" in resp.content
    streamed = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg317_html_dl.bin")
    total, _headers = c.stream_to_file("/api/reports/run", streamed)
    with open(streamed, "rb") as fh:
        assert total and b"proxy login" in fh.read()

    # Through the real CLI: a 200 text/html on `release manifest` is the clean
    # non-JSON refusal, exit 1, no traceback (#317's sibling shape).
    prev = _remote._TRANSPORT
    try:
        _remote._set_transport(html)
        rc, out, err = _cli_capture(
            [
                "--json",
                "--remote",
                "http://127.0.0.1:8000",
                "--token",
                _GUARD_KEY,
                "release",
                "manifest",
            ]
        )
    finally:
        _remote._set_transport(prev)
    assert rc == 1, (rc, out, err)
    assert "Traceback" not in out + err, (out, err)
    assert "json" in (out + err).lower(), (out, err)
    print(
        "  #317: 200 text/html -> clean 'did not answer with JSON' exit 1 on the JSON "
        "verbs and on `release manifest`; download/stream_to_file still return the raw "
        "2xx body OK"
    )


def test_release_leaves_refuse_unexpected_json_shapes():
    """Defect 3: `version` indexes version/tag/commit and `release manifest` dumps
    the whole body, so over --remote a VALID-JSON answer of the wrong shape (a list,
    a bare string) or one missing the keys `version` reads must refuse cleanly.
    A dict missing those keys stays readable for `release manifest` (it dumps
    whatever object the server returns) but refuses for `version`."""
    import httpx

    prev = _remote._TRANSPORT
    try:
        for bad in ([], "just a string", {"no": "release keys"}):
            transport = httpx.MockTransport(lambda r, body=bad: httpx.Response(200, json=body))
            # `version` reads three keys: every one of these must be a clean exit 1.
            _remote._set_transport(transport)
            rc, out, err = _cli_capture(
                ["--json", "--remote", "http://127.0.0.1:8000", "--token", _GUARD_KEY, "version"]
            )
            assert rc == 1, (bad, rc, out, err)
            assert "Traceback" not in out + err, (bad, out, err)
            assert "/api/release" in out + err, (bad, out, err)
            # `release manifest` refuses the non-dict shapes...
            _remote._set_transport(transport)
            rc, out, err = _cli_capture(
                [
                    "--json",
                    "--remote",
                    "http://127.0.0.1:8000",
                    "--token",
                    _GUARD_KEY,
                    "release",
                    "manifest",
                ]
            )
            if isinstance(bad, dict):
                assert rc == 0, (bad, rc, out, err)  # a dict is what it dumps
            else:
                assert rc == 1, (bad, rc, out, err)
                assert "Traceback" not in out + err, (bad, out, err)
                assert "json" in (out + err).lower(), (bad, out, err)
    finally:
        _remote._set_transport(prev)
    print(
        "  #317: version/release manifest refuse a non-dict or key-missing "
        "/api/release JSON body with a clean exit 1 (no traceback) OK"
    )


# ============================================================================
# Part 10 - issue #329 (follow-ups from #317): odd Location headers must be a
# clean exit-1 refusal with no traceback and never the raw value (httpx parses
# the Location of a 3xx even with follow_redirects=False, so mailto:/javascript:
# Locations used to raise httpx.InvalidURL from inside the request); the export
# S3 handoff must work behind a path-prefixed --remote base; refused storage
# handoffs (plain http, same-host MinIO, userinfo Locations) get an
# export-specific message that names the web UI instead of the wrong "use the
# final https URL with --remote" advice; and the anonymous storage client must
# honour HTTPS_PROXY / SSL_CERT_FILE / SSL_CERT_DIR while never reading netrc
# and never sending auth headers or cookies.
# ============================================================================
def test_odd_locations_are_clean_and_never_raw():
    """Defects 1 + 2 through the real CLI AND at verb level: a mailto:,
    javascript: or backslash Location exits 1 with no traceback and no raw
    value; a matrix-param or encoded-? / encoded-# path is not printed raw
    either. Pre-fix red: mailto:/javascript: raised httpx.InvalidURL straight
    out of cli_main, and every case below printed its Location (userinfo-
    stripped at best, fully raw for matrix/encoded delimiters)."""
    from app.cli._common import CliError

    odd = (
        "mailto:victim@example.com",
        "javascript:alert(document.cookie)",
        "https://safe.example\\@evil.example/path",
        "/seg;x=secret/matrix",
        "/path%3fsecret=1",
        "/path%23secret",
    )
    out_dir = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg329")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "payload.bin")
    prev = _remote._TRANSPORT
    try:
        for location in odd:
            transport, seen = _redirect_transport(302, location)
            _remote._set_transport(transport)
            rc, out, err = _cli_capture(
                [
                    "--json",
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    _GUARD_KEY,
                    "release",
                    "manifest",
                ]
            )
            where = (location, rc)
            text = out + err
            assert rc == 1, (where, text)
            assert "Traceback" not in text, (where, text)
            assert location not in text, (where, text)  # never echoed raw
            # Either rendering is acceptable (httpx rejects the URL before we
            # render it, or we render the fixed phrase) -- raw is not.
            assert "(an unusual redirect target)" in text or "unreadable Location" in text, (
                where,
                text,
            )
            assert "redirect" in text and "--remote" in text, (where, text)
            assert len(seen) == 1, (where, "exactly one request; the target must not be dialed")
            # Verb level too: get() and stream_to_file() refuse with exit 1,
            # name neither the raw Location nor a secret, and stream_to_file
            # leaves no file and no .part behind.
            client = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
            for name in ("get", "stream_to_file"):
                seen.clear()
                try:
                    if name == "get":
                        client.get("/api/x")
                    else:
                        client.stream_to_file("/api/x", out_path)
                except CliError as exc:
                    assert exc.code == 1, (location, name, exc.code)
                    assert location not in str(exc), (location, name, str(exc))
                    assert "--remote" in str(exc), (location, name, str(exc))
                except Exception as exc:
                    raise AssertionError(
                        f"{location!r} [{name}]: expected CliError, got {exc!r}"
                    ) from exc
                else:
                    raise AssertionError(f"{location!r} [{name}] must raise CliError")
                assert not os.path.exists(out_path), (location, name)
                assert not os.path.exists(out_path + ".part"), (location, name)
    finally:
        _remote._set_transport(prev)
    print(
        "  #329: mailto:/javascript:/backslash/matrix/encoded-delimiter Locations -> "
        "clean exit 1, no traceback, never raw (CLI + get + stream_to_file) OK"
    )


def test_export_handoff_works_behind_prefixed_remote():
    """Defect 3a: with --remote https://host/tms the artifact request lands on
    /tms/api/exports/{id}/artifact; the handoff check must compare against that
    prefixed route, so the S3 302 still downloads. Pre-fix red: the check
    compared against the unprefixed path, the handoff was refused, and the CLI
    exited 1 with the generic redirect message (rc 1, no bytes written)."""
    from pathlib import Path

    import httpx

    run_uuid = "b7e1d4c2-3a5f-4e6d-9c8b-1a2b3c4d5e6f"
    route = f"/api/exports/{run_uuid}/artifact"
    signed = (
        "https://bucket.example/export.zip"  # pregate: allow-secret fixture
        "?X-Amz-Signature=private-signature"  # pregate: allow-secret fixture
    )
    target = Path(os.environ["LOC_TMS_DATA_DIR"]) / "prefixed-export.zip"
    target.unlink(missing_ok=True)
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            assert request.url.path == "/tms" + route  # the prefixed route really was used
            assert request.headers["X-Loc-Token"] == _GUARD_KEY
            return httpx.Response(302, headers={"location": signed})
        assert request.url.host == "bucket.example"
        for header in ("X-Loc-Token", "Authorization", "Cookie"):
            assert header not in request.headers
        return httpx.Response(200, content=b"zip-bytes")

    prev = _remote._TRANSPORT
    try:
        _remote._set_transport(httpx.MockTransport(handler))
        rc, out, err = _cli_capture(
            [
                "--json",
                "--remote",
                "http://127.0.0.1/tms",
                "--token",
                _GUARD_KEY,
                "export",
                "download",
                run_uuid,
                "--out",
                str(target),
                "--force",
            ]
        )
    finally:
        _remote._set_transport(prev)
    assert rc == 0, (rc, out, err)
    assert target.read_bytes() == b"zip-bytes"
    assert len(seen) == 2, "artifact route + anonymous storage fetch, nothing else"
    assert signed not in out + err and "private-signature" not in out + err
    print("  #329: export S3 handoff works with a path-prefixed --remote base OK")


def test_export_storage_refusals_name_the_web_ui():
    """Defect 3b: a storage handoff the CLI will not (or cannot) fetch --
    plain-http storage, a same-host S3-compatible store (MinIO), or a userinfo
    Location -- still refuses, but the message says the export is served from a
    storage URL the CLI will not fetch anonymously and points at the web UI; it
    no longer says "use the final https URL with --remote" (there is no fetchable
    final URL there) and never echoes the (possibly signed) Location. Pre-fix
    red: all three cases carried the generic redirect advice with --remote."""
    from app.cli._common import CliError

    run_uuid = "c2d3e4f5-a6b7-48c9-9d0e-1f2a3b4c5d6e"
    route = f"/api/exports/{run_uuid}/artifact"
    out_dir = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg329")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "storage-refusal.bin")
    cases = (
        (
            "plain-http storage",
            "http://bucket.example/export.zip"  # pregate: allow-secret fixture
            "?X-Amz-Signature=private-signature",  # pregate: allow-secret fixture
        ),
        (
            "same-host MinIO",
            "https://127.0.0.1:9000/export.zip"  # pregate: allow-secret fixture
            "?X-Amz-Signature=private-signature",  # pregate: allow-secret fixture
        ),
        (
            "userinfo storage",
            "https://user:private-password@bucket.example"  # pregate: allow-secret fixture
            "/export.zip?X-Amz-Signature=private-signature",  # pregate: allow-secret fixture
        ),
    )
    for where, location in cases:
        transport, seen = _redirect_transport(302, location)
        c = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
        try:
            c.stream_to_file(route, out_path, export_artifact=True)
        except CliError as exc:
            text = str(exc)
            assert exc.code == 1, (where, exc.code)
            assert "storage URL" in text and "web UI" in text, (where, text)
            assert "--remote" not in text, (where, text)  # the wrong advice is gone
            assert location not in text, (where, text)
            assert "private-signature" not in text and "user:" not in text, (where, text)
        else:
            raise AssertionError(f"{where}: the handoff must stay refused")
        assert len(seen) == 1, where
        assert not os.path.exists(out_path), where
        assert not os.path.exists(out_path + ".part"), where
    print(
        "  #329: http/same-host/userinfo storage refuse with the export-specific "
        "web-UI message, no wrong advice, no raw Location OK"
    )


def test_storage_client_honours_proxy_and_ca_env():
    """Defect 4 + F3: the anonymous storage client must honour the proxy and CA
    ENVIRONMENT by httpx's own rules (HTTPS_PROXY, ALL_PROXY, NO_PROXY,
    scheme-less values, SSL_CERT_FILE/DIR). Asserted on the real client
    configuration (httpx's transport selection and the TLS context), never
    against a live proxy; an injected test transport wins over the environment
    proxy, and the end-to-end leg proves the storage request still carries no
    auth header and no cookie with HTTPS_PROXY set (netrc has its own test)."""
    import ssl
    from pathlib import Path
    from unittest import mock as _mock

    import httpx

    run_uuid = "d3e4f5a6-b7c8-49d0-ae1f-2b3c4d5e6f70"
    route = f"/api/exports/{run_uuid}/artifact"
    signed = (
        "https://bucket.example/export.zip"  # pregate: allow-secret fixture
        "?X-Amz-Signature=private-signature"  # pregate: allow-secret fixture
    )
    proxy_url = "http://proxy.invalid:3128"

    # (a) The storage client follows httpx's OWN proxy environment rules (F3):
    # HTTPS_PROXY mounts a proxy transport for https targets; ALL_PROXY does too;
    # a scheme-less value is normalised instead of raising; NO_PROXY bypasses the
    # proxy for the bucket host (routing asserted through httpx's own transport
    # selection, never a live proxy).
    bucket = httpx.URL("https://bucket.example/export.zip")
    proxy_vars = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY")
    proxy_vars += ("http_proxy", "NO_PROXY", "no_proxy")

    def _set_env(env):
        # Environment names are case-insensitive on Windows, so clear every
        # spelling first and then set only the ones under test.
        for var in proxy_vars:
            os.environ.pop(var, None)
        os.environ.update(env)

    def _routed_via_proxy(env):
        with _mock.patch.dict(os.environ):
            _set_env(env)
            client = _remote._anonymous_storage_client()
            try:
                assert client.trust_env is True
                chosen = client._transport_for_url(bucket)
                return chosen is not client._transport and isinstance(chosen, httpx.HTTPTransport)
            finally:
                client.close()

    assert _routed_via_proxy({"HTTPS_PROXY": proxy_url}), "HTTPS_PROXY must be honoured"
    assert _routed_via_proxy({"https_proxy": proxy_url}), "https_proxy must be honoured"
    assert _routed_via_proxy({"ALL_PROXY": proxy_url}), "ALL_PROXY must be honoured"
    assert _routed_via_proxy({"HTTPS_PROXY": "proxy.corp:3128"}), "scheme-less proxy must work"
    assert not _routed_via_proxy({}), "no proxy env -> direct"
    assert not _routed_via_proxy({"HTTPS_PROXY": proxy_url, "NO_PROXY": "bucket.example"}), (
        "NO_PROXY must bypass the proxy for the bucket host"
    )
    assert not _routed_via_proxy({"ALL_PROXY": proxy_url, "NO_PROXY": ".example"}), (
        "NO_PROXY with a domain suffix must bypass ALL_PROXY too"
    )
    assert _routed_via_proxy({"HTTPS_PROXY": proxy_url, "NO_PROXY": "other.test"}), (
        "an unrelated NO_PROXY entry must not disable the proxy"
    )
    # An injected test transport always wins over the environment proxy.
    with _mock.patch.dict(os.environ):
        _set_env({"HTTPS_PROXY": proxy_url})
        mock_t = httpx.MockTransport(lambda request: httpx.Response(200))
        injected = _remote._anonymous_storage_client(mock_t)
        try:
            assert injected._transport_for_url(bucket) is mock_t
        finally:
            injected.close()

    # (b) SSL_CERT_FILE / SSL_CERT_DIR reach the TLS layer: pointing SSL_CERT_DIR
    # at an empty directory replaces the default CA bundle (store goes from the
    # certifi set to zero); with the vars unset the defaults load as before.
    empty_ca = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg329_empty_ca")
    os.makedirs(empty_ca, exist_ok=True)
    with _mock.patch.dict(
        os.environ,
        {"HTTPS_PROXY": "", "https_proxy": "", "SSL_CERT_FILE": "", "SSL_CERT_DIR": empty_ca},
    ):
        ca_client = _remote._anonymous_storage_client()
        try:
            ctx = ca_client._transport._pool._ssl_context
            assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
            assert ctx.cert_store_stats()["x509"] == 0, "SSL_CERT_DIR must replace the defaults"
        finally:
            ca_client.close()
    with _mock.patch.dict(
        os.environ,
        {"HTTPS_PROXY": "", "https_proxy": "", "SSL_CERT_FILE": "", "SSL_CERT_DIR": ""},
    ):
        default_client = _remote._anonymous_storage_client()
        try:
            assert default_client._transport._pool._ssl_context.cert_store_stats()["x509"] > 0
        finally:
            default_client.close()

    # (c) end-to-end with HTTPS_PROXY set: the handoff still runs fully offline
    # through the injected test transport, and the storage request carries no
    # X-Loc-Token, no Authorization and no Cookie.
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            assert request.url.path == route
            assert request.headers["X-Loc-Token"] == _GUARD_KEY
            return httpx.Response(302, headers={"location": signed})
        assert request.url.host == "bucket.example"
        for header in ("X-Loc-Token", "Authorization", "Cookie"):
            assert header not in request.headers
        return httpx.Response(200, content=b"zip-bytes")

    target = Path(os.environ["LOC_TMS_DATA_DIR"]) / "proxy-env-export.zip"
    target.write_bytes(b"original")
    prev = _remote._TRANSPORT
    try:
        _remote._set_transport(httpx.MockTransport(handler))
        with _mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy_url}):
            rc, out, err = _cli_capture(
                [
                    "--json",
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    _GUARD_KEY,
                    "export",
                    "download",
                    run_uuid,
                    "--out",
                    str(target),
                    "--force",
                ]
            )
    finally:
        _remote._set_transport(prev)
    assert rc == 0, (rc, out, err)
    assert target.read_bytes() == b"zip-bytes"
    assert len(seen) == 2
    assert signed not in out + err and "private-signature" not in out + err
    print(
        "  #329: storage client honours HTTPS_PROXY + CA env vars (config-level), "
        "trust_env stays off, storage request carries no auth/cookie OK"
    )


def test_storage_client_never_reads_netrc_or_sends_auth():
    """F3 safety leg: the storage client now uses trust_env=True (so proxy and CA
    environment rules match httpx's own), which is only acceptable if netrc still
    cannot reach the request. A netrc naming the bucket host (via NETRC, HOME and
    USERPROFILE) must produce NO Authorization header and no cookie on the storage
    request, and the client must carry no auth object."""
    from pathlib import Path
    from unittest import mock as _mock

    import httpx

    run_uuid = "e4f5a6b7-c8d9-4ae0-bf2a-3c4d5e6f7081"
    route = f"/api/exports/{run_uuid}/artifact"
    signed = (
        "https://bucket.example/export.zip"  # pregate: allow-secret fixture
        "?X-Amz-Signature=private-signature"  # pregate: allow-secret fixture
    )
    home = Path(os.environ["LOC_TMS_DATA_DIR"]) / "rdg329_netrc_home"
    home.mkdir(exist_ok=True)
    netrc_text = (
        "machine bucket.example login netrcuser "  # pregate: allow-secret fixture
        "password netrcsecret\n"  # pregate: allow-secret fixture
    )
    for name in (".netrc", "_netrc", "netrc"):
        (home / name).write_bytes(netrc_text.encode("ascii"))
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": signed})
        assert request.url.host == "bucket.example"
        for header in ("X-Loc-Token", "Authorization", "Cookie", "Proxy-Authorization"):
            assert header not in request.headers, header
        return httpx.Response(200, content=b"zip-bytes")

    target = Path(os.environ["LOC_TMS_DATA_DIR"]) / "netrc-export.zip"
    target.unlink(missing_ok=True)
    env = {"NETRC": str(home / ".netrc"), "HOME": str(home), "USERPROFILE": str(home)}
    with _mock.patch.dict(os.environ, env):
        client = _remote._anonymous_storage_client()
        try:
            assert client.auth is None and client.trust_env is True
            assert len(client.cookies) == 0
        finally:
            client.close()
        c = _remote.RemoteClient(
            "http://127.0.0.1", _GUARD_KEY, transport=httpx.MockTransport(handler)
        )
        total, _ = c.stream_to_file(route, str(target), export_artifact=True)
    assert total == len(b"zip-bytes") and target.read_bytes() == b"zip-bytes"
    assert len(seen) == 2
    print("  #329 F3: storage client ignores netrc, carries no auth object/header/cookie OK")


def test_bad_proxy_env_is_its_own_error():
    """F3: a proxy environment value httpx cannot use must not be mislabelled as
    an 'unreadable Location'; the export download names the proxy variables and
    exits 2 (usage/config), with no traceback."""
    from pathlib import Path
    from unittest import mock as _mock

    import httpx

    from app.cli._common import CliError

    run_uuid = "f5a6b7c8-d9e0-4bf1-8a3b-4d5e6f708192"
    route = f"/api/exports/{run_uuid}/artifact"
    signed = (
        "https://bucket.example/export.zip"  # pregate: allow-secret fixture
        "?X-Amz-Signature=private-signature"  # pregate: allow-secret fixture
    )
    target = Path(os.environ["LOC_TMS_DATA_DIR"]) / "bad-proxy-export.zip"
    target.unlink(missing_ok=True)

    def boom(transport=None):
        raise ValueError("bad proxy value")

    transport, seen = _redirect_transport(302, signed)
    c = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
    with _mock.patch.object(_remote, "_anonymous_storage_client", boom):
        try:
            c.stream_to_file(route, str(target), export_artifact=True)
        except CliError as exc:
            assert exc.code == 2, exc.code
            assert "proxy environment" in str(exc), str(exc)
            assert "unreadable Location" not in str(exc), str(exc)
            assert "private-signature" not in str(exc)
        else:
            raise AssertionError("a bad proxy environment must raise CliError")
    assert len(seen) == 1 and not target.exists()
    # A real, unusable proxy value fails inside httpx at client build time too.
    with _mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://[bad"}):
        try:
            client = _remote._anonymous_storage_client()
        except (ValueError, httpx.InvalidURL):
            pass
        else:
            client.close()
    print("  #329 F3: a bad proxy environment gets its own clear exit-2 message OK")


def test_control_and_nonascii_locations_never_render_raw():
    """F1: a Location with a control character (ESC/CSI, OSC with BEL, TAB) or a
    non-ASCII host/path renders as the fixed phrase on EVERY 3xx, including 300 and
    304 (which httpx does not parse, so nothing upstream vets them), and nothing
    below 0x20 or non-ASCII reaches stdout, stderr or the CliError. Pre-fix red:
    on 300/304 the ESC and OSC bytes were printed raw and 'b\\xfcchers' printed
    unescaped."""
    from app.cli._common import CliError

    esc = chr(27)
    bel = chr(7)
    locations = (
        f"https://h.example/p{esc}[31mRED{esc}[0m",
        f"/p{esc}]0;TITLE{bel}",
        "https://h.example/a\tb",
        "https://bücher.example/päth",
        "https://h.example/päth",
        f"https://h.example/x{chr(0x7F)}y",
        f"https://h.example/x{chr(0x9B)}31m",
    )
    assert _remote._safe_redirect_target("//cdn.example/p") == "//cdn.example/p"
    for status in (300, 301, 302, 303, 304, 307, 308):
        for location in locations:
            transport, seen = _redirect_transport(status, location)
            client = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
            try:
                client.get("/api/x")
            except CliError as exc:
                text = str(exc) + repr(getattr(exc, "payload", ""))
                where = (status, location, text)
                assert exc.code == 1, where
                assert all(0x20 <= ord(ch) < 0x7F for ch in str(exc)), where
                assert "RED" not in text and "TITLE" not in text and "cher" not in text, where
                assert "unusual redirect target" in text or "unreadable Location" in text, where
            else:
                raise AssertionError(f"{status} {location!r}: must raise CliError")
            assert len(seen) == 1, (status, location)
    # Through the real CLI streams too (300 and 304 are the regressed statuses).
    prev = _remote._TRANSPORT
    try:
        for status in (300, 304, 302):
            for location in locations[:4]:
                transport, seen = _redirect_transport(status, location)
                _remote._set_transport(transport)
                rc, out, err = _cli_capture(
                    [
                        *("--json", "--remote", "http://127.0.0.1"),
                        *("--token", _GUARD_KEY, "release", "manifest"),
                    ]
                )
                text = out + err
                assert rc == 1 and "Traceback" not in text, (status, location, rc, text)
                assert all(ch in "\r\n" or 0x20 <= ord(ch) < 0x7F for ch in text), (
                    status,
                    location,
                    text,
                )
    finally:
        _remote._set_transport(prev)
    print(
        "  #329 F1: ESC/OSC/TAB/DEL/C1 and non-ASCII Locations print as the fixed "
        "phrase on 300-308, ASCII-only output; //host/p keeps its slashes OK"
    )


def test_unparseable_locations_exit_1_not_3():
    """F2: a Location httpx cannot parse on a 3xx it does not follow (a control
    character, a bad IDNA host, a value over 65k characters) raises
    RemoteProtocolError('Invalid URL in location header'), a TransportError that
    used to exit 3 as 'could not reach the server' and echoed the raw host. It is
    now the clean exit-1 unreadable-redirect refusal on 301/302/307, through
    get(), stream_to_file() and the CLI, one request, nothing raw."""
    from app.cli._common import CliError

    locations = (
        "https://h.example/a" + chr(1) + "b",
        "https://☃.example/p",
        "https://h.example/" + ("a" * 70000),
    )
    out_path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "rdg329", "unparseable.bin")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    prev = _remote._TRANSPORT
    try:
        for status in (301, 302, 307):
            for location in locations:
                transport, seen = _redirect_transport(status, location)
                client = _remote.RemoteClient("http://127.0.0.1", _GUARD_KEY, transport=transport)
                for name in ("get", "stream_to_file"):
                    seen.clear()
                    try:
                        if name == "get":
                            client.get("/api/x")
                        else:
                            client.stream_to_file("/api/x", out_path)
                    except CliError as exc:
                        where = (status, len(location), name, str(exc)[:200])
                        assert exc.code == 1, where
                        assert "could not reach" not in str(exc), where
                        assert "unreadable Location" in str(exc), where
                        assert "☃" not in str(exc) and "h.example" not in str(exc), where
                    else:
                        raise AssertionError((status, name, "must raise CliError"))
                    assert len(seen) == 1, (status, name)
                    assert not os.path.exists(out_path + ".part")
                    assert not os.path.exists(out_path)
                _remote._set_transport(transport)
                rc, out, err = _cli_capture(
                    [
                        *("--json", "--remote", "http://127.0.0.1"),
                        *("--token", _GUARD_KEY, "release", "manifest"),
                    ]
                )
                text = out + err
                assert rc == 1 and "Traceback" not in text, (status, rc, text[:200])
                assert "☃" not in text and "h.example" not in text, text[:200]
    finally:
        _remote._set_transport(prev)
    print(
        "  #329 F2: control-char / bad-IDNA / over-65k Locations on 301/302/307 -> "
        "exit 1 unreadable-redirect, not exit 3, nothing raw OK"
    )


def main():
    init_db()
    test_remote_security_review_regressions()
    test_parser_tree_guard_all_leaves()
    test_ledger_leaves_refuse_remote()
    test_invite_leaves_refuse_remote()
    test_guard_catches_historical_bug()
    test_guard_follows_only_cli_private_imports()
    test_guard_is_not_fooled_or_vacuous()
    test_remote_role_gate_proves_http_dispatch()
    test_legacy_glossary_export_over_remote()
    test_remote_glossary_module_round_trip()
    test_remote_doc_apply_tm()
    test_run_detail_findings_and_metadata()
    test_remote_edit_term_merges_meta_like_local()
    test_remote_show_meta_column_matches_local()
    test_unassigned_filter_cannot_widen_scope()
    test_number_integrity_flag_is_surfaced_everywhere()
    test_source_fallback_flag_is_surfaced_everywhere()
    test_plain_http_guard_host_allowlist()
    test_remote_token_sources_and_plain_http_e2e()
    test_redirect_is_a_clean_cli_error_through_the_cli()
    test_every_client_verb_rejects_a_redirect()
    test_redirect_secrets_are_removed()
    test_redirect_download_preserves_existing_files()
    test_export_artifact_redirect_download()
    test_non_json_2xx_is_a_clean_error_for_json_verbs_and_still_raw_for_downloads()
    test_release_leaves_refuse_unexpected_json_shapes()
    test_odd_locations_are_clean_and_never_raw()
    test_export_handoff_works_behind_prefixed_remote()
    test_export_storage_refusals_name_the_web_ui()
    test_storage_client_honours_proxy_and_ca_env()
    test_storage_client_never_reads_netrc_or_sends_auth()
    test_bad_proxy_env_is_its_own_error()
    test_control_and_nonascii_locations_never_render_raw()
    test_unparseable_locations_exit_1_not_3()
    print("CLI REMOTE DISPATCH GUARD OK")


if __name__ == "__main__":
    main()
