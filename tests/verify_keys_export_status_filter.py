"""K9 — the EXPORT STATUS FILTER, surfaced on all three key-export surfaces.

The maintainer's 2026-08-07 call: the service-layer filter (`key_files.export_locale`'s
`status=` argument) already existed with KEY-LEVEL semantics; this wave is pure
surface plumbing — the web export form, `keys export --status`, and the
`GET /api/projects/{pid}/keys/export?status=` query param — each exposing EXACTLY
the service's semantics with the SAME status vocabulary.

KEY-LEVEL, stated once so no surface can quietly reinterpret it: a key is included
or excluded WHOLE. It ships iff at least one of its per-locale value rows (every
plural form counts) carries the requested segment status; otherwise the whole key
is absent. There is no such thing as a partial key in an export — a plural key
whose `one` form is approved but whose `other` form is edited ships BOTH forms
under `--status approved`, because the KEY matches.

WHAT THIS SUITE PINS
  A  the service filter, KEY-LEVEL, on BOTH shapes (JSON + PO): a matching key
     ships whole, a non-matching key is absent whole.
  B  a PLURAL key is all-or-nothing: one matching form pulls every form in.
  C  the unfiltered export is UNCHANGED (regression — the default must not move).
  D  the web GET route honours `?status=`.
  E  the /api route honours `?status=` (bytes-identical filtering to the service).
  F  the CLI `keys export --status` honours it, and pre-fix the flag DID NOT EXIST
     — a behavioural proof: the HEAD parser REJECTS `--status`, the working-tree
     parser ACCEPTS it.
  G  the K8 interplay end to end: an over-limit machine write routes to
     NEEDS_REVIEW (K8), so `--status needs_review` MUST capture it — the stated
     reason the filter and K8 must compose.
  H  the source-locale (base-value) export ignores `status` (there is no per-locale
     status to filter on) — documented behaviour, asserted so it cannot silently
     change.

Run as its own hermetic subprocess by tests/test_verify_suites.py. Prints
'verify_keys_export_status_filter OK'.
"""

import io
import json as _json
import os
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_k9status_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Project,
    ProjectType,
    Role,
    SegStatus,
    StringKey,
)
from app.services import (
    apikeys,
    content,
    key_files,
    security,
    settings,
)
from app.services import (
    keys as keys_svc,
)

init_db()

PW = "k9status-pw-123456"
_N = [0]
NS = "app"
TGT = "de-DE"


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


