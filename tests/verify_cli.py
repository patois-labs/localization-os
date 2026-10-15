"""Headless CLI verification (Phases 1-2) - suite #41.

Drives app.cli.__main__.main([...]) IN-PROCESS against a throwaway DATA_DIR for
the config/db/audit/health commands, and drives `init` in a fresh SUBPROCESS with
its own empty DATA_DIR (init seeds users + flips the login wall, so it needs an
isolated DB that the in-process suite's engine hasn't already populated).

Hermetic like the rest: throwaway LOC_TMS_DATA_DIR + cleared provider keys at
import, so it forces MockProvider and never touches the real data dir. ASCII-only
prints (the cp1252 console throws on non-ASCII).
"""

import json
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# Throwaway data dir (also gates OFF account seeding / login wall).
from tests._bootstrap import init_env

init_env("loctms_cli_")

from app.db import init_db
from tests._cli_runner import run_cli


def run(argv):
    """Run the CLI in-process, capturing (exit_code, stdout, stderr).

    2026-07-14: a project requires a customer when one exists. These CLI
    fixtures create projects for OTHER reasons and don't specify a customer, so a
    `project create` without `--customer-id` gets ONE auto-attached here (a single
    cached suite customer). Calls that DO specify a customer (or test its rejection)
    are left untouched."""
    return run_cli(_inject_customer(argv))


def run_json(argv):
    code, out, err = run(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


# 2026-07-14: a project requires a customer when one exists. The CLI project-
# create fixtures below aren't about customers, so they all borrow ONE cached customer
# via `_cust_arg()` (appended to the create argv). Created lazily on first use.
_G_CUST = None


def _cust_arg():
    global _G_CUST
    if _G_CUST is None:
        code, pay, _ = run_json(["customer", "create", "CLI Suite Cust"])
        assert code == 0, pay
        _G_CUST = pay["id"]
    return ["--customer-id", str(_G_CUST)]


def _inject_customer(argv):
    """Auto-attach a suite customer to a `project create` argv that doesn't name one
    (a project requires a customer when one exists). A no-op for any other command, or
    when --customer-id is already present. Detects `project create` as the first two
    non-global tokens."""
    toks = [a for a in argv if not (isinstance(a, str) and a.startswith("-"))]
    if (
        len(toks) >= 2
        and toks[0] == "project"
        and toks[1] == "create"
        and "--customer-id" not in argv
    ):
        return list(argv) + _cust_arg()
    return argv


# --- db init-schema + health ------------------------------------------------
def test_init_schema_and_health():
    code, out, _ = run(["db", "init-schema"])
    assert code == 0, code
    assert "initialized" in out.lower(), out
    code, out, _ = run(["health"])
    assert code == 0 and "OK" in out, (code, out)
    print("  db init-schema on temp DB + health report OK")


# --- db backup refuses to clobber an existing file without --force ----------
def test_db_backup_overwrite_guard():
    init_db()
    fd, dst = tempfile.mkstemp(prefix="loctms_backup_", suffix=".db")
    os.close(fd)  # file already exists -> backup must refuse without --force
    try:
        code, _, err = run(["db", "backup", dst])
        assert code == 2 and "exists" in err.lower(), (code, err)
        code, payload, _ = run_json(["db", "backup", dst, "--force"])
        assert code == 0 and payload["path"] == dst, payload
        assert os.path.getsize(dst) > 0, "backup file is empty"
    finally:
        os.remove(dst)
    print("  db backup refuses to overwrite without --force, --force overwrites OK")


# --- config set -> get -> list roundtrip (incl. a UI-NONE key) --------------
def test_config_roundtrip_plain():
    init_db()
    # api_rate_limit_per_minute is one of the keys with NO UI form today.
    # fix-audit-settings gate round 1 L4: the throttle is security-sensitive (--yes).
    code, _, _ = run(["config", "set", "api_rate_limit_per_minute", "200", "--yes"])
    assert code == 0, code
    code, payload, _ = run_json(["config", "get", "api_rate_limit_per_minute"])
    assert code == 0 and payload["value"] == "200", payload
    # list must surface it (grouped) and NOT under --json lose it.
    code, payload, _ = run_json(["config", "list", "--group", "security"])
    assert code == 0 and payload["settings"]["api_rate_limit_per_minute"] == "200", payload
    print("  config set->get->list roundtrip (UI-none key) OK")


# --- a secret key never echoes plaintext ------------------------------------
def test_config_secret_masked():
    init_db()
    # #280: every LDAP key is security-sensitive, so the host write confirms (--yes).
    code, _, _ = run(["config", "set", "ldap_bind_password_enc", "hunter2hunter2", "--yes"])
    assert code == 0, code
    code, out, _ = run(["config", "get", "ldap_bind_password_enc"])
    assert "hunter2" not in out, "secret leaked in human output!"
    assert "(set)" in out, out
    code, payload, _ = run_json(["config", "get", "ldap_bind_password_enc"])
    assert payload["value"] == "(set)" and payload["secret"] is True, payload
    assert "hunter2" not in json.dumps(payload), "secret leaked in json output!"
    # and it must actually be stored (decryptable), not silently dropped
    from app.services import settings

    assert settings.get_secret("ldap_bind_password_enc") == "hunter2hunter2"
    print("  config secret key shows (set), never plaintext OK")


# --- an endpoint-URL key redacts embedded credentials on read ---------------
def test_config_endpoint_url_redacted():
    init_db()
    from app.services import settings

    # otel_endpoint / blobstore_s3_endpoint_url may carry a credential in their userinfo
    # or query. is_secret_key is False for them (the scheme+host is operator info), so
    # `config get` must PARTIALLY redact -- keep scheme+host+path, strip userinfo/query.
    settings.set_many(
        {
            "otel_endpoint": "https://user:SUPERSECRET123@collector:4318/v1/traces",
            "blobstore_s3_endpoint_url": "https://minio:9000/bucket?sig=SUPERSECRET123",
        }
    )
    code, out, _ = run(["config", "get", "otel_endpoint"])
    assert code == 0 and "SUPERSECRET123" not in out, out
    assert "https://collector:4318/v1/traces" in out, out
    code, payload, _ = run_json(["config", "get", "blobstore_s3_endpoint_url"])
    assert "SUPERSECRET123" not in json.dumps(payload), payload
    assert payload["value"] == "https://minio:9000/bucket?(redacted)", payload
    # `config list` (the bulk read) must not leak it either.
    code, out, _ = run(["config", "list"])
    assert "SUPERSECRET123" not in out, "endpoint credential leaked in `config list`"
    settings.set_many({"otel_endpoint": "", "blobstore_s3_endpoint_url": ""})
    print("  config endpoint-URL key redacts embedded userinfo/query, never plaintext OK")


# --- config rejects an unknown key ------------------------------------------
def test_config_rejects_unknown():
    code, _, err = run(["config", "set", "not_a_real_key", "x"])
    assert code == 2, code
    assert "unknown setting key" in err.lower(), err
    print("  config rejects unknown key (exit 2) OK")


# --- config apply from a temp .toml -----------------------------------------
def test_config_apply_toml():
    init_db()
    from app.services import settings

    fd, path = tempfile.mkstemp(suffix=".toml", prefix="loctms_cfg_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('mt_concurrency = "8"\n')
        fh.write('session_idle_minutes = "77"\n')
    try:
        # dry-run writes nothing
        code, _, _ = run(["config", "apply", path, "--dry-run"])
        assert code == 0, code
        assert settings.get("session_idle_minutes") != "77", "dry-run wrote!"
        # real apply (#280: session_idle_minutes is security-sensitive, so --yes)
        code, _payload, _ = run_json(["config", "apply", path, "--yes"])
        assert code == 0, code
        assert settings.get("mt_concurrency") == "8", settings.get("mt_concurrency")
        assert settings.get("session_idle_minutes") == "77"
        # unknown key in a file rejects the WHOLE file
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('mt_concurrency = "3"\nbogus_key = "1"\n')
        code, _, err = run(["config", "apply", path])
        assert code == 2 and "unknown" in err.lower(), (code, err)
        assert settings.get("mt_concurrency") == "8", "rejected file still wrote!"
        # #28: a UTF-8 BOM (PowerShell `-Encoding utf8` default; utf-8-sig here) must
        # be stripped, not rejected as 'invalid TOML (line 1, col 1)'. Round-trips.
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write('mt_concurrency = "5"\n')
        assert open(path, "rb").read().startswith(b"\xef\xbb\xbf"), "test file lost its BOM"  # noqa: SIM115
        code, _, err = run(["config", "apply", path])
        assert code == 0, (code, err)
        assert settings.get("mt_concurrency") == "5", ("BOM'd TOML must apply", err)
    finally:
        os.remove(path)
    print("  config apply from .toml (dry-run + real + reject-unknown + BOM) OK")


# --- config export-env ------------------------------------------------------
def test_config_export_env():
    code, out, _ = run(["config", "export-env"])
    assert code == 0, code
    assert "LOC_TMS_ADMIN_EMAIL=" in out, out
    assert "ANTHROPIC_API_KEY=<secret>" in out, "secret env not masked!"
    # F4: the phase-3 trust source-restriction secret is scaffolded and secret-masked
    # (parity with the other trust-env enumeration surfaces; never echo a secret value).
    assert "LOC_TMS_TRUST_HEADER_SECRET=<secret>" in out, (
        f"the trust header secret must be scaffolded AND masked: {out}"
    )
    # MED-3: DATABASE_URL is NOT secret-flagged. It renders as its blank example (blank =
    # zero-config SQLite default), never "DATABASE_URL=<secret>", which would flip the
    # scaffold to a broken Postgres boot against an unparseable URL.
    assert "DATABASE_URL=<secret>" not in out, ("DATABASE_URL must not render as a secret", out)
    assert "DATABASE_URL=" in out, out
    print("  config export-env scaffold (secrets masked) OK")


# --- audit verify on a fresh chain ------------------------------------------
def test_audit_verify():
    init_db()
    from app.services import audit, settings

    settings.set_many({"audit_enabled": "1"})
    audit.log(actor="cli", action="test.event", target="x")
    code, payload, _ = run_json(["audit", "verify"])
    assert code == 0 and payload["ok"] is True, payload
    print("  audit verify on a fresh chain OK")


# --- --remote on an in-process-ONLY command is cleanly refused (exit 2) ------
def test_remote_stub_rejected():
    # config get is an in-process-only operator command (P6): --remote must refuse
    # cleanly with exit 2 rather than silently running against the host.
    code, _, err = run(["--remote", "http://127.0.0.1", "config", "get", "org_name"])
    assert code == 2, code
    assert "not available over --remote" in err.lower(), err
    print("  --remote on an in-process-only command refused cleanly (exit 2) OK")


# --- init on an empty DB creates admin + one-time key; re-run no-ops --------
# init seeds users + flips the wall, so it runs in a FRESH subprocess with its
# own empty DATA_DIR (the in-process suite already populated this process's DB).
def _init_subprocess(data_dir, argv):
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
        env[k] = ""
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json"] + argv,  # noqa: RUF005
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    payload = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
    return out.returncode, payload, out.stderr


def test_init_creates_admin_then_noops():
    data_dir = tempfile.mkdtemp(prefix="loctms_cli_init_")
    code, payload, err = _init_subprocess(
        data_dir,
        [
            "init",
            "--admin-email",
            "admin@example.com",
            "--admin-password",
            "supersecret123",
            "--issue-key",
        ],
    )
    assert code == 0, (code, err)
    assert payload["action"] == "created", payload
    assert payload["admin_email"] == "admin@example.com"
    assert payload["login_wall"] is True
    key = payload.get("api_key", "")
    assert key.startswith("lsu_"), "one-time key not minted"

    # re-run: idempotent no-op, no new key
    code, payload, err = _init_subprocess(
        data_dir,
        ["init", "--admin-email", "admin@example.com", "--admin-password", "supersecret123"],
    )
    assert code == 0, (code, err)
    assert payload["action"] == "noop", payload
    assert "api_key" not in payload
    print("  init creates admin + one-time key, re-run no-ops OK")


def test_init_password_policy():
    data_dir = tempfile.mkdtemp(prefix="loctms_cli_pol_")
    code, payload, err = _init_subprocess(
        data_dir, ["init", "--admin-email", "a@b.com", "--admin-password", "short"]
    )
    assert code == 1, (code, err)
    assert "12 char" in (err + json.dumps(payload)).lower() or "at least" in err.lower(), (
        payload,
        err,
    )
    print("  init enforces the 12-char password policy OK")


def test_init_from_env():
    data_dir = tempfile.mkdtemp(prefix="loctms_cli_env_")
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    env["LOC_TMS_ADMIN_EMAIL"] = "envadmin@example.com"
    env["LOC_TMS_ADMIN_PASSWORD"] = "supersecretenv1"
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
        env[k] = ""
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json", "init", "--from-env"],
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert out.returncode == 0, out.stderr
    assert payload["admin_email"] == "envadmin@example.com", payload
    print("  init --from-env seeds from LOC_TMS_ADMIN_* OK")


# --- stale-schema guard: a DB older than the code fails friendly, not raw ----
# Reproduces the real-world footgun: the multi-org build added customer.org_id, but
# a DB last touched by run.py BEFORE that build is missing it, and read commands
# deliberately don't auto-migrate. The dispatcher must catch the SQLite
# OperationalError and print a `db migrate` hint (exit 1) instead of dumping a raw
# SQLAlchemy traceback. Runs in a SUBPROCESS with its own DB (we surgically drop a
# column, which the in-process shared engine must never see).
def test_stale_schema_friendly_error():
    import sqlite3

    data_dir = tempfile.mkdtemp(prefix="loctms_cli_stale_")
    # Build a fresh, up-to-date schema in this throwaway DB...
    code, _, err = _init_subprocess(data_dir, ["db", "init-schema"])
    assert code == 0, (code, err)
    # ...then rewind it to the PRE-multi-org shape: rebuild `customer` WITHOUT the
    # org_id column the multi-org build added (a plain DROP COLUMN is refused because
    # org_id carries a FK to organization, so recreate the table in the old shape).
    db_path = os.path.join(data_dir, "loc_tms.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            "DROP TABLE customer;"
            "CREATE TABLE customer ("
            "  id INTEGER PRIMARY KEY,"
            "  name VARCHAR NOT NULL,"
            "  created_at DATETIME"
            ");"
        )
        conn.commit()
    finally:
        conn.close()

    # A read command must NOT crash with a raw traceback: friendly hint + exit 1.
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
        env[k] = ""
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "customer", "list"],
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 1, (out.returncode, out.stderr)
    err_l = out.stderr.lower()
    assert "db migrate" in err_l, out.stderr
    assert "schema is older" in err_l and "org_id" in err_l, out.stderr
    # The friendly path must NOT leak the raw traceback (that's what -v is for).
    assert "traceback" not in err_l, "raw traceback leaked on stale-schema error!"
    assert "operationalerror" not in err_l, "raw exception leaked on stale-schema error!"

    # --json surfaces the same guard as a structured error (schema_stale: true).
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json", "customer", "list"],
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 1, (out.returncode, out.stderr)
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False and payload.get("schema_stale") is True, payload
    assert "db migrate" in payload["error"].lower(), payload

    # -v/--verbose keeps the full traceback available for debugging.
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "-v", "customer", "list"],
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 1, out.returncode
    assert "Traceback" in out.stderr and "OperationalError" in out.stderr, (
        "-v should still surface the raw traceback"
    )

    # And `db migrate` HEALS it: after migrating, the same read command succeeds.
    code, _, err = _init_subprocess(data_dir, ["db", "migrate"])
    assert code == 0, (code, err)
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "--json", "customer", "list"],
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, (out.returncode, out.stderr)
    print(
        "  stale schema -> friendly 'db migrate' hint (exit 1, no traceback); "
        "--json + -v honored; migrate heals OK"
    )


# --- doc translate <file path> -> a helpful hint, not 'invalid int value' ----
# The footgun: `doc translate C:\...\onboarding-email.txt` — passing a FILE where a
# numeric document id is expected. argparse's default 'invalid int value' is opaque;
# the custom _doc_id type must point the operator at `doc upload` first. This is a
# pure argparse-type check (no DB), so it runs in-process; argparse exits 2 (usage)
# and writes the message to stderr.
def test_doc_id_path_argument_hint():
    for path_arg in (
        r"C:\Users\alex\onboarding-email.txt",
        "/tmp/onboarding-email.txt",
        "onboarding-email.txt",
    ):
        code, _, err = run(["doc", "translate", path_arg, "--locale", "es"])
        assert code == 2, (path_arg, code, err)
        el = err.lower()
        assert "numeric document id" in el, (path_arg, err)
        assert "doc upload" in el, (path_arg, err)
        assert "invalid int value" not in el, (path_arg, err)
    # doc show / doc export take the same type -> same helpful message.
    code, _, err = run(["doc", "show", r"C:\x\file.txt"])
    assert code == 2 and "numeric document id" in err.lower(), err
    code, _, err = run(["doc", "export", "file.docx", "--locale", "es"])
    assert code == 2 and "numeric document id" in err.lower(), err
    print("  doc translate/show/export reject a file-path doc_id with an upload hint OK")


