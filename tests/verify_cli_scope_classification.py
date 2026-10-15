"""CLI: every verb is classified for multi-org scope, and one central gate enforces it (#556).

#515 fixed the unscoped CLI verbs one at a time. This suite pins the structural fix in
app/cli/_scope.py + app/cli/__main__.py::_central_scope_gate:

  [1] every leaf of the real argparse tree is classified exactly once (org-scoped, operator-only
      or install-wide), the table names no verb the parser lacks, and a NEW unclassified verb is
      detected (the recurrence guard: it must fail the day someone adds a verb without a class).
  [2] multi-org ON, in-process, no --org and no --operator: every org-scoped and operator-only
      verb is refused by the central gate (exit 2, before the verb body runs); install-wide
      verbs are not touched by it. --org does not unlock an operator-only verb.
  [3] --org <id> satisfies the gate for org-scoped verbs, and the key-verified --operator door
      satisfies both classes.
  [4] `doc show` honours the --org scope (a foreign org's document reads as not found) and
      `admin tick` is operator-only in-process.
  [5] gate OFF (every real install) and --remote: the central gate does nothing.

Multi-org stays OFF on every real install; this suite only turns it on in a throwaway data dir.
"""

import argparse
import hashlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cli_scope_")

from sqlmodel import Session

from app.cli import _scope
from app.cli.__main__ import _central_scope_gate, build_parser
from app.cli._common import CliError, Emitter
from app.db import engine, init_db
from app.models import Customer, Document, Organization, Project
from app.services import access, settings
from tests._cli_runner import run_cli

OP_SECRET = "cliscope-operator-door-value-123"  # pregate: allow-secret throwaway operator key

FIX = {}
_MARK = "multi-org mode is ON"


def _fixture():
    init_db()
    with Session(engine) as s:
        for key in ("a", "b"):
            org = Organization(name=f"Org {key} cliscope")
            s.add(org)
            s.commit()
            s.refresh(org)
            FIX[f"org_{key}"] = org.id
            cust = Customer(name=f"Cust {key}", org_id=org.id)
            s.add(cust)
            s.commit()
            s.refresh(cust)
            proj = Project(
                name=f"Proj {key}", customer_id=cust.id, org_id=org.id, source_locale="en-US"
            )
            s.add(proj)
            s.commit()
            s.refresh(proj)
            doc = Document(project_id=proj.id, filename=f"d_{key}.md", fmt="md")
            s.add(doc)
            s.commit()
            s.refresh(doc)
            FIX[f"doc_{key}"] = doc.id
    settings.set_many(
        {
            "security_auth_required": "1",
            "operator_key_hash": hashlib.sha256(OP_SECRET.encode()).hexdigest(),
        }
    )


def _set_gate(on: bool):
    # THROWAWAY data dir only (init_env): multi-org is never enabled on a real install.
    assert "loctms_cli_scope_" in os.environ["LOC_TMS_DATA_DIR"]
    settings.set_many({"multi_org_enabled": "1" if on else "0"})
    assert access.multi_org_active() is on


def _value_for(act) -> str:
    return str((act.choices and sorted(act.choices)[0]) or "1")


