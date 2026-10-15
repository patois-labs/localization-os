"""CLI de-duplication (#533): one implementation, no drift, no behavior change.

  1. `admin routing rule list --json` returns the same payload as `routing rule list --json`
     (the admin copy used to drop match_subject, language_set_id, created_by, created_at), and
     the two groups share one implementation of add/list/show/toggle/delete.
  2. `admin gdpr export` and `user export` share one implementation: a record holding a non-ISO
     value (a datetime) exports through BOTH (only the admin copy used default=str).
  3. The seven layer-2 feature gates delegate to `_common.feature_gate`, which raises the
     registry's own cli_refusal and exit code while the switch is off.
  4. The small helper copies (`_get_customer`, `_parse_kv`, `_apikey_expiry_cell`, the export
     file-writing tail) are one function each.

Final line: "CLI DEDUPE OK".
"""

import datetime
import inspect
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cli_dedupe_")

from app.db import init_db
from tests._cli_runner import run_cli


def _json(argv):
    code, out, err = run_cli(["--json", *argv])
    assert code == 0, (argv, code, out, err)
    return json.loads(out.strip().splitlines()[-1])


def test_routing_twins_share_one_implementation():
    from app.cli import cmd_admin_helpers

    p = _json(["admin", "routing", "rule", "add", "--ext", "docx", "--priority", "5"])
    assert p["ok"] is True and p["match_ext"] == "docx", p
    # the admin create payload is the full rule dict, like the routing twin's
    assert {"match_subject", "language_set_id", "created_by", "created_at"} <= set(p), sorted(p)
    admin_list = _json(["admin", "routing", "rule", "list"])
    routing_list = _json(["routing", "rule", "list"])
    assert admin_list == routing_list, (admin_list, routing_list)
    first = admin_list["rules"][0]
    assert {"match_subject", "language_set_id", "created_by", "created_at"} <= set(first), first
    rid = str(first["id"])
    assert _json(["admin", "routing", "rule", "show", rid]) == _json(
        ["routing", "rule", "show", rid]
    )
    t1 = _json(["admin", "routing", "rule", "toggle", rid])
    t2 = _json(["routing", "rule", "toggle", rid])
    assert t1["active"] is False and t2["active"] is True, (t1, t2)
    # reserved v2 fields are refused identically (exit 2) by both groups
    for group in (["admin", "routing"], ["routing"]):
        code, out, err = run_cli([*group, "rule", "add", "--subject", "invoice"])
        assert code == 2, (group, code, out, err)
    src = inspect.getsource(cmd_admin_helpers._rule_list)
    assert "cmd_routing.rule_list_local" in src, "the admin list must delegate"
    assert _json(["admin", "routing", "rule", "delete", rid, "--yes"])["deleted"] is True
    print("  1. admin routing rule == routing rule (full payload, one implementation)")


def test_gdpr_twins_share_one_implementation():
    from app.cli import cmd_admin_helpers
    from app.kernel import gdpr, security

    security.create_user("subject@example.com", "Subject", "translator", "")
    real = gdpr.export_user
    gdpr.export_user = lambda uid, caller_org_id=None: iter(
        [("profile", {"when": datetime.datetime(2026, 1, 2, 3, 4, 5)})]
    )
    try:
        a = _json(["admin", "gdpr", "export", "subject@example.com"])
        u = _json(["user", "export", "subject@example.com"])
    finally:
        gdpr.export_user = real
    assert a == u and a["records"] == 1, (a, u)
    assert "2026-01-02" in json.dumps(a["sections"]), a
    assert "cmd_user._user_export" in inspect.getsource(cmd_admin_helpers._gdpr_export)
    print("  2. admin gdpr export == user export (non-ISO values serialize in both)")


def test_feature_gates_delegate_to_the_registry():
    from app import features
    from app.cli import (
        _common,
        cmd_image,
        cmd_query,
        cmd_report,
        cmd_routing,
        cmd_work,
        keys_common,
        vendor_common,
    )
    from app.kernel import settings

    gates = {
        "images": cmd_image._flag_gate,
        "queries": cmd_query._flag_gate,
        "reports": cmd_report._flag_gate,
        "routing": cmd_routing._flag_gate,
        "worklist": cmd_work._flag_gate,
        "keys": keys_common._flag_gate,
        "vendors": vendor_common._flag_gate,
    }
    for slug, gate in gates.items():
        assert f'feature_gate("{slug}")' in inspect.getsource(gate), slug
        feat = features.feature_by_slug(slug)
        assert feat.cli_refusal, f"{slug} must carry its own refusal text"
        prior = settings.get(feat.setting_key)
        settings.set_many({feat.setting_key: "0"})
        try:
            try:
                _common.feature_gate(slug)
            except _common.CliError as exc:
                assert str(exc) == feat.cli_refusal and exc.code == feat.cli_exit_code, slug
            else:
                raise AssertionError(f"feature_gate({slug!r}) did not refuse while off")
            settings.set_many({feat.setting_key: "1"})
            _common.feature_gate(slug)  # on: no-op
        finally:
            settings.set_many({feat.setting_key: prior if prior in ("0", "1") else "1"})
    print("  3. seven layer-2 gates delegate to feature_gate; refusal text lives in the registry")


def test_helper_copies_are_one_function():
    from app.cli import (
        _common,
        _remote_impl_part3,
        cmd_connector,
        cmd_content_type,
        cmd_engine,
        cmd_project_project,
        cmd_style_guide,
        cmd_user,
    )

    for mod in (cmd_content_type, cmd_project_project, cmd_style_guide):
        assert mod._get_customer is _common.get_customer, mod.__name__
    assert cmd_connector._parse_kv is _common.parse_kv is cmd_engine._parse_kv
    assert cmd_user._apikey_expiry_cell is _common.apikey_expiry_cell
    assert _remote_impl_part3._apikey_expiry_cell is _common.apikey_expiry_cell
    assert _common.apikey_expiry_cell({}) == "never"
    assert _common.apikey_expiry_cell({"expires_at": "2026-01-02T00:00", "expired": True}) == (
        "2026-01-02 (expired)"
    )
    # (the memory / suggestions export tails that shared _emit_export_lines left with LLMemory)
    # the term-metadata helpers: one definition shared by the in-process and --remote arms
    from app.cli import _remote_impl_common_leaf, cmd_project_part5

    assert cmd_project_project._term_meta_from_args is _common.term_meta_from_args
    assert _remote_impl_common_leaf._term_meta_from_args is _common.term_meta_from_args
    assert cmd_project_part5._term_meta_from_args is _common.term_meta_from_args
    assert _remote_impl_common_leaf._TERM_META_FLAGS is _common.TERM_META_FLAGS
    print("  4. _get_customer / _parse_kv / _apikey_expiry_cell: one copy each")


def main_() -> None:
    init_db()
    test_routing_twins_share_one_implementation()
    test_gdpr_twins_share_one_implementation()
    test_feature_gates_delegate_to_the_registry()
    test_helper_copies_are_one_function()
    print("CLI DEDUPE OK")


if __name__ == "__main__":
    main_()
