"""Stand up an ISOLATED, REAL-ENGINE Localization OS instance for the tester campaign.

`scripts\\test-instance.cmd <verb> <name> [port]` is the thin cmd wrapper; this module does
the work. It stands up a repeatable regression-test instance:

  * a THROWAWAY `LOC_TMS_DATA_DIR` under `<repo>/.test-instances/<name>/datadir` -- the repo's
    real `data/` dir and its `.protected` marker are never touched, and `up` REFUSES to run if
    the resolved data dir is under the repo `data/` or carries a `.protected` marker;
  * every cloud/credential-shaped env var PRE-SET TO EMPTY in the child environment, so
    `load_dotenv(override=False)` in app/config.py cannot pull a value out of the repo `.env`
    (that is the whole mechanism -- see BLANKED_ENV_VARS below);
  * REAL local engines only, in one of three ENGINE SETS (`--engines`, default `both`):
      - `both`     -- LM Studio (`local` kind) AND an Ollama box (`openai_compatible`
                      kind), both preflighted over HTTP BEFORE anything is written;
      - `ollama`   -- the Ollama box ALONE. LM Studio is never probed, never configured
                      and never called: the install default provider (`local`) is pointed at the
                      Ollama box's OpenAI-compatible surface (`providers._normalize_local_base`
                      appends `/v1`), so every seeded run/evaluation/mission slot lands there;
      - `lmstudio` -- LM Studio ALONE (the mirror image of `ollama`, added 2026-09-16 because the
                      box has been down at campaign time twice). The Ollama box is never probed,
                      never configured, never called and never presented as present: this
                      instance simply HAS NO engine #2, and says so;
  * `security_auth_required=1`, `remote_admin_enabled=1`, every `opt_in` module ON except
    `scim`/`saml` (their write door moved to /settings/enterprise behind a boot preflight);
  * one instance of every resource family it can reach, seeded through the product's own
    seams (the /api surface with the admin key where a route exists, the in-process CLI
    otherwise) -- never raw SQL;
  * a detached uvicorn on a private port that outlives this process;
  * the MCP server registered with an MCP client CLI (`LOC_TEST_MCP_CLI`, optional) and proved over
  stdio.

SECRETS: the minted API keys and the generated web password are written ONLY to
`api-key.txt`, `api-key-viewer.txt` and `web-login.txt` inside the instance root (which is
gitignored). They are never printed, logged, or written into `instance.md` / `seed.json`.

Usage:
    python scripts/test_instance.py up <name> [port] [--engines both|ollama|lmstudio]
    python scripts/test_instance.py status <name>
    python scripts/test_instance.py down <name> [--legacy-pid]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_VENV_BIN = "Scripts" if os.name == "nt" else "bin"
_VENV_EXE = "python.exe" if os.name == "nt" else "python"
# The project virtualenv's interpreter when it exists, else the interpreter running this script.
VENV_PY = (
    _ROOT / ".venv" / _VENV_BIN / _VENV_EXE
    if (_ROOT / ".venv" / _VENV_BIN / _VENV_EXE).exists()
    else Path(sys.executable)
)
INSTANCES_ROOT = _ROOT / ".test-instances"
MCP_SERVER = _ROOT / "integrations" / "mcp-server" / "server.py"

# --- engines (REAL, local; the tester campaign's standing rule) --------------------------
LM_STUDIO_URL = "http://127.0.0.1:1234"
LM_STUDIO_MODEL = "google/gemma-4-12b"
# Ollama hosts to try, in order: LOC_TMS_TEST_OLLAMA_HOSTS, comma separated, for example
# "http://ollama-host.example:11434,http://ollama-two.example:11434". There is no default;
# `--engines lmstudio` never needs one.
OLLAMA_HOSTS = tuple(
    host.strip().rstrip("/")
    for host in os.environ.get("LOC_TMS_TEST_OLLAMA_HOSTS", "").split(",")
    if host.strip()
)
OLLAMA_MODEL = "gemma4:e4b-it-qat"
OLLAMA_API_KEY = "ollama"  # Ollama ignores the bearer, but the OpenAI SDK requires a non-empty one

# The engine sets `up --engines` accepts.
#   both   -- LM Studio is the install default (`local`), the Ollama box is engine profile #2.
#   ollama -- LM STUDIO IS NEVER TOUCHED (not probed, not configured, not called). The built-in
#             `local` provider IS the Ollama box: LOCAL_BASE_URL points at it and
#             providers._normalize_local_base appends `/v1`, which is exactly the surface Ollama
#             serves (`GET /v1/models`, `POST /v1/chat/completions` -- both live-verified
#             2026-09-08). DEFAULT_PROVIDER therefore stays the built-in name `local` rather than
#             an `engine:<id>` ref: get_provider() accepts either, but `local` is the rung every
#             unspecified slot (a bare workflow step, a roster default, an eval candidate) falls
#             back to, so pointing IT at the box is what stops any slot from resolving to `mock`.
#             The `engine:<id>` profile is created anyway, so the profile-ref path is exercised too.
#   lmstudio -- THE OLLAMA BOX IS NEVER TOUCHED (not probed, not configured, not called, not
#             named). LM Studio is the install default (`local`) exactly as under `both`, and
#             there is NO engine profile #2: no `engine:<id>` ref in the Advanced roster, no
#             engine-#2 MT proof (it is recorded as SKIPPED, honestly), and instance.md declares
#             engine #2 ABSENT rather than describing a server this instance cannot reach. Added
#             2026-09-16: the box was down at campaign time on 09-15 and again on 09-16 while LM
#             Studio was up, and both older sets require it, so a lane that needs a REAL model was
#             blocked for no reason. `preflight_engines` returns ("", "") for the box's base and
#             model under this set, so every consumer must tolerate an EMPTY `ollama_base`.
ENGINE_SETS = ("both", "ollama", "lmstudio")

# --- every key-shaped / egress-shaped env var blanked in the child environment ------------
# Sources: app/config.py `_get("...")` reads, plus the integration + enterprise vars found by
# grepping app/ and integrations/ for getenv/environ of *_KEY|*_TOKEN|*_SECRET|*_PASSWORD.
# Pre-setting them to "" is what stops load_dotenv(override=False) from reaching the repo .env.
BLANKED_ENV_VARS = (
    # LLM / cloud providers
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "BRAVE_API_KEY",
    # database / storage
    "DATABASE_URL",
    "LOC_TMS_DATABRICKS_HOST",
    "LOC_TMS_DATABRICKS_CLIENT_ID",
    "LOC_TMS_DATABRICKS_CLIENT_SECRET",
    "LOC_TMS_DATABRICKS_TOKEN",
    "LOC_TMS_DATABRICKS_SMOKE",
    "LOC_TMS_S3_ACCESS_KEY_ID",
    "LOC_TMS_S3_SECRET_ACCESS_KEY",
    "LOC_TMS_S3_SESSION_TOKEN",
    "LOC_TMS_S3_BUCKET",
    "LOC_TMS_S3_ENDPOINT_URL",
    "LOC_TMS_S3_REGION",
    "LOC_TMS_S3_SMOKE",
    # network / identity / back office
    "TS_AUTHKEY",
    "LOC_TMS_TAILNET_PROXY",
    "LOC_TMS_OPERATOR_KEY",
    "LOC_TMS_TRUST_HEADER_SECRET",
    "LOC_TMS_TRUST_HEADER_SECRETS",
    "LOC_TMS_ADMIN_PASSWORD",
    "LOC_TMS_ADMIN_EMAIL",
    "VENDOR_STUDIO_SSO_SECRET",
    "VENDOR_STUDIO_URL",
    # this repo's own client tokens (must never reach the test instance)
    "LOC_STUDIO_API_CLI",
    "LOC_STUDIO_TOKEN",
    "LOC_STUDIO_BASE_URL",
    # guards we never want inherited
    "LOC_TMS_ALLOW_REAL_DATA",
    "LOC_TMS_ALLOW_MOCK_EGRESS",
    "LOC_TMS_SERVER",
    "LOC_TMS_EMBEDDED",
    "FLY_APP_NAME",
    # round-1 gate L1: the two vars that can make "real engines only" FAIL OPEN.
    # config.MOCK_FALLBACK_OK (app/config.py: LOC_TMS_MOCK_FALLBACK) lets providers.py
    # substitute deterministic MOCK output whenever a real engine call fails -- silently, and
    # mid-campaign; DEFAULT_FALLBACK_PROVIDER can point the fallback at a cloud provider.
    "LOC_TMS_MOCK_FALLBACK",
    "DEFAULT_FALLBACK_PROVIDER",
    # round-1 gate L1: the install-identity secrets. Both spellings, because config._secret
    # accepts the unprefixed one on a non-embedded boot -- a host that exports the generic name
    # would have this THROWAWAY instance adopt the real install's session/encryption key.
    # Blanking is safe: _secret() tests `if env_val:`, so "" falls through to the keyfile arm
    # and a fresh per-instance secret is generated in the throwaway data dir.
    "LOC_TMS_SESSION_SECRET",
    "SESSION_SECRET",
    "LOC_TMS_ENCRYPTION_KEY",
    "ENCRYPTION_KEY",
)

# Any argv token that FOLLOWS a flag matching one of these words carries a secret and must be
# blanked before the argv is put in a message. See `redact_argv` (round-1 gate H1).
SECRET_FLAG_WORDS = ("password", "secret", "token", "key", "passphrase", "credential")

# opt_in modules turned ON so their surfaces exist for the sweep. scim/saml are deliberately
# excluded: features_admin.DOOR_MOVED sends their toggle to /settings/enterprise behind an
# operator-key step-up + a boot-brick preflight (and both are REMOTE_IMMUTABLE).
OPT_IN_SKIP = ("scim", "saml")

DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

# Round-3 gate L6: MODULE-LEVEL INDIRECTION for the four stdlib entry points the guard suite
# needs to stand in for (a subprocess that "fails", a Popen that is not really launched, a sleep
# that does not wait, an rmtree that must never run). The suite patches THESE names, so a test
# that dies between patch and restore leaves `subprocess.run`, `subprocess.Popen`, `time.sleep`
# and `shutil.rmtree` -- shared, process-wide stdlib module attributes -- untouched for every
# other test in the interpreter. Every call site in this module goes through them.
_run = subprocess.run
_popen = subprocess.Popen
_sleep = time.sleep
_rmtree = shutil.rmtree


# ======================================================================================= #
# small helpers
# ======================================================================================= #
def _say(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


class Abort(RuntimeError):
    """A refusal: printed as `ERROR: ...` and exits non-zero."""


def _root_for(name: str) -> Path:
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise Abort(f"instance name {name!r} must be alphanumeric/dash/underscore")
    return INSTANCES_ROOT / name


def _password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Tt1" + "".join(secrets.choice(alphabet) for _ in range(21))


def _write_secret(path: Path, value: str) -> None:
    """Write a secret to its own file with a trailing newline and nothing else."""
    # #519: owner-only. Create with 0600 and chmod again so a file left by an earlier run (or
    # a loose umask) is tightened too; web-login.txt and the API key files must not be
    # readable by other local users on a shared host.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.chmod(path, 0o600)


def redact_argv(args) -> str:
    """Render an argv for a MESSAGE with every secret VALUE blanked (round-1 gate H1).

    `cli()` used to interpolate the raw argv into its Abort text, and two call sites pass a
    plaintext password on argv (`init --admin-password`, `user create --password`). Any failure
    -- a re-run over a rebuilt datadir, a locked DB, a validation refusal -- therefore printed
    the live web-login password to stdout/stderr, where the campaign captures it into a
    transcript or a log file. The value after any `--*password*` / `--*secret*` / `--*token*` /
    `--*key*` flag is now `***`, in the message only: the child still gets the real argv.

    `--flag=value` is handled too (the flag itself is truncated at the `=`)."""
    out: list[str] = []
    redact_next = False
    for raw in args:
        arg = str(raw)
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        low = arg.lower()
        if low.startswith("-") and "=" in low:
            head = low.split("=", 1)[0]
            if any(word in head for word in SECRET_FLAG_WORDS):
                out.append(f"{arg.split('=', 1)[0]}=***")
                continue
        out.append(arg)
        redact_next = low.startswith("-") and any(word in low for word in SECRET_FLAG_WORDS)
    return " ".join(out)


def _password_values(args) -> list[str]:
    """The VALUES an argv carries behind its secret-shaped flags, for `scrub` (round-1 gate H1).

    The mirror of `redact_argv`: that one blanks the value in the argv echo, this one collects
    the same values so they can also be blanked out of the CHILD's captured output."""
    values: list[str] = []
    take_next = False
    for raw in args:
        arg = str(raw)
        if take_next:
            values.append(arg)
            take_next = False
            continue
        low = arg.lower()
        if low.startswith("-") and "=" in low:
            head, _, tail = arg.partition("=")
            if any(word in head.lower() for word in SECRET_FLAG_WORDS):
                values.append(tail)
                continue
        take_next = low.startswith("-") and any(word in low for word in SECRET_FLAG_WORDS)
    return values


def scrub(text: str, *secrets_: str) -> str:
    """Blank known secret VALUES anywhere in a captured stream (round-1 gate L3).

    A CLI that echoes its own invocation in a usage/error message defeats "never echo argv":
    the token then rides `proc.stderr` into `instance.md`. Every place a child's output is
    stored or rendered runs it through here with the values that child was handed."""
    out = str(text or "")
    for value in secrets_:
        if value and len(str(value)) >= 8:
            out = out.replace(str(value), "***")
    return out


def _pid_alive(pid: int) -> bool:
    try:
        out = _run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception:
        return False
    return str(pid) in out and "No tasks" not in out


def _read_pid(path: Path) -> int:
    """A pid file's value, or 0 for missing/blank/junk (round-1 gate L6: `status` used to
    `int()` the raw text outside the Abort handler, so a hand-edited pid file gave the user a
    traceback instead of a refusal)."""
    try:
        return int((path.read_text(encoding="utf-8") or "").strip())
    except Exception:
        return 0


def port_owner_pids(port: int) -> list[int]:
    """Every pid LISTENING on 127.0.0.1:<port>, newest netstat row first (round-1 gate M1).

    uvicorn with an explicit `workers=1` takes the multiprocess-supervisor path, so the pid
    `Popen` returns is a SUPERVISOR that forks the process actually bound to the port. Nothing
    but the OS knows which pid owns the socket, so ask it."""
    try:
        out = _run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=60
        ).stdout
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        if local.rsplit(":", 1)[-1] != str(port):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid and pid not in pids:
            pids.append(pid)
    return pids