def _run_json(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json"] + argv)  # noqa: RUF005
    out = buf.getvalue()
    return rc, (_json.loads(out) if out.strip() else {})


def _run_both(argv):
    """Human mode with STDOUT+STDERR folded together — the export summary/notices
    ride stderr so a `keys export > file` redirect keeps clean bytes on stdout."""
    from contextlib import redirect_stderr

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _switch(on: bool) -> None:
    settings.set_many({"key_projects_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _mk_customer() -> int:
    with Session(engine) as s:
        c = Customer(name="K9Cust %d" % _uniq())  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_key_project(cust_id, targets="de-DE, ru-RU") -> int:
    with Session(engine) as s:
        p = content.project_create(
            s,
            "K9Keys %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales=targets,
            customer_id=str(cust_id),
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _add_key(pid, name, base="Hello", plural=False, char_limit=None) -> int:
    with Session(engine) as s:
        project = s.get(Project, pid)
        return keys_svc.create_key(
            s,
            project,
            name,
            namespace=NS,
            is_plural=plural,
            char_limit=char_limit,
            base_value=({"other": base} if plural else base),
            actor="fixture",
        ).id


def _set(pid, key_id, text, *, status=None, plural_category=None, origin=None):
    with Session(engine) as s:
        k = s.get(StringKey, key_id)
        keys_svc.set_value(
            s,
            k,
            TGT,
            text,
            origin=origin or keys_svc.ORIGIN_HUMAN,
            status=status,
            plural_category=plural_category,
            actor="fixture",
        )


def _export(pid, *, fmt="json", status=None):
    """The service filter, namespace-scoped so the build file carries BARE names
    (msgid == the key's own name) — the cleanest thing to assert against."""
    with Session(engine) as s:
        project = s.get(Project, pid)
        return key_files.export_locale(
            s, project, TGT, fmt=fmt, namespace=NS, status=status
        ).decode("utf-8")


def _seed_three_statuses(pid):
    """approved / edited / mt in ONE locale — one flat-named key each."""
    ka = _add_key(pid, "keep", base="Keep")
    kd = _add_key(pid, "draft", base="Draft")
    km = _add_key(pid, "mtonly", base="Machine")
    _set(pid, ka, "Behalten", status=SegStatus.APPROVED)
    _set(pid, kd, "Entwurf", status=SegStatus.EDITED)
    _set(pid, km, "Maschine", status=SegStatus.MT)
    return ka, kd, km


# ============================================================================
# [A] the service filter, KEY-LEVEL, both shapes
# ============================================================================
def test_service_filter_json_is_key_level():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)

    body = _json.loads(_export(pid, status=SegStatus.APPROVED))
    assert body.get("keep") == "Behalten", body
    assert "draft" not in body and "mtonly" not in body, (
        "a non-matching key must be ABSENT WHOLE, not blanked",
        body,
    )

    body = _json.loads(_export(pid, status=SegStatus.EDITED))
    assert set(body) == {"draft"}, body

    body = _json.loads(_export(pid, status=SegStatus.MT))
    assert set(body) == {"mtonly"}, body
    print("  [A] service JSON status filter is key-level (whole key in/out) OK")


def test_service_filter_po_is_key_level():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)

    po = _export(pid, fmt="po", status=SegStatus.APPROVED)
    assert 'msgid "keep"' in po, po
    assert 'msgid "draft"' not in po and 'msgid "mtonly"' not in po, (
        "the .po build file must drop non-matching keys entirely",
        po,
    )
    assert 'msgstr "Behalten"' in po, po
    print("  [A] service PO status filter is key-level OK")


# ============================================================================
# [B] a PLURAL key is all-or-nothing
# ============================================================================
def test_plural_key_is_all_or_nothing():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    kp = _add_key(pid, "items", base="items", plural=True)
    # one form APPROVED, the other EDITED — the KEY matches `approved`, so BOTH
    # forms must ship (key-level, never a partial key).
    _set(pid, kp, "1 Ding", status=SegStatus.APPROVED, plural_category="one")
    _set(pid, kp, "viele Dinge", status=SegStatus.EDITED, plural_category="other")

    body = _json.loads(_export(pid, status=SegStatus.APPROVED))
    assert "items" in body, ("the key matches on its `one` form", body)
    forms = body["items"]
    assert forms.get("one") == "1 Ding", forms
    assert forms.get("other") == "viele Dinge", (
        "the NON-matching plural form must ride along — the key ships WHOLE",
        forms,
    )

    # no form is EDITED-and-nothing-else... the `other` form IS edited, so
    # filtering on `edited` also pulls the whole key (its `other` form matches).
    body = _json.loads(_export(pid, status=SegStatus.EDITED))
    assert "items" in body and body["items"].get("one") == "1 Ding", body

    # a status no form carries drops the whole key.
    body = _json.loads(_export(pid, status=SegStatus.MT))
    assert body == {} or "items" not in body, body
    print("  [B] plural key filters all-or-nothing (any form matches -> whole key) OK")


# ============================================================================
# [C] unfiltered export UNCHANGED (regression)
# ============================================================================
def test_unfiltered_export_is_unchanged():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)

    body = _json.loads(_export(pid, status=None))
    assert set(body) == {"keep", "draft", "mtonly"}, (
        "no status filter must export EVERY key — the default cannot move",
        body,
    )

    po = _export(pid, fmt="po", status=None)
    for name in ("keep", "draft", "mtonly"):
        assert f'msgid "{name}"' in po, (name, po)
    print("  [C] the unfiltered export ships every key on both shapes OK")


# ============================================================================
# [D] the WEB GET route
# ============================================================================
def test_web_route_honours_status():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)
    admin = _login("k9-web@x.com")

    r = admin.get(
        "/projects/%d/keys/export?locale=%s&fmt=json&namespace=%s&status=%s"  # noqa: UP031
        % (pid, TGT, NS, SegStatus.APPROVED)
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    body = _json.loads(r.content)
    assert set(body) == {"keep"}, body

    # no status -> unchanged
    r = admin.get("/projects/%d/keys/export?locale=%s&fmt=json&namespace=%s" % (pid, TGT, NS))  # noqa: UP031
    assert set(_json.loads(r.content)) == {"keep", "draft", "mtonly"}, r.content
    print("  [D] web export route filters by ?status= (and default is unchanged) OK")


def _login(email, role=Role.ADMIN):
    from tests._csrf import csrf_for

    security.create_user(email, email, role, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


# ============================================================================
# [E] the /api route
# ============================================================================
def test_api_route_honours_status():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)
    user = security.create_user("k9-api@x.com", "k9-api@x.com", Role.ADMIN, PW)
    _row, tok = apikeys.create_key(user.id, "k9", scopes="api keys")
    client = TestClient(app)
    head = {"X-Loc-Token": tok}

    r = client.get(
        "/api/projects/%d/keys/export?locale=%s&namespace=%s&status=%s"  # noqa: UP031
        % (pid, TGT, NS, SegStatus.EDITED),
        headers=head,
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert set(_json.loads(r.content)) == {"draft"}, r.content

    r = client.get(
        "/api/projects/%d/keys/export?locale=%s&namespace=%s" % (pid, TGT, NS),  # noqa: UP031
        headers=head,  # noqa: RUF100, UP031
    )
    assert set(_json.loads(r.content)) == {"keep", "draft", "mtonly"}, r.content
    print("  [E] /api export route filters by status= (and default is unchanged) OK")


# ============================================================================
# [F] the CLI `keys export --status`, plus the pre-fix behavioural proof
# ============================================================================
def test_cli_export_honours_status():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)

    rc, p = _run_json(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--locale",
            TGT,
            "--namespace",
            NS,
            "--status",
            SegStatus.MT,
        ]
    )
    assert rc == 0, p
    assert set(_json.loads(p["content"])) == {"mtonly"}, p

    rc, p = _run_json(["keys", "export", "--project", str(pid), "--locale", TGT, "--namespace", NS])
    assert set(_json.loads(p["content"])) == {"keep", "draft", "mtonly"}, p
    print("  [F] CLI `keys export --status` filters (and default is unchanged) OK")


