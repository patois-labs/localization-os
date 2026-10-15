"""Ops verification (Phase 4-B) — suite #30.

Covers the opt-in structured JSON logging, the LOG_FORMAT/LOG_LEVEL config
defaults, and the env-vs-keyfile secret precedence. ASCII-only prints (the
cp1252 console throws on non-ASCII). Like verify_chunk1, it bootstraps a
throwaway LOC_TMS_DATA_DIR so it never touches the real data dir.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# Throwaway data dir (also gates OFF account seeding / login wall, like the rest).
from tests._bootstrap import init_env

init_env("loctms_ops_")

from app import config
from app.logging_config import (
    _JSON_HANDLER_FLAG,
    JsonFormatter,
    configure_logging,
)


def _make_record(msg="hello", level=logging.INFO, name="loc_tms", exc_info=None):
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


def test_json_formatter_basic():
    fmt = JsonFormatter()
    line = fmt.format(_make_record("a message"))
    obj = json.loads(line)  # must be valid JSON on one line
    assert "\n" not in line, "JSON log line must be single-line"
    for k in ("ts", "level", "logger", "message"):
        assert k in obj, f"missing key {k}"
    assert obj["level"] == "INFO"
    assert obj["logger"] == "loc_tms"
    assert obj["message"] == "a message"
    assert obj["ts"].endswith("Z"), "ts should be ISO-8601 UTC (Z suffix)"
    print("  json formatter: single-line JSON with ts/level/logger/message OK")


def test_json_formatter_args():
    # %-style args must be interpolated into `message`.
    fmt = JsonFormatter()
    rec = logging.LogRecord(
        "loc_tms", logging.WARNING, __file__, 1, "count=%s state=%s", (5, "queued"), None
    )
    obj = json.loads(fmt.format(rec))
    assert obj["message"] == "count=5 state=queued", obj["message"]
    assert obj["level"] == "WARNING"
    print("  json formatter: %-args interpolated + level=WARNING OK")


def test_json_formatter_exception():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        rec = _make_record("failed", level=logging.ERROR, exc_info=sys.exc_info())
    obj = json.loads(fmt.format(rec))
    assert "traceback" in obj, "exception record must carry a traceback"
    assert "ValueError" in obj["traceback"], "traceback should name the exception"
    assert "boom" in obj["traceback"]
    assert obj.get("exc_type") == "ValueError"
    print("  json formatter: exception record includes traceback OK")


def test_json_formatter_extra():
    # extra={...} fields land at the top level, JSON-safe.
    fmt = JsonFormatter()
    rec = _make_record("with extra")
    rec.job_id = 42
    rec.detail = {"nested": True}
    obj = json.loads(fmt.format(rec))
    assert obj["job_id"] == 42
    assert obj["detail"] == {"nested": True}
    print("  json formatter: extra fields surfaced cleanly OK")


def test_configure_text_is_noop():
    # Default LOG_FORMAT=text: configure_logging() must NOT add a JSON handler.
    assert config.LOG_FORMAT == "text", "test env must not set LOG_FORMAT"
    root = logging.getLogger()
    before = list(root.handlers)
    configure_logging()
    after = list(root.handlers)
    assert after == before, "text mode must not touch root handlers"
    assert not any(getattr(h, _JSON_HANDLER_FLAG, False) for h in root.handlers), (
        "no JSON handler should be installed in text mode"
    )
    print("  configure_logging(text): no-op, no JSON handler OK")


def test_configure_json_idempotent():
    # Force JSON mode and confirm one handler, not stacked on repeat calls.
    root = logging.getLogger()
    saved_fmt = config.LOG_FORMAT
    config.LOG_FORMAT = "json"
    try:
        configure_logging()
        configure_logging()  # second call must not stack
        json_handlers = [h for h in root.handlers if getattr(h, _JSON_HANDLER_FLAG, False)]
        assert len(json_handlers) == 1, f"expected exactly 1 JSON handler, got {len(json_handlers)}"
        assert isinstance(json_handlers[0].formatter, JsonFormatter)
        print("  configure_logging(json): idempotent, exactly one handler OK")
    finally:
        # Restore clean state so we don't leak a JSON handler into other checks.
        for h in [h for h in root.handlers if getattr(h, _JSON_HANDLER_FLAG, False)]:
            root.removeHandler(h)
        config.LOG_FORMAT = saved_fmt


def test_http_client_request_lines_quieted_302():
    # #302: httpx logs the full request URL at INFO. In EVERY format configure_logging
    # floors httpx/httpcore at WARNING so no request line reaches the process log.
    saved_fmt = config.LOG_FORMAT
    root = logging.getLogger()
    try:
        for fmt in ("text", "json"):
            for n in ("httpx", "httpcore"):
                logging.getLogger(n).setLevel(logging.NOTSET)
            config.LOG_FORMAT = fmt
            configure_logging()
            for n in ("httpx", "httpcore"):
                assert not logging.getLogger(n).isEnabledFor(logging.INFO), (fmt, n)
        # Never LOWERS a level an operator set higher.
        logging.getLogger("httpx").setLevel(logging.ERROR)
        configure_logging()
        assert logging.getLogger("httpx").level == logging.ERROR
    finally:
        for h in [h for h in root.handlers if getattr(h, _JSON_HANDLER_FLAG, False)]:
            root.removeHandler(h)
        config.LOG_FORMAT = saved_fmt
    print("  configure_logging: httpx/httpcore request lines quieted in text + json OK")


_ACCESS_SECRETS = (  # pregate: allow-secret fake log-probe values
    "CONFIDENTIAL customer sentence",
    "CONFIDENTIAL+customer+sentence",
    "OAUTHCODE302",
    "OAUTHSTATE302",
    "INVITETOKEN302abcdef",
)
_ACCESS_PATHS = (
    "/concordance?project_id=1&q=CONFIDENTIAL+customer+sentence&source=en&target=fr",
    "/api/tm?q=CONFIDENTIAL%20customer%20sentence",
    "/api/projects/3/keys?q=CONFIDENTIAL customer sentence",
    "/connectors/oauth/callback?code=OAUTHCODE302&state=OAUTHSTATE302",
    "/auth/oidc/callback?code=OAUTHCODE302&state=OAUTHSTATE302",
    "/auth/accept/INVITETOKEN302abcdef",
)


def test_access_log_drops_query_and_invite_token_302():
    # #302 gate round 1: uvicorn's access log prints the full path + query at INFO, so
    # the editor's /concordance?q=<text>, /api/tm?q=, the keys API search, OAuth/OIDC
    # callback code+state and the /auth/accept/<token> invite token reached the log in
    # BOTH formats. configure_logging installs a filter on uvicorn.access in every
    # format; run.py's JSON dictConfig carries it too.
    import inspect

    from uvicorn.logging import AccessFormatter
    from uvicorn.protocols.http import h11_impl

    from app.logging_config import AccessLogRedactFilter

    # Pin the record shape the filter rewrites (args[2] = path with query).
    assert "'%s - \"%s %s HTTP/%s\" %d'" in inspect.getsource(h11_impl), "uvicorn shape moved"

    access = logging.getLogger("uvicorn.access")
    saved = (config.LOG_FORMAT, list(access.filters), access.level, access.propagate)
    root = logging.getLogger()
    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    try:
        for fmt in ("text", "json"):
            for f in list(access.filters):
                access.removeFilter(f)
            config.LOG_FORMAT = fmt
            configure_logging()
            configure_logging()  # idempotent: one filter
            assert sum(isinstance(f, AccessLogRedactFilter) for f in access.filters) == 1
            cap = _Cap()
            cap.setFormatter(
                AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s')
                if fmt == "text"
                else JsonFormatter()
            )
            access.addHandler(cap)
            access.setLevel(logging.INFO)
            lines.clear()
            try:
                for path in _ACCESS_PATHS:
                    access.info(
                        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", path, "1.1", 200
                    )
            finally:
                access.removeHandler(cap)
            assert len(lines) == len(_ACCESS_PATHS), lines
            for line in lines:
                for s in _ACCESS_SECRETS:
                    assert s not in line, f"[{fmt}] {s!r} leaked: {line}"
                assert "200" in line, line
            assert "/concordance?[query redacted]" in lines[0], lines[0]
            assert "/auth/accept/[redacted]" in lines[-1], lines[-1]
        # run.py's uvicorn dictConfig (json and text) attaches the same filter.
        import run as runmod

        for fmt in ("json", "text"):
            config.LOG_FORMAT = fmt
            cfg = runmod._uvicorn_log_config()
            assert "access_redact" in cfg["loggers"]["uvicorn.access"]["filters"], (fmt, cfg)
            assert cfg["filters"]["access_redact"]["()"].endswith("AccessLogRedactFilter")
    finally:
        for h in [h for h in root.handlers if getattr(h, _JSON_HANDLER_FLAG, False)]:
            root.removeHandler(h)
        config.LOG_FORMAT, filters, level, prop = saved
        for f in list(access.filters):
            access.removeFilter(f)
        for f in filters:
            access.addFilter(f)
        access.setLevel(level)
        access.propagate = prop
    print("  uvicorn access log: query strings + invite token redacted in text + json OK")


_INVITE_402 = "INVITETOKEN402abcdef"  # pregate: allow-secret fake log-probe value


def test_access_log_redacts_prefixed_invite_402():
    # #402: the redactor was anchored ^/auth/accept/, so an app served behind a
    # root_path/proxy prefix logged /prefix/auth/accept/<token> with the complete
    # usable token. uvicorn builds the logged path as quote(root_path + unquoted
    # request path) (uvicorn.protocols.utils.get_path_with_query_string), so the
    # credential segment can sit at ANY depth; the match is now the auth/accept
    # segment boundary. Separators in the logged path are literal '/' (uvicorn
    # unquotes then requotes), so encoded-path variants normalize into these forms.
    from app.logging_config import redact_access_path

    cases = [
        # (logged path, expected) -- regression: unprefixed still redacts
        (f"/auth/accept/{_INVITE_402}", "/auth/accept/[redacted]"),
        # root_path / reverse-proxy prefix
        (f"/prefix/auth/accept/{_INVITE_402}", "/prefix/auth/accept/[redacted]"),
        # deeper mount depth, and empty-segment normalization
        (f"/a/b/c/auth/accept/{_INVITE_402}", "/a/b/c/auth/accept/[redacted]"),
        (f"//auth/accept/{_INVITE_402}", "//auth/accept/[redacted]"),
        # prefixed credential + query string both redacted
        (
            f"/prefix/auth/accept/{_INVITE_402}?next=%2Fx",
            "/prefix/auth/accept/[redacted]?[query redacted]",
        ),
        # percent-encoded characters inside the token: the whole segment goes
        ("/prefix/auth/accept/Invt%2Bken402", "/prefix/auth/accept/[redacted]"),
        # a client %2F-encoded separator arrives unquoted-then-requoted, so the
        # LOGGED path carries a literal '/': the token continues past the slash
        ("/prefix/auth/accept/Invt/ken402", "/prefix/auth/accept/[redacted]"),
        # malformed double slash after the boundary: the route 404s so the token
        # is still UNCONSUMED -- the whole remainder must not reach the log
        (f"/prefix/auth/accept//{_INVITE_402}", "/prefix/auth/accept/[redacted]"),
        # trailing junk after the token (never routes; same blanking rule)
        (f"/auth/accept/{_INVITE_402}/trailing", "/auth/accept/[redacted]"),
        # negatives: no auth/accept SEGMENT boundary => path untouched
        (f"/accept/{_INVITE_402}", f"/accept/{_INVITE_402}"),
        (f"/auth/accepted/{_INVITE_402}", f"/auth/accepted/{_INVITE_402}"),
        (f"/xauth/accept/{_INVITE_402}", f"/xauth/accept/{_INVITE_402}"),
        ("/auth/accept/", "/auth/accept/[redacted]"),  # empty token: boundary still blanks
    ]
    for path, want in cases:
        got = redact_access_path(path)
        assert got == want, (path, got, want)
    print("  #402 redact_access_path: auth/accept boundary matched at any mount depth OK")


def test_prefixed_invite_never_in_access_log_402():
    # #402 repro as a regression, exercising the REAL check end to end: a real
    # admin invitation served under TestClient(root_path='/prefix') (the route
    # answers 200), then a standard uvicorn five-argument access record carrying
    # the prefixed path through the production filter chain (configure_logging
    # installs AccessLogRedactFilter on uvicorn.access). The token must not
    # survive in either format. The anchored pre-#402 regex leaves it in.
    from fastapi.testclient import TestClient
    from uvicorn.protocols.utils import get_path_with_query_string

    from app.db import init_db
    from app.main import app
    from app.services import security

    init_db()
    inv = security.create_invite("invite402@example.com", "admin", "seed@example.com")
    client = TestClient(app, root_path="/prefix")
    resp = client.get(f"/prefix/auth/accept/{inv.token}")
    assert resp.status_code == 200, resp.status_code
    assert inv.token in resp.text, "fixture sanity: the accept page carries the token"

    # The exact string uvicorn would log for that request (root_path + requoted
    # path, empty query string here).
    logged = get_path_with_query_string(
        {"path": f"/prefix/auth/accept/{inv.token}", "query_string": b""}
    )
    assert inv.token in logged

    access = logging.getLogger("uvicorn.access")
    saved = (config.LOG_FORMAT, list(access.filters), access.level, access.propagate)
    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    try:
        for fmt in ("text", "json"):
            config.LOG_FORMAT = fmt
            configure_logging()  # idempotent; installs the production filter
            cap = _Cap()
            access.addHandler(cap)
            access.setLevel(logging.INFO)
            lines.clear()
            try:
                access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", logged, "1.1", 200)
            finally:
                access.removeHandler(cap)
            assert len(lines) == 1, lines
            assert inv.token not in lines[0], f"[{fmt}] token leaked: {lines[0]}"
            assert "/prefix/auth/accept/[redacted]" in lines[0], lines[0]
            assert "200" in lines[0], lines[0]
    finally:
        root = logging.getLogger()
        for h in [h for h in root.handlers if getattr(h, _JSON_HANDLER_FLAG, False)]:
            root.removeHandler(h)
        config.LOG_FORMAT, filters, level, prop = saved
        for f in list(access.filters):
            access.removeFilter(f)
        for f in filters:
            access.addFilter(f)
        access.setLevel(level)
        access.propagate = prop
    print("  #402 prefixed invite: real invite + real filter, token gone in text + json OK")


def test_config_defaults():
    assert config.LOG_FORMAT == "text", config.LOG_FORMAT
    assert config.LOG_LEVEL == "INFO", config.LOG_LEVEL
    print("  config: LOG_FORMAT=text / LOG_LEVEL=INFO defaults OK")


# --- Secret precedence (subprocess: config caches at import) ---
# A fresh subprocess with its own env + its own throwaway DATA_DIR is the clean
# way to assert precedence, since app.config resolves secrets once at import.

_PROBE = (
    "import json; from app import config; "
    "print(json.dumps({"
    "'ss': config.SESSION_SECRET, 'ek': config.ENCRYPTION_KEY, "
    "'sss': config.SESSION_SECRET_SOURCE, 'eks': config.ENCRYPTION_KEY_SOURCE, "
    # secret_source() is the SAME helper web/admin.py routes its key_source display
    # through, so asserting it here covers that display contract without a render.
    "'ssrc': config.secret_source('session'), "
    "'eksrc': config.secret_source('encryption')}))"
)


_SS_KEYFILE = ".session_secret"
_EK_KEYFILE = ".enc_key"

# Stable substrings of the two boot-time warnings _secret() emits (config.py). The
# subprocess resolves secrets at import, so the warning lands on the child's stderr.
_DIFFER_WARN = "OUT-RANKS the env var"
_EMBEDDED_WARN = "DELIBERATELY IGNORED in embedded mode"
_ROW5_WARN = "The prefixed spelling WINS"  # both spellings set to differing values


def _probe_config(extra_env, data_dir=None):
    env = dict(os.environ)
    # BLANK (do NOT pop) both spellings. config.py calls load_dotenv(override=False),
    # which only skips keys ALREADY PRESENT in the environment -- a POPPED key is
    # absent, so load_dotenv RE-INJECTS it from the repo .env (e.g. a SESSION_SECRET=
    # line our own docs suggest), flaking the keyfile/embedded cases. Setting each to
    # "" keeps the key present (load_dotenv leaves it blank) and config._get treats ""
    # as unset for both spellings. A caller's extra_env still overrides below.
    for k in (
        "LOC_TMS_SESSION_SECRET",
        "LOC_TMS_ENCRYPTION_KEY",
        "SESSION_SECRET",
        "ENCRYPTION_KEY",
    ):
        env[k] = ""
    # A caller-supplied dir enables the SAME-dir provenance case (a fresh mkdtemp
    # per call, the default, cannot catch a provenance flip across two boots).
    env["LOC_TMS_DATA_DIR"] = data_dir or tempfile.mkdtemp(prefix="loctms_ops_sub_")
    env.update(extra_env)
    proc = subprocess.run([sys.executable, "-c", _PROBE], env=env, cwd=_ROOT, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    r = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    r["_stderr"] = proc.stderr.decode(errors="replace")
    return r


def _write_keyfiles(d, ss_val, ek_val):
    with open(os.path.join(d, _SS_KEYFILE), "w", encoding="utf-8") as fh:
        fh.write(ss_val)
    with open(os.path.join(d, _EK_KEYFILE), "w", encoding="utf-8") as fh:
        fh.write(ek_val)


def test_secret_env_wins():
    r = _probe_config(
        {
            "LOC_TMS_SESSION_SECRET": "env-session-xyz-0123456789abcdef",
            "LOC_TMS_ENCRYPTION_KEY": "env-enc-abc",
        }
    )
    assert r["ss"] == "env-session-xyz-0123456789abcdef", r
    assert r["ek"] == "env-enc-abc", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    print("  secrets: env vars WIN over keyfiles, source='env' OK")


def test_secret_keyfile_fallback():
    r = _probe_config({})  # neither env var set
    assert r["sss"] == "keyfile", r
    assert r["eks"] == "keyfile", r
    assert r["ss"] and r["ek"], "keyfile fallback must still produce values"
    print("  secrets: keyfile fallback works when env unset, source='keyfile' OK")


def test_secret_source_is_recorded():
    # In THIS process (no env secrets, throwaway data dir), both came from keyfile.
    assert config.SESSION_SECRET_SOURCE == "keyfile"
    assert config.ENCRYPTION_KEY_SOURCE == "keyfile"
    print("  secrets: config records the source (keyfile) cleanly OK")


# --- Train S: both env spellings honored (prefixed wins; keyfile-safe) ---------


def test_secret_prefixed_only_is_env():
    # Baseline for the both-spellings matrix: prefixed spelling => source 'env'.
    r = _probe_config(
        {"LOC_TMS_SESSION_SECRET": "pfx-ss-0123456789abcdef", "LOC_TMS_ENCRYPTION_KEY": "pfx-ek"}
    )
    assert r["ss"] == "pfx-ss-0123456789abcdef" and r["ek"] == "pfx-ek", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    assert r["ssrc"] == "env" and r["eksrc"] == "env", r
    assert _DIFFER_WARN not in r["_stderr"] and _EMBEDDED_WARN not in r["_stderr"], r
    assert _ROW5_WARN not in r["_stderr"], r
    print("  secrets: prefixed spelling => source 'env', no warning OK")


def test_secret_unprefixed_only_fresh_no_keyfile():
    # THE TRAP-CLOSED REGRESSION: unprefixed var, no keyfile, non-embedded =>
    # value used, source 'env', and NO keyfile is ever written for it.
    d = tempfile.mkdtemp(prefix="loctms_ops_unpfx_")
    r = _probe_config(
        {"SESSION_SECRET": "unpfx-ss-0123456789abcdef", "ENCRYPTION_KEY": "unpfx-ek"}, data_dir=d
    )
    assert r["ss"] == "unpfx-ss-0123456789abcdef" and r["ek"] == "unpfx-ek", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    assert r["ssrc"] == "env" and r["eksrc"] == "env", r  # admin key_source contract
    assert not os.path.exists(os.path.join(d, _SS_KEYFILE)), "no session keyfile"
    assert not os.path.exists(os.path.join(d, _EK_KEYFILE)), "no enc keyfile"
    assert _DIFFER_WARN not in r["_stderr"] and _EMBEDDED_WARN not in r["_stderr"], r
    assert _ROW5_WARN not in r["_stderr"], r
    print("  secrets: unprefixed spelling (fresh) => 'env', no keyfile written OK")


def test_secret_unprefixed_same_dir_provenance_stable():
    # HIGH-1 provenance regression: two consecutive boots on the SAME data dir with
    # only the unprefixed var set => source 'env' BOTH boots, NO keyfile, NO warning.
    # The value is never persisted, so provenance can never flip 'env'->'keyfile'.
    d = tempfile.mkdtemp(prefix="loctms_ops_samedir_")
    extra = {"SESSION_SECRET": "sd-ss-0123456789abcdef", "ENCRYPTION_KEY": "sd-ek"}
    r1 = _probe_config(extra, data_dir=d)
    r2 = _probe_config(extra, data_dir=d)
    assert r1["sss"] == "env" and r1["eks"] == "env", r1
    assert r2["sss"] == "env" and r2["eks"] == "env", r2
    assert r2["ss"] == "sd-ss-0123456789abcdef" and r2["ek"] == "sd-ek", r2
    assert not os.path.exists(os.path.join(d, _SS_KEYFILE)), "no keyfile after 2 boots"
    assert not os.path.exists(os.path.join(d, _EK_KEYFILE)), "no keyfile after 2 boots"
    assert _DIFFER_WARN not in r2["_stderr"] and _EMBEDDED_WARN not in r2["_stderr"], r2
    print("  secrets: unprefixed same-dir => 'env' both boots, no keyfile/warn OK")


def test_secret_keyfile_outranks_unprefixed_identical_silent():
    # Existing install untouched + correct-operator-no-noise (HIGH-1): a keyfile
    # out-ranks the unprefixed var, and IDENTICAL values fire NOTHING.
    d = tempfile.mkdtemp(prefix="loctms_ops_kf_same_")
    _write_keyfiles(d, "kf-ss-0123456789abcdef", "kf-ek")
    r = _probe_config(
        {"SESSION_SECRET": "kf-ss-0123456789abcdef", "ENCRYPTION_KEY": "kf-ek"}, data_dir=d
    )
    assert r["ss"] == "kf-ss-0123456789abcdef" and r["ek"] == "kf-ek", r
    assert r["sss"] == "keyfile" and r["eks"] == "keyfile", r
    assert r["ssrc"] == "keyfile" and r["eksrc"] == "keyfile", r  # admin display
    assert _DIFFER_WARN not in r["_stderr"], r
    print("  secrets: keyfile out-ranks identical unprefixed, silent OK")


def test_secret_keyfile_outranks_unprefixed_differ_warns():
    # BLOCKER-2 regression: keyfile out-ranks a DIFFERING unprefixed var (existing
    # install untouched) AND the rotation-trap differ-warning is emitted.
    d = tempfile.mkdtemp(prefix="loctms_ops_kf_diff_")
    _write_keyfiles(d, "kf-ss-0123456789abcdef", "kf-ek")
    r = _probe_config(
        {"SESSION_SECRET": "different-ss-0123456789abcdef", "ENCRYPTION_KEY": "different-ek"},
        data_dir=d,
    )
    assert r["ss"] == "kf-ss-0123456789abcdef" and r["ek"] == "kf-ek", r
    assert r["sss"] == "keyfile" and r["eks"] == "keyfile", r
    assert r["ssrc"] == "keyfile" and r["eksrc"] == "keyfile", r  # admin display
    assert _DIFFER_WARN in r["_stderr"], r["_stderr"]
    assert "docs/SECRETS.md" in r["_stderr"], r["_stderr"]
    print("  secrets: keyfile out-ranks differing unprefixed + differ-warning OK")


def test_secret_neither_set_existing_keyfile_silent():
    # SPEC row 7 (the pure byte-identity row): neither spelling set + an EXISTING
    # keyfile => the keyfile is used, source 'keyfile', and NOTHING warns. This is
    # the already-running-install path that must stay unchanged from today.
    d = tempfile.mkdtemp(prefix="loctms_ops_row7_")
    _write_keyfiles(d, "kf-ss-0123456789abcdef", "kf-ek")
    r = _probe_config({}, data_dir=d)
    assert r["ss"] == "kf-ss-0123456789abcdef" and r["ek"] == "kf-ek", r
    assert r["sss"] == "keyfile" and r["eks"] == "keyfile", r
    assert r["ssrc"] == "keyfile" and r["eksrc"] == "keyfile", r
    assert _DIFFER_WARN not in r["_stderr"] and _EMBEDDED_WARN not in r["_stderr"], r
    assert _ROW5_WARN not in r["_stderr"], r
    print("  secrets: neither set + existing keyfile => keyfile, silent OK")


def test_secret_prefixed_wins_over_existing_keyfile():
    # SPEC row 1 variant: prefixed env set AND a keyfile ALSO present => prefixed
    # wins (source 'env'); the keyfile is not consulted. Unchanged from historical
    # behavior (the prefixed env var always out-ranked the keyfile).
    d = tempfile.mkdtemp(prefix="loctms_ops_row1kf_")
    _write_keyfiles(d, "kf-ss-0123456789abcdef", "kf-ek")
    r = _probe_config(
        {"LOC_TMS_SESSION_SECRET": "pfx-ss-0123456789abcdef", "LOC_TMS_ENCRYPTION_KEY": "pfx-ek"},
        data_dir=d,
    )
    assert r["ss"] == "pfx-ss-0123456789abcdef" and r["ek"] == "pfx-ek", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    assert r["ssrc"] == "env" and r["eksrc"] == "env", r
    assert _DIFFER_WARN not in r["_stderr"] and _ROW5_WARN not in r["_stderr"], r
    print("  secrets: prefixed wins over an existing keyfile OK")


def test_secret_both_set_differ_prefixed_wins_warns():
    # SPEC row 5: both spellings set to DIFFERENT values => prefixed wins
    # (source 'env') AND a warning fires naming both spellings + the ignored value.
    r = _probe_config(
        {
            "LOC_TMS_SESSION_SECRET": "pfx-ss-0123456789abcdef",
            "SESSION_SECRET": "unpfx-ss-0123456789abcdef",
            "LOC_TMS_ENCRYPTION_KEY": "pfx-ek",
            "ENCRYPTION_KEY": "unpfx-ek",
        }
    )
    assert r["ss"] == "pfx-ss-0123456789abcdef" and r["ek"] == "pfx-ek", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    assert _ROW5_WARN in r["_stderr"], r["_stderr"]
    assert "docs/SECRETS.md" in r["_stderr"], r["_stderr"]
    print("  secrets: both spellings set (differ) => prefixed wins + warning OK")


def test_secret_both_set_identical_silent():
    # SPEC row 5b: both spellings set to the SAME value => prefixed wins, SILENT
    # (nothing to reconcile, no boot noise for a consistent operator).
    r = _probe_config(
        {
            "LOC_TMS_SESSION_SECRET": "same-ss-0123456789abcdef",
            "SESSION_SECRET": "same-ss-0123456789abcdef",
            "LOC_TMS_ENCRYPTION_KEY": "same-ek",
            "ENCRYPTION_KEY": "same-ek",
        }
    )
    assert r["ss"] == "same-ss-0123456789abcdef" and r["ek"] == "same-ek", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    assert _ROW5_WARN not in r["_stderr"], r["_stderr"]
    print("  secrets: both spellings set (identical) => prefixed wins, silent OK")


def test_secret_embedded_both_set_differ_silent():
    # SPEC row E3: in embedded mode the unprefixed var is NEVER consulted, so a
    # differing unprefixed alongside a set prefixed fires NOTHING (the host that
    # exports a generic SESSION_SECRET must not be nagged). Prefixed wins as always.
    r = _probe_config(
        {
            "LOC_TMS_EMBEDDED": "1",
            "LOC_TMS_SERVER": "1",
            "LOC_TMS_SESSION_SECRET": "pfx-ss-0123456789abcdef",
            "SESSION_SECRET": "host-ss",
            "LOC_TMS_ENCRYPTION_KEY": "pfx-ek",
            "ENCRYPTION_KEY": "host-ek",
        }
    )
    assert r["ss"] == "pfx-ss-0123456789abcdef" and r["ek"] == "pfx-ek", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    assert _ROW5_WARN not in r["_stderr"], r["_stderr"]
    assert _EMBEDDED_WARN not in r["_stderr"], r["_stderr"]
    print("  secrets: embedded + both spellings (differ) => prefixed wins, silent OK")


def test_secret_embedded_unprefixed_ignored_warns():
    # H2 + MEDIUM-1: embedded boot, unprefixed-only, no keyfile => the unprefixed
    # var is NOT adopted (a fresh keyfile is generated) and the embedded ignore-
    # warning fires. Values must NOT equal the unprefixed input.
    d = tempfile.mkdtemp(prefix="loctms_ops_emb_")
    r = _probe_config(
        {
            "LOC_TMS_EMBEDDED": "1",
            "LOC_TMS_SERVER": "1",
            "SESSION_SECRET": "host-ss",
            "ENCRYPTION_KEY": "host-ek",
        },
        data_dir=d,
    )
    assert r["sss"] == "keyfile" and r["eks"] == "keyfile", r
    assert r["ss"] != "host-ss" and r["ek"] != "host-ek", r
    assert os.path.exists(os.path.join(d, _SS_KEYFILE)), "keyfile generated"
    assert _EMBEDDED_WARN in r["_stderr"], r["_stderr"]
    assert _DIFFER_WARN not in r["_stderr"], r["_stderr"]
    print("  secrets: embedded ignores unprefixed + generates keyfile + warns OK")


def test_secret_embedded_persisted_keyfile_silent():
    # Embedded + a PERSISTED keyfile + unprefixed present => keyfile wins silently:
    # the embedded ignore-warning requires a FRESHLY generated keyfile, and the
    # differ-warning is non-embedded only. The archetypal embedded box (host exports
    # a generic SESSION_SECRET) must not be nagged when it has its own volume.
    d = tempfile.mkdtemp(prefix="loctms_ops_emb_persist_")
    _write_keyfiles(d, "vol-ss-0123456789abcdef", "vol-ek")
    r = _probe_config(
        {
            "LOC_TMS_EMBEDDED": "1",
            "LOC_TMS_SERVER": "1",
            "SESSION_SECRET": "host-ss",
            "ENCRYPTION_KEY": "host-ek",
        },
        data_dir=d,
    )
    assert r["ss"] == "vol-ss-0123456789abcdef" and r["ek"] == "vol-ek", r
    assert r["sss"] == "keyfile" and r["eks"] == "keyfile", r
    assert _EMBEDDED_WARN not in r["_stderr"], r["_stderr"]
    assert _DIFFER_WARN not in r["_stderr"], r["_stderr"]
    print("  secrets: embedded + persisted keyfile => silent OK")


def test_secret_embedded_bare_first_boot_silent():
    # The "unprefixed var present" conjunct of the embedded ignore-warning is
    # load-bearing: a bare embedded FIRST boot with NEITHER spelling set and no
    # keyfile must generate its keyfiles SILENTLY -- warning about a nonexistent
    # variable would be noise (and Phase 0's verify_embedded_profile.py boots
    # exactly this way). Keyfiles are generated; no warning of either kind fires.
    d = tempfile.mkdtemp(prefix="loctms_ops_emb_bare_")
    r = _probe_config({"LOC_TMS_EMBEDDED": "1", "LOC_TMS_SERVER": "1"}, data_dir=d)
    assert r["sss"] == "keyfile" and r["eks"] == "keyfile", r
    assert os.path.exists(os.path.join(d, _SS_KEYFILE)), "keyfile generated"
    assert os.path.exists(os.path.join(d, _EK_KEYFILE)), "keyfile generated"
    assert _EMBEDDED_WARN not in r["_stderr"], r["_stderr"]
    assert _DIFFER_WARN not in r["_stderr"], r["_stderr"]
    print("  secrets: embedded bare first boot => keyfiles generated, silent OK")


def main():
    test_json_formatter_basic()
    test_json_formatter_args()
    test_json_formatter_exception()
    test_json_formatter_extra()
    test_configure_text_is_noop()
    test_configure_json_idempotent()
    test_http_client_request_lines_quieted_302()
    test_access_log_drops_query_and_invite_token_302()
    test_access_log_redacts_prefixed_invite_402()
    test_prefixed_invite_never_in_access_log_402()
    test_config_defaults()
    test_secret_source_is_recorded()
    test_secret_env_wins()
    test_secret_keyfile_fallback()
    test_secret_prefixed_only_is_env()
    test_secret_unprefixed_only_fresh_no_keyfile()
    test_secret_unprefixed_same_dir_provenance_stable()
    test_secret_keyfile_outranks_unprefixed_identical_silent()
    test_secret_keyfile_outranks_unprefixed_differ_warns()
    test_secret_neither_set_existing_keyfile_silent()
    test_secret_prefixed_wins_over_existing_keyfile()
    test_secret_both_set_differ_prefixed_wins_warns()
    test_secret_both_set_identical_silent()
    test_secret_embedded_both_set_differ_silent()
    test_secret_embedded_unprefixed_ignored_warns()
    test_secret_embedded_persisted_keyfile_silent()
    test_secret_embedded_bare_first_boot_silent()
    print("SECRET ENV SPELLINGS OK")
    print("OPS OK")


if __name__ == "__main__":
    main()