# bug #10: the bad-id hint used to hard-code "doc translate" as the suggested
# subcommand regardless of which one was actually invoked. Each subcommand's
# _doc_id binding (functools.partial(subcommand=...)) must name ITSELF. The
# subcommand-specific hint only fires for a PATH-LIKE value (the classic mistake
# of passing a filename instead of the numeric id upload returns).
def test_doc_id_hint_names_actual_subcommand():
    code, _, err = run(["doc", "show", "file.docx"])
    assert code == 2 and "doc show takes a numeric document id" in err, err
    code, _, err = run(["doc", "translate", "file.docx", "--locale", "es"])
    assert code == 2 and "doc translate takes a numeric document id" in err, err
    code, _, err = run(["doc", "export", "file.docx", "--locale", "es"])
    assert code == 2 and "doc export takes a numeric document id" in err, err
    code, _, err = run(["doc", "apply-tm", "file.docx", "es"])
    assert code == 2 and "doc apply-tm takes a numeric document id" in err, err
    print("  doc show/translate/export/apply-tm bad-id hints name their own subcommand OK")


# bug #7 (systemic): a huge integer id must fail with a clean argparse usage
# error (exit 2, no traceback) rather than a raw OverflowError from the SQLite
# binder several layers down. Spot-check across several unrelated command
# groups that all now share the bounded_id() argparse type.
def test_huge_id_rejected_cleanly():
    huge = "99999999999999999999"
    for argv in (
        ["project", "show", huge],
        ["doc", "show", huge],
        ["customer", "show", huge],
        ["engine", "show", huge],
        ["connector", "show", huge],
        ["org", "show", huge],
        ["job", "status", huge],
        ["template", "show", huge],
        ["segprofile", "show", huge],
        ["workflow", "show", huge],
    ):
        code, out, err = run(argv)
        assert code == 2, (argv, code, out, err)
        assert "overflow" not in (out + err).lower(), (argv, out, err)
        assert "traceback" not in (out + err).lower(), (argv, out, err)
    # Zero and negative ids are also not valid (positive-only autoincrement ids).
    for bad in ("0", "-5"):
        code, _, err = run(["project", "show", bad])
        assert code == 2, (bad, code, err)
    print("  huge/non-positive ids rejected with a clean usage error (no OverflowError) OK")


# H2 regression: the OTHER huge-id crash site -- the free-form "email or numeric
# id" helpers (cmd_user._resolve_user, cmd_admin._resolve_user) don't go through
# bounded_id's argparse `type=` at all (the argument is a string that MIGHT be an
# email), so they used to reach `session.get(User, int(ref))` directly and blow
# up with the same raw OverflowError. Every command that takes a user-or-id ref
# is exposed to this (user show/role/delete/reset-password/gdpr-export/...,
# member add/remove, apikey issue --user); spot-check a representative few.
def test_huge_user_ref_rejected_cleanly():
    init_db()
    huge = "99999999999999999999"

    code, out, err = run(["user", "show", huge])
    assert code == 2, (code, out, err)
    assert "overflow" not in (out + err).lower(), (out, err)
    assert "traceback" not in (out + err).lower(), (out, err)
    assert "no such user" in err.lower(), err

    code, payload, _ = run_json(
        ["project", "create", "HugeRefProj", "--targets", "fr-FR"] + _cust_arg()  # noqa: RUF005
    )
    pid = payload["id"]
    code, out, err = run(["member", "add", huge, str(pid)])
    assert code == 2, (code, out, err)
    assert "overflow" not in (out + err).lower(), (out, err)
    assert "traceback" not in (out + err).lower(), (out, err)
    assert "no such user" in err.lower(), err
    print(
        "  huge email-or-id user refs (user show, member add) rejected cleanly (no OverflowError) OK"  # noqa: E501
    )


# ============================================================================
# Phase 3 — content commands (customer/project/tm/glossary/langset + doc).
# All in-process against this suite's throwaway DATA_DIR + mock engine, so the
# doc-translate assertions are free + deterministic (the mock prefixes each
# segment with "[<locale>] ").
# ============================================================================


def _mini_tmx(pairs, src, tgt) -> bytes:
    """A tiny valid TMX for `pairs` = [(source, target), ...]."""
    tus = "".join(
        f'<tu><tuv xml:lang="{src}"><seg>{s}</seg></tuv>'
        f'<tuv xml:lang="{tgt}"><seg>{t}</seg></tuv></tu>'
        for s, t in pairs
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<tmx version="1.4"><header creationtool="verify" '
        f'srclang="{src}" datatype="plaintext" segtype="sentence"/>'
        f"<body>{tus}</body></tmx>"
    ).encode()


def test_customer_and_project_create():
    init_db()
    code, payload, _ = run_json(["customer", "create", "Globex"])
    assert code == 0 and payload["ok"] is True, payload
    cust_id = payload["id"]

    code, payload, _ = run_json(
        [
            "project",
            "create",
            "Handbook",
            "--source",
            "en-US",
            "--targets",
            "fr-FR, de-DE",
            "--customer-id",
            str(cust_id),
        ]
    )
    assert code == 0, code
    assert payload["source_locale"] == "en-US", payload
    assert set(payload["target_locales"]) == {"fr-FR", "de-DE"}, payload
    pid = payload["id"]

    # add-locale is additive + idempotent
    code, payload, _ = run_json(["project", "add-locale", str(pid), "ja-JP"])
    assert code == 0 and payload["added"] is True, payload
    code, payload, _ = run_json(["project", "add-locale", str(pid), "ja-JP"])
    assert code == 0 and payload["added"] is False, "add-locale not idempotent!"

    # show reflects the customer + locales
    code, payload, _ = run_json(["project", "show", str(pid)])
    assert code == 0 and payload["customer"] == "Globex", payload
    assert "ja-JP" in payload["target_locales"], payload
    print("  customer+project create, add-locale idempotent, show OK")


# --- issue #19: `project show` was write-only for due_date/content_type/
# segmentation_profile (set-due/set-content-type/segprofile assign were
# unverifiable). ---------------------------------------------------------------
def test_project_show_reveals_due_content_type_segprofile():
    init_db()
    code, pay, _ = run_json(["customer", "create", "WF Show Proj Cust"])
    assert code == 0, pay
    cust_id = pay["id"]
    code, pay, _ = run_json(
        ["project", "create", "Show CT/Due/Seg Proj", "--customer-id", str(cust_id)]
    )
    assert code == 0, pay
    pid = pay["id"]

    code, pay, _ = run_json(["content-type", "create", "Legal Docs"])
    assert code == 0, pay
    ct_id = pay["id"]

    code, pay, _ = run_json(["segprofile", "create", "Custom Seg"])
    assert code == 0, pay
    seg_id = pay["profile"]["id"]

    code, _, err = run(["project", "set-due", str(pid), "2026-08-01"])
    assert code == 0, err
    code, _, err = run(["project", "set-content-type", str(pid), str(ct_id)])
    assert code == 0, err
    code, _, err = run(["segprofile", "assign", str(pid), "--profile", str(seg_id)])
    assert code == 0, err

    # --json: every field set above is readable back.
    code, pay, err = run_json(["project", "show", str(pid)])
    assert code == 0, (pay, err)
    assert pay["customer_id"] == cust_id, pay
    assert pay["due_date"] == "2026-08-01", pay
    assert pay["content_type_id"] == ct_id, pay
    assert pay["content_type_name"] == "Legal Docs", pay
    assert pay["segmentation_profile_id"] == seg_id, pay
    assert pay["segmentation_profile_resolved_id"] == seg_id, pay
    assert pay["segmentation_profile_resolved_name"] == "Custom Seg", pay

    # human output: all four are visible (not the pre-fix silence).
    code, out, err = run(["project", "show", str(pid)])
    assert code == 0, err
    assert "2026-08-01" in out, out
    assert "Legal Docs" in out, out
    assert f"override #{seg_id}" in out and "Custom Seg" in out, out

    # clearing/inheriting: content type General, segprofile back to inherit chain.
    code, _, err = run(["project", "set-content-type", str(pid), "--clear"])
    assert code == 0, err
    code, _, err = run(["segprofile", "assign", str(pid)])
    assert code == 0, err
    code, pay, err = run_json(["project", "show", str(pid)])
    assert code == 0, (pay, err)
    assert pay["content_type_id"] is None and pay["content_type_name"] is None, pay
    assert pay["segmentation_profile_id"] is None, pay
    print(
        "  #19 `project show` reveals due_date/content_type/segmentation_profile/"
        "customer_id (human + --json) OK"
    )


# bug #8: --source used to sail through with no catalog validation even though
# --targets already raised a clean InvalidLocaleError for garbage codes. Both
# must now be symmetric.
def test_project_create_source_locale_validated():
    init_db()
    code, out, err = run(
        ["project", "create", "BadSource", "--source", "zz-ZZ", "--targets", "fr-FR"]
    )
    assert code == 2, (code, out, err)
    assert "zz-zz" in err.lower() and "locale" in err.lower(), err
    # a valid (but non-canonical-cased) source still works. 2026-07-14: a project
    # requires a customer when one exists — create one and pass --customer-id.
    code, ccpay, _ = run_json(["customer", "create", "GoodSourceCust"])
    assert code == 0, ccpay
    code, payload, _ = run_json(
        [
            "project",
            "create",
            "GoodSource",
            "--source",
            "EN-us",
            "--targets",
            "fr-FR",
            "--customer-id",
            str(ccpay["id"]),
        ]
    )
    assert code == 0 and payload["source_locale"].lower() == "en-us", payload
    print("  project create --source zz-ZZ -> clean usage error (symmetric with --targets) OK")


def test_langset_and_apply():
    init_db()
    code, payload, _ = run_json(["langset", "create", "Nordics", "--targets", "sv-SE,da-DK,nb-NO"])
    assert code == 0, code
    ls_id = payload["id"]
    assert set(payload["target_locales"]) == {"sv-SE", "da-DK", "nb-NO"}, payload

    code, payload, _ = run_json(["project", "create", "Site", "--targets", "sv-SE"] + _cust_arg())  # noqa: RUF005
    pid = payload["id"]
    # applying the set adds only the 2 NOT already present (sv-SE dedups)
    code, payload, _ = run_json(["project", "apply-langset", str(pid), str(ls_id)])
    assert code == 0 and payload["added"] == 2, payload
    # re-apply is idempotent (adds 0)
    code, payload, _ = run_json(["project", "apply-langset", str(pid), str(ls_id)])
    assert code == 0 and payload["added"] == 0, "apply-langset not idempotent!"

    # update replaces targets; delete needs --yes
    code, payload, _ = run_json(
        ["langset", "update", str(ls_id), "--name", "Nordics2", "--targets", "fi-FI"]
    )
    assert code == 0 and payload["name"] == "Nordics2", payload
    assert payload["target_locales"] == ["fi-FI"], payload
    code, payload, _ = run_json(["langset", "delete", str(ls_id), "--yes"])
    assert code == 0 and payload["deleted"] is True, payload
    print("  langset create/apply(idempotent)/update/delete OK")


def test_doc_upload_translate_export():
    init_db()
    code, payload, _ = run_json(
        ["project", "create", "DocFlow", "--targets", "fr-FR"] + _cust_arg()  # noqa: RUF005
    )
    pid = payload["id"]

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="loctms_doc_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Hello world. This is a test.\nGoodbye now.\n")
    out_path = path + ".fr-FR.txt"
    try:
        code, payload, _ = run_json(["doc", "upload", str(pid), path])
        assert code == 0, code
        doc_id = payload["id"]
        assert payload["fmt"] == "txt", payload

        # translate on the MOCK engine: segments get filled ("[fr-FR] ...").
        code, payload, _ = run_json(["doc", "translate", str(doc_id), "--locale", "fr-FR"])
        assert code == 0, code
        assert payload["stats"]["translated"] >= 1, payload
        assert payload["stats"]["errors"] == 0, payload

        # show reports translated == segments for fr-FR
        code, payload, _ = run_json(["doc", "show", str(doc_id)])
        loc = payload["locales"]["fr-FR"]
        assert loc["segments"] >= 1 and loc["translated"] == loc["segments"], payload

        # export writes bytes; mock output carries the locale tag -> round-trips
        code, payload, _ = run_json(
            ["doc", "export", str(doc_id), "--locale", "fr-FR", "-o", out_path]
        )
        assert code == 0 and payload["bytes"] > 0, payload
        with open(out_path, encoding="utf-8") as fh:
            body = fh.read()
        assert "[fr-FR]" in body, "mock translation missing from export!"

        # translating a locale not on the doc fails cleanly (exit 2)
        code, _, err = run(["doc", "translate", str(doc_id), "--locale", "zz-ZZ"])
        assert code == 2 and "not on document" in err.lower(), (code, err)
    finally:
        for p in (path, out_path):
            if os.path.exists(p):
                os.remove(p)
    print("  doc upload -> translate (mock) -> show -> export round-trip OK")


def test_doc_translate_dead_engine_fails_loud():
    """HARDENING: a project whose default_provider is an engine:<id> that went dead
    (disabled/deleted/keyless) must make `doc translate` FAIL LOUD — non-zero exit +
    a clear stderr message naming the ref — NOT silently substitute mock output.
    (A keyless BUILT-IN provider still degrades to mock: that is the sanctioned
    offline mode the rest of this suite relies on.)"""
    from sqlmodel import Session as _S

    from app.db import engine as _db_engine
    from app.models import Project as _Project
    from app.services import engines as _eng

    init_db()
    code, payload, _ = run_json(
        ["project", "create", "DeadEngineFlow", "--targets", "fr-FR"] + _cust_arg()  # noqa: RUF005
    )
    pid = payload["id"]
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="loctms_dead_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Hello world. This is a test.\n")
    try:
        code, payload, _ = run_json(["doc", "upload", str(pid), path])
        assert code == 0, code
        doc_id = payload["id"]
        # A valid engine ref, then disable it AFTER (deleted/disabled-after-config).
        prof = _eng.create_profile("DeadCLIDeepL", "deepl", api_key="k1")
        _eng.update_profile(prof.id, enabled=False)
        with _S(_db_engine) as s:
            proj = s.get(_Project, pid)
            proj.default_provider = f"engine:{prof.id}"
            s.add(proj)
            s.commit()
        code, _out, err = run(["doc", "translate", str(doc_id), "--locale", "fr-FR"])
        assert code != 0, f"dead engine ref must exit non-zero, got {code}"
        assert f"engine:{prof.id}" in err, f"stderr must name the dead ref: {err!r}"
        # NOT silently mock-translated: no `[fr-FR] ...` committed.
        _code2, show, _ = run_json(["doc", "show", str(doc_id)])
        loc = show["locales"]["fr-FR"]
        assert loc["translated"] == 0, f"no mock target may be committed, got {loc}"
    finally:
        if os.path.exists(path):
            os.remove(path)
    print(
        "  doc translate with a dead engine:<id> ref FAILS LOUD (non-zero exit, "
        "names ref, no mock output) OK"
    )