def _build_parser_from_source(src: str):
    """Load an arbitrary cmd_keys.py SOURCE as a throwaway module under the real
    `app.cli` package (so its relative imports resolve) and hang its `register`
    off a fresh argparse tree. Used to build the PRE-FIX parser from `git show`."""
    import argparse
    import importlib.util

    spec = importlib.util.spec_from_loader("app.cli._cmd_keys_prefix", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "app.cli"
    exec(compile(src, "<cmd_keys@pre-K9>", "exec"), mod.__dict__)
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    mod.register(subs)
    return parser


# The pre-K9 commit, PERMANENT in main history: `f1c81c8` is the K8-merge tip this
# branch forked from. It is pinned by SHA on purpose — HEAD is the wrong anchor for a
# fail-pre-fix guard, because once this branch's own fix commits, HEAD CONTAINS the
# fix and the "must not already declare --status" assertion inverts and trips (the
# exact lesson f1c81c8's own message records for the [N4] fail-before pin).
_PRE_K9_COMMIT = "f1c81c8"


def test_cli_status_flag_did_not_exist_pre_fix():
    """BEHAVIOURAL fail-pre-fix proof (no git stash): the parser built from the
    committed PRE-K9 source (`f1c81c8`) REJECTS `--status` on `keys export`; the
    working-tree parser ACCEPTS it. argparse answers an unknown flag with
    SystemExit(2)."""
    pre_src = subprocess.check_output(
        ["git", "show", f"{_PRE_K9_COMMIT}:app/cli/cmd_keys.py"],
        cwd=_ROOT,
        text=True,  # noqa: RUF100, UP032
    )
    assert (
        "--status" not in pre_src.split("def _keys_export")[0].split('add_parser("export"')[-1]
    ), "guard assumption: the pre-K9 export parser must not already declare --status"

    pre = _build_parser_from_source(pre_src)
    with pytest.raises(SystemExit):
        pre.parse_args(["keys", "export", "--project", "1", "--status", "mt"])

    # positive control: the SAME harness accepts every OTHER export flag pre-fix,
    # so the SystemExit above is about `--status` specifically, not a broken parser.
    ns = pre.parse_args(["keys", "export", "--project", "1", "--namespace", "app"])
    assert ns.namespace == "app", ns

    # and the CURRENT (working-tree) parser accepts --status.
    from app.cli.__main__ import build_parser

    now = build_parser()
    got = now.parse_args(["keys", "export", "--project", "1", "--status", "mt"])
    assert got.status == "mt", got
    print("  [F] pre-fix CLI REJECTS `keys export --status`; post-fix ACCEPTS it OK")


# ============================================================================
# [G] the K8 interplay, end to end
# ============================================================================
def test_over_limit_held_value_is_caught_by_needs_review_filter():
    """K8 says an over-limit MACHINE write routes to NEEDS_REVIEW. K9's whole point
    is that a release manager can then say `--status needs_review` and get exactly
    those held values — the composition the K8 re-assertion exists to protect."""
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    over = _add_key(pid, "cramped", base="Home", char_limit=5)
    fine = _add_key(pid, "roomy", base="Home")
    # the exact over-limit machine write from verify_char_limit [B1]: committed, but
    # routed to NEEDS_REVIEW because 23 chars > a 5-char limit.
    _set(pid, over, "Einstellungen und mehr", origin=keys_svc.ORIGIN_IMPORT)
    _set(pid, fine, "Zuhause", status=SegStatus.APPROVED)

    with Session(engine) as s:
        seg = keys_svc.values_for(s, s.get(StringKey, over), TGT)[0]
        assert seg.status == SegStatus.NEEDS_REVIEW, (
            "K8 precondition: the over-limit machine write must be needs_review",
            seg.status,
        )

    # service
    body = _json.loads(_export(pid, status=SegStatus.NEEDS_REVIEW))
    assert set(body) == {"cramped"}, (
        "the needs_review filter must catch the K8-held value and ONLY it",
        body,
    )
    assert body["cramped"] == "Einstellungen und mehr", body

    # CLI, same locale — the over-limit value is ALSO reported over-limit (K8's own
    # signal) while being CAUGHT by the status filter (K9's), the two staying distinct.
    rc, p = _run_json(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--locale",
            TGT,
            "--namespace",
            NS,
            "--status",
            SegStatus.NEEDS_REVIEW,
        ]
    )
    assert rc == 0, p
    assert set(_json.loads(p["content"])) == {"cramped"}, p
    assert p.get("over_char_limit", 0) >= 1 and "cramped" in "".join(
        p.get("over_char_limit_keys") or []
    ), ("K8's over-limit report and K9's status filter must both fire, distinctly", p)
    print(
        "  [G] an over-limit-held value is caught by --status needs_review, "
        "end to end, K8 report intact OK"
    )


