"""Feature flags of add-ons that are not installed: refused on every write door, hidden from lists.

In the open core the paid features (Evaluations Lab, LLMemory, workflow suggestions, Databricks
export, Advanced Workflows) have no code. Before this fix `config feature enable evaluations`
printed "= on", exited 0 and stored "1" while the feature still read as off everywhere, and
`config feature disable` then said "no change" and left the stale "1" behind.

What this pins:

  [1] SEAM: enabling a not-installed add-on feature raises FeatureNotInstalled naming the
      add-on, writes nothing and audits nothing; disabling it clears a stale "1" (changed=True,
      one audit row) and is a no-op when the stored value is already "0".
  [2] CLI: `config feature enable <paid>` exits non-zero with the message and writes nothing;
      `config feature disable <paid>` clears a stale "1"; `config features` lists no add-on
      feature (local and --json).
  [3] API: POST /api/admin/features/<paid> {"enabled": true} answers 400 with the message and
      writes nothing; GET /api/admin/features lists no add-on feature. (MCP has no flag door:
      manage_install(set_config) relays to POST /api/admin/config, which refuses every
      registry feature key; verify_feature_flag_admin [7] and [9] pin that.)
  [4] WEB: the Settings > Features form answers 404 for a paid slug and writes nothing.
  [5] A core feature still flips normally (no false refusal).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII-only. Final line:
"FEATURE ADDON REFUSAL OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_addonflag_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import editions, features
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import AuditLog, Role
from app.services import apikeys, features_admin, security, settings
from tests._csrf import csrf_for

PW = "addonflag-pw-123456"
PAID = [s for s in editions.PAID_FEATURE_ADDON if not editions.feature_installed(s)]
CORE_SLUG = "reports"


def _key(slug):
    return features.feature_by_slug(slug).setting_key


def _stored(slug):
    settings.invalidate_cache()
    return str(settings.get(_key(slug)) or "0")


def _audits():
    with Session(engine) as s:
        return len(s.exec(select(AuditLog).where(AuditLog.action == "feature.toggle")).all())


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, buf.getvalue()


def test_seam():
    for slug in PAID:
        settings.set_many({_key(slug): "0"})
        before = _audits()
        try:
            features_admin.set_feature_enabled(slug, True, actor="t")
        except features_admin.FeatureNotInstalled as exc:
            assert "not installed" in str(exc) and slug in str(exc), str(exc)
        else:
            raise AssertionError(f"{slug}: enable of a not-installed add-on must be refused")
        assert _stored(slug) == "0" and _audits() == before, slug
        # a stale "1" (written by an older version or `config set`) is cleared by disable
        settings.set_many({_key(slug): "1"})
        out = features_admin.set_feature_enabled(slug, False, actor="t")
        assert out["changed"] is True and _stored(slug) == "0", (slug, out)
        assert _audits() == before + 1, slug
        out = features_admin.set_feature_enabled(slug, False, actor="t")
        assert out["changed"] is False and _audits() == before + 1, (slug, out)
    listed = {r["slug"] for r in features_admin.list_features()}
    assert not listed & set(PAID), listed & set(PAID)
    print(f"  [1] seam: {len(PAID)} add-on features refused, stale 1 cleared, not listed OK")


def test_cli():
    for slug in PAID:
        settings.set_many({_key(slug): "0"})
        rc, out = _run(["config", "feature", "enable", slug])
        assert rc not in (0, None), (slug, rc, out)
        assert _stored(slug) == "0", slug
        settings.set_many({_key(slug): "1"})
        rc, out = _run(["config", "feature", "disable", slug])
        assert rc == 0 and "no change" not in out, (slug, rc, out)
        assert _stored(slug) == "0", slug
    rc, out = _run(["--json", "config", "features"])
    assert rc == 0, out
    slugs = {r["slug"] for r in _json.loads(out)["features"]}
    assert CORE_SLUG in slugs and not slugs & set(PAID), slugs
    rc, out = _run(["config", "features"])
    assert rc == 0 and not any(
        f" {s} " in f" {line} " for s in PAID for line in out.splitlines()
    ), out
    print("  [2] CLI: enable refused non-zero, disable clears a stale 1, list hides add-ons OK")


def test_api(api, raw):
    h = {"X-Loc-Token": raw}
    for slug in PAID:
        settings.set_many({_key(slug): "0"})
        r = api.post(f"/api/admin/features/{slug}", headers=h, json={"enabled": True})
        assert r.status_code == 400, (slug, r.status_code, r.text[:200])
        assert "not installed" in r.json().get("error", ""), r.text[:200]
        assert _stored(slug) == "0", slug
    r = api.get("/api/admin/features", headers=h)
    assert r.status_code == 200, r.text[:200]
    slugs = {row["slug"] for row in r.json()["features"]}
    assert CORE_SLUG in slugs and not slugs & set(PAID), slugs
    print("  [3] API: enable refused 400 with the message, list hides add-ons OK")


def test_web(email):
    from app.main import app

    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    tok = csrf_for(c)
    for slug in PAID:
        settings.set_many({_key(slug): "0"})
        r = c.post(
            f"/settings/features/{slug}",
            data={"enabled": "1", "csrf_token": tok},
            follow_redirects=False,
        )
        assert r.status_code == 404, (slug, r.status_code)
        assert _stored(slug) == "0", slug
    # [5] a core feature still flips
    r = c.post(
        f"/settings/features/{CORE_SLUG}",
        data={"enabled": "0", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303 and _stored(CORE_SLUG) == "0", r.status_code
    r = c.post(
        f"/settings/features/{CORE_SLUG}",
        data={"enabled": "1", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303 and _stored(CORE_SLUG) == "1", r.status_code
    print("  [4][5] web: add-on toggle 404 and unwritten; a core feature still flips OK")


def main():
    init_db()
    assert PAID, "the core edition has no add-on features installed, so the list is never empty"
    email = "addonflag@x.com"
    user = security.create_user(email, email, Role.ADMIN, PW)
    _row, raw = apikeys.create_key(user.id, "addonflag", scopes="api docs admin")
    settings.set_many({"remote_admin_enabled": "1"})
    test_seam()
    test_cli()
    from app.main import app

    test_api(TestClient(app), raw)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        test_web(email)
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("FEATURE ADDON REFUSAL OK")


if __name__ == "__main__":
    main()