# --- doc apply-tm (web/CLI parity: POST /documents/{id}/apply-tm) -----------
# The web route apply_tm() (app/web/documents.py) and this CLI command both call
# the SAME app.services.content.apply_tm_to_document(); this proves the CLI path
# actually fills a segment from a 100% TM match, not just that it parses args.
def test_doc_apply_tm_fills_from_exact_match():
    init_db()
    code, payload, _ = run_json(
        ["project", "create", "ApplyTM", "--targets", "fr-FR"] + _cust_arg()  # noqa: RUF005
    )
    pid = payload["id"]

    # Seed the project TM with an EXACT-match TMX entry via `tm import` (the same
    # segment TM apply_tm_to_document() queries via tm.lookup_batch).
    fd, tmx_path = tempfile.mkstemp(suffix=".tmx", prefix="loctms_atm_")
    os.close(fd)
    with open(tmx_path, "wb") as fh:
        fh.write(_mini_tmx([("Hello world.", "Bonjour le monde.")], "en-US", "fr-FR"))

    fd, doc_path = tempfile.mkstemp(suffix=".txt", prefix="loctms_atm_doc_")
    os.close(fd)
    with open(doc_path, "w", encoding="utf-8") as fh:
        fh.write("Hello world.\n")
    try:
        code, payload, _ = run_json(
            ["tm", "import", str(pid), tmx_path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["imported"] == 1, payload

        code, payload, _ = run_json(["doc", "upload", str(pid), doc_path])
        assert code == 0, code
        doc_id = payload["id"]

        # Before apply-tm: freshly-ingested segment has no target text yet.
        code, payload, _ = run_json(["doc", "show", str(doc_id)])
        loc = payload["locales"]["fr-FR"]
        assert loc["translated"] == 0, "segment already translated before apply-tm!"

        # apply-tm fills the segment from the 100% TM match via
        # content.apply_tm_to_document — the SAME service the web route calls.
        code, payload, _ = run_json(["doc", "apply-tm", str(doc_id), "fr-FR"])
        assert code == 0 and payload["ok"] is True, payload
        assert payload["applied"] == 1, payload

        # show now reports the segment translated, and the actual DB row carries
        # the TM target text + a 100% match pct (not merely a count going up).
        code, payload, _ = run_json(["doc", "show", str(doc_id)])
        loc = payload["locales"]["fr-FR"]
        assert loc["translated"] == 1, payload
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import Segment

        with Session(engine) as s:
            seg = s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                )
            ).first()
        assert seg.target_text == "Bonjour le monde.", seg.target_text
        assert seg.tm_match_pct == 100, seg.tm_match_pct

        # re-running apply-tm is a no-op (segment already has target text; the
        # service only fills segments with NO existing target_text).
        code, payload, _ = run_json(["doc", "apply-tm", str(doc_id), "fr-FR"])
        assert code == 0 and payload["applied"] == 0, payload

        # bug #11: an invalid locale used to silently return 0 applied - the SAME
        # observable result as a real (valid-locale) empty TM, so a typo'd locale
        # was indistinguishable from "nothing to apply". It must now be a clean,
        # visible rejection instead of a quiet no-op.
        code, out, err = run(["doc", "apply-tm", str(doc_id), "zz-ZZ"])
        assert code == 2, (code, out, err)
        assert "zz-zz" in err.lower() and "locale" in err.lower(), err
    finally:
        for p in (tmx_path, doc_path):
            if os.path.exists(p):
                os.remove(p)
    print("  doc apply-tm fills a segment from a 100% TM match (web/CLI parity) OK")


# H1 regression: apply-tm used to hard-reject ANY non-catalog locale, even one
# already sitting on the project as a grandfathered TargetLocale (a legacy row,
# or one seeded by a connector's free-text config per add_target_locale's
# comment). A project that was previously translatable in e.g. "cy-GB" must stay
# translatable; a locale that is BOTH invalid AND unknown to the project (a
# genuine typo, not a grandfathered row) must still raise -- that's bug #11
# above, unchanged.
def test_apply_tm_grandfathers_noncatalog_project_locale():
    init_db()
    code, payload, _ = run_json(
        ["project", "create", "Grandfathered", "--targets", "fr-FR"] + _cust_arg()  # noqa: RUF005
    )
    pid = payload["id"]

    fd, doc_path = tempfile.mkstemp(suffix=".txt", prefix="loctms_gf_doc_")
    os.close(fd)
    with open(doc_path, "w", encoding="utf-8") as fh:
        fh.write("Hello world.\n")
    try:
        code, payload, _ = run_json(["doc", "upload", str(pid), doc_path])
        assert code == 0, code
        doc_id = payload["id"]

        # Seed a non-catalog TargetLocale directly (simulating a legacy /
        # connector-synced row that bypassed catalog validation at insert time),
        # then seed its segments the same way the service does for a new locale.
        from sqlmodel import Session

        from app.db import engine
        from app.models import Document, Project, TargetLocale
        from app.services import content, languages_catalog

        legacy_locale = "cy-GB"
        assert not languages_catalog.is_valid(legacy_locale), (
            "test locale must NOT be catalog-valid to exercise the grandfather path"
        )
        with Session(engine) as s:
            s.add(TargetLocale(project_id=pid, locale=legacy_locale))
            s.commit()
            doc = s.get(Document, doc_id)
            content._seed_locale_segments(s, doc.id, legacy_locale)

            # Service path: must SUCCEED (no raise), returning a count.
            project = s.get(Project, pid)
            applied = content.apply_tm_to_document(s, doc, project, legacy_locale)
            assert applied == 0, applied  # no TM entries seeded; just must not raise

        # CLI path: same grandfathered locale must exit 0, not a usage error.
        code, payload, err = run_json(["doc", "apply-tm", str(doc_id), legacy_locale])
        assert code == 0 and payload["ok"] is True, (code, payload, err)
        assert payload["applied"] == 0, payload

        # A garbage locale that is NEITHER catalog-valid NOR a project TargetLocale
        # must still be a clean, visible rejection (bug #11 behavior preserved).
        code, out, err = run(["doc", "apply-tm", str(doc_id), "zz-ZZ"])
        assert code == 2, (code, out, err)
        assert "zz-zz" in err.lower() and "locale" in err.lower(), err
    finally:
        if os.path.exists(doc_path):
            os.remove(doc_path)
    print(
        "  apply-tm grandfathers a non-catalog EXISTING project locale; "
        "still rejects an unknown+invalid one OK"
    )


