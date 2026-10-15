"""Production image build context, ownership and base pins, offline: no docker, no network.

Three security-review issues, each proven statically (Docker is not available here):

  #255  .dockerignore is an ALLOW-LIST. `fly deploy` builds from the working tree, and the
        old deny-list shipped every local folder it did not name (.test-instances/ with
        test keyfiles, databases, api-key.txt and web-login.txt; sample-docs/; logs). This
        suite emulates Docker's .dockerignore matcher (last match wins, a pattern also
        matches everything under a matched directory, `**` spans directories) and proves:
          * every top-level entry of .gitignore, and every named secret or local path
            (data/, .env, .claude/, .test-instances/, VIDEO_DEMOS/, planning/, ...), is
            excluded, also when secret-shaped files sit inside allowed folders;
          * every tracked file that ships is inside the allow-list, and everything the
            image needs at build or run time (Dockerfile COPY sources, /app/... paths the
            Dockerfile runs, `from scripts import X` in app/, BASE_DIR / "name" reads) ships;
          * the working tree itself (untracked folders included) sends nothing else, which
            is the reviewer's probe turned into a check;
          * mutations of the allow-list are caught.
  #256  /app is root-owned and read-only to the runtime user: the runtime COPY has no
        --chown, group/other write bits are stripped, only /data is chowned to `app`, and
        a build-time check (run as `app`) fails the build otherwise. The check's shell body
        is executed under sh with stubs for both outcomes.
  #236  every FROM takes its image from PYTHON_BASE / TAILSCALE_BASE (both Python stages
        share one ARG), no `latest`, and scripts/pin_base_images.py can pin both by digest
        (exercised with a fake resolver; real digests are never invented here). Whether
        the Dockerfile is pinned yet is REPORTED, not failed: filling the digests needs
        Docker and the network (docs/DEPLOY.md, "Base image pins").
  2026-10-06 the shipped images carry no pip (the scan flags pip's vendored packages):
        KEEP_PIP defaults to 0, only the CI check image sets 1, CI asserts pip is absent
        from the shipped build, no operator file sets KEEP_PIP, Vendor Studio drops pip too.

ASCII-only prints.
"""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_docker_image_")

from scripts import pin_base_images as pins

DOCKERFILE = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (_ROOT / ".dockerignore").read_text(encoding="utf-8")

# What may reach the builder: whole folders, and single files.
_ALLOWED_DIRS = ("app/", "alembic/", "assets/")
_ALLOWED_FILES = {
    "alembic.ini",
    "docker-entrypoint.sh",
    "pyproject.toml",
    "requirements.lock",
    "requirements-postgres.lock",
    "requirements-saml.lock",
    "requirements-ldap.lock",
    "requirements-sqlcipher.lock",
    "scripts/locks/requirements-saml-lxml.lock",
    "scripts/locks/requirements-build.lock",
    "scripts/saml_smoke.py",
    "scripts/convert_db_encryption.py",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
}
# Paths that must never ship, whatever else changes (brief + the reviewer's inventory).
_NEVER = (
    "data/loc_tms.db",
    "data/.enc_key",
    "data/uploads/a.docx",
    "data_qa/loc_tms.db",
    ".env",
    ".env.local",
    ".env.example",
    ".claude/settings.json",
    ".claude/worktrees/x/app/main.py",
    ".test-instances/inst1/.enc_key",
    ".test-instances/inst1/.session_secret",
    ".test-instances/inst1/api-key.txt",
    ".test-instances/inst1/web-login.txt",
    ".test-instances/inst1/loc_tms.db",
    "VIDEO_DEMOS/demo.mp4",
    "planning/INDEX.md",
    "tests/verify_docker_image.py",
    "docs/DEPLOY.md",
    "deploy/aws-hosted/aws-verify.sh",
    "tools/vendor_studio/run.py",
    "research/x.md",
    "sample-docs/x.docx",
    "suite_all.log",
    ".git/HEAD",
    ".github/workflows/ci.yml",
    ".pytest_cache/v/x",
    ".ruff_cache/x",
    "scratchpad/tagqa/payload.json",
    "TMSRequirements.docx",
    "Dockerfile",
    "fly.toml",
    "scripts/pregate.py",
    "scripts/test_instance.py",
    # secret-shaped files a local run could leave INSIDE an allowed folder
    "app/.env",
    "app/services/.env.local",
    "app/loc_tms.db",
    "app/data/loc_tms.db-wal",
    "alembic/.enc_key",
    "app/.session_secret",
    "app/__pycache__/main.cpython-313.pyc",
    "app/services/x.pyc",
)