def _cim_process_field(pid: int, field: str) -> tuple[str, str]:
    """(value, query_failure) for one `Win32_Process` field of a pid.

    Round-3 gate L4: `_command_line` used to collapse "the process has no matching command
    line" and "the query could not run at all" (no `powershell` on PATH, a sick WMI/CIM service,
    an AV hook) into the same empty string. That fails CLOSED -- correct for a kill decision --
    but silently, so `down` refused EVERY kill (including our own healthy supervisor) with a
    message blaming the process. The two cases are now distinct: `query_failure` is "" whenever
    the query itself ran, and names the exact CIM query when it did not."""
    query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').{field}"
    try:
        proc = _run(
            ["powershell", "-NoProfile", "-Command", query],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return "", f"`{query}` could not run ({type(exc).__name__}: {str(exc)[:120]})"
    if getattr(proc, "returncode", 0) != 0:
        detail = (getattr(proc, "stderr", "") or "").strip().replace("\n", " ")[:160]
        return "", f"`{query}` failed rc={proc.returncode}: {detail}"
    return (getattr(proc, "stdout", "") or "").strip(), ""


def _parent_pid(pid: int) -> int:
    out, _failure = _cim_process_field(pid, "ParentProcessId")
    try:
        return int(out.splitlines()[0].strip()) if out else 0
    except Exception:
        return 0


def _creation_date(pid: int) -> str:
    """`Win32_Process.CreationDate` for a pid, or "" (round-3 gate L1).

    The IDENTITY of a pid, not just its number: Windows recycles pid numbers, and a process
    that exits between `pid_is_ours` and `taskkill` can be replaced by a stranger holding the
    same number. Two reads that agree prove the pid did not turn over in that window."""
    value, _failure = _cim_process_field(pid, "CreationDate")
    return value


# Round-4 gate L3: `_command_line`'s failure warning is LATCHED. `stop_server`'s port loop asks
# `pid_is_ours` once per owner per iteration, up to 20 iterations, so a broken CIM service printed
# the same four-line warning dozens of times per `down` and buried the notes that followed. The
# latch is reset at the start of every verb, so the warning is printed once per command.
_CMDLINE_WARNED = False


def _cmdline_warning_latch(value: bool) -> None:
    global _CMDLINE_WARNED
    _CMDLINE_WARNED = value


def _command_line(pid: int) -> tuple[str, str]:
    """(`Win32_Process.CommandLine`, query_failure) for a pid.

    Round-2 gate M1: a recorded pid is NOT evidence. Pid files survive a reboot and Windows
    recycles pids freely, so `down` must ask the OS what the process it is about to kill
    actually IS before killing it. Round-3 gate L4: a query that could not RUN is announced on
    stdout, so the operator can tell a broken tool from a foreign process."""
    value, failure = _cim_process_field(pid, "CommandLine")
    if failure and not _CMDLINE_WARNED:
        _cmdline_warning_latch(True)
        _say(
            f"  WARNING: could not read command lines ({failure}); `down` cannot verify "
            f"ownership and will refuse to kill anything until the CIM query works again "
            f"(reported once per command; every later query failure this run is silent)"
        )
    return value, failure


def port_is_free(port: int) -> bool:
    """True when nothing is bound to 127.0.0.1:<port> RIGHT NOW (round-1 gate M2).

    No SO_REUSEADDR: on Windows that option lets a second socket steal a bound port, which
    would make this probe say "free" about a port that is serving."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(Exception):
            sock.close()


# ======================================================================================= #
# HTTP (httpx from the venv; the product depends on it, so it is always available)
# ======================================================================================= #
def _httpx():
    import httpx

    return httpx


def _probe_json(url: str, timeout: float = 10.0):
    httpx = _httpx()
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


class Api:
    """Thin /api client for the seeding + proof phases (X-Loc-Token, like the CLI's remote arm)."""

    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self._token = token
        self._client = _httpx().Client(timeout=600.0)

    def _hdr(self, token=None):
        t = self._token if token is None else token
        return {"X-Loc-Token": t} if t else {}

    def get(self, path: str, token=None, **kw):
        return self._client.get(self.base + path, headers=self._hdr(token), **kw)

    def post(self, path: str, body=None, token=None, **kw):
        return self._client.post(self.base + path, json=body or {}, headers=self._hdr(token), **kw)

    def put(self, path: str, body=None, token=None, **kw):
        return self._client.put(self.base + path, json=body or {}, headers=self._hdr(token), **kw)

    def upload(self, path: str, files, data=None, token=None):
        return self._client.post(
            self.base + path, files=files, data=data or {}, headers=self._hdr(token)
        )

    def jpost(self, path: str, body=None, **kw):
        r = self.post(path, body, **kw)
        if r.status_code >= 400:
            raise Abort(f"POST {path} -> {r.status_code} {r.text[:300]}")
        return r.json()

    def jget(self, path: str, **kw):
        r = self.get(path, **kw)
        if r.status_code >= 400:
            raise Abort(f"GET {path} -> {r.status_code} {r.text[:300]}")
        return r.json()


# ======================================================================================= #
# environment
# ======================================================================================= #
def child_env(datadir: Path, ollama_base: str, engine_set: str = "both") -> dict:
    """The child environment, with the DEFAULT PROVIDER pointed at the chosen engine set.

    `DEFAULT_PROVIDER=local` in every set, but WHICH server `local` is differs: under
    `--engines ollama` LOCAL_BASE_URL is the Ollama box (config.DEFAULT_MODELS['local'] is its
    model id), so the built-in provider every unspecified slot falls back to is that box and
    LM Studio is never contacted. Under `both` -- and under `lmstudio`, which differs only in
    having no engine #2 at all -- it is LM Studio, as before. Under `lmstudio` `ollama_base` is
    the empty string, so `LOC_TMS_TEST_OLLAMA_BASE` is empty and nothing in the child env names
    the box."""
    env = dict(os.environ)
    for var in BLANKED_ENV_VARS:
        env[var] = ""
    env["LOC_TMS_DATA_DIR"] = str(datadir)
    # The host list belongs to this tool, not to the instance: never let it name the box.
    env["LOC_TMS_TEST_OLLAMA_HOSTS"] = ""
    # #291: a throwaway data dir alone is now a real install; tester instances are test mode.
    env["LOC_TMS_TEST_MODE"] = "1"
    if engine_set == "ollama":
        env["LOCAL_BASE_URL"] = ollama_base
        env["LOCAL_API_KEY"] = OLLAMA_API_KEY
        env["DEFAULT_LOCAL_MODEL"] = OLLAMA_MODEL
    else:
        env["LOCAL_BASE_URL"] = LM_STUDIO_URL
        env["LOCAL_API_KEY"] = "lm-studio"
        env["DEFAULT_LOCAL_MODEL"] = LM_STUDIO_MODEL
    env["DEFAULT_PROVIDER"] = "local"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["LOC_TMS_TEST_OLLAMA_BASE"] = ollama_base  # informational, for the log
    env["LOC_TMS_TEST_ENGINE_SET"] = engine_set  # informational, for the log
    return env


def assert_data_dir_safe(datadir: Path) -> None:
    """Backstop, not the method: refuse anything under the repo data/ or a .protected dir."""
    repo_data = (_ROOT / "data").resolve()
    target = datadir.resolve()
    for candidate in [target, *target.parents]:
        if candidate == repo_data:
            raise Abort(f"refusing: {target} is under the repo data dir {repo_data}")
        if (candidate / ".protected").exists():
            raise Abort(f"refusing: {candidate} carries a .protected marker")
    inherited = os.environ.get("LOC_TMS_DATA_DIR", "").strip()
    if inherited:
        inh = Path(inherited).resolve()
        for candidate in [inh, *inh.parents]:
            if candidate == repo_data or (candidate / ".protected").exists():
                raise Abort(
                    "refusing: LOC_TMS_DATA_DIR in this environment points at a protected/real "
                    "data dir; unset it before running test-instance"
                )


# ======================================================================================= #
# engines preflight (BEFORE anything is written)
# ======================================================================================= #
def preflight_engines(engine_set: str = "both") -> tuple[str, str, str]:
    """Return (lm_model_id, ollama_base, ollama_model_id) or Abort.

    `--engines ollama` returns "" for the LM Studio model and does NOT probe 127.0.0.1:1234 --
    the whole point of that set is that LM Studio is left alone, so an instance must be able to
    come up while it is off (or serving somebody else).

    `--engines lmstudio` is the mirror image: it returns ("", "") for the Ollama base and model
    and does NOT probe either `OLLAMA_HOSTS` address, so an instance comes up while that box is
    down. Nothing downstream may read the empty base as a URL."""
    if engine_set not in ENGINE_SETS:
        raise Abort(f"unknown engine set {engine_set!r} (choose one of {', '.join(ENGINE_SETS)})")
    lm_model = ""
    if engine_set in ("both", "lmstudio"):
        try:
            models = [
                m.get("id") for m in _probe_json(f"{LM_STUDIO_URL}/v1/models").get("data", [])
            ]
        except Exception as exc:  # pragma: no cover - live probe
            raise Abort(f"LM Studio at {LM_STUDIO_URL} is not reachable: {exc}") from exc
        if LM_STUDIO_MODEL not in models:
            raise Abort(f"LM Studio does not list {LM_STUDIO_MODEL} (has: {', '.join(models[:6])})")
        lm_model = LM_STUDIO_MODEL
    if engine_set == "lmstudio":
        # The box is not part of this instance: no probe, no base, no model. Returning here is
        # what makes `up` independent of a host that has been down at campaign time twice.
        return lm_model, "", ""

    if not OLLAMA_HOSTS:
        raise Abort(
            "no Ollama host configured: set LOC_TMS_TEST_OLLAMA_HOSTS to a comma separated "
            "list of base URLs (for example http://ollama-host.example:11434), or use "
            "`--engines lmstudio`"
        )
    last = ""
    for host in OLLAMA_HOSTS:
        try:
            tags = [m.get("name") for m in _probe_json(f"{host}/api/tags").get("models", [])]
        except Exception as exc:  # pragma: no cover - live probe
            last = f"{host}: {exc}"
            continue
        if OLLAMA_MODEL in tags:
            if engine_set == "ollama":
                # The product never speaks Ollama's native /api surface: `local` and
                # `openai_compatible` both POST /v1/chat/completions. When the box IS the default
                # provider, preflight the surface the product will actually use.
                try:
                    ids = [m.get("id") for m in _probe_json(f"{host}/v1/models").get("data", [])]
                except Exception as exc:  # pragma: no cover - live probe
                    raise Abort(
                        f"{host} answers /api/tags but its OpenAI-compatible /v1/models does "
                        f"not: {exc}. `--engines ollama` needs that surface."
                    ) from exc
                if OLLAMA_MODEL not in ids:
                    raise Abort(
                        f"{host}/v1/models does not list {OLLAMA_MODEL} (has: "
                        f"{', '.join(i for i in ids if i)})"
                    )
            return lm_model, host, OLLAMA_MODEL
        last = f"{host}: has {', '.join(t for t in tags if t)} but not {OLLAMA_MODEL}"
    raise Abort(f"no Ollama host serves {OLLAMA_MODEL} ({last})")


# ======================================================================================= #
# in-process CLI
# ======================================================================================= #
def cli(env: dict, *args: str, as_json: bool = False, check: bool = True, timeout: int = 900):
    argv = [str(VENV_PY), "-m", "app.cli"]
    if as_json:
        argv.append("--json")
    argv.extend(args)
    proc = _run(argv, cwd=str(_ROOT), env=env, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        # round-1 gate H1: the argv AND the child's own output go through the redactor. `args`
        # carries a plaintext password at two call sites, and a CLI that echoes its invocation
        # in its usage text would otherwise leak it back through stderr.
        secret_values = [str(a) for a in args if isinstance(a, str)]
        raise Abort(
            f"CLI {redact_argv(args)} failed rc={proc.returncode}: "
            f"{scrub((proc.stderr or proc.stdout).strip(), *_password_values(secret_values))[:400]}"
        )
    if as_json and proc.returncode == 0:
        try:
            return json.loads(proc.stdout)
        except Exception:
            return proc.stdout
    return proc


# ======================================================================================= #
# server lifecycle
# ======================================================================================= #
_UVICORN_SNIPPET = (
    "import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', port={port}, "
    "reload=False, workers=1, proxy_headers=False, access_log=True)"
)

# Round-3 gate M1: the INSTANCE STAMP. The datadir reaches the child through the ENVIRONMENT,
# never the command line, so before this the only mark a real uvicorn child could ever carry was
# the port-keyed snippet -- shared by every test instance that ever used that port, which made
# `down` on a RETIRED root kill whichever instance holds the port now and call it success.
# The stamp is an inert trailing comment inside the `-c` snippet, so it is part of the command
# line the OS reports and changes nothing about what Python executes. It is not a secret: the
# instance root and its port are already printed by `up` and written into instance.md.
#
# Round-4 gate M1: the stamp is TERMINATED. Matching is a substring test against a whole command
# line, so an unterminated stamp made every NAME-PREFIX root a valid claimant of another
# instance's processes (`.test-instances\feat` matched `...\featoff`'s command line, so
# `down feat` would `taskkill /T` featoff's tree and exit 0). The closing delimiter means a match
# compares the full delimited value and cannot run past the root's last path segment.
_STAMP_PREFIX = "# loc-test-instance:"
_STAMP_SUFFIX = "#"
# The delimiter that terminated the stamp EMITTED by the `a7cd3494` build: the closing quote of
# the `-c` argument on the real command line. Accepted when MATCHING (never emitted) so instances
# started by that build stay provably ours -- see `instance_markers`.
_STAMP_LEGACY_SUFFIX = '"'


def _stamp_for(root: Path) -> str:
    """The instance-unique marker `start_server` puts inside the child's `-c` snippet.

    Emitted AND matched with both delimiters (round-4 gate M1), so `<...>\\feat#` is not a
    substring of a command line stamped `<...>\\featoff#`."""
    return f"{_STAMP_PREFIX}{root}{_STAMP_SUFFIX}"


def uvicorn_command(root: Path, port: int) -> str:
    """The exact `-c` snippet `start_server` launches: the uvicorn call plus this root's stamp."""
    return f"{_UVICORN_SNIPPET.format(port=int(port))}  {_stamp_for(root.resolve())}"


def start_server(root: Path, port: int, env: dict) -> subprocess.Popen:
    # Round-2 gate L8: `cmd_up` bind-tested this port about a minute ago -- before the engine
    # preflight, the stale-root rmtree, `alembic upgrade head` and ~20 feature toggles. Anything
    # that took the port in that window would send us back down the M2 path (a uvicorn that dies
    # on bind while somebody else's /health answers). Re-check IMMEDIATELY before the bind and
    # abort here, where nothing has been launched yet, rather than one door later.
    if not port_is_free(port):
        raise Abort(
            f"port {port} was taken between the pre-flight check and the server start "
            f"(listener pid(s) {port_owner_pids(port) or 'unknown'}): aborting rather than "
            f"starting a uvicorn that cannot bind. The instance root has been rebuilt; re-run "
            f"`up` on a free port."
        )
    # The log handle is handed to a DETACHED child and closed here: the child keeps its own
    # duplicated descriptor, so it outlives this process (and every Claude tool call).
    with open(root / "server.log", "ab") as log:
        proc = _popen(
            [str(VENV_PY), "-c", uvicorn_command(root, port)],
            cwd=str(_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=DETACHED,
            close_fds=True,
        )
    # The SUPERVISOR pid is recorded immediately so a crash between here and the listener
    # resolution still leaves `down` something to kill. server.pid (the LISTENER) is written by
    # `record_pids` once /health answers -- see round-1 gate M1.
    (root / "server.supervisor.pid").write_text(str(proc.pid), encoding="utf-8")
    (root / "server.pid").write_text(str(proc.pid), encoding="utf-8")
    (root / "server.port").write_text(str(port), encoding="utf-8")
    return proc


def record_pids(root: Path, port: int, proc: subprocess.Popen) -> tuple[int, int, str]:
    """Resolve the pid that actually OWNS the port and record it (round-1 gate M1 + M2).

    M1: uvicorn 0.52 with `workers=1` forks a worker; the Popen pid supervises it. `server.pid`
    used to name the supervisor, so a supervisor killed alone left an ORPHAN holding the port
    with the datadir open, while `down` reported success and the next `up` rmtree'd the datadir
    out from under it. `server.pid` is now the LISTENER; `server.supervisor.pid` keeps the other.

    M2: the listener must be OURS. With the port bind-tested free moments earlier and the child
    still alive, an owner that is neither the supervisor nor one of its children means we are
    talking to somebody else's server -- refuse rather than seed against it.

    Round-2 gate L3: an EMPTY netstat used to fall silently back to `listener = supervisor` --
    which is M1's original bug (the pid file names the supervisor) and skips M2's refusal with no
    trace anywhere. netstat is retried first, and when it still says nothing the fallback is
    announced on stdout AND returned as a note that `up` renders into instance.md. The refusal
    still applies in that arm: an unresolvable owner is only ever adopted while OUR OWN child is
    still alive -- if it is not, the /health 200 was somebody else's and this is an abort."""
    supervisor = proc.pid
    owners: list[int] = []
    for attempt in range(5):
        owners = port_owner_pids(port)
        if owners:
            break
        if attempt < 4:
            _sleep(0.5)
    listener = 0
    for pid in owners:
        if pid == supervisor or _parent_pid(pid) == supervisor:
            listener = pid
            break
    note = ""
    if not listener:
        if owners:
            raise Abort(
                f"port {port} is owned by pid(s) {owners}, none of which is our uvicorn "
                f"(supervisor {supervisor}) or its child: the /health 200 came from ANOTHER "
                f"server. Refusing to seed against it."
            )
        if proc.poll() is not None:
            raise Abort(
                f"netstat lists no owner for port {port} AND our uvicorn child exited "
                f"rc={proc.returncode}: the /health 200 was somebody else's server. Refusing "
                f"to seed against it."
            )
        note = (
            f"netstat could not resolve the owner of port {port} after 5 tries, so `server.pid` "
            f"names the SUPERVISOR ({supervisor}), not the listener. `down` will fall back to "
            f"killing the supervisor's process TREE; if a worker ever survives it, kill the port "
            f"owner by hand."
        )
        _say(f"  WARNING: {note}")
        listener = supervisor
    (root / "server.pid").write_text(str(listener), encoding="utf-8")
    (root / "server.supervisor.pid").write_text(str(supervisor), encoding="utf-8")
    return listener, supervisor, note


def wait_health(port: int, proc: subprocess.Popen | None = None, timeout: float = 120.0) -> dict:
    """Poll /health, but FAIL FAST if our own child died (round-1 gate M2).

    Without the `proc.poll()` arm a uvicorn that could not bind (port already taken) died
    instantly and this loop was satisfied by the OTHER instance's 200 -- the script then
    declared itself up, seeded against the wrong server, and wrote a fictional instance.md."""
    httpx = _httpx()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise Abort(
                f"the uvicorn child exited rc={proc.returncode} before /health answered; "
                f"see server.log (a /health 200 on port {port} now would be somebody else's)"
            )
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code} {r.text[:120]}"
        except Exception as exc:
            last = str(exc)
        _sleep(1.0)
    raise Abort(f"/health never came up on port {port} (last: {last})")


def _kill(pid: int) -> None:
    _run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)


def _read_port(root: Path) -> int:
    """The port this instance was started on: `server.port` first, else parsed back out of
    instance.md (instances created before `server.port` existed).

    Round-3 gate M1: a root that has been `down`ed cleanly is RETIRED -- `stop_server` deletes
    `server.port` along with the pid files -- and a retired root must have no port to act on at
    all, or a stray second `down` re-arms itself off instance.md and goes hunting for whoever
    holds that number now. The instance.md fallback therefore applies only while a pid file is
    still on disk, i.e. while this root still claims to own a running server.

    Round-4 gate M1 (the disarm): the pid-file test now gates `server.port` TOO, not just the
    instance.md fallback. Roots retired by an EARLIER build kept their `server.port` on disk
    (`.test-instances/gate2` still names 8361), so they stayed live port-arm launchers that a
    stray `down` could point at whoever holds that number now. No pid file = no claim on any
    server = no port to act on, whatever `server.port` or instance.md still say."""
    if not any((root / n).exists() for n in ("server.pid", "server.supervisor.pid")):
        return 0
    port = _read_pid(root / "server.port")
    if port:
        return port
    md = root / "instance.md"
    if md.exists():
        for line in md.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("- Name:") and "port: `" in line:
                with contextlib.suppress(Exception):
                    return int(line.split("port: `")[1].split("`")[0])
    return 0


def instance_markers(root: Path, port: int = 0) -> list[str]:
    """The strings that PROVE a command line belongs to THIS instance (round-2/3 gate M1).

    Two independent marks, each INSTANCE-UNIQUE, either of which is conclusive:
      * this root's instance STAMP -- the inert `# loc-test-instance:<root>` comment
        `start_server` puts inside the child's `-c` snippet;
      * the instance's own throwaway datadir path, for a child that carries it on its argv.
    Both are literal substrings of the child's `Win32_Process.CommandLine`.

    Round-3 gate M1: the bare uvicorn snippet (`app.main:app` + the port) is NOT here any more.
    It matched any test instance that happened to be on that port, so a `down` of a retired root
    killed whichever instance holds the port NOW and reported success. A port number is a hint,
    not evidence. `port` is still accepted so every call site keeps its shape, and is deliberately
    unused.

    Round-4 gate M1: every stamp marker is DELIMITED, so a name-prefix root cannot claim another
    instance's processes. Two delimiters are accepted, and both are terminators on a real command
    line: `#` (this build's suffix) and `"` (the closing quote of the `-c` argument, which is what
    terminated the stamp emitted by the `a7cd3494` build). The second keeps instances started by
    that build -- the campaign's own -- provably ours and therefore stoppable, without reopening
    the collision: `...\\feat"` is no more a substring of `...\\featoff"` than `...\\feat#` is."""
    markers = [
        _stamp_for(root),
        _stamp_for(root.resolve()),
        f"{_STAMP_PREFIX}{root}{_STAMP_LEGACY_SUFFIX}",
        f"{_STAMP_PREFIX}{root.resolve()}{_STAMP_LEGACY_SUFFIX}",
        str(root / "datadir"),
        str((root / "datadir").resolve()),
    ]
    return [m for m in dict.fromkeys(markers) if m]


def looks_like_loc_uvicorn(cmdline: str) -> bool:
    """True when this command line is a python running THIS PRODUCT's uvicorn app.

    Round-4 gate H1: the bare `app.main:app` snippet is not evidence of OWNERSHIP (that was
    round-3 M1), but it is a perfectly good NEGATIVE discriminator. A live python running
    `app.main:app` that carries no stamp is not a stranger whose pid file we may throw away --
    it is a LEGACY instance, ours-but-unprovable, started by a build from before the stamp
    existed. Nothing about it may be deleted, killed, or described as "not this instance"."""
    if not cmdline:
        return False
    lowered = cmdline.lower()
    return "app.main:app" in cmdline and ("python" in lowered or "uvicorn" in lowered)


def is_unstamped_loc_uvicorn(pid: int, root: Path | None = None) -> tuple[bool, str]:
    """(is this pid a LEGACY instance, why not) -- a live `app.main:app` python with NO stamp.

    Round-4 gate H1: the precondition `down --legacy-pid` checks against the port owner. A
    process carrying SOME stamp is a stamped instance of a specific root, and if that root is not
    ours the ordinary rules already refuse it -- `--legacy-pid` must never reach it.

    Round-5 gate L1: the refusal used to assert `and it is not this root's` without ever comparing
    the stamp to a root -- and for a healthy MODERN instance of the root being torn down it IS this
    root's stamp, which made the note the operator reads flatly false. `root` is optional so the
    function still answers the bare "is this pid unstamped?" question; when it is given, the two
    cases are named apart."""
    cmdline, failure = _command_line(pid)
    if failure:
        return False, f"ownership CANNOT BE VERIFIED for pid {pid}: {failure}"
    if not cmdline:
        return False, f"pid {pid} has no readable command line"
    if not looks_like_loc_uvicorn(cmdline):
        return False, f"pid {pid} is not a python running `app.main:app`"
    if _STAMP_PREFIX in cmdline:
        if root is not None and any(m in cmdline for m in instance_markers(root)):
            return False, (
                f"pid {pid} carries THIS root's own instance stamp, so it is already provably "
                f"ours and `--legacy-pid` is not needed for it"
            )
        return False, f"pid {pid} carries ANOTHER instance root's stamp"
    return True, ""


def pid_is_ours(pid: int, root: Path, port: int = 0, trusted_parents=()) -> tuple[bool, str]:
    """(is this pid provably THIS instance's, why/why not) -- round-2 gate M1.

    Evidence, in order:
      1. the pid's own command line carries one of `instance_markers` -- this root's stamp or
         this root's datadir path, both unique to `<repo>/.test-instances/<name>`;
      2. its parent is a pid we ALREADY proved ours in this same call (an orphaned or re-exec'd
         worker; not circular, since the root of the chain is always a command-line match).
    Anything else is NOT ours, whatever the pid files say: pid files survive reboots and Windows
    recycles pids, so "the number matches" is not evidence.

    Round-3 gate L3: a refusal reports the PID and nothing else. The old text quoted 70
    characters of a third party's command line, which is a classic place for other tools'
    credentials (`-p<password>`, `--token=...`) and reached the console through `down`'s notes."""
    if pid <= 0:
        return False, "no pid"
    cmdline, failure = _command_line(pid)
    for marker in instance_markers(root):
        if marker and marker in cmdline:
            return True, f"its command line carries this instance's marker `{marker[:64]}`"
    parent = _parent_pid(pid)
    if parent and parent in tuple(trusted_parents):
        return True, f"child of pid {parent}, already proven to be this instance"
    if failure:
        return False, (
            f"ownership CANNOT BE VERIFIED: {failure}. This is a failure of the tool, not a "
            f"verdict about pid {pid}"
        )
    if not cmdline:
        return False, f"pid {pid} has no readable command line (already gone, or another user's)"
    if _STAMP_PREFIX in cmdline:
        # Round-5 gate H1: a STAMP names a specific instance root. The marker loop above already
        # answered "is that root this one?", so reaching here with a stamp on the command line
        # means the process provably belongs to a DIFFERENT root. That is a stranger -- never the
        # "ours but unprovable" LEGACY verdict below, which is what made `--legacy-pid` able to
        # `taskkill /T /F` another root's proven, live instance and exit 0.
        return False, (
            f"pid {pid} carries ANOTHER instance root's `{_STAMP_PREFIX}` stamp, so it is "
            f"provably NOT this instance's (it belongs to a different test instance)"
        )
    if looks_like_loc_uvicorn(cmdline):
        # Round-4 gate H1: ours-but-unprovable, NOT a stranger. Every caller must keep its pid
        # file, kill nothing, and point the operator at `--legacy-pid`.
        return False, (
            f"pid {pid} is a LEGACY INSTANCE: a live python running `app.main:app` that carries "
            f"no `{_STAMP_PREFIX}` stamp for this root. It was almost certainly started by a "
            f"build from before the stamp existed, so it is probably OURS but cannot be proven"
        )
    return False, f"pid {pid} is a foreign process; its command line carries no marker of this root"


def _health_answers(port: int) -> bool:
    """True when 127.0.0.1:<port>/health answers 200 RIGHT NOW."""
    try:
        return _httpx().get(f"http://127.0.0.1:{port}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


def stop_server(root: Path, legacy_pid: bool = False) -> tuple[str, bool]:
    """Kill this instance's supervisor and listener, and (only if it is still ours) the port
    owner. Returns (notes, ok); `ok` False means `down` must exit non-zero.

    Round-1 gate M1: `down` used to kill one recorded pid and call a missing pid "already gone"
    -- success while an orphaned worker was still serving a live admin key.

    Round-2 gate M1: the fix for that orphan became a way to kill a STRANGER. Both arms killed on
    a NUMBER -- a recorded pid that Windows may have recycled since the last reboot, or whoever
    happens to own the recorded port now (a second test instance, a dev server, a tunnel). Every
    kill is now gated on `pid_is_ours`, i.e. on `Win32_Process.CommandLine`, and the port arm
    additionally requires that the recorded pids are gone and that /health on that port still
    answers -- the exact orphan condition it exists for. A candidate that cannot prove itself is
    reported and LEFT ALONE, and `down` exits non-zero so nothing reads the refusal as success.

    Round-2 gate L4: a `down` that leaves a listener behind no longer exits 0.

    Round-4 gate H1: "not provably ours" is THREE verdicts, not two. A live python running
    `app.main:app` with no stamp is a LEGACY instance -- ours, started before the stamp existed
    -- and it keeps its pid files, is never killed, and is refused with `--legacy-pid` named.

    Round-5 gate H1 / round-6 gate H1: `legacy_pid=True` licenses a PID, not a call. It is spent
    only on an unstamped `app.main:app` python (with /health answering on this root's recorded
    port, every owner of which is unstamped) that is EITHER a current owner of that port OR the
    parent supervisor of one -- uvicorn `workers=1` forks the worker that holds the port, so the
    supervisor never owns it and killing the worker alone only makes uvicorn respawn it. A recorded
    pid that owns nothing and has no port-owning child is judged by the ordinary rules, so a
    `server.pid`/`server.supervisor.pid` that a reboot left pointing at another root's live
    instance can never be killed under the flag. Round-5 gate M1: the
    precondition is evaluated lazily, so a stray `--legacy-pid` on a healthy modern instance
    behaves exactly like a plain `down`."""
    _cmdline_warning_latch(False)  # round-4 gate L3: warn once per command, not per query
    notes: list[str] = []
    ok = True
    port = _read_port(root)
    supervisor = _read_pid(root / "server.supervisor.pid")
    listener = _read_pid(root / "server.pid")
    if not (root / "server.pid").exists() and not supervisor:
        notes.append("no server.pid on disk")
    if not port and not supervisor and not listener:
        # Round-4 gate M1: a RETIRED root -- no pid file, so `_read_port` gives it no port either.
        # There is nothing here to prove, kill or clean up, and no port to go hunting on.
        notes.append(
            f"{root} is RETIRED: no pid file, so this root claims no server and has no port to "
            f"act on (any `server.port`/instance.md left in it is ignored). Nothing was stopped; "
            f"only the MCP registration is being removed"
        )
        return "; ".join(notes), True

    # Round-4 gate H1 / round-5 gate M1: the `--legacy-pid` PRECONDITION, decided at most once and
    # LAZILY -- only if a LEGACY verdict is actually reached. Deciding it eagerly made a STRAY
    # `--legacy-pid` on a healthy modern instance a reported failure: this root's own stamp IS a
    # stamp, so the precondition always "REFUSED" it, `ok` went False, and a teardown that fully
    # succeeded exited 2, kept all three pid files and left the MCP registration pointing at a
    # dead server.
    legacy_state: dict[str, str] = {}
    # Round-6 gate L3: the recorded port's owners are resolved ONCE per `down` and read back by
    # every per-pid licence decision. `_legacy_licenses` used to re-run `port_owner_pids` (a second
    # `netstat` subprocess) per LEGACY pid, which also opened a TOCTOU window: the owner set could
    # change between the precondition and the licence, so a pid could be licensed against a set
    # that was never the one proved unstamped.
    legacy_owners: list[int] = []

    def _legacy_licence() -> tuple[bool, str]:
        """(is the `--legacy-pid` precondition met, why not) -- evaluated at most once per call."""
        nonlocal ok
        if "why" in legacy_state:
            return not legacy_state["why"], legacy_state["why"]
        owners_now = [p for p in port_owner_pids(port) if p] if port else []
        legacy_owners[:] = owners_now
        block = ""
        if not port:
            block = "this root records no port, so /health cannot be checked"
        elif not _health_answers(port):
            block = f"/health does not answer on the recorded port {port}"
        elif not owners_now:
            block = f"nothing owns port {port}"
        else:
            bad = []
            for owner in owners_now:
                unstamped, why_not = is_unstamped_loc_uvicorn(owner, root)
                if not unstamped:
                    bad.append(why_not)
            block = "; ".join(bad)
        legacy_state["why"] = block
        if block:
            ok = False
            notes.append(
                f"WARNING: --legacy-pid was REFUSED ({block}) -- it is only accepted when "
                f"/health answers on the recorded port and that port's owner is an unstamped "
                f"`app.main:app` python. Nothing was killed under it"
            )
        # Round-6 gate L2: a met PRECONDITION is not an acceptance and is not announced as one.
        # The old note here said "--legacy-pid ACCEPTED for the CURRENT OWNER(S) of port N [...]"
        # before any pid had been licensed -- it named owners that were never candidates, and it
        # printed on calls that then failed. Each ACCEPTED line is now emitted by `_legacy_licenses`
        # at the moment one specific pid is licensed, beside the per-pid refusal for every pid that
        # was not, so the notes say exactly what the flag licensed and what it did not.
        return not block, block

    def _legacy_licenses(pid: int) -> tuple[bool, str]:
        """(may `--legacy-pid` license THIS pid, why not) -- round-5 gate H1.

        The licence is per-PID, not per-call. The precondition proves something about the CURRENT
        OWNERS of this root's recorded port; round 4 then spent that licence on the RECORDED pids,
        which were never checked at all -- so a `server.supervisor.pid` that a reboot had left
        pointing at another root's stamped, live instance was killed with `taskkill /T /F` while
        `down` exited 0.

        Round-6 gate H1: requiring the pid to be a port OWNER was too narrow to do the flag's one
        job. uvicorn with `workers=1` runs a SUPERVISOR that forks the worker actually holding the
        port (`record_pids`), so the supervisor never appears in `netstat`; killing the worker alone
        makes uvicorn respawn it on the same socket seconds later. The canonical legacy instance --
        two pid files, no stamp -- was therefore un-`down`able. A pid is licensed when it is an
        unstamped `app.main:app` python AND it is either a current owner of the recorded port or the
        PARENT of one, using the same `_parent_pid` relation `record_pids` uses to pair the two."""
        licensed, block = _legacy_licence()
        if not licensed:
            return False, block
        owners = legacy_owners  # round-6 gate L3: resolved once per `down`, not once per pid
        if pid in owners:
            how = f"OWNS this root's recorded port {port}"
        elif children := [o for o in owners if _parent_pid(o) == pid]:
            how = (
                f"is the PARENT SUPERVISOR of that port's owner(s) {children}, which is how "
                f"uvicorn `workers=1` pairs the two"
            )
        else:
            return False, (
                f"pid {pid} is neither an owner of this root's recorded port {port} {owners} nor "
                f"the parent of one, and `--legacy-pid` only ever licenses that port's own "
                f"unstamped owner(s) and their parent supervisor"
            )
        unstamped, why_not = is_unstamped_loc_uvicorn(pid, root)
        if not unstamped:
            return False, why_not
        notes.append(
            f"--legacy-pid ACCEPTED for pid {pid}: it {how}, /health answers on port {port}, "
            f"every owner of that port is an unstamped `app.main:app` python, and so is this pid "
            f"-- it is taken as this instance's for THIS CALL ONLY"
        )
        return True, ""

    legacy_seen = False

    def _verdict(pid: int, trusted) -> tuple[bool, str]:
        """`pid_is_ours`, plus the one-call `--legacy-pid` acceptance (round-4 gate H1)."""
        nonlocal legacy_seen
        ours, why = pid_is_ours(pid, root, port, trusted)
        if ours:
            return True, why
        if "LEGACY INSTANCE" in why:
            legacy_seen = True
            if legacy_pid:
                licensed, block = _legacy_licenses(pid)
                if licensed:
                    return True, (
                        f"pid {pid} is an unstamped `app.main:app` python that owns this root's "
                        f"recorded port {port} or is the parent supervisor of an owner, accepted "
                        f"under `--legacy-pid` (/health answers there and every owner of that port "
                        f"is unstamped too)"
                    )
                why = f"{why}. `--legacy-pid` does NOT license this pid: {block}"
        return False, why

    # Ownership is decided for BOTH pids before either is killed: `taskkill /T` on the supervisor
    # takes the worker with it, and a dead pid has no command line left to prove itself with.
    proven: list[int] = []
    verdicts: list[tuple[str, int, str, str]] = []
    for label, pidname, pid in (
        ("supervisor", "server.supervisor.pid", supervisor),
        ("listener", "server.pid", listener),
    ):
        if not pid:
            continue
        if not _pid_alive(pid):
            notes.append(f"{label} pid {pid} was already gone")
            continue
        ours, why = _verdict(pid, proven)
        if not ours:
            ok = False
            # Round-3 gate M2: a pid we can PROVE is not ours makes this pid file worthless --
            # it names a stranger. Keeping it deadlocked the tool (`down` refused forever, `up`
            # saw "a live server" and told the operator to run `down`). The refusal still sets
            # ok=False so the exit code and the warning stand; only the useless file goes.
            # A refusal we could not decide (a broken CIM query) keeps its file: nothing was
            # proven there.
            # Round-4 gate H1: a LEGACY instance keeps its pid file. It is the only record of
            # which processes to kill by hand, and the pid it names is (almost certainly) this
            # instance's own live server -- deleting it destroyed the operator's only handle.
            dropped = ""
            if "CANNOT BE VERIFIED" not in why and "LEGACY INSTANCE" not in why:
                (root / pidname).unlink(missing_ok=True)
                dropped = (
                    f"; `{pidname}` named pid {pid}, which this instance does not own, so THAT "
                    f"FILE WAS DELETED (it was stale; the pid it held was {pid})"
                )
            if "LEGACY INSTANCE" in why:
                notes.append(
                    f"WARNING: {label} pid {pid} is a LEGACY INSTANCE of this tool ({why}) -- "
                    f"NOTHING was killed and `{pidname}` was KEPT. To take it down, re-run "
                    f"`down` with `--legacy-pid`: for one call it accepts a recorded pid that is "
                    f"an unstamped `app.main:app` OWNER of port {port} or the PARENT SUPERVISOR "
                    f"of such an owner, while /health answers there and every owner of that port "
                    f"is unstamped. A recorded pid that owns nothing and has no port-owning child "
                    f"is never licensed -- kill it by hand"
                )
                continue
            notes.append(
                f"WARNING: {label} pid {pid} belongs to another process, not this instance "
                f"({why}) -- not killing it{dropped}"
            )
            continue
        proven.append(pid)
        verdicts.append((label, pid, why, _creation_date(pid)))

    for label, pid, why, born in verdicts:
        if not _pid_alive(pid):
            # `taskkill /T` on the supervisor takes its worker with it: the LISTENER is normally
            # already gone by the time this loop reaches it, and that is the healthy path.
            notes.append(f"{label} pid {pid} went with the process tree")
            continue
        # Round-3 gate L1: the verdict above cost a full CIM round trip per pid, so it is about a
        # second old. A proven pid that exits inside that window and whose NUMBER is immediately
        # reused would take `taskkill /T /F` onto a stranger's whole tree. Creation time is the
        # identity a recycled pid cannot fake.
        now_born = _creation_date(pid)
        if born and now_born and now_born != born:
            ok = False
            notes.append(
                f"WARNING: {label} pid {pid} was REPLACED between the ownership check and the "
                f"kill (its creation time changed) -- not killing it"
            )
            continue
        _kill(pid)
        notes.append(f"killed {label} pid {pid} ({why})")

    # The backstop for an ORPHANED worker: something still serves this instance's port after both
    # recorded pids are gone. It runs only in exactly that state, and only against an owner that
    # proves itself ours.
    if port:
        # `taskkill /F` returns before the OS has finished tearing the process (and its socket)
        # down, so give the kills we just issued a few seconds to land. Without this pause the
        # ownership check below would run against our OWN dying process and refuse it.
        if proven:
            deadline = time.time() + 15.0
            while time.time() < deadline:
                if not port_owner_pids(port) and not any(
                    p and _pid_alive(p) for p in (supervisor, listener)
                ):
                    break
                _sleep(0.5)
        still_recorded = [p for p in (supervisor, listener) if p and _pid_alive(p)]
        owners = [p for p in port_owner_pids(port) if p]
        if not owners:
            notes.append(f"port {port} has no listener")
        elif still_recorded:
            ok = False
            notes.append(
                f"WARNING: port {port} still has listener(s) {owners} and recorded pid(s) "
                f"{still_recorded} are still alive -- not killing by port identity"
            )
        elif not _health_answers(port):
            ok = False
            notes.append(
                f"WARNING: port {port} is owned by pid(s) {owners}, not this instance "
                f"(this instance's pids are gone and /health does not answer there) "
                f"-- not killing it"
            )
        else:
            killed_owners: set[int] = set()
            for _ in range(20):
                owners = [p for p in port_owner_pids(port) if p]
                if not owners:
                    break
                acted = False
                for pid in owners:
                    born = _creation_date(pid)  # round-3 gate L1: identity, not just the number
                    ours, why = _verdict(pid, proven)
                    if not ours:
                        ok = False
                        notes.append(
                            f"WARNING: port {port} is owned by pid {pid}, not this instance "
                            f"({why}) -- not killing it"
                        )
                        continue
                    # Round-4 gate L1: the SAME two-non-empty-readings rule the pid loop uses.
                    # An owner that simply exits inside this window reads back "" -- that is a
                    # process that ended, not a recycled number, and calling it "was REPLACED"
                    # made a `down` that in fact freed the port exit 2 with a false claim.
                    now_born = _creation_date(pid)
                    if born and now_born and now_born != born:
                        ok = False
                        notes.append(
                            f"WARNING: port-{port} owner pid {pid} was REPLACED between the "
                            f"ownership check and the kill -- not killing it"
                        )
                        continue
                    _kill(pid)
                    killed_owners.add(pid)
                    proven.append(pid)
                    acted = True
                if not acted:
                    break
                _sleep(0.5)
            for pid in sorted(killed_owners):
                notes.append(f"killed port-{port} owner pid {pid}")
            if port_owner_pids(port):
                ok = False
                notes.append(f"WARNING: port {port} still has a listener")
            else:
                notes.append(f"port {port} has no listener")
    if ok:
        # Round-3 gate M1: `server.port` goes with the pid files. A cleanly stopped root is
        # RETIRED; leaving the port behind (and `_read_port`'s instance.md fallback) is what let a
        # stray later `down` on this root go hunting for whoever holds that number now.
        for name in ("server.pid", "server.supervisor.pid", "server.port"):
            (root / name).unlink(missing_ok=True)
    else:
        # Round-3 gate M2: the refusal names the ESCAPE HATCH -- which files, in which directory.
        kept = [n for n in ("server.pid", "server.supervisor.pid") if (root / n).exists()]
        live_owners = [p for p in port_owner_pids(port) if p] if port else []
        if kept:
            hatch = (
                f"the pid file(s) {', '.join(kept)} were KEPT (this `down` did not finish). "
                f"ESCAPE HATCH: if you know this instance is really gone (a reboot, or the "
                f"machine was rebuilt), delete {', '.join(kept)} and server.port in {root} by "
                f"hand, then re-run `down` (or `up` straight away). Round-5 gate M2: `up` reads "
                f"`server.port` even with no pid file, so if a server IS still holding this "
                f"root's recorded port it refuses rather than rebuilding the root under it"
            )
            if legacy_seen:
                hatch += (
                    f". If instead it is STILL RUNNING and was started before the stamp existed "
                    f"(a LEGACY instance), do NOT delete anything: re-run "
                    f"`down --legacy-pid`, which kills a recorded pid only while that pid is an "
                    f"unstamped `app.main:app` OWNER of port {port} (or the parent supervisor of "
                    f"one) and /health answers there"
                )
            notes.append(hatch)
        elif live_owners:
            # Round-4 gate H1: never invite a re-`up` while something is still serving. `up`
            # would rmtree this root -- key files, instance.md, seed.json -- out from under a
            # live server holding the datadir open. The pids the deleted files used to hold are
            # carried here, because that record is now the operator's only handle.
            ok = False
            notes.append(
                f"a server is STILL RUNNING on port {port}: kill pid(s) {live_owners} by hand "
                f"(they are the only handle left -- the pid file(s) that named them are gone). "
                f"Do NOT run `up` on this instance until that port is free"
            )
        else:
            notes.append(
                f"every recorded pid file named a process that is not this instance and has "
                f"been deleted from {root}; nothing is listening on "
                f"{f'port {port}' if port else 'any recorded port'}, so `up` can run again "
                f"(delete server.port there too if anything still refuses)"
            )
    return "; ".join(notes) or "nothing to stop", ok


# ======================================================================================= #
# MCP
# ======================================================================================= #
MCP_CLI_ENV = "LOC_TEST_MCP_CLI"


def _mcp_cli() -> str | None:
    """The MCP client CLI named by `LOC_TEST_MCP_CLI`, resolved on PATH (round-1 gate L2).

    There is NO default: the variable names any client whose CLI offers `mcp add`, `mcp list`
    and `mcp remove <name>` (the shape `claude mcp` and similar tools share). When it is unset
    (or blank) this returns None and every MCP registration step is SKIPPED cleanly -- `up`
    still succeeds and prints the equivalent stdio command instead.

    `shell=True` with a LIST argv routed the admin token through an extra `cmd.exe` command
    line for no benefit (one more same-user-readable copy). Resolving the launcher instead
    means the token reaches exactly one process."""
    configured = os.environ.get(MCP_CLI_ENV, "").strip()
    if not configured:
        return None
    found = shutil.which(configured)
    if not found:
        raise Abort(
            f"`{configured}` ({MCP_CLI_ENV}) is not on PATH "
            "(needed to register/inspect the MCP server)"
        )
    return found


def mcp_stdio_command(name: str, port: int, root: Path) -> str:
    """The equivalent stdio command for a client you register by hand, token REDACTED."""
    out_dir = root / "mcp-out"
    return (
        f"LOC_STUDIO_BASE_URL=http://127.0.0.1:{port} LOC_STUDIO_TOKEN=<admin key from "
        f"api-key.txt> LOC_STUDIO_OUTPUT_DIR={out_dir} {VENV_PY} {MCP_SERVER}"
    )


def mcp_register(name: str, port: int, token: str, root: Path) -> str:
    """Register `loc-os-<name>` with the MCP client CLI, REPLACING a stale entry of the same name.

    A previous instance (or a reboot that killed the server without a `down`) leaves the client
    registration behind pointing at a dead port and a REVOKED key. `mcp add` on an existing
    name is not a reliable upsert, so the stale row is removed first and the result says
    `re-registered` -- an instance.md that claims "registered" over somebody else's stale entry
    is the same fiction gate M2 closed for ports.

    With `LOC_TEST_MCP_CLI` unset nothing is registered and the result says so, naming the
    equivalent stdio command (the token is never printed)."""
    cli = _mcp_cli()
    if cli is None:
        return (
            f"skipped: {MCP_CLI_ENV} is not set, so no MCP client was touched; equivalent stdio "
            f"command: {mcp_stdio_command(name, port, root)}"
        )
    out_dir = root / "mcp-out"
    out_dir.mkdir(exist_ok=True)
    replaced = f"loc-os-{name}" in mcp_list()
    if replaced:
        mcp_unregister(name)
    argv = [
        cli,
        "mcp",
        "add",
        "--env",
        f"LOC_STUDIO_BASE_URL=http://127.0.0.1:{port}",
        "--env",
        f"LOC_STUDIO_TOKEN={token}",
        "--env",
        f"LOC_STUDIO_OUTPUT_DIR={out_dir}",
        f"loc-os-{name}",
        str(VENV_PY),
        str(MCP_SERVER),
    ]
    proc = _run(argv, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        # Never echo argv (it carries the token) -- AND scrub the child's own stderr, because a
        # CLI that prints the offending invocation in its usage text would otherwise put the
        # admin key straight into `facts["mcp_registered"]` and thence instance.md (gate L3).
        return f"mcp add FAILED rc={proc.returncode}: {scrub(proc.stderr or '', token)[:200]}"
    listing = mcp_list()
    if f"loc-os-{name}" not in listing:
        return "added but not listed"
    if replaced:
        return "re-registered (a stale entry of the same name was removed first)"
    return "registered"


def mcp_list() -> str:
    """`mcp list` stdout (read-only; the only verb safe to run against a live campaign).

    Empty when `LOC_TEST_MCP_CLI` is unset."""
    try:
        cli = _mcp_cli()
        if cli is None:
            return ""
        return _run([cli, "mcp", "list"], capture_output=True, text=True, timeout=60).stdout
    except Abort:
        return ""


def mcp_unregister(name: str) -> str:
    cli = _mcp_cli()
    if cli is None:
        return f"skipped: {MCP_CLI_ENV} is not set"
    proc = _run(
        [cli, "mcp", "remove", f"loc-os-{name}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return (proc.stdout or proc.stderr or "").strip()[:200] or f"rc={proc.returncode}"


def _mcp_env(port: int, token: str, root: Path) -> dict:
    env = dict(os.environ)
    for var in BLANKED_ENV_VARS:
        env[var] = ""
    env["LOC_STUDIO_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["LOC_STUDIO_TOKEN"] = token
    env["LOC_STUDIO_OUTPUT_DIR"] = str(root / "mcp-out")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


_MCP_HANDSHAKE = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-instance", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]


def _mcp_rpc(port: int, token: str, root: Path, request: dict, timeout: int = 180):
    """Drive the stdio MCP server through initialize + one request; return (msg, stderr).

    stdin is held OPEN while the reply is read: closing it (what `input=` does) makes the
    server shut down before an async tool call finishes, which reads as "no response"."""
    proc = _popen(
        [str(VENV_PY), str(MCP_SERVER)],
        cwd=str(MCP_SERVER.parent),
        env=_mcp_env(port, token, root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    found = None
    try:
        for msg in [*_MCP_HANDSHAKE, request]:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                got = json.loads(line)
            except Exception:  # noqa: S112 -- non-JSON stdout noise; keep reading for the reply
                continue
            if got.get("id") == request.get("id"):
                found = got
                break
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=15)
    err = ""
    with contextlib.suppress(Exception):
        err = proc.stderr.read() or ""
    return found, err


def mcp_call_tool(port: int, token: str, root: Path, name: str, arguments: dict) -> str:
    msg, err = _mcp_rpc(
        port,
        token,
        root,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    if msg is None:
        # Round-2 gate L1: the MCP child is launched with LOC_STUDIO_TOKEN=<admin key> in its
        # environment, so a traceback that quotes its own configuration would carry the key into
        # whatever prints this string. Scrub at the source, not at (some of) the call sites.
        return f"no response (stderr tail: {scrub(err, token).strip()[-200:]})"
    if msg.get("error"):
        return f"error: {json.dumps(msg['error'])[:300]}"
    result = msg.get("result") or {}
    texts = [c.get("text", "") for c in (result.get("content") or []) if isinstance(c, dict)]
    return f"isError={result.get('isError')} {' '.join(texts)[:300]}"


def mcp_tool_count(port: int, token: str, root: Path) -> tuple[int, str]:
    """Drive the MCP server over stdio with initialize + tools/list; return (count, note)."""
    msg, err = _mcp_rpc(
        port, token, root, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    if msg and isinstance(msg.get("result"), dict):
        return len(msg["result"].get("tools") or []), "stdio initialize + tools/list"
    # Round-2 gate L1: this note becomes facts["mcp_note"] and is RENDERED INTO instance.md, so
    # the admin key the child was handed must be blanked out of its stderr before it gets there.
    return -1, f"tools/list produced no result (stderr tail: {scrub(err, token).strip()[-200:]})"


# ======================================================================================= #
# seeding
# ======================================================================================= #
class Seeder:
    """Seeds one instance of every resource family; a family that fights back is recorded
    under `skipped` with its reason and the sweep moves on (the brief's 15-minute rule)."""

    def __init__(self, api: Api, env: dict, root: Path, ollama_base: str, engine_set: str = "both"):
        self.api = api
        self.env = env
        self.root = root
        self.ollama_base = ollama_base
        self.engine_set = engine_set
        # The model id every `provider: local` slot resolves to. Under `--engines ollama` that is
        # the Ollama box's id, so no seeded row can name (or reach) LM Studio; under `both` and
        # `lmstudio` it is the LM Studio id. `ollama_base` is "" under `lmstudio`.
        self.text_model = OLLAMA_MODEL if engine_set == "ollama" else LM_STUDIO_MODEL
        self.ids: dict = {}
        self.skipped: list[tuple[str, str]] = []
        # Round-1 gate M4: a REACHABILITY PROBE is not a seed. `/api/worklist` and
        # `/api/reports/run` are computed surfaces -- neither creates a row -- and filing them
        # under "seeded ids" told the tester a worklist item and a report run existed. They live
        # here now and render under their own heading.
        self.probes: dict = {}

    def step(self, family: str, fn, *, probe: bool = False):
        try:
            value = fn()
            if value is not None:
                (self.probes if probe else self.ids)[family] = value
            kind = "probed" if probe else "seeded"
            _say(f"  {kind} {family}: {json.dumps(value, default=str)[:160]}")
        except Exception as exc:
            # Round-2 gate L5: this comment used to claim a scrub the next line did not do. It
            # does it now, and with the value that is actually in reach: `Api` raises Aborts
            # built from a response BODY, and a 4xx/5xx body that echoes the request (a proxy
            # error page, a validation dump of the headers) can quote the admin token this
            # seeder authenticates with. The reason string lands in instance.md's "Not seeded"
            # table, so it is scrubbed before it is stored.
            # Round-3 gate L5: `self.api._token` DIRECTLY, not `getattr(..., "")`. The getattr
            # form degraded silently -- rename `Api._token` and `scrub` gets "" (a documented
            # no-op), so the scrub switches off with every test still green. An AttributeError
            # here is the signal.
            reason = scrub(str(exc).replace("\n", " "), self.api._token)[:300]
            self.skipped.append((family, reason))
            _say(f"  SKIP {family}: {reason[:160]}")

    # -- individual families ------------------------------------------------------------
    def run_all(self) -> None:
        api = self.api

        self.step(
            "customer_1", lambda: api.jpost("/api/customers", {"name": "Acme Robotics"})["id"]
        )
        self.step(
            "customer_2",
            lambda: api.jpost("/api/customers", {"name": "Globex (confinement)"})["id"],
        )
        c1 = self.ids.get("customer_1")
        c2 = self.ids.get("customer_2")

        self.step(
            "content_type", lambda: api.jpost("/api/content-types", {"name": "UI strings"})["id"]
        )
        self.step(
            "langset",
            lambda: api.jpost(
                "/api/langsets", {"name": "EU core", "source": "en-US", "targets": "es-ES,fr-FR"}
            )["id"],
        )
        self.step(
            "segmentation_profile",
            lambda: api.jpost("/api/segprofiles", {"name": "Test SRX", "description": "seeded"})[
                "segprofile"
            ]["id"],
        )
        self.step(
            "style_guide",
            lambda: api.jpost(
                "/api/style-guides",
                {"name": "Acme tone", "body": "Be concise. Use the formal register."},
            )["id"],
        )

        self.step(
            "project_document",
            lambda: api.jpost(
                "/api/projects",
                {
                    "name": "Acme Docs (document)",
                    "customer_id": c1,
                    "source_locale": "en-US",
                    "targets": "es-ES",
                    "project_type": "document",
                    "provider": "local",
                },
            )["id"],
        )
        self.step(
            "project_key_based",
            lambda: api.jpost(
                "/api/projects",
                {
                    "name": "Acme App (key_based)",
                    "customer_id": c1,
                    "source_locale": "en-US",
                    "targets": "es-ES",
                    "project_type": "key_based",
                    "provider": "local",
                },
            )["id"],
        )
        self.step(
            "project_customer2",
            lambda: api.jpost(
                "/api/projects",
                {
                    "name": "Globex Docs (confinement target)",
                    "customer_id": c2,
                    "source_locale": "en-US",
                    "targets": "fr-FR",
                    "project_type": "document",
                },
            )["id"],
        )
        pdoc = self.ids.get("project_document")

        self.step("document_text", lambda: self._document(pdoc))
        self.step("segments", lambda: self._segments())
        self.step(
            "document_short",
            lambda: self._document(
                pdoc, name="seed-short.txt", body="Save changes.\nDelete this item.\n"
            ),
        )
        self.step("keys_namespace", lambda: self._keys())
        self.step("image_document", self._image_document)

        self.step("engine_profiles", self._engines)
        self.step("workflow_mt", lambda: self._workflow_mt())
        self.step("workflow_lqa_qe", lambda: self._workflow_lqa())
        self.step("run_default_engine", self._run_on_default_engine)

        self.step("tm_entry", self._tm)
        self.step("glossary", lambda: self._glossary(c1))
        self.step("terminology_concept", self._concept)
        self.step("vendor", lambda: api.jpost("/api/vendors", {"name": "Acme LSP"})["vendor"]["id"])
        self.step(
            "budget_department",
            lambda: (
                api.jpost("/api/budgets", {"department": "marketing", "limit": 250}) or "marketing"
            ),
        )
        self.step("lqa_finding", self._lqa)
        self.step("connection", self._connection)
        self.step("project_template", lambda: self._template(pdoc))
        self.step(
            "prompt_template",
            lambda: api.jpost(
                "/api/prompt-templates",
                {"name": "Seed MT prompt", "body": "Translate to {target}: {text}"},
            )["id"],
        )
        self.step("memory_manual", lambda: self._memory(c1))
        self.step("memory_suggestion", lambda: self._memory_suggestion(c1))
        self.step("query_thread", lambda: self._query(pdoc))
        self.step("routing_rule", self._routing)
        # PROBES, not seeds (gate M4): both are computed read surfaces that persist nothing.
        self.step("reports_run_probe", self._report, probe=True)
        self.step("worklist_probe", self._worklist, probe=True)
        self.step("export_run", self._export)
        self.step("evaluation", self._evaluation)
        self.step("advanced_mission", lambda: self._advanced(pdoc))

    # -- helpers ------------------------------------------------------------------------
    def _document(self, pid, name: str = "seed-source.txt", body: str = ""):
        # The ollama set gets a two-line corpus: that box runs an 8k context and every seeded run
        # (workflow MT, the engine-ref proof, the Advanced expansion) walks this document.
        body = body or (
            "The robot arm calibrates itself on startup.\nPress Start to begin.\n"
            if self.engine_set == "ollama"
            else (
                "The robot arm calibrates itself on startup.\n"
                "Press Start to begin a new production run.\n"
                "Maintenance is due every ninety days.\n"
                "Contact support if the status light blinks red.\n"
            )
        )
        src = self.root / name
        src.write_text(body, encoding="utf-8")
        with open(src, "rb") as fh:
            r = self.api.upload(
                "/api/documents",
                files={"file": (name, fh, "text/plain")},
                data={"project_id": str(pid), "mode": "segmented"},
            )
        if r.status_code >= 400:
            raise Abort(f"POST /api/documents -> {r.status_code} {r.text[:200]}")
        return r.json().get("id") or r.json().get("document_id")

    def _segments(self):
        doc = self.ids.get("document_text")
        # the light shape is per-locale COUNTS; ?detail=segments is the opt-in that carries ids
        rows = self.api.jget(f"/api/documents/{doc}/segments", params={"detail": "segments"})
        ids = []
        for entry in (rows.get("locales") or {}).values():
            ids.extend(s.get("id") for s in (entry.get("segment_details") or []))
        if not ids:
            raise Abort("document has no segments")
        return ids[:5]

    def _keys(self):
        pid = self.ids.get("project_key_based")
        payload = {
            "keys": [
                {
                    "name": "app::start",
                    "namespace": "mobile",
                    "base": "Start",
                    "description": "CTA",
                },
                {"name": "app::cancel", "namespace": "mobile", "base": "Cancel"},
                {"name": "app::retry", "namespace": "mobile", "base": "Try again"},
            ]
        }
        out = self.api.jpost(f"/api/projects/{pid}/keys", payload)
        created = out.get("created") or []  # a list of key IDS (ints), not objects
        ids = [k.get("id") if isinstance(k, dict) else k for k in created]
        if ids and ids[0]:
            self.api.put(f"/api/keys/{ids[0]}/values/es-ES", {"text": "Empezar"})
        return {"namespace": "mobile", "key_ids": ids}

    def _image_document(self):
        maker = _ROOT / "scripts" / "make_demo_image.py"
        if not maker.exists():
            raise Abort("scripts/make_demo_image.py missing")
        out = self.root / "seed-image.png"
        proc = _run(
            [str(VENV_PY), str(maker), "-o", str(out)],
            cwd=str(_ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if not out.exists():
            # the maker may take a positional path instead
            proc = _run(
                [str(VENV_PY), str(maker), str(out)],
                cwd=str(_ROOT),
                env=self.env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        if not out.exists():
            raise Abort(f"make_demo_image produced nothing ({(proc.stderr or '')[:120]})")
        pid = self.ids.get("project_document")
        with open(out, "rb") as fh:
            r = self.api.upload(
                "/api/images",
                files={"file": ("seed-image.png", fh, "image/png")},
                data={"project_id": str(pid)},
            )
        if r.status_code >= 400:
            raise Abort(f"POST /api/images -> {r.status_code} {r.text[:200]}")
        return r.json().get("id") or r.json().get("document_id")

    def _engines(self):
        # `--engines ollama` creates NO LM Studio profile: an engine ring that carries a card for
        # a server this instance must never call is exactly the mis-seed the tester would trip on.
        if self.engine_set != "ollama":
            cli(
                self.env,
                "engine",
                "create",
                "--name",
                "LM Studio (gemma-4-12b)",
                "--kind",
                "local",
                "--base-url",
                LM_STUDIO_URL,
                "--model",
                LM_STUDIO_MODEL,
            )
        # ...and `--engines lmstudio` creates NO Ollama profile, for the same reason in mirror:
        # `preflight_engines` never probed that box, `self.ollama_base` is "", and a profile
        # pointing at an empty base url is a card for a server that does not exist here.
        if self.engine_set != "lmstudio":
            cli(
                self.env,
                "engine",
                "create",
                "--name",
                "Ollama box (gemma4:e4b-it-qat)",
                "--kind",
                "openai_compatible",
                "--base-url",
                self.ollama_base,
                "--model",
                OLLAMA_MODEL,
                "--secret",
                f"api_key={OLLAMA_API_KEY}",  # pregate: allow-secret placeholder bearer
            )
        listed = cli(self.env, "engine", "list", as_json=True)
        rows = listed if isinstance(listed, list) else listed.get("engines", [])
        # Round-7 gate L: this list is what instance.md prints under "Engine profiles created",
        # beside prose saying no second engine profile exists. On a throwaway datadir the ring
        # holds exactly what was created two lines up, so anything else is a contradiction the
        # facts sheet must not paper over -- refuse instead of printing both.
        if self.engine_set == "lmstudio":
            names = [str(r.get("name", "")) for r in rows if isinstance(r, dict)]
            if len(rows) != 1 or not any("LM Studio" in n for n in names):
                raise Abort(
                    "--engines lmstudio expects exactly ONE engine profile in the ring (LM "
                    f"Studio), found {len(rows)}: {', '.join(names) or '(unnamed)'}"
                )
        # The Ollama profile's id is what an `engine:<id>` provider ref needs (the Advanced
        # roster and the engine-#2 MT proof both resolve through it) -- remember it here rather
        # than re-listing later. Under `lmstudio` there is no such profile, so the adoption loop
        # does not run at all: an Ollama-shaped row would be a stranger, never engine #2.
        if self.engine_set != "lmstudio":
            for row in rows:
                if isinstance(row, dict) and "Ollama" in str(row.get("name", "")):
                    self.ids["engine_ollama_id"] = row.get("id")
        return [{"id": e.get("id"), "name": e.get("name"), "kind": e.get("kind")} for e in rows]

    def _workflow_mt(self):
        wid = self.api.jpost("/api/workflows", {"name": "Seed MT + TM", "scope": "global"})["id"]
        self.api.jpost(
            f"/api/workflows/{wid}/steps",
            {"step_type": "tm_match", "name": "TM leverage", "fill_threshold": 95},
        )
        self.api.jpost(
            f"/api/workflows/{wid}/steps",
            {
                "step_type": "llm_agent",
                "name": f"MT ({self.engine_label()})",
                "provider": "local",
                "model": self.text_model,
                "prompt_text": "Translate the following text into {target_locale}. "
                "Return only the translation.\n\n{text}",
            },
        )
        return wid

    def _workflow_lqa(self):
        wid = self.api.jpost("/api/workflows", {"name": "Seed LQA + QE gate", "scope": "global"})[
            "id"
        ]
        self.api.jpost(
            f"/api/workflows/{wid}/steps",
            {
                "step_type": "llm_agent",
                "name": "MT with QE gate",
                "provider": "local",
                "model": self.text_model,
                "prompt_text": "Translate into {target_locale}:\n\n{text}",
                "qe_gate": True,
                "qe_threshold": 70,
                "qe_provider": "local",
            },
        )
        self.api.jpost(
            f"/api/workflows/{wid}/steps",
            {
                "step_type": "lqa",
                "name": "LQA judge",
                "provider": "local",
                "model": self.text_model,
                "lqa_threshold": 60,
            },
        )
        self.api.jpost(
            f"/api/workflows/{wid}/steps",
            {"step_type": "human_review", "name": "Reviewer sign-off"},
        )
        return wid

    def engine_label(self) -> str:
        """How the DEFAULT (`local`) provider should be named in seeded rows and instance.md."""
        return "Ollama box" if self.engine_set == "ollama" else "LM Studio"

    def _run_on_default_engine(self):
        """Run the seeded MT workflow on the DEFAULT provider (`local`) end to end.

        Under `--engines ollama` `local` IS the Ollama box, so this is the run that proves the
        box actually served the product. The returned `steps` rows are the product's own
        per-step forensic record (provider + model as the run resolved them), not our claim."""
        wid = self.ids.get("workflow_mt")
        doc = self.ids.get("document_text")
        started = self.api.jpost(f"/api/workflows/{wid}/run", {"doc_id": doc, "locale": "es-ES"})
        rid = started.get("run_id") or started.get("id")
        deadline = time.time() + 900
        status = started.get("status", "running")
        while time.time() < deadline and status in ("running", "queued", "pending"):
            _sleep(5)
            status = self.api.jget(f"/api/runs/{rid}").get("status", "?")
        detail = self.api.jget(f"/api/runs/{rid}")
        # `failure_shape` is the per-step forensic row: it carries the provider + model the
        # run ACTUALLY used, which is the engine id as observed THROUGH the product.
        return {
            "run_id": rid,
            "status": status,
            "document_id": doc,
            "locale": "es-ES",
            "steps": [
                {
                    "index": r.get("index"),
                    "status": r.get("status"),
                    "provider": r.get("provider"),
                    "model": r.get("model"),
                    "tokens_out": r.get("output_chars"),
                }
                for r in (detail.get("failure_shape") or [])
            ],
        }

    def _tm(self):
        pid = self.ids.get("project_document")
        segs = self.ids.get("segments") or []
        if segs:
            r = self.api.post(f"/api/segments/{segs[0]}/approve")
            if r.status_code < 400:
                return {"via": "segment approve -> TM write-back", "segment_id": segs[0]}
        import base64

        tmx = (
            '<?xml version="1.0" encoding="utf-8"?><tmx version="1.4"><header srclang="en-US" '
            'adminlang="en-US" segtype="sentence" datatype="plaintext" '
            'creationtool="test-instance" '
            'creationtoolversion="1" o-tmf="tmx"/><body><tu><tuv xml:lang="en-US"><seg>Start</seg>'
            '</tuv><tuv xml:lang="es-ES"><seg>Empezar</seg></tuv></tu></body></tmx>'
        )
        out = self.api.jpost(
            f"/api/projects/{pid}/tm/import",
            {
                "tmx_b64": base64.b64encode(tmx.encode("utf-8")).decode("ascii"),
                "source": "en-US",
                "target": "es-ES",
            },
        )
        return {"via": "tmx import", "result": out}

    def _glossary(self, customer_id):
        gid = self.api.jpost("/api/glossaries", {"name": "Acme terms", "customer_id": customer_id})[
            "id"
        ]
        self.api.jpost(f"/api/glossaries/{gid}/pairs", {"pairs": ["en-US:es-ES"]})
        self.api.jpost(
            f"/api/glossaries/{gid}/terms",
            {
                "source_term": "robot arm",
                "target_term": "brazo robotico",
                "source": "en-US",
                "target": "es-ES",
            },
        )
        self.api.post(f"/api/glossaries/{gid}/approve")
        return gid

    def _concept(self):
        gid = self.ids.get("glossary")
        out = self.api.jpost(
            f"/api/glossaries/{gid}/concepts",
            {
                # a term the glossary does not already carry: `glossary add-term` DUAL-WRITES
                # a concept when terminology_concept_editor is on, so reusing "robot arm"
                # collides on the homonym guard (400), not on the editor seam.
                "domain": "robotics",
                "definition": "The powered jaw at the end of the arm.",
                "terms": [{"term": "gripper", "locale": "en-US"}],
            },
        )
        return out.get("id") or out

    def _lqa(self):
        doc = self.ids.get("document_text")
        segs = self.ids.get("segments") or []
        out = self.api.jpost(
            f"/api/documents/{doc}/lqa/findings",
            {
                "segment_id": segs[0],
                "dimension": "Terminology",
                "severity": "minor",
                "note": "Seeded finding for the tester sweep.",
            },
        )
        return out.get("id") or out

    def _connection(self):
        out = self.api.jpost(
            "/api/connectors",
            {"kind": "airtable", "name": "Seed Airtable", "config": {"base_id": "appSEED"}},
        )
        return out.get("id") or out

    def _template(self, pid):
        out = self.api.jpost(
            "/api/templates", {"name": "Seed template", "project_id": pid, "scope": "global"}
        )
        return (out.get("template") or {}).get("id") or out

    def _memory(self, customer_id):
        out = self.api.jpost(
            "/api/memories/note",
            {
                "customer_id": customer_id,
                # term_decision is a TERM kind: llmemory.add_note requires BOTH terms
                # and ignores `body` (it returns None -- a bare 400 -- without them).
                "kind": "term_decision",
                "body": "Always translate 'Start' as 'Empezar' for Acme UI buttons.",
                "source_term": "Start",
                "target_term": "Empezar",
                "source_locale": "en-US",
                "target_locale": "es-ES",
            },
        )
        return out.get("id") or out

    def _memory_suggestion(self, customer_id):
        """Generate memory suggestions -- and count a run that PROPOSED NOTHING as a SKIP.

        Round-1 gate M4: this used to return the generator's response whatever it said, so a
        `{"scanned": 0, "proposed": 0}` landed in the seed table as if a suggestion row existed.
        The tester then probed an empty queue and had no "not seeded" row to explain it."""
        out = self.api.jpost("/api/memory-suggestions/generate", {"customer_id": customer_id})
        proposed = 0
        if isinstance(out, dict):
            for field in ("proposed", "created", "suggestions"):
                value = out.get(field)
                if isinstance(value, int):
                    proposed = max(proposed, value)
                elif isinstance(value, list):
                    proposed = max(proposed, len(value))
        if proposed <= 0:
            raise Abort(
                "generate ran and proposed 0 suggestions (nothing in this fresh instance to "
                f"mine yet): {json.dumps(out, default=str)[:200]}"
            )
        return out

    def _query(self, pid):
        out = self.api.jpost(
            "/api/queries",
            {
                "project_id": pid,
                "subject": "Seeded query",
                "body": "Is 'Start' a verb or a noun here?",
            },
        )
        qid = (out.get("thread") or {}).get("id") or out.get("id")
        if qid:
            self.api.post(f"/api/queries/{qid}/posts", {"body": "Verb.", "answer": True})
        return qid or out

    def _routing(self):
        out = self.api.jpost(
            "/api/routing/rules",
            {
                "ext": "docx",
                "department": "marketing",
                "priority": 100,
                "project": str(self.ids.get("project_document") or ""),
                "provider": "local",
            },
        )
        return out.get("id") or out

    def _report(self):
        r = self.api.get("/api/reports/run", params={"kind": "cost", "group_by": "provider"})
        if r.status_code >= 400:
            raise Abort(f"GET /api/reports/run -> {r.status_code} {r.text[:200]}")
        body = r.json()
        rows = body.get("rows") if isinstance(body, dict) else body
        return {"kind": "cost", "rows": len(rows or [])}

    def _export(self):
        r = self.api.post("/api/exports", {"scope": "self", "emitters": ["jsonl"]})
        if r.status_code >= 400:
            raise Abort(f"POST /api/exports -> {r.status_code} {r.text[:200]}")
        body = r.json()  # 202 {"ok", "run_uuid", "job_id", "status_url"}
        rid = body.get("run_uuid")
        job = body.get("job_id")
        status = "?"
        for _ in range(10):
            _sleep(2)
            st = self.api.get(f"/api/exports/{rid}")
            if st.status_code >= 400:
                break
            status = st.json().get("status", "?")
            if status in ("ready", "done", "failed"):
                break
        return {
            "export_run_uuid": rid,
            "job_id": job,
            "status": status,
            "note": "submission is what this proves; the staging JOB is async and this "
            "posture runs no daemons -- run it with `loc-tms job run <id>` or a tick.",
        }

    def _evaluation(self):
        api = self.api
        ds = api.jpost(
            "/api/evaluations/datasets",
            {
                "name": "Seed eval set",
                "source_locale": "en-US",
                "target_locale": "es-ES",
                "text": "Start\tEmpezar\nCancel\tCancelar\nTry again\tIntentar de nuevo\n",
            },
        )
        did = ds.get("id") or ds.get("dataset_id")
        cand = api.jpost(
            "/api/evaluations/candidates",
            {
                "name": f"Seed candidate ({self.engine_label()})",
                "workflow_id": self.ids.get("workflow_mt"),
            },
        )
        cid = cand.get("id") or cand.get("candidate_id")
        est = api.jpost("/api/evaluations/runs/start", {"dataset_id": did, "candidate_ids": [cid]})
        estimate = (est.get("estimate") or {}).get("total_usd", 0.0)
        started = api.jpost(
            "/api/evaluations/runs/start",
            {
                "dataset_id": did,
                "candidate_ids": [cid],
                "confirm": True,
                "acknowledge_estimate_usd": estimate,
                "allow_unlimited": True,
            },
        )
        run_ids = started.get("run_ids") or []
        rid = run_ids[0] if run_ids else None
        status = "?"
        deadline = time.time() + 900
        while rid and time.time() < deadline:
            got = api.get(f"/api/evaluations/runs/{rid}")
            status = got.json().get("status", "?") if got.status_code < 400 else "?"
            if status not in ("running", "queued", "pending"):
                break
            _sleep(5)
        return {
            "dataset_id": did,
            "items": 3,
            "candidate_id": cid,
            "run_ids": run_ids,
            "status": status,
            "cost_usd": started.get("cost_usd"),
        }

    def _advanced(self, pid):
        """Create ONE Conductor mission whose every committee slot names a REAL engine.

        Round-1 gate M3: the roster used to be `{"target_locale": "es-ES"}` and nothing else,
        and an unspecified slot defaults to `{"provider": "mock"}` (conductor.py
        `_fallback_text_engine`, `hosts.engine_host` -> "mock"). Every Advanced probe the tester
        ran was therefore deterministic OFFLINE fabrication while instance.md claimed nothing
        seeded here used mock. Now: sighted delegate + adjudicator on engine #1 (LM Studio,
        `local`), the CONTEXT-BLIND delegate on engine #2 (the Ollama box, by `engine:<id>` ref)
        -- the blind slot is the smallest prompt in the mission (source + target locale only),
        which is the one that fits that box's small-call budget. The roster default provider is
        `local`, so the unset `judge`/`vision` slots resolve to a real engine too, never mock.
        At least one text delegate must be sighted or /api answers 400.

        Under `--engines ollama` the SAME shape holds with one server behind it: `local` is the
        Ollama box (LOCAL_BASE_URL) and the `engine:<id>` ref is that box's profile, so both
        delegates and the adjudicator are real and neither reaches LM Studio.

        Under `--engines lmstudio` there is no engine #2 and no `engine_ollama_id`, so the blind
        delegate falls back to `local` + the LM Studio model like every other slot: one real
        server, no ref to a profile this instance does not have, still nothing on `mock`."""
        return self.api_create_advanced_mission(pid)

    def api_create_advanced_mission(self, pid, name: str = "Seed mission"):
        model = getattr(self, "text_model", LM_STUDIO_MODEL)
        ollama_ref = self.ids.get("engine_ollama_id")
        blind = (
            {"provider": f"engine:{ollama_ref}", "context_blind": True}
            if ollama_ref
            else {"provider": "local", "model": model, "context_blind": True}
        )
        out = self.api.jpost(
            "/api/advanced/missions",
            {
                "project_id": pid,
                "name": name,
                "brief": "Translate the seeded document into es-ES with the local engines.",
                # document_ids/segment_ids are only accepted on a CONDUCTOR mission over a
                # document project (the route refuses them on a scripted one).
                "mode": "conductor",
                "budget_usd": 1.0,
                "document_ids": [self.ids.get("document_short")],
                # target_locale is a MODEL_ROSTER field, not a top-level one: /api folds only
                # mode/committee/units/research down into the roster, and the document expansion
                # reads roster["target_locale"].
                "model_roster": {
                    "target_locale": "es-ES",
                    "provider": "local",
                    "model": model,
                    "committee": {
                        "text": [
                            {"provider": "local", "model": model},
                            blind,
                        ],
                        "adjudicator": {"provider": "local", "model": model},
                    },
                },
            },
        )
        mission = out.get("mission") or {}
        # The create response renders the parsed roster back as `mission["committee"]` (the
        # Committee.as_view() shape). Recording THAT -- not the body we sent -- is what makes
        # seed.json auditable for gate M3: it shows what the product actually stored.
        roster = mission.get("model_roster") or {}
        committee = mission.get("committee") or (
            roster.get("committee") if isinstance(roster, dict) else None
        )
        return {
            "mission_id": mission.get("id") or out,
            "ran": False,
            "note": "CREATE ONLY -- this mission has never been launched (no run row, no "
            "transcript). Launch it yourself if the sweep needs one.",
            "committee": committee,
        }

    def _worklist(self):
        r = self.api.get("/api/worklist")
        if r.status_code >= 400:
            raise Abort(f"GET /api/worklist -> {r.status_code} {r.text[:200]}")
        body = r.json()
        return {"reachable": True, "sections": list(body)[:8] if isinstance(body, dict) else "list"}


# ======================================================================================= #
# live proofs (brief step 9)
# ======================================================================================= #
def _feature_rows(body):
    wrapped = body.get("features") or body.get("rows") if isinstance(body, dict) else body
    rows = wrapped or []
    return [r for r in rows if isinstance(r, dict)]


def _row_state(row: dict) -> str:
    for field in ("enabled", "on", "state", "value"):
        if field in row:
            val = row[field]
            if isinstance(val, bool):
                return "on" if val else "off"
            return str(val)
    return "?"


def run_proofs(
    api: Api,
    env: dict,
    base: str,
    port: int,
    viewer_token: str,
    root: Path,
    ids: dict,
    engine_set: str = "both",
) -> list:
    proofs: list[tuple[str, str]] = []

    def rec(label, value):
        # Gate L3: every proof cell is scrubbed of BOTH tokens before it is stored or echoed --
        # several of these values are a child's raw stderr, and a CLI that quotes its own
        # invocation would otherwise put a live key into instance.md.
        safe = scrub(str(value), api._token, viewer_token).replace("\n", " ")
        proofs.append((label, safe[:1600]))
        _say(f"  proof {label}: {safe[:140]}")

    r = api.get("/health")
    rec("GET /health", f"{r.status_code} {r.text[:200]}")
    r = api.get("/api/release")
    rec("GET /api/release", f"{r.status_code} {r.text[:300]}")

    r = api.get("/api/admin/features")
    if r.status_code == 200:
        rows = _feature_rows(r.json())
        rec("GET /api/admin/features (admin key)", f"200, {len(rows)} features")
        rec(
            "feature slug=state(semantics)",
            "; ".join(
                f"{f.get('slug')}={_row_state(f)}"
                f"({f.get('semantics') or ('opt_in' if f.get('opt_in') else '')})"
                for f in rows
            ),
        )
    else:
        rec("GET /api/admin/features (admin key)", f"{r.status_code} {r.text[:200]}")

    r = api.get("/api/admin/features", token="")
    rec("GET /api/admin/features (NO key) -- expect 401", f"{r.status_code} {r.text[:120]}")
    r = api.get("/api/admin/features", token=viewer_token)
    rec("GET /api/admin/features (viewer key) -- expect 403", f"{r.status_code} {r.text[:120]}")

    # engine ring: only the two local engines, and each answers its own live probe
    listed = cli(env, "engine", "list", as_json=True, check=False)
    engines = listed if isinstance(listed, list) else (listed or {}).get("engines", [])
    rec("engine list (in-process)", json.dumps(engines, default=str)[:400])
    for eng in engines if isinstance(engines, list) else []:
        if not isinstance(eng, dict) or not eng.get("id"):
            continue
        proc = cli(env, "engine", "test", str(eng["id"]), check=False, timeout=300)
        rec(
            f"engine test #{eng['id']} ({eng.get('name')})",
            f"rc={proc.returncode} {(proc.stdout or proc.stderr).strip()[:200]}",
        )

    # There is NO /api route that lists engine profiles: the engine ring is host-only
    # (`engine list` above). What the wire can be asked is whether a CLOUD provider ref is
    # listable at all -- with every cloud key blanked, none of them is.
    for cloud in ("claude", "openai", "gemini"):
        probe = cli(env, "providers", "models", "--provider", cloud, check=False, timeout=180)
        rec(
            f"cloud provider `{cloud}` is unusable (no credential)",
            f"rc={probe.returncode} {(probe.stdout or probe.stderr).strip()[:160]}",
        )

    # CLI in-process + remote
    proc = cli(env, "config", "features", check=False)
    rec(
        "CLI in-process `config features`",
        f"rc={proc.returncode}, {len((proc.stdout or '').splitlines())} lines",
    )
    token = api._token
    proc = _run(
        [str(VENV_PY), "-m", "app.cli", "--remote", base, "--token", token, "config", "features"],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    rec(
        "CLI --remote `config features`",
        f"rc={proc.returncode}, {len((proc.stdout or '').splitlines())} lines"
        f"{'' if proc.returncode == 0 else ' :: ' + (proc.stderr or '')[:160]}",
    )

    # feature flip round trip over --remote: reports OFF -> 404 -> ON -> 200
    def remote(*args):
        return _run(
            [str(VENV_PY), "-m", "app.cli", "--remote", base, "--token", token, *args],
            cwd=str(_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    # Gate L4: the RESTORE arm is in a `finally`. Both MCP stdio calls below carry a 180 s
    # timeout, and an exception between the disable and the enable used to hand the tester an
    # instance with `reports` OFF while instance.md said ON.
    before = api.get("/api/reports/run", params={"kind": "cost", "group_by": "provider"})
    off = remote("config", "feature", "disable", "reports")
    try:
        during = api.get("/api/reports/run", params={"kind": "cost", "group_by": "provider"})
        mcp_off = mcp_tool_count(port, token, root)
        mcp_refusal = mcp_call_tool(port, token, root, "run_report", {"kind": "cost"})
    finally:
        on = remote("config", "feature", "enable", "reports")
    after = api.get("/api/reports/run", params={"kind": "cost", "group_by": "provider"})
    mcp_on = mcp_call_tool(port, token, root, "run_report", {"kind": "cost"})
    rec(
        "feature flip round-trip (reports, over --remote)",
        f"baseline {before.status_code} -> disable rc={off.returncode} -> "
        f"GET /api/reports/run {during.status_code} -> enable rc={on.returncode} -> "
        f"{after.status_code}",
    )
    rec(
        "MCP while `reports` is OFF",
        f"tools/list count {mcp_off[0]} (tool NOT removed from the list); "
        f"tools/call run_report -> {mcp_refusal}",
    )
    rec("MCP after `reports` is back ON", f"tools/call run_report -> {mcp_on}")

    # host-only key flipped IN-PROCESS while the server runs
    # Gate L4 again: same try/finally shape, so a failure mid-probe cannot leave the campaign's
    # instance with data_export switched OFF behind an instance.md that says otherwise.
    base_export = api.post("/api/exports", {"scope": "self", "emitters": ["jsonl"]})
    hostflip = cli(env, "config", "set", "data_export_enabled", "0", "--yes", check=False)
    try:
        _sleep(1.0)
        during_export = api.post("/api/exports", {"scope": "self", "emitters": ["jsonl"]})
        remote_try = remote("config", "feature", "enable", "data_export")
    finally:
        cli(env, "config", "set", "data_export_enabled", "1", "--yes", check=False)
    _sleep(1.0)
    after_export = api.post("/api/exports", {"scope": "self", "emitters": ["jsonl"]})
    rec(
        "host-only flip seen by the RUNNING server (data_export_enabled)",
        f"before POST /api/exports {base_export.status_code} -> host `config set ...=0` "
        f"rc={hostflip.returncode} -> {during_export.status_code} -> restored "
        f"{after_export.status_code}; remote enable attempt rc={remote_try.returncode} "
        f"({(remote_try.stderr or remote_try.stdout or '').strip()[:140]})",
    )

    proof_engine_two(api, env, ids, engine_set, rec)
    return proofs


_RUN_OK = ("completed", "complete", "succeeded", "success", "done", "ok")


def _seeded_run_standing(ids: dict) -> str:
    """One honest sentence about the seeded default-engine run, from what was RECORDED.

    Reviewer round-1 MED: the skipped engine-#2 row used to assert that the seeded run "already
    proved end to end" whatever actually happened. A seeded run that failed, that is still
    `running` when the 15-minute deadline expired, or whose whole step raised (so it landed in
    `skipped` and never reached `ids`) must be reported as exactly that."""
    run = ids.get("run_default_engine")
    if not isinstance(run, dict):
        return (
            "No seeded workflow run is recorded for the default engine, so nothing is claimed "
            "about it here -- read the seeded-families and not-seeded tables above."
        )
    status = str(run.get("status") or "?")
    where = f"run {run.get('run_id')}"
    if status.lower() in _RUN_OK:
        return f"The seeded workflow run above ({where}) finished `{status}` on that engine."
    return (
        f"WARNING: the seeded workflow run above ({where}) did NOT finish -- status `{status}`. "
        "This instance has no proven engine; treat every seeded result as suspect."
    )


def proof_engine_two(api: Api, env: dict, ids: dict, engine_set: str, rec) -> None:
    """The engine-#2 MT proof, recorded through `rec` (a `run_proofs` cell writer).

    An MT call through the OLLAMA box's ENGINE PROFILE, via the product's own translate seam.
    Under `--engines both` this is engine #2 (the seeded run proved engine #1 / LM Studio);
    under `--engines ollama` it is the same box the seeded run used, reached the OTHER way
    (an `engine:<id>` profile ref instead of the built-in `local` default), so both provider
    resolution paths are proven against the one server this instance is allowed to call.

    Under `--engines lmstudio` there IS no engine #2, so the proof is SKIPPED and says so. It is
    never recorded as `ok`: a green proof cell for a server this instance never called would be
    the same class of lie as a mock roster (round-1 gate M3). The one engine's own standing is
    DERIVED from the recorded seeded run (`ids["run_default_engine"]`), never asserted: a seeded
    run that failed, timed out or never happened must not be published here as a success."""
    if engine_set == "lmstudio":
        rec(
            "MT through engine #2",
            f"SKIPPED: engine #2 absent (lmstudio set) -- this instance has one engine, LM "
            f"Studio (`local`, {LM_STUDIO_MODEL}). Nothing was called for this proof. "
            f"{_seeded_run_standing(ids)}",
        )
        return
    label_which = "the Ollama box" if engine_set == "ollama" else "engine #2 (Ollama box)"
    try:
        eid = _ollama_engine_id(env)
        wid = api.jpost("/api/workflows", {"name": "Proof MT (Ollama)", "scope": "global"})["id"]
        api.jpost(
            f"/api/workflows/{wid}/steps",
            {
                "step_type": "llm_agent",
                "name": "MT via engine ref",
                "provider": f"engine:{eid}",
                "prompt_text": "Translate into {target_locale}. Return only the translation.\n\n"
                "{text}",
            },
        )
        started = api.jpost(
            f"/api/workflows/{wid}/run",
            {"doc_id": ids.get("document_short") or ids.get("document_text"), "locale": "es-ES"},
        )
        rid = started.get("run_id") or started.get("id")
        status = started.get("status", "running")
        deadline = time.time() + 900
        while time.time() < deadline and status in ("running", "queued", "pending"):
            _sleep(5)
            status = api.jget(f"/api/runs/{rid}").get("status", "?")
        rec(
            f"MT through {label_which}, provider ref engine:<id>",
            f"workflow {wid} run {rid} -> {status} (engine profile #{eid}, model {OLLAMA_MODEL})",
        )
    except Exception as exc:
        rec(f"MT through {label_which}", f"FAILED: {exc}")


def _ollama_engine_id(env: dict):
    listed = cli(env, "engine", "list", as_json=True, check=False)
    engines = listed if isinstance(listed, list) else (listed or {}).get("engines", [])
    for eng in engines if isinstance(engines, list) else []:
        if isinstance(eng, dict) and "Ollama" in str(eng.get("name", "")):
            return eng.get("id")
    return None


# ======================================================================================= #
# instance.md
# ======================================================================================= #
def engine_section(facts: dict) -> list:
    """The `## Engines` body, DERIVED from the engine set this instance actually came up on.

    Under `--engines ollama` there is exactly one server and LM Studio must not be mentioned as
    if it were wired: an instance.md that lists an engine the instance never calls is the same
    class of lie as a mock roster (gate M3). Under `--engines lmstudio` the same rule applies to
    the Ollama box, which is why this branch names it nowhere and states plainly that engine #2
    is ABSENT -- silence would leave a tester guessing whether a second engine exists."""
    if facts["engine_set"] == "lmstudio":
        return [
            "**Engine set: `lmstudio` (`up ... --engines lmstudio`) -- LM Studio ONLY;"
            " ENGINE #2 ABSENT. This instance has exactly one engine. No second engine was"
            " probed, no second engine profile exists, no seeded row or roster slot carries an"
            " `engine:<id>` ref, and the engine-#2 MT proof is recorded as SKIPPED, not as"
            " passed.**",
            "",
            f"- Engine #1 `local` kind: LM Studio at `{LM_STUDIO_URL}`, model id"
            f" **`{facts['lm_model']}`**"
            " (also the install default: `DEFAULT_PROVIDER=local`, `LOCAL_BASE_URL` set), so"
            " every unspecified slot (a bare workflow step, a roster default, an eval candidate)"
            " resolves to it rather than to `mock`.",
            "- Engine #2: **absent.** Nothing here reaches a second engine; a probe that needs"
            " two distinct engines is out of scope for this instance (stand one up with"
            " `--engines both` for that).",
            f"- Engine profiles created: {facts['engines']}",
            "- No cloud engine is usable: every cloud key is blanked in the child environment"
            " (list below), so `claude`/`openai`/`gemini` providers have no credential. There is"
            " no /api route that lists engine profiles (the ring is host-only). Probes:"
            " `loc-tms engine list`, `loc-tms engine test <id>` and `loc-tms providers models"
            " claude|openai|gemini` -- see the proofs section.",
            f"- Mock provider: {facts['mock_note']}",
        ]
    ollama_line = (
        f"- Ollama box at `{facts['ollama_base']}`, model id **`{facts['ollama_model']}`**"
        " (small calls only; 8k context in practice)."
    )
    if facts["engine_set"] == "ollama":
        head = [
            "**Engine set: `ollama` (`up ... --engines ollama`) -- the Ollama box ONLY."
            " LM STUDIO IS NOT PART OF THIS INSTANCE: it was never probed, no engine profile"
            " names it, and no seeded row can reach it.**",
            "",
            ollama_line,
            "- That box IS the install default: `DEFAULT_PROVIDER=local` with"
            f" `LOCAL_BASE_URL={facts['ollama_base']}` and"
            f" `DEFAULT_LOCAL_MODEL={facts['ollama_model']}`."
            " The built-in `local` provider is a plain OpenAI-compatible client"
            " (`providers._normalize_local_base` appends `/v1`), which is exactly the surface"
            " Ollama serves -- so every unspecified slot (a bare workflow step, a roster"
            " default, an eval candidate) resolves to this box, not to `mock`.",
            "- The same box also has an `openai_compatible` ENGINE PROFILE, so the"
            " `engine:<id>` resolution path is exercised too (see the MT proof below).",
        ]
    else:
        head = [
            "**Engine set: `both` (the default) -- LM Studio and the Ollama box.**",
            "",
            f"- Engine #1 `local` kind: LM Studio at `{LM_STUDIO_URL}`, model id"
            f" **`{facts['lm_model']}`**"
            " (also the install default: `DEFAULT_PROVIDER=local`, `LOCAL_BASE_URL` set).",
            f"- Engine #2 `openai_compatible` kind: {ollama_line[2:]}",
        ]
    return [
        *head,
        f"- Engine profiles created: {facts['engines']}",
        "- No cloud engine is usable: every cloud key is blanked in the child environment (list"
        " below), so `claude`/`openai`/`gemini` providers have no credential. There is no"
        " /api route that lists engine profiles (the ring is host-only). Probes:"
        " `loc-tms engine list`, `loc-tms engine test <id>` and `loc-tms providers models"
        " claude|openai|gemini` -- see the proofs section.",
        f"- Mock provider: {facts['mock_note']}",
    ]


def write_instance_md(root: Path, facts: dict) -> None:
    n = facts["name"]
    port = facts["port"]
    engine_flag = f" --engines {facts['engine_set']}"
    lines = [
        f"# Test instance `{n}` (isolated, real engines)",
        "",
        f"Created: {facts['created']} by `scripts\\test-instance.cmd up {n} {port}{engine_flag}`.",
        "No secrets in this file: the API keys and the web password live in their own files below.",
        "",
        "## Identity",
        "",
        f"- Name: `{n}`  ·  port: `{port}`  ·  base URL: `{facts['base']}`",
        f"- Commit under test: `{facts['commit']}` (branch `{facts['branch']}`) --"
        f" {facts['tree_state']}",
        f"- `/api/release`: {facts['release']}",
        f"- Instance root: `{root}`",
        f"- Data dir (throwaway): `{facts['datadir']}`",
        f"- Server log: `{root}\\server.log`",
        f"- **Listener pid** (the process BOUND to {port}): `{facts['listener_pid']}`"
        f"  ·  uvicorn supervisor pid: `{facts['supervisor_pid']}`"
        + (f"\n- **WARNING:** {facts['pid_note']}" if facts.get("pid_note") else ""),
        f"- pid files: `{root}\\server.pid` (LISTENER) and `{root}\\server.supervisor.pid`."
        " uvicorn with `workers=1` runs a supervisor that forks the worker actually holding the"
        " port, so the two differ. Kill the supervisor alone and the worker is ORPHANED still"
        " serving this port: use `down`, never Task Manager on one pid. `down` kills both pids"
        " and, if something is STILL serving this port afterwards, that owner too -- but only"
        " ever a process whose `Win32_Process.CommandLine` proves it is THIS INSTANCE: this"
        f" root's stamp `{_stamp_for(root.resolve())}` (an inert trailing comment inside the"
        " child's `-c` snippet) or this data dir, or a child of a pid already proven that way."
        " A port number is a hint, not evidence: another test instance on this port is a"
        " stranger to this root and survives `down` here. A pid file survives a reboot and"
        " Windows recycles pids, so anything `down` cannot prove is reported and left running,"
        " and `down` then exits non-zero.",
        "- **After a reboot** the pid files can name UNRELATED live processes that Windows handed"
        " the same numbers. `down` proves each one, refuses to kill what is not ours, and DELETES"
        " the pid file that named the stranger (STALE-FILE CLEANUP: the process is dead, or alive"
        " and not a Localization OS server at all); `up` does the same and carries on. If a"
        " `down` still"
        " refuses (its CIM query is broken, say), the escape hatch is to delete"
        f" `server.pid`, `server.supervisor.pid` and `server.port` in `{root}` by hand and re-run."
        " A clean `down` deletes all three itself.",
        "- **A LEGACY instance is different, and nothing about it is deleted.** The stamp is NEW"
        " (commit `a7cd3494`): a server started by an earlier `test-instance.cmd` carries no"
        " stamp, so `down` cannot prove it. It is recognised as a live python running"
        " `app.main:app` and reported as `LEGACY INSTANCE` -- pid files KEPT, nothing killed,"
        " exit non-zero -- and `up` refuses over it rather than rebuilding this root under a live"
        f" server. LEGACY TEARDOWN: `scripts\\test-instance.cmd down {root.name} --legacy-pid`,"
        " which for one call accepts a recorded pid that is an unstamped `app.main:app` owner of"
        " this root's recorded port, or the PARENT SUPERVISOR of such an owner (uvicorn"
        " `workers=1` forks the worker that holds the port, so both halves of a legacy instance"
        " are covered), while `/health` answers there. A recorded pid that owns nothing and has no"
        " port-owning child is never licensed by it; kill that one by hand.",
        f"- Tail the log: `Get-Content -Wait -Tail 50 {root}\\server.log`",
        "",
        "## Credentials (paths only)",
        "",
        f"- Admin API key (scopes `api docs keys admin`): `{root}\\api-key.txt`",
        f"- Viewer API key (scopes `api docs keys`, bound to customer 1):"
        f" `{root}\\api-key-viewer.txt`",
        f"- Web login (admin email line 1, password line 2): `{root}\\web-login.txt`",
        "- Header: `X-Loc-Token: <contents of the key file>`.",
        "",
        "## Engines (REAL, local only)",
        "",
        *engine_section(facts),
        "",
        "## Environment blanked (so `load_dotenv(override=False)` cannot reach the repo `.env`)",
        "",
        "```",
        "\n".join(BLANKED_ENV_VARS),
        "```",
        "",
        "`LOC_TMS_SERVER` is set to the EMPTY STRING in the child environment (blanked with the"
        " list above, not absent). `app/config.py` truthy-parses it, so empty is falsy and"
        " `IS_TEST` holds exactly as it would with the variable missing.",
        "",
        "### What `IS_TEST` changes vs a real install (know this before filing an issue)",
        "",
        "- no scheduler / retention daemons run (`app/main.py` startup) -- run one tick on demand"
        " with the call below;",
        "- `engines.migrate_legacy_local()` and `segmentation_profiles.migrate_legacy_custom()`"
        " are SKIPPED, so no legacy engine/segmentation profile is auto-created;",
        "- the login-wall auto-enable is skipped (this harness sets `security_auth_required=1`"
        " explicitly instead);",
        "- `_backfill_glossary_concepts()` is skipped;",
        "- `init_db()` takes the `create_all` + additive-shim path on every boot AFTER the first"
        " one (the first schema build here runs under `LOC_TMS_SERVER=1` precisely so it goes"
        " through ALEMBIC to head -- see `db migrate` in the up sequence).",
        "",
        "All five are no-ops on a fresh data dir, so nothing is wrong today; they are listed so a"
        " schema or engine-seeding surprise is read as posture, not as a product bug.",
        "",
        "Run one scheduler tick on demand:",
        "",
        "```",
        f'curl -X POST -H "X-Loc-Token: <admin key>" {facts["base"]}/api/admin/tick',
        "```",
        "",
        "## Features",
        "",
        "Seeded state: `security_auth_required=1`, `remote_admin_enabled=1`, every `opt_in`"
        " module ON except `scim` and `saml` (their toggle door moved to `/settings/enterprise`"
        " behind an operator-key step-up + boot-brick preflight, and both are"
        " REMOTE_IMMUTABLE), and every default-ON module left ON.",
        "",
        facts["feature_table"],
        "",
        "## Seeded ids",
        "",
        facts["seed_table"],
        "",
        "## Not seeded",
        "",
        "Everything below was ATTEMPTED and did not produce a row. Do not probe these expecting"
        " data.",
        "",
        facts["skip_table"],
        "",
        "## Reachability probes (NOT seeds -- nothing was created)",
        "",
        "These surfaces are COMPUTED: `/api/worklist` is derived from the other families and has"
        " no row type of its own, and `/api/reports/run` computes a report rather than persisting"
        " a report run. They answered, which is all this records.",
        "",
        facts["probe_table"],
        "",
        "## MCP",
        "",
        f"- MCP client registration `loc-os-{n}`: {facts['mcp_registered']}.",
        f"- Tool count observed over stdio (`initialize` + `tools/list`): **{facts['mcp_tools']}**"
        f" ({facts['mcp_note']}).",
        f"- Output dir: `{root}\\mcp-out`. Env the server needs: `LOC_STUDIO_BASE_URL`,"
        " `LOC_STUDIO_TOKEN` (the admin key), `LOC_STUDIO_OUTPUT_DIR`.",
        "",
        "## CLI command shapes",
        "",
        "In-process (LOCAL-ROOT: no login, no RBAC, actor `cli`) -- set the data dir first:",
        "",
        "```",
        f'$env:LOC_TMS_DATA_DIR = "{facts["datadir"]}"',
        f'$env:LOCAL_BASE_URL = "{facts["local_base"]}"; $env:DEFAULT_PROVIDER = "local"',
        f'$env:DEFAULT_LOCAL_MODEL = "{facts["local_model"]}"',
        f"{VENV_PY} -m app.cli config features",
        "```",
        "",
        "Remote (thin /api client, org-scoped by the token). `--remote` takes the token as the",
        "`--token` VALUE: there is no env var and no `--token-file`, so read the key file into a",
        "variable rather than pasting it:",
        "",
        "```",
        f'$tok = (Get-Content "{root}\\api-key.txt" -Raw).Trim()',
        f"{VENV_PY} -m app.cli --remote {facts['base']} --token $tok config features",
        "```",
        "",
        "## Live proofs observed at `up`",
        "",
        facts["proof_table"],
        "",
        "## Down",
        "",
        "```",
        f"scripts\\test-instance.cmd down {n}",
        "```",
        "",
        "Rebuild this instance on the same engine set (a fresh `up` REPLACES the whole instance"
        " root -- old keys, seed.json and instance.md included -- and re-registers the MCP"
        " entry):",
        "",
        "```",
        f"scripts\\test-instance.cmd up {n} {port}{engine_flag}",
        "```",
        "",
        "Leaves the instance root (log + seed.json) on disk; stops the server and removes the",
        "MCP registration.",
        "",
    ]
    (root / "instance.md").write_text("\n".join(lines), encoding="utf-8")


def _mock_note(seeder: Seeder) -> str:
    """The mock-provider sentence, DERIVED from what was actually seeded (round-1 gate M3).

    The old text was a constant claiming "nothing seeded here uses it" while the one Advanced
    mission ran its whole committee on `mock`. It is now computed: every seeded value is
    searched for a `"provider": "mock"` slot, and the sentence says what is true."""
    blob = json.dumps(seeder.ids, default=str).lower()
    uses_mock = '"provider": "mock"' in blob or '"provider":"mock"' in blob
    head = (
        "the `mock` provider ships in the product and stays SELECTABLE, but it is not the "
        "default (`DEFAULT_PROVIDER=local`)"
    )
    if uses_mock:
        return (
            f"{head}, and WARNING: at least one seeded roster slot still resolves to `mock` -- "
            'grep seed.json for `"provider": "mock"` before trusting an Advanced result.'
        )
    engine_set = getattr(seeder, "engine_set", "both")
    if engine_set == "ollama":
        engines_phrase = (
            "the ONE real engine of this instance's engine set (the Ollama box, reached both as "
            "the built-in `local` default and as an `engine:<id>` profile)"
        )
    elif engine_set == "lmstudio":
        engines_phrase = (
            "the ONE real engine of this instance's engine set (LM Studio, the built-in `local` "
            "default; engine #2 is absent, so no slot carries an `engine:<id>` ref)"
        )
    else:
        engines_phrase = "the two REAL local engines"
    return (
        f"{head}, and no seeded roster names it: the Advanced mission's committee (both text "
        f"delegates + the adjudicator) and the mission's default provider are {engines_phrase}, "
        "so an unspecified slot falls back to `local`, never to the offline host."
    )


def _md_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|")[:1600] for c in row) + " |")
    return "\n".join(out) if rows else "_(none)_"


# ======================================================================================= #
# verbs
# ======================================================================================= #
def cmd_up(name: str, port: int, engine_set: str = "both") -> int:
    if engine_set not in ENGINE_SETS:
        raise Abort(f"unknown engine set {engine_set!r} (choose one of {', '.join(ENGINE_SETS)})")
    root = _root_for(name)
    # Round-3 gate M2: "a live pid file" is not "a live server". `down` learned that pid files
    # survive reboots and Windows recycles pids; `up` had been left on the old rule, and the two
    # interlocked -- `down` refused to kill a stranger and KEPT the pid file, `up` saw the same
    # stranger and told the operator to run `down`. Ask the same question `down` asks, and treat
    # a pid we can prove is NOT ours as what it is: a stale file.
    _cmdline_warning_latch(False)  # round-4 gate L3
    # Read BEFORE the loop: a pid file deleted below would take the recorded port with it
    # (`_read_port` needs a pid file), and the port guard under this loop is exactly what stops
    # `up` from rmtree'ing this root out from under a live server it could not prove (round-4 H1).
    #
    # Round-5 gate M2: `server.port` is read UNGATED as the fallback. `_read_port` returns 0 when
    # no pid file exists (round-4 M1's disarm, which is right for `stop_server`'s port arm), and
    # the tool's own ESCAPE HATCH tells the operator to delete the pid files by hand -- including
    # on the CANNOT-BE-VERIFIED path, which fires precisely when the server may still be serving.
    # That left the guard below disabled in exactly the state the hatch creates, and `up <name>
    # <other port>` walked into `_rmtree(root)` over a live server. `server.port` survives the
    # hatch, so the guard reads it directly here.
    recorded_port = _read_port(root) or _read_pid(root / "server.port")
    for pidname in ("server.pid", "server.supervisor.pid"):
        pidfile = root / pidname
        pid = _read_pid(pidfile)
        if pid <= 0 or not _pid_alive(pid):
            continue
        ours, why = pid_is_ours(pid, root, recorded_port)
        if "LEGACY INSTANCE" in why:
            # Round-4 gate H1: ours-but-unprovable is NOT a stale file. `up` would rmtree this
            # root -- the key files, instance.md, seed.json and the datadir the server holds
            # open -- while it is still serving.
            raise Abort(
                f"instance {name} has a live LEGACY server (pid {pid} in {pidname}: {why}). "
                f"Refusing to rebuild this instance root while a server that is probably ours "
                f"is still running. Stop it with `scripts\\test-instance.cmd down {name} "
                f"--legacy-pid` (that flag is the sanctioned way to take down an instance "
                f"started before the stamp existed: for one call it licenses the unstamped "
                f"`app.main:app` owners of this root's recorded port and their parent supervisor, "
                f"which is both halves of a uvicorn `workers=1` instance), then re-run `up`."
            )
        if ours:
            raise Abort(
                f"instance {name} already has a live server (pid {pid}, {why}); run "
                f"`scripts\\test-instance.cmd down {name}` first. If `down` also refuses, "
                f"delete server.pid, server.supervisor.pid and server.port in {root} by hand "
                f"and re-run this command."
            )
        if "CANNOT BE VERIFIED" in why:
            raise Abort(
                f"instance {name} has a live pid {pid} in {pidname} and ownership {why}. "
                f"Refusing to rebuild this instance root while a process it may own is still "
                f"running. ESCAPE HATCH: fix the CIM query, or -- if you know the instance is "
                f"gone -- delete server.pid, server.supervisor.pid and server.port in {root} "
                f"by hand and re-run."
            )
        _say(
            f"  {pidname} names live pid {pid}, but {why} -- Windows recycles pid numbers, so "
            f"that file is stale and is being deleted (nothing was killed)"
        )
        pidfile.unlink(missing_ok=True)

    # Round-4 gate H1: the RECORDED port, not just the requested one. `up <name> <other-port>`
    # skips the check below entirely, and with the pid files gone (or never written) nothing else
    # stood between a live server and the `_rmtree(root)` further down -- which on Windows tears
    # out instance.md and the key files before failing on the locked SQLite file. Any
    # `app.main:app` python on the recorded port is either this instance or a legacy one; either
    # way this root is not safe to rebuild.
    if recorded_port:
        for owner in port_owner_pids(recorded_port):
            cmdline, _failure = _command_line(owner)
            if looks_like_loc_uvicorn(cmdline):
                raise Abort(
                    f"instance {name} still has a server on its RECORDED port {recorded_port} "
                    f"(pid {owner} is a python running `app.main:app`). Refusing to rebuild "
                    f"{root} while a live server holds its data dir open -- `up` replaces this "
                    f"root whole. Run `scripts\\test-instance.cmd down {name}` (add "
                    f"`--legacy-pid` if it reports a LEGACY INSTANCE) and re-run `up`."
                )

    # Gate M2: the PORT is checked before anything is written or killed. Without this a second
    # `up` on a busy port produced a uvicorn that died on bind while `wait_health` was satisfied
    # by the OTHER instance -- a fully fictional instance.md, an MCP entry pointing at the
    # wrong server, and a pid file naming a dead process.
    if not port_is_free(port):
        owners = port_owner_pids(port)
        raise Abort(
            f"port {port} is already in use (listener pid(s) {owners or 'unknown'}). Pick another "
            f"port or `down` whatever owns it -- refusing to start a second instance behind it."
        )

    datadir = root / "datadir"
    assert_data_dir_safe(datadir)

    _say(f"preflighting engines (set: {engine_set})...")
    lm_model, ollama_base, ollama_model = preflight_engines(engine_set)
    _say(f"  LM Studio: {lm_model or '(not part of this engine set -- not probed)'}")
    _say(
        f"  Ollama:    {ollama_model} at {ollama_base}"
        if ollama_base
        else "  Ollama:    (not part of this engine set -- not probed)"
    )

    # A STALE ROOT is replaced whole, not merged into. Everything in it belongs to an instance
    # that no longer exists: the key files hold REVOKED keys, seed.json holds ids from a datadir
    # about to be deleted, and instance.md would otherwise be a half-overwritten mix of two runs.
    # Safe by construction -- `_root_for` confines the path to `.test-instances/<name>`, the live
    # pid check above ran first, and `assert_data_dir_safe` has vetted the datadir underneath.
    if root.exists():
        stale = sorted(p.name for p in root.iterdir())
        _say(f"  replacing the stale instance root ({len(stale)} entries: {', '.join(stale[:8])})")
        _rmtree(root)
        if root.exists():
            raise Abort(f"could not remove the stale instance root {root} (a file is locked?)")
    root.mkdir(parents=True, exist_ok=True)
    datadir.mkdir(parents=True)
    env = child_env(datadir, ollama_base, engine_set)

    # Schema to ALEMBIC HEAD. app/config.py: IS_TEST = LOC_TMS_DATA_DIR set AND LOC_TMS_SERVER
    # unset, and under IS_TEST init_db() takes the create_all() branch instead of alembic. So the
    # SCHEMA step alone runs with LOC_TMS_SERVER=1 (fresh dir, no tables -> _alembic_upgrade_head);
    # every later step -- including the server itself -- runs with it UNSET, which is the
    # test-mode boot the harness wants (no scheduler / retention daemons).
    _say("initializing schema (alembic head)...")
    schema_env = dict(env)
    schema_env["LOC_TMS_SERVER"] = "1"
    cli(schema_env, "db", "migrate")

    _say("seeding the first admin user...")
    admin_email = f"admin@{name}.test"
    admin_pw = _password()
    viewer_email = f"viewer@{name}.test"
    viewer_pw = _password()
    cli(
        env,
        "init",
        "--admin-email",
        admin_email,
        "--admin-password",
        admin_pw,
        "--admin-name",
        "Test Admin",
    )
    _write_secret(root / "web-login.txt", f"{admin_email}\n{admin_pw}")

    _say("seeding settings + features...")
    cli(env, "config", "set", "security_auth_required", "1", "--yes")
    cli(env, "config", "set", "remote_admin_enabled", "1", "--yes")
    import app.modules as modules

    feature_rows = []
    for mod in modules.MODULES:
        if mod.semantics == "opt_in" and mod.slug not in OPT_IN_SKIP:
            proc = cli(env, "config", "feature", "enable", mod.slug, check=False)
            state = (
                "ON (enabled by test-instance)"
                if proc.returncode == 0
                else (f"FAILED to enable: {(proc.stderr or proc.stdout).strip()[:100]}")
            )
        elif mod.slug in OPT_IN_SKIP:
            state = "OFF (door moved to /settings/enterprise; left off on purpose)"
        else:
            state = "ON (default)"
        feature_rows.append((mod.slug, mod.semantics, mod.setting_key, state))

    _say("starting detached uvicorn...")
    proc = start_server(root, port, env)
    health = wait_health(port, proc)
    listener_pid, supervisor_pid, pid_note = record_pids(root, port, proc)
    _say(
        f"  listener pid {listener_pid} (supervisor {supervisor_pid}), "
        f"/health {json.dumps(health)[:120]}"
    )

    base = f"http://127.0.0.1:{port}"

    _say("minting keys...")
    cli(
        env,
        "user",
        "create",
        "--email",
        viewer_email,
        "--role",
        "viewer",
        "--name",
        "Test Viewer",
        "--password",
        viewer_pw,
    )
    admin_key = _mint(env, admin_email, "api docs keys admin")
    _write_secret(root / "api-key.txt", admin_key)
    viewer_key = _mint(env, viewer_email, "api docs keys")
    _write_secret(root / "api-key-viewer.txt", viewer_key)
    with open(root / "web-login.txt", "a", encoding="utf-8") as fh:
        fh.write(f"{viewer_email}\n{viewer_pw}\n")

    api = Api(base, admin_key)

    _say("seeding resource families...")
    seeder = Seeder(api, env, root, ollama_base, engine_set)
    seeder.run_all()
    # bind the viewer to customer 1 now that it exists (custbound tier)
    if seeder.ids.get("customer_1"):
        cli(
            env,
            "user",
            "customer",
            viewer_email,
            "--customer",
            str(seeder.ids["customer_1"]),
            check=False,
        )
    (root / "seed.json").write_text(
        json.dumps(
            {
                # The engine set is recorded FIRST so anyone reading seed.json knows which server
                # every `"provider": "local"` row below actually resolved to.
                "_engine_set": engine_set,
                "_engines": {
                    "default_provider": "local",
                    "local_base_url": env["LOCAL_BASE_URL"],
                    "local_model": env["DEFAULT_LOCAL_MODEL"],
                    "lm_studio": lm_model or None,
                    "ollama_base": ollama_base,
                    "ollama_model": ollama_model,
                },
                **seeder.ids,
                "_probes_not_seeds": seeder.probes,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    _say("registering MCP server (needs LOC_TEST_MCP_CLI, else skipped)...")
    mcp_state = mcp_register(name, port, admin_key, root)
    tools, mcp_note = mcp_tool_count(port, admin_key, root)
    _say(f"  {mcp_state}; tools/list -> {tools}")

    _say("running live proofs...")
    proofs = run_proofs(api, env, base, port, viewer_key, root, seeder.ids, engine_set)

    # facts sheet
    commit = _run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(_ROOT), capture_output=True, text=True
    ).stdout.strip()
    branch = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(_ROOT), capture_output=True, text=True
    ).stdout.strip()
    # "Commit under test" is a LIE on a dirty tree: this instance boots the working copy, not that
    # commit. Say which, and name the files, so a tester filing an issue quotes a reproducible
    # state instead of a commit that never ran here.
    dirty = [
        line[3:].strip()
        for line in _run(
            ["git", "status", "--porcelain"], cwd=str(_ROOT), capture_output=True, text=True
        ).stdout.splitlines()
        if line.strip()
    ]
    tree_state = "clean working tree"
    if dirty:
        tree_state = (
            f"**working tree DIRTY at `up`: {len(dirty)} path(s)** -- this instance runs the "
            f"WORKING COPY, not that commit: {', '.join(dirty[:12])}"
            f"{' ...' if len(dirty) > 12 else ''}"
        )
    rel = api.get("/api/release")
    engines = seeder.ids.get("engine_profiles", "see 'not seeded'")
    facts = {
        "name": name,
        "port": port,
        "base": base,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "commit": commit,
        "branch": branch,
        "tree_state": tree_state,
        "release": (rel.text[:200] if rel.status_code == 200 else f"{rel.status_code}"),
        "datadir": str(datadir),
        "engine_set": engine_set,
        "lm_model": lm_model,
        "ollama_base": ollama_base,
        "ollama_model": ollama_model,
        "local_base": env["LOCAL_BASE_URL"],
        "local_model": env["DEFAULT_LOCAL_MODEL"],
        "engines": json.dumps(engines, default=str)[:400],
        "listener_pid": listener_pid,
        "supervisor_pid": supervisor_pid,
        "pid_note": pid_note,
        "mock_note": _mock_note(seeder),
        "feature_table": _md_table(
            ["slug", "semantics", "setting key", "state at up"], feature_rows
        ),
        "seed_table": _md_table(
            ["family", "id(s)"],
            [(k, json.dumps(v, default=str)[:300]) for k, v in seeder.ids.items()],
        ),
        "skip_table": _md_table(["family", "reason"], seeder.skipped),
        "probe_table": _md_table(
            ["surface", "observed"],
            [(k, json.dumps(v, default=str)[:300]) for k, v in seeder.probes.items()],
        ),
        "proof_table": _md_table(["proof", "observed"], proofs),
        "mcp_registered": mcp_state,
        "mcp_tools": tools,
        "mcp_note": mcp_note,
    }
    write_instance_md(root, facts)
    _say("")
    _say(f"instance {name} is UP on {base} (engine set: {engine_set})")
    _say(f"  facts sheet: {root}\\instance.md")
    _say(
        f"  seeded {len(seeder.ids)} families, {len(seeder.skipped)} not seeded, "
        f"{len(seeder.probes)} reachability probes (not seeds)"
    )
    _say(f"  listener pid {listener_pid} on port {port} (supervisor {supervisor_pid})")
    return 0


def _mint(env: dict, user: str, scopes: str) -> str:
    """Mint a per-user API key through the product's own seam and return it (never printed)."""
    out = cli(
        env,
        "apikey",
        "issue",
        "--user",
        user,
        "--name",
        "test-instance",
        "--scopes",
        scopes,
        as_json=True,
    )
    if isinstance(out, dict):
        for field in ("api_key", "key", "token", "raw"):
            if out.get(field):
                return str(out[field]).strip()
    raise Abort(f"could not read the minted key for {user} out of `apikey issue --json`")


def _scrub_with_instance_keys(text: str, root: Path) -> str:
    """Blank THIS instance's own key values out of a captured stream (round-2 gate L2).

    `down` echoes `mcp remove` output, and the registration it removes carries
    `LOC_STUDIO_TOKEN=<admin key>` in its `--env`, so a client build that prints the removed
    entry's config would print the admin key to the console. `down` never held the key, so it
    could not scrub even in principle -- it reads the key files HERE, and the values never leave
    this function: they are only ever passed to `scrub` as needles, never returned or printed."""
    needles: list[str] = []
    for name in ("api-key.txt", "api-key-viewer.txt"):
        with contextlib.suppress(Exception):
            needles.append((root / name).read_text(encoding="utf-8").strip())
    return scrub(text, *needles)


def cmd_down(name: str, legacy_pid: bool = False) -> int:
    root = _root_for(name)
    if not root.exists():
        raise Abort(f"no instance root at {root}")
    notes, ok = stop_server(root, legacy_pid=legacy_pid)
    _say(notes)
    if ok:
        _say(f"mcp remove loc-os-{name}: {_scrub_with_instance_keys(mcp_unregister(name), root)}")
    else:
        # Round-3 gate L2: the unregister used to run unconditionally, BEFORE the `ok` test. A
        # refused `down` therefore deleted the `loc-os-<name>` entry that documents a server
        # which is still running with a live admin key -- the tester's MCP surface vanished from
        # under a serving instance, and the exit code said "failed" without saying that half the
        # teardown had happened anyway.
        _say(
            f"MCP registration `loc-os-{name}` was LEFT IN PLACE: this `down` did not stop "
            f"the instance, and removing the entry would hide a server that is still serving."
        )
    _say(f"instance root left on disk: {root}")
    if not ok:
        # Round-2 gate M1/L4: a `down` that refused to kill a process it could not prove was ours,
        # or that left a listener behind, must NOT tell a script the instance is stopped.
        sys.stderr.write(
            f"ERROR: `down {name}` did not fully stop the instance (see the WARNING lines above). "
            f"Nothing that could not be proven to belong to this instance was killed, and the MCP "
            f"entry `loc-os-{name}` was kept. ESCAPE HATCH: if the instance really is gone (a "
            f"reboot, or the machine was rebuilt), delete server.pid, server.supervisor.pid and "
            f"server.port in {root} by hand, then re-run `down` (or `up`). If the WARNING lines "
            f"say LEGACY INSTANCE, the instance is still RUNNING and was started before this "
            f"build's stamp existed: delete nothing and re-run "
            f"`scripts\\test-instance.cmd down {name} --legacy-pid`.\n"
        )
        return 2
    return 0


def cmd_status(name: str) -> int:
    root = _root_for(name)
    if not root.exists():
        _say(f"no instance root at {root}")
        return 1
    # Gate L6: the pid files are parsed through the SAME guarded reader `stop_server` uses, so a
    # junk/hand-edited `server.pid` prints "(unreadable)" instead of raising ValueError outside
    # the Abort handler and dumping a traceback on the user.
    listener = _read_pid(root / "server.pid")
    supervisor = _read_pid(root / "server.supervisor.pid")
    _say(
        f"listener pid: {listener or '(none/unreadable)'} "
        f"alive={bool(listener) and _pid_alive(listener)}"
    )
    _say(
        f"supervisor pid: {supervisor or '(none/unreadable)'} "
        f"alive={bool(supervisor) and _pid_alive(supervisor)}"
    )
    port = _read_port(root)
    port_label = f"port {port}"
    if not port:
        # Round-5 gate L2: `_read_port` gives a root with no pid file no port at all (round-4 M1,
        # which is right for `down`'s port arm -- a retired root must not go hunting). But `status`
        # is the one command an operator runs to find out whether a server is STILL UP after the
        # escape hatch told them to delete the pid files by hand, and keying off `_read_port` made
        # it answer nothing at all. Reporting is not acting: fall back to `server.port`, labelled.
        port = _read_pid(root / "server.port")
        if port:
            port_label = (
                f"port {port} (recorded; this root claims no pid -- RETIRED or hand-cleared)"
            )
    if port:
        owners = port_owner_pids(port)
        _say(f"{port_label} listener pid(s): {owners or '(nothing listening)'}")
        if owners and listener and listener not in owners:
            _say(f"  WARNING: server.pid ({listener}) does NOT own port {port} -- run `down`")
        try:
            r = _httpx().get(f"http://127.0.0.1:{port}/health", timeout=5.0)
            _say(f"/health: {r.status_code} {r.text[:160]}")
        except Exception as exc:
            _say(f"/health: unreachable ({exc})")
    listing = mcp_list()
    _say(f"MCP loc-os-{name} registered: {f'loc-os-{name}' in listing}")
    seed = root / "seed.json"
    _say(f"seed.json present: {seed.exists()}")
    if seed.exists():
        engine_set = "(not recorded -- seeded before --engines existed)"
        with contextlib.suppress(Exception):
            engine_set = json.loads(seed.read_text(encoding="utf-8")).get("_engine_set", engine_set)
        _say(f"engine set: {engine_set}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="test_instance", description=__doc__)
    ap.add_argument("verb", choices=("up", "down", "status"))
    ap.add_argument("name")
    ap.add_argument("port", nargs="?", type=int, default=8350)
    ap.add_argument(
        "--engines",
        choices=ENGINE_SETS,
        default="both",
        help=(
            "which REAL engines this instance runs on: `both` (LM Studio + the Ollama box, the "
            "default), `ollama` (the Ollama box alone -- LM Studio is never probed, configured "
            "or called, and the install default provider points at the box) or `lmstudio` (LM "
            "Studio alone -- the Ollama box is never probed, configured or called, and the "
            "instance has no engine #2). `up` only."
        ),
    )
    ap.add_argument(
        "--legacy-pid",
        action="store_true",
        help=(
            "`down` only: for exactly one call, accept a RECORDED pid of this root as this "
            "instance's, for a server started before the `# loc-test-instance:<root>#` stamp "
            "existed (any instance started before commit a7cd3494). The licence is per PID: it "
            "reaches a recorded pid ONLY while /health answers on this root's recorded port, "
            "every owner of that port is an unstamped python running `app.main:app`, AND that "
            "pid is itself one of those owners or the PARENT SUPERVISOR of one (uvicorn "
            "`workers=1` forks the worker that holds the port, so a legacy instance's supervisor "
            "owns nothing but must die, or uvicorn respawns the worker) -- so it can never reach "
            "a stamped process, a pid that owns nothing and has no port-owning child (a stale "
            "supervisor pid a reboot pointed at another root's live instance), or a dead port. "
            "A recorded pid it does not license is judged by the "
            "ordinary rules. Passing it when nothing is a LEGACY INSTANCE changes nothing: the "
            "precondition is only evaluated if a LEGACY verdict is actually reached, so a stray "
            "`--legacy-pid` on a healthy instance behaves exactly like a plain `down`. Without "
            "it, `down` reports such a server as a LEGACY INSTANCE, keeps its pid files and "
            "kills nothing."
        ),
    )
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.verb == "up":
            return cmd_up(args.name, args.port, args.engines)
        if args.verb == "down":
            return cmd_down(args.name, args.legacy_pid)
        return cmd_status(args.name)
    except Abort as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