def _argv_for(leaf: argparse.ArgumentParser, path) -> list:
    """A syntactically valid argv for the leaf: a placeholder for each required positional,
    required option and required mutually-exclusive group. Placeholders only need to PARSE; the
    central gate runs before any verb body."""
    argv = list(path)
    given = set()
    for act in leaf._actions:
        if isinstance(act, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if act.option_strings:
            if not act.required:
                continue
            argv.append(act.option_strings[-1])
            given.add(act.dest)
            if act.nargs == 0:
                continue
        elif act.nargs in ("?", "*"):
            continue
        else:
            given.add(act.dest)
        argv.append(_value_for(act))
    for group in leaf._mutually_exclusive_groups:
        if not group.required or any(a.dest in given for a in group._group_actions):
            continue
        act = group._group_actions[0]
        if act.option_strings:
            argv.append(act.option_strings[-1])
            if act.nargs != 0:
                argv.append(_value_for(act))
        else:
            argv.append(_value_for(act))
        given.add(act.dest)
    return argv


_PARSER = build_parser()
_EMIT = Emitter(as_json=False, quiet=True, verbose=False)


def _leaves():
    return [(path, leaf) for path, leaf in _scope.iter_leaves(_PARSER) if path]


def _parse(extra, path, leaf):
    """Parse the verb with placeholder arguments, optionally behind global flags."""
    return _PARSER.parse_args([*extra, *_argv_for(leaf, path)])


def _run_gate(extra, path, leaf):
    args = _parse(extra, path, leaf)
    _central_scope_gate(args, _EMIT)
    return args


def test_1_every_verb_is_classified():
    parser = build_parser()
    assert _scope.unclassified(parser) == [], _scope.unclassified(parser)
    assert _scope.stale(parser) == [], _scope.stale(parser)
    sets = (_scope._ORG_SCOPED_VERBS, _scope._OPERATOR_ONLY_VERBS, _scope._INSTALL_WIDE_VERBS)
    assert sum(len(s) for s in sets) == len(_scope.VERB_SCOPE), "a verb is in two classes"
    assert set(_scope.VERB_SCOPE.values()) == {
        _scope.ORG_SCOPED,
        _scope.OPERATOR_ONLY,
        _scope.INSTALL_WIDE,
    }
    # The recurrence guard: a verb added to the tree without a class is reported.
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    new = sub.add_parser("brand-new-verb")
    new.set_defaults(func=lambda a, e: 0)
    assert "brand-new-verb" in _scope.unclassified(parser)
    # ... and a deleted verb leaves a stale entry.
    del sub.choices["brand-new-verb"]
    assert _scope.unclassified(parser) == []
    # Spot checks that the classes mean what they say.
    assert _scope.VERB_SCOPE["project create"] == _scope.ORG_SCOPED
    assert _scope.VERB_SCOPE["user org"] == _scope.OPERATOR_ONLY
    assert _scope.VERB_SCOPE["admin tick"] == _scope.OPERATOR_ONLY
    assert _scope.VERB_SCOPE["db backup"] == _scope.INSTALL_WIDE
    assert _scope.scope_of("not a verb") == _scope.ORG_SCOPED, "unknown must fail closed"
    counts = {
        k: sum(1 for v in _scope.VERB_SCOPE.values() if v == k)
        for k in set(_scope.VERB_SCOPE.values())
    }
    print(f"  [1] all {len(_scope.VERB_SCOPE)} verbs classified once: {counts}")


def test_2_gate_refuses_unscoped_in_process():
    _set_gate(True)
    refused = 0
    for path, leaf in _leaves():
        verb = " ".join(path)
        cls = _scope.VERB_SCOPE[verb]
        if cls == _scope.INSTALL_WIDE:
            continue
        try:
            _run_gate([], path, leaf)
        except CliError as exc:
            assert exc.code == 2 and _MARK in str(exc), (verb, cls, exc.code, str(exc)[:200])
            refused += 1
        else:
            raise AssertionError(f"{verb} ({cls}) ran with no scope under multi-org")
        if cls == _scope.OPERATOR_ONLY:
            # --org cannot scope an operator-only verb.
            try:
                _run_gate(["--org", str(FIX["org_a"])], path, leaf)
            except CliError as exc:
                assert exc.code == 2 and "--org cannot" in str(exc), (verb, str(exc)[:200])
            else:
                raise AssertionError(f"--org unlocked the operator-only verb {verb}")
    assert refused > 300, refused
    # End to end through main(): the gate really sits in the dispatcher, ahead of the verb body
    # (a handful of verbs, one per group shape: each full run builds the whole parser).
    for argv in (
        ["project", "list"],
        ["user", "list"],
        ["engine", "list"],
        ["org", "list"],
        ["admin", "tick"],
        ["workflow", "list"],
    ):
        code, _out, err = run_cli(argv)
        assert code == 2 and _MARK in err, (argv, code, err[-200:])
    for argv in (["db", "verify"], ["config", "list"]):
        code, _out, err = run_cli(argv)
        assert _MARK not in err, (argv, code, err[-200:])
    print(f"  [2] multi-org on, no scope: all {refused} org-scoped / operator-only verbs refused")


def _gate(argv_extra, verb_path):
    for path, leaf in _leaves():
        if " ".join(path) == verb_path:
            return _run_gate(argv_extra, path, leaf)
    raise AssertionError(verb_path)


def test_3_scoped_and_operator_doors():
    _set_gate(True)
    org_args = _gate(["--org", str(FIX["org_a"])], "project create")
    assert org_args._op_org_id == FIX["org_a"]
    try:
        _gate(["--org", str(FIX["org_a"])], "admin tick")
    except CliError as exc:
        assert exc.code == 2 and "--org cannot" in str(exc), exc
    else:
        raise AssertionError("--org unlocked an operator-only verb")
    # #515 agreement: the shared-setting writers are operator-only at the central gate too, and
    # org-scoped verbs are not blocked by it.
    for verb in (
        "admin retention config",
        "admin retention purge-now",
        "admin srx upload",
        "admin ldap config",
        "admin oidc config",
        "admin lqa rulebook set",
        "connector token-regen",
    ):
        assert _scope.VERB_SCOPE[verb] == _scope.OPERATOR_ONLY, verb
    for verb in ("job run", "doc show"):
        assert _scope.VERB_SCOPE[verb] == _scope.ORG_SCOPED, verb
        assert _gate(["--org", str(FIX["org_a"])], verb)._op_org_id == FIX["org_a"], verb
    os.environ["LOC_TMS_OPERATOR_KEY"] = OP_SECRET
    try:
        for verb in ("project create", "admin tick", "org list"):
            args = _gate(["--operator"], verb)
            assert getattr(args, "_op_org_id", None) is None
        # An install-wide verb needs no scope at all.
        _gate([], "db backup")
        _gate([], "config list")
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
    # A wrong operator key is refused.
    os.environ["LOC_TMS_OPERATOR_KEY"] = "not-the-key"  # pregate: allow-secret throwaway
    try:
        try:
            _gate(["--operator"], "admin tick")
        except CliError as exc:
            assert exc.code == 2, exc
        else:
            raise AssertionError("a wrong operator key passed the gate")
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
    print("  [3] --org scopes org verbs, --operator (key-verified) opens both, wrong key refused")


def test_4_import_doc_scope_and_admin_tick():
    _set_gate(True)
    foreign = FIX["doc_b"]
    code, out, err = run_cli(["--org", str(FIX["org_a"]), "doc", "show", str(foreign)])
    # A foreign org's document is never readable: "not found", never success.
    assert code != 0 and "no such document" in err.lower(), (code, out, err)
    code, out, err = run_cli(["admin", "tick"])
    assert code == 2 and _MARK in err, (code, err[-200:])
    print("  [4] doc show is org-scoped; admin tick needs --operator in-process")


def test_5_gate_off_and_remote_untouched():
    _set_gate(False)
    for path, leaf in _leaves():
        if _scope.VERB_SCOPE[" ".join(path)] != _scope.INSTALL_WIDE:
            _run_gate([], path, leaf)
    _set_gate(True)
    try:
        for verb in ("project create", "admin tick"):
            _gate(["--remote", "http://127.0.0.1:1", "--token", "lsu_" + "x" * 4], verb)
    finally:
        _set_gate(False)
    print("  [5] gate off and --remote: the central gate does nothing")


def main():
    _fixture()
    try:
        test_1_every_verb_is_classified()
        test_2_gate_refuses_unscoped_in_process()
        test_3_scoped_and_operator_doors()
        test_4_import_doc_scope_and_admin_tick()
    finally:
        settings.set_many({"multi_org_enabled": "0"})
    test_5_gate_off_and_remote_untouched()
    print("verify_cli_scope_classification OK")


if __name__ == "__main__":
    main()