# --------------------------------------------------------------------------- matcher
def _pattern_regex(pattern):
    """Docker's patternmatcher translation: `**` spans directories, `*` and `?` do not."""
    out, i = "^", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i + 1 : i + 2] == "*":
                i += 1
                if pattern[i + 1 : i + 2] == "/":
                    i += 1
                if i + 1 >= len(pattern):
                    out += ".*" if out == "^" else "(/.*)?"
                else:
                    out += "(.*/)?"
            else:
                out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(out + "$")


def compile_ignore(text):
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:].strip() if negate else line
        pattern = os.path.normpath(pattern).replace("\\", "/").lstrip("/")
        rules.append((_pattern_regex(pattern), negate, line))
    return rules


def shipped(path, rules):
    """True when `docker build` would send this context-relative path to the builder."""
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    excluded = False
    for regex, negate, _line in rules:
        if any(regex.match(c) for c in candidates):
            excluded = not negate
    return not excluded


def allowed(path):
    return path in _ALLOWED_FILES or path.startswith(_ALLOWED_DIRS)


def _bytecode(path):
    return "__pycache__/" in path or path.endswith((".pyc", ".pyo", ".pyd"))


def _tracked():
    out = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return [p for p in out.splitlines() if p]


def _gitignore_representatives():
    """One concrete path per .gitignore entry (a file inside it for folder patterns)."""
    reps = []
    for raw in (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        rep = line.lstrip("/").replace("**/", "").replace("*", "x")
        if rep.endswith("/"):
            rep += "leak.txt"
        reps.append((line, rep))
    return reps


def ignore_errors(text):
    """Every #255 rule; [] when the .dockerignore is right."""
    rules = compile_ignore(text)
    errors = []
    if not rules or rules[0][2] != "*":
        errors.append("the first pattern must be * (an allow-list, not a deny-list)")
    for line, rep in _gitignore_representatives():
        if shipped(rep, rules):
            errors.append(f"gitignored {line!r} ships ({rep})")
    for path in _NEVER:
        if shipped(path, rules):
            errors.append(f"never-ship path ships: {path}")
    for path in _tracked():
        if _bytecode(path):
            continue
        is_shipped, is_allowed = shipped(path, rules), allowed(path)
        if is_shipped and not is_allowed:
            errors.append(f"tracked file outside the allow-list ships: {path}")
        if is_allowed and not is_shipped:
            errors.append(f"allow-listed tracked file does not ship: {path}")
    for need in runtime_needs():
        if not shipped(need, rules):
            errors.append(f"the image needs {need} but it is not sent to the builder")
    return errors


def runtime_needs():
    """Context paths the build or the running image reads, derived from the sources."""
    needs = set()
    joined = DOCKERFILE.replace("\\\n", " ")
    for line in joined.splitlines():
        words = line.split()
        if words and words[0].upper() == "COPY" and not any(w.startswith("--from") for w in words):
            srcs = [w for w in words[1:-1] if not w.startswith("--")]
            for src in srcs:
                if src == ".":
                    needs.update(("app/main.py", "alembic.ini", "docker-entrypoint.sh"))
                else:
                    needs.add(src)
    for ref in re.findall(r"/app/([A-Za-z0-9_./-]+)", DOCKERFILE):
        if (_ROOT / ref).is_file():
            needs.add(ref)
    for py in (_ROOT / "app").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for mod in re.findall(r"^\s*from scripts import (\w+)", text, re.M):
            needs.add(f"scripts/{mod}.py")
        for mod in re.findall(r"^\s*(?:import|from) scripts\.(\w+)", text, re.M):
            needs.add(f"scripts/{mod}.py")
        for name in re.findall(r"BASE_DIR\s*/\s*\"([^\"]+)\"", text):
            if name in ("RELEASE", "RELEASE_COMMIT", ".env", "data"):
                continue  # written in the image, or deliberately never shipped
            target = _ROOT / name
            if target.is_dir():
                needs.update(
                    p.relative_to(_ROOT).as_posix()
                    for p in target.rglob("*")
                    if p.is_file() and not _bytecode(p.as_posix())
                )
            else:
                needs.add(name)
    return sorted(needs)


# --------------------------------------------------------------------------- #255
def _allow_list_holds():
    errors = ignore_errors(DOCKERIGNORE)
    assert not errors, "\n".join(errors[:40])
    rules = compile_ignore(DOCKERIGNORE)
    tracked = [p for p in _tracked() if not _bytecode(p)]
    count = sum(1 for p in tracked if shipped(p, rules))
    print(
        f"  .dockerignore allow-list: {count} of {len(tracked)} tracked files ship, all inside it"
    )
    print(f"      excluded: {len(_gitignore_representatives())} gitignored entries and")
    print(
        f"      {len(_NEVER)} never-ship paths; shipped: all {len(runtime_needs())} runtime needs"
    )
    print("      (COPY sources, /app paths, scripts imports, BASE_DIR reads)")


def _working_tree_probe():
    """The reviewer's probe: top-level entries of THIS checkout that would ship."""
    rules = compile_ignore(DOCKERIGNORE)
    leaks = []
    for name in sorted(os.listdir(_ROOT)):
        full = _ROOT / name
        if full.is_dir():
            for dirpath, dirnames, files in os.walk(full):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for f in files:
                    rel = (Path(dirpath) / f).relative_to(_ROOT).as_posix()
                    if shipped(rel, rules) and not allowed(rel) and not _bytecode(rel):
                        leaks.append(rel)
                if len(leaks) > 20:
                    break
        elif shipped(name, rules) and not allowed(name):
            leaks.append(name)
    assert not leaks, f"working tree would ship: {leaks[:20]}"
    print("  working tree probe: nothing outside the allow-list would be sent to the builder")


def _allow_list_mutations():
    base = DOCKERIGNORE
    mutants = {
        "deny-list (no leading *)": base.replace("\n*\n", "\n", 1),
        "planning re-included": base + "\n!planning/\n",
        "all scripts re-included": base + "\n!scripts/\n",
        "tests re-included": base + "\n!tests/\n",
        ".test-instances re-included": base + "\n!.test-instances/\n",
        "nested .env no longer excluded": base.replace("**/.env\n", "", 1),
        "nested databases no longer excluded": base.replace("**/*.db\n", "", 1),
        "migrations dropped": base.replace("!alembic/\n", "", 1),
        "db encrypt helper dropped": base.replace("!scripts/convert_db_encryption.py\n", "", 1),
        "SAML smoke dropped": base.replace("!scripts/saml_smoke.py\n", "", 1),
        "locks dropped": base.replace("!requirements-saml.lock\n", "", 1),
        "old deny-list (base a3a45f2f shape)": (
            ".venv/\ndata/\n.env\n.git/\n.claude/\ntests/\n*.md\n!README.md\ndeploy/\n"
        ),
    }
    for name, mutant in mutants.items():
        assert mutant != base, f"mutation {name!r} did not apply"
        assert ignore_errors(mutant), f".dockerignore mutation not caught: {name}"
    print(f"  {len(mutants)} .dockerignore mutations all caught (incl. the old deny-list)")


def _matcher_self_test():
    rules = compile_ignore("*\n!app/\n**/__pycache__/\n**/*.db\n!scripts/a.py\n")
    cases = {
        "app/main.py": True,
        "app/x/__pycache__/m.pyc": False,
        "app/x.db": False,
        "x.db": False,
        "scripts/a.py": True,
        "scripts/b.py": False,
        "planning/x.md": False,
        "README.md": False,
    }
    for path, want in cases.items():
        assert shipped(path, rules) is want, (path, want)
    print(f"  matcher self-test: {len(cases)} Docker semantics cases agree")


# --------------------------------------------------------------------------- #256
_OWNERSHIP_CHECK = (
    'RUN test -w /data && test -z "$(find /app -writable -print -quit)" || '
    '{ echo "image check FAILED: /app must be read-only and /data writable for user app" '
    ">&2; exit 1; }"
)


def _runtime_instructions(content):
    stages, current = [], None
    for raw in content.replace("\\\n", " ").splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("FROM "):
            current = []
            stages.append((line, current))
        elif current is not None:
            current.append(line)
    for head, instructions in stages:
        if head.endswith(" AS runtime"):
            return instructions
    raise AssertionError("runtime stage missing")


def ownership_errors(content, *, require_release=True):
    errors = []
    try:
        runtime = _runtime_instructions(content)
    except AssertionError as exc:
        return [str(exc)]
    copies = [i for i, ins in enumerate(runtime) if ins.startswith("COPY ") and "--from" not in ins]
    if [runtime[i] for i in copies] != ["COPY . /app"]:
        errors.append(f"the app must be copied as root with exactly 'COPY . /app': {copies}")
    for ins in runtime:
        # a chown command (up to the next && ; |) whose arguments include /app or below
        if re.search(r"\bchown\b[^&;|]*\s/app(/|\s|$)", ins):
            errors.append(f"/app must not be chowned: {ins}")
        if "--chown" in ins or "--chmod" in ins:
            errors.append(f"no COPY --chown/--chmod in the runtime stage: {ins}")
    users = [i for i, ins in enumerate(runtime) if ins.startswith("USER ")]
    if [runtime[i] for i in users] != ["USER app"]:
        errors.append(f"expected exactly one 'USER app': {[runtime[i] for i in users]}")
        return errors
    user_at = users[0]
    if not any("chown -R app:app /data" in ins for ins in runtime[:user_at]):
        errors.append("/data must stay owned by the app user")
    strip = [i for i, ins in enumerate(runtime) if "chmod -R go-w /app" in ins]
    if not strip or strip[0] > user_at:
        errors.append("group/other write bits must be stripped from /app as root")
    release = [i for i, ins in enumerate(runtime) if "> /app/RELEASE" in ins]
    if require_release and (
        not release or release[0] > user_at or (copies and release[0] < copies[0])
    ):
        errors.append("RELEASE must be written as root after the app copy")
    checks = [i for i, ins in enumerate(runtime) if ins == _OWNERSHIP_CHECK]
    if len(checks) != 1 or checks[0] < user_at:
        errors.append("the build-time ownership check must run once, as the app user")
    if copies and copies[0] > user_at:
        errors.append("the app copy must happen before USER app")
    return errors


def _sh():
    candidates = [shutil.which("sh"), shutil.which("bash")]
    git = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent
        candidates += [git_root / "bin" / "sh.exe", git_root / "usr" / "bin" / "sh.exe"]
    for found in candidates:
        low = str(found or "").replace("\\", "/").lower()
        if (
            found
            and "/system32/" not in low
            and "/windowsapps/" not in low
            and Path(found).exists()
        ):
            return str(found)
    return None


def _ownership_holds():
    errors = ownership_errors(DOCKERFILE)
    assert not errors, errors
    mutants = {
        "COPY --chown=app:app (base)": DOCKERFILE.replace(
            "COPY . /app", "COPY --chown=app:app . /app"
        ),
        "chown /app later": DOCKERFILE.replace(
            "&& chmod -R go-w /app", "&& chmod -R go-w /app && chown -R app:app /app"
        ),
        "write bits kept": DOCKERFILE.replace(" \\\n    && chmod -R go-w /app", ""),
        "check removed": DOCKERFILE.replace("RUN test -w /data", "RUN true || test -w /data"),
        "check made non-fatal": DOCKERFILE.replace("         exit 1; }", "         true; }"),
        "check run as root": DOCKERFILE.replace(
            "USER app\n\n# Build-time proof", "# Build-time proof", 1
        ).replace("EXPOSE 8000", "USER app\nEXPOSE 8000"),
        "COPY --chmod": DOCKERFILE.replace("COPY . /app", "COPY --chmod=777 . /app"),
    }
    for name, mutant in mutants.items():
        assert mutant != DOCKERFILE, f"mutation {name!r} did not apply"
        assert ownership_errors(mutant), f"ownership mutation not caught: {name}"
    print(f"  /app root-owned + read-only to app, /data writable; {len(mutants)} mutations caught")
    sh = _sh()
    if sh is None:
        print("  SKIP: no sh on PATH; the ownership check body was not executed")
        return
    body = _OWNERSHIP_CHECK[len("RUN ") :]
    with tempfile.TemporaryDirectory() as data:
        data_sh = data.replace("\\", "/")
        missing = data_sh + "/missing"

        def run(find_out, data_dir):
            script = "find() { printf '%s' \"$FIND_OUT\"; }\n" + body.replace("/data", data_dir)
            env = dict(os.environ, FIND_OUT=find_out)
            proc = subprocess.run(
                [sh, "-c", script], capture_output=True, text=True, env=env, timeout=60
            )
            return proc.returncode, proc.stdout + proc.stderr

        rc, out = run("", data_sh)
        assert rc == 0 and not out.strip(), ("read-only /app + writable /data must pass", rc, out)
        rc, out = run("/app/app/main.py", data_sh)
        assert rc != 0 and "image check FAILED" in out, ("a writable /app must fail", rc, out)
        rc, out = run("", missing)
        assert rc != 0 and "image check FAILED" in out, ("an unwritable /data must fail", rc, out)
    print("  sh: the ownership check passes read-only /app, fails writable /app or no /data")


# --------------------------------------------------------------------------- #236
_FAKE = {  # test-only digests for the resolver stub; never written to the Dockerfile
    "python:3.13-slim": "sha256:" + "a" * 64,
    "tailscale/tailscale:v1.102.5": "sha256:" + "b" * 64,
}


def _base_pins_hold():
    errors = pins.base_errors(DOCKERFILE, require_digest=True)
    assert not errors, errors
    args = pins.base_args(DOCKERFILE)
    py = pins.REF_RE.match(args["PYTHON_BASE"])
    assert py and py["tag"] == "3.13-slim", args["PYTHON_BASE"]
    assert pins.REF_RE.match(args["TAILSCALE_BASE"])["tag"].startswith("v")
    for arg in pins.unpinned(DOCKERFILE):
        print(f"  PENDING: {arg}={args[arg]} has no digest yet (run pin_base_images.py)")
    mutants = {
        "literal python FROM (base)": DOCKERFILE.replace(
            "FROM ${PYTHON_BASE} AS runtime", "FROM python:3.13-slim AS runtime"
        ),
        "literal tailscale FROM (base)": DOCKERFILE.replace(
            "FROM ${TAILSCALE_BASE} AS tailscale", "FROM tailscale/tailscale:v1.102.5 AS tailscale"
        ),
        "stages drift onto two ARGs": DOCKERFILE.replace(
            "FROM ${PYTHON_BASE} AS builder", "FROM ${TAILSCALE_BASE} AS builder"
        ),
        "floating latest": re.sub(
            r"(?m)^ARG TAILSCALE_BASE=tailscale/tailscale:v[0-9.]+",
            "ARG TAILSCALE_BASE=tailscale/tailscale:latest",
            DOCKERFILE,
        ),
        "ARG missing": re.sub(r"(?m)^ARG PYTHON_BASE=.*\n", "", DOCKERFILE),
        "wrong image name": re.sub(
            r"(?m)^ARG PYTHON_BASE=python:", "ARG PYTHON_BASE=pyth0n:", DOCKERFILE
        ),
        "malformed digest": re.sub(
            r"(?m)^(ARG PYTHON_BASE=\S+?)(@sha256:[0-9a-f]+)?$", r"\1@sha256:1234", DOCKERFILE
        ),
        "digest without a tag": re.sub(
            r"(?m)^ARG PYTHON_BASE=\S+$", "ARG PYTHON_BASE=python@sha256:" + "c" * 64, DOCKERFILE
        ),
        "extra unpinned stage": DOCKERFILE + "\nFROM debian:bookworm AS extra\n",
    }
    for name, mutant in mutants.items():
        assert mutant != DOCKERFILE, f"mutation {name!r} did not apply"
        assert pins.base_errors(mutant), f"base-image mutation not caught: {name}"

    # The pin script, with a fake resolver: both ARGs pinned, tag kept, nothing else moved.
    unpinned_text = re.sub(r"(?m)^(ARG \w+_BASE=[^@\s]+)@\S+$", r"\1", DOCKERFILE)
    pinned, changes = pins.pin(unpinned_text, _FAKE.__getitem__)
    assert len(changes) == 2 and not pins.base_errors(pinned, require_digest=True), changes
    assert pins.base_args(pinned)["PYTHON_BASE"] == "python:3.13-slim@" + _FAKE["python:3.13-slim"]
    before = [ln for ln in unpinned_text.splitlines() if not re.match(r"ARG \w+_BASE=", ln)]
    after = [ln for ln in pinned.splitlines() if not re.match(r"ARG \w+_BASE=", ln)]
    assert before == after, "pinning must change only the two ARG lines"
    assert pins.pin(pinned, _FAKE.__getitem__)[1] == [], "re-pinning the same digest is a no-op"
    bumped, changes = pins.pin(pinned, lambda ref: "sha256:" + "d" * 64)
    assert len(changes) == 2 and "d" * 64 in pins.base_args(bumped)["TAILSCALE_BASE"]
    for bad in ("sha256:xyz", "", "md5:" + "a" * 32):
        try:
            pins.pin(unpinned_text, lambda ref, bad=bad: bad)
        except ValueError:
            continue
        raise AssertionError(f"pin() accepted a malformed digest {bad!r}")
    assert pins.base_errors(unpinned_text, require_digest=True), "--check must fail unpinned"

    # The registry resolver sends a token request then HEAD for the index.
    digest = "sha256:" + "e" * 64
    calls = []
    registry_reply = b'{"token": "fixture"}'  # pregate: allow-secret offline registry fixture

    class Response(io.BytesIO):
        headers = {"Docker-Content-Digest": digest}  # noqa: RUF012 - mutable test fixture

    def fetch(request, **kwargs):
        calls.append(request)
        if request.get_method() == "HEAD":
            assert request.full_url.endswith("/v2/library/python/manifests/3.13-slim")
            assert "application/vnd.oci.image.index.v1+json" in request.headers["Accept"]
            assert "manifest.list.v2+json" in request.headers["Accept"]
            return Response()
        assert request.full_url.startswith("https://auth.docker.io/token?")
        return Response(registry_reply)

    assert pins.resolve_digest("python:3.13-slim", fetcher=fetch) == digest
    assert len(calls) == 2
    Response.headers = {}
    try:
        pins.resolve_digest("python:3.13-slim", fetcher=fetch)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Accepted a missing registry digest")

    # --check through main() on a temp copy: 1 unpinned, 0 pinned.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Dockerfile"
        path.write_bytes(unpinned_text.encode("utf-8"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert pins.main(["--check", "--dockerfile", str(path)]) == 1
        assert out.getvalue().count("is not pinned by digest") == 2, out.getvalue()
        path.write_bytes(pinned.encode("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            assert pins.main(["--check", "--dockerfile", str(path)]) == 0
        assert b"\r\n" not in path.read_bytes()
    print(f"  base images via PYTHON_BASE/TAILSCALE_BASE; {len(mutants)} mutations caught;")
    print("      pin_base_images: pins both, keeps tags, touches only the ARG lines,")
    print("      rejects malformed digests, --check fails unpinned and passes pinned")


def _docs_name_the_mechanism():
    deploy = (_ROOT / "docs/DEPLOY.md").read_text(encoding="utf-8")
    assert "### Base image pins" in deploy and "pin_base_images.py --check" in deploy
    assert "scripts/pin_base_images.py" in DOCKERFILE
    print("  docs/DEPLOY.md and the Dockerfile name the pin script and --check")


def _hosted_image_pins():
    for name in (
        "docker-compose.yml",
        ".github/workflows/ci.yml",
        "docs/embedded.md",
        "deploy/aws-hosted/caddy/Caddyfile.cloudflare",
    ):
        content = (_ROOT / name).read_text(encoding="utf-8")
        # Any caddy/postgres tag (caddy:latest too, gate F3); "/etc/caddy:ro" mounts are paths.
        references = re.findall(
            r"(?<![\w/.-])(?:caddy|postgres):[\w.\-]+(?:@sha256:[0-9a-f]{64})?", content
        )
        assert references and all("@sha256:" in ref for ref in references), name
    vendor = (_ROOT / "tools/vendor_studio/Dockerfile").read_text(encoding="utf-8")
    assert len(re.findall(r"^FROM python:3.13-slim@sha256:[0-9a-f]{64} AS", vendor, re.M)) == 2


def _vendor_image_holds():
    root = _ROOT / "tools/vendor_studio"
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    ignore = (root / ".dockerignore").read_text(encoding="utf-8")

    def context_errors(content):
        rules = compile_ignore(content)
        errors = []
        if not rules or rules[0][2] != "*":
            errors.append("Vendor Studio needs an allow-list")
        required = {"run.py", "requirements.lock"}
        for path in _tracked():
            prefix = "tools/vendor_studio/"
            if path.startswith(prefix):
                relative = path[len(prefix) :]
                if relative.startswith(("vstudio/", "static/", "templates/")):
                    required.add(relative)
                elif relative not in required and shipped(relative, rules):
                    errors.append(f"file outside allow-list ships: {relative}")
        for path in required:
            if not shipped(path, rules):
                errors.append(f"runtime file excluded: {path}")
        forbidden = ["new-local-folder/secret.txt", "tests/test_hardening.py", "README.md"]
        for prefix in ("", "vstudio/", "static/nested/", "templates/"):
            forbidden.extend(
                prefix + name
                for name in (
                    ".env",
                    ".env.local",
                    "private.db",
                    "private.db-wal",
                    "private.db-shm",
                    ".enc_key",
                    ".session_secret",
                    ".setup_token",
                    "data/private.txt",
                    "__pycache__/config.pyc",
                    "config.pyc",
                )
            )
        for path in forbidden:
            if shipped(path, rules):
                errors.append(f"private file ships: {path}")
        return errors

    assert not ownership_errors(dockerfile, require_release=False), ownership_errors(
        dockerfile, require_release=False
    )
    assert not context_errors(ignore), context_errors(ignore)
    for old, new in (
        ("COPY . /app", "COPY --chown=app:app . /app"),
        ("chmod -R go-w /app", "true"),
        ("RUN test -w /data", "RUN true || test -w /data"),
    ):
        mutant = dockerfile.replace(old, new)
        assert mutant != dockerfile
        assert ownership_errors(mutant, require_release=False), old
    for mutant in (
        ignore.replace("*\n", "", 1),
        ignore + "\n!vstudio/.env\n",
        ignore.replace("!templates/", "templates/"),
        "__pycache__/\n**/__pycache__/\n*.pyc\ndata/\n*.db\n.env\n.env.*\ntests/\n",
    ):
        assert mutant != ignore
        assert context_errors(mutant)
    print("  Vendor Studio: read-only app, writable data, allow-listed context; mutations caught")


_PIP_PATHS = (
    "/usr/local/lib/python3.13/site-packages/pip",
    "/opt/venv/lib/python3.13/site-packages/pip",
    "/usr/local/bin/pip3",
    "/opt/venv/bin/pip3",
)


def _stage_after(content, marker, copy):
    """Instructions of the stage that starts at `marker`, after its venv COPY; None if absent."""
    joined = content.replace("\\\n", " ")
    _head, found, body = joined.partition(marker)
    if not found:
        return None
    body = re.split(r"(?m)^FROM ", body, maxsplit=1)[0]
    _before, found, after = body.partition(copy)
    return after if found else None


def pip_removal_errors(dockerfile, ci, vendor, operator_files):
    """The shipped images carry no pip (2026-10-06 scan fix); [] when that still holds."""
    errors = []
    copy = "COPY --from=builder /opt/venv /opt/venv"
    runtime = _stage_after(dockerfile, "FROM ${PYTHON_BASE} AS runtime", copy)
    if runtime is None:
        errors.append("runtime stage or its venv COPY missing")
    else:
        if not re.search(r"(?m)^ARG KEEP_PIP=0\s*$", runtime):
            errors.append("KEEP_PIP must default to 0 after the venv COPY")
        removal = re.search(r'(?m)^RUN if \[ "\$KEEP_PIP" = 0 \]; then\s+rm -rf ([^;]*);', runtime)
        if not removal or not all(path in removal.group(1).split() for path in _PIP_PATHS):
            errors.append("the default build must rm pip from system and venv Python")
    build = [line for line in ci.splitlines() if "docker build " in line]
    ship = [line for line in build if "-t localization-os-release-ship ." in line]
    check = [line for line in build if "-t localization-os-release-check ." in line]
    if len(ship) != 1 or "KEEP_PIP" in ship[0]:
        errors.append("CI must build the shipped default image once, without KEEP_PIP")
    if len(check) != 1 or "--build-arg KEEP_PIP=1 " not in check[0]:
        errors.append("CI check image must be the only KEEP_PIP=1 build")
    if "KEEP_PIP" in "".join(build).replace(check[0] if check else "", ""):
        errors.append("KEEP_PIP set on another CI build")
    probe = [line for line in ci.splitlines() if "localization-os-release-ship -c " in line]
    if len(probe) != 1 or not all(
        needle in probe[0]
        for needle in (
            "/usr/local/bin/python3 /opt/venv/bin/python",
            'sys.exit(u.find_spec(\\"pip\\") is not None)" || exit 1',
            'for tool in pip pip3 pip3.13; do ! command -v "$tool" || exit 1; done',
        )
    ):
        errors.append("CI must assert pip is absent from the shipped image")
    for name, text in operator_files.items():
        if "KEEP_PIP" in text:
            errors.append(f"{name} sets KEEP_PIP on a served image")
    vendor_runtime = _stage_after(vendor, "AS runtime", copy)
    if vendor_runtime is None or not re.search(
        r"(?m)^RUN rm -rf .*" + re.escape("/opt/venv/lib/python3.13/site-packages/pip "),
        vendor_runtime,
    ):
        errors.append("Vendor Studio runtime must rm pip after its venv COPY")
    return errors


def _no_pip_at_runtime():
    ci = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    vendor = (_ROOT / "tools/vendor_studio/Dockerfile").read_text(encoding="utf-8")
    operator_files = {
        path: (_ROOT / path).read_text(encoding="utf-8")
        for path in subprocess.run(
            ["git", "ls-files", "docker-compose*.yml", "deploy"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if path.endswith((".sh", ".yml", ".yaml", ".toml", ".md"))
    }
    assert operator_files, "no operator files found"
    errors = pip_removal_errors(DOCKERFILE, ci, vendor, operator_files)
    assert not errors, errors
    mutants = {
        "KEEP_PIP default 1": (DOCKERFILE.replace("ARG KEEP_PIP=0", "ARG KEEP_PIP=1"), ci, vendor),
        "venv pip kept": (
            DOCKERFILE.replace("/opt/venv/lib/python3.13/site-packages/pip ", "/tmp/x ", 1),
            ci,
            vendor,
        ),
        "shipped CI build keeps pip": (
            DOCKERFILE,
            ci.replace(
                "docker build --build-arg RELEASE=",
                "docker build --build-arg KEEP_PIP=1 --build-arg RELEASE=",
                1,
            ),
            vendor,
        ),
        "absence probe dropped": (
            DOCKERFILE,
            "\n".join(line for line in ci.splitlines() if "release-ship -c" not in line),
            vendor,
        ),
        "probe passes on a missing interpreter": (
            DOCKERFILE,
            ci.replace('is not None)" || exit 1', 'is not None)" 2>/dev/null', 1),
            vendor,
        ),
        "Vendor Studio keeps pip": (
            DOCKERFILE,
            ci,
            vendor.replace(
                "RUN rm -rf /usr/local/lib/python3.13/site-packages/pip", "RUN true || rm -rf /x", 1
            ),
        ),
    }
    for name, (dockerfile, ci_text, vendor_text) in mutants.items():
        assert (dockerfile, ci_text, vendor_text) != (DOCKERFILE, ci, vendor), name
        assert pip_removal_errors(dockerfile, ci_text, vendor_text, operator_files), name
    served = dict(operator_files, **{"fly.toml": 'KEEP_PIP = "1"\n'})
    assert pip_removal_errors(DOCKERFILE, ci, vendor, served), "operator KEEP_PIP"
    print(
        f"  no pip in shipped images (Dockerfile, CI probe, Vendor Studio);"
        f" {len(mutants) + 1} mutations caught"
    )


def main():
    _no_pip_at_runtime()
    _vendor_image_holds()
    _hosted_image_pins()
    _matcher_self_test()
    _allow_list_holds()
    _working_tree_probe()
    _allow_list_mutations()
    _ownership_holds()
    _base_pins_hold()
    _docs_name_the_mechanism()
    print("DOCKER IMAGE OK")


if __name__ == "__main__":
    main()