# ============================================================================
# [H] source-locale export ignores status (documented behaviour)
# ============================================================================
def test_base_export_ignores_status():
    """The source locale serves each key's BASE value; base values have no
    per-locale segment status, so `status=` is a no-op there — asserted so it
    cannot silently start dropping base keys."""
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)
    with Session(engine) as s:
        project = s.get(Project, pid)
        filtered = key_files.export_locale(
            s, project, "en-US", fmt="json", namespace=NS, status=SegStatus.APPROVED
        ).decode("utf-8")
    body = _json.loads(filtered)
    assert set(body) == {"keep", "draft", "mtonly"}, (
        "a base-value export has no per-locale status to filter on",
        body,
    )
    print("  [H] source-locale (base) export ignores status OK")


# ============================================================================
# [I] F3 — the `filtered_out` report channel (the outcome that used to be silent)
# ============================================================================
def _export_report(pid, *, fmt="json", status=None):
    rep: list = []
    with Session(engine) as s:
        project = s.get(Project, pid)
        data = key_files.export_locale(
            s, project, TGT, fmt=fmt, namespace=NS, status=status, report=rep
        ).decode("utf-8")
    return data, rep


def test_filtered_out_is_reported_at_the_service():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)

    # a REAL filter: status=approved drops draft + mtonly, and NAMES them.
    data, rep = _export_report(pid, status=SegStatus.APPROVED)
    assert set(_json.loads(data)) == {"keep"}, data
    assert set(key_files.filtered_out_keys(rep)) == {"draft", "mtonly"}, rep

    # a BOGUS status: a valid EMPTY file (full .po header would render, JSON is {}),
    # but every dropped key is named so the emptiness is legible rather than mistaken
    # for a project with no strings — the exact silent outcome F3 retires.
    data, rep = _export_report(pid, status="not_a_real_status")
    assert _json.loads(data) == {}, ("a bogus filter yields a valid EMPTY file", data)
    assert set(key_files.filtered_out_keys(rep)) == {"keep", "draft", "mtonly"}, rep
    assert any("FILTERED OUT" in n for n in key_files.export_notices(rep)), (
        key_files.export_notices(rep)
    )

    # an UNFILTERED export names nothing (the default outcome is unchanged).
    _data, rep = _export_report(pid, status=None)
    assert key_files.filtered_out_keys(rep) == [], rep
    print("  [I] filtered_out reported at the service (real + bogus + unfiltered) OK")