def test_tm_import_export_roundtrip():
    init_db()

    code, payload, _ = run_json(["project", "create", "TMflow", "--targets", "fr-FR"] + _cust_arg())  # noqa: RUF005
    pid = payload["id"]

    fd, in_path = tempfile.mkstemp(suffix=".tmx", prefix="loctms_tmin_")
    os.close(fd)
    fd, out_path = tempfile.mkstemp(suffix=".tmx", prefix="loctms_tmout_")
    os.close(fd)
    os.remove(out_path)  # export refuses to overwrite unless --force
    with open(in_path, "wb") as fh:
        fh.write(
            _mini_tmx(
                [("Hello world.", "Bonjour le monde."), ("Goodbye.", "Au revoir.")],
                "en-US",
                "fr-FR",
            )
        )
    try:
        code, payload, _ = run_json(
            ["tm", "import", str(pid), in_path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["imported"] == 2, payload

        code, payload, _ = run_json(
            ["tm", "export", str(pid), out_path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["bytes"] > 0, payload

        # round-trip: import the exported file into a SECOND project -> same 2 rows
        code, payload, _ = run_json(["project", "create", "TMflow2", "--targets", "fr-FR"])
        pid2 = payload["id"]
        code, payload, _ = run_json(
            ["tm", "import", str(pid2), out_path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["imported"] == 2, "TMX round-trip lost entries!"

        # export refuses to clobber without --force
        code, _, err = run(
            ["tm", "export", str(pid), out_path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 2 and "exists" in err.lower(), (code, err)
    finally:
        for p in (in_path, out_path):
            if os.path.exists(p):
                os.remove(p)
    print("  tm import/export TMX round-trip (+overwrite guard) OK")


# bug #5: re-importing the SAME TMX must not duplicate SegmentTM rows - a second
# import reports a skip (or update, if the target text changed) instead of
# growing the TM.
def test_tm_import_idempotent():
    init_db()
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import SegmentTM

    code, payload, _ = run_json(["project", "create", "TMIdempotent", "--targets", "fr-FR"])
    pid = payload["id"]

    fd, path = tempfile.mkstemp(suffix=".tmx", prefix="loctms_tmidem_")
    os.close(fd)
    with open(path, "wb") as fh:
        fh.write(
            _mini_tmx(
                [("Hello world.", "Bonjour le monde."), ("Goodbye.", "Au revoir.")],
                "en-US",
                "fr-FR",
            )
        )
    try:
        # first import: 2 brand-new rows.
        code, payload, _ = run_json(
            ["tm", "import", str(pid), path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["imported"] == 2, payload
        assert payload.get("updated", 0) == 0 and payload.get("skipped", 0) == 0, payload
        with Session(engine) as s:
            n_rows = len(s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all())
        assert n_rows == 2, n_rows

        # re-import the SAME file: 0 new rows, 2 skipped (unchanged) - no dup rows.
        code, payload, _ = run_json(
            ["tm", "import", str(pid), path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["imported"] == 0, payload
        assert payload["skipped"] == 2, payload
        with Session(engine) as s:
            n_rows = len(s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all())
        assert n_rows == 2, f"re-import duplicated rows: {n_rows}"

        # re-import a REVISED translation for the same source: updates in place
        # (still no new row), reported as "updated" not "imported".
        with open(path, "wb") as fh:
            fh.write(
                _mini_tmx(
                    [("Hello world.", "Salut le monde."), ("Goodbye.", "Au revoir.")],
                    "en-US",
                    "fr-FR",
                )
            )
        code, payload, _ = run_json(
            ["tm", "import", str(pid), path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["imported"] == 0, payload
        assert payload["updated"] == 1 and payload["skipped"] == 1, payload
        with Session(engine) as s:
            rows = s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
        assert len(rows) == 2, f"update path duplicated rows: {len(rows)}"
        targets = {r.target_text for r in rows}
        assert "Salut le monde." in targets, targets
    finally:
        if os.path.exists(path):
            os.remove(path)
    print(
        "  tm import is idempotent: re-import skips unchanged rows, updates "
        "revised ones, never duplicates OK"
    )


def test_glossary_import_add_list():
    init_db()
    code, payload, _ = run_json(["project", "create", "Gloss", "--targets", "fr-FR"])
    pid = payload["id"]

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="loctms_gl_")
    os.close(fd)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("source_term,target_term,dnt,sense_label,context_cue\n")
        fh.write("widget,bidule,,,\n")
        fh.write("API,,1,,\n")
    try:
        code, payload, _ = run_json(
            ["glossary", "import", str(pid), path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["entries"] == 2, payload

        # add a single term
        code, payload, _ = run_json(
            [
                "glossary",
                "add",
                str(pid),
                "gadget",
                "--target-term",
                "gadget-fr",
                "--target",
                "fr-FR",
            ]
        )
        assert code == 0 and payload["source_term"] == "gadget", payload

        # list surfaces all three, with the DNT flag on API
        code, payload, _ = run_json(["glossary", "list", str(pid)])
        by_term = {e["source_term"]: e for e in payload["entries"]}
        assert set(by_term) == {"widget", "API", "gadget"}, payload
        assert by_term["API"]["dnt"] is True, "DNT flag not imported!"
        assert by_term["widget"]["target_term"] == "bidule", payload
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("  glossary import + add + list (incl. DNT) OK")


# bug #6: a CSV row carrying ONLY a sense_label (no plain/base row for that term)
# used to leave GlossaryEntry.target_term blank forever - the translation was
# "buried" in the sense row, so the grid/lookup (which read the base column
# first) showed nothing. The import must backfill the base target_term from the
# first/primary sense.
def test_glossary_sense_row_sets_base_target_term():
    init_db()
    code, payload, _ = run_json(["project", "create", "GlossSense", "--targets", "fr-FR"])
    pid = payload["id"]

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="loctms_glsense_")
    os.close(fd)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("source_term,target_term,dnt,sense_label,context_cue\n")
        # "bank" has ONLY sense rows - no plain row ever sets target_term directly.
        fh.write("bank,banque,,finance,money account\n")
        fh.write("bank,rive,,geography,river edge\n")
        # a term with both a sense row AND a later plain row: the plain row's
        # explicit value must win (never clobbered back to the sense's).
        fh.write("charge,frais,,billing,invoice fee\n")
        fh.write("charge,accusation,,,\n")
    try:
        code, payload, _ = run_json(
            ["glossary", "import", str(pid), path, "--source", "en-US", "--target", "fr-FR"]
        )
        assert code == 0 and payload["entries"] == 2 and payload["senses"] == 3, payload

        code, payload, _ = run_json(["glossary", "list", str(pid)])
        by_term = {e["source_term"]: e for e in payload["entries"]}
        # "bank": base target_term backfilled from the FIRST sense ("banque"),
        # not left blank.
        assert by_term["bank"]["target_term"] == "banque", by_term["bank"]
        assert by_term["bank"]["senses"] == 2, by_term["bank"]
        # "charge": the later plain row's explicit value wins over the sense.
        assert by_term["charge"]["target_term"] == "accusation", by_term["charge"]
        assert by_term["charge"]["senses"] == 1, by_term["charge"]
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("  glossary sense-only CSV rows backfill the base target_term OK")


# ============================================================================
# Phase 4 — connector commands (CRUD/test/rotate/schedule/sync/oauth adjuncts).
# Driven against the SAME in-repo httpx.MockTransport pattern verify_connectors.py
# uses, injected via cmd_connector._set_transport(). No live network, no real
# creds. Hermetic + ASCII-only prints.
# ============================================================================
import httpx

from app.cli import cmd_connector


def _airtable_transport():
    """Fake Airtable: test()/rotate() read a record; browse lists bases + fields."""

    def handler(req):
        path = req.url.path
        if req.method == "GET" and path.startswith("/v0/appX/Content"):
            return httpx.Response(
                200, json={"records": [{"id": "rec1", "fields": {"Notes": "Hello world"}}]}
            )
        if req.method == "GET" and path == "/v0/meta/bases":
            return httpx.Response(200, json={"bases": [{"id": "appX", "name": "Marketing"}]})
        if req.method == "GET" and path == "/v0/meta/bases/appX/tables":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": "tbl1",
                            "name": "Content",
                            "fields": [{"name": "Notes"}, {"name": "Notes (fr-FR)"}],
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def test_connector_crud_test_rotate():
    init_db()
    from app.services.connectors import store

    cmd_connector._set_transport(_airtable_transport())
    try:
        # create -> encrypted secret, auth=token
        code, payload, _ = run_json(
            [
                "connector",
                "create",
                "--kind",
                "airtable",
                "--name",
                "Marketing",
                "--config",
                "base_id=appX",
                "--config",
                "table=Content",
                "--secret",
                "api_key=patSECRET",
            ]
        )
        assert code == 0 and payload["ok"] is True, payload
        assert payload["kind"] == "airtable" and payload["auth"] == "token", payload
        cid = payload["id"]
        # the secret is encrypted at rest, never echoed
        conn = store.get(cid)
        assert "patSECRET" not in conn.secret_enc, "secret stored in plaintext!"
        assert "patSECRET" not in json.dumps(payload), "secret leaked in create output!"

        # list surfaces it
        code, payload, _ = run_json(["connector", "list"])
        assert code == 0 and any(c["id"] == cid for c in payload["connections"]), payload

        # show never reveals the secret value
        code, payload, _ = run_json(["connector", "show", str(cid)])
        assert code == 0 and payload["config"]["base_id"] == "appX", payload
        assert "patSECRET" not in json.dumps(payload), "secret leaked in show!"

        # test -> mock 200 (lists a base) -> ok
        code, payload, _ = run_json(["connector", "test", str(cid)])
        assert code == 0 and payload["ok"] is True, payload

        # browse containers (mock bases) + fields (mock table schema)
        code, payload, _ = run_json(["connector", "browse", str(cid)])
        assert code == 0 and payload["containers"][0]["id"] == "appX", payload
        code, payload, _ = run_json(
            ["connector", "browse", str(cid), "--base", "appX", "--table", "Content"]
        )
        assert code == 0 and payload["fields"] == ["Notes", "Notes (fr-FR)"], payload

        # rotate to new (working) creds -> mock test passes, secret replaced
        code, payload, _ = run_json(
            ["connector", "rotate", str(cid), "--secret", "api_key=patROTATED"]
        )
        assert code == 0 and payload["ok"] is True, payload
        assert store.dec_secret(store.get(cid).secret_enc)["api_key"] == "patROTATED"
        assert "patROTATED" not in json.dumps(payload), "rotated secret leaked!"

        # delete needs --yes
        code, payload, _ = run_json(["connector", "delete", str(cid), "--yes"])
        assert code == 0 and payload["deleted"] is True, payload
        assert store.get(cid) is None, "connection not deleted"
    finally:
        cmd_connector._set_transport(None)
    print("  connector create->list->show->test->browse->rotate->delete OK")


class _FakeStdin:
    """Minimal stand-in for sys.stdin so D3 tests can drive the `--secret KEY`
    (getpass prompt) and `--secret KEY=-` (stdin) forms without a real terminal."""

    def __init__(self, tty=False, line=""):
        self._tty = tty
        self._line = line

    def isatty(self):
        return self._tty

    def readline(self):
        return self._line


def test_connector_secret_argv_warns():
    """D3 (audit Cluster D): `--secret K=V` still works on `create`/`update`/
    `rotate` (back-compat) but must warn to stderr that the credential landed on
    argv (shell history / `ps`) - the prompt/stdin forms are preferred. The
    warning names the key but never echoes the value itself."""
    init_db()
    code, payload, err = run_json(
        [
            "connector",
            "create",
            "--kind",
            "airtable",
            "--name",
            "ArgvWarn",
            "--config",
            "base_id=appX",
            "--config",
            "table=Content",
            "--secret",
            "api_key=patARGV",
        ]
    )
    assert code == 0 and payload["ok"] is True, payload
    assert "patARGV" not in err, f"the secret value must never appear in the warning: {err!r}"
    assert "api_key" in err, f"warning should name the key passed on argv: {err!r}"
    assert "command line" in err.lower() or "argv" in err.lower(), (
        f"expected an argv/command-line warning on stderr, got: {err!r}"
    )

    # D3 review finding (LOW): the EMPTY-KEY usage-error branch must not echo
    # the raw argument either - `--secret =VALUE` carries a live value, and
    # stderr can be captured into logs/CI transcripts where argv was not.
    code2, _payload2, err2 = run_json(
        [
            "connector",
            "create",
            "--kind",
            "airtable",
            "--name",
            "ArgvWarn2",
            "--config",
            "base_id=appX",
            "--config",
            "table=Content",
            "--secret",
            "=leakedEMPTYKEY",
        ]
    )
    assert code2 != 0, "empty --secret key must be a usage error"
    assert "leakedEMPTYKEY" not in err2, (
        f"the secret value must never appear in the empty-key error: {err2!r}"
    )
    print("  connector create --secret K=V warns on stderr (value itself kept out of the warning)")
    print("  connector create --secret =VALUE errors WITHOUT echoing the value")


def test_connector_secret_prompt_and_stdin():
    """D3: `--secret KEY` (no '=') prompts via getpass; `--secret KEY=-` reads
    the value from stdin. Neither form triggers the argv-exposure warning, and
    both must land the intended value in the stored, encrypted secret - proving
    the prompt/stdin paths actually work, not just that they don't warn."""
    init_db()
    import getpass as _getpass

    from app.services.connectors import store as _cstore

    orig_stdin, orig_getpass = sys.stdin, _getpass.getpass

    # --- `--secret api_key` -> getpass prompt (simulated interactive stdin) ---
    sys.stdin = _FakeStdin(tty=True)
    _getpass.getpass = lambda prompt="": "patPROMPTED"
    try:
        code, payload, err = run_json(
            [
                "connector",
                "create",
                "--kind",
                "airtable",
                "--name",
                "PromptForm",
                "--config",
                "base_id=appX",
                "--config",
                "table=Content",
                "--secret",
                "api_key",
            ]
        )
    finally:
        sys.stdin, _getpass.getpass = orig_stdin, orig_getpass
    assert code == 0 and payload["ok"] is True, payload
    assert "command line" not in err.lower() and "argv" not in err.lower(), (
        f"the prompt form must not trigger the argv-exposure warning: {err!r}"
    )
    assert "patPROMPTED" not in err, "the prompted secret must never be echoed"
    stored = _cstore.dec_secret(_cstore.get(payload["id"]).secret_enc)
    assert stored["api_key"] == "patPROMPTED", (
        "the getpass-prompted value must be the one actually stored"
    )

    # --- `--secret api_key=-` -> read one line from stdin ---
    sys.stdin = _FakeStdin(line="patSTDIN\n")
    try:
        code2, payload2, err2 = run_json(
            [
                "connector",
                "create",
                "--kind",
                "airtable",
                "--name",
                "StdinForm",
                "--config",
                "base_id=appX",
                "--config",
                "table=Content",
                "--secret",
                "api_key=-",
            ]
        )
    finally:
        sys.stdin = orig_stdin
    assert code2 == 0 and payload2["ok"] is True, payload2
    assert "command line" not in err2.lower() and "argv" not in err2.lower(), (
        f"the stdin form must not trigger the argv-exposure warning: {err2!r}"
    )
    stored2 = _cstore.dec_secret(_cstore.get(payload2["id"]).secret_enc)
    assert stored2["api_key"] == "patSTDIN", (
        "the stdin-piped value (newline stripped) must be the one actually stored"
    )
    print(
        "  connector create --secret KEY (getpass) and KEY=- (stdin) both store "
        "correctly with no argv warning"
    )


def test_connector_secret_bare_key_noninteractive_errors():
    """D3: `--secret KEY` with no '=' and a non-interactive stdin (no tty, e.g. a
    cron job with a forgotten value) is a clean usage error, not a hang or a
    silent empty-secret create."""
    init_db()
    orig_stdin = sys.stdin
    sys.stdin = _FakeStdin(tty=False)
    try:
        code, payload, _err = run_json(
            [
                "connector",
                "create",
                "--kind",
                "airtable",
                "--name",
                "NoTTY",
                "--config",
                "base_id=appX",
                "--config",
                "table=Content",
                "--secret",
                "api_key",
            ]
        )
    finally:
        sys.stdin = orig_stdin
    assert code != 0 and payload.get("ok") is not True, payload
    print("  connector create --secret KEY with no tty and no value is a clean usage error")


def test_connector_schedule_on_off():
    init_db()
    from app.services import scheduler
    from app.services.connectors import store

    conn = store.create(
        "airtable", "Sched", {"source_field": "Notes"}, {"api_key": "patS"}, actor="cli"
    )
    # set a schedule (floored at the service minimum) then turn it off
    code, payload, _ = run_json(["connector", "schedule", str(conn.id), "--every-minutes", "1"])
    assert code == 0 and payload["every_minutes"] == scheduler.MIN_EVERY_MINUTES, payload
    assert scheduler.interval_minutes(store.get(conn.id)) == scheduler.MIN_EVERY_MINUTES

    code, payload, _ = run_json(["connector", "schedule", str(conn.id), "--off"])
    assert code == 0 and payload["every_minutes"] == 0, payload
    assert scheduler.interval_minutes(store.get(conn.id)) == 0, "schedule not cleared"
    print("  connector schedule set (floored) + off OK")


def test_connector_oauth_status():
    init_db()
    from app.services import settings
    from app.services.connectors import store

    # provider app UNconfigured by default -> configured=False
    code, payload, _ = run_json(["connector", "oauth-status", "--kind", "airtable"])
    assert code == 0 and payload["oauth_supported"] is True, payload
    assert payload["provider_app_configured"] is False, payload
    # configure the airtable client_id -> configured=True
    settings.set_many({"oauth_airtable_client_id": "cid123"})
    code, payload, _ = run_json(["connector", "oauth-status", "--kind", "airtable"])
    assert code == 0 and payload["provider_app_configured"] is True, payload
    # a token-mode connection reports auth_mode=token
    conn = store.create("airtable", "OA", {}, {"api_key": "patX"}, actor="cli")
    code, payload, _ = run_json(["connector", "oauth-status", str(conn.id)])
    assert code == 0 and payload["auth_mode"] == "token", payload
    print("  connector oauth-status: unconfigured/configured + token connection OK")


def test_connector_set_oauth_secret_roundtrip():
    init_db()
    from app.services.connectors import store

    conn = store.create("airtable", "OAuth conn", {}, {"api_key": "patLEGACY"}, actor="cli")
    assert store.auth_mode(store.dec_secret(store.get(conn.id).secret_enc)) == "token"

    fd, path = tempfile.mkstemp(suffix=".json", prefix="loctms_oauth_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "oauth": {
                    "access_token": "at_live",
                    "refresh_token": "rt_live",
                    "expires_at": 0,
                    "scope": "data.records:read",
                }
            },
            fh,
        )
    try:
        code, payload, _ = run_json(
            ["connector", "set-oauth-secret", str(conn.id), "--from-json", path]
        )
        assert code == 0 and payload["auth"] == "oauth", payload
        # the store now reports oauth mode; the injected token round-trips (encrypted)
        assert store.is_oauth(store.get(conn.id)), "connection not in oauth mode"
        secret = store.dec_secret(store.get(conn.id).secret_enc)
        assert secret["oauth"]["access_token"] == "at_live", secret
        assert "at_live" not in json.dumps(payload), "oauth token leaked in output!"

        # rotate must now REFUSE (OAuth reconnects via the provider)
        code, payload, _ = run_json(
            ["connector", "rotate", str(conn.id), "--secret", "api_key=patNEW"]
        )
        assert code == 2 and payload["ok"] is False, payload
    finally:
        os.remove(path)
    print("  connector set-oauth-secret round-trip (oauth mode) + rotate refuses OAuth OK")


def test_connector_token_regen():
    init_db()
    from app.services import settings

    before = settings.read_secret_setting("connector_api_token")
    code, payload, _ = run_json(["connector", "token-regen", "--yes"])
    assert code == 0 and payload["connector_api_token"].startswith("lsk_"), payload
    assert settings.read_secret_setting("connector_api_token") == payload["connector_api_token"]
    assert payload["connector_api_token"] != before, "token not rotated"
    print("  connector token-regen mints a fresh lsk_ token OK")


# ============================================================================
# Phase 5 — user / apikey / member commands + headless password reset.
# All in-process against this suite's throwaway DATA_DIR + mock engine. Passwords
# and API keys are asserted to authenticate/revoke through the real service
# paths (security.authenticate / apikeys.verify_key). Hermetic + ASCII-only.
# ============================================================================


def test_user_lifecycle():
    init_db()
    from app.services import security

    # create -> role -> department
    code, payload, _ = run_json(
        [
            "user",
            "create",
            "--email",
            "sam@x.com",
            "--role",
            "translator",
            "--name",
            "Sam",
            "--password",
            "initialpass123",
            "--department",
            "Sales",
        ]
    )
    assert code == 0 and payload["ok"] is True, payload
    uid = payload["id"]
    assert payload["role"] == "translator" and payload["department"] == "sales", payload
    # password authenticates via the real auth path
    assert security.authenticate("sam@x.com", "initialpass123"), "initial pw did not authenticate"

    # #4: create with a short (11-char) password is refused cleanly (nonzero exit,
    # no traceback) and creates NO user; the policy now lives in create_user itself.
    code, _, err = run(
        [
            "user",
            "create",
            "--email",
            "weak@x.com",
            "--role",
            "translator",
            "--password",
            "shortpw1234",
        ]
    )
    assert code != 0 and ("12" in err or "at least" in err.lower()), (code, err)
    assert "Traceback" not in err, err
    assert security.get_by_email("weak@x.com") is None, "weak-password user was created"

    code, payload, _ = run_json(["user", "role", "sam@x.com", "reviewer"])
    assert code == 0 and payload["new"] == "reviewer", payload
    # department resolves the user by numeric id too (str(uid)), stored lower-cased
    code, payload, _ = run_json(["user", "department", str(uid), "Marketing"])
    assert code == 0 and payload["new"] == "marketing", payload

    # unknown role rejected (exit 2), no change
    code, _, err = run(["user", "role", "sam@x.com", "wizard"])
    assert code == 2 and "unknown role" in err.lower(), (code, err)

    # disable -> auth fails; enable -> auth works again
    code, payload, _ = run_json(["user", "disable", "sam@x.com", "--yes"])
    assert code == 0 and payload["active"] is False, payload
    assert not security.authenticate("sam@x.com", "initialpass123"), (
        "disabled user still authenticates!"
    )

    # user enable is an in-process-only command: --remote must refuse cleanly
    # (exit 2) rather than fall through to _set_active and run against the host.
    code, _, err = run(["--remote", "http://127.0.0.1", "user", "enable", "sam@x.com"])
    assert code == 2 and "not available over --remote" in err.lower(), (code, err)
    assert not security.authenticate("sam@x.com", "initialpass123"), (
        "--remote user enable must not have run locally!"
    )

    code, payload, _ = run_json(["user", "enable", "sam@x.com"])
    assert code == 0 and payload["active"] is True, payload
    assert security.authenticate("sam@x.com", "initialpass123"), (
        "re-enabled user cannot authenticate"
    )

    # reset-password: new pw authenticates, OLD pw fails
    code, payload, _ = run_json(
        ["user", "reset-password", "sam@x.com", "--password", "brandnewpass456"]
    )
    assert code == 0 and payload["email"] == "sam@x.com", payload
    assert security.authenticate("sam@x.com", "brandnewpass456"), "reset pw did not authenticate"
    assert not security.authenticate("sam@x.com", "initialpass123"), (
        "OLD pw still authenticates after reset!"
    )

    # reset-password enforces the 12-char policy (exit 2), pw unchanged
    code, _, err = run(["user", "reset-password", "sam@x.com", "--password", "short"])
    assert code == 2 and ("12" in err or "at least" in err.lower()), (code, err)
    assert security.authenticate("sam@x.com", "brandnewpass456"), "rejected reset changed the pw!"

    # show reflects the current state
    code, payload, _ = run_json(["user", "show", "sam@x.com"])
    assert code == 0 and payload["role"] == "reviewer" and payload["active"] is True, payload
    print("  user create->role->department->disable/enable->reset-password (auth proven) OK")


# --- issue #19: `user show`'s human table omitted the bound customer entirely
# (--json had only the bare customer_id, no name). -----------------------------
def test_user_show_reveals_bound_customer():
    init_db()
    code, pay, _ = run_json(["customer", "create", "User Show Cust"])
    assert code == 0, pay
    cust_id = pay["id"]
    code, pay, _ = run_json(
        [
            "user",
            "create",
            "--email",
            "boundcust@x.com",
            "--role",
            "translator",
            "--password",
            "boundcustpass123",
            "--customer",
            str(cust_id),
        ]
    )
    assert code == 0, pay

    # --json: customer_id AND the resolved customer_name.
    code, pay, err = run_json(["user", "show", "boundcust@x.com"])
    assert code == 0, (pay, err)
    assert pay["customer_id"] == cust_id, pay
    assert pay["customer_name"] == "User Show Cust", pay

    # human: the customer id+name is visible (pre-fix: not shown at all).
    code, out, err = run(["user", "show", "boundcust@x.com"])
    assert code == 0, err
    assert f"#{cust_id}" in out and "User Show Cust" in out, out

    # a staff/unbound user: customer_name is None, human shows a clear "(none)".
    code, pay, _ = run_json(
        [
            "user",
            "create",
            "--email",
            "unbound@x.com",
            "--role",
            "translator",
            "--password",
            "unboundpass1234",
        ]
    )
    assert code == 0, pay
    code, pay, err = run_json(["user", "show", "unbound@x.com"])
    assert code == 0, (pay, err)
    assert pay["customer_id"] is None and pay["customer_name"] is None, pay
    code, out, err = run(["user", "show", "unbound@x.com"])
    assert code == 0, err
    assert "none" in out.lower(), out
    print("  #19 `user show` reveals the bound customer id+name (human + --json) OK")


def test_user_last_admin_guard():
    init_db()
    from app.services import security

    security.create_user("solo-admin@x.com", "Solo", "admin", "adminpassword123")
    # only admin -> disable refused (exit 1)
    code, _, err = run(["user", "disable", "solo-admin@x.com", "--yes"])
    assert code == 1 and "last active administrator" in err.lower(), (code, err)
    assert security.authenticate("solo-admin@x.com", "adminpassword123"), (
        "guard disabled the admin anyway!"
    )
    print("  user disable refuses the last active administrator OK")


def test_apikey_issue_authenticate_revoke():
    init_db()
    from app.services import apikeys, security

    security.create_user("keys@x.com", "Keys", "translator", "translatorpass123")

    # issue prints the key ONCE (human mode shows a store-now banner)
    code, out, _ = run(["apikey", "issue", "--user", "keys@x.com", "--name", "ci"])
    assert code == 0 and "lsu_" in out and "STORE THIS NOW" in out, out

    # issue --json returns the key; capture it and prove it authenticates
    code, payload, _ = run_json(["apikey", "issue", "--user", "keys@x.com", "--scopes", "docs"])
    assert code == 0, code
    full_key = payload["api_key"]
    assert full_key.startswith("lsu_"), payload
    key_id = payload["id"]
    key = apikeys.verify_key(full_key)  # the API-identity verification path
    assert key is not None and key.user_id, "issued key did not authenticate"
    assert "docs" in key.scopes and "api" in key.scopes, key.scopes

    # list surfaces the key
    code, payload, _ = run_json(["apikey", "list", "--user", "keys@x.com"])
    assert code == 0 and any(k["id"] == key_id for k in payload["keys"]), payload

    # revoke -> key no longer authenticates
    code, payload, _ = run_json(["apikey", "revoke", str(key_id)])
    assert code == 0 and payload["revoked"] is True, payload
    assert apikeys.verify_key(full_key) is None, "revoked key still authenticates!"

    # revoke-all disables the remaining key(s)
    code, payload, _ = run_json(["apikey", "revoke-all", "keys@x.com", "--yes"])
    assert code == 0, code
    assert all(not k.active for k in apikeys.list_keys(security.get_by_email("keys@x.com").id))
    print("  apikey issue(once)->authenticate->list->revoke(fails)->revoke-all OK")


def test_member_add_list_remove():
    init_db()
    from app.services import security

    security.create_user("mem@x.com", "Mem", "translator", "translatorpass123")
    code, payload, _ = run_json(["project", "create", "MemProj", "--targets", "fr-FR"])
    pid = payload["id"]

    code, payload, _ = run_json(["member", "add", "mem@x.com", str(pid)])
    assert code == 0 and payload["ok"] is True, payload
    # idempotent add
    code, payload, _ = run_json(["member", "add", "mem@x.com", str(pid)])
    assert code == 0 and "already a member" in payload["message"], payload

    code, payload, _ = run_json(["member", "list", str(pid)])
    assert code == 0 and any(m["email"] == "mem@x.com" for m in payload["members"]), payload

    code, payload, _ = run_json(["member", "remove", "mem@x.com", str(pid), "--yes"])
    assert code == 0 and payload["removed"] is True, payload
    code, payload, _ = run_json(["member", "list", str(pid)])
    assert code == 0 and not payload["members"], "member not removed"
    print("  member add(idempotent)->list->remove OK")


def test_gdpr_export_and_erase():
    init_db()
    from app.services import security

    security.create_user("subject@x.com", "Subject", "translator", "translatorpass123")
    # give them a project so the export has linked records
    run_json(["project", "create", "SubjProj", "--actor", "subject@x.com", "--targets", "fr-FR"])

    # export shape: NDJSON of {"section": ...} objects incl. the user row
    code, payload, _ = run_json(["user", "export", "subject@x.com"])
    assert code == 0 and payload["records"] >= 1, payload
    sections = payload["sections"]
    user_rows = [s for s in sections if s["section"] == "user"]
    assert user_rows and user_rows[0]["email"] == "subject@x.com", payload
    # secrets never leave
    assert "password_hash" not in json.dumps(sections), "password_hash leaked in export!"

    # -o FILE refuses to clobber an existing file without --force, same guard as
    # the binary exporters (doc export / tm export).
    fd, out_path = tempfile.mkstemp(suffix=".ndjson", prefix="loctms_p5user_")
    os.close(fd)
    try:
        code, _, err = run(["user", "export", "subject@x.com", "-o", out_path])
        assert code == 2 and "exists" in err.lower(), (code, err)
        code, payload, _ = run_json(["user", "export", "subject@x.com", "-o", out_path, "--force"])
        assert code == 0 and payload["records"] >= 1, payload
    finally:
        os.remove(out_path)

    # erase: tombstone; the original email no longer resolves + user deactivated
    code, payload, _ = run_json(["user", "erase", "subject@x.com", "--yes"])
    assert code == 0 and payload["ok"] is True, payload
    assert security.get_by_email("subject@x.com") is None, "erased user still resolves by email!"
    assert not security.authenticate("subject@x.com", "translatorpass123"), (
        "erased user still authenticates!"
    )

    # erase without --yes in --json refuses (destructive-op guard)
    security.create_user("subject2@x.com", "Subject2", "viewer", "viewerpasss1234")
    code, payload, _ = run_json(["user", "erase", "subject2@x.com"])
    assert code == 1 and payload["ok"] is False, payload
    assert security.get_by_email("subject2@x.com") is not None, "erased despite no --yes!"
    print("  gdpr export shape (no secrets) + erase tombstone (user gone) OK")


# ============================================================================
# Phase 6 — job / workflow commands (SYNCHRONOUS execution).
# All in-process against this suite's throwaway DATA_DIR + mock engine. The whole
# point of the phase is that the one-shot CLI runs jobs/workflows synchronously
# (jobs.run_sync + orchestrator.run_workflow) — NEVER submit()-and-exit — so these
# assert the work ACTUALLY happened (segments translated, Job COMPLETED not QUEUED,
# StepRun rows written) rather than merely enqueued. Hermetic + ASCII-only.
# ============================================================================


def _make_doc(pid, sentences, name_prefix="loctms_p6"):
    """Upload a tiny .txt doc into project `pid`; return its id. Caller cleans up
    via the returned path."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix=name_prefix)
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sentences) + "\n")
    code, payload, _ = run_json(["doc", "upload", str(pid), path])
    assert code == 0, (code, payload)
    return payload["id"], path


def test_job_run_translate_document_sync():
    init_db()
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Job, JobStatus, Segment

    code, payload, _ = run_json(["project", "create", "JobProj", "--targets", "fr-FR"])
    pid = payload["id"]
    doc_id, path = _make_doc(pid, ["Hello world.", "This is a test.", "Goodbye now."])
    try:
        # run the translate_document kind SYNCHRONOUSLY via jobs.run_sync
        code, payload, _ = run_json(
            [
                "job",
                "run",
                "translate_document",
                "--payload",
                f"document_id={doc_id}",
                "--payload",
                "locale=fr-FR",
            ]
        )
        assert code == 0 and payload["ok"] is True, payload
        # the Job must be COMPLETED (NOT left QUEUED — the whole risk of the phase)
        assert payload["status"] == JobStatus.COMPLETED, payload
        jid = payload["id"]
        with Session(engine) as s:
            job = s.get(Job, jid)
            assert job.status == JobStatus.COMPLETED, f"job left {job.status}, not completed!"
            segs = s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                )
            ).all()
        assert segs and all((sg.target_text or "").strip() for sg in segs), (
            "sync job left untranslated segments!"
        )
        assert payload["result"]["translated"] == len(segs), payload

        # status <id> resolves the same finished job
        code, payload, _ = run_json(["job", "status", str(jid)])
        assert code == 0 and payload["status"] == JobStatus.COMPLETED, payload

        # list surfaces it; --kind filters
        code, payload, _ = run_json(["job", "list", "--kind", "translate_document"])
        assert code == 0 and any(j["id"] == jid for j in payload["jobs"]), payload
        assert all(j["kind"] == "translate_document" for j in payload["jobs"]), payload

        # an unknown kind is a clean usage error (exit 2)
        code, _, err = run(["job", "run", "not_a_kind", "--payload", "x=1"])
        assert code == 2 and "unknown job kind" in err.lower(), (code, err)

        # #3 regression: a NONEXISTENT document_id must fail CLEANLY (job FAILED,
        # exit 1, a "no such document" message) — NOT a raw AttributeError
        # traceback from translate.pretranslate_stream deref'ing doc.project_id.
        code, payload, err = run_json(
            [
                "job",
                "run",
                "translate_document",
                "--payload",
                "document_id=99999",
                "--payload",
                "locale=fr-FR",
            ]
        )
        assert code == 1 and payload["ok"] is False, (code, payload)
        assert payload["status"] == JobStatus.FAILED, payload
        blob = (json.dumps(payload) + err).lower()
        assert "no such document" in blob, ("expected clean not-found message", payload, err)
        assert "attributeerror" not in blob and "traceback" not in blob, (
            "missing-doc job must not leak a traceback/AttributeError",
            payload,
            err,
        )
        # the Job row itself carries the clean message (not the AttributeError repr)
        with Session(engine) as s:
            failed_job = s.get(Job, payload["id"])
            assert failed_job.status == JobStatus.FAILED, failed_job.status
            assert "no such document" in (failed_job.error or "").lower(), failed_job.error
            assert "attributeerror" not in (failed_job.error or "").lower(), failed_job.error

        # #3 (bonus): a MISSING document_id payload field also fails cleanly.
        code, payload, err = run_json(
            ["job", "run", "translate_document", "--payload", "locale=fr-FR"]
        )
        assert code == 1 and payload["ok"] is False, (code, payload)
        assert "attributeerror" not in (json.dumps(payload) + err).lower(), (payload, err)

        # #3 at the SERVICE seam: pretranslate_stream itself raises a clean,
        # catchable DocumentNotFound (not an AttributeError) for a missing doc,
        # so EVERY caller is covered, not just the job path.
        from app.services import translate

        with Session(engine) as s:
            try:
                list(translate.pretranslate_stream(s, 99999, "fr-FR"))
                assert False, "pretranslate_stream(missing doc) should have raised"
            except translate.DocumentNotFound as exc:
                assert "no such document" in str(exc).lower(), str(exc)
    finally:
        if os.path.exists(path):
            os.remove(path)
    print(
        "  job run translate_document SYNC: Job COMPLETED, segments translated; "
        "status/list/unknown-kind + clean missing-doc/missing-field failure OK"
    )


def test_workflow_crud_and_run():
    init_db()
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import RunStatus, StepRun, WorkflowRun

    # create + add 2 steps
    code, payload, _ = run_json(["workflow", "create", "--name", "EN-FR 2-step"])
    assert code == 0 and payload["ok"] is True, payload
    wf_id = payload["id"]
    code, payload, _ = run_json(
        [
            "workflow",
            "add-step",
            str(wf_id),
            "--type",
            "llm_agent",
            "--name",
            "Translator",
            "--prompt",
            "Translate: {text}",
        ]
    )
    assert code == 0 and payload["order_index"] == 0, payload
    code, payload, _ = run_json(
        [
            "workflow",
            "add-step",
            str(wf_id),
            "--type",
            "llm_agent",
            "--name",
            "Reviewer",
            "--prompt",
            "Review: {text}",
        ]
    )
    assert code == 0 and payload["order_index"] == 1, payload

    # show reflects 2 steps
    code, payload, _ = run_json(["workflow", "show", str(wf_id)])
    assert code == 0 and len(payload["steps"]) == 2, payload

    # run on a fixture doc to completion (mock engine -> deterministic)
    code, payload, _ = run_json(["project", "create", "WFProj", "--targets", "fr-FR"])
    pid = payload["id"]
    doc_id, path = _make_doc(pid, ["Run me through the workflow."])
    try:
        code, payload, _ = run_json(["workflow", "run", str(doc_id), str(wf_id), "fr-FR"])
        assert code == 0 and payload["status"] == "completed", payload
        assert payload["steps"] == 2, payload
        run_id = payload["run_id"]
        # StepRun rows persisted for both steps; run COMPLETED
        with Session(engine) as s:
            run = s.get(WorkflowRun, run_id)
            assert run.status == RunStatus.COMPLETED, run.status
            srs = s.exec(
                select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
            ).all()
        assert len(srs) == 2, f"expected 2 StepRuns, got {len(srs)}"
        assert all(sr.output_text for sr in srs), "a step produced no output!"

        # delete-step drops one; show reflects it
        code, payload, _ = run_json(["workflow", "show", str(wf_id)])
        step_to_del = payload["steps"][1]["id"]
        code, payload, _ = run_json(["workflow", "delete-step", str(wf_id), str(step_to_del)])
        assert code == 0 and payload["deleted"] is True, payload
        code, payload, _ = run_json(["workflow", "show", str(wf_id)])
        assert len(payload["steps"]) == 1, payload

        # clone deep-copies the (now 1-step) workflow
        code, payload, _ = run_json(["workflow", "clone", str(wf_id)])
        assert code == 0 and payload["steps"] == 1, payload
        assert payload["id"] != wf_id, payload
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("  workflow create->add 2 steps->run (2 StepRuns, COMPLETED)->delete-step->clone OK")


# --- issue #19: `workflow show` was write-only for a step's pinned glossary,
# prompt text, and the F2/QE/TM-examples engine-layer toggles. -----------------
def test_workflow_show_reveals_stored_step_config():
    """Every field `workflow add-step` can set must round-trip through `workflow
    show` (human table AND --json) -- pre-fix, --json omitted glossary_id/name,
    prompt_text, qe_gate/threshold, adaptive_feedback(+window), doc_context_prepass,
    and tm_examples(+window) entirely, and the human table showed none of them."""
    init_db()
    code, pay, _ = run_json(["customer", "create", "WF Show Cust"])
    assert code == 0, pay
    cust_id = pay["id"]

    code, pay, _ = run_json(
        ["glossary", "module", "create", "WF Show GL", "--customer", str(cust_id)]
    )
    assert code == 0, pay
    gid = pay["id"]
    code, _, err = run(["glossary", "module", "set-pairs", str(gid), "--pair", "en-US:es-ES"])
    assert code == 0, err
    code, _, err = run(["glossary", "module", "approve", str(gid)])
    assert code == 0, err

    code, pay, _ = run_json(["workflow", "create", "--name", "WF Show Test"])
    assert code == 0, pay
    wf_id = pay["id"]
    prompt = "Translate this instruction, which is intentionally long: {text}"
    code, pay, err = run_json(
        [
            "workflow",
            "add-step",
            str(wf_id),
            "--type",
            "llm_agent",
            "--name",
            "Translator",
            "--prompt",
            prompt,
            "--glossary",
            str(gid),
            "--qe-gate",
            "--qe-threshold",
            "80",
            "--adaptive-feedback",
            "--adaptive-window",
            "4",
            "--doc-context-prepass",
            "--tm-examples-window",
            "2",
        ]
    )
    assert code == 0, (pay, err)

    # --json: every stored field is readable back, byte-exact.
    code, pay, err = run_json(["workflow", "show", str(wf_id)])
    assert code == 0, (pay, err)
    st = pay["steps"][0]
    assert st["glossary_id"] == gid, st
    assert st["glossary_name"] == "WF Show GL", st
    assert st["prompt_text"] == prompt, st
    assert st["qe_gate"] is True and st["qe_threshold"] == 80, st
    assert st["adaptive_feedback"] is True and st["adaptive_feedback_window"] == 4, st
    assert st["doc_context_prepass"] is True, st
    assert st["tm_examples"] is True and st["tm_examples_window"] == 2, st

    # human table: glossary name, qe state, opts flags, and a truncated prompt
    # snippet are all visible (not just the pre-fix idx/id/name/type/provider/branch).
    code, out, err = run(["workflow", "show", str(wf_id)])
    assert code == 0, err
    assert "WF Show GL" in out, out
    assert "on(80)" in out, out
    assert "adaptive:4" in out, out
    assert "doc-ctx" in out, out
    assert "tm-ex:2" in out, out
    assert "Translate this instruction" in out, out
    print(
        "  #19 `workflow show` reveals pinned glossary/prompt/qe-gate/adaptive/"
        "tm-examples/doc-context (human + --json) OK"
    )


# --- issue #183: `workflow show` rendered origin=unknown for a stamped workflow
# whose created_via value had no ORIGIN_LABELS entry. -------------------------
def test_workflow_show_reveals_origin_label():
    """A workflow with created_via="showcase-seed" renders its human origin label on
    the local `workflow show` path, never origin=unknown. The web + remote surfaces
    call the same origin_label, so this real-path check plus the origin_label unit
    check in verify_workflow_shapes cover every surface."""
    init_db()
    from sqlmodel import Session

    from app.db import engine
    from app.models import Workflow

    code, pay, _ = run_json(["workflow", "create", "--name", "Origin Label WF"])
    assert code == 0, pay
    wf_id = pay["id"]
    with Session(engine) as session:
        wf = session.get(Workflow, wf_id)
        wf.created_via = "showcase-seed"
        session.add(wf)
        session.commit()

    code, out, err = run(["workflow", "show", str(wf_id)])
    assert code == 0, err
    assert "origin=seeded for the showcase demo" in out, out
    assert "origin=unknown" not in out, out
    print("  #183 `workflow show` renders origin=seeded for the showcase demo OK")


def test_workflow_human_review_pause_resume():
    init_db()
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import RunStatus, StepRun, WorkflowRun

    # a 3-step workflow with a human_review in the middle
    code, payload, _ = run_json(["workflow", "create", "--name", "With human review"])
    wf_id = payload["id"]
    run_json(
        [
            "workflow",
            "add-step",
            str(wf_id),
            "--type",
            "llm_agent",
            "--name",
            "Translator",
            "--prompt",
            "Translate: {text}",
        ]
    )
    run_json(
        ["workflow", "add-step", str(wf_id), "--type", "human_review", "--name", "Human review"]
    )
    run_json(
        [
            "workflow",
            "add-step",
            str(wf_id),
            "--type",
            "llm_agent",
            "--name",
            "Finalizer",
            "--prompt",
            "Finalize: {text}",
        ]
    )

    code, payload, _ = run_json(["project", "create", "HRProj", "--targets", "fr-FR"])
    pid = payload["id"]
    doc_id, path = _make_doc(pid, ["Pause here please."])
    try:
        # run -> must PAUSE at the human step (index 1), NOT complete
        code, payload, _ = run_json(["workflow", "run", str(doc_id), str(wf_id), "fr-FR"])
        assert code == 0 and payload["status"] == "paused", payload
        assert payload["paused_step"] == 1, payload
        run_id = payload["run_id"]
        with Session(engine) as s:
            assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED, "run not paused!"

        # human-mode run should print a clear resume hint
        code, out, _ = run(["workflow", "run", str(doc_id), str(wf_id), "fr-FR"])
        # (re-running re-creates; this is a fresh run — just assert the hint text)
        assert "resume" in out.lower() and "PAUSED" in out, out

        # resume -> runs the remaining step(s) to completion
        code, payload, _ = run_json(
            [
                "workflow",
                "resume",
                str(run_id),
                "--edited-text",
                "HUMAN EDITED",
                "--locale",
                "fr-FR",
            ]
        )
        assert code == 0 and payload["status"] == "completed", payload
        with Session(engine) as s:
            wr = s.get(WorkflowRun, run_id)
            assert wr.status == RunStatus.COMPLETED, wr.status
            srs = s.exec(select(StepRun).where(StepRun.workflow_run_id == run_id)).all()
        # translator + (paused human StepRun) + finalizer = 3 StepRun rows
        assert len(srs) == 3, f"expected 3 StepRuns after resume, got {len(srs)}"

        # resuming a now-completed run is a clean usage error (exit 2)
        code, _, err = run(["workflow", "resume", str(run_id), "--locale", "fr-FR"])
        assert code == 2 and "not paused" in err.lower(), (code, err)
    finally:
        if os.path.exists(path):
            os.remove(path)
    print(
        "  workflow human_review: run PAUSES (clear resume hint) -> resume "
        "COMPLETES; resume-of-completed refused OK"
    )


def test_workflow_multi_segment_resume_failure_and_success():
    """Issue #23 (a/b) + the #2 multi-segment regression. Cover a MULTI-segment doc:
    (a) a resume whose --edited-text has the WRONG segment count is REFUSED cleanly
        (CliError, exit 1, ok:false, 'segment count' message) — the run STAYS PAUSED
        and re-resumable, NOT terminally FAILED, and NO partial targets are written
        (behavior CHANGED from the old terminal-FAIL: issue #23);
    (b) `workflow show-run` exposes the paused run's segments (count + content) for
        headless inspection;
    (c) `workflow resume --edited-segments <json>` refuses a wrong-count file (run
        untouched) and round-trips a correct-count file 1:1 to completion; and
    (d) a healthy --edited-text resume (correct SEG_JOINER count) still completes at
        exit 0 (the byte-stable happy path)."""
    init_db()
    import json as _json

    from sqlmodel import Session, select

    from app.db import engine
    from app.models import RunStatus, Segment, WorkflowRun
    from app.services import orchestrator

    def _wf_with_human():
        _code, payload, _ = run_json(["workflow", "create", "--name", "Multi-seg review"])
        wf_id = payload["id"]
        run_json(
            [
                "workflow",
                "add-step",
                str(wf_id),
                "--type",
                "llm_agent",
                "--name",
                "Translator",
                "--prompt",
                "Translate: {text}",
            ]
        )
        run_json(
            ["workflow", "add-step", str(wf_id), "--type", "human_review", "--name", "Human review"]
        )
        run_json(
            [
                "workflow",
                "add-step",
                str(wf_id),
                "--type",
                "llm_agent",
                "--name",
                "Finalizer",
                "--prompt",
                "Finalize: {text}",
            ]
        )
        return wf_id

    code, payload, _ = run_json(["project", "create", "MSResume", "--targets", "fr-FR"])
    pid = payload["id"]
    # THREE segments -> a single-string --edited-text is a 1-vs-3 count mismatch.
    doc_id, path = _make_doc(pid, ["First line.", "Second line.", "Third line."])
    # Per-process unique path: two verify_cli.py runs sharing the OS temp dir must
    # not collide on a fixed "edited_segments.json" name (observed transient
    # failure when two harness chunks overlapped, 2026-08-25).
    seg_fd, seg_json = tempfile.mkstemp(suffix=".json", prefix="loctms_p6_editedsegs_")
    os.close(seg_fd)
    try:
        # ---- (a) wrong-count --edited-text -> REFUSED, run stays PAUSED ----
        wf_id = _wf_with_human()
        code, payload, _ = run_json(["workflow", "run", str(doc_id), str(wf_id), "fr-FR"])
        assert code == 0 and payload["status"] == "paused", payload
        run_id = payload["run_id"]
        with Session(engine) as s:
            pre = sorted(
                s.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                    )
                ).all(),
                key=lambda x: x.order_index,
            )
            pre_targets = [sg.target_text for sg in pre]

        code, payload, err = run_json(
            [
                "workflow",
                "resume",
                str(run_id),
                "--edited-text",
                "ONLY ONE PART NO DELIMITER",
                "--locale",
                "fr-FR",
            ]
        )
        assert code == 1, ("refused resume must exit nonzero", code, payload, err)
        assert payload["ok"] is False, ("refused resume must report ok:false", payload)
        assert "segment count" in (payload.get("error") or "").lower(), (
            "CLI must surface the realignment error message",
            payload,
        )
        # NEW (#23): the run is left PAUSED (re-resumable), NOT failed; targets untouched.
        with Session(engine) as s:
            wr = s.get(WorkflowRun, run_id)
            assert wr.status == RunStatus.PAUSED, (
                "a refused resume must leave the run PAUSED, not FAILED",
                wr.status,
            )
            post = sorted(
                s.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                    )
                ).all(),
                key=lambda x: x.order_index,
            )
        assert [sg.target_text for sg in post] == pre_targets, (
            "refused resume must leave targets untouched (no partial/blank write)",
            pre_targets,
            [sg.target_text for sg in post],
        )

        # ---- (b) show-run exposes the paused run's segments ----
        code, payload, _ = run_json(["workflow", "show-run", str(run_id)])
        assert code == 0 and payload["ok"] and payload["paused"], payload
        assert payload["segment_count"] == 3, ("show-run must expose all 3 segments", payload)
        assert len(payload["segments"]) == 3, payload
        assert payload["paused_step"]["type"] == "human_review", payload["paused_step"]

        # ---- (c) --edited-segments: wrong-count refused, correct-count round-trips ----
        # wrong-count file -> refused BEFORE any state change (run still PAUSED)
        with open(seg_json, "w", encoding="utf-8") as fh:
            _json.dump(["only", "two"], fh)
        code, payload, _ = run_json(
            ["workflow", "resume", str(run_id), "--edited-segments", seg_json, "--locale", "fr-FR"]
        )
        assert code == 1 and payload["ok"] is False, (
            "wrong-count edited-segments must be refused",
            payload,
        )
        assert "segment count" in (payload.get("error") or "").lower(), payload
        with Session(engine) as s:
            assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED, (
                "wrong-count edited-segments must not touch run state"
            )
        # correct-count file -> round-trips 1:1 to completion (no hand-assembled delimiter)
        with open(seg_json, "w", encoding="utf-8") as fh:
            _json.dump(["SEG A", "SEG B", "SEG C"], fh)
        code, payload, _ = run_json(
            ["workflow", "resume", str(run_id), "--edited-segments", seg_json, "--locale", "fr-FR"]
        )
        assert code == 0 and payload["ok"] and payload["status"] == "completed", payload
        with Session(engine) as s:
            assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED
            got = sorted(
                s.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                    )
                ).all(),
                key=lambda x: x.order_index,
            )
        gts = [sg.target_text or "" for sg in got]
        assert "SEG A" in gts[0] and "SEG B" in gts[1] and "SEG C" in gts[2], (
            "edited-segments must map 1:1 to segments by index",
            gts,
        )

        # ---- (d) healthy --edited-text resume still completes / exit 0 (happy path) ----
        wf2 = _wf_with_human()
        code, payload, _ = run_json(["workflow", "run", str(doc_id), str(wf2), "fr-FR"])
        assert code == 0 and payload["status"] == "paused", payload
        run2 = payload["run_id"]
        good = orchestrator.SEG_JOINER.join(["EDIT ONE", "EDIT TWO", "EDIT THREE"])
        code, payload, _ = run_json(
            ["workflow", "resume", str(run2), "--edited-text", good, "--locale", "fr-FR"]
        )
        assert code == 0 and payload["ok"] is True, ("healthy resume must exit 0", payload)
        assert payload["status"] == "completed", payload
        with Session(engine) as s:
            assert s.get(WorkflowRun, run2).status == RunStatus.COMPLETED
            segs = sorted(
                s.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                    )
                ).all(),
                key=lambda x: x.order_index,
            )
        assert all((sg.target_text or "").strip() for sg in segs), (
            "healthy resume left a segment blank!"
        )
    finally:
        for p in (path, seg_json):
            if os.path.exists(p):
                os.remove(p)
    print(
        "  workflow #23 headless review: wrong-count resume REFUSED (stays PAUSED, "
        "re-resumable, no partial write); show-run exposes segments; --edited-segments "
        "wrong-count refused / correct round-trips 1:1; happy-path --edited-text OK"
    )


# ============================================================================
# Phase 7 — admin commands (semantic/srx/retention/ldap/oidc/audit/gdpr/routing/
# budget/lqa). All in-process against this suite's throwaway DATA_DIR + mock
# engine. Irreversible ops (retention purge-now, gdpr erase) are proven to refuse
# without --yes and to actually delete WITH --yes; secrets never echo. Hermetic +
# ASCII-only prints.
# ============================================================================


def test_admin_srx_validate_upload_enable():
    init_db()
    from app.services import settings, srx

    # validate the shipped default -> OK
    default_srx = os.path.join(_ROOT, "app", "resources", "default.srx")
    code, payload, _ = run_json(["admin", "srx", "validate", default_srx])
    assert code == 0 and payload["ok"] is True, payload

    # validate a BAD file -> clean failure (exit 1), nothing written
    fd, bad = tempfile.mkstemp(suffix=".srx", prefix="loctms_p7bad_")
    os.close(fd)
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("<not-srx>garbage</not-srx>")
    try:
        code, _, err = run(["admin", "srx", "validate", bad])
        assert code == 1 and "invalid srx" in err.lower(), (code, err)
        # upload of the SAME bad file is rejected too, and does not set srx_file
        code, _, err = run(["admin", "srx", "upload", bad])
        assert code == 1 and "rejected" in err.lower(), (code, err)
        assert settings.get("srx_file") == "", "bad upload set srx_file anyway!"

        # upload the VALID default -> installs the custom pointer + file on disk
        code, payload, _ = run_json(["admin", "srx", "upload", default_srx])
        assert code == 0 and payload["bytes"] > 0, payload
        assert settings.get("srx_file") == "segmentation.srx", settings.get("srx_file")
        assert srx.custom_srx_path().exists(), "custom srx not written to disk!"

        # enable flips the setting
        code, payload, _ = run_json(["admin", "srx", "enable"])
        assert code == 0 and payload["srx_enabled"] == "1", payload
        assert settings.get("srx_enabled") == "1"
    finally:
        os.remove(bad)
        settings.set_many({"srx_enabled": "0", "srx_file": ""})  # leave default
    print("  admin srx validate(good/bad) + upload(reject bad/install good) + enable OK")


def test_admin_retention_config_and_purge():
    init_db()
    from datetime import datetime, timedelta

    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Document, Project, Segment

    # config: set the window, and prove NO purge fires as a side effect of config.
    code, payload, _ = run_json(
        ["admin", "retention", "config", "--enabled", "on", "--days", "30", "--yes"]
    )
    assert code == 0 and payload["retention_enabled"] == "1", payload
    assert payload["retention_days"] == "30", payload

    # Seed one OLD document (backdated 60 days) + a fresh one; only the old ages out.
    with Session(engine) as s:
        proj = Project(name="RetProj", created_by="cli")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        old = Document(
            project_id=proj.id,
            filename="old.txt",
            fmt="txt",
            created_by="cli",
            created_at=datetime.utcnow() - timedelta(days=60),
        )
        new = Document(
            project_id=proj.id,
            filename="new.txt",
            fmt="txt",
            created_by="cli",
            created_at=datetime.utcnow(),
        )
        s.add(old)
        s.add(new)
        s.commit()
        s.refresh(old)
        s.refresh(new)
        s.add(
            Segment(
                document_id=old.id, target_locale="fr-FR", order_index=0, source_text="Old line."
            )
        )
        s.commit()
        old_id, new_id = old.id, new.id

    # config with NO purge fired: both documents still present.
    with Session(engine) as s:
        assert s.get(Document, old_id) is not None, "config must not purge!"
        assert s.get(Document, new_id) is not None

    # purge-now WITHOUT --yes in --json refuses (destructive-op guard), nothing gone
    code, payload, _ = run_json(["admin", "retention", "purge-now"])
    assert code == 1 and payload["ok"] is False, payload
    with Session(engine) as s:
        assert s.get(Document, old_id) is not None, "purge fired without --yes!"

    # purge-now --yes removes ONLY the backdated document (and its segment)
    code, payload, _ = run_json(["admin", "retention", "purge-now", "--yes"])
    assert code == 0 and payload["ok"] is True, payload
    assert payload["removed"]["documents"] >= 1, payload
    with Session(engine) as s:
        assert s.get(Document, old_id) is None, "backdated doc not purged!"
        assert s.get(Document, new_id) is not None, "fresh doc wrongly purged!"
        assert not s.exec(select(Segment).where(Segment.document_id == old_id)).all(), (
            "old segment not purged!"
        )
    run_json(["admin", "retention", "config", "--enabled", "off", "--days", "0", "--yes"])
    print("  admin retention config (no purge) + purge-now --yes removes only aged rows OK")


def test_admin_ldap_config_and_test_login():
    init_db()
    from ldap3 import MOCK_SYNC, Connection, Server

    from app.services import ldap_auth, settings

    base_dn = "dc=corp,dc=test"
    directory = {
        "uid=carol,ou=people,dc=corp,dc=test": {
            "userPassword": "carol-dir-pw",
            "mail": "carol@corp.test",
            "cn": "Carol",
            "displayName": "Carol Cee",
            "uid": "carol",
            "objectClass": "inetOrgPerson",
        },
    }

    def _factory(uri, user, password):
        # MOCK_SYNC: no sockets, no real server (mirror verify_ldap._make_conn).
        conn = Connection(
            Server("fake-dc"),
            user=user or None,
            password=password or None,
            client_strategy=MOCK_SYNC,
        )
        for dn, attrs in directory.items():
            conn.strategy.add_entry(dn, attrs)
        if not conn.bind():
            raise ConnectionError("simulated bind failure")
        return conn

    ldap_auth.set_server_factory(_factory)
    try:
        # config the connection settings (bind password stored ENCRYPTED)
        code, payload, _ = run_json(
            [
                "admin",
                "ldap",
                "config",
                "--enabled",
                "on",
                "--host",
                "ldap://fake-dc",
                "--base-dn",
                base_dn,
                "--user-filter",
                "(|(uid={username})(mail={username}))",
                "--default-role",
                "reviewer",
                "--yes",
            ]
        )
        assert code == 0 and payload["ldap_enabled"] == "1", payload
        assert ldap_auth.enabled(), "ldap not enabled after config"
        # status shows the bind password only as (set)/(unset), never plaintext
        code, out, _ = run(["admin", "ldap", "config", "--bind-password", "svc-secret-pw", "--yes"])
        assert code == 0 and "svc-secret-pw" not in out, "bind pw leaked in config output!"
        code, payload, _ = run_json(["admin", "ldap", "status"])
        assert payload["ldap_bind_password_enc"] == "(set)", payload
        assert "svc-secret-pw" not in json.dumps(payload), "bind pw leaked in status!"

        # test-login with the directory password -> provisions + succeeds
        code, payload, _ = run_json(
            ["admin", "ldap", "test-login", "carol@corp.test", "--password", "carol-dir-pw"]
        )
        assert code == 0 and payload["ok"] is True, payload
        assert payload["email"] == "carol@corp.test", payload
        assert payload["auth_source"] == "ldap", payload

        # wrong password -> clean failure (exit 1)
        code, _, err = run(
            ["admin", "ldap", "test-login", "carol@corp.test", "--password", "wrong"]
        )
        assert code == 1 and "failed" in err.lower(), (code, err)
    finally:
        ldap_auth.set_server_factory(None)
        settings.set_many({"ldap_enabled": "0"})
    print("  admin ldap config (bind pw encrypted) + test-login via MOCK_SYNC OK")


def test_admin_oidc_config_masks_secret():
    init_db()
    from app.services import settings

    code, out, _ = run(
        [
            "admin",
            "oidc",
            "config",
            "--enabled",
            "on",
            "--issuer",
            "https://idp.example.com",
            "--client-id",
            "cid",
            "--client-secret",
            "oidc-super-secret",
            "--yes",
        ]
    )
    assert code == 0 and "oidc-super-secret" not in out, "client secret leaked!"
    code, payload, _ = run_json(["admin", "oidc", "status"])
    assert payload["sso_enabled"] == "1" and payload["sso_issuer"] == "https://idp.example.com"
    assert payload["sso_client_secret"] == "(set)", payload
    assert "oidc-super-secret" not in json.dumps(payload), "client secret leaked in status!"
    # the secret is actually stored (round-trips), not dropped. R3 hardening: it is
    # now Fernet-ENCRYPTED at rest (set_secret), so read via read_secret_setting and
    # confirm the raw Setting value is ciphertext, never the plaintext.
    assert settings.read_secret_setting("sso_client_secret") == "oidc-super-secret"
    assert settings.get("sso_client_secret") != "oidc-super-secret", "stored in cleartext"
    settings.set_many({"sso_enabled": "0"})
    print("  admin oidc config stores + masks the client secret (never echoed) OK")


def test_admin_audit_export_and_verify():
    init_db()
    from app.services import audit, settings

    settings.set_many({"audit_enabled": "1"})
    audit.log(actor="cli", action="test.p7.event", target="thing")

    # verify passes on the live chain
    code, payload, _ = run_json(["admin", "audit", "verify"])
    assert code == 0 and payload["ok"] is True and payload["checked"] >= 1, payload

    # export NDJSON to a file, filtered to our action; re-verify the chain fields
    fd, out_path = tempfile.mkstemp(suffix=".ndjson", prefix="loctms_p7aud_")
    os.close(fd)
    os.remove(out_path)  # export refuses to overwrite unless --force
    try:
        code, payload, _ = run_json(
            ["admin", "audit", "export", "--action", "test.p7.event", "-o", out_path]
        )
        assert code == 0 and payload["records"] >= 1, payload
        with open(out_path, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
        assert rows and all(r["action"] == "test.p7.event" for r in rows), rows
        # each row ships prev_hash + row_hash so a downstream store can re-verify
        assert all("row_hash" in r and "prev_hash" in r for r in rows), rows

        # re-export to the SAME path refuses to clobber without --force ...
        code, _, err = run(
            ["admin", "audit", "export", "--action", "test.p7.event", "-o", out_path]
        )
        assert code == 2 and "exists" in err.lower(), (code, err)
        # ... but --force overwrites it.
        code, payload, _ = run_json(
            ["admin", "audit", "export", "--action", "test.p7.event", "-o", out_path, "--force"]
        )
        assert code == 0 and payload["records"] >= 1, payload
    finally:
        os.remove(out_path)
    print("  admin audit export NDJSON (chain fields) + verify + overwrite guard OK")


def test_admin_gdpr_export_and_erase():
    init_db()
    from app.services import security

    security.create_user("p7subj@x.com", "P7 Subject", "translator", "translatorpass123")
    run_json(["project", "create", "P7Subj", "--actor", "p7subj@x.com", "--targets", "fr-FR"])

    # export shape: NDJSON sections incl. the user row; no secrets
    code, payload, _ = run_json(["admin", "gdpr", "export", "p7subj@x.com"])
    assert code == 0 and payload["records"] >= 1, payload
    user_rows = [s for s in payload["sections"] if s["section"] == "user"]
    assert user_rows and user_rows[0]["email"] == "p7subj@x.com", payload
    assert "password_hash" not in json.dumps(payload["sections"]), "password_hash leaked!"

    # -o FILE refuses to clobber an existing file without --force, same as the
    # binary exporters (doc export / tm export).
    fd, out_path = tempfile.mkstemp(suffix=".ndjson", prefix="loctms_p7gdpr_")
    os.close(fd)
    try:
        code, _, err = run(["admin", "gdpr", "export", "p7subj@x.com", "-o", out_path])
        assert code == 2 and "exists" in err.lower(), (code, err)
        code, payload, _ = run_json(
            ["admin", "gdpr", "export", "p7subj@x.com", "-o", out_path, "--force"]
        )
        assert code == 0 and payload["records"] >= 1, payload
    finally:
        os.remove(out_path)

    # erase without --yes in --json refuses (destructive-op guard)
    code, payload, _ = run_json(["admin", "gdpr", "erase", "p7subj@x.com"])
    assert code == 1 and payload["ok"] is False, payload
    assert security.get_by_email("p7subj@x.com") is not None, "erased despite no --yes!"

    # erase --yes -> tombstone; the original email no longer resolves
    code, payload, _ = run_json(["admin", "gdpr", "erase", "p7subj@x.com", "--yes"])
    assert code == 0 and payload["ok"] is True, payload
    assert security.get_by_email("p7subj@x.com") is None, "erased user still resolves!"
    print("  admin gdpr export (no secrets) + erase tombstone (--yes) OK")


def test_admin_routing_rule_lifecycle():
    init_db()
    from sqlmodel import Session

    from app.db import engine
    from app.models import RoutingRule

    code, payload, _ = run_json(["project", "create", "RouteP", "--targets", "fr-FR"])
    pid = payload["id"]

    # add
    code, payload, _ = run_json(
        [
            "admin",
            "routing",
            "rule",
            "add",
            "--ext",
            "srt,vtt",
            "--priority",
            "10",
            "--project",
            str(pid),
            "--department",
            "Legal",
        ]
    )
    assert code == 0 and payload["ok"] is True, payload
    rid = payload["id"]
    # department is stored VERBATIM on the rule (the resolver lower-cases at match
    # time); only User.department is stored lower-cased — parity with the web form.
    assert payload["match_ext"] == "srt,vtt" and payload["department"] == "Legal", payload
    assert payload["active"] is True, payload

    # list surfaces it
    code, payload, _ = run_json(["admin", "routing", "rule", "list"])
    assert code == 0 and any(r["id"] == rid for r in payload["rules"]), payload

    # toggle -> inactive
    code, payload, _ = run_json(["admin", "routing", "rule", "toggle", str(rid)])
    assert code == 0 and payload["active"] is False, payload
    with Session(engine) as s:
        assert s.get(RoutingRule, rid).active is False

    # delete without --yes in --json refuses; with --yes removes it
    code, payload, _ = run_json(["admin", "routing", "rule", "delete", str(rid)])
    assert code == 1 and payload["ok"] is False, payload
    with Session(engine) as s:
        assert s.get(RoutingRule, rid) is not None, "deleted without --yes!"
    code, payload, _ = run_json(["admin", "routing", "rule", "delete", str(rid), "--yes"])
    assert code == 0 and payload["deleted"] is True, payload
    with Session(engine) as s:
        assert s.get(RoutingRule, rid) is None, "rule not deleted"

    # deleting a missing rule is a clean usage error (exit 2)
    code, _, err = run(["admin", "routing", "rule", "delete", "999999", "--yes"])
    assert code == 2 and "no such rule" in err.lower(), (code, err)
    print("  admin routing rule add/list/toggle/delete (--yes guard) OK")


def test_admin_budget_lifecycle():
    init_db()
    from sqlmodel import Session

    from app.db import engine
    from app.models import DepartmentBudget

    # set (create), then set again (update in place, same id)
    code, payload, _ = run_json(["admin", "budget", "set", "Support", "5000"])
    assert code == 0 and payload["created"] is True, payload
    bid = payload["id"]
    assert payload["department"] == "Support", payload  # stored verbatim
    # a case-insensitive re-set finds + UPDATES the same row (get_budget is CI)
    code, payload, _ = run_json(["admin", "budget", "set", "support", "8000"])
    assert code == 0 and payload["created"] is False, "second set must UPDATE in place"
    assert payload["id"] == bid and payload["monthly_token_limit"] == 8000, payload

    # list surfaces it with this month's spend
    code, payload, _ = run_json(["admin", "budget", "list"])
    row = next((b for b in payload["budgets"] if b["id"] == bid), None)
    assert row is not None and row["spent_this_month"] == 0, payload

    # delete without --yes refuses; with --yes removes it
    code, payload, _ = run_json(["admin", "budget", "delete", str(bid)])
    assert code == 1 and payload["ok"] is False, payload
    with Session(engine) as s:
        assert s.get(DepartmentBudget, bid) is not None, "deleted without --yes!"
    code, payload, _ = run_json(["admin", "budget", "delete", str(bid), "--yes"])
    assert code == 0 and payload["deleted"] is True, payload
    with Session(engine) as s:
        assert s.get(DepartmentBudget, bid) is None, "budget not deleted"
    print("  admin budget set(create/update)/list/delete (--yes guard) OK")


def test_admin_lqa_rulebook_get_set():
    init_db()
    from app.services import lqa

    # get on a missing pair -> empty rulebook (no crash)
    code, payload, _ = run_json(["admin", "lqa", "rulebook", "get", "en-US", "de-DE"])
    assert code == 0 and payload["rulebook"] == "", payload

    fd, path = tempfile.mkstemp(suffix=".md", prefix="loctms_p7lqa_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("FORBID cheap\nPrefer formal register.\n")
    try:
        code, payload, _ = run_json(["admin", "lqa", "rulebook", "set", "en-US", "de-DE", path])
        assert code == 0 and payload["chars"] > 0, payload
        # persisted through the real service path
        assert "FORBID cheap" in lqa.load_rulebook("en-US", "de-DE")
        # get now returns it
        code, payload, _ = run_json(["admin", "lqa", "rulebook", "get", "en-US", "de-DE"])
        assert code == 0 and "FORBID cheap" in payload["rulebook"], payload
    finally:
        os.remove(path)
    print("  admin lqa rulebook get(empty)/set/get round-trip OK")


def test_lqa_findings_machine():
    """ISSUE #56 F7 (parity): `lqa findings --machine` exposes the STORED machine scoring
    (workflow-run + ad-hoc findings + the saved score/gate) via the SAME reader the web LQA
    scorecard uses, so the reviewer surface and the machine surface reach parity. In-process
    only — `--remote` is refused (no /api twin: reading a scoring is web-only)."""
    init_db()
    from sqlmodel import Session as _S

    from app.db import engine as _db_engine
    from app.models import (
        LQAResult as _LQA,
    )
    from app.models import (
        RunStatus as _RS,
    )
    from app.models import (
        StepRun as _SR,
    )
    from app.models import (
        StepType as _ST,
    )
    from app.models import (
        Workflow as _WF,
    )
    from app.models import (
        WorkflowRun as _WR,
    )
    from app.models import (
        WorkflowStep as _WS,
    )

    code, payload, _ = run_json(
        ["project", "create", "F7Machine", "--targets", "fr-FR"] + _cust_arg()  # noqa: RUF005
    )
    pid = payload["id"]
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="loctms_f7_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Buy the widget today.\n")
    try:
        code, payload, _ = run_json(["doc", "upload", str(pid), path])
        assert code == 0, code
        doc_id = payload["id"]
        # A stored workflow LQA scoring: the DURABLE StepRun (score 93, gate failed ->
        # the headline) plus its finding row (segment_id NULL -> the findings list).
        with _S(_db_engine) as s:
            wf = _WF(name="F7 wf", scope="global")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            st = _WS(
                workflow_id=wf.id, order_index=0, name="LQA", step_type=_ST.LQA, provider="mock"
            )
            s.add(st)
            s.commit()
            s.refresh(st)
            wr = _WR(
                workflow_id=wf.id, document_id=doc_id, target_locale="fr-FR", status=_RS.COMPLETED
            )
            s.add(wr)
            s.commit()
            s.refresh(wr)
            s.add(
                _SR(
                    workflow_run_id=wr.id,
                    step_id=st.id,
                    status=_RS.COMPLETED,
                    score=93.0,
                    gate="failed",
                )
            )
            s.add(
                _LQA(
                    document_id=doc_id,
                    segment_id=None,
                    dimension="Accuracy",
                    severity="major",
                    note="run finding",
                    score=93.0,
                    target_locale="fr-FR",
                    finding_source="llm",
                    judge="claude-haiku-4-5",
                )
            )
            s.commit()
        code, payload, _ = run_json(
            ["lqa", "findings", "--document", str(doc_id), "--locale", "fr-FR", "--machine"]
        )
        assert code == 0, code
        assert payload["scoring"]["stored_score"] == 93, payload
        assert payload["scoring"]["gate"] == "needs_review", payload
        assert payload["scoring"]["regime"] == "run", payload
        assert payload["scoring"]["has_scoring"] is True, payload
        assert len(payload["run"]) == 1 and payload["run"][0]["note"] == "run finding", payload
        # --remote is refused (in-process only), exit non-zero with a clear message.
        # (--remote/--token are GLOBAL args -> before the subcommand; the CliError fires
        # BEFORE any HTTP, so the bogus URL is never dialled.)
        code, _, err = run(
            [
                "--remote",
                "http://127.0.0.1:0",
                "--token",
                "x",
                "lqa",
                "findings",
                "--document",
                str(doc_id),
                "--machine",
            ]
        )
        assert code != 0 and "in-process only" in err, (code, err)
    finally:
        os.remove(path)
    print(
        "  lqa findings --machine: stored scoring 93/needs_review + rows via the shared "
        "reader; --remote refused OK"
    )


# --- P7: org management commands (create/list/rename/show/suspend) + user org --
# These create Organization rows. Because the whole verify_* session can share one
# engine (all suites run in one `pytest -q`), each P7 test SNAPSHOTS the org ids +
# users up front and DELETES anything it created in a finally, so multi-org's
# single-Default-org / last-admin invariants stay intact cross-suite (hermetic).
def _cli_gate_on():
    from app.services import settings

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _cli_gate_off():
    from app.services import settings

    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _org_ids():
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Organization

    with Session(engine) as s:
        return {o.id for o in s.exec(select(Organization)).all()}


def _cleanup_orgs(before_ids, emails=()):
    """Delete every Organization created since `before_ids` + the named users, so a
    shared-engine session's org/admin invariants are restored."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Organization, Project, User

    with Session(engine) as s:
        for o in s.exec(select(Organization)).all():
            if o.id not in before_ids:
                # Drop the throwaway org's child rows BEFORE the org itself and
                # FLUSH between levels: project.org_id / user.org_id ->
                # organization.id are real FKs with no SQLModel Relationship for the
                # unit-of-work to order a single flush by, so an unflushed
                # child-then-parent delete lets a later select's autoflush emit
                # DELETE organization first and trip the FK (which Postgres, always
                # FK-enforcing, would reject too). Users stamped to the org are
                # dropped here regardless of `emails` so none dangles.
                for p in s.exec(select(Project).where(Project.org_id == o.id)).all():
                    s.delete(p)
                for u in s.exec(select(User).where(User.org_id == o.id)).all():
                    s.delete(u)
                s.flush()
                s.delete(o)
        s.flush()
        for email in emails:
            u = s.exec(select(User).where(User.email == email)).first()
            if u is not None:
                s.delete(u)
        s.commit()


def test_org_commands_gate_off():
    """Gate OFF (single-tenant): the org group runs freely with NO --operator —
    byte-identical to today. create/list/rename/show/suspend/unsuspend + dup/404
    guards all work in-process."""
    init_db()
    _cli_gate_off()
    before = _org_ids()
    try:
        # create
        code, pay, err = run_json(["org", "create", "--name", "CliOrg One"])
        assert code == 0 and pay["ok"] and pay["status"] == "active", (code, pay, err)
        oid = pay["id"]
        # list includes it
        code, pay, err = run_json(["org", "list"])
        assert code == 0 and any(o["id"] == oid for o in pay["organizations"]), (code, pay)
        # duplicate name -> usage error (exit 2)
        code, _pay, err = run(["org", "create", "--name", "CliOrg One"])
        assert code == 2 and "already exists" in err.lower(), (code, err)
        # rename
        code, pay, err = run_json(["org", "rename", str(oid), "--name", "CliOrg Renamed"])
        assert code == 0 and pay["name"] == "CliOrg Renamed", (code, pay)
        # show
        code, pay, err = run_json(["org", "show", str(oid)])
        assert code == 0 and pay["id"] == oid and pay["status"] == "active", (code, pay)
        # suspend (--yes to skip the prompt) then unsuspend
        code, pay, err = run_json(["org", "suspend", str(oid), "--yes"])
        assert code == 0 and pay["status"] == "suspended", (code, pay)
        code, pay, err = run_json(["org", "unsuspend", str(oid)])
        assert code == 0 and pay["status"] == "active", (code, pay)
        # show on a bogus id -> usage error
        code, _pay, err = run(["org", "show", "999999"])
        assert code == 2 and "no such organization" in err.lower(), (code, err)
        print("  P7 org (gate off): create/list/rename/show/suspend/unsuspend + dup/404 guards OK")
    finally:
        _cleanup_orgs(before)


def test_user_org_move_gate_off():
    """Gate OFF: `user org <user> <org>` moves User.org_id ONLY; the moved user's
    projects stay put (the footgun) — proven by the message + the DB."""
    init_db()
    _cli_gate_off()
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Project, User

    before = _org_ids()
    try:
        code, pay, _ = run_json(["org", "create", "--name", "MoveDest"])
        dest = pay["id"]
        # a user + a project they created (stays in the OLD org after the move)
        run(
            [
                "user",
                "create",
                "--email",
                "mover@x.com",
                "--role",
                "admin",
                "--password",
                "fixture-pass-1234",
            ]
        )
        with Session(engine) as s:
            u = s.exec(select(User).where(User.email == "mover@x.com")).first()
            old_org = u.org_id
            proj = Project(name="Mover Project", org_id=old_org, created_by="mover@x.com")
            s.add(proj)
            s.commit()
            s.refresh(proj)
            proj_id = proj.id
        code, pay, err = run_json(["user", "org", "mover@x.com", str(dest)])
        assert code == 0 and pay["new_org_id"] == dest and pay["old_org_id"] == old_org, (code, pay)
        with Session(engine) as s:
            u = s.exec(select(User).where(User.email == "mover@x.com")).first()
            assert u.org_id == dest, "user.org_id not moved"
            # FOOTGUN: the project STAYED in the old org (never re-parented).
            assert s.get(Project, proj_id).org_id == old_org, (
                "P7 footgun broken: moving a user re-parented their project"
            )
            # tidy the mover's kept project (in an org that survives) before cleanup.
            s.delete(s.get(Project, proj_id))
            s.commit()
        # a bogus destination org -> usage error
        code, _pay, err = run(["user", "org", "mover@x.com", "999999"])
        assert code == 2 and "no such organization" in err.lower(), (code, err)
        print("  P7 user org (gate off): moves org_id only; project stays in old org (footgun) OK")
    finally:
        _cleanup_orgs(before, emails=["mover@x.com"])


def test_org_commands_operator_gate():
    """Gate ON (SaaS): the org group is a cross-org operator action — an in-process
    run WITHOUT --operator is REFUSED (exit 2); --org is NOT accepted; a valid
    --operator (env key vs stored hash) authorizes it. --remote is refused too."""
    import hashlib

    init_db()
    before = _org_ids()
    _cli_gate_on()
    try:
        # (1) no --operator -> refused
        code, _pay, err = run(["org", "list"])
        assert code == 2 and "multi-org mode is on" in err.lower(), (code, err)
        code, _pay, err = run(["org", "create", "--name", "GateBlocked"])
        assert code == 2 and "--operator" in err.lower(), (code, err)
        # (1b) user org move refused without --operator too
        code, _pay, err = run(["user", "org", "someone@x.com", "1"])
        assert code == 2 and "multi-org mode is on" in err.lower(), (code, err)
        # (2) --remote is refused (in-process operator command)
        code, _pay, err = run(["--remote", "http://127.0.0.1", "--token", "lsu_x", "org", "list"])
        assert code == 2 and "not available over --remote" in err.lower(), (code, err)
        # (3) configure the operator key; a RIGHT --operator run works
        secret = "org-operator-secret-value-123"
        from app.services import settings as _s

        _s.set_many({"operator_key_hash": hashlib.sha256(secret.encode()).hexdigest()})
        _s.invalidate_cache()
        os.environ["LOC_TMS_OPERATOR_KEY"] = "wrong-key"
        code, _pay, err = run(["--operator", "org", "list"])
        assert code == 2 and "does not match" in err.lower(), (code, err)
        os.environ["LOC_TMS_OPERATOR_KEY"] = secret
        code, pay, err = run_json(["--operator", "org", "create", "--name", "OperatorMade"])
        assert code == 0 and pay["ok"], (code, pay, err)
        code, pay, err = run_json(["--operator", "org", "list"])
        assert code == 0 and any(o["name"] == "OperatorMade" for o in pay["organizations"]), pay

        # GATE R1 MED-1: verify_multiorg held the ONLY assertion in the tree that these four
        # verbs are AUDITED, inside a test that drove the back-office console. That test went
        # with the console. The audit is written by the CALLER (app/cli/cmd_org.py), not by
        # access.create_org/rename_org/set_org_active, so a service-level replacement asserts
        # nothing. Relocated here, beside the surviving caller, exactly as the user.move_org
        # assertion was. org.delete's equivalent still lives in verify_multiorg.
        from app.services import audit as _audit_org

        def _org_rows(action):
            return [r for r in _audit_org.recent(limit=200) if r.action == action]

        code, pay, err = run_json(["--operator", "org", "create", "--name", "AuditedOrg"])
        assert code == 0 and pay["ok"], (code, pay, err)
        made_id = pay["id"]  # _org_row is splatted into the payload
        assert _org_rows("org.create"), "cli org create must write an org.create audit row"
        rename = ["--operator", "org", "rename", str(made_id), "--name", "AuditedRenamed"]
        code, pay, err = run_json(rename)
        assert code == 0 and pay["ok"], (code, pay, err)
        assert _org_rows("org.rename"), "cli org rename must write an org.rename audit row"
        code, pay, err = run_json(["--operator", "org", "suspend", str(made_id), "--yes"])
        assert code == 0 and pay["ok"], (code, pay, err)
        assert _org_rows("org.suspend"), "cli org suspend must write an org.suspend audit row"
        code, pay, err = run_json(["--operator", "org", "unsuspend", str(made_id)])
        assert code == 0 and pay["ok"], (code, pay, err)
        assert _org_rows("org.unsuspend"), "cli org unsuspend must write an org.unsuspend row"
        for action in ("org.create", "org.rename", "org.suspend", "org.unsuspend"):
            assert any(f"org_id={made_id}" in (r.detail or "") for r in _org_rows(action)), (
                f"{action} audit detail must carry the org id",
                [r.detail for r in _org_rows(action)],
            )
        print("  P7 org (gate on): create/rename/suspend/unsuspend each write an audit row")

        # GATE R4 MED-2: and they write it with audit_enabled OFF, which is the property the
        # assertions above do NOT cover, because they run with the gate on and would pass on
        # an unforced call. That is exactly how the regression hid: every force=True site for
        # org.* lived in the back-office route, these CLI twins never forced, and deleting
        # the console deleted the whole class while three documents went on describing it.
        #
        # audit.py's rule is that OPERATOR verbs write unconditionally, so that whoever can
        # turn auditing off cannot then perform an un-audited install-wide write. An operator
        # here has shell access and could edit the DB directly, so this is not a wall; it is
        # the difference between a change that leaves a record by default and one that does
        # not. Assert the property, not the code shape: a future refactor that drops force=
        # goes red here rather than in a gate four rounds later.
        _s.set_many({"audit_enabled": "0"})
        _s.invalidate_cache()
        try:
            # NOT `before`: that name is the outer org-id set this test's finally hands to
            # _cleanup_orgs, and shadowing it with an int broke the cleanup. Caught by the
            # suite on the first run, which is the argument for running the owners.
            rows_before = len(_org_rows("org.create"))
            code, pay, err = run_json(["--operator", "org", "create", "--name", "ForcedOrg"])
            assert code == 0 and pay["ok"], (code, pay, err)
            assert len(_org_rows("org.create")) == rows_before + 1, (
                "cli org create wrote NO audit row with audit_enabled=0. These are operator "
                "verbs and must pass force=True; the forced sites for org.* used to live in "
                "the back-office route that was deleted 2026-09-20."
            )
            forced_id = pay["id"]

            # GATE R5 MED-2: EVERY verb in the class, not two of them. The first version of
            # this test covered create and rename, and CONTROL-EVIDENCE-MAP AU-07 was then
            # written to claim "bound by verify_cli". One mutation per verb showed the claim
            # was true for 2 of 7: dropping force= from suspend, unsuspend, delete or
            # user.move_org left this suite GREEN. A binding that covers a third of its
            # class is worse than no claim, because the claim is what stops the next person
            # checking.
            def _forced(action, argv, marker):
                seen = len(_org_rows(action))
                code, pay, err = run_json(argv)
                assert code == 0 and pay.get("ok"), (action, code, pay, err)
                rows = _org_rows(action)
                assert len(rows) == seen + 1, (
                    f"cli {marker} wrote NO audit row with audit_enabled=0. Every verb in "
                    "the operator class must pass force=True; the forced sites for these "
                    "lived in the back-office route deleted 2026-09-20, and AU-07 in "
                    "the planning notes cites THIS test as the "
                    "binding for that control."
                )
                return rows

            _forced(
                "org.rename",
                ["--operator", "org", "rename", str(forced_id), "--name", "ForcedRenamed"],
                "org rename",
            )
            _forced(
                "org.suspend",
                ["--operator", "org", "suspend", str(forced_id), "--yes"],
                "org suspend",
            )
            _forced(
                "org.unsuspend",
                ["--operator", "org", "unsuspend", str(forced_id)],
                "org unsuspend",
            )

            # user.move_org: needs a user and a destination org, both created here so the
            # verb runs for real rather than being asserted about.
            code, pay, err = run_json(["--operator", "org", "create", "--name", "ForcedDest"])
            assert code == 0 and pay["ok"], (code, pay, err)
            dest_id = pay["id"]
            # --org is required here: the gate is ACTIVE in this block, so an unscoped
            # `user create` is refused (exit 2) and the move below would then fail with
            # "no such user", which is what happened on the first run of this test.
            ucode, _upay, uerr = run_json(
                [
                    "--org",
                    str(forced_id),
                    "user",
                    "create",
                    "--email",
                    "forced-mover@x.com",
                    "--role",
                    # Not an admin: moving an org's last admin out is refused (gate
                    # multiorg-inv round 1, LOW-1), and this block is about the audit row.
                    "translator",
                    "--password",
                    "fixture-pass-1234",
                ]
            )
            assert ucode == 0, ("could not seed the user to move", ucode, uerr)
            moved = _forced(
                "user.move_org",
                ["--operator", "user", "org", "forced-mover@x.com", str(dest_id)],
                "user org",
            )
            assert any((r.target or "") == "forced-mover@x.com" for r in moved), (
                "the forced user.move_org row does not name the user",
                [r.target for r in moved],
            )

            # org.delete last: it removes the org the verbs above operated on.
            _forced(
                "org.delete",
                ["--operator", "org", "delete", str(forced_id), "--yes"],
                "org delete",
            )
            print("  P7 org: all 6 operator verbs audit with audit_enabled=0 (forced class)")
        finally:
            _s.set_many({"audit_enabled": "1"})
            _s.invalidate_cache()

        # 2026-09-20: `cli user org` is now the ONLY door for a cross-org move, since the
        # back-office console was removed. verify_multiorg used to assert this audit row
        # off the console's move route; the assertion moves HERE, to the surviving caller,
        # because the AUDIT is written by the caller and not by access.move_user_to_org.
        # Dropping it rather than relocating it would have quietly lost audit coverage for
        # an operator action that can still reparent a user.
        from sqlmodel import Session

        from app.db import engine
        from app.models import Organization as _Org
        from app.models import User as _User
        from app.services import audit as _audit
        from app.services import security as _sec

        with Session(engine) as _s3:
            src = _Org(name="MoveFrom")
            dst = _Org(name="MoveTo")
            _s3.add(src)
            _s3.add(dst)
            _s3.commit()
            src_id, dst_id = src.id, dst.id
        # #181: created IN its org (the seam refuses an org-less create under the gate).
        moved = _sec.create_user(
            "moved@cliaudit.test", "Moved", "translator", "move-pass-1234", org_id=src_id
        )
        code, pay, err = run_json(["--operator", "user", "org", "moved@cliaudit.test", str(dst_id)])
        assert code == 0 and pay["ok"], (code, pay, err)
        with Session(engine) as _s3:
            assert _s3.get(_User, moved.id).org_id == dst_id, "the move did not take effect"
        rows = [r for r in _audit.recent(limit=100) if r.action == "user.move_org"]
        assert rows, "cli user org must write a user.move_org audit row"
        assert any(f"org {src_id}->{dst_id}" in (r.detail or "") for r in rows), (
            "move audit detail must carry the org transition",
            [r.detail for r in rows],
        )
        assert any("content stays" in (r.detail or "") for r in rows), (
            "move audit detail must surface the content-stays footgun"
        )
        print(
            "  P7 org (gate on): unscoped REFUSED; --remote refused; --operator (right key) authorizes OK"  # noqa: E501
        )
        print("  P7 user org (gate on): moves org_id + audits user.move_org with the footgun note")
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
        _cli_gate_off()
        _cleanup_orgs(before)
        from app.services import settings as _s2

        _s2.set_many({"operator_key_hash": ""})
        _s2.invalidate_cache()


def test_sensitive_keys_single_source_of_truth():
    """The CLI's sensitive/secret key sets DERIVE from the service layer's, so they can never
    diverge. `settings.SENSITIVE_KEYS` and `settings.SECRET_SETTING_KEYS` are the ONE source of
    truth; `cmd_config._SENSITIVE_KEYS` / `_ENCRYPTED_SECRET_KEYS` resolve to those SAME frozensets.
    They HAD diverged on main despite the "MUST stay in sync" contract: the service SENSITIVE copy
    carried `data_export_allow_plaintext_staging` and the service SECRET copy carried
    `saml_sp_private_key_enc`, neither of which the CLI copies had -- so a CLI `config set` would
    skip a confirm (or plaintext-store the SAML SP private key) the /api route enforced. This pins
    the identity so the divergence cannot recur."""
    from app.cli import cmd_config
    from app.services import settings

    # Identity (not merely equality): the CLI reads the very same frozenset object.
    assert cmd_config._SENSITIVE_KEYS is settings.SENSITIVE_KEYS, (
        "cmd_config._SENSITIVE_KEYS diverged from settings.SENSITIVE_KEYS"
    )
    assert cmd_config._ENCRYPTED_SECRET_KEYS is settings.SECRET_SETTING_KEYS, (
        "cmd_config._ENCRYPTED_SECRET_KEYS diverged from settings.SECRET_SETTING_KEYS"
    )
    # The specific keys the main divergence had dropped are present on BOTH surfaces now.
    assert "data_export_allow_plaintext_staging" in cmd_config._SENSITIVE_KEYS
    assert "saml_sp_private_key_enc" in cmd_config._ENCRYPTED_SECRET_KEYS
    # The web.search settings left with the paid Web verification add-on: none is registered any
    # more, so `config set` of one is an unknown-key refusal (not a silently unmanaged key).
    for _k in (
        "advanced_web_search_enabled",
        "advanced_web_search_provider",
        "advanced_web_search_cost_usd",
        "advanced_web_search_max_per_unit",
        "advanced_searxng_base_url",
    ):
        assert _k not in settings.DEFAULTS, f"removed setting {_k!r} is still in settings.DEFAULTS"
    print("  sensitive-keys single-source-of-truth OK")


def main_suite():
    test_sensitive_keys_single_source_of_truth()
    test_init_schema_and_health()
    test_db_backup_overwrite_guard()
    test_config_roundtrip_plain()
    test_config_secret_masked()
    test_config_endpoint_url_redacted()
    test_config_rejects_unknown()
    test_config_apply_toml()
    test_config_export_env()
    test_audit_verify()
    test_remote_stub_rejected()
    test_init_creates_admin_then_noops()
    test_init_password_policy()
    test_init_from_env()
    test_stale_schema_friendly_error()
    test_doc_id_path_argument_hint()
    test_doc_id_hint_names_actual_subcommand()
    test_huge_id_rejected_cleanly()
    test_huge_user_ref_rejected_cleanly()
    # Phase 3 content commands
    test_customer_and_project_create()
    test_project_show_reveals_due_content_type_segprofile()
    test_project_create_source_locale_validated()
    test_langset_and_apply()
    test_doc_upload_translate_export()
    test_doc_translate_dead_engine_fails_loud()
    test_doc_apply_tm_fills_from_exact_match()
    test_apply_tm_grandfathers_noncatalog_project_locale()
    test_tm_import_export_roundtrip()
    test_tm_import_idempotent()
    test_glossary_import_add_list()
    test_glossary_sense_row_sets_base_target_term()
    # Phase 4 connector commands
    test_connector_crud_test_rotate()
    test_connector_secret_argv_warns()
    test_connector_secret_prompt_and_stdin()
    test_connector_secret_bare_key_noninteractive_errors()
    test_connector_schedule_on_off()
    test_connector_oauth_status()
    test_connector_set_oauth_secret_roundtrip()
    test_connector_token_regen()
    # Phase 5 user / apikey / member commands
    test_user_lifecycle()
    test_user_show_reveals_bound_customer()
    test_user_last_admin_guard()
    test_apikey_issue_authenticate_revoke()
    test_member_add_list_remove()
    test_gdpr_export_and_erase()
    # Phase 6 job / workflow commands
    test_job_run_translate_document_sync()
    test_workflow_crud_and_run()
    test_workflow_show_reveals_stored_step_config()
    test_workflow_show_reveals_origin_label()
    test_workflow_human_review_pause_resume()
    test_workflow_multi_segment_resume_failure_and_success()
    # Phase 7 admin commands
    test_admin_srx_validate_upload_enable()
    test_admin_retention_config_and_purge()
    test_admin_ldap_config_and_test_login()
    test_admin_oidc_config_masks_secret()
    test_admin_audit_export_and_verify()
    test_admin_gdpr_export_and_erase()
    test_admin_routing_rule_lifecycle()
    test_admin_budget_lifecycle()
    test_admin_lqa_rulebook_get_set()
    test_lqa_findings_machine()
    # P7 org management commands (create/list/rename/show/suspend + user org move)
    test_org_commands_gate_off()
    test_user_org_move_gate_off()
    test_org_commands_operator_gate()
    print("CLI OK")


if __name__ == "__main__":
    main_suite()