def test_filtered_out_header_on_web_and_api():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)

    admin = _login("k9-f3web@x.com")
    r = admin.get(
        "/projects/%d/keys/export?locale=%s&fmt=json&namespace=%s&status=%s"  # noqa: UP031
        % (pid, TGT, NS, SegStatus.APPROVED)
    )
    assert r.headers.get(key_files.HDR_FILTERED) == "2", dict(r.headers)
    assert "draft" in (r.headers.get(key_files.HDR_FILTERED_KEYS) or ""), dict(r.headers)

    user = security.create_user("k9-f3api@x.com", "k9-f3api@x.com", Role.ADMIN, PW)
    _row, tok = apikeys.create_key(user.id, "k9f3", scopes="api keys")
    client = TestClient(app)
    head = {"X-Loc-Token": tok}
    r = client.get(
        "/api/projects/%d/keys/export?locale=%s&namespace=%s&status=%s"  # noqa: UP031
        % (pid, TGT, NS, SegStatus.APPROVED),
        headers=head,
    )
    assert r.headers.get(key_files.HDR_FILTERED) == "2", dict(r.headers)

    # a CLEAN (unfiltered) export emits NO filtered header — the byte-identical path.
    r = client.get(
        "/api/projects/%d/keys/export?locale=%s&namespace=%s" % (pid, TGT, NS),  # noqa: UP031
        headers=head,  # noqa: RUF100, UP031
    )
    assert key_files.HDR_FILTERED not in r.headers, dict(r.headers)
    print("  [I] X-Export-Filtered-Out on web + /api (absent when unfiltered) OK")


def test_filtered_out_on_the_cli():
    _switch(True)
    pid = _mk_key_project(_mk_customer())
    _seed_three_statuses(pid)

    rc, p = _run_json(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--locale",
            TGT,
            "--namespace",
            NS,
            "--status",
            SegStatus.APPROVED,
        ]
    )
    assert rc == 0, p
    assert p.get("filtered_out") == 2, p
    assert set(p.get("filtered_out_keys") or []) == {"draft", "mtonly"}, p

    # human mode prints the notice on stderr (a `keys export > file` redirect keeps
    # clean bytes on stdout; the summary rides stderr).
    rc, out = _run_both(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--locale",
            TGT,
            "--namespace",
            NS,
            "--status",
            SegStatus.APPROVED,
        ]
    )
    assert rc == 0 and "FILTERED OUT" in out, out

    # unfiltered: zero, named nothing.
    rc, p = _run_json(["keys", "export", "--project", str(pid), "--locale", TGT, "--namespace", NS])
    assert p.get("filtered_out") == 0 and (p.get("filtered_out_keys") or []) == [], p
    print("  [I] CLI carries filtered_out in --json and prints it in the summary OK")


def main():
    test_service_filter_json_is_key_level()
    test_service_filter_po_is_key_level()
    test_plural_key_is_all_or_nothing()
    test_unfiltered_export_is_unchanged()
    test_web_route_honours_status()
    test_api_route_honours_status()
    test_cli_export_honours_status()
    test_cli_status_flag_did_not_exist_pre_fix()
    test_over_limit_held_value_is_caught_by_needs_review_filter()
    test_base_export_ignores_status()
    test_filtered_out_is_reported_at_the_service()
    test_filtered_out_header_on_web_and_api()
    test_filtered_out_on_the_cli()
    print("verify_keys_export_status_filter OK")


if __name__ == "__main__":
    main()
